import os
import json
import pytest
from core.state_store import StateStore

def test_state_store_disaster_recovery_from_backup(tmp_path):
    target_file = str(tmp_path / "positions.json")
    
    # Initial good save
    initial_data = {"SOL-USD": {"direction": "SHORT", "entry": 99.72}}
    StateStore.save(target_file, initial_data)
    
    # Save an update to ensure .bak exists with initial_data
    updated_data = {"SOL-USD": {"direction": "SHORT", "entry": 99.72}, "ETH-USD": {"direction": "LONG", "entry": 2300.0}}
    StateStore.save(target_file, updated_data)
    
    bak_file = f"{StateStore._normalize_path(target_file)}.bak"
    assert os.path.exists(bak_file)
    
    # Simulate corruption / 0-byte wipe of primary file
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("")  # 0 bytes empty
    
    # StateStore.load must recover from .bak rather than returning empty default
    recovered = StateStore.load(target_file)
    assert recovered is not None
    assert "SOL-USD" in recovered
    assert recovered["SOL-USD"]["direction"] == "SHORT"

def test_state_store_reentrancy_no_deadlock(tmp_path):
    target_file = str(tmp_path / "reentrant.json")
    
    # Acquire the lock explicitly and then call save/load from same thread
    norm_path = StateStore._normalize_path(target_file)
    with StateStore._get_lock(norm_path):
        StateStore.save(target_file, {"status": "ok"})
        loaded = StateStore.load(target_file)
        assert loaded == {"status": "ok"}
