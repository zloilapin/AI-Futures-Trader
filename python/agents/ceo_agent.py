import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class CEOAgent(BaseAgent):
    """
    The Chief Investment Officer (CIO / CEO) of the trading syndicate.
    
    Uses a DETERMINISTIC voting engine to compute the final decision and conviction
    from analyst reports + MTF trend alignment. The LLM is used ONLY for generating
    human-readable reasoning text — it has NO power over the final LONG/SHORT/HOLD vote.
    """
    
    # Analyst weights: Price Action & Derivatives are PRIMARY drivers,
    # Indicators & OrderBook are confirmation, News is context.
    ANALYST_WEIGHTS = {
        "Candle_Agent": 1.5,       # Price Action — primary
        "OI_Funding_Agent": 1.5,   # Derivatives — primary
        "Indicator_Agent": 1.0,    # Technical confirmation
        "Order_Book_Agent": 1.0,    # Orderflow confirmation
        "News_Agent": 0.5,         # Macro context
    }
    DEFAULT_WEIGHT = 1.0

    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("CEO_Agent", logger, llm_client)

        import os
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "ceo_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    def _compute_deterministic_decision(self, analyst_reports: list, mtf_data: dict) -> Dict[str, Any]:
        """
        Pure-math voting engine. No LLM involved.
        
        1. Tally weighted votes from analyst signals + confidence.
        2. Apply MTF trend penalty/bonus.
        3. Return deterministic decision, conviction, and breakdown.
        """
        long_score = 0.0
        short_score = 0.0
        max_possible_score = 0.0
        vote_breakdown = []

        for report in analyst_reports:
            if not isinstance(report, dict):
                continue
            signal = str(report.get("signal", "NEUTRAL")).upper()
            confidence = float(report.get("confidence", 50) or 50)
            agent_name = report.get("agent_name", "Unknown")
            
            weight = self.ANALYST_WEIGHTS.get(agent_name, self.DEFAULT_WEIGHT)
            weighted_vote = (confidence / 100.0) * weight

            if signal == "BULLISH":
                long_score += weighted_vote
            elif signal == "BEARISH":
                short_score += weighted_vote
            # NEUTRAL adds nothing

            max_possible_score += weight  # max is weight * 1.0 (100% confidence)
            vote_breakdown.append(f"{agent_name}: {signal} ({confidence}%, w={weight})")

        # Net direction
        net_score = long_score - short_score
        if net_score > 0:
            raw_direction = "LONG"
            dominant_score = long_score
        elif net_score < 0:
            raw_direction = "SHORT"
            dominant_score = short_score
        else:
            raw_direction = "HOLD"
            dominant_score = 0.0

        # Base conviction: how strong is the consensus (0-100)
        if max_possible_score > 0:
            base_conviction = (dominant_score / max_possible_score) * 100
        else:
            base_conviction = 0.0

        # --- MTF Trend Penalty / Bonus ---
        trend_1h = str(mtf_data.get("trend_1h", "NEUTRAL")).upper()
        trend_4h = str(mtf_data.get("trend_4h", "NEUTRAL")).upper()
        alignment = str(mtf_data.get("mtf_alignment", "MIXED_CHOP")).upper()

        mtf_multiplier = 1.0
        mtf_note = ""

        if raw_direction in ("LONG", "SHORT"):
            expected_trend = "BULLISH" if raw_direction == "LONG" else "BEARISH"
            opposite_trend = "BEARISH" if raw_direction == "LONG" else "BULLISH"

            if alignment == "FULL_ALIGNMENT" and trend_4h == expected_trend:
                mtf_multiplier = 1.2
                mtf_note = f"MTF FULL ALIGNMENT bonus (x1.2): 15m/1H/4H all {expected_trend}"
            elif trend_4h == opposite_trend and trend_1h == opposite_trend:
                mtf_multiplier = 0.4
                mtf_note = f"MTF COUNTER-TREND penalty (x0.4): 1H+4H both {opposite_trend}, signal is {raw_direction}"
            elif trend_4h == opposite_trend:
                mtf_multiplier = 0.5
                mtf_note = f"MTF 4H CONFLICT penalty (x0.5): 4H is {opposite_trend}, signal is {raw_direction}"
            elif trend_1h == opposite_trend:
                mtf_multiplier = 0.7
                mtf_note = f"MTF 1H CONFLICT penalty (x0.7): 1H is {opposite_trend}, signal is {raw_direction}"
            elif alignment == "MIXED_CHOP":
                mtf_multiplier = 0.8
                mtf_note = "MTF MIXED/CHOP penalty (x0.8): no clear trend alignment"

        final_conviction = min(100, max(0, base_conviction * mtf_multiplier))
        final_conviction = round(final_conviction, 1)

        # If conviction is too low after penalties, force HOLD
        if final_conviction < 20:
            final_decision = "HOLD"
        else:
            final_decision = raw_direction

        return {
            "decision": final_decision,
            "conviction": final_conviction,
            "raw_direction": raw_direction,
            "base_conviction": round(base_conviction, 1),
            "mtf_multiplier": mtf_multiplier,
            "mtf_note": mtf_note,
            "long_score": round(long_score, 2),
            "short_score": round(short_score, 2),
            "max_score": round(max_possible_score, 2),
            "vote_breakdown": vote_breakdown,
        }

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Детерминированный движок голосования + LLM для текстового резюме...")
        
        symbol = data.get("symbol", "UNKNOWN")
        analyst_reports = data.get("analyst_reports", [])
        historical_context = data.get("historical_context", [])
        mtf_data = data.get("multi_timeframe", {})
        
        # ═══ STEP 1: Deterministic voting engine ═══
        engine_result = self._compute_deterministic_decision(analyst_reports, mtf_data)
        
        det_decision = engine_result["decision"]
        det_conviction = engine_result["conviction"]
        
        breakdown_str = " | ".join(engine_result["vote_breakdown"])
        engine_summary = (
            f"[Deterministic Engine] {symbol}: "
            f"LONG={engine_result['long_score']}, SHORT={engine_result['short_score']} "
            f"(max={engine_result['max_score']}). "
            f"Base conviction={engine_result['base_conviction']}%, "
            f"MTF x{engine_result['mtf_multiplier']} → "
            f"FINAL: {det_decision} @ {det_conviction}%. "
            f"Votes: [{breakdown_str}]"
        )
        if engine_result["mtf_note"]:
            engine_summary += f" | {engine_result['mtf_note']}"
        
        self.logger.info(f"[{self.name}] {engine_summary}")
        print(f"🧮 {engine_summary}")
        
        # ═══ STEP 2: LLM generates human-readable reasoning (advisory only) ═══
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
            response_text = await self.llm_client.generate(full_prompt)
            llm_response = self._parse_json(response_text)
        except Exception as e:
            self.logger.warning(f"[{self.name}] LLM failed, using engine-only output: {e}")
        
        # ═══ STEP 3: Override LLM decision with deterministic values ═══
        llm_reasoning = llm_response.get("reasoning_en", llm_response.get("reasoning", ""))
        llm_consensus = llm_response.get("consensus_summary", "")
        llm_mtf_validation = llm_response.get("mtf_validation", "")
        
        # Prepend the engine's math to the reasoning
        final_reasoning = f"[DETERMINISTIC] {engine_summary}\n\n[LLM ADVISORY] {llm_reasoning}"
        
        return {
            "decision": det_decision,
            "conviction": det_conviction,
            "reasoning_en": final_reasoning,
            "reasoning_ru": llm_response.get("reasoning_ru", ""),
            "consensus_summary": llm_consensus,
            "mtf_validation": llm_mtf_validation,
            "engine_breakdown": engine_result,
        }
