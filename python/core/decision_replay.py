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
        Computes aggregate decision funnel metrics across all candidates.
        """
        if snapshots is None:
            snapshots = self.load_snapshots()

        total = len(snapshots)
        if total == 0:
            return {
                "total_candidates": 0,
                "stages": {},
                "actions": {},
                "rejection_tags": {},
                "strategies": {},
                "risk_penalties_avg": {},
                "conversion_rate_pct": 0.0
            }

        stages = Counter(s.get("stage", "UNKNOWN") for s in snapshots)
        actions = Counter(s.get("final_action", "UNKNOWN") for s in snapshots)
        rejection_tags = Counter(s.get("rejection_tag") for s in snapshots if s.get("rejection_tag"))

        # Strategy breakdown & conversion
        strategies: Dict[str, Dict[str, int]] = {}
        risk_penalty_sums = Counter()
        risk_penalty_counts = 0

        for s in snapshots:
            r = s.get("router", {})
            strat = r.get("strategy_mode", "UNROUTED")
            if strat not in strategies:
                strategies[strat] = {"total": 0, "executed": 0, "pullback_wait": 0, "rejected": 0}
            strategies[strat]["total"] += 1
            
            action = s.get("final_action")
            if action == "EXECUTED":
                strategies[strat]["executed"] += 1
            elif action == "WAIT_FOR_PULLBACK":
                strategies[strat]["pullback_wait"] += 1
            else:
                strategies[strat]["rejected"] += 1

            ceo = s.get("ceo", {})
            breakdown = ceo.get("risk_breakdown", {})
            if breakdown and isinstance(breakdown, dict):
                risk_penalty_counts += 1
                for cat, val in breakdown.items():
                    if isinstance(val, (int, float)):
                        risk_penalty_sums[cat] += val

        executed_count = actions.get("EXECUTED", 0)
        conversion_rate = round(executed_count / total * 100, 2) if total > 0 else 0.0

        avg_penalties = {}
        if risk_penalty_counts > 0:
            for cat, total_val in risk_penalty_sums.items():
                avg_penalties[cat] = round(total_val / risk_penalty_counts, 2)

        return {
            "total_candidates": total,
            "stages": dict(stages),
            "actions": dict(actions),
            "rejection_tags": dict(rejection_tags),
            "strategies": strategies,
            "risk_penalties_avg": avg_penalties,
            "conversion_rate_pct": conversion_rate
        }

    def format_report(self, funnel: Dict[str, Any]) -> str:
        """Formats funnel metrics into a readable report."""
        total = funnel.get("total_candidates", 0)
        if total == 0:
            return "📭 No decision snapshots recorded yet in log."

        lines = [
            "============================================================",
            "📊 DECISION PIPELINE FUNNEL & TELEMETRY REPORT",
            "============================================================",
            f"Total Candidates Scanned: {total}",
            f"Total Trades Executed:    {funnel.get('actions', {}).get('EXECUTED', 0)} ({funnel.get('conversion_rate_pct', 0.0)}%)",
            f"Waiting for Pullback:     {funnel.get('actions', {}).get('WAIT_FOR_PULLBACK', 0)}",
            f"Rejected / Vetoed:        {funnel.get('actions', {}).get('REJECTED', 0)}",
            f"Execution Failures:       {funnel.get('actions', {}).get('EXECUTION_FAILED', 0)}",
            "",
            "🔻 FUNNEL STAGE DROP-OFF (Where candidates exited):",
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
            cnt = funnel.get("stages", {}).get(code, 0)
            pct = round(cnt / total * 100, 1) if total > 0 else 0.0
            bar = "█" * int(pct // 5)
            lines.append(f"  {label:<32}: {cnt:>4} ({pct:>5.1f}%) {bar}")

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
            "🧭 STRATEGY CONVERSION BREAKDOWN:",
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
        risk_manager: Optional[Any] = None
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
        ceo_proposal = {
            "decision": ceo_data.get("decision", "HOLD"),
            "conviction": ceo_data.get("conviction", 0),
            "directional_confidence": ceo_data.get("directional_confidence", 0),
            "entry_quality": ceo_data.get("entry_quality", 0),
            "reasoning_en": ceo_data.get("reasoning", "")
        }

        profile = snapshot.get("trading_profile", "BALANCED")
        rm = risk_manager or MockRiskRules()

        # Offline evaluate
        replayed_decision: FinalTradeDecision = DeterministicGuard.evaluate(
            strategy_profile=strat_profile,
            ceo_proposal=ceo_proposal,
            profile=profile,
            risk_manager=rm,
            symbol=snapshot.get("symbol", "")
        )

        orig_action = guard_data.get("action")
        orig_decision = guard_data.get("decision")
        replayed_action = replayed_decision.trade_action
        replayed_dir = replayed_decision.decision

        matches = (orig_action == replayed_action and orig_decision == replayed_dir)
        diff = {
            "original": {"decision": orig_decision, "action": orig_action},
            "replayed": {"decision": replayed_dir, "action": replayed_action}
        }
        msg = "MATCH" if matches else f"MISMATCH: Orig({orig_decision}, {orig_action}) vs Replayed({replayed_dir}, {replayed_action})"
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
