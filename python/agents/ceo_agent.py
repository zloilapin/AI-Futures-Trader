import json
from typing import Dict, Any, Tuple, Optional
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
    
    Uses dynamic OpenRouter models (e.g. Qwen, DeepSeek) for the Primary CEO and Escalation.
    
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

        self.logger.info(f"[{self.name}] {self.llm_client.model_name} (Judge) анализирует дебаты Bull vs Bear по {symbol}...")
        
        # Strip heavy candle arrays to save ~1,500 prompt tokens
        clean_mtf = dict(mtf_data) if isinstance(mtf_data, dict) else {}
        for tf_k in ["tf_15m", "tf_1h", "tf_4h"]:
            if tf_k in clean_mtf and isinstance(clean_mtf[tf_k], dict):
                clean_mtf[tf_k] = {k: v for k, v in clean_mtf[tf_k].items() if k != "candles_20"}

        payload = {
            "target_symbol": symbol,
            "strategy_mode": data.get("strategy_mode", "TREND_FOLLOWING"),
            "direction_bias": data.get("direction_bias", "NEUTRAL"),
            "router_reasoning": data.get("router_reasoning", ""),
            "macro_regime": data.get("macro_regime", "RANGE_CHOPPY"),
            "macro_profile": data.get("macro_profile", "BALANCED"),
            "multi_timeframe_context": clean_mtf,
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
            print(f"👔 [CEO {self.llm_client.model_name}] HOLD (Нет направленного преимущества)")
        else:
            log_detail = f"Decision: {decision} (DirConf: {directional_confidence}%, RiskPenalty: {total_penalty}, EntryQuality: {entry_quality}%, Action: {trade_action})"
            print(f"👔 [CEO {self.llm_client.model_name}] {decision} (DirConf: {directional_confidence}%, EntryQuality: {entry_quality}% -> {trade_action})")
            
        self.logger.info(f"[{self.name}] Primary CEO ({self.llm_client.model_name}): {log_detail}")
        
        final_hold_category = "NONE"
        
        # ESCALATION MODEL LOGIC (Cost Optimized)
        primary_decision = decision
        primary_conviction = conviction
        primary_dir_conf = directional_confidence
        primary_entry_quality = entry_quality
        escalated = False
        esc_decision_log = "N/A"
        esc_conv_log = "N/A"
        disputed_arbitration = False
        
        if decision == "HOLD":
            self.logger.info(f"[{self.name}] Primary CEO decided HOLD. Bypassing escalation to save API costs.")
            print(f"⏩ [Escalation Bypassed] Рынок не имеет явного тренда (HOLD). Вторая модель ({self.escalation_llm.model_name}) не вызывается для экономии API.")
        elif self.llm_client.model_name == self.escalation_llm.model_name:
            self.logger.info(f"[{self.name}] Primary and Escalation models are identical ({self.llm_client.model_name}). Bypassing escalation to prevent echo chamber.")
            print(f"⏩ [Escalation Bypassed] Основная и эскалационная модели совпали ({self.llm_client.model_name}). Эскалация отменена (предотвращение эхо-камеры).")
        elif conviction >= 80:
            self.logger.info(f"[{self.name}] High conviction {decision} (EntryQuality {conviction}% >= 80%). Bypassing escalation.")
            print(f"⏩ [Escalation Bypassed] Качество входа Primary CEO достаточно высоко ({conviction}%). Вторая модель ({self.escalation_llm.model_name}) не вызывается.")
        elif conviction < 60:
            self.logger.info(f"[{self.name}] Entry Quality low ({decision} {conviction}% < 60%). Bypassing escalation.")
            print(f"⏩ [Escalation Bypassed] Слишком низкое качество входа Primary CEO ({conviction}% < 60%). Пропуск сделки (HOLD/WAIT).")
        else:
            escalated = True
            self.logger.info(f"[{self.name}] Conviction {conviction}% (60-79%). Escalating to {self.escalation_llm.model_name}...")
            print(f"⚠️ [Escalation] Спорный сетап ({decision} {conviction}%). Подключаем {self.escalation_llm.model_name} для финального вердикта...")
            
            escalation_prompt = f"""You are the Supreme Escalation AI ({self.escalation_llm.model_name}) for an elite crypto prop-trading firm.
The Primary CEO ({self.llm_client.model_name}) has proposed a {decision} on {symbol} with Directional Confidence of {directional_confidence}%, Risk Penalty of {total_penalty}, and Entry Quality (Conviction) of {conviction}%.
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

CRITICAL: Return RAW JSON ONLY. Your output MUST start immediately with '{{' and end with '}}'. Do NOT output markdown bullet lists, internal reasoning, or conversational text outside the JSON.
"""
            arbitration_failed = False
            arbitration_error = ""
            esc_response = None
            esc_decision = "HOLD"
            esc_conviction = 0
            esc_dir_conf = 0
            esc_entry_qual = 0
            disputed_arbitration = False

            try:
                original_llm = self.llm_client
                self.llm_client = self.escalation_llm
                try:
                    esc_response = await self.generate_json(escalation_prompt, required_keys=["decision", "score_breakdown", "reasoning_en"])
                finally:
                    self.llm_client = original_llm
            except Exception as e:
                arbitration_failed = True
                arbitration_error = str(e)

            if not arbitration_failed and esc_response:
                esc_raw_decision = str(esc_response.get("decision", "ERROR")).upper().strip()
                if esc_raw_decision in ["LONG", "SHORT", "HOLD"]:
                    k3_breakdown = esc_response.get("score_breakdown", {})
                    esc_score_res = self._validate_and_compute_score(esc_raw_decision, k3_breakdown, market_context=data)
                    
                    if esc_score_res.get("math_conflict", False):
                        # Format/arithmetic inconsistency by arbitrator
                        # Rule: "Ошибка формата или арифметики у арбитра не должна считаться доказательством неправильности исходного сигнала."
                        self.logger.warning(f"[{self.name}] Арбитр {self.escalation_llm.model_name} допустил математическую нестыковку. Не аннулируем сигнал Primary CEO.")
                        arbitration_failed = True
                        arbitration_error = "Arbitrator score breakdown math conflict"
                    else:
                        esc_decision = esc_score_res["decision"]
                        esc_conviction = esc_score_res["conviction"]
                        esc_dir_conf = esc_score_res["directional_confidence"]
                        esc_entry_qual = esc_score_res["entry_quality"]
                        esc_decision_log = esc_decision
                        esc_conv_log = esc_conviction
                        k3_reasoning = esc_response.get("reasoning_en", "")
                        reasoning = f"[Primary CEO: {reasoning}]\n\n[ESCALATION VERDICT ({self.escalation_llm.model_name}): {k3_reasoning}]"
                        self.logger.info(f"[{self.name}] Escalation ({self.escalation_llm.model_name}) Final Decision: {esc_decision} (DirConf: {esc_dir_conf}%, EntryQuality: {esc_entry_qual}%)")
                        print(f"🧠 [{self.escalation_llm.model_name}] Вердикт: {esc_decision} (DirConf: {esc_dir_conf}%, EntryQuality: {esc_entry_qual}%)")
                else:
                    arbitration_failed = True
                    arbitration_error = f"Invalid decision value: {esc_raw_decision}"

            # --- ARBITRATION RESOLUTION ENGINE ---
            # Case 1: Full Consensus (both models agree)
            if not arbitration_failed and esc_decision == primary_decision:
                decision = esc_decision
                conviction = int((primary_conviction * 0.6) + (esc_conviction * 0.4))
                directional_confidence = int((primary_dir_conf * 0.6) + (esc_dir_conf * 0.4))
                entry_quality = int((primary_entry_quality * 0.6) + (esc_entry_qual * 0.4))
                if conviction >= 75:
                    trade_action = "ENTER"
                elif directional_confidence >= 75 and conviction < 70:
                    trade_action = "WAIT_FOR_PULLBACK"
                else:
                    trade_action = "REDUCE_SIZE"
                print(f"🤝 [Consensus] Модели пришли к согласию! Подтвержден {decision}. Conviction: {conviction}% (Primary: {primary_conviction}%, Esc: {esc_conviction}%) | Entry Quality: {entry_quality}%")

            # Case 2: Model Conflict OR Arbitrator Format/Math Failure
            else:
                disputed_arbitration = True
                dispute_reason = arbitration_error if arbitration_failed else f"Конфликт моделей ({primary_decision} vs {esc_decision})"
                self.logger.warning(f"[{self.name}] {dispute_reason}. Передаем решение независимым правилам риска...")
                print(f"⚖️ [Arbitration Gate] {dispute_reason}. Сигнал Primary CEO ({primary_decision}) проверяется независимыми правилами риска...")
                
                is_risk_approved, risk_rationale, risk_metrics = self._evaluate_independent_risk_rules(analyst_reports, data, primary_decision)
                
                if is_risk_approved:
                    # Approved by independent risk rules:
                    # "Не давать Llama-8B безусловное право отменять сильный сигнал Qwen-72B. Но и не разрешать Qwen автоматически открывать сделку при уверенности 70%."
                    decision = primary_decision
                    # Discount conviction to safe corridor [60..65]
                    conviction = min(primary_conviction, 65)
                    directional_confidence = primary_dir_conf
                    entry_quality = conviction
                    # Force REDUCE_SIZE to prevent full-size exposure on disputed trade
                    trade_action = "REDUCE_SIZE"
                    reasoning += f"\n\n[INDEPENDENT RISK ARBITRATION APPROVED: {risk_rationale}. Sizing capped to REDUCE_SIZE (Conviction {conviction}%).]"
                    self.logger.info(f"[{self.name}] Независимые правила риска ОДОБРИЛИ {decision}: {risk_rationale}. Action: REDUCE_SIZE.")
                    print(f"✅ [Arbitration Gate] Независимые правила риска ПОДТВЕРДИЛИ {decision}: {risk_rationale}. Допуск с защитным объемом (REDUCE_SIZE, {conviction}%).")
                else:
                    # Independent risk rules rejected
                    decision = "HOLD"
                    conviction = 0
                    entry_quality = 0
                    directional_confidence = 0
                    trade_action = "HOLD"
                    final_hold_category = "INDEPENDENT_RISK_REJECTION"
                    reasoning += f"\n\n[INDEPENDENT RISK ARBITRATION REJECTED: {risk_rationale}. Trade converted to HOLD.]"
                    self.logger.info(f"[{self.name}] Независимые правила риска ОТКЛОНИЛИ {primary_decision}: {risk_rationale}. Итог: HOLD.")
                    print(f"🛡️ [Arbitration Gate] Независимые правила риска ОТКЛОНИЛИ сделку: {risk_rationale}. Итог: HOLD.")

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
                    f.write(f"[{ts}] {symbol} | Result: {decision} | {self.llm_client.model_name}: {primary_decision} ({primary_conviction}%) | {self.escalation_llm.model_name}: {esc_decision_log} ({esc_conv_log}%)\n")
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
            "escalated": escalated,
            "disputed_arbitration": disputed_arbitration
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

    def _evaluate_independent_risk_rules(
        self,
        analyst_reports: list,
        market_context: Dict[str, Any],
        candidate_decision: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Independent Risk Arbitrator:
        Evaluates deterministic technical signals and risk thresholds without relying on LLM consensus.
        Used when LLM models conflict or arbitrator experiences math/format failure.
        
        Returns:
            (is_approved: bool, reason: str, metrics: Dict[str, Any])
        """
        candidate = candidate_decision.upper().strip()
        if candidate not in ["LONG", "SHORT"]:
            return False, f"Candidate decision is {candidate}, not actionable", {}

        # 1. Independent Analyst Syndicate Count
        signals = [r.get("signal", "NEUTRAL").upper() for r in (analyst_reports or []) if isinstance(r, dict)]
        bullish_count = signals.count("BULLISH") + signals.count("LONG")
        bearish_count = signals.count("BEARISH") + signals.count("SHORT")
        
        supporting_analysts = bearish_count if candidate == "SHORT" else bullish_count
        opposing_analysts = bullish_count if candidate == "SHORT" else bearish_count

        # Requirement: At least 2 analysts support and at most 1 analyst opposes
        if supporting_analysts < 2:
            return False, f"Недостаточно поддержки аналитиков (за: {supporting_analysts} < 2)", {
                "supporting": supporting_analysts, "opposing": opposing_analysts
            }
        if opposing_analysts > 1:
            return False, f"Слишком высокое сопротивление аналитиков (против: {opposing_analysts} > 1)", {
                "supporting": supporting_analysts, "opposing": opposing_analysts
            }

        # 2. MTF Trend Alignment
        mtf_ctx = market_context.get("multi_timeframe_context", {}) if isinstance(market_context, dict) else {}
        tf_1h = mtf_ctx.get("tf_1h", {}) if isinstance(mtf_ctx, dict) and isinstance(mtf_ctx.get("tf_1h"), dict) else {}
        tf_4h = mtf_ctx.get("tf_4h", {}) if isinstance(mtf_ctx, dict) and isinstance(mtf_ctx.get("tf_4h"), dict) else {}
        
        trend_1h = tf_1h.get("trend", "neutral").lower()
        trend_4h = tf_4h.get("trend", "neutral").lower()

        if candidate == "SHORT":
            if trend_1h == "bullish" and trend_4h == "bullish":
                return False, "Контртренд: 1h и 4h таймфреймы оба бычьи", {"trend_1h": trend_1h, "trend_4h": trend_4h}
        elif candidate == "LONG":
            if trend_1h == "bearish" and trend_4h == "bearish":
                return False, "Контртренд: 1h и 4h таймфреймы оба медвежьи", {"trend_1h": trend_1h, "trend_4h": trend_4h}

        # 3. RSI Extrema Check
        rsi, fear_greed = self._extract_market_metrics(market_context or {})
        if rsi is not None:
            if candidate == "SHORT" and rsi < 25.0:
                return False, f"Экстремальная перепроданность для шорта (RSI={rsi:.1f} < 25)", {"rsi": rsi}
            elif candidate == "LONG" and rsi > 75.0:
                return False, f"Экстремальная перекупленность для лонга (RSI={rsi:.1f} > 75)", {"rsi": rsi}

        # 4. Derivatives Funding Check
        derivatives = market_context.get("derivatives_data", {}) if isinstance(market_context, dict) else {}
        funding = derivatives.get("funding_rate") or derivatives.get("funding_rate_decimal")
        if funding is not None:
            try:
                funding_val = float(funding)
                if candidate == "LONG" and funding_val > 0.0005:
                    return False, f"Фандинг перегружен в лонг ({funding_val:.4f})", {"funding": funding_val}
                elif candidate == "SHORT" and funding_val < -0.0005:
                    return False, f"Фандинг перегружен в шорт ({funding_val:.4f})", {"funding": funding_val}
            except (ValueError, TypeError):
                pass

        return True, f"Аналитики: {supporting_analysts} vs {opposing_analysts}, MTF тренд согласован", {
            "supporting": supporting_analysts,
            "opposing": opposing_analysts,
            "rsi": rsi,
            "trend_1h": trend_1h,
            "trend_4h": trend_4h
        }

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
        if isinstance(indicators, dict):
            for k in ["rsi_14", "rsi", "RSI", "RSI_14"]:
                if k in indicators and indicators[k] is not None:
                    try:
                        rsi = float(indicators[k])
                        break
                    except (ValueError, TypeError):
                        pass
                
        news_data = data.get("news_data") or data.get("market_data", {}).get("news_data", {})
        if isinstance(news_data, dict):
            for k in ["fear_and_greed_index", "sentiment_score", "fear_greed", "fng_index", "fng"]:
                if k in news_data and news_data[k] is not None:
                    try:
                        fear_greed = float(news_data[k])
                        break
                    except (ValueError, TypeError):
                        pass
            if fear_greed is None and "latest_event" in news_data:
                m_ev = re.search(r'Fear\s*(?:&|and)\s*Greed\s*Index\s*:\s*(\d+)', str(news_data["latest_event"]), re.IGNORECASE)
                if m_ev:
                    try:
                        fear_greed = float(m_ev.group(1))
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

    def _calculate_minimum_risk_penalty(
        self,
        decision: str,
        rsi: float | None,
        fear_greed: float | None,
        market_context: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Deterministic minimum risk penalty floor.
        Evaluates:
        1. RSI extremes & counter-divergence
        2. Sentiment euphoria/panic
        3. Overextension from EMA-20 base (late entry detection)
        4. Bollinger Band position exhaustion
        Returns a negative integer between -40 and 0.
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

        # Quantitative Entry Quality: Overextension & Late Entry Penalty
        if market_context and isinstance(market_context, dict) and dec in ["LONG", "SHORT"]:
            indicators = market_context.get("indicators", {})
            if isinstance(indicators, dict):
                ema_20 = float(indicators.get("ema_20") or 0.0)
                cur_price = float(market_context.get("price_data", {}).get("current_price") or indicators.get("current_price") or 0.0)
                atr_pct = float(indicators.get("atr_pct") or 1.5)
                bb_pos = float(indicators.get("bb_position_pct") or 50.0)

                # Overextension from EMA-20 (Late entry penalty: price already ran without pausing)
                if ema_20 > 0 and cur_price > 0:
                    if dec == "LONG" and cur_price > ema_20:
                        dist_pct = ((cur_price - ema_20) / cur_price) * 100.0
                        if dist_pct > max(1.2, 1.5 * atr_pct):
                            penalty -= 15  # Severe overextension (late entry)
                        elif dist_pct > max(0.8, 1.0 * atr_pct):
                            penalty -= 8   # Moderate overextension
                    elif dec == "SHORT" and cur_price < ema_20:
                        dist_pct = ((ema_20 - cur_price) / cur_price) * 100.0
                        if dist_pct > max(1.2, 1.5 * atr_pct):
                            penalty -= 15  # Severe overextension (late entry)
                        elif dist_pct > max(0.8, 1.0 * atr_pct):
                            penalty -= 8   # Moderate overextension

                # Bollinger Band Exhaustion
                if dec == "LONG" and bb_pos >= 92.0:
                    penalty -= 8  # Buying at upper band extreme
                elif dec == "SHORT" and bb_pos <= 8.0:
                    penalty -= 8  # Shorting at lower band extreme
                    
        return max(-40, penalty)

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
        req_decision = str(decision).upper().strip()
        if req_decision == "ERROR":
            return ScoreResult({
                "decision": "ERROR",
                "conviction": 0,
                "directional_confidence": 0,
                "entry_quality": 0,
                "risk_score": 0,
                "risk_penalties": 0,
                "trade_action": "HOLD",
                "raw_net_score": 0.0,
                "math_conflict": False
            })

        max_weights = {
            "bull_argument": 50,
            "bear_argument": 50,
            "mtf_trend": 50
        }
        
        raw_bull = 0.0
        raw_bear = 0.0
        raw_mtf = 0.0
        
        if isinstance(breakdown, dict):
            for k, v in breakdown.items():
                try:
                    if isinstance(v, dict):
                        continue
                    val = float(v)
                    key = k.lower().replace(" ", "").replace("_", "")
                    
                    if "bull" in key:
                        raw_bull += min(max_weights["bull_argument"], abs(val))
                    elif "bear" in key:
                        raw_bear += min(max_weights["bear_argument"], abs(val))
                    elif "mtf" in key or "trend" in key:
                        raw_mtf += min(max_weights["mtf_trend"], abs(val))
                except (ValueError, TypeError):
                    continue

        # Deterministic Sign Assignment & Direction Separation:
        # Check market context or direction bias for objective MTF trend alignment
        mtf_bias = "NEUTRAL"
        if market_context and isinstance(market_context, dict):
            direction_bias = str(market_context.get("direction_bias", "NEUTRAL")).upper()
            if direction_bias in ["SHORT", "BEARISH"]:
                mtf_bias = "BEARISH"
            elif direction_bias in ["LONG", "BULLISH"]:
                mtf_bias = "BULLISH"
            else:
                mtf_ctx = market_context.get("multi_timeframe_context", {})
                if isinstance(mtf_ctx, dict):
                    tf_1h = mtf_ctx.get("tf_1h", {}) if isinstance(mtf_ctx.get("tf_1h"), dict) else {}
                    tf_4h = mtf_ctx.get("tf_4h", {}) if isinstance(mtf_ctx.get("tf_4h"), dict) else {}
                    if tf_1h.get("trend") == "bearish" or tf_4h.get("trend") == "bearish":
                        mtf_bias = "BEARISH"
                    elif tf_1h.get("trend") == "bullish" or tf_4h.get("trend") == "bullish":
                        mtf_bias = "BULLISH"

        # If mtf_bias is still neutral, align trend contribution with the model's explicit trade direction
        if mtf_bias == "NEUTRAL":
            if req_decision == "SHORT":
                mtf_bias = "BEARISH"
            elif req_decision == "LONG":
                mtf_bias = "BULLISH"

        bull_mtf_contrib = raw_mtf if mtf_bias == "BULLISH" else 0.0
        bear_mtf_contrib = raw_mtf if mtf_bias == "BEARISH" else 0.0

        total_bull = raw_bull + bull_mtf_contrib
        total_bear = raw_bear + bear_mtf_contrib
        net_directional_score = total_bull - total_bear

        # Separate directional check from raw score numbers
        math_conflict = False
        if req_decision == "SHORT":
            if total_bull > total_bear + 20.0:
                math_conflict = True
                self.logger.warning(f"[{self.name}] Math hallucination: LLM proposed SHORT but net score is Bullish ({total_bull} vs {total_bear}).")
                calculated_decision = "HOLD"
                directional_confidence = 0
            else:
                calculated_decision = "SHORT"
                directional_confidence = min(100, max(0, int(total_bear if total_bear > 0 else abs(net_directional_score))))
        elif req_decision == "LONG":
            if total_bear > total_bull + 20.0:
                math_conflict = True
                self.logger.warning(f"[{self.name}] Math hallucination: LLM proposed LONG but net score is Bearish ({total_bear} vs {total_bull}).")
                calculated_decision = "HOLD"
                directional_confidence = 0
            else:
                calculated_decision = "LONG"
                directional_confidence = min(100, max(0, int(total_bull if total_bull > 0 else abs(net_directional_score))))
        else: # HOLD
            calculated_decision = "HOLD"
            directional_confidence = min(100, max(0, int(abs(net_directional_score))))

        # Calculate Risk Penalties
        rsi, fear_greed = self._extract_market_metrics(market_context or {})
        llm_penalty = self._extract_llm_penalty(breakdown)
        deterministic_penalty = self._calculate_minimum_risk_penalty(calculated_decision, rsi, fear_greed, market_context=market_context)

        # Strictest penalty wins (both are negative/zero, min(-10, -15) selects -15)
        total_penalty = min(llm_penalty, deterministic_penalty)
        # Cap cumulative penalties at MAX_TOTAL_RISK_PENALTY = -40
        total_penalty = max(-40, total_penalty)

        # Rule 3: Entry Quality formula (Risk-Adjusted execution quality)
        entry_quality = max(0, directional_confidence + total_penalty)
        conviction = entry_quality

        # Rule 2: Separate risk_score (0..100) vs risk_penalties (-40..0)
        risk_score = min(100, int((abs(total_penalty) / 40.0) * 100)) if total_penalty < 0 else 0

        # Rule 4: Action derivation separating Directional Confidence from Entry Quality:
        # Rule: Do not lower threshold from 60% just to increase trade count.
        # Rule: If an entry is late/overextended, wait for pullback or skip (HOLD), NEVER force an entry with reduced size.
        if calculated_decision == "HOLD" or directional_confidence < 60:
            trade_action = "HOLD"
        elif entry_quality < 60:
            # Late or poor entry point (overextended from EMA, extreme RSI/BB, poor R:R)
            if directional_confidence >= 70:
                trade_action = "WAIT_FOR_PULLBACK"
            else:
                trade_action = "HOLD"
        elif directional_confidence >= 75 and entry_quality < 70:
            trade_action = "WAIT_FOR_PULLBACK"
        elif entry_quality >= 75:
            trade_action = "ENTER"
        else: # 60 <= entry_quality < 75
            trade_action = "REDUCE_SIZE"

        return ScoreResult({
            "decision": calculated_decision,
            "directional_confidence": directional_confidence,
            "entry_quality": entry_quality,
            "conviction": conviction,
            "risk_score": risk_score,
            "risk_penalties": total_penalty,
            "trade_action": trade_action,
            "raw_net_score": net_directional_score,
            "math_conflict": math_conflict
        })
