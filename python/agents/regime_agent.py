import os
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient
import json

class RegimeAgent(BaseAgent):
    """
    The Regime Detection Agent.
    Evaluates BTC and ETH market conditions to determine the overall market regime
    (Trending, Choppy, High Volatility) which dynamically controls the risk profile.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Regime_Agent", logger, llm_client)
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "regime_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Analyzing macro market regime...")
        
        payload = {
            "btc_market_data": data.get("btc_data", {}),
            "eth_market_data": data.get("eth_data", {})
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Data:\n{data_string}"
        
        try:
            return await self.generate_json(full_prompt, required_keys=["regime", "recommended_profile", "reasoning_en"])
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to evaluate market regime: {e}")
            # Fallback to balanced if failed
            return {
                "regime": "RANGE_CHOPPY",
                "recommended_profile": "BALANCED",
                "reasoning_en": f"Fallback due to error: {e}"
            }
