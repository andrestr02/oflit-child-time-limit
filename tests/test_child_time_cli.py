#!/usr/bin/python3

import importlib.util
import os
import pwd
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "child-time"

spec = importlib.util.spec_from_file_location("child_time_cli", SCRIPT)
child_time = importlib.util.module_from_spec(spec)
spec.loader.exec_module(child_time)


class ParseDurationTests(unittest.TestCase):
    def test_human_durations(self):
        self.assertEqual(child_time.parse_duration("90m"), 5400)
        self.assertEqual(child_time.parse_duration("2h25m"), 8700)
        self.assertEqual(child_time.parse_duration("3h"), 10800)
        self.assertEqual(child_time.parse_duration("30s"), 30)

    def test_plain_integer_means_seconds_for_backward_compatibility(self):
        self.assertEqual(child_time.parse_duration("7200"), 7200)

    def test_invalid_duration(self):
        for value in ("", "0m", "-5m", "2 hours", "1h30x"):
            with self.subTest(value=value):
                with self.assertRaises(child_time.ChildTimeError):
                    child_time.parse_duration(value)


class ClockTests(unittest.TestCase):
    def test_parse_future_clock(self):
        now = datetime(2026, 9, 4, 9, 11, tzinfo=timezone.utc)
        target = child_time.parse_clock("10:45", now=now)
        self.assertEqual(target.hour, 10)
        self.assertEqual(target.minute, 45)

    def test_reject_past_clock(self):
        now = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
        with self.assertRaises(child_time.ChildTimeError):
            child_time.parse_clock("09:59", now=now)


class ConfigUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "child-time-limit.conf"
        self.config.write_text(
            "# username=seconds-per-day\nazzahra=7200\nhudzaifah=7200\nibrohim=7200\n"
        )
        os.chmod(self.config, 0o600)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_atomic_update_preserves_other_users_and_mode(self):
        old_limit, new_limit = child_time.atomic_update_limit(
            "hudzaifah", 8700, path=self.config
        )
        self.assertEqual(old_limit, 7200)
        self.assertEqual(new_limit, 8700)
        self.assertEqual(
            self.config.read_text(),
            "# username=seconds-per-day\nazzahra=7200\nhudzaifah=8700\nibrohim=7200\n",
        )
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_unknown_user_refused(self):
        with self.assertRaises(child_time.ChildTimeError):
            child_time.atomic_update_limit("unknown", 3600, path=self.config)

    def test_duplicate_user_refused(self):
        self.config.write_text("child=3600\nchild=7200\n")
        with self.assertRaises(child_time.ChildTimeError):
            child_time.load_config(self.config)


class ReductionGuardTests(unittest.TestCase):
    def test_reduction_below_used_requires_force(self):
        with self.assertRaises(child_time.ChildTimeError):
            child_time.confirm_reduction(
                "child", used=4000, old_limit=7200, new_limit=3600, force=False
            )

    def test_force_allows_reduction_below_used(self):
        child_time.confirm_reduction(
            "child", used=4000, old_limit=7200, new_limit=3600, force=True
        )


class UsernameTests(unittest.TestCase):
    def test_invalid_username_rejected(self):
        with self.assertRaises(child_time.ChildTimeError):
            child_time.validate_username("../root", require_local=False)

    def test_valid_username_format(self):
        child_time.validate_username("child_1", require_local=False)


if __name__ == "__main__":
    unittest.main()
