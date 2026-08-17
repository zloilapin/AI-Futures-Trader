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
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("Indicator_Agent", logger, llm_client)

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный анализ технических индикаторов...")
        
        indicators = market_data.get("indicators", {})
        price_data = market_data.get("price_data", {})
        
        rsi = indicators.get("rsi_14")
        macd = indicators.get("macd")
        macd_signal = indicators.get("macd_signal")
        current_price = price_data.get("current_price", 0)
        ema_20 = indicators.get("ema_20")
        
        signal = "NEUTRAL"
        confidence = 50
        reason_parts = []
        
        bull_score = 0
        bear_score = 0

        if rsi is not None:
            if rsi < 30:
                bull_score += 2
                reason_parts.append(f"RSI перепродан ({rsi:.1f})")
            elif rsi > 70:
                bear_score += 2
                reason_parts.append(f"RSI перекуплен ({rsi:.1f})")
            else:
                if rsi > 55: bull_score += 1
                elif rsi < 45: bear_score += 1
                reason_parts.append(f"RSI нейтрален ({rsi:.1f})")

        if macd is not None and macd_signal is not None:
            if macd > macd_signal and macd < 0:
                bull_score += 2
                reason_parts.append("MACD бычье пересечение ниже нуля")
            elif macd < macd_signal and macd > 0:
                bear_score += 2
                reason_parts.append("MACD медвежье пересечение выше нуля")
            elif macd > macd_signal:
                bull_score += 1
                reason_parts.append("MACD гистограмма зелёная")
            else:
                bear_score += 1
                reason_parts.append("MACD гистограмма красная")

        if ema_20 is not None and current_price:
            if current_price > ema_20:
                bull_score += 1
                reason_parts.append("Цена выше EMA-20")
            else:
                bear_score += 1
                reason_parts.append("Цена ниже EMA-20")

        if bull_score >= 4:
            signal = "BULLISH"
            confidence = 70 + (bull_score * 5)
        elif bear_score >= 4:
            signal = "BEARISH"
            confidence = 70 + (bear_score * 5)
        elif bull_score > bear_score:
            signal = "BULLISH"
            confidence = 55 + (bull_score * 5)
        elif bear_score > bull_score:
            signal = "BEARISH"
            confidence = 55 + (bear_score * 5)

        return {
            "signal": signal,
            "confidence": min(confidence, 95),
            "reasoning": ". ".join(reason_parts)
        }
