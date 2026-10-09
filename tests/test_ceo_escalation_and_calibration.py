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
