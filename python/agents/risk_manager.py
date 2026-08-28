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

    def _get_profile_rules(self) -> tuple[float, float, float, float, int]:
        profile = config.TRADING_PROFILE
        if profile == "AGGRESSIVE":
            # Risk 5% per trade, SL 2.5x ATR, TP 4.5x ATR (RR 1:1.8), Max Margin 45%
            return (0.05, 2.5, 4.5, 0.45, 70)
        elif profile == "CONSERVATIVE":
            # Risk 0.5% per trade, SL 2.0x ATR, TP 3.0x ATR, Max Margin 10%
            return (0.005, 2.0, 3.0, 0.10, 85)
        else:
            # BALANCED: Risk 1% per trade, SL 1.5x ATR, TP 2.5x ATR, Max Margin 20%
            return (0.01, 1.5, 2.5, 0.20, 80)

    async def analyze(self, ceo_decision: Dict[str, Any], portfolio_data: Dict[str, Any], market_data: Dict[str, Any]) -> Dict[str, Any]:
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
        risk_pct, sl_mult, tp_mult, max_margin_pct, min_conviction = self._get_profile_rules()
        profile_name = config.TRADING_PROFILE
        leverage = config.LEVERAGE
        
        self.logger.info(f"[{self.name}] Расчет математики риска по профилю: {profile_name} (Риск на сделку: {risk_pct*100}%, Leverage: {leverage}x)...")
        
        decision = ceo_decision.get("decision", "HOLD")
        conviction = ceo_decision.get("conviction", 0)
        
        price_data = market_data.get("price_data", {})
        current_price = float(price_data.get("current_price", 1.0) or 1.0)
        
        indicators = market_data.get("indicators", {})
        atr_14 = float(indicators.get("atr_14", current_price * 0.02) or current_price * 0.02)
        
        total_balance = float(portfolio_data.get("total_usd", portfolio_data.get("current_balance", 0.0)) or 0.0)
        
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
            # QW #6: Win Rate Gate Check
            win_count = portfolio_data.get("win_count", 0)
            loss_count = portfolio_data.get("loss_count", 0)
            total_trades = win_count + loss_count
            
            if total_trades >= 10:
                win_rate = win_count / total_trades
                if win_rate < 0.45:
                    penalty_factor = max(0.2, win_rate / 0.45) # Soft penalty instead of harsh 0.5 cut
                    self.logger.warning(f"[{self.name}] ⚠️ WIN RATE GATE TRIGGERED: Win rate is {win_rate*100:.1f}%. Soft penalty applied (x{penalty_factor:.2f}).")
                    min_conviction = min(95, min_conviction + 5)
                    risk_pct *= penalty_factor
                    
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
                
            # Drawdown Protection & Kelly Criterion
            recent_streak = portfolio_data.get("recent_streak", [])
            dynamic_risk_pct = risk_pct
            
            if len(recent_streak) >= 3:
                last_three = recent_streak[-3:]
                if last_three == ["LOSS", "LOSS", "LOSS"]:
                    dynamic_risk_pct = risk_pct * 0.25
                    self.logger.warning(f"[{self.name}] 🚨 RED ALERT: 3 losses in a row. Risk slashed to {dynamic_risk_pct*100}%.")
                elif last_three[-2:] == ["LOSS", "LOSS"]:
                    dynamic_risk_pct = risk_pct * 0.5
                    self.logger.warning(f"[{self.name}] DRAWDOWN PROTECTION: 2 losses in a row. Risk cut to {dynamic_risk_pct*100}%.")
            
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
            
            # Fee and Funding Impact on RR
            derivatives_data = market_data.get("derivatives_data", {})
            fee_pct = float(derivatives_data.get("taker_fee_pct", 0.0005)) * 2 # Open + Close
            funding_pct = abs(float(derivatives_data.get("funding_rate", 0.0001)))
            
            # ═══ 4. Max Position Size Guard ═══
            # max_notional = max_margin_pct * total_balance * leverage
            max_notional_usd = total_balance * max_margin_pct * leverage
            if notional_usd > max_notional_usd:
                notional_usd = max_notional_usd
                contracts = notional_usd / current_price if current_price > 0 else 0
                
            # ═══ 5. Rounding & Final Math Alignment ═══
            size_increment = float(derivatives_data.get("size_increment", 0.001))
            if size_increment > 0:
                # Nado correctly floors the amount to size_increment, we should do the same
                contracts = (contracts // size_increment) * size_increment
            notional_usd = round(contracts * current_price, 2)
            margin_usd = round(notional_usd / leverage if leverage > 0 else notional_usd, 2)
            margin_pct = round((margin_usd / total_balance) * 100 if total_balance > 0 else 0, 2)

            # --- 6. Maximum Notional Guard ---
            symbol = ceo_decision.get("symbol", "")
            
            if notional_usd > max_notional_usd:
                msg = f"Required notional ${notional_usd:.2f} exceeds max allowed ${max_notional_usd:.2f}."
                self.logger.warning(f"[{self.name}] ❌ MAX NOTIONAL VETO: {msg}")
                approved = False
                veto_category = "MAX_MARGIN"
                veto_reason = msg
            elif margin_usd > total_balance:
                msg = f"Required margin ${margin_usd:.2f} exceeds balance (${total_balance:.2f})."
                self.logger.warning(f"[{self.name}] ❌ INSUFFICIENT BALANCE VETO: {msg}")
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
