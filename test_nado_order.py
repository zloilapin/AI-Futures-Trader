import asyncio
import os
import time
from core.config import config
from services.nado_trading_service import NadoTradingService

async def main():
    print("=== TESTING NADO TRADING SERVICE ===")
    service = NadoTradingService()
    
    if not service.is_connected:
        print("❌ Failed to connect to Nado SDK")
        return
        
    print(f"✅ Connected. Wallet address: {service.wallet.get_address()}")
    
    # Check active positions
    positions = await service.get_active_positions()
    print(f"📊 Active positions: {len(positions)}")
    
    # Try to simulate an order for a known token (e.g. SOL-USD or ETH-USD)
    symbol = "SOL-USD"
    direction = "LONG"
    entry_price = 150.0  # mock price
    notional_usd = 20.0  # small mock size
    
    print(f"\n🚀 Attempting to simulate {direction} on {symbol} for ${notional_usd}...")
    
    # We will call open_position directly. Since this actually places an order,
    # we should be careful. We can use a very small amount, or just let it fail/succeed.
    # Alternatively, we can just print the payload it WOULD send.
    
    # Let's call open_position and see what exception it throws (if any)
    try:
        res = await service.open_position(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            notional_usd=notional_usd,
            tp_price=160.0,
            sl_price=140.0,
            leverage=2
        )
        print(f"Result: {res}")
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
