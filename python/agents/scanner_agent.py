import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class ScannerAgent(BaseAgent):
    """
    Tactical gatekeeper agent enforcing ATR Volatility Guard and spread/slippage checks.
    Halts the analysis cycle if the market is choppy, low-volatility, or has catastrophic spread.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Scanner_Agent", logger, llm_client)

    async def analyze(self, asset_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Предполётная проверка ATR волатильности и спреда (Детерминированная)...")
        
        price_data = asset_data.get("price_data", {})
        ob_data = asset_data.get("order_book_data", {})
        indicators = asset_data.get("indicators", {})
        
        current_price = float(price_data.get("current_price", 1.0) or 1.0)
        spread = float(ob_data.get("spread", 0) or 0)
        atr_14 = float(indicators.get("atr_14", 0) or 0)
        
        # CRITICAL-18: Fail-closed if ATR is missing
        if atr_14 == 0:
            return {
                "reasoning": "Данные ATR отсутствуют или равны 0. Оценка риска невозможна.",
                "status": "DATA_INVALID",
                "proceed": False
            }
            
        # MEDIUM-19: Canonical Spread Calculation
        spread_pct = float(ob_data.get("spread_pct", 0.0))
        atr_pct = round((atr_14 / current_price) * 100, 4) if current_price > 0 else 0.0
        
        # HIGH-20: Dynamic Spread Limits
        max_allowed_spread = min(0.75, max(0.15, atr_pct * 0.20))
        
        if spread_pct > max_allowed_spread:
            return {
                "reasoning": f"Спред слишком высокий: {spread_pct:.3f}% > {max_allowed_spread:.3f}%.",
                "status": "SPREAD_TOO_HIGH",
                "proceed": False
            }
            
        if atr_pct > 0 and atr_pct < 0.03:
            return {
                "reasoning": f"Волатильность слишком низкая: ATR {atr_pct}% < 0.03%.",
                "status": "LOW_VOLATILITY",
                "proceed": False
            }
            
        return {
            "reasoning": f"Рынок ликвиден (спред {spread_pct}%) и волатилен (ATR {atr_pct}%).",
            "status": "READY",
            "proceed": True
        }
