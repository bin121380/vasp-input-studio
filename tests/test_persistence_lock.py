from __future__ import annotations

import multiprocessing
from pathlib import Path

from app.persistence import file_lock


def _hold_lock(lock_path: str, entered: multiprocessing.synchronize.Event, release: multiprocessing.synchronize.Event) -> None:
    with file_lock(Path(lock_path)):
        entered.set()
        release.wait(timeout=10)


def test_file_lock_serializes_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    target = tmp_path / "state.json"
    first_entered = context.Event()
    first_release = context.Event()
    second_entered = context.Event()
    second_release = context.Event()
    second_release.set()

    first = context.Process(target=_hold_lock, args=(str(target), first_entered, first_release))
    second = context.Process(target=_hold_lock, args=(str(target), second_entered, second_release))
    first.start()
    try:
        assert first_entered.wait(timeout=10)
        second.start()
        assert not second_entered.wait(timeout=0.5)
        first_release.set()
        assert second_entered.wait(timeout=10)
    finally:
        first_release.set()
        second_release.set()
        first.join(timeout=10)
        second.join(timeout=10)
        if first.is_alive():
            first.terminate()
            first.join(timeout=5)
        if second.is_alive():
            second.terminate()
            second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
