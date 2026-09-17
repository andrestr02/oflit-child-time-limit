import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "child-time"


def load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "child_time_cli_scheduled_test",
        str(CLI),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ScheduledUntilTests(unittest.TestCase):
    def test_until_rejects_scheduled_user(self):
        cli = load_cli()

        args = mock.Mock()
        args.username = "azzahra"
        args.clock = "16:00"

        slot = cli.core.ScheduledSlot(
            "azzahra",
            540,
            720,
            3600,
            "09:00-12:00",
        )

        with mock.patch.object(
            cli.core,
            "load_schedule_policy",
            return_value={"azzahra": [slot]},
        ), mock.patch.object(
            cli.core,
            "apply_limit_transaction",
        ) as transaction:
            with self.assertRaises(cli.ChildTimeError) as ctx:
                cli.command_until(args)

        self.assertIn("scheduled", str(ctx.exception).lower())
        transaction.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ScheduledDailyCeilingMutationTests(unittest.TestCase):
    def setUp(self):
        self.core = load_cli().core
        self.slots = [
            self.core.ScheduledSlot(
                "azzahra", 540, 720, 3600, "09:00-12:00"
            ),
            self.core.ScheduledSlot(
                "azzahra", 900, 1020, 3600, "15:00-17:00"
            ),
        ]

    def test_candidate_ceiling_below_slot_aggregate_is_rejected(self):
        with self.assertRaises(self.core.ChildTimeError):
            self.core.validate_scheduled_daily_ceiling(
                "azzahra",
                5400,
                {"azzahra": self.slots},
            )

    def test_candidate_ceiling_equal_to_slot_aggregate_is_allowed(self):
        self.core.validate_scheduled_daily_ceiling(
            "azzahra",
            7200,
            {"azzahra": self.slots},
        )

    def test_unscheduled_user_is_unchanged(self):
        self.core.validate_scheduled_daily_ceiling(
            "hudzaifah",
            1800,
            {"azzahra": self.slots},
        )


if __name__ == "__main__":
    unittest.main()


class ScheduledDailyCeilingTransactionTests(unittest.TestCase):
    def _slots(self, core):
        return [
            core.ScheduledSlot(
                "azzahra", 540, 720, 3600, "09:00-12:00"
            ),
            core.ScheduledSlot(
                "azzahra", 900, 1020, 3600, "15:00-17:00"
            ),
        ]

    def test_transaction_rejects_ceiling_below_schedule_before_write(self):
        import tempfile

        cli = load_cli()
        core = cli.core
        slots = self._slots(core)

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "child-time-limit.conf"
            config_path.write_text("azzahra=7200\n")

            with mock.patch.object(
                core,
                "load_schedule_policy",
                return_value={"azzahra": slots},
            ), mock.patch.object(
                core,
                "read_used",
                return_value=0,
            ), mock.patch.object(
                core,
                "_atomic_update_limit_locked",
                return_value=(7200, 5400),
            ) as writer:
                with self.assertRaises(core.ChildTimeError) as ctx:
                    core.apply_limit_transaction(
                        "azzahra",
                        lambda old_limit, used: 5400,
                        config_path=config_path,
                    )

            self.assertIn("scheduled", str(ctx.exception).lower())
            writer.assert_not_called()

    def test_transaction_allows_ceiling_equal_to_schedule_aggregate(self):
        import tempfile

        cli = load_cli()
        core = cli.core
        slots = self._slots(core)

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "child-time-limit.conf"
            config_path.write_text("azzahra=9000\n")

            with mock.patch.object(
                core,
                "load_schedule_policy",
                return_value={"azzahra": slots},
            ), mock.patch.object(
                core,
                "read_used",
                return_value=0,
            ), mock.patch.object(
                core,
                "_atomic_update_limit_locked",
                return_value=(9000, 7200),
            ) as writer:
                result = core.apply_limit_transaction(
                    "azzahra",
                    lambda old_limit, used: 7200,
                    config_path=config_path,
                )

            writer.assert_called_once()
            self.assertEqual(result.new_limit, 7200)


if __name__ == "__main__":
    unittest.main()
