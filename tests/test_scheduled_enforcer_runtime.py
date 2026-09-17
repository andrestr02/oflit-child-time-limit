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


class ScheduledRuntimePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader(
            "child_time_enforcer_d7_test",
            str(ENFORCER),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.enforcer = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.enforcer)

    def test_scheduled_persistence_writes_slot_before_daily(self):
        calls = []

        def write_schedule():
            calls.append("slot")

        def write_daily():
            calls.append("daily")

        self.enforcer.persist_scheduled_runtime(
            write_schedule=write_schedule,
            write_daily=write_daily,
        )

        self.assertEqual(calls, ["slot", "daily"])

    def test_slot_write_failure_prevents_daily_write(self):
        calls = []

        def write_schedule():
            calls.append("slot")
            raise OSError("simulated scheduled-state failure")

        def write_daily():
            calls.append("daily")

        with self.assertRaises(OSError):
            self.enforcer.persist_scheduled_runtime(
                write_schedule=write_schedule,
                write_daily=write_daily,
            )

        self.assertEqual(calls, ["slot"])

    def test_daily_failure_happens_after_slot_write(self):
        calls = []

        def write_schedule():
            calls.append("slot")

        def write_daily():
            calls.append("daily")
            raise OSError("simulated legacy-state failure")

        with self.assertRaises(OSError):
            self.enforcer.persist_scheduled_runtime(
                write_schedule=write_schedule,
                write_daily=write_daily,
            )

        self.assertEqual(calls, ["slot", "daily"])


if __name__ == "__main__":
    unittest.main()


class ScheduledRuntimeStepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader(
            "child_time_enforcer_d7b_test",
            str(ENFORCER),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.enforcer = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.enforcer)

        cls.core = __import__(
            "src.child_time_core",
            fromlist=["child_time_core"],
        )

    def setUp(self):
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

    def step(
        self,
        *,
        start_second,
        end_second,
        minute_of_day,
        legacy_used=0,
        morning_used=0,
        afternoon_used=0,
    ):
        return self.enforcer.process_scheduled_interval(
            daily_limit=7200,
            legacy_used=legacy_used,
            slots=self.slots,
            slot_usage={
                "09:00-12:00": morning_used,
                "15:00-17:00": afternoon_used,
            },
            start_second=start_second,
            end_second=end_second,
            minute_of_day=minute_of_day,
        )

    def test_active_morning_interval_is_accounted(self):
        result = self.step(
            start_second=10 * 3600,
            end_second=10 * 3600 + 1,
            minute_of_day=10 * 60,
        )

        self.assertEqual(result.charged_seconds, 1)
        self.assertEqual(result.daily_used, 1)
        self.assertEqual(result.slot_usage["09:00-12:00"], 1)
        self.assertFalse(result.terminate)

    def test_crossing_slot_end_is_clipped_then_terminated(self):
        result = self.step(
            start_second=12 * 3600 - 0.3,
            end_second=12 * 3600 + 0.7,
            minute_of_day=12 * 60,
        )

        self.assertAlmostEqual(result.charged_seconds, 0.3)
        self.assertAlmostEqual(
            result.slot_usage["09:00-12:00"],
            0.3,
        )
        self.assertTrue(result.terminate)
        self.assertEqual(result.reason, "outside_slot")

    def test_gap_charges_nothing_and_terminates(self):
        result = self.step(
            start_second=13 * 3600,
            end_second=13 * 3600 + 1,
            minute_of_day=13 * 60,
        )

        self.assertEqual(result.charged_seconds, 0)
        self.assertEqual(result.daily_used, 0)
        self.assertTrue(result.terminate)
        self.assertEqual(result.reason, "outside_slot")

    def test_afternoon_unlocks_after_morning_exhausted(self):
        result = self.step(
            start_second=15 * 3600,
            end_second=15 * 3600 + 1,
            minute_of_day=15 * 60,
            legacy_used=3600,
            morning_used=3600,
        )

        self.assertEqual(result.charged_seconds, 1)
        self.assertEqual(result.daily_used, 3601)
        self.assertEqual(result.slot_usage["09:00-12:00"], 3600)
        self.assertEqual(result.slot_usage["15:00-17:00"], 1)
        self.assertFalse(result.terminate)

    def test_reconciliation_never_reduces_legacy_usage(self):
        result = self.step(
            start_second=15 * 3600,
            end_second=15 * 3600 + 1,
            minute_of_day=15 * 60,
            legacy_used=4000,
            morning_used=3600,
        )

        self.assertEqual(result.daily_used, 4001)
        self.assertEqual(result.slot_usage["15:00-17:00"], 1)

    def test_daily_ceiling_terminates(self):
        result = self.step(
            start_second=15 * 3600,
            end_second=15 * 3600 + 2,
            minute_of_day=15 * 60,
            legacy_used=7199,
            afternoon_used=100,
        )

        self.assertEqual(result.charged_seconds, 1)
        self.assertEqual(result.daily_used, 7200)
        self.assertTrue(result.terminate)
        self.assertEqual(result.reason, "daily_exhausted")


if __name__ == "__main__":
    unittest.main()


class ScheduledMainIntegrationContractTests(unittest.TestCase):
    def test_main_runtime_loads_schedule_policy(self):
        source = ENFORCER.read_text()

        main_source = source[source.index("def main():"):]

        self.assertIn(
            "core.load_schedule_policy",
            main_source,
            "main runtime must hot-load scheduled quota policy",
        )

    def test_main_runtime_reads_scheduled_state(self):
        source = ENFORCER.read_text()

        main_source = source[source.index("def main():"):]

        self.assertIn(
            "core.read_schedule_used",
            main_source,
            "scheduled users must load per-slot usage state",
        )

    def test_main_runtime_persists_scheduled_state(self):
        source = ENFORCER.read_text()

        main_source = source[source.index("def main():"):]

        self.assertIn(
            "core.write_schedule_used",
            main_source,
            "scheduled accounting must persist per-slot state",
        )

    def test_main_runtime_uses_scheduled_pipeline(self):
        source = ENFORCER.read_text()

        main_start = source.index("def main():")
        main_end = source.index(
            'if __name__ == "__main__":',
            main_start,
        )
        main_source = source[main_start:main_end]

        self.assertIn(
            "process_scheduled_interval(",
            main_source,
            "main runtime must use the tested scheduled accounting pipeline",
        )


if __name__ == "__main__":
    unittest.main()
