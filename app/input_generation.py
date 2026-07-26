from __future__ import annotations

from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

from .config import settings
from .magnetism import default_magmom_values
from .structure_resolution import (
    generated_input_structure_path,
    relax_contcar_path,
    relax_primitive_path,
    root_structure_path,
)


COMMON_DIR = settings.incar_template_dir
INCAR_TEMPLATE_MAP = {
    "INCAR.relax": "INCAR.relax.template",
    "INCAR.scf": "INCAR.scf.template",
    "INCAR.dos": "INCAR.dos.template",
    "INCAR.converge": "INCAR.converge.template",
    "INCAR.band": "INCAR.band.template",
    "INCAR.elastic": "INCAR.elastic.template",
    "INCAR.charge": "INCAR.charge.template",
    "INCAR.phonon": "INCAR.phonon.template",
}

GEOMETRY_FUNCTIONALS = {"PBE", "PBEsol"}
ELECTRONIC_FUNCTIONALS = {"PBE", "HSE06"}


def configured_vaspkit_binary() -> Path | None:
    override = os.environ.get("VASPKIT_CMD")
    if override:
        return Path(override).expanduser()
    return settings.vaspkit_cmd


VASPKIT_BINARY = configured_vaspkit_binary()
DEFAULT_ENCUT = 520.0


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def norm(vector: list[float]) -> float:
    return math.sqrt(dot(vector, vector))


def reciprocal_lattice(lattice: list[list[float]]) -> list[list[float]]:
    a_vec, b_vec, c_vec = lattice
    volume = dot(a_vec, cross(b_vec, c_vec))
    if abs(volume) < 1e-12:
        raise ValueError("Lattice vectors are singular")
    factor = 2 * math.pi / volume
    return [
        [value * factor for value in cross(b_vec, c_vec)],
        [value * factor for value in cross(c_vec, a_vec)],
        [value * factor for value in cross(a_vec, b_vec)],
    ]


def parse_poscar_model(text: str) -> dict[str, Any]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) < 8:
        raise ValueError("POSCAR content is incomplete")

    title = lines[0].strip()
    scale = float(lines[1].split()[0])
    lattice = [[float(value) * scale for value in lines[idx].split()[:3]] for idx in range(2, 5)]
    species = lines[5].split()
    counts = [int(value) for value in lines[6].split()]

    line_idx = 7
    if lines[line_idx].lower().startswith("s"):
        line_idx += 1

    coord_mode = lines[line_idx].strip().lower()
    line_idx += 1
    total_atoms = sum(counts)
    raw_positions = [[float(value) for value in lines[line_idx + idx].split()[:3]] for idx in range(total_atoms)]

    expanded_species: list[str] = []
    for element, count in zip(species, counts):
        expanded_species.extend([element] * count)

    recip = reciprocal_lattice(lattice)
    atoms: list[dict[str, Any]] = []
    fractional_positions: list[list[float]] = []
    cart_positions: list[list[float]] = []
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
            frac = [dot(cart, vector) / (2 * math.pi) for vector in recip]

        fractional_positions.append(frac)
        cart_positions.append(cart)
        atoms.append({"element": element, "cartesian": cart, "fractional": frac})

    return {
        "title": title,
        "lattice": lattice,
        "species": species,
        "counts": counts,
        "atoms": atoms,
        "fractional_positions": fractional_positions,
        "cartesian_positions": cart_positions,
    }


def parse_kpoints_mesh(path: Path) -> list[int] | None:
    if not path.exists():
        return None
    lines = [line.strip() for line in path.read_text(errors="ignore").splitlines() if line.strip()]
    if len(lines) < 4:
        return None
    candidate = lines[3].split()
    if len(candidate) < 3:
        return None
    try:
        return [max(1, int(float(value))) for value in candidate[:3]]
    except ValueError:
        return None


def mesh_from_lattice(lattice: list[list[float]], target_product: float = 64.0) -> list[int]:
    lengths = [max(norm(vector), 1e-6) for vector in lattice]
    mesh: list[int] = []
    for length in lengths:
        value = int(round(target_product / length))
        mesh.append(max(1, min(31, value)))
    return mesh


def infer_scf_mesh(
    system_dir: Path,
    metadata: dict[str, Any],
    poscar: dict[str, Any],
    *,
    existing_path: Path | None = None,
    allow_existing: bool = True,
) -> list[int]:
    mesh = metadata.get("kmesh")
    if isinstance(mesh, list) and len(mesh) == 3:
        return [max(1, int(value)) for value in mesh]

    if allow_existing:
        existing = parse_kpoints_mesh(existing_path or (system_dir / "KPOINTS.scf"))
        if existing:
            return existing

    return mesh_from_lattice(poscar["lattice"])


def infer_phonon_mesh(
    system_dir: Path,
    metadata: dict[str, Any],
    poscar: dict[str, Any],
    *,
    allow_existing: bool = True,
) -> list[int]:
    mesh = metadata.get("phonon_kmesh")
    if isinstance(mesh, list) and len(mesh) == 3:
        return [max(1, int(value)) for value in mesh]

    if allow_existing:
        existing = parse_kpoints_mesh(system_dir / "KPOINTS.phonon")
        if existing:
            return existing

    scf_mesh = infer_scf_mesh(system_dir, metadata, poscar, allow_existing=allow_existing)
    return [max(1, (value + 1) // 2) for value in scf_mesh]


def infer_dos_mesh(
    system_dir: Path,
    metadata: dict[str, Any],
    poscar: dict[str, Any],
    *,
    allow_existing: bool = True,
) -> list[int]:
    mesh = metadata.get("dos_kmesh")
    if isinstance(mesh, list) and len(mesh) == 3:
        return [max(1, int(value)) for value in mesh]

    if allow_existing:
        existing = parse_kpoints_mesh(system_dir / "KPOINTS.dos")
        if existing:
            return existing

    scf_mesh = infer_scf_mesh(system_dir, metadata, poscar, allow_existing=allow_existing)
    return [max(1, min(31, value + 2 if value > 1 else 1)) for value in scf_mesh]


def infer_phonon_supercell(metadata: dict[str, Any]) -> list[int]:
    dim = metadata.get("phonon_supercell")
    if isinstance(dim, list) and len(dim) == 3:
        return [max(1, int(value)) for value in dim]
    return [2, 2, 2]


def supercell_repeat(dim: list[int]) -> int:
    repeat = 1
    for value in dim:
        repeat *= max(1, int(value))
    return repeat


def kpoints_mesh_text(mesh: list[int]) -> str:
    return f"""Automatic mesh
0
Gamma
{mesh[0]} {mesh[1]} {mesh[2]}
0 0 0
"""


def _parse_fractional_token(token: str) -> float:
    piece = token.strip()
    if "/" in piece and not any(ch in piece for ch in ".eE"):
        return float(Fraction(piece))
    return float(piece)


def _parse_line_mode_segments(text: str) -> list[list[list[float]]]:
    lines = text.splitlines()
    if len(lines) < 5:
        return []

    segments: list[list[list[float]]] = []
    current: list[list[float]] = []
    for raw_line in lines[4:]:
        stripped = raw_line.strip()
        if not stripped:
            if len(current) >= 2:
                segments.append(current)
            current = []
            continue

        tokens = re.split(r"\s+", stripped)
        if len(tokens) < 3:
            continue
        try:
            coords = [_parse_fractional_token(tokens[0]), _parse_fractional_token(tokens[1]), _parse_fractional_token(tokens[2])]
        except ValueError:
            continue
        current.append(coords)

    if len(current) >= 2:
        segments.append(current)
    return segments


def _format_qpoint(value: float) -> str:
    return f"{value:.8f}"


def band_conf_text(species: list[str], band_paths: list[list[list[float]]], dim: list[int] | None = None, band_points: int = 101) -> str:
    supercell = dim or [2, 2, 2]
    points = max(20, int(band_points))
    atom_name = " ".join(species)
    band_value = ", ".join(
        "  ".join(" ".join(_format_qpoint(component) for component in point) for point in segment)
        for segment in band_paths
    )
    return f"""ATOM_NAME = {atom_name}
DIM = {supercell[0]} {supercell[1]} {supercell[2]}
PRIMITIVE_AXES = AUTO
BAND = {band_value}
BAND_POINTS = {points}
BAND_CONNECTION = .TRUE.
"""


def formula_label(system_dir: Path, metadata: dict[str, Any]) -> str:
    return str(metadata.get("formula") or system_dir.name)


def default_magmom(species: list[str], counts: list[int], metadata: dict[str, Any]) -> list[float]:
    return default_magmom_values(species, counts, metadata)


def magmom_string(values: list[float]) -> str:
    return " ".join(f"{value:g}" for value in values)


def format_incar_value(value: Any) -> str:
    if isinstance(value, bool):
        return ".TRUE." if value else ".FALSE."
    if isinstance(value, list):
        return " ".join(format_incar_value(item) for item in value)
    return str(value)


def normalized_geometry_functional(metadata: dict[str, Any]) -> str:
    value = str(metadata.get("xc_geometry") or "PBE").strip()
    return value if value in GEOMETRY_FUNCTIONALS else "PBE"


def normalized_electronic_functional(metadata: dict[str, Any]) -> str:
    value = str(metadata.get("xc_electronic") or "PBE").strip()
    return value if value in ELECTRONIC_FUNCTIONALS else "PBE"


def default_encut(metadata: dict[str, Any]) -> float:
    raw = metadata.get("encut")
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_ENCUT


def encut_string(metadata: dict[str, Any]) -> str:
    value = default_encut(metadata)
    return str(int(value)) if abs(value - round(value)) < 1e-9 else f"{value:g}"


def incar_overrides(metadata: dict[str, Any], path_name: str) -> dict[str, Any]:
    raw = metadata.get("incar_overrides")
    if not isinstance(raw, dict):
        return {}

    step_name = path_name.split(".", 1)[1] if "." in path_name else path_name
    merged: dict[str, Any] = {}
    for key in (path_name, step_name):
        candidate = raw.get(key)
        if isinstance(candidate, dict):
            merged.update(candidate)
    return merged


def material_strategy_overrides(path_name: str, metadata: dict[str, Any], poscar: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    species = poscar["species"]
    counts = poscar["counts"]
    total_atoms = sum(counts)
    notes: list[str] = []
    overrides: dict[str, Any] = {}

    spin_polarized = bool(metadata.get("spin_polarized", False))
    electronic_type = str(metadata.get("electronic_type") or "auto").lower()
    material_class = str(metadata.get("material_class") or "bulk").lower()

    geometry_steps = {"INCAR.relax", "INCAR.converge", "INCAR.elastic", "INCAR.charge", "INCAR.phonon"}
    electronic_steps = {"INCAR.scf", "INCAR.dos", "INCAR.band"}

    if spin_polarized:
        overrides["ISPIN"] = 2
        mm = default_magmom(species, counts, metadata)
        if len(mm) == total_atoms:
            if path_name == "INCAR.phonon":
                mm = mm * supercell_repeat(infer_phonon_supercell(metadata))
            overrides["MAGMOM"] = mm
        notes.append("Applied spin-polarized defaults (ISPIN=2, MAGMOM from material settings).")
    else:
        overrides["ISPIN"] = 1
        notes.append("Applied non-spin-polarized defaults (ISPIN=1).")

    if electronic_type == "metal":
        overrides["ISMEAR"] = 1
        overrides["SIGMA"] = 0.2
        notes.append("Applied metallic smearing defaults (ISMEAR=1, SIGMA=0.2).")
    elif electronic_type in {"semiconductor", "insulator"}:
        if path_name == "INCAR.dos" and material_class == "bulk":
            overrides["ISMEAR"] = -5
            notes.append("Applied tetrahedron DOS defaults for gapped bulk system (ISMEAR=-5).")
        else:
            overrides["ISMEAR"] = 0
            overrides["SIGMA"] = 0.05
            notes.append("Applied semiconducting/insulating defaults (ISMEAR=0, SIGMA=0.05).")
    else:
        overrides["ISMEAR"] = 0
        overrides["SIGMA"] = 0.05
        notes.append("Applied safe auto smearing defaults (ISMEAR=0, SIGMA=0.05).")

    if material_class in {"2d", "slab"}:
        overrides["ISYM"] = 0
        notes.append("Applied slab/2D symmetry defaults (ISYM=0).")
        if path_name == "INCAR.relax":
            overrides["ISIF"] = 2
            notes.append("Applied slab/2D relax default (ISIF=2) so the vacuum direction is not relaxed implicitly.")
        if path_name == "INCAR.elastic":
            overrides["ISIF"] = 2
            notes.append("Built-in elastic workflow is bulk-oriented. For slab/2D, keep ISIF=2 and switch to a custom in-plane strain workflow instead of interpreting 3D vacuum-dependent elastic constants.")
        if bool(metadata.get("has_dipole", False)):
            overrides["LDIPOL"] = True
            overrides["IDIPOL"] = 3
            notes.append("Applied dipole correction for slab/2D system (LDIPOL=.TRUE., IDIPOL=3).")
    elif material_class == "molecule":
        overrides["ISYM"] = 0
        overrides["ISMEAR"] = 0
        overrides["SIGMA"] = 0.01
        if path_name == "INCAR.relax":
            overrides["ISIF"] = 2
        if bool(metadata.get("has_dipole", False)):
            overrides["LDIPOL"] = True
            overrides["IDIPOL"] = 4
        notes.append("Applied molecular defaults (ISYM=0, ISMEAR=0, SIGMA=0.01, relax ISIF=2).")
    else:
        overrides["ISYM"] = 2
        notes.append("Applied bulk symmetry defaults (ISYM=2).")

    geometry_functional = normalized_geometry_functional(metadata)
    electronic_functional = normalized_electronic_functional(metadata)

    if path_name in geometry_steps:
        if geometry_functional == "PBEsol":
            overrides["GGA"] = "PS"
            notes.append("Applied PBEsol geometry defaults (GGA=PS).")
        else:
            overrides["GGA"] = "PE"
            notes.append("Applied PBE geometry defaults (GGA=PE).")

    if path_name in electronic_steps:
        if electronic_functional == "HSE06":
            overrides.update(
                {
                    "GGA": "PE",
                    "LHFCALC": True,
                    "HFSCREEN": 0.2,
                    "AEXX": 0.25,
                    "LASPH": True,
                    "PRECFOCK": "Fast",
                    "ALGO": "Damped",
                    "TIME": 0.4,
                }
            )
            notes.append("Applied HSE06 defaults (LHFCALC, HFSCREEN=0.2, AEXX=0.25).")
            if path_name == "INCAR.band":
                overrides["ISTART"] = 1
                overrides["ICHARG"] = 1
                overrides["HFRCUT"] = -1
                notes.append("Applied HSE band restart defaults (ISTART=1, ICHARG=1, HFRCUT=-1).")
        else:
            overrides["GGA"] = "PE"
            notes.append("Applied PBE electronic defaults (GGA=PE).")

    if path_name == "INCAR.phonon":
        overrides["ISYM"] = 0
        overrides["IBRION"] = -1
        overrides["NSW"] = 0
        overrides["LREAL"] = False
        overrides["EDIFF"] = 1e-8
        notes.append("Applied finite-displacement phonon force defaults (ISYM=0, IBRION=-1, NSW=0, LREAL=.FALSE., EDIFF=1E-8).")
        if electronic_type != "metal":
            overrides["ISMEAR"] = 0
            overrides["SIGMA"] = 0.01
            notes.append("Applied phonon smearing defaults for non-metals (ISMEAR=0, SIGMA=0.01).")

    if metadata.get("use_vdw", False):
        overrides["IVDW"] = 12
        notes.append("Applied vdW correction hook (IVDW=12).")

    if metadata.get("use_ldau", False):
        overrides["LDAU"] = True
        overrides["LMAXMIX"] = metadata.get("lmaxmix", 4)
        notes.append("Applied DFT+U hook. Confirm LDAU* parameters and LMAXMIX manually.")

    if metadata.get("use_soc", False):
        overrides["LSORBIT"] = True
        overrides["LNONCOLLINEAR"] = True
        overrides["ISYM"] = 0
        notes.append("Applied SOC hook. Confirm vasp_ncl workflow manually.")

    return overrides, notes


def apply_incar_overrides(content: str, overrides: dict[str, Any]) -> str:
    if not overrides:
        return content

    rendered: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("#"):
            rendered.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        if key in overrides:
            rendered.append(f"{key} = {format_incar_value(overrides[key])}")
            seen.add(key)
        else:
            rendered.append(line)

    for key, value in overrides.items():
        if key not in seen:
            rendered.append(f"{key} = {format_incar_value(value)}")

    return "\n".join(rendered) + "\n"


def render_incar(path_name: str, system_dir: Path, metadata: dict[str, Any], poscar: dict[str, Any]) -> tuple[str, list[str]]:
    template_name = INCAR_TEMPLATE_MAP[path_name]
    template = (COMMON_DIR / template_name).read_text(encoding="utf-8")
    system_label = formula_label(system_dir, metadata)
    magmom = default_magmom(poscar["species"], poscar["counts"], metadata)
    supercell = infer_phonon_supercell(metadata)
    atom_magmom = metadata.get("atom_magmom", 0)

    rendered = template.replace("__SYSTEM__", system_label)
    rendered = rendered.replace("__ENCUT__", encut_string(metadata))
    rendered = rendered.replace("__MAGMOM__", magmom_string(magmom))
    rendered = rendered.replace("__MAGMOM_SUPERCELL__", magmom_string(magmom * supercell_repeat(supercell)))
    rendered = rendered.replace("__ATOM_MAGMOM__", format_incar_value(atom_magmom))
    strategy_overrides, strategy_notes = material_strategy_overrides(path_name, metadata, poscar)
    rendered = apply_incar_overrides(rendered, strategy_overrides)
    overrides = incar_overrides(metadata, path_name)
    rendered = apply_incar_overrides(rendered, overrides)

    notes = [
        f"Generated default {path_name} from the bundled neutral template set.",
        "Review ENCUT, smearing, and MAGMOM before production runs.",
    ]
    notes.extend(strategy_notes)
    if overrides:
        notes.append("Applied project-specific INCAR overrides from metadata.json.")
    if metadata.get("spin_polarized") and all(value == 0 for value in magmom):
        notes.append("Spin-polarized mode currently uses zero MAGMOM; set element- or atom-resolved moments if needed.")
    return rendered if rendered.endswith("\n") else rendered + "\n", notes


def display_k_label(label: str) -> str:
    return "G" if label == "GAMMA" else label


def seekpath_kpath_text(poscar: dict[str, Any]) -> str:
    import seekpath

    species_numbers = {symbol: index + 1 for index, symbol in enumerate(poscar["species"])}
    numbers: list[int] = []
    for symbol, count in zip(poscar["species"], poscar["counts"]):
        numbers.extend([species_numbers[symbol]] * count)

    path_data = seekpath.get_path((poscar["lattice"], poscar["fractional_positions"], numbers))
    lines = [
        "K-Path Generated by VASP Input Studio (SeekPath)",
        "40",
        "Line-mode",
        "Reciprocal",
    ]
    for start, end in path_data["path"]:
        start_coord = path_data["point_coords"][start]
        end_coord = path_data["point_coords"][end]
        lines.append(f"{start_coord[0]:.8f} {start_coord[1]:.8f} {start_coord[2]:.8f} ! {display_k_label(start)}")
        lines.append(f"{end_coord[0]:.8f} {end_coord[1]:.8f} {end_coord[2]:.8f} ! {display_k_label(end)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def rewrite_line_mode_points(text: str, line_points: int) -> str:
    lines = text.splitlines()
    if len(lines) >= 2:
        lines[1] = str(max(20, int(line_points)))
    return "\n".join(lines).rstrip() + "\n"


def normalize_custom_kpath_text(text: str, line_points: int) -> str:
    normalized = rewrite_line_mode_points(text, line_points)
    if not _parse_line_mode_segments(normalized):
        raise ValueError("Custom band path must be a valid line-mode KPOINTS/KPATH definition")
    return normalized


def vaspkit_band_task(material_class: str) -> str | None:
    normalized = str(material_class or "bulk").lower()
    if normalized == "bulk":
        return "303"
    if normalized in {"2d", "slab"}:
        return "302"
    return None


def _relative_structure_label(system_dir: Path, structure_path: Path) -> str:
    try:
        return structure_path.relative_to(system_dir).as_posix()
    except ValueError:
        return structure_path.as_posix()


def _structure_source_note(system_dir: Path, structure_path: Path) -> str:
    if structure_path == relax_primitive_path(system_dir):
        return "Using runs/relax/PRIMCELL.vasp as the downstream structure source."
    if structure_path == relax_contcar_path(system_dir):
        return "Using runs/relax/CONTCAR as the latest relaxed structure source."
    return f"Using {_relative_structure_label(system_dir, structure_path)} as the structure source."


def generate_kpath_text(
    system_dir: Path,
    metadata: dict[str, Any],
    poscar: dict[str, Any],
    line_points: int,
    *,
    structure_source: Path,
) -> tuple[str, list[str]]:
    band_path_mode = str(metadata.get("band_path_mode") or "").strip().lower()
    custom_band_path = str(metadata.get("band_path_text") or "").strip()
    if band_path_mode == "custom":
        if not custom_band_path:
            raise ValueError("Band path mode is set to custom, but no custom band path was saved")
        normalized = normalize_custom_kpath_text(custom_band_path, line_points)
        return normalized, ["Using the custom line-mode band path saved in Material Parameters."]

    material_class = str(metadata.get("material_class") or "bulk").lower()
    task = vaspkit_band_task(material_class)
    notes: list[str] = []

    if task and VASPKIT_BINARY and VASPKIT_BINARY.exists():
        try:
            with TemporaryDirectory(prefix="vasp-studio-kpath-") as tmp_dir:
                tmp_path = Path(tmp_dir)
                (tmp_path / "POSCAR").write_text(structure_source.read_text(errors="ignore"), encoding="utf-8")
                completed = subprocess.run(
                    [str(VASPKIT_BINARY), "-task", task],
                    cwd=tmp_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=90,
                    check=False,
                )
                kpath_path = tmp_path / "KPATH.in"
                if kpath_path.exists() and kpath_path.stat().st_size > 0:
                    notes.append(f"Generated band path with VASPKIT task {task} for {material_class} geometry.")
                    if completed.returncode != 0:
                        notes.append("VASPKIT returned non-zero but still produced a usable KPATH.in; review the path if needed.")
                    return rewrite_line_mode_points(kpath_path.read_text(errors="ignore"), line_points), notes
                notes.append("VASPKIT did not produce KPATH.in; falling back to SeekPath.")
        except Exception as exc:
            notes.append(f"VASPKIT path generation failed ({exc}); falling back to SeekPath.")
    elif task:
        notes.append("VASPKIT is unavailable, so band path generation fell back to SeekPath.")

    notes.append("Generated symmetry path with SeekPath fallback.")
    return rewrite_line_mode_points(seekpath_kpath_text(poscar), line_points), notes


def band_conf_from_kpath(
    system_dir: Path,
    metadata: dict[str, Any],
    poscar: dict[str, Any],
    band_points: int,
    *,
    structure_source: Path,
    kpath_text_override: str | None = None,
) -> tuple[str, list[str]]:
    kpath_path = system_dir / "KPATH.in"
    notes: list[str] = []
    if kpath_text_override and kpath_text_override.strip():
        kpath_text = rewrite_line_mode_points(kpath_text_override, band_points)
        notes.append("Derived phonopy BAND path from the KPATH.in content generated in the same update.")
    elif str(metadata.get("band_path_mode") or "").strip().lower() == "custom" and str(metadata.get("band_path_text") or "").strip():
        kpath_text = normalize_custom_kpath_text(str(metadata.get("band_path_text") or ""), band_points)
        notes.append("Derived phonopy BAND path from the custom band path saved in Material Parameters.")
    elif kpath_path.exists() and kpath_path.stat().st_size > 0:
        kpath_text = rewrite_line_mode_points(kpath_path.read_text(errors="ignore"), band_points)
        notes.append("Derived phonopy BAND path from the current KPATH.in file so manual band-path edits stay in sync.")
    else:
        kpath_text, path_notes = generate_kpath_text(
            system_dir,
            metadata,
            poscar,
            band_points,
            structure_source=structure_source,
        )
        notes.extend(path_notes)
        notes.append("Derived phonopy BAND path from the generated line-mode KPATH definition.")

    band_paths = _parse_line_mode_segments(kpath_text)
    if not band_paths:
        raise ValueError("Could not derive phonopy BAND path from the current KPATH definition")

    supercell = infer_phonon_supercell(metadata)
    notes.append(f"Using phonon supercell DIM = {supercell[0]} {supercell[1]} {supercell[2]}.")
    notes.append(f"Using BAND_POINTS = {band_points}.")
    notes.append("ATOM_NAME is taken from the POSCAR species line, not from the project formula label.")
    return band_conf_text(poscar["species"], band_paths, supercell, band_points=band_points), notes


def generate_input_content(
    system_dir: Path,
    relative_path: str,
    metadata: dict[str, Any] | None = None,
    *,
    planned_inputs: dict[str, str] | None = None,
) -> dict[str, Any]:
    structure_path = generated_input_structure_path(system_dir, relative_path)
    if not structure_path.exists():
        raise ValueError("POSCAR is required before generating other inputs")

    metadata = dict(metadata) if metadata is not None else read_json(system_dir / "metadata.json", {})
    poscar = parse_poscar_model(structure_path.read_text(errors="ignore"))
    structure_note = _structure_source_note(system_dir, structure_path)
    allow_existing_mesh = structure_path == root_structure_path(system_dir)

    if relative_path in {"KPOINTS.relax", "KPOINTS.scf", "KPOINTS.downstream"}:
        mesh = infer_scf_mesh(
            system_dir,
            metadata,
            poscar,
            existing_path=system_dir / relative_path,
            allow_existing=allow_existing_mesh,
        )
        notes = [
            f"Generated uniform SCF mesh {mesh[0]} {mesh[1]} {mesh[2]}.",
            "This uses metadata.kmesh when available, otherwise a lattice-length heuristic.",
            structure_note,
        ]
        if relative_path == "KPOINTS.relax":
            notes.append("This relax mesh is copied to runs/relax/KPOINTS.")
        elif relative_path == "KPOINTS.scf":
            notes.append("This static SCF mesh is used for SCF and related post-relax mesh-based steps.")
        elif relative_path == "KPOINTS.downstream":
            notes.append("Legacy downstream mesh support remains for old projects; prefer KPOINTS.scf for new runs.")
        return {
            "path": relative_path,
            "content": kpoints_mesh_text(mesh),
            "notes": notes,
        }

    if relative_path == "KPOINTS.phonon":
        mesh = infer_phonon_mesh(system_dir, metadata, poscar, allow_existing=allow_existing_mesh)
        return {
            "path": relative_path,
            "content": kpoints_mesh_text(mesh),
            "notes": [
                f"Generated phonon mesh {mesh[0]} {mesh[1]} {mesh[2]}.",
                "This uses metadata.phonon_kmesh when available, otherwise halves the SCF mesh.",
                structure_note,
            ],
        }

    if relative_path == "KPOINTS.dos":
        mesh = infer_dos_mesh(system_dir, metadata, poscar, allow_existing=allow_existing_mesh)
        return {
            "path": relative_path,
            "content": kpoints_mesh_text(mesh),
            "notes": [
                f"Generated DOS mesh {mesh[0]} {mesh[1]} {mesh[2]}.",
                "This uses metadata.dos_kmesh when available, otherwise a slightly denser mesh than SCF.",
                structure_note,
            ],
        }

    if relative_path == "KPATH.in":
        line_points = max(20, int(metadata.get("band_points", 40)))
        content, path_notes = generate_kpath_text(
            system_dir,
            metadata,
            poscar,
            line_points,
            structure_source=structure_path,
        )
        return {
            "path": relative_path,
            "content": content,
            "notes": [
                *path_notes,
                f"Using {line_points} points per line segment in the exported KPATH.in preview.",
                structure_note,
            ],
        }

    if relative_path == "KPOINTS.band":
        line_points = max(20, int(metadata.get("band_points", 40)))
        content, path_notes = generate_kpath_text(
            system_dir,
            metadata,
            poscar,
            line_points,
            structure_source=structure_path,
        )
        electronic_functional = normalized_electronic_functional(metadata)
        notes = [
            *path_notes,
            f"Using {line_points} points per line segment.",
            structure_note,
        ]
        if electronic_functional == "HSE06":
            notes.append("For HSE06 band, this line path will be copied to KPOINTS_OPT and paired with the SCF mesh KPOINTS.")
        else:
            notes.append("This line-mode path will be used directly as KPOINTS for the standard band run.")
        return {
            "path": relative_path,
            "content": content,
            "notes": notes,
        }

    if relative_path == "band.conf":
        band_points = max(20, int(metadata.get("band_points", 101)))
        content, notes = band_conf_from_kpath(
            system_dir,
            metadata,
            poscar,
            band_points,
            structure_source=structure_path,
            kpath_text_override=(planned_inputs or {}).get("KPATH.in"),
        )
        notes.append(structure_note)
        return {
            "path": relative_path,
            "content": content,
            "notes": notes,
        }

    if relative_path in INCAR_TEMPLATE_MAP:
        content, notes = render_incar(relative_path, system_dir, metadata, poscar)
        notes.append(structure_note)
        return {"path": relative_path, "content": content, "notes": notes}

    raise ValueError(f"Auto-generation is not supported for {relative_path}")
