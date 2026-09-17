import unittest
from unittest.mock import Mock

from src import child_time_core as core


class ScheduledPersistenceOrderTests(unittest.TestCase):
    def test_slot_state_is_persisted_before_daily_state(self):
        calls = []

        def write_slot():
            calls.append("slot")

        def write_daily():
            calls.append("daily")

        core.persist_scheduled_accounting(
            write_slot=write_slot,
            write_daily=write_daily,
        )

        self.assertEqual(calls, ["slot", "daily"])

    def test_daily_state_is_not_written_if_slot_write_fails(self):
        write_daily = Mock()

        def write_slot():
            raise OSError("simulated slot persistence failure")

        with self.assertRaises(OSError):
            core.persist_scheduled_accounting(
                write_slot=write_slot,
                write_daily=write_daily,
            )

        write_daily.assert_not_called()

    def test_daily_failure_occurs_only_after_slot_is_durable(self):
        calls = []

        def write_slot():
            calls.append("slot")

        def write_daily():
            calls.append("daily")
            raise OSError("simulated daily persistence failure")

        with self.assertRaises(OSError):
            core.persist_scheduled_accounting(
                write_slot=write_slot,
                write_daily=write_daily,
            )

        self.assertEqual(calls, ["slot", "daily"])


if __name__ == "__main__":
    unittest.main()
