#!/usr/bin/python3

import ast
import importlib.machinery
import importlib.util
import multiprocessing
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CORE_SCRIPT = ROOT / "src" / "child_time_core.py"


def _load_core(name="child_time_core_under_test"):
    loader = importlib.machinery.SourceFileLoader(name, str(CORE_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"Cannot create import spec for {CORE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    output = StringIO()
    with redirect_stdout(output):
        loader.exec_module(module)
    return module, output.getvalue()


core, _IMPORT_OUTPUT = _load_core()


def process_transaction_worker(script, config, state_dir, delta, barrier, results):
    try:
        worker, _ = _load_core(f"child_time_core_worker_{os.getpid()}")
        worker.validate_username = lambda username, require_local=True: None
        barrier.wait(timeout=5)
        worker.apply_limit_transaction(
            "hudzaifah",
            lambda old_limit, used: old_limit + delta,
            config_path=Path(config),
            state_dir=Path(state_dir),
        )
        results.put(None)
    except BaseException as exc:  # noqa: BLE001 - surfaced in parent test
        results.put(f"{type(exc).__name__}: {exc}")


class ImportHygieneTests(unittest.TestCase):
    def test_core_imports_without_output(self):
        self.assertEqual(_IMPORT_OUTPUT, "")

    def test_core_contains_no_forbidden_constructs(self):
        tree = ast.parse(CORE_SCRIPT.read_text(), filename=str(CORE_SCRIPT))

        forbidden_imports = {
            "argparse",
            "subprocess",
            "dbus",
            "polkit",
        }
        forbidden_calls = {"sys.exit"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name, forbidden_imports,
                        f"unexpected import: {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    node.module, forbidden_imports,
                    f"unexpected import: {node.module}",
                )
            elif isinstance(node, ast.Name) and node.id == "argparse":
                self.fail("argparse referenced in core")
            elif isinstance(node, ast.FunctionDef) and node.name == "main":
                self.fail("main() defined in core")
            elif isinstance(node, ast.Call):
                target = None
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        target = f"{node.func.value.id}.{node.func.attr}"
                elif isinstance(node.func, ast.Name):
                    target = node.func.id
                self.assertNotIn(target, forbidden_calls | {"exit", "quit"})

        source = CORE_SCRIPT.read_text()
        self.assertNotIn("os.geteuid", source)
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("systemctl", source.split('"""', 2)[-1])


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
        self.assertEqual(target.hour, 10)
        self.assertEqual(target.minute, 45)

    def test_reject_past_clock(self):
        now = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
        with self.assertRaises(core.ChildTimeError):
            core.parse_clock("09:59", now=now)


class UsernameTests(unittest.TestCase):
    def test_invalid_username_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            core.validate_username("../root", require_local=False)

    def test_valid_username_format(self):
        core.validate_username("child_1", require_local=False)


class ConfigParsingTests(unittest.TestCase):
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

    def test_config_parsing(self):
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits, {"azzahra": 7200, "hudzaifah": 7200, "ibrohim": 7200})

    def test_duplicate_user_refused(self):
        self.config.write_text("child=3600\nchild=7200\n")
        with self.assertRaises(core.ChildTimeError):
            core.load_config(self.config)

    def test_runtime_default_config_is_patchable(self):
        with mock.patch.object(core, "CONFIG", self.config):
            _, limits, _ = core.load_config()
        self.assertEqual(limits["hudzaifah"], 7200)


class ReadUsedTests(unittest.TestCase):
    def test_stale_state_returns_zero(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            (state_dir / "child.state").write_text("2000-01-01 9999\n")
            self.assertEqual(core.read_used("child", state_dir=state_dir), 0)

    def test_missing_state_returns_zero(self):
        with tempfile.TemporaryDirectory() as tempdir:
            self.assertEqual(core.read_used("child", state_dir=Path(tempdir)), 0)

    def test_runtime_default_state_dir_is_patchable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            (state_dir / "child.state").write_text("2000-01-01 9999\n")
            with mock.patch.object(core, "STATE_DIR", state_dir):
                self.assertEqual(core.read_used("child"), 0)

    def test_explicit_state_dir_without_patching_default(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)
            (state_dir / "child.state").write_text(f"{core.today()} 42\n")
            # STATE_DIR is intentionally left untouched.
            self.assertEqual(core.read_used("child", state_dir=state_dir), 42)


class ImmutableTypeTests(unittest.TestCase):
    def test_status_row_is_immutable(self):
        row = core.StatusRow("child", 10, 20, 30, "AVAILABLE")
        with self.assertRaises(AttributeError):
            row.used = 99

    def test_limit_mutation_result_is_immutable(self):
        result = core.LimitMutationResult(
            username="child", used=10, old_limit=20, new_limit=30, remaining=20
        )
        with self.assertRaises(AttributeError):
            result.new_limit = 99


class StatusRowsTests(unittest.TestCase):
    def test_raw_usage_above_limit_is_preserved(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = root / "child-time-limit.conf"
            state_dir = root / "state"
            state_dir.mkdir()

            config.write_text("child=3600\n")
            (state_dir / "child.state").write_text(f"{core.today()} 5400\n")

            rows = core.status_rows(config_path=config, state_dir=state_dir)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.username, "child")
        self.assertEqual(row.used, 5400)
        self.assertEqual(row.remaining, 0)
        self.assertEqual(row.limit, 3600)
        self.assertEqual(row.status, "EXHAUSTED")


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self.root / "child-time-limit.conf"
        self.config.write_text(
            "# username=seconds-per-day\nazzahra=7200\nhudzaifah=7200\nibrohim=7200\n"
        )
        os.chmod(self.config, 0o600)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        # These tests exercise transaction/locking semantics with a
        # fictitious username, not real-account validation (which is
        # exercised separately in UsernameTests).
        self.username_patch = mock.patch.object(
            core, "validate_username", lambda username, require_local=True: None
        )
        self.username_patch.start()
        self.addCleanup(self.username_patch.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_state(self, username, used, day=None):
        day = day or core.today()
        (self.state_dir / f"{username}.state").write_text(f"{day} {used}\n")

    def test_mutation_prints_nothing(self):
        output = StringIO()
        with redirect_stdout(output):
            core.apply_limit_transaction(
                "hudzaifah",
                lambda old_limit, used: 8700,
                config_path=self.config,
                state_dir=self.state_dir,
            )
        self.assertEqual(output.getvalue(), "")

    def test_explicit_temporary_config_path(self):
        result = core.apply_limit_transaction(
            "hudzaifah",
            lambda old_limit, used: 8700,
            config_path=self.config,
            state_dir=self.state_dir,
        )
        self.assertEqual(result.old_limit, 7200)
        self.assertEqual(result.new_limit, 8700)

    def test_explicit_temporary_state_dir_without_patching_state_dir(self):
        self._write_state("hudzaifah", 1800)
        result = core.apply_limit_transaction(
            "hudzaifah",
            lambda old_limit, used: old_limit + 60,
            config_path=self.config,
            state_dir=self.state_dir,
        )
        self.assertEqual(result.used, 1800)

    def test_mutation_never_changes_usage_state_bytes(self):
        self._write_state("hudzaifah", 1800)
        state_path = self.state_dir / "hudzaifah.state"
        before = state_path.read_bytes()
        core.apply_limit_transaction(
            "hudzaifah",
            lambda old_limit, used: old_limit + 60,
            config_path=self.config,
            state_dir=self.state_dir,
        )
        after = state_path.read_bytes()
        self.assertEqual(before, after)

    def test_mutation_never_changes_usage_state_mtime(self):
        self._write_state("hudzaifah", 1800)
        state_path = self.state_dir / "hudzaifah.state"
        before_mtime = state_path.stat().st_mtime_ns
        core.apply_limit_transaction(
            "hudzaifah",
            lambda old_limit, used: old_limit + 60,
            config_path=self.config,
            state_dir=self.state_dir,
        )
        after_mtime = state_path.stat().st_mtime_ns
        self.assertEqual(before_mtime, after_mtime)

    def test_reduction_below_used_rejected_without_force(self):
        self._write_state("hudzaifah", 4000)
        with self.assertRaises(core.ChildTimeError):
            core.apply_limit_transaction(
                "hudzaifah",
                lambda old_limit, used: 3600,
                config_path=self.config,
                state_dir=self.state_dir,
            )
        # Config must be unchanged after the refused reduction.
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7200)

    def test_forced_reduction_accepted_and_preserves_used(self):
        self._write_state("hudzaifah", 5400)
        result = core.apply_limit_transaction(
            "hudzaifah",
            lambda old_limit, used: 3600,
            force=True,
            config_path=self.config,
            state_dir=self.state_dir,
        )
        self.assertEqual(result.used, 5400)
        self.assertEqual(result.new_limit, 3600)
        self.assertEqual(result.remaining, 0)

    def test_unknown_user_refused(self):
        with self.assertRaises(core.ChildTimeError):
            core.apply_limit_transaction(
                "unknown",
                lambda old_limit, used: 3600,
                config_path=self.config,
                state_dir=self.state_dir,
            )

    def test_until_uses_authoritative_usage_inside_transaction(self):
        now = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
        target = core.parse_clock("10:00", now=now)
        seconds_until = int((target - now).total_seconds())
        self._write_state("hudzaifah", 1800)

        result = core.apply_limit_transaction(
            "hudzaifah",
            lambda old_limit, used: used + seconds_until,
            reason="until",
            config_path=self.config,
            state_dir=self.state_dir,
        )
        self.assertEqual(result.new_limit, 1800 + seconds_until)

    def test_add_recalculates_authoritative_current_limit_inside_lock(self):
        barrier = threading.Barrier(3)
        errors = []

        def add_time():
            try:
                barrier.wait()
                core.apply_limit_transaction(
                    "hudzaifah",
                    lambda old_limit, used: old_limit + 300,
                    config_path=self.config,
                    state_dir=self.state_dir,
                )
            except Exception as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=add_time) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7800)

    def test_subtract_recalculates_authoritative_current_limit_inside_lock(self):
        barrier = threading.Barrier(3)
        errors = []

        def subtract_time():
            try:
                barrier.wait()

                def subtract(old_limit, used):
                    return old_limit - 300

                core.apply_limit_transaction(
                    "hudzaifah",
                    subtract,
                    config_path=self.config,
                    state_dir=self.state_dir,
                )
            except Exception as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=subtract_time) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 6600)

    def test_threaded_concurrent_writers_do_not_lose_updates(self):
        barrier = threading.Barrier(6)
        errors = []

        def add_time():
            try:
                barrier.wait()
                core.apply_limit_transaction(
                    "hudzaifah",
                    lambda old_limit, used: old_limit + 60,
                    config_path=self.config,
                    state_dir=self.state_dir,
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=add_time) for _ in range(5)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7200 + 5 * 60)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(core.fcntl, "flock"),
        "requires POSIX flock semantics",
    )
    def test_separate_process_concurrent_writers_do_not_lose_updates(self):
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(3)
        results = context.Queue()
        processes = [
            context.Process(
                target=process_transaction_worker,
                args=(
                    str(self.config),
                    str(self.config),
                    str(self.state_dir),
                    300,
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
        self.assertEqual([process.exitcode for process in processes], [0, 0])
        _, limits, _ = core.load_config(self.config)
        self.assertEqual(limits["hudzaifah"], 7800)

    def test_atomic_write_preserves_comments_order_mode_ownership(self):
        st_before = self.config.stat()
        old_limit, new_limit = core.atomic_update_limit(
            "hudzaifah", 8700, path=self.config
        )
        self.assertEqual(old_limit, 7200)
        self.assertEqual(new_limit, 8700)
        self.assertEqual(
            self.config.read_text(),
            "# username=seconds-per-day\nazzahra=7200\nhudzaifah=8700\nibrohim=7200\n",
        )
        st_after = self.config.stat()
        self.assertEqual(st_after.st_mode & 0o7777, st_before.st_mode & 0o7777)
        self.assertEqual(st_after.st_uid, st_before.st_uid)
        self.assertEqual(st_after.st_gid, st_before.st_gid)

    def test_atomic_replacement_failure_preserves_original_config(self):
        with mock.patch.object(core.os, "replace", side_effect=OSError("full")):
            with self.assertRaisesRegex(core.ChildTimeError, "Cannot update"):
                core.atomic_update_limit("hudzaifah", 8700, path=self.config)
        self.assertIn("hudzaifah=7200", self.config.read_text())
        leftovers = [
            path for path in self.root.glob(self.config.name + ".*")
            if path.name != self.config.name + ".lock"
        ]
        self.assertEqual(leftovers, [])

    def test_mutation_body_oserror_is_not_mislabeled_as_lock_failure(self):
        with mock.patch.object(
            core, "_atomic_update_limit_locked", side_effect=OSError("body")
        ):
            with self.assertRaisesRegex(OSError, "body"):
                core.atomic_update_limit("hudzaifah", 8700, path=self.config)


class ReductionGuardTests(unittest.TestCase):
    def test_reduction_below_used_requires_force(self):
        with self.assertRaises(core.ChildTimeError):
            core.confirm_reduction(
                "child", used=4000, old_limit=7200, new_limit=3600, force=False
            )

    def test_force_allows_reduction_below_used(self):
        core.confirm_reduction(
            "child", used=4000, old_limit=7200, new_limit=3600, force=True
        )


if __name__ == "__main__":
    unittest.main()
