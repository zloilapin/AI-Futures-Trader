import json
from typing import Dict, Any
import os

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class CEOAgent(BaseAgent):
    """
    The Chief Investment Officer (CIO / CEO) of the trading syndicate.
    
    Uses Llama 70B as the Primary CEO, and Gemini 3.7 Flash as the Escalation Model 
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
            llm_response = await self.generate_json(full_prompt, required_keys=["decision", "score_breakdown", "reasoning_en"])
        except Exception as e:
            self.logger.warning(f"[{self.name}] Primary LLM failed: {e}")
            return {"decision": "ERROR", "conviction": 0, "hold_category": "LLM_ERROR", "reasoning_en": f"Primary LLM failed: {e}"}

        decision = str(llm_response.get("decision", "ERROR")).upper()
        if decision == "ERROR":
            return {"decision": "ERROR", "conviction": 0, "hold_category": "LLM_ERROR", "reasoning_en": llm_response.get("reasoning", "LLM Error")}
        
        breakdown = llm_response.get("score_breakdown", {})
        decision, conviction = self._validate_and_compute_score(decision, breakdown)
        
        reasoning = llm_response.get("reasoning_en", "")
        
        self.logger.info(f"[{self.name}] Primary CEO Llama 70B Decision: {decision} (Conf: {conviction}%)")
        print(f"👔 [CEO Llama 70B] {decision} ({conviction}%)")
        
        final_hold_category = "NONE"
        
        # ESCALATION MODEL LOGIC
        if conviction >= 80:
            self.logger.info(f"[{self.name}] High conviction {decision} ({conviction}% >= 80%). Bypassing escalation.")
        else:
            self.logger.info(f"[{self.name}] Conviction < 80% ({decision} {conviction}%). Escalating to Gemini...")
            print(f"⚠️ [Escalation] Недостаточная уверенность ({decision} {conviction}%). Передача дела в Gemini 3.7 Flash для поиска возможностей...")
            
            escalation_prompt = f"""You are the Supreme Escalation AI (Gemini 3.7 Flash) for an elite crypto prop-trading firm.
The Primary CEO (Llama 70B) has proposed a {decision} on {symbol} with a conviction of {conviction}%.
Your job is to review the exact same data and provide a FINAL decisive verdict.
If the primary CEO missed a strong setup and defaulted to HOLD, you must OVERRIDE and find the LONG/SHORT opportunity.
If the setup is truly weak, confirm the HOLD.

Primary CEO Reasoning:
{reasoning}

Here is the raw data:
{data_string}

Provide a JSON with your final decision. You can confirm the trade with high confidence, or VETO it with a HOLD.
SCHEMA:
{{
  "decision": "LONG",
  "score_breakdown": {{
    "candle": 16,
    "orderbook": 12,
    "derivatives": 18,
    "indicators": 13,
    "news": -2,
    "mtf": 17
  }},
  "reasoning_en": "Your detailed escalation review reasoning"
}}
"""
            try:
                original_llm = self.llm_client
                self.llm_client = self.escalation_llm
                try:
                    k3_response = await self.generate_json(escalation_prompt, required_keys=["decision", "score_breakdown", "reasoning_en"])
                finally:
                    self.llm_client = original_llm
                
                decision = str(k3_response.get("decision", "ERROR")).upper()
                if decision == "ERROR":
                    raise ValueError("Gemini returned ERROR")
                    
                k3_breakdown = k3_response.get("score_breakdown", {})
                decision, conviction = self._validate_and_compute_score(decision, k3_breakdown)
                
                k3_reasoning = k3_response.get("reasoning_en", "")
                
                reasoning = f"[Primary CEO: {reasoning}]\n\n[ESCALATION GEMINI VERDICT: {k3_reasoning}]"
                self.logger.info(f"[{self.name}] Escalation Gemini Final Decision: {decision} ({conviction}%)")
                print(f"🧠 [Gemini 3.7 Flash] Вердикт: {decision} ({conviction}%)")
            except Exception as e:
                self.logger.error(f"[{self.name}] Escalation LLM failed: {e}")
                decision = "ERROR"
                conviction = 0
                reasoning += f"\n\n[ESCALATION FAILED: {e}. Strict Fallback triggered.]"

        # Deterministic Decision Engine for HOLD Category
        if decision == "HOLD":
            final_hold_category = self._determine_hold_category(analyst_reports, conviction)
        else:
            final_hold_category = "NONE"

        return {
            "decision": decision,
            "conviction": conviction,
            "reasoning_en": reasoning,
            "reasoning_ru": llm_response.get("reasoning_ru", ""),
            "consensus_summary": llm_response.get("consensus_summary", ""),
            "mtf_validation": llm_response.get("mtf_validation", ""),
            "hold_category": final_hold_category if decision == "HOLD" else "NONE"
        }

    def _determine_hold_category(self, analyst_reports: list, conviction: int) -> str:
        if conviction < 80 and conviction > 0:
            return "LOW_CONFIDENCE"
            
        signals = [r.get("signal", "NEUTRAL").upper() for r in analyst_reports if isinstance(r, dict)]
        bullish = signals.count("BULLISH") + signals.count("LONG")
        bearish = signals.count("BEARISH") + signals.count("SHORT")
        
        if bullish > 0 and bearish > 0:
            return "ANALYST_DISAGREEMENT"
            
        news = next((r for r in analyst_reports if isinstance(r, dict) and r.get("agent_name") == "News_Agent"), None)
        if news and news.get("signal", "NEUTRAL").upper() in ["BEARISH", "SHORT"] and bullish > 0:
            return "NEWS_RISK"
            
        return "LOW_EDGE"

    def _validate_and_compute_score(self, decision: str, breakdown: dict) -> tuple[str, int]:
        if decision == "ERROR":
            return "ERROR", 0
            
        max_weights = {
            "candle": 20,
            "orderbook": 15,
            "derivatives": 20,
            "indicators": 15,
            "news": 10,
            "mtf": 20
        }
        
        net_score = 0
        if isinstance(breakdown, dict):
            for k, v in breakdown.items():
                try:
                    val = float(v)
                    key = k.lower().replace(" ", "").replace("_", "")
                    
                    limit = 0
                    if "candle" in key: limit = max_weights["candle"]
                    elif "orderbook" in key or "ob" in key: limit = max_weights["orderbook"]
                    elif "deriv" in key or "oi" in key or "funding" in key: limit = max_weights["derivatives"]
                    elif "indicator" in key: limit = max_weights["indicators"]
                    elif "news" in key or "sentiment" in key: limit = max_weights["news"]
                    elif "mtf" in key or "timeframe" in key: limit = max_weights["mtf"]
                    else: limit = 20
                    
                    val = max(-limit, min(limit, val))
                    net_score += val
                except (ValueError, TypeError):
                    continue
                    
        conviction = min(100, int(abs(net_score)))
        
        # Prevent math hallucinations
        if decision == "LONG" and net_score < 0:
            self.logger.warning(f"[{self.name}] Math hallucination: Decision is LONG but net_score is {net_score}. Overriding to HOLD.")
            decision = "HOLD"
        elif decision == "SHORT" and net_score > 0:
            self.logger.warning(f"[{self.name}] Math hallucination: Decision is SHORT but net_score is {net_score}. Overriding to HOLD.")
            decision = "HOLD"
            
        return decision, conviction
