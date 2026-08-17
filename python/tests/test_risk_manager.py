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
    # Mocking LLM return value for Risk Manager decision
    llm_client.generate.return_value = '{"approved": true, "reasoning": "Test", "notional_size_usd": 100, "position_size_pct": 10, "entry_price": 50000, "take_profit_price": 51000, "stop_loss_price": 49000, "risk_reward_ratio": 2.0, "liquidation_price": 48000}'
    return RiskManager(logger, llm_client)

@pytest.mark.asyncio
async def test_risk_manager_minimum_notional(risk_manager):
    ceo_decision = {"decision": "LONG", "conviction": 90, "symbol": "BTC"}
    portfolio_data = {"total_usd": 1000.0, "recent_streak": []}
    market_data = {
        "price_data": {"current_price": 50000.0},
        "indicators": {"atr_14": 50000.0, "rsi_14": 50}, # Massive ATR to force tiny pos_usd
        "order_book_data": {"spread_pct": 0.01}
    }
    
    # With 60 balance, aggressive profile (5%), risk is $3. 
    # Current price 50000. SL distance is 1000. Units = 3 / 1000 = 0.003
    # notional = 0.003 * 50000 = 150
    # This is < 15? Wait, 150 > 15. The comment in test was wrong.
    # Ah, the test had total_balance = 1000.0. 
    # risk is 50. distance is 1000. contracts = 0.05. notional = 2500.
    # Wait, the test uses ATR = 50000, which makes distance_to_sl very large (75000).
    # So size will be tiny. We just assert it gets vetoed.
    res = await risk_manager.analyze(ceo_decision, portfolio_data, market_data)
    assert res["approved"] is False

@pytest.mark.asyncio
async def test_risk_manager_minimum_notional_veto(risk_manager):
    ceo_decision = {"decision": "LONG", "conviction": 90, "symbol": "BTC"}
    portfolio_data = {"total_usd": 1.0, "recent_streak": []} # Tiny balance!
    market_data = {
        "price_data": {"current_price": 50000.0},
        "indicators": {"atr_14": 50000.0, "rsi_14": 50}, 
        "order_book_data": {"spread_pct": 0.01}
    }
    
    # balance=10. risk 1% = $0.1. pos_usd will be tiny. 
    # MIN_NOTIONAL = 15. total_balance (10) * leverage (1) = 10 < 15.
    # It should VETO!
    res = await risk_manager.analyze(ceo_decision, portfolio_data, market_data)
    assert res["approved"] is False

@pytest.mark.asyncio
async def test_risk_manager_min_base_amount(risk_manager):
    ceo_decision = {"decision": "LONG", "conviction": 90, "symbol": "BTC"}
    portfolio_data = {"total_usd": 100.0, "recent_streak": []}
    market_data = {
        "price_data": {"current_price": 60000.0},
        "indicators": {"atr_14": 3000.0, "rsi_14": 50},
        "order_book_data": {"spread_pct": 0.01}
    }
    
    res = await risk_manager.analyze(ceo_decision, portfolio_data, market_data)
    assert res["approved"] is True
