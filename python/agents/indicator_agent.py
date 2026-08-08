import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class IndicatorAgent(BaseAgent):
    """
    Specialized agent for interpreting quantitative technical indicators (RSI, EMA-20, MACD momentum).
    Identifies momentum divergence, trend direction, and overbought/oversold conditions on Nado DEX.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Indicator_Agent", logger, llm_client)

        self.system_instruction = (
            "You are a Senior Quantitative Technical Strategist for DEX perpetual futures.\n"
            "Your objective: Evaluate mathematical technical indicators (RSI-14, EMA-20, MACD) and determine technical momentum.\n\n"
            "Analytical Rules:\n"
            "1. RSI (Relative Strength Index): RSI < 35 = Oversold / Bullish Reversal potential. RSI > 70 = Overbought / Bearish Reversal potential. 45-55 = Neutral momentum.\n"
            "2. EMA-20 (Trend Filter): Price > EMA-20 indicates bullish trend bias. Price < EMA-20 indicates bearish trend bias.\n"
            "3. MACD (Momentum & Crossover): Positive MACD histogram & bullish crossover = expanding upward momentum. Negative MACD histogram & bearish crossover = expanding downward momentum.\n"
            "4. Confluence Scoring: Require agreement between at least 2 out of 3 indicator signals for strong confidence (>75%).\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL",\n'
            '  "confidence": <int 1-100>,\n'
            '  "indicator_confluence": "<e.g., Bullish MACD Crossover + Price above EMA-20>",\n'
            '  "reasoning": "<clear quantitative breakdown of RSI, EMA, and MACD indicators>"\n'
            "}"
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
