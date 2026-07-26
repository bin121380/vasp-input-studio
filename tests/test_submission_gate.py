import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main


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
SCF_OUTCAR_COMPLETE = "OUTCAR tail\nGeneral timing and accounting informations for this job:\n"


class SubmissionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-submission-gate-")
        self.base = Path(self.tempdir.name)
        self.jobs_file = self.base / "jobs.json"
        self.jobs_file.write_text("[]\n", encoding="utf-8")
        self.system_dir = self.base / "Si"
        self.system_dir.mkdir(parents=True, exist_ok=True)
        (self.system_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "formula": "Si",
                    "material_class": "bulk",
                    "kmesh": [8, 8, 8],
                    "phonon_kmesh": [4, 4, 4],
                    "phonon_dos_kmesh": [8, 8, 8],
                    "phonon_supercell": [2, 2, 2],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.system_dir / "POTCAR").write_text("TITEL  = PAW_PBE Si 05Jan2001\nENMAX = 400.000\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.phonon").write_text("Automatic mesh\n0\nGamma\n4 4 4\n0 0 0\n", encoding="utf-8")
        (self.system_dir / "INCAR.phonon").write_text("ISPIN = 1\nENCUT = 650\nLREAL = .FALSE.\n", encoding="utf-8")
        (self.system_dir / "band.conf").write_text("ATOM_NAME = Si\nDIM = 2 2 2\n", encoding="utf-8")
        (self.system_dir / "runs" / "relax").mkdir(parents=True, exist_ok=True)
        (self.system_dir / "runs" / "relax" / "CONTCAR").write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "runs" / "scf").mkdir(parents=True, exist_ok=True)
        (self.system_dir / "runs" / "scf" / "CHGCAR").write_text("charge\n", encoding="utf-8")
        (self.system_dir / "runs" / "scf" / "OUTCAR").write_text(SCF_OUTCAR_COMPLETE, encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _memory_with_available_gb(value: float) -> object:
        return type("VirtualMemory", (), {"available": int(value * 1024**3)})()

    def test_submit_job_rejects_unsupported_step_before_launch(self) -> None:
        request = main.SubmitJobRequest(system="Si", step="bogus", target="local", mpi_np=1)

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(main.submit_job(request))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Unsupported step", ctx.exception.detail)

    def test_phonon_gate_blocks_manifest_backed_spin_mismatch(self) -> None:
        manifest = {
            "input_snapshots": {
                "INCAR.scf": {
                    "content": "ISPIN = 2\nENCUT = 600\n",
                }
            }
        }
        run_manifest = self.system_dir / "runs" / "scf" / "run_manifest.json"
        run_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(self.system_dir, "phonon", "local", 1)

        self.assertEqual(gate["readiness"], "blocked")
        self.assertTrue(
            any("spin polarization" in message and "completed scf run manifest" in message for message in gate["blocking_errors"])
        )

    def test_phonon_gate_warns_when_only_workspace_inputs_disagree(self) -> None:
        (self.system_dir / "INCAR.scf").write_text("ISPIN = 2\nENCUT = 600\n", encoding="utf-8")

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(self.system_dir, "phonon", "local", 1)

        self.assertFalse(any("spin polarization" in message for message in gate["blocking_errors"]))
        self.assertTrue(any("spin polarization" in message for message in gate["warnings"]))

    def test_phonon_gate_uses_band_conf_dim_for_magmom_count(self) -> None:
        (self.system_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "formula": "Si",
                    "material_class": "bulk",
                    "kmesh": [8, 8, 8],
                    "phonon_kmesh": [4, 4, 4],
                    "phonon_dos_kmesh": [8, 8, 8],
                    "phonon_supercell": [2, 2, 2],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.system_dir / "band.conf").write_text("ATOM_NAME = Si\nDIM = 1 1 1\n", encoding="utf-8")
        (self.system_dir / "INCAR.phonon").write_text("ISPIN = 2\nMAGMOM = 2*1.0\nENCUT = 650\n", encoding="utf-8")

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(self.system_dir, "phonon", "local", 1)

        self.assertFalse(any("MAGMOM count" in message for message in gate["blocking_errors"]))

    def test_phonon_gate_blocks_lreal_true(self) -> None:
        (self.system_dir / "INCAR.phonon").write_text("ISPIN = 1\nENCUT = 650\nEDIFF = 1E-8\nLREAL = .TRUE.\n", encoding="utf-8")

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(self.system_dir, "phonon", "local", 1)

        self.assertEqual(gate["readiness"], "blocked")
        self.assertTrue(any("LREAL != .FALSE." in message for message in gate["blocking_errors"]))

    def test_phonon_gate_blocks_nac_without_born_when_band_conf_requests_it(self) -> None:
        (self.system_dir / "INCAR.phonon").write_text("ISPIN = 1\nENCUT = 650\nEDIFF = 1E-8\nLREAL = .FALSE.\n", encoding="utf-8")
        (self.system_dir / "band.conf").write_text("ATOM_NAME = Si\nDIM = 2 2 2\nNAC = .TRUE.\n", encoding="utf-8")

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(self.system_dir, "phonon", "local", 1)

        self.assertEqual(gate["readiness"], "blocked")
        self.assertTrue(any("band.conf requests NAC = .TRUE." in message for message in gate["blocking_errors"]))

    def test_local_mpi_within_detected_cores_does_not_force_np_one(self) -> None:
        resources = {"physical_cores": 8, "logical_cores": 16, "available_cores": 16, "default_mpi_np": 1}

        with (
            patch.object(main, "JOBS_FILE", self.jobs_file),
            patch.object(main, "local_cpu_resources", return_value=resources),
            patch.object(main.psutil, "virtual_memory", return_value=self._memory_with_available_gb(32)),
        ):
            gate = main.submission_gate(self.system_dir, "phonon", "local", 4)

        warning_text = " ".join(gate["warnings"])
        self.assertNotIn("np=1", warning_text)
        self.assertNotIn("For this workstation", warning_text)
        self.assertNotIn("small workstation", warning_text)
        self.assertFalse(any("MPI NP" in message and "exceeds" in message for message in gate["warnings"]))

    def test_local_mpi_warns_only_when_exceeding_available_cores(self) -> None:
        resources = {"physical_cores": 4, "logical_cores": 8, "available_cores": 8, "default_mpi_np": 1}

        with (
            patch.object(main, "JOBS_FILE", self.jobs_file),
            patch.object(main, "local_cpu_resources", return_value=resources),
            patch.object(main.psutil, "virtual_memory", return_value=self._memory_with_available_gb(32)),
        ):
            gate = main.submission_gate(self.system_dir, "phonon", "local", 12)

        self.assertEqual(gate["risk_level"], "high")
        self.assertTrue(any("MPI NP = 12 exceeds the 8 CPU core(s)" in message for message in gate["warnings"]))

    def test_local_hse_warning_uses_detected_resources_not_np_one_rule(self) -> None:
        metadata = json.loads((self.system_dir / "metadata.json").read_text(encoding="utf-8"))
        metadata["xc_electronic"] = "HSE06"
        (self.system_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        resources = {"physical_cores": 8, "logical_cores": 16, "available_cores": 16, "default_mpi_np": 1}

        with (
            patch.object(main, "JOBS_FILE", self.jobs_file),
            patch.object(main, "local_cpu_resources", return_value=resources),
            patch.object(main.psutil, "virtual_memory", return_value=self._memory_with_available_gb(32)),
        ):
            gate = main.submission_gate(self.system_dir, "scf", "local", 4)

        warning_text = " ".join(gate["warnings"])
        self.assertIn("Detected local CPU", warning_text)
        self.assertNotIn("np=1", warning_text)
        self.assertNotIn("On this workstation", warning_text)

    def test_hse_band_requires_mesh_kpoints_input(self) -> None:
        (self.system_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "formula": "Si",
                    "material_class": "bulk",
                    "xc_electronic": "HSE06",
                    "kmesh": [8, 8, 8],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.system_dir / "INCAR.band").write_text("LHFCALC = .TRUE.\nHFSCREEN = 0.2\nAEXX = 0.25\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.band").write_text("Band path\n121\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n", encoding="utf-8")
        (self.system_dir / "KPATH.in").write_text("Band path\n121\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n", encoding="utf-8")
        (self.system_dir / "runs" / "scf" / "WAVECAR").write_text("wavecar\n", encoding="utf-8")
        (self.system_dir / "runs" / "scf" / "OUTCAR").write_text(
            "LHFCALC = T\nHFSCREEN = 0.2\nGeneral timing and accounting informations for this job:\n",
            encoding="utf-8",
        )

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(self.system_dir, "band", "local", 1)

        self.assertEqual(gate["readiness"], "blocked")
        self.assertTrue(any("HSE06 band requires the SCF mesh input KPOINTS.scf." in message for message in gate["blocking_errors"]))

    def test_prepare_band_inputs_blocks_untracked_kpoints_band(self) -> None:
        with patch.object(
            main,
            "refresh_generated_inputs_if_needed",
            return_value={
                "regenerated": [],
                "errors": [],
                "decisions": {
                    "KPOINTS.band": {"reason": "untracked", "state": {"tracked": False}},
                    "KPATH.in": {"reason": "fresh", "state": {}},
                },
            },
        ):
            result = main._prepare_band_inputs_for_submission(self.system_dir)

        self.assertTrue(any("KPOINTS.band predates freshness tracking" in message for message in result["blocking_errors"]))

    def test_prepare_band_inputs_blocks_manual_stale_kpoints_band(self) -> None:
        with patch.object(
            main,
            "refresh_generated_inputs_if_needed",
            return_value={
                "regenerated": [],
                "errors": [],
                "decisions": {
                    "KPOINTS.band": {"reason": "manual_override", "state": {"manual_override": True, "stale": True}},
                    "KPATH.in": {"reason": "fresh", "state": {}},
                },
            },
        ):
            result = main._prepare_band_inputs_for_submission(self.system_dir)

        self.assertTrue(any("KPOINTS.band was edited after generation" in message for message in result["blocking_errors"]))

    def test_launch_local_band_applies_preflight_warnings(self) -> None:
        dummy_proc = type("DummyProc", (), {"pid": 2468})()
        with patch.object(
            main,
            "_prepare_step_inputs_for_submission",
            return_value={"warnings": ["Auto-regenerated band inputs before submission: KPOINTS.band."], "blocking_errors": [], "regenerated": ["KPOINTS.band"], "decisions": {}},
        ), patch.object(
            main,
            "SYSTEMS_DIR",
            self.base,
        ), patch.object(
            main,
            "submission_gate",
            return_value={"warnings": [], "risk_level": "ok", "blocking_errors": [], "readiness": "ready"},
        ), patch.object(
            main,
            "supports_aiida_submission",
            return_value=False,
        ), patch.object(
            main,
            "local_legacy_runner_issue",
            return_value=None,
        ), patch.object(
            main,
            "local_legacy_runner_path",
            return_value=Path("/bin/true"),
        ), patch.object(
            main.subprocess,
            "Popen",
            return_value=dummy_proc,
        ), patch.object(
            main,
            "_attach_run_manifest",
            return_value={},
        ), patch.object(
            main,
            "_mutate_jobs",
            return_value=None,
        ):
            record = main.launch_local_step("Si", "band", 1)

        self.assertIn("Auto-regenerated band inputs before submission: KPOINTS.band.", record["warnings"])
        self.assertTrue(record["attempt_dir"].startswith(f"{main.LOCAL_ATTEMPT_WORKDIR_DIRNAME}/band/Si-band-"))
        self.assertTrue(record["target_log"].endswith("/log"))

    def test_launch_local_band_stops_on_preflight_blocker(self) -> None:
        with patch.object(
            main,
            "_prepare_step_inputs_for_submission",
            return_value={"warnings": [], "blocking_errors": ["KPOINTS.band predates freshness tracking."], "regenerated": [], "decisions": {}},
        ), patch.object(
            main,
            "SYSTEMS_DIR",
            self.base,
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.launch_local_step("Si", "band", 1)

        self.assertIn("KPOINTS.band predates freshness tracking.", ctx.exception.detail)

    def test_prepare_dos_inputs_warns_manual_stale_kpoints_dos(self) -> None:
        with patch.object(
            main,
            "refresh_generated_inputs_if_needed",
            return_value={
                "regenerated": [],
                "errors": [],
                "decisions": {
                    "INCAR.dos": {"reason": "fresh", "state": {}},
                    "KPOINTS.dos": {"reason": "manual_override", "state": {"manual_override": True, "stale": True}},
                },
            },
        ):
            result = main._prepare_step_inputs_for_submission(self.system_dir, "dos", apply_changes=False)

        self.assertFalse(result["blocking_errors"])
        self.assertTrue(any("KPOINTS.dos was edited after generation" in message for message in result["warnings"]))

    def test_prepare_scf_inputs_warns_untracked_kpoints_scf(self) -> None:
        with patch.object(
            main,
            "refresh_generated_inputs_if_needed",
            return_value={
                "regenerated": [],
                "errors": [],
                "decisions": {
                    "INCAR.scf": {"reason": "fresh", "state": {}},
                    "KPOINTS.scf": {"reason": "untracked", "state": {"tracked": False}},
                },
            },
        ):
            result = main._prepare_step_inputs_for_submission(self.system_dir, "scf", apply_changes=False)

        self.assertFalse(result["blocking_errors"])
        self.assertTrue(any("KPOINTS.scf predates freshness tracking" in message for message in result["warnings"]))

    def test_dos_gate_blocks_when_completed_scf_baseline_drifted(self) -> None:
        incar_scf = self.system_dir / "INCAR.scf"
        kpoints_scf = self.system_dir / "KPOINTS.scf"
        incar_scf.write_text("ENCUT = 520\n", encoding="utf-8")
        kpoints_scf.write_text("Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n", encoding="utf-8")
        (self.system_dir / "INCAR.dos").write_text("ISMEAR = -5\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.dos").write_text("Automatic mesh\n0\nGamma\n10 10 10\n0 0 0\n", encoding="utf-8")

        manifest = {
            "resolved_inputs": {
                "structure": "POSCAR",
                "incar": "INCAR.scf",
                "kpoints": "KPOINTS.scf",
            },
            "input_snapshots": {
                "POSCAR": {"content": POSCAR_TEXT},
                "INCAR.scf": {"content": "ENCUT = 520\n"},
                "KPOINTS.scf": {"content": "Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n"},
            },
            "potcar_summary": {
                "sha256": main._sha256_bytes((self.system_dir / "POTCAR").read_bytes()),
            },
        }
        (self.system_dir / "runs" / "scf" / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        incar_scf.write_text("ENCUT = 600\n", encoding="utf-8")

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(self.system_dir, "dos", "local", 1)

        self.assertEqual(gate["readiness"], "blocked")
        self.assertTrue(
            any(
                "Completed scf baseline no longer matches the current INCAR input (INCAR.scf)." in message
                for message in gate["blocking_errors"]
            )
        )

    def test_scf_gate_allows_current_kpoints_to_differ_from_relax_baseline(self) -> None:
        (self.system_dir / "INCAR.scf").write_text("ENCUT = 520\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.scf").write_text("Automatic mesh\n0\nGamma\n6 6 6\n0 0 0\n", encoding="utf-8")
        manifest = {
            "resolved_inputs": {
                "structure": "POSCAR",
                "incar": "INCAR.relax",
                "kpoints": "KPOINTS.scf",
            },
            "input_snapshots": {
                "POSCAR": {"content": POSCAR_TEXT},
                "INCAR.relax": {"content": "ENCUT = 520\n"},
                "KPOINTS.scf": {"content": "Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n"},
            },
            "potcar_summary": {
                "sha256": main._sha256_bytes((self.system_dir / "POTCAR").read_bytes()),
            },
        }
        (self.system_dir / "INCAR.relax").write_text("ENCUT = 520\n", encoding="utf-8")
        (self.system_dir / "runs" / "relax" / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            gate = main.submission_gate(self.system_dir, "scf", "local", 1)

        self.assertNotEqual(gate["readiness"], "blocked")
        self.assertFalse(any("Re-run relax before scf" in message for message in gate["blocking_errors"]))
        self.assertTrue(any("different mesh" in message for message in gate["warnings"]))


if __name__ == "__main__":
    unittest.main()
