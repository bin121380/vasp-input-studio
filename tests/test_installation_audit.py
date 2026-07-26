import unittest

from app import main


class InstallationAuditTests(unittest.TestCase):
    @staticmethod
    def _item_by_title(audit: dict, title: str) -> dict:
        return next(item for item in audit["items"] if item["title"] == title)

    def test_daemon_failure_note_includes_aiida_probe_detail(self) -> None:
        audit = main.installation_audit(
            {
                "available": True,
                "profile": "vasp_studio_pg",
                "profile_exists": True,
                "has_broker": True,
                "daemon_running": False,
                "daemon_status": "The daemon could not be reached because of a stale PID file.",
                "codes": [{"label": "vasp_std_localhost"}],
                "potcar_families": [{"label": "PBE_64"}],
            }
        )

        daemon_item = self._item_by_title(audit, "AiiDA daemon")
        self.assertEqual(daemon_item["status"], "fail")
        self.assertIn("vasp_studio_pg", daemon_item["note"])
        self.assertIn("stale PID file", daemon_item["note"])


if __name__ == "__main__":
    unittest.main()
