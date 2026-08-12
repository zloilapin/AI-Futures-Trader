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
            clean_text = response_text.strip()
            
            # 1. Поиск блока JSON внутри markdown ограждений
            matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
            if matches:
                clean_text = matches[-1].strip()
            else:
                # 2. Если ограждений нет, ищем от первой { до последней }
                start_idx = clean_text.find('{')
                end_idx = clean_text.rfind('}')
                if start_idx != -1 and end_idx != -1:
                    clean_text = clean_text[start_idx:end_idx+1]

            parsed = json.loads(clean_text)
            if isinstance(parsed, list):
                if len(parsed) > 0 and isinstance(parsed[0], dict):
                    parsed = parsed[0]
                else:
                    return {"signal": "ERROR", "reasoning": "LLM returned a list instead of JSON object"}
            elif not isinstance(parsed, dict):
                return {"signal": "ERROR", "reasoning": f"LLM returned non-dict: {type(parsed).__name__}"}
            return parsed
            
        except json.JSONDecodeError as e:
            self.logger.error(f"[{self.name}] Ошибка парсинга JSON: {e}. Сырой текст: {response_text}")
            return {
                "signal": "ERROR", 
                "reasoning": f"Failed to parse JSON response: {e}",
                "raw_response": response_text
            }
            
