import os
import asyncio
from typing import Optional

class LLMClient:
    """
    Unified LLM Client supporting Groq, Gemini, and OpenRouter APIs.
    Acts as the 'brain' interface for all trading agents.
    """
    def __init__(self, model_name: Optional[str] = None):
        self.groq_key = os.getenv("GROQ_API_KEY")
        self.gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if self.gemini_key and ("your_gemini_api_key" in self.gemini_key or self.gemini_key.startswith("AQ.")):
            # Если токен не AIzaSy или заглушка, отдаем приоритет Groq
            pass

        if self.groq_key and not self.groq_key.startswith("your_"):
            from groq import AsyncGroq
            self.provider = "groq"
            self.model_name = model_name or "llama-3.3-70b-versatile"
            self.client = AsyncGroq(api_key=self.groq_key, timeout=15.0)
            print(f"[LLMClient] Инициализирован провайдер Groq ({self.model_name})")
        elif self.gemini_key and not self.gemini_key.startswith("your_"):
            from google import genai
            self.provider = "gemini"
            self.model_name = model_name or "gemini-2.0-flash"
            os.environ["GOOGLE_API_KEY"] = self.gemini_key
            self.client = genai.Client(api_key=self.gemini_key)
            print(f"[LLMClient] Инициализирован провайдер Gemini ({self.model_name})")
        else:
            self.provider = "none"
            print("⚠️ [LLMClient] Валидные ключи API (GROQ_API_KEY / GEMINI_API_KEY) не найдены.")

    async def generate(self, prompt: str, max_retries: int = 5) -> str:
        """
        Sends the fully formatted prompt to the LLM and retrieves JSON response.
        """
        if self.provider == "groq":
            for attempt in range(max_retries):
                try:
                    response = await self.client.chat.completions.create(
                        messages=[{"role": "user", "content": prompt}],
                        model=self.model_name,
                        response_format={"type": "json_object"}
                    )
                    return response.choices[0].message.content
                except Exception as e:
                    err_msg = str(e)
                    if "429" in err_msg or "rate_limit" in err_msg or "time out" in err_msg.lower():
                        wait_time = (attempt + 1) * 8
                        print(f"⚠️ [LLMClient Groq] Лимит частоты (429). Повтор через {wait_time}s... (Попытка {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        print(f"❌ [LLMClient Groq] Ошибка при запросе: {e}")
                        return "{}"
            return "{}"

        elif self.provider == "gemini":
            from google.genai import types
            for attempt in range(max_retries):
                try:
                    response = await self.client.aio.models.generate_content(
                        model=self.model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json"
                        )
                    )
                    return response.text
                except Exception as e:
                    err_msg = str(e)
                    if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                        wait_time = (attempt + 1) * 5
                        print(f"⚠️ [LLMClient Gemini] Лимит частоты (429). Повтор через {wait_time}s... (Попытка {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        print(f"❌ [LLMClient Gemini] Ошибка при запросе: {e}")
                        return "{}"
            return "{}"

        return "{}"

# Пример для локального тестирования
if __name__ == "__main__":
    async def test_client():
        client = LLMClient()
        test_prompt = 'You are a crypto assistant. Return JSON with "signal": "BULLISH" and "reason": "test".'
        print("Отправка тестового запроса...")
        res = await client.generate(test_prompt)
        print(f"Ответ: {res}")

    asyncio.run(test_client())
    
