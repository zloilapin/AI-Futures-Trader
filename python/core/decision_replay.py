import os
import sys
import json
import time
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from core.models import FinalTradeDecision
from core.deterministic_guard import DeterministicGuard
from core.strategy_router import StrategyProfile


class MockRiskRules:
    """Mock risk rules provider for offline deterministic replay without exchange dependencies."""
    def _get_profile_rules(self, profile: str, strategy_mode: str) -> Dict[str, Any]:
        thresholds = {
            "CONSERVATIVE": 80,
            "BALANCED": 75,
            "AGGRESSIVE": 70
        }
        min_conv = thresholds.get(profile, 75)
        if strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"]:
            min_conv = max(65, min_conv - 5)
        return {"min_conviction": min_conv}


class CapturedRiskRules:
    """Replays using the exact captured min_conviction threshold from the candidate snapshot."""
    def __init__(self, min_conviction: int = 75):
        self.min_conviction = min_conviction
        self.MIN_CONVICTION_TO_TRADE = min_conviction

    def _get_profile_rules(self, profile: str, strategy_mode: str) -> Dict[str, Any]:
        return {"min_conviction": self.min_conviction}


class DecisionReplayEngine:
    """
    Offline telemetry replay and analysis utility for the AI-Futures-Trader decision block.
    Enables audit of the decision funnel, bottleneck identification, and deterministic replay.
    """
    def __init__(self, filepath: str = "data/memory/decision_snapshots.jsonl"):
        self.filepath = filepath

    def load_snapshots(self) -> List[Dict[str, Any]]:
        """Loads and parses all decision snapshots from JSONL log."""
        if not os.path.exists(self.filepath):
            return []
        
        snapshots = []
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            snapshots.append(record)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"⚠️ [ReplayEngine] Error reading {self.filepath}: {e}")
        return snapshots

    def filter_snapshots(
        self,
        snapshots: Optional[List[Dict[str, Any]]] = None,
        symbol: Optional[str] = None,
        stage: Optional[str] = None,
        strategy_mode: Optional[str] = None,
        final_action: Optional[str] = None,
        min_timestamp: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Filters snapshots by various criteria."""
        if snapshots is None:
            snapshots = self.load_snapshots()

        filtered = []
        for s in snapshots:
            if symbol and s.get("symbol", "").upper() != symbol.upper():
                continue
            if stage and s.get("stage", "").upper() != stage.upper():
                continue
            if final_action and s.get("final_action", "").upper() != final_action.upper():
                continue
            if min_timestamp and s.get("timestamp", 0) < min_timestamp:
                continue
            if strategy_mode:
                mode = s.get("router", {}).get("strategy_mode", "")
                if mode.upper() != strategy_mode.upper():
                    continue
            filtered.append(s)
        return filtered

    def compute_funnel(self, snapshots: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Computes aggregate decision funnel metrics deduplicated by candidate_id.
        Prevents multi-stage logging of the same candidate from inflating the denominator (Audit Point 4).
        """
        if snapshots is None:
            snapshots = self.load_snapshots()

        total_records = len(snapshots)
        if total_records == 0:
            return {
                "total_records": 0,
                "total_candidates": 0,
                "stages": {},
                "stage_dropoff": {},
                "actions": {},
                "rejection_tags": {},
                "strategies": {},
                "risk_penalties_avg": {},
                "conversion_rate_pct": 0.0
            }

        # Group snapshots by stable candidate_id
        candidates: Dict[str, List[Dict[str, Any]]] = {}
        for s in snapshots:
            c_id = s.get("candidate_id") or f"{s.get('symbol', 'UNKNOWN')}:{s.get('cycle_number', '0')}"
            if c_id not in candidates:
                candidates[c_id] = []
            candidates[c_id].append(s)

        total_candidates = len(candidates)

        actions = Counter()
        rejection_tags = Counter()
        stage_dropoff = Counter()
        stages_reached = Counter()

        strategies: Dict[str, Dict[str, int]] = {}
        risk_penalty_sums = Counter()
        risk_penalty_counts = 0

        for c_id, snaps in candidates.items():
            # Chronological order
            snaps_sorted = sorted(snaps, key=lambda x: x.get("timestamp", 0))
            terminal_snap = snaps_sorted[-1]

            # Track stages this candidate participated in
            seen_stages = set()
            for s in snaps_sorted:
                st = s.get("stage", "UNKNOWN")
                if st not in seen_stages:
                    stages_reached[st] += 1
                    seen_stages.add(st)

            # Determine terminal candidate outcome
            has_executed = any(s.get("final_action") == "EXECUTED" for s in snaps_sorted)
            if has_executed:
                cand_action = "EXECUTED"
            else:
                cand_action = terminal_snap.get("final_action", "UNKNOWN")

            actions[cand_action] += 1
            if cand_action != "EXECUTED":
                stage_dropoff[terminal_snap.get("stage", "UNKNOWN")] += 1
                if terminal_snap.get("rejection_tag"):
                    rejection_tags[terminal_snap.get("rejection_tag")] += 1

            # Strategy association from latest available router decision
            strat = "UNROUTED"
            for s in reversed(snaps_sorted):
                r_mode = s.get("router", {}).get("strategy_mode")
                if r_mode:
                    strat = r_mode
                    break

            if strat not in strategies:
                strategies[strat] = {"total": 0, "executed": 0, "pullback_wait": 0, "rejected": 0}
            strategies[strat]["total"] += 1
            if cand_action == "EXECUTED":
                strategies[strat]["executed"] += 1
            elif cand_action == "WAIT_FOR_PULLBACK":
                strategies[strat]["pullback_wait"] += 1
            else:
                strategies[strat]["rejected"] += 1

            # Penalties: record from the latest snapshot that has risk_breakdown
            for s in reversed(snaps_sorted):
                breakdown = s.get("ceo", {}).get("risk_breakdown")
                if breakdown and isinstance(breakdown, dict):
                    risk_penalty_counts += 1
                    for cat, val in breakdown.items():
                        if isinstance(val, (int, float)):
                            risk_penalty_sums[cat] += val
                    break

        executed_count = actions.get("EXECUTED", 0)
        conversion_rate = round(executed_count / total_candidates * 100, 2) if total_candidates > 0 else 0.0

        avg_penalties = {}
        if risk_penalty_counts > 0:
            for cat, total_val in risk_penalty_sums.items():
                avg_penalties[cat] = round(total_val / risk_penalty_counts, 2)

        return {
            "total_records": total_records,
            "total_candidates": total_candidates,
            "stages": dict(stages_reached),
            "stage_dropoff": dict(stage_dropoff),
            "actions": dict(actions),
            "rejection_tags": dict(rejection_tags),
            "strategies": strategies,
            "risk_penalties_avg": avg_penalties,
            "conversion_rate_pct": conversion_rate
        }

    def format_report(self, funnel: Dict[str, Any]) -> str:
        """Formats funnel metrics into a readable report."""
        total = funnel.get("total_candidates", 0)
        records = funnel.get("total_records", total)
        if total == 0:
            return "📭 No decision snapshots recorded yet in log."

        lines = [
            "============================================================",
            "📊 DECISION PIPELINE FUNNEL & TELEMETRY REPORT",
            "============================================================",
            f"Total Candidates Scanned: {total}",
            f"Total Telemetry Records:  {records}",
            f"Total Trades Executed:    {funnel.get('actions', {}).get('EXECUTED', 0)} ({funnel.get('conversion_rate_pct', 0.0)}%)",
            f"Waiting for Pullback:     {funnel.get('actions', {}).get('WAIT_FOR_PULLBACK', 0)}",
            f"Rejected / Vetoed:        {funnel.get('actions', {}).get('REJECTED', 0)}",
            f"Execution Failures:       {funnel.get('actions', {}).get('EXECUTION_FAILED', 0)}",
            "",
            "🔻 FUNNEL STAGE DROP-OFF (Where unique candidates exited):",
            "------------------------------------------------------------"
        ]

        stage_order = [
            ("DATA_GUARD", "1. Data Quality Guard"),
            ("SCANNER", "2. Scanner Agent (ATR/Spread)"),
            ("ANALYST_QUORUM", "3. Analyst Quorum (≥3 agents)"),
            ("STRATEGY_ROUTER", "4. Strategy Router Consensus"),
            ("DETERMINISTIC_GUARD", "5. Deterministic Guard"),
            ("SLIPPAGE_GATE", "6. Slippage Gate"),
            ("RISK_MANAGER", "7. Risk Manager Veto"),
            ("EXECUTION", "8. Execution on Exchange")
        ]

        for code, label in stage_order:
            drop_cnt = funnel.get("stage_dropoff", {}).get(code, 0)
            reach_cnt = funnel.get("stages", {}).get(code, 0)
            pct = round(drop_cnt / total * 100, 1) if total > 0 else 0.0
            bar = "█" * int(pct // 5)
            lines.append(f"  {label:<32}: Exited {drop_cnt:>3} ({pct:>5.1f}%) [Reached: {reach_cnt:>3}] {bar}")

        lines.extend([
            "",
            "🏷️ TOP REJECTION TAGS:",
            "------------------------------------------------------------"
        ])
        top_rejections = sorted(funnel.get("rejection_tags", {}).items(), key=lambda x: x[1], reverse=True)[:8]
        if top_rejections:
            for tag, cnt in top_rejections:
                lines.append(f"  • {tag:<30}: {cnt}")
        else:
            lines.append("  (None)")

        lines.extend([
            "",
            "🧭 STRATEGY CONVERSION BREAKDOWN (Unique Candidates):",
            "------------------------------------------------------------"
        ])
        for strat, data in funnel.get("strategies", {}).items():
            s_total = data.get("total", 0)
            s_exec = data.get("executed", 0)
            s_wait = data.get("pullback_wait", 0)
            conv = round(s_exec / s_total * 100, 1) if s_total > 0 else 0.0
            lines.append(f"  • {strat:<22}: {s_total:>3} candidates | Executed: {s_exec:>2} ({conv:>5.1f}%) | Wait: {s_wait:>2}")

        if funnel.get("risk_penalties_avg"):
            lines.extend([
                "",
                "⚖️ CATEGORIZED RISK PENALTIES (Averages for evaluated setups):",
                "------------------------------------------------------------"
            ])
            for cat, avg_val in funnel["risk_penalties_avg"].items():
                lines.append(f"  • {cat:<24}: {avg_val:>6.2f} pts")

        lines.append("============================================================")
        return "\n".join(lines)

    def replay_guard_decision(
        self,
        snapshot: Dict[str, Any],
        risk_manager: Optional[Any] = None,
        strict: bool = False
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Replays DeterministicGuard offline using snapshot input data.
        Verifies if deterministic guard logic produces the exact same decision and action.
        """
        guard_data = snapshot.get("guard")
        if not guard_data:
            return False, "Snapshot has no DeterministicGuard record to replay", {}

        router_data = snapshot.get("router", {})
        strategy_mode = router_data.get("strategy_mode", "TREND_FOLLOWING")
        direction_bias = router_data.get("direction_bias", "NEUTRAL")

        # Mock StrategyProfile
        strat_profile = StrategyProfile(
            has_directional_signal=router_data.get("has_directional_signal", True),
            strategy_mode=strategy_mode,
            direction_bias=direction_bias,
            reasoning=router_data.get("reasoning", "Replay Profile")
        )

        ceo_data = snapshot.get("ceo", {})
        debate_data = snapshot.get("debate", {})
        # Audit Point 1: Replay must use original CEO proposed trade_action, NOT guard_data["action"]!
        original_ceo_trade_action = ceo_data.get("trade_action")
        ceo_trade_action = original_ceo_trade_action or guard_data.get("action", "HOLD")
        raw_conv = ceo_data.get("conviction", 0)
        ceo_proposal = {
            "decision": ceo_data.get("decision", "HOLD"),
            "conviction": raw_conv,
            "directional_confidence": ceo_data.get("directional_confidence", raw_conv),
            "entry_quality": ceo_data.get("entry_quality", raw_conv),
            "trade_action": ceo_trade_action,
            "reasoning_en": ceo_data.get("reasoning", ""),
            "debate_status": debate_data.get("debate_status") or ceo_data.get("debate_status", "COMPLETE"),
            "bull_status": debate_data.get("bull_status") or ceo_data.get("bull_status", "OK"),
            "bear_status": debate_data.get("bear_status") or ceo_data.get("bear_status", "OK"),
            "disputed_arbitration": ceo_data.get("disputed_arbitration", False)
        }

        # Reconstruct market_data from market_guard_inputs if available (Audit Point 6)
        mgi = snapshot.get("market_guard_inputs")
        reconstructed_market_data = None
        if mgi and isinstance(mgi, dict):
            raw_rsi = mgi.get("rsi_14") if mgi.get("rsi_14") is not None else mgi.get("rsi")
            raw_bb = mgi.get("bb_position_pct") if mgi.get("bb_position_pct") is not None else mgi.get("bb_pos")
            reconstructed_market_data = {
                "price_data": {"current_price": mgi.get("current_price", 0.0)},
                "current_price": mgi.get("current_price", 0.0),
                "indicators": {
                    "donchian_high": mgi.get("donchian_high", 0.0),
                    "donchian_low": mgi.get("donchian_low", 0.0),
                    "volume_spike_pct": mgi.get("volume_spike_pct", 0.0),
                    "atr_pct": mgi.get("atr_pct", 0.0),
                    "bb_position_pct": float(raw_bb) if raw_bb is not None else None,
                    "ema_20": mgi.get("ema_20", 0.0),
                    "rsi_14": float(raw_rsi) if raw_rsi is not None else None,
                    "algo_signals": mgi.get("algo_signals") or {}
                },
                "multi_timeframe": mgi.get("multi_timeframe") or {},
                "derivatives_data": {
                    "funding_rate": mgi.get("funding_rate", 0.0)
                },
                "is_pullback_retest": bool(mgi.get("is_pullback_retest", False))
            }

        # Required context and indicator invariants by strategy mode (Audit Follow-up P1 & P2)
        REQUIRED_CONTEXT_BY_STRATEGY = {
            "TREND_FOLLOWING": [
                ("multi_timeframe", lambda m: bool(m.get("multi_timeframe"))),
                ("rsi_or_bb", lambda m: m.get("rsi_14") is not None or m.get("bb_position_pct") is not None),
            ],
            "BREAKOUT": [
                ("donchian", lambda m: m.get("donchian_high", 0.0) > 0 or m.get("donchian_low", 0.0) > 0),
                ("volume_spike_pct", lambda m: m.get("volume_spike_pct") is not None),
            ],
            "VOLATILITY_MOMENTUM": [
                ("donchian", lambda m: m.get("donchian_high", 0.0) > 0 or m.get("donchian_low", 0.0) > 0),
                ("volume_spike_pct", lambda m: m.get("volume_spike_pct") is not None),
            ],
            "MEAN_REVERSION": [
                ("bb_or_rsi", lambda m: m.get("bb_position_pct") is not None or m.get("rsi_14") is not None),
            ],
            "PULLBACK": [
                ("pullback_context", lambda m: "is_pullback_retest" in m or m.get("ema_20", 0.0) > 0 or m.get("atr_pct") is not None),
            ],
            "RELATIVE_MOMENTUM": [
                ("volume_spike_pct", lambda m: m.get("volume_spike_pct") is not None),
            ],
        }

        # Strict context completeness validation (Audit Follow-up P1 & P2)
        if strict:
            if not original_ceo_trade_action:
                return False, "INCOMPLETE_REPLAY: missing original ceo.trade_action in snapshot", {"status": "INCOMPLETE_REPLAY"}
            if not mgi or not isinstance(mgi, dict):
                return False, "INCOMPLETE_REPLAY: missing market_guard_inputs in snapshot", {"status": "INCOMPLETE_REPLAY"}
            if mgi.get("current_price", 0.0) <= 0:
                return False, "INCOMPLETE_REPLAY: missing valid current_price in market_guard_inputs", {"status": "INCOMPLETE_REPLAY"}

            for rule_name, validator in REQUIRED_CONTEXT_BY_STRATEGY.get(strategy_mode, []):
                if not validator(mgi):
                    return False, f"INCOMPLETE_REPLAY: missing required indicator/context '{rule_name}' for {strategy_mode}", {"status": "INCOMPLETE_REPLAY"}
        elif strategy_mode in ["BREAKOUT", "VOLATILITY_MOMENTUM"] and mgi is None:
            return False, "INCOMPLETE_REPLAY: missing market_guard_inputs in snapshot", {"status": "INCOMPLETE_REPLAY"}

        profile = snapshot.get("trading_profile", "BALANCED")
        if risk_manager:
            rm = risk_manager
        elif snapshot.get("min_conviction") is not None:
            rm = CapturedRiskRules(min_conviction=int(snapshot["min_conviction"]))
        else:
            rm = MockRiskRules()

        # Offline evaluate
        replayed_decision: FinalTradeDecision = DeterministicGuard.evaluate(
            strategy_profile=strat_profile,
            ceo_proposal=ceo_proposal,
            profile=profile,
            risk_manager=rm,
            market_data=reconstructed_market_data,
            symbol=snapshot.get("symbol", "")
        )

        orig_action = guard_data.get("action")
        orig_decision = guard_data.get("decision")
        orig_actionable = guard_data.get("is_actionable")
        orig_guard_status = guard_data.get("guard_status")
        orig_rejection_tag = guard_data.get("rejection_tag")

        replayed_action = replayed_decision.trade_action
        replayed_dir = replayed_decision.decision
        replayed_actionable = replayed_decision.is_actionable
        replayed_guard_status = replayed_decision.guard_status
        replayed_rejection_tag = replayed_decision.rejection_tag

        # Audit Point 1: Complete invariant comparison across all guard dimensions
        matches = (
            orig_action == replayed_action and
            orig_decision == replayed_dir and
            (orig_actionable is None or orig_actionable == replayed_actionable) and
            (orig_guard_status is None or orig_guard_status == replayed_guard_status) and
            (orig_rejection_tag is None or orig_rejection_tag == replayed_rejection_tag)
        )
        diff = {
            "original": {
                "decision": orig_decision,
                "action": orig_action,
                "is_actionable": orig_actionable,
                "guard_status": orig_guard_status,
                "rejection_tag": orig_rejection_tag
            },
            "replayed": {
                "decision": replayed_dir,
                "action": replayed_action,
                "is_actionable": replayed_actionable,
                "guard_status": replayed_guard_status,
                "rejection_tag": replayed_rejection_tag
            }
        }
        if not original_ceo_trade_action:
            diff["uncertified_fallback_trade_action"] = True
        msg = "MATCH" if matches else (
            f"MISMATCH: Orig(dir={orig_decision}, act={orig_action}, actable={orig_actionable}, tag={orig_rejection_tag}) vs "
            f"Replayed(dir={replayed_dir}, act={replayed_action}, actable={replayed_actionable}, tag={replayed_rejection_tag})"
        )
        return matches, msg, diff

    def run_guard_replay_audit(self, snapshots: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Runs replay audit across all snapshots that have Deterministic Guard data.
        Returns match stats and any discrepancies found.
        """
        if snapshots is None:
            snapshots = self.load_snapshots()

        eligible = [s for s in snapshots if "guard" in s and s.get("guard")]
        if not eligible:
            return {"total_audited": 0, "matches": 0, "mismatches": 0, "details": []}

        matches = 0
        mismatches = 0
        details = []

        for s in eligible:
            is_match, msg, diff = self.replay_guard_decision(s)
            if is_match:
                matches += 1
            else:
                mismatches += 1
                details.append({
                    "symbol": s.get("symbol"),
                    "timestamp": s.get("timestamp"),
                    "diff": diff,
                    "reason": msg
                })

        return {
            "total_audited": len(eligible),
            "matches": matches,
            "mismatches": mismatches,
            "match_rate_pct": round(matches / len(eligible) * 100, 2) if eligible else 100.0,
            "details": details
        }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Decision Pipeline Telemetry & Offline Replay")
    parser.add_argument("--file", default="data/memory/decision_snapshots.jsonl", help="Path to JSONL snapshots")
    parser.add_argument("--symbol", default=None, help="Filter by symbol (e.g. BTC-USD)")
    parser.add_argument("--strategy", default=None, help="Filter by strategy mode")
    parser.add_argument("--action", default=None, help="Filter by final action")
    parser.add_argument("--verify-guard", action="store_true", help="Run deterministic replay verification")
    parser.add_argument("--limit", type=int, default=10, help="Number of recent records to display")
    args = parser.parse_args()

    engine = DecisionReplayEngine(filepath=args.file)
    snapshots = engine.filter_snapshots(
        symbol=args.symbol,
        strategy_mode=args.strategy,
        final_action=args.action
    )

    funnel = engine.compute_funnel(snapshots)
    print(engine.format_report(funnel))

    if args.verify_guard:
        print("\n🔍 Running Offline Deterministic Guard Replay Audit...")
        audit = engine.run_guard_replay_audit(snapshots)
        print(f"Audited: {audit['total_audited']} setups | Matches: {audit['matches']} | Mismatches: {audit['mismatches']}")
        print(f"Determinism Integrity Rate: {audit['match_rate_pct']}%")
        if audit["details"]:
            print("⚠️ Discrepancies:")
            for d in audit["details"]:
                print(f"  • {d['symbol']}: {d['reason']}")


if __name__ == "__main__":
    main()
