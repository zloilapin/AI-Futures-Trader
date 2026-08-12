import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class OrderBookAgent(BaseAgent):
    """
    Specialized market microstructure agent for analyzing DEX order book depth, bid-ask imbalances, and liquidity walls.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Order_Book_Agent", logger, llm_client)
        
        self.system_instruction = (
            "You are a High-Frequency Market Maker and Order Book Flow Analyst at a crypto prop-trading firm.\n"
            "Your objective: Analyze DEX order book depth (bid/ask imbalance and liquidity walls) to determine institutional intent, spoofing, and the path of least resistance.\n\n"
            "Professional Evaluation Rules:\n"
            "1. Liquidity Magnetism vs Support/Resistance: Retail views massive walls as impenetrable support/resistance. Institutions view them as liquidity pools to hunt. If price is aggressively moving towards a massive wall, it is often a target (magnet), not a bounce zone.\n"
            "2. Spoofing & Manipulation: Extreme imbalances (e.g., 5x more bids than asks) are often fake (spoofing) to induce retail buying. Be highly skeptical of perfect ratios.\n"
            "3. Absorption & Icebergs: If price hits a heavy wall and stops, but the wall size doesn't deplete, it indicates passive absorption by limit orders (Iceberg orders). This is a genuine reversal signal.\n"
            "4. Path of Least Resistance: The market moves where there is the least liquidity blocking the way. If the ask side is thin and bids are stacked but creeping up, it's bullish.\n\n"
            "Do not blindly trust 'Bid > Ask = Bullish'. Read the institutional manipulation behind the data.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning": "<step-by-step breakdown of spoofing, liquidity magnetism, and true imbalance>",\n'
            '  "imbalance_verdict": "<e.g., Massive bid spoofing detected / Genuine ask absorption>",\n'
            '  "confidence": <int 1-100>,\n'
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL"\n'
            "}\n"
            "CRITICAL: Output ONLY valid JSON. Do not write any conversational text, explanations, or Python scripts outside the JSON object. Do not simulate missing data."
        )

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Анализ микроструктуры стакана и ликвидности...")
        
        ob_data = market_data.get("order_book_data", {})
        price_data = market_data.get("price_data", {})
        
        payload = {
            "symbol": market_data.get("symbol"),
            "current_price": price_data.get("current_price"),
            "order_book_metrics": ob_data
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nOrder Book Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
