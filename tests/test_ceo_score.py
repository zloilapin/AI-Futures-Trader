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
    (85, "REDUCE_SIZE", "BALANCED", True, None),
    (85, "REDUCE_SIZE", "AGGRESSIVE", True, None),
    (72, "REDUCE_SIZE", "BALANCED", False, "COHORT_D_NEGATIVE_EXPECTANCY"),
    (72, "REDUCE_SIZE", "AGGRESSIVE", False, "COHORT_D_NEGATIVE_EXPECTANCY"),
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
    rm = RiskManager(MagicMock(), MagicMock(), default_macro_regime="RANGE_CHOPPY")
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


def test_categorized_risk_penalties_anti_double_counting():
    """
    Verifies that:
    1. Correlated price extension metrics (RSI, EMA distance, Bollinger position)
       do NOT triple-punish the setup (anti-double-counting).
    2. Independent risk categories (Price Extension + Macro Sentiment)
       accumulate cleanly.
    3. Structured risk_breakdown is populated.
    """
    ceo = get_mock_ceo()
    # Market context with extreme RSI (96.8 -> -15), EMA overextension (2.0% -> -15), and BB position (95% -> -8)
    market_context = {
        "indicators": {
            "rsi_14": 96.8,
            "ema_20": 100.0,
            "current_price": 102.5,  # 2.5% over EMA-20
            "atr_pct": 1.0,
            "bb_position_pct": 95.0
        },
        "news_data": {
            "fear_and_greed_index": 85.0  # Macro euphoria -> -10
        }
    }
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 35,
        "risk_penalties": 0
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)

    # Price extension took the most severe single metric (-15) rather than summing -15+-15+-8 = -38
    assert result["risk_breakdown"]["price_extension"] == -15
    # Macro sentiment is evaluated independently (-10)
    assert result["risk_breakdown"]["macro_sentiment"] == -10
    # Total is sum of independent categories: -15 + -10 = -25
    assert result["risk_penalties"] == -25
    assert result["risk_breakdown"]["total"] == -25
    # Directional confidence: 45 + 35 = 80
    assert result["directional_confidence"] == 80
    # Entry quality: 80 - 25 = 55 (< 60) -> trade_action WAIT_FOR_PULLBACK or HOLD
    assert result["entry_quality"] == 55
    assert result["trade_action"] in ["WAIT_FOR_PULLBACK", "HOLD"]


def test_mtf_conflict_zero_contribution():
    """
    Audit Point 1:
    Verifies that when 1H and 4H timeframes conflict (e.g. 1H Bullish vs 4H Bearish),
    the MTF contribution is strictly 0.0 points.
    LLM's positive mtf_trend (+30) must NOT override the conflict and turn it into BULLISH!
    """
    ceo = get_mock_ceo()
    market_context = {
        "multi_timeframe_context": {
            "tf_1h": {"trend": "bullish"},
            "tf_4h": {"trend": "bearish"}
        }
    }
    # Bull 45, Bear 0, LLM mtf_trend 30
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 30
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)

    # MTF must be 0, so directional confidence is ONLY from bull_argument (45)
    # If the bug was present, it would be 45 + 30 = 75!
    assert result["directional_confidence"] == 45
    assert result["trade_action"] == "HOLD"  # 45 < 60 directional confidence -> trade_action HOLD


def test_mtf_unknown_fallback_to_llm():
    """
    Audit Point 1:
    When timeframe data is UNKNOWN (no 1H or 4H data in market context),
    fallback to explicit sign of LLM's mtf_trend is allowed.
    """
    ceo = get_mock_ceo()
    market_context = {}  # Empty context -> UNKNOWN
    breakdown = {
        "bull_argument": 40,
        "bear_argument": 0,
        "mtf_trend": 30
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)
    assert result["directional_confidence"] == 70  # 40 + 30


def test_risk_penalties_legacy_total_dict():
    """
    Audit Point 5:
    When LLM provides a dictionary with only {"total": -20} without categorized keys,
    the penalty must NOT be dropped to 0!
    """
    ceo = get_mock_ceo()
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 40,
        "risk_penalties": {
            "total": -20
        }
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context={})
    assert result["risk_penalties"] == -20
    assert result["entry_quality"] == 85 - 20  # 65
    assert result["risk_breakdown"]["total"] == -20


def test_risk_penalties_anti_double_counting_with_explicit_total():
    """
    Audit Point 5:
    When LLM provides both individual orthogonal categories AND an explicit total,
    anti-double-counting ensures they are not summed twice.
    """
    ceo = get_mock_ceo()
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 40,
        "risk_penalties": {
            "price_extension": -10,
            "macro_sentiment": -5,
            "total": -15
        }
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context={})
    # Must NOT sum -15 (categories) + -15 (total) = -30
    assert result["risk_penalties"] == -15
    assert result["risk_breakdown"]["total"] == -15


def test_debate_completeness_guard_veto():
    """
    Audit Point 2:
    When debate_status != 'COMPLETE' (e.g. DEGRADED_BULL_FAILED or DEGRADED_BEAR_FAILED),
    DeterministicGuard must deterministically veto ordinary trades to HOLD (tag: INCOMPLETE_DEBATE),
    preventing trade entry on one-sided analytical failure.
    """
    from core.deterministic_guard import DeterministicGuard
    from core.strategy_router import StrategyProfile
    from agents.risk_manager import RiskManager

    strat_profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        reasoning="Trend test"
    )
    ceo_proposal = {
        "decision": "LONG",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 85,
        "trade_action": "ENTER",
        "debate_status": "DEGRADED_BEAR_FAILED"  # Bear agent crashed
    }
    rm = MagicMock()
    rm._get_profile_rules.return_value = {"min_conviction": 75}

    decision = DeterministicGuard.evaluate(
        strategy_profile=strat_profile,
        ceo_proposal=ceo_proposal,
        profile="BALANCED",
        risk_manager=rm,
        symbol="BTC-USD"
    )

    assert decision.decision == "HOLD"
    assert decision.trade_action == "HOLD"
    assert decision.rejection_tag == "INCOMPLETE_DEBATE"
    assert decision.is_actionable is False


def test_mtf_partial_one_timeframe_zero_contribution():
    """
    Audit Point 2:
    When only one timeframe is known (e.g. 1H is bullish but 4H is missing/None),
    the state is PARTIAL, NOT ALIGNED_BULLISH!
    Partial timeframe context must have zero MTF contribution (raw_mtf = 0.0),
    preventing unwarranted confidence on incomplete data.
    """
    ceo = get_mock_ceo()
    market_context = {
        "multi_timeframe_context": {
            "trend_1h": "bullish",
            "trend_4h": ""  # 4H is missing!
        }
    }
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 30
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context=market_context)
    # Directional confidence must be ONLY from bull_argument (45), NOT 45 + 30 = 75!
    assert result["directional_confidence"] == 45
    assert result["trade_action"] == "HOLD"


def test_risk_penalties_liquidity_friction_without_total():
    """
    Audit Point 4:
    When LLM provides {"liquidity_friction": -10} without a "total" key,
    it must be properly recognized and deducted in the categorized sum.
    """
    ceo = get_mock_ceo()
    breakdown = {
        "bull_argument": 45,
        "bear_argument": 0,
        "mtf_trend": 0,
        "risk_penalties": {
            "liquidity_friction": -10
        }
    }
    result = ceo._validate_and_compute_score("LONG", breakdown, market_context={})
    assert result["risk_penalties"] == -10
    assert result["entry_quality"] == 35  # 45 - 10
    assert result["risk_breakdown"]["liquidity"] == -10


def test_debate_guard_breakout_exception_matrix():
    """
    Audit Point 3:
    Full matrix test for Debate Completeness Guard and its breakout exception:
    - Case A: Degraded debate + BREAKOUT strategy, but price did not break Donchian -> HOLD (INCOMPLETE_DEBATE)
    - Case B: Degraded debate + BREAKOUT strategy, price broke Donchian, but vol spike < 120% -> HOLD (INCOMPLETE_DEBATE)
    - Case C: Degraded debate + BREAKOUT strategy, price broke Donchian AND vol spike >= 120% -> Allowed (bypass debate veto)
    - Case D: Degraded debate + BREAKOUT strategy, with NaN indicators -> HOLD (INCOMPLETE_DEBATE)
    - Case E: Degraded debate + BREAKOUT strategy, but CEO opposes strategy bias -> HOLD (STRATEGY_GUARD_VETO)
    """
    from core.deterministic_guard import DeterministicGuard
    from core.strategy_router import StrategyProfile
    import math

    rm = MagicMock()
    rm._get_profile_rules.return_value = {"min_conviction": 75}

    strat_breakout_long = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="BREAKOUT",
        direction_bias="LONG",
        reasoning="Breakout test"
    )

    ceo_proposal_enter = {
        "decision": "LONG",
        "conviction": 85,
        "directional_confidence": 85,
        "entry_quality": 85,
        "trade_action": "ENTER",
        "debate_status": "DEGRADED_BULL_FAILED"
    }

    # Case A: Price (64000) does not break Donchian High (65000)
    mkt_a = {
        "price_data": {"current_price": 64000.0},
        "indicators": {"donchian_high": 65000.0, "volume_spike_pct": 150.0}
    }
    res_a = DeterministicGuard.evaluate(strat_breakout_long, ceo_proposal_enter.copy(), "BALANCED", rm, mkt_a, "BTC-USD")
    assert res_a.decision == "HOLD"
    assert res_a.rejection_tag == "INCOMPLETE_DEBATE"

    # Case B: Price breaks Donchian High, but volume spike is 110% (< 120%)
    mkt_b = {
        "price_data": {"current_price": 66000.0},
        "indicators": {"donchian_high": 65000.0, "volume_spike_pct": 110.0}
    }
    res_b = DeterministicGuard.evaluate(strat_breakout_long, ceo_proposal_enter.copy(), "BALANCED", rm, mkt_b, "BTC-USD")
    assert res_b.decision == "HOLD"
    assert res_b.rejection_tag == "INCOMPLETE_DEBATE"

    # Case C: Confirmed Breakout: price > Donchian High AND volume spike >= 120%
    mkt_c = {
        "price_data": {"current_price": 66000.0},
        "indicators": {"donchian_high": 65000.0, "volume_spike_pct": 135.0}
    }
    res_c = DeterministicGuard.evaluate(strat_breakout_long, ceo_proposal_enter.copy(), "BALANCED", rm, mkt_c, "BTC-USD")
    assert res_c.decision == "LONG"
    assert res_c.trade_action == "ENTER"
    assert res_c.is_actionable is True

    # Case D: NaN or invalid indicators
    mkt_d = {
        "price_data": {"current_price": float("nan")},
        "indicators": {"donchian_high": float("nan"), "volume_spike_pct": 200.0}
    }
    res_d = DeterministicGuard.evaluate(strat_breakout_long, ceo_proposal_enter.copy(), "BALANCED", rm, mkt_d, "BTC-USD")
    assert res_d.decision == "HOLD"
    assert res_d.rejection_tag == "INCOMPLETE_DEBATE"

    # Case E: Breakout confirmed mathematically, but CEO decision opposes strategy direction_bias
    strat_breakout_short = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="BREAKOUT",
        direction_bias="SHORT",
        reasoning="Breakout short"
    )
    res_e = DeterministicGuard.evaluate(strat_breakout_short, ceo_proposal_enter.copy(), "BALANCED", rm, mkt_c, "BTC-USD")
    assert res_e.decision == "HOLD"
    assert res_e.rejection_tag == "STRATEGY_GUARD_VETO"


def test_mtf_unrecognized_strings_are_not_treated_as_valid_timeframes():
    """
    Audit Follow-up P2:
    Arbitrary strings like 'unknown', 'n/a', 'unavailable' must NOT be counted as valid
    timeframes and must NOT accidentally trigger NEUTRAL_CHOP!
    - 'unknown' + 'n/a' -> UNKNOWN (fallback to LLM sign allowed if present)
    - 'bullish' + 'unavailable' -> PARTIAL (zero MTF bonus)
    - 'bullish' + 'neutral' -> NEUTRAL_CHOP (both valid, no conflict, zero bonus)
    - 'bullish' + 'bullish' -> ALIGNED_BULLISH (full bonus)
    """
    ceo = get_mock_ceo()

    # Case 1: Both unknown/n/a -> UNKNOWN (fallback to LLM allowed)
    mkt_unknown = {
        "multi_timeframe_context": {
            "trend_1h": "unknown",
            "trend_4h": "n/a"
        }
    }
    breakdown = {"bull_argument": 40, "bear_argument": 0, "mtf_trend": 20}
    res1 = ceo._validate_and_compute_score("LONG", breakdown, market_context=mkt_unknown)
    # Since UNKNOWN, fallback to LLM mtf_trend (20) is allowed -> 40 + 20 = 60
    assert res1["directional_confidence"] == 60

    # Case 2: 1H bullish, 4H unavailable -> PARTIAL (zero bonus, NO fallback!)
    mkt_partial = {
        "multi_timeframe_context": {
            "trend_1h": "bullish",
            "trend_4h": "unavailable"
        }
    }
    res2 = ceo._validate_and_compute_score("LONG", breakdown, market_context=mkt_partial)
    # Must NOT be ALIGNED_BULLISH and must NOT get LLM fallback -> strictly 40
    assert res2["directional_confidence"] == 40

    # Case 3: 1H bullish, 4H explicit neutral -> NEUTRAL_CHOP (both valid, zero bonus)
    mkt_chop = {
        "multi_timeframe_context": {
            "trend_1h": "bullish",
            "trend_4h": "neutral"
        }
    }
    res3 = ceo._validate_and_compute_score("LONG", breakdown, market_context=mkt_chop)
    assert res3["directional_confidence"] == 40

    # Case 4: 1H bullish, 4H bullish -> ALIGNED_BULLISH (full bonus)
    mkt_aligned = {
        "multi_timeframe_context": {
            "trend_1h": "bullish",
            "trend_4h": "bullish"
        }
    }
    res4 = ceo._validate_and_compute_score("LONG", breakdown, market_context=mkt_aligned)
    assert res4["directional_confidence"] == 60


def test_apply_breakout_penalty_override_cycles_56_to_58():
    """
    Validates apply_breakout_penalty_override on historical cycles #56-#58:
    - #56 BTC-USD: Vol 217%, Breakout 82145, price 82469 (+0.39% chase <= 1.5%), OB=NEUTRAL.
      DirConf 75%, Penalty -20 -> Capped to -10 -> Qual 65% (REDUCE_SIZE)
    - #57 BTC-USD: Vol 403%, Breakout 82145, price 82796 (+0.79% chase <= 1.5%), OB=BULL.
      DirConf 85%, Penalty -20 -> Capped to -5 -> Qual 80% (ENTER)
    - #57 BNB-USD: Vol 262%, Breakout 733.57, price 739.19 (+0.76% chase <= 1.5%), OB=NEUTRAL.
      DirConf 80%, Penalty -20 -> Capped to -5 -> Qual 75% (ENTER)
    - #57 LIT-USD: Vol 586%, Breakout 3.2102, price 3.3406 (+4.06% chase > 1.5%), OB=NEUTRAL.
      DirConf 72%, Penalty -25 -> NOT overridden (WAIT_FOR_PULLBACK)
    """
    from agents.ceo_agent import apply_breakout_penalty_override

    # Cycle #56 BTC-USD
    btc_56_ceo = {
        "decision": "LONG",
        "dir_conf": 75.0,
        "risk_penalty": -20.0,
        "entry_quality": 55.0,
        "action": "WAIT_FOR_PULLBACK"
    }
    btc_56_router = {
        "strategy_type": "BREAKOUT",
        "volume_pct": 217.0,
        "breakout_level": 82145.0,
        "current_price": 82469.0, # +0.39% chase
        "orderbook_bias": "NEUTRAL"
    }
    res_btc_56 = apply_breakout_penalty_override(btc_56_ceo, btc_56_router)
    assert res_btc_56["risk_penalty"] == -10.0
    assert res_btc_56["entry_quality"] == 65.0
    assert res_btc_56["action"] == "REDUCE_SIZE"

    # Cycle #57 BTC-USD
    btc_57_ceo = {
        "decision": "LONG",
        "dir_conf": 85.0,
        "risk_penalty": -20.0,
        "entry_quality": 65.0,
        "action": "WAIT_FOR_PULLBACK"
    }
    btc_57_router = {
        "strategy_type": "BREAKOUT",
        "volume_pct": 403.0,
        "breakout_level": 82145.0,
        "current_price": 82796.0, # +0.79% chase
        "orderbook_bias": "BULL"
    }
    res_btc_57 = apply_breakout_penalty_override(btc_57_ceo, btc_57_router)
    assert res_btc_57["risk_penalty"] == -5.0
    assert res_btc_57["entry_quality"] == 80.0
    assert res_btc_57["action"] == "ENTER"

    # Cycle #57 BNB-USD
    bnb_57_ceo = {
        "decision": "LONG",
        "dir_conf": 80.0,
        "risk_penalty": -20.0,
        "entry_quality": 60.0,
        "action": "WAIT_FOR_PULLBACK"
    }
    bnb_57_router = {
        "strategy_type": "BREAKOUT",
        "volume_pct": 262.0,
        "breakout_level": 733.57,
        "current_price": 739.19, # +0.76% chase
        "orderbook_bias": "NEUTRAL"
    }
    res_bnb_57 = apply_breakout_penalty_override(bnb_57_ceo, bnb_57_router)
    assert res_bnb_57["risk_penalty"] == -5.0
    assert res_bnb_57["entry_quality"] == 75.0
    assert res_bnb_57["action"] == "ENTER"

    # Cycle #57 LIT-USD (Overextended: chase 4.06% > 1.5%)
    lit_57_ceo = {
        "decision": "LONG",
        "dir_conf": 72.0,
        "risk_penalty": -25.0,
        "entry_quality": 47.0,
        "action": "WAIT_FOR_PULLBACK"
    }
    lit_57_router = {
        "strategy_type": "BREAKOUT",
        "volume_pct": 586.0,
        "breakout_level": 3.2102,
        "current_price": 3.3406, # +4.06% chase
        "orderbook_bias": "NEUTRAL"
    }
    res_lit_57 = apply_breakout_penalty_override(lit_57_ceo, lit_57_router)
    # Filter does NOT lift penalty on overextended chase (> 1.5%)
    assert res_lit_57["risk_penalty"] == -25.0
    assert res_lit_57["entry_quality"] == 47.0
    assert res_lit_57["action"] == "WAIT_FOR_PULLBACK"


def test_breakout_override_opposing_orderbook_and_low_volume():
    """
    Ensures override fails closed (does NOT upgrade) when:
    1. Opposing order book is detected (e.g. BEAR book on LONG breakout).
    2. Volume is insufficient (< 180%).
    3. Directional confidence is low (< 75%).
    """
    from agents.ceo_agent import apply_breakout_penalty_override

    # Case 1: Opposing order book
    ceo_dec = {
        "decision": "LONG",
        "dir_conf": 80.0,
        "risk_penalty": -20.0,
        "entry_quality": 60.0,
        "action": "WAIT_FOR_PULLBACK"
    }
    router_opposing = {
        "strategy_type": "BREAKOUT",
        "volume_pct": 350.0,
        "breakout_level": 100.0,
        "current_price": 100.5,
        "orderbook_bias": "BEAR"
    }
    res_opp = apply_breakout_penalty_override(ceo_dec.copy(), router_opposing)
    assert res_opp["action"] == "WAIT_FOR_PULLBACK"
    assert res_opp["risk_penalty"] == -20.0

    # Case 2: Volume < 180%
    router_low_vol = {
        "strategy_type": "BREAKOUT",
        "volume_pct": 160.0,
        "breakout_level": 100.0,
        "current_price": 100.5,
        "orderbook_bias": "NEUTRAL"
    }
    res_vol = apply_breakout_penalty_override(ceo_dec.copy(), router_low_vol)
    assert res_vol["action"] == "WAIT_FOR_PULLBACK"
    assert res_vol["risk_penalty"] == -20.0

    # Case 3: DirConf < 75%
    ceo_low_dir = {
        "decision": "LONG",
        "dir_conf": 70.0,
        "risk_penalty": -20.0,
        "entry_quality": 50.0,
        "action": "WAIT_FOR_PULLBACK"
    }
    router_valid = {
        "strategy_type": "BREAKOUT",
        "volume_pct": 250.0,
        "breakout_level": 100.0,
        "current_price": 100.5,
        "orderbook_bias": "NEUTRAL"
    }
    res_dir = apply_breakout_penalty_override(ceo_low_dir, router_valid)
    assert res_dir["action"] == "WAIT_FOR_PULLBACK"
    assert res_dir["risk_penalty"] == -20.0


def test_extract_router_metadata_from_payload():
    """
    Tests extract_router_metadata correctly parses pipeline payload.
    """
    from agents.ceo_agent import extract_router_metadata

    pipeline_payload = {
        "strategy_mode": "BREAKOUT",
        "direction_bias": "LONG",
        "current_price": 82469.0,
        "router_reasoning": "BREAKOUT LONG: Пробой 82145.0 с объемом 217%",
        "indicators": {
            "volume_spike_pct": 217.0,
            "donchian_high": 82145.0
        },
        "subordinate_analyst_reports": [
            {"agent_name": "Order_Book_Agent", "signal": "NEUTRAL"}
        ]
    }
    meta = extract_router_metadata(pipeline_payload)
    assert meta["strategy_type"] == "BREAKOUT"
    assert meta["volume_pct"] == 217.0
    assert meta["breakout_level"] == 82145.0
    assert meta["current_price"] == 82469.0
    assert meta["orderbook_bias"] == "NEUTRAL"





