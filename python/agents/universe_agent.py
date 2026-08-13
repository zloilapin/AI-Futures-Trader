import json
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class UniverseAgent(BaseAgent):
    """
    The macro-filter agent. Scans the broader market to select the most liquid 
    and promising trading pairs, filtering out low-volume or scam tokens.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("Universe_Agent", logger, llm_client)
        
        self.system_instruction = (
            "You are a Senior Quantitative Analyst. Your job is to filter the top trending perps from the broad market data "
            "to find where the institutional money and retail crowds are clashing today on Kraken Futures.\n"
            "Select up to 4 assets that have the highest volume and most significant 24h change (both positive or negative).\n"
            "Professional Selection Rules:\n"
            "1. Liquidity is King: Never trade illiquid tokens. Prioritize assets with massive 24h volume to ensure tight spreads and zero slippage.\n"
            "2. Volatility & Momentum: Look for assets with significant price changes (huge gainers or massive losers). This means the asset is 'in play' and has a news catalyst or narrative.\n"
            "3. Avoid the Chop: Ignore assets with high volume but 0%-1% price change. They are stuck in a dead range (choppy consolidation) and will only burn our capital through spread and funding fees.\n"
            "4. Core Majors: Always include majors (BTC, ETH, SOL) if they show decent movement, as they dictate the broad market trend.\n"
            "Based on these pro-trader rules, select the top 5-7 most promising perpetual assets for the current trading session.\n"
            "Output JSON strictly matching this schema:\n"
            "{\n"
            '  "selected_pairs": ["<TICKER1>", "<TICKER2>", ...],\n'
            '  "reasoning": "<step-by-step reasoning explaining why these specific assets are in play today>"\n'
            "}\n"
            "CRITICAL: Output ONLY valid JSON. Do not write any conversational text, explanations, or Python scripts outside the JSON object. Do not simulate missing data."
        )

    async def analyze(self, broad_market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluates the broad market to select the active trading universe.
        """
        self.logger.info(f"[{self.name}] Сканирование широкого рынка для отбора активов...")
        
        # Данные по всему рынку (топ объемов, лидеры роста/падения)
        data_string = json.dumps(broad_market_data, indent=2)
        
        # Формируем финальный запрос
        full_prompt = f"{self.system_instruction}\n\nBroad Market Data:\n{data_string}"
        
        # Отправляем в LLM
        response_text = await self.llm_client.generate(full_prompt)
        
        # Ожидаем, что парсер вернет словарь со списком 'selected_pairs'
        return self._parse_json(response_text)
        
