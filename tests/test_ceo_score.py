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
