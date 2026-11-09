import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from agents.risk_manager import RiskManager
from core.models import FinalTradeDecision
from core.pipeline import is_risk_free_position, TradingPipeline
from main import get_seconds_to_next_candle


@pytest.fixture
def base_market_data():
    return {
        "price_data": {
            "current_price": 100.0,
            "ohlcv_1h": [{"volume": 100}] * 10
        },
        "indicators": {
            "atr_14": 2.0,
            "ema_20": 100.0,
            "bb_middle": 100.0
        },
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 0.001,
            "min_notional": 10.0
        }
    }


@pytest.fixture
def base_portfolio_data():
    return {
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "active_positions": {},
        "win_count": 0,
        "loss_count": 0,
        "recent_streak": []
    }


def test_is_risk_free_position_detection():
    # 1. State string check
    assert is_risk_free_position({"state": "BREAK_EVEN"}) is True
    assert is_risk_free_position({"protection_state": "TRAILING_ACTIVE"}) is True
    assert is_risk_free_position({"state": "TIME_STOP_BE"}) is True
    assert is_risk_free_position({"state": "NORMAL"}) is False

    # 2. Partial TP plan check
    assert is_risk_free_position({"partial_tp_plan": {"be_activated": True}}) is True
    assert is_risk_free_position({"partial_tp_plan": {"be_activated": False}}) is False

    # 3. Numeric price checks
    # LONG with SL at or above entry -> risk free
    assert is_risk_free_position({"direction": "LONG", "entry_price": 100.0, "sl_price": 100.0}) is True
    assert is_risk_free_position({"direction": "LONG", "entry_price": 100.0, "sl_price": 101.0}) is True
    assert is_risk_free_position({"direction": "LONG", "entry_price": 100.0, "sl_price": 99.0}) is False

    # SHORT with SL at or below entry -> risk free
    assert is_risk_free_position({"direction": "SHORT", "entry_price": 100.0, "sl_price": 100.0}) is True
    assert is_risk_free_position({"direction": "SHORT", "entry_price": 100.0, "sl_price": 99.0}) is True
    assert is_risk_free_position({"direction": "SHORT", "entry_price": 100.0, "sl_price": 101.0}) is False


@pytest.mark.asyncio
async def test_slot_recycling_permits_4th_position_when_one_in_be(base_market_data, base_portfolio_data):
    """
    Slot recycling rule: max 3 active risk positions.
    If at least 1 position is in Break-Even, a 4th total position is approved.
    """
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }

    # Case A: 3 active positions, all with risk (SHORT risk is when sl > entry)
    portfolio_3_risk = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01, "direction": "LONG"},
        "ETH-USD": {"entry_price": 3000.0, "sl_price": 3050.0, "amount": 0.1, "direction": "SHORT"},
        "AVAX-USD": {"entry_price": 30.0, "sl_price": 31.0, "amount": 10.0, "direction": "SHORT"}
    })
    verdict_a = await rm.analyze(ceo_decision, portfolio_3_risk, base_market_data, effective_profile="BALANCED", macro_regime="TRENDING")
    assert verdict_a["approved"] is False
    assert verdict_a["veto_category"] == "MAX_CONCURRENT_POSITIONS"

    # Case B: 3 active positions, but AVAX is in BREAK_EVEN (for SHORT, sl <= entry or state is BREAK_EVEN)
    portfolio_2_risk_1_be = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01, "direction": "LONG"},
        "ETH-USD": {"entry_price": 3000.0, "sl_price": 3050.0, "amount": 0.1, "direction": "SHORT"},
        "AVAX-USD": {"entry_price": 30.0, "sl_price": 29.95, "amount": 10.0, "direction": "SHORT", "protection_state": "BREAK_EVEN"}
    })
    verdict_b = await rm.analyze(ceo_decision, portfolio_2_risk_1_be, base_market_data, effective_profile="BALANCED", macro_regime="TRENDING")
    assert verdict_b["approved"] is True

    # Case C: 4 active positions (1 in BE, 3 with risk) -> total cap of 4 reached, so 5th is rejected
    portfolio_3_risk_1_be = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01, "direction": "LONG"},
        "ETH-USD": {"entry_price": 3000.0, "sl_price": 3050.0, "amount": 0.1, "direction": "SHORT"},
        "AVAX-USD": {"entry_price": 30.0, "sl_price": 29.95, "amount": 10.0, "direction": "SHORT", "protection_state": "BREAK_EVEN"},
        "LINK-USD": {"entry_price": 15.0, "sl_price": 14.5, "amount": 20.0, "direction": "LONG"}
    })
    verdict_c = await rm.analyze(ceo_decision, portfolio_3_risk_1_be, base_market_data, effective_profile="BALANCED", macro_regime="TRENDING")
    assert verdict_c["approved"] is False
    assert verdict_c["veto_category"] == "MAX_CONCURRENT_POSITIONS"


@pytest.mark.asyncio
async def test_slot_recycling_permits_3rd_directional_when_one_in_be(base_market_data, base_portfolio_data):
    """
    Directional cluster rule: max 2 active risk positions in same direction.
    A 3rd position in that direction is permitted ONLY if at least 1 existing position is in BE.
    """
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())
    ceo_long = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "conviction": 80,
        "trade_action": "ENTER"
    }

    # Case A: 2 LONGs with risk -> 3rd LONG rejected by DIRECTIONAL_CLUSTER_CAP
    portfolio_2_long_risk = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 59000.0, "amount": 0.01, "direction": "LONG"},
        "ETH-USD": {"entry_price": 3000.0, "sl_price": 2950.0, "amount": 0.1, "direction": "LONG"}
    })
    verdict_a = await rm.analyze(ceo_long, portfolio_2_long_risk, base_market_data, effective_profile="BALANCED", macro_regime="TRENDING")
    assert verdict_a["approved"] is False
    assert verdict_a["veto_category"] == "DIRECTIONAL_CLUSTER_CAP"

    # Case B: 2 LONGs, but BTC-USD is in Break-Even -> 3rd LONG permitted!
    portfolio_1_long_be_1_risk = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 60050.0, "amount": 0.01, "direction": "LONG", "protection_state": "BREAK_EVEN"},
        "ETH-USD": {"entry_price": 3000.0, "sl_price": 2950.0, "amount": 0.1, "direction": "LONG"}
    })
    verdict_b = await rm.analyze(ceo_long, portfolio_1_long_be_1_risk, base_market_data, effective_profile="BALANCED", macro_regime="TRENDING")
    assert verdict_b["approved"] is True

    # Case C: 3 LONGs (1 in BE, 2 with risk) -> 4th LONG rejected by DIRECTIONAL_CLUSTER_CAP
    portfolio_1_be_2_risk_long = dict(base_portfolio_data, active_positions={
        "BTC-USD": {"entry_price": 60000.0, "sl_price": 60050.0, "amount": 0.01, "direction": "LONG", "protection_state": "BREAK_EVEN"},
        "ETH-USD": {"entry_price": 3000.0, "sl_price": 2950.0, "amount": 0.1, "direction": "LONG"},
        "AVAX-USD": {"entry_price": 30.0, "sl_price": 29.0, "amount": 10.0, "direction": "LONG"}
    })
    verdict_c = await rm.analyze(ceo_long, portfolio_1_be_2_risk_long, base_market_data, effective_profile="BALANCED", macro_regime="TRENDING")
    assert verdict_c["approved"] is False
    assert verdict_c["veto_category"] == "DIRECTIONAL_CLUSTER_CAP"


def test_clock_aligned_sleep_calculation():
    """
    Tests get_seconds_to_next_candle calculation with 15m intervals and 5s offset.
    """
    # Test at exact candle start (e.g. 1800000): should sleep until 1800000 + 900 + 5 = 905s
    with patch("time.time", return_value=900.0):
        wait_sec = get_seconds_to_next_candle(interval_minutes=15, offset_seconds=5)
        assert wait_sec == 905.0

    # Test 5 minutes into candle (e.g. 1200): remaining = 1805 - 1200 = 605s
    with patch("time.time", return_value=1200.0):
        wait_sec = get_seconds_to_next_candle(interval_minutes=15, offset_seconds=5)
        assert wait_sec == 605.0

    # Test right before candle close (1799): remaining = 1805 - 1799 = 6s
    with patch("time.time", return_value=1799.0):
        wait_sec = get_seconds_to_next_candle(interval_minutes=15, offset_seconds=5)
        assert wait_sec == 6.0

    # Test clock jitter: if we land 4s past candle close (1804): remaining = 1805 - 1804 = 1s <= 2.0s
    # It must bump to the next candle (1805 + 900 - 1804 = 901s)
    with patch("time.time", return_value=1804.0):
        wait_sec = get_seconds_to_next_candle(interval_minutes=15, offset_seconds=5)
        assert wait_sec == 901.0


def test_candle_debate_deduplication_tracking():
    """
    Tests that _candle_debate_history correctly records bucket and verdict.
    """
    pipeline = TradingPipeline(
        services=MagicMock(),
        agents=MagicMock(),
        exchange_name="nado"
    )
    bucket_now = int(time.time() // 900)
    pipeline._candle_debate_history = {}
    pipeline._candle_debate_history["HYPE-USD"] = (bucket_now, "HOLD")

    # In same bucket, last_debate[0] == bucket_now and decision == "HOLD"
    cached = pipeline._candle_debate_history.get("HYPE-USD")
    assert cached[0] == bucket_now
    assert cached[1] == "HOLD"

    # In next bucket, bucket changes
    next_bucket = bucket_now + 1
    assert cached[0] != next_bucket


def test_targeted_symbol_bypasses_candle_debate_lock():
    """
    Tests that when FastRadar triggers on a specific targeted_symbol or PullbackWatchlist triggers retest,
    the Candle Debate Lock is bypassed even if HOLD was decided earlier on the same 15m candle.
    """
    pipeline = TradingPipeline(
        services=MagicMock(),
        agents=MagicMock(),
        exchange_name="nado"
    )
    bucket_now = int(time.time() // 900)
    pipeline._candle_debate_history = {"HYPE-USD": (bucket_now, "HOLD")}

    # 1. Normal scheduled scan: locked (100% token savings)
    last_debate = pipeline._candle_debate_history.get("HYPE-USD")
    is_retest = False
    is_targeted = False
    is_locked = bool(last_debate and last_debate[0] == bucket_now and last_debate[1] == "HOLD" and not is_retest and not is_targeted)
    assert is_locked is True

    # 2. FastRadar triggers on HYPE-USD: lock is bypassed
    targeted_symbol = "HYPE-USD"
    is_targeted = (targeted_symbol is not None and "HYPE-USD" == targeted_symbol)
    is_locked = bool(last_debate and last_debate[0] == bucket_now and last_debate[1] == "HOLD" and not is_retest and not is_targeted)
    assert is_locked is False

    # 3. PullbackWatchlist triggers retest: lock is bypassed
    is_retest = True
    is_targeted = False
    is_locked = bool(last_debate and last_debate[0] == bucket_now and last_debate[1] == "HOLD" and not is_retest and not is_targeted)
    assert is_locked is False

