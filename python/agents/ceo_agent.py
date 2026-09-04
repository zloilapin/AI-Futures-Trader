import json
from typing import Dict, Any, Tuple
import os
import re

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient


class ScoreResult(dict):
    """
    Holds the deterministic scoring result of CEOAgent.
    Acts as a dictionary containing:
      - decision: 'LONG' | 'SHORT' | 'HOLD'
      - directional_confidence: 0..100
      - entry_quality: 0..100
      - conviction: 0..100 (alias for entry_quality for pipeline & risk manager compatibility)
      - risk_score: 0..100
      - risk_penalties: -30..0
      - trade_action: 'ENTER' | 'WAIT_FOR_PULLBACK' | 'REDUCE_SIZE' | 'HOLD'
      - raw_net_score: float
    
    Supports 2-tuple unpacking for 100% backward compatibility:
      decision, conviction = result
    """
    def __iter__(self):
        return iter([self["decision"], self["conviction"]])


class CEOAgent(BaseAgent):
    """
    The Chief Investment Officer (CIO / CEO) of the trading syndicate.
    
    Uses Llama 70B as the Primary CEO, and Gemini 3.7 Flash as the Escalation Model 
    for medium-confidence trades.
    
    Architecture:
    - Trend strength determines DIRECTIONAL CONFIDENCE (0..100).
    - Market risks determine ENTRY QUALITY (0..100) via mandatory risk penalties (0..-30).
    - Strict deterministic floor ensures extreme RSI (>90) and sentiment extremes cannot be bypassed.
    """
    
    def __init__(self, logger: TradeLogger, primary_llm: LLMClient, escalation_llm: LLMClient):
        super().__init__("CEO_Agent", logger, primary_llm)
        self.escalation_llm = escalation_llm

        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "ceo_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes the 4-Tier Escalation Model logic with separated Directional Confidence
        and Risk-Adjusted Entry Quality.
        """
        symbol = data.get("symbol")
        analyst_reports = data.get("subordinate_analyst_reports", [])
        mtf_data = data.get("multi_timeframe_context", {})
        historical_context = data.get("historical_context", {})

        self.logger.info(f"[{self.name}] Llama 70B (Judge) анализирует дебаты Bull vs Bear по {symbol}...")
        
        payload = {
            "target_symbol": symbol,
            "multi_timeframe_context": mtf_data,
            "bull_thesis": data.get("bull_thesis", {}),
            "bear_thesis": data.get("bear_thesis", {}),
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

        raw_decision = str(llm_response.get("decision", "ERROR")).upper()
        if raw_decision == "ERROR":
            return {"decision": "ERROR", "conviction": 0, "hold_category": "LLM_ERROR", "reasoning_en": llm_response.get("reasoning", "LLM Error")}
        
        breakdown = llm_response.get("score_breakdown", {})
        score_res = self._validate_and_compute_score(raw_decision, breakdown, market_context=data)
        
        decision = score_res["decision"]
        conviction = score_res["conviction"]
        directional_confidence = score_res["directional_confidence"]
        entry_quality = score_res["entry_quality"]
        total_penalty = score_res["risk_penalties"]
        risk_score = score_res["risk_score"]
        trade_action = score_res["trade_action"]
        
        reasoning = llm_response.get("reasoning_en", "")
        
        if decision == "HOLD":
            log_detail = "Decision: HOLD (Conf: N/A)"
            print(f"👔 [CEO Llama 70B] HOLD (Нет направленного преимущества)")
        else:
            log_detail = f"Decision: {decision} (DirConf: {directional_confidence}%, RiskPenalty: {total_penalty}, EntryQuality: {entry_quality}%, Action: {trade_action})"
            print(f"👔 [CEO Llama 70B] {decision} (DirConf: {directional_confidence}%, EntryQuality: {entry_quality}% -> {trade_action})")
            
        self.logger.info(f"[{self.name}] Primary CEO Llama 70B: {log_detail}")
        
        final_hold_category = "NONE"
        
        # ESCALATION MODEL LOGIC (Cost Optimized)
        primary_decision = decision
        primary_conviction = conviction
        primary_dir_conf = directional_confidence
        primary_entry_quality = entry_quality
        escalated = False
        gemini_decision_log = "N/A"
        gemini_conv_log = "N/A"
        
        if decision == "HOLD":
            self.logger.info(f"[{self.name}] Primary CEO decided HOLD. Bypassing escalation to save API costs.")
            print(f"⏩ [Escalation Bypassed] Рынок не имеет явного тренда (HOLD). Gemini не вызывается для экономии API.")
        elif conviction >= 80:
            self.logger.info(f"[{self.name}] High conviction {decision} (EntryQuality {conviction}% >= 80%). Bypassing escalation.")
            print(f"⏩ [Escalation Bypassed] Качество входа Llama достаточно высоко ({conviction}%). Gemini не вызывается.")
        elif conviction < 60:
            self.logger.info(f"[{self.name}] Entry Quality low ({decision} {conviction}% < 60%). Bypassing escalation.")
            print(f"⏩ [Escalation Bypassed] Слишком низкое качество входа Llama ({conviction}% < 60%). Пропуск сделки (HOLD/WAIT).")
        else:
            escalated = True
            self.logger.info(f"[{self.name}] Conviction {conviction}% (60-79%). Escalating to Gemini...")
            print(f"⚠️ [Escalation] Спорный сетап ({decision} {conviction}%). Подключаем Gemini 3.7 Flash для финального вердикта...")
            
            escalation_prompt = f"""You are the Supreme Escalation AI (Gemini 3.7 Flash) for an elite crypto prop-trading firm.
The Primary CEO (Llama 70B) has proposed a {decision} on {symbol} with Directional Confidence of {directional_confidence}%, Risk Penalty of {total_penalty}, and Entry Quality (Conviction) of {conviction}%.
Proposed Trade Action: {trade_action}.
Your job is to review the exact same data and provide a FINAL decisive verdict.
Evaluate both trend strength (directional confidence) and counter-risks (RSI extremes, sentiment, divergences).
If the primary CEO missed a strong setup and defaulted to HOLD, you must OVERRIDE and find the LONG/SHORT opportunity.
If the setup is truly weak or too risky, confirm HOLD or WAIT.

Primary CEO Reasoning:
{reasoning}

Here is the raw data:
{data_string}

Provide a JSON strictly matching this schema:
{{
  "decision": "LONG | SHORT | HOLD",
  "directional_confidence": 75,
  "risk_score": 50,
  "entry_quality": 65,
  "trade_action": "ENTER | WAIT_FOR_PULLBACK | REDUCE_SIZE | HOLD",
  "score_breakdown": {{
    "bull_argument": 25,
    "bear_argument": -5,
    "mtf_trend": 15,
    "risk_penalties": {{
      "rsi_extreme": 0,
      "sentiment_euphoria": 0,
      "total": 0
    }}
  }},
  "winning_argument": "Bull / Bear / Neither",
  "consensus_summary": "Your detailed escalation review reasoning",
  "reasoning_en": "Step-by-step CIO executive summary"
}}
"""
            try:
                original_llm = self.llm_client
                self.llm_client = self.escalation_llm
                try:
                    k3_response = await self.generate_json(escalation_prompt, required_keys=["decision", "score_breakdown", "reasoning_en"])
                finally:
                    self.llm_client = original_llm
                
                gemini_raw_decision = str(k3_response.get("decision", "ERROR")).upper()
                if gemini_raw_decision == "ERROR":
                    raise ValueError("Gemini returned ERROR")
                    
                k3_breakdown = k3_response.get("score_breakdown", {})
                gemini_score_res = self._validate_and_compute_score(gemini_raw_decision, k3_breakdown, market_context=data)
                
                gemini_decision = gemini_score_res["decision"]
                gemini_conviction = gemini_score_res["conviction"]
                gemini_dir_conf = gemini_score_res["directional_confidence"]
                gemini_entry_qual = gemini_score_res["entry_quality"]
                
                gemini_decision_log = gemini_decision
                gemini_conv_log = gemini_conviction
                k3_reasoning = k3_response.get("reasoning_en", "")
                
                reasoning = f"[Primary CEO: {reasoning}]\n\n[ESCALATION GEMINI VERDICT: {k3_reasoning}]"
                self.logger.info(f"[{self.name}] Escalation Gemini Final Decision: {gemini_decision} (DirConf: {gemini_dir_conf}%, EntryQuality: {gemini_entry_qual}%)")
                print(f"🧠 [Gemini 3.7 Flash] Вердикт: {gemini_decision} (DirConf: {gemini_dir_conf}%, EntryQuality: {gemini_entry_qual}%)")
                
                # Consensus Check logic
                if gemini_decision == primary_decision:
                    decision = gemini_decision
                    conviction = int((primary_conviction * 0.6) + (gemini_conviction * 0.4))
                    directional_confidence = int((primary_dir_conf * 0.6) + (gemini_dir_conf * 0.4))
                    entry_quality = conviction
                    if conviction >= 75:
                        trade_action = "ENTER"
                    elif directional_confidence >= 75 and conviction < 70:
                        trade_action = "WAIT_FOR_PULLBACK"
                    else:
                        trade_action = "REDUCE_SIZE"
                    print(f"🤝 [Consensus] Модели пришли к согласию! Подтвержден {decision}. Entry Quality: {conviction}% (Llama: {primary_conviction}%, Gemini: {gemini_conviction}%)")
                else:
                    print(f"⚔️ [Conflict] Llama ({primary_decision}) и Gemini ({gemini_decision}) разошлись во мнениях. Итог: HOLD.")
                    decision = "HOLD"
                    conviction = 0
                    entry_quality = 0
                    directional_confidence = 0
                    trade_action = "HOLD"
                    
            except Exception as e:
                self.logger.error(f"[{self.name}] Escalation LLM failed: {e}")
                decision = "ERROR"
                conviction = 0
                entry_quality = 0
                trade_action = "HOLD"
                reasoning += f"\n\n[ESCALATION FAILED: {e}. Strict Fallback triggered.]"

        # Deterministic Decision Engine for HOLD Category
        if decision == "HOLD":
            final_hold_category = self._determine_hold_category(analyst_reports, conviction)
        else:
            final_hold_category = "NONE"

        # Log confidence stats for successful trades
        if decision in ["LONG", "SHORT"]:
            try:
                import datetime
                with open("confidence_stats.log", "a", encoding="utf-8") as f:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{ts}] {symbol} | Result: {decision} | Llama: {primary_decision} ({primary_conviction}%) | Gemini: {gemini_decision_log} ({gemini_conv_log}%)\n")
            except Exception as e:
                self.logger.error(f"Failed to log confidence stats: {e}")

        return {
            "decision": decision,
            "conviction": conviction,
            "directional_confidence": directional_confidence,
            "entry_quality": entry_quality,
            "risk_score": risk_score,
            "risk_penalties": total_penalty,
            "trade_action": trade_action,
            "reasoning_en": reasoning,
            "reasoning_ru": llm_response.get("reasoning_ru", ""),
            "consensus_summary": llm_response.get("consensus_summary", ""),
            "mtf_validation": llm_response.get("mtf_validation", ""),
            "hold_category": final_hold_category if decision == "HOLD" else "NONE",
            "primary_conviction": primary_conviction,
            "escalated": escalated
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

    def _extract_market_metrics(self, data: Dict[str, Any]) -> Tuple[float | None, float | None]:
        """
        Extracts RSI and Fear & Greed index from market_data, indicators, news_data,
        or subordinate analyst reports.
        """
        rsi = None
        fear_greed = None
        
        if not isinstance(data, dict):
            return rsi, fear_greed

        # 1. Direct indicators / news_data
        indicators = data.get("indicators") or data.get("market_data", {}).get("indicators", {})
        if isinstance(indicators, dict) and "rsi_14" in indicators:
            try:
                rsi = float(indicators["rsi_14"])
            except (ValueError, TypeError):
                pass
                
        news_data = data.get("news_data") or data.get("market_data", {}).get("news_data", {})
        if isinstance(news_data, dict):
            fg = news_data.get("fear_and_greed_index")
            if fg is not None:
                try:
                    fear_greed = float(fg)
                except (ValueError, TypeError):
                    pass
                    
        # Direct top-level rsi or fear_greed (useful in tests/payloads)
        if rsi is None and "rsi" in data:
            try:
                rsi = float(data["rsi"])
            except (ValueError, TypeError):
                pass
        if fear_greed is None and "fear_greed" in data:
            try:
                fear_greed = float(data["fear_greed"])
            except (ValueError, TypeError):
                pass
                
        # 2. Fallback: Parse from analyst reports reasoning text
        analyst_reports = data.get("subordinate_analyst_reports", [])
        if isinstance(analyst_reports, list):
            for r in analyst_reports:
                if not isinstance(r, dict):
                    continue
                text = str(r.get("reasoning", ""))
                if rsi is None:
                    m_rsi = re.search(r'RSI[^\d]*?(\d+(?:\.\d+)?)', text, re.IGNORECASE)
                    if m_rsi:
                        try:
                            rsi = float(m_rsi.group(1))
                        except (ValueError, TypeError):
                            pass
                if fear_greed is None:
                    m_fg = re.search(r'(?:Fear|Greed)[^\d]*?(\d{1,3})', text, re.IGNORECASE)
                    if m_fg:
                        try:
                            fear_greed = float(m_fg.group(1))
                        except (ValueError, TypeError):
                            pass
                            
        return rsi, fear_greed

    def _calculate_minimum_risk_penalty(self, decision: str, rsi: float | None, fear_greed: float | None) -> int:
        """
        Deterministic minimum risk penalty floor.
        Ensures that extreme technical or sentiment risks cannot be zeroed out by the LLM.
        Returns a negative integer between -30 and 0.
        """
        penalty = 0
        dec = str(decision).upper()
        
        if dec == "LONG":
            if rsi is not None:
                if rsi > 95:
                    penalty -= 15
                elif rsi > 90:
                    penalty -= 12
                elif rsi > 80:
                    penalty -= 10
                elif rsi > 70:
                    penalty -= 5
                    
            if fear_greed is not None:
                if fear_greed >= 80:
                    penalty -= 10
                elif fear_greed >= 70:
                    penalty -= 5
                    
        elif dec == "SHORT":
            if rsi is not None:
                if rsi < 5:
                    penalty -= 15
                elif rsi < 10:
                    penalty -= 12
                elif rsi < 20:
                    penalty -= 10
                elif rsi < 30:
                    penalty -= 5
                    
            if fear_greed is not None:
                if fear_greed <= 20:
                    penalty -= 10
                elif fear_greed <= 30:
                    penalty -= 5
                    
        return max(-30, penalty)

    def _extract_llm_penalty(self, breakdown: dict) -> int:
        """
        Extracts the risk penalty proposed by the LLM in score_breakdown.
        Returns a negative integer between -30 and 0.
        """
        if not isinstance(breakdown, dict):
            return 0
            
        penalties = breakdown.get("risk_penalties")
        extracted_penalty = 0.0
        
        if isinstance(penalties, dict):
            if "total" in penalties:
                try:
                    extracted_penalty = -abs(float(penalties["total"]))
                except (ValueError, TypeError):
                    pass
            else:
                total = 0.0
                for k, v in penalties.items():
                    try:
                        total += abs(float(v))
                    except (ValueError, TypeError):
                        pass
                extracted_penalty = -total
        elif penalties is not None:
            try:
                extracted_penalty = -abs(float(penalties))
            except (ValueError, TypeError):
                pass
        else:
            # Check individual keys
            for k in ["risk_penalty", "risk_adjustment", "rsi_penalty", "rsi_extreme"]:
                if k in breakdown:
                    try:
                        extracted_penalty -= abs(float(breakdown[k]))
                    except (ValueError, TypeError):
                        pass
                        
        return max(-30, int(extracted_penalty))

    def _validate_and_compute_score(
        self,
        decision: str,
        breakdown: dict,
        market_context: Dict[str, Any] = None
    ) -> ScoreResult:
        if decision == "ERROR":
            return ScoreResult({
                "decision": "ERROR",
                "conviction": 0,
                "directional_confidence": 0,
                "entry_quality": 0,
                "risk_score": 0,
                "risk_penalties": 0,
                "trade_action": "HOLD",
                "raw_net_score": 0.0
            })

        max_weights = {
            "bull_argument": 50,
            "bear_argument": 50,
            "mtf_trend": 50
        }
        
        bull_score = 0.0
        bear_score = 0.0
        mtf_score = 0.0
        
        if isinstance(breakdown, dict):
            for k, v in breakdown.items():
                try:
                    if isinstance(v, dict):
                        continue
                    val = float(v)
                    key = k.lower().replace(" ", "").replace("_", "")
                    
                    if "bull" in key:
                        limit = max_weights["bull_argument"]
                        val = abs(val) # Positive for Bull
                        bull_score += max(0, min(limit, val))
                    elif "bear" in key:
                        limit = max_weights["bear_argument"]
                        val = -abs(val) # Negative for Bear
                        bear_score += max(-limit, min(0, val))
                    elif "mtf" in key or "trend" in key:
                        limit = max_weights["mtf_trend"]
                        mtf_score += max(-limit, min(limit, val))
                except (ValueError, TypeError):
                    continue

        net_directional_score = bull_score + bear_score + mtf_score

        # Rule 1: Direction is strictly determined by sign, never abs()
        if net_directional_score > 0:
            calculated_decision = "LONG"
        elif net_directional_score < 0:
            calculated_decision = "SHORT"
        else:
            calculated_decision = "HOLD"

        # abs() is strictly for magnitude (confidence)
        directional_confidence = min(100, max(0, int(abs(net_directional_score))))

        # Detect math hallucination / direction conflict
        req_decision = str(decision).upper()
        if req_decision == "LONG" and net_directional_score < 0:
            self.logger.warning(f"[{self.name}] Math hallucination: LLM proposed LONG but net score is {net_directional_score:.1f} (Bearish). Overriding to HOLD.")
            return ScoreResult({
                "decision": "HOLD",
                "conviction": 0,
                "directional_confidence": 0,
                "entry_quality": 0,
                "risk_score": 50,
                "risk_penalties": 0,
                "trade_action": "HOLD",
                "raw_net_score": net_directional_score
            })
        elif req_decision == "SHORT" and net_directional_score > 0:
            self.logger.warning(f"[{self.name}] Math hallucination: LLM proposed SHORT but net score is {net_directional_score:.1f} (Bullish). Overriding to HOLD.")
            return ScoreResult({
                "decision": "HOLD",
                "conviction": 0,
                "directional_confidence": 0,
                "entry_quality": 0,
                "risk_score": 50,
                "risk_penalties": 0,
                "trade_action": "HOLD",
                "raw_net_score": net_directional_score
            })
        elif req_decision == "HOLD" and calculated_decision == "HOLD":
            return ScoreResult({
                "decision": "HOLD",
                "conviction": 0,
                "directional_confidence": 0,
                "entry_quality": 0,
                "risk_score": 0,
                "risk_penalties": 0,
                "trade_action": "HOLD",
                "raw_net_score": net_directional_score
            })

        # Calculate Risk Penalties
        rsi, fear_greed = self._extract_market_metrics(market_context or {})
        llm_penalty = self._extract_llm_penalty(breakdown)
        deterministic_penalty = self._calculate_minimum_risk_penalty(calculated_decision, rsi, fear_greed)

        # Strictest penalty wins (both are negative/zero, min(-10, -15) selects -15)
        total_penalty = min(llm_penalty, deterministic_penalty)
        # Cap cumulative penalties at MAX_TOTAL_RISK_PENALTY = -30
        total_penalty = max(-30, total_penalty)

        # Rule 3: Entry Quality formula
        entry_quality = max(0, directional_confidence + total_penalty)
        conviction = entry_quality

        # Rule 2: Separate risk_score (0..100) vs risk_penalties (-30..0)
        risk_score = min(100, int((abs(total_penalty) / 30.0) * 100)) if total_penalty < 0 else 0

        # Rule 4: Action derivation while preserving directional bias
        if calculated_decision == "HOLD" or directional_confidence < 60:
            trade_action = "HOLD"
        elif directional_confidence >= 75 and entry_quality < 70:
            trade_action = "WAIT_FOR_PULLBACK"
        elif entry_quality >= 75:
            trade_action = "ENTER"
        else:
            trade_action = "REDUCE_SIZE"

        return ScoreResult({
            "decision": calculated_decision,
            "directional_confidence": directional_confidence,
            "entry_quality": entry_quality,
            "conviction": conviction,
            "risk_score": risk_score,
            "risk_penalties": total_penalty,
            "trade_action": trade_action,
            "raw_net_score": net_directional_score
        })
