from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from .config import settings


WORKCHAIN_ENTRIES = (
    "vasp.vasp",
    "vasp.relax",
    "vasp.converge",
    "vasp.bands",
    "vasp.master",
)
AIIDA_STATUS_TTL_SECONDS = 15.0
_STATUS_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
RABBITMQ_CONFIG_PATH = Path("/etc/rabbitmq/rabbitmq.conf")
RABBITMQ_TIMEOUT_RECOMMENDED_MS = 36_000_000_000
RABBITMQ_TIMEOUT_RISK_VERSION = (3, 8, 15)


@dataclass
class AiiDAImportState:
    available: bool
    error: str | None = None


def aiida_backend_enabled() -> bool:
    return bool(settings.enable_aiida_backend)


def get_import_state() -> AiiDAImportState:
    if not aiida_backend_enabled():
        return AiiDAImportState(
            available=False,
            error=(
                "AiiDA support is disabled. Set VASP_STUDIO_ENABLE_AIIDA_BACKEND=1 "
                "and install requirements-aiida.txt to enable it."
            ),
        )
    try:
        import aiida  # noqa: F401
        import aiida_vasp  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive import probe
        return AiiDAImportState(available=False, error=str(exc))
    return AiiDAImportState(available=True)


def _sanitize_config(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if any(token in lowered for token in ("password", "secret", "token")):
            sanitized[key] = "***"
        else:
            sanitized[key] = value
    return sanitized


def clear_aiida_status_cache() -> None:
    _STATUS_CACHE.clear()
    available_workchains.cache_clear()


def _cached_status(kind: str, profile_name: str, producer: Any, *, force_refresh: bool = False) -> dict[str, Any]:
    cache_key = (kind, profile_name)
    now = time.monotonic()
    if not force_refresh:
        cached = _STATUS_CACHE.get(cache_key)
        if cached and now - cached[0] < AIIDA_STATUS_TTL_SECONDS:
            return deepcopy(cached[1])

    payload = producer()
    _STATUS_CACHE[cache_key] = (now, deepcopy(payload))
    return deepcopy(payload)


def _unavailable_status(profile_name: str) -> dict[str, Any]:
    state = get_import_state()
    warning = (
        "AiiDA support is disabled for this installation."
        if not aiida_backend_enabled()
        else "AiiDA or AiiDA-VASP is not installed in the current environment."
    )
    return {
        "available": False,
        "enabled": aiida_backend_enabled(),
        "profile": profile_name,
        "error": state.error,
        "warnings": [warning],
        "codes": [],
        "potcar_families": [],
        "profiles": [],
        "profile_exists": False,
        "has_broker": False,
        "daemon_available": False,
        "daemon_running": False,
        "rabbitmq": {
            "version": None,
            "version_raw": None,
            "consumer_timeout_ms": None,
            "config_path": str(RABBITMQ_CONFIG_PATH),
            "requires_timeout_mitigation": False,
            "timeout_recommended": False,
            "compatibility": "unknown",
            "note": "",
        },
    }


def _parse_version_tuple(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    matches = re.findall(r"\d+", value)
    if not matches:
        return None
    return tuple(int(item) for item in matches[:4])


def _read_rabbitmq_version() -> str | None:
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}\n", "rabbitmq-server"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:  # pragma: no cover - distro specific
        return None
    return result.stdout.strip() or None


def _read_rabbitmq_consumer_timeout_ms() -> int | None:
    if not RABBITMQ_CONFIG_PATH.exists():
        return None
    pattern = re.compile(r"^\s*consumer_timeout\s*=\s*(\d+)\s*$")
    for line in RABBITMQ_CONFIG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        match = pattern.match(body)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def _rabbitmq_runtime_payload(*, has_broker: bool) -> dict[str, Any]:
    version_raw = _read_rabbitmq_version()
    version_tuple = _parse_version_tuple(version_raw)
    consumer_timeout_ms = _read_rabbitmq_consumer_timeout_ms()
    requires_timeout_mitigation = bool(version_tuple and version_tuple >= RABBITMQ_TIMEOUT_RISK_VERSION)
    timeout_recommended = consumer_timeout_ms is not None and consumer_timeout_ms >= RABBITMQ_TIMEOUT_RECOMMENDED_MS

    compatibility = "unknown"
    note = ""
    if not has_broker:
        compatibility = "not_applicable"
        note = "RabbitMQ is not configured for this profile."
    elif requires_timeout_mitigation and not timeout_recommended:
        compatibility = "risk"
        note = (
            "RabbitMQ uses delivery acknowledgement timeouts that can break long-running AiiDA jobs. "
            f"Set consumer_timeout >= {RABBITMQ_TIMEOUT_RECOMMENDED_MS} ms in {RABBITMQ_CONFIG_PATH}."
        )
    elif requires_timeout_mitigation:
        compatibility = "mitigated"
        note = (
            "RabbitMQ requires consumer_timeout mitigation for long AiiDA jobs. "
            "The timeout is configured, but AiiDA may still emit a version warning because it only checks the version."
        )
    elif version_tuple is not None:
        compatibility = "ok"
        note = "RabbitMQ version does not require the long-job timeout mitigation documented by AiiDA."

    return {
        "version": ".".join(str(item) for item in version_tuple[:3]) if version_tuple else None,
        "version_raw": version_raw,
        "consumer_timeout_ms": consumer_timeout_ms,
        "config_path": str(RABBITMQ_CONFIG_PATH),
        "requires_timeout_mitigation": requires_timeout_mitigation,
        "timeout_recommended": timeout_recommended,
        "compatibility": compatibility,
        "note": note,
    }


def _collect_aiida_inventory(profile_name: str) -> dict[str, Any]:
    state = get_import_state()
    if not state.available:
        return _unavailable_status(profile_name)

    from aiida.manage import load_profile
    from aiida.manage.configuration import load_config
    from aiida.orm import Group, InstalledCode, QueryBuilder

    config = load_config(create=True)
    warnings: list[str] = []
    profiles = list(config.profile_names)
    profile_exists = profile_name in profiles

    inventory: dict[str, Any] = {
        "available": True,
        "enabled": aiida_backend_enabled(),
        "profile": profile_name,
        "profile_exists": profile_exists,
        "profiles": profiles,
        "codes": [],
        "potcar_families": [],
        "warnings": warnings,
        "has_broker": False,
        "daemon_available": False,
        "daemon_running": False,
        "daemon_status": "Unknown",
        "rabbitmq": {
            "version": None,
            "version_raw": None,
            "consumer_timeout_ms": None,
            "config_path": str(RABBITMQ_CONFIG_PATH),
            "requires_timeout_mitigation": False,
            "timeout_recommended": False,
            "compatibility": "unknown",
            "note": "",
        },
    }

    if not profile_exists:
        warnings.append("Configured profile does not exist yet.")
        return inventory

    profile = config.get_profile(profile_name)
    inventory["has_broker"] = bool(profile.process_control_backend)
    inventory["daemon_available"] = bool(profile.process_control_backend)

    if not profile.process_control_backend:
        warnings.append("RabbitMQ is not configured, so daemon submission is unavailable.")
        warnings.append("This profile can use run() but cannot use submit() to the daemon.")

    try:
        load_profile(profile_name)
        inventory["codes"] = [
            {
                "label": code.label,
                "full_label": code.full_label,
                "default_calc_job_plugin": code.default_calc_job_plugin,
                "filepath_executable": str(code.filepath_executable),
                "computer": code.computer.label,
            }
            for code in InstalledCode.collection.all()
        ]
        inventory["potcar_families"] = [
            {
                "label": group.label,
                "count": group.count(),
                "type_string": group.type_string,
            }
            for group in QueryBuilder().append(Group, filters={"type_string": "vasp.potcar"}).all(flat=True)
        ]
    except Exception as exc:  # pragma: no cover - environment specific
        warnings.append(f"Profile loading succeeded incompletely: {exc}")

    try:
        from aiida.engine import get_daemon_client

        if profile.process_control_backend:
            client = get_daemon_client(profile_name)
            inventory["daemon_running"] = bool(client.is_daemon_running)
            inventory["daemon_status"] = client.get_status()
        else:
            inventory["daemon_running"] = False
            inventory["daemon_status"] = "No broker configured"
    except Exception as exc:  # pragma: no cover - environment specific
        inventory["daemon_running"] = False
        inventory["daemon_status"] = str(exc)

    inventory["rabbitmq"] = _rabbitmq_runtime_payload(has_broker=inventory["has_broker"])
    if inventory["rabbitmq"]["compatibility"] == "risk":
        warnings.append(inventory["rabbitmq"]["note"])

    return inventory


def aiida_inventory(profile_name: str, *, force_refresh: bool = False) -> dict[str, Any]:
    return _cached_status(
        "inventory",
        profile_name,
        lambda: _collect_aiida_inventory(profile_name),
        force_refresh=force_refresh,
    )


def _collect_aiida_status(profile_name: str) -> dict[str, Any]:
    state = get_import_state()
    if not state.available:
        unavailable = _unavailable_status(profile_name)
        unavailable["workchains"] = []
        return unavailable

    from aiida import __version__ as aiida_version
    from aiida.manage import load_profile
    from aiida.manage.configuration import load_config
    from aiida.orm import Computer, QueryBuilder, User
    from aiida.plugins import WorkflowFactory

    config = load_config(create=True)
    inventory = _collect_aiida_inventory(profile_name)
    status: dict[str, Any] = {
        **inventory,
        "enabled": aiida_backend_enabled(),
        "aiida_version": aiida_version,
        "default_profile": config.default_profile_name,
        "config_dir": str(Path(config.dirpath)),
        "workchains": available_workchains(),
    }

    if not inventory.get("profile_exists"):
        return status

    profile = config.get_profile(profile_name)
    status["storage_backend"] = profile.storage_backend
    status["storage_config"] = _sanitize_config(profile.storage_config)
    status["storage_path"] = profile.storage_config.get("filepath")
    status["process_control_backend"] = profile.process_control_backend
    status["process_control_config"] = _sanitize_config(profile.process_control_config)

    try:
        load_profile(profile_name)
        qb = QueryBuilder()
        status["computer_count"] = qb.append(Computer).count()
        qb = QueryBuilder()
        status["user_count"] = qb.append(User).count()
    except Exception as exc:  # pragma: no cover - environment specific
        status["warnings"] = [*status.get("warnings", []), f"Profile loading succeeded incompletely: {exc}"]
        status["computer_count"] = None
        status["user_count"] = None

    try:
        # This confirms the plugin entry points resolve in the current environment.
        status["workflow_factory_check"] = {
            entry: WorkflowFactory(entry).__name__ for entry in WORKCHAIN_ENTRIES
        }
    except Exception as exc:  # pragma: no cover - plugin probe
        status["warnings"] = [*status.get("warnings", []), f"Workflow entry point probe failed: {exc}"]

    return status


def aiida_status(profile_name: str, *, force_refresh: bool = False) -> dict[str, Any]:
    return _cached_status(
        "full",
        profile_name,
        lambda: _collect_aiida_status(profile_name),
        force_refresh=force_refresh,
    )


@lru_cache(maxsize=1)
def available_workchains() -> list[dict[str, str]]:
    state = get_import_state()
    if not state.available:
        return []

    from aiida.plugins import WorkflowFactory

    workchains: list[dict[str, str]] = []
    for entry in WORKCHAIN_ENTRIES:
        workchain = WorkflowFactory(entry)
        doc = (getattr(workchain, "__doc__", "") or "").strip().splitlines()
        summary = doc[0].strip() if doc else ""
        workchains.append(
            {
                "entry_point": entry,
                "class_name": workchain.__name__,
                "module": workchain.__module__,
                "summary": summary,
            }
        )
    return workchains
