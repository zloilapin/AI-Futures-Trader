import os
import aiohttp
import asyncio
from typing import Optional
from core.session import SessionManager

class TelegramService:
    """
    Asynchronous service to send messages to a Telegram chat.
    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
    """
    def __init__(self):
        # Берем ключи из переменных окружения (безопасный подход)
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.public_channel_id = os.getenv("PUBLIC_CHANNEL_ID")
        
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage" if self.bot_token else None

    MAX_MSG_LEN = 4000

    @staticmethod
    def _is_valid_credential(val: Optional[str]) -> bool:
        if not val or not isinstance(val, str):
            return False
        clean = val.strip().lower()
        return bool(clean and "your_" not in clean and "placeholder" not in clean)

    @classmethod
    def _safe_truncate(cls, text: str) -> str:
        if not text:
            return ""
        if len(text) > cls.MAX_MSG_LEN:
            return text[:cls.MAX_MSG_LEN - 20] + "\n...[TRUNCATED]"
        return text

    async def send_message(self, text: str, parse_mode: str = "Markdown", reply_markup: dict = None) -> bool:
        """
        Sends a text message to the configured Telegram chat.
        Includes automatic fallback to plain text if Markdown parsing fails.
        Safely enforces Telegram's 4096 character limit.
        """
        if not self._is_valid_credential(self.bot_token) or not self._is_valid_credential(self.chat_id):
            print("⚠️ [TelegramService] Токен или ID чата не настроены в .env! Сообщение выведено только в консоль.")
            return False

        safe_text = self._safe_truncate(text)
        payload = {
            "chat_id": self.chat_id,
            "text": safe_text,
            "disable_web_page_preview": True
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            session = await SessionManager.get()
            async with session.post(self.api_url, json=payload) as response:
                if response.status == 200:
                    print("✅ [TelegramService] Уведомление успешно доставлено в Telegram!")
                    return True
                elif response.status == 429:
                    # Rate limited: wait and retry once
                    retry_after = 1.0
                    try:
                        resp_data = await response.json()
                        retry_after = float(resp_data.get("parameters", {}).get("retry_after", 1.0))
                    except Exception:
                        pass
                    await asyncio.sleep(retry_after)
                    async with session.post(self.api_url, json=payload) as retry_res:
                        return retry_res.status == 200
                elif response.status == 400:
                    err = await response.text()
                    is_parse_err = any(kw in err.lower() for kw in ["parse", "entity", "markdown", "can't parse"])
                    if is_parse_err and payload.get("parse_mode"):
                        print("⚠️ [TelegramService] Ошибка парсинга Markdown entities, повторная отправка без форматирования...")
                        fallback_payload = dict(payload)
                        fallback_payload.pop("parse_mode", None)
                        async with session.post(self.api_url, json=fallback_payload) as fallback_res:
                            if fallback_res.status == 200:
                                print("✅ [TelegramService] Уведомление доставлено без форматирования.")
                                return True
                            else:
                                err2 = await fallback_res.text()
                                print(f"❌ [TelegramService] Ошибка отправки (fallback): HTTP {fallback_res.status} - {err2}")
                                return False
                    else:
                        print(f"❌ [TelegramService] Ошибка запроса HTTP 400 (не связанная с Markdown): {err}")
                        return False
                else:
                    error_data = await response.text()
                    print(f"❌ [TelegramService] Ошибка отправки: HTTP {response.status} - {error_data}")
                    return False
        except Exception as e:
            print(f"❌ [TelegramService] Критическая ошибка при отправке в Telegram: {e}")
            return False

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        """
        Answers a callback query to stop the loading spinner on Telegram buttons.
        """
        if not self._is_valid_credential(self.bot_token) or not callback_query_id:
            return False
            
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload = {
            "callback_query_id": str(callback_query_id),
            "text": text
        }
        try:
            session = await SessionManager.get()
            async with session.post(url, json=payload) as response:
                return response.status == 200
        except Exception as e:
            print(f"❌ [TelegramService] Ошибка answerCallbackQuery: {e}")
            return False

    async def broadcast_to_channel(self, text: str, parse_mode: str = "Markdown") -> bool:
        """
        Отправляет сообщение в публичный канал, если он настроен.
        """
        if not self._is_valid_credential(self.bot_token) or not self._is_valid_credential(self.public_channel_id):
            print("ℹ️ [TelegramService] PUBLIC_CHANNEL_ID не настроен. Пропуск трансляции.")
            return False

        safe_text = self._safe_truncate(text)
        payload = {
            "chat_id": self.public_channel_id,
            "text": safe_text,
            "disable_web_page_preview": True
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            session = await SessionManager.get()
            async with session.post(self.api_url, json=payload) as response:
                if response.status == 200:
                    print("📢 [TelegramService] Сообщение успешно отправлено в публичный канал!")
                    return True
                elif response.status == 429:
                    retry_after = 1.0
                    try:
                        resp_data = await response.json()
                        retry_after = float(resp_data.get("parameters", {}).get("retry_after", 1.0))
                    except Exception:
                        pass
                    await asyncio.sleep(retry_after)
                    async with session.post(self.api_url, json=payload) as retry_res:
                        return retry_res.status == 200
                elif response.status == 400:
                    err = await response.text()
                    is_parse_err = any(kw in err.lower() for kw in ["parse", "entity", "markdown", "can't parse"])
                    if is_parse_err and payload.get("parse_mode"):
                        print("⚠️ [TelegramService] Ошибка парсинга Markdown entities в канале, повторная отправка без форматирования...")
                        fallback_payload = dict(payload)
                        fallback_payload.pop("parse_mode", None)
                        async with session.post(self.api_url, json=fallback_payload) as fallback_res:
                            if fallback_res.status == 200:
                                return True
                            else:
                                err2 = await fallback_res.text()
                                print(f"❌ [TelegramService] Ошибка отправки в канал (fallback): HTTP {fallback_res.status} - {err2}")
                                return False
                    else:
                        print(f"❌ [TelegramService] Ошибка запроса в канал HTTP 400 (не Markdown): {err}")
                        return False
                else:
                    err = await response.text()
                    print(f"❌ [TelegramService] Ошибка отправки в канал: HTTP {response.status} - {err}")
                    return False
        except Exception as e:
            print(f"❌ [TelegramService] Ошибка отправки в публичный канал: {e}")
            return False
