import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class IndicatorAgent(BaseAgent):
    """
    Specialized agent for interpreting quantitative technical indicators (RSI, EMA-20, MACD momentum).
    Identifies momentum divergence, trend direction, and overbought/oversold conditions on Kraken Futures.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Indicator_Agent", logger, llm_client)

        self.system_instruction = (
            "You are a Senior Quantitative Analyst at a crypto prop-trading firm.\n"
            "Your objective: Evaluate technical indicators (RSI, EMA, MACD) to find momentum divergences and dynamic value zones, avoiding retail traps.\n\n"
            "Professional Evaluation Rules:\n"
            "1. Momentum Divergence (High Conviction): Look for discrepancies between price and RSI/MACD. If price makes a Lower Low but RSI makes a Higher Low (Bullish Divergence), it's a massive reversal signal. If price makes a Higher High but RSI makes a Lower High (Bearish Divergence), momentum is dying.\n"
            "2. The Overbought Trap: Do NOT automatically short because RSI > 70. In strong trends, RSI stays overbought for a long time. It indicates strength, not an immediate top.\n"
            "3. EMA-20 Pullbacks (Dynamic Value): Never buy far above the EMA-20 (overextended). Wait for price to pull back and retest the EMA-20 as dynamic support before going LONG in an uptrend.\n"
            "4. MACD Trend Strength: Focus on the MACD histogram's acceleration/deceleration rather than just the crossover. Fading histogram means the current leg is losing steam.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning": "<step-by-step institutional breakdown of momentum divergences and dynamic value zones>",\n'
            '  "indicator_confluence": "<e.g., Bullish Divergence on RSI + Pullback to EMA-20>",\n'
            '  "confidence": <int 1-100>,\n'
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL"\n'
            "}\n"
            "CRITICAL: Output ONLY valid JSON. Do not write any conversational text, explanations, or Python scripts outside the JSON object. Do not simulate missing data."
        )

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Количественный анализ технический индикаторов (RSI, EMA, MACD)...")
        
        indicators = market_data.get("indicators", {})
        price_data = market_data.get("price_data", {})
        
        payload = {
            "symbol": market_data.get("symbol"),
            "current_price": price_data.get("current_price"),
            "indicators": indicators
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nTechnical Indicator Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
