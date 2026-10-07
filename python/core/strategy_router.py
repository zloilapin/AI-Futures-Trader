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
        mtf_data = market_data.get("multi_timeframe", {})
        mtf_alignment = mtf_data.get("mtf_alignment")
        trend_1h = mtf_data.get("trend_1h", "")
        indicators = market_data.get("indicators", {})
        bb_position_pct = indicators.get("bb_position_pct", 50.0)
        volume_spike_pct = indicators.get("volume_spike_pct", 0.0)
        donchian_high = indicators.get("donchian_high", 0.0)
        donchian_low = indicators.get("donchian_low", 0.0)
        current_price = float(market_data.get("price_data", {}).get("current_price", 0.0))
        bb_width_pct = indicators.get("bb_width_pct", 0.0)
        
        # Tech consensus (excluding News)
        tech_bulls = sum(1 for r in valid_reports if r.get("agent_name") != "News_Agent" and str(r.get("signal", "")).upper() in ["BULLISH", "LONG"])
        tech_bears = sum(1 for r in valid_reports if r.get("agent_name") != "News_Agent" and str(r.get("signal", "")).upper() in ["BEARISH", "SHORT"])
        ob_bull = any(r.get("agent_name") == "Order_Book_Agent" and str(r.get("signal", "")).upper() in ["BULLISH", "LONG"] for r in valid_reports)
        ob_bear = any(r.get("agent_name") == "Order_Book_Agent" and str(r.get("signal", "")).upper() in ["BEARISH", "SHORT"] for r in valid_reports)

        last_closed_candle_close = indicators.get("last_closed_candle_close", current_price)

        # 1. Breakout Strategy (Highest Priority Momentum - evaluated first so it is not intercepted by local scalping)
        # We require the last CLOSED candle to pierce the channel to avoid fakeouts on active wicks
        if volume_spike_pct >= 200.0 and last_closed_candle_close > donchian_high and tech_bulls >= 1 and ob_bull:
            return StrategyProfile(True, "BREAKOUT", "LONG", f"BREAKOUT LONG: Пробой {donchian_high} (close={last_closed_candle_close}) с объемом {volume_spike_pct}%, OB=BULL")
        if volume_spike_pct >= 200.0 and last_closed_candle_close < donchian_low and tech_bears >= 1 and ob_bear:
            return StrategyProfile(True, "BREAKOUT", "SHORT", f"BREAKOUT SHORT: Пробой {donchian_low} (close={last_closed_candle_close}) с объемом {volume_spike_pct}%, OB=BEAR")

        # 2. Relative Momentum
        asset_return_24h = indicators.get("asset_return_24h", 0.0)
        btc_return_24h = macro_cache.get("BTC-USD", {}).get("indicators", {}).get("asset_return_24h", 0.0) if "BTC-USD" in macro_cache else 0.0
        rs_divergence = round(asset_return_24h - btc_return_24h, 2)
        btc_trend_1h = macro_cache.get("BTC-USD", {}).get("multi_timeframe", {}).get("trend_1h", "NEUTRAL") if "BTC-USD" in macro_cache else "NEUTRAL"
        oi_trend = market_data.get("derivatives_data", {}).get("open_interest_trend", "neutral")

        if symbol != "BTC-USD":
            if btc_trend_1h == "BULLISH" and rs_divergence <= -5.0 and tech_bulls >= 2 and volume_spike_pct >= 50.0 and oi_trend in ["rising", "stable"]:
                return StrategyProfile(True, "RELATIVE_MOMENTUM", "LONG", f"RELATIVE_MOMENTUM LONG: Отставание от BTC {rs_divergence}%, Bulls={tech_bulls}, Vol={volume_spike_pct}%, OI={oi_trend}")
            if btc_trend_1h in ["BEARISH", "NEUTRAL"] and rs_divergence >= 10.0 and tech_bears >= 2 and volume_spike_pct >= 50.0 and oi_trend in ["rising", "stable", "falling"]:
                return StrategyProfile(True, "RELATIVE_MOMENTUM", "SHORT", f"RELATIVE_MOMENTUM SHORT: Аномальный памп {rs_divergence}%, Bears={tech_bears}, Vol={volume_spike_pct}%, OI={oi_trend}")

        # 3. Volatility Momentum (Scalping)
        if mtf_alignment in ["MIXED_CHOP", "COUNTER_TREND_WARNING"] and bb_width_pct > 10.0:
            if ob_bull and tech_bulls >= 2 and volume_spike_pct >= 100.0 and oi_trend == "rising":
                return StrategyProfile(True, "VOLATILITY_MOMENTUM", "LONG", f"VOLATILITY_MOMENTUM LONG: Волатильность {bb_width_pct}%, Vol={volume_spike_pct}%, OI={oi_trend}")
            if ob_bear and tech_bears >= 2 and volume_spike_pct >= 100.0 and oi_trend == "rising":
                return StrategyProfile(True, "VOLATILITY_MOMENTUM", "SHORT", f"VOLATILITY_MOMENTUM SHORT: Волатильность {bb_width_pct}%, Vol={volume_spike_pct}%, OI={oi_trend}")

        # 4. Macro Regimes Logic
        if mtf_alignment == "COUNTER_TREND_WARNING":
            if trend_1h == "BULLISH" and tech_bulls >= 2:
                return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Отскок по тренду: Bulls={tech_bulls}, 1H={trend_1h}")
            if trend_1h == "BEARISH" and tech_bears >= 2:
                return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Откат по тренду: Bears={tech_bears}, 1H={trend_1h}")
            return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", "ОТКЛОНЕН (Попытка торговли против макро-тренда).")

        if mtf_alignment == "TRANSITION":
            if trend_1h == "BULLISH" and tech_bulls >= 1 and tech_bears == 0:
                return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Ранний разворот в лонг: 15m/1H Bullish, Bulls={tech_bulls}")
            if trend_1h == "BEARISH" and tech_bears >= 1 and tech_bulls == 0:
                return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Ранний разворот в шорт: 15m/1H Bearish, Bears={tech_bears}")
            return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", "ОТКЛОНЕН (TRANSITION, но нет чистого консенсуса).")

        if mtf_alignment == "MIXED_CHOP":
            # Safety Guard: In high volatility macro regime, mean reversion is dangerous (risk of breakout/liquidation cascade)
            if detected_regime == "HIGH_VOLATILITY":
                return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MEAN_REVERSION заблокирован в макро-режиме HIGH_VOLATILITY, bb_pos={bb_position_pct}%).")

            if bb_position_pct <= 5.0 and tech_bulls >= 1:
                return StrategyProfile(True, "MEAN_REVERSION", "LONG", f"MEAN_REVERSION LONG: от нижней границы Bollinger, bb_pos={bb_position_pct}%")
            if bb_position_pct >= 95.0 and tech_bears >= 1:
                return StrategyProfile(True, "MEAN_REVERSION", "SHORT", f"MEAN_REVERSION SHORT: от верхней границы Bollinger, bb_pos={bb_position_pct}%")
            return StrategyProfile(False, "MEAN_REVERSION", "NEUTRAL", f"ОТКЛОНЕН (MIXED_CHOP, цена внутри канала, bb_pos={bb_position_pct}%).")

        if mtf_alignment == "FULL_ALIGNMENT":
            if tech_bulls > tech_bears:
                return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"FULL_ALIGNMENT MTF trend + подтверждение быков ({tech_bulls} vs {tech_bears}).")
            elif tech_bears > tech_bulls:
                return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"FULL_ALIGNMENT MTF trend + подтверждение медведей ({tech_bears} vs {tech_bulls}).")
            elif tech_bulls == 0 and tech_bears == 0:
                # All 3 timeframes fully aligned with 0 analyst opposition
                if trend_1h == "BULLISH":
                    return StrategyProfile(True, "TREND_FOLLOWING", "LONG", "FULL_ALIGNMENT MTF trend (15m/1h/4h Bullish), нет сопротивления аналитиков.")
                elif trend_1h == "BEARISH":
                    return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "FULL_ALIGNMENT MTF trend (15m/1h/4h Bearish), нет сопротивления аналитиков.")

        # Fallback to pure consensus
        if tech_bulls >= 2 and tech_bears <= 1:
            return StrategyProfile(True, "TREND_FOLLOWING", "LONG", f"Bullish консенсус {tech_bulls} vs {tech_bears}")
        if tech_bears >= 2 and tech_bulls <= 1:
            return StrategyProfile(True, "TREND_FOLLOWING", "SHORT", f"Bearish консенсус {tech_bears} vs {tech_bulls}")

        return StrategyProfile(False, "TREND_FOLLOWING", "NEUTRAL", f"Боковик/нет консенсуса (bulls={tech_bulls}, bears={tech_bears}, MTF={mtf_alignment}).")
