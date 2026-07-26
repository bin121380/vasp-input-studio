from __future__ import annotations

from pathlib import Path


RELAX_CONTCAR_RELATIVE = "runs/relax/CONTCAR"
RELAX_PRIMITIVE_RELATIVE = "runs/relax/PRIMCELL.vasp"
RELAX_PRIMITIVE_LOG_RELATIVE = "runs/relax/vaspkit_primitive.log"
RELAX_PRIMITIVE_SYMMETRY_RELATIVE = "runs/relax/SYMMETRY"
RELAX_MESH_KPOINTS_RELATIVE = "KPOINTS.relax"
SCF_MESH_KPOINTS_RELATIVE = "KPOINTS.scf"
LEGACY_DOWNSTREAM_MESH_KPOINTS_RELATIVE = "KPOINTS.downstream"
DOWNSTREAM_MESH_KPOINTS_RELATIVE = LEGACY_DOWNSTREAM_MESH_KPOINTS_RELATIVE

DOWNSTREAM_STEPS = {"scf", "dos", "band", "converge", "elastic", "charge", "phonon"}
DOWNSTREAM_MANAGED_INPUTS = {
    "INCAR.scf",
    "INCAR.dos",
    "INCAR.converge",
    "INCAR.band",
    "INCAR.elastic",
    "INCAR.charge",
    "INCAR.phonon",
    SCF_MESH_KPOINTS_RELATIVE,
    LEGACY_DOWNSTREAM_MESH_KPOINTS_RELATIVE,
    "KPOINTS.dos",
    "KPATH.in",
    "KPOINTS.band",
    "KPOINTS.phonon",
    "band.conf",
}
ROOT_STRUCTURE_MANAGED_INPUTS = {
    "POSCAR",
    "INCAR.relax",
    RELAX_MESH_KPOINTS_RELATIVE,
}
PRIMITIVE_REGENERATED_INPUTS = (
    "INCAR.scf",
    "INCAR.dos",
    "INCAR.converge",
    "INCAR.band",
    "INCAR.elastic",
    "INCAR.charge",
    "INCAR.phonon",
    SCF_MESH_KPOINTS_RELATIVE,
    "KPOINTS.dos",
    "KPATH.in",
    "KPOINTS.band",
    "KPOINTS.phonon",
    "band.conf",
)


def _nonempty(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def relative_system_path(system_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(system_dir).as_posix()
    except ValueError:
        return path.as_posix()


def root_structure_path(system_dir: Path) -> Path:
    return system_dir / "POSCAR"


def relax_contcar_path(system_dir: Path) -> Path:
    return system_dir / RELAX_CONTCAR_RELATIVE


def relax_primitive_path(system_dir: Path) -> Path:
    return system_dir / RELAX_PRIMITIVE_RELATIVE


def relax_primitive_log_path(system_dir: Path) -> Path:
    return system_dir / RELAX_PRIMITIVE_LOG_RELATIVE


def relax_primitive_symmetry_path(system_dir: Path) -> Path:
    return system_dir / RELAX_PRIMITIVE_SYMMETRY_RELATIVE


def downstream_mesh_kpoints_path(system_dir: Path) -> Path:
    return system_dir / DOWNSTREAM_MESH_KPOINTS_RELATIVE


def relax_mesh_kpoints_path(system_dir: Path) -> Path:
    return system_dir / RELAX_MESH_KPOINTS_RELATIVE


def scf_mesh_kpoints_path(system_dir: Path) -> Path:
    return system_dir / SCF_MESH_KPOINTS_RELATIVE


def primitive_cell_available(system_dir: Path) -> bool:
    return _nonempty(relax_primitive_path(system_dir))


def relax_contcar_available(system_dir: Path) -> bool:
    return _nonempty(relax_contcar_path(system_dir))


def downstream_structure_path(system_dir: Path) -> Path:
    contcar = relax_contcar_path(system_dir)
    if _nonempty(contcar):
        return contcar
    return root_structure_path(system_dir)


def resolved_structure_path(system_dir: Path, step: str, *, resume: bool = False) -> Path:
    normalized = str(step or "").strip().lower()
    if normalized == "relax":
        if resume and _nonempty(relax_contcar_path(system_dir)):
            return relax_contcar_path(system_dir)
        return root_structure_path(system_dir)
    if normalized in DOWNSTREAM_STEPS:
        return downstream_structure_path(system_dir)
    return root_structure_path(system_dir)


def resolved_mesh_kpoints_path(system_dir: Path, step: str) -> Path:
    normalized = str(step or "").strip().lower()
    if normalized == "relax":
        relax_mesh = relax_mesh_kpoints_path(system_dir)
        return relax_mesh if _nonempty(relax_mesh) else scf_mesh_kpoints_path(system_dir)
    if normalized in {"scf", "converge", "elastic", "charge", "band"}:
        scf_mesh = scf_mesh_kpoints_path(system_dir)
        if _nonempty(scf_mesh):
            return scf_mesh
        legacy_downstream = downstream_mesh_kpoints_path(system_dir)
        if _nonempty(legacy_downstream):
            return legacy_downstream
    return scf_mesh_kpoints_path(system_dir)


def generated_input_structure_path(system_dir: Path, relative_path: str) -> Path:
    normalized = relative_path.strip().replace("\\", "/")
    if normalized in DOWNSTREAM_MANAGED_INPUTS:
        return downstream_structure_path(system_dir)
    return root_structure_path(system_dir)


def generated_input_structure_relative_path(system_dir: Path, relative_path: str) -> str:
    return relative_system_path(system_dir, generated_input_structure_path(system_dir, relative_path))


def resolved_structure_relative_path(system_dir: Path, step: str, *, resume: bool = False) -> str:
    return relative_system_path(system_dir, resolved_structure_path(system_dir, step, resume=resume))


def uses_downstream_structure(relative_path: str) -> bool:
    normalized = relative_path.strip().replace("\\", "/")
    return normalized in DOWNSTREAM_MANAGED_INPUTS
