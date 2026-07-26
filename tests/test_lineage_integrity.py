import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main


POSCAR_SI2 = """Si
1.0
5.43 0 0
0 5.43 0
0 0 5.43
Si
2
Direct
0 0 0
0.25 0.25 0.25
"""

POSCAR_SI1 = """Si
1.0
3.84 0 0
0 3.84 0
0 0 3.84
Si
1
Direct
0 0 0
"""

POTCAR_SI = "TITEL  = PAW_PBE Si 05Jan2001\nENMAX = 400.000\n"
POTCAR_GE = "TITEL  = PAW_PBE Ge 05Jan2001\nENMAX = 400.000\n"
SCF_OUTCAR_COMPLETE = "OUTCAR tail\nGeneral timing and accounting informations for this job:\n"


class LineageIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-lineage-")
        self.base = Path(self.tempdir.name)
        self.jobs_file = self.base / "jobs.json"
        self.jobs_file.write_text("[]\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_system(self, name: str, poscar_text: str = POSCAR_SI2) -> Path:
        system_dir = self.base / name
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "POSCAR").write_text(poscar_text, encoding="utf-8")
        return system_dir

    def test_fork_settings_writes_lineage_and_regenerates_clean_inputs(self) -> None:
        source_dir = self._write_system("Source")
        (source_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "formula": "Source",
                    "material_class": "slab",
                    "electronic_type": "metal",
                    "kmesh": [9, 9, 1],
                    "phonon_kmesh": [3, 3, 1],
                    "phonon_dos_kmesh": [6, 6, 1],
                    "phonon_supercell": [2, 2, 1],
                    "potcar_profile": "semicore",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (source_dir / ".generation_meta.json").write_text('{"copied": "bad"}\n', encoding="utf-8")
        (source_dir / "INCAR.scf").write_text("SOURCE_ONLY = 1\n", encoding="utf-8")
        (source_dir / "POTCAR").write_text(POTCAR_SI, encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            payload = main.create_project(
                main.ProjectCreateRequest(
                    name="Forked",
                    creation_mode="fork_settings",
                    source_system="Source",
                    material_class="bulk",
                )
            )

        target_dir = self.base / "Forked"
        lineage = json.loads((target_dir / ".system_lineage.json").read_text(encoding="utf-8"))
        metadata = json.loads((target_dir / "metadata.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["name"], "Forked")
        self.assertEqual(lineage["parent_system"], "Source")
        self.assertEqual(lineage["source_kind"], "fork_settings")
        self.assertEqual(lineage["structure_source"], "POSCAR")
        self.assertEqual(metadata["material_class"], "slab")
        if (target_dir / "POTCAR").exists():
            self.assertNotEqual(
                (target_dir / "POTCAR").read_text(encoding="utf-8"),
                POTCAR_SI,
            )
        self.assertNotIn("SOURCE_ONLY", (target_dir / "INCAR.scf").read_text(encoding="utf-8"))
        self.assertNotIn('"copied"', (target_dir / ".generation_meta.json").read_text(encoding="utf-8"))

    def test_adopt_child_modes_copy_relax_and_primitive_structures_with_lineage(self) -> None:
        source_dir = self._write_system("Parent", POSCAR_SI2)
        (source_dir / "metadata.json").write_text('{"formula": "Parent", "material_class": "bulk"}\n', encoding="utf-8")
        relax_dir = source_dir / "runs" / "relax"
        relax_dir.mkdir(parents=True, exist_ok=True)
        (relax_dir / "CONTCAR").write_text(POSCAR_SI2.replace("5.43", "5.50", 1), encoding="utf-8")
        (relax_dir / "PRIMCELL.vasp").write_text(POSCAR_SI1, encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            main.create_project(
                main.ProjectCreateRequest(
                    name="RelaxChild",
                    creation_mode="adopt_relax_child",
                    source_system="Parent",
                    material_class="bulk",
                )
            )
            main.create_project(
                main.ProjectCreateRequest(
                    name="PrimitiveChild",
                    creation_mode="adopt_primitive_child",
                    source_system="Parent",
                    material_class="bulk",
                )
            )

        relax_child = self.base / "RelaxChild"
        primitive_child = self.base / "PrimitiveChild"
        relax_lineage = json.loads((relax_child / ".system_lineage.json").read_text(encoding="utf-8"))
        primitive_lineage = json.loads((primitive_child / ".system_lineage.json").read_text(encoding="utf-8"))

        self.assertEqual((relax_child / "POSCAR").read_text(encoding="utf-8").strip(), (source_dir / "runs" / "relax" / "CONTCAR").read_text(encoding="utf-8").strip())
        self.assertEqual((primitive_child / "POSCAR").read_text(encoding="utf-8").strip(), POSCAR_SI1.strip())
        self.assertEqual(relax_lineage["structure_variant"], "relax_child")
        self.assertEqual(relax_lineage["adopted_from_step"], "relax")
        self.assertEqual(relax_lineage["structure_source"], "runs/relax/CONTCAR")
        self.assertEqual(primitive_lineage["structure_variant"], "primitive")
        self.assertEqual(primitive_lineage["adopted_from_step"], "relax")
        self.assertEqual(primitive_lineage["structure_source"], "runs/relax/PRIMCELL.vasp")

    def test_submission_gate_blocks_potcar_identity_mismatch(self) -> None:
        system_dir = self._write_system("Mismatch")
        (system_dir / "metadata.json").write_text('{"formula": "Mismatch", "material_class": "bulk"}\n', encoding="utf-8")
        (system_dir / "POTCAR").write_text(POTCAR_GE, encoding="utf-8")
        (system_dir / "KPOINTS.scf").write_text("Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n", encoding="utf-8")
        (system_dir / "INCAR.relax").write_text("ENCUT = 520\n", encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(system_dir, "relax", "local", 1)

        self.assertEqual(gate["readiness"], "blocked")
        self.assertTrue(any("POTCAR species order" in message for message in gate["blocking_errors"]))

    def test_submission_gate_blocks_quarantine_and_phonon_dim_mismatch(self) -> None:
        system_dir = self._write_system("OrphanChild")
        (system_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "formula": "OrphanChild",
                    "material_class": "bulk",
                    "phonon_supercell": [2, 2, 2],
                    "source_template": "Parent",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (system_dir / "POTCAR").write_text(POTCAR_SI, encoding="utf-8")
        (system_dir / "KPOINTS.phonon").write_text("Automatic mesh\n0\nGamma\n4 4 4\n0 0 0\n", encoding="utf-8")
        (system_dir / "INCAR.phonon").write_text("ISPIN = 1\nENCUT = 520\nEDIFF = 1E-8\nLREAL = .FALSE.\n", encoding="utf-8")
        (system_dir / "band.conf").write_text("ATOM_NAME = Si\nDIM = 1 1 1\n", encoding="utf-8")
        relax_dir = system_dir / "runs" / "relax"
        relax_dir.mkdir(parents=True, exist_ok=True)
        (relax_dir / "CONTCAR").write_text(POSCAR_SI2, encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(system_dir, "phonon", "local", 1)

        self.assertEqual(gate["readiness"], "blocked")
        self.assertTrue(any("quarantined" in message for message in gate["blocking_errors"]))
        self.assertTrue(any("band.conf DIM" in message for message in gate["blocking_errors"]))

    def test_integrity_report_and_audit_flag_lineage_identity_and_archive_gaps(self) -> None:
        parent_dir = self._write_system("Parent")
        (parent_dir / "metadata.json").write_text('{"formula": "Parent"}\n', encoding="utf-8")
        parent_relax = parent_dir / "runs" / "relax"
        parent_relax.mkdir(parents=True, exist_ok=True)
        (parent_relax / "CONTCAR").write_text(POSCAR_SI2, encoding="utf-8")

        child_dir = self._write_system("Parent-1")
        (child_dir / "metadata.json").write_text('{"formula": "Parent-1"}\n', encoding="utf-8")
        (child_dir / "POTCAR").write_text(POTCAR_GE, encoding="utf-8")
        self.jobs_file.write_text(
            json.dumps(
                [
                    {
                        "id": "Parent-1-scf-latest",
                        "system": "Parent-1",
                        "step": "scf",
                        "state": "running",
                        "created_at": "2026-04-08T08:40:31",
                    },
                    {
                        "id": "Parent-1-scf-old",
                        "system": "Parent-1",
                        "step": "scf",
                        "state": "finished",
                        "created_at": "2026-04-03T17:21:32",
                        "finished_at": "2026-04-03T17:21:32",
                    },
                ],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            report = main.system_integrity_report(child_dir)
            audit = main.integrity_audit_payload()

        finding_kinds = {item["kind"] for item in report["findings"]}
        self.assertTrue(report["quarantined"])
        self.assertTrue({"lineage_gap", "identity_mismatch", "archive_gap"}.issubset(finding_kinds))
        self.assertTrue(any(item["name"] == "Parent-1" and item["quarantined"] for item in audit["systems"]))
        self.assertIn("lineage_gap", audit["counts_by_kind"])

    def test_step_status_syncs_manifest_when_outputs_are_complete(self) -> None:
        system_dir = self._write_system("SyncMe")
        run_dir = system_dir / "runs" / "scf"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "OUTCAR").write_text(SCF_OUTCAR_COMPLETE, encoding="utf-8")
        (run_dir / "run_manifest.json").write_text(
            json.dumps({"system": "SyncMe", "step": "scf", "state": "queued"}, indent=2) + "\n",
            encoding="utf-8",
        )

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            status = main.step_status(system_dir, "scf")

        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(status, "completed")
        self.assertEqual(manifest["state"], "finished")
        self.assertTrue(manifest["finished_at"])

    def test_run_manifest_records_lineage_and_structure_identity(self) -> None:
        system_dir = self._write_system("Manifest")
        (system_dir / "metadata.json").write_text('{"formula": "Manifest"}\n', encoding="utf-8")
        (system_dir / "INCAR.scf").write_text("ENCUT = 520\n", encoding="utf-8")
        (system_dir / "KPOINTS.scf").write_text("Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n", encoding="utf-8")
        main.write_system_lineage(
            system_dir,
            {
                "system_id": "Manifest",
                "display_name": "Manifest",
                "parent_system": "Parent",
                "source_kind": "fork_settings",
                "structure_variant": "root",
                "structure_source": "POSCAR",
                "adopted_from_step": None,
                "quarantined": False,
                "created_at": "2026-04-21T12:00:00",
            },
        )
        record = {
            "id": "Manifest-scf-20260421120000",
            "state": "queued",
            "created_at": "2026-04-21T12:00:00",
            "warnings": [],
            "effective_params": [],
        }

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            manifest_path = main._write_run_manifest(system_dir, "scf", record)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "queued")
        self.assertEqual(manifest["lineage_snapshot"]["parent_system"], "Parent")
        self.assertEqual(manifest["composition_formula"], "Si2")
        self.assertEqual(manifest["structure_fingerprint"], main._sha256_bytes((system_dir / "POSCAR").read_bytes()))


if __name__ == "__main__":
    unittest.main()
