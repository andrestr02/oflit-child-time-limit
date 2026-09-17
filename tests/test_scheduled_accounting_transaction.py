import unittest

from src import child_time_core as core


class ScheduledAccountingTransactionTests(unittest.TestCase):
    def setUp(self):
        self.slot = core.ScheduledSlot(
            username="azzahra",
            start_minute=9 * 60,
            end_minute=12 * 60,
            quota_seconds=3600,
            slot_id="09:00-12:00",
        )

    def apply(self, *, daily_used=0, slot_used=0, charge=1.0, daily_limit=7200):
        return core.apply_scheduled_charge(
            daily_limit=daily_limit,
            daily_used=daily_used,
            slot=self.slot,
            slot_used=slot_used,
            charge_seconds=charge,
        )

    def test_normal_charge_updates_both_views(self):
        result = self.apply(daily_used=100, slot_used=100, charge=1)
        self.assertEqual(result.daily_used, 101)
        self.assertEqual(result.slot_used, 101)
        self.assertFalse(result.slot_exhausted)
        self.assertFalse(result.daily_exhausted)

    def test_charge_is_clipped_at_slot_quota(self):
        result = self.apply(slot_used=3599.5, charge=1)
        self.assertEqual(result.slot_used, 3600)
        self.assertAlmostEqual(result.charged_seconds, 0.5)
        self.assertTrue(result.slot_exhausted)

    def test_charge_is_clipped_at_daily_ceiling(self):
        result = self.apply(
            daily_used=7199.5,
            slot_used=100,
            charge=1,
        )
        self.assertEqual(result.daily_used, 7200)
        self.assertAlmostEqual(result.charged_seconds, 0.5)
        self.assertTrue(result.daily_exhausted)

    def test_already_exhausted_slot_adds_no_usage(self):
        result = self.apply(slot_used=3600, charge=1)
        self.assertEqual(result.charged_seconds, 0)
        self.assertEqual(result.slot_used, 3600)
        self.assertTrue(result.slot_exhausted)

    def test_already_exhausted_daily_ceiling_adds_no_usage(self):
        result = self.apply(daily_used=7200, slot_used=100, charge=1)
        self.assertEqual(result.charged_seconds, 0)
        self.assertEqual(result.daily_used, 7200)
        self.assertTrue(result.daily_exhausted)

    def test_negative_charge_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.apply(charge=-1)

    def test_negative_usage_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.apply(daily_used=-1)

        with self.assertRaises(core.ChildTimeError):
            self.apply(slot_used=-1)

    def test_slot_usage_above_quota_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.apply(slot_used=3601)


if __name__ == "__main__":
    unittest.main()
