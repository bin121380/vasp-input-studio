import hashlib
import tempfile
import unittest
from pathlib import Path

from app import main
from app.persistence import read_json


POSCAR_TEXT = """Si
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


class RunManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-run-manifest-")
        self.system_dir = Path(self.tempdir.name) / "Si"
        self.system_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_write_run_manifest_persists_effective_params_and_version(self) -> None:
        (self.system_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "metadata.json").write_text('{"formula": "Si", "electronic_type": "semiconductor"}\n', encoding="utf-8")
        (self.system_dir / "INCAR.dos").write_text("ISMEAR = -5\nSIGMA = 0.05\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.dos").write_text("Automatic mesh\n0\nGamma\n9 9 9\n0 0 0\n", encoding="utf-8")
        potcar_text = "TITEL  = PAW_PBE Si 05Jan2001\n   ENMAX  =  400.000; ENMIN  =  300.000 eV\n"
        (self.system_dir / "POTCAR").write_text(potcar_text, encoding="utf-8")
        record = {
            "id": "Si-dos-20260322010101",
            "target": "local",
            "backend": "legacy",
            "submission_mode": "legacy",
            "mpi_np": 1,
            "state": "running",
            "created_at": "2026-03-22T01:01:01",
            "risk_level": "review",
            "readiness": "review",
            "warnings": ["Electronic type is still auto."],
            "effective_params": [
                {"name": "ISMEAR", "value": "-5", "source": "platform DOS default", "source_kind": "platform_default"},
                {"name": "KPOINTS", "value": "9 9 9", "source": "1.5x SCF mesh", "source_kind": "derived_dos_mesh"},
            ],
            "resume": False,
            "resumed_from": None,
        }

        manifest_path = main._write_run_manifest(self.system_dir, "dos", record)
        payload = read_json(manifest_path, {})

        self.assertEqual(manifest_path, self.system_dir / "runs" / "dos" / "run_manifest.json")
        self.assertEqual(payload["app_version"], main.settings.app_version)
        self.assertEqual(payload["system"], "Si")
        self.assertEqual(payload["step"], "dos")
        self.assertEqual(payload["job_id"], record["id"])
        self.assertEqual(payload["manifest_version"], 1)
        self.assertEqual(payload["resolved_inputs"]["structure"], "POSCAR")
        self.assertIn("input_snapshots", payload)
        self.assertEqual(payload["input_snapshots"]["POSCAR"]["content"], POSCAR_TEXT)
        self.assertEqual(payload["input_snapshots"]["INCAR.dos"]["content"], "ISMEAR = -5\nSIGMA = 0.05\n")
        self.assertEqual(payload["input_snapshots"]["KPOINTS.dos"]["content"], "Automatic mesh\n0\nGamma\n9 9 9\n0 0 0\n")
        self.assertEqual(payload["input_snapshots"]["metadata.json"]["content"], '{"formula": "Si", "electronic_type": "semiconductor"}\n')
        self.assertEqual(payload["potcar_summary"]["titles"], ["Si"])
        self.assertEqual(payload["potcar_summary"]["max_enmax"], 400.0)
        self.assertEqual(payload["potcar_summary"]["sha256"], hashlib.sha256(potcar_text.encode("utf-8")).hexdigest())
        self.assertEqual(payload["effective_params"][0]["source_kind"], "platform_default")
        self.assertEqual(payload["effective_params"][1]["source_kind"], "derived_dos_mesh")

    def test_write_run_manifest_keeps_aiida_specific_fields(self) -> None:
        (self.system_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "INCAR.band").write_text("ISMEAR = 0\nSIGMA = 0.05\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.band").write_text("Band path\n121\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n", encoding="utf-8")
        (self.system_dir / "KPATH.in").write_text("Band path\n121\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n", encoding="utf-8")
        record = {
            "id": "Si-band-aiida-20260322020202",
            "target": "local",
            "backend": "aiida",
            "submission_mode": "aiida",
            "profile": "vasp_studio_pg",
            "workchain": "vasp.relax",
            "code_label": "vasp-std-6.4.3",
            "process_pk": 1234,
            "process_uuid": "00000000-0000-0000-0000-000000001234",
            "process_label": "VaspWorkChain",
            "mpi_np": 1,
            "state": "queued",
            "created_at": "2026-03-22T02:02:02",
            "warnings": [],
            "effective_params": [],
            "resume": False,
            "resumed_from": None,
        }

        manifest_path = main._write_run_manifest(self.system_dir, "band", record)
        payload = read_json(manifest_path, {})

        self.assertEqual(payload["backend"], "aiida")
        self.assertEqual(payload["submission_mode"], "aiida")
        self.assertEqual(payload["profile"], "vasp_studio_pg")
        self.assertEqual(payload["process_pk"], 1234)
        self.assertEqual(payload["process_label"], "VaspWorkChain")
        self.assertIn("KPOINTS.band", payload["input_snapshots"])
        self.assertIn("KPATH.in", payload["input_snapshots"])

    def test_write_run_manifest_for_hse_band_snapshots_mesh_and_kpoints_opt(self) -> None:
        (self.system_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "metadata.json").write_text('{"formula": "Si", "xc_electronic": "HSE06"}\n', encoding="utf-8")
        (self.system_dir / "INCAR.band").write_text("LHFCALC = .TRUE.\nHFSCREEN = 0.2\nAEXX = 0.25\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.scf").write_text("Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.band").write_text("Band path\n121\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n", encoding="utf-8")
        (self.system_dir / "KPATH.in").write_text("Band path\n121\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n", encoding="utf-8")

        manifest_path = main._write_run_manifest(
            self.system_dir,
            "band",
            {
                "id": "Si-band-hse-20260322030303",
                "target": "local",
                "backend": "legacy",
                "submission_mode": "legacy",
                "mpi_np": 1,
                "state": "queued",
                "created_at": "2026-03-22T03:03:03",
                "warnings": [],
                "effective_params": [],
                "resume": False,
                "resumed_from": None,
            },
        )
        payload = read_json(manifest_path, {})

        self.assertEqual(payload["resolved_inputs"]["kpoints"], "KPOINTS.scf")
        self.assertEqual(payload["resolved_inputs"]["kpoints_opt"], "KPOINTS.band")
        self.assertIn("KPOINTS.scf", payload["input_snapshots"])
        self.assertIn("KPOINTS.band", payload["input_snapshots"])


if __name__ == "__main__":
    unittest.main()
