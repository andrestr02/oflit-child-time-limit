#!/usr/bin/python3

import importlib.machinery
import importlib.util
import multiprocessing
import os
import tempfile
import threading
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timezone
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


def process_add_worker(script, config, barrier, results):
    try:
        worker_loader = importlib.machinery.SourceFileLoader(
            f"child_time_worker_{os.getpid()}", script
        )
        worker_spec = importlib.util.spec_from_loader(
            worker_loader.name, worker_loader
        )
        worker_module = importlib.util.module_from_spec(worker_spec)
        worker_loader.exec_module(worker_module)
        worker_module.validate_username = lambda username: None
        worker_module.read_used = lambda username: 0
        barrier.wait(timeout=5)
        with redirect_stdout(StringIO()):
            worker_module.apply_limit(
                "hudzaifah",
                lambda old_limit, used: old_limit + 300,
                path=Path(config),
            )
        results.put(None)
    except BaseException as exc:
        results.put(f"{type(exc).__name__}: {exc}")


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

    def test_runtime_default_config_is_patchable(self):
        with mock.patch.object(child_time, "CONFIG", self.config):
            _, limits, _ = child_time.load_config()
        self.assertEqual(limits["hudzaifah"], 7200)

    def test_concurrent_adds_do_not_lose_an_update(self):
        barrier = threading.Barrier(3)
        errors = []

        def add_time():
            try:
                barrier.wait()
                child_time.command_add(Namespace(username="hudzaifah", duration="5m"))
            except Exception as exc:  # surfaced in the main test thread below
                errors.append(exc)

        with mock.patch.object(child_time, "CONFIG", self.config), \
             mock.patch.object(child_time, "validate_username"), \
             mock.patch.object(child_time, "read_used", return_value=0), \
             redirect_stdout(StringIO()):
            threads = [threading.Thread(target=add_time) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7800)

    def test_concurrent_subtracts_do_not_lose_an_update(self):
        barrier = threading.Barrier(3)
        errors = []

        def subtract_time():
            try:
                barrier.wait()
                child_time.command_subtract(
                    Namespace(username="hudzaifah", duration="5m", force=False)
                )
            except Exception as exc:  # surfaced in the main test thread below
                errors.append(exc)

        with mock.patch.object(child_time, "CONFIG", self.config), \
             mock.patch.object(child_time, "validate_username"), \
             mock.patch.object(child_time, "read_used", return_value=0), \
             redirect_stdout(StringIO()):
            threads = [threading.Thread(target=subtract_time) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 6600)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(child_time.fcntl, "flock"),
        "requires POSIX flock semantics",
    )
    def test_separate_process_adds_do_not_lose_an_update(self):
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(3)
        results = context.Queue()
        processes = [
            context.Process(
                target=process_add_worker,
                args=(str(SCRIPT), str(self.config), barrier, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        barrier.wait(timeout=5)
        for process in processes:
            process.join(timeout=5)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

        child_results = [results.get(timeout=1) for _ in processes]
        self.assertEqual(child_results, [None, None])
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7800)

    def test_write_failure_is_reported_and_original_is_preserved(self):
        with mock.patch.object(child_time.os, "replace", side_effect=OSError("full")):
            with self.assertRaisesRegex(child_time.ChildTimeError, "Cannot update"):
                child_time.atomic_update_limit("hudzaifah", 8700, path=self.config)
        self.assertIn("hudzaifah=7200", self.config.read_text())
        leftovers = [
            path for path in self.root.glob(self.config.name + ".*")
            if path.name != self.config.name + ".lock"
        ]
        self.assertEqual(leftovers, [])

    def test_mutation_oserror_is_not_reported_as_lock_failure(self):
        with mock.patch.object(
            child_time, "_atomic_update_limit_locked", side_effect=OSError("body")
        ):
            with self.assertRaisesRegex(OSError, "body"):
                child_time.atomic_update_limit("hudzaifah", 8700, path=self.config)

    def test_until_uses_current_usage_inside_mutation(self):
        now = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
        with mock.patch.object(child_time, "CONFIG", self.config), \
             mock.patch.object(child_time, "validate_username"), \
             mock.patch.object(child_time, "local_now", return_value=now), \
             mock.patch.object(child_time, "read_used", return_value=1800), \
             redirect_stdout(StringIO()):
            child_time.command_until(Namespace(username="hudzaifah", clock="10:00"))
        _, limits, _ = child_time.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 5400)


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

class StatusRowsTests(unittest.TestCase):
    def test_status_preserves_usage_above_forced_lower_limit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = root / "child-time-limit.conf"
            state_dir = root / "state"
            state_dir.mkdir()

            config.write_text("child=3600\n")
            (state_dir / "child.state").write_text(
                f"{child_time.today()} 5400\n"
            )

            with mock.patch.object(child_time, "CONFIG", config), \
                 mock.patch.object(child_time, "STATE_DIR", state_dir):
                rows = child_time.status_rows()

            self.assertEqual(rows[0][0], "child")
            self.assertEqual(rows[0][1], 5400)
            self.assertEqual(rows[0][2], 0)
            self.assertEqual(rows[0][3], 3600)
            self.assertEqual(rows[0][4], "EXHAUSTED")

    def test_runtime_default_state_dir_is_patchable_and_stale_state_is_zero(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            (state_dir / "child.state").write_text("2000-01-01 9999\n")
            with mock.patch.object(child_time, "STATE_DIR", state_dir):
                self.assertEqual(child_time.read_used("child"), 0)

    def test_missing_state_is_zero(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.object(child_time, "STATE_DIR", Path(tempdir)):
                self.assertEqual(child_time.read_used("child"), 0)

if __name__ == "__main__":
    unittest.main()
