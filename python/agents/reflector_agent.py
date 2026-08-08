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
        self.system_instruction = (
            "You are a Senior Trading Post-Mortem & Machine Learning Reflection Specialist.\n"
            "Your objective: Perform a post-trade autopsy on a closed position (especially losing trades).\n"
            "Identify the root cause of the failure or success, and extract a concise, actionable trading lesson.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "trade_outcome": "WIN" | "LOSS",\n'
            '  "root_cause": "<e.g., Entered LONG into 4H bear trend / RSI divergence fakeout>",\n'
            '  "actionable_rule": "<e.g., WARNING: Do not enter LONG when 4H trend is BEARISH and RSI > 60>",\n'
            '  "reasoning": "<brief post-mortem breakdown>"\n'
            "}"
        )

    def get_lessons(self, limit: int = 5) -> List[str]:
        """Returns the most recent actionable rules/lessons learned."""
        if not os.path.exists(self.lessons_file):
            return []
        try:
            with open(self.lessons_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                lessons = [item.get("actionable_rule") for item in data if item.get("actionable_rule")]
                return lessons[-limit:]
        except Exception:
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
