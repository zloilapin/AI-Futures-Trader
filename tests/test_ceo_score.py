import pytest
from unittest.mock import MagicMock
from agents.ceo_agent import CEOAgent

def get_mock_ceo():
    logger = MagicMock()
    primary_llm = MagicMock()
    escalation_llm = MagicMock()
    return CEOAgent(logger, primary_llm, escalation_llm)

def test_high_conviction_setup():
    ceo = get_mock_ceo()
    # Bull 45, Bear 0, MTF 35 -> 80% (High Conviction Long)
    decision, conviction = ceo._validate_and_compute_score("LONG", {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 35
    })
    assert decision == "LONG"
    assert conviction == 80

def test_medium_conviction_with_minor_counter():
    ceo = get_mock_ceo()
    # Bull 45, Bear -10, MTF 30 -> 65% (Eligible for Gemini escalation)
    decision, conviction = ceo._validate_and_compute_score("LONG", {
        "bull_argument": 45,
        "bear_argument": -10,
        "mtf_trend": 30
    })
    assert decision == "LONG"
    assert conviction == 65

def test_choppy_market_low_conviction():
    ceo = get_mock_ceo()
    # Bull 40, Bear -10, MTF 0 (Choppy, no alignment) -> 30% (< 60%, correctly filtered to HOLD)
    decision, conviction = ceo._validate_and_compute_score("LONG", {
        "bull_argument": 40,
        "bear_argument": -10,
        "mtf_trend": 0
    })
    assert decision == "LONG"
    assert conviction == 30

def test_direct_conflict_neutral():
    ceo = get_mock_ceo()
    # Bull 35, Bear -35, MTF 0 -> 0%
    decision, conviction = ceo._validate_and_compute_score("LONG", {
        "bull_argument": 35,
        "bear_argument": -35,
        "mtf_trend": 0
    })
    assert conviction == 0

def test_short_setup():
    ceo = get_mock_ceo()
    # Bear -45, Bull 5, MTF -30 -> -70 -> SHORT 70%
    decision, conviction = ceo._validate_and_compute_score("SHORT", {
        "bull_argument": 5,
        "bear_argument": -45,
        "mtf_trend": -30
    })
    assert decision == "SHORT"
    assert conviction == 70

def test_math_hallucination_override():
    ceo = get_mock_ceo()
    # Model says LONG, but numbers are net bearish (Bull 10, Bear -40, MTF -10 -> -40)
    decision, conviction = ceo._validate_and_compute_score("LONG", {
        "bull_argument": 10,
        "bear_argument": -40,
        "mtf_trend": -10
    })
    assert decision == "HOLD"
    assert conviction == 0

def test_bch_regression_extreme_rsi_and_sentiment():
    """
    Mandatory BCH regression test:
    Trend: Strong bullish (Bull +45, Bear 0, MTF +40)
    RSI: 96.8, Fear & Greed: 74
    Ensures that strong trend does NOT mask extreme overbought risks.
    """
    ceo = get_mock_ceo()
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 40
    }
    market_context = {
        "indicators": {"rsi_14": 96.8},
        "news_data": {"fear_and_greed_index": 74}
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)
    
    assert result["decision"] == "LONG"
    assert result["directional_confidence"] >= 80
    assert result["entry_quality"] <= 70
    assert result["entry_quality"] < result["directional_confidence"]
    assert result["risk_penalties"] == -20 # -15 (RSI > 95) + -5 (F&G >= 70)
    assert result["conviction"] == result["entry_quality"]
    assert result["trade_action"] == "WAIT_FOR_PULLBACK"

def test_cumulative_risk_penalty_cap():
    """
    Ensures that multiple independent penalties cannot exceed the MAX_TOTAL_RISK_PENALTY (-30).
    """
    ceo = get_mock_ceo()
    breakdown = {
        "bull_argument": 50,
        "bear_argument": 0,
        "mtf_trend": 40,
        "risk_penalties": {
            "rsi_extreme": -20,
            "sentiment_euphoria": -15,
            "resistance": -10
        }
    }
    result = ceo._validate_and_compute_score("LONG", breakdown)
    assert result["risk_penalties"] == -30  # Capped at -30
    assert result["directional_confidence"] == 90
    assert result["entry_quality"] == 60

def test_deterministic_floor_overrides_llm_zero_penalty():
    """
    Deterministic safety: If LLM hallucinates 0 penalty when RSI is 92.5,
    the deterministic floor enforces minimum -12 penalty.
    """
    ceo = get_mock_ceo()
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 40,
        "risk_penalties": 0
    }
    market_context = {
        "indicators": {"rsi_14": 92.5}
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)
    assert result["risk_penalties"] <= -12
    assert result["entry_quality"] == 85 + result["risk_penalties"]

def test_short_oversold_bounce_risk():
    """
    Tests SHORT setups where extreme oversold RSI (< 10) and extreme Fear (<= 20)
    correctly apply bounce risk penalties.
    """
    ceo = get_mock_ceo()
    breakdown = {
        "bull_argument": 0,
        "bear_argument": -45,
        "mtf_trend": -40
    }
    market_context = {
        "indicators": {"rsi_14": 8.0},
        "news_data": {"fear_and_greed_index": 18}
    }
    result = ceo._validate_and_compute_score("SHORT", breakdown, market_context=market_context)
    assert result["decision"] == "SHORT"
    assert result["directional_confidence"] == 85
    assert result["risk_penalties"] <= -20 # -12 (RSI < 10) + -10 (F&G <= 20) = -22
    assert result["entry_quality"] <= 65
    assert result["trade_action"] == "WAIT_FOR_PULLBACK"

def test_direction_determined_by_sign_not_abs():
    """
    Ensures that direction is strictly determined by sign, never abs().
    """
    ceo = get_mock_ceo()
    breakdown = {
        "bull_argument": 10,
        "bear_argument": -50,
        "mtf_trend": 10
    }
    result = ceo._validate_and_compute_score("SHORT", breakdown)
    assert result["decision"] == "SHORT"
    assert result["directional_confidence"] == 30

def test_pipeline_passes_rsi_to_ceo_context():
    """
    Test that real MarketDataService data structure (rsi_14 and sentiment_score)
    is correctly extracted by CEOAgent and produces appropriate penalties.
    """
    ceo = get_mock_ceo()
    # Real shape as returned by MarketDataService
    market_data = {
        "symbol": "BCH-USD",
        "indicators": {
            "rsi_14": 96.8,
            "ema_20": 250.0,
            "macd": 1.5
        },
        "news_data": {
            "symbol": "BCH-USD",
            "sentiment_score": 74.0,
            "latest_event": "Market Sentiment: Greed (Fear & Greed Index: 74)"
        }
    }
    # Pass via ceo_payload structure as done in pipeline.py
    ceo_payload = {
        "symbol": "BCH-USD",
        "indicators": market_data["indicators"],
        "news_data": market_data["news_data"],
        "market_data": market_data
    }
    
    rsi, fear_greed = ceo._extract_market_metrics(ceo_payload)
    assert rsi == 96.8
    assert fear_greed == 74.0
    
    # Test scoring with this real payload
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 40
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context=ceo_payload)
    assert result["decision"] == "LONG"
    assert result["directional_confidence"] == 85
    assert result["risk_penalties"] == -20  # -15 (RSI > 95) + -5 (F&G 74)
    assert result["entry_quality"] == 65
    assert result["trade_action"] == "WAIT_FOR_PULLBACK"

def test_rsi_directional_conditioning_no_cross_penalty():
    """
    Ensures penalties are direction-conditioned:
    - Overbought RSI does NOT penalize SHORT.
    - Oversold RSI does NOT penalize LONG.
    - But extreme conditions DO penalize trend-following entries.
    """
    ceo = get_mock_ceo()
    
    # Case A: Shorting with RSI = 96.8, F&G = 74 (Overbought market, Short entry)
    # The deterministic counter-risk floor for SHORT is 0 (no oversold penalty).
    pen_short = ceo._calculate_minimum_risk_penalty("SHORT", rsi=96.8, fear_greed=74.0)
    assert pen_short == 0, f"Expected 0 penalty for SHORT on overbought market, got {pen_short}"
    
    # Case B: Longing with RSI = 8.0, F&G = 18 (Oversold market, Long entry)
    # The deterministic counter-risk floor for LONG is 0 (no overbought penalty).
    pen_long = ceo._calculate_minimum_risk_penalty("LONG", rsi=8.0, fear_greed=18.0)
    assert pen_long == 0, f"Expected 0 penalty for LONG on oversold market, got {pen_long}"
    
    # Case C: Shorting into extreme oversold bottom (RSI = 8.0, F&G = 18)
    pen_short_oversold = ceo._calculate_minimum_risk_penalty("SHORT", rsi=8.0, fear_greed=18.0)
    assert pen_short_oversold == -22  # -12 (RSI < 10) + -10 (F&G <= 20)
    
    # Case D: Longing into extreme overbought top (RSI = 96.8, F&G = 74)
    pen_long_overbought = ceo._calculate_minimum_risk_penalty("LONG", rsi=96.8, fear_greed=74.0)
    assert pen_long_overbought == -20  # -15 (RSI > 95) + -5 (F&G >= 70)

@pytest.mark.asyncio
@pytest.mark.parametrize("entry_quality,action,profile,expected_approved,expected_veto", [
    (85, "ENTER", "CONSERVATIVE", True, None),
    (65, "WAIT_FOR_PULLBACK", "BALANCED", False, "WAIT_FOR_PULLBACK"),
    (65, "WAIT_FOR_PULLBACK", "AGGRESSIVE", False, "WAIT_FOR_PULLBACK"),
    (72, "REDUCE_SIZE", "BALANCED", True, None),
    (72, "REDUCE_SIZE", "AGGRESSIVE", True, None),
    (55, "HOLD", "BALANCED", False, "LOW_CONFIDENCE"),
    (55, "HOLD", "AGGRESSIVE", False, "LOW_CONFIDENCE"),
])
async def test_profile_matrix_execution_and_sizing(entry_quality, action, profile, expected_approved, expected_veto):
    """
    User-specified Profile Matrix test:
    Verifies that WAIT_FOR_PULLBACK is strictly respected across all profiles (including AGGRESSIVE),
    and REDUCE_SIZE downscales position sizing.
    """
    from agents.risk_manager import RiskManager
    rm = RiskManager(MagicMock(), MagicMock())
    portfolio_data = {"total_usd": 1000.0, "available_margin": 1000.0, "active_positions": {}}
    market_data = {
        "price_data": {"current_price": 250.0, "ohlcv_1h": [{"volume": 100}] * 10},
        "indicators": {"atr_14": 5.0},
        "multi_timeframe": {"mtf_alignment": "FULL_ALIGNMENT"},
        "derivatives_data": {"size_increment": 0.001, "min_notional": 10.0}
    }
    ceo_decision = {
        "decision": "LONG",
        "conviction": entry_quality,
        "entry_quality": entry_quality,
        "directional_confidence": 85,
        "trade_action": action,
        "symbol": "BCH-USD"
    }
    verdict = await rm.analyze(ceo_decision, portfolio_data, market_data, effective_profile=profile)
    assert verdict["approved"] == expected_approved
    if expected_veto:
        assert verdict["veto_category"] == expected_veto
    if expected_approved and action == "REDUCE_SIZE":
        # Compare notional size vs action == "ENTER" to ensure it was downsized by action_mult
        ceo_enter = dict(ceo_decision, trade_action="ENTER")
        enter_verdict = await rm.analyze(ceo_enter, portfolio_data, market_data, effective_profile=profile)
        assert verdict["notional_size_usd"] < enter_verdict["notional_size_usd"]

