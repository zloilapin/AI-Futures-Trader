import os
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient
from core.config import config

class RiskManager(BaseAgent):
    """
    Gatekeeper agent responsible for capital preservation, risk profiles, and position sizing on Kraken Futures.
    Calculates exact Take Profit (TP), Stop Loss (SL), position amount (USD / %), and risk/reward ratio.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Risk_Manager", logger, llm_client)

    def _get_profile_rules(self) -> tuple[float, float, float, float, int]:
        profile = config.TRADING_PROFILE
        if profile == "AGGRESSIVE":
            # Risk 5% per trade, SL 1.5x ATR (min 1.2%), TP 4.5x ATR (RR 1:3), Max Margin 45%
            return (0.05, 1.5, 4.5, 0.45, 70)
        elif profile == "CONSERVATIVE":
            # Risk 0.5% per trade, SL 2.0x ATR, TP 3.0x ATR, Max Margin 10%
            return (0.005, 2.0, 3.0, 0.10, 85)
        else:
            # BALANCED: Risk 1% per trade, SL 1.5x ATR, TP 2.5x ATR, Max Margin 20%
            return (0.01, 1.5, 2.5, 0.20, 80)

    async def analyze(self, ceo_decision: Dict[str, Any], portfolio_data: Dict[str, Any], market_data: Dict[str, Any]) -> Dict[str, Any]:
        risk_pct, sl_mult, tp_mult, max_margin_pct, min_conviction = self._get_profile_rules()
        profile_name = config.TRADING_PROFILE
        
        self.logger.info(f"[{self.name}] Расчет математики риска по профилю: {profile_name} (Риск на сделку: {risk_pct*100}%)...")
        
        decision = ceo_decision.get("decision", "HOLD")
        conviction = ceo_decision.get("conviction", 0)
        
        price_data = market_data.get("price_data", {})
        current_price = float(price_data.get("current_price", 1.0) or 1.0)
        
        indicators = market_data.get("indicators", {})
        atr_14 = float(indicators.get("atr_14", current_price * 0.02) or current_price * 0.02)
        
        total_balance = float(portfolio_data.get("total_usd", portfolio_data.get("current_balance", 0.0)) or 0.0)
        
        approved = None
        pos_usd = 0.0
        pos_pct = 0.0
        sl_price = 0.0
        tp_price = 0.0
        rr_ratio = 0.0
        
        if total_balance <= 0:
            self.logger.warning(f"[{self.name}] ❌ INSUFFICIENT BALANCE: Total balance is {total_balance}. Blocking trade.")
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
                elif last_three[-2:] == ["WIN", "WIN"]:
                    dynamic_risk_pct = min(risk_pct * 1.4, 0.07) # Boost risk, max 7%
                    self.logger.info(f"[{self.name}] KELLY CRITERION: 2 wins in a row. Risk boosted to {dynamic_risk_pct*100}%.")
            
            # Position Sizing
            risk_usd = total_balance * dynamic_risk_pct
            
            if distance_to_sl > 0:
                units = risk_usd / distance_to_sl
                pos_usd = units * current_price
            else:
                pos_usd = 0
                
            # Apply Slippage Penalty & Veto
            if spread_pct > config.SPREAD_VETO_THRESHOLD:
                self.logger.warning(f"[{self.name}] ❌ SPREAD VETO: Spread is {spread_pct}% (Too illiquid). Blocking trade.")
                approved = False
            elif spread_pct > config.SPREAD_PENALTY_THRESHOLD:
                pos_usd *= 0.8 # Cut position by 20%
                
            # Leverage Integration
            leverage = config.LEVERAGE
            
            # Liquidation Price Check
            liq_price = 0.0
            if decision == "LONG":
                liq_price = current_price * (1 - (1 / leverage) + 0.005)
                if sl_price <= liq_price:
                    self.logger.warning(f"[{self.name}] SL {sl_price} is below Liquidation {liq_price}. Adjusting SL.")
                    sl_price = liq_price * 1.005
            elif decision == "SHORT":
                liq_price = current_price * (1 + (1 / leverage) - 0.005)
                if sl_price >= liq_price:
                    self.logger.warning(f"[{self.name}] SL {sl_price} is above Liquidation {liq_price}. Adjusting SL.")
                    sl_price = liq_price * 0.995
            
            # Recalculate distance after SL adjustment
            distance_to_sl = abs(current_price - sl_price)
            
            # Fee and Funding Impact on RR
            fee_pct = 0.0005 * 2 # Open + Close
            funding_pct = 0.0001
            
            # Max Position Size Guard (Notional = Max Margin * Leverage)
            max_pos_usd = total_balance * max_margin_pct * leverage
            if pos_usd > max_pos_usd:
                pos_usd = max_pos_usd
                
            # Minimum Order Size & Asset Amount Guard
            base_amount = pos_usd / current_price if current_price > 0 else 0
            symbol = ceo_decision.get("symbol", "")
            min_base = 0.0001 if "BTC" in symbol else (0.01 if "ETH" in symbol else 1.0)
            
            if base_amount > 0 and base_amount < min_base:
                self.logger.info(f"[{self.name}] Bumping base amount from {base_amount} to {min_base}.")
                pos_usd = min_base * current_price
                
            if pos_usd > 0 and pos_usd < config.MIN_NOTIONAL:
                if total_balance * leverage < config.MIN_NOTIONAL:
                    self.logger.warning(f"[{self.name}] ❌ MIN ORDER VETO: Required notional ${pos_usd:.2f} is below min ${config.MIN_NOTIONAL} and balance cannot cover it.")
                    approved = False
                else:
                    self.logger.info(f"[{self.name}] Bumping position from ${pos_usd:.2f} to min notional ${config.MIN_NOTIONAL}.")
                    pos_usd = config.MIN_NOTIONAL
                
            if pos_usd > max_pos_usd:
                self.logger.warning(f"[{self.name}] ❌ MIN ORDER VETO: Required notional ${pos_usd:.2f} exceeds max allowed ${max_pos_usd:.2f}.")
                approved = False
                
            margin_usd = pos_usd / leverage if leverage > 0 else pos_usd
            pos_pct = (margin_usd / total_balance) * 100 if total_balance > 0 else 0
            
            # RR adjusted for fees
            effective_tp_dist = distance_to_tp - (current_price * (fee_pct + funding_pct))
            effective_sl_dist = distance_to_sl + (current_price * (fee_pct + funding_pct))
            rr_ratio = effective_tp_dist / effective_sl_dist if effective_sl_dist > 0 else 0
            
            # Format nicely
            pos_usd = round(pos_usd, 2)
            margin_usd = round(margin_usd, 2)
            pos_pct = round(pos_pct, 2)
            sl_price = round(sl_price, 6)
            tp_price = round(tp_price, 6)
            liq_price = round(liq_price, 6)
            rr_ratio = round(rr_ratio, 2)
            
            if approved is not False: # If not vetoed earlier
                approved = True
            
        parsed_res = {
            "approved": approved if approved is not None else False,
            "reasoning": f"Math calculated based on {profile_name} profile. SL={sl_price}, TP={tp_price}, RR={rr_ratio}",
            "position_size_usd": pos_usd,
            "position_size_pct": pos_pct,
            "entry_price": current_price,
            "take_profit_price": tp_price,
            "stop_loss_price": sl_price,
            "risk_reward_ratio": rr_ratio,
            "liquidation_price": liq_price
        }
                
        return parsed_res
