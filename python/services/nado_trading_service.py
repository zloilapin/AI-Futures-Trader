import logging
import asyncio
import math
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple
from core.interfaces import BaseTradingService
from core.web3_wallet import Web3Wallet

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
        self.product_map: Dict[str, int] = {}
        self.default_subaccount_id = None
        
        # Stats & Cooldown tracking
        self.win_count = 0
        self.loss_count = 0
        self.recent_streak = []
        self._initial_balance = None
        self._daily_start_balance = None
        self._daily_start_date = None
        self._daily_start_total_trades = 0
        self.cooldown_until = 0.0
        self._last_cooldown_processed_len = 0
        self._load_state()
        
        # NOTE: self.initialize(nado_client) MUST be awaited explicitly after instantiation.

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
                from core.config import config
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
            asyncio.create_task(self.start_fast_price_monitor())
            
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to init Nado SDK: {e}")
            self.is_connected = False

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Returns the current portfolio balance from Nado."""
        if not self.is_connected or not self.default_subaccount_id:
            return {"total_usd": 0.0, "current_balance": 0.0, "balance": 0.0, "margin_used": 0.0, "free_margin": 0.0, "pnl": 0.0}
        
        try:
            summary = await asyncio.to_thread(self.client.subaccount.get_engine_subaccount_summary, self.default_subaccount_id)
            
            # SubaccountInfoData has a healths array. index 0 is Initial Margin health
            if hasattr(summary, "healths") and len(summary.healths) > 0:
                health = summary.healths[0]
                margin_used = float(health.liabilities) / 1e18
                free_margin = float(health.health) / 1e18
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
                        
            if self._initial_balance is None and equity > 0:
                self._initial_balance = equity
                self._save_state()
                
            initial = self._initial_balance or equity
            
            # Daily Drawdown and Intraday PnL Tracking
            import time
            today_str = time.strftime("%Y-%m-%d", time.gmtime())
            if getattr(self, "_daily_start_date", None) != today_str or getattr(self, "_daily_start_balance", None) is None:
                self._daily_start_date = today_str
                self._daily_start_balance = equity
                self._daily_start_total_trades = self.win_count + self.loss_count
                self._save_state()

            # Defense-in-depth: External deposit/withdrawal / baseline desynchronization guard
            # If no positions are open and no trades were executed today, an equity shift > 15%
            # is an external transfer (or state baseline artifact), NOT a market drawdown.
            daily_trades_count = (self.win_count + self.loss_count) - getattr(self, "_daily_start_total_trades", self.win_count + self.loss_count)
            daily_start = self._daily_start_balance or equity
            
            if active_count == 0 and daily_trades_count == 0 and pnl == 0.0:
                if daily_start > 0 and abs(equity - daily_start) / daily_start > 0.15:
                    logger.info(
                        f"[NadoTradingService] 🔄 External balance change detected: ${daily_start:.2f} -> ${equity:.2f} "
                        f"(0 active positions, 0 trades today). Re-anchoring daily start balance."
                    )
                    self._daily_start_balance = equity
                    self._daily_start_total_trades = self.win_count + self.loss_count
                    self._save_state()
                    daily_start = equity

            daily_pnl_usd = round(equity - daily_start, 2)
            daily_pnl_pct = round(((equity - daily_start) / daily_start) * 100, 2) if daily_start > 0 else 0.0
            daily_drawdown_pct = round(max(0.0, -daily_pnl_pct / 100.0), 4)

            total_trades = self.win_count + self.loss_count
            win_rate = round((self.win_count / total_trades) * 100, 1) if total_trades > 0 else 0.0
            
            return {
                "initial_balance": round(initial, 2),
                "current_balance": round(equity, 2),
                "total_usd": round(equity, 2),
                "total_pnl_usd": round(equity - initial, 2),
                "total_pnl_pct": round(((equity - initial) / initial) * 100, 2) if initial > 0 else 0.0,
                "daily_pnl_usd": daily_pnl_usd,
                "daily_pnl_pct": daily_pnl_pct,
                "daily_drawdown_pct": daily_drawdown_pct,
                "daily_trades_count": daily_trades_count,
                "unrealized_pnl_usd": pnl,
                "unrealized_pnl": pnl,
                "roi_pct": round(((equity - initial) / initial) * 100, 2) if initial > 0 else 0.0,
                "available_margin": round(free_margin, 2),
                "used_margin": round(margin_used, 2),
                "active_positions_count": active_count,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "win_rate_pct": win_rate,
                "recent_streak": self.recent_streak
            }
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to get portfolio summary: {e}")
            return {"total_usd": 0.0, "current_balance": 0.0, "balance": 0.0, "margin_used": 0.0, "free_margin": 0.0, "pnl": 0.0}

    async def get_active_positions(self, bypass_cache: bool = False) -> List[Dict[str, Any]]:
        """Returns active open positions from Nado."""
        if not self.is_connected:
            return []
            
        import time
        if not bypass_cache and getattr(self, "_pos_cache_time", 0) > 0:
            if time.time() - self._pos_cache_time < 15.0 and hasattr(self, "_pos_cache"):
                return self._pos_cache
                
        active_list = []
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
            except Exception as e:
                logger.warning(f"[NadoTradingService] ⚠️ Failed to refresh market cache: {e}")
                
            address = self.wallet.get_address()
            
            # Fetch all subaccounts for this address
            res = await asyncio.to_thread(self.client.subaccount.get_subaccounts, address)
            if not res or not res.subaccounts:
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
                        "original_thesis": local_pos.get("original_thesis", "")
                    })
            
            self._pos_cache = active_list
            import time
            self._pos_cache_time = time.time()
            
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to fetch active positions: {e}")
            
        return active_list

    async def open_position(self, symbol: str, direction: str, entry_price: float, notional_usd: float, tp_price: float, sl_price: float, leverage: int, original_thesis: str = "", contracts: float = 0.0, atr_value: float = 0.0) -> bool:
        """Submits an EIP-712 signed order to Nado Gateway."""
        if not self.is_connected:
            logger.error(f"[NadoTradingService] Cannot open {direction} on {symbol} - SDK not connected.")
            return False

        clean_sym = symbol.replace('/', '-').strip().upper()
        base_sym = clean_sym.split('-')[0]
        canonical_sym = f"{base_sym}-USD"
        
        # Guard: Strict duplicate position prevention
        if any(k in self.active_positions for k in [canonical_sym, base_sym, clean_sym, symbol]):
            logger.warning(
                f"[NadoTradingService] ⚠️ Позиция по {symbol} ({canonical_sym}) уже открыта в active_positions! "
                f"Блокировка дублирующего ордера для защиты капитала."
            )
            return False
            
        logger.info(f"[NadoTradingService] 🚀 Routing {direction} {symbol} to Nado DEX...")
        
        actual_entry_price = entry_price
        
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
            
            base_symbol = symbol.split('-')[0].upper()
            product_id = self.product_map.get(base_symbol)
            if product_id is None:
                logger.error(f"[NadoTradingService] ❌ Unknown symbol {symbol} - not found in Nado product map!")
                return False
                
            params_dict = self._get_market_parameters(product_id)
            if params_dict["size_increment_x18"] == 0:
                logger.error(f"[NadoTradingService] ❌ Market params not found for product_id {product_id}")
                return False
                
            amount_base = contracts if contracts > 0 else notional_usd / entry_price
            amount_x18 = int(amount_base * 10**18)
            # Explicitly guard against non-directional execution (HOLD, WAIT, etc.)
            dir_clean = str(direction).strip().upper()
            if dir_clean == "LONG":
                limit_price = entry_price * 1.005
            elif dir_clean == "SHORT":
                limit_price = entry_price * 0.995
                amount_x18 = -amount_x18
            else:
                logger.error(f"[NadoTradingService] ❌ Refusing execution: invalid direction '{direction}'. Only LONG or SHORT allowed!")
                return False
                
            price_x18 = int(limit_price * 10**18)
            
            # Align to size and price increments
            size_increment = params_dict["size_increment_x18"]
            price_increment = params_dict["price_increment_x18"]
            
            abs_amount_x18 = abs(amount_x18)
            abs_amount_x18 = (abs_amount_x18 // size_increment) * size_increment
            amount_x18 = abs_amount_x18 if amount_x18 >= 0 else -abs_amount_x18
            
            price_x18 = (price_x18 // price_increment) * price_increment
            
            if amount_x18 == 0:
                logger.error(f"[NadoTradingService] ❌ Order amount is zero after step size alignment.")
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
            
            logger.warning(
                f"[NADO TIME DEBUG] "
                f"nonce={nonce}, "
                f"recv_time={decoded_recv_time}, "
                f"local_now={now_ms}, "
                f"ttl={decoded_recv_time - now_ms}ms"
            )
            
            # Use official SDK appendix builder
            try:
                from nado_protocol.utils.order import build_appendix
                try:
                    from nado_protocol.utils.expiration import OrderType
                    order_type = OrderType.IOC
                except ImportError:
                    # Fallback if OrderType is in a different module
                    order_type = 1 # Assuming IOC is 1, or use whatever enum Nado SDK uses
                
                appendix = build_appendix(order_type=order_type)
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
            
            logger.warning(
                f"[NADO FINAL ORDER] "
                f"product={product_id}, "
                f"nonce={order.nonce}, "
                f"decoded_recv={int(order.nonce) >> 20}, "
                f"ttl={(int(order.nonce) >> 20) - (time.time_ns() // 1_000_000)}ms"
            )
            
            logger.warning(
                f"[NADO DEBUG] "
                f"Gateway={getattr(self.client.context.engine_client, 'url', 'Unknown')} "
                f"ChainID={getattr(self.client.context.engine_client, 'chain_id', 'Unknown')}"
            )
            
            # Pure SDK call (runs synchronously in thread to avoid blocking)
            try:
                res = await asyncio.to_thread(self.client.market.place_order, params)
            except Exception as e:
                err_str = str(e)
                print(f"❌ [NadoTradingService] Ошибка размещения ордера на бирже: {err_str}")
                if "2070" in err_str and "maximum open interest" in err_str:
                    logger.warning(f"[NadoTradingService] ⚠️ Market OI limit reached for {symbol} (Testnet limitation). Order rejected.")
                else:
                    logger.error(f"[NadoTradingService] ❌ Order placement failed: {e}")
                return False
                
            digest = res.data.digest if res.data else None
            
            if not digest:
                logger.error(f"[NadoTradingService] ❌ Order placement failed, no digest returned.")
                return False
                
            logger.info(f"[NadoTradingService] ✅ Order Accepted by Sequencer. Digest: {digest}")
            logger.info(f"[NadoTradingService] ⏳ Waiting for sequencer fill confirmation...")
            
            filled = False
            actual_filled_x18 = 0
            
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
                                quote_f = abs(float(order_info.quote_filled))
                                base_f = abs(float(order_info.base_filled))
                                if base_f > 0:
                                    actual_entry_price = quote_f / base_f
                                    logger.info(f"[NadoTradingService] 🔍 Exact Entry Price from fill: {actual_entry_price}")
                            except Exception:
                                pass
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
                                quote_f = abs(float(final_order.quote_filled))
                                base_f = abs(float(final_order.base_filled))
                                if base_f > 0:
                                    actual_entry_price = quote_f / base_f
                                    logger.info(f"[NadoTradingService] 🔍 Exact Entry Price from fill on final check: {actual_entry_price}")
                            except Exception:
                                pass
                            logger.info(f"[NadoTradingService] ✅ Order {digest} FILLED on final check!")
                except Exception as e:
                    logger.warning(f"[NadoTradingService] ⚠️ Final order status check failed for {digest}: {e}")
                    
                if not filled:
                    logger.error(f"[NadoTradingService] ❌ Order {digest} was accepted but NOT filled within 10s. Cancelling to prevent race condition...")
                    try:
                        from nado_protocol.engine_client.types.execute import CancelOrdersParams
                        cancel_params = CancelOrdersParams(
                            productIds=[product_id],
                            digests=[digest],
                            sender=sender
                        )
                        await asyncio.to_thread(self.client.market.cancel_orders, cancel_params)
                        logger.info(f"[NadoTradingService] 🗑️ Order {digest} cancelled successfully.")
                    except Exception as cancel_e:
                        logger.error(f"[NadoTradingService] ❌ Failed to cancel timed out order {digest}: {cancel_e}")
                    
                    return False
            
            # --- Actual Risk vs Approved Risk Check (CRITICAL-13) ---
            actual_base_amount = abs(actual_filled_x18) / 1e18
            expected_risk_usd = actual_base_amount * abs(entry_price - sl_price)
            actual_risk_usd = actual_base_amount * abs(actual_entry_price - sl_price)
            
            # If slippage caused the risk to increase by more than 10% (with a strict $1.00 floor to ignore dust and normal spread)
            if sl_price > 0 and expected_risk_usd > 0 and actual_risk_usd > expected_risk_usd + max(expected_risk_usd * 0.10, 1.00):
                logger.error(
                    f"[NadoTradingService] ❌ FATAL SLIPPAGE: Actual risk (${actual_risk_usd:.2f}) exceeds approved risk "
                    f"(${expected_risk_usd:.2f}) due to bad execution price ({actual_entry_price:.4f} vs expected {entry_price:.4f}). "
                    f"Emergency closing position to protect capital."
                )
                try:
                    await self.force_close_position(symbol, bypass_check=True)
                except Exception as e:
                    logger.error(f"[NadoTradingService] ⚠️ Failed to close high-risk position {symbol}: {e}")
                return False
                
            # --- Partial Fill Guard (CRITICAL-4) ---
            fill_ratio = abs(actual_filled_x18) / abs(amount_x18) if amount_x18 != 0 else 0
            if fill_ratio < 0.95:
                logger.error(f"[NadoTradingService] ❌ PARTIAL FILL {fill_ratio*100:.1f}%. Closing immediately to prevent slivers.")
                try:
                    await self.force_close_position(symbol, bypass_check=True)
                except Exception as e:
                    logger.error(f"[NadoTradingService] ⚠️ Failed to close partial position {symbol}: {e}")
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
            price_increment_base = float(price_increment) / 1e18
            
            # Place Native Stop Loss
            if sl_price > 0:
                exec_price = sl_price * 0.9 if direction.upper() == "LONG" else sl_price * 1.1
                
                # Convert to integer x18 representation FIRST to avoid float math imprecision
                exec_price_x18 = int(exec_price * 10**18)
                trigger_price_x18 = int(sl_price * 10**18)
                
                # Round perfectly using integer arithmetic
                exec_price_x18 = (exec_price_x18 // price_increment) * price_increment
                trigger_price_x18 = (trigger_price_x18 // price_increment) * price_increment
                
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
                            f"[NadoTradingService] ℹ️ Отложенный SL не выставлен на бирже (объем ${notional_usd:.2f} < $100 лимита биржи). "
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
                exec_price = tp_price * 0.9 if direction.upper() == "LONG" else tp_price * 1.1
                
                exec_price_x18 = int(exec_price * 10**18)
                trigger_price_x18 = int(tp_price * 10**18)
                
                exec_price_x18 = (exec_price_x18 // price_increment) * price_increment
                trigger_price_x18 = (trigger_price_x18 // price_increment) * price_increment
                
                tp_digest = None
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
                            f"[NadoTradingService] ℹ️ Отложенный TP не выставлен на бирже (объем ${notional_usd:.2f} < $100 лимита биржи). "
                            f"Позиция полностью защищена программным Take-Profit на {tp_price:.4f} через Fast Monitor (5с)."
                        )
                    else:
                        logger.warning(
                            f"[NadoTradingService] ⚠️ Биржевой TP не выставлен ({e}). "
                            f"Позиция удерживается под программной защитой Take-Profit на {tp_price:.4f}!"
                        )
            
            # Recalculate notional_usd to reflect actual fill amount
            notional_usd = abs(actual_filled_x18 / 1e18) * actual_entry_price
            
            # Store position state to prevent duplicate orders and track PnL
            self.active_positions[symbol] = {
                "direction": direction.upper(),
                "entry_price": actual_entry_price,
                "size_usd": notional_usd,
                "notional_usd": notional_usd,
                "margin_used": notional_usd / float(leverage) if leverage > 0 else notional_usd,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "leverage": leverage,
                "highest_price": actual_entry_price,
                "lowest_price": actual_entry_price,
                "protection_state": "PROTECTED",
                "atr_reference": atr_value,
                "product_id": product_id,
                "sender": sender,
                "sl_digest": sl_digest,
                "tp_digest": tp_digest,
                "sl_type": sl_type,
                "tp_type": tp_type if tp_price > 0 else None,
                "trigger_amount_x18": trigger_amount_x18,
                "contracts": abs(actual_filled_x18 / 1e18),
                "accumulated_realized_pnl": 0.0,
                "original_thesis": original_thesis,
                "native_triggers_placed": bool(sl_digest or tp_digest),
                "open_time": time.time()
            }
            if tp_price > 0:
                try:
                    self.initialize_partial_tp_plan(symbol)
                except Exception as plan_err:
                    logger.warning(f"[NadoTradingService] ⚠️ Could not initialize partial TP plan for {symbol}: {plan_err}")
            self._save_positions()
                
            return True
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to place order: {e}")
            try:
                import traceback
                with open("logs/nado_errors.txt", "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {symbol} {direction} ERROR: {type(e).__name__}: {e}\n{traceback.format_exc()}\n")
            except Exception:
                pass
            return False

    def _load_state(self):
        from core.state_store import StateStore
        state_file = "data/memory/nado_state.json"
        state = StateStore.load(state_file)
        if "initial_balance" in state:
            self._initial_balance = float(state["initial_balance"])
            logger.info(f"[NadoTradingService] 💾 Loaded initial balance: {self._initial_balance}")
        if "daily_start_balance" in state and state["daily_start_balance"] is not None:
            self._daily_start_balance = float(state["daily_start_balance"])
        if "daily_start_date" in state and state["daily_start_date"] is not None:
            self._daily_start_date = str(state["daily_start_date"])
        if "win_count" in state:
            self.win_count = int(state["win_count"])
        if "loss_count" in state:
            self.loss_count = int(state["loss_count"])
        if "_daily_start_total_trades" in state and state["_daily_start_total_trades"] is not None:
            self._daily_start_total_trades = int(state["_daily_start_total_trades"])
        else:
            self._daily_start_total_trades = self.win_count + self.loss_count
        if "recent_streak" in state:
            self.recent_streak = state["recent_streak"]
        if "cooldown_until" in state:
            self.cooldown_until = float(state["cooldown_until"])
        if "_last_cooldown_processed_len" in state:
            self._last_cooldown_processed_len = int(state["_last_cooldown_processed_len"])

    def _save_state(self):
        from core.state_store import StateStore
        state_file = "data/memory/nado_state.json"
        StateStore.save(state_file, {
            "initial_balance": self._initial_balance,
            "daily_start_balance": getattr(self, "_daily_start_balance", None),
            "daily_start_date": getattr(self, "_daily_start_date", None),
            "_daily_start_total_trades": getattr(self, "_daily_start_total_trades", 0),
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "recent_streak": self.recent_streak,
            "cooldown_until": getattr(self, "cooldown_until", 0.0),
            "_last_cooldown_processed_len": getattr(self, "_last_cooldown_processed_len", 0)
        })

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
        """Loads persisted active positions from disk."""
        try:
            from core.state_store import StateStore
            state_file = "data/memory/active_positions.json"
            data = StateStore.load(state_file)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to load active positions from disk: {e}")
            return {}

    async def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """Checks if a position was closed natively by Nado (TP/SL trigger)."""
        closed_reports = []
        if not self.is_connected or symbol not in self.active_positions:
            return closed_reports
            
        try:
            # Bypass cache for checking closures to be perfectly accurate
            positions = await self.get_active_positions(bypass_cache=True)
            
            # If the position is no longer in the active list, it was closed natively!
            base_symbol = symbol.split('-')[0].upper()
            still_open = False
            target_pnl = 0.0
            
            for p in positions:
                p_base = p["symbol"].split('-')[0].upper()
                if p_base == base_symbol:
                    still_open = True
                    break
                    
            if not still_open:
                # Position is gone, meaning Nado native Trigger Order (SL/TP) executed!
                logger.info(f"[NadoTradingService] ⚡ Native Trigger executed for {symbol}! Position closed on-chain.")
                
                pos = self.active_positions[symbol]
                direction = str(pos.get("direction", "LONG")).upper()
                entry_price = float(pos.get("entry_price") or 0.0)
                size_usd = float(pos.get("size_usd") or 0.0)
                
                # Fetch fresh mark/oracle price if current_price was passed as 0 or missing
                if current_price <= 0:
                    prod_id = pos.get("product_id") or self.product_map.get(base_symbol)
                    if prod_id is not None and getattr(self, "_market_cache", None) and prod_id in self._market_cache:
                        m_info = self._market_cache[prod_id]
                        if hasattr(m_info, "oracle_price_x18"):
                            current_price = float(m_info.oracle_price_x18) / 1e18
                        elif hasattr(m_info, "product") and hasattr(m_info.product, "oracle_price_x18"):
                            current_price = float(m_info.product.oracle_price_x18) / 1e18

                # Safe fallback price if market price is completely unavailable
                effective_market_price = current_price if current_price > 0 else (entry_price if entry_price > 0 else 1.0)

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
                
                # --- Exact Execution Price (CRITICAL-6) ---
                try:
                    from nado_protocol.indexer_client.types.query import IndexerSubaccountHistoricalOrdersParams
                    
                    address = self.wallet.get_address()
                    res_sub = await asyncio.to_thread(self.client.subaccount.get_subaccounts, address)
                    subaccount_id = res_sub.subaccounts[0].subaccount if res_sub and res_sub.subaccounts else None
                    prod_id = pos.get("product_id") or self.product_map.get(base_symbol)
                    
                    if subaccount_id and prod_id is not None:
                        params = IndexerSubaccountHistoricalOrdersParams(
                            subaccounts=[subaccount_id],
                            product_ids=[prod_id],
                            limit=20,
                        )
                        history = await asyncio.to_thread(self.client.market.get_subaccount_historical_orders, params)
                        
                        if history and history.orders:
                            expected_base = abs(float(pos.get("trigger_amount_x18", 0) or 0)) / 1e18 if pos.get("trigger_amount_x18") else 0.0
                            if expected_base == 0:
                                expected_base = abs(float(pos.get("amount", 0) or pos.get("contracts", 0) or 0.0))
                            if expected_base == 0 and entry_price > 0 and size_usd > 0:
                                expected_base = size_usd / entry_price

                            open_time = float(pos.get("open_time", 0) or pos.get("timestamp", 0) or getattr(self, "_start_time", 0))

                            # 1. Match closing order (opposite sign of position direction)
                            for order in history.orders:
                                quote = abs(float(order.quote_filled))
                                base = abs(float(order.base_filled))
                                bf = float(order.base_filled)
                                
                                # Strict Time Check: Must have executed AFTER position was opened/started
                                if hasattr(order, "timestamp"):
                                    ts = getattr(order, "timestamp", None)
                                    if ts is not None and str(ts).strip() != "None":
                                        try:
                                            order_ts_sec = float(ts) / 1000.0 if len(str(ts)) > 10 else float(ts)
                                            if open_time > 0 and order_ts_sec < (open_time - 5.0): # 5s clock skew tolerance
                                                continue
                                        except (ValueError, TypeError):
                                            pass
                                elif open_time > 0:
                                    # Order has no timestamp, cannot safely attribute
                                    continue
                                        
                                if (direction == "LONG" and bf < 0) or (direction == "SHORT" and bf > 0):
                                    if base > 0:
                                        # Size Match Check (allow up to 10% tolerance for exchange rounding/fees)
                                        if expected_base > 0 and abs(base - expected_base) / max(expected_base, 1e-6) > 0.10:
                                            continue
                                            
                                        exec_price = quote / base
                                        if exec_price > 0:
                                            logger.info(f"[NadoTradingService] 🔍 Found exact exit execution price from history: {exec_price}")
                                            exit_price = exec_price
                                            triggered_by = f"{triggered_by} (EXACT)"
                                            break
                            
                            # 2. Match and verify entry order from history to ensure 100% accurate PnL
                            for order in history.orders:
                                quote = abs(float(order.quote_filled))
                                base = abs(float(order.base_filled))
                                bf = float(order.base_filled)
                                
                                # Strict Size Match Check
                                if expected_base > 0 and abs(base - expected_base) / max(expected_base, 1e-6) > 0.10:
                                    continue
                                    
                                if (direction == "LONG" and bf > 0) or (direction == "SHORT" and bf < 0):
                                    if base > 0:
                                        hist_entry = quote / base
                                        if hist_entry > 0 and abs(hist_entry - entry_price) / max(entry_price, 1e-6) > 0.0005:
                                            logger.info(f"[NadoTradingService] 🎯 Corrected entry price from history: {entry_price:.4f} -> {hist_entry:.4f}")
                                            entry_price = hist_entry
                                        break
                except Exception as e:
                    logger.warning(f"[NadoTradingService] ⚠️ Could not fetch exact history for {symbol}, falling back to estimation: {e}")
                
                # Strict safety guarantee: exit_price must NEVER be <= 0
                if exit_price <= 0:
                    exit_price = effective_market_price if effective_market_price > 0 else entry_price

                if direction == "LONG":
                    gross_pnl = (exit_price - entry_price) / entry_price * size_usd
                else:
                    gross_pnl = (entry_price - exit_price) / entry_price * size_usd
                    
                # Exact round-trip fee approximation (0.05% * 2)
                estimated_fees = size_usd * 0.001
                target_pnl = gross_pnl - estimated_fees
                accumulated_slices = float(pos.get("accumulated_realized_pnl", 0.0))
                if accumulated_slices != 0.0:
                    target_pnl += accumulated_slices
                    logger.info(f"[NadoTradingService] 🧩 Total Trade Net PnL (including partial TP slices ${accumulated_slices:.2f}): ${target_pnl:.2f}")
                logger.info(f"[NadoTradingService] 💰 Gross PnL: ${gross_pnl:.2f}, Est Fees: ${estimated_fees:.2f} -> Net PnL: ${target_pnl:.2f}")
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
                
                if target_pnl > 0.001:
                    self._update_streak_and_cooldown("WIN")
                elif target_pnl < -0.001:
                    self._update_streak_and_cooldown("LOSS")
                else:
                    logger.info(f"[NadoTradingService] ℹ️ Position closed at Breakeven (PnL: ${target_pnl:.2f}). Streak not modified.")
                    self._save_state()
                    
                closure_tag = f"CLOSED_ON_CHAIN ({triggered_by})" if triggered_by != "MANUAL_OR_ON_CHAIN" else "CLOSED_ON_CHAIN"
                closed_reports.append({
                    "symbol": symbol,
                    "direction": direction,
                    "triggered_by": closure_tag,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_usd": target_pnl,
                    "roi_pct": (target_pnl / pos.get("margin_used", size_usd) * 100) if size_usd > 0 else 0,
                    "original_thesis": pos.get("original_thesis", "") if pos else ""
                })
            elif still_open:
                pos = self.active_positions.get(symbol)
                if pos and not pos.get("sl_digest"):
                    direction = pos.get("direction", "LONG").upper()
                    sl_price = float(pos.get("sl_price", 0.0))
                    tp_price = float(pos.get("tp_price", 0.0))
                    
                    hit_sl = (direction == "LONG" and sl_price > 0 and current_price <= sl_price) or \
                             (direction == "SHORT" and sl_price > 0 and current_price >= sl_price)
                    hit_tp = (direction == "LONG" and tp_price > 0 and current_price >= tp_price) or \
                             (direction == "SHORT" and tp_price > 0 and current_price <= tp_price)
                             
                    if hit_sl or hit_tp:
                        if pos.get("is_closing"):
                            logger.info(f"[NadoTradingService] ⏳ Skipping {symbol} — already being closed by Fast Monitor.")
                            return closed_reports
                        pos["is_closing"] = True
                        reason = "SL" if hit_sl else "TP"
                        logger.info(f"[NadoTradingService] 🚨 Stage 1.5 Software {reason} triggered for {symbol} at {current_price:.4f}!")
                        if not hasattr(self, "_position_locks"):
                            self._position_locks = {}
                        lock = self._position_locks.setdefault(symbol, asyncio.Lock())
                        async with lock:
                            success, realized_pnl = await self.force_close_position(symbol, bypass_check=True)
                        if success:
                            closed_reports.append({
                                "symbol": symbol,
                                "direction": direction,
                                "triggered_by": f"SOFTWARE_{reason}",
                                "entry_price": pos.get("entry_price", current_price),
                                "exit_price": current_price,
                                "pnl_usd": realized_pnl,
                                "roi_pct": (realized_pnl / pos.get("margin_used", pos.get("size_usd", 1.0)) * 100) if pos.get("size_usd", 0) > 0 else 0,
                                "original_thesis": pos.get("original_thesis", "")
                            })
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to check native position state: {e}")
            
        return closed_reports

    async def force_close_position(self, symbol: str, bypass_check: bool = False, max_retries: int = 3) -> tuple:
        """Manually closes a position on Nado by firing a close_position market order.
        Includes retry logic and backoff to handle Indexer lag immediately after entry."""
        if not self.is_connected:
            return False, 0.0
            
        try:
            base_symbol = symbol.split('-')[0].upper()
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
                positions = await self.get_active_positions(bypass_cache=True)
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
                        logger.warning(f"[NadoTradingService] ⚠️ bypass_check=True, but position {symbol} not found in Indexer. PnL will be 0.")
                            
                # Fire close_position via SDK
                try:
                    res = await asyncio.to_thread(self.client.market.close_position, subaccount, product_id)
                    logger.info(f"[NadoTradingService] 🧹 Successfully forced closed {symbol}. TX: {res}")
                    
                    # Cancel any remaining trigger orders on exchange
                    pos_data = self.active_positions.get(symbol) or self.active_positions.get(base_symbol) or {}
                    sl_dig = pos_data.get("sl_digest")
                    tp_dig = pos_data.get("tp_digest")
                    digests_to_cancel = [d for d in [sl_dig, tp_dig] if d]
                    if digests_to_cancel:
                        await self._cancel_trigger_digests(product_id, digests_to_cancel, sender=pos_data.get("sender", subaccount))

                    # Clean up local cache and update stats
                    if symbol in self.active_positions:
                        del self.active_positions[symbol]
                    alias_key = base_symbol if '-' in symbol else f"{base_symbol}-USD"
                    if alias_key in self.active_positions:
                        del self.active_positions[alias_key]
                    self._save_positions()
                        
                    pnl = target_pos["pnl"] if target_pos else 0.0
                    if pnl > 0.001:
                        self._update_streak_and_cooldown("WIN")
                    elif pnl < -0.001:
                        self._update_streak_and_cooldown("LOSS")
                    else:
                        logger.info(f"[NadoTradingService] ℹ️ Force close at breakeven/near-zero PnL (${pnl:.4f}). Streak unchanged.")
                        self._save_state()
                        
                    return True, pnl
                except Exception as close_e:
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

        # Calculate base amount in x18
        amount_x18 = int(contracts_to_close * 10**18)
        # Floor strictly down to size_increment
        amount_x18 = (amount_x18 // size_increment) * size_increment
        if amount_x18 <= 0:
            return PartialCloseResult(success=False, error="Order amount is zero after size_increment alignment.")

        # Determine counter-direction and limit price:
        # LONG -> counter-order is SELL (negative amount)
        # SHORT -> counter-order is BUY (positive amount)
        if direction == "LONG":
            order_amount_x18 = -amount_x18
            limit_price = current_price * (1.0 - max_slippage_pct)
        elif direction == "SHORT":
            order_amount_x18 = amount_x18
            limit_price = current_price * (1.0 + max_slippage_pct)
        else:
            return PartialCloseResult(success=False, error=f"Invalid position direction: {direction}")

        # Align limit price to price_increment
        price_x18 = int(limit_price * 10**18)
        price_x18 = (price_x18 // price_increment) * price_increment

        # Build appendix with reduce_only=True
        try:
            from nado_protocol.utils.order import build_appendix, order_reduce_only
            from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
            from nado_protocol.utils.nonce import gen_order_nonce
            from nado_protocol.utils.bytes32 import subaccount_to_hex
            from nado_protocol.engine_client.types.execute import OrderParams, PlaceOrderParams

            appendix = build_appendix(order_type=OrderType.IOC, reduce_only=True)
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
            return PartialCloseResult(success=False, error=f"Order submission failed: {place_err}")

        digest = getattr(getattr(res, "data", None), "digest", None)
        if not digest:
            return PartialCloseResult(success=False, error="Order rejected or no digest returned by sequencer.")

        logger.info(f"[NadoTradingService] ⚡ Partial Close IOC order placed. Digest: {digest}. Waiting for fill confirmation...")

        # Poll sequencer / indexer for fill verification
        filled = False
        actual_filled_x18 = 0
        actual_fill_price = current_price

        for attempt in range(max_poll_attempts):
            await asyncio.sleep(1.0)
            try:
                hist_data = await asyncio.to_thread(self.client.market.get_historical_orders_by_digest, [digest])
                if hist_data and getattr(hist_data, "orders", None):
                    order_info = hist_data.orders[0]
                    base_f = abs(int(getattr(order_info, "base_filled", 0)))
                    if base_f > 0:
                        filled = True
                        actual_filled_x18 = base_f
                        try:
                            quote_f = abs(float(getattr(order_info, "quote_filled", 0)))
                            if base_f > 0:
                                actual_fill_price = quote_f / base_f
                        except Exception:
                            actual_fill_price = current_price
                        break
            except Exception as poll_e:
                logger.warning(f"[NadoTradingService] ⚠️ Polling digest {digest} error: {poll_e}")

        if not filled or actual_filled_x18 == 0:
            return PartialCloseResult(
                success=False,
                digest=digest,
                requested_contracts=contracts_to_close,
                error="IOC order expired without fills (market moved or lack of matching depth)."
            )

        actual_filled_contracts = actual_filled_x18 / 1e18
        # Calculate gross PnL and subtract estimated taker trading fee (~0.05%) for accurate net accounting
        fee_rate = 0.0005
        fee_usd = actual_filled_contracts * actual_fill_price * fee_rate
        if direction == "LONG":
            gross_pnl = actual_filled_contracts * (actual_fill_price - entry_price)
        else:
            gross_pnl = actual_filled_contracts * (entry_price - actual_fill_price)
        slice_pnl = round(gross_pnl - fee_usd, 4)

        is_partial = actual_filled_contracts < (contracts_to_close * 0.99)

        logger.info(
            f"[NadoTradingService] ✅ Partial Close FILLED for {symbol}: "
            f"{actual_filled_contracts:.4f} contracts @ {actual_fill_price:.4f}. "
            f"Slice PnL: ${slice_pnl:.2f}. Partial fill: {is_partial}"
        )

        return PartialCloseResult(
            success=True,
            digest=digest,
            requested_contracts=contracts_to_close,
            filled_contracts=actual_filled_contracts,
            fill_price=actual_fill_price,
            realized_pnl=slice_pnl,
            is_partial_fill=is_partial
        )

    def initialize_partial_tp_plan(self, symbol: str) -> Optional[Any]:
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

        try:
            from services.partial_tp_calculator import PartialTPCalculator
            import time
            plan = PartialTPCalculator.calculate_plan(
                symbol=canonical_sym,
                direction=direction,
                entry_price=entry_price,
                tp_final_price=tp_price,
                total_contracts=total_contracts,
                market_params=params,
                created_at=time.time()
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
            # Revert from SUBMITTING to PENDING so monitor can retry on next valid tick
            tier.status = "PENDING"
            tier.error_message = result.error
            pos["partial_tp_plan"] = plan.to_dict()
            self._save_positions()
            logger.warning(f"[NadoTradingService] ⚠️ Tier {tier_index} execution failed for {symbol}: {result.error}. Reverted to PENDING.")
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
        rem_x18 = int(plan.remaining_contracts * 10**18)
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
        # Strict invariant: only finalize closure if remaining contracts are zero / dust (< min_size)
        # AND tier is completely filled (status == "FILLED") or remaining_contracts <= 1e-6.
        if plan.remaining_contracts <= 1e-6 or (tier.is_final and tier.status == "FILLED" and plan.remaining_contracts < plan.min_size):
            logger.info(f"[NadoTradingService] 🏁 All partial tiers completed for {canonical_sym}! Finalizing position closure.")
            self._finalize_partial_tp_closure(
                symbol=canonical_sym,
                pos=pos,
                exit_price=result.fill_price,
                triggered_by=f"PARTIAL_TP_TIER_{tier_index}"
            )
        else:
            self._save_positions()
            # Phase 4: BE Protection & SL Resizing
            if tier.action == "PROTECT_BREAKEVEN":
                pos["protection_state"] = "BREAK_EVEN"
                plan.be_activated = True
                entry_p = float(pos.get("entry_price") or 0.0)
                direction = str(pos.get("direction", "LONG")).upper()
                if entry_p > 0:
                    fee_pct = 0.001
                    product_id = pos.get("product_id") or self.product_map.get(canonical_sym.split('-')[0].upper())
                    params = self._get_market_parameters(product_id) if product_id is not None else {}
                    price_inc = float(params.get("price_increment_x18", 0)) / 1e18 if params.get("price_increment_x18") else 0.001
                    cost_buffer = entry_p * fee_pct
                    if direction == "LONG":
                        raw_be = entry_p + cost_buffer
                        be_price = math.ceil(raw_be / price_inc) * price_inc if price_inc > 0 else raw_be
                        current_sl = float(pos.get("sl_price") or 0.0)
                        if be_price > current_sl:
                            pos["sl_price"] = round(be_price, 6)
                    else:
                        raw_be = entry_p - cost_buffer
                        be_price = math.floor(raw_be / price_inc) * price_inc if price_inc > 0 else raw_be
                        current_sl = float(pos.get("sl_price") or 0.0)
                        if current_sl <= 0 or be_price < current_sl:
                            pos["sl_price"] = round(be_price, 6)
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()
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

    def reconcile_partial_tp_with_chain(self, symbol: str, on_chain_contracts: float) -> bool:
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
            self._finalize_partial_tp_closure(
                symbol=canonical_sym,
                pos=pos,
                exit_price=float(pos.get("entry_price", 0.0)),
                triggered_by="ON_CHAIN_RECONCILED_CLOSED"
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

    def _finalize_partial_tp_closure(self, symbol: str, pos: dict, exit_price: float, triggered_by: str) -> dict:
        """
        Cleans up position from active_positions, cancels active trigger orders,
        updates win/loss streaks and saves state after final tier or full on-chain exit.
        """
        canonical_sym = symbol.replace('/', '-').strip().upper()
        if '-' not in canonical_sym:
            canonical_sym = f"{canonical_sym}-USD"
        base_sym = canonical_sym.split('-')[0].upper()

        total_pnl = float(pos.get("accumulated_realized_pnl", 0.0))
        entry_price = float(pos.get("entry_price", 0.0))
        initial_size = float(pos.get("size_usd", 0.0))

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

        # Update streak and cooldown
        if total_pnl > 0.001:
            self._update_streak_and_cooldown("WIN")
        elif total_pnl < -0.001:
            self._update_streak_and_cooldown("LOSS")
        else:
            self._save_state()

        self._save_positions()

        logger.info(
            f"[NadoTradingService] 🎯 Position {canonical_sym} completely finalized! "
            f"Exit: {exit_price:.4f}, Total PnL: ${total_pnl:.2f}, Trigger: {triggered_by}"
        )

        return {
            "symbol": canonical_sym,
            "direction": pos.get("direction", "LONG"),
            "triggered_by": triggered_by,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_usd": total_pnl,
            "roi_pct": (total_pnl / pos.get("margin_used", initial_size) * 100) if initial_size > 0 else 0,
            "original_thesis": pos.get("original_thesis", "")
        }

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
                
                # In Nado / Vertex engine contracts, book_info.min_size is set to a flat
                # dummy default of 100 * 1e18 across all markets (BTC, ETH, stocks, altcoins).
                # Real minimum executable order size on Nado is 1 size_increment.
                raw_min_size = float(getattr(market_info.book_info, "min_size", 0)) / 1e18
                if raw_min_size == 100.0 or raw_min_size <= 0.0:
                    params["min_size"] = params["size_increment_base"]
                else:
                    params["min_size"] = max(raw_min_size, params["size_increment_base"])

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
        """
        valid_digests = [d for d in digests if d and isinstance(d, str)]
        if not valid_digests or not self.is_connected or product_id is None:
            return False

        target_sender = sender or self.default_subaccount_id
        if not target_sender:
            return False

        try:
            from nado_protocol.trigger_client.types.execute import CancelTriggerOrdersParams
            cancel_params = CancelTriggerOrdersParams(
                productIds=[product_id],
                digests=valid_digests,
                sender=target_sender
            )
            await asyncio.to_thread(self.client.market.cancel_trigger_orders, cancel_params)
            logger.info(f"[NadoTradingService] 🗑️ Successfully cancelled {len(valid_digests)} trigger orders on product {product_id}: {valid_digests}")
            return True
        except Exception as e:
            logger.warning(f"[NadoTradingService] ⚠️ Failed to cancel trigger orders {valid_digests}: {e}")
            return False

    async def sync_with_exchange(self, is_startup: bool = False) -> None:
        """Syncs local state with active positions on Nado."""
        if not self.is_connected:
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
                orphans = [k for k in list(self.active_positions.keys()) if k.split('-')[0].upper() not in on_chain_bases]
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
                                        exact_entry = abs(qf) / abs(bf)
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

        if not hasattr(self, "_position_locks"):
            self._position_locks = {}
        
        lock = self._position_locks.setdefault(symbol, asyncio.Lock())
        
        async with lock:
            # Re-fetch pos to ensure we have the latest state inside the lock
            pos = self.active_positions.get(symbol)
            if not pos:
                return False
                
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
                    pos["protection_state"] = new_protection_state
                if new_contracts is not None and new_contracts > 0:
                    amount_base = float(new_contracts)
                    amount_x18_val = int(amount_base * 10**18)
                    pos["trigger_amount_x18"] = str(-amount_x18_val if direction == "LONG" else amount_x18_val)
                self._save_positions()
                logger.info(f"[NadoTradingService] 🛡️ Software Stop Loss updated for {symbol} -> {new_sl_price:.4f}")
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
                
            # 2. Cancel old SL
            await self._cancel_trigger_digests(product_id, [sl_digest], sender=sender)
            pos["sl_digest"] = None
            pos["sl_price"] = new_sl_price
            if new_protection_state:
                pos["protection_state"] = new_protection_state

            # Compute trigger amount (strictly aligned to remaining contracts)
            if new_contracts is not None and new_contracts > 0:
                amount_base = float(new_contracts)
            elif "contracts" in pos and float(pos["contracts"]) > 0:
                amount_base = float(pos["contracts"])
            else:
                amount_base = abs(float(pos.get("trigger_amount_x18", 0))) / 1e18

            amount_x18_val = int(amount_base * 10**18)
            trigger_amount_str = str(-amount_x18_val if direction == "LONG" else amount_x18_val)
            pos["trigger_amount_x18"] = trigger_amount_str
                
            # 3. Place new SL immediately with retry
            exec_price = new_sl_price * 0.9 if direction == "LONG" else new_sl_price * 1.1
            
            exec_price_x18 = int(exec_price * 10**18)
            trigger_price_x18 = int(new_sl_price * 10**18)
            
            exec_price_x18 = (exec_price_x18 // price_increment) * price_increment
            trigger_price_x18 = (trigger_price_x18 // price_increment) * price_increment
            
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
                        trigger_type=pos["sl_type"],
                        reduce_only=True
                    )
                    
                    if sl_res and sl_res.data and sl_res.data.digest:
                        new_digest = sl_res.data.digest
                        break
                    else:
                        raise ValueError("Missing digest in Nado response")
                except Exception as e:
                    last_err = e
                    logger.warning(f"[NadoTradingService] ⚠️ On-chain SL placement attempt {attempt+1}/{max_trigger_retries} failed for {symbol}: {e}")
                    if attempt < max_trigger_retries - 1:
                        await asyncio.sleep(0.5)

            if new_digest:
                pos["sl_digest"] = new_digest
                pos["unverified_triggers"] = False
                pos["sl_protection_mode"] = "EXCHANGE_PROTECTED"
                self._save_positions()
                logger.info(f"[NadoTradingService] ✅ Stop Loss updated on-chain for {symbol} -> {new_sl_price:.4f} (digest: {new_digest})")
                return True
            else:
                pos["sl_digest"] = None
                pos["unverified_triggers"] = True
                pos["sl_protection_mode"] = "SOFTWARE_ONLY"
                self._save_positions()
                logger.warning(
                    f"[NadoTradingService] ⚠️ Failed to place NEW on-chain SL for {symbol} after {max_trigger_retries} attempts ({last_err}). "
                    f"Active Software Stop Loss remains ENFORCED at {new_sl_price:.4f} via Fast Price Monitor."
                )
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

        # Resolve hanging SUBMITTING tier (if any from previous crash)
        if tier.status == "SUBMITTING":
            tier.status = "PARTIALLY_FILLED" if tier.filled_contracts > 0 else "PENDING"
            pos["partial_tp_plan"] = plan.to_dict()
            self._save_positions()

        if tier.status not in ("PENDING", "PARTIALLY_FILLED"):
            return None

        contracts_to_close = tier.remaining_tier_contracts
        if contracts_to_close < plan.min_size:
            tier.status = "FILLED"
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

        if not hasattr(self, "_position_locks"):
            self._position_locks = {}
        lock = self._position_locks.setdefault(canonical_sym, asyncio.Lock())

        async with lock:
            # Re-fetch pos and tier status inside lock
            pos = self.active_positions.get(canonical_sym) or self.active_positions.get(symbol)
            if not pos or not pos.get("partial_tp_plan") or pos.get("is_closing"):
                return None
            plan = PartialTPPlan.from_dict(pos["partial_tp_plan"])
            tier = plan.get_active_or_next_tier()
            if not tier:
                return None
            if tier.status == "SUBMITTING":
                tier.status = "PARTIALLY_FILLED" if tier.filled_contracts > 0 else "PENDING"
            if tier.status not in ("PENDING", "PARTIALLY_FILLED"):
                return None

            contracts_to_close = tier.remaining_tier_contracts
            if contracts_to_close < plan.min_size:
                tier.status = "FILLED"
                pos["partial_tp_plan"] = plan.to_dict()
                self._save_positions()
                return None

            tier.status = "SUBMITTING"
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

            applied = self.apply_partial_close_to_position(
                symbol=canonical_sym,
                tier_index=tier.tier_index,
                result=close_result
            )

            if not close_result.success or not applied:
                logger.warning(
                    f"[NadoTradingService] ⚠️ Tier {tier.tier_index} execution failed for {canonical_sym}: {close_result.error}"
                )
                return None

            # Prepare Telegram notification
            slice_pnl = close_result.realized_pnl
            pnl_emoji = "🎉" if slice_pnl >= 0 else "🔻"
            entry_p = float(pos.get("entry_price", 0.0))
            rem_contracts = plan.remaining_contracts - close_result.filled_contracts
            tier_fraction_pct = int(tier.fraction * 100)
            
            be_info = ""
            if tier.action == "PROTECT_BREAKEVEN":
                prot_mode = pos.get("sl_protection_mode", "EXCHANGE_PROTECTED")
                if prot_mode == "EXCHANGE_PROTECTED":
                    be_info = "\n🔒 *Защита:* Стоп-лосс на бирже перенесен в *БЕЗУБЫТОК* (+0.1% комиссия)!"
                else:
                    be_info = "\n🛡️ *Защита:* Включен *программный безубыток* (на бирже ордер не подтверждён или размер < $100)!"
            elif tier.is_final:
                be_info = "\n🏁 *Финал:* Все ступени Take-Profit выполнены! Позиция полностью зафиксирована."

            msg = (
                f"{pnl_emoji} *PARTIAL TAKE PROFIT / ЧАСТИЧНАЯ ФИКСАЦИЯ (TP{tier.tier_index} - {tier_fraction_pct}%)*\n\n"
                f"🪙 *Asset / Монета:* `{canonical_sym}`\n"
                f"📊 *Direction / Направление:* `{direction}`\n"
                f"🎯 *Target / Цель:* `${target_price:,.4f}` ➔ *Fill / Исполнение:* `${close_result.fill_price:,.4f}`\n"
                f"💵 *Entry / Вход:* `${entry_p:,.4f}`\n"
                f"💰 *Slice PnL / Прибыль части:* `+${slice_pnl:,.2f}`\n"
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

    async def start_fast_price_monitor(self):
        """
        Background task that updates highest/lowest prices for active positions.
        Runs continuously in the background (every SENTINEL_FAST_POLL_SEC seconds).
        """
        from core.config import config
        poll_interval = getattr(config, "SENTINEL_FAST_POLL_SEC", 5)
        logger.info(f"[NadoTradingService] ⚡ Fast Price Monitor started (interval: {poll_interval}s).")
        
        while True:
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
                            entry = pos.get("entry_price", 0)
                            if current_price > pos.get("highest_price", entry):
                                pos["highest_price"] = current_price
                            if current_price < pos.get("lowest_price", entry):
                                pos["lowest_price"] = current_price

                            # Software SL/TP Trigger Check (ALWAYS ACTIVE as a safety net)
                            # We check this regardless of sl_digest to cover exchange failures, dropped orders, or replacement windows
                            direction = pos.get("direction", "LONG").upper()
                            sl_price = float(pos.get("sl_price", 0.0))
                            tp_price = float(pos.get("tp_price", 0.0))
                            
                            hit_sl = (direction == "LONG" and sl_price > 0 and current_price <= sl_price) or \
                                     (direction == "SHORT" and sl_price > 0 and current_price >= sl_price)
                            
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
                                     
                            if (hit_sl or hit_tp) and not pos.get("is_closing"):
                                pos["is_closing"] = True
                                trigger_name = "STOP LOSS" if hit_sl else "TAKE PROFIT"
                                target_val = sl_price if hit_sl else tp_price
                                logger.info(
                                    f"[NadoTradingService] 🚨 Fast Monitor: Software {trigger_name} triggered for {symbol}! "
                                    f"Current: {current_price:.4f}, Target: {target_val:.4f}. Executing market close..."
                                )
                                
                                async def _close_and_notify_fast(sym=symbol, t_name=trigger_name, p_dict=dict(pos), cur_p=current_price):
                                    if not hasattr(self, "_position_locks"):
                                        self._position_locks = {}
                                    lock = self._position_locks.setdefault(sym, asyncio.Lock())
                                    async with lock:
                                        success, pnl = await self.force_close_position(sym, bypass_check=True)
                                    if success:
                                        pnl_emoji = "🎉" if pnl >= 0 else "🔻"
                                        slices_pnl = float(p_dict.get("accumulated_realized_pnl", 0.0))
                                        total_trade_pnl = pnl + slices_pnl
                                        pnl_display = f"${pnl:,.2f}"
                                        if slices_pnl != 0.0:
                                            pnl_display += f" (Общий PnL с учетом ступеней TP: `${total_trade_pnl:,.2f}`)"
                                        msg = (
                                            f"{pnl_emoji} *TRADE CLOSED / СДЕЛКА ЗАКРЫТА (FAST_MONITOR_{t_name})*\n\n"
                                            f"🪙 *Asset / Монета:* `{sym}`\n"
                                            f"📊 *Direction / Направление:* `{p_dict.get('direction', 'UNKNOWN')}`\n"
                                            f"🎯 *Entry / Вход:* `${p_dict.get('entry_price', 0):,.4f}` ➔ *Exit / Выход:* `${cur_p:,.4f}`\n"
                                            f"💰 *PnL:* {pnl_display}\n"
                                        )
                                        try:
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
