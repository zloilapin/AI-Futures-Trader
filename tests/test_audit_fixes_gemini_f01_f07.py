import pytest
import asyncio
import json
import time
from unittest.mock import MagicMock, AsyncMock, patch

from core.strategy_router import StrategyRouter, StrategyProfile
from core.models import FinalTradeDecision
from agents.ceo_agent import CEOAgent
from agents.risk_manager import RiskManager
from core.logger import TradeLogger

@pytest.fixture
def mock_logger():
    return MagicMock(spec=TradeLogger)

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.model_name = "Primary"
    return llm

# ==============================================================================
# F-01 & F-02: StrategyRouter Pullback Retest Vetoes
# ==============================================================================
def test_f01_transition_retest_veto_in_router():
    """F-01: Prohibit TREND_FOLLOWING pullback retest in TRANSITION regime early."""
    market_data = {
        "is_pullback_retest": True,
        "pullback_retest_direction": "LONG",
        "indicators": {"bb_pos": 50.0, "atr_14": 1.0}
    }
    profile = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=[],
        macro_cache={},
        detected_regime="TRANSITION"
    )
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "TRANSITION_RETEST_VETO"
    assert "TRANSITION" in profile.reasoning

def test_f02_falling_knife_retest_veto_in_router():
    """F-02: Anti-knife catching rejects retest LONG if technical bears >= 3."""
    market_data = {
        "is_pullback_retest": True,
        "pullback_retest_direction": "LONG",
        "indicators": {"bb_pos": 50.0, "atr_14": 1.0}
    }
    bear_reports = [
        {"agent_name": "Trend_Agent", "signal": "BEARISH"},
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
    ]
    profile = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=bear_reports,
        macro_cache={},
        detected_regime="TRENDING"
    )
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "FALLING_KNIFE_VETO"
    assert "Ловля падающих ножей" in profile.reasoning

# ==============================================================================
# F-02: CEO Agent Does Not Resurrect HOLD on Retest
# ==============================================================================
@pytest.mark.asyncio
async def test_f02_ceo_does_not_resurrect_hold_on_retest(mock_logger, mock_llm):
    """F-02: If CEO judge decides HOLD on retest, it must NOT be resurrected into market entry."""
    ceo = CEOAgent(mock_logger, mock_llm, mock_llm)
    
    # Simulate LLM response proposing HOLD (e.g. broken structure)
    mock_llm.generate = AsyncMock(return_value=json.dumps({
        "decision": "HOLD",
        "score_breakdown": {"bull_argument": 10, "bear_argument": 10},
        "reasoning_en": "Support level smashed through"
    }))
    
    payload = {
        "symbol": "SOL-USD",
        "strategy_mode": "TREND_FOLLOWING",
        "direction_bias": "LONG",
        "is_pullback_retest": True,
        "pullback_retest_direction": "LONG",
        "pullback_context": {"direction": "LONG", "directional_confidence": 80},
        "indicators": {"ema_20": 100.0, "atr_14": 2.0},
        "price_data": {"current_price": 95.0},
        "subordinate_analyst_reports": []
    }
    
    verdict = await ceo.analyze(payload)
    assert verdict["decision"] == "HOLD"
    assert verdict["trade_action"] == "HOLD"
    assert verdict.get("conviction", 0) <= 60

    # Also directly verify _validate_and_compute_score doesn't resurrect HOLD to LONG
    score_res = ceo._validate_and_compute_score("HOLD", {"bull_argument": 10, "bear_argument": 10}, market_context=payload)
    assert score_res["decision"] == "HOLD"
    assert score_res["trade_action"] == "HOLD"

# ==============================================================================
# F-03: Macro Leaders Alignment Veto
# ==============================================================================
def test_f03_macro_leaders_bearish_collapse_vetos_altcoin_long():
    """F-03: When both BTC and ETH are in FULL_ALIGNMENT BEARISH, altcoin LONG is blocked."""
    market_data = {
        "multi_timeframe": {
            "mtf_alignment": "FULL_ALIGNMENT",
            "trend_1h": "BULLISH"
        },
        "indicators": {
            "bb_pos": 50.0,
            "donchian_high": 100.0,
            "volume_spike_pct": 50.0
        },
        "price_data": {"current_price": 90.0}
    }
    bull_reports = [
        {"agent_name": "Trend_Agent", "signal": "BULLISH"},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH"}
    ]
    macro_cache = {
        "BTC-USD": {
            "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT", "trend_1h": "BEARISH"}
        },
        "ETH-USD": {
            "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT", "trend_1h": "BEARISH"}
        }
    }
    profile = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=bull_reports,
        macro_cache=macro_cache,
        detected_regime="TRENDING"
    )
    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "MACRO_LEADERS_VETO"
    assert "FULL_ALIGNMENT BEARISH" in profile.reasoning

# ==============================================================================
# F-04: Nado Trigger Optimization $101 Bump Preserved in Stage 7
# ==============================================================================
@pytest.mark.asyncio
async def test_f04_nado_trigger_101_bump_preserved_against_target_margin(mock_logger, mock_llm):
    """F-04: $101 bump for native on-chain triggers is preserved when usable margin permits."""
    rm = RiskManager(mock_logger, mock_llm, default_macro_regime="TRENDING")
    rm.get_bayesian_strategy_multiplier = MagicMock(return_value=(1.0, 0.55, "mock"))
    
    # Balance $95.00. target_margin_pct=0.10 -> target_margin_usd = $9.50.
    # At leverage 10x, old max_safe_notional = 9.50 * 10 = $95.00, which would have chopped $101 back down!
    # But usable_margin = min(95.0*0.8, 95.0*0.20) = $19.00, which easily covers $101/10 = $10.10 margin.
    portfolio = {
        "total_usd": 95.00,
        "available_margin": 95.00,
        "active_positions": {},
        "recent_streak": []
    }
    market = {
        "price_data": {"current_price": 100.00},
        "indicators": {"atr_14": 0.67, "ema_20": 100.00, "bb_middle": 100.00},
        "order_book_data": {"spread_pct": 0.01},
        "derivatives_data": {
            "taker_fee_pct": 0.0005,
            "funding_rate": 0.0001,
            "size_increment": 0.01,
            "min_size": 0.01,
            "min_notional": 0.0
        }
    }
    ceo_enter = {
        "symbol": "SOL-USD",
        "decision": "LONG",
        "conviction": 80,
        "entry_quality": 80,
        "trade_action": "REDUCE_SIZE"
    }
    res = await rm.analyze(ceo_enter, portfolio, market, effective_profile="BALANCED", macro_regime="TRENDING")
    assert res["approved"] is True
    # Notional should remain at $101.00 (or >= 100.0) to pass exchange limit!
    assert res["notional_size_usd"] >= 100.0, f"Expected notional >= 100.0, got {res['notional_size_usd']}"

# ==============================================================================
# F-05: RiskManager OHLCV 1H Fallback and Young Candle Shield
# ==============================================================================
@pytest.mark.asyncio
async def test_f05_risk_manager_ohlcv_1h_fallback_to_multi_timeframe(mock_logger, mock_llm):
    """F-05: RiskManager reads 1h candles from multi_timeframe.tf_1h.candles_20 and protects young candle."""
    rm = RiskManager(mock_logger, mock_llm, default_macro_regime="TRENDING")
    
    # 10 candles: 9 preceding with 100 volume, latest young candle at second 5 with 1 volume.
    # v_prev = 100, avg = 100 -> effective_v = max(1, 100) = 100 >= avg -> NO penalty (volume_mult = 1.0)
    candles = [{"volume": 100.0}] * 9 + [{"volume": 1.0}]
    market = {
        "price_data": {"current_price": 100.0},
        "indicators": {"atr_14": 2.0, "ema_20": 100.0, "bb_middle": 100.0},
        "multi_timeframe": {
            "tf_1h": {"candles_20": candles}
        },
        "order_book_data": {"spread_pct": 0.01},
        "derivatives_data": {"taker_fee_pct": 0.0005, "size_increment": 0.01, "min_notional": 0.0}
    }
    portfolio = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    ceo_enter = {"symbol": "BTC-USD", "decision": "LONG", "conviction": 85, "trade_action": "ENTER"}
    
    res = await rm.analyze(ceo_enter, portfolio, market, effective_profile="BALANCED", macro_regime="TRENDING")
    applied = res.get("applied_multipliers", {})
    assert applied.get("quality_mult") == 1.0, f"Expected quality_mult=1.0 without young candle penalty, got {applied.get('quality_mult')}"

# ==============================================================================
# F-07: check_pullback_watchlist Ignores Risk-Free BE Slots
# ==============================================================================
@pytest.mark.asyncio
async def test_f07_check_pullback_watchlist_ignores_risk_free_positions():
    """F-07: Pipeline check_pullback_watchlist does not count BE position against directional cluster cap."""
    from core.pipeline import TradingPipeline
    from unittest.mock import MagicMock
    
    mock_agents = MagicMock()
    mock_services = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "nado")
    pipeline._cycle_running = False
    
    # Active positions: 2 SHORTs, BUT 1 is in BREAK_EVEN!
    pipeline.services.trading_service.active_positions = {
        "BTC-USD": {
            "symbol": "BTC-USD",
            "direction": "SHORT",
            "entry_price": 60000.0,
            "sl_price": 59000.0,  # in profit / BE!
            "protection_state": "SOFTWARE_BREAK_EVEN"
        },
        "ETH-USD": {
            "symbol": "ETH-USD",
            "direction": "SHORT",
            "entry_price": 3000.0,
            "sl_price": 3050.0,  # at risk
            "protection_state": "ACTIVE"
        }
    }
    
    # Add SOL-USD SHORT to watchlist (pullback reached target)
    pipeline._pullback_watchlist = {
        "SOL-USD": {
            "symbol": "SOL-USD",
            "direction": "SHORT",
            "signal_price": 100.0,
            "target_pullback_price": 102.0,
            "invalidation_price": 105.0,
            "created_at": time.time(),
            "expires_at": time.time() + 1800,
            "ttl_seconds": 1800
        }
    }
    
    # Mock market price for SOL at target 102.0 via fetcher
    pipeline.services.fetcher.fetch_ohlcv = AsyncMock(return_value={"current_price": 102.0})
    
    triggered, sym, reason = await pipeline.check_pullback_watchlist()
    assert triggered is True
    assert sym == "SOL-USD"
    assert "retest" in reason.lower()
