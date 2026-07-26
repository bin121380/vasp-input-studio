import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main


class JobCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-job-cleanup-")
        self.base = Path(self.tempdir.name)
        self.jobs_file = self.base / "jobs.json"
        self.jobs_file.write_text("[]\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_jobs(self, jobs: list[dict]) -> None:
        self.jobs_file.write_text(json.dumps(jobs, indent=2) + "\n", encoding="utf-8")

    def _read_jobs(self) -> list[dict]:
        return json.loads(self.jobs_file.read_text(encoding="utf-8"))

    def test_cleanup_prefers_selected_terminal_job_when_other_history_exists(self) -> None:
        jobs = [
            {
                "id": "Sample-scf-failed-new",
                "system": "Sample",
                "step": "scf",
                "state": "stopped",
                "created_at": "2026-04-23T19:54:54",
            },
            {
                "id": "Sample-scf-finished-old",
                "system": "Sample",
                "step": "scf",
                "state": "finished",
                "created_at": "2026-04-23T18:01:00",
            },
        ]
        self._write_jobs(jobs)

        with patch.object(main, "JOBS_FILE", self.jobs_file), patch.object(
            main,
            "_refresh_job_states_in_place",
            return_value=False,
        ), patch.object(
            main,
            "remove_job_runtime_artifacts",
            return_value=None,
        ):
            result = main.cleanup_job_history(
                system="Sample",
                step="scf",
                keep_latest=1,
                selected_job_id="Sample-scf-failed-new",
            )

        remaining_ids = [job["id"] for job in self._read_jobs()]
        self.assertEqual(remaining_ids, ["Sample-scf-finished-old"])
        self.assertEqual(result["removed_job_ids"], ["Sample-scf-failed-new"])
        self.assertTrue(result["selected_job_removed"])

    def test_cleanup_keeps_default_latest_when_selected_job_is_out_of_scope(self) -> None:
        jobs = [
            {
                "id": "Sample-scf-failed-new",
                "system": "Sample",
                "step": "scf",
                "state": "failed",
                "created_at": "2026-04-23T19:54:54",
            },
            {
                "id": "Sample-scf-finished-old",
                "system": "Sample",
                "step": "scf",
                "state": "finished",
                "created_at": "2026-04-23T18:01:00",
            },
            {
                "id": "Sample-dos-failed",
                "system": "Sample",
                "step": "dos",
                "state": "failed",
                "created_at": "2026-04-23T20:00:00",
            },
        ]
        self._write_jobs(jobs)

        with patch.object(main, "JOBS_FILE", self.jobs_file), patch.object(
            main,
            "_refresh_job_states_in_place",
            return_value=False,
        ), patch.object(
            main,
            "remove_job_runtime_artifacts",
            return_value=None,
        ):
            result = main.cleanup_job_history(
                system="Sample",
                step="scf",
                keep_latest=1,
                selected_job_id="Sample-dos-failed",
            )

        remaining_ids = [job["id"] for job in self._read_jobs()]
        self.assertEqual(remaining_ids, ["Sample-scf-failed-new", "Sample-dos-failed"])
        self.assertEqual(result["removed_job_ids"], ["Sample-scf-finished-old"])
        self.assertFalse(result["selected_job_removed"])

    def test_cleanup_does_not_remove_selected_when_it_is_the_only_terminal_record(self) -> None:
        jobs = [
            {
                "id": "Sample-scf-stopped-only",
                "system": "Sample",
                "step": "scf",
                "state": "stopped",
                "created_at": "2026-04-23T19:54:54",
            }
        ]
        self._write_jobs(jobs)

        with patch.object(main, "JOBS_FILE", self.jobs_file), patch.object(
            main,
            "_refresh_job_states_in_place",
            return_value=False,
        ), patch.object(
            main,
            "remove_job_runtime_artifacts",
            return_value=None,
        ):
            result = main.cleanup_job_history(
                system="Sample",
                step="scf",
                keep_latest=1,
                selected_job_id="Sample-scf-stopped-only",
            )

        remaining_ids = [job["id"] for job in self._read_jobs()]
        self.assertEqual(remaining_ids, ["Sample-scf-stopped-only"])
        self.assertEqual(result["removed_job_ids"], [])
        self.assertFalse(result["selected_job_removed"])

    def test_remove_job_runtime_artifacts_deletes_attempt_directory(self) -> None:
        system_dir = self.base / "Sample"
        attempt_dir = system_dir / main.LOCAL_ATTEMPT_WORKDIR_DIRNAME / "scf" / "Sample-scf-20260427120000"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "OUTCAR").write_text("outcar\n", encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base):
            main.remove_job_runtime_artifacts(
                {
                    "id": "Sample-scf-20260427120000",
                    "system": "Sample",
                    "step": "scf",
                    "target": "local",
                    "backend": "legacy",
                    "attempt_dir": f"{main.LOCAL_ATTEMPT_WORKDIR_DIRNAME}/scf/Sample-scf-20260427120000",
                }
            )

        self.assertFalse(attempt_dir.exists())


if __name__ == "__main__":
    unittest.main()
