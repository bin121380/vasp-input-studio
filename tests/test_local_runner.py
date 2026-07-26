import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.runner_assets import bundled_runner_script_path


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


def runner_env(**overrides: str) -> dict[str, str]:
    """Minimal subprocess environment so ambient VASP_*/MPI_* settings on the
    developer machine cannot leak into runner tests."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("VASP", "MPI_", "VASPKIT", "PHONOPY"))
    }
    env.update(overrides)
    return env


class LocalRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-local-runner-")
        self.base = Path(self.tempdir.name)
        self.system_dir = self.base / "Si"
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self.script_path = bundled_runner_script_path("run_step.sh")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_common_system_inputs(self) -> None:
        (self.system_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "POTCAR").write_text("TITEL  = PAW_PBE Si 05Jan2001\nENMAX = 400.000\n", encoding="utf-8")

    def _write_env_script(self, body: str) -> Path:
        path = self.base / "env.sh"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "export VASPKIT_CMD=true\n"
            "export PHONOPY_CMD=true\n"
            "export PYTHON_CMD=python3\n"
            f"{body}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def test_band_requires_completed_scf_baseline_not_just_chgcar(self) -> None:
        self._write_common_system_inputs()
        (self.system_dir / "INCAR.band").write_text("ISMEAR = 0\nSIGMA = 0.05\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.band").write_text(
            "Band path\n121\nLine-mode\nReciprocal\n0 0 0 ! GAMMA\n0.5 0 0 ! X\n",
            encoding="utf-8",
        )
        (self.system_dir / "KPOINTS.scf").write_text("Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n", encoding="utf-8")
        scf_dir = self.system_dir / "runs" / "scf"
        scf_dir.mkdir(parents=True, exist_ok=True)
        (scf_dir / "CHGCAR").write_text("charge\n", encoding="utf-8")
        (scf_dir / "OUTCAR").write_text("Iteration 1(1)\n", encoding="utf-8")
        env_script = self._write_env_script("run_vasp() { :; }\n")

        result = subprocess.run(
            ["bash", str(self.script_path), str(self.system_dir), "band"],
            capture_output=True,
            text=True,
            env=runner_env(VASP_ENV_SH=str(env_script)),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not show a completed SCF baseline", result.stderr)

    def test_scf_run_writes_exit_code_metadata(self) -> None:
        self._write_common_system_inputs()
        (self.system_dir / "INCAR.scf").write_text("ISMEAR = 0\nSIGMA = 0.05\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.scf").write_text("Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n", encoding="utf-8")
        env_script = self._write_env_script(
            "run_vasp() {\n"
            "  printf 'fake vasp\\n'\n"
            "  printf 'General timing and accounting informations for this job:\\n' > OUTCAR\n"
            "  printf 'doscar\\n' > DOSCAR\n"
            "  printf 'charge\\n' > CHGCAR\n"
            "}\n"
        )

        result = subprocess.run(
            ["bash", str(self.script_path), str(self.system_dir), "scf"],
            capture_output=True,
            text=True,
            env=runner_env(VASP_ENV_SH=str(env_script)),
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        payload = json.loads((self.system_dir / "runs" / "scf" / "exit_code.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "finished")
        self.assertEqual(payload["step"], "scf")
        self.assertEqual(payload["exit_code"], 0)
        self.assertIsNotNone(payload["finished_at"])

    def test_scf_run_respects_custom_attempt_work_dir(self) -> None:
        self._write_common_system_inputs()
        (self.system_dir / "INCAR.scf").write_text("ISMEAR = 0\nSIGMA = 0.05\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.scf").write_text("Automatic mesh\n0\nGamma\n8 8 8\n0 0 0\n", encoding="utf-8")
        env_script = self._write_env_script(
            "run_vasp() {\n"
            "  printf 'General timing and accounting informations for this job:\\n' > OUTCAR\n"
            "  printf 'charge\\n' > CHGCAR\n"
            "}\n"
        )
        attempt_dir = self.system_dir / ".attempt_workdirs" / "scf" / "Si-scf-20260427120000"

        result = subprocess.run(
            ["bash", str(self.script_path), str(self.system_dir), "scf"],
            capture_output=True,
            text=True,
            env=runner_env(VASP_ENV_SH=str(env_script), VASP_WORK_DIR=str(attempt_dir)),
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
        self.assertTrue((attempt_dir / "OUTCAR").exists())
        self.assertTrue((attempt_dir / "exit_code.json").exists())


if __name__ == "__main__":
    unittest.main()
