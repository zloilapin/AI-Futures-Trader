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
                if win_rate < 0.40:
                    self.logger.warning(f"[{self.name}] ⚠️ WIN RATE GATE TRIGGERED: Win rate is {win_rate*100:.1f}%. Increasing threshold and halving risk.")
                    min_conviction = max(90, min_conviction)
                    risk_pct *= 0.5
                    
            if conviction < min_conviction:
                return {
                    "approved": False,
                    "reasoning": f"Conviction {conviction} is below Win Rate Gate threshold {min_conviction}"
                }

            # Calculate ATR based SL and TP with a minimum floor to avoid noise
            if decision == "LONG":
                sl_dist = max(atr_14 * sl_mult, current_price * 0.012) # Min 1.2% SL
                tp_dist = max(atr_14 * tp_mult, current_price * 0.036) # Min 3.6% TP
                sl_price = current_price - sl_dist
                tp_price = current_price + tp_dist
            else: # SHORT
                sl_dist = max(atr_14 * sl_mult, current_price * 0.012)
                tp_dist = max(atr_14 * tp_mult, current_price * 0.036)
                sl_price = current_price + sl_dist
                tp_price = current_price - tp_dist
                
            distance_to_sl = abs(current_price - sl_price)
            distance_to_tp = abs(tp_price - current_price)
            
            # Slippage Penalty
            spread_pct = float(market_data.get("order_book_data", {}).get("spread_pct", 0))
            if spread_pct > 0.4:
                self.logger.warning(f"[{self.name}] High spread detected ({spread_pct}%). Applying slippage penalty.")
                
            # Drawdown Protection & Kelly Criterion
            recent_streak = portfolio_data.get("recent_streak", [])
            dynamic_risk_pct = risk_pct
            
            if len(recent_streak) >= 3:
                last_three = recent_streak[-3:]
                if last_three == ["LOSS", "LOSS", "LOSS"]:
                    dynamic_risk_pct = 0.001 # 0.1% risk (Red Alert)
                    self.logger.warning(f"[{self.name}] 🚨 RED ALERT: 3 losses in a row. Risk slashed to 0.1%.")
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
            if spread_pct > 1.0:
                self.logger.warning(f"[{self.name}] ❌ SPREAD VETO: Spread is {spread_pct}% (Too illiquid). Blocking trade.")
                approved = False
            elif spread_pct > 0.4:
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
                
            MIN_NOTIONAL = 15.0
            if pos_usd > 0 and pos_usd < MIN_NOTIONAL:
                if total_balance * leverage < MIN_NOTIONAL:
                    self.logger.warning(f"[{self.name}] ❌ MIN ORDER VETO: Required notional ${pos_usd:.2f} is below min ${MIN_NOTIONAL} and balance cannot cover it.")
                    approved = False
                else:
                    self.logger.info(f"[{self.name}] Bumping position from ${pos_usd:.2f} to min notional ${MIN_NOTIONAL}.")
                    pos_usd = MIN_NOTIONAL
                
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
            
        # Call LLM to validate the pre-calculated math and fundamental risks
        system_instruction = (
            f"You are an expert Risk Manager for Kraken Futures operating under the '{profile_name}' Risk Profile.\n"
            "Evaluation Rules:\n"
            "1. You are provided with EXACT pre-calculated risk parameters (SL, TP, Position Size) based on ATR volatility and strict account risk %.\n"
            "2. Review the CEO's reasoning and the current market spread/RSI.\n"
            "3. If the market condition is too dangerous (e.g. huge spread or extreme fundamental risk), you can override and set approved: false.\n"
            "4. Otherwise, set approved: true and exactly copy the provided calculated risk metrics into your output JSON.\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning": "<step-by-step detailed risk reasoning, acknowledging the ATR-based math>",\n'
            '  "position_size_usd": <float>,\n'
            '  "position_size_pct": <float>,\n'
            '  "entry_price": <float>,\n'
            '  "take_profit_price": <float>,\n'
            '  "stop_loss_price": <float>,\n'
            '  "risk_reward_ratio": <float>,\n'
            '  "liquidation_price": <float>,\n'
            '  "approved": true | false\n'
            "}\n"
            "CRITICAL: Output ONLY valid JSON. Do not write any conversational text, explanations, or Python scripts outside the JSON object. Do not simulate missing data."
        )
        
        payload = {
            "trading_profile": profile_name,
            "ceo_decision": ceo_decision,
            "calculated_risk_parameters": {
                "pre_approved": approved,
                "entry_price": current_price,
                "position_size_usd": pos_usd,
                "margin_usd": margin_usd if approved else 0,
                "leverage": config.LEVERAGE,
                "position_margin_pct": pos_pct,
                "stop_loss_price": sl_price,
                "take_profit_price": tp_price,
                "risk_reward_ratio": rr_ratio,
                "max_risk_usd": round(total_balance * risk_pct, 2)
            },
            "current_market": {
                "spread_pct": market_data.get("order_book_data", {}).get("spread_pct"),
                "rsi_14": indicators.get("rsi_14"),
                "atr_14": atr_14
            }
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{system_instruction}\n\nExecution Data:\n{data_string}"

        response = await self.llm_client.generate(full_prompt)
        parsed_res = self._parse_json(response)
        
        # Post-Validation Clamp: Guard against LLM hallucinations
        # We completely overwrite LLM numbers with the exact Python math.
        if not approved:
            parsed_res["approved"] = False
            
        if parsed_res.get("approved"):
            parsed_res["position_size_usd"] = pos_usd
            parsed_res["position_size_pct"] = pos_pct
            parsed_res["entry_price"] = current_price
            parsed_res["take_profit_price"] = tp_price
            parsed_res["stop_loss_price"] = sl_price
            parsed_res["risk_reward_ratio"] = rr_ratio
            parsed_res["liquidation_price"] = liq_price
                
        return parsed_res
