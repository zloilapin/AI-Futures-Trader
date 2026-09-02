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
        
        payload = {
            "target_symbol": symbol,
            "multi_timeframe_context": mtf_data,
            "analyst_reports": analyst_reports
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Data:\n{data_string}"
        
        try:
            return await self.generate_json(full_prompt, required_keys=["thesis_score", "bearish_arguments", "summary"])
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to build thesis: {e}")
            return {"thesis_score": 0, "bearish_arguments": [], "summary": "Error generating bearish thesis"}
