#!/usr/bin/python3

"""Static packaging checks for the privileged D-Bus backend artifacts,
mirroring the style of InstallerTests/UninstallerTests in
test_child_time_cli.py (text assertions against install.sh/uninstall.sh,
not execution against a live system)."""

import unittest
import xml.dom.minidom
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallerBackendTests(unittest.TestCase):
    def setUp(self):
        self.installer = (ROOT / "install.sh").read_text()

    def test_installs_new_lib_modules_with_expected_mode(self):
        for module in (
            "child_time_operations.py",
            "child_time_backend.py",
            "child_time_polkit.py",
        ):
            with self.subTest(module=module):
                self.assertIn(
                    f'install -m 0644 "$ROOT_DIR/src/{module}" '
                    f"/usr/local/lib/child-time-limit/{module}",
                    self.installer,
                )

    def test_installs_backend_executable(self):
        self.assertIn(
            'install -m 0755 "$ROOT_DIR/src/child-time-backend" '
            "/usr/local/sbin/child-time-backend",
            self.installer,
        )

    def test_installs_core_and_backend_lib_modules_before_cli_and_backend_executables(self):
        core_install = "/usr/local/lib/child-time-limit/child_time_core.py"
        backend_lib_install = "/usr/local/lib/child-time-limit/child_time_backend.py"
        cli_install = (
            'install -m 0755 "$ROOT_DIR/src/child-time" /usr/local/sbin/child-time'
        )
        backend_exe_install = (
            'install -m 0755 "$ROOT_DIR/src/child-time-backend" '
            "/usr/local/sbin/child-time-backend"
        )
        self.assertLess(self.installer.index(core_install), self.installer.index(cli_install))
        self.assertLess(
            self.installer.index(backend_lib_install),
            self.installer.index(backend_exe_install),
        )

    def test_installs_dbus_policy_and_activation_files(self):
        self.assertIn(
            'install -m 0644 "$ROOT_DIR/dbus/id.oflit.ChildTime1.conf" '
            "/usr/share/dbus-1/system.d/id.oflit.ChildTime1.conf",
            self.installer,
        )
        self.assertIn(
            'install -m 0644 "$ROOT_DIR/dbus/id.oflit.ChildTime1.service" '
            "/usr/share/dbus-1/system-services/id.oflit.ChildTime1.service",
            self.installer,
        )

    def test_installs_polkit_policy(self):
        self.assertIn(
            'install -m 0644 "$ROOT_DIR/polkit/id.oflit.ChildTime1.policy" '
            "/usr/share/polkit-1/actions/id.oflit.ChildTime1.policy",
            self.installer,
        )

    def test_installs_backend_systemd_unit(self):
        self.assertIn(
            'install -m 0644 "$ROOT_DIR/systemd/child-time-backend.service" '
            "/etc/systemd/system/child-time-backend.service",
            self.installer,
        )

    def test_backend_service_is_not_eagerly_enabled_or_started(self):
        """The backend is D-Bus bus-activated; install.sh must not force
        it to always run, unlike the enforcer."""
        self.assertNotIn("systemctl enable child-time-backend.service", self.installer)
        self.assertNotIn("systemctl start child-time-backend.service", self.installer)
        self.assertNotIn("systemctl restart child-time-backend.service", self.installer)
        self.assertNotIn(
            "systemctl enable --now child-time-backend.service", self.installer
        )

    def test_does_not_touch_config_or_state_for_backend_artifacts(self):
        """Sanity check: none of the new artifact-install lines should be
        anywhere near config/state mutation; the installer must remain
        upgrade-safe."""
        self.assertNotIn("rm -rf /var/lib/child-time-limit", self.installer)
        self.assertNotIn("rm -f /etc/child-time-limit.conf", self.installer)


class UninstallerBackendTests(unittest.TestCase):
    def setUp(self):
        self.uninstaller = (ROOT / "uninstall.sh").read_text()

    def test_disables_backend_service(self):
        self.assertIn(
            "systemctl disable --now child-time-backend.service", self.uninstaller
        )

    def test_removes_backend_systemd_unit(self):
        self.assertIn(
            "rm -f /etc/systemd/system/child-time-backend.service", self.uninstaller
        )

    def test_removes_dbus_and_polkit_artifacts(self):
        self.assertIn(
            "rm -f /usr/share/dbus-1/system-services/id.oflit.ChildTime1.service",
            self.uninstaller,
        )
        self.assertIn(
            "rm -f /usr/share/dbus-1/system.d/id.oflit.ChildTime1.conf", self.uninstaller
        )
        self.assertIn(
            "rm -f /usr/share/polkit-1/actions/id.oflit.ChildTime1.policy",
            self.uninstaller,
        )

    def test_removes_backend_executable_and_lib_modules(self):
        self.assertIn("rm -f /usr/local/sbin/child-time-backend", self.uninstaller)
        for module in (
            "child_time_operations.py",
            "child_time_backend.py",
            "child_time_polkit.py",
        ):
            self.assertIn(
                f"rm -f /usr/local/lib/child-time-limit/{module}", self.uninstaller
            )

    def test_still_preserves_config_and_state(self):
        self.assertIn("/etc/child-time-limit.conf", self.uninstaller)
        self.assertIn("/var/lib/child-time-limit/", self.uninstaller)
        self.assertNotIn("rm -f /etc/child-time-limit.conf", self.uninstaller)
        self.assertNotIn("rm -rf /var/lib/child-time-limit", self.uninstaller)

    def test_backend_lib_removal_precedes_lib_dir_rmdir(self):
        rmdir_line = "rmdir --ignore-fail-on-non-empty /usr/local/lib/child-time-limit "
        backend_rm = "rm -f /usr/local/lib/child-time-limit/child_time_backend.py"
        self.assertLess(
            self.uninstaller.index(backend_rm),
            self.uninstaller.index(rmdir_line.strip()),
        )


class DBusConfXmlTests(unittest.TestCase):
    def test_conf_is_well_formed_and_deny_by_default(self):
        path = ROOT / "dbus" / "id.oflit.ChildTime1.conf"
        text = path.read_text()
        xml.dom.minidom.parseString(text)
        self.assertIn('<deny own="id.oflit.ChildTime1"/>', text)
        self.assertIn('<policy user="root">', text)


class PolkitPolicyXmlTests(unittest.TestCase):
    def test_policy_is_well_formed_and_defines_both_actions(self):
        path = ROOT / "polkit" / "id.oflit.ChildTime1.policy"
        text = path.read_text()
        xml.dom.minidom.parseString(text)
        self.assertIn('action id="id.oflit.ChildTime1.status"', text)
        self.assertIn('action id="id.oflit.ChildTime1.manage"', text)

    def test_manage_action_requires_admin_authentication(self):
        path = ROOT / "polkit" / "id.oflit.ChildTime1.policy"
        text = path.read_text()
        manage_block = text.split('action id="id.oflit.ChildTime1.manage"')[1]
        self.assertIn("auth_admin", manage_block.split("</action>")[0])

    def test_status_action_requires_admin_authentication(self):
        """Parity with the existing accepted CLI: `child-time status` also
        calls require_root(). An unauthenticated status action would be a
        NEW, looser capability, not a neutral default -- see
        SSOT_CONFIRMATION_PENDING note in the policy file / architecture doc."""
        path = ROOT / "polkit" / "id.oflit.ChildTime1.policy"
        text = path.read_text()
        status_block = text.split('action id="id.oflit.ChildTime1.status"')[1]
        self.assertIn("auth_admin", status_block.split("</action>")[0])


class DBusServiceFileTests(unittest.TestCase):
    def test_service_activation_file_references_systemd_unit(self):
        text = (ROOT / "dbus" / "id.oflit.ChildTime1.service").read_text()
        self.assertIn("Name=id.oflit.ChildTime1", text)
        self.assertIn("SystemdService=child-time-backend.service", text)

    def test_systemd_unit_matches_dbus_activation_contract(self):
        text = (ROOT / "systemd" / "child-time-backend.service").read_text()
        self.assertIn("Type=dbus", text)
        self.assertIn("BusName=id.oflit.ChildTime1", text)
        self.assertIn("ExecStart=/usr/local/sbin/child-time-backend", text)

    def test_systemd_unit_does_not_use_incompatible_hardening(self):
        """ProtectSystem=strict would break the core's mkstemp+rename
        atomic-write pattern in /etc (see child_time_core.py's
        _atomic_update_limit_locked, which creates a new temp file and a
        policy lock file directly inside /etc, not just rewrites one
        already-listed path)."""
        text = (ROOT / "systemd" / "child-time-backend.service").read_text()
        self.assertNotIn("ProtectSystem=strict", text)


if __name__ == "__main__":
    unittest.main()
