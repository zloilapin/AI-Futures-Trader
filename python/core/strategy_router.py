from typing import Dict, Any, List
from dataclasses import dataclass

@dataclass
class StrategyProfile:
    has_directional_signal: bool
    strategy_mode: str
    direction_bias: str # "LONG", "SHORT", "NEUTRAL"
    reasoning: str

class StrategyRouter:
    """
    Evaluates raw local market conditions and agent reports to determine
    the mathematically optimal execution strategy (if any) before passing to CEO.
    """
    
    @staticmethod
    def evaluate(
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

        mtf_data = market_data.get("multi_timeframe") or {}
        mtf_alignment = mtf_data.get("mtf_alignment")
        trend_1h = str(mtf_data.get("trend_1h") or "").upper()
        indicators = market_data.get("indicators") or {}
        
        bb_position_pct = _safe_float(indicators.get("bb_position_pct", indicators.get("bb_pos")), 50.0)
        volume_spike_pct = _safe_float(indicators.get("volume_spike_pct"), 0.0)
        donchian_high = _safe_float(indicators.get("donchian_high"), 0.0)
        donchian_low = _safe_float(indicators.get("donchian_low"), 0.0)
        current_price = _safe_float((market_data.get("price_data") or {}).get("current_price"), 0.0)
        bb_width_pct = _safe_float(indicators.get("bb_width_pct"), 0.0)
        atr_pct = _safe_float(indicators.get("atr_pct"), 0.0)
        last_closed_candle_close = _safe_float(indicators.get("last_closed_candle_close"), current_price)

        # Tech consensus (excluding News)
        tech_bulls = sum(1 for r in valid_reports if r.get("agent_name") != "News_Agent" and str(r.get("signal", "")).upper() in ["BULLISH", "LONG"])
        tech_bears = sum(1 for r in valid_reports if r.get("agent_name") != "News_Agent" and str(r.get("signal", "")).upper() in ["BEARISH", "SHORT"])
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

        # 1. Breakout Strategy (Highest Priority Momentum)
        # Requires strictly positive Donchian channels (> 0) to prevent phantom breakouts on corrupt/zero data
        if donchian_high > 0 and (current_price > donchian_high or last_closed_candle_close > donchian_high) and tech_bulls >= 1 and (
            (volume_spike_pct >= 200.0 and ob_bull) or (volume_spike_pct >= 120.0 and trend_1h == "BULLISH")
        ):
            return StrategyProfile(True, "BREAKOUT", "LONG", f"BREAKOUT LONG: Пробой {donchian_high} (close={last_closed_candle_close}, price={current_price}) с объемом {volume_spike_pct}%, OB={'BULL' if ob_bull else 'NEUTRAL'}")
        elif donchian_low > 0 and (current_price < donchian_low or last_closed_candle_close < donchian_low) and tech_bears >= 1 and (
            (volume_spike_pct >= 200.0 and ob_bear) or (volume_spike_pct >= 100.0 and trend_1h == "BEARISH")
        ):
            return StrategyProfile(True, "BREAKOUT", "SHORT", f"BREAKOUT SHORT: Пробой {donchian_low} (close={last_closed_candle_close}, price={current_price}) с объемом {volume_spike_pct}%, OB={'BEAR' if ob_bear else 'NEUTRAL'}")

        # 2. Relative Momentum (Requires valid BTC benchmark in macro_cache)
        oi_trend = str((market_data.get("derivatives_data") or {}).get("open_interest_trend") or "neutral").lower()

        if symbol != "BTC-USD" and isinstance(macro_cache, dict) and "BTC-USD" in macro_cache:
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
                    return StrategyProfile(True, "RELATIVE_MOMENTUM", "LONG", f"RELATIVE_MOMENTUM LONG: Лидер рынка (+{rs_divergence}% vs BTC), Bulls={tech_bulls}, Vol={volume_spike_pct}%, OI={oi_trend}")
                # 2b. Relative Momentum Catch-Up (Laggard)
                elif btc_trend_1h == "BULLISH" and rs_divergence <= -5.0 and tech_bulls >= 2 and volume_spike_pct >= 50.0 and oi_trend in ["rising", "stable"]:
                    return StrategyProfile(True, "RELATIVE_MOMENTUM", "LONG", f"RELATIVE_MOMENTUM LONG: Отставание от BTC {rs_divergence}%, Bulls={tech_bulls}, Vol={volume_spike_pct}%, OI={oi_trend}")
                # 2c. Overextended Exhausted Pump (Fade Short)
                elif btc_trend_1h in ["BEARISH", "NEUTRAL"] and rs_divergence >= 10.0 and tech_bears >= 2 and volume_spike_pct >= 50.0 and oi_trend in ["rising", "stable", "falling"]:
                    return StrategyProfile(True, "RELATIVE_MOMENTUM", "SHORT", f"RELATIVE_MOMENTUM SHORT: Аномальный памп {rs_divergence}%, Bears={tech_bears}, Vol={volume_spike_pct}%, OI={oi_trend}")

        # 3. Volatility Momentum (Scalping)
        # 15m BB Width expansion: normal quiet chop is 0.2-0.5%, active volatility expansion is >= 0.8% (or ATR >= 0.25%)
        if mtf_alignment in ["MIXED_CHOP", "COUNTER_TREND_WARNING"] and (bb_width_pct >= 0.8 or atr_pct >= 0.25):
            # Guard against deadlock if both tech_bulls and tech_bears are high
            if not (tech_bulls >= 2 and tech_bears >= 2):
                if ob_bull and tech_bulls >= 2 and volume_spike_pct >= 100.0 and oi_trend in ["rising", "stable"]:
                    return StrategyProfile(True, "VOLATILITY_MOMENTUM", "LONG", f"VOLATILITY_MOMENTUM LONG: Волатильность {bb_width_pct}% (ATR={atr_pct}%), Vol={volume_spike_pct}%, OI={oi_trend}")
                elif ob_bear and tech_bears >= 2 and volume_spike_pct >= 100.0 and oi_trend in ["rising", "stable"]:
                    return StrategyProfile(True, "VOLATILITY_MOMENTUM", "SHORT", f"VOLATILITY_MOMENTUM SHORT: Волатильность {bb_width_pct}% (ATR={atr_pct}%), Vol={volume_spike_pct}%, OI={oi_trend}")

        # 4. Macro Regimes Logic
        if mtf_alignment == "COUNTER_TREND_WARNING":
            if trend_1h == "BULLISH" and tech_bulls >= 2:
                return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Отскок по тренду: Bulls={tech_bulls}, 1H={trend_1h}")
            elif trend_1h == "BEARISH" and (tech_bears >= 2 or (tech_bears >= 1 and candle_signal in ["BEARISH", "SHORT"] and volume_spike_pct >= 50.0)):
                return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Импульс/откат по тренду: Bears={tech_bears}, 1H={trend_1h}, Vol={volume_spike_pct}%")
            return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", "ОТКЛОНЕН (Попытка торговли против макро-тренда).")

        if mtf_alignment == "TRANSITION":
            if trend_1h == "BULLISH" and tech_bulls >= 1 and tech_bears == 0:
                return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Ранний разворот в лонг: 15m/1H Bullish, Bulls={tech_bulls}")
            elif trend_1h == "BEARISH" and tech_bears >= 1 and tech_bulls == 0:
                return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Ранний разворот в шорт: 15m/1H Bearish, Bears={tech_bears}")
            return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", "ОТКЛОНЕН (TRANSITION, но нет чистого консенсуса).")

        if mtf_alignment == "MIXED_CHOP":
            # Safety Guard: In high volatility or unknown/degraded macro regimes, mean reversion is dangerous
            if detected_regime in ["HIGH_VOLATILITY", "UNKNOWN", "DATA_INSUFFICIENT"]:
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION заблокирован в макро-режиме {detected_regime}, bb_pos={bb_position_pct}%).")

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
                if detected_regime == "TRENDING_BEAR":
                    return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION LONG заблокирован в макро-режиме TRENDING_BEAR: риск падения без отскока).")
                if tech_bulls >= 1:
                    return StrategyProfile(True, "MEAN_REVERSION", "LONG", f"MEAN_REVERSION LONG: от нижней границы Bollinger, bb_pos={bb_position_pct:.2f}%, width={bb_width_pct:.2f}%")
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (Цена у нижней границы {bb_position_pct:.2f}%, но нет бычьих сигналов: Bulls={tech_bulls}).")

            if bb_position_pct >= 88.0:
                if detected_regime == "TRENDING_BULL":
                    return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION SHORT заблокирован в макро-режиме TRENDING_BULL: риск пробоя вверх).")
                if tech_bears >= 1:
                    return StrategyProfile(True, "MEAN_REVERSION", "SHORT", f"MEAN_REVERSION SHORT: от верхней границы Bollinger, bb_pos={bb_position_pct:.2f}%, width={bb_width_pct:.2f}%")
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (Цена у верхней границы {bb_position_pct:.2f}%, но нет медвежьих сигналов: Bears={tech_bears}).")

            return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MIXED_CHOP, цена внутри канала: bb_pos={bb_position_pct:.2f}%).")

        if mtf_alignment == "FULL_ALIGNMENT":
            # In FULL_ALIGNMENT (all 3 timeframes aligned in trend_1h):
            # Direction MUST strictly match trend_1h! Rejecting counter-trend analyst disagreements.
            if trend_1h == "BULLISH":
                if tech_bulls >= tech_bears:
                    if tech_bulls == 0 and tech_bears == 0:
                        return StrategyProfile(True, "TREND_FOLLOWING", "LONG", "FULL_ALIGNMENT MTF trend (15m/1h/4h Bullish), нет сопротивления аналитиков.")
                    return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"FULL_ALIGNMENT MTF trend (Bullish) + подтверждение быков ({tech_bulls} vs {tech_bears}).")
                else:
                    return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (FULL_ALIGNMENT Bullish тренд, но аналитики в медвежьей оппозиции: {tech_bears} vs {tech_bulls}).")
            elif trend_1h == "BEARISH":
                if tech_bears >= tech_bulls:
                    if tech_bulls == 0 and tech_bears == 0:
                        return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "FULL_ALIGNMENT MTF trend (15m/1h/4h Bearish), нет сопротивления аналитиков.")
                    return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"FULL_ALIGNMENT MTF trend (Bearish) + подтверждение медведей ({tech_bears} vs {tech_bulls}).")
                else:
                    return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"ОТКЛОНЕН (FULL_ALIGNMENT Bearish тренд, но аналитики в бычьей оппозиции: {tech_bulls} vs {tech_bears}).")

        # Fallback to pure consensus (only if MTF alignment is not in explicit conflict)
        if tech_bulls >= 2 and tech_bears <= 1:
            return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Bullish консенсус {tech_bulls} vs {tech_bears}")
        elif tech_bears >= 2 and tech_bulls <= 1:
            return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Bearish консенсус {tech_bears} vs {tech_bulls}")

        return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"Боковик/нет консенсуса (bulls={tech_bulls}, bears={tech_bears}, MTF={mtf_alignment}).")
