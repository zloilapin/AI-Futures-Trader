import os
import json
from typing import Dict, Any, List

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class ReflectorAgent(BaseAgent):
    """
    Self-Reflection / Post-Trade Autopsy Agent.
    Analyzes closed trades (especially Stop Loss losses) to extract lessons learned 
    and store negative pattern warnings in data/memory/lessons.json.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient, lessons_file: str = "data/memory/lessons.json"):
        super().__init__("Reflector_Agent", logger, llm_client)
        self.lessons_file = lessons_file
        import os
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "reflector_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    def get_lessons(self, limit: int = 5, symbol: str = None) -> List[str]:
        """Returns the most recent actionable rules/lessons learned, prioritizing LOSSes and specific symbols."""
        if not os.path.exists(self.lessons_file):
            return []
        try:
            with open(self.lessons_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                
                # Filter only for losses (we want to learn from mistakes, not successes)
                loss_data = [item for item in data if item.get("trade_outcome") == "LOSS" and item.get("actionable_rule")]
                
                lessons = []
                # First, get lessons specific to this symbol
                if symbol:
                    symbol_lessons = [item.get("actionable_rule") for item in loss_data if item.get("symbol") == symbol]
                    lessons.extend(symbol_lessons[-limit:])
                
                # If we still have room, pad with generic recent losses
                if len(lessons) < limit:
                    generic_lessons = [item.get("actionable_rule") for item in loss_data if item.get("symbol") != symbol]
                    lessons.extend(generic_lessons[-(limit - len(lessons)):])
                
                return lessons
        except Exception as e:
            self.logger.error(f"[{self.name}] Error reading lessons: {e}")
            return []

    def _save_lesson(self, reflection: Dict[str, Any]):
        os.makedirs(os.path.dirname(self.lessons_file), exist_ok=True)
        data = []
        if os.path.exists(self.lessons_file):
            try:
                with open(self.lessons_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = []

        data.append(reflection)
        
        # Enforce memory rotation limit
        if len(data) > 100:
            data = data[-100:]
            
        with open(self.lessons_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

    async def reflect(self, closed_trade: Dict[str, Any], market_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Runs LLM reflection on a closed trade and saves actionable rule to memory.
        """
        self.logger.info(f"[{self.name}] Проведение пост-мортем анализа сделки по {closed_trade.get('symbol')}...")
        
        payload = {
            "closed_trade": closed_trade,
            "market_context_at_close": market_context
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nClosed Trade Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        reflection = self._parse_json(response_text)
        
        if reflection and reflection.get("actionable_rule"):
            self._save_lesson(reflection)
            print(f"🧠 [ReflectorAgent] Урок извлечен и сохранен в память: {reflection.get('actionable_rule')}")

        return reflection
