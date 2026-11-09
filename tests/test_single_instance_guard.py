import sys
import os
import pytest
from datetime import datetime


def test_acquire_single_instance_lock_logic(tmp_path):
    lock_path = str(tmp_path / "bot.lock")
    
    # 1. Acquire first lock
    f1 = open(lock_path, "a+")
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(f1.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f1.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # 2. Attempt second lock on same file in another handle -> must fail
    f2 = open(lock_path, "a+")
    locked_out = False
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f2.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        locked_out = True
    finally:
        f2.close()

    assert locked_out is True, "Second process must be locked out by OS file lock!"

    # 3. Release first lock
    f1.close()

    # 4. Now acquire lock again -> must succeed
    f3 = open(lock_path, "a+")
    acquired = False
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f3.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f3.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    finally:
        f3.close()

    assert acquired is True, "Lock must be re-acquirable once released"

