import os
import json
import math
from typing import Dict, Any, Optional, Tuple, List

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient
from core.config import config

def _extract_cand_field(cand: Any, key: str, default: Any = None) -> Any:
    """Defensively extracts a field from a dictionary, dataclass, or Pydantic model."""
    if isinstance(cand, dict):
        return cand.get(key, default)
    if hasattr(cand, key):
        v = getattr(cand, key)
        return v if v is not None else default
    if hasattr(cand, "get"):
        return cand.get(key, default)
    return default

def _is_finite_positive(val: Any) -> bool:
    """Returns True iff val is a finite float strictly greater than zero."""
    try:
        f = float(val)
        return math.isfinite(f) and f > 0.0
    except (TypeError, ValueError):
        return False

def _is_finite_non_negative(val: Any) -> bool:
    """Returns True iff val is a finite float greater than or equal to zero."""
    try:
        f = float(val)
        return math.isfinite(f) and f >= 0.0
    except (TypeError, ValueError):
        return False

class RiskManager(BaseAgent):
    """
    Gatekeeper agent responsible for capital preservation, risk profiles, and position sizing.
    Calculates exact Take Profit (TP), Stop Loss (SL), position amount (USD / %), and risk/reward ratio.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient, default_macro_regime: str = "UNKNOWN"):
        super().__init__("Risk_Manager", logger, llm_client)
        self.default_macro_regime = default_macro_regime

    @staticmethod
    def is_risk_free_position(pos: dict) -> bool:
        """Returns True if position has zero open price risk (BE or trailing stop beyond entry)."""
        if not isinstance(pos, dict):
            return False
        state = str(pos.get("protection_state") or pos.get("state") or "").upper()
        if "PENDING" not in state and any(k in state for k in ("BREAK_EVEN", "TRAILING", "TIME_STOP_BE")):
            return True
        plan = pos.get("partial_tp_plan")
        if isinstance(plan, dict) and plan.get("be_activated"):
            return True
        direction = str(pos.get("direction", pos.get("side", ""))).upper()
        try:
            entry = float(pos.get("entry_price") or 0.0)
            sl = float(pos.get("sl_price") or 0.0)
            if entry > 0 and sl > 0:
                if ("LONG" in direction or "BUY" in direction) and sl >= entry:
                    return True
                if ("SHORT" in direction or "SELL" in direction) and sl <= entry:
                    return True
        except (ValueError, TypeError):
            pass
        return False

    def _get_profile_rules(self, profile: str, strategy_mode: str = "TREND_FOLLOWING") -> dict:
        max_concurrent = getattr(config, "MAX_CONCURRENT_POSITIONS", 3)
        if profile == "AGGRESSIVE":
            rules = {
                "min_conviction": 65,
                "base_risk": 0.020,
                "risk_cap": 0.025,
                "portfolio_risk_cap": 0.06,
                "max_daily_drawdown_pct": 0.08, # 8% max daily loss
                "max_concurrent_positions": max_concurrent,
                "sl_mult": 1.75,
                "tp_mult": 3.0,
                "min_rr": 1.20,
                "target_margin_pct": 0.20,
                "max_margin_pct": 0.45,
                "max_leverage": 15,
                "sentinel_be_atr": 1.5,
                "sentinel_trail_atr": 2.5,
                "sentinel_trail_activation_atr": 2.5,
                "sentinel_trail_distance_atr": 2.0,
                "sentinel_min_improve_atr": 0.25
            }
        elif profile == "CONSERVATIVE":
            rules = {
                "min_conviction": 80,
                "base_risk": 0.005,
                "risk_cap": 0.010,
                "portfolio_risk_cap": 0.02,
                "max_daily_drawdown_pct": 0.04, # 4% max daily loss
                "max_concurrent_positions": min(2, max_concurrent),
                "sl_mult": 2.0,
                "tp_mult": 3.0,
                "min_rr": 1.25,
                "target_margin_pct": 0.05,
                "max_margin_pct": 0.10,
                "max_leverage": 5,
                "sentinel_be_atr": 1.2,
                "sentinel_trail_atr": 2.2,
                "sentinel_trail_activation_atr": 2.2,
                "sentinel_trail_distance_atr": 1.8,
                "sentinel_min_improve_atr": 0.30
            }
        else: # BALANCED
            rules = {
                "min_conviction": 70,
                "base_risk": 0.015,
                "risk_cap": 0.020,
                "portfolio_risk_cap": 0.04,
                "max_daily_drawdown_pct": 0.06, # 6% max daily loss
                "max_concurrent_positions": max_concurrent,
                "sl_mult": 1.5,
                "tp_mult": 2.6,
                "min_rr": 1.25,
                "target_margin_pct": 0.10,
                "max_margin_pct": 0.20,
                "max_leverage": 10,
                "sentinel_be_atr": 1.5,
                "sentinel_trail_atr": 2.5,
                "sentinel_trail_activation_atr": 2.5,
                "sentinel_trail_distance_atr": 2.0,
                "sentinel_min_improve_atr": 0.25
            }
            
        # --- CENTRALIZED STRATEGY ROUTING ---
        if strategy_mode == "VOLATILITY_MOMENTUM":
            rules["min_conviction"] = 65  # Fast/early entries naturally score lower initially
            rules["min_rr"] = 1.00        # Scalp / momentum quick-grab allows 1.00:1 net RR
        elif strategy_mode == "BREAKOUT":
            rules["min_conviction"] = 65  # Fast/early entries naturally score lower initially
            rules["min_rr"] = min(rules.get("min_rr", 1.25), 1.10)
        elif strategy_mode == "RELATIVE_MOMENTUM":
            rules["min_conviction"] = 68
            rules["min_rr"] = min(rules.get("min_rr", 1.25), 1.20)
        elif strategy_mode == "MEAN_REVERSION":
            rules["min_conviction"] = 70
            if getattr(config, "ANTI_BOT_MODE", False) is True:
                # In Anti-Bot Predator Mode, mean reversion scalps have a high win-rate (70-80%)
                # targeting a fast snapback to EMA-20 / BB middle with a safety stop outside the wick.
                # Net RR naturally ranges 0.25 - 0.75:1 while strictly guaranteeing positive net profit after fees.
                rules["min_rr"] = 0.25
            else:
                rules["min_rr"] = 1.00 # Mean reversion requires at least 1:1 reward-to-risk
            
        # Slight penalty for non-trend strategies if the overall macro is highly conservative
        if profile == "CONSERVATIVE" and strategy_mode != "TREND_FOLLOWING":
            rules["min_conviction"] = min(90, rules["min_conviction"] + 5)
            
        return rules

    def _get_conviction_multiplier(self, min_conviction: int, conviction: int) -> float:
        """
        Flat-Protected Conviction Scaling:
        Prevents Adverse Sizing Selection (entering losers at 1.25x and winners at 0.60x).
        - Below threshold    -> 0.00x (veto)
        - Boundary (+0..4%)  -> 0.90x (slight caution)
        - Standard (+5%+)    -> 1.00x (strict 1.0R cap, never over-leverage on LLM euphoria)
        """
        if conviction < min_conviction:
            return 0.0
        delta = conviction - min_conviction
        if delta < 5:
            return 0.90
        return 1.00

    def _calculate_drawdown_multiplier(
        self,
        streak: list,
        daily_drawdown_pct: float = 0.0,
        current_balance: float = 0.0,
        peak_balance: float = 0.0
    ) -> tuple[float, str]:
        """
        Two-Factor Drawdown Recovery & Sizing Multiplier:
        1. Equity Drawdown from High Water Mark (or daily_drawdown_pct):
           - < 2.0%: 1.00x (routine noise / 1-2 standard stops)
           - < 4.0%: 0.85x (mild drawdown)
           - < 6.0%: 0.70x (moderate drawdown)
           - < 10.0%: 0.45x (elevated drawdown)
           - >= 10.0%: 0.25x (critical drawdown -> PENALTY_SUPPRESSION)
        2. Consecutive Loss Streak State Machine:
           - 0 losses: 1.00x
           - 1 loss: 0.90x (routine single stop: -10% from base risk)
           - 2 losses: 0.50x (drawdown protection, prevents double margin penalty)
           - 3 losses: 0.40x (red alert: allows top setups with Signal>=1.0, suppresses degraded)
           - 4 losses: 0.35x (deep drawdown: allows only Signal>=1.0)
           - >= 5 losses: 0.25x (hard stop-loss circuit breaker -> PENALTY_SUPPRESSION)
        3. Recovery Transitions:
           - Probation loss after recovery win following 3-loss streak: 0.60x (not 0.25x deadlock)
           - Probation win after 3-loss streak: 0.75x
        Effective multiplier is min(dd_from_equity, dd_from_streak).
        """
        if peak_balance > 0 and current_balance > 0 and current_balance < peak_balance:
            dd_pct = ((peak_balance - current_balance) / peak_balance) * 100.0
        else:
            dd_val = float(daily_drawdown_pct or 0.0)
            dd_pct = dd_val if dd_val > 1.0 else max(0.0, dd_val * 100.0)

        # 1. Equity Drawdown factor
        if dd_pct < 2.0:
            dd_from_equity = 1.00
            equity_msg = f"EQUITY_NORMAL: Drawdown {dd_pct:.2f}% (<2.0%)."
        elif dd_pct < 4.0:
            dd_from_equity = 0.85
            equity_msg = f"EQUITY_MILD: Drawdown {dd_pct:.2f}% (2.0-4.0%)."
        elif dd_pct < 6.0:
            dd_from_equity = 0.70
            equity_msg = f"EQUITY_MODERATE: Drawdown {dd_pct:.2f}% (4.0-6.0%)."
        elif dd_pct < 10.0:
            dd_from_equity = 0.45
            equity_msg = f"EQUITY_ELEVATED: Drawdown {dd_pct:.2f}% (6.0-10.0%)."
        else:
            dd_from_equity = 0.25
            equity_msg = f"EQUITY_CRITICAL: Drawdown {dd_pct:.2f}% (>=10.0%)."

        # 2. Consecutive Losses factor
        if not streak:
            dd_from_streak = 1.00
            streak_msg = "STREAK_NORMAL"
        else:
            consecutive_losses = 0
            for res in reversed(streak):
                if res == "LOSS":
                    consecutive_losses += 1
                else:
                    break

            if consecutive_losses >= 5:
                dd_from_streak = 0.25
                streak_msg = "🚨 CRITICAL STREAK: 5+ losses in a row. Risk cut to 25% (PENALTY_SUPPRESSION active)."
            elif consecutive_losses == 4:
                dd_from_streak = 0.35
                streak_msg = "DEEP DRAWDOWN: 4 losses in a row. Risk cut to 35%."
            elif consecutive_losses == 3:
                dd_from_streak = 0.40
                streak_msg = "🚨 ELEVATED DRAWDOWN: 3 losses in a row. Risk cut to 40%."
            elif consecutive_losses == 2:
                dd_from_streak = 0.50
                streak_msg = "DRAWDOWN PROTECTION: 2 losses in a row. Risk cut to 50%."
            elif consecutive_losses == 1:
                # Check recovery probation state machine
                if len(streak) >= 4 and any(streak[i:i+2] == ["LOSS", "LOSS"] for i in range(max(0, len(streak)-5), len(streak)-2)):
                    dd_from_streak = 0.60
                    streak_msg = "PROBATION: Mild loss during recovery from drawdown. Sizing kept at 60%."
                else:
                    dd_from_streak = 0.90
                    streak_msg = "MILD_DRAWDOWN: 1 loss. Sizing at 90%."
            else:
                # Last was WIN: check recent history for smooth step-up recovery transitions
                consecutive_wins = 0
                for res in reversed(streak):
                    if res == "WIN":
                        consecutive_wins += 1
                    else:
                        break

                preceding_losses = 0
                idx_before_wins = len(streak) - consecutive_wins - 1
                while idx_before_wins >= 0 and streak[idx_before_wins] == "LOSS":
                    preceding_losses += 1
                    idx_before_wins -= 1

                if preceding_losses >= 2:
                    if consecutive_wins == 1:
                        dd_from_streak = 0.75
                        streak_msg = f"PROBATION RECOVERY TIER 1: 1 win after {preceding_losses} losses. Sizing smoothed to 75%."
                    elif consecutive_wins == 2:
                        dd_from_streak = 0.85
                        streak_msg = f"PROBATION RECOVERY TIER 2: 2 wins after {preceding_losses} losses. Sizing smoothed to 85%."
                    else:
                        dd_from_streak = 1.00
                        streak_msg = "STREAK_NORMAL: Full recovery achieved."
                else:
                    dd_from_streak = 1.00
                    streak_msg = "STREAK_NORMAL"

        final_dd_mult = round(min(dd_from_equity, dd_from_streak), 2)
        final_msg = f"{streak_msg} | {equity_msg} (Final Mult: {final_dd_mult:.2f}x)"
        return final_dd_mult, final_msg

    def is_pre_debate_viable(self, portfolio_data: Dict[str, Any], profile_name: str = "BALANCED") -> tuple[bool, str]:
        """
        Pre-Debate Risk Viability Check:
        Quickly evaluates if a trade is mathematically capable of passing the Risk Manager
        under current portfolio health (drawdown, streak, kelly) BEFORE calling expensive LLM debates.
        Returns: (is_viable, reason)
        """
        import time
        cooldown_until = float(portfolio_data.get("cooldown_until") or 0.0)
        if cooldown_until > time.time():
            remaining_min = (cooldown_until - time.time()) / 60.0
            return False, f"Portfolio in post-streak cooldown for {remaining_min:.1f} more minutes."

        total_balance = float(
            portfolio_data.get("total_usd") or
            portfolio_data.get("total_balance") or
            portfolio_data.get("equity") or
            portfolio_data.get("current_balance") or
            portfolio_data.get("initial_balance") or
            100.0
        )
        peak_balance = float(portfolio_data.get("peak_balance") or portfolio_data.get("initial_balance") or total_balance)
        raw_daily_dd = float(portfolio_data.get("daily_drawdown_pct") or 0.0)
        daily_drawdown_pct = raw_daily_dd / 100.0 if raw_daily_dd > 1.0 else raw_daily_dd
        recent_streak = portfolio_data.get("recent_streak", [])

        effective_account_dd = max(
            daily_drawdown_pct * 100.0,
            ((peak_balance - total_balance) / peak_balance * 100.0) if peak_balance > total_balance > 0 else 0.0
        )

        dd_mult, _ = self._calculate_drawdown_multiplier(
            recent_streak, daily_drawdown_pct, current_balance=total_balance, peak_balance=peak_balance
        )

        total_trades = int(portfolio_data.get("total_trades", 0))
        raw_win_rate = float(portfolio_data.get("win_rate_pct", 50.0))
        win_rate = raw_win_rate / 100.0 if raw_win_rate > 1.0 else raw_win_rate
        hist_payoff = float(portfolio_data.get("historical_payoff_ratio", 0.0) or portfolio_data.get("payoff_ratio", 1.5) or 1.0)
        kelly_floor = float(portfolio_data.get("min_kelly_mult", 0.50) or 0.50)
        raw_kelly = self._calculate_kelly_multiplier(win_rate, hist_payoff, total_trades) if total_trades >= 15 else 1.0
        kelly_mult = max(kelly_floor, raw_kelly)

        portfolio_mult = dd_mult * kelly_mult

        profile_rules = self._get_profile_rules(profile_name)
        base_risk = profile_rules.get("base_risk", 0.015)
        min_viable_risk = base_risk * 0.25

        # With realistic market friction (spread and taker fee), projected market mult is <= 0.90
        projected_market_mult = 0.90
        max_projected_risk = base_risk * 1.0 * projected_market_mult * portfolio_mult

        if max_projected_risk < min_viable_risk and effective_account_dd >= 12.0:
            return False, (
                f"Portfolio drawdown ({effective_account_dd:.2f}% >= 12.0%) and streak/kelly multiplier ({portfolio_mult:.2f}x) "
                f"mathematically suppress dynamic risk ({max_projected_risk*100:.3f}%) below min viable risk ({min_viable_risk*100:.3f}%). "
                f"High-Conviction Floor inactive."
            )

        return True, "Viable"

    def _calculate_kelly_multiplier(
        self,
        win_rate: float,
        payoff_ratio: float = 0.0,
        total_trades: int = 0,
        realized_rr: Optional[float] = None
    ) -> float:
        """
        Calculates Fractional (Half) Kelly sizing multiplier based on HISTORICAL track record.
        f* = (p * b - (1 - p)) / b
        where:
          p = historical win rate (0.0 to 1.0)
          b = historical payoff ratio (average win / average loss)
        Clamped to [0.50, 1.00] as asymmetric downside damping only.
        Does NOT boost above 1.00 to prevent overleveraging on short-term sample clustering.
        Only activated when total_trades >= 15 and b > 0 and finite.
        """
        try:
            p = float(win_rate)
            if not math.isfinite(p):
                return 1.0
            p = max(0.0, min(1.0, p))
            
            raw_b = payoff_ratio if payoff_ratio > 0 else (realized_rr or 0.0)
            b = float(raw_b)
            if not math.isfinite(b) or b <= 0.0:
                return 1.0
        except (ValueError, TypeError):
            return 1.0

        if total_trades < 15 or p <= 0:
            return 1.0
        
        q = 1.0 - p
        f_star = (p * b - q) / b
        
        # Asymmetric downside damping with continuous calibration:
        # Instead of a discrete binary step jump between 0.50 and 1.00 at f* = 0,
        # linearly interpolate marginal edges (0 < f* < 0.25) smoothly between 0.50 and 1.00.
        if f_star <= 0:
            return 0.50
        elif f_star >= 0.25:
            return 1.00
        else:
            return round(0.50 + (f_star / 0.25) * 0.50, 4)

    @classmethod
    def compute_strategy_regime_stats_from_journal(
        cls, journal_records: list, decay: float = 0.97
    ) -> Dict[str, Dict[str, float]]:
        """
        Computes win/loss statistics grouped by '{regime}:{strategy_tag}' from historical trade journal
        using exponential memory decay (Discounted Beta-Bernoulli) to prevent sample inertia and adapt
        to regime shifts.

        Decay factor gamma (default: 0.97) gives higher weight to recent trades:
          - Effective sample size N_eff = 1 / (1 - gamma) ~= 33 trades per regime bucket.
          - decay=1.0 can be passed to disable discounting (for static analysis/unit tests).

        Win (R > +0.15): alpha increment = 1.0 + min(1.0, max(0.0, r / 2.0)) (bonus for outsized winners)
        Loss (R < -0.22): beta increment = 1.0
        Scratch (-0.22 <= R <= +0.15): neutral (no alpha/beta increment, temporal decay applied)
        """
        stats: Dict[str, Dict[str, float]] = {}
        if not isinstance(journal_records, list):
            return stats

        for trade in journal_records:
            if not isinstance(trade, dict):
                continue
            regime = str(trade.get("macro_regime") or trade.get("regime") or "UNKNOWN").upper()
            strategy = str(trade.get("strategy_mode") or trade.get("strategy_tag") or "UNKNOWN").upper()
            if regime == "UNKNOWN" and strategy == "UNKNOWN":
                continue

            key = f"{regime}:{strategy}"
            if key not in stats:
                stats[key] = {"wins": 0.0, "losses": 0.0, "total": 0}

            # Exponential decay on historical pseudo-counts
            if 0.0 < decay < 1.0:
                stats[key]["wins"] *= decay
                stats[key]["losses"] *= decay

            r = float(trade.get("realized_r", 0.0) or 0.0)
            stats[key]["total"] += 1
            if r > 0.15:
                win_pts = 1.0 + min(1.0, max(0.0, r / 2.0))
                stats[key]["wins"] += win_pts
            elif r < -0.22:
                stats[key]["losses"] += 1.0

        return stats

    def get_bayesian_strategy_multiplier(
        self, strategy_tag: str, regime: str, streak_history_by_tag: Optional[Dict[str, Any]] = None
    ) -> Tuple[float, float, str]:
        """
        Computes the Bayesian confidence multiplier for a given strategy and market regime
        using a conjugate Beta-Bernoulli prior Beta(alpha, beta).
        Prior: alpha=4.0, beta=4.0 -> E[p] = 0.50 (neutral 50% baseline).
        Returns: (multiplier [0.65x .. 1.15x], posterior_win_prob, info_str)
        """
        key = f"{regime}:{strategy_tag}"
        stats = (streak_history_by_tag or {}).get(key, {"wins": 0.0, "losses": 0.0, "total": 0})
        wins = float(stats.get("wins", 0.0) or 0.0)
        losses = float(stats.get("losses", 0.0) or 0.0)

        # Conjugate Beta prior: Beta(4, 4)
        alpha = 4.0 + wins
        beta = 4.0 + losses
        posterior_win_prob = alpha / (alpha + beta)

        # Scale multiplier in safe boundary [0.65x .. 1.15x]
        raw_mult = 1.0 + (posterior_win_prob - 0.50) * 1.5
        bayes_mult = round(max(0.65, min(1.15, raw_mult)), 3)
        info_str = f"Beta({alpha:.1f}, {beta:.1f}) -> p={posterior_win_prob:.2f} [Bucket: {key}]"
        return bayes_mult, round(posterior_win_prob, 4), info_str

    def _build_veto_result(self, veto_category: str, reason: str, trade_action: str = "HOLD") -> Dict[str, Any]:
        """Uniform dictionary constructor for rejected / vetoed trade candidates."""
        return {
            "approved": False,
            "veto_category": veto_category,
            "trade_action": trade_action,
            "reasoning": f"❌ ОТКЛОНЕНО ({veto_category}): {reason}",
            "risk_amount_usd": 0.0,
            "notional_size_usd": 0.0,
            "margin_usd": 0.0,
            "leverage": 1.0,
            "effective_leverage": 1.0,
            "contracts": 0.0,
            "margin_pct": 0.0,
            "entry_price": 0.0,
            "take_profit_price": 0.0,
            "take_profit_pct": 0.0,
            "stop_loss_price": 0.0,
            "stop_loss_pct": 0.0,
            "risk_reward_ratio": 0.0,
            "liquidation_price": 0.0,
            "position_size_pct": 0.0
        }

    async def analyze(self, ceo_decision: Dict[str, Any], portfolio_data: Dict[str, Any], market_data: Dict[str, Any], effective_profile: str = "BALANCED", macro_regime: Optional[str] = None, strategy_mode: str = "TREND_FOLLOWING") -> Dict[str, Any]:
        """
        Deterministic risk engine. All sizing is math-only, no LLM.
        
        Canonical field definitions:
            risk_amount_usd  — max USD we're willing to LOSE on this trade (balance * risk_pct)
            notional_usd     — total exposure = contracts * entry_price (e.g. $500 at 10x = $50 margin)
            margin_usd       — collateral locked = notional_usd / leverage
            leverage         — multiplier from config
            contracts        — base asset amount = notional_usd / entry_price
            margin_pct       — margin_usd as % of total_balance
        """
        # Defensive input guards
        if not ceo_decision:
            return self._build_veto_result("NO_SIGNAL", "Missing or empty trade proposal.", "HOLD")
            
        if market_data is None:
            market_data = {}
        if portfolio_data is None:
            portfolio_data = {}

        profile_rules = self._get_profile_rules(effective_profile, strategy_mode)
        min_conviction = profile_rules["min_conviction"]
        base_risk = profile_rules["base_risk"]
        risk_cap = profile_rules["risk_cap"]
        sl_mult = profile_rules["sl_mult"]
        tp_mult = profile_rules["tp_mult"]
        max_margin_pct = profile_rules["max_margin_pct"]
        min_rr_required = profile_rules.get("min_rr", 1.25)
        
        profile_name = effective_profile
        
        # Strategy mode SL/TP overrides
        if strategy_mode == "VOLATILITY_MOMENTUM":
            sl_mult *= 0.5       # Cut stop loss distance in half (tight stop)
            tp_mult *= 0.5       # Cut take profit distance in half (quick grab)
            self.logger.info(f"[{self.name}] 🔥 VOLATILITY_MOMENTUM MODE ACTIVATED: Tightening SL/TP.")
            profile_name += "_VOL_MOMENTUM"
        elif strategy_mode == "MEAN_REVERSION":
            if getattr(config, "ANTI_BOT_MODE", False) is True:
                sl_mult = 1.25
                tp_mult = 2.2
                min_rr_required = min(min_rr_required, 0.25)
                self.logger.info(f"[{self.name}] 🦈 ANTI_BOT MEAN_REVERSION MODE: sl_mult=1.25, tp_mult=2.2, min_rr={min_rr_required}")
                profile_name += "_ANTI_BOT_MR"
            else:
                sl_mult = 1.0        # Tight stop just beyond extreme band
                tp_mult = 1.2        # Reversion multiplier
                self.logger.info(f"[{self.name}] 🔄 MEAN_REVERSION MODE ACTIVATED: Targeting mean reversion to EMA-20.")
                profile_name += "_MEAN_REVERSION"
        
        self.logger.info(f"[{self.name}] Расчет математики риска по профилю: {profile_name} (Base Risk: {base_risk*100}%)...")
        
        decision = _extract_cand_field(ceo_decision, "decision", "HOLD")
        conviction = int(_extract_cand_field(ceo_decision, "conviction", 0) or 0)
        trade_action = _extract_cand_field(ceo_decision, "trade_action", "ENTER")
        symbol = _extract_cand_field(ceo_decision, "symbol", "")
        
        price_data = market_data.get("price_data", {})
        if not isinstance(price_data, dict):
            price_data = {}
        raw_price = price_data.get("current_price", 0.0)
        
        indicators = market_data.get("indicators", {})
        if not isinstance(indicators, dict):
            indicators = {}
        raw_atr = indicators.get("atr_14", 0.0)
        
        raw_total_balance = portfolio_data.get("total_usd") or portfolio_data.get("total_balance") or portfolio_data.get("equity") or portfolio_data.get("current_balance", 0.0)
        raw_avail_margin = portfolio_data.get("available_margin", raw_total_balance)
        
        # ═══ Stage 1: Deterministic Guard & Early Numeric Sanitization (P2) ═══
        if hasattr(ceo_decision, "is_actionable") and not getattr(ceo_decision, "is_actionable", True):
            veto_cat = getattr(ceo_decision, "rejection_tag", None) or "GUARD_VETO"
            veto_reason = getattr(ceo_decision, "guard_reason", "") or f"Blocked by Deterministic Guard: {veto_cat}"
            self.logger.warning(f"[{self.name}] 🚫 DETERMINISTIC GUARD VETO: {veto_reason}")
            return self._build_veto_result(veto_cat, veto_reason, trade_action)
            
        if not _is_finite_positive(raw_price):
            self.logger.warning(f"[{self.name}] ❌ INVALID PRICE: current_price is {raw_price}. Blocking trade.")
            return self._build_veto_result("INVALID_PRICE", f"Missing, non-positive or non-finite current price ({raw_price}).", trade_action)
        current_price = float(raw_price)
            
        if not _is_finite_positive(raw_atr):
            self.logger.warning(f"[{self.name}] ❌ INVALID ATR: atr_14 is {raw_atr}. Blocking trade to prevent corrupted risk sizing.")
            return self._build_veto_result("INVALID_ATR", f"Missing, non-positive or non-finite ATR_14 data ({raw_atr}).", trade_action)
        atr_14 = float(raw_atr)
            
        if not _is_finite_positive(raw_total_balance):
            self.logger.warning(f"[{self.name}] ❌ INSUFFICIENT BALANCE: Total balance is {raw_total_balance}. Blocking trade.")
            return self._build_veto_result("INSUFFICIENT_BALANCE", f"Total balance is non-positive or non-finite (${raw_total_balance}).", trade_action)
        total_balance = float(raw_total_balance)
            
        if not _is_finite_non_negative(raw_avail_margin):
            self.logger.warning(f"[{self.name}] ❌ INVALID MARGIN: Available margin is {raw_avail_margin}. Blocking trade.")
            return self._build_veto_result("INSUFFICIENT_BALANCE", f"Available margin is non-finite or invalid ({raw_avail_margin}).", trade_action)
        available_margin = float(raw_avail_margin)
        
        max_leverage = profile_rules.get("max_leverage", 10.0)
        # Check if Deterministic Guard flagged exhaustion scaling or leverage override
        is_exhaustion_scale = bool(_extract_cand_field(ceo_decision, "exhaustion_scale", False))
        max_leverage_override = _extract_cand_field(ceo_decision, "max_leverage_override")
        if max_leverage_override is not None:
            try:
                max_leverage = min(float(max_leverage), float(max_leverage_override))
            except (ValueError, TypeError):
                pass
        elif is_exhaustion_scale:
            max_leverage = min(float(max_leverage), 4.0)
        max_notional_usd = total_balance * max_leverage

        if trade_action == "WAIT_FOR_PULLBACK":
            msg = "CEO recommended WAIT_FOR_PULLBACK (Overheated market condition). Blocking trade execution."
            self.logger.warning(f"[{self.name}] 🚫 WAIT_FOR_PULLBACK VETO: {msg}")
            return self._build_veto_result("WAIT_FOR_PULLBACK", msg, "WAIT_FOR_PULLBACK")
        elif decision not in ["LONG", "SHORT"]:
            return self._build_veto_result("NO_SIGNAL", f"Decision is {decision}", "HOLD")
        elif conviction < min_conviction:
            return self._build_veto_result("LOW_CONFIDENCE", f"Conviction {conviction} is below profile threshold {min_conviction}", trade_action)

        # Gate 1.5: Macro Regime Integrity Veto (Fail-Closed Independent Risk Layer)
        # If macro regime is unconfirmed, corrupt, or telemetry is lost (UNKNOWN or DATA_INSUFFICIENT),
        # strictly veto opening of ANY new positions regardless of candidate quality or available margin.
        target_regime = macro_regime if macro_regime is not None else getattr(self, "default_macro_regime", "UNKNOWN")
        norm_macro = str(target_regime or "").strip().upper()
        if not norm_macro or norm_macro in ["UNKNOWN", "DATA_INSUFFICIENT", "NONE"]:
            msg = f"New position entry strictly blocked: Macro market regime is {norm_macro or 'EMPTY'} (telemetry unconfirmed)."
            self.logger.warning(f"[{self.name}] 🚫 UNKNOWN_MACRO_REGIME VETO: {msg}")
            return self._build_veto_result("UNKNOWN_MACRO_REGIME", msg, trade_action)

        # ═══ Stage 2: Portfolio Health & Defense-in-Depth Gates ═══

        # Gate 2.1: Max Daily Drawdown & Subaccount Health Veto Gate (P1 Finding 2 & P2 Finding 9)
        # 1. Check Subaccount Health (Nado Protocol Health model)
        maint_health_usd = portfolio_data.get("maintenance_health_usd")
        if maint_health_usd is not None:
            try:
                m_health = float(maint_health_usd)
                if not math.isfinite(m_health) or m_health < 0.0:
                    msg = f"Subaccount maintenance margin health is critical or non-finite ({maint_health_usd}). Liquidations imminent. Halting new trades."
                    self.logger.critical(f"[{self.name}] 🚨 SUBACCOUNT_HEALTH_CRITICAL VETO: {msg}")
                    return self._build_veto_result("SUBACCOUNT_HEALTH_CRITICAL", msg, trade_action)
            except (ValueError, TypeError):
                msg = f"Subaccount maintenance margin health is unparseable ({maint_health_usd!r}). Halting new trades."
                self.logger.critical(f"[{self.name}] 🚨 SUBACCOUNT_HEALTH_CRITICAL VETO: {msg}")
                return self._build_veto_result("SUBACCOUNT_HEALTH_CRITICAL", msg, trade_action)
        elif portfolio_data.get("strict_portfolio_health", False):
            msg = "Subaccount maintenance health is missing in strict health mode. Blocking trades."
            self.logger.warning(f"[{self.name}] 🚫 MISSING_PORTFOLIO_HEALTH VETO: {msg}")
            return self._build_veto_result("MISSING_PORTFOLIO_HEALTH", msg, trade_action)

        max_daily_dd = profile_rules.get("max_daily_drawdown_pct", 0.06)
        raw_dd = portfolio_data.get("daily_drawdown_pct")
        raw_pnl_pct = portfolio_data.get("daily_pnl_pct")
        raw_pnl_usd = portfolio_data.get("daily_pnl_usd")

        if raw_dd is None and raw_pnl_pct is None and raw_pnl_usd is None:
            if portfolio_data.get("strict_portfolio_health", False):
                msg = "Portfolio drawdown data is completely missing in strict health mode. Blocking trades."
                self.logger.warning(f"[{self.name}] 🚫 MISSING_PORTFOLIO_HEALTH VETO: {msg}")
                return self._build_veto_result("MISSING_PORTFOLIO_HEALTH", msg, trade_action)
            daily_drawdown_pct = 0.0
        else:
            if raw_dd is not None:
                dd_val = float(raw_dd or 0.0)
                daily_drawdown_pct = dd_val / 100.0 if dd_val > 1.0 else dd_val
            else:
                daily_drawdown_pct = 0.0

            # Derive implied drawdown from daily_pnl_usd (ground truth) and daily_pnl_pct
            implied_dd = 0.0
            if raw_pnl_usd is not None and total_balance > 0:
                pnl_usd = float(raw_pnl_usd or 0.0)
                implied_dd = max(implied_dd, max(0.0, -pnl_usd / total_balance) if pnl_usd < 0 else 0.0)
            if raw_pnl_pct is not None:
                pnl_val = float(raw_pnl_pct or 0.0)
                # daily_pnl_pct is always on 0-100% scale (e.g. -0.32% or -7.0%).
                # Standard conversion to decimal fraction: pnl_val / 100.0
                pnl_pct_dec = pnl_val / 100.0
                implied_dd = max(implied_dd, max(0.0, -pnl_pct_dec) if pnl_pct_dec < 0 else 0.0)

            # Contradiction guard: if drawdown reports 0 (or low) but PnL reflects significant loss,
            # always adopt the conservative larger drawdown
            if implied_dd > daily_drawdown_pct:
                if (implied_dd - daily_drawdown_pct) > 0.02 and raw_dd is not None:
                    self.logger.warning(
                        f"[{self.name}] ⚠️ Drawdown discrepancy detected: reported {daily_drawdown_pct*100:.2f}%, "
                        f"but PnL implies {implied_dd*100:.2f}%. Enforcing conservative implied drawdown."
                    )
                daily_drawdown_pct = implied_dd

        if not _is_finite_non_negative(daily_drawdown_pct):
            msg = f"Daily drawdown data is corrupted or non-finite ({daily_drawdown_pct}). Blocking trades."
            self.logger.warning(f"[{self.name}] 🚫 PORTFOLIO_HEALTH_CORRUPTED VETO: {msg}")
            return self._build_veto_result("PORTFOLIO_HEALTH_CORRUPTED", msg, trade_action)

        if daily_drawdown_pct >= max_daily_dd:
            msg = f"Daily drawdown limit reached ({daily_drawdown_pct*100:.2f}% >= {max_daily_dd*100:.2f}% limit). Trading halted to protect capital."
            self.logger.warning(f"[{self.name}] 🚨 MAX_DAILY_DRAWDOWN VETO: {msg}")
            return self._build_veto_result("MAX_DAILY_DRAWDOWN", msg, trade_action)

        # Gate 2.0: Unresolved Risk Breach Incident Veto & Open Position Normalization
        active_positions = portfolio_data.get("active_positions", {})
        if not isinstance(active_positions, (dict, list)):
            active_positions = {}
        raw_pos_items = active_positions.values() if isinstance(active_positions, dict) else active_positions
        
        # Deduplicate positions by unique position signature / canonical footprint
        unique_positions = []
        seen_pos_ids = set()
        seen_pos_canonical = set()
        for pos in raw_pos_items:
            if isinstance(pos, dict):
                p_id = id(pos)
                if p_id in seen_pos_ids:
                    continue
                seen_pos_ids.add(p_id)

                p_sym = str(pos.get("symbol", "")).replace('/', '-').strip().upper()
                p_prod = pos.get("_product_id") or pos.get("product_id")
                p_sub = pos.get("_subaccount") or pos.get("subaccount") or pos.get("sender", "")
                
                raw_s = pos.get("contracts")
                if raw_s is None:
                    raw_s = pos.get("amount", pos.get("base_amount", 0.0))
                try:
                    s_val = abs(float(raw_s))
                except (ValueError, TypeError):
                    s_val = 0.0

                p_entry = round(float(pos.get("entry_price", 0.0) or 0.0), 4)
                p_dir = str(pos.get("direction", pos.get("side", ""))).upper()
                p_sl = round(float(pos.get("sl_price", 0.0) or 0.0), 4)

                explicit_id = pos.get("position_id") or pos.get("id")
                if explicit_id:
                    canon_key = f"ID:{explicit_id}"
                elif p_prod is not None and p_sub:
                    canon_key = f"PROD_SUB:{p_prod}:{p_sub}:{s_val:.4f}:{p_entry}:{p_dir}"
                else:
                    canon_key = f"FP:{p_sym}:{p_sub}:{p_dir}:{s_val:.4f}:{p_entry}:{p_sl}"

                if canon_key in seen_pos_canonical:
                    continue
                seen_pos_canonical.add(canon_key)
                unique_positions.append(pos)

        # Gate 2.0: Unresolved Risk Breach Incident Veto
        for pos in unique_positions:
            if pos.get("protection_state") == "RISK_BREACH_UNRESOLVED" or pos.get("requires_reconciliation"):
                sym_breach = pos.get("symbol", "UNKNOWN")
                msg = f"Critical risk incident unresolved on {sym_breach}! New trades strictly blocked until capital is safe."
                log_fn = getattr(self.logger, "critical", self.logger.error)
                log_fn(f"[{self.name}] 🚨 RISK_BREACH_UNRESOLVED VETO: {msg}")
                return self._build_veto_result("RISK_BREACH_UNRESOLVED", msg, trade_action)

        pending_recs = portfolio_data.get("pending_reconciliations") or {}
        if isinstance(pending_recs, dict):
            for rec_k, rec_v in pending_recs.items():
                if isinstance(rec_v, dict):
                    rec_status = rec_v.get("status")
                    rec_prot = rec_v.get("protection_state")
                    rec_reason = rec_v.get("reason")
                    if (rec_status in ["PENDING_ORDER_RECONCILIATION", "RISK_BREACH_UNRESOLVED"] or
                        rec_prot == "RISK_BREACH_UNRESOLVED" or
                        rec_reason in ["RISK_BREACH", "RISK_BREACH_UNRESOLVED"]):
                        msg = f"Critical risk incident pending reconciliation on {rec_k}! New trades strictly blocked."
                        log_fn = getattr(self.logger, "critical", self.logger.error)
                        log_fn(f"[{self.name}] 🚨 RISK_BREACH_UNRESOLVED VETO: {msg}")
                        return self._build_veto_result("RISK_BREACH_UNRESOLVED", msg, trade_action)

        # Gate 2.2: Maximum Concurrent Positions Veto Gate (Risk-Free Slot Recycling: 3 Risk + 1 Break-Even)
        max_risk_positions = profile_rules.get("max_concurrent_positions", getattr(config, "MAX_CONCURRENT_POSITIONS", 3))
        risk_positions = [p for p in unique_positions if not self.is_risk_free_position(p)]
        be_positions = [p for p in unique_positions if self.is_risk_free_position(p)]
        max_total_positions = getattr(config, "MAX_ACCOUNT_POSITIONS", 4) if len(be_positions) > 0 else max_risk_positions

        if len(unique_positions) >= max_total_positions:
            msg = f"Maximum concurrent positions reached ({len(unique_positions)} >= {max_total_positions} limit, BE: {len(be_positions)})."
            self.logger.warning(f"[{self.name}] 🚫 MAX_CONCURRENT_POSITIONS VETO: {msg}")
            return self._build_veto_result("MAX_CONCURRENT_POSITIONS", msg, trade_action)

        if len(risk_positions) >= max_risk_positions:
            msg = f"Maximum concurrent positions reached with active risk ({len(risk_positions)} >= {max_risk_positions} limit, BE: {len(be_positions)})."
            self.logger.warning(f"[{self.name}] 🚫 MAX_CONCURRENT_POSITIONS VETO: {msg}")
            return self._build_veto_result("MAX_CONCURRENT_POSITIONS", msg, trade_action)

        # Gate 2.2b: Directional Cluster Cap (Risk-Free Slot Recycling: 2 Risk + 1 Break-Even)
        # Prevents opening more than 2 positions with active risk in the same direction.
        # A 3rd directional position is permitted ONLY if at least 1 existing position is in Break-Even.
        same_dir_total = 0
        same_dir_risk = 0
        for p in unique_positions:
            p_dir = str(p.get("direction", p.get("side", ""))).upper()
            if (decision == "LONG" and ("LONG" in p_dir or "BUY" in p_dir)) or \
               (decision == "SHORT" and ("SHORT" in p_dir or "SELL" in p_dir)):
                same_dir_total += 1
                if not self.is_risk_free_position(p):
                    same_dir_risk += 1

        max_dir_risk = 2
        max_dir_total = 3 if (same_dir_total > same_dir_risk) else 2

        if same_dir_total >= max_dir_total or same_dir_risk >= max_dir_risk:
            msg = (
                f"Directional cluster limit reached: {same_dir_total}/{max_dir_total} active {decision} positions "
                f"({same_dir_risk}/{max_dir_risk} with active risk) in {macro_regime} regime."
            )
            self.logger.warning(f"[{self.name}] 🚫 DIRECTIONAL_CLUSTER_CAP VETO: {msg}")
            return self._build_veto_result("DIRECTIONAL_CLUSTER_CAP", msg, trade_action)

        # Gate 2.2c: 1-Per-Candle Breakout Cluster Cap (Defense-in-depth)
        # Prevents opening multiple breakout market orders in the same direction on the same 15m candle bucket
        if strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"]:
            import time
            now_ts = time.time()
            current_bucket = int(now_ts // 900)
            for p in unique_positions:
                if isinstance(p, dict):
                    p_strat = str(p.get("strategy_mode", "")).upper()
                    p_open = float(p.get("open_time", 0.0) or p.get("timestamp", 0.0) or 0.0)
                    p_dir = str(p.get("direction", p.get("side", ""))).upper()
                    if p_strat in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and int(p_open // 900) == current_bucket:
                        if (decision == "LONG" and ("LONG" in p_dir or "BUY" in p_dir)) or \
                           (decision == "SHORT" and ("SHORT" in p_dir or "SELL" in p_dir)):
                            msg = f"1-Per-Candle Breakout Cap: active {decision} BREAKOUT already opened in candle bucket {current_bucket}."
                            self.logger.warning(f"[{self.name}] ⏸️ BREAKOUT_CANDLE_CLUSTER_CAP VETO: {msg}")
                            return self._build_veto_result("BREAKOUT_CANDLE_CLUSTER_CAP", msg, trade_action)

        # Gate 2.3: Duplicate Asset Exposure Veto Gate
        if symbol:
            clean_sym = symbol.replace('/', '-').strip().upper()
            base_sym = clean_sym.split('-')[0]
            canonical_sym = f"{base_sym}-USD"
            
            if isinstance(active_positions, dict):
                active_keys = set(active_positions.keys())
            elif isinstance(active_positions, list):
                active_keys = {p.get("symbol", "") for p in active_positions if isinstance(p, dict)}
            else:
                active_keys = set()
                
            active_bases = {k.replace('/', '-').split('-')[0].upper() for k in active_keys if k}
            seen_unique_bases = {str(p.get('symbol', '')).replace('/', '-').split('-')[0].upper() for p in unique_positions if p.get('symbol')}
            if base_sym in active_bases or canonical_sym in active_keys or clean_sym in active_keys or base_sym in seen_unique_bases:
                msg = f"Position for {symbol} ({canonical_sym}) is already active. Duplicate entries prohibited."
                self.logger.warning(f"[{self.name}] 🚫 ALREADY_OPEN VETO: {msg}")
                return self._build_veto_result("ALREADY_OPEN", msg, trade_action)

        # Gate 2.4: Total Portfolio Open Risk Budget Cap & Open Positions Validation (P1)
        MAX_TOTAL_PORTFOLIO_RISK_PCT = profile_rules.get("portfolio_risk_cap", 0.03)
        max_portfolio_risk_usd = total_balance * MAX_TOTAL_PORTFOLIO_RISK_PCT
        
        existing_risk_usd = 0.0
        est_exit_fee_rate = float(market_data.get("derivatives_data", {}).get("taker_fee_pct") or 0.0005)
        
        for pos in unique_positions:
            pos_sym = str(pos.get("symbol", "")).replace('/', '-').strip().upper() or "UNKNOWN"
            
            # 1. State integrity check
            if pos.get("requires_reconciliation") or pos.get("protection_state") in ["RISK_BREACH_UNRESOLVED", "DESYNC", "UNKNOWN"]:
                msg = f"Active position {pos_sym} has unconfirmed protection or desynchronized state ({pos.get('protection_state')}). New trades blocked."
                self.logger.critical(f"[{self.name}] 🚨 RISK_BREACH_UNRESOLVED VETO: {msg}")
                return self._build_veto_result("RISK_BREACH_UNRESOLVED", msg, trade_action)
                
            # 2. Extract position size (support both contracts and signed amount)
            raw_size = pos.get("contracts")
            if raw_size is None:
                raw_size = pos.get("amount", pos.get("base_amount", 0.0))
            try:
                f_size = float(raw_size)
                if not math.isfinite(f_size):
                    raise ValueError(f"Non-finite size {raw_size}")
            except (ValueError, TypeError):
                msg = f"Active position {pos_sym} has invalid or non-finite size ({raw_size}). State desynchronized."
                self.logger.warning(f"[{self.name}] 🚫 PORTFOLIO_STATE_DESYNC VETO: {msg}")
                return self._build_veto_result("PORTFOLIO_STATE_DESYNC", msg, trade_action)
                
            size = abs(f_size)
            if size <= 1e-7:
                # Closed or negligible dust position
                continue
                
            # 3. Validate entry price
            raw_entry = pos.get("entry_price", 0.0)
            if not _is_finite_positive(raw_entry):
                msg = f"Active position {pos_sym} has invalid or non-finite entry price ({raw_entry}). State desynchronized."
                self.logger.warning(f"[{self.name}] 🚫 PORTFOLIO_STATE_DESYNC VETO: {msg}")
                return self._build_veto_result("PORTFOLIO_STATE_DESYNC", msg, trade_action)
            entry = float(raw_entry)
            
            # 4. Validate stop-loss: MUST be strictly positive and finite
            raw_sl = pos.get("sl_price", 0.0)
            if not _is_finite_positive(raw_sl):
                msg = f"Active position {pos_sym} has missing, non-positive or non-finite stop-loss ({raw_sl}). Cannot reliably bound open risk."
                self.logger.warning(f"[{self.name}] 🚫 UNBOUNDED_OPEN_POSITION_RISK VETO: {msg}")
                return self._build_veto_result("UNBOUNDED_OPEN_POSITION_RISK", msg, trade_action)
            sl = float(raw_sl)
            
            # 5. Calculate verified risk to stop: price movement loss + round-trip exit fee
            # Directional risk: if trailing stop has moved into profit, capital risk is eliminated
            pos_dir = str(pos.get("direction", pos.get("side", ""))).upper()
            if "SHORT" in pos_dir or f_size < 0:
                is_short = True
            elif "LONG" in pos_dir or f_size > 0:
                is_short = False
            elif sl > entry:
                is_short = True
            elif sl < entry:
                is_short = False
            else:
                is_short = None

            if is_short is True:
                net_move_risk = (sl - entry) + (sl * est_exit_fee_rate)
            elif is_short is False:
                net_move_risk = (entry - sl) + (sl * est_exit_fee_rate)
            else:
                net_move_risk = abs(entry - sl) + (sl * est_exit_fee_rate)

            pos_risk = size * max(0.0, net_move_risk)
            existing_risk_usd += pos_risk
                
        remaining_risk_budget_usd = max(0.0, max_portfolio_risk_usd - existing_risk_usd)
        if remaining_risk_budget_usd < (total_balance * 0.002):
            msg = f"Total open risk budget exhausted: Active risk ${existing_risk_usd:.2f} >= Cap ${max_portfolio_risk_usd:.2f} ({MAX_TOTAL_PORTFOLIO_RISK_PCT*100}% limit)."
            self.logger.warning(f"[{self.name}] 🚫 TOTAL OPEN RISK VETO: {msg}")
            return self._build_veto_result("TOTAL_OPEN_RISK_CAP", msg, trade_action)

        # ═══ Stage 3: Dynamic Price Distances & Slippage Alignment ═══
        order_book_data = market_data.get("order_book_data", {})
        if not isinstance(order_book_data, dict):
            order_book_data = {}
        market_spread_pct = float(order_book_data.get("spread_pct") or 0.0) / 100.0

        base_asset = symbol.split('-')[0].upper() if symbol else ""
        if strategy_mode == "VOLATILITY_MOMENTUM" or base_asset in ["BTC", "ETH"]:
            base_slip = 0.001  # 0.10% (10 bps) for liquid majors & fast scalps
        else:
            base_slip = 0.002  # 0.20% (20 bps) for altcoins

        derivatives_data = market_data.get("derivatives_data", {})
        if not isinstance(derivatives_data, dict):
            derivatives_data = {}

        from core.risk_calculator import compute_execution_prices
        half_spread_pct = market_spread_pct / 2.0
        explicit_slip = float(derivatives_data["expected_slippage_pct"]) if derivatives_data.get("expected_slippage_pct") is not None else None
        exec_price_info = compute_execution_prices(
            current_price=current_price,
            direction=decision,
            expected_slippage_pct=explicit_slip,
            half_spread_pct=half_spread_pct,
            base_asset=base_asset
        )
        execution_entry = exec_price_info["execution_entry"]
        expected_limit_price = exec_price_info["limit_price"]
        expected_slip_buffer = exec_price_info["slip_buffer"]
        expected_slippage_pct = exec_price_info["expected_slippage_pct"]

        # Calculate SL and TP distances based on strategy mode
        is_anti_bot = bool(getattr(config, "ANTI_BOT_MODE", False))
        if strategy_mode == "MEAN_REVERSION":
            mean_target = float(indicators.get("bb_middle") or indicators.get("ema_20") or 0.0)
            if mean_target <= 0:
                msg = "Missing or invalid mean target (bb_middle / ema_20) for MEAN_REVERSION."
                self.logger.warning(f"[{self.name}] ❌ INVALID_INDICATORS VETO: {msg}")
                return self._build_veto_result("INVALID_INDICATORS", msg, trade_action)

            # Directional check: price MUST be on the correct side of the mean
            if decision == "LONG" and current_price >= mean_target:
                msg = f"Mean Reversion LONG invalid: current price ({current_price:.2f}) is already at or above mean target ({mean_target:.2f})."
                self.logger.warning(f"[{self.name}] ❌ INVALID_MEAN_TARGET VETO: {msg}")
                return self._build_veto_result("INVALID_MEAN_TARGET", msg, trade_action)
            elif decision == "SHORT" and current_price <= mean_target:
                msg = f"Mean Reversion SHORT invalid: current price ({current_price:.2f}) is already at or below mean target ({mean_target:.2f})."
                self.logger.warning(f"[{self.name}] ❌ INVALID_MEAN_TARGET VETO: {msg}")
                return self._build_veto_result("INVALID_MEAN_TARGET", msg, trade_action)

            dist_to_mean = abs(current_price - mean_target)
            is_anti_bot = bool(getattr(config, "ANTI_BOT_MODE", False))
            if is_anti_bot:
                min_edge_pct = 0.005  # 0.5% min edge in anti-bot predator mode
                min_sl_pct = 0.0045   # 0.45%
                min_tp_pct = 0.0085   # 0.85%
                sl_mult = 1.25
                tp_mult = 2.2
            else:
                min_edge_pct = 0.008  # 0.8% minimum distance to mean to justify slippage & exchange fees
                min_sl_pct = 0.008
                min_tp_pct = 0.008

            if dist_to_mean < (current_price * min_edge_pct):
                msg = f"Distance to mean ({dist_to_mean:.2f}, {dist_to_mean/current_price*100:.2f}%) is too narrow to cover fees/slippage (< {min_edge_pct*100:.1f}% min edge)."
                self.logger.warning(f"[{self.name}] ❌ LOW_EDGE VETO: {msg}")
                return self._build_veto_result("LOW_EDGE", msg, trade_action)

            if is_anti_bot:
                # Anti-Bot v2.0: Compact SL clamped to max 1.2% of entry price
                raw_sl = max(atr_14 * sl_mult, current_price * min_sl_pct)
                sl_dist_base = min(raw_sl, current_price * 0.012)

                # Sniper Take-Profit targeting EMA-20 (dynamic mean return)
                ema_20 = float(indicators.get("ema_20") or 0.0)
                if ema_20 > 0:
                    ema_dist = abs(current_price - ema_20)
                    if ema_dist >= (current_price * 0.0065):
                        tp_dist_base = max(ema_dist * 1.15, current_price * min_tp_pct)
                    else:
                        tp_dist_base = max(atr_14 * 2.2, current_price * 0.0095)
                else:
                    tp_dist_base = max(dist_to_mean * 0.90, current_price * min_tp_pct)
            else:
                tp_dist_base = dist_to_mean * 0.90
                sl_dist_base = max(atr_14 * sl_mult, current_price * min_sl_pct)
        elif strategy_mode == "VOLATILITY_MOMENTUM":
            # Scalping mode: BTC/ETH can tolerate tighter noise floors (0.6% / 1.2%),
            # while altcoins require at least 1.0% SL and 2.0% TP to prevent 1-min noise wicks from prematurely hitting stops
            min_sl_ratio = 0.006 if base_asset in ["BTC", "ETH"] else 0.010
            min_tp_ratio = 0.012 if base_asset in ["BTC", "ETH"] else 0.020
            sl_dist_base = max(atr_14 * sl_mult, current_price * min_sl_ratio)
            tp_dist_base = max(atr_14 * tp_mult, current_price * min_tp_ratio)
        else:
            # Lever 5: Volatility-Adaptive Stop Loss Clamp with Structural Anchor (Step 4)
            is_major = base_asset in ["BTC", "ETH"]
            min_sl_dist = current_price * (0.008 if is_major else 0.010)  # 0.8% floor for majors, 1.0% for altcoins
            max_sl_dist = current_price * (0.022 if is_major else 0.055)  # 2.2% ceiling for majors, 5.5% for volatile altcoins
            raw_sl_dist = atr_14 * sl_mult if atr_14 > 0 else (current_price * 0.015)

            # Preserve wide stop for extreme outliers (e.g. stress tests with ATR > 10% of price)
            atr_pct = (atr_14 / current_price) if current_price > 0 else 0.0
            if atr_pct <= 0.10:
                sl_dist_clamped = min(max_sl_dist, max(min_sl_dist, raw_sl_dist))
            else:
                sl_dist_clamped = raw_sl_dist

            # Structural anchor behind recent 16-candle swing extreme (Step 4)
            indicators = market_data.get("indicators") or {}
            swing_high_15m = float(indicators.get("swing_high_15m") or 0.0)
            swing_low_15m = float(indicators.get("swing_low_15m") or 0.0)

            c_list = price_data.get("candles") or market_data.get("multi_timeframe", {}).get("tf_15m", {}).get("candles_20", [])
            if (swing_high_15m <= 0.0 or swing_low_15m <= 0.0) and isinstance(c_list, list) and c_list:
                try:
                    h_16 = [float(c.get("high", current_price)) for c in c_list[-16:] if isinstance(c, dict)]
                    l_16 = [float(c.get("low", current_price)) for c in c_list[-16:] if isinstance(c, dict)]
                    if h_16: swing_high_15m = max(h_16)
                    if l_16: swing_low_15m = min(l_16)
                except Exception:
                    pass

            atr_buffer = 0.25 * (atr_14 if atr_14 > 0 else current_price * 0.01)

            has_structure = (decision == "SHORT" and swing_high_15m > 0) or (decision == "LONG" and swing_low_15m > 0)
            if has_structure:
                is_retest = bool(
                    _extract_cand_field(ceo_decision, "is_pullback_retest")
                    or (market_data.get("is_pullback_retest") if market_data else False)
                    or "RETEST" in str(_extract_cand_field(ceo_decision, "reasoning_en", "")).upper()
                    or "RETEST" in str(_extract_cand_field(ceo_decision, "reasoning", "")).upper()
                )
                max_sl_dist_allowed = 2.50 if is_retest else 1.95

                if decision == "SHORT":
                    structural_sl = swing_high_15m + atr_buffer
                    struct_dist_pct = ((structural_sl - current_price) / current_price) * 100.0 if current_price > 0 and structural_sl > current_price else 0.0
                else: # LONG
                    structural_sl = swing_low_15m - atr_buffer
                    struct_dist_pct = ((current_price - structural_sl) / current_price) * 100.0 if current_price > 0 and structural_sl < current_price else 0.0

                # Distance-to-Structure Gate (Step 4):
                # If distance to structural SL > max_sl_dist_allowed (1.95% or 2.50% for retest),
                # do NOT compress stop into noise zone! Reject immediate market entry and route to WAIT_FOR_PULLBACK.
                if struct_dist_pct > max_sl_dist_allowed:
                    lvl_ref = swing_high_15m if decision == "SHORT" else swing_low_15m
                    msg = (
                        f"Distance-to-Structure Gate: Расстояние до структурного стопа ({struct_dist_pct:.2f}% > {max_sl_dist_allowed:.2f}%) "
                        f"слишком велико из-за позднего входа (структурный SL={structural_sl:.4f} за уровнем {lvl_ref:.4f}). "
                        f"Сжатие стопа внутрь зоны шума запрещено. Рекомендуется WAIT_FOR_PULLBACK ближе к уровню ретеста."
                    )
                    self.logger.warning(f"[{self.name}] ❌ DISTANCE_TO_STRUCTURE_VETO: {msg}")
                    return self._build_veto_result("DISTANCE_TO_STRUCTURE_VETO", msg, "WAIT_FOR_PULLBACK")

                # Capped ATR distance so that 1.75*ATR multiplier never pushes a valid structural entry beyond max_sl_dist_allowed
                capped_atr_dist = min(sl_dist_clamped, current_price * (max_sl_dist_allowed / 100.0))
                if decision == "SHORT":
                    atr_based_sl = current_price + capped_atr_dist
                    sl_price = max(atr_based_sl, structural_sl)
                    sl_dist_base = sl_price - current_price
                else: # LONG
                    atr_based_sl = current_price - capped_atr_dist
                    sl_price = min(atr_based_sl, structural_sl)
                    sl_dist_base = current_price - sl_price
            else:
                sl_dist_base = sl_dist_clamped

            # Target Take Profit preserves or improves standard R:R >= 1.8
            target_rr = max(1.8, (tp_mult / sl_mult) if sl_mult > 0 else 1.8)
            tp_dist_base = max(atr_14 * tp_mult, sl_dist_base * target_rr, current_price * config.MIN_TP_PCT)

        if decision == "LONG":
            sl_price = current_price - sl_dist_base
            tp_price = current_price + tp_dist_base
        else: # SHORT
            sl_price = current_price + sl_dist_base
            tp_price = current_price - tp_dist_base

        # Profit Booster #1: Structural Take-Profit in front of 48h Macro Box Boundaries
        if strategy_mode == "TREND_FOLLOWING":
            indicators_map = market_data.get("indicators") or {}
            low_48h = float(indicators_map.get("low_48h") or 0.0)
            high_48h = float(indicators_map.get("high_48h") or 0.0)
            eff_atr = atr_14 if atr_14 > 0 else (current_price * 0.01)
            if decision == "SHORT" and low_48h > 0 and tp_price <= low_48h:
                box_tp = low_48h + (0.15 * eff_atr)
                if current_price > box_tp and ((current_price - box_tp) / max(sl_dist_base, 1e-9)) >= 1.45:
                    self.logger.info(
                        f"[{self.name}] 🎯 Booster #1 Structural TP: SHORT TP adjusted from {tp_price:.4f} to {box_tp:.4f} "
                        f"(in front of 48h low {low_48h:.4f}, R:R={((current_price - box_tp) / max(sl_dist_base, 1e-9)):.2f}R >= 1.45R)."
                    )
                    tp_price = box_tp
            elif decision == "LONG" and high_48h > 0 and tp_price >= high_48h:
                box_tp = high_48h - (0.15 * eff_atr)
                if box_tp > current_price and ((box_tp - current_price) / max(sl_dist_base, 1e-9)) >= 1.45:
                    self.logger.info(
                        f"[{self.name}] 🎯 Booster #1 Structural TP: LONG TP adjusted from {tp_price:.4f} to {box_tp:.4f} "
                        f"(in front of 48h high {high_48h:.4f}, R:R={((box_tp - current_price) / max(sl_dist_base, 1e-9)):.2f}R >= 1.45R)."
                    )
                    tp_price = box_tp

        # Strict Mean Reversion Guard: TP must NEVER cross or touch the mean target
        # For Anti-Bot v2.0, EMA-20 * 1.15 target intentionally penetrates beyond EMA-20 inside the channel,
        # so bypass mean_buffer clamping when is_anti_bot is True.
        if strategy_mode == "MEAN_REVERSION" and mean_target > 0 and not is_anti_bot:
            mean_buffer = max(mean_target * 0.001, dist_to_mean * 0.05)
            if decision == "LONG":
                tp_price = min(tp_price, mean_target - mean_buffer)
            else: # SHORT
                tp_price = max(tp_price, mean_target + mean_buffer)
            
        # Compute actual risk distances from EXPECTED EXECUTION ENTRY
        distance_to_sl = abs(execution_entry - sl_price)
        distance_to_tp = abs(tp_price - execution_entry)

        # Strict directional alignment check against expected execution entry (P1 Finding 3)
        if decision == "LONG":
            if sl_price >= execution_entry:
                msg = f"LONG SL ({sl_price:.4f}) is at or above expected execution entry ({execution_entry:.4f}) after slippage."
                self.logger.warning(f"[{self.name}] ❌ INVALID_PRICE_LEVELS VETO: {msg}")
                return self._build_veto_result("INVALID_PRICE_LEVELS", msg, trade_action)
            if tp_price <= execution_entry:
                msg = f"LONG TP ({tp_price:.4f}) is at or below expected execution entry ({execution_entry:.4f}) after slippage."
                self.logger.warning(f"[{self.name}] ❌ INVALID_PRICE_LEVELS VETO: {msg}")
                return self._build_veto_result("INVALID_PRICE_LEVELS", msg, trade_action)
        elif decision == "SHORT":
            if sl_price <= execution_entry:
                msg = f"SHORT SL ({sl_price:.4f}) is at or below expected execution entry ({execution_entry:.4f}) after slippage."
                self.logger.warning(f"[{self.name}] ❌ INVALID_PRICE_LEVELS VETO: {msg}")
                return self._build_veto_result("INVALID_PRICE_LEVELS", msg, trade_action)
            if tp_price >= execution_entry:
                msg = f"SHORT TP ({tp_price:.4f}) is at or above expected execution entry ({execution_entry:.4f}) after slippage."
                self.logger.warning(f"[{self.name}] ❌ INVALID_PRICE_LEVELS VETO: {msg}")
                return self._build_veto_result("INVALID_PRICE_LEVELS", msg, trade_action)

        # Slippage edge check for Mean Reversion: ensure expected execution entry doesn't consume all profit
        if strategy_mode == "MEAN_REVERSION":
            if decision == "LONG" and tp_price <= execution_entry:
                msg = f"Mean Reversion LONG TP ({tp_price:.2f}) is at or below expected execution entry ({execution_entry:.2f}) due to slippage."
                self.logger.warning(f"[{self.name}] ❌ LOW_EDGE VETO: {msg}")
                return self._build_veto_result("LOW_EDGE", msg, trade_action)
            elif decision == "SHORT" and tp_price >= execution_entry:
                msg = f"Mean Reversion SHORT TP ({tp_price:.2f}) is at or above expected execution entry ({execution_entry:.2f}) due to slippage."
                self.logger.warning(f"[{self.name}] ❌ LOW_EDGE VETO: {msg}")
                return self._build_veto_result("LOW_EDGE", msg, trade_action)

        # ═══ Stage 4: Net Realized Risk/Reward & Expectancy Gate ═══
        derivatives_data = market_data.get("derivatives_data", {})
        if not isinstance(derivatives_data, dict):
            derivatives_data = {}
        fee_pct = float(derivatives_data.get("taker_fee_pct") or 0.0005) * 2 # Open + Close

        # Strategy-dependent holding hours
        if strategy_mode == "VOLATILITY_MOMENTUM":
            holding_hours = 1.0
        elif strategy_mode == "MEAN_REVERSION":
            holding_hours = 2.0
        else:
            holding_hours = 4.0

        # Preserve genuine 0.0 funding without overriding with non-zero defaults (P1 Finding 4)
        raw_fr = derivatives_data.get("funding_rate_hourly")
        if raw_fr is None:
            raw_fr = derivatives_data.get("funding_rate")
        funding_rate_hourly = float(raw_fr) if (raw_fr is not None and str(raw_fr).strip() != "") else 0.0
        funding_cost_pct = (funding_rate_hourly if decision == "LONG" else -funding_rate_hourly) * holding_hours
        
        unit_entry_fee = execution_entry * (fee_pct / 2)
        unit_tp_exit_fee = tp_price * (fee_pct / 2)
        unit_sl_exit_fee = sl_price * (fee_pct / 2)
        unit_funding_cost = execution_entry * funding_cost_pct
        
        unit_net_profit = distance_to_tp - unit_entry_fee - unit_tp_exit_fee - unit_funding_cost
        unit_net_loss = distance_to_sl + unit_entry_fee + unit_sl_exit_fee + max(0.0, unit_funding_cost)
        candidate_net_rr = unit_net_profit / unit_net_loss if unit_net_loss > 0 else 0.0
        realized_rr = candidate_net_rr  # Backward compatibility alias

        # Universal Net Profit Floor Check
        if unit_net_profit <= 0:
            msg = (f"Candidate trade has non-positive net profit (${unit_net_profit:.4f}) "
                   f"after expected slippage, round-trip fees (${unit_entry_fee + unit_tp_exit_fee:.4f}), "
                   f"and holding funding (${unit_funding_cost:.4f}).")
            self.logger.warning(f"[{self.name}] ❌ POOR_RISK_REWARD VETO: {msg}")
            return self._build_veto_result("POOR_RISK_REWARD", msg, trade_action)

        # Strict Risk/Reward Floor Validation (UNIVERSAL — applies to ALL strategy modes):
        # min_rr_required is loaded from profile rules and MUST be enforced here.
        if candidate_net_rr < min_rr_required:
            msg = (f"{strategy_mode} net RR ({candidate_net_rr:.2f}) is below minimum required "
                   f"{min_rr_required:.2f}:1 for {profile_name} profile "
                   f"(after slippage and fees). "
                   f"TP dist={distance_to_tp:.4f}, SL dist={distance_to_sl:.4f}, "
                   f"fees={unit_entry_fee + unit_tp_exit_fee:.4f}, funding={unit_funding_cost:.4f}. Routed to WAIT_FOR_PULLBACK.")
            self.logger.warning(f"[{self.name}] ❌ POOR_RISK_REWARD VETO: {msg}")
            return self._build_veto_result("POOR_RISK_REWARD", msg, "WAIT_FOR_PULLBACK")

        # Pre-compute Bayesian Strategy Multiplier before Expectancy Gate
        strat_tag = str(strategy_mode or "TREND_FOLLOWING").upper()
        regime_tag = str(macro_regime or self.default_macro_regime or "UNKNOWN").upper()
        streak_by_tag = portfolio_data.get("streak_history_by_tag")
        if streak_by_tag is None:
            try:
                from core.state_store import StateStore
                journal_records = StateStore.load("data/memory/trade_journal.json", default=[])
                streak_by_tag = self.compute_strategy_regime_stats_from_journal(journal_records, decay=0.97)
            except Exception:
                streak_by_tag = {}

        is_anti_bot_mr = bool((getattr(config, "ANTI_BOT_MODE", False) or strategy_mode == "MEAN_REVERSION") and strat_tag == "MEAN_REVERSION")

        bayes_mult, bayes_prob, bayes_info = self.get_bayesian_strategy_multiplier(
            strategy_tag=strat_tag,
            regime=regime_tag,
            streak_history_by_tag=streak_by_tag
        )
        if is_anti_bot_mr:
            bayes_mult = max(0.90, bayes_mult)

        # ═══ Bayesian Expectancy Gate & Regime Strategy Filter ═══
        # 1. Prohibit BREAKOUT in TRANSITION regime (statistically negative expectation: false breakout wick traps)
        allow_forbidden = bool(getattr(config, "ALLOW_FORBIDDEN_STRATEGIES", False))

        if not allow_forbidden and regime_tag == "TRANSITION" and strat_tag == "BREAKOUT":
            self.logger.warning(
                f"[{self.name}] ❌ STRATEGY REGIME VETO: {regime_tag}:{strat_tag} is prohibited due to false breakout wick traps. "
                f"BREAKOUT is strictly banned in TRANSITION."
            )
            return self._build_veto_result(
                "REGIME_STRATEGY_MISMATCH",
                f"BREAKOUT is prohibited in TRANSITION regime (risk of liquidity traps)",
                trade_action
            )
        elif allow_forbidden and regime_tag == "TRANSITION" and strat_tag == "BREAKOUT":
            self.logger.info(
                f"[{self.name}] ⚠️ STRATEGY REGIME VETO BYPASSED (ALLOW_FORBIDDEN_STRATEGIES=True): "
                f"Allowing {regime_tag}:{strat_tag} with Cohort B safety sizing."
            )
            trade_action = "REDUCE_SIZE"

        # 2. Anti-Death-Spiral Bayesian Expectancy Gate:
        # On small/immature trade sample (< 10 trades), NEVER apply hard VETO on allowed trend strategies.
        # Until at least 10 real trades are recorded, smooth multiplier within [0.85, 1.15].
        bucket_stats = (streak_by_tag or {}).get(f"{regime_tag}:{strat_tag}", {})
        bucket_losses = float(bucket_stats.get("losses", 0.0) or 0.0)
        bucket_total = int(bucket_stats.get("total", 0) or 0)

        if strat_tag in ["TREND_FOLLOWING", "RELATIVE_MOMENTUM"]:
            if bucket_total < 10:
                bayes_mult = max(0.85, min(1.15, bayes_mult))
                self.logger.info(
                    f"[{self.name}] 🛡️ Bayesian Warm-Up Protection ({regime_tag}:{strat_tag}, {bucket_total}/10 trades): "
                    f"Clamping bayes_mult={bayes_mult:.2f} in [0.85, 1.15] without hard veto."
                )
            elif bayes_prob < 0.40:
                self.logger.warning(
                    f"[{self.name}] ❌ BAYESIAN NEGATIVE EDGE VETO: {bayes_info} has posterior win prob {bayes_prob:.1%} < 40.0% "
                    f"on mature sample of {bucket_total} trades. Rejecting trade candidate."
                )
                return self._build_veto_result(
                    "NEGATIVE_BAYESIAN_EXPECTANCY",
                    f"Bayesian win probability ({bayes_prob:.1%}) < 40.0% for bucket {regime_tag}:{strat_tag} ({bucket_total} trades)",
                    trade_action
                )
        else:
            if bucket_total >= 10 and bayes_prob < 0.40:
                self.logger.warning(
                    f"[{self.name}] ❌ BAYESIAN NEGATIVE EDGE VETO: {bayes_info} has posterior win prob {bayes_prob:.1%} < 40.0% "
                    f"on mature sample of {bucket_total} trades. Rejecting trade candidate."
                )
                return self._build_veto_result(
                    "NEGATIVE_BAYESIAN_EXPECTANCY",
                    f"Bayesian win probability ({bayes_prob:.1%}) < 40.0% for bucket {regime_tag}:{strat_tag} ({bucket_total} trades)",
                    trade_action
                )

        # Expectancy Gate with Hysteresis (evaluates rolling window era if >= 10 trades, else lifetime if >= 30 trades)
        window_total = int(portfolio_data.get("window_trades_count", 0) or 0)
        if window_total >= 10:
            win_count = int(portfolio_data.get("window_win_count", 0) or 0)
            loss_count = int(portfolio_data.get("window_loss_count", 0) or 0)
            total_trades = window_total
            win_rate = (win_count / total_trades) if total_trades > 0 else 0.0
            min_req_trades = 10
        else:
            win_count = int(portfolio_data.get("win_count", 0) or 0)
            loss_count = int(portfolio_data.get("loss_count", 0) or 0)
            total_trades = win_count + loss_count
            win_rate = (win_count / total_trades) if total_trades > 0 else 0.0
            min_req_trades = 30

        expectancy_penalty_active = getattr(self, "_expectancy_penalty_active", False)
        
        hist_payoff = float(portfolio_data.get("historical_payoff_ratio", 0.0) or portfolio_data.get("historical_avg_rr", 0.0) or 0.0)
        if hist_payoff <= 0.0:
            avg_w = float(portfolio_data.get("avg_win_usd", 0.0) or 0.0)
            avg_l = float(portfolio_data.get("avg_loss_usd", 0.0) or 0.0)
            if avg_w > 0 and avg_l > 0:
                hist_payoff = avg_w / avg_l
            # If missing, remain 0.0 without fabricating unmeasured 1.0 assumption
        
        if total_trades >= min_req_trades and hist_payoff > 0:
            historical_expected_r = (win_rate * hist_payoff) - (1.0 - win_rate)
            
            # Hysteresis: trigger at E < -0.10R, clear at E > +0.05R
            if not expectancy_penalty_active and historical_expected_r < -0.10:
                self._expectancy_penalty_active = True
                expectancy_penalty_active = True
            elif expectancy_penalty_active and historical_expected_r > 0.05:
                self._expectancy_penalty_active = False
                expectancy_penalty_active = False
                
            # Anti-Double Penalty & Era Alignment:
            # If Discounted Beta-Bernoulli confirms a positive strategy edge (bayes_mult > 1.0),
            # or if era recovery floor is active (min_kelly_mult >= 0.80), suppress the generic lifetime penalty.
            if expectancy_penalty_active and (bayes_mult > 1.0 or float(portfolio_data.get("min_kelly_mult", 0.50) or 0.50) >= 0.80):
                self.logger.info(
                    f"[{self.name}] ℹ️ EXPECTANCY GATE BYPASS: Specific regime edge or era recovery active "
                    f"(bayes_mult={bayes_mult:.3f}, b_hist={hist_payoff:.2f}). Legacy lifetime penalty suppressed."
                )
                expectancy_penalty_active = False

        if expectancy_penalty_active:
                penalty_factor = 0.75
                self.logger.warning(
                    f"[{self.name}] ⚠️ EXPECTANCY GATE ACTIVE: Historical strategy expectancy is {historical_expected_r:.2f}R "
                    f"(< -0.10R threshold on {total_trades} trades with b_hist={hist_payoff:.2f}). "
                    f"WR: {win_rate*100:.1f}%. Applying risk penalty (x{penalty_factor}) and raising min_conviction."
                )
                min_conviction = min(95, min_conviction + 5)
                base_risk *= penalty_factor
                
        if conviction < min_conviction:
            return self._build_veto_result("LOW_CONFIDENCE", f"Conviction {conviction} is below Expectancy/Win Rate Gate threshold {min_conviction}", trade_action)

        # ═══ Stage 5: Multipliers & Dynamic Risk Percentage ═══
        # 1. Volume & Spread Quality Multiplier (F-05 Fix: Production Key Fallback & Young Candle Shield)
        volume_mult = 1.0
        ohlcv = (
            market_data.get("price_data", {}).get("ohlcv_1h")
            or market_data.get("multi_timeframe", {}).get("tf_1h", {}).get("candles_20", [])
        )
        if isinstance(ohlcv, list) and len(ohlcv) >= 10:
            avg_volume_10 = sum(float(c.get("volume", 0) or 0) for c in ohlcv[-10:]) / 10
            # Young candle defense: at early seconds of a new 1h candle, v[-1] is near 0.
            # Use max of latest candle and preceding closed candle:
            v_curr = float(ohlcv[-1].get("volume", 0) or 0)
            v_prev = float(ohlcv[-2].get("volume", 0) or 0) if len(ohlcv) >= 2 else v_curr
            effective_v = max(v_curr, v_prev)
            if avg_volume_10 > 0 and effective_v < avg_volume_10:
                volume_mult = 0.85
                
        spread_pct = float(market_data.get("order_book_data", {}).get("spread_pct", 0) or 0)
        spread_mult = 1.0
        if spread_pct > (config.SPREAD_PENALTY_THRESHOLD * 0.5):
            spread_mult = 0.90
            
        quality_mult = volume_mult * spread_mult

        # 2. Drawdown Streak Multiplier (Step-Wise Recovery)
        recent_streak = portfolio_data.get("recent_streak", [])
        peak_bal = float(portfolio_data.get("peak_balance") or portfolio_data.get("initial_balance") or 0.0)
        dd_mult, dd_msg = self._calculate_drawdown_multiplier(
            recent_streak, daily_drawdown_pct, current_balance=total_balance, peak_balance=peak_bal
        )
        if dd_mult < 1.0:
            self.logger.warning(f"[{self.name}] {dd_msg}")

        # 3. Fractional (Half) Kelly Multiplier with Era Recovery Soft Floor
        kelly_floor = float(portfolio_data.get("min_kelly_mult", 0.50) or 0.50)
        raw_kelly = self._calculate_kelly_multiplier(win_rate, hist_payoff, total_trades) if total_trades >= 15 else 1.0
        kelly_mult = max(kelly_floor, raw_kelly)

        # 4. Conviction and Action Multipliers (Signal Edge)
        conv_for_risk = float(_extract_cand_field(ceo_decision, "conviction_for_risk", conviction) or conviction)
        conf_mult = self._get_conviction_multiplier(min_conviction, max(conviction, int(conv_for_risk)))
        
        # Check for exhaustion scale or explicit risk_mult_override from Deterministic Guard
        risk_mult_override = _extract_cand_field(ceo_decision, "risk_mult_override")
        action_size_mult_override = _extract_cand_field(ceo_decision, "action_size_mult_override")
        is_vol_explosion = bool(_extract_cand_field(ceo_decision, "volume_explosion_override", False))
        if risk_mult_override is not None:
            try:
                action_mult = float(risk_mult_override)
            except (ValueError, TypeError):
                action_mult = 0.50 if (is_exhaustion_scale or is_vol_explosion) else (0.85 if trade_action == "REDUCE_SIZE" else 1.0)
        elif action_size_mult_override is not None:
            try:
                action_mult = float(action_size_mult_override)
            except (ValueError, TypeError):
                action_mult = 0.50
        elif is_exhaustion_scale or is_vol_explosion:
            action_mult = 0.50
        else:
            action_mult = 0.85 if trade_action == "REDUCE_SIZE" else 1.0

        # Cohort Determination for mathematical expectancy analytics (Groups A, B, C, D):
        # A: High Conviction (>= min_conviction + 10), Full Size (action == ENTER)
        # B: High Conviction (>= min_conviction + 10 or (conv_for_risk >= min(75.0, min_conviction + 5.0) and entry_quality >= 60.0)), Reduced Size (action == REDUCE_SIZE)
        # C: Low Conviction (< min_conviction + 10), Full Size (action == ENTER)
        # D: Low Conviction (< min_conviction + 10), Reduced Size (action == REDUCE_SIZE)
        is_full_size = (trade_action != "REDUCE_SIZE")
        if is_full_size:
            is_high_conv = conv_for_risk >= (min_conviction + 10)
        else:
            # For REDUCE_SIZE (e.g. Pullback Retests or conservative entries), allow solid setups
            # (Explicit conv_for_risk >= 80, or Pullback Retest with DirConf >= 75 and EntryQuality >= 60)
            # to qualify for Cohort B protection rather than falling into empirical Cohort D veto.
            cand_eq = float(_extract_cand_field(ceo_decision, "entry_quality", conviction) or conviction)
            has_explicit_conv_risk = _extract_cand_field(ceo_decision, "conviction_for_risk") is not None
            is_retest = bool(
                _extract_cand_field(ceo_decision, "is_pullback_retest")
                or (market_data.get("is_pullback_retest") if market_data else False)
                or "RETEST" in str(_extract_cand_field(ceo_decision, "reasoning_en", "")).upper()
                or "RETEST" in str(_extract_cand_field(ceo_decision, "reasoning", "")).upper()
            )
            is_high_conv = (conv_for_risk >= (min_conviction + 10)) or (
                has_explicit_conv_risk and conv_for_risk >= 75.0 and cand_eq >= 60.0
            ) or (
                is_retest and conv_for_risk >= min(75.0, min_conviction + 5.0) and cand_eq >= 60.0
            ) or is_vol_explosion

        if is_high_conv and is_full_size:
            cohort = "A"
        elif is_high_conv and not is_full_size:
            cohort = "B"
        elif not is_high_conv and is_full_size:
            cohort = "C"
        else:
            cohort = "D"

        # Gate: Block Cohort D trades (low conviction + REDUCE_SIZE) due to empirical negative expectancy E(R) < 0
        block_cohort_d = getattr(config, "BLOCK_COHORT_D_TRADES", True)
        if block_cohort_d and cohort == "D":
            msg = (f"Cohort D trade vetoed: Conviction {conviction}% is below high-confidence threshold "
                   f"({min_conviction + 10}%) with REDUCE_SIZE. Empirical expectancy E(R) < 0; "
                   f"capital preserved by enforcing strict HOLD.")
            self.logger.warning(f"[{self.name}] 🚫 COHORT_D_VETO: {msg}")
            return self._build_veto_result("COHORT_D_NEGATIVE_EXPECTANCY", msg, "HOLD")

        # Orthogonal Factor 2: Market Friction (Volume & Spread)
        market_mult = quality_mult
        if is_anti_bot_mr:
            market_mult = max(1.0, market_mult)

        # Orthogonal Factor 3: Portfolio Health (Drawdown streak & Kelly)
        portfolio_mult = dd_mult * kelly_mult

        # Anti-Double Penalty: If portfolio is already strictly throttled (<= 0.50x) by DD streak or Kelly,
        # do not apply action_mult = 0.85 penalty from REDUCE_SIZE on top of it.
        effective_action_mult = 1.0 if (portfolio_mult <= 0.50 and trade_action == "REDUCE_SIZE") else action_mult
        raw_signal_mult = conf_mult * effective_action_mult
        min_allowed_signal = 0.50 if (is_exhaustion_scale or is_vol_explosion) else 0.85
        signal_mult = max(min_allowed_signal, raw_signal_mult)

        # Combine into dynamic risk percentage with a safe floor check
        # Orthogonal Factor 4: Online Bayesian Strategy Sizing (Pre-computed above for Expectancy Gate)

        # Anti-Double Penalty for Bayes: If portfolio is already strictly throttled (portfolio_mult <= 0.45)
        # and bayes_mult < 1.0, clamp bayes penalty floor to 0.85x to avoid double penalty starvation
        effective_bayes_mult = max(0.90 if is_anti_bot_mr else 0.85, bayes_mult) if (portfolio_mult <= 0.45 and bayes_mult < 1.0) else (max(0.90, bayes_mult) if is_anti_bot_mr else bayes_mult)

        dynamic_risk_pct = base_risk * signal_mult * market_mult * portfolio_mult * effective_bayes_mult
        # Lowered from 0.35 to 0.25 of base risk (0.375% for BALANCED, 0.50% for AGGRESSIVE)
        # to prevent mathematical deadlocks while preserving exchange minimum viable sizing
        min_viable_risk = base_risk * 0.25

        if dynamic_risk_pct < min_viable_risk:
            # High-Conviction / Tier-A Setup Floor:
            # Protect high-conviction trades (Cohort A or B, or Conviction >= 75%)
            # when market friction is low (market_mult >= 0.85) and actual account drawdown is safe (< 12%).
            # Clamp dynamic risk to min_viable_risk (preserving positive bayes edge if bayes_mult > 1.0).
            is_high_conviction_setup = (
                (trade_action == "ENTER" and (cohort == "A" or conviction >= 75))
                or (trade_action == "REDUCE_SIZE" and (cohort in ["A", "B"] or conviction >= 75))
            )
            effective_account_dd = max(
                daily_drawdown_pct * 100.0,
                ((peak_bal - total_balance) / peak_bal * 100.0) if peak_bal > total_balance > 0 else 0.0
            )
            if is_high_conviction_setup and market_mult >= 0.85 and effective_account_dd < 12.0:
                boosted_floor = min_viable_risk * max(1.0, bayes_mult)
                self.logger.info(
                    f"[{self.name}] 🛡️ High-Conviction Floor applied: Boosting dynamic risk from "
                    f"{dynamic_risk_pct*100:.3f}% to {boosted_floor*100:.3f}% "
                    f"(Cohort={cohort}, Conviction={conviction}%, Action={trade_action}, DD={effective_account_dd:.2f}%, Bayes=x{bayes_mult:.2f})"
                )
                dynamic_risk_pct = boosted_floor
            else:
                msg = (f"Dynamic risk ({dynamic_risk_pct*100:.3f}%) fell below minimum viable risk "
                       f"({min_viable_risk*100:.3f}%). Cumulative penalties (Signal x{signal_mult:.2f}, "
                       f"DD x{dd_mult:.2f}, Qual x{quality_mult:.2f}, Kelly x{kelly_mult:.2f}, Bayes x{bayes_mult:.2f}) "
                       f"suppress viable trade edge. Vetoing to protect capital.")
                self.logger.warning(f"[{self.name}] 🚫 PENALTY_SUPPRESSION VETO: {msg}")
                return self._build_veto_result("PENALTY_SUPPRESSION", msg, trade_action)

        dynamic_risk_pct = min(dynamic_risk_pct, risk_cap)
        
        applied_multipliers = {
            "conf_mult": conf_mult,
            "action_mult": effective_action_mult,
            "signal_mult": round(signal_mult, 4),
            "quality_mult": round(quality_mult, 4),
            "market_mult": round(market_mult, 4),
            "dd_mult": round(dd_mult, 4),
            "kelly_mult": round(kelly_mult, 4),
            "bayes_mult": round(bayes_mult, 4),
            "combined_mult": round(dynamic_risk_pct / base_risk, 4) if base_risk > 0 else 1.0
        }

        self.logger.info(
            f"[{self.name}] Final Risk: {dynamic_risk_pct*100:.2f}% (Base: {base_risk*100}%, Signal: x{signal_mult:.2f} "
            f"[Conf: x{conf_mult}, Action: x{effective_action_mult}], Market: x{market_mult:.2f}, "
            f"Portfolio: x{portfolio_mult:.2f} [DD: x{dd_mult}, Kelly: x{kelly_mult:.2f}], "
            f"Bayes: x{bayes_mult:.2f} [{bayes_info}], Cohort: {cohort}, Cap: {risk_cap*100}%)"
        )

        # ═══ Stage 6: Position Sizing (Canonical Math with Fee Integration) ═══
        risk_amount_usd = total_balance * dynamic_risk_pct
        if remaining_risk_budget_usd > 0:
            risk_amount_usd = min(risk_amount_usd, remaining_risk_budget_usd)

        taker_fee_pct = float(derivatives_data.get("taker_fee_pct") or 0.0005)
        # Unit loss per contract = price distance to SL + round-trip taker fees + adverse funding
        fee_loss_per_contract = (execution_entry + sl_price) * taker_fee_pct if (execution_entry > 0 and sl_price > 0) else 0.0
        adverse_funding_per_contract = max(0.0, unit_funding_cost)
        effective_loss_per_contract = distance_to_sl + fee_loss_per_contract + adverse_funding_per_contract

        if effective_loss_per_contract > 0:
            contracts = risk_amount_usd / effective_loss_per_contract
            notional_usd = contracts * execution_entry
            
            # Nado DEX CLOB & Native Trigger Optimization ($104.00 Exchange Safe Bump):
            # If notional is below the exchange CLOB threshold ($35.00 <= notional_usd < 103.50),
            # conditionally bump notional to $104.00 to pass exchange limit (avoiding error 2094),
            # provided the resulting risk does not exceed max(risk_amount_usd * 1.45, total_balance * 0.026).
            min_bump_threshold = 35.0
            target_bump_notional = 104.0
            is_trigger_bumped = False
            is_production_mode = getattr(config, "SHADOW_MODE", False) or getattr(config, "NADO_LIVE_TRADING_ENABLED", False) or getattr(config, "LIVE_TRADING_ENABLED", False)
            raw_mn = derivatives_data.get("min_notional")
            needs_bump = (is_production_mode or raw_mn is None or float(raw_mn or 0.0) >= 100.0)
            if needs_bump and min_bump_threshold <= notional_usd < 103.50:
                bumped_notional = target_bump_notional
                bumped_contracts = bumped_notional / execution_entry if execution_entry > 0 else contracts
                bumped_risk = bumped_contracts * effective_loss_per_contract
                max_allowed_bump_risk = max(risk_amount_usd * 1.45, total_balance * 0.026) if total_balance > 0 else (risk_amount_usd * 1.45)
                can_bump = (bumped_notional <= max_notional_usd) and (bumped_risk <= max_allowed_bump_risk)
                if bumped_notional <= max_notional_usd and total_balance > 0 and can_bump:
                    self.logger.info(
                        f"[{self.name}] ⚡ Nado CLOB Optimization: Bumped notional from ${notional_usd:.2f} to ${bumped_notional:.2f} "
                        f"to pass $101 exchange limit for CLOB and native SL/TP triggers (Risk: ${bumped_risk:.2f} / {(bumped_risk/total_balance)*100:.2f}%)."
                    )
                    notional_usd = bumped_notional
                    contracts = bumped_contracts
                    risk_amount_usd = bumped_risk
                    is_trigger_bumped = True

            # Hard Cap on Max Account Notional
            if notional_usd > max_notional_usd:
                notional_usd = max_notional_usd
                contracts = notional_usd / execution_entry
                risk_amount_usd = contracts * effective_loss_per_contract
                self.logger.info(f"[{self.name}] ⚠️ Notional clamped to Max Notional (${max_notional_usd:.2f}). Reduced risk amount to ${risk_amount_usd:.2f}.")
        else:
            contracts = 0.0
            notional_usd = 0.0


        # Slippage Penalty & Veto
        if spread_pct > config.SPREAD_VETO_THRESHOLD:
            msg = f"Spread is {spread_pct}% (Too illiquid). Blocking trade."
            self.logger.warning(f"[{self.name}] ❌ SPREAD VETO: {msg}")
            return self._build_veto_result("SPREAD", msg, trade_action)
        elif spread_pct > config.SPREAD_PENALTY_THRESHOLD:
            notional_usd *= 0.8 # Cut notional by 20%
            contracts = notional_usd / execution_entry if execution_entry > 0 else 0.0
            risk_amount_usd = contracts * effective_loss_per_contract

        # ═══ Stage 7: Target Margin & Required Leverage ═══
        target_margin_pct = profile_rules.get("target_margin_pct", 0.10)
        usable_margin = min(available_margin * 0.8, total_balance * max_margin_pct)
        # Note: Capital allocation budget. Drawdown and action penalties are already fully
        # accounted for in dynamic_risk_pct (Stage 5) to avoid double-penalizing position size.
        target_margin_usd = min(total_balance * target_margin_pct, usable_margin)
        
        if target_margin_usd > 0:
            required_leverage = notional_usd / target_margin_usd
        else:
            required_leverage = 1.0
            
        # Volatility Leverage Cap
        atr_pct_val = (atr_14 / current_price) * 100 if current_price > 0 else 1.0
        if atr_pct_val >= 5.0:
            vol_max_leverage = 3.0
        elif atr_pct_val >= 3.0:
            vol_max_leverage = 5.0
        elif atr_pct_val >= 2.0:
            vol_max_leverage = 7.0
        elif atr_pct_val >= 1.0:
            vol_max_leverage = 10.0
        else:
            vol_max_leverage = 15.0
            
        # Liquidation Precedence Guard:
        # Stop loss MUST trigger well before liquidation price.
        # Minimum safety cushion is 30% between SL and liquidation distance:
        # sl_dist_pct <= liq_dist_pct * 0.70 => 1/L >= (sl_dist_pct / 0.70) + mmf
        mmf = float(derivatives_data.get("maintenance_margin_pct", 0.025) or 0.025)
        if mmf <= 0 or mmf >= 0.5:
            mmf = 0.025
        sl_dist_pct = distance_to_sl / execution_entry if execution_entry > 0 else 0.05
        if sl_dist_pct > 0:
            max_liq_leverage = 1.0 / ((sl_dist_pct / 0.70) + mmf)
        else:
            max_liq_leverage = max_leverage

        liq_safe_ceiling = max(1.0, float(int(max_liq_leverage)))
        safe_ceiling_leverage = min(max_leverage, vol_max_leverage, liq_safe_ceiling)
        
        # Determine integer leverage:
        # If required_leverage is within safe_ceiling_leverage, ceil(required_leverage) ensures
        # required margin (notional / leverage) <= target_margin_usd without unnecessarily shrinking notional.
        # If required_leverage exceeds safe ceiling, cap at int(safe_ceiling_leverage).
        candidate_leverage = math.ceil(required_leverage) if required_leverage > 0 else 1.0
        if candidate_leverage <= safe_ceiling_leverage:
            final_leverage = max(1.0, float(candidate_leverage))
        else:
            final_leverage = max(1.0, float(int(safe_ceiling_leverage)))
        
        # Enforce Target Margin strictly (Variant A: margin is the anchor) (F-04 Fix: Protect $101 Nado Bump)
        is_trigger_bumped = locals().get("is_trigger_bumped", False) or (abs(notional_usd - 101.0) < 1e-4)

        # When Nado Trigger Optimization is active ($101.00 bump to pass $100 exchange limit), or if notional can fit in usable_margin,
        # allow using usable_margin (up to max_margin_pct) rather than restricting to target_margin_usd,
        # so we do not clamp $101.00 back down below $100.50 and trigger an unnecessary MIN_NOTIONAL veto.
        fits_in_usable_margin = (final_leverage > 0 and (notional_usd / final_leverage) <= usable_margin)
        allowed_margin_ceiling = usable_margin if (is_trigger_bumped or is_anti_bot_mr) else target_margin_usd
        max_safe_notional = allowed_margin_ceiling * final_leverage

        if notional_usd > max_safe_notional:
            # If notional was intentionally bumped to $101.00 for native exchange triggers or in Anti-Bot MR mode,
            # check if usable_margin (capped at max_margin_pct) can safely accommodate it without exceeding limits
            required_bump_margin = notional_usd / final_leverage if final_leverage > 0 else notional_usd
            if (is_trigger_bumped or is_anti_bot_mr) and required_bump_margin <= usable_margin:
                self.logger.info(
                    f"[{self.name}] 🛡️ Preserving Nado Trigger Optimization ($101.00 notional): "
                    f"Required margin ${required_bump_margin:.2f} <= usable margin ${usable_margin:.2f} at {final_leverage}x."
                )
            elif (is_trigger_bumped or is_anti_bot_mr) and (notional_usd >= 100.50) and (notional_usd / final_leverage <= usable_margin * 0.95):
                self.logger.info(
                    f"[{self.name}] 🛡️ Preserving notional above $100.50: required margin ${notional_usd / final_leverage:.2f} fits in usable_margin * 0.95 (${usable_margin * 0.95:.2f})."
                )
            elif notional_usd >= 100.50 and (104.0 / final_leverage <= usable_margin):
                preserved_notional = max(104.0, max_safe_notional)
                self.logger.info(
                    f"[{self.name}] 🛡️ Stage 7 Protection: Preserving notional at ${preserved_notional:.2f} >= $104.00 "
                    f"to prevent clamping below exchange trigger minimum (fits in usable margin ${usable_margin:.2f} at {final_leverage}x)."
                )
                notional_usd = preserved_notional
                contracts = notional_usd / execution_entry if execution_entry > 0 else 0.0
                risk_amount_usd = contracts * effective_loss_per_contract
            else:
                self.logger.warning(
                    f"[{self.name}] Required margin at {final_leverage}x would exceed target margin. "
                    f"Reducing notional from ${notional_usd:.2f} to ${max_safe_notional:.2f} to strictly enforce target."
                )
                notional_usd = max_safe_notional
                contracts = notional_usd / execution_entry if execution_entry > 0 else 0.0
                risk_amount_usd = contracts * effective_loss_per_contract
            
        effective_leverage = final_leverage

        # Compute exact liquidation price
        # LONG: Entry * (1 - 1/L + MMF)
        # SHORT: Entry * (1 + 1/L - MMF)
        if decision == "LONG":
            liq_price = execution_entry * (1.0 - (1.0 / effective_leverage) + mmf)
        else:
            liq_price = execution_entry * (1.0 + (1.0 / effective_leverage) - mmf)
        liq_price = max(0.0, round(liq_price, 6))

        # Absolute safety check: SL must NOT breach or equal liquidation price
        if (decision == "LONG" and sl_price <= liq_price) or (decision == "SHORT" and sl_price >= liq_price):
            msg = f"Liquidation precedence violation: SL ({sl_price}) is beyond or equal to liquidation price ({liq_price:.4f})."
            self.logger.warning(f"[{self.name}] ❌ LIQUIDATION_RISK VETO: {msg}")
            return self._build_veto_result("LIQUIDATION_RISK", msg, trade_action)

        # Strict Margin Cushion Guard: Distance to liquidation must be at least 1.40x distance to SL (P1 Finding 2)
        # Ensures that SL triggers with sufficient safety margin before maintenance margin exhaustion
        distance_to_liq = abs(execution_entry - liq_price)
        if distance_to_sl > 0 and distance_to_liq < (1.40 * distance_to_sl):
            cushion_ratio = distance_to_liq / distance_to_sl
            msg = (
                f"Insufficient liquidation cushion: distance to liq ({distance_to_liq:.4f}) "
                f"is only {cushion_ratio:.2f}x distance to SL ({distance_to_sl:.4f}), "
                f"below required 1.40x safety margin buffer (L={effective_leverage}x, MMF={mmf*100:.1f}%)."
            )
            self.logger.warning(f"[{self.name}] ❌ LIQUIDATION_RISK VETO: {msg}")
            return self._build_veto_result("LIQUIDATION_RISK", msg, trade_action)
        
        self.logger.info(
            f"[{self.name}] Final Effective Leverage: {effective_leverage}x "
            f"(Profile Max: {max_leverage}x, Volatility Max: {vol_max_leverage}x, "
            f"Liq Safe Max: {liq_safe_ceiling:.0f}x, Required: {required_leverage:.1f}x, Liq Price: {liq_price:.4f}, Cushion: {distance_to_liq/distance_to_sl:.2f}x)"
        )

        # ═══ Stage 8: Rounding, Increments & Exchange Sanity ═══
        size_increment = float(derivatives_data.get("size_increment") or 0.0)
        price_increment = float(derivatives_data.get("price_increment") or 0.0)
        is_strict_metadata = bool(derivatives_data.get("strict_metadata", False))
        if size_increment <= 0:
            if is_strict_metadata:
                msg = f"Market metadata incomplete: missing or non-positive size_increment ({size_increment}) for {symbol}. Refusing trade."
                self.logger.warning(f"[{self.name}] 🚫 MARKET_METADATA_UNAVAILABLE VETO: {msg}")
                return self._build_veto_result("MARKET_METADATA_UNAVAILABLE", msg, trade_action)
            self.logger.warning(
                f"[{self.name}] ⚠️ Missing or non-positive size_increment in derivatives_data for {symbol}. "
                f"Defaulting to safe fallback 0.001."
            )
            size_increment = 0.001
        if is_strict_metadata and price_increment <= 0:
            msg = f"Market metadata incomplete: missing or non-positive price_increment ({price_increment}) for {symbol}. Refusing trade."
            self.logger.warning(f"[{self.name}] 🚫 MARKET_METADATA_UNAVAILABLE VETO: {msg}")
            return self._build_veto_result("MARKET_METADATA_UNAVAILABLE", msg, trade_action)
        if size_increment > 0:
            if is_trigger_bumped:
                steps = math.ceil(round(contracts / size_increment, 8))
            else:
                steps = math.floor(round(contracts / size_increment, 8))
            step_str = f"{size_increment:.10f}".rstrip("0")
            decimals = len(step_str.split(".")[1]) if "." in step_str else 0
            contracts = round(steps * size_increment, min(decimals, 8))
        notional_usd = round(contracts * execution_entry, 2)
        margin_usd = round(notional_usd / effective_leverage if effective_leverage > 0 else notional_usd, 2)
        margin_pct = round((margin_usd / total_balance) * 100 if total_balance > 0 else 0, 2)

        raw_min = derivatives_data.get("min_size")
        is_confirmed_min = bool(derivatives_data.get("confirmed_min_size") or derivatives_data.get("is_confirmed"))
        is_production_mode = getattr(config, "SHADOW_MODE", False) or getattr(config, "NADO_LIVE_TRADING_ENABLED", False) or getattr(config, "LIVE_TRADING_ENABLED", False)
        raw_min_notional = derivatives_data.get("min_notional")
        if is_production_mode or (raw_min_notional is not None and float(raw_min_notional or 0.0) >= 40.0):
            min_viable_floor = float(getattr(config, "MIN_NOTIONAL_FLOOR_USD", 40.0) or 40.0)
            min_notional = float(raw_min_notional or 0.0) if raw_min_notional is not None else 100.0
        else:
            min_viable_floor = float(raw_min_notional or 0.0)
            min_notional = float(raw_min_notional or 0.0)

        if raw_min is not None:
            try:
                min_size = float(raw_min)
                if not math.isfinite(min_size):
                    raise ValueError(f"Non-finite min_size {raw_min}")
            except (ValueError, TypeError):
                if is_strict_metadata:
                    msg = f"Market metadata incomplete: invalid min_size ({raw_min}) for {symbol}. Refusing trade in strict mode."
                    self.logger.warning(f"[{self.name}] 🚫 MARKET_METADATA_UNAVAILABLE VETO: {msg}")
                    return self._build_veto_result("MARKET_METADATA_UNAVAILABLE", msg, trade_action)
                min_size = size_increment

            if is_strict_metadata and min_size <= 0.0:
                msg = f"Market metadata incomplete: non-positive min_size ({min_size}) for {symbol}. Refusing trade in strict mode."
                self.logger.warning(f"[{self.name}] 🚫 MARKET_METADATA_UNAVAILABLE VETO: {msg}")
                return self._build_veto_result("MARKET_METADATA_UNAVAILABLE", msg, trade_action)

            # In strict mode, an unconfirmed 100.0 cannot be guessed
            if is_strict_metadata and min_size == 100.0 and not is_confirmed_min and size_increment > 0 and size_increment != 100.0:
                msg = f"Market metadata incomplete: unconfirmed dummy min_size (100.0) for {symbol}. Cannot confirm whether 100.0 is engine dummy or true minimum without confirmation."
                self.logger.warning(f"[{self.name}] 🚫 MARKET_METADATA_UNAVAILABLE VETO: {msg}")
                return self._build_veto_result("MARKET_METADATA_UNAVAILABLE", msg, trade_action)

            # In non-strict mode, retain legacy fallback for unconfirmed 100.0 or <= 0
            if not is_strict_metadata:
                is_dummy_default = (min_size == 100.0 and not is_confirmed_min and size_increment > 0 and size_increment != 100.0)
                if min_size <= 0.0 or (size_increment > 0 and min_size < size_increment) or is_dummy_default:
                    min_size = size_increment
        else:
            if is_strict_metadata:
                msg = f"Market metadata incomplete: missing min_size parameter for {symbol}. Refusing trade in strict mode."
                self.logger.warning(f"[{self.name}] 🚫 MARKET_METADATA_UNAVAILABLE VETO: {msg}")
                return self._build_veto_result("MARKET_METADATA_UNAVAILABLE", msg, trade_action)
            min_size = size_increment
        
        if notional_usd <= 0 or contracts <= 0:
            msg = "Calculated order size is 0. Blocking trade."
            self.logger.warning(f"[{self.name}] 🚫 ZERO SIZE VETO: {msg}")
            return self._build_veto_result("MIN_SIZE", msg, trade_action)
        elif min_size > 0 and contracts < min_size:
            msg = f"Calculated contracts ({contracts}) is below exchange minimum size ({min_size})."
            self.logger.warning(f"[{self.name}] 🚫 MIN_SIZE VETO: {msg}")
            return self._build_veto_result("MIN_SIZE", msg, trade_action)
        elif notional_usd < 35.0:
            msg = (
                f"VETO: MIN_NOTIONAL. Calculated notional (${notional_usd:.2f}) is below minimum floor "
                f"($35.00 minimum slot size). Refusing micro-dust trade to preserve portfolio slots."
            )
            self.logger.warning(f"[{self.name}] 🚫 MIN_NOTIONAL VETO: {msg}")
            return self._build_veto_result("MIN_NOTIONAL", msg, trade_action)
        elif notional_usd < 103.50:
            NADO_SIZE_INCREMENTS = {
                "LIT-USD": 1.0,
                "SOL-USD": 0.1,
                "HYPE-USD": 0.1,
                "ZEC-USD": 0.01,
                "BNB-USD": 0.005,
                "ETH-USD": 0.001,
                "BTC-USD": 0.00005,
            }
            canonical_sym = symbol.replace("/", "-").strip().upper()
            if "-" not in canonical_sym:
                canonical_sym = f"{canonical_sym}-USD"
            effective_inc = size_increment if size_increment > 0 else NADO_SIZE_INCREMENTS.get(canonical_sym, 0.0001)

            step_str = f"{effective_inc:.10f}".rstrip("0")
            decimals = len(step_str.split(".")[1]) if "." in step_str else 0
            target_bump = 104.0
            raw_bump_cntr = target_bump / execution_entry if execution_entry > 0 else 0.0
            bump_steps = math.ceil(round(raw_bump_cntr / effective_inc, 8))
            bump_contracts = round(bump_steps * effective_inc, min(decimals, 8))
            bump_notional = round(bump_contracts * execution_entry, 2)
            bump_risk = bump_contracts * effective_loss_per_contract
            bump_margin = round(bump_notional / effective_leverage if effective_leverage > 0 else bump_notional, 2)

            max_allowed_risk_budget = max(risk_amount_usd * 1.45, total_balance * 0.026) if total_balance > 0 else (risk_amount_usd * 1.45)
            can_safely_bump = (
                notional_usd >= 35.0
                and bump_notional >= 101.0
                and bump_notional <= max_notional_usd
                and bump_margin <= (usable_margin * 1.05)
                and bump_margin <= available_margin
                and bump_risk <= max_allowed_risk_budget
            )

            if can_safely_bump:
                self.logger.info(
                    f"[{self.name}] ⚡ Stage 8: Safely bumped sub-notional from ${notional_usd:.2f} to ${bump_notional:.2f} >= $101.00 "
                    f"(Contracts: {bump_contracts}, Margin: ${bump_margin:.2f} <= ${usable_margin:.2f}, Risk: ${bump_risk:.2f} <= ${max_allowed_risk_budget:.2f})."
                )
                contracts = bump_contracts
                notional_usd = bump_notional
                risk_amount_usd = round(bump_risk, 2)
                margin_usd = bump_margin
                margin_pct = round((margin_usd / total_balance) * 100 if total_balance > 0 else 0, 2)
                is_trigger_bumped = True
            else:
                msg = (
                    f"VETO: MIN_NOTIONAL (${notional_usd:.2f} < $101.00 exchange CLOB min_size). "
                    f"Calculated notional is below exchange minimum and cannot be safely bumped to $104.00 "
                    f"(bump risk ${bump_risk:.2f} vs cap ${max_allowed_risk_budget:.2f}, margin ${bump_margin:.2f} vs usable ${usable_margin:.2f})."
                )
                self.logger.warning(f"[{self.name}] 🚫 MIN_NOTIONAL VETO: {msg}")
                return self._build_veto_result("MIN_NOTIONAL", msg, trade_action)
        elif notional_usd > max_notional_usd:
            msg = f"Required notional ${notional_usd:.2f} exceeds max allowed ${max_notional_usd:.2f}."
            self.logger.warning(f"[{self.name}] 🚫 MAX NOTIONAL VETO: {msg}")
            return self._build_veto_result("MAX_MARGIN", msg, trade_action)
        elif margin_usd > available_margin:
            msg = f"Required margin ${margin_usd:.2f} exceeds available margin (${available_margin:.2f})."
            self.logger.warning(f"[{self.name}] ❌ INSUFFICIENT MARGIN VETO: {msg}")
            return self._build_veto_result("INSUFFICIENT_BALANCE", msg, trade_action)

        if notional_usd < 101.0:
            msg = f"VETO: MIN_NOTIONAL (${notional_usd:.2f} < $101.00 exchange CLOB min_size). Refusing order."
            self.logger.warning(f"[{self.name}] 🚫 MIN_NOTIONAL VETO: {msg}")
            return self._build_veto_result("MIN_NOTIONAL", msg, trade_action)

        # Re-anchor risk_amount_usd to actual rounded contracts
        actual_planned_risk_usd = contracts * effective_loss_per_contract
        if is_trigger_bumped:
            risk_amount_usd = round(min(actual_planned_risk_usd, max_allowed_risk_budget), 2)
        else:
            risk_amount_usd = round(actual_planned_risk_usd, 2)
        sl_price = round(sl_price, 6)
        tp_price = round(tp_price, 6)
        rr_ratio = round(realized_rr, 2)

        # Re-verify net profit and net RR post-rounding on actual contracts (Point 9)
        post_round_profit = contracts * unit_net_profit
        if post_round_profit <= 0:
            msg = f"Candidate trade has non-positive net profit (${post_round_profit:.4f}) after rounding to exchange step size."
            self.logger.warning(f"[{self.name}] ❌ POOR_RISK_REWARD VETO: {msg}")
            return self._build_veto_result("POOR_RISK_REWARD", msg, trade_action)
        if candidate_net_rr < min_rr_required:
            msg = f"Candidate net RR ({candidate_net_rr:.2f}) is below minimum required {min_rr_required:.2f}:1 after rounding."
            self.logger.warning(f"[{self.name}] ❌ POOR_RISK_REWARD VETO: {msg}")
            return self._build_veto_result("POOR_RISK_REWARD", msg, trade_action)

        # Unified Loss Estimation & Verification (P0-3 & P1-2 & P1-3 & P1-4)
        from core.risk_calculator import estimate_total_loss, calculate_max_tolerated_risk, calculate_max_allowed_stress_loss
        taker_fee_pct = float(derivatives_data.get("taker_fee_pct") or 0.0005)
        slippage_stress = float(derivatives_data.get("slippage_stress_pct") or expected_slippage_pct)
        
        # Preserve genuine 0.0 funding without overriding with non-zero defaults (Point 8)
        raw_fr_final = derivatives_data.get("funding_rate_hourly")
        if raw_fr_final is None:
            raw_fr_final = derivatives_data.get("funding_rate")
        funding_rate_hourly = float(raw_fr_final) if (raw_fr_final is not None and str(raw_fr_final).strip() != "") else 0.0
        
        # Strategy-dependent holding hours
        if strategy_mode == "VOLATILITY_MOMENTUM":
            holding_hours = 1.0
        elif strategy_mode == "MEAN_REVERSION":
            holding_hours = 2.0
        else:
            holding_hours = 4.0

        try:
            loss_calc = estimate_total_loss(
                contracts=contracts,
                fill_price=execution_entry,
                sl_price=sl_price,
                taker_fee_pct=taker_fee_pct,
                slippage_stress_pct=slippage_stress,
                funding_rate_hourly=funding_rate_hourly,
                holding_hours=holding_hours,
                approved_risk=risk_amount_usd,
                is_long=(decision == "LONG")
            )
        except ValueError as val_err:
            msg = f"Invalid risk parameters for loss calculation: {val_err}"
            self.logger.warning(f"[{self.name}] ❌ DATA_INVALID VETO: {msg}")
            return self._build_veto_result("DATA_INVALID", msg, trade_action)

        # 1. Total Expected Loss Veto Gate (Price loss + round-trip taker fees)
        # Uniform centralized tolerance policy (Point 4)
        max_allowed_expected_loss = calculate_max_tolerated_risk(risk_amount_usd)
        if loss_calc.total_expected_loss_usd > max_allowed_expected_loss:
            msg = (
                f"Total expected loss (${loss_calc.total_expected_loss_usd:.2f}, "
                f"price=${loss_calc.price_loss_usd:.2f} + fees=${loss_calc.fee_loss_usd:.2f}) "
                f"exceeds approved risk budget (${risk_amount_usd:.2f} limit: ${max_allowed_expected_loss:.2f})."
            )
            self.logger.warning(f"[{self.name}] ❌ EXCESSIVE_RISK VETO: {msg}")
            return self._build_veto_result("EXCESSIVE_RISK", msg, trade_action)

        # 2. Stress Loss Veto Gate (Adverse slippage + holding funding stress)
        # Uniform centralized stress risk ceiling (Point 4)
        max_allowed_stress_loss = calculate_max_allowed_stress_loss(risk_amount_usd)
        if loss_calc.total_stress_loss_usd > max_allowed_stress_loss:
            msg = (
                f"Total stress loss (${loss_calc.total_stress_loss_usd:.2f}, "
                f"slippage=${loss_calc.slippage_stress_usd:.2f}, funding=${loss_calc.funding_loss_usd:.2f}) "
                f"exceeds stress risk ceiling (${max_allowed_stress_loss:.2f}, budget=${risk_amount_usd:.2f})."
            )
            self.logger.warning(f"[{self.name}] ❌ EXCESSIVE_STRESS_RISK VETO: {msg}")
            return self._build_veto_result("EXCESSIVE_STRESS_RISK", msg, trade_action)

        # ═══ Stage 8 Final Verification: Enforce Minimum Viable Floor ($40.00) ═══
        notional_usd = round(contracts * execution_entry, 2)
        if notional_usd < 101.0:
            msg = (
                f"VETO: MIN_NOTIONAL (${notional_usd:.2f} < $101.00 exchange CLOB min_size). "
                f"Final verified notional is below exchange minimum notional floor ($101.00)."
            )
            self.logger.warning(f"[{self.name}] 🚫 MIN_NOTIONAL VETO: {msg}")
            return self._build_veto_result("MIN_NOTIONAL", msg, trade_action)

        is_software_tpsl = False
        final_reasoning = f"Math calculated based on {profile_name} profile. SL={sl_price}, TP={tp_price}, RR={rr_ratio}"
        planned_price_risk_usd = round(contracts * distance_to_sl, 2)
        return {
            "approved": True,
            "veto_category": None,
            "trade_action": trade_action,
            "reasoning": final_reasoning,
            "is_software_tpsl": is_software_tpsl,
            "software_tpsl": is_software_tpsl,
            "use_software_tpsl": is_software_tpsl,
            "software_sl_tp_only": is_software_tpsl,
            "execution_mode": "SOFTWARE_TPSL" if is_software_tpsl else "NATIVE_TPSL",
            "risk_amount_usd": risk_amount_usd,
            "approved_risk_usd": risk_amount_usd,
            "planned_risk_usd": risk_amount_usd,
            "planned_price_risk_usd": planned_price_risk_usd,
            "base_risk_pct": base_risk,
            "final_risk_pct": dynamic_risk_pct,
            "cohort": cohort,
            "applied_multipliers": applied_multipliers,
            "expected_fee_usd": loss_calc.fee_loss_usd,
            "total_expected_loss_usd": loss_calc.total_expected_loss_usd,
            "total_stress_loss_usd": loss_calc.total_stress_loss_usd,
            "max_tolerated_risk_usd": loss_calc.max_tolerated_risk_usd,
            "max_allowed_stress_loss_usd": round(max_allowed_stress_loss, 2),
            "funding_rate_hourly": funding_rate_hourly,
            "holding_hours": holding_hours,
            "slippage_stress_pct": slippage_stress,
            "notional_size_usd": notional_usd,
            "notional_usd": notional_usd,
            "margin_usd": margin_usd,
            "leverage": effective_leverage,
            "effective_leverage": effective_leverage,
            "contracts": contracts,
            "margin_pct": margin_pct,
            "entry_price": execution_entry,
            "expected_slippage_pct": expected_slippage_pct,
            "take_profit_price": tp_price,
            "tp_price": tp_price,
            "take_profit_pct": round(abs(tp_price - execution_entry) / execution_entry * 100, 2) if execution_entry > 0 else 0,
            "stop_loss_price": sl_price,
            "sl_price": sl_price,
            "stop_loss_pct": round(abs(sl_price - execution_entry) / execution_entry * 100, 2) if execution_entry > 0 else 0,
            "risk_reward_ratio": rr_ratio,
            "candidate_net_rr": round(candidate_net_rr, 2),
            "historical_payoff_ratio": round(hist_payoff, 2),
            "liquidation_price": liq_price,
            "estimated_liquidation_price": liq_price,
            "is_estimated_liquidation": True,
            "limit_price": expected_limit_price,
            "slip_buffer": expected_slip_buffer,
            "maintenance_margin_pct": mmf,
            "position_size_pct": margin_pct,
            "bayes_strategy_multiplier": bayes_mult,
            "bayes_posterior_win_prob": bayes_prob,
            "bayes_strategy_bucket": f"{regime_tag}:{strat_tag}",
        }

