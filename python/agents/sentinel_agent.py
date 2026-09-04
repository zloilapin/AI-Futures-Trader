import os
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient
import json

class SentinelAgent(BaseAgent):
    """
    The Sentinel Agent (Risk Overwatch).
    Monitors active positions against current market conditions to see if the original thesis holds.
    Can trigger an early exit if the thesis is invalidated.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Sentinel_Agent", logger, llm_client)
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "sentinel_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        symbol = data.get("symbol")
        position_details = data.get("position_details", {})
        market_data = data.get("market_data", {})
        original_thesis = data.get("original_thesis", "")

        if hasattr(self.logger, "debug"):
            self.logger.debug(f"[{self.name}] Checking health of active {position_details.get('direction', '')} position on {symbol}...")
        
        payload = {
            "target_symbol": symbol,
            "position_details": position_details,
            "market_data": market_data,
            "original_thesis": original_thesis
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nData:\n{data_string}"
        
        try:
            return await self.generate_json(full_prompt, required_keys=["decision", "reasoning_en"])
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to evaluate position: {e}")
            return {"decision": "ERROR", "reasoning_en": f"Error: {e}"}
