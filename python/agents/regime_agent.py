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
            ema_trend = str(summary.get("ema_trend", "neutral")).lower()
            macd_label = str(summary.get("macd_label", "neutral")).lower()

            mtf_trends = summary.get("mtf_trends", {})
            t15 = str(mtf_trends.get("15m", "NEUTRAL")).upper()
            t1h = str(mtf_trends.get("1h", "NEUTRAL")).upper()
            t4h = str(mtf_trends.get("4h", "NEUTRAL")).upper()

            bull_votes = sum(1 for t in [t15, t1h, t4h] if t in ["BULLISH", "UP"])
            bear_votes = sum(1 for t in [t15, t1h, t4h] if t in ["BEARISH", "DOWN"])
            if bull_votes > bear_votes:
                trend_dir = "BULLISH"
            elif bear_votes > bull_votes:
                trend_dir = "BEARISH"
            else:
                trend_dir = "NEUTRAL"

            # --- 1. TREND SCORE (0-100) ---
            # Pillar A: Multi-Timeframe Structural Trend (35 pts)
            mtf_pts = 0.0
            if align == "FULL_ALIGNMENT":
                mtf_pts = 35.0
            elif align in ["PARTIAL_ALIGNMENT", "STRONG_TREND"] or (bull_votes >= 2 or bear_votes >= 2):
                mtf_pts = 20.0
            elif align == "MIXED_CHOP":
                mtf_pts = 5.0
            elif align == "COUNTER_TREND_WARNING":
                mtf_pts = 0.0

            # Pillar B: Directional Efficiency (Kaufman ER-14) (30 pts)
            # ER 0.50+ represents strong institutional directional flow
            er_pts = min(30.0, (er / 0.50) * 30.0) if er > 0 else 0.0

            # Pillar C: Moving Average & MACD Momentum Alignment (25 pts)
            ma_pts = 0.0
            if trend_dir == "BULLISH":
                if ema_trend in ["up", "bullish", "strong_bullish"]:
                    ma_pts += 15.0
                elif ema_trend in ["down", "bearish", "strong_bearish"]:
                    ma_pts -= 10.0
                if macd_label in ["bullish", "buy"]:
                    ma_pts += 10.0
                elif macd_label in ["bearish", "sell"]:
                    ma_pts -= 5.0
            elif trend_dir == "BEARISH":
                if ema_trend in ["down", "bearish", "strong_bearish"]:
                    ma_pts += 15.0
                elif ema_trend in ["up", "bullish", "strong_bullish"]:
                    ma_pts -= 10.0
                if macd_label in ["bearish", "sell"]:
                    ma_pts += 10.0
                elif macd_label in ["bullish", "buy"]:
                    ma_pts -= 5.0
            else:
                # Neutral direction
                if ema_trend in ["up", "bullish", "down", "bearish"]:
                    ma_pts += 5.0
                if macd_label in ["bullish", "bearish"]:
                    ma_pts += 5.0

            # Pillar D: RSI Trend Quality & Sustainability (10 pts, with Overbought/Oversold Guard)
            # Replaces unproven flat "RSI > 65 -> +20" heuristic.
            # Sustained momentum in trend direction is rewarded; exhaustion levels are guarded.
            rsi_pts = 0.0
            if trend_dir == "BULLISH":
                if 50.0 <= rsi <= 68.0:
                    rsi_pts = 10.0  # Healthy sustainable bull momentum corridor
                elif 68.0 < rsi <= 75.0:
                    rsi_pts = 5.0   # Extended momentum, approaching overbought
                elif rsi > 75.0:
                    rsi_pts = -10.0 # Overbought exhaustion / blow-off top warning
                elif rsi < 40.0:
                    rsi_pts = -10.0 # Bull trend broken by severe downside drop
            elif trend_dir == "BEARISH":
                if 32.0 <= rsi <= 50.0:
                    rsi_pts = 10.0  # Healthy sustainable bear momentum corridor
                elif 25.0 <= rsi < 32.0:
                    rsi_pts = 5.0   # Extended momentum, approaching oversold
                elif rsi < 25.0:
                    rsi_pts = -10.0 # Oversold exhaustion / climax sell-off warning
                elif rsi > 60.0:
                    rsi_pts = -10.0 # Bear trend broken by strong upside bounce
            else:
                if 42.0 <= rsi <= 58.0:
                    rsi_pts = 0.0
                elif (52.0 <= rsi <= 65.0) or (35.0 <= rsi <= 48.0):
                    rsi_pts = 5.0

            trend = max(0.0, min(100.0, mtf_pts + er_pts + ma_pts + rsi_pts))

            # --- 2. RANGE / CHOP SCORE (0-100) ---
            # Low ER indicates non-directional oscillation
            er_range_pts = max(0.0, 35.0 * (1.0 - (er / 0.30)))
            mtf_range_pts = 35.0 if align == "MIXED_CHOP" else (15.0 if align in ["TRANSITION", "COUNTER_TREND_WARNING"] else 0.0)
            
            rsi_range_pts = 0.0
            if 42.0 <= rsi <= 58.0:
                rsi_range_pts = 15.0
            elif 38.0 <= rsi <= 62.0:
                rsi_range_pts = 8.0

            ma_flat_pts = 0.0
            if ema_trend in ["flat", "neutral", "none"]:
                ma_flat_pts += 15.0
            elif ma_pts <= 10.0:
                ma_flat_pts += 8.0

            range_sc = max(0.0, min(100.0, er_range_pts + mtf_range_pts + rsi_range_pts + ma_flat_pts))

            # --- 3. VOLATILITY SCORE (0-100) ---
            atr_vol_pts = min(50.0, (atr / 1.8) * 50.0)
            fr_vol_pts = 30.0 if fr_pct >= 0.05 else (15.0 if fr_pct >= 0.02 else 0.0)
            chop_vol_pts = 20.0 if (er < 0.20 and atr > 1.2) else 0.0

            vol = max(0.0, min(100.0, atr_vol_pts + fr_vol_pts + chop_vol_pts))
            
            return {"trend": trend, "range": range_sc, "volatility": vol}

        btc_scores = calculate_asset_scores(btc_summary)
        eth_scores = calculate_asset_scores(eth_summary)
        
        # Composite scoring (BTC 70%, ETH 30%)
        trend_score = (btc_scores["trend"] * 0.7) + (eth_scores["trend"] * 0.3)
        range_score = (btc_scores["range"] * 0.7) + (eth_scores["range"] * 0.3)
        vol_score = (btc_scores["volatility"] * 0.7) + (eth_scores["volatility"] * 0.3)
        
        btc_atr = float(btc_summary.get("atr_pct", 0.0) or 0.0)
        
        # Regime determination based on dominant score
        # High ATR / Extreme volatility must take precedence to protect capital
        if vol_score >= 65.0 or btc_atr >= 1.5:
            regime = "HIGH_VOLATILITY"
            profile = "CONSERVATIVE"
        elif trend_score >= 60.0 and trend_score > range_score + 15.0:
            regime = "TRENDING"
            profile = "AGGRESSIVE"
        elif range_score >= 55.0 and range_score > trend_score + 15.0:
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

