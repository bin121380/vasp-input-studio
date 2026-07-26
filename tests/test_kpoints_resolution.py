import tempfile
import unittest
from pathlib import Path

from app.structure_resolution import resolved_mesh_kpoints_path


class KpointsResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="vasp-studio-kpoints-resolution-")
        self.system_dir = Path(self.tempdir.name)
        relax_dir = self.system_dir / "runs" / "relax"
        relax_dir.mkdir(parents=True, exist_ok=True)
        (relax_dir / "CONTCAR").write_text("relaxed\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_relax_uses_relax_mesh_and_scf_uses_scf_mesh(self) -> None:
        (self.system_dir / "KPOINTS.relax").write_text("relax mesh\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.scf").write_text("scf mesh\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.downstream").write_text("legacy mesh\n", encoding="utf-8")

        self.assertEqual(resolved_mesh_kpoints_path(self.system_dir, "relax"), self.system_dir / "KPOINTS.relax")
        self.assertEqual(resolved_mesh_kpoints_path(self.system_dir, "scf"), self.system_dir / "KPOINTS.scf")
        self.assertEqual(resolved_mesh_kpoints_path(self.system_dir, "elastic"), self.system_dir / "KPOINTS.scf")

    def test_legacy_downstream_is_only_a_static_mesh_fallback(self) -> None:
        (self.system_dir / "KPOINTS.relax").write_text("relax mesh\n", encoding="utf-8")
        (self.system_dir / "KPOINTS.downstream").write_text("legacy mesh\n", encoding="utf-8")

        self.assertEqual(resolved_mesh_kpoints_path(self.system_dir, "relax"), self.system_dir / "KPOINTS.relax")
        self.assertEqual(resolved_mesh_kpoints_path(self.system_dir, "scf"), self.system_dir / "KPOINTS.downstream")


if __name__ == "__main__":
    unittest.main()
