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

        # Enrich key_levels with precomputed structural and ATR levels
        raw_key_levels = dict(data.get("key_levels", {}))
        indicators_dict = data.get("indicators", {}) or {}
        curr_px = float(raw_key_levels.get("current_price") or indicators_dict.get("current_price") or 0.0)
        atr_14 = float(raw_key_levels.get("atr_14") or indicators_dict.get("atr_14") or (curr_px * 0.015))
        swing_h = float(raw_key_levels.get("swing_high_15m") or indicators_dict.get("swing_high_15m") or 0.0)
        swing_l = float(raw_key_levels.get("swing_low_15m") or indicators_dict.get("swing_low_15m") or 0.0)
        h_48h = float(raw_key_levels.get("high_48h") or indicators_dict.get("high_48h") or 0.0)
        l_48h = float(raw_key_levels.get("low_48h") or indicators_dict.get("low_48h") or 0.0)
        macro_box = float(raw_key_levels.get("macro_box_pos_pct") or indicators_dict.get("macro_box_pos_pct") or 50.0)

        key_levels = {
            **raw_key_levels,
            "current_price": curr_px,
            "atr_14": atr_14,
            "swing_high_15m": swing_h,
            "swing_low_15m": swing_l,
            "high_48h": h_48h,
            "low_48h": l_48h,
            "macro_box_pos_pct": macro_box,
            "atr_target_long": round(curr_px + 2.0 * atr_14, 4) if curr_px > 0 else 0.0,
            "atr_target_short": round(curr_px - 2.0 * atr_14, 4) if curr_px > 0 else 0.0,
            "structural_sl_long": round(swing_l - 0.25 * atr_14, 4) if swing_l > 0 else round(curr_px - 1.5 * atr_14, 4),
            "structural_sl_short": round(swing_h + 0.25 * atr_14, 4) if swing_h > 0 else round(curr_px + 1.5 * atr_14, 4),
        }

        payload = {
            "target_symbol": symbol,
            "key_levels": key_levels,
            "multi_timeframe_context": clean_mtf,
            "analyst_reports": analyst_reports
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Data:\n{data_string}"

        from core.config import config
        if getattr(config, "ANTI_BOT_MODE", False) is True:
            anti_bot_context = (
                "\n[🦈 ANTI-BOT LIQUIDITY HUNTER CONTEXT (Institutional v2.0)]:\n"
                "- System is hunting momentum/breakout bots trapped at overbought Bollinger Bands or extreme Donchian pump highs.\n"
                "- Do NOT concede defeat or score low simply because higher timeframes are in an uptrend.\n"
                "- If price is at the upper band (bb_position >= 85%), RSI is overbought (> 65), or upper rejection wicks and trapped buyers are present, "
                "defend the SHORT thesis aggressively! Highlight retail FOMO exhaustion, bull trap mechanics, liquidity sweeps, and high-probability mean reversion back to EMA-20.\n"
            )
            full_prompt = f"{full_prompt}\n{anti_bot_context}"
        
        try:
            res = await self.generate_json(full_prompt, required_keys=["thesis_score", "bearish_arguments", "summary"])
            if isinstance(res, dict) and res.get("signal") != "ERROR" and "thesis_score" in res:
                try:
                    score = float(res.get("thesis_score", 0))
                    res["thesis_score"] = max(0, min(10, int(score)))
                except (ValueError, TypeError):
                    res["thesis_score"] = 0
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
