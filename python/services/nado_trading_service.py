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
        self.active_positions = self._load_positions()
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
                        "_product_id": product_id
                    })
            
            self._pos_cache = active_list
            import time
            self._pos_cache_time = time.time()
            
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to fetch active positions: {e}")
            
        return active_list

    async def open_position(self, symbol: str, direction: str, entry_price: float, notional_usd: float, tp_price: float, sl_price: float, leverage: int, original_thesis: str = "", contracts: float = 0.0) -> bool:
        """Submits an EIP-712 signed order to Nado Gateway."""
        if not self.is_connected:
            logger.error(f"[NadoTradingService] Cannot open {direction} on {symbol} - SDK not connected.")
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
            # entry_price (from RiskManager) already includes the expected slippage (execution_entry).
            # We use it directly as the limit price to avoid double-slippage penalty.
            limit_price = entry_price
            
            if direction.upper() == "SHORT":
                amount_x18 = -amount_x18
                
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
            
            # If slippage caused the risk to increase by more than 10% (with a strict $0.01 floor to ignore dust)
            if sl_price > 0 and expected_risk_usd > 0 and actual_risk_usd > expected_risk_usd + max(expected_risk_usd * 0.10, 0.01):
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
                "atr_reference": 0.0,
                "product_id": product_id,
                "sender": sender,
                "sl_digest": sl_digest,
                "tp_digest": tp_digest,
                "sl_type": sl_type,
                "tp_type": tp_type if tp_price > 0 else None,
                "trigger_amount_x18": trigger_amount_x18,
                "original_thesis": original_thesis,
                "open_time": time.time()
            }
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
                
                # --- Exact Execution Price (CRITICAL-6) ---
                try:
                    from nado_protocol.indexer_client.types.query import IndexerSubaccountHistoricalOrdersParams
                    
                    address = self.wallet.get_address()
                    res_sub = await asyncio.to_thread(self.client.subaccount.get_subaccounts, address)
                    subaccount_id = res_sub.subaccounts[0].subaccount if res_sub and res_sub.subaccounts else None
                    
                    if subaccount_id and "product_id" in pos:
                        params = IndexerSubaccountHistoricalOrdersParams(
                            subaccounts=[subaccount_id],
                            product_ids=[pos["product_id"]],
                            limit=20,
                        )
                        history = await asyncio.to_thread(self.client.market.get_subaccount_historical_orders, params)
                        
                        if history and history.orders:
                            expected_base = abs(float(pos.get("trigger_amount_x18", 0)) / 1e18) if "trigger_amount_x18" in pos else 0
                            open_time = float(pos.get("open_time", 0))

                            # 1. Match closing order (opposite sign of position direction)
                            for order in history.orders:
                                quote = abs(float(order.quote_filled))
                                base = abs(float(order.base_filled))
                                bf = float(order.base_filled)
                                
                                # Strict Time Check
                                if open_time > 0 and hasattr(order, "timestamp"):
                                    order_ts_sec = float(order.timestamp) / 1000.0 if len(str(order.timestamp)) > 10 else float(order.timestamp)
                                    if order_ts_sec < open_time:
                                        continue
                                        
                                if (direction == "LONG" and bf < 0) or (direction == "SHORT" and bf > 0):
                                    if base > 0:
                                        # Strict Size Match Check (allow up to 5% tolerance for exchange rounding)
                                        if expected_base > 0 and abs(base - expected_base) / max(expected_base, 1e-6) > 0.05:
                                            continue
                                            
                                        exec_price = quote / base
                                        logger.info(f"[NadoTradingService] 🔍 Found exact exit execution price from history: {exec_price}")
                                        exit_price = exec_price
                                        triggered_by += " (EXACT)"
                                        break
                            
                            # 2. Match and verify entry order from history to ensure 100% accurate PnL
                            for order in history.orders:
                                quote = abs(float(order.quote_filled))
                                base = abs(float(order.base_filled))
                                bf = float(order.base_filled)
                                
                                # Strict Size Match Check
                                if expected_base > 0 and abs(base - expected_base) / max(expected_base, 1e-6) > 0.05:
                                    continue
                                    
                                if (direction == "LONG" and bf > 0) or (direction == "SHORT" and bf < 0):
                                    if base > 0:
                                        hist_entry = quote / base
                                        if abs(hist_entry - entry_price) / max(entry_price, 1e-6) > 0.0005:
                                            logger.info(f"[NadoTradingService] 🎯 Corrected entry price from history: {entry_price:.4f} -> {hist_entry:.4f}")
                                            entry_price = hist_entry
                                        break
                except Exception as e:
                    logger.warning(f"[NadoTradingService] ⚠️ Could not fetch exact history for {symbol}, falling back to estimation: {e}")
                
                if direction == "LONG":
                    gross_pnl = (exit_price - entry_price) / entry_price * size_usd
                else:
                    gross_pnl = (entry_price - exit_price) / entry_price * size_usd
                    
                # Exact round-trip fee approximation (0.05% * 2)
                estimated_fees = size_usd * 0.001
                target_pnl = gross_pnl - estimated_fees
                logger.info(f"[NadoTradingService] 💰 Gross PnL: ${gross_pnl:.2f}, Est Fees: ${estimated_fees:.2f} -> Net PnL: ${target_pnl:.2f}")
                if symbol in self.active_positions:
                    del self.active_positions[symbol]
                alias_key = base_symbol if '-' in symbol else f"{base_symbol}-USD"
                if alias_key in self.active_positions:
                    del self.active_positions[alias_key]
                self._save_positions()
                
                if target_pnl > 0.001:
                    self.win_count += 1
                    self.recent_streak.append("WIN")
                elif target_pnl < -0.001:
                    self.loss_count += 1
                    self.recent_streak.append("LOSS")
                else:
                    logger.info(f"[NadoTradingService] ℹ️ Position closed at Breakeven (PnL: ${target_pnl:.2f}). Streak not modified.")
                self.recent_streak = self.recent_streak[-10:]
                self._save_state()
                    
                closed_reports.append({
                    "symbol": symbol,
                    "direction": direction,
                    "triggered_by": "CLOSED_ON_CHAIN",  # Avoiding false TP/SL attribution without indexer proof
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
                    
                    # Clean up local cache and update stats
                    if symbol in self.active_positions:
                        del self.active_positions[symbol]
                    alias_key = base_symbol if '-' in symbol else f"{base_symbol}-USD"
                    if alias_key in self.active_positions:
                        del self.active_positions[alias_key]
                    self._save_positions()
                        
                    pnl = target_pos["pnl"] if target_pos else 0.0
                    if pnl > 0.001:
                        self.win_count += 1
                        self.recent_streak.append("WIN")
                    elif pnl < -0.001:
                        self.loss_count += 1
                        self.recent_streak.append("LOSS")
                    else:
                        logger.info(f"[NadoTradingService] ℹ️ Force close at breakeven/near-zero PnL (${pnl:.4f}). Streak unchanged.")
                    self.recent_streak = self.recent_streak[-10:]
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
                params["min_size"] = float(getattr(market_info.book_info, "min_size", 0)) / 1e18
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

    async def sync_with_exchange(self) -> None:
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
            
            # Clean up local active positions that are no longer open on-chain
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
                        if tracked.get("sl_price", 0.0) == 0.0 or tracked.get("tp_price", 0.0) == 0.0:
                            saved_positions = self._load_positions()
                            saved_p = saved_positions.get(canonical_symbol) or saved_positions.get(base_symbol) or {}
                            if tracked.get("sl_price", 0.0) == 0.0 and saved_p.get("sl_price", 0.0) > 0:
                                tracked["sl_price"] = float(saved_p["sl_price"])
                                logger.info(f"[NadoTradingService] 💾 Restored SL {tracked['sl_price']:.4f} from disk for tracked {canonical_symbol}.")
                            if tracked.get("tp_price", 0.0) == 0.0 and saved_p.get("tp_price", 0.0) > 0:
                                tracked["tp_price"] = float(saved_p["tp_price"])
                                logger.info(f"[NadoTradingService] 💾 Restored TP {tracked['tp_price']:.4f} from disk for tracked {canonical_symbol}.")
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
                            
                        if tp_price == 0.0 or sl_price == 0.0:
                            logger.warning(f"[NadoTradingService] ⚠️ Triggers for {canonical_symbol} (TP: {tp_price}, SL: {sl_price}).")
                    else:
                        logger.info(f"[NadoTradingService] 🎯 Restored verified TP ({tp_price}) and SL ({sl_price}) for {canonical_symbol}.")
                        
                except Exception as e:
                    logger.warning(f"[NadoTradingService] ⚠️ Failed to fetch real trigger orders for {canonical_symbol} ({e}). Marking UNVERIFIED.")

                saved_positions = self._load_positions()
                saved_p = saved_positions.get(canonical_symbol) or saved_positions.get(base_symbol) or {}
                highest_price = saved_p.get("highest_price", entry)
                lowest_price = saved_p.get("lowest_price", entry)
                protection_state = saved_p.get("protection_state", "PROTECTED")

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
                    "atr_reference": 0.0,
                    "product_id": product_id,
                    "sender": self.default_subaccount_id,
                    "sl_digest": sl_digest,
                    "sl_type": sl_type,
                    "trigger_amount_x18": trigger_amount_x18 or str(int((pos.get("size_usd", 0.0) / entry) * 10**18)),
                    "original_thesis": saved_p.get("original_thesis", f"Restored on-chain position for {canonical_symbol}"),
                    "unverified_triggers": (tp_price == 0.0 or sl_price == 0.0)
                }
                restored += 1

            self._save_positions()
            if restored > 0:
                logger.info(f"[NadoTradingService] ♻️ Successfully synced {restored} positions from Nado.")
        except Exception as e:
            logger.error(f"[NadoTradingService] ❌ Failed to sync with Nado: {e}")

    async def update_stop_loss(self, symbol: str, new_sl_price: float) -> bool:
        """
        Atomically updates the stop loss for a position.
        Ensures thread-safety per symbol and verifies the trigger order exists.
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
                
            current_sl = pos.get("sl_price", 0)
            if current_sl == new_sl_price:
                return True
                
            product_id = pos.get("product_id")
            sl_digest = pos.get("sl_digest")
            sender = pos.get("sender")
            direction = pos.get("direction")
            
            if product_id is None:
                base_sym = symbol.split('-')[0].upper()
                product_id = self.product_map.get(base_sym) or self.product_map.get(f"{base_sym}-USD") or self.product_map.get(symbol)
                if product_id is not None:
                    pos["product_id"] = product_id

            if not sl_digest:
                pos["sl_price"] = new_sl_price
                self._save_positions()
                logger.info(f"[NadoTradingService] 🛡️ Software Stop Loss updated for {symbol} -> {new_sl_price:.4f}")
                return True

            if product_id is None:
                logger.error(f"[NadoTradingService] Cannot update on-chain SL for {symbol} - missing product_id.")
                return False

            from nado_protocol.engine_client.types.execute import CancelOrdersParams
            
            # 1. Fetch market params for correct increments
            params_dict = self._get_market_parameters(product_id)
            price_increment = params_dict["price_increment_x18"]
            if price_increment == 0:
                logger.error(f"[NadoTradingService] Cannot update SL for {symbol} - price_increment is 0")
                return False
                
            # 2. Cancel old SL
            cancel_params = CancelOrdersParams(
                productIds=[product_id],
                digests=[sl_digest],
                sender=sender
            )
            try:
                await asyncio.to_thread(self.client.market.cancel_trigger_orders, cancel_params)
            except Exception as e:
                logger.warning(f"[NadoTradingService] Failed to cancel old SL for {symbol}: {e}. Proceeding to place new SL.")
                
            # 3. Place new SL immediately
            exec_price = new_sl_price * 0.9 if direction == "LONG" else new_sl_price * 1.1
            
            exec_price_x18 = int(exec_price * 10**18)
            trigger_price_x18 = int(new_sl_price * 10**18)
            
            exec_price_x18 = (exec_price_x18 // price_increment) * price_increment
            trigger_price_x18 = (trigger_price_x18 // price_increment) * price_increment
            
            try:
                sl_res = await asyncio.to_thread(
                    self.client.market.place_price_trigger_order,
                    product_id=product_id,
                    price_x18=str(exec_price_x18),
                    amount_x18=pos["trigger_amount_x18"],
                    trigger_price_x18=str(trigger_price_x18),
                    trigger_type=pos["sl_type"],
                    reduce_only=True
                )
                
                if sl_res and sl_res.data and sl_res.data.digest:
                    new_digest = sl_res.data.digest
                    pos["sl_digest"] = new_digest
                    pos["sl_price"] = new_sl_price
                    self._save_positions()
                    logger.info(f"[NadoTradingService] ✅ Stop Loss updated for {symbol} -> {new_sl_price:.4f}")
                    return True
                else:
                    raise ValueError("Missing digest in Nado response")
                    
            except Exception as e:
                logger.error(
                    f"[NadoTradingService] 🚨 FATAL: Failed to place NEW on-chain SL for {symbol} after cancelling OLD SL ({e}). "
                    f"Position is UNPROTECTED on-chain! Executing Emergency Force Close to lock in safety."
                )
                pos["sl_digest"] = None
                pos["is_closing"] = True
                asyncio.create_task(self.force_close_position(symbol, bypass_check=True))
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
                            hit_tp = (direction == "LONG" and tp_price > 0 and current_price >= tp_price) or \
                                     (direction == "SHORT" and tp_price > 0 and current_price <= tp_price)
                                     
                            if (hit_sl or hit_tp) and not pos.get("is_closing"):
                                pos["is_closing"] = True
                                trigger_name = "STOP LOSS" if hit_sl else "TAKE PROFIT"
                                target_val = sl_price if hit_sl else tp_price
                                logger.info(
                                    f"[NadoTradingService] 🚨 Fast Monitor: Software {trigger_name} triggered for {symbol}! "
                                    f"Current: {current_price:.4f}, Target: {target_val:.4f}. Executing market close..."
                                )
                                
                                async def _close_and_notify_fast(sym=symbol, t_name=trigger_name, p_dict=dict(pos), cur_p=current_price):
                                    success, pnl = await self.force_close_position(sym, bypass_check=True)
                                    if success:
                                        pnl_emoji = "🎉" if pnl >= 0 else "🔻"
                                        msg = (
                                            f"{pnl_emoji} *TRADE CLOSED / СДЕЛКА ЗАКРЫТА (FAST_MONITOR_{t_name})*\n\n"
                                            f"🪙 *Asset / Монета:* `{sym}`\n"
                                            f"📊 *Direction / Направление:* `{p_dict.get('direction', 'UNKNOWN')}`\n"
                                            f"🎯 *Entry / Вход:* `${p_dict.get('entry_price', 0):,.4f}` ➔ *Exit / Выход:* `${cur_p:,.4f}`\n"
                                            f"💰 *PnL:* `${pnl:,.2f}`\n"
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
