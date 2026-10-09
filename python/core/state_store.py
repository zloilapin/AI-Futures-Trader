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
    def _normalize_path(cls, filepath: str) -> str:
        if os.path.isabs(filepath):
            return filepath
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if filepath.startswith("python/") or filepath.startswith("python\\"):
            filepath = filepath[7:]
        return os.path.join(base_dir, filepath)

    @classmethod
    def load(cls, filepath: str, default: Any = None) -> Any:
        if default is None:
            default = {}
        filepath = cls._normalize_path(filepath)
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
        filepath = cls._normalize_path(filepath)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        import uuid
        import time
        temp_filepath = f"{filepath}.{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:6]}.tmp"
        
        with cls._get_lock(filepath):
            try:
                with open(temp_filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False, default=str)
                
                # Atomic replace with retry loop for Windows file lock contention
                for attempt in range(5):
                    try:
                        os.replace(temp_filepath, filepath)
                        break
                    except (PermissionError, OSError) as pe:
                        if attempt < 4:
                            time.sleep(0.05 * (attempt + 1))
                        else:
                            # Direct write fallback if os.replace is locked on Windows
                            try:
                                with open(filepath, "w", encoding="utf-8") as f:
                                    json.dump(data, f, indent=4, ensure_ascii=False, default=str)
                                break
                            except Exception:
                                raise pe
            except Exception as e:
                print(f"❌ [StateStore] Failed to save {filepath}: {e}")
            finally:
                if os.path.exists(temp_filepath):
                    try:
                        os.remove(temp_filepath)
                    except Exception:
                        pass
