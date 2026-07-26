#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def locate_grid_header(lines: list[str]) -> tuple[int, tuple[int, int, int]]:
    if len(lines) < 8:
        raise ValueError("CHGCAR-like file is too short")

    counts = [int(value) for value in lines[6].split()]
    total_atoms = sum(counts)
    index = 7
    if lines[index].strip().lower().startswith("s"):
        index += 1
    index += 1 + total_atoms

    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        raise ValueError("Missing volumetric grid header")

    dims = tuple(int(value) for value in lines[index].split()[:3])
    if len(dims) != 3:
        raise ValueError("Invalid volumetric grid dimensions")
    return index, dims


def read_density(path: Path) -> tuple[list[str], tuple[int, int, int], list[float]]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    grid_index, dims = locate_grid_header(lines)
    total_values = dims[0] * dims[1] * dims[2]
    values: list[float] = []
    for line in lines[grid_index + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        values.extend(float(token) for token in stripped.split())
        if len(values) >= total_values:
            break
    if len(values) < total_values:
        raise ValueError(f"{path} does not contain enough grid values")
    return lines[: grid_index + 1], dims, values[:total_values]


def write_density(path: Path, header: list[str], values: list[float]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(header))
        handle.write("\n")
        for index in range(0, len(values), 5):
            chunk = values[index : index + 5]
            handle.write(" ".join(f"{value: .11E}" for value in chunk))
            handle.write("\n")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("Usage: chgcar_sum.py <AECCAR0> <AECCAR2> <OUT>", file=sys.stderr)
        return 2

    left_path = Path(argv[1])
    right_path = Path(argv[2])
    out_path = Path(argv[3])

    left_header, left_dims, left_values = read_density(left_path)
    right_header, right_dims, right_values = read_density(right_path)
    if left_dims != right_dims:
        raise ValueError(f"Grid mismatch: {left_dims} != {right_dims}")

    summed = [a + b for a, b in zip(left_values, right_values)]
    write_density(out_path, left_header, summed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
