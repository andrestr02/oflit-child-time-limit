import unittest
from unittest import mock
from pathlib import Path
import tempfile

from src import child_time_core as core


class ScheduledStatusTests(unittest.TestCase):
    def _slots(self):
        return [
            core.ScheduledSlot(
                "azzahra", 540, 720, 3600, "09:00-12:00"
            ),
            core.ScheduledSlot(
                "azzahra", 900, 1020, 3600, "15:00-17:00"
            ),
        ]

    def test_scheduled_status_active_slot(self):
        slots = self._slots()

        detail = core.scheduled_status_detail(
            slots,
            {
                "09:00-12:00": 1200,
                "15:00-17:00": 0,
            },
            minute_of_day=600,
        )

        self.assertEqual(detail.mode, "ACTIVE")
        self.assertEqual(detail.slot_id, "09:00-12:00")
        self.assertEqual(detail.used, 1200)
        self.assertEqual(detail.remaining, 2400)

    def test_scheduled_status_gap_reports_next_slot(self):
        slots = self._slots()

        detail = core.scheduled_status_detail(
            slots,
            {
                "09:00-12:00": 3600,
                "15:00-17:00": 0,
            },
            minute_of_day=780,
        )

        self.assertEqual(detail.mode, "NEXT")
        self.assertEqual(detail.slot_id, "15:00-17:00")
        self.assertEqual(detail.used, 0)
        self.assertEqual(detail.remaining, 3600)

    def test_scheduled_status_after_final_slot(self):
        slots = self._slots()

        detail = core.scheduled_status_detail(
            slots,
            {
                "09:00-12:00": 3600,
                "15:00-17:00": 3600,
            },
            minute_of_day=1080,
        )

        self.assertEqual(detail.mode, "DONE")
        self.assertIsNone(detail.slot_id)
        self.assertEqual(detail.used, 0)
        self.assertEqual(detail.remaining, 0)

    def test_scheduled_status_slot_exhausted_while_window_active(self):
        slots = self._slots()

        detail = core.scheduled_status_detail(
            slots,
            {
                "09:00-12:00": 3600,
                "15:00-17:00": 0,
            },
            minute_of_day=600,
        )

        self.assertEqual(detail.mode, "EXHAUSTED")
        self.assertEqual(detail.slot_id, "09:00-12:00")
        self.assertEqual(detail.remaining, 0)

    def test_status_rows_preserves_legacy_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "limits.conf"
            config.write_text("hudzaifah=7200\n")

            with mock.patch.object(
                core,
                "read_used",
                return_value=1800,
            ):
                rows = core.status_rows(
                    selected="hudzaifah",
                    config_path=config,
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].username, "hudzaifah")
        self.assertEqual(rows[0].used, 1800)
        self.assertEqual(rows[0].remaining, 5400)
        self.assertEqual(rows[0].status, "AVAILABLE")

    def test_status_rows_scheduled_daily_usage_reconciles_slot_state(self):
        slots = self._slots()

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "limits.conf"
            config.write_text("azzahra=7200\n")

            with mock.patch.object(
                core,
                "load_schedule_policy",
                return_value={"azzahra": slots},
            ), mock.patch.object(
                core,
                "read_used",
                return_value=1000,
            ), mock.patch.object(
                core,
                "read_schedule_used",
                return_value={
                    "09:00-12:00": 3600,
                    "15:00-17:00": 1200,
                },
            ):
                rows = core.status_rows(
                    selected="azzahra",
                    config_path=config,
                )

        self.assertEqual(rows[0].used, 4800)
        self.assertEqual(rows[0].remaining, 2400)


class ScheduledStatusContractTests(unittest.TestCase):
    def test_cli_print_status_mentions_slot_information(self):
        source = Path("src/child-time").read_text()
        self.assertIn("scheduled_status", source)

    def test_legacy_status_command_remains_present(self):
        source = Path("src/child-time").read_text()
        self.assertIn("def print_status(", source)
        self.assertIn("core.status_rows", source)


if __name__ == "__main__":
    unittest.main()
