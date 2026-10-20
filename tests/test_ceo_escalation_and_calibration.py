import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock

from agents.ceo_agent import CEOAgent, ScoreResult
from core.logger import TradeLogger


@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


# ==============================================================================
# 1. ESCALATION BYPASS & ECHO CHAMBER PREVENTION TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_escalation_bypassed_when_models_are_identical(mock_logger):
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-2.5-72b-instruct"
    escalation_llm = MagicMock()
    escalation_llm.model_name = "qwen/qwen-2.5-72b-instruct" # Same model!

    ceo = CEOAgent(mock_logger, primary_llm, escalation_llm)
    ceo.generate_json = AsyncMock(return_value={
        "decision": "LONG",
        "score_breakdown": {
            "bull_argument": 40,
            "bear_argument": 0,
            "mtf_trend": 30,
            "risk_penalties": 0
        },
        "reasoning_en": "Solid long setup"
    })

    payload = {
        "symbol": "BTC-USD",
        "subordinate_analyst_reports": [],
        "multi_timeframe_context": {"trend_1h": "BULLISH", "trend_4h": "BULLISH"},
        "indicators": {"rsi_14": 55.0}
    }

    result = await ceo.analyze(payload)
    # Conviction is 70% (in 60-79 range), but escalation is bypassed because models are identical!
    assert result["decision"] == "LONG"
    assert result["conviction"] == 70
    assert result["escalated"] is False
    assert result["disputed_arbitration"] is False


@pytest.mark.asyncio
async def test_escalation_bypassed_when_escalation_llm_is_none(mock_logger):
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-2.5-72b-instruct"

    ceo = CEOAgent(mock_logger, primary_llm, escalation_llm=None)
    ceo.generate_json = AsyncMock(return_value={
        "decision": "SHORT",
        "score_breakdown": {
            "bull_argument": 0,
            "bear_argument": -40,
            "mtf_trend": -30,
            "risk_penalties": 0
        },
        "reasoning_en": "Solid short setup"
    })

    payload = {
        "symbol": "ETH-USD",
        "subordinate_analyst_reports": [],
        "multi_timeframe_context": {"trend_1h": "BEARISH", "trend_4h": "BEARISH"},
        "indicators": {"rsi_14": 45.0}
    }

    result = await ceo.analyze(payload)
    assert result["decision"] == "SHORT"
    assert result["conviction"] == 70
    assert result["escalated"] is False


@pytest.mark.asyncio
async def test_escalation_bypassed_on_high_conviction(mock_logger):
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-2.5-72b-instruct"
    escalation_llm = MagicMock()
    escalation_llm.model_name = "meta-llama/llama-3.1-8b-instruct"

    ceo = CEOAgent(mock_logger, primary_llm, escalation_llm)
    ceo.generate_json = AsyncMock(return_value={
        "decision": "LONG",
        "score_breakdown": {
            "bull_argument": 45,
            "bear_argument": 0,
            "mtf_trend": 40,
            "risk_penalties": 0
        },
        "reasoning_en": "Pristine A+ long setup"
    })

    payload = {
        "symbol": "SOL-USD",
        "subordinate_analyst_reports": [],
        "multi_timeframe_context": {"trend_1h": "BULLISH", "trend_4h": "BULLISH"},
        "indicators": {"rsi_14": 55.0}
    }

    result = await ceo.analyze(payload)
    assert result["decision"] == "LONG"
    assert result["conviction"] == 85
    assert result["escalated"] is False


# ==============================================================================
# 2. ARBITRATION CONSENSUS & CONFLICT RESOLUTION TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_consensus_never_forces_reduce_size_when_conviction_below_60(mock_logger):
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-2.5-72b-instruct"
    escalation_llm = MagicMock()
    escalation_llm.model_name = "meta-llama/llama-3.1-8b-instruct"

    ceo = CEOAgent(mock_logger, primary_llm, escalation_llm)

    # Primary returns LONG with conviction 65% (DirConf 85%, Penalties -20)
    primary_resp = {
        "decision": "LONG",
        "score_breakdown": {"bull_argument": 45, "bear_argument": 0, "mtf_trend": 40, "risk_penalties": -20},
        "reasoning_en": "Primary evaluation"
    }
    # Arbitrator returns LONG but with conviction 45% (DirConf 75%, Penalties -30)
    esc_resp = {
        "decision": "LONG",
        "score_breakdown": {"bull_argument": 40, "bear_argument": 0, "mtf_trend": 35, "risk_penalties": -30},
        "reasoning_en": "Arbitrator evaluation"
    }

    call_count = 0
    async def mock_gen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return primary_resp if call_count == 1 else esc_resp

    ceo.generate_json = AsyncMock(side_effect=mock_gen)

    payload = {
        "symbol": "BTC-USD",
        "subordinate_analyst_reports": [],
        "multi_timeframe_context": {"trend_1h": "BULLISH", "trend_4h": "BULLISH"},
        "indicators": {"rsi_14": 55.0}
    }

    result = await ceo.analyze(payload)
    assert result["escalated"] is True
    # Weighted conviction: int(65*0.6 + 45*0.4) = 39 + 18 = 57% (< 60)
    assert result["conviction"] < 60
    # Rule: when conviction < 60, action MUST NOT be REDUCE_SIZE!
    assert result["trade_action"] in ["WAIT_FOR_PULLBACK", "HOLD"]
    assert result["trade_action"] != "REDUCE_SIZE"


@pytest.mark.asyncio
async def test_arbitration_disagreement_independent_risk_approval(mock_logger):
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-2.5-72b-instruct"
    escalation_llm = MagicMock()
    escalation_llm.model_name = "meta-llama/llama-3.1-8b-instruct"

    ceo = CEOAgent(mock_logger, primary_llm, escalation_llm)

    # Primary returns SHORT 70%
    primary_resp = {
        "decision": "SHORT",
        "score_breakdown": {"bull_argument": 0, "bear_argument": -40, "mtf_trend": -30, "risk_penalties": 0},
        "reasoning_en": "Primary breakdown"
    }
    # Arbitrator returns HOLD (conflict!)
    esc_resp = {
        "decision": "HOLD",
        "score_breakdown": {"bull_argument": 20, "bear_argument": -20, "mtf_trend": 0, "risk_penalties": 0},
        "reasoning_en": "Arbitrator hesitation"
    }

    call_count = 0
    async def mock_gen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return primary_resp if call_count == 1 else esc_resp

    ceo.generate_json = AsyncMock(side_effect=mock_gen)

    # 3 supporting bearish analysts, 0 opposing, aligned MTF
    payload = {
        "symbol": "ETH-USD",
        "subordinate_analyst_reports": [
            {"agent_name": "Candle_Agent", "signal": "BEARISH"},
            {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
            {"agent_name": "Order_Book_Agent", "signal": "BEARISH"}
        ],
        "multi_timeframe_context": {"trend_1h": "BEARISH", "trend_4h": "BEARISH"},
        "indicators": {"rsi_14": 40.0},
        "derivatives_data": {"funding_rate": 0.0001}
    }

    result = await ceo.analyze(payload)
    assert result["escalated"] is True
    assert result["disputed_arbitration"] is True
    # Independent risk rules approve -> SHORT maintained, conviction capped at 65%, trade_action = REDUCE_SIZE
    assert result["decision"] == "SHORT"
    assert result["conviction"] == 65
    assert result["trade_action"] == "REDUCE_SIZE"


# ==============================================================================
# 3. RISK PENALTIES, LATE ENTRY & BB EXHAUSTION TESTS
# ==============================================================================

def test_late_entry_overextension_from_ema_penalty(mock_logger):
    ceo = CEOAgent(mock_logger, MagicMock(), MagicMock())

    # Market context: price is 110, EMA-20 is 100. dist_pct = (110 - 100)/110 = 9.09% (> 1.5 * 1.5)
    market_context = {
        "price_data": {"current_price": 110.0},
        "indicators": {
            "ema_20": 100.0,
            "atr_pct": 1.5,
            "rsi_14": 60.0
        }
    }
    pen = ceo._calculate_minimum_risk_penalty("LONG", rsi=60.0, fear_greed=50.0, market_context=market_context)
    # Severe overextension from EMA-20 triggers -15 penalty
    assert pen <= -15


def test_bollinger_band_exhaustion_penalty_with_bb_pos_key(mock_logger):
    ceo = CEOAgent(mock_logger, MagicMock(), MagicMock())

    # Market context uses 'bb_pos' instead of 'bb_position_pct'
    market_context = {
        "indicators": {
            "bb_pos": 95.0, # Exhaustion at upper band
            "rsi_14": 65.0
        }
    }
    pen = ceo._calculate_minimum_risk_penalty("LONG", rsi=65.0, fear_greed=50.0, market_context=market_context)
    # Upper band extreme triggers -8 penalty
    assert pen <= -8


def test_top_level_mtf_trend_keys_properly_set_bias(mock_logger):
    ceo = CEOAgent(mock_logger, MagicMock(), MagicMock())

    # Payload with top-level trend_1h and trend_4h inside clean_mtf
    breakdown = {
        "bull_argument": 0,
        "bear_argument": 35,
        "mtf_trend": 35 # positive raw value from smaller LLM
    }
    market_context = {
        "multi_timeframe_context": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH"
        }
    }
    res = ceo._validate_and_compute_score("SHORT", breakdown, market_context=market_context)
    assert res["decision"] == "SHORT"
    assert res["math_conflict"] is False
    assert res["directional_confidence"] >= 70


# ==============================================================================
# 4. CEO ESCALATION PATCH & THREE CRITICAL FIXES (CYCLES #12, #35, #42-#47, #49)
# ==============================================================================

def test_ceo_escalation_patch_methods_direct():
    """
    Direct unit test for CEOEscalationPatch:
    - should_bypass_escalation for HOLD, ENTER, REDUCE_SIZE, and gray zone
    - validate_arbiter_math for valid math, out-of-range, and penalty inconsistencies
    - resolve_with_escalation with fallback on math discrepancy
    """
    from agents.ceo_agent import CEOEscalationPatch

    # 1. Bypass checks
    # Neutral / HOLD
    bp_hold, _ = CEOEscalationPatch.should_bypass_escalation({"action": "HOLD", "decision": "HOLD", "dir_conf": 50, "entry_quality": 30})
    assert bp_hold is True

    # High-conviction ENTER (Cycle #35/#42/#45: DirConf=80, EntryQual=75 >= 70)
    bp_enter, _ = CEOEscalationPatch.should_bypass_escalation({"action": "ENTER", "decision": "LONG", "dir_conf": 80.0, "entry_quality": 75.0})
    assert bp_enter is True

    # Solid REDUCE_SIZE (Cycle #49: DirConf=86, EntryQual=73 >= 60)
    bp_reduce, _ = CEOEscalationPatch.should_bypass_escalation({"action": "REDUCE_SIZE", "decision": "SHORT", "dir_conf": 86.0, "entry_quality": 73.0})
    assert bp_reduce is True

    # Borderline setup (WAIT_FOR_PULLBACK or gray zone)
    bp_borderline, _ = CEOEscalationPatch.should_bypass_escalation({"action": "WAIT_FOR_PULLBACK", "decision": "SHORT", "dir_conf": 70.0, "entry_quality": 55.0})
    assert bp_borderline is False

    # 2. validate_arbiter_math checks
    # Valid response
    assert CEOEscalationPatch.validate_arbiter_math({"decision": "LONG", "dir_conf": 80.0, "entry_quality": 70.0, "risk_penalty": -10.0}) is True
    # Inconsistent penalty (DirConf 80, Penalty -5, but EntryQuality 50 -> expected 75, diff > 3)
    assert CEOEscalationPatch.validate_arbiter_math({"decision": "LONG", "dir_conf": 80.0, "entry_quality": 50.0, "risk_penalty": -5.0}) is False
    # Quality > Confidence
    assert CEOEscalationPatch.validate_arbiter_math({"decision": "LONG", "dir_conf": 60.0, "entry_quality": 85.0}) is False
    # Invalid direction
    assert CEOEscalationPatch.validate_arbiter_math({"decision": "MAYBE", "dir_conf": 70.0, "entry_quality": 60.0}) is False

    # 3. resolve_with_escalation fallback on math error
    def faulty_arbiter():
        return {"decision": "HOLD", "dir_conf": 5.0, "entry_quality": 35.0, "risk_penalty": -5.0}

    primary_dec = {
        "decision": "SHORT",
        "action": "WAIT_FOR_PULLBACK",
        "trade_action": "WAIT_FOR_PULLBACK",
        "dir_conf": 75.0,
        "entry_quality": 57.0
    }
    resolved = CEOEscalationPatch.resolve_with_escalation("ETH-USD", primary_dec, {}, call_arbiter_fn=faulty_arbiter)
    # MUST strictly preserve WAIT_FOR_PULLBACK and not reset to HOLD!
    assert resolved["final_status"] == "WAIT_FOR_PULLBACK"
    assert resolved["action"] == "WAIT_FOR_PULLBACK"
    assert resolved["decision"] == "SHORT"
    assert resolved["conviction_for_risk"] == 75.0


@pytest.mark.asyncio
async def test_ceo_analyze_bypasses_8b_on_high_conviction_enter(mock_logger):
    """
    Issue 1: High conviction ENTER signals (Cycles #35, #42, #45)
    qwen-plus gave DirConf: 80-85%, EntryQuality: 75%, Action: ENTER.
    Escalation 8b must NOT be called, and final_status must be APPROVED_ENTER.
    """
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-plus"
    escalation_llm = MagicMock()
    escalation_llm.model_name = "meta-llama/llama-3.1-8b-instruct"

    ceo = CEOAgent(mock_logger, primary_llm, escalation_llm)
    # Bull 45, Bear 0, MTF 35, Penalties -5 -> DirConf 80%, EntryQual 75% -> Action ENTER
    ceo.generate_json = AsyncMock(return_value={
        "decision": "LONG",
        "score_breakdown": {
            "bull_argument": 45,
            "bear_argument": 0,
            "mtf_trend": 35,
            "risk_penalties": -5
        },
        "reasoning_en": "High conviction breakout long"
    })

    payload = {
        "symbol": "BNB-USD",
        "subordinate_analyst_reports": [],
        "multi_timeframe_context": {"trend_1h": "BULLISH", "trend_4h": "BULLISH"},
        "indicators": {"rsi_14": 55.0}
    }

    result = await ceo.analyze(payload)
    # 8B escalation must be bypassed
    assert result["escalated"] is False
    assert result["decision"] == "LONG"
    assert result["trade_action"] == "ENTER"
    assert result["final_status"] == "APPROVED_ENTER"
    assert result["conviction_for_risk"] == 80.0
    # generate_json was only called once (for Primary, 8B was NOT called)
    assert ceo.generate_json.call_count == 1


@pytest.mark.asyncio
async def test_ceo_analyze_bypasses_8b_on_confident_reduce_size(mock_logger):
    """
    Issue 3: Confident REDUCE_SIZE (Cycle #49 HYPE-USD)
    qwen-plus gave DirConf: 86%, EntryQuality: 73%, Action: REDUCE_SIZE.
    Escalation 8b must NOT be called, preventing dilution and Cohort D veto.
    """
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-plus"
    escalation_llm = MagicMock()
    escalation_llm.model_name = "meta-llama/llama-3.1-8b-instruct"

    ceo = CEOAgent(mock_logger, primary_llm, escalation_llm)
    # Bear -46, Bull 0, MTF -40 -> DirConf 86%, Penalties -13 -> EntryQual 73% (< 75 -> REDUCE_SIZE)
    ceo.generate_json = AsyncMock(return_value={
        "decision": "SHORT",
        "score_breakdown": {
            "bull_argument": 0,
            "bear_argument": -46,
            "mtf_trend": -40,
            "risk_penalties": -13
        },
        "reasoning_en": "Solid trend continuation short"
    })

    payload = {
        "symbol": "HYPE-USD",
        "subordinate_analyst_reports": [],
        "multi_timeframe_context": {"trend_1h": "BEARISH", "trend_4h": "BEARISH"},
        "indicators": {"rsi_14": 42.0}
    }

    result = await ceo.analyze(payload)
    assert result["escalated"] is False
    assert result["decision"] == "SHORT"
    assert result["trade_action"] == "REDUCE_SIZE"
    assert result["final_status"] == "REDUCE_SIZE"
    assert result["conviction_for_risk"] >= 80.0
    assert ceo.generate_json.call_count == 1


@pytest.mark.asyncio
async def test_ceo_analyze_preserves_verdict_on_arbiter_math_conflict(mock_logger):
    """
    Issue 2: Arbiter math hallucination fallback (Cycles #12, #43-#47)
    Primary proposed SHORT WAIT_FOR_PULLBACK.
    Arbiter returns broken math (score_breakdown conflict or invalid penalty math).
    System must strictly preserve WAIT_FOR_PULLBACK and NOT reset to HOLD.
    """
    primary_llm = MagicMock()
    primary_llm.model_name = "qwen/qwen-plus"
    escalation_llm = MagicMock()
    escalation_llm.model_name = "meta-llama/llama-3.1-8b-instruct"

    ceo = CEOAgent(mock_logger, primary_llm, escalation_llm)

    # Primary: Bear -40, Bull 0, MTF -35 -> DirConf 75%, Penalties -18 -> EntryQual 57% -> WAIT_FOR_PULLBACK
    primary_resp = {
        "decision": "SHORT",
        "score_breakdown": {
            "bull_argument": 0,
            "bear_argument": -40,
            "mtf_trend": -35,
            "risk_penalties": -18
        },
        "reasoning_en": "Primary short pullback setup"
    }
    # Arbiter hallucination: returns decision SHORT but bull 40, bear 0, MTF 30 (total opposite math conflict!)
    arbiter_resp = {
        "decision": "SHORT",
        "score_breakdown": {
            "bull_argument": 40,
            "bear_argument": 0,
            "mtf_trend": 30,
            "risk_penalties": 0
        },
        "reasoning_en": "Arbitrator math hallucination"
    }

    call_count = 0
    async def mock_gen(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return primary_resp if call_count == 1 else arbiter_resp

    ceo.generate_json = AsyncMock(side_effect=mock_gen)

    payload = {
        "symbol": "ZEC-USD",
        "subordinate_analyst_reports": [],
        "multi_timeframe_context": {"trend_1h": "BEARISH", "trend_4h": "BEARISH"},
        "indicators": {"rsi_14": 38.0}
    }

    result = await ceo.analyze(payload)
    assert result["escalated"] is True
    # MUST NOT reset to HOLD!
    assert result["decision"] == "SHORT"
    assert result["trade_action"] == "WAIT_FOR_PULLBACK"
    assert result["final_status"] == "WAIT_FOR_PULLBACK"
    assert result["conviction_for_risk"] == 75.0


@pytest.mark.asyncio
async def test_risk_manager_cohort_b_assigned_with_conviction_for_risk():
    """
    Issue 3 Verification in RiskManager:
    When candidate has trade_action == REDUCE_SIZE and conviction == 65%,
    but conviction_for_risk == 86.0%, it belongs to Cohort B (High Conviction, Reduced Size)
    and is NOT blocked by COHORT_D_NEGATIVE_EXPECTANCY.
    """
    from agents.risk_manager import RiskManager

    logger = MagicMock()
    llm = MagicMock()
    rm = RiskManager(logger, llm)

    ceo_decision = {
        "symbol": "HYPE-USD",
        "decision": "SHORT",
        "conviction": 73,
        "conviction_for_risk": 86.0,
        "trade_action": "REDUCE_SIZE",
        "entry_quality": 73
    }

    portfolio_data = {
        "total_usd": 100.0,
        "current_balance": 100.0,
        "available_margin": 100.0,
        "daily_drawdown_pct": 0.0,
        "recent_streak": ["WIN", "WIN"],
        "active_positions": {}
    }

    market_data = {
        "price_data": {"current_price": 25.0},
        "indicators": {"atr_14": 1.0, "rsi_14": 45.0},
        "derivatives_data": {"funding_rate": 0.0, "taker_fee_pct": 0.0005}
    }

    result = await rm.analyze(
        ceo_decision=ceo_decision,
        portfolio_data=portfolio_data,
        market_data=market_data,
        effective_profile="BALANCED",
        macro_regime="BEAR_NORMAL",
        strategy_mode="TREND_FOLLOWING"
    )

    # Must NOT be vetoed by COHORT_D_NEGATIVE_EXPECTANCY
    assert result.get("rejection_tag") != "COHORT_D_NEGATIVE_EXPECTANCY"
    assert result.get("approved") is True
    assert result.get("cohort") == "B"

