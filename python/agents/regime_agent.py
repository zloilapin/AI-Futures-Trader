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

        def is_high_volatility(asset_summary: Dict[str, Any]) -> bool:
            atr = float(asset_summary.get("atr_pct", 0.0) or 0.0)
            fr_pct = abs(float(asset_summary.get("funding_rate", 0.0) or 0.0)) * 100
            er_14 = float(asset_summary.get("er_14", 0.0) or 0.0)
            if fr_pct >= 0.05:
                return True
            if atr >= 1.5 and er_14 < 0.25:
                return True
            return False

        btc_high_vol = is_high_volatility(btc_summary)
        eth_high_vol = is_high_volatility(eth_summary)
        
        btc_er = float(btc_summary.get("er_14", 0.0) or 0.0)
        eth_er = float(eth_summary.get("er_14", 0.0) or 0.0)
        composite_er = (btc_er * 0.7) + (eth_er * 0.3)
        
        btc_align = btc_summary.get("mtf_alignment", "MIXED_CHOP")
        eth_align = eth_summary.get("mtf_alignment", "MIXED_CHOP")

        if btc_high_vol or eth_high_vol:
            regime = "HIGH_VOLATILITY"
            profile = "CONSERVATIVE"
            reason = []
            if btc_high_vol:
                btc_fr_pct = float(btc_summary.get('funding_rate', 0.0) or 0.0) * 100
                reason.append(f"BTC High Vol (ATR={btc_summary.get('atr_pct', 0)}%, FR={btc_fr_pct:.3f}%)")
            if eth_high_vol:
                eth_fr_pct = float(eth_summary.get('funding_rate', 0.0) or 0.0) * 100
                reason.append(f"ETH High Vol (ATR={eth_summary.get('atr_pct', 0)}%, FR={eth_fr_pct:.3f}%)")
            reasoning = "Deterministically triggered HIGH_VOLATILITY: " + " | ".join(reason)
            
        elif composite_er >= 0.28 and (btc_align == "FULL_ALIGNMENT" or eth_align == "FULL_ALIGNMENT"):
            regime = "TRENDING"
            profile = "AGGRESSIVE"
            reasoning = f"Deterministically triggered TRENDING: Composite ER is {composite_er:.2f} (>=0.28). BTC MTF: {btc_align}, ETH MTF: {eth_align}."
            
        elif composite_er < 0.32 and (btc_align == "MIXED_CHOP" or eth_align == "MIXED_CHOP") and btc_align != "FULL_ALIGNMENT" and eth_align != "FULL_ALIGNMENT":
            regime = "RANGE_CHOPPY"
            profile = "BALANCED"
            reasoning = f"Deterministically triggered RANGE_CHOPPY: Composite ER is {composite_er:.2f} (<0.32). BTC MTF: {btc_align}, ETH MTF: {eth_align}."
            
        else:
            regime = "TRANSITION"
            profile = "BALANCED"
            reasoning = f"Deterministically triggered TRANSITION: Composite ER is {composite_er:.2f}. BTC: {btc_align}, ETH: {eth_align}."

        result = {
            "regime": regime,
            "recommended_profile": profile,
            "reasoning_en": reasoning
        }

        self.logger.info(f"[{self.name}] Final Regime: {regime} -> {profile}")
        return result

