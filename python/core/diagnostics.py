import json
import os
from collections import defaultdict
from typing import Dict, Any

class DiagnosticTracker:
    """
    Tracks and categorizes the reasons for signal rejections (HOLD / VETO) over time.
    Provides a statistical view of the decision funnel and closed trade performance.
    """
    def __init__(self, filepath="data/memory/diagnostics.json"):
        self.filepath = filepath
        self.stats = {
            "total_scans": 0,
            "trades_executed": 0,
            "execution_failed": 0,
            "win_trades": 0,
            "loss_trades": 0,
            "breakeven_trades": 0,
            "total_pnl_usd": 0.0,
            "rejections": defaultdict(int)
        }
        self._load_stats()

    def _load_stats(self):
        from core.state_store import StateStore
        data = StateStore.load(self.filepath)
        if data and isinstance(data, dict):
            self.stats["total_scans"] = data.get("total_scans", 0)
            self.stats["trades_executed"] = data.get("trades_executed", 0)
            self.stats["execution_failed"] = data.get("execution_failed", 0)
            self.stats["win_trades"] = data.get("win_trades", 0)
            self.stats["loss_trades"] = data.get("loss_trades", 0)
            self.stats["breakeven_trades"] = data.get("breakeven_trades", 0)
            self.stats["total_pnl_usd"] = float(data.get("total_pnl_usd", 0.0) or 0.0)
            
            raw_rejections = data.get("rejections", {})
            rejections = defaultdict(int)
            if isinstance(raw_rejections, dict):
                for k, v in raw_rejections.items():
                    norm_k = str(k).strip().replace(' ', '_').upper()
                    try:
                        rejections[norm_k] += int(v)
                    except (ValueError, TypeError):
                        pass
            self.stats["rejections"] = rejections

    def _save_stats(self):
        from core.state_store import StateStore
        save_payload = dict(self.stats)
        save_payload["rejections"] = dict(self.stats["rejections"])
        StateStore.save(self.filepath, save_payload)

    def record_scan(self):
        self.stats["total_scans"] += 1
        self._save_stats()

    def record_rejection(self, category: str):
        cat = str(category or "UNKNOWN").strip().replace(' ', '_').upper()
        self.stats["rejections"][cat] += 1
        self._save_stats()

    def record_trade(self):
        self.stats["trades_executed"] += 1
        self._save_stats()
        
    def record_execution_failed(self):
        self.stats["execution_failed"] += 1
        self._save_stats()

    def record_closed_trade(self, outcome: str, pnl_usd: float = 0.0):
        out = str(outcome or "BREAK_EVEN").strip().upper()
        if out == "WIN" or pnl_usd > 0.001:
            self.stats["win_trades"] = self.stats.get("win_trades", 0) + 1
        elif out == "LOSS" or pnl_usd < -0.001:
            self.stats["loss_trades"] = self.stats.get("loss_trades", 0) + 1
        else:
            self.stats["breakeven_trades"] = self.stats.get("breakeven_trades", 0) + 1

        current_pnl = float(self.stats.get("total_pnl_usd", 0.0) or 0.0)
        self.stats["total_pnl_usd"] = round(current_pnl + float(pnl_usd or 0.0), 4)
        self._save_stats()

    def get_summary_text(self) -> str:
        s = self.stats
        total = s.get("total_scans", 0)
        trades = s.get("trades_executed", 0)
        fails = s.get("execution_failed", 0)
        wins = s.get("win_trades", 0)
        losses = s.get("loss_trades", 0)
        bes = s.get("breakeven_trades", 0)
        pnl = s.get("total_pnl_usd", 0.0)
        
        closed_total = wins + losses + bes
        win_rate = (wins / closed_total * 100) if closed_total > 0 else 0.0
        
        lines = [
            "📊 *SIGNAL & TRADE STATS*",
            "-----------------------",
            f"Total Scans: {total}",
            f"Trades Executed: {trades} (Failed: {fails})",
            f"Closed Trades: {closed_total} (W: {wins} | L: {losses} | BE: {bes})",
            f"Win Rate: {win_rate:.1f}%",
            f"Realized PnL: ${pnl:+,.2f}",
            "",
            "*Rejected Signals:*",
            "-----------------------"
        ]
        
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
            "FETCH_ERROR",
            "AGENT_ERROR",
            "DATA_INVALID",
            "WAIT_FOR_PULLBACK",
            "STRATEGY_GUARD_VETO",
            "SLIPPAGE_VETO"
        ]
        
        all_rejections = {cat: 0 for cat in expected_categories}
        for cat, count in s.get("rejections", {}).items():
            all_rejections[cat] = count
            
        sorted_rejections = sorted(all_rejections.items(), key=lambda item: (-item[1], item[0]))
        
        for category, count in sorted_rejections:
            if count > 0 or category in expected_categories[:6]:
                lines.append(f"`{category.ljust(22)} {count}`")
            
        return "\n".join(lines)

# Global singleton
tracker = DiagnosticTracker()
