import pytest
import asyncio
import os
import json
from unittest.mock import patch, MagicMock, AsyncMock

from core.config import config
from services.kraken_trading_service import KrakenTradingService
from agents.risk_manager import RiskManager

@pytest.fixture
def trading_service():
    service = KrakenTradingService()
    service.api_key = "fake"
    # Mock CCXT Exchange
    service.exchange = MagicMock()
    service.exchange.apiKey = "fake"
    service.exchange.secret = "fake"
    service.exchange.create_order = AsyncMock()
    service.exchange.create_market_order = AsyncMock()
    service.exchange.cancel_order = AsyncMock()
    service.active_positions.clear()
    
    # Pre-set balance mock
    service.exchange.fetch_balance = AsyncMock(return_value={
        "info": {"accounts": [{"availableMargin": 1000.0, "initialMargin": 0.0}]},
        "free": {"USD": 1000.0, "USDT": 0.0},
        "total": {"USD": 1000.0, "USDT": 0.0}
    })
    
    # Mock adjust_ledger to avoid writing to disk
    service.adjust_ledger = MagicMock()
    service._save_positions = MagicMock()
    service._save_stats = MagicMock()
    return service

@pytest.mark.asyncio
async def test_scenario_open_and_partial_fill(trading_service):
    """
    Test that a partial fill only records the filled amount in `size_base` and not the requested amount.
    """
    symbol = "BTC/USD"
    
    # Mock execute to return a partial fill
    async def mock_execute(sym, side, amount, lev=None, sl=None):
        # Requested amount was e.g. 0.1 BTC, but filled 0.05 BTC
        return {
            "status": "open",
            "filled": amount * 0.5,
            "remaining": amount * 0.5,
            "average": 50000.0,
            "id": "mock123"
        }
    
    trading_service._execute_market_order = AsyncMock(side_effect=mock_execute)
    trading_service.exchange.cancel_order = AsyncMock()
    
    # Attempt to open position of 0.1 BTC (represented by 5000 USD size)
    # signature: symbol, direction, entry_price, size_usd, tp_price, sl_price, leverage, is_virtual
    result = await trading_service.open_position(symbol, "LONG", 50000.0, 5000.0, 55000.0, 45000.0, 5, False)
    assert result is True
    
    # Verify local state recorded only 0.05 BTC (half of 5000/50000 = 0.1)
    pos = trading_service.active_positions[symbol]
    assert pos["size_base"] == 0.05
    # Ensure remaining order was cancelled
    trading_service.exchange.cancel_order.assert_called_once_with("mock123", "BTC/USD:USD")

@pytest.mark.asyncio
async def test_scenario_tp_and_sl(trading_service):
    """
    Test Keeper correctly identifies TP and SL hits and sends close order.
    """
    symbol = "ETH/USD"
    trading_service.active_positions[symbol] = {
        "id": "pos1",
        "symbol": symbol,
        "direction": "LONG",
        "entry_price": 2000.0,
        "size_usd": 2000.0,
        "leverage": 5,
        "size_base": 1.0,
        "tp_price": 2200.0,
        "sl_price": 1800.0,
        "is_virtual": False
    }
    
    # Mock execute to close successfully
    trading_service._execute_market_order = AsyncMock(return_value={"status": "closed", "average": 1800.0})
    
    # Provide market data that hits SL
    reports = await trading_service.check_and_update_positions(symbol, 1750.0)
    
    assert len(reports) == 1
    assert reports[0]["pnl_usd"] == -200.0 # (1800 - 2000) * 1.0
    assert reports[0]["triggered_by"] == "SL"
    # Position should be removed
    assert symbol not in trading_service.active_positions

@pytest.mark.asyncio
async def test_scenario_trailing_stop(trading_service):
    """
    Test that when profit exceeds 1.5%, the trailing stop activates and updates the exchange SL.
    """
    symbol = "SOL/USD"
    trading_service.active_positions[symbol] = {
        "id": "pos1",
        "symbol": symbol,
        "direction": "LONG",
        "entry_price": 100.0,
        "size_usd": 1000.0,
        "leverage": 5,
        "size_base": 10.0,
        "tp_price": 120.0,
        "sl_price": 90.0,
        "is_virtual": False,
        "highest_price": 100.0,
        "lowest_price": 100.0
    }
    
    trading_service._update_exchange_sl = AsyncMock()
    
    # Provide price that is +2% (102.0)
    # This should trigger trailing stop logic: trail_sl = 102.0 * (1 - 0.015) = 100.47
    await trading_service.check_and_update_positions(symbol, 102.0)
    
    pos = trading_service.active_positions[symbol]
    assert pos["sl_price"] == 100.47
    trading_service._update_exchange_sl.assert_called_once_with(symbol, "LONG", 100.47, 10.0)

@pytest.mark.asyncio
async def test_scenario_manual_closure(trading_service):
    """
    Test that if manually_closed is set, the position is silently removed without PnL logic.
    """
    symbol = "DOGE/USD"
    trading_service.active_positions[symbol] = {
        "id": "pos1",
        "symbol": symbol,
        "direction": "LONG",
        "entry_price": 0.1,
        "size_base": 10000.0,
        "size_usd": 1000.0,
        "tp_price": 0.12,
        "sl_price": 0.08,
        "manually_closed": True, # Simulate sync_with_exchange marking it closed
        "is_virtual": False
    }
    
    reports = await trading_service.check_and_update_positions(symbol, 0.11)
    assert len(reports) == 1
    assert reports[0]["triggered_by"] == "Ручное закрытие на бирже"
    assert symbol not in trading_service.active_positions

@pytest.mark.asyncio
async def test_scenario_api_loss(trading_service):
    """
    Test robust error handling when the exchange API fails on order creation.
    """
    symbol = "ADA/USD"
    
    # Mock execute to raise Exception (simulating CCXT NetworkError)
    trading_service._execute_market_order = AsyncMock(side_effect=Exception("ccxt.NetworkError"))
    
    # Using open_position directly. The caller will get an exception or open_position will return False based on logic.
    # Looking at open_position, it prints error but actually we want to see if it handles it.
    # _execute_market_order does not catch exceptions unless wrapped, wait, open_position wraps it?
    # Actually _execute_market_order wraps the CCXT call and returns None if it fails.
    # Let's mock _execute_market_order to return None (which is what it does on error).
    trading_service._execute_market_order = AsyncMock(return_value=None)
    
    result = await trading_service.open_position(symbol, "LONG", 500.0, 0.5, 5, 0.45, 0.55, False)
    assert result is False
    assert symbol not in trading_service.active_positions

@pytest.mark.asyncio
async def test_scenario_bot_restart():
    """
    Test state recovery from JSON file.
    """
    state_file = "data/active_positions.json"
    os.makedirs("data", exist_ok=True)
    with open(state_file, "w") as f:
        json.dump({
            "BTC/USD": {
                "id": "pos123",
                "symbol": "BTC/USD",
                "direction": "SHORT",
                "entry_price": 60000.0,
                "size_base": 0.1,
                "tp_price": 55000.0,
                "sl_price": 61000.0
            }
        }, f)
        
    service = KrakenTradingService()
    assert "BTC/USD" in service.active_positions
    assert service.active_positions["BTC/USD"]["size_base"] == 0.1
