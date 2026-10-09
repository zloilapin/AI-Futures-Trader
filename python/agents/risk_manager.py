import os
import json
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

class RiskManager(BaseAgent):
    """
    Gatekeeper agent responsible for capital preservation, risk profiles, and position sizing.
    Calculates exact Take Profit (TP), Stop Loss (SL), position amount (USD / %), and risk/reward ratio.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Risk_Manager", logger, llm_client)

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
                "tp_mult": 2.2,
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
        if strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"]:
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

    def _calculate_drawdown_multiplier(self, streak: list) -> tuple[float, str]:
        """
        State Machine Drawdown Recovery:
        - Consecutive losses:
            >= 3 losses -> REDUCED_25 (0.25x)
            == 2 losses -> REDUCED_50 (0.50x)
        - Recovery transitions from REDUCED_25:
            * Next is WIN -> RECOVERY_50 (0.50x probation)
            * If next after probation is LOSS -> drops right back to REDUCED_25 (0.25x)!
            * If next after probation is WIN -> full recovery to NORMAL (1.00x)!
        """
        if not streak:
            return 1.0, "NORMAL"

        consecutive_losses = 0
        for res in reversed(streak):
            if res == "LOSS":
                consecutive_losses += 1
            else:
                break

        if consecutive_losses >= 3:
            return 0.25, "🚨 RED ALERT: 3+ losses in a row. Risk cut to 25%."
        elif consecutive_losses == 2:
            return 0.50, "DRAWDOWN PROTECTION: 2 losses in a row. Risk cut to 50%."

        # Check recovery state machine
        if len(streak) >= 4:
            if streak[-1] == "LOSS" and len(streak) >= 5 and streak[-5:-2] == ["LOSS", "LOSS", "LOSS"] and streak[-2] == "WIN":
                return 0.25, "🚨 FAILED RECOVERY: Loss during probation after 3-loss streak. Risk reset to 25%."
            elif streak[-1] == "WIN" and streak[-4:-1] == ["LOSS", "LOSS", "LOSS"]:
                return 0.50, "PROBATION RECOVERY: 1 win after 3 losses. Risk kept at 50%."

        return 1.0, "NORMAL"

    def _calculate_kelly_multiplier(self, win_rate: float, realized_rr: float, total_trades: int) -> float:
        """
        Calculates Fractional (Half) Kelly sizing multiplier.
        f* = (p * b - (1 - p)) / b
        where:
          p = win rate (0.0 to 1.0)
          b = realized reward-to-risk ratio
        Clamped to [0.50, 1.25] to prevent overleveraging while rewarding statistical edge.
        Only activated when total_trades >= 15.
        """
        if total_trades < 15 or realized_rr <= 0 or win_rate <= 0:
            return 1.0
        
        q = 1.0 - win_rate
        f_star = (win_rate * realized_rr - q) / realized_rr
        
        # Half-Kelly multiplier centered at 1.0:
        if f_star <= 0:
            return 0.50
        
        half_kelly_mult = 1.0 + (0.5 * f_star)
        return max(0.50, min(1.25, half_kelly_mult))

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

    async def analyze(self, ceo_decision: Dict[str, Any], portfolio_data: Dict[str, Any], market_data: Dict[str, Any], effective_profile: str = "BALANCED", macro_regime: str = "RANGE_CHOPPY", strategy_mode: str = "TREND_FOLLOWING") -> Dict[str, Any]:
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
        current_price = float(price_data.get("current_price", 0.0) or 0.0)
        
        indicators = market_data.get("indicators", {})
        if not isinstance(indicators, dict):
            indicators = {}
        atr_14 = float(indicators.get("atr_14", 0) or 0)
        
        total_balance = float(portfolio_data.get("total_usd", portfolio_data.get("current_balance", 0.0)) or 0.0)
        available_margin = float(portfolio_data.get("available_margin", total_balance))
        max_leverage = profile_rules.get("max_leverage", 10.0)
        max_notional_usd = total_balance * max_leverage
        
        # ═══ Stage 1: Deterministic Guard & Static Input Checks ═══
        if hasattr(ceo_decision, "is_actionable") and not getattr(ceo_decision, "is_actionable", True):
            veto_cat = getattr(ceo_decision, "rejection_tag", None) or "GUARD_VETO"
            veto_reason = getattr(ceo_decision, "guard_reason", "") or f"Blocked by Deterministic Guard: {veto_cat}"
            self.logger.warning(f"[{self.name}] 🚫 DETERMINISTIC GUARD VETO: {veto_reason}")
            return self._build_veto_result(veto_cat, veto_reason, trade_action)
        elif current_price <= 0:
            self.logger.warning(f"[{self.name}] ❌ INVALID PRICE: current_price is {current_price}. Blocking trade.")
            return self._build_veto_result("INVALID_PRICE", f"Missing or non-positive current price ({current_price}).", trade_action)
        elif atr_14 <= 0:
            self.logger.warning(f"[{self.name}] ❌ INVALID ATR: atr_14 is {atr_14}. Blocking trade to prevent corrupted risk sizing.")
            return self._build_veto_result("INVALID_ATR", "Missing or zero ATR_14 data.", trade_action)
        elif total_balance <= 0:
            self.logger.warning(f"[{self.name}] ❌ INSUFFICIENT BALANCE: Total balance is {total_balance}. Blocking trade.")
            return self._build_veto_result("INSUFFICIENT_BALANCE", f"Total balance is non-positive (${total_balance:.2f}).", trade_action)
        elif trade_action == "WAIT_FOR_PULLBACK":
            msg = "CEO recommended WAIT_FOR_PULLBACK (Overheated market condition). Blocking trade execution."
            self.logger.warning(f"[{self.name}] 🚫 WAIT_FOR_PULLBACK VETO: {msg}")
            return self._build_veto_result("WAIT_FOR_PULLBACK", msg, "WAIT_FOR_PULLBACK")
        elif decision not in ["LONG", "SHORT"]:
            return self._build_veto_result("NO_SIGNAL", f"Decision is {decision}", "HOLD")
        elif conviction < min_conviction:
            return self._build_veto_result("LOW_CONFIDENCE", f"Conviction {conviction} is below profile threshold {min_conviction}", trade_action)

        # ═══ Stage 2: Portfolio Health & Defense-in-Depth Gates ═══

        # Gate 2.1: Max Daily Drawdown Veto Gate
        max_daily_dd = profile_rules.get("max_daily_drawdown_pct", 0.06)
        raw_dd = portfolio_data.get("daily_drawdown_pct")
        if raw_dd is not None:
            dd_val = float(raw_dd or 0.0)
            daily_drawdown_pct = dd_val / 100.0 if dd_val > 1.0 else dd_val
        else:
            raw_pnl_pct = portfolio_data.get("daily_pnl_pct")
            if raw_pnl_pct is not None:
                pnl_val = float(raw_pnl_pct or 0.0)
                pnl_pct_dec = pnl_val / 100.0 if abs(pnl_val) > 1.0 else pnl_val
                daily_drawdown_pct = max(0.0, -pnl_pct_dec) if pnl_pct_dec < 0 else 0.0
            else:
                raw_pnl_usd = portfolio_data.get("daily_pnl_usd")
                if raw_pnl_usd is not None and total_balance > 0:
                    pnl_usd = float(raw_pnl_usd or 0.0)
                    daily_drawdown_pct = max(0.0, -pnl_usd / total_balance) if pnl_usd < 0 else 0.0
                else:
                    daily_drawdown_pct = 0.0

        if daily_drawdown_pct >= max_daily_dd:
            msg = f"Daily drawdown limit reached ({daily_drawdown_pct*100:.2f}% >= {max_daily_dd*100:.2f}% limit). Trading halted to protect capital."
            self.logger.warning(f"[{self.name}] 🚨 MAX_DAILY_DRAWDOWN VETO: {msg}")
            return self._build_veto_result("MAX_DAILY_DRAWDOWN", msg, trade_action)

        # Gate 2.2: Maximum Concurrent Positions Veto Gate
        active_positions = portfolio_data.get("active_positions", {})
        if not isinstance(active_positions, (dict, list)):
            active_positions = {}
        max_positions = profile_rules.get("max_concurrent_positions", getattr(config, "MAX_CONCURRENT_POSITIONS", 3))
        num_active = len(active_positions)
        if num_active >= max_positions:
            msg = f"Maximum concurrent positions reached ({num_active} >= {max_positions} limit)."
            self.logger.warning(f"[{self.name}] 🚫 MAX_CONCURRENT_POSITIONS VETO: {msg}")
            return self._build_veto_result("MAX_CONCURRENT_POSITIONS", msg, trade_action)

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
            if base_sym in active_bases or canonical_sym in active_keys or clean_sym in active_keys:
                msg = f"Position for {symbol} ({canonical_sym}) is already active. Duplicate entries prohibited."
                self.logger.warning(f"[{self.name}] 🚫 ALREADY_OPEN VETO: {msg}")
                return self._build_veto_result("ALREADY_OPEN", msg, trade_action)

        # Gate 2.4: Total Portfolio Open Risk Budget Cap
        MAX_TOTAL_PORTFOLIO_RISK_PCT = profile_rules.get("portfolio_risk_cap", 0.03)
        max_portfolio_risk_usd = total_balance * MAX_TOTAL_PORTFOLIO_RISK_PCT
        
        existing_risk_usd = 0.0
        pos_items = active_positions.values() if isinstance(active_positions, dict) else active_positions
        for pos in pos_items:
            if isinstance(pos, dict):
                entry = float(pos.get("entry_price", 0) or 0)
                sl = float(pos.get("sl_price", 0) or 0)
                amount = abs(float(pos.get("amount", 0) or 0))
                
                if amount > 0:
                    if entry > 0 and sl > 0:
                        pos_risk = amount * abs(entry - sl)
                    else:
                        pos_risk = float(pos.get("size_usd", amount * entry) or 0)
                else:
                    pos_risk = 0.0
                    
                existing_risk_usd += pos_risk
                
        remaining_risk_budget_usd = max(0.0, max_portfolio_risk_usd - existing_risk_usd)
        if remaining_risk_budget_usd < (total_balance * 0.002):
            msg = f"Total open risk budget exhausted: Active risk ${existing_risk_usd:.2f} >= Cap ${max_portfolio_risk_usd:.2f} ({MAX_TOTAL_PORTFOLIO_RISK_PCT*100}% limit)."
            self.logger.warning(f"[{self.name}] 🚫 TOTAL OPEN RISK VETO: {msg}")
            return self._build_veto_result("TOTAL_OPEN_RISK_CAP", msg, trade_action)

        # ═══ Stage 3: Dynamic Price Distances & Slippage Alignment ═══
        base_asset = symbol.split('-')[0].upper() if symbol else ""
        if base_asset in ["BTC", "ETH"]:
            expected_slippage_pct = 0.005 # 0.5%
        else:
            expected_slippage_pct = 0.01  # 1.0%
            
        if decision == "LONG":
            execution_entry = current_price * (1.0 + expected_slippage_pct)
        else:
            execution_entry = current_price * (1.0 - expected_slippage_pct)

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
            # Scalping mode with tight SL (0.5% min) and TP (1.0% min)
            sl_dist_base = max(atr_14 * sl_mult, current_price * 0.005)
            tp_dist_base = max(atr_14 * tp_mult, current_price * 0.010)
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
        funding_rate = float(derivatives_data.get("funding_rate") or 0.0001)
        funding_cost_pct = funding_rate if decision == "LONG" else -funding_rate
        
        unit_entry_fee = execution_entry * (fee_pct / 2)
        unit_tp_exit_fee = tp_price * (fee_pct / 2)
        unit_sl_exit_fee = sl_price * (fee_pct / 2)
        unit_funding_cost = execution_entry * funding_cost_pct
        
        unit_net_profit = distance_to_tp - unit_entry_fee - unit_tp_exit_fee - unit_funding_cost
        unit_net_loss = distance_to_sl + unit_entry_fee + unit_sl_exit_fee + unit_funding_cost
        realized_rr = unit_net_profit / unit_net_loss if unit_net_loss > 0 else 0.0

        # Strict Risk/Reward Floor Validation:
        # For Mean Reversion, capping TP to the mean must not reduce net RR below 1.00:1
        if strategy_mode == "MEAN_REVERSION" and realized_rr < 1.0:
            msg = f"Mean Reversion net RR ({realized_rr:.2f}) is below minimum acceptable 1.00:1 ratio after TP mean cap and fees."
            self.logger.warning(f"[{self.name}] ❌ POOR_RISK_REWARD VETO: {msg}")
            return self._build_veto_result("POOR_RISK_REWARD", msg, trade_action)
        elif realized_rr <= 0.0:
            msg = f"{strategy_mode} net profit is non-positive ({realized_rr:.2f} RR) after execution slippage and exchange fees."
            self.logger.warning(f"[{self.name}] ❌ POOR_RISK_REWARD VETO: {msg}")
            return self._build_veto_result("POOR_RISK_REWARD", msg, trade_action)

        # Expectancy Gate with Hysteresis (N >= 30 trades)
        win_count = int(portfolio_data.get("win_count", 0) or 0)
        loss_count = int(portfolio_data.get("loss_count", 0) or 0)
        total_trades = win_count + loss_count
        win_rate = (win_count / total_trades) if total_trades > 0 else 0.0
        expectancy_penalty_active = getattr(self, "_expectancy_penalty_active", False)
        
        if total_trades >= 30:
            expected_r = (win_rate * realized_rr) - (1.0 - win_rate)
            
            # Hysteresis: trigger at E < -0.10R, clear at E > +0.05R
            if not expectancy_penalty_active and expected_r < -0.10:
                self._expectancy_penalty_active = True
                expectancy_penalty_active = True
            elif expectancy_penalty_active and expected_r > 0.05:
                self._expectancy_penalty_active = False
                expectancy_penalty_active = False
                
            if expectancy_penalty_active:
                penalty_factor = 0.75
                self.logger.warning(
                    f"[{self.name}] ⚠️ EXPECTANCY GATE ACTIVE: Realized Expected R is {expected_r:.2f}R (< -0.10R threshold on {total_trades} trades). "
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
        dd_mult, dd_msg = self._calculate_drawdown_multiplier(recent_streak)
        if dd_mult < 1.0:
            self.logger.warning(f"[{self.name}] {dd_msg}")

        # 3. Fractional (Half) Kelly Multiplier (Active at N >= 15 trades)
        kelly_mult = self._calculate_kelly_multiplier(win_rate, realized_rr, total_trades) if total_trades >= 15 else 1.0

        # 4. Conviction and Action Multipliers
        conf_mult = self._get_conviction_multiplier(min_conviction, conviction)
        action_mult = 0.75 if trade_action == "REDUCE_SIZE" else 1.0

        # Combine into dynamic risk percentage
        dynamic_risk_pct = base_risk * conf_mult * quality_mult * dd_mult * action_mult * kelly_mult
        dynamic_risk_pct = min(dynamic_risk_pct, risk_cap)
        
        self.logger.info(
            f"[{self.name}] Final Risk: {dynamic_risk_pct*100:.2f}% (Base: {base_risk*100}%, Conf: x{conf_mult}, "
            f"Quality: x{quality_mult:.2f}, DD: x{dd_mult}, Kelly: x{kelly_mult:.2f}, Action: x{action_mult}, Cap: {risk_cap*100}%)"
        )

        # ═══ Stage 6: Position Sizing (Canonical Math) ═══
        risk_amount_usd = total_balance * dynamic_risk_pct
        if remaining_risk_budget_usd > 0:
            risk_amount_usd = min(risk_amount_usd, remaining_risk_budget_usd)

        if distance_to_sl > 0:
            contracts = risk_amount_usd / distance_to_sl
            notional_usd = contracts * execution_entry
            
            # Hard Cap on Max Account Notional
            if notional_usd > max_notional_usd:
                notional_usd = max_notional_usd
                contracts = notional_usd / execution_entry
                risk_amount_usd = contracts * distance_to_sl
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
            risk_amount_usd = contracts * distance_to_sl

        # ═══ Stage 7: Target Margin & Required Leverage ═══
        target_margin_pct = profile_rules.get("target_margin_pct", 0.10)
        usable_margin = min(available_margin * 0.8, total_balance * max_margin_pct)
        target_margin_usd = min(total_balance * target_margin_pct, usable_margin) * dd_mult
        
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
            
        safe_ceiling_leverage = min(max_leverage, vol_max_leverage)
        final_leverage = min(required_leverage, safe_ceiling_leverage)
        final_leverage = max(1.0, float(int(final_leverage)))
        
        # Enforce Target Margin strictly (Variant A: margin is the anchor)
        max_safe_notional = target_margin_usd * final_leverage
        if notional_usd > max_safe_notional:
            self.logger.warning(
                f"[{self.name}] Required margin at {final_leverage}x would exceed target margin. "
                f"Reducing notional from ${notional_usd:.2f} to ${max_safe_notional:.2f} to strictly enforce target."
            )
            notional_usd = max_safe_notional
            contracts = notional_usd / execution_entry if execution_entry > 0 else 0.0
            risk_amount_usd = contracts * distance_to_sl
            
        effective_leverage = final_leverage
        
        self.logger.info(
            f"[{self.name}] Final Effective Leverage: {effective_leverage}x "
            f"(Profile Max: {max_leverage}x, Volatility Max: {vol_max_leverage}x, "
            f"Required: {required_leverage:.1f}x)"
        )

        # ═══ Stage 8: Rounding, Increments & Exchange Sanity ═══
        size_increment = float(derivatives_data.get("size_increment") or 0.001)
        if size_increment > 0:
            contracts = (contracts // size_increment) * size_increment
        notional_usd = round(contracts * execution_entry, 2)
        margin_usd = round(notional_usd / effective_leverage if effective_leverage > 0 else notional_usd, 2)
        margin_pct = round((margin_usd / total_balance) * 100 if total_balance > 0 else 0, 2)

        min_size = float(derivatives_data.get("min_size") or 0.0)
        min_notional = float(derivatives_data.get("min_notional") or 0.0)
        
        if min_size > 0 and (min_size * current_price) > max_notional_usd:
            self.logger.info(f"[{self.name}] ℹ️ Reported min_size ({min_size}) exceeds max account notional (${max_notional_usd:.2f}). Treating as unconfigured testnet artifact.")
            min_size = 0.0
        
        if notional_usd <= 0 or contracts <= 0:
            msg = "Calculated order size is 0. Blocking trade."
            self.logger.warning(f"[{self.name}] 🚫 ZERO SIZE VETO: {msg}")
            return self._build_veto_result("MIN_SIZE", msg, trade_action)
        elif notional_usd > max_notional_usd:
            msg = f"Required notional ${notional_usd:.2f} exceeds max allowed ${max_notional_usd:.2f}."
            self.logger.warning(f"[{self.name}] 🚫 MAX NOTIONAL VETO: {msg}")
            return self._build_veto_result("MAX_MARGIN", msg, trade_action)
        elif margin_usd > available_margin:
            msg = f"Required margin ${margin_usd:.2f} exceeds available margin (${available_margin:.2f})."
            self.logger.warning(f"[{self.name}] ❌ INSUFFICIENT MARGIN VETO: {msg}")
            return self._build_veto_result("INSUFFICIENT_BALANCE", msg, trade_action)

        risk_amount_usd = round(risk_amount_usd, 2)
        sl_price = round(sl_price, 6)
        tp_price = round(tp_price, 6)
        rr_ratio = round(realized_rr, 2)

        # Verify Actual Risk with rounded contracts
        actual_risk_usd = contracts * distance_to_sl
        if actual_risk_usd > (risk_amount_usd * 1.05):
            msg = f"Actual risk ${actual_risk_usd:.2f} exceeds allowed risk ${risk_amount_usd:.2f}."
            self.logger.warning(f"[{self.name}] ❌ RISK VETO: {msg}")
            return self._build_veto_result("EXCESSIVE_RISK", msg, trade_action)

        final_reasoning = f"Math calculated based on {profile_name} profile. SL={sl_price}, TP={tp_price}, RR={rr_ratio}"
        return {
            "approved": True,
            "veto_category": None,
            "trade_action": trade_action,
            "reasoning": final_reasoning,
            "risk_amount_usd": risk_amount_usd,
            "notional_size_usd": notional_usd,
            "margin_usd": margin_usd,
            "leverage": effective_leverage,
            "effective_leverage": effective_leverage,
            "contracts": contracts,
            "margin_pct": margin_pct,
            "entry_price": execution_entry,
            "take_profit_price": tp_price,
            "take_profit_pct": round(abs(tp_price - execution_entry) / execution_entry * 100, 2) if execution_entry > 0 else 0,
            "stop_loss_price": sl_price,
            "stop_loss_pct": round(abs(sl_price - execution_entry) / execution_entry * 100, 2) if execution_entry > 0 else 0,
            "risk_reward_ratio": rr_ratio,
            "liquidation_price": 0.0,
            "position_size_pct": margin_pct,
        }

