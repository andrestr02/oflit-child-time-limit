import importlib.util
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CORE = load_module(
    "child_time_core_access_tests",
    ROOT / "src/child_time_core.py",
)

CLI = load_module(
    "child_time_cli_access_tests",
    ROOT / "src/child-time",
)


class AccessWindowCoreTests(unittest.TestCase):
    def test_missing_config_preserves_v120_default(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "missing.conf"
            self.assertEqual(
                CORE.load_access_window(path),
                ("09:00", "17:00"),
            )

    def test_atomic_update_round_trip_and_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "access.conf"

            result = CORE.atomic_update_access_window(
                "07:30", "19:15", path
            )

            self.assertEqual(result, ("07:30", "19:15"))
            self.assertEqual(
                CORE.load_access_window(path),
                ("07:30", "19:15"),
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_or_reversed_window_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "access.conf"

            for start, end in (
                ("18:00", "08:00"),
                ("09:00", "09:00"),
                ("25:00", "17:00"),
            ):
                with self.subTest(start=start, end=end):
                    with self.assertRaises(CORE.ChildTimeError):
                        CORE.atomic_update_access_window(
                            start, end, path
                        )

    def test_setting_a_start_time_already_passed_today_still_succeeds(self):
        """An access window is a recurring daily policy, not a one-time
        deadline: configuring 08:00 at noon must not be rejected the way
        `child-time until` rejects an already-passed clock target."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "access.conf"

            with mock.patch.object(
                CORE, "local_now", return_value=datetime(2026, 9, 17, 12, 0)
            ):
                result = CORE.atomic_update_access_window("08:00", "18:00", path)

            self.assertEqual(result, ("08:00", "18:00"))
            self.assertEqual(CORE.load_access_window(path), ("08:00", "18:00"))

    def test_loading_a_config_whose_start_has_already_passed_today_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "access.conf"
            path.write_text("start=08:00\nend=18:00\n")

            with mock.patch.object(
                CORE, "local_now", return_value=datetime(2026, 9, 17, 23, 0)
            ):
                self.assertEqual(CORE.load_access_window(path), ("08:00", "18:00"))

    def test_concurrent_updates_never_corrupt_the_config(self):
        """atomic_update_access_window must not share a fixed temp file
        name across concurrent invocations: every writer's temp file must
        be unique, and the config must always parse as one complete,
        internally-consistent window afterward."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "access.conf"
            CORE.atomic_update_access_window("09:00", "17:00", path)

            barrier = threading.Barrier(2)
            errors = []

            def writer(start, end):
                try:
                    barrier.wait(timeout=5)
                    for _ in range(25):
                        CORE.atomic_update_access_window(start, end, path)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=writer, args=("07:00", "15:00")),
                threading.Thread(target=writer, args=("10:00", "20:00")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(errors, errors)
            self.assertTrue(all(not t.is_alive() for t in threads))

            # The file must always be exactly one of the two writers'
            # complete, valid windows -- never a half-written mix of both.
            start, end = CORE.load_access_window(path)
            self.assertIn((start, end), {("07:00", "15:00"), ("10:00", "20:00")})


class AccessWindowConfigExampleTests(unittest.TestCase):
    EXAMPLE = ROOT / "config/child-time-access.conf.example"

    def test_example_config_has_real_newlines_not_escaped_literals(self):
        raw = self.EXAMPLE.read_bytes()
        self.assertNotIn(b"\\n", raw)
        self.assertIn(b"\n", raw)

    def test_example_config_parses_to_the_documented_default(self):
        self.assertEqual(
            CORE.load_access_window(self.EXAMPLE),
            ("09:00", "17:00"),
        )


class AccessWindowCliTests(unittest.TestCase):
    def test_window_without_arguments_shows_current_policy(self):
        out = io.StringIO()

        with mock.patch.object(CLI.os, "geteuid", return_value=0), \
             mock.patch.object(
                 CLI.core,
                 "load_access_window",
                 return_value=("08:00", "18:00"),
             ):
            with redirect_stdout(out):
                rc = CLI.main(["window"])

        self.assertEqual(rc, 0)
        self.assertIn("08:00", out.getvalue())
        self.assertIn("18:00", out.getvalue())

    def test_window_with_start_and_end_updates_policy(self):
        out = io.StringIO()

        with mock.patch.object(CLI.os, "geteuid", return_value=0), \
             mock.patch.object(
                 CLI.core,
                 "atomic_update_access_window",
                 return_value=("08:00", "18:00"),
             ) as update:
            with redirect_stdout(out):
                rc = CLI.main(["window", "08:00", "18:00"])

        self.assertEqual(rc, 0)
        update.assert_called_once_with("08:00", "18:00")
        self.assertIn("Access window updated", out.getvalue())

    def test_window_requires_both_arguments(self):
        out = io.StringIO()

        with redirect_stdout(out):
            rc = CLI.main(["window", "08:00"])

        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
