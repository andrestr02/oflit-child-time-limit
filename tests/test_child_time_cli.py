#!/usr/bin/python3

import importlib.machinery
import importlib.util
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "child-time"


def load_cli(name="child_time_cli_test"):
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"Cannot create import spec for {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


child_time = load_cli()


class AdapterContractTests(unittest.TestCase):
    def test_cli_loads_sibling_core(self):
        self.assertEqual(
            Path(child_time.core.__file__).resolve(),
            (ROOT / "src" / "child_time_core.py").resolve(),
        )

    def test_compatibility_aliases_point_to_core(self):
        names = (
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
            "_atomic_update_limit_locked",
            "confirm_reduction",
            "status_rows",
            "apply_limit_transaction",
            "local_now",
            "today",
            "fmt",
            "human",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(child_time, name),
                    getattr(child_time.core, name),
                )

    def test_status_output_uses_core_rows(self):
        row = child_time.StatusRow(
            username="child",
            used=1800,
            remaining=1800,
            limit=3600,
            status="AVAILABLE",
        )

        output = StringIO()
        with mock.patch.object(
            child_time.core,
            "status_rows",
            return_value=[row],
        ), redirect_stdout(output):
            child_time.print_status()

        text = output.getvalue()
        self.assertIn("child", text)
        self.assertIn("00:30:00", text)
        self.assertIn("01:00:00", text)
        self.assertIn("AVAILABLE", text)


class CommandAdapterTests(unittest.TestCase):
    def test_set_delegates_to_core_transaction(self):
        result = child_time.LimitMutationResult(
            username="child",
            used=0,
            old_limit=3600,
            new_limit=7200,
            remaining=7200,
            reason=None,
        )

        with mock.patch.object(
            child_time.core,
            "apply_limit_transaction",
            return_value=result,
        ) as apply, redirect_stdout(StringIO()):
            child_time.command_set(
                Namespace(username="child", duration="2h", force=False)
            )

        apply.assert_called_once_with(
            "child",
            7200,
            force=False,
        )

    def test_add_delegates_atomic_calculation_to_core(self):
        result = child_time.LimitMutationResult(
            username="child",
            used=0,
            old_limit=3600,
            new_limit=5400,
            remaining=5400,
            reason="add 30m",
        )

        with mock.patch.object(
            child_time.core,
            "apply_limit_transaction",
            return_value=result,
        ) as apply, redirect_stdout(StringIO()):
            child_time.command_add(
                Namespace(username="child", duration="30m")
            )

        args, kwargs = apply.call_args
        self.assertEqual(args[0], "child")
        self.assertEqual(args[1](3600, 0), 5400)
        self.assertEqual(kwargs["reason"], "add 30m")

    def test_subtract_delegates_atomic_calculation_to_core(self):
        result = child_time.LimitMutationResult(
            username="child",
            used=0,
            old_limit=7200,
            new_limit=6300,
            remaining=6300,
            reason="subtract 15m",
        )

        with mock.patch.object(
            child_time.core,
            "apply_limit_transaction",
            return_value=result,
        ) as apply, redirect_stdout(StringIO()):
            child_time.command_subtract(
                Namespace(
                    username="child",
                    duration="15m",
                    force=False,
                )
            )

        args, kwargs = apply.call_args
        self.assertEqual(args[0], "child")
        self.assertEqual(args[1](7200, 0), 6300)
        self.assertFalse(kwargs["force"])
        self.assertEqual(kwargs["reason"], "subtract 15m")

    def test_until_remains_active_use_quota_not_wall_clock_expiry(self):
        now = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
        target = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)

        result = child_time.LimitMutationResult(
            username="child",
            used=1800,
            old_limit=7200,
            new_limit=5400,
            remaining=3600,
            reason="continuous active use until 10:00",
        )

        with mock.patch.object(
            child_time.core,
            "local_now",
            return_value=now,
        ), mock.patch.object(
            child_time.core,
            "parse_clock",
            return_value=target,
        ), mock.patch.object(
            child_time.core,
            "apply_limit_transaction",
            return_value=result,
        ) as apply, redirect_stdout(StringIO()):
            child_time.command_until(
                Namespace(username="child", clock="10:00")
            )

        args, kwargs = apply.call_args

        self.assertEqual(args[0], "child")

        # 09:00 -> 10:00 = 3600 seconds remaining ACTIVE USE.
        # Existing usage is supplied by the core transaction at mutation
        # time, so the resulting daily limit becomes used + 3600.
        self.assertEqual(args[1](7200, 1800), 5400)

        self.assertEqual(
            kwargs["reason"],
            "continuous active use until 10:00",
        )


class InstallerUpgradeTests(unittest.TestCase):
    def test_installer_explicitly_restarts_enforcer_after_upgrade(self):
        installer = (ROOT / "install.sh").read_text()

        enable = "systemctl enable child-time-enforcer.service"
        restart = "systemctl restart child-time-enforcer.service"
        old_enable_now = "systemctl enable --now child-time-enforcer.service"

        self.assertIn(enable, installer)
        self.assertIn(restart, installer)
        self.assertNotIn(old_enable_now, installer)

        artifact_install = (
            'install -m 0755 "$ROOT_DIR/src/child-time-enforcer" '
            '/usr/local/sbin/child-time-enforcer'
        )
        daemon_reload = "systemctl daemon-reload"

        self.assertLess(
            installer.index(artifact_install),
            installer.index(daemon_reload),
        )
        self.assertLess(
            installer.index(daemon_reload),
            installer.index(restart),
        )


class LegacyStatusTests(unittest.TestCase):
    def test_usage_above_limit_is_reported_without_capping(self):
        script = ROOT / "src" / "child-time-status"

        loader = importlib.machinery.SourceFileLoader(
            "child_time_legacy_status",
            str(script),
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

            with mock.patch.object(
                legacy_status.os,
                "geteuid",
                return_value=0,
            ), redirect_stdout(output):
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
