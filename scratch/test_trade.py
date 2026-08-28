
import asyncio
import sys
import logging
from dotenv import load_dotenv
load_dotenv()
sys.path.append("python")
from services.nado_trading_service import NadoTradingService

logging.basicConfig(level=logging.INFO)

async def main():
    print("Testing NadoTradingService global fix...")
    svc = NadoTradingService()
    await svc.initialize()
    if not svc.is_connected:
        print("Init failed")
        return
        
    print("Opening test limit order (price 79000) so it WILL fill (IOC)...")
    # symbol, direction, entry_price, notional_usd, tp_price, sl_price, leverage
    res = await svc.open_position("BTC-USD", "LONG", 79000.0, 10.0, 15000.0, 9000.0, 2)
    print(f"Result: {res}")
    
if __name__ == "__main__":
    asyncio.run(main())
