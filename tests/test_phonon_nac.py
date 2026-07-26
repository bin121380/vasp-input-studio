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
2
Direct
0 0 0
0.25 0.25 0.25
"""


class PhononNacTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-phonon-nac-")
        self.system_dir = Path(self.tempdir.name) / "Si"
        self.system_dir.mkdir(parents=True, exist_ok=True)
        (self.system_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")
        (self.system_dir / "metadata.json").write_text('{"formula": "Si", "phonon_supercell": [2, 2, 2], "band_points": 81}\n', encoding="utf-8")
        (self.system_dir / "band.conf").write_text("ATOM_NAME = Si\nDIM = 2 2 2\nBAND_POINTS = 81\n", encoding="utf-8")
        (self.system_dir / "INCAR.charge").write_text("ENCUT = 520\nLEPSILON = .FALSE.\n", encoding="utf-8")
        charge_dir = self.system_dir / "runs" / "charge"
        charge_dir.mkdir(parents=True, exist_ok=True)
        (charge_dir / "OUTCAR").write_text("Born effective charges\n", encoding="utf-8")
        (charge_dir / "POSCAR").write_text(POSCAR_TEXT, encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_update_phonon_nac_settings_writes_band_conf_controls(self) -> None:
        payload = main._update_phonon_nac_settings(self.system_dir, True, "1 0 0")

        band_conf = (self.system_dir / "band.conf").read_text(encoding="utf-8")
        self.assertIn("NAC = .TRUE.", band_conf)
        self.assertIn("Q_DIRECTION = 1 0 0", band_conf)
        self.assertTrue(payload["phonon_nac"]["enabled"])
        self.assertEqual(payload["phonon_nac"]["q_direction_text"], "1 0 0")

    def test_prepare_charge_for_born_sets_lepsilon_and_strict_defaults(self) -> None:
        payload = main._prepare_charge_for_born(self.system_dir)

        incar = (self.system_dir / "INCAR.charge").read_text(encoding="utf-8")
        self.assertIn("LEPSILON = .TRUE.", incar)
        self.assertIn("IBRION = -1", incar)
        self.assertIn("NSW = 0", incar)
        self.assertIn("LREAL = .FALSE.", incar)
        self.assertIn("EDIFF = 1E-8", incar)
        self.assertEqual(payload["path"], "INCAR.charge")
        self.assertTrue(payload["phonon_nac"]["charge_lepsilon"])

    def test_build_born_from_charge_writes_workspace_and_charge_copies(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": "# epsilon and Z*\n1 0 0 0 1 0 0 0 1\n"})()
        with patch.object(main, "_phonopy_vasp_born_command", return_value="/usr/bin/phonopy-vasp-born"), patch.object(
            main.subprocess,
            "run",
            return_value=completed,
        ):
            payload = main._build_born_from_charge(self.system_dir)

        self.assertEqual((self.system_dir / "BORN").read_text(encoding="utf-8"), "# epsilon and Z*\n1 0 0 0 1 0 0 0 1\n")
        self.assertEqual((self.system_dir / "runs" / "charge" / "BORN").read_text(encoding="utf-8"), "# epsilon and Z*\n1 0 0 0 1 0 0 0 1\n")
        self.assertEqual(payload["path"], "BORN")
        self.assertEqual(payload["source_outcar"], "runs/charge/OUTCAR")
        self.assertTrue(payload["phonon_nac"]["born_available"])


if __name__ == "__main__":
    unittest.main()
