"""Heuristic structure analysis used to advise (never silently override) setup choices.

The routines here answer two questions from a POSCAR alone:

* Is this cell periodic in three directions (bulk), two (slab/2D), or none
  (isolated molecule)? This is decided from the largest vacuum gap along each
  lattice direction, measured as a perpendicular thickness so that non-orthogonal
  cells are handled correctly.
* Does the composition contain elements that commonly carry a local moment, so
  that a spin-polarized workflow should at least be considered?

Both answers are advisory. They are surfaced with the evidence that produced
them so the user can disagree, and neither is applied without an explicit click.
"""

from __future__ import annotations

import math
from typing import Any

from .magnetism import SPECIES_MAGMOM_GUESSES


# Gap between atom centres, along the perpendicular direction, above which a
# lattice direction is treated as non-periodic. Real vacuum is smaller than the
# centre-to-centre gap by roughly two atomic radii, so this threshold sits well
# above any physical bond or van der Waals contact (graphite interlayer spacing
# is ~3.35 A, a long ionic contact ~3.5 A) while staying below the vacuum
# padding used in slab calculations (typically >= 12 A).
VACUUM_GAP_THRESHOLD = 8.0

# Below this the classification is reported but flagged as uncertain.
VACUUM_GAP_UNCERTAIN_MARGIN = 2.0

# Elements with a partially filled d/f shell that routinely carry a local moment
# in compounds. Closed-shell d10 species (Zn, Cd, Ag, Au) are deliberately
# absent: they are non-magnetic in their common oxidation states.
STRONGLY_MAGNETIC_SPECIES = {
    "Cr", "Mn", "Fe", "Co", "Ni",
    "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm",
    "U", "Np", "Pu",
}

# Elements that may or may not be magnetic depending on oxidation state,
# coordination and covalency. They warrant a mention, not a recommendation.
POSSIBLY_MAGNETIC_SPECIES = {
    "Ti", "V", "Cu", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Sc", "Y", "Zr", "Yb", "O",
}


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(vector: list[float]) -> float:
    return math.sqrt(_dot(vector, vector))


def perpendicular_spacings(lattice: list[list[float]]) -> list[float]:
    """Interplanar spacing along each lattice direction.

    For direction ``i`` this is the cell volume divided by the area of the face
    spanned by the other two vectors, i.e. the perpendicular thickness of the
    cell. Using ``|a_i|`` instead would overestimate the available room in a
    skewed cell, which is exactly where naive vacuum detection goes wrong.
    """
    a, b, c = lattice
    volume = abs(_dot(a, _cross(b, c)))
    if volume < 1e-9:
        raise ValueError("Lattice vectors are singular")
    return [
        volume / _norm(_cross(b, c)),
        volume / _norm(_cross(c, a)),
        volume / _norm(_cross(a, b)),
    ]


def largest_fractional_gap(fractions: list[float]) -> float:
    """Largest empty stretch along a periodic axis, in fractional units.

    The wrap-around gap between the last and first atom is included, so a slab
    centred on the cell boundary (atoms near f=0 and f=1, vacuum in the middle
    of the cell, or the reverse) is measured correctly.
    """
    if not fractions:
        return 1.0
    wrapped = sorted(value - math.floor(value) for value in fractions)
    if len(wrapped) == 1:
        return 1.0
    gaps = [second - first for first, second in zip(wrapped, wrapped[1:])]
    gaps.append(1.0 - wrapped[-1] + wrapped[0])
    return max(gaps)


def vacuum_gaps(lattice: list[list[float]], fractional_positions: list[list[float]]) -> list[float]:
    """Largest vacuum gap along each lattice direction, in angstrom."""
    spacings = perpendicular_spacings(lattice)
    gaps: list[float] = []
    for axis in range(3):
        column = [position[axis] for position in fractional_positions]
        gaps.append(largest_fractional_gap(column) * spacings[axis])
    return gaps


def _axis_label(axis: int) -> str:
    return "abc"[axis]


def _join_species(species: list[str]) -> str:
    if len(species) <= 1:
        return "".join(species)
    return f"{', '.join(species[:-1])} and {species[-1]}"


def detect_dimensionality(lattice: list[list[float]], fractional_positions: list[list[float]]) -> dict[str, Any]:
    """Classify the cell as bulk, slab/2D, or molecule from its vacuum gaps."""
    gaps = vacuum_gaps(lattice, fractional_positions)
    open_axes = [axis for axis, gap in enumerate(gaps) if gap >= VACUUM_GAP_THRESHOLD]
    borderline = [
        axis
        for axis, gap in enumerate(gaps)
        if abs(gap - VACUUM_GAP_THRESHOLD) < VACUUM_GAP_UNCERTAIN_MARGIN
    ]

    uncertain_kind: str | None = None
    if len(open_axes) >= 3:
        material_class = "molecule"
        reason = (
            f"Vacuum along all three directions "
            f"({gaps[0]:.1f}, {gaps[1]:.1f}, {gaps[2]:.1f} A between atom centres)."
        )
    elif len(open_axes) == 1:
        axis = open_axes[0]
        material_class = "2d"
        reason = (
            f"About {gaps[axis]:.1f} A of vacuum along the {_axis_label(axis)} direction "
            f"and none along the other two."
        )
    elif len(open_axes) == 2:
        material_class = "bulk"
        uncertain_kind = "wire"
        labels = " and ".join(_axis_label(axis) for axis in open_axes)
        reason = (
            f"Vacuum along {labels} but not the third direction. This looks like a wire or "
            f"chain geometry, which this tool does not model directly; treating it as bulk."
        )
    else:
        material_class = "bulk"
        reason = (
            f"No direction has more than {VACUUM_GAP_THRESHOLD:.0f} A of vacuum "
            f"(largest gap {max(gaps):.1f} A), so the cell is periodic in 3D."
        )

    if uncertain_kind is None and borderline:
        uncertain_kind = "borderline_gap"

    return {
        "material_class": material_class,
        "vacuum_gaps": [round(gap, 2) for gap in gaps],
        "vacuum_axis": _axis_label(open_axes[0]) if len(open_axes) == 1 else None,
        "open_axes": [_axis_label(axis) for axis in open_axes],
        "certain": uncertain_kind is None,
        "uncertain_kind": uncertain_kind,
        "reason": reason,
    }


def detect_magnetism(species: list[str]) -> dict[str, Any]:
    """Flag compositions that commonly need a spin-polarized workflow."""
    strong = [element for element in species if element in STRONGLY_MAGNETIC_SPECIES]
    possible = [
        element
        for element in species
        if element in POSSIBLY_MAGNETIC_SPECIES and element not in STRONGLY_MAGNETIC_SPECIES
    ]

    # Oxygen alone is not worth mentioning: it would fire on every oxide while
    # carrying a moment only alongside an open-shell partner.
    if possible == ["O"]:
        possible = []

    if strong:
        reason = (
            f"{_join_species(strong)} {'carries' if len(strong) == 1 else 'carry'} a local moment in most "
            f"compounds, so a spin-polarized run is the safer starting point. Non-magnetic ground states are "
            f"still possible; compare total energies if the moment collapses to zero."
        )
    elif possible:
        reason = (
            f"{_join_species(possible)} {'carries' if len(possible) == 1 else 'carry'} a moment only in some "
            f"oxidation states and environments. Spin polarization is left off; enable it if you expect an "
            f"open-shell configuration."
        )
    else:
        reason = "No elements that commonly carry a local moment were found."

    return {
        "recommend_spin_polarized": bool(strong),
        "strong_species": strong,
        "possible_species": possible,
        "certain": not possible or bool(strong),
        "reason": reason,
        "suggested_magmom": [
            float(SPECIES_MAGMOM_GUESSES.get(element, 0.0)) for element in species
        ],
    }


def analyze_structure(poscar: dict[str, Any]) -> dict[str, Any]:
    """Full advisory analysis for one parsed POSCAR."""
    dimensionality = detect_dimensionality(poscar["lattice"], poscar["fractional_positions"])
    magnetism = detect_magnetism(poscar["species"])
    return {
        "dimensionality": dimensionality,
        "magnetism": magnetism,
        "certain": bool(dimensionality["certain"] and magnetism["certain"]),
    }
