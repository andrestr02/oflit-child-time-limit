#!/usr/bin/python3

import importlib.machinery
import importlib.util
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "child-time"

loader = importlib.machinery.SourceFileLoader("child_time_cli", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None:
    raise RuntimeError(f"Cannot create import spec for {SCRIPT}")
child_time = importlib.util.module_from_spec(spec)
loader.exec_module(child_time)


def _write_fake_core(path, marker):
    path.write_text(f'MARKER = "{marker}"\n')


class RequireRootTests(unittest.TestCase):
    def test_require_root_raises_when_not_root(self):
        with mock.patch.object(child_time.os, "geteuid", return_value=1000):
            with self.assertRaises(child_time.ChildTimeError):
                child_time.require_root()

    def test_require_root_passes_when_root(self):
        with mock.patch.object(child_time.os, "geteuid", return_value=0):
            child_time.require_root()


class DelegationTests(unittest.TestCase):
    """The CLI must not reimplement policy logic; it re-exports the core."""

    def test_cli_names_are_the_core_implementation(self):
        for name in (
            "ChildTimeError",
            "StatusRow",
            "LimitMutationResult",
            "parse_duration",
            "parse_clock",
            "validate_username",
            "load_config",
            "read_used",
            "policy_lock",
            "atomic_update_limit",
            "confirm_reduction",
            "status_rows",
            "apply_limit_transaction",
            "local_now",
            "today",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(child_time, name), getattr(child_time.core, name))


class StatusOutputTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "child-time-limit.conf"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config.write_text("child=3600\n")
        (self.state_dir / "child.state").write_text(f"{child_time.today()} 5400\n")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_status_reports_raw_usage_above_limit_without_capping(self):
        output = StringIO()
        with mock.patch.object(child_time.core, "CONFIG", self.config), \
             mock.patch.object(child_time.core, "STATE_DIR", self.state_dir), \
             redirect_stdout(output):
            child_time.print_status()

        row = next(
            line for line in output.getvalue().splitlines() if line.startswith("child")
        )
        fields = row.split()
        self.assertEqual(fields[0], "child")
        self.assertEqual(fields[1], "01:30:00")  # used: 5400s, never capped to limit
        self.assertEqual(fields[2], "00:00:00")  # remaining
        self.assertEqual(fields[3], "01:00:00")  # limit
        self.assertEqual(fields[4], "EXHAUSTED")


class CommandDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "child-time-limit.conf"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config.write_text("hudzaifah=7200\n")
        self.root_patch = mock.patch.object(child_time.os, "geteuid", return_value=0)
        self.config_patch = mock.patch.object(child_time.core, "CONFIG", self.config)
        self.state_patch = mock.patch.object(
            child_time.core, "STATE_DIR", self.state_dir
        )
        self.username_patch = mock.patch.object(
            child_time.core, "validate_username",
            lambda username, require_local=True: None,
        )
        self.root_patch.start()
        self.config_patch.start()
        self.state_patch.start()
        self.username_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.username_patch.stop)
        self.addCleanup(self.tempdir.cleanup)

    def _run(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = child_time.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_set_command(self):
        rc, out, _ = self._run(["set", "hudzaifah", "2h25m"])
        self.assertEqual(rc, 0)
        self.assertIn("New limit  : 02:25:00 (2h25m)", out)
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 8700)

    def test_add_command(self):
        rc, out, _ = self._run(["add", "hudzaifah", "5m"])
        self.assertEqual(rc, 0)
        self.assertIn("Mode       : add 5m", out)
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7500)

    def test_subtract_command(self):
        rc, out, _ = self._run(["subtract", "hudzaifah", "5m"])
        self.assertEqual(rc, 0)
        self.assertIn("Mode       : subtract 5m", out)
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 6900)

    def test_until_command_uses_authoritative_usage(self):
        now = child_time.local_now().replace(hour=9, minute=0, second=0, microsecond=0)
        (self.state_dir / "hudzaifah.state").write_text(f"{child_time.today()} 1800\n")
        with mock.patch.object(child_time.core, "local_now", return_value=now):
            rc, out, _ = self._run(["until", "hudzaifah", "10:00"])
        self.assertEqual(rc, 0)
        self.assertIn("continuous active use until 10:00", out)
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 1800 + 3600)

    def test_reduction_without_force_is_rejected(self):
        (self.state_dir / "hudzaifah.state").write_text(f"{child_time.today()} 4000\n")
        rc, out, err = self._run(["subtract", "hudzaifah", "1h"])
        self.assertEqual(rc, 2)
        self.assertIn("ERROR", err)
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7200)

    def test_forced_reduction_preserves_used_and_zeroes_remaining(self):
        (self.state_dir / "hudzaifah.state").write_text(f"{child_time.today()} 5400\n")
        rc, out, _ = self._run(["set", "hudzaifah", "1h", "--force"])
        self.assertEqual(rc, 0)
        self.assertIn("Used today : 01:30:00", out)
        self.assertIn("Remaining  : 00:00:00", out)


class ImportPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.addCleanup(self.tempdir.cleanup)

    def test_source_tree_sibling_wins_over_stale_installed_core(self):
        sibling = self.root / "sibling_core.py"
        installed = self.root / "installed_core.py"
        _write_fake_core(sibling, "sibling")
        _write_fake_core(installed, "installed")

        with mock.patch.object(child_time, "_SIBLING_CORE", sibling), \
             mock.patch.object(child_time, "_INSTALLED_CORE", installed):
            module = child_time._load_core()

        self.assertEqual(module.MARKER, "sibling")

    def test_installed_layout_fallback_resolves_product_owned_core(self):
        sibling = self.root / "no_sibling_here.py"
        installed = self.root / "installed_core.py"
        _write_fake_core(installed, "installed")

        with mock.patch.object(child_time, "_SIBLING_CORE", sibling), \
             mock.patch.object(child_time, "_INSTALLED_CORE", installed):
            module = child_time._load_core()

        self.assertEqual(module.MARKER, "installed")

    def test_missing_core_raises_import_error(self):
        sibling = self.root / "no_sibling_here.py"
        installed = self.root / "no_installed_here.py"

        with mock.patch.object(child_time, "_SIBLING_CORE", sibling), \
             mock.patch.object(child_time, "_INSTALLED_CORE", installed):
            with self.assertRaises(ImportError):
                child_time._load_core()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.installer = (ROOT / "install.sh").read_text()

    def test_installer_explicitly_restarts_enforcer_after_upgrade(self):
        enable = "systemctl enable child-time-enforcer.service"
        restart = "systemctl restart child-time-enforcer.service"
        old_enable_now = "systemctl enable --now child-time-enforcer.service"

        self.assertIn(enable, self.installer)
        self.assertIn(restart, self.installer)
        self.assertNotIn(old_enable_now, self.installer)

        artifact_install = (
            'install -m 0755 "$ROOT_DIR/src/child-time-enforcer" '
            '/usr/local/sbin/child-time-enforcer'
        )
        daemon_reload = "systemctl daemon-reload"

        self.assertLess(
            self.installer.index(artifact_install), self.installer.index(daemon_reload)
        )
        self.assertLess(
            self.installer.index(daemon_reload), self.installer.index(restart)
        )

    def test_installer_creates_product_owned_lib_directory(self):
        self.assertIn("install -d -m 0755 /usr/local/lib/child-time-limit", self.installer)

    def test_installer_installs_core_module_with_expected_mode(self):
        core_install = (
            'install -m 0644 "$ROOT_DIR/src/child_time_core.py" '
            '/usr/local/lib/child-time-limit/child_time_core.py'
        )
        self.assertIn(core_install, self.installer)

    def test_installer_installs_core_before_cli(self):
        core_install = "/usr/local/lib/child-time-limit/child_time_core.py"
        cli_install = (
            'install -m 0755 "$ROOT_DIR/src/child-time" /usr/local/sbin/child-time'
        )
        self.assertLess(
            self.installer.index(core_install), self.installer.index(cli_install)
        )

    def test_installer_does_not_use_dist_packages(self):
        self.assertNotIn("dist-packages", self.installer)


class UninstallerTests(unittest.TestCase):
    def setUp(self):
        self.uninstaller = (ROOT / "uninstall.sh").read_text()

    def test_uninstaller_removes_core_module_and_pycache(self):
        self.assertIn(
            "rm -f /usr/local/lib/child-time-limit/child_time_core.py", self.uninstaller
        )
        self.assertIn("__pycache__", self.uninstaller)
        self.assertIn("child_time_core", self.uninstaller)

    def test_uninstaller_removes_lib_dir_only_if_empty(self):
        self.assertIn("rmdir", self.uninstaller)
        self.assertIn("/usr/local/lib/child-time-limit", self.uninstaller)

    def test_uninstaller_preserves_config_and_state(self):
        self.assertIn("/etc/child-time-limit.conf", self.uninstaller)
        self.assertIn("/var/lib/child-time-limit/", self.uninstaller)
        self.assertNotIn("rm -f /etc/child-time-limit.conf", self.uninstaller)
        self.assertNotIn("rm -rf /var/lib/child-time-limit", self.uninstaller)


class LegacyStatusTests(unittest.TestCase):
    def test_usage_above_limit_is_reported_without_capping(self):
        script = ROOT / "src" / "child-time-status"
        loader = importlib.machinery.SourceFileLoader(
            "child_time_legacy_status", str(script)
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        if spec is None:
            self.fail(f"Cannot create import spec for {script}")

        legacy_status = importlib.util.module_from_spec(spec)
        loader.exec_module(legacy_status)

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = root / "child-time-limit.conf"
            state_dir = root / "state"
            state_dir.mkdir()

            config.write_text("child=3600\n")
            (state_dir / "child.state").write_text(
                f"{legacy_status.today()} 5400\n"
            )

            legacy_status.CONFIG = str(config)
            legacy_status.STATE_DIR = str(state_dir)

            output = StringIO()
            with mock.patch.object(legacy_status.os, "geteuid", return_value=0), \
                 redirect_stdout(output):
                rc = legacy_status.main()

            self.assertEqual(rc, 0)

            row = next(
                line
                for line in output.getvalue().splitlines()
                if line.startswith("child")
            )
            fields = row.split()

            self.assertEqual(fields[0], "child")
            self.assertEqual(fields[1], "01:30:00")
            self.assertEqual(fields[2], "00:00:00")
            self.assertEqual(fields[3], "01:00:00")
            self.assertEqual(fields[4], "EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
