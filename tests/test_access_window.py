#!/usr/bin/env python3

import ast
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOGIN = ROOT / "src" / "child-time-login-check"
ENFORCER = ROOT / "src" / "child-time-enforcer"


def load_function(path, name):
    """Load one top-level function without executing script runtime."""
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))

    selected = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            selected.append(node)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"ACCESS_START", "ACCESS_END"}
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == name:
            selected.append(node)

    module = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


class AccessWindowContractTests(unittest.TestCase):
    """
    Global policy:
        09:00 inclusive
        17:00 exclusive

    Both PAM login enforcement and the running-session enforcer must use
    the same boundary semantics.
    """

    CASES = (
        (datetime(2026, 9, 17, 8, 59, 59), False),
        (datetime(2026, 9, 17, 9, 0, 0), True),
        (datetime(2026, 9, 17, 12, 0, 0), True),
        (datetime(2026, 9, 17, 16, 59, 59), True),
        (datetime(2026, 9, 17, 17, 0, 0), False),
        (datetime(2026, 9, 17, 23, 59, 59), False),
    )

    def test_login_check_has_access_window_function(self):
        fn = load_function(LOGIN, "within_access_window")
        for now, expected in self.CASES:
            with self.subTest(now=now):
                self.assertEqual(fn(now), expected)

    def test_enforcer_has_access_window_function(self):
        fn = load_function(ENFORCER, "within_access_window")
        for now, expected in self.CASES:
            with self.subTest(now=now):
                self.assertEqual(fn(now), expected)

    def test_both_runtime_paths_have_identical_window_contract(self):
        login_fn = load_function(LOGIN, "within_access_window")
        enforcer_fn = load_function(ENFORCER, "within_access_window")

        for now, _ in self.CASES:
            with self.subTest(now=now):
                self.assertEqual(login_fn(now), enforcer_fn(now))


if __name__ == "__main__":
    unittest.main()
