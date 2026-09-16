import ast
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOGIN = ROOT / "src/child-time-login-check"
ENFORCER = ROOT / "src/child-time-enforcer"


def load_runtime_window_namespace(path):
    tree = ast.parse(path.read_text())

    wanted_assignments = {
        "ACCESS_CONFIG",
        "DEFAULT_ACCESS_START",
        "DEFAULT_ACCESS_END",
    }
    wanted_functions = {
        "load_access_window",
        "within_access_window",
    }

    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Real imports from the source file, not hand-injected names --
            # a script that uses a name (e.g. Path) without importing it
            # must fail here exactly as it would at real runtime.
            nodes.append(node)

        elif isinstance(node, ast.Assign):
            names = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if names & wanted_assignments:
                nodes.append(node)

        elif isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
            nodes.append(node)

    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace = {}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


class AccessWindowContractTests(unittest.TestCase):
    def setUp(self):
        self.login = load_runtime_window_namespace(LOGIN)
        self.enforcer = load_runtime_window_namespace(ENFORCER)

    def test_default_window_contract_is_identical(self):
        cases = [
            (datetime(2026, 9, 17, 8, 59, 59), False),
            (datetime(2026, 9, 17, 9, 0), True),
            (datetime(2026, 9, 17, 12, 0), True),
            (datetime(2026, 9, 17, 16, 59, 59), True),
            (datetime(2026, 9, 17, 17, 0), False),
            (datetime(2026, 9, 17, 23, 59, 59), False),
        ]

        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.conf"

            for ns in (self.login, self.enforcer):
                ns["ACCESS_CONFIG"] = missing

            for now, expected in cases:
                with self.subTest(now=now):
                    self.assertEqual(
                        self.login["within_access_window"](now),
                        expected,
                    )
                    self.assertEqual(
                        self.enforcer["within_access_window"](now),
                        expected,
                    )

    def test_custom_window_contract_is_identical(self):
        cases = [
            (datetime(2026, 9, 17, 7, 59, 59), False),
            (datetime(2026, 9, 17, 8, 0), True),
            (datetime(2026, 9, 17, 12, 0), True),
            (datetime(2026, 9, 17, 17, 59, 59), True),
            (datetime(2026, 9, 17, 18, 0), False),
        ]

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "access.conf"
            cfg.write_text("start=08:00\nend=18:00\n")

            for ns in (self.login, self.enforcer):
                ns["ACCESS_CONFIG"] = cfg

            for now, expected in cases:
                with self.subTest(now=now):
                    self.assertEqual(
                        self.login["within_access_window"](now),
                        expected,
                    )
                    self.assertEqual(
                        self.enforcer["within_access_window"](now),
                        expected,
                    )

    def test_login_denial_message_is_not_hardcoded_to_the_default_window(self):
        """The denial message must reflect whatever window is configured,
        not a fixed '09:00 and 17:00' string left over from the fixed
        v1.2.0 policy."""
        source = LOGIN.read_text()
        self.assertNotIn("09:00 and 17:00", source)
        self.assertIn("window_start", source)
        self.assertIn("window_end", source)

    def test_malformed_runtime_config_falls_back_identically(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "access.conf"
            cfg.write_text("start=20:00\nend=08:00\n")

            for ns in (self.login, self.enforcer):
                ns["ACCESS_CONFIG"] = cfg

            inside = datetime(2026, 9, 17, 12, 0)
            outside = datetime(2026, 9, 17, 18, 0)

            self.assertTrue(self.login["within_access_window"](inside))
            self.assertTrue(self.enforcer["within_access_window"](inside))
            self.assertFalse(self.login["within_access_window"](outside))
            self.assertFalse(self.enforcer["within_access_window"](outside))


if __name__ == "__main__":
    unittest.main()
