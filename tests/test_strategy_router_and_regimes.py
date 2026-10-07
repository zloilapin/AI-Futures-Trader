import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock
from core.strategy_router import StrategyRouter, StrategyProfile
from core.pipeline import TradingPipeline, AgentRegistry, ServiceRegistry
from core.models import FinalTradeDecision, FinalRiskDecision
from core.deterministic_guard import DeterministicGuard
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
    # Crucially, it must be strictly LESS than the mean target (101.5)
    assert verdict["take_profit_price"] < ema_20
    assert verdict["take_profit_price"] >= 101.0
    # SL should be tight: ATR * 1.0 = 1.0 -> 99.0
    assert abs(verdict["stop_loss_price"] - 99.0) < 0.1

@pytest.mark.asyncio
async def test_mean_reversion_short_tp_strictly_before_mean(mock_logger, mock_llm):
    """
    Mean Reversion SHORT verification:
    TP must be strictly GREATER than the mean target (ema_20 / bb_middle),
    and must not cross the mean to the downside.
    """
    rm = RiskManager(mock_logger, mock_llm)
    current_price = 100.0
    ema_20 = 98.0 # 2.0% distance down to mean
    atr_14 = 1.0

    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": current_price, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {"atr_14": atr_14, "ema_20": ema_20, "bb_middle": ema_20},
        "multi_timeframe": {"mtf_alignment": "MIXED_CHOP"},
        "derivatives_data": {"size_increment": 0.001, "min_notional": 10.0}
    }
    ceo_decision = {
        "decision": "SHORT",
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
    # TP must be strictly ABOVE the mean target for a SHORT
    assert verdict["take_profit_price"] > ema_20
    # 100 - (2.0 * 0.90) = 98.20
    assert abs(verdict["take_profit_price"] - 98.20) < 0.05

@pytest.mark.asyncio
async def test_mean_reversion_narrow_edge_vetoed(mock_logger, mock_llm):
    """
    P1 Overshoot scenario:
    If distance to mean is only 0.40% (current_price=100.0, mean=100.40),
    fees and slippage destroy any statistical edge.
    The RiskManager must veto with LOW_EDGE instead of setting an overshooting TP.
    """
    rm = RiskManager(mock_logger, mock_llm)
    current_price = 100.0
    ema_20 = 100.40 # Only 0.40% away
    atr_14 = 0.5

    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": current_price, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {"atr_14": atr_14, "ema_20": ema_20, "bb_middle": ema_20},
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

    assert verdict["approved"] is False
    assert verdict["veto_category"] == "LOW_EDGE"

@pytest.mark.asyncio
async def test_mean_reversion_price_already_past_mean_vetoed(mock_logger, mock_llm):
    """
    If price is already above the mean for a LONG, reversion upwards to the mean is impossible.
    Must veto with INVALID_MEAN_TARGET.
    """
    rm = RiskManager(mock_logger, mock_llm)
    current_price = 101.0
    ema_20 = 100.0 # Mean is BELOW current price
    atr_14 = 1.0

    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": current_price, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {"atr_14": atr_14, "ema_20": ema_20, "bb_middle": ema_20},
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

    assert verdict["approved"] is False
    assert verdict["veto_category"] == "INVALID_MEAN_TARGET"

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

# ==============================================================================
# 4. PIPELINE GUARDS & FULL INTEGRATION TESTS (P0 & P1 Audited Fixes)
# ==============================================================================

def create_test_pipeline(mock_logger, mock_llm):
    """Factory helper to build a fully wired TradingPipeline for unit and integration testing."""
    risk_manager = RiskManager(mock_logger, mock_llm)

    mock_services = MagicMock(spec=ServiceRegistry)
    mock_services.logger = mock_logger
    mock_services.fetcher = MagicMock()
    mock_services.tg_sender = MagicMock()
    mock_services.tg_sender.send_message = AsyncMock()
    mock_services.tg_sender.broadcast_to_channel = AsyncMock()
    mock_services.trading_service = MagicMock()
    mock_services.trading_service.active_positions = {}
    mock_services.trading_service.cooldown_until = 0
    mock_services.trading_service.recent_streak = []
    mock_services.trading_service.sync_with_exchange = AsyncMock()
    mock_services.trading_service.get_portfolio_summary = AsyncMock(return_value={
        "total_usd": 1000.0,
        "available_margin": 1000.0,
        "active_positions": {}
    })
    mock_services.trading_service.get_market_limits = AsyncMock(return_value={
        "size_increment": 0.001,
        "min_notional": 10.0,
        "min_size": 0.001
    })
    mock_services.trading_service.open_position = AsyncMock(return_value=True)

    mock_agents = MagicMock(spec=AgentRegistry)
    mock_agents.risk = risk_manager
    mock_agents.universe = MagicMock()
    mock_agents.universe.analyze = AsyncMock(return_value={"selected_pairs": ["BTC-USD"]})
    mock_agents.regime = MagicMock()
    mock_agents.regime.analyze = AsyncMock(return_value={"regime": "RANGE_CHOPPY", "recommended_profile": "BALANCED", "reasoning_en": "Balanced chop"})
    mock_agents.scanner = MagicMock()
    mock_agents.scanner.analyze = AsyncMock(return_value={"proceed": True, "reasoning": "Valid"})

    analyst_names = {
        "candle": "Candle_Agent",
        "orderbook": "Order_Book_Agent",
        "oi_funding": "OI_Funding_Agent",
        "news": "News_Agent",
        "indicator": "Indicator_Agent"
    }
    for analyst_attr, agent_name in analyst_names.items():
        analyst = MagicMock()
        analyst.name = agent_name
        analyst.analyze = AsyncMock(return_value={"signal": "BULLISH", "confidence": 80, "status": "COMPLETED"})
        setattr(mock_agents, analyst_attr, analyst)

    mock_agents.bull = MagicMock()
    mock_agents.bull.analyze = AsyncMock(return_value={"summary": "Bull debate thesis"})
    mock_agents.bear = MagicMock()
    mock_agents.bear.analyze = AsyncMock(return_value={"summary": "Bear debate thesis"})
    mock_agents.ceo = MagicMock()
    mock_agents.sentinel = MagicMock()
    mock_agents.sentinel.analyze = AsyncMock(return_value={})
    mock_agents.telegram = MagicMock()
    mock_agents.telegram.analyze = AsyncMock(return_value={"message": "Trade summary"})
    mock_agents.reflector = MagicMock()
    mock_agents.reflector.get_lessons = MagicMock(return_value=[])
    mock_agents.memory = MagicMock()
    mock_agents.memory.get_recent_context = MagicMock(return_value=[])
    mock_agents.memory.save_cycle = MagicMock()

    pipeline = TradingPipeline(mock_agents, mock_services, "nado")
    return pipeline, mock_agents, mock_services


def test_pipeline_direction_guard_logic(mock_logger, mock_llm):
    """
    P2-1 & P0-2 Fix Verification:
    Tests the REAL TradingPipeline.apply_pipeline_guards_and_sync() method.
    If StrategyRouter set BREAKOUT LONG, and CEO tries to invert to SHORT,
    the Guard in TradingPipeline must catch it, force decision to HOLD, and sync ceo_verdict.
    """
    pipeline, agents, services = create_test_pipeline(mock_logger, mock_llm)
    strategy_profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="BREAKOUT",
        direction_bias="LONG",
        reasoning="Breakout setup with strong volume"
    )
    ceo_verdict = {
        "decision": "SHORT",
        "conviction": 75,
        "trade_action": "ENTER",
        "reasoning_en": "Bearish counter-thesis convinced CEO"
    }

    # ACT: Run real production pipeline method
    decision, conviction, trade_action, min_conv = pipeline.apply_pipeline_guards_and_sync(
        strategy_profile, ceo_verdict, "BALANCED", symbol="BTC-USD"
    )

    # ASSERT: The production pipeline guarded and synchronized the state
    assert decision == "HOLD"
    assert conviction == 0
    assert trade_action == "HOLD"
    assert ceo_verdict["decision"] == "HOLD"
    assert ceo_verdict["conviction"] == 0
    assert ceo_verdict["trade_action"] == "HOLD"
    assert ceo_verdict["hold_category"] == "STRATEGY_GUARD_VETO"
    assert "Strategy Guard" in ceo_verdict["reasoning_en"]


@pytest.mark.asyncio
async def test_pipeline_syncs_ceo_verdict_for_volatility_momentum_pullback(mock_logger, mock_llm):
    """
    P0-1 Fix Verification:
    Tests TradingPipeline.apply_pipeline_guards_and_sync() followed by real RiskManager.analyze().
    When VOLATILITY_MOMENTUM converts WAIT_FOR_PULLBACK to ENTER, ceo_verdict must be synced
    so that RiskManager does NOT veto with WAIT_FOR_PULLBACK.
    """
    pipeline, agents, services = create_test_pipeline(mock_logger, mock_llm)
    strategy_profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="VOLATILITY_MOMENTUM",
        direction_bias="LONG",
        reasoning="15m volatility momentum"
    )
    ceo_verdict = {
        "decision": "LONG",
        "conviction": 70,
        "entry_quality": 70,
        "directional_confidence": 75,
        "trade_action": "WAIT_FOR_PULLBACK",
        "symbol": "BTC-USD"
    }

    # ACT: Run real production pipeline method
    decision, conviction, trade_action, min_conv = pipeline.apply_pipeline_guards_and_sync(
        strategy_profile, ceo_verdict, "BALANCED", symbol="BTC-USD"
    )

    # ASSERT: Pipeline converted trade_action and synchronized ceo_verdict
    assert decision == "LONG"
    assert trade_action == "ENTER"
    assert ceo_verdict["trade_action"] == "ENTER"

    # Pass directly into real RiskManager to verify approval
    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": 100.0, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {"atr_14": 1.0},
        "multi_timeframe": {"mtf_alignment": "MIXED_CHOP"},
        "derivatives_data": {"size_increment": 0.001, "min_notional": 10.0}
    }
    risk_verdict = await agents.risk.analyze(
        ceo_verdict, portfolio_data, market_data,
        effective_profile="BALANCED", strategy_mode="VOLATILITY_MOMENTUM"
    )

    assert risk_verdict["approved"] is True
    assert risk_verdict.get("veto_category") is None


@pytest.mark.asyncio
async def test_full_pipeline_cycle_volatility_momentum_pullback_to_risk_execution(mock_logger, mock_llm, monkeypatch):
    """
    P0 Full End-to-End Integration Test:
    CEO (WAIT_FOR_PULLBACK)
      ↓
    pipeline.run_cycle()
      ↓
    VOLATILITY_MOMENTUM override (trade_action -> ENTER)
      ↓
    ceo_verdict synced in pipeline
      ↓
    Real RiskManager.analyze (approved: True, not vetoed)
      ↓
    trading_service.open_position (CALLED!)
    """
    pipeline, agents, services = create_test_pipeline(mock_logger, mock_llm)

    candles = [
        {"open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 10.0}
        for _ in range(20)
    ]
    market_data = {
        "symbol": "BTC-USD",
        "price_data": {
            "current_price": 100.0,
            "candles_20": candles,
            "ohlcv_1h": [{"volume": 100}] * 10,
            "ohlcv_15m": [{"volume": 100, "close": 100.0, "open": 99.0}] * 10
        },
        "order_book_data": {
            "best_bid": 99.95,
            "best_ask": 100.05,
            "spread_pct": 0.05,
            "bid_volume": 100.0,
            "ask_volume": 100.0
        },
        "indicators": {
            "atr_14": 1.0,
            "ema_20": 100.0,
            "bb_middle": 100.0,
            "bb_upper": 105.0,
            "bb_lower": 95.0,
            "bb_width_pct": 12.0,
            "volume_spike_pct": 120.0,
            "vol_15m_ratio": 2.5
        },
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_15m": "BULLISH",
            "trend_1h": "NEUTRAL",
            "trend_4h": "NEUTRAL",
            "vol_15m_ratio": 2.5
        },
        "derivatives_data": {
            "size_increment": 0.001,
            "min_notional": 10.0,
            "min_size": 0.001,
            "funding_rate": 0.0001,
            "open_interest_trend": "rising"
        }
    }

    services.fetcher.fetch_all_market_data = AsyncMock(return_value=market_data)
    services.fetcher.fetch_active_perps = AsyncMock(return_value=[{"symbol": "BTC-USD", "volume_24h": 50000000}])

    # CEO Agent originally outputs WAIT_FOR_PULLBACK
    ceo_verdict = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 70,
        "entry_quality": 60,
        "directional_confidence": 75,
        "trade_action": "WAIT_FOR_PULLBACK",
        "reasoning_en": "Fast momentum breakout, but waiting for slight pullback"
    }
    agents.ceo.analyze = AsyncMock(return_value=ceo_verdict)

    # Patch sleep so the test completes instantaneously
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    # EXECUTE FULL REAL PIPELINE CYCLE
    await pipeline.run_cycle(1, force_scan=True)

    # VERIFY: open_position was called with LONG
    # If the P0 bug existed (ceo_verdict not synced), RiskManager would veto with WAIT_FOR_PULLBACK,
    # and open_position would never be called!
    assert services.trading_service.open_position.called is True
    call_args = services.trading_service.open_position.call_args[1]
    assert call_args["symbol"] == "BTC-USD"
    assert call_args["direction"] == "LONG"
    assert call_args["notional_usd"] > 0
    assert ceo_verdict["trade_action"] == "ENTER"


@pytest.mark.asyncio
async def test_full_pipeline_cycle_direction_guard_blocks_trade(mock_logger, mock_llm, monkeypatch):
    """
    Direction Guard Full End-to-End Integration Test:
    StrategyRouter (BREAKOUT LONG)
      ↓
    CEO Agent (attempts SHORT)
      ↓
    pipeline Direction Guard (converts to HOLD, hold_category=STRATEGY_GUARD_VETO)
      ↓
    Execution Gate (BLOCKED, open_position NEVER called)
    """
    pipeline, agents, services = create_test_pipeline(mock_logger, mock_llm)

    candles = [
        {"open": 102.0, "high": 104.0, "low": 101.0, "close": 103.0, "volume": 25.0}
        for _ in range(20)
    ]
    market_data = {
        "symbol": "BTC-USD",
        "price_data": {
            "current_price": 103.0,
            "candles_20": candles,
            "ohlcv_1h": [{"volume": 100}] * 10
        },
        "order_book_data": {
            "best_bid": 102.95,
            "best_ask": 103.05,
            "spread_pct": 0.05,
            "bid_volume": 100.0,
            "ask_volume": 100.0
        },
        "indicators": {
            "volume_spike_pct": 250.0,
            "bb_width_pct": 14.0,
            "donchian_high": 100.0,
            "donchian_low": 90.0,
            "last_closed_candle_close": 102.0,
            "atr_14": 1.0
        },
        "multi_timeframe": {
            "mtf_alignment": "MIXED_CHOP",
            "trend_1h": "BULLISH"
        },
        "derivatives_data": {
            "size_increment": 0.001,
            "min_notional": 10.0,
            "min_size": 0.001,
            "open_interest_trend": "rising",
            "funding_rate": 0.0001
        }
    }

    services.fetcher.fetch_all_market_data = AsyncMock(return_value=market_data)
    services.fetcher.fetch_active_perps = AsyncMock(return_value=[{"symbol": "BTC-USD", "volume_24h": 50000000}])

    # CEO tries to contradict Breakout LONG with a SHORT decision
    ceo_verdict = {
        "symbol": "BTC-USD",
        "decision": "SHORT",
        "conviction": 80,
        "entry_quality": 80,
        "directional_confidence": 85,
        "trade_action": "ENTER",
        "reasoning_en": "Counter-trend bear argument convinced CEO"
    }
    agents.ceo.analyze = AsyncMock(return_value=ceo_verdict)

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await pipeline.run_cycle(1, force_scan=True)

    # Guard must block the trade: open_position must NOT be called
    assert services.trading_service.open_position.called is False
    assert ceo_verdict["decision"] == "HOLD"
    assert ceo_verdict["hold_category"] == "STRATEGY_GUARD_VETO"


# ==============================================================================
# 5. UNIFIED PIPELINE CONTRACT TESTS (Deterministic Guard -> RiskManager -> Execution)
# ==============================================================================

def test_deterministic_guard_produces_canonical_final_trade_decision(mock_logger, mock_llm):
    """
    Contract Test 1:
    DeterministicGuard evaluates raw CEO proposal against strategy and market rules,
    producing an immutable FinalTradeDecision with explicit actionability and rejection tags.
    """
    risk_manager = RiskManager(mock_logger, mock_llm)

    # Scenario A: Direction bias contradiction in Breakout
    profile_breakout = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="BREAKOUT",
        direction_bias="LONG",
        reasoning="Donchian breakout long"
    )
    ceo_proposal_contra = {
        "decision": "SHORT",
        "conviction": 85,
        "trade_action": "ENTER",
        "reasoning_en": "Counter-trend idea"
    }
    decision_a = DeterministicGuard.evaluate(
        strategy_profile=profile_breakout,
        ceo_proposal=ceo_proposal_contra,
        profile="BALANCED",
        risk_manager=risk_manager,
        symbol="BTC-USD"
    )
    assert isinstance(decision_a, FinalTradeDecision)
    assert decision_a.decision == "HOLD"
    assert decision_a.conviction == 0
    assert decision_a.trade_action == "HOLD"
    assert decision_a.is_actionable is False
    assert decision_a.rejection_tag == "STRATEGY_GUARD_VETO"
    assert decision_a.guard_status == "BLOCKED"

    # Scenario B: Volatility Momentum Pullback Override
    profile_vol = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="VOLATILITY_MOMENTUM",
        direction_bias="LONG",
        reasoning="15m fast momentum"
    )
    ceo_proposal_pb = {
        "decision": "LONG",
        "conviction": 70,
        "trade_action": "WAIT_FOR_PULLBACK",
        "entry_quality": 65,
        "directional_confidence": 75
    }
    decision_b = DeterministicGuard.evaluate(
        strategy_profile=profile_vol,
        ceo_proposal=ceo_proposal_pb,
        profile="BALANCED",
        risk_manager=risk_manager,
        symbol="ETH-USD"
    )
    assert decision_b.decision == "LONG"
    assert decision_b.trade_action == "ENTER"
    assert decision_b.is_actionable is True
    assert decision_b.guard_status == "PASSED"

    # Scenario C: Extreme Funding Rate Gate
    market_data_extreme_funding = {
        "derivatives_data": {"funding_rate": 0.0008} # 0.08% > 0.05%
    }
    decision_c = DeterministicGuard.evaluate(
        strategy_profile=profile_vol,
        ceo_proposal={"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        profile="BALANCED",
        risk_manager=risk_manager,
        market_data=market_data_extreme_funding,
        symbol="SOL-USD"
    )
    assert decision_c.decision == "LONG" # Decision itself preserved
    assert decision_c.is_actionable is False # But marked non-actionable
    assert decision_c.rejection_tag == "FUNDING_VETO"
    assert decision_c.guard_status == "BLOCKED"

    # Scenario D: Backward compatibility unpacking
    dec, conv, act, m_conv = decision_b
    assert dec == "LONG"
    assert conv == 70
    assert act == "ENTER"
    assert m_conv == 65


@pytest.mark.asyncio
async def test_risk_manager_strict_contract_binary_approve_or_veto_only(mock_logger, mock_llm):
    """
    Contract Test 2:
    RiskManager receives FinalTradeDecision.
    It can ONLY APPROVE or VETO. It NEVER mutates 'decision'.
    """
    risk_manager = RiskManager(mock_logger, mock_llm)

    # 1. Non-actionable candidate passed to RiskManager -> strictly VETO
    blocked_trade = FinalTradeDecision(
        symbol="BTC-USD",
        decision="LONG",
        conviction=80,
        trade_action="ENTER",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        min_conviction=70,
        is_actionable=False,
        guard_status="BLOCKED",
        guard_reason="Extreme funding rate",
        rejection_tag="FUNDING_VETO"
    )
    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": 100.0, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {"atr_14": 1.0},
        "derivatives_data": {"size_increment": 0.001}
    }
    risk_res_1 = await risk_manager.analyze(blocked_trade, portfolio_data, market_data)
    assert risk_res_1["approved"] is False
    assert risk_res_1["veto_category"] == "FUNDING_VETO"
    assert blocked_trade.decision == "LONG" # NEVER mutated

    # 2. Actionable candidate with healthy portfolio -> strictly APPROVE
    valid_trade = FinalTradeDecision(
        symbol="BTC-USD",
        decision="LONG",
        conviction=75,
        trade_action="ENTER",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        min_conviction=70,
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="All checks passed."
    )
    risk_res_2 = await risk_manager.analyze(valid_trade, portfolio_data, market_data)
    assert risk_res_2["approved"] is True
    assert risk_res_2.get("veto_category") is None
    assert risk_res_2["notional_size_usd"] > 0
    assert valid_trade.decision == "LONG" # NEVER mutated


@pytest.mark.asyncio
async def test_pipeline_contract_immutable_decision_throughout_execution(mock_logger, mock_llm, monkeypatch):
    """
    Contract Test 3:
    Full pipeline execution preserves the strict contract:
    - FinalTradeDecision.decision is passed to open_position.
    - If exchange rejects order, risk_verdict['approved'] remains True (RiskManager approved it),
      while risk_verdict['execution_status'] is marked 'REJECTED_BY_EXCHANGE'.
    """
    pipeline, agents, services = create_test_pipeline(mock_logger, mock_llm)

    market_data = {
        "symbol": "BTC-USD",
        "price_data": {
            "current_price": 100.0,
            "candles_20": [{"open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 10.0}] * 20,
            "ohlcv_1h": [{"volume": 100}] * 10,
            "ohlcv_15m": [{"volume": 100, "close": 100.0, "open": 99.0}] * 10
        },
        "order_book_data": {"best_bid": 99.95, "best_ask": 100.05, "spread_pct": 0.05, "bid_volume": 100.0, "ask_volume": 100.0},
        "indicators": {"atr_14": 1.0, "ema_20": 100.0, "bb_middle": 100.0, "bb_upper": 105.0, "bb_lower": 95.0, "bb_width_pct": 12.0, "volume_spike_pct": 120.0, "vol_15m_ratio": 2.5},
        "multi_timeframe": {"mtf_alignment": "MIXED_CHOP", "trend_15m": "BULLISH", "trend_1h": "NEUTRAL", "trend_4h": "NEUTRAL", "vol_15m_ratio": 2.5},
        "derivatives_data": {"size_increment": 0.001, "min_notional": 10.0, "min_size": 0.001, "funding_rate": 0.0001, "open_interest_trend": "rising"}
    }
    services.fetcher.fetch_all_market_data = AsyncMock(return_value=market_data)
    services.fetcher.fetch_active_perps = AsyncMock(return_value=[{"symbol": "BTC-USD", "volume_24h": 50000000}])

    # Simulate exchange rejection on open_position
    services.trading_service.open_position = AsyncMock(return_value=False)

    ceo_verdict = {
        "symbol": "BTC-USD",
        "decision": "LONG",
        "conviction": 75,
        "entry_quality": 75,
        "directional_confidence": 80,
        "trade_action": "ENTER",
        "reasoning_en": "Strong setup"
    }
    agents.ceo.analyze = AsyncMock(return_value=ceo_verdict)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await pipeline.run_cycle(1, force_scan=True)

    # open_position was called with direction="LONG"
    assert services.trading_service.open_position.called is True
    call_args = services.trading_service.open_position.call_args[1]
    assert call_args["direction"] == "LONG"

    # Memory saved the cycle with approved risk assessment and REJECTED_BY_EXCHANGE execution status
    assert agents.memory.save_cycle.called is True
    saved_cycle = agents.memory.save_cycle.call_args[0][0]
    assert saved_cycle["ceo_decision"]["decision"] == "LONG"
    assert saved_cycle["risk_assessment"]["approved"] is True # RiskManager DID approve!
    assert saved_cycle["risk_assessment"]["execution_status"] == "REJECTED_BY_EXCHANGE"


def test_guard_cannot_originate_trade_when_ceo_chooses_hold(mock_logger, mock_llm):
    """
    Audit Check 1:
    Verifies Guard cannot take a trade decision instead of CEO.
    Even with strong strategy bias (e.g. BREAKOUT LONG), if CEO decides HOLD,
    DeterministicGuard MUST NOT convert it to LONG/SHORT. is_actionable MUST be False.
    """
    risk_manager = RiskManager(mock_logger, mock_llm)
    profile_breakout = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="BREAKOUT",
        direction_bias="LONG",
        reasoning="Strong breakout setup"
    )
    ceo_hold = {
        "decision": "HOLD",
        "conviction": 40,
        "trade_action": "HOLD",
        "reasoning_en": "Market too uncertain"
    }
    decision = DeterministicGuard.evaluate(
        strategy_profile=profile_breakout,
        ceo_proposal=ceo_hold,
        profile="BALANCED",
        risk_manager=risk_manager,
        symbol="BTC-USD"
    )
    assert decision.decision == "HOLD"
    assert decision.is_actionable is False
    assert decision.guard_status == "HOLD"


def test_final_trade_decision_is_strictly_immutable(mock_logger, mock_llm):
    """
    Audit Check 2:
    Verifies that FinalTradeDecision is frozen=True at runtime.
    Any attempt to mutate 'decision' raises FrozenInstanceError.
    """
    from dataclasses import FrozenInstanceError
    trade_decision = FinalTradeDecision(
        symbol="ETH-USD",
        decision="LONG",
        conviction=75,
        trade_action="ENTER",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        min_conviction=70,
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="OK"
    )
    with pytest.raises(FrozenInstanceError):
        trade_decision.decision = "SHORT"


@pytest.mark.asyncio
async def test_execution_and_risk_strictly_blocks_wait_and_hold(mock_logger, mock_llm):
    """
    Audit Check 3:
    Verifies neither RiskManager nor Execution can reinterpret WAIT or HOLD as a trade.
    - RiskManager refuses WAIT_FOR_PULLBACK and HOLD.
    - NadoTradingService.open_position immediately returns False if direction is 'HOLD' or 'WAIT'.
    """
    risk_manager = RiskManager(mock_logger, mock_llm)
    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": 100.0, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {"atr_14": 1.0},
        "derivatives_data": {"size_increment": 0.001}
    }

    # 1. RiskManager with trade_action WAIT_FOR_PULLBACK
    wait_decision = FinalTradeDecision(
        symbol="BTC-USD",
        decision="LONG",
        conviction=75,
        trade_action="WAIT_FOR_PULLBACK",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        min_conviction=70,
        is_actionable=False,
        guard_status="BLOCKED",
        guard_reason="Wait",
        rejection_tag="WAIT_FOR_PULLBACK"
    )
    risk_res = await risk_manager.analyze(wait_decision, portfolio_data, market_data)
    assert risk_res["approved"] is False

    # 2. RiskManager with decision HOLD
    hold_decision = FinalTradeDecision(
        symbol="BTC-USD",
        decision="HOLD",
        conviction=0,
        trade_action="HOLD",
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        min_conviction=70,
        is_actionable=False,
        guard_status="HOLD",
        guard_reason="Hold",
        rejection_tag="CEO_HOLD"
    )
    risk_res_hold = await risk_manager.analyze(hold_decision, portfolio_data, market_data)
    assert risk_res_hold["approved"] is False

    # 3. NadoTradingService.open_position rejects non-directional values
    from services.nado_trading_service import NadoTradingService
    service = NadoTradingService(nado_client=MagicMock())
    service.is_connected = True
    service.product_map = {"BTC": 1, "BTC-USD": 1}
    service._get_market_parameters = MagicMock(return_value={"size_increment_x18": 10**15, "price_increment_x18": 10**15})

    # Passing "HOLD" must be immediately rejected with False, NOT executed as a short!
    res_hold = await service.open_position("BTC-USD", "HOLD", 100.0, 100.0, 105.0, 95.0, 5)
    assert res_hold is False

    # Passing "WAIT_FOR_PULLBACK" must also be rejected
    res_wait = await service.open_position("BTC-USD", "WAIT_FOR_PULLBACK", 100.0, 100.0, 105.0, 95.0, 5)
    assert res_wait is False


