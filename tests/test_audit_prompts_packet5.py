# tests/test_audit_prompts_packet5.py
import pytest
import os
import json
from unittest.mock import MagicMock, AsyncMock

from agents.ceo_agent import CEOAgent
from agents.bull_agent import BullAgent
from agents.bear_agent import BearAgent
from services.market_data_service import MarketDataService


def test_p01_sign_rule_and_mtf_reconciliation_short():
    """
    P-01: Verify Sign Rule in prompts and MTF sign reconciliation in _validate_and_compute_score.
    Ensures that an LLM returning positive mtf_trend on a valid SHORT does not trigger
    a false Math Hallucination hard veto when bear argument won.
    """
    # 1. Prompt check
    with open("python/prompts/ceo_prompt.txt", "r", encoding="utf-8") as f:
        ceo_prompt = f.read()
    assert "For SHORT trades, MTF_Trend MUST be NEGATIVE" in ceo_prompt
    assert "For LONG trades, MTF_Trend MUST be POSITIVE" in ceo_prompt
    assert '"bull_argument": "<int 0..50' in ceo_prompt

    # 2. Agent escalation prompt check
    logger_mock = MagicMock()
    llm_mock = MagicMock()
    llm_mock.model_name = "Primary-LLM"
    esc_llm_mock = MagicMock()
    esc_llm_mock.model_name = "Escalation-LLM"
    ceo = CEOAgent(logger_mock, llm_mock, esc_llm_mock)

    # 3. Test _validate_and_compute_score with positive mtf on SHORT in UNKNOWN mtf context
    breakdown = {
        "bull_argument": 5,
        "bear_argument": -40,
        "mtf_trend": 35, # Positive magnitude accidentally provided
        "risk_penalties": {
            "price_extension": 0,
            "macro_sentiment": 0,
            "market_structure": 0,
            "liquidity_friction": 0,
            "total": 0
        }
    }
    # Market context with unknown/missing MTF
    market_context = {"symbol": "BTC-USD"}
    score_res = ceo._validate_and_compute_score("SHORT", breakdown, market_context=market_context)

    # Should NOT trigger math_conflict and should approve SHORT!
    assert not score_res.get("math_conflict"), "False math_conflict triggered on SHORT with positive mtf!"
    assert score_res["decision"] == "SHORT"
    assert score_res["directional_confidence"] > 50


def test_p02_pullback_retest_addon_stabilization_rules():
    """
    P-02: Verify Pullback Retest Addon does NOT command ignoring counter-trend candles,
    and requires checking stabilization before approving execution.
    """
    with open("python/agents/ceo_agent.py", "r", encoding="utf-8") as f:
        ceo_code = f.read()

    # Must NOT command blind knife-catching
    assert "Do NOT penalize short-term 15m counter-trend candles" not in ceo_code

    # Must require stabilization check and allow HOLD on violent momentum
    assert "Check if the pullback is stabilizing" in ceo_code
    assert "violent momentum AGAINST" in ceo_code
    assert "output HOLD!" in ceo_code
    assert "Approve execution with ENTER or REDUCE_SIZE ONLY if price shows rejection/stabilization" in ceo_code


def test_p03_prompts_clean_syntax_and_nado_reference():
    """
    P-03: Verify all 4 prompt files contain clean syntax, no Python string artifacts,
    and reference Nado DEX instead of Kraken Futures.
    """
    files = [
        "python/prompts/candle_prompt.txt",
        "python/prompts/indicator_prompt.txt",
        "python/prompts/order_book_prompt.txt",
        "python/prompts/oi_funding_prompt.txt"
    ]

    for f_path in files:
        with open(f_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Zero python concatenation artifacts
        assert "'\n            '" not in content, f"Stray python artifact found in {f_path}"
        assert "'\r\n            '" not in content, f"Stray python artifact found in {f_path}"
        assert '"}CRITICAL:' not in content, f"Malformed closing brace found in {f_path}"
        assert "Kraken Futures" not in content, f"Stray Kraken Futures found in {f_path}"

    # Specifically check Nado DEX in candle prompt
    with open("python/prompts/candle_prompt.txt", "r", encoding="utf-8") as f:
        c_text = f.read()
    assert "Nado DEX" in c_text


@pytest.mark.asyncio
async def test_p04_key_levels_in_debate_payload_and_prompts():
    """
    P-04: Verify Bull and Bear agents receive key_levels and prompts instruct
    using key_levels for targets and invalidations without hardcoded numbers.
    """
    # 1. Prompts check
    with open("python/prompts/bull_prompt.txt", "r", encoding="utf-8") as f:
        bull_p = f.read()
    assert "12.5" not in bull_p
    assert "11.2" not in bull_p
    assert "key_levels" in bull_p
    assert "potential_target MUST be a realistic target price ABOVE current_price" in bull_p

    with open("python/prompts/bear_prompt.txt", "r", encoding="utf-8") as f:
        bear_p = f.read()
    assert "10.5" not in bear_p
    assert "12.1" not in bear_p
    assert "key_levels" in bear_p
    assert "potential_target MUST be a realistic target price BELOW current_price" in bear_p

    # 2. BullAgent and BearAgent payload forwarding
    logger_mock = MagicMock()
    llm_mock = MagicMock()
    llm_mock.generate = AsyncMock(return_value=json.dumps({
        "thesis_score": 8,
        "bullish_arguments": ["Strong support holding"],
        "potential_target": 72000.0,
        "invalidation_level": 68000.0,
        "summary": "Bullish thesis summary"
    }))

    bull = BullAgent(logger_mock, llm_mock)
    payload = {
        "symbol": "BTC-USD",
        "key_levels": {
            "current_price": 70000.0,
            "ema_20": 69500.0,
            "donchian_high": 71500.0,
            "donchian_low": 68500.0,
            "bb_upper": 72000.0,
            "bb_lower": 68000.0,
            "atr_14": 1000.0
        },
        "multi_timeframe_context": {},
        "analyst_reports": []
    }
    await bull.analyze(payload)
    
    call_args = llm_mock.generate.call_args[0][0]
    assert '"key_levels"' in call_args
    assert '"current_price": 70000.0' in call_args


def test_p05_thresholds_alignment_and_action_ranges():
    """
    P-05: Verify Breakout volume threshold alignment and disambiguated action ranges.
    Entry Quality 65-74 with high DirConf should result in REDUCE_SIZE, not WAIT_FOR_PULLBACK.
    """
    # 1. Prompt check
    with open("python/prompts/ceo_prompt.txt", "r", encoding="utf-8") as f:
        ceo_p = f.read()
    assert "spike (>=150%)" in ceo_p
    assert "REDUCE_SIZE (if directional confidence >= 70% and entry quality is 65-74%)" in ceo_p
    assert "WAIT_FOR_PULLBACK (if directional confidence >= 75%, but entry quality < 65%" in ceo_p

    # 2. Agent action range testing
    logger_mock = MagicMock()
    llm_mock = MagicMock()
    esc_llm_mock = MagicMock()
    ceo = CEOAgent(logger_mock, llm_mock, esc_llm_mock)

    # Moderate entry quality: DirConf = 80, Penalty = -12 -> Entry Quality = 68
    # Under old logic: Quality < 70 and DirConf >= 75 forced WAIT_FOR_PULLBACK!
    # Under new aligned logic: Quality 68 is within [65..74], so action is REDUCE_SIZE!
    breakdown_reduce = {
        "bull_argument": 40,
        "bear_argument": -5,
        "mtf_trend": 40,
        "risk_penalties": {
            "price_extension": -5,
            "macro_sentiment": -4,
            "market_structure": -3,
            "liquidity_friction": 0,
            "total": -12
        }
    }
    res_reduce = ceo._validate_and_compute_score("LONG", breakdown_reduce, market_context={"symbol": "ETH-USD"})
    assert res_reduce["directional_confidence"] >= 75
    assert 65 <= res_reduce["entry_quality"] < 75
    assert res_reduce["trade_action"] == "REDUCE_SIZE", f"Expected REDUCE_SIZE, got {res_reduce['trade_action']}"

    # Severe price extension: Penalty = -20 -> Entry Quality = 60 (< 65)
    breakdown_pullback = {
        "bull_argument": 40,
        "bear_argument": -5,
        "mtf_trend": 40,
        "risk_penalties": {
            "price_extension": -15, # severe price extension
            "macro_sentiment": -5,
            "market_structure": 0,
            "liquidity_friction": 0,
            "total": -20
        }
    }
    res_pullback = ceo._validate_and_compute_score("LONG", breakdown_pullback, market_context={"symbol": "ETH-USD"})
    assert res_pullback["directional_confidence"] >= 75
    assert res_pullback["entry_quality"] < 65
    assert res_pullback["trade_action"] == "WAIT_FOR_PULLBACK", f"Expected WAIT_FOR_PULLBACK, got {res_pullback['trade_action']}"


@pytest.mark.asyncio
async def test_p06_universe_agent_prompt_and_market_data():
    """
    P-06: Verify Universe Agent prompt focuses on volumeQuote and DEX market presence,
    and MarketDataService.fetch_active_perps provides complete structured records.
    """
    with open("python/prompts/universe_prompt.txt", "r", encoding="utf-8") as f:
        u_prompt = f.read()
    assert "Prioritize assets with high volumeQuote" in u_prompt
    assert "Core Majors" in u_prompt

    # Test MarketDataService fallback structure
    mds = MarketDataService()
    perps = await mds.fetch_active_perps(limit=5)
    assert len(perps) > 0
    for p in perps:
        assert "symbol" in p
        assert "volumeQuote" in p
        assert "change_24h" in p
