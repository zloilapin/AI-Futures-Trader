import asyncio
import os
from dotenv import load_dotenv
import ccxt.async_support as ccxt
import json

async def main():
    load_dotenv('C:/Users/Admin/Downloads/AI-Futures-Trader-main/.env')
    api_key = os.getenv('KRAKEN_API_KEY')
    api_secret = os.getenv('KRAKEN_API_SECRET')
    if not api_key:
        print("NO API KEY")
        return
        
    exchange = ccxt.krakenfutures({
        'apiKey': api_key,
        'secret': api_secret
    })
    
    try:
        trades = await exchange.fetch_my_trades(limit=20)
        for t in trades:
            print(f"Trade: {t['datetime']} | {t['symbol']} | {t['side']} | {t['price']} | {t['amount']} | realized_pnl: {t.get('info', {}).get('realizedPnl')}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await exchange.close()

asyncio.run(main())
