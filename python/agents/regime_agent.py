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
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "regime_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

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
        self.logger.info(f"[{self.name}] Analyzing macro market regime...")

        # Distill macro summary (removes raw orderbooks, candles_20, and deep noise)
        macro_summary = self._extract_macro_summary(data)
        data_string = json.dumps(macro_summary, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Data:\n{data_string}"

        fallback_result = {
            "regime": "RANGE_CHOPPY",
            "recommended_profile": "BALANCED",
            "reasoning_en": "Fallback safe mode: default balanced risk profile."
        }

        try:
            raw_result = await self.generate_json(
                full_prompt,
                required_keys=["regime", "recommended_profile", "reasoning_en"]
            )

            if not isinstance(raw_result, dict) or raw_result.get("signal") == "ERROR":
                self.logger.warning(f"[{self.name}] LLM failed to return valid JSON. Applying safe fallback.")
                return fallback_result

            result = dict(raw_result)

            valid_regimes = {"TRENDING", "RANGE_CHOPPY", "HIGH_VOLATILITY"}
            valid_profiles = {"AGGRESSIVE", "BALANCED", "CONSERVATIVE"}

            regime = str(result.get("regime", "")).strip().upper()
            profile = str(result.get("recommended_profile", "")).strip().upper()

            if regime not in valid_regimes or profile not in valid_profiles:
                self.logger.warning(f"[{self.name}] Invalid regime ({regime}) or profile ({profile}) from LLM. Defaulting to safe values.")
                regime = "RANGE_CHOPPY"
                profile = "BALANCED"

            # === DETERMINISTIC RISK OVERRIDE (Safety First) ===
            btc_summary = macro_summary.get("BTC", {})
            btc_atr = float(btc_summary.get("atr_pct", 0.0) or 0.0)
            btc_fr = abs(float(btc_summary.get("funding_rate", 0.0) or 0.0))
            btc_rsi = float(btc_summary.get("rsi_14", 50.0) or 50.0)

            btc_is_high_vol = (
                btc_atr >= 1.5
                or btc_fr >= 0.05
                or (btc_atr >= 1.2 and (btc_rsi < 25 or btc_rsi > 75))
            )

            # Rule: If BTC is in HIGH_VOLATILITY, the system CANNOT be AGGRESSIVE (BTC Risk Override)
            if btc_is_high_vol:
                self.logger.warning(
                    f"[{self.name}] BTC Risk Override triggered: BTC is in high volatility "
                    f"(ATR={btc_atr}%, FR={btc_fr}%, RSI={btc_rsi}). Forcing CONSERVATIVE profile."
                )
                regime = "HIGH_VOLATILITY"
                profile = "CONSERVATIVE"
                result["reasoning_en"] = (
                    f"BTC Risk Override: BTC in high volatility (ATR {btc_atr}%, FR {btc_fr}%). "
                    + str(result.get("reasoning_en", ""))
                )

            # Enforce Risk Hierarchy: HIGH_VOLATILITY -> CONSERVATIVE only
            if regime == "HIGH_VOLATILITY":
                profile = "CONSERVATIVE"
            # Note: RANGE_CHOPPY no longer forces downgrade from AGGRESSIVE.
            # User-selected profile is respected; individual asset MTF alignment
            # is checked per-asset in Pipeline and RiskManager.

            result["regime"] = regime
            result["recommended_profile"] = profile
            return result

        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to evaluate market regime: {e}")
            fallback_result["reasoning_en"] = f"Fallback due to exception: {e}"
            return fallback_result

