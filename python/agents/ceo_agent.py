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

        import os
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "ceo_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

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
            "historical_trade_memory": historical_context,
            "past_lessons_learned": data.get("past_lessons_learned", [])
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nExecutive Dashboard Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
