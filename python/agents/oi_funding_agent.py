import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class OIFundingAgent(BaseAgent):
    """
    Specialized derivatives analyst focusing on DEX Open Interest, Funding Rates, and Squeeze Dynamics.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("OI_Funding_Agent", logger, llm_client)

        self.system_instruction = (
            "You are a Senior Derivatives Analyst specializing in crypto perpetuals, Open Interest (OI), and Funding Rates.\n"
            "Your objective: Evaluate market leverage, funding bias, and squeeze probabilities.\n\n"
            "Analytical Principles:\n"
            "1. Funding Rate Analysis:\n"
            "   - Positive Funding (> +0.01%): Longs pay shorts. Overcrowded long positions (Bearish squeeze risk).\n"
            "   - Negative Funding (< -0.01%): Shorts pay longs. Overcrowded short positions (Bullish squeeze opportunity).\n"
            "   - Near 0.00%: Healthy balanced leverage.\n"
            "2. Open Interest (OI) Momentum:\n"
            "   - Price Up + OI Up: Strong bullish trend confirmation.\n"
            "   - Price Down + OI Up: Strong bearish trend confirmation / aggressive shorting.\n"
            "   - Price Up + OI Down: Short covering / weak rally.\n"
            "   - Price Down + OI Down: Long liquidation / weak drop.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL",\n'
            '  "confidence": <int 1-100>,\n'
            '  "squeeze_risk": "<e.g., High Short Squeeze potential due to negative funding (-0.012%)>",\n'
            '  "reasoning": "<institutional analysis of Open Interest expansion and funding rate bias>"\n'
            "}"
        )

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Анализ открытого интереса (OI) и ставок финансирования (Funding)...")
        
        oi_data = market_data.get("derivatives_data", {})
        price_data = market_data.get("price_data", {})
        
        payload = {
            "symbol": market_data.get("symbol"),
            "current_price": price_data.get("current_price"),
            "derivatives_data": oi_data
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nDerivatives Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
