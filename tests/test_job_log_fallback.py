import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main


class JobLogFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-job-log-fallback-")
        self.base = Path(self.tempdir.name)
        self.jobs_file = self.base / "jobs.json"
        self.jobs_file.write_text("[]\n", encoding="utf-8")
        self.system_dir = self.base / "Sample"
        self.system_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_job_log_falls_back_to_archived_outcar_when_aiida_metadata_is_missing(self) -> None:
        job = {
            "id": "Sample-scf-recovered-20260419221200",
            "system": "Sample",
            "step": "scf",
            "backend": "aiida",
            "state": "finished",
            "display_state": "result_ready",
            "status_summary": "Result files detected",
            "finished_at": "2026-04-19T22:12:00",
        }
        archive_dir = self.system_dir / main.JOB_RESULT_ARCHIVE_DIRNAME / "scf" / job["id"]
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "OUTCAR").write_text("OUTCAR tail\nGeneral timing and accounting informations for this job:\n", encoding="utf-8")
        self.jobs_file.write_text(json.dumps([job], indent=2) + "\n", encoding="utf-8")

        with patch.object(main, "SYSTEMS_DIR", self.base), patch.object(main, "JOBS_FILE", self.jobs_file), patch.object(
            main,
            "refresh_job_states",
            return_value=[job],
        ), patch.object(
            main,
            "aiida_job_log_payload",
            side_effect=RuntimeError("Missing AiiDA process PK"),
        ):
            payload = asyncio.run(main.job_log(job["id"]))

        self.assertEqual(payload["job_id"], job["id"])
        self.assertEqual(payload["display_state"], "result_ready")
        self.assertIn("Unable to read AiiDA process log: Missing AiiDA process PK", payload["log_notice"])
        self.assertIn("Showing the local tail of OUTCAR instead.", payload["log_notice"])
        self.assertIn("General timing and accounting informations for this job", payload["content"])


if __name__ == "__main__":
    unittest.main()
