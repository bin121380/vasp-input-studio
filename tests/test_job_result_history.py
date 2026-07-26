import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main


POSCAR_TEXT = """Si
1.0
5.43 0 0
0 5.43 0
0 0 5.43
Si
1
Direct
0 0 0
"""

PHONON_BAND_YAML = """nqpoint: 2
npath: 1
phonon:
- q-position: [0.0, 0.0, 0.0]
  distance: 0.0
  band:
  - # 1
    frequency: -0.5
  - # 2
    frequency: 1.0
- q-position: [0.5, 0.0, 0.0]
  distance: 1.0
  band:
  - # 1
    frequency: 0.2
  - # 2
    frequency: 1.5
"""

TOTAL_DOS_TEXT = """# freq dos
-0.5 0.10
0.0 0.20
1.0 0.30
"""

MESH_YAML = "mesh: [4, 4, 4]\n"
RUN_MANIFEST_TEXT = "{\n  \"step\": \"scf\"\n}\n"
SCF_OUTCAR_COMPLETE = "OUTCAR tail\nGeneral timing and accounting informations for this job:\n"


class JobResultHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-job-history-")
        self.base = Path(self.tempdir.name)
        self.jobs_file = self.base / "jobs.json"
        self.jobs_file.write_text("[]\n", encoding="utf-8")
        self.system_dir = self.base / "Si"
        self.system_dir.mkdir(parents=True, exist_ok=True)
        (self.system_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_live_phonon_result(self) -> None:
        run_dir = self.system_dir / "runs" / "phonon"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "FORCE_SETS").write_text("forces\n", encoding="utf-8")
        (run_dir / "band.yaml").write_text(PHONON_BAND_YAML, encoding="utf-8")
        (run_dir / "total_dos.dat").write_text(TOTAL_DOS_TEXT, encoding="utf-8")
        (run_dir / "mesh.yaml").write_text(MESH_YAML, encoding="utf-8")
        dis_dir = run_dir / "dis-001"
        dis_dir.mkdir(parents=True, exist_ok=True)
        (dis_dir / "vasprun.xml").write_text("<vasprun />\n", encoding="utf-8")

    def _write_archived_phonon_result(self, job_id: str) -> Path:
        archive_dir = self.system_dir / main.JOB_RESULT_ARCHIVE_DIRNAME / "phonon" / job_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "FORCE_SETS").write_text("forces\n", encoding="utf-8")
        (archive_dir / "band.yaml").write_text(PHONON_BAND_YAML, encoding="utf-8")
        (archive_dir / "total_dos.dat").write_text(TOTAL_DOS_TEXT, encoding="utf-8")
        (archive_dir / "mesh.yaml").write_text(MESH_YAML, encoding="utf-8")
        return archive_dir

    def _write_archived_scf_result(self, job_id: str) -> Path:
        archive_dir = self.system_dir / main.JOB_RESULT_ARCHIVE_DIRNAME / "scf" / job_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "OUTCAR").write_text(SCF_OUTCAR_COMPLETE, encoding="utf-8")
        (archive_dir / "OSZICAR").write_text("oszicar\n", encoding="utf-8")
        (archive_dir / "DOSCAR").write_text("doscar\n", encoding="utf-8")
        (archive_dir / "run_manifest.json").write_text(RUN_MANIFEST_TEXT, encoding="utf-8")
        return archive_dir

    def test_non_latest_job_does_not_reuse_live_results(self) -> None:
        self._write_live_phonon_result()
        jobs = [
            {
                "id": "Si-phonon-latest",
                "system": "Si",
                "step": "phonon",
                "state": "finished",
                "created_at": "2026-04-08T08:40:31",
                "finished_at": "2026-04-08T08:40:31",
            },
            {
                "id": "Si-phonon-old",
                "system": "Si",
                "step": "phonon",
                "state": "finished",
                "created_at": "2026-04-03T17:21:32",
                "finished_at": "2026-04-03T17:21:32",
            },
        ]
        self.jobs_file.write_text(json.dumps(jobs, indent=2) + "\n", encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertTrue(main.job_results_ready(jobs[0]))
            self.assertFalse(main.job_results_ready(jobs[1]))
            payload = main.job_result_context_payload(jobs[1])

        self.assertFalse(payload["available"])
        self.assertIsNone(payload["source_kind"])
        self.assertIn("only the job log can be shown", payload["source_note"])

    def test_archived_job_context_uses_archive_directory(self) -> None:
        old_job = {
            "id": "Si-phonon-old",
            "system": "Si",
            "step": "phonon",
            "state": "finished",
            "created_at": "2026-04-03T17:21:32",
            "finished_at": "2026-04-03T17:21:32",
        }
        latest_job = {
            "id": "Si-phonon-latest",
            "system": "Si",
            "step": "phonon",
            "state": "finished",
            "created_at": "2026-04-08T08:40:31",
            "finished_at": "2026-04-08T08:40:31",
        }
        self.jobs_file.write_text(json.dumps([latest_job, old_job], indent=2) + "\n", encoding="utf-8")
        archive_dir = self._write_archived_phonon_result(old_job["id"])

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertTrue(main.job_results_ready(old_job))
            payload = main.job_result_context_payload(old_job)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["source_kind"], "archive")
        self.assertEqual(payload["source_run_dir"], archive_dir.relative_to(self.system_dir).as_posix())
        self.assertEqual(payload["phonon_visualization"]["status"], "plot_ready")
        self.assertEqual(payload["phonon_dos_visualization"]["status"], "plot_ready")
        self.assertEqual(payload["result_highlights"][0]["title"], "Phonon Stability")

    def test_phonon_visualization_keeps_ticks_when_kpath_sampling_differs(self) -> None:
        self._write_live_phonon_result()
        (self.system_dir / "KPATH.in").write_text(
            "Generated path\n81\nLine-mode\nReciprocal\n0 0 0 GAMMA\n0.5 0 0 X\n",
            encoding="utf-8",
        )

        payload = main.phonon_visualization(self.system_dir)

        self.assertIsNotNone(payload)
        self.assertEqual([point[0] for point in payload["series"][0]["points"]], [0.0, 1.0])
        self.assertEqual([item["label"] for item in payload["x_ticks"]], ["Γ", "X"])
        self.assertAlmostEqual(payload["x_ticks"][0]["x"], 0.0)
        self.assertAlmostEqual(payload["x_ticks"][1]["x"], 1.0)
        self.assertEqual(payload["display_x_ticks"], payload["x_ticks"])
        self.assertEqual(payload["verticals"], [0.0, 1.0])

    def test_archived_scf_context_surfaces_previewable_files(self) -> None:
        old_job = {
            "id": "Si-scf-old",
            "system": "Si",
            "step": "scf",
            "state": "finished",
            "created_at": "2026-04-03T17:21:32",
            "finished_at": "2026-04-03T17:21:32",
        }
        latest_job = {
            "id": "Si-scf-new",
            "system": "Si",
            "step": "scf",
            "state": "running",
            "created_at": "2026-04-08T08:40:31",
        }
        self.jobs_file.write_text(json.dumps([latest_job, old_job], indent=2) + "\n", encoding="utf-8")
        archive_dir = self._write_archived_scf_result(old_job["id"])

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertTrue(main.job_results_ready(old_job))
            payload = main.job_result_context_payload(old_job)
            preview_path = main.preview_file_path(
                self.system_dir,
                f"{main.JOB_RESULT_ARCHIVE_DIRNAME}/scf/{old_job['id']}/OUTCAR",
            )

        self.assertEqual(preview_path, archive_dir / "OUTCAR")
        self.assertIn(
            f"{main.JOB_RESULT_ARCHIVE_DIRNAME}/scf/{old_job['id']}/OUTCAR",
            payload["preview_files"],
        )
        self.assertTrue(
            any(
                item["path"] == f"{main.JOB_RESULT_ARCHIVE_DIRNAME}/scf/{old_job['id']}/OUTCAR"
                and item["historical_step"] == "scf"
                for item in payload["tree"]
            )
        )

    def test_non_latest_aiida_job_archives_from_attempt_remote_directory(self) -> None:
        old_job = {
            "id": "Si-scf-old",
            "system": "Si",
            "step": "scf",
            "state": "finished",
            "backend": "aiida",
            "process_pk": 1234,
            "created_at": "2026-04-03T17:21:32",
            "finished_at": "2026-04-03T18:21:32",
        }
        latest_job = {
            "id": "Si-scf-new",
            "system": "Si",
            "step": "scf",
            "state": "running",
            "backend": "aiida",
            "process_pk": 5678,
            "created_at": "2026-04-08T08:40:31",
        }
        self.jobs_file.write_text(json.dumps([latest_job, old_job], indent=2) + "\n", encoding="utf-8")

        remote_dir = self.base / "remote-scf-old"
        remote_dir.mkdir(parents=True, exist_ok=True)
        (remote_dir / "OUTCAR").write_text(SCF_OUTCAR_COMPLETE, encoding="utf-8")
        (remote_dir / "OSZICAR").write_text("oszicar\n", encoding="utf-8")
        (remote_dir / "DOSCAR").write_text("doscar\n", encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file), patch.object(
            main,
            "aiida_remote_path",
            return_value=remote_dir,
        ):
            archive_dir = main._maybe_archive_job_results(old_job)

        self.assertIsNotNone(archive_dir)
        self.assertEqual(archive_dir, self.system_dir / main.JOB_RESULT_ARCHIVE_DIRNAME / "scf" / old_job["id"])
        self.assertTrue((archive_dir / "OUTCAR").exists())
        self.assertTrue((archive_dir / "DOSCAR").exists())

    def test_local_archive_copies_launcher_log_and_exit_code_metadata(self) -> None:
        job = {
            "id": "Si-scf-local",
            "system": "Si",
            "step": "scf",
            "state": "finished",
            "target": "local",
            "backend": "legacy",
            "created_at": "2026-04-27T10:00:00",
            "finished_at": "2026-04-27T11:00:00",
        }
        self.jobs_file.write_text(json.dumps([job], indent=2) + "\n", encoding="utf-8")

        run_dir = self.system_dir / "runs" / "scf"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "OUTCAR").write_text(SCF_OUTCAR_COMPLETE, encoding="utf-8")
        (run_dir / "OSZICAR").write_text("oszicar\n", encoding="utf-8")
        (run_dir / "DOSCAR").write_text("doscar\n", encoding="utf-8")
        (run_dir / "exit_code.json").write_text(
            json.dumps({"status": "finished", "step": "scf", "exit_code": 0}, indent=2) + "\n",
            encoding="utf-8",
        )

        runtime_dir = self.base / "runtime"
        job_logs = runtime_dir / "job_logs"
        job_logs.mkdir(parents=True, exist_ok=True)
        launcher_log = job_logs / f"{job['id']}.log"
        launcher_log.write_text("Finished scf in local test\n", encoding="utf-8")
        job["launcher_log"] = str(launcher_log)

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file), patch.object(
            main,
            "RUNTIME_DIR",
            runtime_dir,
        ):
            archive_dir = main._maybe_archive_job_results(job)

        self.assertIsNotNone(archive_dir)
        self.assertTrue((archive_dir / "exit_code.json").exists())
        self.assertTrue((archive_dir / "launcher.log").exists())
        self.assertIn("Finished scf", (archive_dir / "launcher.log").read_text(encoding="utf-8"))

    def test_non_latest_local_job_can_read_from_dedicated_attempt_directory(self) -> None:
        old_job = {
            "id": "Si-scf-old",
            "system": "Si",
            "step": "scf",
            "state": "finished",
            "target": "local",
            "backend": "legacy",
            "attempt_dir": f"{main.LOCAL_ATTEMPT_WORKDIR_DIRNAME}/scf/Si-scf-old",
            "created_at": "2026-04-03T17:21:32",
            "finished_at": "2026-04-03T17:21:32",
        }
        latest_job = {
            "id": "Si-scf-new",
            "system": "Si",
            "step": "scf",
            "state": "running",
            "target": "local",
            "backend": "legacy",
            "attempt_dir": f"{main.LOCAL_ATTEMPT_WORKDIR_DIRNAME}/scf/Si-scf-new",
            "created_at": "2026-04-08T08:40:31",
        }
        self.jobs_file.write_text(json.dumps([latest_job, old_job], indent=2) + "\n", encoding="utf-8")
        attempt_dir = self.system_dir / main.LOCAL_ATTEMPT_WORKDIR_DIRNAME / "scf" / old_job["id"]
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "OUTCAR").write_text(SCF_OUTCAR_COMPLETE, encoding="utf-8")
        (attempt_dir / "DOSCAR").write_text("doscar\n", encoding="utf-8")
        (attempt_dir / "OSZICAR").write_text("oszicar\n", encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            payload = main.job_result_context_payload(old_job)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["source_kind"], "attempt")
        self.assertEqual(payload["source_run_dir"], f"{main.LOCAL_ATTEMPT_WORKDIR_DIRNAME}/scf/{old_job['id']}")


if __name__ == "__main__":
    unittest.main()
