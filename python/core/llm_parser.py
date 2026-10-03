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
            
        # Валидация схемы (обязательные ключи)
        if required_keys:
            missing_keys = [k for k in required_keys if k not in parsed]
            if missing_keys:
                raise ValueError(f"Missing required keys in JSON output: {missing_keys}")
                
        return parsed
