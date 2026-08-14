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
        self.total_pnl_usd = 0.0
        self.win_count = 0
        self.loss_count = 0
        self.recent_streak = []
        self.initial_deposit = 0.0
        self.net_transfers = 0.0
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, 'r') as f:
                    st = json.load(f)
                    self.total_pnl_usd = st.get('total_pnl_usd', 0.0)
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
                    "total_pnl_usd": self.total_pnl_usd,
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
        Converts generic symbol (e.g. BTC) to Kraken Futures Perpetual symbol format.
        Usually ccxt handles standard formats, e.g., 'BTC/USD:USD' or similar for linear perps.
        """
        base = symbol.upper()
        if base == "BTC":
            base = "BTC"
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
        account_roi_pct = ((total_balance - base_capital) / base_capital * 100) if base_capital > 0 else 0.0
        
        return {
            "total_usd": total_balance,
            "current_balance": total_balance,
            "initial_balance": base_capital,
            "total_pnl_usd": self.total_pnl_usd,
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

    async def _execute_market_order(self, symbol: str, direction: str, size_base: float, leverage: int = 1, sl_price: float = None):
        """Helper to execute order via CCXT, with optional Hard Stop-Loss"""
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
                    
            print(f"🌐 [KrakenTradingService] Отправка MARKET {side.upper()} ордера {size_base} {formatted_symbol}...")
            order = await self.exchange.create_market_order(formatted_symbol, side, size_base)
            
            status = order.get('status', 'unknown')
            print(f"✅ [KrakenTradingService] ЗАПРОС ПРИНЯТ! ID: {order.get('id')} | Статус: {status}")
            
            if status == 'rejected':
                print(f"❌ [KrakenTradingService] БИРЖА ОТКЛОНИЛА ОРДЕР! (Слишком большое плечо, нехватка маржи или Post-Only). Полный ответ: {order}")
                return None
            elif status == 'canceled':
                print(f"❌ [KrakenTradingService] БИРЖА ОТМЕНИЛА ОРДЕР! Полный ответ: {order}")
                return None
            return order
        except Exception as e:
            print(f"❌ [KrakenTradingService] Ошибка сети при отправке ордера: {e}")
            return None

    def register_pending_trade(self, trade_params: dict) -> str:
        trade_id = str(uuid.uuid4())[:8]
        trade_params["created_at"] = time.time()
        self.pending_trades[trade_id] = trade_params
        return trade_id

    async def wait_and_virtual_open(self, trade_id: str, tg_sender):
        import asyncio
        await asyncio.sleep(300) # Ждем 5 минут
        if trade_id in self.pending_trades:
            trade = self.pending_trades.pop(trade_id)
            symbol = trade.get("symbol", "UNKNOWN")
            print(f"👻 [Timeout] Пользователь не ответил. Открываем {symbol} виртуально.")
            trade.pop("created_at", None)
            trade["is_virtual"] = True
            await self.open_position(**trade)
            if tg_sender:
                await tg_sender.send_message(f"👻 Время на подтверждение вышло (5 мин). Сделка по {symbol} открыта ВИРТУАЛЬНО (Бумажная торговля). Утром проверим результат!")

    async def open_position(self, symbol: str, direction: str, entry_price: float, size_usd: float, tp_price: float, sl_price: float, leverage: int = 1, is_virtual: bool = False):
        """
        Calculates position size in base currency and opens a Market order via Kraken Futures API.
        If is_virtual is True, skips the API call and simulates the trade.
        """
        if not self.api_key and not is_virtual:
            print(f"❌ [KrakenTradingService] Нет API ключей. Сделка {direction} по {symbol} отменена.")
            return False

        mode_str = "ВИРТУАЛЬНОЙ" if is_virtual else "БОЕВОЙ"
        print(f"🚀 [KrakenTradingService] ПОДГОТОВКА {mode_str} СДЕЛКИ: {direction} {symbol}")
        
        size_base = size_usd / entry_price
        
        # Execute trade
        order_result = True
        if not is_virtual:
            order_result = await self._execute_market_order(symbol, direction, size_base, leverage, sl_price)
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
                    print(f"🔄 [KrakenTradingService] Уровни скорректированы. Новый SL: {sl_price}, Новый TP: {tp_price}")
                    entry_price = float(fill_price)
                    
                # Устанавливаем Hard Stop-Loss с учетом скорректированной цены и актуального объема
                if sl_price:
                    try:
                        stop_side = 'sell' if direction == 'LONG' else 'buy'
                        await self.exchange.create_order(formatted_symbol, 'stop', stop_side, actual_size, None, {'stopPrice': sl_price, 'reduceOnly': True})
                        print(f"🛡️ [KrakenTradingService] Установлен ЖЕСТКИЙ Stop-Loss на бирже по цене {sl_price}!")
                    except Exception as e:
                        print(f"⚠️ [KrakenTradingService] Биржа не приняла хард-стоп ({e}). НЕМЕДЛЕННО ЗАКРЫВАЕМ ПОЗИЦИЮ во избежание риска.")
                        try:
                            abort_side = 'sell' if direction == 'LONG' else 'buy'
                            await self.exchange.create_market_order(formatted_symbol, abort_side, actual_size)
                            print(f"✅ [KrakenTradingService] Аварийное закрытие (отмена) позиции {formatted_symbol} выполнено успешно.")
                        except Exception as abort_err:
                            print(f"🚨 [KrakenTradingService] КРИТИЧЕСКАЯ ОШИБКА АВАРИЙНОГО ЗАКРЫТИЯ: {abort_err}")
                        order_result = None  # Сбрасываем, чтобы позиция не добавилась
            else:
                actual_size = size_base
        
        if order_result:
            pos_id = str(uuid.uuid4())[:8]
            self.active_positions[symbol] = {
                "id": pos_id,
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "size_usd": size_usd,        # NOTIONAL
                "leverage": leverage,        # LEVERAGE
                "margin_usd": size_usd / leverage if leverage > 0 else size_usd, # MARGIN
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

    async def sync_with_exchange(self):
        """
        Синхронизирует локальные позиции с реальными позициями на бирже.
        Если сделка была закрыта вручную, она бесшумно удаляется из памяти.
        """
        if not self.exchange.apiKey:
            return

        try:
            exchange_positions = await self.exchange.fetch_positions()
            open_symbols = set()
            for p in exchange_positions:
                size = float(p.get('contracts', 0) or 0)
                info_size = float(p.get('info', {}).get('size', 0) or 0)
                if size > 0 or abs(info_size) > 0:
                    sym = p.get('symbol')
                    if sym:
                        open_symbols.add(sym)

            symbols_to_remove = []
            for symbol, pos in self.active_positions.items():
                if pos.get("is_virtual", False):
                    continue
                    
                formatted_symbol = symbol
                if hasattr(self, '_format_symbol'):
                    formatted_symbol = self._format_symbol(symbol)
                
                # Check if it matches exactly or starts with the symbol (some exchanges use BTC/USD:USD vs BTC/USD)
                found = False
                for open_sym in open_symbols:
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
                
        except Exception as e:
            print(f"⚠️ [State Sync] Ошибка синхронизации позиций: {e}")

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
        
        # Trailing Stop Logic (1.5% trail)
        trailing_pct = 0.015
        
        if "highest_price" not in pos: pos["highest_price"] = entry_price
        if "lowest_price" not in pos: pos["lowest_price"] = entry_price
        
        if direction == "LONG":
            pos["highest_price"] = max(pos["highest_price"], current_price)
            # Activate trailing stop only when profit > 1.5%
            if pos["highest_price"] >= entry_price * 1.015:
                trail_sl = pos["highest_price"] * (1 - trailing_pct)
                if trail_sl > pos["sl_price"]:
                    pos["sl_price"] = trail_sl
                    print(f"📈 [Keeper] {symbol} Trailing Stop подтянут до {trail_sl:.4f}")
                    if not pos.get("is_virtual", False):
                        import asyncio
                        asyncio.create_task(self._update_exchange_sl(symbol, direction, trail_sl, pos["size_base"]))
        else:
            pos["lowest_price"] = min(pos["lowest_price"], current_price)
            if pos["lowest_price"] <= entry_price * 0.985:
                trail_sl = pos["lowest_price"] * (1 + trailing_pct)
                if trail_sl < pos["sl_price"]:
                    pos["sl_price"] = trail_sl
                    print(f"📉 [Keeper] {symbol} Trailing Stop подтянут до {trail_sl:.4f}")
                    if not pos.get("is_virtual", False):
                        import asyncio
                        asyncio.create_task(self._update_exchange_sl(symbol, direction, trail_sl, pos["size_base"]))

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
                print(f"ℹ️ [Keeper] Оценка PnL пропущена для {symbol}, так как позиция была закрыта вне бота (Hard SL/TP или вручную).")
            else:
                if direction == "LONG":
                    pnl = (actual_exit_price - entry_price) * pos["size_base"]
                else:
                    pnl = (entry_price - actual_exit_price) * pos["size_base"]
                
                if pnl > 0:
                    self.win_count += 1
                    self.recent_streak.append("WIN")
                else:
                    self.loss_count += 1
                    self.recent_streak.append("LOSS")
                    
                self.recent_streak = self.recent_streak[-10:] # Keep only last 10
                self.total_pnl_usd += pnl
                self._save_stats()
            
            trigger_reason = triggered_exit
            if triggered_exit == "MANUAL_CLOSE":
                trigger_reason = "Ручное закрытие на бирже"
            elif is_virtual:
                trigger_reason = f"{triggered_exit} (Виртуально)"
                
            margin_usd = pos.get("margin_usd", pos["size_usd"] / pos.get("leverage", 1) if pos.get("leverage", 1) > 0 else pos["size_usd"])
                
            report = {
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "exit_price": actual_exit_price,
                "pnl_usd": pnl,
                "pnl_pct": (pnl / pos["size_usd"]) * 100 if pos["size_usd"] > 0 else 0.0,
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
        pnl = abs(current_price - entry_price) * (pos["size_usd"] / entry_price)
        if (direction == "LONG" and current_price < entry_price) or (direction == "SHORT" and current_price > entry_price):
            pnl = -pnl
            
        if pnl > 0: self.win_count += 1
        else: self.loss_count += 1
        self.total_pnl_usd += pnl
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
        self.total_pnl_usd = 0.0
        self._save_stats()
