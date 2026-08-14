import os
import time
import asyncio
from typing import Optional
from core.exceptions import LLMCircuitBreakerException

class LLMClient:
    """
    Unified LLM Client supporting Groq, Gemini, and OpenRouter APIs.
    Acts as the 'brain' interface for all trading agents.
    """
    def __init__(self, model_name: Optional[str] = None):
        self.groq_key = os.getenv("GROQ_API_KEY")
        self.gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY")
        
        self.kie_key = os.getenv("KIE_API_KEY")
        self.circuit_breaker_until = 0
        self._aiohttp_session = None
        
        if self.gemini_key and ("your_gemini_api_key" in self.gemini_key or self.gemini_key.startswith("AQ.")):
            # Если токен не AIzaSy или заглушка, отдаем приоритет Groq
            pass

        if self.kie_key and not self.kie_key.startswith("your_"):
            self.provider = "kie"
            self.model_name = model_name or os.getenv("KIE_MODEL", "DeepSeek-V3") # Kie default
            print(f"[LLMClient] Инициализирован провайдер Kie.ai ({self.model_name})")
        elif self.openrouter_key and not self.openrouter_key.startswith("your_"):
            self.provider = "openrouter"
            self.model_name = model_name or "meta-llama/llama-3.1-8b-instruct:free"
            print(f"[LLMClient] Инициализирован провайдер OpenRouter ({self.model_name})")
        elif self.groq_key and not self.groq_key.startswith("your_"):
            from groq import AsyncGroq
            self.provider = "groq"
            self.model_name = model_name or "llama-3.1-8b-instant"
            self.client = AsyncGroq(api_key=self.groq_key, timeout=45.0)
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
            print("⚠️ [LLMClient] Валидные ключи API (GROQ, GEMINI, OPENROUTER) не найдены.")

    async def _get_session(self):
        import aiohttp
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession()
        return self._aiohttp_session

    async def generate(self, prompt: str, max_retries: int = 5) -> str:
        """
        Sends the fully formatted prompt to the LLM and retrieves JSON response.
        """
        if time.time() < self.circuit_breaker_until:
            remaining = int((self.circuit_breaker_until - time.time()) / 60)
            print(f"🛑 [LLMClient] Circuit Breaker активен. Пропуск генерации (еще {remaining} мин).")
            raise LLMCircuitBreakerException("LLM is currently unavailable due to repeated failures.")

        # Глобальный троттлинг для бесплатных лимитов (макс 20 запросов в минуту)
        await asyncio.sleep(3)
        
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
                        break
                        
            # Если дошли сюда, значит все попытки исчерпаны
            self.circuit_breaker_until = time.time() + 300 # 5 min cooldown
            raise LLMCircuitBreakerException("Groq API failed after max retries")

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
                        break
            
            self.circuit_breaker_until = time.time() + 300
            raise LLMCircuitBreakerException("Gemini API failed after max retries")

        elif self.provider in ["openrouter", "kie"]:
            is_kie = self.provider == "kie"
            auth_key = self.kie_key if is_kie else self.openrouter_key
            
            headers = {
                "Authorization": f"Bearer {auth_key}",
                "Content-Type": "application/json"
            }
            
            if not hasattr(self, "or_models"):
                self.or_models = [self.model_name]

            for model_to_try in self.or_models:
                api_url = f"https://api.kie.ai/{model_to_try}/v1/chat/completions" if is_kie else "https://openrouter.ai/api/v1/chat/completions"
                payload = {
                    "model": model_to_try,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 2500
                }
                for attempt in range(max_retries):
                    try:
                        session = await self._get_session()
                        async with session.post(api_url, headers=headers, json=payload) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if self.model_name != model_to_try and not is_kie:
                                    print(f"✅ [LLMClient OpenRouter] Найдена рабочая модель: {model_to_try}")
                                    self.model_name = model_to_try
                                    # Move working model to front for future calls
                                    self.or_models.remove(model_to_try)
                                    self.or_models.insert(0, model_to_try)
                                return data["choices"][0]["message"]["content"]
                            else:
                                err_msg = await resp.text()
                                if "429" in err_msg or "rate limit" in err_msg.lower() or "500" in err_msg or "503" in err_msg:
                                    wait_time = (attempt + 1) * 5
                                    print(f"⚠️ [LLMClient {self.provider.upper()}] Ошибка {resp.status} для {model_to_try}. Повтор через {wait_time}s...")
                                    await asyncio.sleep(wait_time)
                                else:
                                    print(f"❌ [LLMClient] {model_to_try} недоступна: {err_msg[:100]}...")
                                    break # Try next model on 404/400
                    except Exception as e:
                        print(f"❌ [LLMClient {self.provider.upper()}] Ошибка сети: {e}")
                        break # Try next model
                        
            # Если дошли сюда, значит все модели и попытки исчерпаны
            self.circuit_breaker_until = time.time() + 300
            raise LLMCircuitBreakerException(f"{self.provider.upper()} API failed after max retries")

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
    
