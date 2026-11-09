import pytest
import time
from unittest.mock import MagicMock
from agents.sentinel_agent import SentinelAgent
from services.partial_tp_calculator import PartialTPCalculator


@pytest.fixture
def mock_logger():
    return MagicMock()


@pytest.mark.asyncio
async def test_sentinel_be_triggers_at_0_9r_even_if_atr_threshold_not_reached(mock_logger):
    """
    Verifies that Sentinel triggers Break-Even when floating profit reaches the hybrid BE threshold:
    max(tuned_be_r * R, 1.2%, be_atr * ATR).
    With 1R = $2.0 and tuned_be_r = 1.10, 1.0R ($2.0) gives breathing room, while reaching +1.15R triggers BE.
    """
    agent = SentinelAgent(mock_logger)
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "initial_sl": 98.0,
        "protection_state": "PROTECTED",
        "open_time": time.time() - 3600  # 1 hour ago
    }
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "be_activation_r": 1.10,
        "sentinel_fee_pct": 0.001,
        "sentinel_slippage_pct": 0.001
    }
    
    # 1. At +1.0R (102.00), profit is 2.00 < required 2.20 (1.10R) -> does NOT trigger prematurely
    market_1r = {"price_data": {"current_price": 102.00}}
    res_1r = await agent.analyze(pos, market_1r, profile_rules, atr_value=1.5)
    assert res_1r["new_sl"] is None
    assert res_1r["state"] == "PROTECTED"

    # 2. At +1.15R (102.30), profit is 2.30 >= required 2.20 (1.10R) -> triggers BE
    market_be = {"price_data": {"current_price": 102.30}}
    result = await agent.analyze(pos, market_be, profile_rules, atr_value=1.5)
    assert result["new_sl"] is not None
    assert result["state"] == "BREAK_EVEN"
    assert result["new_sl"] > 100.0  # entry + cost_buffer
    assert "BE trigger" in result["reasoning"]


@pytest.mark.asyncio
async def test_sentinel_timestop_triggers_be_after_180_minutes_with_modest_profit(mock_logger):
    """
    Verifies Time-Stop Stagnation Guard (Tier 1):
    If a trade is open >= 3 hours (180 min) with modest profit (>=0.15R),
    SL is pulled to Break-Even to prevent round-trip losses on exhausting momentum.
    """
    agent = SentinelAgent(mock_logger)
    now = time.time()
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "initial_sl": 98.0,
        "protection_state": "PROTECTED",
        "open_time": now - (190 * 60)  # 190 minutes ago (>= 180m)
    }
    # Modest profit: +0.40 price move (+0.20R, below 0.9R and below 1.2 ATR)
    market_data = {
        "price_data": {"current_price": 100.40}
    }
    profile_rules = {
        "sentinel_be_atr": 1.2,
        "be_activation_r": 0.90,
        "sentinel_fee_pct": 0.001,
        "sentinel_slippage_pct": 0.001
    }
    result = await agent.analyze(pos, market_data, profile_rules, atr_value=1.5)
    assert result["new_sl"] is not None
    assert result["state"] == "TIME_STOP_BE"
    assert result["new_sl"] > 100.0  # entry + cost_buffer
    assert "Time-Stop" in result["reasoning"]


@pytest.mark.asyncio
async def test_sentinel_timestop_tightens_sl_after_240_minutes_if_stagnant(mock_logger):
    """
    Verifies Time-Stop Risk Decay Guard (Tier 2):
    If a trade is open >= 4 hours (240 min) and stagnant in loss,
    SL distance is tightened by 50% to cut tail risk in half.
    """
    agent = SentinelAgent(mock_logger)
    now = time.time()
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "sl_price": 98.0,
        "initial_sl": 98.0,
        "protection_state": "PROTECTED",
        "open_time": now - (250 * 60)  # 250 minutes ago (>= 240m)
    }
    # Price stagnant near entry in slight loss
    market_data = {
        "price_data": {"current_price": 99.40}
    }
    profile_rules = {
        "sentinel_be_atr": 1.2,
        "sentinel_min_improve_atr": 0.10
    }
    result = await agent.analyze(pos, market_data, profile_rules, atr_value=1.0)
    assert result["new_sl"] is not None
    assert result["state"] == "TIME_STOP_TIGHTEN"
    # Initial risk dist was 2.0 (100 - 98). Tightened 50% = entry - 1.0 = 99.0.
    assert result["new_sl"] == pytest.approx(99.0)
    assert "50%" in result["reasoning"]


@pytest.mark.asyncio
async def test_sentinel_timestop_max_horizon_after_360_minutes(mock_logger):
    """
    Verifies Maximum Holding Horizon Guard (Tier 3):
    If a trade is open >= 6 hours (360 min) without reaching TP,
    SL is aggressively tightened to -0.25R (75% risk reduction) to eliminate zombie positions.
    """
    agent = SentinelAgent(mock_logger)
    now = time.time()
    pos = {
        "symbol": "ETH-USD",
        "direction": "SHORT",
        "entry_price": 2300.0,
        "sl_price": 2340.0,
        "initial_sl": 2340.0,
        "protection_state": "PROTECTED",
        "open_time": now - (380 * 60)  # 380 minutes ago (>= 360m)
    }
    # Price stagnant near entry in slight loss
    market_data = {
        "price_data": {"current_price": 2310.0}
    }
    profile_rules = {
        "sentinel_be_atr": 1.2,
        "sentinel_min_improve_atr": 0.10
    }
    result = await agent.analyze(pos, market_data, profile_rules, atr_value=20.0)
    assert result["new_sl"] is not None
    assert result["state"] == "TIME_STOP_MAX_HORIZON"
    # Initial risk dist was 40.0 (2340 - 2300). Tightened to -0.25R = entry + 10.0 = 2310.0.
    assert result["new_sl"] == pytest.approx(2310.0)
    assert "TIME_STOP_MAX_HORIZON" in result["state"]


def test_partial_tp_anchors_to_1r_when_sl_provided():
    """
    Verifies that when sl_price is provided, TP1 in 2_TIER mode is anchored to 1.0R
    rather than a fixed 50% fraction of final TP.
    """
    market_params = {
        "size_increment": 0.1,
        "min_size": 0.1,
        "price_increment": 0.01
    }
    # Case matching SOL-USD trade:
    # Entry 107.93, SL 106.79 (1R = 1.14), final TP 111.11 (delta_tp = 3.18)
    plan = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=107.93,
        tp_final_price=111.11,
        total_contracts=1.1,
        market_params=market_params,
        preferred_mode="2_TIER",
        sl_price=106.79
    )
    assert plan.mode == "2_TIER"
    assert len(plan.tiers) == 2
    t1 = plan.tiers[0]
    # In old code: target was 107.93 + (3.18 * 0.50) = 109.52.
    # In new code: target is anchored to 107.93 + 1.14 = 109.07!
    assert t1.target_price == pytest.approx(109.07)
    assert t1.action == "PROTECT_BREAKEVEN"


@pytest.mark.asyncio
async def test_sentinel_trend_following_stop_choking_prevention(mock_logger):
    """
    Verifies Stop Choking Prevention for TREND_FOLLOWING:
    At 190 min (>=180m), small profit (+0.25R) does NOT trigger BE to allow the trend room to breathe.
    At 310 min (>=300m), profit >= 0.85R (+0.90R) safely triggers BE.
    """
    agent = SentinelAgent(mock_logger)
    now = time.time()
    pos = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 50000.0,
        "sl_price": 49000.0,
        "initial_sl": 49000.0,
        "strategy_mode": "TREND_FOLLOWING",
        "protection_state": "PROTECTED",
        "open_time": now - (190 * 60)
    }
    profile_rules = {
        "sentinel_be_atr": 1.2,
        "be_activation_r": 1.10,
        "sentinel_fee_pct": 0.001,
        "sentinel_slippage_pct": 0.001
    }
    # Modest profit: +250 (+0.25R, 1R = 1000)
    market_190m = {"price_data": {"current_price": 50250.0}}
    res_190m = await agent.analyze(pos, market_190m, profile_rules, atr_value=500.0)
    assert res_190m["new_sl"] is None
    assert res_190m["state"] == "PROTECTED"

    # At 310m with >= 0.85R (+900):
    pos["open_time"] = now - (310 * 60)
    market_310m = {"price_data": {"current_price": 50900.0}}
    res_310m = await agent.analyze(pos, market_310m, profile_rules, atr_value=500.0)
    assert res_310m["new_sl"] is not None
    assert res_310m["state"] == "TIME_STOP_BE"


@pytest.mark.asyncio
async def test_sentinel_parabolic_trailing_dynamic_contraction(mock_logger):
    """
    Verifies that Parabolic Trailing contracts distance at higher R-multiples:
    At 1.0R, trail is 1.50 ATR wide.
    At 3.0R, trail dynamically contracts to ~0.88 ATR, locking in profit.
    """
    agent = SentinelAgent(mock_logger)
    pos = {
        "symbol": "ETH-USD",
        "direction": "LONG",
        "entry_price": 2000.0,
        "sl_price": 1950.0,
        "initial_sl": 1950.0,
        "strategy_mode": "TREND_FOLLOWING",
        "protection_state": "PROTECTED",
        "highest_price": 2150.0,  # 3.0R profit (1R = 50.0)
    }
    profile_rules = {
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.05
    }
    eval_atr = 25.0
    market_data = {"price_data": {"current_price": 2145.0}}
    result = await agent.analyze(pos, market_data, profile_rules, atr_value=eval_atr)
    assert result["state"] == "TRAILING"
    # current_r = (2150 - 2000) / 50 = 3.0
    # parabolic_mult = 1.0 / (1.0 + 0.35 * 2.0) = 1.0 / 1.7 = 0.5882
    # clamped_dist = max(0.45, 1.5 * 0.5882) = 0.88235 ATR = 22.059
    # trail_target = 2150 - 22.059 = 2127.94
    expected_sl = 2150.0 - (1.5 / 1.7 * eval_atr)
    assert result["new_sl"] == pytest.approx(expected_sl, rel=1e-3)
    assert "Parabolic Trailing" in result["reasoning"]
