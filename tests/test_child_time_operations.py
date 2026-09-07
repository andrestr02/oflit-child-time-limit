#!/usr/bin/python3

import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "child_time_operations.py"

loader = importlib.machinery.SourceFileLoader("child_time_operations", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
if spec is None:
    raise RuntimeError(f"Cannot create import spec for {SCRIPT}")
ops = importlib.util.module_from_spec(spec)
loader.exec_module(ops)

core = ops.core


class TransactionScratchMixin:
    """Provides an isolated config/state pair, matching the pattern used
    by CommandDispatchTests in test_child_time_cli.py."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "child-time-limit.conf"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config.write_text("hudzaifah=7200\n")
        self.username_patch = mock.patch.object(
            core, "validate_username", lambda username, require_local=True: None
        )
        self.username_patch.start()
        self.addCleanup(self.username_patch.stop)
        self.addCleanup(self.tempdir.cleanup)

    def _apply(self, calculate_limit, **kwargs):
        return core.apply_limit_transaction(
            "hudzaifah",
            calculate_limit,
            config_path=self.config,
            state_dir=self.state_dir,
            **kwargs,
        )


class BuildSetTests(TransactionScratchMixin, unittest.TestCase):
    def test_set_returns_plain_seconds_and_no_reason(self):
        limit, reason = ops.build_set("2h25m")
        self.assertEqual(limit, 8700)
        self.assertIsNone(reason)

    def test_set_applies_via_transaction(self):
        limit, reason = ops.build_set("2h25m")
        result = self._apply(limit, reason=reason)
        self.assertEqual(result.new_limit, 8700)


class BuildAddTests(TransactionScratchMixin, unittest.TestCase):
    def test_add_reason_and_delta(self):
        calculate_limit, reason = ops.build_add("5m")
        self.assertEqual(reason, "add 5m")
        result = self._apply(calculate_limit, reason=reason)
        self.assertEqual(result.new_limit, 7500)


class BuildSubtractTests(TransactionScratchMixin, unittest.TestCase):
    def test_subtract_reason_and_delta(self):
        calculate_limit, reason = ops.build_subtract("5m")
        self.assertEqual(reason, "subtract 5m")
        result = self._apply(calculate_limit, reason=reason)
        self.assertEqual(result.new_limit, 6900)

    def test_subtract_to_zero_or_negative_rejected(self):
        calculate_limit, _ = ops.build_subtract("3h")
        with self.assertRaises(core.ChildTimeError):
            self._apply(calculate_limit)
        # config must remain untouched by the rejected calculation
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7200)


class BuildUntilTests(TransactionScratchMixin, unittest.TestCase):
    def test_until_uses_authoritative_usage(self):
        now = core.local_now().replace(hour=9, minute=0, second=0, microsecond=0)
        (self.state_dir / "hudzaifah.state").write_text(f"{core.today()} 1800\n")
        calculate_limit, reason = ops.build_until("10:00", now=now)
        self.assertEqual(reason, "continuous active use until 10:00")
        result = self._apply(calculate_limit)
        # 1800 used + 3600 seconds until 10:00 == 5400
        self.assertEqual(result.new_limit, 1800 + 3600)


if __name__ == "__main__":
    unittest.main()
