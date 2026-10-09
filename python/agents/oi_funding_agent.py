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
        
        oi_data = market_data.get("derivatives_data") or {}
        
        raw_funding = oi_data.get("funding_rate", oi_data.get("funding_rate_decimal", 0.0))
        try:
            funding_rate = float(raw_funding if raw_funding is not None else 0.0)
        except (ValueError, TypeError):
            funding_rate = 0.0

        raw_oi = oi_data.get("open_interest_usd")
        try:
            oi_usd = float(raw_oi if raw_oi is not None else 0.0)
        except (ValueError, TypeError):
            oi_usd = 0.0

        oi_trend = str(oi_data.get("open_interest_trend") or "neutral").lower()
        
        signal = "NEUTRAL"
        confidence = 50
        reason_parts = []
        
        bull_score = 0
        bear_score = 0
        
        if funding_rate > 0.0005:  # High positive funding (Longs paying shorts)
            bear_score += 2
            reason = f"Фандинг сильно позитивный ({funding_rate:.4f}). Лонги перегружены, риск сквиза вниз"
            if oi_trend == "rising":
                bear_score += 1
                reason += " (усилено ростом OI)"
            reason_parts.append(reason)
        elif funding_rate < -0.0005: # High negative funding (Shorts paying longs)
            bull_score += 2
            reason = f"Фандинг сильно негативный ({funding_rate:.4f}). Шорты перегружены, риск шорт-сквиза"
            if oi_trend == "rising":
                bull_score += 1
                reason += " (усилено ростом OI)"
            reason_parts.append(reason)
        else:
            reason_parts.append(f"Фандинг нейтрален ({funding_rate:.4f})")
            
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
