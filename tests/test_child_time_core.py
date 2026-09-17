#!/usr/bin/python3

import importlib.util
import multiprocessing
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "src" / "child_time_core.py"

spec = importlib.util.spec_from_file_location("child_time_core_test", CORE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load {CORE_PATH}")

core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def process_add_worker(core_path, config, state_dir, barrier, results):
    try:
        spec = importlib.util.spec_from_file_location(
            f"child_time_core_worker_{os.getpid()}",
            core_path,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        module.validate_username = lambda username: None
        barrier.wait(timeout=5)

        module.apply_limit_transaction(
            "hudzaifah",
            lambda old_limit, used: old_limit + 300,
            config_path=Path(config),
            state_dir=Path(state_dir),
        )
        results.put(None)
    except BaseException as exc:
        results.put(f"{type(exc).__name__}: {exc}")


class ParseDurationTests(unittest.TestCase):
    def test_human_durations(self):
        self.assertEqual(core.parse_duration("90m"), 5400)
        self.assertEqual(core.parse_duration("2h25m"), 8700)
        self.assertEqual(core.parse_duration("3h"), 10800)
        self.assertEqual(core.parse_duration("30s"), 30)

    def test_plain_integer_means_seconds_for_backward_compatibility(self):
        self.assertEqual(core.parse_duration("7200"), 7200)

    def test_invalid_duration(self):
        for value in ("", "0m", "-5m", "2 hours", "1h30x"):
            with self.subTest(value=value):
                with self.assertRaises(core.ChildTimeError):
                    core.parse_duration(value)


class ClockTests(unittest.TestCase):
    def test_parse_future_clock(self):
        now = datetime(2026, 9, 4, 9, 11, tzinfo=timezone.utc)
        target = core.parse_clock("10:45", now=now)
        self.assertEqual((target.hour, target.minute), (10, 45))

    def test_reject_past_clock(self):
        now = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
        with self.assertRaises(core.ChildTimeError):
            core.parse_clock("09:59", now=now)


class ConfigUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "child-time-limit.conf"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.config.write_text(
            "# username=seconds-per-day\n"
            "azzahra=7200\n"
            "hudzaifah=7200\n"
            "ibrohim=7200\n"
        )
        os.chmod(self.config, 0o600)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_atomic_update_preserves_other_users_and_mode(self):
        old_limit, new_limit = core.atomic_update_limit(
            "hudzaifah", 8700, path=self.config
        )
        self.assertEqual((old_limit, new_limit), (7200, 8700))
        self.assertEqual(
            self.config.read_text(),
            "# username=seconds-per-day\n"
            "azzahra=7200\n"
            "hudzaifah=8700\n"
            "ibrohim=7200\n",
        )
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_unknown_user_refused(self):
        with self.assertRaises(core.ChildTimeError):
            core.atomic_update_limit("unknown", 3600, path=self.config)

    def test_duplicate_user_refused(self):
        self.config.write_text("child=3600\nchild=7200\n")
        with self.assertRaises(core.ChildTimeError):
            core.load_config(self.config)

    def test_runtime_default_config_is_patchable(self):
        with mock.patch.object(core, "CONFIG", self.config):
            _, limits, _ = core.load_config()
        self.assertEqual(limits["hudzaifah"], 7200)

    def test_concurrent_adds_do_not_lose_update(self):
        barrier = threading.Barrier(3)
        errors = []

        def add():
            try:
                barrier.wait()
                core.apply_limit_transaction(
                    "hudzaifah",
                    lambda old, used: old + 300,
                    config_path=self.config,
                    state_dir=self.state_dir,
                )
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(core, "validate_username"):
            threads = [threading.Thread(target=add) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7800)

    def test_concurrent_subtracts_do_not_lose_update(self):
        barrier = threading.Barrier(3)
        errors = []

        def subtract():
            try:
                barrier.wait()
                core.apply_limit_transaction(
                    "hudzaifah",
                    lambda old, used: old - 300,
                    config_path=self.config,
                    state_dir=self.state_dir,
                )
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(core, "validate_username"):
            threads = [threading.Thread(target=subtract) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 6600)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(core.fcntl, "flock"),
        "requires POSIX flock semantics",
    )
    def test_separate_process_adds_do_not_lose_update(self):
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(3)
        results = context.Queue()

        processes = [
            context.Process(
                target=process_add_worker,
                args=(
                    str(CORE_PATH),
                    str(self.config),
                    str(self.state_dir),
                    barrier,
                    results,
                ),
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
        self.assertEqual([p.exitcode for p in processes], [0, 0])

        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7800)

    def test_write_failure_preserves_original(self):
        with mock.patch.object(
            core.os, "replace", side_effect=OSError("full")
        ):
            with self.assertRaisesRegex(core.ChildTimeError, "Cannot update"):
                core.atomic_update_limit(
                    "hudzaifah", 8700, path=self.config
                )

        self.assertIn("hudzaifah=7200", self.config.read_text())

        leftovers = [
            path
            for path in self.root.glob(self.config.name + ".*")
            if path.name != self.config.name + ".lock"
        ]
        self.assertEqual(leftovers, [])

    def test_mutation_oserror_not_reported_as_lock_failure(self):
        with mock.patch.object(
            core,
            "_atomic_update_limit_locked",
            side_effect=OSError("body"),
        ):
            with self.assertRaisesRegex(OSError, "body"):
                core.atomic_update_limit(
                    "hudzaifah", 8700, path=self.config
                )


class ReductionGuardTests(unittest.TestCase):
    def test_reduction_below_used_requires_force(self):
        with self.assertRaises(core.ChildTimeError):
            core.confirm_reduction(
                "child",
                used=4000,
                old_limit=7200,
                new_limit=3600,
                force=False,
            )

    def test_force_allows_reduction_below_used(self):
        core.confirm_reduction(
            "child",
            used=4000,
            old_limit=7200,
            new_limit=3600,
            force=True,
        )


class UsernameTests(unittest.TestCase):
    def test_invalid_username_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            core.validate_username("../root", require_local=False)

    def test_valid_username_format(self):
        core.validate_username("child_1", require_local=False)


class StateAndStatusTests(unittest.TestCase):
    def test_status_preserves_usage_above_forced_lower_limit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = root / "child-time-limit.conf"
            state_dir = root / "state"
            state_dir.mkdir()

            config.write_text("child=3600\n")
            (state_dir / "child.state").write_text(
                f"{core.today()} 5400\n"
            )

            schedule = root / "child-time-schedule.conf"
            schedule.write_text("")

            rows = core.status_rows(
                config_path=config,
                state_dir=state_dir,
                schedule_path=schedule,
            )

            self.assertEqual(rows[0].username, "child")
            self.assertEqual(rows[0].used, 5400)
            self.assertEqual(rows[0].remaining, 0)
            self.assertEqual(rows[0].limit, 3600)
            self.assertEqual(rows[0].status, "EXHAUSTED")

    def test_runtime_default_state_dir_is_patchable_and_stale_is_zero(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            (state_dir / "child.state").write_text(
                "2000-01-01 9999\n"
            )
            with mock.patch.object(core, "STATE_DIR", state_dir):
                self.assertEqual(core.read_used("child"), 0)

    def test_missing_state_is_zero(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.object(core, "STATE_DIR", Path(tempdir)):
                self.assertEqual(core.read_used("child"), 0)


if __name__ == "__main__":
    unittest.main()
