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
            current_prompt += f"\n\n[SCHEMA REQUIRED]: You MUST return a JSON object with EXACTLY these keys: {', '.join(required_keys)}"
            
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
            
