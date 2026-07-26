import math
import unittest

from app import precision


CUBIC_SI = [[5.43, 0.0, 0.0], [0.0, 5.43, 0.0], [0.0, 0.0, 5.43]]
FCC_AL_PRIMITIVE = [[0.0, 2.025, 2.025], [2.025, 0.0, 2.025], [2.025, 2.025, 0.0]]
GRAPHENE = [[2.46, 0.0, 0.0], [-1.23, 2.46 * math.sqrt(3) / 2, 0.0], [0.0, 0.0, 20.0]]


class MeshDensityTests(unittest.TestCase):
    def test_denser_tier_never_samples_more_coarsely(self) -> None:
        tiers = {
            tier["id"]: tier
            for tier in precision.all_tier_settings(CUBIC_SI, electronic_type="semiconductor")
        }

        self.assertLessEqual(tiers["quick"]["kpoint_count"], tiers["standard"]["kpoint_count"])
        self.assertLessEqual(tiers["standard"]["kpoint_count"], tiers["high"]["kpoint_count"])
        self.assertLessEqual(tiers["quick"]["encut"], tiers["standard"]["encut"])
        self.assertLessEqual(tiers["standard"]["encut"], tiers["high"]["encut"])

    def test_small_cell_gets_denser_mesh_than_large_cell(self) -> None:
        # The reciprocal-density rule must give the 1-atom fcc primitive cell a
        # finer mesh than the 8-atom cubic cell of a comparable material.
        small = precision.tier_settings("standard", FCC_AL_PRIMITIVE, electronic_type="semiconductor")
        large = precision.tier_settings("standard", CUBIC_SI, electronic_type="semiconductor")

        self.assertGreater(small["kmesh"][0], large["kmesh"][0])

    def test_metals_are_sampled_more_finely_than_insulators(self) -> None:
        metal = precision.tier_settings("standard", CUBIC_SI, electronic_type="metal")
        insulator = precision.tier_settings("standard", CUBIC_SI, electronic_type="insulator")

        self.assertGreater(metal["kpoint_count"], insulator["kpoint_count"])

    def test_dos_mesh_is_at_least_as_dense_as_scf_mesh(self) -> None:
        for tier_id in precision.PRECISION_TIERS:
            tier = precision.tier_settings(tier_id, CUBIC_SI, electronic_type="metal")
            for dos_value, scf_value in zip(tier["dos_kmesh"], tier["kmesh"]):
                self.assertGreaterEqual(dos_value, scf_value, msg=f"tier={tier_id}")

    def test_mesh_counts_stay_within_bounds(self) -> None:
        tiny = [[0.9, 0.0, 0.0], [0.0, 0.9, 0.0], [0.0, 0.0, 0.9]]
        tier = precision.tier_settings("high", tiny, electronic_type="metal")

        for value in tier["kmesh"] + tier["dos_kmesh"]:
            self.assertGreaterEqual(value, 1)
            self.assertLessEqual(value, precision.MAX_MESH_PER_AXIS)


class NonPeriodicDirectionTests(unittest.TestCase):
    def test_slab_gets_a_single_kpoint_along_the_vacuum_axis(self) -> None:
        tier = precision.tier_settings(
            "standard", GRAPHENE, material_class="2d", vacuum_axis="c", electronic_type="semiconductor"
        )

        self.assertEqual(tier["kmesh"][2], 1)
        self.assertEqual(tier["dos_kmesh"][2], 1)
        self.assertEqual(tier["phonon_supercell"][2], 1)
        self.assertGreater(tier["kmesh"][0], 1)

    def test_slab_vacuum_axis_a_is_respected(self) -> None:
        lattice = [[20.0, 0.0, 0.0], [0.0, 2.46, 0.0], [0.0, 0.0, 2.46]]
        tier = precision.tier_settings("standard", lattice, material_class="slab", vacuum_axis="a")

        self.assertEqual(tier["kmesh"][0], 1)
        self.assertGreater(tier["kmesh"][1], 1)

    def test_molecule_is_gamma_only(self) -> None:
        box = [[15.0, 0.0, 0.0], [0.0, 15.0, 0.0], [0.0, 0.0, 15.0]]
        tier = precision.tier_settings("high", box, material_class="molecule")

        self.assertEqual(tier["kmesh"], [1, 1, 1])
        self.assertEqual(tier["dos_kmesh"], [1, 1, 1])
        self.assertEqual(tier["phonon_supercell"], [1, 1, 1])


class EncutTests(unittest.TestCase):
    def test_encut_is_derived_from_potcar_enmax(self) -> None:
        value, note, from_potcar = precision.encut_for_tier(precision.PRECISION_TIERS["standard"], 400.0)

        self.assertEqual(value, 520.0)  # 1.3 x 400, already a multiple of 10
        self.assertIn("400", note)
        self.assertTrue(from_potcar)

    def test_encut_rounds_up_to_ten(self) -> None:
        value, _, _ = precision.encut_for_tier(precision.PRECISION_TIERS["high"], 259.0)

        self.assertEqual(value, 390.0)  # 1.5 x 259 = 388.5 -> 390

    def test_encut_falls_back_and_explains_why(self) -> None:
        value, note, from_potcar = precision.encut_for_tier(precision.PRECISION_TIERS["standard"], None)

        self.assertEqual(value, 520.0)
        self.assertIn("no POTCAR", note)
        self.assertFalse(from_potcar)

    def test_every_tier_clears_the_pulay_safety_floor(self) -> None:
        # ENCUT is global and INCAR.relax / INCAR.elastic run at ISIF=3, so a
        # cutoff near ENMAX would bias the relaxed cell through Pulay stress.
        enmax = 245.3
        for tier_id in precision.PRECISION_TIERS:
            value, _, _ = precision.encut_for_tier(precision.PRECISION_TIERS[tier_id], enmax)
            self.assertGreaterEqual(
                value,
                enmax * precision.ENCUT_RELAXATION_SAFETY_FACTOR,
                msg=f"tier={tier_id} would trip the submission audit's 1.3 x ENMAX rule",
            )


class PhononDosMeshTests(unittest.TestCase):
    def test_phonon_dos_qmesh_is_not_derived_from_the_electronic_mesh(self) -> None:
        # phonopy's --mesh is a cheap interpolation grid over the primitive zone.
        # Deriving it from the supercell k-mesh made Standard and High identical
        # and left both far too coarse for a converged DOS.
        tiers = {
            tier["id"]: tier for tier in precision.all_tier_settings(CUBIC_SI, electronic_type="semiconductor")
        }

        self.assertNotEqual(tiers["standard"]["phonon_dos_kmesh"], tiers["high"]["phonon_dos_kmesh"])
        self.assertLess(tiers["quick"]["phonon_dos_kmesh"][0], tiers["standard"]["phonon_dos_kmesh"][0])
        for tier_id, tier in tiers.items():
            self.assertGreater(
                tier["phonon_dos_kmesh"][0],
                tier["phonon_kmesh"][0],
                msg=f"tier={tier_id}: a q-mesh for DOS interpolation should be far denser than the SCF supercell mesh",
            )

    def test_phonon_dos_qmesh_respects_non_periodic_axes(self) -> None:
        tier = precision.tier_settings("high", GRAPHENE, material_class="2d", vacuum_axis="c")

        self.assertEqual(tier["phonon_dos_kmesh"][2], 1)


class SupercellTests(unittest.TestCase):
    def test_supercell_reaches_the_target_thickness(self) -> None:
        dims = precision.supercell_for_target(CUBIC_SI, 10.0)

        self.assertEqual(dims, [2, 2, 2])  # 2 x 5.43 = 10.86 A >= 10 A

    def test_supercell_is_capped(self) -> None:
        tiny = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        dims = precision.supercell_for_target(tiny, 100.0)

        self.assertEqual(dims, [precision.MAX_SUPERCELL_PER_AXIS] * 3)

    def test_supercell_atom_count_is_reported(self) -> None:
        tier = precision.tier_settings("standard", CUBIC_SI, atom_count=8)

        self.assertEqual(tier["supercell_atom_count"], 8 * 2 * 2 * 2)

    def test_a_capped_supercell_admits_it_missed_the_target(self) -> None:
        # fcc Al primitive: 4 x 2.34 A = 9.4 A, short of the 12 A High target.
        tier = precision.tier_settings("high", FCC_AL_PRIMITIVE, atom_count=1)

        self.assertEqual(tier["phonon_supercell"], [precision.MAX_SUPERCELL_PER_AXIS] * 3)
        self.assertTrue(
            any("capped" in note for note in tier["notes"]),
            msg="a clamped supercell must say so instead of claiming the target was met",
        )

    def test_an_uncapped_supercell_does_not_warn(self) -> None:
        tier = precision.tier_settings("standard", CUBIC_SI, atom_count=8)

        self.assertFalse(any("capped" in note for note in tier["notes"]))


class TierContractTests(unittest.TestCase):
    def test_unknown_tier_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            precision.tier_settings("ultra", CUBIC_SI)

    def test_every_tier_exposes_the_fields_the_form_needs(self) -> None:
        required = {
            "id", "label", "tagline", "description", "kmesh", "dos_kmesh",
            "phonon_kmesh", "phonon_dos_kmesh", "phonon_supercell", "encut",
            "band_points", "wallclock_seconds", "kpoint_count", "notes",
        }
        for tier in precision.all_tier_settings(CUBIC_SI):
            self.assertTrue(required.issubset(tier.keys()), msg=tier["id"])
            self.assertTrue(tier["notes"])


if __name__ == "__main__":
    unittest.main()
