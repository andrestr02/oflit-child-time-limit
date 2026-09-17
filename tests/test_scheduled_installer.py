import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
SERVICE_DIR = ROOT / "systemd"
SCHEDULE_EXAMPLE = ROOT / "config" / "child-time-schedule.conf.example"


class ScheduledInstallerContractTests(unittest.TestCase):
    def test_schedule_example_exists(self):
        self.assertTrue(
            SCHEDULE_EXAMPLE.is_file(),
            "v1.4 must ship a schedule configuration example",
        )

    def test_installer_creates_schedule_state_directory_0700(self):
        source = INSTALLER.read_text()
        self.assertIn(
            "install -d -m 0700 /var/lib/child-time-limit/schedules",
            source,
        )

    def test_installer_preserves_existing_schedule_config(self):
        source = INSTALLER.read_text()
        self.assertIn(
            "if [[ ! -e /etc/child-time-schedule.conf ]]; then",
            source,
        )
        self.assertIn(
            'chmod 0600 /etc/child-time-schedule.conf',
            source,
        )

    def test_installer_installs_schedule_config_0600(self):
        source = INSTALLER.read_text()
        self.assertIn(
            'install -m 0600 "$ROOT_DIR/config/child-time-schedule.conf.example" '
            "/etc/child-time-schedule.conf",
            source,
        )

    def test_no_second_enforcer_service_is_added(self):
        services = sorted(SERVICE_DIR.glob("*.service"))
        self.assertEqual(
            [p.name for p in services],
            ["child-time-enforcer.service"],
            "scheduled quotas must reuse the existing enforcer service",
        )


if __name__ == "__main__":
    unittest.main()


class ScheduledInstallerUpgradeSafetyTests(unittest.TestCase):
    def test_existing_schedule_config_is_never_reinstalled(self):
        source = INSTALLER.read_text()

        start = source.index(
            "if [[ ! -e /etc/child-time-schedule.conf ]]; then"
        )
        end = source.index("\nfi", start) + len("\nfi")
        block = source[start:end]

        self.assertEqual(
            block.count(
                'install -m 0600 "$ROOT_DIR/config/'
                'child-time-schedule.conf.example" '
                '/etc/child-time-schedule.conf'
            ),
            1,
        )

        else_branch = block.split("else", 1)[1]

        self.assertNotIn(
            "child-time-schedule.conf.example",
            else_branch,
            "upgrade must never overwrite the existing schedule policy",
        )
        self.assertIn(
            "chmod 0600 /etc/child-time-schedule.conf",
            else_branch,
        )

    def test_existing_daily_and_access_config_preservation_remains_present(self):
        source = INSTALLER.read_text()

        self.assertIn(
            "if [[ ! -e /etc/child-time-limit.conf ]]; then",
            source,
        )
        self.assertIn(
            "if [[ ! -e /etc/child-time-access.conf ]]; then",
            source,
        )


if __name__ == "__main__":
    unittest.main()
