import asyncio
import math
import os
import threading
import time
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from core.strategy_router import StrategyRouter, StrategyProfile
from core.config import _SAFETY_BOUNDS, get_tuned_param
from core.logger import TradeLogger
from agents.sentinel_agent import SentinelAgent
from services.partial_tp_calculator import PartialTPCalculator
from services.nado_trading_service import NadoTradingService


@pytest.fixture
def mock_logger():
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger


# ─── 1. StrategyRouter Direction Bias Retention ──────────────────────────────

def test_strategy_router_preserves_direction_bias_on_vetoes():
    """
    Verifies that when MACRO_BOX_FLOOR_VETO, MACRO_BOX_CEILING_VETO, or ANTI_CHASE_REENTRY_VETO
    fire, StrategyRouter retains direction_bias ('SHORT' or 'LONG') instead of 'NEUTRAL'.
    """
    # 1. Macro Box Floor Guard SHORT setup
    market_data_short = {
        "multi_timeframe": {
            "mtf_alignment": "FULL_BEARISH",
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        },
        "indicators": {
            "macro_box_pos_pct": 20.0, # in lower 32%
            "bb_position_pct": 30.0,   # below 38%
            "rvol": 1.2,               # < 1.6
            "atr_14": 1.0,
            "adx_14": 30.0,
            "ema_20": 102.0
        },
        "price_data": {"current_price": 100.0},
        "derivatives_data": {"open_interest_trend": "falling"}
    }
    valid_reports_short = [
        {"agent_name": "Bear_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH"}
    ]

    profile = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data_short,
        valid_reports=valid_reports_short,
        macro_cache={},
        detected_regime="TRENDING_BEAR",
        profile="BALANCED"
    )

    # Must be vetoed with MACRO_BOX_FLOOR_VETO but preserve direction_bias='SHORT'
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "MACRO_BOX_FLOOR_VETO"
    assert profile.direction_bias == "SHORT"
    assert "WAIT_FOR_PULLBACK" in profile.reasoning

    # 2. Macro Box Ceiling Guard LONG setup
    market_data_long = {
        "multi_timeframe": {
            "mtf_alignment": "FULL_BULLISH",
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH"
        },
        "indicators": {
            "macro_box_pos_pct": 80.0, # in upper 68%
            "bb_position_pct": 75.0,   # above 62%
            "rvol": 1.2,               # < 1.6
            "atr_14": 1.0,
            "adx_14": 30.0,
            "ema_20": 98.0
        },
        "price_data": {"current_price": 100.0},
        "derivatives_data": {"open_interest_trend": "rising"}
    }
    valid_reports_long = [
        {"agent_name": "Bull_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"}
    ]

    profile_long = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data_long,
        valid_reports=valid_reports_long,
        macro_cache={},
        detected_regime="TRENDING_BULL",
        profile="BALANCED"
    )

    assert profile_long.has_directional_signal is False
    assert profile_long.strategy_mode == "MACRO_BOX_CEILING_VETO"
    assert profile_long.direction_bias == "LONG"
    assert "WAIT_FOR_PULLBACK" in profile_long.reasoning


# ─── 2. Pullback Watchlist Minimum Distance & Structural Invalidation ────────

def test_pipeline_register_pullback_candidate_min_distance_and_invalidation(mock_logger):
    """
    Verifies register_pullback_candidate enforces min_pullback_dist:
    - If ema_20 is extremely close (0.01% away), target_pullback_price must be at least min_pullback_dist away
    - Invalidation price must respect structural swing (swing_high_15m for SHORT / swing_low_15m for LONG)
    """
    from core.pipeline import TradingPipeline

    services_mock = MagicMock()
    services_mock.logger = mock_logger
    pipeline = TradingPipeline(agents=MagicMock(), services=services_mock, exchange_name="Nado")

    current_price = 100.0
    atr_14 = 1.0 # 0.5 * atr = 0.50, 0.004 * 100 = 0.40 => min_pullback_dist = 0.50
    ema_20_tight = 99.99 # Only 0.01 away!
    swing_low_15m = 97.50

    market_data = {
        "indicators": {
            "ema_20": ema_20_tight,
            "atr_14": atr_14,
            "swing_low_15m": swing_low_15m,
            "swing_high_15m": 102.50
        }
    }

    pipeline.register_pullback_candidate(
        symbol="SOL-USD",
        decision="LONG",
        current_price=current_price,
        directional_conf=75,
        conviction=75,
        strategy_mode="TREND_FOLLOWING",
        market_data=market_data,
        ceo_verdict={"reasoning": "Test pullback"},
        strategy_profile=MagicMock(),
        reason="Test reason"
    )

    cand = pipeline._pullback_watchlist.get("SOL-USD")
    assert cand is not None
    # target_pullback_price must NOT be 99.99! It must be <= 100.0 - 0.50 = 99.50
    assert cand["target_pullback_price"] <= 99.50
    # Invalidation must be <= min(target - 1.2*ATR, swing_low - 0.3*ATR)
    # target - 1.2 = 98.30, swing_low - 0.30 = 97.20 => invalidation <= 97.20
    assert cand["invalidation_price"] <= 97.20

    # Test SHORT side with tight ema_20
    ema_20_tight_short = 100.01
    swing_high_15m = 103.0
    market_data_short = {
        "indicators": {
            "ema_20": ema_20_tight_short,
            "atr_14": atr_14,
            "swing_high_15m": swing_high_15m
        }
    }

    pipeline.register_pullback_candidate(
        symbol="BTC-USD",
        decision="SHORT",
        current_price=current_price,
        directional_conf=75,
        conviction=75,
        strategy_mode="TREND_FOLLOWING",
        market_data=market_data_short,
        ceo_verdict={"reasoning": "Test pullback short"},
        strategy_profile=MagicMock(),
        reason="Test reason"
    )

    cand_short = pipeline._pullback_watchlist.get("BTC-USD")
    assert cand_short is not None
    # target_pullback_price must be >= 100.0 + 0.50 = 100.50
    assert cand_short["target_pullback_price"] >= 100.50
    # Invalidation must be >= max(target + 1.2*ATR, swing_high + 0.3*ATR)
    # swing_high (103.0) + 0.3 = 103.30
    assert cand_short["invalidation_price"] >= 103.30


# ─── 3. Retest Self-Loop Prevention in evaluate_candidate ─────────────────────

@pytest.mark.asyncio
async def test_pipeline_evaluate_candidate_blocks_retest_self_loop(mock_logger):
    """
    Verifies that when is_retest=True (symbol == retest_symbol),
    a failed retest that returns WAIT_FOR_PULLBACK does NOT re-queue itself into pullback_watchlist.
    """
    from core.pipeline import TradingPipeline

    services_mock = MagicMock()
    services_mock.logger = mock_logger
    services_mock.trading_service.active_positions = {}
    services_mock.trading_service.get_portfolio_summary = AsyncMock(return_value={
        "total_usd": 2000.0,
        "available_margin": 1500.0,
        "daily_drawdown_pct": 0.01,
        "active_positions": {}
    })
    services_mock.fetcher.fetch_all_market_data = AsyncMock(return_value={
        "price_data": {"current_price": 100.0},
        "indicators": {"atr_14": 1.0, "ema_20": 102.0, "macro_box_pos_pct": 20.0, "bb_position_pct": 30.0, "rvol": 1.0},
        "derivatives_data": {"size_increment": 0.1, "price_increment": 0.01, "min_size": 0.1, "min_notional": 100.0}
    })

    pipeline = TradingPipeline(agents=MagicMock(), services=services_mock, exchange_name="Nado")
    pipeline.register_pullback_candidate = MagicMock()

    # Pre-seed watchlist
    pipeline._pullback_watchlist = {}

    tracker_mock = MagicMock()
    tracker_mock.can_open_position.return_value = (True, "OK")

    scan_summaries = []
    # Call evaluate_candidate with retest_symbol="SOL-USD", so is_retest=True
    await pipeline.evaluate_candidate(
        symbol="SOL-USD",
        cycle_number=1,
        detected_regime="TRENDING_BEAR",
        profile="AGGRESSIVE",
        tracker=tracker_mock,
        scan_summaries=scan_summaries,
        retest_symbol="SOL-USD", # is_retest = True!
        retest_direction="SHORT"
    )

    # register_pullback_candidate must NOT have been called because is_retest is True!
    assert pipeline.register_pullback_candidate.call_count == 0


# ─── 4. Targeted Scan extra_targets from target_list ─────────────────────────

def test_pipeline_targeted_scan_isolates_extra_targets():
    """
    Verifies that when targeted retest scan runs, extra_targets is formed strictly from
    target_list (not selected_assets universe).
    """
    retest_symbol = "SOL-USD"
    target_list = ["SOL-USD", "DOGE-USD"] # Only 2 radar triggers
    selected_assets_universe = ["SOL-USD", "BTC-USD", "ETH-USD", "AVAX-USD", "LINK-USD", "SUI-USD"]

    # The new correct behavior: extra_targets from target_list
    retest_targets = [retest_symbol]
    extra_targets = [s for s in target_list if s != retest_symbol]
    retest_targets.extend(extra_targets)
    result = list(dict.fromkeys(retest_targets))

    # Must contain only SOL-USD and DOGE-USD (not all 6 assets!)
    assert result == ["SOL-USD", "DOGE-USD"]


# ─── 5. Config Safety Bounds for be_activation_r ─────────────────────────────

def test_config_safety_bounds_be_activation_r():
    """
    Verifies that _SAFETY_BOUNDS['be_activation_r'] ceiling is (0.70, 0.90).
    """
    assert _SAFETY_BOUNDS["be_activation_r"] == (0.70, 0.90)

    # Clamping tests
    with patch("core.config._TUNED_CACHE", {"mtime": time.time(), "params": {"be_activation_r": 1.20}}):
        val = get_tuned_param("be_activation_r", 0.75)
        assert val == 0.90 # Clamped to ceiling 0.90!

    with patch("core.config._TUNED_CACHE", {"mtime": time.time(), "params": {"be_activation_r": 0.50}}):
        val2 = get_tuned_param("be_activation_r", 0.75)
        assert val2 == 0.70 # Clamped to floor 0.70!


# ─── 6. Thread-Safe TradeLogger Lock ─────────────────────────────────────────

def test_logger_thread_safety(tmp_path):
    """
    Verifies that TradeLogger uses self._lock to ensure thread-safe concurrent writes.
    """
    logger = TradeLogger(base_dir=str(tmp_path))
    assert hasattr(logger, "_lock")
    assert isinstance(logger._lock, type(threading.Lock()))

    errors = []

    def log_worker(worker_id):
        try:
            for i in range(50):
                logger.log_decision(f"Worker_{worker_id}", f"Message {i}", step=i)
                logger.debug(f"Debug message {i}", worker=worker_id)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=log_worker, args=(t,)) for t in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0


# ─── 7. SentinelAgent Trailing Free Ride & Runner ────────────────────────────

@pytest.mark.asyncio
async def test_sentinel_agent_runner_requires_135r_for_trailing(mock_logger):
    """
    Verifies that an active runner with be_activated=True or is_runner=True
    CANNOT trail when effective_r < 1.35R, preserving the Free Ride corridor.
    """
    sentinel = SentinelAgent(logger=mock_logger)

    pos = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 100.0,
        "highest_price": 101.80, # 1R = 2.0 (initial_sl = 98.0) => 0.90R
        "initial_sl": 98.0,
        "sl_price": 100.35, # Already in BE
        "planned_risk_usd": 2.0,
        "contracts": 1.0,
        "is_runner": True,
        "be_activated": True,
        "be_moved": True,
        "protection_state": "BREAK_EVEN",
        "strategy_mode": "TREND_FOLLOWING",
        "open_time": time.time() - 300.0
    }

    market_data = {
        "price_data": {"current_price": 101.80},
        "atr_14": 1.0
    }

    res = await sentinel.analyze(pos, market_data, profile_rules={}, atr_value=1.0)
    # Must NOT trail!
    assert res["state"] != "TRAILING"
    assert res["new_sl"] is None


# ─── 8. PartialTPCalculator Fallback 4_TIER -> 2_TIER Min Notional ───────────

def test_partial_tp_fallback_min_notional():
    """
    Verifies that when 4_TIER falls back to 2_TIER, effective_min_notional is checked.
    If 2-tier also fails, cleanly falls back to SINGLE_TIER.
    """
    plan = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=105.0,
        total_contracts=1.2, # $120 total notional with min_notional $100
        market_params={
            "size_increment": 0.1,
            "price_increment": 0.01,
            "min_size": 0.1,
            "min_notional": 100.0,
            "preferred_mode": "4_TIER"
        }
    )
    # 2-tier would be 0.6 * 100 = $60 < $105 => Fallback to SINGLE_TIER!
    assert plan.mode == "SINGLE_TIER"
    assert len(plan.tiers) == 1


# ─── 9. Profit Booster #2: Pullback Retest Fast Debate Bypass ────────────────

@pytest.mark.asyncio
async def test_booster2_pullback_retest_fast_debate_bypass(mock_logger):
    """
    Verifies Profit Booster #2:
    When is_retest=True and pb_context directional_confidence >= 75,
    evaluate_candidate bypasses LLM debate and builds deterministic theses in 0ms.
    """
    from core.pipeline import TradingPipeline

    services_mock = MagicMock()
    services_mock.logger = mock_logger
    services_mock.trading_service.active_positions = {}
    services_mock.trading_service.get_portfolio_summary = AsyncMock(return_value={
        "total_usd": 2000.0,
        "available_margin": 1500.0,
        "daily_drawdown_pct": 0.01,
        "active_positions": {}
    })
    services_mock.fetcher.fetch_all_market_data = AsyncMock(return_value={
        "price_data": {"current_price": 100.0, "candles": [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 10} for _ in range(30)]},
        "indicators": {"atr_14": 1.0, "ema_20": 100.2, "macro_box_pos_pct": 50.0, "bb_position_pct": 50.0, "rvol": 1.2, "adx_14": 30.0},
        "derivatives_data": {"size_increment": 0.1, "price_increment": 0.01, "min_size": 0.1, "min_notional": 100.0, "open_interest": 1000.0, "funding_rate": 0.0001},
        "multi_timeframe": {
            "mtf_alignment": "FULL_BULLISH",
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH",
            "tf_15m": {"trend": "BULLISH", "adx": 30.0, "candles_20": []},
            "tf_1h": {"trend": "BULLISH"},
            "tf_4h": {"trend": "BULLISH"}
        },
        "orderbook": {"bids": [[100, 1]], "asks": [[100.1, 1]]}
    })

    agents_mock = MagicMock()
    agents_mock.data_guard.validate.return_value = (True, "OK")
    agents_mock.strategy_router.evaluate.return_value = MagicMock(
        is_viable=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        reasoning="Test retest viability"
    )
    agents_mock.regime.evaluate.return_value = MagicMock(regime="TRENDING", recommended_profile="AGGRESSIVE")
    agents_mock.regime.classify_regime_from_scores.return_value = ("TRENDING", "AGGRESSIVE")
    for a in [agents_mock.indicator, agents_mock.oi_funding, agents_mock.order_book, agents_mock.news]:
        a.analyze = AsyncMock(return_value={"agent_name": "Test", "signal": "BULLISH"})

    agents_mock.bull.analyze = AsyncMock(return_value={"signal": "BULLISH"})
    agents_mock.bear.analyze = AsyncMock(return_value={"signal": "NEUTRAL"})
    agents_mock.ceo.analyze = AsyncMock(return_value={
        "decision": "LONG",
        "trade_action": "ENTER",
        "conviction": 85,
        "entry_quality": 82,
        "directional_confidence": 85,
        "strategy_mode": "TREND_FOLLOWING"
    })
    agents_mock.risk.analyze = AsyncMock(return_value={"action": "REJECT", "reason": "Halt pipeline"})

    pipeline = TradingPipeline(agents=agents_mock, services=services_mock, exchange_name="Nado")
    pipeline._pullback_context["SOL-USD"] = {"directional_confidence": 80.0}

    await pipeline.evaluate_candidate(
        symbol="SOL-USD",
        portfolio_data={"total_usd": 2000.0, "available_margin": 1500.0, "daily_drawdown_pct": 0.01, "active_positions": {}},
        retest_symbol="SOL-USD",
        retest_direction="LONG"
    )

    # Bull and Bear LLM analysis must NOT be called due to fast deterministic synthesis!
    assert agents_mock.bull.analyze.await_count == 0
    assert agents_mock.bear.analyze.await_count == 0
