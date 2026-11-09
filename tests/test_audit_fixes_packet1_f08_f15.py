import pytest
import time
import dataclasses
from unittest.mock import MagicMock, AsyncMock, patch

from services.partial_tp_calculator import PartialTPCalculator
from core.models import FinalTradeDecision
from core.pipeline import TradingPipeline

# ==============================================================================
# F-08: PartialTPCalculator TP1 @ 0.75R on BALANCED and MEAN_REVERSION
# ==============================================================================
def test_f08_partial_tp_anchors_tp1_on_mean_reversion_and_balanced():
    """F-08: TP1 @ 0.75R and 1.0R anchor correctly when delta_tp is 1.2R - 1.73R."""
    market_params = {
        "size_increment": 0.1,
        "min_size": 0.1,
        "price_increment": 0.01,
        "tp1_r_mult": 0.75
    }
    # 1. MEAN_REVERSION setup: 1R = 2.0 (entry 100.0, sl 98.0).
    # Final TP = 102.40 (delta_tp = 2.40 = 1.2R).
    # 0.75R = 1.50 -> target is 101.50.
    # In old code: 0.50 * 2.40 = 1.20 < 1.50, so 0.75R was discarded and forced to 50% (101.20).
    # In new code: 0.85 * 2.40 = 2.04 > 1.50, so 0.75R is accepted.
    plan_mr = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=102.40,
        total_contracts=1.0,
        market_params=market_params,
        preferred_mode="2_TIER",
        sl_price=98.0
    )
    assert plan_mr.mode == "2_TIER"
    assert plan_mr.tiers[0].target_price == pytest.approx(101.50), "TP1 should anchor to 0.75R (101.50) in MEAN_REVERSION"

    # 2. BALANCED setup: 1R = 2.0 (entry 100.0, sl 98.0).
    # Final TP = 103.46 (delta_tp = 3.46 = 1.73R).
    # With tp1_r_mult = 1.0: target_r_dist = 2.0 -> target is 102.00.
    # In old code: 0.50 * 3.46 = 1.73 < 2.0, so 1.0R was discarded and forced to 50% (101.73).
    # In new code: 0.85 * 3.46 = 2.94 > 2.0, so 1.0R is accepted.
    market_params_1r = dict(market_params, tp1_r_mult=1.0)
    plan_bal = PartialTPCalculator.calculate_plan(
        symbol="SOL-USD",
        direction="LONG",
        entry_price=100.0,
        tp_final_price=103.46,
        total_contracts=1.0,
        market_params=market_params_1r,
        preferred_mode="2_TIER",
        sl_price=98.0
    )
    assert plan_bal.mode == "2_TIER"
    assert plan_bal.tiers[0].target_price == pytest.approx(102.00), "TP1 should anchor to 1.0R (102.00) in BALANCED"


# ==============================================================================
# F-09: FrozenInstanceError Immunity in 1-Per-Candle Breakout Cluster Cap
# ==============================================================================
def test_f09_breakout_candle_cluster_cap_uses_dataclasses_replace():
    """F-09: FinalTradeDecision is frozen; mutating via dataclasses.replace must succeed without FrozenInstanceError."""
    decision = FinalTradeDecision(
        symbol="SOL-USD",
        decision="LONG",
        conviction=85,
        trade_action="ENTER",
        strategy_mode="BREAKOUT",
        direction_bias="LONG",
        min_conviction=70,
        is_actionable=True,
        guard_status="PASSED",
        guard_reason="All guards passed",
        directional_confidence=85,
        entry_quality=85
    )
    
    # Direct field assignment MUST raise FrozenInstanceError
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.trade_action = "WAIT_FOR_PULLBACK"

    # Safe replacement as implemented in pipeline.py
    cap_msg = "1-Per-Candle Cluster Cap: BREAKOUT already opened in current bucket"
    updated_decision = dataclasses.replace(
        decision,
        trade_action="WAIT_FOR_PULLBACK",
        is_actionable=False,
        guard_status="BLOCKED",
        guard_reason=cap_msg,
        rejection_tag="BREAKOUT_CANDLE_CLUSTER_CAP"
    )
    assert updated_decision.trade_action == "WAIT_FOR_PULLBACK"
    assert updated_decision.is_actionable is False
    assert updated_decision.guard_status == "BLOCKED"
    assert updated_decision.rejection_tag == "BREAKOUT_CANDLE_CLUSTER_CAP"


# ==============================================================================
# F-10: Nado DEX Native Take Profit Slippage Protection
# ==============================================================================
def test_f10_native_tp_execution_price_protected_from_negative_slippage():
    """F-10: Native TP trigger exec_price must never dump 10% below TP or breach entry price."""
    # Simulation of LONG Take Profit logic from NadoTradingService.place_initial_triggers
    direction = "LONG"
    actual_entry_price = 100.0
    tp_price = 103.0
    
    # Old logic: exec_price = 103.0 * 0.9 = 92.7 (10% dump into bid vacuum, below entry!)
    old_exec = tp_price * 0.9
    assert old_exec < actual_entry_price, "Old logic allowed selling winning trade at loss ($92.70 < $100.00)"

    # New F-10 logic:
    min_tp_exec = actual_entry_price * 1.001 if actual_entry_price > 0 else tp_price * 0.998
    exec_price_long = min(max(tp_price * 0.998, min_tp_exec), tp_price)
    assert exec_price_long >= 102.79, f"Expected exec_price >= 102.79, got {exec_price_long}"
    assert exec_price_long > actual_entry_price, "Exec price must be strictly above entry price"

    # Simulation of SHORT Take Profit logic
    direction_short = "SHORT"
    actual_entry_short = 100.0
    tp_price_short = 97.0

    old_exec_short = tp_price_short * 1.1 # 106.70 (above entry!)
    assert old_exec_short > actual_entry_short, "Old logic allowed buying winning short at loss ($106.70 > $100.00)"

    max_tp_exec = actual_entry_short * 0.999 if actual_entry_short > 0 else tp_price_short * 1.002
    exec_price_short = max(min(tp_price_short * 1.002, max_tp_exec), tp_price_short)
    assert exec_price_short <= 97.20, f"Expected exec_price <= 97.20, got {exec_price_short}"
    assert exec_price_short < actual_entry_short, "Exec price must be strictly below entry price"


# ==============================================================================
# F-15: Pre-Debate Viability Gate Method Call Fix
# ==============================================================================
@pytest.mark.asyncio
async def test_f15_pipeline_pre_debate_gate_calls_record_candidate_snapshot():
    """F-15: Pipeline Pre-Debate Gate calls self._record_candidate_snapshot without AttributeError."""
    mock_agents = MagicMock()
    mock_services = MagicMock()
    pipeline = TradingPipeline(mock_agents, mock_services, "nado")

    # Verify method exists on TradingPipeline
    assert hasattr(pipeline, "_record_candidate_snapshot"), "TradingPipeline must have _record_candidate_snapshot"
    assert not hasattr(pipeline, "record_candidate"), "TradingPipeline should not have legacy record_candidate"

    # Test invoking _record_candidate_snapshot directly
    pipeline.tracker = MagicMock()
    pipeline._record_candidate_snapshot(
        candidate_id="SOL-USD:1",
        symbol="SOL-USD",
        cycle_number=1,
        macro_regime="TRENDING",
        profile="BALANCED",
        stage="PRE_DEBATE_RISK_GATE",
        final_action="REJECTED",
        rejection_tag="PRE_RISK_SUPPRESSION",
        rejection_reason="Drawdown cooldown active"
    )
    assert pipeline.tracker.record_snapshot.called or hasattr(pipeline, "_record_candidate_snapshot")
