from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import shutil
import subprocess
import threading
import fnmatch
from datetime import datetime
from functools import lru_cache, wraps
from pathlib import Path, PurePosixPath
from typing import Any

import psutil
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from .aiida_service import aiida_backend_enabled, aiida_inventory, aiida_status, available_workchains
from .aiida_submit import (
    AiiDASubmissionError,
    aiida_remote_path,
    aiida_job_log_payload,
    preview_aiida_submission,
    refresh_aiida_job,
    request_aiida_pause,
    sync_aiida_job_to_workspace,
    stop_aiida_process,
    submit_aiida_submission,
    supports_aiida_submission,
)
from .config import settings
from .input_generation import generate_input_content
from .materials import (
    AUTO_REGEN_REASON_CUSTOM_BAND_PATH,
    AUTO_REGEN_REASON_MANUAL_OVERRIDE,
    AUTO_REGEN_REASON_UNTRACKED,
    BAND_SUBMISSION_GUARDED_FILES,
    DOWNSTREAM_GENERATED_FILE_CANDIDATES,
    GENERATED_FILE_CANDIDATES,
    MANAGED_FILE_CANDIDATES,
    PREVIEW_FILE_CANDIDATES,
    WRITABLE_FILE_CANDIDATES,
    defaults_for_material_class,
    effective_phonon_supercell,
    file_generation_state,
    file_generation_states,
    initialize_project_inputs,
    material_form_payload,
    max_potcar_enmax,
    parse_band_conf_supercell,
    parse_structure_text,
    preview_files,
    record_generated_file,
    refresh_generated_inputs_if_needed,
    refresh_structure_inputs,
    save_material_settings,
    sync_metadata_from_band_conf,
    sync_metadata_from_kpoints_scf,
    writable_files,
)
from .persistence import atomic_write_text, file_lock, read_json, write_json
from .runner_assets import bundled_runner_script_path, ensure_workspace_runner_assets
from .schedulers import (
    SchedulerError,
    normalize_scheduler_kind,
    preview_remote_submission,
    refresh_remote_job,
    submit_remote_job,
)
from .structure_resolution import (
    PRIMITIVE_REGENERATED_INPUTS,
    downstream_structure_path,
    primitive_cell_available,
    relative_system_path,
    relax_contcar_path,
    resolved_mesh_kpoints_path,
    relax_primitive_log_path,
    relax_primitive_path,
    relax_primitive_symmetry_path,
    resolved_structure_path,
)


ROOT = settings.workspace_root
SYSTEMS_DIR = ROOT / "systems"
RESULTS_DIR = ROOT / "results"
SCRIPTS_DIR = ROOT / "scripts"
RUNTIME_DIR = settings.runtime_dir
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
JOBS_FILE = RUNTIME_DIR / "jobs.json"
PROFILES_FILE = RUNTIME_DIR / "submission_profiles.json"
ensure_workspace_runner_assets()

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
ASSET_VERSION = str(
    max(
        (Path(__file__).resolve().parents[1] / "static" / "app.js").stat().st_mtime_ns,
        (Path(__file__).resolve().parents[1] / "static" / "styles.css").stat().st_mtime_ns,
    )
)


def local_legacy_runner_path() -> Path:
    return bundled_runner_script_path("run_step.sh")


def local_legacy_runner_issue() -> str | None:
    run_step = local_legacy_runner_path()
    if not run_step.exists():
        return f"Bundled local runner is missing: {run_step}"
    if not settings.vasp_env_script or not settings.vasp_env_script.exists():
        return "Local VASP launcher is not configured. Set VASP_STUDIO_VASP_ENV_SCRIPT to enable local submissions."
    return None


def summarize_audit_items(items: list[dict[str, str]]) -> dict[str, Any]:
    counts = {"pass": 0, "review": 0, "fail": 0}
    for item in items:
        status = str(item.get("status") or "review")
        counts[status] = counts.get(status, 0) + 1
    overall = "fail" if counts.get("fail") else "review" if counts.get("review") else "pass"
    return {"overall": overall, "counts": counts, "items": items}


def _compact_status_detail(value: Any, *, limit: int = 260) -> str:
    if isinstance(value, dict):
        detail = ", ".join(f"{key}: {val}" for key, val in value.items() if val not in (None, "", []))
    else:
        detail = str(value or "")
    detail = " ".join(detail.split())
    if len(detail) > limit:
        return detail[: limit - 3].rstrip() + "..."
    return detail


def installation_audit(backend: dict[str, Any] | None = None) -> dict[str, Any]:
    aiida_state = backend or aiida_status(settings.aiida_profile_name)
    env_file = settings.project_root / ".env.local"
    troubleshooting_path = settings.project_root / "INSTALLATION_TROUBLESHOOTING.md"
    items: list[dict[str, str]] = []

    def add(title: str, status: str, note: str) -> None:
        items.append({"title": title, "status": status, "note": note})

    add(
        "Installer config file",
        "pass" if env_file.exists() else "fail",
        f"Runtime config file is present at {env_file}." if env_file.exists() else f"Expected runtime config file is missing: {env_file}",
    )

    run_step = local_legacy_runner_path()
    add(
        "Bundled runner asset",
        "pass" if run_step.exists() else "fail",
        f"Bundled local runner is present at {run_step}." if run_step.exists() else f"Bundled local runner is missing: {run_step}",
    )

    if settings.vasp_env_script:
        add(
            "Local VASP launcher",
            "pass" if settings.vasp_env_script.exists() else "fail",
            f"VASP env script is configured at {settings.vasp_env_script}." if settings.vasp_env_script.exists() else f"Configured VASP env script is missing: {settings.vasp_env_script}",
        )
    else:
        add(
            "Local VASP launcher",
            "review",
            "Local VASP submission is not configured. Set VASP_STUDIO_VASP_ENV_SCRIPT to a shell file that exports MPI_CMD and VASP_CMD.",
        )

    if settings.vaspkit_cmd:
        add(
            "VASPKIT helper",
            "pass" if settings.vaspkit_cmd.exists() else "fail",
            f"VASPKIT is configured at {settings.vaspkit_cmd}." if settings.vaspkit_cmd.exists() else f"Configured VASPKIT binary is missing: {settings.vaspkit_cmd}",
        )
    else:
        add(
            "VASPKIT helper",
            "review",
            "VASPKIT is not configured. K-path generation can still fall back, but VASPKIT-specific helpers are unavailable.",
        )

    if settings.potcar_archive_pbe_64:
        add(
            "POTCAR archive",
            "pass" if settings.potcar_archive_pbe_64.exists() else "fail",
            f"POTCAR archive is configured at {settings.potcar_archive_pbe_64}." if settings.potcar_archive_pbe_64.exists() else f"Configured POTCAR archive is missing: {settings.potcar_archive_pbe_64}",
        )
    else:
        add(
            "POTCAR archive",
            "review",
            "POTCAR archive is not configured. POTCAR Mapping will fall back to plain element names and will not expose sv/pv variants.",
        )

    if not aiida_backend_enabled():
        add(
            "AiiDA extension",
            "review",
            "AiiDA backend is disabled. Set VASP_STUDIO_ENABLE_AIIDA_BACKEND=1 and install requirements-aiida.txt if you need the AiiDA workflow.",
        )
    elif not aiida_state.get("available"):
        add(
            "AiiDA extension",
            "fail",
            f"AiiDA backend is enabled but unavailable: {aiida_state.get('error') or 'import probe failed.'}",
        )
    else:
        add(
            "AiiDA extension",
            "pass",
            f"AiiDA backend is enabled and importable for profile {aiida_state.get('profile') or settings.aiida_profile_name}.",
        )

        profile_exists = bool(aiida_state.get("profile_exists"))
        add(
            "AiiDA profile",
            "pass" if profile_exists else "fail",
            f"Configured profile {aiida_state.get('profile') or settings.aiida_profile_name} exists." if profile_exists else f"Configured profile {aiida_state.get('profile') or settings.aiida_profile_name} does not exist yet.",
        )

        if profile_exists:
            if bool(aiida_state.get("has_broker")):
                daemon_status = "pass" if bool(aiida_state.get("daemon_running")) else "fail"
                daemon_detail = _compact_status_detail(aiida_state.get("daemon_status"))
                daemon_note = (
                    f"AiiDA daemon is running for profile {aiida_state.get('profile') or settings.aiida_profile_name}."
                    if bool(aiida_state.get("daemon_running"))
                    else (
                        f"AiiDA daemon is not running for profile {aiida_state.get('profile') or settings.aiida_profile_name}."
                        + (f" Last daemon probe: {daemon_detail}" if daemon_detail else "")
                    )
                )
            else:
                daemon_status = "fail"
                daemon_note = "AiiDA profile has no broker/process-control backend configured, so daemon submission is unavailable."
            add("AiiDA daemon", daemon_status, daemon_note)

            codes = aiida_state.get("codes") or []
            add(
                "AiiDA VASP codes",
                "pass" if codes else "fail",
                f"Detected {len(codes)} registered AiiDA code(s)." if codes else "No AiiDA VASP codes are registered for this installation.",
            )

            potcar_families = aiida_state.get("potcar_families") or []
            add(
                "AiiDA POTCAR family",
                "pass" if potcar_families else "fail",
                f"Detected {len(potcar_families)} AiiDA POTCAR family entry/entries." if potcar_families else "No AiiDA POTCAR family is available for this installation.",
            )

    audit = summarize_audit_items(items)
    audit["troubleshooting_path"] = str(troubleshooting_path)
    return audit


def backend_features(backend: dict[str, Any] | None = None) -> dict[str, Any]:
    aiida_state = backend or aiida_status(settings.aiida_profile_name)
    return {
        "aiida_enabled": aiida_backend_enabled(),
        "aiida_available": bool(aiida_state.get("available")),
        "local_runner_script_available": local_legacy_runner_path().exists(),
        "local_submission_available": local_legacy_runner_issue() is None,
        "vaspkit_available": bool(settings.vaspkit_cmd and settings.vaspkit_cmd.exists()),
        "potcar_generation_available": bool(settings.potcar_archive_pbe_64 and settings.potcar_archive_pbe_64.exists()),
    }


def local_cpu_resources() -> dict[str, int | None]:
    physical = psutil.cpu_count(logical=False)
    logical = psutil.cpu_count(logical=True)
    affinity = None
    if hasattr(os, "sched_getaffinity"):
        try:
            affinity = len(os.sched_getaffinity(0))
        except OSError:
            affinity = None
    available = affinity or logical or physical or 1
    return {
        "physical_cores": int(physical) if physical else None,
        "logical_cores": int(logical) if logical else None,
        "available_cores": int(available),
        "default_mpi_np": int(settings.vasp_mpi_np),
    }


def format_cpu_resource_note(resources: dict[str, int | None]) -> str:
    available = resources.get("available_cores") or 1
    physical = resources.get("physical_cores")
    logical = resources.get("logical_cores")
    if physical and logical:
        return f"Detected local CPU: {available} core(s) available to this process, {physical} physical / {logical} logical core(s) on the host."
    if logical:
        return f"Detected local CPU: {available} core(s) available to this process, {logical} logical core(s) on the host."
    return f"Detected local CPU: {available} core(s) available to this process."


TRANSITION_METALS = {
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
}

ATOMIC_MASSES = {
    "H": 1.00794,
    "He": 4.002602,
    "Li": 6.941,
    "Be": 9.012182,
    "B": 10.811,
    "C": 12.0107,
    "N": 14.0067,
    "O": 15.9994,
    "F": 18.9984032,
    "Ne": 20.1797,
    "Na": 22.98976928,
    "Mg": 24.3050,
    "Al": 26.9815386,
    "Si": 28.0855,
    "P": 30.973762,
    "S": 32.065,
    "Cl": 35.453,
    "Ar": 39.948,
    "K": 39.0983,
    "Ca": 40.078,
    "Sc": 44.955912,
    "Ti": 47.867,
    "V": 50.9415,
    "Cr": 51.9961,
    "Mn": 54.93804391,
    "Fe": 55.845,
    "Co": 58.933194,
    "Ni": 58.6934,
    "Cu": 63.546,
    "Zn": 65.38,
    "Ga": 69.723,
    "Ge": 72.64,
    "As": 74.92160,
    "Se": 78.96,
    "Br": 79.904,
    "Kr": 83.798,
    "Rb": 85.4678,
    "Sr": 87.62,
    "Y": 88.90585,
    "Zr": 91.224,
    "Nb": 92.90638,
    "Mo": 95.96,
    "Tc": 98.0,
    "Ru": 101.07,
    "Rh": 102.90550,
    "Pd": 106.42,
    "Ag": 107.8682,
    "Cd": 112.411,
    "In": 114.818,
    "Sn": 118.710,
    "Sb": 121.760,
    "Te": 127.60,
    "I": 126.90447,
    "Xe": 131.293,
    "Cs": 132.9054519,
    "Ba": 137.327,
    "La": 138.90547,
    "Ce": 140.116,
    "Pr": 140.90765,
    "Nd": 144.242,
    "Sm": 150.36,
    "Eu": 151.964,
    "Gd": 157.25,
    "Tb": 158.92535,
    "Dy": 162.500,
    "Ho": 164.93032,
    "Er": 167.259,
    "Tm": 168.93421,
    "Yb": 173.054,
    "Lu": 174.9668,
    "Hf": 178.49,
    "Ta": 180.94788,
    "W": 183.84,
    "Re": 186.207,
    "Os": 190.23,
    "Ir": 192.217,
    "Pt": 195.084,
    "Au": 196.966569,
    "Hg": 200.59,
    "Tl": 204.3833,
    "Pb": 207.2,
    "Bi": 208.98040,
}

LOCAL_RESUMABLE_LEGACY_STEPS = {"relax", "scf"}
ACTIVE_JOB_STATES = {"running", "running_or_partial", "submitted_remote", "queued", "pausing", "stopping"}
TERMINAL_JOB_STATES = {"finished", "failed", "paused", "stopped"}
BACKEND_ISSUE_PATTERN = re.compile(
    r"(database|broker|rabbitmq|connection|operationalerror|consumer[_ ]timeout|transport)",
    re.IGNORECASE,
)
INCAR_CONSISTENCY_FIELDS = (
    ("ISPIN", "spin polarization", "block"),
    ("LHFCALC", "hybrid-functional mode", "block"),
    ("AEXX", "hybrid exact-exchange fraction", "block"),
    ("HFSCREEN", "hybrid screening parameter", "block"),
    ("GGA", "GGA family", "block"),
    ("METAGGA", "meta-GGA family", "block"),
    ("LDAU", "DFT+U enable flag", "block"),
    ("LDAUL", "LDAUL", "block"),
    ("LDAUU", "LDAUU", "block"),
    ("LDAUJ", "LDAUJ", "block"),
    ("ENCUT", "plane-wave cutoff", "review"),
    ("LASPH", "LASPH", "review"),
)
INCAR_COMPARISON_DEFAULTS = {
    "ISPIN": "1",
    "LHFCALC": "FALSE",
    "LDAU": "FALSE",
    "LASPH": "FALSE",
}
RUNTIME_PREVIEW_PATTERN = re.compile(
    r"runs/(dos|scf)/(PDOS_[^/]+\.dat|IPDOS_[^/]+\.dat|vaspkit_pdos\.log)$|"
    r"runs/relax/(PRIMCELL\.vasp|SYMMETRY|vaspkit_primitive\.log)$",
    re.IGNORECASE,
)
JOB_RESULT_ARCHIVE_DIRNAME = ".job_results"
LOCAL_ATTEMPT_WORKDIR_DIRNAME = ".attempt_workdirs"
JOB_ARCHIVE_PATTERNS: dict[str, tuple[str, ...]] = {
    "relax": ("CONTCAR", "OUTCAR", "OSZICAR", "PRIMCELL.vasp", "SYMMETRY", "vaspkit_primitive.log", "exit_code.json", "launcher.log", "run_manifest.json"),
    "scf": ("OUTCAR", "OSZICAR", "DOSCAR", "FERMI_ENERGY", "exit_code.json", "launcher.log", "run_manifest.json"),
    "dos": ("DOSCAR", "OUTCAR", "FERMI_ENERGY", "PDOS_*.dat", "IPDOS_*.dat", "vaspkit_pdos.log", "exit_code.json", "launcher.log", "run_manifest.json"),
    "band": (
        "EIGENVAL",
        "BAND_GAP",
        "OUTCAR",
        "FERMI_ENERGY",
        "KLABELS",
        "KPOINTS",
        "KPOINTS_OPT",
        "REFORMATTED_BAND*.dat",
        "PROCAR_OPT",
        "exit_code.json",
        "launcher.log",
        "run_manifest.json",
    ),
    "elastic": ("OUTCAR", "ELASTIC_TENSOR", "vaspkit_elastic.log", "exit_code.json", "launcher.log", "run_manifest.json"),
    "charge": ("ACF.dat", "BORN", "bader.log", "chgsum.log", "exit_code.json", "launcher.log", "run_manifest.json"),
    "phonon": (
        "band.yaml",
        "band.pdf",
        "mesh.yaml",
        "total_dos.dat",
        "projected_dos.dat",
        "FORCE_SETS",
        "BORN",
        "band.conf",
        "phonopy.yaml",
        "phonopy_disp.yaml",
        "phonopy_plot.log",
        "phonopy_dos.log",
        "phonopy_pdos.log",
        "exit_code.json",
        "launcher.log",
        "run_manifest.json",
    ),
}
ALLOWED_SUBMISSION_STEPS = {"relax", "scf", "dos", "elastic", "band", "charge", "phonon", "converge"}
UI_FILE_PREVIEW_MAX_BYTES = 2 * 1024 * 1024


def _job_result_preview_patterns(step: str) -> tuple[str, ...]:
    return tuple(
        pattern
        for pattern in JOB_ARCHIVE_PATTERNS.get(step, ())
        if not pattern.lower().endswith(".pdf")
    )


def _matches_result_preview_pattern(relative_path: str, patterns: tuple[str, ...]) -> bool:
    normalized = relative_path.strip().replace("\\", "/")
    if not normalized:
        return False
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def _safe_ui_relative_path(relative_path: str) -> str | None:
    normalized = relative_path.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return None
    raw_parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return None
    return normalized


def _resolved_path_within_root(path: Path, root: Path) -> Path | None:
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return resolved_path


def _job_result_preview_path_allowed(relative_path: str) -> bool:
    normalized = _safe_ui_relative_path(relative_path)
    if not normalized:
        return False

    parts = PurePosixPath(normalized).parts
    if len(parts) >= 3 and parts[0] == "runs":
        step = parts[1]
        patterns = _job_result_preview_patterns(step)
        tail = "/".join(parts[2:])
        return bool(patterns) and _matches_result_preview_pattern(tail, patterns)

    if len(parts) >= 4 and parts[0] == JOB_RESULT_ARCHIVE_DIRNAME:
        step = parts[1]
        patterns = _job_result_preview_patterns(step)
        tail = "/".join(parts[3:])
        return bool(patterns) and _matches_result_preview_pattern(tail, patterns)

    return False


class SubmitJobRequest(BaseModel):
    system: str
    step: str
    target: str = "local"
    mpi_np: int | None = Field(default=None, ge=1, le=128)


class JobCleanupRequest(BaseModel):
    system: str | None = None
    step: str | None = None
    keep_latest: int = Field(default=1, ge=0, le=20)
    selected_job_id: str | None = None


class ProjectCreateRequest(BaseModel):
    name: str
    creation_mode: str | None = None
    source_system: str | None = None
    material_class: str = "bulk"
    structure_content: str | None = None
    structure_format: str = "poscar"
    poscar_content: str | None = None


class SaveFileRequest(BaseModel):
    system: str
    path: str
    content: str
    expert_override: bool = False


class GenerateFileRequest(BaseModel):
    system: str
    path: str


class MaterialSettingsRequest(BaseModel):
    system: str
    formula: str
    material_class: str = "bulk"
    electronic_type: str = "auto"
    xc_geometry: str = "PBE"
    xc_electronic: str = "PBE"
    spin_polarized: bool = False
    encut: float = Field(default=520, gt=0)
    potcar_family: str = "PBE_64"
    potcar_profile: str = "conservative"
    potcar_mapping_text: str = ""
    kmesh_text: str = ""
    dos_kmesh_text: str = ""
    phonon_kmesh_text: str = ""
    phonon_dos_kmesh_text: str = ""
    phonon_supercell_text: str = ""
    magmom_text: str = ""
    band_points: int = Field(default=101, ge=20, le=500)
    band_kpoints_distance: float = Field(default=0.05, gt=0.0, le=1.0)
    band_path_mode: str = "auto"
    band_path_text: str = ""
    wallclock_seconds: int = Field(default=43200, ge=3600, le=604800)
    advanced_overrides_json: str = ""
    precision_tier: str = ""


class PhononNacSettingsRequest(BaseModel):
    system: str
    enabled: bool = False
    q_direction_text: str = ""


class SystemOnlyRequest(BaseModel):
    system: str


class RemoteProfileRequest(BaseModel):
    name: str
    host: str
    user: str = ""
    workspace_root: str
    vasp_mpi_np: int = Field(default=1, ge=1, le=128)
    pre_command: str = ""
    scheduler_kind: str = "ssh"
    queue_name: str = ""
    account: str = ""
    walltime: str = "24:00:00"
    submit_options: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            raise ValueError("Profile name is required.")
        if candidate == "local":
            raise ValueError("'local' is reserved for the built-in profile.")
        if candidate.startswith("-"):
            raise ValueError("Profile name must not start with '-'.")
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
            raise ValueError("Profile name must not contain whitespace or control characters.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", candidate):
            raise ValueError("Profile name may contain only letters, digits, '_' and '-'.")
        return candidate

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            raise ValueError("Host is required.")
        if candidate.startswith("-"):
            raise ValueError("Host must not start with '-'.")
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
            raise ValueError("Host must not contain whitespace or control characters.")
        return candidate

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        candidate = str(value or "").strip()
        if candidate.startswith("-"):
            raise ValueError("User must not start with '-'.")
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
            raise ValueError("User must not contain whitespace or control characters.")
        return candidate

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            raise ValueError("Remote workspace root is required.")
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
            raise ValueError("Remote workspace root must not contain whitespace or control characters.")
        if not candidate.startswith("/"):
            raise ValueError("Remote workspace root must be an absolute POSIX path.")
        return candidate


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def validate_submission_step(step: str) -> str:
    normalized = str(step or "").strip().lower()
    if normalized not in ALLOWED_SUBMISSION_STEPS:
        raise HTTPException(status_code=400, detail=f"Unsupported step: {step}")
    return normalized


RUN_MANIFEST_VERSION = 1
LINEAGE_FILENAME = ".system_lineage.json"
LINEAGE_VERSION = 1
CREATION_MODE_NEW_FROM_STRUCTURE = "new_from_structure"
CREATION_MODE_FORK_SETTINGS = "fork_settings"
CREATION_MODE_ADOPT_RELAX_CHILD = "adopt_relax_child"
CREATION_MODE_ADOPT_PRIMITIVE_CHILD = "adopt_primitive_child"
CREATION_MODE_VALUES = {
    CREATION_MODE_NEW_FROM_STRUCTURE,
    CREATION_MODE_FORK_SETTINGS,
    CREATION_MODE_ADOPT_RELAX_CHILD,
    CREATION_MODE_ADOPT_PRIMITIVE_CHILD,
}
SYSTEM_PARAMETER_FIELDS = (
    "formula",
    "material_class",
    "electronic_type",
    "xc_geometry",
    "xc_electronic",
    "spin_polarized",
    "encut",
    "potcar_family",
    "potcar_profile",
    "potcar_mapping",
    "potcar_symbols",
    "kmesh",
    "dos_kmesh",
    "phonon_kmesh",
    "phonon_dos_kmesh",
    "phonon_supercell",
    "magmom",
    "band_points",
    "band_kpoints_distance",
    "band_path_mode",
    "band_path_text",
    "wallclock_seconds",
    "incar_overrides",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lineage_path(system_dir: Path) -> Path:
    return system_dir / LINEAGE_FILENAME


_FINGERPRINT_CACHE: dict[Path, tuple[int, int, str]] = {}


def _structure_fingerprint(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    cached = _FINGERPRINT_CACHE.get(path)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    digest = _sha256_bytes(path.read_bytes())
    _FINGERPRINT_CACHE[path] = (stat.st_mtime_ns, stat.st_size, digest)
    return digest


def _composition_formula_from_structure(structure: dict[str, Any] | None) -> str | None:
    if not structure:
        return None
    species = structure.get("species") or []
    counts = structure.get("counts") or []
    if not species or len(species) != len(counts):
        return None
    parts: list[str] = []
    for element, count in zip(species, counts):
        try:
            normalized_count = int(count)
        except (TypeError, ValueError):
            return None
        parts.append(str(element))
        if normalized_count != 1:
            parts.append(str(normalized_count))
    return "".join(parts)


def _composition_formula_from_path(path: Path) -> str | None:
    return _composition_formula_from_structure(parse_poscar_file(path))


def _normalize_creation_mode(raw_mode: str | None, source_system: str | None) -> str:
    normalized = str(raw_mode or "").strip().lower()
    if normalized in CREATION_MODE_VALUES:
        return normalized
    source = str(source_system or "").strip()
    if source and source != "__blank__":
        return CREATION_MODE_FORK_SETTINGS
    return CREATION_MODE_NEW_FROM_STRUCTURE


def _lineage_default_payload(system_dir: Path) -> dict[str, Any]:
    metadata = read_json(system_dir / "metadata.json", {})
    return {
        "version": LINEAGE_VERSION,
        "system_id": system_dir.name,
        "display_name": str(metadata.get("formula") or system_dir.name),
        "parent_system": None,
        "source_kind": "workspace_root",
        "structure_variant": "root",
        "structure_source": "POSCAR",
        "adopted_from_step": None,
        "quarantined": False,
        "created_at": None,
    }


def _normalize_lineage_payload(system_dir: Path, payload: dict[str, Any] | None) -> dict[str, Any]:
    base = _lineage_default_payload(system_dir)
    source = payload if isinstance(payload, dict) else {}
    created_at = source.get("created_at")
    return {
        "version": int(source.get("version") or base["version"]),
        "system_id": str(source.get("system_id") or base["system_id"]),
        "display_name": str(source.get("display_name") or base["display_name"]),
        "parent_system": str(source.get("parent_system")).strip() if source.get("parent_system") else None,
        "source_kind": str(source.get("source_kind") or base["source_kind"]),
        "structure_variant": str(source.get("structure_variant") or base["structure_variant"]),
        "structure_source": str(source.get("structure_source") or base["structure_source"]),
        "adopted_from_step": str(source.get("adopted_from_step")).strip() if source.get("adopted_from_step") else None,
        "quarantined": bool(source.get("quarantined", base["quarantined"])),
        "created_at": str(created_at).strip() if created_at else None,
    }


def lineage_file_exists(system_dir: Path) -> bool:
    return lineage_path(system_dir).exists()


def load_system_lineage(system_dir: Path) -> dict[str, Any]:
    return _normalize_lineage_payload(system_dir, read_json(lineage_path(system_dir), {}))


def write_system_lineage(system_dir: Path, payload: dict[str, Any]) -> None:
    write_json(lineage_path(system_dir), _normalize_lineage_payload(system_dir, payload))


def _system_parameter_subset(metadata: dict[str, Any]) -> dict[str, Any]:
    subset: dict[str, Any] = {}
    for key in SYSTEM_PARAMETER_FIELDS:
        if key in metadata:
            subset[key] = metadata[key]
    return subset


def _suspicious_untracked_child_reason(system_name: str) -> str | None:
    lowered = str(system_name or "").strip().lower()
    if not lowered:
        return None
    if re.search(r"-\d+$", lowered):
        return "System name looks like a numbered fork, but no lineage record is present."
    if "prim" in lowered and "primitive" not in lowered:
        return "System name looks like a primitive-child typo, but no lineage record is present."
    if "primitive" in lowered:
        return "System name looks like a primitive-derived child, but no lineage record is present."
    return None


def _matched_parent_structure_source(system_dir: Path) -> dict[str, str] | None:
    target_fingerprint = _structure_fingerprint(system_dir / "POSCAR")
    if not target_fingerprint:
        return None
    for candidate in project_dirs():
        if candidate == system_dir:
            continue
        relax_path = relax_contcar_path(candidate)
        if _nonempty(relax_path) and _structure_fingerprint(relax_path) == target_fingerprint:
            return {
                "parent_system": candidate.name,
                "structure_variant": "relax_child",
                "structure_source": _relative_system_path(candidate, relax_path),
                "adopted_from_step": "relax",
            }
        primitive_path = relax_primitive_path(candidate)
        if _nonempty(primitive_path) and _structure_fingerprint(primitive_path) == target_fingerprint:
            return {
                "parent_system": candidate.name,
                "structure_variant": "primitive",
                "structure_source": _relative_system_path(candidate, primitive_path),
                "adopted_from_step": "relax",
            }
    return None


def _potcar_base_species(title: str) -> str:
    match = re.match(r"([A-Z][a-z]?)", str(title or "").strip())
    return match.group(1) if match else str(title or "").strip()


def _potcar_base_species_titles(titles: list[str]) -> list[str]:
    return [_potcar_base_species(title) for title in titles]


def _make_integrity_finding(kind: str, severity: str, message: str, *, path: str | None = None, related_system: str | None = None) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "kind": kind,
        "severity": severity,
        "message": message,
    }
    if path:
        finding["path"] = path
    if related_system:
        finding["related_system"] = related_system
    return finding


def _integrity_overall(findings: list[dict[str, Any]]) -> str:
    if any(item.get("severity") == "fail" for item in findings):
        return "fail"
    if any(item.get("severity") == "review" for item in findings):
        return "review"
    return "pass"


def _iso_from_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _relative_system_path(system_dir: Path, path: Path) -> str:
    return relative_system_path(system_dir, path)


def _resolved_structure_input(system_dir: Path, step: str, record: dict[str, Any]) -> Path | None:
    path = resolved_structure_path(system_dir, step, resume=bool(record.get("resume")))
    return path if path.exists() and path.is_file() else None


def _default_step_kpoints_path(system_dir: Path, step: str) -> Path | None:
    normalized = str(step or "").strip().lower()
    if normalized in {"relax", "scf", "converge", "elastic", "charge"}:
        return resolved_mesh_kpoints_path(system_dir, normalized)
    if normalized == "dos":
        return system_dir / "KPOINTS.dos"
    if normalized == "band":
        if _band_uses_kpoints_opt(system_dir):
            return resolved_mesh_kpoints_path(system_dir, normalized)
        return system_dir / "KPOINTS.band"
    if normalized == "phonon":
        return system_dir / "KPOINTS.phonon"
    return None


def _resolved_step_kpoints_input(system_dir: Path, step: str) -> Path | None:
    path = _default_step_kpoints_path(system_dir, step)
    if path is None:
        return None
    return path if path.exists() and path.is_file() else None


def _resolved_step_mesh_kpoints_input(system_dir: Path, step: str) -> Path | None:
    normalized = str(step or "").strip().lower()
    if normalized not in {"relax", "scf", "converge", "elastic", "charge", "band"}:
        return None
    path = resolved_mesh_kpoints_path(system_dir, normalized)
    return path if path.exists() and path.is_file() else None


def _resolved_step_input_paths(system_dir: Path, step: str, record: dict[str, Any]) -> dict[str, Path]:
    resolved: dict[str, Path] = {}

    structure_path = _resolved_structure_input(system_dir, step, record)
    if structure_path is not None:
        resolved["structure"] = structure_path

    kpoints_path = _resolved_step_kpoints_input(system_dir, step)
    if kpoints_path is not None:
        resolved["kpoints"] = kpoints_path

    if step == "band" and _band_uses_kpoints_opt(system_dir):
        kpoints_opt_path = system_dir / "KPOINTS.band"
        if kpoints_opt_path.exists() and kpoints_opt_path.is_file():
            resolved["kpoints_opt"] = kpoints_opt_path

    common_candidates = {
        "poscar": ["POSCAR"],
        "metadata": ["metadata.json"],
    }
    step_candidates = {
        "relax": {
            "incar": ["INCAR.relax"],
        },
        "scf": {
            "incar": ["INCAR.scf"],
        },
        "dos": {
            "incar": ["INCAR.dos"],
        },
        "converge": {
            "incar": ["INCAR.converge"],
        },
        "elastic": {
            "incar": ["INCAR.elastic"],
        },
        "band": {
            "incar": ["INCAR.band"],
            "kpath": ["KPATH.in"],
        },
        "charge": {
            "incar": ["INCAR.charge"],
        },
        "phonon": {
            "incar": ["INCAR.phonon"],
            "band_conf": ["band.conf"],
            "kpath": ["KPATH.in"],
        },
    }

    for role, candidates in {**common_candidates, **step_candidates.get(step, {})}.items():
        for candidate in candidates:
            path = system_dir / candidate
            if path.exists() and path.is_file():
                resolved[role] = path
                break

    return resolved


def _band_uses_kpoints_opt(system_dir: Path) -> bool:
    metadata = read_json(system_dir / "metadata.json", {})
    if str(metadata.get("xc_electronic") or "").strip() == "HSE06":
        return True
    raw = _incar_value(system_dir / "INCAR.band", "LHFCALC")
    if raw is None:
        return False
    normalized = raw.strip().upper()
    return normalized in {".TRUE.", "TRUE", "T", "1", "Y", "YES"}


def _submission_input_record_fields(system_dir: Path, step: str, *, resume: bool = False) -> dict[str, Any]:
    kpoints_path = _resolved_step_kpoints_input(system_dir, step) or _default_step_kpoints_path(system_dir, step) or (system_dir / "KPOINTS.scf")
    record: dict[str, Any] = {
        "structure_input": _relative_system_path(system_dir, resolved_structure_path(system_dir, step, resume=resume)),
        "kpoints_input": _relative_system_path(system_dir, kpoints_path),
    }
    if step == "band":
        if _band_uses_kpoints_opt(system_dir):
            record["band_mode"] = "hybrid"
            record["mesh_kpoints_input"] = record["kpoints_input"]
            record["kpoints_opt_input"] = "KPOINTS.band"
        else:
            record["band_mode"] = "standard"
            record["kpoints_line_input"] = record["kpoints_input"]
        if (system_dir / "KPATH.in").exists():
            record["band_path_input"] = "KPATH.in"
    return record


def _snapshot_text_file(system_dir: Path, path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    content = path.read_text(encoding="utf-8", errors="ignore")
    return {
        "path": _relative_system_path(system_dir, path),
        "content": content,
        "sha256": _sha256_bytes(content.encode("utf-8")),
    }


def _potcar_summary(system_dir: Path) -> dict[str, Any] | None:
    potcar_path = system_dir / "POTCAR"
    if not potcar_path.exists() or not potcar_path.is_file():
        return None
    return {
        "path": "POTCAR",
        "titles": potcar_titles(potcar_path),
        "max_enmax": _max_potcar_enmax(potcar_path),
        "sha256": _sha256_bytes(potcar_path.read_bytes()),
    }


def _sync_run_manifest_runtime_state(system_dir: Path, step: str, state: str | None, finished_at: str | None = None) -> bool:
    manifest_path = system_dir / "runs" / step / "run_manifest.json"
    payload = read_json(manifest_path, {})
    if not isinstance(payload, dict) or not payload:
        return False

    changed = False
    normalized_state = str(state or "").strip()
    if normalized_state and payload.get("state") != normalized_state:
        payload["state"] = normalized_state
        changed = True

    if finished_at:
        if payload.get("finished_at") != finished_at:
            payload["finished_at"] = finished_at
            changed = True
    elif payload.get("finished_at") and normalized_state not in {"finished", "failed", "paused", "stopped", "completed"}:
        payload.pop("finished_at", None)
        changed = True

    if changed:
        write_json(manifest_path, payload)
    return changed


def _ensure_run_manifest_completion(system_dir: Path, step: str) -> bool:
    completion_ts = step_completion_timestamp(system_dir, step)
    if completion_ts is None:
        return False
    return _sync_run_manifest_runtime_state(system_dir, step, "finished", _iso_from_timestamp(completion_ts))


def _write_run_manifest(system_dir: Path, step: str, record: dict[str, Any]) -> Path:
    run_dir = system_dir / "runs" / step
    run_dir.mkdir(parents=True, exist_ok=True)

    resolved_inputs = _resolved_step_input_paths(system_dir, step, record)
    snapshot_paths: list[Path] = []
    seen_snapshot_paths: set[str] = set()
    for path in resolved_inputs.values():
        normalized = str(path.resolve())
        if normalized not in seen_snapshot_paths:
            snapshot_paths.append(path)
            seen_snapshot_paths.add(normalized)

    input_snapshots: dict[str, dict[str, Any]] = {}
    for path in snapshot_paths:
        snapshot = _snapshot_text_file(system_dir, path)
        if snapshot is None:
            continue
        input_snapshots[snapshot["path"]] = snapshot

    payload: dict[str, Any] = {key: value for key, value in record.items()}
    structure_input = resolved_inputs.get("structure") or (system_dir / "POSCAR")
    payload.update(
        {
            "manifest_version": RUN_MANIFEST_VERSION,
            "app_version": settings.app_version,
            "system": system_dir.name,
            "step": step,
            "job_id": record.get("id"),
            "state": str(record.get("state") or ""),
            "finished_at": record.get("finished_at"),
            "lineage_snapshot": load_system_lineage(system_dir),
            "structure_fingerprint": _structure_fingerprint(structure_input),
            "composition_formula": _composition_formula_from_path(structure_input),
            "resolved_inputs": {
                role: _relative_system_path(system_dir, path)
                for role, path in resolved_inputs.items()
            },
            "input_snapshots": input_snapshots,
            "effective_params": list(record.get("effective_params") or []),
        }
    )
    potcar_summary = _potcar_summary(system_dir)
    if potcar_summary is not None:
        payload["potcar_summary"] = potcar_summary

    manifest_path = run_dir / "run_manifest.json"
    write_json(manifest_path, payload)
    return manifest_path


def _attach_run_manifest(system_dir: Path, step: str, record: dict[str, Any]) -> dict[str, Any]:
    try:
        manifest_path = _write_run_manifest(system_dir, step, record)
        record["run_manifest"] = str(manifest_path)
        record.pop("run_manifest_error", None)
    except Exception as exc:
        record["run_manifest_error"] = str(exc)
        record["warnings"] = _merge_warnings(
            list(record.get("warnings") or []),
            [f"Run manifest could not be written: {exc}"],
        )
    return record


def load_jobs() -> list[dict[str, Any]]:
    return read_json(JOBS_FILE, [])


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    with file_lock(JOBS_FILE):
        write_json(JOBS_FILE, jobs)


def load_profiles() -> list[dict[str, Any]]:
    return read_json(PROFILES_FILE, [])


def save_profiles(profiles: list[dict[str, Any]]) -> None:
    with file_lock(PROFILES_FILE):
        write_json(PROFILES_FILE, profiles)


def _load_jobs_unlocked() -> list[dict[str, Any]]:
    payload = read_json(JOBS_FILE, [])
    return payload if isinstance(payload, list) else []


def _save_jobs_unlocked(jobs: list[dict[str, Any]]) -> None:
    write_json(JOBS_FILE, jobs)


def _mutate_jobs(
    mutator,
    *,
    refresh: bool = False,
    save_if_unchanged: bool = True,
):
    with file_lock(JOBS_FILE):
        jobs = _load_jobs_unlocked()
        changed = _refresh_job_states_in_place(jobs) if refresh else False
        result = mutator(jobs)
        if save_if_unchanged or changed:
            _save_jobs_unlocked(jobs)
        return result


def get_submission_profile(name: str) -> dict[str, Any] | None:
    return next((item for item in load_profiles() if item["name"] == name), None)


def project_dirs() -> list[Path]:
    return sorted([path for path in SYSTEMS_DIR.iterdir() if path.is_dir()], key=lambda path: path.name)


def load_summary() -> dict[str, Any]:
    return read_json(RESULTS_DIR / "calculated_summary.json", {})


def read_text_preview(path: Path, limit: int = 4000) -> str:
    if not path.exists():
        return ""
    data = path.read_text(errors="ignore")
    return data[:limit]


def read_limited_text_payload(path: Path, limit: int = UI_FILE_PREVIEW_MAX_BYTES) -> dict[str, Any]:
    size = path.stat().st_size if path.exists() else 0
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    truncated = len(data) > limit
    content = data[:limit].decode("utf-8", errors="ignore")
    if truncated:
        content = (
            content.rstrip()
            + f"\n\n[Preview truncated: showing first {limit} bytes of {size} bytes. Use the runtime file directly for the full content.]\n"
        )
    return {
        "content": content,
        "truncated": truncated,
        "size_bytes": size,
        "preview_limit_bytes": limit,
    }


def read_text_tail(path: Path, limit: int = 8000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode(errors="ignore")
    except OSError:
        return ""


def latest_matching_file(base: Path, pattern: str) -> Path | None:
    candidates = [path for path in base.glob(pattern) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _local_attempt_root(system_dir: Path) -> Path:
    return system_dir / LOCAL_ATTEMPT_WORKDIR_DIRNAME


def _local_attempt_dir_for_job(system_dir: Path, step: str, job_id: str) -> Path:
    return _local_attempt_root(system_dir) / step / job_id


def _job_attempt_dir(job: dict[str, Any]) -> Path | None:
    if job.get("target") != "local" or job.get("backend") not in {None, "", "legacy"}:
        return None
    system = str(job.get("system") or "").strip()
    step = str(job.get("step") or "").strip()
    job_id = str(job.get("id") or "").strip()
    if not system or not step or not job_id:
        return None

    system_dir = SYSTEMS_DIR / system
    raw = str(job.get("attempt_dir") or "").strip()
    if raw:
        attempt_path = Path(raw)
        if not attempt_path.is_absolute():
            attempt_path = system_dir / raw
        return _resolved_path_within_root(attempt_path, system_dir)

    default_path = _local_attempt_dir_for_job(system_dir, step, job_id)
    if default_path.exists():
        return default_path
    return None


def legacy_active_log_path(job: dict[str, Any]) -> Path:
    launcher_log = Path(job["launcher_log"])
    if job.get("target") != "local":
        return launcher_log

    target_log = Path(job["target_log"]) if job.get("target_log") else None
    if job.get("system") and job.get("step") == "phonon":
        run_dir = _job_attempt_dir(job) or (SYSTEMS_DIR / job["system"] / "runs" / "phonon")
        if run_dir.exists():
            nested = latest_matching_file(run_dir, "dis-*/log")
            if nested and nested.stat().st_size >= 0:
                return nested

    if target_log and target_log.exists() and target_log.stat().st_size > 0:
        return target_log
    return launcher_log


def _job_log_fallback_payload(job: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
    run_dir, source_kind = _job_result_source_run_dir(job)
    candidate_paths: list[Path] = []
    if run_dir is not None:
        candidate_paths.extend(
            [
                run_dir / "log",
                run_dir / "launcher.log",
                run_dir / "vasp_output",
                run_dir / "OUTCAR",
                run_dir / "OSZICAR",
                run_dir / "exit_code.json",
                run_dir / "run_manifest.json",
            ]
        )

    content_path = next((path for path in candidate_paths if path.exists() and path.is_file() and path.stat().st_size > 0), None)
    source_label = "archived" if source_kind == "archive" else "attempt" if source_kind == "attempt" else "live" if source_kind == "live" else "saved"
    notice_bits = [f"AiiDA process metadata is unavailable for this {source_label} historical job."]
    if reason:
        notice_bits.append(reason)
    if content_path is not None:
        notice_bits.append(f"Showing the local tail of {content_path.name} instead.")
        content = read_text_tail(content_path, 16000)
        log_path = str(content_path)
    else:
        notice_bits.append("No local launcher log is available.")
        content = "\n".join(notice_bits) + "\n"
        log_path = "historical://unavailable"

    return {
        "job_id": str(job.get("id") or ""),
        "state": str(job.get("state") or "unknown"),
        "display_state": job.get("display_state", job.get("state", "unknown")),
        "status_summary": job.get("status_summary", ""),
        "log_path": log_path,
        "log_notice": " ".join(notice_bits),
        "content": content,
    }


def file_tree(base: Path, depth: int = 2) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    base_parts = len(base.parts)
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if rel.name == ".generation_meta.json":
            continue
        if rel.parts and rel.parts[0] == JOB_RESULT_ARCHIVE_DIRNAME:
            continue
        level = len(path.parts) - base_parts
        if level > depth:
            continue
        items.append(
            {
                "path": str(rel),
                "type": "dir" if path.is_dir() else "file",
                "size": path.stat().st_size if path.is_file() else None,
            }
        )
    return items


def step_completion_marker(run_dir: Path, step: str) -> Path:
    markers = {
        "relax": run_dir / "CONTCAR",
        "scf": run_dir / "OUTCAR",
        "dos": run_dir / "DOSCAR",
        "converge": run_dir / "OUTCAR",
        "elastic": run_dir / "OUTCAR",
        "band": run_dir / "EIGENVAL",
        "charge": run_dir / "CHGCAR",
        "phonon": run_dir / "FORCE_SETS",
    }
    return markers[step]


def _nonempty(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _charge_completed(run_dir: Path) -> bool:
    required = [
        run_dir / "CHGCAR",
        run_dir / "AECCAR0",
        run_dir / "AECCAR2",
        run_dir / "CHGCAR_sum",
        run_dir / "ACF.dat",
    ]
    return all(_nonempty(path) for path in required)


def _tail_contains(path: Path, patterns: tuple[str, ...], limit: int = 65536) -> bool:
    if not _nonempty(path):
        return False
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        text = handle.read().decode(errors="ignore").upper()
    return any(pattern.upper() in text for pattern in patterns)


def _outcar_completed(path: Path) -> bool:
    return _tail_contains(path, ("General timing and accounting informations for this job",))


def _scf_completed(run_dir: Path) -> bool:
    return _outcar_completed(run_dir / "OUTCAR")


def _phonon_completed(run_dir: Path) -> bool:
    if not _nonempty(run_dir / "FORCE_SETS") or not _nonempty(run_dir / "band.yaml"):
        return False
    displacement_dirs = sorted(path for path in run_dir.glob("dis-*") if path.is_dir())
    if not displacement_dirs:
        return False
    return all(_nonempty(path / "vasprun.xml") for path in displacement_dirs)


def _band_completed(run_dir: Path) -> bool:
    return _parse_band_gap(run_dir / "BAND_GAP") is not None or _nonempty(run_dir / "PROCAR_OPT") or _eigenval_has_band_entries(run_dir / "EIGENVAL")


def step_completed(system_dir: Path, step: str) -> bool:
    run_dir = system_dir / "runs" / step
    if not run_dir.exists():
        return False
    if step == "scf":
        return _scf_completed(run_dir)
    if step == "elastic":
        return _parse_elastic_summary(run_dir / "OUTCAR") is not None
    if step == "band":
        return _band_completed(run_dir)
    if step == "dos":
        return _nonempty(run_dir / "DOSCAR")
    if step == "charge":
        return _charge_completed(run_dir)
    if step == "phonon":
        return _phonon_completed(run_dir)
    marker = step_completion_marker(run_dir, step)
    return _nonempty(marker)


def _step_completion_artifacts(run_dir: Path, step: str) -> list[Path]:
    if step == "scf":
        return [run_dir / "OUTCAR"] if _scf_completed(run_dir) else []
    if step == "elastic":
        return [run_dir / "OUTCAR"] if _parse_elastic_summary(run_dir / "OUTCAR") is not None else []
    if step == "band":
        artifacts: list[Path] = []
        if _parse_band_gap(run_dir / "BAND_GAP") is not None:
            artifacts.append(run_dir / "BAND_GAP")
        if _nonempty(run_dir / "PROCAR_OPT"):
            artifacts.append(run_dir / "PROCAR_OPT")
        if _eigenval_has_band_entries(run_dir / "EIGENVAL"):
            artifacts.append(run_dir / "EIGENVAL")
        return artifacts
    if step == "dos":
        return [run_dir / "DOSCAR"] if _nonempty(run_dir / "DOSCAR") else []
    if step == "charge":
        required = [run_dir / name for name in ("CHGCAR", "AECCAR0", "AECCAR2", "CHGCAR_sum", "ACF.dat")]
        return required if all(_nonempty(path) for path in required) else []
    if step == "phonon":
        if not _phonon_completed(run_dir):
            return []
        displacement_outputs = sorted(path / "vasprun.xml" for path in run_dir.glob("dis-*") if _nonempty(path / "vasprun.xml"))
        return [run_dir / "FORCE_SETS", run_dir / "band.yaml", *displacement_outputs]
    marker = step_completion_marker(run_dir, step)
    return [marker] if _nonempty(marker) else []


def step_completion_timestamp(system_dir: Path, step: str) -> float | None:
    run_dir = system_dir / "runs" / step
    if not run_dir.exists():
        return None
    artifacts = _step_completion_artifacts(run_dir, step)
    if not artifacts:
        return None
    return max(path.stat().st_mtime for path in artifacts)


def _job_event_timestamp(job: dict[str, Any]) -> float | None:
    def _parse_timestamp(raw: Any) -> float | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw)).timestamp()
        except ValueError:
            return None

    for key in ("finished_at", "created_at", "control_requested_at"):
        timestamp = _parse_timestamp(job.get(key))
        if timestamp is not None:
            return timestamp
    return None


def _latest_job_sort_key(job: dict[str, Any]) -> tuple[float, float, float, str]:
    def _parse_timestamp(raw: Any) -> float | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw)).timestamp()
        except ValueError:
            return None

    created_at = _parse_timestamp(job.get("created_at"))
    finished_at = _parse_timestamp(job.get("finished_at"))
    control_requested_at = _parse_timestamp(job.get("control_requested_at"))
    primary = created_at if created_at is not None else finished_at if finished_at is not None else control_requested_at
    return (
        primary if primary is not None else float("-inf"),
        finished_at if finished_at is not None else float("-inf"),
        control_requested_at if control_requested_at is not None else float("-inf"),
        str(job.get("id") or ""),
    )


def latest_job_for_step(system_name: str, step: str) -> dict[str, Any] | None:
    candidates = [job for job in load_jobs() if job.get("system") == system_name and job.get("step") == step]
    return max(candidates, key=_latest_job_sort_key) if candidates else None


def latest_job_state_for_step(system_name: str, step: str) -> str | None:
    job = latest_job_for_step(system_name, step)
    if job:
        state = str(job.get("state") or "").strip()
        if state:
            return state
    return None


def _job_archive_root(system_dir: Path) -> Path:
    return system_dir / JOB_RESULT_ARCHIVE_DIRNAME


def job_result_archive_dir(job: dict[str, Any]) -> Path | None:
    system = str(job.get("system") or "").strip()
    step = str(job.get("step") or "").strip()
    job_id = str(job.get("id") or "").strip()
    if not system or not step or not job_id:
        return None
    return _job_archive_root(SYSTEMS_DIR / system) / step / job_id


def _job_archive_relative_path(job: dict[str, Any]) -> str | None:
    archive_dir = job_result_archive_dir(job)
    if archive_dir is None:
        return None
    try:
        return archive_dir.relative_to(SYSTEMS_DIR / str(job.get("system") or "").strip()).as_posix()
    except ValueError:
        return archive_dir.as_posix()


def _job_archive_has_files(archive_dir: Path | None) -> bool:
    if archive_dir is None or not archive_dir.exists() or not archive_dir.is_dir():
        return False
    return any(path.is_file() for path in archive_dir.rglob("*"))


def _job_is_latest_for_step(job: dict[str, Any]) -> bool:
    system = str(job.get("system") or "").strip()
    step = str(job.get("step") or "").strip()
    if not system or not step:
        return False
    latest = latest_job_for_step(system, step)
    return bool(latest and latest.get("id") == job.get("id"))


def _job_archive_source_dir(job: dict[str, Any]) -> tuple[Path | None, str | None]:
    if job.get("backend") == "aiida":
        try:
            remote_path = aiida_remote_path(settings.aiida_profile_name, job)
        except Exception:
            remote_path = None
        if remote_path is not None and remote_path.exists() and remote_path.is_dir():
            return remote_path, "aiida_remote"

    attempt_dir = _job_attempt_dir(job)
    if attempt_dir is not None and attempt_dir.exists() and attempt_dir.is_dir():
        return attempt_dir, "attempt"

    if _job_is_latest_for_step(job):
        system = str(job.get("system") or "").strip()
        step = str(job.get("step") or "").strip()
        if system and step:
            run_dir = SYSTEMS_DIR / system / "runs" / step
            if run_dir.exists() and run_dir.is_dir():
                return run_dir, "live"
    return None, None


def _copy_archive_file(source_path: Path | None, destination: Path) -> bool:
    if source_path is None or not source_path.exists() or not source_path.is_file():
        return False
    source_stat = source_path.stat()
    if not destination.exists():
        shutil.copy2(source_path, destination)
        return True
    destination_stat = destination.stat()
    if destination_stat.st_size != source_stat.st_size or destination_stat.st_mtime_ns < source_stat.st_mtime_ns:
        shutil.copy2(source_path, destination)
        return True
    return True


def _archive_job_results(job: dict[str, Any]) -> Path | None:
    archive_dir = job_result_archive_dir(job)
    if archive_dir is None:
        return None
    patterns = JOB_ARCHIVE_PATTERNS.get(str(job.get("step") or "").strip(), ())
    if not patterns:
        return None

    source_dir, _ = _job_archive_source_dir(job)
    if source_dir is None:
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    copied = False
    for pattern in patterns:
        for source_path in sorted(source_dir.glob(pattern)):
            copied = _copy_archive_file(source_path, archive_dir / source_path.name) or copied

    launcher_log = managed_launcher_log_path(job)
    copied = _copy_archive_file(launcher_log, archive_dir / "launcher.log") or copied
    return archive_dir if copied else None


def _job_result_source_run_dir(job: dict[str, Any]) -> tuple[Path | None, str | None]:
    archive_dir = job_result_archive_dir(job)
    if _job_archive_has_files(archive_dir):
        return archive_dir, "archive"

    attempt_dir = _job_attempt_dir(job)
    if attempt_dir is not None and attempt_dir.exists() and attempt_dir.is_dir():
        return attempt_dir, "attempt"

    if _job_is_latest_for_step(job):
        system = str(job.get("system") or "").strip()
        step = str(job.get("step") or "").strip()
        if system and step:
            run_dir = SYSTEMS_DIR / system / "runs" / step
            if run_dir.exists() and run_dir.is_dir():
                return run_dir, "live"
    return None, None


def _job_exit_code_payload(job: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[Path] = []
    run_dir, _ = _job_result_source_run_dir(job)
    if run_dir is not None:
        candidates.append(run_dir / "exit_code.json")

    archive_dir = job_result_archive_dir(job)
    if archive_dir is not None:
        archive_exit = archive_dir / "exit_code.json"
        if archive_exit not in candidates:
            candidates.append(archive_exit)

    system = str(job.get("system") or "").strip()
    step = str(job.get("step") or "").strip()
    if system and step:
        live_exit = SYSTEMS_DIR / system / "runs" / step / "exit_code.json"
        if live_exit not in candidates:
            candidates.append(live_exit)

    for path in candidates:
        payload = read_json(path, None)
        if isinstance(payload, dict):
            return payload
    return None


def _sync_directory_contents(source_dir: Path, destination_dir: Path) -> bool:
    if not source_dir.exists() or not source_dir.is_dir():
        return False

    destination_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    source_dirs: set[Path] = {Path(".")}
    source_files: set[Path] = set()

    for source_path in sorted(source_dir.rglob("*")):
        relative = source_path.relative_to(source_dir)
        if source_path.is_dir():
            source_dirs.add(relative)
            (destination_dir / relative).mkdir(parents=True, exist_ok=True)
            continue

        if not source_path.is_file():
            continue
        source_files.add(relative)
        destination = destination_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_stat = source_path.stat()
        if not destination.exists():
            shutil.copy2(source_path, destination)
            changed = True
            continue
        destination_stat = destination.stat()
        if destination_stat.st_size != source_stat.st_size or destination_stat.st_mtime_ns < source_stat.st_mtime_ns:
            shutil.copy2(source_path, destination)
            changed = True

    for destination_path in sorted(destination_dir.rglob("*"), key=lambda path: len(path.relative_to(destination_dir).parts), reverse=True):
        relative = destination_path.relative_to(destination_dir)
        if destination_path.is_file():
            if relative not in source_files:
                destination_path.unlink()
                changed = True
        elif destination_path.is_dir():
            if relative not in source_dirs and not any(destination_path.iterdir()):
                destination_path.rmdir()
                changed = True

    return changed


def _sync_attempt_manifest(job: dict[str, Any]) -> bool:
    attempt_dir = _job_attempt_dir(job)
    system = str(job.get("system") or "").strip()
    step = str(job.get("step") or "").strip()
    if attempt_dir is None or not system or not step:
        return False
    manifest_path = SYSTEMS_DIR / system / "runs" / step / "run_manifest.json"
    if not manifest_path.exists() or not manifest_path.is_file():
        return False
    attempt_dir.mkdir(parents=True, exist_ok=True)
    destination = attempt_dir / "run_manifest.json"
    source_stat = manifest_path.stat()
    if not destination.exists():
        shutil.copy2(manifest_path, destination)
        return True
    destination_stat = destination.stat()
    if destination_stat.st_size != source_stat.st_size or destination_stat.st_mtime_ns < source_stat.st_mtime_ns:
        shutil.copy2(manifest_path, destination)
        return True
    return False


def _sync_local_attempt_to_live_run_dir(job: dict[str, Any]) -> bool:
    attempt_dir = _job_attempt_dir(job)
    if attempt_dir is None or not attempt_dir.exists() or not attempt_dir.is_dir() or not _job_is_latest_for_step(job):
        return False
    system = str(job.get("system") or "").strip()
    step = str(job.get("step") or "").strip()
    if not system or not step:
        return False
    live_run_dir = SYSTEMS_DIR / system / "runs" / step
    return _sync_directory_contents(attempt_dir, live_run_dir)


def _step_results_ready_in_dir(run_dir: Path, step: str) -> bool:
    try:
        if step == "elastic":
            return _parse_elastic_summary(run_dir / "OUTCAR") is not None
        if step == "band":
            return _parse_band_summary(run_dir) is not None
        if step == "dos":
            return _nonempty(run_dir / "DOSCAR")
        if step == "charge":
            return _charge_completed(run_dir)
        if step == "phonon":
            return _nonempty(run_dir / "FORCE_SETS") and _nonempty(run_dir / "band.yaml")
        if step == "relax":
            return _nonempty(run_dir / "CONTCAR")
        if step == "scf":
            return _scf_completed(run_dir)
    except Exception:
        return False
    return False


def _maybe_archive_job_results(job: dict[str, Any]) -> Path | None:
    archive_dir = job_result_archive_dir(job)
    if _job_archive_has_files(archive_dir):
        return archive_dir
    if not is_terminal_job_state(job.get("state")):
        return None
    source_dir, _ = _job_archive_source_dir(job)
    step = str(job.get("step") or "").strip()
    if source_dir is None or not step:
        return None
    if not _step_results_ready_in_dir(source_dir, step):
        return None
    return _archive_job_results(job)


def step_status(system_dir: Path, step: str) -> str:
    latest_job = latest_job_for_step(system_dir.name, step)
    latest_state = str((latest_job or {}).get("state") or "").strip()
    if latest_state in ACTIVE_JOB_STATES:
        _sync_run_manifest_runtime_state(system_dir, step, latest_state, None)
        return latest_state

    run_dir = system_dir / "runs" / step
    completion_ts = step_completion_timestamp(system_dir, step)
    if completion_ts is not None:
        _ensure_run_manifest_completion(system_dir, step)
        if latest_state in {"failed", "paused", "stopped"}:
            job_ts = _job_event_timestamp(latest_job or {})
            if job_ts is not None and job_ts >= completion_ts:
                _sync_run_manifest_runtime_state(system_dir, step, latest_state, (latest_job or {}).get("finished_at"))
                return latest_state
        return "completed"

    if latest_state in {"failed", "paused", "stopped"}:
        _sync_run_manifest_runtime_state(system_dir, step, latest_state, (latest_job or {}).get("finished_at"))
        return latest_state
    if run_dir.exists():
        if latest_state:
            return latest_state if latest_state in ACTIVE_JOB_STATES else "running_or_partial"
        return "running_or_partial"
    if latest_state:
        return latest_state if latest_state in {"failed", "paused", "stopped"} else "running_or_partial"
    return "not_started"


def sanitize_project_name(name: str) -> str:
    cleaned = "".join(ch for ch in name.strip() if ch.isalnum() or ch in {"-", "_"})
    if not cleaned:
        raise HTTPException(status_code=400, detail="Invalid project name")
    return cleaned


def resolve_system_dir(system_name: str, *, must_exist: bool = True) -> Path:
    name = str(system_name or "").strip()
    if not name or name in {".", ".."} or name != Path(name).name:
        raise HTTPException(status_code=400, detail="Invalid system name")
    resolved = _resolved_path_within_root(SYSTEMS_DIR / name, SYSTEMS_DIR)
    if resolved is None or resolved == SYSTEMS_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid system name")
    system_dir = SYSTEMS_DIR / name
    if must_exist and not system_dir.is_dir():
        raise HTTPException(status_code=404, detail="System not found")
    return system_dir


def active_jobs_for_system(system_name: str) -> list[dict[str, Any]]:
    jobs = refresh_job_states()
    return [
        job
        for job in jobs
        if job.get("system") == system_name and str(job.get("state") or "").lower() in ACTIVE_JOB_STATES
    ]


def remove_project_summary(system_name: str) -> None:
    summary_path = RESULTS_DIR / "calculated_summary.json"
    summary = read_json(summary_path, {})
    if isinstance(summary, dict) and system_name in summary:
        summary.pop(system_name, None)
        write_json(summary_path, summary)


def delete_project(system_name: str) -> dict[str, Any]:
    system_dir = resolve_system_dir(system_name)
    system_name = system_dir.name

    active = active_jobs_for_system(system_name)
    if active:
        labels = ", ".join(f"{job.get('id')} ({job.get('state')})" for job in active)
        raise HTTPException(status_code=400, detail=f"Cannot delete {system_name} while jobs are active: {labels}")

    def _remove_project_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        active = [
            job
            for job in jobs
            if job.get("system") == system_name and str(job.get("state") or "").lower() in ACTIVE_JOB_STATES
        ]
        if active:
            labels = ", ".join(f"{job.get('id')} ({job.get('state')})" for job in active)
            raise HTTPException(status_code=400, detail=f"Cannot delete {system_name} while jobs are active: {labels}")
        removed = [job for job in jobs if job.get("system") == system_name]
        jobs[:] = [job for job in jobs if job.get("system") != system_name]
        return removed

    removed_jobs = _mutate_jobs(_remove_project_jobs, refresh=True)

    for job in removed_jobs:
        launcher_log = managed_launcher_log_path(job)
        if launcher_log is not None:
            launcher_log.unlink(missing_ok=True)

    shutil.rmtree(system_dir)
    remove_project_summary(system_name)
    return {
        "deleted_system": system_name,
        "removed_job_count": len(removed_jobs),
        "remaining_systems": len(project_dirs()),
        "deleted_at": now_iso(),
    }


def _project_creation_source_dir(mode: str, source_system: str | None) -> Path | None:
    source_name = str(source_system or "").strip()
    if source_name == "__blank__":
        source_name = ""
    if mode == CREATION_MODE_NEW_FROM_STRUCTURE:
        if source_name:
            raise HTTPException(status_code=400, detail="new_from_structure does not accept source_system.")
        return None
    if not source_name:
        raise HTTPException(status_code=400, detail=f"{mode} requires source_system.")
    return resolve_system_dir(source_name)


def _project_creation_poscar_text(
    mode: str,
    source_dir: Path | None,
    imported_structure: str | None,
) -> tuple[str, str]:
    if mode == CREATION_MODE_NEW_FROM_STRUCTURE:
        if not imported_structure:
            raise HTTPException(status_code=400, detail="new_from_structure requires an imported POSCAR or CIF structure.")
        return imported_structure, "request.structure_content"

    if mode == CREATION_MODE_FORK_SETTINGS:
        if imported_structure:
            return imported_structure, "request.structure_content"
        if source_dir is None:
            raise HTTPException(status_code=400, detail="fork_settings requires source_system when no structure is provided.")
        source_path = source_dir / "POSCAR"
        if not source_path.exists():
            raise HTTPException(status_code=400, detail=f"{source_dir.name} has no POSCAR to fork from.")
        return source_path.read_text(encoding="utf-8"), "POSCAR"

    if imported_structure:
        raise HTTPException(status_code=400, detail=f"{mode} does not accept structure_content.")
    if source_dir is None:
        raise HTTPException(status_code=400, detail=f"{mode} requires source_system.")

    if mode == CREATION_MODE_ADOPT_RELAX_CHILD:
        source_path = relax_contcar_path(source_dir)
        if not _nonempty(source_path):
            raise HTTPException(status_code=400, detail=f"{source_dir.name} has no completed relax structure at runs/relax/CONTCAR.")
        return source_path.read_text(encoding="utf-8"), _relative_system_path(source_dir, source_path)

    if mode == CREATION_MODE_ADOPT_PRIMITIVE_CHILD:
        source_path = relax_primitive_path(source_dir)
        if not _nonempty(source_path):
            raise HTTPException(status_code=400, detail=f"{source_dir.name} has no generated primitive structure at runs/relax/PRIMCELL.vasp.")
        return source_path.read_text(encoding="utf-8"), _relative_system_path(source_dir, source_path)

    raise HTTPException(status_code=400, detail=f"Unsupported creation_mode: {mode}")


def _project_seed_metadata(mode: str, new_name: str, source_dir: Path | None, requested_material_class: str) -> dict[str, Any]:
    if source_dir is not None:
        source_metadata = read_json(source_dir / "metadata.json", {})
        metadata = _system_parameter_subset(source_metadata)
    else:
        metadata = {}

    default_material_class = str(
        metadata.get("material_class")
        or requested_material_class
        or "bulk"
    )
    material_defaults = defaults_for_material_class(default_material_class)

    metadata["formula"] = new_name
    metadata["source_template"] = source_dir.name if source_dir is not None else None
    metadata["material_class"] = str(metadata.get("material_class") or default_material_class)
    metadata["electronic_type"] = str(metadata.get("electronic_type") or material_defaults["electronic_type"])
    metadata["xc_geometry"] = str(metadata.get("xc_geometry") or material_defaults.get("xc_geometry") or "PBE")
    metadata["xc_electronic"] = str(metadata.get("xc_electronic") or material_defaults.get("xc_electronic") or "PBE")
    metadata["spin_polarized"] = bool(metadata.get("spin_polarized", material_defaults["spin_polarized"]))
    metadata["encut"] = float(metadata.get("encut") or material_defaults.get("encut", 520))
    metadata["kmesh"] = list(metadata.get("kmesh") or material_defaults["kmesh"])
    metadata["phonon_kmesh"] = list(metadata.get("phonon_kmesh") or material_defaults["phonon_kmesh"])
    metadata["phonon_dos_kmesh"] = list(
        metadata.get("phonon_dos_kmesh")
        or material_defaults.get("phonon_dos_kmesh")
        or material_defaults["phonon_kmesh"]
    )
    metadata["phonon_supercell"] = list(metadata.get("phonon_supercell") or material_defaults["phonon_supercell"])
    metadata["band_points"] = int(metadata.get("band_points") or material_defaults["band_points"])
    metadata["band_kpoints_distance"] = float(
        metadata.get("band_kpoints_distance") or material_defaults["band_kpoints_distance"]
    )
    metadata["wallclock_seconds"] = int(metadata.get("wallclock_seconds") or material_defaults["wallclock_seconds"])
    metadata["potcar_profile"] = str(metadata.get("potcar_profile") or material_defaults["potcar_profile"])
    return metadata


def _project_lineage_payload(new_name: str, mode: str, source_dir: Path | None, structure_source: str) -> dict[str, Any]:
    variant = "root"
    adopted_from_step = None
    if mode == CREATION_MODE_ADOPT_RELAX_CHILD:
        variant = "relax_child"
        adopted_from_step = "relax"
    elif mode == CREATION_MODE_ADOPT_PRIMITIVE_CHILD:
        variant = "primitive"
        adopted_from_step = "relax"

    return {
        "version": LINEAGE_VERSION,
        "system_id": new_name,
        "display_name": new_name,
        "parent_system": source_dir.name if source_dir is not None else None,
        "source_kind": mode,
        "structure_variant": variant,
        "structure_source": structure_source,
        "adopted_from_step": adopted_from_step,
        "quarantined": False,
        "created_at": now_iso(),
    }


def parse_poscar_text(text: str) -> dict[str, Any]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) < 8:
        raise ValueError("POSCAR content is incomplete")

    title = lines[0].strip()
    scale = float(lines[1].split()[0])
    lattice = []
    for idx in range(2, 5):
        lattice.append([float(value) * scale for value in lines[idx].split()[:3]])

    species = lines[5].split()
    counts = [int(value) for value in lines[6].split()]

    line_idx = 7
    if lines[line_idx].lower().startswith("s"):
        line_idx += 1

    coord_mode = lines[line_idx].strip().lower()
    line_idx += 1
    total_atoms = sum(counts)

    raw_positions = []
    for idx in range(total_atoms):
        raw_positions.append([float(value) for value in lines[line_idx + idx].split()[:3]])

    expanded_species = []
    for element, count in zip(species, counts):
        expanded_species.extend([element] * count)

    atoms = []
    for element, position in zip(expanded_species, raw_positions):
        if coord_mode.startswith("d"):
            frac = position
            cart = [
                frac[0] * lattice[0][0] + frac[1] * lattice[1][0] + frac[2] * lattice[2][0],
                frac[0] * lattice[0][1] + frac[1] * lattice[1][1] + frac[2] * lattice[2][1],
                frac[0] * lattice[0][2] + frac[1] * lattice[1][2] + frac[2] * lattice[2][2],
            ]
        else:
            cart = [value * scale for value in position]
            frac = None
        atoms.append({"element": element, "cartesian": cart, "fractional": frac})

    return {
        "title": title,
        "lattice": lattice,
        "species": species,
        "counts": counts,
        "atoms": atoms,
    }


def parse_poscar_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return parse_poscar_text(path.read_text(errors="ignore"))
    except (ValueError, IndexError):
        return None


def _vector_length(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _vector_dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cell_volume(lattice: list[list[float]]) -> float:
    return abs(
        lattice[0][0] * (lattice[1][1] * lattice[2][2] - lattice[1][2] * lattice[2][1])
        - lattice[0][1] * (lattice[1][0] * lattice[2][2] - lattice[1][2] * lattice[2][0])
        + lattice[0][2] * (lattice[1][0] * lattice[2][1] - lattice[1][1] * lattice[2][0])
    )


def _cubic_lattice_summary(lattice: list[list[float]]) -> dict[str, Any] | None:
    if len(lattice) != 3:
        return None
    lengths = [_vector_length(vector) for vector in lattice]
    average_length = sum(lengths) / 3.0
    if average_length <= 0.0:
        return None
    if max(abs(length - average_length) for length in lengths) > max(1e-3, average_length * 0.02):
        return None

    cosines: list[float] = []
    for first, second in ((0, 1), (0, 2), (1, 2)):
        denominator = lengths[first] * lengths[second]
        if denominator <= 0.0:
            return None
        cosines.append(_vector_dot(lattice[first], lattice[second]) / denominator)

    average_cosine = sum(cosines) / 3.0
    if max(abs(value - average_cosine) for value in cosines) > 0.04:
        return None

    cosine_tolerance = 0.05
    if max(abs(value) for value in cosines) <= cosine_tolerance:
        return {
            "kind": "simple_cubic",
            "conventional_cubic_a_A": average_length,
            "distinct_from_primitive": False,
            "display_label": "Cubic a",
            "note": "Primitive cell already matches the conventional cubic axes.",
        }
    if max(abs(value - 0.5) for value in cosines) <= cosine_tolerance:
        return {
            "kind": "primitive_fcc",
            "conventional_cubic_a_A": average_length * math.sqrt(2.0),
            "distinct_from_primitive": True,
            "display_label": "Cubic a",
            "note": "Derived from a primitive fcc cell; primitive |a1| is shorter by sqrt(2).",
        }
    if max(abs(value + (1.0 / 3.0)) for value in cosines) <= cosine_tolerance:
        return {
            "kind": "primitive_bcc",
            "conventional_cubic_a_A": average_length * (2.0 / math.sqrt(3.0)),
            "distinct_from_primitive": True,
            "display_label": "Cubic a",
            "note": "Derived from a primitive bcc cell; primitive |a1| is shorter by sqrt(3)/2.",
        }
    return None


def _structure_mass_amu(structure: dict[str, Any]) -> float | None:
    species = structure.get("species", [])
    counts = structure.get("counts", [])
    total = 0.0
    for element, count in zip(species, counts):
        mass = ATOMIC_MASSES.get(str(element))
        if mass is None:
            return None
        total += mass * int(count)
    return total


def _hydrogen_wt_percent(structure: dict[str, Any]) -> float | None:
    species = structure.get("species", [])
    counts = structure.get("counts", [])
    mass_total = _structure_mass_amu(structure)
    if mass_total in {None, 0.0}:
        return None
    hydrogen_mass = 0.0
    for element, count in zip(species, counts):
        if str(element) == "H":
            hydrogen_mass += ATOMIC_MASSES["H"] * int(count)
    return 100.0 * hydrogen_mass / mass_total


def _parse_mag(path: Path) -> float | None:
    if not path.exists():
        return None
    mags = re.findall(r"mag=\s*([-0-9.]+)", path.read_text(errors="ignore"))
    return float(mags[-1]) if mags else None


def derived_summary(system_dir: Path) -> dict[str, Any]:
    relax_structure = parse_poscar_file(system_dir / "runs" / "relax" / "CONTCAR")
    base_structure = relax_structure or parse_poscar_file(system_dir / "POSCAR")
    summary: dict[str, Any] = {}

    if base_structure:
        lattice = base_structure.get("lattice", [])
        if len(lattice) == 3:
            primitive_a = _vector_length(lattice[0])
            summary["a_A"] = primitive_a
            summary["primitive_a_A"] = primitive_a
            summary["a_display_A"] = primitive_a
            summary["a_display_label"] = "Primitive |a1|"
            summary["volume_A3"] = _cell_volume(lattice)
            mass_amu = _structure_mass_amu(base_structure)
            if mass_amu is not None and summary["volume_A3"] > 0:
                summary["density_g_cm3"] = (mass_amu * 1.66053906660) / summary["volume_A3"]
            cubic_summary = _cubic_lattice_summary(lattice)
            if cubic_summary:
                summary["cubic_lattice_kind"] = cubic_summary["kind"]
                summary["conventional_cubic_a_A"] = cubic_summary["conventional_cubic_a_A"]
                summary["a_display_A"] = cubic_summary["conventional_cubic_a_A"]
                summary["a_display_label"] = cubic_summary["display_label"]
                summary["a_display_note"] = cubic_summary["note"]
                summary["conventional_cubic_distinct"] = cubic_summary["distinct_from_primitive"]
        hydrogen_wt = _hydrogen_wt_percent(base_structure)
        if hydrogen_wt is not None:
            summary["hydrogen_wt_percent"] = hydrogen_wt

    moment = None
    for candidate in (
        system_dir / "runs" / "scf" / "OSZICAR",
        system_dir / "runs" / "relax" / "OSZICAR",
    ):
        moment = _parse_mag(candidate)
        if moment is not None:
            break

    if moment is None:
        completed_electronic_step = any(
            (system_dir / "runs" / step / "OUTCAR").exists()
            for step in ("scf", "relax")
        )
        relax_ispin = (_incar_value(system_dir / "INCAR.relax", "ISPIN") or "").strip()
        scf_ispin = (_incar_value(system_dir / "INCAR.scf", "ISPIN") or "").strip()
        if completed_electronic_step and (relax_ispin == "1" or scf_ispin == "1"):
            moment = 0.0
    if moment is not None:
        summary["magnetic_moment_muB"] = moment

    return summary


def system_summary(system_dir: Path) -> dict[str, Any]:
    summary = dict(load_summary().get(system_dir.name, {}))
    summary.update(derived_summary(system_dir))
    return summary


def input_review_entries(system_dir: Path) -> list[dict[str, Any]]:
    ordered_paths = [
        "POSCAR",
        "INCAR.relax",
        "INCAR.scf",
        "INCAR.dos",
        "INCAR.converge",
        "INCAR.band",
        "INCAR.elastic",
        "INCAR.charge",
        "INCAR.phonon",
        "KPOINTS.relax",
        "KPOINTS.scf",
        "KPOINTS.dos",
        "KPATH.in",
        "KPOINTS.band",
        "KPOINTS.phonon",
        "band.conf",
        "BORN",
    ]
    entries: list[dict[str, Any]] = []
    states = file_generation_states(system_dir, ordered_paths)
    for relative_path in ordered_paths:
        full_path = system_dir / relative_path
        if not full_path.exists() or not full_path.is_file():
            continue
        entries.append(
            {
                "path": relative_path,
                "content": full_path.read_text(errors="ignore"),
                "generated": relative_path in GENERATED_FILE_CANDIDATES,
                "read_only": relative_path not in WRITABLE_FILE_CANDIDATES,
                "generation_state": states.get(relative_path, {}),
            }
        )
    return entries


def combined_input_review_text(entries: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for entry in entries:
        sections.append(f"===== {entry['path']} =====\n{entry['content'].rstrip()}\n")
    return "\n".join(sections).rstrip() + ("\n" if sections else "")


def poscar_species(path: Path) -> list[str]:
    lines = [line.rstrip() for line in path.read_text(errors="ignore").splitlines() if line.strip()]
    if len(lines) < 7:
        return []
    return lines[5].split()


def potcar_titles(path: Path) -> list[str]:
    titles: list[str] = []
    pattern = re.compile(r"TITEL\s*=\s*PAW_[A-Z0-9]+\s+(\S+)")
    for line in path.read_text(errors="ignore").splitlines():
        match = pattern.search(line)
        if match:
            titles.append(match.group(1))
    return titles


def _max_potcar_enmax(path: Path) -> float | None:
    return max_potcar_enmax(path)


def _merge_warnings(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if item not in seen:
                merged.append(item)
                seen.add(item)
    return merged


def _triplet_from_metadata(metadata: dict[str, Any], key: str, fallback: list[int]) -> list[int]:
    raw = metadata.get(key)
    if isinstance(raw, list) and len(raw) == 3:
        try:
            return [max(1, int(value)) for value in raw]
        except (TypeError, ValueError):
            return fallback
    return fallback


def _product(values: list[int]) -> int:
    result = 1
    for value in values:
        result *= max(1, int(value))
    return result


def _incar_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", re.IGNORECASE)
    for line in path.read_text(errors="ignore").splitlines():
        match = pattern.match(line.split("!")[0].split("#")[0].strip())
        if match:
            return match.group(1).strip()
    return None


def _incar_value_from_text(text: str, key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", re.IGNORECASE)
    for line in text.splitlines():
        match = pattern.match(line.split("!")[0].split("#")[0].strip())
        if match:
            return match.group(1).strip()
    return None


def _normalize_incar_comparison_value(key: str, value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        normalized = INCAR_COMPARISON_DEFAULTS.get(key, "")
    if not normalized:
        return None
    upper = re.sub(r"\s+", " ", normalized.upper())
    if upper in {".TRUE.", "TRUE", "T", "1", "YES", "Y"}:
        return "TRUE"
    if upper in {".FALSE.", "FALSE", "F", "0", "NO", "N"}:
        return "FALSE"
    if key in {"ENCUT", "AEXX", "HFSCREEN"}:
        try:
            number = float(upper)
        except ValueError:
            return upper
        return str(int(number)) if abs(number - round(number)) < 1e-9 else f"{number:g}"
    return upper


def _reference_incar_snapshot(system_dir: Path, step: str) -> dict[str, Any] | None:
    relative_path = f"INCAR.{step}"
    manifest_path = system_dir / "runs" / step / "run_manifest.json"
    payload = read_json(manifest_path, {})
    snapshots = payload.get("input_snapshots")
    if isinstance(snapshots, dict):
        snapshot = snapshots.get(relative_path)
        if isinstance(snapshot, dict) and isinstance(snapshot.get("content"), str):
            return {
                "content": snapshot["content"],
                "source_kind": "run_manifest",
                "source_label": f"completed {step} run manifest",
                "path": relative_path,
            }

    path = system_dir / relative_path
    if path.exists() and path.is_file():
        return {
            "content": path.read_text(encoding="utf-8", errors="ignore"),
            "source_kind": "workspace_input",
            "source_label": f"current {relative_path}",
            "path": relative_path,
        }
    return None


def _canonical_json_sha256_text(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _sha256_bytes(text.encode("utf-8"))
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _sha256_bytes(canonical.encode("utf-8"))


def _snapshot_matches_current_text_file(snapshot: dict[str, Any], current_path: Path) -> bool:
    if not current_path.exists() or not current_path.is_file():
        return False
    snapshot_content = snapshot.get("content")
    if not isinstance(snapshot_content, str):
        return False
    current_content = current_path.read_text(encoding="utf-8", errors="ignore")
    if current_path.name == "metadata.json":
        return _canonical_json_sha256_text(snapshot_content) == _canonical_json_sha256_text(current_content)
    expected_sha = str(snapshot.get("sha256") or "")
    current_sha = _sha256_bytes(current_content.encode("utf-8"))
    return current_sha == (expected_sha or _sha256_bytes(snapshot_content.encode("utf-8")))


def _baseline_input_label(role: str, relative_path: str) -> str:
    if role == "structure":
        return f"structure input ({relative_path})"
    if role == "kpoints":
        return f"KPOINTS input ({relative_path})"
    if role == "incar":
        return f"INCAR input ({relative_path})"
    if role == "potcar":
        return "POTCAR"
    return relative_path


def _current_baseline_inputs(system_dir: Path, step: str) -> dict[str, Path]:
    resolved = _resolved_step_input_paths(system_dir, step, {})
    filtered = {
        role: path
        for role, path in resolved.items()
        if role in {"structure", "incar", "kpoints"}
    }
    potcar_path = system_dir / "POTCAR"
    if potcar_path.exists() and potcar_path.is_file():
        filtered["potcar"] = potcar_path
    return filtered


def _baseline_steps_for_submission(system_dir: Path, step: str) -> list[str]:
    normalized = str(step or "").strip().lower()
    downstream_structure = resolved_structure_path(system_dir, "scf") != (system_dir / "POSCAR")
    if normalized == "scf":
        return ["relax"] if downstream_structure else []
    if normalized in {"dos", "band", "charge"}:
        baselines = ["scf"]
        if downstream_structure:
            baselines.insert(0, "relax")
        return baselines
    if normalized == "converge":
        return ["relax"] if resolved_structure_path(system_dir, "converge") != (system_dir / "POSCAR") else []
    if normalized in {"elastic", "phonon"}:
        return ["relax"]
    return []


def _baseline_step_freshness(system_dir: Path, baseline_step: str, consumer_step: str) -> dict[str, list[str]]:
    manifest_path = system_dir / "runs" / baseline_step / "run_manifest.json"
    payload = read_json(manifest_path, {})
    if not isinstance(payload, dict) or not payload:
        return {
            "blocking_errors": [],
            "warnings": [
                f"Completed {baseline_step} baseline cannot be freshness-checked because runs/{baseline_step}/run_manifest.json is missing."
            ],
        }

    resolved_inputs = payload.get("resolved_inputs")
    snapshots = payload.get("input_snapshots")
    potcar_summary = payload.get("potcar_summary")
    if not isinstance(resolved_inputs, dict):
        resolved_inputs = {}
    if not isinstance(snapshots, dict):
        snapshots = {}
    if not isinstance(potcar_summary, dict):
        potcar_summary = {}

    blockers: list[str] = []
    warnings: list[str] = []

    for role, current_path in _current_baseline_inputs(system_dir, baseline_step).items():
        current_relative = _relative_system_path(system_dir, current_path)
        label = _baseline_input_label(role, current_relative)
        if baseline_step == "relax" and role == "kpoints":
            warnings.append(
                f"Completed relax baseline used its saved KPOINTS input, but {consumer_step} will use its own current mesh. "
                "This is normal when rerunning static follow-up steps with a different mesh."
            )
            continue
        if role == "potcar":
            expected_sha = str(potcar_summary.get("sha256") or "")
            if not expected_sha:
                warnings.append(
                    f"Completed {baseline_step} baseline has no POTCAR digest in its run manifest, so POTCAR freshness could not be verified before {consumer_step}."
                )
                continue
            if expected_sha != _sha256_bytes(current_path.read_bytes()):
                blockers.append(f"Completed {baseline_step} baseline no longer matches the current {label}. Re-run {baseline_step} before {consumer_step}.")
            continue

        expected_relative = str(resolved_inputs.get(role) or "")
        if not expected_relative:
            warnings.append(
                f"Completed {baseline_step} baseline has no recorded {role} input in its run manifest, so that input could not be freshness-checked before {consumer_step}."
            )
            continue
        if expected_relative != current_relative:
            blockers.append(
                f"Completed {baseline_step} baseline used {expected_relative} for {role}, but the current {role} input is {current_relative}. Re-run {baseline_step} before {consumer_step}."
            )
            continue
        snapshot = snapshots.get(expected_relative)
        if not isinstance(snapshot, dict):
            warnings.append(
                f"Completed {baseline_step} baseline is missing the recorded snapshot for {expected_relative}, so that input could not be freshness-checked before {consumer_step}."
            )
            continue
        if not _snapshot_matches_current_text_file(snapshot, current_path):
            blockers.append(f"Completed {baseline_step} baseline no longer matches the current {label}. Re-run {baseline_step} before {consumer_step}.")

    return {
        "blocking_errors": blockers,
        "warnings": warnings,
    }


def _upstream_baseline_consistency(system_dir: Path, step: str) -> dict[str, list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    for baseline_step in _baseline_steps_for_submission(system_dir, step):
        if step_status(system_dir, baseline_step) != "completed":
            continue
        report = _baseline_step_freshness(system_dir, baseline_step, step)
        blockers.extend(report["blocking_errors"])
        warnings = _merge_warnings(warnings, report["warnings"])
    return {
        "blocking_errors": blockers,
        "warnings": warnings,
    }


def _incar_consistency_reference_steps(system_dir: Path, step: str) -> list[str]:
    normalized = str(step or "").strip().lower()
    if normalized in {"dos", "band", "charge"}:
        return ["scf"] if step_status(system_dir, "scf") == "completed" else []
    if normalized == "elastic":
        return ["relax"] if step_status(system_dir, "relax") == "completed" else []
    if normalized == "phonon":
        references: list[str] = []
        if step_status(system_dir, "relax") == "completed":
            references.append("relax")
        if step_status(system_dir, "scf") == "completed":
            references.append("scf")
        return references
    return []


def _downstream_incar_consistency(system_dir: Path, step: str) -> dict[str, list[str]]:
    consumer_path = system_dir / f"INCAR.{step}"
    if not consumer_path.exists() or not consumer_path.is_file():
        return {"blocking_errors": [], "warnings": []}

    consumer_text = consumer_path.read_text(encoding="utf-8", errors="ignore")
    blockers: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()

    for reference_step in _incar_consistency_reference_steps(system_dir, step):
        reference = _reference_incar_snapshot(system_dir, reference_step)
        if reference is None:
            continue
        for key, label, severity in INCAR_CONSISTENCY_FIELDS:
            reference_value = _normalize_incar_comparison_value(key, _incar_value_from_text(reference["content"], key))
            consumer_value = _normalize_incar_comparison_value(key, _incar_value_from_text(consumer_text, key))
            if reference_value == consumer_value:
                continue
            marker = (reference_step, key)
            if marker in seen:
                continue
            seen.add(marker)
            message = (
                f"INCAR.{step} differs from {reference['source_label']} for {label} "
                f"({key}: {reference_value or '<unset>'} vs {consumer_value or '<unset>'})."
            )
            if severity == "block" and reference["source_kind"] == "run_manifest":
                blockers.append(f"{message} Re-run upstream steps or align the {step} input first.")
            else:
                warnings.append(f"{message} If upstream runs already finished under older settings, rerun them before {step}.")

    return {
        "blocking_errors": blockers,
        "warnings": warnings,
    }


def _phonon_input_consistency(system_dir: Path) -> dict[str, list[str]]:
    return _downstream_incar_consistency(system_dir, "phonon")


def _incar_truthy(path: Path, key: str) -> bool:
    value = (_incar_value(path, key) or "").strip().upper()
    return value in {".TRUE.", "TRUE", "T", "1", "Y", "YES"}


def _incar_float(path: Path, key: str) -> float | None:
    raw = _incar_value(path, key)
    if raw is None:
        return None
    try:
        return float(raw.split()[0])
    except (IndexError, ValueError):
        return None


def _outcar_is_hse(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(errors="ignore")
    if re.search(r"^\s*LHFCALC\s*=\s*T", text, re.IGNORECASE | re.MULTILINE):
        return True
    return "HFSCREEN" in text.upper()


def _kpoints_mesh(path: Path) -> list[int] | None:
    if not path.exists():
        return None
    lines = [line.strip() for line in path.read_text(errors="ignore").splitlines() if line.strip()]
    if len(lines) < 4:
        return None
    parts = lines[3].split()
    if len(parts) < 3:
        return None
    try:
        return [max(1, int(float(value))) for value in parts[:3]]
    except ValueError:
        return None


def _band_conf_truthy(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        current_key, value = stripped.split("=", 1)
        if current_key.strip().upper() != key.strip().upper():
            continue
        normalized = value.strip().upper().replace(" ", "")
        return normalized in {".TRUE.", "TRUE", "READ"}
    return False


def _phonon_born_path(system_dir: Path) -> Path | None:
    for candidate in (
        system_dir / "BORN",
        system_dir / "runs" / "phonon" / "BORN",
        system_dir / "runs" / "charge" / "BORN",
    ):
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _phonon_nac_requested(system_dir: Path) -> bool:
    return _band_conf_truthy(system_dir / "band.conf", "NAC")


def _band_conf_value_from_text(text: str, key: str) -> str | None:
    target = key.strip().upper()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        current_key, value = line.split("=", 1)
        if current_key.strip().upper() == target:
            return value.strip()
    return None


def _normalize_triplet_text(text: str) -> str:
    values = [chunk for chunk in re.split(r"[\s,]+", text.strip()) if chunk]
    if len(values) != 3:
        raise ValueError("Q_DIRECTION must contain exactly three numeric values.")
    numbers: list[float] = []
    for value in values:
        try:
            numbers.append(float(value))
        except ValueError as exc:
            raise ValueError("Q_DIRECTION values must be numeric.") from exc
    return " ".join(f"{number:g}" for number in numbers)


def _set_or_replace_key_value_line(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
    replaced = False
    lines: list[str] = []
    for raw_line in text.splitlines():
        if pattern.match(raw_line.split("#", 1)[0].strip()):
            if not replaced:
                lines.append(f"{key} = {value}")
                replaced = True
            continue
        lines.append(raw_line.rstrip("\n"))
    if not replaced:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append(f"{key} = {value}")
    return "\n".join(lines).rstrip() + "\n"


def _remove_key_value_line(text: str, key: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
    lines = [raw_line.rstrip("\n") for raw_line in text.splitlines() if not pattern.match(raw_line.split("#", 1)[0].strip())]
    return ("\n".join(lines).rstrip() + "\n") if lines else ""


def _phonopy_vasp_born_command() -> str | None:
    for candidate in ("phonopy-vasp-born", "outcar-born"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _phonon_nac_payload(system_dir: Path) -> dict[str, Any]:
    band_conf_path = system_dir / "band.conf"
    band_conf_text = band_conf_path.read_text(errors="ignore") if band_conf_path.exists() else ""
    q_direction = _band_conf_value_from_text(band_conf_text, "Q_DIRECTION") or ""
    born_path = _phonon_born_path(system_dir)
    charge_outcar = system_dir / "runs" / "charge" / "OUTCAR"
    tool = _phonopy_vasp_born_command()
    return {
        "enabled": _phonon_nac_requested(system_dir),
        "q_direction_text": q_direction,
        "band_conf_available": band_conf_path.exists(),
        "band_conf_path": "band.conf" if band_conf_path.exists() else None,
        "born_available": born_path is not None,
        "born_path": _relative_system_path(system_dir, born_path) if born_path is not None else None,
        "charge_lepsilon": _incar_truthy(system_dir / "INCAR.charge", "LEPSILON"),
        "charge_outcar_ready": charge_outcar.exists() and charge_outcar.is_file(),
        "charge_outcar_path": _relative_system_path(system_dir, charge_outcar) if charge_outcar.exists() and charge_outcar.is_file() else "runs/charge/OUTCAR",
        "born_tool_available": tool is not None,
        "born_tool": Path(tool).name if tool else None,
    }


def _update_phonon_nac_settings(system_dir: Path, enabled: bool, q_direction_text: str) -> dict[str, Any]:
    band_conf_path = system_dir / "band.conf"
    generated_baseline = None
    if band_conf_path.exists():
        band_conf_text = band_conf_path.read_text(encoding="utf-8", errors="ignore")
    else:
        generated = generate_input_content(system_dir, "band.conf")
        generated_baseline = generated["content"]
        band_conf_text = generated_baseline

    band_conf_text = _set_or_replace_key_value_line(band_conf_text, "NAC", ".TRUE." if enabled else ".FALSE.")
    if enabled and q_direction_text.strip():
        band_conf_text = _set_or_replace_key_value_line(band_conf_text, "Q_DIRECTION", _normalize_triplet_text(q_direction_text))
    else:
        band_conf_text = _remove_key_value_line(band_conf_text, "Q_DIRECTION")

    atomic_write_text(band_conf_path, band_conf_text)
    if generated_baseline is not None:
        record_generated_file(system_dir, "band.conf", generated_baseline)
    synced_metadata_keys = sync_metadata_from_band_conf(system_dir, band_conf_text)
    return {
        "path": "band.conf",
        "saved_at": now_iso(),
        "synced_metadata_keys": synced_metadata_keys,
        "phonon_nac": _phonon_nac_payload(system_dir),
    }


def _prepare_charge_for_born(system_dir: Path) -> dict[str, Any]:
    incar_path = system_dir / "INCAR.charge"
    generated_baseline = None
    if incar_path.exists():
        content = incar_path.read_text(encoding="utf-8", errors="ignore")
    else:
        generated = generate_input_content(system_dir, "INCAR.charge")
        generated_baseline = generated["content"]
        content = generated_baseline

    content = _set_or_replace_key_value_line(content, "LEPSILON", ".TRUE.")
    content = _set_or_replace_key_value_line(content, "IBRION", "-1")
    content = _set_or_replace_key_value_line(content, "NSW", "0")
    content = _set_or_replace_key_value_line(content, "LREAL", ".FALSE.")
    content = _set_or_replace_key_value_line(content, "EDIFF", "1E-8")

    atomic_write_text(incar_path, content)
    if generated_baseline is not None:
        record_generated_file(system_dir, "INCAR.charge", generated_baseline)
    return {
        "path": "INCAR.charge",
        "saved_at": now_iso(),
        "phonon_nac": _phonon_nac_payload(system_dir),
    }


def _build_born_from_charge(system_dir: Path) -> dict[str, Any]:
    charge_dir = system_dir / "runs" / "charge"
    outcar_path = charge_dir / "OUTCAR"
    if not outcar_path.exists() or not outcar_path.is_file():
        raise ValueError("runs/charge/OUTCAR is required before BORN can be generated.")
    tool = _phonopy_vasp_born_command()
    if not tool:
        raise ValueError("phonopy-vasp-born is not available in PATH.")

    poscar_path = charge_dir / "POSCAR"
    if not poscar_path.exists():
        poscar_path = resolved_structure_path(system_dir, "charge")
    if not poscar_path.exists() or not poscar_path.is_file():
        raise ValueError("A POSCAR matching the charge run is required before BORN can be generated.")

    completed = subprocess.run(
        [tool, "--outcar", str(outcar_path), str(poscar_path)],
        cwd=str(charge_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
        check=False,
    )
    born_text = completed.stdout.strip()
    if completed.returncode != 0:
        raise ValueError(f"phonopy-vasp-born failed: {born_text or 'no output'}")
    if not born_text:
        raise ValueError("phonopy-vasp-born completed without producing BORN content.")

    rendered = born_text.rstrip() + "\n"
    atomic_write_text(system_dir / "BORN", rendered)
    atomic_write_text(charge_dir / "BORN", rendered)
    return {
        "path": "BORN",
        "saved_at": now_iso(),
        "source_outcar": _relative_system_path(system_dir, outcar_path),
        "source_poscar": _relative_system_path(system_dir, poscar_path),
        "command": [Path(tool).name, "--outcar", str(outcar_path), str(poscar_path)],
        "phonon_nac": _phonon_nac_payload(system_dir),
    }


def _phonon_force_policy(system_dir: Path, metadata: dict[str, Any]) -> dict[str, list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    phonon_incar = system_dir / "INCAR.phonon"
    if not phonon_incar.exists() or not phonon_incar.is_file():
        return {"blocking_errors": blockers, "warnings": warnings}

    ediff = _incar_float(phonon_incar, "EDIFF")
    if ediff is None:
        warnings.append("INCAR.phonon does not set EDIFF explicitly. For finite-displacement forces, 1E-8 is the safer default.")
    elif ediff > 1e-6:
        blockers.append(f"INCAR.phonon uses EDIFF = {ediff:g}, which is too loose for the built-in finite-displacement phonon workflow. Tighten it to 1E-6 or below.")
    elif ediff > 1e-8:
        warnings.append(f"INCAR.phonon uses EDIFF = {ediff:g}. This is acceptable, but 1E-8 is a safer force-accuracy target for production phonons.")

    lreal = (_incar_value(phonon_incar, "LREAL") or "").strip().upper()
    if lreal and lreal not in {".FALSE.", "FALSE", "F", "0", "NO", "N"}:
        blockers.append("INCAR.phonon uses LREAL != .FALSE.. The built-in phonon workflow expects LREAL = .FALSE. for force accuracy.")

    ibrion = _incar_value(phonon_incar, "IBRION")
    if ibrion is not None and ibrion.strip() != "-1":
        warnings.append("INCAR.phonon does not currently show IBRION = -1, but the runner will force single-point finite-displacement settings at launch.")

    nsw = _incar_value(phonon_incar, "NSW")
    if nsw is not None and nsw.strip() != "0":
        warnings.append("INCAR.phonon does not currently show NSW = 0, but the runner will force single-point finite-displacement settings at launch.")

    isym = _incar_value(phonon_incar, "ISYM")
    if isym is not None and isym.strip() not in {"0", "-1"}:
        warnings.append("INCAR.phonon keeps ISYM symmetry enabled. For displaced supercells, ISYM = 0 is usually the safer choice.")

    electronic_type = str(metadata.get("electronic_type") or "auto").lower()
    ismear = _incar_value(phonon_incar, "ISMEAR")
    sigma = _incar_float(phonon_incar, "SIGMA")
    if electronic_type != "metal":
        if ismear is not None and ismear.strip() not in {"0", "-5"}:
            warnings.append("INCAR.phonon is not using the usual non-metal phonon smearing (ISMEAR = 0). Review the force-calculation policy before running phonon.")
        if sigma is not None and sigma > 0.02:
            warnings.append(f"INCAR.phonon uses SIGMA = {sigma:g}. For non-metal phonons, 0.01 is usually the safer default.")

    band_conf_nac = _phonon_nac_requested(system_dir)
    lepsilon = _incar_truthy(phonon_incar, "LEPSILON")
    born_path = _phonon_born_path(system_dir)
    if lepsilon and not band_conf_nac:
        blockers.append("INCAR.phonon sets LEPSILON = .TRUE., but band.conf does not request NAC = .TRUE.. Align the built-in phonon NAC workflow before submitting.")
    if band_conf_nac and born_path is None:
        blockers.append("band.conf requests NAC = .TRUE., but no BORN file is available in the workspace.")
    if born_path is not None and not band_conf_nac:
        warnings.append(f"{_relative_system_path(system_dir, born_path)} is available, but band.conf does not request NAC. LO-TO splitting will stay disabled in phonon post-processing.")
    potcar_symbols = metadata.get("potcar_symbols")
    if born_path is None and isinstance(potcar_symbols, list) and len(set(str(item) for item in potcar_symbols if item)) > 1:
        warnings.append("No BORN file is available. If this multi-species system is polar and you need LO-TO splitting, run a LEPSILON calculation and build BORN with phonopy-vasp-born before phonon post-processing.")

    return {"blocking_errors": blockers, "warnings": warnings}


def _magmom_entry_count(raw_value: str | None) -> int | None:
    if not raw_value:
        return None
    count = 0
    for token in raw_value.split():
        piece = token.strip()
        if not piece:
            continue
        if "*" in piece:
            left, right = piece.split("*", 1)
            try:
                repeat = int(float(left))
                float(right)
            except ValueError:
                return None
            count += max(0, repeat)
        else:
            try:
                float(piece)
            except ValueError:
                return None
            count += 1
    return count if count > 0 else None


def _magmom_has_nonzero(raw_value: str | None) -> bool:
    if not raw_value:
        return False
    for token in raw_value.split():
        piece = token.strip()
        if not piece:
            continue
        if "*" in piece:
            left, right = piece.split("*", 1)
            try:
                repeat = int(float(left))
                moment = float(right)
            except ValueError:
                return False
            if repeat > 0 and abs(moment) > 1e-9:
                return True
        else:
            try:
                if abs(float(piece)) > 1e-9:
                    return True
            except ValueError:
                return False
    return False


def _parse_magmom_values(raw_value: str | None) -> list[float]:
    if not raw_value:
        return []
    values: list[float] = []
    for token in raw_value.split():
        piece = token.strip()
        if not piece:
            continue
        if "*" in piece:
            left, right = piece.split("*", 1)
            try:
                repeat = int(float(left))
                moment = float(right)
            except ValueError:
                return []
            if repeat > 0:
                values.extend([moment] * repeat)
        else:
            try:
                values.append(float(piece))
            except ValueError:
                return []
    return values


def _effective_spin_metadata(system_dir: Path, metadata: dict[str, Any], step: str | None = None) -> tuple[bool, list[float]]:
    magmom = metadata.get("magmom") if isinstance(metadata.get("magmom"), list) else []
    magmom = [float(value) for value in magmom] if magmom else []
    magmom_nonzero = any(abs(float(value)) > 1e-9 for value in magmom)
    spin_polarized = bool(metadata.get("spin_polarized", False))

    candidates: list[Path] = []
    if step:
        candidates.append(system_dir / f"INCAR.{step}")
    for filename in ("INCAR.scf", "INCAR.relax", "INCAR"):
        path = system_dir / filename
        if path not in candidates:
            candidates.append(path)

    for incar_path in candidates:
        raw_ispin = (_incar_value(incar_path, "ISPIN") or "").strip()
        raw_magmom = _incar_value(incar_path, "MAGMOM")
        parsed_magmom = _parse_magmom_values(raw_magmom)
        if raw_ispin == "2" or _magmom_has_nonzero(raw_magmom):
            spin_polarized = True
        if parsed_magmom and any(abs(value) > 1e-9 for value in parsed_magmom):
            magmom = parsed_magmom
            magmom_nonzero = True
            break
        if raw_ispin == "2" and parsed_magmom and not magmom_nonzero:
            magmom = parsed_magmom

    return spin_polarized, magmom


def _band_conf_requests_force_constants(path: Path) -> bool:
    return _band_conf_truthy(path, "FORCE_CONSTANTS")


def _step_in_progress_states() -> set[str]:
    return {"running", "queued", "submitted_remote"}


def _legacy_archived_terminal_state(job: dict[str, Any]) -> str | None:
    if job.get("recovered_history") or job.get("backend") == "recovered" or job.get("target") == "archive":
        return None
    if str(job.get("state") or "").strip().lower() in {"paused", "stopped"} or job.get("control_state") in {"pause_requested", "stop_requested"}:
        return None

    exit_payload = _job_exit_code_payload(job)
    exit_code = exit_payload.get("exit_code") if isinstance(exit_payload, dict) else None
    if isinstance(exit_code, int):
        return "finished" if exit_code == 0 else "failed"

    launcher_candidates: list[Path] = []
    launcher_log = Path(job["launcher_log"]) if job.get("launcher_log") else None
    if launcher_log is not None:
        launcher_candidates.append(launcher_log)
    run_dir, _ = _job_result_source_run_dir(job)
    if run_dir is not None:
        archived_launcher = run_dir / "launcher.log"
        if archived_launcher not in launcher_candidates:
            launcher_candidates.append(archived_launcher)

    text = None
    for candidate in launcher_candidates:
        if candidate.exists() and candidate.is_file():
            text = candidate.read_text(errors="ignore")
            break
    if text is None:
        return None

    step = str(job.get("step") or "").strip()
    if step and f"Finished {step}" in text:
        return "finished"
    if re.search(r"\b(traceback|error|failed)\b", text, re.IGNORECASE):
        return "failed"
    return None


def _parse_band_gap(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    text = path.read_text(errors="ignore")
    gap_match = re.search(r"Band Gap \(eV\):\s+(.+)", text)
    if not gap_match:
        return None
    gap_values = re.findall(r"[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?", gap_match.group(1))
    if not gap_values:
        return None
    gap = float(gap_values[-1])
    fermi_match = re.search(r"Fermi Energy \(eV\):\s+(.+)", text)
    fermi_eV = None
    if fermi_match:
        fermi_values = re.findall(r"[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?", fermi_match.group(1))
        if fermi_values:
            fermi_eV = float(fermi_values[-1])
    character_match = re.search(r"Band Character:\s+([A-Za-z-]+)", text)
    if gap <= 0.01:
        character = "tiny-gap / metallic-like"
    elif gap < 0.5:
        character = "narrow-gap"
    else:
        character = "gapped"
    if character_match:
        raw_character = character_match.group(1).strip().lower()
        if raw_character == "direct":
            character = "direct-gap"
        elif raw_character == "indirect":
            character = "indirect-gap"
    return {
        "gap_eV": gap,
        "fermi_eV": fermi_eV,
        "character": character,
    }


def _eigenval_has_band_entries(path: Path) -> bool:
    if not _nonempty(path):
        return False

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            lines = [line.rstrip("\n") for _, line in zip(range(12), handle)]
    except OSError:
        return False

    payload = [line.strip() for line in lines[6:] if line.strip()]
    if len(payload) < 2:
        return False

    kpoint_fields = payload[0].split()
    band_fields = payload[1].split()
    if len(kpoint_fields) < 4 or len(band_fields) < 3:
        return False

    try:
        int(band_fields[0])
        float(band_fields[1])
        float(band_fields[2])
    except ValueError:
        return False

    return True


def _parse_band_summary(run_dir: Path) -> dict[str, Any] | None:
    gap_summary = _parse_band_gap(run_dir / "BAND_GAP")
    if gap_summary:
        return {
            "mode": "standard",
            "status": "gap",
            **gap_summary,
        }

    if _nonempty(run_dir / "PROCAR_OPT"):
        return {
            "mode": "hybrid",
            "status": "hybrid_ready",
            "character": "hybrid band ready",
            "source": "PROCAR_OPT",
        }

    if _eigenval_has_band_entries(run_dir / "EIGENVAL"):
        return {
            "mode": "standard",
            "status": "eigenval_ready",
            "character": "band eigenvalues ready",
            "source": "EIGENVAL",
        }

    return None


def _parse_first_float(text: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def _parse_fermi_reference(run_dir: Path) -> float | None:
    band_gap_summary = _parse_band_gap(run_dir / "BAND_GAP")
    if band_gap_summary and band_gap_summary.get("fermi_eV") is not None:
        return float(band_gap_summary["fermi_eV"])

    fermi_path = run_dir / "FERMI_ENERGY"
    if fermi_path.exists():
        value = _parse_first_float(fermi_path.read_text(errors="ignore"))
        if value is not None:
            return value

    outcar_path = run_dir / "OUTCAR"
    if outcar_path.exists():
        matches = re.findall(r"E-fermi\s*:\s*([-0-9.]+)", outcar_path.read_text(errors="ignore"))
        if matches:
            return float(matches[-1])
    return None


def _comparison_fermi_reference(system_dir: Path, run_dir: Path | None = None) -> tuple[float | None, str | None]:
    scf_dir = system_dir / "runs" / "scf"
    scf_fermi = _parse_fermi_reference(scf_dir)
    if scf_fermi is not None:
        return scf_fermi, "SCF"
    if run_dir is not None:
        local_fermi = _parse_fermi_reference(run_dir)
        if local_fermi is not None:
            return local_fermi, run_dir.name.upper()
    return None, None


def _shift_series_axis(series: list[dict[str, Any]], axis: str, delta: float) -> list[dict[str, Any]]:
    if abs(delta) < 1e-9:
        return series
    index = 0 if axis == "x" else 1
    shifted: list[dict[str, Any]] = []
    for item in series:
        points = []
        for pair in item.get("points", []):
            x_value = float(pair[0])
            y_value = float(pair[1])
            if index == 0:
                points.append([x_value + delta, y_value])
            else:
                points.append([x_value, y_value + delta])
        shifted.append({**item, "points": points})
    return shifted


def _dos_density_at_zero(series: list[dict[str, Any]]) -> float | None:
    density = 0.0
    found = False
    for item in series:
        points = item.get("points", [])
        if not points:
            continue
        nearest = min(points, key=lambda pair: abs(float(pair[0])))
        density += abs(float(nearest[1]))
        found = True
    return density if found else None


def _dos_source_run(
    system_dir: Path,
    *,
    preferred_step: str | None = None,
    preferred_run_dir: Path | None = None,
) -> tuple[str | None, Path | None]:
    if preferred_step and preferred_run_dir is not None and _nonempty(preferred_run_dir / "DOSCAR"):
        return preferred_step, preferred_run_dir
    for step in ("dos", "scf"):
        run_dir = system_dir / "runs" / step
        if _nonempty(run_dir / "DOSCAR"):
            return step, run_dir
    return None, None


def _parse_total_doscar(path: Path) -> dict[str, Any] | None:
    if not _nonempty(path):
        return None
    lines = path.read_text(errors="ignore").splitlines()
    if len(lines) < 7:
        return None
    header = lines[5].split()
    if len(header) < 4:
        return None
    try:
        energy_max = float(header[0])
        energy_min = float(header[1])
        points_count = int(float(header[2]))
        fermi_eV = float(header[3])
    except ValueError:
        return None
    if points_count <= 0 or len(lines) < 6 + points_count:
        return None

    data_lines = lines[6 : 6 + points_count]
    first_parts = data_lines[0].split()
    if len(first_parts) < 3:
        return None

    spin_polarized = len(first_parts) >= 5
    total_points: list[list[float]] = []
    up_points: list[list[float]] = []
    down_points: list[list[float]] = []
    y_values: list[float] = []
    dos_at_fermi: float | None = None
    fermi_distance: float | None = None

    for raw_line in data_lines:
        parts = raw_line.split()
        if len(parts) < 3:
            return None
        try:
            energy_rel = float(parts[0]) - fermi_eV
            if spin_polarized:
                up_value = float(parts[1])
                down_value = float(parts[2])
            else:
                total_value = float(parts[1])
        except ValueError:
            return None

        if spin_polarized:
            up_points.append([energy_rel, up_value])
            down_points.append([energy_rel, -down_value])
            y_values.extend((up_value, -down_value))
            current_fermi_density = up_value + down_value
        else:
            total_points.append([energy_rel, total_value])
            y_values.append(total_value)
            current_fermi_density = total_value

        current_distance = abs(energy_rel)
        if fermi_distance is None or current_distance < fermi_distance:
            fermi_distance = current_distance
            dos_at_fermi = current_fermi_density

    if not y_values:
        return None

    series: list[dict[str, Any]] = []
    if spin_polarized:
        series.append(
            {
                "label": "Spin up",
                "spin": "up",
                "color": "#c66c27",
                "points": up_points,
            }
        )
        series.append(
            {
                "label": "Spin down",
                "spin": "down",
                "color": "#4367c7",
                "points": down_points,
            }
        )
    else:
        series.append(
            {
                "label": "Total DOS",
                "spin": "total",
                "color": "#2e8b57",
                "points": total_points,
            }
        )

    return {
        "series": series,
        "spin_polarized": spin_polarized,
        "mirrored_spin": spin_polarized,
        "fermi_eV": fermi_eV,
        "dos_at_fermi": dos_at_fermi,
        "points_count": points_count,
        "x_range": [energy_min - fermi_eV, energy_max - fermi_eV],
        "y_range": [min(y_values), max(y_values)],
    }


def _normalize_k_label(label: str) -> str:
    cleaned = label.strip().replace("_", "").replace("\\", "")
    if not cleaned:
        return "?"
    upper = cleaned.upper()
    if upper in {"G", "GAMMA"}:
        return "Γ"
    return cleaned


def _coalesce_tick_labels(ticks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for item in ticks:
        x_value = round(float(item["x"]), 6)
        label = _normalize_k_label(str(item["label"]))
        if combined and abs(float(combined[-1]["x"]) - x_value) < 1e-6:
            labels = str(combined[-1]["label"]).split("|")
            if label not in labels:
                labels.append(label)
                combined[-1]["label"] = "|".join(labels)
            continue
        combined.append({"x": x_value, "label": label})
    return combined


def _cross_product(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _reciprocal_lattice(lattice: list[list[float]]) -> list[list[float]] | None:
    volume = _cell_volume(lattice)
    if volume <= 1e-12:
        return None
    scale = (2.0 * math.pi) / volume
    a_vec, b_vec, c_vec = lattice
    return [
        [value * scale for value in _cross_product(b_vec, c_vec)],
        [value * scale for value in _cross_product(c_vec, a_vec)],
        [value * scale for value in _cross_product(a_vec, b_vec)],
    ]


def _fractional_to_cartesian_recip(frac: list[float], reciprocal: list[list[float]] | None) -> list[float]:
    if reciprocal is None:
        return list(frac)
    return [
        frac[0] * reciprocal[0][0] + frac[1] * reciprocal[1][0] + frac[2] * reciprocal[2][0],
        frac[0] * reciprocal[0][1] + frac[1] * reciprocal[1][1] + frac[2] * reciprocal[2][1],
        frac[0] * reciprocal[0][2] + frac[1] * reciprocal[1][2] + frac[2] * reciprocal[2][2],
    ]


def _parse_klabels(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    ticks: list[dict[str, Any]] = []
    for line in path.read_text(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            x_value = float(parts[1])
        except ValueError:
            continue
        ticks.append({"x": x_value, "label": parts[0]})
    return _coalesce_tick_labels(ticks)


def _expanded_display_ticks(ticks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not ticks:
        return []

    expanded: list[dict[str, Any]] = []
    for index, item in enumerate(ticks):
        label = str(item.get("label") or "").strip()
        x_value = float(item.get("x") or 0.0)
        parts = [part.strip() for part in label.split("|") if part.strip()]
        if len(parts) <= 1:
            expanded.append({"x": x_value, "label": label})
            continue

        spacings: list[float] = []
        if index > 0:
            spacings.append(abs(x_value - float(ticks[index - 1].get("x") or 0.0)))
        if index + 1 < len(ticks):
            spacings.append(abs(float(ticks[index + 1].get("x") or 0.0) - x_value))
        positive_spacings = [value for value in spacings if value > 1e-6]
        base_spacing = min(positive_spacings) if positive_spacings else 0.3
        offset = min(base_spacing * 0.08, 0.08)
        start = -0.5 * offset * (len(parts) - 1)
        for part_index, part in enumerate(parts):
            expanded.append({"x": x_value + start + offset * part_index, "label": part})
    return expanded


def _parse_line_mode_path(path: Path, structure: dict[str, Any] | None) -> dict[str, Any] | None:
    if not path.exists():
        return None
    lines = [line.rstrip() for line in path.read_text(errors="ignore").splitlines() if line.strip()]
    if len(lines) < 6:
        return None
    try:
        points_per_segment = int(lines[1].split()[0])
    except (ValueError, IndexError):
        return None
    coord_lines: list[tuple[list[float], str]] = []
    for line in lines[4:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            coords = [float(parts[0]), float(parts[1]), float(parts[2])]
        except ValueError:
            continue
        label = " ".join(parts[3:]).strip()
        coord_lines.append((coords, label))
    if len(coord_lines) < 2:
        return None

    reciprocal = None
    if structure and isinstance(structure.get("lattice"), list):
        reciprocal = _reciprocal_lattice(structure["lattice"])

    x_values: list[float] = []
    tick_labels: list[dict[str, Any]] = []
    segment_labels: list[tuple[str, str]] = []
    offset = 0.0
    for segment_index in range(0, len(coord_lines) - 1, 2):
        start_coords, start_label = coord_lines[segment_index]
        end_coords, end_label = coord_lines[segment_index + 1]
        start_cart = _fractional_to_cartesian_recip(start_coords, reciprocal)
        end_cart = _fractional_to_cartesian_recip(end_coords, reciprocal)
        segment_length = _vector_length([
            end_cart[0] - start_cart[0],
            end_cart[1] - start_cart[1],
            end_cart[2] - start_cart[2],
        ])
        tick_labels.append({"x": offset, "label": start_label})
        end_offset = offset + segment_length
        tick_labels.append({"x": end_offset, "label": end_label})
        segment_labels.append((start_label, end_label))
        for point_index in range(points_per_segment):
            if points_per_segment <= 1:
                t_value = 0.0
            else:
                t_value = point_index / (points_per_segment - 1)
            x_values.append(offset + segment_length * t_value)
        offset = end_offset

    ticks = _coalesce_tick_labels(tick_labels)
    return {
        "x_values": x_values,
        "ticks": ticks,
        "verticals": [item["x"] for item in ticks],
        "segment_labels": segment_labels,
    }


def _select_band_indices(bands: list[list[float]], max_bands: int = 18, window: tuple[float, float] = (-6.0, 6.0)) -> list[int]:
    ranked: list[tuple[bool, float, int]] = []
    low, high = window
    for index, energies in enumerate(bands):
        if not energies:
            continue
        in_window = any(low <= value <= high for value in energies)
        min_abs = min(abs(value) for value in energies)
        ranked.append((not in_window, min_abs, index))
    ranked.sort()
    return sorted(index for _, _, index in ranked[:max_bands])


def _band_energy_window(series: list[dict[str, Any]]) -> list[float]:
    energies: list[float] = []
    for item in series:
        energies.extend(float(pair[1]) for pair in item.get("points", []))
    if not energies:
        return [-6.0, 6.0]
    low = min(energies)
    high = max(energies)
    clipped_low = max(-8.0, math.floor(low - 0.3))
    clipped_high = min(8.0, math.ceil(high + 0.3))
    if clipped_high - clipped_low < 2.0:
        center = 0.5 * (low + high)
        clipped_low = center - 1.5
        clipped_high = center + 1.5
    return [float(clipped_low), float(clipped_high)]


def _parse_reformatted_band_series(path: Path, spin_label: str, color: str) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[list[float]] = []
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        try:
            rows.append([float(value) for value in parts])
        except ValueError:
            continue
    if not rows or len(rows[0]) < 2:
        return []

    x_values = [row[0] for row in rows]
    bands = [[row[column] for row in rows] for column in range(1, len(rows[0]))]
    selected = _select_band_indices(bands)
    return [
        {
            "label": f"{spin_label}-{index + 1}",
            "spin": spin_label,
            "color": color,
            "points": [[x_value, energies[row_index]] for row_index, x_value in enumerate(x_values)],
        }
        for index in selected
        for energies in [bands[index]]
    ]


def _parse_standard_reformatted_band_series(run_dir: Path) -> list[dict[str, Any]]:
    series = _parse_reformatted_band_series(run_dir / "REFORMATTED_BAND_UP.dat", "up", "#b85a2d")
    series.extend(_parse_reformatted_band_series(run_dir / "REFORMATTED_BAND_DW.dat", "down", "#5b6ad0"))
    if series:
        return series
    return _parse_reformatted_band_series(run_dir / "REFORMATTED_BAND.dat", "band", "#b85a2d")


def _parse_eigenval_series(path: Path, x_values: list[float], fermi_eV: float, hybrid: bool = False) -> list[dict[str, Any]] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    lines = path.read_text(errors="ignore").splitlines()
    if len(lines) < 7:
        return None
    counts = lines[5].split()
    if len(counts) < 3:
        return None
    try:
        _, kpoints_count, bands_count = [int(value) for value in counts[:3]]
    except ValueError:
        return None
    if len(x_values) != kpoints_count:
        return None

    kpoint_energies_up: list[list[float]] = []
    kpoint_energies_down: list[list[float]] = []
    line_index = 6
    for _ in range(kpoints_count):
        while line_index < len(lines) and not lines[line_index].strip():
            line_index += 1
        if line_index >= len(lines):
            return None
        kpoint_line = lines[line_index].split()
        line_index += 1
        if len(kpoint_line) < 4:
            return None

        up: list[float] = []
        down: list[float] = []
        for _ in range(bands_count):
            if line_index >= len(lines):
                return None
            parts = lines[line_index].split()
            line_index += 1
            if len(parts) >= 5:
                up.append(float(parts[1]) - fermi_eV)
                down.append(float(parts[2]) - fermi_eV)
            elif len(parts) >= 3:
                up.append(float(parts[1]) - fermi_eV)
            else:
                return None
        kpoint_energies_up.append(up)
        if down:
            kpoint_energies_down.append(down)

    series: list[dict[str, Any]] = []
    up_bands = [[row[index] for row in kpoint_energies_up] for index in range(bands_count)]
    for index in _select_band_indices(up_bands):
        band = up_bands[index]
        series.append(
            {
                "label": f"{'hybrid' if hybrid else 'up'}-{index + 1}",
                "spin": "up",
                "color": "#b85a2d",
                "points": [[x_values[row_index], energy] for row_index, energy in enumerate(band)],
            }
        )

    if kpoint_energies_down:
        down_bands = [[row[index] for row in kpoint_energies_down] for index in range(bands_count)]
        for index in _select_band_indices(down_bands):
            band = down_bands[index]
            series.append(
                {
                    "label": f"down-{index + 1}",
                    "spin": "down",
                    "color": "#5b6ad0",
                    "points": [[x_values[row_index], energy] for row_index, energy in enumerate(band)],
                }
            )
    return series


def _band_artifacts(run_dir: Path) -> list[str]:
    names = [
        "BAND_GAP",
        "REFORMATTED_BAND.dat",
        "REFORMATTED_BAND_UP.dat",
        "REFORMATTED_BAND_DW.dat",
        "KLABELS",
        "KLINES.dat",
        "EIGENVAL",
        "PROCAR",
        "PROCAR_OPT",
        "FERMI_ENERGY",
        "KPOINTS",
        "KPOINTS_OPT",
        "OUTCAR",
        "vasprun.xml",
    ]
    return [name for name in names if _nonempty(run_dir / name)]


def _build_standard_band_visualization(system_dir: Path, run_dir: Path) -> dict[str, Any] | None:
    local_fermi = _parse_fermi_reference(run_dir)
    reference_fermi, reference_origin = _comparison_fermi_reference(system_dir, run_dir)
    display_fermi = reference_fermi if reference_fermi is not None else local_fermi
    shift_delta = 0.0
    if local_fermi is not None and reference_fermi is not None:
        shift_delta = local_fermi - reference_fermi

    series = _parse_standard_reformatted_band_series(run_dir)
    if not series:
        structure = parse_poscar_file(system_dir / "POSCAR")
        path_data = _parse_line_mode_path(run_dir / "KPOINTS", structure) or _parse_line_mode_path(system_dir / "KPOINTS.band", structure)
        fermi_eV = local_fermi if local_fermi is not None else display_fermi or 0.0
        if path_data:
            series = _parse_eigenval_series(run_dir / "EIGENVAL", path_data["x_values"], fermi_eV, hybrid=False) or []
            if series:
                if abs(shift_delta) > 1e-6:
                    series = _shift_series_axis(series, "y", shift_delta)
                    note = f"Band path reconstructed from EIGENVAL and aligned to the {reference_origin} Fermi level for cross-step comparison."
                else:
                    note = "Band path reconstructed from EIGENVAL because no reformatted VASPKIT band file was present."
                return {
                    "mode": "standard",
                    "status": "plot_ready",
                    "title": "Band path",
                    "note": note,
                    "series": series,
                    "x_ticks": path_data["ticks"],
                    "display_x_ticks": _expanded_display_ticks(path_data["ticks"]),
                    "verticals": path_data["verticals"],
                    "y_range": _band_energy_window(series),
                    "fermi_eV": display_fermi if display_fermi is not None else fermi_eV,
                    "energy_reference": "E - E_F (eV)",
                    "artifacts": _band_artifacts(run_dir),
                }
        return None

    ticks = _parse_klabels(run_dir / "KLABELS")
    if not ticks:
        structure = parse_poscar_file(system_dir / "POSCAR")
        path_data = _parse_line_mode_path(system_dir / "KPOINTS.band", structure)
        ticks = path_data["ticks"] if path_data else []
    if abs(shift_delta) > 1e-6:
        series = _shift_series_axis(series, "y", shift_delta)
        note = f"Standard band result reconstructed from VASPKIT reformatted band data and aligned to the {reference_origin} Fermi level for cross-step comparison."
    else:
        note = "Standard band result reconstructed from VASPKIT reformatted band data."
    return {
        "mode": "standard",
        "status": "plot_ready",
        "title": "Band path",
        "note": note,
        "series": series,
        "x_ticks": ticks,
        "display_x_ticks": _expanded_display_ticks(ticks),
        "verticals": [item["x"] for item in ticks],
        "y_range": _band_energy_window(series),
        "fermi_eV": display_fermi,
        "energy_reference": "E - E_F (eV)",
        "artifacts": _band_artifacts(run_dir),
    }


def _build_hybrid_band_visualization(system_dir: Path, run_dir: Path) -> dict[str, Any] | None:
    structure = parse_poscar_file(system_dir / "POSCAR")
    path_data = _parse_line_mode_path(run_dir / "KPOINTS_OPT", structure) or _parse_line_mode_path(system_dir / "KPOINTS.band", structure)
    local_fermi = _parse_fermi_reference(run_dir)
    reference_fermi, reference_origin = _comparison_fermi_reference(system_dir, run_dir)
    if local_fermi is None:
        local_fermi = reference_fermi
    display_fermi = reference_fermi if reference_fermi is not None else local_fermi
    shift_delta = 0.0
    if local_fermi is not None and reference_fermi is not None:
        shift_delta = local_fermi - reference_fermi
    if path_data and local_fermi is not None:
        series = _parse_eigenval_series(run_dir / "EIGENVAL", path_data["x_values"], local_fermi, hybrid=True)
        if series:
            if abs(shift_delta) > 1e-6:
                series = _shift_series_axis(series, "y", shift_delta)
                note = f"Hybrid/HSE band reconstructed from EIGENVAL and KPOINTS_OPT, aligned to the {reference_origin} Fermi level for cross-step comparison."
            else:
                note = "Hybrid/HSE band reconstructed from EIGENVAL and KPOINTS_OPT."
            return {
                "mode": "hybrid",
                "status": "plot_ready",
                "title": "Hybrid band path",
                "note": note,
                "series": series,
                "x_ticks": path_data["ticks"],
                "display_x_ticks": _expanded_display_ticks(path_data["ticks"]),
                "verticals": path_data["verticals"],
                "y_range": _band_energy_window(series),
                "fermi_eV": display_fermi,
                "energy_reference": "E - E_F (eV)",
                "artifacts": _band_artifacts(run_dir),
            }

    artifacts = _band_artifacts(run_dir)
    if "PROCAR_OPT" in artifacts or "EIGENVAL" in artifacts:
        return {
            "mode": "hybrid",
            "status": "artifact_ready",
            "title": "Hybrid band artifacts",
            "note": "Hybrid run finished and key artifacts are present, but an automatic line plot could not be reconstructed from the current files.",
            "series": [],
            "x_ticks": [],
            "verticals": [],
            "y_range": [-6.0, 6.0],
            "fermi_eV": display_fermi,
            "energy_reference": "E - E_F (eV)",
            "artifacts": artifacts,
        }
    return None


def band_visualization(system_dir: Path, run_dir: Path | None = None) -> dict[str, Any] | None:
    run_dir = run_dir or (system_dir / "runs" / "band")
    band_info = _parse_band_summary(run_dir)
    if not band_info:
        return None
    if band_info["mode"] == "hybrid":
        visualization = _build_hybrid_band_visualization(system_dir, run_dir)
    else:
        visualization = _build_standard_band_visualization(system_dir, run_dir)
    if visualization:
        return visualization
    return {
        "mode": band_info["mode"],
        "status": "artifact_ready",
        "title": "Band artifacts",
        "note": "Band outputs are present, but no plot-ready dataset could be reconstructed automatically.",
        "series": [],
        "x_ticks": [],
        "verticals": [],
        "y_range": [-6.0, 6.0],
        "fermi_eV": _parse_fermi_reference(run_dir),
        "energy_reference": "E - E_F (eV)",
        "artifacts": _band_artifacts(run_dir),
    }


def _phonon_artifacts(run_dir: Path) -> list[str]:
    names = [
        "band.yaml",
        "band.pdf",
        "FORCE_SETS",
        "phonopy.yaml",
        "phonopy_disp.yaml",
        "band.conf",
        "phonopy_plot.log",
    ]
    return [name for name in names if _nonempty(run_dir / name)]


def _phonon_dos_artifacts(run_dir: Path) -> list[str]:
    names = [
        "projected_dos.dat",
        "total_dos.dat",
        "mesh.yaml",
        "phonopy_dos.log",
        "phonopy_pdos.log",
        "FORCE_SETS",
        "phonopy.yaml",
    ]
    return [name for name in names if _nonempty(run_dir / name)]


def _phonon_frequency_window(series: list[dict[str, Any]]) -> list[float]:
    frequencies: list[float] = []
    for item in series:
        frequencies.extend(float(pair[1]) for pair in item.get("points", []))
    if not frequencies:
        return [-1.0, 10.0]
    low = min(frequencies)
    high = max(frequencies)
    padding = max((high - low) * 0.08, 0.5)
    plot_low = min(low - padding, -0.3 if low < 0.0 else -0.1)
    plot_high = high + padding
    if plot_high - plot_low < 2.0:
        center = 0.5 * (plot_high + plot_low)
        plot_low = center - 1.2
        plot_high = center + 1.2
    return [float(plot_low), float(plot_high)]


_PHONON_NQPOINT_RE = re.compile(r"^\s*nqpoint:\s*([0-9]+)\s*$")
_PHONON_NPATH_RE = re.compile(r"^\s*npath:\s*([0-9]+)\s*$")
_PHONON_DISTANCE_RE = re.compile(r"^\s*distance:\s*([-+0-9.eE]+)\s*$")
_PHONON_FREQUENCY_RE = re.compile(r"^\s*frequency:\s*([-+0-9.eE]+)\s*$")
_PHONON_MESH_RE = re.compile(r"^\s*mesh:\s*\[\s*([0-9]+)\s*,\s*([0-9]+)\s*,\s*([0-9]+)\s*\]\s*$")
_PHONON_SEGMENT_NQPOINT_ITEM_RE = re.compile(r"^\s*-\s*([0-9]+)\s*$")


def _phonon_band_cache_key(path: Path) -> tuple[str, int, int] | None:
    if not _nonempty(path):
        return None
    try:
        resolved = path.resolve()
        stat = resolved.stat()
    except OSError:
        return None
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=32)
def _parse_phonon_band_yaml_cached(cache_key: tuple[str, int, int]) -> dict[str, Any] | None:
    path = Path(cache_key[0])
    nqpoint: int | None = None
    npath: int | None = None
    distances: list[float] = []
    frequencies: list[float] = []
    segment_nqpoint: list[int] = []
    in_segment_nqpoint = False

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if in_segment_nqpoint:
                    match = _PHONON_SEGMENT_NQPOINT_ITEM_RE.match(line)
                    if match:
                        segment_nqpoint.append(int(match.group(1)))
                        continue
                    in_segment_nqpoint = False
                match = _PHONON_NQPOINT_RE.match(line)
                if match:
                    nqpoint = int(match.group(1))
                    continue
                match = _PHONON_NPATH_RE.match(line)
                if match:
                    npath = int(match.group(1))
                    continue
                if line.strip() == "segment_nqpoint:":
                    in_segment_nqpoint = True
                    continue
                match = _PHONON_DISTANCE_RE.match(line)
                if match:
                    distances.append(float(match.group(1)))
                    continue
                match = _PHONON_FREQUENCY_RE.match(line)
                if match:
                    frequencies.append(float(match.group(1)))
    except OSError:
        return None

    if not distances or not frequencies:
        return None
    if len(frequencies) % len(distances) != 0:
        return None

    branch_count = len(frequencies) // len(distances)
    if branch_count <= 0:
        return None

    bands_by_branch: list[list[float]] = [[] for _ in range(branch_count)]
    for offset in range(0, len(frequencies), branch_count):
        chunk = frequencies[offset : offset + branch_count]
        if len(chunk) != branch_count:
            return None
        for index, value in enumerate(chunk):
            bands_by_branch[index].append(value)

    negative_count = sum(1 for value in frequencies if value < 0.0)
    return {
        "distances": distances,
        "bands": bands_by_branch,
        "nqpoint": nqpoint or len(distances),
        "npath": npath or 0,
        "segment_nqpoint": segment_nqpoint,
        "min_frequency": min(frequencies),
        "negative_count": negative_count,
    }


def _parse_phonon_band_yaml(path: Path) -> dict[str, Any] | None:
    cache_key = _phonon_band_cache_key(path)
    if cache_key is None:
        return None
    return _parse_phonon_band_yaml_cached(cache_key)


def _distance_segment_point_counts(distances: list[float]) -> list[int]:
    if not distances:
        return []
    boundaries = [0]
    previous = float(distances[0])
    for index, value in enumerate(distances[1:], start=1):
        current = float(value)
        if current <= previous + 1e-9:
            boundaries.append(index)
        previous = current
    boundaries.append(len(distances))
    return [boundaries[index + 1] - boundaries[index] for index in range(len(boundaries) - 1)]


def _phonon_segment_point_counts(parsed: dict[str, Any]) -> list[int]:
    raw_counts = [int(value) for value in parsed.get("segment_nqpoint") or [] if int(value) > 0]
    if raw_counts and sum(raw_counts) == len(parsed.get("distances") or []):
        return raw_counts

    derived = _distance_segment_point_counts([float(value) for value in parsed.get("distances") or []])
    npath = int(parsed.get("npath") or 0)
    if derived and (npath <= 0 or len(derived) == npath):
        return derived
    return raw_counts


def _phonon_ticks_from_path_segments(
    segment_labels: list[tuple[str, str]],
    distances: list[float],
    segment_counts: list[int],
) -> list[dict[str, Any]]:
    if not segment_labels or not distances or not segment_counts:
        return []
    if len(segment_labels) != len(segment_counts):
        return []

    ticks: list[dict[str, Any]] = []
    offset = 0
    for (start_label, end_label), point_count in zip(segment_labels, segment_counts):
        if point_count <= 0:
            return []
        end_index = offset + point_count - 1
        if offset >= len(distances) or end_index >= len(distances):
            return []
        ticks.append({"x": float(distances[offset]), "label": start_label})
        ticks.append({"x": float(distances[end_index]), "label": end_label})
        offset += point_count

    if offset != len(distances):
        return []
    return _coalesce_tick_labels(ticks)


def _phonon_mesh_triplet(run_dir: Path) -> list[int] | None:
    mesh_yaml = run_dir / "mesh.yaml"
    if _nonempty(mesh_yaml):
        try:
            for line in mesh_yaml.read_text(errors="ignore").splitlines():
                match = _PHONON_MESH_RE.match(line)
                if match:
                    return [int(match.group(1)), int(match.group(2)), int(match.group(3))]
        except OSError:
            pass
    return _kpoints_mesh(run_dir / "KPOINTS")


def _parse_phonon_total_dos(path: Path) -> dict[str, Any] | None:
    if not _nonempty(path):
        return None

    points: list[list[float]] = []
    densities: list[float] = []
    negative_weight = 0.0

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                if len(parts) < 2:
                    return None
                try:
                    frequency = float(parts[0])
                    density = float(parts[1])
                except ValueError:
                    return None
                points.append([frequency, density])
                densities.append(density)
                if frequency < 0.0 and density > 0.0:
                    negative_weight += density
    except OSError:
        return None

    if not points or not densities:
        return None

    points.sort(key=lambda pair: pair[0])
    frequencies = [float(pair[0]) for pair in points]
    density_max = max(densities)

    x_min = min(frequencies)
    x_max = max(frequencies)
    x_padding = max((x_max - x_min) * 0.05, 0.5)
    x_low = x_min - x_padding
    x_high = x_max + x_padding
    if x_high - x_low < 2.0:
        center = 0.5 * (x_high + x_low)
        x_low = center - 1.2
        x_high = center + 1.2

    y_high = density_max * 1.08 if density_max > 0.0 else 1.0
    if y_high < 1.0:
        y_high = 1.0

    return {
        "points": points,
        "frequency_range": [float(x_low), float(x_high)],
        "density_range": [0.0, float(y_high)],
        "negative_weight": float(negative_weight),
        "max_density": float(density_max),
        "min_frequency": float(x_min),
        "max_frequency": float(x_max),
    }


def _phonon_pdos_palette(species: list[str]) -> list[str]:
    preferred = {
        "K": "#4aa3ff",
        "Ca": "#e25b52",
        "Co": "#cf5ae8",
        "H": "#58b84f",
    }
    fallback = [
        "#4aa3ff",
        "#e25b52",
        "#cf5ae8",
        "#58b84f",
        "#d08c33",
        "#1f7a8c",
        "#9a4d7c",
        "#6c7ae0",
    ]
    colors: list[str] = []
    fallback_index = 0
    for symbol in species:
        if symbol in preferred:
            colors.append(preferred[symbol])
            continue
        colors.append(fallback[fallback_index % len(fallback)])
        fallback_index += 1
    return colors


def _parse_phonon_projected_dos(path: Path, species: list[str], counts: list[int]) -> dict[str, Any] | None:
    if not _nonempty(path) or not species or not counts:
        return None

    expected_atoms = sum(int(value) for value in counts)
    if expected_atoms <= 0:
        return None

    colors = _phonon_pdos_palette(species)
    series_by_species: list[list[list[float]]] = [[] for _ in species]
    total_points: list[list[float]] = []
    density_values: list[float] = []
    negative_weight = 0.0

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                if len(parts) < 2:
                    return None
                try:
                    values = [float(piece) for piece in parts]
                except ValueError:
                    return None

                frequency = values[0]
                projected_values = values[1:]
                grouped: list[float]
                if len(projected_values) == expected_atoms:
                    grouped = []
                    offset = 0
                    for count in counts:
                        width = int(count)
                        grouped.append(sum(projected_values[offset : offset + width]))
                        offset += width
                elif len(projected_values) == len(species):
                    grouped = projected_values
                else:
                    return None

                total_density = sum(grouped)
                total_points.append([frequency, total_density])
                density_values.append(total_density)
                if frequency < 0.0 and total_density > 0.0:
                    negative_weight += total_density
                for index, density in enumerate(grouped):
                    series_by_species[index].append([frequency, density])
                    density_values.append(density)
    except OSError:
        return None

    if not total_points or not density_values:
        return None

    frequencies = [float(pair[0]) for pair in total_points]
    f_min = min(frequencies)
    f_max = max(frequencies)
    f_padding = max((f_max - f_min) * 0.05, 0.5)
    frequency_range = [float(f_min - f_padding), float(f_max + f_padding)]
    if frequency_range[1] - frequency_range[0] < 2.0:
        center = 0.5 * (frequency_range[1] + frequency_range[0])
        frequency_range = [center - 1.2, center + 1.2]

    density_max = max(density_values)
    density_upper = density_max * 1.08 if density_max > 0.0 else 1.0
    if density_upper < 1.0:
        density_upper = 1.0

    return {
        "total_points": total_points,
        "projected_series": [
            {
                "label": symbol,
                "color": colors[index],
                "points": series_by_species[index],
            }
            for index, symbol in enumerate(species)
        ],
        "frequency_range": frequency_range,
        "density_range": [0.0, float(density_upper)],
        "negative_weight": float(negative_weight),
        "max_density": float(density_max),
    }


def phonon_visualization(system_dir: Path, run_dir: Path | None = None) -> dict[str, Any] | None:
    run_dir = run_dir or (system_dir / "runs" / "phonon")
    band_yaml = run_dir / "band.yaml"
    parsed = _parse_phonon_band_yaml(band_yaml)
    if not parsed:
        if _nonempty(band_yaml):
            return {
                "mode": "phonon_dispersion",
                "status": "artifact_ready",
                "title": "Phonon dispersion artifacts",
                "note": "band.yaml is present, but a plot-ready phonon dispersion dataset could not be reconstructed automatically.",
                "series": [],
                "x_ticks": [],
                "display_x_ticks": [],
                "verticals": [],
                "y_range": [-1.0, 10.0],
                "frequency_reference": "Frequency (THz)",
                "artifacts": _phonon_artifacts(run_dir),
            }
        return None

    structure = parse_poscar_file(system_dir / "POSCAR")
    path_data = _parse_line_mode_path(system_dir / "KPATH.in", structure)
    x_values = parsed["distances"]
    ticks: list[dict[str, Any]] = []
    verticals: list[float] = []
    if path_data:
        ticks = _phonon_ticks_from_path_segments(
            path_data.get("segment_labels", []),
            [float(value) for value in parsed["distances"]],
            _phonon_segment_point_counts(parsed),
        )
        if not ticks and len(path_data["x_values"]) == len(parsed["distances"]):
            x_values = [float(value) for value in path_data["x_values"]]
            ticks = path_data["ticks"]
        verticals = [item["x"] for item in ticks]

    palette = [
        "#b85a2d",
        "#2e8b57",
        "#5b6ad0",
        "#b08b2d",
        "#1f7a8c",
        "#9a4d7c",
        "#c2643f",
        "#3f8f73",
    ]
    series = [
        {
            "label": f"branch-{index + 1}",
            "color": palette[index % len(palette)],
            "points": [[x_values[row_index], frequency] for row_index, frequency in enumerate(branch)],
        }
        for index, branch in enumerate(parsed["bands"])
    ]
    phonon_info = _parse_phonon_summary(band_yaml)
    note_parts = ["Phonon dispersion reconstructed from band.yaml."]
    if phonon_info:
        if phonon_info["negative_count"] == 0:
            note_parts.append("No imaginary modes detected in the plotted branches.")
        else:
            note_parts.append(f"{phonon_info['negative_count']} negative frequencies detected.")
    return {
        "mode": "phonon_dispersion",
        "status": "plot_ready",
        "title": "Phonon dispersion",
        "note": " ".join(note_parts),
        "series": series,
        "x_ticks": ticks,
        "display_x_ticks": _expanded_display_ticks(ticks),
        "verticals": verticals,
        "y_range": _phonon_frequency_window(series),
        "frequency_reference": "Frequency (THz)",
        "artifacts": _phonon_artifacts(run_dir),
    }


def phonon_dos_visualization(system_dir: Path, run_dir: Path | None = None) -> dict[str, Any] | None:
    run_dir = run_dir or (system_dir / "runs" / "phonon")
    structure = parse_poscar_file(system_dir / "POSCAR") or {"species": [], "counts": []}
    species = [str(symbol) for symbol in structure.get("species", [])]
    counts = [int(value) for value in structure.get("counts", [])]
    total = _parse_phonon_total_dos(run_dir / "total_dos.dat")
    projected = _parse_phonon_projected_dos(run_dir / "projected_dos.dat", species, counts)
    artifacts = _phonon_dos_artifacts(run_dir)
    mesh = _phonon_mesh_triplet(run_dir)
    mesh_text = " x ".join(str(value) for value in mesh) if mesh else None

    if not total and not projected:
        if artifacts or _nonempty(run_dir / "band.yaml"):
            note_parts = []
            if _nonempty(run_dir / "FORCE_SETS"):
                note_parts.append("FORCE_SETS is present, but phonon DOS artifacts are not available yet.")
            else:
                note_parts.append("Phonon artifacts are present, but a plot-ready phonon DOS dataset is missing.")
            if mesh_text:
                note_parts.append(f"Last detected phonopy mesh: {mesh_text}.")
            note_parts.append("Regenerate phonon post-processing to build the phonon DOS preview.")
            return {
                "mode": "phonon_total_dos",
                "status": "artifact_ready",
                "title": "Phonon DOS artifacts",
                "note": " ".join(note_parts),
                "series": [],
                "projected_series": [],
                "x_range": [-1.0, 10.0],
                "y_range": [0.0, 1.0],
                "frequency_range": [-1.0, 10.0],
                "density_range": [0.0, 1.0],
                "mesh": mesh,
                "frequency_reference": "Frequency (THz)",
                "density_reference": "states/THz",
                "artifacts": artifacts,
            }
        return None

    reference = projected or total
    note_parts = []
    if projected:
        note_parts.append("Element-resolved phonon DOS reconstructed from projected_dos.dat and grouped by species.")
    elif total:
        note_parts.append("Phonon total DOS reconstructed from total_dos.dat.")
    if mesh_text:
        note_parts.append(f"Sampled on mesh {mesh_text}.")
    if projected and not total:
        note_parts.append("Total DOS trace was reconstructed by summing the projected species channels.")
    elif total and not projected:
        note_parts.append("Projected phonon DOS is not available yet, so the right-hand panel can only show the total trace.")
    if reference and reference["negative_weight"] <= 1e-9:
        note_parts.append("No imaginary-mode DOS weight is visible below 0 THz.")
    else:
        note_parts.append("Imaginary-mode DOS weight remains below 0 THz.")

    series = []
    if total:
        series.append(
            {
                "label": "total",
                "color": "#2e8b57",
                "points": total["points"],
            }
        )
    elif projected:
        series.append(
            {
                "label": "total",
                "color": "#2e8b57",
                "points": projected["total_points"],
            }
        )

    return {
        "mode": "phonon_projected_dos" if projected else "phonon_total_dos",
        "status": "plot_ready",
        "title": "Projected phonon density of states" if projected else "Phonon density of states",
        "note": " ".join(note_parts),
        "series": series,
        "projected_series": projected["projected_series"] if projected else [],
        "x_range": (reference or {"frequency_range": [-1.0, 10.0]})["frequency_range"],
        "y_range": (reference or {"density_range": [0.0, 1.0]})["density_range"],
        "frequency_range": (reference or {"frequency_range": [-1.0, 10.0]})["frequency_range"],
        "density_range": (reference or {"density_range": [0.0, 1.0]})["density_range"],
        "mesh": mesh,
        "max_density": reference["max_density"] if reference else None,
        "frequency_reference": "Frequency (THz)",
        "density_reference": "states/THz",
        "artifacts": artifacts,
    }


def _dos_artifacts(run_dir: Path) -> list[str]:
    names = ["DOSCAR", "OUTCAR", "vasprun.xml"]
    return [name for name in names if _nonempty(run_dir / name)]


_PDOS_FILE_RE = re.compile(r"^PDOS_(?P<element>.+?)(?:_(?P<spin>UP|DW))?\.dat$")


def _pdos_file_parts(path: Path) -> tuple[str, str | None] | None:
    match = _PDOS_FILE_RE.match(path.name)
    if not match:
        return None
    element = str(match.group("element") or "").strip()
    spin = str(match.group("spin") or "").strip().lower() or None
    if not element:
        return None
    return element, spin


def _pdos_element_names(run_dir: Path, structure: dict[str, Any] | None = None) -> list[str]:
    discovered: list[str] = []
    for path in sorted(run_dir.glob("PDOS_*.dat"), key=lambda item: item.name):
        parsed = _pdos_file_parts(path)
        if not parsed:
            continue
        element, _ = parsed
        if element not in discovered:
            discovered.append(element)

    preferred = [str(symbol) for symbol in (structure or {}).get("species", []) if str(symbol)]
    ordered: list[str] = []
    for symbol in preferred + discovered:
        if symbol in discovered and symbol not in ordered:
            ordered.append(symbol)
    return ordered


def _pdos_orbital_group(label: str) -> str | None:
    cleaned = str(label or "").strip().lower()
    if not cleaned:
        return None
    if cleaned == "tot":
        return "tot"
    if cleaned.startswith("s"):
        return "s"
    if cleaned.startswith("p"):
        return "p"
    if cleaned.startswith("d"):
        return "d"
    if cleaned.startswith("f"):
        return "f"
    return None


def _pdos_orbital_color(orbital: str) -> str:
    palette = {
        "s": "#2e8b57",
        "p": "#d64541",
        "d": "#4367c7",
        "f": "#b8872f",
    }
    return palette.get(str(orbital or "").lower(), "#7c6f64")


def _sum_dos_points(
    primary: list[list[float]] | None,
    secondary: list[list[float]] | None,
) -> list[list[float]]:
    left = primary or []
    right = secondary or []
    if not left:
        return [[float(pair[0]), float(pair[1])] for pair in right]
    if not right:
        return [[float(pair[0]), float(pair[1])] for pair in left]
    length = min(len(left), len(right))
    merged: list[list[float]] = []
    for index in range(length):
        merged.append(
            [
                float(left[index][0]),
                float(left[index][1]) + float(right[index][1]),
            ]
        )
    return merged


def _shift_points(points: list[list[float]], delta: float) -> list[list[float]]:
    if abs(delta) < 1e-9:
        return [[float(pair[0]), float(pair[1])] for pair in points]
    return [[float(pair[0]) + delta, float(pair[1])] for pair in points]


def _filter_points_in_window(points: list[list[float]], x_range: list[float]) -> list[list[float]]:
    if not points or len(x_range) < 2:
        return [[float(pair[0]), float(pair[1])] for pair in points]
    low = float(x_range[0])
    high = float(x_range[1])
    return [
        [float(pair[0]), float(pair[1])]
        for pair in points
        if low <= float(pair[0]) <= high
    ]


def _pdos_axis_limit(value: float) -> float:
    if value <= 1e-9:
        return 1.0
    return max(value * 1.08, 0.5)


def _parse_vaspkit_pdos(path: Path) -> dict[str, Any] | None:
    if not _nonempty(path):
        return None

    header: list[str] | None = None
    rows: list[list[float]] = []
    try:
        for line in path.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                tokens = stripped.lstrip("#").split()
                if tokens and tokens[0].lower().startswith("energy"):
                    header = tokens
                continue
            if header is None:
                continue
            parts = stripped.split()
            if len(parts) < len(header):
                return None
            try:
                rows.append([float(piece) for piece in parts[: len(header)]])
            except ValueError:
                return None
    except OSError:
        return None

    if header is None or not rows:
        return None

    orbital_columns: list[tuple[int, str]] = []
    total_column: int | None = None
    for index, label in enumerate(header[1:], start=1):
        group = _pdos_orbital_group(label)
        if group == "tot":
            total_column = index
            continue
        if group is None:
            continue
        orbital_columns.append((index, group))

    if not orbital_columns and total_column is None:
        return None

    orbitals: dict[str, list[list[float]]] = {}
    total_points: list[list[float]] = []
    for values in rows:
        energy = float(values[0])
        grouped: dict[str, float] = {}
        for index, group in orbital_columns:
            grouped[group] = grouped.get(group, 0.0) + max(float(values[index]), 0.0)
        for group, density in grouped.items():
            orbitals.setdefault(group, []).append([energy, density])
        if total_column is not None:
            total_points.append([energy, max(float(values[total_column]), 0.0)])

    if not orbitals and not total_points:
        return None

    return {
        "orbitals": orbitals,
        "total_points": total_points,
    }


def _total_dos_trace(parsed: dict[str, Any]) -> list[list[float]]:
    series = list(parsed.get("series") or [])
    if not series:
        return []
    if not parsed.get("spin_polarized"):
        return [[float(pair[0]), max(float(pair[1]), 0.0)] for pair in series[0].get("points", [])]

    up_points = series[0].get("points", [])
    down_points = series[1].get("points", []) if len(series) > 1 else []
    total: list[list[float]] = []
    for index, up_pair in enumerate(up_points):
        up_density = max(float(up_pair[1]), 0.0)
        down_density = abs(float(down_points[index][1])) if index < len(down_points) else 0.0
        total.append([float(up_pair[0]), up_density + down_density])
    return total


def _projected_dos_window(parsed_total: dict[str, Any]) -> list[float]:
    x_range = list(parsed_total.get("x_range") or [-6.0, 6.0])
    if len(x_range) < 2:
        return [-6.0, 6.0]
    low = float(x_range[0])
    high = float(x_range[1])
    if high - low <= 16.0:
        return [low, high]
    focused_low = max(low, -8.0)
    focused_high = min(high, 8.0)
    if focused_high - focused_low < 6.0:
        return [low, high]
    return [focused_low, focused_high]


def _build_projected_dos_panels(
    system_dir: Path,
    run_dir: Path,
    parsed_total: dict[str, Any],
    *,
    x_shift: float = 0.0,
) -> dict[str, Any] | None:
    structure = parse_poscar_file(system_dir / "POSCAR") or {}
    ordered_elements = _pdos_element_names(run_dir, structure)
    if not ordered_elements:
        return None

    focused_x_range = _projected_dos_window(parsed_total)
    files_by_element: dict[str, dict[str, Path]] = {}
    spin_collapsed = False
    for path in sorted(run_dir.glob("PDOS_*.dat"), key=lambda item: item.name):
        parsed = _pdos_file_parts(path)
        if not parsed:
            continue
        element, spin = parsed
        files_by_element.setdefault(element, {})[spin or "total"] = path
        if spin is not None:
            spin_collapsed = True

    panels: list[dict[str, Any]] = []
    projected_elements: list[str] = []
    orbital_order = ["s", "p", "d", "f"]

    for element in ordered_elements:
        grouped_series: dict[str, list[list[float]]] = {}
        for path in files_by_element.get(element, {}).values():
            parsed = _parse_vaspkit_pdos(path)
            if not parsed:
                continue
            for orbital, points in (parsed.get("orbitals") or {}).items():
                shifted = _filter_points_in_window(_shift_points(points, x_shift), focused_x_range)
                grouped_series[orbital] = _sum_dos_points(grouped_series.get(orbital), shifted)

        if not grouped_series:
            continue

        extra_orbitals = sorted(orbital for orbital in grouped_series if orbital not in orbital_order)
        raw_maxima = {
            orbital: (max(float(pair[1]) for pair in points) if points else 0.0)
            for orbital, points in grouped_series.items()
        }
        panel_peak = max(raw_maxima.values()) if raw_maxima else 0.0
        orbital_cutoff = max(panel_peak * 0.03, 0.02)
        series_payload: list[dict[str, Any]] = []
        panel_max = 0.0
        for orbital in orbital_order + extra_orbitals:
            points = grouped_series.get(orbital) or []
            if not points:
                continue
            max_density = raw_maxima.get(orbital, 0.0)
            if max_density < orbital_cutoff:
                continue
            panel_max = max(panel_max, max_density)
            series_payload.append(
                {
                    "label": f"{element}-{orbital}",
                    "orbital": orbital,
                    "color": _pdos_orbital_color(orbital),
                    "points": points,
                }
            )

        if not series_payload:
            continue

        projected_elements.append(element)
        panels.append(
            {
                "kind": "element",
                "title": element,
                "series": series_payload,
                "y_range": [0.0, _pdos_axis_limit(panel_max)],
                "max_density": panel_max,
            }
        )

    total_points = _filter_points_in_window(_total_dos_trace(parsed_total), focused_x_range)
    if total_points:
        total_max = max(float(pair[1]) for pair in total_points) if total_points else 0.0
        panels.append(
            {
                "kind": "total",
                "title": "Total",
                "series": [
                    {
                        "label": "Total",
                        "orbital": "total",
                        "color": "#2f2f2f",
                        "points": total_points,
                    }
                ],
                "y_range": [0.0, _pdos_axis_limit(total_max)],
                "max_density": total_max,
            }
        )

    if not panels:
        return None

    return {
        "panel_layout": "stacked_pdos",
        "panels": panels,
        "projected_elements": projected_elements,
        "spin_collapsed": spin_collapsed,
        "x_range": focused_x_range,
        "focused_window": focused_x_range,
    }


def dos_visualization(
    system_dir: Path,
    *,
    source_step: str | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any] | None:
    source_step, run_dir = _dos_source_run(system_dir, preferred_step=source_step, preferred_run_dir=run_dir)
    if source_step is None or run_dir is None:
        return None

    parsed = _parse_total_doscar(run_dir / "DOSCAR")
    if not parsed:
        return {
            "mode": "total_dos",
            "status": "artifact_ready",
            "title": "DOS artifacts",
            "note": f"DOSCAR is present in {run_dir.relative_to(system_dir)}, but a plot-ready total DOS trace could not be reconstructed automatically.",
            "series": [],
            "x_range": [-6.0, 6.0],
            "y_range": [0.0, 1.0],
            "fermi_eV": _parse_fermi_reference(run_dir),
            "dos_at_fermi": None,
            "energy_reference": "E - E_F (eV)",
            "density_reference": "states/eV",
            "source_step": source_step,
            "source_run_dir": run_dir.relative_to(system_dir).as_posix(),
            "mirrored_spin": False,
            "artifacts": _dos_artifacts(run_dir),
        }

    note_parts = []
    if source_step == "dos":
        note_parts.append(f"Total DOS reconstructed from {run_dir.relative_to(system_dir)}/DOSCAR.")
    else:
        note_parts.append(f"Dedicated DOS run is not available yet, so this preview falls back to {run_dir.relative_to(system_dir)}/DOSCAR.")
    if parsed["mirrored_spin"]:
        note_parts.append("Spin-down is mirrored below zero for readability.")

    reference_fermi, reference_origin = _comparison_fermi_reference(system_dir, run_dir)
    local_fermi = parsed.get("fermi_eV")
    if (
        reference_fermi is not None
        and isinstance(local_fermi, (int, float))
        and abs(float(local_fermi) - float(reference_fermi)) > 1e-6
    ):
        shift_delta = float(local_fermi) - float(reference_fermi)
        parsed = dict(parsed)
        parsed["series"] = _shift_series_axis(list(parsed["series"]), "x", shift_delta)
        parsed["x_range"] = [float(parsed["x_range"][0]) + shift_delta, float(parsed["x_range"][1]) + shift_delta]
        parsed["raw_fermi_eV"] = local_fermi
        parsed["fermi_eV"] = float(reference_fermi)
        parsed["dos_at_fermi"] = _dos_density_at_zero(parsed["series"])
        note_parts.append(f"Energy zero aligned to the {reference_origin} Fermi level for cross-step comparison.")

    shift_delta = 0.0
    if "raw_fermi_eV" in parsed and isinstance(parsed.get("raw_fermi_eV"), (int, float)) and isinstance(parsed.get("fermi_eV"), (int, float)):
        shift_delta = float(parsed["raw_fermi_eV"]) - float(parsed["fermi_eV"])
    projected = _build_projected_dos_panels(system_dir, run_dir, parsed, x_shift=shift_delta)
    if projected:
        note_parts.append("Element panels combine orbital-resolved PDOS traces generated from VASPKIT.")
        if projected.get("focused_window"):
            note_parts.append("The stacked PDOS preview focuses on the energy window near the Fermi level.")
        if projected.get("spin_collapsed"):
            note_parts.append("Spin-resolved PDOS files were summed into compact per-element orbital panels.")

    return {
        "mode": "projected_dos" if projected else "total_dos",
        "status": "plot_ready",
        "title": "Projected density of states" if projected else "Total density of states",
        "note": " ".join(note_parts),
        "energy_reference": "E - E_F (eV)",
        "density_reference": "states/eV",
        "source_step": source_step,
        "source_run_dir": run_dir.relative_to(system_dir).as_posix(),
        "artifacts": _dos_artifacts(run_dir),
        **parsed,
        **(projected or {}),
    }


def _pdos_preview_paths(system_dir: Path, run_dir: Path | None) -> list[str]:
    if run_dir is None or not run_dir.exists():
        return []
    paths: list[str] = []
    for pattern in ("PDOS_*.dat", "IPDOS_*.dat"):
        for path in sorted(run_dir.glob(pattern), key=lambda item: item.name):
            paths.append(path.relative_to(system_dir).as_posix())
    log_path = run_dir / "vaspkit_pdos.log"
    if _nonempty(log_path):
        paths.append(log_path.relative_to(system_dir).as_posix())
    return paths


def dos_artifacts_payload(
    system_dir: Path,
    *,
    source_step: str | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    source_step, run_dir = _dos_source_run(system_dir, preferred_step=source_step, preferred_run_dir=run_dir)
    if source_step is None or run_dir is None:
        return {
            "status": "review",
            "source_step": None,
            "source_run_dir": None,
            "can_generate_pdos": False,
            "pdos_files": [],
            "ipdos_files": [],
            "preview_paths": [],
            "preview_path": None,
            "elements": [],
            "log_path": None,
            "note": "Complete SCF or DOS first so a DOSCAR is available for element-resolved PDOS post-processing.",
        }

    structure = parse_poscar_file(system_dir / "POSCAR")
    preview_paths = _pdos_preview_paths(system_dir, run_dir)
    pdos_files = [path for path in preview_paths if Path(path).name.startswith("PDOS_")]
    ipdos_files = [path for path in preview_paths if Path(path).name.startswith("IPDOS_")]
    elements = _pdos_element_names(run_dir, structure)
    source_status = step_status(system_dir, source_step)
    source_active = source_status in ACTIVE_JOB_STATES
    vaspkit_ready = bool(settings.vaspkit_cmd and settings.vaspkit_cmd.exists())
    can_generate = bool(_nonempty(run_dir / "DOSCAR")) and not source_active and vaspkit_ready

    if pdos_files:
        note = (
            f"Element-resolved PDOS is ready in {run_dir.relative_to(system_dir)} "
            f"for {', '.join(elements)}."
        )
        status = "ready"
    elif source_active:
        note = f"{source_step.upper()} is still {source_status}. Wait for it to finish before generating PDOS."
        status = "review"
    elif not vaspkit_ready:
        location = settings.vaspkit_cmd or "the configured path"
        note = f"VASPKIT is not available at {location}, so PDOS cannot be generated from the current DOSCAR."
        status = "blocked"
    else:
        note = (
            f"DOSCAR is ready in {run_dir.relative_to(system_dir)}. "
            "Generate element-resolved PDOS without rerunning VASP."
        )
        status = "review"

    return {
        "status": status,
        "source_step": source_step,
        "source_run_dir": run_dir.relative_to(system_dir).as_posix(),
        "can_generate_pdos": can_generate,
        "pdos_files": pdos_files,
        "ipdos_files": ipdos_files,
        "preview_paths": preview_paths,
        "preview_path": pdos_files[0] if pdos_files else (preview_paths[0] if preview_paths else None),
        "elements": elements,
        "log_path": (run_dir / "vaspkit_pdos.log").relative_to(system_dir).as_posix() if (run_dir / "vaspkit_pdos.log").exists() else None,
        "note": note,
    }


def generate_element_pdos(system_dir: Path) -> dict[str, Any]:
    payload = dos_artifacts_payload(system_dir)
    source_step = payload.get("source_step")
    source_run_dir = payload.get("source_run_dir")
    if not source_step or not source_run_dir:
        raise HTTPException(status_code=400, detail="PDOS requires a completed SCF or DOS run with DOSCAR.")
    if not payload.get("can_generate_pdos"):
        raise HTTPException(status_code=400, detail=str(payload.get("note") or "PDOS generation is not available right now."))

    active_jobs = refresh_job_states()
    if any(
        job.get("system") == system_dir.name
        and job.get("step") == source_step
        and str(job.get("state") or "") in _step_in_progress_states()
        for job in active_jobs
    ):
        raise HTTPException(status_code=400, detail=f"{source_step.upper()} is still active. Wait for it to finish before generating PDOS.")

    run_dir = system_dir / source_run_dir
    log_path = run_dir / "vaspkit_pdos.log"
    try:
        with log_path.open("w", encoding="utf-8") as handle:
            if not settings.vaspkit_cmd:
                raise HTTPException(status_code=400, detail="VASPKIT is not configured for this installation.")
            completed = subprocess.run(
                [str(settings.vaspkit_cmd)],
                cwd=str(run_dir),
                input="11\n113\n",
                text=True,
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=300,
            )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=500, detail=f"VASPKIT PDOS generation timed out after {exc.timeout} seconds.") from exc

    refreshed = dos_artifacts_payload(system_dir)
    if refreshed.get("pdos_files"):
        return {
            **refreshed,
            "generated_at": now_iso(),
            "source_step": source_step,
        }

    log_hint = read_text_tail(log_path, 2000).strip()
    if completed.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"VASPKIT PDOS generation failed for {system_dir.name}. {log_hint or 'See runs/*/vaspkit_pdos.log for details.'}",
        )
    raise HTTPException(
        status_code=500,
        detail=f"VASPKIT finished but no PDOS_*.dat files were produced for {system_dir.name}. {log_hint or ''}".strip(),
    )


def _structure_atom_count(path: Path) -> int | None:
    structure = parse_poscar_file(path)
    if not structure:
        return None
    counts = structure.get("counts") or []
    return sum(int(value) for value in counts)


def primitive_cell_payload(system_dir: Path) -> dict[str, Any]:
    relax_path = relax_contcar_path(system_dir)
    primitive_path = relax_primitive_path(system_dir)
    log_path = relax_primitive_log_path(system_dir)
    symmetry_path = relax_primitive_symmetry_path(system_dir)
    relax_ready = step_status(system_dir, "relax") == "completed" and _nonempty(relax_path)
    vaspkit_ready = bool(settings.vaspkit_cmd and settings.vaspkit_cmd.exists())
    primitive_ready = primitive_cell_available(system_dir)
    downstream_path = downstream_structure_path(system_dir)

    if primitive_ready:
        status = "ready"
        note = (
            "Primitive cell is ready for review. Create a dedicated primitive child project "
            "if you want downstream steps to follow this structure."
        )
    elif not relax_ready:
        status = "review"
        note = "Complete relax first so runs/relax/CONTCAR is available for primitive-cell conversion."
    elif not vaspkit_ready:
        status = "blocked"
        location = settings.vaspkit_cmd or "the configured path"
        note = f"VASPKIT is not available at {location}, so primitive-cell conversion cannot run."
    else:
        status = "review"
        note = "Relax is complete. Generate a primitive cell from runs/relax/CONTCAR when you want to inspect it or adopt it into a dedicated child project."

    return {
        "status": status,
        "can_generate": relax_ready and vaspkit_ready,
        "source_path": _relative_system_path(system_dir, relax_path) if _nonempty(relax_path) else None,
        "primitive_path": _relative_system_path(system_dir, primitive_path) if primitive_ready else None,
        "symmetry_path": _relative_system_path(system_dir, symmetry_path) if _nonempty(symmetry_path) else None,
        "log_path": _relative_system_path(system_dir, log_path) if _nonempty(log_path) else None,
        "downstream_structure_path": _relative_system_path(system_dir, downstream_path) if _nonempty(downstream_path) else None,
        "source_atoms": _structure_atom_count(relax_path) if _nonempty(relax_path) else None,
        "primitive_atoms": _structure_atom_count(primitive_path) if primitive_ready else None,
        "note": note,
    }


def regenerate_downstream_inputs_for_primitive(system_dir: Path) -> tuple[list[str], list[str]]:
    generated: list[str] = []
    errors: list[str] = []
    for relative_path in PRIMITIVE_REGENERATED_INPUTS:
        try:
            payload = generate_input_content(system_dir, relative_path)
            content = payload["content"]
            atomic_write_text(system_dir / relative_path, content)
            record_generated_file(system_dir, relative_path, content)
            generated.append(relative_path)
        except Exception as exc:
            errors.append(f"{relative_path}: {exc}")
    return generated, errors


def generate_primitive_cell(system_dir: Path) -> dict[str, Any]:
    payload = primitive_cell_payload(system_dir)
    if not payload.get("can_generate"):
        raise HTTPException(status_code=400, detail=str(payload.get("note") or "Primitive-cell generation is not available right now."))

    active_jobs = refresh_job_states()
    if any(
        job.get("system") == system_dir.name
        and job.get("step") == "relax"
        and str(job.get("state") or "") in _step_in_progress_states()
        for job in active_jobs
    ):
        raise HTTPException(status_code=400, detail="Relax is still active. Wait for it to finish before generating the primitive cell.")

    run_dir = system_dir / "runs" / "relax"
    log_path = relax_primitive_log_path(system_dir)
    primitive_path = relax_primitive_path(system_dir)
    symmetry_path = relax_primitive_symmetry_path(system_dir)
    for path in (primitive_path, symmetry_path):
        if path.exists():
            path.unlink()

    try:
        with log_path.open("w", encoding="utf-8") as handle:
            if not settings.vaspkit_cmd:
                raise HTTPException(status_code=400, detail="VASPKIT is not configured for this installation.")
            completed = subprocess.run(
                [str(settings.vaspkit_cmd), "-task", "602", "-file", "CONTCAR"],
                cwd=str(run_dir),
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=300,
            )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=500, detail=f"VASPKIT primitive-cell generation timed out after {exc.timeout} seconds.") from exc

    refreshed = primitive_cell_payload(system_dir)
    if refreshed.get("primitive_path"):
        return {
            **refreshed,
            "generated_at": now_iso(),
            "regenerated_files": [],
            "regeneration_errors": [],
        }

    log_hint = read_text_tail(log_path, 2000).strip()
    if completed.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"VASPKIT primitive-cell generation failed for {system_dir.name}. {log_hint or 'See runs/relax/vaspkit_primitive.log for details.'}",
        )
    raise HTTPException(
        status_code=500,
        detail=f"VASPKIT finished but did not produce PRIMCELL.vasp for {system_dir.name}. {log_hint or ''}".strip(),
    )


def _submission_managed_inputs(system_dir: Path, step: str) -> list[str]:
    normalized = str(step or "").strip().lower()
    if normalized == "relax":
        return ["INCAR.relax", _relative_system_path(system_dir, resolved_mesh_kpoints_path(system_dir, "relax"))]
    if normalized in {"scf", "converge", "elastic", "charge"}:
        return [
            f"INCAR.{normalized}",
            _relative_system_path(system_dir, resolved_mesh_kpoints_path(system_dir, normalized)),
        ]
    if normalized == "dos":
        return ["INCAR.dos", "KPOINTS.dos"]
    if normalized == "band":
        return ["INCAR.band", "KPATH.in", "KPOINTS.band"]
    if normalized == "phonon":
        return ["INCAR.phonon", "KPOINTS.phonon", "band.conf"]
    return []


def _prepare_step_inputs_for_submission(system_dir: Path, step: str, *, apply_changes: bool) -> dict[str, Any]:
    managed_inputs = _submission_managed_inputs(system_dir, step)
    refresh = refresh_generated_inputs_if_needed(system_dir, managed_inputs, apply_changes=apply_changes)
    warnings: list[str] = []
    blockers: list[str] = []

    if refresh["regenerated"]:
        warnings.append(f"Auto-regenerated {step} inputs before submission: {', '.join(refresh['regenerated'])}.")

    for relative_path, decision in refresh["decisions"].items():
        state = decision.get("state") or {}
        reason = str(decision.get("reason") or "")
        if reason == AUTO_REGEN_REASON_MANUAL_OVERRIDE and state.get("stale"):
            if step == "band" and relative_path == "KPOINTS.band":
                blockers.append(
                    "KPOINTS.band was edited after generation and its source inputs changed. "
                    "Regenerate it or update the saved custom band path before submitting band."
                )
            elif step == "band" and relative_path == "KPATH.in":
                warnings.append(
                    "KPATH.in was edited after generation and its source inputs changed. "
                    "Band will follow KPOINTS.band, so regenerate KPATH.in as well if you want previews to stay aligned."
                )
            else:
                warnings.append(
                    f"{relative_path} was edited after generation and its source inputs changed. "
                    f"{step.capitalize()} will use the current file as-is; regenerate it only if you want template defaults."
                )
        elif reason == AUTO_REGEN_REASON_UNTRACKED:
            if step == "band" and relative_path == "KPOINTS.band":
                blockers.append(
                    "KPOINTS.band predates freshness tracking. Regenerate it once before submitting band "
                    "so the line path matches the current structure/path settings."
                )
            elif step == "band" and relative_path == "KPATH.in":
                warnings.append(
                    "KPATH.in predates freshness tracking. Regenerate it once if you want the preview path "
                    "to match the current band-input baseline."
                )
            else:
                warnings.append(
                    f"{relative_path} predates freshness tracking. {step.capitalize()} will use the current file as-is; "
                    "auto-regenerate it only if you want template defaults."
                )
        elif reason == AUTO_REGEN_REASON_CUSTOM_BAND_PATH and step == "band" and relative_path == "KPATH.in":
            warnings.append("Band is using the saved custom band path. Review KPATH.in if you need the preview path to match that custom definition.")

    for error in refresh["errors"]:
        blockers.append(f"{step.capitalize()} input auto-regeneration failed: {error}")

    return {
        "warnings": warnings,
        "blocking_errors": blockers,
        "regenerated": refresh["regenerated"],
        "decisions": refresh["decisions"],
    }


def _prepare_band_inputs_for_submission(system_dir: Path) -> dict[str, Any]:
    return _prepare_step_inputs_for_submission(system_dir, "band", apply_changes=True)


def _apply_relax_completion_refresh(job: dict[str, Any], previous_state: str) -> bool:
    if str(job.get("step") or "").strip().lower() != "relax":
        return False
    if str(job.get("state") or "").strip().lower() != "finished":
        return False
    if str(previous_state or "").strip().lower() == "finished":
        return False

    system = str(job.get("system") or "").strip()
    if not system:
        return False
    system_dir = SYSTEMS_DIR / system
    if not relax_contcar_path(system_dir).exists():
        return False

    refresh = refresh_generated_inputs_if_needed(system_dir, DOWNSTREAM_GENERATED_FILE_CANDIDATES)
    interesting_decisions = {
        relative_path: {
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "tracked": bool((decision.get("state") or {}).get("tracked")),
            "manual_override": bool((decision.get("state") or {}).get("manual_override")),
            "stale": bool((decision.get("state") or {}).get("stale")),
        }
        for relative_path, decision in refresh["decisions"].items()
        if decision.get("reason") != "fresh"
    }
    if not refresh["regenerated"] and not refresh["errors"] and not interesting_decisions:
        return False

    job["post_relax_refresh"] = {
        "updated_at": now_iso(),
        "regenerated": refresh["regenerated"],
        "errors": refresh["errors"],
        "decisions": interesting_decisions,
    }
    return True


def _origin_safe_label(label: str, fallback_index: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", (label or "").strip().lower()).strip("_")
    return cleaned or f"band_{fallback_index}"


def _write_origin_band_exports(system_dir: Path) -> tuple[Path, Path]:
    payload = band_visualization(system_dir)
    if not payload or not payload.get("series"):
        raise HTTPException(status_code=400, detail="No plot-ready band dataset is available for export.")

    run_dir = system_dir / "runs" / "band"
    export_csv = run_dir / "origin_band_export.csv"
    tick_csv = run_dir / "origin_band_ticks.csv"

    series = payload["series"]
    points_count = len(series[0]["points"])
    if any(len(item["points"]) != points_count for item in series):
        raise HTTPException(status_code=500, detail="Band series lengths are inconsistent and cannot be exported.")

    header = ["k_path"]
    for index, item in enumerate(series, start=1):
        header.append(_origin_safe_label(str(item.get("label") or ""), index))

    lines = [",".join(header)]
    previous_x: float | None = None
    for row_index in range(points_count):
        current_x = float(series[0]["points"][row_index][0])
        # Insert a blank separator row whenever the k-path restarts or repeats at a
        # high-symmetry boundary. Origin will then break the polyline instead of
        # drawing a false vertical jump between two path segments.
        if previous_x is not None and current_x <= previous_x + 1e-9:
            lines.append(",".join([""] * len(header)))
        row = [f"{float(series[0]['points'][row_index][0]):.6f}"]
        for item in series:
            row.append(f"{float(item['points'][row_index][1]):.6f}")
        lines.append(",".join(row))
        previous_x = current_x
    export_csv.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tick_lines = ["label,x"]
    for item in payload.get("display_x_ticks") or payload.get("x_ticks", []):
        tick_lines.append(f"{item['label']},{float(item['x']):.6f}")
    tick_csv.write_text("\n".join(tick_lines) + "\n", encoding="utf-8")
    return export_csv, tick_csv


def _parse_elastic_tensor_file(path: Path) -> list[list[float]] | None:
    if not path.exists():
        return None
    rows: list[list[float]] = []
    for line in path.read_text(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) != 6:
            continue
        try:
            row = [float(value) for value in parts]
        except ValueError:
            continue
        rows.append(row)
        if len(rows) == 6:
            return rows
    return None


def _parse_outcar_elastic_tensor(path: Path) -> list[list[float]] | None:
    if not path.exists():
        return None
    rows: dict[str, list[float]] = {}
    lines = path.read_text(errors="ignore").splitlines()
    for idx, line in enumerate(lines):
        if "TOTAL ELASTIC MODULI (kBar)" not in line:
            continue
        for subline in lines[idx + 1 : idx + 15]:
            parts = subline.split()
            if len(parts) < 7 or parts[0] not in {"XX", "YY", "ZZ", "XY", "YZ", "ZX"}:
                continue
            try:
                rows[parts[0]] = [float(value) / 10.0 for value in parts[1:7]]
            except ValueError:
                continue
    if len(rows) != 6:
        return None
    return [rows["XX"], rows["YY"], rows["ZZ"], rows["XY"], rows["YZ"], rows["ZX"]]


def _parse_vaspkit_elastic_summary(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    text = path.read_text(errors="ignore")
    lines = text.splitlines()
    tensor: list[list[float]] = []
    for idx, line in enumerate(lines):
        if "Stiffness Tensor C_ij (in GPa):" not in line:
            continue
        for subline in lines[idx + 1 : idx + 12]:
            parts = subline.split()
            if len(parts) != 6:
                if tensor:
                    break
                continue
            try:
                tensor.append([float(value) for value in parts])
            except ValueError:
                tensor.clear()
                break
            if len(tensor) == 6:
                break
        if len(tensor) == 6:
            break

    summary = _elastic_metrics_from_tensor(tensor, "VASPKIT log") if len(tensor) == 6 else None
    parsed_any = summary is not None
    if summary is None:
        summary = {"tensor_source": "VASPKIT log"}

    table_patterns = {
        "Bulk Modulus B (GPa)": ("bulk_modulus_voigt_gpa", "bulk_modulus_reuss_gpa", "bulk_modulus_gpa"),
        "Young's Modulus E (GPa)": ("youngs_modulus_voigt_gpa", "youngs_modulus_reuss_gpa", "youngs_modulus_gpa"),
        "Shear Modulus G (GPa)": ("shear_modulus_voigt_gpa", "shear_modulus_reuss_gpa", "shear_modulus_gpa"),
        "Poisson's Ratio v": ("poisson_ratio_voigt", "poisson_ratio_reuss", "poisson_ratio"),
        "P-wave Modulus (GPa)": ("p_wave_modulus_voigt_gpa", "p_wave_modulus_reuss_gpa", "p_wave_modulus_gpa"),
        "Pugh's Ratio (B/G)": ("pugh_ratio_voigt", "pugh_ratio_reuss", "pugh_ratio"),
    }
    hardness_counter = 0
    for line in lines:
        match = re.match(
            r"\s*\|\s*(.*?)\s*\|\s*([-0-9.]+)\s*\|\s*([-0-9.]+)\s*\|\s*([-0-9.]+)\s*\|",
            line,
        )
        if not match:
            continue
        label = match.group(1).strip()
        values = tuple(float(match.group(index)) for index in (2, 3, 4))
        if label in table_patterns:
            keys = table_patterns[label]
            summary.update({keys[0]: values[0], keys[1]: values[1], keys[2]: values[2]})
            parsed_any = True
            continue
        if label.startswith("Vickers Hardness (GPa)"):
            hardness_counter += 1
            if hardness_counter == 1:
                summary.update(
                    {
                        "hardness_chen_voigt_gpa": values[0],
                        "hardness_chen_reuss_gpa": values[1],
                        "hardness_chen_gpa": values[2],
                    }
                )
            elif hardness_counter == 2:
                summary.update(
                    {
                        "hardness_alt_voigt_gpa": values[0],
                        "hardness_alt_reuss_gpa": values[1],
                        "hardness_alt_gpa": values[2],
                    }
                )
            parsed_any = True

    scalar_patterns = {
        "cauchy_pressure_gpa": r"Cauchy Pressure Pc \(GPa\):\s*([-0-9.]+)",
        "kleinman_parameter": r"Kleinman[’']s parameter:\s*([-0-9.]+)",
        "universal_anisotropy": r"Universal Elastic Anisotropy:\s*([-0-9.]+)",
        "chung_buessem_anisotropy": r"Chung-Buessem Anisotropy:\s*([-0-9.]+)",
        "isotropic_poisson_ratio": r"Isotropic Poisson[’']s Ratio:\s*([-0-9.]+)",
        "longitudinal_wave_velocity_m_s": r"Longitudinal wave velocity \(m/s\):\s*([-0-9.]+)",
        "transverse_wave_velocity_m_s": r"Transverse wave velocity \(m/s\):\s*([-0-9.]+)",
        "average_wave_velocity_m_s": r"Average wave velocity \(m/s\):\s*([-0-9.]+)",
        "debye_temperature_k": r"Debye temperature \(K\):\s*([-0-9.]+)",
    }
    for key, pattern in scalar_patterns.items():
        match = re.search(pattern, text)
        if not match:
            continue
        summary[key] = float(match.group(1))
        parsed_any = True

    if "poisson_ratio" not in summary and isinstance(summary.get("isotropic_poisson_ratio"), (int, float)):
        summary["poisson_ratio"] = float(summary["isotropic_poisson_ratio"])
    if "hardness_chen_gpa" not in summary and isinstance(summary.get("hardness_alt_gpa"), (int, float)):
        summary["hardness_chen_gpa"] = float(summary["hardness_alt_gpa"])

    return summary if parsed_any else None


def _invert_matrix(matrix: list[list[float]]) -> list[list[float]] | None:
    size = len(matrix)
    augmented = [
        [float(value) for value in row] + [1.0 if row_index == col_index else 0.0 for col_index in range(size)]
        for row_index, row in enumerate(matrix)
    ]
    for pivot_index in range(size):
        pivot_row = max(range(pivot_index, size), key=lambda row_index: abs(augmented[row_index][pivot_index]))
        pivot = augmented[pivot_row][pivot_index]
        if abs(pivot) < 1e-12:
            return None
        if pivot_row != pivot_index:
            augmented[pivot_index], augmented[pivot_row] = augmented[pivot_row], augmented[pivot_index]
        pivot = augmented[pivot_index][pivot_index]
        augmented[pivot_index] = [value / pivot for value in augmented[pivot_index]]
        for row_index in range(size):
            if row_index == pivot_index:
                continue
            factor = augmented[row_index][pivot_index]
            if abs(factor) < 1e-12:
                continue
            augmented[row_index] = [
                current - factor * pivot_value
                for current, pivot_value in zip(augmented[row_index], augmented[pivot_index])
            ]
    return [row[size:] for row in augmented]


def _values_are_close(values: list[float], *, rel_tol: float = 0.03, abs_tol: float = 0.5) -> bool:
    if not values:
        return False
    reference = float(values[0])
    return all(math.isclose(float(value), reference, rel_tol=rel_tol, abs_tol=abs_tol) for value in values[1:])


def _is_cubic_like_tensor(tensor: list[list[float]]) -> bool:
    if len(tensor) != 6 or any(len(row) != 6 for row in tensor):
        return False
    diagonal = [tensor[0][0], tensor[1][1], tensor[2][2]]
    cross = [tensor[0][1], tensor[0][2], tensor[1][2]]
    shear = [tensor[3][3], tensor[4][4], tensor[5][5]]
    off_block = [
        tensor[0][3], tensor[0][4], tensor[0][5],
        tensor[1][3], tensor[1][4], tensor[1][5],
        tensor[2][3], tensor[2][4], tensor[2][5],
        tensor[3][4], tensor[3][5], tensor[4][5],
    ]
    return (
        _values_are_close(diagonal)
        and _values_are_close(cross)
        and _values_are_close(shear)
        and all(abs(float(value)) < 0.5 for value in off_block)
    )


def _elastic_metrics_from_tensor(tensor: list[list[float]], source_label: str) -> dict[str, Any] | None:
    if len(tensor) != 6 or any(len(row) != 6 for row in tensor):
        return None
    compliance = _invert_matrix(tensor)
    if compliance is None:
        return None

    c11, c22, c33 = tensor[0][0], tensor[1][1], tensor[2][2]
    c12, c13, c23 = tensor[0][1], tensor[0][2], tensor[1][2]
    c44, c55, c66 = tensor[3][3], tensor[4][4], tensor[5][5]
    s11, s22, s33 = compliance[0][0], compliance[1][1], compliance[2][2]
    s12, s13, s23 = compliance[0][1], compliance[0][2], compliance[1][2]
    s44, s55, s66 = compliance[3][3], compliance[4][4], compliance[5][5]

    bulk_voigt = (c11 + c22 + c33 + 2.0 * (c12 + c13 + c23)) / 9.0
    shear_voigt = (c11 + c22 + c33 - c12 - c13 - c23 + 3.0 * (c44 + c55 + c66)) / 15.0

    bulk_reuss_denominator = s11 + s22 + s33 + 2.0 * (s12 + s13 + s23)
    shear_reuss_denominator = 4.0 * (s11 + s22 + s33) - 4.0 * (s12 + s13 + s23) + 3.0 * (s44 + s55 + s66)
    if abs(bulk_reuss_denominator) < 1e-12 or abs(shear_reuss_denominator) < 1e-12:
        return None
    bulk_reuss = 1.0 / bulk_reuss_denominator
    shear_reuss = 15.0 / shear_reuss_denominator

    bulk_hill = (bulk_voigt + bulk_reuss) / 2.0
    shear_hill = (shear_voigt + shear_reuss) / 2.0
    if abs(3.0 * bulk_hill + shear_hill) < 1e-12 or abs(shear_hill) < 1e-12:
        return None

    youngs_modulus = 9.0 * bulk_hill * shear_hill / (3.0 * bulk_hill + shear_hill)
    poisson_ratio = (3.0 * bulk_hill - 2.0 * shear_hill) / (2.0 * (3.0 * bulk_hill + shear_hill))
    pugh_ratio = bulk_hill / shear_hill
    p_wave_modulus = bulk_hill + (4.0 * shear_hill / 3.0)
    universal_anisotropy = 5.0 * (shear_voigt / shear_reuss) + (bulk_voigt / bulk_reuss) - 6.0

    hardness_chen = None
    if bulk_hill > 0.0 and shear_hill > 0.0:
        k_ratio = shear_hill / bulk_hill
        hardness_chen = max(0.0, 2.0 * ((k_ratio * k_ratio * shear_hill) ** 0.585) - 3.0)

    summary: dict[str, Any] = {
        "tensor_source": source_label,
        "tensor_gpa": tensor,
        "compliance_gpa_inv": compliance,
        "C11": c11,
        "C12": c12,
        "C44": c44,
        "bulk_modulus_voigt_gpa": bulk_voigt,
        "bulk_modulus_reuss_gpa": bulk_reuss,
        "bulk_modulus_gpa": bulk_hill,
        "shear_modulus_voigt_gpa": shear_voigt,
        "shear_modulus_reuss_gpa": shear_reuss,
        "shear_modulus_gpa": shear_hill,
        "youngs_modulus_gpa": youngs_modulus,
        "poisson_ratio": poisson_ratio,
        "pugh_ratio": pugh_ratio,
        "p_wave_modulus_gpa": p_wave_modulus,
        "universal_anisotropy": universal_anisotropy,
    }
    if hardness_chen is not None:
        summary["hardness_chen_gpa"] = hardness_chen

    if _is_cubic_like_tensor(tensor):
        cubic_c11 = sum((c11, c22, c33)) / 3.0
        cubic_c12 = sum((c12, c13, c23)) / 3.0
        cubic_c44 = sum((c44, c55, c66)) / 3.0
        denominator = cubic_c11 - cubic_c12
        summary.update(
            {
                "cubic_like": True,
                "C11": cubic_c11,
                "C12": cubic_c12,
                "C44": cubic_c44,
                "cauchy_pressure_gpa": cubic_c12 - cubic_c44,
                "tetragonal_shear_gpa": denominator / 2.0,
            }
        )
        if abs(denominator) >= 1e-12:
            summary["zener_anisotropy"] = 2.0 * cubic_c44 / denominator
    else:
        summary["cubic_like"] = False

    return summary


def _parse_elastic_summary(path: Path) -> dict[str, Any] | None:
    vaspkit_log_path = path.with_name("vaspkit_elastic.log")
    vaspkit_summary = _parse_vaspkit_elastic_summary(vaspkit_log_path)
    if vaspkit_summary is not None:
        return vaspkit_summary
    tensor_path = path.with_name("ELASTIC_TENSOR")
    tensor = _parse_elastic_tensor_file(tensor_path)
    source = "ELASTIC_TENSOR" if tensor else None
    if tensor is None:
        tensor = _parse_outcar_elastic_tensor(path)
        if tensor is not None:
            source = "OUTCAR"
    if tensor is None or source is None:
        return None
    return _elastic_metrics_from_tensor(tensor, source)


def _parse_phonon_summary(path: Path) -> dict[str, Any] | None:
    parsed = _parse_phonon_band_yaml(path)
    if parsed is None:
        return None
    return {
        "min_frequency": float(parsed["min_frequency"]),
        "negative_count": int(parsed["negative_count"]),
    }


def result_highlights(
    system_dir: Path,
    summary: dict[str, Any],
    steps: dict[str, str],
    dos_artifacts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    highlights: list[dict[str, Any]] = []
    relax_dir = system_dir / "runs" / "relax"
    dos_dir = system_dir / "runs" / "dos"
    band_dir = system_dir / "runs" / "band"
    elastic_dir = system_dir / "runs" / "elastic"
    charge_dir = system_dir / "runs" / "charge"
    phonon_dir = system_dir / "runs" / "phonon"

    if steps.get("relax") == "completed":
        lattice_a = summary.get("a_display_A")
        lattice_label = summary.get("a_display_label") or "Lattice a"
        lattice_note = summary.get("a_display_note")
        primitive_a = summary.get("primitive_a_A")
        density = summary.get("density_g_cm3")
        note_parts: list[str] = []
        if isinstance(primitive_a, (int, float)) and isinstance(lattice_a, (int, float)):
            if abs(float(primitive_a) - float(lattice_a)) > 1e-3:
                note_parts.append(f"primitive |a1| = {primitive_a:.3f} A")
        if lattice_note:
            note_parts.append(str(lattice_note))
        if isinstance(density, (int, float)):
            note_parts.append(f"density = {density:.3f} g/cm^3")
        highlights.append(
            {
                "title": "Relaxed Structure",
                "status": "ready",
                "value": f"{lattice_label} = {lattice_a:.3f} A" if isinstance(lattice_a, (int, float)) else "CONTCAR ready",
                "note": " | ".join(note_parts) if note_parts else "Relaxation finished",
            }
        )
        primitive_payload = primitive_cell_payload(system_dir)
        if primitive_payload.get("primitive_path"):
            source_atoms = primitive_payload.get("source_atoms")
            primitive_atoms = primitive_payload.get("primitive_atoms")
            atom_note = ""
            if isinstance(source_atoms, int) and isinstance(primitive_atoms, int):
                atom_note = f"{source_atoms} -> {primitive_atoms} atoms"
            highlights.append(
                {
                    "title": "Primitive Cell",
                    "status": "ready",
                    "value": atom_note or "PRIMCELL.vasp ready",
                    "note": "Generated from the relaxed CONTCAR. Review it here, then create a dedicated primitive child project if you want a separate downstream workflow.",
                }
            )

    band_info = _parse_band_summary(band_dir)
    if band_info:
        if band_info["status"] == "gap":
            highlights.append(
                {
                    "title": "Electronic Character",
                    "status": "ready" if band_info["gap_eV"] > 0.01 else "review",
                    "value": f"gap = {band_info['gap_eV']:.4f} eV",
                    "note": band_info["character"],
                }
            )
        elif band_info["mode"] == "hybrid":
            highlights.append(
                {
                    "title": "Hybrid Band",
                    "status": "ready",
                    "value": "PROCAR_OPT ready",
                    "note": "Hybrid KPOINTS_OPT result is available. Review PROCAR_OPT/OUTCAR for the refined band dispersion.",
                }
            )
        else:
            highlights.append(
                {
                    "title": "Band Result",
                    "status": "ready",
                    "value": "EIGENVAL ready",
                    "note": "Band run completed, but no BAND_GAP summary file was generated.",
                }
            )

    if steps.get("dos") == "completed":
        highlights.append(
            {
                "title": "Density of States",
                "status": "ready",
                "value": "DOSCAR ready",
                "note": "Static DOS run completed with DOSCAR available for total/projected DOS review.",
            }
        )

    dos_payload = dos_artifacts or dos_artifacts_payload(system_dir)
    if dos_payload.get("pdos_files"):
        elements = dos_payload.get("elements") or []
        element_note = ", ".join(elements) if elements else f"{len(dos_payload['pdos_files'])} element files"
        highlights.append(
            {
                "title": "Projected Density of States",
                "status": "ready",
                "value": f"{len(dos_payload['pdos_files'])} PDOS files",
                "note": f"Element-resolved density of states is ready from {dos_payload.get('source_step')} for {element_note}.",
            }
        )

    elastic = _parse_elastic_summary(elastic_dir / "OUTCAR")
    if elastic:
        highlights.append(
            {
                "title": "Elastic Tensor",
                "status": "ready",
                "value": f"C11 {elastic['C11']:.2f} / C12 {elastic['C12']:.2f} / C44 {elastic['C44']:.2f} GPa",
                "note": f"Full 6x6 stiffness tensor loaded from {elastic['tensor_source']} for VRH post-processing.",
            }
        )
        highlights.append(
            {
                "title": "Bulk Modulus",
                "status": "ready",
                "value": f"B = {elastic['bulk_modulus_gpa']:.2f} GPa",
                "note": f"Voigt {elastic['bulk_modulus_voigt_gpa']:.2f} / Reuss {elastic['bulk_modulus_reuss_gpa']:.2f} / Hill {elastic['bulk_modulus_gpa']:.2f}",
            }
        )
        highlights.append(
            {
                "title": "Shear Modulus",
                "status": "ready",
                "value": f"G = {elastic['shear_modulus_gpa']:.2f} GPa",
                "note": f"Voigt {elastic['shear_modulus_voigt_gpa']:.2f} / Reuss {elastic['shear_modulus_reuss_gpa']:.2f} / Hill {elastic['shear_modulus_gpa']:.2f}",
            }
        )
        highlights.append(
            {
                "title": "Young's Modulus",
                "status": "ready",
                "value": f"E = {elastic['youngs_modulus_gpa']:.2f} GPa",
                "note": "Hill-average isotropic Young's modulus from the elastic tensor.",
            }
        )
        highlights.append(
            {
                "title": "Pugh Ratio",
                "status": "ready",
                "value": f"B/G = {elastic['pugh_ratio']:.3f}",
                "note": "Polycrystalline Hill-average modulus ratio. Use as a ductility heuristic, not a hard classifier.",
            }
        )
        highlights.append(
            {
                "title": "Poisson Ratio",
                "status": "ready",
                "value": f"nu = {elastic['poisson_ratio']:.3f}",
                "note": "Isotropic Poisson ratio derived from the Hill bulk and shear moduli.",
            }
        )
        if isinstance(elastic.get("debye_temperature_k"), (int, float)):
            highlights.append(
                {
                    "title": "Debye Temperature",
                    "status": "ready",
                    "value": f"Theta_D = {float(elastic['debye_temperature_k']):.1f} K",
                    "note": "Reported directly from VASPKIT elastic post-processing when the Debye summary is available.",
                }
            )
        hardness = elastic.get("hardness_chen_gpa")
        if isinstance(hardness, (int, float)):
            highlights.append(
                {
                    "title": "Hardness",
                    "status": "review",
                    "value": f"Hv ~= {hardness:.2f} GPa",
                    "note": "Chen empirical hardness estimate from the bulk and shear moduli. Treat as a heuristic, not a direct measurement.",
                }
            )
        if isinstance(elastic.get("cauchy_pressure_gpa"), (int, float)):
            highlights.append(
                {
                    "title": "Cauchy Pressure",
                    "status": "ready",
                    "value": f"{float(elastic['cauchy_pressure_gpa']):.2f} GPa",
                    "note": "Cubic-only C12 - C44 indicator. Positive and negative values are often used as a qualitative bonding heuristic.",
                }
            )
        anisotropy_note_parts = [f"A^U = {elastic['universal_anisotropy']:.3f}"]
        if isinstance(elastic.get("zener_anisotropy"), (int, float)):
            anisotropy_note_parts.append(f"Zener A = {float(elastic['zener_anisotropy']):.3f}")
        highlights.append(
            {
                "title": "Elastic Anisotropy",
                "status": "ready",
                "value": " | ".join(anisotropy_note_parts),
                "note": "Universal anisotropy is available for any 3D elastic tensor; Zener anisotropy is reported only for cubic-like tensors.",
            }
        )

    if (charge_dir / "ACF.dat").exists():
        highlights.append(
            {
                "title": "Bader Charge",
                "status": "ready",
                "value": "ACF.dat available",
                "note": "Reference-charge Bader post-processing completed",
            }
        )

    phonon = _parse_phonon_summary(phonon_dir / "band.yaml")
    if phonon:
        phonon_ready = phonon["negative_count"] == 0
        highlights.append(
            {
                "title": "Phonon Stability",
                "status": "ready" if phonon_ready else "risk",
                "value": f"min freq = {phonon['min_frequency']:.3f}",
                "note": "No imaginary mode" if phonon_ready else f"{phonon['negative_count']} negative frequencies detected",
            }
        )

    formation = summary.get("formation_enthalpy_eV_per_atom")
    if isinstance(formation, (int, float)):
        highlights.append(
            {
                "title": "Formation Enthalpy",
                "status": "ready",
                "value": f"{formation:.4f} eV/atom",
                "note": "Atom-reference chain available",
            }
        )

    if not highlights:
        highlights.append(
            {
                "title": "Results",
                "status": "review",
                "value": "No completed properties yet",
                "note": "Run relax or scf to populate the first result cards",
            }
        )
    return highlights


def system_audit(system_dir: Path, summary: dict[str, Any], steps: dict[str, str]) -> dict[str, Any]:
    metadata = read_json(system_dir / "metadata.json", {})
    structure = parse_poscar_file(system_dir / "POSCAR")
    species = structure.get("species", []) if structure else []
    counts = structure.get("counts", []) if structure else []
    total_atoms = sum(counts)
    spin_polarized, magmom = _effective_spin_metadata(system_dir, metadata)

    items: list[dict[str, str]] = []

    def add(title: str, status: str, note: str) -> None:
        items.append({"title": title, "status": status, "note": note})

    add(
        "Structure input",
        "pass" if structure else "fail",
        f"{total_atoms} atoms detected in POSCAR." if structure else "POSCAR is missing or cannot be parsed.",
    )

    required_inputs = [
        "POTCAR",
        _relative_system_path(system_dir, resolved_mesh_kpoints_path(system_dir, "relax")),
        "KPOINTS.scf",
        "INCAR.relax",
        "INCAR.scf",
    ]
    missing_inputs = [name for name in required_inputs if not (system_dir / name).exists()]
    add(
        "Core inputs",
        "pass" if not missing_inputs else "fail",
        "POTCAR, relax mesh, SCF mesh, relax INCAR, and scf INCAR are present." if not missing_inputs else f"Missing: {', '.join(missing_inputs)}",
    )

    electronic_type = str(metadata.get("electronic_type") or "auto")
    add(
        "Electronic type",
        "review" if electronic_type == "auto" else "pass",
        "Electronic type is still auto; confirm ISMEAR and SIGMA before production." if electronic_type == "auto" else f"Electronic type fixed to {electronic_type}.",
    )

    magmom_nonzero = bool(magmom and any(abs(float(value)) > 1e-9 for value in magmom))
    if any(element in TRANSITION_METALS for element in species):
        if not spin_polarized:
            add("Magnetic setup", "review", "Transition metals are present, but spin polarization is disabled.")
        elif not magmom_nonzero:
            add("Magnetic setup", "review", "Spin is enabled, but MAGMOM is still effectively zero.")
        else:
            add("Magnetic setup", "pass", "Spin and non-zero MAGMOM are configured for the current transition-metal system.")
    elif spin_polarized and not magmom_nonzero:
        add("Magnetic setup", "review", "Spin polarization is enabled, but MAGMOM is still effectively zero.")
    else:
        add("Magnetic setup", "pass", "No transition-metal-specific magnetic warning is triggered.")

    downstream_done = any(steps.get(name) == "completed" for name in ("scf", "dos", "band", "elastic", "charge", "phonon"))
    if steps.get("relax") != "completed":
        add(
            "Relax baseline",
            "fail" if downstream_done else "review",
            "Relax is not completed, but downstream properties already exist." if downstream_done else "Relax has not been completed yet. This is normal for a fresh project, but not for production properties.",
        )
    elif steps.get("scf") != "completed":
        add("SCF baseline", "review", "Relax is ready, but SCF is not completed yet.")
    else:
        add("Production baseline", "pass", "Relax and SCF are both completed.")

    add(
        "Convergence evidence",
        "pass" if steps.get("converge") == "completed" else "review",
        "Convergence workflow completed." if steps.get("converge") == "completed" else "Convergence workflow has not been completed yet.",
    )

    material_class = str(metadata.get("material_class") or "bulk").lower()
    if material_class in {"2d", "slab"}:
        add(
            "Elastic workflow",
            "review",
            "Built-in elastic stage is bulk-oriented. For 2D/slab systems with vacuum, prefer a custom in-plane strain workflow instead of the default 3D elastic tensor path.",
        )

    phonon_info = _parse_phonon_summary(system_dir / "runs" / "phonon" / "band.yaml")
    phonon_lreal = (_incar_value(system_dir / "INCAR.phonon", "LREAL") or "").upper()
    if phonon_info:
        if phonon_info["negative_count"] > 0:
            add("Phonon stability", "fail", f"Current phonon result still shows {phonon_info['negative_count']} negative frequencies.")
        else:
            add("Phonon stability", "pass", "No imaginary modes detected in the current phonon result.")
    else:
        add("Phonon policy", "review" if phonon_lreal and ".FALSE." not in phonon_lreal else "pass",
            "INCAR.phonon uses LREAL != .FALSE.; this is lighter but less robust for paper-grade forces." if phonon_lreal and ".FALSE." not in phonon_lreal else "Phonon-ready force policy is acceptable or phonon has not been run yet.")

    formation = summary.get("formation_enthalpy_eV_per_atom")
    add(
        "Thermodynamic chain",
        "pass" if isinstance(formation, (int, float)) else "review",
        "Formation enthalpy is available." if isinstance(formation, (int, float)) else "Formation enthalpy is still missing isolated-atom reference data.",
    )

    counts_by_status = {
        status: sum(1 for item in items if item["status"] == status)
        for status in ("pass", "review", "fail")
    }
    overall = "fail" if counts_by_status["fail"] else "review" if counts_by_status["review"] else "pass"
    return {"overall": overall, "counts": counts_by_status, "items": items}


def report_snapshot(system_dir: Path, summary: dict[str, Any], steps: dict[str, str]) -> dict[str, Any]:
    completed = sum(1 for status in steps.values() if status == "completed")
    total = len(steps)
    dos_ready = step_completed(system_dir, "dos")
    band = _parse_band_summary(system_dir / "runs" / "band")
    elastic = _parse_elastic_summary(system_dir / "runs" / "elastic" / "OUTCAR")
    phonon = _parse_phonon_summary(system_dir / "runs" / "phonon" / "band.yaml")

    metrics: list[dict[str, Any]] = [
        {
            "label": "Workflow completion",
            "value": f"{completed}/{total}",
            "fraction": completed / total if total else 0.0,
            "tone": "ready" if completed == total else "review",
        }
    ]

    if isinstance(summary.get("magnetic_moment_muB"), (int, float)):
        moment = abs(float(summary["magnetic_moment_muB"]))
        metrics.append(
            {
                "label": "Magnetic moment",
                "value": f"{moment:.3f} uB",
                "fraction": min(moment / 5.0, 1.0),
                "tone": "review" if moment < 0.1 else "ready",
            }
        )

    if band:
        if band["status"] == "gap":
            gap = float(band["gap_eV"])
            metrics.append(
                {
                    "label": "Band gap",
                    "value": f"{gap:.4f} eV",
                    "fraction": min(gap / 2.0, 1.0),
                    "tone": "review" if gap <= 0.01 else "ready",
                }
            )
        elif band["mode"] == "hybrid":
            metrics.append(
                {
                    "label": "Hybrid band",
                    "value": "ready",
                    "fraction": 1.0,
                    "tone": "ready",
                }
            )
        else:
            metrics.append(
                {
                    "label": "Band result",
                    "value": "ready",
                    "fraction": 1.0,
                    "tone": "ready",
                }
            )

    if dos_ready:
        metrics.append(
            {
                "label": "DOS",
                "value": "ready",
                "fraction": 1.0,
                "tone": "ready",
            }
        )

    if elastic:
        metrics.append(
            {
                "label": "Bulk modulus",
                "value": f"{float(elastic['bulk_modulus_gpa']):.2f} GPa",
                "fraction": min(float(elastic["bulk_modulus_gpa"]) / 250.0, 1.0),
                "tone": "ready",
            }
        )
        metrics.append(
            {
                "label": "Pugh ratio",
                "value": f"{float(elastic['pugh_ratio']):.3f}",
                "fraction": min(float(elastic["pugh_ratio"]) / 2.5, 1.0),
                "tone": "review" if float(elastic["pugh_ratio"]) < 1.75 else "ready",
            }
        )

    if phonon:
        negative = int(phonon["negative_count"])
        metrics.append(
            {
                "label": "Phonon check",
                "value": "stable" if negative == 0 else f"{negative} neg",
                "fraction": 1.0 if negative == 0 else max(0.08, 1.0 - min(negative, 100) / 100.0),
                "tone": "ready" if negative == 0 else "risk",
            }
        )

    if isinstance(summary.get("hydrogen_wt_percent"), (int, float)):
        hwt = float(summary["hydrogen_wt_percent"])
        metrics.append(
            {
                "label": "H wt%",
                "value": f"{hwt:.3f} %",
                "fraction": min(hwt / 10.0, 1.0),
                "tone": "ready",
            }
        )

    return {
        "workflow_completed": completed,
        "workflow_total": total,
        "workflow_fraction": completed / total if total else 0.0,
        "metrics": metrics,
    }


def system_integrity_report(system_dir: Path) -> dict[str, Any]:
    metadata = read_json(system_dir / "metadata.json", {})
    structure = parse_poscar_file(system_dir / "POSCAR")
    species = structure.get("species", []) if structure else []
    composition_formula = _composition_formula_from_structure(structure)
    lineage = load_system_lineage(system_dir)
    lineage_present = lineage_file_exists(system_dir)
    findings: list[dict[str, Any]] = []

    potcar_path = system_dir / "POTCAR"
    potcar_title_values = potcar_titles(potcar_path) if potcar_path.exists() else []
    potcar_species = _potcar_base_species_titles(potcar_title_values)
    if species and potcar_species and potcar_species != species:
        findings.append(
            _make_integrity_finding(
                "identity_mismatch",
                "fail",
                f"POTCAR species order {', '.join(potcar_species)} does not match POSCAR species order {', '.join(species)}.",
                path="POTCAR",
            )
        )

    raw_mapping = metadata.get("potcar_mapping")
    if species and isinstance(raw_mapping, dict):
        mapping_keys = [str(key) for key in raw_mapping.keys()]
        if set(mapping_keys) != set(species):
            findings.append(
                _make_integrity_finding(
                    "identity_mismatch",
                    "fail",
                    f"metadata.json potcar_mapping keys ({', '.join(sorted(mapping_keys))}) do not match POSCAR species ({', '.join(species)}).",
                    path="metadata.json",
                )
            )

    band_conf_path = system_dir / "band.conf"
    if band_conf_path.exists():
        band_conf_supercell = parse_band_conf_supercell(band_conf_path.read_text(errors="ignore"))
        metadata_supercell = metadata.get("phonon_supercell")
        if (
            band_conf_supercell is not None
            and isinstance(metadata_supercell, list)
            and len(metadata_supercell) == 3
            and [int(value) for value in metadata_supercell] != band_conf_supercell
        ):
            findings.append(
                _make_integrity_finding(
                    "managed_input_drift",
                    "fail",
                    f"band.conf DIM ({' '.join(str(value) for value in band_conf_supercell)}) does not match metadata phonon_supercell ({' '.join(str(int(value)) for value in metadata_supercell)}).",
                    path="band.conf",
                )
            )

    generation_states = file_generation_states(system_dir, list(MANAGED_FILE_CANDIDATES))
    drifted_paths = sorted(
        relative_path
        for relative_path, state in generation_states.items()
        if state.get("stale") or (not state.get("tracked") and (system_dir / relative_path).exists())
    )
    if drifted_paths:
        findings.append(
            _make_integrity_finding(
                "managed_input_drift",
                "review" if len(drifted_paths) <= 2 else "fail",
                f"Managed inputs need review: {', '.join(drifted_paths[:6])}{'...' if len(drifted_paths) > 6 else ''}.",
            )
        )

    for step in ("relax", "scf", "dos", "converge", "elastic", "band", "charge", "phonon"):
        if step_completed(system_dir, step):
            _ensure_run_manifest_completion(system_dir, step)
        manifest_path = system_dir / "runs" / step / "run_manifest.json"
        if not manifest_path.exists():
            continue
        manifest = read_json(manifest_path, {})
        manifest_state = str(manifest.get("state") or "").strip().lower()
        completed = step_completed(system_dir, step)
        if completed and manifest_state not in {"finished", "completed", "failed", "paused", "stopped"}:
            findings.append(
                _make_integrity_finding(
                    "manifest_state_drift",
                    "fail",
                    f"runs/{step}/run_manifest.json is still marked {manifest_state or '<unset>'}, but result artifacts are complete.",
                    path=f"runs/{step}/run_manifest.json",
                )
            )
        elif manifest_state in {"finished", "completed"} and not completed:
            findings.append(
                _make_integrity_finding(
                    "manifest_state_drift",
                    "review",
                    f"runs/{step}/run_manifest.json is marked finished, but the current completion markers are incomplete.",
                    path=f"runs/{step}/run_manifest.json",
                )
            )

    parent_system = str(lineage.get("parent_system") or "").strip()
    if parent_system:
        parent_dir = SYSTEMS_DIR / parent_system
        if not parent_dir.exists():
            findings.append(
                _make_integrity_finding(
                    "lineage_gap",
                    "fail",
                    f"Lineage parent {parent_system} does not exist.",
                    path=LINEAGE_FILENAME,
                    related_system=parent_system,
                )
            )
        else:
            structure_source = str(lineage.get("structure_source") or "").strip()
            if structure_source:
                source_path = parent_dir / structure_source
                if not source_path.exists():
                    findings.append(
                        _make_integrity_finding(
                            "lineage_gap",
                            "fail",
                            f"Lineage source {parent_system}/{structure_source} is missing.",
                            path=LINEAGE_FILENAME,
                            related_system=parent_system,
                        )
                    )
    elif not lineage_present:
        source_template = str(metadata.get("source_template") or "").strip()
        matched_parent = _matched_parent_structure_source(system_dir)
        suspicious_name_reason = _suspicious_untracked_child_reason(system_dir.name)
        if matched_parent is not None:
            findings.append(
                _make_integrity_finding(
                    "lineage_gap",
                    "fail",
                    f"POSCAR matches {matched_parent['parent_system']}/{matched_parent['structure_source']}, but no lineage record is present.",
                    path="POSCAR",
                    related_system=matched_parent["parent_system"],
                )
            )
        elif source_template and source_template != "__blank__":
            findings.append(
                _make_integrity_finding(
                    "lineage_gap",
                    "fail",
                    f"metadata.json still records source_template = {source_template}, but no lineage record is present.",
                    path="metadata.json",
                    related_system=source_template,
                )
            )
        elif suspicious_name_reason:
            findings.append(_make_integrity_finding("lineage_gap", "fail", suspicious_name_reason, path="POSCAR"))

    jobs_for_system = [job for job in load_jobs() if job.get("system") == system_dir.name]
    for job in jobs_for_system:
        if not is_terminal_job_state(job.get("state")) or _job_is_latest_for_step(job):
            continue
        archive_dir = job_result_archive_dir(job)
        if not _job_archive_has_files(archive_dir):
            findings.append(
                _make_integrity_finding(
                    "archive_gap",
                    "review",
                    f"Historical {job.get('step')} job {job.get('id')} has no archived result snapshot under {JOB_RESULT_ARCHIVE_DIRNAME}.",
                    related_system=system_dir.name,
                )
            )

    quarantined = bool(lineage.get("quarantined")) or any(
        item.get("kind") == "lineage_gap" and item.get("severity") == "fail"
        for item in findings
    )

    counts_by_kind: dict[str, int] = {}
    for item in findings:
        kind = str(item.get("kind") or "review")
        counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1

    return {
        "system": system_dir.name,
        "lineage": {**lineage, "quarantined": quarantined},
        "composition_formula": composition_formula,
        "quarantined": quarantined,
        "overall": _integrity_overall(findings),
        "findings": findings,
        "counts_by_kind": counts_by_kind,
        "blockers": [item["message"] for item in findings if item.get("severity") == "fail"],
    }


def integrity_audit_payload() -> dict[str, Any]:
    reports = [system_integrity_report(system_dir) for system_dir in project_dirs()]
    counts_by_kind: dict[str, int] = {}
    flattened: list[dict[str, Any]] = []
    for report in reports:
        for finding in report["findings"]:
            kind = str(finding.get("kind") or "review")
            counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1
            flattened.append({"system": report["system"], **finding})
    overall = _integrity_overall(flattened)
    return {
        "overall": overall,
        "counts_by_kind": counts_by_kind,
        "quarantined_systems": [report["system"] for report in reports if report["quarantined"]],
        "systems": [
            {
                "name": report["system"],
                "overall": report["overall"],
                "quarantined": report["quarantined"],
                "composition_formula": report["composition_formula"],
                "counts_by_kind": report["counts_by_kind"],
            }
            for report in reports
        ],
        "findings": flattened,
    }


def submission_risk_assessment(system_dir: Path, step: str, target: str, mpi_np: int) -> dict[str, Any]:
    metadata = read_json(system_dir / "metadata.json", {})
    structure_path = resolved_structure_path(system_dir, step)
    structure = parse_poscar_file(structure_path) or {"species": [], "counts": []}
    species = structure.get("species", [])
    counts = structure.get("counts", [])
    total_atoms = sum(counts)
    material_class = str(metadata.get("material_class") or "bulk").lower()
    xc_geometry = str(metadata.get("xc_geometry") or "PBE")
    xc_electronic = str(metadata.get("xc_electronic") or "PBE")
    spin_polarized, magmom = _effective_spin_metadata(system_dir, metadata, step)
    warnings: list[str] = []
    risk_level = "ok"

    def warn(message: str, level: str = "review") -> None:
        nonlocal risk_level
        if message not in warnings:
            warnings.append(message)
        order = {"ok": 0, "review": 1, "high": 2}
        if order[level] > order[risk_level]:
            risk_level = level

    if structure_path != system_dir / "POSCAR":
        warn(
            f"{step.upper()} will use {_relative_system_path(system_dir, structure_path)} as its structure input.",
            "ok",
        )

    if step in {"scf", "dos", "band", "elastic", "charge", "phonon"} and step_status(system_dir, "relax") != "completed":
        warn("Relax is not marked completed. Downstream properties may still be using an unrelaxed or stale structure.", "high")

    if step == "dos" and step_status(system_dir, "scf") != "completed":
        warn("DOS is usually expected to start from a completed SCF state.", "high")
    if step == "band" and step_status(system_dir, "scf") != "completed":
        warn("Band is usually expected to start from a completed SCF state.", "high")
    if step == "band" and material_class == "molecule":
        warn("Band-path workflows are intended for periodic crystals. For isolated molecules, relax + scf is usually the meaningful default path.", "high")
    if step == "elastic" and material_class in {"2d", "slab"}:
        warn(
            "Built-in elastic workflow is intended for 3D bulk cells. For 2D/slab systems with vacuum, the reported 3D elastic tensor depends on the chosen vacuum thickness and should be replaced by a custom in-plane strain workflow.",
            "high",
        )

    if step in {"dos", "band", "charge", "phonon"} and str(metadata.get("electronic_type") or "auto") == "auto":
        warn("Electronic type is still auto. Confirm ISMEAR and SIGMA before production runs.", "review")

    magmom_nonzero = bool(magmom and any(abs(float(value)) > 1e-9 for value in magmom))
    if any(element in TRANSITION_METALS for element in species):
        if not spin_polarized:
            warn("Transition metals are present, but spin-polarized workflow is disabled.", "review")
        if spin_polarized and not magmom_nonzero:
            warn("Spin-polarized workflow is enabled, but MAGMOM is still effectively zero.", "review")
    elif spin_polarized and not magmom_nonzero:
        warn("Spin-polarized workflow is enabled, but MAGMOM is still effectively zero.", "review")

    incar_path = system_dir / f"INCAR.{step}"
    encut = _incar_float(incar_path, "ENCUT")
    max_enmax = _max_potcar_enmax(system_dir / "POTCAR")
    if encut is not None and max_enmax is not None:
        recommended_encut = 1.3 * max_enmax
        if encut + 1e-6 < recommended_encut:
            level = "high" if encut + 1e-6 < max_enmax else "review"
            warn(
                f"ENCUT = {encut:.0f} eV is below the POTCAR-based guideline of about {recommended_encut:.0f} eV (1.3 x max ENMAX = {max_enmax:.0f} eV).",
                level,
            )

    if step == "charge":
        laechg = (_incar_value(system_dir / "INCAR.charge", "LAECHG") or "").upper()
        if ".TRUE." not in laechg and "TRUE" not in laechg:
            warn("INCAR.charge does not explicitly request LAECHG. Bader reference charges may be incomplete.", "review")

    local_resources = local_cpu_resources() if target == "local" else None

    if step == "phonon":
        supercell = effective_phonon_supercell(system_dir, metadata)
        phonon_kmesh = _triplet_from_metadata(metadata, "phonon_kmesh", [4, 4, 4])
        phonon_dos_kmesh = _triplet_from_metadata(metadata, "phonon_dos_kmesh", [max(1, value * 2 if value > 1 else 1) for value in phonon_kmesh])
        expanded_atoms = total_atoms * _product(supercell)
        if expanded_atoms >= 40:
            resource_note = f" {format_cpu_resource_note(local_resources)}" if local_resources else ""
            warn(f"Phonon supercell expands to {expanded_atoms} atoms. Check CPU, memory, and queue limits before local submission.{resource_note}", "high")
        if _product(phonon_kmesh) >= 64 and expanded_atoms >= 40:
            warn("Phonon mesh is dense for the current supercell size. Check memory before local submission.", "high")
        if _product(phonon_dos_kmesh) < _product(phonon_kmesh):
            warn("Phonon DOS q-mesh is coarser than the displacement calculation mesh. DOS features may look undersampled.", "review")
        lreal = (_incar_value(system_dir / "INCAR.phonon", "LREAL") or "").upper()
        if lreal and ".FALSE." not in lreal:
            warn("INCAR.phonon uses LREAL != .FALSE.; this is memory-friendly but can degrade phonon force accuracy.", "review")
        if not (system_dir / "band.conf").exists():
            warn("band.conf is missing, so phonon path/post-processing configuration is incomplete.", "high")
        force_policy = _phonon_force_policy(system_dir, metadata)
        for message in force_policy["warnings"]:
            warn(message, "review")
        for message in force_policy["blocking_errors"]:
            warn(message, "high")
    elif step in {"dos", "band", "charge", "elastic"}:
        consistency = _downstream_incar_consistency(system_dir, step)
        for message in consistency["warnings"]:
            warn(message, "review")
        for message in consistency["blocking_errors"]:
            warn(message, "high")

    if step in {"scf", "dos", "band"} and xc_electronic == "HSE06":
        warn("HSE06 is selected for this electronic-structure step. Expect a much slower and heavier run than PBE.", "high")
        if target == "local":
            warn(f"For local HSE06, avoid running multiple HSE jobs at once and choose MPI NP from measured scaling, CPU cores, and RAM. {format_cpu_resource_note(local_resources or local_cpu_resources())}", "review")
        if step == "band":
            warn("Hybrid HSE band will reuse the SCF mesh as KPOINTS and the generated line path as KPOINTS_OPT.", "review")

    if step in {"relax", "converge", "elastic", "charge", "phonon"} and xc_geometry == "PBEsol":
        warn("Geometry/property defaults are using PBEsol for this step.", "review")

    if target == "local":
        resources = local_resources or local_cpu_resources()
        available_cores = int(resources.get("available_cores") or 1)
        physical_cores = resources.get("physical_cores")
        available_gb = psutil.virtual_memory().available / 1024**3
        if mpi_np > available_cores:
            warn(
                f"MPI NP = {mpi_np} exceeds the {available_cores} CPU core(s) currently available to this app process. Reduce MPI NP or run on a larger machine to avoid oversubscription.",
                "high",
            )
        elif physical_cores and mpi_np > physical_cores and step in {"phonon", "elastic"}:
            warn(
                f"MPI NP = {mpi_np} exceeds the detected physical core count ({physical_cores}). For memory-sensitive {step} runs, monitor RAM and benchmark scaling before increasing MPI further.",
                "review",
            )
        if step in {"elastic", "phonon"} and available_gb < 4.0:
            warn(f"Only about {available_gb:.2f} GB RAM is currently free. This is risky for local {step} runs.", "high")

    return {"warnings": warnings, "risk_level": risk_level}


def submission_gate(system_dir: Path, step: str, target: str, mpi_np: int) -> dict[str, Any]:
    risk = submission_risk_assessment(system_dir, step, target, mpi_np)
    blockers: list[str] = []
    metadata = read_json(system_dir / "metadata.json", {})
    integrity = system_integrity_report(system_dir)
    material_class = str(metadata.get("material_class") or "bulk").lower()
    xc_electronic = str(metadata.get("xc_electronic") or "PBE")
    structure_path = resolved_structure_path(system_dir, step)
    structure = parse_poscar_file(structure_path)
    species = structure.get("species", []) if structure else []
    counts = structure.get("counts", []) if structure else []
    total_atoms = sum(counts)

    def block(message: str) -> None:
        if message not in blockers:
            blockers.append(message)

    if integrity["quarantined"]:
        lineage_blockers = [
            item["message"]
            for item in integrity["findings"]
            if item.get("kind") == "lineage_gap" and item.get("severity") == "fail"
        ]
        detail = lineage_blockers[0] if lineage_blockers else "Resolve lineage and provenance issues first."
        block(f"{system_dir.name} is quarantined. {detail}")

    if structure is None:
        block(f"Structure input is missing or cannot be parsed: {_relative_system_path(system_dir, structure_path)}.")

    required_by_step = {
        "relax": [system_dir / "POTCAR", resolved_mesh_kpoints_path(system_dir, "relax"), system_dir / "INCAR.relax"],
        "scf": [system_dir / "POTCAR", resolved_mesh_kpoints_path(system_dir, "scf"), system_dir / "INCAR.scf"],
        "dos": [system_dir / "POTCAR", system_dir / "KPOINTS.dos", system_dir / "INCAR.dos"],
        "converge": [system_dir / "POTCAR", resolved_mesh_kpoints_path(system_dir, "converge"), system_dir / "INCAR.converge"],
        "band": [system_dir / "POTCAR", system_dir / "KPATH.in", system_dir / "KPOINTS.band", system_dir / "INCAR.band"],
        "elastic": [system_dir / "POTCAR", resolved_mesh_kpoints_path(system_dir, "elastic"), system_dir / "INCAR.elastic"],
        "charge": [system_dir / "POTCAR", resolved_mesh_kpoints_path(system_dir, "charge"), system_dir / "INCAR.charge"],
        "phonon": [system_dir / "POTCAR", system_dir / "KPOINTS.phonon", system_dir / "INCAR.phonon", system_dir / "band.conf"],
    }
    missing = [
        _relative_system_path(system_dir, path)
        for path in required_by_step.get(step, [])
        if not path.exists()
    ]
    if missing:
        block(f"Missing required input files for {step}: {', '.join(missing)}.")

    current_potcar_titles = potcar_titles(system_dir / "POTCAR") if (system_dir / "POTCAR").exists() else []
    potcar_count = len(current_potcar_titles)
    if species and potcar_count and potcar_count != len(species):
        block(f"POTCAR species count ({potcar_count}) does not match POSCAR species count ({len(species)}).")
    elif species and current_potcar_titles:
        potcar_species = _potcar_base_species_titles(current_potcar_titles)
        if potcar_species != species:
            block(f"POTCAR species order ({', '.join(potcar_species)}) does not match POSCAR species order ({', '.join(species)}).")

    raw_mapping = metadata.get("potcar_mapping")
    if species and isinstance(raw_mapping, dict):
        mapping_keys = {str(key) for key in raw_mapping.keys()}
        if mapping_keys != set(species):
            block(f"metadata.json potcar_mapping keys ({', '.join(sorted(mapping_keys))}) do not match POSCAR species ({', '.join(species)}).")

    if step == "phonon" and (system_dir / "band.conf").exists():
        band_conf_supercell = parse_band_conf_supercell((system_dir / "band.conf").read_text(errors="ignore"))
        metadata_supercell = metadata.get("phonon_supercell")
        if (
            band_conf_supercell is not None
            and isinstance(metadata_supercell, list)
            and len(metadata_supercell) == 3
            and [int(value) for value in metadata_supercell] != band_conf_supercell
        ):
            block(
                "band.conf DIM does not match metadata phonon_supercell. Align them before phonon submission."
            )

    incar_path = system_dir / f"INCAR.{step}"
    if incar_path.exists() and total_atoms:
        ispin = (_incar_value(incar_path, "ISPIN") or "").strip()
        magmom_raw = _incar_value(incar_path, "MAGMOM")
        magmom_count = _magmom_entry_count(magmom_raw)
        expected_magmom_count = total_atoms
        if step == "phonon":
            supercell = effective_phonon_supercell(system_dir, metadata)
            expected_magmom_count = total_atoms * _product(supercell)
        if ispin == "2" and magmom_count not in {None, expected_magmom_count}:
            if step == "phonon":
                block(
                    f"{incar_path.name} has MAGMOM count {magmom_count}, but the phonon supercell needs {expected_magmom_count} atom values."
                )
            else:
                block(f"{incar_path.name} has MAGMOM count {magmom_count}, but POSCAR needs {expected_magmom_count} atom values.")
        if ispin == "1" and _magmom_has_nonzero(magmom_raw):
            block(f"{incar_path.name} sets ISPIN=1 but MAGMOM is non-zero. Enable spin polarization or zero MAGMOM first.")

    if step == "band":
        scf_ready = step_completed(system_dir, "scf")
        if not scf_ready:
            block("Band requires a completed SCF baseline in runs/scf.")
        if material_class == "molecule":
            block("Band-path workflow is disabled for molecule projects. Use relax + scf, or switch to Expert Override if you need custom post-processing instead.")
        if xc_electronic == "HSE06":
            mesh_kpoints_path = resolved_mesh_kpoints_path(system_dir, "band")
            if not mesh_kpoints_path.exists():
                block(f"HSE06 band requires the SCF mesh input {_relative_system_path(system_dir, mesh_kpoints_path)}.")
            if not (system_dir / "runs" / "scf" / "WAVECAR").exists():
                block("HSE06 band requires a completed SCF baseline with WAVECAR in runs/scf.")
            if not _outcar_is_hse(system_dir / "runs" / "scf" / "OUTCAR"):
                block("HSE06 band requires an HSE06 SCF baseline. Re-run SCF with Electronic Functional = HSE06 before submitting band.")

    if step == "dos" and step_status(system_dir, "scf") != "completed":
        block("DOS workflow requires a completed SCF baseline first.")

    if step in {"elastic", "phonon"} and step_status(system_dir, "relax") != "completed":
        block(f"{step} requires a completed relax baseline before submission.")

    if step == "elastic" and material_class in {"2d", "slab"}:
        block("Built-in elastic workflow is disabled for 2D/slab projects. Its 3D stress-strain tensor depends on the chosen vacuum thickness; use a custom in-plane 2D elastic workflow instead.")

    if step == "charge" and step_status(system_dir, "scf") != "completed":
        block("Charge workflow requires a completed SCF baseline first.")

    if structure and material_class in {"2d", "slab"}:
        if step == "phonon":
            kpoints_path = system_dir / "KPOINTS.phonon"
        elif step == "dos":
            kpoints_path = system_dir / "KPOINTS.dos"
        else:
            kpoints_path = _resolved_step_mesh_kpoints_input(system_dir, step) or (system_dir / "KPOINTS.scf")
        mesh = _kpoints_mesh(kpoints_path)
        c_length = _vector_length(structure["lattice"][2]) if len(structure.get("lattice", [])) == 3 else None
        if mesh and c_length and c_length >= 15.0 and mesh[2] > 1:
            block(f"{material_class} project has c = {c_length:.2f} A and k_z = {mesh[2]}. Use kz=1 for large-vacuum cells before submitting.")

    if step == "phonon":
        consistency = _downstream_incar_consistency(system_dir, step)
        for message in consistency["blocking_errors"]:
            block(message)
        risk["warnings"] = _merge_warnings(risk["warnings"], consistency["warnings"])
        if consistency["warnings"] and risk["risk_level"] == "ok":
            risk["risk_level"] = "review"
        force_policy = _phonon_force_policy(system_dir, metadata)
        for message in force_policy["blocking_errors"]:
            block(message)
        risk["warnings"] = _merge_warnings(risk["warnings"], force_policy["warnings"])
        if force_policy["warnings"] and risk["risk_level"] == "ok":
            risk["risk_level"] = "review"
        if _band_conf_requests_force_constants(system_dir / "band.conf") and not (
            (system_dir / "FORCE_CONSTANTS").exists() or (system_dir / "runs" / "phonon" / "FORCE_CONSTANTS").exists()
        ):
            block("band.conf requests FORCE_CONSTANTS, but no FORCE_CONSTANTS file is available. Remove that line or generate FORCE_CONSTANTS first.")
        if (system_dir / "runs" / "phonon" / "band.yaml").exists():
            phonon = _parse_phonon_summary(system_dir / "runs" / "phonon" / "band.yaml")
            if phonon and phonon["negative_count"] > 0:
                risk["warnings"] = _merge_warnings(
                    risk["warnings"],
                    [f"Current phonon result already has {phonon['negative_count']} negative frequencies. Recheck settings before rerunning."]
                )
                if risk["risk_level"] == "ok":
                    risk["risk_level"] = "review"
    elif step in {"dos", "band", "charge", "elastic"}:
        consistency = _downstream_incar_consistency(system_dir, step)
        for message in consistency["blocking_errors"]:
            block(message)
        risk["warnings"] = _merge_warnings(risk["warnings"], consistency["warnings"])
        if consistency["warnings"] and risk["risk_level"] == "ok":
            risk["risk_level"] = "review"

    upstream = _upstream_baseline_consistency(system_dir, step)
    for message in upstream["blocking_errors"]:
        block(message)
    risk["warnings"] = _merge_warnings(risk["warnings"], upstream["warnings"])
    if upstream["warnings"] and risk["risk_level"] == "ok":
        risk["risk_level"] = "review"

    active_jobs = refresh_job_states()
    if any(
        job.get("system") == system_dir.name and job.get("step") == step and job.get("state") in _step_in_progress_states()
        for job in active_jobs
    ):
        block(f"A {step} job for {system_dir.name} is already active.")

    readiness = "blocked" if blockers else "review" if risk["warnings"] else "ready"
    if blockers:
        risk["risk_level"] = "high"

    return {
        "warnings": risk["warnings"],
        "risk_level": risk["risk_level"],
        "blocking_errors": blockers,
        "readiness": readiness,
    }


def preview_file_path(system_dir: Path, relative_path: str) -> Path:
    normalized = _safe_ui_relative_path(relative_path)
    if not normalized:
        raise HTTPException(status_code=400, detail="Invalid path")
    if (
        normalized not in PREVIEW_FILE_CANDIDATES
        and not RUNTIME_PREVIEW_PATTERN.fullmatch(normalized)
        and not _job_result_preview_path_allowed(normalized)
    ):
        raise HTTPException(status_code=400, detail=f"File is not available in UI: {relative_path}")
    full_path = _resolved_path_within_root(system_dir / normalized, system_dir)
    if full_path is None:
        raise HTTPException(status_code=400, detail="Invalid path")
    return full_path


def editable_file_path(system_dir: Path, relative_path: str, expert_override: bool = False) -> Path:
    normalized = relative_path.strip().replace("\\", "/")
    writable_candidates = PREVIEW_FILE_CANDIDATES if expert_override else WRITABLE_FILE_CANDIDATES
    if normalized not in writable_candidates:
        raise HTTPException(status_code=400, detail=f"File is not writable in UI: {relative_path}")
    return preview_file_path(system_dir, normalized)


def aiida_recipe_payload(system_dir: Path) -> dict[str, Any]:
    poscar_path = system_dir / "POSCAR"
    potcar_path = system_dir / "POTCAR"
    species = poscar_species(poscar_path) if poscar_path.exists() else []
    titles = potcar_titles(potcar_path) if potcar_path.exists() else []
    mapping = {
        element: potential
        for element, potential in zip(species, titles)
    }
    backend = aiida_inventory(settings.aiida_profile_name)
    return {
        "system": system_dir.name,
        "suggested_workchains": {
            "relax": "vasp.relax",
            "scf": "vasp.vasp",
            "band": "vasp.bands",
            "converge": "vasp.converge",
        },
        "structure_available": poscar_path.exists(),
        "potcar_available": potcar_path.exists(),
        "species_order": species,
        "potcar_titles": titles,
        "suggested_potcar_mapping": mapping,
        "suggested_potcar_family": backend.get("potcar_families", [{}])[0].get("label") if backend.get("potcar_families") else None,
        "suggested_codes": [item["full_label"] for item in backend.get("codes", [])],
        "notes": [
            "Mapping is inferred from the current system POTCAR TITEL entries and POSCAR species order.",
            f"Use {settings.aiida_code_std_label} for general bulk calculations unless a gamma-only workflow is explicitly intended.",
        ],
    }


def system_payload(system_dir: Path) -> dict[str, Any]:
    name = system_dir.name
    summary = system_summary(system_dir)
    metadata = read_json(system_dir / "metadata.json", {})
    backend = aiida_inventory(settings.aiida_profile_name)
    steps = {step: step_status(system_dir, step) for step in ("relax", "scf", "dos", "converge", "elastic", "band", "charge", "phonon")}
    integrity = system_integrity_report(system_dir)
    input_entries = input_review_entries(system_dir)
    dos_payload = dos_artifacts_payload(system_dir)
    primitive_payload = primitive_cell_payload(system_dir)
    preview_names = preview_files(system_dir)
    for path in ("BORN", "runs/band/KPOINTS", "runs/band/KPOINTS_OPT", "runs/phonon/BORN", "runs/charge/BORN"):
        if (system_dir / path).exists() and path not in preview_names:
            preview_names.append(path)
    for path in dos_payload.get("preview_paths", []):
        if path not in preview_names:
            preview_names.append(path)
    for path in (
        primitive_payload.get("primitive_path"),
        primitive_payload.get("symmetry_path"),
        primitive_payload.get("log_path"),
    ):
        if path and path not in preview_names:
            preview_names.append(path)
    generation_states = file_generation_states(system_dir, preview_names)
    key_files = {
        "POSCAR": read_text_preview(system_dir / "POSCAR", 2000),
        "INCAR.relax": read_text_preview(system_dir / "INCAR.relax", 2000),
        "INCAR.scf": read_text_preview(system_dir / "INCAR.scf", 2000),
        "INCAR.dos": read_text_preview(system_dir / "INCAR.dos", 2000),
        "INCAR.converge": read_text_preview(system_dir / "INCAR.converge", 2000),
        "KPOINTS.relax": read_text_preview(system_dir / "KPOINTS.relax", 2000),
        "KPOINTS.scf": read_text_preview(system_dir / "KPOINTS.scf", 2000),
        "KPOINTS.downstream": read_text_preview(system_dir / "KPOINTS.downstream", 2000),
        "KPOINTS.dos": read_text_preview(system_dir / "KPOINTS.dos", 2000),
        "KPATH.in": read_text_preview(system_dir / "KPATH.in", 2000),
        "KPOINTS.band": read_text_preview(system_dir / "KPOINTS.band", 2000),
        "KPOINTS.phonon": read_text_preview(system_dir / "KPOINTS.phonon", 2000),
        "BORN": read_text_preview(system_dir / "BORN", 2000),
    }
    return {
        "name": name,
        "metadata": metadata,
        "lineage": integrity["lineage"],
        "integrity_summary": {
            "overall": integrity["overall"],
            "counts_by_kind": integrity["counts_by_kind"],
            "findings_count": len(integrity["findings"]),
            "blockers": integrity["blockers"],
        },
        "quarantined": integrity["quarantined"],
        "composition_formula": integrity["composition_formula"],
        "material_settings": material_form_payload(system_dir, backend),
        "summary": summary,
        "steps": steps,
        "result_highlights": result_highlights(system_dir, summary, steps, dos_payload),
        "band_visualization": band_visualization(system_dir),
        "dos_visualization": dos_visualization(system_dir),
        "phonon_visualization": phonon_visualization(system_dir),
        "phonon_dos_visualization": phonon_dos_visualization(system_dir),
        "phonon_nac": _phonon_nac_payload(system_dir),
        "dos_artifacts": dos_payload,
        "primitive_cell": primitive_payload,
        "system_audit": system_audit(system_dir, summary, steps),
        "report_snapshot": report_snapshot(system_dir, summary, steps),
        "files": key_files,
        "preview_files": preview_names,
        "writable_files": writable_files(system_dir),
        "generated_files": [name for name in GENERATED_FILE_CANDIDATES if (system_dir / name).exists()],
        "file_states": generation_states,
        "input_review": {
            "entries": input_entries,
            "combined_text": combined_input_review_text(input_entries),
        },
        "tree": file_tree(system_dir, depth=2),
        "structure": parse_poscar_file(system_dir / "POSCAR"),
    }


def _job_result_context_highlights(job: dict[str, Any], system_dir: Path, run_dir: Path) -> list[dict[str, Any]]:
    step = str(job.get("step") or "").strip()
    highlights: list[dict[str, Any]] = []

    if step == "band":
        band_info = _parse_band_summary(run_dir)
        if band_info:
            if band_info["status"] == "gap":
                highlights.append(
                    {
                        "title": "Electronic Character",
                        "status": "ready" if band_info["gap_eV"] > 0.01 else "review",
                        "value": f"gap = {band_info['gap_eV']:.4f} eV",
                        "note": band_info["character"],
                    }
                )
            elif band_info["mode"] == "hybrid":
                highlights.append(
                    {
                        "title": "Hybrid Band",
                        "status": "ready",
                        "value": "PROCAR_OPT ready",
                        "note": "Hybrid band artifacts are available for the selected historical job.",
                    }
                )
            else:
                highlights.append(
                    {
                        "title": "Band Result",
                        "status": "ready",
                        "value": "EIGENVAL ready",
                        "note": "Historical band artifacts are available for this job.",
                    }
                )
        return highlights

    if step in {"dos", "scf"}:
        if _nonempty(run_dir / "DOSCAR"):
            highlights.append(
                {
                    "title": "Density of States",
                    "status": "ready",
                    "value": "DOSCAR ready",
                    "note": "Historical DOS artifacts are available for the selected job.",
                }
            )
        dos_payload = dos_artifacts_payload(system_dir, source_step=step, run_dir=run_dir)
        if dos_payload.get("pdos_files"):
            elements = dos_payload.get("elements") or []
            highlights.append(
                {
                    "title": "Projected Density of States",
                    "status": "ready",
                    "value": f"{len(dos_payload['pdos_files'])} PDOS files",
                    "note": f"Historical element-resolved DOS is ready for {', '.join(elements) if elements else 'the selected job'}.",
                }
            )
        return highlights

    if step == "phonon":
        phonon = _parse_phonon_summary(run_dir / "band.yaml")
        if phonon:
            highlights.append(
                {
                    "title": "Phonon Stability",
                    "status": "ready" if phonon["negative_count"] == 0 else "risk",
                    "value": f"min freq = {phonon['min_frequency']:.3f}",
                    "note": "No imaginary mode" if phonon["negative_count"] == 0 else f"{phonon['negative_count']} negative frequencies detected",
                }
            )
        return highlights

    return highlights


def _job_result_tree_entries(
    job: dict[str, Any],
    system_dir: Path,
    run_dir: Path,
    *,
    source_kind: str | None,
) -> list[dict[str, Any]]:
    source_root = run_dir.relative_to(system_dir).as_posix()
    entries: list[dict[str, Any]] = []
    for item in file_tree(run_dir, depth=2):
        entries.append(
            {
                **item,
                "path": f"{source_root}/{item['path']}",
                "historical_job_id": job["id"],
                "historical_step": str(job.get("step") or "").strip(),
                "source_kind": source_kind,
            }
        )
    return entries


def _job_result_preview_files(job: dict[str, Any], system_dir: Path, run_dir: Path) -> list[str]:
    step = str(job.get("step") or "").strip()
    patterns = _job_result_preview_patterns(step)
    if not patterns:
        return []
    preview_paths: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for source_path in sorted(run_dir.glob(pattern)):
            if not source_path.exists() or not source_path.is_file():
                continue
            relative = source_path.relative_to(system_dir).as_posix()
            if relative in seen:
                continue
            seen.add(relative)
            preview_paths.append(relative)
    return preview_paths


def job_result_context_payload(job: dict[str, Any]) -> dict[str, Any]:
    system_dir = SYSTEMS_DIR / str(job.get("system") or "").strip()
    run_dir, source_kind = _job_result_source_run_dir(job)
    timestamp = str(job.get("finished_at") or job.get("created_at") or "").strip()
    source_note = (
        f"Showing archived results for job {job['id']}."
        if source_kind == "archive"
        else f"Showing the dedicated attempt work directory for job {job['id']}."
        if source_kind == "attempt"
        else f"Showing the current live run directory for the latest job {job['id']}."
        if source_kind == "live"
        else f"Historical result files are not available for job {job['id']}; only the job log can be shown."
    )

    payload: dict[str, Any] = {
        "job_id": job["id"],
        "system": job.get("system"),
        "step": job.get("step"),
        "available": run_dir is not None,
        "source_kind": source_kind,
        "source_run_dir": run_dir.relative_to(system_dir).as_posix() if run_dir is not None else None,
        "source_note": source_note,
        "timestamp": timestamp,
        "result_highlights": [],
        "tree": [],
        "preview_files": [],
    }
    if run_dir is None:
        return payload

    payload["result_highlights"] = _job_result_context_highlights(job, system_dir, run_dir)
    payload["tree"] = _job_result_tree_entries(job, system_dir, run_dir, source_kind=source_kind)
    payload["preview_files"] = _job_result_preview_files(job, system_dir, run_dir)
    step = str(job.get("step") or "").strip()
    if step == "band":
        payload["band_visualization"] = band_visualization(system_dir, run_dir)
    elif step in {"dos", "scf"}:
        payload["dos_visualization"] = dos_visualization(system_dir, source_step=step, run_dir=run_dir)
        payload["dos_artifacts"] = dos_artifacts_payload(system_dir, source_step=step, run_dir=run_dir)
    elif step == "phonon":
        payload["phonon_visualization"] = phonon_visualization(system_dir, run_dir)
        payload["phonon_dos_visualization"] = phonon_dos_visualization(system_dir, run_dir)
    return payload


def legacy_job_failed(job: dict[str, Any]) -> bool:
    exit_payload = _job_exit_code_payload(job)
    exit_code = exit_payload.get("exit_code") if isinstance(exit_payload, dict) else None
    if isinstance(exit_code, int):
        return exit_code != 0

    candidates: list[Path] = []
    target_log = Path(job["target_log"]) if job.get("target") == "local" and job.get("target_log") else None
    launcher_log = Path(job["launcher_log"]) if job.get("launcher_log") else None
    if target_log and target_log.exists():
        candidates.append(target_log)
    if launcher_log and launcher_log.exists():
        candidates.append(launcher_log)

    if job.get("target") == "local" and job.get("system") and job.get("step"):
        run_dir = SYSTEMS_DIR / job["system"] / "runs" / job["step"]
        if run_dir.exists():
            for extra in (run_dir / "log", run_dir / "OUTCAR", run_dir / "phonopy_plot.log"):
                if extra.exists():
                    candidates.append(extra)
            if job["step"] == "phonon":
                candidates.extend(sorted(run_dir.glob("dis-*/log")))
                candidates.extend(sorted(run_dir.glob("dis-*/OUTCAR")))

    markers = (
        "I REFUSE TO CONTINUE",
        "NON-ZERO EXIT CODE",
        "EXIT CODE:    1",
        "STOP 1",
        "ERROR IN SUBSPACE ROTATION",
        "COULD NOT ALLOCATE WAVEFUNCTION",
        "NO VASPRUN.XML FOUND",
        "OUT OF MEMORY",
        "OOM-KILL",
        "KILLED PROCESS",
    )
    for path in candidates:
        text = read_text_tail(path).upper()
        if any(marker in text for marker in markers):
            return True
    return False


def local_resume_supported(job: dict[str, Any]) -> bool:
    if job.get("target") != "local":
        return False
    if job.get("backend") == "aiida":
        return aiida_backend_enabled() and supports_aiida_submission(str(job.get("step") or ""))
    return str(job.get("step") or "") in LOCAL_RESUMABLE_LEGACY_STEPS


def job_supported_actions(job: dict[str, Any]) -> list[str]:
    if job.get("target") != "local":
        return []
    state = str(job.get("state") or "")
    actions: list[str] = []
    aiida_controls_available = job.get("backend") != "aiida" or aiida_backend_enabled()
    if state in {"running", "queued"} and aiida_controls_available:
        actions.extend(["pause", "stop"])
    if state in {"paused", "failed", "stopped"} and local_resume_supported(job):
        actions.append("resume")
    return actions


def is_terminal_job_state(state: str | None) -> bool:
    return str(state or "").strip().lower() in TERMINAL_JOB_STATES


def job_results_ready(job: dict[str, Any]) -> bool:
    system = str(job.get("system") or "").strip()
    step = str(job.get("step") or "").strip()
    if not system or not step:
        return False
    run_dir, _ = _job_result_source_run_dir(job)
    if run_dir is None:
        return False
    return _step_results_ready_in_dir(run_dir, step)


def annotate_job_record(job: dict[str, Any]) -> dict[str, Any]:
    state = str(job.get("state") or "").strip().lower() or "unknown"
    platform_error = str(job.get("platform_error") or "").strip()
    process_status = str(job.get("process_status") or "").strip()
    sync_error = str(job.get("workspace_sync_error") or "").strip()
    result_ready = job_results_ready(job)
    terminal_state = is_terminal_job_state(state)

    backend_issue = bool(platform_error)
    if not backend_issue and job.get("backend") == "aiida" and state == "failed" and process_status:
        backend_issue = bool(BACKEND_ISSUE_PATTERN.search(process_status))

    display_state = state
    notes: list[str] = []
    if backend_issue:
        display_state = "backend_issue"
        notes.append("AiiDA/backend issue")
    elif sync_error and terminal_state:
        display_state = "sync_issue"
        notes.append("Workspace sync issue")
    elif state == "failed" and result_ready:
        display_state = "result_ready"
        notes.append("Result files detected")

    if terminal_state and result_ready and "Result files detected" not in notes:
        notes.append("Result files detected")
    if terminal_state and sync_error and "Workspace sync issue" not in notes:
        notes.append("Workspace sync issue")
    if job.get("backend") == "aiida" and job.get("backend_state"):
        notes.append(f"AiiDA {job['backend_state']}")
    elif job.get("target") == "remote" and job.get("remote_status"):
        notes.append(f"Remote {job['remote_status']}")
    if backend_issue:
        detail = platform_error or process_status
        if detail:
            notes.append(detail.splitlines()[0])

    job["display_state"] = display_state
    job["status_summary"] = " | ".join(notes[:3])
    job["platform_status"] = "error" if backend_issue else "ok"
    job["sync_status"] = "error" if sync_error else "ok" if job.get("workspace_synced_at") else "idle"
    job["result_ready"] = result_ready
    archive_dir = job_result_archive_dir(job)
    if _job_archive_has_files(archive_dir):
        job["result_archive"] = str(archive_dir)
        job["result_archive_status"] = "ready"
    job["is_terminal"] = terminal_state
    job["can_delete"] = job["is_terminal"] and state not in ACTIVE_JOB_STATES
    return job


def managed_launcher_log_path(job: dict[str, Any]) -> Path | None:
    launcher_log = str(job.get("launcher_log") or "").strip()
    if not launcher_log:
        return None
    path = Path(launcher_log)
    return _resolved_path_within_root(path, RUNTIME_DIR / "job_logs")


def remove_job_runtime_artifacts(job: dict[str, Any]) -> None:
    launcher_log = managed_launcher_log_path(job)
    if launcher_log is not None and launcher_log.exists():
        launcher_log.unlink()
    attempt_dir = _job_attempt_dir(job)
    if attempt_dir is not None and attempt_dir.exists():
        shutil.rmtree(attempt_dir, ignore_errors=True)
    archive_dir = job_result_archive_dir(job)
    if archive_dir is not None and archive_dir.exists():
        shutil.rmtree(archive_dir, ignore_errors=True)


def delete_job_record(job_id: str) -> dict[str, Any]:
    deleted: dict[str, Any] | None = None

    def _delete(jobs: list[dict[str, Any]]) -> None:
        nonlocal deleted
        job = next((item for item in jobs if item["id"] == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        state = str(job.get("state") or "").strip().lower()
        if state in ACTIVE_JOB_STATES:
            raise HTTPException(status_code=400, detail=f"Job {job_id} is still active and cannot be deleted")
        deleted = dict(job)
        jobs[:] = [item for item in jobs if item["id"] != job_id]

    _mutate_jobs(_delete, refresh=True)
    if deleted is None:
        raise HTTPException(status_code=404, detail="Job not found")
    remove_job_runtime_artifacts(deleted)
    return {
        "deleted_job_id": job_id,
        "system": deleted.get("system"),
        "step": deleted.get("step"),
        "remaining_jobs": len(load_jobs()),
    }


def cleanup_job_history(
    *,
    system: str | None = None,
    step: str | None = None,
    keep_latest: int = 1,
    selected_job_id: str | None = None,
) -> dict[str, Any]:
    def matches_scope(job: dict[str, Any]) -> bool:
        if system and job.get("system") != system:
            return False
        if step and job.get("step") != step:
            return False
        return True

    removed_jobs: list[dict[str, Any]] = []

    def _cleanup(jobs: list[dict[str, Any]]) -> None:
        terminal_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for job in jobs:
            state = str(job.get("state") or "").strip().lower()
            if not matches_scope(job) or state in ACTIVE_JOB_STATES or not is_terminal_job_state(state):
                continue
            group_key = (str(job.get("system") or ""), str(job.get("step") or ""))
            terminal_groups.setdefault(group_key, []).append(job)

        kept_terminal_ids: set[str] = set()
        for group_jobs in terminal_groups.values():
            keep_candidates = list(group_jobs)
            if selected_job_id and len(keep_candidates) > keep_latest:
                selected_index = next(
                    (index for index, item in enumerate(keep_candidates) if str(item.get("id") or "") == selected_job_id),
                    None,
                )
                if selected_index is not None:
                    keep_candidates.append(keep_candidates.pop(selected_index))
            kept_terminal_ids.update(str(job.get("id") or "") for job in keep_candidates[:keep_latest] if job.get("id"))

        kept_jobs: list[dict[str, Any]] = []
        for job in jobs:
            state = str(job.get("state") or "").strip().lower()
            if not matches_scope(job) or state in ACTIVE_JOB_STATES or not is_terminal_job_state(state):
                kept_jobs.append(job)
                continue

            if str(job.get("id") or "") in kept_terminal_ids:
                kept_jobs.append(job)
                continue

            removed_jobs.append(dict(job))

        jobs[:] = kept_jobs

    _mutate_jobs(_cleanup, refresh=True)
    for job in removed_jobs:
        remove_job_runtime_artifacts(job)

    return {
        "scope": {"system": system, "step": step},
        "keep_latest": keep_latest,
        "removed_count": len(removed_jobs),
        "kept_count": len(load_jobs()),
        "removed_job_ids": [job["id"] for job in removed_jobs],
        "selected_job_removed": selected_job_id is not None and any(job.get("id") == selected_job_id for job in removed_jobs),
    }


def legacy_active_work_dir(job: dict[str, Any]) -> Path:
    run_dir = _job_attempt_dir(job) or (SYSTEMS_DIR / job["system"] / "runs" / job["step"])
    if job.get("step") == "phonon" and run_dir.exists():
        for pattern in ("dis-*/log", "dis-*/OUTCAR"):
            latest = latest_matching_file(run_dir, pattern)
            if latest is not None:
                return latest.parent
    return run_dir


def write_stopcar(work_dir: Path, *, immediate: bool = False) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    stopcar = work_dir / "STOPCAR"
    if immediate:
        stopcar.write_text("LABORT = .TRUE.\n", encoding="utf-8")
    else:
        stopcar.write_text("LSTOP = .TRUE.\n", encoding="utf-8")
    return stopcar


def terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        os.kill(pid, signal.SIGTERM)


def pause_local_legacy_job(job: dict[str, Any]) -> dict[str, Any]:
    work_dir = legacy_active_work_dir(job)
    stopcar = write_stopcar(work_dir)
    job["control_state"] = "pause_requested"
    job["control_requested_at"] = now_iso()
    job["control_details"] = {"mode": "stopcar", "path": str(stopcar)}
    job["state"] = "pausing"
    return job


def stop_local_legacy_job(job: dict[str, Any]) -> dict[str, Any]:
    pid = int(job.get("pid") or 0)
    if not pid:
        raise HTTPException(status_code=400, detail="Legacy job PID is missing")
    terminate_process_group(pid)
    job["control_state"] = "stop_requested"
    job["control_requested_at"] = now_iso()
    job["state"] = "stopping"
    return job


def _refresh_job_states_in_place(jobs: list[dict[str, Any]]) -> bool:
    changed = False
    for job in jobs:
        if job.get("backend") == "aiida":
            previous_state = str(job.get("state") or "").strip()
            try:
                before = json.dumps(job, sort_keys=True, default=str)
                refresh_aiida_job(settings.aiida_profile_name, job)
                job.pop("platform_error", None)
                if job.get("control_state") == "pause_requested":
                    job["state"] = "paused" if job.get("state") in {"finished", "failed", "stopped"} or job.get("exit_status") is not None else "pausing"
                elif job.get("control_state") == "stop_requested":
                    job["state"] = "stopped" if job.get("state") in {"finished", "failed", "stopped"} or job.get("exit_status") is not None else "stopping"
                if not is_terminal_job_state(job.get("state")) and job.get("finished_at"):
                    job.pop("finished_at", None)

                if job.get("state") in {"finished", "paused", "stopped"} and not job.get("workspace_synced_at"):
                    try:
                        sync_info = sync_aiida_job_to_workspace(
                            settings.aiida_profile_name,
                            job,
                            SYSTEMS_DIR / job["system"],
                        )
                        job["workspace_synced_at"] = now_iso()
                        job["workspace_sync"] = sync_info
                    except Exception as sync_exc:  # pragma: no cover - environment specific
                        job["workspace_sync_error"] = str(sync_exc)
                if job.get("state") in {"finished", "failed", "paused", "stopped"} and not job.get("finished_at"):
                    job["finished_at"] = now_iso()
                archived = _maybe_archive_job_results(job)
                if archived is not None:
                    job["result_archive"] = str(archived)
                    job["result_archive_status"] = "ready"
                changed = _apply_relax_completion_refresh(job, previous_state) or changed
                after = json.dumps(job, sort_keys=True, default=str)
                changed = changed or before != after
            except Exception as exc:  # pragma: no cover - environment specific
                job["platform_error"] = str(exc)
                if not previous_state:
                    job["state"] = "failed"
                if is_terminal_job_state(job.get("state")) and not job.get("finished_at"):
                    job["finished_at"] = now_iso()
                changed = True
            before_actions = tuple(job.get("supported_actions", []))
            job["supported_actions"] = job_supported_actions(job)
            before_annotation = json.dumps(
                {
                    "display_state": job.get("display_state"),
                    "status_summary": job.get("status_summary"),
                    "platform_status": job.get("platform_status"),
                    "sync_status": job.get("sync_status"),
                    "result_ready": job.get("result_ready"),
                    "can_delete": job.get("can_delete"),
                },
                sort_keys=True,
                default=str,
            )
            annotate_job_record(job)
            after_annotation = json.dumps(
                {
                    "display_state": job.get("display_state"),
                    "status_summary": job.get("status_summary"),
                    "platform_status": job.get("platform_status"),
                    "sync_status": job.get("sync_status"),
                    "result_ready": job.get("result_ready"),
                    "can_delete": job.get("can_delete"),
                },
                sort_keys=True,
                default=str,
            )
            changed = changed or before_actions != tuple(job.get("supported_actions", []))
            changed = changed or before_annotation != after_annotation
            changed = _sync_run_manifest_runtime_state(
                SYSTEMS_DIR / str(job.get("system") or "").strip(),
                str(job.get("step") or "").strip(),
                str(job.get("state") or "").strip(),
                job.get("finished_at"),
            ) or changed
            continue
        if job.get("target") == "remote":
            previous_state = str(job.get("state") or "").strip()
            profile_name = job.get("profile")
            profile = get_submission_profile(profile_name) if profile_name else None
            if profile:
                try:
                    before = json.dumps(job, sort_keys=True, default=str)
                    refresh_remote_job(profile, job)
                    if not is_terminal_job_state(job.get("state")) and job.get("finished_at"):
                        job.pop("finished_at", None)
                    if job.get("state") in {"finished", "failed"} and not job.get("finished_at"):
                        job["finished_at"] = now_iso()
                    archived = _maybe_archive_job_results(job)
                    if archived is not None:
                        job["result_archive"] = str(archived)
                        job["result_archive_status"] = "ready"
                    changed = _apply_relax_completion_refresh(job, previous_state) or changed
                    after = json.dumps(job, sort_keys=True, default=str)
                    changed = changed or before != after
                except SchedulerError as exc:
                    job["remote_status"] = str(exc)
                    changed = True
            before_actions = tuple(job.get("supported_actions", []))
            job["supported_actions"] = job_supported_actions(job)
            before_annotation = json.dumps(
                {
                    "display_state": job.get("display_state"),
                    "status_summary": job.get("status_summary"),
                    "platform_status": job.get("platform_status"),
                    "sync_status": job.get("sync_status"),
                    "result_ready": job.get("result_ready"),
                    "can_delete": job.get("can_delete"),
                },
                sort_keys=True,
                default=str,
            )
            annotate_job_record(job)
            after_annotation = json.dumps(
                {
                    "display_state": job.get("display_state"),
                    "status_summary": job.get("status_summary"),
                    "platform_status": job.get("platform_status"),
                    "sync_status": job.get("sync_status"),
                    "result_ready": job.get("result_ready"),
                    "can_delete": job.get("can_delete"),
                },
                sort_keys=True,
                default=str,
            )
            changed = changed or before_actions != tuple(job.get("supported_actions", []))
            changed = changed or before_annotation != after_annotation
            changed = _sync_run_manifest_runtime_state(
                SYSTEMS_DIR / str(job.get("system") or "").strip(),
                str(job.get("step") or "").strip(),
                str(job.get("state") or "").strip(),
                job.get("finished_at"),
            ) or changed
            continue
        previous_state = str(job.get("state") or "").strip()
        # Freeze terminal legacy jobs once they have been finalized. Before freezing,
        # reconcile them against their own archived launcher log so old successful
        # runs are not left behind as "failed" just because later reruns reuse the
        # same runs/<step> directory.
        if job.get("state") in {"finished", "failed", "paused", "stopped"} and job.get("finished_at"):
            archived_state = _legacy_archived_terminal_state(job)
            if archived_state and archived_state != job.get("state"):
                job["state"] = archived_state
                changed = True
            archived = _maybe_archive_job_results(job)
            if archived is not None:
                job["result_archive"] = str(archived)
                job["result_archive_status"] = "ready"
            changed = _sync_local_attempt_to_live_run_dir(job) or changed
            before_actions = tuple(job.get("supported_actions", []))
            job["supported_actions"] = job_supported_actions(job)
            before_annotation = json.dumps(
                {
                    "display_state": job.get("display_state"),
                    "status_summary": job.get("status_summary"),
                    "platform_status": job.get("platform_status"),
                    "sync_status": job.get("sync_status"),
                    "result_ready": job.get("result_ready"),
                    "can_delete": job.get("can_delete"),
                },
                sort_keys=True,
                default=str,
            )
            annotate_job_record(job)
            after_annotation = json.dumps(
                {
                    "display_state": job.get("display_state"),
                    "status_summary": job.get("status_summary"),
                    "platform_status": job.get("platform_status"),
                    "sync_status": job.get("sync_status"),
                    "result_ready": job.get("result_ready"),
                    "can_delete": job.get("can_delete"),
                },
                sort_keys=True,
                default=str,
            )
            changed = changed or before_actions != tuple(job.get("supported_actions", []))
            changed = changed or before_annotation != after_annotation
            changed = _sync_run_manifest_runtime_state(
                SYSTEMS_DIR / str(job.get("system") or "").strip(),
                str(job.get("step") or "").strip(),
                str(job.get("state") or "").strip(),
                job.get("finished_at"),
            ) or changed
            changed = _sync_attempt_manifest(job) or changed
            continue
        pid = job.get("pid")
        process_running = False
        if pid and psutil.pid_exists(pid):
            try:
                process_running = psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
            except psutil.Error:
                process_running = False
        if process_running:
            if job.get("control_state") == "pause_requested":
                job["state"] = "pausing"
            elif job.get("control_state") == "stop_requested":
                job["state"] = "stopping"
            else:
                job["state"] = "running"
            if job.get("finished_at"):
                job.pop("finished_at", None)
                changed = True
        else:
            system_dir = SYSTEMS_DIR / job["system"]
            if job.get("control_state") == "pause_requested":
                resolved_state = "paused"
            elif job.get("control_state") == "stop_requested":
                resolved_state = "stopped"
            else:
                resolved_state = "failed" if legacy_job_failed(job) else "finished" if step_completed(system_dir, job["step"]) else "failed"
            if job.get("state") != resolved_state:
                job["state"] = resolved_state
                changed = True
            if job.get("state") in {"finished", "failed", "paused", "stopped"} and not job.get("finished_at"):
                job["finished_at"] = now_iso()
                changed = True
            archived = _maybe_archive_job_results(job)
            if archived is not None:
                job["result_archive"] = str(archived)
                job["result_archive_status"] = "ready"
            changed = _apply_relax_completion_refresh(job, previous_state) or changed
        changed = _sync_local_attempt_to_live_run_dir(job) or changed
        before_actions = tuple(job.get("supported_actions", []))
        job["supported_actions"] = job_supported_actions(job)
        before_annotation = json.dumps(
            {
                "display_state": job.get("display_state"),
                "status_summary": job.get("status_summary"),
                "platform_status": job.get("platform_status"),
                "sync_status": job.get("sync_status"),
                "result_ready": job.get("result_ready"),
                "can_delete": job.get("can_delete"),
            },
            sort_keys=True,
            default=str,
        )
        annotate_job_record(job)
        after_annotation = json.dumps(
            {
                "display_state": job.get("display_state"),
                "status_summary": job.get("status_summary"),
                "platform_status": job.get("platform_status"),
                "sync_status": job.get("sync_status"),
                "result_ready": job.get("result_ready"),
                "can_delete": job.get("can_delete"),
            },
            sort_keys=True,
            default=str,
        )
        changed = changed or before_actions != tuple(job.get("supported_actions", []))
        changed = changed or before_annotation != after_annotation
        changed = _sync_run_manifest_runtime_state(
            SYSTEMS_DIR / str(job.get("system") or "").strip(),
            str(job.get("step") or "").strip(),
            str(job.get("state") or "").strip(),
            job.get("finished_at"),
        ) or changed
        changed = _sync_attempt_manifest(job) or changed
    return changed


def refresh_job_states() -> list[dict[str, Any]]:
    return _mutate_jobs(lambda jobs: list(jobs), refresh=True, save_if_unchanged=False)


def launch_local_step(
    system: str,
    step: str,
    mpi_np: int,
    *,
    resume: bool = False,
    backend_preference: str | None = None,
    resumed_from: str | None = None,
) -> dict[str, Any]:
    step = validate_submission_step(step)
    system_dir = resolve_system_dir(system)
    system = system_dir.name

    fallback_warnings: list[str] = []
    preflight = _prepare_step_inputs_for_submission(system_dir, step, apply_changes=True)
    if preflight["blocking_errors"]:
        raise HTTPException(status_code=400, detail=" ".join(preflight["blocking_errors"]))
    fallback_warnings.extend(preflight["warnings"])
    gate = submission_gate(system_dir, step, "local", mpi_np)
    if gate["blocking_errors"]:
        raise HTTPException(status_code=400, detail=" ".join(gate["blocking_errors"]))
    if step == "converge" and not aiida_backend_enabled():
        raise HTTPException(
            status_code=400,
            detail="Converge submissions require the optional AiiDA backend. Enable VASP_STUDIO_ENABLE_AIIDA_BACKEND=1 and install requirements-aiida.txt.",
        )
    if supports_aiida_submission(step) and aiida_backend_enabled() and backend_preference != "legacy":
        try:
            record = submit_aiida_submission(settings.aiida_profile_name, system_dir, step, mpi_np, resume=resume)
            job_id = f"{system}-{step}-aiida-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            merged_warnings = _merge_warnings(fallback_warnings, record.get("warnings", []), gate["warnings"])
            record.update(
                {
                    "id": job_id,
                    "created_at": now_iso(),
                    "launcher_log": None,
                    "target_log": None,
                    "warnings": merged_warnings,
                    "risk_level": "review" if gate["risk_level"] == "ok" and merged_warnings else gate["risk_level"],
                    "readiness": gate["readiness"],
                    "blocking_errors": [],
                    "resumed_from": resumed_from,
                }
            )
            record.update(_submission_input_record_fields(system_dir, step, resume=resume))
            _attach_run_manifest(system_dir, step, record)
            _mutate_jobs(lambda jobs: jobs.insert(0, record), refresh=True)
            return record
        except AiiDASubmissionError as exc:
            if step == "converge":
                raise HTTPException(status_code=400, detail=f"AiiDA converge submit failed: {exc}") from exc
            fallback_warnings.append(f"AiiDA submit unavailable, falling back to legacy runner: {exc}")
        except Exception as exc:  # pragma: no cover - runtime dependent
            raise HTTPException(status_code=500, detail=f"AiiDA submission failed: {exc}") from exc

    runner_issue = local_legacy_runner_issue()
    if runner_issue:
        raise HTTPException(status_code=400, detail=runner_issue)
    run_step = local_legacy_runner_path()

    log_dir = RUNTIME_DIR / "job_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    job_id = f"{system}-{step}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    launcher_log = log_dir / f"{job_id}.log"
    attempt_dir = _local_attempt_dir_for_job(system_dir, step, job_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["VASP_MPI_NP"] = str(mpi_np)
    env["VASP_ENV_SH"] = str(settings.vasp_env_script)
    env["VASP_WORK_DIR"] = str(attempt_dir)
    if resume:
        env["VASP_RESUME"] = "1"

    with launcher_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [str(run_step), str(system_dir), step],
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

    record = {
        "id": job_id,
        "system": system,
        "step": step,
        "target": "local",
        "backend": "legacy",
        "pid": proc.pid,
        "mpi_np": mpi_np,
        "state": "running",
        "created_at": now_iso(),
        "launcher_log": str(launcher_log),
        "target_log": (attempt_dir / "log").as_posix(),
        "attempt_dir": _relative_system_path(system_dir, attempt_dir),
        "warnings": _merge_warnings(fallback_warnings, gate["warnings"]),
        "risk_level": "review" if gate["risk_level"] == "ok" and fallback_warnings else gate["risk_level"],
        "readiness": gate["readiness"],
        "blocking_errors": [],
        "resume": resume,
        "resumed_from": resumed_from,
    }
    record.update(_submission_input_record_fields(system_dir, step, resume=resume))
    _attach_run_manifest(system_dir, step, record)
    _sync_attempt_manifest(record)
    _mutate_jobs(lambda jobs: jobs.insert(0, record), refresh=True)
    return record


def validate_submission(system: str, step: str, target: str, mpi_np: int) -> dict[str, Any]:
    step = validate_submission_step(step)
    system_dir = resolve_system_dir(system)
    system = system_dir.name

    record: dict[str, Any] = {
        "system": system,
        "step": step,
        "target": target,
        "mpi_np": mpi_np,
        "workspace_root": str(ROOT),
    }
    record.update(_submission_input_record_fields(system_dir, step))
    preflight = _prepare_step_inputs_for_submission(system_dir, step, apply_changes=False)
    gate = submission_gate(system_dir, step, target, mpi_np)
    blocking_errors = list(preflight["blocking_errors"])
    for message in gate["blocking_errors"]:
        if message not in blocking_errors:
            blocking_errors.append(message)
    warnings = _merge_warnings(preflight["warnings"], gate["warnings"])
    record["warnings"] = warnings
    record["blocking_errors"] = blocking_errors
    record["risk_level"] = "high" if blocking_errors else "review" if warnings and gate["risk_level"] == "ok" else gate["risk_level"]
    record["readiness"] = "blocked" if blocking_errors else "review" if warnings and gate["readiness"] == "ready" else gate["readiness"]
    if target == "local":
        if step == "converge" and not aiida_backend_enabled():
            raise HTTPException(
                status_code=400,
                detail="Converge previews require the optional AiiDA backend. Enable VASP_STUDIO_ENABLE_AIIDA_BACKEND=1 and install requirements-aiida.txt.",
            )
        if supports_aiida_submission(step) and aiida_backend_enabled():
            try:
                preview = preview_aiida_submission(settings.aiida_profile_name, system_dir, step, mpi_np)
                preview["warnings"] = _merge_warnings(preview.get("warnings", []), record["warnings"])
                preview["risk_level"] = "high" if record["blocking_errors"] else "review" if preview["warnings"] and record["risk_level"] == "ok" else record["risk_level"]
                preview["blocking_errors"] = record["blocking_errors"]
                preview["readiness"] = record["readiness"]
                record.update(preview)
                return record
            except AiiDASubmissionError as exc:
                if step == "converge":
                    raise HTTPException(status_code=400, detail=f"AiiDA converge preview failed: {exc}") from exc
                record["warnings"].append(f"AiiDA preview unavailable, falling back to legacy runner: {exc}")

        runner_issue = local_legacy_runner_issue()
        if runner_issue:
            raise HTTPException(status_code=400, detail=runner_issue)
        run_step = local_legacy_runner_path()
        record["target_kind"] = "local"
        record["submit_backend"] = "legacy"
        record["submission_mode"] = "legacy"
        record["command_preview"] = [str(run_step), str(system_dir), step]
        return record

    if step == "converge":
        raise HTTPException(status_code=400, detail="Converge is currently available only through the local AiiDA backend")

    profile = next((item for item in load_profiles() if item["name"] == target), None)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Unknown submission profile: {target}")

    try:
        remote_preview = preview_remote_submission(profile, system, step, mpi_np)
    except SchedulerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record["target_kind"] = "remote"
    record["submit_backend"] = "legacy"
    record["submission_mode"] = remote_preview["submission_mode"]
    record["scheduler_kind"] = remote_preview["scheduler_kind"]
    record["host"] = remote_preview["host"]
    record["remote_workspace_root"] = remote_preview["remote_workspace_root"]
    record["remote_log"] = remote_preview["remote_log"]
    record["remote_command_preview"] = remote_preview["remote_command_preview"]
    record["pre_command"] = profile.get("pre_command", "")
    record["warnings"].append("Remote submission expects SSH access and a mirrored workspace path.")
    if record["risk_level"] == "ok":
        record["risk_level"] = "review"
    if record["readiness"] == "ready":
        record["readiness"] = "review"
    return record


def launch_remote_step(system: str, step: str, mpi_np: int, profile_name: str) -> dict[str, Any]:
    step = validate_submission_step(step)
    if step == "converge":
        raise HTTPException(status_code=400, detail="Converge is currently available only through the local AiiDA backend")

    profile = get_submission_profile(profile_name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Unknown submission profile: {profile_name}")
    system_dir = resolve_system_dir(system)
    system = system_dir.name
    preflight = _prepare_step_inputs_for_submission(system_dir, step, apply_changes=True)
    if preflight["blocking_errors"]:
        raise HTTPException(status_code=400, detail=" ".join(preflight["blocking_errors"]))
    gate = submission_gate(system_dir, step, "remote", mpi_np)
    if gate["blocking_errors"]:
        raise HTTPException(status_code=400, detail=" ".join(gate["blocking_errors"]))

    job_id = f"{system}-{step}-remote-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        record = submit_remote_job(ROOT, RUNTIME_DIR, profile, system, step, mpi_np, job_id)
    except SchedulerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record["warnings"] = _merge_warnings(record.get("warnings", []), preflight["warnings"], gate["warnings"])
    record["risk_level"] = "review" if gate["risk_level"] == "ok" and record["warnings"] else gate["risk_level"]
    record["readiness"] = gate["readiness"]
    record["blocking_errors"] = []
    record.update(_submission_input_record_fields(system_dir, step))

    _attach_run_manifest(system_dir, step, record)
    _mutate_jobs(lambda jobs: jobs.insert(0, record), refresh=True)
    return record


def create_project(payload: ProjectCreateRequest) -> dict[str, Any]:
    new_name = sanitize_project_name(payload.name)
    target_dir = SYSTEMS_DIR / new_name
    if target_dir.exists():
        raise HTTPException(status_code=400, detail=f"Project already exists: {new_name}")

    creation_mode = _normalize_creation_mode(payload.creation_mode, payload.source_system)
    raw_structure = payload.structure_content if payload.structure_content is not None else payload.poscar_content
    imported_structure = None
    if raw_structure and raw_structure.strip():
        try:
            imported_structure = parse_structure_text(raw_structure, payload.structure_format)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    source_dir = _project_creation_source_dir(creation_mode, payload.source_system)
    poscar_text, structure_source = _project_creation_poscar_text(creation_mode, source_dir, imported_structure)

    try:
        target_dir.mkdir(parents=True, exist_ok=False)
        atomic_write_text(target_dir / "POSCAR", poscar_text if poscar_text.endswith("\n") else f"{poscar_text}\n")
        metadata = _project_seed_metadata(creation_mode, new_name, source_dir, payload.material_class)
        write_json(target_dir / "metadata.json", metadata)
        write_system_lineage(target_dir, _project_lineage_payload(new_name, creation_mode, source_dir, structure_source))
        initialize_project_inputs(target_dir, formula=new_name)
        return system_payload(target_dir)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": settings.app_name, "app_version": settings.app_version, "asset_version": ASSET_VERSION},
    )


@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    usage = psutil.virtual_memory()
    cpu = local_cpu_resources()
    backend = aiida_inventory(settings.aiida_profile_name)
    return {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "workspace_root": str(ROOT),
        "systems_count": len(project_dirs()),
        "backend_provider": settings.backend_provider,
        "aiida_available": backend["available"],
        "aiida_profile": settings.aiida_profile_name,
        "features": backend_features(backend),
        "memory": {
            "total_gb": round(usage.total / 1024**3, 2),
            "available_gb": round(usage.available / 1024**3, 2),
            "used_percent": usage.percent,
        },
        "cpu": cpu,
    }


@app.get("/api/systems")
async def systems() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for system_dir in project_dirs():
        steps = {step: step_status(system_dir, step) for step in ("relax", "scf", "dos", "converge", "elastic", "band", "charge", "phonon")}
        integrity = system_integrity_report(system_dir)
        payload.append(
            {
                "name": system_dir.name,
                "summary": system_summary(system_dir),
                "composition_formula": integrity["composition_formula"],
                "quarantined": integrity["quarantined"],
                "integrity_summary": {
                    "overall": integrity["overall"],
                    "counts_by_kind": integrity["counts_by_kind"],
                },
                "steps": steps,
            }
        )
    return payload


@app.get("/api/systems/{system_name}")
async def system_detail(system_name: str) -> dict[str, Any]:
    system_dir = resolve_system_dir(system_name)
    return system_payload(system_dir)


@app.get("/api/systems/{system_name}/integrity")
async def system_integrity(system_name: str) -> dict[str, Any]:
    system_dir = resolve_system_dir(system_name)
    return system_integrity_report(system_dir)


@app.get("/api/integrity/audit")
async def integrity_audit() -> dict[str, Any]:
    return integrity_audit_payload()


@app.get("/api/systems/{system_name}/band/export")
async def band_export(system_name: str, kind: str = Query("data")) -> FileResponse:
    system_dir = resolve_system_dir(system_name)

    export_csv, tick_csv = _write_origin_band_exports(system_dir)
    if kind == "data":
        return FileResponse(export_csv, media_type="text/csv", filename=export_csv.name)
    if kind == "ticks":
        return FileResponse(tick_csv, media_type="text/csv", filename=tick_csv.name)
    raise HTTPException(status_code=400, detail="Unknown band export kind")


# Mutating routes below are plain `def` so vaspkit/subprocess work runs in the
# thread pool without freezing the event loop. This lock keeps them mutually
# exclusive, preserving the serialized-write behavior they had on the loop.
_MUTATING_ROUTE_LOCK = threading.Lock()


def _serialized_route(fn):
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _MUTATING_ROUTE_LOCK:
            return fn(*args, **kwargs)

    return wrapper


@app.post("/api/systems/{system_name}/dos/pdos")
@_serialized_route
def generate_dos_pdos(system_name: str) -> dict[str, Any]:
    system_dir = resolve_system_dir(system_name)
    result = generate_element_pdos(system_dir)
    return {
        **result,
        "detail": system_payload(system_dir),
    }


@app.post("/api/systems/{system_name}/relax/primitive")
@_serialized_route
def generate_relax_primitive(system_name: str) -> dict[str, Any]:
    system_dir = resolve_system_dir(system_name)
    result = generate_primitive_cell(system_dir)
    return {
        **result,
        "detail": system_payload(system_dir),
    }


@app.get("/api/aiida/systems/{system_name}/recipe")
async def aiida_system_recipe(system_name: str) -> dict[str, Any]:
    system_dir = resolve_system_dir(system_name)
    return aiida_recipe_payload(system_dir)


@app.post("/api/phonon/nac/save")
@_serialized_route
def save_phonon_nac_settings(payload: PhononNacSettingsRequest) -> dict[str, Any]:
    system_dir = resolve_system_dir(payload.system)
    try:
        result = _update_phonon_nac_settings(system_dir, payload.enabled, payload.q_direction_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result,
        "detail": system_payload(system_dir),
    }


@app.post("/api/phonon/nac/prepare-charge")
@_serialized_route
def prepare_charge_for_phonon_nac(payload: SystemOnlyRequest) -> dict[str, Any]:
    system_dir = resolve_system_dir(payload.system)
    try:
        result = _prepare_charge_for_born(system_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result,
        "detail": system_payload(system_dir),
    }


@app.post("/api/phonon/nac/build-born")
@_serialized_route
def build_phonon_born(payload: SystemOnlyRequest) -> dict[str, Any]:
    system_dir = resolve_system_dir(payload.system)
    try:
        result = _build_born_from_charge(system_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result,
        "detail": system_payload(system_dir),
    }


@app.get("/api/file")
async def file_preview(system: str, path: str = Query(...), expert: bool = Query(default=False)) -> dict[str, Any]:
    system_dir = resolve_system_dir(system)
    full_path = preview_file_path(system_dir, path)
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    writable_candidates = PREVIEW_FILE_CANDIDATES if expert else WRITABLE_FILE_CANDIDATES
    generation_state = file_generation_state(system_dir, path)
    preview_payload = read_limited_text_payload(full_path)
    return {
        "path": path,
        **preview_payload,
        "read_only": path not in writable_candidates,
        "generated": path in GENERATED_FILE_CANDIDATES,
        "generation_state": generation_state,
        "manual_override": generation_state["manual_override"],
        "stale": generation_state["stale"],
        "tracked": generation_state["tracked"],
        "template_driven": generation_state["template_driven"],
    }


@app.post("/api/file/save")
@_serialized_route
def save_file(payload: SaveFileRequest) -> dict[str, Any]:
    system_dir = resolve_system_dir(payload.system)

    full_path = editable_file_path(system_dir, payload.path, expert_override=payload.expert_override)
    content = payload.content
    if not content.endswith("\n"):
        content += "\n"

    if payload.path == "POSCAR":
        try:
            parse_poscar_text(content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid POSCAR: {exc}") from exc
    previous_content = full_path.read_text(encoding="utf-8", errors="ignore") if full_path.exists() else None
    previous_metadata = None
    metadata_path = system_dir / "metadata.json"
    if payload.path == "POSCAR" and metadata_path.exists():
        previous_metadata = metadata_path.read_text(encoding="utf-8", errors="ignore")
    atomic_write_text(full_path, content)
    synced_metadata_keys: list[str] = []
    if payload.path == "band.conf":
        synced_metadata_keys = sync_metadata_from_band_conf(system_dir, content)
    elif payload.path == "KPOINTS.scf":
        synced_metadata_keys = sync_metadata_from_kpoints_scf(system_dir, content)
    if payload.path in MANAGED_FILE_CANDIDATES:
        try:
            generated = generate_input_content(system_dir, payload.path)
        except ValueError:
            generated = None
        if generated is not None:
            generated_content = generated["content"]
            if not generated_content.endswith("\n"):
                generated_content += "\n"
            if content == generated_content:
                record_generated_file(system_dir, payload.path, generated_content)
    if payload.path == "POSCAR":
        try:
            refresh_structure_inputs(system_dir)
        except ValueError as exc:
            if previous_content is not None:
                atomic_write_text(full_path, previous_content)
            if previous_metadata is not None:
                atomic_write_text(metadata_path, previous_metadata)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            if previous_content is not None:
                atomic_write_text(full_path, previous_content)
            if previous_metadata is not None:
                atomic_write_text(metadata_path, previous_metadata)
            raise
    return {
        "saved_at": now_iso(),
        "path": payload.path,
        "synced_metadata_keys": synced_metadata_keys,
        "detail": system_payload(system_dir),
    }


@app.post("/api/file/generate")
@_serialized_route
def generate_file(payload: GenerateFileRequest) -> dict[str, Any]:
    system_dir = resolve_system_dir(payload.system)
    try:
        generated = generate_input_content(system_dir, payload.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.path in GENERATED_FILE_CANDIDATES:
        atomic_write_text(system_dir / payload.path, generated["content"])
        record_generated_file(system_dir, payload.path, generated["content"])
        generated["written"] = True
        generated["saved_at"] = now_iso()
        generated["detail"] = system_payload(system_dir)
        generated["generation_state"] = file_generation_state(system_dir, payload.path)
    else:
        generated["written"] = False
    return generated


@app.get("/api/material-settings")
async def material_settings(system: str) -> dict[str, Any]:
    system_dir = resolve_system_dir(system)
    backend = aiida_inventory(settings.aiida_profile_name)
    return material_form_payload(system_dir, backend)


@app.post("/api/material-settings/save")
@_serialized_route
def save_material_settings_endpoint(payload: MaterialSettingsRequest) -> dict[str, Any]:
    system_dir = resolve_system_dir(payload.system)
    backend = aiida_inventory(settings.aiida_profile_name)
    try:
        result = save_material_settings(system_dir, payload.model_dump(), backend)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "saved_at": now_iso(),
        "detail": system_payload(system_dir),
        **result,
    }


@app.get("/api/submission-profiles")
async def submission_profiles() -> list[dict[str, Any]]:
    local = {
        "name": "local",
        "host": "localhost",
        "user": "",
        "workspace_root": str(ROOT),
        "vasp_mpi_np": settings.vasp_mpi_np,
        "pre_command": "",
        "scheduler_kind": "local",
        "queue_name": "",
        "account": "",
        "walltime": "",
        "submit_options": "",
        "kind": "local",
    }
    remote = [{**item, "scheduler_kind": normalize_scheduler_kind(item), "kind": "remote"} for item in load_profiles()]
    return [local, *remote]


@app.get("/api/backend/status")
async def backend_status() -> dict[str, Any]:
    status = aiida_status(settings.aiida_profile_name)
    return {
        "provider": settings.backend_provider,
        "workspace_root": str(ROOT),
        "features": backend_features(status),
        "aiida": status,
        "installation_audit": installation_audit(status),
        "aiida_rest_api_url": settings.aiida_rest_api_url,
    }


@app.get("/api/aiida/status")
async def aiida_backend_status() -> dict[str, Any]:
    return aiida_status(settings.aiida_profile_name)


@app.get("/api/aiida/workchains")
async def aiida_workchains() -> list[dict[str, Any]]:
    return available_workchains()


@app.get("/api/aiida/codes")
async def aiida_codes() -> list[dict[str, Any]]:
    return aiida_inventory(settings.aiida_profile_name).get("codes", [])


@app.post("/api/submission-profiles")
async def create_submission_profile(payload: RemoteProfileRequest) -> dict[str, Any]:
    profiles = load_profiles()
    if any(item["name"] == payload.name for item in profiles) or payload.name == "local":
        raise HTTPException(status_code=400, detail=f"Profile already exists: {payload.name}")
    record = payload.model_dump()
    record["scheduler_kind"] = normalize_scheduler_kind(record)
    profiles.append(record)
    save_profiles(profiles)
    return record


@app.post("/api/projects")
async def create_project_endpoint(payload: ProjectCreateRequest) -> dict[str, Any]:
    return create_project(payload)


@app.delete("/api/projects/{system_name}")
async def delete_project_endpoint(system_name: str) -> dict[str, Any]:
    return delete_project(system_name)


def get_job_or_404(job_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    jobs = refresh_job_states()
    job = next((item for item in jobs if item["id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs, job


def ensure_job_action(job: dict[str, Any], action: str) -> None:
    if action not in job_supported_actions(job):
        raise HTTPException(status_code=400, detail=f"Action '{action}' is not available for job {job['id']} in state {job.get('state')}")


def resume_local_job(job: dict[str, Any]) -> dict[str, Any]:
    return launch_local_step(
        job["system"],
        job["step"],
        int(job.get("mpi_np") or settings.vasp_mpi_np),
        resume=True,
        backend_preference=str(job.get("backend") or "legacy"),
        resumed_from=job["id"],
    )


@app.get("/api/jobs")
async def jobs() -> list[dict[str, Any]]:
    return refresh_job_states()


@app.post("/api/jobs/submit")
async def submit_job(payload: SubmitJobRequest) -> dict[str, Any]:
    mpi_np = payload.mpi_np or settings.vasp_mpi_np
    if payload.target == "local":
        return launch_local_step(payload.system, payload.step, mpi_np)
    return launch_remote_step(payload.system, payload.step, mpi_np, payload.target)


@app.post("/api/jobs/validate-submit")
async def validate_job_submit(payload: SubmitJobRequest) -> dict[str, Any]:
    mpi_np = payload.mpi_np or settings.vasp_mpi_np
    return validate_submission(payload.system, payload.step, payload.target, mpi_np)


@app.post("/api/jobs/run-step")
async def run_step_job(payload: SubmitJobRequest) -> dict[str, Any]:
    return await submit_job(payload)


@app.post("/api/jobs/cleanup")
async def cleanup_jobs(payload: JobCleanupRequest) -> dict[str, Any]:
    return cleanup_job_history(
        system=payload.system,
        step=payload.step,
        keep_latest=payload.keep_latest,
        selected_job_id=payload.selected_job_id,
    )


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, Any]:
    return delete_job_record(job_id)


@app.get("/api/jobs/{job_id}/log")
async def job_log(job_id: str) -> dict[str, Any]:
    jobs = refresh_job_states()
    job = next((item for item in jobs if item["id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("backend") == "aiida":
        try:
            payload = aiida_job_log_payload(settings.aiida_profile_name, job)
            payload.setdefault("display_state", job.get("display_state", payload.get("state")))
            payload.setdefault("status_summary", job.get("status_summary", ""))
            return payload
        except Exception as exc:  # pragma: no cover - runtime dependent
            return _job_log_fallback_payload(job, reason=f"Unable to read AiiDA process log: {exc}")
    if not job.get("launcher_log"):
        return _job_log_fallback_payload(job, reason="Missing launcher log path.")
    active = legacy_active_log_path(job)
    return {
        "job_id": job_id,
        "state": job["state"],
        "display_state": job.get("display_state", job["state"]),
        "status_summary": job.get("status_summary", ""),
        "log_path": str(active),
        "content": read_text_tail(active, 16000),
    }


@app.get("/api/jobs/{job_id}/result-context")
async def job_result_context(job_id: str) -> dict[str, Any]:
    jobs = refresh_job_states()
    job = next((item for item in jobs if item["id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_result_context_payload(job)


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict[str, Any]:
    def _pause(jobs: list[dict[str, Any]]) -> dict[str, Any]:
        job = next((item for item in jobs if item["id"] == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        ensure_job_action(job, "pause")
        if job.get("target") != "local":
            raise HTTPException(status_code=400, detail="Pause is currently supported only for local jobs")

        if job.get("backend") == "aiida":
            details = request_aiida_pause(settings.aiida_profile_name, job)
            job["control_state"] = "pause_requested"
            job["control_requested_at"] = now_iso()
            job["control_details"] = details
            job["state"] = "pausing"
        else:
            pause_local_legacy_job(job)
        return dict(job)

    return _mutate_jobs(_pause, refresh=True)


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> dict[str, Any]:
    def _stop(jobs: list[dict[str, Any]]) -> dict[str, Any]:
        job = next((item for item in jobs if item["id"] == job_id), None)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        ensure_job_action(job, "stop")
        if job.get("target") != "local":
            raise HTTPException(status_code=400, detail="Stop is currently supported only for local jobs")

        if job.get("backend") == "aiida":
            details = stop_aiida_process(settings.aiida_profile_name, job)
            job["control_state"] = "stop_requested"
            job["control_requested_at"] = now_iso()
            job["control_details"] = details
            job["state"] = "stopping"
        else:
            stop_local_legacy_job(job)
        return dict(job)

    return _mutate_jobs(_stop, refresh=True)


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict[str, Any]:
    jobs, job = get_job_or_404(job_id)
    ensure_job_action(job, "resume")
    new_job = resume_local_job(job)
    def _mark_resumed(jobs: list[dict[str, Any]]) -> None:
        original = next((item for item in jobs if item["id"] == job_id), None)
        if original is not None:
            original["resumed_by"] = new_job["id"]

    _mutate_jobs(_mark_resumed, refresh=True)
    return new_job
