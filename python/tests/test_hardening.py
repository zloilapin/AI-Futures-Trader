import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch


# ═══════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def kraken_service():
    """Creates a KrakenTradingService with mocked exchange."""
    with patch.dict('os.environ', {'KRAKEN_API_KEY': 'test', 'KRAKEN_API_SECRET': 'test'}):
        from services.kraken_trading_service import KrakenTradingService
        service = KrakenTradingService()
        service.exchange = AsyncMock()
        service.exchange.markets = {'BTC/USD:USD': {}}
        service.exchange.apiKey = 'test'
        # Default: fetch_positions returns no positions
        service.exchange.fetch_positions = AsyncMock(return_value=[])
        # Default: fetch_open_orders returns empty
        service.exchange.fetch_open_orders = AsyncMock(return_value=[])
        return service


@pytest.fixture
def ceo_agent():
    """Creates a CEOAgent with mocked LLM."""
    from agents.ceo_agent import CEOAgent
    from core.logger import TradeLogger
    from unittest.mock import AsyncMock
    logger = TradeLogger()
    llm_client = AsyncMock()
    llm_client.generate = AsyncMock(return_value='{"reasoning_en": "test", "decision": "LONG", "conviction": 90}')
    return CEOAgent(logger, llm_client)


# ═══════════════════════════════════════════════════════
# P0.5: Local idempotency guard
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_duplicate_guard_local(kraken_service):
    """P0.5: open_position rejects if symbol already in active_positions."""
    # Pre-populate a position
    kraken_service.active_positions["BTC"] = {
        "direction": "LONG", "entry_price": 50000, "is_virtual": False
    }
    
    result = await kraken_service.open_position(
        "BTC", "LONG", 50000.0, 100.0, 51000.0, 49000.0, leverage=10
    )
    
    assert result is False
    # Exchange should NOT have been called
    kraken_service.exchange.create_market_order.assert_not_called()


# ═══════════════════════════════════════════════════════
# P0.1: Exchange-level duplicate guard
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_duplicate_guard_exchange(kraken_service):
    """P0.1: open_position rejects if position already exists on exchange."""
    # Exchange reports an existing BTC position
    kraken_service.exchange.fetch_positions = AsyncMock(return_value=[
        {"symbol": "BTC/USD:USD", "contracts": 0.5, "info": {"size": 0.5}}
    ])
    
    result = await kraken_service.open_position(
        "BTC", "LONG", 50000.0, 100.0, 51000.0, 49000.0, leverage=10
    )
    
    assert result is False
    kraken_service.exchange.create_market_order.assert_not_called()


# ═══════════════════════════════════════════════════════
# Sync: restore orphan from exchange
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_restores_orphan(kraken_service):
    """sync_with_exchange() discovers and restores a position that exists on Kraken but not locally."""
    kraken_service.active_positions = {}  # Empty local state
    
    kraken_service.exchange.fetch_positions = AsyncMock(return_value=[
        {
            "symbol": "ETH/USD:USD",
            "contracts": 1.0,
            "side": "long",
            "entryPrice": 3000.0,
            "leverage": 5,
            "info": {"size": 1.0, "entry_price": 3000.0}
        }
    ])
    # No open orders for SL/TP
    kraken_service.exchange.fetch_open_orders = AsyncMock(return_value=[])
    
    await kraken_service.sync_with_exchange()
    
    assert "ETH" in kraken_service.active_positions
    pos = kraken_service.active_positions["ETH"]
    assert pos["direction"] == "LONG"
    assert pos["entry_price"] == 3000.0
    assert pos["restored_from_exchange"] is True


# ═══════════════════════════════════════════════════════
# Sync: remove stale local position
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_removes_stale(kraken_service):
    """sync_with_exchange() marks local positions as manually_closed if not on exchange."""
    kraken_service.active_positions = {
        "SOL": {
            "direction": "SHORT", "entry_price": 150.0, "is_virtual": False,
            "sl_price": 160, "tp_price": 140, "size_base": 1.0
        }
    }
    # Exchange has NO positions
    kraken_service.exchange.fetch_positions = AsyncMock(return_value=[])
    
    await kraken_service.sync_with_exchange()
    
    assert kraken_service.active_positions["SOL"].get("manually_closed") is True


# ═══════════════════════════════════════════════════════
# Sync: restore SL/TP from open orders
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_restores_sl_tp(kraken_service):
    """sync_with_exchange() recovers SL and TP from open conditional orders."""
    kraken_service.active_positions = {}
    
    kraken_service.exchange.fetch_positions = AsyncMock(return_value=[
        {
            "symbol": "BTC/USD:USD",
            "contracts": 0.01,
            "side": "long",
            "entryPrice": 60000.0,
            "leverage": 10,
            "info": {"size": 0.01}
        }
    ])
    kraken_service.exchange.fetch_open_orders = AsyncMock(return_value=[
        {"type": "stop-loss", "triggerPrice": 58000.0},
        {"type": "take_profit", "triggerPrice": 65000.0}
    ])
    
    await kraken_service.sync_with_exchange()
    
    assert "BTC" in kraken_service.active_positions
    assert kraken_service.active_positions["BTC"]["sl_price"] == 58000.0
    assert kraken_service.active_positions["BTC"]["tp_price"] == 65000.0


# ═══════════════════════════════════════════════════════
# CEO: Deterministic voting engine
# ═══════════════════════════════════════════════════════

def test_ceo_deterministic_long(ceo_agent):
    """CEO deterministic engine returns LONG when majority of analysts are BULLISH."""
    reports = [
        {"signal": "BULLISH", "confidence": 80, "agent_name": "Candle_Agent"},
        {"signal": "BULLISH", "confidence": 70, "agent_name": "OI_Funding_Agent"},
        {"signal": "BULLISH", "confidence": 60, "agent_name": "Indicator_Agent"},
        {"signal": "NEUTRAL", "confidence": 50, "agent_name": "Order_Book_Agent"},
        {"signal": "BEARISH", "confidence": 40, "agent_name": "News_Agent"},
    ]
    mtf = {"trend_15m": "BULLISH", "trend_1h": "BULLISH", "trend_4h": "BULLISH", "mtf_alignment": "FULL_ALIGNMENT"}
    
    result = ceo_agent._compute_deterministic_decision(reports, mtf)
    
    assert result["decision"] == "LONG"
    assert result["conviction"] > 50  # Should be boosted by MTF alignment
    assert result["mtf_multiplier"] == 1.2
    assert result["long_score"] > result["short_score"]


def test_ceo_deterministic_hold_on_neutral(ceo_agent):
    """CEO returns HOLD when analysts are split evenly."""
    reports = [
        {"signal": "BULLISH", "confidence": 70, "agent_name": "Candle_Agent"},
        {"signal": "BEARISH", "confidence": 70, "agent_name": "OI_Funding_Agent"},
        {"signal": "NEUTRAL", "confidence": 50, "agent_name": "Indicator_Agent"},
        {"signal": "NEUTRAL", "confidence": 50, "agent_name": "Order_Book_Agent"},
        {"signal": "NEUTRAL", "confidence": 50, "agent_name": "News_Agent"},
    ]
    mtf = {"trend_15m": "NEUTRAL", "trend_1h": "NEUTRAL", "trend_4h": "NEUTRAL", "mtf_alignment": "MIXED_CHOP"}
    
    result = ceo_agent._compute_deterministic_decision(reports, mtf)
    
    # Long and short scores should be equal (same weight, same confidence)
    assert result["long_score"] == result["short_score"]
    assert result["decision"] == "HOLD"


# ═══════════════════════════════════════════════════════
# CEO: MTF counter-trend penalty
# ═══════════════════════════════════════════════════════

def test_ceo_mtf_penalty(ceo_agent):
    """CEO penalizes conviction when signal opposes 4H trend."""
    reports = [
        {"signal": "BULLISH", "confidence": 90, "agent_name": "Candle_Agent"},
        {"signal": "BULLISH", "confidence": 85, "agent_name": "OI_Funding_Agent"},
        {"signal": "BULLISH", "confidence": 80, "agent_name": "Indicator_Agent"},
        {"signal": "BULLISH", "confidence": 75, "agent_name": "Order_Book_Agent"},
        {"signal": "BULLISH", "confidence": 70, "agent_name": "News_Agent"},
    ]
    
    # All analysts say LONG but 4H is BEARISH
    mtf_aligned = {"trend_15m": "BULLISH", "trend_1h": "BULLISH", "trend_4h": "BULLISH", "mtf_alignment": "FULL_ALIGNMENT"}
    mtf_counter = {"trend_15m": "BULLISH", "trend_1h": "BEARISH", "trend_4h": "BEARISH", "mtf_alignment": "COUNTER_TREND_WARNING"}
    
    result_aligned = ceo_agent._compute_deterministic_decision(reports, mtf_aligned)
    result_counter = ceo_agent._compute_deterministic_decision(reports, mtf_counter)
    
    # Counter-trend should have significantly lower conviction
    assert result_counter["conviction"] < result_aligned["conviction"]
    assert result_counter["mtf_multiplier"] == 0.4


# ═══════════════════════════════════════════════════════
# Keeper: Breakeven guard
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_breakeven_guard_long(kraken_service):
    """SL moves to breakeven when price reaches 50% of TP distance for LONG."""
    kraken_service.active_positions["BTC"] = {
        "direction": "LONG",
        "entry_price": 50000.0,
        "tp_price": 52000.0,    # TP at 52000
        "sl_price": 49000.0,    # SL at 49000
        "size_base": 0.01,
        "notional_usd": 500,
        "margin_usd": 50,
        "leverage": 10,
        "is_virtual": True,     # Virtual to avoid exchange calls
        "timestamp": time.time(),
        "breakeven_activated": False
    }
    
    # Price at 51000 = 50% of (52000 - 50000) = entry + 1000
    result = await kraken_service.check_and_update_positions("BTC", 51000.0)
    
    # SL should have moved to entry price (breakeven)
    assert kraken_service.active_positions["BTC"]["sl_price"] == 50000.0


# ═══════════════════════════════════════════════════════
# Keeper: Time-based stop
# ═══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_time_stop(kraken_service):
    """Position closes after 8 hours TTL."""
    kraken_service.active_positions["DOGE"] = {
        "direction": "LONG",
        "entry_price": 0.10,
        "tp_price": 0.12,
        "sl_price": 0.08,
        "size_base": 100.0,
        "notional_usd": 10,
        "margin_usd": 1,
        "leverage": 10,
        "is_virtual": True,
        "timestamp": time.time() - (9 * 3600),  # 9 hours ago
        "breakeven_activated": False
    }
    
    closed = await kraken_service.check_and_update_positions("DOGE", 0.10)
    
    assert len(closed) == 1
    assert closed[0]["triggered_by"] == "TIME_STOP (Виртуально)"
    assert "DOGE" not in kraken_service.active_positions
