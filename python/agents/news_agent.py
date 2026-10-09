import json
import time
from typing import Dict, Any

from agents.base_agent import BaseAgent
from core.logger import TradeLogger
from core.llm_client import LLMClient

class NewsAgent(BaseAgent):
    """
    Specialized sentiment & macro backdrop agent analyzing Crypto Fear & Greed Index and social sentiment.
    Includes caching so market-wide sentiment is only analyzed via LLM once per cycle, saving tokens.
    """
    def __init__(self, logger: TradeLogger, llm_client: LLMClient):
        super().__init__("News_Agent", logger, llm_client)

        import os
        prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "news_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_instruction = f.read()

        self._cached_result: Dict[str, Any] = {}
        self._cached_score: float = -1.0
        self._cache_time: float = 0.0

    async def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        self.logger.info(f"[{self.name}] Анализ сентимента рынка и индекса Fear & Greed...")
        
        news_data = market_data.get("news_data") or {}
        raw_score = news_data.get("sentiment_score")
        try:
            score = float(raw_score if raw_score is not None else 50.0)
        except (ValueError, TypeError):
            score = 50.0

        now = time.time()

        # Cache valid for 30 minutes if sentiment_score hasn't changed
        if self._cached_result and (now - self._cache_time < 1800) and (abs(self._cached_score - score) < 1.0):
            cached = dict(self._cached_result)
            self.logger.info(f"[{self.name}] [Cache Hit] Используем закэшированный сентимент (Score: {score}): {cached.get('signal')}")
            return cached
        
        payload = {
            "symbol": market_data.get("symbol"),
            "sentiment_data": news_data
        }
        
        data_string = json.dumps(payload, indent=2)
        full_prompt = f"{self.system_instruction}\n\nMarket Sentiment Data:\n{data_string}"
        
        result = None
        if self.llm_client:
            try:
                result = await self.generate_json(full_prompt, required_keys=["signal", "confidence", "reasoning"])
            except Exception as e:
                self.logger.warning(f"[{self.name}] LLM generation failed: {e}")

        if isinstance(result, dict) and result.get("signal") not in ("ERROR", None):
            self._cached_result = result
            self._cached_score = score
            self._cache_time = now
            return result

        # Deterministic contrarian sentiment fallback (aligned with news_prompt.txt)
        if score <= 25.0:
            fallback_signal = "BULLISH"
            fallback_conf = 75
            fallback_reason = f"Extreme Fear ({score:.0f}/100): институциональная аккумуляция, асимметрия в лонг (фолбэк)."
        elif score >= 75.0:
            fallback_signal = "BEARISH"
            fallback_conf = 75
            fallback_reason = f"Extreme Greed ({score:.0f}/100): эйфория ритейла, риск истощения и коррекции (фолбэк)."
        elif score < 40.0:
            fallback_signal = "BULLISH"
            fallback_conf = 60
            fallback_reason = f"Умеренный страх на рынке ({score:.0f}/100): слабый уклон в лонг (фолбэк)."
        elif score > 60.0:
            fallback_signal = "BEARISH"
            fallback_conf = 60
            fallback_reason = f"Умеренная жадность ({score:.0f}/100): слабый уклон в шорт (фолбэк)."
        else:
            fallback_signal = "NEUTRAL"
            fallback_conf = 50
            fallback_reason = f"Нейтральный сентимент ({score:.0f}/100): отсутствие выраженного нарратива (фолбэк)."

        fallback_result = {
            "signal": fallback_signal,
            "confidence": fallback_conf,
            "reasoning": fallback_reason
        }
        self._cached_result = fallback_result
        self._cached_score = score
        self._cache_time = now
        return fallback_result
