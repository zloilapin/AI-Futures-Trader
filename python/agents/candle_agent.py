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
            "You are a Senior Price Action & Liquidity Analyst at a crypto prop-trading firm. You trade pure price action, focusing on market psychology and liquidity.\n"
            "Your objective: Analyze 15-minute candlestick structure to identify where retail traders are trapped and where institutional money is flowing.\n\n"
            "Professional Evaluation Rules:\n"
            "1. Liquidity Sweeps (Stop Runs): Look for long wicks that sweep previous local highs/lows and immediately reject. This means retail stop-losses were hunted to fill institutional orders. Strong reversal signal.\n"
            "2. Market Structure & Displacement: Identify genuine breaks of structure (BOS). A real structural shift is accompanied by large displacement candles (strong body, little wick), not weak choppy candles.\n"
            "3. Trapped Traders (Fakeouts): Identify patterns where a breakout fails and closes back inside the range ('look above and fail' or 'look below and fail'). This indicates exhaustion and an impending aggressive move in the opposite direction.\n"
            "4. Volume & Effort: Ensure that impulsive moves are backed by volume. High volume on a doji or small candle indicates massive hidden absorption by limit orders.\n\n"
            "Do not just list basic patterns like 'bullish engulfing'. Explain the psychology (e.g., 'Swept local lows with a long wick, trapping early shorts, followed by strong upward displacement').\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning": "<step-by-step institutional analysis of sweeps, trapped traders, and displacement>",\n'
            '  "pattern_detected": "<e.g., Liquidity Sweep / Failed Breakout / Strong Displacement>",\n'
            '  "confidence": <int 1-100>,\n'
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL"\n'
            "}\n"
            "CRITICAL: Output ONLY valid JSON. Do not write any conversational text, explanations, or Python scripts outside the JSON object. Do not simulate missing data."
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
