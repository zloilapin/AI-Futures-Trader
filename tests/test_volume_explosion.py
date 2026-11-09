import pytest
from unittest.mock import MagicMock
from core.deterministic_guard import DeterministicGuard
from core.strategy_router import StrategyRouter, StrategyProfile
from agents.risk_manager import RiskManager


def test_volume_explosion_overrides_wait_for_pullback_to_reduce_size():
    """
    Test that a 3.0x volume explosion (>= 300%) with strong directional conviction (>= 75%)
    and a solid full-bodied breakout candle (no exhaustion wick) safely overrides
    WAIT_FOR_PULLBACK to REDUCE_SIZE and elevates conviction/entry_qual to the Optuna floor (71.0%).
    """
    mock_risk = MagicMock()
    mock_risk._get_profile_rules.return_value = {"min_conviction": 70}

    ceo_proposal = {
        "symbol": "HYPE-USD",
        "decision": "SHORT",
        "conviction": 56, # Depressed by CEO penalty
        "directional_confidence": 76,
        "entry_quality": 56, # Low because price moved away from EMA
        "trade_action": "WAIT_FOR_PULLBACK",
        "sl_price": 81.50 # Distance to SL: (81.50 - 79.696)/79.696 = 2.26% <= 3.5%
    }

    market_data = {
        "price_data": {
            "current_price": 79.696,
            "candles_20": [
                # Candle open=81.0, high=81.2, low=79.5, close=79.696
                # Candle range = 1.7, c_pos = (79.696 - 79.5)/1.7 = 0.197 <= 0.35 (solid bear candle closing at bottom)
                {"open": 81.0, "high": 81.2, "low": 79.5, "close": 79.696, "volume": 60000}
            ]
        },
        "indicators": {
            "volume_spike_pct": 320.0, # 3.2x volume explosion
            "current_price": 79.696
        }
    }

    strategy_profile = StrategyProfile(True, "BREAKOUT", "SHORT", "BREAKOUT SHORT")
    verdict = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=mock_risk,
        market_data=market_data,
        symbol="HYPE-USD"
    )

    assert verdict.is_actionable is True
    assert verdict.trade_action == "REDUCE_SIZE"
    assert verdict.decision == "SHORT"
    assert verdict.volume_explosion_override is True
    assert verdict.action_size_mult_override == 0.50
    assert verdict.conviction >= 71
    assert verdict.entry_quality >= 71


def test_volume_explosion_blocked_by_exhaustion_wick():
    """
    Test that an exhaustion wick (e.g. LONG candle rejected with large upper wick, c_pos < 0.65)
    blocks VOLUME_EXPLOSION and retains WAIT_FOR_PULLBACK to prevent buying blow-off tops.
    """
    mock_risk = MagicMock()
    mock_risk._get_profile_rules.return_value = {"min_conviction": 70}

    ceo_proposal = {
        "symbol": "HYPE-USD",
        "decision": "LONG",
        "conviction": 55,
        "directional_confidence": 80,
        "entry_quality": 55,
        "trade_action": "WAIT_FOR_PULLBACK",
        "sl_price": 84.0
    }

    # Candle spiked to 88.0, but dropped back to 85.0:
    # Range = 88.0 - 83.0 = 5.0. c_pos = (85.0 - 83.0) / 5.0 = 0.40 < 0.65 (exhaustion wick on top)
    market_data = {
        "price_data": {
            "current_price": 85.0,
            "candles_20": [
                {"open": 83.5, "high": 88.0, "low": 83.0, "close": 85.0, "volume": 75000}
            ]
        },
        "indicators": {
            "volume_spike_pct": 350.0,
            "current_price": 85.0
        }
    }

    strategy_profile = StrategyProfile(True, "BREAKOUT", "LONG", "BREAKOUT LONG")
    verdict = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=mock_risk,
        market_data=market_data,
        symbol="HYPE-USD"
    )

    assert verdict.volume_explosion_override is False
    assert verdict.trade_action == "WAIT_FOR_PULLBACK"
    assert verdict.is_actionable is False


def test_volume_explosion_blocked_by_sl_distance_cap():
    """
    Test that if distance to structural SL exceeds 3.5%, VOLUME_EXPLOSION market entry is blocked.
    """
    mock_risk = MagicMock()
    mock_risk._get_profile_rules.return_value = {"min_conviction": 70}

    ceo_proposal = {
        "symbol": "HYPE-USD",
        "decision": "LONG",
        "conviction": 55,
        "directional_confidence": 80,
        "entry_quality": 55,
        "trade_action": "WAIT_FOR_PULLBACK",
        "sl_price": 76.0 # Distance: (80.0 - 76.0)/80.0 = 5.0% > 3.5%
    }

    market_data = {
        "price_data": {
            "current_price": 80.0,
            "candles_20": [
                {"open": 77.0, "high": 80.5, "low": 77.0, "close": 80.0, "volume": 70000}
            ]
        },
        "indicators": {
            "volume_spike_pct": 320.0,
            "current_price": 80.0
        }
    }

    strategy_profile = StrategyProfile(True, "BREAKOUT", "LONG", "BREAKOUT LONG")
    verdict = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=dict(ceo_proposal),
        profile="BALANCED",
        risk_manager=mock_risk,
        market_data=market_data,
        symbol="HYPE-USD"
    )

    assert verdict.volume_explosion_override is False
    assert verdict.trade_action == "WAIT_FOR_PULLBACK"


def test_strategy_router_volume_explosion_bypass():
    """
    Test that StrategyRouter permits Breakout in deep chop or transition
    when volume_spike_pct >= 300% and at least 2 aligned analysts agree.
    """
    market_data = {
        "price_data": {
            "current_price": 82.0
        },
        "indicators": {
            "donchian_high": 80.0,
            "donchian_low": 75.0,
            "volume_spike_pct": 310.0, # Volume explosion
            "last_closed_candle_close": 81.5,
            "range_score": 75.0, # Deep chop range
            "bb_width_pct": 3.0,
            "bb_position_pct": 115.0 # Extended past Bollinger
        },
        "derivatives_data": {
            "open_interest_trend": "rising"
        }
    }

    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH"},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL"},
        {"agent_name": "News_Agent", "signal": "NEUTRAL"}
    ]

    profile = StrategyRouter.evaluate(
        symbol="HYPE-USD",
        market_data=market_data,
        valid_reports=analyst_reports,
        detected_regime="RANGE_CHOPPY",
        macro_cache={}
    )

    assert profile.has_directional_signal is True
    assert profile.strategy_mode == "BREAKOUT"
    assert profile.direction_bias == "LONG"


@pytest.mark.asyncio
async def test_risk_manager_handles_volume_explosion_cohort_b_and_half_size():
    """
    Test that RiskManager categorizes volume_explosion_override into Cohort B
    (preventing Cohort D veto) and enforces 0.50x action size multiplier.
    """
    rm = RiskManager(logger=MagicMock(), llm_client=MagicMock())

    ceo_decision = {
        "symbol": "HYPE-USD",
        "decision": "SHORT",
        "conviction": 71,
        "directional_confidence": 76,
        "entry_quality": 71,
        "trade_action": "REDUCE_SIZE",
        "volume_explosion_override": True,
        "action_size_mult_override": 0.50
    }

    portfolio_data = {
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "active_positions": {},
        "win_count": 0,
        "loss_count": 0,
        "recent_streak": []
    }

    market_data = {
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

    verdict = await rm.analyze(
        ceo_decision,
        portfolio_data,
        market_data,
        effective_profile="BALANCED",
        macro_regime="TRENDING"
    )

    assert verdict["approved"] is True
    assert verdict["cohort"] == "B"
    applied = verdict["applied_multipliers"]
    assert applied["action_mult"] == 0.50
