import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class OIFundingAgent(BaseAgent):
    """
    Specialized derivatives analyst focusing on DEX Open Interest, Funding Rates, and Squeeze Dynamics.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("OI_Funding_Agent", logger, llm_client)

        self.system_instruction = (
            "You are a Senior Derivatives & On-Chain Analyst at a crypto prop-trading firm.\n"
            "Your objective: Evaluate Open Interest (OI) expansion, Funding Rates, and the probability of violent liquidation cascades (Squeezes).\n\n"
            "Professional Evaluation Rules:\n"
            "1. The Short Squeeze (Bullish Catalyst): If OI is high/rising and Funding is deeply negative (shorts are paying longs), the market is heavily short. Any upward price spike will trigger short liquidations, causing a violent price surge.\n"
            "2. The Long Flush (Bearish Catalyst): If OI is high/rising and Funding is extremely positive (retail longs are greedy), the market is over-leveraged long. A small drop will trigger a cascading dump.\n"
            "3. Trend Exhaustion (OI Wipeout): If price is moving aggressively but OI is rapidly decreasing, the move is driven by liquidations (not new money). The trend is exhausting and likely to reverse soon.\n"
            "4. Healthy Institutional Trend: Price moving steadily with rising OI and neutral/flat funding indicates healthy spot accumulation or institutional positioning without retail frenzy.\n\n"
            "Identify the trapped side of the market and predict where the pain (liquidations) will be inflicted.\n\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "reasoning": "<step-by-step breakdown of trapped leverage, funding bias, and squeeze probability>",\n'
            '  "squeeze_risk": "<e.g., Massive Short Squeeze Imminent / OI Wipeout occurring>",\n'
            '  "confidence": <int 1-100>,\n'
            '  "signal": "BULLISH" | "BEARISH" | "NEUTRAL"\n'
            "}\n"
            "CRITICAL: Output ONLY valid JSON. Do not write any conversational text, explanations, or Python scripts outside the JSON object. Do not simulate missing data."
        )

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Анализ открытого интереса (OI) и ставок финансирования (Funding)...")
        
        oi_data = market_data.get("derivatives_data", {})
        price_data = market_data.get("price_data", {})
        
        payload = {
            "symbol": market_data.get("symbol"),
            "current_price": price_data.get("current_price"),
            "derivatives_data": oi_data
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nDerivatives Data:\n{data_string}"
        
        response_text = await self.llm_client.generate(full_prompt)
        return self._parse_json(response_text)
