import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class CandleAgent(BaseAgent):
    """
    Specialized agent for Price Action and Japanese Candlestick Pattern analysis on DEX perp markets.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Candle_Agent", logger, llm_client)

        self.system_instruction = (
            "You are a Senior Quantitative Price Action Analyst specializing in cryptocurrency perpetual futures on DEXs (Nado DEX / Ink L2).\n"
            "Your objective: Analyze 15-minute candlestick structure, price momentum, wick rejections, and key support/resistance levels.\n\n"
            "Evaluation Criteria:\n"
            "1. Candle Structure: Evaluate recent 15m OHLCV data for bullish/bearish engulfing patterns, hammer/pinbars, or doji indecision.\n"
            "2. Rejection & Liquidity Wicks: Look for upper/lower wicks testing key levels (liquidity sweeps or rejections).\n"
            "3. Trend Alignment: Determine if the 15m price structure is making Higher Highs / Higher Lows (Uptrend) or Lower Highs / Lower Lows (Downtrend).\n"
            "4. Volume Confirmation: Verify if candle moves are backed by expanding volume or low-volume fakeouts.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL",\n'
            '  "confidence": <int 1-100>,\n'
            '  "pattern_detected": "<e.g., Bullish Engulfing / Pinbar Rejection / Range Chop>",\n'
            '  "reasoning": "<concise institutional analysis of price action and wicks>"\n'
            "}"
        )

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Глубокий анализ прайс-экшена и свечных паттернов...")
        
        price_data = market_data.get("price_data", {})
        ohlcv = market_data.get("ohlcv_15m", [])
        
        payload = {
            "symbol": market_data.get("symbol"),
            "current_price": price_data.get("current_price"),
            "price_change_24h_pct": price_data.get("change_24h_pct"),
            "volume_24h": price_data.get("volume_24h"),
            "recent_15m_candles": ohlcv[-20:] if ohlcv else []
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nPrice Action Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
