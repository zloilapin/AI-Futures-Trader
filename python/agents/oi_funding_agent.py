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

        # Determine price momentum / direction from price_data or indicators
        price_dir = "NEUTRAL"
        ind = market_data.get("indicators") or {}
        price_data = market_data.get("price_data") or {}
        curr_px = float(price_data.get("current_price") or ind.get("current_price") or 0.0)
        ema_20 = float(ind.get("ema_20") or 0.0)

        # 1. First priority: completed 15m candle close vs open
        candles = price_data.get("candles") or []
        if len(candles) >= 1:
            last_c = candles[-1]
            c_open = float(last_c.get("open", 0.0))
            c_close = float(last_c.get("close", 0.0))
            if c_close > c_open:
                price_dir = "UP"
            elif c_close < c_open:
                price_dir = "DOWN"
        
        # 2. Second priority: price vs EMA-20
        if price_dir == "NEUTRAL" and curr_px > 0 and ema_20 > 0:
            if curr_px > ema_20:
                price_dir = "UP"
            elif curr_px < ema_20:
                price_dir = "DOWN"

        # Institutional capital flow dynamics (Price Direction + OI Trend)
        if oi_trend == "rising":
            if price_dir == "UP":
                bull_score += 2
                reason_parts.append("Приток институционального капитала в лонги (цена растет + OI растет: аккумуляция)")
            elif price_dir == "DOWN":
                bear_score += 2
                reason_parts.append("Приток институционального капитала в шорты (цена падает + OI растет: набор коротких позиций)")
            else:
                reason_parts.append("Открытый интерес растет в консолидации (накопление позиций)")
        elif oi_trend == "falling":
            if price_dir == "UP":
                bear_score += 1
                reason_parts.append("Рост цены на фоне закрытия шортов (цена растет + OI падает: шорт-сквиз/истощение импульса)")
            elif price_dir == "DOWN":
                bull_score += 1
                reason_parts.append("Падение на сбросе позиций (цена падает + OI падает: ликвидация перегруженных лонгов/капитуляция)")
            else:
                reason_parts.append("Снижение открытого интереса (отток капитала с рынка)")

        # Funding rate extremes (Crowd overextension & Squeeze risk - F-13 0.02%/hr threshold)
        if funding_rate > 0.0002:  # High positive funding (Longs paying shorts > 0.02%/hr)
            bear_score += 2
            reason_parts.append(f"Фандинг сильно позитивный ({funding_rate:.4f} > 0.02%/ч: лонги перегружены, риск сквиза вниз)")
        elif funding_rate < -0.0002: # High negative funding (Shorts paying longs < -0.02%/hr)
            bull_score += 2
            reason_parts.append(f"Фандинг сильно негативный ({funding_rate:.4f} < -0.02%/ч: шорты перегружены, риск шорт-сквиза)")
        else:
            reason_parts.append(f"Фандинг сбалансирован ({funding_rate:.4f})")
            
        if oi_usd > 1000000:
            reason_parts.append("Высокий открытый интерес")
        
        if bull_score > bear_score:
            signal = "BULLISH"
            diff = bull_score - bear_score
            confidence = min(85, 60 + diff * 10)
        elif bear_score > bull_score:
            signal = "BEARISH"
            diff = bear_score - bull_score
            confidence = min(85, 60 + diff * 10)
            
        return {
            "signal": signal,
            "confidence": min(confidence, 90),
            "reasoning": ". ".join(reason_parts)
        }
