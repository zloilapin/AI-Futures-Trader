import asyncio
import math
import time
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from agents.risk_manager import RiskManager
from agents.sentinel_agent import SentinelAgent
from services.nado_trading_service import NadoTradingService
from services.partial_tp_calculator import PartialTPCalculator
from core.bayesian_optimizer import TradingBayesianOptimizer


# ─── 1. Distance-to-Structure Gate Tests (RiskManager) ────────────────────────

@pytest.fixture
def mock_logger():
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


@pytest.mark.asyncio
async def test_distance_to_structure_gate_checks_structural_sl_not_atr(mock_logger):
    """
    Test that Distance-to-Structure Gate measures distance to structural_sl, NOT atr_based_sl.
    With ATR = 1.2%, 1.75 * ATR = 2.10% > 1.95%, but swing_high is only 0.60% away (+ 0.30% ATR buffer = 0.90%).
    This trade must pass and NOT trigger DISTANCE_TO_STRUCTURE_VETO!
    """
    rm = RiskManager(logger=mock_logger, llm_client=MagicMock())

    current_price = 100.0
    atr_14 = 1.20 # 1.2% ATR
    swing_high_15m = 100.60 # 0.60% away

    market_data = {
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": atr_14,
            "swing_high_15m": swing_high_15m,
            "swing_low_15m": 98.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {
            "size_increment": 0.1,
            "price_increment": 0.01,
            "min_size": 0.1,
            "min_notional": 100.0,
            "max_leverage": 20.0
        }
    }

    portfolio_data = {
        "total_usd": 2000.0,
        "available_margin": 1500.0,
        "daily_drawdown_pct": 0.01,
        "active_positions": {}
    }

    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85.0,
        "directional_confidence": 85.0,
        "entry_quality": 80.0
    }

    res = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="AGGRESSIVE", # 1.75x ATR SL
        macro_regime="TRENDING_BEAR"
    )

    # Must NOT veto on distance to structure!
    assert res.get("approved") is True
    assert res.get("veto_category") is None
    # Verify sl_price stays within max_sl_dist_allowed (Tier 2 SOL ceiling is 2.35% => <= 102.35)
    assert res["sl_price"] <= 102.35
    assert res["sl_price"] >= swing_high_15m


@pytest.mark.asyncio
async def test_distance_to_structure_gate_vetoes_when_structure_itself_is_too_far(mock_logger):
    """
    If structural_sl itself is beyond 1.95% (e.g. 2.125% away), it MUST trigger DISTANCE_TO_STRUCTURE_VETO.
    """
    rm = RiskManager(logger=mock_logger, llm_client=MagicMock())

    current_price = 100.0
    atr_14 = 0.50
    # swing_high at 102.00 + 0.25*0.50 = 102.125 => 2.125% > 1.95%
    swing_high_15m = 102.00

    market_data = {
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": atr_14,
            "swing_high_15m": swing_high_15m,
            "swing_low_15m": 98.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {
            "size_increment": 0.1,
            "price_increment": 0.01,
            "min_size": 0.1,
            "min_notional": 100.0,
            "max_leverage": 20.0
        }
    }

    portfolio_data = {
        "total_usd": 2000.0,
        "available_margin": 1500.0,
        "daily_drawdown_pct": 0.01,
        "active_positions": {}
    }

    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85.0,
        "directional_confidence": 85.0,
        "entry_quality": 80.0
    }

    res = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="AGGRESSIVE",
        macro_regime="TRENDING_BEAR"
    )

    assert res.get("approved") is False
    assert res.get("veto_category") == "DISTANCE_TO_STRUCTURE_VETO"
    assert res.get("trade_action") == "WAIT_FOR_PULLBACK"


@pytest.mark.asyncio
async def test_distance_to_structure_gate_retest_priority_corridor(mock_logger):
    """
    On a confirmed RETEST (via is_pullback_retest or 'RETEST' in reasoning),
    allow up to 2.50% structural stop distance.
    """
    rm = RiskManager(logger=mock_logger, llm_client=MagicMock())

    current_price = 100.0
    atr_14 = 0.50
    # swing_high at 101.95 + 0.125 buffer = 102.075 => 2.075% (would fail 1.95%, but passes 2.50% for retest)
    swing_high_15m = 101.95

    market_data = {
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": atr_14,
            "swing_high_15m": swing_high_15m,
            "swing_low_15m": 98.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {
            "size_increment": 0.1,
            "price_increment": 0.01,
            "min_size": 0.1,
            "min_notional": 100.0,
            "max_leverage": 20.0
        }
    }

    portfolio_data = {
        "total_usd": 2000.0,
        "available_margin": 1500.0,
        "daily_drawdown_pct": 0.01,
        "active_positions": {}
    }

    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85.0,
        "directional_confidence": 85.0,
        "entry_quality": 80.0,
        "reasoning_en": "RETEST of EMA-20 dynamic resistance confirmed"
    }

    res = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="AGGRESSIVE",
        macro_regime="TRENDING_BEAR"
    )

    assert res.get("approved") is True
    assert res.get("veto_category") is None


# ─── 2. Post-Only Maker Price Calculation & Error 2008 Interception ───────────

@pytest.mark.asyncio
async def test_use_maker_entry_price_calculation_and_no_2008_error():
    service = NadoTradingService()
    service.is_connected = True
    service.product_map = {"SOL": 2}
    service._market_cache = {}
    service.client = MagicMock()
    service.wallet = MagicMock()
    service.wallet.get_address.return_value = "0x1111111111111111111111111111111111111111"
    service.default_subaccount_id = "0x1111111111111111111111111111111111111111000000000000000000000000"
    service._get_market_parameters = MagicMock(return_value={
        "size_increment_x18": int(0.01 * 10**18),
        "price_increment_x18": int(0.01 * 10**18),
        "min_size": 0.01,
        "min_notional": 100.0
    })

    # Mock market price return: ask 100.05, bid 100.00
    mock_price_data = MagicMock()
    mock_price_data.bid_x18 = int(100.00 * 10**18)
    mock_price_data.ask_x18 = int(100.05 * 10**18)
    service.client.market.get_latest_market_price.return_value = mock_price_data

    # Mock first place_order failing with 2008, second place_order (IOC) succeeding
    first_call = True
    def mock_place_order(params):
        nonlocal first_call
        if first_call:
            first_call = False
            raise Exception("Order would cross the book (code 2008)")
        mock_res = MagicMock()
        mock_res.data = MagicMock(digest="0xabc123")
        return mock_res

    service.client.market.place_order.side_effect = mock_place_order

    mock_order = MagicMock()
    mock_order.base_filled = str(int(1.05 * 10**18))
    mock_order.quote_filled = str(int(105.0 * 10**18))
    mock_order.fee = 0
    mock_order.submission_idx = 1
    service.client.market.get_historical_orders_by_digest.return_value = MagicMock(orders=[mock_order])

    with patch.object(service, "_save_state", MagicMock()):
        # Should execute and successfully recover via IOC fallback without unhandled exception
        success = await service.open_position(
            symbol="SOL-USD",
            direction="LONG",
            entry_price=100.00,
            notional_usd=105.0,
            tp_price=103.00,
            sl_price=98.50,
            leverage=5,
            limit_price=100.20, # Taker limit passed by RiskManager
            strategy_mode="TREND_FOLLOWING", # Triggers use_maker_entry = True
            use_software_tpsl=True,
            approved_risk_usd=1.80
        )

        assert success is True
        assert service.client.market.place_order.call_count == 2


# ─── 3. Approved Risk USD Synchronization on $101 Bump ────────────────────────

@pytest.mark.asyncio
async def test_stage7_preserves_notional_at_104_without_unnecessary_shrink(mock_logger):
    """
    If notional is $110 and target_margin_usd would shrink it to $60,
    Stage 7 must preserve notional at >= $104 when it fits in usable_margin.
    """
    rm = RiskManager(logger=mock_logger, llm_client=MagicMock())

    current_price = 100.0
    market_data = {
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": 1.0,
            "swing_high_15m": 101.20,
            "swing_low_15m": 98.80,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {
            "size_increment": 0.01,
            "price_increment": 0.01,
            "min_size": 0.01,
            "min_notional": 100.0,
            "max_leverage": 10.0
        }
    }

    portfolio_data = {
        "total_usd": 400.0,
        "available_margin": 300.0,
        "daily_drawdown_pct": 0.01,
        "active_positions": {}
    }

    ceo_decision = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "trade_action": "ENTER",
        "conviction": 85.0,
        "directional_confidence": 85.0,
        "entry_quality": 80.0
    }

    res = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING_BULL"
    )

    assert res.get("approved") is True
    assert res.get("notional_usd") >= 101.0
    assert res.get("risk_amount_usd") > 0


# ─── 4. FastPriceMonitor SL Safety Buffer & Freshness ─────────────────────────

@pytest.mark.asyncio
async def test_fast_price_monitor_sl_safety_buffer_for_native_sl():
    """
    Tests that FastPriceMonitor applies 0.20% buffer for real positions with sl_digest,
    and ignores stale oracle prices older than 15s.
    """
    service = NadoTradingService()
    service.is_connected = True
    service.product_map = {"SOL": 2}

    # Stale cache (age 30s)
    service._market_cache = {
        2: MagicMock(oracle_price_x18=int(98.0 * 10**18)) # Oracle indicates 98.0 (below SL)
    }
    service._market_cache_ts = time.time() - 30.0 # 30 seconds old!

    # Active position with native SL at 98.0
    service.active_positions = {
        "SOL-USD": {
            "product_id": 2,
            "direction": "LONG",
            "entry_price": 100.0,
            "sl_price": 98.0,
            "tp_price": 104.0,
            "sl_digest": "0xnative_sl_confirmed",
            "is_virtual": False,
            "is_shadow": False
        }
    }

    # Live market price from SDK is fresh: 97.90 (touches SL at 98.0, but within 0.20% buffer: 98.0 * 0.998 = 97.804)
    # 97.90 > 97.804 => backup SL must NOT trigger yet!
    mock_price_data = MagicMock()
    mock_price_data.price_x18 = int(97.90 * 10**18)
    service.client = MagicMock()
    service.client.market.get_latest_market_price.return_value = mock_price_data
    service.close_position = AsyncMock()

    # Run monitor for 1 iteration
    async def run_one_cycle():
        task = asyncio.create_task(service.start_fast_price_monitor())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await run_one_cycle()

    # close_position should NOT have been called because 97.90 > 98.0 * (1 - 0.0020) = 97.804
    assert service.close_position.call_count == 0


# ─── 5. Bayesian Optimizer Small Position Protect Simulation ──────────────────

def test_bayesian_optimizer_small_pos_protect_simulation():
    optimizer = TradingBayesianOptimizer()

    params = {
        "chop_range_threshold": 65.0,
        "anti_chop_bb_max": 75.0,
        "min_reduce_size_conv": 60.0,
        "be_activation_r": 0.80
    }

    # Trade 1: notional $104 (< $200), hit BE (mfe_r = 1.0 > 0.80), then stopped out (raw_r = -0.50)
    # Under small_pos_protect, 100% exited at BE (+0.05R) rather than 50% partial
    trades = [
        {
            "notional_usd": 104.0,
            "macro_range_score": 40.0,
            "bb_pos_pct": 50.0,
            "entry_quality": 75.0,
            "direction": "LONG",
            "realized_r": -0.50,
            "max_favorable_excursion_r": 1.0,
            "cohort": "A"
        },
        {
            "notional_usd": 104.0,
            "macro_range_score": 40.0,
            "bb_pos_pct": 50.0,
            "entry_quality": 75.0,
            "direction": "LONG",
            "realized_r": 1.50,
            "max_favorable_excursion_r": 1.8,
            "cohort": "A"
        }
    ]

    res = optimizer.simulate_trades(trades, params)
    assert res["retained_count"] == 2
    # Trade 1 adj_r = 0.05, Trade 2 adj_r = 1.50 => total net_r = 1.55
    assert math.isclose(res["net_r"], 1.55, abs_tol=1e-4)


def test_bayesian_optimizer_objective_be_activation_r_range():
    optimizer = TradingBayesianOptimizer()
    optimizer.trades = []

    trial = MagicMock()
    trial.suggest_float.side_effect = lambda name, low, high, step=None: low

    optimizer.objective(trial)

    # Verify suggest_float was called for be_activation_r with low=0.70, high=0.90
    calls = trial.suggest_float.call_args_list
    be_call = next((c for c in calls if c[0][0] == "be_activation_r"), None)
    assert be_call is not None
    assert be_call[0][1] == 0.70
    assert be_call[0][2] == 0.90


# ─── 6. SentinelAgent Trailing & Time-Stop Fixes ─────────────────────────────

@pytest.mark.asyncio
async def test_sentinel_agent_no_premature_trailing_during_free_ride(mock_logger):
    """
    Verifies that when be_activated=True, be_moved=True, or is_runner=True,
    SentinelAgent DOES NOT activate TRAILING between 0.75R and 1.34R (Free Ride phase).
    It only activates TRAILING once effective_r >= 1.35R.
    """
    sentinel = SentinelAgent(logger=mock_logger)

    # Long position: entry = 100.0, initial_sl = 98.0 (1R = 2.0)
    # Current price = 101.70 => profit = 1.70 => 0.85R (within 0.75R - 1.35R corridor)
    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "highest_price": 101.70,
        "initial_sl": 98.0,
        "sl_price": 100.35, # Already moved to BE (+ 0.35% cost buffer)
        "planned_risk_usd": 2.0,
        "contracts": 1.0,
        "be_activated": True,
        "be_moved": True,
        "partial_tp_taken": True,
        "is_runner": True,
        "protection_state": "BREAK_EVEN",
        "strategy_mode": "TREND_FOLLOWING",
        "open_time": time.time() - 600.0 # 10 mins ago
    }

    market_data = {
        "price_data": {"current_price": 101.70},
        "atr_14": 1.0,
        "indicators": {"atr_14": 1.0}
    }
    profile_rules = {"be_activation_r": 0.75}

    # At 0.85R, trailing must NOT be activated!
    res = await sentinel.analyze(pos, market_data, profile_rules=profile_rules, atr_value=1.0)
    assert res["state"] != "TRAILING"
    assert res["new_sl"] is None # No trail update

    # Now price reaches 102.80 => profit = 2.80 => 1.40R >= 1.35R
    market_data["price_data"]["current_price"] = 102.80
    pos["highest_price"] = 102.80
    res2 = await sentinel.analyze(pos, market_data, profile_rules=profile_rules, atr_value=1.0)
    assert res2["state"] == "TRAILING"
    assert res2["new_sl"] is not None
    assert res2["new_sl"] > 100.05 # Trailed above BE!


@pytest.mark.asyncio
async def test_sentinel_agent_time_stop_sequential_timers_and_min_be_r(mock_logger):
    """
    Verifies Time-Stop sequential timers and min_be_r = 0.25 for TREND_FOLLOWING:
    - Age 200m (>= 180m) with profit +0.30R: moves SL to BE (TIME_STOP_BE)
    - Age 260m (>= 240m) without profit (loss -0.2R): tightens SL to -0.50R (TIME_STOP_TIGHTEN)
    - Age 380m (>= 360m) without profit: tightens SL to -0.25R (TIME_STOP_MAX_HORIZON)
    """
    sentinel = SentinelAgent(logger=mock_logger)
    now = time.time()
    profile_rules = {}

    # Case A: Age 200m with +0.30R profit
    pos_a = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "highest_price": 100.60,
        "initial_sl": 98.0,
        "planned_risk_usd": 2.0,
        "contracts": 1.0,
        "sl_price": 98.0,
        "protection_state": "ACTIVE",
        "strategy_mode": "TREND_FOLLOWING",
        "open_time": now - (200.0 * 60.0) # 200 min
    }
    market_data_a = {"price_data": {"current_price": 100.60}, "atr_14": 1.0}
    res_a = await sentinel.analyze(pos_a, market_data_a, profile_rules=profile_rules, atr_value=1.0)
    assert res_a["state"] == "TIME_STOP_BE"
    assert res_a["new_sl"] >= 100.0 # BE + cost buffer

    # Case B: Age 260m without profit (current_price = 99.50)
    pos_b = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "highest_price": 100.10,
        "initial_sl": 98.0, # 1R = 2.0
        "planned_risk_usd": 2.0,
        "contracts": 1.0,
        "sl_price": 98.0,
        "protection_state": "ACTIVE",
        "strategy_mode": "TREND_FOLLOWING",
        "open_time": now - (260.0 * 60.0) # 260 min
    }
    market_data_b = {"price_data": {"current_price": 99.50}, "atr_14": 1.0}
    res_b = await sentinel.analyze(pos_b, market_data_b, profile_rules=profile_rules, atr_value=1.0)
    assert res_b["state"] == "TIME_STOP_TIGHTEN"
    # Tightened to -0.50R => 100 - 0.50 * 2.0 = 99.0
    assert math.isclose(res_b["new_sl"], 99.0, abs_tol=1e-3)

    # Case C: Age 380m without profit
    pos_c = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "highest_price": 100.10,
        "initial_sl": 98.0,
        "planned_risk_usd": 2.0,
        "contracts": 1.0,
        "sl_price": 98.0,
        "protection_state": "ACTIVE",
        "strategy_mode": "TREND_FOLLOWING",
        "open_time": now - (380.0 * 60.0) # 380 min
    }
    market_data_c = {"price_data": {"current_price": 99.50}, "atr_14": 1.0}
    res_c = await sentinel.analyze(pos_c, market_data_c, profile_rules=profile_rules, atr_value=1.0)
    assert res_c["state"] == "TIME_STOP_MAX_HORIZON"
    # Tightened to -0.25R => 100 - 0.25 * 2.0 = 99.50
    assert math.isclose(res_c["new_sl"], 99.50, abs_tol=1e-3)


# ─── 7. FastPriceMonitor Instant BE for Single-Tier ──────────────────────────

@pytest.mark.asyncio
async def test_fast_price_monitor_instant_be_activation_on_single_tier():
    """
    Verifies that FastPriceMonitor immediately sets be_moved=True, protection_state='PENDING_BREAK_EVEN',
    and dispatches update_stop_loss_to_be when cur_mfe_r >= be_trigger_r (0.75R).
    """
    service = NadoTradingService()
    service.is_connected = True
    service.client = MagicMock()
    service.update_stop_loss_to_be = AsyncMock(return_value=True)
    service._save_positions = MagicMock()

    # SINGLE_TIER position ($104 notional, no early TP)
    # 1R = 100 - 98 = 2.0. With cnt=1, p_risk = 2.0.
    # highest_price = 101.60 => cur_mfe_r = 1.60 / 2.0 = 0.80 >= 0.75
    pos = {
        "symbol": "SOL-USD",
        "product_id": 1,
        "direction": "LONG",
        "entry_price": 100.0,
        "highest_price": 101.60,
        "lowest_price": 100.0,
        "contracts": 1.0,
        "planned_risk_usd": 2.0,
        "sl_price": 98.0,
        "tp_price": 106.0,
        "be_activation_r": 0.75,
        "be_moved": False,
        "protection_state": "ACTIVE",
        "max_favorable_excursion_r": 0.0
    }
    service.active_positions = {"SOL-USD": pos}

    # Mock get_latest_market_price returning 101.50
    mock_price = MagicMock()
    mock_price.price_x18 = int(101.50 * 1e18)
    service.client.market.get_latest_market_price.return_value = mock_price

    with patch("core.config.config.SENTINEL_FAST_POLL_SEC", 0.01):
        monitor_task = asyncio.create_task(service.start_fast_price_monitor())
        await asyncio.sleep(0.05)
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    assert pos.get("be_moved") is True
    assert pos.get("protection_state") == "PENDING_BREAK_EVEN"
    assert pos.get("max_favorable_excursion_r") == 0.80
    service.update_stop_loss_to_be.assert_called_with("SOL-USD")


# ─── 8. PartialTPCalculator Fallback 4_TIER -> 2_TIER Check ──────────────────

def test_partial_tp_calculator_fallback_min_notional_check():
    """
    Verifies that PartialTPCalculator fallback branch 4_TIER -> 2_TIER checks effective_min_notional:
    - Total position $150 (contracts 1.5 @ $100), min_notional = $100:
      4-tier fails ($37.5 < $105), 2-tier fails ($75 < $105) => Falls back cleanly to SINGLE_TIER!
    - Total position $300 (contracts 3.0 @ $100), min_notional = $100:
      4-tier fails ($75 < $105), but 2-tier succeeds ($150 >= $105) => Mode is 2_TIER!
    """
    # Scenario 1: $150 total notional with min_notional $100
    plan_small = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=106.0,
        total_contracts=1.5,
        market_params={
            "size_increment": 0.1,
            "price_increment": 0.01,
            "min_size": 0.1,
            "min_notional": 100.0,
            "preferred_mode": "4_TIER"
        }
    )
    # 2-tier notional would be 0.7 * 100 = $70 < $105 => SINGLE_TIER
    assert plan_small.mode == "SINGLE_TIER"
    assert len(plan_small.tiers) == 1
    assert plan_small.tiers[0].fraction == 1.0

    # Scenario 2: $300 total notional with min_notional $100
    plan_large = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=106.0,
        total_contracts=3.0,
        market_params={
            "size_increment": 0.1,
            "price_increment": 0.01,
            "min_size": 0.1,
            "min_notional": 100.0,
            "preferred_mode": "4_TIER"
        }
    )
    # 4-tier would be 0.7 * 100 = $70 < $105, but 2-tier is 1.5 * 100 = $150 >= $105 => 2_TIER
    assert plan_large.mode == "2_TIER"
    assert len(plan_large.tiers) == 2
    assert plan_large.tiers[0].fraction == 0.50
    assert plan_large.tiers[1].fraction == 1.00


# ─── Profit Boosters Verification Tests ──────────────────────────────────────

@pytest.mark.asyncio
async def test_booster1_structural_take_profit_in_front_of_48h_box(mock_logger):
    """
    Verifies Profit Booster #1:
    In TREND_FOLLOWING, when standard tp_price overshoots the 48h Macro Box boundary,
    tp_price is locked just in front of the boundary (low_48h + 0.15*ATR for SHORT, high_48h - 0.15*ATR for LONG)
    if the resulting R:R >= 1.45.
    """
    rm = RiskManager(logger=mock_logger, llm_client=MagicMock())

    current_price = 100.0
    atr_14 = 1.0
    # SHORT scenario:
    # sl_dist_base = 1.5 (SL at 101.5), standard TP at 96.0 (4.0R distance)
    # low_48h is at 97.0 (support floor), so standard TP (96.0) is beyond 48h low!
    # box_tp = 97.0 + 0.15 * 1.0 = 97.15.
    # R:R = (100.0 - 97.15) / 1.5 = 2.85 / 1.5 = 1.90 >= 1.45.
    market_data_short = {
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": atr_14,
            "low_48h": 97.0,
            "high_48h": 105.0,
            "swing_high_15m": 101.0,
            "swing_low_15m": 97.5,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {
            "size_increment": 0.1,
            "price_increment": 0.01,
            "min_size": 0.1,
            "min_notional": 0.0
        }
    }
    ceo_decision_short = {
        "decision": "SHORT",
        "trade_action": "ENTER",
        "conviction": 85,
        "entry_quality": 80,
        "strategy_mode": "TREND_FOLLOWING",
        "is_pullback_retest": False
    }

    portfolio_data = {
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "active_positions": {}
    }

    res_short = await rm.analyze(
        ceo_decision=ceo_decision_short,
        portfolio_data=portfolio_data,
        market_data=market_data_short,
        macro_regime="TRENDING_BEARISH"
    )
    assert res_short.get("approved") is True
    # tp_price must be adjusted to box_tp = 97.15
    assert abs(res_short["tp_price"] - 97.15) < 0.01

    # LONG scenario:
    # current_price = 100.0, SL at 98.5 (dist = 1.5), high_48h at 103.0
    # standard TP at 104.5 > high_48h (103.0)
    # box_tp = 103.0 - 0.15 * 1.0 = 102.85.
    # R:R = (102.85 - 100.0) / 1.5 = 2.85 / 1.5 = 1.90 >= 1.45.
    market_data_long = {
        "price_data": {"current_price": current_price},
        "indicators": {
            "atr_14": atr_14,
            "low_48h": 95.0,
            "high_48h": 103.0,
            "swing_high_15m": 102.5,
            "swing_low_15m": 99.0,
            "macro_range_score": 30.0,
            "bb_pos": 50.0
        },
        "derivatives_data": {
            "size_increment": 0.1,
            "price_increment": 0.01,
            "min_size": 0.1,
            "min_notional": 0.0
        }
    }
    ceo_decision_long = {
        "decision": "LONG",
        "trade_action": "ENTER",
        "conviction": 85,
        "entry_quality": 80,
        "strategy_mode": "TREND_FOLLOWING",
        "is_pullback_retest": False
    }

    res_long = await rm.analyze(
        ceo_decision=ceo_decision_long,
        portfolio_data=portfolio_data,
        market_data=market_data_long,
        macro_regime="TRENDING_BULLISH"
    )
    assert res_long.get("approved") is True
    assert abs(res_long["tp_price"] - 102.85) < 0.01


@pytest.mark.asyncio
async def test_booster3_sentinel_stepped_runner_atr_floor(mock_logger):
    """
    Verifies Profit Booster #3:
    In Dynamic Parabolic Trailing, the ATR clamp distance uses a stepped floor:
    min_atr_floor = 0.85 when 1.35R <= effective_r < 3.0 (gives room to breathe),
    min_atr_floor = 0.50 when effective_r >= 3.0 (locks in big gains).
    """
    sentinel = SentinelAgent(logger=mock_logger)
    profile_rules = {"sentinel_trail_distance_atr": 1.0, "sentinel_min_improve_atr": 0.05}

    # 1. Position at 2.0R (below 3.0R): min_atr_floor must be 0.85
    pos_2r = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 50000.0,
        "current_price": 52000.0,
        "highest_price": 52000.0,
        "lowest_price": 50000.0,
        "initial_sl": 49000.0, # r_dist = 1000
        "sl_price": 50000.0,
        "protection_state": "BREAK_EVEN",
        "strategy_mode": "TREND_FOLLOWING",
        "contracts": 1.0,
        "planned_risk_usd": 1000.0,
        "open_time": time.time() - 3600,
        "is_runner": True
    }
    market_data = {
        "symbol": "BTC-USD",
        "price_data": {"current_price": 52000.0},
        "indicators": {"atr_14": 1000.0}
    }
    # parabolic_mult at 2.0R = 1 / (1 + 0.35 * 1.0) = 1 / 1.35 = 0.7407
    # runner_trail_dist * parabolic_mult = 1.0 * 0.7407 = 0.7407 < 0.85
    # clamped_dist must be clamped up to 0.85 (NOT down to 0.45!)
    # trail_target = 52000 - 0.85 * 1000 = 51150.0
    res_2r = await sentinel.analyze(pos_2r, market_data, profile_rules=profile_rules, atr_value=1000.0)
    assert res_2r["state"] == "TRAILING"
    assert abs(res_2r["new_sl"] - 51150.0) < 1.0

    # 2. Position at 4.0R (>= 3.0R): min_atr_floor can clamp down to 0.50
    # parabolic_mult at 4.0R = 1 / (1 + 0.35 * 3.0) = 1 / 2.05 = 0.4878
    # clamped_dist = max(0.50, 0.4878) = 0.50
    pos_4r = dict(pos_2r)
    pos_4r["current_price"] = 54000.0
    pos_4r["highest_price"] = 54000.0
    market_data_4r = {
        "symbol": "BTC-USD",
        "price_data": {"current_price": 54000.0},
        "indicators": {"atr_14": 1000.0}
    }
    res_4r = await sentinel.analyze(pos_4r, market_data_4r, profile_rules=profile_rules, atr_value=1000.0)
    assert res_4r["state"] == "TRAILING"
    # trail_target = 54000 - 0.50 * 1000 = 53500.0
    assert abs(res_4r["new_sl"] - 53500.0) < 1.0
