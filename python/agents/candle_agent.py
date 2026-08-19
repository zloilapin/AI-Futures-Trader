import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class CandleAgent(BaseAgent):
    """
    Specialized agent for Price Action and Japanese Candlestick Pattern analysis on DEX perp markets.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient = None):
        super().__init__("Candle_Agent", logger, llm_client)

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный анализ прайс-экшена и свечей...")
        
        price_data = market_data.get("price_data", {})
        ohlcv = price_data.get("candles_20", [])
        
        if not ohlcv or len(ohlcv) < 2:
            return {"signal": "NEUTRAL", "confidence": 0, "reasoning": "Not enough candle data for analysis", "pattern_detected": "None"}
            
        last_candle = ohlcv[-1]
        prev_candle = ohlcv[-2]
        
        try:
            o1 = float(last_candle.get("open", 0))
            h1 = float(last_candle.get("high", 0))
            l1 = float(last_candle.get("low", 0))
            c1 = float(last_candle.get("close", 0))
            v1 = float(last_candle.get("volume", 0))
            
            o2 = float(prev_candle.get("open", 0))
            h2 = float(prev_candle.get("high", 0))
            l2 = float(prev_candle.get("low", 0))
            c2 = float(prev_candle.get("close", 0))
            v2 = float(prev_candle.get("volume", 0))
        except Exception as e:
            return {"signal": "ERROR", "reasoning": f"Invalid OHLCV format: {e}"}
            
        # Math checks
        body1 = abs(c1 - o1)
        total_range1 = h1 - l1
        upper_wick1 = h1 - max(c1, o1)
        lower_wick1 = min(c1, o1) - l1
        
        is_bullish1 = c1 > o1
        is_bearish1 = c1 < o1
        
        body2 = abs(c2 - o2)
        is_bearish2 = c2 < o2
        is_bullish2 = c2 > o2

        signal = "NEUTRAL"
        confidence = 50
        reasoning = "Обычное движение цены, нет четких паттернов ликвидности."
        pattern = "None"
        
        if total_range1 > 0:
            # 1. Sweep (Pin Bar / Hammer)
            if lower_wick1 > body1 * 2 and lower_wick1 > upper_wick1 * 2 and is_bullish1:
                signal = "BULLISH"
                confidence = 80
                reasoning = "Длинная нижняя тень. Сбор ликвидности (стопов) снизу и агрессивный откуп."
                pattern = "Bullish Liquidity Sweep"
                
            elif upper_wick1 > body1 * 2 and upper_wick1 > lower_wick1 * 2 and is_bearish1:
                signal = "BEARISH"
                confidence = 80
                reasoning = "Длинная верхняя тень. Сбор ликвидности (стопов) сверху и давление продавцов."
                pattern = "Bearish Liquidity Sweep"
                
            # 2. Engulfing + Displacement
            elif is_bullish1 and is_bearish2 and c1 > h2 and o1 <= c2:
                signal = "BULLISH"
                confidence = 75
                reasoning = "Бычье поглощение. Сильный импульс (Displacement), перекрывающий предыдущее падение."
                pattern = "Bullish Engulfing"
                
            elif is_bearish1 and is_bullish2 and c1 < l2 and o1 >= c2:
                signal = "BEARISH"
                confidence = 75
                reasoning = "Медвежье поглощение. Сильный дамп (Displacement), перекрывающий предыдущий рост."
                pattern = "Bearish Engulfing"

        return {
            "signal": signal,
            "confidence": confidence,
            "reasoning": reasoning,
            "pattern_detected": pattern
        }
