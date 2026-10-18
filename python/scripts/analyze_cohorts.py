"""
Cohort & Expectancy Analysis Tool for AI-Futures-Trader
Analyzes closed trades logged in data/memory/trade_journal.json across Cohorts A, B, C, D.

Cohorts:
  Group A: High Conviction (>= min_conviction + 10), Full Size (ENTER)
  Group B: High Conviction (>= min_conviction + 10), Reduced Size (REDUCE_SIZE)
  Group C: Low Conviction (< min_conviction + 10), Full Size (ENTER)
  Group D: Low Conviction (< min_conviction + 10), Reduced Size (REDUCE_SIZE)
"""

import os
import sys
import json
from collections import defaultdict

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add python directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.state_store import StateStore


def analyze_trade_journal(journal_path="data/memory/trade_journal.json", use_demo=False):
    if use_demo:
        journal = [
            {"cohort": "A", "realized_r": 2.10, "net_pnl_usd": 4.20, "fee_usd": 0.10},
            {"cohort": "A", "realized_r": -1.00, "net_pnl_usd": -2.00, "fee_usd": 0.10},
            {"cohort": "A", "realized_r": 1.80, "net_pnl_usd": 3.60, "fee_usd": 0.10},
            {"cohort": "B", "realized_r": 1.50, "net_pnl_usd": 2.25, "fee_usd": 0.08},
            {"cohort": "B", "realized_r": -1.00, "net_pnl_usd": -1.50, "fee_usd": 0.08},
            {"cohort": "C", "realized_r": 1.20, "net_pnl_usd": 2.40, "fee_usd": 0.10},
            {"cohort": "C", "realized_r": -1.00, "net_pnl_usd": -2.00, "fee_usd": 0.10},
            {"cohort": "D", "realized_r": -1.00, "net_pnl_usd": -1.25, "fee_usd": 0.06},
            {"cohort": "D", "realized_r": -0.85, "net_pnl_usd": -1.05, "fee_usd": 0.06},
            {"cohort": "D", "realized_r": 0.40, "net_pnl_usd": 0.50, "fee_usd": 0.06},
        ]
        print("\n[ℹ️] Running in DEMO mode with synthetic trade samples:")
    else:
        journal = StateStore.load(journal_path, default=[])
        if not journal:
            print(f"\n[ℹ️] Trade journal is currently empty ({journal_path}).")
            print("    Waiting for positions to close on-chain to record realized R-multiples.")
            print("    Run with '--demo' to see a preview of the report format: python python/scripts/analyze_cohorts.py --demo\n")
            return

    print(f"\n=========================================================================================")
    print(f"📊 AI-FUTURES-TRADER: COHORT EXPECTANCY & R-MULTIPLE AUDIT REPORT")
    print(f"   Total logged closed trades: {len(journal)}")
    print(f"=========================================================================================\n")

    cohort_stats = defaultdict(lambda: {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "sum_r": 0.0,
        "win_r": [],
        "loss_r": [],
        "total_pnl_usd": 0.0,
        "total_fees_usd": 0.0
    })

    for trade in journal:
        cohort = trade.get("cohort", "UNKNOWN")
        r = float(trade.get("realized_r", 0.0))
        pnl = float(trade.get("net_pnl_usd", 0.0))
        fee = float(trade.get("fee_usd", 0.0))

        c = cohort_stats[cohort]
        c["trades"] += 1
        c["sum_r"] += r
        c["total_pnl_usd"] += pnl
        c["total_fees_usd"] += fee

        if pnl > 0 or r > 0:
            c["wins"] += 1
            c["win_r"].append(r)
        else:
            c["losses"] += 1
            c["loss_r"].append(r)

    # Print summary table
    headers = ["Cohort", "Trades", "Win Rate", "Avg Win (R)", "Avg Loss (R)", "Exp E(R)", "Net PnL ($)"]
    print(f"{headers[0]:<10} | {headers[1]:<8} | {headers[2]:<10} | {headers[3]:<12} | {headers[4]:<12} | {headers[5]:<10} | {headers[6]:<12}")
    print("-" * 89)

    for cohort_id in ["A", "B", "C", "D", "UNKNOWN"]:
        if cohort_id not in cohort_stats and cohort_id != "UNKNOWN":
            continue
        c = cohort_stats.get(cohort_id)
        if not c or c["trades"] == 0:
            continue

        n = c["trades"]
        wr = (c["wins"] / n) * 100.0 if n > 0 else 0.0
        avg_win_r = (sum(c["win_r"]) / len(c["win_r"])) if c["win_r"] else 0.0
        avg_loss_r = (sum(c["loss_r"]) / len(c["loss_r"])) if c["loss_r"] else 0.0
        avg_r = c["sum_r"] / n if n > 0 else 0.0
        pnl_str = f"${c['total_pnl_usd']:+.2f}"

        print(f"{cohort_id:<10} | {n:<8} | {wr:>8.1f}% | {avg_win_r:>10.2f}R | {avg_loss_r:>10.2f}R | {avg_r:>8.2f}R | {pnl_str:>12}")

    print("-" * 89)

    # Recommendations based on empirical data
    d_stats = cohort_stats.get("D")
    if d_stats and d_stats["trades"] >= 3:
        d_exp = d_stats["sum_r"] / d_stats["trades"]
        if d_exp < 0:
            print(f"\n[🚨 RECOMMENDATION] Cohort D has negative expectancy E(R) = {d_exp:+.2f}R over {d_stats['trades']} trades.")
            print("   Action: Consider activating auto-veto in RiskManager for low-conviction REDUCE_SIZE signals.\n")
        else:
            print(f"\n[✅ CONFIRMATION] Cohort D has positive expectancy E(R) = {d_exp:+.2f}R over {d_stats['trades']} trades.")
            print("   Current 0.60x floor and orthogonal sizing are preserving statistical edge.\n")


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    analyze_trade_journal(use_demo=demo)
