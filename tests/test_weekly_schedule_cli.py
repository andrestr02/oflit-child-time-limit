"""Administrator CLI surface for weekly schedules and weekday limits.

All tests run against a policy tree in a TemporaryDirectory by patching
the core's path constants, so the production files are never read or
written. `validate_username` is patched where a mutation would otherwise
require a matching local account, keeping the suite machine-independent.
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "child-time"
CORE = ROOT / "src" / "child_time_core.py"

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "child_time_cli_weekly_test",
        str(CLI),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CliHarness:
    """Run CLI commands against an isolated policy tree."""

    def __init__(self, directory, limits="hudzaifah=3600\n"):
        self.dir = Path(directory)
        self.config = self.dir / "child-time-limit.conf"
        self.schedule = self.dir / "child-time-schedule.conf"
        self.daily = self.dir / "child-time-daily.conf"
        self.access = self.dir / "child-time-access.conf"
        self.state = self.dir / "state"
        self.schedule_state = self.state / "schedules"
        self.state.mkdir(parents=True, exist_ok=True)
        self.schedule_state.mkdir(parents=True, exist_ok=True)
        self.config.write_text(limits)
        self.access.write_text("start=06:00\nend=22:00\n")
        self.cli = load_cli()

    @contextmanager
    def active(self):
        core = self.cli.core
        with mock.patch.object(core, "CONFIG", self.config), \
             mock.patch.object(core, "SCHEDULE_CONFIG", self.schedule), \
             mock.patch.object(core, "DAILY_CONFIG", self.daily), \
             mock.patch.object(core, "ACCESS_CONFIG", self.access), \
             mock.patch.object(core, "STATE_DIR", self.state), \
             mock.patch.object(core, "SCHEDULE_STATE_DIR", self.schedule_state), \
             mock.patch.object(core, "validate_username", lambda *a, **k: None), \
             mock.patch.object(
                 self.cli, "validate_username", lambda *a, **k: None
             ), \
             mock.patch.object(self.cli.os, "geteuid", return_value=0):
            yield

    def run(self, *argv):
        """Run `child-time ...`; returns (exit_code, stdout)."""
        out = StringIO()
        with self.active(), redirect_stdout(out):
            code = self.cli.main(list(argv))
        return code, out.getvalue()

    def snapshot(self):
        return (
            self.schedule.read_text() if self.schedule.exists() else None,
            self.daily.read_text() if self.daily.exists() else None,
            self.config.read_text(),
        )


class ScheduleListTests(unittest.TestCase):
    def test_list_reports_no_schedules_for_a_plain_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            code, out = harness.run("schedule", "list", "hudzaifah")

        self.assertEqual(code, 0)
        self.assertIn("no schedules", out)

    def test_list_shows_identifier_days_window_and_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            code, out = harness.run("schedule", "list", "hudzaifah")

        self.assertEqual(code, 0)
        self.assertIn("SCHEDULE_ID", out)
        self.assertIn("15:00-17:00", out)
        self.assertIn("daily", out)
        self.assertIn("1h", out)


class ScheduleAddTests(unittest.TestCase):
    def test_add_without_days_creates_an_all_week_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            code, out = harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )

            self.assertEqual(code, 0)
            self.assertIn("added", out)
            self.assertIn("15:00-17:00", harness.schedule.read_text())

    def test_add_with_weekday_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            code, out = harness.run(
                "schedule", "add", "hudzaifah",
                "--days", "mon-fri",
                "--start", "08:00", "--end", "09:00", "--quota", "30m",
            )

            self.assertEqual(code, 0)
            self.assertIn("mon-fri/08:00-09:00", out)
            self.assertIn("mon-fri/08:00-09:00=1800", harness.schedule.read_text())

    def test_add_accepts_all_seven_weekdays_individually(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            for index, name in enumerate(self.weekday_names(harness)):
                hour = 8 + index
                code, _ = harness.run(
                    "schedule", "add", "hudzaifah",
                    "--days", name,
                    "--start", f"{hour:02d}:00", "--end", f"{hour:02d}:30",
                    "--quota", "30m",
                )
                self.assertEqual(code, 0, name)

            code, out = harness.run("schedule", "list", "hudzaifah")
            for name in self.weekday_names(harness):
                self.assertIn(f"{name}/", out)

    @staticmethod
    def weekday_names(harness):
        return list(harness.cli.core.WEEKDAY_NAMES)

    def test_add_rejects_invalid_input_without_touching_config(self):
        cases = [
            ("bad weekday", ["--days", "funday", "--start", "09:00",
                             "--end", "10:00", "--quota", "30m"]),
            ("inverted window", ["--start", "17:00", "--end", "15:00",
                                 "--quota", "30m"]),
            ("zero quota", ["--start", "09:00", "--end", "10:00",
                            "--quota", "0s"]),
            ("quota over window", ["--start", "09:00", "--end", "09:30",
                                   "--quota", "2h"]),
            ("quota over daily", ["--days", "mon", "--start", "09:00",
                                  "--end", "12:00", "--quota", "2h"]),
        ]

        for label, extra in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmp:
                    harness = CliHarness(tmp)
                    harness.run(
                        "schedule", "add", "hudzaifah",
                        "--start", "15:00", "--end", "17:00", "--quota", "1h",
                    )
                    before = harness.snapshot()

                    code, _ = harness.run(
                        "schedule", "add", "hudzaifah", *extra
                    )

                    self.assertEqual(code, 2, label)
                    self.assertEqual(harness.snapshot(), before, label)

    def test_add_rejects_an_overlapping_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            harness.run(
                "schedule", "add", "hudzaifah", "--days", "fri",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            before = harness.snapshot()

            code, _ = harness.run(
                "schedule", "add", "hudzaifah", "--days", "mon-fri",
                "--start", "16:00", "--end", "18:00", "--quota", "1h",
            )

            self.assertEqual(code, 2)
            self.assertEqual(harness.snapshot(), before)

    def test_add_rejects_a_duplicate_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            before = harness.snapshot()

            code, _ = harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )

            self.assertEqual(code, 2)
            self.assertEqual(harness.snapshot(), before)

    def test_add_rejects_an_unconfigured_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            before = harness.snapshot()

            code, _ = harness.run(
                "schedule", "add", "someone_else",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )

            self.assertEqual(code, 2)
            self.assertEqual(harness.snapshot(), before)


class ScheduleEditTests(unittest.TestCase):
    def _with_afternoon(self, tmp, limits="hudzaifah=7200\n"):
        harness = CliHarness(tmp, limits)
        harness.run(
            "schedule", "add", "hudzaifah",
            "--start", "15:00", "--end", "17:00", "--quota", "1h",
        )
        return harness

    def test_edit_quota_keeps_the_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._with_afternoon(tmp)
            code, out = harness.run(
                "schedule", "edit", "hudzaifah", "15:00-17:00", "--quota", "90m",
            )

            self.assertEqual(code, 0)
            self.assertIn("edited", out)
            self.assertIn("15:00-17:00=5400", harness.schedule.read_text())

    def test_edit_days_changes_the_identifier_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._with_afternoon(tmp)
            code, out = harness.run(
                "schedule", "edit", "hudzaifah", "15:00-17:00", "--days", "mon-fri",
            )

            self.assertEqual(code, 0)
            self.assertIn("15:00-17:00 -> mon-fri/15:00-17:00", out)
            self.assertIn("restarts from zero", out)
            self.assertIn("mon-fri/15:00-17:00=3600", harness.schedule.read_text())

    def test_edit_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._with_afternoon(tmp)
            code, _ = harness.run(
                "schedule", "edit", "hudzaifah", "15:00-17:00",
                "--start", "14:00", "--end", "16:00",
            )

            self.assertEqual(code, 0)
            self.assertIn("14:00-16:00=3600", harness.schedule.read_text())

    def test_edit_rejects_unknown_identifier_and_lists_known_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._with_afternoon(tmp)
            before = harness.snapshot()

            out = StringIO()
            with harness.active(), redirect_stdout(out):
                with self.assertRaises(harness.cli.ChildTimeError) as ctx:
                    harness.cli.core.edit_schedule(
                        "hudzaifah", "nope", quota_seconds=60
                    )

            self.assertIn("15:00-17:00", str(ctx.exception))
            self.assertEqual(harness.snapshot(), before)

    def test_edit_requires_at_least_one_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._with_afternoon(tmp)
            before = harness.snapshot()

            code, _ = harness.run(
                "schedule", "edit", "hudzaifah", "15:00-17:00",
            )

            self.assertEqual(code, 2)
            self.assertEqual(harness.snapshot(), before)

    def test_edit_that_would_break_the_daily_ceiling_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._with_afternoon(tmp, "hudzaifah=3600\n")
            before = harness.snapshot()

            code, _ = harness.run(
                "schedule", "edit", "hudzaifah", "15:00-17:00", "--quota", "2h",
            )

            self.assertEqual(code, 2)
            self.assertEqual(harness.snapshot(), before)


class ScheduleRemoveTests(unittest.TestCase):
    def test_remove_deletes_only_the_named_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            harness.run(
                "schedule", "add", "hudzaifah", "--days", "fri",
                "--start", "09:00", "--end", "12:00", "--quota", "1h",
            )

            code, out = harness.run(
                "schedule", "remove", "hudzaifah", "fri/09:00-12:00",
            )

            self.assertEqual(code, 0)
            self.assertIn("removed", out)
            text = harness.schedule.read_text()
            self.assertNotIn("fri/09:00-12:00", text)
            self.assertIn("15:00-17:00=3600", text)

    def test_remove_rejects_unknown_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            before = harness.snapshot()

            code, _ = harness.run(
                "schedule", "remove", "hudzaifah", "fri/09:00-12:00",
            )

            self.assertEqual(code, 2)
            self.assertEqual(harness.snapshot(), before)


class WeekdayLimitCliTests(unittest.TestCase):
    def test_set_with_day_writes_only_that_weekday(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            code, out = harness.run("set", "hudzaifah", "2h", "--day", "fri")

            self.assertEqual(code, 0)
            self.assertIn("fri only", out)
            self.assertIn("fri=7200", harness.daily.read_text())
            # The base limit file is untouched.
            self.assertEqual(harness.config.read_text(), "hudzaifah=3600\n")

    def test_set_without_day_still_changes_the_base_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            code, _ = harness.run("set", "hudzaifah", "2h")

            self.assertEqual(code, 0)
            self.assertIn("hudzaifah=7200", harness.config.read_text())
            self.assertFalse(harness.daily.exists())

    def test_set_day_accepts_long_weekday_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            code, _ = harness.run("set", "hudzaifah", "2h", "--day", "Friday")

            self.assertEqual(code, 0)
            self.assertIn("fri=7200", harness.daily.read_text())

    def test_set_day_rejects_an_invalid_weekday(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            before = harness.snapshot()

            code, _ = harness.run("set", "hudzaifah", "2h", "--day", "funday")

            self.assertEqual(code, 2)
            self.assertEqual(harness.snapshot(), before)

    def test_set_day_below_that_days_scheduled_quota_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            harness.run(
                "schedule", "add", "hudzaifah", "--days", "fri",
                "--start", "09:00", "--end", "12:00", "--quota", "2h",
            )
            before = harness.snapshot()

            code, _ = harness.run("set", "hudzaifah", "1h", "--day", "fri")

            self.assertEqual(code, 2)
            self.assertEqual(harness.snapshot(), before)

    def test_base_limit_change_ignores_overridden_weekdays(self):
        """Friday is overridden, so lowering the base must not trip on it."""
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            harness.run("set", "hudzaifah", "2h", "--day", "fri")
            harness.run(
                "schedule", "add", "hudzaifah", "--days", "fri",
                "--start", "09:00", "--end", "11:00", "--quota", "2h",
            )

            code, _ = harness.run("set", "hudzaifah", "1h")

            self.assertEqual(code, 0)
            self.assertIn("hudzaifah=3600", harness.config.read_text())


class LimitsInspectionTests(unittest.TestCase):
    def test_limits_prints_the_full_acceptance_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            harness.run("set", "hudzaifah", "2h", "--day", "fri")
            harness.run(
                "schedule", "add", "hudzaifah", "--days", "fri",
                "--start", "09:00", "--end", "12:00", "--quota", "1h",
            )

            code, out = harness.run("limits", "hudzaifah")

        self.assertEqual(code, 0)

        rows = {}
        for line in out.splitlines():
            fields = line.split()
            if fields and fields[0].rstrip("*") in (
                "mon", "tue", "wed", "thu", "fri", "sat", "sun"
            ):
                rows[fields[0].rstrip("*")] = line

        for day in ("mon", "tue", "wed", "thu", "sat", "sun"):
            self.assertIn("01:00:00", rows[day], day)
            self.assertIn("15:00-17:00", rows[day], day)
            self.assertNotIn("09:00-12:00", rows[day], day)

        self.assertIn("02:00:00", rows["fri"])
        self.assertIn("override", rows["fri"])
        self.assertIn("fri/09:00-12:00", rows["fri"])
        self.assertIn("15:00-17:00", rows["fri"])

    def test_limits_reports_a_user_with_no_restrictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "ibrohim=5400\n")
            code, out = harness.run("limits", "ibrohim")

        self.assertEqual(code, 0)
        self.assertIn("no scheduled restriction", out)
        self.assertEqual(out.count("01:30:00"), 7)

    def test_limits_rejects_an_unconfigured_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            code, _ = harness.run("limits", "someone_else")

        self.assertEqual(code, 2)


class ValidateCommandTests(unittest.TestCase):
    def test_validate_accepts_a_good_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            code, out = harness.run("validate")

        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_validate_fails_on_a_hand_broken_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.schedule.write_text("[hudzaifah]\nfunday/09:00-10:00=600\n")
            code, _ = harness.run("validate")

        self.assertEqual(code, 2)

    def test_validate_fails_when_a_weekday_quota_exceeds_its_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.schedule.write_text(
                "[hudzaifah]\n15:00-17:00=3600\nfri/09:00-12:00=3600\n"
            )
            code, _ = harness.run("validate")

        self.assertEqual(code, 2)

    def test_validate_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            before = harness.snapshot()
            harness.run("validate")
            self.assertEqual(harness.snapshot(), before)


class LegacyCliCompatibilityTests(unittest.TestCase):
    def test_existing_commands_still_exist(self):
        cli = load_cli()
        parser = cli.build_parser()
        actions = [
            action
            for action in parser._subparsers._group_actions[0].choices
        ]
        for command in ("status", "set", "add", "subtract", "window", "until"):
            self.assertIn(command, actions)

    def test_legacy_set_add_subtract_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "ibrohim=5400\n")

            self.assertEqual(harness.run("set", "ibrohim", "2h")[0], 0)
            self.assertIn("ibrohim=7200", harness.config.read_text())

            self.assertEqual(harness.run("add", "ibrohim", "30m")[0], 0)
            self.assertIn("ibrohim=9000", harness.config.read_text())

            self.assertEqual(harness.run("subtract", "ibrohim", "30m")[0], 0)
            self.assertIn("ibrohim=7200", harness.config.read_text())

    def test_status_still_runs_for_a_user_without_schedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "ibrohim=5400\n")
            code, out = harness.run("status", "ibrohim")

        self.assertEqual(code, 0)
        self.assertIn("ibrohim", out)
        self.assertIn("AVAILABLE", out)


class ConcurrentMutationTests(unittest.TestCase):
    """Separate processes really contend for the policy lock here."""

    SCRIPT = textwrap.dedent(
        """
        import importlib.machinery, importlib.util, sys
        from pathlib import Path

        core_path, config, start, end = sys.argv[1:5]
        loader = importlib.machinery.SourceFileLoader("c", core_path)
        core = importlib.util.module_from_spec(
            importlib.util.spec_from_loader("c", loader)
        )
        loader.exec_module(core)
        core.validate_username = lambda *a, **k: None

        try:
            core.add_schedule(
                "hudzaifah", None, start, end, 600,
                config_path=Path(config),
            )
        except core.ChildTimeError as exc:
            print(f"rejected: {exc}", file=sys.stderr)
            raise SystemExit(3)
        """
    )

    def test_concurrent_adds_all_land_and_leave_a_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            script = Path(tmp) / "add.py"
            script.write_text(self.SCRIPT)

            windows = [
                ("08:00", "08:30"),
                ("09:00", "09:30"),
                ("10:00", "10:30"),
                ("11:00", "11:30"),
                ("12:00", "12:30"),
                ("13:00", "13:30"),
            ]

            processes = [
                subprocess.Popen(
                    [sys.executable, str(script), str(CORE),
                     str(harness.config), start, end],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for start, end in windows
            ]
            results = [process.communicate() for process in processes]
            codes = [process.returncode for process in processes]

            self.assertEqual(
                codes, [0] * len(windows),
                f"a concurrent add failed: {results}",
            )

            # The file must be parseable and hold every schedule exactly once.
            _, _, schedules = harness.cli.core.load_effective_policy(
                config_path=harness.config,
                schedule_path=harness.schedule,
                daily_path=harness.daily,
            )
            slot_ids = sorted(
                slot.slot_id for slot in schedules["hudzaifah"]
            )
            self.assertEqual(
                slot_ids,
                sorted(f"{start}-{end}" for start, end in windows),
            )

    def test_concurrent_adds_never_leave_a_partial_file(self):
        """No interleaving may produce a file that fails to parse."""
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            script = Path(tmp) / "add.py"
            script.write_text(self.SCRIPT)

            processes = [
                subprocess.Popen(
                    [sys.executable, str(script), str(CORE),
                     str(harness.config), f"{8 + index:02d}:00",
                     f"{8 + index:02d}:30"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for index in range(8)
            ]
            for process in processes:
                process.wait()

            text = harness.schedule.read_text()
            self.assertNotIn("\x00", text)
            # Parses cleanly, and no leftover temporary files remain.
            harness.cli.core.load_effective_policy(
                config_path=harness.config,
                schedule_path=harness.schedule,
                daily_path=harness.daily,
            )
            leftovers = [
                path.name
                for path in Path(tmp).iterdir()
                if path.name.startswith("child-time-schedule.conf.")
                and not path.name.endswith(".lock")
            ]
            self.assertEqual(leftovers, [])


class AtomicWriteTests(unittest.TestCase):
    def test_failed_write_leaves_the_previous_policy_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp, "hudzaifah=7200\n")
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            before = harness.schedule.read_text()

            core = harness.cli.core
            with mock.patch.object(
                core.os, "replace", side_effect=OSError("disk full")
            ):
                with mock.patch.object(core, "validate_username", lambda *a, **k: None):
                    with self.assertRaises(core.ChildTimeError):
                        core.add_schedule(
                            "hudzaifah", "mon", "08:00", "09:00", 600,
                            config_path=harness.config,
                        )

            self.assertEqual(harness.schedule.read_text(), before)

    def test_schedule_file_keeps_restrictive_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = CliHarness(tmp)
            harness.run(
                "schedule", "add", "hudzaifah",
                "--start", "15:00", "--end", "17:00", "--quota", "1h",
            )
            self.assertEqual(harness.schedule.stat().st_mode & 0o777, 0o600)

            harness.run("set", "hudzaifah", "2h", "--day", "fri")
            self.assertEqual(harness.daily.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
