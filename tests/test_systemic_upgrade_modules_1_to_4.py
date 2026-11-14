import pytest
import math
import json
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from core.models import FinalTradeDecision
from core.strategy_router import StrategyProfile
from core.config import (
    PARAMETER_INVARIANTS,
    SCHEMA_VERSION,
    validate_and_clamp_params,
    get_tuned_param,
    reset_tuned_param_cache,
)
from core.deterministic_guard import DeterministicGuard
from agents.risk_manager import (
    RiskManager,
    get_normalized_atr_pct,
    solve_optimal_pullback_entry,
)
from agents.ceo_agent import CEOAgent, ScoreResult


# ═════════════════════════════════════════════════════════════════════════════
# MODULE 01: Multi-Timeframe Quorum & LTF Retracement Tolerance Tests
# ═════════════════════════════════════════════════════════════════════════════

def test_module1_watchlist_retest_tolerates_ltf_retracement_bounce():
    """
    On a SHORT watchlist retest, LTF candles & oscillators naturally bounce bullish
    as price pulls back to the retest level. This must NOT trigger counter-momentum divergence
    unless a full structural breakout is confirmed.
    """
    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 65.0},      # Expected LTF retracement bounce
        {"agent_name": "Indicator_Agent", "signal": "BULLISH", "confidence": 64.0},   # Expected LTF bounce
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH", "confidence": 62.0},  # Resistance absorption confirmed!
        {"agent_name": "OI_Funding_Agent", "signal": "NEUTRAL", "confidence": 50.0},
    ]
    market_data = {
        "price_data": {"current_price": 67200.0},
        "invalidation_price": 67800.0,
        "htf_direction": "SHORT",
    }
    wl_meta = {
        "target_entry_price": 67200.0,
        "invalidation_price": 67800.0,
        "htf_direction": "SHORT",
    }

    ok, tag, reason = DeterministicGuard.evaluate_analyst_quorum(
        direction="SHORT",
        analyst_reports=analyst_reports,
        entry_trigger_type="WATCHLIST_RETEST",
        market_data=market_data,
        watchlist_metadata=wl_meta
    )
    assert ok is True
    assert tag is None
    assert "retest quorum confirmed" in reason.lower()


def test_module1_watchlist_retest_vetoes_on_full_structural_breakout():
    """
    Vetoes only if LTF momentum indicates a TRUE structural reversal breakout:
    Candle >= 72% AND Indicator >= 72% AND OrderBook >= 65% opposing.
    """
    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 75.0},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH", "confidence": 74.0},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH", "confidence": 68.0},
        {"agent_name": "OI_Funding_Agent", "signal": "NEUTRAL", "confidence": 50.0},
    ]
    market_data = {
        "price_data": {"current_price": 67200.0},
        "invalidation_price": 67800.0,
        "htf_direction": "SHORT",
    }

    ok, tag, reason = DeterministicGuard.evaluate_analyst_quorum(
        direction="SHORT",
        analyst_reports=analyst_reports,
        entry_trigger_type="WATCHLIST_RETEST",
        market_data=market_data
    )
    assert ok is False
    assert tag == "COUNTER_MOMENTUM_DIVERGENCE_VETO"
    assert "Full structural breakout against retest" in reason


def test_module1_watchlist_retest_invalidation_breach_veto():
    """If current price breaches the structural invalidation level, retest is strictly vetoed."""
    analyst_reports = [
        {"agent_name": "Order_Book_Agent", "signal": "BEARISH", "confidence": 70.0},
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 60.0},
    ]
    # Price 68100 > invalidation level 67800 for SHORT
    market_data = {
        "price_data": {"current_price": 68100.0},
        "invalidation_price": 67800.0,
        "htf_direction": "SHORT",
    }

    ok, tag, reason = DeterministicGuard.evaluate_analyst_quorum(
        direction="SHORT",
        analyst_reports=analyst_reports,
        entry_trigger_type="WATCHLIST_RETEST",
        market_data=market_data
    )
    assert ok is False
    assert tag == "WATCHLIST_RETEST_INVALIDATION_VETO"
    assert "breached structural invalidation level" in reason


def test_module1_regular_scan_momentum_alignment_enforced():
    """
    On REGULAR_SCAN, standard momentum continuation rules apply:
    Opposing Candle >= 60% and Indicator >= 60% triggers COUNTER_MOMENTUM_DIVERGENCE_VETO.
    """
    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BEARISH", "confidence": 62.0},
        {"agent_name": "Indicator_Agent", "signal": "BEARISH", "confidence": 65.0},
        {"agent_name": "Order_Book_Agent", "signal": "BULLISH", "confidence": 75.0},
        {"agent_name": "OI_Funding_Agent", "signal": "BULLISH", "confidence": 70.0},
    ]
    ok, tag, reason = DeterministicGuard.evaluate_analyst_quorum(
        direction="LONG",
        analyst_reports=analyst_reports,
        entry_trigger_type="REGULAR_SCAN"
    )
    assert ok is False
    assert tag == "COUNTER_MOMENTUM_DIVERGENCE_VETO"


# ═════════════════════════════════════════════════════════════════════════════
# MODULE 02: ATR Data Contract & Mathematical Inverse Solver (P*) Tests
# ═════════════════════════════════════════════════════════════════════════════

def test_module2_get_normalized_atr_pct_priorities_and_clamping():
    # Priority a: volatility_metrics["atr_1h_pct"]
    md1 = {"volatility_metrics": {"atr_1h_pct": 2.35}}
    assert math.isclose(get_normalized_atr_pct(md1, 67000.0), 2.35, abs_tol=1e-4)

    # Priority b: indicators["atr_1h"] / entry_price * 100
    md2 = {"indicators": {"atr_1h": 1340.0}}
    assert math.isclose(get_normalized_atr_pct(md2, 67000.0), 2.0, abs_tol=1e-4)

    # Priority c: indicators["atr_15m"] * 2.0 / entry_price * 100
    md3 = {"indicators": {"atr_14": 502.5}}
    assert math.isclose(get_normalized_atr_pct(md3, 67000.0), 1.5, abs_tol=1e-4)

    # Priority d: Clamped bounds [0.25, 20.0]
    md_low = {"volatility_metrics": {"atr_1h_pct": 0.05}}
    assert math.isclose(get_normalized_atr_pct(md_low, 67000.0), 0.25, abs_tol=1e-4)

    md_high = {"volatility_metrics": {"atr_1h_pct": 35.0}}
    assert math.isclose(get_normalized_atr_pct(md_high, 67000.0), 20.0, abs_tol=1e-4)


def test_module2_solve_optimal_pullback_entry_short_satisfies_all_invariants():
    """
    Mathematical test for SHORT:
    P_mkt = 67000, Structural S = 68500 (2.24% away > max 1.85%).
    Solver must find P* >= 67000 such that:
      (1) Stop dist <= 0.90 * max_sl_pct
      (2) Risk on min_notional <= 0.95 * max_risk_usd
      (3) Net R:R >= 1.8:1
      (4) Breathing room >= 0.60 * atr_15m
    """
    p_star = solve_optimal_pullback_entry(
        direction="SHORT",
        current_price=67000.0,
        structural_sl=68500.0,
        target_tp=64000.0,
        atr_15m=200.0,
        max_sl_dist_pct=1.85,
        max_risk_usd=2.00,
        min_notional_usd=100.0,
        min_net_rr=1.8,
        taker_fee_pct=0.0005
    )
    assert p_star is not None
    assert p_star >= 67000.0
    assert p_star < 68500.0

    # Invariant 1: Stop distance <= 0.90 * max_sl_pct
    stop_dist_pct = ((68500.0 - p_star) / p_star) * 100.0
    assert stop_dist_pct <= (0.90 * 1.85) + 1e-4

    # Invariant 2: Risk on min notional <= 0.95 * max_risk_usd
    contracts = 100.0 / p_star
    risk_usd = contracts * (68500.0 - p_star)
    assert risk_usd <= (0.95 * 2.00) + 1e-4

    # Invariant 3: Net R:R >= 1.8
    tp_dist = p_star - 64000.0
    sl_dist = 68500.0 - p_star
    assert (tp_dist / sl_dist) >= 1.8 - 1e-4

    # Invariant 4: Breathing room >= 0.60 * atr_15m (120.0)
    assert sl_dist >= 0.60 * 200.0


def test_module2_solve_optimal_pullback_entry_long_satisfies_all_invariants():
    """
    Mathematical test for LONG:
    P_mkt = 67000, Structural S = 65500. Target T = 70500.
    """
    p_star = solve_optimal_pullback_entry(
        direction="LONG",
        current_price=67000.0,
        structural_sl=65500.0,
        target_tp=70500.0,
        atr_15m=200.0,
        max_sl_dist_pct=1.85,
        max_risk_usd=2.00,
        min_notional_usd=100.0,
        min_net_rr=1.8,
        taker_fee_pct=0.0005
    )
    assert p_star is not None
    assert p_star <= 67000.0
    assert p_star > 65500.0

    # Invariant 1 & 2
    stop_dist_pct = ((p_star - 65500.0) / p_star) * 100.0
    assert stop_dist_pct <= (0.90 * 1.85) + 1e-4
    contracts = 100.0 / p_star
    assert (contracts * (p_star - 65500.0)) <= (0.95 * 2.00) + 1e-4

    # Invariant 3 & 4
    tp_dist = 70500.0 - p_star
    sl_dist = p_star - 65500.0
    assert (tp_dist / sl_dist) >= 1.8 - 1e-4
    assert sl_dist >= 0.60 * 200.0


def test_module2_solve_optimal_pullback_entry_impossible_returns_none():
    """If breathing room conflicts with max risk cap, returns None cleanly."""
    # S is too close to breathing room (e.g. 10 USD away when ATR is 1000)
    res = solve_optimal_pullback_entry(
        direction="SHORT",
        current_price=67000.0,
        structural_sl=67050.0,
        target_tp=64000.0,
        atr_15m=500.0, # Breathing room = 300, but S - P_mkt is only 50
        max_sl_dist_pct=1.85,
        max_risk_usd=2.00
    )
    assert res is None


def test_module2_solve_optimal_pullback_entry_balanced_rr_140():
    """
    Verifies that setups with structural TP at 1.40R - 1.70R solve successfully
    under the BALANCED profile minimum R:R (1.40), whereas an overly rigid 2.0+ R:R would discard them.
    """
    # Default min_net_rr is now 1.40
    p_star_default = solve_optimal_pullback_entry(
        direction="SHORT",
        current_price=67000.0,
        structural_sl=68000.0,
        target_tp=66000.0,
        atr_15m=200.0,
        max_sl_dist_pct=1.85,
        max_risk_usd=2.00,
        min_notional_usd=100.0,
    )
    # Structural TP is 66000, SL is 68000.
    # At P=67000, RR = (67000-66000)/(68000-67000) = 1.0.
    # To get RR >= 1.40: (P - 66000) / (68000 - P) >= 1.4 => 2.4 P >= 66000 + 95200 = 161200 => P >= 67166.7
    # For RR >= 2.0: (P - 66000) / (68000 - P) >= 2.0 => 3.0 P >= 66000 + 136000 = 202000 => P >= 67333.3
    # Check that default solves for P* >= 67166.7 and satisfies 1.40 R:R
    assert p_star_default is not None
    assert p_star_default >= 67166.0
    net_rr = (p_star_default - 66000.0) / (68000.0 - p_star_default)
    assert net_rr >= 1.40 - 1e-4

    # When requiring rigid 2.0 R:R:
    p_star_strict = solve_optimal_pullback_entry(
        direction="SHORT",
        current_price=67000.0,
        structural_sl=68000.0,
        target_tp=66000.0,
        atr_15m=200.0,
        max_sl_dist_pct=1.85,
        max_risk_usd=2.00,
        min_notional_usd=100.0,
        min_net_rr=2.00,
    )
    if p_star_strict is not None:
        assert (p_star_strict - 66000.0) / (68000.0 - p_star_strict) >= 2.0 - 1e-4



# ═════════════════════════════════════════════════════════════════════════════
# MODULE 03: Orthogonal C_dir vs Q_loc & Contextual Sentiment Bypass Tests
# ═════════════════════════════════════════════════════════════════════════════

def test_module3_contextual_sentiment_bypass_in_trending_regime():
    """
    In a TRENDING regime with expanding momentum, Fear & Greed euphoria (>= 80)
    reflects healthy trend continuation, NOT an overbought reversal. Penalty must be bypassed.
    """
    logger = MagicMock()
    llm = MagicMock()
    llm.model_name = "test_model"
    ceo = CEOAgent(logger, llm, llm)

    market_context = {
        "macro_regime": "TRENDING",
        "market_data": {
            "macro_regime": "TRENDING",
            "indicators": {
                "bb_pos_pct": 65.0,        # Not in extreme exhaustion (>85)
                "trend_strength_1h": 32.0,  # Expanding trend (>20)
            }
        }
    }

    # Evaluate categorized penalties directly with fear_greed = 85 (Extreme Greed)
    penalty, breakdown = ceo._calculate_categorized_risk_penalties(
        decision="LONG",
        breakdown={},
        rsi=62.0,
        fear_greed=85.0,
        market_context=market_context
    )
    # macro_sentiment penalty must be 0 (bypassed) instead of -10
    assert breakdown.get("macro_sentiment", 0) == 0


def test_module3_contextual_sentiment_enforced_in_exhaustion_band():
    """
    If price IS in the extreme exhaustion band (bb_pos_pct > 85),
    Fear & Greed penalty is NOT bypassed and is properly enforced.
    """
    logger = MagicMock()
    llm = MagicMock()
    llm.model_name = "test_model"
    ceo = CEOAgent(logger, llm, llm)

    market_context = {
        "macro_regime": "TRENDING",
        "market_data": {
            "indicators": {
                "bb_pos_pct": 92.0,        # In extreme exhaustion (>85)
                "trend_strength_1h": 32.0,
            }
        }
    }

    penalty, breakdown = ceo._calculate_categorized_risk_penalties(
        decision="LONG",
        breakdown={},
        rsi=75.0,
        fear_greed=85.0,
        market_context=market_context
    )
    assert breakdown.get("macro_sentiment", 0) <= -5


def test_module3_systemic_routing_cases_in_deterministic_guard():
    """
    Case 1: High C_dir >= 65% and High Q_loc >= 60% => Immediate Entry.
    Case 2: High C_dir >= 65% and Low Q_loc < 60% => WAIT_FOR_PULLBACK.
    Case 3: Low C_dir < 65% => HOLD.
    """
    strat = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        reasoning=""
    )
    rm = MagicMock()
    rm._get_profile_rules.return_value = {"min_conviction": 65}

    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH", "confidence": 75.0},
        {"agent_name": "Indicator_Agent", "signal": "BULLISH", "confidence": 70.0},
    ]

    # Case 1: C_dir = 80, Q_loc = 85 => ENTER
    ceo1 = {
        "decision": "LONG",
        "conviction": 75,
        "directional_confidence": 80,
        "location_quality": 85.0,
        "entry_quality": 75,
        "trade_action": "ENTER",
        "risk_breakdown": {"price_extension": 0}
    }
    d1 = DeterministicGuard.evaluate(
        strategy_profile=strat,
        ceo_proposal=ceo1,
        profile="BALANCED",
        risk_manager=rm,
        symbol="BTC-USD",
        analyst_reports=analyst_reports
    )
    assert d1.decision == "LONG"
    assert d1.trade_action in ["ENTER", "REDUCE_SIZE"]

    # Case 2: C_dir = 80, Q_loc = 40 (overextended) => WAIT_FOR_PULLBACK
    ceo2 = {
        "decision": "LONG",
        "conviction": 65,
        "directional_confidence": 80,
        "location_quality": 40.0,
        "entry_quality": 65,
        "trade_action": "ENTER",
        "risk_breakdown": {"price_extension": -15}
    }
    d2 = DeterministicGuard.evaluate(
        strategy_profile=strat,
        ceo_proposal=ceo2,
        profile="BALANCED",
        risk_manager=rm,
        symbol="BTC-USD",
        analyst_reports=analyst_reports
    )
    assert d2.trade_action == "WAIT_FOR_PULLBACK"
    assert d2.guard_status == "PULLBACK_WATCHLIST"

    # Case 3: C_dir = 55 (< 65%) => HOLD
    ceo3 = {
        "decision": "LONG",
        "conviction": 55,
        "directional_confidence": 55,
        "location_quality": 90.0,
        "entry_quality": 55,
        "trade_action": "ENTER",
        "risk_breakdown": {"price_extension": 0}
    }
    d3 = DeterministicGuard.evaluate(
        strategy_profile=strat,
        ceo_proposal=ceo3,
        profile="BALANCED",
        risk_manager=rm,
        symbol="BTC-USD",
        analyst_reports=analyst_reports
    )
    assert d3.decision == "HOLD"
    assert d3.trade_action == "HOLD"


# ═════════════════════════════════════════════════════════════════════════════
# MODULE 04: Parameter Invariants, Auto-Healing & Position Hydration Tests
# ═════════════════════════════════════════════════════════════════════════════

def test_module4_parameter_invariants_clamping_and_relational_healing():
    """
    Validates hard individual bounds:
      half_risk: [0.50, 0.95]
      be_activation_r: [1.10, 1.40]
      trail_activation_r: [1.40, 2.50]
    And relational invariants:
      be >= half_risk + 0.35
      trail >= be + 0.25
    """
    raw_params = {
        "half_risk_activation_r": 0.80,
        "be_activation_r": 0.90,  # Legacy 0.90 violates lower bound 1.10 and relational (0.80 + 0.35 = 1.15)
        "trail_activation_r": 1.20, # Violates lower bound 1.40 and relational (1.15 + 0.25 = 1.40)
    }

    clamped = validate_and_clamp_params(raw_params)
    assert clamped["half_risk_activation_r"] == 0.80
    assert clamped["be_activation_r"] == 1.15  # Healed to 0.80 + 0.35
    assert clamped["trail_activation_r"] == 1.40 # Healed to 1.15 + 0.25


def test_module4_disk_auto_healing(tmp_path):
    """Verifies validate_and_clamp_params auto-heals an outdated disk cache to schema version 2."""
    config_file = tmp_path / "tuned_hyperparams.json"
    old_data = {
        "schema_version": 1,
        "best_params": {
            "be_activation_r": 0.85,
            "half_risk_activation_r": 0.75,
            "trail_activation_r": 1.30
        }
    }
    config_file.write_text(json.dumps(old_data, indent=4), encoding="utf-8")

    clamped = validate_and_clamp_params(old_data["best_params"], auto_heal_disk=True, source_path=config_file)
    assert clamped["be_activation_r"] == 1.10
    assert clamped["trail_activation_r"] == 1.40

    # Verify on-disk file was updated to schema_version 2
    updated_disk = json.loads(config_file.read_text(encoding="utf-8"))
    assert updated_disk["schema_version"] == SCHEMA_VERSION
    assert updated_disk["best_params"]["be_activation_r"] == 1.10


def test_module4_position_hydration_in_nado_trading_service():
    """
    Verifies that active positions loaded from disk or restored after restart
    automatically have their parameters validated and clamped to invariant bounds.
    """
    from services.nado_trading_service import NadoTradingService
    
    service = NadoTradingService(nado_client=MagicMock())
    
    # Simulate a position stored with legacy clamped parameters (e.g. be_activation_r = 0.90)
    legacy_pos = {
        "BTC-USD": {
            "symbol": "BTC-USD",
            "entry_price": 67226.0,
            "direction": "SHORT",
            "half_risk_activation_r": 0.75,
            "be_activation_r": 0.90,  # Legacy clamp
            "trail_activation_r": 1.10,
        }
    }
    
    with patch("core.state_store.StateStore.load", return_value=legacy_pos):
        clean_positions = service._load_positions()
        btc_pos = clean_positions["BTC-USD"]
        assert btc_pos["half_risk_activation_r"] == 0.75
        assert btc_pos["be_activation_r"] == 1.10  # Automatically clamped/healed to >= 0.75 + 0.35 and in [1.10, 1.40]
        assert btc_pos["trail_activation_r"] == 1.40 # Automatically clamped/healed to in [1.40, 2.50]
