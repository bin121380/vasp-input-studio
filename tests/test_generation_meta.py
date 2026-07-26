import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json

from app import input_generation, materials


POSCAR_TEXT = """SrTiO3
1.0
3.905 0 0
0 3.905 0
0 0 3.905
Sr Ti O
1 1 3
Direct
0 0 0
0.5 0.5 0.5
0.5 0.5 0
0.5 0 0.5
0 0.5 0.5
"""


class GenerationMetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-generation-meta-")
        self.system_dir = Path(self.tempdir.name) / "SrTiO3"
        self.system_dir.mkdir(parents=True, exist_ok=True)
        (self.system_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "metadata.json").write_text('{"formula": "SrTiO3", "band_points": 101}\n', encoding="utf-8")
        (self.system_dir / "KPATH.in").write_text("Generated path\n101\nLine-mode\nReciprocal\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_recorded_generated_file_is_fresh(self) -> None:
        content = "Automatic mesh\n"
        (self.system_dir / "KPOINTS.scf").write_text(content, encoding="utf-8")

        materials.record_generated_file(self.system_dir, "KPOINTS.scf", content)
        state = materials.file_generation_state(self.system_dir, "KPOINTS.scf")

        self.assertTrue(state["fresh"])
        self.assertFalse(state["manual_override"])
        self.assertFalse(state["stale"])
        self.assertEqual(state["tags"], ["generated"])

    def test_manual_edit_marks_generated_file_manual(self) -> None:
        path = self.system_dir / "KPOINTS.scf"
        path.write_text("Automatic mesh\n", encoding="utf-8")
        materials.record_generated_file(self.system_dir, "KPOINTS.scf", "Automatic mesh\n")

        path.write_text("Manual mesh\n", encoding="utf-8")
        state = materials.file_generation_state(self.system_dir, "KPOINTS.scf")

        self.assertTrue(state["manual_override"])
        self.assertFalse(state["stale"])
        self.assertIn("manual", state["tags"])

    def test_metadata_change_marks_generated_file_stale(self) -> None:
        path = self.system_dir / "KPOINTS.scf"
        path.write_text("Automatic mesh\n", encoding="utf-8")
        materials.record_generated_file(self.system_dir, "KPOINTS.scf", "Automatic mesh\n")

        (self.system_dir / "metadata.json").write_text('{"formula": "SrTiO3", "band_points": 151}\n', encoding="utf-8")
        state = materials.file_generation_state(self.system_dir, "KPOINTS.scf")

        self.assertFalse(state["manual_override"])
        self.assertTrue(state["stale"])
        self.assertIn("metadata.json", state["changed_sources"])

    def test_sync_metadata_from_kpoints_scf_updates_kmesh(self) -> None:
        (self.system_dir / "metadata.json").write_text('{"formula": "SrTiO3", "kmesh": [10, 10, 10]}\n', encoding="utf-8")
        content = "Automatic mesh\n0\nGamma\n6 6 6\n0 0 0\n"

        updated = materials.sync_metadata_from_kpoints_scf(self.system_dir, content)
        metadata = json.loads((self.system_dir / "metadata.json").read_text(encoding="utf-8"))

        self.assertEqual(updated, ["kmesh"])
        self.assertEqual(metadata["kmesh"], [6, 6, 6])

    def test_relax_contcar_change_marks_kpath_stale(self) -> None:
        relax_dir = self.system_dir / "runs" / "relax"
        relax_dir.mkdir(parents=True, exist_ok=True)
        contcar = relax_dir / "CONTCAR"
        contcar.write_text(POSCAR_TEXT, encoding="utf-8")

        content = "Generated path\n101\nLine-mode\nReciprocal\n"
        (self.system_dir / "KPATH.in").write_text(content, encoding="utf-8")
        materials.record_generated_file(self.system_dir, "KPATH.in", content)

        contcar.write_text(POSCAR_TEXT.replace("3.905 0 0", "4.00 0 0"), encoding="utf-8")
        state = materials.file_generation_state(self.system_dir, "KPATH.in")

        self.assertTrue(state["stale"])
        self.assertIn("runs/relax/CONTCAR", state["changed_sources"])

    def test_custom_kpath_ignores_structure_source_changes(self) -> None:
        relax_dir = self.system_dir / "runs" / "relax"
        relax_dir.mkdir(parents=True, exist_ok=True)
        contcar = relax_dir / "CONTCAR"
        contcar.write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "formula": "SrTiO3",
                    "band_points": 101,
                    "band_path_mode": "custom",
                    "band_path_text": "Paper path\n101\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        content = "Paper path\n101\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n"
        (self.system_dir / "KPATH.in").write_text(content, encoding="utf-8")
        materials.record_generated_file(self.system_dir, "KPATH.in", content)

        contcar.write_text(POSCAR_TEXT.replace("3.905 0 0", "4.00 0 0"), encoding="utf-8")
        state = materials.file_generation_state(self.system_dir, "KPATH.in")

        self.assertFalse(state["stale"])
        self.assertNotIn("runs/relax/CONTCAR", state["changed_sources"])

    def test_kpath_change_marks_band_conf_stale(self) -> None:
        path = self.system_dir / "band.conf"
        path.write_text("Band config\n", encoding="utf-8")
        materials.record_generated_file(self.system_dir, "band.conf", "Band config\n")

        (self.system_dir / "KPATH.in").write_text("Manual path\n", encoding="utf-8")
        state = materials.file_generation_state(self.system_dir, "band.conf")

        self.assertTrue(state["stale"])
        self.assertIn("KPATH.in", state["changed_sources"])

    def test_refresh_generated_inputs_if_needed_regenerates_stale_kpath(self) -> None:
        relax_dir = self.system_dir / "runs" / "relax"
        relax_dir.mkdir(parents=True, exist_ok=True)
        contcar = relax_dir / "CONTCAR"
        contcar.write_text(POSCAR_TEXT, encoding="utf-8")

        original = "Generated path\n101\nLine-mode\nReciprocal\n"
        regenerated = "Regenerated path\n101\nLine-mode\nReciprocal\n"
        path = self.system_dir / "KPATH.in"
        path.write_text(original, encoding="utf-8")
        materials.record_generated_file(self.system_dir, "KPATH.in", original)
        contcar.write_text(POSCAR_TEXT.replace("3.905 0 0", "4.00 0 0"), encoding="utf-8")

        with patch.object(materials, "generate_input_content", return_value={"content": regenerated}):
            result = materials.refresh_generated_inputs_if_needed(self.system_dir, ["KPATH.in"])

        self.assertEqual(result["regenerated"], ["KPATH.in"])
        self.assertEqual(path.read_text(encoding="utf-8"), regenerated)
        state = materials.file_generation_state(self.system_dir, "KPATH.in")
        self.assertTrue(state["fresh"])

    def test_untracked_generated_file_is_reported(self) -> None:
        (self.system_dir / "KPOINTS.dos").write_text("Legacy file\n", encoding="utf-8")

        state = materials.file_generation_state(self.system_dir, "KPOINTS.dos")

        self.assertFalse(state["tracked"])
        self.assertIn("untracked", state["tags"])

    def test_material_form_payload_exposes_default_phonon_dos_mesh(self) -> None:
        payload = materials.material_form_payload(self.system_dir, backend={})

        self.assertEqual(payload["phonon_kmesh_text"], "4 4 4")
        self.assertEqual(payload["phonon_dos_kmesh_text"], "8 8 8")

    def test_sync_metadata_from_band_conf_updates_supercell_and_band_points(self) -> None:
        (self.system_dir / "metadata.json").write_text(
            json.dumps({"formula": "SrTiO3", "phonon_supercell": [2, 2, 2], "band_points": 101}, indent=2) + "\n",
            encoding="utf-8",
        )

        updated = materials.sync_metadata_from_band_conf(
            self.system_dir,
            "ATOM_NAME = Sr Ti O\nDIM = 1 1 1\nBAND_POINTS = 81\n",
        )

        metadata = json.loads((self.system_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(updated, ["phonon_supercell", "band_points"])
        self.assertEqual(metadata["phonon_supercell"], [1, 1, 1])
        self.assertEqual(metadata["band_points"], 81)
        self.assertTrue((self.system_dir / "metadata.json.bak").exists())

    def test_generate_phonon_incar_uses_strict_force_defaults(self) -> None:
        payload = input_generation.generate_input_content(
            self.system_dir,
            "INCAR.phonon",
            metadata={
                "formula": "SrTiO3",
                "material_class": "bulk",
                "electronic_type": "semiconductor",
                "xc_geometry": "PBE",
                "xc_electronic": "PBE",
                "spin_polarized": False,
                "encut": 520,
                "phonon_supercell": [2, 2, 2],
            },
        )

        self.assertIn("ISYM = 0", payload["content"])
        self.assertIn("IBRION = -1", payload["content"])
        self.assertIn("NSW = 0", payload["content"])
        self.assertIn("LREAL = .FALSE.", payload["content"])
        self.assertIn("EDIFF = 1e-08", payload["content"])
        self.assertIn("ISMEAR = 0", payload["content"])
        self.assertIn("SIGMA = 0.01", payload["content"])

    def test_save_material_settings_persists_custom_band_path(self) -> None:
        payload = {
            "formula": "SrTiO3",
            "material_class": "bulk",
            "electronic_type": "metal",
            "xc_geometry": "PBE",
            "xc_electronic": "PBE",
            "spin_polarized": True,
            "potcar_family": "PBE_64",
            "potcar_profile": "conservative",
            "potcar_mapping_text": "Sr=Sr_sv\nTi=Ti\nO=O",
            "kmesh_text": "19 19 19",
            "dos_kmesh_text": "21 21 21",
            "phonon_kmesh_text": "4 4 4",
            "phonon_dos_kmesh_text": "8 8 8",
            "phonon_supercell_text": "2 2 2",
            "magmom_text": "0 3 0 0 0",
            "band_points": 101,
            "band_kpoints_distance": 0.05,
            "band_path_mode": "custom",
            "band_path_text": "Paper path\n101\nLine-Mode\nReciprocal\n0.0 0.0 0.0 GAMMA\n0.0 0.5 0.0 X\n\n0.0 0.5 0.0 X\n0.5 0.5 0.0 M\n",
            "wallclock_seconds": 43200,
            "advanced_overrides_json": "",
        }

        with patch.object(
            materials,
            "planned_generated_inputs",
            return_value={"KPATH.in": "Paper path\n", "KPOINTS.band": "Band mesh\n"},
        ), patch.object(
            materials,
            "write_generated_inputs",
            return_value=["KPATH.in", "KPOINTS.band"],
        ):
            materials.save_material_settings(self.system_dir, payload, backend={})

        form_payload = materials.material_form_payload(self.system_dir, backend={})
        self.assertEqual(form_payload["band_path_mode"], "custom")
        self.assertIn("Paper path", form_payload["band_path_text"])
        self.assertIn("\n101\n", form_payload["band_path_text"])

    def test_save_material_settings_uses_planned_kpath_for_band_conf(self) -> None:
        (self.system_dir / "KPATH.in").write_text(
            "Old path\n101\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n",
            encoding="utf-8",
        )
        payload = {
            "formula": "SrTiO3",
            "material_class": "bulk",
            "electronic_type": "metal",
            "xc_geometry": "PBE",
            "xc_electronic": "PBE",
            "spin_polarized": True,
            "potcar_family": "PBE_64",
            "potcar_profile": "conservative",
            "potcar_mapping_text": "Sr=Sr_sv\nTi=Ti\nO=O",
            "kmesh_text": "19 19 19",
            "dos_kmesh_text": "21 21 21",
            "phonon_kmesh_text": "4 4 4",
            "phonon_dos_kmesh_text": "8 8 8",
            "phonon_supercell_text": "2 2 2",
            "magmom_text": "0 3 0 0 0",
            "band_points": 101,
            "band_kpoints_distance": 0.05,
            "band_path_mode": "custom",
            "band_path_text": "Paper path\n101\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0 0.5 0 ! Y\n",
            "wallclock_seconds": 43200,
            "advanced_overrides_json": "",
        }

        materials.save_material_settings(self.system_dir, payload, backend={})

        kpath_text = (self.system_dir / "KPATH.in").read_text(encoding="utf-8")
        band_conf = (self.system_dir / "band.conf").read_text(encoding="utf-8")
        self.assertIn("0 0.5 0", kpath_text)
        self.assertIn("BAND = 0.00000000 0.00000000 0.00000000  0.00000000 0.50000000 0.00000000", band_conf)
        self.assertNotIn("0.50000000 0.00000000 0.00000000", band_conf)

    def test_save_material_settings_writes_metadata_backup(self) -> None:
        original = '{"formula": "SrTiO3", "band_points": 101}\n'
        (self.system_dir / "metadata.json").write_text(original, encoding="utf-8")
        payload = {
            "formula": "SrTiO3-updated",
            "material_class": "bulk",
            "electronic_type": "metal",
            "xc_geometry": "PBE",
            "xc_electronic": "PBE",
            "spin_polarized": False,
            "potcar_family": "PBE_64",
            "potcar_profile": "conservative",
            "potcar_mapping_text": "Sr=Sr_sv\nTi=Ti\nO=O",
            "kmesh_text": "19 19 19",
            "dos_kmesh_text": "21 21 21",
            "phonon_kmesh_text": "4 4 4",
            "phonon_dos_kmesh_text": "8 8 8",
            "phonon_supercell_text": "2 2 2",
            "magmom_text": "0 0 0 0 0",
            "band_points": 101,
            "band_kpoints_distance": 0.05,
            "band_path_mode": "auto",
            "band_path_text": "",
            "wallclock_seconds": 43200,
            "advanced_overrides_json": "",
        }

        with patch.object(
            materials,
            "planned_generated_inputs",
            return_value={"KPATH.in": "Generated path\n", "KPOINTS.band": "Band mesh\n"},
        ), patch.object(
            materials,
            "write_generated_inputs",
            return_value=["KPATH.in", "KPOINTS.band"],
        ):
            materials.save_material_settings(self.system_dir, payload, backend={})

        backup = self.system_dir / "metadata.json.bak"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_save_material_settings_persists_phonon_dos_mesh(self) -> None:
        payload = {
            "formula": "SrTiO3",
            "material_class": "bulk",
            "electronic_type": "metal",
            "xc_geometry": "PBE",
            "xc_electronic": "PBE",
            "spin_polarized": False,
            "potcar_family": "PBE_64",
            "potcar_profile": "conservative",
            "potcar_mapping_text": "Sr=Sr_sv\nTi=Ti\nO=O",
            "kmesh_text": "19 19 19",
            "dos_kmesh_text": "21 21 21",
            "phonon_kmesh_text": "4 4 4",
            "phonon_dos_kmesh_text": "9 9 9",
            "phonon_supercell_text": "2 2 2",
            "magmom_text": "0 0 0 0 0",
            "band_points": 101,
            "band_kpoints_distance": 0.05,
            "band_path_mode": "auto",
            "band_path_text": "",
            "wallclock_seconds": 43200,
            "advanced_overrides_json": "",
        }

        with patch.object(
            materials,
            "planned_generated_inputs",
            return_value={"KPATH.in": "Generated path\n", "KPOINTS.band": "Band mesh\n"},
        ), patch.object(
            materials,
            "write_generated_inputs",
            return_value=["KPATH.in", "KPOINTS.band"],
        ):
            materials.save_material_settings(self.system_dir, payload, backend={})

        saved_metadata = json.loads((self.system_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_metadata["phonon_dos_kmesh"], [9, 9, 9])

    def test_save_material_settings_keeps_metadata_unchanged_when_generation_plan_fails(self) -> None:
        original = '{"formula": "SrTiO3", "band_points": 101}\n'
        (self.system_dir / "metadata.json").write_text(original, encoding="utf-8")
        payload = {
            "formula": "SrTiO3-updated",
            "material_class": "bulk",
            "electronic_type": "metal",
            "xc_geometry": "PBE",
            "xc_electronic": "PBE",
            "spin_polarized": False,
            "potcar_family": "PBE_64",
            "potcar_profile": "conservative",
            "potcar_mapping_text": "Sr=Sr_sv\nTi=Ti\nO=O",
            "kmesh_text": "19 19 19",
            "dos_kmesh_text": "21 21 21",
            "phonon_kmesh_text": "4 4 4",
            "phonon_dos_kmesh_text": "8 8 8",
            "phonon_supercell_text": "2 2 2",
            "magmom_text": "0 0 0 0 0",
            "band_points": 101,
            "band_kpoints_distance": 0.05,
            "band_path_mode": "auto",
            "band_path_text": "",
            "wallclock_seconds": 43200,
            "advanced_overrides_json": "",
        }

        with patch.object(materials, "planned_generated_inputs", side_effect=ValueError("planned generation failed")):
            with self.assertRaisesRegex(ValueError, "planned generation failed"):
                materials.save_material_settings(self.system_dir, payload, backend={})

        self.assertEqual((self.system_dir / "metadata.json").read_text(encoding="utf-8"), original)
        self.assertFalse((self.system_dir / "metadata.json.bak").exists())

    def test_refresh_structure_inputs_preserves_mapping_and_manual_overrides(self) -> None:
        (self.system_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "formula": "SrTiO3",
                    "potcar_mapping": {"Sr": "Sr_sv", "Ti": "Ti_pv", "O": "O"},
                    "band_points": 101,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        manual_path = self.system_dir / "INCAR.band"
        manual_path.write_text("Generated INCAR.band\n", encoding="utf-8")
        materials.record_generated_file(self.system_dir, "INCAR.band", "Generated INCAR.band\n")
        manual_path.write_text("Manual INCAR.band\n", encoding="utf-8")

        def fake_generate_input_content(
            system_dir: Path,
            relative_path: str,
            metadata: dict[str, object] | None = None,
            *,
            planned_inputs: dict[str, str] | None = None,
        ) -> dict[str, str]:
            return {"content": f"Generated {relative_path}\n"}

        with patch.object(materials, "generate_input_content", side_effect=fake_generate_input_content):
            result = materials.refresh_structure_inputs(self.system_dir)

        metadata = json.loads((self.system_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["potcar_mapping"], {"Sr": "Sr_sv", "Ti": "Ti_pv", "O": "O"})
        self.assertEqual(manual_path.read_text(encoding="utf-8"), "Manual INCAR.band\n")
        self.assertNotIn("INCAR.band", result["generated_files"])
        self.assertEqual((self.system_dir / "KPOINTS.scf").read_text(encoding="utf-8"), "Generated KPOINTS.scf\n")


if __name__ == "__main__":
    unittest.main()
