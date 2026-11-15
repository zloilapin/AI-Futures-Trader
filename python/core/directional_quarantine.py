import time
import os
from typing import Dict, List, Tuple, Optional
from core.state_store import StateStore

class DirectionalQuarantine:
    """
    INVARIANT 2: Stateful Directional Slot Quarantine on Stop-Out.
    Tracks stopouts (realized_r <= -0.35) by direction ('LONG', 'SHORT').
    
    Rules:
    1. Single Stop-Out Slot Quarantine (90 Minutes / 5400s):
       Whenever 1 trade closes with realized_r <= -0.35 in direction D,
       reduce the directional capacity (effective_directional_cap) by 1 (e.g. from 2 to 1).
    2. Double Stop-Out Directional Freeze (120 Minutes / 7200s):
       If 2 trades in direction D close with realized_r <= -0.35 within a rolling 180-minute window (10800s),
       freeze all new entries in direction D (effective_directional_cap = 0) for 120 minutes (7200s).
    """

    STORAGE_FILE = "data/memory/directional_quarantine.json"

    def __init__(self):
        self._stopouts: Dict[str, List[float]] = {"LONG": [], "SHORT": []}
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        
        # 1. Load persisted quarantine state if present
        data = StateStore.load(self.STORAGE_FILE, default=None)
        if isinstance(data, dict):
            for d in ["LONG", "SHORT"]:
                raw_list = data.get(d, [])
                if isinstance(raw_list, list):
                    self._stopouts[d] = [float(ts) for ts in raw_list if isinstance(ts, (int, float))]
        
        # 2. Backfill from trade_journal.json to guarantee statefulness across cold restarts
        try:
            journal = StateStore.load("data/memory/trade_journal.json", default=[])
            if isinstance(journal, list):
                now = time.time()
                for trade in journal:
                    if not isinstance(trade, dict):
                        continue
                    direction = str(trade.get("direction", "")).upper()
                    if direction not in ["LONG", "SHORT"]:
                        continue
                    realized_r = float(trade.get("realized_r", 0.0) or 0.0)
                    close_ts = float(trade.get("close_time", 0.0) or trade.get("timestamp", 0.0) or 0.0)
                    if realized_r <= -0.35 and close_ts > 0 and (now - close_ts) <= 86400:
                        if close_ts not in self._stopouts[direction]:
                            self._stopouts[direction].append(close_ts)
                            self._stopouts[direction].sort()
        except Exception:
            pass

        self._prune_old()

    def _prune_old(self, max_age_seconds: float = 86400.0, now_ts: Optional[float] = None):
        now = float(now_ts if now_ts is not None else time.time())
        for d in ["LONG", "SHORT"]:
            self._stopouts[d] = [ts for ts in self._stopouts.get(d, []) if (now - ts) <= max_age_seconds]

    def _save(self, now_ts: Optional[float] = None):
        self._prune_old(now_ts=now_ts)
        try:
            StateStore.save(self.STORAGE_FILE, self._stopouts)
        except Exception:
            pass

    def record_stopout(self, direction: str, timestamp: Optional[float] = None, realized_r: float = 0.0):
        """Records a stopout event if realized_r <= -0.35."""
        if realized_r > -0.35:
            return
        self._ensure_loaded()
        dir_up = str(direction).upper()
        if dir_up not in ["LONG", "SHORT"]:
            return
        ts = float(timestamp if timestamp is not None else time.time())
        if ts not in self._stopouts[dir_up]:
            self._stopouts[dir_up].append(ts)
            self._stopouts[dir_up].sort()
            self._save(now_ts=ts)

    def get_effective_directional_cap(
        self,
        direction: str,
        base_cap: int = 2,
        now_ts: Optional[float] = None
    ) -> Tuple[int, str]:
        """
        Calculates the effective directional cap for direction D.
        Returns: (effective_cap, reason_str)
        """
        self._ensure_loaded()
        dir_up = str(direction).upper()
        if dir_up not in ["LONG", "SHORT"]:
            return base_cap, "OK"

        now = float(now_ts if now_ts is not None else time.time())
        # All stopouts in direction D within rolling 180 min (10800s)
        stops_180m = [ts for ts in self._stopouts.get(dir_up, []) if (now - ts) <= 10800.0]
        recent_count = len(stops_180m)
        most_recent_ts = max(stops_180m) if stops_180m else 0.0
        elapsed_since_most_recent = (now - most_recent_ts) if most_recent_ts > 0 else float("inf")

        # 1. Double Stop-Out Directional Freeze (120 Minutes / 7200s)
        if recent_count >= 2 and elapsed_since_most_recent <= 7200.0:
            rem_m = int(max(1, (7200.0 - elapsed_since_most_recent) // 60))
            reason = (
                f"DOUBLE_STOPOUT_FREEZE: {recent_count} stopouts (<= -0.35R) in {dir_up} within 180m. "
                f"All new {dir_up} entries frozen (cap=0, {rem_m}m remaining)."
            )
            return 0, reason

        # 2. Single Stop-Out Slot Quarantine (90 Minutes / 5400s)
        if recent_count >= 1 and elapsed_since_most_recent <= 5400.0:
            rem_m = int(max(1, (5400.0 - elapsed_since_most_recent) // 60))
            effective_cap = max(0, base_cap - 1)
            reason = (
                f"SINGLE_STOPOUT_QUARANTINE: {recent_count} stopout (<= -0.35R) in {dir_up} within 90m. "
                f"{dir_up} cap reduced from {base_cap} to {effective_cap} ({rem_m}m remaining)."
            )
            return effective_cap, reason

        return base_cap, "OK"

    def reset_quarantine(self):
        """Testing utility to clear state."""
        self._stopouts = {"LONG": [], "SHORT": []}
        self._loaded = True
        self._save()

# Global singleton
directional_quarantine = DirectionalQuarantine()
