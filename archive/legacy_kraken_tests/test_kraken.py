import asyncio
import os
from dotenv import load_dotenv
from python.services.kraken_trading_service import KrakenTradingService

async def main():
    print("=== ТЕСТ ПОДКЛЮЧЕНИЯ К KRAKEN FUTURES ===")
    # Загружаем ключи из .env
    load_dotenv()
    
    # Инициализируем сервис
    trading_service = KrakenTradingService()
    
    print("\nЗапрос баланса с биржи...")
    # Запрашиваем баланс
    portfolio = await trading_service.get_portfolio_summary()
    
    print("\n=== РЕЗУЛЬТАТ ===")
    print(f"Доступный баланс (USD): ${portfolio.get('total_usd', 0):,.2f}")
    
    # Закрываем сессию
    await trading_service._close_exchange_async()
    print("Соединение успешно закрыто.")

if __name__ == "__main__":
    asyncio.run(main())
