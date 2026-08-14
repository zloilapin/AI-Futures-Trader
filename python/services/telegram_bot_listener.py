import os
import aiohttp
import asyncio
import traceback
from typing import Callable, Awaitable, Dict, Any, Optional

from services.paper_trading_service import PaperTradingService

class TelegramBotListener:
    """
    Background listener for interactive Telegram commands (/status, /pnl, /balance, /scan, /help).
    """
    def __init__(self, trading_service, trigger_scan_callback: Optional[Callable[[], Awaitable[None]]] = None):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.trading_service = trading_service
        self.trigger_scan_callback = trigger_scan_callback
        self.offset = 0
        self.running = False
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates" if self.bot_token else None
        self.send_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage" if self.bot_token else None
        self._background_tasks = set()  # Prevent GC of background tasks
        self.is_scanning = False

    async def _send_reply(self, text: str, reply_markup: dict = None):
        if not self.send_url or not self.chat_id:
            print(f"⚠️ [TelegramListener] _send_reply: send_url или chat_id не задан! send_url={bool(self.send_url)}, chat_id={bool(self.chat_id)}")
            return
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.send_url, json=payload) as resp:
                    if resp.status == 200:
                        print(f"✅ [TelegramListener] Ответ отправлен в Telegram (Markdown).")
                    else:
                        error_text = await resp.text()
                        print(f"⚠️ [TelegramListener] Markdown ответ не прошёл (HTTP {resp.status}): {error_text[:200]}")
                        # Повторяем без Markdown
                        payload.pop("parse_mode", None)
                        async with session.post(self.send_url, json=payload) as resp2:
                            if resp2.status == 200:
                                print(f"✅ [TelegramListener] Ответ отправлен без форматирования.")
                            else:
                                error_text2 = await resp2.text()
                                print(f"❌ [TelegramListener] Не удалось отправить ответ (HTTP {resp2.status}): {error_text2[:200]}")
        except Exception as e:
            print(f"❌ [TelegramListener] Ошибка отправки ответа: {type(e).__name__}: {e}")

    async def handle_command(self, text: str):
        cmd = text.strip().split()[0].lower()
        print(f"📩 [TelegramListener] Получена команда: {cmd}")

        try:
            if cmd in ["/status", "/info"]:
                profile = os.getenv("TRADING_PROFILE", "BALANCED")
                interval = os.getenv("SCAN_INTERVAL_MINUTES", "15")
                rest_start = os.getenv("REST_START_TIME", "19:00")
                rest_end = os.getenv("REST_END_TIME", "07:00")
                
                summary = await self.trading_service.get_portfolio_summary()
                reply = (
                    f"🤖 *AI TRADER STATUS | KRAKEN FUTURES*\n\n"
                    f"🟢 *Статус системы:* Активна 24/7\n"
                    f"⏱️ *Интервал сканирования:* каждые {interval} мин\n"
                    f"⚙️ *Профиль риска:* `{profile}`\n"
                    f"🌙 *График отдыха:* с {rest_start} до {rest_end} МСК\n\n"
                    f"💰 *Текущий баланс:* `${summary['current_balance']:,.2f}`\n"
                    f"💼 *Активных позиций:* `{summary['active_positions_count']}`"
                )
                await self._send_reply(reply)
                
            elif cmd == "/deposit":
                parts = text.strip().split()
                if len(parts) > 1:
                    amount = float(parts[1])
                    if hasattr(self.trading_service, "adjust_ledger"):
                        self.trading_service.adjust_ledger(amount)
                        await self._send_reply(f"✅ Внесено (Deposit): `${amount:,.2f}`.\nКапитал для расчета ROI обновлен.")
                else:
                    await self._send_reply("Использование: `/deposit <сумма>`")
                    
            elif cmd == "/withdraw":
                parts = text.strip().split()
                if len(parts) > 1:
                    amount = float(parts[1])
                    if hasattr(self.trading_service, "adjust_ledger"):
                        self.trading_service.adjust_ledger(-amount)
                        await self._send_reply(f"✅ Выведено (Withdraw): `${amount:,.2f}`.\nКапитал для расчета ROI обновлен.")
                else:
                    await self._send_reply("Использование: `/withdraw <сумма>`")
                    
            elif cmd == "/reset_ledger":
                if hasattr(self.trading_service, "reset_ledger"):
                    self.trading_service.reset_ledger()
                    await self._send_reply("🔄 Ledger сброшен! Текущий баланс стал новой стартовой точкой для подсчета ROI.")
                else:
                    await self._send_reply("Сброс Ledger не поддерживается текущим режимом торговли.")

            elif cmd in ["/pnl", "/balance", "/portfolio"]:
                s = await self.trading_service.get_portfolio_summary()
                reply = (
                    f"📊 *PORTFOLIO & PnL SUMMARY*\n\n"
                    f"💵 *Эквити (С учетом PnL):* `${s['current_balance']:,.2f}`\n"
                    f"🛡️ *Свободная маржа:* `${s['available_margin']:,.2f}`\n"
                    f"💰 *Нереализованный PnL:* `${s['unrealized_pnl']:+.2f}` (ROI: {s.get('roi_pct', 0):+.2f}%)\n"
                    f"📈 *Общий PnL (закрытые):* `${s['total_pnl_usd']:+.2f}` ({s['total_pnl_pct']:+.2f}%)\n"
                    f"💼 *Открытых позиций:* `{s['active_positions_count']}`\n"
                    f"🏆 *Винрейт:* `{s['win_rate_pct']}%` (Побед: {s['win_count']} / Потерь: {s['loss_count']})\n"
                    f"🏦 *Начальный депозит:* `${s['initial_balance']:,.2f}`"
                )
                await self._send_reply(reply)

            elif cmd in ["/positions", "/pos"]:
                positions = getattr(self.trading_service, "active_positions", {})
                if not positions:
                    await self._send_reply("💼 *Открытых позиций сейчас нет.*")
                else:
                    reply_lines = [f"💼 *ТЕКУЩИЕ ОТКРЫТЫЕ ПОЗИЦИИ ({len(positions)}):*\n"]
                    inline_keyboard = []
                    
                    for sym, pos in positions.items():
                        mode = "👻 Вирт" if pos.get("is_virtual") else "⚡ Боевая"
                        direction = pos.get("direction", "UNKNOWN")
                        icon = "🟢" if direction == "LONG" else "🔴"
                        entry = pos.get("entry_price", 0)
                        size = pos.get("size_usd", 0)
                        leverage = pos.get("leverage", 1)
                        margin = pos.get("margin_usd", size / leverage if leverage > 0 else size)
                        
                        reply_lines.append(f"{icon} *{sym}* | {direction} | {mode}")
                        reply_lines.append(f"💵 Вход: `${entry:,.2f}` | Объем: `${size:,.0f}` (Маржа: ${margin:,.0f}, {leverage}x)\n")
                        
                        inline_keyboard.append([{"text": f"❌ Закрыть {sym}", "callback_data": f"forceclose_{sym}"}])
                    
                    reply_markup = {"inline_keyboard": inline_keyboard}
                    await self._send_reply("\n".join(reply_lines), reply_markup=reply_markup)

            elif cmd in ["/risk", "/profile"]:
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "🛡️ Консервативный", "callback_data": "setrisk_CONSERVATIVE"}],
                        [{"text": "⚖️ Сбалансированный", "callback_data": "setrisk_BALANCED"}],
                        [{"text": "🔥 Агрессивный", "callback_data": "setrisk_AGGRESSIVE"}]
                    ]
                }
                await self._send_reply("⚙️ *Выберите профиль риска:*\n(Применится ко всем новым сделкам)", reply_markup=reply_markup)

            elif cmd in ["/scan", "/run"]:
                print(f"🔔 [TelegramListener] Обработка команды /scan...")
                if self.is_scanning:
                    await self._send_reply("⏳ Сканирование уже выполняется. Пожалуйста, дождитесь окончания текущего цикла.")
                elif self.trigger_scan_callback:
                    self.is_scanning = True
                    await self._send_reply("🚀 *Запуск немедленного сканирования рынка Nado DEX по запросу...*")
                    
                    async def wrapped_scan():
                        try:
                            await self.trigger_scan_callback()
                        finally:
                            self.is_scanning = False
                            
                    task = asyncio.create_task(wrapped_scan())
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                    print(f"🔔 [TelegramListener] Задача сканирования создана: {task.get_name()}")
                else:
                    print(f"❌ [TelegramListener] trigger_scan_callback НЕ задан!")
                    await self._send_reply("❌ Ошибка: callback сканирования не настроен.")

            elif cmd in ["/help", "/start", "/menu"]:
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "📊 Баланс и PnL", "callback_data": "cmd_balance"}, {"text": "💼 Позиции", "callback_data": "cmd_positions"}],
                        [{"text": "⚙️ Профиль риска", "callback_data": "cmd_risk"}, {"text": "ℹ️ Статус", "callback_data": "cmd_status"}],
                        [{"text": "🚀 Принудительный скан", "callback_data": "cmd_scan"}]
                    ]
                }
                reply = (
                    f"🤖 *ГЛАВНОЕ МЕНЮ БОТА NADO DEX*\n\n"
                    f"Выберите действие с помощью кнопок ниже или используйте текстовые команды:\n\n"
                    f"🔹 `/status` — Узнать текущий режим и статус\n"
                    f"🔹 `/balance` (или `/pnl`) — Статистика побед и текущий баланс\n"
                    f"🔹 `/positions` — Список активных сделок и управление ими\n"
                    f"🔹 `/risk` — Переключение профиля риска\n"
                    f"🔹 `/scan` — Принудительно начать цикл сканирования\n"
                    f"🔹 `/deposit <сумма>` — Учесть ручное пополнение для точного ROI\n"
                    f"🔹 `/withdraw <сумма>` — Учесть ручной вывод для точного ROI\n"
                    f"🔹 `/reset_ledger` — Сбросить статистику PnL и начальный капитал\n"
                    f"🔹 `/help` — Это меню"
                )
                await self._send_reply(reply, reply_markup=reply_markup)
        except Exception as e:
            print(f"❌ [TelegramListener] Ошибка обработки команды '{cmd}': {type(e).__name__}: {e}")
            traceback.print_exc()

    async def handle_callback(self, callback_id: str, callback_data: str, chat_id: str):
        print(f"🔘 [TelegramListener] Нажата кнопка: {callback_data}")
        from services.telegram_service import TelegramService
        tg = TelegramService()
        
        if callback_data.startswith("approve_") or callback_data.startswith("reject_"):
            action, trade_id = callback_data.split("_")
            if hasattr(self.paper_trading, "pending_trades") and trade_id in self.paper_trading.pending_trades:
                trade = self.paper_trading.pending_trades.pop(trade_id)
                
                # Timeout check (5 minutes = 300 seconds)
                import time
                if time.time() - trade.get("created_at", 0) > 300:
                    await tg.answer_callback_query(callback_id, "Сделка просрочена (>5 мин) ⏳")
                    print(f"⏳ [TelegramListener] Сделка {trade['symbol']} отклонена по таймауту (>5 мин).")
                    await tg.send_message(f"⏳ Сделка по {trade['symbol']} устарела (>5 минут) и была автоматически отменена.")
                    return

                if action == "approve":
                    await tg.answer_callback_query(callback_id, "Сделка ОДОБРЕНА ✅")
                    print(f"✅ [TelegramListener] Пользователь вручную ОДОБРИЛ сделку {trade['symbol']}")
                    
                    tg_msg = trade.pop("tg_message", None)
                    trade.pop("created_at", None)
                    await self.paper_trading.open_position(**trade)
                    
                    if tg_msg:
                        await tg.broadcast_to_channel(tg_msg)
                        
                    await tg.send_message(f"✅ Одобрено: Сделка по {trade['symbol']} открыта на бирже.")
                else:
                    await tg.answer_callback_query(callback_id, "Сделка отклонена ❌")
                    print(f"❌ [TelegramListener] Пользователь отклонил сделку {trade['symbol']}")
                    await tg.send_message(f"❌ Отмена: Сделка по {trade['symbol']} отклонена пользователем.")
            else:
                await tg.answer_callback_query(callback_id, "Ошибка: Сделка не найдена или устарела.")
                
        elif callback_data.startswith("forceclose_"):
            symbol = callback_data.split("_")[1]
            await tg.answer_callback_query(callback_id, f"Закрываю {symbol}... ⏳")
            
            success, result = await self.paper_trading.force_close_position(symbol)
            if success:
                mode = "👻 [ВИРТУАЛЬНО]" if result.get('is_virtual') else "⚡ [БОЕВАЯ]"
                pnl = result.get('pnl_usd', 0)
                roi = result.get('roi_pct', 0)
                exit_price = result.get('exit_price', 0)
                emoji = "🎉" if pnl >= 0 else "🔻"
                msg = f"{emoji} {mode} Сделка по {symbol} ЗАКРЫТА ВРУЧНУЮ!\n💰 PnL: `${pnl:+.2f}` (ROI: {roi:+.2f}%)\n🎯 Выход: `${exit_price:,.2f}`"
                await tg.send_message(msg)
                await tg.broadcast_to_channel(msg)
            else:
                await tg.send_message(f"❌ Ошибка закрытия {symbol}: {result}")
                
        elif callback_data.startswith("setrisk_"):
            new_profile = callback_data.split("_")[1]
            os.environ["TRADING_PROFILE"] = new_profile
            await tg.answer_callback_query(callback_id, f"Профиль {new_profile} установлен ✅")
            await tg.send_message(f"✅ Профиль риска успешно изменен на `{new_profile}`.")
            
        elif callback_data.startswith("cmd_"):
            await tg.answer_callback_query(callback_id, "Загрузка... ⏳")
            if callback_data == "cmd_balance": await self.handle_command("/balance")
            elif callback_data == "cmd_positions": await self.handle_command("/positions")
            elif callback_data == "cmd_risk": await self.handle_command("/risk")
            elif callback_data == "cmd_status": await self.handle_command("/status")
            elif callback_data == "cmd_scan": await self.handle_command("/scan")

    async def start_listening(self):
        if not self.api_url or not self.chat_id:
            print("❌ [TelegramListener] НЕ ЗАПУЩЕН: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы в .env!")
            return
        
        if self.bot_token and "your_telegram" in self.bot_token:
            print("❌ [TelegramListener] НЕ ЗАПУЩЕН: TELEGRAM_BOT_TOKEN содержит placeholder 'your_telegram'!")
            return

        self.running = True
        print("📲 [TelegramListener] Фоновый слушатель команд Telegram (/status, /pnl, /scan) запущен.")
        print(f"📲 [TelegramListener] Chat ID: {self.chat_id} | Polling timeout: 30s")

        while self.running:
            try:
                url = f"{self.api_url}?offset={self.offset}&timeout=30"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            results = data.get("result", [])
                            for result in results:
                                self.offset = result["update_id"] + 1
                                
                                if "callback_query" in result:
                                    callback_query = result["callback_query"]
                                    cb_data = callback_query.get("data")
                                    cb_id = callback_query.get("id")
                                    cb_chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
                                    
                                    if cb_chat_id == str(self.chat_id):
                                        await self.handle_callback(cb_id, cb_data, cb_chat_id)
                                    else:
                                        print(f"⚠️ [TelegramListener] Кнопка от чужого чата {cb_chat_id}")
                                
                                elif "message" in result:
                                    message = result.get("message", {})
                                    text = message.get("text", "")
                                    sender_chat_id = str(message.get("chat", {}).get("id", ""))
                                    
                                    if text.startswith("/"):
                                        print(f"📨 [TelegramListener] Входящая команда: '{text}' от chat_id={sender_chat_id} (ожидается: {self.chat_id})")
                                        if sender_chat_id == str(self.chat_id):
                                            await self.handle_command(text)
                                        else:
                                            print(f"⚠️ [TelegramListener] Команда '{text}' от ЧУЖОГО чата {sender_chat_id}, игнорируем.")
                        else:
                            error_body = await resp.text()
                            print(f"❌ [TelegramListener] Ошибка polling (HTTP {resp.status}): {error_body[:300]}")
                            await asyncio.sleep(5)
            except asyncio.CancelledError:
                print("🛑 [TelegramListener] Слушатель остановлен (CancelledError).")
                break
            except asyncio.TimeoutError:
                # Нормальный таймаут long-polling, просто повторяем
                continue
            except Exception as e:
                print(f"❌ [TelegramListener] Ошибка в цикле polling: {type(e).__name__}: {e}")
                traceback.print_exc()
                await asyncio.sleep(5)
