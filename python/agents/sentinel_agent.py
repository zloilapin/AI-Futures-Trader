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
        else:
            lowest = min(lowest, entry, current_price)

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
        be_atr = profile_rules.get("sentinel_be_atr", getattr(config, "SENTINEL_BE_ATR", 1.0))
        trail_activation = profile_rules.get("sentinel_trail_activation_atr", getattr(config, "SENTINEL_TRAIL_ACTIVATION_ATR", 1.5))
        trail_dist = profile_rules.get("sentinel_trail_distance_atr", getattr(config, "SENTINEL_TRAIL_DISTANCE_ATR", 1.5))
        min_improve = profile_rules.get("sentinel_min_improve_atr", getattr(config, "SENTINEL_MIN_IMPROVE_ATR", 0.25))

        # Dynamic cost buffer (Estimated fees + slippage)
        # Nado Taker fee is typically 0.05% (0.0005). Roundtrip = 0.1% (0.0010).
        fee_pct = profile_rules.get("sentinel_fee_pct", 0.001)
        slippage_pct = profile_rules.get("sentinel_slippage_pct", 0.001)

        symbol = str(pos.get("symbol", "")).upper()
        # For non-major altcoins (neither BTC nor ETH), increase default slippage buffer if not overridden in rules
        if symbol and not ("BTC" in symbol or "ETH" in symbol) and "sentinel_slippage_pct" not in profile_rules:
            slippage_pct = 0.0025  # 0.25% exit slippage protection for volatile altcoins

        cost_buffer = entry * (fee_pct + slippage_pct)

        # Minimum improvement floor: prevents trigger order churn on low-ATR noise
        min_improve_pct = profile_rules.get("sentinel_min_improve_pct", getattr(config, "SENTINEL_MIN_IMPROVE_PCT", 0.0005))
        effective_min_improve = max(min_improve * eval_atr, entry * min_improve_pct)

        candidate_sl = None
        new_state = current_state
        reasoning = ""

        if direction == "LONG":
            profit_distance = current_price - entry

            # 1. Break-Even Check
            if profit_distance >= (be_atr * eval_atr):
                be_target = entry + cost_buffer
                if current_sl < be_target:
                    candidate_sl = be_target
                    new_state = "BREAK_EVEN"
                    reasoning = f"Price passed {be_atr}x ATR. SL moved to Break-Even + cost buffer."

            # 2. Trailing Check
            if profit_distance >= (trail_activation * eval_atr):
                trail_target = highest - (trail_dist * eval_atr)
                if candidate_sl is None or trail_target > candidate_sl:
                    candidate_sl = trail_target
                    new_state = "TRAILING"
                    reasoning = f"Price passed {trail_activation}x ATR. SL trailed {trail_dist}x ATR from highest price."

            # Enforce Monotonic SL (Never worsen)
            if candidate_sl is not None:
                new_sl = max(current_sl, candidate_sl) if current_sl > 0 else candidate_sl
                # Check minimum improvement threshold
                if current_sl == 0 or (new_sl - current_sl) >= effective_min_improve:
                    return {"new_sl": new_sl, "state": new_state, "reasoning": reasoning}

        else: # SHORT
            profit_distance = entry - current_price

            # 1. Break-Even Check
            if profit_distance >= (be_atr * eval_atr):
                be_target = entry - cost_buffer
                if current_sl > be_target or current_sl == 0:
                    candidate_sl = be_target
                    new_state = "BREAK_EVEN"
                    reasoning = f"Price passed {be_atr}x ATR. SL moved to Break-Even + cost buffer."

            # 2. Trailing Check
            if profit_distance >= (trail_activation * eval_atr):
                trail_target = lowest + (trail_dist * eval_atr)
                if candidate_sl is None or trail_target < candidate_sl:
                    candidate_sl = trail_target
                    new_state = "TRAILING"
                    reasoning = f"Price passed {trail_activation}x ATR. SL trailed {trail_dist}x ATR from lowest price."

            # Enforce Monotonic SL (Never worsen)
            if candidate_sl is not None:
                new_sl = min(current_sl, candidate_sl) if current_sl > 0 else candidate_sl
                # Check minimum improvement threshold
                if current_sl == 0 or (current_sl - new_sl) >= effective_min_improve:
                    return {"new_sl": new_sl, "state": new_state, "reasoning": reasoning}

        return {"new_sl": None, "state": current_state, "reasoning": "No update required."}
