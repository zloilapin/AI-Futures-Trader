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
        
        import os
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "universe_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

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
        return await self.generate_json(full_prompt, required_keys=["selected_pairs", "reasoning"])
