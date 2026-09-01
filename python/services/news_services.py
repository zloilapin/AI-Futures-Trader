import aiohttp
import xml.etree.ElementTree as ET
from typing import Dict, Any

class NewsService:
    """
    Сервис для сбора новостного фона из публичного RSS канала (CoinTelegraph).
    """
    def __init__(self):
        pass

    async def fetch_news_sentiment(self, symbol: str) -> Dict[str, Any]:
        """
        Асинхронно собирает новости (RSS) и возвращает последние заголовки.
        """
        url = "https://cointelegraph.com/rss"
        headlines = []
        try:
            from core.session import SessionManager
            session = await SessionManager.get()
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    root = ET.fromstring(content)
                    for item in root.findall('.//item')[:3]:
                        title = item.find('title')
                        if title is not None and title.text:
                            headlines.append(title.text)
        except Exception as e:
            print(f"⚠️ [NewsService] Ошибка парсинга RSS: {e}")
            raise Exception(f"Не удалось получить новости для {symbol}") from e
            
        if not headlines:
            headlines = ["No recent news found."]

        return {
            "symbol": symbol,
            "sentiment": "neutral",
            "headlines": headlines
        }
