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

        self.system_instruction = (
            "You are a strict Volatility & Liquidity Gatekeeper for a quantitative DEX trading system.\n"
            "Your objective: Evaluate immediate market volatility (ATR), orderbook spread, and liquidity.\n\n"
            "STRICT GATEKEEPER RULES:\n"
            "1. ATR Volatility Guard: If 'atr_pct' is extremely low (< 0.05%), the market is in dead consolidation. Halt trading (proceed: false, status: 'MARKET_CHOPPY').\n"
            "2. Spread Guard: If 'spread_pct' > 0.15%, slippage is too high. Halt trading (proceed: false, status: 'SPREAD_TOO_HIGH').\n"
            "3. Healthy Market: If 'atr_pct' >= 0.05% AND 'spread_pct' <= 0.15%, APPROVE DEEP ANALYSIS (proceed: true, status: 'READY'). Note: If 'atr_pct' is 0.0% but 'spread_pct' is excellent, you may approve it.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning": "<step-by-step concise breakdown of ATR volatility and spread status>",\n'
            '  "status": "READY" | "MARKET_CHOPPY" | "SPREAD_TOO_HIGH" | "LOW_LIQUIDITY",\n'
            '  "proceed": true | false\n'
            "}"
        )

    async def analyze(self, asset_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Предполётная проверка ATR волатильности и спреда...")
        
        price_data = asset_data.get("price_data", {})
        ob_data = asset_data.get("order_book_data", {})
        indicators = asset_data.get("indicators", {})
        
        current_price = float(price_data.get("current_price", 1.0) or 1.0)
        spread = float(ob_data.get("spread", 0) or 0)
        atr_14 = float(indicators.get("atr_14", 0) or 0)
        
        spread_pct = round((spread / current_price) * 100, 4) if current_price > 0 else 0.0
        atr_pct = round((atr_14 / current_price) * 100, 4) if current_price > 0 else 0.0
        
        payload = {
            "symbol": asset_data.get("symbol"),
            "current_price": current_price,
            "spread_pct": spread_pct,
            "atr_pct": atr_pct,
            "rsi_14": indicators.get("rsi_14")
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nPre-Flight Market Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
