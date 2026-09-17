import tempfile
import unittest
from pathlib import Path

from src.child_time_core import (
    ChildTimeError,
    load_schedule_policy,
    resolve_active_slot,
    resolve_next_slot,
)


class ScheduledQuotaCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.schedule = self.root / "schedule.conf"
        self.limits = {
            "azzahra": 7200,
            "hudzaifah": 7200,
            "ibrohim": 1800,
        }

    def load(self, text, limits=None):
        self.schedule.write_text(text)
        return load_schedule_policy(
            path=self.schedule,
            limits=self.limits if limits is None else limits,
        )

    def test_missing_schedule_file_means_no_scheduled_users(self):
        policy = load_schedule_policy(
            path=self.root / "missing.conf",
            limits=self.limits,
        )
        self.assertEqual(policy, {})

    def test_one_user_two_slots(self):
        policy = self.load(
            """
[azzahra]
09:00-12:00=3600
15:00-17:00=3600
"""
        )
        slots = policy["azzahra"]
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0].slot_id, "09:00-12:00")
        self.assertEqual(slots[0].quota_seconds, 3600)
        self.assertEqual(slots[1].slot_id, "15:00-17:00")

    def test_multiple_users_are_independent(self):
        policy = self.load(
            """
[azzahra]
09:00-12:00=3600
15:00-17:00=3600

[hudzaifah]
10:00-12:00=1800
15:30-17:00=1800
"""
        )
        self.assertEqual(len(policy["azzahra"]), 2)
        self.assertEqual(len(policy["hudzaifah"]), 2)
        self.assertEqual(policy["hudzaifah"][0].slot_id, "10:00-12:00")

    def test_unconfigured_user_is_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[unknown]
09:00-10:00=600
"""
            )

    def test_invalid_clock_is_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[azzahra]
25:00-26:00=600
"""
            )

    def test_start_equal_end_is_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[azzahra]
09:00-09:00=600
"""
            )

    def test_overnight_slot_is_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[azzahra]
22:00-01:00=600
"""
            )

    def test_zero_quota_is_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[azzahra]
09:00-10:00=0
"""
            )

    def test_negative_quota_is_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[azzahra]
09:00-10:00=-1
"""
            )

    def test_quota_larger_than_slot_is_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[azzahra]
09:00-10:00=3601
"""
            )

    def test_overlapping_slots_are_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[azzahra]
09:00-12:00=1800
11:00-13:00=1800
"""
            )

    def test_touching_slots_are_accepted(self):
        policy = self.load(
            """
[azzahra]
09:00-10:00=1800
10:00-11:00=1800
"""
        )
        self.assertEqual(
            [slot.slot_id for slot in policy["azzahra"]],
            ["09:00-10:00", "10:00-11:00"],
        )

    def test_input_reorder_keeps_stable_ids_and_order(self):
        policy = self.load(
            """
[azzahra]
15:00-17:00=3600
09:00-12:00=3600
"""
        )
        self.assertEqual(
            [slot.slot_id for slot in policy["azzahra"]],
            ["09:00-12:00", "15:00-17:00"],
        )

    def test_start_is_inclusive(self):
        slots = self.load(
            """
[azzahra]
09:00-12:00=3600
"""
        )["azzahra"]
        self.assertEqual(resolve_active_slot(slots, 9 * 60).slot_id, "09:00-12:00")

    def test_end_is_exclusive(self):
        slots = self.load(
            """
[azzahra]
09:00-12:00=3600
"""
        )["azzahra"]
        self.assertIsNone(resolve_active_slot(slots, 12 * 60))

    def test_gap_has_no_active_slot(self):
        slots = self.load(
            """
[azzahra]
09:00-12:00=3600
15:00-17:00=3600
"""
        )["azzahra"]
        self.assertIsNone(resolve_active_slot(slots, 13 * 60))

    def test_next_slot_from_gap(self):
        slots = self.load(
            """
[azzahra]
09:00-12:00=3600
15:00-17:00=3600
"""
        )["azzahra"]
        self.assertEqual(
            resolve_next_slot(slots, 13 * 60).slot_id,
            "15:00-17:00",
        )

    def test_no_next_slot_after_last_start(self):
        slots = self.load(
            """
[azzahra]
09:00-12:00=3600
15:00-17:00=3600
"""
        )["azzahra"]
        self.assertIsNone(resolve_next_slot(slots, 16 * 60))

    def test_aggregate_quota_over_daily_ceiling_is_rejected(self):
        with self.assertRaises(ChildTimeError):
            self.load(
                """
[azzahra]
09:00-12:00=3600
15:00-17:00=3600
""",
                limits={"azzahra": 7199},
            )

    def test_aggregate_quota_equal_daily_ceiling_is_accepted(self):
        policy = self.load(
            """
[azzahra]
09:00-12:00=3600
15:00-17:00=3600
"""
        )
        self.assertEqual(
            sum(slot.quota_seconds for slot in policy["azzahra"]),
            7200,
        )

    def test_user_without_section_remains_unscheduled(self):
        policy = self.load(
            """
[azzahra]
09:00-12:00=3600
"""
        )
        self.assertNotIn("hudzaifah", policy)


if __name__ == "__main__":
    unittest.main()
