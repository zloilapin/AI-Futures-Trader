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
            "You are a Senior Macro Strategist and Sentiment Analyst at a crypto hedge fund.\n"
            "Your objective: Evaluate market sentiment metrics, the Fear & Greed Index, and social narratives to identify contrarian opportunities.\n\n"
            "Professional Evaluation Rules:\n"
            "1. Blood in the Streets (Contrarian Long): When Fear & Greed is at Extreme Fear (0-25) and retail is panicking, institutions are quietly accumulating. This provides a high-probability asymmetric LONG opportunity.\n"
            "2. Euphoria & Exhaustion (Contrarian Short): When the index shows Extreme Greed (76-100) and retail is bragging about gains, the market has run out of marginal buyers. The risk of a severe correction is very high. Prepare to SHORT.\n"
            "3. Catalyst Momentum: If there is a massive fundamental narrative (e.g., ETF launch, major network upgrade), momentum can stay 'Greedy' for an extended time. Do not short immediately if the narrative is fresh.\n"
            "4. The Choppy Middle: Neutral sentiment (40-60) means the market lacks a clear narrative. Expect choppy, range-bound price action without strong conviction.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning": "<step-by-step breakdown of institutional contrarian sentiment vs retail behavior>",\n'
            '  "sentiment_classification": "<e.g., Extreme Fear - Institutional Accumulation Phase>",\n'
            '  "confidence": <int 1-100>,\n'
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL"\n'
            "}\n"
            "CRITICAL: Output ONLY valid JSON. Do not write any conversational text, explanations, or Python scripts outside the JSON object. Do not simulate missing data."
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
