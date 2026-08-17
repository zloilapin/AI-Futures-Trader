import json
from typing import Dict, Any
import os

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class CEOAgent(BaseAgent):
    """
    The Chief Investment Officer (CIO / CEO) of the trading syndicate.
    
    Uses Llama 70B as the Primary CEO, and Kimi K3 as the Escalation Model 
    for medium-confidence trades.
    """
    
    def __init__(self, logger: TradeLogger, primary_llm: LLMClient, escalation_llm: LLMClient):
        super().__init__("CEO_Agent", logger, primary_llm)
        self.escalation_llm = escalation_llm

        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "ceo_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes the 4-Tier Escalation Model logic.
        """
        symbol = data.get("symbol")
        analyst_reports = data.get("analyst_reports", [])
        mtf_data = data.get("multi_timeframe_context", {})
        historical_context = data.get("historical_context", {})

        self.logger.info(f"[{self.name}] Llama 70B анализирует консенсус аналитиков по {symbol}...")
        
        payload = {
            "target_symbol": symbol,
            "multi_timeframe_context": mtf_data,
            "subordinate_analyst_reports": analyst_reports,
            "historical_trade_memory": historical_context,
            "past_lessons_learned": data.get("past_lessons_learned", [])
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nExecutive Dashboard Data:\n{data_string}"
        
        llm_response = {}
        try:
            llm_response = await self.generate_json(full_prompt)
        except Exception as e:
            self.logger.warning(f"[{self.name}] Primary LLM failed: {e}")
            return {"decision": "HOLD", "conviction": 0, "hold_category": "LLM_ERROR", "reasoning_en": f"Primary LLM failed: {e}"}

        decision = str(llm_response.get("decision", "NO_SIGNAL")).upper()
        
        try:
            conviction = int(llm_response.get("conviction", 0))
        except (ValueError, TypeError):
            conviction = 0
            
        reasoning = llm_response.get("reasoning_en", "")
        
        self.logger.info(f"[{self.name}] Primary CEO Llama 70B Decision: {decision} (Conf: {conviction}%)")
        print(f"👔 [CEO Llama 70B] {decision} ({conviction}%)")
        
        # ESCALATION MODEL LOGIC
        if decision in ["LONG", "SHORT"]:
            if conviction >= 80:
                self.logger.info(f"[{self.name}] High conviction ({conviction}% >= 80%). Bypassing escalation.")
            elif 55 <= conviction < 80:
                self.logger.info(f"[{self.name}] Medium conviction ({conviction}%). Escalating to Kimi K3...")
                print(f"⚠️ [Escalation] Спорная ситуация ({conviction}%). Передача дела в Kimi K3 для второго мнения...")
                
                escalation_prompt = f"""You are the Supreme Escalation AI (Kimi K3) for an elite crypto prop-trading firm.
The Primary CEO (Llama 70B) has proposed a {decision} on {symbol} with a mediocre conviction of {conviction}%.
Your job is to review the exact same data and provide a FINAL decisive verdict.

Primary CEO Reasoning:
{reasoning}

Here is the raw data:
{data_string}

Provide a JSON with your final decision. You can confirm the trade with high confidence, or VETO it with a HOLD.
SCHEMA:
{{
  "decision": "LONG" | "SHORT" | "HOLD",
  "conviction": <int 1-100>,
  "reasoning_en": "<your detailed escalation review reasoning>"
}}
"""
                try:
                    # Temporary swap of llm_client for generate_json retry wrapper
                    original_llm = self.llm_client
                    self.llm_client = self.escalation_llm
                    k3_response = await self.generate_json(escalation_prompt)
                    self.llm_client = original_llm
                    
                    decision = str(k3_response.get("decision", "NO_SIGNAL")).upper()
                    try:
                        conviction = int(k3_response.get("conviction", 0))
                    except (ValueError, TypeError):
                        conviction = 0
                        
                    k3_reasoning = k3_response.get("reasoning_en", "")
                    
                    reasoning = f"[Primary CEO: {reasoning}]\n\n[ESCALATION K3 VERDICT: {k3_reasoning}]"
                    self.logger.info(f"[{self.name}] Escalation K3 Final Decision: {decision} ({conviction}%)")
                    print(f"🧠 [Kimi K3] Вердикт: {decision} ({conviction}%)")
                except Exception as e:
                    self.logger.error(f"[{self.name}] Escalation LLM failed: {e}")
                    decision = "HOLD"
                    conviction = 0
                    reasoning += f"\n\n[ESCALATION FAILED: {e}. Defaulting to HOLD.]"
            else:
                self.logger.info(f"[{self.name}] Low conviction ({conviction}% < 55%). Forcing HOLD.")
                print(f"🛑 [CEO] Слишком низкая уверенность ({conviction}%). Отмена сделки (HOLD).")
                decision = "HOLD"

        return {
            "decision": decision,
            "conviction": conviction,
            "reasoning_en": reasoning,
            "reasoning_ru": llm_response.get("reasoning_ru", ""),
            "consensus_summary": llm_response.get("consensus_summary", ""),
            "mtf_validation": llm_response.get("mtf_validation", ""),
            "hold_category": "ESCALATION_VETO" if decision == "HOLD" and llm_response.get("decision") != "HOLD" else "CEO_HOLD"
        }
