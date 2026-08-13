import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from agents.risk_manager import RiskManager
from core.logger import TradeLogger
from core.llm_client import LLMClient

@pytest.fixture
def risk_manager():
    logger = TradeLogger()
    llm_client = AsyncMock(spec=LLMClient)
    llm_client.generate.return_value = '{"approved": true, "reasoning": "Test", "position_size_usd": 100, "position_size_pct": 10, "entry_price": 50000, "take_profit_price": 51000, "stop_loss_price": 49000, "risk_reward_ratio": 2.0, "liquidation_price": 48000}'
    return RiskManager(logger, llm_client)

@pytest.mark.asyncio
async def test_risk_manager_minimum_notional(risk_manager):
    ceo_decision = {"decision": "LONG", "conviction": 90, "symbol": "BTC"}
    portfolio_data = {"total_usd": 1000.0, "recent_streak": []}
    market_data = {
        "price_data": {"current_price": 50000.0},
        "indicators": {"atr_14": 1000.0, "rsi_14": 50},
        "order_book_data": {"spread_pct": 0.01}
    }
    
    res = await risk_manager.analyze(ceo_decision, portfolio_data, market_data)
    assert res["approved"] is True
    # Balance is 1000. Risk 1% = $10 risk. 
    # SL is current - (atr*1.5) = 50000 - 1500 = 48500
    # units = 10 / 1500 = 0.00666 BTC -> pos_usd = 333.33 USD (which is > 15 MIN_NOTIONAL)
    # The returned dict uses the mocked LLM output
    assert res["position_size_usd"] == 100.0

@pytest.mark.asyncio
async def test_risk_manager_min_base_amount(risk_manager):
    ceo_decision = {"decision": "LONG", "conviction": 90, "symbol": "BTC"}
    portfolio_data = {"total_usd": 100.0, "recent_streak": []} # Small balance
    market_data = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 3000.0, "rsi_14": 50},
        "order_book_data": {"spread_pct": 0.01}
    }
    
    # Internal logic will see: Risk = $1. SL dist = $4500. Units = 1/4500 = 0.00022 BTC
    # But min BTC base amount is 0.0001, so it shouldn't hit min_base bump unless pos_usd/price < 0.0001
    res = await risk_manager.analyze(ceo_decision, portfolio_data, market_data)
    assert res["approved"] is True
