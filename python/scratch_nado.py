import asyncio
import aiohttp
import json

async def fetch_nado():
    base_url = "https://gateway.prod.nado.xyz/v1/query"
    async with aiohttp.ClientSession() as session:
        # 1. Fetch all products / symbols
        print("--- Fetching All Products ---")
        async with session.get(f"{base_url}?type=all_products") as resp:
            data = await resp.text()
            print(f"Status: {resp.status}")
            try:
                parsed = json.loads(data)
                print(json.dumps(parsed, indent=2)[:500] + "...")
            except:
                print(data[:500])
                
        # 2. Fetch Symbols
        print("\n--- Fetching Symbols ---")
        async with session.get(f"{base_url}?type=symbols") as resp:
            data = await resp.text()
            try:
                parsed = json.loads(data)
                print(json.dumps(parsed, indent=2)[:500] + "...")
            except:
                print(data[:500])
                
        # 3. Fetch Market Liquidity for Product 1 (usually BTC or ETH)
        print("\n--- Fetching Market Liquidity (Product 1) ---")
        async with session.get(f"{base_url}?type=market_liquidity&product_id=1") as resp:
            data = await resp.text()
            try:
                parsed = json.loads(data)
                print(json.dumps(parsed, indent=2)[:500] + "...")
            except:
                print(data[:500])

asyncio.run(fetch_nado())
