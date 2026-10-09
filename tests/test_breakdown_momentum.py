import pytest
from unittest.mock import AsyncMock, MagicMock
from core.strategy_router import StrategyRouter, StrategyProfile
from core.deterministic_guard import DeterministicGuard
from core.models import FinalTradeDecision
from agents.indicator_agent import IndicatorAgent
from agents.risk_manager import RiskManager
from core.logger import TradeLogger

def test_indicator_agent_context_aware_rsi():
    """
    Verifies that in a macro bearish trend, RSI < 30 confirms selling momentum
    and yields a BEARISH signal instead of falsely triggering a bullish bounce.
    """
    logger = MagicMock(spec=TradeLogger)
    agent = IndicatorAgent(logger)
    
    # Market data with Bearish Macro Trend
    market_data = {
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT"
        },
        "indicators": {
            "rsi_14": 24.5,  # Extreme oversold in crash
            "macd": -50.0,
            "macd_signal": -40.0,  # Below signal line
            "ema_20": 2650.0
        },
        "price_data": {
            "current_price": 2580.0  # Below EMA-20
        }
    }
    
    import asyncio
    res = asyncio.run(agent.analyze(market_data))
    
    # In macro bearish context, RSI < 30 gives bear_score, EMA-20 gives bear_score, MACD gives bear_score
    assert res["signal"] == "BEARISH"
    assert "импульса продаж" in res["reasoning"]

def test_indicator_agent_range_rsi_preserves_mean_reversion():
    """
    Verifies that in a neutral / range market, RSI < 30 preserves traditional
    oversold (bullish bounce) mean-reversion scoring.
    """
    logger = MagicMock(spec=TradeLogger)
    agent = IndicatorAgent(logger)
    
    market_data = {
        "multi_timeframe": {
            "trend_1h": "NEUTRAL",
            "trend_4h": "NEUTRAL",
            "mtf_alignment": "MIXED_CHOP"
        },
        "indicators": {
            "rsi_14": 25.0,
            "macd": 5.0,
            "macd_signal": 2.0,
            "ema_20": 2600.0
        },
        "price_data": {
            "current_price": 2610.0
        }
    }
    
    import asyncio
    res = asyncio.run(agent.analyze(market_data))
    assert "RSI перепродан" in res["reasoning"]

def test_strategy_router_breakout_short_on_dump():
    """
    Verifies that StrategyRouter triggers BREAKOUT SHORT on high volume
    Donchian breakdown when 1H is BEARISH, even if the order book is neutral.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        },
        "indicators": {
            "volume_spike_pct": 110.0,  # Above 100% threshold
            "donchian_high": 2700.0,
            "donchian_low": 2620.0,
            "last_closed_candle_close": 2600.0,  # Below Donchian low
            "bb_width_pct": 8.0,
            "bb_position_pct": 2.0
        },
        "price_data": {"current_price": 2590.0},
        "derivatives_data": {"open_interest_trend": "falling"}
    }
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL"}  # Neutral order book
    ]
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        "ETH-USD", market_data, valid_reports, macro_cache,
        detected_regime="BEAR_TREND", profile="BALANCED"
    )

    assert profile.has_directional_signal is True
    assert profile.strategy_mode == "BREAKOUT"
    assert profile.direction_bias == "SHORT"
    assert "BREAKOUT SHORT" in profile.reasoning

def test_strategy_router_counter_trend_warning_allows_impulse_short():
    """
    Verifies that in COUNTER_TREND_WARNING (e.g. 1H/4H Bearish, 15m briefly adjusting),
    a bearish candle with volume does NOT get falsely rejected as 'counter-trend trade'.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "COUNTER_TREND_WARNING",
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        },
        "indicators": {
            "volume_spike_pct": 65.0,
            "donchian_high": 2700.0,
            "donchian_low": 2550.0,
            "last_closed_candle_close": 2610.0,
            "bb_width_pct": 6.0,
            "bb_position_pct": 20.0
        },
        "price_data": {"current_price": 2605.0},
        "derivatives_data": {"open_interest_trend": "falling"}
    }
    valid_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Indicator_Agent", "signal": "NEUTRAL"}
    ]
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        "ETH-USD", market_data, valid_reports, macro_cache,
        detected_regime="BEAR_TREND", profile="BALANCED"
    )

    assert profile.has_directional_signal is True
    assert profile.strategy_mode == "TREND_FOLLOWING"
    assert profile.direction_bias == "SHORT"
    assert "Импульс/откат по тренду" in profile.reasoning

def test_deterministic_guard_breakout_pullback_override_confirmed():
    """
    Verifies that for BREAKOUT setups, DeterministicGuard converts WAIT_FOR_PULLBACK
    to ENTER ONLY WHEN market indicators confirm the Donchian breakdown + volume surge.
    """
    strategy_profile = StrategyProfile(True, "BREAKOUT", "SHORT", "Donchian breakdown")
    ceo_proposal = {
        "decision": "SHORT",
        "conviction": 75,
        "trade_action": "WAIT_FOR_PULLBACK",
        "directional_confidence": 80,
        "entry_quality": 60,
        "reasoning": "Breakdown underway"
    }
    market_data = {
        "price_data": {"current_price": 2500.0},
        "indicators": {
            "donchian_low": 2550.0,
            "volume_spike_pct": 130.0
        }
    }
    
    mock_risk = MagicMock(spec=RiskManager)
    mock_risk._get_profile_rules.return_value = {"min_conviction": 65}
    
    res = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=ceo_proposal,
        profile="BALANCED",
        risk_manager=mock_risk,
        market_data=market_data,
        symbol="ETH-USD"
    )
    
    assert res.decision == "SHORT"
    assert res.trade_action == "ENTER"
    assert res.is_actionable is True

def test_deterministic_guard_breakout_unconfirmed_preserves_pullback():
    """
    Verifies that for BREAKOUT setups WITHOUT confirmed Donchian break / volume surge,
    DeterministicGuard PRESERVES WAIT_FOR_PULLBACK and does not force entry.
    """
    strategy_profile = StrategyProfile(True, "BREAKOUT", "SHORT", "Donchian setup")
    ceo_proposal = {
        "decision": "SHORT",
        "conviction": 75,
        "trade_action": "WAIT_FOR_PULLBACK",
        "directional_confidence": 80,
        "entry_quality": 60,
        "reasoning": "Breakdown pending"
    }
    # Market data is inside the range (no breakdown)
    market_data = {
        "price_data": {"current_price": 2600.0},
        "indicators": {
            "donchian_low": 2550.0,  # Price (2600) is ABOVE donchian low (2550)
            "volume_spike_pct": 30.0
        }
    }
    
    mock_risk = MagicMock(spec=RiskManager)
    mock_risk._get_profile_rules.return_value = {"min_conviction": 65}
    
    res = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=ceo_proposal,
        profile="BALANCED",
        risk_manager=mock_risk,
        market_data=market_data,
        symbol="ETH-USD"
    )
    
    assert res.decision == "SHORT"
    assert res.trade_action == "WAIT_FOR_PULLBACK"
    assert res.is_actionable is False
    assert res.guard_status == "PULLBACK_WATCHLIST"
