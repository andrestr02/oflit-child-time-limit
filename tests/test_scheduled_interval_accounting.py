import unittest

from src import child_time_core as core


class ScheduledIntervalAccountingTests(unittest.TestCase):
    def setUp(self):
        self.slot = core.ScheduledSlot(
            username="azzahra",
            start_minute=9 * 60,
            end_minute=12 * 60,
            quota_seconds=3600,
            slot_id="09:00-12:00",
        )

    def charge(self, start_second, end_second):
        return core.scheduled_interval_charge(
            self.slot,
            start_second,
            end_second,
        )

    def test_interval_fully_inside_slot_is_fully_charged(self):
        self.assertEqual(
            self.charge(
                10 * 3600,
                10 * 3600 + 1,
            ),
            1.0,
        )

    def test_interval_before_slot_is_not_charged(self):
        self.assertEqual(
            self.charge(
                8 * 3600 + 59 * 60,
                8 * 3600 + 59 * 60 + 1,
            ),
            0.0,
        )

    def test_interval_after_slot_is_not_charged(self):
        self.assertEqual(
            self.charge(
                12 * 3600,
                12 * 3600 + 1,
            ),
            0.0,
        )

    def test_interval_crossing_slot_start_is_clipped(self):
        self.assertAlmostEqual(
            self.charge(
                9 * 3600 - 0.4,
                9 * 3600 + 0.6,
            ),
            0.6,
            places=6,
        )

    def test_interval_crossing_slot_end_is_clipped(self):
        self.assertAlmostEqual(
            self.charge(
                12 * 3600 - 0.3,
                12 * 3600 + 0.7,
            ),
            0.3,
            places=6,
        )

    def test_interval_covering_entire_slot_is_limited_to_slot(self):
        self.assertEqual(
            self.charge(
                8 * 3600,
                13 * 3600,
            ),
            3 * 3600,
        )

    def test_zero_length_interval_is_zero(self):
        self.assertEqual(
            self.charge(
                10 * 3600,
                10 * 3600,
            ),
            0.0,
        )

    def test_reversed_interval_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.charge(
                10 * 3600 + 1,
                10 * 3600,
            )

    def test_negative_clock_second_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.charge(-0.1, 1.0)

    def test_clock_second_beyond_day_is_rejected(self):
        with self.assertRaises(core.ChildTimeError):
            self.charge(0.0, 24 * 3600 + 0.1)


if __name__ == "__main__":
    unittest.main()
