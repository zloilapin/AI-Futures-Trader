import pytest
from unittest.mock import MagicMock
from core.strategy_router import StrategyRouter

def test_breakout_blocked_in_deep_chop_range_72():
    """
    Verifies that when macro range score >= 70% (e.g. Cycle #4: RANGE 72.0%),
    a Donchian breakout (such as XRP-USD volume spike 281%) is strictly rejected
    to protect capital from bull/bear wick traps.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_1h": "BULLISH",
            "trend_4h": "NEUTRAL"
        },
        "indicators": {
            "volume_spike_pct": 281.0,
            "donchian_high": 1.3062,
            "donchian_low": 1.2500,
            "last_closed_candle_close": 1.3100,
            "bb_width_pct": 3.5,
            "bb_position_pct": 92.0
        },
        "price_data": {"current_price": 1.3120},
        "derivatives_data": {"open_interest_trend": "rising"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"}
    ]
    macro_cache = {
        "regime_verdict": {
            "regime": "RANGE_CHOPPY",
            "range_score": 72.0,
            "trend_score": 25.8
        }
    }

    profile = StrategyRouter.evaluate(
        "XRP-USD", market_data, valid_reports, macro_cache,
        detected_regime="RANGE_CHOPPY", profile="BALANCED"
    )

    # Must be rejected under deep chop anti-trap rule
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "BREAKOUT"
    assert profile.direction_bias == "NEUTRAL"
    assert "ОТКЛОНЕН" in profile.reasoning
    assert "Range=72.0%" in profile.reasoning

def test_full_alignment_long_blocked_at_channel_high_in_deep_chop():
    """
    Verifies that in extreme chop (e.g. Cycle #5: RANGE 91.8%),
    FULL_ALIGNMENT cannot enter LONG at the high of the channel (bb_position_pct >= 70%)
    such as re-entering ZEC-USD at $1252.27 on channel ceiling right after trailing exit.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BULLISH",
            "trend_4h": "BULLISH"
        },
        "indicators": {
            "volume_spike_pct": 60.0,
            "donchian_high": 1260.0,
            "donchian_low": 1200.0,
            "last_closed_candle_close": 1252.0,
            "bb_width_pct": 4.0,
            "bb_position_pct": 82.5  # High in Bollinger band
        },
        "price_data": {"current_price": 1252.27},
        "derivatives_data": {"open_interest_trend": "stable"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"}
    ]
    macro_cache = {
        "regime_verdict": {
            "regime": "RANGE_CHOPPY",
            "range_score": 91.8,
            "trend_score": 15.6
        }
    }

    profile = StrategyRouter.evaluate(
        "ZEC-USD", market_data, valid_reports, macro_cache,
        detected_regime="RANGE_CHOPPY", profile="BALANCED"
    )

    assert profile.has_directional_signal is False
    assert "ОТКЛОНЕН" in profile.reasoning
    assert "bb_pos=82.5% >= 70%" in profile.reasoning
    assert "Range=91.8%" in profile.reasoning

def test_full_alignment_short_blocked_at_channel_low_in_deep_chop():
    """
    Verifies that in chop, FULL_ALIGNMENT cannot enter SHORT at channel floor (bb_position_pct <= 30%).
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        },
        "indicators": {
            "volume_spike_pct": 50.0,
            "donchian_high": 1260.0,
            "donchian_low": 1200.0,
            "last_closed_candle_close": 1205.0,
            "bb_width_pct": 4.0,
            "bb_position_pct": 18.0  # Floor of Bollinger band
        },
        "price_data": {"current_price": 1204.0},
        "derivatives_data": {"open_interest_trend": "falling"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BEARISH"},
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"}
    ]
    macro_cache = {
        "regime_verdict": {
            "regime": "RANGE_CHOPPY",
            "range_score": 75.0,
            "trend_score": 20.0
        }
    }

    profile = StrategyRouter.evaluate(
        "ZEC-USD", market_data, valid_reports, macro_cache,
        detected_regime="RANGE_CHOPPY", profile="BALANCED"
    )

    assert profile.has_directional_signal is False
    assert "ОТКЛОНЕН" in profile.reasoning
    assert "bb_pos=18.0% <= 30%" in profile.reasoning

def test_mean_reversion_and_pullbacks_still_allowed_in_chop():
    """
    Verifies that while breakout and extreme chasing are blocked,
    legitimate MEAN_REVERSION entries from channel boundaries remain active.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_1h": "NEUTRAL",
            "trend_4h": "NEUTRAL"
        },
        "indicators": {
            "volume_spike_pct": 40.0,
            "donchian_high": 100.0,
            "donchian_low": 90.0,
            "bb_width_pct": 3.0,  # Adequate width for altcoin (> 1.5%)
            "bb_position_pct": 8.0  # At lower band <= 12%
        },
        "price_data": {"current_price": 91.0},
        "derivatives_data": {"open_interest_trend": "stable"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH"}
    ]
    macro_cache = {
        "regime_verdict": {
            "regime": "RANGE_CHOPPY",
            "range_score": 80.0
        }
    }

    profile = StrategyRouter.evaluate(
        "SOL-USD", market_data, valid_reports, macro_cache,
        detected_regime="RANGE_CHOPPY", profile="BALANCED"
    )

    assert profile.has_directional_signal is True
    assert profile.strategy_mode == "MEAN_REVERSION"
    assert profile.direction_bias == "LONG"
