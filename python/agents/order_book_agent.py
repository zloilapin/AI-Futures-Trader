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
            "You are an Institutional Market Microstructure & Order Book Analyst specializing in DEX perpetuals (Nado DEX / Ink L2).\n"
            "Your objective: Analyze order book depth, bid-ask imbalance, liquidity walls, and execution spread.\n\n"
            "Evaluation Criteria:\n"
            "1. Order Book Imbalance Ratio: Bid Volume / Ask Volume. Ratio > 1.2 = Heavy Buying Pressure (Bullish). Ratio < 0.8 = Heavy Selling Pressure (Bearish).\n"
            "2. Liquidity Walls: Identify large bid walls (support) or ask walls (resistance) near current price.\n"
            "3. Spread & Execution Safety: Ensure spread is tight and slippage is minimal for execution safety.\n"
            "4. Path of Least Resistance: Determine where price will move based on orderbook imbalance.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL",\n'
            '  "confidence": <int 1-100>,\n'
            '  "imbalance_verdict": "<e.g., Bid Imbalance + Strong Buy Wall at $63,500>",\n'
            '  "reasoning": "<institutional breakdown of liquidity walls, bid/ask depth, and spread>"\n'
            "}"
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
