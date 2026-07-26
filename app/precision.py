"""Precision tiers: structure-aware starting points for the sampling parameters.

A newcomer should not have to invent an SCF mesh, a DOS mesh and a plane-wave
cutoff before their first calculation. These tiers derive all of them from the
cell itself, using the same reciprocal-space density rule VASP's ``KSPACING``
uses, so that a small primitive cell automatically gets a denser mesh than a
large supercell of the same material.

The tiers are honest starting points, not converged settings. Convergence with
respect to k-points and ENCUT is system-dependent and remains the user's job;
the convergence workflow exists for exactly that.
"""

from __future__ import annotations

import math
from typing import Any

from .structure_analysis import perpendicular_spacings


# Reciprocal-space sampling density in A^-1, following VASP's KSPACING
# convention (mesh_i = ceil(|b_i| / KSPACING) with |b_i| including the 2*pi
# factor). Smaller means denser.
PRECISION_TIERS: dict[str, dict[str, Any]] = {
    "quick": {
        "label": "Quick",
        "tagline": "Fast screening",
        "description": "Coarse k-point sampling, for checking that a workflow runs end to end. Never report a number from this level.",
        "kspacing": 0.40,
        "encut_factor": 1.3,
        "encut_fallback": 400.0,
        "phonon_dos_kspacing": 0.10,
        "band_points": 41,
        "supercell_target_angstrom": 8.0,
        "wallclock_seconds": 6 * 3600,
    },
    "standard": {
        "label": "Standard",
        "tagline": "Balanced default",
        "description": "Where most people start a real calculation. Not a converged setting: check the cutoff and k-points before you report anything.",
        "kspacing": 0.25,
        "encut_factor": 1.3,
        "encut_fallback": 520.0,
        "phonon_dos_kspacing": 0.07,
        "band_points": 81,
        "supercell_target_angstrom": 10.0,
        "wallclock_seconds": 12 * 3600,
    },
    "high": {
        "label": "High",
        "tagline": "Tight settings",
        "description": "Denser mesh and higher cutoff, noticeably more expensive. Tighter is not the same as converged: it still needs a convergence test.",
        "kspacing": 0.18,
        "encut_factor": 1.5,
        "encut_fallback": 600.0,
        "phonon_dos_kspacing": 0.05,
        "band_points": 121,
        "supercell_target_angstrom": 12.0,
        "wallclock_seconds": 24 * 3600,
    },
}

# ENCUT is one global value applied to every step, and INCAR.relax / INCAR.elastic
# both run at ISIF=3. A basis set fixed at roughly ENMAX does not follow the
# changing cell, so the uncorrected Pulay stress biases the relaxed volume and any
# stress-derived elastic constant. Every tier therefore sits at or above the same
# 1.3 x ENMAX floor the submission audit already enforces; tiers differ in k-point
# sampling, not in whether the cutoff is safe for a cell relaxation.
ENCUT_RELAXATION_SAFETY_FACTOR = 1.3

# Metals need finer Brillouin-zone sampling to resolve the Fermi surface.
METAL_KSPACING_FACTOR = 0.75

# DOS is integrated over the zone, so it wants a denser mesh than the SCF step.
DOS_KSPACING_FACTOR = 0.7

# Largest mesh count generated along one axis, to keep an unusual cell from
# producing an absurd k-point count.
MAX_MESH_PER_AXIS = 31

# Phonon supercells grow cost as N^3; refuse to auto-suggest beyond this.
MAX_SUPERCELL_PER_AXIS = 4


def reciprocal_lengths(lattice: list[list[float]]) -> list[float]:
    """Lengths of the reciprocal lattice vectors, including the 2*pi factor."""
    return [2 * math.pi / spacing for spacing in perpendicular_spacings(lattice)]


def mesh_from_kspacing(
    lattice: list[list[float]],
    kspacing: float,
    *,
    periodic_axes: tuple[bool, bool, bool] = (True, True, True),
) -> list[int]:
    """Gamma-centred mesh for a given reciprocal-space sampling density."""
    mesh: list[int] = []
    for axis, length in enumerate(reciprocal_lengths(lattice)):
        if not periodic_axes[axis]:
            mesh.append(1)
            continue
        count = math.ceil(length / kspacing - 1e-9)
        mesh.append(max(1, min(MAX_MESH_PER_AXIS, count)))
    return mesh


def periodic_axes_for_class(material_class: str, vacuum_axis: str | None) -> tuple[bool, bool, bool]:
    """Which lattice directions should carry more than one k-point."""
    normalized = str(material_class or "bulk").strip().lower()
    if normalized == "molecule":
        return (False, False, False)
    if normalized in {"2d", "slab"}:
        axis_index = {"a": 0, "b": 1, "c": 2}.get(str(vacuum_axis or "c").lower(), 2)
        return tuple(index != axis_index for index in range(3))  # type: ignore[return-value]
    return (True, True, True)


def supercell_for_target(lattice: list[list[float]], target_angstrom: float) -> list[int]:
    """Smallest supercell whose perpendicular thickness reaches the target."""
    dims: list[int] = []
    for spacing in perpendicular_spacings(lattice):
        count = math.ceil(target_angstrom / max(spacing, 1e-6) - 1e-9)
        dims.append(max(1, min(MAX_SUPERCELL_PER_AXIS, count)))
    return dims


def encut_for_tier(tier: dict[str, Any], max_enmax: float | None) -> tuple[float, str, bool]:
    """Plane-wave cutoff, the sentence explaining it, and whether it is POTCAR-derived."""
    if max_enmax and max_enmax > 0:
        factor = max(float(tier["encut_factor"]), ENCUT_RELAXATION_SAFETY_FACTOR)
        value = float(math.ceil(max_enmax * factor / 10.0) * 10)
        note = (
            f"Cutoff {value:.0f} eV is {factor:.2g} x the largest ENMAX in this project's POTCAR "
            f"({max_enmax:.0f} eV). Staying at least 1.3 x ENMAX keeps the cell relaxation and the "
            f"elastic tensor free of large Pulay-stress error."
        )
        return value, note, True
    value = float(tier["encut_fallback"])
    note = (
        f"Cutoff {value:.0f} eV is a generic placeholder: this project has no POTCAR yet, so the "
        f"cutoff cannot be derived from ENMAX. Save parameters to build the POTCAR, then click this "
        f"level again to replace the placeholder."
    )
    return value, note, False


def tier_settings(
    tier_id: str,
    lattice: list[list[float]],
    *,
    material_class: str = "bulk",
    electronic_type: str = "auto",
    vacuum_axis: str | None = None,
    max_enmax: float | None = None,
    atom_count: int = 0,
) -> dict[str, Any]:
    """Resolve one precision tier against a concrete cell."""
    tier = PRECISION_TIERS.get(tier_id)
    if tier is None:
        raise ValueError(f"Unknown precision tier: {tier_id}")

    is_metal = str(electronic_type or "auto").strip().lower() == "metal"
    kspacing = float(tier["kspacing"]) * (METAL_KSPACING_FACTOR if is_metal else 1.0)
    axes = periodic_axes_for_class(material_class, vacuum_axis)

    scf_mesh = mesh_from_kspacing(lattice, kspacing, periodic_axes=axes)
    dos_kspacing = kspacing * DOS_KSPACING_FACTOR
    dos_mesh = mesh_from_kspacing(lattice, dos_kspacing, periodic_axes=axes)
    supercell = supercell_for_target(lattice, float(tier["supercell_target_angstrom"]))
    if str(material_class or "").strip().lower() in {"2d", "slab", "molecule"}:
        supercell = [
            dim if axes[index] else 1
            for index, dim in enumerate(supercell)
        ]

    # Electronic sampling of the phonon supercell, whose Brillouin zone is smaller
    # than the unit cell's by exactly the supercell repetition.
    phonon_mesh = [
        max(1, math.ceil(mesh / max(1, dim)))
        for mesh, dim in zip(scf_mesh, supercell)
    ]
    # phonopy's --mesh is a q-point sampling of the *primitive* zone used to
    # interpolate the phonon DOS and thermal properties. It is post-processing that
    # costs seconds, so it is sampled far more finely than any electronic mesh and
    # must not be derived from one.
    phonon_dos_mesh = mesh_from_kspacing(
        lattice, float(tier["phonon_dos_kspacing"]), periodic_axes=axes
    )

    encut, encut_note, encut_from_potcar = encut_for_tier(tier, max_enmax)

    notes = [
        f"SCF mesh from a {kspacing:.2f} A^-1 reciprocal-space spacing"
        + (" (tightened because Electronic Type is set to metal)" if is_metal else "")
        + f"; the DOS mesh uses {dos_kspacing:.2f} A^-1 because a DOS is integrated over the whole zone.",
        encut_note,
        f"The phonon DOS q-mesh {phonon_dos_mesh[0]}x{phonon_dos_mesh[1]}x{phonon_dos_mesh[2]} is a "
        f"phonopy interpolation grid, not a VASP mesh, so it is sampled finely at negligible cost.",
    ]
    if not is_metal:
        notes.append(
            "Electronic Type is not set to metal, so the mesh is sampled for a gapped system. "
            "Switch it to metal if this material has a Fermi surface, or the sampling will be too coarse."
        )
    if not all(axes):
        frozen = [label for label, periodic in zip("abc", axes) if not periodic]
        notes.append(
            f"Only 1 k-point along {', '.join(frozen)}: the cell is still repeated in that direction, but "
            f"the vacuum layer means the bands barely disperse there, so extra k-points would only cost time."
        )
    if str(material_class or "").strip().lower() in {"2d", "slab"} and not vacuum_axis:
        notes.append(
            "No single vacuum direction was detected, so c is assumed to be the out-of-plane axis. "
            "Check that this matches your cell before submitting."
        )
    supercell_atoms = int(atom_count) * supercell[0] * supercell[1] * supercell[2]
    target = float(tier["supercell_target_angstrom"])
    spacings = perpendicular_spacings(lattice)
    reached = [dim * spacing for dim, spacing in zip(supercell, spacings)]
    short_axes = [
        (label, value)
        for label, value, dim, periodic in zip("abc", reached, supercell, axes)
        if periodic and dim >= MAX_SUPERCELL_PER_AXIS and value < target
    ]
    size_text = f"{supercell[0]}x{supercell[1]}x{supercell[2]}"
    if supercell_atoms:
        size_text += f" = {supercell_atoms} atoms"
    if short_axes:
        detail = ", ".join(f"{label} reaches only {value:.1f} A" for label, value in short_axes)
        notes.append(
            f"Phonon supercell {size_text}, capped at {MAX_SUPERCELL_PER_AXIS} cells per direction, so "
            f"{detail} instead of the {target:.0f} A target. Force constants will be truncated; enlarge "
            f"the supercell by hand if the dispersion looks wrong."
        )
    else:
        notes.append(
            f"Phonon supercell {size_text}, giving at least {target:.0f} A along every periodic direction."
        )

    return {
        "id": tier_id,
        "label": tier["label"],
        "tagline": tier["tagline"],
        "description": tier["description"],
        "kspacing": round(kspacing, 3),
        "kmesh": scf_mesh,
        "dos_kmesh": dos_mesh,
        "phonon_kmesh": phonon_mesh,
        "phonon_dos_kmesh": phonon_dos_mesh,
        "phonon_supercell": supercell,
        "supercell_atom_count": supercell_atoms,
        "encut": encut,
        "encut_from_potcar": encut_from_potcar,
        "band_points": int(tier["band_points"]),
        "wallclock_seconds": int(tier["wallclock_seconds"]),
        "kpoint_count": scf_mesh[0] * scf_mesh[1] * scf_mesh[2],
        "notes": notes,
    }


def all_tier_settings(
    lattice: list[list[float]],
    *,
    material_class: str = "bulk",
    electronic_type: str = "auto",
    vacuum_axis: str | None = None,
    max_enmax: float | None = None,
    atom_count: int = 0,
) -> list[dict[str, Any]]:
    return [
        tier_settings(
            tier_id,
            lattice,
            material_class=material_class,
            electronic_type=electronic_type,
            vacuum_axis=vacuum_axis,
            max_enmax=max_enmax,
            atom_count=atom_count,
        )
        for tier_id in PRECISION_TIERS
    ]
