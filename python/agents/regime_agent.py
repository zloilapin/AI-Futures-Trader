import os
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class RegimeAgent(BaseAgent):
    """
    The Regime Detection Agent (Macro Economist & Market Risk Gate).
    Evaluates BTC and ETH macro metrics to determine the overall market regime
    (HIGH_VOLATILITY, RANGE_CHOPPY, TRENDING) which dynamically controls the risk profile.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Regime_Agent", logger, llm_client)

    def _extract_asset_macro(self, raw_asset: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts strictly essential macro risk metrics for an asset (BTC or ETH),
        completely stripping raw candles, order book, and deep arrays.
        """
        if not isinstance(raw_asset, dict):
            return {}

        # If already formatted summary
        if "mtf_trends" in raw_asset and "atr_pct" in raw_asset and "order_book_data" not in raw_asset:
            return raw_asset

        mtf = raw_asset.get("multi_timeframe", {}) or {}
        price_data = raw_asset.get("price_data", {}) or {}
        indicators = raw_asset.get("indicators", {}) or {}
        derivatives = raw_asset.get("derivatives_data", {}) or {}
        news = raw_asset.get("news_data", {}) or {}

        price = price_data.get("current_price") or raw_asset.get("current_price", 0.0)

        t15 = mtf.get("trend_15m") or mtf.get("tf_15m", {}).get("trend", "NEUTRAL")
        t1h = mtf.get("trend_1h") or mtf.get("tf_1h", {}).get("trend", "NEUTRAL")
        t4h = mtf.get("trend_4h") or mtf.get("tf_4h", {}).get("trend", "NEUTRAL")
        alignment = mtf.get("mtf_alignment", "MIXED_CHOP")

        atr_pct = float(indicators.get("atr_pct", 0.0) or 0.0)
        rsi_14 = float(indicators.get("rsi_14", 50.0) or 50.0)
        er_14 = float(indicators.get("er_14", 0.0) or 0.0)
        ema_trend = indicators.get("ema_trend", "neutral")
        macd_label = indicators.get("macd_label") or indicators.get("macd_signal", "neutral")

        funding_rate = float(derivatives.get("funding_rate", 0.0) or derivatives.get("funding_rate_hourly", 0.0) or 0.0)
        open_interest_usd = float(derivatives.get("open_interest_usd", 0.0) or 0.0)
        news_sentiment = news.get("overall_sentiment") or news.get("sentiment_score") or "neutral"

        return {
            "price": price,
            "mtf_trends": {
                "15m": t15,
                "1h": t1h,
                "4h": t4h
            },
            "mtf_alignment": alignment,
            "atr_pct": atr_pct,
            "rsi_14": rsi_14,
            "er_14": er_14,
            "ema_trend": ema_trend,
            "macd_label": macd_label,
            "funding_rate": funding_rate,
            "open_interest_usd": open_interest_usd,
            "news_sentiment": news_sentiment
        }

    def _extract_macro_summary(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Builds the clean macro summary strictly for BTC and ETH without raw data bloat.
        """
        btc_raw = data.get("btc_data") or data.get("BTC") or {}
        eth_raw = data.get("eth_data") or data.get("ETH") or {}

        return {
            "BTC": self._extract_asset_macro(btc_raw),
            "ETH": self._extract_asset_macro(eth_raw)
        }

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Analyzing macro market regime deterministically...")

        # Distill macro summary
        macro_summary = self._extract_macro_summary(data)
        
        btc_summary = macro_summary.get("BTC", {})
        eth_summary = macro_summary.get("ETH", {})

        def calculate_asset_scores(summary: Dict[str, Any]) -> Dict[str, float]:
            atr = float(summary.get("atr_pct", 0.0) or 0.0)
            fr_pct = abs(float(summary.get("funding_rate", 0.0) or 0.0)) * 100
            er = float(summary.get("er_14", 0.0) or 0.0)
            align = summary.get("mtf_alignment", "MIXED_CHOP")
            rsi = float(summary.get("rsi_14", 50.0) or 50.0)

            # 1. Trend Score (0-100)
            trend = min(100.0, (er / 0.5) * 40.0) # ER of 0.5 is very strong trend
            if align == "FULL_ALIGNMENT": trend += 40.0
            if align == "COUNTER_TREND_WARNING": trend -= 20.0
            if rsi > 65 or rsi < 35: trend += 20.0
            trend = max(0.0, min(100.0, trend))

            # 2. Range Score (0-100)
            range_sc = min(100.0, max(0.0, 100.0 - (er / 0.3) * 100.0)) # Low ER = High Range
            if align == "MIXED_CHOP": range_sc += 40.0
            if 40 <= rsi <= 60: range_sc += 20.0
            range_sc = max(0.0, min(100.0, range_sc))

            # 3. Volatility Score (0-100)
            vol = min(100.0, (atr / 2.0) * 60.0) # ATR of 2.0% is very high
            if fr_pct >= 0.05: vol += 40.0 # High funding = extreme leverage/vol
            if er < 0.2 and atr > 1.5: vol += 20.0 # Noisy wide chop
            vol = max(0.0, min(100.0, vol))
            
            return {"trend": trend, "range": range_sc, "volatility": vol}

        btc_scores = calculate_asset_scores(btc_summary)
        eth_scores = calculate_asset_scores(eth_summary)
        
        # Composite scoring (BTC 70%, ETH 30%)
        trend_score = (btc_scores["trend"] * 0.7) + (eth_scores["trend"] * 0.3)
        range_score = (btc_scores["range"] * 0.7) + (eth_scores["range"] * 0.3)
        vol_score = (btc_scores["volatility"] * 0.7) + (eth_scores["volatility"] * 0.3)
        
        # Regime determination based on dominant score
        if vol_score >= 75.0:
            regime = "HIGH_VOLATILITY"
            profile = "CONSERVATIVE"
        elif trend_score >= 60.0 and trend_score > range_score + 10.0:
            regime = "TRENDING"
            profile = "AGGRESSIVE"
        elif range_score >= 60.0 and range_score > trend_score + 10.0:
            regime = "RANGE_CHOPPY"
            profile = "BALANCED"
        else:
            regime = "TRANSITION"
            profile = "BALANCED"

        reasoning = (
            f"Multi-Factor Regime Scores -> TREND: {trend_score:.1f} | "
            f"RANGE: {range_score:.1f} | VOLATILITY: {vol_score:.1f}. "
            f"Determined Regime: {regime}."
        )

        result = {
            "regime": regime,
            "recommended_profile": profile,
            "reasoning_en": reasoning
        }

        self.logger.info(f"[{self.name}] {reasoning} Profile: {profile}")
        return result

