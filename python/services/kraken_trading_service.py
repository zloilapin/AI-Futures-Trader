from core.interfaces import BaseTradingService
import os
import time
import asyncio
import uuid
import ccxt.async_support as ccxt
from typing import Dict, Any, List

class KrakenTradingService:
    """
    Live Execution Service for Kraken Futures via CCXT.
    Handles authenticated API requests, fetching balances, and opening/closing positions.
    """
    def __init__(self):
        self.api_key = os.getenv("KRAKEN_API_KEY", "")
        self.api_secret = os.getenv("KRAKEN_API_SECRET", "")
        
        self.exchange = ccxt.krakenfutures({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
        })
        
        if not self.api_key or not self.api_secret:
            print("⚠️ [KrakenTradingService] ВНИМАНИЕ: KRAKEN_API_KEY или KRAKEN_API_SECRET не найдены в .env!")
        else:
            print("✅ [KrakenTradingService] Учетные данные Kraken Futures инициализированы.")

        # Local tracker for active positions (Keeper Logic)
        self.positions_file = "data/active_positions.json"
        self.active_positions = self._load_positions()
        self.pending_trades = {}
        
        # Performance Tracking
        self.stats_file = "data/portfolio_stats.json"
        self._load_stats()
        
    def _load_stats(self):
        import os, json
        self.win_count = 0
        self.loss_count = 0
        self.recent_streak = []
        self.initial_deposit = 0.0
        self.net_transfers = 0.0
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, 'r') as f:
                    st = json.load(f)
                    self.win_count = st.get('win_count', 0)
                    self.loss_count = st.get('loss_count', 0)
                    self.recent_streak = st.get('recent_streak', [])
                    self.initial_deposit = st.get('initial_deposit', 0.0)
                    self.net_transfers = st.get('net_transfers', 0.0)
            except Exception as e:
                print(f"⚠️ [KrakenTradingService] Ошибка загрузки статистики: {e}")

    def _save_stats(self):
        import os, json
        os.makedirs(os.path.dirname(self.stats_file), exist_ok=True)
        try:
            with open(self.stats_file, 'w') as f:
                json.dump({
                    "win_count": self.win_count,
                    "loss_count": self.loss_count,
                    "recent_streak": self.recent_streak,
                    "initial_deposit": self.initial_deposit,
                    "net_transfers": self.net_transfers
                }, f)
        except Exception as e:
            print(f"⚠️ [KrakenTradingService] Ошибка сохранения статистики: {e}")

    def _load_positions(self) -> dict:
        import os, json
        if os.path.exists(self.positions_file):
            try:
                with open(self.positions_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ [KrakenTradingService] Ошибка загрузки активных позиций: {e}")
        return {}

    def _save_positions(self):
        import os, json
        os.makedirs(os.path.dirname(self.positions_file), exist_ok=True)
        try:
            with open(self.positions_file, 'w') as f:
                json.dump(self.active_positions, f, indent=4)
        except Exception as e:
            print(f"⚠️ [KrakenTradingService] Ошибка сохранения активных позиций: {e}")
        
    async def _close_exchange_async(self):
        """Clean up CCXT session"""
        if self.exchange:
            await self.exchange.close()

    def _format_symbol(self, symbol: str) -> str:
        """
        Converts generic symbol (e.g. BTC or BTC/USD) to Kraken Futures Perpetual symbol format.
        Usually ccxt handles standard formats, e.g., 'BTC/USD:USD' or similar for linear perps.
        """
        base = symbol.upper().replace("/USD", "").replace(":USD", "")
        return f"{base}/USD:USD"

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """
        Reads real USD margin balance from Kraken Futures.
        """
        total_balance = 0.0
        
        if self.api_key and self.api_secret:
            try:
                # Fetch balance
                balance = await self.exchange.fetch_balance()
                
                # Aggregate USD, USDC, and USDT for total margin available
                total_balance = 0.0
                
                # Try to get flex account details from Kraken Futures
                unrealized = 0.0
                used_margin = 0.0
                avail_margin = 0.0
                
                if 'info' in balance and 'accounts' in balance['info']:
                    accounts = balance['info']['accounts']
                    if isinstance(accounts, dict) and 'flex' in accounts:
                        flex = accounts['flex']
                        total_balance = float(flex.get('portfolioValue', 0.0))
                        unrealized = float(flex.get('totalUnrealized', 0.0))
                        used_margin = float(flex.get('initialMarginWithOrders', 0.0))
                        avail_margin = float(flex.get('availableMargin', 0.0))
                
                # Fallback to ccxt balance if flex account data isn't found or is 0
                if total_balance == 0.0:
                    for asset in ['USD', 'USDC', 'USDT']:
                        if asset in balance:
                            total_balance += float(balance[asset].get('total', 0.0))
                            
                    if total_balance == 0.0 and 'total' in balance:
                        tot_dict = balance['total']
                        total_balance += float(tot_dict.get('USD', 0.0))
                        total_balance += float(tot_dict.get('USDC', 0.0))
                        total_balance += float(tot_dict.get('USDT', 0.0))
                    
                    avail_margin = total_balance
                    
                # Вычисляем нереализованный PnL и задействованную маржу по нашим локальным данным (если биржа не отдает)
                # или если биржа отдает, берем из нее. Здесь мы используем локальный счетчик для простоты:
                for pos in self.active_positions.values():
                    # Приблизительный расчет нереализованного PnL для get_status
                    if pos.get("is_virtual"): continue
                    # Используем последнюю известную цену входа
                    ep = pos.get("entry_price", 0)
                    if ep > 0:
                        # Мы не знаем current price в get_status, поэтому unrealized будет оцениваться Keeper'ом,
                        # Но margin мы знаем точно:
                        m = pos.get("margin_usd", 0)
                        used_margin += m
                
                print(f"💰 [KrakenTradingService] Баланс аккаунта Kraken Futures (USD+USDC+USDT): ${total_balance:,.2f}")
            except Exception as e:
                print(f"⚠️ [KrakenTradingService] Ошибка запроса баланса: {e}")
                # Fallback so bot doesn't crash during debugging
                total_balance = 0.0
        else:
            print("⚠️ [KrakenTradingService] API ключи не настроены, возвращаем 0.0 баланс.")

        total_trades = self.win_count + self.loss_count
        win_rate = (self.win_count / total_trades * 100) if total_trades > 0 else 0.0
        
        # Обновляем initial_deposit при первом запуске
        if self.initial_deposit == 0.0 and total_balance > 0.0:
            self.initial_deposit = total_balance
            self._save_stats()
            
        base_capital = self.initial_deposit + self.net_transfers
        total_pnl_usd = total_balance - base_capital if base_capital > 0 else 0.0
        account_roi_pct = (total_pnl_usd / base_capital * 100) if base_capital > 0 else 0.0
        
        return {
            "total_usd": total_balance,
            "current_balance": total_balance,
            "initial_balance": base_capital,
            "total_pnl_usd": total_pnl_usd,
            "total_pnl_pct": account_roi_pct,
            "win_rate_pct": win_rate,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "recent_streak": self.recent_streak,
            "available_margin": avail_margin,
            "used_margin": used_margin,
            "active_positions_count": len(self.active_positions),
            "unrealized_pnl": unrealized,
            "roi_pct": (unrealized / used_margin * 100) if used_margin > 0 else 0.0
        }

    async def _execute_market_order(self, symbol: str, direction: str, size_base: float, leverage: int = 1, sl_price: float = None, tp_price: float = None):
        """Helper to execute order via CCXT, with optional atomic Hard Stop-Loss and Take-Profit"""
        formatted_symbol = self._format_symbol(symbol)
        side = 'buy' if direction == 'LONG' else 'sell'
        
        try:
            # Загружаем рынки, если они еще не загружены, чтобы CCXT мог правильно округлить объем
            if not self.exchange.markets:
                await self.exchange.load_markets()
                
            try:
                market = self.exchange.market(formatted_symbol)
                min_amount = market.get('limits', {}).get('amount', {}).get('min')
                if min_amount and size_base < min_amount:
                    print(f"❌ [KrakenTradingService] Размер позиции {size_base} меньше минимума биржи ({min_amount} {formatted_symbol}). Отмена.")
                    return None
                    
                # Пытаемся округлить объем до правильной точности биржи
                size_base = float(self.exchange.amount_to_precision(formatted_symbol, size_base))
            except Exception as e:
                print(f"⚠️ [KrakenTradingService] Не удалось автоматически округлить объем для {formatted_symbol}: {e}")
                # Безопасный fallback: не меняем кардинально объем (как раньше до 1.0), 
                # а просто округляем до 4 знаков. Если биржа не примет - она просто вернет ошибку.
                size_base = round(size_base, 4)

            if leverage > 1 and hasattr(self.exchange, 'set_leverage'):
                try:
                    await self.exchange.set_leverage(leverage, formatted_symbol)
                    print(f"⚙️ [KrakenTradingService] Плечо установлено на {leverage}x для {formatted_symbol}")
                except Exception as e:
                    print(f"⚠️ [KrakenTradingService] Не удалось динамически установить плечо {leverage}x: {e}")
                    
            params = {}
            if sl_price:
                params['stopLossPrice'] = sl_price
            if tp_price:
                params['takeProfitPrice'] = tp_price
                
            print(f"🌐 [KrakenTradingService] Отправка MARKET {side.upper()} ордера {size_base} {formatted_symbol}...")
            if params:
                print(f"🛡️ [KrakenTradingService] Прикреплены атомные параметры: {params}")
                
            order = await self.exchange.create_market_order(formatted_symbol, side, size_base, params=params)
            
            status = order.get('status', 'unknown')
            order_id = order.get('id')
            print(f"✅ [KrakenTradingService] ЗАПРОС ПРИНЯТ! ID: {order_id} | Статус: {status}")
            
            if status == 'rejected':
                print(f"❌ [KrakenTradingService] БИРЖА ОТКЛОНИЛА ОРДЕР! (Слишком большое плечо, нехватка маржи или Post-Only). Полный ответ: {order}")
                return None
            elif status == 'canceled':
                print(f"❌ [KrakenTradingService] БИРЖА ОТМЕНИЛА ОРДЕР! Полный ответ: {order}")
                return None
            
            # P0.4: Fill verification — if status is not final, poll for actual fill
            if status not in ('closed', 'canceled', 'rejected') and order_id:
                print(f"⏳ [P0.4 Fill Verify] Статус '{status}' — ожидание 2 сек и проверка финального fill...")
                await asyncio.sleep(2)
                try:
                    final_order = await self.exchange.fetch_order(order_id, formatted_symbol)
                    order = final_order  # Replace with verified data
                    print(f"✅ [P0.4 Fill Verify] Финальный статус: {order.get('status')} | filled: {order.get('filled')} | remaining: {order.get('remaining')}")
                except Exception as e:
                    print(f"⚠️ [P0.4 Fill Verify] Не удалось проверить финальный статус ордера: {e}")
            
            return order
        except Exception as e:
            print(f"❌ [KrakenTradingService] Ошибка сети при отправке ордера: {e}")
            return None


    async def open_position(self, symbol: str, direction: str, entry_price: float, notional_usd: float, tp_price: float, sl_price: float, leverage: int = 1, is_virtual: bool = False):
        """
        Calculates position size in base currency and opens a Market order via Kraken Futures API.
        If is_virtual is True, skips the API call and simulates the trade.
        
        Safety guards:
        - P0.5: Local idempotency — rejects if symbol already in active_positions
        - P0.1: Exchange duplicate guard — checks fetch_positions() before opening
        """
        if not self.api_key and not is_virtual:
            print(f"❌ [KrakenTradingService] Нет API ключей. Сделка {direction} по {symbol} отменена.")
            return False

        # ═══ P0.5: Local idempotency guard ═══
        if symbol in self.active_positions:
            existing = self.active_positions[symbol]
            if not existing.get("manually_closed", False):
                print(f"❌ [P0.5 Idempotency] Позиция {symbol} уже существует в active_positions (dir={existing.get('direction')}). Дубликат заблокирован.")
                return False

        # ═══ P0.1: Exchange-level duplicate guard ═══
        if not is_virtual and self.api_key:
            try:
                exchange_positions = await self.exchange.fetch_positions()
                formatted_symbol = self._format_symbol(symbol)
                for p in exchange_positions:
                    ex_sym = p.get('symbol', '')
                    ex_contracts = float(p.get('contracts', 0) or 0)
                    ex_info_size = abs(float(p.get('info', {}).get('size', 0) or 0))
                    if (ex_sym == formatted_symbol or ex_sym.replace(':', '') == formatted_symbol.replace(':', '')):
                        if ex_contracts > 0 or ex_info_size > 0:
                            print(f"❌ [P0.1 Duplicate Guard] Позиция {symbol} УЖЕ ОТКРЫТА на бирже (contracts={ex_contracts})! Дубликат заблокирован.")
                            return False
            except Exception as e:
                print(f"🚨 [P0.1 Duplicate Guard] НЕ УДАЛОСЬ проверить позиции на бирже: {e}. Открывать позицию ОПАСНО (может быть дубликат). БЛОКИРОВКА СДЕЛКИ.")
                return False

        mode_str = "ВИРТУАЛЬНОЙ" if is_virtual else "БОЕВОЙ"
        print(f"🚀 [KrakenTradingService] ПОДГОТОВКА {mode_str} СДЕЛКИ: {direction} {symbol}")
        
        size_base = notional_usd / entry_price
        actual_size = size_base  # Default; overridden by actual fill data for live trades
        
        # Execute trade
        order_result = True
        if not is_virtual:
            # We do NOT pass sl_price and tp_price here, to ensure we create them immediately after and verify creation synchronously
            order_result = await self._execute_market_order(symbol, direction, size_base, leverage, sl_price=None, tp_price=None)
            if isinstance(order_result, dict):
                # Проверка фактического исполнения и комиссии
                filled = order_result.get('filled')
                remaining = order_result.get('remaining', 0.0)
                status = order_result.get('status')
                fee_info = order_result.get('fee')
                formatted_symbol = self._format_symbol(symbol)
                
                if status == 'open' and remaining > 0:
                    print(f"⚠️ [KrakenTradingService] Ордер частично исполнен (filled: {filled}, remaining: {remaining}). Отменяем остаток.")
                    try:
                        order_id = order_result.get('id')
                        if order_id:
                            await self.exchange.cancel_order(order_id, formatted_symbol)
                    except Exception as e:
                        print(f"⚠️ [KrakenTradingService] Ошибка отмены остатка: {e}")
                
                if filled == 0 and status != 'unknown':
                    print(f"❌ [KrakenTradingService] Ордер не исполнен (filled = 0). Сделка отменена.")
                    return False
                    
                if fee_info and isinstance(fee_info, dict):
                    fee_cost = float(fee_info.get('cost', 0.0))
                    if fee_cost > 0:
                        self.adjust_ledger(-fee_cost)
                        print(f"💸 [KrakenTradingService] Комиссия за сделку: {fee_cost:.4f} {fee_info.get('currency', '')} вычтена из Ledger.")

                actual_size = filled if (filled and filled > 0) else (order_result.get('amount') or size_base)
                
                # Проверка проскальзывания и обновление entry_price
                fill_price = order_result.get('average') or order_result.get('price') or entry_price
                if fill_price > 0 and abs(fill_price - entry_price) / entry_price > 0.0001:
                    print(f"⚠️ [KrakenTradingService] Slippage detected! Planned: {entry_price}, Actual: {fill_price}")
                    slippage_diff = fill_price - entry_price
                    if tp_price: tp_price = tp_price + slippage_diff
                    if sl_price: sl_price = sl_price + slippage_diff
                    print(f"🔄 [KrakenTradingService] Уровни скорректированы из-за проскальзывания. Новый SL: {sl_price:.4f}, Новый TP: {tp_price:.4f}")
                    entry_price = float(fill_price)

                # ═══ P0.2: IMMEDIATELY CREATE REDUCE-ONLY SL ═══
                if sl_price and sl_price > 0:
                    print(f"🛡️ [P0.2 SL] Немедленное создание Hard Stop-Loss: {sl_price}...")
                    stop_side = 'sell' if direction == 'LONG' else 'buy'
                    try:
                        formatted_symbol = self._format_symbol(symbol)
                        await self.exchange.create_order(
                            formatted_symbol, 'stop', stop_side, actual_size, None,
                            {'stopPrice': sl_price, 'reduceOnly': True}
                        )
                        print(f"✅ [P0.2 SL] Hard Stop-Loss успешно создан: {sl_price}")
                    except Exception as sl_err:
                        print(f"🚨 [P0.2 SL] КРИТИЧЕСКАЯ ОШИБКА: не удалось создать SL! Аварийное закрытие позиции...")
                        print(f"🚨 [P0.2 SL] Ошибка: {sl_err}")
                        # Emergency close
                        try:
                            close_side = 'sell' if direction == 'LONG' else 'buy'
                            await self.exchange.create_market_order(formatted_symbol, close_side, actual_size, params={'reduceOnly': True})
                            print(f"🚨 [P0.2 SL] Позиция аварийно закрыта из-за невозможности установить SL!")
                            return False
                        except Exception as close_err:
                            print(f"🚨🚨🚨 [P0.2] НЕВОЗМОЖНО ЗАКРЫТЬ ПОЗИЦИЮ БЕЗ SL! ТРЕБУЕТСЯ РУЧНОЕ ВМЕШАТЕЛЬСТВО! Ошибка: {close_err}")
                            return False

                # ═══ P0.2.1: IMMEDIATELY CREATE REDUCE-ONLY TP ═══
                if tp_price and tp_price > 0:
                    print(f"🎯 [P0.2 TP] Немедленное создание Hard Take-Profit: {tp_price}...")
                    tp_side = 'sell' if direction == 'LONG' else 'buy'
                    try:
                        formatted_symbol = self._format_symbol(symbol)
                        await self.exchange.create_order(
                            formatted_symbol, 'take_profit', tp_side, actual_size, None,
                            {'stopPrice': tp_price, 'reduceOnly': True}
                        )
                        print(f"✅ [P0.2 TP] Hard Take-Profit успешно создан: {tp_price}")
                    except Exception as tp_err:
                        print(f"🚨 [P0.2 TP] КРИТИЧЕСКАЯ ОШИБКА: не удалось создать TP! Аварийное закрытие позиции и отмена SL...")
                        print(f"🚨 [P0.2 TP] Ошибка: {tp_err}")
                        try:
                            # 1. Отменяем созданный SL
                            open_orders = await self.exchange.fetch_open_orders(formatted_symbol)
                            for order in open_orders:
                                if order.get('type') in ('stop', 'stop-loss', 'stopMarket'):
                                    await self.exchange.cancel_order(order['id'], formatted_symbol)
                            
                            # 2. Закрываем саму позицию
                            close_side = 'sell' if direction == 'LONG' else 'buy'
                            await self.exchange.create_market_order(formatted_symbol, close_side, actual_size, params={'reduceOnly': True})
                            print(f"🚨 [P0.2 TP] Позиция аварийно закрыта из-за невозможности установить TP!")
                            return False
                        except Exception as close_err:
                            print(f"🚨🚨🚨 [P0.2] НЕВОЗМОЖНО ЗАКРЫТЬ ПОЗИЦИЮ ПРИ ОШИБКЕ TP! ТРЕБУЕТСЯ РУЧНОЕ ВМЕШАТЕЛЬСТВО! Ошибка: {close_err}")
                            return False

            else:
                actual_size = size_base
                
            # ═══ P1: Authoritative Liquidation Check ═══
            if not is_virtual and sl_price and sl_price > 0:
                try:
                    formatted_symbol = self._format_symbol(symbol)
                    ex_positions = await self.exchange.fetch_positions()
                    for p in ex_positions:
                        ex_sym = p.get('symbol', '')
                        if ex_sym == formatted_symbol or ex_sym.replace(':', '') == formatted_symbol.replace(':', ''):
                            actual_liq = p.get('liquidationPrice')
                            if actual_liq and actual_liq > 0:
                                needs_adjustment = False
                                if direction == 'LONG' and sl_price <= actual_liq:
                                    print(f"⚠️ [P1 Auth Check] Изначальный SL ({sl_price}) ниже или равен реальной ликвидации ({actual_liq})! Двигаем SL...")
                                    sl_price = actual_liq * 1.005
                                    needs_adjustment = True
                                elif direction == 'SHORT' and sl_price >= actual_liq:
                                    print(f"⚠️ [P1 Auth Check] Изначальный SL ({sl_price}) выше или равен реальной ликвидации ({actual_liq})! Двигаем SL...")
                                    sl_price = actual_liq * 0.995
                                    needs_adjustment = True
                                
                                if needs_adjustment:
                                    await self._update_exchange_sl(symbol, direction, sl_price, actual_size)
                            break
                except Exception as liq_err:
                    print(f"⚠️ [P1 Auth Check] Не удалось проверить реальную цену ликвидации: {liq_err}")
        
        if order_result:
            pos_id = str(uuid.uuid4())[:8]
            self.active_positions[symbol] = {
                "id": pos_id,
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "notional_usd": notional_usd,        # NOTIONAL
                "leverage": leverage,        # LEVERAGE
                "margin_usd": notional_usd / leverage if leverage > 0 else notional_usd, # MARGIN
                "size_base": actual_size,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "breakeven_activated": False,
                "is_virtual": is_virtual,
                "timestamp": time.time()
            }
            self._save_positions()
            return True
        return False

    async def _update_exchange_sl(self, symbol: str, direction: str, new_sl: float, size_base: float):
        try:
            formatted_symbol = self._format_symbol(symbol) if hasattr(self, '_format_symbol') else symbol
            # Cancel all existing stop orders for this symbol first
            open_orders = await self.exchange.fetch_open_orders(formatted_symbol)
            for order in open_orders:
                if order.get('type') == 'stop' or order.get('type') == 'stop-loss' or order.get('type') == 'stopMarket':
                    await self.exchange.cancel_order(order['id'], formatted_symbol)
                    
            # Create a new stop loss order
            stop_side = 'sell' if direction == 'LONG' else 'buy'
            await self.exchange.create_order(formatted_symbol, 'stop', stop_side, size_base, None, {'stopPrice': new_sl, 'reduceOnly': True})
            print(f"✅ [KrakenTradingService] Успешно обновлен Hard Stop-Loss на бирже: {new_sl} для {symbol}")
        except Exception as e:
            print(f"⚠️ [KrakenTradingService] Ошибка при обновлении Hard Stop-Loss на бирже: {e}")

    async def sync_with_exchange(self):
        """
        Bidirectional state synchronization with Kraken Futures.
        
        1. Removes local positions that no longer exist on the exchange (closed externally).
        2. Restores exchange positions that are missing from local state (crash recovery, manual opens).
        
        JSON is treated as a CACHE. Kraken is the source of truth.
        """
        if not self.exchange.apiKey:
            return

        try:
            exchange_positions = await self.exchange.fetch_positions()
            
            # Build a map of real exchange positions: { formatted_symbol: position_data }
            exchange_map = {}
            for p in exchange_positions:
                size = float(p.get('contracts', 0) or 0)
                info_size = float(p.get('info', {}).get('size', 0) or 0)
                if size > 0 or abs(info_size) > 0:
                    sym = p.get('symbol')
                    if sym:
                        exchange_map[sym] = p

            # --- PHASE 1: Remove stale local positions ---
            symbols_to_remove = []
            for symbol, pos in self.active_positions.items():
                if pos.get("is_virtual", False):
                    continue
                    
                formatted_symbol = self._format_symbol(symbol)
                
                found = False
                for open_sym in exchange_map:
                    if open_sym == formatted_symbol or open_sym.replace(':', '') == formatted_symbol.replace(':', ''):
                        found = True
                        break
                        
                if not found:
                    print(f"🔄 [State Sync] Сделка {symbol} не найдена на бирже (вероятно закрыта вручную). Удаление из памяти.")
                    symbols_to_remove.append(symbol)
                    
            for symbol in symbols_to_remove:
                self.active_positions[symbol]["manually_closed"] = True
                
            if symbols_to_remove:
                self._save_positions()

            # --- PHASE 2: Restore orphaned exchange positions ---
            # Build a set of formatted symbols we already track locally
            local_formatted = set()
            for symbol in self.active_positions:
                if not self.active_positions[symbol].get("is_virtual", False):
                    local_formatted.add(self._format_symbol(symbol))

            restored_count = 0
            for ex_symbol, ex_pos in exchange_map.items():
                # Check if we already track this position
                already_tracked = False
                for local_sym in local_formatted:
                    if ex_symbol == local_sym or ex_symbol.replace(':', '') == local_sym.replace(':', ''):
                        already_tracked = True
                        break
                
                if already_tracked:
                    continue
                
                # --- Reconstruct position from exchange data ---
                try:
                    contracts = float(ex_pos.get('contracts', 0) or 0)
                    info_size = float(ex_pos.get('info', {}).get('size', 0) or 0)
                    size_base = contracts if contracts > 0 else abs(info_size)
                    
                    if size_base <= 0:
                        continue
                    
                    # Direction
                    side = ex_pos.get('side', '').lower()
                    if side == 'long':
                        direction = 'LONG'
                    elif side == 'short':
                        direction = 'SHORT'
                    else:
                        # Fallback: positive info.size = long, negative = short
                        direction = 'LONG' if info_size > 0 else 'SHORT'
                    
                    # Entry price
                    entry_price = float(ex_pos.get('entryPrice', 0) or 0)
                    if entry_price <= 0:
                        entry_price = float(ex_pos.get('info', {}).get('entry_price', 0) or 0)
                    if entry_price <= 0:
                        print(f"⚠️ [State Sync] Позиция {ex_symbol} на бирже, но entry_price = 0. Пропуск.")
                        continue
                    
                    # Leverage
                    leverage = int(float(ex_pos.get('leverage', 1) or 1))
                    if leverage <= 0:
                        leverage = 1
                    
                    # Notional & Margin
                    notional_usd = size_base * entry_price
                    margin_usd = notional_usd / leverage if leverage > 0 else notional_usd
                    
                    # Derive short symbol key (e.g. "BTC" from "BTC/USD:USD")
                    short_symbol = ex_symbol.split('/')[0] if '/' in ex_symbol else ex_symbol
                    
                    # Best-effort SL/TP recovery from open conditional orders
                    sl_price = 0.0
                    tp_price = 0.0
                    try:
                        open_orders = await self.exchange.fetch_open_orders(ex_symbol)
                        for order in open_orders:
                            order_type = (order.get('type') or '').lower()
                            trigger = float(order.get('triggerPrice') or order.get('stopPrice') or order.get('info', {}).get('triggerPrice', 0) or 0)
                            if trigger <= 0:
                                continue
                            
                            if order_type in ('stop', 'stop-loss', 'stopmarket', 'stop_loss_limit'):
                                sl_price = trigger
                            elif order_type in ('take_profit', 'takeprofit', 'takeprofitmarket', 'take_profit_limit'):
                                tp_price = trigger
                    except Exception as e:
                        print(f"⚠️ [State Sync] Не удалось загрузить ордера для {ex_symbol}: {e}")
                    
                    # Build position record
                    pos_id = str(uuid.uuid4())[:8]
                    self.active_positions[short_symbol] = {
                        "id": pos_id,
                        "symbol": short_symbol,
                        "direction": direction,
                        "entry_price": entry_price,
                        "notional_usd": notional_usd,
                        "leverage": leverage,
                        "margin_usd": margin_usd,
                        "size_base": size_base,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "breakeven_activated": False,
                        "is_virtual": False,
                        "timestamp": time.time(),
                        "restored_from_exchange": True
                    }
                    restored_count += 1
                    
                    sl_status = f"${sl_price:,.2f}" if sl_price > 0 else "НЕТ"
                    tp_status = f"${tp_price:,.2f}" if tp_price > 0 else "НЕТ"
                    print(f"🔄 [State Sync] ═══════════════════════════════════════")
                    print(f"🔄 [State Sync] ВОССТАНОВЛЕНА ПОТЕРЯННАЯ ПОЗИЦИЯ ИЗ KRAKEN!")
                    print(f"🔄 [State Sync]   Символ: {short_symbol} | Направление: {direction}")
                    print(f"🔄 [State Sync]   Вход: ${entry_price:,.2f} | Объем: {size_base}")
                    print(f"🔄 [State Sync]   Notional: ${notional_usd:,.2f} | Маржа: ${margin_usd:,.2f}")
                    print(f"🔄 [State Sync]   SL: {sl_status} | TP: {tp_status}")
                    print(f"🔄 [State Sync] ═══════════════════════════════════════")
                    
                except Exception as e:
                    print(f"⚠️ [State Sync] Ошибка восстановления позиции {ex_symbol}: {e}")
            
            if restored_count > 0:
                self._save_positions()
                print(f"✅ [State Sync] Восстановлено {restored_count} позиций из Kraken.")
                
        except Exception as e:
            print(f"🚨 [State Sync] Ошибка синхронизации позиций: {e}")
            raise RuntimeError(f"FAIL-OPEN PREVENTED: Синхронизация с Kraken не удалась ({e}). Торговля приостановлена.")

    async def check_and_update_positions(self, symbol: str, current_price: float) -> List[Dict[str, Any]]:
        """
        Keeper logic: acts as a software stop-loss.
        If current price hits TP/SL locally, sends a market order to close.
        Since this method is called synchronously in main loop, we dispatch closing orders via asyncio.create_task.
        """
        closed_reports = []
        if symbol not in self.active_positions:
            return closed_reports
            
        pos = self.active_positions[symbol]
        direction = pos["direction"]
        tp_price = pos["tp_price"]
        sl_price = pos["sl_price"]
        entry_price = pos["entry_price"]
        
        # Breakeven Guard (SL -> Entry at 50% TP)
        # Если цена прошла 50% пути до тейк-профита, стоп лосс переводится в безубыток
        if direction == "LONG":
            halfway_to_tp = entry_price + ((tp_price - entry_price) * 0.5)
            if current_price >= halfway_to_tp and pos["sl_price"] < entry_price:
                pos["sl_price"] = entry_price
                print(f"🛡️ [Breakeven Guard] {symbol} Цена прошла 50% до TP. SL переведен в безубыток: {entry_price:.4f}")
                if not pos.get("is_virtual", False):
                    import asyncio
                    asyncio.create_task(self._update_exchange_sl(symbol, direction, entry_price, pos["size_base"]))
        else:
            halfway_to_tp = entry_price - ((entry_price - tp_price) * 0.5)
            if current_price <= halfway_to_tp and pos["sl_price"] > entry_price:
                pos["sl_price"] = entry_price
                print(f"🛡️ [Breakeven Guard] {symbol} Цена прошла 50% до TP. SL переведен в безубыток: {entry_price:.4f}")
                if not pos.get("is_virtual", False):
                    import asyncio
                    asyncio.create_task(self._update_exchange_sl(symbol, direction, entry_price, pos["size_base"]))

        # TP / SL Execution trigger
        triggered_exit = None
        
        # TTL Check (8 hours)
        ttl_seconds = 8 * 3600
        time_alive = time.time() - pos.get("timestamp", time.time())
        if time_alive > ttl_seconds:
            triggered_exit = "TIME_STOP"
            print(f"⏱️ [KrakenTradingService/Keeper] Сделка по {symbol} открыта более 8 часов. Срабатывает Time-Based Stop.")
            
        if pos.get("manually_closed"):
            triggered_exit = "MANUAL_CLOSE"
            
        if not triggered_exit:
            if direction == "LONG":
                if current_price >= tp_price:
                    triggered_exit = "TP"
                elif current_price <= sl_price:
                    triggered_exit = "SL"
            else:
                if current_price <= tp_price:
                    triggered_exit = "TP"
                elif current_price >= sl_price:
                    triggered_exit = "SL"
                
        if triggered_exit:
            is_virtual = pos.get("is_virtual", False)
            mode_prefix = "👻 [ВИРТУАЛЬНО]" if is_virtual else "⚡ [БОЕВАЯ]"
            print(f"{mode_prefix} [Keeper] Сработал {triggered_exit} для {symbol}! Отправка ордера на закрытие...")
            
            # Close position asynchronously if not virtual and not manually closed
            actual_exit_price = current_price
            if not is_virtual and triggered_exit != "MANUAL_CLOSE":
                close_direction = "SHORT" if direction == "LONG" else "LONG"
                close_result = await self._execute_market_order(symbol, close_direction, pos["size_base"])
                if close_result is None:
                    print(f"❌ [Keeper] Ошибка закрытия сделки {symbol}. Отмена удаления из памяти.")
                    return closed_reports
                
                if isinstance(close_result, dict):
                    avg_price = close_result.get('average') or close_result.get('price')
                    if avg_price:
                        actual_exit_price = float(avg_price)
            
            # Correct PnL calculation independent of exit reason
            if triggered_exit == "MANUAL_CLOSE":
                pnl = 0.0
                print(f"ℹ️ [Keeper] Позиция закрыта вне бота (Hard SL/TP или вручную). Оцениваем Win/Loss по текущей рыночной цене {current_price}.")
                if direction == "LONG":
                    estimated_pnl = (current_price - entry_price) * pos["size_base"]
                else:
                    estimated_pnl = (entry_price - current_price) * pos["size_base"]
                
                if estimated_pnl > 0:
                    self.win_count += 1
                    self.recent_streak.append("WIN")
                else:
                    self.loss_count += 1
                    self.recent_streak.append("LOSS")
                
                self.recent_streak = self.recent_streak[-10:]
                self._save_stats()
            else:
                if direction == "LONG":
                    pnl = (actual_exit_price - entry_price) * pos["size_base"]
                else:
                    pnl = (entry_price - actual_exit_price) * pos["size_base"]
                
                # Deduct exit fee (Taker 0.05%) from PnL for accurate tracking
                # Entry fee is already deducted via adjust_ledger when the order is placed.
                exit_fee = pos.get("notional_usd", 0) * 0.0005
                if exit_fee > 0:
                    pnl -= exit_fee
                    print(f"💸 [Keeper] Комиссия за закрытие (Taker 0.05%): ${exit_fee:.4f} вычтена из PnL.")

                if pnl > 0:
                    self.win_count += 1
                    self.recent_streak.append("WIN")
                else:
                    self.loss_count += 1
                    self.recent_streak.append("LOSS")
                    
                self.recent_streak = self.recent_streak[-10:] # Keep only last 10
                self._save_stats()
            
            trigger_reason = triggered_exit
            if triggered_exit == "MANUAL_CLOSE":
                trigger_reason = "Ручное закрытие на бирже"
            elif is_virtual:
                trigger_reason = f"{triggered_exit} (Виртуально)"
                
            notional_usd = pos.get("notional_usd", 0.0)
            margin_usd = pos.get("margin_usd", notional_usd / pos.get("leverage", 1) if pos.get("leverage", 1) > 0 else notional_usd)
                
            report = {
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "exit_price": actual_exit_price,
                "pnl_usd": pnl,
                "pnl_pct": (pnl / notional_usd) * 100 if notional_usd > 0 else 0.0,
                "roi_pct": (pnl / margin_usd) * 100 if margin_usd > 0 else 0.0,
                "margin_usd": margin_usd,
                "leverage": pos.get("leverage", 1),
                "triggered_by": trigger_reason
            }
            print(f"🧹 Позиция {symbol} удалена из активных (причина: {triggered_exit}).")
            del self.active_positions[symbol]
            self._save_positions()
            
            closed_reports.append(report)
            
        return closed_reports

    async def force_close_position(self, symbol: str) -> tuple[bool, Any]:
        """Manually forces a close via Telegram button."""
        if symbol not in self.active_positions:
            return False, "Позиция не найдена"
            
        pos = self.active_positions[symbol]
        direction = pos["direction"]
        is_virtual = pos.get("is_virtual", False)
        close_direction = "SHORT" if direction == "LONG" else "LONG"
        
        try:
            current_price = pos["entry_price"]
            if self.exchange:
                ticker = await self.exchange.fetch_ticker(self._format_symbol(symbol))
                current_price = ticker['last']
            
            if not is_virtual:
                close_res = await self._execute_market_order(symbol, close_direction, pos["size_base"])
                if close_res is None:
                    return False, "Ошибка биржи при закрытии позиции"
        except Exception as e:
            return False, f"Ошибка биржи: {e}"
            
        entry_price = pos["entry_price"]
        pnl = abs(current_price - entry_price) * (pos["notional_usd"] / entry_price)
        if (direction == "LONG" and current_price < entry_price) or (direction == "SHORT" and current_price > entry_price):
            pnl = -pnl
            
        if pnl > 0: self.win_count += 1
        else: self.loss_count += 1
        self._save_stats()
        
        del self.active_positions[symbol]
        self._save_positions()
        return True, {"pnl_usd": pnl, "exit_price": current_price, "is_virtual": is_virtual}

    def adjust_ledger(self, amount_usd: float):
        """Ручная корректировка депозита (положительная = пополнение, отрицательная = вывод)"""
        self.net_transfers += amount_usd
        self._save_stats()
        
    def reset_ledger(self, new_balance: float = 0.0):
        """Сбрасывает Ledger и устанавливает текущий баланс как начальный."""
        self.initial_deposit = new_balance
        self.net_transfers = 0.0
        self._save_stats()
