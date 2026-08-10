import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class NewsAgent(BaseAgent):
    """
    Specialized sentiment & macro backdrop agent analyzing Crypto Fear & Greed Index and social sentiment.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("News_Agent", logger, llm_client)

        self.system_instruction = (
            "You are a Sentiment & Macro Strategist specializing in crypto markets and DeFi sentiment.\n"
            "Your objective: Evaluate market sentiment metrics, Fear & Greed Index, and broader market mood.\n\n"
            "Analytical Rules:\n"
            "1. Fear & Greed Index Scale (0 - 100):\n"
            "   - 0 - 25: Extreme Fear (Contrarian Bullish Opportunity - market blood in the streets).\n"
            "   - 26 - 45: Fear (Cautiously Bullish / Neutral).\n"
            "   - 46 - 55: Neutral.\n"
            "   - 56 - 75: Greed (Cautiously Bearish / Overextended).\n"
            "   - 76 - 100: Extreme Greed (Contrarian Bearish Risk - profit taking / correction likely).\n"
            "2. Sentiment Synthesis: Combine index score with news sentiment data.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning": "<step-by-step institutional sentiment breakdown of Fear & Greed and social backdrop>",\n'
            '  "sentiment_classification": "<e.g., Contrarian Bullish on Extreme Fear (28/100)>",\n'
            '  "confidence": <int 1-100>,\n'
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL"\n'
            "}"
        )

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Анализ сентимента рынка и индекса Fear & Greed...")
        
        news_data = market_data.get("news_data", {})
        
        payload = {
            "symbol": market_data.get("symbol"),
            "sentiment_data": news_data
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Sentiment Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
