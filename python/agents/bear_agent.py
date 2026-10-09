import os
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient
import json

class BearAgent(BaseAgent):
    """
    The Perma-Bear Agent.
    Specializes in finding SHORT opportunities and defending them.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Bear_Agent", logger, llm_client)
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "bear_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        symbol = data.get("symbol")
        analyst_reports = data.get("analyst_reports", [])
        mtf_data = data.get("multi_timeframe_context", {})

        self.logger.info(f"[{self.name}] Building SHORT thesis for {symbol}...")
        
        # Strip heavy candle arrays to save ~1,500 prompt tokens
        clean_mtf = dict(mtf_data) if isinstance(mtf_data, dict) else {}
        for tf_k in ["tf_15m", "tf_1h", "tf_4h"]:
            if tf_k in clean_mtf and isinstance(clean_mtf[tf_k], dict):
                clean_mtf[tf_k] = {k: v for k, v in clean_mtf[tf_k].items() if k != "candles_20"}

        payload = {
            "target_symbol": symbol,
            "multi_timeframe_context": clean_mtf,
            "analyst_reports": analyst_reports
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Data:\n{data_string}"
        
        try:
            res = await self.generate_json(full_prompt, required_keys=["thesis_score", "bearish_arguments", "summary"])
            if isinstance(res, dict) and res.get("signal") != "ERROR" and "thesis_score" in res:
                return res
            self.logger.warning(f"[{self.name}] LLM returned invalid thesis schema or ERROR: {res}")
        except Exception as e:
            self.logger.error(f"[{self.name}] Failed to build thesis: {e}")

        return {
            "thesis_score": 0,
            "bearish_arguments": ["Не удалось сформировать медвежий тезис из-за сбоя генерации."],
            "potential_target": 0.0,
            "invalidation_level": 0.0,
            "summary": "Fallback thesis (Bear LLM error)"
        }
