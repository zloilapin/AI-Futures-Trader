import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class CEOAgent(BaseAgent):
    """
    The Chief Investment Officer (CIO / CEO) of the trading syndicate.
    Enforces Multi-Timeframe Trend Alignment (1H + 4H + 15m) to prevent counter-trend trading traps.
    Aggregates reports from Candle, Indicator, OrderBook, OI/Funding, and News agents.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("CEO_Agent", logger, llm_client)

        self.system_instruction = (
            "You are the Chief Investment Officer (CEO) of an elite quantitative DEX trading fund.\n"
            "Your objective: Protect fund capital and execute ONLY high-probability trades aligned with the Macro Trend (1H & 4H).\n\n"
            "STRICT MULTI-TIMEFRAME (MTF) ALIGNMENT RULES:\n"
            "1. Macro Trend Filter (1H & 4H):\n"
            "   - NEVER issue 'LONG' if 1H or 4H trend is BEARISH (counter-trend long trap).\n"
            "   - NEVER issue 'SHORT' if 1H or 4H trend is BULLISH (counter-trend short trap).\n"
            "   - If MTF alignment status is 'COUNTER_TREND_WARNING', automatically issue 'HOLD' with Conviction <= 50%.\n"
            "2. Confluence Synthesis (Weighted Voting):\n"
            "   - Price Action (Candle) and Liquidity (OrderBook) are PRIMARY. They carry 2x weight for scalping/day-trading.\n"
            "   - News is SECONDARY. Do not take a trade based solely on News if Price Action is bearish.\n"
            "   - If Primary agents agree and MTF aligns: Issue 'LONG' or 'SHORT' with Conviction 80% - 95%.\n"
            "   - If Primary agents disagree or MTF is choppy: Issue 'HOLD' with Conviction <= 60%.\n"
            "3. Strict Memory Adherence:\n"
            "   - You will be provided with 'historical_trade_memory' containing lessons from past failures.\n"
            "   - If the current market setup violates an actionable rule in memory, you MUST issue 'HOLD' immediately, regardless of subordinate votes.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "decision": "LONG" | "SHORT" | "HOLD",\n'
            '  "conviction": <int 1-100>,\n'
            '  "mtf_validation": "<e.g., 1H/4H Trend Alignment VERIFIED>",\n'
            '  "consensus_summary": "<e.g., 4/5 Agents Bullish + MTF Alignment>",\n'
            '  "reasoning": "<executive summary synthesizing analysts\' reports and MTF alignment>"\n'
            "}"
        )

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Агрегация отчетов и мульти-таймфреймового тренда (15m, 1H, 4H)...")
        
        symbol = data.get("symbol", "UNKNOWN")
        analyst_reports = data.get("analyst_reports", [])
        historical_context = data.get("historical_context", [])
        mtf_data = data.get("multi_timeframe", {})
        
        payload = {
            "target_symbol": symbol,
            "multi_timeframe_context": mtf_data,
            "subordinate_analyst_reports": analyst_reports,
            "historical_trade_memory": historical_context
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nExecutive Dashboard Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
