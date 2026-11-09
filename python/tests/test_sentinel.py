import asyncio
import unittest
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.sentinel_agent import SentinelAgent
from core.logger import TradeLogger

class MockLogger(TradeLogger):
    def __init__(self):
        pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def debug(self, msg): pass

class TestSentinelAgent(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logger = MockLogger()
        self.agent = SentinelAgent(self.logger)
        self.profile_rules = {
            "sentinel_be_atr": 1.0,
            "be_activation_r": 1.0,
            "sentinel_trail_activation_atr": 2.5,
            "sentinel_trail_distance_atr": 1.5,
            "sentinel_min_improve_atr": 0.25
        }
        self.atr = 100.0  # ATR is 100 points

    async def test_long_no_update(self):
        pos = {
            "direction": "LONG",
            "entry_price": 10000,
            "sl_price": 9800,
            "highest_price": 10050,
            "protection_state": "PROTECTED"
        }
        market_data = {"price_data": {"current_price": 10050}}
        
        result = await self.agent.analyze(pos, market_data, self.profile_rules, self.atr)
        self.assertIsNone(result["new_sl"])
        self.assertEqual(result["state"], "PROTECTED")

    async def test_long_break_even(self):
        pos = {
            "direction": "LONG",
            "entry_price": 10000,
            "sl_price": 9800,
            "highest_price": 10200,
            "protection_state": "PROTECTED"
        }
        market_data = {"price_data": {"current_price": 10200}} # 200 pts profit (= 1.0R and > 1.2% entry)
        
        result = await self.agent.analyze(pos, market_data, self.profile_rules, self.atr)
        self.assertIsNotNone(result["new_sl"])
        # Cost buffer = (10000 * 0.001) + (10000 * 0.001) = 20
        self.assertAlmostEqual(result["new_sl"], 10020)
        self.assertEqual(result["state"], "BREAK_EVEN")

    async def test_long_trailing_stop(self):
        pos = {
            "direction": "LONG",
            "entry_price": 10000,
            "sl_price": 10020,
            "highest_price": 10300, # Max reached 3.0 ATR
            "protection_state": "BREAK_EVEN"
        }
        market_data = {"price_data": {"current_price": 10260}} # Current price above 2.5 ATR activation
        
        result = await self.agent.analyze(pos, market_data, self.profile_rules, self.atr)
        self.assertIsNotNone(result["new_sl"])
        # Target = highest - (1.5 * 100) = 10300 - 150 = 10150
        self.assertAlmostEqual(result["new_sl"], 10150)
        self.assertEqual(result["state"], "TRAILING")

    async def test_long_monotonic_invariant(self):
        # Already trailed up to 10180
        pos = {
            "direction": "LONG",
            "entry_price": 10000,
            "sl_price": 10180,
            "highest_price": 10300, 
            "protection_state": "TRAILING"
        }
        market_data = {"price_data": {"current_price": 10260}}
        
        result = await self.agent.analyze(pos, market_data, self.profile_rules, self.atr)
        # Trailing target is 10150. But current SL is 10180.
        # Should not worsen the SL. Should return None since 10180 >= 10180 and delta is 0
        self.assertIsNone(result["new_sl"])
        
    async def test_short_break_even(self):
        pos = {
            "direction": "SHORT",
            "entry_price": 10000,
            "sl_price": 10200,
            "lowest_price": 9800,
            "protection_state": "PROTECTED"
        }
        market_data = {"price_data": {"current_price": 9800}} # 200 pts profit (= 1.0R and > 1.2% entry)
        
        result = await self.agent.analyze(pos, market_data, self.profile_rules, self.atr)
        self.assertIsNotNone(result["new_sl"])
        # Cost buffer = 20
        self.assertAlmostEqual(result["new_sl"], 9980)
        self.assertEqual(result["state"], "BREAK_EVEN")

    async def test_short_trailing_stop(self):
        pos = {
            "direction": "SHORT",
            "entry_price": 10000,
            "sl_price": 9980,
            "lowest_price": 9700, # 3.0 ATR profit
            "protection_state": "BREAK_EVEN"
        }
        market_data = {"price_data": {"current_price": 9740}} # Current price above 2.5 ATR activation
        
        result = await self.agent.analyze(pos, market_data, self.profile_rules, self.atr)
        self.assertIsNotNone(result["new_sl"])
        # Target = lowest + (1.5 * 100) = 9700 + 150 = 9850
        self.assertAlmostEqual(result["new_sl"], 9850)
        self.assertEqual(result["state"], "TRAILING")

if __name__ == "__main__":
    unittest.main()
