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
        
        price_data = market_data.get("price_data") or {}
        ohlcv = price_data.get("candles_20", [])
        indicators = market_data.get("indicators") or {}
        atr_14 = float(indicators.get("atr_14", 0) or 0)
        
        if not ohlcv or len(ohlcv) < 2:
            return {"signal": "NEUTRAL", "confidence": 0, "reasoning": "Not enough candle data for analysis", "pattern_detected": "None"}

        avg_volume_10 = 0
        try:
            if len(ohlcv) >= 10:
                avg_volume_10 = sum(float(c.get("volume", 0) or 0) for c in ohlcv[-10:]) / 10
            elif len(ohlcv) > 0:
                avg_volume_10 = sum(float(c.get("volume", 0) or 0) for c in ohlcv) / len(ohlcv)
        except Exception:
            avg_volume_10 = 0
            
        def _parse_candle(c: Dict[str, Any]):
            return {
                "open": float(c.get("open", 0) or 0),
                "high": float(c.get("high", 0) or 0),
                "low": float(c.get("low", 0) or 0),
                "close": float(c.get("close", 0) or 0),
                "volume": float(c.get("volume", 0) or 0)
            }

        try:
            # If 3 or more candles are present, ohlcv[-1] is the unclosed live bar,
            # ohlcv[-2] is the last confirmed closed bar, and ohlcv[-3] is the preceding closed bar.
            if len(ohlcv) >= 3:
                c_eval = _parse_candle(ohlcv[-2])
                c_prev = _parse_candle(ohlcv[-3])
                c_live = _parse_candle(ohlcv[-1])
                is_confirmed_closed = True
            else:
                c_eval = _parse_candle(ohlcv[-1])
                c_prev = _parse_candle(ohlcv[-2])
                c_live = None
                is_confirmed_closed = False
        except Exception as e:
            return {"signal": "ERROR", "confidence": 0, "reasoning": f"Invalid OHLCV format: {e}", "pattern_detected": "None"}

        o1, h1, l1, c1, v1 = c_eval["open"], c_eval["high"], c_eval["low"], c_eval["close"], c_eval["volume"]
        o2, h2, l2, c2, v2 = c_prev["open"], c_prev["high"], c_prev["low"], c_prev["close"], c_prev["volume"]

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
        bar_desc = "закрытой свече" if is_confirmed_closed else "текущей свече"
        reasoning = f"Обычное движение цены, нет четких паттернов ликвидности на {bar_desc}."
        pattern = "None"
        
        if total_range1 > 0:
            # 1. Sweep (Pin Bar / Hammer) on evaluated bar
            if lower_wick1 > body1 * 2 and lower_wick1 > upper_wick1 * 2 and is_bullish1 and (lower_wick1 / total_range1) > 0.5:
                if total_range1 > 0.75 * atr_14 and v1 > avg_volume_10:
                    signal = "BULLISH"
                    confidence = 80
                    reasoning = f"Длинная нижняя тень на {bar_desc}. Сбор ликвидности (стопов) снизу и агрессивный откуп."
                    pattern = "Bullish Liquidity Sweep"
                else:
                    signal = "BULLISH"
                    confidence = 55
                    reasoning = f"Слабое отклонение снизу на {bar_desc}, недостаточный объем или волатильность."
                    pattern = "Weak Bullish Rejection"
                
            elif upper_wick1 > body1 * 2 and upper_wick1 > lower_wick1 * 2 and is_bearish1 and (upper_wick1 / total_range1) > 0.5:
                if total_range1 > 0.75 * atr_14 and v1 > avg_volume_10:
                    signal = "BEARISH"
                    confidence = 80
                    reasoning = f"Длинная верхняя тень на {bar_desc}. Сбор ликвидности (стопов) сверху и давление продавцов."
                    pattern = "Bearish Liquidity Sweep"
                else:
                    signal = "BEARISH"
                    confidence = 55
                    reasoning = f"Слабое отклонение сверху на {bar_desc}, недостаточный объем или волатильность."
                    pattern = "Weak Bearish Rejection"
                
            # 2. Standard Engulfing
            elif is_bullish1 and is_bearish2 and c1 > o2 and o1 < c2 and body1 > body2 * 1.2:
                signal = "BULLISH"
                confidence = 75
                reasoning = f"Бычье поглощение на {bar_desc}. Тело свечи полностью перекрывает тело предыдущей."
                pattern = "Bullish Engulfing"
                
            elif is_bearish1 and is_bullish2 and c1 < o2 and o1 > c2 and body1 > body2 * 1.2:
                signal = "BEARISH"
                confidence = 75
                reasoning = f"Медвежье поглощение на {bar_desc}. Тело свечи полностью перекрывает тело предыдущей."
                pattern = "Bearish Engulfing"
                
            # 3. Breakout (Reversal or Continuation)
            elif is_bullish1 and c1 > h2:
                signal = "BULLISH"
                confidence = 75 if is_bearish2 else 70
                continuation_str = "после отката" if is_bearish2 else "продолжение тренда"
                reasoning = f"Бычий пробой ({continuation_str}) на {bar_desc}. Закрытие выше максимума предыдущей свечи."
                pattern = "Bullish Breakout"
                
            elif is_bearish1 and c1 < l2:
                signal = "BEARISH"
                confidence = 75 if is_bullish2 else 70
                continuation_str = "после отката" if is_bullish2 else "продолжение тренда"
                reasoning = f"Медвежий пробой ({continuation_str}) на {bar_desc}. Закрытие ниже минимума предыдущей свечи."
                pattern = "Bearish Breakout"

        # 4. Check active forming bar for live liquidity sweep if evaluated bar was neutral
        if signal == "NEUTRAL" and c_live is not None:
            o_l, h_l, l_l, c_l, v_l = c_live["open"], c_live["high"], c_live["low"], c_live["close"], c_live["volume"]
            range_l = h_l - l_l
            body_l = abs(c_l - o_l)
            upper_l = h_l - max(c_l, o_l)
            lower_l = min(c_l, o_l) - l_l
            if range_l > 0:
                if lower_l > body_l * 2 and lower_l > upper_l * 2 and c_l > o_l and (lower_l / range_l) > 0.5:
                    if range_l > 0.75 * atr_14 and v_l > avg_volume_10:
                        signal = "BULLISH"
                        confidence = 70
                        reasoning = "Активный внутридневной сбор ликвидности снизу (длинная тень откупа на формирующейся свече)."
                        pattern = "Live Bullish Liquidity Sweep"
                elif upper_l > body_l * 2 and upper_l > lower_l * 2 and c_l < o_l and (upper_l / range_l) > 0.5:
                    if range_l > 0.75 * atr_14 and v_l > avg_volume_10:
                        signal = "BEARISH"
                        confidence = 70
                        reasoning = "Активное внутридневное отторжение сверху (длинная верхняя тень на формирующейся свече)."
                        pattern = "Live Bearish Liquidity Sweep"

        return {
            "signal": signal,
            "confidence": confidence,
            "reasoning": reasoning,
            "pattern_detected": pattern
        }
