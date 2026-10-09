import os
import json
from typing import Dict, Any, List

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class ReflectorAgent(BaseAgent):
    """
    Self-Reflection / Post-Trade Autopsy Agent.
    Analyzes closed trades (especially Stop Loss losses) to extract lessons learned 
    and store negative pattern warnings in data/memory/lessons.json.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient, lessons_file: str = "data/memory/lessons.json"):
        super().__init__("Reflector_Agent", logger, llm_client)
        self.lessons_file = lessons_file
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "reflector_prompt.txt")
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                self.system_instruction = f.read()
        except Exception as e:
            self.logger.warning(f"[{self.name}] Could not load reflector prompt file ({e}). Using default instruction.")
            self.system_instruction = (
                "You are a Senior Trading Post-Mortem Specialist. Output JSON with symbol, "
                "reasoning, root_cause, actionable_rule, and trade_outcome (WIN/LOSS)."
            )

    @staticmethod
    def _normalize_symbol(sym: Any) -> str:
        return str(sym or "").replace('/', '-').split('-')[0].upper()

    @staticmethod
    def _clean_context_for_llm(context: Dict[str, Any]) -> Dict[str, Any]:
        """Prunes raw candle arrays and order book depth to save prompt tokens."""
        if not isinstance(context, dict):
            return {}
        clean = {}
        for k, v in context.items():
            if k in ["ohlcv", "candles", "raw_candles"]:
                continue
            if k == "order_book" and isinstance(v, dict):
                clean["order_book"] = {
                    "best_bid": v.get("best_bid"),
                    "best_ask": v.get("best_ask"),
                    "spread_pct": v.get("spread_pct"),
                    "imbalance": v.get("imbalance")
                }
            elif k == "multi_timeframe" and isinstance(v, dict):
                clean["multi_timeframe"] = {
                    "trend_15m": v.get("trend_15m", "NEUTRAL"),
                    "trend_1h": v.get("trend_1h", "NEUTRAL"),
                    "trend_4h": v.get("trend_4h", "NEUTRAL"),
                    "mtf_alignment": v.get("mtf_alignment", "MIXED_CHOP")
                }
            else:
                clean[k] = v
        return clean

    def get_lessons(self, limit: int = 5, symbol: str = None) -> List[str]:
        """Returns the most recent actionable rules/lessons learned, prioritizing LOSSes and specific symbols."""
        try:
            from core.state_store import StateStore
            data = StateStore.load(self.lessons_file, default=[])
            if not isinstance(data, list):
                return []
                
            # Filter only for losses (we want to learn from mistakes, not successes)
            loss_data = [
                item for item in data 
                if isinstance(item, dict) 
                and str(item.get("trade_outcome", "")).upper() == "LOSS" 
                and item.get("actionable_rule") 
                and str(item.get("actionable_rule")).strip()
            ]
            
            lessons: List[str] = []
            target_norm = self._normalize_symbol(symbol) if symbol else ""

            # 1. First, get lessons specific to this symbol
            if target_norm:
                for item in reversed(loss_data):
                    item_norm = self._normalize_symbol(item.get("symbol"))
                    if item_norm == target_norm:
                        rule = str(item["actionable_rule"]).strip()
                        if rule not in lessons:
                            lessons.append(rule)
                        if len(lessons) >= limit:
                            break
            
            # 2. If we still have room, pad with generic recent losses
            if len(lessons) < limit:
                for item in reversed(loss_data):
                    item_norm = self._normalize_symbol(item.get("symbol"))
                    if not target_norm or item_norm != target_norm:
                        rule = str(item["actionable_rule"]).strip()
                        if rule not in lessons:
                            lessons.append(rule)
                        if len(lessons) >= limit:
                            break
            
            return lessons
        except Exception as e:
            self.logger.error(f"[{self.name}] Error reading lessons: {e}")
            return []

    def _save_lesson(self, reflection: Dict[str, Any]):
        if not isinstance(reflection, dict) or not reflection.get("actionable_rule"):
            return
        from core.state_store import StateStore
        data = StateStore.load(self.lessons_file, default=[])
        if not isinstance(data, list):
            data = []
        data.append(reflection)
        
        # Enforce memory rotation limit
        if len(data) > 100:
            data = data[-100:]
            
        StateStore.save(self.lessons_file, data)

    async def reflect(self, closed_trade: Dict[str, Any], market_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Runs LLM reflection on a closed trade and saves actionable rule to memory.
        """
        if not isinstance(closed_trade, dict):
            return {}

        symbol = closed_trade.get("symbol", "UNKNOWN")
        self.logger.info(f"[{self.name}] Проведение пост-мортем анализа сделки по {symbol}...")
        
        cleaned_context = self._clean_context_for_llm(market_context)
        payload = {
            "closed_trade": closed_trade,
            "market_context_at_close": cleaned_context
        }
        
        data_string = json.dumps(payload, indent=2, default=str)
        full_prompt = f"{self.system_instruction}\n\nClosed Trade Data:\n{data_string}"
        
        reflection = await self.generate_json(
            full_prompt, 
            required_keys=["symbol", "reasoning", "root_cause", "actionable_rule", "trade_outcome"]
        )
        
        if reflection and isinstance(reflection, dict) and reflection.get("actionable_rule"):
            if not reflection.get("symbol"):
                reflection["symbol"] = symbol
            if not reflection.get("trade_outcome"):
                pnl = float(closed_trade.get("pnl_usd", 0.0) or 0.0)
                reflection["trade_outcome"] = "WIN" if pnl > 0.001 else "LOSS"
            self._save_lesson(reflection)
            print(f"🧠 [ReflectorAgent] Урок извлечен и сохранен в память: {reflection.get('actionable_rule')}")

        return reflection
