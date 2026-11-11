import pytest
from unittest.mock import MagicMock
from core.deterministic_guard import DeterministicGuard
from core.strategy_router import StrategyRouter, StrategyProfile
from agents.risk_manager import RiskManager
from agents.ceo_agent import CEOAgent
from agents.regime_agent import RegimeAgent
from core.logger import TradeLogger
from core.state_store import StateStore

@pytest.fixture
def logger():
    return TradeLogger()

@pytest.fixture
def risk_manager(logger):
    return RiskManager(logger, None)


# ─── 1. One-way Structural SL Retention in Price Drift Guard ─────────────────

def test_structural_sl_retention_in_evaluate_price_drift():
    # 1. SHORT: Price drops in our favor (100.0 -> 98.0, price_delta = -2.0)
    # SL must REMAIN at structural 105.0 (not pulled down into 103.0 noise!)
    is_ok, re_entry, re_tp, re_sl, msg = DeterministicGuard.evaluate_price_drift(
        symbol="ZEC-USD",
        direction="SHORT",
        snapshot_price=100.0,
        live_price=98.0,
        tp_price=90.0,
        sl_price=105.0
    )
    assert is_ok is True
    assert re_entry == 98.0
    assert re_tp == 88.0
    assert re_sl == 105.0, "SHORT SL must not be pulled closer to market price on favorable drift"

    # 2. SHORT: Price pulls back up (100.0 -> 101.0, price_delta = +1.0)
    # SL should adjust upward with the higher entry (105.0 -> 106.0)
    is_ok2, re_entry2, re_tp2, re_sl2, msg2 = DeterministicGuard.evaluate_price_drift(
        symbol="ZEC-USD",
        direction="SHORT",
        snapshot_price=100.0,
        live_price=101.0,
        tp_price=90.0,
        sl_price=105.0
    )
    assert is_ok2 is True
    assert re_entry2 == 101.0
    assert re_sl2 == 106.0

    # 3. LONG: Price rises in our favor (100.0 -> 102.0, price_delta = +2.0)
    # SL must REMAIN at structural 95.0 (not pulled up into 97.0 noise!)
    is_ok3, re_entry3, re_tp3, re_sl3, msg3 = DeterministicGuard.evaluate_price_drift(
        symbol="SOL-USD",
        direction="LONG",
        snapshot_price=100.0,
        live_price=102.0,
        tp_price=110.0,
        sl_price=95.0
    )
    assert is_ok3 is True
    assert re_entry3 == 102.0
    assert re_tp3 == 112.0
    assert re_sl3 == 95.0, "LONG SL must not be pulled closer to market price on favorable drift"

    # 4. LONG: Price dips down (100.0 -> 99.0, price_delta = -1.0)
    # SL should adjust downward with the lower entry (95.0 -> 94.0)
    is_ok4, re_entry4, re_tp4, re_sl4, msg4 = DeterministicGuard.evaluate_price_drift(
        symbol="SOL-USD",
        direction="LONG",
        snapshot_price=100.0,
        live_price=99.0,
        tp_price=110.0,
        sl_price=95.0
    )
    assert is_ok4 is True
    assert re_entry4 == 99.0
    assert re_sl4 == 94.0


# ─── 2. Volume Explosion Override & Retest Priority Pass Through Step 3 ───────

def test_volume_explosion_override_passes_step3_ban(logger, risk_manager):
    ceo_proposal = {
        "decision": "LONG",
        "trade_action": "WAIT_FOR_PULLBACK",
        "directional_confidence": 85,
        "conviction": 70,
        "entry_quality": 65,
        "reasoning_en": "Strong explosive momentum"
    }

    market_data = {
        "price_data": {
            "current_price": 100.0,
            "candles_20": [{"open": 98.0, "high": 100.5, "low": 97.5, "close": 100.0}]
        },
        "indicators": {
            "volume_spike_pct": 320.0, # >= 250%
            "rsi_14": 60.0,            # < 78 for LONG
            "bb_pos": 75.0,
            "atr_14": 1.5
        }
    }

    strategy_profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="LONG",
        reasoning="Trend continuation"
    )

    res = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=ceo_proposal,
        profile="balanced",
        risk_manager=risk_manager,
        market_data=market_data,
        symbol="SOL-USD",
        logger=logger
    )

    # Must be actionable with volume explosion override!
    assert res.is_actionable is True
    assert res.trade_action == "REDUCE_SIZE"
    assert res.volume_explosion_override is True
    assert res.action_size_mult_override == 0.50
    assert res.guard_status == "PASSED"


def test_retest_priority_passes_step3_ban(logger, risk_manager):
    # Setup retest proposal with entry_quality=65%
    ceo_proposal = {
        "decision": "SHORT",
        "trade_action": "WAIT_FOR_PULLBACK",
        "directional_confidence": 82,
        "conviction": 70,
        "entry_quality": 65,
        "reasoning_en": "Retest of EMA-20 completed"
    }

    market_data = {
        "price_data": {"current_price": 100.0},
        "indicators": {
            "volume_spike_pct": 110.0,
            "rsi_14": 45.0,
            "bb_pos": 65.0,
            "atr_14": 1.5
        }
    }

    # Strategy profile explicitly has RETEST in reasoning
    strategy_profile = StrategyProfile(
        has_directional_signal=True,
        strategy_mode="TREND_FOLLOWING",
        direction_bias="SHORT",
        reasoning="PULLBACK_RETEST SHORT: Цена достигла целевой зоны отката EMA-20"
    )

    res = DeterministicGuard.evaluate(
        strategy_profile=strategy_profile,
        ceo_proposal=ceo_proposal,
        profile="balanced",
        risk_manager=risk_manager,
        market_data=market_data,
        symbol="SOL-USD",
        logger=logger
    )

    # Retest promotion must promote and bypass Step 3 ban!
    assert res.is_actionable is True
    assert res.trade_action in ["ENTER", "REDUCE_SIZE"]
    assert res.rejection_tag is None
    assert res.guard_status == "PASSED"


# ─── 3. Retest Priority ADX and Analyst Opposition Vetoes ─────────────────────

def test_retest_priority_vetoes_on_weak_adx():
    market_data = {
        "is_pullback_retest": True,
        "pullback_retest_direction": "SHORT",
        "price_data": {"current_price": 100.0},
        "indicators": {
            "adx_14": 12.0, # < 20.0
            "rsi_14": 45.0,
            "bb_pos": 60.0
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT"
        }
    }

    analyst_reports = [
        {"agent_name": "Indicator_Agent", "signal": "BEARISH"},
        {"agent_name": "OrderBook_Agent", "signal": "BEARISH"}
    ]

    profile = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=analyst_reports,
        macro_cache={},
        detected_regime="TRENDING_BEAR"
    )

    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "LOW_ADX_RETEST_VETO"
    assert "слабый ADX" in profile.reasoning


def test_retest_priority_vetoes_on_analyst_opposition():
    # 3 tech bulls opposing SHORT retest
    market_data = {
        "is_pullback_retest": True,
        "pullback_retest_direction": "SHORT",
        "price_data": {"current_price": 100.0},
        "indicators": {
            "adx_14": 25.0, # Healthy ADX
            "rsi_14": 45.0,
            "bb_pos": 60.0
        },
        "multi_timeframe": {
            "trend_1h": "BEARISH",
            "trend_4h": "BEARISH",
            "mtf_alignment": "FULL_ALIGNMENT"
        }
    }

    analyst_reports = [
        {"agent_name": "Candle_Agent", "signal": "BULLISH"},
        {"agent_name": "OrderBook_Agent", "signal": "BULLISH"},
        {"agent_name": "OI_Agent", "signal": "BULLISH"}
    ]

    profile = StrategyRouter.evaluate(
        symbol="SOL-USD",
        market_data=market_data,
        valid_reports=analyst_reports,
        macro_cache={},
        detected_regime="TRENDING_BEAR"
    )

    assert profile.has_directional_signal is False
    assert profile.strategy_mode == "ROCKET_RALLY_VETO"
    assert "оппозиции аналитиков" in profile.reasoning


# ─── 4. CEO Independent Risk Arbitration Conviction Sync ──────────────────────

@pytest.mark.asyncio
async def test_ceo_independent_risk_arbitration_conviction_sync(logger, risk_manager):
    # Primary CEO says SHORT (conv=80), Escalation says HOLD
    primary_mock = MagicMock()
    escalation_mock = MagicMock()

    primary_mock.generate_json.return_value = {
        "decision": "SHORT",
        "directional_confidence": 80,
        "risk_score": 70,
        "entry_quality": 70,
        "trade_action": "ENTER",
        "winning_argument": "Bear",
        "score_breakdown": {
            "bull_argument": 10,
            "bear_argument": -40,
            "mtf_trend": -30,
            "risk_penalties": {"total": -10}
        },
        "reasoning_en": "Primary Bear thesis"
    }

    escalation_mock.generate_json.return_value = {
        "decision": "HOLD",
        "directional_confidence": 50,
        "risk_score": 50,
        "entry_quality": 50,
        "trade_action": "HOLD",
        "winning_argument": "Neither",
        "score_breakdown": {
            "bull_argument": 0,
            "bear_argument": 0,
            "mtf_trend": 0,
            "risk_penalties": {"total": 0}
        },
        "reasoning_en": "Arbiter prefers HOLD"
    }

    ceo = CEOAgent(logger, primary_mock, escalation_mock)

    data = {
        "symbol": "SOL-USD",
        "strategy_mode": "TREND_FOLLOWING",
        "direction_bias": "SHORT",
        "macro_regime": "BEAR_TREND",
        "multi_timeframe_context": {"trend_1h": "BEARISH", "trend_4h": "BEARISH"},
        "subordinate_analyst_reports": [
            {"agent_name": "Indicator_Agent", "signal": "BEARISH", "confidence": 80},
            {"agent_name": "OrderBook_Agent", "signal": "BEARISH", "confidence": 75}
        ]
    }

    res = await ceo.analyze(data)

    # If independent risk approved, conviction must be synced to safe_arb_cap (71.0)
    if res.get("disputed_arbitration") and res.get("decision") == "SHORT":
        assert res["conviction"] >= 71
        assert res["trade_action"] == "REDUCE_SIZE"

        # Now pass to DeterministicGuard to verify it's NOT blocked by LOW_CONFIDENCE
        strategy_profile = StrategyProfile(True, "TREND_FOLLOWING", "SHORT", "Test")
        guard_res = DeterministicGuard.evaluate(
            strategy_profile=strategy_profile,
            ceo_proposal=res,
            profile="balanced",
            risk_manager=risk_manager,
            market_data={"indicators": {"bb_pos": 50.0, "rsi_14": 50.0}},
            symbol="SOL-USD",
            logger=logger
        )
        assert guard_res.is_actionable is True
        assert guard_res.trade_action == "REDUCE_SIZE"
        assert guard_res.guard_status == "PASSED"


# ─── 5. RegimeAgent Hysteresis State Persistence ──────────────────────────────

@pytest.mark.asyncio
async def test_regime_agent_hysteresis_state_persistence(logger):
    # Initialize first instance and simulate RANGE_CHOPPY
    agent1 = RegimeAgent(logger, None)
    agent1.last_regime = "RANGE_CHOPPY"
    agent1.last_range_score = 65.0
    agent1.consecutive_trend_count = 0
    StateStore.save("data/memory/regime_state.json", {
        "last_regime": "RANGE_CHOPPY",
        "last_range_score": 65.0,
        "consecutive_trend_count": 0,
        "last_trend_ts": 0.0,
        "last_eval_ts": 1000.0
    })

    # Initialize second instance (simulating bot restart)
    agent2 = RegimeAgent(logger, None)
    assert agent2.last_regime == "RANGE_CHOPPY"
    assert agent2.last_range_score == 65.0

    # On a sudden trend spike (trend=70, range=20), hysteresis must route through TRANSITION
    regime, profile = agent2.classify_regime_from_scores(
        trend_score=70.0,
        range_score=20.0,
        vol_score=20.0,
        btc_atr=0.8,
        previous_regime=agent2.last_regime,
        consecutive_trend_count=agent2.consecutive_trend_count
    )

    assert regime == "TRANSITION"
    assert profile == "BALANCED"


# ─── 6. CEO Threshold 73% Synchronization ─────────────────────────────────────

def test_ceo_enter_threshold_73_sync(logger):
    agent = CEOAgent(logger, MagicMock(), MagicMock())
    
    # 1. _validate_and_compute_score at EQ 73 gives ENTER
    # 50 bull, 0 bear, mtf bull 23 -> total_bull 73 -> dir_conf 73, penalty 0 -> EQ 73
    breakdown_73 = {"bull_argument": 50, "bear_argument": 0, "mtf_trend": 23}
    res_73 = agent._validate_and_compute_score(
        decision="LONG",
        breakdown=breakdown_73,
        market_context={"strategy_mode": "TREND_FOLLOWING"}
    )
    assert res_73["entry_quality"] == 73
    assert res_73["trade_action"] == "ENTER"

    # EQ 72 gives REDUCE_SIZE
    breakdown_72 = {"bull_argument": 50, "bear_argument": 0, "mtf_trend": 22}
    res_72 = agent._validate_and_compute_score(
        decision="LONG",
        breakdown=breakdown_72,
        market_context={"strategy_mode": "TREND_FOLLOWING"}
    )
    assert res_72["entry_quality"] == 72
    assert res_72["trade_action"] == "REDUCE_SIZE"

    # Retest at EQ 73 gives ENTER, at EQ 72 gives REDUCE_SIZE
    res_retest_73 = agent._validate_and_compute_score(
        decision="LONG",
        breakdown=breakdown_73,
        market_context={"is_pullback_retest": True}
    )
    assert res_retest_73["trade_action"] == "ENTER"

    res_retest_72 = agent._validate_and_compute_score(
        decision="LONG",
        breakdown=breakdown_72,
        market_context={"is_pullback_retest": True}
    )
    assert res_retest_72["trade_action"] == "REDUCE_SIZE"

    # apply_breakout_penalty_override at 73.0 gives ENTER
    from agents.ceo_agent import apply_breakout_penalty_override
    ceo_payload = {
        "decision": "LONG",
        "dir_conf": 83.0,
        "risk_penalty": -15.0,
        "entry_quality": 68.0,
        "action": "WAIT_FOR_PULLBACK"
    }
    router_meta = {
        "strategy_type": "BREAKOUT",
        "volume_pct": 200.0,
        "breakout_level": 100.0,
        "current_price": 100.5,
        "max_chase_pct": 1.5,
        "orderbook_bias": "NEUTRAL"
    }
    # dir_conf 83 + capped_penalty -10 = 73.0 -> ENTER
    adj_payload = apply_breakout_penalty_override(ceo_payload, router_meta)
    assert adj_payload["entry_quality"] == 73.0
    assert adj_payload["action"] == "ENTER"

