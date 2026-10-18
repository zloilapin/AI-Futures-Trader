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
