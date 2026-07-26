import math
import unittest

from app import structure_analysis


def hexagonal_lattice(a: float, c: float) -> list[list[float]]:
    return [[a, 0.0, 0.0], [-a / 2, a * math.sqrt(3) / 2, 0.0], [0.0, 0.0, c]]


class VacuumGeometryTests(unittest.TestCase):
    def test_perpendicular_spacing_ignores_skew(self) -> None:
        # |c| is 13.3 A but the cell is only 4 A thick perpendicular to the ab plane.
        lattice = [[4.0, 0.0, 0.0], [0.0, 4.0, 0.0], [9.0, 9.0, 4.0]]
        spacings = structure_analysis.perpendicular_spacings(lattice)

        self.assertAlmostEqual(spacings[2], 4.0, places=6)
        self.assertLess(spacings[2], math.sqrt(9**2 + 9**2 + 4**2))

    def test_largest_gap_wraps_around_periodic_boundary(self) -> None:
        # Atoms clustered at both ends: the real gap is the one in the middle.
        self.assertAlmostEqual(structure_analysis.largest_fractional_gap([0.02, 0.98]), 0.96, places=6)
        # Atoms clustered in the middle: the real gap straddles the boundary.
        self.assertAlmostEqual(structure_analysis.largest_fractional_gap([0.48, 0.52]), 0.96, places=6)

    def test_single_atom_leaves_whole_axis_open(self) -> None:
        self.assertEqual(structure_analysis.largest_fractional_gap([0.5]), 1.0)


class DimensionalityTests(unittest.TestCase):
    def test_dense_bulk_is_periodic_in_three_directions(self) -> None:
        lattice = [[5.43, 0.0, 0.0], [0.0, 5.43, 0.0], [0.0, 0.0, 5.43]]
        positions = [[0.0, 0.0, 0.0], [0.25, 0.25, 0.25], [0.5, 0.5, 0.0], [0.0, 0.5, 0.5]]

        result = structure_analysis.detect_dimensionality(lattice, positions)

        self.assertEqual(result["material_class"], "bulk")
        self.assertIsNone(result["vacuum_axis"])
        self.assertTrue(result["certain"])

    def test_monolayer_with_vacuum_is_detected_as_2d(self) -> None:
        lattice = hexagonal_lattice(2.46, 20.0)
        positions = [[0.0, 0.0, 0.5], [1 / 3, 2 / 3, 0.5]]

        result = structure_analysis.detect_dimensionality(lattice, positions)

        self.assertEqual(result["material_class"], "2d")
        self.assertEqual(result["vacuum_axis"], "c")

    def test_monolayer_straddling_cell_boundary_is_still_2d(self) -> None:
        # Same slab shifted so its atoms sit at the cell edge and the vacuum is
        # split across the periodic boundary.
        lattice = hexagonal_lattice(2.46, 20.0)
        positions = [[0.0, 0.0, 0.0], [1 / 3, 2 / 3, 0.02]]

        result = structure_analysis.detect_dimensionality(lattice, positions)

        self.assertEqual(result["material_class"], "2d")
        self.assertEqual(result["vacuum_axis"], "c")

    def test_layered_van_der_waals_bulk_is_not_mistaken_for_2d(self) -> None:
        # Graphite: 3.35 A interlayer spacing must stay bulk.
        lattice = hexagonal_lattice(2.46, 6.70)
        positions = [[0.0, 0.0, 0.0], [1 / 3, 2 / 3, 0.0], [0.0, 0.0, 0.5], [2 / 3, 1 / 3, 0.5]]

        result = structure_analysis.detect_dimensionality(lattice, positions)

        self.assertEqual(result["material_class"], "bulk")

    def test_isolated_cluster_is_detected_as_molecule(self) -> None:
        lattice = [[15.0, 0.0, 0.0], [0.0, 15.0, 0.0], [0.0, 0.0, 15.0]]
        positions = [[0.5, 0.5, 0.5], [0.56, 0.5, 0.5]]

        result = structure_analysis.detect_dimensionality(lattice, positions)

        self.assertEqual(result["material_class"], "molecule")
        self.assertEqual(result["open_axes"], ["a", "b", "c"])

    def test_wire_geometry_is_flagged_with_its_own_reason_code(self) -> None:
        # Vacuum along two directions: not a slab, and the generic "borderline
        # gap" wording would be actively misleading here.
        lattice = [[15.0, 0.0, 0.0], [0.0, 15.0, 0.0], [0.0, 0.0, 2.5]]
        positions = [[0.5, 0.5, 0.0], [0.5, 0.5, 0.5]]

        result = structure_analysis.detect_dimensionality(lattice, positions)

        self.assertEqual(result["material_class"], "bulk")
        self.assertEqual(result["uncertain_kind"], "wire")
        self.assertFalse(result["certain"])

    def test_borderline_vacuum_is_reported_as_uncertain(self) -> None:
        lattice = [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 9.0]]
        positions = [[0.0, 0.0, 0.0], [0.5, 0.5, 0.12]]

        result = structure_analysis.detect_dimensionality(lattice, positions)

        self.assertFalse(result["certain"])
        self.assertEqual(result["uncertain_kind"], "borderline_gap")


class MagnetismTests(unittest.TestCase):
    def test_transition_metal_triggers_spin_recommendation(self) -> None:
        result = structure_analysis.detect_magnetism(["Fe", "O"])

        self.assertTrue(result["recommend_spin_polarized"])
        self.assertIn("Fe", result["strong_species"])

    def test_closed_shell_d10_does_not_trigger_recommendation(self) -> None:
        result = structure_analysis.detect_magnetism(["Zn", "O"])

        self.assertFalse(result["recommend_spin_polarized"])
        self.assertEqual(result["strong_species"], [])

    def test_ambiguous_species_are_reported_without_recommending(self) -> None:
        result = structure_analysis.detect_magnetism(["Ti", "O"])

        self.assertFalse(result["recommend_spin_polarized"])
        self.assertIn("Ti", result["possible_species"])
        self.assertFalse(result["certain"])

    def test_oxygen_alone_does_not_produce_an_advisory(self) -> None:
        # Otherwise every oxide would get a magnetism note it does not need.
        result = structure_analysis.detect_magnetism(["Mg", "O"])

        self.assertEqual(result["possible_species"], [])
        self.assertTrue(result["certain"])

    def test_species_list_reads_grammatically(self) -> None:
        result = structure_analysis.detect_magnetism(["Fe", "Mn", "O"])

        self.assertIn("Fe and Mn carry", result["reason"])

    def test_single_species_uses_singular_verb(self) -> None:
        result = structure_analysis.detect_magnetism(["Fe", "O"])

        self.assertIn("Fe carries", result["reason"])

    def test_suggested_magmom_matches_species_order(self) -> None:
        result = structure_analysis.detect_magnetism(["Fe", "O", "Mn"])

        self.assertEqual(len(result["suggested_magmom"]), 3)
        self.assertGreater(result["suggested_magmom"][0], 0.0)
        self.assertEqual(result["suggested_magmom"][1], 0.0)


if __name__ == "__main__":
    unittest.main()
