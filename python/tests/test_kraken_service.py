import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from services.kraken_trading_service import KrakenTradingService

@pytest.fixture
def kraken_service():
    service = KrakenTradingService()
    service.api_key = "test"
    service.api_secret = "test"
    service.exchange = AsyncMock()
    service.exchange.apiKey = "test"
    service.exchange.markets = {"BTC/USD:USD": {}, "ETH/USD:USD": {}}
    # P0.1: Default no positions on exchange
    service.exchange.fetch_positions = AsyncMock(return_value=[])
    # P0.2: Default no open orders
    service.exchange.fetch_open_orders = AsyncMock(return_value=[])
    service.active_positions = {}
    return service

@pytest.mark.asyncio
async def test_kraken_format_symbol(kraken_service):
    # Default is BTC/USD:USD
    assert kraken_service._format_symbol("BTC") == "BTC/USD:USD"
    assert kraken_service._format_symbol("ETH") == "ETH/USD:USD"

@pytest.mark.asyncio
async def test_kraken_slippage_detection(kraken_service):
    # Mocking order execution
    mock_order = {
        "id": "123",
        "status": "closed",
        "filled": 0.002,
        "remaining": 0.0,
        "average": 50050.0, # Slippage! (planned 50000.0)
        "amount": 0.002
    }
    kraken_service._execute_market_order = AsyncMock(return_value=mock_order)
    
    # We pass entry_price = 50000.0
    await kraken_service.open_position("BTC", "LONG", 50000.0, 100.0, 51000.0, 49000.0)
    
    # Active positions should now have entry_price = 50050.0
    assert "BTC" in kraken_service.active_positions
    assert kraken_service.active_positions["BTC"]["entry_price"] == 50050.0

@pytest.mark.asyncio
async def test_virtual_mode(kraken_service):
    # Ensure virtual mode doesn't call actual exchange
    await kraken_service.open_position("ETH", "SHORT", 3000.0, 100.0, 2000.0, 4000.0, is_virtual=True)
    assert kraken_service.exchange.create_market_order.call_count == 0
    assert "ETH" in kraken_service.active_positions
    assert kraken_service.active_positions["ETH"]["is_virtual"] is True
