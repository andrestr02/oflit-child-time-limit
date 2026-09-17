import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.child_time_core import (
    ChildTimeError,
    ScheduledSlot,
    read_schedule_used,
    reconcile_daily_used,
    scheduled_total_used,
    write_schedule_used,
)


class ScheduledQuotaStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.state_dir = Path(self.tmp.name) / "schedules"
        self.state_dir.mkdir()

        self.slots = [
            ScheduledSlot(
                username="azzahra",
                start_minute=9 * 60,
                end_minute=12 * 60,
                quota_seconds=3600,
                slot_id="09:00-12:00",
            ),
            ScheduledSlot(
                username="azzahra",
                start_minute=15 * 60,
                end_minute=17 * 60,
                quota_seconds=3600,
                slot_id="15:00-17:00",
            ),
        ]

    def write_state(self, text):
        path = self.state_dir / "azzahra.state"
        path.write_text(text)
        return path

    def read(self, day="2026-09-17"):
        return read_schedule_used(
            "azzahra",
            self.slots,
            day=day,
            state_dir=self.state_dir,
        )

    def test_missing_state_returns_zero_for_every_current_slot(self):
        self.assertEqual(
            self.read(),
            {
                "09:00-12:00": 0,
                "15:00-17:00": 0,
            },
        )

    def test_current_day_state_is_loaded(self):
        self.write_state(
            """2026-09-17
09:00-12:00=1250
15:00-17:00=600
"""
        )
        self.assertEqual(
            self.read(),
            {
                "09:00-12:00": 1250,
                "15:00-17:00": 600,
            },
        )

    def test_stale_day_returns_zero(self):
        self.write_state(
            """2026-09-16
09:00-12:00=1250
15:00-17:00=600
"""
        )
        self.assertEqual(
            self.read(),
            {
                "09:00-12:00": 0,
                "15:00-17:00": 0,
            },
        )

    def test_missing_slot_entry_defaults_to_zero(self):
        self.write_state(
            """2026-09-17
09:00-12:00=1250
"""
        )
        self.assertEqual(
            self.read(),
            {
                "09:00-12:00": 1250,
                "15:00-17:00": 0,
            },
        )

    def test_unknown_old_slot_is_ignored(self):
        self.write_state(
            """2026-09-17
08:00-09:00=900
09:00-12:00=1250
"""
        )
        self.assertEqual(
            self.read(),
            {
                "09:00-12:00": 1250,
                "15:00-17:00": 0,
            },
        )

    def test_malformed_state_line_is_rejected(self):
        self.write_state(
            """2026-09-17
09:00-12:00
"""
        )
        with self.assertRaises(ChildTimeError):
            self.read()

    def test_non_integer_usage_is_rejected(self):
        self.write_state(
            """2026-09-17
09:00-12:00=abc
"""
        )
        with self.assertRaises(ChildTimeError):
            self.read()

    def test_negative_usage_is_rejected(self):
        self.write_state(
            """2026-09-17
09:00-12:00=-1
"""
        )
        with self.assertRaises(ChildTimeError):
            self.read()

    def test_duplicate_slot_entry_is_rejected(self):
        self.write_state(
            """2026-09-17
09:00-12:00=100
09:00-12:00=200
"""
        )
        with self.assertRaises(ChildTimeError):
            self.read()

    def test_writer_round_trip(self):
        written = write_schedule_used(
            "azzahra",
            self.slots,
            {
                "09:00-12:00": 1250,
                "15:00-17:00": 600,
            },
            day="2026-09-17",
            state_dir=self.state_dir,
        )

        self.assertEqual(
            written,
            {
                "09:00-12:00": 1250,
                "15:00-17:00": 600,
            },
        )
        self.assertEqual(self.read(), written)

    def test_writer_creates_state_mode_0600(self):
        write_schedule_used(
            "azzahra",
            self.slots,
            {},
            day="2026-09-17",
            state_dir=self.state_dir,
        )

        mode = (self.state_dir / "azzahra.state").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_writer_missing_slot_defaults_to_zero(self):
        write_schedule_used(
            "azzahra",
            self.slots,
            {"09:00-12:00": 1250},
            day="2026-09-17",
            state_dir=self.state_dir,
        )

        self.assertEqual(
            self.read(),
            {
                "09:00-12:00": 1250,
                "15:00-17:00": 0,
            },
        )

    def test_writer_rejects_unknown_slot(self):
        with self.assertRaises(ChildTimeError):
            write_schedule_used(
                "azzahra",
                self.slots,
                {"08:00-09:00": 100},
                day="2026-09-17",
                state_dir=self.state_dir,
            )

    def test_writer_rejects_negative_usage(self):
        with self.assertRaises(ChildTimeError):
            write_schedule_used(
                "azzahra",
                self.slots,
                {"09:00-12:00": -1},
                day="2026-09-17",
                state_dir=self.state_dir,
            )

    def test_writer_rejects_usage_above_slot_quota(self):
        with self.assertRaises(ChildTimeError):
            write_schedule_used(
                "azzahra",
                self.slots,
                {"09:00-12:00": 3601},
                day="2026-09-17",
                state_dir=self.state_dir,
            )

    def test_writer_validation_failure_preserves_existing_state(self):
        original = """2026-09-17
09:00-12:00=1200
15:00-17:00=500
"""
        path = self.write_state(original)

        with self.assertRaises(ChildTimeError):
            write_schedule_used(
                "azzahra",
                self.slots,
                {"09:00-12:00": 9999},
                day="2026-09-17",
                state_dir=self.state_dir,
            )

        self.assertEqual(path.read_text(), original)

    def test_writer_replace_failure_preserves_existing_state(self):
        original = """2026-09-17
09:00-12:00=1200
15:00-17:00=500
"""
        path = self.write_state(original)

        with mock.patch(
            "src.child_time_core.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaises(ChildTimeError):
                write_schedule_used(
                    "azzahra",
                    self.slots,
                    {
                        "09:00-12:00": 1300,
                        "15:00-17:00": 500,
                    },
                    day="2026-09-17",
                    state_dir=self.state_dir,
                )

        self.assertEqual(path.read_text(), original)

        leftovers = [
            item
            for item in self.state_dir.iterdir()
            if item.name != "azzahra.state"
        ]
        self.assertEqual(leftovers, [])

    def test_total_used_sums_current_slots(self):
        usage = {
            "09:00-12:00": 1250,
            "15:00-17:00": 600,
        }
        self.assertEqual(scheduled_total_used(usage), 1850)

    def test_reconcile_prefers_slot_total_when_daily_write_lagged(self):
        usage = {
            "09:00-12:00": 1250,
            "15:00-17:00": 600,
        }
        self.assertEqual(reconcile_daily_used(1800, usage), 1850)

    def test_reconcile_preserves_daily_when_slot_write_lagged(self):
        usage = {
            "09:00-12:00": 1200,
            "15:00-17:00": 600,
        }
        self.assertEqual(reconcile_daily_used(1850, usage), 1850)

    def test_reconcile_equal_views_is_stable(self):
        usage = {
            "09:00-12:00": 1250,
            "15:00-17:00": 600,
        }
        self.assertEqual(reconcile_daily_used(1850, usage), 1850)


if __name__ == "__main__":
    unittest.main()
