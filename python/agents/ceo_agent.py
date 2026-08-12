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
            "You are the Chief Investment Officer (CIO) and Head Portfolio Manager of an elite crypto prop-trading firm.\n"
            "Your objective: Protect fund capital and execute ONLY high-probability 'A+' setups. You aggregate reports from your specialized institutional analysts (Price Action, Order Book, Derivatives, Macros, Quants).\n\n"
            "STRICT CIO DECISION RULES:\n"
            "1. The Sniper Approach (Capital Preservation): The market is designed to take retail money. Your default stance is 'HOLD'. You only deploy capital when multiple agents report massive confluence (e.g., A Liquidity Sweep + RSI Divergence + Massive Short Squeeze Risk).\n"
            "2. Macro Trend Override (1H & 4H): Never fight the 1H/4H trend unless the Macro Sentiment Agent reports extreme 'Blood in the Streets' capitulation. Counter-trend trading without extreme panic is forbidden.\n"
            "3. Weighting the Analysts:\n"
            "   - Price Action (Sweeps/BOS) & Derivatives (Squeezes/Funding) are your PRIMARY profit drivers. Weight them heavily.\n"
            "   - Technical Indicators and Order Book are CONFIRMATION tools.\n"
            "4. Strict Memory Adherence: If the current market setup resembles a past failure in 'historical_trade_memory', abort the trade (HOLD) immediately.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning_en": "<step-by-step CIO executive summary of the confluence (or lack thereof) across all institutional reports>",\n'
            '  "reasoning_ru": "<step-by-step CIO executive summary in STRICT Russian language (ru-RU). DO NOT use Ukrainian words>",\n'
            '  "mtf_validation": "<e.g., 1H/4H Trend Alignment VERIFIED / OVERRIDDEN DUE TO CAPITULATION>",\n'
            '  "consensus_summary": "<e.g., Perfect Storm: Liquidity Sweep + Short Squeeze + Divergence>",\n'
            '  "conviction": <int 1-100 (Only >75% for A+ setups)>,\n'
            '  "decision": "LONG" | "SHORT" | "HOLD"\n'
            "}\n"
            "CRITICAL: Output ONLY valid JSON. Do not write any conversational text, explanations, or Python scripts outside the JSON object. Do not simulate missing data."
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
