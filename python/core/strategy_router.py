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
        max_age_seconds: float = 14400.0,
        journal_path: str = "data/memory/trade_journal.json"
    ):
        """
        Anti-Chase Re-Entry Guard (Same-Direction Re-Entry Cooldown):
        Prevents serial re-entering at the exact bottom/top of an exhausted momentum impulse.
        If the latest closed trade for `symbol` in trade_journal was in the same direction
        and profitable within max_age_seconds (default 240 minutes / 4 hours):
          - For SHORT: forbidden if current_price < (last_exit_price + 0.5 * ATR) OR bb_pos < 45.0%
          - For LONG: forbidden if current_price > (last_exit_price - 0.5 * ATR) OR bb_pos > 55.0%
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

        net_pnl = float(last_trade.get("net_pnl_usd", 0.0) or 0.0)
        realized_r = float(last_trade.get("realized_r", 0.0) or 0.0)
        is_profitable = (net_pnl > 0.0) or (realized_r > 0.0)

        if not is_profitable:
            return True, f"Last trade for {symbol} was not profitable (PnL={net_pnl:.2f}, R={realized_r:.2f})"

        import time
        now_ts = time.time()
        trade_ts = float(last_trade.get("timestamp", 0.0) or 0.0)
        age_sec = now_ts - trade_ts
        if trade_ts > 0 and age_sec > max_age_seconds:
            return True, f"Cooldown expired ({age_sec / 60.0:.1f}m > {max_age_seconds / 60.0:.1f}m)"

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
                age_min = (age_sec / 60.0) if trade_ts > 0 else 0.0
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
                age_min = (age_sec / 60.0) if trade_ts > 0 else 0.0
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
            can_reenter, anti_chase_reason = StrategyRouter.check_anti_chase_reentry(
                symbol=symbol,
                target_direction=profile_res.direction_bias,
                current_price=current_price,
                eval_atr=eval_atr,
                bb_position_pct=bb_pos,
                max_age_seconds=14400.0
            )
            if not can_reenter:
                return StrategyProfile(
                    False,
                    "ANTI_CHASE_REENTRY_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН ({anti_chase_reason})"
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
                        "NEUTRAL",
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

            # Anti-Knife Catching: Reject retest ONLY if analysts violently oppose (>= 3 opposing)
            # Corrective 15m candle color is expected on retest and does NOT cancel the trade
            if retest_dir == "LONG" and tech_bears >= 3:
                return StrategyProfile(
                    False,
                    "FALLING_KNIFE_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Ловля падающих ножей: ретест LONG отклонен из-за сильного медвежьего давления аналитиков: Bears={tech_bears})."
                )
            elif retest_dir == "SHORT" and tech_bulls >= 3:
                return StrategyProfile(
                    False,
                    "ROCKET_RALLY_VETO",
                    "NEUTRAL",
                    f"ОТКЛОНЕН (Ловля ракет: ретест SHORT отклонен из-за сильного бычьего давления аналитиков: Bulls={tech_bulls})."
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
