import os
import json
import math
from typing import Dict, Any, Optional

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
                "sentinel_be_atr": 2.2,
                "sentinel_trail_atr": 3.0,
                "sentinel_trail_activation_atr": 3.0,
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
                "sentinel_be_atr": 1.5,
                "sentinel_trail_atr": 2.0,
                "sentinel_trail_activation_atr": 2.5,
                "sentinel_trail_distance_atr": 2.0,
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
                "sentinel_be_atr": 1.8,
                "sentinel_trail_atr": 2.5,
                "sentinel_trail_activation_atr": 2.5,
                "sentinel_trail_distance_atr": 1.5,
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
            rules["min_rr"] = 1.00 # Mean reversion requires at least 1:1 reward-to-risk
            
        # Slight penalty for non-trend strategies if the overall macro is highly conservative
        if profile == "CONSERVATIVE" and strategy_mode != "TREND_FOLLOWING":
            rules["min_conviction"] = min(90, rules["min_conviction"] + 5)
            
        return rules

    def _get_conviction_multiplier(self, min_conviction: int, conviction: int) -> float:
        """
        Relative asymmetric scaling: Scaled relative to the profile threshold.
        - threshold + 0-4%   -> 0.70x (boundary penalty)
        - threshold + 5-14%  -> 1.00x (base setup)
        - threshold + 15-19% -> 1.15x (high quality)
        - threshold + 20%+   -> 1.25x (ceiling)
        """
        if conviction < min_conviction:
            return 0.0
        delta = conviction - min_conviction
        if delta < 5:
            return 0.70
        elif delta < 15:
            return 1.00
        elif delta < 20:
            return 1.15
        else:
            return 1.25

    def _calculate_drawdown_multiplier(self, streak: list, daily_drawdown_pct: float = 0.0) -> tuple[float, str]:
        """
        Drawdown Recovery & Sizing Multiplier (Smooth Scale):
        1. Account daily drawdown percentage:
           - < 3%: 1.00x (routine intraday noise/normal single stop)
           - 3% to 5%: 0.75x
           - 5% to 10%: 0.50x
           - >= 10%: 0.35x
        2. Consecutive loss streak state machine:
           - 0 losses (last was WIN): 1.00x
           - 1 loss (single routine stop): 0.85x
           - 2 consecutive losses: 0.60x
           - >= 3 consecutive losses: 0.40x
        3. Recovery transitions:
           - Loss during probation after a 3-loss streak: 0.60x (not 0.25x deadlock)
           - 1 win after 3 losses: 0.75x probation
        Effective multiplier is min(pct_mult, streak_mult), clamped to >= 0.35x.
        """
        # 1. Daily drawdown tier
        if daily_drawdown_pct < 0.03:
            dd_pct_mult = 1.00
            dd_pct_msg = "NORMAL: Low drawdown (<3%)."
        elif daily_drawdown_pct < 0.05:
            dd_pct_mult = 0.75
            dd_pct_msg = "MILD_DRAWDOWN: Daily drawdown 3-5%."
        elif daily_drawdown_pct < 0.10:
            dd_pct_mult = 0.50
            dd_pct_msg = "MODERATE_DRAWDOWN: Daily drawdown 5-10%."
        else:
            dd_pct_mult = 0.35
            dd_pct_msg = "SEVERE_DRAWDOWN: Daily drawdown >=10%."

        # 2. Streak state machine
        if not streak:
            streak_mult = 1.00
            streak_msg = "NORMAL"
        else:
            consecutive_losses = 0
            for res in reversed(streak):
                if res == "LOSS":
                    consecutive_losses += 1
                else:
                    break

            if consecutive_losses >= 3:
                streak_mult = 0.40
                streak_msg = "🚨 RED ALERT: 3+ losses in a row. Risk cut to 40%."
            elif consecutive_losses == 2:
                streak_mult = 0.50
                streak_msg = "DRAWDOWN PROTECTION: 2 losses in a row. Risk cut to 50%."
            elif consecutive_losses == 1:
                # Check recovery probation state machine
                if len(streak) >= 5 and streak[-5:-2] == ["LOSS", "LOSS", "LOSS"] and streak[-2] == "WIN":
                    streak_mult = 0.60
                    streak_msg = "PROBATION: Loss during recovery after 3-loss streak. Risk set to 60%."
                else:
                    streak_mult = 0.85
                    streak_msg = "MILD_DRAWDOWN: 1 loss. Sizing at 85%."
            else:
                # Last was WIN: check if recovering from 3 losses
                if len(streak) >= 4 and streak[-4:-1] == ["LOSS", "LOSS", "LOSS"]:
                    streak_mult = 0.75
                    streak_msg = "PROBATION RECOVERY: 1 win after 3 losses. Risk kept at 75%."
                else:
                    streak_mult = 1.00
                    streak_msg = "NORMAL"

        final_mult = max(0.35, min(dd_pct_mult, streak_mult))
        final_msg = f"{streak_msg} | {dd_pct_msg} (Mult: {final_mult:.2f}x)"
        return final_mult, final_msg


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
        
        raw_total_balance = portfolio_data.get("total_usd", portfolio_data.get("current_balance", 0.0))
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

            # Derive implied drawdown from daily_pnl_pct or daily_pnl_usd
            implied_dd = 0.0
            if raw_pnl_pct is not None:
                pnl_val = float(raw_pnl_pct or 0.0)
                pnl_pct_dec = pnl_val / 100.0 if abs(pnl_val) > 1.0 else pnl_val
                implied_dd = max(0.0, -pnl_pct_dec) if pnl_pct_dec < 0 else 0.0
            elif raw_pnl_usd is not None and total_balance > 0:
                pnl_usd = float(raw_pnl_usd or 0.0)
                implied_dd = max(0.0, -pnl_usd / total_balance) if pnl_usd < 0 else 0.0

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

        # Gate 2.2: Maximum Concurrent Positions Veto Gate
        max_positions = profile_rules.get("max_concurrent_positions", getattr(config, "MAX_CONCURRENT_POSITIONS", 3))
        num_active = len(unique_positions)
        if num_active >= max_positions:
            msg = f"Maximum concurrent positions reached ({num_active} >= {max_positions} limit)."
            self.logger.warning(f"[{self.name}] 🚫 MAX_CONCURRENT_POSITIONS VETO: {msg}")
            return self._build_veto_result("MAX_CONCURRENT_POSITIONS", msg, trade_action)

        # Gate 2.2b: Directional Cluster Cap in Non-Trending / Choppy / Unknown Regimes
        # Prevents opening all positions in the same direction unless market is in a confirmed TRENDING regime.
        if str(macro_regime).upper() != "TRENDING":
            same_dir_count = 0
            for p in unique_positions:
                p_dir = str(p.get("direction", p.get("side", ""))).upper()
                if (decision == "LONG" and ("LONG" in p_dir or "BUY" in p_dir)) or \
                   (decision == "SHORT" and ("SHORT" in p_dir or "SELL" in p_dir)):
                    same_dir_count += 1
            if same_dir_count >= 2:
                msg = f"Directional cluster limit reached: {same_dir_count} active {decision} positions already open in {macro_regime} regime."
                self.logger.warning(f"[{self.name}] 🚫 DIRECTIONAL_CLUSTER_CAP VETO: {msg}")
                return self._build_veto_result("DIRECTIONAL_CLUSTER_CAP", msg, trade_action)

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
            pos_price_risk = size * abs(entry - sl)
            pos_fee_risk = size * sl * est_exit_fee_rate
            pos_risk = pos_price_risk + pos_fee_risk
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
            min_edge_pct = 0.008  # 0.8% minimum distance to mean to justify slippage & exchange fees
            if dist_to_mean < (current_price * min_edge_pct):
                msg = f"Distance to mean ({dist_to_mean:.2f}, {dist_to_mean/current_price*100:.2f}%) is too narrow to cover fees/slippage (< {min_edge_pct*100:.1f}% min edge)."
                self.logger.warning(f"[{self.name}] ❌ LOW_EDGE VETO: {msg}")
                return self._build_veto_result("LOW_EDGE", msg, trade_action)

            # Target 90% of distance to mean
            tp_dist_base = dist_to_mean * 0.90
            sl_dist_base = max(atr_14 * sl_mult, current_price * 0.008)
        elif strategy_mode == "VOLATILITY_MOMENTUM":
            # Scalping mode: BTC/ETH can tolerate tighter noise floors (0.6% / 1.2%),
            # while altcoins require at least 1.0% SL and 2.0% TP to prevent 1-min noise wicks from prematurely hitting stops
            min_sl_ratio = 0.006 if base_asset in ["BTC", "ETH"] else 0.010
            min_tp_ratio = 0.012 if base_asset in ["BTC", "ETH"] else 0.020
            sl_dist_base = max(atr_14 * sl_mult, current_price * min_sl_ratio)
            tp_dist_base = max(atr_14 * tp_mult, current_price * min_tp_ratio)
        else:
            sl_dist_base = max(atr_14 * sl_mult, current_price * config.MIN_SL_PCT)
            tp_dist_base = max(atr_14 * tp_mult, current_price * config.MIN_TP_PCT)

        if decision == "LONG":
            sl_price = current_price - sl_dist_base
            tp_price = current_price + tp_dist_base
        else: # SHORT
            sl_price = current_price + sl_dist_base
            tp_price = current_price - tp_dist_base

        # Strict Mean Reversion Guard: TP must NEVER cross or touch the mean target
        if strategy_mode == "MEAN_REVERSION" and mean_target > 0:
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
                   f"fees={unit_entry_fee + unit_tp_exit_fee:.4f}, funding={unit_funding_cost:.4f}")
            self.logger.warning(f"[{self.name}] ❌ POOR_RISK_REWARD VETO: {msg}")
            return self._build_veto_result("POOR_RISK_REWARD", msg, trade_action)

        # Expectancy Gate with Hysteresis (N >= 30 trades evaluated on HISTORICAL closed trades) (P1 Finding 1)
        win_count = int(portfolio_data.get("win_count", 0) or 0)
        loss_count = int(portfolio_data.get("loss_count", 0) or 0)
        total_trades = win_count + loss_count
        win_rate = (win_count / total_trades) if total_trades > 0 else 0.0
        expectancy_penalty_active = getattr(self, "_expectancy_penalty_active", False)
        
        hist_payoff = float(portfolio_data.get("historical_payoff_ratio", 0.0) or portfolio_data.get("historical_avg_rr", 0.0) or 0.0)
        if hist_payoff <= 0.0:
            avg_w = float(portfolio_data.get("avg_win_usd", 0.0) or 0.0)
            avg_l = float(portfolio_data.get("avg_loss_usd", 0.0) or 0.0)
            if avg_w > 0 and avg_l > 0:
                hist_payoff = avg_w / avg_l
            # If missing, remain 0.0 without fabricating unmeasured 1.0 assumption
        
        if total_trades >= 30 and hist_payoff > 0:
            historical_expected_r = (win_rate * hist_payoff) - (1.0 - win_rate)
            
            # Hysteresis: trigger at E < -0.10R, clear at E > +0.05R
            if not expectancy_penalty_active and historical_expected_r < -0.10:
                self._expectancy_penalty_active = True
                expectancy_penalty_active = True
            elif expectancy_penalty_active and historical_expected_r > 0.05:
                self._expectancy_penalty_active = False
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
        # 1. Volume & Spread Quality Multiplier
        volume_mult = 1.0
        ohlcv = market_data.get("price_data", {}).get("ohlcv_1h", [])
        if isinstance(ohlcv, list) and len(ohlcv) >= 10:
            avg_volume_10 = sum(float(c.get("volume", 0) or 0) for c in ohlcv[-10:]) / 10
            v1 = float(ohlcv[-1].get("volume", 0) or 0)
            if avg_volume_10 > 0 and v1 < avg_volume_10:
                volume_mult = 0.85
                
        spread_pct = float(market_data.get("order_book_data", {}).get("spread_pct", 0) or 0)
        spread_mult = 1.0
        if spread_pct > (config.SPREAD_PENALTY_THRESHOLD * 0.5):
            spread_mult = 0.90
            
        quality_mult = volume_mult * spread_mult

        # 2. Drawdown Streak Multiplier (Step-Wise Recovery)
        recent_streak = portfolio_data.get("recent_streak", [])
        dd_mult, dd_msg = self._calculate_drawdown_multiplier(recent_streak, daily_drawdown_pct)
        if dd_mult < 1.0:
            self.logger.warning(f"[{self.name}] {dd_msg}")

        # 3. Fractional (Half) Kelly Multiplier (Active at N >= 15 trades using historical payoff ratio)
        kelly_mult = self._calculate_kelly_multiplier(win_rate, hist_payoff, total_trades) if total_trades >= 15 else 1.0

        # 4. Conviction and Action Multipliers (Signal Edge)
        conf_mult = self._get_conviction_multiplier(min_conviction, conviction)
        action_mult = 0.75 if trade_action == "REDUCE_SIZE" else 1.0

        # Cohort Determination for mathematical expectancy analytics (Groups A, B, C, D):
        # A: High Conviction (>= min_conviction + 10), Full Size (action == ENTER)
        # B: High Conviction (>= min_conviction + 10), Reduced Size (action == REDUCE_SIZE)
        # C: Low Conviction (< min_conviction + 10), Full Size (action == ENTER)
        # D: Low Conviction (< min_conviction + 10), Reduced Size (action == REDUCE_SIZE)
        is_high_conv = conviction >= (min_conviction + 10)
        is_full_size = (trade_action != "REDUCE_SIZE")
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

        # Signal edge multiplier with dampening against excessive double-penalty:
        # If conviction is already near threshold (conf_mult = 0.70) AND trade_action is REDUCE_SIZE (0.75),
        # compounding 0.70 * 0.75 = 0.525 severely punishes trades.
        # We preserve action downscaling while setting a conservative signal floor (0.60x):
        raw_signal_mult = conf_mult * action_mult
        signal_mult = max(0.60, raw_signal_mult) if (conf_mult < 1.0 and action_mult < 1.0) else raw_signal_mult

        # Orthogonal Factor 2: Market Friction (Volume & Spread)
        market_mult = quality_mult

        # Orthogonal Factor 3: Portfolio Health (Drawdown streak & Kelly)
        portfolio_mult = dd_mult * kelly_mult

        # Combine into dynamic risk percentage with a safe floor check
        dynamic_risk_pct = base_risk * signal_mult * market_mult * portfolio_mult
        min_viable_risk = base_risk * 0.35  # Protect at least 35% of base risk to meet exchange minimums

        if dynamic_risk_pct < min_viable_risk:
            # If trade is a clean, high-conviction ENTER signal (Cohort A) and not degraded by market friction (spread/volume),
            # while account daily drawdown is safe (< 3%), clamp to min_viable_risk to prevent artificial deadlocks.
            if trade_action == "ENTER" and cohort == "A" and market_mult >= 0.85 and daily_drawdown_pct < 0.03:
                self.logger.info(
                    f"[{self.name}] 🛡️ Soft floor applied: Dynamic risk ({dynamic_risk_pct*100:.3f}%) clamped to min_viable_risk "
                    f"({min_viable_risk*100:.3f}%) for high-conviction Cohort A ENTER trade."
                )
                dynamic_risk_pct = min_viable_risk
            else:
                msg = (f"Dynamic risk ({dynamic_risk_pct*100:.3f}%) fell below minimum viable risk "
                       f"({min_viable_risk*100:.3f}%). Cumulative penalties (Signal x{signal_mult:.2f}, "
                       f"DD x{dd_mult}, Qual x{quality_mult:.2f}, Kelly x{kelly_mult:.2f}) suppress viable trade edge. Vetoing to protect capital.")
                self.logger.warning(f"[{self.name}] 🚫 PENALTY_SUPPRESSION VETO: {msg}")
                return self._build_veto_result("PENALTY_SUPPRESSION", msg, trade_action)


        dynamic_risk_pct = min(dynamic_risk_pct, risk_cap)
        
        applied_multipliers = {
            "conf_mult": conf_mult,
            "action_mult": action_mult,
            "signal_mult": round(signal_mult, 4),
            "quality_mult": round(quality_mult, 4),
            "dd_mult": round(dd_mult, 4),
            "kelly_mult": round(kelly_mult, 4),
            "combined_mult": round(dynamic_risk_pct / base_risk, 4) if base_risk > 0 else 1.0
        }

        self.logger.info(
            f"[{self.name}] Final Risk: {dynamic_risk_pct*100:.2f}% (Base: {base_risk*100}%, Signal: x{signal_mult:.2f} "
            f"[Conf: x{conf_mult}, Action: x{action_mult}], Market: x{market_mult:.2f}, "
            f"Portfolio: x{portfolio_mult:.2f} [DD: x{dd_mult}, Kelly: x{kelly_mult:.2f}], Cohort: {cohort}, Cap: {risk_cap*100}%)"
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
        
        # Enforce Target Margin strictly (Variant A: margin is the anchor)
        max_safe_notional = target_margin_usd * final_leverage
        if notional_usd > max_safe_notional:
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
            contracts = (contracts // size_increment) * size_increment
        notional_usd = round(contracts * execution_entry, 2)
        margin_usd = round(notional_usd / effective_leverage if effective_leverage > 0 else notional_usd, 2)
        margin_pct = round((margin_usd / total_balance) * 100 if total_balance > 0 else 0, 2)

        raw_min = derivatives_data.get("min_size")
        is_confirmed_min = bool(derivatives_data.get("confirmed_min_size") or derivatives_data.get("is_confirmed"))
        min_notional = float(derivatives_data.get("min_notional") or 0.0)

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
        elif min_notional > 0 and notional_usd < min_notional:
            msg = f"Calculated notional (${notional_usd:.2f}) is below exchange minimum notional (${min_notional:.2f})."
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

        # Re-anchor risk_amount_usd to actual rounded contracts
        actual_planned_risk_usd = contracts * effective_loss_per_contract
        risk_amount_usd = round(min(risk_amount_usd, actual_planned_risk_usd), 2)
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
                approved_risk=risk_amount_usd
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

        final_reasoning = f"Math calculated based on {profile_name} profile. SL={sl_price}, TP={tp_price}, RR={rr_ratio}"
        planned_price_risk_usd = round(contracts * distance_to_sl, 2)
        return {
            "approved": True,
            "veto_category": None,
            "trade_action": trade_action,
            "reasoning": final_reasoning,
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
            "margin_usd": margin_usd,
            "leverage": effective_leverage,
            "effective_leverage": effective_leverage,
            "contracts": contracts,
            "margin_pct": margin_pct,
            "entry_price": execution_entry,
            "expected_slippage_pct": expected_slippage_pct,
            "take_profit_price": tp_price,
            "take_profit_pct": round(abs(tp_price - execution_entry) / execution_entry * 100, 2) if execution_entry > 0 else 0,
            "stop_loss_price": sl_price,
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
        }

