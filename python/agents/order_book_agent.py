import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class OrderBookAgent(BaseAgent):
    """
    Specialized market microstructure agent for analyzing DEX order book depth, bid-ask imbalances, and liquidity walls.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("Order_Book_Agent", logger, llm_client)

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный анализ микроструктуры стакана...")
        
        ob_data = market_data.get("order_book_data", {})
        
        imbalance = ob_data.get("imbalance", 0.0)
        spread = ob_data.get("spread", 0.0)
        
        signal = "NEUTRAL"
        confidence = 50
        reason_parts = []
        
        if spread > 0:
            reason_parts.append(f"Спред {spread:.4f}")
            
        if imbalance > 0.3:
            signal = "BULLISH"
            confidence = 60 + int(imbalance * 30)
            reason_parts.append(f"Значительный перевес объема покупателей (imbalance: {imbalance:.4f}).")
        elif imbalance < -0.3:
            signal = "BEARISH"
            confidence = 60 + int(abs(imbalance) * 30)
            reason_parts.append(f"Значительный перевес объема продавцов (imbalance: {imbalance:.4f}).")
        else:
            reason_parts.append(f"Баланс покупателей и продавцов нейтрален (imbalance: {imbalance:.4f})")
            
        return {
            "signal": signal,
            "confidence": min(confidence, 90),
            "reasoning": ". ".join(reason_parts)
        }
