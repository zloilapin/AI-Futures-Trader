# CRASH AUDIT EXTENSION: BLOCK_06 TAIL (UPDATED)

## sentinel_agent.py (from line 216 to end)
```python
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
            can_trail = (
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
                # Check minimum improvement threshold
                if current_sl == 0 or (new_sl - current_sl) >= effective_min_improve:
                    return {"new_sl": new_sl, "state": new_state, "reasoning": reasoning}

        else: # SHORT
            profit_distance = entry - current_price
            trough_profit = entry - lowest
            current_r = (profit_distance / planned_r_dist) if planned_r_dist > 0 else (profit_distance / eval_atr)
            peak_r = (trough_profit / planned_r_dist) if planned_r_dist > 0 else (trough_profit / eval_atr)
            effective_r = max(current_r, peak_r)

            # 1. Break-Even Check: strictly for 0.75R <= effective_r < 1.35R (single move to BE)
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
            can_trail = (
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
                # Check minimum improvement threshold
                if current_sl == 0 or (current_sl - new_sl) >= effective_min_improve:
                    return {"new_sl": new_sl, "state": new_state, "reasoning": reasoning}

        return {"new_sl": None, "state": current_state, "reasoning": "No update required."}


SentinelAgent.evaluate = SentinelAgent.analyze

SentinelAgent.update_trailing_sl = SentinelAgent.analyze
```

---

## partial_tp_calculator.py (FULL FILE)
```python
"""
Pure Deterministic Partial Take-Profit Calculator & Plan Manager.
Phase 1 implementation: Mathematical calculation, lot sizing, and tier adaptation.
Zero network dependencies, pure algorithmic logic.
"""

import math
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional


@dataclass
class TPTier:
    tier_index: int               # 1-indexed (1, 2, 3, 4)
    fraction: float               # Nominal fraction target (e.g. 0.25, 0.50, 0.75, 1.00)
    target_price: float           # Price rounded to market price_increment
    close_contracts: float        # Exact contracts to close at this tier (aligned to size_increment)
    cumulative_fraction: float    # Cumulative nominal fraction closed after this tier
    status: str = "PENDING"       # PENDING, SUBMITTING, FILLED, PARTIALLY_FILLED, FAILED, CANCELED
    is_final: bool = False        # True if this tier liquidates all remaining volume
    action: Optional[str] = None  # e.g. "PROTECT_BREAKEVEN", "START_TRAILING", "CLOSE_ALL"
    order_digest: Optional[str] = None
    filled_contracts: float = 0.0
    fill_price: Optional[float] = None
    realized_pnl_usd: float = 0.0
    executed_at: Optional[float] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    last_attempt_time: Optional[float] = None
    requires_pnl_reconciliation: bool = False
    pnl_reconciled: bool = True

    @property
    def remaining_tier_contracts(self) -> float:
        """Returns pending contracts remaining to be filled for this specific tier."""
        return max(0.0, round(self.close_contracts - self.filled_contracts, 8))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TPTier":
        valid_fields = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class PartialTPPlan:
    symbol: str
    direction: str                # "LONG" or "SHORT"
    entry_price: float
    tp_final_price: float
    total_contracts: float
    tiers: List[TPTier]
    mode: str                     # "4_TIER", "2_TIER", "SINGLE_TIER"
    min_size: float
    size_increment: float
    price_increment: float
    remaining_contracts: float
    be_activated: bool = False
    created_at: Optional[float] = None

    def get_active_or_next_tier(self) -> Optional[TPTier]:
        """
        Returns the currently active tier to execute or resolve.
        Strict sequential invariant:
        - If any earlier tier is SUBMITTING or PARTIALLY_FILLED (with remaining >= min_size),
          it MUST be completed/resolved before any subsequent tier can be processed.
        - Unclosable dust (< min_size) on intermediate tiers is absorbed into the final tier.
        - FAILED tiers are returned for bounded retry or halting.
        - Returns None if all tiers are FILLED or no actionable tier exists.
        """
        for tier in self.tiers:
            # 0. Halted tier stops the entire ladder
            if tier.status == "HALTED":
                return None

            # 1. In-flight or crash-recovery tier
            if tier.status == "SUBMITTING":
                return tier
            
            # 2. Incomplete partial fill that still has tradable volume
            if tier.status == "PARTIALLY_FILLED":
                rem = tier.remaining_tier_contracts
                if rem >= self.min_size and rem >= self.size_increment:
                    return tier
                else:
                    # Dust remainder (< min_size) cannot be closed as a standalone order on exchange.
                    # Mark this tier as FILLED; absorb dust remainder into the final tier.
                    tier.status = "FILLED"
                    final_tier = next((t for t in self.tiers if t.is_final), None)
                    if final_tier and final_tier != tier:
                        final_tier.close_contracts = round(final_tier.close_contracts + rem, 8)
                    continue
            
            # 3. Next pending tier (strictly sequential)
            if tier.status == "PENDING":
                return tier
                
            # 4. Failed tier (caller evaluates retry policy vs exhaustion)
            if tier.status == "FAILED":
                return tier

        return None

    def get_next_pending_tier(self) -> Optional[TPTier]:
        """Backward-compatible alias pointing to get_active_or_next_tier."""
        return self.get_active_or_next_tier()

    def get_tier(self, tier_index: int) -> Optional[TPTier]:
        """Returns tier by its 1-indexed number."""
        for tier in self.tiers:
            if tier.tier_index == tier_index:
                return tier
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "tp_final_price": self.tp_final_price,
            "total_contracts": self.total_contracts,
            "tiers": [t.to_dict() for t in self.tiers],
            "mode": self.mode,
            "min_size": self.min_size,
            "size_increment": self.size_increment,
            "price_increment": self.price_increment,
            "remaining_contracts": self.remaining_contracts,
            "be_activated": self.be_activated,
            "created_at": self.created_at
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PartialTPPlan":
        tiers = [TPTier.from_dict(t) for t in data.get("tiers", [])]
        return cls(
            symbol=data["symbol"],
            direction=data["direction"],
            entry_price=float(data["entry_price"]),
            tp_final_price=float(data["tp_final_price"]),
            total_contracts=float(data["total_contracts"]),
            tiers=tiers,
            mode=data.get("mode", "SINGLE_TIER"),
            min_size=float(data.get("min_size", 0.0)),
            size_increment=float(data.get("size_increment", 0.0)),
            price_increment=float(data.get("price_increment", 0.0)),
            remaining_contracts=float(data.get("remaining_contracts", data["total_contracts"])),
            be_activated=bool(data.get("be_activated", False)),
            created_at=data.get("created_at")
        )


class PartialTPCalculator:
    """
    Algorithmic calculator for multi-tier partial take-profit plans.
    Guarantees:
    - Accurate pricing strictly based on distance fractions.
    - Sizing strictly floored to size_increment (never inflates risk).
    - Dynamic adaptive ladder (4-tier -> 2-tier -> single-tier) based on product min_size.
    - Zero dust: the final tier always closes the exact remaining balance.
    """

    @staticmethod
    def round_to_tick(price: float, tick_size: float) -> float:
        """Rounds price to the nearest tick_size with clean floating precision."""
        if tick_size <= 0:
            return round(price, 6)
        
        ticks = round(price / tick_size)
        rounded = ticks * tick_size
        
        # Determine decimal places of tick_size for clean float representation
        tick_str = f"{tick_size:.10f}".rstrip("0")
        decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
        return round(rounded, min(decimals, 8))

    @staticmethod
    def ceil_to_tick(price: float, tick_size: float) -> float:
        """Rounds price UP to next tick away from entry (for LONG TP)."""
        if tick_size <= 0:
            return round(price, 6)
        ticks = math.ceil(round(price / tick_size, 8))
        rounded = ticks * tick_size
        tick_str = f"{tick_size:.10f}".rstrip("0")
        decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
        return round(rounded, min(decimals, 8))

    @staticmethod
    def floor_to_tick(price: float, tick_size: float) -> float:
        """Rounds price DOWN to next tick away from entry (for SHORT TP)."""
        if tick_size <= 0:
            return round(price, 6)
        ticks = math.floor(round(price / tick_size, 8))
        rounded = ticks * tick_size
        tick_str = f"{tick_size:.10f}".rstrip("0")
        decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
        return round(rounded, min(decimals, 8))

    @staticmethod
    def floor_to_step(amount: float, step_size: float) -> float:
        """Floors amount down to the nearest step_size to never exceed allowed risk."""
        if step_size <= 0:
            return round(amount, 6)
        
        steps = math.floor(round(amount / step_size, 8))
        floored = steps * step_size
        
        step_str = f"{step_size:.10f}".rstrip("0")
        decimals = len(step_str.split(".")[1]) if "." in step_str else 0
        return round(floored, min(decimals, 8))

    @classmethod
    def calculate_plan(
        cls,
        symbol: str,
        direction: str,
        entry_price: float,
        tp_final_price: float,
        total_contracts: float,
        market_params: Dict[str, Any],
        created_at: Optional[float] = None,
        preferred_mode: Optional[str] = None,
        sl_price: Optional[float] = None
    ) -> PartialTPPlan:
        """
        Builds a comprehensive PartialTPPlan based on entry, final TP, exchange product parameters,
        and preferred tier mode (2_TIER, 4_TIER, SINGLE_TIER).
        """
        dir_clean = str(direction).strip().upper()
        if dir_clean not in ("LONG", "SHORT"):
            raise ValueError(f"Invalid direction '{direction}'. Must be LONG or SHORT.")

        if entry_price <= 0 or tp_final_price <= 0 or total_contracts <= 0:
            raise ValueError("entry_price, tp_final_price, and total_contracts must all be > 0.")

        if dir_clean == "LONG" and tp_final_price <= entry_price:
            raise ValueError(f"For LONG, tp_final_price ({tp_final_price}) must be > entry_price ({entry_price}).")
        elif dir_clean == "SHORT" and tp_final_price >= entry_price:
            raise ValueError(f"For SHORT, tp_final_price ({tp_final_price}) must be < entry_price ({entry_price}).")

        # Resolve preferred mode (defaults to 2_TIER / 50-50 per risk design)
        if preferred_mode is None:
            try:
                from core.config import config
                preferred_mode = getattr(config, "PARTIAL_TP_MODE", "2_TIER")
            except Exception:
                preferred_mode = "2_TIER"
        preferred_mode = str(preferred_mode or "2_TIER").upper().strip()

        # Extract market parameters
        size_inc = float(
            market_params.get("size_increment")
            or market_params.get("size_increment_base")
            or 0.0
        )
        if size_inc <= 0 and market_params.get("size_increment_x18"):
            try:
                size_inc = float(market_params["size_increment_x18"]) / 1e18
            except (ValueError, TypeError):
                size_inc = 0.001
        if size_inc <= 0:
            size_inc = 0.001

        price_inc = float(
            market_params.get("price_increment")
            or 0.0
        )
        if price_inc <= 0 and market_params.get("price_increment_x18"):
            try:
                price_inc = float(market_params["price_increment_x18"]) / 1e18
            except (ValueError, TypeError):
                price_inc = 0.01
        if price_inc <= 0:
            price_inc = 0.01

        min_size = float(market_params.get("min_size") or 0.0)
        if min_size <= 0:
            min_size = size_inc

        # Total distance
        delta_tp = abs(tp_final_price - entry_price)

        # Helper to compute price at fraction f (Directed Rounding ensures targets never move closer to entry)
        def get_price_at(fraction: float) -> float:
            if dir_clean == "LONG":
                raw_price = entry_price + (delta_tp * fraction)
                return cls.ceil_to_tick(raw_price, price_inc)
            else:
                raw_price = entry_price - (delta_tp * fraction)
                return cls.floor_to_tick(raw_price, price_inc)

        # Total contracts rounded down to step size
        total_contracts = cls.floor_to_step(total_contracts, size_inc)
        if total_contracts <= 0:
            raise ValueError(f"Total contracts ({total_contracts}) is zero after size_increment alignment.")

        # Minimum notional exchange threshold (e.g. from market_params or default $10.00 / min threshold)
        min_notional = float(market_params.get("min_notional") or 0.0)

        # --- TIER SIZING & ADAPTATION LOGIC ---
        if preferred_mode == "2_TIER":
            # 2-Tier Candidate (50% / 50% with immediate Breakeven on TP1)
            q2 = cls.floor_to_step(0.50 * total_contracts, size_inc)
            rem2 = cls.floor_to_step(total_contracts - q2, size_inc)

            # Check both contract size constraints and notional size constraints.
            # For SHORT positions, TP price is lower than entry; evaluate notional at min(entry_price, tp_final_price)
            # with an extra 5% buffer to guarantee neither tier ever falls below exchange min_notional.
            worst_price = min(entry_price, tp_final_price)
            notional_q2 = q2 * worst_price
            notional_rem2 = rem2 * worst_price
            effective_min_notional = min_notional * 1.05 if min_notional > 0 else 0.0

            can_do_2_tier = (
                q2 >= min_size and
                rem2 >= min_size and
                q2 > 0 and
                rem2 > 0 and
                (effective_min_notional <= 0 or (notional_q2 >= effective_min_notional and notional_rem2 >= effective_min_notional))
            )

            if can_do_2_tier:
                # If sl_price is provided, anchor TP1 to tp1_r_mult (default 1.0R, or tuned 0.75R) if within (0, 0.85 * delta_tp)
                t1_target = get_price_at(0.50)
                target_sl = sl_price if sl_price is not None else market_params.get("sl_price")
                if target_sl is not None:
                    try:
                        sl_f = float(target_sl)
                        if sl_f > 0:
                            r_dist = abs(entry_price - sl_f)
                            tp1_r_mult = float(market_params.get("tp1_r_mult") or 1.0)
                            target_r_dist = tp1_r_mult * r_dist
                            # F-08 Fix: Allow TP1 @ 0.75R-1.0R to activate even on moderate R:R setups (BALANCED 1.73R, MEAN_REVERSION 1.2R)
                            if 0 < target_r_dist < (0.85 * delta_tp):
                                t1_target = cls.ceil_to_tick(entry_price + target_r_dist, price_inc) if dir_clean == "LONG" else cls.floor_to_tick(entry_price - target_r_dist, price_inc)
                    except (ValueError, TypeError):
                        pass

                tiers = [
                    TPTier(
                        tier_index=1,
                        fraction=0.50,
                        target_price=t1_target,
                        close_contracts=q2,
                        cumulative_fraction=0.50,
                        is_final=False,
                        action="PROTECT_BREAKEVEN"
                    ),
                    TPTier(
                        tier_index=2,
                        fraction=1.00,
                        target_price=cls.round_to_tick(tp_final_price, price_inc),
                        close_contracts=rem2,
                        cumulative_fraction=1.00,
                        is_final=True,
                        action="CLOSE_ALL"
                    )
                ]
                mode = "2_TIER"
            else:
                # Fallback to SINGLE_TIER if position volume too small for 2 tiers
                tiers = [
                    TPTier(
                        tier_index=1,
                        fraction=1.00,
                        target_price=cls.round_to_tick(tp_final_price, price_inc),
                        close_contracts=total_contracts,
                        cumulative_fraction=1.00,
                        is_final=True,
                        action="CLOSE_ALL"
                    )
                ]
                mode = "SINGLE_TIER"

        elif preferred_mode == "SINGLE_TIER":
            tiers = [
                TPTier(
                    tier_index=1,
                    fraction=1.00,
                    target_price=cls.round_to_tick(tp_final_price, price_inc),
                    close_contracts=total_contracts,
                    cumulative_fraction=1.00,
                    is_final=True,
                    action="CLOSE_ALL"
                )
            ]
            mode = "SINGLE_TIER"

        else: # "4_TIER"
            # 1. Test 4-tier candidate
            q4 = cls.floor_to_step(0.25 * total_contracts, size_inc)
            rem4 = cls.floor_to_step(total_contracts - (3 * q4), size_inc)
            worst_price = min(entry_price, tp_final_price)
            notional_q4 = q4 * worst_price
            notional_rem4 = rem4 * worst_price
            effective_min_notional = min_notional * 1.05 if min_notional > 0 else 0.0

            can_do_4_tier = (
                q4 >= min_size and
                rem4 >= min_size and
                q4 > 0 and
                rem4 > 0 and
                (effective_min_notional <= 0 or (notional_q4 >= effective_min_notional and notional_rem4 >= effective_min_notional))
            )

            if can_do_4_tier:
                tiers = [
                    TPTier(
                        tier_index=1,
                        fraction=0.25,
                        target_price=get_price_at(0.25),
                        close_contracts=q4,
                        cumulative_fraction=0.25,
                        is_final=False,
                        action=None
                    ),
                    TPTier(
                        tier_index=2,
                        fraction=0.50,
                        target_price=get_price_at(0.50),
                        close_contracts=q4,
                        cumulative_fraction=0.50,
                        is_final=False,
                        action="PROTECT_BREAKEVEN"
                    ),
                    TPTier(
                        tier_index=3,
                        fraction=0.75,
                        target_price=get_price_at(0.75),
                        close_contracts=q4,
                        cumulative_fraction=0.75,
                        is_final=False,
                        action="START_TRAILING"
                    ),
                    TPTier(
                        tier_index=4,
                        fraction=1.00,
                        target_price=cls.round_to_tick(tp_final_price, price_inc),
                        close_contracts=rem4,
                        cumulative_fraction=1.00,
                        is_final=True,
                        action="CLOSE_ALL"
                    )
                ]
                mode = "4_TIER"
            else:
                # 2. Test 2-tier candidate
                q2 = cls.floor_to_step(0.50 * total_contracts, size_inc)
                rem2 = cls.floor_to_step(total_contracts - q2, size_inc)
                notional_q2 = q2 * worst_price
                notional_rem2 = rem2 * worst_price

                can_do_2_tier = (
                    q2 >= min_size and
                    rem2 >= min_size and
                    q2 > 0 and
                    rem2 > 0 and
                    (effective_min_notional <= 0 or (notional_q2 >= effective_min_notional and notional_rem2 >= effective_min_notional))
                )

                if can_do_2_tier:
                    tiers = [
                        TPTier(
                            tier_index=1,
                            fraction=0.50,
                            target_price=get_price_at(0.50),
                            close_contracts=q2,
                            cumulative_fraction=0.50,
                            is_final=False,
                            action="PROTECT_BREAKEVEN"
                        ),
                        TPTier(
                            tier_index=2,
                            fraction=1.00,
                            target_price=cls.round_to_tick(tp_final_price, price_inc),
                            close_contracts=rem2,
                            cumulative_fraction=1.00,
                            is_final=True,
                            action="CLOSE_ALL"
                        )
                    ]
                    mode = "2_TIER"
                else:
                    # 3. Fallback: SINGLE_TIER (100% full TP)
                    tiers = [
                        TPTier(
                            tier_index=1,
                            fraction=1.00,
                            target_price=cls.round_to_tick(tp_final_price, price_inc),
                            close_contracts=total_contracts,
                            cumulative_fraction=1.00,
                            is_final=True,
                            action="CLOSE_ALL"
                        )
                    ]
                    mode = "SINGLE_TIER"

        return PartialTPPlan(
            symbol=symbol,
            direction=dir_clean,
            entry_price=entry_price,
            tp_final_price=tp_final_price,
            total_contracts=total_contracts,
            tiers=tiers,
            mode=mode,
            min_size=min_size,
            size_increment=size_inc,
            price_increment=price_inc,
            remaining_contracts=total_contracts,
            created_at=created_at
        )
```
