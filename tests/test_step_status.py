import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app import main


SCF_OUTCAR_COMPLETE = "OUTCAR tail\nGeneral timing and accounting informations for this job:\n"
EIGENVAL_HEADER_ONLY = """   10   10    1    1
  0.9709285E+01  0.5159022E-09  0.5159022E-09  0.5159022E-09  0.5000000E-15
  1.000000000000000E-004
  CAR
 Sample_band
     35    112     24
"""
EIGENVAL_WITH_BANDS = """    9    9    1    2
  0.1115433E+02  0.5216754E-09  0.5216754E-09  0.5216754E-09  0.5000000E-15
  1.000000000000000E-004
  CAR
 Sample_band
     34    486     28

  0.0000000E+00  0.0000000E+00  0.0000000E+00  0.2057613E-02
      1        -39.642115      -39.642115   1.000000   1.000000
      2        -27.658374      -27.658374   1.000000   1.000000
"""


class StepStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-step-status-")
        self.base = Path(self.tempdir.name)
        self.jobs_file = self.base / "jobs.json"
        self.jobs_file.write_text("[]\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _system_dir(self, name: str = "Sample") -> Path:
        system_dir = self.base / name
        system_dir.mkdir(parents=True, exist_ok=True)
        return system_dir

    def _write_jobs(self, jobs: list[dict]) -> None:
        self.jobs_file.write_text(json.dumps(jobs, indent=2) + "\n", encoding="utf-8")

    def _set_mtime(self, path: Path, dt: datetime) -> None:
        timestamp = dt.timestamp()
        os.utime(path, (timestamp, timestamp))

    def test_running_job_overrides_old_completion_marker(self) -> None:
        system_dir = self._system_dir()
        marker = system_dir / "runs" / "relax" / "CONTCAR"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("relaxed\n", encoding="utf-8")
        self._set_mtime(marker, datetime.now() - timedelta(hours=2))
        self._write_jobs(
            [
                {
                    "system": system_dir.name,
                    "step": "relax",
                    "state": "running",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
            ]
        )

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertEqual(main.step_status(system_dir, "relax"), "running")

    def test_newer_failed_job_beats_older_completed_output(self) -> None:
        system_dir = self._system_dir()
        marker = system_dir / "runs" / "scf" / "OUTCAR"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(SCF_OUTCAR_COMPLETE, encoding="utf-8")
        self._set_mtime(marker, datetime.now() - timedelta(hours=3))
        self._write_jobs(
            [
                {
                    "system": system_dir.name,
                    "step": "scf",
                    "state": "failed",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
            ]
        )

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertEqual(main.step_status(system_dir, "scf"), "failed")

    def test_completed_output_beats_older_failed_job(self) -> None:
        system_dir = self._system_dir()
        marker = system_dir / "runs" / "scf" / "OUTCAR"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(SCF_OUTCAR_COMPLETE, encoding="utf-8")
        self._set_mtime(marker, datetime.now())
        self._write_jobs(
            [
                {
                    "system": system_dir.name,
                    "step": "scf",
                    "state": "failed",
                    "created_at": (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
                }
            ]
        )

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertEqual(main.step_status(system_dir, "scf"), "completed")

    def test_scf_restart_charge_density_does_not_count_as_completed(self) -> None:
        system_dir = self._system_dir()
        run_dir = system_dir / "runs" / "scf"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "CHGCAR").write_text("charge\n", encoding="utf-8")
        (run_dir / "OUTCAR").write_text("Iteration 1(1)\n", encoding="utf-8")

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertEqual(main.step_status(system_dir, "scf"), "running_or_partial")

    def test_band_header_only_eigenval_is_not_result_ready(self) -> None:
        system_dir = self._system_dir()
        run_dir = system_dir / "runs" / "band"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "EIGENVAL").write_text(EIGENVAL_HEADER_ONLY, encoding="utf-8")

        self.assertFalse(main._eigenval_has_band_entries(run_dir / "EIGENVAL"))
        self.assertFalse(main._step_results_ready_in_dir(run_dir, "band"))
        self.assertFalse(main._band_completed(run_dir))

    def test_band_eigenval_with_kpoint_and_band_rows_is_result_ready(self) -> None:
        system_dir = self._system_dir()
        run_dir = system_dir / "runs" / "band"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "EIGENVAL").write_text(EIGENVAL_WITH_BANDS, encoding="utf-8")

        self.assertTrue(main._eigenval_has_band_entries(run_dir / "EIGENVAL"))
        self.assertTrue(main._step_results_ready_in_dir(run_dir, "band"))
        self.assertTrue(main._band_completed(run_dir))

    def test_failed_band_with_header_only_eigenval_stays_failed_in_ui(self) -> None:
        system_dir = self._system_dir()
        run_dir = system_dir / "runs" / "band"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "EIGENVAL").write_text(EIGENVAL_HEADER_ONLY, encoding="utf-8")
        job = {
            "id": f"{system_dir.name}-band-20260429000100",
            "system": system_dir.name,
            "step": "band",
            "state": "failed",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._write_jobs([job])

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file):
            annotated = main.annotate_job_record(dict(job))

        self.assertFalse(annotated["result_ready"])
        self.assertEqual(annotated["display_state"], "failed")

    def test_latest_job_for_step_uses_newest_created_at_not_list_order(self) -> None:
        jobs = [
            {
                "id": "Sample-scf-old",
                "system": "Sample",
                "step": "scf",
                "state": "finished",
                "created_at": "2026-04-03T17:21:32",
                "finished_at": "2026-04-03T18:00:00",
            },
            {
                "id": "Sample-scf-new",
                "system": "Sample",
                "step": "scf",
                "state": "failed",
                "created_at": "2026-04-08T08:40:31",
                "finished_at": "2026-04-08T08:45:00",
            },
        ]
        self._write_jobs(jobs[::-1])

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            latest = main.latest_job_for_step("Sample", "scf")

        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], "Sample-scf-new")

    def test_legacy_job_failed_prefers_exit_code_metadata(self) -> None:
        system_dir = self._system_dir()
        run_dir = system_dir / "runs" / "scf"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "exit_code.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "step": "scf",
                    "exit_code": 17,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        job = {
            "id": "Sample-scf-20260427000100",
            "system": system_dir.name,
            "step": "scf",
            "target": "local",
            "backend": "legacy",
            "launcher_log": str(self.base / "job.log"),
        }
        (self.base / "job.log").write_text("launcher finished without explicit error markers\n", encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base):
            self.assertTrue(main.legacy_job_failed(job))

    def test_queued_job_without_run_directory_is_not_started_no_more(self) -> None:
        system_dir = self._system_dir()
        self._write_jobs(
            [
                {
                    "system": system_dir.name,
                    "step": "phonon",
                    "state": "queued",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
            ]
        )

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertEqual(main.step_status(system_dir, "phonon"), "queued")

    def test_missing_run_directory_and_job_is_not_started(self) -> None:
        system_dir = self._system_dir()

        with patch.object(main, "JOBS_FILE", self.jobs_file):
            self.assertEqual(main.step_status(system_dir, "phonon"), "not_started")

    def test_apply_relax_completion_refresh_records_post_relax_updates(self) -> None:
        system_dir = self._system_dir()
        relax_dir = system_dir / "runs" / "relax"
        relax_dir.mkdir(parents=True, exist_ok=True)
        (relax_dir / "CONTCAR").write_text("relaxed\n", encoding="utf-8")
        job = {"system": system_dir.name, "step": "relax", "state": "finished"}

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(
            main,
            "refresh_generated_inputs_if_needed",
            return_value={
                "regenerated": ["KPATH.in"],
                "errors": [],
                "decisions": {
                    "KPATH.in": {
                        "action": "regenerated",
                        "reason": "stale",
                        "state": {"tracked": True, "manual_override": False, "stale": True},
                    }
                },
            },
        ):
            changed = main._apply_relax_completion_refresh(job, "running")

        self.assertTrue(changed)
        self.assertEqual(job["post_relax_refresh"]["regenerated"], ["KPATH.in"])
        self.assertEqual(job["post_relax_refresh"]["decisions"]["KPATH.in"]["reason"], "stale")

    def test_refresh_job_states_triggers_relax_completion_refresh_for_finished_legacy_job(self) -> None:
        system_dir = self._system_dir()
        relax_dir = system_dir / "runs" / "relax"
        relax_dir.mkdir(parents=True, exist_ok=True)
        (relax_dir / "CONTCAR").write_text("relaxed\n", encoding="utf-8")
        jobs = [{"system": system_dir.name, "step": "relax", "state": "running", "pid": 999999}]

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(
            main.psutil,
            "pid_exists",
            return_value=False,
        ), patch.object(
            main,
            "legacy_job_failed",
            return_value=False,
        ), patch.object(
            main,
            "step_completed",
            return_value=True,
        ), patch.object(
            main,
            "_maybe_archive_job_results",
            return_value=None,
        ), patch.object(
            main,
            "job_supported_actions",
            return_value=[],
        ), patch.object(
            main,
            "annotate_job_record",
            side_effect=lambda job: job,
        ), patch.object(
            main,
            "refresh_generated_inputs_if_needed",
            return_value={
                "regenerated": ["KPOINTS.band"],
                "errors": [],
                "decisions": {
                    "KPOINTS.band": {
                        "action": "regenerated",
                        "reason": "stale",
                        "state": {"tracked": True, "manual_override": False, "stale": True},
                    }
                },
            },
        ):
            changed = main._refresh_job_states_in_place(jobs)

        self.assertTrue(changed)
        self.assertEqual(jobs[0]["state"], "finished")
        self.assertEqual(jobs[0]["post_relax_refresh"]["regenerated"], ["KPOINTS.band"])


if __name__ == "__main__":
    unittest.main()
