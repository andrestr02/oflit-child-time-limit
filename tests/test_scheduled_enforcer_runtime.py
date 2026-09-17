import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENFORCER = ROOT / "src" / "child-time-enforcer"


class ScheduledEnforcerRuntimeTests(unittest.TestCase):
    def test_enforcer_is_importable_without_starting_daemon(self):
        loader = importlib.machinery.SourceFileLoader(
            "child_time_enforcer_test",
            str(ENFORCER),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)

        # Import must return. It must not acquire the production lock,
        # create production state, sleep, or enter the daemon loop.
        loader.exec_module(module)

        self.assertTrue(
            hasattr(module, "main"),
            "enforcer must expose main() after runtime refactor",
        )


if __name__ == "__main__":
    unittest.main()


class ScheduledEnforcerAccountingTests(unittest.TestCase):
    def setUp(self):
        self.core = __import__(
            "src.child_time_core",
            fromlist=["child_time_core"],
        )
        self.morning = self.core.ScheduledSlot(
            username="azzahra",
            start_minute=9 * 60,
            end_minute=12 * 60,
            quota_seconds=3600,
            slot_id="09:00-12:00",
        )
        self.afternoon = self.core.ScheduledSlot(
            username="azzahra",
            start_minute=15 * 60,
            end_minute=17 * 60,
            quota_seconds=3600,
            slot_id="15:00-17:00",
        )

    def account(self, start_second, end_second, *,
                daily_used=0, morning_used=0, afternoon_used=0):
        return self.core.account_scheduled_interval(
            daily_limit=7200,
            daily_used=daily_used,
            slots=[self.morning, self.afternoon],
            slot_usage={
                self.morning.slot_id: morning_used,
                self.afternoon.slot_id: afternoon_used,
            },
            start_second=start_second,
            end_second=end_second,
        )

    def test_crossing_morning_start_charges_only_inside_slot(self):
        result = self.account(
            9 * 3600 - 0.4,
            9 * 3600 + 0.6,
        )
        self.assertAlmostEqual(result.charged_seconds, 0.6)
        self.assertAlmostEqual(result.slot_usage[self.morning.slot_id], 0.6)

    def test_crossing_morning_end_charges_only_until_end(self):
        result = self.account(
            12 * 3600 - 0.3,
            12 * 3600 + 0.7,
        )
        self.assertAlmostEqual(result.charged_seconds, 0.3)
        self.assertAlmostEqual(result.slot_usage[self.morning.slot_id], 0.3)

    def test_gap_interval_charges_nothing(self):
        result = self.account(
            13 * 3600,
            13 * 3600 + 10,
        )
        self.assertEqual(result.charged_seconds, 0)
        self.assertEqual(result.daily_used, 0)

    def test_afternoon_does_not_charge_morning_slot(self):
        result = self.account(
            15 * 3600,
            15 * 3600 + 1,
            morning_used=3600,
        )
        self.assertEqual(result.slot_usage[self.morning.slot_id], 3600)
        self.assertEqual(result.slot_usage[self.afternoon.slot_id], 1)
        self.assertEqual(result.charged_seconds, 1)

    def test_exhausted_slot_adds_nothing(self):
        result = self.account(
            10 * 3600,
            10 * 3600 + 1,
            morning_used=3600,
        )
        self.assertEqual(result.charged_seconds, 0)
        self.assertTrue(result.slot_exhausted)

    def test_daily_ceiling_clips_charge(self):
        result = self.account(
            15 * 3600,
            15 * 3600 + 1,
            daily_used=7199.5,
            afternoon_used=100,
        )
        self.assertAlmostEqual(result.charged_seconds, 0.5)
        self.assertEqual(result.daily_used, 7200)
        self.assertTrue(result.daily_exhausted)


if __name__ == "__main__":
    unittest.main()


class ScheduledEnforcerDecisionTests(unittest.TestCase):
    def setUp(self):
        self.core = __import__(
            "src.child_time_core",
            fromlist=["child_time_core"],
        )
        self.slots = [
            self.core.ScheduledSlot(
                username="azzahra",
                start_minute=9 * 60,
                end_minute=12 * 60,
                quota_seconds=3600,
                slot_id="09:00-12:00",
            ),
            self.core.ScheduledSlot(
                username="azzahra",
                start_minute=15 * 60,
                end_minute=17 * 60,
                quota_seconds=3600,
                slot_id="15:00-17:00",
            ),
        ]

    def decide(self, minute, *, daily_used=0,
               morning_used=0, afternoon_used=0):
        return self.core.evaluate_scheduled_enforcement(
            daily_limit=7200,
            daily_used=daily_used,
            slots=self.slots,
            slot_usage={
                "09:00-12:00": morning_used,
                "15:00-17:00": afternoon_used,
            },
            minute_of_day=minute,
        )

    def test_active_morning_slot_may_continue(self):
        result = self.decide(10 * 60)
        self.assertFalse(result.terminate)
        self.assertEqual(result.reason, "allowed")

    def test_exact_slot_end_requires_termination(self):
        result = self.decide(12 * 60)
        self.assertTrue(result.terminate)
        self.assertEqual(result.reason, "outside_slot")

    def test_gap_requires_termination(self):
        result = self.decide(13 * 60)
        self.assertTrue(result.terminate)
        self.assertEqual(result.reason, "outside_slot")

    def test_exhausted_active_slot_requires_termination(self):
        result = self.decide(
            10 * 60,
            morning_used=3600,
        )
        self.assertTrue(result.terminate)
        self.assertEqual(result.reason, "slot_exhausted")

    def test_daily_ceiling_requires_termination(self):
        result = self.decide(
            15 * 60,
            daily_used=7200,
        )
        self.assertTrue(result.terminate)
        self.assertEqual(result.reason, "daily_exhausted")

    def test_afternoon_unlocks_after_morning_exhausted(self):
        result = self.decide(
            15 * 60,
            daily_used=3600,
            morning_used=3600,
            afternoon_used=0,
        )
        self.assertFalse(result.terminate)
        self.assertEqual(result.reason, "allowed")


if __name__ == "__main__":
    unittest.main()
