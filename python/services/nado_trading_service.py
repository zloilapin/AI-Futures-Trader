import logging
import asyncio
from typing import Dict, Any, List
from core.interfaces import BaseTradingService
from core.web3_wallet import Web3Wallet

logger = logging.getLogger("System_Core")

class NadoTradingService(BaseTradingService):
    """
    Adapter for executing trades on Nado DEX (Ink L2).
    Uses the official nado-protocol Python SDK (requires Python 3.12+).
    """
    
    def __init__(self):
        self.wallet = Web3Wallet()
        self.client = None
        self.is_connected = False
        self.active_positions = {}
        self.product_map: Dict[str, int] = {}
        self.default_subaccount_id = None
        
        # Stats tracking
        self.win_count = 0
        self.loss_count = 0
        self._init_sdk()

    def _init_sdk(self):
        if not self.wallet.is_configured():
            logger.error("[NadoTradingService] ❌ Wallet not configured. Nado execution disabled.")
            return

        try:
            # We import nado_protocol locally so the rest of the bot doesn't crash 
            # if the SDK isn't installed yet.
            from nado_protocol.client import create_nado_client, NadoClientMode, NadoClient
            
            # Initialize NadoClient with the private key
            self.client = create_nado_client(
                mode=NadoClientMode.MAINNET,
                signer=self.wallet.get_private_key()
            )
            self.is_connected = True
            
            # Fetch product map dynamically
            try:
                products = self.client.market.get_all_product_symbols()
                for p in products:
                    base_symbol = p.symbol.split('-')[0].upper()
                    self.product_map[base_symbol] = p.product_id
                
                # Fetch subaccounts to find the default one
                self.default_subaccount_id = None
                address = self.wallet.get_address()
                if address:
                    res = self.client.subaccount.get_subaccounts(address)
                    if res and res.subaccounts:
                        for sa in res.subaccounts:
                            if sa.subaccount_name == 'default':
                                self.default_subaccount_id = sa.subaccount
                                break
                
                logger.info(f"[NadoTradingService] ✅ Connected to Nado SDK. Loaded {len(self.product_map)} products. Default SA: {self.default_subaccount_id}")
            except Exception as e:
                logger.error(f"[NadoTradingService] ⚠️ Failed to load Nado initial state: {e}")
        except ImportError:
            logger.error("[NadoTradingService] ❌ 'nado-protocol' SDK not found! Please run 'pip install nado-protocol'")
            self.is_connected = False

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Returns the current portfolio balance from Nado."""
        if not self.is_connected or not self.default_subaccount_id:
            return {"balance": 0.0, "margin_used": 0.0, "free_margin": 0.0, "pnl": 0.0}
        
        try:
            summary = await asyncio.to_thread(self.client.subaccount.get_engine_subaccount_summary, self.default_subaccount_id)
            
            # SubaccountInfoData has a healths array. index 0 is Initial Margin health
            if hasattr(summary, "healths") and len(summary.healths) > 0:
                health = summary.healths[0]
                balance = float(health.assets) / 1e18
                margin_used = float(health.liabilities) / 1e18
                free_margin = float(health.health) / 1e18
            else:
                balance = margin_used = free_margin = 0.0
            
            pnl = 0.0
            active_count = len(await self.get_active_positions())
            
            total_trades = self.win_count + self.loss_count
            win_rate = round((self.win_count / total_trades) * 100, 1) if total_trades > 0 else 0.0
            
            return {
                "initial_balance": 39.11,
                "current_balance": round(balance, 2),
                "total_pnl_usd": round(balance - 39.11, 2),
                "total_pnl_pct": round(((balance - 39.11) / 39.11) * 100, 2) if balance else 0.0,
                "unrealized_pnl_usd": pnl,
                "unrealized_pnl": pnl,
                "roi_pct": round(((balance - 39.11) / 39.11) * 100, 2) if balance else 0.0,
                "available_margin": round(free_margin, 2),
                "used_margin": round(margin_used, 2),
                "active_positions_count": active_count,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "win_rate_pct": win_rate
            }
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to get portfolio summary: {e}")
            return {"balance": 0.0, "margin_used": 0.0, "free_margin": 0.0, "pnl": 0.0}

    async def get_active_positions(self) -> List[Dict[str, Any]]:
        """Returns active open positions from Nado."""
        if not self.is_connected:
            return []
            
        active_list = []
        try:
            # Reverse map for product_id -> symbol
            id_to_symbol = {v: k for k, v in self.product_map.items()}
            address = self.wallet.get_address()
            
            # Fetch all subaccounts for this address
            res = await asyncio.to_thread(self.client.subaccount.get_subaccounts, address)
            if not res or not res.subaccounts:
                return []
                
            for sa in res.subaccounts:
                pos_data = await asyncio.to_thread(self.client.market.get_isolated_positions, sa.subaccount)
                if not hasattr(pos_data, "isolated_positions") or not pos_data.isolated_positions:
                    continue
                    
                for pos in pos_data.isolated_positions:
                    base_amount = float(pos.base_balance.balance.amount) / 1e18
                    if abs(base_amount) < 1e-6:
                        continue # Ignore zero or dust positions
                        
                    product_id = pos.base_product.product_id
                    symbol = id_to_symbol.get(product_id, f"UNKNOWN-{product_id}")
                    current_price = float(pos.base_product.oracle_price_x18) / 1e18
                    size_usd = abs(base_amount) * current_price
                    direction = "LONG" if base_amount > 0 else "SHORT"
                    
                    # Merge with local cache for TP/SL and Entry
                    local_pos = self.active_positions.get(symbol, {})
                    entry_price = local_pos.get("entry_price", current_price)
                    
                    # Calculate PnL
                    if direction == "LONG":
                        pnl = (current_price - entry_price) * abs(base_amount)
                    else:
                        pnl = (entry_price - current_price) * abs(base_amount)
                        
                    active_list.append({
                        "symbol": symbol,
                        "direction": direction,
                        "entry_price": entry_price,
                        "size_usd": size_usd,
                        "tp_price": local_pos.get("tp_price", 0.0),
                        "sl_price": local_pos.get("sl_price", 0.0),
                        "leverage": local_pos.get("leverage", 10),
                        "pnl": pnl,
                        "_subaccount": sa.subaccount,
                        "_product_id": product_id
                    })
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to fetch active positions: {e}")
            
        return active_list

    async def open_position(self, symbol: str, direction: str, entry_price: float, notional_usd: float, tp_price: float, sl_price: float, leverage: int) -> bool:
        """Submits an EIP-712 signed order to Nado Gateway."""
        if not self.is_connected:
            logger.error(f"[NadoTradingService] Cannot open {direction} on {symbol} - SDK not connected.")
            return False
            
        logger.info(f"[NadoTradingService] 🚀 Routing {direction} {symbol} to Nado DEX...")
        
        try:
            import time
            import asyncio
            from nado_protocol.engine_client.types.execute import PlaceOrderParams, OrderParams
            
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
                
            market_info = self._market_cache.get(product_id)
            if not market_info:
                logger.error(f"[NadoTradingService] ❌ Market info not found for product_id {product_id}")
                return False
            amount_base = notional_usd / entry_price
            amount_x18 = int(amount_base * 10**18)
            price_x18 = int(entry_price * 10**18)
            
            # Align to size and price increments
            size_increment = int(market_info.book_info.size_increment)
            price_increment = int(market_info.book_info.price_increment_x18)
            
            amount_x18 = (amount_x18 // size_increment) * size_increment
            price_x18 = (price_x18 // price_increment) * price_increment
            
            if amount_x18 <= 0:
                logger.error(f"[NadoTradingService] ❌ Order amount is zero after step size alignment.")
                return False
            
            if direction.upper() == "SHORT":
                amount_x18 = -amount_x18
                
            # Expiration 1 hour from now
            expiration = int(time.time()) + 3600
            
            from nado_protocol.utils.subaccount import SubaccountParams
            sender = SubaccountParams(subaccount_name="default", subaccount_owner=self.wallet.get_address())
            
            # Order version must be 1 (appendix)
            order = OrderParams(
                sender=sender,
                amount=amount_x18,
                priceX18=price_x18,
                expiration=expiration,
                appendix=1
            )
            
            params = PlaceOrderParams(
                product_id=product_id,
                order=order
            )
            
            res = self.client.market.place_order(params)
            logger.info(f"[NadoTradingService] ✅ Order placed successfully: {res}")
            
            # Store mock position state to prevent duplicate orders
            self.active_positions[symbol] = {
                "direction": direction.upper(),
                "entry_price": entry_price,
                "size_usd": size_usd,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "leverage": leverage
            }
            return True
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to place order: {e}")
            return False

    async def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """Checks if TP/SL was hit."""
        closed_reports = []
        if symbol not in self.active_positions:
            return closed_reports
            
        pos = self.active_positions[symbol]
        direction = pos.get("direction")
        tp_price = pos.get("tp_price", 0.0)
        sl_price = pos.get("sl_price", 0.0)
        
        triggered = None
        
        if direction == "LONG":
            if tp_price and current_price >= tp_price:
                triggered = "TAKE_PROFIT"
            elif sl_price and current_price <= sl_price:
                triggered = "STOP_LOSS"
        elif direction == "SHORT":
            if tp_price and current_price <= tp_price:
                triggered = "TAKE_PROFIT"
            elif sl_price and current_price >= sl_price:
                triggered = "STOP_LOSS"
                
        if triggered:
            logger.info(f"[NadoTradingService] ⚠️ {triggered} triggered for {symbol} at {current_price}!")
            import asyncio
            # Trigger asynchronous close so we don't block the loop
            asyncio.create_task(self.force_close_position(symbol))
            
            closed_reports.append({
                "symbol": symbol,
                "reason": triggered,
                "exit_price": current_price
            })
            
        return closed_reports

    async def force_close_position(self, symbol: str) -> tuple:
        """Manually closes a position on Nado by firing a close_position market order."""
        if not self.is_connected:
            return False, 0.0
            
        try:
            positions = await self.get_active_positions()
            base_symbol = symbol.split('-')[0].upper()
            
            target_pos = None
            for p in positions:
                if p["symbol"] == base_symbol:
                    target_pos = p
                    break
                    
            if not target_pos:
                logger.error(f"[NadoTradingService] ⚠️ No active position found on Nado for {symbol} to close.")
                return False, 0.0
                
            from nado_protocol.engine_client.types.execute import PlaceOrderParams, OrderParams
            import time
            
            subaccount = target_pos["_subaccount"]
            product_id = target_pos["_product_id"]
            
            # Fire close_position via SDK
            res = await asyncio.to_thread(self.client.market.close_position, subaccount, product_id)
            logger.info(f"[NadoTradingService] ✅ Successfully forced closed {symbol}. TX: {res}")
            
            # Clean up local cache and update stats
            if base_symbol in self.active_positions:
                del self.active_positions[base_symbol]
                
            if target_pos["pnl"] > 0:
                self.win_count += 1
            else:
                self.loss_count += 1
                
            return True, {"pnl_usd": target_pos["pnl"]}
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to force close {symbol}: {e}")
            return False, {}

    async def sync_with_exchange(self) -> None:
        """Syncs local state with Nado state."""
        pass
