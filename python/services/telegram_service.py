import os
import aiohttp
import asyncio
from typing import Optional

class TelegramService:
    """
    Asynchronous service to send messages to a Telegram chat.
    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
    """
    def __init__(self):
        # Берем ключи из переменных окружения (безопасный подход)
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage" if self.bot_token else None

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """
        Sends a text message to the configured Telegram chat.
        Includes automatic fallback to plain text if Markdown parsing fails.
        """
        if not self.bot_token or not self.chat_id or "your_telegram" in self.bot_token or "your_telegram" in self.chat_id:
            print("⚠️ [TelegramService] Токен или ID чата не настроены в .env! Сообщение выведено только в консоль.")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status == 200:
                        print("✅ [TelegramService] Уведомление успешно доставлено в Telegram!")
                        return True
                    elif response.status == 400:
                        # В случае ошибки форматирования Markdown пробуем отправить без разметки
                        print("⚠️ [TelegramService] Ошибка форматирования Markdown, повторная отправка без форматирования...")
                        payload.pop("parse_mode", None)
                        async with session.post(self.api_url, json=payload) as fallback_res:
                            if fallback_res.status == 200:
                                print("✅ [TelegramService] Уведомление доставлено без форматирования.")
                                return True
                            else:
                                err = await fallback_res.text()
                                print(f"❌ [TelegramService] Ошибка отправки: HTTP {fallback_res.status} - {err}")
                                return False
                    else:
                        error_data = await response.text()
                        print(f"❌ [TelegramService] Ошибка отправки: HTTP {response.status} - {error_data}")
                        return False
        except Exception as e:
            print(f"❌ [TelegramService] Критическая ошибка при отправке в Telegram: {e}")
            return False

