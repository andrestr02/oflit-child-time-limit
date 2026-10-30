"""Regression cover for the enforcer's logind/loginctl hardening.

These behaviours were applied to the production host out of band and
were missing from the source tree. They are security-relevant: a hung
or failing `loginctl` must never be read as "nobody is logged in",
because that would silently stop charging usage.
"""

import importlib.machinery
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENFORCER = ROOT / "src" / "child-time-enforcer"


def load_enforcer():
    loader = importlib.machinery.SourceFileLoader(
        "child_time_enforcer_resilience_test",
        str(ENFORCER),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["loginctl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class LoginctlTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.enforcer = load_enforcer()

    def test_both_loginctl_calls_are_time_bound(self):
        source = ENFORCER.read_text()
        self.assertEqual(source.count("timeout=5"), 2, "both calls must time out")

    def test_list_sessions_timeout_raises_runtime_error(self):
        with mock.patch.object(
            self.enforcer.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="loginctl", timeout=5),
        ):
            with self.assertRaises(RuntimeError):
                self.enforcer.active_graphical_users({"someuser": 3600})

    def test_list_sessions_failure_raises_runtime_error(self):
        with mock.patch.object(
            self.enforcer.subprocess,
            "run",
            return_value=completed(returncode=1, stderr="boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.enforcer.active_graphical_users({"someuser": 3600})

    def test_show_session_failure_raises_runtime_error(self):
        calls = [
            completed(stdout="c1 1000 someuser seat0\n"),
            completed(returncode=1, stderr="gone"),
        ]
        with mock.patch.object(
            self.enforcer.subprocess, "run", side_effect=calls
        ):
            with self.assertRaises(RuntimeError):
                self.enforcer.active_graphical_users({"someuser": 3600})

    def test_show_session_timeout_raises_runtime_error(self):
        calls = [
            completed(stdout="c1 1000 someuser seat0\n"),
            subprocess.TimeoutExpired(cmd="loginctl", timeout=5),
        ]
        with mock.patch.object(
            self.enforcer.subprocess, "run", side_effect=calls
        ):
            with self.assertRaises(RuntimeError):
                self.enforcer.active_graphical_users({"someuser": 3600})

    def test_healthy_logind_returns_active_configured_users(self):
        calls = [
            completed(stdout="c1 1000 someuser seat0\n"),
            completed(
                stdout="Name=someuser\nType=wayland\nClass=user\nActive=yes\n"
            ),
        ]
        with mock.patch.object(
            self.enforcer.subprocess, "run", side_effect=calls
        ):
            self.assertEqual(
                self.enforcer.active_graphical_users({"someuser": 3600}),
                {"someuser"},
            )

    def test_unconfigured_user_is_never_returned(self):
        calls = [
            completed(stdout="c1 1000 adminuser seat0\n"),
            completed(
                stdout="Name=adminuser\nType=wayland\nClass=user\nActive=yes\n"
            ),
        ]
        with mock.patch.object(
            self.enforcer.subprocess, "run", side_effect=calls
        ):
            self.assertEqual(
                self.enforcer.active_graphical_users({"someuser": 3600}),
                set(),
            )


class FailClosedCallSiteTests(unittest.TestCase):
    def test_loop_keeps_previous_active_users_when_logind_fails(self):
        """A logind blip must not pause accounting for active children."""
        source = ENFORCER.read_text()
        self.assertIn(
            "        try:\n"
            "            current_active = active_graphical_users(limits)\n"
            "        except RuntimeError:",
            source,
        )
        self.assertIn("current_active = set(previous_active)", source)

    def test_fail_closed_branch_is_documented(self):
        source = ENFORCER.read_text()
        self.assertIn("Fail closed", source)


if __name__ == "__main__":
    unittest.main()
