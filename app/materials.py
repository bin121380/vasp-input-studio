from __future__ import annotations

import hashlib
import json
import re
import tarfile
from datetime import datetime
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import Any

from .config import settings
from .input_generation import generate_input_content, normalize_custom_kpath_text, parse_poscar_model
from .magnetism import default_magmom_values
from .persistence import atomic_write_bytes, atomic_write_text, backup_file, read_json, write_json
from .structure_resolution import (
    RELAX_CONTCAR_RELATIVE,
    RELAX_PRIMITIVE_RELATIVE,
    generated_input_structure_relative_path,
    uses_downstream_structure,
)


POTCAR_ARCHIVES = (
    {settings.aiida_potcar_family_name: settings.potcar_archive_pbe_64}
    if settings.potcar_archive_pbe_64
    else {}
)

POTCAR_PROFILE_DESCRIPTIONS = {
    "conservative": "Prefer standard PAW datasets when available.",
    "semicore": "Prefer semicore-aware pv/sv datasets for transition and alkali/alkaline-earth metals.",
}

MATERIAL_PRESETS = {
    "bulk_metal": {
        "label": "Bulk Metal",
        "description": "3D bulk with metallic smearing and denser SCF mesh.",
        "material_class": "bulk",
        "electronic_type": "metal",
        "spin_polarized": False,
        "encut": 520,
        "kmesh": [12, 12, 12],
        "phonon_kmesh": [4, 4, 4],
        "phonon_dos_kmesh": [8, 8, 8],
        "phonon_supercell": [2, 2, 2],
        "band_points": 81,
        "band_kpoints_distance": 0.04,
        "wallclock_seconds": 12 * 3600,
        "potcar_profile": "conservative",
        "xc_geometry": "PBE",
        "xc_electronic": "PBE",
    },
    "bulk_semiconductor": {
        "label": "Bulk Semiconductor",
        "description": "3D bulk with conservative smearing for gapped systems.",
        "material_class": "bulk",
        "electronic_type": "semiconductor",
        "spin_polarized": False,
        "encut": 520,
        "kmesh": [10, 10, 10],
        "phonon_kmesh": [4, 4, 4],
        "phonon_dos_kmesh": [8, 8, 8],
        "phonon_supercell": [2, 2, 2],
        "band_points": 81,
        "band_kpoints_distance": 0.05,
        "wallclock_seconds": 12 * 3600,
        "potcar_profile": "conservative",
        "xc_geometry": "PBE",
        "xc_electronic": "PBE",
    },
    "magnetic_bulk": {
        "label": "Magnetic Bulk",
        "description": "Spin-polarized bulk workflow for magnetic compounds.",
        "material_class": "bulk",
        "electronic_type": "auto",
        "spin_polarized": True,
        "encut": 520,
        "kmesh": [10, 10, 10],
        "phonon_kmesh": [3, 3, 3],
        "phonon_dos_kmesh": [6, 6, 6],
        "phonon_supercell": [2, 2, 2],
        "band_points": 81,
        "band_kpoints_distance": 0.05,
        "wallclock_seconds": 18 * 3600,
        "potcar_profile": "semicore",
        "xc_geometry": "PBE",
        "xc_electronic": "PBE",
    },
    "slab_2d": {
        "label": "2D / Slab",
        "description": "Low-dimensional setup with reduced out-of-plane sampling.",
        "material_class": "slab",
        "electronic_type": "auto",
        "spin_polarized": False,
        "encut": 520,
        "kmesh": [9, 9, 1],
        "phonon_kmesh": [3, 3, 1],
        "phonon_dos_kmesh": [6, 6, 1],
        "phonon_supercell": [2, 2, 1],
        "band_points": 61,
        "band_kpoints_distance": 0.05,
        "wallclock_seconds": 18 * 3600,
        "potcar_profile": "conservative",
        "xc_geometry": "PBE",
        "xc_electronic": "PBE",
    },
    "molecule_cluster": {
        "label": "Molecule",
        "description": "Gamma-only starting point for isolated molecules or clusters.",
        "material_class": "molecule",
        "electronic_type": "insulator",
        "spin_polarized": False,
        "encut": 520,
        "kmesh": [1, 1, 1],
        "phonon_kmesh": [1, 1, 1],
        "phonon_dos_kmesh": [1, 1, 1],
        "phonon_supercell": [1, 1, 1],
        "band_points": 41,
        "band_kpoints_distance": 0.08,
        "wallclock_seconds": 6 * 3600,
        "potcar_profile": "conservative",
        "xc_geometry": "PBE",
        "xc_electronic": "PBE",
    },
}

GEOMETRY_FUNCTIONAL_OPTIONS = (
    {"value": "PBE", "label": "PBE", "description": "General-purpose semilocal default for structure/property steps."},
    {"value": "PBEsol", "label": "PBEsol", "description": "Often useful for lattice optimization of solids and slabs."},
)

ELECTRONIC_FUNCTIONAL_OPTIONS = (
    {"value": "PBE", "label": "PBE", "description": "Semilocal default for SCF, DOS, and band."},
    {"value": "HSE06", "label": "HSE06", "description": "Hybrid functional for electronic structure; much heavier than PBE."},
)

GEOMETRY_FUNCTIONAL_VALUES = {item["value"] for item in GEOMETRY_FUNCTIONAL_OPTIONS}
ELECTRONIC_FUNCTIONAL_VALUES = {item["value"] for item in ELECTRONIC_FUNCTIONAL_OPTIONS}

MATERIAL_CLASS_DEFAULT_PRESETS = {
    "bulk": "bulk_semiconductor",
    "2d": "slab_2d",
    "slab": "slab_2d",
    "molecule": "molecule_cluster",
}

SEMICORE_POTCAR_PREFERENCES = {
    "Li": ("Li_sv", "Li"),
    "Na": ("Na", "Na_pv", "Na_sv"),
    "K": ("K_sv", "K_pv", "K"),
    "Rb": ("Rb_sv", "Rb_pv", "Rb"),
    "Cs": ("Cs_sv", "Cs", "Cs_pv"),
    "Ca": ("Ca_sv", "Ca_pv", "Ca"),
    "Sr": ("Sr_sv", "Sr", "Sr_pv"),
    "Ba": ("Ba_sv", "Ba", "Ba_pv"),
    "Sc": ("Sc_sv", "Sc"),
    "Ti": ("Ti_pv", "Ti_sv", "Ti"),
    "V": ("V_pv", "V_sv", "V"),
    "Cr": ("Cr_pv", "Cr", "Cr_sv"),
    "Mn": ("Mn_pv", "Mn"),
    "Fe": ("Fe_pv", "Fe"),
    "Co": ("Co", "Co_pv"),
    "Ni": ("Ni", "Ni_pv"),
    "Cu": ("Cu_pv", "Cu"),
    "Zn": ("Zn", "Zn_sv"),
    "Ga": ("Ga_d", "Ga"),
    "Ge": ("Ge_d", "Ge"),
    "Y": ("Y_sv", "Y"),
    "Zr": ("Zr_sv", "Zr"),
    "Nb": ("Nb_pv", "Nb"),
    "Mo": ("Mo_pv", "Mo"),
    "La": ("La",),
    "H": ("H",),
    "C": ("C",),
    "N": ("N",),
    "O": ("O",),
    "F": ("F",),
    "P": ("P",),
    "S": ("S",),
    "Cl": ("Cl",),
}

PREVIEW_FILE_CANDIDATES = (
    "POSCAR",
    "BORN",
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
    "KPOINTS.downstream",
    "KPOINTS.dos",
    "KPATH.in",
    "KPOINTS.band",
    "KPOINTS.phonon",
    "band.conf",
)

WRITABLE_FILE_CANDIDATES = (
    "POSCAR",
    "INCAR.relax",
    "INCAR.scf",
    "INCAR.dos",
    "INCAR.converge",
    "INCAR.band",
    "INCAR.elastic",
    "INCAR.charge",
    "INCAR.phonon",
)

GENERATED_FILE_CANDIDATES = (
    "KPOINTS.relax",
    "KPOINTS.scf",
    "KPOINTS.dos",
    "KPATH.in",
    "KPOINTS.band",
    "KPOINTS.phonon",
    "band.conf",
)

INCAR_TEMPLATE_FILES = (
    "INCAR.relax",
    "INCAR.scf",
    "INCAR.dos",
    "INCAR.converge",
    "INCAR.band",
    "INCAR.elastic",
    "INCAR.charge",
    "INCAR.phonon",
)

MANAGED_FILE_CANDIDATES = tuple(dict.fromkeys((*GENERATED_FILE_CANDIDATES, *INCAR_TEMPLATE_FILES)))
GENERATION_META_FILENAME = ".generation_meta.json"
GENERATION_META_VERSION = 1
GENERATION_META_GENERATOR = "vasp-studio-managed-inputs/v1"
DOWNSTREAM_GENERATED_FILE_CANDIDATES = tuple(path for path in GENERATED_FILE_CANDIDATES if uses_downstream_structure(path))
BAND_SUBMISSION_GUARDED_FILES = ("KPATH.in", "KPOINTS.band")

AUTO_REGEN_REASON_FRESH = "fresh"
AUTO_REGEN_REASON_MISSING = "missing"
AUTO_REGEN_REASON_STALE = "stale"
AUTO_REGEN_REASON_UNTRACKED = "untracked"
AUTO_REGEN_REASON_MANUAL_OVERRIDE = "manual_override"
AUTO_REGEN_REASON_CUSTOM_BAND_PATH = "custom_band_path"
AUTO_REGEN_REASON_UNSUPPORTED = "unsupported"

def preview_files(system_dir: Path) -> list[str]:
    return [name for name in PREVIEW_FILE_CANDIDATES if (system_dir / name).exists()]


def writable_files(system_dir: Path) -> list[str]:
    return [name for name in WRITABLE_FILE_CANDIDATES if (system_dir / name).exists()]


def generation_meta_path(system_dir: Path) -> Path:
    return system_dir / GENERATION_META_FILENAME


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_text(text: str) -> str:
    return _digest_bytes(text.encode("utf-8"))


def _file_digest(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return _digest_bytes(path.read_bytes())


def _metadata_digest(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _digest_bytes(path.read_bytes())
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _digest_text(canonical)


def _normalized_relative_path(relative_path: str) -> str:
    return relative_path.strip().replace("\\", "/")


def _band_path_mode(metadata: dict[str, Any]) -> str:
    normalized = str(metadata.get("band_path_mode") or "auto").strip().lower()
    return normalized if normalized in {"auto", "custom"} else "auto"


def _managed_structure_source_name(system_dir: Path, relative_path: str, metadata: dict[str, Any]) -> str | None:
    normalized = _normalized_relative_path(relative_path)
    if normalized in {"KPATH.in", "KPOINTS.band"} and _band_path_mode(metadata) == "custom":
        return None
    return generated_input_structure_relative_path(system_dir, normalized)


def load_generation_meta(system_dir: Path) -> dict[str, Any]:
    payload = read_json(generation_meta_path(system_dir), {})
    files = payload.get("files")
    if not isinstance(files, dict):
        files = {}
    return {
        "version": int(payload.get("version") or GENERATION_META_VERSION),
        "files": files,
    }


def save_generation_meta(system_dir: Path, payload: dict[str, Any]) -> None:
    normalized = {
        "version": int(payload.get("version") or GENERATION_META_VERSION),
        "files": payload.get("files") if isinstance(payload.get("files"), dict) else {},
    }
    write_json(generation_meta_path(system_dir), normalized)


def _managed_source_paths(system_dir: Path, relative_path: str) -> list[str]:
    normalized = _normalized_relative_path(relative_path)
    metadata = read_json(system_dir / "metadata.json", {})
    sources = ["metadata.json"]
    structure_source = _managed_structure_source_name(system_dir, normalized, metadata)
    if structure_source:
        sources.append(structure_source)
    if normalized in {"KPOINTS.band", "band.conf"} and (system_dir / "KPATH.in").exists():
        sources.append("KPATH.in")
    return list(dict.fromkeys(sources))


def _managed_source_digests(system_dir: Path, relative_path: str) -> dict[str, str]:
    digests: dict[str, str] = {}
    for source_name in _managed_source_paths(system_dir, relative_path):
        source_path = system_dir / source_name
        if source_name == "metadata.json":
            digests[source_name] = _metadata_digest(source_path)
        else:
            digests[source_name] = _file_digest(source_path)
    return digests


def _source_label(source_name: str) -> str:
    labels = {
        "POSCAR": "POSCAR",
        "metadata.json": "material settings",
        RELAX_CONTCAR_RELATIVE: "runs/relax/CONTCAR",
        RELAX_PRIMITIVE_RELATIVE: "runs/relax/PRIMCELL.vasp",
        "KPATH.in": "KPATH.in",
    }
    return labels.get(source_name, source_name)


def _generation_note(
    *,
    managed: bool,
    generated: bool,
    template_driven: bool,
    tracked: bool,
    manual_override: bool,
    stale: bool,
    changed_sources: list[str],
) -> str:
    if not managed:
        return ""
    label = "generated file" if generated else "template-driven file" if template_driven else "managed file"
    if not tracked:
        return f"This {label} predates freshness tracking. Regenerate it once to establish a baseline."
    if manual_override and stale:
        changed = ", ".join(_source_label(source) for source in changed_sources)
        return f"This {label} was edited after generation, and its inputs changed: {changed}."
    if manual_override:
        return f"This {label} was edited after the last auto-generation."
    if stale:
        changed = ", ".join(_source_label(source) for source in changed_sources)
        return f"This {label} is stale because its inputs changed: {changed}."
    return f"This {label} matches the last auto-generated baseline."


def record_generated_files(system_dir: Path, generated_contents: dict[str, str]) -> None:
    if not generated_contents:
        return
    payload = load_generation_meta(system_dir)
    files = payload.setdefault("files", {})
    timestamp = datetime.now().isoformat(timespec="seconds")
    for relative_path, content in generated_contents.items():
        files[relative_path] = {
            "generated_at": timestamp,
            "generator_version": GENERATION_META_GENERATOR,
            "content_digest": _digest_text(content),
            "source_digests": _managed_source_digests(system_dir, relative_path),
        }
    save_generation_meta(system_dir, payload)


def record_generated_file(system_dir: Path, relative_path: str, content: str) -> None:
    record_generated_files(system_dir, {relative_path: content})


def file_generation_state(system_dir: Path, relative_path: str) -> dict[str, Any]:
    managed = relative_path in MANAGED_FILE_CANDIDATES
    generated = relative_path in GENERATED_FILE_CANDIDATES
    template_driven = relative_path in INCAR_TEMPLATE_FILES
    full_path = system_dir / relative_path
    tracked = False
    manual_override = False
    stale = False
    changed_sources: list[str] = []
    entry: dict[str, Any] | None = None

    if managed and full_path.exists() and full_path.is_file():
        entry = load_generation_meta(system_dir).get("files", {}).get(relative_path)
        tracked = isinstance(entry, dict)
        if tracked:
            recorded_digest = str(entry.get("content_digest") or "")
            manual_override = _file_digest(full_path) != recorded_digest
            recorded_sources = entry.get("source_digests")
            if not isinstance(recorded_sources, dict):
                recorded_sources = {}
            current_sources = _managed_source_digests(system_dir, relative_path)
            changed_sources = [
                source_name
                for source_name, current_digest in current_sources.items()
                if recorded_sources.get(source_name) != current_digest
            ]
            stale = bool(changed_sources)

    tags: list[str] = []
    if generated:
        tags.append("generated")
    elif template_driven:
        tags.append("template")
    if managed and not tracked and full_path.exists():
        tags.append("untracked")
    if manual_override:
        tags.append("manual")
    if stale:
        tags.append("stale")

    return {
        "managed": managed,
        "generated": generated,
        "template_driven": template_driven,
        "tracked": tracked,
        "manual_override": manual_override,
        "stale": stale,
        "fresh": managed and tracked and not manual_override and not stale,
        "changed_sources": changed_sources,
        "tags": tags,
        "note": _generation_note(
            managed=managed,
            generated=generated,
            template_driven=template_driven,
            tracked=tracked,
            manual_override=manual_override,
            stale=stale,
            changed_sources=changed_sources,
        ),
        "generated_at": entry.get("generated_at") if isinstance(entry, dict) else None,
        "generator_version": entry.get("generator_version") if isinstance(entry, dict) else None,
    }


def file_generation_states(system_dir: Path, relative_paths: list[str] | tuple[str, ...] | None = None) -> dict[str, dict[str, Any]]:
    paths = list(relative_paths) if relative_paths is not None else preview_files(system_dir)
    return {
        relative_path: file_generation_state(system_dir, relative_path)
        for relative_path in paths
    }


def generated_file_refresh_decision(
    system_dir: Path,
    relative_path: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalized_relative_path(relative_path)
    state = file_generation_state(system_dir, normalized)
    full_path = system_dir / normalized
    metadata = metadata if metadata is not None else read_json(system_dir / "metadata.json", {})

    if normalized not in MANAGED_FILE_CANDIDATES:
        return {"path": normalized, "action": "skip", "reason": AUTO_REGEN_REASON_UNSUPPORTED, "state": state}

    if full_path.exists():
        if state["manual_override"]:
            return {"path": normalized, "action": "skip", "reason": AUTO_REGEN_REASON_MANUAL_OVERRIDE, "state": state}
        if normalized in BAND_SUBMISSION_GUARDED_FILES and _band_path_mode(metadata) == "custom":
            return {"path": normalized, "action": "skip", "reason": AUTO_REGEN_REASON_CUSTOM_BAND_PATH, "state": state}
        if not state["tracked"]:
            return {"path": normalized, "action": "skip", "reason": AUTO_REGEN_REASON_UNTRACKED, "state": state}
        if not state["stale"]:
            return {"path": normalized, "action": "skip", "reason": AUTO_REGEN_REASON_FRESH, "state": state}

    reason = AUTO_REGEN_REASON_MISSING if not full_path.exists() else AUTO_REGEN_REASON_STALE
    return {"path": normalized, "action": "regenerate", "reason": reason, "state": state}


def refresh_generated_inputs_if_needed(
    system_dir: Path,
    relative_paths: list[str] | tuple[str, ...],
    *,
    apply_changes: bool = True,
) -> dict[str, Any]:
    metadata = read_json(system_dir / "metadata.json", {})
    decisions: dict[str, dict[str, Any]] = {}
    regenerated: list[str] = []
    errors: list[str] = []

    for relative_path in relative_paths:
        normalized = _normalized_relative_path(relative_path)
        if normalized in decisions:
            continue
        decision = generated_file_refresh_decision(system_dir, normalized, metadata=metadata)
        if decision["action"] != "regenerate":
            decisions[normalized] = decision
            continue
        if not apply_changes:
            decisions[normalized] = decision
            continue
        try:
            payload = generate_input_content(system_dir, normalized)
            content = payload["content"]
            atomic_write_text(system_dir / normalized, content)
            record_generated_file(system_dir, normalized, content)
            regenerated.append(normalized)
            decisions[normalized] = {
                **decision,
                "action": "regenerated",
                "state": file_generation_state(system_dir, normalized),
            }
        except Exception as exc:
            errors.append(f"{normalized}: {exc}")
            decisions[normalized] = {**decision, "action": "error", "error": str(exc)}

    return {
        "regenerated": regenerated,
        "errors": errors,
        "decisions": decisions,
    }


def parse_int_triplet(text: str, *, fallback: list[int] | None = None) -> list[int]:
    if not text.strip():
        if fallback is None:
            raise ValueError("Missing required mesh values")
        return fallback
    values = [chunk for chunk in re.split(r"[\s,]+", text.strip()) if chunk]
    if len(values) != 3:
        raise ValueError("Expected exactly three integers")
    try:
        return [max(1, int(value)) for value in values]
    except ValueError as exc:
        raise ValueError("Triplet values must be integers") from exc


def parse_kpoints_mesh_text(text: str) -> list[int] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 4:
        return None
    values = lines[3].split()
    if len(values) < 3:
        return None
    try:
        return [max(1, int(float(value))) for value in values[:3]]
    except ValueError:
        return None


def sync_metadata_from_kpoints_scf(system_dir: Path, content: str | None = None) -> list[str]:
    metadata_path = system_dir / "metadata.json"
    metadata = read_json(metadata_path, {})
    kpoints_text = content
    if kpoints_text is None:
        kpoints_path = system_dir / "KPOINTS.scf"
        if not kpoints_path.exists():
            return []
        kpoints_text = kpoints_path.read_text(errors="ignore")

    mesh = parse_kpoints_mesh_text(kpoints_text)
    if mesh is None:
        return []
    if mesh == metadata.get("kmesh"):
        return []

    metadata["kmesh"] = mesh
    backup_file(metadata_path)
    write_json(metadata_path, metadata)
    return ["kmesh"]


def _band_conf_value(text: str, key: str) -> str | None:
    target = key.strip().upper()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        current_key, value = line.split("=", 1)
        if current_key.strip().upper() == target:
            return value.strip()
    return None


def parse_band_conf_supercell(text: str) -> list[int] | None:
    raw = _band_conf_value(text, "DIM")
    if raw is None:
        return None
    try:
        return parse_int_triplet(raw)
    except ValueError:
        return None


def parse_band_conf_band_points(text: str) -> int | None:
    raw = _band_conf_value(text, "BAND_POINTS")
    if raw is None:
        return None
    token = raw.split()[0] if raw.split() else ""
    if not token:
        return None
    try:
        return max(20, int(float(token)))
    except ValueError:
        return None


def effective_phonon_supercell(system_dir: Path, metadata: dict[str, Any] | None = None) -> list[int]:
    band_conf_path = system_dir / "band.conf"
    if band_conf_path.exists():
        parsed = parse_band_conf_supercell(band_conf_path.read_text(errors="ignore"))
        if parsed is not None:
            return parsed
    return _default_supercell(metadata or {})


def sync_metadata_from_band_conf(system_dir: Path, content: str | None = None) -> list[str]:
    metadata_path = system_dir / "metadata.json"
    metadata = read_json(metadata_path, {})
    band_conf_text = content
    if band_conf_text is None:
        band_conf_path = system_dir / "band.conf"
        if not band_conf_path.exists():
            return []
        band_conf_text = band_conf_path.read_text(errors="ignore")

    updated_keys: list[str] = []
    supercell = parse_band_conf_supercell(band_conf_text)
    if supercell is not None and supercell != _default_supercell(metadata):
        metadata["phonon_supercell"] = supercell
        updated_keys.append("phonon_supercell")

    band_points = parse_band_conf_band_points(band_conf_text)
    current_band_points = max(20, int(metadata.get("band_points", 101)))
    if band_points is not None and band_points != current_band_points:
        metadata["band_points"] = band_points
        updated_keys.append("band_points")

    if updated_keys:
        backup_file(metadata_path)
        write_json(metadata_path, metadata)
    return updated_keys


def parse_float_sequence(text: str) -> list[float]:
    values = [chunk for chunk in re.split(r"[\s,]+", text.strip()) if chunk]
    try:
        return [float(value) for value in values]
    except ValueError as exc:
        raise ValueError("MAGMOM values must be numeric") from exc


def format_triplet(values: list[int] | tuple[int, int, int] | None) -> str:
    if not values:
        return ""
    return " ".join(str(int(value)) for value in values[:3])


def format_float_list(values: list[float] | list[int] | None) -> str:
    if not values:
        return ""
    return " ".join(f"{float(value):g}" for value in values)


def format_number(value: int | float | None) -> str:
    if value is None:
        return ""
    number = float(value)
    return str(int(number)) if abs(number - round(number)) < 1e-9 else f"{number:g}"


def defaults_for_material_class(material_class: str) -> dict[str, Any]:
    preset_id = MATERIAL_CLASS_DEFAULT_PRESETS.get(str(material_class or "bulk").lower(), "bulk_semiconductor")
    return dict(MATERIAL_PRESETS[preset_id])


def potcar_titles(path: Path) -> list[str]:
    titles: list[str] = []
    pattern = re.compile(r"TITEL\s*=\s*PAW_\S+\s+(\S+)")
    if not path.exists():
        return titles
    for line in path.read_text(errors="ignore").splitlines():
        match = pattern.search(line)
        if match:
            titles.append(match.group(1))
    return titles


@lru_cache(maxsize=None)
def family_potential_labels(family: str) -> tuple[str, ...]:
    archive_path = POTCAR_ARCHIVES.get(family)
    if archive_path is None or not archive_path.exists():
        return ()

    labels: set[str] = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if len(parts) == 2 and parts[1] == "POTCAR":
                labels.add(parts[0])
    return tuple(sorted(labels))


def potential_options_for_species(species: str, family: str) -> list[str]:
    labels = list(family_potential_labels(family))
    if not labels:
        return [species]

    exact = [label for label in labels if label == species]
    variants = [label for label in labels if label.startswith(f"{species}_")]
    gw_variants = [label for label in labels if label.startswith(f"{species}") and label not in exact and label not in variants]
    options = exact + variants + gw_variants
    return options or [species]


def recommended_potential_for_species(species: str, family: str, profile: str = "conservative") -> str:
    options = potential_options_for_species(species, family)
    if profile == "conservative":
        if species in options:
            return species
        preferred = SEMICORE_POTCAR_PREFERENCES.get(species, (species,))
        for candidate in preferred:
            if candidate in options:
                return candidate
        return options[0]

    preferred = SEMICORE_POTCAR_PREFERENCES.get(species, (species,))
    for candidate in preferred:
        if candidate in options:
            return candidate
    if species in options:
        return species
    return options[0]


def default_potcar_mapping(species: list[str], metadata: dict[str, Any], potcar_path: Path) -> dict[str, str]:
    raw_mapping = metadata.get("potcar_mapping")
    if isinstance(raw_mapping, dict):
        return {str(key): str(value) for key, value in raw_mapping.items()}

    raw_symbols = metadata.get("potcar_symbols")
    if isinstance(raw_symbols, list) and len(raw_symbols) == len(species):
        return {element: str(symbol) for element, symbol in zip(species, raw_symbols)}

    titles = potcar_titles(potcar_path)
    if len(titles) == len(species):
        return {element: potential for element, potential in zip(species, titles)}

    family = str(metadata.get("potcar_family") or "PBE_64")
    profile = str(metadata.get("potcar_profile") or "conservative")
    return {element: recommended_potential_for_species(element, family, profile) for element in species}


def merged_potcar_mapping(species: list[str], metadata: dict[str, Any]) -> dict[str, str]:
    family = str(metadata.get("potcar_family") or "PBE_64")
    profile = str(metadata.get("potcar_profile") or "conservative")
    available_labels = set(family_potential_labels(family))
    raw_mapping = metadata.get("potcar_mapping") if isinstance(metadata.get("potcar_mapping"), dict) else {}
    merged: dict[str, str] = {}

    for element in species:
        candidate = str(raw_mapping.get(element) or "").strip()
        if candidate and (not available_labels or candidate in available_labels):
            merged[element] = candidate
            continue
        merged[element] = recommended_potential_for_species(element, family, profile)
    return merged


def mapping_text(mapping: dict[str, str], species_order: list[str]) -> str:
    lines: list[str] = []
    for element in species_order:
        lines.append(f"{element} = {mapping.get(element, element)}")
    return "\n".join(lines)


def parse_mapping_text(text: str, species_order: list[str]) -> dict[str, str]:
    mapping = {element: element for element in species_order}
    if not text.strip():
        return mapping

    for raw_line in text.replace(",", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError("POTCAR mapping must use 'Element = Potential' lines")
        key, value = [part.strip() for part in line.split("=", 1)]
        if key not in mapping:
            raise ValueError(f"Unknown species in mapping: {key}")
        if not value:
            raise ValueError(f"Missing potential label for species: {key}")
        mapping[key] = value
    return mapping


def parse_structure_text(content: str, structure_format: str) -> str:
    text = content.strip()
    if not text:
        raise ValueError("Structure content is empty")

    if structure_format == "poscar":
        if not text.endswith("\n"):
            text += "\n"
        parse_poscar_model(text)
        return text

    if structure_format != "cif":
        raise ValueError(f"Unsupported structure format: {structure_format}")

    try:
        from ase.io import read, write
    except ModuleNotFoundError as exc:
        raise RuntimeError("CIF import requires the 'ase' package in the VASP Input Studio environment") from exc

    def convert_cif(cif_text: str):
        atoms_local = read(StringIO(cif_text), format="cif")
        buffer_local = StringIO()
        write(buffer_local, atoms_local, format="vasp", direct=True, sort=False, vasp5=True)
        return buffer_local.getvalue()

    try:
        poscar_text = convert_cif(text)
    except Exception as exc:
        # Some downloaded CIF files ship with placeholder space-group number 0.
        # ASE rejects these, but treating them as P1 is a practical fallback for import.
        sanitized = re.sub(
            r"(^\s*_(?:space_group_IT_number|symmetry_Int_Tables_number)\s+)(0|\?)\s*$",
            r"\g<1>1",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if "_symmetry_space_group_name_H-M" not in sanitized and "_space_group_name_H-M_alt" not in sanitized:
            sanitized += "\n_symmetry_space_group_name_H-M 'P 1'\n"
        try:
            poscar_text = convert_cif(sanitized)
        except Exception as fallback_exc:
            raise ValueError(f"CIF import failed: {fallback_exc}") from exc

    parse_poscar_model(poscar_text)
    return poscar_text if poscar_text.endswith("\n") else poscar_text + "\n"


def _default_kmesh(metadata: dict[str, Any], fallback: list[int]) -> list[int]:
    raw = metadata.get("kmesh")
    if isinstance(raw, list) and len(raw) == 3:
        return [max(1, int(value)) for value in raw]
    return fallback


def _default_phonon_kmesh(metadata: dict[str, Any], fallback: list[int]) -> list[int]:
    raw = metadata.get("phonon_kmesh")
    if isinstance(raw, list) and len(raw) == 3:
        return [max(1, int(value)) for value in raw]
    return fallback


def _default_phonon_dos_kmesh(metadata: dict[str, Any], fallback: list[int]) -> list[int]:
    raw = metadata.get("phonon_dos_kmesh")
    if isinstance(raw, list) and len(raw) == 3:
        return [max(1, int(value)) for value in raw]
    return fallback


def _default_dos_kmesh(metadata: dict[str, Any], fallback: list[int]) -> list[int]:
    raw = metadata.get("dos_kmesh")
    if isinstance(raw, list) and len(raw) == 3:
        return [max(1, int(value)) for value in raw]
    return fallback


def _default_supercell(metadata: dict[str, Any]) -> list[int]:
    raw = metadata.get("phonon_supercell")
    if isinstance(raw, list) and len(raw) == 3:
        return [max(1, int(value)) for value in raw]
    return [2, 2, 2]


def _default_magmom(metadata: dict[str, Any], species: list[str], counts: list[int]) -> list[float]:
    return default_magmom_values(species, counts, metadata)


def _derived_dos_kmesh_from_kmesh(kmesh: list[int]) -> list[int]:
    return [max(1, min(31, value + 2 if value > 1 else 1)) for value in kmesh]


def _derived_phonon_kmesh_from_kmesh(kmesh: list[int]) -> list[int]:
    return [max(1, (value + 1) // 2) for value in kmesh]


def _derived_phonon_dos_kmesh_from_phonon_kmesh(phonon_kmesh: list[int]) -> list[int]:
    return [max(1, min(31, value * 2 if value > 1 else 1)) for value in phonon_kmesh]


def material_form_payload(system_dir: Path, backend: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = read_json(system_dir / "metadata.json", {})
    poscar = parse_poscar_model((system_dir / "POSCAR").read_text(errors="ignore"))
    species = poscar["species"]
    counts = poscar["counts"]
    total_atoms = sum(counts)
    kmesh = _default_kmesh(metadata, [8, 8, 8])
    dos_kmesh = _default_dos_kmesh(metadata, _derived_dos_kmesh_from_kmesh(kmesh))
    phonon_kmesh = _default_phonon_kmesh(metadata, _derived_phonon_kmesh_from_kmesh(kmesh))
    phonon_dos_kmesh = _default_phonon_dos_kmesh(metadata, _derived_phonon_dos_kmesh_from_phonon_kmesh(phonon_kmesh))
    supercell = _default_supercell(metadata)
    magmom = _default_magmom(metadata, species, counts)
    potcar_mapping = default_potcar_mapping(species, metadata, system_dir / "POTCAR")
    potcar_families = [item.get("label") for item in (backend or {}).get("potcar_families", []) if item.get("label")]
    selected_family = str(metadata.get("potcar_family") or (potcar_families[0] if potcar_families else "PBE_64"))
    selected_profile = str(metadata.get("potcar_profile") or "conservative")
    overrides = metadata.get("incar_overrides") if isinstance(metadata.get("incar_overrides"), dict) else {}
    potcar_options = [
        {
            "species": element,
            "selected": potcar_mapping.get(element, element),
            "recommended": recommended_potential_for_species(element, selected_family, selected_profile),
            "recommended_by_profile": {
                profile: recommended_potential_for_species(element, selected_family, profile)
                for profile in POTCAR_PROFILE_DESCRIPTIONS
            },
            "options": potential_options_for_species(element, selected_family),
        }
        for element in species
    ]

    xc_geometry = str(metadata.get("xc_geometry") or "PBE")
    if xc_geometry not in GEOMETRY_FUNCTIONAL_VALUES:
        xc_geometry = "PBE"
    xc_electronic = str(metadata.get("xc_electronic") or "PBE")
    if xc_electronic not in ELECTRONIC_FUNCTIONAL_VALUES:
        xc_electronic = "PBE"
    encut = metadata.get("encut", 520)
    band_path_text = str(metadata.get("band_path_text") or "")
    band_path_mode = str(metadata.get("band_path_mode") or ("custom" if band_path_text.strip() else "auto")).lower()
    if band_path_mode not in {"auto", "custom"}:
        band_path_mode = "auto"

    return {
        "formula": str(metadata.get("formula") or system_dir.name),
        "species": species,
        "counts": counts,
        "total_atoms": total_atoms,
        "material_class": str(metadata.get("material_class") or "bulk"),
        "electronic_type": str(metadata.get("electronic_type") or "auto"),
        "xc_geometry": xc_geometry,
        "xc_electronic": xc_electronic,
        "xc_geometry_options": list(GEOMETRY_FUNCTIONAL_OPTIONS),
        "xc_electronic_options": list(ELECTRONIC_FUNCTIONAL_OPTIONS),
        "spin_polarized": bool(metadata.get("spin_polarized", any(abs(value) > 1e-9 for value in magmom))),
        "potcar_family": selected_family,
        "potcar_families": potcar_families,
        "potcar_profile": selected_profile,
        "potcar_generation_available": bool(POTCAR_ARCHIVES.get(selected_family) and POTCAR_ARCHIVES[selected_family].exists()),
        "potcar_profiles": [
            {"value": key, "label": key, "description": value}
            for key, value in POTCAR_PROFILE_DESCRIPTIONS.items()
        ],
        "potcar_mapping": potcar_mapping,
        "potcar_mapping_text": mapping_text(potcar_mapping, species),
        "potcar_options": potcar_options,
        "encut": float(encut),
        "encut_text": format_number(encut),
        "kmesh_text": format_triplet(kmesh),
        "dos_kmesh_text": format_triplet(dos_kmesh),
        "phonon_kmesh_text": format_triplet(phonon_kmesh),
        "phonon_dos_kmesh_text": format_triplet(phonon_dos_kmesh),
        "phonon_supercell_text": format_triplet(supercell),
        "magmom_text": format_float_list(magmom),
        "band_points": int(metadata.get("band_points", 101)),
        "band_kpoints_distance": float(metadata.get("band_kpoints_distance", 0.05)),
        "band_path_mode": band_path_mode,
        "band_path_text": band_path_text,
        "wallclock_seconds": int(metadata.get("wallclock_seconds", 12 * 3600)),
        "advanced_overrides_json": json.dumps(overrides, indent=2, ensure_ascii=False) if overrides else "",
        "source_template": metadata.get("source_template"),
        "presets": [
            {
                "id": key,
                **value,
                "encut": float(value.get("encut", 520)),
                "encut_text": format_number(value.get("encut", 520)),
                "kmesh_text": format_triplet(value["kmesh"]),
                "dos_kmesh_text": format_triplet(_derived_dos_kmesh_from_kmesh(value["kmesh"])),
                "phonon_kmesh_text": format_triplet(value["phonon_kmesh"]),
                "phonon_dos_kmesh_text": format_triplet(value.get("phonon_dos_kmesh") or _derived_phonon_dos_kmesh_from_phonon_kmesh(value["phonon_kmesh"])),
                "phonon_supercell_text": format_triplet(value["phonon_supercell"]),
                "magmom_text": format_float_list(
                    default_magmom_values(
                        species,
                        counts,
                        {
                            "spin_polarized": value.get("spin_polarized", False),
                            "magmom": value.get("magmom"),
                        },
                    )
                ),
                "xc_geometry": value.get("xc_geometry", "PBE"),
                "xc_electronic": value.get("xc_electronic", "PBE"),
            }
            for key, value in MATERIAL_PRESETS.items()
        ],
    }


def build_potcar_bytes(system_dir: Path, family: str, mapping: dict[str, str]) -> bytes:
    archive_path = POTCAR_ARCHIVES.get(family)
    if archive_path is None:
        raise ValueError(f"Local POTCAR generation currently supports only: {', '.join(sorted(POTCAR_ARCHIVES))}")
    if not archive_path.exists():
        raise ValueError(f"Missing local POTCAR archive: {archive_path}")

    poscar = parse_poscar_model((system_dir / "POSCAR").read_text(errors="ignore"))
    species = poscar["species"]
    potentials = [mapping.get(element, element) for element in species]
    output_path = system_dir / "POTCAR"

    parts: list[bytes] = []
    with tarfile.open(archive_path, "r:gz") as archive:
        for symbol in potentials:
            member = archive.getmember(f"{symbol}/POTCAR")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"Failed to extract POTCAR for {symbol}")
            parts.append(handle.read())

    return b"".join(parts)


def regenerate_potcar(system_dir: Path, family: str, mapping: dict[str, str]) -> Path:
    output_path = system_dir / "POTCAR"
    atomic_write_bytes(output_path, build_potcar_bytes(system_dir, family, mapping))
    return output_path


def regenerate_potcar_if_available(system_dir: Path, family: str, mapping: dict[str, str]) -> Path | None:
    archive_path = POTCAR_ARCHIVES.get(family)
    if archive_path is None or not archive_path.exists():
        return None
    return regenerate_potcar(system_dir, family, mapping)


def regenerate_project_inputs(system_dir: Path, *, include_incar: bool = False) -> list[str]:
    generated: list[str] = []
    generated_contents: dict[str, str] = {}
    paths = list(GENERATED_FILE_CANDIDATES)
    if include_incar:
        paths.extend(INCAR_TEMPLATE_FILES)

    for relative_path in paths:
        payload = generate_input_content(system_dir, relative_path)
        content = payload["content"]
        atomic_write_text(system_dir / relative_path, content)
        generated_contents[relative_path] = content
        generated.append(relative_path)
    record_generated_files(system_dir, generated_contents)
    return generated


def planned_generated_inputs(
    system_dir: Path,
    metadata: dict[str, Any],
    relative_paths: list[str] | tuple[str, ...],
) -> dict[str, str]:
    planned: dict[str, str] = {}
    for relative_path in relative_paths:
        payload = generate_input_content(
            system_dir,
            relative_path,
            metadata=metadata,
            planned_inputs=planned,
        )
        planned[relative_path] = payload["content"]
    return planned


def write_generated_inputs(system_dir: Path, generated_contents: dict[str, str]) -> list[str]:
    for relative_path, content in generated_contents.items():
        atomic_write_text(system_dir / relative_path, content)
    record_generated_files(system_dir, generated_contents)
    return list(generated_contents)


def save_material_settings(system_dir: Path, payload: dict[str, Any], backend: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata_path = system_dir / "metadata.json"
    current_metadata = read_json(metadata_path, {})
    poscar = parse_poscar_model((system_dir / "POSCAR").read_text(errors="ignore"))
    species = poscar["species"]
    counts = poscar["counts"]
    total_atoms = sum(counts)

    formula = str(payload.get("formula") or system_dir.name).strip() or system_dir.name
    potcar_families = [item.get("label") for item in (backend or {}).get("potcar_families", []) if item.get("label")]
    potcar_family = str(payload.get("potcar_family") or current_metadata.get("potcar_family") or (potcar_families[0] if potcar_families else "PBE_64"))
    potcar_profile = str(payload.get("potcar_profile") or current_metadata.get("potcar_profile") or "conservative")
    xc_geometry = str(payload.get("xc_geometry") or current_metadata.get("xc_geometry") or "PBE")
    if xc_geometry not in GEOMETRY_FUNCTIONAL_VALUES:
        xc_geometry = "PBE"
    xc_electronic = str(payload.get("xc_electronic") or current_metadata.get("xc_electronic") or "PBE")
    if xc_electronic not in ELECTRONIC_FUNCTIONAL_VALUES:
        xc_electronic = "PBE"
    try:
        encut = float(payload.get("encut") or current_metadata.get("encut") or 520)
    except (TypeError, ValueError) as exc:
        raise ValueError("ENCUT must be numeric") from exc

    kmesh = parse_int_triplet(str(payload.get("kmesh_text") or ""), fallback=_default_kmesh(current_metadata, [8, 8, 8]))
    dos_kmesh = parse_int_triplet(
        str(payload.get("dos_kmesh_text") or ""),
        fallback=_default_dos_kmesh(current_metadata, _derived_dos_kmesh_from_kmesh(kmesh)),
    )
    phonon_kmesh = parse_int_triplet(
        str(payload.get("phonon_kmesh_text") or ""),
        fallback=_default_phonon_kmesh(current_metadata, _derived_phonon_kmesh_from_kmesh(kmesh)),
    )
    phonon_dos_kmesh = parse_int_triplet(
        str(payload.get("phonon_dos_kmesh_text") or ""),
        fallback=_default_phonon_dos_kmesh(current_metadata, _derived_phonon_dos_kmesh_from_phonon_kmesh(phonon_kmesh)),
    )
    phonon_supercell = parse_int_triplet(str(payload.get("phonon_supercell_text") or ""), fallback=_default_supercell(current_metadata))
    band_points = max(20, int(payload.get("band_points") or current_metadata.get("band_points", 101)))
    band_kpoints_distance = float(payload.get("band_kpoints_distance") or current_metadata.get("band_kpoints_distance", 0.05))
    wallclock_seconds = max(3600, int(payload.get("wallclock_seconds") or current_metadata.get("wallclock_seconds", 12 * 3600)))
    band_path_mode = str(payload.get("band_path_mode") or current_metadata.get("band_path_mode") or "auto").strip().lower()
    if band_path_mode not in {"auto", "custom"}:
        band_path_mode = "auto"
    raw_band_path_text = str(payload.get("band_path_text") or "")
    band_path_text = ""
    if band_path_mode == "custom":
        band_path_text = normalize_custom_kpath_text(raw_band_path_text, band_points)

    magmom_values = parse_float_sequence(str(payload.get("magmom_text") or ""))
    if len(magmom_values) not in {len(species), total_atoms}:
        raise ValueError(f"MAGMOM must contain either {len(species)} species values or {total_atoms} atom values")

    mapping = parse_mapping_text(str(payload.get("potcar_mapping_text") or ""), species)
    overrides_text = str(payload.get("advanced_overrides_json") or "").strip()
    if overrides_text:
        try:
            overrides = json.loads(overrides_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid advanced overrides JSON: {exc.msg}") from exc
        if not isinstance(overrides, dict):
            raise ValueError("Advanced overrides must be a JSON object")
    else:
        overrides = {}

    metadata = dict(current_metadata)
    metadata.update(
        {
            "formula": formula,
            "material_class": str(payload.get("material_class") or "bulk"),
            "electronic_type": str(payload.get("electronic_type") or "auto"),
            "xc_geometry": xc_geometry,
            "xc_electronic": xc_electronic,
            "spin_polarized": bool(payload.get("spin_polarized")),
            "encut": encut,
            "potcar_family": potcar_family,
            "potcar_profile": potcar_profile,
            "potcar_mapping": mapping,
            "potcar_symbols": [mapping.get(element, element) for element in species],
            "kmesh": kmesh,
            "dos_kmesh": dos_kmesh,
            "phonon_kmesh": phonon_kmesh,
            "phonon_dos_kmesh": phonon_dos_kmesh,
            "phonon_supercell": phonon_supercell,
            "magmom": magmom_values,
            "band_points": band_points,
            "band_kpoints_distance": band_kpoints_distance,
            "band_path_mode": band_path_mode,
            "wallclock_seconds": wallclock_seconds,
        }
    )
    if band_path_mode == "custom":
        metadata["band_path_text"] = band_path_text
    else:
        metadata.pop("band_path_text", None)
    if overrides:
        metadata["incar_overrides"] = overrides
    else:
        metadata.pop("incar_overrides", None)

    planned_potcar = None
    if POTCAR_ARCHIVES.get(potcar_family) and POTCAR_ARCHIVES[potcar_family].exists():
        planned_potcar = build_potcar_bytes(system_dir, potcar_family, mapping)
    generated_contents = planned_generated_inputs(
        system_dir,
        metadata,
        [*GENERATED_FILE_CANDIDATES, *INCAR_TEMPLATE_FILES],
    )

    backup_file(metadata_path)
    write_json(metadata_path, metadata)
    potcar_path = None
    if planned_potcar is not None:
        potcar_path = system_dir / "POTCAR"
        atomic_write_bytes(potcar_path, planned_potcar)
    generated = write_generated_inputs(system_dir, generated_contents)

    return {
        "material_settings": material_form_payload(system_dir, backend),
        "metadata": metadata,
        "generated_files": generated,
        "potcar_path": str(potcar_path) if potcar_path else None,
    }


def initialize_project_inputs(system_dir: Path, *, formula: str | None = None) -> dict[str, Any]:
    metadata = read_json(system_dir / "metadata.json", {})
    poscar = parse_poscar_model((system_dir / "POSCAR").read_text(errors="ignore"))
    species = poscar["species"]
    counts = poscar["counts"]
    total_atoms = sum(counts)

    if not metadata.get("formula"):
        metadata["formula"] = formula or system_dir.name
    metadata.setdefault("material_class", "bulk")
    metadata.setdefault("electronic_type", "auto")
    metadata.setdefault("xc_geometry", "PBE")
    metadata.setdefault("xc_electronic", "PBE")
    metadata.setdefault("spin_polarized", False)
    metadata.setdefault("encut", 520)
    metadata.setdefault("potcar_family", "PBE_64")
    metadata.setdefault("potcar_profile", "conservative")
    metadata.setdefault("kmesh", [8, 8, 8])
    metadata.setdefault("dos_kmesh", _derived_dos_kmesh_from_kmesh([max(1, int(value)) for value in metadata["kmesh"]]))
    metadata.setdefault("phonon_kmesh", _derived_phonon_kmesh_from_kmesh([max(1, int(value)) for value in metadata["kmesh"]]))
    metadata.setdefault("phonon_dos_kmesh", _derived_phonon_dos_kmesh_from_phonon_kmesh([max(1, int(value)) for value in metadata["phonon_kmesh"]]))
    metadata.setdefault("phonon_supercell", [2, 2, 2])
    metadata.setdefault("band_points", 101)
    metadata.setdefault("band_kpoints_distance", 0.05)
    metadata.setdefault("band_path_mode", "auto")
    metadata.setdefault("wallclock_seconds", 12 * 3600)
    metadata["potcar_mapping"] = merged_potcar_mapping(species, metadata)
    metadata["potcar_symbols"] = [metadata["potcar_mapping"][element] for element in species]

    raw_magmom = metadata.get("magmom")
    if not isinstance(raw_magmom, list) or len(raw_magmom) not in {len(species), total_atoms}:
        metadata["magmom"] = default_magmom_values(species, counts, metadata)

    planned_potcar = None
    family = str(metadata.get("potcar_family") or "PBE_64")
    mapping = metadata["potcar_mapping"]
    if POTCAR_ARCHIVES.get(family) and POTCAR_ARCHIVES[family].exists():
        planned_potcar = build_potcar_bytes(system_dir, family, mapping)
    generated_contents = planned_generated_inputs(system_dir, metadata, [*GENERATED_FILE_CANDIDATES, *INCAR_TEMPLATE_FILES])

    write_json(system_dir / "metadata.json", metadata)
    potcar_path = None
    if planned_potcar is not None:
        potcar_path = system_dir / "POTCAR"
        atomic_write_bytes(potcar_path, planned_potcar)
    generated = write_generated_inputs(system_dir, generated_contents)
    return {"potcar_path": str(potcar_path) if potcar_path else None, "generated_files": generated}


def refresh_structure_inputs(system_dir: Path) -> dict[str, Any]:
    metadata = read_json(system_dir / "metadata.json", {})
    poscar = parse_poscar_model((system_dir / "POSCAR").read_text(errors="ignore"))
    species = poscar["species"]
    counts = poscar["counts"]
    total_atoms = sum(counts)

    metadata["potcar_mapping"] = merged_potcar_mapping(species, metadata)
    metadata["potcar_symbols"] = [metadata["potcar_mapping"][element] for element in species]
    metadata.setdefault("xc_geometry", "PBE")
    metadata.setdefault("xc_electronic", "PBE")
    metadata.setdefault("encut", 520)
    metadata.setdefault("band_path_mode", "auto")

    raw_magmom = metadata.get("magmom")
    if not isinstance(raw_magmom, list) or len(raw_magmom) not in {len(species), total_atoms}:
        metadata["magmom"] = default_magmom_values(species, counts, metadata)

    managed_existing = {
        relative_path
        for relative_path in MANAGED_FILE_CANDIDATES
        if (system_dir / relative_path).exists()
    }
    managed_states = {
        relative_path: file_generation_state(system_dir, relative_path)
        for relative_path in managed_existing
    }
    files_to_refresh = [
        relative_path
        for relative_path in MANAGED_FILE_CANDIDATES
        if relative_path not in managed_existing
        or (
            managed_states[relative_path]["tracked"]
            and not managed_states[relative_path]["manual_override"]
        )
    ]
    generated_contents = planned_generated_inputs(system_dir, metadata, files_to_refresh)
    planned_potcar = None
    family = str(metadata.get("potcar_family") or "PBE_64")
    mapping = metadata["potcar_mapping"]
    if POTCAR_ARCHIVES.get(family) and POTCAR_ARCHIVES[family].exists():
        planned_potcar = build_potcar_bytes(system_dir, family, mapping)

    backup_file(system_dir / "metadata.json")
    write_json(system_dir / "metadata.json", metadata)
    potcar_path = None
    if planned_potcar is not None:
        potcar_path = system_dir / "POTCAR"
        atomic_write_bytes(potcar_path, planned_potcar)
    generated = write_generated_inputs(system_dir, generated_contents)
    return {"potcar_path": str(potcar_path) if potcar_path else None, "generated_files": generated}
