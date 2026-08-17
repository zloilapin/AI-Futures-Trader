import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class OIFundingAgent(BaseAgent):
    """
    Specialized derivatives analyst focusing on DEX Open Interest, Funding Rates, and Squeeze Dynamics.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("OI_Funding_Agent", logger, llm_client)

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный анализ открытого интереса (OI) и Funding...")
        
        oi_data = market_data.get("derivatives_data", {})
        
        funding_rate = oi_data.get("funding_rate", 0.0)
        oi_usd = oi_data.get("open_interest_usd", 0.0)
        
        signal = "NEUTRAL"
        confidence = 50
        reason_parts = []
        
        bull_score = 0
        bear_score = 0
        
        if funding_rate > 0.005:  # High positive funding (Longs paying shorts)
            bear_score += 2
            reason_parts.append(f"Фандинг сильно позитивный ({funding_rate:.4f}%). Лонги перегружены, риск сквиза вниз")
        elif funding_rate < -0.005: # High negative funding (Shorts paying longs)
            bull_score += 2
            reason_parts.append(f"Фандинг сильно негативный ({funding_rate:.4f}%). Шорты перегружены, риск шорт-сквиза")
        else:
            reason_parts.append(f"Фандинг нейтрален ({funding_rate:.4f}%)")
            
        if oi_usd > 1000000:
            reason_parts.append("Высокий открытый интерес")
        
        if bull_score > bear_score:
            signal = "BULLISH"
            confidence = 60 + bull_score * 10
        elif bear_score > bull_score:
            signal = "BEARISH"
            confidence = 60 + bear_score * 10
            
        return {
            "signal": signal,
            "confidence": min(confidence, 90),
            "reasoning": ". ".join(reason_parts)
        }
