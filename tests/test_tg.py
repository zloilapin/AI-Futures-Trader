import os
import asyncio
from dotenv import load_dotenv
from services.telegram_service import TelegramService

async def test_broadcast():
    load_dotenv()
    tg = TelegramService()
    print("Testing broadcast to channel...")
    res = await tg.broadcast_to_channel("Тестовое сообщение от AI-Trader")
    print(f"Result: {res}")

if __name__ == "__main__":
    asyncio.run(test_broadcast())
