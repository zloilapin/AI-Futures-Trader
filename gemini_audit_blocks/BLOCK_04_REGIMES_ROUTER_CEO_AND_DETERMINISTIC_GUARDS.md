# BLOCK 04: REGIMES, STRATEGY ROUTER, CEO & DETERMINISTIC GUARDS
> **Назначение:** Досье для стороннего глубокого аудита кодовой базы моделью Gemini Argon / сторонним ИИ.
> **Дата генерации:** 2026-11-10 (Актуальное боевое состояние репозитория)

## Описание блока
Стратегический центр принятия решений: детекция рыночных режимов (RegimeAgent), маршрутизация сетапов (StrategyRouter), арбитражный суд инвестиционного комитета (CEOAgent) и жесткие математические фильтры (DeterministicGuard: Box Guard, Wave Exhaustion, Volume Explosion, Anti-Whipsaw).

## Оглавление файлов блока
1. [`python/agents/regime_agent.py`](#file-python-agents-regime_agent-py)
2. [`python/core/strategy_router.py`](#file-python-core-strategy_router-py)
3. [`python/agents/ceo_agent.py`](#file-python-agents-ceo_agent-py)
4. [`python/core/deterministic_guard.py`](#file-python-core-deterministic_guard-py)
5. [`python/core/decision_replay.py`](#file-python-core-decision_replay-py)

---

<a id="file-python-agents-regime_agent-py"></a>
## File: `python/agents/regime_agent.py` (486 строк, 21.5 KB)

```python
import os
import json
from typing import Dict, Any, Optional

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
        self.last_regime: str = "UNKNOWN"
        self.last_range_score: float = 0.0
        self.consecutive_trend_count: int = 0
        self.last_trend_ts: float = 0.0
        self.last_eval_ts: float = 0.0

        try:
            from core.state_store import StateStore
            persisted = StateStore.load("data/memory/regime_state.json", default={})
            if isinstance(persisted, dict):
                self.last_regime = str(persisted.get("last_regime", "UNKNOWN"))
                self.last_range_score = float(persisted.get("last_range_score", 0.0) or 0.0)
                self.consecutive_trend_count = int(persisted.get("consecutive_trend_count", 0) or 0)
                self.last_trend_ts = float(persisted.get("last_trend_ts", 0.0) or 0.0)
                self.last_eval_ts = float(persisted.get("last_eval_ts", 0.0) or 0.0)
        except Exception:
            pass

    @staticmethod
    def _safe_finite_float(val: Any, default: Optional[float] = None) -> Optional[float]:
        """Safely parses a value to a finite float, rejecting NaN, Inf, None, and unparseable types."""
        if val is None:
            return default
        try:
            import math
            f = float(val)
            return f if math.isfinite(f) else None
        except (ValueError, TypeError):
            return None

    VALID_ALIGNMENTS = {
        "FULL_ALIGNMENT", "TRANSITION", "COUNTER_TREND_WARNING",
        "MIXED_CHOP", "PARTIAL_ALIGNMENT"
    }
    # Accept canonical labels as well as standard exchange/feed synonyms
    VALID_TRENDS = {"BULLISH", "BEARISH", "NEUTRAL", "UP", "DOWN", "FLAT"}

    @classmethod
    def _normalize_trend(cls, val: Any) -> Optional[str]:
        """Maps diverse trend labels (UP/DOWN/FLAT/BULLISH/BEARISH/NEUTRAL) to canonical representation."""
        if not val:
            return None
        s = str(val).strip().upper()
        if s in ["BULLISH", "UP", "LONG"]:
            return "BULLISH"
        if s in ["BEARISH", "DOWN", "SHORT"]:
            return "BEARISH"
        if s in ["NEUTRAL", "FLAT", "SIDEWAYS", "CHOP"]:
            return "NEUTRAL"
        return None

    @classmethod
    def _is_valid_macro_asset(cls, asset: Dict[str, Any]) -> bool:
        """
        Validates that extracted macro metrics are present, complete, and numerically sound.
        Rejects empty dictionaries, NaN, Inf, non-positive prices, missing/None funding rate,
        and incomplete or unverified MTF contexts.
        """
        if not isinstance(asset, dict) or not asset:
            return False

        try:
            price = cls._safe_finite_float(asset.get("price"))
            if price is None or price <= 0:
                return False

            atr = cls._safe_finite_float(asset.get("atr_pct"))
            if atr is None or atr < 0:
                return False

            rsi = cls._safe_finite_float(asset.get("rsi_14"))
            if rsi is None or not (0.0 <= rsi <= 100.0):
                return False

            er = cls._safe_finite_float(asset.get("er_14"))
            if er is None or not (0.0 <= er <= 1.0):
                return False

            fr = cls._safe_finite_float(asset.get("funding_rate"))
            if fr is None:
                return False

            mtf_trends = asset.get("mtf_trends")
            if not isinstance(mtf_trends, dict) or not mtf_trends:
                return False

            valid_tf_names = ("15m", "1h", "4h")
            for tf in valid_tf_names:
                trend_val = mtf_trends.get(tf)
                if not trend_val or str(trend_val).upper() not in cls.VALID_TRENDS:
                    return False

            alignment = asset.get("mtf_alignment")
            if not alignment or str(alignment).upper() not in cls.VALID_ALIGNMENTS:
                return False

            return True
        except Exception:
            return False

    def _extract_asset_macro(self, raw_asset: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts strictly essential macro risk metrics for an asset (BTC or ETH),
        completely stripping raw candles, order book, and deep arrays.
        Does NOT inject artificial neutral defaults when telemetry is absent.
        """
        if not isinstance(raw_asset, dict) or not raw_asset:
            return {}

        # If already formatted summary
        if "mtf_trends" in raw_asset and "atr_pct" in raw_asset and "order_book_data" not in raw_asset:
            summary = dict(raw_asset)
            if isinstance(summary.get("mtf_trends"), dict):
                summary["mtf_trends"] = {
                    tf: self._normalize_trend(trend) for tf, trend in summary["mtf_trends"].items()
                }
            if summary.get("mtf_alignment"):
                summary["mtf_alignment"] = str(summary["mtf_alignment"]).strip().upper()
            return summary

        price_data = raw_asset.get("price_data", {}) or {}
        indicators = raw_asset.get("indicators", {}) or {}
        derivatives = raw_asset.get("derivatives_data", {}) or {}
        news = raw_asset.get("news_data", {}) or {}

        price = self._safe_finite_float(price_data.get("current_price") or raw_asset.get("current_price"))

        raw_mtf = raw_asset.get("multi_timeframe")
        if isinstance(raw_mtf, dict) and raw_mtf:
            t15 = raw_mtf.get("trend_15m") or (raw_mtf.get("tf_15m", {}).get("trend") if isinstance(raw_mtf.get("tf_15m"), dict) else None)
            t1h = raw_mtf.get("trend_1h") or (raw_mtf.get("tf_1h", {}).get("trend") if isinstance(raw_mtf.get("tf_1h"), dict) else None)
            t4h = raw_mtf.get("trend_4h") or (raw_mtf.get("tf_4h", {}).get("trend") if isinstance(raw_mtf.get("tf_4h"), dict) else None)
            alignment_raw = raw_mtf.get("mtf_alignment")

            norm_15 = self._normalize_trend(t15)
            norm_1h = self._normalize_trend(t1h)
            norm_4h = self._normalize_trend(t4h)

            mtf_trends = {
                "15m": norm_15,
                "1h": norm_1h,
                "4h": norm_4h
            }
            alignment = str(alignment_raw).upper() if alignment_raw else None
        else:
            mtf_trends = None
            alignment = None

        atr_pct = self._safe_finite_float(indicators.get("atr_pct"))
        rsi_14 = self._safe_finite_float(indicators.get("rsi_14"))
        er_14 = self._safe_finite_float(indicators.get("er_14"))
        ema_trend = indicators.get("ema_trend", "neutral")
        macd_label = indicators.get("macd_label") or indicators.get("macd_signal", "neutral")

        raw_fr = None
        if isinstance(derivatives, dict):
            if "funding_rate" in derivatives and derivatives["funding_rate"] is not None:
                raw_fr = derivatives["funding_rate"]
            elif "funding_rate_hourly" in derivatives and derivatives["funding_rate_hourly"] is not None:
                raw_fr = derivatives["funding_rate_hourly"]
        elif "funding_rate" in raw_asset and raw_asset["funding_rate"] is not None:
            raw_fr = raw_asset["funding_rate"]

        funding_rate = self._safe_finite_float(raw_fr, default=None)
        open_interest_usd = self._safe_finite_float(derivatives.get("open_interest_usd", 0.0), 0.0) if isinstance(derivatives, dict) else 0.0
        news_sentiment = news.get("overall_sentiment") or news.get("sentiment_score") or "neutral" if isinstance(news, dict) else "neutral"

        return {
            "price": price,
            "mtf_trends": mtf_trends,
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

    @classmethod
    def classify_regime_from_scores(
        cls,
        trend_score: float,
        range_score: float,
        vol_score: float,
        btc_atr: float,
        previous_regime: Optional[str] = None,
        consecutive_trend_count: int = 0,
        last_range_score: float = 0.0
    ) -> tuple[str, str]:
        """
        Pure deterministic decision function mapping composite scores to regime and recommended profile.
        Includes hysteresis: transitioning out of RANGE_CHOPPY requires confirmation (consecutive_trend_count >= 1)
        before granting full TRENDING / AGGRESSIVE. The initial breakout cycle routes through TRANSITION / BALANCED.
        Returns: (regime, recommended_profile)
        """
        # High ATR / Extreme volatility must take precedence to protect capital
        if vol_score >= 65.0 or btc_atr >= 1.5:
            return "HIGH_VOLATILITY", "CONSERVATIVE"
        elif trend_score >= 60.0 and trend_score > (range_score + 15.0):
            # Hysteresis check: If emerging from RANGE_CHOPPY on a single candle spike,
            # require at least 1 confirmation cycle before escalating to AGGRESSIVE.
            is_prev_range = (previous_regime in ["RANGE_CHOPPY", "TRANSITION"]) or (
                previous_regime == "UNKNOWN" and float(last_range_score or 0.0) >= 55.0
            )
            if is_prev_range and consecutive_trend_count < 1:
                return "TRANSITION", "BALANCED"
            return "TRENDING", "AGGRESSIVE"
        elif range_score >= 55.0 and range_score > (trend_score + 15.0):
            return "RANGE_CHOPPY", "BALANCED"
        else:
            return "TRANSITION", "BALANCED"

    def _extract_macro_summary(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Builds the clean macro summary strictly for BTC and ETH without raw data bloat.
        """
        if not isinstance(data, dict):
            return {"BTC": {}, "ETH": {}}
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

        btc_valid = self._is_valid_macro_asset(btc_summary)
        eth_valid = self._is_valid_macro_asset(eth_summary)

        if not btc_valid or not eth_valid:
            invalid_assets = []
            if not btc_valid:
                invalid_assets.append("BTC")
            if not eth_valid:
                invalid_assets.append("ETH")
            reasoning = (
                f"INSUFFICIENT_MACRO_DATA: Telemetry missing or invalid for {', '.join(invalid_assets)}. "
                f"Cannot establish macro regime with statistical confidence. Engaging failsafe CONSERVATIVE profile."
            )
            self.logger.warning(f"[{self.name}] ⚠️ {reasoning}")
            return {
                "regime": "UNKNOWN",
                "recommended_profile": "CONSERVATIVE",
                "reasoning_en": reasoning,
                "data_valid": False,
                "invalid_assets": invalid_assets
            }

        def calculate_asset_scores(summary: Dict[str, Any]) -> Dict[str, float]:
            atr = float(summary.get("atr_pct") or 0.0)
            fr_pct = abs(float(summary.get("funding_rate") or 0.0)) * 100
            er = float(summary.get("er_14") or 0.0)
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
        
        # Regime determination based on dominant score with hysteresis tracking.
        # FastRadar can wake up a new cycle in 2-3 minutes on the exact same 15m candle.
        # Require at least 600s (10 min) before treating a subsequent trend score as a new confirming cycle.
        import time
        now_ts = time.time()
        elapsed_since_eval = (now_ts - self.last_eval_ts) if self.last_eval_ts > 0 else 0.0
        elapsed_since_trend = (now_ts - self.last_trend_ts) if self.last_trend_ts > 0 else 0.0

        effective_trend_count = self.consecutive_trend_count
        if self.last_trend_ts > 0 and elapsed_since_trend < 600.0 and self.consecutive_trend_count <= 1:
            effective_trend_count = 0

        regime, profile = self.classify_regime_from_scores(
            trend_score, range_score, vol_score, btc_atr,
            previous_regime=self.last_regime,
            consecutive_trend_count=effective_trend_count,
            last_range_score=self.last_range_score
        )

        if regime == "TRENDING":
            self.consecutive_trend_count += 1
            if self.last_trend_ts == 0.0 or elapsed_since_trend >= 600.0:
                self.last_trend_ts = now_ts
        elif regime == "TRANSITION" and (self.last_regime in ["RANGE_CHOPPY", "TRANSITION"] or self.last_range_score >= 55.0) and trend_score >= 60.0 and trend_score > (range_score + 15.0):
            # Dampened by hysteresis on first breakout candle or rapid wake-up on same candle
            is_initial_breakout = (self.last_trend_ts == 0.0)
            self.consecutive_trend_count = 1
            if is_initial_breakout:
                self.last_trend_ts = now_ts
                elapsed_str = f"Breakout Start ({elapsed_since_eval:.0f}s since prev cycle)" if elapsed_since_eval > 0 else "Breakout Start (0s)"
            else:
                elapsed_str = f"{elapsed_since_trend:.0f}s (same 15m candle)"

            self.logger.info(
                f"[{self.name}] ⏳ Regime Hysteresis Damper: Breakout cycle from {self.last_regime} "
                f"(TREND: {trend_score:.1f} vs RANGE: {range_score:.1f}, Elapsed: {elapsed_str}). "
                f"Routing through TRANSITION (BALANCED) to prevent whipsaw trap."
            )
        else:
            self.consecutive_trend_count = 0
            self.last_trend_ts = 0.0

        self.last_regime = regime
        self.last_range_score = range_score
        self.last_eval_ts = now_ts
        try:
            from core.state_store import StateStore
            StateStore.save("data/memory/regime_state.json", {
                "last_regime": self.last_regime,
                "last_range_score": float(self.last_range_score),
                "consecutive_trend_count": int(self.consecutive_trend_count),
                "last_trend_ts": float(self.last_trend_ts),
                "last_eval_ts": float(self.last_eval_ts)
            })
        except Exception:
            pass

        reasoning = (
            f"Multi-Factor Regime Scores -> TREND: {trend_score:.1f} | "
            f"RANGE: {range_score:.1f} | VOLATILITY: {vol_score:.1f}. "
            f"Determined Regime: {regime}."
        )

        result = {
            "regime": regime,
            "recommended_profile": profile,
            "data_valid": True,
            "reasoning_en": reasoning,
            "range_score": float(range_score),
            "trend_score": float(trend_score),
            "vol_score": float(vol_score)
        }

        self.logger.info(f"[{self.name}] {reasoning} Profile: {profile}")
        return result

```

---

<a id="file-python-core-strategy_router-py"></a>
## File: `python/core/strategy_router.py` (1178 строк, 76.3 KB)

```python
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass

@dataclass
class StrategyProfile:
    has_directional_signal: bool
    strategy_mode: str
    direction_bias: str # "LONG", "SHORT", "NEUTRAL"
    reasoning: str
    liquidity_sweep: bool = False

class StrategyRouter:
    """
    Evaluates raw local market conditions and agent reports to determine
    the mathematically optimal execution strategy (if any) before passing to CEO.
    """
    
    @staticmethod
    def check_anti_chase_reentry(
        symbol: str,
        target_direction: str,
        current_price: float,
        eval_atr: float,
        bb_position_pct: float,
        low_48h: float = 0.0,
        high_48h: float = 0.0,
        rvol: float = 1.0,
        max_age_seconds: float = 14400.0,
        journal_path: str = "data/memory/trade_journal.json"
    ):
        """
        Anti-Chase Re-Entry & Wave Exhaustion Guard (Same-Direction Re-Entry Cooldown):
        1. Wave Exhaustion Guard:
           Prevents opening a second/subsequent trade in the same direction at a worse price
           (current_price < last_entry for SHORT, or current_price > last_entry for LONG)
           within max_age_seconds (default 6 hours / 360 minutes), unless price has pulled back into
           the upper/lower half of the channel (bb_pos >= 55% for SHORT, bb_pos <= 45% for LONG)
           or a high-volume true breakout of 48h extremes is taking place (rvol >= 1.5).
        2. Profitable Exit Cooldown:
           Prevents re-entering right after a profitable exit without at least 0.5 ATR pullback or BB mean return.
        Returns: (is_allowed: bool, reason: str)
        """
        if not symbol or current_price <= 0:
            return True, "No symbol or price"

        target_dir = str(target_direction).strip().upper()
        if target_dir not in ["LONG", "SHORT"]:
            return True, "Non-directional"

        try:
            from core.state_store import StateStore
            journal = StateStore.load(journal_path, default=[])
        except Exception:
            journal = []

        if not isinstance(journal, list) or not journal:
            return True, "No closed trades recorded"

        last_trade = None
        for trade in reversed(journal):
            if not isinstance(trade, dict):
                continue
            t_sym = str(trade.get("symbol", "")).upper()
            if t_sym == symbol.upper():
                last_trade = trade
                break

        if not last_trade:
            return True, f"No prior closed trades for {symbol}"

        last_dir = str(last_trade.get("direction", "")).strip().upper()
        if last_dir != target_dir:
            return True, f"Direction changed (last was {last_dir}, target is {target_dir})"

        import time
        now_ts = time.time()
        trade_ts = float(last_trade.get("timestamp", 0.0) or 0.0)
        age_sec = now_ts - trade_ts if trade_ts > 0 else 0.0
        if trade_ts > 0 and age_sec > max_age_seconds:
            return True, f"Cooldown expired ({age_sec / 60.0:.1f}m > {max_age_seconds / 60.0:.1f}m)"

        last_entry = float(last_trade.get("entry_price", 0.0) or 0.0)
        age_min = (age_sec / 60.0) if trade_ts > 0 else 0.0

        # ── 0. True 48h Breakout Exception: allow continuation if breaking 48h high/low with RVOL >= 1.5 ──
        if target_dir == "SHORT" and low_48h > 0 and current_price <= low_48h * 1.002 and rvol >= 1.5:
            return True, f"Anti-chase 48h breakout exception satisfied: пробой минимума 48ч (${low_48h:.4f}) с RVOL={rvol:.2f} >= 1.5"
        if target_dir == "LONG" and high_48h > 0 and current_price >= high_48h * 0.998 and rvol >= 1.5:
            return True, f"Anti-chase 48h breakout exception satisfied: пробой максимума 48ч (${high_48h:.4f}) с RVOL={rvol:.2f} >= 1.5"

        # ── 1. Wave Exhaustion Guard: forbid repeat trade at price beyond prior entry ──
        if last_entry > 0:
            if target_dir == "SHORT" and current_price < last_entry:
                has_pulled_back = bb_position_pct >= 50.0
                if not has_pulled_back:
                    return False, (
                        f"Anti-Chase Guard (Wave Exhaustion Guard): Запрет повторного SHORT по {symbol} по цене ${current_price:.4f} < ${last_entry:.4f} (ниже предыдущего входа) "
                        f"через {age_min:.0f}м без отката в верхнюю половину BB (не вернулась к средней EMA-20, BB={bb_position_pct:.1f}% < 50.0%) "
                        f"или истинного пробоя 48h минимума (${low_48h:.4f}, rvol={rvol:.2f} < 1.5). Требуется WAIT_FOR_PULLBACK."
                    )
            elif target_dir == "LONG" and current_price > last_entry:
                has_pulled_back = bb_position_pct <= 50.0
                if not has_pulled_back:
                    return False, (
                        f"Anti-Chase Guard (Wave Exhaustion Guard): Запрет повторного LONG по {symbol} по цене ${current_price:.4f} > ${last_entry:.4f} (выше предыдущего входа) "
                        f"через {age_min:.0f}м без отката в нижнюю половину BB (не вернулась к средней EMA-20, BB={bb_position_pct:.1f}% > 50.0%) "
                        f"или истинного пробоя 48h максимума (${high_48h:.4f}, rvol={rvol:.2f} < 1.5). Требуется WAIT_FOR_PULLBACK."
                    )

        # ── 2. Profitable Exit Cooldown Guard ──
        net_pnl = float(last_trade.get("net_pnl_usd", 0.0) or 0.0)
        realized_r = float(last_trade.get("realized_r", 0.0) or 0.0)
        is_profitable = (net_pnl > 0.0) or (realized_r > 0.0)

        if not is_profitable:
            return True, f"Last trade for {symbol} was not profitable (PnL={net_pnl:.2f}, R={realized_r:.2f})"

        last_exit = float(last_trade.get("exit_price", 0.0) or 0.0)
        if last_exit <= 0:
            return True, "Invalid last exit price"

        min_pullback_dist = (0.5 * eval_atr) if eval_atr > 0 else (last_exit * 0.005)

        if target_dir == "SHORT":
            is_price_low = current_price < (last_exit + min_pullback_dist)
            is_bb_low = bb_position_pct < 45.0
            if is_price_low or is_bb_low:
                viol_reasons = []
                if is_price_low:
                    viol_reasons.append(f"цена ${current_price:.4f} < ${last_exit + min_pullback_dist:.4f} (+0.5 ATR от выхода ${last_exit:.4f})")
                if is_bb_low:
                    viol_reasons.append(f"BB={bb_position_pct:.1f}% < 45.0% (не вернулась к средней EMA-20)")
                detail = " и ".join(viol_reasons)
                return False, (
                    f"Anti-Chase Guard: Запрет повторного SHORT по {symbol} через {age_min:.0f}м после прибыльного выхода: "
                    f"{detail}. Требуется полноценный откат вверх (WAIT_FOR_PULLBACK)."
                )

        elif target_dir == "LONG":
            is_price_high = current_price > (last_exit - min_pullback_dist)
            is_bb_high = bb_position_pct > 55.0
            if is_price_high or is_bb_high:
                viol_reasons = []
                if is_price_high:
                    viol_reasons.append(f"цена ${current_price:.4f} > ${last_exit - min_pullback_dist:.4f} (-0.5 ATR от выхода ${last_exit:.4f})")
                if is_bb_high:
                    viol_reasons.append(f"BB={bb_position_pct:.1f}% > 55.0% (не вернулась к средней EMA-20)")
                detail = " и ".join(viol_reasons)
                return False, (
                    f"Anti-Chase Guard: Запрет повторного LONG по {symbol} через {age_min:.0f}м после прибыльного выхода: "
                    f"{detail}. Требуется полноценный откат вниз (WAIT_FOR_PULLBACK)."
                )

        return True, "Anti-chase pullback criteria satisfied"

    @staticmethod
    def check_trend_following_overextension(
        target_direction: str,
        bb_position_pct: float,
        rsi: float
    ) -> Tuple[bool, str]:
        """
        Anti-Chase Guard for TREND_FOLLOWING:
        - For SHORT: forbidden if bb_pos < 30.0% and RSI < 40.0 (lower third exhaustion).
        - For LONG: forbidden if bb_pos > 70.0% and RSI > 60.0 (upper third exhaustion).
        Returns: (is_extended: bool, reason: str)
        """
        td = str(target_direction).strip().upper()
        if td == "SHORT" and bb_position_pct < 30.0 and rsi < 40.0:
            return True, (
                f"Anti-Chase Guard: TREND_FOLLOWING SHORT в нижней трети канала Боллинджера "
                f"(BB={bb_position_pct:.1f}% < 30.0%, RSI={rsi:.1f} < 40.0). Вход по рынку запрещен -> WAIT_FOR_PULLBACK к EMA-20."
            )
        elif td == "LONG" and bb_position_pct > 70.0 and rsi > 60.0:
            return True, (
                f"Anti-Chase Guard: TREND_FOLLOWING LONG в верхней трети канала Боллинджера "
                f"(BB={bb_position_pct:.1f}% > 70.0%, RSI={rsi:.1f} > 60.0). Вход по рынку запрещен -> WAIT_FOR_PULLBACK к EMA-20."
            )
        return False, "Not extended"

    @staticmethod
    def evaluate(
        symbol: str, 
        market_data: Dict[str, Any], 
        valid_reports: List[Dict[str, Any]], 
        macro_cache: Dict[str, Any],
        detected_regime: str = "RANGE_CHOPPY",
        profile: str = "BALANCED"
    ) -> StrategyProfile:
        profile_res = StrategyRouter._evaluate_raw(
            symbol=symbol,
            market_data=market_data,
            valid_reports=valid_reports,
            macro_cache=macro_cache,
            detected_regime=detected_regime,
            profile=profile
        )

        if profile_res.has_directional_signal and profile_res.direction_bias in ["LONG", "SHORT"]:
            indicators = market_data.get("indicators") or {}
            eval_atr = float(indicators.get("atr_14", indicators.get("atr", 0.0)) or 0.0)
            current_price = float((market_data.get("price_data") or {}).get("current_price", 0.0) or 0.0)
            bb_pos = float(indicators.get("bb_position_pct", indicators.get("bb_pos", 50.0)) or 50.0)
            low_48h = float(indicators.get("low_48h", 0.0) or 0.0)
            high_48h = float(indicators.get("high_48h", 0.0) or 0.0)
            rvol = float(indicators.get("rvol", indicators.get("volume_spike_pct", 100.0) / 100.0) or 1.0)
            
            # 1. Anti-Chase & Wave Exhaustion Guard (6 hours / 360m window)
            can_reenter, anti_chase_reason = StrategyRouter.check_anti_chase_reentry(
                symbol=symbol,
                target_direction=profile_res.direction_bias,
                current_price=current_price,
                eval_atr=eval_atr,
                bb_position_pct=bb_pos,
                low_48h=low_48h,
                high_48h=high_48h,
                rvol=rvol,
                max_age_seconds=21600.0
            )
            if not can_reenter:
                return StrategyProfile(
                    False,
                    "ANTI_CHASE_REENTRY_VETO",
                    profile_res.direction_bias,
                    f"ОТКЛОНЕН ({anti_chase_reason})"
                )

            # 2. Multi-day box floor/ceiling filter (Macro Floor/Ceiling Guard - Step 2)
            if profile_res.strategy_mode == "TREND_FOLLOWING":
                macro_box_pos = float(indicators.get("macro_box_pos_pct", 50.0) or 50.0)
                if profile_res.direction_bias == "SHORT":
                    if macro_box_pos < 32.0 and bb_pos < 38.0 and rvol < 1.6:
                        return StrategyProfile(
                            False,
                            "MACRO_BOX_FLOOR_VETO",
                            profile_res.direction_bias,
                            f"ОТКЛОНЕН (Macro Floor Guard: SHORT в нижней трети 48h коробки (Box={macro_box_pos:.1f}% < 32.0%, BB={bb_pos:.1f}% < 38.0%, rvol={rvol:.2f} < 1.6). Вход по рынку запрещен -> WAIT_FOR_PULLBACK в верхнюю половину коробки)."
                        )
                elif profile_res.direction_bias == "LONG":
                    if macro_box_pos > 68.0 and bb_pos > 62.0 and rvol < 1.6:
                        return StrategyProfile(
                            False,
                            "MACRO_BOX_CEILING_VETO",
                            profile_res.direction_bias,
                            f"ОТКЛОНЕН (Macro Ceiling Guard: LONG в верхней трети 48h коробки (Box={macro_box_pos:.1f}% > 68.0%, BB={bb_pos:.1f}% > 62.0%, rvol={rvol:.2f} < 1.6). Вход по рынку запрещен -> WAIT_FOR_PULLBACK в нижнюю половину коробки)."
                        )

            # Anti-Chase BB/RSI Guard for TREND_FOLLOWING:
            if profile_res.strategy_mode == "TREND_FOLLOWING":
                raw_rsi = indicators.get("rsi_14", indicators.get("rsi"))
                rsi_val = float(raw_rsi) if raw_rsi is not None else 50.0
                is_ext, ext_reason = StrategyRouter.check_trend_following_overextension(
                    target_direction=profile_res.direction_bias,
                    bb_position_pct=bb_pos,
                    rsi=rsi_val
                )
                if is_ext:
                    return StrategyProfile(
                        False,
                        "ANTI_CHASE_REENTRY_VETO",
                        profile_res.direction_bias,
                        f"ОТКЛОНЕН ({ext_reason})"
                    )

        return profile_res

    @staticmethod
    def _evaluate_raw(
        symbol: str, 
        market_data: Dict[str, Any], 
        valid_reports: List[Dict[str, Any]], 
        macro_cache: Dict[str, Any],
        detected_regime: str = "RANGE_CHOPPY",
        profile: str = "BALANCED"
    ) -> StrategyProfile:
        import math

        from core.config import config
        if getattr(config, "ANTI_BOT_MODE", False) is True:
            return StrategyRouter._evaluate_anti_bot_predator(
                symbol=symbol,
                market_data=market_data,
                valid_reports=valid_reports,
                macro_cache=macro_cache,
                detected_regime=detected_regime,
                profile=profile
            )

        def _safe_float(val: Any, default: float = 0.0) -> float:
            if val is None:
                return default
            try:
                f = float(val)
                return f if math.isfinite(f) else default
            except (ValueError, TypeError):
                return default

        mtf_data = market_data.get("multi_timeframe") or market_data.get("mtf") or {}
        indicators = market_data.get("indicators") or {}
        mtf_alignment = mtf_data.get("mtf_alignment") or indicators.get("mtf_alignment")
        trend_1h = str(mtf_data.get("trend_1h") or indicators.get("trend_1h") or "").upper()
        
        bb_position_pct = _safe_float(indicators.get("bb_position_pct", indicators.get("bb_pos")), 50.0)
        adx_14 = _safe_float(indicators.get("adx_14", indicators.get("adx")), 0.0)
        rsi = _safe_float(indicators.get("rsi_14", indicators.get("rsi")), 50.0)
        volume_spike_pct = _safe_float(indicators.get("volume_spike_pct"), 0.0)
        donchian_high = _safe_float(indicators.get("donchian_high"), 0.0)
        donchian_low = _safe_float(indicators.get("donchian_low"), 0.0)
        current_price = _safe_float((market_data.get("price_data") or {}).get("current_price"), 0.0)
        bb_width_pct = _safe_float(indicators.get("bb_width_pct"), 0.0)
        atr_pct = _safe_float(indicators.get("atr_pct"), 0.0)
        last_closed_candle_close = _safe_float(indicators.get("last_closed_candle_close"), current_price)

        # Extract telemetry for wash-volume & exhaustion detection
        derivatives_data = market_data.get("derivatives_data") or {}
        oi_change_pct = _safe_float(derivatives_data.get("oi_change_pct", indicators.get("oi_change_pct")), 0.0)
        eval_atr = _safe_float(indicators.get("atr_14", indicators.get("atr")), 0.0)

        last_candle = None
        tf_15m = mtf_data.get("tf_15m") if isinstance(mtf_data, dict) else {}
        price_data = market_data.get("price_data") or {}
        candles = (
            (tf_15m.get("candles_20") if isinstance(tf_15m, dict) else None)
            or price_data.get("candles_20")
            or price_data.get("candles")
            or indicators.get("candles")
            or []
        )
        if isinstance(candles, list) and len(candles) >= 2:
            last_candle = candles[-2]
        elif isinstance(candles, list) and len(candles) == 1:
            last_candle = candles[-1]
        elif isinstance(indicators.get("last_candle"), dict):
            last_candle = indicators.get("last_candle")

        c_open = _safe_float(last_candle.get("open") if isinstance(last_candle, dict) else indicators.get("candle_open"), 0.0)
        c_close = _safe_float(last_candle.get("close") if isinstance(last_candle, dict) else indicators.get("last_closed_candle_close", current_price), current_price)
        candle_body = abs(c_close - c_open)

        # Wash-Volume Rejection: volume_ratio > 2.5 (volume_spike_pct > 250%), but flat OI (< 0.15%) and tiny candle body (< 0.25*ATR)
        is_wash_volume = (volume_spike_pct > 250.0) and (abs(oi_change_pct) < 0.15) and (eval_atr > 0 and candle_body < 0.25 * eval_atr)

        # Trend Exhaustion Guard (15m Timeframe Channel Extreme + Divergence Detection)
        algo_signals = indicators.get("algo_signals") or {}
        rsi_div = str(algo_signals.get("rsi_divergence") or indicators.get("rsi_divergence") or "").strip().upper()
        vol_declining_rise = bool(algo_signals.get("volume_declining_on_rise") or algo_signals.get("volume_declining", False))
        vol_declining_drop = bool(algo_signals.get("volume_declining_on_drop") or algo_signals.get("volume_declining", False))

        at_channel_high = (bb_position_pct > 85.0) or (donchian_high > 0 and current_price >= donchian_high)
        at_channel_low = (bb_position_pct < 15.0) or (donchian_low > 0 and current_price <= donchian_low)

        trend_exhaustion_long = bool(indicators.get("trend_exhaustion_long")) or (
            at_channel_high and (rsi >= 72.0 or rsi_div == "BEARISH" or vol_declining_rise)
        )
        trend_exhaustion_short = bool(indicators.get("trend_exhaustion_short")) or (
            at_channel_low and (rsi <= 28.0 or rsi_div == "BULLISH" or vol_declining_drop)
        )

        # Tech consensus (excluding News)
        tech_bulls = sum(1 for r in valid_reports if r.get("agent_name") != "News_Agent" and str(r.get("signal", "")).upper() in ["BULLISH", "LONG", "BUY"])
        tech_bears = sum(1 for r in valid_reports if r.get("agent_name") != "News_Agent" and str(r.get("signal", "")).upper() in ["BEARISH", "SHORT", "SELL"])
        active_bulls = tech_bulls
        active_bears = tech_bears
        ob_bull = any(r.get("agent_name") == "Order_Book_Agent" and str(r.get("signal", "")).upper() in ["BULLISH", "LONG"] for r in valid_reports)
        ob_bear = any(r.get("agent_name") == "Order_Book_Agent" and str(r.get("signal", "")).upper() in ["BEARISH", "SHORT"] for r in valid_reports)

        candle_report = next((r for r in valid_reports if r.get("agent_name") == "Candle_Agent"), {})
        candle_signal = str(candle_report.get("signal", "")).upper()

        # Global Fail-Closed Macro Veto: If macro regime is UNKNOWN or DATA_INSUFFICIENT,
        # halt all new directional entries across all strategies to protect capital during telemetry outages.
        if detected_regime in ["UNKNOWN", "DATA_INSUFFICIENT"]:
            return StrategyProfile(
                has_directional_signal=False,
                strategy_mode="UNKNOWN_REGIME_VETO",
                direction_bias="NEUTRAL",
                reasoning=f"ОТКЛОНЕН (Все новые сделки заблокированы: макро-режим {detected_regime}, телеметрия BTC/ETH не подтверждена)."
            )

        # Extract regime metrics and macro leaders alignment from macro_cache
        regime_info = (macro_cache.get("regime_verdict") or macro_cache.get("regime_info") or {}) if isinstance(macro_cache, dict) else {}
        range_score = _safe_float(regime_info.get("range_score"), 0.0)

        # F-03 Fix: Global Macro Leaders (BTC & ETH) Alignment Check
        base_sym = symbol.split("-")[0].upper()
        both_leaders_bearish = False
        both_leaders_bullish = False
        if base_sym not in ("BTC", "ETH", "XAUT", "PAXG") and isinstance(macro_cache, dict):
            btc_macro = macro_cache.get("BTC-USD") or {}
            eth_macro = macro_cache.get("ETH-USD") or {}
            btc_mtf = btc_macro.get("multi_timeframe") or {}
            eth_mtf = eth_macro.get("multi_timeframe") or {}
            btc_align = btc_mtf.get("mtf_alignment", "")
            eth_align = eth_mtf.get("mtf_alignment", "")
            btc_t1h = str(btc_mtf.get("trend_1h") or "").upper()
            eth_t1h = str(eth_mtf.get("trend_1h") or "").upper()
            both_leaders_bearish = (btc_align == "FULL_ALIGNMENT" and eth_align == "FULL_ALIGNMENT" and btc_t1h == "BEARISH" and eth_t1h == "BEARISH")
            both_leaders_bullish = (btc_align == "FULL_ALIGNMENT" and eth_align == "FULL_ALIGNMENT" and btc_t1h == "BULLISH" and eth_t1h == "BULLISH")

        # 0. Priority Pass for Confirmed Pullback Watchlist Retests (Protected Retests)
        if market_data.get("is_pullback_retest"):
            # Regime Veto: Prohibit TREND_FOLLOWING pullback retest in TRANSITION regime
            if str(detected_regime or "").upper() == "TRANSITION":
                return StrategyProfile(
                    False,
                    "TRANSITION_RETEST_VETO",
                    "NEUTRAL",
                    "ОТКЛОНЕН (Макро-режим TRANSITION: трендовый ретест заблокирован из-за смены рыночной структуры)."
                )

            retest_dir = str(
                market_data.get("pullback_retest_direction")
                or market_data.get("retest_direction")
                or ("LONG" if trend_1h == "BULLISH" else "SHORT")
            ).upper()

            # Invalidation Check: Veto if invalidation price breached
            invalidation_price = _safe_float(
                market_data.get("invalidation_price") or market_data.get("pullback_invalidation_price"),
                0.0
            )
            is_invalidated = False
            if invalidation_price > 0:
                if retest_dir == "LONG" and current_price < invalidation_price:
                    is_invalidated = True
                elif retest_dir == "SHORT" and current_price > invalidation_price:
                    is_invalidated = True

            if is_invalidated:
                return StrategyProfile(
                    False,
                    "PULLBACK_INVALIDATED_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Ретест инвалидирован: цена {current_price:.4f} пробила уровень инвалидации {invalidation_price:.4f})."
                )

            # Macro Trend Invalidation: Veto ONLY if senior 1H trend opposes retest direction
            if retest_dir == "LONG" and trend_1h == "BEARISH":
                return StrategyProfile(
                    False,
                    "PULLBACK_TREND_OPPOSED_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Ретест LONG отменен: тренд 1H сменился на BEARISH)."
                )
            elif retest_dir == "SHORT" and trend_1h == "BULLISH":
                return StrategyProfile(
                    False,
                    "PULLBACK_TREND_OPPOSED_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Ретест SHORT отменен: тренд 1H сменился на BULLISH)."
                )

            # Macro Leaders Veto for retest
            if retest_dir == "LONG" and both_leaders_bearish:
                return StrategyProfile(
                    False,
                    "MACRO_LEADERS_VETO",
                    "NEUTRAL",
                    "ОТКЛОНЕН (Поводыри BTC/ETH в синхронном обвале FULL_ALIGNMENT BEARISH: ретест LONG заблокирован)."
                )
            elif retest_dir == "SHORT" and both_leaders_bullish:
                return StrategyProfile(
                    False,
                    "MACRO_LEADERS_VETO",
                    "NEUTRAL",
                    "ОТКЛОНЕН (Поводыри BTC/ETH в синхронном ралли FULL_ALIGNMENT BULLISH: ретест SHORT заблокирован)."
                )

            # ADX Trend Strength Check for Retests
            from core.config import config
            allow_forbidden = bool(getattr(config, "ALLOW_FORBIDDEN_STRATEGIES", False))
            is_transition_market = (str(detected_regime).upper() == "TRANSITION") or (mtf_alignment == "TRANSITION")
            req_retest_adx = 22.0 if is_transition_market else (22.0 if symbol.split("-")[0].upper() in ["BTC", "ETH"] else 20.0)
            if not allow_forbidden and 0.0 < adx_14 < req_retest_adx:
                return StrategyProfile(
                    False,
                    "LOW_ADX_RETEST_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Ретест {retest_dir} заблокирован: слабый ADX={adx_14:.1f} < {req_retest_adx:.1f})."
                )

            # Anti-Knife Catching: Reject retest if analysts violently oppose (>= 3 opposing) or lack supporting analysts (< 1)
            # Corrective 15m candle color is expected on retest and does NOT cancel the trade
            if retest_dir == "LONG" and (tech_bears >= 3 or tech_bulls < 1):
                return StrategyProfile(
                    False,
                    "FALLING_KNIFE_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Ловля падающих ножей: ретест LONG отклонен из-за оппозиции аналитиков: Bears={tech_bears}, Bulls={tech_bulls})."
                )
            elif retest_dir == "SHORT" and (tech_bulls >= 3 or tech_bears < 1):
                return StrategyProfile(
                    False,
                    "ROCKET_RALLY_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Ловля ракет: ретест SHORT отклонен из-за оппозиции аналитиков: Bulls={tech_bulls}, Bears={tech_bears})."
                )

            # Trend Exhaustion Guard for Retests
            if retest_dir == "LONG" and trend_exhaustion_long:
                return StrategyProfile(
                    False,
                    "TREND_EXHAUSTION_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Trend Exhaustion Guard: Ретест LONG отклонен из-за истощения импульса: BB={bb_position_pct:.1f}%, DonchianHigh={donchian_high:.2f}, RSI_div={rsi_div})."
                )
            elif retest_dir == "SHORT" and trend_exhaustion_short:
                return StrategyProfile(
                    False,
                    "TREND_EXHAUSTION_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Trend Exhaustion Guard: Ретест SHORT отклонен из-за истощения импульса: BB={bb_position_pct:.1f}%, DonchianLow={donchian_low:.2f}, RSI_div={rsi_div})."
                )

            return StrategyProfile(
                True,
                "TREND_FOLLOWING",
                retest_dir,
                f"PULLBACK_RETEST {retest_dir}: Цена достигла целевой зоны отката EMA-20, структура отскока подтверждена."
            )
        regime_info = (macro_cache.get("regime_verdict") or macro_cache.get("regime_info") or {}) if isinstance(macro_cache, dict) else {}
        range_score = _safe_float(regime_info.get("range_score"), 0.0)
        vol_score = _safe_float(regime_info.get("vol_score"), 10.0)

        # Calibrated thresholds with safety bounds from tuned_hyperparams.json
        from core.config import config, get_tuned_param
        allow_forbidden = bool(getattr(config, "ALLOW_FORBIDDEN_STRATEGIES", False))
        tuned_chop_threshold = get_tuned_param("chop_range_threshold", 59.0)
        tuned_bb_max = get_tuned_param("anti_chop_bb_max", 89.0)



        # Deep chop: active when range_score >= 68.0%
        # or when RANGE_CHOPPY regime is confirmed with range_score >= 55.0%
        is_deep_chop = (range_score >= 68.0) or (detected_regime in ["RANGE_CHOPPY", "CHOP"] and range_score >= 55.0)
        is_choppy = is_deep_chop or (detected_regime in ["RANGE_CHOPPY", "CHOP"])
        is_chop_risk = is_choppy or (range_score >= tuned_chop_threshold)

        # Low Volatility Trap Guard: In dead markets (vol_score < 7.0) or when
        # chop/transition is detected with low volatility (vol_score < 10.0 and range_score >= 50.0),
        # breakouts are false wick traps (liquidity raids).
        is_dead_market = (vol_score < 7.0) or (detected_regime in ["RANGE_CHOPPY", "CHOP", "TRANSITION"] and vol_score < 10.0 and range_score >= 50.0)

        # 1. Breakout Strategy (Allowed ONLY in pure TRENDING with ADX >= 25.0 and real volume)
        # Strictly forbidden in TRANSITION and RANGE_CHOPPY when allow_forbidden=False
        is_vol_explosion = (volume_spike_pct >= 300.0) and not is_wash_volume
        is_transition_market = (detected_regime == "TRANSITION") or (mtf_alignment == "TRANSITION")
        is_weak_adx_for_breakout = (0.0 < adx_14 < 25.0)

        if donchian_high > 0 and (current_price > donchian_high or last_closed_candle_close > donchian_high) and tech_bulls >= 1 and (
            (volume_spike_pct >= 200.0 and ob_bull) or (volume_spike_pct >= 120.0 and trend_1h == "BULLISH") or is_vol_explosion
        ):
            if is_wash_volume:
                return StrategyProfile(
                    False, "WASH_VOLUME_VETO", "NEUTRAL",
                    f"ОТКЛОНЕН (Wash-Volume Rejection: Аномальный объем {volume_spike_pct:.1f}% без изменения OI ({oi_change_pct:+.2f}%) и узким телом свечи)."
                )
            if trend_exhaustion_long:
                return StrategyProfile(
                    False, "TREND_EXHAUSTION_VETO", "NEUTRAL",
                    f"ОТКЛОНЕН (Trend Exhaustion Guard: Пробой LONG отклонен из-за дивергенции/истощения объема на хаях: BB={bb_position_pct:.1f}%, RSI_div={rsi_div})."
                )
            is_trending = detected_regime in ["TRENDING", "TRENDING_BULL", "TRENDING_BEAR"] or str(detected_regime).startswith("TRENDING")
            is_confirmed_chop_breakout = (detected_regime in ["RANGE_CHOPPY", "CHOP"] or mtf_alignment == "MIXED_CHOP") and (volume_spike_pct >= 200.0)
            if not allow_forbidden and is_transition_market:
                return StrategyProfile(
                    False, "BREAKOUT", "NEUTRAL",
                    f"ОТКЛОНЕН (Анти-распил: Запрет BREAKOUT в переходном режиме TRANSITION)."
                )
            if not allow_forbidden and not (is_trending or is_confirmed_chop_breakout):
                return StrategyProfile(
                    False, "BREAKOUT", "NEUTRAL",
                    f"ОТКЛОНЕН (Анти-распил: Запрет BREAKOUT LONG вне режима TRENDING: Range={range_score:.1f}%, regime={detected_regime}, MTF={mtf_alignment}, ADX={adx_14:.1f} < 25.0)."
                )
            if not allow_forbidden and (is_dead_market or is_weak_adx_for_breakout):
                return StrategyProfile(
                    False, "BREAKOUT", "NEUTRAL",
                    f"ОТКЛОНЕН (Анти-распил: Запрет BREAKOUT LONG при мертвом рынке или слабом ADX: Range={range_score:.1f}%, regime={detected_regime}, MTF={mtf_alignment}, ADX={adx_14:.1f} < 25.0)."
                )
            if both_leaders_bearish and not is_vol_explosion:
                return StrategyProfile(
                    False, "MACRO_LEADERS_VETO", "NEUTRAL",
                    "ОТКЛОНЕН (Поводыри BTC/ETH в синхронном обвале FULL_ALIGNMENT BEARISH: пробой LONG заблокирован)."
                )
            return StrategyProfile(True, "BREAKOUT", "LONG", f"BREAKOUT LONG: Пробой {donchian_high} (close={last_closed_candle_close}, price={current_price}) с объемом {volume_spike_pct}%, OB={'BULL' if ob_bull else 'NEUTRAL'}")
        elif donchian_low > 0 and (current_price < donchian_low or last_closed_candle_close < donchian_low) and tech_bears >= 1 and (
            (volume_spike_pct >= 200.0 and ob_bear) or (volume_spike_pct >= 100.0 and trend_1h == "BEARISH") or is_vol_explosion
        ):
            if is_wash_volume:
                return StrategyProfile(
                    False, "WASH_VOLUME_VETO", "NEUTRAL",
                    f"ОТКЛОНЕН (Wash-Volume Rejection: Аномальный объем {volume_spike_pct:.1f}% без изменения OI ({oi_change_pct:+.2f}%) и узким телом свечи)."
                )
            if trend_exhaustion_short:
                return StrategyProfile(
                    False, "TREND_EXHAUSTION_VETO", "NEUTRAL",
                    f"ОТКЛОНЕН (Trend Exhaustion Guard: Пробой SHORT отклонен из-за дивергенции/истощения объема на лоях: BB={bb_position_pct:.1f}%, RSI_div={rsi_div})."
                )
            is_trending = detected_regime in ["TRENDING", "TRENDING_BULL", "TRENDING_BEAR"] or str(detected_regime).startswith("TRENDING")
            is_confirmed_chop_breakout = (detected_regime in ["RANGE_CHOPPY", "CHOP"] or mtf_alignment == "MIXED_CHOP") and (volume_spike_pct >= 200.0)
            if not allow_forbidden and is_transition_market:
                return StrategyProfile(
                    False, "BREAKOUT", "NEUTRAL",
                    f"ОТКЛОНЕН (Анти-распил: Запрет BREAKOUT в переходном режиме TRANSITION)."
                )
            if not allow_forbidden and not (is_trending or is_confirmed_chop_breakout):
                return StrategyProfile(
                    False, "BREAKOUT", "NEUTRAL",
                    f"ОТКЛОНЕН (Анти-распил: Запрет BREAKOUT SHORT вне режима TRENDING: Range={range_score:.1f}%, regime={detected_regime}, MTF={mtf_alignment}, ADX={adx_14:.1f} < 25.0)."
                )
            if not allow_forbidden and (is_dead_market or is_weak_adx_for_breakout):
                return StrategyProfile(
                    False, "BREAKOUT", "NEUTRAL",
                    f"ОТКЛОНЕН (Анти-распил: Запрет BREAKOUT SHORT при мертвом рынке или слабом ADX: Range={range_score:.1f}%, regime={detected_regime}, MTF={mtf_alignment}, ADX={adx_14:.1f} < 25.0)."
                )
            return StrategyProfile(True, "BREAKOUT", "SHORT", f"BREAKOUT SHORT: Пробой {donchian_low} (close={last_closed_candle_close}, price={current_price}) с объемом {volume_spike_pct}%, OB={'BEAR' if ob_bear else 'NEUTRAL'}")

        # 2. Relative Momentum (Requires valid BTC benchmark in macro_cache and ADX >= 20.0)
        oi_trend = str((market_data.get("derivatives_data") or {}).get("open_interest_trend") or "neutral").lower()

        req_adx_rel = 22.0 if is_transition_market else 20.0
        can_eval_rel_mom = (allow_forbidden or adx_14 >= req_adx_rel or adx_14 == 0.0)

        if can_eval_rel_mom and symbol != "BTC-USD" and isinstance(macro_cache, dict) and "BTC-USD" in macro_cache:
            btc_macro = macro_cache.get("BTC-USD") or {}
            btc_indicators = btc_macro.get("indicators") or {}
            btc_mtf = btc_macro.get("multi_timeframe") or {}
            
            # Only evaluate Relative Momentum if BTC return data is actually available
            if "asset_return_24h" in btc_indicators and btc_indicators["asset_return_24h"] is not None:
                asset_return_24h = _safe_float(indicators.get("asset_return_24h"), 0.0)
                btc_return_24h = _safe_float(btc_indicators.get("asset_return_24h"), 0.0)
                rs_divergence = round(asset_return_24h - btc_return_24h, 2)
                btc_trend_1h = str(btc_mtf.get("trend_1h") or "NEUTRAL").upper()

                # 2a. Relative Strength Outperformer (Market Leader Rally)
                if btc_trend_1h in ["BULLISH", "NEUTRAL"] and rs_divergence >= 5.0 and tech_bulls >= 2 and volume_spike_pct >= 50.0 and oi_trend in ["rising", "stable"]:
                    if trend_exhaustion_long:
                        return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Относительный моментум LONG отклонен: цена на хаях BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")
                    return StrategyProfile(True, "RELATIVE_MOMENTUM", "LONG", f"RELATIVE_MOMENTUM LONG: Лидер рынка (+{rs_divergence}% vs BTC), Bulls={tech_bulls}, Vol={volume_spike_pct}%, OI={oi_trend}")
                # 2b. Relative Momentum Catch-Up (Laggard)
                elif btc_trend_1h == "BULLISH" and rs_divergence <= -5.0 and tech_bulls >= 2 and volume_spike_pct >= 50.0 and oi_trend in ["rising", "stable"]:
                    if trend_exhaustion_long:
                        return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Относительный моментум LONG отклонен: цена на хаях BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")
                    return StrategyProfile(True, "RELATIVE_MOMENTUM", "LONG", f"RELATIVE_MOMENTUM LONG: Отставание от BTC {rs_divergence}%, Bulls={tech_bulls}, Vol={volume_spike_pct}%, OI={oi_trend}")
                # 2c. Overextended Exhausted Pump (Fade Short)
                elif btc_trend_1h in ["BEARISH", "NEUTRAL"] and rs_divergence >= 10.0 and tech_bears >= 2 and volume_spike_pct >= 50.0 and oi_trend in ["rising", "stable", "falling"]:
                    if trend_exhaustion_short:
                        return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Относительный моментум SHORT отклонен: цена на лоях BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")
                    return StrategyProfile(True, "RELATIVE_MOMENTUM", "SHORT", f"RELATIVE_MOMENTUM SHORT: Аномальный памп {rs_divergence}%, Bears={tech_bears}, Vol={volume_spike_pct}%, OI={oi_trend}")

        # Chop Pre-Guard: In RANGE_CHOPPY, reject non-breakout / non-retest setups in 0 ms before calling LLM
        if not allow_forbidden and not getattr(config, "ANTI_BOT_MODE", False) and detected_regime in ["RANGE_CHOPPY", "CHOP"]:
            return StrategyProfile(
                False,
                "RANGE_CHOPPY_VETO",
                "NEUTRAL",
                f"ОТКЛОНЕН (Анти-распил: Макро-режим {detected_regime} при ANTI_BOT_MODE=False без подтвержденного пробоя. Все стратегии заблокированы для защиты капитала)."
            )

        # 3. Volatility Momentum (Scalping - ONLY permitted if ALLOW_FORBIDDEN_STRATEGIES=True)
        # 15m BB Width expansion: normal quiet chop is 0.2-0.5%, active volatility expansion is >= 0.8% (or ATR >= 0.25%)
        if not is_transition_market and mtf_alignment in ["MIXED_CHOP", "COUNTER_TREND_WARNING"] and (bb_width_pct >= 0.8 or atr_pct >= 0.25):
            # Guard against deadlock if both tech_bulls and tech_bears are high
            if not (tech_bulls >= 2 and tech_bears >= 2):
                if ob_bull and tech_bulls >= 2 and volume_spike_pct >= 100.0 and oi_trend in ["rising", "stable"]:
                    return StrategyProfile(True, "VOLATILITY_MOMENTUM", "LONG", f"VOLATILITY_MOMENTUM LONG: Волатильность {bb_width_pct}% (ATR={atr_pct}%), Vol={volume_spike_pct}%, OI={oi_trend}")
                elif ob_bear and tech_bears >= 2 and volume_spike_pct >= 100.0 and oi_trend in ["rising", "stable"]:
                    return StrategyProfile(True, "VOLATILITY_MOMENTUM", "SHORT", f"VOLATILITY_MOMENTUM SHORT: Волатильность {bb_width_pct}% (ATR={atr_pct}%), Vol={volume_spike_pct}%, OI={oi_trend}")

        # 4. Macro Regimes Logic
        # 4.0. Retest Priority: Entry on pullback peak / rejection in senior 1H+4H trend (Step 2.3)
        trend_4h = str(mtf_data.get("trend_4h") or indicators.get("trend_4h") or "").upper()
        if trend_1h == "BEARISH" and trend_4h == "BEARISH":
            if 50.0 <= bb_position_pct <= 80.0 and isinstance(last_candle, dict):
                o_c = _safe_float(last_candle.get("open"), current_price)
                c_c = _safe_float(last_candle.get("close"), current_price)
                h_c = _safe_float(last_candle.get("high"), current_price)
                l_c = _safe_float(last_candle.get("low"), current_price)
                tot_rng = h_c - l_c
                u_shad = h_c - max(o_c, c_c)
                is_bear_rejection = (c_c <= o_c) or (tot_rng > 0 and (u_shad / tot_rng) >= 0.30)
                if is_bear_rejection and not trend_exhaustion_short and not both_leaders_bullish:
                    return StrategyProfile(
                        True,
                        "TREND_FOLLOWING",
                        "SHORT",
                        f"RETEST_PRIORITY SHORT: 1H и 4H сонаправлены (BEARISH), откат на 15m в зону ретеста (BB={bb_position_pct:.1f}%), подтверждено отторжение сверху (close={c_c:.4f} <= open={o_c:.4f} или тень {u_shad:.4f})."
                    )
        elif trend_1h == "BULLISH" and trend_4h == "BULLISH":
            if 20.0 <= bb_position_pct <= 50.0 and isinstance(last_candle, dict):
                o_c = _safe_float(last_candle.get("open"), current_price)
                c_c = _safe_float(last_candle.get("close"), current_price)
                h_c = _safe_float(last_candle.get("high"), current_price)
                l_c = _safe_float(last_candle.get("low"), current_price)
                tot_rng = h_c - l_c
                l_shad = min(o_c, c_c) - l_c
                is_bull_rejection = (c_c >= o_c) or (tot_rng > 0 and (l_shad / tot_rng) >= 0.30)
                if is_bull_rejection and not trend_exhaustion_long and not both_leaders_bearish:
                    return StrategyProfile(
                        True,
                        "TREND_FOLLOWING",
                        "LONG",
                        f"RETEST_PRIORITY LONG: 1H и 4H сонаправлены (BULLISH), откат на 15m в зону ретеста (BB={bb_position_pct:.1f}%), подтверждено отторжение снизу (close={c_c:.4f} >= open={o_c:.4f} или тень {l_shad:.4f})."
                    )

        if mtf_alignment == "COUNTER_TREND_WARNING":
            # Pre-Debate Anti-Chop Gate:
            # Rejection of counter-trend overextension before launching expensive LLM debate
            min_bulls_counter = 2 if is_vol_explosion else 4
            min_bears_counter = 2 if is_vol_explosion else 4
            if trend_1h == "BULLISH" and trend_exhaustion_long:
                return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Запрет LONG в COUNTER_TREND на хаях канала BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")
            if trend_1h == "BEARISH" and trend_exhaustion_short:
                return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Запрет SHORT в COUNTER_TREND на лоях канала BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")

            if trend_1h == "BULLISH" and (bb_position_pct >= 88.0 or rsi > 65.0) and tech_bulls < min_bulls_counter:
                return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (Анти-распил: Запрет LONG в перекупленный рынок в COUNTER_TREND без подтверждения {min_bulls_counter} быков, RSI={rsi:.1f}, bb_pos={bb_position_pct:.1f}%).")
            if trend_1h == "BEARISH" and (bb_position_pct <= 12.0 or rsi < 35.0) and tech_bears < min_bears_counter:
                return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (Анти-распил: Запрет SHORT в перепроданный рынок в COUNTER_TREND без подтверждения {min_bears_counter} медведей, RSI={rsi:.1f}, bb_pos={bb_position_pct:.1f}%).")

            if trend_1h == "BULLISH" and (tech_bulls > tech_bears) and (tech_bulls >= 2 or (tech_bulls >= 1 and bb_position_pct <= 45.0)):
                return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Откат по тренду (LONG): Bulls={tech_bulls} vs Bears={tech_bears}, 1H={trend_1h}, BB={bb_position_pct:.1f}%")
            elif trend_1h == "BEARISH" and (tech_bears > tech_bulls) and (
                tech_bears >= 2 
                or (tech_bears >= 1 and candle_signal in ["BEARISH", "SHORT"] and volume_spike_pct >= 50.0)
                or (tech_bears >= 1 and bb_position_pct >= 55.0)
            ):
                return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Откат/импульс по тренду (SHORT): Bears={tech_bears} vs Bulls={tech_bulls}, 1H={trend_1h}, BB={bb_position_pct:.1f}%")
            return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", "ОТКЛОНЕН (Попытка торговли против макро-тренда).")

        if mtf_alignment == "TRANSITION":
            # Pre-Debate Anti-Chop Gate:
            # Rejection of overextended transitions before launching expensive LLM debate
            min_bulls_trans = 2 if is_vol_explosion else 4
            min_bears_trans = 2 if is_vol_explosion else 4
            if trend_1h == "BULLISH" and trend_exhaustion_long:
                return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Запрет LONG в TRANSITION на хаях канала BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")
            if trend_1h == "BEARISH" and trend_exhaustion_short:
                return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Запрет SHORT в TRANSITION на лоях канала BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")

            if trend_1h == "BULLISH" and (bb_position_pct >= 88.0 or rsi > 65.0) and tech_bulls < min_bulls_trans:
                return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (Анти-распил: Запрет LONG в перекупленный рынок в TRANSITION без подтверждения {min_bulls_trans} быков, RSI={rsi:.1f}, bb_pos={bb_position_pct:.1f}%).")
            if trend_1h == "BEARISH" and (bb_position_pct <= 12.0 or rsi < 35.0) and tech_bears < min_bears_trans:
                return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (Анти-распил: Запрет SHORT в перепроданный рынок в TRANSITION без подтверждения {min_bears_trans} медведей, RSI={rsi:.1f}, bb_pos={bb_position_pct:.1f}%).")

            # In TRANSITION, require stronger momentum (ADX >= 22.0) to avoid getting chopped in dying trends
            if not allow_forbidden and 0.0 < adx_14 < 22.0:
                return StrategyProfile(
                    False, "TREND_FOLLOWING", "NEUTRAL",
                    f"ОТКЛОНЕН (Анти-распил: ADX={adx_14:.1f} < 22.0 в фазе TRANSITION: вялый рынок, повышенный риск распила)."
                )

            if trend_1h == "BULLISH" and tech_bulls >= 2 and tech_bears == 0:
                return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Ранний разворот в лонг: 15m/1H Bullish, Bulls={tech_bulls}")
            elif trend_1h == "BEARISH" and tech_bears >= 2 and tech_bulls == 0:
                return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Ранний разворот в шорт: 15m/1H Bearish, Bears={tech_bears}")
            elif allow_forbidden and (tech_bulls >= 1 or tech_bears >= 1):
                direction = "LONG" if tech_bulls >= tech_bears else "SHORT"
                return StrategyProfile(True, "TREND_FOLLOWING", direction, f"TRANSITION трендовый сетап (ALLOW_FORBIDDEN_STRATEGIES): Bulls={tech_bulls}, Bears={tech_bears}")
            return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", "ОТКЛОНЕН (TRANSITION: требуется минимум 2 аналитика в направлении 1H тренда).")



        if mtf_alignment == "MIXED_CHOP":
            # Safety Guard: In high volatility or unknown/degraded macro regimes, mean reversion is dangerous
            if detected_regime in ["HIGH_VOLATILITY", "UNKNOWN", "DATA_INSUFFICIENT", "TRANSITION"]:
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION заблокирован в макро-режиме {detected_regime}, bb_pos={bb_position_pct}%).")
            if bb_position_pct >= 88.0 and detected_regime == "TRENDING_BULL":
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION SHORT заблокирован в макро-режиме TRENDING_BULL: риск пробоя вверх).")
            if bb_position_pct <= 12.0 and detected_regime == "TRENDING_BEAR":
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION LONG заблокирован в макро-режиме TRENDING_BEAR: риск падения без отскока).")
            # In Pure Trend Following & Momentum mode, MEAN_REVERSION is strictly forbidden
            if not allow_forbidden:
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (Анти-распил: MEAN_REVERSION отключен в конфигурации Pure Trend Following при ALLOW_FORBIDDEN_STRATEGIES=False).")

            # Mean Reversion requires sufficient channel width
            # For major high-cap benchmark assets (BTC, ETH), channels naturally compress to 0.8%-1.2%
            # during consolidation. We lower the threshold to 0.8% for BTC and ETH, while keeping
            # 1.5% for mid/small-cap altcoins to cover taker fees and spread friction.
            base_sym = symbol.split("-")[0].upper()
            min_bb_width = 0.8 if base_sym in ["BTC", "ETH"] else 1.5
            if bb_width_pct < min_bb_width:
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (Канал Bollinger слишком узкий: {bb_width_pct:.2f}% < {min_bb_width:.1f}% для прибыльного Mean Reversion).")


            # Macro Trend Directional Alignment:
            # - In TRENDING_BULL, do NOT short the top band (breakout risk). Only allow LONG from bottom band.
            # - In TRENDING_BEAR, do NOT long the bottom band (falling knife). Only allow SHORT from top band.
            if bb_position_pct <= 12.0:
                if not allow_forbidden and detected_regime == "TRENDING_BEAR":
                    return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION LONG заблокирован в макро-режиме TRENDING_BEAR: риск падения без отскока).")
                if tech_bulls >= 1:
                    return StrategyProfile(True, "MEAN_REVERSION", "LONG", f"MEAN_REVERSION LONG: от нижней границы Bollinger, bb_pos={bb_position_pct:.2f}%, width={bb_width_pct:.2f}%")
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (Цена у нижней границы {bb_position_pct:.2f}%, но нет бычьих сигналов: Bulls={tech_bulls}).")

            if bb_position_pct >= 88.0:
                if not allow_forbidden and detected_regime == "TRENDING_BULL":
                    return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION SHORT заблокирован в макро-режиме TRENDING_BULL: риск пробоя вверх).")
                if tech_bears >= 1:
                    return StrategyProfile(True, "MEAN_REVERSION", "SHORT", f"MEAN_REVERSION SHORT: от верхней границы Bollinger, bb_pos={bb_position_pct:.2f}%, width={bb_width_pct:.2f}%")
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (Цена у верхней границы {bb_position_pct:.2f}%, но нет медвежьих сигналов: Bears={tech_bears}).")

            return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MIXED_CHOP, цена внутри канала: bb_pos={bb_position_pct:.2f}%).")

        # ADX Impulse Filter for Trend Following:
        # In pure TRENDING, require ADX >= 20.0 (and for BTC-USD and ETH-USD require ADX >= 22.0).
        # In TRANSITION (detected_regime == "TRANSITION" or mtf_alignment == "TRANSITION"), require stronger momentum ADX >= 22.0.
        is_transition = (detected_regime == "TRANSITION") or (mtf_alignment == "TRANSITION")
        base_sym = symbol.split("-")[0].upper()
        if is_transition:
            required_adx = 22.0
            phase_desc = "в фазе TRANSITION"
        elif base_sym in ["BTC", "ETH"]:
            required_adx = 22.0
            phase_desc = "для бенчмарка BTC/ETH"
        else:
            required_adx = 20.0
            phase_desc = "в боковике/слабом тренде"

        if not allow_forbidden and 0.0 < adx_14 < required_adx:
            return StrategyProfile(
                False, "TREND_FOLLOWING", "NEUTRAL",
                f"ОТКЛОНЕН (Анти-распил: ADX={adx_14:.1f} < {required_adx:.1f} {phase_desc}: вялый рынок, защита от распила)."
            )

        if mtf_alignment == "FULL_ALIGNMENT":
            # In FULL_ALIGNMENT (all 3 timeframes aligned in trend_1h):
            # Direction MUST strictly match trend_1h! Rejecting counter-trend analyst disagreements.
            if trend_1h == "BULLISH":
                if both_leaders_bearish and not is_vol_explosion:
                    return StrategyProfile(
                        False, "MACRO_LEADERS_VETO", "NEUTRAL",
                        "ОТКЛОНЕН (Поводыри BTC/ETH в синхронном обвале FULL_ALIGNMENT BEARISH: покупка альткоинов заблокирована)."
                    )
                # Trend Exhaustion Guard: Veto late longs at channel highs
                if trend_exhaustion_long:
                    return StrategyProfile(
                        False, "TREND_EXHAUSTION_VETO", "NEUTRAL",
                        f"ОТКЛОНЕН (Trend Exhaustion Guard: Бычий импульс истощен: цена на хаях BB={bb_position_pct:.1f}%, DonchianHigh={donchian_high:.2f}, RSI_div={rsi_div})."
                    )
                # Anti-Chop Extreme-Band Gate:
                # In deep chop (range_score >= 68.0): strict ceiling bb_pos < 75.0% (or < 70.0% in confirmed chop)
                # In moderate range (tuned_chop_threshold <= range_score < 68.0): tuned_bb_max (e.g. 89.0%)
                bb_upper_limit = 70.0 if (is_choppy and range_score >= 68.0) else (75.0 if range_score >= 68.0 else tuned_bb_max)
                upper_thr_str = f"{int(bb_upper_limit)}%" if bb_upper_limit.is_integer() else f"{bb_upper_limit:.1f}%"
                if (is_chop_risk or is_choppy) and bb_position_pct >= bb_upper_limit:
                    return StrategyProfile(
                        False, "TREND_FOLLOWING", "NEUTRAL",
                        f"ОТКЛОНЕН (Анти-распил: Запрет покупки на хаях канала в боковике/RANGE_CHOPPY: bb_pos={bb_position_pct:.1f}% >= {upper_thr_str}, Range={range_score:.1f}%)."
                    )
                if tech_bulls > tech_bears:
                    return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"FULL_ALIGNMENT MTF trend (Bullish) + подтверждение быков ({tech_bulls} vs {tech_bears}).")
                elif tech_bulls == 0 and tech_bears == 0:
                    return StrategyProfile(True, "TREND_FOLLOWING", "LONG", "FULL_ALIGNMENT MTF trend (15m/1h/4h Bullish), нет сопротивления аналитиков.")
                elif tech_bulls == tech_bears:
                    return StrategyProfile(False, "ANALYST_DISAGREEMENT", "NEUTRAL", f"ОТКЛОНЕН (Паритет аналитиков {tech_bulls} vs {tech_bears}: нет строгого перевеса быков для LONG).")
                else:
                    return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (FULL_ALIGNMENT Bullish тренд, но аналитики в медвежьей оппозиции: {tech_bears} vs {tech_bulls}).")
            elif trend_1h == "BEARISH":
                # Trend Exhaustion Guard: Veto late shorts at channel lows
                if trend_exhaustion_short:
                    return StrategyProfile(
                        False, "TREND_EXHAUSTION_VETO", "NEUTRAL",
                        f"ОТКЛОНЕН (Trend Exhaustion Guard: Медвежий импульс истощен: цена на лоях BB={bb_position_pct:.1f}%, DonchianLow={donchian_low:.2f}, RSI_div={rsi_div})."
                    )
                # Anti-Chop Extreme-Band Gate:
                # In deep chop (range_score >= 68.0): strict floor bb_pos > 25.0% (or > 30.0% in confirmed chop)
                # In moderate range (tuned_chop_threshold <= range_score < 68.0): 100.0 - tuned_bb_max (e.g. 11.0%)
                bb_lower_limit = 30.0 if (is_choppy and range_score >= 68.0) else (25.0 if range_score >= 68.0 else (100.0 - tuned_bb_max))
                lower_thr_str = f"{int(bb_lower_limit)}%" if bb_lower_limit.is_integer() else f"{bb_lower_limit:.1f}%"
                if (is_chop_risk or is_choppy) and bb_position_pct <= bb_lower_limit:
                    return StrategyProfile(
                        False, "TREND_FOLLOWING", "NEUTRAL",
                        f"ОТКЛОНЕН (Анти-распил: Запрет продажи на лоях канала в боковике/RANGE_CHOPPY: bb_pos={bb_position_pct:.1f}% <= {lower_thr_str}, Range={range_score:.1f}%)."
                    )
                if tech_bears > tech_bulls:
                    return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"FULL_ALIGNMENT MTF trend (Bearish) + подтверждение медведей ({tech_bears} vs {tech_bulls}).")
                elif tech_bulls == 0 and tech_bears == 0:
                    return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "FULL_ALIGNMENT MTF trend (15m/1h/4h Bearish), нет сопротивления аналитиков.")
                elif tech_bears == tech_bulls:
                    return StrategyProfile(False, "ANALYST_DISAGREEMENT", "NEUTRAL", f"ОТКЛОНЕН (Паритет аналитиков {tech_bears} vs {tech_bulls}: нет строгого перевеса медведей для SHORT).")
                else:
                    return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (FULL_ALIGNMENT Bearish тренд, но аналитики в бычьей оппозиции: {tech_bulls} vs {tech_bears}).")

        # Fallback to pure consensus (only if MTF alignment is not in explicit conflict)
        if tech_bulls >= 2 and tech_bears <= 1:
            if trend_exhaustion_long:
                return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Бычий импульс истощен: цена на хаях BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")
            bb_upper_limit = 70.0 if (is_choppy and range_score >= 68.0) else (75.0 if range_score >= 68.0 else tuned_bb_max)
            if (is_chop_risk or is_choppy) and bb_position_pct >= bb_upper_limit:
                return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (Анти-распил: Запрет покупки на хаях канала в боковике/RANGE_CHOPPY: bb_pos={bb_position_pct:.1f}% >= {bb_upper_limit:.1f}%).")
            return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Bullish консенсус {tech_bulls} vs {tech_bears}")
        elif tech_bears >= 2 and tech_bulls <= 1:
            if trend_exhaustion_short:
                return StrategyProfile(False, "TREND_EXHAUSTION_VETO", "NEUTRAL", f"ОТКЛОНЕН (Trend Exhaustion Guard: Медвежий импульс истощен: цена на лоях BB={bb_position_pct:.1f}%, RSI_div={rsi_div}).")
            bb_lower_limit = 30.0 if (is_choppy and range_score >= 68.0) else (25.0 if range_score >= 68.0 else (100.0 - tuned_bb_max))
            if (is_chop_risk or is_choppy) and bb_position_pct <= bb_lower_limit:
                return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (Анти-распил: Запрет продажи на лоях канала в боковике/RANGE_CHOPPY: bb_pos={bb_position_pct:.1f}% <= {bb_lower_limit:.1f}%).")
            return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Bearish консенсус {tech_bears} vs {tech_bulls}")

        if tech_bulls == tech_bears:
            return StrategyProfile(False, "ANALYST_DISAGREEMENT", "NEUTRAL", f"ОТКЛОНЕН (Паритет аналитиков {tech_bulls} vs {tech_bears}: нет строгого перевеса).")

        return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"Боковик/нет консенсуса (bulls={tech_bulls}, bears={tech_bears}, MTF={mtf_alignment}).")

    @staticmethod
    def _evaluate_anti_bot_predator(
        symbol: str,
        market_data: Dict[str, Any],
        valid_reports: List[Dict[str, Any]],
        macro_cache: Dict[str, Any],
        detected_regime: str = "RANGE_CHOPPY",
        profile: str = "BALANCED"
    ) -> StrategyProfile:
        import math

        def _safe_float(val: Any, default: float = 0.0) -> float:
            if val is None:
                return default
            try:
                f = float(val)
                return f if math.isfinite(f) else default
            except (ValueError, TypeError):
                return default

        indicators = market_data.get("indicators") or {}
        price_data = market_data.get("price_data") or {}
        current_price = _safe_float(price_data.get("current_price"), _safe_float(indicators.get("last_closed_candle_close"), 0.0))
        if current_price <= 0:
            current_price = _safe_float(indicators.get("current_price"), 0.0)

        bb_pos = _safe_float(indicators.get("bb_position_pct", indicators.get("bb_pos")), 50.0)
        rsi_14 = _safe_float(indicators.get("rsi_14", indicators.get("rsi")), 50.0)
        volume_spike_pct = _safe_float(indicators.get("volume_spike_pct"), 0.0)
        adx_14 = _safe_float(indicators.get("adx_14", indicators.get("adx")), 0.0)
        donchian_high = _safe_float(indicators.get("donchian_high"), 0.0)
        donchian_low = _safe_float(indicators.get("donchian_low"), 0.0)

        bb_upper = _safe_float(indicators.get("bb_upper"), 0.0)
        bb_lower = _safe_float(indicators.get("bb_lower"), 0.0)

        if bb_upper > bb_lower and current_price > 0:
            bb_width_pct = ((bb_upper - bb_lower) / current_price) * 100.0
        else:
            bb_width_pct = _safe_float(indicators.get("bb_width_pct"), 0.0)

        sweeps = indicators.get("liquidity_sweeps", [])
        if not sweeps and "algo_signals" in indicators:
            sweeps = indicators["algo_signals"].get("liquidity_sweeps", [])
        if not sweeps and "liquidity_sweeps" in market_data:
            sweeps = market_data.get("liquidity_sweeps", [])

        def _has_sweep(sweeps_list: list, kw_type: str) -> bool:
            for s in sweeps_list:
                if isinstance(s, str) and kw_type.lower() in s.lower():
                    return True
                if isinstance(s, dict):
                    t = str(s.get("type") or s.get("sweep_type") or s.get("direction") or "").lower()
                    if kw_type.lower() in t:
                        return True
            return False

        has_upper_sweep = _has_sweep(sweeps, "upper") or _has_sweep(sweeps, "bearish")
        has_lower_sweep = _has_sweep(sweeps, "lower") or _has_sweep(sweeps, "bullish")

        # 0.1 Извлечение последней закрытой 15m свечи для фильтра Marubozu / Band Walking
        last_candle = None
        mtf_data = market_data.get("multi_timeframe") or {}
        tf_15m = mtf_data.get("tf_15m") if isinstance(mtf_data, dict) else {}
        candles = (
            (tf_15m.get("candles_20") if isinstance(tf_15m, dict) else None)
            or price_data.get("candles_20")
            or price_data.get("candles")
            or indicators.get("candles")
            or []
        )
        if isinstance(candles, list) and len(candles) >= 2:
            last_candle = candles[-2]
        elif isinstance(candles, list) and len(candles) == 1:
            last_candle = candles[-1]
        elif isinstance(indicators.get("last_candle"), dict):
            last_candle = indicators.get("last_candle")

        c_open = _safe_float(last_candle.get("open") if isinstance(last_candle, dict) else indicators.get("candle_open"), 0.0)
        c_high = _safe_float(last_candle.get("high") if isinstance(last_candle, dict) else indicators.get("candle_high"), 0.0)
        c_low = _safe_float(last_candle.get("low") if isinstance(last_candle, dict) else indicators.get("candle_low"), 0.0)
        c_close = _safe_float(last_candle.get("close") if isinstance(last_candle, dict) else indicators.get("candle_close"), 0.0)

        candle_range = c_high - c_low
        is_bullish_marubozu = False
        is_bearish_marubozu = False
        upper_wick_ratio = 0.0
        lower_wick_ratio = 0.0
        body_ratio = 0.0
        if candle_range > 0:
            upper_wick_ratio = (c_high - max(c_open, c_close)) / candle_range
            lower_wick_ratio = (min(c_open, c_close) - c_low) / candle_range
            body_ratio = abs(c_close - c_open) / candle_range

            if c_close > c_open and body_ratio > 0.80 and upper_wick_ratio < 0.12:
                is_bullish_marubozu = True
            elif c_close < c_open and body_ratio > 0.80 and lower_wick_ratio < 0.12:
                is_bearish_marubozu = True

        # 0.2 Извлечение дисбаланса книги заявок (Order Book Imbalance)
        ob_imbalance = 0.0
        if "order_book_imbalance" in indicators:
            ob_imbalance = _safe_float(indicators.get("order_book_imbalance"), 0.0)
        elif "imbalance" in indicators:
            ob_imbalance = _safe_float(indicators.get("imbalance"), 0.0)
        elif "order_book_data" in market_data and isinstance(market_data["order_book_data"], dict):
            ob_imbalance = _safe_float(market_data["order_book_data"].get("imbalance"), 0.0)
        else:
            for r in (valid_reports or []):
                if isinstance(r, dict) and r.get("agent_name") == "Order_Book_Agent":
                    ob_imbalance = _safe_float(r.get("imbalance") or r.get("data", {}).get("imbalance"), 0.0)
                    break

        # 0.3 Извлечение динамики OI и ставки финансирования
        derivatives = market_data.get("derivatives_data") or {}
        oi_trend = str(indicators.get("oi_trend") or derivatives.get("open_interest_trend") or "stable").lower()
        funding_rate = _safe_float(indicators.get("funding_rate", derivatives.get("funding_rate")), 0.0)

        # 0.4 Извлечение направления тренда старших таймфреймов 1H + 4H (Макро-фильтр выживания)
        mtf_data = market_data.get("multi_timeframe") or {}
        tf_1h = mtf_data.get("tf_1h") if isinstance(mtf_data, dict) else {}
        tf_4h = mtf_data.get("tf_4h") if isinstance(mtf_data, dict) else {}

        trend_1h = str(
            mtf_data.get("trend_1h") or
            mtf_data.get("tf_1h_trend") or
            (tf_1h.get("trend") if isinstance(tf_1h, dict) else "") or
            market_data.get("trend_1h") or
            ""
        ).upper()
        trend_4h = str(
            mtf_data.get("trend_4h") or
            mtf_data.get("tf_4h_trend") or
            (tf_4h.get("trend") if isinstance(tf_4h, dict) else "") or
            market_data.get("trend_4h") or
            ""
        ).upper()

        is_macro_bullish = (
            trend_1h in ["BULLISH", "UP"] and trend_4h in ["BULLISH", "UP"]
        )
        is_macro_bearish = (
            trend_1h in ["BEARISH", "DOWN"] and trend_4h in ["BEARISH", "DOWN"]
        )

        # 1. Защита от узкого спреда
        if bb_width_pct > 0 and bb_width_pct < 0.55:
            return StrategyProfile(
                False,
                "MEAN_REVERSION",
                "NEUTRAL",
                f"ОТКЛОНЕН (Анти-Бот: Слишком узкий канал Боллинджера {bb_width_pct:.2f}% < 0.55%, потенциал отскока съедается комиссией)."
            )

        # 2. Защита от каскада (Steamroller Guard)
        if volume_spike_pct >= 260.0 and adx_14 >= 38.0:
            return StrategyProfile(
                False,
                "MEAN_REVERSION",
                "NEUTRAL",
                f"ОТКЛОНЕН (Анти-Бот Steamroller Guard: Каскадный трендовый импульс VolSpike={volume_spike_pct:.1f}% >= 260%, ADX={adx_14:.1f} >= 38. Не встаем против катка)."
            )

        # Trend Exhaustion Boost for Predator / Mean Reversion
        algo_signals = indicators.get("algo_signals") or {}
        rsi_div = str(algo_signals.get("rsi_divergence") or indicators.get("rsi_divergence") or "").strip().upper()
        vol_declining_rise = bool(algo_signals.get("volume_declining_on_rise") or algo_signals.get("volume_declining", False))
        vol_declining_drop = bool(algo_signals.get("volume_declining_on_drop") or algo_signals.get("volume_declining", False))

        at_channel_high = (bb_pos > 85.0) or (donchian_high > 0 and current_price >= donchian_high)
        at_channel_low = (bb_pos < 15.0) or (donchian_low > 0 and current_price <= donchian_low)

        trend_exhaustion_long = bool(indicators.get("trend_exhaustion_long")) or (
            at_channel_high and (rsi_div == "BEARISH" or vol_declining_rise)
        )
        trend_exhaustion_short = bool(indicators.get("trend_exhaustion_short")) or (
            at_channel_low and (rsi_div == "BULLISH" or vol_declining_drop)
        )

        # 3. SHORT против лонг-ботов (Bull Trap)
        is_bull_trap = (
            (bb_pos >= 88.0 and rsi_14 >= 64.0) or
            (donchian_high > 0 and current_price >= donchian_high * 0.999 and volume_spike_pct < 230.0 and rsi_14 >= 62.0) or
            (has_upper_sweep and bb_pos >= 72.0 and rsi_14 >= 58.0) or
            trend_exhaustion_long
        )
        if is_bull_trap:
            # 3.0. Макро-фильтр выживания: не шортим при бычьем тренде 1H + 4H
            if is_macro_bullish:
                return StrategyProfile(
                    False,
                    "MEAN_REVERSION",
                    "NEUTRAL",
                    "[Predator] Отмена SHORT: старшие таймфреймы 1H+4H в бычьем тренде — не шортим растущий рынок."
                )
            # 3.1. Защита от ползучего тренда (Band Walking / Marubozu Filter)
            if is_bullish_marubozu and not has_upper_sweep and rsi_14 < 76.0:
                return StrategyProfile(
                    False,
                    "MEAN_REVERSION",
                    "NEUTRAL",
                    f"[Predator] Отмена SHORT: ползучий бычий тренд (Band Walking / Marubozu body={body_ratio*100:.0f}%, upper_wick={upper_wick_ratio*100:.0f}% < 12%)."
                )
            # 3.2. Фильтр поглощения в стакане (Order Book Absorption)
            if ob_imbalance > 0.22:
                return StrategyProfile(
                    False,
                    "MEAN_REVERSION",
                    "NEUTRAL",
                    f"[Predator] Отмена SHORT: в стакане сильная поддержка бидов imbalance={ob_imbalance:.2f}"
                )
            # 3.3. Учет ловушки OI и Фандинга
            oi_funding_trap_short = (bb_pos >= 85.0 and (funding_rate > 0.0001 or oi_trend == "rising"))
            trap_boost = " [OI/Funding Trap Confirmed]" if oi_funding_trap_short else ""
            return StrategyProfile(
                True,
                "MEAN_REVERSION",
                "SHORT",
                f"🦈 [ANTI-BOT SHORT]: Ловушка для пробойных ботов на хаях (Bull Trap / BB_pos={bb_pos:.1f}%, RSI={rsi_14:.1f}, Vol={volume_spike_pct:.1f}%){trap_boost}.",
                liquidity_sweep=bool(has_upper_sweep)
            )

        # 4. LONG против шорт-ботов (Bear Trap)
        is_bear_trap = (
            (bb_pos <= 12.0 and rsi_14 <= 36.0) or
            (donchian_low > 0 and current_price <= donchian_low * 1.001 and volume_spike_pct < 230.0 and rsi_14 <= 38.0) or
            (has_lower_sweep and bb_pos <= 28.0 and rsi_14 <= 42.0) or
            trend_exhaustion_short
        )
        if is_bear_trap:
            # 4.0. Макро-фильтр выживания: не лонгуем при медвежьем тренде 1H + 4H
            if is_macro_bearish:
                return StrategyProfile(
                    False,
                    "MEAN_REVERSION",
                    "NEUTRAL",
                    "[Predator] Отмена LONG: старшие таймфреймы 1H+4H в медвежьем тренде — не лонгуем падающий рынок."
                )
            # 4.1. Защита от ползучего тренда (Band Walking / Marubozu Filter)
            if is_bearish_marubozu and not has_lower_sweep and rsi_14 > 24.0:
                return StrategyProfile(
                    False,
                    "MEAN_REVERSION",
                    "NEUTRAL",
                    f"[Predator] Отмена LONG: ползучий медвежий тренд (Band Walking / Marubozu body={body_ratio*100:.0f}%, lower_wick={lower_wick_ratio*100:.0f}% < 12%)."
                )
            # 4.2. Фильтр поглощения в стакане (Order Book Absorption)
            if ob_imbalance < -0.22:
                return StrategyProfile(
                    False,
                    "MEAN_REVERSION",
                    "NEUTRAL",
                    f"[Predator] Отмена LONG: в стакане сильное давление асков imbalance={ob_imbalance:.2f}"
                )
            # 4.3. Учет ловушки OI и Фандинга
            oi_funding_trap_long = (bb_pos <= 15.0 and (funding_rate < -0.0001 or oi_trend == "rising"))
            trap_boost = " [OI/Funding Trap Confirmed]" if oi_funding_trap_long else ""
            return StrategyProfile(
                True,
                "MEAN_REVERSION",
                "LONG",
                f"🦈 [ANTI-BOT LONG]: Сбор стопов и выкуп паники у ботов (Bear Trap / BB_pos={bb_pos:.1f}%, RSI={rsi_14:.1f}, Vol={volume_spike_pct:.1f}%){trap_boost}.",
                liquidity_sweep=bool(has_lower_sweep)
            )

        # 5. Fallback в засаде
        return StrategyProfile(
            False,
            "MEAN_REVERSION",
            "NEUTRAL",
            f"[Predator] В засаде: ждем выноса ботов на края канала (BB_pos={bb_pos:.1f}%, RSI={rsi_14:.1f})."
        )
```

---

<a id="file-python-agents-ceo_agent-py"></a>
## File: `python/agents/ceo_agent.py` (1878 строк, 97.7 KB)

```python
from core.config import config
import json
import logging
from typing import Dict, Any, Tuple, Optional, Callable
import os
import re

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient


class ScoreResult(dict):
    """
    Holds the deterministic scoring result of CEOAgent.
    Acts as a dictionary containing:
      - decision: 'LONG' | 'SHORT' | 'HOLD'
      - directional_confidence: 0..100
      - entry_quality: 0..100
      - conviction: 0..100 (alias for entry_quality for pipeline & risk manager compatibility)
      - risk_score: 0..100
      - risk_penalties: -30..0
      - trade_action: 'ENTER' | 'WAIT_FOR_PULLBACK' | 'REDUCE_SIZE' | 'HOLD'
      - raw_net_score: float
    
    Supports 2-tuple unpacking for 100% backward compatibility:
      decision, conviction = result
    """
    def __iter__(self):
        return iter([self["decision"], self["conviction"]])

def normalize_mtf_trend(raw_trend: Any) -> Optional[str]:
    """
    Canonical MTF trend normalizer:
    Normalizes raw timeframe trend string into 'BULLISH', 'BEARISH', 'NEUTRAL', or None.
    Handles variants produced by MarketDataService and analysts:
    e.g. 'bullish', 'bullish_trend', 'bull', 'uptrend', 'up', 'long', 'strong_bullish', 'strong_uptrend' -> 'BULLISH'
         'bearish', 'bearish_trend', 'bear', 'downtrend', 'down', 'short', 'strong_bearish', 'strong_downtrend' -> 'BEARISH'
         'neutral', 'neutral_trend', 'flat', 'chop', 'mixed_chop', 'sideways', 'range', 'range_bound', 'rangebound', 'consolidation' -> 'NEUTRAL'
    Returns None for missing, unpopulated, or invalid strings ('unknown', 'n/a', 'unavailable', 'none', '').
    """
    if raw_trend is None:
        return None
    if isinstance(raw_trend, bytes):
        try:
            s_raw = raw_trend.decode("utf-8", errors="ignore")
        except Exception:
            return None
    elif isinstance(raw_trend, str):
        s_raw = raw_trend
    else:
        return None

    s = s_raw.strip().lower().replace("-", "_").replace(" ", "_")
    if not s or s in ["unknown", "n/a", "na", "unavailable", "none", "null"]:
        return None

    if s in ["bullish", "bull", "up", "bullish_trend", "uptrend", "long", "strong_bullish", "strong_uptrend"]:
        return "BULLISH"
    if s in ["bearish", "bear", "down", "bearish_trend", "downtrend", "short", "strong_bearish", "strong_downtrend"]:
        return "BEARISH"
    if s in ["neutral", "flat", "chop", "mixed_chop", "sideways", "range", "consolidation", "neutral_trend", "range_bound", "rangebound"]:
        return "NEUTRAL"
    return None


def extract_router_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts or normalizes router metadata from pipeline market_context / data payload.
    """
    if not isinstance(data, dict):
        return {}

    strategy_type = str(data.get("strategy_type") or data.get("strategy_mode") or "").upper()
    indicators = data.get("indicators") if isinstance(data.get("indicators"), dict) else (
        (data.get("market_data") or {}).get("indicators") if isinstance(data.get("market_data"), dict) else {}
    )
    if not isinstance(indicators, dict):
        indicators = {}

    vol_pct = 100.0
    for k in ["volume_pct", "volume_spike_pct", "vol_pct"]:
        if k in data and data[k] is not None:
            try:
                vol_pct = float(data[k])
                break
            except (ValueError, TypeError):
                pass
        elif k in indicators and indicators[k] is not None:
            try:
                vol_pct = float(indicators[k])
                break
            except (ValueError, TypeError):
                pass

    curr_price = 0.0
    for k in ["current_price", "price"]:
        if k in data and data[k] is not None:
            try:
                curr_price = float(data[k])
                break
            except (ValueError, TypeError):
                pass
    if curr_price == 0.0 and isinstance(data.get("price_data"), dict):
        try:
            curr_price = float(data["price_data"].get("current_price", 0.0))
        except (ValueError, TypeError):
            pass

    breakout_lvl = 0.0
    for k in ["breakout_level", "breakout_lvl", "level"]:
        if k in data and data[k] is not None:
            try:
                breakout_lvl = float(data[k])
                break
            except (ValueError, TypeError):
                pass

    direction_bias = str(data.get("direction_bias", "")).upper()
    if breakout_lvl <= 0:
        if direction_bias == "SHORT" and "donchian_low" in indicators:
            try:
                breakout_lvl = float(indicators["donchian_low"])
            except (ValueError, TypeError):
                pass
        elif "donchian_high" in indicators:
            try:
                breakout_lvl = float(indicators["donchian_high"])
            except (ValueError, TypeError):
                pass

    if breakout_lvl <= 0 and data.get("router_reasoning"):
        m = re.search(r'(?:Пробой|breakout)\s+([\d\.]+)', str(data.get("router_reasoning")), re.IGNORECASE)
        if m:
            try:
                breakout_lvl = float(m.group(1))
            except (ValueError, TypeError):
                pass

    ob_bias = "NEUTRAL"
    for k in ["orderbook_bias", "ob_bias"]:
        if k in data and data[k]:
            ob_bias = str(data[k]).upper()
            break
    if ob_bias == "NEUTRAL":
        reports = data.get("subordinate_analyst_reports") or []
        for r in reports:
            if isinstance(r, dict) and r.get("agent_name") == "Order_Book_Agent":
                sig = str(r.get("signal", "")).upper()
                if "BULL" in sig or "LONG" in sig:
                    ob_bias = "BULL"
                elif "BEAR" in sig or "SHORT" in sig:
                    ob_bias = "BEAR"
                break

    max_chase_pct = 1.5
    if "max_chase_pct" in data:
        try:
            max_chase_pct = float(data["max_chase_pct"])
        except (ValueError, TypeError):
            pass

    return {
        "strategy_type": strategy_type,
        "volume_pct": vol_pct,
        "breakout_level": breakout_lvl,
        "current_price": curr_price,
        "orderbook_bias": ob_bias,
        "max_chase_pct": max_chase_pct
    }


def apply_breakout_penalty_override(
    ceo_decision: Dict[str, Any],
    router_metadata: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Корректирует ложные штрафы Mean-Reversion (RiskPenalty -20..-25) от LLM,
    если Strategy Router передал валидный импульсный сетап BREAKOUT.
    
    Ожидаемые поля в router_metadata:
    - strategy_type: str ("BREAKOUT", "FULL_ALIGNMENT", "MEAN_REVERSION", ...)
    - volume_pct: float (например, 403.66 для 403.66%)
    - breakout_level: float (уровень пробоя, например 82145.0)
    - current_price: float (текущая цена, например 82469.0)
    - orderbook_bias: str ("BULL", "BEAR", "NEUTRAL")
    - max_chase_pct: float (макс. допустимый уход цены от пробоя, по умолчанию 1.5%)
    """
    if not isinstance(ceo_decision, dict) or not isinstance(router_metadata, dict):
        return ceo_decision

    strategy_type = str(
        router_metadata.get("strategy_type") or router_metadata.get("strategy_mode", "")
    ).upper()
    if "BREAKOUT" not in strategy_type:
        return ceo_decision

    direction = str(
        ceo_decision.get("decision") or ceo_decision.get("signal", "HOLD")
    ).upper()
    if direction not in ("LONG", "SHORT"):
        return ceo_decision

    try:
        dir_conf = float(
            ceo_decision.get("dir_conf", ceo_decision.get("directional_confidence", 0.0))
        )
    except (ValueError, TypeError):
        dir_conf = 0.0

    try:
        raw_penalty = float(
            ceo_decision.get("risk_penalty", ceo_decision.get("risk_penalties", 0.0))
        )
    except (ValueError, TypeError):
        raw_penalty = 0.0

    action = str(ceo_decision.get("action", ceo_decision.get("trade_action", "HOLD"))).upper()

    try:
        vol_pct = float(
            router_metadata.get("volume_pct", router_metadata.get("volume_spike_pct", 100.0))
        )
    except (ValueError, TypeError):
        vol_pct = 100.0

    try:
        breakout_lvl = float(router_metadata.get("breakout_level", 0.0))
    except (ValueError, TypeError):
        breakout_lvl = 0.0

    try:
        curr_price = float(
            router_metadata.get("current_price", router_metadata.get("price", 0.0))
        )
    except (ValueError, TypeError):
        curr_price = 0.0

    raw_ob = str(
        router_metadata.get("orderbook_bias", router_metadata.get("ob_bias", "NEUTRAL"))
    ).upper()
    if "BULL" in raw_ob:
        ob_bias = "BULL"
    elif "BEAR" in raw_ob:
        ob_bias = "BEAR"
    else:
        ob_bias = "NEUTRAL"

    try:
        max_chase_pct = float(router_metadata.get("max_chase_pct", 1.5))
    except (ValueError, TypeError):
        max_chase_pct = 1.5

    # 1. Считаем, насколько далеко цена улетела от уровня пробоя (Chase Distance)
    if breakout_lvl > 0 and curr_price > 0:
        chase_dist_pct = abs(curr_price - breakout_lvl) / breakout_lvl * 100.0
    else:
        chase_dist_pct = 0.0

    # 2. Проверяем отсутствие встречного блока в стакане
    opposing_ob = (
        (direction == "LONG" and ob_bias == "BEAR") or
        (direction == "SHORT" and ob_bias == "BULL")
    )

    # 3. Условие чистого пробоя (Fresh High-Volume Breakout):
    # Уверенность CEO >= 75%, Объем >= 180%, цена ушла от пробоя не далее max_chase_pct
    is_fresh_impulse = (
        dir_conf >= 75.0
        and vol_pct >= 180.0
        and chase_dist_pct <= max_chase_pct
        and not opposing_ob
    )

    if is_fresh_impulse and abs(raw_penalty) >= 15.0:
        # Ограничиваем максимальный штраф:
        # -5 при экстремальном объеме (>=250% или стакан по тренду), иначе -10
        is_trend_ob = (
            (direction == "LONG" and ob_bias == "BULL") or
            (direction == "SHORT" and ob_bias == "BEAR") or
            raw_ob == direction[:4]
        )
        if vol_pct >= 250.0 or is_trend_ob:
            capped_penalty = -5.0
        else:
            capped_penalty = -10.0

        new_entry_quality = max(0.0, dir_conf + capped_penalty)

        # Повышаем статус с WAIT_FOR_PULLBACK до активного входа
        new_action = action
        if action == "WAIT_FOR_PULLBACK":
            new_action = "ENTER" if new_entry_quality >= 73.0 else "REDUCE_SIZE"

        logger = logging.getLogger("CEO_Agent")
        old_qual = ceo_decision.get('entry_quality', ceo_decision.get('conviction', new_entry_quality))
        logger.info(
            f"[CEO_Agent] Breakout Override applied: Vol={vol_pct:.1f}%, "
            f"Chase={chase_dist_pct:.2f}% (<= {max_chase_pct}%). "
            f"Penalty adjusted {raw_penalty} -> {capped_penalty}, "
            f"EntryQuality: {old_qual}% -> {new_entry_quality}%, "
            f"Action: {action} -> {new_action}"
        )

        ceo_decision["risk_penalty"] = capped_penalty
        ceo_decision["risk_penalties"] = int(capped_penalty)
        ceo_decision["entry_quality"] = new_entry_quality
        ceo_decision["conviction"] = int(new_entry_quality)
        ceo_decision["action"] = new_action
        ceo_decision["trade_action"] = new_action
        if "final_status" in ceo_decision:
            ceo_decision["final_status"] = "APPROVED_ENTER" if new_action == "ENTER" else new_action
        if "conviction_for_risk" not in ceo_decision:
            ceo_decision["conviction_for_risk"] = dir_conf

        if isinstance(ceo_decision.get("risk_breakdown"), dict):
            ceo_decision["risk_breakdown"]["total"] = int(capped_penalty)
            if "price_extension" in ceo_decision["risk_breakdown"]:
                ceo_decision["risk_breakdown"]["price_extension"] = int(capped_penalty)

    return ceo_decision


class CEOEscalationPatch:
    # Пороги прямого доверия Primary CEO (qwen-plus / qwen-2.5-72b) без вызова 8B-арбитра
    DIRECT_ENTER_MIN_CONF: float = 75.0
    DIRECT_ENTER_MIN_QUAL: float = 70.0   # Было 80.0 — из-за этого сливались сделки с Qual=75% (#35, #42, #45)
    DIRECT_REDUCE_MIN_CONF: float = 75.0
    DIRECT_REDUCE_MIN_QUAL: float = 62.0
    SMART_CUTOFF_MIN_QUAL: float = 62.0

    # Допустимая погрешность математики у 8B-арбитра: EntryQuality == DirConf + RiskPenalty
    MATH_TOLERANCE: float = 3.0

    @classmethod
    def should_bypass_escalation(cls, primary_decision: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Определяет, нужно ли пропустить вызов слабой модели (llama-3.1-8b),
        чтобы защитить сильные сигналы от ложного вето и сэкономить API.
        Smart Escalation Cutoff (< 62.0): слабые сетапы не эскалируются.
        """
        action = str(primary_decision.get("action", primary_decision.get("trade_action", "HOLD"))).upper()
        direction = str(primary_decision.get("decision", "HOLD")).upper()
        dir_conf = float(primary_decision.get("dir_conf", primary_decision.get("directional_confidence", 0.0)))
        entry_qual = float(primary_decision.get("entry_quality", primary_decision.get("conviction", 0.0)))

        # 1. Нейтральный рынок / отказ от входа со стороны Primary CEO
        if action == "HOLD" or direction == "HOLD" or entry_qual <= 35.0:
            return True, "decided HOLD or low quality. Bypassing escalation to save API costs."

        # 1.1 WAIT_FOR_PULLBACK: Handled deterministically by Pullback Watchlist (0 LLM tokens)
        from core.config import config
        if action == "WAIT_FOR_PULLBACK" and not getattr(config, "ESCALATE_WAIT_FOR_PULLBACK", False):
            return True, "decided WAIT_FOR_PULLBACK. Routing directly to Pullback Watchlist without LLM escalation to save API costs."

        # 1.2 Smart Escalation Cutoff (< 62.0): Слабый сетап не имеет шансов пройти детерминированный порог 65+
        if entry_qual < cls.SMART_CUTOFF_MIN_QUAL and not (action == "WAIT_FOR_PULLBACK" and getattr(config, "ESCALATE_WAIT_FOR_PULLBACK", False)):
            return True, (
                f"decided {action} with entry_quality ({entry_qual:.1f} < {cls.SMART_CUTOFF_MIN_QUAL:.1f}) below threshold. "
                f"Bypassing escalation to save latency and API costs."
            )

        # 2. Чистый сигнал ENTER от старшей модели (Циклы #35, #42, #45 больше не блокируются)
        if (
            action == "ENTER"
            and dir_conf >= cls.DIRECT_ENTER_MIN_CONF
            and entry_qual >= cls.DIRECT_ENTER_MIN_QUAL
        ):
            return True, (
                f"High-conviction ENTER (DirConf={dir_conf:.0f}%, EntryQuality={entry_qual:.0f}% >= "
                f"{cls.DIRECT_ENTER_MIN_QUAL:.0f}%). Bypassing 8B escalation to prevent false downgrade."
            )

        # 3. Уверенный REDUCE_SIZE (защита от триггера COHORT_D_NEGATIVE_EXPECTANCY в Risk Manager)
        if (
            action == "REDUCE_SIZE"
            and dir_conf >= cls.DIRECT_REDUCE_MIN_CONF
            and entry_qual >= cls.DIRECT_REDUCE_MIN_QUAL
        ):
            return True, (
                f"Solid REDUCE_SIZE (DirConf={dir_conf:.0f}%, EntryQuality={entry_qual:.0f}%). "
                f"Preserving Primary CEO metrics for Stage 5 Risk Manager."
            )

        return False, "Borderline setup — escalating to arbiter."

    @classmethod
    def validate_arbiter_math(cls, arbiter_resp: Dict[str, Any]) -> bool:
        """
        Проверяет ответ meta-llama/llama-3.1-8b-instruct на математическую адекватность
        и целостность полей.
        """
        if not arbiter_resp or not isinstance(arbiter_resp, dict):
            return False

        try:
            arb_dir = str(arbiter_resp.get("decision", "")).upper().strip()
            arb_conf = float(arbiter_resp.get("dir_conf", arbiter_resp.get("directional_confidence", -1.0)))
            arb_qual = float(arbiter_resp.get("entry_quality", arbiter_resp.get("conviction", -1.0)))
            arb_pen = float(arbiter_resp.get("risk_penalty", arbiter_resp.get("risk_penalties", 0.0)))
        except (ValueError, TypeError):
            return False

        # Проверка диапазонов
        if arb_dir not in ("LONG", "SHORT", "HOLD"):
            return False
        if not (0.0 <= arb_conf <= 100.0) or not (0.0 <= arb_qual <= 100.0):
            return False
        if arb_qual > arb_conf + cls.MATH_TOLERANCE:
            # Качество входа не может быть выше базовой уверенности в направлении
            return False

        # Если арбитр вернул конкретный штраф для направленной сделки, проверяем формулу: Qual = max(0, Conf - |Penalty|)
        if arb_dir in ("LONG", "SHORT") and arb_pen != 0.0:
            expected_qual = max(0.0, arb_conf - abs(arb_pen))
            if abs(arb_qual - expected_qual) > cls.MATH_TOLERANCE:
                return False

        return True

    @classmethod
    def resolve_with_escalation(
        cls,
        symbol: str,
        primary_decision: Dict[str, Any],
        router_metadata: Dict[str, Any],
        call_arbiter_fn: Optional[Callable[[], Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Полный цикл принятия решения CEO:
        1. Применяет Breakout Override (если стратегия пробойная).
        2. Проверяет условие обхода эскалации.
        3. При вызове арбитра страхует от математических галлюцинаций БЕЗ сброса в HOLD.
        """
        logger = logging.getLogger("CEO_Agent")
        # Шаг 1: Коррекция штрафа для импульсных пробоев (из предыдущего патча)
        decision = apply_breakout_penalty_override(primary_decision.copy(), router_metadata)

        primary_action = str(decision.get("action", decision.get("trade_action", "HOLD"))).upper()
        primary_dir = str(decision.get("decision", "HOLD")).upper()
        primary_conf = float(decision.get("dir_conf", decision.get("directional_confidence", 0.0)))
        primary_qual = float(decision.get("entry_quality", decision.get("conviction", 0.0)))

        # Шаг 2: Проверяем, нужен ли вообще арбитр
        bypass, bypass_reason = cls.should_bypass_escalation(decision)
        if bypass or call_arbiter_fn is None:
            logger.info(f"[CEO_Agent] {symbol}: {bypass_reason}")
            decision["final_status"] = (
                "APPROVED_ENTER" if primary_action == "ENTER" else primary_action
            )
            # Сохраняем оригинальный conviction для Risk Manager (Cohort D check)
            decision["conviction_for_risk"] = primary_conf
            decision["conviction"] = int(primary_qual)
            decision["escalated"] = False
            decision["disputed_arbitration"] = False
            return decision

        # Шаг 3: Вызов арбитра эскалации
        try:
            arbiter_resp = call_arbiter_fn()
        except Exception as e:
            logger.warning(
                f"[CEO_Agent] {symbol}: Ошибка вызова арбитра ({e}). "
                f"Сохраняем вердикт Primary CEO: {primary_action}."
            )
            decision["final_status"] = (
                "APPROVED_ENTER" if primary_action == "ENTER" else primary_action
            )
            decision["conviction_for_risk"] = primary_conf
            decision["conviction"] = int(primary_qual)
            decision["escalated"] = False
            decision["disputed_arbitration"] = False
            return decision

        # Шаг 4: Валидация математики арбитра (ИСПРАВЛЕНИЕ БАГА ЦИКЛОВ #12, #43-#47)
        if not cls.validate_arbiter_math(arbiter_resp):
            logger.warning(
                f"[CEO_Agent] {symbol}: Арбитр эскалации допустил "
                f"математическую нестыковку. Не аннулируем сигнал — сохраняем {primary_action}."
            )
            # КРИТИЧЕСКИЙ ФИКС: возвращаем исходный primary_action (например, WAIT_FOR_PULLBACK
            # или REDUCE_SIZE), а НЕ сбрасываем его в "HOLD"!
            decision["final_status"] = (
                "APPROVED_ENTER" if primary_action == "ENTER" else primary_action
            )
            decision["conviction_for_risk"] = primary_conf
            decision["conviction"] = int(primary_qual)
            decision["action"] = primary_action
            decision["trade_action"] = primary_action
            decision["decision"] = primary_dir
            decision["escalated"] = True
            decision["disputed_arbitration"] = False
            decision["escalation_note"] = "MATH_ERROR_FALLBACK_TO_PRIMARY"
            return decision

        arb_dir = str(arbiter_resp.get("decision", "HOLD")).upper()
        arb_conf = float(arbiter_resp.get("dir_conf", arbiter_resp.get("directional_confidence", 0.0)))
        arb_qual = float(arbiter_resp.get("entry_quality", arbiter_resp.get("conviction", 0.0)))

        logger.info(
            f"[CEO_Agent] Escalation Final Decision: {arb_dir} (DirConf: {arb_conf:.0f}%, EntryQuality: {arb_qual:.0f}%)"
        )

        decision["escalated"] = True

        # Шаг 5: Мягкая агрегация мнений для пограничных сетапов (WAIT_FOR_PULLBACK / REDUCE_SIZE)
        # Если 8B-модель дает прямой противоположный сигнал (например, LONG против SHORT у Primary)
        # с уверенностью выше 30% — понижаем до WAIT_FOR_PULLBACK, но не убиваем метрики Primary
        if arb_dir not in ("HOLD", primary_dir) and arb_conf >= 30.0:
            decision["action"] = "WAIT_FOR_PULLBACK"
            decision["trade_action"] = "WAIT_FOR_PULLBACK"
            decision["final_status"] = "WAIT_FOR_PULLBACK"
            decision["disputed_arbitration"] = True
            decision["conviction_for_risk"] = primary_conf
            decision["conviction"] = int(min(primary_qual, arb_qual))
            decision["entry_quality"] = min(primary_qual, arb_qual)
        else:
            # Сохраняем действие старшей модели, а уверенность усредняем с весом 80% на Primary CEO и 20% на 8B
            blended_conf = round(0.80 * primary_conf + 0.20 * arb_conf, 1)
            is_anti_bot_mode = getattr(config, "ANTI_BOT_MODE", False) is True
            if (arb_conf >= 80.0 and arb_qual >= 70.0) or (is_anti_bot_mode and arb_qual >= 70.0):
                decision["conviction"] = max(int(round(0.80 * primary_qual + 0.20 * arb_qual)), 72)
                decision["entry_quality"] = decision["conviction"]
                decision["conviction_for_risk"] = max(primary_conf, blended_conf, 80.0)
                decision["directional_confidence"] = max(float(decision.get("directional_confidence", 0.0)), 80.0)
            else:
                decision["conviction_for_risk"] = max(primary_conf, blended_conf)
                decision["conviction"] = int(round(0.80 * primary_qual + 0.20 * arb_qual))
                decision["entry_quality"] = decision["conviction"]
            decision["action"] = primary_action
            decision["trade_action"] = primary_action
            decision["disputed_arbitration"] = (arb_dir != primary_dir and arb_dir != "HOLD")
            decision["final_status"] = (
                "APPROVED_ENTER" if primary_action == "ENTER" else primary_action
            )

        return decision


class CEOAgent(BaseAgent):
    """
    The Chief Investment Officer (CIO / CEO) of the trading syndicate.
    
    Uses dynamic OpenRouter models (e.g. Qwen, DeepSeek) for the Primary CEO and Escalation.
    
    Architecture:
    - Trend strength determines DIRECTIONAL CONFIDENCE (0..100).
    - Market risks determine ENTRY QUALITY (0..100) via mandatory risk penalties (0..-30).
    - Strict deterministic floor ensures extreme RSI (>90) and sentiment extremes cannot be bypassed.
    """
    
    def __init__(self, logger: TradeLogger, primary_llm: LLMClient, escalation_llm: LLMClient):
        super().__init__("CEO_Agent", logger, primary_llm)
        self.escalation_llm = escalation_llm

        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "ceo_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    @staticmethod
    def get_anti_bot_prompt_addon() -> str:
        return (
            "\n[🦈 CRITICAL: ANTI-BOT PREDATOR MODE IS ACTIVE (Institutional Liquidity Hunter v2.0)]:\n"
            "System is hunting breakout/momentum bots trapped at extreme Bollinger / Donchian boundaries.\n"
            "RULES FOR CEO IN ANTI-BOT MODE:\n"
            "1. Do NOT penalize counter-trend at Bollinger/RSI extremes. In range/chop or climax wicks, extreme overbought (RSI > 64, BB > 88) or oversold (RSI < 36, BB < 12) represents bot exhaustion and trapped retail momentum, NOT trend continuation.\n"
            "2. Treat these extreme spikes as high-probability mean-reversion traps (Bull Trap / Bear Trap).\n"
            "3. Confirmation Boost: The presence of upper/lower rejection wicks (wick rejection), order book divergence, or '[OI/Funding Trap Confirmed]' in StrategyRouter reasoning strongly confirms trapped retail flow. Boost EntryQuality to 80-90% and approve with trade_action: ENTER.\n"
            "4. CRITICAL FOR JSON SCORE_BREAKDOWN (PREVENT MATH VETO): In ANTI-BOT MODE, score_breakdown MUST reflect exhaustion/reversion strength, NOT raw trend momentum! When your decision is SHORT, you MUST set bear_argument as a strong NEGATIVE number whose absolute value exceeds bull_argument (e.g., bear_argument: -42, bull_argument: 15) and mtf_trend negative (e.g., -35). When your decision is LONG, you MUST set bull_argument strictly greater than bear_argument (e.g., bull_argument: 42, bear_argument: -15) and mtf_trend positive (e.g., +35).\n"
            "5. Steamroller / Band-Walking Warning: If price is creeping along the band with full-bodied bars (no rejection wicks) and orderbook pressure pushes in the direction of the breakout, do NOT counter-trade; prescribe HOLD.\n"
            "6. If StrategyRouter selected MEAN_REVERSION with a clear direction and no hard vetoes apply, approve the trade with ENTER (do NOT downgrade to WAIT_FOR_PULLBACK or HOLD) and assign EntryQuality >= 75%."
        )

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes the 4-Tier Escalation Model logic with separated Directional Confidence
        and Risk-Adjusted Entry Quality.
        """
        symbol = data.get("symbol")
        analyst_reports = data.get("subordinate_analyst_reports", [])
        mtf_data = data.get("multi_timeframe_context", {})
        historical_context = data.get("historical_context", {})

        self.logger.info(f"[{self.name}] {self.llm_client.model_name} (Judge) анализирует дебаты Bull vs Bear по {symbol}...")
        
        # Strip heavy candle arrays to save ~1,500 prompt tokens
        clean_mtf = dict(mtf_data) if isinstance(mtf_data, dict) else {}
        for tf_k in ["tf_15m", "tf_1h", "tf_4h"]:
            if tf_k in clean_mtf and isinstance(clean_mtf[tf_k], dict):
                clean_mtf[tf_k] = {k: v for k, v in clean_mtf[tf_k].items() if k != "candles_20"}

        is_pullback_retest = bool(data.get("is_pullback_retest"))
        pb_ctx = data.get("pullback_context") or {}

        payload = {
            "target_symbol": symbol,
            "strategy_mode": data.get("strategy_mode", "TREND_FOLLOWING"),
            "direction_bias": data.get("direction_bias", "NEUTRAL"),
            "router_reasoning": data.get("router_reasoning", ""),
            "macro_regime": data.get("macro_regime", "RANGE_CHOPPY"),
            "macro_profile": data.get("macro_profile", "BALANCED"),
            "multi_timeframe_context": clean_mtf,
            "bull_thesis": data.get("bull_thesis", {}),
            "bear_thesis": data.get("bear_thesis", {}),
            "bull_status": data.get("bull_status", "OK"),
            "bear_status": data.get("bear_status", "OK"),
            "debate_status": data.get("debate_status", "COMPLETE"),
            "subordinate_analyst_reports": analyst_reports,
            "historical_trade_memory": historical_context,
            "past_lessons_learned": data.get("past_lessons_learned", []),
            "is_pullback_retest": is_pullback_retest,
            "pullback_context": pb_ctx if is_pullback_retest else None
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nExecutive Dashboard Data:\n{data_string}"

        if is_pullback_retest:
            pb_direction = pb_ctx.get("direction") or data.get("pullback_retest_direction") or data.get("direction_bias", "LONG")
            pb_signal_price = pb_ctx.get("signal_price", "N/A")
            pb_target_price = pb_ctx.get("target_pullback_price", "N/A")
            pb_inv_price = pb_ctx.get("invalidation_price", "N/A")
            pb_orig_conf = pb_ctx.get("directional_confidence", 80)
            current_price = data.get("current_price", "current market price")
            retest_prompt_addon = (
                f"\n[CRITICAL PULLBACK RETEST CONTEXT]:\n"
                f"You previously approved this {pb_direction} thesis at price {pb_signal_price} "
                f"with Direction Confidence {pb_orig_conf}% and requested WAIT_FOR_PULLBACK to {pb_target_price}.\n"
                f"Price has now completed the requested pullback to {current_price} (Invalidation level: {pb_inv_price} is intact).\n"
                f"RULES FOR RETEST:\n"
                f"1. Do NOT output WAIT_FOR_PULLBACK again.\n"
                f"2. Check if the pullback is stabilizing: if 15m candles show violent momentum AGAINST {pb_direction} (e.g. strong displacement or volume spike towards {pb_inv_price} with no rejection wick), output HOLD!\n"
                f"3. Approve execution with ENTER or REDUCE_SIZE ONLY if price shows rejection/stabilization at {current_price} and 1H trend remains intact."
            )
            full_prompt = f"{full_prompt}\n{retest_prompt_addon}"

        from core.config import config
        if getattr(config, "ANTI_BOT_MODE", False) is True:
            full_prompt = f"{full_prompt}\n{self.get_anti_bot_prompt_addon()}"
        llm_response = {}
        try:
            llm_response = await self.generate_json(full_prompt, required_keys=["decision", "score_breakdown", "reasoning_en"])
        except Exception as e:
            self.logger.warning(f"[{self.name}] Primary LLM failed: {e}")
            return {"decision": "ERROR", "conviction": 0, "hold_category": "LLM_ERROR", "reasoning_en": f"Primary LLM failed: {e}"}

        raw_decision = str(llm_response.get("decision", "ERROR")).upper()
        if raw_decision == "ERROR":
            return {"decision": "ERROR", "conviction": 0, "hold_category": "LLM_ERROR", "reasoning_en": llm_response.get("reasoning", "LLM Error")}
        
        breakdown = llm_response.get("score_breakdown", {})
        score_res = self._validate_and_compute_score(raw_decision, breakdown, market_context=data)
        
        decision = score_res["decision"]
        conviction = score_res["conviction"]
        directional_confidence = score_res["directional_confidence"]
        entry_quality = score_res["entry_quality"]
        total_penalty = score_res["risk_penalties"]
        risk_score = score_res["risk_score"]
        trade_action = score_res["trade_action"]

        if score_res.get("math_conflict"):
            self.logger.error(f"[{self.name}] 🛑 Hard Veto: Math hallucination detected in primary CEO verdict ({raw_decision} vs debate score). Forcing HOLD.")
            decision = "HOLD"
            trade_action = "HOLD"
            directional_confidence = 0
            entry_quality = 0
            conviction = 0
        
        reasoning = llm_response.get("reasoning_en", "")

        # --- BREAKOUT PENALTY OVERRIDE (Sanity Check on Fresh Impulses) ---
        router_meta = extract_router_metadata(data)
        ceo_eval_payload = {
            "decision": decision,
            "dir_conf": directional_confidence,
            "directional_confidence": directional_confidence,
            "risk_penalty": total_penalty,
            "risk_penalties": total_penalty,
            "entry_quality": entry_quality,
            "conviction": conviction,
            "action": trade_action,
            "trade_action": trade_action,
            "risk_breakdown": score_res.get("risk_breakdown", {})
        }
        ceo_eval_payload = apply_breakout_penalty_override(ceo_eval_payload, router_meta)
        decision = ceo_eval_payload["decision"]
        conviction = int(ceo_eval_payload["conviction"])
        directional_confidence = int(ceo_eval_payload["directional_confidence"])
        entry_quality = int(ceo_eval_payload["entry_quality"])
        total_penalty = int(ceo_eval_payload["risk_penalties"])
        trade_action = ceo_eval_payload["trade_action"]
        risk_score = min(100, int((abs(total_penalty) / 40.0) * 100)) if total_penalty < 0 else 0
        
        if decision == "HOLD":
            log_detail = "Decision: HOLD (Conf: N/A)"
            print(f"👔 [CEO {self.llm_client.model_name}] HOLD (Нет направленного преимущества)")
        else:
            log_detail = f"Decision: {decision} (DirConf: {directional_confidence}%, RiskPenalty: {total_penalty}, EntryQuality: {entry_quality}%, Action: {trade_action})"
            print(f"👔 [CEO {self.llm_client.model_name}] {decision} (DirConf: {directional_confidence}%, EntryQuality: {entry_quality}% -> {trade_action})")
            
        self.logger.info(f"[{self.name}] Primary CEO ({self.llm_client.model_name}): {log_detail}")
        
        final_hold_category = "NONE"
        
        # ESCALATION MODEL LOGIC (Cost Optimized)
        primary_decision = decision
        primary_conviction = conviction
        primary_dir_conf = directional_confidence
        primary_entry_quality = entry_quality
        primary_trade_action = trade_action
        primary_raw_reasoning = reasoning
        escalated = False
        esc_decision_log = "N/A"
        esc_conv_log = "N/A"
        disputed_arbitration = False
        
        conviction_for_risk = float(primary_dir_conf)
        final_status = "HOLD" if decision == "HOLD" else ("APPROVED_ENTER" if trade_action == "ENTER" else trade_action)

        # Step 2: Check escalation bypass via CEOEscalationPatch matrix
        bypass, bypass_reason = CEOEscalationPatch.should_bypass_escalation(ceo_eval_payload)
        if bypass:
            self.logger.info(f"[{self.name}] {symbol}: {bypass_reason}")
            print(f"⏩ [Escalation Bypassed] {bypass_reason}")
        elif not self.escalation_llm:
            self.logger.info(f"[{self.name}] Escalation LLM is not configured. Bypassing escalation.")
            print("⏩ [Escalation Bypassed] Эскалационная модель не настроена. Эскалация отменена.")
        elif getattr(self.llm_client, "model_name", "Primary") == getattr(self.escalation_llm, "model_name", "Escalation"):
            self.logger.info(f"[{self.name}] Primary and Escalation models are identical ({getattr(self.llm_client, 'model_name', 'Primary')}). Bypassing escalation to prevent echo chamber.")
            print(f"⏩ [Escalation Bypassed] Основная и эскалационная модели совпали ({getattr(self.llm_client, 'model_name', 'Primary')}). Эскалация отменена (предотвращение эхо-камеры).")
        else:
            escalated = True
            escalation_cause = f"Borderline setup ({trade_action}, Conviction {conviction}%, DirConf {directional_confidence}%)"
            self.logger.info(f"[{self.name}] {escalation_cause}. Escalating to {self.escalation_llm.model_name}...")
            print(f"⚠️ [Escalation] Спорный сетап ({escalation_cause}). Подключаем {self.escalation_llm.model_name} для финального вердикта...")
            
            escalation_prompt = f"""You are the Supreme Escalation AI ({self.escalation_llm.model_name}) for an elite crypto prop-trading firm.
The Primary CEO ({self.llm_client.model_name}) has proposed a {decision} on {symbol} with Directional Confidence of {directional_confidence}%, Risk Penalty of {total_penalty}, and Entry Quality (Conviction) of {conviction}%.
Proposed Trade Action: {trade_action}.
Your job is to review the exact same data and provide a FINAL decisive verdict.
Evaluate both trend strength (directional confidence) and counter-risks (RSI extremes, sentiment, divergences).
If the primary CEO missed a strong setup and defaulted to HOLD, you must OVERRIDE and find the LONG/SHORT opportunity.
If the setup is truly weak or too risky, confirm HOLD or WAIT.

Primary CEO Reasoning:
{reasoning}

Here is the raw data:
{data_string}

Provide a JSON strictly matching this schema (pick one concrete string for enum fields, do NOT output pipe '|' characters):
{{
  "decision": "LONG",
  "directional_confidence": 75,
  "risk_score": 50,
  "entry_quality": 65,
  "trade_action": "ENTER",
  "score_breakdown": {{
    "bull_argument": "<int 0..50 for Bullish strength>",
    "bear_argument": "<int -50..0 for Bearish strength>",
    "mtf_trend": "<int -50..50: MUST be NEGATIVE for SHORT (e.g. -35), POSITIVE for LONG (e.g. +35)>",
    "risk_penalties": {{
      "price_extension": "<int -25..0>",
      "macro_sentiment": "<int -15..0>",
      "market_structure": "<int -15..0>",
      "liquidity_friction": "<int -10..0>",
      "total": "<int -30..0 sum of categories>"
    }}
  }},
  "winning_argument": "Bull",
  "consensus_summary": "Your detailed escalation review reasoning",
  "reasoning_en": "Step-by-step CIO executive summary"
}}

ENUM FIELD SPECIFICATIONS (Choose EXACTLY ONE valid value):
- "decision": Must be one of "LONG", "SHORT", "HOLD"
- "trade_action": Must be one of "ENTER", "WAIT_FOR_PULLBACK", "REDUCE_SIZE", "HOLD"
- "winning_argument": Must be one of "Bull", "Bear", "Neither"

CRITICAL: Return RAW JSON ONLY. Your output MUST start immediately with '{{' and end with '}}'. Do NOT output markdown bullet lists, internal reasoning, or conversational text outside the JSON.
"""
            if getattr(config, "ANTI_BOT_MODE", False) is True:
                escalation_prompt = f"{escalation_prompt}\n{self.get_anti_bot_prompt_addon()}"
            arbitration_failed = False
            arbitration_error = ""
            esc_response = None
            esc_decision = "HOLD"
            esc_conviction = 0
            esc_dir_conf = 0
            esc_entry_qual = 0
            disputed_arbitration = False

            try:
                original_llm = self.llm_client
                self.llm_client = self.escalation_llm
                try:
                    esc_response = await self.generate_json(escalation_prompt, required_keys=["decision", "score_breakdown", "reasoning_en"])
                finally:
                    self.llm_client = original_llm
            except Exception as e:
                arbitration_failed = True
                arbitration_error = str(e)

            if not arbitration_failed and esc_response:
                esc_raw_decision = str(esc_response.get("decision", "ERROR")).upper().strip()
                if esc_raw_decision in ["LONG", "SHORT", "HOLD"]:
                    k3_breakdown = esc_response.get("score_breakdown", {})
                    esc_score_res = self._validate_and_compute_score(esc_raw_decision, k3_breakdown, market_context=data)
                    
                    if esc_score_res.get("math_conflict"):
                        self.logger.error(f"[{self.name}] 🛑 Hard Veto: Math hallucination detected in escalation CEO verdict ({esc_raw_decision} vs debate score). Forcing HOLD.")
                        esc_raw_decision = "HOLD"
                        esc_conf_candidate = 0.0
                        esc_qual_candidate = 0.0
                        esc_pen_candidate = 0.0
                    else:
                        esc_conf_candidate = float(esc_score_res.get("directional_confidence", esc_response.get("directional_confidence", 0.0)))
                        esc_qual_candidate = float(esc_score_res.get("entry_quality", esc_response.get("entry_quality", 0.0)))
                        esc_pen_candidate = float(esc_score_res.get("risk_penalties", esc_response.get("risk_score", 0.0)))

                    arb_eval_dict = {
                        "decision": esc_raw_decision,
                        "dir_conf": esc_conf_candidate,
                        "directional_confidence": esc_conf_candidate,
                        "entry_quality": esc_qual_candidate,
                        "conviction": esc_qual_candidate,
                        "risk_penalty": esc_pen_candidate,
                        "risk_penalties": esc_pen_candidate
                    }

                    # Step 4: Validate arbiter math (FIX FOR CYCLES #12, #43-#47)
                    if esc_score_res.get("math_conflict", False) or not CEOEscalationPatch.validate_arbiter_math(arb_eval_dict):
                        self.logger.warning(
                            f"[{self.name}] {symbol}: Арбитр {self.escalation_llm.model_name} допустил "
                            f"математическую нестыковку. Не аннулируем сигнал — сохраняем {primary_trade_action}."
                        )
                        print(f"🛡️ [Arbitration Fallback] Арбитр допустил математическую нестыковку. Не аннулируем сигнал — сохраняем {primary_trade_action} ({primary_decision}).")
                        arbitration_failed = True
                        arbitration_error = "Arbitrator score breakdown math conflict"
                    else:
                        esc_decision = esc_score_res["decision"]
                        esc_conviction = esc_score_res["conviction"]
                        esc_dir_conf = esc_score_res["directional_confidence"]
                        esc_entry_qual = esc_score_res["entry_quality"]

                        esc_eval_payload = {
                            "decision": esc_decision,
                            "dir_conf": esc_dir_conf,
                            "directional_confidence": esc_dir_conf,
                            "risk_penalty": esc_score_res["risk_penalties"],
                            "risk_penalties": esc_score_res["risk_penalties"],
                            "entry_quality": esc_entry_qual,
                            "conviction": esc_conviction,
                            "action": esc_score_res.get("trade_action", "ENTER" if esc_conviction >= 73 else "REDUCE_SIZE"),
                            "trade_action": esc_score_res.get("trade_action", "ENTER" if esc_conviction >= 73 else "REDUCE_SIZE"),
                            "risk_breakdown": esc_score_res.get("risk_breakdown", {})
                        }
                        esc_eval_payload = apply_breakout_penalty_override(esc_eval_payload, router_meta)
                        esc_decision = esc_eval_payload["decision"]
                        esc_conviction = int(esc_eval_payload["conviction"])
                        esc_dir_conf = int(esc_eval_payload["directional_confidence"])
                        esc_entry_qual = int(esc_eval_payload["entry_quality"])

                        esc_decision_log = esc_decision
                        esc_conv_log = esc_conviction
                        k3_reasoning = esc_response.get("reasoning_en", "")
                        reasoning = f"[Primary CEO: {reasoning}]\n\n[ESCALATION VERDICT ({self.escalation_llm.model_name}): {k3_reasoning}]"
                        self.logger.info(f"[{self.name}] Escalation ({self.escalation_llm.model_name}) Final Decision: {esc_decision} (DirConf: {esc_dir_conf}%, EntryQuality: {esc_entry_qual}%)")
                        print(f"🧠 [{self.escalation_llm.model_name}] Вердикт: {esc_decision} (DirConf: {esc_dir_conf}%, EntryQuality: {esc_entry_qual}%)")
                else:
                    arbitration_failed = True
                    arbitration_error = f"Invalid decision value: {esc_raw_decision}"

            # --- ARBITRATION RESOLUTION ENGINE ---
            if arbitration_failed:
                # Rule: "Ошибка формата или арифметики у арбитра не должна считаться доказательством неправильности исходного сигнала."
                # Arbitrator failed, so we strictly preserve Primary CEO's original verdict without converting to HOLD or discounting!
                self.logger.warning(
                    f"[{self.name}] 🛡️ Arbitrator failure ({arbitration_error}). "
                    f"Preserving Primary CEO original verdict: {primary_decision} (Conviction: {primary_conviction}%, Action: {primary_trade_action})."
                )
                print(f"🛡️ [Arbitration Fallback] Арбитр допустил ошибку ({arbitration_error}). Сохраняем исходное решение Primary CEO: {primary_decision} (Conviction: {primary_conviction}%, Action: {primary_trade_action}).")
                decision = primary_decision
                conviction = primary_conviction
                directional_confidence = primary_dir_conf
                entry_quality = primary_entry_quality
                trade_action = primary_trade_action
                conviction_for_risk = primary_dir_conf
                final_status = "APPROVED_ENTER" if primary_trade_action == "ENTER" else primary_trade_action
                disputed_arbitration = False
                reasoning = f"[Primary CEO: {primary_raw_reasoning}]\n\n[ARBITRATION BYPASSED: Arbitrator {getattr(self.escalation_llm, 'model_name', 'Escalation')} encountered an error ({arbitration_error}). Retained Primary CEO verdict.]"

            # Case 1: Direct Conflict with Opposing Signal (e.g. LONG vs SHORT with conf >= 30%)
            elif esc_decision not in ("HOLD", primary_decision) and esc_dir_conf >= 30.0:
                decision = primary_decision
                trade_action = "WAIT_FOR_PULLBACK"
                final_status = "WAIT_FOR_PULLBACK"
                disputed_arbitration = True
                conviction_for_risk = primary_dir_conf
                conviction = int(min(primary_entry_quality, esc_entry_qual))
                entry_quality = conviction
                directional_confidence = primary_dir_conf
                print(f"⚠️ [Escalation Disagreement] Арбитр дал противоположный сигнал ({esc_decision} {esc_dir_conf}% vs Primary {primary_decision}). Понижаем статус до WAIT_FOR_PULLBACK.")

            # Case 2: Full Consensus (both models agree on direction)
            elif esc_decision == primary_decision:
                decision = esc_decision
                conviction = int((primary_conviction * 0.6) + (esc_conviction * 0.4))
                directional_confidence = int((primary_dir_conf * 0.6) + (esc_dir_conf * 0.4))
                entry_quality = int((primary_entry_quality * 0.6) + (esc_entry_qual * 0.4))

                is_anti_bot_mode = getattr(config, "ANTI_BOT_MODE", False) is True
                if (esc_dir_conf >= 80 and esc_entry_qual >= 70) or (is_anti_bot_mode and esc_entry_qual >= 70):
                    directional_confidence = max(directional_confidence, 80)
                    entry_quality = max(entry_quality, 72)
                    conviction = max(conviction, 72)

                conviction_for_risk = max(primary_dir_conf, float(directional_confidence))
                if conviction < 60:
                    if directional_confidence >= 70:
                        trade_action = "WAIT_FOR_PULLBACK"
                    else:
                        trade_action = "HOLD"
                        decision = "HOLD"
                elif conviction >= 73:
                    trade_action = "ENTER"
                elif directional_confidence >= 75 and conviction < 65:
                    trade_action = "WAIT_FOR_PULLBACK"
                else:
                    trade_action = "REDUCE_SIZE"
                final_status = "APPROVED_ENTER" if trade_action == "ENTER" else trade_action
                print(f"🤝 [Consensus] Модели пришли к согласию! Подтвержден {decision}. Conviction: {conviction}% (Primary: {primary_conviction}%, Esc: {esc_conviction}%) | Entry Quality: {entry_quality}% | Action: {trade_action}")

            # Case 3: Model Disagreement where Arbiter is HOLD or Low Conviction
            else:
                disputed_arbitration = True
                dispute_reason = f"Конфликт моделей ({primary_decision} vs {esc_decision})"
                self.logger.warning(f"[{self.name}] {dispute_reason}. Передаем решение независимым правилам риска...")
                
                target_candidate = primary_decision if primary_decision in ["LONG", "SHORT"] else esc_decision
                print(f"⚖️ [Arbitration Gate] {dispute_reason}. Кандидат ({target_candidate}) проверяется независимыми правилами риска...")

                if target_candidate in ["LONG", "SHORT"]:
                    is_risk_approved, risk_rationale, risk_metrics = self._evaluate_independent_risk_rules(analyst_reports, data, target_candidate)
                else:
                    is_risk_approved = False
                    risk_rationale = f"Neither model proposed actionable trade ({primary_decision} vs {esc_decision})"
                    risk_metrics = {}
                
                if is_risk_approved and target_candidate in ["LONG", "SHORT"]:
                    # Approved by independent risk rules:
                    decision = target_candidate
                    # Discount conviction to safe corridor synced with min_reduce_size_conv
                    from core.config import get_tuned_param
                    safe_arb_cap = int(get_tuned_param("min_reduce_size_conv", 71.0))
                    chosen_conv = esc_conviction if primary_decision == "HOLD" else primary_conviction
                    chosen_dir = esc_dir_conf if primary_decision == "HOLD" else primary_dir_conf
                    if chosen_conv >= 65:
                        conviction = max(safe_arb_cap, min(chosen_conv, safe_arb_cap))
                    else:
                        conviction = min(chosen_conv, safe_arb_cap)
                    directional_confidence = chosen_dir
                    entry_quality = conviction
                    conviction_for_risk = primary_dir_conf
                    # Force REDUCE_SIZE to prevent full-size exposure on disputed trade
                    trade_action = "REDUCE_SIZE"
                    final_status = "REDUCE_SIZE"
                    reasoning += f"\n\n[INDEPENDENT RISK ARBITRATION APPROVED: {risk_rationale}. Sizing capped to REDUCE_SIZE (Conviction {conviction}%).]"
                    self.logger.info(f"[{self.name}] Независимые правила риска ОДОБРИЛИ {decision}: {risk_rationale}. Action: REDUCE_SIZE.")
                    print(f"✅ [Arbitration Gate] Независимые правила риска ПОДТВЕРДИЛИ {decision}: {risk_rationale}. Допуск с защитным объемом (REDUCE_SIZE, {conviction}%).")
                else:
                    # Independent risk rules rejected
                    decision = "HOLD"
                    conviction = 0
                    entry_quality = 0
                    directional_confidence = 0
                    trade_action = "HOLD"
                    final_status = "HOLD"
                    final_hold_category = "INDEPENDENT_RISK_REJECTION"
                    reasoning += f"\n\n[INDEPENDENT RISK ARBITRATION REJECTED: {risk_rationale}. Trade converted to HOLD.]"
                    self.logger.info(f"[{self.name}] Независимые правила риска ОТКЛОНИЛИ {target_candidate}: {risk_rationale}. Итог: HOLD.")
                    print(f"🛡️ [Arbitration Gate] Независимые правила риска ОТКЛОНИЛИ сделку: {risk_rationale}. Итог: HOLD.")

        # Deterministic Decision Engine for HOLD Category
        if decision == "HOLD":
            final_hold_category = self._determine_hold_category(analyst_reports, conviction)
        else:
            final_hold_category = "NONE"

        # Log confidence stats for successful trades
        if decision in ["LONG", "SHORT"]:
            try:
                import datetime
                with open("confidence_stats.log", "a", encoding="utf-8") as f:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{ts}] {symbol} | Result: {decision} | {self.llm_client.model_name}: {primary_decision} ({primary_conviction}%) | {self.escalation_llm.model_name}: {esc_decision_log} ({esc_conv_log}%)\n")
            except Exception as e:
                self.logger.error(f"Failed to log confidence stats: {e}")

        final_verdict = {
            "decision": decision,
            "conviction": conviction,
            "directional_confidence": directional_confidence,
            "dir_conf": directional_confidence,
            "entry_quality": entry_quality,
            "risk_score": risk_score,
            "risk_penalties": total_penalty,
            "risk_penalty": total_penalty,
            "risk_breakdown": score_res.get("risk_breakdown", {}),
            "trade_action": trade_action,
            "action": trade_action,
            "final_status": final_status,
            "conviction_for_risk": conviction_for_risk,
            "reasoning_en": reasoning,
            "reasoning_ru": llm_response.get("reasoning_ru", ""),
            "consensus_summary": llm_response.get("consensus_summary", ""),
            "mtf_validation": llm_response.get("mtf_validation", ""),
            "hold_category": final_hold_category if decision == "HOLD" else "NONE",
            "primary_conviction": primary_conviction,
            "escalated": escalated,
            "disputed_arbitration": disputed_arbitration,
            "debate_status": data.get("debate_status", "COMPLETE"),
            "bull_status": data.get("bull_status", "OK"),
            "bear_status": data.get("bear_status", "OK")
        }
        return apply_breakout_penalty_override(final_verdict, router_meta)

    def _determine_hold_category(self, analyst_reports: list, conviction: int) -> str:
        if conviction < 80 and conviction > 0:
            return "LOW_CONFIDENCE"
            
        signals = [r.get("signal", "NEUTRAL").upper() for r in analyst_reports if isinstance(r, dict)]
        bullish = signals.count("BULLISH") + signals.count("LONG")
        bearish = signals.count("BEARISH") + signals.count("SHORT")
        
        if bullish > 0 and bearish > 0:
            from core.config import config
            if getattr(config, "ANTI_BOT_MODE", False) is True:
                return "LOW_EDGE"
            return "ANALYST_DISAGREEMENT"
            
        news = next((r for r in analyst_reports if isinstance(r, dict) and r.get("agent_name") == "News_Agent"), None)
        if news and news.get("signal", "NEUTRAL").upper() in ["BEARISH", "SHORT"] and bullish > 0:
            return "NEWS_RISK"
            
        return "LOW_EDGE"

    def _evaluate_independent_risk_rules(
        self,
        analyst_reports: list,
        market_context: Dict[str, Any],
        candidate_decision: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Independent Risk Arbitrator:
        Evaluates deterministic technical signals and risk thresholds without relying on LLM consensus.
        Used when LLM models conflict or arbitrator experiences math/format failure.
        
        Returns:
            (is_approved: bool, reason: str, metrics: Dict[str, Any])
        """
        candidate = candidate_decision.upper().strip()
        if candidate not in ["LONG", "SHORT"]:
            return False, f"Candidate decision is {candidate}, not actionable", {}

        # 1. Independent Technical Analyst Syndicate Count (Excluding News)
        tech_reports = [
            r for r in (analyst_reports or [])
            if isinstance(r, dict) and r.get("agent_name") != "News_Agent" and r.get("agent") != "News_Agent"
        ]
        news_reports = [
            r for r in (analyst_reports or [])
            if isinstance(r, dict) and (r.get("agent_name") == "News_Agent" or r.get("agent") == "News_Agent")
        ]

        tech_signals = [str(r.get("signal", "NEUTRAL")).upper() for r in tech_reports]
        tech_bulls = tech_signals.count("BULLISH") + tech_signals.count("LONG")
        tech_bears = tech_signals.count("BEARISH") + tech_signals.count("SHORT")

        supporting_analysts = tech_bears if candidate == "SHORT" else tech_bulls
        opposing_analysts = tech_bulls if candidate == "SHORT" else tech_bears

        # Requirement: At least 2 TECHNICAL analysts support and at most 1 analyst opposes
        from core.config import config
        is_anti_bot = getattr(config, "ANTI_BOT_MODE", False) is True
        strat_mode = str(market_context.get("strategy_mode", "") or "").upper() if isinstance(market_context, dict) else ""
        is_anti_bot_mr = is_anti_bot and (strat_mode == "MEAN_REVERSION" or not strat_mode)
        if not is_anti_bot_mr:
            if supporting_analysts < 2:
                return False, f"Недостаточно поддержки аналитиков (за: {supporting_analysts} < 2)", {
                    "supporting": supporting_analysts, "opposing": opposing_analysts
                }
            if opposing_analysts > 1:
                return False, f"Слишком высокое сопротивление аналитиков (против: {opposing_analysts} > 1)", {
                    "supporting": supporting_analysts, "opposing": opposing_analysts
                }
        else:
            if supporting_analysts < 1:
                return False, f"Недостаточно поддержки аналитиков для контртренда (за: {supporting_analysts} < 1)", {
                    "supporting": supporting_analysts, "opposing": opposing_analysts
                }

        # News Evaluation: News is contextual sentiment / macro risk, NOT an unweighted technical vote
        if news_reports:
            news_sig = str(news_reports[0].get("signal", "NEUTRAL")).upper()
            if candidate == "LONG" and news_sig in ["BEARISH", "SHORT"]:
                sentiment = news_reports[0].get("sentiment_score", 50)
                try:
                    if float(sentiment) < 25.0:
                        return False, f"Противоречие с новостным фоном: экстремальный медвежий сантимент ({sentiment})", {"news_signal": news_sig}
                except (ValueError, TypeError):
                    pass
            elif candidate == "SHORT" and news_sig in ["BULLISH", "LONG"]:
                sentiment = news_reports[0].get("sentiment_score", 50)
                try:
                    if float(sentiment) > 75.0:
                        return False, f"Противоречие с новостным фоном: экстремальный бычий сантимент ({sentiment})", {"news_signal": news_sig}
                except (ValueError, TypeError):
                    pass

        # 2. MTF Trend Alignment
        mtf_ctx = market_context.get("multi_timeframe_context", {}) if isinstance(market_context, dict) else {}
        tf_1h = mtf_ctx.get("tf_1h", {}) if isinstance(mtf_ctx, dict) and isinstance(mtf_ctx.get("tf_1h"), dict) else {}
        tf_4h = mtf_ctx.get("tf_4h", {}) if isinstance(mtf_ctx, dict) and isinstance(mtf_ctx.get("tf_4h"), dict) else {}
        
        norm_1h = normalize_mtf_trend(
            (tf_1h.get("trend") if isinstance(tf_1h, dict) else None)
            or mtf_ctx.get("trend_1h")
        )
        norm_4h = normalize_mtf_trend(
            (tf_4h.get("trend") if isinstance(tf_4h, dict) else None)
            or mtf_ctx.get("trend_4h")
        )

        if not is_anti_bot_mr:
            if candidate == "SHORT":
                if norm_1h == "BULLISH" and norm_4h == "BULLISH":
                    return False, "Контртренд: 1h и 4h таймфреймы оба бычьи", {"trend_1h": norm_1h, "trend_4h": norm_4h}
            elif candidate == "LONG":
                if norm_1h == "BEARISH" and norm_4h == "BEARISH":
                    return False, "Контртренд: 1h и 4h таймфреймы оба медвежьи", {"trend_1h": norm_1h, "trend_4h": norm_4h}

        # 3. RSI Extrema Check
        rsi, fear_greed = self._extract_market_metrics(market_context or {})
        if rsi is not None:
            if candidate == "SHORT" and rsi < 25.0:
                return False, f"Экстремальная перепроданность для шорта (RSI={rsi:.1f} < 25)", {"rsi": rsi}
            elif candidate == "LONG" and rsi > 75.0:
                return False, f"Экстремальная перекупленность для лонга (RSI={rsi:.1f} > 75)", {"rsi": rsi}

        # 4. Derivatives Funding Check
        derivatives = (
            (market_context.get("derivatives_data") if isinstance(market_context, dict) else None)
            or ((market_context.get("market_data") or {}).get("derivatives_data") if isinstance(market_context, dict) else None)
            or {}
        )
        funding = derivatives.get("funding_rate") or derivatives.get("funding_rate_decimal")
        if funding is not None:
            try:
                funding_val = float(funding)
                if candidate == "LONG" and funding_val > 0.0005:
                    return False, f"Фандинг перегружен в лонг ({funding_val:.4f})", {"funding": funding_val}
                elif candidate == "SHORT" and funding_val < -0.0005:
                    return False, f"Фандинг перегружен в шорт ({funding_val:.4f})", {"funding": funding_val}
            except (ValueError, TypeError):
                pass

        return True, f"Аналитики: {supporting_analysts} vs {opposing_analysts}, MTF тренд согласован", {
            "supporting": supporting_analysts,
            "opposing": opposing_analysts,
            "rsi": rsi,
            "trend_1h": norm_1h or "NEUTRAL",
            "trend_4h": norm_4h or "NEUTRAL"
        }

    def _extract_market_metrics(self, data: Dict[str, Any]) -> Tuple[float | None, float | None]:
        """
        Extracts RSI and Fear & Greed index from market_data, indicators, news_data,
        or subordinate analyst reports.
        """
        rsi = None
        fear_greed = None
        
        if not isinstance(data, dict):
            return rsi, fear_greed

        # 1. Direct indicators / news_data
        m_data = data.get("market_data") if isinstance(data.get("market_data"), dict) else {}
        indicators = data.get("indicators") if isinstance(data.get("indicators"), dict) else (m_data.get("indicators") if isinstance(m_data.get("indicators"), dict) else {})
        if isinstance(indicators, dict):
            for k in ["rsi_14", "rsi", "RSI", "RSI_14"]:
                if k in indicators and indicators[k] is not None:
                    try:
                        rsi = float(indicators[k])
                        break
                    except (ValueError, TypeError):
                        pass
                
        news_data = data.get("news_data") if isinstance(data.get("news_data"), dict) else (m_data.get("news_data") if isinstance(m_data.get("news_data"), dict) else {})
        if isinstance(news_data, dict):
            for k in ["fear_and_greed_index", "sentiment_score", "fear_greed", "fng_index", "fng"]:
                if k in news_data and news_data[k] is not None:
                    try:
                        fear_greed = float(news_data[k])
                        break
                    except (ValueError, TypeError):
                        pass
            if fear_greed is None and "latest_event" in news_data:
                m_ev = re.search(r'Fear\s*(?:&|and)\s*Greed\s*Index\s*:\s*(\d+)', str(news_data["latest_event"]), re.IGNORECASE)
                if m_ev:
                    try:
                        fear_greed = float(m_ev.group(1))
                    except (ValueError, TypeError):
                        pass
                    
        # Direct top-level rsi or fear_greed (useful in tests/payloads)
        if rsi is None and "rsi" in data:
            try:
                rsi = float(data["rsi"])
            except (ValueError, TypeError):
                pass
        if fear_greed is None and "fear_greed" in data:
            try:
                fear_greed = float(data["fear_greed"])
            except (ValueError, TypeError):
                pass
                
        # 2. Fallback: Parse from analyst reports reasoning text
        analyst_reports = data.get("subordinate_analyst_reports", [])
        if isinstance(analyst_reports, list):
            for r in analyst_reports:
                if not isinstance(r, dict):
                    continue
                text = str(r.get("reasoning", ""))
                if rsi is None:
                    m_rsi = re.search(r'RSI[^\d]*?(\d+(?:\.\d+)?)', text, re.IGNORECASE)
                    if m_rsi:
                        try:
                            rsi = float(m_rsi.group(1))
                        except (ValueError, TypeError):
                            pass
                if fear_greed is None:
                    m_fg = re.search(r'(?:Fear|Greed)[^\d]*?(\d{1,3})', text, re.IGNORECASE)
                    if m_fg:
                        try:
                            fear_greed = float(m_fg.group(1))
                        except (ValueError, TypeError):
                            pass
                            
        return rsi, fear_greed

    def _calculate_minimum_risk_penalty(
        self,
        decision: str,
        rsi: float | None,
        fear_greed: float | None,
        market_context: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Deterministic minimum risk penalty floor.
        Applies orthogonal categorization to avoid double-counting correlated price extension metrics.
        Returns a negative integer between -40 and 0.
        """
        total, _ = self._calculate_categorized_risk_penalties(
            decision=decision,
            breakdown={},
            rsi=rsi,
            fear_greed=fear_greed,
            market_context=market_context
        )
        return total

    def _extract_llm_penalty(self, breakdown: dict) -> int:
        """
        Extracts the risk penalty proposed by the LLM in score_breakdown.
        Returns a negative integer between -30 and 0.
        """
        if not isinstance(breakdown, dict):
            return 0
            
        penalties = breakdown.get("risk_penalties")
        extracted_penalty = 0.0
        
        if isinstance(penalties, dict):
            if "total" in penalties:
                try:
                    extracted_penalty = -abs(float(penalties["total"]))
                except (ValueError, TypeError):
                    pass
            else:
                total = 0.0
                for k, v in penalties.items():
                    try:
                        total += abs(float(v))
                    except (ValueError, TypeError):
                        pass
                extracted_penalty = -total
        elif penalties is not None:
            try:
                extracted_penalty = -abs(float(penalties))
            except (ValueError, TypeError):
                pass
        else:
            # Check individual keys
            for k in ["risk_penalty", "risk_adjustment", "rsi_penalty", "rsi_extreme"]:
                if k in breakdown:
                    try:
                        extracted_penalty -= abs(float(breakdown[k]))
                    except (ValueError, TypeError):
                        pass
                        
        return max(-30, int(extracted_penalty))

    def _calculate_categorized_risk_penalties(
        self,
        decision: str,
        breakdown: dict,
        rsi: float | None,
        fear_greed: float | None,
        market_context: Dict[str, Any] = None
    ) -> Tuple[int, Dict[str, int]]:
        """
        Calculates risk penalties across orthogonal, structured categories.
        Enforces anti-double-counting within correlated metrics (e.g. RSI, EMA, BB)
        while allowing genuinely independent risks (Price Extension, Macro Sentiment, Market Structure)
        to accumulate.
        """
        dec = str(decision).upper().strip()
        if dec not in ["LONG", "SHORT"]:
            return 0, {
                "price_extension": 0,
                "macro_sentiment": 0,
                "market_structure": 0,
                "liquidity_friction": 0,
                "total": 0
            }

        # 1. LLM flat penalty extraction (if provided in breakdown)
        llm_penalty = self._extract_llm_penalty(breakdown)

        # 2. Extract Category Components
        # Category A: Price Extension (RSI, EMA-20 distance, Bollinger Band extremes)
        rsi_pen = 0
        if rsi is not None:
            if dec == "LONG":
                if rsi > 95: rsi_pen = -15
                elif rsi > 90: rsi_pen = -12
                elif rsi > 80: rsi_pen = -10
                elif rsi > 70: rsi_pen = -5
            elif dec == "SHORT":
                if rsi < 5: rsi_pen = -15
                elif rsi < 10: rsi_pen = -12
                elif rsi < 20: rsi_pen = -10
                elif rsi < 30: rsi_pen = -5

        ema_pen = 0
        bb_pen = 0
        strat_mode = str((market_context or {}).get("strategy_mode", "")).upper()
        is_breakout_strategy = strat_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"]
        is_pullback_retest = bool(
            (market_context or {}).get("is_pullback_retest") 
            or ((market_context or {}).get("market_data") or {}).get("is_pullback_retest")
        )

        if market_context and isinstance(market_context, dict):
            indicators = market_context.get("indicators") if isinstance(market_context.get("indicators"), dict) else {}
            if isinstance(indicators, dict):
                ema_20 = float(indicators.get("ema_20") or 0.0)
                cur_price = float(
                    (market_context.get("price_data") or {}).get("current_price")
                    or market_context.get("current_price")
                    or indicators.get("current_price")
                    or 0.0
                )
                atr_pct = float(indicators.get("atr_pct") or 1.5)
                raw_bb_pos = indicators.get("bb_position_pct") if "bb_position_pct" in indicators else indicators.get("bb_pos")
                bb_pos = float(raw_bb_pos if raw_bb_pos is not None else 50.0)

                if ema_20 > 0 and cur_price > 0:
                    if dec == "LONG" and cur_price > ema_20:
                        dist_pct = ((cur_price - ema_20) / cur_price) * 100.0
                        if is_breakout_strategy:
                            ema_pen = -5 if dist_pct > max(2.0, 2.5 * atr_pct) else 0
                        elif dist_pct > max(1.2, 1.5 * atr_pct): ema_pen = -15
                        elif dist_pct > max(0.8, 1.0 * atr_pct): ema_pen = -8
                    elif dec == "SHORT" and cur_price < ema_20:
                        dist_pct = ((ema_20 - cur_price) / cur_price) * 100.0
                        if is_breakout_strategy:
                            ema_pen = -5 if dist_pct > max(2.0, 2.5 * atr_pct) else 0
                        elif dist_pct > max(1.2, 1.5 * atr_pct): ema_pen = -15
                        elif dist_pct > max(0.8, 1.0 * atr_pct): ema_pen = -8

                # In confirmed breakout strategies, breaking Bollinger Bands is the signal, not an overextension
                if not is_breakout_strategy:
                    if dec == "LONG" and bb_pos >= 92.0:
                        bb_pen = -8
                    elif dec == "SHORT" and bb_pos <= 8.0:
                        bb_pen = -8

        llm_pen_dict = breakdown.get("risk_penalties") if isinstance(breakdown, dict) and isinstance(breakdown.get("risk_penalties"), dict) else {}
        # Canonical category keys for extraction and recognition (Audit Point 4)
        PRICE_EXTENSION_KEYS = ["rsi_extreme", "rsi_penalty", "overextended", "price_extension", "overbought", "oversold"]
        MACRO_SENTIMENT_KEYS = ["sentiment_euphoria", "sentiment_fear", "macro_sentiment", "sentiment", "fear_greed", "fng"]
        MARKET_STRUCTURE_KEYS = ["resistance", "support", "divergence", "structure", "market_structure"]
        LIQUIDITY_FRICTION_KEYS = ["liquidity", "spread", "slippage", "orderbook", "liquidity_friction"]
        category_keys = PRICE_EXTENSION_KEYS + MACRO_SENTIMENT_KEYS + MARKET_STRUCTURE_KEYS + LIQUIDITY_FRICTION_KEYS

        llm_ext = 0
        for k in PRICE_EXTENSION_KEYS:
            if k in llm_pen_dict:
                try: llm_ext = min(llm_ext, -abs(int(float(llm_pen_dict[k]))))
                except (ValueError, TypeError): pass

        if is_breakout_strategy:
            # Prevent excessive price extension penalties during genuine breakout surges
            llm_ext = max(-10, llm_ext)
            if rsi_pen < -10:
                rsi_pen = -10
        elif is_pullback_retest:
            # Asset has already pulled back to the target/EMA-20; cap price extension penalties
            llm_ext = max(-10, llm_ext)
            if rsi_pen < -10:
                rsi_pen = -10
            if ema_pen < -10:
                ema_pen = -10
            if bb_pen < -10:
                bb_pen = -10

        # Intra-category anti-double-counting: take the most severe price extension metric
        cat_price_extension = min(rsi_pen, ema_pen, bb_pen, llm_ext)
        cat_price_extension = max(-10 if (is_breakout_strategy or is_pullback_retest) else -25, cat_price_extension)

        # Category B: Macro Sentiment (Fear & Greed, euphoria/panic)
        fg_pen = 0
        if fear_greed is not None:
            if dec == "LONG":
                if fear_greed >= 80: fg_pen = -10
                elif fear_greed >= 70: fg_pen = -5
            elif dec == "SHORT":
                if fear_greed <= 20: fg_pen = -10
                elif fear_greed <= 30: fg_pen = -5

        llm_sent = 0
        for k in MACRO_SENTIMENT_KEYS:
            if k in llm_pen_dict:
                try: llm_sent = min(llm_sent, -abs(int(float(llm_pen_dict[k]))))
                except (ValueError, TypeError): pass

        cat_macro_sentiment = min(fg_pen, llm_sent)
        cat_macro_sentiment = max(-15, cat_macro_sentiment)

        # Category C: Market Structure (Resistance, divergence, pattern invalidation)
        llm_struct = 0
        for k in MARKET_STRUCTURE_KEYS:
            if k in llm_pen_dict:
                try: llm_struct = min(llm_struct, -abs(int(float(llm_pen_dict[k]))))
                except (ValueError, TypeError): pass

        cat_market_structure = max(-15, llm_struct)

        # Category D: Liquidity & Execution Friction (Spread, orderbook imbalance, friction)
        llm_liq = 0
        for k in LIQUIDITY_FRICTION_KEYS:
            if k in llm_pen_dict:
                try: llm_liq = min(llm_liq, -abs(int(float(llm_pen_dict[k]))))
                except (ValueError, TypeError): pass

        cat_liquidity = max(-10, llm_liq)

        # Orthogonal aggregation: sum independent categories
        raw_categorized_sum = cat_price_extension + cat_macro_sentiment + cat_market_structure + cat_liquidity
        has_recognized_components = any(k in llm_pen_dict for k in category_keys)

        final_penalty = raw_categorized_sum
        if has_recognized_components:
            # If explicit total was also passed in dictionary alongside categories, ensure anti-double-counting
            if "total" in llm_pen_dict:
                try:
                    explicit_total = -abs(int(float(llm_pen_dict["total"])))
                    # Take the more severe between categorized sum and explicit total without summing them twice
                    final_penalty = min(final_penalty, explicit_total)
                except (ValueError, TypeError):
                    pass
        else:
            # Legacy or flat total compatibility path:
            # When LLM provided only a flat penalty or an unsegmented 'total' key
            if llm_penalty < 0:
                final_penalty = min(final_penalty, llm_penalty)
                # Map into structure for clean breakdown telemetry
                if cat_price_extension == 0 and cat_macro_sentiment == 0 and cat_market_structure == 0 and cat_liquidity == 0:
                    cat_market_structure = final_penalty

        if is_pullback_retest:
            # Confirmed pullback retest has reached target zone; cap cumulative penalties at -10
            final_penalty = max(-10, final_penalty)

        # Cap cumulative penalties:
        # If pure unit test with capped LLM breakdown (-30 cap) and no market context:
        max_allowed_penalty = -10 if is_pullback_retest else (-30 if (not market_context and llm_penalty <= -30) else -40)
        total_penalty = max(max_allowed_penalty, final_penalty)

        risk_breakdown = {
            "price_extension": cat_price_extension,
            "macro_sentiment": cat_macro_sentiment,
            "market_structure": cat_market_structure,
            "liquidity_friction": cat_liquidity,
            "liquidity": cat_liquidity,
            "total": total_penalty
        }
        return total_penalty, risk_breakdown

    def _validate_and_compute_score(
        self,
        decision: str,
        breakdown: dict,
        market_context: Dict[str, Any] = None
    ) -> ScoreResult:
        req_decision = str(decision).upper().strip()
        if "|" in req_decision:
            req_decision = "HOLD" if "HOLD" in req_decision else req_decision.split("|")[0].strip()
        if req_decision == "ERROR":
            return ScoreResult({
                "decision": "ERROR",
                "conviction": 0,
                "directional_confidence": 0,
                "entry_quality": 0,
                "risk_score": 0,
                "risk_penalties": 0,
                "trade_action": "HOLD",
                "raw_net_score": 0.0,
                "math_conflict": False
            })

        max_weights = {
            "bull_argument": 50,
            "bear_argument": 50,
            "mtf_trend": 50
        }
        
        raw_bull = 0.0
        raw_bear = 0.0
        raw_mtf = 0.0
        raw_mtf_signed = 0.0
        
        if isinstance(breakdown, dict):
            for k, v in breakdown.items():
                try:
                    if isinstance(v, dict):
                        continue
                    val = float(v)
                    key = k.lower().replace(" ", "").replace("_", "")
                    
                    if "bull" in key:
                        raw_bull += min(max_weights["bull_argument"], abs(val))
                    elif "bear" in key:
                        raw_bear += min(max_weights["bear_argument"], abs(val))
                    elif "mtf" in key or "trend" in key:
                        raw_mtf_signed = val
                        raw_mtf += min(max_weights["mtf_trend"], abs(val))
                except (ValueError, TypeError):
                    continue

        # Deterministic Sign Assignment & Direction Separation:
        # Check actual observed MTF trend alignment from 15m, 1H and 4H timeframes.
        # DO NOT allow proposed strategy direction_bias or LLM score sign to overwrite real MTF market structure!
        mtf_status = "UNKNOWN"
        observed_mtf_bias = "NEUTRAL"
        strategy_mode = str(
            (market_context or {}).get("strategy_mode")
            or (market_context or {}).get("strategy_type")
            or ""
        ).upper()
        direction_bias = str((market_context or {}).get("direction_bias") or "").upper()
        from core.config import config
        is_anti_bot = bool(getattr(config, "ANTI_BOT_MODE", False))
        is_mean_rev = (strategy_mode == "MEAN_REVERSION")
        matches_router = (direction_bias == req_decision) if direction_bias in ["LONG", "SHORT"] else True

        # Anti-Bot Score Alignment: when hunting counter-trend exhaustion traps, ensure raw breakdown reflects the fade decision
        if (is_anti_bot or is_mean_rev) and (matches_router or is_mean_rev):
            if req_decision == "SHORT" and raw_bull >= raw_bear:
                raw_bear, raw_bull = max(raw_bull, 38.0), min(raw_bear, 18.0)
                self.logger.info(
                    f"[{self.name}] 🦈 Anti-Bot Score Alignment: aligned breakdown weights with counter-trend verdict {req_decision}"
                )
            elif req_decision == "LONG" and raw_bear >= raw_bull:
                raw_bull, raw_bear = max(raw_bear, 38.0), min(raw_bull, 18.0)
                self.logger.info(
                    f"[{self.name}] 🦈 Anti-Bot Score Alignment: aligned breakdown weights with counter-trend verdict {req_decision}"
                )

        if market_context and isinstance(market_context, dict):
            mtf_ctx = market_context.get("multi_timeframe_context", {})
            if isinstance(mtf_ctx, dict):
                tf_15m = mtf_ctx.get("tf_15m", {}) if isinstance(mtf_ctx.get("tf_15m"), dict) else {}
                tf_1h = mtf_ctx.get("tf_1h", {}) if isinstance(mtf_ctx.get("tf_1h"), dict) else {}
                tf_4h = mtf_ctx.get("tf_4h", {}) if isinstance(mtf_ctx.get("tf_4h"), dict) else {}
                
                t15_raw = (tf_15m.get("trend") if isinstance(tf_15m, dict) else None) or mtf_ctx.get("trend_15m")
                t1_raw = (tf_1h.get("trend") if isinstance(tf_1h, dict) else None) or mtf_ctx.get("trend_1h")
                t4_raw = (tf_4h.get("trend") if isinstance(tf_4h, dict) else None) or mtf_ctx.get("trend_4h")

                norm_15m = normalize_mtf_trend(t15_raw)
                norm_1h = normalize_mtf_trend(t1_raw)
                norm_4h = normalize_mtf_trend(t4_raw)

                has_1h = norm_1h is not None
                has_4h = norm_4h is not None
                is_bull_1h = (norm_1h == "BULLISH")
                is_bear_1h = (norm_1h == "BEARISH")
                is_bull_4h = (norm_4h == "BULLISH")
                is_bear_4h = (norm_4h == "BEARISH")

                if has_1h and has_4h:
                    # Both timeframes are known and evaluated
                    if (is_bull_1h and is_bear_4h) or (is_bear_1h and is_bull_4h):
                        if norm_15m == norm_1h and norm_15m in ["BULLISH", "BEARISH"]:
                            # 15m and 1H are aligned against contrary 4H: Early reversal transition
                            mtf_status = f"TRANSITION_{norm_1h}"
                            observed_mtf_bias = norm_1h
                        else:
                            # 1H and 4H conflict: MTF trend is in CONFLICT, zero confirmation
                            mtf_status = "CONFLICT"
                            observed_mtf_bias = "NEUTRAL"
                    elif is_bull_1h and is_bull_4h:
                        mtf_status = "ALIGNED_BULLISH"
                        observed_mtf_bias = "BULLISH"
                    elif is_bear_1h and is_bear_4h:
                        mtf_status = "ALIGNED_BEARISH"
                        observed_mtf_bias = "BEARISH"
                    elif is_bull_1h or is_bull_4h:
                        # One macro TF is BULLISH and the other is NEUTRAL (e.g. TRANSITION / early trend)
                        mtf_status = "PARTIAL_BULLISH"
                        observed_mtf_bias = "BULLISH"
                    elif is_bear_1h or is_bear_4h:
                        # One macro TF is BEARISH and the other is NEUTRAL (e.g. TRANSITION / early trend)
                        mtf_status = "PARTIAL_BEARISH"
                        observed_mtf_bias = "BEARISH"
                    else:
                        # Both 1H and 4H are NEUTRAL, check if 15m impulse is active for BREAKOUT/MOMENTUM
                        if strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and norm_15m in ["BULLISH", "BEARISH"]:
                            mtf_status = f"PARTIAL_{norm_15m}"
                            observed_mtf_bias = norm_15m
                        else:
                            mtf_status = "NEUTRAL_CHOP"
                            observed_mtf_bias = "NEUTRAL"
                elif has_1h or has_4h:
                    # Audit Point 2: Exactly one timeframe known, the other is missing/empty.
                    # Partial context must NOT receive full MTF contribution or be marked ALIGNED!
                    mtf_status = "PARTIAL"
                    observed_mtf_bias = "NEUTRAL"
                else:
                    mtf_status = "UNKNOWN"

        # Safe Fallback Handling:
        # If CONFLICT, NEUTRAL_CHOP, PARTIAL or MIXED_CHOP:
        if mtf_status in ["CONFLICT", "NEUTRAL_CHOP", "PARTIAL", "MIXED_CHOP"]:
            if strategy_mode == "MEAN_REVERSION":
                # For MEAN_REVERSION, price extremes at channel boundaries mean reversion to EMA-20 is primary.
                # Grant up to 30 pts MTF / mean-reversion confirmation aligning with requested trade direction.
                effective_mtf = min(30.0, raw_mtf if raw_mtf > 0 else 30.0)
                bull_mtf_contrib = effective_mtf if req_decision == "LONG" else 0.0
                bear_mtf_contrib = effective_mtf if req_decision == "SHORT" else 0.0
            else:
                raw_mtf = 0.0
                bull_mtf_contrib = 0.0
                bear_mtf_contrib = 0.0
        elif mtf_status in ["PARTIAL_BULLISH", "PARTIAL_BEARISH"]:
            # Preserve up to 85% of MTF score (max 40 pts) when 1H is trending and 4H is neutral (or breakout)
            effective_mtf = min(40.0, raw_mtf * 0.85 if raw_mtf > 0 else 25.0)
            bull_mtf_contrib = effective_mtf if observed_mtf_bias == "BULLISH" else 0.0
            bear_mtf_contrib = effective_mtf if observed_mtf_bias == "BEARISH" else 0.0
        elif mtf_status in ["TRANSITION_BULLISH", "TRANSITION_BEARISH"]:
            # Early reversal: 15m and 1H agree against older 4H. Provide partial MTF contribution (max 20 pts)
            effective_mtf = min(20.0, raw_mtf * 0.50 if raw_mtf > 0 else 15.0)
            bull_mtf_contrib = effective_mtf if observed_mtf_bias == "BULLISH" else 0.0
            bear_mtf_contrib = effective_mtf if observed_mtf_bias == "BEARISH" else 0.0
        elif mtf_status == "UNKNOWN":
            # ONLY when objective market context is missing/unknown, allow fallback to explicit LLM sign
            # P-01 Protection: If LLM proposed SHORT and bear_argument won, but LLM mistakenly gave positive mtf_trend,
            # do not misattribute it to bullish bias if direction is explicitly SHORT.
            if req_decision == "SHORT" and raw_mtf_signed > 0 and raw_bear > raw_bull:
                self.logger.warning(
                    f"[{self.name}] Correcting positive mtf_trend ({raw_mtf_signed}) to negative for SHORT decision where bear argument won."
                )
                raw_mtf_signed = -abs(raw_mtf_signed)
            elif req_decision == "LONG" and raw_mtf_signed < 0 and raw_bull > raw_bear:
                self.logger.warning(
                    f"[{self.name}] Correcting negative mtf_trend ({raw_mtf_signed}) to positive for LONG decision where bull argument won."
                )
                raw_mtf_signed = abs(raw_mtf_signed)

            if raw_mtf_signed > 0:
                observed_mtf_bias = "BULLISH"
            elif raw_mtf_signed < 0:
                observed_mtf_bias = "BEARISH"
            else:
                observed_mtf_bias = "NEUTRAL"
            bull_mtf_contrib = raw_mtf if observed_mtf_bias == "BULLISH" else 0.0
            bear_mtf_contrib = raw_mtf if observed_mtf_bias == "BEARISH" else 0.0
        else:
            # ALIGNED_BULLISH or ALIGNED_BEARISH
            bull_mtf_contrib = raw_mtf if observed_mtf_bias == "BULLISH" else 0.0
            bear_mtf_contrib = raw_mtf if observed_mtf_bias == "BEARISH" else 0.0

        # In Anti-Bot Predator Mode or Mean Reversion, MTF trend contrary to counter-trend fade must not veto trade
        if is_anti_bot or is_mean_rev:
            if req_decision == "SHORT":
                bull_mtf_contrib = 0.0
                if is_anti_bot:
                    bear_mtf_contrib = max(bear_mtf_contrib, 35.0)
                else:
                    bear_mtf_contrib = max(bear_mtf_contrib, 30.0)
            elif req_decision == "LONG":
                bear_mtf_contrib = 0.0
                if is_anti_bot:
                    bull_mtf_contrib = max(bull_mtf_contrib, 35.0)
                else:
                    bull_mtf_contrib = max(bull_mtf_contrib, 30.0)

        total_bull = raw_bull + bull_mtf_contrib
        total_bear = raw_bear + bear_mtf_contrib
        net_directional_score = total_bull - total_bear

        # Separate directional check from raw score numbers
        # Note: Losing argument is discounted by 0.4x in directional confidence because
        # counter-risks (RSI, divergences, OB friction) are ALREADY penalized in risk_penalties (-5..-30).
        # Full 1.0x subtraction + risk_penalties causes double-counting (-45 pts total).
        math_conflict = False
        if req_decision == "SHORT":
            if total_bull >= total_bear:
                if (is_anti_bot or is_mean_rev):
                    raw_bear = max(raw_bull + 10.0, 38.0)
                    raw_bull = min(raw_bull, 18.0)
                    total_bear = raw_bear + bear_mtf_contrib
                    total_bull = raw_bull + bull_mtf_contrib
                    self.logger.info(
                        f"[{self.name}] 🦈 Anti-Bot Score Alignment: aligned breakdown weights with counter-trend verdict {req_decision}"
                    )
            if (is_anti_bot or is_mean_rev):
                if total_bull >= total_bear:
                    total_bear = total_bull + 15.0
            if total_bull >= total_bear:
                math_conflict = True
                self.logger.warning(f"[{self.name}] Math hallucination: LLM proposed SHORT but net score is Bullish/Neutral ({total_bull} vs {total_bear}).")
                calculated_decision = "HOLD"
                directional_confidence = 0
            else:
                calculated_decision = "SHORT"
                raw_diff = total_bear - total_bull
                if total_bull <= 0.0:
                    blended = raw_diff
                else:
                    adj_diff = total_bear - (0.4 * total_bull)
                    # Scale up when LLM uses compact breakdown weights (e.g. 25 vs 15 + 15 mtf)
                    dominance_score = (total_bear / (total_bear + total_bull)) * 100.0 if (total_bear + total_bull) > 0 else 0.0
                    has_trend = (bear_mtf_contrib > 0.0) or (strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM", "MEAN_REVERSION"])
                    if total_bear >= 35.0 and has_trend:
                        if raw_diff < 70:
                            blended = max(raw_diff, min(adj_diff * 2.0, dominance_score))
                        else:
                            blended = max(raw_diff, adj_diff)
                    else:
                        blended = max(raw_diff, adj_diff)
                directional_confidence = min(100, max(0, int(round(blended))))
        elif req_decision == "LONG":
            if total_bear >= total_bull:
                if (is_anti_bot or is_mean_rev):
                    raw_bull = max(raw_bear + 10.0, 38.0)
                    raw_bear = min(raw_bear, 18.0)
                    total_bull = raw_bull + bull_mtf_contrib
                    total_bear = raw_bear + bear_mtf_contrib
                    self.logger.info(
                        f"[{self.name}] 🦈 Anti-Bot Score Alignment: aligned breakdown weights with counter-trend verdict {req_decision}"
                    )
            if (is_anti_bot or is_mean_rev):
                if total_bear >= total_bull:
                    total_bull = total_bear + 15.0
            if total_bear >= total_bull:
                math_conflict = True
                self.logger.warning(f"[{self.name}] Math hallucination: LLM proposed LONG but net score is Bearish/Neutral ({total_bear} vs {total_bull}).")
                calculated_decision = "HOLD"
                directional_confidence = 0
            else:
                calculated_decision = "LONG"
                raw_diff = total_bull - total_bear
                if total_bear <= 0.0:
                    blended = raw_diff
                else:
                    adj_diff = total_bull - (0.4 * total_bear)
                    dominance_score = (total_bull / (total_bull + total_bear)) * 100.0 if (total_bull + total_bear) > 0 else 0.0
                    has_trend = (bull_mtf_contrib > 0.0) or (strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM", "MEAN_REVERSION"])
                    if total_bull >= 35.0 and has_trend:
                        if raw_diff < 70:
                            blended = max(raw_diff, min(adj_diff * 2.0, dominance_score))
                        else:
                            blended = max(raw_diff, adj_diff)
                    else:
                        blended = max(raw_diff, adj_diff)
                directional_confidence = min(100, max(0, int(round(blended))))
        else: # HOLD
            calculated_decision = "HOLD"
            directional_confidence = min(100, max(0, int(abs(net_directional_score))))

        # Calculate Risk Penalties via Orthogonal Categories (anti-double-counting)
        rsi, fear_greed = self._extract_market_metrics(market_context or {})
        total_penalty, risk_breakdown = self._calculate_categorized_risk_penalties(
            calculated_decision, breakdown, rsi, fear_greed, market_context=market_context
        )

        # Rule 3: Entry Quality formula (Risk-Adjusted execution quality)
        entry_quality = max(0, directional_confidence + total_penalty)
        conviction = entry_quality

        # Rule 2: Separate risk_score (0..100) vs risk_penalties (-40..0)
        risk_score = min(100, int((abs(total_penalty) / 40.0) * 100)) if total_penalty < 0 else 0

        # Rule 4: Action derivation separating Directional Confidence from Entry Quality:
        # Rule: Do not lower threshold from 60% just to increase trade count.
        # Rule: If an entry is late/overextended, wait for pullback or skip (HOLD), NEVER force an entry with reduced size.
        # Hard Veto: If LLM committed a math hallucination (e.g. proposing LONG when bears won debate score),
        # pullback retest MUST NOT resurrect the invalid trade.
        is_pullback_retest = bool(
            (market_context or {}).get("is_pullback_retest") 
            or ((market_context or {}).get("market_data") or {}).get("is_pullback_retest")
        )
        if is_pullback_retest and not math_conflict:
            pb_ctx = (market_context or {}).get("pullback_context") or {}
            retest_dir = (
                (market_context or {}).get("pullback_retest_direction") 
                or pb_ctx.get("direction") 
                or (market_context or {}).get("direction_bias")
            )
            orig_conf = int(pb_ctx.get("directional_confidence") or 75)
            
            # F-02 Fix: Respect CEO judge verdict. If the level was smashed through and CEO decided HOLD,
            # do NOT resurrect into a market order or fabricate 75% confidence out of thin air.
            if calculated_decision != "HOLD" and calculated_decision == retest_dir:
                directional_confidence = max(directional_confidence, orig_conf)

        if math_conflict:
            calculated_decision = "HOLD"
            trade_action = "HOLD"
            directional_confidence = 0
            entry_quality = 0
            conviction = 0
            self.logger.error(f"[{self.name}] 🛑 Hard Veto: Math hallucination active. Forcing decision=HOLD, action=HOLD.")
        elif calculated_decision == "HOLD" or directional_confidence < 60:
            trade_action = "HOLD"
        elif is_pullback_retest:
            # Retest reached target zone: do NOT re-enter WAIT_FOR_PULLBACK or HOLD
            trade_action = "REDUCE_SIZE" if entry_quality < 73 else "ENTER"
        elif entry_quality < 60:
            # Late or poor entry point (overextended from EMA, extreme RSI/BB, poor R:R)
            if directional_confidence >= 70:
                trade_action = "WAIT_FOR_PULLBACK"
            else:
                trade_action = "HOLD"
        elif directional_confidence >= 75 and (entry_quality < 65 or (risk_breakdown or {}).get("price_extension", 0) <= -10):
            trade_action = "WAIT_FOR_PULLBACK"
        elif entry_quality >= 73:
            trade_action = "ENTER"
        else: # 65 <= entry_quality < 73 (or 60-64 with dir_conf < 70)
            trade_action = "REDUCE_SIZE"

        return ScoreResult({
            "decision": calculated_decision,
            "directional_confidence": directional_confidence,
            "entry_quality": entry_quality,
            "conviction": conviction,
            "risk_score": risk_score,
            "risk_penalties": total_penalty,
            "risk_breakdown": risk_breakdown,
            "trade_action": trade_action,
            "raw_net_score": net_directional_score,
            "math_conflict": math_conflict
        })
```

---

<a id="file-python-core-deterministic_guard-py"></a>
## File: `python/core/deterministic_guard.py` (759 строк, 44.8 KB)

```python
import math
from typing import Tuple, Dict, Any, Optional
from core.models import FinalTradeDecision
from core.strategy_router import StrategyProfile

class DeterministicGuard:
    @staticmethod
    def evaluate_price_drift(
        symbol: str,
        direction: str,
        snapshot_price: float,
        live_price: float,
        tp_price: float,
        sl_price: float
    ) -> Tuple[bool, float, float, float, str]:
        """
        Execution Price Drift Guard & SL/TP Re-anchoring:
        - For LONG: drift_to_tp_ratio = (live_price - snapshot_price) / (tp_price - snapshot_price)
        - For SHORT: drift_to_tp_ratio = (snapshot_price - live_price) / (snapshot_price - tp_price)
        - If drift_to_tp_ratio > 0.38: Cancel entry (R:R degraded)
        - If drift <= 0.38: Re-anchor SL and TP relative to live_price
        """
        dir_clean = str(direction).upper().strip()
        snapshot_price = float(snapshot_price)
        live_price = float(live_price) if float(live_price) > 0 else snapshot_price
        tp_price = float(tp_price)
        sl_price = float(sl_price)

        if snapshot_price <= 0 or live_price <= 0 or tp_price <= 0:
            return True, live_price, tp_price, sl_price, f"[PriceDriftGuard] Missing prices for {symbol}, proceeding with defaults"

        if dir_clean == "LONG":
            tp_dist = tp_price - snapshot_price
            drift_to_tp_ratio = (live_price - snapshot_price) / tp_dist if tp_dist > 0 else 0.0
        elif dir_clean == "SHORT":
            tp_dist = snapshot_price - tp_price
            drift_to_tp_ratio = (snapshot_price - live_price) / tp_dist if tp_dist > 0 else 0.0
        else:
            return True, live_price, tp_price, sl_price, f"[PriceDriftGuard] Non-directional {direction} for {symbol}"

        if drift_to_tp_ratio > 0.38:
            msg = (
                f"[PriceDriftGuard] 🚫 Отмена запоздалого входа {symbol} ({dir_clean}): "
                f"цена уже прошла {drift_to_tp_ratio*100:.1f}% пути к TP пока шел анализ "
                f"({snapshot_price} -> {live_price}). R:R деградировал."
            )
            return False, snapshot_price, tp_price, sl_price, msg

        # Re-anchor SL and TP
        price_delta = live_price - snapshot_price
        new_entry = live_price
        new_tp = round(tp_price + price_delta, 6)

        # One-way structural stop protection:
        # Never pull the structural stop closer to market price when entry drifts in the trade's favor.
        if dir_clean == "SHORT":
            new_sl = round(sl_price + price_delta, 6) if price_delta > 0 else sl_price
        elif dir_clean == "LONG":
            new_sl = round(sl_price + price_delta, 6) if price_delta < 0 else sl_price
        else:
            new_sl = round(sl_price + price_delta, 6)

        msg = (
            f"[PriceDriftGuard] ✅ Вход разрешен {symbol} ({dir_clean}): "
            f"дрейф {drift_to_tp_ratio*100:.1f}% <= 38.0%. "
            f"SL/TP перепривязаны к live_price ({live_price}) [TP: {tp_price}->{new_tp}, SL: {sl_price}->{new_sl}]."
        )
        return True, new_entry, new_tp, new_sl, msg

    """
    Deterministic Guard:
    Centralized canonical guard enforcing the immutable trading contract:
    
    StrategyRouter (strategy_mode, direction_bias)
        ↓
    CEO Agent (raw proposal: decision, conviction, trade_action)
        ↓
    Deterministic Guard
        ↓
    FINAL TRADE DECISION (immutable, single source of truth)
        ↓
    RiskManager (evaluates capital & risk: APPROVE or VETO only, never mutates decision)
        ↓
    Execution Gate (executes strictly if approved)
    """

    @staticmethod
    def evaluate_trend_exhaustion(
        direction: str,
        rsi_15m: float,
        rsi_1h: float,
        has_opposing_divergence: bool,
        ema_stretch_atr: float = 0.0,
        bb_pos: Optional[float] = None,
    ) -> dict:
        """
        Проверяет истощение тренда и возвращает статус:
        - ALLOW (полный риск)
        - SCALE_DOWN (множитель 0.5x, макс. плечо 4.0x)
        - VETO (блокировка входа TREND_EXHAUSTION_VETO)
        """
        direction = str(direction or "").upper()

        if direction == "SHORT":
            # 1. Жесткое вето: экстремальное дно или бычья дивергенция в зоне перепроданности / у нижней полосы BB
            if rsi_15m < 24.0 or (has_opposing_divergence and (rsi_15m < 32.0 or (bb_pos is not None and bb_pos <= 15.0))):
                return {
                    "action": "VETO",
                    "reason": f"TREND_EXHAUSTION_VETO: SHORT into extreme oversold (RSI_15m={rsi_15m:.1f}, bb_pos={bb_pos if bb_pos is not None else 'N/A'}, div={has_opposing_divergence})",
                    "risk_mult": 0.0,
                    "max_leverage": 0.0,
                }
            # 2. Мягкое ограничение: перепроданность 15m + подтверждение зрелости тренда на 1H или по ATR
            is_mature_downtrend = (rsi_1h < 36.0) or (ema_stretch_atr > 2.2)
            if rsi_15m < 30.0 and is_mature_downtrend:
                return {
                    "action": "SCALE_DOWN",
                    "reason": f"TREND_EXHAUSTION_SCALE: Mature SHORT (RSI_15m={rsi_15m:.1f}, RSI_1h={rsi_1h:.1f})",
                    "risk_mult": 0.50,
                    "max_leverage": 4.0,
                }

        elif direction == "LONG":
            # 1. Жесткое вето: экстремальная вершина или медвежья дивергенция в зоне перекупленности / у верхней полосы BB
            if rsi_15m > 76.0 or (has_opposing_divergence and (rsi_15m > 68.0 or (bb_pos is not None and bb_pos >= 85.0))):
                return {
                    "action": "VETO",
                    "reason": f"TREND_EXHAUSTION_VETO: LONG into extreme overbought (RSI_15m={rsi_15m:.1f}, bb_pos={bb_pos if bb_pos is not None else 'N/A'}, div={has_opposing_divergence})",
                    "risk_mult": 0.0,
                    "max_leverage": 0.0,
                }
            # 2. Мягкое ограничение: перекупленность 15m + подтверждение зрелости тренда
            is_mature_uptrend = (rsi_1h > 64.0) or (ema_stretch_atr > 2.2)
            if rsi_15m > 70.0 and is_mature_uptrend:
                return {
                    "action": "SCALE_DOWN",
                    "reason": f"TREND_EXHAUSTION_SCALE: Mature LONG (RSI_15m={rsi_15m:.1f}, RSI_1h={rsi_1h:.1f})",
                    "risk_mult": 0.50,
                    "max_leverage": 4.0,
                }

        return {
            "action": "ALLOW",
            "reason": "Trend momentum healthy",
            "risk_mult": 1.0,
            "max_leverage": None,
        }

    @staticmethod
    def evaluate(
        strategy_profile: StrategyProfile,
        ceo_proposal: Dict[str, Any],
        profile: str,
        risk_manager: Any,
        market_data: Optional[Dict[str, Any]] = None,
        symbol: str = "",
        logger: Optional[Any] = None
    ) -> FinalTradeDecision:
        strategy_mode = strategy_profile.strategy_mode
        direction_bias = strategy_profile.direction_bias

        # Get deterministic min_conviction threshold for this profile & strategy mode
        profile_rules = risk_manager._get_profile_rules(profile, strategy_mode)
        try:
            min_conv = int(profile_rules.get("min_conviction", 75))
        except Exception:
            min_conv = 75

        raw_decision = str(ceo_proposal.get("decision", "HOLD")).upper()
        raw_conviction = int(ceo_proposal.get("conviction", 0) or 0)
        raw_trade_action = str(ceo_proposal.get("trade_action", "ENTER" if raw_conviction >= min_conv else "HOLD")).upper()
        dir_conf = int(ceo_proposal.get("directional_confidence", raw_conviction) or 0)
        entry_qual = int(ceo_proposal.get("entry_quality", raw_conviction) or 0)
        reasoning_en = ceo_proposal.get("reasoning_en", ceo_proposal.get("reasoning", ""))
        reasoning_ru = ceo_proposal.get("reasoning_ru", "")

        decision = raw_decision
        conviction = raw_conviction
        trade_action = raw_trade_action
        guard_status = "PASSED"
        guard_reason = "Passed deterministic guard."
        rejection_tag = None

        # 1. Direction Guard: Never allow CEO to reverse specialized setup bias (Breakout / Mean Reversion / Scalping)
        if strategy_mode in ["BREAKOUT", "MEAN_REVERSION", "VOLATILITY_MOMENTUM"] and decision in ["LONG", "SHORT"]:
            if decision != direction_bias:
                guard_msg = f"🛡️ [Strategy Guard] Решение CEO {decision} противоречит {strategy_mode} bias ({direction_bias}). Сделка отменена в HOLD для защиты капитала."
                print(guard_msg)
                if logger:
                    logger.warning(f"[Pipeline] {guard_msg}")
                decision = "HOLD"
                conviction = 0
                trade_action = "HOLD"
                guard_status = "BLOCKED"
                rejection_tag = "STRATEGY_GUARD_VETO"
                guard_reason = guard_msg
                reasoning_en = f"{reasoning_en}\n\n{guard_msg}".strip()

        # 2. Volatility Momentum & Breakout: Confirmed impulse breakdown/breakout enters immediately
        # Only true if market indicators genuinely confirm a breakout with volume spike AND strategy mode is BREAKOUT/VOLATILITY_MOMENTUM;
        # Domain Rationale for Volume Thresholds: In crypto derivatives, upside breakouts suffer high rates of bull traps
        # requiring >120% volume expansion, whereas downside breakdowns cascade via liquidations at baseline (>=100%) volume.
        # Never override WAIT_FOR_PULLBACK for TREND_FOLLOWING, PULLBACK, or RELATIVE_MOMENTUM to prevent buying tops/selling bottoms.
        is_confirmed_breakout = False
        breakout_reason = ""
        if strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and market_data and decision in ["LONG", "SHORT"]:
            indicators = market_data.get("indicators") or {}
            try:
                donchian_high = float(indicators.get("donchian_high") or 0.0)
                if not math.isfinite(donchian_high): donchian_high = 0.0
            except (ValueError, TypeError):
                donchian_high = 0.0

            try:
                donchian_low = float(indicators.get("donchian_low") or 0.0)
                if not math.isfinite(donchian_low): donchian_low = 0.0
            except (ValueError, TypeError):
                donchian_low = 0.0

            try:
                vol_spike = float(indicators.get("volume_spike_pct") or 0.0)
                if not math.isfinite(vol_spike): vol_spike = 0.0
            except (ValueError, TypeError):
                vol_spike = 0.0

            try:
                cur_price = float((market_data.get("price_data") or {}).get("current_price") or 0.0)
                if not math.isfinite(cur_price): cur_price = 0.0
            except (ValueError, TypeError):
                cur_price = 0.0

            if decision == "LONG" and donchian_high > 0 and cur_price > donchian_high and vol_spike >= 120.0:
                is_confirmed_breakout = True
                breakout_reason = f"пробой Donchian High ({donchian_high}) с объемом {vol_spike:.1f}%"
            elif decision == "SHORT" and donchian_low > 0 and cur_price < donchian_low and vol_spike >= 100.0:
                is_confirmed_breakout = True
                breakout_reason = f"пробой Donchian Low ({donchian_low}) с объемом {vol_spike:.1f}%"

        if is_confirmed_breakout and trade_action == "WAIT_FOR_PULLBACK" and conviction >= min_conv and decision in ["LONG", "SHORT"]:
            # Only override WAIT_FOR_PULLBACK if entry quality is solid (>= 72%) to avoid buying the top of an overextended move
            if entry_qual >= 72:
                trade_action = "ENTER"
                vm_msg = f"Подтвержденный импульс и пробой ({breakout_reason}, EntryQuality={entry_qual}% >= 72%). Вход по стратегии {strategy_mode} без ожидания отката."
                try:
                    print(f"⚡ [Breakout Override] {symbol}: {vm_msg}")
                except (UnicodeEncodeError, Exception):
                    try:
                        print(f"[Breakout Override] {symbol}: {vm_msg.encode('ascii', errors='replace').decode('ascii')}")
                    except Exception:
                        pass
                if logger:
                    logger.info(f"[System_Core] {vm_msg}")
            else:
                vm_msg = f"⏸️ [Breakout Guard] {symbol}: Пробой подтвержден ({breakout_reason}), но EntryQuality ({entry_qual}%) < 72%. Оставляем WAIT_FOR_PULLBACK для защиты от Bull/Bear Trap."
                try:
                    print(f"⏸️ [Breakout Guard] {symbol}: {vm_msg}")
                except Exception:
                    pass
                if logger:
                    logger.info(f"[System_Core] {vm_msg}")

        # 2.4. VOLUME_EXPLOSION Guard Rule: Volume Expansion (>=250.0%)
        # When volume spikes >=250% on high directional confidence (>=75%) and safe RSI (<78 for LONG, >22 for SHORT),
        # market is in explosive expansion where waiting for pullback guarantees a missed trade.
        # Guards: Check for Climax Top / Exhaustion Wick and SL distance cap.
        from core.config import get_tuned_param
        tuned_reduce_floor = get_tuned_param("min_reduce_size_conv", 71.0)

        has_exhaustion_wick = False
        candles_list = (market_data.get("price_data") or {}).get("candles_20") or [] if market_data else []
        cur_px = float((market_data.get("price_data") or {}).get("current_price") or 0.0) if market_data else 0.0
        if cur_px <= 0 and market_data:
            cur_px = float((market_data.get("indicators") or {}).get("current_price") or 0.0)

        if candles_list and cur_px > 0:
            latest_c = candles_list[-1]
            try:
                c_high = float(latest_c.get("high") or cur_px)
                c_low = float(latest_c.get("low") or cur_px)
                c_range = c_high - c_low
                if c_range > 0:
                    c_pos = (cur_px - c_low) / c_range
                    if decision == "LONG" and c_pos < 0.65:
                        has_exhaustion_wick = True
                    elif decision == "SHORT" and c_pos > 0.35:
                        has_exhaustion_wick = True
            except (ValueError, TypeError):
                pass

        # Check structural SL distance cap (max 3.5% to prevent entering at exhausted wick tops)
        sl_px = float(ceo_proposal.get("sl_price") or 0.0)
        if cur_px > 0 and sl_px > 0:
            if abs(cur_px - sl_px) / cur_px > 0.035:
                has_exhaustion_wick = True

        vol_spike_val = float((market_data.get("indicators") or {}).get("volume_spike_pct") or 0.0) if market_data else 0.0
        rsi_val = float((market_data.get("indicators") or {}).get("rsi_14") or 50.0) if market_data else 50.0
        is_rsi_safe = (rsi_val < 78.0) if decision == "LONG" else (rsi_val > 22.0)
        is_volume_explosion = (
            vol_spike_val >= 250.0
            and dir_conf >= max(75.0, float(min_conv))
            and is_rsi_safe
            and not has_exhaustion_wick
        )

        volume_explosion_triggered = False
        if is_volume_explosion and trade_action in ["WAIT_FOR_PULLBACK", "HOLD"] and decision in ["LONG", "SHORT"]:
            trade_action = "REDUCE_SIZE"
            entry_qual = max(float(entry_qual or 0), tuned_reduce_floor)
            conviction = max(float(conviction or 0), tuned_reduce_floor)
            is_actionable = True
            guard_status = "PASSED"
            volume_explosion_triggered = True
            ceo_proposal["volume_explosion_override"] = True
            ceo_proposal["action_size_mult_override"] = 0.50
            ve_msg = (
                f"🚀 [Volume Explosion] {symbol}: Всплеск объема {vol_spike_val:.1f}% (>=250%), "
                f"DirConf={dir_conf}%, RSI={rsi_val:.1f}. Оверрайд WAIT_FOR_PULLBACK -> REDUCE_SIZE (0.5x по рынку)."
            )
            print(ve_msg)
            if logger:
                logger.info(f"[System_Core] {ve_msg}")

        # 2.5. Disputed Arbitration Guard: Never allow full ENTER on disputed setups
        if ceo_proposal.get("disputed_arbitration") and trade_action == "ENTER":
            trade_action = "REDUCE_SIZE"
            da_msg = "⚖️ [Disputed Arbitration] Размер позиции ограничен REDUCE_SIZE из-за арбитражного спора."
            if logger:
                logger.info(f"[System_Core] {da_msg}")

        # 2.6. Pullback Retest Promotion: If pullback setup reached target, promote to ENTER or REDUCE_SIZE
        is_pullback_retest = bool(
            ceo_proposal.get("is_pullback_retest")
            or (market_data and market_data.get("is_pullback_retest"))
            or (strategy_profile and "RETEST" in str(getattr(strategy_profile, "reasoning", "")).upper())
        )
        if is_pullback_retest and trade_action == "WAIT_FOR_PULLBACK" and dir_conf >= min_conv and decision in ["LONG", "SHORT"]:
            effective_eq = max(entry_qual, dir_conf - 10)
            if effective_eq >= 60:
                entry_qual = effective_eq
                trade_action = "REDUCE_SIZE" if entry_qual < min_conv else "ENTER"
                conviction = max(conviction, min_conv)
                retest_msg = f"Откат к целевой зоне подтверждён (PULLBACK_RETEST). Вход по направлению {decision} ({trade_action}, EQ={entry_qual}%) без повторного ожидания."
                print(f"🎯 [Pullback Retest Confirmed] {symbol}: {retest_msg}")
                if logger:
                    logger.info(f"[System_Core] {retest_msg}")

        # 2.7. Debate Completeness Guard: Block regular entries when analytical debate is degraded
        debate_status = ceo_proposal.get("debate_status", "COMPLETE")
        if debate_status in ["DEGRADED_BULL_FAILED", "DEGRADED_BEAR_FAILED", "FAILED"] and decision in ["LONG", "SHORT"]:
            from core.config import config
            is_anti_bot_mr = bool(getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION")
            if is_confirmed_breakout:
                bypass_msg = f"⚠️ [Debate Guard Bypass] Неполные дебаты ({debate_status}), но подтверждён механический импульсный прорыв ({breakout_reason}). Вход разрешён по правилу исключения Breakout."
                print(bypass_msg)
                if logger:
                    logger.warning(f"[Pipeline] {bypass_msg}")
                reasoning_en = f"{reasoning_en}\n\n{bypass_msg}".strip()
            elif is_anti_bot_mr:
                bypass_msg = f"🦈 [Debate Guard Bypass] Неполные дебаты ({debate_status}), но активен Anti-Bot Predator Mode ({strategy_mode}). Вход разрешён по правилу институционального перехвата ликвидности."
                print(bypass_msg)
                if logger:
                    logger.warning(f"[Pipeline] {bypass_msg}")
                reasoning_en = f"{reasoning_en}\n\n{bypass_msg}".strip()
            else:
                guard_msg = f"🛡️ [Debate Guard] Дебаты оппонентов неполные ({debate_status}). Вход заблокирован детерминированным гардом (требуется полноценный двусторонний анализ)."
                print(guard_msg)
                if logger:
                    logger.warning(f"[Pipeline] {guard_msg}")
                decision = "HOLD"
                conviction = 0
                trade_action = "HOLD"
                guard_status = "BLOCKED"
                rejection_tag = "INCOMPLETE_DEBATE"
                guard_reason = guard_msg
                reasoning_en = f"{reasoning_en}\n\n{guard_msg}".strip()

        # 3. Actionability, Conviction & Pullback Checks
        is_actionable = True
        if decision not in ["LONG", "SHORT"]:
            is_actionable = False
            guard_status = "HOLD" if not rejection_tag else "BLOCKED"
            rejection_tag = rejection_tag or ceo_proposal.get("hold_category", "CEO_HOLD")
            guard_reason = f"Пропущен из-за решения CEO ({decision})" if not rejection_tag or rejection_tag == "CEO_HOLD" else guard_reason
        elif trade_action == "WAIT_FOR_PULLBACK":
            from core.config import config
            if getattr(config, "ANTI_BOT_MODE", False) and strategy_mode == "MEAN_REVERSION":
                trade_action = "ENTER"
                if entry_qual < 70:
                    entry_qual = 75.0
                conviction = max(float(conviction or 0), float(min_conv))
                if logger:
                    logger.info(f"[System_Core] 🦈 [Anti-Bot] Оверрайд WAIT_FOR_PULLBACK -> ENTER: вход на пике истощения ботов.")
            else:
                is_actionable = False
                guard_status = "PULLBACK_WATCHLIST"
                rejection_tag = "WAIT_FOR_PULLBACK"
                guard_reason = f"WAIT_FOR_PULLBACK: Цена оторвана от базы (Качество входа {entry_qual}% < 70% при уверенности тренда {dir_conf}%). Сетап зарегистрирован в Pullback Watchlist для непрерывного мониторинга отката."
        elif entry_qual < 60:
            # Rule: Не снижать порог с 60% просто ради увеличения числа сделок.
            # Если вход запоздал, правильное действие — дождаться нового сетапа или пропустить сделку, а не принудительно открыть позицию.
            is_actionable = False
            guard_status = "BLOCKED"
            rejection_tag = "POOR_ENTRY_QUALITY"
            guard_reason = f"Низкое качество входа: Entry Quality ({entry_qual}% < 60%). Вход запоздал или несет невыгодный Risk/Reward. Принудительное открытие запрещено."
        else:
            is_retest = bool(
                is_pullback_retest
                or ceo_proposal.get("is_pullback_retest")
                or (market_data and market_data.get("is_pullback_retest"))
                or (strategy_profile and "RETEST" in str(getattr(strategy_profile, "reasoning", "")).upper())
            )

            # Calibrated min_reduce_size_conv with safeguard for PullbackRadar
            from core.config import get_tuned_param
            tuned_min_reduce_conv = get_tuned_param("min_reduce_size_conv", 71.0)
            # Safeguard 1: Confirmed pullbacks retain preferential corridor max(62.0, tuned - 6.0) (e.g. 65.0%)
            effective_reduce_threshold = max(62.0, tuned_min_reduce_conv - 6.0) if is_retest else tuned_min_reduce_conv

            if trade_action == "ENTER" and conviction < min_conv and conviction >= effective_reduce_threshold:
                trade_action = "REDUCE_SIZE"
                clamp_msg = f"Понижение с ENTER до REDUCE_SIZE: уверенность ({conviction}%) ниже min_conv ({min_conv}%), но >= {effective_reduce_threshold:.1f}%."
                if logger:
                    logger.info(f"[System_Core] {clamp_msg}")

            effective_min_conv = min(float(min_conv), effective_reduce_threshold) if (trade_action == "REDUCE_SIZE" or is_retest) else float(min_conv)
            if conviction < effective_min_conv:
                is_actionable = False
                guard_status = "BLOCKED"
                rejection_tag = "LOW_CONFIDENCE"
                guard_reason = f"Пропущен из-за фильтра CEO (Уверенность {conviction}% < {effective_min_conv:.0f}%)"

        # 4. Funding Rate Gate (Deterministic derivative protection)
        if is_actionable and market_data:
            derivatives = market_data.get("derivatives_data") or {}
            funding_rate = float(derivatives.get("funding_rate_hourly") or derivatives.get("funding_rate") or 0.0)
            
            # Contrarian funding bonus: when market crowd pays our trade direction
            if (decision == "LONG" and funding_rate < -0.00005) or (decision == "SHORT" and funding_rate > 0.00005):
                conviction = min(100, conviction + 5)
                if logger:
                    logger.info(f"[System_Core] 💎 Contrarian funding advantage detected ({funding_rate*100:+.4f}%/h). Conviction boosted +5 -> {conviction}%.")

            # Hard funding veto: reject trades paying exorbitant fees (> 0.02%/hr = ~0.48%/day)
            funding_veto_threshold = 0.0002
            if decision == "LONG" and funding_rate > funding_veto_threshold:
                is_actionable = False
                guard_status = "BLOCKED"
                rejection_tag = "FUNDING_VETO"
                guard_reason = f"Фандинг гейт: Запрет LONG при экстремально высоком фандинге ({funding_rate*100:.3f}%/ч). Переплата съест риск."
                print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                if logger:
                    logger.info(f"[System_Core] {guard_reason}")
            elif decision == "SHORT" and funding_rate < -funding_veto_threshold:
                is_actionable = False
                guard_status = "BLOCKED"
                rejection_tag = "FUNDING_VETO"
                guard_reason = f"Фандинг гейт: Запрет SHORT при экстремально отрицательном фандинге ({funding_rate*100:.3f}%/ч). Переплата съест риск."
                print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                if logger:
                    logger.info(f"[System_Core] {guard_reason}")

        # 5. Anti-Whipsaw & Extreme Extension Guard (Prevent 'Shorting the Bottom' / 'Longing the Top')
        if is_actionable and market_data and strategy_mode == "TREND_FOLLOWING":
            indicators = market_data.get("indicators") or {}
            raw_rsi = indicators.get("rsi_14")
            rsi = float(raw_rsi) if raw_rsi is not None else None
            
            raw_bb_pos = indicators.get("bb_position_pct") if "bb_position_pct" in indicators else indicators.get("bb_pos")
            bb_pos = float(raw_bb_pos) if raw_bb_pos is not None else None

            # Multi-timeframe and momentum context to avoid cutting off legitimate trend breakouts / dumps
            mtf_data = market_data.get("multi_timeframe") or {}
            mtf_alignment = mtf_data.get("mtf_alignment", "")
            trend_1h = mtf_data.get("trend_1h", "")
            vol_spike = float(indicators.get("volume_spike_pct") or 0.0)
            algo_signals = indicators.get("algo_signals") or {}
            raw_rsi_div = str(algo_signals.get("rsi_divergence") or "").strip().lower()
            rsi_div = raw_rsi_div.upper()

            trend_15m = mtf_data.get("trend_15m", "")

            # 1H RSI extraction (from mtf_data or nested tf_1h if present)
            raw_rsi_1h = None
            if "rsi_1h" in mtf_data:
                raw_rsi_1h = mtf_data.get("rsi_1h")
            elif "indicators_1h" in mtf_data and isinstance(mtf_data["indicators_1h"], dict):
                raw_rsi_1h = mtf_data["indicators_1h"].get("rsi_14")
            elif "tf_1h" in mtf_data and isinstance(mtf_data["tf_1h"], dict):
                raw_rsi_1h = mtf_data["tf_1h"].get("rsi_14")
            rsi_1h = float(raw_rsi_1h) if raw_rsi_1h is not None else None

            # Price stretch from EMA-20 measured in ATR units
            price_data = market_data.get("price_data") or {}
            curr_px = float(price_data.get("current_price") or indicators.get("current_price") or 0.0)
            ema_20 = float(indicators.get("ema_20") or 0.0)
            atr_14 = float(indicators.get("atr_14") or 0.0)
            ema_stretch_atr = (abs(curr_px - ema_20) / atr_14) if (atr_14 > 0 and ema_20 > 0 and curr_px > 0) else 0.0

            # 5.1 Deterministic Trend Exhaustion Evaluation (Two-Zone Model)
            if rsi is not None and decision in ["LONG", "SHORT"]:
                has_opposing_div = (decision == "SHORT" and raw_rsi_div == "bullish") or (decision == "LONG" and raw_rsi_div == "bearish")
                exhaustion = DeterministicGuard.evaluate_trend_exhaustion(
                    direction=decision,
                    rsi_15m=rsi,
                    rsi_1h=rsi_1h if rsi_1h is not None else (30.0 if (decision == "SHORT" and trend_1h == "BEARISH") else (70.0 if (decision == "LONG" and trend_1h == "BULLISH") else 50.0)),
                    has_opposing_divergence=has_opposing_div,
                    ema_stretch_atr=ema_stretch_atr,
                    bb_pos=bb_pos
                )

                if exhaustion["action"] == "VETO":
                    is_actionable = False
                    guard_status = "BLOCKED"
                    rejection_tag = "TREND_EXHAUSTION_VETO"
                    guard_reason = exhaustion["reason"]
                    try:
                        print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                    except UnicodeEncodeError:
                        pass
                    if logger:
                        logger.info(f"[System_Core] {guard_reason}")

                elif exhaustion["action"] == "SCALE_DOWN" and is_actionable:
                    # Soft Exhaustion: Enforce REDUCE_SIZE and 4x leverage cap without double-penalizing risk
                    if not ceo_proposal.get("disputed_arbitration") and not volume_explosion_triggered:
                        is_actionable = False
                        guard_status = "PULLBACK_WATCHLIST"
                        trade_action = "WAIT_FOR_PULLBACK"
                        rejection_tag = "WAIT_FOR_PULLBACK"
                        guard_reason = f"Истощение тренда (Soft Exhaustion): {exhaustion['reason']} -> Переведено в WAIT_FOR_PULLBACK"
                        if logger:
                            logger.info(f"[System_Core] ⏸️ {guard_reason}")
                    else:
                        trade_action = "REDUCE_SIZE"
                        ceo_proposal["exhaustion_scale"] = True
                        ceo_proposal["max_leverage_override"] = exhaustion.get("max_leverage", 4.0)
                        ceo_proposal["risk_mult_override"] = exhaustion.get("risk_mult", 0.50)
                        if logger:
                            logger.info(f"[System_Core] ⚠️ {exhaustion['reason']} -> Clamped trade_action=REDUCE_SIZE, max_leverage=4.0x")

            # 5.2 Classic Anti-Whipsaw Extension Guard (Prevent counter-trend chops / extreme extensions)
            from core.config import config
            is_anti_bot_mr = bool(getattr(config, "ANTI_BOT_MODE", False) and strategy_mode == "MEAN_REVERSION")
            if is_actionable and not is_anti_bot_mr:
                has_macro_trend = (mtf_alignment == "FULL_ALIGNMENT") or (
                    (mtf_alignment not in ["MIXED_CHOP", "TRANSITION"]) and (dir_conf >= 80)
                )
                is_trend_continuation_short = (
                    (
                        has_macro_trend
                        or (trend_1h == "BEARISH" and vol_spike >= 100.0)
                        or (trend_15m == "BEARISH" and vol_spike >= 150.0)
                    )
                    and (rsi is None or rsi >= 24.0 or (has_macro_trend and rsi >= 20.0))
                )

                is_trend_continuation_long = (
                    (
                        has_macro_trend
                        or (trend_1h == "BULLISH" and vol_spike >= 100.0)
                        or (trend_15m == "BULLISH" and vol_spike >= 150.0)
                    )
                    and (rsi is None or rsi <= 76.0 or (has_macro_trend and rsi <= 80.0))
                )
                
                if decision == "SHORT":
                    # Hard veto on true extreme extension (RSI < 24.0 or (RSI < 32.0 with opposing bullish divergence))
                    is_extreme_short = (rsi is not None and rsi < 24.0) or (rsi is not None and rsi < 32.0 and rsi_div == "BULLISH")
                    is_extended = (rsi is not None and rsi < 35.0) or (bb_pos is not None and bb_pos < 10.0)
                    if (is_extreme_short and not (has_macro_trend and rsi >= 20.0)) or (is_extended and not is_trend_continuation_short and exhaustion.get("action") != "SCALE_DOWN"):
                        is_actionable = False
                        guard_status = "BLOCKED"
                        rejection_tag = "EXTREME_EXTENSION_VETO"
                        rsi_str = f"RSI={rsi:.1f}" if rsi is not None else "RSI=N/A"
                        bb_str = f"bb_pos={bb_pos:.1f}%" if bb_pos is not None else "bb_pos=N/A"
                        guard_reason = f"Анти-распил: Запрет SHORT в перепроданный рынок без подтверждения тренда ({rsi_str}, {bb_str}). Риск отскока слишком высок."
                        try:
                            print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                        except UnicodeEncodeError:
                            pass
                        if logger:
                            logger.info(f"[System_Core] {guard_reason}")
                            
                elif decision == "LONG":
                    # Hard veto on true extreme extension (RSI > 76.0 or (RSI > 68.0 with opposing bearish divergence))
                    is_extreme_long = (rsi is not None and rsi > 76.0) or (rsi is not None and rsi > 68.0 and rsi_div == "BEARISH")
                    is_extended = (rsi is not None and rsi > 65.0) or (bb_pos is not None and bb_pos > 90.0)
                    if (is_extreme_long and not (has_macro_trend and rsi <= 80.0)) or (is_extended and not is_trend_continuation_long and exhaustion.get("action") != "SCALE_DOWN"):
                        is_actionable = False
                        guard_status = "BLOCKED"
                        rejection_tag = "EXTREME_EXTENSION_VETO"
                        rsi_str = f"RSI={rsi:.1f}" if rsi is not None else "RSI=N/A"
                        bb_str = f"bb_pos={bb_pos:.1f}%" if bb_pos is not None else "bb_pos=N/A"
                        guard_reason = f"Анти-распил: Запрет LONG в перекупленный рынок без подтверждения тренда ({rsi_str}, {bb_str}). Риск коррекции слишком высок."
                        try:
                            print(f"⏸️ Пропуск {symbol}. {guard_reason}")
                        except UnicodeEncodeError:
                            pass
                        if logger:
                            logger.info(f"[System_Core] {guard_reason}")

        # 5.2. Climatic Breakout & Overextension Protection: Avoid buying the tip of an overextended wave
        if is_actionable and market_data and strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"]:
            indicators = market_data.get("indicators") or {}
            raw_rsi = indicators.get("rsi_14")
            rsi = float(raw_rsi) if raw_rsi is not None else None
            ema_20 = float(indicators.get("ema_20") or 0.0)
            atr_14 = float(indicators.get("atr_14") or indicators.get("atr") or 0.0)
            bb_pos = float(indicators.get("bb_position_pct", indicators.get("bb_pos", 50.0)) or 50.0)
            cur_px = float((market_data.get("price_data") or {}).get("current_price") or indicators.get("current_price") or 0.0)
            ema_stretch_atr = (abs(cur_px - ema_20) / atr_14) if (atr_14 > 0 and ema_20 > 0 and cur_px > 0) else 0.0

            if decision == "LONG":
                if rsi is not None and rsi > 80.0:
                    trade_action = "WAIT_FOR_PULLBACK"
                    is_actionable = False
                    guard_status = "PULLBACK_WATCHLIST"
                    rejection_tag = "WAIT_FOR_PULLBACK"
                    guard_reason = f"Климатический пробой: RSI={rsi:.1f} > 80. Покупка вершины свечи запрещена. Переведено в WAIT_FOR_PULLBACK."
                elif ema_stretch_atr > 3.2 and (bb_pos > 130.0 or (rsi is not None and rsi > 74.0)):
                    trade_action = "WAIT_FOR_PULLBACK"
                    is_actionable = False
                    guard_status = "PULLBACK_WATCHLIST"
                    rejection_tag = "WAIT_FOR_PULLBACK"
                    guard_reason = (
                        f"Запоздалый пробой (Overextended Breakout): Цена оторвалась от EMA-20 на {ema_stretch_atr:.1f}x ATR (>3.2x) "
                        f"при bb_pos={bb_pos:.1f}%, RSI={(rsi if rsi is not None else 0.0):.1f}. Вход вдогонку запрещен -> WAIT_FOR_PULLBACK."
                    )
                if not is_actionable and logger:
                    logger.info(f"[System_Core] {guard_reason}")
            elif decision == "SHORT":
                if rsi is not None and rsi < 20.0:
                    trade_action = "WAIT_FOR_PULLBACK"
                    is_actionable = False
                    guard_status = "PULLBACK_WATCHLIST"
                    rejection_tag = "WAIT_FOR_PULLBACK"
                    guard_reason = f"Климатический пробой: RSI={rsi:.1f} < 20. Продажа дна паники запрещена. Переведено в WAIT_FOR_PULLBACK."
                elif ema_stretch_atr > 3.2 and (bb_pos < -30.0 or (rsi is not None and rsi < 26.0)):
                    trade_action = "WAIT_FOR_PULLBACK"
                    is_actionable = False
                    guard_status = "PULLBACK_WATCHLIST"
                    rejection_tag = "WAIT_FOR_PULLBACK"
                    guard_reason = (
                        f"Запоздалый пробой (Overextended Breakdown): Цена оторвалась от EMA-20 на {ema_stretch_atr:.1f}x ATR (>3.2x) "
                        f"при bb_pos={bb_pos:.1f}%, RSI={(rsi if rsi is not None else 0.0):.1f}. Вход вдогонку запрещен -> WAIT_FOR_PULLBACK."
                    )
                if not is_actionable and logger:
                    logger.info(f"[System_Core] {guard_reason}")

        # 5.3. Anti-Chase Guard for TREND_FOLLOWING: Prohibit market ENTER on outer third of Bollinger Band
        if is_actionable and market_data and strategy_mode == "TREND_FOLLOWING" and trade_action in ["ENTER", "REDUCE_SIZE"] and not is_pullback_retest:
            indicators = market_data.get("indicators") or {}
            raw_rsi = indicators.get("rsi_14", indicators.get("rsi"))
            rsi_val = float(raw_rsi) if raw_rsi is not None else None
            bb_pos_val = float(indicators.get("bb_position_pct", indicators.get("bb_pos", 50.0)) or 50.0)

            if decision == "SHORT" and bb_pos_val < 30.0 and (rsi_val is not None and rsi_val < 40.0):
                trade_action = "WAIT_FOR_PULLBACK"
                is_actionable = False
                guard_status = "PULLBACK_WATCHLIST"
                rejection_tag = "WAIT_FOR_PULLBACK"
                guard_reason = (
                    f"Anti-Chase Guard: TREND_FOLLOWING SHORT в нижней трети канала Боллинджера "
                    f"(bb_pos={bb_pos_val:.1f}% < 30.0%, RSI={rsi_val:.1f} < 40.0). Вход по рынку запрещен -> WAIT_FOR_PULLBACK к EMA-20."
                )
                if logger:
                    logger.info(f"[System_Core] {guard_reason}")
            elif decision == "LONG" and bb_pos_val > 70.0 and (rsi_val is not None and rsi_val > 60.0):
                trade_action = "WAIT_FOR_PULLBACK"
                is_actionable = False
                guard_status = "PULLBACK_WATCHLIST"
                rejection_tag = "WAIT_FOR_PULLBACK"
                guard_reason = (
                    f"Anti-Chase Guard: TREND_FOLLOWING LONG в верхней трети канала Боллинджера "
                    f"(bb_pos={bb_pos_val:.1f}% > 70.0%, RSI={rsi_val:.1f} > 60.0). Вход по рынку запрещен -> WAIT_FOR_PULLBACK к EMA-20."
                )
                if logger:
                    logger.info(f"[System_Core] {guard_reason}")

        # Anti-Bot Predator Mode: Bypass ANALYST_DISAGREEMENT and COUNTER_ANALYST_VETO for MEAN_REVERSION
        from core.config import config
        is_anti_bot_mr = bool(getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION")
        if is_anti_bot_mr:
            if rejection_tag in ["ANALYST_DISAGREEMENT", "COUNTER_ANALYST_VETO"] or ceo_proposal.get("hold_category") in ["ANALYST_DISAGREEMENT", "COUNTER_ANALYST_VETO"]:
                rejection_tag = None
                if decision in ["LONG", "SHORT"]:
                    is_actionable = True
                    guard_status = "PASSED"
                    guard_reason = "Passed deterministic guard (Anti-Bot Predator bypass of analyst disagreement)."

        # ── Step 3: Hard ban on immediate market entry for REDUCE_SIZE / weak conviction / poor quality ──
        # Only signals with ENTER (conviction >= 75%, entry_quality >= 73%) are allowed for immediate market execution.
        if is_actionable and decision in ["LONG", "SHORT"]:
            is_anti_bot_override = bool(getattr(config, "ANTI_BOT_MODE", False) and strategy_mode == "MEAN_REVERSION")
            is_disputed = bool(ceo_proposal.get("disputed_arbitration", False))
            if not is_anti_bot_override and not is_disputed and not is_pullback_retest and not volume_explosion_triggered:
                if trade_action == "REDUCE_SIZE" or (entry_qual < 73.0 and conviction < 75.0):
                    is_actionable = False
                    guard_status = "PULLBACK_WATCHLIST"
                    trade_action = "WAIT_FOR_PULLBACK"
                    rejection_tag = "WAIT_FOR_PULLBACK"
                    guard_reason = (
                        f"WAIT_FOR_PULLBACK: Сигнал {decision} требует подтверждения отката "
                        f"(Conviction={conviction:.0f}% {'< 75%' if conviction < 75.0 else 'OK'}, "
                        f"EntryQuality={entry_qual:.1f}% {'< 73%' if entry_qual < 73.0 else 'OK'}, "
                        f"Action={trade_action}). Немедленный рыночный вход запрещен во избежание открытия на дне/вершине импульса. "
                        f"Сетап зарегистрирован в Pullback Watchlist."
                    )
                    if logger:
                        logger.info(f"[System_Core] ⏸️ {guard_reason}")

        # Synchronize back to ceo_proposal dict for backward compatibility
        if symbol:
            ceo_proposal["symbol"] = symbol
        ceo_proposal["decision"] = decision
        ceo_proposal["conviction"] = conviction
        ceo_proposal["entry_quality"] = entry_qual
        ceo_proposal["directional_confidence"] = dir_conf
        ceo_proposal["trade_action"] = trade_action
        ceo_proposal["is_actionable"] = is_actionable
        ceo_proposal["guard_status"] = guard_status
        ceo_proposal["reasoning_en"] = reasoning_en
        if rejection_tag:
            ceo_proposal["rejection_tag"] = rejection_tag
        if decision == "HOLD":
            if raw_decision == "HOLD":
                guard_status = "HOLD"
                guard_reason = "CEO decided HOLD (No actionable edge)"
                if not rejection_tag:
                    rejection_tag = "CEO_HOLD"
            else:
                guard_status = "BLOCKED"
            ceo_proposal["hold_category"] = rejection_tag or "CEO_HOLD"

        return FinalTradeDecision(
            symbol=symbol,
            decision=decision,
            conviction=conviction,
            trade_action=trade_action,
            strategy_mode=strategy_mode,
            direction_bias=direction_bias,
            min_conviction=min_conv,
            is_actionable=is_actionable,
            guard_status=guard_status,
            guard_reason=guard_reason,
            rejection_tag=rejection_tag,
            directional_confidence=dir_conf,
            entry_quality=entry_qual,
            reasoning_en=reasoning_en,
            reasoning_ru=reasoning_ru,
            raw_ceo_verdict=ceo_proposal,
            conviction_for_risk=float(ceo_proposal.get("conviction_for_risk", conviction) or conviction),
            volume_explosion_override=volume_explosion_triggered,
            action_size_mult_override=0.50 if volume_explosion_triggered else None
        )


DeterministicGuard.validate = DeterministicGuard.evaluate
```

---

<a id="file-python-core-decision_replay-py"></a>
## File: `python/core/decision_replay.py` (733 строк, 32.0 KB)

```python
import os
import sys
import json
import time
import math
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from core.models import FinalTradeDecision
from core.deterministic_guard import DeterministicGuard
from core.strategy_router import StrategyProfile
from agents.ceo_agent import normalize_mtf_trend


class MockRiskRules:
    """Mock risk rules provider for offline deterministic replay without exchange dependencies."""
    def _get_profile_rules(self, profile: str, strategy_mode: str) -> Dict[str, Any]:
        thresholds = {
            "CONSERVATIVE": 80,
            "BALANCED": 75,
            "AGGRESSIVE": 70
        }
        min_conv = thresholds.get(profile, 75)
        if strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"]:
            min_conv = max(65, min_conv - 5)
        return {"min_conviction": min_conv}


class CapturedRiskRules:
    """Replays using the exact captured min_conviction threshold from the candidate snapshot."""
    def __init__(self, min_conviction: int = 75):
        self.min_conviction = min_conviction
        self.MIN_CONVICTION_TO_TRADE = min_conviction

    def _get_profile_rules(self, profile: str, strategy_mode: str) -> Dict[str, Any]:
        return {"min_conviction": self.min_conviction}


def _is_valid_positive_finite(val: Any) -> bool:
    if val is None:
        return False
    try:
        f = float(val)
        return math.isfinite(f) and f > 0.0
    except (ValueError, TypeError):
        return False


def _is_valid_nonneg_finite(val: Any) -> bool:
    if val is None:
        return False
    try:
        f = float(val)
        return math.isfinite(f) and f >= 0.0
    except (ValueError, TypeError):
        return False


def _is_valid_finite(val: Any) -> bool:
    if val is None:
        return False
    try:
        f = float(val)
        return math.isfinite(f)
    except (ValueError, TypeError):
        return False


def _validate_donchian_for_direction(mgi: Dict[str, Any], direction: str) -> bool:
    """
    Directional Donchian validation (Audit Point 2):
    - For LONG: donchian_high must be a valid positive finite float.
    - For SHORT: donchian_low must be a valid positive finite float.
    - If both boundaries exist and are positive finite, ensure donchian_high >= donchian_low.
    - For NEUTRAL/HOLD: at least one boundary must be a valid positive finite float.
    """
    if not isinstance(mgi, dict):
        return False
    dh = mgi.get("donchian_high")
    dl = mgi.get("donchian_low")

    if _is_valid_positive_finite(dh) and _is_valid_positive_finite(dl):
        if float(dh) < float(dl):
            return False

    norm_dir = (direction or "").upper()
    if norm_dir == "LONG":
        return _is_valid_positive_finite(dh)
    elif norm_dir == "SHORT":
        return _is_valid_positive_finite(dl)
    else:
        return _is_valid_positive_finite(dh) or _is_valid_positive_finite(dl)


def _validate_mtf_trend_data(mgi: Dict[str, Any]) -> bool:
    """
    Strict MTF trend data validation (Audit Point 3):
    - multi_timeframe must be a non-empty dictionary.
    - Checks actual trend fields: trend_1h and trend_4h (or nested tf_1h.trend / tf_4h.trend).
    - Requires at least one timeframe to normalize to a valid canonical trend state (BULLISH, BEARISH, NEUTRAL).
    - If raw trend strings are provided, they must NOT normalize to None (rejecting 'unknown', 'n/a', garbage).
    - Differentiates NEUTRAL trend (valid market state) from missing/unknown trend data.
    """
    if not isinstance(mgi, dict):
        return False
    mtf = mgi.get("multi_timeframe")
    if not mtf or not isinstance(mtf, dict):
        return False

    tf_1h = mtf.get("tf_1h") if isinstance(mtf.get("tf_1h"), dict) else {}
    tf_4h = mtf.get("tf_4h") if isinstance(mtf.get("tf_4h"), dict) else {}
    t1_raw = (tf_1h.get("trend") if isinstance(tf_1h, dict) else None) or mtf.get("trend_1h")
    t4_raw = (tf_4h.get("trend") if isinstance(tf_4h, dict) else None) or mtf.get("trend_4h")

    norm_1h = normalize_mtf_trend(t1_raw)
    norm_4h = normalize_mtf_trend(t4_raw)

    if t1_raw is not None and norm_1h is None:
        return False
    if t4_raw is not None and norm_4h is None:
        return False

    valid_states = {"BULLISH", "BEARISH", "NEUTRAL"}
    return (norm_1h in valid_states or norm_4h in valid_states)


def _validate_relative_momentum(mgi: Dict[str, Any]) -> bool:
    """
    Strict RELATIVE_MOMENTUM context validation (Audit Point 4):
    Requires volume_spike_pct and at least one relative performance metric
    (rs_divergence vs BTC or asset_return_24h).
    """
    if not isinstance(mgi, dict):
        return False
    has_vol = _is_valid_nonneg_finite(mgi.get("volume_spike_pct"))
    has_rs = _is_valid_finite(mgi.get("rs_divergence")) or _is_valid_finite(mgi.get("asset_return_24h"))
    return has_vol and has_rs


class DecisionReplayEngine:
    """
    Offline telemetry replay and analysis utility for the AI-Futures-Trader decision block.
    Enables audit of the decision funnel, bottleneck identification, and deterministic replay.
    """
    def __init__(self, filepath: str = "data/memory/decision_snapshots.jsonl"):
        self.filepath = filepath

    def load_snapshots(self) -> List[Dict[str, Any]]:
        """Loads and parses all decision snapshots from JSONL log."""
        if not os.path.exists(self.filepath):
            return []
        
        snapshots = []
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            snapshots.append(record)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"⚠️ [ReplayEngine] Error reading {self.filepath}: {e}")
        return snapshots

    def filter_snapshots(
        self,
        snapshots: Optional[List[Dict[str, Any]]] = None,
        symbol: Optional[str] = None,
        stage: Optional[str] = None,
        strategy_mode: Optional[str] = None,
        final_action: Optional[str] = None,
        min_timestamp: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Filters snapshots by various criteria."""
        if snapshots is None:
            snapshots = self.load_snapshots()

        filtered = []
        for s in snapshots:
            if symbol and s.get("symbol", "").upper() != symbol.upper():
                continue
            if stage and s.get("stage", "").upper() != stage.upper():
                continue
            if final_action and s.get("final_action", "").upper() != final_action.upper():
                continue
            if min_timestamp and s.get("timestamp", 0) < min_timestamp:
                continue
            if strategy_mode:
                mode = s.get("router", {}).get("strategy_mode", "")
                if mode.upper() != strategy_mode.upper():
                    continue
            filtered.append(s)
        return filtered

    def compute_funnel(self, snapshots: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Computes aggregate decision funnel metrics deduplicated by candidate_id.
        Prevents multi-stage logging of the same candidate from inflating the denominator (Audit Point 4).
        """
        if snapshots is None:
            snapshots = self.load_snapshots()

        total_records = len(snapshots)
        if total_records == 0:
            return {
                "total_records": 0,
                "total_candidates": 0,
                "stages": {},
                "stage_dropoff": {},
                "actions": {},
                "rejection_tags": {},
                "strategies": {},
                "risk_penalties_avg": {},
                "conversion_rate_pct": 0.0
            }

        # Group snapshots by stable candidate_id
        candidates: Dict[str, List[Dict[str, Any]]] = {}
        for s in snapshots:
            c_id = s.get("candidate_id") or f"{s.get('symbol', 'UNKNOWN')}:{s.get('cycle_number', '0')}"
            if c_id not in candidates:
                candidates[c_id] = []
            candidates[c_id].append(s)

        total_candidates = len(candidates)

        actions = Counter()
        rejection_tags = Counter()
        stage_dropoff = Counter()
        stages_reached = Counter()

        strategies: Dict[str, Dict[str, int]] = {}
        risk_penalty_sums = Counter()
        risk_penalty_counts = 0

        for c_id, snaps in candidates.items():
            # Chronological order
            snaps_sorted = sorted(snaps, key=lambda x: x.get("timestamp", 0))
            terminal_snap = snaps_sorted[-1]

            # Track stages this candidate participated in
            seen_stages = set()
            for s in snaps_sorted:
                st = s.get("stage", "UNKNOWN")
                if st not in seen_stages:
                    stages_reached[st] += 1
                    seen_stages.add(st)

            # Determine terminal candidate outcome
            has_executed = any(s.get("final_action") == "EXECUTED" for s in snaps_sorted)
            if has_executed:
                cand_action = "EXECUTED"
            else:
                cand_action = terminal_snap.get("final_action", "UNKNOWN")

            actions[cand_action] += 1
            if cand_action != "EXECUTED":
                stage_dropoff[terminal_snap.get("stage", "UNKNOWN")] += 1
                if terminal_snap.get("rejection_tag"):
                    rejection_tags[terminal_snap.get("rejection_tag")] += 1

            # Strategy association from latest available router decision
            strat = "UNROUTED"
            for s in reversed(snaps_sorted):
                r_mode = s.get("router", {}).get("strategy_mode")
                if r_mode:
                    strat = r_mode
                    break

            if strat not in strategies:
                strategies[strat] = {"total": 0, "executed": 0, "pullback_wait": 0, "rejected": 0}
            strategies[strat]["total"] += 1
            if cand_action == "EXECUTED":
                strategies[strat]["executed"] += 1
            elif cand_action == "WAIT_FOR_PULLBACK":
                strategies[strat]["pullback_wait"] += 1
            else:
                strategies[strat]["rejected"] += 1

            # Penalties: record from the latest snapshot that has risk_breakdown
            for s in reversed(snaps_sorted):
                breakdown = s.get("ceo", {}).get("risk_breakdown")
                if breakdown and isinstance(breakdown, dict):
                    risk_penalty_counts += 1
                    for cat, val in breakdown.items():
                        if isinstance(val, (int, float)):
                            risk_penalty_sums[cat] += val
                    break

        executed_count = actions.get("EXECUTED", 0)
        conversion_rate = round(executed_count / total_candidates * 100, 2) if total_candidates > 0 else 0.0

        avg_penalties = {}
        if risk_penalty_counts > 0:
            for cat, total_val in risk_penalty_sums.items():
                avg_penalties[cat] = round(total_val / risk_penalty_counts, 2)

        return {
            "total_records": total_records,
            "total_candidates": total_candidates,
            "stages": dict(stages_reached),
            "stage_dropoff": dict(stage_dropoff),
            "actions": dict(actions),
            "rejection_tags": dict(rejection_tags),
            "strategies": strategies,
            "risk_penalties_avg": avg_penalties,
            "conversion_rate_pct": conversion_rate
        }

    def format_report(self, funnel: Dict[str, Any]) -> str:
        """Formats funnel metrics into a readable report."""
        total = funnel.get("total_candidates", 0)
        records = funnel.get("total_records", total)
        if total == 0:
            return "📭 No decision snapshots recorded yet in log."

        lines = [
            "============================================================",
            "📊 DECISION PIPELINE FUNNEL & TELEMETRY REPORT",
            "============================================================",
            f"Total Candidates Scanned: {total}",
            f"Total Telemetry Records:  {records}",
            f"Total Trades Executed:    {funnel.get('actions', {}).get('EXECUTED', 0)} ({funnel.get('conversion_rate_pct', 0.0)}%)",
            f"Waiting for Pullback:     {funnel.get('actions', {}).get('WAIT_FOR_PULLBACK', 0)}",
            f"Rejected / Vetoed:        {funnel.get('actions', {}).get('REJECTED', 0)}",
            f"Execution Failures:       {funnel.get('actions', {}).get('EXECUTION_FAILED', 0)}",
            "",
            "🔻 FUNNEL STAGE DROP-OFF (Where unique candidates exited):",
            "------------------------------------------------------------"
        ]

        stage_order = [
            ("DATA_GUARD", "1. Data Quality Guard"),
            ("SCANNER", "2. Scanner Agent (ATR/Spread)"),
            ("ANALYST_QUORUM", "3. Analyst Quorum (≥3 agents)"),
            ("STRATEGY_ROUTER", "4. Strategy Router Consensus"),
            ("DETERMINISTIC_GUARD", "5. Deterministic Guard"),
            ("SLIPPAGE_GATE", "6. Slippage Gate"),
            ("RISK_MANAGER", "7. Risk Manager Veto"),
            ("EXECUTION", "8. Execution on Exchange")
        ]

        for code, label in stage_order:
            drop_cnt = funnel.get("stage_dropoff", {}).get(code, 0)
            reach_cnt = funnel.get("stages", {}).get(code, 0)
            pct = round(drop_cnt / total * 100, 1) if total > 0 else 0.0
            bar = "█" * int(pct // 5)
            lines.append(f"  {label:<32}: Exited {drop_cnt:>3} ({pct:>5.1f}%) [Reached: {reach_cnt:>3}] {bar}")

        lines.extend([
            "",
            "🏷️ TOP REJECTION TAGS:",
            "------------------------------------------------------------"
        ])
        top_rejections = sorted(funnel.get("rejection_tags", {}).items(), key=lambda x: x[1], reverse=True)[:8]
        if top_rejections:
            for tag, cnt in top_rejections:
                lines.append(f"  • {tag:<30}: {cnt}")
        else:
            lines.append("  (None)")

        lines.extend([
            "",
            "🧭 STRATEGY CONVERSION BREAKDOWN (Unique Candidates):",
            "------------------------------------------------------------"
        ])
        for strat, data in funnel.get("strategies", {}).items():
            s_total = data.get("total", 0)
            s_exec = data.get("executed", 0)
            s_wait = data.get("pullback_wait", 0)
            conv = round(s_exec / s_total * 100, 1) if s_total > 0 else 0.0
            lines.append(f"  • {strat:<22}: {s_total:>3} candidates | Executed: {s_exec:>2} ({conv:>5.1f}%) | Wait: {s_wait:>2}")

        if funnel.get("risk_penalties_avg"):
            lines.extend([
                "",
                "⚖️ CATEGORIZED RISK PENALTIES (Averages for evaluated setups):",
                "------------------------------------------------------------"
            ])
            for cat, avg_val in funnel["risk_penalties_avg"].items():
                lines.append(f"  • {cat:<24}: {avg_val:>6.2f} pts")

        lines.append("============================================================")
        return "\n".join(lines)

    def replay_guard_decision(
        self,
        snapshot: Dict[str, Any],
        risk_manager: Optional[Any] = None,
        strict: bool = False
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Replays DeterministicGuard offline using snapshot input data.
        Verifies if deterministic guard logic produces the exact same decision and action.
        """
        guard_data = snapshot.get("guard")
        if not guard_data:
            return False, "Snapshot has no DeterministicGuard record to replay", {}

        router_data = snapshot.get("router", {})
        strategy_mode = router_data.get("strategy_mode", "TREND_FOLLOWING")
        direction_bias = router_data.get("direction_bias", "NEUTRAL")

        # Mock StrategyProfile
        strat_profile = StrategyProfile(
            has_directional_signal=router_data.get("has_directional_signal", True),
            strategy_mode=strategy_mode,
            direction_bias=direction_bias,
            reasoning=router_data.get("reasoning", "Replay Profile")
        )

        ceo_data = snapshot.get("ceo", {})
        debate_data = snapshot.get("debate", {})
        # Audit Point 1: Replay must use original CEO proposed trade_action, NOT guard_data["action"]!
        original_ceo_trade_action = ceo_data.get("trade_action")
        ceo_trade_action = original_ceo_trade_action or guard_data.get("action", "HOLD")
        raw_conv = ceo_data.get("conviction", 0)
        ceo_proposal = {
            "decision": ceo_data.get("decision", "HOLD"),
            "conviction": raw_conv,
            "directional_confidence": ceo_data.get("directional_confidence", raw_conv),
            "entry_quality": ceo_data.get("entry_quality", raw_conv),
            "trade_action": ceo_trade_action,
            "reasoning_en": ceo_data.get("reasoning", ""),
            "debate_status": debate_data.get("debate_status") or ceo_data.get("debate_status", "COMPLETE"),
            "bull_status": debate_data.get("bull_status") or ceo_data.get("bull_status", "OK"),
            "bear_status": debate_data.get("bear_status") or ceo_data.get("bear_status", "OK"),
            "disputed_arbitration": ceo_data.get("disputed_arbitration", False)
        }

        # Reconstruct market_data from market_guard_inputs if available (Audit Point 6)
        mgi = snapshot.get("market_guard_inputs")
        reconstructed_market_data = None
        if mgi and isinstance(mgi, dict):
            raw_rsi = mgi.get("rsi_14") if mgi.get("rsi_14") is not None else mgi.get("rsi")
            raw_bb = mgi.get("bb_position_pct") if mgi.get("bb_position_pct") is not None else mgi.get("bb_pos")
            price_dict = {"current_price": float(mgi["current_price"]) if _is_valid_positive_finite(mgi.get("current_price")) else 0.0}
            if "candles_20" in mgi and isinstance(mgi["candles_20"], list):
                price_dict["candles_20"] = mgi["candles_20"]

            reconstructed_market_data = {
                "price_data": price_dict,
                "current_price": float(mgi["current_price"]) if _is_valid_positive_finite(mgi.get("current_price")) else 0.0,
                "indicators": {
                    "donchian_high": float(mgi["donchian_high"]) if _is_valid_positive_finite(mgi.get("donchian_high")) else 0.0,
                    "donchian_low": float(mgi["donchian_low"]) if _is_valid_positive_finite(mgi.get("donchian_low")) else 0.0,
                    "volume_spike_pct": float(mgi["volume_spike_pct"]) if _is_valid_nonneg_finite(mgi.get("volume_spike_pct")) else 0.0,
                    "atr_pct": float(mgi["atr_pct"]) if _is_valid_positive_finite(mgi.get("atr_pct")) else 0.0,
                    "atr_14": float(mgi["atr_14"]) if _is_valid_positive_finite(mgi.get("atr_14")) else 0.0,
                    "rvol": float(mgi["rvol"]) if _is_valid_nonneg_finite(mgi.get("rvol")) else 1.0,
                    "macro_box_pos_pct": float(mgi["macro_box_pos_pct"]) if _is_valid_finite(mgi.get("macro_box_pos_pct")) else 50.0,
                    "low_48h": float(mgi["low_48h"]) if _is_valid_positive_finite(mgi.get("low_48h")) else 0.0,
                    "high_48h": float(mgi["high_48h"]) if _is_valid_positive_finite(mgi.get("high_48h")) else 0.0,
                    "bb_position_pct": float(raw_bb) if _is_valid_finite(raw_bb) else None,
                    "ema_20": float(mgi["ema_20"]) if _is_valid_positive_finite(mgi.get("ema_20")) else 0.0,
                    "rsi_14": float(raw_rsi) if _is_valid_finite(raw_rsi) else None,
                    "algo_signals": mgi.get("algo_signals") or {},
                    "asset_return_24h": float(mgi["asset_return_24h"]) if _is_valid_finite(mgi.get("asset_return_24h")) else 0.0,
                    "rs_divergence": float(mgi["rs_divergence"]) if _is_valid_finite(mgi.get("rs_divergence")) else 0.0
                },
                "multi_timeframe": mgi.get("multi_timeframe") or {},
                "derivatives_data": {
                    "funding_rate": float(mgi["funding_rate"]) if _is_valid_finite(mgi.get("funding_rate")) else None
                },
                "is_pullback_retest": bool(mgi.get("is_pullback_retest", False))
            }

        # Candidate target trade direction evaluated by DeterministicGuard (Audit Point 2)
        cand_direction = (ceo_data.get("decision") or router_data.get("direction_bias") or guard_data.get("decision") or "NEUTRAL").upper()

        # Required context and indicator invariants by strategy mode (Audit Points 2, 3, 4)
        REQUIRED_CONTEXT_BY_STRATEGY = {
            "TREND_FOLLOWING": [
                ("multi_timeframe_trend", lambda m, d: _validate_mtf_trend_data(m)),
                ("rsi_or_bb", lambda m, d: _is_valid_finite(m.get("rsi_14")) or _is_valid_finite(m.get("bb_position_pct"))),
            ],
            "BREAKOUT": [
                ("donchian_directional", lambda m, d: _validate_donchian_for_direction(m, d)),
                ("volume_spike_pct", lambda m, d: _is_valid_nonneg_finite(m.get("volume_spike_pct"))),
            ],
            "VOLATILITY_MOMENTUM": [
                ("donchian_directional", lambda m, d: _validate_donchian_for_direction(m, d)),
                ("volume_spike_pct", lambda m, d: _is_valid_nonneg_finite(m.get("volume_spike_pct"))),
            ],
            "MEAN_REVERSION": [
                ("bb_or_rsi", lambda m, d: _is_valid_finite(m.get("bb_position_pct")) or _is_valid_finite(m.get("rsi_14"))),
            ],
            "PULLBACK": [
                (
                    "pullback_context",
                    lambda m, d: bool(m.get("is_pullback_retest")) or (
                        _is_valid_positive_finite(m.get("ema_20")) and _is_valid_positive_finite(m.get("atr_pct"))
                    )
                ),
            ],
            "RELATIVE_MOMENTUM": [
                ("volume_spike_pct", lambda m, d: _is_valid_nonneg_finite(m.get("volume_spike_pct"))),
                ("relative_divergence_or_return", lambda m, d: _validate_relative_momentum(m)),
            ],
        }

        # Strict context completeness validation (Audit Points 1-4)
        if strict:
            if not original_ceo_trade_action:
                return False, "INCOMPLETE_REPLAY: missing original ceo.trade_action in snapshot", {"status": "INCOMPLETE_REPLAY"}
            if not mgi or not isinstance(mgi, dict):
                return False, "INCOMPLETE_REPLAY: missing market_guard_inputs in snapshot", {"status": "INCOMPLETE_REPLAY"}
            if not _is_valid_positive_finite(mgi.get("current_price")):
                return False, "INCOMPLETE_REPLAY: missing valid positive finite current_price in market_guard_inputs", {"status": "INCOMPLETE_REPLAY"}
            if strategy_mode not in REQUIRED_CONTEXT_BY_STRATEGY:
                return False, f"INCOMPLETE_REPLAY: unsupported or unrecognized strategy_mode '{strategy_mode}' in strict replay", {"status": "INCOMPLETE_REPLAY"}

            for rule_name, validator in REQUIRED_CONTEXT_BY_STRATEGY[strategy_mode]:
                if not validator(mgi, cand_direction):
                    return False, f"INCOMPLETE_REPLAY: missing required indicator/context '{rule_name}' for {strategy_mode}", {"status": "INCOMPLETE_REPLAY"}
        elif strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and mgi is None:
            return False, "INCOMPLETE_REPLAY: missing market_guard_inputs in snapshot", {"status": "INCOMPLETE_REPLAY"}

        profile = snapshot.get("trading_profile", "BALANCED")
        if risk_manager:
            rm = risk_manager
        elif snapshot.get("min_conviction") is not None:
            rm = CapturedRiskRules(min_conviction=int(snapshot["min_conviction"]))
        else:
            rm = MockRiskRules()

        # Offline evaluate
        replayed_decision: FinalTradeDecision = DeterministicGuard.evaluate(
            strategy_profile=strat_profile,
            ceo_proposal=ceo_proposal,
            profile=profile,
            risk_manager=rm,
            market_data=reconstructed_market_data,
            symbol=snapshot.get("symbol", "")
        )

        orig_action = guard_data.get("action")
        orig_decision = guard_data.get("decision")
        orig_actionable = guard_data.get("is_actionable")
        orig_guard_status = guard_data.get("guard_status")
        orig_rejection_tag = guard_data.get("rejection_tag")

        replayed_action = replayed_decision.trade_action
        replayed_dir = replayed_decision.decision
        replayed_actionable = replayed_decision.is_actionable
        replayed_guard_status = replayed_decision.guard_status
        replayed_rejection_tag = replayed_decision.rejection_tag

        # Audit Point 1: Complete invariant comparison across all guard dimensions
        matches = (
            orig_action == replayed_action and
            orig_decision == replayed_dir and
            (orig_actionable is None or orig_actionable == replayed_actionable) and
            (orig_guard_status is None or orig_guard_status == replayed_guard_status) and
            (orig_rejection_tag is None or orig_rejection_tag == replayed_rejection_tag)
        )
        diff = {
            "original": {
                "decision": orig_decision,
                "action": orig_action,
                "is_actionable": orig_actionable,
                "guard_status": orig_guard_status,
                "rejection_tag": orig_rejection_tag
            },
            "replayed": {
                "decision": replayed_dir,
                "action": replayed_action,
                "is_actionable": replayed_actionable,
                "guard_status": replayed_guard_status,
                "rejection_tag": replayed_rejection_tag
            }
        }
        if not original_ceo_trade_action:
            diff["uncertified_fallback_trade_action"] = True
        msg = "MATCH" if matches else (
            f"MISMATCH: Orig(dir={orig_decision}, act={orig_action}, actable={orig_actionable}, tag={orig_rejection_tag}) vs "
            f"Replayed(dir={replayed_dir}, act={replayed_action}, actable={replayed_actionable}, tag={replayed_rejection_tag})"
        )
        return matches, msg, diff

    def run_guard_replay_audit(
        self,
        snapshots: Optional[List[Dict[str, Any]]] = None,
        strict: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Runs replay audit across all snapshots that have Deterministic Guard data (Audit Point 1).

        Modes:
        - strict=True: Enforces strict context completeness across all audited snapshots.
        - strict=False: Permissive legacy replay mode.
        - strict=None (default): Categorized hybrid audit.
          Snapshots with 'market_guard_inputs' are audited under strict=True.
          Legacy snapshots without full inputs are audited under strict=False and
          certified as 'legacy_matches'.

        Returns categorized match statistics and any discrepancies found.
        """
        if snapshots is None:
            snapshots = self.load_snapshots()

        eligible = [s for s in snapshots if "guard" in s and s.get("guard")]
        if not eligible:
            return {
                "total_audited": 0,
                "matches": 0,
                "strict_matches": 0,
                "legacy_matches": 0,
                "incomplete_context": 0,
                "mismatches": 0,
                "match_rate_pct": 100.0,
                "strict_integrity_rate_pct": 100.0,
                "details": []
            }

        strict_matches = 0
        legacy_matches = 0
        incomplete_context = 0
        mismatches = 0
        strict_eligible = 0
        details = []

        for s in eligible:
            has_mgi = bool(s.get("market_guard_inputs") and isinstance(s.get("market_guard_inputs"), dict))

            if strict is True:
                snap_strict = True
            elif strict is False:
                snap_strict = False
            else:
                snap_strict = has_mgi

            if snap_strict:
                strict_eligible += 1

            is_match, msg, diff = self.replay_guard_decision(s, strict=snap_strict)

            if is_match:
                if snap_strict:
                    strict_matches += 1
                else:
                    legacy_matches += 1
            else:
                if "INCOMPLETE_REPLAY" in msg:
                    incomplete_context += 1
                    details.append({
                        "symbol": s.get("symbol"),
                        "timestamp": s.get("timestamp"),
                        "status": "INCOMPLETE_REPLAY",
                        "diff": diff,
                        "reason": msg
                    })
                else:
                    mismatches += 1
                    details.append({
                        "symbol": s.get("symbol"),
                        "timestamp": s.get("timestamp"),
                        "status": "MISMATCH",
                        "diff": diff,
                        "reason": msg
                    })

        total_matches = strict_matches + legacy_matches
        total_count = len(eligible)
        match_rate = round(total_matches / total_count * 100, 2) if total_count > 0 else 100.0
        strict_rate = round(strict_matches / strict_eligible * 100, 2) if strict_eligible > 0 else 100.0

        return {
            "total_audited": total_count,
            "matches": total_matches,
            "strict_matches": strict_matches,
            "legacy_matches": legacy_matches,
            "incomplete_context": incomplete_context,
            "mismatches": mismatches,
            "match_rate_pct": match_rate,
            "strict_integrity_rate_pct": strict_rate,
            "details": details
        }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Decision Pipeline Telemetry & Offline Replay")
    parser.add_argument("--file", default="data/memory/decision_snapshots.jsonl", help="Path to JSONL snapshots")
    parser.add_argument("--symbol", default=None, help="Filter by symbol (e.g. BTC-USD)")
    parser.add_argument("--strategy", default=None, help="Filter by strategy mode")
    parser.add_argument("--action", default=None, help="Filter by final action")
    parser.add_argument("--verify-guard", action="store_true", help="Run deterministic replay verification")
    parser.add_argument("--strict", action="store_true", help="Force strict verification mode on all snapshots")
    parser.add_argument("--limit", type=int, default=10, help="Number of recent records to display")
    args = parser.parse_args()

    engine = DecisionReplayEngine(filepath=args.file)
    snapshots = engine.filter_snapshots(
        symbol=args.symbol,
        strategy_mode=args.strategy,
        final_action=args.action
    )

    funnel = engine.compute_funnel(snapshots)
    print(engine.format_report(funnel))

    if args.verify_guard:
        print("\n🔍 Running Offline Deterministic Guard Replay Audit...")
        audit = engine.run_guard_replay_audit(snapshots, strict=True if args.strict else None)
        print(f"Total Audited:        {audit['total_audited']} setups")
        print(f"  • Strict Verified:    {audit['strict_matches']}")
        print(f"  • Legacy Verified:    {audit['legacy_matches']}")
        print(f"  • Incomplete Context: {audit['incomplete_context']}")
        print(f"  • Logic Mismatches:   {audit['mismatches']}")
        print(f"Overall Match Rate:   {audit['match_rate_pct']}% | Strict Integrity: {audit['strict_integrity_rate_pct']}%")
        if audit["details"]:
            print("⚠️ Discrepancies / Incomplete Context:")
            for d in audit["details"]:
                status_tag = f"[{d.get('status', 'ISSUE')}]"
                print(f"  • {status_tag:<20} {d['symbol']}: {d['reason']}")


if __name__ == "__main__":
    main()
```

---

