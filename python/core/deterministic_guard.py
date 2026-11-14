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

        # Re-anchor SL and TP relative to live_price:
        # Both SL and TP shift by price_delta so the risk distance abs(new_entry - new_sl)
        # remains identical to the approved risk distance, preserving R:R and approved risk budget.
        price_delta = live_price - snapshot_price
        new_entry = live_price
        new_tp = round(tp_price + price_delta, 6)
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
    def evaluate_analyst_quorum(
        direction: str,
        analyst_reports: Optional[Any],
        entry_trigger_type: str = "REGULAR_SCAN",
        market_data: Optional[Dict[str, Any]] = None,
        watchlist_metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Deterministic ANALYST_QUORUM_GATE (Systemic Module 01 Upgrade):
        Multi-Timeframe Aware Quorum & Counter-Momentum Divergence Logic.

        For REGULAR_SCAN / IMPULSE_BREAKOUT:
          - Requires LTF + HTF momentum alignment.
          - Vetoes if BOTH Candle_Agent and Indicator_Agent oppose with >= 60% conviction.
          - Standard Quorum: >= 2 technical agents matching >= 60% OR >= 1 matching >= 75% with 0 opposing.

        For WATCHLIST_RETEST:
          - Structural Validity: Validates price has not breached invalidation_price, HTF structure intact.
          - LTF Retracement Tolerance: Retracement momentum is expected; only vetoes on FULL structural breakout
            (Candle >= 72% AND Indicator >= 72% AND OrderBook >= 65% opposing).
          - Retest Quorum: Requires at least 1 technical/microstructure agent confirming absorption/rejection (>= 60%).
        """
        dir_clean = str(direction or "").upper().strip()
        if dir_clean not in ("LONG", "SHORT"):
            return True, None, None

        if not analyst_reports:
            return True, None, None

        reports_map = {}
        if isinstance(analyst_reports, dict):
            for k, v in analyst_reports.items():
                name_key = str((v.get("agent_name") or k) if isinstance(v, dict) else (getattr(v, "agent_name", None) or k)).strip().lower().replace(' ', '_')
                reports_map[name_key] = v
        elif isinstance(analyst_reports, list):
            for idx, r in enumerate(analyst_reports):
                if isinstance(r, dict):
                    name_key = str(r.get("agent_name") or r.get("name") or f"agent_{idx}").strip().lower().replace(' ', '_')
                    reports_map[name_key] = r
                elif hasattr(r, "__dict__") or hasattr(r, "signal"):
                    name_key = str(getattr(r, "agent_name", None) or getattr(r, "name", None) or f"agent_{idx}").strip().lower().replace(' ', '_')
                    reports_map[name_key] = r

        def get_agent_data(alias_list, fallback_idx=None):
            for a in alias_list:
                for k, v in reports_map.items():
                    if a in k:
                        if isinstance(v, dict):
                            sig = str(v.get("signal") or v.get("direction") or "NEUTRAL").upper().strip()
                            try:
                                conf = float(v.get("confidence") or 0.0)
                            except (ValueError, TypeError):
                                conf = 0.0
                        else:
                            sig = str(getattr(v, "signal", None) or getattr(v, "direction", None) or "NEUTRAL").upper().strip()
                            try:
                                conf = float(getattr(v, "confidence", 0.0) or 0.0)
                            except (ValueError, TypeError):
                                conf = 0.0
                        return sig, conf
            if fallback_idx is not None and isinstance(analyst_reports, list) and len(analyst_reports) > fallback_idx:
                r = analyst_reports[fallback_idx]
                if isinstance(r, dict):
                    sig = str(r.get("signal") or r.get("direction") or "NEUTRAL").upper().strip()
                    try:
                        conf = float(r.get("confidence") or 0.0)
                    except (ValueError, TypeError):
                        conf = 0.0
                    return sig, conf
                elif hasattr(r, "__dict__") or hasattr(r, "signal"):
                    sig = str(getattr(r, "signal", None) or getattr(r, "direction", None) or "NEUTRAL").upper().strip()
                    try:
                        conf = float(getattr(r, "confidence", 0.0) or 0.0)
                    except (ValueError, TypeError):
                        conf = 0.0
                    return sig, conf
            return "NEUTRAL", 0.0

        candle_sig, candle_conf = get_agent_data(["candle"], fallback_idx=0)
        ob_sig, ob_conf = get_agent_data(["order_book", "orderbook", "depth"], fallback_idx=1)
        oi_sig, oi_conf = get_agent_data(["oi_funding", "oi", "funding"], fallback_idx=2)
        ind_sig, ind_conf = get_agent_data(["indicator"], fallback_idx=4 if (isinstance(analyst_reports, list) and len(analyst_reports) >= 5) else 3)

        tech_agents_data = [
            ("Candle_Agent", candle_sig, candle_conf),
            ("Order_Book_Agent", ob_sig, ob_conf),
            ("OI_Funding_Agent", oi_sig, oi_conf),
            ("Indicator_Agent", ind_sig, ind_conf)
        ]

        target_matching = "BULLISH" if dir_clean == "LONG" else "BEARISH"
        target_opposing = "BEARISH" if dir_clean == "LONG" else "BULLISH"

        trigger_type_str = str(entry_trigger_type or "REGULAR_SCAN").upper().strip()
        is_watchlist_retest = (trigger_type_str == "WATCHLIST_RETEST")

        # ══════════════════════════════════════════════════════════════════════════
        # BRANCH A: WATCHLIST_RETEST (Mean-Reversion into HTF Trend)
        # ══════════════════════════════════════════════════════════════════════════
        if is_watchlist_retest:
            # 1. Structural Validity Check
            cur_px = 0.0
            if market_data:
                cur_px = float((market_data.get("price_data") or {}).get("current_price") or (market_data.get("indicators") or {}).get("current_price") or 0.0)

            inval_px = 0.0
            if watchlist_metadata:
                inval_px = float(watchlist_metadata.get("invalidation_price") or 0.0)
            elif market_data:
                inval_px = float(market_data.get("invalidation_price") or 0.0)

            if cur_px > 0 and inval_px > 0:
                if dir_clean == "LONG" and cur_px < inval_px:
                    msg = f"WATCHLIST_RETEST_INVALIDATION_VETO: Price ({cur_px:.4f}) breached structural invalidation level ({inval_px:.4f}) for LONG retest."
                    return False, "WATCHLIST_RETEST_INVALIDATION_VETO", msg
                elif dir_clean == "SHORT" and cur_px > inval_px:
                    msg = f"WATCHLIST_RETEST_INVALIDATION_VETO: Price ({cur_px:.4f}) breached structural invalidation level ({inval_px:.4f}) for SHORT retest."
                    return False, "WATCHLIST_RETEST_INVALIDATION_VETO", msg

            # Verify HTF structure remains aligned
            htf_dir = ""
            if watchlist_metadata:
                htf_dir = str(watchlist_metadata.get("htf_direction") or "").upper().strip()
            if not htf_dir and market_data:
                htf_dir = str(market_data.get("htf_direction") or market_data.get("macro_trend") or "").upper().strip()
            if htf_dir in ("LONG", "SHORT", "UPTREND", "DOWNTREND", "BULLISH", "BEARISH"):
                htf_norm = "LONG" if htf_dir in ("LONG", "UPTREND", "BULLISH") else "SHORT"
                if htf_norm != dir_clean:
                    msg = f"WATCHLIST_RETEST_INVALIDATION_VETO: Higher timeframe structure ({htf_dir}) opposes retest direction {dir_clean}."
                    return False, "WATCHLIST_RETEST_INVALIDATION_VETO", msg

            # 2. LTF Retracement Tolerance:
            # Retracement momentum is natural during pullback. Only veto if FULL structural breakout occurs:
            # Candle >= 72% AND Indicator >= 72% AND OrderBook >= 65% opposing.
            candle_breakout = (candle_sig == target_opposing and candle_conf >= 72.0)
            indicator_breakout = (ind_sig == target_opposing and ind_conf >= 72.0)
            ob_breakout = (ob_sig == target_opposing and ob_conf >= 65.0)
            if candle_breakout and indicator_breakout and ob_breakout:
                msg = (
                    f"COUNTER_MOMENTUM_DIVERGENCE_VETO: Full structural breakout against retest detected "
                    f"(Candle {candle_conf:.0f}%, Indicator {ind_conf:.0f}%, OrderBook {ob_conf:.0f}% opposing {dir_clean})."
                )
                return False, "COUNTER_MOMENTUM_DIVERGENCE_VETO", msg

            # 3. Retest Quorum Requirement:
            # Require at least 1 technical or microstructure agent confirming absorption/rejection in trade direction (>= 60%).
            retest_confirmed = any(
                sig == target_matching and conf >= 60.0
                for _, sig, conf in tech_agents_data
            )
            if not retest_confirmed:
                msg = (
                    f"QUORUM_INSUFFICIENT_VETO: Retest quorum not met for {dir_clean}. "
                    f"Requires at least 1 technical or microstructure agent confirming absorption/rejection with >= 60% conviction."
                )
                return False, "QUORUM_INSUFFICIENT_VETO", msg

            return True, None, "Watchlist retest quorum confirmed."

        # ══════════════════════════════════════════════════════════════════════════
        # BRANCH B: REGULAR_SCAN / IMPULSE_BREAKOUT (Momentum Continuation)
        # ══════════════════════════════════════════════════════════════════════════
        # 1. Counter-Momentum Divergence Veto:
        # If BOTH Candle_Agent and Indicator_Agent explicitly oppose CEO direction with conviction > 50%
        candle_opposes = (candle_sig == target_opposing and candle_conf > 50.0)
        indicator_opposes = (ind_sig == target_opposing and ind_conf > 50.0)
        if candle_opposes and indicator_opposes:
            msg = (
                f"COUNTER_MOMENTUM_DIVERGENCE_VETO: Both Candle_Agent ({candle_sig} {candle_conf:.0f}%) "
                f"and Indicator_Agent ({ind_sig} {ind_conf:.0f}%) explicitly oppose proposed {dir_clean} entry."
            )
            return False, "COUNTER_MOMENTUM_DIVERGENCE_VETO", msg

        # 2. Minimum Technical Quorum:
        matching_ge_60 = 0
        matching_ge_75 = 0
        opposing_count = 0
        all_neutral = True

        for name, sig, conf in tech_agents_data:
            if sig == target_matching:
                all_neutral = False
                if conf >= 60.0:
                    matching_ge_60 += 1
                if conf >= 75.0:
                    matching_ge_75 += 1
            elif sig == target_opposing:
                all_neutral = False
                if conf > 50.0:
                    opposing_count += 1
            elif sig == "NEUTRAL" and conf > 50.0:
                all_neutral = False

        has_quorum = (matching_ge_60 >= 2) or (matching_ge_75 >= 1 and opposing_count == 0)
        if not has_quorum:
            if all_neutral:
                msg = f"QUORUM_INSUFFICIENT_VETO: All 4 technical sub-agents are NEUTRAL for proposed {dir_clean}. Apathetic entries strictly prohibited."
            else:
                msg = (
                    f"QUORUM_INSUFFICIENT_VETO: Technical quorum not met for {dir_clean} "
                    f"({matching_ge_60}/4 match with conf>=60%, {opposing_count} opposing). Requires >=2 matching >=60% OR >=1 >=75% with 0 opposing."
                )
            return False, "QUORUM_INSUFFICIENT_VETO", msg

        return True, None, "Analyst quorum confirmed."

    @staticmethod
    def evaluate(
        strategy_profile: StrategyProfile,
        ceo_proposal: Dict[str, Any],
        profile: str,
        risk_manager: Any,
        market_data: Optional[Dict[str, Any]] = None,
        symbol: str = "",
        logger: Optional[Any] = None,
        analyst_reports: Optional[Any] = None
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

        # 1.1 Deterministic ANALYST_QUORUM_GATE (Systemic Module 01 Upgrade)
        is_pullback_retest = bool((market_data or {}).get("is_pullback_retest") or (ceo_proposal or {}).get("is_pullback_retest"))
        trigger_type = (
            (market_data or {}).get("entry_trigger_type")
            or ceo_proposal.get("entry_trigger_type")
            or ("WATCHLIST_RETEST" if is_pullback_retest else "REGULAR_SCAN")
        )
        wl_meta = (
            (market_data or {}).get("watchlist_metadata")
            or ceo_proposal.get("watchlist_metadata")
        )

        if decision in ["LONG", "SHORT"] and trade_action in ["ENTER", "REDUCE_SIZE"] and analyst_reports:
            quorum_ok, quorum_tag, quorum_reason = DeterministicGuard.evaluate_analyst_quorum(
                direction=decision,
                analyst_reports=analyst_reports,
                entry_trigger_type=trigger_type,
                market_data=market_data,
                watchlist_metadata=wl_meta
            )
            if not quorum_ok:
                guard_msg = f"🛡️ [Analyst Quorum Gate] {symbol}: {quorum_reason}"
                print(guard_msg)
                if logger:
                    logger.warning(f"[Pipeline] {guard_msg}")
                decision = "HOLD"
                conviction = 0
                trade_action = "HOLD"
                guard_status = "BLOCKED"
                rejection_tag = quorum_tag
                guard_reason = quorum_reason
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

        # ── Step 2.9: Module 03 Systemic Routing (Decoupled C_dir vs Q_loc) ──
        risk_bd = (ceo_proposal.get("risk_breakdown") or {})
        ext_pen = abs(float(risk_bd.get("price_extension", 0.0) or 0.0))
        calc_q_loc = max(0.0, min(100.0, 100.0 - (ext_pen * 4.0)))
        q_loc = float(ceo_proposal.get("location_quality", calc_q_loc) or calc_q_loc)

        if decision in ["LONG", "SHORT"] and not is_anti_bot_mr and not is_pullback_retest and not volume_explosion_triggered:
            if dir_conf < 65:
                # Case 3: Low C_dir < 65% => HOLD (No structural edge, discarded)
                decision = "HOLD"
                trade_action = "HOLD"
                is_actionable = False
                guard_status = "HOLD"
                rejection_tag = "LOW_DIRECTIONAL_CONVICTION"
                guard_reason = f"Directional conviction ({dir_conf}% < 65%) insufficient for directional trade. Routed to HOLD."
            elif q_loc < 60.0:
                # Case 2: High C_dir >= 65% AND Low Q_loc < 60% => WAIT_FOR_PULLBACK
                trade_action = "WAIT_FOR_PULLBACK"
                is_actionable = False
                guard_status = "PULLBACK_WATCHLIST"
                rejection_tag = "WAIT_FOR_PULLBACK"
                guard_reason = f"Location quality ({q_loc:.1f}% < 60%) extended from value. Routed to WAIT_FOR_PULLBACK at calculated P*."

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
            action_size_mult_override=0.50 if volume_explosion_triggered else None,
            entry_trigger_type=trigger_type,
            watchlist_metadata=wl_meta,
            target_pullback_price=ceo_proposal.get("target_pullback_price")
        )


DeterministicGuard.validate = DeterministicGuard.evaluate
