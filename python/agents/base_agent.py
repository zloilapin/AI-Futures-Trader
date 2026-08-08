import json
import re
from typing import Dict, Any

# Импорты ядра
from core.logger import TradeLogger
from core.llm_client import LLMClient

class BaseAgent:
    """
    Базовый класс для всех ИИ-агентов системы.
    Предоставляет общие зависимости и надежную утилиту для парсинга JSON.
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

    def _parse_json(self, response_text: str) -> Dict[str, Any]:
        """
        Утилита для очистки и парсинга ответа от LLM с защитой от ошибок формата.
        """
        if not response_text:
            self.logger.error(f"[{self.name}] Получен пустой ответ от LLM.")
            return {"signal": "ERROR", "reasoning": "Empty response from LLM"}

        try:
            # Надежная очистка markdown-ограждений с помощью регулярных выражений
            clean_text = response_text.strip()
            if clean_text.startswith("```"):
                clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text)
                clean_text = re.sub(r"\s*```$", "", clean_text)

            return json.loads(clean_text)
            
        except json.JSONDecodeError as e:
            self.logger.error(f"[{self.name}] Ошибка парсинга JSON: {e}. Сырой текст: {response_text}")
            return {
                "signal": "ERROR", 
                "reasoning": f"Failed to parse JSON response: {e}",
                "raw_response": response_text
            }
            
