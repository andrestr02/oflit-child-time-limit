#!/usr/bin/python3

import ast
import importlib.machinery
import importlib.util
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "child_time_backend.py"

loader = importlib.machinery.SourceFileLoader("child_time_backend", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None:
    raise RuntimeError(f"Cannot create import spec for {SCRIPT}")
backend_module = importlib.util.module_from_spec(spec)
loader.exec_module(backend_module)

core = backend_module.core
ChildTimeBackend = backend_module.ChildTimeBackend


class HygieneTests(unittest.TestCase):
    """The backend is the domain layer: it must stay free of transport
    and authorization concerns, and of any shell/eval escape hatch."""

    def test_no_forbidden_constructs(self):
        tree = ast.parse(SCRIPT.read_text(), filename=str(SCRIPT))
        forbidden_imports = {"argparse", "subprocess", "dbus", "polkit"}
        forbidden_names = {"eval", "exec"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, forbidden_imports)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_names)
            elif isinstance(node, ast.Attribute) and node.attr == "system":
                # os.system specifically
                if isinstance(node.value, ast.Name):
                    self.assertFalse(
                        node.value.id == "os" and node.attr == "system",
                        "os.system referenced in backend",
                    )

    def test_no_path_parameters_exposed(self):
        """No public method may accept a caller-controlled config/state
        path -- every operation targets the single production location
        owned by child_time_core."""
        for name in ("status", "set_limit", "add_limit", "subtract_limit", "until"):
            method = getattr(ChildTimeBackend, name)
            params = set(inspect.signature(method).parameters)
            self.assertNotIn("config_path", params, name)
            self.assertNotIn("state_dir", params, name)
            self.assertNotIn("path", params, name)


class BackendScratchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "child-time-limit.conf"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config.write_text("hudzaifah=7200\nazzahra=3600\n")

        self.config_patch = mock.patch.object(core, "CONFIG", self.config)
        self.state_patch = mock.patch.object(core, "STATE_DIR", self.state_dir)
        self.username_patch = mock.patch.object(
            core, "validate_username", lambda username, require_local=True: None
        )
        self.config_patch.start()
        self.state_patch.start()
        self.username_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.username_patch.stop)
        self.addCleanup(self.tempdir.cleanup)

        self.backend = ChildTimeBackend()

    def test_status_all_users(self):
        rows = self.backend.status()
        names = {row.username for row in rows}
        self.assertEqual(names, {"hudzaifah", "azzahra"})

    def test_status_single_user(self):
        rows = self.backend.status("hudzaifah")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].username, "hudzaifah")
        self.assertEqual(rows[0].limit, 7200)

    def test_status_unknown_user_maps_to_unknown_user_error(self):
        with self.assertRaises(backend_module.UnknownUserError):
            self.backend.status("nosuchuser")

    def test_add_limit(self):
        result = self.backend.add_limit("hudzaifah", "5m")
        self.assertEqual(result.new_limit, 7500)
        self.assertEqual(result.reason, "add 5m")

    def test_subtract_limit(self):
        result = self.backend.subtract_limit("hudzaifah", "5m")
        self.assertEqual(result.new_limit, 6900)

    def test_set_limit(self):
        result = self.backend.set_limit("hudzaifah", "1h")
        self.assertEqual(result.new_limit, 3600)

    def test_until(self):
        now = core.local_now().replace(hour=9, minute=0, second=0, microsecond=0)
        (self.state_dir / "hudzaifah.state").write_text(f"{core.today()} 1800\n")
        with mock.patch.object(core, "local_now", return_value=now):
            result = self.backend.until("hudzaifah", "10:00")
        self.assertEqual(result.new_limit, 1800 + 3600)

    def test_reduction_without_force_maps_to_reduction_error(self):
        (self.state_dir / "hudzaifah.state").write_text(f"{core.today()} 4000\n")
        with self.assertRaises(backend_module.ReductionRequiresForceError):
            self.backend.subtract_limit("hudzaifah", "1h")
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7200)

    def test_reduction_with_force_succeeds(self):
        (self.state_dir / "hudzaifah.state").write_text(f"{core.today()} 4000\n")
        result = self.backend.subtract_limit("hudzaifah", "1h", force=True)
        self.assertEqual(result.new_limit, 3600)
        self.assertEqual(result.used, 4000)

    def test_invalid_duration_maps_to_invalid_duration_error(self):
        with self.assertRaises(backend_module.InvalidDurationError):
            self.backend.add_limit("hudzaifah", "not-a-duration")

    def test_unknown_user_mutation_maps_to_unknown_user_error(self):
        with self.assertRaises(backend_module.UnknownUserError):
            self.backend.add_limit("nosuchuser", "5m")

    def test_invalid_limit_from_over_subtraction(self):
        with self.assertRaises(backend_module.InvalidLimitError):
            self.backend.subtract_limit("hudzaifah", "3h", force=True)

    def test_unrelated_user_untouched(self):
        self.backend.add_limit("hudzaifah", "5m")
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["azzahra"], 3600)

    def test_state_never_mutated_by_backend(self):
        state_file = self.state_dir / "hudzaifah.state"
        state_file.write_text(f"{core.today()} 100\n")
        before = state_file.read_bytes()
        before_mtime = state_file.stat().st_mtime_ns
        self.backend.add_limit("hudzaifah", "5m")
        self.assertEqual(state_file.read_bytes(), before)
        self.assertEqual(state_file.stat().st_mtime_ns, before_mtime)


if __name__ == "__main__":
    unittest.main()
