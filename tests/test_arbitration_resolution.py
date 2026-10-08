import pytest
from unittest.mock import MagicMock, AsyncMock

from agents.ceo_agent import CEOAgent, ScoreResult
from core.deterministic_guard import DeterministicGuard
from core.strategy_router import StrategyProfile
from core.models import FinalTradeDecision


def test_deterministic_sign_normalization_short():
    """
    Test that positive trend values provided by smaller LLMs in SHORT setups
    are deterministically recognized as trend strength in favor of SHORT,
    and DO NOT trigger a false 'Math hallucination'.
    """
    logger = MagicMock()
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-2.5-72b-instruct"
    esc_llm = MagicMock()
    esc_llm.model_name = "meta-llama/llama-3.1-8b-instruct"

    ceo = CEOAgent(logger, primary_llm, esc_llm)

    # Breakdown where smaller model gave positive mtf_trend: 25 and bear_argument: 25 for a SHORT
    breakdown = {
        "bull_argument": 0,
        "bear_argument": 35,
        "mtf_trend": 35,
        "risk_penalties": {"total": -5}
    }
    market_context = {
        "direction_bias": "SHORT",
        "multi_timeframe_context": {
            "tf_1h": {"trend": "bearish"},
            "tf_4h": {"trend": "bearish"}
        }
    }

    res = ceo._validate_and_compute_score("SHORT", breakdown, market_context=market_context)

    assert res["decision"] == "SHORT"
    assert res["math_conflict"] is False
    assert res["directional_confidence"] >= 50
    assert res["trade_action"] in ["REDUCE_SIZE", "ENTER", "WAIT_FOR_PULLBACK"]


def test_independent_risk_rules_approval():
    """
    Test that _evaluate_independent_risk_rules approves a SHORT when at least 2
    syndicate analysts support, <=1 opposes, and MTF trend is aligned.
    """
    logger = MagicMock()
    ceo = CEOAgent(logger, MagicMock(), MagicMock())

    analyst_reports = [
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
        {"agent_name": "Candle_Agent", "signal": "BEARISH"},
        {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL"},
        {"agent_name": "OI_Funding_Agent", "signal": "NEUTRAL"}
    ]
    market_context = {
        "multi_timeframe_context": {
            "tf_1h": {"trend": "bearish"},
            "tf_4h": {"trend": "bearish"}
        },
        "indicators": {"rsi_14": 42.0},
        "derivatives_data": {"funding_rate": 0.0001}
    }

    is_approved, reason, metrics = ceo._evaluate_independent_risk_rules(analyst_reports, market_context, "SHORT")

    assert is_approved is True
    assert "Аналитики: 2 vs 0" in reason


def test_independent_risk_rules_rejection_on_split():
    """
    Test that _evaluate_independent_risk_rules rejects when analysts are split (not enough consensus).
    """
    logger = MagicMock()
    ceo = CEOAgent(logger, MagicMock(), MagicMock())

    analyst_reports = [
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
        {"agent_name": "Candle_Agent", "signal": "BULLISH"},
        {"agent_name": "News_Agent", "signal": "BULLISH"}
    ]
    market_context = {
        "multi_timeframe_context": {
            "tf_1h": {"trend": "neutral"},
            "tf_4h": {"trend": "neutral"}
        }
    }

    is_approved, reason, metrics = ceo._evaluate_independent_risk_rules(analyst_reports, market_context, "SHORT")

    assert is_approved is False
    assert "Недостаточно поддержки" in reason or "Слишком высокое сопротивление" in reason


def test_deterministic_guard_caps_disputed_trade():
    """
    Test that DeterministicGuard ensures disputed arbitration trades are capped at REDUCE_SIZE.
    """
    risk_manager = MagicMock()
    risk_manager._get_profile_rules.return_value = {"min_conviction": 65}

    profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        reasoning="Test trend following"
    )

    ceo_proposal = {
        "decision": "SHORT",
        "conviction": 65,
        "directional_confidence": 75,
        "entry_quality": 65,
        "trade_action": "ENTER", # Raw proposal asked for ENTER
        "disputed_arbitration": True # But it was disputed!
    }

    decision = DeterministicGuard.evaluate(
        strategy_profile=profile,
        ceo_proposal=ceo_proposal,
        profile="AGGRESSIVE",
        risk_manager=risk_manager
    )

    assert decision.is_actionable is True
    assert decision.decision == "SHORT"
    assert decision.trade_action == "REDUCE_SIZE" # Capped to REDUCE_SIZE!


def test_deterministic_guard_trend_pullback_conversion():
    """
    Test that in strong TREND_FOLLOWING with high conviction, WAIT_FOR_PULLBACK is converted
    to REDUCE_SIZE to prevent missing the entire trend.
    """
    risk_manager = MagicMock()
    risk_manager._get_profile_rules.return_value = {"min_conviction": 65}

    profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        reasoning="Test trend following"
    )

    ceo_proposal = {
        "decision": "SHORT",
        "conviction": 70,
        "directional_confidence": 75,
        "entry_quality": 70,
        "trade_action": "WAIT_FOR_PULLBACK"
    }

    decision = DeterministicGuard.evaluate(
        strategy_profile=profile,
        ceo_proposal=ceo_proposal,
        profile="AGGRESSIVE",
        risk_manager=risk_manager
    )

    # Rule: Уменьшенный объём сам по себе не делает плохой вход безопасным.
    # Без подтверждения пробоя WAIT_FOR_PULLBACK отправляется в PULLBACK_WATCHLIST и не входит автоматически.
    assert decision.is_actionable is False
    assert decision.decision == "SHORT"
    assert decision.trade_action == "WAIT_FOR_PULLBACK"
    assert decision.guard_status == "PULLBACK_WATCHLIST"


@pytest.mark.asyncio
async def test_ceo_hold_bypasses_escalation_without_unbound_error():
    """
    Test that when Primary CEO returns HOLD, escalation is cleanly bypassed
    and the return dictionary contains 'disputed_arbitration': False without UnboundLocalError.
    """
    logger = MagicMock()
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-plus"
    esc_llm = MagicMock()
    esc_llm.model_name = "meta-llama/llama-3.1-8b-instruct"

    ceo = CEOAgent(logger, primary_llm, esc_llm)
    ceo.generate_json = AsyncMock(return_value={
        "decision": "HOLD",
        "score_breakdown": {
            "bull_argument": 10,
            "bear_argument": 10,
            "mtf_trend": 0,
            "risk_penalties": {"total": 0}
        },
        "reasoning_en": "No directional edge, staying in cash.",
        "consensus_summary": "Hold."
    })

    payload = {
        "symbol": "SOL-USD",
        "subordinate_analyst_reports": [],
        "multi_timeframe_context": {},
        "historical_context": {}
    }

    verdict = await ceo.analyze(payload)
    assert verdict["decision"] == "HOLD"
    assert verdict["escalated"] is False
    assert verdict["disputed_arbitration"] is False
    assert "conviction" in verdict
    assert "entry_quality" in verdict
