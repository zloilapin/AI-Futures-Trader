import json
import os
from collections import defaultdict
from typing import Dict, Any

class DiagnosticTracker:
    """
    Tracks and categorizes the reasons for signal rejections (HOLD / VETO) over time.
    Provides a statistical view of the decision funnel to identify bottlenecks.
    """
    def __init__(self, filepath="data/memory/diagnostics.json"):
        self.filepath = filepath
        self.stats = {
            "total_scans": 0,
            "trades_executed": 0,
            "execution_failed": 0,
            "rejections": defaultdict(int)
        }
        self._load_stats()

    def _load_stats(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.stats["total_scans"] = data.get("total_scans", 0)
                    self.stats["trades_executed"] = data.get("trades_executed", 0)
                    self.stats["execution_failed"] = data.get("execution_failed", 0)
                    rejections = data.get("rejections", {})
                    self.stats["rejections"] = defaultdict(int, rejections)
            except Exception:
                pass

    def _save_stats(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, indent=4)
        except Exception:
            pass

    def record_scan(self):
        self.stats["total_scans"] += 1
        self._save_stats()

    def record_rejection(self, category: str):
        self.stats["rejections"][category] += 1
        self._save_stats()

    def record_trade(self):
        self.stats["trades_executed"] += 1
        self._save_stats()
        
    def record_execution_failed(self):
        self.stats["execution_failed"] += 1
        self._save_stats()

    def get_summary_text(self) -> str:
        s = self.stats
        total = s["total_scans"]
        trades = s["trades_executed"]
        fails = s["execution_failed"]
        
        lines = [
            "📊 *SIGNAL STATS*",
            "-----------------------",
            f"Total Scans: {total}",
            f"Trades Executed: {trades}",
            f"Execution Fails: {fails}",
            "",
            "*Rejected:*",
            "-----------------------"
        ]
        
        # Predefined categories we always want to show
        expected_categories = [
            "CEO_HOLD",
            "MARKET_CHOPPY",
            "LOW_CONFIDENCE",
            "LOW_RR",
            "SPREAD",
            "SPREAD_TOO_HIGH",
            "MIN_NOTIONAL",
            "MAX_MARGIN",
            "NO_SIGNAL",
            "FETCH_ERROR"
        ]
        
        # Create a unified dictionary of all rejections (expected + any others dynamically added)
        all_rejections = {cat: 0 for cat in expected_categories}
        for cat, count in s["rejections"].items():
            all_rejections[cat] = count
            
        # Sort rejections by count descending, then alphabetically
        sorted_rejections = sorted(all_rejections.items(), key=lambda item: (-item[1], item[0]))
        
        for category, count in sorted_rejections:
            lines.append(f"`{category.ljust(18)} {count}`")
            
        return "\n".join(lines)

# Global singleton
tracker = DiagnosticTracker()
