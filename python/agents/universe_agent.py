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
            "You are a macro-level quantitative analyst focusing on market-wide screening "
            "across decentralized exchanges, specifically targeting environments like Nado DEX. "
            "Analyze the provided broad market data (volume surges, top gainers/losers, liquidity shifts). "
            "You are provided with a 'trending_perps' list containing assets, their 24h quote volume (vol24h), and 24h price change percentage (change24h). "
            "Your goal is to mathematically select the top 5-7 most promising perpetual assets for the current trading cycle based on highest volume and volatility (strong price change). "
            "Strictly avoid low-cap scam tokens and focus exclusively on assets with sufficient liquidity on Nado DEX. "
            "Output a JSON strictly matching this schema: "
            "{\"selected_pairs\": [\"BTC\", \"ETH\", \"SOL\", \"DOGE\", \"AVAX\"], \"reasoning\": \"<brief explanation>\"}"
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
        
