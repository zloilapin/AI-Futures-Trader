import logging
from typing import Dict, Any, Optional
from core.logger import TradeLogger

class SentinelAgent:
    """
    Deterministic Execution/Risk Controller for active positions.
    Manages Break-Even and Trailing Stops dynamically based on ATR.
    Replaces the old LLM-based logic.
    """
    def __init__(self, logger: TradeLogger, llm_client=None):
        self.name = "Sentinel_Agent"
        self.logger = logger
        # llm_client kept for signature compatibility with main.py, but unused.

    async def analyze(self, pos: Dict[str, Any], market_data: Dict[str, Any], profile_rules: dict, atr_value: float) -> Dict[str, Any]:
        """
        Evaluates if the SL should be moved to Break-Even or Trailed.
        Enforces monotonic SL (never worsen).
        """
        if not isinstance(pos, dict) or not isinstance(market_data, dict):
            return {"new_sl": None, "state": "PROTECTED", "reasoning": "Invalid position or market data dictionary."}

        current_state = pos.get("protection_state", "PROTECTED")
        try:
            entry = float(pos.get("entry_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            entry = 0.0

        price_dict = market_data.get("price_data", {}) if isinstance(market_data.get("price_data"), dict) else {}
        try:
            current_price = float(price_dict.get("current_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            current_price = 0.0

        try:
            current_sl = float(pos.get("sl_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            current_sl = 0.0

        direction = str(pos.get("direction", "LONG")).upper()

        if entry <= 0 or current_price <= 0:
            return {"new_sl": None, "state": current_state, "reasoning": "Invalid price data."}

        try:
            highest = float(pos.get("highest_price", entry) or entry)
        except (TypeError, ValueError):
            highest = entry

        try:
            lowest = float(pos.get("lowest_price", entry) or entry)
        except (TypeError, ValueError):
            lowest = entry

        # Guarantee extremes consistency with current price and entry
        if direction == "LONG":
            highest = max(highest, entry, current_price)
            pos["highest_price"] = highest
        else:
            lowest = min(lowest, entry, current_price)
            pos["lowest_price"] = lowest

        # Update running Max Favorable Excursion (MFE in R-multiples)
        p_risk = float(pos.get("planned_risk_usd", 0.0) or pos.get("approved_risk_usd", 0.0) or 0.0)
        cnt = float(pos.get("contracts", 0.0) or pos.get("amount", 0.0) or 0.0)
        if p_risk <= 0.0 and entry > 0 and cnt > 0:
            sl_init = float(pos.get("sl_price", 0.0) or 0.0)
            if sl_init > 0:
                p_risk = cnt * abs(entry - sl_init)
        if p_risk > 0 and cnt > 0 and entry > 0:
            peak_diff = (highest - entry) if direction == "LONG" else (entry - lowest)
            cur_mfe = round(max(0.0, (peak_diff * cnt) / p_risk), 4)
            if cur_mfe > float(pos.get("max_favorable_excursion_r", 0.0) or 0.0):
                pos["max_favorable_excursion_r"] = cur_mfe

        # If position is explicitly flagged as requiring ATR reconciliation, preserve native SL and suspend trailing
        if pos.get("atr_reconciliation_required") and float(pos.get("atr_reference", 0.0) or 0.0) <= 0:
            return {
                "new_sl": None,
                "state": current_state,
                "reasoning": "Позиция требует сверки ATR. Биржевые SL/TP активны, автоматическое подтягивание приостановлено."
            }

        # Use the ATR at the time of entry to prevent shrinking thresholds when volatility drops
        try:
            ref_atr = float(pos.get("atr_reference", 0.0) or 0.0)
        except (TypeError, ValueError):
            ref_atr = 0.0
        eval_atr = ref_atr if ref_atr > 0 else (atr_value if atr_value > 0 else 0.0)

        if eval_atr <= 0:
            return {"new_sl": None, "state": current_state, "reasoning": "Invalid ATR data."}

        if not isinstance(profile_rules, dict):
            profile_rules = {}

        # Profile parameters (fallback to defaults if not provided)
        from core.config import config
        be_atr = profile_rules.get("sentinel_be_atr", getattr(config, "SENTINEL_BE_ATR", 1.2))
        trail_activation = profile_rules.get("sentinel_trail_activation_atr", getattr(config, "SENTINEL_TRAIL_ACTIVATION_ATR", 2.0))
        trail_dist = profile_rules.get("sentinel_trail_distance_atr", getattr(config, "SENTINEL_TRAIL_DISTANCE_ATR", 1.5))
        min_improve = profile_rules.get("sentinel_min_improve_atr", getattr(config, "SENTINEL_MIN_IMPROVE_ATR", 0.25))

        # Dynamic cost buffer (Estimated fees + slippage)
        # Nado Taker fee is typically 0.05% (0.0005). Roundtrip = 0.1% (0.0010).
        # We ensure a minimum total buffer of at least 0.15% - 0.20% to absorb partial TP commissions & slippage.
        fee_pct = profile_rules.get("sentinel_fee_pct", 0.0010)
        slippage_pct = profile_rules.get("sentinel_slippage_pct", 0.0010)

        symbol = str(pos.get("symbol", "")).upper()
        # For non-major altcoins (neither BTC nor ETH), increase default slippage buffer if not overridden in rules
        if symbol and not ("BTC" in symbol or "ETH" in symbol) and "sentinel_slippage_pct" not in profile_rules:
            slippage_pct = 0.0025  # 0.25% exit slippage protection for volatile altcoins

        total_cost_pct = max(fee_pct + slippage_pct, 0.0015)  # Floor at 0.15% total buffer
        cost_buffer = entry * total_cost_pct

        # Minimum improvement floor: prevents trigger order churn on low-ATR noise
        min_improve_pct = profile_rules.get("sentinel_min_improve_pct", getattr(config, "SENTINEL_MIN_IMPROVE_PCT", 0.0005))
        effective_min_improve = max(min_improve * eval_atr, entry * min_improve_pct)

        # Calculate planned 1R distance from entry to initial SL
        initial_sl = float(pos.get("initial_sl") or pos.get("approved_sl_price") or 0.0)
        if initial_sl <= 0 and current_sl > 0:
            if direction == "LONG" and current_sl < entry:
                initial_sl = current_sl
            elif direction == "SHORT" and current_sl > entry:
                initial_sl = current_sl
            elif current_state in ("PROTECTED", "SOFTWARE_PROTECTED", "NATIVE_SL_CONFIRMED", "SUBMITTING_PROTECTION"):
                initial_sl = current_sl

        cnt = float(pos.get("contracts", 0.0) or pos.get("amount", 0.0) or 0.0)
        p_risk = float(pos.get("planned_risk_usd", 0.0) or pos.get("approved_risk_usd", 0.0) or 0.0)
        if initial_sl > 0:
            planned_r_dist = abs(entry - initial_sl)
        elif p_risk > 0 and cnt > 0:
            planned_r_dist = p_risk / cnt
        else:
            planned_r_dist = max(eval_atr * 1.5, entry * 0.01)

        if pos.get("initial_sl") is None and initial_sl > 0:
            pos["initial_sl"] = initial_sl

        strategy_mode = str(pos.get("strategy_mode", "")).upper()
        from core.config import config
        is_anti_bot_pos = bool(pos.get("is_anti_bot") or pos.get("anti_bot_mode") or getattr(config, "ANTI_BOT_MODE", False))
        is_anti_bot_mr = is_anti_bot_pos or (strategy_mode == "MEAN_REVERSION")
        vol_spike = float((market_data.get("indicators") or {}).get("volume_spike_pct") or 0.0) if market_data else 0.0

        # Calculate position age for Time-Stop / Stagnation Guard
        import time
        now_ts = time.time()
        open_time = float(pos.get("open_time", 0.0) or pos.get("timestamp", 0.0) or 0.0)
        age_minutes = ((now_ts - open_time) / 60.0) if open_time > 0 else 0.0

        # Lever 4: Dynamic Runner Trailing Mode
        is_runner = bool(pos.get("is_runner") or pos.get("runner"))

        candidate_sl = None
        new_state = current_state
        reasoning = ""

        # Minimum price move distance (default 1.2% of entry to prevent premature BE on tight stops)
        min_be_pct = profile_rules.get("sentinel_min_be_pct", 0.012)
        min_pct_distance = entry * min_be_pct

        # Check if Partial TP Tier 1 is still pending (strictly for MULTI_TIER)
        ptp_plan = pos.get("partial_tp_plan")
        has_pending_tp1 = False
        tp1_target_dist = None
        if isinstance(ptp_plan, dict) and ptp_plan.get("mode") != "SINGLE_TIER":
            tiers = ptp_plan.get("tiers", [])
            if tiers and isinstance(tiers, list) and not tiers[0].get("is_final", False) and tiers[0].get("status") == "PENDING":
                has_pending_tp1 = True
                tp1_target = tiers[0].get("target_price")
                if tp1_target is not None:
                    try:
                        t_price = float(tp1_target)
                        if t_price > 0:
                            tp1_target_dist = abs(t_price - entry)
                    except (ValueError, TypeError):
                        pass

        # Break-Even & Three-Stage Profit Protection Parameters
        # Stage 1: HALF_RISK_LOCK at +0.75R MFE (compresses SL to -0.40R)
        # Stage 2: BE_SCRATCH_LOCK at +1.15R MFE (moves SL to BE + 0.20R)
        # Stage 3: STEPPED_ATR_TRAIL at +1.50R+ MFE
        use_three_stage = bool(
            pos.get("three_stage_trailing")
            or profile_rules.get("three_stage_trailing")
            or float(profile_rules.get("be_activation_r") or pos.get("be_activation_r") or 0.0) >= 1.15
        )
        half_risk_r = float(profile_rules.get("half_risk_activation_r") or pos.get("half_risk_activation_r") or 0.75)
        be_scratch_r = float(profile_rules.get("be_activation_r") or pos.get("be_activation_r") or 1.15)
        stepped_trail_r = float(profile_rules.get("trail_activation_r") or pos.get("trail_activation_r") or 1.50)

        tuned_be_r = be_scratch_r if use_three_stage else float(profile_rules.get("be_activation_r") or pos.get("be_activation_r") or 0.75)
        if has_pending_tp1 and tp1_target_dist is not None and tp1_target_dist > 0:
            required_be_distance = max(tp1_target_dist, cost_buffer * 2.0)
        elif planned_r_dist > 0:
            required_be_distance = max(tuned_be_r * planned_r_dist, cost_buffer * 2.0)
        else:
            required_be_distance = max(min_pct_distance, be_atr * eval_atr)

        if direction == "LONG":
            profit_distance = current_price - entry
            peak_profit = highest - entry
            current_r = (profit_distance / planned_r_dist) if planned_r_dist > 0 else (profit_distance / eval_atr)
            peak_r = (peak_profit / planned_r_dist) if planned_r_dist > 0 else (peak_profit / eval_atr)
            effective_r = max(current_r, peak_r)
            tp1_blocked = has_pending_tp1 and tp1_target_dist is not None and tp1_target_dist > 0 and profit_distance < required_be_distance

            # 1. Three-Stage Profit Protection (or Legacy BE)
            if use_three_stage and not tp1_blocked and not is_runner:
                # Stage 3: Stepped ATR Trail at effective_r >= 1.50R
                if effective_r >= stepped_trail_r:
                    floor_r = 1.20 if effective_r >= 2.00 else 0.75
                    stepped_floor = entry + (floor_r * planned_r_dist) if planned_r_dist > 0 else (entry + (floor_r * eval_atr))
                    atr_trail = highest - (1.2 * eval_atr)
                    trail_target = max(stepped_floor, atr_trail)
                    be_floor = entry + max(cost_buffer, 0.20 * planned_r_dist)
                    trail_target = max(trail_target, be_floor)
                    if candidate_sl is None or trail_target > candidate_sl:
                        candidate_sl = trail_target
                        new_state = "TRAILING"
                        reasoning = (
                            f"Stage 3 Stepped ATR Trail: effective_r={effective_r:.2f}R >= {stepped_trail_r:.2f}R. "
                            f"SL trailed to {candidate_sl:.4f} (floor=+{floor_r:.2f}R, 1.2x ATR behind high={highest:.4f})."
                        )
                # Stage 2: BE_SCRATCH_LOCK at 1.15R <= effective_r < 1.50R
                elif effective_r >= be_scratch_r:
                    be_buf = max(cost_buffer, 0.20 * planned_r_dist) if planned_r_dist > 0 else cost_buffer
                    be_target = entry + be_buf
                    if candidate_sl is None or be_target > candidate_sl:
                        candidate_sl = be_target
                        new_state = "BREAK_EVEN"
                        reasoning = (
                            f"Stage 2 BE Scratch Lock: effective_r={effective_r:.2f}R >= {be_scratch_r:.2f}R. "
                            f"SL moved to BE + buffer ({be_target:.4f}, +0.20R) protecting trade from noise."
                        )
                # Stage 1: HALF_RISK_LOCK at 0.75R <= effective_r < 1.15R
                elif effective_r >= half_risk_r:
                    half_risk_target = entry - (0.40 * planned_r_dist) if planned_r_dist > 0 else (entry - (0.60 * eval_atr))
                    if candidate_sl is None or half_risk_target > candidate_sl:
                        candidate_sl = half_risk_target
                        new_state = "HALF_RISK_LOCK"
                        reasoning = (
                            f"Stage 1 Half Risk Lock: effective_r={effective_r:.2f}R >= {half_risk_r:.2f}R. "
                            f"Initial risk compressed from -1.00R to -0.40R ({half_risk_target:.4f}) to allow structural retests."
                        )
            else:
                # Legacy 1-Stage Break-Even
                if has_pending_tp1 and tp1_target_dist is not None and tp1_target_dist > 0:
                    is_be_reached = profit_distance >= required_be_distance
                else:
                    is_be_reached = (profit_distance >= required_be_distance) or (effective_r >= tuned_be_r)

                if is_be_reached:
                    be_target = entry + cost_buffer
                    if current_sl < be_target:
                        candidate_sl = be_target
                        new_state = "BREAK_EVEN"
                        r_str = f" ({current_r:.2f}R, peak={peak_r:.2f}R)" if planned_r_dist > 0 else ""
                        tp1_note = " (TP1 synchronized)" if has_pending_tp1 else ""
                        reasoning = f"Price reached BE trigger{r_str}{tp1_note} (dist: {profit_distance:.4f} >= required {required_be_distance:.4f}). SL moved to Break-Even + cost buffer."

            # 1.5. Breakout Stagnation Guard (Breakout Momentum Decay at 75 min - strictly skipped for Anti-Bot / MR)
            max_mfe = float(pos.get("max_favorable_excursion_r", 0.0) or 0.0)
            if not is_anti_bot_mr and strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and age_minutes >= 75.0:
                cur_r = (profit_distance / planned_r_dist) if planned_r_dist > 0 else 0.0
                be_target = entry + cost_buffer
                if max_mfe >= 0.45 and cur_r <= 0.15:
                    if current_sl < be_target:
                        candidate_sl = be_target
                        new_state = "BREAKOUT_STAGNATION_BE"
                        reasoning = (
                            f"Breakout Stagnation Guard: Позиция {age_minutes:.0f}м в рынке (>=75м). "
                            f"MFE достигало +{max_mfe:.2f}R, но прибыль сдулась до {cur_r:.2f}R. "
                            f"SL подтянут в безубыток (+cost_buffer) для ликвидации риска отката."
                        )
                elif max_mfe < 0.45 and current_sl < entry:
                    tightened_sl = entry - (0.50 * planned_r_dist) if planned_r_dist > 0 else (entry - (0.75 * eval_atr))
                    if tightened_sl > current_sl:
                        candidate_sl = tightened_sl
                        new_state = "BREAKOUT_STAGNATION_TIGHTEN"
                        reasoning = (
                            f"Breakout Stagnation Guard: Позиция {age_minutes:.0f}м в рынке (>=75м) без импульса "
                            f"(MFE={max_mfe:.2f}R < 0.45R). Риск урезан на 50% (SL подтянут до -0.50R)."
                        )

            # 1.6. Extreme Blowout Defense: Disabled for Anti-Bot MR (v2.1 Patch: band extremes are normal entry zones for MEAN_REVERSION; strictly defer to planned sl_price)
            # (No premature tightening on volume spikes for MEAN_REVERSION)

            # 2. Time-Stop / Stagnation Guard (Time-Based Risk Decay - Doubled for Anti-Bot MR: 360m / 480m / 720m)
            # Stop Choking Prevention: TREND_FOLLOWING needs 180m and at least 0.25R profit to avoid premature BE exit
            t1_age = 360.0 if is_anti_bot_mr else 180.0
            t2_age = 480.0 if is_anti_bot_mr else 240.0
            t3_age = 720.0 if is_anti_bot_mr else 360.0
            if age_minutes >= t1_age and (candidate_sl is None or candidate_sl <= current_sl):
                be_target = entry + cost_buffer
                min_be_r = 0.25 if strategy_mode == "TREND_FOLLOWING" else 0.15
                is_modest_profit = (planned_r_dist > 0 and profit_distance >= min_be_r * planned_r_dist) or (profit_distance >= min_be_r * eval_atr)
                if is_modest_profit and current_sl < be_target:
                    candidate_sl = be_target
                    new_state = "TIME_STOP_BE"
                    reasoning = f"Time-Stop: Позиция удерживается {age_minutes:.0f}м (>={t1_age:.0f}м) с прибылью (+{profit_distance:.4f} >= {min_be_r:.2f}R). SL подтянут в безубыток для защиты от отката."
                elif age_minutes >= t3_age and current_sl < entry:
                    tightened_sl = entry - (0.25 * planned_r_dist) if planned_r_dist > 0 else (entry - (0.40 * eval_atr))
                    if tightened_sl > current_sl:
                        candidate_sl = tightened_sl
                        new_state = "TIME_STOP_MAX_HORIZON"
                        reasoning = f"Time-Stop: Позиция удерживается {age_minutes:.0f}м (>={t3_age:.0f}м). Достигнут макс. горизонт, SL подтянут до -0.25R для ликвидации зомби-риска."
                elif age_minutes >= t2_age and current_sl < entry:
                    tightened_sl = entry - (0.50 * planned_r_dist) if planned_r_dist > 0 else (entry - (0.75 * eval_atr))
                    if tightened_sl > current_sl:
                        candidate_sl = tightened_sl
                        new_state = "TIME_STOP_TIGHTEN"
                        reasoning = f"Time-Stop: Позиция удерживается {age_minutes:.0f}м (>={t2_age:.0f}м) без импульса. Риск урезан на 50% (SL подтянут до -0.50R)."

            # 3. Dynamic Parabolic Trailing Check (strictly at effective_r >= 1.35R or runner)
            can_trail = not (use_three_stage and not is_runner and effective_r < stepped_trail_r) and (
                (is_runner and effective_r >= 1.35)
                or (
                    current_state == "TRAILING"
                    and (("initial_sl" not in pos and "planned_risk_usd" not in pos) or effective_r >= 1.35)
                )
                or (
                    effective_r >= 1.35
                    and (
                        current_state in ("BREAK_EVEN", "SOFTWARE_BREAK_EVEN", "TRAILING", "RUNNER")
                        or profit_distance >= (trail_activation * eval_atr)
                    )
                )
            )

            if can_trail:
                use_parabolic = is_runner or (strategy_mode == "TREND_FOLLOWING")
                if use_parabolic:
                    runner_trail_dist = min(trail_dist, 1.5)
                    parabolic_mult = 1.0 / (1.0 + 0.35 * max(0.0, effective_r - 1.0))
                    # Profit Booster #3: Stepped floor gives runners breathing room (0.85*ATR) between 1.35R-3.0R
                    min_atr_floor = 0.50 if effective_r >= 3.0 else 0.85
                    clamped_dist = max(min_atr_floor, runner_trail_dist * parabolic_mult)
                    trail_target = highest - (clamped_dist * eval_atr)
                    reasoning = (
                        f"Dynamic Parabolic Trailing: SL trailed {clamped_dist:.2f}x ATR ({trail_target:.4f}) "
                        f"behind peak ({highest:.4f}, peak_R={effective_r:.2f}R)."
                    )
                else:
                    trail_target = highest - (trail_dist * eval_atr)
                    reasoning = f"Price passed 1.35R trailing threshold. SL trailed {trail_dist}x ATR from highest price."

                # Trailing SL must never be placed below Break-Even floor
                # And at effective_r >= 1.35R, guarantee locked result does not drop below +0.75R..+1.0R floor
                be_floor = entry + cost_buffer
                lock_in_floor = (entry + (0.75 * planned_r_dist)) if (planned_r_dist > 0 and initial_sl > 0 and effective_r >= 1.35) else be_floor
                min_trail_floor = max(be_floor, lock_in_floor)
                trail_target = max(trail_target, min_trail_floor)

                if candidate_sl is None or trail_target > candidate_sl:
                    candidate_sl = trail_target
                    new_state = "TRAILING"

            # Enforce Monotonic SL (Never worsen)
            if candidate_sl is not None:
                new_sl = max(current_sl, candidate_sl) if current_sl > 0 else candidate_sl
                # Check minimum improvement threshold (>= 0.15R hysteresis when three_stage_trailing is explicitly set)
                min_hysteresis = max(effective_min_improve, 0.15 * planned_r_dist) if (bool(pos.get("three_stage_trailing")) and planned_r_dist > 0) else effective_min_improve
                if current_sl == 0 or (new_sl - current_sl) >= min_hysteresis:
                    return {"new_sl": new_sl, "state": new_state, "reasoning": reasoning}

        else: # SHORT
            profit_distance = entry - current_price
            trough_profit = entry - lowest
            current_r = (profit_distance / planned_r_dist) if planned_r_dist > 0 else (profit_distance / eval_atr)
            peak_r = (trough_profit / planned_r_dist) if planned_r_dist > 0 else (trough_profit / eval_atr)
            effective_r = max(current_r, peak_r)
            tp1_blocked = has_pending_tp1 and tp1_target_dist is not None and tp1_target_dist > 0 and profit_distance < required_be_distance

            # 1. Three-Stage Profit Protection (or Legacy BE)
            if use_three_stage and not tp1_blocked and not is_runner:
                # Stage 3: Stepped ATR Trail at effective_r >= 1.50R
                if effective_r >= stepped_trail_r:
                    floor_r = 1.20 if effective_r >= 2.00 else 0.75
                    stepped_ceiling = entry - (floor_r * planned_r_dist) if planned_r_dist > 0 else (entry - (floor_r * eval_atr))
                    atr_trail = lowest + (1.2 * eval_atr)
                    trail_target = min(stepped_ceiling, atr_trail)
                    be_floor = entry - max(cost_buffer, 0.20 * planned_r_dist)
                    trail_target = min(trail_target, be_floor)
                    if candidate_sl is None or trail_target < candidate_sl:
                        candidate_sl = trail_target
                        new_state = "TRAILING"
                        reasoning = (
                            f"Stage 3 Stepped ATR Trail: effective_r={effective_r:.2f}R >= {stepped_trail_r:.2f}R. "
                            f"SL trailed to {candidate_sl:.4f} (floor=+{floor_r:.2f}R, 1.2x ATR above low={lowest:.4f})."
                        )
                # Stage 2: BE_SCRATCH_LOCK at 1.15R <= effective_r < 1.50R
                elif effective_r >= be_scratch_r:
                    be_buf = max(cost_buffer, 0.20 * planned_r_dist) if planned_r_dist > 0 else cost_buffer
                    be_target = entry - be_buf
                    if candidate_sl is None or be_target < candidate_sl:
                        candidate_sl = be_target
                        new_state = "BREAK_EVEN"
                        reasoning = (
                            f"Stage 2 BE Scratch Lock: effective_r={effective_r:.2f}R >= {be_scratch_r:.2f}R. "
                            f"SL moved to BE - buffer ({be_target:.4f}, +0.20R) protecting trade from noise."
                        )
                # Stage 1: HALF_RISK_LOCK at 0.75R <= effective_r < 1.15R
                elif effective_r >= half_risk_r:
                    half_risk_target = entry + (0.40 * planned_r_dist) if planned_r_dist > 0 else (entry + (0.60 * eval_atr))
                    if candidate_sl is None or half_risk_target < candidate_sl:
                        candidate_sl = half_risk_target
                        new_state = "HALF_RISK_LOCK"
                        reasoning = (
                            f"Stage 1 Half Risk Lock: effective_r={effective_r:.2f}R >= {half_risk_r:.2f}R. "
                            f"Initial risk compressed from -1.00R to -0.40R ({half_risk_target:.4f}) to allow structural retests."
                        )
            else:
                # Legacy 1-Stage Break-Even
                if has_pending_tp1 and tp1_target_dist is not None and tp1_target_dist > 0:
                    is_be_reached = profit_distance >= required_be_distance
                else:
                    is_be_reached = (profit_distance >= required_be_distance) or (effective_r >= tuned_be_r)

                if is_be_reached:
                    be_target = entry - cost_buffer
                    if current_sl > be_target or current_sl == 0:
                        candidate_sl = be_target
                        new_state = "BREAK_EVEN"
                        r_str = f" ({current_r:.2f}R, peak={peak_r:.2f}R)" if planned_r_dist > 0 else ""
                        tp1_note = " (TP1 synchronized)" if has_pending_tp1 else ""
                        reasoning = f"Price reached BE trigger{r_str}{tp1_note} (dist: {profit_distance:.4f} >= required {required_be_distance:.4f}). SL moved to Break-Even - cost buffer."

            # 1.5. Breakout Stagnation Guard (Breakout Momentum Decay at 75 min - strictly skipped for Anti-Bot / MR)
            max_mfe = float(pos.get("max_favorable_excursion_r", 0.0) or 0.0)
            if not is_anti_bot_mr and strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and age_minutes >= 75.0:
                cur_r = (profit_distance / planned_r_dist) if planned_r_dist > 0 else 0.0
                be_target = entry - cost_buffer
                if max_mfe >= 0.45 and cur_r <= 0.15:
                    if current_sl > be_target or current_sl == 0:
                        candidate_sl = be_target
                        new_state = "BREAKOUT_STAGNATION_BE"
                        reasoning = (
                            f"Breakout Stagnation Guard: Позиция {age_minutes:.0f}м в рынке (>=75м). "
                            f"MFE достигало +{max_mfe:.2f}R, но прибыль сдулась до {cur_r:.2f}R. "
                            f"SL подтянут в безубыток (-cost_buffer) для ликвидации риска отката."
                        )
                elif max_mfe < 0.45 and (current_sl == 0 or current_sl > entry):
                    tightened_sl = entry + (0.50 * planned_r_dist) if planned_r_dist > 0 else (entry + (0.75 * eval_atr))
                    if current_sl == 0 or tightened_sl < current_sl:
                        candidate_sl = tightened_sl
                        new_state = "BREAKOUT_STAGNATION_TIGHTEN"
                        reasoning = (
                            f"Breakout Stagnation Guard: Позиция {age_minutes:.0f}м в рынке (>=75м без импульса) "
                            f"(MFE={max_mfe:.2f}R < 0.45R). Риск урезан на 50% (SL подтянут до -0.50R)."
                        )

            # 1.6. Extreme Blowout Defense: Disabled for Anti-Bot MR (v2.1 Patch: band extremes are normal entry zones for MEAN_REVERSION; strictly defer to planned sl_price)
            # (No premature tightening on volume spikes for MEAN_REVERSION)

            # 2. Time-Stop / Stagnation Guard (Time-Based Risk Decay - Doubled for Anti-Bot MR: 360m / 480m / 720m)
            # Stop Choking Prevention: TREND_FOLLOWING needs 180m and at least 0.25R profit to avoid premature BE exit
            t1_age = 360.0 if is_anti_bot_mr else 180.0
            t2_age = 480.0 if is_anti_bot_mr else 240.0
            t3_age = 720.0 if is_anti_bot_mr else 360.0
            if age_minutes >= t1_age and (candidate_sl is None or (current_sl > 0 and candidate_sl >= current_sl)):
                be_target = entry - cost_buffer
                min_be_r = 0.25 if strategy_mode == "TREND_FOLLOWING" else 0.15
                is_modest_profit = (planned_r_dist > 0 and profit_distance >= min_be_r * planned_r_dist) or (profit_distance >= min_be_r * eval_atr)
                if is_modest_profit and (current_sl > be_target or current_sl == 0):
                    candidate_sl = be_target
                    new_state = "TIME_STOP_BE"
                    reasoning = f"Time-Stop: Позиция удерживается {age_minutes:.0f}м (>={t1_age:.0f}м) с прибылью (+{profit_distance:.4f} >= {min_be_r:.2f}R). SL подтянут в безубыток для защиты от отката."
                elif age_minutes >= t3_age and (current_sl == 0 or current_sl > entry):
                    tightened_sl = entry + (0.25 * planned_r_dist) if planned_r_dist > 0 else (entry + (0.40 * eval_atr))
                    if current_sl == 0 or tightened_sl < current_sl:
                        candidate_sl = tightened_sl
                        new_state = "TIME_STOP_MAX_HORIZON"
                        reasoning = f"Time-Stop: Позиция удерживается {age_minutes:.0f}м (>={t3_age:.0f}м). Достигнут макс. горизонт, SL подтянут до -0.25R для ликвидации зомби-риска."
                elif age_minutes >= t2_age and (current_sl == 0 or current_sl > entry):
                    tightened_sl = entry + (0.50 * planned_r_dist) if planned_r_dist > 0 else (entry + (0.75 * eval_atr))
                    if current_sl == 0 or tightened_sl < current_sl:
                        candidate_sl = tightened_sl
                        new_state = "TIME_STOP_TIGHTEN"
                        reasoning = f"Time-Stop: Позиция удерживается {age_minutes:.0f}м (>={t2_age:.0f}м) без импульса. Риск урезан на 50% (SL подтянут до -0.50R)."

            # 3. Dynamic Parabolic Trailing Check (strictly at effective_r >= 1.35R or runner)
            can_trail = not (use_three_stage and not is_runner and effective_r < stepped_trail_r) and (
                (is_runner and effective_r >= 1.35)
                or (
                    current_state == "TRAILING"
                    and (("initial_sl" not in pos and "planned_risk_usd" not in pos) or effective_r >= 1.35)
                )
                or (
                    effective_r >= 1.35
                    and (
                        current_state in ("BREAK_EVEN", "SOFTWARE_BREAK_EVEN", "TRAILING", "RUNNER")
                        or profit_distance >= (trail_activation * eval_atr)
                    )
                )
            )

            if can_trail:
                use_parabolic = is_runner or (strategy_mode == "TREND_FOLLOWING")
                if use_parabolic:
                    runner_trail_dist = min(trail_dist, 1.5)
                    parabolic_mult = 1.0 / (1.0 + 0.35 * max(0.0, effective_r - 1.0))
                    # Profit Booster #3: Stepped floor gives runners breathing room (0.85*ATR) between 1.35R-3.0R
                    min_atr_floor = 0.50 if effective_r >= 3.0 else 0.85
                    clamped_dist = max(min_atr_floor, runner_trail_dist * parabolic_mult)
                    trail_target = lowest + (clamped_dist * eval_atr)
                    reasoning = (
                        f"Dynamic Parabolic Trailing: SL trailed {clamped_dist:.2f}x ATR ({trail_target:.4f}) "
                        f"above trough ({lowest:.4f}, trough_R={effective_r:.2f}R)."
                    )
                else:
                    trail_target = lowest + (trail_dist * eval_atr)
                    reasoning = f"Price passed 1.35R trailing threshold. SL trailed {trail_dist}x ATR from lowest price."

                # Trailing SL must never be placed above Break-Even floor
                # And at effective_r >= 1.35R, guarantee locked result does not drop below +0.75R..+1.0R floor
                be_floor = entry - cost_buffer
                lock_in_floor = (entry - (0.75 * planned_r_dist)) if (planned_r_dist > 0 and initial_sl > 0 and effective_r >= 1.35) else be_floor
                max_trail_ceiling = min(be_floor, lock_in_floor)
                trail_target = min(trail_target, max_trail_ceiling)

                if candidate_sl is None or trail_target < candidate_sl:
                    candidate_sl = trail_target
                    new_state = "TRAILING"

            # Enforce Monotonic SL (Never worsen)
            if candidate_sl is not None:
                new_sl = min(current_sl, candidate_sl) if current_sl > 0 else candidate_sl
                # Check minimum improvement threshold (>= 0.15R hysteresis when three_stage_trailing is explicitly set)
                min_hysteresis = max(effective_min_improve, 0.15 * planned_r_dist) if (bool(pos.get("three_stage_trailing")) and planned_r_dist > 0) else effective_min_improve
                if current_sl == 0 or (current_sl - new_sl) >= min_hysteresis:
                    return {"new_sl": new_sl, "state": new_state, "reasoning": reasoning}

        return {"new_sl": None, "state": current_state, "reasoning": "No update required."}


SentinelAgent.evaluate = SentinelAgent.analyze

SentinelAgent.update_trailing_sl = SentinelAgent.analyze
