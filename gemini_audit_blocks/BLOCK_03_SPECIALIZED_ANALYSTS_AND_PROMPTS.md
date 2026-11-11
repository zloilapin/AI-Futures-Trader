# BLOCK 03: SPECIALIZED ANALYSTS & DEBATE AGENTS
> **Назначение:** Досье для стороннего глубокого аудита кодовой базы моделью Gemini Argon / сторонним ИИ.
> **Дата генерации:** 2026-11-10 (Актуальное боевое состояние репозитория)

## Описание блока
Аналитический слой системы: детерминированные калькуляторы паттернов (свечи, стакан, OI, индикаторы), клиент OpenRouter LLM, парсер JSON и противоборствующие агенты BullAgent / BearAgent.

## Оглавление файлов блока
1. [`python/core/llm_client.py`](#file-python-core-llm_client-py)
2. [`python/core/llm_parser.py`](#file-python-core-llm_parser-py)
3. [`python/agents/base_agent.py`](#file-python-agents-base_agent-py)
4. [`python/agents/universe_agent.py`](#file-python-agents-universe_agent-py)
5. [`python/agents/scanner_agent.py`](#file-python-agents-scanner_agent-py)
6. [`python/agents/candle_agent.py`](#file-python-agents-candle_agent-py)
7. [`python/agents/indicator_agent.py`](#file-python-agents-indicator_agent-py)
8. [`python/agents/order_book_agent.py`](#file-python-agents-order_book_agent-py)
9. [`python/agents/oi_funding_agent.py`](#file-python-agents-oi_funding_agent-py)
10. [`python/agents/news_agent.py`](#file-python-agents-news_agent-py)
11. [`python/agents/bull_agent.py`](#file-python-agents-bull_agent-py)
12. [`python/agents/bear_agent.py`](#file-python-agents-bear_agent-py)

---

<a id="file-python-core-llm_client-py"></a>
## File: `python/core/llm_client.py` (414 строк, 23.3 KB)

```python
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
            self._available_providers.append({"provider": "openrouter", "model": model_name or os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")})
            # Fallback на модели из конфигурации
            fallback_free_models = [
                os.getenv("OPENROUTER_MODEL_CEO", "deepseek/deepseek-chat"),
                os.getenv("OPENROUTER_MODEL_CHEAP", "google/gemini-2.5-flash-lite"),
                os.getenv("OPENROUTER_MODEL_ESCALATION", "meta-llama/llama-3.3-70b-instruct")
            ]
            for fb_m in fallback_free_models:
                if fb_m != (model_name or ""):
                    self._available_providers.append({"provider": "openrouter", "model": fb_m})
        elif provider == "groq" and self.groq_key and not self.groq_key.startswith("your_"):
            self._available_providers.append({"provider": "groq", "model": model_name or "llama-3.1-70b-versatile"})
            
        # Add remaining fallback providers only if provider was not explicitly restricted to openrouter
        if provider != "openrouter":
            if self.openrouter_key and not self.openrouter_key.startswith("your_"):
                self._available_providers.append({"provider": "openrouter", "model": os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")})
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
            self._current_provider_dict = None
            print("⚠️ [LLMClient] Валидные ключи API не найдены.")

    def _set_provider(self, provider_dict):
        self._current_provider_dict = dict(provider_dict)
        self.provider = provider_dict["provider"]
        self.model_name = provider_dict["model"]
        self.or_models = [self.model_name]
        if self.provider == "openrouter":
            models_chain = [self.model_name]
            if "qwen" in self.model_name:
                for alt in ["deepseek/deepseek-chat"]:
                    if alt not in models_chain:
                        models_chain.append(alt)
            elif "llama" in self.model_name:
                for alt in ["meta-llama/llama-3.3-70b-instruct"]:
                    if alt not in models_chain:
                        models_chain.append(alt)
            elif "deepseek" in self.model_name:
                for alt in ["deepseek/deepseek-chat"]:
                    if alt not in models_chain:
                        models_chain.append(alt)
            elif "gemini" in self.model_name:
                for alt in ["google/gemini-2.5-flash-lite"]:
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
        if not self._available_providers:
            return False

        current_p = getattr(self, "_current_provider_dict", None)
        if not current_p and self.provider != "none":
            current_p = {"provider": self.provider, "model": self.model_name}

        next_p = self._available_providers.pop(0)
        if current_p and current_p.get("provider") != "none":
            self._available_providers.append(current_p)

        print(f"🔄 [LLMClient] Переключение провайдера на {next_p['provider'].capitalize()}...")
        self._set_provider(next_p)
        return True

    def get_unique_providers_count(self) -> int:
        all_p = []
        curr = getattr(self, "_current_provider_dict", None)
        if curr and curr.get("provider") != "none":
            all_p.append(curr)
        elif self.provider != "none":
            all_p.append({"provider": self.provider, "model": self.model_name})
        all_p.extend(self._available_providers)

        unique = {p.get("provider") for p in all_p if isinstance(p, dict) and p.get("provider") and p.get("provider") != "none"}
        return max(1, len(unique))

    async def generate(self, prompt: str, max_retries: int = 5, switch_count: int = 0) -> str:
        """
        Sends the fully formatted prompt to the LLM and retrieves JSON response.
        """
        if time.time() < self.circuit_breaker_until:
            remaining = int((self.circuit_breaker_until - time.time()) / 60)
            print(f"🛑 [LLMClient] Circuit Breaker активен. Пропуск генерации (еще {remaining} мин).")
            raise LLMCircuitBreakerException("LLM is currently unavailable due to repeated failures.")

        max_switches = self.get_unique_providers_count()
        current_prompt = prompt

        if self.provider == "groq":
            if not getattr(self, "client", None):
                print("⚠️ [LLMClient Groq] Клиент Groq не инициализирован. Переключение...")
                if switch_count < max_switches and self._switch_provider():
                    return await self.generate(prompt, max_retries, switch_count=switch_count + 1)
                raise LLMCircuitBreakerException("Groq client is not available")
            for attempt in range(max_retries):
                try:
                    response = await self.client.chat.completions.create(
                        messages=[{"role": "user", "content": current_prompt}],
                        model=self.model_name,
                        response_format={"type": "json_object"},
                        temperature=0.0
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
                        
            if switch_count < max_switches and self._switch_provider():
                return await self.generate(prompt, max_retries, switch_count=switch_count + 1)
                
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
                            response_mime_type="application/json",
                            temperature=0.0
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
            
            if switch_count < max_switches and self._switch_provider():
                return await self.generate(prompt, max_retries, switch_count=switch_count + 1)

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
                        "max_tokens": 900,
                        "temperature": 0.0
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
                        async with session.post(api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                # F-23 Fix: Transient fallback must NOT permanently downgrade primary model or reorder chain
                                if self.model_name != model_to_try and not is_kie:
                                    print(f"ℹ️ [LLMClient OpenRouter] Временный ответ от резервной модели: {model_to_try} (основная модель: {self.model_name})")
                                
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
                        
            if switch_count < max_switches and self._switch_provider():
                return await self.generate(prompt, max_retries, switch_count=switch_count + 1)

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
```

---

<a id="file-python-core-llm_parser-py"></a>
## File: `python/core/llm_parser.py` (148 строк, 7.3 KB)

```python
import json
import re
from typing import Dict, Any, List

class LLMStructuredOutputParser:
    """
    Unified LLM Structured Output Parser for the AI-Futures-Trader syndicate.
    Extracts JSON from markdown code blocks or raw text, parses it, and validates
    that all required keys are present.
    """
    
    @staticmethod
    def parse_and_validate(response_text: str, required_keys: List[str] = None) -> Dict[str, Any]:
        """
        Parses an LLM string response into a JSON dictionary and validates the schema.
        
        Args:
            response_text (str): The raw text response from the LLM.
            required_keys (List[str], optional): Keys that MUST exist in the output dictionary.
            
        Returns:
            Dict[str, Any]: The parsed and validated dictionary.
            
        Raises:
            ValueError: If the response is not valid JSON, not a dictionary, or missing required keys.
        """
        if not response_text or not str(response_text).strip():
            raise ValueError("Empty response from LLM")
            
        clean_text = str(response_text).strip()
        
        # 1. Поиск блока JSON внутри markdown ограждений (json code blocks)
        matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
        if matches:
            clean_text = matches[-1].strip()
        else:
            # 2. Если ограждений нет, ищем от первой { до последней }
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_text = clean_text[start_idx:end_idx+1]
            elif start_idx != -1:
                # Truncated response recovery: starts with { but missing closing }
                candidate = clean_text[start_idx:]
                last_comma = candidate.rfind(',')
                if last_comma != -1:
                    candidate = candidate[:last_comma]
                if candidate.count('"') % 2 != 0:
                    candidate += '"'
                open_brackets = candidate.count('[') - candidate.count(']')
                open_braces = candidate.count('{') - candidate.count('}')
                candidate += ']' * max(0, open_brackets)
                candidate += '}' * max(0, open_braces)
                clean_text = candidate
            else:
                # 3. Fallback: Parse key-value bullet points or lines (e.g. - decision: "LONG")
                extracted_dict = {}
                for line in clean_text.splitlines():
                    m = re.match(r'^\s*[-*#]?\s*["\']?([a-zA-Z0-9_]+)["\']?\s*[:=]\s*["\']?([^"\'\n\r]+)["\']?', line.strip())
                    if m:
                        k, v = m.group(1).strip().lower(), m.group(2).strip().rstrip(',')
                        if v.lower() == "true":
                            extracted_dict[k] = True
                        elif v.lower() == "false":
                            extracted_dict[k] = False
                        else:
                            try:
                                if "." in v:
                                    extracted_dict[k] = float(v)
                                else:
                                    extracted_dict[k] = int(v)
                            except ValueError:
                                extracted_dict[k] = v.strip('"\'')
                
                # Check if we extracted a meaningful decision/signal
                key_identifiers = ["decision", "signal", "trade_action"]
                if any(ident in extracted_dict for ident in key_identifiers):
                    # NOTE: Synthetic score_breakdown was intentionally removed to avoid falsifying
                    # math verification or masking hallucinations. Agents requiring score_breakdown
                    # must receive it directly from the LLM or fail validation to trigger a retry.
                    if "reasoning_en" not in extracted_dict:
                        extracted_dict["reasoning_en"] = clean_text[:400]
                    parsed = extracted_dict
                    if required_keys:
                        missing_keys = [k for k in required_keys if k not in parsed]
                        if missing_keys:
                            raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                    return parsed

                print(f"\n[DEBUG LLM_PARSER] Failed to find {{...}}. RAW RESPONSE: {repr(clean_text)}\n")
                raise ValueError("No JSON object '{...}' found in the LLM response.")

        # Парсинг с fallback восстановлением
        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError:
            # 1. Попытка безопасного парсинга Python dict через ast.literal_eval (одинарные кавычки, True/False)
            import ast
            try:
                cand = ast.literal_eval(clean_text)
                if isinstance(cand, dict):
                    parsed = cand
                else:
                    raise ValueError()
            except Exception:
                # 2. Восстановление незакрытых скобок и кавычек
                repaired = clean_text.rstrip()
                if repaired.count('"') % 2 != 0:
                    repaired += '"'
                open_brackets = repaired.count('[') - repaired.count(']')
                open_braces = repaired.count('{') - repaired.count('}')
                repaired += ']' * max(0, open_brackets)
                repaired += '}' * max(0, open_braces)
                try:
                    parsed = json.loads(repaired)
                except Exception:
                    try:
                        # 3. Замена одинарных кавычек на двойные
                        parsed = json.loads(repaired.replace("'", '"'))
                    except Exception as e:
                        raise ValueError(f"JSON Parsing Error: {str(e)}")
            
        # Проверка типа
        if isinstance(parsed, list):
            if len(parsed) > 0 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            else:
                raise ValueError("LLM returned a list instead of a JSON object (dict)")
        elif not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed).__name__}")
            
        # Defense-in-depth: Sanitize echoed enum schemas (e.g. if model literally outputs "LONG | SHORT | HOLD")
        for k in ["decision", "trade_action", "winning_argument", "signal"]:
            if k in parsed and isinstance(parsed[k], str) and ("|" in parsed[k] or "/" in parsed[k]):
                if k in ["decision", "trade_action"]:
                    parsed[k] = "HOLD"
                elif k == "signal":
                    parsed[k] = "NEUTRAL"
                elif k == "winning_argument":
                    parsed[k] = "Neither"

        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
```json ... ```)
        matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
        if matches:
            clean_text = matches[-1].strip()
        else:
            # 2. Если ограждений нет, ищем от первой { до последней }
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_text = clean_text[start_idx:end_idx+1]
            elif start_idx != -1:
                # Truncated response recovery: starts with { but missing closing }
                candidate = clean_text[start_idx:]
                last_comma = candidate.rfind(',')
                if last_comma != -1:
                    candidate = candidate[:last_comma]
                if candidate.count('"') % 2 != 0:
                    candidate += '"'
                open_brackets = candidate.count('[') - candidate.count(']')
                open_braces = candidate.count('{') - candidate.count('}')
                candidate += ']' * max(0, open_brackets)
                candidate += '}' * max(0, open_braces)
                clean_text = candidate
            else:
                # 3. Fallback: Parse key-value bullet points or lines (e.g. - decision: "LONG")
                extracted_dict = {}
                for line in clean_text.splitlines():
                    m = re.match(r'^\s*[-*#]?\s*["\']?([a-zA-Z0-9_]+)["\']?\s*[:=]\s*["\']?([^"\'\n\r]+)["\']?', line.strip())
                    if m:
                        k, v = m.group(1).strip().lower(), m.group(2).strip().rstrip(',')
                        if v.lower() == "true":
                            extracted_dict[k] = True
                        elif v.lower() == "false":
                            extracted_dict[k] = False
                        else:
                            try:
                                if "." in v:
                                    extracted_dict[k] = float(v)
                                else:
                                    extracted_dict[k] = int(v)
                            except ValueError:
                                extracted_dict[k] = v.strip('"\'')
                
                # Check if we extracted a meaningful decision/signal
                key_identifiers = ["decision", "signal", "trade_action"]
                if any(ident in extracted_dict for ident in key_identifiers):
                    # NOTE: Synthetic score_breakdown was intentionally removed to avoid falsifying
                    # math verification or masking hallucinations. Agents requiring score_breakdown
                    # must receive it directly from the LLM or fail validation to trigger a retry.
                    if "reasoning_en" not in extracted_dict:
                        extracted_dict["reasoning_en"] = clean_text[:400]
                    parsed = extracted_dict
                    if required_keys:
                        missing_keys = [k for k in required_keys if k not in parsed]
                        if missing_keys:
                            raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                    return parsed

                print(f"\n[DEBUG LLM_PARSER] Failed to find {{...}}. RAW RESPONSE: {repr(clean_text)}\n")
                raise ValueError("No JSON object '{...}' found in the LLM response.")

        # Парсинг с fallback восстановлением
        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError:
            # 1. Попытка безопасного парсинга Python dict через ast.literal_eval (одинарные кавычки, True/False)
            import ast
            try:
                cand = ast.literal_eval(clean_text)
                if isinstance(cand, dict):
                    parsed = cand
                else:
                    raise ValueError()
            except Exception:
                # 2. Восстановление незакрытых скобок и кавычек
                repaired = clean_text.rstrip()
                if repaired.count('"') % 2 != 0:
                    repaired += '"'
                open_brackets = repaired.count('[') - repaired.count(']')
                open_braces = repaired.count('{') - repaired.count('}')
                repaired += ']' * max(0, open_brackets)
                repaired += '}' * max(0, open_braces)
                try:
                    parsed = json.loads(repaired)
                except Exception:
                    try:
                        # 3. Замена одинарных кавычек на двойные
                        parsed = json.loads(repaired.replace("'", '"'))
                    except Exception as e:
                        raise ValueError(f"JSON Parsing Error: {str(e)}")
            
        # Проверка типа
        if isinstance(parsed, list):
            if len(parsed) > 0 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            else:
                raise ValueError("LLM returned a list instead of a JSON object (dict)")
        elif not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed).__name__}")
            
        # Defense-in-depth: Sanitize echoed enum schemas (e.g. if model literally outputs "LONG | SHORT | HOLD")
        for k in ["decision", "trade_action", "winning_argument", "signal"]:
            if k in parsed and isinstance(parsed[k], str) and ("|" in parsed[k] or "/" in parsed[k]):
                if k in ["decision", "trade_action"]:
                    parsed[k] = "HOLD"
                elif k == "signal":
                    parsed[k] = "NEUTRAL"
                elif k == "winning_argument":
                    parsed[k] = "Neither"

        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
```json ... ```)
        matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
        if matches:
            clean_text = matches[-1].strip()
        else:
            # 2. Если ограждений нет, ищем от первой { до последней }
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_text = clean_text[start_idx:end_idx+1]
            elif start_idx != -1:
                # Truncated response recovery: starts with { but missing closing }
                candidate = clean_text[start_idx:]
                last_comma = candidate.rfind(',')
                if last_comma != -1:
                    candidate = candidate[:last_comma]
                if candidate.count('"') % 2 != 0:
                    candidate += '"'
                open_brackets = candidate.count('[') - candidate.count(']')
                open_braces = candidate.count('{') - candidate.count('}')
                candidate += ']' * max(0, open_brackets)
                candidate += '}' * max(0, open_braces)
                clean_text = candidate
            else:
                # 3. Fallback: Parse key-value bullet points or lines (e.g. - decision: "LONG")
                extracted_dict = {}
                for line in clean_text.splitlines():
                    m = re.match(r'^\s*[-*#]?\s*["\']?([a-zA-Z0-9_]+)["\']?\s*[:=]\s*["\']?([^"\'\n\r]+)["\']?', line.strip())
                    if m:
                        k, v = m.group(1).strip().lower(), m.group(2).strip().rstrip(',')
                        if v.lower() == "true":
                            extracted_dict[k] = True
                        elif v.lower() == "false":
                            extracted_dict[k] = False
                        else:
                            try:
                                if "." in v:
                                    extracted_dict[k] = float(v)
                                else:
                                    extracted_dict[k] = int(v)
                            except ValueError:
                                extracted_dict[k] = v.strip('"\'')
                
                # Check if we extracted a meaningful decision/signal
                key_identifiers = ["decision", "signal", "trade_action"]
                if any(ident in extracted_dict for ident in key_identifiers):
                    if "reasoning_en" not in extracted_dict:
                        extracted_dict["reasoning_en"] = clean_text[:400]
                    parsed = extracted_dict
                    if required_keys:
                        missing_keys = [k for k in required_keys if k not in parsed]
                        if missing_keys:
                            raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                    return parsed

                print(f"\n[DEBUG LLM_PARSER] Failed to find {{...}}. RAW RESPONSE: {repr(clean_text)}\n")
                raise ValueError("No JSON object '{...}' found in the LLM response.")

        # Парсинг с fallback восстановлением
        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError:
            # 1. Попытка безопасного парсинга Python dict через ast.literal_eval (одинарные кавычки, True/False)
            import ast
            try:
                cand = ast.literal_eval(clean_text)
                if isinstance(cand, dict):
                    parsed = cand
                else:
                    raise ValueError()
            except Exception:
                # 2. Восстановление незакрытых скобок и кавычек
                repaired = clean_text.rstrip()
                if repaired.count('"') % 2 != 0:
                    repaired += '"'
                open_brackets = repaired.count('[') - repaired.count(']')
                open_braces = repaired.count('{') - repaired.count('}')
                repaired += ']' * max(0, open_brackets)
                repaired += '}' * max(0, open_braces)
                try:
                    parsed = json.loads(repaired)
                except Exception:
                    try:
                        # 3. Замена одинарных кавычек на двойные
                        parsed = json.loads(repaired.replace("'", '"'))
                    except Exception as e:
                        raise ValueError(f"JSON Parsing Error: {str(e)}")
            
        # Проверка типа
        if isinstance(parsed, list):
            if len(parsed) > 0 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            else:
                raise ValueError("LLM returned a list instead of a JSON object (dict)")
        elif not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed).__name__}")
            
        # Defense-in-depth: Sanitize echoed enum schemas (e.g. if model literally outputs "LONG | SHORT | HOLD")
        for k in ["decision", "trade_action", "winning_argument", "signal"]:
            if k in parsed and isinstance(parsed[k], str) and ("|" in parsed[k] or "/" in parsed[k]):
                if k in ["decision", "trade_action"]:
                    parsed[k] = "HOLD"
                elif k == "signal":
                    parsed[k] = "NEUTRAL"
                elif k == "winning_argument":
                    parsed[k] = "Neither"

        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
```json ... ```)
        matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
        if matches:
            clean_text = matches[-1].strip()
        else:
            # 2. Если ограждений нет, ищем от первой { до последней }
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_text = clean_text[start_idx:end_idx+1]
            elif start_idx != -1:
                # Truncated response recovery: starts with { but missing closing }
                candidate = clean_text[start_idx:]
                last_comma = candidate.rfind(',')
                if last_comma != -1:
                    candidate = candidate[:last_comma]
                if candidate.count('"') % 2 != 0:
                    candidate += '"'
                open_brackets = candidate.count('[') - candidate.count(']')
                open_braces = candidate.count('{') - candidate.count('}')
                candidate += ']' * max(0, open_brackets)
                candidate += '}' * max(0, open_braces)
                clean_text = candidate
            else:
                # 3. Fallback: Parse key-value bullet points or lines (e.g. - decision: "LONG")
                extracted_dict = {}
                for line in clean_text.splitlines():
                    m = re.match(r'^\s*[-*#]?\s*["\']?([a-zA-Z0-9_]+)["\']?\s*[:=]\s*["\']?([^"\'\n\r]+)["\']?', line.strip())
                    if m:
                        k, v = m.group(1).strip().lower(), m.group(2).strip().rstrip(',')
                        if v.lower() == "true":
                            extracted_dict[k] = True
                        elif v.lower() == "false":
                            extracted_dict[k] = False
                        else:
                            try:
                                if "." in v:
                                    extracted_dict[k] = float(v)
                                else:
                                    extracted_dict[k] = int(v)
                            except ValueError:
                                extracted_dict[k] = v.strip('"\'')
                
                # Check if we extracted a meaningful decision/signal
                key_identifiers = ["decision", "signal", "trade_action"]
                if any(ident in extracted_dict for ident in key_identifiers):
                    if "reasoning_en" not in extracted_dict:
                        extracted_dict["reasoning_en"] = clean_text[:400]
                    parsed = extracted_dict
                    if required_keys:
                        missing_keys = [k for k in required_keys if k not in parsed]
                        if missing_keys:
                            raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                    return parsed

                print(f"\n[DEBUG LLM_PARSER] Failed to find {{...}}. RAW RESPONSE: {repr(clean_text)}\n")
                raise ValueError("No JSON object '{...}' found in the LLM response.")

        # Парсинг с fallback восстановлением
        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError:
            # 1. Попытка безопасного парсинга Python dict через ast.literal_eval (одинарные кавычки, True/False)
            import ast
            try:
                cand = ast.literal_eval(clean_text)
                if isinstance(cand, dict):
                    parsed = cand
                else:
                    raise ValueError()
            except Exception:
                # 2. Восстановление незакрытых скобок и кавычек
                repaired = clean_text.rstrip()
                if repaired.count('"') % 2 != 0:
                    repaired += '"'
                open_brackets = repaired.count('[') - repaired.count(']')
                open_braces = repaired.count('{') - repaired.count('}')
                repaired += ']' * max(0, open_brackets)
                repaired += '}' * max(0, open_braces)
                try:
                    parsed = json.loads(repaired)
                except Exception:
                    try:
                        # 3. Замена одинарных кавычек на двойные
                        parsed = json.loads(repaired.replace("'", '"'))
                    except Exception as e:
                        raise ValueError(f"JSON Parsing Error: {str(e)}")
            
        # Проверка типа
        if isinstance(parsed, list):
            if len(parsed) > 0 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            else:
                raise ValueError("LLM returned a list instead of a JSON object (dict)")
        elif not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed).__name__}")
            
        # Defense-in-depth: Sanitize echoed enum schemas (e.g. if model literally outputs "LONG | SHORT | HOLD")
        for k in ["decision", "trade_action", "winning_argument", "signal"]:
            if k in parsed and isinstance(parsed[k], str) and ("|" in parsed[k] or "/" in parsed[k]):
                if k in ["decision", "trade_action"]:
                    parsed[k] = "HOLD"
                elif k == "signal":
                    parsed[k] = "NEUTRAL"
                elif k == "winning_argument":
                    parsed[k] = "Neither"

        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
```json ... ```)
        matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
        if matches:
            clean_text = matches[-1].strip()
        else:
            # 2. Если ограждений нет, ищем от первой { до последней }
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_text = clean_text[start_idx:end_idx+1]
            elif start_idx != -1:
                # Truncated response recovery: starts with { but missing closing }
                candidate = clean_text[start_idx:]
                last_comma = candidate.rfind(',')
                if last_comma != -1:
                    candidate = candidate[:last_comma]
                if candidate.count('"') % 2 != 0:
                    candidate += '"'
                open_brackets = candidate.count('[') - candidate.count(']')
                open_braces = candidate.count('{') - candidate.count('}')
                candidate += ']' * max(0, open_brackets)
                candidate += '}' * max(0, open_braces)
                clean_text = candidate
            else:
                # 3. Fallback: Parse key-value bullet points or lines (e.g. - decision: "LONG")
                extracted_dict = {}
                for line in clean_text.splitlines():
                    m = re.match(r'^\s*[-*#]?\s*["\']?([a-zA-Z0-9_]+)["\']?\s*[:=]\s*["\']?([^"\'\n\r]+)["\']?', line.strip())
                    if m:
                        k, v = m.group(1).strip().lower(), m.group(2).strip().rstrip(',')
                        if v.lower() == "true":
                            extracted_dict[k] = True
                        elif v.lower() == "false":
                            extracted_dict[k] = False
                        else:
                            try:
                                if "." in v:
                                    extracted_dict[k] = float(v)
                                else:
                                    extracted_dict[k] = int(v)
                            except ValueError:
                                extracted_dict[k] = v.strip('"\'')
                
                # Check if we extracted a meaningful decision/signal
                key_identifiers = ["decision", "signal", "trade_action"]
                if any(ident in extracted_dict for ident in key_identifiers):
                    if "reasoning_en" not in extracted_dict:
                        extracted_dict["reasoning_en"] = clean_text[:400]
                    parsed = extracted_dict
                    if required_keys:
                        missing_keys = [k for k in required_keys if k not in parsed]
                        if missing_keys:
                            raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                    return parsed

                print(f"\n[DEBUG LLM_PARSER] Failed to find {{...}}. RAW RESPONSE: {repr(clean_text)}\n")
                raise ValueError("No JSON object '{...}' found in the LLM response.")

        # Парсинг с fallback восстановлением
        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError:
            # 1. Попытка безопасного парсинга Python dict через ast.literal_eval (одинарные кавычки, True/False)
            import ast
            try:
                cand = ast.literal_eval(clean_text)
                if isinstance(cand, dict):
                    parsed = cand
                else:
                    raise ValueError()
            except Exception:
                # 2. Восстановление незакрытых скобок и кавычек
                repaired = clean_text.rstrip()
                if repaired.count('"') % 2 != 0:
                    repaired += '"'
                open_brackets = repaired.count('[') - repaired.count(']')
                open_braces = repaired.count('{') - repaired.count('}')
                repaired += ']' * max(0, open_brackets)
                repaired += '}' * max(0, open_braces)
                try:
                    parsed = json.loads(repaired)
                except Exception:
                    try:
                        # 3. Замена одинарных кавычек на двойные
                        parsed = json.loads(repaired.replace("'", '"'))
                    except Exception as e:
                        raise ValueError(f"JSON Parsing Error: {str(e)}")
            
        # Проверка типа
        if isinstance(parsed, list):
            if len(parsed) > 0 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            else:
                raise ValueError("LLM returned a list instead of a JSON object (dict)")
        elif not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed).__name__}")
            
        # Defense-in-depth: Sanitize echoed enum schemas (e.g. if model literally outputs "LONG | SHORT | HOLD")
        for k in ["decision", "trade_action", "winning_argument", "signal"]:
            if k in parsed and isinstance(parsed[k], str) and ("|" in parsed[k] or "/" in parsed[k]):
                if k in ["decision", "trade_action"]:
                    parsed[k] = "HOLD"
                elif k == "signal":
                    parsed[k] = "NEUTRAL"
                elif k == "winning_argument":
                    parsed[k] = "Neither"

        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
```json ... ```)
        matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
        if matches:
            clean_text = matches[-1].strip()
        else:
            # 2. Если ограждений нет, ищем от первой { до последней }
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_text = clean_text[start_idx:end_idx+1]
            elif start_idx != -1:
                # Truncated response recovery: starts with { but missing closing }
                candidate = clean_text[start_idx:]
                last_comma = candidate.rfind(',')
                if last_comma != -1:
                    candidate = candidate[:last_comma]
                if candidate.count('"') % 2 != 0:
                    candidate += '"'
                open_brackets = candidate.count('[') - candidate.count(']')
                open_braces = candidate.count('{') - candidate.count('}')
                candidate += ']' * max(0, open_brackets)
                candidate += '}' * max(0, open_braces)
                clean_text = candidate
            else:
                # 3. Fallback: Parse key-value bullet points or lines (e.g. - decision: "LONG")
                extracted_dict = {}
                for line in clean_text.splitlines():
                    m = re.match(r'^\s*[-*#]?\s*["\']?([a-zA-Z0-9_]+)["\']?\s*[:=]\s*["\']?([^"\'\n\r]+)["\']?', line.strip())
                    if m:
                        k, v = m.group(1).strip().lower(), m.group(2).strip().rstrip(',')
                        if v.lower() == "true":
                            extracted_dict[k] = True
                        elif v.lower() == "false":
                            extracted_dict[k] = False
                        else:
                            try:
                                if "." in v:
                                    extracted_dict[k] = float(v)
                                else:
                                    extracted_dict[k] = int(v)
                            except ValueError:
                                extracted_dict[k] = v.strip('"\'')
                
                # Check if we extracted a meaningful decision/signal
                key_identifiers = ["decision", "signal", "trade_action"]
                if any(ident in extracted_dict for ident in key_identifiers):
                    if "reasoning_en" not in extracted_dict:
                        extracted_dict["reasoning_en"] = clean_text[:400]
                    parsed = extracted_dict
                    if required_keys:
                        missing_keys = [k for k in required_keys if k not in parsed]
                        if missing_keys:
                            raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                    return parsed

                print(f"\n[DEBUG LLM_PARSER] Failed to find {{...}}. RAW RESPONSE: {repr(clean_text)}\n")
                raise ValueError("No JSON object '{...}' found in the LLM response.")

        # Парсинг с fallback восстановлением
        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError:
            # 1. Попытка безопасного парсинга Python dict через ast.literal_eval (одинарные кавычки, True/False)
            import ast
            try:
                cand = ast.literal_eval(clean_text)
                if isinstance(cand, dict):
                    parsed = cand
                else:
                    raise ValueError()
            except Exception:
                # 2. Восстановление незакрытых скобок и кавычек
                repaired = clean_text.rstrip()
                if repaired.count('"') % 2 != 0:
                    repaired += '"'
                open_brackets = repaired.count('[') - repaired.count(']')
                open_braces = repaired.count('{') - repaired.count('}')
                repaired += ']' * max(0, open_brackets)
                repaired += '}' * max(0, open_braces)
                try:
                    parsed = json.loads(repaired)
                except Exception:
                    try:
                        # 3. Замена одинарных кавычек на двойные
                        parsed = json.loads(repaired.replace("'", '"'))
                    except Exception as e:
                        raise ValueError(f"JSON Parsing Error: {str(e)}")
            
        # Проверка типа
        if isinstance(parsed, list):
            if len(parsed) > 0 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            else:
                raise ValueError("LLM returned a list instead of a JSON object (dict)")
        elif not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed).__name__}")
            
        # Defense-in-depth: Sanitize echoed enum schemas (e.g. if model literally outputs "LONG | SHORT | HOLD")
        for k in ["decision", "trade_action", "winning_argument", "signal"]:
            if k in parsed and isinstance(parsed[k], str) and ("|" in parsed[k] or "/" in parsed[k]):
                if k in ["decision", "trade_action"]:
                    parsed[k] = "HOLD"
                elif k == "signal":
                    parsed[k] = "NEUTRAL"
                elif k == "winning_argument":
                    parsed[k] = "Neither"

        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
```json ... ```)
        matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
        if matches:
            clean_text = matches[-1].strip()
        else:
            # 2. Если ограждений нет, ищем от первой { до последней }
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                clean_text = clean_text[start_idx:end_idx+1]
            elif start_idx != -1:
                # Truncated response recovery: starts with { but missing closing }
                candidate = clean_text[start_idx:]
                last_comma = candidate.rfind(',')
                if last_comma != -1:
                    candidate = candidate[:last_comma]
                if candidate.count('"') % 2 != 0:
                    candidate += '"'
                open_brackets = candidate.count('[') - candidate.count(']')
                open_braces = candidate.count('{') - candidate.count('}')
                candidate += ']' * max(0, open_brackets)
                candidate += '}' * max(0, open_braces)
                clean_text = candidate
            else:
                # 3. Fallback: Parse key-value bullet points or lines (e.g. - decision: "LONG")
                extracted_dict = {}
                for line in clean_text.splitlines():
                    m = re.match(r'^\s*[-*#]?\s*["\']?([a-zA-Z0-9_]+)["\']?\s*[:=]\s*["\']?([^"\'\n\r]+)["\']?', line.strip())
                    if m:
                        k, v = m.group(1).strip().lower(), m.group(2).strip().rstrip(',')
                        if v.lower() == "true":
                            extracted_dict[k] = True
                        elif v.lower() == "false":
                            extracted_dict[k] = False
                        else:
                            try:
                                if "." in v:
                                    extracted_dict[k] = float(v)
                                else:
                                    extracted_dict[k] = int(v)
                            except ValueError:
                                extracted_dict[k] = v.strip('"\'')
                
                # Check if we extracted a meaningful decision/signal
                key_identifiers = ["decision", "signal", "trade_action"]
                if any(ident in extracted_dict for ident in key_identifiers):
                    if "score_breakdown" not in extracted_dict:
                        extracted_dict["score_breakdown"] = {
                            "bull_argument": 20,
                            "bear_argument": -10,
                            "mtf_trend": 10,
                            "risk_penalties": {"total": 0}
                        }
                    if "reasoning_en" not in extracted_dict:
                        extracted_dict["reasoning_en"] = clean_text[:400]
                    parsed = extracted_dict
                    if required_keys:
                        missing_keys = [k for k in required_keys if k not in parsed]
                        if missing_keys:
                            raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                    return parsed

                print(f"\n[DEBUG LLM_PARSER] Failed to find {{...}}. RAW RESPONSE: {repr(clean_text)}\n")
                raise ValueError("No JSON object '{...}' found in the LLM response.")

        # Парсинг с fallback восстановлением
        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError:
            # 1. Попытка безопасного парсинга Python dict через ast.literal_eval (одинарные кавычки, True/False)
            import ast
            try:
                cand = ast.literal_eval(clean_text)
                if isinstance(cand, dict):
                    parsed = cand
                else:
                    raise ValueError()
            except Exception:
                # 2. Восстановление незакрытых скобок и кавычек
                repaired = clean_text.rstrip()
                if repaired.count('"') % 2 != 0:
                    repaired += '"'
                open_brackets = repaired.count('[') - repaired.count(']')
                open_braces = repaired.count('{') - repaired.count('}')
                repaired += ']' * max(0, open_brackets)
                repaired += '}' * max(0, open_braces)
                try:
                    parsed = json.loads(repaired)
                except Exception:
                    try:
                        # 3. Замена одинарных кавычек на двойные
                        parsed = json.loads(repaired.replace("'", '"'))
                    except Exception as e:
                        raise ValueError(f"JSON Parsing Error: {str(e)}")
            
        # Проверка типа
        if isinstance(parsed, list):
            if len(parsed) > 0 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            else:
                raise ValueError("LLM returned a list instead of a JSON object (dict)")
        elif not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed).__name__}")
            
        # Defense-in-depth: Sanitize echoed enum schemas (e.g. if model literally outputs "LONG | SHORT | HOLD")
        for k in ["decision", "trade_action", "winning_argument", "signal"]:
            if k in parsed and isinstance(parsed[k], str) and ("|" in parsed[k] or "/" in parsed[k]):
                if k in ["decision", "trade_action"]:
                    parsed[k] = "HOLD"
                elif k == "signal":
                    parsed[k] = "NEUTRAL"
                elif k == "winning_argument":
                    parsed[k] = "Neither"

        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
```

---

<a id="file-python-agents-base_agent-py"></a>
## File: `python/agents/base_agent.py` (51 строк, 2.8 KB)

```python
import json
from typing import Dict, Any, List

# Импорты ядра
from core.logger import TradeLogger
from core.llm_client import LLMClient
from core.llm_parser import LLMStructuredOutputParser

class BaseAgent:
    """
    Базовый класс для всех ИИ-агентов системы.
    Предоставляет общие зависимости и надежную утилиту для генерации и валидации JSON.
    """
    def __init__(self, name: str, logger: TradeLogger, llm_client: LLMClient):
        self.name = name
        self.logger = logger
        self.llm_client = llm_client

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Метод, который должен быть переопределен в каждом дочернем классе.
        """
        raise NotImplementedError("Дочерний класс должен реализовать метод analyze()")

    async def generate_json(self, prompt: str, required_keys: List[str] = None, max_retries: int = 3) -> Dict[str, Any]:
        """
        Generates and parses JSON from the LLM. If parsing fails, retries by appending
        the specific error to the prompt.
        """
        current_prompt = prompt
        if required_keys:
            current_prompt += f"\n\n[SCHEMA REQUIRED]: You MUST return a JSON object starting with '{{' and ending with '}}' with EXACTLY these keys: {', '.join(required_keys)}. Never output plain text lists or conversational explanations outside the JSON."
            
        for attempt in range(max_retries):
            response_text = await self.llm_client.generate(current_prompt)
            try:
                parsed = LLMStructuredOutputParser.parse_and_validate(response_text, required_keys)
                return parsed
            except ValueError as e:
                self.logger.warning(f"[{self.name}] Попытка {attempt+1}/{max_retries} провалилась из-за формата JSON: {e}")
                error_msg = str(e)
                
                # Smart retry logic
                if "Expecting" in error_msg or "Unterminated" in error_msg:
                    current_prompt += f"\n\n[SYSTEM ERROR]: Output truncated. {error_msg}. KEEP REASONING EXTREMELY CONCISE. RETURN VALID CLOSED JSON."
                else:
                    current_prompt += f"\n\n[SYSTEM ERROR]: Schema/JSON error: {error_msg}. OUTPUT VALID JSON ONLY."
        
        self.logger.error(f"[{self.name}] Фатальная ошибка: Не удалось сгенерировать валидный JSON после {max_retries} попыток.")
        return {"signal": "ERROR", "decision": "ERROR", "reasoning": "LLM failed to produce valid JSON after retries."}
            
```

---

<a id="file-python-agents-universe_agent-py"></a>
## File: `python/agents/universe_agent.py` (107 строк, 4.5 KB)

```python
import json
from typing import Dict, Any, List

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class UniverseAgent(BaseAgent):
    """
    The macro-filter agent. Scans the broader market to select the most liquid 
    and promising trading pairs, filtering out low-volume or scam tokens.
    Operates deterministically by default (<0.001s, 0 tokens), with optional LLM override.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("Universe_Agent", logger, llm_client)
        
        import os
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "universe_prompt.txt")
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        else:
            self.system_instruction = ""

    def select_deterministic_universe(self, broad_market_data: Dict[str, Any], limit: int = 7) -> Dict[str, Any]:
        """
        Fast deterministic universe selection (< 0.001s, 0 tokens).
        Algorithm:
        1. Select Core Majors (BTC, ETH, SOL) if present in the active perps.
        2. Rank remaining altcoins by liquidity (volumeQuote * atr_pct if present, or volumeQuote).
        3. Fill up to limit (default 7).
        """
        trending_perps = broad_market_data.get("trending_perps") or []
        if not trending_perps:
            # Safe default fallback
            default_majors = ["BTC-USD", "ETH-USD", "SOL-USD"]
            return {
                "selected_pairs": default_majors,
                "reasoning": "Резервный детерминированный выбор: базовые поводыри рынка (BTC, ETH, SOL)."
            }

        # 1. Normalize available symbols
        majors_priority = ["BTC-USD", "ETH-USD", "SOL-USD"]
        selected_majors = []
        altcoins = []

        for p in trending_perps:
            sym = p.get("symbol", "")
            base = sym.replace('/', '-').split('-')[0].upper()
            canonical_sym = f"{base}-USD"
            
            vol = float(p.get("volumeQuote") or 0.0)
            atr_pct = float(p.get("atr_pct") or 1.0)
            score = vol * atr_pct
            
            p_entry = {
                "symbol": canonical_sym,
                "volumeQuote": vol,
                "atr_pct": atr_pct,
                "score": score
            }
            
            if canonical_sym in majors_priority:
                selected_majors.append(p_entry)
            else:
                altcoins.append(p_entry)

        # Sort majors by priority order
        selected_majors.sort(key=lambda x: majors_priority.index(x["symbol"]) if x["symbol"] in majors_priority else 999)
        major_symbols = [m["symbol"] for m in selected_majors]

        # Sort altcoins by score descending
        altcoins.sort(key=lambda x: x["score"], reverse=True)
        remaining_slots = max(0, limit - len(major_symbols))
        selected_alts = [a["symbol"] for a in altcoins[:remaining_slots]]

        final_selection = major_symbols + selected_alts
        reasoning = (
            f"Детерминированный отбор (0 токенов, <0.001с): "
            f"{len(major_symbols)} лидеров ({', '.join(major_symbols)}) + "
            f"{len(selected_alts)} ликвидных альткоинов ({', '.join(selected_alts)})."
        )

        return {
            "selected_pairs": final_selection,
            "reasoning": reasoning
        }

    async def analyze(self, broad_market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluates the broad market to select the active trading universe.
        Runs deterministically by default (0 tokens, <0.001s).
        """
        self.logger.info(f"[{self.name}] Сканирование широкого рынка для отбора активов...")
        
        from core.config import config
        use_llm = getattr(config, "USE_LLM_UNIVERSE", False) and bool(self.llm_client)

        if not use_llm:
            res = self.select_deterministic_universe(broad_market_data)
            self.logger.info(f"[{self.name}] {res['reasoning']}")
            return res

        # LLM fallback / override mode
        data_string = json.dumps(broad_market_data, indent=2)
        full_prompt = f"{self.system_instruction}\n\nBroad Market Data:\n{data_string}"
        return await self.generate_json(full_prompt, required_keys=["selected_pairs", "reasoning"])
```

---

<a id="file-python-agents-scanner_agent-py"></a>
## File: `python/agents/scanner_agent.py` (63 строк, 2.7 KB)

```python
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class ScannerAgent(BaseAgent):
    """
    Tactical gatekeeper agent enforcing ATR Volatility Guard and spread/slippage checks.
    Halts the analysis cycle if the market is choppy, low-volatility, or has catastrophic spread.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Scanner_Agent", logger, llm_client)

    async def analyze(self, asset_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Предполётная проверка ATR волатильности и спреда (Детерминированная)...")
        
        price_data = asset_data.get("price_data", {})
        ob_data = asset_data.get("order_book_data", {})
        indicators = asset_data.get("indicators", {})
        
        current_price = float(price_data.get("current_price", 1.0) or 1.0)
        spread = float(ob_data.get("spread", 0) or 0)
        atr_14 = float(indicators.get("atr_14", 0) or 0)
        
        # CRITICAL-18: Fail-closed if ATR is missing
        if atr_14 == 0:
            return {
                "reasoning": "Данные ATR отсутствуют или равны 0. Оценка риска невозможна.",
                "status": "DATA_INVALID",
                "proceed": False
            }
            
        # MEDIUM-19: Canonical Spread Calculation
        spread_pct_val = ob_data.get("spread_pct")
        spread_pct = float(spread_pct_val) if spread_pct_val is not None else 0.0
        if spread_pct == 0.0 and spread > 0 and current_price > 0:
            spread_pct = round((spread / current_price) * 100, 4)
        atr_pct = round((atr_14 / current_price) * 100, 4) if current_price > 0 else 0.0
        
        # HIGH-20: Dynamic Spread Limits
        max_allowed_spread = min(0.75, max(0.15, atr_pct * 0.20))
        
        if spread_pct > max_allowed_spread:
            return {
                "reasoning": f"Спред слишком высокий: {spread_pct:.3f}% > {max_allowed_spread:.3f}%.",
                "status": "SPREAD_TOO_HIGH",
                "proceed": False
            }
            
        if atr_pct > 0 and atr_pct < 0.03:
            return {
                "reasoning": f"Волатильность слишком низкая: ATR {atr_pct}% < 0.03%.",
                "status": "LOW_VOLATILITY",
                "proceed": False
            }
            
        return {
            "reasoning": f"Рынок ликвиден (спред {spread_pct}%) и волатилен (ATR {atr_pct}%).",
            "status": "READY",
            "proceed": True
        }
```

---

<a id="file-python-agents-candle_agent-py"></a>
## File: `python/agents/candle_agent.py` (161 строк, 8.6 KB)

```python
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class CandleAgent(BaseAgent):
    """
    Specialized agent for Price Action and Japanese Candlestick Pattern analysis on DEX perp markets.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("Candle_Agent", logger, llm_client)

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный анализ прайс-экшена и свечей...")
        
        price_data = market_data.get("price_data") or {}
        ohlcv = price_data.get("candles_20", [])
        indicators = market_data.get("indicators") or {}
        atr_14 = float(indicators.get("atr_14", 0) or 0)
        
        if not ohlcv or len(ohlcv) < 2:
            return {"signal": "NEUTRAL", "confidence": 0, "reasoning": "Not enough candle data for analysis", "pattern_detected": "None"}

        avg_volume_10 = 0
        try:
            if len(ohlcv) >= 10:
                avg_volume_10 = sum(float(c.get("volume", 0) or 0) for c in ohlcv[-10:]) / 10
            elif len(ohlcv) > 0:
                avg_volume_10 = sum(float(c.get("volume", 0) or 0) for c in ohlcv) / len(ohlcv)
        except Exception:
            avg_volume_10 = 0
            
        def _parse_candle(c: Dict[str, Any]):
            return {
                "open": float(c.get("open", 0) or 0),
                "high": float(c.get("high", 0) or 0),
                "low": float(c.get("low", 0) or 0),
                "close": float(c.get("close", 0) or 0),
                "volume": float(c.get("volume", 0) or 0)
            }

        try:
            # If 3 or more candles are present, ohlcv[-1] is the unclosed live bar,
            # ohlcv[-2] is the last confirmed closed bar, and ohlcv[-3] is the preceding closed bar.
            if len(ohlcv) >= 3:
                c_eval = _parse_candle(ohlcv[-2])
                c_prev = _parse_candle(ohlcv[-3])
                c_live = _parse_candle(ohlcv[-1])
                is_confirmed_closed = True
            else:
                c_eval = _parse_candle(ohlcv[-1])
                c_prev = _parse_candle(ohlcv[-2])
                c_live = None
                is_confirmed_closed = False
        except Exception as e:
            return {"signal": "ERROR", "confidence": 0, "reasoning": f"Invalid OHLCV format: {e}", "pattern_detected": "None"}

        o1, h1, l1, c1, v1 = c_eval["open"], c_eval["high"], c_eval["low"], c_eval["close"], c_eval["volume"]
        o2, h2, l2, c2, v2 = c_prev["open"], c_prev["high"], c_prev["low"], c_prev["close"], c_prev["volume"]

        body1 = abs(c1 - o1)
        total_range1 = h1 - l1
        upper_wick1 = h1 - max(c1, o1)
        lower_wick1 = min(c1, o1) - l1
        
        is_bullish1 = c1 > o1
        is_bearish1 = c1 < o1
        
        body2 = abs(c2 - o2)
        is_bearish2 = c2 < o2
        is_bullish2 = c2 > o2

        signal = "NEUTRAL"
        confidence = 50
        bar_desc = "закрытой свече" if is_confirmed_closed else "текущей свече"
        reasoning = f"Обычное движение цены, нет четких паттернов ликвидности на {bar_desc}."
        pattern = "None"
        
        if total_range1 > 0:
            # 1. Sweep (Pin Bar / Hammer) on evaluated bar
            if lower_wick1 > body1 * 2 and lower_wick1 > upper_wick1 * 2 and is_bullish1 and (lower_wick1 / total_range1) > 0.5:
                if total_range1 > 0.75 * atr_14 and v1 > avg_volume_10:
                    signal = "BULLISH"
                    confidence = 80
                    reasoning = f"Длинная нижняя тень на {bar_desc}. Сбор ликвидности (стопов) снизу и агрессивный откуп."
                    pattern = "Bullish Liquidity Sweep"
                else:
                    signal = "BULLISH"
                    confidence = 55
                    reasoning = f"Слабое отклонение снизу на {bar_desc}, недостаточный объем или волатильность."
                    pattern = "Weak Bullish Rejection"
                
            elif upper_wick1 > body1 * 2 and upper_wick1 > lower_wick1 * 2 and is_bearish1 and (upper_wick1 / total_range1) > 0.5:
                if total_range1 > 0.75 * atr_14 and v1 > avg_volume_10:
                    signal = "BEARISH"
                    confidence = 80
                    reasoning = f"Длинная верхняя тень на {bar_desc}. Сбор ликвидности (стопов) сверху и давление продавцов."
                    pattern = "Bearish Liquidity Sweep"
                else:
                    signal = "BEARISH"
                    confidence = 55
                    reasoning = f"Слабое отклонение сверху на {bar_desc}, недостаточный объем или волатильность."
                    pattern = "Weak Bearish Rejection"
                
            # 2. Standard Engulfing
            elif is_bullish1 and is_bearish2 and c1 > o2 and o1 < c2 and body1 > body2 * 1.2:
                signal = "BULLISH"
                confidence = 75
                reasoning = f"Бычье поглощение на {bar_desc}. Тело свечи полностью перекрывает тело предыдущей."
                pattern = "Bullish Engulfing"
                
            elif is_bearish1 and is_bullish2 and c1 < o2 and o1 > c2 and body1 > body2 * 1.2:
                signal = "BEARISH"
                confidence = 75
                reasoning = f"Медвежье поглощение на {bar_desc}. Тело свечи полностью перекрывает тело предыдущей."
                pattern = "Bearish Engulfing"
                
            # 3. Breakout (Reversal or Continuation)
            elif is_bullish1 and c1 > h2:
                signal = "BULLISH"
                confidence = 75 if is_bearish2 else 70
                continuation_str = "после отката" if is_bearish2 else "продолжение тренда"
                reasoning = f"Бычий пробой ({continuation_str}) на {bar_desc}. Закрытие выше максимума предыдущей свечи."
                pattern = "Bullish Breakout"
                
            elif is_bearish1 and c1 < l2:
                signal = "BEARISH"
                confidence = 75 if is_bullish2 else 70
                continuation_str = "после отката" if is_bullish2 else "продолжение тренда"
                reasoning = f"Медвежий пробой ({continuation_str}) на {bar_desc}. Закрытие ниже минимума предыдущей свечи."
                pattern = "Bearish Breakout"

        # 4. Check active forming bar for live liquidity sweep if evaluated bar was neutral
        if signal == "NEUTRAL" and c_live is not None:
            o_l, h_l, l_l, c_l, v_l = c_live["open"], c_live["high"], c_live["low"], c_live["close"], c_live["volume"]
            range_l = h_l - l_l
            body_l = abs(c_l - o_l)
            upper_l = h_l - max(c_l, o_l)
            lower_l = min(c_l, o_l) - l_l
            if range_l > 0:
                if lower_l > body_l * 2 and lower_l > upper_l * 2 and c_l > o_l and (lower_l / range_l) > 0.5:
                    if range_l > 0.75 * atr_14 and v_l > avg_volume_10:
                        signal = "BULLISH"
                        confidence = 70
                        reasoning = "Активный внутридневной сбор ликвидности снизу (длинная тень откупа на формирующейся свече)."
                        pattern = "Live Bullish Liquidity Sweep"
                elif upper_l > body_l * 2 and upper_l > lower_l * 2 and c_l < o_l and (upper_l / range_l) > 0.5:
                    if range_l > 0.75 * atr_14 and v_l > avg_volume_10:
                        signal = "BEARISH"
                        confidence = 70
                        reasoning = "Активное внутридневное отторжение сверху (длинная верхняя тень на формирующейся свече)."
                        pattern = "Live Bearish Liquidity Sweep"

        return {
            "signal": signal,
            "confidence": confidence,
            "reasoning": reasoning,
            "pattern_detected": pattern
        }
```

---

<a id="file-python-agents-indicator_agent-py"></a>
## File: `python/agents/indicator_agent.py` (236 строк, 11.6 KB)

```python
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class IndicatorAgent(BaseAgent):
    """
    Specialized agent for interpreting quantitative technical indicators (RSI, EMA-20, MACD momentum).
    Identifies momentum divergence, trend direction, and overbought/oversold conditions on Nado DEX.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("Indicator_Agent", logger, llm_client)

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный анализ технических индикаторов...")
        
        indicators = market_data.get("indicators") or {}
        price_data = market_data.get("price_data") or {}
        
        rsi = indicators.get("rsi_14")
        macd = indicators.get("macd")
        macd_signal = indicators.get("macd_signal")
        current_price = price_data.get("current_price", 0)
        ema_20 = indicators.get("ema_20")
        
        signal = "NEUTRAL"
        confidence = 50
        reason_parts = []
        
        bull_score = 0
        bear_score = 0

        mtf = market_data.get("multi_timeframe") or {}
        trend_1h = mtf.get("trend_1h", "NEUTRAL")
        trend_4h = mtf.get("trend_4h", "NEUTRAL")
        is_macro_bearish = (trend_1h == "BEARISH" or trend_4h == "BEARISH")
        is_macro_bullish = (trend_1h == "BULLISH" or trend_4h == "BULLISH")

        # 48h Macro Box & 15m Structural Extremes (Audit Step 1)
        c_1h = (mtf.get("tf_1h") or {}).get("candles_20", []) if isinstance(mtf, dict) else []
        c_15m = (price_data.get("candles_20") or price_data.get("candles") or []) if isinstance(price_data, dict) else []

        high_48h = float(indicators.get("high_48h") or 0.0)
        low_48h = float(indicators.get("low_48h") or 0.0)
        macro_box_pos_pct = float(indicators.get("macro_box_pos_pct") or 50.0)

        if c_1h:
            h_48_list = [float(c.get("high", current_price)) for c in c_1h[-48:] if isinstance(c, dict)]
            l_48_list = [float(c.get("low", current_price)) for c in c_1h[-48:] if isinstance(c, dict)]
            if h_48_list and l_48_list:
                high_48h = max(h_48_list)
                low_48h = min(l_48_list)
                if current_price > 0 and (high_48h - low_48h) > 0:
                    macro_box_pos_pct = round((current_price - low_48h) / max(high_48h - low_48h, 1e-9) * 100.0, 2)

        swing_high_15m = float(indicators.get("swing_high_15m") or current_price)
        swing_low_15m = float(indicators.get("swing_low_15m") or current_price)
        if c_15m:
            h_16_list = [float(c.get("high", current_price)) for c in c_15m[-16:] if isinstance(c, dict)]
            l_16_list = [float(c.get("low", current_price)) for c in c_15m[-16:] if isinstance(c, dict)]
            if h_16_list: swing_high_15m = round(max(h_16_list), 6)
            if l_16_list: swing_low_15m = round(min(l_16_list), 6)

        # Inject into market_data indicators for router and risk manager
        if isinstance(indicators, dict):
            indicators["high_48h"] = high_48h
            indicators["low_48h"] = low_48h
            indicators["macro_box_pos_pct"] = macro_box_pos_pct
            indicators["swing_high_15m"] = swing_high_15m
            indicators["swing_low_15m"] = swing_low_15m

        if macro_box_pos_pct < 32.0:
            reason_parts.append(f"Цена в нижней трети 48h коробки ({macro_box_pos_pct:.1f}%)")
        elif macro_box_pos_pct > 68.0:
            reason_parts.append(f"Цена в верхней трети 48h коробки ({macro_box_pos_pct:.1f}%)")
        else:
            reason_parts.append(f"Положение в 48h коробке: {macro_box_pos_pct:.1f}%")

        if rsi is not None:
            if rsi < 30:
                if is_macro_bearish and not is_macro_bullish:
                    # In a dominant bearish trend, RSI < 30 confirms aggressive downward momentum
                    bear_score += 2
                    reason_parts.append(f"RSI в зоне сильного импульса продаж ({rsi:.1f})")
                else:
                    bull_score += 2
                    reason_parts.append(f"RSI перепродан ({rsi:.1f})")
            elif rsi > 70:
                if is_macro_bullish and not is_macro_bearish:
                    # In a dominant bullish trend, RSI > 70 confirms strong upward momentum
                    bull_score += 2
                    reason_parts.append(f"RSI в зоне сильного импульса покупок ({rsi:.1f})")
                else:
                    bear_score += 2
                    reason_parts.append(f"RSI перекуплен ({rsi:.1f})")
            else:
                if rsi > 55: bull_score += 1
                elif rsi < 45: bear_score += 1
                reason_parts.append(f"RSI нейтрален ({rsi:.1f})")

        if macd is not None and macd_signal is not None:
            if macd > macd_signal and macd < 0:
                bull_score += 2
                reason_parts.append("MACD бычье пересечение ниже нуля")
            elif macd < macd_signal and macd > 0:
                bear_score += 2
                reason_parts.append("MACD медвежье пересечение выше нуля")
            elif macd > macd_signal:
                bull_score += 1
                reason_parts.append("MACD выше сигнальной линии")
            else:
                bear_score += 1
                reason_parts.append("MACD ниже сигнальной линии")

        if ema_20 is not None and current_price:
            if current_price > ema_20:
                bull_score += 1
                reason_parts.append("Цена выше EMA-20")
            else:
                bear_score += 1
                reason_parts.append("Цена ниже EMA-20")

        # Evaluate algorithmic signals
        algo_signals = indicators.get("algo_signals") or {}
        if algo_signals:
            rsi_div = str(algo_signals.get("rsi_divergence", "")).upper()
            if rsi_div == "BULLISH":
                bull_score += 3
                reason_parts.append("Обнаружена бычья дивергенция RSI (разворотный сигнал)")
            elif rsi_div == "BEARISH":
                bear_score += 3
                reason_parts.append("Обнаружена медвежья дивергенция RSI (разворотный сигнал)")
                
            macd_cross = str(algo_signals.get("macd_crossover", "")).upper()
            if macd_cross in ["BULLISH", "BULLISH_CROSS"]:
                bull_score += 2
                reason_parts.append("Недавний бычий MACD кроссовер")
            elif macd_cross in ["BEARISH", "BEARISH_CROSS"]:
                bear_score += 2
                reason_parts.append("Недавний медвежий MACD кроссовер")
                
            sweeps = algo_signals.get("liquidity_sweeps", [])
            bull_sweeps = [
                s for s in sweeps 
                if s.get("type") in ["bullish_sweep", "lower_sweep"] 
                or s.get("sweep_type") == "bullish_sweep" 
                or s.get("direction") == "bullish"
            ]
            bear_sweeps = [
                s for s in sweeps 
                if s.get("type") in ["bearish_sweep", "upper_sweep"] 
                or s.get("sweep_type") == "bearish_sweep" 
                or s.get("direction") == "bearish"
            ]
            
            if bull_sweeps:
                bull_score += min(2, len(bull_sweeps))
                reason_parts.append(f"Обнаружен сбор ликвидности снизу ({len(bull_sweeps)}x)")
            if bear_sweeps:
                bear_score += min(2, len(bear_sweeps))
                reason_parts.append(f"Обнаружен сбор ликвидности сверху ({len(bear_sweeps)}x)")

        # Interpret VWAP (intraday benchmark)
        vwap = indicators.get("vwap")
        if vwap is not None and current_price:
            try:
                vwap_val = float(vwap)
                if current_price > vwap_val:
                    bull_score += 1
                    reason_parts.append(f"Цена выше VWAP ({vwap_val:.2f})")
                elif current_price < vwap_val:
                    bear_score += 1
                    reason_parts.append(f"Цена ниже VWAP ({vwap_val:.2f})")
            except (ValueError, TypeError):
                pass

        # Interpret ADX (trend strength confirmation)
        adx = indicators.get("adx_14")
        if adx is not None and float(adx) >= 25.0:
            plus_di = float(indicators.get("plus_di_14", 0.0) or 0.0)
            minus_di = float(indicators.get("minus_di_14", 0.0) or 0.0)
            if plus_di > minus_di:
                bull_score += 1
                reason_parts.append(f"Сильный бычий тренд по ADX ({float(adx):.1f})")
            elif minus_di > plus_di:
                bear_score += 1
                reason_parts.append(f"Сильный медвежий тренд по ADX ({float(adx):.1f})")

        net_score = bull_score - bear_score

        # Higher Timeframe Trend Weighting Guard
        # Prevents 15m local noise (oversold bounce or overbought pullback) from shouting 70%+ confidence
        # directly against the dominant 1H macro trend.
        is_counter_trend = False
        if trend_1h == "BEARISH" and net_score > 0:
            is_counter_trend = True
            net_score = max(0, net_score - 2)
            reason_parts.append("Внимание: Сигнал 15м направлен против медвежьего 1H тренда (пенализация -2 балла)")
        elif trend_1h == "BULLISH" and net_score < 0:
            is_counter_trend = True
            net_score = min(0, net_score + 2)
            reason_parts.append("Внимание: Сигнал 15м направлен против бычьего 1H тренда (пенализация -2 балла)")

        # If both sides are strong and balanced, it's a technical conflict -> NEUTRAL
        if bull_score >= 3 and bear_score >= 3 and abs(net_score) < 2:
            signal = "NEUTRAL"
            confidence = 50
            reason_parts.append(f"Противоречивые сигналы индикаторов (Быки: {bull_score}, Медведи: {bear_score}). Нейтралитет.")
        elif net_score >= 3:
            signal = "BULLISH"
            confidence = (70 + min(25, net_score * 5)) if not is_counter_trend else min(60, 50 + net_score * 3)
        elif net_score <= -3:
            signal = "BEARISH"
            confidence = (70 + min(25, abs(net_score) * 5)) if not is_counter_trend else min(60, 50 + abs(net_score) * 3)
        elif net_score > 0:
            signal = "BULLISH"
            confidence = (55 + min(15, net_score * 5)) if not is_counter_trend else min(55, 45 + net_score * 3)
        elif net_score < 0:
            signal = "BEARISH"
            confidence = (55 + min(15, abs(net_score) * 5)) if not is_counter_trend else min(55, 45 + abs(net_score) * 3)
        else:
            signal = "NEUTRAL"
            confidence = 50

        return {
            "signal": signal,
            "confidence": min(confidence, 95),
            "reasoning": ". ".join(reason_parts),
            "macro_box_pos_pct": macro_box_pos_pct,
            "swing_high_15m": swing_high_15m,
            "swing_low_15m": swing_low_15m,
            "high_48h": high_48h,
            "low_48h": low_48h
        }
```

---

<a id="file-python-agents-order_book_agent-py"></a>
## File: `python/agents/order_book_agent.py` (63 строк, 2.9 KB)

```python
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class OrderBookAgent(BaseAgent):
    """
    Specialized market microstructure agent for analyzing DEX order book depth, bid-ask imbalances, and liquidity walls.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("Order_Book_Agent", logger, llm_client)

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный анализ микроструктуры стакана...")
        
        ob_data = market_data.get("order_book_data") or {}
        
        raw_imbalance = ob_data.get("imbalance")
        try:
            imbalance = float(raw_imbalance if raw_imbalance is not None else 0.0)
        except (ValueError, TypeError):
            imbalance = 0.0

        raw_spread = ob_data.get("spread")
        try:
            spread = float(raw_spread if raw_spread is not None else 0.0)
        except (ValueError, TypeError):
            spread = 0.0
        
        signal = "NEUTRAL"
        confidence = 50
        reason_parts = []
        
        if spread > 0:
            reason_parts.append(f"Спред {spread:.4f}")
            
        # Refined threshold from 0.30 to 0.15 (compensating for exponential distance weighting)
        if imbalance >= 0.15:
            signal = "BULLISH"
            if imbalance >= 0.28:
                confidence = min(85, 75 + int((imbalance - 0.28) * 35))
                reason_parts.append(f"Агрессивное институциональное доминирование покупателей в стакане (imbalance: {imbalance:.4f}).")
            else:
                confidence = min(74, 60 + int((imbalance - 0.15) * 100))
                reason_parts.append(f"Умеренный перевес объема покупателей в стакане (imbalance: {imbalance:.4f}).")
        elif imbalance <= -0.15:
            signal = "BEARISH"
            if imbalance <= -0.28:
                confidence = min(85, 75 + int((abs(imbalance) - 0.28) * 35))
                reason_parts.append(f"Агрессивное институциональное доминирование продавцов в стакане (imbalance: {imbalance:.4f}).")
            else:
                confidence = min(74, 60 + int((abs(imbalance) - 0.15) * 100))
                reason_parts.append(f"Умеренный перевес объема продавцов в стакане (imbalance: {imbalance:.4f}).")
        else:
            reason_parts.append(f"Баланс покупателей и продавцов нейтрален (imbalance: {imbalance:.4f})")
            
        return {
            "signal": signal,
            "confidence": min(confidence, 90),
            "reasoning": ". ".join(reason_parts)
        }
```

---

<a id="file-python-agents-oi_funding_agent-py"></a>
## File: `python/agents/oi_funding_agent.py` (117 строк, 5.5 KB)

```python
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class OIFundingAgent(BaseAgent):
    """
    Specialized derivatives analyst focusing on DEX Open Interest, Funding Rates, and Squeeze Dynamics.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("OI_Funding_Agent", logger, llm_client)

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный анализ открытого интереса (OI) и Funding...")
        
        oi_data = market_data.get("derivatives_data") or {}
        
        raw_funding = oi_data.get("funding_rate", oi_data.get("funding_rate_decimal", 0.0))
        try:
            funding_rate = float(raw_funding if raw_funding is not None else 0.0)
        except (ValueError, TypeError):
            funding_rate = 0.0

        raw_oi = oi_data.get("open_interest_usd")
        try:
            oi_usd = float(raw_oi if raw_oi is not None else 0.0)
        except (ValueError, TypeError):
            oi_usd = 0.0

        oi_trend = str(oi_data.get("open_interest_trend") or "neutral").lower()
        
        signal = "NEUTRAL"
        confidence = 50
        reason_parts = []
        
        bull_score = 0
        bear_score = 0

        # Determine price momentum / direction from price_data or indicators
        price_dir = "NEUTRAL"
        ind = market_data.get("indicators") or {}
        price_data = market_data.get("price_data") or {}
        curr_px = float(price_data.get("current_price") or ind.get("current_price") or 0.0)
        ema_20 = float(ind.get("ema_20") or 0.0)

        # 1. First priority: completed 15m candle close vs open
        candles = price_data.get("candles_20") or price_data.get("candles") or []
        if len(candles) >= 2:
            last_c = candles[-2]
        elif len(candles) == 1:
            last_c = candles[-1]
        else:
            last_c = None
        if isinstance(last_c, dict):
            c_open = float(last_c.get("open", 0.0) or 0.0)
            c_close = float(last_c.get("close", 0.0) or 0.0)
            if c_close > c_open:
                price_dir = "UP"
            elif c_close < c_open:
                price_dir = "DOWN"
        
        # 2. Second priority: price vs EMA-20
        if price_dir == "NEUTRAL" and curr_px > 0 and ema_20 > 0:
            if curr_px > ema_20:
                price_dir = "UP"
            elif curr_px < ema_20:
                price_dir = "DOWN"

        # Institutional capital flow dynamics (Price Direction + OI Trend)
        if oi_trend == "rising":
            if price_dir == "UP":
                bull_score += 2
                reason_parts.append("Приток институционального капитала в лонги (цена растет + OI растет: аккумуляция)")
            elif price_dir == "DOWN":
                bear_score += 2
                reason_parts.append("Приток институционального капитала в шорты (цена падает + OI растет: набор коротких позиций)")
            else:
                reason_parts.append("Открытый интерес растет в консолидации (накопление позиций)")
        elif oi_trend == "falling":
            if price_dir == "UP":
                bear_score += 1
                reason_parts.append("Рост цены на фоне закрытия шортов (цена растет + OI падает: шорт-сквиз/истощение импульса)")
            elif price_dir == "DOWN":
                bull_score += 1
                reason_parts.append("Падение на сбросе позиций (цена падает + OI падает: ликвидация перегруженных лонгов/капитуляция)")
            else:
                reason_parts.append("Снижение открытого интереса (отток капитала с рынка)")

        # Funding rate extremes (Crowd overextension & Squeeze risk - F-13 0.02%/hr threshold)
        if funding_rate > 0.0002:  # High positive funding (Longs paying shorts > 0.02%/hr)
            bear_score += 2
            reason_parts.append(f"Фандинг сильно позитивный ({funding_rate:.4f} > 0.02%/ч: лонги перегружены, риск сквиза вниз)")
        elif funding_rate < -0.0002: # High negative funding (Shorts paying longs < -0.02%/hr)
            bull_score += 2
            reason_parts.append(f"Фандинг сильно негативный ({funding_rate:.4f} < -0.02%/ч: шорты перегружены, риск шорт-сквиза)")
        else:
            reason_parts.append(f"Фандинг сбалансирован ({funding_rate:.4f})")
            
        if oi_usd > 1000000:
            reason_parts.append("Высокий открытый интерес")
        
        if bull_score > bear_score:
            signal = "BULLISH"
            diff = bull_score - bear_score
            confidence = min(85, 60 + diff * 10)
        elif bear_score > bull_score:
            signal = "BEARISH"
            diff = bear_score - bull_score
            confidence = min(85, 60 + diff * 10)
            
        return {
            "signal": signal,
            "confidence": min(confidence, 90),
            "reasoning": ". ".join(reason_parts)
        }
```

---

<a id="file-python-agents-news_agent-py"></a>
## File: `python/agents/news_agent.py` (133 строк, 5.9 KB)

```python
import json
import time
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class NewsAgent(BaseAgent):
    """
    Specialized sentiment & macro backdrop agent analyzing Crypto Fear & Greed Index and social sentiment.
    Includes caching so market-wide sentiment is only analyzed via LLM once per cycle, saving tokens.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("News_Agent", logger, llm_client)

        import os
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "news_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

        self._cached_result: Dict[str, Any] = {}
        self._cached_score: float = -1.0
        self._cached_trend_1h: str = ""
        self._cache_time: float = 0.0

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Анализ сентимента рынка и индекса Fear & Greed...")
        
        news_data = market_data.get("news_data") or {}
        raw_score = news_data.get("sentiment_score")
        try:
            score = float(raw_score if raw_score is not None else 50.0)
        except (ValueError, TypeError):
            score = 50.0

        mtf = market_data.get("multi_timeframe") or {}
        trend_1h = str(
            mtf.get("mtf_trend_1h")
            or mtf.get("trend_1h")
            or (mtf.get("tf_1h", {}).get("trend") if isinstance(mtf.get("tf_1h"), dict) else None)
            or "NEUTRAL"
        ).upper()
        trend_4h = str(
            mtf.get("mtf_trend_4h")
            or mtf.get("trend_4h")
            or (mtf.get("tf_4h", {}).get("trend") if isinstance(mtf.get("tf_4h"), dict) else None)
            or "NEUTRAL"
        ).upper()

        now = time.time()

        # Cache valid for 30 minutes if (round(score), trend_1h) hasn't changed
        if (
            self._cached_result
            and (now - self._cache_time < 1800)
            and round(self._cached_score) == round(score)
            and self._cached_trend_1h == trend_1h
        ):
            cached = dict(self._cached_result)
            self.logger.info(f"[{self.name}] [Cache Hit] Используем закэшированный сентимент (Score: {score}, 1H: {trend_1h}): {cached.get('signal')}")
            return cached
        
        payload = {
            "symbol": market_data.get("symbol"),
            "sentiment_data": news_data,
            "mtf_trend_1h": trend_1h,
            "mtf_trend_4h": trend_4h
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Sentiment Data:\n{data_string}"
        
        result = None
        if self.llm_client:
            try:
                result = await self.generate_json(full_prompt, required_keys=["signal", "confidence", "reasoning"])
            except Exception as e:
                self.logger.warning(f"[{self.name}] LLM generation failed: {e}")

        if isinstance(result, dict) and result.get("signal") not in ("ERROR", None):
            self._cached_result = result
            self._cached_score = score
            self._cached_trend_1h = trend_1h
            self._cache_time = now
            return result

        # Deterministic contrarian sentiment fallback (aligned with news_prompt.txt)
        if score < 40.0 and trend_1h == "BEARISH":
            fallback_signal = "NEUTRAL"
            fallback_conf = 55
            fallback_reason = (
                f"Страх на рынке ({score:.0f}/100), но 1H тренд нисходящий (BEARISH): "
                f"контртрендовый лонг заблокирован без подтверждения истощения продаж (фолбэк)."
            )
        elif score > 60.0 and trend_1h == "BULLISH":
            fallback_signal = "NEUTRAL"
            fallback_conf = 55
            fallback_reason = (
                f"Жадность на рынке ({score:.0f}/100), но 1H тренд восходящий (BULLISH): "
                f"контртрендовый шорт заблокирован без подтверждения истощения покупок (фолбэк)."
            )
        elif score <= 25.0:
            fallback_signal = "BULLISH"
            fallback_conf = 75
            fallback_reason = f"Extreme Fear ({score:.0f}/100): институциональная аккумуляция, асимметрия в лонг (фолбэк)."
        elif score >= 75.0:
            fallback_signal = "BEARISH"
            fallback_conf = 75
            fallback_reason = f"Extreme Greed ({score:.0f}/100): эйфория ритейла, риск истощения и коррекции (фолбэк)."
        elif score < 40.0:
            fallback_signal = "BULLISH"
            fallback_conf = 60
            fallback_reason = f"Умеренный страх на рынке ({score:.0f}/100): слабый уклон в лонг (фолбэк)."
        elif score > 60.0:
            fallback_signal = "BEARISH"
            fallback_conf = 60
            fallback_reason = f"Умеренная жадность ({score:.0f}/100): слабый уклон в шорт (фолбэк)."
        else:
            fallback_signal = "NEUTRAL"
            fallback_conf = 50
            fallback_reason = f"Нейтральный сентимент ({score:.0f}/100): отсутствие выраженного нарратива (фолбэк)."

        fallback_result = {
            "signal": fallback_signal,
            "confidence": fallback_conf,
            "reasoning": fallback_reason
        }
        self._cached_result = fallback_result
        self._cached_score = score
        self._cached_trend_1h = trend_1h
        self._cache_time = now
        return fallback_result
```

---

<a id="file-python-agents-bull_agent-py"></a>
## File: `python/agents/bull_agent.py` (99 строк, 5.1 KB)

```python
import os
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient
import json

class BullAgent(BaseAgent):
    """
    The Perma-Bull Agent.
    Specializes in finding LONG opportunities and defending them.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Bull_Agent", logger, llm_client)
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "bull_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        symbol = data.get("symbol")
        analyst_reports = data.get("analyst_reports", [])
        mtf_data = data.get("multi_timeframe_context", {})

        self.logger.info(f"[{self.name}] Building LONG thesis for {symbol}...")
        
        # Strip heavy candle arrays to save ~1,500 prompt tokens
        clean_mtf = dict(mtf_data) if isinstance(mtf_data, dict) else {}
        for tf_k in ["tf_15m", "tf_1h", "tf_4h"]:
            if tf_k in clean_mtf and isinstance(clean_mtf[tf_k], dict):
                clean_mtf[tf_k] = {k: v for k, v in clean_mtf[tf_k].items() if k != "candles_20"}

        # Enrich key_levels with precomputed structural and ATR levels
        raw_key_levels = dict(data.get("key_levels", {}))
        indicators_dict = data.get("indicators", {}) or {}
        curr_px = float(raw_key_levels.get("current_price") or indicators_dict.get("current_price") or 0.0)
        atr_14 = float(raw_key_levels.get("atr_14") or indicators_dict.get("atr_14") or (curr_px * 0.015))
        swing_h = float(raw_key_levels.get("swing_high_15m") or indicators_dict.get("swing_high_15m") or 0.0)
        swing_l = float(raw_key_levels.get("swing_low_15m") or indicators_dict.get("swing_low_15m") or 0.0)
        h_48h = float(raw_key_levels.get("high_48h") or indicators_dict.get("high_48h") or 0.0)
        l_48h = float(raw_key_levels.get("low_48h") or indicators_dict.get("low_48h") or 0.0)
        macro_box = float(raw_key_levels.get("macro_box_pos_pct") or indicators_dict.get("macro_box_pos_pct") or 50.0)

        key_levels = {
            **raw_key_levels,
            "current_price": curr_px,
            "atr_14": atr_14,
            "swing_high_15m": swing_h,
            "swing_low_15m": swing_l,
            "high_48h": h_48h,
            "low_48h": l_48h,
            "macro_box_pos_pct": macro_box,
            "atr_target_long": round(curr_px + 2.0 * atr_14, 4) if curr_px > 0 else 0.0,
            "atr_target_short": round(curr_px - 2.0 * atr_14, 4) if curr_px > 0 else 0.0,
            "structural_sl_long": round(swing_l - 0.25 * atr_14, 4) if swing_l > 0 else round(curr_px - 1.5 * atr_14, 4),
            "structural_sl_short": round(swing_h + 0.25 * atr_14, 4) if swing_h > 0 else round(curr_px + 1.5 * atr_14, 4),
        }

        payload = {
            "target_symbol": symbol,
            "key_levels": key_levels,
            "multi_timeframe_context": clean_mtf,
            "analyst_reports": analyst_reports
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Data:\n{data_string}"

        from core.config import config
        if getattr(config, "ANTI_BOT_MODE", False) is True:
            anti_bot_context = (
                "\n[🦈 ANTI-BOT LIQUIDITY HUNTER CONTEXT (Institutional v2.0)]:\n"
                "- System is hunting momentum/breakout bots trapped at oversold Bollinger Bands or extreme Donchian panic lows.\n"
                "- Do NOT concede defeat or score low simply because higher timeframes are in a downtrend.\n"
                "- If price is at the lower band (bb_position <= 15%), RSI is oversold (< 35), or lower rejection wicks and absorption are present, "
                "defend the LONG thesis aggressively! Highlight panic seller exhaustion, short squeeze potential, liquidity absorption, and high-probability mean reversion back to EMA-20.\n"
            )
            full_prompt = f"{full_prompt}\n{anti_bot_context}"
        
        try:
            res = await self.generate_json(full_prompt, required_keys=["thesis_score", "bullish_arguments", "summary"])
            if isinstance(res, dict) and res.get("signal") != "ERROR" and "thesis_score" in res:
                try:
                    score = float(res.get("thesis_score", 0))
                    res["thesis_score"] = max(0, min(10, int(score)))
                except (ValueError, TypeError):
                    res["thesis_score"] = 0
                return res
            self.logger.warning(f"[{self.name}] LLM returned invalid thesis schema or ERROR: {res}")
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to build thesis: {e}")

        return {
            "thesis_score": 0,
            "bullish_arguments": ["Не удалось сформировать бычий тезис из-за сбоя генерации."],
            "potential_target": 0.0,
            "invalidation_level": 0.0,
            "summary": "Fallback thesis (Bull LLM error)"
        }
```

---

<a id="file-python-agents-bear_agent-py"></a>
## File: `python/agents/bear_agent.py` (99 строк, 5.1 KB)

```python
import os
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient
import json

class BearAgent(BaseAgent):
    """
    The Perma-Bear Agent.
    Specializes in finding SHORT opportunities and defending them.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Bear_Agent", logger, llm_client)
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "bear_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        symbol = data.get("symbol")
        analyst_reports = data.get("analyst_reports", [])
        mtf_data = data.get("multi_timeframe_context", {})

        self.logger.info(f"[{self.name}] Building SHORT thesis for {symbol}...")
        
        # Strip heavy candle arrays to save ~1,500 prompt tokens
        clean_mtf = dict(mtf_data) if isinstance(mtf_data, dict) else {}
        for tf_k in ["tf_15m", "tf_1h", "tf_4h"]:
            if tf_k in clean_mtf and isinstance(clean_mtf[tf_k], dict):
                clean_mtf[tf_k] = {k: v for k, v in clean_mtf[tf_k].items() if k != "candles_20"}

        # Enrich key_levels with precomputed structural and ATR levels
        raw_key_levels = dict(data.get("key_levels", {}))
        indicators_dict = data.get("indicators", {}) or {}
        curr_px = float(raw_key_levels.get("current_price") or indicators_dict.get("current_price") or 0.0)
        atr_14 = float(raw_key_levels.get("atr_14") or indicators_dict.get("atr_14") or (curr_px * 0.015))
        swing_h = float(raw_key_levels.get("swing_high_15m") or indicators_dict.get("swing_high_15m") or 0.0)
        swing_l = float(raw_key_levels.get("swing_low_15m") or indicators_dict.get("swing_low_15m") or 0.0)
        h_48h = float(raw_key_levels.get("high_48h") or indicators_dict.get("high_48h") or 0.0)
        l_48h = float(raw_key_levels.get("low_48h") or indicators_dict.get("low_48h") or 0.0)
        macro_box = float(raw_key_levels.get("macro_box_pos_pct") or indicators_dict.get("macro_box_pos_pct") or 50.0)

        key_levels = {
            **raw_key_levels,
            "current_price": curr_px,
            "atr_14": atr_14,
            "swing_high_15m": swing_h,
            "swing_low_15m": swing_l,
            "high_48h": h_48h,
            "low_48h": l_48h,
            "macro_box_pos_pct": macro_box,
            "atr_target_long": round(curr_px + 2.0 * atr_14, 4) if curr_px > 0 else 0.0,
            "atr_target_short": round(curr_px - 2.0 * atr_14, 4) if curr_px > 0 else 0.0,
            "structural_sl_long": round(swing_l - 0.25 * atr_14, 4) if swing_l > 0 else round(curr_px - 1.5 * atr_14, 4),
            "structural_sl_short": round(swing_h + 0.25 * atr_14, 4) if swing_h > 0 else round(curr_px + 1.5 * atr_14, 4),
        }

        payload = {
            "target_symbol": symbol,
            "key_levels": key_levels,
            "multi_timeframe_context": clean_mtf,
            "analyst_reports": analyst_reports
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Data:\n{data_string}"

        from core.config import config
        if getattr(config, "ANTI_BOT_MODE", False) is True:
            anti_bot_context = (
                "\n[🦈 ANTI-BOT LIQUIDITY HUNTER CONTEXT (Institutional v2.0)]:\n"
                "- System is hunting momentum/breakout bots trapped at overbought Bollinger Bands or extreme Donchian pump highs.\n"
                "- Do NOT concede defeat or score low simply because higher timeframes are in an uptrend.\n"
                "- If price is at the upper band (bb_position >= 85%), RSI is overbought (> 65), or upper rejection wicks and trapped buyers are present, "
                "defend the SHORT thesis aggressively! Highlight retail FOMO exhaustion, bull trap mechanics, liquidity sweeps, and high-probability mean reversion back to EMA-20.\n"
            )
            full_prompt = f"{full_prompt}\n{anti_bot_context}"
        
        try:
            res = await self.generate_json(full_prompt, required_keys=["thesis_score", "bearish_arguments", "summary"])
            if isinstance(res, dict) and res.get("signal") != "ERROR" and "thesis_score" in res:
                try:
                    score = float(res.get("thesis_score", 0))
                    res["thesis_score"] = max(0, min(10, int(score)))
                except (ValueError, TypeError):
                    res["thesis_score"] = 0
                return res
            self.logger.warning(f"[{self.name}] LLM returned invalid thesis schema or ERROR: {res}")
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to build thesis: {e}")

        return {
            "thesis_score": 0,
            "bearish_arguments": ["Не удалось сформировать медвежий тезис из-за сбоя генерации."],
            "potential_target": 0.0,
            "invalidation_level": 0.0,
            "summary": "Fallback thesis (Bear LLM error)"
        }
```

---

