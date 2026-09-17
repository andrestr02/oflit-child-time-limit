import unittest

from src import child_time_core as core


class ScheduledLoginEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.slots = [
            core.ScheduledSlot(
                username="azzahra",
                start_minute=9 * 60,
                end_minute=12 * 60,
                quota_seconds=3600,
                slot_id="09:00-12:00",
            ),
            core.ScheduledSlot(
                username="azzahra",
                start_minute=15 * 60,
                end_minute=17 * 60,
                quota_seconds=3600,
                slot_id="15:00-17:00",
            ),
        ]

    def evaluate(
        self,
        minute,
        daily_used=0,
        slot_usage=None,
        daily_limit=7200,
        slots=None,
    ):
        if slots is None:
            slots = self.slots
        if slot_usage is None:
            slot_usage = {
                "09:00-12:00": 0,
                "15:00-17:00": 0,
            }

        return core.evaluate_login_eligibility(
            daily_limit=daily_limit,
            daily_used=daily_used,
            slots=slots,
            slot_usage=slot_usage,
            minute_of_day=minute,
        )

    def test_unscheduled_user_preserves_legacy_allow(self):
        result = self.evaluate(
            minute=13 * 60,
            slots=[],
            slot_usage={},
            daily_used=100,
            daily_limit=7200,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "allowed")

    def test_unscheduled_user_daily_limit_exhausted_is_denied(self):
        result = self.evaluate(
            minute=13 * 60,
            slots=[],
            slot_usage={},
            daily_used=7200,
            daily_limit=7200,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "daily_exhausted")

    def test_scheduled_user_before_first_slot_is_denied(self):
        result = self.evaluate(minute=8 * 60 + 59)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "outside_slot")

    def test_scheduled_user_in_gap_is_denied(self):
        result = self.evaluate(minute=13 * 60)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "outside_slot")

    def test_scheduled_user_at_slot_start_is_allowed(self):
        result = self.evaluate(minute=9 * 60)
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "allowed")
        self.assertEqual(result.slot.slot_id, "09:00-12:00")

    def test_scheduled_user_at_slot_end_is_denied(self):
        result = self.evaluate(minute=12 * 60)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "outside_slot")

    def test_current_slot_with_remaining_quota_is_allowed(self):
        result = self.evaluate(
            minute=10 * 60,
            slot_usage={
                "09:00-12:00": 3599,
                "15:00-17:00": 0,
            },
        )
        self.assertTrue(result.allowed)

    def test_current_slot_exhausted_is_denied(self):
        result = self.evaluate(
            minute=10 * 60,
            slot_usage={
                "09:00-12:00": 3600,
                "15:00-17:00": 0,
            },
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "slot_exhausted")

    def test_morning_exhaustion_does_not_block_afternoon_slot(self):
        result = self.evaluate(
            minute=15 * 60,
            daily_used=3600,
            slot_usage={
                "09:00-12:00": 3600,
                "15:00-17:00": 0,
            },
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.slot.slot_id, "15:00-17:00")

    def test_daily_ceiling_exhausted_blocks_active_slot(self):
        result = self.evaluate(
            minute=15 * 60,
            daily_used=7200,
            slot_usage={
                "09:00-12:00": 3600,
                "15:00-17:00": 0,
            },
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "daily_exhausted")

    def test_reconciliation_blocks_when_slot_total_exposes_daily_exhaustion(self):
        result = self.evaluate(
            minute=15 * 60,
            daily_used=7100,
            slot_usage={
                "09:00-12:00": 3600,
                "15:00-17:00": 3600,
            },
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "daily_exhausted")


    def test_negative_slot_usage_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.evaluate(
                minute=10 * 60,
                slot_usage={
                    "09:00-12:00": -1,
                    "15:00-17:00": 0,
                },
            )

    def test_slot_usage_above_quota_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.evaluate(
                minute=10 * 60,
                slot_usage={
                    "09:00-12:00": 3601,
                    "15:00-17:00": 0,
                },
            )

    def test_invalid_daily_limit_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.evaluate(
                minute=10 * 60,
                daily_limit=0,
            )

    def test_invalid_minute_of_day_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.evaluate(minute=24 * 60)


if __name__ == "__main__":
    unittest.main()
