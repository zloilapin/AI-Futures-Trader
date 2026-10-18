import os
import json
import pytest
from core.diagnostics import tracker
from core.decision_replay import DecisionReplayEngine, MockRiskRules
from core.strategy_router import StrategyProfile
from core.models import FinalTradeDecision
from core.deterministic_guard import DeterministicGuard


def test_record_decision_snapshot_file_writing(tmp_path):
    test_file = str(tmp_path / "test_snapshots.jsonl")
    snapshot1 = {
        "timestamp": 1700000000.0,
        "cycle_number": 1,
        "symbol": "BTC-USD",
        "stage": "STRATEGY_ROUTER",
        "final_action": "REJECTED",
        "rejection_tag": "NO_SIGNAL"
    }
    snapshot2 = {
        "timestamp": 1700000060.0,
        "cycle_number": 1,
        "symbol": "ETH-USD",
        "stage": "EXECUTION",
        "final_action": "EXECUTED",
        "router": {"strategy_mode": "BREAKOUT"}
    }

    tracker.record_decision_snapshot(snapshot1, filepath=test_file)
    tracker.record_decision_snapshot(snapshot2, filepath=test_file)

    assert os.path.exists(test_file)
    with open(test_file, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]

    assert len(lines) == 2
    assert lines[0]["symbol"] == "BTC-USD"
    assert lines[1]["symbol"] == "ETH-USD"


def test_decision_replay_load_and_filter(tmp_path):
    test_file = str(tmp_path / "replay_data.jsonl")
    data = [
        {"symbol": "BTC-USD", "stage": "DATA_GUARD", "final_action": "REJECTED", "router": {"strategy_mode": "RANGE"}},
        {"symbol": "ETH-USD", "stage": "EXECUTION", "final_action": "EXECUTED", "router": {"strategy_mode": "BREAKOUT"}},
        {"symbol": "SOL-USD", "stage": "DETERMINISTIC_GUARD", "final_action": "WAIT_FOR_PULLBACK", "router": {"strategy_mode": "TREND_FOLLOWING"}},
    ]
    with open(test_file, "w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item) + "\n")
        f.write("corrupted json line\n")

    engine = DecisionReplayEngine(filepath=test_file)
    loaded = engine.load_snapshots()
    assert len(loaded) == 3

    # Filter by symbol
    btc_only = engine.filter_snapshots(snapshots=loaded, symbol="BTC-USD")
    assert len(btc_only) == 1
    assert btc_only[0]["symbol"] == "BTC-USD"

    # Filter by strategy
    breakout_only = engine.filter_snapshots(snapshots=loaded, strategy_mode="BREAKOUT")
    assert len(breakout_only) == 1
    assert breakout_only[0]["symbol"] == "ETH-USD"

    # Filter by final_action
    executed = engine.filter_snapshots(snapshots=loaded, final_action="EXECUTED")
    assert len(executed) == 1
    assert executed[0]["final_action"] == "EXECUTED"


def test_decision_replay_funnel_computation(tmp_path):
    test_file = str(tmp_path / "funnel.jsonl")
    data = [
        {
            "symbol": "BTC-USD",
            "stage": "DATA_GUARD",
            "final_action": "REJECTED",
            "rejection_tag": "DATA_INVALID"
        },
        {
            "symbol": "ETH-USD",
            "stage": "STRATEGY_ROUTER",
            "final_action": "REJECTED",
            "rejection_tag": "NO_SIGNAL",
            "router": {"strategy_mode": "TREND_FOLLOWING"}
        },
        {
            "symbol": "SOL-USD",
            "stage": "DETERMINISTIC_GUARD",
            "final_action": "WAIT_FOR_PULLBACK",
            "rejection_tag": "WAIT_FOR_PULLBACK",
            "router": {"strategy_mode": "TREND_FOLLOWING"},
            "ceo": {"risk_breakdown": {"price_extension": -15, "macro_sentiment": -5}}
        },
        {
            "symbol": "AVAX-USD",
            "stage": "EXECUTION",
            "final_action": "EXECUTED",
            "router": {"strategy_mode": "BREAKOUT"},
            "ceo": {"risk_breakdown": {"price_extension": 0, "macro_sentiment": 0}}
        }
    ]
    with open(test_file, "w", encoding="utf-8") as f:
        for d in data:
            f.write(json.dumps(d) + "\n")

    engine = DecisionReplayEngine(filepath=test_file)
    funnel = engine.compute_funnel()

    assert funnel["total_candidates"] == 4
    assert funnel["actions"]["EXECUTED"] == 1
    assert funnel["actions"]["WAIT_FOR_PULLBACK"] == 1
    assert funnel["actions"]["REJECTED"] == 2
    assert funnel["conversion_rate_pct"] == 25.0

    # Categorized risk averages
    assert "price_extension" in funnel["risk_penalties_avg"]
    assert funnel["risk_penalties_avg"]["price_extension"] == -7.5

    # Report text formatting
    report = engine.format_report(funnel)
    assert "DECISION PIPELINE FUNNEL & TELEMETRY REPORT" in report
    assert "Total Candidates Scanned: 4" in report
    assert "CATEGORIZED RISK PENALTIES" in report


def test_guard_replay_verification(tmp_path):
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    # Construct a historical snapshot where DeterministicGuard ran
    snapshot = {
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {
            "strategy_mode": "TREND_FOLLOWING",
            "direction_bias": "LONG",
            "has_directional_signal": True,
            "reasoning": "Strong trend"
        },
        "ceo": {
            "decision": "LONG",
            "conviction": 85,
            "directional_confidence": 85,
            "entry_quality": 85,
            "reasoning": "Clear setup"
        },
        "guard": {
            "decision": "LONG",
            "action": "ENTER",
            "conviction": 85,
            "reason": "Passed deterministic guard.",
            "is_actionable": True
        }
    }

    is_match, msg, diff = engine.replay_guard_decision(snapshot)
    assert is_match is True
    assert msg == "MATCH"
    assert diff["original"]["action"] == diff["replayed"]["action"] == "ENTER"

    # Test audit runner
    audit = engine.run_guard_replay_audit([snapshot])
    assert audit["total_audited"] == 1
    assert audit["matches"] == 1
    assert audit["mismatches"] == 0
    assert audit["match_rate_pct"] == 100.0


def test_funnel_deduplication_by_candidate_id(tmp_path):
    """
    Audit Point 4:
    Verifies that compute_funnel deduplicates snapshots sharing the same candidate_id.
    A candidate recording multiple stages before executing must NOT count as multiple
    candidates in the denominator.
    """
    test_file = str(tmp_path / "multi_stage_funnel.jsonl")
    records = [
        # Candidate 1: BTC-USD cycle 1 (records 4 stages, ends in EXECUTED)
        {"candidate_id": "BTC-USD:1", "symbol": "BTC-USD", "cycle_number": 1, "stage": "STRATEGY_ROUTER", "final_action": "REDUCED", "router": {"strategy_mode": "TREND_FOLLOWING"}, "timestamp": 100},
        {"candidate_id": "BTC-USD:1", "symbol": "BTC-USD", "cycle_number": 1, "stage": "DETERMINISTIC_GUARD", "final_action": "ENTER", "router": {"strategy_mode": "TREND_FOLLOWING"}, "timestamp": 101},
        {"candidate_id": "BTC-USD:1", "symbol": "BTC-USD", "cycle_number": 1, "stage": "RISK_MANAGER", "final_action": "APPROVED", "router": {"strategy_mode": "TREND_FOLLOWING"}, "timestamp": 102},
        {"candidate_id": "BTC-USD:1", "symbol": "BTC-USD", "cycle_number": 1, "stage": "EXECUTION", "final_action": "EXECUTED", "router": {"strategy_mode": "TREND_FOLLOWING"}, "timestamp": 103},

        # Candidate 2: ETH-USD cycle 1 (records 2 stages, ends in WAIT_FOR_PULLBACK)
        {"candidate_id": "ETH-USD:1", "symbol": "ETH-USD", "cycle_number": 1, "stage": "STRATEGY_ROUTER", "final_action": "REDUCED", "router": {"strategy_mode": "TREND_FOLLOWING"}, "timestamp": 100},
        {"candidate_id": "ETH-USD:1", "symbol": "ETH-USD", "cycle_number": 1, "stage": "DETERMINISTIC_GUARD", "final_action": "WAIT_FOR_PULLBACK", "rejection_tag": "WAIT_FOR_PULLBACK", "router": {"strategy_mode": "TREND_FOLLOWING"}, "timestamp": 101},

        # Candidate 3: SOL-USD cycle 1 (rejected at SCANNER)
        {"candidate_id": "SOL-USD:1", "symbol": "SOL-USD", "cycle_number": 1, "stage": "SCANNER", "final_action": "REJECTED", "rejection_tag": "SCANNER_BLOCKED", "timestamp": 99}
    ]
    with open(test_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    engine = DecisionReplayEngine(filepath=test_file)
    funnel = engine.compute_funnel()

    assert funnel["total_records"] == 7
    assert funnel["total_candidates"] == 3  # Exactly 3 unique candidates!
    assert funnel["actions"]["EXECUTED"] == 1
    assert funnel["actions"]["WAIT_FOR_PULLBACK"] == 1
    assert funnel["actions"]["REJECTED"] == 1

    # Conversion rate must be 1 / 3 = 33.33% (NOT 1 / 7 = 14.28%!)
    assert funnel["conversion_rate_pct"] == 33.33
    assert funnel["strategies"]["TREND_FOLLOWING"]["total"] == 2
    assert funnel["strategies"]["TREND_FOLLOWING"]["executed"] == 1


def test_replay_with_captured_market_inputs_and_threshold(tmp_path):
    """
    Audit Point 3:
    Verifies that replay_guard_decision reconstructs market inputs (e.g. extreme funding rate)
    and captured min_conviction threshold from the candidate snapshot.
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    snapshot = {
        "candidate_id": "BTC-USD:42",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "min_conviction": 70,
        "router": {
            "strategy_mode": "TREND_FOLLOWING",
            "direction_bias": "LONG",
            "has_directional_signal": True,
            "reasoning": "Strong long"
        },
        "ceo": {
            "decision": "LONG",
            "conviction": 72,
            "directional_confidence": 85,
            "entry_quality": 72,
            "trade_action": "ENTER",
            "reasoning": "High conviction"
        },
        "market_guard_inputs": {
            "current_price": 65000.0,
            "funding_rate": 0.0008,  # > 0.0005 -> triggers FUNDING_VETO!
            "donchian_high": 66000.0,
            "donchian_low": 63000.0,
            "volume_spike_pct": 100.0,
            "atr_pct": 1.2,
            "bb_position_pct": 60.0
        },
        "guard": {
            "decision": "LONG",
            "action": "ENTER",
            "conviction": 72,
            "reason": "Funding gate veto",
            "is_actionable": False
        }
    }

    is_match, msg, diff = engine.replay_guard_decision(snapshot)
    assert is_match is True
    assert msg == "MATCH"
    assert diff["replayed"]["action"] == "ENTER"
    assert diff["replayed"]["decision"] == "LONG"


def test_replay_incomplete_detection_on_strict(tmp_path):
    """
    Audit Point 3:
    When strict=True or strategy is BREAKOUT without market_guard_inputs,
    replay_guard_decision returns INCOMPLETE_REPLAY instead of synthetic MATCH.
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    # Snapshot lacking market_guard_inputs
    snapshot = {
        "candidate_id": "BTC-USD:42",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {
            "strategy_mode": "BREAKOUT",
            "direction_bias": "LONG",
            "has_directional_signal": True,
            "reasoning": "Breakout"
        },
        "ceo": {
            "decision": "LONG",
            "conviction": 85,
            "trade_action": "ENTER"
        },
        "guard": {
            "decision": "LONG",
            "action": "ENTER"
        }
    }

    is_match, msg, diff = engine.replay_guard_decision(snapshot)
    assert is_match is False
    assert "INCOMPLETE_REPLAY" in msg


def test_replay_uses_original_ceo_input_not_guard_action(tmp_path):
    """
    Audit Point 1:
    Replay must take the original proposed CEO action ("ENTER") and confirm that
    DeterministicGuard legitimately converted it to "HOLD" (with full match on
    rejection_tag and is_actionable). It must NOT cheat by taking guard's "HOLD" as the input!
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    snapshot = {
        "candidate_id": "BTC-USD:c1_12345",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "min_conviction": 75,
        "router": {
            "strategy_mode": "TREND_FOLLOWING",
            "direction_bias": "LONG",
            "has_directional_signal": True,
            "reasoning": "Trend long"
        },
        "ceo": {
            "decision": "LONG",
            "conviction": 85,
            "directional_confidence": 85,
            "entry_quality": 85,
            "trade_action": "ENTER",  # CEO originally wanted to ENTER
            "debate_status": "DEGRADED_BEAR_FAILED"  # But debate was degraded!
        },
        "guard": {
            "decision": "HOLD",
            "action": "HOLD",
            "is_actionable": False,
            "guard_status": "BLOCKED",
            "rejection_tag": "INCOMPLETE_DEBATE"
        }
    }

    is_match, msg, diff = engine.replay_guard_decision(snapshot)
    assert is_match is True
    assert msg == "MATCH"
    assert diff["replayed"]["action"] == "HOLD"
    assert diff["replayed"]["decision"] == "HOLD"
    assert diff["replayed"]["is_actionable"] is False
    assert diff["replayed"]["rejection_tag"] == "INCOMPLETE_DEBATE"
    assert diff["replayed"]["guard_status"] == "BLOCKED"


def test_replay_strict_context_completeness(tmp_path):
    """
    Audit Point 6:
    When strict=True:
    - TREND_FOLLOWING missing multi_timeframe must return INCOMPLETE_REPLAY.
    - TREND_FOLLOWING with complete MTF & RSI context returns MATCH.
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    # Incomplete snapshot (missing MTF)
    incomplete_snapshot = {
        "candidate_id": "BTC-USD:c1_999",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {
            "strategy_mode": "TREND_FOLLOWING",
            "direction_bias": "LONG",
            "has_directional_signal": True
        },
        "ceo": {
            "decision": "LONG",
            "conviction": 80,
            "trade_action": "ENTER"
        },
        "guard": {
            "decision": "LONG",
            "action": "ENTER",
            "is_actionable": True,
            "guard_status": "PASSED"
        },
        "market_guard_inputs": {
            "current_price": 65000.0,
            # missing multi_timeframe and RSI!
        }
    }

    match_incompl, msg_incompl, _ = engine.replay_guard_decision(incomplete_snapshot, strict=True)
    assert match_incompl is False
    assert "INCOMPLETE_REPLAY" in msg_incompl

    # Complete snapshot
    complete_snapshot = {
        "candidate_id": "BTC-USD:c1_999",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "min_conviction": 75,
        "router": {
            "strategy_mode": "TREND_FOLLOWING",
            "direction_bias": "LONG",
            "has_directional_signal": True
        },
        "ceo": {
            "decision": "LONG",
            "conviction": 80,
            "trade_action": "ENTER"
        },
        "guard": {
            "decision": "LONG",
            "action": "ENTER",
            "is_actionable": True,
            "guard_status": "PASSED"
        },
        "market_guard_inputs": {
            "current_price": 65000.0,
            "rsi_14": 52.0,
            "bb_position_pct": 50.0,
            "multi_timeframe": {
                "mtf_alignment": "FULL_ALIGNMENT",
                "trend_1h": "BULLISH",
                "trend_4h": "BULLISH"
            }
        }
    }

    match_compl, msg_compl, _ = engine.replay_guard_decision(complete_snapshot, strict=True)
    assert match_compl is True
    assert msg_compl == "MATCH"


def test_replay_strict_fails_on_missing_ceo_trade_action(tmp_path):
    """
    Audit Follow-up P1:
    In strict=True, if snapshot lacks original ceo.trade_action,
    replay must fail with INCOMPLETE_REPLAY instead of falling back to guard_data["action"]
    and performing circular validation.
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    snapshot = {
        "candidate_id": "BTC-USD:c1_missing_action",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "min_conviction": 75,
        "router": {
            "strategy_mode": "TREND_FOLLOWING",
            "direction_bias": "LONG",
            "has_directional_signal": True
        },
        "ceo": {
            "decision": "LONG",
            "conviction": 80
            # missing "trade_action"!
        },
        "guard": {
            "decision": "LONG",
            "action": "ENTER",
            "is_actionable": True,
            "guard_status": "PASSED"
        },
        "market_guard_inputs": {
            "current_price": 65000.0,
            "rsi_14": 50.0,
            "multi_timeframe": {"trend_1h": "BULLISH"}
        }
    }

    match, msg, diff = engine.replay_guard_decision(snapshot, strict=True)
    assert match is False
    assert "INCOMPLETE_REPLAY: missing original ceo.trade_action" in msg


def test_replay_strict_fails_when_bb_and_rsi_both_none(tmp_path):
    """
    Audit Follow-up P1:
    When raw_bb was None, pipeline must not save a fake 50.0.
    If both bb_position_pct and rsi_14 are None in market_guard_inputs,
    strict replay for TREND_FOLLOWING must catch this as incomplete!
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    snapshot = {
        "candidate_id": "BTC-USD:c1_none_bb",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "min_conviction": 75,
        "router": {
            "strategy_mode": "TREND_FOLLOWING",
            "direction_bias": "LONG",
            "has_directional_signal": True
        },
        "ceo": {
            "decision": "LONG",
            "conviction": 80,
            "trade_action": "ENTER"
        },
        "guard": {
            "decision": "LONG",
            "action": "ENTER",
            "is_actionable": True,
            "guard_status": "PASSED"
        },
        "market_guard_inputs": {
            "current_price": 65000.0,
            "rsi_14": None,            # None!
            "bb_position_pct": None,   # None! (Not 50.0)
            "multi_timeframe": {"trend_1h": "BULLISH"}
        }
    }

    match, msg, diff = engine.replay_guard_decision(snapshot, strict=True)
    assert match is False
    assert "INCOMPLETE_REPLAY" in msg
    assert "rsi_or_bb" in msg


def test_replay_strict_strategy_matrix(tmp_path):
    """
    Audit Follow-up P2:
    Verifies that strict replay validates required fields for all strategy modes
    (BREAKOUT, PULLBACK, MEAN_REVERSION).
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    # 1. BREAKOUT missing donchian levels
    snap_bo = {
        "candidate_id": "BTC-USD:c1_bo",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "BREAKOUT", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {"current_price": 65000.0, "volume_spike_pct": 150.0, "donchian_high": 0.0, "donchian_low": 0.0}
    }
    m1, msg1, _ = engine.replay_guard_decision(snap_bo, strict=True)
    assert m1 is False
    assert "donchian" in msg1

    # 2. PULLBACK missing pullback context (is_pullback_retest, ema_20, atr_pct)
    snap_pb = {
        "candidate_id": "BTC-USD:c1_pb",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "PULLBACK", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {"current_price": 65000.0, "ema_20": 0.0, "atr_pct": None}
    }
    m2, msg2, _ = engine.replay_guard_decision(snap_pb, strict=True)
    assert m2 is False
    assert "pullback_context" in msg2

    # 3. MEAN_REVERSION missing both BB and RSI
    snap_mr = {
        "candidate_id": "BTC-USD:c1_mr",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "MEAN_REVERSION", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {"current_price": 65000.0, "bb_position_pct": None, "rsi_14": None}
    }
    m3, msg3, _ = engine.replay_guard_decision(snap_mr, strict=True)
    assert m3 is False
    assert "bb_or_rsi" in msg3


def test_replay_strict_pullback_rejects_missing_context_even_with_is_pullback_retest_false(tmp_path):
    """
    Auditor P1 Requirement:
    is_pullback_retest=False with missing/zero ema_20 and missing atr_pct
    must NOT pass strict replay!
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    snapshot = {
        "candidate_id": "BTC-USD:c1_pb_false",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "PULLBACK", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {
            "current_price": 65000.0,
            "is_pullback_retest": False,  # Present, but False!
            "ema_20": 0.0,                # Zero / unpopulated!
            "atr_pct": None               # Missing!
        }
    }
    match, msg, _ = engine.replay_guard_decision(snapshot, strict=True)
    assert match is False
    assert "INCOMPLETE_REPLAY" in msg
    assert "pullback_context" in msg


def test_replay_strict_breakout_rejects_none_volume(tmp_path):
    """
    Auditor P1 Requirement:
    Breakout with missing volume (None) must NOT pass strict replay.
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    snapshot = {
        "candidate_id": "BTC-USD:c1_bo_novol",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "BREAKOUT", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {
            "current_price": 65000.0,
            "donchian_high": 64000.0,
            "donchian_low": 60000.0,
            "volume_spike_pct": None      # Volume was unpopulated/None!
        }
    }
    match, msg, _ = engine.replay_guard_decision(snapshot, strict=True)
    assert match is False
    assert "INCOMPLETE_REPLAY" in msg
    assert "volume_spike_pct" in msg


def test_replay_strict_rejects_unsupported_strategy_mode(tmp_path):
    """
    Auditor P2 Requirement:
    Unknown or unsupported strategy mode must be rejected in strict replay,
    never silently bypassing validation.
    """
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    snapshot = {
        "candidate_id": "BTC-USD:c1_unknown_strat",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "UNKNOWN_CUSTOM_STRATEGY", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {"current_price": 65000.0}
    }
    match, msg, _ = engine.replay_guard_decision(snapshot, strict=True)
    assert match is False
    assert "INCOMPLETE_REPLAY" in msg
    assert "unsupported or unrecognized strategy_mode" in msg


def test_replay_strict_rejects_nan_and_infinite_numeric_inputs(tmp_path):
    """
    Auditor P2 Requirement:
    current_price=NaN or infinite values of indicators must fail strict replay.
    """
    import math
    engine = DecisionReplayEngine(filepath=str(tmp_path / "empty.jsonl"))

    # Case A: NaN price
    snap_nan = {
        "candidate_id": "BTC-USD:c1_nan_price",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "TREND_FOLLOWING", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {
            "current_price": float("nan"),
            "rsi_14": 50.0,
            "multi_timeframe": {"trend_1h": "BULLISH"}
        }
    }
    mA, msgA, _ = engine.replay_guard_decision(snap_nan, strict=True)
    assert mA is False
    assert "current_price" in msgA

    # Case B: Inf price
    snap_inf = {
        "candidate_id": "BTC-USD:c1_inf_price",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "TREND_FOLLOWING", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {
            "current_price": float("inf"),
            "rsi_14": 50.0,
            "multi_timeframe": {"trend_1h": "BULLISH"}
        }
    }
    mB, msgB, _ = engine.replay_guard_decision(snap_inf, strict=True)
    assert mB is False
    assert "current_price" in msgB

    # Case C: NaN donchian
    snap_nan_dh = {
        "candidate_id": "BTC-USD:c1_nan_dh",
        "symbol": "BTC-USD",
        "trading_profile": "BALANCED",
        "router": {"strategy_mode": "BREAKOUT", "direction_bias": "LONG", "has_directional_signal": True},
        "ceo": {"decision": "LONG", "conviction": 80, "trade_action": "ENTER"},
        "guard": {"decision": "LONG", "action": "ENTER"},
        "market_guard_inputs": {
            "current_price": 65000.0,
            "donchian_high": float("nan"),
            "donchian_low": float("nan"),
            "volume_spike_pct": 150.0
        }
    }
    mC, msgC, _ = engine.replay_guard_decision(snap_nan_dh, strict=True)
    assert mC is False
    assert "donchian" in msgC




