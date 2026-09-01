import os
import json
import threading
from typing import Dict, Any

class StateStore:
    """
    Centralized JSON State Store with Atomic Writes.
    Prevents file corruption on unexpected crashes by writing to a temporary file
    and replacing the original file atomically.
    Uses thread locks to prevent race conditions during reads/writes.
    """
    _locks: Dict[str, threading.Lock] = {}

    @classmethod
    def _get_lock(cls, filepath: str) -> threading.Lock:
        if filepath not in cls._locks:
            cls._locks[filepath] = threading.Lock()
        return cls._locks[filepath]

    @classmethod
    def load(cls, filepath: str, default: Any = None) -> Any:
        if default is None:
            default = {}
        if not os.path.exists(filepath):
            return default
            
        with cls._get_lock(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ [StateStore] Failed to load {filepath}: {e}")
                return default

    @classmethod
    def save(cls, filepath: str, data: Any):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        temp_filepath = f"{filepath}.tmp"
        
        with cls._get_lock(filepath):
            try:
                with open(temp_filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                
                # Atomic replace
                os.replace(temp_filepath, filepath)
            except Exception as e:
                print(f"❌ [StateStore] Failed to save {filepath}: {e}")
                if os.path.exists(temp_filepath):
                    try:
                        os.remove(temp_filepath)
                    except:
                        pass
