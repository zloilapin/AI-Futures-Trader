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
    def __init__(self, paper_trading: PaperTradingService, trigger_scan_callback: Optional[Callable[[], Awaitable[None]]] = None):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.paper_trading = paper_trading
        self.trigger_scan_callback = trigger_scan_callback
        self.offset = 0
        self.running = False
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates" if self.bot_token else None
        self.send_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage" if self.bot_token else None
        self._background_tasks = set()  # Prevent GC of background tasks

    async def _send_reply(self, text: str):
        if not self.send_url or not self.chat_id:
            print(f"⚠️ [TelegramListener] _send_reply: send_url или chat_id не задан! send_url={bool(self.send_url)}, chat_id={bool(self.chat_id)}")
            return
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
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
                
                summary = await self.paper_trading.get_portfolio_summary()
                reply = (
                    f"🤖 *AI TRADER STATUS | NADO DEX*\n\n"
                    f"🟢 *Статус системы:* Активна 24/7\n"
                    f"⏱️ *Интервал сканирования:* каждые {interval} мин\n"
                    f"⚙️ *Профиль риска:* `{profile}`\n"
                    f"🌙 *График отдыха:* с {rest_start} до {rest_end} МСК\n\n"
                    f"💰 *Текущий баланс:* `${summary['current_balance']:,.2f}`\n"
                    f"💼 *Активных позиций:* `{summary['active_positions_count']}`"
                )
                await self._send_reply(reply)

            elif cmd in ["/pnl", "/balance", "/portfolio"]:
                s = await self.paper_trading.get_portfolio_summary()
                reply = (
                    f"📊 *PORTFOLIO & PnL SUMMARY*\n\n"
                    f"💵 *Текущий баланс:* `${s['current_balance']:,.2f}`\n"
                    f"📈 *Общий PnL:* `${s['total_pnl_usd']:+.2f}` ({s['total_pnl_pct']:+.2f}%)\n"
                    f"💼 *Открытых позиций:* `{s['active_positions_count']}`\n"
                    f"🏆 *Винрейт:* `{s['win_rate_pct']}%` (Побед: {s['win_count']} / Потерь: {s['loss_count']})\n"
                    f"🏦 *Начальный депозит:* `${s['initial_balance']:,.2f}`"
                )
                await self._send_reply(reply)

            elif cmd in ["/scan", "/run"]:
                print(f"🔔 [TelegramListener] Обработка команды /scan...")
                await self._send_reply("🚀 *Запуск немедленного сканирования рынка Nado DEX по запросу...*")
                if self.trigger_scan_callback:
                    print(f"🔔 [TelegramListener] trigger_scan_callback найден, запускаем задачу...")
                    task = asyncio.create_task(self.trigger_scan_callback())
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                    print(f"🔔 [TelegramListener] Задача сканирования создана: {task.get_name()}")
                else:
                    print(f"❌ [TelegramListener] trigger_scan_callback НЕ задан!")
                    await self._send_reply("❌ Ошибка: callback сканирования не настроен.")

            elif cmd in ["/help", "/start"]:
                reply = (
                    f"💡 *ДОСТУПНЫЕ КОМАНДЫ БОТА:*\n\n"
                    f"🔹 `/status` — Статус работы, режим и профиль риска\n"
                    f"🔹 `/pnl` или `/balance` — Баланс портфеля, профит и статистика\n"
                    f"🔹 `/scan` — Запустить немедленное сканирование всех монет\n"
                    f"🔹 `/help` — Справка по командам"
                )
                await self._send_reply(reply)
        except Exception as e:
            print(f"❌ [TelegramListener] Ошибка обработки команды '{cmd}': {type(e).__name__}: {e}")
            traceback.print_exc()

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
