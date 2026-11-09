import pytest
from unittest.mock import MagicMock
from agents.regime_agent import RegimeAgent
from agents.ceo_agent import CEOAgent
from agents.risk_manager import RiskManager
from services.nado_trading_service import NadoTradingService

@pytest.fixture
def mock_logger():
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger

@pytest.fixture
def mock_llm_client():
    client = MagicMock()
    client.model_name = "test-model"
    return client

# 1. Scratch Guard Unit Tests
def test_scratch_guard_classification(mock_logger):
    service = NadoTradingService.__new__(NadoTradingService)
    service.win_count = 5
    service.loss_count = 5
    service.recent_streak = ["WIN", "LOSS"]
    service._save_state = MagicMock()
    service.logger = mock_logger

    # Case A: Micro-losses within [-0.22R, +0.15R] must be SCRATCH
    res1 = service._evaluate_and_update_streak(net_pnl=-0.0038, realized_r=-0.006, planned_risk=0.50, symbol="ETH-USD")
    assert res1 == "SCRATCH"
    assert service.recent_streak == ["WIN", "LOSS"]
    assert service.loss_count == 5

    res2 = service._evaluate_and_update_streak(net_pnl=-0.0372, realized_r=-0.196, planned_risk=0.19, symbol="ZEC-USD")
    assert res2 == "SCRATCH"
    assert service.recent_streak == ["WIN", "LOSS"]
    assert service.loss_count == 5

    res3 = service._evaluate_and_update_streak(net_pnl=+0.0456, realized_r=+0.117, planned_risk=0.39, symbol="SOL-USD")
    assert res3 == "SCRATCH"
    assert service.recent_streak == ["WIN", "LOSS"]
    assert service.win_count == 5

    # Case B: Real stop loss (< -0.22R) must be LOSS
    res4 = service._evaluate_and_update_streak(net_pnl=-0.4725, realized_r=-1.074, planned_risk=0.44, symbol="ETH-USD")
    assert res4 == "LOSS"
    assert service.recent_streak[-1] == "LOSS"
    assert service.loss_count == 6

    # Case C: Real target win (> +0.15R) must be WIN
    res5 = service._evaluate_and_update_streak(net_pnl=+0.5564, realized_r=+0.320, planned_risk=1.74, symbol="ETH-USD")
    assert res5 == "WIN"
    assert service.recent_streak[-1] == "WIN"
    assert service.win_count == 6


# 2. Regime Hysteresis Unit Tests
def test_regime_agent_hysteresis_damper(mock_logger, mock_llm_client):
    regime_agent = RegimeAgent(mock_logger, mock_llm_client)

    # Initial state: RANGE_CHOPPY
    regime1, profile1 = regime_agent.classify_regime_from_scores(
        trend_score=15.0, range_score=70.0, vol_score=30.0, btc_atr=0.8
    )
    assert regime1 == "RANGE_CHOPPY"
    assert profile1 == "BALANCED"

    # Sudden 1-candle spike to trend: must be dampened to TRANSITION / BALANCED
    regime2, profile2 = regime_agent.classify_regime_from_scores(
        trend_score=95.0, range_score=10.0, vol_score=30.0, btc_atr=0.8,
        previous_regime="RANGE_CHOPPY", consecutive_trend_count=0
    )
    assert regime2 == "TRANSITION"
    assert profile2 == "BALANCED"

    # Second consecutive cycle of trend: confirmed to TRENDING / AGGRESSIVE
    regime3, profile3 = regime_agent.classify_regime_from_scores(
        trend_score=95.0, range_score=10.0, vol_score=30.0, btc_atr=0.8,
        previous_regime="TRANSITION", consecutive_trend_count=1
    )
    assert regime3 == "TRENDING"
    assert profile3 == "AGGRESSIVE"


# 3. CEO Math Hallucination Hard Veto Tests
def test_ceo_math_hallucination_hard_veto(mock_logger, mock_llm_client):
    ceo = CEOAgent(mock_logger, mock_llm_client, mock_llm_client)

    # LLM proposes LONG, but breakdown shows bears scored 33.5 vs bulls 25.0 (Cycle #9 case)
    breakdown = {
        "bull_arguments_score": 25.0,
        "bear_arguments_score": 33.5,
        "multi_timeframe_score": 0.0
    }
    market_context = {
        "is_pullback_retest": True,
        "pullback_retest_direction": "LONG",
        "pullback_context": {"direction": "LONG", "directional_confidence": 75}
    }

    score_res = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)
    assert score_res["math_conflict"] is True
    assert score_res["decision"] == "HOLD"
    assert score_res["trade_action"] == "HOLD"
    assert score_res["directional_confidence"] == 0
    assert score_res["entry_quality"] == 0


# 4. Risk Manager Floor and Pre-Debate Viability Tests
def test_risk_manager_pre_debate_viability(mock_logger, mock_llm_client):
    risk_mgr = RiskManager(mock_logger, mock_llm_client)

    # State A: Normal 9.5% drawdown, 5 losses in streak -> High-Conviction Floor viable
    portfolio_normal = {
        "total_balance": 90.5,
        "peak_balance": 100.0,
        "daily_drawdown_pct": 0.095,
        "recent_streak": ["LOSS", "LOSS", "LOSS", "LOSS", "LOSS"]
    }
    is_viable, reason = risk_mgr.is_pre_debate_viable(portfolio_normal, profile_name="AGGRESSIVE")
    assert is_viable is True

    # State B: Deep drawdown 13.0% (>= 12.0%) + 5 losses -> suppressed mathematically, saves tokens
    portfolio_deep = {
        "total_balance": 87.0,
        "peak_balance": 100.0,
        "daily_drawdown_pct": 0.13,
        "recent_streak": ["LOSS", "LOSS", "LOSS", "LOSS", "LOSS"]
    }
    is_viable_deep, reason_deep = risk_mgr.is_pre_debate_viable(portfolio_deep, profile_name="AGGRESSIVE")
    assert is_viable_deep is False
    assert "mathematically suppress" in reason_deep
