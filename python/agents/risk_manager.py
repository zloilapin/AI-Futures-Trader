import os
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient
from core.config import config

class RiskManager(BaseAgent):
    """
    Gatekeeper agent responsible for capital preservation, risk profiles, and position sizing.
    Calculates exact Take Profit (TP), Stop Loss (SL), position amount (USD / %), and risk/reward ratio.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Risk_Manager", logger, llm_client)

    def _get_profile_rules(self, profile: str) -> dict:
        if profile == "AGGRESSIVE":
            return {"min_conviction": 65, "base_risk": 0.015, "risk_cap": 0.03, "sl_mult": 1.75, "tp_mult": 3.5, "target_margin_pct": 0.20, "max_margin_pct": 0.45, "max_leverage": 15}
        elif profile == "CONSERVATIVE":
            return {"min_conviction": 80, "base_risk": 0.005, "risk_cap": 0.01, "sl_mult": 2.0, "tp_mult": 3.0, "target_margin_pct": 0.05, "max_margin_pct": 0.10, "max_leverage": 5}
        else: # BALANCED
            return {"min_conviction": 70, "base_risk": 0.01, "risk_cap": 0.015, "sl_mult": 1.5, "tp_mult": 2.5, "target_margin_pct": 0.10, "max_margin_pct": 0.20, "max_leverage": 10}

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

    async def analyze(self, ceo_decision: Dict[str, Any], portfolio_data: Dict[str, Any], market_data: Dict[str, Any], effective_profile: str = "BALANCED") -> Dict[str, Any]:
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
        profile_rules = self._get_profile_rules(effective_profile)
        min_conviction = profile_rules["min_conviction"]
        base_risk = profile_rules["base_risk"]
        risk_cap = profile_rules["risk_cap"]
        sl_mult = profile_rules["sl_mult"]
        tp_mult = profile_rules["tp_mult"]
        max_margin_pct = profile_rules["max_margin_pct"]
        
        profile_name = effective_profile
        
        self.logger.info(f"[{self.name}] Расчет математики риска по профилю: {profile_name} (Base Risk: {base_risk*100}%)...")
        
        decision = ceo_decision.get("decision", "HOLD")
        conviction = ceo_decision.get("conviction", 0)
        
        price_data = market_data.get("price_data", {})
        current_price = float(price_data.get("current_price", 1.0) or 1.0)
        
        indicators = market_data.get("indicators", {})
        atr_14 = float(indicators.get("atr_14", current_price * 0.02) or current_price * 0.02)
        
        total_balance = float(portfolio_data.get("total_usd", portfolio_data.get("current_balance", 0.0)) or 0.0)
        available_margin = float(portfolio_data.get("available_margin", total_balance))
        
        # Initialized output fields
        approved = None
        veto_category = None
        veto_reason = ""
        risk_amount_usd = 0.0   # Max USD we're willing to lose
        notional_usd = 0.0      # Total exposure (contracts * price)
        margin_usd = 0.0        # Collateral locked (notional / leverage)
        contracts = 0.0          # Base asset amount
        margin_pct = 0.0         # margin_usd as % of balance
        sl_price = 0.0
        tp_price = 0.0
        rr_ratio = 0.0
        liq_price = 0.0
        
        if total_balance <= 0:
            self.logger.warning(f"[{self.name}] ❌ INSUFFICIENT BALANCE: Total balance is {total_balance}. Blocking trade.")
            approved = False
            veto_category = "INSUFFICIENT_BALANCE"
        elif decision in ["LONG", "SHORT"] and conviction >= min_conviction:
            
            # --- Portfolio Correlated Exposure Check ---
            # Maximum total portfolio risk across all open positions (3.0% of balance)
            MAX_TOTAL_PORTFOLIO_RISK_PCT = 0.03
            max_portfolio_risk_usd = total_balance * MAX_TOTAL_PORTFOLIO_RISK_PCT
            
            existing_risk_usd = 0.0
            active_positions = portfolio_data.get("active_positions", {})
            for pos_sym, pos in active_positions.items():
                if isinstance(pos, dict):
                    pos_notional = float(pos.get("notional_usd", 0.0))
                    pos_risk = float(pos.get("risk_amount_usd", pos_notional * 0.02))
                    existing_risk_usd += pos_risk
                    
            remaining_risk_budget_usd = max(0.0, max_portfolio_risk_usd - existing_risk_usd)
            
            if remaining_risk_budget_usd < (total_balance * 0.002):
                msg = f"Portfolio correlated risk budget exhausted: Active risk ${existing_risk_usd:.2f} >= Cap ${max_portfolio_risk_usd:.2f} (3% limit)."
                self.logger.warning(f"[{self.name}] 🚫 PORTFOLIO RISK VETO: {msg}")
                return {
                    "approved": False,
                    "veto_category": "PORTFOLIO_RISK_CAP",
                    "reasoning": msg
                }

            # --- Expectancy Gate with Hysteresis (N >= 30 trades) ---
            win_count = portfolio_data.get("win_count", 0)
            loss_count = portfolio_data.get("loss_count", 0)
            total_trades = win_count + loss_count
            
            expectancy_penalty_active = getattr(self, "_expectancy_penalty_active", False)
            
            if total_trades >= 30:
                win_rate = win_count / total_trades
                target_rr = tp_mult / sl_mult
                expected_r = (win_rate * target_rr) - (1.0 - win_rate)
                
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
                        f"[{self.name}] ⚠️ EXPECTANCY GATE ACTIVE (Hysteresis ON): Expected R is {expected_r:.2f}R (< -0.10R threshold on {total_trades} trades). "
                        f"WR: {win_rate*100:.1f}%. Applying risk penalty (x{penalty_factor}) and raising min_conviction."
                    )
                    min_conviction = min(95, min_conviction + 5)
                    base_risk *= penalty_factor
                    
            if conviction < min_conviction:
                return {
                    "approved": False,
                    "veto_category": "LOW_CONFIDENCE",
                    "reasoning": f"Conviction {conviction} is below Win Rate Gate threshold {min_conviction}"
                }

            # Calculate ATR based SL and TP with a minimum floor to avoid noise
            if decision == "LONG":
                sl_dist = max(atr_14 * sl_mult, current_price * config.MIN_SL_PCT)
                tp_dist = max(atr_14 * tp_mult, current_price * config.MIN_TP_PCT)
                sl_price = current_price - sl_dist
                tp_price = current_price + tp_dist
            else: # SHORT
                sl_dist = max(atr_14 * sl_mult, current_price * config.MIN_SL_PCT)
                tp_dist = max(atr_14 * tp_mult, current_price * config.MIN_TP_PCT)
                sl_price = current_price + sl_dist
                tp_price = current_price - tp_dist
                
            distance_to_sl = abs(current_price - sl_price)
            distance_to_tp = abs(tp_price - current_price)
            
            # Slippage Penalty
            spread_pct = float(market_data.get("order_book_data", {}).get("spread_pct", 0))
            if spread_pct > config.SPREAD_PENALTY_THRESHOLD:
                self.logger.warning(f"[{self.name}] High spread detected ({spread_pct}%). Applying slippage penalty.")
                
            # --- Market Regime Check (Trend-Following vs Chop) ---
            mtf_data = market_data.get("multi_timeframe", {})
            mtf_alignment = mtf_data.get("mtf_alignment", "MIXED_CHOP")
            
            if mtf_alignment == "MIXED_CHOP":
                msg = f"Market regime is MIXED_CHOP (15m: {mtf_data.get('trend_15m')}, 1h: {mtf_data.get('trend_1h')}, 4h: {mtf_data.get('trend_4h')}). Trend-following system cannot trade in chop."
                self.logger.warning(f"[{self.name}] 🚫 REGIME VETO: {msg}")
                return {
                    "approved": False,
                    "veto_category": "REGIME_VETO",
                    "reasoning": msg
                }
            elif mtf_alignment == "FULL_ALIGNMENT":
                regime_mult = 1.0
            elif mtf_alignment == "COUNTER_TREND_WARNING":
                regime_mult = 0.8  # Pullback in prevailing 1h/4h trend
            else:
                regime_mult = 0.8

            # --- Multi-Factor Signal Quality ---
            # 1. Volume Confirmation: Current volume vs 10-period SMA
            volume_mult = 1.0
            ohlcv = price_data.get("candles_20", [])
            if len(ohlcv) >= 10:
                avg_volume_10 = sum(float(c.get("volume", 0)) for c in ohlcv[-10:]) / 10
                v1 = float(ohlcv[-1].get("volume", 0))
                if avg_volume_10 > 0 and v1 < avg_volume_10:
                    volume_mult = 0.85
                    
            # 2. Spread Quality
            spread_mult = 1.0
            if spread_pct > (config.SPREAD_PENALTY_THRESHOLD * 0.5):
                spread_mult = 0.90
                
            quality_mult = volume_mult * spread_mult
            
            # --- Drawdown Protection (Step-Wise Recovery) ---
            recent_streak = portfolio_data.get("recent_streak", [])
            dd_mult, dd_msg = self._calculate_drawdown_multiplier(recent_streak)
            if dd_mult < 1.0:
                self.logger.warning(f"[{self.name}] {dd_msg}")

            # --- Dynamic Risk Calculation ---
            conf_mult = self._get_conviction_multiplier(min_conviction, conviction)
            dynamic_risk_pct = base_risk * conf_mult * regime_mult * quality_mult * dd_mult
            dynamic_risk_pct = min(dynamic_risk_pct, risk_cap)
            
            self.logger.info(f"[{self.name}] Final Risk: {dynamic_risk_pct*100:.2f}% (Base: {base_risk*100}%, Conf: x{conf_mult}, Regime: x{regime_mult}, Quality: x{quality_mult:.2f}, DD: x{dd_mult}, Cap: {risk_cap*100}%)")
            
            # --- 1. Finalize SL ---
            # NOTE: Removed standalone liquidation price calculation.
            # In a cross-margin DEX (Nado/Vertex), liquidation is based on account-wide Health (maintenance margin),
            # not a single price point for a single position. Adjusting SL based on a simplistic formula is dangerous.
            liq_price = 0.0
            
            # Recalculate distance after SL adjustment
            distance_to_sl = abs(current_price - sl_price)

            # ═══ 2. Position Sizing (canonical math) ═══
            # risk_amount_usd = how much USD we're willing to LOSE
            risk_amount_usd = total_balance * dynamic_risk_pct
            
            # Clamp to remaining portfolio risk budget
            if 'remaining_risk_budget_usd' in locals() and remaining_risk_budget_usd > 0:
                risk_amount_usd = min(risk_amount_usd, remaining_risk_budget_usd)
            
            # contracts = risk_amount_usd / distance_to_sl (units of base asset)
            # notional_usd = contracts * current_price (total exposure)
            if distance_to_sl > 0:
                contracts = risk_amount_usd / distance_to_sl
                notional_usd = contracts * current_price
            else:
                contracts = 0
                notional_usd = 0
                
            # ═══ 3. Apply Slippage Penalty & Veto ═══
            if spread_pct > config.SPREAD_VETO_THRESHOLD:
                msg = f"Spread is {spread_pct}% (Too illiquid). Blocking trade."
                self.logger.warning(f"[{self.name}] ❌ SPREAD VETO: {msg}")
                approved = False
                veto_category = "SPREAD"
                veto_reason = msg
            elif spread_pct > config.SPREAD_PENALTY_THRESHOLD:
                notional_usd *= 0.8 # Cut notional by 20%
                contracts = notional_usd / current_price if current_price > 0 else 0
                risk_amount_usd = contracts * distance_to_sl # Fix HIGH #4: Recalculate planned risk to match reduced position
            
            # Fee and Funding Impact on RR
            derivatives_data = market_data.get("derivatives_data", {})
            fee_pct = float(derivatives_data.get("taker_fee_pct", 0.0005)) * 2 # Open + Close
            funding_pct = abs(float(derivatives_data.get("funding_rate", 0.0001)))
            
            # ═══ 4. Target Margin & Required Leverage ═══
            target_margin_pct = profile_rules.get("target_margin_pct", 0.10)
            max_leverage = profile_rules.get("max_leverage", 10.0)
            
            # Target margin based on available margin rather than total balance
            # with a reserve buffer (e.g. max 80% of available margin can be used for target)
            usable_margin = min(available_margin * 0.8, total_balance * max_margin_pct)
            target_margin_usd = min(total_balance * target_margin_pct, usable_margin)
            
            if target_margin_usd > 0:
                required_leverage = notional_usd / target_margin_usd
            else:
                required_leverage = 1.0
                
            # ═══ 5. Hard Caps on Leverage ═══
            # 5a. Volatility Cap
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
                
            # 5b. Liquidation Safety Limit (Liquidation must be 3x further than SL)
            LIQUIDATION_BUFFER = 3.0
            sl_pct = (distance_to_sl / current_price) if current_price > 0 else 0.01
            if sl_pct > 0:
                liq_max_leverage = 1.0 / (sl_pct * LIQUIDATION_BUFFER)
            else:
                liq_max_leverage = max_leverage
                
            # Final allowed ceiling leverage
            safe_ceiling_leverage = min(max_leverage, vol_max_leverage, liq_max_leverage)
            
            # The actual leverage used is what's required, capped by the absolute safety ceiling
            final_leverage = min(required_leverage, safe_ceiling_leverage)
            final_leverage = max(1.0, round(final_leverage))
            
            # ═══ 6. Position Size Reduction (If Required > Safe Ceiling) ═══
            if required_leverage > safe_ceiling_leverage:
                # We need more leverage than is safe to open the requested notional size.
                # Must reduce notional to fit within max_margin * safe_ceiling_leverage
                max_safe_notional = usable_margin * safe_ceiling_leverage
                if notional_usd > max_safe_notional:
                    self.logger.warning(
                        f"[{self.name}] Required leverage ({required_leverage:.1f}x) > Safe Final ({final_leverage}x). "
                        f"Reducing notional from ${notional_usd:.2f} to ${max_safe_notional:.2f}."
                    )
                    notional_usd = max_safe_notional
                    contracts = notional_usd / current_price if current_price > 0 else 0
                    # Recalculate Risk USD based on reduced position size
                    risk_amount_usd = contracts * distance_to_sl
            
            # Also apply absolute max notional guard in case it wasn't caught
            max_notional_usd = total_balance * max_margin_pct * final_leverage
            if notional_usd > max_notional_usd:
                notional_usd = max_notional_usd
                contracts = notional_usd / current_price if current_price > 0 else 0
                risk_amount_usd = contracts * distance_to_sl
                
            leverage = final_leverage
            
            self.logger.info(
                f"[{self.name}] Final Leverage: {leverage}x "
                f"(Profile Max: {max_leverage}x, Volatility Max: {vol_max_leverage}x, "
                f"Liq Safety: {liq_max_leverage:.1f}x, Required: {required_leverage:.1f}x)"
            )
            
            # ═══ 7. Rounding & Final Math Alignment ═══
            size_increment = float(derivatives_data.get("size_increment", 0.001))
            if size_increment > 0:
                # Nado correctly floors the amount to size_increment, we should do the same
                contracts = (contracts // size_increment) * size_increment
            notional_usd = round(contracts * current_price, 2)
            margin_usd = round(notional_usd / leverage if leverage > 0 else notional_usd, 2)
            margin_pct = round((margin_usd / total_balance) * 100 if total_balance > 0 else 0, 2)

            # --- 6. Maximum Notional Guard ---
            symbol = ceo_decision.get("symbol", "")
            
            min_size = float(derivatives_data.get("min_size", 0.0))
            min_notional = float(derivatives_data.get("min_notional", 10.0))
            
            # Sanity check: If reported min_size exceeds max possible notional of account, it's a testnet dummy artifact
            if min_size > 0 and (min_size * current_price) > max_notional_usd:
                self.logger.info(f"[{self.name}] ℹ️ Reported min_size ({min_size}) exceeds max account notional (${max_notional_usd:.2f}). Treating as unconfigured testnet artifact.")
                min_size = 0.0
            
            if notional_usd <= 0 or contracts <= 0:
                msg = f"Calculated order size is 0. Blocking trade."
                self.logger.warning(f"[{self.name}] 🚫 ZERO SIZE VETO: {msg}")
                approved = False
                veto_category = "MIN_SIZE"
                veto_reason = msg
            elif min_size > 0 and contracts < min_size:
                # Пользователь подтвердил, что биржа по факту не имеет ограничений
                self.logger.info(f"[{self.name}] ℹ️ Размер ордера ({contracts}) меньше заявленного API минимума ({min_size}). Ограничение игнорируется.")
            elif min_notional > 0 and notional_usd < min_notional:
                msg = f"Calculated notional ${notional_usd:.2f} is below exchange minimum notional (${min_notional:.2f}). Blocking trade."
                self.logger.warning(f"[{self.name}] 🚫 MIN NOTIONAL VETO: {msg}")
                approved = False
                veto_category = "MIN_NOTIONAL"
                veto_reason = msg
            elif notional_usd > max_notional_usd:
                msg = f"Required notional ${notional_usd:.2f} exceeds max allowed ${max_notional_usd:.2f}."
                self.logger.warning(f"[{self.name}] 🚫 MAX NOTIONAL VETO: {msg}")
                approved = False
                veto_category = "MAX_MARGIN"
                veto_reason = msg
            elif margin_usd > available_margin:
                msg = f"Required margin ${margin_usd:.2f} exceeds available margin (${available_margin:.2f})."
                self.logger.warning(f"[{self.name}] ❌ INSUFFICIENT MARGIN VETO: {msg}")
                approved = False
                veto_category = "INSUFFICIENT_BALANCE"
                veto_reason = msg
            
            risk_amount_usd = round(risk_amount_usd, 2)
            sl_price = round(sl_price, 6)
            tp_price = round(tp_price, 6)
            liq_price = round(liq_price, 6)
            
            # RR adjusted for fees
            effective_tp_dist = distance_to_tp - (current_price * (fee_pct + funding_pct))
            effective_sl_dist = distance_to_sl + (current_price * (fee_pct + funding_pct))
            rr_ratio = round(effective_tp_dist / effective_sl_dist if effective_sl_dist > 0 else 0, 2)
            
            # ═══ 7. Verify Actual Risk (using rounded contracts) ═══
            actual_risk_usd = contracts * distance_to_sl
            # Add a 5% tolerance for floating point and contract precision rounding noise
            if actual_risk_usd > (risk_amount_usd * 1.05) and approved is not False:
                msg = f"Actual risk ${actual_risk_usd:.2f} exceeds allowed risk ${risk_amount_usd:.2f}."
                self.logger.warning(f"[{self.name}] ❌ RISK VETO: {msg}")
                approved = False
                veto_category = "EXCESSIVE_RISK"
                veto_reason = msg
            
            if approved is not False: # If not vetoed earlier
                approved = True
            
            
        final_reasoning = f"Math calculated based on {profile_name} profile. SL={sl_price}, TP={tp_price}, RR={rr_ratio}"
        if not approved and veto_reason:
            final_reasoning = f"❌ ОТКЛОНЕНО ({veto_category}): {veto_reason} | {final_reasoning}"
            
        parsed_res = {
            "approved": approved if approved is not None else False,
            "veto_category": veto_category,
            "reasoning": final_reasoning,
            # ═══ Canonical fields (unambiguous) ═══
            "risk_amount_usd": risk_amount_usd,     # Max USD willing to lose
            "notional_size_usd": notional_usd,       # Total exposure (contracts * price)
            "margin_usd": margin_usd,                # Collateral locked (notional / leverage)
            "leverage": leverage,                     # Multiplier
            "contracts": contracts,                   # Base asset amount
            "margin_pct": margin_pct,                 # margin_usd as % of balance
            # ═══ Levels ═══
            "entry_price": current_price,
            "take_profit_price": tp_price,
            "take_profit_pct": round(abs(tp_price - current_price) / current_price * 100, 2) if current_price > 0 else 0,
            "stop_loss_price": sl_price,
            "stop_loss_pct": round(abs(sl_price - current_price) / current_price * 100, 2) if current_price > 0 else 0,
            "risk_reward_ratio": rr_ratio,
            "liquidation_price": liq_price,
            # ═══ Legacy aliases (for backward compatibility) ═══
            "position_size_pct": margin_pct,
        }
                
        return parsed_res
