import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOGIN = ROOT / "src/child-time-login-check"


def load_login_module():
    loader = importlib.machinery.SourceFileLoader(
        "child_time_login_check_test", str(LOGIN)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class LegacyLoginParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self.limit_config = self.root / "child-time-limit.conf"
        self.access_config = self.root / "child-time-access.conf"
        self.state_dir = self.root / "state"
        self.schedule_config = self.root / "child-time-schedule.conf"
        self.schedule_state_dir = self.root / "schedules"

        self.state_dir.mkdir()
        self.schedule_state_dir.mkdir()
        self.limit_config.write_text("azzahra=7200\n")
        self.access_config.write_text("start=09:00\nend=17:00\n")
        self.schedule_config.write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        # Import must not execute PAM/login policy as a side effect.
        with mock.patch.dict(os.environ, {"PAM_USER": "andrestr02"}):
            return load_login_module()

    def test_import_has_no_pam_side_effect(self):
        module = self.load()
        self.assertTrue(callable(module.check_login))
        self.assertTrue(callable(module.main))

    def test_unconfigured_user_fails_open(self):
        module = self.load()

        result = module.check_login(
            "andrestr02",
            datetime(2026, 9, 17, 10, 0),
            config_path=self.limit_config,
            access_config_path=self.access_config,
            state_dir=self.state_dir,
            schedule_config_path=self.schedule_config,
            schedule_state_dir=self.schedule_state_dir,
        )

        self.assertTrue(result.allowed)

    def test_configured_child_under_daily_limit_is_allowed(self):
        module = self.load()
        (self.state_dir / "azzahra.state").write_text(
            "2026-09-17 3599\n"
        )

        result = module.check_login(
            "azzahra",
            datetime(2026, 9, 17, 10, 0),
            config_path=self.limit_config,
            access_config_path=self.access_config,
            state_dir=self.state_dir,
            schedule_config_path=self.schedule_config,
            schedule_state_dir=self.schedule_state_dir,
        )

        self.assertTrue(result.allowed)

    def test_configured_child_at_daily_limit_is_denied(self):
        module = self.load()
        (self.state_dir / "azzahra.state").write_text(
            "2026-09-17 7200\n"
        )

        result = module.check_login(
            "azzahra",
            datetime(2026, 9, 17, 10, 0),
            config_path=self.limit_config,
            access_config_path=self.access_config,
            state_dir=self.state_dir,
            schedule_config_path=self.schedule_config,
            schedule_state_dir=self.schedule_state_dir,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "daily_exhausted")


if __name__ == "__main__":
    unittest.main()

class ScheduledLoginRuntimeTests(LegacyLoginParityTests):
    def setUp(self):
        super().setUp()


        self.schedule_config.write_text(
            "[azzahra]\n"
            "09:00-12:00=3600\n"
            "15:00-17:00=3600\n"
        )

    def check(self, when):
        module = self.load()
        return module.check_login(
            "azzahra",
            when,
            config_path=self.limit_config,
            access_config_path=self.access_config,
            state_dir=self.state_dir,
            schedule_config_path=self.schedule_config,
            schedule_state_dir=self.schedule_state_dir,
        )

    def test_slot_end_boundary_is_denied(self):
        result = self.check(datetime(2026, 9, 17, 12, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "outside_slot")

    def test_inside_active_slot_is_allowed(self):
        result = self.check(datetime(2026, 9, 17, 10, 0))
        self.assertTrue(result.allowed)

    def test_gap_between_slots_is_denied(self):
        result = self.check(datetime(2026, 9, 17, 13, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "outside_slot")

    def test_exhausted_active_slot_is_denied(self):
        (self.schedule_state_dir / "azzahra.state").write_text(
            "2026-09-17\n"
            "09:00-12:00=3600\n"
            "15:00-17:00=0\n"
        )

        result = self.check(datetime(2026, 9, 17, 10, 0))
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "slot_exhausted")

    def test_afternoon_slot_unlocks_after_morning_exhaustion(self):
        (self.state_dir / "azzahra.state").write_text(
            "2026-09-17 3600\n"
        )
        (self.schedule_state_dir / "azzahra.state").write_text(
            "2026-09-17\n"
            "09:00-12:00=3600\n"
            "15:00-17:00=0\n"
        )

        result = self.check(datetime(2026, 9, 17, 15, 0))
        self.assertTrue(result.allowed)
        self.assertEqual(result.slot.slot_id, "15:00-17:00")

    def test_malformed_schedule_policy_fails_closed(self):
        self.schedule_config.write_text(
            "[azzahra]\n"
            "09:00-12:00=INVALID\n"
        )

        result = self.check(datetime(2026, 9, 17, 10, 0))

        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "policy_error")

    def test_malformed_current_day_schedule_state_fails_closed(self):
        (self.schedule_state_dir / "azzahra.state").write_text(
            "2026-09-17\n"
            "09:00-12:00=INVALID\n"
        )

        result = self.check(datetime(2026, 9, 17, 10, 0))

        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "policy_error")

