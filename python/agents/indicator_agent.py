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
                reason_parts.append("MACD выше сигнальной линии")
            else:
                bear_score += 1
                reason_parts.append("MACD ниже сигнальной линии")

        if ema_20 is not None and current_price:
            if current_price > ema_20:
                bull_score += 1
                reason_parts.append("Цена выше EMA-20")
            else:
                bear_score += 1
                reason_parts.append("Цена ниже EMA-20")

        # Evaluate algorithmic signals
        algo_signals = market_data.get("algo_signals", {})
        if algo_signals:
            if algo_signals.get("rsi_divergence") == "BULLISH":
                bull_score += 3
                reason_parts.append("Обнаружена бычья дивергенция RSI (разворотный сигнал)")
            elif algo_signals.get("rsi_divergence") == "BEARISH":
                bear_score += 3
                reason_parts.append("Обнаружена медвежья дивергенция RSI (разворотный сигнал)")
                
            if algo_signals.get("macd_crossover") == "BULLISH":
                bull_score += 2
                reason_parts.append("Недавний бычий MACD кроссовер")
            elif algo_signals.get("macd_crossover") == "BEARISH":
                bear_score += 2
                reason_parts.append("Недавний медвежий MACD кроссовер")
                
            sweeps = algo_signals.get("liquidity_sweeps", [])
            bull_sweeps = [s for s in sweeps if s.get("type") == "bullish_sweep"]
            bear_sweeps = [s for s in sweeps if s.get("type") == "bearish_sweep"]
            
            if bull_sweeps:
                bull_score += min(2, len(bull_sweeps))
                reason_parts.append(f"Обнаружен сбор ликвидности снизу ({len(bull_sweeps)}x)")
            if bear_sweeps:
                bear_score += min(2, len(bear_sweeps))
                reason_parts.append(f"Обнаружен сбор ликвидности сверху ({len(bear_sweeps)}x)")

        net_score = bull_score - bear_score
        
        # If both sides are strong and balanced, it's a technical conflict -> NEUTRAL
        if bull_score >= 3 and bear_score >= 3 and abs(net_score) < 2:
            signal = "NEUTRAL"
            confidence = 50
            reason_parts.append(f"Противоречивые сигналы индикаторов (Быки: {bull_score}, Медведи: {bear_score}). Нейтралитет.")
        elif net_score >= 3:
            signal = "BULLISH"
            confidence = 70 + min(25, net_score * 5)
        elif net_score <= -3:
            signal = "BEARISH"
            confidence = 70 + min(25, abs(net_score) * 5)
        elif net_score > 0:
            signal = "BULLISH"
            confidence = 55 + min(15, net_score * 5)
        elif net_score < 0:
            signal = "BEARISH"
            confidence = 55 + min(15, abs(net_score) * 5)
        else:
            signal = "NEUTRAL"
            confidence = 50

        return {
            "signal": signal,
            "confidence": min(confidence, 95),
            "reasoning": ". ".join(reason_parts)
        }
