import os
import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class RiskManager(BaseAgent):
    """
    Gatekeeper agent responsible for capital preservation, risk profiles, and position sizing on Nado DEX.
    Calculates exact Take Profit (TP), Stop Loss (SL), position amount (USD / %), and risk/reward ratio.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Risk_Manager", logger, llm_client)

    def _get_profile_rules(self) -> tuple[float, float, float, float, int]:
        profile = os.getenv("TRADING_PROFILE", "BALANCED").upper()
        if profile == "AGGRESSIVE":
            # Risk 5% per trade, SL 1.5x ATR (min 1.2%), TP 4.5x ATR (RR 1:3), Max pos 50% of portfolio
            return (0.05, 1.5, 4.5, 0.50, 70)
        elif profile == "CONSERVATIVE":
            # Risk 0.5% per trade, SL 2.0x ATR, TP 3.0x ATR, Max pos 10%
            return (0.005, 2.0, 3.0, 0.10, 85)
        else:
            # BALANCED: Risk 1% per trade, SL 1.5x ATR, TP 2.5x ATR, Max pos 20%
            return (0.01, 1.5, 2.5, 0.20, 80)

    async def analyze(self, ceo_decision: Dict[str, Any], portfolio_data: Dict[str, Any], market_data: Dict[str, Any]) -> Dict[str, Any]:
        risk_pct, sl_mult, tp_mult, max_pos_pct, min_conviction = self._get_profile_rules()
        profile_name = os.getenv("TRADING_PROFILE", "BALANCED").upper()
        
        self.logger.info(f"[{self.name}] Расчет математики риска по профилю: {profile_name} (Риск на сделку: {risk_pct*100}%)...")
        
        decision = ceo_decision.get("decision", "HOLD")
        conviction = ceo_decision.get("conviction", 0)
        
        price_data = market_data.get("price_data", {})
        current_price = float(price_data.get("current_price", 1.0) or 1.0)
        
        indicators = market_data.get("indicators", {})
        atr_14 = float(indicators.get("atr_14", current_price * 0.02) or current_price * 0.02)
        
        total_balance = float(portfolio_data.get("total_usd", 1000.0) or 1000.0)
        
        approved = False
        pos_usd = 0.0
        pos_pct = 0.0
        sl_price = 0.0
        tp_price = 0.0
        rr_ratio = 0.0
        
        if decision in ["LONG", "SHORT"] and conviction >= min_conviction:
            # Calculate ATR based SL and TP with a minimum floor to avoid noise
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
            spread = float(market_data.get("order_book_data", {}).get("spread", 0))
            if spread > 0.4:
                self.logger.warning(f"[{self.name}] High spread detected ({spread}%). Applying slippage penalty.")
                # We will penalize the position size after initial calculation
                
            # Drawdown Protection & Kelly Criterion
            recent_streak = portfolio_data.get("recent_streak", [])
            dynamic_risk_pct = risk_pct
            
            if len(recent_streak) >= 2:
                last_two = recent_streak[-2:]
                if last_two == ["LOSS", "LOSS"]:
                    dynamic_risk_pct = risk_pct * 0.5
                    self.logger.warning(f"[{self.name}] DRAWDOWN PROTECTION: 2 losses in a row. Risk cut to {dynamic_risk_pct*100}%.")
                elif last_two == ["WIN", "WIN"]:
                    dynamic_risk_pct = min(risk_pct * 1.4, 0.07) # Boost risk, max 7%
                    self.logger.info(f"[{self.name}] KELLY CRITERION: 2 wins in a row. Risk boosted to {dynamic_risk_pct*100}%.")
            
            # Position Sizing
            risk_usd = total_balance * dynamic_risk_pct
            
            if distance_to_sl > 0:
                # Risk = units * distance_to_sl => units = Risk / distance_to_sl
                units = risk_usd / distance_to_sl
                pos_usd = units * current_price
            else:
                pos_usd = 0
                
            # Apply Slippage Penalty
            if spread > 0.4:
                pos_usd *= 0.8 # Cut position by 20%
                
            # Max Position Size Guard
            max_pos_usd = total_balance * max_pos_pct
            if pos_usd > max_pos_usd:
                pos_usd = max_pos_usd
                
            pos_pct = (pos_usd / total_balance) * 100 if total_balance > 0 else 0
            rr_ratio = distance_to_tp / distance_to_sl if distance_to_sl > 0 else 0
            
            # Format nicely
            pos_usd = round(pos_usd, 2)
            pos_pct = round(pos_pct, 2)
            sl_price = round(sl_price, 6)
            tp_price = round(tp_price, 6)
            rr_ratio = round(rr_ratio, 2)
            
            approved = True
            
        # Call LLM to validate the pre-calculated math and fundamental risks
        system_instruction = (
            f"You are an expert Risk Manager for Nado DEX operating under the '{profile_name}' Risk Profile.\n"
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
            '  "approved": true | false\n'
            "}"
        )
        
        payload = {
            "trading_profile": profile_name,
            "ceo_decision": ceo_decision,
            "calculated_risk_parameters": {
                "pre_approved": approved,
                "entry_price": current_price,
                "position_size_usd": pos_usd,
                "position_size_pct": pos_pct,
                "stop_loss_price": sl_price,
                "take_profit_price": tp_price,
                "risk_reward_ratio": rr_ratio,
                "max_risk_usd": round(total_balance * risk_pct, 2)
            },
            "current_market": {
                "spread": market_data.get("order_book_data", {}).get("spread"),
                "rsi_14": indicators.get("rsi_14"),
                "atr_14": atr_14
            }
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{system_instruction}\n\nExecution Data:\n{data_string}"

        response = await self.llm_client.generate(full_prompt)
        return self._parse_json(response)
