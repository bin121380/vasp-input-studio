import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app import main, schedulers


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-security-")
        self.base = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_preview_file_path_rejects_parent_traversal_into_sibling_project(self) -> None:
        system_dir = self.base / "Si"
        sibling_dir = self.base / "Si2"
        (system_dir / "runs" / "scf").mkdir(parents=True, exist_ok=True)
        sibling_dir.mkdir(parents=True, exist_ok=True)
        (sibling_dir / "OUTCAR").write_text("secret\n", encoding="utf-8")

        with self.assertRaises(HTTPException) as ctx:
            main.preview_file_path(system_dir, "runs/scf/../../../Si2/OUTCAR")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Invalid path")

    def test_read_limited_text_payload_truncates_large_preview(self) -> None:
        path = self.base / "OUTCAR"
        path.write_text("abcdef", encoding="utf-8")

        payload = main.read_limited_text_payload(path, limit=3)

        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["size_bytes"], 6)
        self.assertEqual(payload["preview_limit_bytes"], 3)
        self.assertTrue(payload["content"].startswith("abc"))
        self.assertIn("Preview truncated", payload["content"])

    def test_managed_launcher_log_path_requires_real_runtime_log_ancestor(self) -> None:
        runtime_dir = self.base / "runtime"
        legitimate = runtime_dir / "job_logs" / "job.log"
        rogue = runtime_dir / "job_logs-evil" / "job.log"
        legitimate.parent.mkdir(parents=True, exist_ok=True)
        rogue.parent.mkdir(parents=True, exist_ok=True)
        legitimate.write_text("ok\n", encoding="utf-8")
        rogue.write_text("bad\n", encoding="utf-8")

        with patch.object(main, "RUNTIME_DIR", runtime_dir):
            resolved = main.managed_launcher_log_path({"launcher_log": str(legitimate)})
            rejected = main.managed_launcher_log_path({"launcher_log": str(rogue)})

        self.assertEqual(resolved, legitimate.resolve())
        self.assertIsNone(rejected)

    def test_remote_profile_request_rejects_option_like_host(self) -> None:
        with self.assertRaises(ValidationError):
            main.RemoteProfileRequest(
                name="cluster_a",
                host="-V",
                user="",
                workspace_root="/remote/workspace",
            )

    def test_submit_remote_job_rejects_non_numeric_ssh_pid(self) -> None:
        profile = {
            "name": "cluster_a",
            "host": "cluster-a",
            "user": "",
            "workspace_root": "/remote/workspace",
            "scheduler_kind": "ssh",
            "vasp_mpi_np": 1,
            "pre_command": "",
        }

        result = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": "not-a-pid\n",
                "stderr": "",
            },
        )()

        with patch.object(schedulers.subprocess, "run", return_value=result):
            with self.assertRaises(schedulers.SchedulerError) as ctx:
                schedulers.submit_remote_job(self.base, self.base / "runtime", profile, "Si", "scf", 1, "job1")

        self.assertIn("valid PID", str(ctx.exception))

    def test_resolve_system_dir_rejects_traversal_names(self) -> None:
        systems_dir = self.base / "systems"
        (systems_dir / "Si").mkdir(parents=True, exist_ok=True)
        (self.base / "outside").mkdir(parents=True, exist_ok=True)

        with patch.object(main, "SYSTEMS_DIR", systems_dir):
            for name in ("..", "../outside", "../../etc", "a/b", "/etc", ".", "", "  "):
                with self.assertRaises(HTTPException, msg=f"name={name!r}") as ctx:
                    main.resolve_system_dir(name)
                self.assertEqual(ctx.exception.status_code, 400, msg=f"name={name!r}")

    def test_resolve_system_dir_accepts_existing_project(self) -> None:
        systems_dir = self.base / "systems"
        (systems_dir / "Si").mkdir(parents=True, exist_ok=True)

        with patch.object(main, "SYSTEMS_DIR", systems_dir):
            resolved = main.resolve_system_dir("Si")
            self.assertEqual(resolved, systems_dir / "Si")

            with self.assertRaises(HTTPException) as ctx:
                main.resolve_system_dir("Missing")
            self.assertEqual(ctx.exception.status_code, 404)

    def test_resolve_system_dir_rejects_symlink_escape(self) -> None:
        systems_dir = self.base / "systems"
        systems_dir.mkdir(parents=True, exist_ok=True)
        outside = self.base / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (systems_dir / "escape").symlink_to(outside)

        with patch.object(main, "SYSTEMS_DIR", systems_dir):
            with self.assertRaises(HTTPException) as ctx:
                main.resolve_system_dir("escape")
            self.assertEqual(ctx.exception.status_code, 400)

    def test_refresh_remote_job_slurm_queries_state_without_crashing(self) -> None:
        profile = {
            "name": "cluster_b",
            "host": "cluster-b",
            "user": "",
            "workspace_root": "/remote/workspace",
            "scheduler_kind": "slurm",
            "vasp_mpi_np": 1,
            "pre_command": "",
        }
        job = {"state": "submitted_remote", "scheduler_job_id": "12345"}

        captured: dict[str, str] = {}

        def fake_query(host: str, command: str) -> str:
            captured["command"] = command
            return "RUNNING\n"

        with patch.object(schedulers, "_run_remote_query", side_effect=fake_query):
            refreshed = schedulers.refresh_remote_job(profile, job)

        self.assertEqual(refreshed["remote_status"], "RUNNING")
        self.assertEqual(refreshed["state"], "running")
        self.assertIn("${state:-UNKNOWN}", captured["command"])

    def test_refresh_remote_job_marks_empty_ssh_pid_failed(self) -> None:
        profile = {
            "name": "cluster_a",
            "host": "cluster-a",
            "user": "",
            "workspace_root": "/remote/workspace",
            "scheduler_kind": "ssh",
            "vasp_mpi_np": 1,
            "pre_command": "",
        }
        job = {"state": "submitted_remote", "remote_pid": ""}

        refreshed = schedulers.refresh_remote_job(profile, job)

        self.assertEqual(refreshed["state"], "failed")
        self.assertEqual(refreshed["remote_status"], "INVALID_PID")


if __name__ == "__main__":
    unittest.main()
