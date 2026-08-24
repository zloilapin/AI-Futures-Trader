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
    
    def __init__(self, nado_client=None):
        self.wallet = Web3Wallet()
        self.client = None
        self.is_connected = False
        self.active_positions = {}
        self.product_map: Dict[str, int] = {}
        self.default_subaccount_id = None
        
        # Stats tracking
        self.win_count = 0
        self.loss_count = 0
        self._initial_balance = None
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
                self.client = create_nado_client(
                    mode=NadoClientMode.MAINNET,
                    signer=self.wallet.get_private_key()
                )
            self.is_connected = True
            
            # Fetch product map dynamically
            import asyncio
            markets_data = await asyncio.to_thread(self.client.market.get_all_engine_markets)
            for m in markets_data.perp_products:
                base_symbol = m.symbol.split('-')[0].upper()
                self.product_map[base_symbol] = m.product_id
                
            from nado_protocol.utils.subaccount import subaccount_to_hex
            self.default_subaccount_id = subaccount_to_hex(self.wallet.get_address(), "default")
                
            logger.info(f"[NadoTradingService] ✅ Successfully connected. Products loaded: {len(self.product_map)}")
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to init Nado SDK: {e}")
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
            
            if self._initial_balance is None and balance > 0:
                self._initial_balance = balance
                self._save_state()
                
            initial = self._initial_balance or balance
            
            pnl = 0.0
            active_count = len(await self.get_active_positions())
            
            total_trades = self.win_count + self.loss_count
            win_rate = round((self.win_count / total_trades) * 100, 1) if total_trades > 0 else 0.0
            
            return {
                "initial_balance": round(initial, 2),
                "current_balance": round(balance, 2),
                "total_pnl_usd": round(balance - initial, 2),
                "total_pnl_pct": round(((balance - initial) / initial) * 100, 2) if initial > 0 else 0.0,
                "unrealized_pnl_usd": pnl,
                "unrealized_pnl": pnl,
                "roi_pct": round(((balance - initial) / initial) * 100, 2) if initial > 0 else 0.0,
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
                summary = await asyncio.to_thread(self.client.subaccount.get_engine_subaccount_summary, sa.subaccount)
                if not hasattr(summary, "perp_balances") or not summary.perp_balances:
                    continue
                    
                for pos in summary.perp_balances:
                    base_amount = float(pos.balance.amount) / 1e18
                    if abs(base_amount) < 1e-6:
                        continue # Ignore zero or dust positions
                        
                    product_id = pos.product_id
                    symbol = id_to_symbol.get(product_id, f"UNKNOWN-{product_id}")
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
                    local_pos = self.active_positions.get(symbol, {})
                    try:
                        v_quote = float(pos.balance.v_quote_balance) / 1e18
                        real_entry = abs(v_quote) / abs(base_amount) if abs(base_amount) > 0 else current_price
                    except Exception:
                        real_entry = current_price
                        
                    entry_price = local_pos.get("entry_price", real_entry)
                    
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
                
            params_dict = self._get_market_parameters(product_id)
            if params_dict["size_increment_x18"] == 0:
                logger.error(f"[NadoTradingService] ❌ Market params not found for product_id {product_id}")
                return False
                
            amount_base = notional_usd / entry_price
            amount_x18 = int(amount_base * 10**18)
            # Apply slippage for market-like execution (IOC)
            if direction.upper() == "LONG":
                limit_price = entry_price * 1.05  # Pay up to 5% more
            else:
                limit_price = entry_price * 0.95  # Sell for up to 5% less
                amount_x18 = -amount_x18
                
            price_x18 = int(limit_price * 10**18)
            
            # Align to size and price increments
            size_increment = params_dict["size_increment_x18"]
            price_increment = params_dict["price_increment_x18"]
            
            amount_x18 = (amount_x18 // size_increment) * size_increment
            price_x18 = (price_x18 // price_increment) * price_increment
            
            if amount_x18 == 0:
                logger.error(f"[NadoTradingService] ❌ Order amount is zero after step size alignment.")
                return False
                
            # Expiration for IOC
            try:
                from nado_protocol.utils.expiration import OrderType, get_expiration_timestamp
                expiration = get_expiration_timestamp(OrderType.IOC, int(time.time()) + 60)
            except ImportError:
                expiration = int(time.time()) + 60
            
            from nado_protocol.utils.subaccount import subaccount_to_hex
            from nado_protocol.utils.math import gen_order_nonce
            sender = subaccount_to_hex(self.wallet.get_address(), "default")
            
            # Order version must be 1 (appendix)
            order = OrderParams(
                sender=sender,
                amount=amount_x18,
                priceX18=price_x18,
                expiration=expiration,
                nonce=gen_order_nonce(),
                appendix=1
            )
            
            params = PlaceOrderParams(
                product_id=product_id,
                order=order
            )
            
            res = self.client.market.place_order(params)
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
                            logger.info(f"[NadoTradingService] ✅ Order {digest} FILLED on final check!")
                except Exception as e:
                    logger.warning(f"[NadoTradingService] ⚠️ Final order status check failed for {digest}: {e}")
                    
                if not filled:
                    logger.error(f"[NadoTradingService] ❌ Order {digest} was accepted but NOT filled within 10s. Cancelling to prevent race condition...")
                    try:
                        from nado_protocol.engine_client.types.execute import CancelOrdersParams, CancelOrderParams
                        cancel_params = CancelOrdersParams(
                            txs=[
                                CancelOrderParams(
                                    product_id=product_id,
                                    digest=digest,
                                    sender=sender
                                )
                            ]
                        )
                        await asyncio.to_thread(self.client.market.cancel_orders, cancel_params)
                        logger.info(f"[NadoTradingService] 🗑️ Order {digest} cancelled successfully.")
                    except Exception as cancel_e:
                        logger.error(f"[NadoTradingService] ❌ Failed to cancel timed out order {digest}: {cancel_e}")
                    
                    return False
            
            # --- Native Trigger Orders (TP/SL) ---
            trigger_amount_x18 = str(-actual_filled_x18)
            
            if direction.upper() == "LONG":
                sl_type = "oracle_price_below"
                tp_type = "oracle_price_above"
            else:
                sl_type = "oracle_price_above"
                tp_type = "oracle_price_below"
                
            sl_digest = None
            # Place Native Stop Loss
            if sl_price > 0:
                exec_price = sl_price * 0.9 if direction.upper() == "LONG" else sl_price * 1.1
                exec_price = (exec_price // price_increment) * price_increment
                trigger_price = (sl_price // price_increment) * price_increment
                try:
                    sl_res = await asyncio.to_thread(
                        self.client.market.place_price_trigger_order,
                        product_id=product_id,
                        price_x18=str(int(exec_price * 10**18)),
                        amount_x18=trigger_amount_x18,
                        trigger_price_x18=str(int(trigger_price * 10**18)),
                        trigger_type=sl_type,
                        reduce_only=True
                    )
                    sl_digest = sl_res.data.digest if sl_res and sl_res.data else None
                    logger.info(f"[NadoTradingService] 🛡️ Native Stop Loss placed at {sl_price}")
                except Exception as e:
                    logger.error(f"[NadoTradingService] ❌ Failed to place Native SL: {e}. ABORTING POSITION!")
                    await self.force_close_position(symbol, bypass_check=True)
                    return False

            # Place Native Take Profit
            if tp_price > 0:
                exec_price = tp_price * 0.9 if direction.upper() == "LONG" else tp_price * 1.1
                exec_price = (exec_price // price_increment) * price_increment
                trigger_price = (tp_price // price_increment) * price_increment
                try:
                    await asyncio.to_thread(
                        self.client.market.place_price_trigger_order,
                        product_id=product_id,
                        price_x18=str(int(exec_price * 10**18)),
                        amount_x18=trigger_amount_x18,
                        trigger_price_x18=str(int(trigger_price * 10**18)),
                        trigger_type=tp_type,
                        reduce_only=True
                    )
                    logger.info(f"[NadoTradingService] 🎯 Native Take Profit placed at {tp_price}")
                except Exception as e:
                    logger.error(f"[NadoTradingService] ❌ Failed to place Native TP: {e}. ABORTING POSITION!")
                    if sl_digest:
                        try:
                            from nado_protocol.engine_client.types.execute import CancelOrdersParams, CancelOrderParams
                            await asyncio.to_thread(self.client.market.cancel_orders, CancelOrdersParams(txs=[CancelOrderParams(product_id=product_id, digest=sl_digest, sender=sender)]))
                        except Exception:
                            pass
                    await self.force_close_position(symbol, bypass_check=True)
                    return False
            
            # Store position state to prevent duplicate orders and track PnL
            self.active_positions[symbol] = {
                "direction": direction.upper(),
                "entry_price": entry_price,
                "size_usd": notional_usd,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "leverage": leverage
            }
            return True
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to place order: {e}")
            return False

    def _load_state(self):
        import os, json
        state_file = "data/memory/nado_state.json"
        if os.path.exists(state_file):
            try:
                with open(state_file, "r") as f:
                    state = json.load(f)
                    if "initial_balance" in state:
                        self._initial_balance = float(state["initial_balance"])
                        logger.info(f"[NadoTradingService] 💾 Loaded initial balance: {self._initial_balance}")
            except Exception as e:
                logger.error(f"[NadoTradingService] ⚠️ Failed to load state: {e}")

    def _save_state(self):
        import os, json
        state_file = "data/memory/nado_state.json"
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        try:
            with open(state_file, "w") as f:
                json.dump({"initial_balance": self._initial_balance}, f)
        except Exception as e:
            logger.error(f"[NadoTradingService] ⚠️ Failed to save state: {e}")

    async def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """Checks if a position was closed natively by Nado (TP/SL trigger)."""
        closed_reports = []
        if symbol not in self.active_positions:
            return closed_reports
            
        try:
            # Poll actual blockchain state instead of doing virtual price math
            positions = await self.get_active_positions()
            
            # If the position is no longer in the active list, it was closed natively!
            base_symbol = symbol.split('-')[0].upper()
            still_open = False
            target_pnl = 0.0
            
            for p in positions:
                if p["symbol"] == base_symbol:
                    still_open = True
                    break
                    
            if not still_open:
                # Position is gone, meaning Nado native Trigger Order (SL/TP) executed!
                logger.info(f"[NadoTradingService] ⚡ Native Trigger executed for {symbol}! Position closed on-chain.")
                
                pos = self.active_positions[symbol]
                direction = pos["direction"]
                entry_price = pos["entry_price"]
                size_usd = pos["size_usd"]
                
                if direction == "LONG":
                    target_pnl = (current_price - entry_price) / entry_price * size_usd
                else:
                    target_pnl = (entry_price - current_price) / entry_price * size_usd
                    
                del self.active_positions[symbol]
                
                if target_pnl > 0:
                    self.win_count += 1
                else:
                    self.loss_count += 1
                    
                closed_reports.append({
                    "symbol": symbol,
                    "direction": direction,
                    "triggered_by": "CLOSED_ON_CHAIN",  # Avoiding false TP/SL attribution without indexer proof
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "pnl_usd": target_pnl,
                    "roi_pct": (target_pnl / pos.get("margin_used", size_usd) * 100) if size_usd > 0 else 0
                })
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to check native position state: {e}")
            
        return closed_reports

    async def force_close_position(self, symbol: str, bypass_check: bool = False) -> tuple:
        """Manually closes a position on Nado by firing a close_position market order."""
        if not self.is_connected:
            return False, 0.0
            
        try:
            base_symbol = symbol.split('-')[0].upper()
            target_pos = None
            
            if not bypass_check:
                positions = await self.get_active_positions()
                
                for p in positions:
                    if p["symbol"] == base_symbol:
                        target_pos = p
                        break
                        
                if not target_pos:
                    return False, 0.0
                
            # Fetch real info
            product_id = self.product_map.get(base_symbol)
            if not product_id:
                return False, 0.0
                
            address = self.wallet.get_address()
            res = await asyncio.to_thread(self.client.subaccount.get_subaccounts, address)
            if not res or not res.subaccounts:
                return False, 0.0
            subaccount = res.subaccounts[0].subaccount
            
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
                
            return True, target_pos["pnl"]
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to force close {symbol}: {e}")
            return False, 0.0

    def _get_market_parameters(self, product_id: int) -> dict:
        params = {
            "min_size_usd": 0.0,
            "size_increment_base": 0.0,
            "size_increment_x18": 0,
            "price_increment_x18": 0
        }
        if not getattr(self, "_market_cache", None):
            return params
            
        market_info = self._market_cache.get(product_id)
        if market_info and hasattr(market_info, "book_info"):
            try:
                params["min_size_usd"] = float(market_info.book_info.min_size) / 1e18
                params["size_increment_base"] = float(market_info.book_info.size_increment) / 1e18
                params["size_increment_x18"] = int(market_info.book_info.size_increment)
                params["price_increment_x18"] = int(market_info.book_info.price_increment_x18)
            except Exception:
                pass
        return params

    async def get_market_limits(self, symbol: str) -> dict:
        """Fetch min_size and size_increment for the given product"""
        limits = {"min_size_usd": 0.0, "size_increment": 0.0}
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
            if params["min_size_usd"] > 0:
                limits["min_size_usd"] = params["min_size_usd"]
                limits["size_increment"] = params["size_increment_base"]
        except Exception as e:
            logger.warning(f"[NadoTradingService] ⚠️ Could not fetch limits for {symbol}: {e}")
        return limits

    async def sync_with_exchange(self) -> None:
        """Syncs local state with active positions on Nado."""
        if not self.is_connected:
            return
            
        try:
            positions = await self.get_active_positions()
            restored = 0
            for pos in positions:
                symbol = pos["symbol"]
                if symbol not in self.active_positions:
                    logger.info(f"[NadoTradingService] ♻️ Restored active position on {symbol} after restart.")
                    
                    entry = pos["entry_price"]
                    direction = pos["direction"]
                    base_symbol = symbol.split('-')[0].upper()
                    product_id = self.product_map.get(base_symbol)
                    
                    tp_price = 0.0
                    sl_price = 0.0
                    
                    try:
                        # In Nado/Vertex, trigger orders are fetched via indexer
                        res = await asyncio.to_thread(self.client.indexer.get_trigger_orders, {"subaccount": self.default_subaccount_id, "product_id": product_id, "pending": True})
                        if res and hasattr(res, 'orders'):
                            for o in res.orders:
                                # Safely extract trigger price
                                t_price = 0.0
                                if hasattr(o, 'order') and hasattr(o.order, 'trigger_price_x18'):
                                    t_price = float(o.order.trigger_price_x18) / 1e18
                                elif hasattr(o, 'trigger_price_x18'):
                                    t_price = float(o.trigger_price_x18) / 1e18
                                    
                                if t_price > 0:
                                    if direction == "LONG":
                                        if t_price > entry:
                                            tp_price = t_price
                                        else:
                                            sl_price = t_price
                                    else:
                                        if t_price < entry:
                                            tp_price = t_price
                                        else:
                                            sl_price = t_price
                        
                        if tp_price == 0.0 or sl_price == 0.0:
                            logger.error(f"[NadoTradingService] ❌ Missing real TP/SL for restored position {symbol}. Emergency close triggered!")
                            await self.force_close_position(symbol)
                            continue
                            
                    except Exception as e:
                        logger.error(f"[NadoTradingService] ❌ Failed to fetch real trigger orders for {symbol} ({e}). Emergency close triggered!")
                        await self.force_close_position(symbol)
                        continue
                    
                    self.active_positions[symbol] = {
                        "direction": direction,
                        "entry_price": entry,
                        "size_usd": pos.get("size_usd", 0.0),
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "leverage": pos.get("leverage", 1)
                    }
                    restored += 1
            if restored > 0:
                logger.info(f"[NadoTradingService] ♻️ Successfully synced {restored} positions from Nado.")
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to sync with Nado: {e}")
