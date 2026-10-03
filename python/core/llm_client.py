import os
import sys
import time
import asyncio
import random
from typing import Optional
from core.exceptions import LLMCircuitBreakerException

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

class LLMClient:
    """
    Unified LLM Client supporting Groq, Gemini, and OpenRouter APIs.
    Acts as the 'brain' interface for all trading agents.
    """
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    cycle_tokens = 0

    @classmethod
    def reset_cycle_tokens(cls):
        cls.cycle_tokens = 0

    @classmethod
    def get_token_stats(cls):
        return {
            "cycle_tokens": cls.cycle_tokens,
            "total_tokens": cls.total_tokens,
            "total_prompt_tokens": cls.total_prompt_tokens,
            "total_completion_tokens": cls.total_completion_tokens
        }

    def __init__(self, provider: Optional[str] = None, model_name: Optional[str] = None):
        self.groq_key = os.getenv("GROQ_API_KEY")
        self.gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY")
        self.cerebras_key = os.getenv("CEREBRAS_API_KEY")
        self.kie_key = os.getenv("KIE_API_KEY")
        self.circuit_breaker_until = 0
        self._aiohttp_session = None
        
        self.client = None
        
        # Build dynamic fallback queue
        self._available_providers = []
        
        # If an explicit provider is requested, put it first in the queue
        if provider == "openrouter" and self.openrouter_key and not self.openrouter_key.startswith("your_"):
            self._available_providers.append({"provider": "openrouter", "model": model_name or os.getenv("OPENROUTER_MODEL", "poolside/laguna-s-2.1:free")})
            # Fallback на модели из конфигурации
            fallback_free_models = [
                os.getenv("OPENROUTER_MODEL_CEO", "qwen/qwen3.8-flash"),
                os.getenv("OPENROUTER_MODEL_CHEAP", "deepseek/deepseek-v4.1-flash"),
                os.getenv("OPENROUTER_MODEL_ESCALATION", "z-ai/glm-5.3")
            ]
            for fb_m in fallback_free_models:
                if fb_m != (model_name or ""):
                    self._available_providers.append({"provider": "openrouter", "model": fb_m})
        elif provider == "groq" and self.groq_key and not self.groq_key.startswith("your_"):
            self._available_providers.append({"provider": "groq", "model": model_name or "llama-3.1-70b-versatile"})
            
        # Add remaining fallback providers only if provider was not explicitly restricted to openrouter
        if provider != "openrouter":
            if self.openrouter_key and not self.openrouter_key.startswith("your_"):
                self._available_providers.append({"provider": "openrouter", "model": os.getenv("OPENROUTER_MODEL", "poolside/laguna-s-2.1:free")})
            if provider != "groq" and self.groq_key and not self.groq_key.startswith("your_"):
                self._available_providers.append({"provider": "groq", "model": "llama-3.1-70b-versatile"})
            if self.gemini_key and not self.gemini_key.startswith("your_"):
                self._available_providers.append({"provider": "gemini", "model": "gemini-2.0-flash"})
            if self.cerebras_key and not self.cerebras_key.startswith("your_"):
                self._available_providers.append({"provider": "cerebras", "model": "gpt-oss-120b"})
            if self.kie_key and not self.kie_key.startswith("your_"):
                self._available_providers.append({"provider": "kie", "model": os.getenv("KIE_MODEL", "DeepSeek-V3")})
            
        if self._available_providers:
            self._set_provider(self._available_providers.pop(0))
        else:
            self.provider = "none"
            self.model_name = "none"
            print("⚠️ [LLMClient] Валидные ключи API не найдены.")

    def _set_provider(self, provider_dict):
        self.provider = provider_dict["provider"]
        self.model_name = provider_dict["model"]
        self.or_models = [self.model_name]
        if self.provider == "openrouter":
            models_chain = [self.model_name]
            if "qwen" in self.model_name:
                for alt in ["qwen/qwen-2.5-72b-instruct", "qwen/qwen-plus", "qwen/qwen-turbo"]:
                    if alt not in models_chain:
                        models_chain.append(alt)
            elif "glm" in self.model_name:
                for alt in ["z-ai/glm-4-9b-chat"]:
                    if alt not in models_chain:
                        models_chain.append(alt)
            elif "deepseek" in self.model_name:
                for alt in ["deepseek/deepseek-chat"]:
                    if alt not in models_chain:
                        models_chain.append(alt)
            self.or_models = models_chain
        print(f"[LLMClient] Инициализирован провайдер {self.provider.capitalize()} ({self.model_name})")
        if self.provider == "groq":
            try:
                from groq import AsyncGroq
                self.client = AsyncGroq(api_key=self.groq_key, timeout=45.0)
            except Exception as e:
                print(f"❌ [LLMClient] Не удалось инициализировать Groq client: {e}")
                self.client = None
        elif self.provider == "gemini":
            try:
                from google import genai
                os.environ["GOOGLE_API_KEY"] = self.gemini_key
                self.client = genai.Client(api_key=self.gemini_key)
            except Exception as e:
                print(f"❌ [LLMClient] Не удалось инициализировать Gemini client: {e}")
                self.client = None

    async def _get_session(self):
        import aiohttp
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession()
        return self._aiohttp_session

    def _switch_provider(self) -> bool:
        """Switch to the next available provider if current one fails completely."""
        if self._available_providers:
            next_p = self._available_providers.pop(0)
            print(f"🔄 [LLMClient] Переключение провайдера на {next_p['provider'].capitalize()}...")
            self._set_provider(next_p)
            return True
        return False

    async def generate(self, prompt: str, max_retries: int = 5) -> str:
        """
        Sends the fully formatted prompt to the LLM and retrieves JSON response.
        """
        if time.time() < self.circuit_breaker_until:
            remaining = int((self.circuit_breaker_until - time.time()) / 60)
            print(f"🛑 [LLMClient] Circuit Breaker активен. Пропуск генерации (еще {remaining} мин).")
            raise LLMCircuitBreakerException("LLM is currently unavailable due to repeated failures.")

        current_prompt = prompt

        if self.provider == "groq":
            if not getattr(self, "client", None):
                print("⚠️ [LLMClient Groq] Клиент Groq не инициализирован. Переключение...")
                if self._switch_provider():
                    return await self.generate(prompt, max_retries)
                raise LLMCircuitBreakerException("Groq client is not available")
            for attempt in range(max_retries):
                try:
                    response = await self.client.chat.completions.create(
                        messages=[{"role": "user", "content": current_prompt}],
                        model=self.model_name,
                        response_format={"type": "json_object"}
                    )
                    if hasattr(response, "usage") and response.usage:
                        p_tok = getattr(response.usage, "prompt_tokens", 0) or 0
                        c_tok = getattr(response.usage, "completion_tokens", 0) or 0
                        t_tok = getattr(response.usage, "total_tokens", p_tok + c_tok) or 0
                        LLMClient.total_prompt_tokens += p_tok
                        LLMClient.total_completion_tokens += c_tok
                        LLMClient.total_tokens += t_tok
                        LLMClient.cycle_tokens += t_tok
                        if t_tok > 0:
                            print(f"🪙 [Tokens Groq] {self.model_name}: in={p_tok}, out={c_tok} | Запрос: {t_tok} | В цикле: {LLMClient.cycle_tokens:,}")
                    return response.choices[0].message.content
                except Exception as e:
                    err_msg = str(e)
                    print(f"⚠️ [LLMClient Groq] Исходная ошибка от API: {err_msg}")
                    if "429" in err_msg or "rate_limit" in err_msg or "time out" in err_msg.lower():
                        wait_time = min(60, (2 ** attempt) + random.uniform(0, 1))
                        print(f"⚠️ [LLMClient Groq] Лимит частоты (429). Повтор через {wait_time:.2f}s... (Попытка {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    elif "400" in err_msg or "json_validate_failed" in err_msg.lower():
                        current_prompt += "\n\n[SYSTEM WARNING: Your previous response failed JSON validation. Please strictly output a valid JSON object without markdown formatting.]"
                        wait_time = 2
                        print(f"⚠️ [LLMClient Groq] Ошибка валидации JSON (400). Self-Correction промпта... Повтор через {wait_time}s... (Попытка {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        print(f"❌ [LLMClient Groq] Ошибка при запросе: {err_msg}")
                        break
                        
            if self._switch_provider():
                return await self.generate(prompt, max_retries)
                
            self.circuit_breaker_until = time.time() + 300 # 5 min cooldown
            raise LLMCircuitBreakerException("All API providers failed after max retries")

        elif self.provider == "gemini":
            from google.genai import types
            for attempt in range(max_retries):
                try:
                    response = await self.client.aio.models.generate_content(
                        model=self.model_name,
                        contents=current_prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json"
                        )
                    )
                    return response.text
                except Exception as e:
                    err_msg = str(e)
                    if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                        wait_time = min(60, (2 ** attempt) + random.uniform(0, 1))
                        print(f"⚠️ [LLMClient Gemini] Лимит частоты (429). Повтор через {wait_time:.2f}s... (Попытка {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        print(f"❌ [LLMClient Gemini] Ошибка при запросе: {e}")
                        break
            
            if self._switch_provider():
                return await self.generate(prompt, max_retries)

            self.circuit_breaker_until = time.time() + 300
            raise LLMCircuitBreakerException("All API providers failed after max retries")

        elif self.provider in ["openrouter", "kie", "cerebras"]:
            is_kie = self.provider == "kie"
            is_cerebras = self.provider == "cerebras"
            if is_kie:
                auth_key = self.kie_key
            elif is_cerebras:
                auth_key = self.cerebras_key
            else:
                auth_key = str(self.openrouter_key or "").strip()
            
            headers = {
                "Authorization": f"Bearer {auth_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/zloilapin/AI-Futures-Trader",
                "X-Title": "AI Futures Trader"
            }
            
            if not hasattr(self, "or_models"):
                self.or_models = [self.model_name]

            for model_to_try in self.or_models:
                if is_kie:
                    api_url = f"https://api.kie.ai/{model_to_try}/v1/chat/completions"
                elif is_cerebras:
                    api_url = "https://api.cerebras.ai/v1/chat/completions"
                else:
                    api_url = "https://openrouter.ai/api/v1/chat/completions"
                    
                for attempt in range(max_retries):
                    system_instruction = (
                        "You are an automated quantitative trading AI. "
                        "You MUST respond ONLY with a single valid JSON object strictly conforming to the requested schema. "
                        "Never output chain-of-thought monologue, conversational text, or explanations outside the JSON. "
                        "Your response MUST start with '{' and end with '}'."
                    )
                    messages = [
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": current_prompt}
                    ]
                    payload = {
                        "model": model_to_try if not is_cerebras else self.model_name,
                        "messages": messages,
                        "max_tokens": 3500
                    }
                    
                    # OpenRouter reasoning effort control: use 'low' effort so reasoning is brief and budget is left for output
                    if not is_cerebras and not is_kie:
                        if not hasattr(self, "_no_effort_models"):
                            self._no_effort_models = set()
                        if model_to_try not in self._no_effort_models:
                            if any(k in model_to_try.lower() for k in ["glm", "qwen", "deepseek"]):
                                payload["reasoning"] = {"effort": "low"}
                    
                    # OpenRouter: Some models (like Moonshot Kimi) fail with 400 if response_format is provided
                    is_known_no_json = "moonshot" in model_to_try.lower() or "kimi" in model_to_try.lower()
                    if hasattr(self, "_no_json_format_models") and model_to_try in self._no_json_format_models:
                        is_known_no_json = True
                        
                    if not is_known_no_json:
                        payload["response_format"] = {"type": "json_object"}
                    try:
                        import aiohttp
                        session = await self._get_session()
                        async with session.post(api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if self.model_name != model_to_try and not is_kie:
                                    print(f"✅ [LLMClient OpenRouter] Найдена рабочая модель: {model_to_try}")
                                    self.model_name = model_to_try
                                    if hasattr(self, "or_models") and model_to_try in self.or_models:
                                        self.or_models.remove(model_to_try)
                                        self.or_models.insert(0, model_to_try)
                                
                                usage = data.get("usage", {})
                                p_tok = int(usage.get("prompt_tokens", 0) or 0)
                                c_tok = int(usage.get("completion_tokens", 0) or 0)
                                t_tok = int(usage.get("total_tokens", p_tok + c_tok) or 0)
                                LLMClient.total_prompt_tokens += p_tok
                                LLMClient.total_completion_tokens += c_tok
                                LLMClient.total_tokens += t_tok
                                LLMClient.cycle_tokens += t_tok
                                if t_tok > 0:
                                    print(f"🪙 [Tokens OpenRouter] {model_to_try}: in={p_tok}, out={c_tok} | Запрос: {t_tok} | В цикле: {LLMClient.cycle_tokens:,}")

                                choice = data["choices"][0]
                                message = choice.get("message", {})
                                content = message.get("content") or ""
                                reasoning = message.get("reasoning") or ""
                                
                                # If content doesn't contain a JSON object, but reasoning does, extract it
                                if "{" not in content and "{" in reasoning:
                                    content = reasoning
                                elif not content and reasoning:
                                    content = reasoning

                                # Degenerated output protection (e.g. float number or raw numbers instead of JSON)
                                if "{" not in content:
                                    print(f"⚠️ [LLMClient OpenRouter] {model_to_try} вернул ответ без JSON ('{content[:40]}'). Переключение на резервную модель...")
                                    break

                                return content
                            else:
                                err_msg = await resp.text()
                                if resp.status == 401 or "401" in err_msg or "user not found" in err_msg.lower():
                                    print(f"❌ [LLMClient OpenRouter] Ошибка 401 (User not found). Ключ недействителен на OpenRouter!")
                                    break
                                elif resp.status == 402 or "402" in err_msg or "payment" in err_msg.lower():
                                    print(f"❌ [LLMClient {self.provider.upper()}] Ошибка 402 (Payment Required). Закончились кредиты. Переключаемся на другого провайдера...")
                                    break
                                elif "429" in err_msg or "rate limit" in err_msg.lower() or "500" in err_msg or "503" in err_msg:
                                    wait_time = min(60, (2 ** attempt) + random.uniform(0, 1))
                                    print(f"⚠️ [LLMClient {self.provider.upper()}] Ошибка {resp.status} для {model_to_try}. Повтор через {wait_time:.2f}s...")
                                    await asyncio.sleep(wait_time)
                                elif "400" in err_msg:
                                    if any(term in err_msg.lower() for term in ["not found", "does not exist", "no endpoints", "invalid model"]):
                                        print(f"❌ [LLMClient {self.provider.upper()}] Модель {model_to_try} не найдена или недоступна на OpenRouter: {err_msg[:120]}...")
                                        break
                                    current_prompt += "\n\n[SYSTEM WARNING: Your previous response failed JSON validation. Please strictly output a valid JSON object without markdown formatting.]"
                                    if not hasattr(self, "_no_json_format_models"):
                                        self._no_json_format_models = set()
                                    self._no_json_format_models.add(model_to_try)
                                    print(f"⚠️ [LLMClient {self.provider.upper()}] Ошибка 400 (возможно не поддерживается JSON mode). Self-Correction промпта и отключение json_object...")
                                    await asyncio.sleep(2)
                                else:
                                    print(f"❌ [LLMClient] {model_to_try} недоступна: {err_msg[:100]}...")
                                    break 
                    except Exception as e:
                        err_str = str(e) or "Таймаут соединения"
                        wait_time = min(60, (2 ** attempt) + random.uniform(0, 1))
                        print(f"⚠️ [LLMClient {self.provider.upper()}] Ошибка сети ({err_str}). Повтор через {wait_time:.2f}s...")
                        await asyncio.sleep(wait_time)
                        
            if self._switch_provider():
                return await self.generate(prompt, max_retries)

            self.circuit_breaker_until = time.time() + 300
            raise LLMCircuitBreakerException("All API providers failed after max retries")

        return "{}"

    async def close(self):
        """Закрытие всех асинхронных сессий клиента"""
        if self._aiohttp_session and not self._aiohttp_session.closed:
            await self._aiohttp_session.close()
            
        if self.provider == "groq" and hasattr(self, "client") and hasattr(self.client, "close"):
            await self.client.close()
            
        print("🧹 [LLMClient] Ресурсы LLM клиента успешно очищены.")

if __name__ == "__main__":
    async def test_client():
        client = LLMClient()
        test_prompt = 'You are a crypto assistant. Return JSON with "signal": "BULLISH" and "reason": "test".'
        print("Отправка тестового запроса...")
        res = await client.generate(test_prompt)
        print(f"Ответ: {res}")
        await client.close()

    asyncio.run(test_client())
