from __future__ import annotations

import shutil
from pathlib import Path

from .config import settings


RUNNER_ASSET_NAMES = ("run_step.sh", "chgcar_sum.py")


def bundled_runner_script_dir() -> Path:
    return settings.runner_script_dir


def bundled_runner_script_path(name: str = "run_step.sh") -> Path:
    return bundled_runner_script_dir() / name


def workspace_runner_script_dir() -> Path:
    return settings.workspace_root / "scripts"


def workspace_runner_script_path(name: str = "run_step.sh") -> Path:
    return workspace_runner_script_dir() / name


def ensure_workspace_runner_assets() -> list[Path]:
    source_dir = bundled_runner_script_dir()
    target_dir = workspace_runner_script_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    synced: list[Path] = []
    for name in RUNNER_ASSET_NAMES:
        source_path = source_dir / name
        if not source_path.exists():
            continue
        target_path = target_dir / name
        source_bytes = source_path.read_bytes()
        if not target_path.exists() or target_path.read_bytes() != source_bytes:
            shutil.copy2(source_path, target_path)
        target_path.chmod(source_path.stat().st_mode)
        synced.append(target_path)
    return synced
