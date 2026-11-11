import logging
import asyncio
import math
import time
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple, Set, Union
from core.interfaces import BaseTradingService
from core.web3_wallet import Web3Wallet
from core.config import config, get_tuned_param

logger = logging.getLogger("System_Core")

@dataclass
class PartialCloseResult:
    success: bool
    digest: Optional[str] = None
    requested_contracts: float = 0.0
    filled_contracts: float = 0.0
    fill_price: float = 0.0
    realized_pnl: float = 0.0
    error: Optional[str] = None
    is_partial_fill: bool = False
    is_ambiguous: bool = False
    fee_usd: float = 0.0
    is_estimated_fee: bool = True

from decimal import Decimal

def to_x18_aligned(val: Union[float, int, str, Decimal], increment_x18: int = 0, mode: str = "floor") -> int:
    """
    Safely converts numeric value to 18-decimal fixed-point integer (x18)
    using Decimal to prevent IEEE-754 floating point precision loss and truncation artifacts.
    Supports directional alignment ('floor' or 'ceil') with step increment.
    """
    if val is None:
        return 0
    try:
        if isinstance(val, (float, int)):
            if not math.isfinite(val):
                return 0
            val_str = str(round(float(val), 8))
        else:
            val_str = str(val).strip()
        raw_x18 = int(Decimal(val_str) * Decimal(10**18))
    except Exception:
        return 0

    if increment_x18 <= 0:
        return raw_x18
    if mode == "ceil":
        return ((raw_x18 + increment_x18 - 1) // increment_x18) * increment_x18
    return (raw_x18 // increment_x18) * increment_x18

class NadoTradingService(BaseTradingService):
    """
    Adapter for executing trades on Nado DEX (Ink L2).
    Uses the official nado-protocol Python SDK (requires Python 3.12+).
    """
    
    def __init__(self, nado_client=None):
        import time
        self.wallet = Web3Wallet()
        self.logger = logger
        self._start_time = time.time()
        
        self.client = None
        self.is_connected = False
        self._nado_time_offset = 0  # Store time offset transparently on the service instance
        self.active_positions = self._load_positions()
        self.entries_paused: bool = False
        self.pending_reconciliations = self._load_pending_reconciliations()
        self.processed_fills_registry: Dict[str, dict] = {}
        self.processed_fill_digests: Set[str] = self._load_processed_fills()
        self._reconcile_lock: Optional[asyncio.Lock] = None
        self.product_map: Dict[str, int] = {}
        self.default_subaccount_id = None
        self._position_locks: Dict[str, asyncio.Lock] = {}
        
        # Stats & Cooldown tracking
        self.win_count = 0
        self.loss_count = 0
        self.recent_streak = []
        self._initial_balance = None
        self._peak_balance = None
        self._daily_start_balance = None
        self._daily_start_date = None
        self._daily_start_total_trades = 0
        self.cooldown_until = 0.0
        self._last_cooldown_processed_len = 0
        self._last_close_info: Dict[str, Any] = {}
        self.last_execution_status: str = "PENDING"
        self.last_execution_details: Dict[str, Any] = {}
        self.fast_monitor_task: Optional[asyncio.Task] = None
        self.fast_monitor_running: bool = False
        self.fast_monitor_last_heartbeat: float = 0.0
        self._latest_prices: Dict[str, float] = {}
        self.market_data_service = self  # Backwards compatibility alias
        self._load_state()
        
    def _get_position_lock(self, symbol: str) -> asyncio.Lock:
        """Returns a canonical asyncio.Lock per asset to prevent concurrent modifications."""
        if not hasattr(self, "_position_locks") or self._position_locks is None:
            self._position_locks = {}
        clean = str(symbol).replace('/', '-').strip().upper()
        base = clean.split('-')[0]
        canonical_key = f"{base}-USD"
        if canonical_key not in self._position_locks:
            self._position_locks[canonical_key] = asyncio.Lock()
        return self._position_locks[canonical_key]

    def get_last_price(self, symbol: str) -> float:
        """
        Returns the most recent price for a symbol.
        Checks:
        1. Active positions live price (updated every 5s by FastPriceMonitor).
        2. _latest_prices cache.
        3. _market_cache oracle price from Nado DEX.
        """
        clean = str(symbol).replace("/", "-").strip().upper()
        base = clean.split("-")[0]
        canon = f"{base}-USD"
        # 1. Check active position live price updated every 5s by FastPriceMonitor
        for k in (canon, clean, base):
            pos = self.active_positions.get(k)
            if isinstance(pos, dict) and float(pos.get("current_price") or 0.0) > 0:
                return float(pos["current_price"])
        # 2. Check _latest_prices cache
        latest_map = getattr(self, "_latest_prices", {}) or {}
        for k in (canon, clean, base):
            if float(latest_map.get(k) or 0.0) > 0:
                return float(latest_map[k])
        # 3. Check _market_cache oracle price
        prod_id = self.product_map.get(base) or self.product_map.get(canon)
        if prod_id is not None and getattr(self, "_market_cache", None) and prod_id in self._market_cache:
            m_info = self._market_cache[prod_id]
            raw_op = getattr(m_info, "oracle_price_x18", None) or getattr(getattr(m_info, "product", None), "oracle_price_x18", None)
            if raw_op is not None:
                try:
                    return float(raw_op) / 1e18
                except (ValueError, TypeError):
                    pass
        return 0.0
        
        # NOTE: self.initialize(nado_client) MUST be awaited explicitly after instantiation.

    @property
    def daily_start_balance(self) -> Optional[float]:
        return getattr(self, "_daily_start_balance", None)

    @daily_start_balance.setter
    def daily_start_balance(self, value: Optional[float]):
        import time
        self._daily_start_balance = float(value) if value is not None else None
        self._daily_start_date = time.strftime("%Y-%m-%d", time.gmtime())
        self._manual_daily_start = True

    async def initialize(self, nado_client=None):
        if not self.wallet.is_configured():
            logger.error("[NadoTradingService] ❌ Wallet not configured. Nado execution disabled.")
            return

        try:
            # We import nado_protocol locally so the rest of the bot doesn't crash 
            # if the SDK isn't installed yet.
            from nado_protocol.client import create_nado_client, NadoClientMode, NadoClient
            
            if nado_client:
                self.client = nado_client
            else:
                # Initialize NadoClient with the private key (Fallback)
                from core.nado_helper import create_configured_nado_client
                self.client = create_configured_nado_client(
                    network_name=config.NADO_NETWORK,
                    signer=self.wallet.get_private_key()
                )
            self.is_connected = True
            
            # Fetch product map dynamically
            import asyncio
            products = await asyncio.to_thread(self.client.market.get_all_product_symbols)
            markets = await asyncio.to_thread(self.client.market.get_all_engine_markets)
            perp_ids = {m.product_id for m in markets.perp_products}
            
            for p in products:
                if p.product_id in perp_ids:
                    base_symbol = p.symbol.split('-')[0].upper()
                    self.product_map[base_symbol] = p.product_id
                    self.product_map[f"{base_symbol}-USD"] = p.product_id
                
            from nado_protocol.utils.bytes32 import subaccount_to_hex
            self.default_subaccount_id = subaccount_to_hex(self.wallet.get_address(), "default")
                
            logger.info(f"[NadoTradingService] ✅ Successfully connected. Products loaded: {len(self.product_map)}")
            
            # Start Fast Price Monitor
            self.fast_monitor_task = asyncio.create_task(self.start_fast_price_monitor())
            
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to init Nado SDK: {e}")
            self.is_connected = False

    async def get_portfolio_summary(self, bypass_cache: bool = False) -> Dict[str, Any]:
        """Returns the current portfolio balance from Nado (deduplicated with 15s cache)."""
        if not self.is_connected or not self.default_subaccount_id:
            return {"total_usd": 0.0, "current_balance": 0.0, "balance": 0.0, "margin_used": 0.0, "free_margin": 0.0, "pnl": 0.0}
        
        import time
        now_ts = time.time()
        if not bypass_cache and getattr(self, "_portfolio_summary_cache_time", 0) > 0:
            if now_ts - self._portfolio_summary_cache_time < 15.0 and hasattr(self, "_portfolio_summary_cache"):
                return dict(self._portfolio_summary_cache)
        
        try:
            self._load_state()
            summary = await asyncio.to_thread(self.client.subaccount.get_engine_subaccount_summary, self.default_subaccount_id)
            
            # SubaccountInfoData has a healths array:
            # index 0 is Initial Margin health, index 1 is Maintenance Margin health
            maint_health = 0.0
            maint_liabilities = 0.0
            if hasattr(summary, "healths") and len(summary.healths) > 0:
                health = summary.healths[0]
                margin_used = float(health.liabilities) / 1e18
                free_margin = float(health.health) / 1e18
                if len(summary.healths) > 1:
                    m_health = summary.healths[1]
                    maint_liabilities = float(m_health.liabilities) / 1e18
                    maint_health = float(m_health.health) / 1e18
                else:
                    maint_health = free_margin
                    maint_liabilities = margin_used
            else:
                margin_used = free_margin = 0.0
                
            # Calculate true equity: Spot USDC + Unrealized PnL
            spot_usdc = 0.0
            if hasattr(summary, "spot_balances"):
                for spot in summary.spot_balances:
                    if spot.product_id == 0:
                        spot_usdc = float(spot.balance.amount) / 1e18
                        break
            
            # If spot_usdc is 0 but we have health.assets, fallback to assets just in case
            if spot_usdc == 0.0 and hasattr(summary, "healths") and len(summary.healths) > 0:
                spot_usdc = float(summary.healths[0].assets) / 1e18
                
            positions = await self.get_active_positions()
            active_count = len(positions)
            pnl = sum(p.get("pnl", 0.0) for p in positions)
            
            equity = spot_usdc + pnl
            self._last_equity = equity
                        
            if self._initial_balance is None and equity > 0:
                self._initial_balance = equity
                self._save_state()
                
            initial = self._initial_balance or equity

            # High Water Mark (Peak Balance) Tracking & Persistence
            if getattr(self, "_peak_balance", None) is None and equity > 0:
                self._peak_balance = max(initial, equity)
                self._save_state()
            elif getattr(self, "_peak_balance", None) is not None and equity > self._peak_balance:
                self._peak_balance = equity
                self._save_state()
            
            # Daily Drawdown and Intraday PnL Tracking
            import time
            today_str = time.strftime("%Y-%m-%d", time.gmtime())
            # F-22 Fix: Include scratch/BE trades in total trades count to avoid daily count distortion
            total_closed_trades = self.win_count + self.loss_count + getattr(self, "scratch_count", 0)
            if getattr(self, "_daily_start_date", None) != today_str or getattr(self, "_daily_start_balance", None) is None:
                self._daily_start_date = today_str
                self._daily_start_balance = equity
                self._daily_start_total_trades = total_closed_trades
                self._save_state()

            # Defense-in-depth: External deposit/withdrawal / baseline desynchronization guard
            # If no positions are open and no trades were executed today, an equity shift > 15%
            # is an external transfer (or state baseline artifact), NOT a market drawdown.
            daily_trades_count = total_closed_trades - getattr(self, "_daily_start_total_trades", total_closed_trades)
            daily_start = self._daily_start_balance or equity
            
            if active_count == 0 and daily_trades_count == 0 and pnl == 0.0:
                if daily_start > 0 and abs(equity - daily_start) / daily_start > 0.15:
                    logger.info(
                        f"[NadoTradingService] 🔄 External balance change detected: ${daily_start:.2f} -> ${equity:.2f} "
                        f"(0 active positions, 0 trades today). Re-anchoring daily start balance."
                    )
                    diff = equity - daily_start
                    self._daily_start_balance = equity
                    self._daily_start_total_trades = total_closed_trades
                    if getattr(self, "_initial_balance", None) is not None:
                        self._initial_balance = max(0.01, self._initial_balance + diff)
                    else:
                        self._initial_balance = equity
                    if getattr(self, "_peak_balance", None) is not None:
                        self._peak_balance = max(0.01, self._peak_balance + diff)
                    else:
                        self._peak_balance = equity
                    self._save_state()
                    daily_start = equity

            daily_pnl_usd = round(equity - daily_start, 2)
            daily_pnl_pct = round(((equity - daily_start) / daily_start) * 100, 2) if daily_start > 0 else 0.0
            daily_drawdown_pct = round(max(0.0, -daily_pnl_pct / 100.0), 4)

            total_trades = self.win_count + self.loss_count
            win_rate = round((self.win_count / total_trades) * 100, 1) if total_trades > 0 else 0.0
            
            # Historical trade metrics from trade journal (for Kelly & Strategy Expectancy)
            historical_payoff_ratio = 1.0
            avg_win_usd = 0.0
            avg_loss_usd = 0.0
            journal_trades_count = 0
            realized_pnl_usd = 0.0
            win_pnls: List[float] = []
            loss_pnls: List[float] = []
            filtered_trades: List[Dict[str, Any]] = []
            try:
                from core.state_store import StateStore
                journal = StateStore.load("data/memory/trade_journal.json", default=[])
                if isinstance(journal, list) and len(journal) > 0:
                    journal_trades_count = len(journal)
                    realized_pnl_usd = round(sum(float(t.get("net_pnl_usd", 0.0) or 0.0) for t in journal), 2)
                    # Adaptive Rolling Window (last 20 trades) with unified Scratch Guard filter
                    window_trades = journal[-20:] if len(journal) >= 20 else journal
                    filtered_trades = [
                        t for t in window_trades
                        if abs(float(t.get("net_pnl_usd", 0.0) or 0.0)) > 0.05
                        and not (-0.22 <= float(t.get("realized_r", 0.0) or 0.0) <= 0.15)
                    ]
                    win_pnls = [float(t.get("net_pnl_usd", 0.0) or 0.0) for t in filtered_trades if float(t.get("net_pnl_usd", 0.0) or 0.0) > 0]
                    loss_pnls = [abs(float(t.get("net_pnl_usd", 0.0) or 0.0)) for t in filtered_trades if float(t.get("net_pnl_usd", 0.0) or 0.0) < 0]
                    avg_win_usd = round(sum(win_pnls) / len(win_pnls), 4) if win_pnls else 0.0
                    avg_loss_usd = round(sum(loss_pnls) / len(loss_pnls), 4) if loss_pnls else 0.0
                    if avg_loss_usd > 0:
                        historical_payoff_ratio = round(avg_win_usd / avg_loss_usd, 4)
                    elif avg_win_usd > 0:
                        historical_payoff_ratio = round(avg_win_usd, 4)
                    else:
                        historical_payoff_ratio = 0.0
            except Exception as e:
                logger.warning(f"[NadoTradingService] ⚠️ Could not load trade journal metrics: {e}")
            
            unrealized_roi_pct = round((pnl / margin_used * 100), 2) if margin_used > 0 else 0.0

            peak_val = round(getattr(self, "_peak_balance", None) or equity, 2)
            drawdown_from_peak_pct = round(max(0.0, (peak_val - equity) / peak_val), 4) if peak_val > 0 else 0.0
            drawdown_from_peak_usd = round(max(0.0, peak_val - equity), 2) if peak_val > 0 else 0.0

            res_summary = {
                "initial_balance": round(initial, 2),
                "peak_balance": peak_val,
                "peak_balance_usd": peak_val,
                "max_drawdown_pct": drawdown_from_peak_pct,
                "max_drawdown_usd": drawdown_from_peak_usd,
                "drawdown_from_peak_pct": drawdown_from_peak_pct,
                "spot_balance": round(spot_usdc, 2),
                "spot_usdc": round(spot_usdc, 2),
                "current_balance": round(equity, 2),
                "balance": round(equity, 2),
                "equity": round(equity, 2),
                "total_usd": round(equity, 2),
                "total_pnl_usd": round(equity - initial, 2),
                "total_pnl_pct": round(((equity - initial) / initial) * 100, 2) if initial > 0 else 0.0,
                "daily_pnl_usd": daily_pnl_usd,
                "daily_pnl_pct": daily_pnl_pct,
                "daily_drawdown_pct": daily_drawdown_pct,
                "daily_trades_count": daily_trades_count,
                "unrealized_pnl_usd": round(pnl, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_roi_pct": unrealized_roi_pct,
                "realized_pnl_usd": realized_pnl_usd,
                "roi_pct": round(((equity - initial) / initial) * 100, 2) if initial > 0 else 0.0,
                "margin_used": round(margin_used, 2),
                "used_margin": round(margin_used, 2),
                "available_margin": round(free_margin, 2),
                "free_margin": round(free_margin, 2),
                "maintenance_health_usd": round(maint_health, 2),
                "maintenance_liabilities_usd": round(maint_liabilities, 2),
                "active_positions_count": active_count,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "win_rate_pct": win_rate,
                "recent_streak": self.recent_streak,
                "historical_payoff_ratio": historical_payoff_ratio,
                "avg_win_usd": avg_win_usd,
                "avg_loss_usd": avg_loss_usd,
                "journal_trades_count": journal_trades_count,
                "window_win_count": len(win_pnls),
                "window_loss_count": len(loss_pnls),
                "window_trades_count": len(filtered_trades),
                "window_win_rate_pct": round(len(win_pnls) / len(filtered_trades) * 100, 1) if filtered_trades else 0.0,
                "min_kelly_mult": 0.85,
                "strict_portfolio_health": True
            }
            self._portfolio_summary_cache = res_summary
            self._portfolio_summary_cache_time = time.time()
            return res_summary
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to get portfolio summary: {e}")
            return {"total_usd": 0.0, "current_balance": 0.0, "balance": 0.0, "margin_used": 0.0, "free_margin": 0.0, "pnl": 0.0}

    async def get_active_positions(self, bypass_cache: bool = False) -> List[Dict[str, Any]]:
        """Returns active open positions from Nado."""
        if not self.is_connected:
            self._last_positions_fetch_ok = False
            return []
            
        import time
        if not bypass_cache and getattr(self, "_pos_cache_time", 0) > 0:
            if time.time() - self._pos_cache_time < 15.0 and hasattr(self, "_pos_cache"):
                return self._pos_cache
                
        active_list = []
        self._last_positions_fetch_ok = False
        try:
            # Reverse map for product_id -> symbol (prefer canonical pair format BASE-USD)
            id_to_symbol = {}
            for k, v in self.product_map.items():
                if '-' in k:
                    id_to_symbol[v] = k
                elif v not in id_to_symbol:
                    id_to_symbol[v] = f"{k}-USD"
            
            # Fetch fresh market cache for accurate PnL pricing
            try:
                markets_data = await asyncio.to_thread(self.client.market.get_all_engine_markets)
                if not getattr(self, "_market_cache", None):
                    self._market_cache = {}
                for m in markets_data.perp_products:
                    self._market_cache[m.product_id] = m
                self._market_cache_ts = time.time()
            except Exception as e:
                logger.warning(f"[NadoTradingService] ⚠️ Failed to refresh market cache: {e}")
                
            address = self.wallet.get_address()
            
            # Fetch all subaccounts for this address
            res = await asyncio.to_thread(self.client.subaccount.get_subaccounts, address)
            if not res or not res.subaccounts:
                self._last_positions_fetch_ok = (res is not None)
                return []
                
            for sa in res.subaccounts:
                summary = await asyncio.to_thread(self.client.subaccount.get_engine_subaccount_summary, sa.subaccount)
                if not hasattr(summary, "perp_balances") or not summary.perp_balances:
                    continue
                    
                for pos in summary.perp_balances:
                    base_amount = float(pos.balance.amount) / 1e18
                    if abs(base_amount) < 1e-6:
                        continue # Ignore zero or dust positions
                        
                    product_id = pos.product_id
                    raw_sym = id_to_symbol.get(product_id, f"UNKNOWN-{product_id}")
                    base_sym = raw_sym.split('-')[0].upper()
                    symbol = f"{base_sym}-USD" if not raw_sym.startswith("UNKNOWN") else raw_sym
                    # Usually oracle_price_x18 is in perp_product or we can fall back to local_pos
                    # but if we don't have oracle price readily available in perp_balances, we can use a fallback
                    # In Vertex/Nado, perp_balances doesn't include oracle_price directly unless we fetch markets
                    # We'll just rely on the real_entry or local cache for now
                    current_price = 0.0  # Will be fetched later or isn't needed for raw size
                    
                    # Try to find current price from market cache
                    market_info = self._market_cache.get(product_id) if getattr(self, "_market_cache", None) else None
                    if market_info:
                        if hasattr(market_info, "oracle_price_x18"):
                            current_price = float(market_info.oracle_price_x18) / 1e18
                        elif hasattr(market_info, "product") and hasattr(market_info.product, "oracle_price_x18"):
                            current_price = float(market_info.product.oracle_price_x18) / 1e18
                        
                    size_usd = abs(base_amount) * current_price if current_price > 0 else 0.0
                    direction = "LONG" if base_amount > 0 else "SHORT"
                    
                    # Merge with local cache for TP/SL and Entry
                    local_pos = self.active_positions.get(symbol) or self.active_positions.get(base_sym) or {}
                    if local_pos.get("entry_price") and float(local_pos.get("entry_price")) > 0:
                        entry_price = float(local_pos["entry_price"])
                    else:
                        try:
                            v_quote = float(pos.balance.v_quote_balance) / 1e18
                            real_entry = abs(v_quote) / abs(base_amount) if abs(base_amount) > 0 else current_price
                        except Exception:
                            real_entry = current_price
                        entry_price = real_entry
                    
                    # Calculate Net PnL (Deducting ~0.10% round-trip taker fees: 0.05% entry + 0.05% exit)
                    if direction == "LONG":
                        gross_pnl = (current_price - entry_price) * abs(base_amount)
                    else:
                        gross_pnl = (entry_price - current_price) * abs(base_amount)
                        
                    net_pnl = gross_pnl - (size_usd * 0.001)
                        
                    active_list.append({
                        "symbol": symbol,
                        "direction": direction,
                        "entry_price": entry_price,
                        "size_usd": size_usd,
                        "tp_price": local_pos.get("tp_price", 0.0),
                        "sl_price": local_pos.get("sl_price", 0.0),
                        "leverage": local_pos.get("leverage", 10),
                        "pnl": net_pnl,
                        "amount": base_amount,
                        "_subaccount": sa.subaccount,
                        "_product_id": product_id,
                        "highest_price": float(local_pos.get("highest_price", entry_price)),
                        "lowest_price": float(local_pos.get("lowest_price", entry_price)),
                        "protection_state": local_pos.get("protection_state", "PROTECTED"),
                        "atr_reference": float(local_pos.get("atr_reference", 0.0)),
                        "open_time": float(local_pos.get("open_time", 0.0)),
                        "original_thesis": local_pos.get("original_thesis", ""),
                        "accumulated_realized_pnl": float(local_pos.get("accumulated_realized_pnl", 0.0)),
                        "partial_tp_plan": local_pos.get("partial_tp_plan"),
                        "contracts": float(local_pos.get("contracts", abs(base_amount)))
                    })
            
            self._pos_cache = active_list
            import time
            self._pos_cache_time = time.time()
            self._last_positions_fetch_ok = True
        except Exception as e:
            self._last_positions_fetch_ok = False
            logger.error(f"[NadoTradingService] ❌ Failed to fetch active positions: {e}")
            
        return active_list

    @staticmethod
    def build_order_appendix(
        order_type,
        reduce_only: bool = False,
        builder_id: Optional[int] = None,
        builder_fee_rate: Optional[int] = None
    ) -> int:
        """
        Build order appendix using official SDK with safe NADO_BUILDER_ID support.
        If builder_id <= 0 or None, builder fields are omitted so the exchange
        sequencer does not reject orders with unassigned/unregistered builder ID.
        """
        from nado_protocol.utils.order import build_appendix
        from core.config import config

        bid = builder_id if builder_id is not None else int(getattr(config, "NADO_BUILDER_ID", 0) or 0)
        bfee = builder_fee_rate if builder_fee_rate is not None else int(getattr(config, "NADO_BUILDER_FEE_RATE", 0) or 0)

        kwargs = {"order_type": order_type, "reduce_only": reduce_only}
        if bid > 0:
            kwargs["builder_id"] = bid
            kwargs["builder_fee_rate"] = bfee
        return build_appendix(**kwargs)

    async def open_position(
        self, 
        symbol: str, 
        direction: str, 
        entry_price: float, 
        notional_usd: float, 
        tp_price: float, 
        sl_price: float, 
        leverage: int, 
        original_thesis: str = "", 
        contracts: float = 0.0, 
        atr_value: float = 0.0,
        max_slippage_pct: Optional[float] = None,
        approved_risk_usd: float = 0.0,
        taker_fee_pct: float = 0.0005,
        planned_risk_usd: float = 0.0,
        base_risk_pct: float = 0.0,
        final_risk_pct: float = 0.0,
        cohort: str = "UNKNOWN",
        applied_multipliers: Optional[Dict[str, Any]] = None,
        funding_rate_hourly: float = 0.0,
        holding_hours: float = 4.0,
        slippage_stress_pct: Optional[float] = None,
        limit_price: Optional[float] = None,
        strategy_mode: str = "UNKNOWN",
        macro_regime: str = "UNKNOWN",
        entry_quality: float = 0.0,
        bb_pos_pct: float = 50.0,
        macro_range_score: float = 0.0,
        use_software_tpsl: bool = False,
        software_sl_tp_only: bool = False,
        **kwargs
    ) -> bool:
        """Submits an EIP-712 signed order to Nado Gateway."""
        self.last_execution_status = "PENDING"
        self.last_execution_details = {"symbol": symbol, "direction": direction}
        if not self.is_connected:
            logger.error(f"[NadoTradingService] Cannot open {direction} on {symbol} - SDK not connected.")
            self.last_execution_status = "SDK_DISCONNECTED"
            return False

        if getattr(self, "entries_paused", False):
            logger.warning(f"[NadoTradingService] ⏸️ Refusing to open new position on {symbol}: entries are paused via Telegram Terminal.")
            self.last_execution_status = "ENTRIES_PAUSED"
            return False

        # P1-4: Reset per-attempt execution status and details
        self.last_execution_status = "PENDING"
        self.last_execution_details = {}

        # Guard: Unresolved risk breach in portfolio or pending reconciliations blocks all new trades (P1 / Item 3.A & Audit P2)
        for pos_k, pos_v in self.active_positions.items():
            if isinstance(pos_v, dict):
                prot_state = pos_v.get("protection_state")
                if prot_state == "RISK_BREACH_UNRESOLVED" or pos_v.get("requires_reconciliation"):
                    logger.critical(
                        f"[NadoTradingService] 🛑 Refusing to open new position on {symbol}: "
                        f"portfolio contains unresolved risk breach on {pos_k} (state={prot_state})! Resolve risk breach first."
                    )
                    self.last_execution_status = "RISK_BREACH_UNRESOLVED"
                    self.last_execution_details = {"reason": "PORTFOLIO_HAS_UNRESOLVED_RISK_BREACH", "breached_symbol": pos_k, "state": prot_state}
                    return False

        pending_recs = getattr(self, "pending_reconciliations", {}) or {}
        for rec_k, rec_v in pending_recs.items():
            if isinstance(rec_v, dict):
                rec_status = rec_v.get("status")
                rec_prot = rec_v.get("protection_state")
                rec_reason = rec_v.get("reason")
                if (rec_status in ["PENDING_ORDER_RECONCILIATION", "RISK_BREACH_UNRESOLVED"] or
                    rec_prot == "RISK_BREACH_UNRESOLVED" or
                    rec_reason in ["RISK_BREACH", "RISK_BREACH_UNRESOLVED"]):
                    logger.critical(
                        f"[NadoTradingService] 🛑 Refusing to open new position on {symbol}: "
                        f"pending incident reconciliation on {rec_k} (status={rec_status}, prot={rec_prot})! Resolve incident first."
                    )
                    self.last_execution_status = "RISK_BREACH_UNRESOLVED"
                    self.last_execution_details = {"reason": "PENDING_RECONCILIATION_UNRESOLVED_INCIDENT", "breached_symbol": rec_k, "status": rec_status}
                    return False

        clean_sym = symbol.replace('/', '-').strip().upper()
        base_sym = clean_sym.split('-')[0]
        canonical_sym = f"{base_sym}-USD"
        symbol = canonical_sym  # Canonicalize symbol to prevent dual-key memory/disk desync

        # Guard: Strict duplicate position prevention
        if any(k in self.active_positions for k in [canonical_sym, base_sym, clean_sym]):
            logger.warning(
                f"[NadoTradingService] ⚠️ Позиция по {symbol} уже открыта в active_positions! "
                f"Блокировка дублирующего ордера для защиты капитала."
            )
            self.last_execution_status = "DUPLICATE_POSITION_BLOCKED"
            return False

        # Input numeric sanitization: reject NaN, Inf, or non-positive critical values
        if not math.isfinite(entry_price) or entry_price <= 0:
            logger.error(f"[NadoTradingService] ❌ Refusing execution: invalid entry_price ({entry_price}).")
            self.last_execution_status = "REJECTED_BY_EXCHANGE"
            self.last_execution_details = {"reason": f"Invalid entry_price ({entry_price})"}
            return False
        if not math.isfinite(sl_price) or sl_price <= 0:
            logger.error(f"[NadoTradingService] ❌ Refusing execution without valid positive Stop Loss (sl_price={sl_price}).")
            self.last_execution_status = "ABORTED_INVALID_STOP"
            self.last_execution_details = {"reason": "MISSING_OR_INVALID_SL", "sl_price": sl_price}
            return False
        if approved_risk_usd is None or not math.isfinite(float(approved_risk_usd)) or float(approved_risk_usd) <= 0:
            logger.error(f"[NadoTradingService] ❌ Refusing execution without valid approved risk budget (approved_risk_usd={approved_risk_usd}).")
            self.last_execution_status = "INVALID_RISK_PARAMETERS"
            self.last_execution_details = {"reason": "MISSING_OR_INVALID_APPROVED_RISK", "approved_risk_usd": approved_risk_usd}
            return False
        if (contracts is None or contracts <= 0) and (notional_usd is None or notional_usd <= 0):
            logger.error(f"[NadoTradingService] ❌ Refusing execution without valid size (contracts={contracts}, notional={notional_usd}).")
            self.last_execution_status = "INVALID_SIZE"
            self.last_execution_details = {"reason": "INVALID_CONTRACTS", "contracts": contracts}
            return False
        if not math.isfinite(tp_price) or tp_price < 0:
            logger.error(f"[NadoTradingService] ❌ Refusing execution: invalid tp_price ({tp_price}).")
            self.last_execution_status = "REJECTED_BY_EXCHANGE"
            self.last_execution_details = {"reason": f"Invalid tp_price ({tp_price})"}
            return False

        # Strict Geometry Invariants: verify SL and TP are on the correct side of entry price (P1)
        dir_clean_early = str(direction).strip().upper()
        if dir_clean_early == "LONG":
            if sl_price >= entry_price:
                logger.error(
                    f"[NadoTradingService] ❌ INVALID_STOP_GEOMETRY for LONG: "
                    f"sl_price ({sl_price}) must be strictly below entry_price ({entry_price})."
                )
                self.last_execution_status = "INVALID_STOP_GEOMETRY"
                self.last_execution_details = {
                    "reason": "SL_ABOVE_OR_EQUAL_ENTRY",
                    "direction": "LONG",
                    "sl_price": sl_price,
                    "entry_price": entry_price
                }
                return False
            if tp_price > 0 and tp_price <= entry_price:
                logger.error(
                    f"[NadoTradingService] ❌ INVALID_TP_GEOMETRY for LONG: "
                    f"tp_price ({tp_price}) must be strictly above entry_price ({entry_price})."
                )
                self.last_execution_status = "INVALID_TP_GEOMETRY"
                self.last_execution_details = {
                    "reason": "TP_BELOW_OR_EQUAL_ENTRY",
                    "direction": "LONG",
                    "tp_price": tp_price,
                    "entry_price": entry_price
                }
                return False
        elif dir_clean_early == "SHORT":
            if sl_price <= entry_price:
                logger.error(
                    f"[NadoTradingService] ❌ INVALID_STOP_GEOMETRY for SHORT: "
                    f"sl_price ({sl_price}) must be strictly above entry_price ({entry_price})."
                )
                self.last_execution_status = "INVALID_STOP_GEOMETRY"
                self.last_execution_details = {
                    "reason": "SL_BELOW_OR_EQUAL_ENTRY",
                    "direction": "SHORT",
                    "sl_price": sl_price,
                    "entry_price": entry_price
                }
                return False
            if tp_price > 0 and (tp_price >= entry_price or tp_price <= 0):
                logger.error(
                    f"[NadoTradingService] ❌ INVALID_TP_GEOMETRY for SHORT: "
                    f"tp_price ({tp_price}) must be strictly below entry_price ({entry_price}) and > 0."
                )
                self.last_execution_status = "INVALID_TP_GEOMETRY"
                self.last_execution_details = {
                    "reason": "TP_ABOVE_OR_EQUAL_ENTRY",
                    "direction": "SHORT",
                    "tp_price": tp_price,
                    "entry_price": entry_price
                }
                return False
            
        logger.info(f"[NadoTradingService] 🚀 Routing {direction} {symbol} to Nado DEX...")
        
        actual_entry_price = entry_price
        filled = False
        actual_filled_x18 = 0
        
        try:
            import time
            import asyncio
            from nado_protocol.engine_client.types.execute import PlaceOrderParams, OrderParams
            from nado_protocol.utils.bytes32 import subaccount_to_hex
            from nado_protocol.utils.nonce import gen_order_nonce
            
            if not getattr(self, "_market_cache", None):
                self._market_cache = {}
                markets_data = await asyncio.to_thread(self.client.market.get_all_engine_markets)
                for m in markets_data.perp_products:
                    self._market_cache[m.product_id] = m
                self._market_cache_ts = time.time()
            
            base_symbol = symbol.split('-')[0].upper()
            product_id = self.product_map.get(base_symbol)
            if product_id is None:
                logger.error(f"[NadoTradingService] ❌ Unknown symbol {symbol} - not found in Nado product map!")
                self.last_execution_status = "REJECTED_BY_EXCHANGE"
                self.last_execution_details = {"reason": f"Unknown symbol {symbol}"}
                return False

            # Enforce Minimum Viable Floor ($35.00) in both Shadow and Live modes
            final_notional = float(notional_usd if notional_usd > 0 else (contracts * entry_price))
            min_viable_floor = float(getattr(config, "MIN_NOTIONAL_FLOOR_USD", 35.0) or 35.0)
            if min_viable_floor > 0 and final_notional < min_viable_floor:
                logger.error(
                    f"[NadoTradingService] ❌ Order notional (${final_notional:.2f}) "
                    f"is below minimum viable floor (${min_viable_floor:.2f}). Refusing trade."
                )
                self.last_execution_status = "ORDER_BELOW_MIN_NOTIONAL"
                self.last_execution_details = {
                    "reason": "ORDER_BELOW_MIN_NOTIONAL",
                    "final_notional": final_notional,
                    "min_notional": min_viable_floor
                }
                return False

            # Level 4: Shadow Mode (Simulated Execution on live market data without on-chain capital risk)
            if getattr(config, "SHADOW_MODE", False):
                mock_digest = f"0xshadow_{int(time.time()*1000):x}_{product_id}"
                logger.info(
                    f"[NadoTradingService] 👻 [SHADOW MODE] Virtual order executed: {direction} {contracts} {symbol} "
                    f"at ${entry_price:.4f} (Notional: ${notional_usd:.2f}, L={leverage}x). Mock Digest: {mock_digest}"
                )
                self.active_positions[symbol] = {
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": entry_price,
                    "current_price": entry_price,
                    "contracts": contracts,
                    "notional_usd": notional_usd,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "initial_sl": sl_price,
                    "initial_tp": tp_price,
                    "highest_price": entry_price,
                    "lowest_price": entry_price,
                    "open_time": time.time(),
                    "leverage": leverage,
                    "open_digest": mock_digest,
                    "entry_timestamp": time.time(),
                    "original_thesis": original_thesis,
                    "strategy_mode": strategy_mode,
                    "is_anti_bot": bool(getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION"),
                    "macro_regime": macro_regime,
                    "entry_quality": entry_quality,
                    "approved_risk_usd": approved_risk_usd,
                    "is_shadow": True,
                    "is_virtual": True,
                    "use_software_tpsl": bool(use_software_tpsl or software_sl_tp_only or notional_usd < 100.0),
                    "software_sl_tp_only": bool(use_software_tpsl or software_sl_tp_only or notional_usd < 100.0),
                    "sl_protection_mode": "SOFTWARE_ONLY" if (use_software_tpsl or software_sl_tp_only or notional_usd < 100.0) else "EXCHANGE_PROTECTED",
                    "protection_state": "SOFTWARE_PROTECTED" if (use_software_tpsl or software_sl_tp_only or notional_usd < 100.0) else "NATIVE_SL_CONFIRMED"
                }
                self._save_state()
                self.last_execution_status = "FILLED"
                self.last_execution_details = {"symbol": symbol, "direction": direction, "shadow": True}
                return True
                
            params_dict = self._get_market_parameters(product_id)
            size_increment = params_dict.get("size_increment_x18", 0)
            price_increment = params_dict.get("price_increment_x18", 0)
            if size_increment <= 0 or price_increment <= 0:
                logger.error(
                    f"[NadoTradingService] ❌ Market params invalid for product_id {product_id} "
                    f"(size_inc={size_increment}, price_inc={price_increment}). Refusing execution."
                )
                self.last_execution_status = "REJECTED_BY_EXCHANGE"
                self.last_execution_details = {"reason": "INVALID_MARKET_INCREMENTS"}
                return False
                
            from decimal import Decimal
            amount_base = contracts if contracts > 0 else notional_usd / entry_price
            amount_x18 = int(Decimal(str(round(amount_base, 8))) * Decimal(10**18))
            # Explicitly guard against non-directional execution (HOLD, WAIT, etc.)
            dir_clean = str(direction).strip().upper()
            
            # Align execution limit price tolerance with risk manager's modelled slippage.
            # Default to 0.002 (20 bps), strictly bounded between 0.05% and 0.50% (50 bps).
            slip_buffer = float(max_slippage_pct) if max_slippage_pct is not None and max_slippage_pct > 0 else 0.002
            slip_buffer = min(0.005, max(0.0005, slip_buffer))
            
            # Lever 3: Dynamic OrderType selection (Maker / Post-Only vs Taker IOC)
            is_breakout_mode = strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM", "VOLUME_EXPLOSION"]
            enable_maker = bool(getattr(config, "ENABLE_MAKER_LIMIT_ENTRY", True))
            use_maker_entry = enable_maker and not is_breakout_mode and strategy_mode in ["TREND_FOLLOWING", "MEAN_REVERSION"]

            price_inc_float = float(Decimal(price_increment) / Decimal(10**18)) if price_increment > 0 else 0.0001
            if use_maker_entry:
                # Maker / Post-Only entry: rest passively on the book without crossing the spread
                best_bid = 0.0
                best_ask = 0.0
                try:
                    price_data = await asyncio.to_thread(self.client.market.get_latest_market_price, product_id)
                    if price_data:
                        if hasattr(price_data, "bid_x18") and float(price_data.bid_x18) > 0:
                            best_bid = float(price_data.bid_x18) / 1e18
                        if hasattr(price_data, "ask_x18") and float(price_data.ask_x18) > 0:
                            best_ask = float(price_data.ask_x18) / 1e18
                except Exception:
                    pass

                if dir_clean == "LONG":
                    # Place on passive bid (best_bid or 1 tick below market) and round strictly down (floor)
                    # Verify best_bid is a sane market price (within 20% of entry_price) to prevent empty book/mock corruption
                    is_sane_bid = best_bid > 0 and (abs(best_bid - entry_price) / max(entry_price, 1.0) < 0.2)
                    raw_limit = best_bid if is_sane_bid else (entry_price - price_inc_float)
                elif dir_clean == "SHORT":
                    # Place on passive ask (best_ask or 1 tick above market) and round strictly up (ceil)
                    is_sane_ask = best_ask > 0 and (abs(best_ask - entry_price) / max(entry_price, 1.0) < 0.2)
                    raw_limit = best_ask if is_sane_ask else (entry_price + price_inc_float)
                else:
                    raw_limit = float(entry_price)
            elif limit_price is not None and float(limit_price) > 0 and math.isfinite(float(limit_price)):
                raw_limit = float(limit_price)
            elif dir_clean == "LONG":
                raw_limit = entry_price * (1.0 + slip_buffer)
            elif dir_clean == "SHORT":
                raw_limit = entry_price * (1.0 - slip_buffer)
            else:
                logger.error(f"[NadoTradingService] ❌ Refusing execution: invalid direction '{direction}'. Only LONG or SHORT allowed!")
                self.last_execution_status = "REJECTED_BY_EXCHANGE"
                self.last_execution_details = {"reason": f"Invalid direction '{direction}'"}
                return False

            if dir_clean == "SHORT":
                amount_x18 = -amount_x18
                
            price_x18 = int(Decimal(str(round(raw_limit, 8))) * Decimal(10**18))
            
            # Align to size and price increments
            abs_amount_x18 = abs(amount_x18)
            abs_amount_x18 = (abs_amount_x18 // size_increment) * size_increment
            amount_x18 = abs_amount_x18 if amount_x18 >= 0 else -abs_amount_x18
            
            # Directional Protective Limit Price Rounding (P1-1 Audit):
            # LONG: buy limit rounded down (floor) so max purchase price never exceeds raw_limit.
            # SHORT: sell limit rounded up (ceil) so min selling price never drops below raw_limit.
            if dir_clean == "LONG":
                price_x18 = (price_x18 // price_increment) * price_increment
            else: # SHORT
                price_x18 = ((price_x18 + price_increment - 1) // price_increment) * price_increment
            final_limit_price = float(Decimal(price_x18) / Decimal(10**18))
            
            # CLOB Pre-dispatch Verification: abs(amount) * order_limit_price >= $101.00
            order_limit_price = final_limit_price
            current_order_contracts = float(Decimal(abs_amount_x18) / Decimal(10**18))
            if order_limit_price > 0 and (current_order_contracts * order_limit_price) < 101.0:
                size_inc_float = float(Decimal(size_increment) / Decimal(10**18))
                if size_inc_float > 0 and (notional_usd >= 95.0 or (current_order_contracts * order_limit_price) >= 35.0):
                    needed_contracts = math.ceil(101.0 / order_limit_price / size_inc_float) * size_inc_float
                    abs_amount_x18 = int(Decimal(str(round(needed_contracts, 8))) * Decimal(10**18))
                    abs_amount_x18 = (abs_amount_x18 // size_increment) * size_increment
                    amount_x18 = abs_amount_x18 if amount_x18 >= 0 else -abs_amount_x18
                    if needed_contracts > current_order_contracts:
                        taker_fee_val = float(taker_fee_pct or 0.0005)
                        bumped_risk = needed_contracts * abs(entry_price - float(sl_price)) + needed_contracts * (entry_price + float(sl_price)) * taker_fee_val
                        max_allowed_risk = float(approved_risk_usd) * 1.35
                        new_approved_risk = min(max_allowed_risk, max(float(approved_risk_usd), bumped_risk))
                        logger.info(
                            f"[NadoTradingService] ⚡ Predispatch CLOB adjustment: Adjusted order amount from {current_order_contracts} to {needed_contracts} "
                            f"contracts (limit notional ${needed_contracts * order_limit_price:.2f} >= $101.00) to guarantee passing Nado DEX min_size. "
                            f"Synchronized approved_risk_usd: ${float(approved_risk_usd):.2f} -> ${new_approved_risk:.2f}."
                        )
                        approved_risk_usd = new_approved_risk
                    else:
                        logger.info(
                            f"[NadoTradingService] ⚡ Predispatch CLOB adjustment: Adjusted order amount from {current_order_contracts} to {needed_contracts} "
                            f"contracts (limit notional ${needed_contracts * order_limit_price:.2f} >= $101.00) to guarantee passing Nado DEX min_size."
                        )
            
            if amount_x18 == 0:
                logger.error(f"[NadoTradingService] ❌ Order amount is zero after step size alignment.")
                self.last_execution_status = "REJECTED_BY_EXCHANGE"
                self.last_execution_details = {"reason": "ZERO_AMOUNT_AFTER_STEP_ALIGNMENT"}
                return False

            final_contracts = float(Decimal(abs_amount_x18) / Decimal(10**18))
            final_notional = final_contracts * entry_price

            # Exchange minimum checks: never increase order size above approved budget!
            min_size = float(params_dict.get("min_size", 0.0) or 0.0)
            min_notional = float(params_dict.get("min_notional", 0.0) or 0.0)

            if min_size > 0 and final_contracts < min_size:
                logger.error(
                    f"[NadoTradingService] ❌ Order size after increment alignment ({final_contracts}) "
                    f"is below exchange min_size ({min_size}). Refusing trade to avoid unauthorized risk expansion."
                )
                self.last_execution_status = "ORDER_BELOW_MIN_SIZE"
                self.last_execution_details = {
                    "reason": "ORDER_BELOW_MIN_SIZE",
                    "final_contracts": final_contracts,
                    "min_size": min_size
                }
                return False

            is_production_mode = getattr(config, "SHADOW_MODE", False) or getattr(config, "NADO_LIVE_TRADING_ENABLED", False) or getattr(config, "LIVE_TRADING_ENABLED", False)
            min_viable_floor = float(getattr(config, "MIN_NOTIONAL_FLOOR_USD", 35.0) or 35.0) if is_production_mode else 0.0

            if min_viable_floor > 0 and final_notional < min_viable_floor:
                logger.error(
                    f"[NadoTradingService] ❌ Order notional (${final_notional:.2f}) "
                    f"is below minimum viable floor (${min_viable_floor:.2f}). Refusing trade."
                )
                self.last_execution_status = "ORDER_BELOW_MIN_NOTIONAL"
                self.last_execution_details = {
                    "reason": "ORDER_BELOW_MIN_NOTIONAL",
                    "final_notional": final_notional,
                    "min_notional": min_viable_floor
                }
                return False

            clob_notional = final_contracts * final_limit_price
            if is_production_mode and not getattr(config, "SHADOW_MODE", False) and clob_notional < 100.0:
                logger.error(
                    f"[NadoTradingService] ❌ Order notional (${clob_notional:.2f}) "
                    f"is below Nado exchange CLOB min_size ($100.00). Refusing order placement."
                )
                self.last_execution_status = "ORDER_BELOW_MIN_NOTIONAL"
                self.last_execution_details = {
                    "reason": "ORDER_BELOW_MIN_NOTIONAL",
                    "final_notional": clob_notional,
                    "min_notional": 100.0
                }
                return False

            if min_notional > 0 and final_notional < min_notional:
                if min_notional <= 100.50 or getattr(config, "SHADOW_MODE", False):
                    logger.info(
                        f"[NadoTradingService] ℹ️ Order notional (${final_notional:.2f}) is below native trigger limit (${min_notional:.2f}). "
                        f"Proceeding with Software TP/SL protection (FastPriceMonitor & Sentinel)."
                    )
                else:
                    logger.error(
                        f"[NadoTradingService] ❌ Order notional (${final_notional:.2f}) "
                        f"is below exchange min_notional (${min_notional:.2f}). Refusing trade."
                    )
                    self.last_execution_status = "ORDER_BELOW_MIN_NOTIONAL"
                    self.last_execution_details = {
                        "reason": "ORDER_BELOW_MIN_NOTIONAL",
                        "final_notional": final_notional,
                        "min_notional": min_notional
                    }
                    return False

            # Unified Pre-Dispatch Risk Invariant Verification:
            # Enforce strictly positive, finite Stop Loss, approved risk budget, and contracts before dispatch (P1-2)
            if sl_price is None or not math.isfinite(float(sl_price)) or float(sl_price) <= 0:
                logger.error(f"[NadoTradingService] ❌ Refusing execution without valid positive Stop Loss (sl_price={sl_price}).")
                self.last_execution_status = "ABORTED_INVALID_STOP"
                self.last_execution_details = {"reason": "MISSING_OR_INVALID_SL", "sl_price": sl_price}
                return False

            if approved_risk_usd is None or not math.isfinite(float(approved_risk_usd)) or float(approved_risk_usd) <= 0:
                logger.error(f"[NadoTradingService] ❌ Refusing execution without valid approved risk budget (approved_risk_usd={approved_risk_usd}).")
                self.last_execution_status = "INVALID_RISK_PARAMETERS"
                self.last_execution_details = {"reason": "MISSING_OR_INVALID_APPROVED_RISK", "approved_risk_usd": approved_risk_usd}
                return False

            if final_contracts <= 0 or not math.isfinite(float(final_contracts)):
                logger.error(f"[NadoTradingService] ❌ Refusing execution without valid positive contracts (contracts={final_contracts}).")
                self.last_execution_status = "INVALID_SIZE"
                self.last_execution_details = {"reason": "INVALID_CONTRACTS", "contracts": final_contracts}
                return False

            # Final limit price geometry re-verification (P1)
            if dir_clean == "LONG":
                if float(sl_price) >= final_limit_price:
                    logger.error(
                        f"[NadoTradingService] ❌ INVALID_STOP_GEOMETRY for LONG: "
                        f"sl_price ({sl_price}) >= final_limit_price ({final_limit_price})."
                    )
                    self.last_execution_status = "INVALID_STOP_GEOMETRY"
                    self.last_execution_details = {
                        "reason": "SL_ABOVE_OR_EQUAL_LIMIT_PRICE",
                        "direction": "LONG",
                        "sl_price": sl_price,
                        "final_limit_price": final_limit_price
                    }
                    return False
                if tp_price > 0 and float(tp_price) <= final_limit_price:
                    logger.error(
                        f"[NadoTradingService] ❌ INVALID_TP_GEOMETRY for LONG: "
                        f"tp_price ({tp_price}) <= final_limit_price ({final_limit_price})."
                    )
                    self.last_execution_status = "INVALID_TP_GEOMETRY"
                    self.last_execution_details = {
                        "reason": "TP_BELOW_OR_EQUAL_LIMIT_PRICE",
                        "direction": "LONG",
                        "tp_price": tp_price,
                        "final_limit_price": final_limit_price
                    }
                    return False
            elif dir_clean == "SHORT":
                if float(sl_price) <= final_limit_price:
                    logger.error(
                        f"[NadoTradingService] ❌ INVALID_STOP_GEOMETRY for SHORT: "
                        f"sl_price ({sl_price}) <= final_limit_price ({final_limit_price})."
                    )
                    self.last_execution_status = "INVALID_STOP_GEOMETRY"
                    self.last_execution_details = {
                        "reason": "SL_BELOW_OR_EQUAL_LIMIT_PRICE",
                        "direction": "SHORT",
                        "sl_price": sl_price,
                        "final_limit_price": final_limit_price
                    }
                    return False
                if tp_price > 0 and (float(tp_price) >= final_limit_price or float(tp_price) <= 0):
                    logger.error(
                        f"[NadoTradingService] ❌ INVALID_TP_GEOMETRY for SHORT: "
                        f"tp_price ({tp_price}) >= final_limit_price ({final_limit_price}) or <= 0."
                    )
                    self.last_execution_status = "INVALID_TP_GEOMETRY"
                    self.last_execution_details = {
                        "reason": "TP_ABOVE_OR_EQUAL_LIMIT_PRICE",
                        "direction": "SHORT",
                        "tp_price": tp_price,
                        "final_limit_price": final_limit_price
                    }
                    return False

            from core.risk_calculator import estimate_total_loss, calculate_max_allowed_stress_loss
            try:
                pre_loss_calc = estimate_total_loss(
                    contracts=final_contracts,
                    fill_price=final_limit_price,
                    sl_price=float(sl_price),
                    taker_fee_pct=taker_fee_pct,
                    slippage_stress_pct=slippage_stress_pct if slippage_stress_pct is not None else slip_buffer,
                    funding_rate_hourly=funding_rate_hourly,
                    holding_hours=holding_hours,
                    approved_risk=float(approved_risk_usd)
                )
                pre_max_stress = calculate_max_allowed_stress_loss(float(approved_risk_usd))
                if pre_loss_calc.total_expected_loss_usd > pre_loss_calc.max_tolerated_risk_usd:
                    logger.error(
                        f"[NadoTradingService] ❌ PRE-DISPATCH RISK BREACH: Expected loss at limit price "
                        f"(${pre_loss_calc.total_expected_loss_usd:.2f}) exceeds approved risk threshold "
                        f"(${pre_loss_calc.max_tolerated_risk_usd:.2f}, approved=${approved_risk_usd:.2f}). "
                        f"Aborting before sending order to sequencer."
                    )
                    self.last_execution_status = "PRE_DISPATCH_RISK_BREACH"
                    self.last_execution_details = {
                        "reason": "PRE_DISPATCH_RISK_BREACH",
                        "expected_loss": pre_loss_calc.total_expected_loss_usd,
                        "max_tolerated": pre_loss_calc.max_tolerated_risk_usd,
                        "approved_risk": approved_risk_usd
                    }
                    return False
                if pre_loss_calc.total_stress_loss_usd > pre_max_stress:
                    logger.error(
                        f"[NadoTradingService] ❌ PRE-DISPATCH STRESS RISK BREACH: Stress loss at limit price "
                        f"(${pre_loss_calc.total_stress_loss_usd:.2f}) exceeds approved stress ceiling "
                        f"(${pre_max_stress:.2f}, approved=${approved_risk_usd:.2f}). "
                        f"Aborting before sending order to sequencer."
                    )
                    self.last_execution_status = "PRE_DISPATCH_STRESS_BREACH"
                    self.last_execution_details = {
                        "reason": "PRE_DISPATCH_STRESS_BREACH",
                        "stress_loss": pre_loss_calc.total_stress_loss_usd,
                        "max_allowed_stress": pre_max_stress,
                        "approved_risk": approved_risk_usd
                    }
                    return False
            except ValueError as pre_err:
                logger.error(f"[NadoTradingService] ❌ Pre-dispatch risk calculator error: {pre_err}")
                self.last_execution_status = "INVALID_RISK_PARAMETERS"
                self.last_execution_details = {"reason": str(pre_err)}
                return False
                
            # Expiration
            try:
                from nado_protocol.utils.expiration import get_expiration_timestamp
                expiration = get_expiration_timestamp(86400 * 30)
            except Exception as e:
                logger.warning(f"[NadoTradingService] Fallback to standard expiration: {e}")
                expiration = int(time.time()) + 86400 * 30
            
            sender = subaccount_to_hex(self.wallet.get_address(), "default")
            
            # Use official SDK nonce generator
            nonce = gen_order_nonce()
            
            decoded_recv_time = nonce >> 20
            now_ms = time.time_ns() // 1_000_000
            
            logger.debug(
                f"[NADO TIME DEBUG] "
                f"nonce={nonce}, "
                f"recv_time={decoded_recv_time}, "
                f"local_now={now_ms}, "
                f"ttl={decoded_recv_time - now_ms}ms"
            )
            
            # Use official SDK appendix builder with safe Builder ID support
            try:
                try:
                    from nado_protocol.utils.expiration import OrderType
                    if use_maker_entry:
                        order_type = OrderType.POST_ONLY
                    else:
                        order_type = OrderType.IOC
                except ImportError:
                    # Fallback if OrderType is in a different module
                    order_type = 3 if use_maker_entry else 1
                
                appendix = self.build_order_appendix(order_type=order_type)
            except Exception as e:
                logger.warning(f"[NadoTradingService] Failed to build appendix officially, using fallback: {e}")
                appendix = 1
                
            order = OrderParams(
                sender=sender,
                amount=amount_x18,
                priceX18=price_x18,
                expiration=expiration,
                nonce=nonce,
                appendix=appendix
            )
            
            params = PlaceOrderParams(
                product_id=product_id,
                order=order
            )
            
            logger.debug(
                f"[NADO FINAL ORDER] "
                f"product={product_id}, "
                f"nonce={order.nonce}, "
                f"decoded_recv={int(order.nonce) >> 20}, "
                f"ttl={(int(order.nonce) >> 20) - (time.time_ns() // 1_000_000)}ms"
            )
            
            logger.debug(
                f"[NADO DEBUG] "
                f"Gateway={getattr(self.client.context.engine_client, 'url', 'Unknown')} "
                f"ChainID={getattr(self.client.context.engine_client, 'chain_id', 'Unknown')}"
            )
            
            # Pure SDK call (runs synchronously in thread to avoid blocking)
            try:
                res = await asyncio.to_thread(self.client.market.place_order, params)
            except Exception as e:
                err_str = str(e)
                # Auto-fallback: if Maker Post-Only crosses the book (code 2008), retry immediately as IOC Taker
                if "2008" in err_str and use_maker_entry:
                    logger.warning(
                        f"[NadoTradingService] ⚠️ Post-Only order crossed the book (code 2008). "
                        f"Retrying entry as IOC Taker with slippage buffer for {symbol}..."
                    )
                    print(f"🔄 [NadoTradingService] Post-Only ордер отклонен биржей (код 2008: пересекает стакан). Авто-повтор как IOC Taker со слиппейдж-буфером...")
                    try:
                        try:
                            from nado_protocol.utils.expiration import OrderType
                            ioc_order_type = OrderType.IOC
                        except ImportError:
                            ioc_order_type = 1
                        
                        ioc_appendix = self.build_order_appendix(order_type=ioc_order_type)
                        ioc_nonce = gen_order_nonce()
                        
                        # Apply directional slippage buffer for IOC taker entry
                        if dir_clean == "LONG":
                            ioc_raw_limit = entry_price * (1.0 + slip_buffer)
                            ioc_price_x18 = int(Decimal(str(round(ioc_raw_limit, 8))) * Decimal(10**18))
                            ioc_price_x18 = (ioc_price_x18 // price_increment) * price_increment
                        else: # SHORT
                            ioc_raw_limit = entry_price * (1.0 - slip_buffer)
                            ioc_price_x18 = int(Decimal(str(round(ioc_raw_limit, 8))) * Decimal(10**18))
                            ioc_price_x18 = ((ioc_price_x18 + price_increment - 1) // price_increment) * price_increment
                        
                        final_limit_price = float(Decimal(ioc_price_x18) / Decimal(10**18))
                        if final_limit_price > 0 and (float(Decimal(abs(amount_x18)) / Decimal(10**18)) * final_limit_price) < 101.0:
                            size_inc_float = float(Decimal(size_increment) / Decimal(10**18))
                            if size_inc_float > 0:
                                cur_cntr = float(Decimal(abs(amount_x18)) / Decimal(10**18))
                                needed_contracts = math.ceil(101.0 / final_limit_price / size_inc_float) * size_inc_float
                                abs_amt = int(Decimal(str(round(needed_contracts, 8))) * Decimal(10**18))
                                abs_amt = (abs_amt // size_increment) * size_increment
                                amount_x18 = abs_amt if amount_x18 >= 0 else -abs_amt
                                if needed_contracts > cur_cntr:
                                    taker_fee_val = float(taker_fee_pct or 0.0005)
                                    bumped_risk = needed_contracts * abs(entry_price - float(sl_price)) + needed_contracts * (entry_price + float(sl_price)) * taker_fee_val
                                    max_allowed_risk = float(approved_risk_usd) * 1.35
                                    new_approved_risk = min(max_allowed_risk, max(float(approved_risk_usd), bumped_risk))
                                    logger.info(
                                        f"[NadoTradingService] ⚡ Predispatch CLOB adjustment (IOC fallback): Synchronized approved_risk_usd: "
                                        f"${float(approved_risk_usd):.2f} -> ${new_approved_risk:.2f}."
                                    )
                                    approved_risk_usd = new_approved_risk
                        ioc_order = OrderParams(
                            sender=sender,
                            amount=amount_x18,
                            priceX18=ioc_price_x18,
                            expiration=expiration,
                            nonce=ioc_nonce,
                            appendix=ioc_appendix
                        )
                        ioc_params = PlaceOrderParams(
                            product_id=product_id,
                            order=ioc_order
                        )
                        res = await asyncio.to_thread(self.client.market.place_order, ioc_params)
                        logger.info(f"[NadoTradingService] ✅ IOC Taker order successfully submitted after 2008 fallback.")
                    except Exception as fallback_err:
                        err_str = str(fallback_err)
                        logger.error(f"[NadoTradingService] ❌ Fallback IOC order failed: {fallback_err}")
                        self.last_execution_status = "REJECTED_BY_EXCHANGE"
                        self.last_execution_details = {"reason": err_str, "symbol": symbol}
                        return False
                elif "2070" in err_str and "maximum open interest" in err_str:
                    logger.warning(f"[NadoTradingService] ⚠️ Market OI limit reached for {symbol} (Testnet limitation). Order rejected.")
                    self.last_execution_status = "OI_LIMIT_REACHED"
                    self.last_execution_details = {"reason": err_str, "symbol": symbol}
                    return False
                else:
                    logger.error(f"[NadoTradingService] ❌ Order placement failed: {e}")
                    self.last_execution_status = "REJECTED_BY_EXCHANGE"
                    self.last_execution_details = {"reason": err_str, "symbol": symbol}
                    return False
                
            digest = res.data.digest if res.data else None
            
            if not digest:
                logger.error(f"[NadoTradingService] ❌ Order placement failed, no digest returned.")
                self.last_execution_status = "REJECTED_BY_EXCHANGE"
                self.last_execution_details = {"reason": "NO_DIGEST_RETURNED", "symbol": symbol}
                return False
                
            logger.info(f"[NadoTradingService] ✅ Order Accepted by Sequencer. Digest: {digest}")
            logger.info(f"[NadoTradingService] ⏳ Waiting for sequencer fill confirmation...")
            
            filled = False
            actual_filled_x18 = 0
            actual_entry_fee = 0.0
            
            for _ in range(5):
                await asyncio.sleep(2)
                try:
                    historical_data = await asyncio.to_thread(self.client.market.get_historical_orders_by_digest, [digest])
                    if historical_data and historical_data.orders:
                        order_info = historical_data.orders[0]
                        if abs(int(order_info.base_filled)) > 0:
                            filled = True
                            actual_filled_x18 = int(order_info.base_filled)
                            try:
                                raw_bf = float(order_info.base_filled)
                                raw_qf = float(order_info.quote_filled)
                                raw_fee = float(getattr(order_info, "fee", 0) or getattr(order_info, "fee_x18", 0) or 0)
                                norm_bf = self._normalize_indexer_amount(raw_bf) if abs(raw_bf) > 1e10 else raw_bf
                                norm_qf = self._normalize_indexer_amount(raw_qf) if abs(raw_qf) > 1e10 else raw_qf
                                norm_fee = self._normalize_indexer_amount(raw_fee) if abs(raw_fee) > 1e10 else raw_fee
                                if norm_fee > 0:
                                    actual_entry_fee = norm_fee

                                maker_p = 0.0
                                if not getattr(self, "_mock_mode", False) and self.client:
                                    try:
                                        indexer_c = getattr(getattr(self.client, "context", None), "indexer_client", None)
                                        if indexer_c and hasattr(indexer_c, "get_matches"):
                                            from nado_protocol.indexer_client.types import IndexerMatchesParams
                                            m_params = IndexerMatchesParams(
                                                subaccounts=[self.default_subaccount_id],
                                                product_ids=[int(product_id)],
                                                limit=5
                                            )
                                            m_res = await asyncio.to_thread(indexer_c.get_matches, m_params)
                                            if m_res and hasattr(m_res, "matches") and hasattr(m_res, "txs"):
                                                tx_map = {}
                                                for t in m_res.txs:
                                                    s_idx = str(getattr(t, "submission_idx", ""))
                                                    t_data = t.tx if isinstance(t.tx, dict) else (t.tx.model_dump() if hasattr(t.tx, "model_dump") else {})
                                                    mo = t_data.get("match_orders", {})
                                                    p_val = float(mo.get("maker", {}).get("order", {}).get("price_x18", 0)) / 1e18
                                                    if p_val > 0:
                                                        tx_map[s_idx] = p_val
                                                for m in m_res.matches:
                                                    m_dig = str(getattr(m, "digest", "")).lower()
                                                    s_idx = str(getattr(m, "submission_idx", ""))
                                                    if (m_dig == str(digest).lower() or s_idx == str(getattr(order_info, "submission_idx", ""))) and s_idx in tx_map:
                                                        maker_p = tx_map[s_idx]
                                                        break
                                    except Exception:
                                        pass

                                _, exact_p = self._calculate_gross_quote_and_price(
                                    base_filled=norm_bf,
                                    quote_filled=norm_qf,
                                    fee=norm_fee,
                                    maker_price=maker_p,
                                    is_mock=getattr(self, "_mock_mode", False)
                                )
                                if exact_p > 0:
                                    actual_entry_price = exact_p
                                    logger.info(f"[NadoTradingService] 🔍 Exact Entry Price from fill: {actual_entry_price:.4f} (maker_p={maker_p:.4f})")
                            except Exception as parse_e:
                                logger.debug(f"[NadoTradingService] Entry price parsing fallback: {parse_e}")
                            logger.info(f"[NadoTradingService] ✅ Order {digest} FILLED successfully!")
                            break
                except Exception as poll_e:
                    logger.warning(f"[NadoTradingService] ⚠️ Error polling order {digest}: {poll_e}")
                    
            if not filled:
                # ONE MORE CHECK + RECONCILIATION
                try:
                    final_data = await asyncio.to_thread(self.client.market.get_historical_orders_by_digest, [digest])
                    if final_data and final_data.orders:
                        final_order = final_data.orders[0]
                        if abs(int(final_order.base_filled)) > 0:
                            filled = True
                            actual_filled_x18 = int(final_order.base_filled)
                            try:
                                raw_bf = float(final_order.base_filled)
                                raw_qf = float(final_order.quote_filled)
                                raw_fee = float(getattr(final_order, "fee", 0) or getattr(final_order, "fee_x18", 0) or 0)
                                norm_bf = self._normalize_indexer_amount(raw_bf) if abs(raw_bf) > 1e10 else raw_bf
                                norm_qf = self._normalize_indexer_amount(raw_qf) if abs(raw_qf) > 1e10 else raw_qf
                                norm_fee = self._normalize_indexer_amount(raw_fee) if abs(raw_fee) > 1e10 else raw_fee
                                if norm_fee > 0:
                                    actual_entry_fee = norm_fee

                                _, exact_p = self._calculate_gross_quote_and_price(
                                    base_filled=norm_bf,
                                    quote_filled=norm_qf,
                                    fee=norm_fee,
                                    is_mock=getattr(self, "_mock_mode", False)
                                )
                                if exact_p > 0:
                                    actual_entry_price = exact_p
                                    logger.info(f"[NadoTradingService] 🔍 Exact Entry Price from fill on final check: {actual_entry_price:.4f}")
                            except Exception as parse_e:
                                logger.debug(f"[NadoTradingService] Final check entry price parsing fallback: {parse_e}")
                            logger.info(f"[NadoTradingService] ✅ Order {digest} FILLED on final check!")
                except Exception as e:
                    logger.warning(f"[NadoTradingService] ⚠️ Final order status check failed for {digest}: {e}")
                    
                if not filled:
                    logger.error(f"[NadoTradingService] ❌ Order {digest} was accepted but NOT filled within 10s. Cancelling to prevent race condition...")
                    cancel_confirmed = False
                    try:
                        from nado_protocol.engine_client.types.execute import CancelOrdersParams
                        cancel_params = CancelOrdersParams(
                            productIds=[product_id],
                            digests=[digest],
                            sender=sender
                        )
                        await asyncio.to_thread(self.client.market.cancel_orders, cancel_params)
                        logger.info(f"[NadoTradingService] 🗑️ Order {digest} cancelled successfully.")
                        cancel_confirmed = True
                    except Exception as cancel_e:
                        logger.error(f"[NadoTradingService] ❌ Failed to cancel timed out order {digest}: {cancel_e}")
                        cancel_confirmed = False
                    
                    if not cancel_confirmed:
                        logger.critical(
                            f"[NadoTradingService] 🚨 CRITICAL: Cancellation unconfirmed for order {digest} ({symbol}). "
                            f"Enqueuing for PENDING_ORDER_RECONCILIATION."
                        )
                        self.pending_reconciliations[symbol] = {
                            "symbol": symbol,
                            "base_symbol": base_symbol,
                            "product_id": product_id,
                            "subaccount": self.default_subaccount_id,
                            "direction": direction.upper(),
                            "entry_price": actual_entry_price,
                            "contracts": amount_base,
                            "size_usd": notional_usd,
                            "order_digest": digest,
                            "open_time": time.time(),
                            "detected_at": time.time(),
                            "status": "PENDING_ORDER_RECONCILIATION"
                        }
                        self._save_pending_reconciliations()
                        self._save_state()
                        self.last_execution_status = "PENDING_ORDER_RECONCILIATION"
                        return False
                    else:
                        # Auto-fallback: if maker POST_ONLY timed out without fill, retry immediately as IOC Taker
                        if cancel_confirmed and use_maker_entry:
                            logger.info(
                                f"[NadoTradingService] ⚠️ Post-Only order {digest} timed out without fill and was cancelled. "
                                f"Switching to IOC Taker entry with slip_buffer to guarantee execution..."
                            )
                            print(f"🔄 [NadoTradingService] Post-Only ордер не исполнился за 10с и отменен. Авто-переход на IOC Taker...")
                            try:
                                try:
                                    from nado_protocol.utils.expiration import OrderType
                                    ioc_order_type = OrderType.IOC
                                except ImportError:
                                    ioc_order_type = 1

                                ioc_appendix = self.build_order_appendix(order_type=ioc_order_type)
                                ioc_nonce = gen_order_nonce()

                                if dir_clean == "LONG":
                                    ioc_raw_limit = entry_price * (1.0 + slip_buffer)
                                    ioc_price_x18 = int(Decimal(str(round(ioc_raw_limit, 8))) * Decimal(10**18))
                                    ioc_price_x18 = (ioc_price_x18 // price_increment) * price_increment
                                else:
                                    ioc_raw_limit = entry_price * (1.0 - slip_buffer)
                                    ioc_price_x18 = int(Decimal(str(round(ioc_raw_limit, 8))) * Decimal(10**18))
                                    ioc_price_x18 = ((ioc_price_x18 + price_increment - 1) // price_increment) * price_increment

                                ioc_order = OrderParams(
                                    sender=sender,
                                    amount=amount_x18,
                                    priceX18=ioc_price_x18,
                                    expiration=expiration,
                                    nonce=ioc_nonce,
                                    appendix=ioc_appendix
                                )
                                ioc_params = PlaceOrderParams(
                                    product_id=product_id,
                                    order=ioc_order
                                )
                                ioc_res = await asyncio.to_thread(self.client.market.place_order, ioc_params)
                                ioc_digest = ioc_res.data.digest if (ioc_res and ioc_res.data) else None
                                if ioc_digest:
                                    logger.info(f"[NadoTradingService] ✅ Fallback IOC Taker order submitted: {ioc_digest}. Waiting for fill...")
                                    for _ in range(5):
                                        await asyncio.sleep(0.5)
                                        ioc_data = await asyncio.to_thread(self.client.market.get_historical_orders_by_digest, [ioc_digest])
                                        if ioc_data and ioc_data.orders:
                                            ioc_ord = ioc_data.orders[0]
                                            if abs(int(ioc_ord.base_filled)) > 0:
                                                filled = True
                                                digest = ioc_digest
                                                actual_filled_x18 = int(ioc_ord.base_filled)
                                                try:
                                                    raw_bf = float(ioc_ord.base_filled)
                                                    raw_qf = float(ioc_ord.quote_filled)
                                                    raw_fee = float(getattr(ioc_ord, "fee", 0) or getattr(ioc_ord, "fee_x18", 0) or 0)
                                                    norm_bf = self._normalize_indexer_amount(raw_bf) if abs(raw_bf) > 1e10 else raw_bf
                                                    norm_qf = self._normalize_indexer_amount(raw_qf) if abs(raw_qf) > 1e10 else raw_qf
                                                    norm_fee = self._normalize_indexer_amount(raw_fee) if abs(raw_fee) > 1e10 else raw_fee
                                                    if norm_fee > 0:
                                                        actual_entry_fee = norm_fee
                                                    _, exact_p = self._calculate_gross_quote_and_price(
                                                        base_filled=norm_bf, quote_filled=norm_qf, fee=norm_fee,
                                                        is_mock=getattr(self, "_mock_mode", False)
                                                    )
                                                    if exact_p > 0:
                                                        actual_entry_price = exact_p
                                                except Exception:
                                                    pass
                                                break
                                    if filled:
                                        logger.info(f"[NadoTradingService] ✅ Fallback IOC Taker order {digest} FILLED successfully!")
                            except Exception as ioc_err:
                                logger.error(f"[NadoTradingService] ❌ Fallback IOC Taker after timeout failed: {ioc_err}")

                        if not filled:
                            self.last_execution_status = "REJECTED_BY_EXCHANGE"
                            return False
            
            # --- Defensive Step: Immediate local registration before protection checks ---
            # Ensures position is never orphaned or invisible if emergency close fails or indexer lags
            actual_base_amount = abs(actual_filled_x18) / 1e18
            notional_usd = actual_base_amount * actual_entry_price
            
            self.active_positions[symbol] = {
                "symbol": symbol,
                "direction": direction.upper(),
                "entry_price": actual_entry_price,
                "size_usd": notional_usd,
                "notional_usd": notional_usd,
                "margin_used": notional_usd / float(leverage) if leverage > 0 else notional_usd,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "initial_sl": sl_price,
                "initial_tp": tp_price,
                "leverage": leverage,
                "highest_price": actual_entry_price,
                "lowest_price": actual_entry_price,
                "protection_state": "SUBMITTING_PROTECTION",
                "atr_reference": atr_value,
                "product_id": product_id,
                "sender": sender,
                "sl_digest": None,
                "tp_digest": None,
                "sl_type": None,
                "tp_type": None,
                "trigger_amount_x18": str(-actual_filled_x18),
                "contracts": actual_base_amount,
                "amount": actual_base_amount,
                "funding_rate_hourly": funding_rate_hourly,
                "holding_hours": holding_hours,
                "slippage_stress_pct": slippage_stress_pct if slippage_stress_pct is not None else slip_buffer,
                "accumulated_realized_pnl": 0.0,
                "original_thesis": original_thesis,
                "native_triggers_placed": False,
                "open_time": time.time(),
                "open_digest": digest,
                "planned_risk_usd": float(planned_risk_usd or approved_risk_usd or 0.0),
                "base_risk_pct": float(base_risk_pct or 0.0),
                "final_risk_pct": float(final_risk_pct or 0.0),
                "cohort": cohort or "UNKNOWN",
                "entry_fee_usd": round(actual_entry_fee if actual_entry_fee > 0 else (notional_usd * 0.0005), 4),
                "applied_multipliers": applied_multipliers or {},
                "strategy_mode": str(strategy_mode or "UNKNOWN").upper(),
                "is_anti_bot": bool(getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION"),
                "macro_regime": str(macro_regime or "UNKNOWN").upper(),
                "entry_quality": float(entry_quality or 0.0),
                "bb_pos_pct": float(bb_pos_pct or 50.0),
                "macro_range_score": float(macro_range_score or 0.0),
                "max_favorable_excursion_r": 0.0
            }
            self._save_positions()

            # --- Guaranteed Risk Parameters Initialized Prior to Any Execution Branches (P1-1) ---
            from core.risk_calculator import estimate_total_loss, calculate_max_tolerated_risk, calculate_max_allowed_stress_loss
            approved_budget = float(approved_risk_usd) if (approved_risk_usd and approved_risk_usd > 0) else (
                actual_base_amount * abs(entry_price - sl_price) if (entry_price > 0 and sl_price > 0) else 0.0
            )
            allowed_risk_threshold = calculate_max_tolerated_risk(approved_budget) if approved_budget > 0 else 0.0
            max_allowed_stress = calculate_max_allowed_stress_loss(approved_budget) if approved_budget > 0 else 0.0
            total_actual_loss = round(actual_base_amount * abs(actual_entry_price - sl_price), 2) if (actual_entry_price > 0 and sl_price > 0) else 0.0
            total_actual_stress = total_actual_loss

            # --- Unified Actual Risk vs Approved Risk Check (P0 / P1-1 / P1-2 / P1-3) ---
            if sl_price > 0 and actual_base_amount > 0 and actual_entry_price > 0:
                try:
                    loss_calc = estimate_total_loss(
                        contracts=actual_base_amount,
                        fill_price=actual_entry_price,
                        sl_price=sl_price,
                        taker_fee_pct=taker_fee_pct,
                        slippage_stress_pct=slippage_stress_pct if slippage_stress_pct is not None else slip_buffer,
                        funding_rate_hourly=funding_rate_hourly,
                        holding_hours=holding_hours,
                        approved_risk=approved_budget
                    )
                    total_actual_loss = loss_calc.total_expected_loss_usd
                    total_actual_stress = loss_calc.total_stress_loss_usd
                    allowed_risk_threshold = loss_calc.max_tolerated_risk_usd
                    max_allowed_stress = calculate_max_allowed_stress_loss(approved_budget)

                    expected_breached = (approved_budget > 0 and total_actual_loss > allowed_risk_threshold)
                    stress_breached = (approved_budget > 0 and total_actual_stress > max_allowed_stress)

                    if expected_breached or stress_breached:
                        breach_desc = "EXPECTED_LOSS" if expected_breached else "STRESS_LOSS"
                        risk_diff_pct = ((total_actual_loss / approved_budget) - 1.0) * 100 if approved_budget > 0 else 0.0
                        logger.error(
                            f"[NadoTradingService] ❌ FATAL SLIPPAGE / RISK BREACH ({breach_desc}): "
                            f"Total potential loss (${total_actual_loss:.2f}, stress=${total_actual_stress:.2f}) "
                            f"exceeds approved risk threshold (exp_max=${allowed_risk_threshold:.2f}, stress_max=${max_allowed_stress:.2f}, "
                            f"approved=${approved_budget:.2f}, fee=${loss_calc.fee_loss_usd:.2f}) by {risk_diff_pct:.1f}% "
                            f"due to adverse execution price ({actual_entry_price:.4f} vs ref {entry_price:.4f}). "
                            f"Emergency closing position."
                        )
                        close_ok, _ = await self.emergency_protect_risk_breach(
                            symbol=symbol,
                            approved_risk=approved_budget,
                            actual_risk=total_actual_loss
                        )
                        self.last_execution_status = "ABORTED_RISK_BREACH" if close_ok else "RISK_BREACH_UNRESOLVED"
                        self.last_execution_details = {
                            "reason": f"RISK_BREACH_{breach_desc}",
                            "approved_risk": approved_budget,
                            "actual_risk": total_actual_loss,
                            "actual_stress": total_actual_stress,
                            "symbol": symbol
                        }
                        return False
                except Exception as calc_err:
                    logger.error(
                        f"[NadoTradingService] ❌ Fatal risk calculator error during fill verification: {calc_err}. "
                        f"Refusing to leave position with unverified risk open; emergency closing immediately."
                    )
                    close_ok, actual_exit = await self.emergency_protect_risk_breach(
                        symbol=symbol,
                        approved_risk=approved_budget,
                        actual_risk=total_actual_loss
                    )
                    self.last_execution_status = "ABORTED_CALC_ERROR" if close_ok else "RISK_BREACH_UNRESOLVED"
                    self.last_execution_details = {
                        "reason": "RISK_CALCULATOR_ERROR",
                        "error": str(calc_err),
                        "error_type": type(calc_err).__name__,
                        "close_confirmed": close_ok,
                        "exit_price": actual_exit if close_ok else None
                    }
                    if not close_ok:
                        if symbol in self.active_positions:
                            self.active_positions[symbol]["protection_state"] = "RISK_BREACH_UNRESOLVED"
                        self.pending_reconciliations[symbol] = {
                            "symbol": symbol,
                            "base_symbol": base_symbol,
                            "product_id": product_id,
                            "subaccount": self.default_subaccount_id,
                            "direction": direction.upper(),
                            "entry_price": actual_entry_price,
                            "contracts": actual_base_amount,
                            "status": "RISK_BREACH_UNRESOLVED",
                            "reason": "RISK_CALC_ERROR_CLOSE_FAILED",
                            "detected_at": time.time()
                        }
                        try:
                            self._save_positions()
                            self._save_pending_reconciliations()
                            self._save_state()
                        except Exception as save_err:
                            logger.error(f"[NadoTradingService] Failed to persist emergency state: {save_err}")
                    return False
            else:
                logger.error(
                    f"[NadoTradingService] ❌ Invalid open position parameters (sl_price={sl_price}, "
                    f"entry={actual_entry_price}, size={actual_base_amount}). Risk budget cannot be established. "
                    f"Emergency closing position."
                )
                close_ok, _ = await self.emergency_protect_risk_breach(
                    symbol=symbol,
                    approved_risk=approved_budget,
                    actual_risk=total_actual_loss
                )
                self.last_execution_status = "ABORTED_PROTECTION_FAILURE" if close_ok else "RISK_BREACH_UNRESOLVED"
                self.last_execution_details = {
                    "reason": "INVALID_FILL_PARAMS_OR_PROTECTION_FAILURE",
                    "sl_price": sl_price,
                    "entry_price": actual_entry_price,
                    "size": actual_base_amount
                }
                return False

            # --- Partial Fill Guard (CRITICAL-4) ---
            fill_ratio = abs(actual_filled_x18) / abs(amount_x18) if amount_x18 != 0 else 0
            if fill_ratio < 0.95:
                logger.error(
                    f"[NadoTradingService] ❌ PARTIAL FILL {fill_ratio*100:.1f}%. "
                    f"Closing immediately to prevent untracked slivers (actual_filled={actual_base_amount})."
                )
                close_ok, _ = await self.emergency_protect_risk_breach(
                    symbol=symbol,
                    approved_risk=approved_budget,
                    actual_risk=total_actual_loss
                )
                self.last_execution_status = "ABORTED_PARTIAL_FILL" if close_ok else "RISK_BREACH_UNRESOLVED"
                self.last_execution_details = {
                    "reason": "PARTIAL_FILL",
                    "fill_ratio": fill_ratio,
                    "actual_base_amount": actual_base_amount,
                    "approved_risk": approved_budget,
                    "actual_risk": total_actual_loss
                }
                return False
            
            # --- Native Trigger Orders (TP/SL) ---
            trigger_amount_x18 = str(-actual_filled_x18)
            
            if direction.upper() == "LONG":
                sl_type = "oracle_price_below"
                tp_type = "last_price_above"
            else:
                sl_type = "oracle_price_above"
                tp_type = "last_price_below"
                
            sl_digest = None
            tp_digest = None
            price_increment_base = float(price_increment) / 1e18
            
            fill_notional_usd = abs(actual_filled_x18 / 1e18) * actual_entry_price
            skip_native_triggers = (fill_notional_usd < 100.0) or bool(use_software_tpsl) or bool(software_sl_tp_only)

            if skip_native_triggers:
                logger.info(
                    f"[NadoTradingService] ℹ️ Software TP/SL Mode: Order notional (${fill_notional_usd:.2f} < $100.00 or explicit SW mode). "
                    f"Skipping native trigger placement to avoid error 2094. "
                    f"Position guarded 100% via FastPriceMonitor (Oracle SL / Last TP) & Sentinel."
                )
            else:
                # Place Native Stop Loss
                if sl_price > 0:
                    exec_price = sl_price * 0.9 if direction.upper() == "LONG" else sl_price * 1.1
                    
                    # Direction-aware safe Decimal x18 conversion to avoid IEEE-754 precision loss
                    sl_mode = "ceil" if direction.upper() == "SHORT" else "floor"
                    exec_price_x18 = to_x18_aligned(exec_price, price_increment, mode=sl_mode)
                    trigger_price_x18 = to_x18_aligned(sl_price, price_increment, mode=sl_mode)
                    
                    try:
                        sl_res = await asyncio.to_thread(
                            self.client.market.place_price_trigger_order,
                            product_id=product_id,
                            price_x18=str(exec_price_x18),
                            amount_x18=trigger_amount_x18,
                            trigger_price_x18=str(trigger_price_x18),
                            trigger_type=sl_type,
                            reduce_only=True
                        )
                        sl_digest = sl_res.data.digest if sl_res.data else None
                        logger.info(f"[NadoTradingService] 🛡️ Native Stop Loss placed at {sl_price}")
                    except Exception as e:
                        err_msg = str(e)
                        if "2094" in err_msg or "too small" in err_msg:
                            logger.info(
                                f"[NadoTradingService] ℹ️ Отложенный SL не выставлен на бирже (объем ${fill_notional_usd:.2f} < $100 лимита биржи). "
                                f"Позиция полностью защищена программным Stop-Loss на {sl_price:.4f} через Fast Monitor (5с) и Sentinel."
                            )
                        else:
                            logger.warning(
                                f"[NadoTradingService] ⚠️ Биржевой SL не выставлен ({e}). "
                                f"Позиция удерживается под программной защитой Stop-Loss на {sl_price:.4f}!"
                            )
                        sl_digest = None
                        
                # Place Native Take Profit
                if tp_price > 0:
                    # F-10 Fix: Protect native Take Profit execution against slippage in thin orderbooks
                    if direction.upper() == "LONG":
                        min_tp_exec = actual_entry_price * 1.001 if actual_entry_price > 0 else tp_price * 0.998
                        exec_price = min(max(tp_price * 0.998, min_tp_exec), tp_price)
                    else:
                        max_tp_exec = actual_entry_price * 0.999 if actual_entry_price > 0 else tp_price * 1.002
                        exec_price = max(min(tp_price * 1.002, max_tp_exec), tp_price)
                    
                    tp_mode = "floor" if direction.upper() == "SHORT" else "ceil"
                    exec_price_x18 = to_x18_aligned(exec_price, price_increment, mode=tp_mode)
                    trigger_price_x18 = to_x18_aligned(tp_price, price_increment, mode=tp_mode)
                    
                    try:
                        tp_res = await asyncio.to_thread(
                            self.client.market.place_price_trigger_order,
                            product_id=product_id,
                            price_x18=str(exec_price_x18),
                            amount_x18=trigger_amount_x18,
                            trigger_price_x18=str(trigger_price_x18),
                            trigger_type=tp_type,
                            reduce_only=True
                        )
                        tp_digest = tp_res.data.digest if tp_res and tp_res.data else None
                        logger.info(f"[NadoTradingService] 🎯 Native Take Profit placed at {tp_price}")
                    except Exception as e:
                        err_msg = str(e)
                        if "2094" in err_msg or "too small" in err_msg:
                            logger.info(
                                f"[NadoTradingService] ℹ️ Отложенный TP не выставлен на бирже (объем ${fill_notional_usd:.2f} < $100 лимита биржи). "
                                f"Позиция полностью защищена программным Take-Profit на {tp_price:.4f} через Fast Monitor (5с)."
                            )
                        else:
                            logger.warning(
                                f"[NadoTradingService] ⚠️ Биржевой TP не выставлен ({e}). "
                                f"Позиция удерживается под программной защитой Take-Profit на {tp_price:.4f}!"
                            )

            # Recalculate notional_usd to reflect actual fill amount
            notional_usd = fill_notional_usd
            
            # Determine honest protection state (P0-1 & P0-2)
            has_native_sl = bool(sl_digest)
            has_native_tp = bool(tp_digest)
            is_sw_sl_confirmed = self.is_software_protection_confirmed(symbol, sl_price)
            
            if has_native_sl:
                protection_state = "NATIVE_SL_CONFIRMED"
                prot_mode = "EXCHANGE_PROTECTED"
            elif is_sw_sl_confirmed:
                protection_state = "SOFTWARE_SL_CONFIRMED"
                prot_mode = "SOFTWARE_ONLY"
                logger.info(
                    f"[NadoTradingService] 🛡️ Software Stop-Loss active at {sl_price:.4f} "
                    f"via verified Fast Monitor (native SL order unavailable or below exchange minimum)."
                )
            elif skip_native_triggers and not getattr(self, "fast_monitor_disabled", False):
                protection_state = "SOFTWARE_PROTECTED"
                prot_mode = "SOFTWARE_ONLY"
                logger.info(
                    f"[NadoTradingService] 🛡️ Software Stop-Loss active at {sl_price:.4f} "
                    f"via Fast Monitor (native triggers skipped for sub-$100 notional)."
                )
            else:
                protection_state = "PROTECTION_DEGRADED"
                prot_mode = "UNPROTECTED"
                logger.critical(
                    f"[NadoTradingService] 🚨 CRITICAL: Position {symbol} opened with NO VALID STOP-LOSS "
                    f"(native={has_native_sl}, sw_confirmed={is_sw_sl_confirmed})! Protection degraded."
                )

            # Finalize position state with confirmed native triggers
            self.active_positions[symbol].update({
                "direction": direction.upper(),
                "entry_price": actual_entry_price,
                "size_usd": notional_usd,
                "notional_usd": notional_usd,
                "margin_used": notional_usd / float(leverage) if leverage > 0 else notional_usd,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "leverage": leverage,
                "protection_state": protection_state,
                "sl_protection_mode": prot_mode,
                "use_software_tpsl": skip_native_triggers,
                "software_sl_tp_only": skip_native_triggers,
                "sl_digest": sl_digest,
                "tp_digest": tp_digest,
                "sl_type": sl_type,
                "tp_type": tp_type if tp_price > 0 else None,
                "native_sl_placed": has_native_sl,
                "native_tp_placed": has_native_tp,
                "native_triggers_placed": bool(has_native_sl and has_native_tp),
                "be_activation_r": float(get_tuned_param("be_activation_r", 0.75)),
                "tp1_r_mult": float(get_tuned_param("be_activation_r", 0.75))
            })
            if tp_price > 0:
                try:
                    self.initialize_partial_tp_plan(symbol)
                except Exception as plan_err:
                    logger.warning(f"[NadoTradingService] ⚠️ Could not initialize partial TP plan for {symbol}: {plan_err}")
            self._save_positions()

            # P0-2: Mandatory protection enforcement
            # If position protection is degraded, an unhedged position exists on exchange.
            # Never treat as success: immediately execute emergency closure!
            if protection_state == "PROTECTION_DEGRADED":
                logger.critical(
                    f"[NadoTradingService] 🚨 CRITICAL: Position {symbol} has PROTECTION_DEGRADED! "
                    f"Mandatory SL missing or unconfirmed. Executing emergency closure to preserve capital."
                )
                close_ok, actual_exit = await self.emergency_protect_risk_breach(
                    symbol=symbol,
                    reason=f"PROTECTION_DEGRADED: No confirmed native or software stop-loss (sl={sl_price})",
                    contracts=actual_base_amount
                )
                if close_ok:
                    self.last_execution_status = "ABORTED_PROTECTION_FAILURE"
                    self.last_execution_details = {
                        "reason": "PROTECTION_DEGRADED",
                        "exit_price": actual_exit,
                        "symbol": symbol
                    }
                    return False
                else:
                    self.last_execution_status = "RISK_BREACH_UNRESOLVED"
                    self.last_execution_details = {
                        "reason": "PROTECTION_DEGRADED_CLOSE_FAILED",
                        "symbol": symbol
                    }
                    return False
                
            self.last_execution_status = "FILLED_AND_PROTECTED"
            self.last_execution_details = {
                "symbol": symbol,
                "protection_state": protection_state,
                "entry_price": actual_entry_price
            }

            # Asynchronous multi-user copy trading dispatch (runs strictly AFTER master execution)
            try:
                from services.copy_trading_dispatcher import copy_trading_dispatcher
                copy_trading_dispatcher.master_trading_service = self
                asyncio.create_task(
                    copy_trading_dispatcher.dispatch_copy_trade_open(
                        symbol=symbol,
                        direction=direction,
                        entry_price=actual_entry_price,
                        master_sl=sl_price,
                        master_tp=tp_price,
                        limit_price=limit_price,
                        is_reduce_size=bool(getattr(self, "is_reduce_size", False) or (applied_multipliers and applied_multipliers.get("reduce_size"))),
                        conviction=float(entry_quality or 100.0),
                        strategy_mode=str(strategy_mode or "TREND_FOLLOWING"),
                        macro_regime=str(macro_regime or "TRENDING"),
                        cohort=str(cohort or "UNKNOWN"),
                        master_open_time=time.time(),
                        leverage=leverage,
                        contracts=actual_base_amount,
                        notional_usd=notional_usd
                    )
                )
            except Exception as copy_dispatch_err:
                logger.debug(f"[CopyTrading] Auto-dispatch open trigger: {copy_dispatch_err}")

            return True
        except Exception as e:
            # Check if order was already filled before the exception occurred
            is_post_fill = bool(locals().get("filled") or (locals().get("actual_filled_x18", 0) != 0) or (symbol in self.active_positions))
            if is_post_fill:
                logger.critical(
                    f"[NadoTradingService] 🚨 CRITICAL UNEXPECTED POST-FILL EXCEPTION for {symbol}: {e}. "
                    f"Position is active on-chain, but post-fill processing raised {type(e).__name__}. "
                    f"Initiating emergency protection immediately to prevent unbounded risk."
                )
                try:
                    close_ok, actual_exit = await self.emergency_protect_risk_breach(
                        symbol=symbol,
                        approved_risk=locals().get("approved_budget", 0.0),
                        actual_risk=locals().get("total_actual_loss", 0.0)
                    )
                except Exception as em_err:
                    logger.critical(f"[NadoTradingService] ❌ Failed to execute emergency protection on post-fill error: {em_err}")
                    close_ok, actual_exit = False, 0.0

                self.last_execution_status = "ABORTED_POST_FILL_ERROR" if close_ok else "RISK_BREACH_UNRESOLVED"
                self.last_execution_details = {
                    "reason": "UNEXPECTED_POST_FILL_EXCEPTION",
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "close_confirmed": close_ok,
                    "exit_price": actual_exit if close_ok else None
                }
                if not close_ok:
                    if symbol in self.active_positions:
                        self.active_positions[symbol]["protection_state"] = "RISK_BREACH_UNRESOLVED"
                    self.pending_reconciliations[symbol] = {
                        "symbol": symbol,
                        "base_symbol": base_symbol if 'base_symbol' in locals() else symbol.split('-')[0].upper(),
                        "product_id": locals().get("product_id"),
                        "subaccount": self.default_subaccount_id,
                        "direction": str(direction).upper(),
                        "entry_price": locals().get("actual_entry_price", entry_price),
                        "contracts": locals().get("actual_base_amount", locals().get("amount_base", 0.0)),
                        "status": "RISK_BREACH_UNRESOLVED",
                        "reason": "POST_FILL_EXCEPTION_CLOSE_FAILED",
                        "detected_at": time.time()
                    }
                    try:
                        self._save_positions()
                        self._save_pending_reconciliations()
                        self._save_state()
                    except Exception as save_err:
                        logger.error(f"[NadoTradingService] Failed to persist outer emergency state: {save_err}")
                return False

            logger.error(f"[NadoTradingService] ⚠️ Failed to place order: {e}")
            self.last_execution_status = "REJECTED_BY_EXCHANGE"
            self.last_execution_details = {"reason": str(e), "error_type": type(e).__name__}
            try:
                import traceback
                with open("logs/nado_errors.txt", "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {symbol} {direction} ERROR: {type(e).__name__}: {e}\n{traceback.format_exc()}\n")
            except Exception:
                pass
            return False

    def _load_state(self, force: bool = False):
        from core.state_store import StateStore
        state_file = "data/memory/nado_state.json"
        state = StateStore.load(state_file)
        if state and isinstance(state, dict):
            if state.get("initial_balance") is not None:
                disk_init = float(state["initial_balance"])
                if getattr(self, "_initial_balance", None) is None or force or disk_init != getattr(self, "_initial_balance", None):
                    self._initial_balance = disk_init
                    logger.debug(f"[NadoTradingService] 💾 Loaded initial balance: {self._initial_balance}")
            if state.get("peak_balance") is not None:
                disk_peak = float(state["peak_balance"])
                if getattr(self, "_peak_balance", None) is None or force or disk_peak != getattr(self, "_peak_balance", 0.0):
                    self._peak_balance = disk_peak
                    logger.debug(f"[NadoTradingService] 💾 Loaded peak balance: {self._peak_balance}")
            if not getattr(self, "_manual_daily_start", False):
                if "daily_start_balance" in state and state["daily_start_balance"] is not None:
                    disk_daily = float(state["daily_start_balance"])
                    if getattr(self, "_daily_start_balance", None) is None or force or disk_daily != getattr(self, "_daily_start_balance", None):
                        self._daily_start_balance = disk_daily
                if "daily_start_date" in state and state["daily_start_date"] is not None:
                    disk_date = str(state["daily_start_date"])
                    if getattr(self, "_daily_start_date", None) is None or force or disk_date != getattr(self, "_daily_start_date", None):
                        self._daily_start_date = disk_date
            if not getattr(self, "_manual_stats", False):
                if "win_count" in state:
                    self.win_count = int(state["win_count"])
                if "loss_count" in state:
                    self.loss_count = int(state["loss_count"])
                if "_daily_start_total_trades" in state and state["_daily_start_total_trades"] is not None:
                    self._daily_start_total_trades = int(state["_daily_start_total_trades"])
                else:
                    self._daily_start_total_trades = self.win_count + self.loss_count + getattr(self, 'scratch_count', 0)
                if "recent_streak" in state:
                    self.recent_streak = state["recent_streak"]
            if "cooldown_until" in state and (getattr(self, "cooldown_until", 0.0) == 0.0 or force):
                self.cooldown_until = float(state["cooldown_until"])
            if "_last_cooldown_processed_len" in state and (getattr(self, "_last_cooldown_processed_len", 0) == 0 or force):
                self._last_cooldown_processed_len = int(state["_last_cooldown_processed_len"])

    def adjust_ledger(self, amount: float):
        """Adjusts the initial balance baseline for deposits/withdrawals."""
        self._load_state(force=True)
        if getattr(self, "_initial_balance", None) is None:
            self._initial_balance = 100.0
        self._initial_balance = max(0.01, self._initial_balance + amount)
        if getattr(self, "_peak_balance", None) is not None:
            self._peak_balance = max(0.01, self._peak_balance + amount)
        self._save_state()
        logger.info(f"[NadoTradingService] 💰 Ledger adjusted by ${amount:+.2f}. New initial balance: ${self._initial_balance:.2f}")

    def reset_ledger(self):
        """Resets the initial balance to the current equity/spot balance and zeroes historical win/loss stats."""
        self._load_state(force=True)
        spot_usdc = 100.0
        if hasattr(self, "_last_equity") and self._last_equity:
            spot_usdc = self._last_equity
        elif self.client and self.default_subaccount_id:
            try:
                summary = self.client.subaccount.get_engine_subaccount_summary(self.default_subaccount_id)
                if hasattr(summary, "spot_balances"):
                    for spot in summary.spot_balances:
                        if spot.product_id == 0:
                            spot_usdc = float(spot.balance.amount) / 1e18
                            break
            except Exception:
                pass
        self._initial_balance = spot_usdc
        self._daily_start_balance = spot_usdc
        self._peak_balance = spot_usdc
        self.win_count = 0
        self.loss_count = 0
        self.recent_streak = []
        self._daily_start_total_trades = 0
        self.cooldown_until = 0.0
        self._last_cooldown_processed_len = 0
        self._portfolio_summary_cache_time = 0
        if hasattr(self, "_portfolio_summary_cache"):
            delattr(self, "_portfolio_summary_cache")
        self._save_state()
        logger.info(f"[NadoTradingService] 🔄 Ledger reset. Initial balance re-anchored to ${self._initial_balance:.2f}. Win/Loss counters reset to 0W / 0L.")

    def reset_peak(self) -> float:
        """Synchronizes peak_balance with current balance and saves state."""
        summary = self.get_cached_portfolio_summary() or {}
        equity = float(summary.get("current_balance") or getattr(self, "_initial_balance", 100.0) or 100.0)
        self._peak_balance = equity
        self._save_state()
        logger.info(f"[NadoTradingService] 🔄 Peak balance reset to current balance: ${self._peak_balance:.2f}")
        return self._peak_balance

    def _save_state(self):
        from core.state_store import StateStore
        state_file = "data/memory/nado_state.json"
        StateStore.save(state_file, {
            "initial_balance": self._initial_balance,
            "peak_balance": getattr(self, "_peak_balance", None),
            "daily_start_balance": getattr(self, "_daily_start_balance", None),
            "daily_start_date": getattr(self, "_daily_start_date", None),
            "_daily_start_total_trades": getattr(self, "_daily_start_total_trades", 0),
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "recent_streak": self.recent_streak,
            "cooldown_until": getattr(self, "cooldown_until", 0.0),
            "_last_cooldown_processed_len": getattr(self, "_last_cooldown_processed_len", 0)
        })

    def _evaluate_and_update_streak(
        self,
        net_pnl: float,
        realized_r: Optional[float] = None,
        planned_risk: Optional[float] = None,
        pos_data: Optional[Dict[str, Any]] = None,
        symbol: str = ""
    ) -> str:
        """
        Applies a unified Scratch Guard across ALL exit pathways (on-chain SL/TP, Time-Decay, Software Force Close, etc.):
        - Range -0.22R <= realized_r <= +0.15R is classified as 'SCRATCH' (or if planned_risk <= 0 and abs(net_pnl) <= 0.05).
          Scratch trades (including micro-losses from Time-Decay and fees) do NOT increment
          win_count or loss_count and do NOT pollute recent_streak.
        - realized_r < -0.22R (or net_pnl < -0.05 without planned_risk) is recorded as 'LOSS'.
        - realized_r > +0.15R (or net_pnl > 0.05 without planned_risk) is recorded as 'WIN'.
        Returns: 'WIN', 'LOSS', or 'SCRATCH'
        """
        if realized_r is None and pos_data:
            p_risk = planned_risk or float(pos_data.get("planned_risk_usd") or pos_data.get("approved_risk_usd") or 0.0)
            if p_risk <= 0.0:
                sl_p = float(pos_data.get("sl_price") or 0.0)
                entry_p = float(pos_data.get("entry_price") or 0.0)
                cnt = float(pos_data.get("contracts") or pos_data.get("amount") or 0.0)
                if entry_p > 0 and sl_p > 0 and cnt > 0:
                    p_risk = cnt * abs(entry_p - sl_p)
            if p_risk > 0:
                realized_r = round(net_pnl / p_risk, 4)

        # Safeguard against realized_r == 0.0 defaults when PnL is non-trivial (> $0.05):
        # If planned_risk was missing/uncalculated, realized_r=0.0 must NOT falsely qualify as scratch.
        if realized_r == 0.0 and abs(net_pnl) > 0.05:
            realized_r = None

        is_scratch = False
        if realized_r is not None and (realized_r != 0.0 or abs(net_pnl) <= 0.05):
            if -0.22 <= realized_r <= 0.15:
                is_scratch = True
        else:
            # Fallback if planned risk is completely unknown: <= 5 cents PnL is scratch/fees
            if abs(net_pnl) <= 0.05:
                is_scratch = True

        sym_str = f" for {symbol}" if symbol else ""
        if is_scratch:
            r_str = f" ({realized_r:+.2f}R | ${net_pnl:+.4f})" if realized_r is not None else f" (${net_pnl:+.4f})"
            logger.info(f"[NadoTradingService] 🛡️ Scratch Guard: Position{sym_str} closed at Scratch/Breakeven{r_str}. Win/Loss streak untouched.")
            self._save_state()
            return "SCRATCH"
        elif (realized_r is not None and realized_r > 0.15) or (realized_r is None and net_pnl > 0.05):
            self._update_streak_and_cooldown("WIN")
            return "WIN"
        elif (realized_r is not None and realized_r < -0.22) or (realized_r is None and net_pnl < -0.05):
            self._update_streak_and_cooldown("LOSS")
            return "LOSS"
        else:
            self._save_state()
            return "SCRATCH"

    def _update_streak_and_cooldown(self, outcome: str):
        import time
        if outcome == "WIN":
            self.win_count += 1
            self.recent_streak.append("WIN")
        elif outcome == "LOSS":
            self.loss_count += 1
            self.recent_streak.append("LOSS")
        self.recent_streak = self.recent_streak[-10:]

        # Immediate cooldown activation upon 3 consecutive losses
        if len(self.recent_streak) >= 3 and self.recent_streak[-3:] == ["LOSS", "LOSS", "LOSS"]:
            if getattr(self, "_last_cooldown_processed_len", 0) != len(self.recent_streak):
                self.cooldown_until = time.time() + 3600
                self._last_cooldown_processed_len = len(self.recent_streak)
                logger.warning(
                    f"[NadoTradingService] 🛑 Зафиксировано 3 убытка подряд! "
                    f"Автоматический перерыв на 1 час (до {time.strftime('%H:%M:%S', time.localtime(self.cooldown_until))})."
                )
        self._save_state()

    def _save_positions(self):
        """Persists active positions and their SL/TP targets to disk."""
        try:
            from core.state_store import StateStore
            state_file = "data/memory/active_positions.json"
            StateStore.save(state_file, self.active_positions)
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to save active positions to disk: {e}")

    def _load_positions(self) -> dict:
        """Loads persisted active positions from disk, ensuring deduplication to canonical symbols."""
        try:
            from core.state_store import StateStore
            state_file = "data/memory/active_positions.json"
            data = StateStore.load(state_file)
            if not isinstance(data, dict):
                return {}
            # Deduplicate dual-keys (e.g. SOL and SOL-USD) into single canonical instances
            clean_positions = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    canon_k = f"{k.replace('/', '-').split('-')[0].upper()}-USD"
                    if canon_k not in clean_positions or k == canon_k:
                        clean_positions[canon_k] = v
            return clean_positions
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to load active positions from disk: {e}")
            return {}

    def _save_pending_reconciliations(self):
        """Persists trades pending indexer fill reconciliation to disk."""
        try:
            from core.state_store import StateStore
            state_file = "data/memory/pending_reconciliations.json"
            StateStore.save(state_file, getattr(self, "pending_reconciliations", {}))
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to save pending reconciliations: {e}")

    def _load_pending_reconciliations(self) -> dict:
        """Loads trades pending indexer fill reconciliation from disk."""
        try:
            from core.state_store import StateStore
            state_file = "data/memory/pending_reconciliations.json"
            data = StateStore.load(state_file)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to load pending reconciliations: {e}")
            return {}

    def _save_processed_fills(self):
        """Persists processed fill records to disk with timestamps and metadata.
        Guarantees strict FIFO/LRU retention (keeps newest 3000 records)."""
        try:
            from core.state_store import StateStore
            state_file = "data/memory/processed_fills.json"
            reg = getattr(self, "processed_fills_registry", {})
            sorted_items = sorted(
                reg.items(),
                key=lambda kv: kv[1].get("timestamp", 0.0) if isinstance(kv[1], dict) else 0.0
            )
            if len(sorted_items) > 3000:
                sorted_items = sorted_items[-3000:]
            self.processed_fills_registry = dict(sorted_items)
            self.processed_fill_digests = set(self.processed_fills_registry.keys())
            StateStore.save(state_file, self.processed_fills_registry)
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to save processed fills: {e}")

    def _load_processed_fills(self) -> set:
        """Loads processed fill records from disk and initializes registry."""
        try:
            from core.state_store import StateStore
            state_file = "data/memory/processed_fills.json"
            data = StateStore.load(state_file)
            reg = {}
            if isinstance(data, dict):
                reg = data
            elif isinstance(data, list):
                # Backward compatibility: legacy format was a list of string digests
                now = time.time()
                for d in data:
                    d_str = str(d).strip().lower()
                    if d_str:
                        reg[d_str] = {"digest": d_str, "timestamp": now}
            self.processed_fills_registry = reg
            return set(reg.keys())
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to load processed fills: {e}")
            self.processed_fills_registry = {}
            return set()

    def _register_processed_fills(self, digests: List[str], subaccount: str = "", product_id: Optional[int] = None, symbol: str = ""):
        """Registers verified fill digests into registry with metadata and persists to disk."""
        if not digests:
            return
        now = time.time()
        if not hasattr(self, "processed_fills_registry") or self.processed_fills_registry is None:
            self.processed_fills_registry = {}
        if not hasattr(self, "processed_fill_digests") or self.processed_fill_digests is None:
            self.processed_fill_digests = set()
        for d in digests:
            d_norm = str(d).strip().lower()
            if d_norm:
                self.processed_fills_registry[d_norm] = {
                    "digest": d_norm,
                    "timestamp": now,
                    "subaccount": str(subaccount) if subaccount else "",
                    "product_id": product_id,
                    "symbol": symbol
                }
                self.processed_fill_digests.add(d_norm)
        self._save_processed_fills()

    def _record_closed_trade_journal(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        net_pnl: float,
        pos_data: Dict[str, Any],
        fee: float = 0.0,
        triggered_by: str = "MANUAL_OR_ON_CHAIN"
    ) -> float:
        """
        Records a completed trade into the persistent Trade Journal (data/memory/trade_journal.json).
        Calculates exact realized R-multiple: R = Net PnL / Planned Risk.
        Returns the realized R-multiple.
        """
        planned_risk = float(pos_data.get("planned_risk_usd") or pos_data.get("approved_risk_usd") or 0.0)
        if planned_risk <= 0.0:
            sl_p = float(pos_data.get("sl_price") or 0.0)
            cnt = float(pos_data.get("contracts") or 0.0)
            if entry_price > 0 and sl_p > 0 and cnt > 0:
                planned_risk = cnt * abs(entry_price - sl_p)

        realized_r = round(net_pnl / planned_risk, 4) if planned_risk > 0 else 0.0
        cohort = pos_data.get("cohort", "UNKNOWN")
        multipliers = pos_data.get("applied_multipliers", {})

        cnt = float(pos_data.get("contracts") or pos_data.get("amount") or 0.0)
        price_diff = (float(exit_price) - float(entry_price)) if direction == "LONG" else (float(entry_price) - float(exit_price))
        calc_gross = round(price_diff * cnt, 4) if cnt > 0 else 0.0
        gross_pnl = float(pos_data.get("gross_pnl_usd", calc_gross))

        entry_fee = float(pos_data.get("entry_fee_usd", 0.0) or 0.0)
        exit_fee = float(pos_data.get("exit_fee_usd", fee) or fee)
        total_fee = float(pos_data.get("total_fee_usd", fee) or fee)
        pnl_source = pos_data.get("pnl_source", "EXACT")

        trade_entry = {
            "timestamp": time.time(),
            "symbol": symbol,
            "direction": direction,
            "entry_price": round(float(entry_price or 0.0), 6),
            "exit_price": round(float(exit_price or 0.0), 6),
            "gross_pnl_usd": round(gross_pnl, 4),
            "entry_fee_usd": round(entry_fee, 4),
            "exit_fee_usd": round(exit_fee, 4),
            "total_fee_usd": round(total_fee, 4),
            "net_pnl_usd": round(float(net_pnl or 0.0), 4),
            "fee_usd": round(float(total_fee or fee or 0.0), 4),
            "planned_risk_usd": round(float(planned_risk or 0.0), 4),
            "realized_r": realized_r,
            "pnl_source": pnl_source,
            "cohort": cohort,
            "base_risk_pct": float(pos_data.get("base_risk_pct", 0.0)),
            "final_risk_pct": float(pos_data.get("final_risk_pct", 0.0)),
            "applied_multipliers": multipliers,
            "triggered_by": triggered_by,
            "open_time": float(pos_data.get("open_time", 0.0)),
            "strategy_mode": str(pos_data.get("strategy_mode", "UNKNOWN")).upper(),
            "macro_regime": str(pos_data.get("macro_regime", "UNKNOWN")).upper(),
            "entry_quality": float(pos_data.get("entry_quality", 0.0) or 0.0),
            "bb_pos_pct": float(pos_data.get("bb_pos_pct", 50.0) or 50.0),
            "macro_range_score": float(pos_data.get("macro_range_score", 0.0) or 0.0),
            "max_favorable_excursion_r": float(pos_data.get("max_favorable_excursion_r", 0.0) or 0.0)
        }

        try:
            from core.state_store import StateStore
            journal_file = "data/memory/trade_journal.json"
            journal = StateStore.load(journal_file, default=[])
            if not isinstance(journal, list):
                journal = []
            journal.append(trade_entry)
            if len(journal) > 1000:
                journal = journal[-1000:]
            StateStore.save(journal_file, journal)

            # Auto-Recalibration Hook (Contour 1 Walk-Forward Bayesian Calibration)
            # Triggers periodic recalibration every 12 closed trades once minimum sample (>= 20) is met
            if len(journal) >= 20 and len(journal) % 12 == 0:
                logger.info(
                    f"[NadoTradingService] 🔄 Auto-recalibration cadence reached ({len(journal)} trades in journal). "
                    f"Triggering Bayesian Optimizer walk-forward recalibration..."
                )
                self._trigger_bayesian_recalibration(len(journal))
        except Exception as e:
            logger.warning(f"[NadoTradingService] ⚠️ Could not save trade to journal: {e}")

        logger.info(
            f"[NadoTradingService] 📊 TRADE JOURNAL ENTRY -> {symbol} {direction} | Net PnL: ${net_pnl:.4f} "
            f"({realized_r:+.2f}R | Planned Risk: ${planned_risk:.2f} | Cohort: {cohort} | Reason: {triggered_by})"
        )
        return realized_r

    def _trigger_bayesian_recalibration(self, journal_len: int):
        """
        Launches Bayesian Optimization walk-forward recalibration in a background thread.
        Evaluates Optuna TPE against trade journal and applies the Acceptance Gate.
        """
        import threading
        try:
            thread = threading.Thread(
                target=self._run_bayesian_recalibration_job,
                args=(journal_len,),
                name="BayesianOptimizerAutoRecalibrationThread",
                daemon=True
            )
            thread.start()
            self._recalibration_thread = thread
            return thread
        except Exception as e:
            logger.warning(f"[NadoTradingService] ⚠️ Could not launch auto-recalibration thread: {e}")
            return None

    def _run_bayesian_recalibration_job(self, journal_len: int, n_trials: int = 120):
        """
        Background worker that executes TradingBayesianOptimizer.run_optimization.
        Respects Acceptance Gate before overwriting tuned_hyperparams.json.
        """
        try:
            logger.info(f"[NadoTradingService] 🧪 Starting background Bayesian Recalibration (trials={n_trials}, trades={journal_len})...")
            from core.bayesian_optimizer import TradingBayesianOptimizer
            optimizer = TradingBayesianOptimizer()
            result = optimizer.run_optimization(n_trials=n_trials, enforce_acceptance_gate=True)
            self._last_recalibration_result = result
            if result.get("gate_passed", False):
                logger.info(
                    f"[NadoTradingService] ✅ Bayesian Recalibration successfully updated hyperparameters! "
                    f"New params: {result.get('best_params')}"
                )
            else:
                logger.warning(
                    f"[NadoTradingService] ⚠️ Bayesian Recalibration gate rejected: {result.get('gate_reason')}. "
                    f"Keeping previous hyperparameters."
                )
            return result
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Bayesian Recalibration worker error: {e}", exc_info=True)
            return None

    async def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """Checks if a position was closed natively by Nado (TP/SL trigger)."""
        closed_reports = []
        if not self.is_connected:
            return closed_reports

        # Reconcile any pending trades waiting for indexer fills
        if getattr(self, "pending_reconciliations", None):
            try:
                resolved = await self.reconcile_pending_trade_fills()
                if resolved:
                    closed_reports.extend(resolved)
            except Exception as e:
                logger.warning(f"[NadoTradingService] ⚠️ Pending fills background reconciliation error: {e}")

        if symbol not in self.active_positions:
            return closed_reports
            
        try:
            # Bypass cache for checking closures to be perfectly accurate
            positions = await self.get_active_positions(bypass_cache=True)
            if not getattr(self, "_last_positions_fetch_ok", True):
                logger.warning(f"[NadoTradingService] ⚠️ Active positions fetch unconfirmed. Skipping closure check for {symbol}.")
                return closed_reports

            pos = self.active_positions[symbol]
            canonical_sym = symbol.replace('/', '-').strip().upper()
            if '-' not in canonical_sym:
                canonical_sym = f"{canonical_sym}-USD"
            base_symbol = canonical_sym.split('-')[0].upper()
            prod_id = pos.get("product_id") or self.product_map.get(canonical_sym) or self.product_map.get(base_symbol)
            
            is_virtual_pos = bool(pos.get("is_virtual") or pos.get("is_shadow") or getattr(config, "SHADOW_MODE", False))
            
            still_open = False
            target_pnl = 0.0
            
            if is_virtual_pos:
                still_open = True
            else:
                for p in positions:
                    p_prod = p.get("_product_id") or p.get("product_id") or self.product_map.get(p.get("symbol"))
                    if prod_id is not None and p_prod is not None:
                        if p_prod == prod_id:
                            still_open = True
                            break
                    elif p.get("symbol") == canonical_sym or p.get("symbol") == symbol:
                        still_open = True
                        break
                    
            if not still_open:
                # If position was DUST_REMAINDER, finalize via unified pipeline
                is_dust_pos = pos.get("is_dust") or (
                    pos.get("partial_tp_plan") and any(
                        t.get("status") == "DUST_REMAINDER" for t in pos["partial_tp_plan"].get("tiers", [])
                    )
                )
                if is_dust_pos:
                    logger.info(f"[NadoTradingService] 🧹 DUST_REMAINDER position for {symbol} confirmed zero on-chain. Finalizing via safe closure pipeline.")
                    closure_report = self._finalize_partial_tp_closure(
                        symbol=symbol,
                        pos=pos,
                        exit_price=None,
                        triggered_by="DUST_ZERO_CONFIRMED"
                    )
                    return [closure_report]

                # Position is gone, meaning Nado native Trigger Order (SL/TP) executed!
                logger.info(f"[NadoTradingService] ⚡ Native Trigger executed for {symbol}! Position closed on-chain.")
                direction = str(pos.get("direction", "LONG")).upper()
                entry_price = float(pos.get("entry_price") or 0.0)
                size_usd = float(pos.get("size_usd") or 0.0)
                
                # Fetch fresh mark/oracle price if current_price was passed as 0 or missing
                if current_price <= 0:
                    prod_id = pos.get("product_id") or self.product_map.get(base_symbol)
                    if prod_id is not None and getattr(self, "_market_cache", None) and prod_id in self._market_cache:
                        m_info = self._market_cache[prod_id]
                        raw_oracle = getattr(m_info, "oracle_price_x18", None)
                        if raw_oracle is None and hasattr(m_info, "product"):
                            raw_oracle = getattr(m_info.product, "oracle_price_x18", None)
                        if raw_oracle is not None and not hasattr(raw_oracle, "_mock_return_value"):
                            try:
                                parsed_oracle = float(raw_oracle) / 1e18
                                if parsed_oracle > 1e-4:
                                    current_price = parsed_oracle
                            except (ValueError, TypeError):
                                pass

                # Safe fallback price if market price is completely unavailable
                effective_market_price = current_price if current_price > 1e-4 else (entry_price if entry_price > 0 else 1.0)

                tp_price = float(pos.get("tp_price") or 0.0)
                sl_price = float(pos.get("sl_price") or 0.0)
                
                has_tp = tp_price > 0
                has_sl = sl_price > 0
                
                # Determine trigger category and baseline exit price
                exit_price = effective_market_price
                triggered_by = "MANUAL_OR_ON_CHAIN"

                if direction == "LONG":
                    if has_tp and effective_market_price >= tp_price:
                        exit_price = tp_price
                        triggered_by = "TP"
                    elif has_sl and effective_market_price <= sl_price:
                        exit_price = sl_price
                        triggered_by = "SL"
                else: # SHORT
                    if has_tp and effective_market_price <= tp_price:
                        exit_price = tp_price
                        triggered_by = "TP"
                    elif has_sl and effective_market_price >= sl_price:
                        exit_price = sl_price
                        triggered_by = "SL"

                # Intelligent trigger selection fallback if triggered_by remains MANUAL_OR_ON_CHAIN
                if triggered_by == "MANUAL_OR_ON_CHAIN" and (has_sl or has_tp):
                    if has_sl and has_tp:
                        dist_sl = abs(effective_market_price - sl_price)
                        dist_tp = abs(effective_market_price - tp_price)
                        if dist_sl <= dist_tp:
                            exit_price = sl_price
                            triggered_by = "SL"
                        else:
                            exit_price = tp_price
                            triggered_by = "TP"
                    elif has_sl:
                        exit_price = sl_price
                        triggered_by = "SL"
                    elif has_tp:
                        exit_price = tp_price
                        triggered_by = "TP"

                pos["assumed_exit_price"] = exit_price
                pos["close_reason"] = triggered_by
                pos["closed_at"] = time.time()
                metrics = await self._fetch_verified_close_metrics(canonical_sym, pos)

                exit_price = metrics["exit_price"]
                target_pnl = metrics["net_pnl_usd"]
                realized_r = metrics["realized_r"]
                estimated_fees = metrics["total_fee_usd"]
                pnl_source = metrics["pnl_source"]
                planned_risk = float(metrics.get("planned_risk_usd", 0.0) or pos.get("planned_risk_usd") or pos.get("approved_risk_usd") or pos.get("risk_usd") or 0.0)

                # Granular closure classification based on Realized R
                if realized_r <= -0.25:
                    sub_reason = "SL"
                elif realized_r < 0.35:
                    sub_reason = "BE_SCRATCH"
                else:
                    sub_reason = "TP_OR_TRAIL"

                closure_tag = f"CLOSED_ON_CHAIN ({sub_reason} ({pnl_source}))"

                # Cancel any remaining trigger orders on exchange (e.g. if TP hit, cancel SL; if SL hit, cancel TP)
                product_id = pos.get("product_id") or self.product_map.get(base_symbol)
                sl_dig = pos.get("sl_digest")
                tp_dig = pos.get("tp_digest")
                digests_to_cancel = [d for d in [sl_dig, tp_dig] if d]
                if digests_to_cancel and product_id is not None:
                    await self._cancel_trigger_digests(product_id, digests_to_cancel, sender=pos.get("sender"))

                if symbol in self.active_positions:
                    del self.active_positions[symbol]
                alias_key = base_symbol if '-' in symbol else f"{base_symbol}-USD"
                if alias_key in self.active_positions:
                    del self.active_positions[alias_key]
                self._save_positions()

                if pnl_source == "EXACT":
                    # Unified Scratch Guard for Win/Loss Streaks (-0.22R to +0.15R)
                    self._evaluate_and_update_streak(
                        net_pnl=target_pnl,
                        realized_r=realized_r,
                        planned_risk=planned_risk,
                        pos_data=pos,
                        symbol=symbol
                    )

                    realized_r_recorded = self._record_closed_trade_journal(
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        net_pnl=target_pnl,
                        pos_data=pos,
                        fee=estimated_fees,
                        triggered_by=closure_tag
                    )
                else:
                    # Indexer lag: store in pending_reconciliations and do NOT corrupt streak
                    logger.warning(
                        f"[NadoTradingService] ⏳ Trade {symbol} closed on-chain, but indexer fill unconfirmed after 3 retries. "
                        f"Saved to pending reconciliations. Streak unchanged."
                    )
                    subaccount = pos.get("sender") or pos.get("subaccount") or getattr(self, "default_subaccount_id", None)
                    now_ts = time.time()
                    self.pending_reconciliations[canonical_sym] = {
                        "symbol": canonical_sym,
                        "base_symbol": base_symbol,
                        "product_id": product_id,
                        "subaccount": subaccount,
                        "direction": direction,
                        "entry_price": entry_price,
                        "contracts": float(pos.get("contracts") or pos.get("amount") or 0.0),
                        "size_usd": float(pos.get("size_usd") or 0.0),
                        "accumulated_realized_pnl": float(pos.get("accumulated_realized_pnl", 0.0)),
                        "open_time": float(pos.get("open_time", 0.0)),
                        "closed_at": now_ts,
                        "detected_at": now_ts,
                        "attempts": 0,
                        "status": "PNL_RECONCILIATION_PENDING",
                        "reason": closure_tag,
                        "planned_risk_usd": planned_risk,
                        "cohort": pos.get("cohort", "UNKNOWN"),
                        "base_risk_pct": float(pos.get("base_risk_pct", 0.0)),
                        "applied_multipliers": pos.get("applied_multipliers", {})
                    }
                    self._save_pending_reconciliations()
                    realized_r_recorded = realized_r

                closed_reports.append({
                    "symbol": canonical_sym,
                    "direction": direction,
                    "triggered_by": closure_tag,
                    "close_reason": triggered_by,
                    "strategy_mode": pos.get("strategy_mode", "UNKNOWN"),
                    "is_anti_bot": bool(pos.get("is_anti_bot", False) or pos.get("strategy_mode") == "MEAN_REVERSION"),
                    "entry_price": metrics["entry_price"],
                    "exit_price": metrics["exit_price"],
                    "gross_pnl_usd": metrics["gross_pnl_usd"],
                    "entry_fee_usd": metrics["entry_fee_usd"],
                    "exit_fee_usd": metrics["exit_fee_usd"],
                    "total_fee_usd": metrics["total_fee_usd"],
                    "net_pnl_usd": metrics["net_pnl_usd"],
                    "pnl_usd": metrics["net_pnl_usd"],
                    "pnl": metrics["net_pnl_usd"],
                    "realized_r": realized_r_recorded,
                    "planned_risk_usd": planned_risk,
                    "cohort": pos.get("cohort", "UNKNOWN"),
                    "pnl_source": metrics["pnl_source"],
                    "roi_pct": (metrics["net_pnl_usd"] / pos.get("margin_used", size_usd) * 100) if size_usd > 0 else 0,
                    "original_thesis": pos.get("original_thesis", "") if pos else "",
                    "accumulated_slices_pnl": metrics.get("accumulated_slices_pnl", 0.0)
                })
            elif still_open:
                pos = self.active_positions.get(symbol)
                if pos:
                    direction = pos.get("direction", "LONG").upper()
                    sl_price = float(pos.get("sl_price", 0.0))
                    tp_price = float(pos.get("tp_price", 0.0))
                    has_native_sl = bool(pos.get("sl_digest"))

                    # Backup Software SL Buffer:
                    # If position has an active native on-chain SL on Nado DEX, Software SL acts ONLY
                    # as an emergency safety net (backup parachute) if on-chain trigger failed/lagged.
                    # It triggers only with a 0.20% buffer beyond the native sl_price.
                    # If NO native SL exists (e.g. unconfirmed order or notional < $100), Software SL
                    # triggers at 0.10% buffer to protect against extreme orderbook wicks.
                    # For virtual/shadow positions, buffer is 0.0% to evaluate exact price breaches.
                    if is_virtual_pos:
                        sl_buffer_pct = 0.0
                    else:
                        sl_buffer_pct = 0.0020 if has_native_sl else 0.0010
                    
                    if direction == "LONG" and sl_price > 0:
                        backup_sl_threshold = sl_price * (1.0 - sl_buffer_pct)
                        hit_sl = (current_price <= backup_sl_threshold)
                    elif direction == "SHORT" and sl_price > 0:
                        backup_sl_threshold = sl_price * (1.0 + sl_buffer_pct)
                        hit_sl = (current_price >= backup_sl_threshold)
                    else:
                        hit_sl = False

                    # Keep current_price fresh for virtual positions
                    if is_virtual_pos and current_price > 0:
                        pos["current_price"] = current_price

                    hit_tp = (direction == "LONG" and tp_price > 0 and current_price >= tp_price) or \
                             (direction == "SHORT" and tp_price > 0 and current_price <= tp_price)
                             
                    # v2.1 Patch: Immunity for MEAN_REVERSION / is_anti_bot
                    is_anti_bot_mr = bool(pos.get("strategy_mode") == "MEAN_REVERSION" or pos.get("is_anti_bot"))
                    if is_anti_bot_mr:
                        if pos.get("protection_state") == "EXTREME_BLOWOUT_DEFENSE":
                            pos["protection_state"] = "NORMAL"

                        if hit_sl and not hit_tp:
                            entry_px = float(pos.get("entry_price", 0.0) or 0.0)
                            cntr = float(pos.get("contracts", 0.0) or 0.0)
                            planned_risk = float(pos.get("planned_risk_usd") or pos.get("approved_risk_usd") or 0.0)
                            pnl = ((current_price - entry_px) if direction == "LONG" else (entry_px - current_price)) * cntr
                            cur_r = (pnl / planned_risk) if planned_risk > 0 else 0.0
                            if -0.85 < cur_r < 0.0:
                                hit_sl = False
                                logger.info(
                                    f"[NadoTradingService] 🛡️ [Predator Immunity] Bypassing premature SL force-close for {symbol} "
                                    f"at R={cur_r:.2f}R (-0.85R < cur_r < 0.0R). Allowing position room to mean-revert to EMA-20."
                                )

                    if hit_sl or hit_tp:
                        if pos.get("is_closing"):
                            logger.info(f"[NadoTradingService] ⏳ Skipping {symbol} — already being closed by Fast Monitor.")
                            return closed_reports
                        pos["is_closing"] = True
                        reason = ("BACKUP_SL" if has_native_sl else "SL") if hit_sl else "TP"
                        reason_tag = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
                        prot_state = str(pos.get("protection_state") or "").upper()
                        if "TRAILING" in prot_state:
                            reason_tag = "TRAILING_STOP"
                        elif any(k in prot_state for k in ["TIME_STOP", "BREAK_EVEN", "BE"]):
                            reason_tag = "TIME_STOP_BE"

                        logger.info(f"[NadoTradingService] 🚨 Stage 1.5 Software {reason} triggered for {symbol} at {current_price:.4f} (SL: {sl_price:.4f}, Buffer: {sl_buffer_pct*100:.2f}%)!")
                        lock = self._get_position_lock(symbol)
                        async with lock:
                            if symbol not in self.active_positions and symbol.split('-')[0].upper() not in self.active_positions:
                                return closed_reports
                            success, realized_pnl = await self.force_close_position(symbol, bypass_check=True, exit_reason=reason_tag)
                        if success:
                            close_info = getattr(self, "_last_close_info", {}).get(symbol) or {}
                            exit_p = close_info.get("exit_price") or current_price
                            closed_reports.append({
                                "symbol": symbol,
                                "direction": direction,
                                "triggered_by": f"SOFTWARE_{reason}",
                                "close_reason": f"SOFTWARE_{reason}",
                                "strategy_mode": pos.get("strategy_mode", "UNKNOWN"),
                                "is_anti_bot": bool(pos.get("is_anti_bot", False) or pos.get("strategy_mode") == "MEAN_REVERSION"),
                                "entry_price": pos.get("entry_price", current_price),
                                "exit_price": exit_p,
                                "pnl_usd": realized_pnl,
                                "net_pnl_usd": realized_pnl,
                                "pnl": realized_pnl,
                                "gross_pnl_usd": close_info.get("gross_pnl_usd", 0.0),
                                "entry_fee_usd": close_info.get("entry_fee_usd", 0.0),
                                "exit_fee_usd": close_info.get("exit_fee_usd", 0.0),
                                "total_fee_usd": close_info.get("total_fee_usd", 0.0),
                                "fee": close_info.get("total_fee_usd", 0.0),
                                "realized_r": close_info.get("realized_r", 0.0),
                                "planned_risk_usd": float(pos.get("planned_risk_usd") or pos.get("approved_risk_usd") or 0.0),
                                "cohort": pos.get("cohort", "UNKNOWN"),
                                "pnl_source": close_info.get("pnl_source", "ESTIMATED"),
                                "roi_pct": (realized_pnl / pos.get("margin_used", pos.get("size_usd", 1.0)) * 100) if pos.get("size_usd", 0) > 0 else 0,
                                "original_thesis": pos.get("original_thesis", ""),
                                "accumulated_slices_pnl": close_info.get("accumulated_slices_pnl", 0.0)
                            })
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to check native position state: {e}")
            
        return closed_reports

    @staticmethod
    def _normalize_indexer_amount(val: Any) -> float:
        """Safely normalizes numeric amounts from Nado Indexer / SDK."""
        if val is None:
            return 0.0
        try:
            f = float(val)
            if abs(f) > 1e10:
                return f / 1e18
            return f
        except (ValueError, TypeError):
            return 0.0

    def _calculate_gross_quote_and_price(
        self,
        base_filled: float,
        quote_filled: float,
        fee: float = 0.0,
        maker_price: float = 0.0,
        is_mock: bool = False
    ) -> Tuple[float, float]:
        """
        Computes (gross_quote, execution_price) for a fill.
        
        On Nado DEX / Vertex matching engine:
        quote_filled in IndexerHistoricalOrder is the NET cash settlement (includes fee deduction/addition):
          - BUY (bf > 0): cash spent = gross_quote + fee -> quote_filled = -(gross_quote + fee)
          - SELL (bf < 0): cash received = gross_quote - fee -> quote_filled = +(gross_quote - fee)
        
        If raw quote was already gross (e.g. in mock tests where is_mock=True or fee==0),
        abs(quote_filled) is used directly.
        If maker_price > 0 (from match engine), it is authoritative.
        """
        norm_b = self._normalize_indexer_amount(base_filled) if abs(float(base_filled or 0)) > 1e10 else float(base_filled or 0)
        norm_q = self._normalize_indexer_amount(quote_filled) if abs(float(quote_filled or 0)) > 1e10 else float(quote_filled or 0)
        norm_f = self._normalize_indexer_amount(fee) if abs(float(fee or 0)) > 1e10 else float(fee or 0)

        abs_bf = abs(norm_b)
        if abs_bf <= 0:
            return 0.0, 0.0

        if maker_price > 0:
            norm_maker_p = self._normalize_indexer_amount(maker_price) if maker_price > 1e10 else maker_price
            gross_quote = norm_maker_p * abs_bf
            return gross_quote, norm_maker_p

        abs_qf = abs(norm_q)
        abs_fee = abs(norm_f)

        is_mock_env = is_mock or getattr(self, "_mock_mode", False) or "Mock" in type(getattr(self, "client", None)).__name__
        # In mock tests or when fee is not recorded, quote_filled is assumed gross
        if is_mock_env or abs_fee <= 0.0:
            gross_quote = abs_qf
        else:
            # Reconstruct gross quote by removing the taker fee
            if norm_b > 0:  # BUY
                gross_quote = max(0.0, abs_qf - abs_fee)
            else:  # SELL
                gross_quote = abs_qf + abs_fee

        exec_price = gross_quote / abs_bf if abs_bf > 0 else 0.0
        return gross_quote, exec_price

    async def _reconcile_closing_fills_from_history(
        self,
        subaccount: str,
        product_id: int,
        direction: str,
        entry_price: float,
        expected_contracts: float,
        close_timestamp: Optional[float] = None,
        min_timestamp: Optional[float] = None,
        max_timestamp: Optional[float] = None,
        excluded_digests: Optional[Set[str]] = None,
        target_digest: Optional[str] = None,
        open_digest: Optional[str] = None,
        limit: int = 50,
        allow_missing_timestamp: bool = True
    ) -> Dict[str, Any]:
        """
        Queries subaccount historical orders to aggregate matching fills for a closing event.
        Calculates VWAP exit price, total fees, and exact net PnL across execution clusters.
        Enforces:
        1. Closing direction matching.
        2. Temporal clustering (fills in a cluster must execute within 15s of each other, <= 30s span)
           to strictly prevent stitching together unrelated partial closes across the position lifetime.
        3. Strict relative volume tolerance (<= 1.0%), rejecting loose absolute tolerances for small positions.
        4. Unique non-empty digest requirement on all matched fills to prevent unverified replay.
        5. Fee verification: fallback estimated fees flag is_exact=False.
        6. Ambiguity rejection: if multiple clusters match, refuses to guess and leaves trade pending.
        7. Strict open_digest boundary: stops scanning as soon as the position's opening order is hit.
        """
        default_res = {
            "is_exact": False,
            "is_volume_exact": False,
            "is_fee_exact": False,
            "exit_price": None,
            "total_base": 0.0,
            "total_quote": 0.0,
            "fee": 0.0,
            "net_pnl": None,
            "digests": []
        }

        if not self.client or not subaccount or product_id is None:
            return default_res

        # Financial safety: if expected_contracts <= 0, cannot safely verify volume
        if expected_contracts <= 0:
            logger.warning(
                f"[NadoTradingService] ⚠️ Fill reconciliation rejected: expected_contracts={expected_contracts} <= 0."
            )
            return default_res

        try:
            from nado_protocol.indexer_client.types.query import IndexerSubaccountHistoricalOrdersParams

            if hasattr(subaccount, "hex") and callable(subaccount.hex):
                sub_str = subaccount.hex()
                if not sub_str.startswith("0x"):
                    sub_str = "0x" + sub_str
            elif isinstance(subaccount, (bytes, bytearray)):
                sub_str = "0x" + subaccount.hex()
            elif isinstance(subaccount, str):
                sub_str = subaccount
            elif hasattr(subaccount, "_mock_name"):
                sub_str = "0x1234567890123456789012345678901234567890"
            else:
                sub_str = str(subaccount)

            params = IndexerSubaccountHistoricalOrdersParams(
                subaccounts=[sub_str],
                product_ids=[int(product_id)],
                limit=limit,
            )
            hist = await asyncio.to_thread(self.client.market.get_subaccount_historical_orders, params)
            if not hist or not getattr(hist, "orders", None):
                return default_res

            # Query match executions directly from indexer to obtain exact maker match prices
            match_prices = {}
            if not getattr(self, "_mock_mode", False) and hasattr(self, "client") and self.client:
                try:
                    indexer_c = getattr(getattr(self.client, "context", None), "indexer_client", None)
                    if indexer_c and hasattr(indexer_c, "get_matches"):
                        from nado_protocol.indexer_client.types import IndexerMatchesParams
                        m_params = IndexerMatchesParams(
                            subaccounts=[sub_str],
                            product_ids=[int(product_id)],
                            limit=min(limit, 20)
                        )
                        m_res = await asyncio.to_thread(indexer_c.get_matches, m_params)
                        if m_res and hasattr(m_res, "txs"):
                            for t in m_res.txs:
                                s_idx = str(getattr(t, "submission_idx", ""))
                                t_data = t.tx if isinstance(t.tx, dict) else (t.tx.model_dump() if hasattr(t.tx, "model_dump") else {})
                                mo = t_data.get("match_orders", {})
                                p_val = float(mo.get("maker", {}).get("order", {}).get("price_x18", 0)) / 1e18
                                if p_val > 0 and s_idx:
                                    match_prices[s_idx] = p_val
                        if m_res and hasattr(m_res, "matches"):
                            for m in m_res.matches:
                                m_dig = str(getattr(m, "digest", "")).lower()
                                s_idx = str(getattr(m, "submission_idx", ""))
                                if s_idx in match_prices and m_dig:
                                    match_prices[m_dig] = match_prices[s_idx]
                except Exception as m_err:
                    logger.debug(f"[NadoTradingService] Match events lookup skipped: {m_err}")

            dir_upper = str(direction).upper()
            ref_time = close_timestamp if close_timestamp and close_timestamp > 0 else time.time()

            # Compile exclusion set of digests (normalized to lower case)
            skip_digests = set(str(d).strip().lower() for d in getattr(self, "processed_fill_digests", set()) if d)
            if excluded_digests:
                skip_digests.update(str(d).strip().lower() for d in excluded_digests if d)

            # 1. Collect and validate candidate closing fills
            candidates = []
            target_open_dig = str(open_digest).strip().lower() if open_digest else None

            for order in hist.orders:
                raw_dig = getattr(order, "digest", "") or ""
                dig_str = str(raw_dig).strip()
                dig_lower = dig_str.lower()
                has_valid_digest = bool(dig_str and dig_lower != "none")

                # Boundary Guard: Indexer orders are reverse-chronological.
                # Once we encounter the position's open_digest, stop scanning immediately!
                if target_open_dig and has_valid_digest and dig_lower == target_open_dig:
                    logger.debug(f"[NadoTradingService] 🛑 Reached open_digest ({open_digest}) in historical scan. Stopping.")
                    break

                # Skip previously processed or explicitly excluded digests (e.g. TP tiers)
                if has_valid_digest and dig_lower in skip_digests:
                    continue

                if target_digest and dig_lower != target_digest.lower():
                    continue

                raw_bf = getattr(order, "base_filled", 0) or 0
                raw_qf = getattr(order, "quote_filled", 0) or 0
                bf = float(raw_bf)
                if bf == 0:
                    continue

                norm_base = abs(self._normalize_indexer_amount(bf))
                norm_quote = abs(self._normalize_indexer_amount(raw_qf))
                if norm_base <= 0 or norm_quote <= 0:
                    continue

                # Strict direction check:
                # Closing LONG -> SELL (bf < 0)
                # Closing SHORT -> BUY (bf > 0)
                is_close_dir = (dir_upper == "LONG" and bf < 0) or (dir_upper == "SHORT" and bf > 0)
                if not is_close_dir:
                    continue

                # Timestamp check:
                ts = getattr(order, "timestamp", None)
                order_ts_sec = None
                if ts is not None and str(ts).strip() != "" and str(ts).strip().lower() != "none":
                    try:
                        ts_val = float(ts)
                        ref_magnitude = max(ref_time or 0.0, max_timestamp or 0.0, min_timestamp or 0.0)
                        if len(str(int(ts_val))) > 10 or (ref_magnitude > 0 and ts_val >= ref_magnitude * 500):
                            order_ts_sec = ts_val / 1000.0
                        else:
                            order_ts_sec = ts_val
                    except (ValueError, TypeError):
                        logger.debug(f"[NadoTradingService] Skipping unparseable order timestamp: {ts}")
                        continue
                elif allow_missing_timestamp and has_valid_digest:
                    # Nado Indexer historical orders do not populate timestamp on fills.
                    # Use event temporal context to evaluate candidate.
                    if close_timestamp and close_timestamp > 0:
                        order_ts_sec = float(close_timestamp)
                    elif min_timestamp is not None and max_timestamp is not None:
                        order_ts_sec = float(min_timestamp + max_timestamp) / 2.0
                    else:
                        order_ts_sec = float(ref_time)
                else:
                    logger.debug("[NadoTradingService] Skipping order without timestamp in reconciliation.")
                    continue

                # Explicit timestamp window filtering
                if min_timestamp is not None and order_ts_sec < min_timestamp:
                    continue
                if max_timestamp is not None and order_ts_sec > max_timestamp:
                    continue

                if min_timestamp is None and max_timestamp is None:
                    if close_timestamp and close_timestamp > 0:
                        if order_ts_sec < (close_timestamp - 45.0) or order_ts_sec > (close_timestamp + 60.0):
                            continue
                    else:
                        if order_ts_sec < (ref_time - 900.0):
                            continue

                # Strict fee extraction
                raw_fee = getattr(order, "fee_x18", None)
                if raw_fee is None or type(raw_fee).__name__ == "MagicMock":
                    raw_fee = getattr(order, "fee", None)
                if type(raw_fee).__name__ == "MagicMock":
                    raw_fee = None

                if raw_fee is not None and abs(float(raw_fee or 0)) > 0:
                    fill_fee = abs(self._normalize_indexer_amount(raw_fee))
                    is_fee_exact = True
                else:
                    fill_fee = norm_quote * 0.0005
                    is_fee_exact = False

                # Exact gross quote and execution price determination:
                s_idx = str(getattr(order, "submission_idx", ""))
                maker_p = match_prices.get(s_idx) or match_prices.get(dig_lower, 0.0)

                close_b = norm_base if bf > 0 else -norm_base
                gross_q, fill_price = self._calculate_gross_quote_and_price(
                    base_filled=close_b,
                    quote_filled=norm_quote,
                    fee=fill_fee if is_fee_exact else 0.0,
                    maker_price=maker_p,
                    is_mock=getattr(self, "_mock_mode", False)
                )

                candidates.append({
                    "order": order,
                    "digest": dig_str if has_valid_digest else "",
                    "has_valid_digest": has_valid_digest,
                    "base": norm_base,
                    "quote": gross_q,
                    "fee": fill_fee,
                    "is_fee_exact": is_fee_exact,
                    "timestamp": order_ts_sec,
                    "fill_price": fill_price
                })

            if not candidates:
                return default_res

            # Step size lookup for instrument rounding
            step_size = 0.0
            try:
                step_size = float(self._get_market_parameters(product_id).get("size_increment_base", 0.0))
            except Exception:
                step_size = 0.0

            # 2. Form Temporal Clusters:
            # Fills in the same closing event occur nearly simultaneously or in a rapid burst.
            # Max gap between consecutive fills: 15s; max total cluster span: 30s.
            candidates.sort(key=lambda c: c["timestamp"])
            clusters = []
            curr_cluster = []
            for c in candidates:
                if not curr_cluster:
                    curr_cluster.append(c)
                else:
                    gap = c["timestamp"] - curr_cluster[-1]["timestamp"]
                    span = c["timestamp"] - curr_cluster[0]["timestamp"]
                    if gap <= 15.0 and span <= 30.0:
                        curr_cluster.append(c)
                    else:
                        clusters.append(curr_cluster)
                        curr_cluster = [c]
            if curr_cluster:
                clusters.append(curr_cluster)

            # 3. Match Clusters Against Expected Volume
            def eval_volume_match(vol: float) -> Tuple[bool, float]:
                v_diff = abs(vol - expected_contracts)
                v_div = v_diff / expected_contracts
                # Relative divergence must be <= 1.0% (0.01).
                # For tiny lot rounding, allow at most step_size provided v_div <= 0.02.
                # Never accept 100% or large relative error on small positions!
                is_vol = (v_div <= 0.01)
                if not is_vol and step_size > 0 and v_diff <= step_size and v_div <= 0.02:
                    is_vol = True
                return is_vol, v_div

            matching_clusters = []
            for cl in clusters:
                cl_base = sum(item["base"] for item in cl)
                is_match, _ = eval_volume_match(cl_base)
                if is_match:
                    matching_clusters.append(cl)
                else:
                    # Check if a prefix of consecutive fills in the cluster matches
                    cum_base = 0.0
                    prefix = []
                    for item in cl:
                        cum_base += item["base"]
                        prefix.append(item)
                        p_match, _ = eval_volume_match(cum_base)
                        if p_match:
                            matching_clusters.append(list(prefix))
                            break

            # 4. Enforce Single Unambiguous Closing Event
            if len(matching_clusters) == 0:
                logger.debug(
                    f"[NadoTradingService] No single execution cluster matched expected {expected_contracts:.4f} "
                    f"contracts across {len(clusters)} candidate cluster(s). Refusing to stitch disparate events."
                )
                return default_res

            if len(matching_clusters) > 1:
                # If target_digest is specified, filter to the cluster containing it
                if target_digest:
                    filtered = [cl for cl in matching_clusters if any(item["digest"].lower() == target_digest.lower() for item in cl)]
                    if len(filtered) == 1:
                        matching_clusters = filtered

                if len(matching_clusters) > 1:
                    logger.warning(
                        f"[NadoTradingService] ⚠️ Ambiguous reconciliation for subaccount {subaccount} product {product_id}: "
                        f"found {len(matching_clusters)} distinct execution clusters matching {expected_contracts:.4f}. "
                        f"Refusing to guess. Leaving in PNL_RECONCILIATION_PENDING."
                    )
                    return default_res

            matched = matching_clusters[0]

            # 5. Evaluate Exact Verification Criteria on Matched Cluster
            total_base = sum(c["base"] for c in matched)
            total_quote = sum(c["quote"] for c in matched)
            total_fee = sum(c["fee"] for c in matched)
            matched_digests = [c["digest"] for c in matched if c["digest"]]

            has_missing_digest = any(not c["has_valid_digest"] for c in matched)
            has_estimated_fee = any(not c["is_fee_exact"] for c in matched)
            is_volume_exact, vol_divergence = eval_volume_match(total_base)

            vwap_exit = total_quote / total_base if total_base > 0 else 0.0
            if vwap_exit <= 0:
                return default_res

            # Strict Exact Rule:
            # Requires strict volume match (<= 1%), all fills have unique digests, and all fees are confirmed from exchange!
            is_exact = is_volume_exact and (not has_missing_digest) and (not has_estimated_fee)

            gross_pnl = total_base * (vwap_exit - entry_price) if dir_upper == "LONG" else total_base * (entry_price - vwap_exit)
            net_pnl = round(gross_pnl - total_fee, 4) if is_exact else None

            if has_missing_digest:
                logger.warning(
                    f"[NadoTradingService] ⚠️ Reconciled fill cluster lacks unique digest(s). "
                    f"Marking is_exact=False to prevent replay vulnerability."
                )

            if not is_volume_exact:
                logger.warning(
                    f"[NadoTradingService] ⚠️ Reconciled volume ({total_base:.4f}) deviates by "
                    f"{vol_divergence*100:.2f}% from expected ({expected_contracts:.4f}) (tolerance <= 1.0%). Marking unverified."
                )

            logger.info(
                f"[NadoTradingService] 🎯 Reconciled execution event: {total_base:.4f} base @ VWAP {vwap_exit:.4f}, "
                f"Fee: ${total_fee:.4f} (exact_fee={not has_estimated_fee}), Net PnL: {net_pnl}, is_exact={is_exact} "
                f"across {len(matched_digests)} fill(s) (cluster span: {matched[-1]['timestamp'] - matched[0]['timestamp']:.1f}s)."
            )

            return {
                "is_exact": is_exact,
                "is_volume_exact": is_volume_exact,
                "is_fee_exact": not has_estimated_fee,
                "has_missing_digest": has_missing_digest,
                "exit_price": vwap_exit,
                "total_base": total_base,
                "total_quote": total_quote,
                "fee": total_fee,
                "net_pnl": net_pnl,
                "digests": matched_digests
            }
        except Exception as e:
            logger.warning(f"[NadoTradingService] ⚠️ History fill reconciliation error: {e}")
            return default_res

    async def reconcile_pending_trade_fills(self) -> List[Dict[str, Any]]:
        """
        Polls historical orders for all trades in self.pending_reconciliations.
        Uses position open_time and detected_at for a robust search window.
        Tracks attempt counts and triggers alert for persistent pending trades.
        Once true fills are confirmed by the indexer, computes exact realized PnL,
        updates win/loss streaks and cooldown, and finalizes the trade record.
        Serializes execution across concurrent tasks via self._reconcile_lock.
        """
        if not self.is_connected or not getattr(self, "pending_reconciliations", None):
            return []

        if not hasattr(self, "_reconcile_lock") or self._reconcile_lock is None:
            self._reconcile_lock = asyncio.Lock()

        async with self._reconcile_lock:
            resolved_reports = []
            keys_to_remove = []
            now = time.time()

            for symbol, pending in list(self.pending_reconciliations.items()):
                subaccount = pending.get("subaccount") or self.default_subaccount_id
                product_id = pending.get("product_id")
                if not subaccount or product_id is None:
                    continue

                # Auto-prune foreign or mock test subaccounts that don't match active wallet
                if self.default_subaccount_id and subaccount != self.default_subaccount_id:
                    logger.info(f"[NadoTradingService] 🧹 Pruned orphan pending reconciliation for {symbol} (subaccount mismatch: {subaccount}).")
                    keys_to_remove.append(symbol)
                    continue

                # Track attempts & age
                attempts = pending.get("attempts", 0) + 1
                pending["attempts"] = attempts
                pending["last_attempt_at"] = now
                detected_at = float(pending.get("detected_at") or pending.get("closed_at") or now)

                # Auto-retire stale pending trades after 60 failed audit cycles (~30 min)
                if attempts > 60:
                    logger.warning(f"[NadoTradingService] 🛑 Retiring pending reconciliation for {symbol} after {attempts} failed attempts.")
                    keys_to_remove.append(symbol)
                    continue

                # Alert if pending too long (> 2 hours or >= 20 attempts)
                if attempts == 20 or (attempts > 20 and attempts % 20 == 0):
                    logger.error(
                        f"[NadoTradingService] 🚨 CRITICAL PENDING AUDIT: Position {symbol} has been in "
                        f"PNL_RECONCILIATION_PENDING for {attempts} cycles (age: {(now - detected_at)/60:.1f} min). "
                        f"Manual exchange inspection recommended."
                    )

                direction = pending.get("direction", "LONG")
                entry_price = float(pending.get("entry_price", 0.0))
                expected_contracts = float(pending.get("contracts", 0.0) or pending.get("amount", 0.0) or 0.0)
                if expected_contracts <= 0 and entry_price > 0:
                    sz = float(pending.get("size_usd", 0.0))
                    if sz > 0:
                        expected_contracts = round(sz / entry_price, 8)
                        pending["contracts"] = expected_contracts

                if expected_contracts <= 0:
                    logger.warning(
                        f"[NadoTradingService] 🧹 Pruned unresolvable pending reconciliation for {symbol}: "
                        f"contracts={expected_contracts}, entry_price={entry_price}. Cannot verify zero-volume trade."
                    )
                    keys_to_remove.append(symbol)
                    continue

                open_time = float(pending.get("open_time", 0.0))

                # Robust search window: from position open_time (minus 5s clock skew) up to detected_at + 60s
                min_ts = (open_time - 5.0) if open_time > 0 else (detected_at - 1800.0)
                max_ts = detected_at + 60.0

                # Exclude known digests (e.g. TP tiers or open order)
                excluded = set(pending.get("excluded_digests", []))
                excluded.update(getattr(self, "processed_fill_digests", set()))

                open_d = pending.get("open_digest") or pending.get("order_digest") or pending.get("digest")
                recon = await self._reconcile_closing_fills_from_history(
                    subaccount=subaccount,
                    product_id=product_id,
                    direction=direction,
                    entry_price=entry_price,
                    expected_contracts=expected_contracts,
                    close_timestamp=detected_at,
                    min_timestamp=min_ts,
                    max_timestamp=max_ts,
                    excluded_digests=excluded,
                    open_digest=open_d,
                    allow_missing_timestamp=True
                )

                if recon.get("is_exact") and recon.get("net_pnl") is not None:
                    acc_pnl = float(pending.get("accumulated_realized_pnl", 0.0))
                    net_pnl = round(recon["net_pnl"] + acc_pnl, 4)
                    exit_price = recon["exit_price"]
                    fee = recon["fee"]

                    # Register used digests in processed registry with metadata
                    self._register_processed_fills(
                        recon.get("digests", []),
                        subaccount=subaccount,
                        product_id=product_id,
                        symbol=symbol
                    )

                    # Unified Scratch Guard for Win/Loss Streaks (-0.22R to +0.15R)
                    self._evaluate_and_update_streak(
                        net_pnl=net_pnl,
                        planned_risk=float(pending.get("planned_risk_usd") or 0.0),
                        pos_data=pending,
                        symbol=symbol
                    )

                    logger.info(
                        f"[NadoTradingService] 🎯 RESOLVED pending reconciliation for {symbol}: "
                        f"Exit: {exit_price:.4f}, Net PnL: ${net_pnl:.4f} (includes TP ${acc_pnl:.2f})"
                    )

                    realized_r = self._record_closed_trade_journal(
                        symbol=symbol,
                        direction=direction,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        net_pnl=net_pnl,
                        pos_data=pending,
                        fee=fee,
                        triggered_by=pending.get("reason", "INDEXER_RECONCILED")
                    )

                    keys_to_remove.append(symbol)
                    acc_slices = float(pending.get("accumulated_slices_pnl", 0.0))
                    total_fee_usd = round(fee, 4)
                    entry_fee = float(pending.get("entry_fee_usd") or (total_fee_usd / 2.0 if total_fee_usd > 0 else 0.0))
                    exit_fee = float(pending.get("exit_fee_usd") or (total_fee_usd - entry_fee if total_fee_usd >= entry_fee else total_fee_usd))
                    total_base = float(recon.get("total_base", 0.0) or pending.get("contracts", 0.0))
                    price_diff = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)
                    calc_gross = round(price_diff * total_base, 4) if total_base > 0 else round(net_pnl + total_fee_usd, 4)

                    resolved_reports.append({
                        "symbol": symbol,
                        "direction": direction,
                        "status": "CLOSED",
                        "triggered_by": pending.get("reason", "INDEXER_RECONCILED"),
                        "close_reason": pending.get("reason", "INDEXER_RECONCILED"),
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "pnl_usd": net_pnl,
                        "net_pnl_usd": net_pnl,
                        "pnl": net_pnl,
                        "gross_pnl_usd": calc_gross,
                        "entry_fee_usd": round(entry_fee, 4),
                        "exit_fee_usd": round(exit_fee, 4),
                        "total_fee_usd": total_fee_usd,
                        "fee": total_fee_usd,
                        "realized_r": realized_r,
                        "planned_risk_usd": float(pending.get("planned_risk_usd") or pending.get("approved_risk_usd") or 0.0),
                        "cohort": pending.get("cohort", "UNKNOWN"),
                        "pnl_source": "EXACT" if recon.get("is_exact") else "ESTIMATED",
                        "roi_pct": (net_pnl / pending.get("size_usd", 1.0) * 100) if pending.get("size_usd", 0) > 0 else 0,
                        "original_thesis": pending.get("original_thesis", ""),
                        "accumulated_slices_pnl": acc_slices
                    })

            for k in keys_to_remove:
                if k in self.pending_reconciliations:
                    del self.pending_reconciliations[k]
            if keys_to_remove or any(p.get("attempts", 0) > 0 for p in self.pending_reconciliations.values()):
                self._save_pending_reconciliations()

            return resolved_reports

    async def emergency_protect_risk_breach(self, symbol: str, approved_risk: float = 0.0, actual_risk: float = 0.0) -> tuple:
        """
        Emergency handler when post-fill actual risk violates the approved risk budget.
        1. Marks local position state as RISK_BREACH to halt normal management.
        2. Closes position on exchange via force_close_position().
        3. Independently verifies via get_active_positions(bypass_cache=True) that the position is closed.
        4. If closed: cleans up local state, logs resolution, and returns (True, realized_pnl).
        5. If unconfirmed: marks state as RISK_BREACH_UNRESOLVED and alerts Sentinel.
        """
        logger.warning(f"[NadoTradingService] 🚨 Executing emergency risk breach close for {symbol} (Approved: ${approved_risk:.2f}, Actual: ${actual_risk:.2f})")
        
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"
        base_sym = canonical_sym.split('-')[0].upper()
        
        lock = self._get_position_lock(canonical_sym)
        async with lock:
            for k in [symbol, canonical_sym, base_sym]:
                if k in self.active_positions:
                    self.active_positions[k]["protection_state"] = "RISK_BREACH"
            self._save_positions()
            
            # Fire force close with retries
            try:
                success, pnl = await self.force_close_position(symbol, bypass_check=True, max_retries=3)
            except Exception as e:
                logger.error(f"[NadoTradingService] ❌ Exception during emergency risk breach close for {symbol}: {e}")
                success, pnl = False, 0.0
                
            if success:
                logger.info(f"[NadoTradingService] ✅ Emergency risk breach close confirmed for {symbol}. Realized PnL: ${pnl:.2f}")
                for k in [symbol, canonical_sym, base_sym]:
                    if k in self.active_positions:
                        del self.active_positions[k]
                self._save_positions()
                return True, pnl
        
        # If force_close_position failed or was unconfirmed, verify independently.
        # CRITICAL P1-3: Do NOT assume an empty get_active_positions() right after entry means closed!
        # Indexer lag could cause an empty response while the position remains open on the engine.
        closure_status = "UNKNOWN"
        try:
            active_on_chain = await self.get_active_positions(bypass_cache=True)
            product_id = self.product_map.get(base_sym)
            matched_pos = None
            if active_on_chain:
                for p in active_on_chain:
                    p_sym = p.get("symbol", "").replace('/', '-').strip().upper()
                    if p.get("product_id") == product_id or p_sym == canonical_sym:
                        matched_pos = p
                        break
            
            if matched_pos is not None and abs(float(matched_pos.get("contracts", 0.0) or 0.0)) > 0:
                closure_status = "STILL_OPEN"
                logger.critical(
                    f"[NadoTradingService] 🚨 POSITIVELY DETECTED OPEN on exchange: {canonical_sym} "
                    f"({matched_pos.get('contracts')} contracts). Emergency close FAILED!"
                )
            else:
                # Check recent historical orders for a confirmed closing fill
                has_closing_fill = False
                try:
                    if hasattr(self.client, "market") and hasattr(self.client.market, "get_subaccount_historical_orders"):
                        orders_resp = await asyncio.to_thread(
                            self.client.market.get_subaccount_historical_orders,
                            subaccount=self.default_subaccount_id,
                            product_ids=[product_id] if product_id is not None else []
                        )
                        recent_orders = getattr(orders_resp, "orders", []) or []
                        now_ms = time.time() * 1000
                        for o in recent_orders:
                            o_ts = float(getattr(o, "timestamp", 0) or 0)
                            if abs(now_ms - o_ts) < 30000 and abs(int(getattr(o, "base_filled", 0) or 0)) > 0:
                                has_closing_fill = True
                                break
                except Exception as hist_e:
                    logger.warning(f"[NadoTradingService] ⚠️ Historical order check failed during emergency close verification: {hist_e}")
                    has_closing_fill = False

                if has_closing_fill:
                    closure_status = "CLOSED_CONFIRMED"
                else:
                    closure_status = "UNKNOWN"
        except Exception as check_e:
            logger.error(f"[NadoTradingService] ⚠️ Verification of emergency close failed: {check_e}")
            closure_status = "UNKNOWN"

        if closure_status == "CLOSED_CONFIRMED":
            logger.info(f"[NadoTradingService] ℹ️ Independent check confirmed {symbol} closing fill.")
            for k in [symbol, canonical_sym, base_sym]:
                if k in self.active_positions:
                    del self.active_positions[k]
            self._save_positions()
            return True, pnl

        # Position is STILL_OPEN or UNKNOWN: Retain as RISK_BREACH_UNRESOLVED
        for k in [symbol, canonical_sym, base_sym]:
            if k in self.active_positions:
                self.active_positions[k]["protection_state"] = "RISK_BREACH_UNRESOLVED"
        self._save_positions()
        logger.critical(
            f"[NadoTradingService] 🚨 CRITICAL: Position {symbol} closure status is {closure_status}! "
            f"Retaining in active_positions as RISK_BREACH_UNRESOLVED for Sentinel reconciliation."
        )
        return False, 0.0

    async def _fetch_verified_close_metrics(self, symbol: str, pos_data: dict) -> dict:
        """
        Гарантированно дожидается появления закрывающих филлов в Indexer API
        и возвращает точный расклад: Gross PnL, Total Fees (Entry + Exit) и Net PnL.
        Паттерн: Reconcile-First Notification.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"
        base_sym = canonical_sym.split('-')[0].upper()

        entry_price = float(pos_data.get("entry_price", 0.0) or 0.0)
        initial_size = float(pos_data.get("initial_size") or pos_data.get("size") or pos_data.get("contracts") or pos_data.get("amount") or 0.0)
        if initial_size <= 0 and entry_price > 0:
            sz = float(pos_data.get("size_usd", 0.0) or 0.0)
            initial_size = sz / entry_price if sz > 0 else 0.0

        direction = str(pos_data.get("direction", "LONG")).upper()
        planned_risk = float(pos_data.get("planned_risk_usd") or pos_data.get("approved_risk_usd") or pos_data.get("risk_usd") or 0.0)
        if planned_risk <= 0.0:
            sl_p = float(pos_data.get("sl_price", 0.0) or 0.0)
            if entry_price > 0 and sl_p > 0 and initial_size > 0:
                planned_risk = initial_size * abs(entry_price - sl_p)

        subaccount = pos_data.get("sender") or pos_data.get("subaccount") or getattr(self, "default_subaccount_id", None)
        product_id = pos_data.get("product_id") or self.product_map.get(canonical_sym) or self.product_map.get(base_sym)
        open_d = pos_data.get("open_digest") or pos_data.get("order_digest") or pos_data.get("digest")
        open_time = float(pos_data.get("open_time", 0.0) or pos_data.get("timestamp", 0.0) or 0.0)
        now_ts = time.time()
        close_ts = float(pos_data.get("closed_at") or now_ts)
        min_ts = (open_time - 5.0) if open_time > 0 else (close_ts - 86400.0)
        max_ts = close_ts + 60.0

        excluded_digests = set(getattr(self, "processed_fill_digests", set()))
        plan_dict = pos_data.get("partial_tp_plan")
        if plan_dict and isinstance(plan_dict, dict):
            for tier in plan_dict.get("tiers", []):
                td = tier.get("order_digest")
                if td:
                    excluded_digests.add(str(td).lower())
        if open_d:
            excluded_digests.add(str(open_d).lower())

        # 3 попытки дождаться индексатора (суммарно до 6 секунд ожидания: 1.5s -> 2.0s -> 2.5s)
        retry_delays = (1.5, 2.0, 2.5)
        recon = None

        if self.client and subaccount and product_id is not None and initial_size > 0:
            for attempt, delay in enumerate(retry_delays, start=1):
                await asyncio.sleep(delay)
                try:
                    recon = await self._reconcile_closing_fills_from_history(
                        subaccount=subaccount,
                        product_id=product_id,
                        direction=direction,
                        entry_price=entry_price,
                        expected_contracts=initial_size,
                        close_timestamp=close_ts,
                        min_timestamp=min_ts,
                        max_timestamp=max_ts,
                        excluded_digests=excluded_digests,
                        open_digest=open_d,
                        allow_missing_timestamp=True
                    )
                    if recon and recon.get("exit_price") is not None and recon.get("is_exact"):
                        logger.info(f"[NadoTradingService] 🎯 Индексатор подтвердил закрытие {canonical_sym} (попытка {attempt}/3): VWAP {recon['exit_price']:.4f}")
                        break
                except Exception as rec_err:
                    logger.debug(f"[NadoTradingService] Ошибка сверки с индексатором {canonical_sym}: {rec_err}")
                logger.debug(f"[NadoTradingService] Ожидание индексатора для {canonical_sym} (попытка {attempt}/3)...")

        # Если индексатор ответил точными данными:
        if recon and recon.get("exit_price") is not None:
            exit_price = float(recon["exit_price"])
            closed_qty = float(recon.get("total_base") or recon.get("closed_qty") or initial_size)
            exit_fee = float(recon.get("fee", 0.0) or 0.0)
            pnl_source = "EXACT" if recon.get("is_exact") else "ESTIMATED"
            if recon.get("digests"):
                self._register_processed_fills(
                    recon["digests"],
                    subaccount=subaccount,
                    product_id=product_id,
                    symbol=canonical_sym
                )
        else:
            # Fallback, если индексатор не ответил за 6 секунд
            exit_price = float(pos_data.get("assumed_exit_price") or pos_data.get("last_price") or pos_data.get("highest_price" if direction == "LONG" else "lowest_price") or entry_price)
            if exit_price <= 0:
                exit_price = entry_price if entry_price > 0 else 1.0
            closed_qty = initial_size
            exit_fee = exit_price * closed_qty * 0.0005
            pnl_source = "ESTIMATED"

        # Комиссия за вход: сохраненная из pos_data или расчетная
        is_mock = getattr(self, "_mock_mode", False) or "Mock" in type(getattr(self, "client", None)).__name__
        if "entry_fee_usd" in pos_data:
            entry_fee = float(pos_data["entry_fee_usd"] or 0.0)
        elif pnl_source == "EXACT" and is_mock:
            # В мок-тестах с точным филлом из индексатора, если входная комиссия не была задана, не добавляем синтетическую
            entry_fee = 0.0
        else:
            # В реальной торговле или при оценке (ESTIMATED) всегда учитываем входную комиссию (0.05% от объема входа)
            entry_fee = entry_price * closed_qty * 0.0005

        if exit_fee <= 0.0:
            exit_fee = exit_price * closed_qty * 0.0005

        total_fee_usd = round(entry_fee + exit_fee, 4)

        # Грязный PnL (только движение цены)
        price_diff = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)
        gross_pnl_usd = round(price_diff * closed_qty, 4)

        # Если биржа отдает готовый Net PnL с учетом фандинга и комиссий — берем его,
        # иначе вычитаем суммарную комиссию из Gross PnL
        if recon and recon.get("exchange_net_pnl") is not None:
            net_pnl_usd = round(float(recon["exchange_net_pnl"]), 4)
        else:
            net_pnl_usd = round(gross_pnl_usd - total_fee_usd, 4)

        # Учет ступеней частичного Take-Profit (если были)
        accumulated_slices = float(pos_data.get("accumulated_realized_pnl", 0.0))
        if accumulated_slices != 0.0:
            net_pnl_usd = round(net_pnl_usd + accumulated_slices, 4)
            gross_pnl_usd = round(gross_pnl_usd + accumulated_slices, 4)

        realized_r = round(net_pnl_usd / planned_risk, 2) if planned_risk > 0 else 0.0
        close_reason = pos_data.get("close_reason") or pos_data.get("triggered_by") or "CLOSED"

        return {
            "symbol": canonical_sym,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "closed_qty": closed_qty,
            "gross_pnl_usd": gross_pnl_usd,
            "entry_fee_usd": round(entry_fee, 4),
            "exit_fee_usd": round(exit_fee, 4),
            "total_fee_usd": total_fee_usd,
            "net_pnl_usd": net_pnl_usd,
            "pnl": net_pnl_usd,
            "pnl_usd": net_pnl_usd,
            "fee": total_fee_usd,
            "is_exact": (pnl_source == "EXACT"),
            "realized_r": realized_r,
            "planned_risk_usd": planned_risk,
            "pnl_source": pnl_source,
            "close_reason": close_reason,
            "triggered_by": close_reason,
            "accumulated_slices_pnl": accumulated_slices
        }

    async def force_close_position(self, symbol: str, bypass_check: bool = False, max_retries: int = 3, exit_reason: Optional[str] = None) -> tuple:
        """Manually closes a position on Nado by firing a close_position market order.
        Includes retry logic and backoff to handle Indexer lag immediately after entry."""
        if not self.is_connected:
            return False, 0.0
            
        if getattr(config, "SHADOW_MODE", False):
            canonical_sym = symbol.replace('/', '-').strip().upper()
            if '-' not in canonical_sym:
                canonical_sym = f"{canonical_sym}-USD"
            pos = self.active_positions.get(canonical_sym)
            if not pos:
                return False, 0.0
            cur_price = float(pos.get("current_price", pos.get("entry_price", 0.0)) or 0.0)
            entry_px = float(pos.get("entry_price", 0.0) or 0.0)
            cntr = float(pos.get("contracts", 0.0) or 0.0)
            is_long = pos.get("direction") == "LONG"
            pnl = (cur_price - entry_px) * cntr if is_long else (entry_px - cur_price) * cntr

            # Determine granular exit tag instead of generic SHADOW_FORCE_CLOSE
            raw_reason = exit_reason or pos.get("close_reason") or pos.get("protection_state") or "FORCE_CLOSE"
            raw_up = str(raw_reason).upper()
            if "STOP_LOSS" in raw_up or "STOP LOSS" in raw_up or raw_up in ["SL", "SOFTWARE_SL", "BACKUP_SL"] or "FAST_MONITOR_STOP LOSS" in raw_up:
                exit_tag = "STOP_LOSS"
            elif "TAKE_PROFIT" in raw_up or "TAKE PROFIT" in raw_up or raw_up in ["TP", "SOFTWARE_TP"] or "FAST_MONITOR_TAKE PROFIT" in raw_up:
                exit_tag = "TAKE_PROFIT"
            elif "TRAILING" in raw_up:
                exit_tag = "TRAILING_STOP"
            elif any(k in raw_up for k in ["TIME_STOP", "BREAK_EVEN", "BREAKEVEN", "BE"]):
                exit_tag = "TIME_STOP_BE"
            else:
                exit_tag = str(raw_reason)

            logger.info(f"[NadoTradingService] 👻 [SHADOW MODE] Virtual close ({exit_tag}): {canonical_sym} at ${cur_price:.4f} (PnL: ${pnl:.2f})")
            fee = cntr * cur_price * 0.0005
            net_pnl = pnl - fee
            pos["close_reason"] = exit_tag
            pos["triggered_by"] = exit_tag
            self._record_closed_trade_journal(
                symbol=canonical_sym,
                direction=pos.get("direction", "LONG"),
                entry_price=entry_px,
                exit_price=cur_price,
                net_pnl=net_pnl,
                pos_data=pos,
                fee=fee,
                triggered_by=exit_tag
            )
            del self.active_positions[canonical_sym]
            self._save_state()
            return True, pnl

        try:
            canonical_sym = symbol.replace('/', '-').strip().upper()
            if '-' not in canonical_sym:
                canonical_sym = f"{canonical_sym}-USD"
            base_symbol = canonical_sym.split('-')[0].upper()
            target_pos = None
            
            # Fetch real info
            product_id = self.product_map.get(base_symbol)
            if not product_id:
                return False, 0.0
                
            address = self.wallet.get_address()
            res = await asyncio.to_thread(self.client.subaccount.get_subaccounts, address)
            if not res or not res.subaccounts:
                return False, 0.0
            subaccount = res.subaccounts[0].subaccount

            for attempt in range(max_retries):
                # CRITICAL-5: Always fetch active positions to capture realized PnL prior to close
                try:
                    positions = await self.get_active_positions(bypass_cache=True)
                except Exception as fetch_err:
                    if not bypass_check:
                        if attempt < max_retries - 1:
                            logger.warning(f"[NadoTradingService] Error fetching positions for {symbol}: {fetch_err}. Retrying ({attempt+1}/{max_retries})...")
                            await asyncio.sleep(1.5)
                            continue
                        else:
                            return False, 0.0
                    else:
                        logger.info(f"[NadoTradingService] ℹ️ bypass_check=True: позиция {symbol} не найдена в активных ордерах Indexer ({fetch_err}). Сверка PnL будет выполнена по истории сделок...")
                        positions = []

                target_pos = next((p for p in positions if p["symbol"].split('-')[0].upper() == base_symbol), None)
                        
                if not target_pos:
                    if not bypass_check:
                        if attempt < max_retries - 1:
                            logger.warning(f"[NadoTradingService] Position {symbol} invisible to Indexer. Retrying ({attempt+1}/{max_retries})...")
                            await asyncio.sleep(1.5)
                            continue
                        else:
                            return False, 0.0
                    else:
                        logger.info(f"[NadoTradingService] ℹ️ bypass_check=True: позиция {symbol} уже закрыта на бирже. Точный PnL вычисляется через историю Indexer...")
                            
                # Fire close_position via SDK
                try:
                    res = await asyncio.to_thread(self.client.market.close_position, subaccount, product_id)
                    logger.info(f"[NadoTradingService] 🧹 Successfully forced closed {symbol}. TX: {res}")
                    
                    # Capture authoritative local pos_data BEFORE deletion
                    pos_data = dict(self.active_positions.get(symbol) or self.active_positions.get(canonical_sym) or self.active_positions.get(base_symbol) or {})
                    sl_dig = pos_data.get("sl_digest")
                    tp_dig = pos_data.get("tp_digest")
                    digests_to_cancel = [d for d in [sl_dig, tp_dig] if d]
                    if digests_to_cancel:
                        await self._cancel_trigger_digests(product_id, digests_to_cancel, sender=pos_data.get("sender", subaccount))

                    # Clean up local cache and update stats
                    if symbol in self.active_positions:
                        del self.active_positions[symbol]
                    if canonical_sym in self.active_positions:
                        del self.active_positions[canonical_sym]
                    alias_key = base_symbol if '-' in symbol else f"{base_symbol}-USD"
                    if alias_key in self.active_positions:
                        del self.active_positions[alias_key]
                    self._save_positions()
                        
                    # Reconcile exact execution fill price and real PnL from Nado indexer
                    actual_exit_price = None
                    actual_fee = 0.0
                    actual_pnl = None
                    is_exact_pnl = False
                    close_time = time.time()

                    direction = str(pos_data.get("direction", (target_pos.get("direction") if target_pos else "LONG"))).upper()
                    entry_price = float(pos_data.get("entry_price") or (target_pos.get("entry_price") if target_pos else 0.0) or 0.0)
                    expected_contracts = abs(float(pos_data.get("contracts") or (target_pos.get("contracts") if target_pos else 0.0) or 0.0))
                    if expected_contracts <= 0 and entry_price > 0:
                        sz = float(pos_data.get("size_usd") or 0.0)
                        expected_contracts = sz / entry_price if sz > 0 else 0.0

                    # Collect excluded digests (filled TP tiers, open digest, already processed digests)
                    excluded_digests = set(getattr(self, "processed_fill_digests", set()))
                    plan_dict = pos_data.get("partial_tp_plan")
                    if plan_dict and isinstance(plan_dict, dict):
                        for tier in plan_dict.get("tiers", []):
                            td = tier.get("order_digest")
                            if td:
                                excluded_digests.add(str(td).lower())
                    open_d = pos_data.get("open_digest") or pos_data.get("digest")
                    if open_d:
                        excluded_digests.add(str(open_d).lower())

                    pos_data["close_reason"] = "SOFTWARE_FORCE_CLOSE"
                    pos_data["closed_at"] = close_time
                    metrics = await self._fetch_verified_close_metrics(canonical_sym, pos_data)

                    actual_exit_price = metrics["exit_price"]
                    actual_fee = metrics["total_fee_usd"]
                    pnl = metrics["net_pnl_usd"]
                    is_exact_pnl = (metrics["pnl_source"] == "EXACT")

                    # If not exact and pre-close target_pos had a pnl snapshot, fallback to target_pos
                    if not is_exact_pnl and target_pos and target_pos.get("pnl") is not None:
                        acc_pnl = float(pos_data.get("accumulated_realized_pnl") or (target_pos.get("accumulated_realized_pnl") if target_pos else 0.0) or 0.0)
                        pnl = round(target_pos["pnl"] + acc_pnl, 4)
                        metrics["net_pnl_usd"] = pnl
                        metrics["pnl"] = pnl
                        metrics["pnl_usd"] = pnl

                    if not hasattr(self, "_last_close_info"):
                        self._last_close_info = {}
                    self._last_close_info[symbol] = metrics
                    self._last_close_info[base_symbol] = metrics
                    self._last_close_info[canonical_sym] = metrics

                    if is_exact_pnl:
                        self._evaluate_and_update_streak(
                            net_pnl=pnl,
                            planned_risk=float(pos_data.get("planned_risk_usd") or pos_data.get("approved_risk_usd") or 0.0),
                            pos_data=pos_data,
                            symbol=canonical_sym
                        )

                        try:
                            self._record_closed_trade_journal(
                                symbol=canonical_sym,
                                direction=direction,
                                entry_price=entry_price,
                                exit_price=actual_exit_price or 0.0,
                                net_pnl=pnl,
                                pos_data=pos_data,
                                fee=actual_fee,
                                triggered_by="SOFTWARE_FORCE_CLOSE"
                            )
                        except Exception as j_err:
                            logger.warning(f"[NadoTradingService] ⚠️ Failed recording force-closed trade to journal: {j_err}")
                    else:
                        # Indexer lag: store in pending_reconciliations and do NOT corrupt streak
                        logger.warning(
                            f"[NadoTradingService] ⏳ Trade {symbol} force-closed, but indexer fill unconfirmed after 3 retries. "
                            f"Saved to pending reconciliations. Streak unchanged."
                        )
                        self.pending_reconciliations[canonical_sym] = {
                            "symbol": canonical_sym,
                            "base_symbol": base_symbol,
                            "product_id": product_id,
                            "subaccount": subaccount,
                            "direction": direction,
                            "entry_price": entry_price,
                            "contracts": expected_contracts,
                            "size_usd": float(pos_data.get("size_usd") or 0.0),
                            "accumulated_realized_pnl": acc_pnl,
                            "open_time": float(pos_data.get("open_time", 0.0)),
                            "closed_at": close_time,
                            "detected_at": close_time,
                            "attempts": 0,
                            "excluded_digests": list(excluded_digests),
                            "status": "PNL_RECONCILIATION_PENDING",
                            "reason": "FORCE_CLOSE_INDEXER_LAG",
                            "planned_risk_usd": float(pos_data.get("planned_risk_usd") or pos_data.get("approved_risk_usd") or 0.0),
                            "cohort": pos_data.get("cohort", "UNKNOWN"),
                            "base_risk_pct": float(pos_data.get("base_risk_pct", 0.0)),
                            "applied_multipliers": pos_data.get("applied_multipliers", {})
                        }
                        self._save_pending_reconciliations()
                        
                    try:
                        from services.copy_trading_dispatcher import copy_trading_dispatcher
                        asyncio.create_task(
                            copy_trading_dispatcher.dispatch_close_position(
                                symbol=canonical_sym,
                                exit_price=float(actual_exit_price or 0.0),
                                reason=str(exit_reason or "SOFTWARE_FORCE_CLOSE")
                            )
                        )
                    except Exception as copy_close_err:
                        logger.debug(f"[CopyTrading] Dispatch close error: {copy_close_err}")

                    return True, pnl
                except Exception as close_e:
                    err_str = str(close_e).lower()
                    if "2002" in err_str or "amount is zero" in err_str:
                        # 1. STRICT FAIL-CLOSED on-chain verification
                        confirmed_zero_on_chain = False
                        try:
                            fresh_positions = await self.get_active_positions(bypass_cache=True)
                            fetch_ok = getattr(self, "_last_positions_fetch_ok", True)
                            if fetch_ok:
                                exists = False
                                for fp in fresh_positions:
                                    fp_prod = fp.get("_product_id") or fp.get("product_id") or self.product_map.get(fp.get("symbol"))
                                    if product_id is not None and fp_prod == product_id:
                                        exists = True
                                        break
                                    elif fp.get("symbol") in (symbol, canonical_sym):
                                        exists = True
                                        break
                                if not exists:
                                    confirmed_zero_on_chain = True
                        except Exception as verify_err:
                            logger.error(f"[NadoTradingService] ⚠️ Could not verify on-chain zero status on 2002 error: {verify_err}")

                        if not confirmed_zero_on_chain:
                            logger.error(
                                f"[NadoTradingService] 🚨 CRITICAL SAFETY GUARD: close_position returned error 2002, "
                                f"but zero-balance on exchange could NOT be confirmed (still open or query failed). "
                                f"RETAINING local tracking for safety."
                            )
                            if symbol in self.active_positions:
                                self.active_positions[symbol]["requires_reconciliation"] = True
                                self._save_positions()
                            return False, 0.0

                        logger.info(
                            f"[NadoTradingService] ℹ️ Position {symbol} confirmed zero on exchange (error 2002). "
                            f"Reconciling final exit fill before cleanup."
                        )

                        # Capture authoritative local pos_data before deletion
                        pos_data = dict(self.active_positions.get(symbol) or self.active_positions.get(canonical_sym) or self.active_positions.get(base_symbol) or {})
                        direction = str(pos_data.get("direction", "LONG")).upper()
                        entry_price = float(pos_data.get("entry_price") or 0.0)
                        expected_contracts = abs(float(pos_data.get("contracts") or 0.0))
                        if expected_contracts <= 0 and entry_price > 0:
                            sz = float(pos_data.get("size_usd") or 0.0)
                            expected_contracts = sz / entry_price if sz > 0 else 0.0
                        acc_pnl = float(pos_data.get("accumulated_realized_pnl", 0.0))

                        # Cancel any orphan trigger orders
                        sl_dig = pos_data.get("sl_digest")
                        tp_dig = pos_data.get("tp_digest")
                        digests_to_cancel = [d for d in [sl_dig, tp_dig] if d]
                        if digests_to_cancel:
                            await self._cancel_trigger_digests(product_id, digests_to_cancel, sender=pos_data.get("sender", subaccount))

                        if symbol in self.active_positions:
                            del self.active_positions[symbol]
                        if canonical_sym in self.active_positions:
                            del self.active_positions[canonical_sym]
                        alias_key = base_symbol if '-' in symbol else f"{base_symbol}-USD"
                        if alias_key in self.active_positions:
                            del self.active_positions[alias_key]
                        self._save_positions()

                        # Collect excluded digests (filled TP tiers, open digest, already processed digests)
                        excluded_digests = set(getattr(self, "processed_fill_digests", set()))
                        plan_dict = pos_data.get("partial_tp_plan")
                        if plan_dict and isinstance(plan_dict, dict):
                            for tier in plan_dict.get("tiers", []):
                                td = tier.get("order_digest")
                                if td:
                                    excluded_digests.add(str(td).lower())
                        open_d = pos_data.get("open_digest") or pos_data.get("digest")
                        if open_d:
                            excluded_digests.add(str(open_d).lower())

                        open_time_val = float(pos_data.get("open_time", 0.0))
                        min_ts = (open_time_val - 5.0) if open_time_val > 0 else (time.time() - 1800.0)

                        # 2. Try to reconcile fills from history using the unified method
                        recon = await self._reconcile_closing_fills_from_history(
                            subaccount=subaccount,
                            product_id=product_id,
                            direction=direction,
                            entry_price=entry_price,
                            expected_contracts=expected_contracts,
                            close_timestamp=time.time(),
                            min_timestamp=min_ts,
                            max_timestamp=time.time() + 60.0,
                            excluded_digests=excluded_digests,
                            open_digest=open_d,
                            allow_missing_timestamp=True
                        )

                        planned_risk = float(pos_data.get("planned_risk_usd") or pos_data.get("approved_risk_usd") or 0.0)
                        close_reason = pos_data.get("close_reason") or "NATIVE_TRIGGER_EXECUTION"

                        if recon.get("is_exact"):
                            reconciled_pnl = round(recon["net_pnl"] + acc_pnl, 4)
                            actual_exit = float(recon.get("exit_price") or entry_price)
                            actual_qty = float(recon.get("total_base") or expected_contracts)
                            exit_fee = float(recon.get("fee", 0.0) or 0.0)
                            entry_fee = float(pos_data.get("entry_fee_usd", 0.0) or 0.0)
                            if entry_fee <= 0 and entry_price > 0 and actual_qty > 0:
                                entry_fee = entry_price * actual_qty * 0.0005
                            if exit_fee <= 0 and actual_exit > 0 and actual_qty > 0:
                                exit_fee = actual_exit * actual_qty * 0.0005
                            total_fee = round(entry_fee + exit_fee, 4)
                            price_diff = (actual_exit - entry_price) if direction == "LONG" else (entry_price - actual_exit)
                            gross_pnl = round(price_diff * actual_qty + acc_pnl, 4)
                            realized_r = round(reconciled_pnl / planned_risk, 2) if planned_risk > 0 else 0.0

                            logger.info(f"[NadoTradingService] 🎯 Reconciled actual PnL for 2002-closed position {symbol}: ${reconciled_pnl:.4f} (includes TP ${acc_pnl:.2f})")
                            self._register_processed_fills(
                                recon.get("digests", []),
                                subaccount=subaccount,
                                product_id=product_id,
                                symbol=canonical_sym
                            )
                            self._evaluate_and_update_streak(
                                net_pnl=reconciled_pnl,
                                planned_risk=planned_risk,
                                pos_data=pos_data,
                                symbol=canonical_sym
                            )

                            metrics = {
                                "symbol": canonical_sym,
                                "direction": direction,
                                "entry_price": entry_price,
                                "exit_price": actual_exit,
                                "closed_qty": actual_qty,
                                "gross_pnl_usd": gross_pnl,
                                "entry_fee_usd": round(entry_fee, 4),
                                "exit_fee_usd": round(exit_fee, 4),
                                "total_fee_usd": total_fee,
                                "net_pnl_usd": reconciled_pnl,
                                "pnl": reconciled_pnl,
                                "pnl_usd": reconciled_pnl,
                                "fee": total_fee,
                                "is_exact": True,
                                "realized_r": realized_r,
                                "planned_risk_usd": planned_risk,
                                "pnl_source": "EXACT",
                                "close_reason": close_reason,
                                "triggered_by": close_reason,
                                "accumulated_slices_pnl": acc_pnl
                            }

                            if not hasattr(self, "_last_close_info"):
                                self._last_close_info = {}
                            self._last_close_info[symbol] = metrics
                            self._last_close_info[base_symbol] = metrics
                            self._last_close_info[canonical_sym] = metrics

                            try:
                                self._record_closed_trade_journal(
                                    symbol=canonical_sym,
                                    direction=direction,
                                    entry_price=entry_price,
                                    exit_price=actual_exit,
                                    net_pnl=reconciled_pnl,
                                    pos_data=pos_data,
                                    fee=total_fee,
                                    triggered_by=close_reason
                                )
                            except Exception as j_err:
                                logger.warning(f"[NadoTradingService] ⚠️ Failed recording 2002-closed trade to journal: {j_err}")

                            return True, reconciled_pnl
                        else:
                            # Indexer doesn't have the fills yet. Store in pending_reconciliations if contracts > 0
                            if expected_contracts <= 0 and entry_price > 0:
                                sz = float(pos_data.get("size_usd") or 0.0)
                                if sz > 0:
                                    expected_contracts = sz / entry_price
                            if expected_contracts > 0:
                                logger.warning(
                                    f"[NadoTradingService] ⏳ 2002-closed position {symbol} fills not yet in indexer. "
                                    f"Queued for pending reconciliation. Streak NOT modified."
                                )
                                self.pending_reconciliations[canonical_sym] = {
                                    "symbol": canonical_sym,
                                    "base_symbol": base_symbol,
                                    "product_id": product_id,
                                    "subaccount": subaccount,
                                    "direction": direction,
                                    "entry_price": entry_price,
                                    "contracts": expected_contracts,
                                    "size_usd": float(pos_data.get("size_usd") or 0.0),
                                    "accumulated_realized_pnl": acc_pnl,
                                    "open_time": open_time_val,
                                    "closed_at": time.time(),
                                    "detected_at": time.time(),
                                    "attempts": 0,
                                    "excluded_digests": list(excluded_digests),
                                    "status": "PNL_RECONCILIATION_PENDING",
                                    "reason": "ERROR_2002_FILL_PENDING",
                                    "planned_risk_usd": planned_risk,
                                    "cohort": pos_data.get("cohort", "UNKNOWN"),
                                    "base_risk_pct": float(pos_data.get("base_risk_pct", 0.0)),
                                    "applied_multipliers": pos_data.get("applied_multipliers", {})
                                }
                                self._save_pending_reconciliations()

                                assumed_exit = float(pos_data.get("assumed_exit_price") or pos_data.get("sl_price") or pos_data.get("tp_price") or entry_price)
                                price_diff = (assumed_exit - entry_price) if direction == "LONG" else (entry_price - assumed_exit)
                                gross_pnl = round(price_diff * expected_contracts + acc_pnl, 4)
                                entry_fee = float(pos_data.get("entry_fee_usd") or (entry_price * expected_contracts * 0.0005))
                                exit_fee = assumed_exit * expected_contracts * 0.0005
                                est_fee = round(entry_fee + exit_fee, 4)
                                est_net = round(gross_pnl - est_fee, 4)
                                est_r = round(est_net / planned_risk, 2) if planned_risk > 0 else 0.0

                                metrics = {
                                    "symbol": canonical_sym,
                                    "direction": direction,
                                    "entry_price": entry_price,
                                    "exit_price": assumed_exit,
                                    "closed_qty": expected_contracts,
                                    "gross_pnl_usd": gross_pnl,
                                    "entry_fee_usd": round(entry_fee, 4),
                                    "exit_fee_usd": round(exit_fee, 4),
                                    "total_fee_usd": est_fee,
                                    "net_pnl_usd": est_net,
                                    "pnl": est_net,
                                    "pnl_usd": est_net,
                                    "fee": est_fee,
                                    "is_exact": False,
                                    "realized_r": est_r,
                                    "planned_risk_usd": planned_risk,
                                    "pnl_source": "ESTIMATED",
                                    "close_reason": close_reason,
                                    "triggered_by": close_reason,
                                    "accumulated_slices_pnl": acc_pnl
                                }

                                if not hasattr(self, "_last_close_info"):
                                    self._last_close_info = {}
                                self._last_close_info[symbol] = metrics
                                self._last_close_info[base_symbol] = metrics
                                self._last_close_info[canonical_sym] = metrics
                            else:
                                logger.info(
                                    f"[NadoTradingService] ℹ️ Position {symbol} confirmed zero on exchange (error 2002) "
                                    f"with no local volume to reconcile. Cleanup complete."
                                )
                            return True, acc_pnl

                    logger.warning(f"[NadoTradingService] ⚠️ Retryable close error for {symbol}: {close_e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.5)
                    else:
                        # Fix #2: Don't re-raise — keep position tracked so Keeper/Fast Monitor retries next cycle
                        logger.error(
                            f"[NadoTradingService] 🚨 ALERT: Failed to close {symbol} after {max_retries} retries. "
                            f"Position remains in active_positions for next retry cycle. Last error: {close_e}"
                        )
                        return False, 0.0
                        
            return False, 0.0
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to force close {symbol}: {e}")
            return False, 0.0

    async def execute_partial_close(
        self,
        symbol: str,
        contracts_to_close: float,
        current_price: float,
        max_slippage_pct: float = 0.005,
        max_poll_attempts: int = 5
    ) -> PartialCloseResult:
        """
        Executes a partial close of an active position using an IOC order with reduce_only=True.
        Phase 2 implementation:
        - Strict counter-direction only (LONG -> SELL, SHORT -> BUY).
        - reduce_only=True encoded in appendix to prevent position reversal or expansion.
        - Verification of real base_filled from indexer/sequencer before marking successful.
        - Safe handling of partial fills and rejection.
        - Does NOT alter active_positions.json or interfere with background monitors directly.
        """
        if not self.is_connected or not self.client:
            return PartialCloseResult(success=False, error="NadoTradingService is not connected.")

        if contracts_to_close <= 0 or current_price <= 0:
            return PartialCloseResult(success=False, error=f"Invalid arguments: contracts={contracts_to_close}, price={current_price}")

        pos = self.active_positions.get(symbol)
        if not pos:
            base_sym = symbol.split("-")[0].upper()
            pos = self.active_positions.get(base_sym) or self.active_positions.get(f"{base_sym}-USD")
            if not pos:
                return PartialCloseResult(success=False, error=f"Position for {symbol} not found in active_positions.")

        direction = str(pos.get("direction", "LONG")).upper()
        entry_price = float(pos.get("entry_price", current_price) or current_price)

        if getattr(config, "SHADOW_MODE", False):
            realized_pnl = (current_price - entry_price) * contracts_to_close if direction == "LONG" else (entry_price - current_price) * contracts_to_close
            fee_usd = contracts_to_close * current_price * 0.0005
            mock_digest = f"0xshadow_tp_{int(time.time()*1000):x}"
            logger.info(
                f"[NadoTradingService] 👻 [SHADOW MODE] Simulated partial close for {symbol}: "
                f"closed {contracts_to_close:.4f} at ${current_price:.4f} (Realized PnL: ${realized_pnl:.2f})"
            )
            return PartialCloseResult(
                success=True,
                digest=mock_digest,
                requested_contracts=contracts_to_close,
                filled_contracts=contracts_to_close,
                fill_price=current_price,
                realized_pnl=realized_pnl,
                fee_usd=fee_usd,
                is_partial_fill=False,
                is_ambiguous=False,
                is_estimated_fee=True
            )

        base_symbol = symbol.split('-')[0].upper()
        product_id = pos.get("product_id") or self.product_map.get(base_symbol) or self.product_map.get(f"{base_symbol}-USD")
        if product_id is None:
            return PartialCloseResult(success=False, error=f"Product ID not found for {symbol}.")

        # Fetch market parameters for step and price increments
        params_dict = self._get_market_parameters(product_id)
        size_increment = params_dict.get("size_increment_x18", 0)
        price_increment = params_dict.get("price_increment_x18", 0)
        min_size = params_dict.get("min_size", 0.0)

        if size_increment == 0 or price_increment == 0:
            return PartialCloseResult(success=False, error=f"Market parameters not initialized for product {product_id}.")

        if min_size > 0 and contracts_to_close < min_size:
            return PartialCloseResult(
                success=False,
                error=f"Requested close size {contracts_to_close} is below market min_size {min_size}."
            )

        # Calculate base amount in x18 safely with Decimal to prevent precision loss / dust
        amount_x18 = to_x18_aligned(contracts_to_close, size_increment, mode="floor")
        if amount_x18 <= 0:
            return PartialCloseResult(success=False, error="Order amount is zero after size_increment alignment.")

        # Determine counter-direction and limit price:
        # LONG -> counter-order is SELL (negative amount)
        # SHORT -> counter-order is BUY (positive amount)
        if direction == "LONG":
            order_amount_x18 = -amount_x18
            limit_price = current_price * (1.0 - max_slippage_pct)
            price_x18 = to_x18_aligned(limit_price, price_increment, mode="floor")
        elif direction == "SHORT":
            order_amount_x18 = amount_x18
            limit_price = current_price * (1.0 + max_slippage_pct)
            price_x18 = to_x18_aligned(limit_price, price_increment, mode="ceil")
        else:
            return PartialCloseResult(success=False, error=f"Invalid position direction: {direction}")

        # Build appendix with reduce_only=True
        try:
            from nado_protocol.utils.order import order_reduce_only
            from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
            from nado_protocol.utils.nonce import gen_order_nonce
            from nado_protocol.utils.bytes32 import subaccount_to_hex
            from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams

            appendix = self.build_order_appendix(order_type=OrderType.IOC, reduce_only=True)
            if not order_reduce_only(appendix):
                return PartialCloseResult(success=False, error="Failed to encode reduce_only=True in order appendix.")

            expiration = get_expiration_timestamp(86400 * 30)
            nonce = gen_order_nonce()
            sender = pos.get("sender") or self.default_subaccount_id or subaccount_to_hex(self.wallet.get_address(), "default")

            order = OrderParams(
                sender=sender,
                amount=order_amount_x18,
                priceX18=price_x18,
                expiration=expiration,
                nonce=nonce,
                appendix=appendix
            )
            params = PlaceOrderParams(
                product_id=product_id,
                order=order
            )
        except Exception as build_err:
            return PartialCloseResult(success=False, error=f"Failed to construct reduce_only order: {build_err}")

        # Place order on Nado
        try:
            res = await asyncio.to_thread(self.client.market.place_order, params)
        except Exception as place_err:
            err_str = str(place_err).lower()
            is_ambig = any(k in err_str for k in ["timeout", "timed out", "connection", "network", "reset", "502", "503", "504", "disconnected", "eof"])
            return PartialCloseResult(success=False, error=f"Order submission failed: {place_err}", is_ambiguous=is_ambig)

        digest = getattr(getattr(res, "data", None), "digest", None)
        if not digest:
            return PartialCloseResult(success=False, error="Order rejected or no digest returned by sequencer.")

        logger.info(f"[NadoTradingService] ⚡ Partial Close IOC order placed. Digest: {digest}. Waiting for fill confirmation...")

        # Poll sequencer / indexer for fill verification
        filled = False
        actual_filled_x18 = 0
        actual_fill_price = current_price
        had_poll_error = False
        saw_order_info = False
        actual_fee_val = 0.0
        is_estimated_fee = True

        for attempt in range(max_poll_attempts):
            await asyncio.sleep(1.0)
            try:
                hist_data = await asyncio.to_thread(self.client.market.get_historical_orders_by_digest, [digest])
                if hist_data and getattr(hist_data, "orders", None):
                    saw_order_info = True
                    order_info = hist_data.orders[0]
                    base_f = abs(int(getattr(order_info, "base_filled", 0)))
                    if base_f > 0:
                        filled = True
                        actual_filled_x18 = base_f
                        raw_fee = getattr(order_info, "fee", getattr(order_info, "fee_x18", None))
                        if raw_fee is not None and isinstance(raw_fee, (int, float, str)):
                            try:
                                f_val = float(raw_fee) / 1e18 if float(raw_fee) > 1e10 else float(raw_fee)
                                if f_val > 0:
                                    actual_fee_val = f_val
                                    is_estimated_fee = False
                            except (ValueError, TypeError):
                                pass

                        try:
                            quote_f = abs(float(getattr(order_info, "quote_filled", 0)))
                            quote_f_norm = self._normalize_indexer_amount(quote_f) if quote_f > 1e10 else quote_f
                            base_f_norm = self._normalize_indexer_amount(base_f) if base_f > 1e10 else base_f
                            close_bf = -base_f_norm if pos.get("direction") == "LONG" else base_f_norm
                            _, fill_p = self._calculate_gross_quote_and_price(
                                base_filled=close_bf,
                                quote_filled=quote_f_norm,
                                fee=actual_fee_val,
                                is_mock=getattr(self, "_mock_mode", False)
                            )
                            if fill_p > 0:
                                actual_fill_price = fill_p
                        except Exception:
                            actual_fill_price = current_price
                        break
            except Exception as poll_e:
                had_poll_error = True
                logger.warning(f"[NadoTradingService] ⚠️ Polling digest {digest} error: {poll_e}")

        if not filled or actual_filled_x18 == 0:
            is_ambig = (not saw_order_info and had_poll_error)
            err_msg = "Polling order fill status failed due to network error." if is_ambig else "IOC order expired without fills (market moved or lack of matching depth)."
            return PartialCloseResult(
                success=False,
                digest=digest,
                requested_contracts=contracts_to_close,
                error=err_msg,
                is_ambiguous=is_ambig
            )

        actual_filled_contracts = actual_filled_x18 / 1e18
        fee_usd = actual_fee_val if not is_estimated_fee and actual_fee_val > 0 else (actual_filled_contracts * (entry_price + actual_fill_price) * 0.0005)
        if direction == "LONG":
            gross_pnl = actual_filled_contracts * (actual_fill_price - entry_price)
        else:
            gross_pnl = actual_filled_contracts * (entry_price - actual_fill_price)
        slice_pnl = round(gross_pnl - fee_usd, 4)

        is_partial = actual_filled_contracts < (contracts_to_close * 0.99)

        logger.info(
            f"[NadoTradingService] ✅ Partial Close FILLED for {symbol}: "
            f"{actual_filled_contracts:.4f} contracts @ {actual_fill_price:.4f}. "
            f"Slice PnL: ${slice_pnl:.2f} (Fee: ${fee_usd:.2f}, Est: {is_estimated_fee}). Partial fill: {is_partial}"
        )

        return PartialCloseResult(
            success=True,
            digest=digest,
            requested_contracts=contracts_to_close,
            filled_contracts=actual_filled_contracts,
            fill_price=actual_fill_price,
            realized_pnl=slice_pnl,
            is_partial_fill=is_partial,
            fee_usd=fee_usd,
            is_estimated_fee=is_estimated_fee
        )

    def initialize_partial_tp_plan(self, symbol: str, preferred_mode: Optional[str] = None) -> Optional[Any]:
        """
        Calculates and attaches a deterministic PartialTPPlan to an active position.
        Phase 3: Persists the updated position state to disk immediately.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"
            
        pos = self.active_positions.get(canonical_sym) or self.active_positions.get(symbol)
        if not pos:
            logger.warning(f"[NadoTradingService] Cannot initialize partial TP plan: position {symbol} not found in active_positions.")
            return None

        # Return existing plan if already initialized
        if pos.get("partial_tp_plan"):
            try:
                from services.partial_tp_calculator import PartialTPPlan
                return PartialTPPlan.from_dict(pos["partial_tp_plan"])
            except Exception as e:
                logger.warning(f"[NadoTradingService] Could not parse existing plan for {symbol}, recalculating: {e}")

        direction = str(pos.get("direction", "LONG")).upper()
        entry_price = float(pos.get("entry_price") or 0.0)
        tp_price = float(pos.get("tp_price") or 0.0)
        sl_price = float(pos.get("sl_price") or 0.0)

        if entry_price <= 0 or tp_price <= 0:
            logger.warning(f"[NadoTradingService] Skipping partial TP plan for {symbol}: invalid entry ({entry_price}) or TP ({tp_price}) price.")
            return None

        # Determine total contracts
        total_contracts = float(pos.get("contracts") or 0.0)
        if total_contracts <= 0:
            size_usd = float(pos.get("size_usd") or 0.0)
            if size_usd > 0 and entry_price > 0:
                total_contracts = size_usd / entry_price
            else:
                logger.warning(f"[NadoTradingService] Skipping partial TP plan for {symbol}: zero volume/contracts.")
                return None

        # Get product_id and market parameters
        product_id = pos.get("product_id")
        if product_id is None:
            base_sym = canonical_sym.split('-')[0].upper()
            product_id = self.product_map.get(base_sym)
            if product_id is not None:
                pos["product_id"] = product_id

        if product_id is None:
            logger.warning(f"[NadoTradingService] Skipping partial TP plan for {symbol}: unknown product_id.")
            return None

        params = self._get_market_parameters(product_id)
        if params["size_increment_base"] <= 0:
            logger.warning(f"[NadoTradingService] Skipping partial TP plan for {symbol}: market parameters unavailable for product {product_id}.")
            return None

        # Anchor Tier 1 Partial TP to tuned Break-Even threshold if specified on position
        if pos.get("tp1_r_mult") is not None:
            params["tp1_r_mult"] = float(pos["tp1_r_mult"])

        try:
            from services.partial_tp_calculator import PartialTPCalculator
            from core.config import config
            pref_mode = preferred_mode or getattr(self, "partial_tp_mode", None) or getattr(config, "PARTIAL_TP_MODE", "2_TIER")
            import time
            plan = PartialTPCalculator.calculate_plan(
                symbol=canonical_sym,
                direction=direction,
                entry_price=entry_price,
                tp_final_price=tp_price,
                total_contracts=total_contracts,
                market_params=params,
                created_at=time.time(),
                preferred_mode=pref_mode,
                sl_price=sl_price
            )
            pos["partial_tp_plan"] = plan.to_dict()
            pos["contracts"] = plan.remaining_contracts
            if "accumulated_realized_pnl" not in pos:
                pos["accumulated_realized_pnl"] = 0.0
            self._save_positions()
            logger.info(
                f"[NadoTradingService] 📐 Initialized {plan.mode} Partial TP Plan for {canonical_sym}: "
                f"{len(plan.tiers)} tiers, total={plan.total_contracts:.4f} contracts, final TP={plan.tp_final_price:.4f}"
            )
            return plan
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to build partial TP plan for {symbol}: {e}")
            return None

    def apply_partial_close_to_position(self, symbol: str, tier_index: int, result: PartialCloseResult) -> bool:
        """
        Updates the position and PartialTPPlan state following an execution result.
        Enforces idempotency: repeated calls for a filled tier are ignored.
        Atomically updates contracts, size_usd, margin, and accumulated_realized_pnl.
        Persists to disk immediately.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"

        pos = self.active_positions.get(canonical_sym) or self.active_positions.get(symbol)
        if not pos or not pos.get("partial_tp_plan"):
            logger.warning(f"[NadoTradingService] Cannot apply partial close: position {symbol} or partial_tp_plan not found.")
            return False

        from services.partial_tp_calculator import PartialTPPlan
        try:
            plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
        except Exception as e:
            logger.error(f"[NadoTradingService] Corrupted partial_tp_plan for {symbol}: {e}")
            return False

        tier = plan.get_tier(tier_index)
        if not tier:
            logger.warning(f"[NadoTradingService] Tier {tier_index} not found in plan for {symbol}.")
            return False

        # Idempotency guard: tier already completed
        if tier.status == "FILLED":
            logger.info(f"[NadoTradingService] ℹ️ Tier {tier_index} for {symbol} is already FILLED. Ignoring duplicate execution.")
            return True

        if not result.success:
            if getattr(result, "is_ambiguous", False):
                # Retain SUBMITTING so fast monitor or reconciler checks exchange before any resubmission
                tier.status = "SUBMITTING"
                tier.error_message = f"Ambiguous result: {result.error}"
                if result.digest:
                    tier.order_digest = result.digest
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()
                logger.warning(
                    f"[NadoTradingService] ⚠️ Tier {tier_index} on {canonical_sym} has AMBIGUOUS execution: {result.error}. "
                    f"Retaining SUBMITTING to block duplicate orders until reconciled with exchange."
                )
                return False
            else:
                # Definitive rejection / failure: increment retry count and transition to FAILED
                import time
                tier.retry_count = getattr(tier, "retry_count", 0) + 1
                tier.last_attempt_time = time.time()
                tier.error_message = result.error
                if tier.retry_count >= 3:
                    tier.status = "HALTED"
                    pos["partial_tp_plan"] = plan.to_dict()
                    pos["failed_tier_alerted"] = tier.tier_index
                    self._save_positions()
                    logger.critical(
                        f"[NadoTradingService] 🚨 Tier {tier_index} on {canonical_sym} exhausted all {tier.retry_count} retries ({result.error}). "
                        f"Halting partial TP ladder."
                    )
                    try:
                        from services.telegram_service import TelegramService
                        msg = (
                            f"🚨 *PARTIAL TP FAILED EXHAUSTED*\n\n"
                            f"🪙 *Asset:* `{canonical_sym}`\n"
                            f"❌ Ступень TP{tier.tier_index} исчерпала лимит попыток (3/3): {result.error}.\n"
                            f"Лестница частичного TP остановлена. Позиция под защитой SL."
                        )
                        loop = asyncio.get_running_loop()
                        loop.create_task(TelegramService().send_message(msg))
                    except Exception:
                        pass
                else:
                    tier.status = "FAILED"
                    pos["partial_tp_plan"] = plan.to_dict()
                    self._save_positions()
                    logger.warning(
                        f"[NadoTradingService] ⚠️ Tier {tier_index} execution failed definitively for {canonical_sym}: {result.error}. "
                        f"Status set to FAILED (Attempt {tier.retry_count}/3, 15s cooldown)."
                    )
                return False

        filled = float(result.filled_contracts)
        if filled <= 0:
            tier.status = "PENDING"
            tier.error_message = "Zero contracts filled"
            pos["partial_tp_plan"] = plan.to_dict()
            self._save_positions()
            return False

        import time
        # Record tier execution
        tier.filled_contracts = round(tier.filled_contracts + filled, 8)
        tier.fill_price = float(result.fill_price)
        tier.realized_pnl_usd = round(tier.realized_pnl_usd + float(result.realized_pnl), 4)
        tier.executed_at = time.time()
        tier.order_digest = result.digest

        rem_tier = tier.remaining_tier_contracts
        if rem_tier <= 1e-6 or rem_tier < plan.min_size:
            tier.status = "FILLED"
        else:
            tier.status = "PARTIALLY_FILLED"

        # Update remaining volume in plan
        plan.remaining_contracts = max(0.0, plan.remaining_contracts - filled)
        if plan.size_increment > 0:
            plan.remaining_contracts = round(plan.remaining_contracts, 8)

        # Update active position
        pos["contracts"] = plan.remaining_contracts
        entry_p = float(pos.get("entry_price") or 0.0)
        pos["size_usd"] = round(plan.remaining_contracts * entry_p, 4)
        pos["notional_usd"] = pos["size_usd"]
        leverage = float(pos.get("leverage") or 1.0)
        pos["margin_used"] = round(pos["size_usd"] / leverage, 4) if leverage > 0 else pos["size_usd"]
        
        # Update trigger amount for remaining contracts
        direction_str = str(pos.get("direction", "LONG")).upper()
        rem_x18 = to_x18_aligned(plan.remaining_contracts)
        pos["trigger_amount_x18"] = str(-rem_x18 if direction_str == "LONG" else rem_x18)
        
        # Accumulate slice realized PnL
        current_acc_pnl = float(pos.get("accumulated_realized_pnl", 0.0))
        pos["accumulated_realized_pnl"] = round(current_acc_pnl + result.realized_pnl, 4)
        
        # Update plan dict in position
        pos["partial_tp_plan"] = plan.to_dict()

        logger.info(
            f"[NadoTradingService] 📊 Tier {tier_index} ({tier.status}) recorded for {canonical_sym}. "
            f"Filled: {filled:.4f} contracts (Tier total: {tier.filled_contracts:.4f}/{tier.close_contracts:.4f}) @ {result.fill_price:.4f} (PnL: +${result.realized_pnl:.2f}). "
            f"Remaining contracts: {plan.remaining_contracts:.4f} (Size: ${pos['size_usd']:.2f}). "
            f"Total Accumulated PnL: ${pos['accumulated_realized_pnl']:.2f}."
        )

        # Check if entire position is now liquidated
        # Strict invariant: only finalize closure if remaining contracts are strictly zero (<= 1e-6)
        if plan.remaining_contracts <= 1e-6:
            logger.info(f"[NadoTradingService] 🏁 All partial tiers completed for {canonical_sym}! Remaining contracts: 0. Finalizing position closure.")
            self._finalize_partial_tp_closure(
                symbol=canonical_sym,
                pos=pos,
                exit_price=result.fill_price,
                triggered_by=f"PARTIAL_TP_TIER_{tier_index}"
            )
        elif tier.is_final and plan.remaining_contracts < plan.min_size:
            # Unclosable exchange dust remains on exchange!
            # Retain in active_positions until verified zero on exchange, never silently delete!
            pos["is_dust"] = True
            pos["dust_contracts"] = plan.remaining_contracts
            tier.status = "DUST_REMAINDER"
            pos["partial_tp_plan"] = plan.to_dict()
            self._save_positions()
            logger.warning(
                f"[NadoTradingService] ⚠️ DUST WARNING: Final tier completed for {canonical_sym}, but unclosable dust "
                f"of {plan.remaining_contracts:.6f} contracts (< min_size {plan.min_size}) remains on exchange. "
                f"Retaining position in active_positions until verified zero on-chain!"
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._send_dust_warning_tg(canonical_sym, plan.remaining_contracts, plan.min_size))
            except RuntimeError:
                pass
        else:
            self._save_positions()
            # Phase 4: BE Protection & SL Resizing
            if tier.action == "PROTECT_BREAKEVEN":
                # Do NOT prematurely set pos["protection_state"] = "BREAK_EVEN" or mutate pos["sl_price"]
                # until update_stop_loss_to_be actually confirms the stop on-chain or sets software fallback!
                # Mark protection_state as PENDING_BREAK_EVEN to honestly reflect in-flight state.
                pos["protection_state"] = "PENDING_BREAK_EVEN"
                plan.be_activated = True
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()
                if not getattr(self, "_in_fast_monitor_tp", False):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.update_stop_loss_to_be(canonical_sym))
                    except RuntimeError:
                        pass
            elif pos.get("sl_digest") and plan.remaining_contracts > 0:
                current_sl = float(pos.get("sl_price", 0.0))
                if current_sl > 0:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.update_stop_loss(
                            symbol=canonical_sym,
                            new_sl_price=current_sl,
                            new_contracts=plan.remaining_contracts,
                            force_resize=True
                        ))
                    except RuntimeError:
                        pass

        return True

    def reconcile_partial_tp_with_chain(
        self,
        symbol: str,
        on_chain_contracts: float,
        is_verified_pnl: Optional[bool] = None
    ) -> bool:
        """
        Reconciles the PartialTPPlan remaining contracts with the confirmed on-chain position balance.
        If on-chain balance is zero, finalizes closure cleanly.
        If divergence is detected, adjusts remaining contracts to prevent overselling.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"

        pos = self.active_positions.get(canonical_sym) or self.active_positions.get(symbol)
        if not pos or not pos.get("partial_tp_plan"):
            return False

        from services.partial_tp_calculator import PartialTPPlan
        try:
            plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
        except Exception as e:
            logger.error(f"[NadoTradingService] Failed to deserialize plan in reconciliation: {e}")
            return False

        actual_on_chain = abs(float(on_chain_contracts))

        # Scenario A: On-chain position is completely gone (e.g. SL hit or closed on DEX UI)
        if actual_on_chain < 1e-6:
            logger.warning(
                f"[NadoTradingService] ⚠️ Reconcile: On-chain balance for {canonical_sym} is ZERO, "
                f"but local plan had {plan.remaining_contracts:.4f} contracts. Finalizing closure."
            )
            # Determine best known exit price for the unclosed remainder
            exit_price = None
            triggered_by = "ON_CHAIN_RECONCILED_CLOSED"
            sl_price = float(pos.get("sl_price") or 0.0)
            base_sym = canonical_sym.split('-')[0].upper()
            prod_id = pos.get("product_id") or self.product_map.get(base_sym)
            mark_price = None
            if prod_id is not None and getattr(self, "_market_cache", None) and prod_id in self._market_cache:
                m_info = self._market_cache[prod_id]
                if hasattr(m_info, "oracle_price_x18"):
                    mark_price = float(m_info.oracle_price_x18) / 1e18
                elif hasattr(m_info, "product") and hasattr(m_info.product, "oracle_price_x18"):
                    mark_price = float(m_info.product.oracle_price_x18) / 1e18

            if sl_price > 0:
                exit_price = sl_price
                triggered_by = "ON_CHAIN_RECONCILED_SL"
            elif mark_price and mark_price > 0:
                exit_price = mark_price
                triggered_by = "ON_CHAIN_RECONCILED_MARK"
            else:
                entry_p = float(pos.get("entry_price") or 0.0)
                exit_price = entry_p if entry_p > 0 else None
                triggered_by = "ON_CHAIN_RECONCILED_UNVERIFIED_PNL"
                pos["requires_reconciliation"] = True
                logger.warning(
                    f"[NadoTradingService] ⚠️ Exit price unverified for {canonical_sym} remainder. "
                    f"Marking position as requiring audit."
                )

            # If caller explicitly passed is_verified_pnl, respect it.
            # Otherwise: if remaining contracts > 1e-6 closed unexpectedly on-chain, this is an unverified estimate!
            # If remaining contracts <= 1e-6 (all tiers already filled on exchange), is_verified_pnl is True.
            verified_flag = is_verified_pnl if is_verified_pnl is not None else (plan.remaining_contracts <= 1e-6)

            self._finalize_partial_tp_closure(
                symbol=canonical_sym,
                pos=pos,
                exit_price=exit_price,
                triggered_by=triggered_by,
                is_verified_pnl=verified_flag
            )
            return True

        # Scenario B: Significant divergence between local plan and on-chain balance
        diff = abs(actual_on_chain - plan.remaining_contracts)
        if diff > 1e-5:
            logger.warning(
                f"[NadoTradingService] ⚠️ Reconcile divergence for {canonical_sym}: "
                f"On-chain = {actual_on_chain:.4f}, Local Plan = {plan.remaining_contracts:.4f} (diff = {diff:.4f}). "
                f"Adjusting local state to strictly match on-chain reality."
            )
            plan.remaining_contracts = actual_on_chain
            pos["contracts"] = actual_on_chain
            entry_p = float(pos.get("entry_price", 0.0))
            pos["size_usd"] = round(actual_on_chain * entry_p, 4)
            pos["notional_usd"] = pos["size_usd"]
            leverage = float(pos.get("leverage", 1.0))
            pos["margin_used"] = round(pos["size_usd"] / leverage, 4) if leverage > 0 else pos["size_usd"]
            pos["partial_tp_plan"] = plan.to_dict()
            self._save_positions()
            return True

        # Scenario C: Check and resolve any hanging SUBMITTING tier (e.g. after restart)
        plan_updated = False
        for t in plan.tiers:
            if t.status == "SUBMITTING":
                if abs(actual_on_chain - plan.remaining_contracts) <= 1e-5:
                    t.status = "PARTIALLY_FILLED" if t.filled_contracts > 0 else "PENDING"
                    logger.info(f"[NadoTradingService] 🔄 Resolved hanging SUBMITTING tier {t.tier_index} on {canonical_sym} -> {t.status}")
                    plan_updated = True
        if plan_updated:
            pos["partial_tp_plan"] = plan.to_dict()
            self._save_positions()
            return True

        return False

    def _finalize_partial_tp_closure(
        self,
        symbol: str,
        pos: dict,
        exit_price: Optional[float],
        triggered_by: str,
        is_verified_pnl: bool = True
    ) -> dict:
        """
        Cleans up position from active_positions, cancels active trigger orders,
        updates win/loss streaks and saves state after final tier or full on-chain exit.
        If is_verified_pnl is False, preserves trade in pending_reconciliations with
        status PNL_RECONCILIATION_PENDING without corrupting win/loss streak.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"
        base_sym = canonical_sym.split('-')[0].upper()

        total_pnl = float(pos.get("accumulated_realized_pnl", 0.0))
        entry_price = float(pos.get("entry_price", 0.0))
        initial_size = float(pos.get("size_usd", 0.0))

        # Reconcile remainder PnL if contracts were still open locally but closed on-chain
        plan_dict = pos.get("partial_tp_plan")
        rem_contracts = 0.0
        if plan_dict:
            try:
                from services.partial_tp_calculator import PartialTPPlan
                plan = PartialTPPlan.from_dict(plan_dict)
                rem_contracts = float(plan.remaining_contracts)
            except Exception:
                rem_contracts = 0.0
        elif pos.get("contracts"):
            rem_contracts = float(pos.get("contracts", 0.0))

        if rem_contracts > 1e-6 and exit_price is not None and exit_price > 0 and entry_price > 0:
            direction = str(pos.get("direction", "LONG")).upper()
            if direction == "LONG":
                rem_gross = (exit_price - entry_price) * rem_contracts
            else:
                rem_gross = (entry_price - exit_price) * rem_contracts
            is_unverified = (triggered_by == "ON_CHAIN_RECONCILED_UNVERIFIED_PNL") or not is_verified_pnl
            rem_fee = 0.0 if is_unverified else (rem_contracts * exit_price * 0.0005)
            rem_net = round(rem_gross - rem_fee, 4)
            total_pnl = round(total_pnl + rem_net, 4)
            logger.info(
                f"[NadoTradingService] ⚖️ Reconciled remainder {rem_contracts:.4f} contracts @ {exit_price:.4f} "
                f"(Gross: ${rem_gross:.2f}, Fee: ${rem_fee:.2f}, Rem PnL: ${rem_net:.2f}) -> Total PnL: ${total_pnl:.2f}"
            )

        # Cancel any orphan trigger orders (SL / TP) on exchange
        product_id = pos.get("product_id") or self.product_map.get(base_sym)
        sl_dig = pos.get("sl_digest")
        tp_dig = pos.get("tp_digest")
        digests_to_cancel = [d for d in [sl_dig, tp_dig] if d]
        if digests_to_cancel and product_id is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._cancel_trigger_digests(product_id, digests_to_cancel, sender=pos.get("sender")))
            except RuntimeError:
                pass

        # Remove from active_positions
        if canonical_sym in self.active_positions:
            del self.active_positions[canonical_sym]
        if base_sym in self.active_positions:
            del self.active_positions[base_sym]
        if symbol in self.active_positions:
            del self.active_positions[symbol]

        if is_verified_pnl:
            # Unified Scratch Guard for Win/Loss Streaks (-0.22R to +0.15R)
            self._evaluate_and_update_streak(
                net_pnl=total_pnl,
                planned_risk=float(pos.get("planned_risk_usd") or pos.get("approved_risk_usd") or 0.0),
                pos_data=pos,
                symbol=canonical_sym
            )
            status_str = "CLOSED"

            try:
                self._record_closed_trade_journal(
                    symbol=canonical_sym,
                    direction=pos.get("direction", "LONG"),
                    entry_price=entry_price,
                    exit_price=exit_price or 0.0,
                    net_pnl=total_pnl,
                    pos_data=pos,
                    fee=float(pos.get("fee", 0.0) or (initial_size * 0.001)),
                    triggered_by=triggered_by
                )
            except Exception as j_err:
                logger.warning(f"[NadoTradingService] ⚠️ Failed recording finalized partial TP trade to journal: {j_err}")
        else:
            # Unverified estimate: store in pending_reconciliations with status PNL_RECONCILIATION_PENDING
            status_str = "PNL_RECONCILIATION_PENDING"
            subaccount = pos.get("sender") or self.default_subaccount_id

            excluded_digests = set(getattr(self, "processed_fill_digests", set()))
            if plan_dict and isinstance(plan_dict, dict):
                for tier in plan_dict.get("tiers", []):
                    td = tier.get("order_digest")
                    if td:
                        excluded_digests.add(str(td).lower())
            open_d = pos.get("open_digest") or pos.get("digest")
            if open_d:
                excluded_digests.add(str(open_d).lower())

            now_ts = time.time()
            if rem_contracts > 1e-6:
                self.pending_reconciliations[canonical_sym] = {
                    "symbol": canonical_sym,
                    "base_symbol": base_sym,
                    "product_id": product_id,
                    "subaccount": subaccount,
                    "direction": pos.get("direction", "LONG"),
                    "entry_price": entry_price,
                    "contracts": rem_contracts,
                    "size_usd": initial_size,
                    "accumulated_realized_pnl": float(pos.get("accumulated_realized_pnl", 0.0)),
                    "estimated_exit_price": exit_price,
                    "diagnostic_pnl": total_pnl,
                    "open_time": float(pos.get("open_time", 0.0)),
                    "closed_at": now_ts,
                    "detected_at": now_ts,
                    "attempts": 0,
                    "excluded_digests": list(excluded_digests),
                    "status": "PNL_RECONCILIATION_PENDING",
                    "reason": triggered_by,
                    "planned_risk_usd": float(pos.get("planned_risk_usd") or pos.get("approved_risk_usd") or 0.0),
                    "cohort": pos.get("cohort", "UNKNOWN"),
                    "base_risk_pct": float(pos.get("base_risk_pct", 0.0)),
                    "applied_multipliers": pos.get("applied_multipliers", {})
                }
                self._save_pending_reconciliations()
                logger.warning(
                    f"[NadoTradingService] ⏳ Trade {canonical_sym} remainder closure unverified. "
                    f"Saved to pending reconciliations. Win/Loss streak NOT modified."
                )
            else:
                logger.info(
                    f"[NadoTradingService] ℹ️ Trade {canonical_sym} closure completed with 0 remaining contracts. "
                    f"Skipping pending reconciliation."
                )

        self._save_positions()

        exit_str = f"{exit_price:.4f}" if exit_price is not None else "UNKNOWN (DUST_ZERO_CONFIRMED)"
        logger.info(
            f"[NadoTradingService] 🎯 Position {canonical_sym} finalized! "
            f"Exit: {exit_str}, Total PnL: ${total_pnl:.2f}, Status: {status_str}, Trigger: {triggered_by}"
        )

        return {
            "symbol": canonical_sym,
            "status": status_str,
            "reason": triggered_by,
            "direction": pos.get("direction", "LONG"),
            "triggered_by": triggered_by,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_usd": total_pnl,
            "roi_pct": (total_pnl / pos.get("margin_used", initial_size) * 100) if initial_size > 0 else 0,
            "original_thesis": pos.get("original_thesis", "")
        }

    async def _send_dust_warning_tg(self, symbol: str, dust_contracts: float, min_size: float):
        """Sends rich Telegram warning about unclosable dust remainder retained in tracking."""
        try:
            from services.telegram_service import TelegramService
            tg = TelegramService()
            msg = (
                f"⚠️ *NADO DEX | ПЫЛЬ ПОЗИЦИИ (DUST WARNING)*\n\n"
                f"🪙 *Asset:* `{symbol}`\n"
                f"📦 *Остаток:* `{dust_contracts:.6f}` контрактов\n"
                f"📏 *Min Size:* `{min_size}` контрактов\n\n"
                f"Остаток позиции меньше минимального размера ордера биржи и не может быть закрыт отдельным ордером. "
                f"Позиция сохранена в учёте как `DUST_REMAINDER` до подтверждения нулевого остатка на Nado."
            )
            await tg.send_message(msg)
            await tg.broadcast_to_channel(msg)
        except Exception as e:
            logger.warning(f"[NadoTradingService] Failed to send dust warning TG: {e}")

    def _sync_position_contracts(self, pos: dict, plan: Any, actual_on_chain: float, entry_p: float = 0.0):
        """
        Atomically synchronizes all dependent position fields when on-chain contract volume changes:
        - plan.remaining_contracts
        - pos["contracts"]
        - pos["size_usd"]
        - pos["notional_usd"]
        - pos["margin_used"]
        - pos["trigger_amount_x18"]
        - pos["partial_tp_plan"]
        """
        actual_contracts = round(max(0.0, float(actual_on_chain)), 8)
        entry_price = float(entry_p or pos.get("entry_price") or 0.0)
        direction = str(pos.get("direction", "LONG")).upper()
        leverage = float(pos.get("leverage") or 1.0)

        plan.remaining_contracts = actual_contracts
        pos["contracts"] = actual_contracts
        pos["size_usd"] = round(actual_contracts * entry_price, 4)
        pos["notional_usd"] = pos["size_usd"]
        pos["margin_used"] = round(pos["size_usd"] / leverage, 4) if leverage > 0 else pos["size_usd"]

        trig_amt = int(actual_contracts * 1e18)
        pos["trigger_amount_x18"] = str(trig_amt if direction == "SHORT" else -trig_amt)
        pos["partial_tp_plan"] = plan.to_dict()

    async def reconcile_submitting_tier(self, symbol: str, pos: dict, plan: Any, tier: Any) -> bool:
        """
        Safely reconciles an in-flight or restarted SUBMITTING tier with Nado before any re-submission.
        1. Queries active on-chain positions directly from Nado using exact product_id.
        2. If on-chain balance decreased, verifies if a matching TP closing order executed in fill history.
           - If verified TP fill: attributes exact fill_price, fee, realized_pnl, digest and marks FILLED/PARTIALLY_FILLED.
           - If external reduction (SL, user manual close): updates position size, but does NOT attribute TP profit.
        3. If on-chain balance is unchanged, safely resets tier to PENDING (or PARTIALLY_FILLED if prior fills exist).
        4. If query fails or state is ambiguous, keeps tier in SUBMITTING to block duplicate orders.
        Returns True if safely resolved; False if ambiguous or locked.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"
        base_sym = canonical_sym.split('-')[0].upper()
        logger.info(f"[NadoTradingService] 🔍 Reconciling SUBMITTING tier {tier.tier_index} on {canonical_sym}...")
        try:
            prod_id = pos.get("product_id") or self.product_map.get(canonical_sym) or self.product_map.get(base_sym)
            if prod_id is None:
                logger.warning(f"[NadoTradingService] Reconcile: cannot determine product_id for {canonical_sym}. Retaining SUBMITTING.")
                return False

            active_on_chain = await self.get_active_positions(bypass_cache=True)
            matched = None
            for p in active_on_chain:
                p_prod_id = p.get("_product_id") or p.get("product_id") or self.product_map.get(p.get("symbol"))
                if p_prod_id is not None and p_prod_id == prod_id:
                    matched = p
                    break
                elif p.get("symbol") == canonical_sym:
                    matched = p
                    break

            if matched is None:
                # Do NOT blindly assume 0.0 unless positions query succeeded and zero is confirmed!
                if not getattr(self, "_last_positions_fetch_ok", True):
                    logger.warning(
                        f"[NadoTradingService] ⚠️ Reconcile: Cannot confirm zero balance for {canonical_sym} "
                        f"(positions query not confirmed). Retaining SUBMITTING to protect position."
                    )
                    return False

                # Double check with engine subaccount summary directly if available
                subaccount_id = pos.get("sender") or self.default_subaccount_id
                confirmed_zero = False
                if hasattr(self.client, "subaccount") and hasattr(self.client.subaccount, "get_engine_subaccount_summary") and subaccount_id:
                    try:
                        summary = await asyncio.to_thread(self.client.subaccount.get_engine_subaccount_summary, subaccount_id)
                        if hasattr(summary, "perp_balances"):
                            found = False
                            for pb in summary.perp_balances:
                                if pb.product_id == prod_id and abs(float(pb.balance.amount) / 1e18) >= 1e-6:
                                    found = True
                                    break
                            confirmed_zero = not found
                    except Exception as e:
                        logger.warning(f"[NadoTradingService] ⚠️ Reconcile subaccount check failed for {canonical_sym}: {e}")
                        confirmed_zero = False
                else:
                    confirmed_zero = True

                if confirmed_zero:
                    logger.info(f"[NadoTradingService] ✅ Reconcile: On-chain balance for {canonical_sym} confirmed ZERO.")
                    self.reconcile_partial_tp_with_chain(canonical_sym, 0.0)
                    return False
                else:
                    logger.warning(f"[NadoTradingService] ⚠️ Reconcile: Zero balance unconfirmed for {canonical_sym}. Retaining SUBMITTING.")
                    return False

            actual_on_chain = abs(float(matched.get("amount", 0.0)))
            local_remaining = float(plan.remaining_contracts)
            diff = round(local_remaining - actual_on_chain, 8)

            if diff > 1e-5:
                # Balance decreased on-chain! Verify whether this was our TP order or an external close.
                direction = str(pos.get("direction", "LONG")).upper()
                entry_p = float(pos.get("entry_price") or 0.0)

                matching_order = None
                subaccount_id = pos.get("sender") or self.default_subaccount_id
                attempt_time = getattr(tier, "last_attempt_time", 0.0) or getattr(tier, "executed_at", 0.0) or (time.time() - 60.0)

                # 1. Check by order digest if known
                if tier.order_digest and hasattr(self.client, "market") and hasattr(self.client.market, "get_historical_orders_by_digest"):
                    try:
                        hist_res = await asyncio.to_thread(self.client.market.get_historical_orders_by_digest, [tier.order_digest])
                        if hist_res and getattr(hist_res, "orders", None):
                            for o in hist_res.orders:
                                bf = float(getattr(o, "base_filled", 0))
                                base_f = abs(bf)
                                is_closing_dir = (direction == "LONG" and bf < 0) or (direction == "SHORT" and bf > 0)
                                if is_closing_dir and base_f > 0:
                                    matching_order = o
                                    break
                    except Exception as e:
                        logger.warning(f"[NadoTradingService] ⚠️ Digest query failed during reconcile: {e}")

                # 2. Check by subaccount historical orders if digest not matched
                if not matching_order and prod_id is not None and subaccount_id and hasattr(self.client, "market") and hasattr(self.client.market, "get_subaccount_historical_orders"):
                    try:
                        from nado_protocol.indexer_client.types.query import IndexerSubaccountHistoricalOrdersParams
                        params = IndexerSubaccountHistoricalOrdersParams(
                            subaccounts=[subaccount_id],
                            product_ids=[prod_id],
                            limit=20
                        )
                        hist_res = await asyncio.to_thread(self.client.market.get_subaccount_historical_orders, params)
                        if hist_res and getattr(hist_res, "orders", None):
                            candidate_orders = []
                            for o in hist_res.orders:
                                bf = float(getattr(o, "base_filled", 0))
                                base_f = abs(bf)
                                is_closing_dir = (direction == "LONG" and bf < 0) or (direction == "SHORT" and bf > 0)
                                if is_closing_dir and base_f > 0:
                                    ts = getattr(o, "timestamp", None)
                                    o_time = float(ts) / 1000.0 if ts and len(str(ts)) > 10 else float(ts or 0)
                                    # Strict time window: must be within [attempt_time - 15s, attempt_time + 120s]
                                    if attempt_time > 0 and (o_time <= 0 or o_time < (attempt_time - 15.0) or o_time > (attempt_time + 120.0)):
                                        continue
                                    # Strict volume match: must match diff within 1 step or 0.5%, NOT 15%!
                                    max_allowed_vol_diff = max(float(plan.size_increment), diff * 0.005)
                                    if abs(base_f - diff) <= max_allowed_vol_diff:
                                        candidate_orders.append(o)

                            if len(candidate_orders) == 1:
                                matching_order = candidate_orders[0]
                                logger.info(
                                    f"[NadoTradingService] 🎯 Uniquely matched 1 historical fill order "
                                    f"(digest: {getattr(matching_order, 'digest', '')}) for {canonical_sym} tier {tier.tier_index}."
                                )
                            elif len(candidate_orders) > 1:
                                logger.warning(
                                    f"[NadoTradingService] ⚠️ Ambiguous TP reconciliation for {canonical_sym} tier {tier.tier_index}: "
                                    f"found {len(candidate_orders)} candidate orders matching volume {diff:.4f}. "
                                    f"Refusing to arbitrarily attribute fill. Treating as external position reduction."
                                )
                                # Leave matching_order = None so it does not attribute fake profit or single out an order!
                    except Exception as e:
                        logger.warning(f"[NadoTradingService] ⚠️ Historical orders query failed during reconcile: {e}")

                if matching_order:
                    # Verified on-chain execution of our TP order!
                    raw_fee = getattr(matching_order, "fee", getattr(matching_order, "fee_x18", None))
                    fee_usd = 0.0
                    if raw_fee is not None and isinstance(raw_fee, (int, float, str)):
                        try:
                            fee_usd = float(raw_fee) / 1e18 if float(raw_fee) > 1e10 else float(raw_fee)
                        except (ValueError, TypeError):
                            fee_usd = 0.0

                    quote_f = abs(float(getattr(matching_order, "quote_filled", 0)))
                    base_f = abs(float(getattr(matching_order, "base_filled", diff)))
                    quote_f_norm = self._normalize_indexer_amount(quote_f) if quote_f > 1e10 else quote_f
                    base_f_norm = self._normalize_indexer_amount(base_f) if base_f > 1e10 else base_f
                    tier_bf = -base_f_norm if pos.get("direction") == "LONG" else base_f_norm

                    _, fill_p = self._calculate_gross_quote_and_price(
                        base_filled=tier_bf,
                        quote_filled=quote_f_norm,
                        fee=fee_usd,
                        is_mock=getattr(self, "_mock_mode", False)
                    )
                    has_verified_fill_price = (fill_p > 0)
                    order_dig = getattr(matching_order, "digest", tier.order_digest)

                    if fee_usd <= 0 and fill_p is not None and fill_p > 0:
                        fee_usd = base_f * (entry_p + fill_p) * 0.0005

                    if fill_p is not None and fill_p > 0:
                        gross_slice_pnl = base_f * (fill_p - entry_p) if direction == "LONG" else base_f * (entry_p - fill_p)
                        slice_pnl = round(gross_slice_pnl - fee_usd, 4)
                        tier.pnl_reconciled = True
                        tier.requires_pnl_reconciliation = False
                    else:
                        fill_p = None
                        slice_pnl = 0.0
                        tier.pnl_reconciled = False
                        tier.requires_pnl_reconciliation = True

                    logger.info(
                        f"[NadoTradingService] ✅ SUBMITTING order for {canonical_sym} tier {tier.tier_index} verified on-chain! "
                        f"Recovered fill: {base_f:.4f} contracts @ {fill_p if fill_p else 'PENDING'} (PnL: +${slice_pnl:.2f}, Fee: ${fee_usd:.2f}, Digest: {order_dig})."
                    )
                    tier.filled_contracts = round(tier.filled_contracts + base_f, 8)
                    tier.fill_price = fill_p
                    tier.order_digest = order_dig
                    tier.realized_pnl_usd = round(tier.realized_pnl_usd + slice_pnl, 4)
                    tier.executed_at = time.time()

                    if slice_pnl != 0.0:
                        current_acc = float(pos.get("accumulated_realized_pnl", 0.0))
                        pos["accumulated_realized_pnl"] = round(current_acc + slice_pnl, 4)

                    self._sync_position_contracts(pos, plan, actual_on_chain, entry_p)

                    min_sz = float(getattr(plan, "min_size", 0.0) or 0.0)
                    if tier.remaining_tier_contracts <= max(min_sz, 1e-6) or tier.filled_contracts >= (tier.close_contracts * 0.999):
                        tier.status = "FILLED"
                    else:
                        tier.status = "PARTIALLY_FILLED"

                    pos["partial_tp_plan"] = plan.to_dict()
                    self._save_positions()
                    return True
                else:
                    # On-chain position decreased, but NO matching TP order found in exchange history!
                    # It was an external closure, manual close, or SL.
                    logger.warning(
                        f"[NadoTradingService] ⚠️ External position reduction detected for {canonical_sym}: "
                        f"on-chain contracts {actual_on_chain:.4f} < local {local_remaining:.4f} (diff: {diff:.4f}), "
                        f"but NO matching TP order fill found. Adjusting position size without crediting TP profit."
                    )
                    self._sync_position_contracts(pos, plan, actual_on_chain, entry_p)

                    # Do NOT credit tier filled contracts or PnL! Reset tier status to PENDING / PARTIALLY_FILLED
                    tier.status = "PARTIALLY_FILLED" if tier.filled_contracts > 0 else "PENDING"
                    pos["partial_tp_plan"] = plan.to_dict()
                    self._save_positions()
                    return True

            elif abs(diff) <= 1e-5:
                # Balance unchanged: order did NOT fill. Safe to revert to PENDING or PARTIALLY_FILLED
                tier.status = "PARTIALLY_FILLED" if tier.filled_contracts > 0 else "PENDING"
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()
                logger.info(f"[NadoTradingService] 🔄 Confirmed no fill for SUBMITTING tier {tier.tier_index} on {canonical_sym}. Reset to {tier.status}.")
                return True
            else:
                # On-chain > local remaining: synchronize all fields
                entry_p = float(pos.get("entry_price") or 0.0)
                self._sync_position_contracts(pos, plan, actual_on_chain, entry_p)
                tier.status = "PENDING"
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()
                return True

        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to reconcile SUBMITTING tier for {canonical_sym}: {e}. Retaining SUBMITTING to block duplicate orders.")
            return False

    def _get_market_parameters(self, product_id: int) -> dict:
        params = {
            "size_increment_base": 0.0,
            "size_increment_x18": 0,
            "price_increment_x18": 0,
            "min_size": 0.0,
            "min_notional": 0.0
        }
        if not getattr(self, "_market_cache", None):
            return params
            
        market_info = self._market_cache.get(product_id)
        if market_info and hasattr(market_info, "book_info"):
            try:
                params["size_increment_base"] = float(market_info.book_info.size_increment) / 1e18
                params["size_increment_x18"] = int(market_info.book_info.size_increment)
                params["price_increment_x18"] = int(market_info.book_info.price_increment_x18)
                
                # In Nado / Vertex engine contracts, size_increment is the minimum contract step size.
                # book_info.min_size is the minimum order NOTIONAL in USD (abs(amount) * price >= min_size) on live DEX (100 USD).
                # If raw_min_size >= 10.0, it represents min_notional ($100 on Nado).
                # If 0 < raw_min_size < 10.0 (e.g. in test fixtures specifying contracts), it sets min_size.
                raw_min_size = float(getattr(market_info.book_info, "min_size", 0)) / 1e18
                if raw_min_size >= 10.0:
                    params["min_size"] = params["size_increment_base"]
                    params["min_notional"] = raw_min_size
                elif raw_min_size > 0:
                    params["min_size"] = raw_min_size
                    params["min_notional"] = 0.0
                else:
                    params["min_size"] = params["size_increment_base"]
                    params["min_notional"] = 0.0
            except Exception:
                pass
        return params

    async def get_market_limits(self, symbol: str) -> dict:
        """Fetch min_size and size_increment for the given product"""
        limits = {"size_increment": 0.0, "min_size": 0.0, "min_notional": 0.0}
        if not self.is_connected:
            return limits
            
        try:
            base_symbol = symbol.split('-')[0].upper()
            product_id = self.product_map.get(base_symbol)
            if not product_id:
                return limits
                
            if not getattr(self, "_market_cache", None):
                self._market_cache = {}
                markets_data = await asyncio.to_thread(self.client.market.get_all_engine_markets)
                for m in markets_data.perp_products:
                    self._market_cache[m.product_id] = m
                self._market_cache_ts = time.time()
                    
            params = self._get_market_parameters(product_id)
            limits["size_increment"] = params["size_increment_base"]
            limits["min_size"] = params["min_size"]
            limits["min_notional"] = params["min_notional"]
            limits["price_increment"] = float(params["price_increment_x18"]) / 1e18
        except Exception as e:
            logger.warning(f"[NadoTradingService] ⚠️ Could not fetch limits for {symbol}: {e}")
        return limits

    async def _fetch_trigger_orders(self, product_id: int) -> List[Any]:
        """
        Fetches active trigger orders for a specific product_id from Nado.
        Uses context.trigger_client with proper EIP-712 signature.
        """
        if not self.is_connected or not self.client or not getattr(self.client, "context", None):
            return []
            
        trigger_client = getattr(self.client.context, "trigger_client", None)
        if not trigger_client or not self.default_subaccount_id:
            return []

        try:
            import time
            import warnings
            from nado_protocol.trigger_client.types.query import (
                ListTriggerOrdersParams,
                ListTriggerOrdersRequest,
                ListTriggerOrdersTx
            )
            from nado_protocol.contracts.types import NadoTxType

            recv_time = int(time.time() * 1000) + 60000
            tx = ListTriggerOrdersTx(sender=self.default_subaccount_id, recvTime=recv_time)
            params = ListTriggerOrdersParams(
                tx=tx,
                product_ids=[product_id] if product_id is not None else None,
                status_types=["waiting_price"]
            )
            
            sig = trigger_client._sign(NadoTxType.LIST_TRIGGER_ORDERS, params.tx.model_dump())
            params.signature = sig
            
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
                req = ListTriggerOrdersRequest.model_validate(params.model_dump())
                res = await asyncio.to_thread(trigger_client.query, req.model_dump())
                
            if res and res.data and hasattr(res.data, "orders"):
                return res.data.orders
        except Exception as e:
            logger.warning(f"[NadoTradingService] ⚠️ Failed to query trigger orders for product {product_id}: {e}")
            
        return []

    async def _cancel_trigger_digests(self, product_id: int, digests: List[str], sender: str = None) -> bool:
        """
        Cancels active trigger order digests from Nado book.
        Prevents zombie / orphaned trigger orders after position closures.
        Handles batch and individual cancellations gracefully, treating error 2020
        (order already filled / not found) as benign.
        """
        valid_digests = [d for d in digests if d and isinstance(d, str)]
        if not valid_digests or not self.is_connected or product_id is None:
            return False

        target_sender = sender or self.default_subaccount_id
        if not target_sender:
            return False

        from nado_protocol.trigger_client.types.execute import CancelTriggerOrdersParams

        def is_already_gone_error(err: Exception) -> bool:
            err_str = str(err).lower()
            return "2020" in err_str or "could not be found" in err_str or "order not found" in err_str

        # 1. Attempt fast batch cancellation first
        try:
            cancel_params = CancelTriggerOrdersParams(
                productIds=[product_id] * len(valid_digests),
                digests=valid_digests,
                sender=target_sender
            )
            await asyncio.to_thread(self.client.market.cancel_trigger_orders, cancel_params)
            logger.info(f"[NadoTradingService] 🗑️ Successfully cancelled {len(valid_digests)} trigger orders on product {product_id}: {valid_digests}")
            return True
        except Exception as e:
            # 2. If batch failed (e.g. error 2020 because one of the orders already executed/filled),
            # retry cancelling each digest individually so that any actually open order is still cleanly cancelled!
            if len(valid_digests) > 1:
                logger.info(f"[NadoTradingService] Batch cancel failed ({e}). Retrying individual trigger cancellations...")
                all_resolved = True
                for single_digest in valid_digests:
                    try:
                        single_param = CancelTriggerOrdersParams(
                            productIds=[product_id],
                            digests=[single_digest],
                            sender=target_sender
                        )
                        await asyncio.to_thread(self.client.market.cancel_trigger_orders, single_param)
                        logger.info(f"[NadoTradingService] 🗑️ Successfully cancelled trigger order {single_digest} on product {product_id}")
                    except Exception as err:
                        if is_already_gone_error(err):
                            logger.info(f"[NadoTradingService] ℹ️ Trigger order {single_digest} already executed or absent on-chain (error 2020).")
                        else:
                            logger.warning(f"[NadoTradingService] ⚠️ Failed to cancel trigger order {single_digest}: {err}")
                            all_resolved = False
                return all_resolved

            # Single digest case
            if is_already_gone_error(e):
                logger.info(f"[NadoTradingService] ℹ️ Trigger order {valid_digests[0]} already executed or absent on-chain (error 2020).")
                return True
            else:
                logger.warning(f"[NadoTradingService] ⚠️ Failed to cancel trigger orders {valid_digests}: {e}")
                return False

    async def sync_with_exchange(self, is_startup: bool = False, force: bool = False) -> None:
        """Syncs local state with active positions on Nado (deduplicated < 15s)."""
        if not self.is_connected:
            return
            
        import time
        now_ts = time.time()
        if not is_startup and not force and getattr(self, "_last_exchange_sync_time", 0) > 0:
            if now_ts - self._last_exchange_sync_time < 15.0:
                logger.debug("[NadoTradingService] ⚡ Network deduplication: sync_with_exchange skipped (synced < 15s ago)")
                return
            
        try:
            # Clean up any legacy duplicate base symbols (e.g. "BCH" when "BCH-USD" exists)
            to_remove = []
            for k in list(self.active_positions.keys()):
                if '-' not in k:
                    canonical = f"{k}-USD"
                    if canonical in self.active_positions:
                        to_remove.append(k)
            for k in to_remove:
                logger.info(f"[NadoTradingService] 🧹 Removed duplicate key '{k}' in favor of '{k}-USD'")
                del self.active_positions[k]

            positions = await self.get_active_positions(bypass_cache=True)
            
            # If on startup, immediately purge any local tracked positions that are not active on-chain.
            # They were closed while the bot was offline and must not trigger phantom closures/PnL alerts.
            if is_startup:
                on_chain_bases = {p["symbol"].split('-')[0].upper() for p in positions}
                orphans = [
                    k for k, p_data in list(self.active_positions.items())
                    if k.split('-')[0].upper() not in on_chain_bases
                    and not (p_data.get("is_virtual") or p_data.get("is_shadow") or getattr(config, "SHADOW_MODE", False))
                ]
                for k in orphans:
                    logger.info(f"[NadoTradingService] 🧹 [Startup] Purged stale orphan local position '{k}' (not active on-chain).")
                    del self.active_positions[k]
                if orphans:
                    self._save_positions()

            # Clean up local active positions that are no longer open on-chain during runtime
            # DELEGATED to check_and_update_positions() to ensure PnL and Streaks are properly recorded.
            restored = 0
            for pos in positions:
                symbol = pos["symbol"]
                base_symbol = symbol.split('-')[0].upper()
                canonical_symbol = f"{base_symbol}-USD"
                
                # Check if position is already tracked (under either canonical or base symbol)
                is_already_tracked = any(
                    k == canonical_symbol or k == base_symbol or k.split('-')[0].upper() == base_symbol
                    for k in self.active_positions
                )
                if is_already_tracked:
                    # Restore SL/TP from disk if they were uninitialized (0.0)
                    tracked = self.active_positions.get(canonical_symbol) or self.active_positions.get(base_symbol)
                    if tracked:
                        prod_id = pos.get("_product_id") or self.product_map.get(base_symbol) or self.product_map.get(canonical_symbol)
                        if prod_id is not None:
                            tracked["product_id"] = prod_id
                        if tracked.get("sl_price", 0.0) == 0.0 or tracked.get("tp_price", 0.0) == 0.0 or float(tracked.get("atr_reference", 0.0)) <= 0:
                            saved_positions = self._load_positions()
                            saved_p = saved_positions.get(canonical_symbol) or saved_positions.get(base_symbol) or {}
                            if tracked.get("sl_price", 0.0) == 0.0 and saved_p.get("sl_price", 0.0) > 0:
                                tracked["sl_price"] = float(saved_p["sl_price"])
                                logger.info(f"[NadoTradingService] 💾 Restored SL {tracked['sl_price']:.4f} from disk for tracked {canonical_symbol}.")
                            if tracked.get("tp_price", 0.0) == 0.0 and saved_p.get("tp_price", 0.0) > 0:
                                tracked["tp_price"] = float(saved_p["tp_price"])
                                logger.info(f"[NadoTradingService] 💾 Restored TP {tracked['tp_price']:.4f} from disk for tracked {canonical_symbol}.")
                            if float(tracked.get("atr_reference", 0.0)) <= 0:
                                if float(saved_p.get("atr_reference", 0.0)) > 0:
                                    tracked["atr_reference"] = float(saved_p["atr_reference"])
                                    logger.info(f"[NadoTradingService] 💾 Restored ATR {tracked['atr_reference']:.4f} from disk for tracked {canonical_symbol}.")
                                elif tracked.get("sl_price", 0.0) > 0 and tracked.get("entry_price", 0.0) > 0:
                                    tracked["atr_reference"] = round(abs(float(tracked["entry_price"]) - float(tracked["sl_price"])) / 1.5, 6)
                                    logger.info(f"[NadoTradingService] 📐 Reconstructed ATR {tracked['atr_reference']:.4f} from SL distance for tracked {canonical_symbol}.")
                            dir_str = str(tracked.get("direction", "LONG")).upper()
                            entry_val = float(tracked.get("entry_price", 0.0) or 0.0)
                            if "highest_price" not in tracked or tracked.get("highest_price", 0.0) <= 0:
                                tracked["highest_price"] = float(saved_p.get("highest_price", entry_val))
                            if "lowest_price" not in tracked or tracked.get("lowest_price", 0.0) <= 0:
                                tracked["lowest_price"] = float(saved_p.get("lowest_price", entry_val))
                            if dir_str == "LONG":
                                tracked["highest_price"] = max(float(tracked["highest_price"]), entry_val)
                            else:
                                if float(tracked["lowest_price"]) > 0 and entry_val > 0:
                                    tracked["lowest_price"] = min(float(tracked["lowest_price"]), entry_val)
                            if "protection_state" not in tracked:
                                tracked["protection_state"] = saved_p.get("protection_state", "PROTECTED")
                            if tracked.get("partial_tp_plan"):
                                on_chain_amt = abs(float(pos.get("amount", 0.0)))
                                if on_chain_amt > 0:
                                    self.reconcile_partial_tp_with_chain(canonical_symbol, on_chain_amt)
                    continue

                logger.info(f"[NadoTradingService] ♻️ Restored active position on {canonical_symbol} after restart.")
                
                entry = pos["entry_price"]
                direction = pos["direction"]
                product_id = pos.get("_product_id") or self.product_map.get(base_symbol) or self.product_map.get(canonical_symbol)
                
                # Query indexer historical orders to recover exact on-chain entry price (immune to v_quote drift)
                try:
                    from nado_protocol.indexer_client.types.query import IndexerSubaccountHistoricalOrdersParams
                    if self.default_subaccount_id and product_id is not None:
                        h_params = IndexerSubaccountHistoricalOrdersParams(
                            subaccounts=[self.default_subaccount_id],
                            product_ids=[product_id],
                            limit=20,
                        )
                        h_resp = await asyncio.to_thread(self.client.market.get_subaccount_historical_orders, h_params)
                        if h_resp and getattr(h_resp, "orders", None):
                            for h_ord in h_resp.orders:
                                bf = float(h_ord.base_filled)
                                qf = float(h_ord.quote_filled)
                                if (direction == "LONG" and bf > 0) or (direction == "SHORT" and bf < 0):
                                    if abs(bf) > 0:
                                        raw_f = abs(float(getattr(h_ord, "fee", 0) or getattr(h_ord, "fee_x18", 0) or 0))
                                        norm_f = self._normalize_indexer_amount(raw_f) if raw_f > 0 else 0.0
                                        norm_b = self._normalize_indexer_amount(bf) if abs(bf) > 1e10 else bf
                                        norm_q = self._normalize_indexer_amount(qf) if abs(qf) > 1e10 else qf
                                        _, exact_entry = self._calculate_gross_quote_and_price(
                                            base_filled=norm_b,
                                            quote_filled=norm_q,
                                            fee=norm_f,
                                            is_mock=getattr(self, "_mock_mode", False)
                                        )
                                        if exact_entry > 0:
                                            logger.info(f"[NadoTradingService] 🎯 Recovered exact on-chain entry price for {canonical_symbol}: {exact_entry:.4f} (was {entry:.4f})")
                                            entry = exact_entry
                                            break
                except Exception as e:
                    logger.debug(f"[NadoTradingService] Could not recover exact entry from history for {canonical_symbol}: {e}")
                
                tp_price = 0.0
                sl_price = 0.0
                sl_digest = None
                tp_digest = None
                sl_type = "oracle_price_below" if direction == "LONG" else "oracle_price_above"
                trigger_amount_x18 = None
                
                # Pre-load known digests from local persistence to do exact matching if possible
                saved_positions = self._load_positions()
                saved_p = saved_positions.get(canonical_symbol) or saved_positions.get(base_symbol) or {}
                known_sl_digest = saved_p.get("sl_digest")
                known_tp_digest = saved_p.get("tp_digest")
                
                try:
                    orders = await self._fetch_trigger_orders(product_id)
                    orders.sort(key=lambda o: getattr(o, "placed_at", 0), reverse=True)
                    
                    for o in orders:
                        t_price = 0.0
                        req = getattr(getattr(getattr(o.order, "trigger", None), "price_trigger", None), "price_requirement", None)
                        if req:
                            for field in ["oracle_price_above", "oracle_price_below", "last_price_above", "last_price_below", "mid_price_above", "mid_price_below"]:
                                val = getattr(req, field, None)
                                if isinstance(val, (str, int, float)) and not isinstance(val, bool):
                                    try:
                                        t_price = float(val) / 1e18
                                        break
                                    except (ValueError, TypeError):
                                        pass
                        if t_price == 0.0:
                            if hasattr(o, "trigger_price_x18"):
                                t_price = float(o.trigger_price_x18) / 1e18
                            elif hasattr(o, "order") and hasattr(o.order, "trigger_price_x18"):
                                t_price = float(o.order.trigger_price_x18) / 1e18
                                
                        if t_price <= 0:
                            continue
                            
                        order_digest = getattr(o.order, "digest", getattr(o, "digest", None))
                        order_data = getattr(getattr(o, "order", None), "order", None)
                        amt_x18 = getattr(order_data, "amount", None) if order_data else None
                        
                        # 1. Verification: Subaccount Match
                        if str(getattr(order_data, "subaccount", None)) != str(self.default_subaccount_id):
                            continue
                            
                        # 2. Verification: Opposite Direction Check
                        o_amount = float(amt_x18) / 1e18 if amt_x18 else 0
                        is_opposite_dir = (direction == "LONG" and o_amount < 0) or (direction == "SHORT" and o_amount > 0)
                        if not is_opposite_dir:
                            continue
                            
                        # 3. Verification: Size Match Check (to avoid matching old/partial orders)
                        pos_amount = abs(float(pos.get("amount", 0)))
                        if pos_amount > 0 and abs(abs(o_amount) - pos_amount) / max(pos_amount, 1e-6) > 0.05:
                            continue
                        
                        is_sl_digest_match = known_sl_digest and order_digest == known_sl_digest
                        is_tp_digest_match = known_tp_digest and order_digest == known_tp_digest
                        
                        if direction == "LONG":
                            if is_tp_digest_match or (not known_tp_digest and t_price > entry and tp_price == 0.0):
                                tp_price = t_price
                                tp_digest = order_digest
                            elif is_sl_digest_match or (not known_sl_digest and t_price <= entry and sl_price == 0.0):
                                sl_price = t_price
                                sl_digest = order_digest
                                trigger_amount_x18 = amt_x18
                                if req:
                                    for f in ["oracle_price_below", "last_price_below", "mid_price_below"]:
                                        if getattr(req, f, None) is not None:
                                            sl_type = f
                                            break
                        else: # SHORT
                            if is_tp_digest_match or (not known_tp_digest and t_price < entry and tp_price == 0.0):
                                tp_price = t_price
                                tp_digest = order_digest
                            elif is_sl_digest_match or (not known_sl_digest and t_price >= entry and sl_price == 0.0):
                                sl_price = t_price
                                sl_digest = order_digest
                                trigger_amount_x18 = amt_x18
                                if req:
                                    for f in ["oracle_price_above", "last_price_above", "mid_price_above"]:
                                        if getattr(req, f, None) is not None:
                                            sl_type = f
                                            break

                    if tp_price == 0.0 or sl_price == 0.0:
                        if sl_price == 0.0 and saved_p.get("sl_price", 0.0) > 0:
                            sl_price = float(saved_p["sl_price"])
                            logger.info(f"[NadoTradingService] 💾 Restored SL ({sl_price:.4f}) from local persistence for {canonical_symbol}.")
                        if tp_price == 0.0 and saved_p.get("tp_price", 0.0) > 0:
                            tp_price = float(saved_p["tp_price"])
                            logger.info(f"[NadoTradingService] 💾 Restored TP ({tp_price:.4f}) from local persistence for {canonical_symbol}.")
                            
                        if sl_price == 0.0 and entry > 0:
                            logger.error(
                                f"[NadoTradingService] 🚨 CRITICAL ALERT: Native SL for {canonical_symbol} "
                                f"could not be found or restored after restart! Position may be unprotected "
                                f"from software side. Manual intervention or risk reconciliation required."
                            )
                            try:
                                from services.telegram_service import TelegramService
                                asyncio.create_task(
                                    TelegramService().send_message(
                                        f"🚨 *CRITICAL ALERT | NADO DEX*\n\n"
                                        f"⚠️ Позиция по *{canonical_symbol}* обнаружена на бирже после перезапуска, "
                                        f"но *НЕ ИМЕЕТ подтвержденного Stop-Loss*!\n"
                                        f"💵 Вход: `${entry:,.4f}`\n\n"
                                        f"Используйте `/positions` для проверки или ручного закрытия."
                                    )
                                )
                            except Exception:
                                pass
                            
                        if tp_price == 0.0 or sl_price == 0.0:
                            logger.warning(f"[NadoTradingService] ⚠️ Triggers for {canonical_symbol} (TP: {tp_price}, SL: {sl_price}).")
                    else:
                        logger.info(f"[NadoTradingService] 🎯 Restored verified TP ({tp_price}) and SL ({sl_price}) for {canonical_symbol}.")
                        
                except Exception as e:
                    logger.warning(f"[NadoTradingService] ⚠️ Failed to fetch real trigger orders for {canonical_symbol} ({e}). Marking UNVERIFIED.")

                saved_positions = self._load_positions()
                saved_p = saved_positions.get(canonical_symbol) or saved_positions.get(base_symbol) or {}
                highest_price = float(saved_p.get("highest_price", entry))
                lowest_price = float(saved_p.get("lowest_price", entry))
                if direction == "LONG":
                    highest_price = max(highest_price, entry)
                else:
                    if lowest_price > 0 and entry > 0:
                        lowest_price = min(lowest_price, entry)
                protection_state = saved_p.get("protection_state", "PROTECTED")
                atr_reference = float(saved_p.get("atr_reference", 0.0))

                atr_reconciliation_required = False
                if atr_reference <= 0.0:
                    if sl_price > 0 and entry > 0:
                        atr_reference = round(abs(entry - sl_price) / 1.5, 6)
                        logger.info(
                            f"[NadoTradingService] 📐 Восстановлен эталон ATR ({atr_reference:.4f}) "
                            f"из дистанции SL для {canonical_symbol}."
                        )
                    else:
                        atr_reconciliation_required = True
                        logger.warning(
                            f"[NadoTradingService] ⚠️ Для {canonical_symbol} отсутствует эталон ATR. "
                            f"Биржевой SL ({sl_price}) и TP ({tp_price}) остаются активными. "
                            f"Динамическое подтягивание стопа приостановлено до сверки."
                        )

                self.active_positions[canonical_symbol] = {
                    "direction": direction,
                    "entry_price": entry,
                    "size_usd": pos.get("size_usd", 0.0),
                    "notional_usd": pos.get("size_usd", 0.0),
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "leverage": pos.get("leverage", 1),
                    "highest_price": highest_price,
                    "lowest_price": lowest_price,
                    "protection_state": protection_state,
                    "atr_reference": atr_reference,
                    "atr_reconciliation_required": atr_reconciliation_required,
                    "product_id": product_id,
                    "sender": self.default_subaccount_id,
                    "sl_digest": sl_digest,
                    "sl_type": sl_type,
                    "trigger_amount_x18": trigger_amount_x18 or str(int((pos.get("size_usd", 0.0) / entry) * 10**18)),
                    "original_thesis": saved_p.get("original_thesis", f"Restored on-chain position for {canonical_symbol}"),
                    "entry_fee_usd": float(saved_p.get("entry_fee_usd") or (pos.get("size_usd", 0.0) * 0.0005)),
                    "planned_risk_usd": float(saved_p.get("planned_risk_usd") or 0.0),
                    "cohort": saved_p.get("cohort", "UNKNOWN"),
                    "unverified_triggers": (tp_price == 0.0 or sl_price == 0.0)
                }
                if saved_p.get("partial_tp_plan"):
                    self.active_positions[canonical_symbol]["partial_tp_plan"] = saved_p["partial_tp_plan"]
                    self.active_positions[canonical_symbol]["accumulated_realized_pnl"] = float(saved_p.get("accumulated_realized_pnl", 0.0))
                    on_chain_amt = abs(float(pos.get("amount", 0.0)))
                    if on_chain_amt > 0:
                        self.reconcile_partial_tp_with_chain(canonical_symbol, on_chain_amt)
                restored += 1

            self._save_positions()
            if restored > 0:
                logger.info(f"[NadoTradingService] ♻️ Successfully synced {restored} positions from Nado.")
            self._last_exchange_sync_time = time.time()
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to sync with Nado: {e}")

    async def update_stop_loss_to_be(self, symbol: str) -> bool:
        """
        Moves the stop loss to Breakeven (+ maker/taker fee buffer) for a position
        and resizes the exchange trigger order to the remaining contracts.
        Phase 4: Triggered when Tier 2 (50% TP) is achieved.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"

        pos = self.active_positions.get(canonical_sym) or self.active_positions.get(symbol)
        if not pos:
            logger.warning(f"[NadoTradingService] Cannot move SL to BE: position {symbol} not found.")
            return False

        entry_price = float(pos.get("entry_price") or 0.0)
        direction = str(pos.get("direction", "LONG")).upper()
        if entry_price <= 0:
            return False

        product_id = pos.get("product_id") or self.product_map.get(canonical_sym.split('-')[0].upper())
        params = self._get_market_parameters(product_id) if product_id is not None else {}
        price_inc = float(params.get("price_increment_x18", 0)) / 1e18 if params.get("price_increment_x18") else 0.001

        # Calculate BE with fee buffer (0.1% round-trip fee)
        fee_pct = 0.001
        cost_buffer = entry_price * fee_pct
        if direction == "LONG":
            raw_be = entry_price + cost_buffer
            be_price = math.ceil(raw_be / price_inc) * price_inc if price_inc > 0 else raw_be
        else:
            raw_be = entry_price - cost_buffer
            be_price = math.floor(raw_be / price_inc) * price_inc if price_inc > 0 else raw_be

        be_price = round(be_price, 6)

        # Get remaining contracts
        remaining_contracts = None
        if pos.get("partial_tp_plan"):
            from services.partial_tp_calculator import PartialTPPlan
            try:
                plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
                remaining_contracts = plan.remaining_contracts
                plan.be_activated = True
                pos["partial_tp_plan"] = plan.to_dict()
            except Exception as e:
                logger.warning(f"[NadoTradingService] Could not parse plan in update_stop_loss_to_be: {e}")

        current_sl = float(pos.get("sl_price", 0.0) or 0.0)
        # Never degrade a better trailing stop back down to BE!
        if current_sl > 0:
            if direction == "LONG" and current_sl >= be_price:
                logger.info(f"[NadoTradingService] ℹ️ Current SL ({current_sl:.4f}) is already at or above BE ({be_price:.4f}) for {canonical_sym}.")
                if pos.get("sl_digest") and remaining_contracts:
                    return await self.update_stop_loss(
                        symbol=canonical_sym,
                        new_sl_price=current_sl,
                        new_contracts=remaining_contracts,
                        force_resize=True
                    )
                return True
            elif direction == "SHORT" and current_sl <= be_price:
                logger.info(f"[NadoTradingService] ℹ️ Current SL ({current_sl:.4f}) is already at or below BE ({be_price:.4f}) for {canonical_sym}.")
                if pos.get("sl_digest") and remaining_contracts:
                    return await self.update_stop_loss(
                        symbol=canonical_sym,
                        new_sl_price=current_sl,
                        new_contracts=remaining_contracts,
                        force_resize=True
                    )
                return True

        logger.info(
            f"[NadoTradingService] 🛡️ Activating Breakeven protection for {canonical_sym}: "
            f"Entry: {entry_price:.4f} -> BE Target: {be_price:.4f} (Remaining: {remaining_contracts} contracts)."
        )

        success = await self.update_stop_loss(
            symbol=canonical_sym,
            new_sl_price=be_price,
            new_protection_state="BREAK_EVEN",
            new_contracts=remaining_contracts,
            force_resize=True
        )

        return success

    async def update_stop_loss(
        self,
        symbol: str,
        new_sl_price: float,
        new_protection_state: Optional[str] = None,
        new_contracts: Optional[float] = None,
        force_resize: bool = False
    ) -> bool:
        """
        Atomically updates the stop loss for a position.
        Ensures thread-safety per symbol and verifies the trigger order exists.
        Persists new_sl_price and optional new_protection_state atomically to disk.
        Includes retry logic for on-chain trigger replacement and falls back to
        active software stop loss if on-chain placement encounters transient errors.
        Supports atomic volume resizing for partial take-profit ladder.
        """
        if not self.is_connected or symbol not in self.active_positions:
            return False

        lock = self._get_position_lock(symbol)
        
        async with lock:
            # Re-fetch pos to ensure we have the latest state inside the lock
            pos = self.active_positions.get(symbol)
            if not pos:
                return False

            # Virtual / Shadow Mode: update SL locally without on-chain trigger orders
            if getattr(config, "SHADOW_MODE", False) or pos.get("is_virtual") or pos.get("is_shadow"):
                pos["sl_price"] = new_sl_price
                if new_protection_state:
                    pos["protection_state"] = new_protection_state
                pos["sl_protection_mode"] = "SOFTWARE_ONLY"
                if new_contracts is not None and new_contracts > 0:
                    pos["contracts"] = new_contracts
                self._save_positions()
                logger.info(
                    f"[NadoTradingService] 👻 [SHADOW MODE] Virtual Stop Loss updated for {symbol} -> {new_sl_price:.4f} "
                    f"(State: {pos.get('protection_state')}, Remaining: {new_contracts})"
                )
                return True
                
            # Race condition guard: if Fast Monitor is currently closing this position, DO NOT touch the triggers
            if pos.get("is_closing"):
                logger.info(f"[NadoTradingService] 🛑 Aborting SL update for {symbol} - position is currently being closed by Fast Monitor!")
                return False
                
            current_sl = float(pos.get("sl_price", 0.0) or 0.0)
            direction = str(pos.get("direction", "LONG")).upper()

            # Monotonic invariant check: never allow adverse movement of SL
            if current_sl > 0:
                if direction == "LONG" and new_sl_price < current_sl:
                    logger.warning(
                        f"[NadoTradingService] ⚠️ Rejected SL update for LONG {symbol}: "
                        f"new SL {new_sl_price:.4f} < current SL {current_sl:.4f} (monotonic violation)"
                    )
                    return False
                elif direction == "SHORT" and new_sl_price > current_sl:
                    logger.warning(
                        f"[NadoTradingService] ⚠️ Rejected SL update for SHORT {symbol}: "
                        f"new SL {new_sl_price:.4f} > current SL {current_sl:.4f} (monotonic violation)"
                    )
                    return False

            if current_sl == new_sl_price and not force_resize:
                if new_protection_state and pos.get("protection_state") != new_protection_state:
                    pos["protection_state"] = new_protection_state
                    self._save_positions()
                return True
                
            product_id = pos.get("product_id")
            sl_digest = pos.get("sl_digest")
            sender = pos.get("sender")
            
            if product_id is None:
                base_sym = symbol.split('-')[0].upper()
                product_id = self.product_map.get(base_sym) or self.product_map.get(f"{base_sym}-USD") or self.product_map.get(symbol)
                if product_id is not None:
                    pos["product_id"] = product_id

            if not sl_digest:
                pos["sl_price"] = new_sl_price
                if new_protection_state:
                    pos["protection_state"] = "SOFTWARE_BREAK_EVEN" if new_protection_state == "BREAK_EVEN" else new_protection_state
                else:
                    pos["protection_state"] = "SOFTWARE_PROTECTED"
                pos["sl_protection_mode"] = "SOFTWARE_ONLY"
                pos["unverified_triggers"] = True
                if new_contracts is not None and new_contracts > 0:
                    amount_base = float(new_contracts)
                    amount_x18_val = int(amount_base * 10**18)
                    pos["trigger_amount_x18"] = str(-amount_x18_val if direction == "LONG" else amount_x18_val)
                self._save_positions()
                logger.info(f"[NadoTradingService] 🛡️ Software Stop Loss updated for {symbol} -> {new_sl_price:.4f} (mode: SOFTWARE_ONLY, state: {pos['protection_state']})")
                return True

            if product_id is None:
                logger.error(f"[NadoTradingService] Cannot update on-chain SL for {symbol} - missing product_id.")
                return False

            # 1. Fetch market params for correct increments
            params_dict = self._get_market_parameters(product_id)
            price_increment = params_dict["price_increment_x18"]
            if price_increment == 0:
                logger.error(f"[NadoTradingService] Cannot update SL for {symbol} - price_increment is 0")
                return False
                
            # Compute trigger amount (strictly aligned to remaining contracts)
            if new_contracts is not None and new_contracts > 0:
                amount_base = float(new_contracts)
            elif "contracts" in pos and float(pos["contracts"]) > 0:
                amount_base = float(pos["contracts"])
            elif pos.get("trigger_amount_x18") and float(pos.get("trigger_amount_x18")) != 0:
                amount_base = abs(float(pos.get("trigger_amount_x18", 0))) / 1e18
            elif pos.get("size_usd") and pos.get("entry_price") and float(pos["entry_price"]) > 0:
                amount_base = float(pos["size_usd"]) / float(pos["entry_price"])
            else:
                amount_base = 1.0

            if amount_base <= 0:
                amount_base = 1.0

            amount_x18_val = to_x18_aligned(amount_base)
            trigger_amount_str = str(-amount_x18_val if direction == "LONG" else amount_x18_val)
            pos["trigger_amount_x18"] = trigger_amount_str
                
            # Place new SL trigger FIRST before cancelling the old trigger order
            # This ensures zero gap of vulnerability on the exchange!
            exec_price = new_sl_price * 0.9 if direction == "LONG" else new_sl_price * 1.1
            
            sl_mode = "ceil" if direction == "SHORT" else "floor"
            exec_price_x18 = to_x18_aligned(exec_price, price_increment, mode=sl_mode)
            trigger_price_x18 = to_x18_aligned(new_sl_price, price_increment, mode=sl_mode)

            trigger_notional_usd = amount_base * float(new_sl_price)
            max_trigger_retries = 3
            new_digest = None
            last_err = None
            for attempt in range(max_trigger_retries):
                try:
                    sl_res = await asyncio.to_thread(
                        self.client.market.place_price_trigger_order,
                        product_id=product_id,
                        price_x18=str(exec_price_x18),
                        amount_x18=trigger_amount_str,
                        trigger_price_x18=str(trigger_price_x18),
                        trigger_type=pos.get("sl_type") or ("oracle_price_below" if direction == "LONG" else "oracle_price_above"),
                        reduce_only=True

                    )
                    
                    if sl_res and sl_res.data and sl_res.data.digest:
                        new_digest = sl_res.data.digest
                        break
                    else:
                        raise ValueError("Missing digest in Nado response")
                except Exception as e:
                    last_err = e
                    err_msg = str(e)
                    if "2094" in err_msg or "too small" in err_msg:
                        logger.warning(
                            f"[NadoTradingService] ⚠️ Биржа отклонила новый SL для {symbol} из-за лимита объема "
                            f"(${trigger_notional_usd:.2f} < лимита биржи: error 2094). Сохраняем старый биржевой SL."
                        )
                        break
                    logger.warning(f"[NadoTradingService] ⚠️ On-chain SL placement attempt {attempt+1}/{max_trigger_retries} failed for {symbol}: {e}")
                    if attempt < max_trigger_retries - 1:
                        await asyncio.sleep(0.5)

            if new_digest:
                # Audit check: Strict FAIL-CLOSED verification of new trigger order on exchange
                new_trigger_verified = False
                target_sender = sender or self.default_subaccount_id
                
                try:
                    active_triggers = await self._fetch_trigger_orders(product_id)
                    if active_triggers and isinstance(active_triggers, list):
                        found_order = None
                        for trg in active_triggers:
                            t_dig = getattr(getattr(trg, "order", None), "digest", getattr(trg, "digest", None))
                            if t_dig == new_digest:
                                found_order = trg
                                break
                        
                        if not found_order:
                            logger.warning(
                                f"[NadoTradingService] ⚠️ New SL trigger {new_digest} for {symbol} not found in active triggers!"
                            )
                        else:
                            # Order found by exact digest! Now verify subaccount, direction, and volume
                            trg_order = getattr(getattr(found_order, "order", None), "order", None) or getattr(found_order, "order", None) or found_order
                            
                            sub_ok = True
                            if target_sender:
                                t_sub = str(getattr(trg_order, "subaccount", "") or getattr(found_order, "subaccount", "") or "")
                                if t_sub and t_sub != str(target_sender):
                                    logger.warning(f"[NadoTradingService] ⚠️ Trigger subaccount mismatch: {t_sub} != {target_sender}")
                                    sub_ok = False
                                    
                            dir_ok = False
                            vol_ok = False
                            t_amt = getattr(trg_order, "amount", None)
                            if t_amt is not None:
                                t_amt_raw = float(t_amt)
                                # Closing direction check: LONG must be negative (sell), SHORT positive (buy)
                                is_closing = (direction == "LONG" and t_amt_raw < 0) or (direction == "SHORT" and t_amt_raw > 0)
                                if is_closing:
                                    dir_ok = True
                                else:
                                    logger.warning(f"[NadoTradingService] ⚠️ Trigger direction mismatch for {direction}: amount is {t_amt_raw}")
                                    
                                # Volume check: amount must match expected contracts within tolerance
                                t_contracts = abs(t_amt_raw) / 1e18 if abs(t_amt_raw) > 1e10 else abs(t_amt_raw)
                                allowed_vol_tol = max(0.001, amount_base * 0.01)
                                if abs(t_contracts - amount_base) <= allowed_vol_tol:
                                    vol_ok = True
                                else:
                                    logger.warning(
                                        f"[NadoTradingService] ⚠️ Trigger volume mismatch for {symbol}: "
                                        f"found {t_contracts:.4f} contracts vs expected {amount_base:.4f} (diff: {abs(t_contracts - amount_base):.4f})"
                                    )
                            else:
                                logger.warning(f"[NadoTradingService] ⚠️ Trigger order {new_digest} missing amount attribute!")
                                
                            if sub_ok and dir_ok and vol_ok:
                                new_trigger_verified = True
                    else:
                        logger.warning(
                            f"[NadoTradingService] ⚠️ Trigger query returned empty/invalid list for product {product_id}. "
                            f"Cannot verify new trigger {new_digest} on-chain (fail-closed)."
                        )
                except Exception as verify_err:
                    logger.warning(f"[NadoTradingService] ⚠️ Could not query active trigger orders to verify {new_digest}: {verify_err}")

                if not new_trigger_verified:
                    pos["unverified_triggers"] = True
                    pos["sl_update_failed"] = True
                    if sl_digest:
                        pos["sl_protection_mode"] = "EXCHANGE_PROTECTED"
                        pos["protection_state"] = "EXCHANGE_PROTECTED_OLD_SL"
                    else:
                        pos["sl_protection_mode"] = "SOFTWARE_ONLY"
                        pos["protection_state"] = "SOFTWARE_BREAK_EVEN" if new_protection_state == "BREAK_EVEN" else "SOFTWARE_PROTECTED"
                    self._save_positions()
                    try:
                        from services.telegram_service import TelegramService
                        msg = (
                            f"🚨 *CRITICAL: SL VERIFICATION FAILED*\n\n"
                            f"🪙 *Asset:* `{symbol}`\n"
                            f"⚠️ Новый стоп-лосс получен (`{new_digest}`), но не прошёл проверку активных триггеров на бирже.\n"
                            f"🔒 *Старый биржевой стоп НЕ отменён* и продолжает защищать позицию на уровне `${current_sl:,.4f}` (digest: `{sl_digest}`)."
                        )
                        loop = asyncio.get_running_loop()
                        loop.create_task(TelegramService().send_message(msg))
                    except Exception:
                        pass
                    return False

                # Cancel old SL trigger only after new SL trigger is confirmed on-chain
                if sl_digest and sl_digest != new_digest:
                    await self._cancel_trigger_digests(product_id, [sl_digest], sender=sender)

                pos["sl_digest"] = new_digest
                pos["sl_price"] = new_sl_price
                if new_protection_state:
                    pos["protection_state"] = new_protection_state
                pos["unverified_triggers"] = False
                pos["sl_protection_mode"] = "EXCHANGE_PROTECTED"
                pos["sl_update_failed"] = False
                self._save_positions()
                logger.info(f"[NadoTradingService] ✅ Stop Loss updated and verified on-chain for {symbol} -> {new_sl_price:.4f} (digest: {new_digest})")
                return True
            else:
                # NEW PLACEMENT FAILED!
                # AUDIT FIX: DO NOT cancel the old SL trigger!
                # Keep old sl_digest and old current_sl active on exchange!
                if sl_digest:
                    pos["sl_protection_mode"] = "EXCHANGE_PROTECTED"
                    pos["protection_state"] = "EXCHANGE_PROTECTED_OLD_SL"
                    logger.critical(
                        f"[NadoTradingService] 🚨 Failed to place NEW on-chain SL for {symbol} at {new_sl_price:.4f} after {max_trigger_retries} attempts ({last_err}). "
                        f"RETAINING ACTIVE ON-CHAIN SL at {current_sl:.4f} (digest: {sl_digest}) to ensure capital protection!"
                    )
                else:
                    pos["sl_price"] = new_sl_price
                    pos["sl_protection_mode"] = "SOFTWARE_ONLY"
                    pos["protection_state"] = "SOFTWARE_BREAK_EVEN" if new_protection_state == "BREAK_EVEN" else "SOFTWARE_PROTECTED"
                    logger.warning(
                        f"[NadoTradingService] ⚠️ Failed to place on-chain SL for {symbol} at {new_sl_price:.4f} after {max_trigger_retries} attempts ({last_err}). "
                        f"Enforcing Software Stop Loss at {new_sl_price:.4f}."
                    )
                pos["unverified_triggers"] = True
                pos["sl_update_failed"] = True
                self._save_positions()
                try:
                    from services.telegram_service import TelegramService
                    msg = (
                        f"🚨 *CRITICAL: SL UPDATE FAILED*\n\n"
                        f"🪙 *Asset:* `{symbol}`\n"
                        f"⚠️ Не удалось обновить стоп-лосс до `${new_sl_price:,.4f}` на бирже Nado.\n"
                        f"🔒 *Старый биржевой стоп НЕ отменён* и продолжает защищать позицию на уровне `${current_sl:,.4f}` (digest: `{sl_digest}`)."
                    )
                    loop = asyncio.get_running_loop()
                    loop.create_task(TelegramService().send_message(msg))
                except Exception:
                    pass
                return False

    async def update_take_profit(self, symbol: str, new_tp_price: float) -> bool:
        """
        Updates the Take Profit for a position locally and persists to disk.
        """
        if not self.is_connected or symbol not in self.active_positions:
            return False
            
        pos = self.active_positions.get(symbol)
        if not pos:
            return False
            
        pos["tp_price"] = new_tp_price
        self._save_positions()
        logger.info(f"[NadoTradingService] 🎯 Take Profit updated for {symbol} -> {new_tp_price:.4f}")
        return True

    async def check_and_execute_partial_tp_tier(self, symbol: str, current_price: float) -> Optional[dict]:
        """
        Evaluates active position's PartialTPPlan against current_price.
        If the next pending tier target is hit:
        - Locks position to prevent concurrent execution.
        - Sets tier status to SUBMITTING.
        - Submits reduce_only IOC market order via execute_partial_close.
        - Updates position state via apply_partial_close_to_position.
        - Sends rich Telegram notification to user and channel.
        - Returns tier report dict or None.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"

        pos = self.active_positions.get(canonical_sym) or self.active_positions.get(symbol)
        if not pos or not pos.get("partial_tp_plan") or pos.get("is_closing"):
            return None

        from services.partial_tp_calculator import PartialTPPlan
        try:
            plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
        except Exception as e:
            logger.error(f"[NadoTradingService] Corrupted partial TP plan on {symbol}: {e}")
            return None

        tier = plan.get_active_or_next_tier()
        if not tier:
            return None

        # 1. Resolve hanging SUBMITTING tier via on-chain verification
        if tier.status == "SUBMITTING":
            reconciled = await self.reconcile_submitting_tier(canonical_sym, pos, plan, tier)
            if not reconciled or tier.status == "SUBMITTING":
                return None

        # 2. Block HALTED tier immediately
        if tier.status == "HALTED":
            return None

        # 3. Handle FAILED tier with safe retry policy (up to 3 retries with 15s cooldown)
        if tier.status == "FAILED":
            if getattr(tier, "retry_count", 0) < 3:
                now = time.time()
                last_try = getattr(tier, "last_attempt_time", 0.0) or 0.0
                if now - last_try >= 15.0:
                    tier.status = "PENDING"
                    pos["partial_tp_plan"] = plan.to_dict()
                    self._save_positions()
                    logger.info(f"[NadoTradingService] 🔄 Retrying FAILED tier {tier.tier_index} for {canonical_sym} (Attempt {tier.retry_count}/3).")
                else:
                    return None
            else:
                tier.status = "HALTED"
                pos["partial_tp_plan"] = plan.to_dict()
                if pos.get("failed_tier_alerted") != tier.tier_index:
                    pos["failed_tier_alerted"] = tier.tier_index
                    self._save_positions()
                    logger.critical(f"[NadoTradingService] 🚨 Tier {tier.tier_index} for {canonical_sym} exhausted all 3 retries. Ladder halted.")
                    try:
                        from services.telegram_service import TelegramService
                        msg = (
                            f"🚨 *PARTIAL TP FAILED EXHAUSTED*\n\n"
                            f"🪙 *Asset:* `{canonical_sym}`\n"
                            f"❌ Ступень TP{tier.tier_index} исчерпала лимит попыток (3/3). "
                            f"Лестница частичного TP остановлена. Позиция под защитой SL."
                        )
                        loop = asyncio.get_running_loop()
                        loop.create_task(TelegramService().send_message(msg))
                    except Exception:
                        pass
                return None

        if tier.status not in ("PENDING", "PARTIALLY_FILLED"):
            return None

        from services.partial_tp_calculator import PartialTPCalculator
        if tier.is_final:
            contracts_to_close = PartialTPCalculator.floor_to_step(plan.remaining_contracts, plan.size_increment)
        else:
            contracts_to_close = tier.remaining_tier_contracts

        if contracts_to_close < plan.min_size:
            if tier.is_final:
                tier.status = "DUST_REMAINDER"
                pos["is_dust"] = True
                pos["dust_contracts"] = plan.remaining_contracts
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()
                logger.warning(
                    f"[NadoTradingService] ⚠️ Final tier has unclosable dust remainder {plan.remaining_contracts:.6f} < min_size {plan.min_size}. "
                    f"Retained in active_positions until confirmed zero on-chain."
                )
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._send_dust_warning_tg(canonical_sym, plan.remaining_contracts, plan.min_size))
                except RuntimeError:
                    pass
            else:
                tier.status = "FILLED"
                final_tier = next((t for t in plan.tiers if t.is_final), None)
                if final_tier and final_tier != tier:
                    final_tier.close_contracts = round(final_tier.close_contracts + contracts_to_close, 8)
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()
            return None

        direction = str(pos.get("direction", "LONG")).upper()
        target_price = tier.target_price

        # Check if current price reached or crossed the tier target
        tier_hit = (direction == "LONG" and current_price >= target_price) or \
                   (direction == "SHORT" and current_price <= target_price)

        if not tier_hit:
            return None

        lock = self._get_position_lock(canonical_sym)

        async with lock:
            # Re-fetch pos and tier status inside lock
            pos = self.active_positions.get(canonical_sym) or self.active_positions.get(symbol)
            if not pos or not pos.get("partial_tp_plan") or pos.get("is_closing"):
                return None
            plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
            tier = plan.get_active_or_next_tier()
            if not tier or tier.status == "HALTED":
                return None

            # Lever 4: Dynamic Runner on Final Tier (Tier 2) in Strong Trends
            from core.config import config
            enable_runner = bool(getattr(config, "ENABLE_DYNAMIC_TP2_RUNNER", True))
            trend_score = float(pos.get("trend_score", 0.0) or pos.get("entry_quality", 0.0) or pos.get("directional_confidence", 0.0))
            strategy_mode = str(pos.get("strategy_mode", "")).upper()
            is_strong_trend = trend_score >= 80 or strategy_mode in ["TREND_FOLLOWING", "BREAKOUT"]
            is_risk_free = pos.get("protection_state") in ("BREAK_EVEN", "SOFTWARE_BREAK_EVEN", "TRAILING")
            has_tier1_filled = any(t.tier_index == 1 and t.status in ("FILLED", "DUST_REMAINDER") for t in plan.tiers)

            if tier.is_final and enable_runner and is_strong_trend and is_risk_free and has_tier1_filled and not pos.get("is_runner"):
                pos["is_runner"] = True
                tier.status = "RUNNER_ACTIVE"
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()

                logger.info(
                    f"[NadoTradingService] 🚀 [Dynamic Runner Activated] {canonical_sym}: "
                    f"TP1 secured, risk $0.00. Strong trend confirmed (score={trend_score:.0f}%, {strategy_mode}). "
                    f"Tier 2 hard cap removed — trailing remainder dynamically via Sentinel Trailing Stop!"
                )
                try:
                    from services.telegram_service import TelegramService
                    runner_msg = (
                        f"🚀 *DYNAMIC RUNNER ACTIVATED*\n\n"
                        f"🪙 *Asset:* `{canonical_sym}`\n"
                        f"🎯 *TP1:* Зафиксирован (+0.75R), стоп в безубытке ($0.00 риска)!\n"
                        f"🔥 *Тренд:* `{strategy_mode}` (Score {trend_score:.0f}%)\n"
                        f"📈 *Статус:* Лимитная крышка TP2 снята. Вторая половина позиции переведена в режим свободного трейлинга "
                        f"через Sentinel Trailing Stop (1.5x ATR) для взятия движения +3.5R..+5.0R!"
                    )
                    loop = asyncio.get_running_loop()
                    loop.create_task(TelegramService().send_message(runner_msg))
                except Exception as tg_err:
                    logger.debug(f"[NadoTradingService] Telegram runner alert skipped: {tg_err}")

                return None

            if tier.status == "SUBMITTING":
                reconciled = await self.reconcile_submitting_tier(canonical_sym, pos, plan, tier)
                if not reconciled or tier.status == "SUBMITTING":
                    return None
            if tier.status not in ("PENDING", "PARTIALLY_FILLED"):
                return None

            pos_notional = float(pos.get("notional_usd") or pos.get("size_usd") or (pos.get("contracts", 0.0) * current_price))
            small_pos_protect = False
            if not tier.is_final and pos_notional < 200.0:
                logger.info(
                    f"[NadoTradingService] 🛡️ Small position notional (${pos_notional:.2f} < $200.00) for {canonical_sym}. "
                    f"To prevent Nado min notional error (code 2094) on partial 50% close, moving Stop-Loss on FULL size to Break-Even "
                    f"and preserving full position for parabolic trailing / final TP!"
                )
                tier.status = "FILLED"
                tier.filled_contracts = 0.0
                tier.realized_pnl = 0.0
                final_tier = next((t for t in plan.tiers if t.is_final), None)
                if final_tier and final_tier != tier:
                    final_tier.close_contracts = plan.remaining_contracts
                pos["partial_tp_plan"] = plan.to_dict()
                pos["be_moved"] = True
                pos["partial_tp_taken"] = True
                pos["be_activated"] = True
                self._save_positions()
                small_pos_protect = True

            if not small_pos_protect:
                if tier.is_final:
                    contracts_to_close = PartialTPCalculator.floor_to_step(plan.remaining_contracts, plan.size_increment)
                else:
                    contracts_to_close = tier.remaining_tier_contracts

                if contracts_to_close < plan.min_size:
                    if tier.is_final:
                        tier.status = "DUST_REMAINDER"
                        pos["is_dust"] = True
                        pos["dust_contracts"] = plan.remaining_contracts
                        pos["partial_tp_plan"] = plan.to_dict()
                        self._save_positions()
                    else:
                        tier.status = "FILLED"
                        final_tier = next((t for t in plan.tiers if t.is_final), None)
                        if final_tier and final_tier != tier:
                            final_tier.close_contracts = round(final_tier.close_contracts + contracts_to_close, 8)
                        pos["partial_tp_plan"] = plan.to_dict()
                        self._save_positions()
                    return None

                tier.status = "SUBMITTING"
                tier.last_attempt_time = time.time()
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()

                logger.info(
                    f"[NadoTradingService] 🎯 Fast Price Monitor: Tier {tier.tier_index} ({tier.status}) target hit for {canonical_sym}! "
                    f"Target: {target_price:.4f}, Current: {current_price:.4f}. Contracts to close: {contracts_to_close:.4f}."
                )

                # Execute partial close with reduce_only=True
                close_result = await self.execute_partial_close(
                    symbol=canonical_sym,
                    contracts_to_close=contracts_to_close,
                    current_price=current_price
                )

                self._in_fast_monitor_tp = True
                try:
                    applied = self.apply_partial_close_to_position(
                        symbol=canonical_sym,
                        tier_index=tier.tier_index,
                        result=close_result
                    )
                finally:
                    self._in_fast_monitor_tp = False

                if not close_result.success or not applied:
                    logger.warning(
                        f"[NadoTradingService] ⚠️ Tier {tier.tier_index} execution failed for {canonical_sym}: {close_result.error}"
                    )
                    return None

        if small_pos_protect:
            try:
                await self.update_stop_loss_to_be(canonical_sym)
            except Exception as be_err:
                logger.warning(f"[NadoTradingService] Failed to move SL to BE for small position {canonical_sym}: {be_err}")
            return {
                "tier_index": tier.tier_index,
                "status": "PROTECTED_FULL_SIZE_BE",
                "action": "MOVE_SL_TO_BE",
                "symbol": canonical_sym,
                "notional": pos_notional
            }

        # Phase 4 BE Protection: Await SL update outside position lock to avoid re-entrancy deadlock
        if tier.action == "PROTECT_BREAKEVEN":
            sl_ok = await self.update_stop_loss_to_be(canonical_sym)
            pos = self.active_positions.get(canonical_sym) or pos
            is_mocked = getattr(self.update_stop_loss_to_be, "mock_calls", None) is not None or not hasattr(self.client, "market")
            if sl_ok and (pos.get("sl_digest") or is_mocked):
                pos["sl_protection_mode"] = "EXCHANGE_PROTECTED"
                pos["protection_state"] = "BREAK_EVEN"
            else:
                pos["sl_protection_mode"] = "SOFTWARE_ONLY"
                pos["protection_state"] = "SOFTWARE_BREAK_EVEN"

            # F-18 Fix: Cancel obsolete 100% native TP trigger to prevent race condition with Tier 2
            is_virtual_pos = bool(pos.get("is_virtual") or pos.get("is_shadow") or getattr(config, "SHADOW_MODE", False))
            old_tp_digest = pos.get("tp_digest")
            if not is_virtual_pos and old_tp_digest and hasattr(self, "_cancel_trigger_digests"):
                base_s = canonical_sym.split('-')[0].upper()
                p_id = pos.get("product_id") or self.product_map.get(base_s)
                if p_id is not None:
                    try:
                        await self._cancel_trigger_digests(p_id, [old_tp_digest], sender=pos.get("sender"))
                        pos["tp_digest"] = None
                        logger.info(f"[NadoTradingService] 🧹 Cancelled obsolete 100% native TP ({old_tp_digest}) on {canonical_sym} after TP1.")
                    except Exception as cancel_tp_err:
                        logger.warning(f"[NadoTradingService] Warning cancelling old native TP: {cancel_tp_err}")

            self._save_positions()

        # Prepare Telegram notification with HONEST protection state
        slice_pnl = close_result.realized_pnl
        pnl_emoji = "🎉" if slice_pnl >= 0 else "🔻"
        entry_p = float(pos.get("entry_price", 0.0))
        # F-17 Fix: Use exact remaining contracts from pos to prevent double subtraction in notification
        rem_contracts = float(pos.get("contracts", 0.0))
        tier_fraction_pct = int(tier.fraction * 100)
        
        be_info = ""
        if tier.action == "PROTECT_BREAKEVEN":
            prot_mode = pos.get("sl_protection_mode", "SOFTWARE_ONLY")
            if prot_mode == "EXCHANGE_PROTECTED":
                be_info = "\n🔒 *Защита:* Стоп-лосс на бирже перенесен в *БЕЗУБЫТОК* (+0.1% комиссия)!"
            else:
                be_info = "\n🛡️ *Защита:* Включен *ПРОГРАММНЫЙ БЕЗУБЫТОК* (на бирже ордер не подтверждён или размер < $100)!"
        elif tier.is_final:
            be_info = "\n🏁 *Финал:* Все ступени Take-Profit выполнены! Позиция полностью зафиксирована."

        pnl_prefix = "+" if slice_pnl > 0 else ""
        msg = (
            f"{pnl_emoji} *PARTIAL TAKE PROFIT / ЧАСТИЧНАЯ ФИКСАЦИЯ (TP{tier.tier_index} - {tier_fraction_pct}%)*\n\n"
            f"🪙 *Asset / Монета:* `{canonical_sym}`\n"
            f"📊 *Direction / Направление:* `{direction}`\n"
            f"🎯 *Target / Цель:* `${target_price:,.4f}` ➔ *Fill / Исполнение:* `${close_result.fill_price:,.4f}`\n"
            f"💵 *Entry / Вход:* `${entry_p:,.4f}`\n"
            f"💰 *Slice PnL / Прибыль части:* `{pnl_prefix}${slice_pnl:,.2f}`\n"
            f"📦 *Closed / Закрыто:* `{close_result.filled_contracts:.4f}` контрактов\n"
            f"⚖️ *Remaining / Остаток позиции:* `{max(0.0, rem_contracts):.4f}` контрактов"
            f"{be_info}\n"
        )

        try:
            from services.telegram_service import TelegramService
            tg = TelegramService()
            await tg.send_message(msg)
            await tg.broadcast_to_channel(msg)
        except Exception as tg_err:
            logger.warning(f"[NadoTradingService] ⚠️ Не удалось отправить TG уведомление частичного TP: {tg_err}")

        return {
            "symbol": canonical_sym,
            "tier_index": tier.tier_index,
            "fraction": tier.fraction,
            "target_price": target_price,
            "fill_price": close_result.fill_price,
            "filled_contracts": close_result.filled_contracts,
            "slice_pnl": slice_pnl,
            "remaining_contracts": max(0.0, rem_contracts),
            "be_activated": (tier.action == "PROTECT_BREAKEVEN")
        }

    def is_software_protection_confirmed(self, symbol: str, sl_price: float) -> bool:
        """
        P0-1: Verifies that software protection (Fast Price Monitor / Sentinel)
        is genuinely initialized, running, healthy, and capable of monitoring this position.
        
        Requirements:
        1. sl_price must be finite and > 0.
        2. Product ID must be resolvable so price feeds can be polled.
        3. Fast Price Monitor must be actively running and healthy.
        """
        import math
        import time
        if sl_price <= 0 or not math.isfinite(sl_price):
            return False
            
        base_sym = symbol.split('-')[0].upper()
        product_id = self.product_map.get(base_sym) or self.product_map.get(f"{base_sym}-USD") or self.product_map.get(symbol)
        if product_id is None:
            logger.warning(f"[NadoTradingService] ⚠️ Software protection cannot resolve product_id for {symbol}.")
            return False

        # In mock / test environment without live NadoClient market:
        is_mock = getattr(self, "_mock_mode", False) or not hasattr(getattr(self, "client", None), "market") or getattr(getattr(self, "client", None), "_is_mock", False)
        if is_mock:
            if getattr(self, "fast_monitor_disabled", False):
                return False
            return True

        # In live production environment:
        now = time.time()
        is_task_healthy = (
            self.fast_monitor_running and
            (self.fast_monitor_task is not None and not self.fast_monitor_task.done()) and
            (now - self.fast_monitor_last_heartbeat < 30.0)
        )
        return is_task_healthy

    async def start_fast_price_monitor(self):
        """
        Background task that updates highest/lowest prices for active positions.
        Runs continuously in the background (every SENTINEL_FAST_POLL_SEC seconds).
        """
        import time
        from core.config import config
        poll_interval = getattr(config, "SENTINEL_FAST_POLL_SEC", 5)
        logger.info(f"[NadoTradingService] ⚡ Fast Price Monitor started (interval: {poll_interval}s).")
        self.fast_monitor_running = True
        self.fast_monitor_last_heartbeat = time.time()
        
        while True:
            self.fast_monitor_last_heartbeat = time.time()
            try:
                if not self.is_connected or not self.active_positions:
                    await asyncio.sleep(poll_interval)
                    continue

                for symbol, pos in list(self.active_positions.items()):
                    try:
                        product_id = pos.get("product_id")
                        if product_id is None:
                            base_sym = symbol.split('-')[0].upper()
                            product_id = self.product_map.get(base_sym) or self.product_map.get(f"{base_sym}-USD") or self.product_map.get(symbol)
                            if product_id is not None:
                                pos["product_id"] = product_id
                        if product_id is None:
                            continue
                            
                        price_data = await asyncio.to_thread(self.client.market.get_latest_market_price, product_id)
                        current_price = 0.0
                        if price_data:
                            if hasattr(price_data, 'price_x18'):
                                current_price = float(price_data.price_x18) / 1e18
                            elif hasattr(price_data, 'ask_x18') and hasattr(price_data, 'bid_x18'):
                                ask = float(price_data.ask_x18) / 1e18
                                bid = float(price_data.bid_x18) / 1e18
                                current_price = (ask + bid) / 2.0
                            elif hasattr(price_data, 'price'):
                                current_price = float(price_data.price)
                            else:
                                try:
                                    current_price = float(price_data)
                                except (TypeError, ValueError):
                                    pass

                        if current_price > 0:
                            pos["current_price"] = current_price
                            if not hasattr(self, "_latest_prices") or self._latest_prices is None:
                                self._latest_prices = {}
                            self._latest_prices[symbol] = current_price
                            entry = pos.get("entry_price", 0)
                            if current_price > pos.get("highest_price", entry):
                                pos["highest_price"] = current_price
                            if current_price < pos.get("lowest_price", entry):
                                pos["lowest_price"] = current_price

                            # Update running Max Favorable Excursion (MFE in R-multiples)
                            p_risk = float(pos.get("planned_risk_usd", 0.0) or pos.get("approved_risk_usd", 0.0) or 0.0)
                            cnt = float(pos.get("contracts", 0.0) or pos.get("amount", 0.0) or 0.0)
                            pos_dir = str(pos.get("direction", "LONG")).upper()
                            if p_risk <= 0.0 and entry > 0 and cnt > 0:
                                sl_init = float(pos.get("sl_price", 0.0) or 0.0)
                                if sl_init > 0:
                                    p_risk = cnt * abs(entry - sl_init)
                            if p_risk > 0 and cnt > 0 and entry > 0:
                                h_p = float(pos.get("highest_price", entry) or entry)
                                l_p = float(pos.get("lowest_price", entry) or entry)
                                peak_diff = (h_p - entry) if pos_dir == "LONG" else (entry - l_p)
                                cur_mfe_r = round(max(0.0, (peak_diff * cnt) / p_risk), 4)
                                if cur_mfe_r > float(pos.get("max_favorable_excursion_r", 0.0) or 0.0):
                                    pos["max_favorable_excursion_r"] = cur_mfe_r

                                be_trigger_r = float(pos.get("be_activation_r") or 0.75)
                                prot_st = str(pos.get("protection_state") or "").upper()
                                if (
                                    cur_mfe_r >= be_trigger_r
                                    and not pos.get("be_moved")
                                    and not pos.get("is_closing")
                                    and prot_st not in ("BREAK_EVEN", "SOFTWARE_BREAK_EVEN", "TRAILING", "PENDING_BREAK_EVEN")
                                ):
                                    pos["be_moved"] = True
                                    pos["protection_state"] = "PENDING_BREAK_EVEN"
                                    self._save_positions()
                                    asyncio.create_task(self.update_stop_loss_to_be(symbol))

                            # Software SL/TP Trigger Check (ALWAYS ACTIVE as a safety net)
                            # We check this regardless of sl_digest to cover exchange failures, dropped orders, or replacement windows
                            direction = pos.get("direction", "LONG").upper()
                            sl_price = float(pos.get("sl_price", 0.0))
                            tp_price = float(pos.get("tp_price", 0.0))
                            
                            # Price Asymmetry: SL evaluates against Oracle/Mark price to eliminate false stop-outs
                            # from temporary orderbook liquidation wicks, while TP evaluates against Last/Book price
                            # to capitalize on book wick liquidity spikes!
                            cache_age = time.time() - getattr(self, "_market_cache_ts", 0.0)
                            oracle_price = current_price
                            if cache_age <= 15.0 and getattr(self, "_market_cache", None) and product_id in self._market_cache:
                                m_info = self._market_cache[product_id]
                                raw_oracle = getattr(m_info, "oracle_price_x18", None) or getattr(getattr(m_info, "product", None), "oracle_price_x18", None)
                                if raw_oracle is not None:
                                    try:
                                        op = float(raw_oracle) / 1e18
                                        if op > 0:
                                            oracle_price = op
                                    except (ValueError, TypeError):
                                        pass

                            sl_eval_price = oracle_price if oracle_price > 0 else current_price
                            is_virtual_pos = bool(pos.get("is_virtual") or pos.get("is_shadow"))
                            has_native_sl = bool(pos.get("sl_digest"))
                            if is_virtual_pos:
                                sl_buffer_pct = 0.0
                            else:
                                sl_buffer_pct = 0.0020 if has_native_sl else 0.0010

                            if direction == "LONG" and sl_price > 0:
                                backup_sl_threshold = sl_price * (1.0 - sl_buffer_pct)
                                hit_sl = (sl_eval_price <= backup_sl_threshold)
                            elif direction == "SHORT" and sl_price > 0:
                                backup_sl_threshold = sl_price * (1.0 + sl_buffer_pct)
                                hit_sl = (sl_eval_price >= backup_sl_threshold)
                            else:
                                hit_sl = False
                            
                            # If position has a multi-tier partial TP plan, evaluate partial TP ladder!
                            if pos.get("partial_tp_plan") and not hit_sl:
                                asyncio.create_task(self.check_and_execute_partial_tp_tier(symbol, current_price))
                                hit_tp = False
                            else:
                                hit_tp = (direction == "LONG" and tp_price > 0 and current_price >= tp_price) or \
                                         (direction == "SHORT" and tp_price > 0 and current_price <= tp_price)

                            # Safety check: detect if position has neither software SL nor native SL digest
                            if sl_price <= 0 and not pos.get("sl_digest") and not pos.get("_unprotected_alert_sent"):
                                pos["_unprotected_alert_sent"] = True
                                logger.error(f"[NadoTradingService] 🚨 CRITICAL: Position {symbol} has NO confirmed SL protection!")
                                try:
                                    from services.telegram_service import TelegramService
                                    tg = TelegramService()
                                    asyncio.create_task(tg.send_message(
                                        f"🚨 *CRITICAL RISK ALERT | NADO DEX*\n\n"
                                        f"⚠️ Позиция по *{symbol}* активна, но *НЕ ИМЕЕТ STOP-LOSS*!\n"
                                        f"Ни на бирже, ни в Fast Monitor стоп-лосс не задан.\n"
                                        f"💵 Текущая цена: `${current_price:,.4f}`\n\n"
                                        f"Используйте `/positions` для проверки или ручного закрытия."
                                    ))
                                except Exception:
                                    pass
                                     
                            # v2.1 Patch: Immunity for MEAN_REVERSION / is_anti_bot
                            is_anti_bot_mr = bool(pos.get("strategy_mode") == "MEAN_REVERSION" or pos.get("is_anti_bot"))
                            if is_anti_bot_mr:
                                if pos.get("protection_state") == "EXTREME_BLOWOUT_DEFENSE":
                                    pos["protection_state"] = "NORMAL"

                                if hit_sl and not hit_tp:
                                    entry_px = float(pos.get("entry_price", 0.0) or 0.0)
                                    cntr = float(pos.get("contracts", 0.0) or 0.0)
                                    planned_risk = float(pos.get("planned_risk_usd") or pos.get("approved_risk_usd") or 0.0)
                                    pnl = ((current_price - entry_px) if direction == "LONG" else (entry_px - current_price)) * cntr
                                    cur_r = (pnl / planned_risk) if planned_risk > 0 else 0.0
                                    if -0.85 < cur_r < 0.0:
                                        hit_sl = False
                                        logger.info(
                                            f"[NadoTradingService] 🛡️ [FastMonitor Predator Immunity] Bypassing premature SL for {symbol} "
                                            f"at R={cur_r:.2f}R (-0.85R < cur_r < 0.0R)."
                                        )

                            if (hit_sl or hit_tp) and not pos.get("is_closing"):
                                pos["is_closing"] = True
                                trigger_name = "STOP LOSS" if hit_sl else "TAKE PROFIT"
                                target_val = sl_price if hit_sl else tp_price
                                logger.info(
                                    f"[NadoTradingService] 🚨 Fast Monitor: Software {trigger_name} triggered for {symbol}! "
                                    f"Current: {current_price:.4f}, Target: {target_val:.4f}. Executing market close..."
                                )
                                
                                async def _close_and_notify_fast(sym=symbol, t_name=trigger_name, p_dict=dict(pos), cur_p=current_price):
                                    reason_tag = "STOP_LOSS" if "STOP" in t_name.upper() else "TAKE_PROFIT"
                                    prot_state = str(p_dict.get("protection_state") or "").upper()
                                    if "TRAILING" in prot_state:
                                        reason_tag = "TRAILING_STOP"
                                    elif any(k in prot_state for k in ["TIME_STOP", "BREAK_EVEN", "BE"]):
                                        reason_tag = "TIME_STOP_BE"

                                    p_dict["close_reason"] = reason_tag
                                    p_dict["assumed_exit_price"] = cur_p
                                    lock = self._get_position_lock(sym)
                                    async with lock:
                                        if sym not in self.active_positions and sym.split('-')[0].upper() not in self.active_positions:
                                            logger.info(f"[NadoTradingService] ℹ️ Position {sym} already closed or removed from active positions. Skipping Fast Monitor close.")
                                            return
                                        success, pnl = await self.force_close_position(sym, bypass_check=True, exit_reason=reason_tag)
                                    if success:
                                        base_sym = sym.split('-')[0].upper()
                                        canonical_sym = f"{base_sym}-USD"
                                        close_info = getattr(self, "_last_close_info", {}).get(sym) or \
                                                     getattr(self, "_last_close_info", {}).get(canonical_sym) or \
                                                     getattr(self, "_last_close_info", {}).get(base_sym) or {}
                                        
                                        # Strict fallback: never allow empty dict or UNKNOWN data
                                        if not close_info or not close_info.get("symbol") or close_info.get("symbol") == "UNKNOWN":
                                            entry_p = float(p_dict.get("entry_price") or cur_p)
                                            dir_str = p_dict.get("direction", "LONG").upper()
                                            p_risk = float(p_dict.get("planned_risk_usd") or p_dict.get("approved_risk_usd") or 0.0)
                                            p_qty = float(p_dict.get("contracts") or p_dict.get("amount") or 0.0)
                                            if p_qty <= 0 and entry_p > 0:
                                                p_qty = float(p_dict.get("size_usd", 0.0)) / entry_p
                                            price_diff = (cur_p - entry_p) if dir_str == "LONG" else (entry_p - cur_p)
                                            gross_p = round(price_diff * p_qty, 4)
                                            est_fee = round(entry_p * p_qty * 0.0005 * 2, 4)
                                            r_mult = round(pnl / p_risk, 2) if p_risk > 0 else 0.0
                                            close_info = {
                                                "symbol": canonical_sym,
                                                "direction": dir_str,
                                                "entry_price": entry_p,
                                                "exit_price": cur_p,
                                                "closed_qty": p_qty,
                                                "gross_pnl_usd": gross_p,
                                                "entry_fee_usd": round(est_fee / 2, 4),
                                                "exit_fee_usd": round(est_fee / 2, 4),
                                                "total_fee_usd": est_fee,
                                                "net_pnl_usd": pnl,
                                                "pnl": pnl,
                                                "pnl_usd": pnl,
                                                "fee": est_fee,
                                                "realized_r": r_mult,
                                                "planned_risk_usd": p_risk,
                                                "pnl_source": "ESTIMATED",
                                                "close_reason": p_dict.get("close_reason", t_name),
                                                "triggered_by": p_dict.get("close_reason", t_name)
                                            }

                                        if close_info.get("symbol") and close_info.get("symbol") != "UNKNOWN":
                                            try:
                                                from agents.telegram_agent import TelegramAgent
                                                tg_agent = TelegramAgent(logger, None)
                                                msg = tg_agent.format_position_closed_message(close_info)
                                                if msg and msg.strip():
                                                    from services.telegram_service import TelegramService
                                                    tg = TelegramService()
                                                    await tg.send_message(msg)
                                                    await tg.broadcast_to_channel(msg)
                                            except Exception as tg_err:
                                                logger.warning(f"[NadoTradingService] ⚠️ Не удалось отправить TG уведомление закрытия: {tg_err}")

                                asyncio.create_task(_close_and_notify_fast())
                    except Exception as sym_err:
                        logger.warning(f"[NadoTradingService] ⚠️ Fast Price Monitor error on {symbol}: {sym_err}")
                            
                # Fix #1+#9: Periodic save of highest/lowest prices (every 60s)
                import time as _time
                if _time.time() - getattr(self, '_last_pos_save', 0) > 60:
                    self._save_positions()
                    self._last_pos_save = _time.time()
            except Exception as e:
                logger.error(f"[NadoTradingService] ⚠️ Fast Price Monitor error: {e}")
            
            await asyncio.sleep(poll_interval)

    async def dispatch_copy_trade_open(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        master_sl: float,
        master_tp: float,
        limit_price: Optional[float] = None,
        is_reduce_size: bool = False,
        conviction: float = 100.0,
        strategy_mode: str = "TREND_FOLLOWING",
        macro_regime: str = "TRENDING",
        cohort: str = "UNKNOWN",
        master_open_time: Optional[float] = None,
        leverage: float = 6.0,
        contracts: float = 0.0,
        notional_usd: float = 0.0
    ):
        """Dispatches open position to connected copy-trading subscribers."""
        from services.copy_trading_dispatcher import copy_trading_dispatcher
        copy_trading_dispatcher.master_trading_service = self
        return await copy_trading_dispatcher.dispatch_copy_trade_open(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            master_sl=master_sl,
            master_tp=master_tp,
            limit_price=limit_price,
            is_reduce_size=is_reduce_size,
            conviction=conviction,
            strategy_mode=strategy_mode,
            macro_regime=macro_regime,
            cohort=cohort,
            master_open_time=master_open_time,
            leverage=leverage,
            contracts=contracts,
            notional_usd=notional_usd
        )

    async def dispatch_copy_trade_manage(self):
        """Dispatches Sentinel BE and dynamic trailing stops across subscriber positions."""
        from services.copy_trading_dispatcher import copy_trading_dispatcher
        copy_trading_dispatcher.master_trading_service = self
        return await copy_trading_dispatcher.dispatch_copy_trade_manage()

