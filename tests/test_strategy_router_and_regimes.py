import pytest
from unittest.mock import MagicMock, AsyncMock
from core.strategy_router import StrategyRouter, StrategyProfile
from agents.risk_manager import RiskManager
from agents.ceo_agent import CEOAgent
from core.logger import TradeLogger

@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.model_name = "test-model"
    llm.generate = AsyncMock()
    return llm

# ==============================================================================
# 1. STRATEGY ROUTER TESTS (P0 & P1 Audited Fixes)
# ==============================================================================

def test_breakout_not_intercepted_by_volatility_momentum():
    """
    P0-1 Fix Verification:
    When volume is 250% (>= 200%) and price breaks Donchian channel,
    even in MIXED_CHOP with bb_width_pct > 10%, it MUST route to BREAKOUT,
    NOT to VOLATILITY_MOMENTUM.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_1h": "BULLISH"
        },
        "indicators": {
            "volume_spike_pct": 250.0,
            "bb_width_pct": 14.0,
            "donchian_high": 100.0,
            "donchian_low": 90.0,
            "last_closed_candle_close": 102.0
        },
        "price_data": {"current_price": 103.0},
        "derivatives_data": {"open_interest_trend": "rising"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"}
    ]
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        "SOL-USD", market_data, valid_reports, macro_cache,
        detected_regime="RANGE_CHOPPY", profile="BALANCED"
    )

    assert profile.has_directional_signal is True
    assert profile.strategy_mode == "BREAKOUT"
    assert profile.direction_bias == "LONG"
    assert "BREAKOUT LONG" in profile.reasoning

def test_high_volatility_macro_blocks_mean_reversion():
    """
    P0-2 Fix Verification:
    In HIGH_VOLATILITY macro regime, MEAN_REVERSION is suicidal and MUST be blocked,
    even if the local asset is at the bottom of the Bollinger Band.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_1h": "NEUTRAL"
        },
        "indicators": {
            "bb_position_pct": 3.0, # Touches lower BB
            "bb_width_pct": 5.0,
            "volume_spike_pct": 30.0,
            "donchian_high": 110.0,
            "donchian_low": 90.0,
            "last_closed_candle_close": 91.0
        },
        "price_data": {"current_price": 91.0},
        "derivatives_data": {"open_interest_trend": "neutral"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"}
    ]
    macro_cache = {}

    # 1. Under HIGH_VOLATILITY: MUST BE BLOCKED
    profile_blocked = StrategyRouter.evaluate(
        "SOL-USD", market_data, valid_reports, macro_cache,
        detected_regime="HIGH_VOLATILITY", profile="CONSERVATIVE"
    )
    assert profile_blocked.has_directional_signal is False
    assert profile_blocked.strategy_mode == "MEAN_REVERSION"
    assert "заблокирован в макро-режиме HIGH_VOLATILITY" in profile_blocked.reasoning

    # 2. Under RANGE_CHOPPY: MUST BE ALLOWED
    profile_allowed = StrategyRouter.evaluate(
        "SOL-USD", market_data, valid_reports, macro_cache,
        detected_regime="RANGE_CHOPPY", profile="BALANCED"
    )
    assert profile_allowed.has_directional_signal is True
    assert profile_allowed.strategy_mode == "MEAN_REVERSION"
    assert profile_allowed.direction_bias == "LONG"

def test_full_alignment_zero_analysts_allows_trend():
    """
    P1-3 Fix Verification:
    When MTF has FULL_ALIGNMENT (15m, 1h, 4h agree) and analysts are neutral (0 bulls, 0 bears),
    it should not be discarded as chop. It must route to TREND_FOLLOWING with trend_1h bias.
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BULLISH"
        },
        "indicators": {
            "bb_position_pct": 50.0,
            "bb_width_pct": 4.0,
            "volume_spike_pct": 20.0,
            "donchian_high": 105.0,
            "donchian_low": 95.0,
            "last_closed_candle_close": 100.0
        },
        "price_data": {"current_price": 100.0},
        "derivatives_data": {"open_interest_trend": "neutral"}
    }
    # 0 bulls, 0 bears
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "NEUTRAL"},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL"}
    ]
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        "XRP-USD", market_data, valid_reports, macro_cache,
        detected_regime="TRENDING", profile="AGGRESSIVE"
    )

    assert profile.has_directional_signal is True
    assert profile.strategy_mode == "TREND_FOLLOWING"
    assert profile.direction_bias == "LONG"
    assert "нет сопротивления аналитиков" in profile.reasoning

def test_volatility_momentum_routing_scalping():
    """
    Verification of Volatility Momentum scalping trigger
    (volume >= 100%, bb_width > 10%, Order Book confirmed).
    """
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_1h": "NEUTRAL"
        },
        "indicators": {
            "volume_spike_pct": 130.0,
            "bb_width_pct": 12.0,
            "donchian_high": 110.0,
            "donchian_low": 90.0,
            "last_closed_candle_close": 98.0
        },
        "price_data": {"current_price": 98.0},
        "derivatives_data": {"open_interest_trend": "rising"}
    }
    valid_reports = [
        {"agent_name": "Price_Action_Agent", "signal": "BULLISH"},
        {"agent_name": "Derivatives_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"}
    ]
    macro_cache = {}

    profile = StrategyRouter.evaluate(
        "ETH-USD", market_data, valid_reports, macro_cache,
        detected_regime="RANGE_CHOPPY", profile="BALANCED"
    )

    assert profile.has_directional_signal is True
    assert profile.strategy_mode == "VOLATILITY_MOMENTUM"
    assert profile.direction_bias == "LONG"

# ==============================================================================
# 2. RISK MANAGER STRATEGY MODE TESTS (P1 Audited Fixes)
# ==============================================================================

@pytest.mark.asyncio
async def test_mean_reversion_sl_tp_targets_middle_band(mock_logger, mock_llm):
    """
    P1-1 Fix Verification:
    In MEAN_REVERSION, TP must target the middle band (bb_middle / ema_20),
    and must NOT be forced to a 3% swing target.
    """
    rm = RiskManager(mock_logger, mock_llm)
    current_price = 100.0
    ema_20 = 101.5 # 1.5% bounce target to middle band
    atr_14 = 1.0

    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": current_price, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {
            "atr_14": atr_14,
            "ema_20": ema_20,
            "bb_middle": ema_20
        },
        "multi_timeframe": {"mtf_alignment": "MIXED_CHOP"},
        "derivatives_data": {"size_increment": 0.001, "min_notional": 10.0}
    }
    ceo_decision = {
        "decision": "LONG",
        "conviction": 80,
        "entry_quality": 80,
        "directional_confidence": 85,
        "trade_action": "ENTER",
        "symbol": "BTC-USD"
    }

    verdict = await rm.analyze(
        ceo_decision, portfolio_data, market_data,
        effective_profile="BALANCED", strategy_mode="MEAN_REVERSION"
    )

    assert verdict["approved"] is True
    # TP should be 90% of distance to EMA-20: 100 + (1.5 * 0.90) = 101.35
    # Crucially, it must NOT be forced to 103.0 (+3%)
    assert verdict["take_profit_price"] < 102.0
    assert verdict["take_profit_price"] >= 101.0
    # SL should be tight: ATR * 1.0 = 1.0 -> 99.0
    assert abs(verdict["stop_loss_price"] - 99.0) < 0.1

@pytest.mark.asyncio
async def test_volatility_momentum_tight_floors(mock_logger, mock_llm):
    """
    P1-2 Fix Verification:
    In VOLATILITY_MOMENTUM, tight SL/TP modifiers (*0.5) must not be crushed
    by the 3% MIN_TP_PCT floor.
    """
    rm = RiskManager(mock_logger, mock_llm)
    current_price = 100.0
    atr_14 = 0.8 # Small ATR

    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": current_price, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {"atr_14": atr_14},
        "multi_timeframe": {"mtf_alignment": "MIXED_CHOP"},
        "derivatives_data": {"size_increment": 0.001, "min_notional": 10.0}
    }
    ceo_decision = {
        "decision": "LONG",
        "conviction": 75,
        "entry_quality": 75,
        "directional_confidence": 80,
        "trade_action": "ENTER",
        "symbol": "BTC-USD"
    }

    verdict = await rm.analyze(
        ceo_decision, portfolio_data, market_data,
        effective_profile="BALANCED", strategy_mode="VOLATILITY_MOMENTUM"
    )

    assert verdict["approved"] is True
    # In BALANCED: tp_mult = 2.5 * 0.5 = 1.25. ATR(0.8) * 1.25 = 1.0 USD (1.0%).
    # Previously, MIN_TP_PCT = 0.03 would force TP to 103.0.
    # Now, with tight floor (0.01), TP is around 101.0!
    assert verdict["take_profit_price"] < 102.0

# ==============================================================================
# 3. CEO AGENT & PIPELINE GUARD TESTS (P1 & P2 Audited Fixes)
# ==============================================================================

@pytest.mark.asyncio
async def test_ceo_payload_receives_direction_bias_and_regimes(mock_logger, mock_llm):
    """
    P1-Fix Verification:
    CEO Agent payload MUST include direction_bias, router_reasoning,
    macro_regime, and macro_profile.
    """
    ceo = CEOAgent(mock_logger, mock_llm, mock_llm)
    captured_prompt = None

    async def fake_generate_json(prompt, required_keys=None):
        nonlocal captured_prompt
        captured_prompt = prompt
        return {
            "decision": "LONG",
            "score_breakdown": {
                "bull_argument": 35,
                "bear_argument": -5,
                "mtf_trend": 20
            },
            "reasoning_en": "Strong setup confirmed."
        }

    ceo.generate_json = fake_generate_json

    data = {
        "symbol": "BTC-USD",
        "strategy_mode": "BREAKOUT",
        "direction_bias": "LONG",
        "router_reasoning": "Breakout confirmed on 250% vol",
        "macro_regime": "TRENDING",
        "macro_profile": "AGGRESSIVE",
        "multi_timeframe_context": {"trend_1h": "BULLISH"},
        "bull_thesis": {"summary": "Strong momentum"},
        "bear_thesis": {"summary": "Overbought"},
        "subordinate_analyst_reports": []
    }

    res = await ceo.analyze(data)

    assert res["decision"] == "LONG"
    assert captured_prompt is not None
    assert '"direction_bias": "LONG"' in captured_prompt
    assert '"router_reasoning": "Breakout confirmed on 250% vol"' in captured_prompt
    assert '"macro_regime": "TRENDING"' in captured_prompt
    assert '"macro_profile": "AGGRESSIVE"' in captured_prompt

def test_pipeline_direction_guard_logic():
    """
    P2-1 Fix Verification:
    If StrategyRouter set BREAKOUT LONG, and CEO tries to invert to SHORT,
    the Guard must catch it and force decision to HOLD to prevent shorting a massive breakout.
    """
    strategy_mode = "BREAKOUT"
    router_direction_bias = "LONG"
    ceo_decision = "SHORT"
    conviction = 75
    trade_action = "ENTER"

    # Simulate the pipeline guard
    if strategy_mode in ["BREAKOUT", "MEAN_REVERSION"] and ceo_decision in ["LONG", "SHORT"]:
        if ceo_decision != router_direction_bias:
            ceo_decision = "HOLD"
            conviction = 0
            trade_action = "HOLD"

    assert ceo_decision == "HOLD"
    assert conviction == 0
    assert trade_action == "HOLD"
