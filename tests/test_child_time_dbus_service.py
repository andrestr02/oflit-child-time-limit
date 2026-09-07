#!/usr/bin/python3

"""Integration tests for the D-Bus transport layer (child-time-backend).

These tests run the REAL child-time-backend service object against a
real, private, throwaway D-Bus daemon (spawned per test class) -- not a
system bus, not real Polkit. Authorization is supplied by an injected
fake PolkitAuthority so tests can exercise both the "allowed" and
"denied" paths deterministically. Domain state is an isolated tempdir
config/state pair, never production paths.

Skipped automatically if dbus-python, PyGObject, or the `dbus-daemon`
binary are unavailable, so this file does not break portability of the
rest of the suite.
"""

import ast
import importlib.machinery
import importlib.util
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_SCRIPT = ROOT / "src" / "child-time-backend"

try:
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    _HAVE_DBUS = True
except ImportError:
    _HAVE_DBUS = False

_HAVE_DBUS_DAEMON = shutil.which("dbus-daemon") is not None


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"Cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeAuthority:
    def __init__(self, allow=True):
        self.allow = allow
        self.calls = []

    def check_authorization(self, sender, action_id, allow_interactive=True):
        self.calls.append((sender, action_id))
        return self.allow


class HygieneTests(unittest.TestCase):
    """Static checks that do not require dbus-python to be installed."""

    def test_no_shell_execution_constructs(self):
        tree = ast.parse(SERVICE_SCRIPT.read_text(), filename=str(SERVICE_SCRIPT))
        forbidden_imports = {"subprocess", "argparse"}
        forbidden_names = {"eval", "exec"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, forbidden_imports)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_names)

    def test_service_never_touches_core_or_ops_directly(self):
        """The transport layer must delegate to child_time_backend, never
        import child_time_core/child_time_operations itself -- that would
        be domain logic leaking into the D-Bus handler."""
        tree = ast.parse(SERVICE_SCRIPT.read_text(), filename=str(SERVICE_SCRIPT))
        loaded_literals = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "_load" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant):
                        loaded_literals.add(first.value)
        self.assertEqual(
            loaded_literals, {"child_time_backend.py", "child_time_polkit.py"}
        )


@unittest.skipUnless(_HAVE_DBUS, "python3-dbus / PyGObject not installed")
@unittest.skipUnless(_HAVE_DBUS_DAEMON, "dbus-daemon binary not installed")
class DBusIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daemon = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE,
            text=True,
        )
        cls.address = cls.daemon.stdout.readline().strip()
        if not cls.address:
            cls.daemon.kill()
            raise unittest.SkipTest("could not start a private dbus-daemon")

        cls.svc_module = _load(SERVICE_SCRIPT, "child_time_dbus_service_under_test")
        cls.backend_module = cls.svc_module.backend_module

        cls.mainloop_glib = dbus.mainloop.glib.DBusGMainLoop()
        cls.server_bus = dbus.bus.BusConnection(cls.address, mainloop=cls.mainloop_glib)

        cls.loop = GLib.MainLoop()
        cls.thread = threading.Thread(target=cls.loop.run, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.loop.quit()
        cls.daemon.terminate()
        try:
            cls.daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.daemon.kill()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.config = root / "child-time-limit.conf"
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.config.write_text("hudzaifah=7200\nazzahra=3600\n")

        core = self.backend_module.core
        core.CONFIG = self.config
        core.STATE_DIR = self.state_dir
        core.validate_username = lambda username, require_local=True: None

        self.fake_authority = FakeAuthority(allow=True)
        self.bus_name = dbus.service.BusName(
            self.svc_module.BUS_NAME, self.server_bus, do_not_queue=True
        )
        self.service = self.svc_module.ChildTimeService(
            self.server_bus,
            backend=self.backend_module.ChildTimeBackend(),
            authority=self.fake_authority,
        )
        time.sleep(0.1)

        client_bus = dbus.bus.BusConnection(self.address)
        proxy = client_bus.get_object(
            self.svc_module.BUS_NAME, self.svc_module.OBJECT_PATH, introspect=False
        )
        self.iface = dbus.Interface(proxy, dbus_interface=self.svc_module.INTERFACE)

    def tearDown(self):
        self.service.remove_from_connection()
        del self.bus_name

    def test_status_all_users(self):
        rows = self.iface.Status("")
        names = {str(row[0]) for row in rows}
        self.assertEqual(names, {"hudzaifah", "azzahra"})

    def test_add_and_subtract_round_trip(self):
        result = self.iface.AddLimit("hudzaifah", "60")
        self.assertEqual(int(result[3]), 7260)  # new_limit
        result = self.iface.SubtractLimit("hudzaifah", "60", False)
        self.assertEqual(int(result[3]), 7200)

    def test_unrelated_user_untouched(self):
        self.iface.AddLimit("hudzaifah", "60")
        rows = self.iface.Status("azzahra")
        self.assertEqual(int(rows[0][3]), 3600)

    def test_unknown_user_maps_to_stable_error_name(self):
        with self.assertRaises(dbus.exceptions.DBusException) as ctx:
            self.iface.Status("nosuchuser")
        self.assertEqual(
            ctx.exception.get_dbus_name(), "id.oflit.ChildTime1.Error.UnknownUser"
        )

    def test_invalid_duration_maps_to_stable_error_name(self):
        with self.assertRaises(dbus.exceptions.DBusException) as ctx:
            self.iface.AddLimit("hudzaifah", "not-a-duration")
        self.assertEqual(
            ctx.exception.get_dbus_name(), "id.oflit.ChildTime1.Error.InvalidDuration"
        )

    def test_reduction_without_force_maps_to_stable_error_name(self):
        (self.state_dir / "hudzaifah.state").write_text(
            f"{self.backend_module.core.today()} 4000\n"
        )
        with self.assertRaises(dbus.exceptions.DBusException) as ctx:
            self.iface.SubtractLimit("hudzaifah", "1h", False)
        self.assertEqual(
            ctx.exception.get_dbus_name(),
            "id.oflit.ChildTime1.Error.ReductionRequiresForce",
        )

    def test_authorization_denied_blocks_mutation_before_touching_config(self):
        before = self.config.read_bytes()
        self.fake_authority.allow = False
        with self.assertRaises(dbus.exceptions.DBusException) as ctx:
            self.iface.AddLimit("hudzaifah", "60")
        self.assertEqual(
            ctx.exception.get_dbus_name(), "id.oflit.ChildTime1.Error.AuthorizationDenied"
        )
        self.assertEqual(self.config.read_bytes(), before)

    def test_status_and_manage_use_distinct_polkit_actions(self):
        self.iface.Status("")
        self.iface.AddLimit("hudzaifah", "60")
        actions = {action for _sender, action in self.fake_authority.calls}
        self.assertEqual(
            actions,
            {"id.oflit.ChildTime1.status", "id.oflit.ChildTime1.manage"},
        )

    def test_authorization_checked_with_real_caller_unique_name(self):
        self.iface.Status("")
        sender, _action = self.fake_authority.calls[0]
        self.assertTrue(str(sender).startswith(":"))


if __name__ == "__main__":
    unittest.main()
