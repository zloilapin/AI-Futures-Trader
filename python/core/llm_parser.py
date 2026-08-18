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
        
        # 1. Поиск блока JSON внутри markdown ограждений (```json ... ```)
        matches = re.findall(r"```(?:json)?(.*?)```", clean_text, re.DOTALL | re.IGNORECASE)
        if matches:
            clean_text = matches[-1].strip()
        else:
            # 2. Если ограждений нет, ищем от первой { до последней }
            start_idx = clean_text.find('{')
            end_idx = clean_text.rfind('}')
            if start_idx != -1 and end_idx != -1:
                clean_text = clean_text[start_idx:end_idx+1]
            else:
                raise ValueError("No JSON object '{...}' found in the LLM response.")

        # Парсинг
        try:
            parsed = json.loads(clean_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON Parsing Error: {str(e)}")
            
        # Проверка типа
        if isinstance(parsed, list):
            if len(parsed) > 0 and isinstance(parsed[0], dict):
                parsed = parsed[0]
            else:
                raise ValueError("LLM returned a list instead of a JSON object (dict)")
        elif not isinstance(parsed, dict):
            raise ValueError(f"LLM returned non-dict: {type(parsed).__name__}")
            
        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
