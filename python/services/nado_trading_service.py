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
        self.logger = logger
        
        self.client = None
        self.is_connected = False
        self._nado_time_offset = 0  # Store time offset transparently on the service instance
        self.active_positions = {}
        self.product_map: Dict[str, int] = {}
        self.default_subaccount_id = None
        
        # Stats tracking
        self.win_count = 0
        self.loss_count = 0
        self.recent_streak = []
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
                
            from nado_protocol.utils.bytes32 import subaccount_to_hex
            self.default_subaccount_id = subaccount_to_hex(self.wallet.get_address(), "default")
                
            logger.info(f"[NadoTradingService] ✅ Successfully connected. Products loaded: {len(self.product_map)}")
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
            
            total_trades = self.win_count + self.loss_count
            win_rate = round((self.win_count / total_trades) * 100, 1) if total_trades > 0 else 0.0
            
            return {
                "initial_balance": round(initial, 2),
                "current_balance": round(equity, 2),
                "total_usd": round(equity, 2),
                "total_pnl_usd": round(equity - initial, 2),
                "total_pnl_pct": round(((equity - initial) / initial) * 100, 2) if initial > 0 else 0.0,
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
            # Reverse map for product_id -> symbol
            id_to_symbol = {v: k for k, v in self.product_map.items()}
            
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
                        
                    entry_price = real_entry
                    
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
            
            self._pos_cache = active_list
            import time
            self._pos_cache_time = time.time()
            
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to fetch active positions: {e}")
            
        return active_list

    async def open_position(self, symbol: str, direction: str, entry_price: float, notional_usd: float, tp_price: float, sl_price: float, leverage: int, original_thesis: str = "") -> bool:
        """Submits an EIP-712 signed order to Nado Gateway."""
        if not self.is_connected:
            logger.error(f"[NadoTradingService] Cannot open {direction} on {symbol} - SDK not connected.")
            return False
            
        logger.info(f"[NadoTradingService] 🚀 Routing {direction} {symbol} to Nado DEX...")
        
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
                
            amount_base = notional_usd / entry_price
            amount_x18 = int(amount_base * 10**18)
            # Apply dynamic slippage for market-like execution (IOC)
            base_asset = symbol.split('-')[0].upper()
            if base_asset in ["BTC", "ETH"]:
                slippage_pct = 0.005 # 0.5%
            else:
                slippage_pct = 0.01  # 1.0%
                
            if direction.upper() == "LONG":
                limit_price = entry_price * (1 + slippage_pct)
            else:
                limit_price = entry_price * (1 - slippage_pct)
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
            
            # --- Native Trigger Orders (TP/SL) ---
            trigger_amount_x18 = str(-actual_filled_x18)
            
            if direction.upper() == "LONG":
                sl_type = "oracle_price_below"
                tp_type = "oracle_price_above"
            else:
                sl_type = "oracle_price_above"
                tp_type = "oracle_price_below"
                
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
                    logger.error(f"[NadoTradingService] ❌ Failed to place Native SL: {e}. ABORTING POSITION!")
                    await self.force_close_position(symbol, bypass_check=True)
                    return False
                    
            # Place Native Take Profit
            if tp_price > 0:
                exec_price = tp_price * 0.9 if direction.upper() == "LONG" else tp_price * 1.1
                
                exec_price_x18 = int(exec_price * 10**18)
                trigger_price_x18 = int(tp_price * 10**18)
                
                exec_price_x18 = (exec_price_x18 // price_increment) * price_increment
                trigger_price_x18 = (trigger_price_x18 // price_increment) * price_increment
                
                try:
                    await asyncio.to_thread(
                        self.client.market.place_price_trigger_order,
                        product_id=product_id,
                        price_x18=str(exec_price_x18),
                        amount_x18=trigger_amount_x18,
                        trigger_price_x18=str(trigger_price_x18),
                        trigger_type=tp_type,
                        reduce_only=True
                    )
                    logger.info(f"[NadoTradingService] 🎯 Native Take Profit placed at {tp_price}")
                except Exception as e:
                    logger.error(f"[NadoTradingService] ❌ Failed to place Native TP: {e}. ABORTING POSITION!")
                    if sl_digest:
                        try:
                            from nado_protocol.engine_client.types.execute import CancelOrdersParams
                            await asyncio.to_thread(self.client.market.cancel_trigger_orders, CancelOrdersParams(productIds=[product_id], digests=[sl_digest], sender=sender))
                        except Exception:
                            pass
                    await self.force_close_position(symbol, bypass_check=True)
                    return False
            
            # Recalculate notional_usd to reflect actual fill (Option A: allow partial fills)
            notional_usd = abs(actual_filled_x18 / 1e18) * entry_price
            
            # Store position state to prevent duplicate orders and track PnL
            self.active_positions[symbol] = {
                "direction": direction.upper(),
                "entry_price": entry_price,
                "size_usd": notional_usd,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "leverage": leverage,
                "highest_price": entry_price,
                "lowest_price": entry_price,
                "product_id": product_id,
                "sender": sender,
                "sl_digest": sl_digest,
                "sl_type": sl_type,
                "trigger_amount_x18": trigger_amount_x18,
                "original_thesis": original_thesis
            }
            
            if sl_digest:
                asyncio.create_task(self._trailing_stop_monitor(symbol))
                
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
        if "win_count" in state:
            self.win_count = int(state["win_count"])
        if "loss_count" in state:
            self.loss_count = int(state["loss_count"])
        if "recent_streak" in state:
            self.recent_streak = state["recent_streak"]

    def _save_state(self):
        from core.state_store import StateStore
        state_file = "data/memory/nado_state.json"
        StateStore.save(state_file, {
            "initial_balance": self._initial_balance,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "recent_streak": self.recent_streak
        })

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
                
                # Estimate exit price based on which trigger (TP or SL) is closer to the current market price
                # This prevents false PNL if the market bounced before polling
                tp_price = pos.get("tp_price", current_price)
                sl_price = pos.get("sl_price", current_price)
                
                triggered_by = "SL/TP/LIQ"
                if direction == "LONG":
                    if current_price >= tp_price:
                        exit_price = tp_price
                        triggered_by = "TP"
                    elif current_price <= sl_price:
                        exit_price = sl_price
                        triggered_by = "SL"
                    else:
                        exit_price = current_price
                        triggered_by = "Unknown/Manual"
                else: # SHORT
                    if current_price <= tp_price:
                        exit_price = tp_price
                        triggered_by = "TP"
                    elif current_price >= sl_price:
                        exit_price = sl_price
                        triggered_by = "SL"
                    else:
                        exit_price = current_price
                        triggered_by = "Unknown/Manual"
                
                if direction == "LONG":
                    target_pnl = (exit_price - entry_price) / entry_price * size_usd
                else:
                    target_pnl = (entry_price - exit_price) / entry_price * size_usd
                    
                del self.active_positions[symbol]
                
                if target_pnl > 0:
                    self.win_count += 1
                    self.recent_streak.append("WIN")
                else:
                    self.loss_count += 1
                    self.recent_streak.append("LOSS")
                self.recent_streak = self.recent_streak[-10:]
                self._save_state()
                    
                closed_reports.append({
                    "symbol": symbol,
                    "direction": direction,
                    "triggered_by": "CLOSED_ON_CHAIN",  # Avoiding false TP/SL attribution without indexer proof
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_usd": target_pnl,
                    "roi_pct": (target_pnl / pos.get("margin_used", size_usd) * 100) if size_usd > 0 else 0
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
                if not bypass_check:
                    positions = await self.get_active_positions(bypass_cache=True)
                    target_pos = next((p for p in positions if p["symbol"] == base_symbol), None)
                            
                    if not target_pos:
                        if attempt < max_retries - 1:
                            logger.warning(f"[NadoTradingService] Position {symbol} invisible to Indexer. Retrying ({attempt+1}/{max_retries})...")
                            await asyncio.sleep(1.5)
                            continue
                        else:
                            return False, 0.0
                            
                # Fire close_position via SDK
                try:
                    res = await asyncio.to_thread(self.client.market.close_position, subaccount, product_id)
                    logger.info(f"[NadoTradingService] 🧹 Successfully forced closed {symbol}. TX: {res}")
                    
                    # Clean up local cache and update stats
                    if symbol in self.active_positions:
                        del self.active_positions[symbol]
                        
                    pnl = target_pos["pnl"] if target_pos else 0.0
                    if pnl > 0:
                        self.win_count += 1
                        self.recent_streak.append("WIN")
                    elif pnl < 0:
                        self.loss_count += 1
                        self.recent_streak.append("LOSS")
                    self.recent_streak = self.recent_streak[-10:]
                    self._save_state()
                        
                    return True, pnl
                except Exception as close_e:
                    logger.warning(f"[NadoTradingService] ⚠️ Retryable close error for {symbol}: {close_e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.5)
                    else:
                        raise close_e
                        
            return False, 0.0
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to force close {symbol}: {e}")
            return False, 0.0

    def _get_market_parameters(self, product_id: int) -> dict:
        params = {
            "size_increment_base": 0.0,
            "size_increment_x18": 0,
            "price_increment_x18": 0
        }
        if not getattr(self, "_market_cache", None):
            return params
            
        market_info = self._market_cache.get(product_id)
        if market_info and hasattr(market_info, "book_info"):
            try:
                params["size_increment_base"] = float(market_info.book_info.size_increment) / 1e18
                params["size_increment_x18"] = int(market_info.book_info.size_increment)
                params["price_increment_x18"] = int(market_info.book_info.price_increment_x18)
            except Exception:
                pass
        return params

    async def get_market_limits(self, symbol: str) -> dict:
        """Fetch min_size and size_increment for the given product"""
        limits = {"size_increment": 0.0}
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
        except Exception as e:
            logger.warning(f"[NadoTradingService] ⚠️ Could not fetch limits for {symbol}: {e}")
        return limits

    async def sync_with_exchange(self) -> None:
        """Syncs local state with active positions on Nado."""
        if not self.is_connected:
            return
            
        try:
            positions = await self.get_active_positions(bypass_cache=True)
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

    async def _trailing_stop_monitor(self, symbol: str):
        """Background task to manage trailing stop dynamically using Nado SDK."""
        from nado_protocol.engine_client.types.execute import CancelOrdersParams
        
        # Wait a bit for everything to settle
        await asyncio.sleep(5)
        
        while self.is_connected and symbol in self.active_positions:
            try:
                pos = self.active_positions.get(symbol)
                if not pos:
                    break
                    
                product_id = pos.get("product_id")
                direction = pos.get("direction")
                entry = pos.get("entry_price")
                tp = pos.get("tp_price")
                sl = pos.get("sl_price")
                sl_digest = pos.get("sl_digest")
                
                if not sl_digest or product_id is None:
                    break # Restored or missing trigger order info
                
                # Get latest price
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
                
                if current_price <= 0:
                    await asyncio.sleep(10)
                    continue
                    
                # Update Extremes
                if current_price > pos.get("highest_price", entry):
                    pos["highest_price"] = current_price
                if current_price < pos.get("lowest_price", entry):
                    pos["lowest_price"] = current_price

                highest = pos["highest_price"]
                lowest = pos["lowest_price"]
                
                trail_pct = 0.015
                activation_pct = 0.015
                new_sl = None
                
                if direction == "LONG":
                    halfway_to_tp = entry + ((tp - entry) * 0.5) if tp > 0 else float('inf')
                    if current_price >= halfway_to_tp and pos["sl_price"] < entry:
                        new_sl = entry
                        logger.info(f"[NadoTradingService] 🛡️ [Breakeven Guard] {symbol} Price passed 50% TP. SL moved to breakeven: {entry:.4f}")
                    elif (highest - entry) / entry >= activation_pct:
                        candidate_sl = highest * (1 - trail_pct)
                        if candidate_sl > pos["sl_price"]:
                            new_sl = candidate_sl
                            logger.info(f"[NadoTradingService] 📈 [Trailing Stop] {symbol} SL trailed up to: {new_sl:.4f}")
                else:
                    halfway_to_tp = entry - ((entry - tp) * 0.5) if tp > 0 else 0
                    if current_price <= halfway_to_tp and pos["sl_price"] > entry:
                        new_sl = entry
                        logger.info(f"[NadoTradingService] 🛡️ [Breakeven Guard] {symbol} Price passed 50% TP. SL moved to breakeven: {entry:.4f}")
                    elif (entry - lowest) / entry >= activation_pct:
                        candidate_sl = lowest * (1 + trail_pct)
                        if candidate_sl < pos["sl_price"] or pos["sl_price"] == 0:
                            new_sl = candidate_sl
                            logger.info(f"[NadoTradingService] 📉 [Trailing Stop] {symbol} SL trailed down to: {new_sl:.4f}")
                            
                # If we have a new SL, we must replace the trigger order on Nado
                if new_sl and new_sl != pos["sl_price"]:
                    # 1. Cancel old SL
                    cancel_params = CancelOrdersParams(
                        productIds=[product_id],
                        digests=[sl_digest],
                        sender=pos["sender"]
                    )
                    await asyncio.to_thread(self.client.market.cancel_trigger_orders, cancel_params)
                    
                    # 2. Place new SL
                    # Fetch price increment from cached market data (safe dict lookup)
                    params_dict = self._get_market_parameters(product_id)
                    price_increment = params_dict["price_increment_x18"]
                    if price_increment == 0:
                        # Refresh cache if empty
                        try:
                            markets_data = await asyncio.to_thread(self.client.market.get_all_engine_markets)
                            if not getattr(self, "_market_cache", None):
                                self._market_cache = {}
                            for m in markets_data.perp_products:
                                self._market_cache[m.product_id] = m
                            params_dict = self._get_market_parameters(product_id)
                            price_increment = params_dict["price_increment_x18"]
                        except Exception as cache_e:
                            logger.error(f"[NadoTradingService] ❌ Failed to refresh market cache for trailing SL: {cache_e}")
                            await asyncio.sleep(10)
                            continue

                    if price_increment == 0:
                        logger.error(f"[NadoTradingService] ❌ Cannot trail SL for {symbol}: price_increment is 0")
                        await asyncio.sleep(10)
                        continue

                    exec_price = new_sl * 0.9 if direction == "LONG" else new_sl * 1.1
                    
                    exec_price_x18 = int(exec_price * 10**18)
                    trigger_price_x18 = int(new_sl * 10**18)
                    
                    exec_price_x18 = (exec_price_x18 // price_increment) * price_increment
                    trigger_price_x18 = (trigger_price_x18 // price_increment) * price_increment
                    
                    sl_res = await asyncio.to_thread(
                        self.client.market.place_price_trigger_order,
                        product_id=product_id,
                        price_x18=str(exec_price_x18),
                        amount_x18=pos["trigger_amount_x18"],
                        trigger_price_x18=str(trigger_price_x18),
                        trigger_type=pos["sl_type"],
                        reduce_only=True
                    )
                    if sl_res.data:
                        pos["sl_digest"] = sl_res.data.digest
                        pos["sl_price"] = new_sl
                        logger.info(f"[NadoTradingService] ✅ Native Trailing SL replaced successfully for {symbol} at {new_sl:.4f}")
                    else:
                        logger.error(f"[NadoTradingService] ❌ Failed to place trailing SL order for {symbol}")
                        
            except Exception as e:
                logger.error(f"[NadoTradingService] ⚠️ Trailing stop error for {symbol}: {e}")
                
            await asyncio.sleep(10)
