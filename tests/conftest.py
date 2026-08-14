import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock

# Add the python directory to the path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../python')))

@pytest.fixture
def mock_llm_client():
    from core.llm_client import LLMClient
    mock = AsyncMock(spec=LLMClient)
    # Default to Long
    mock.generate.return_value = '{"decision": "LONG", "confidence": 95, "stop_loss": 90, "take_profit": 110, "reasoning": "Test"}'
    return mock

@pytest.fixture
def mock_kraken_service():
    from services.kraken_trading_service import KrakenTradingService
    mock = AsyncMock(spec=KrakenTradingService)
    mock.get_balance.return_value = {"total_usd": 1000.0, "free_margin": 1000.0, "used_margin": 0.0}
    
    # Simple stateful fake for testing positions
    fake_positions = []
    
    async def fake_fetch_positions():
        return fake_positions
        
    async def fake_open_position(symbol, side, size_usd, leverage):
        # By default we simulate filling 100%
        entry = 100.0
        size_base = size_usd / entry
        pos = {
            "symbol": symbol,
            "side": side,
            "size": size_base,
            "entryPrice": entry,
            "markPrice": entry,
            "unrealizedPnl": 0.0,
            "leverage": leverage
        }
        fake_positions.append(pos)
        return {"status": "closed", "filled": size_base, "remaining": 0.0, "average": entry, "fee": {"cost": 0.1, "currency": "USD"}}
        
    async def fake_close_position(symbol, size_to_close, side_to_close):
        fake_positions.clear()
        return {"status": "closed", "filled": size_to_close, "remaining": 0.0, "average": 100.0, "fee": {"cost": 0.1, "currency": "USD"}}
        
    mock.fetch_positions.side_effect = fake_fetch_positions
    mock.open_position.side_effect = fake_open_position
    mock.close_position.side_effect = fake_close_position
    return mock
