import os
import json
import threading
from typing import Dict, Any

class StateStore:
    """
    Centralized JSON State Store with Atomic Writes and Shadow Backup Recovery.
    Prevents file corruption on unexpected crashes by writing to a temporary file
    and replacing the original file atomically.
    Uses re-entrant thread locks (RLock) to prevent race conditions during reads/writes.
    Maintains .bak shadow backups to guarantee disaster recovery if primary file is corrupted.
    """
    _locks: Dict[str, threading.RLock] = {}
    _locks_mutex: threading.RLock = threading.RLock()
    _base_dir_override: Any = None

    @classmethod
    def _normalize_path(cls, filepath: str) -> str:
        if cls._base_dir_override:
            base_dir = cls._base_dir_override
            norm_f = os.path.normpath(filepath)
            if os.path.isabs(filepath):
                if "data" in norm_f:
                    parts = norm_f.split(os.sep + "data" + os.sep)
                    if len(parts) > 1:
                        target = os.path.join(base_dir, "data", parts[-1])
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        return os.path.normcase(os.path.abspath(target))
                return os.path.normcase(os.path.abspath(filepath))
            if filepath.startswith("python/") or filepath.startswith("python\\"):
                filepath = filepath[7:]
            target = os.path.join(base_dir, filepath)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            return os.path.normcase(os.path.abspath(target))

        if os.path.isabs(filepath):
            return os.path.normcase(os.path.abspath(filepath))
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if filepath.startswith("python/") or filepath.startswith("python\\"):
            filepath = filepath[7:]
        return os.path.normcase(os.path.abspath(os.path.join(base_dir, filepath)))

    @classmethod
    def _get_lock(cls, filepath: str) -> threading.RLock:
        norm_key = cls._normalize_path(filepath)
        with cls._locks_mutex:
            if norm_key not in cls._locks:
                cls._locks[norm_key] = threading.RLock()
            return cls._locks[norm_key]

    @classmethod
    def load(cls, filepath: str, default: Any = None) -> Any:
        if default is None:
            default = {}
        norm_path = cls._normalize_path(filepath)
        if not os.path.exists(norm_path) and not os.path.exists(f"{norm_path}.bak"):
            return default
            
        with cls._get_lock(norm_path):
            # Attempt 1: Load primary file if present and non-empty
            if os.path.exists(norm_path):
                try:
                    with open(norm_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        if content.strip():
                            return json.loads(content)
                        else:
                            print(f"⚠️ [StateStore] Primary file is empty (0 bytes): {norm_path}")
                except Exception as e:
                    print(f"⚠️ [StateStore] Failed to load primary {norm_path}: {e}")

            # Attempt 2: Recover from .bak shadow backup
            bak_path = f"{norm_path}.bak"
            if os.path.exists(bak_path):
                try:
                    with open(bak_path, "r", encoding="utf-8") as f:
                        bak_content = f.read()
                        if bak_content.strip():
                            recovered = json.loads(bak_content)
                            print(f"🛡️ [StateStore] Successfully recovered state from backup: {bak_path}")
                            return recovered
                except Exception as bak_e:
                    print(f"❌ [StateStore] Backup file also corrupt {bak_path}: {bak_e}")

            return default

    @classmethod
    def save(cls, filepath: str, data: Any):
        norm_path = cls._normalize_path(filepath)
        os.makedirs(os.path.dirname(norm_path), exist_ok=True)
        import uuid
        import time
        import shutil
        temp_filepath = f"{norm_path}.{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:6]}.tmp"
        bak_filepath = f"{norm_path}.bak"
        
        with cls._get_lock(norm_path):
            try:
                with open(temp_filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False, default=str)
                
                # Update shadow backup before replacing if existing file is non-empty
                if os.path.exists(norm_path):
                    try:
                        if os.path.getsize(norm_path) > 0:
                            shutil.copy2(norm_path, bak_filepath)
                    except Exception:
                        pass

                # Atomic replace with retry loop for Windows file lock contention
                for attempt in range(5):
                    try:
                        os.replace(temp_filepath, norm_path)
                        break
                    except (PermissionError, OSError) as pe:
                        if attempt < 4:
                            time.sleep(0.05 * (attempt + 1))
                        else:
                            # Direct write fallback if os.replace is locked on Windows
                            try:
                                with open(norm_path, "w", encoding="utf-8") as f:
                                    json.dump(data, f, indent=4, ensure_ascii=False, default=str)
                                break
                            except Exception:
                                raise pe
            except Exception as e:
                print(f"❌ [StateStore] Failed to save {norm_path}: {e}")
            finally:
                if os.path.exists(temp_filepath):
                    try:
                        os.remove(temp_filepath)
                    except Exception:
                        pass
