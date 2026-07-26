from __future__ import annotations

from typing import Any


# Conservative per-atom initial guesses for common magnetic species.
# These are starting moments for SCF initialization, not predicted final moments.
SPECIES_MAGMOM_GUESSES: dict[str, float] = {
    "Sc": 1.0,
    "Ti": 2.0,
    "V": 3.0,
    "Cr": 5.0,
    "Mn": 5.0,
    "Fe": 5.0,
    "Co": 3.0,
    "Ni": 2.0,
    "Cu": 1.0,
    "Y": 1.0,
    "Zr": 2.0,
    "Nb": 3.0,
    "Mo": 4.0,
    "Tc": 5.0,
    "Ru": 3.0,
    "Rh": 2.0,
    "Pd": 1.0,
    "Ce": 1.0,
    "Pr": 2.0,
    "Nd": 3.0,
    "Sm": 5.0,
    "Eu": 7.0,
    "Gd": 7.0,
    "Tb": 6.0,
    "Dy": 5.0,
    "Ho": 4.0,
    "Er": 3.0,
    "Tm": 2.0,
    "Yb": 1.0,
    "U": 3.0,
    "Np": 4.0,
    "Pu": 5.0,
}


def explicit_magmom_values(raw: Any, species: list[str], counts: list[int]) -> list[float] | None:
    total_atoms = sum(counts)
    if not isinstance(raw, list):
        return None
    if len(raw) == total_atoms:
        return [float(value) for value in raw]
    if len(raw) == len(species):
        expanded: list[float] = []
        for value, count in zip(raw, counts):
            expanded.extend([float(value)] * count)
        return expanded
    return None


def heuristic_magmom_values(species: list[str], counts: list[int]) -> list[float]:
    expanded: list[float] = []
    for element, count in zip(species, counts):
        guess = float(SPECIES_MAGMOM_GUESSES.get(element, 0.0))
        expanded.extend([guess] * count)
    return expanded


def default_magmom_values(species: list[str], counts: list[int], metadata: dict[str, Any]) -> list[float]:
    explicit = explicit_magmom_values(metadata.get("magmom"), species, counts)
    if explicit is not None:
        return explicit
    if bool(metadata.get("spin_polarized", False)):
        return heuristic_magmom_values(species, counts)
    return [0.0] * sum(counts)
