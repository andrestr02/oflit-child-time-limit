#!/usr/bin/python3

import ast
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "child_time_polkit.py"

loader = importlib.machinery.SourceFileLoader("child_time_polkit", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None:
    raise RuntimeError(f"Cannot create import spec for {SCRIPT}")
polkit_module = importlib.util.module_from_spec(spec)
loader.exec_module(polkit_module)

PolkitAuthority = polkit_module.PolkitAuthority


class FakeAuthorityInterface:
    """Stands in for the real org.freedesktop.PolicyKit1.Authority D-Bus
    proxy: exposes only CheckAuthorization with the real signature's
    return shape, (is_authorized, is_challenge, details)."""

    def __init__(self, is_authorized=True, raise_exc=None):
        self.is_authorized = is_authorized
        self.raise_exc = raise_exc
        self.calls = []

    def CheckAuthorization(self, subject, action_id, details, flags, cancellation_id):
        self.calls.append((subject, action_id, details, flags, cancellation_id))
        if self.raise_exc is not None:
            raise self.raise_exc
        return (self.is_authorized, False, {})


class HygieneTests(unittest.TestCase):
    def test_no_forbidden_constructs(self):
        tree = ast.parse(SCRIPT.read_text(), filename=str(SCRIPT))
        forbidden_imports = {"argparse", "subprocess"}
        forbidden_names = {"eval", "exec"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, forbidden_imports)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_names)

    def test_does_not_import_core_or_backend(self):
        """Polkit is purely an authorization boundary: it must not know
        about policy/domain modules."""
        tree = ast.parse(SCRIPT.read_text(), filename=str(SCRIPT))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module]
                )
                for name in names:
                    self.assertNotIn("child_time_core", str(name))
                    self.assertNotIn("child_time_backend", str(name))


class CheckAuthorizationTests(unittest.TestCase):
    def test_grants_when_authorized(self):
        fake = FakeAuthorityInterface(is_authorized=True)
        authority = PolkitAuthority(fake)
        self.assertTrue(authority.check_authorization(":1.42", "id.oflit.ChildTime1.status"))

    def test_denies_when_not_authorized(self):
        fake = FakeAuthorityInterface(is_authorized=False)
        authority = PolkitAuthority(fake)
        self.assertFalse(authority.check_authorization(":1.42", "id.oflit.ChildTime1.manage"))

    def test_fails_closed_on_exception(self):
        fake = FakeAuthorityInterface(raise_exc=RuntimeError("polkitd unreachable"))
        authority = PolkitAuthority(fake)
        self.assertFalse(authority.check_authorization(":1.42", "id.oflit.ChildTime1.manage"))

    def test_fails_closed_on_missing_sender(self):
        fake = FakeAuthorityInterface(is_authorized=True)
        authority = PolkitAuthority(fake)
        self.assertFalse(authority.check_authorization("", "id.oflit.ChildTime1.manage"))
        self.assertFalse(authority.check_authorization(None, "id.oflit.ChildTime1.manage"))
        # a missing sender must never even reach the authority
        self.assertEqual(fake.calls, [])

    def test_fails_closed_on_malformed_reply(self):
        class WeirdAuthority:
            def CheckAuthorization(self, *args):
                return None  # not subscriptable the way real replies are

        authority = PolkitAuthority(WeirdAuthority())
        self.assertFalse(authority.check_authorization(":1.42", "id.oflit.ChildTime1.manage"))

    def test_subject_is_system_bus_name_of_caller(self):
        fake = FakeAuthorityInterface(is_authorized=True)
        authority = PolkitAuthority(fake)
        authority.check_authorization(":1.99", "id.oflit.ChildTime1.status")
        subject, action_id, details, flags, cancellation_id = fake.calls[0]
        self.assertEqual(subject, ("system-bus-name", {"name": ":1.99"}))
        self.assertEqual(action_id, "id.oflit.ChildTime1.status")


if __name__ == "__main__":
    unittest.main()
