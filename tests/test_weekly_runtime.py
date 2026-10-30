"""Runtime behaviour of the weekly policy: login check and enforcer.

These tests drive the real login-check and enforcer helpers against
isolated temporary policy/state trees with injected wall-clock values,
so weekday behaviour, midnight and Friday->Saturday transitions,
logout/login and a service restart are all deterministic.
"""

import importlib.machinery
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOGIN = ROOT / "src" / "child-time-login-check"
ENFORCER = ROOT / "src" / "child-time-enforcer"
CORE = ROOT / "src" / "child_time_core.py"

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

# A deterministic calendar week. 2026-10-26 is a Monday, and the week
# deliberately crosses a month boundary (ends 2026-11-01, a Sunday).
_MONDAY = datetime(2026, 10, 26)
WEEK = {
    weekday: _MONDAY + timedelta(days=weekday)
    for weekday in range(7)
}


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


core = _load("child_time_core_runtime_test", CORE)


def at(weekday, hour, minute=0, second=0):
    """A local datetime on the given weekday of the reference week."""
    return WEEK[weekday].replace(
        hour=hour, minute=minute, second=second
    ).astimezone()


class RuntimeTree:
    """Isolated policy + state tree for runtime tests."""

    def __init__(self, directory, limits, schedule=None, daily=None):
        self.dir = Path(directory)
        self.config = self.dir / "child-time-limit.conf"
        self.access = self.dir / "child-time-access.conf"
        self.schedule = self.dir / "child-time-schedule.conf"
        self.daily = self.dir / "child-time-daily.conf"
        self.state = self.dir / "state"
        self.schedule_state = self.dir / "schedules"

        self.state.mkdir(parents=True, exist_ok=True)
        self.schedule_state.mkdir(parents=True, exist_ok=True)

        self.config.write_text(limits)
        # Wide access window: these tests are about weekday/slot policy.
        self.access.write_text("start=00:00\nend=23:59\n")
        self.schedule.write_text(schedule or "")
        if daily is not None:
            self.daily.write_text(daily)

    # -- policy ---------------------------------------------------------
    def policy(self):
        return core.load_effective_policy(
            config_path=self.config,
            schedule_path=self.schedule,
            daily_path=self.daily,
        )

    def day_policy(self, username, weekday):
        """Return (effective_limit_seconds, slots) for one weekday."""
        limits, overrides, schedules = self.policy()
        limit = core.effective_daily_limit(
            username, weekday, limits, overrides
        ).seconds
        slots = core.slots_for_weekday(schedules.get(username, []), weekday)
        return limit, slots

    # -- state ----------------------------------------------------------
    def set_daily_used(self, username, day, used):
        (self.state / f"{username}.state").write_text(f"{day} {int(used)}\n")

    def daily_used(self, username, day):
        return core.read_used(
            username, day=day, state_dir=self.state
        )

    def set_slot_used(self, username, day, usage):
        lines = [day] + [f"{key}={int(value)}" for key, value in usage.items()]
        (self.schedule_state / f"{username}.state").write_text(
            "\n".join(lines) + "\n"
        )

    def slot_used(self, username, slots, day):
        return core.read_schedule_used(
            username, slots, day=day, state_dir=self.schedule_state
        )

    # -- login ----------------------------------------------------------
    def check_login(self, username, now):
        login = _load("child_time_login_runtime_test", LOGIN)
        return login.check_login(
            username,
            now,
            config_path=self.config,
            access_config_path=self.access,
            state_dir=self.state,
            schedule_config_path=self.schedule,
            schedule_state_dir=self.schedule_state,
            daily_config_path=self.daily,
        )


ACCEPTANCE_LIMITS = "hudzaifah=3600\n"
ACCEPTANCE_SCHEDULE = "[hudzaifah]\n15:00-17:00=3600\nfri/09:00-12:00=3600\n"
ACCEPTANCE_DAILY = "[hudzaifah]\nfri=7200\n"


class WeekdayLoginRestrictionTests(unittest.TestCase):
    """The Phase 4 scenario, asserted through the real login check."""

    def _tree(self, tmp):
        return RuntimeTree(
            tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
        )

    def test_afternoon_login_allowed_every_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._tree(tmp)
            for weekday in range(7):
                result = tree.check_login("hudzaifah", at(weekday, 15, 30))
                self.assertTrue(
                    result.allowed, core.WEEKDAY_NAMES[weekday]
                )
                self.assertEqual(result.slot.slot_id, "15:00-17:00")

    def test_morning_login_denied_except_on_friday(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._tree(tmp)
            for weekday in (MON, TUE, WED, THU, SAT, SUN):
                result = tree.check_login("hudzaifah", at(weekday, 10, 0))
                self.assertFalse(
                    result.allowed, core.WEEKDAY_NAMES[weekday]
                )
                self.assertEqual(result.reason, "outside_slot")

    def test_friday_morning_login_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._tree(tmp)
            result = tree.check_login("hudzaifah", at(FRI, 10, 0))
            self.assertTrue(result.allowed)
            self.assertEqual(result.slot.slot_id, "fri/09:00-12:00")

    def test_login_denied_outside_every_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._tree(tmp)
            for weekday in range(7):
                for hour in (7, 13, 20):
                    result = tree.check_login("hudzaifah", at(weekday, hour))
                    self.assertFalse(
                        result.allowed,
                        f"{core.WEEKDAY_NAMES[weekday]} {hour}:00",
                    )

    def test_effective_daily_limit_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self._tree(tmp)
            for weekday in range(7):
                limit, slots = tree.day_policy("hudzaifah", weekday)
                if weekday == FRI:
                    self.assertEqual(limit, 7200)
                    self.assertEqual(len(slots), 2)
                else:
                    self.assertEqual(limit, 3600)
                    self.assertEqual(len(slots), 1)


class IndependentSlotQuotaTests(unittest.TestCase):
    def test_exhausting_the_morning_slot_leaves_the_afternoon_slot_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            day = at(FRI, 11).date().isoformat()
            limit, slots = tree.day_policy("hudzaifah", FRI)

            # Morning fully used; nothing used in the afternoon.
            tree.set_slot_used(
                "hudzaifah", day,
                {"fri/09:00-12:00": 3600, "15:00-17:00": 0},
            )
            tree.set_daily_used("hudzaifah", day, 3600)

            # Morning is now refused...
            morning = core.evaluate_login_eligibility(
                daily_limit=limit,
                daily_used=3600,
                slots=slots,
                slot_usage=tree.slot_used("hudzaifah", slots, day),
                minute_of_day=11 * 60,
            )
            self.assertFalse(morning.allowed)
            self.assertEqual(morning.reason, "slot_exhausted")

            # ...but the afternoon slot still has its own full hour.
            afternoon = core.evaluate_login_eligibility(
                daily_limit=limit,
                daily_used=3600,
                slots=slots,
                slot_usage=tree.slot_used("hudzaifah", slots, day),
                minute_of_day=15 * 60 + 30,
            )
            self.assertTrue(afternoon.allowed)
            self.assertEqual(afternoon.slot.slot_id, "15:00-17:00")

    def test_unused_morning_quota_does_not_extend_the_afternoon_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            day = at(FRI, 16).date().isoformat()
            limit, slots = tree.day_policy("hudzaifah", FRI)

            # Morning unused, afternoon fully used.
            usage = {"fri/09:00-12:00": 0, "15:00-17:00": 3600}

            result = core.evaluate_login_eligibility(
                daily_limit=limit,
                daily_used=3600,
                slots=slots,
                slot_usage=usage,
                minute_of_day=16 * 60,
            )
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "slot_exhausted")

    def test_charging_one_slot_does_not_charge_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            limit, slots = tree.day_policy("hudzaifah", FRI)

            # 10:00:00 -> 10:10:00 is inside the morning slot only.
            result = core.account_scheduled_interval(
                daily_limit=limit,
                daily_used=0,
                slots=slots,
                slot_usage={slot.slot_id: 0 for slot in slots},
                start_second=10 * 3600,
                end_second=10 * 3600 + 600,
            )

            self.assertEqual(result.slot_usage["fri/09:00-12:00"], 600)
            self.assertEqual(result.slot_usage["15:00-17:00"], 0)
            self.assertEqual(result.charged_seconds, 600)


class DailyExhaustionTests(unittest.TestCase):
    def test_friday_daily_ceiling_stops_the_afternoon_slot(self):
        """Two hours used on Friday exhausts the day even mid-slot."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            limit, slots = tree.day_policy("hudzaifah", FRI)
            self.assertEqual(limit, 7200)

            result = core.evaluate_login_eligibility(
                daily_limit=limit,
                daily_used=7200,
                slots=slots,
                slot_usage={"fri/09:00-12:00": 3600, "15:00-17:00": 3600},
                minute_of_day=16 * 60,
            )
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "daily_exhausted")

    def test_non_friday_exhausts_after_one_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            day = at(MON, 16).date().isoformat()
            limit, slots = tree.day_policy("hudzaifah", MON)
            self.assertEqual(limit, 3600)

            tree.set_daily_used("hudzaifah", day, 3600)
            tree.set_slot_used("hudzaifah", day, {"15:00-17:00": 3600})

            result = tree.check_login("hudzaifah", at(MON, 16))
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "daily_exhausted")

    def test_daily_charge_stops_at_the_weekday_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            limit, slots = tree.day_policy("hudzaifah", MON)

            # A full two-hour afternoon window may only charge one hour.
            result = core.account_scheduled_interval(
                daily_limit=limit,
                daily_used=0,
                slots=slots,
                slot_usage={"15:00-17:00": 0},
                start_second=15 * 3600,
                end_second=17 * 3600,
            )
            self.assertEqual(result.charged_seconds, 3600)
            self.assertTrue(result.daily_exhausted)


class LogoutLoginBypassTests(unittest.TestCase):
    def test_logout_login_cannot_reset_slot_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            day = at(MON, 16).date().isoformat()
            _, slots = tree.day_policy("hudzaifah", MON)

            # Slot is exhausted in persisted state; legacy daily state has
            # been zeroed (the shape a bypass attempt would produce).
            tree.set_slot_used("hudzaifah", day, {"15:00-17:00": 3600})
            tree.set_daily_used("hudzaifah", day, 0)

            result = tree.check_login("hudzaifah", at(MON, 16))

            self.assertFalse(result.allowed)
            self.assertIn(
                result.reason, ("slot_exhausted", "daily_exhausted")
            )

    def test_reconciliation_never_reports_less_than_either_view(self):
        self.assertEqual(core.reconcile_daily_used(0, {"a": 3600}), 3600)
        self.assertEqual(core.reconcile_daily_used(3600, {"a": 0}), 3600)
        self.assertEqual(
            core.reconcile_daily_used(1800, {"a": 1200, "b": 1200}), 2400
        )

    def test_relogin_inside_the_same_slot_keeps_remaining_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            day = at(MON, 16).date().isoformat()
            _, slots = tree.day_policy("hudzaifah", MON)

            tree.set_slot_used("hudzaifah", day, {"15:00-17:00": 1800})
            tree.set_daily_used("hudzaifah", day, 1800)

            result = tree.check_login("hudzaifah", at(MON, 16))
            self.assertTrue(result.allowed)

            usage = tree.slot_used("hudzaifah", slots, day)
            self.assertEqual(usage["15:00-17:00"], 1800)


class ServiceRestartTests(unittest.TestCase):
    def test_restart_reloads_persisted_slot_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            day = at(FRI, 11).date().isoformat()
            limit, slots = tree.day_policy("hudzaifah", FRI)

            # Simulate the enforcer persisting 40 minutes of morning use.
            core.write_schedule_used(
                "hudzaifah", slots,
                {"fri/09:00-12:00": 2400, "15:00-17:00": 0},
                day=day, state_dir=tree.schedule_state,
            )

            # A "restart" is simply a fresh read of the same state.
            reloaded = tree.slot_used("hudzaifah", slots, day)
            self.assertEqual(reloaded["fri/09:00-12:00"], 2400)

            # Only the remaining 20 minutes may still be charged.
            result = core.account_scheduled_interval(
                daily_limit=limit,
                daily_used=2400,
                slots=slots,
                slot_usage=reloaded,
                start_second=11 * 3600,
                end_second=11 * 3600 + 3600,
            )
            self.assertEqual(result.charged_seconds, 1200)
            self.assertEqual(result.slot_usage["fri/09:00-12:00"], 3600)

    def test_restart_does_not_resurrect_another_days_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            _, slots = tree.day_policy("hudzaifah", FRI)

            # State stamped with Thursday.
            tree.set_slot_used(
                "hudzaifah",
                at(THU, 16).date().isoformat(),
                {"15:00-17:00": 3600},
            )

            usage = tree.slot_used(
                "hudzaifah", slots, at(FRI, 11).date().isoformat()
            )
            self.assertEqual(usage["fri/09:00-12:00"], 0)
            self.assertEqual(usage["15:00-17:00"], 0)


class DayTransitionTests(unittest.TestCase):
    def test_slot_usage_resets_automatically_on_a_new_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            _, monday_slots = tree.day_policy("hudzaifah", MON)

            tree.set_slot_used(
                "hudzaifah",
                at(MON, 16).date().isoformat(),
                {"15:00-17:00": 3600},
            )

            # Tuesday reads the same file but a different day stamp.
            _, tuesday_slots = tree.day_policy("hudzaifah", TUE)
            usage = tree.slot_used(
                "hudzaifah", tuesday_slots, at(TUE, 16).date().isoformat()
            )
            self.assertEqual(usage["15:00-17:00"], 0)

            result = tree.check_login("hudzaifah", at(TUE, 16))
            self.assertTrue(result.allowed)

    def test_friday_to_saturday_drops_the_friday_slot_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )

            friday_limit, friday_slots = tree.day_policy("hudzaifah", FRI)
            saturday_limit, saturday_slots = tree.day_policy("hudzaifah", SAT)

            self.assertEqual(friday_limit, 7200)
            self.assertEqual(saturday_limit, 3600)
            self.assertEqual(
                [slot.slot_id for slot in friday_slots],
                ["fri/09:00-12:00", "15:00-17:00"],
            )
            self.assertEqual(
                [slot.slot_id for slot in saturday_slots],
                ["15:00-17:00"],
            )

            # Saturday morning is refused even though Friday allowed it.
            self.assertTrue(tree.check_login("hudzaifah", at(FRI, 10)).allowed)
            saturday = tree.check_login("hudzaifah", at(SAT, 10))
            self.assertFalse(saturday.allowed)
            self.assertEqual(saturday.reason, "outside_slot")

    def test_friday_usage_does_not_consume_saturday_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            # Friday fully used (2h).
            tree.set_slot_used(
                "hudzaifah",
                at(FRI, 16).date().isoformat(),
                {"fri/09:00-12:00": 3600, "15:00-17:00": 3600},
            )
            tree.set_daily_used(
                "hudzaifah", at(FRI, 16).date().isoformat(), 7200
            )

            result = tree.check_login("hudzaifah", at(SAT, 16))
            self.assertTrue(result.allowed)
            self.assertEqual(result.slot.slot_id, "15:00-17:00")

    def test_friday_only_state_keys_are_not_written_on_saturday(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            _, saturday_slots = tree.day_policy("hudzaifah", SAT)
            day = at(SAT, 16).date().isoformat()

            core.write_schedule_used(
                "hudzaifah", saturday_slots,
                {"15:00-17:00": 600},
                day=day, state_dir=tree.schedule_state,
            )

            text = (tree.schedule_state / "hudzaifah.state").read_text()
            self.assertIn("15:00-17:00=600", text)
            self.assertNotIn("fri/09:00-12:00", text)

    def test_writing_a_slot_that_does_not_apply_today_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            _, saturday_slots = tree.day_policy("hudzaifah", SAT)

            with self.assertRaises(core.ChildTimeError):
                core.write_schedule_used(
                    "hudzaifah", saturday_slots,
                    {"fri/09:00-12:00": 600},
                    day=at(SAT, 16).date().isoformat(),
                    state_dir=tree.schedule_state,
                )

    def test_midnight_crossing_interval_is_not_charged(self):
        """The enforcer refuses to attribute a cross-midnight interval."""
        source = ENFORCER.read_text()
        self.assertIn("same_day_interval", source)
        self.assertIn(
            "if not same_day_interval:\n                terminate(username)",
            source,
        )

    def test_interval_charge_is_clamped_to_the_local_day(self):
        slot = core.ScheduledSlot(
            "hudzaifah", 15 * 60, 17 * 60, 3600, "15:00-17:00"
        )
        with self.assertRaises(core.ChildTimeError):
            core.scheduled_interval_charge(slot, 23 * 3600, 25 * 3600)


class EnforcerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.enforcer = _load("child_time_enforcer_weekly_test", ENFORCER)

    def test_enforcer_resolves_policy_through_the_shared_core(self):
        source = ENFORCER.read_text()
        for symbol in (
            "core.effective_daily_limit",
            "core.slots_for_weekday",
            "core.load_daily_overrides",
        ):
            self.assertIn(symbol, source)

    def test_enforcer_defines_its_runtime_helpers_once(self):
        source = ENFORCER.read_text()
        self.assertEqual(source.count("class ScheduledRuntimeResult:"), 1)
        self.assertEqual(source.count("def process_scheduled_interval("), 1)

    def test_enforcer_terminates_when_the_weekday_slot_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            limit, slots = tree.day_policy("hudzaifah", MON)

            runtime = self.enforcer.process_scheduled_interval(
                daily_limit=limit,
                legacy_used=3595,
                slots=slots,
                slot_usage={"15:00-17:00": 3595},
                start_second=16 * 3600,
                end_second=16 * 3600 + 10,
                minute_of_day=16 * 60,
            )

            self.assertTrue(runtime.terminate)
            self.assertEqual(runtime.slot_usage["15:00-17:00"], 3600)

    def test_enforcer_terminates_outside_the_weekday_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            limit, slots = tree.day_policy("hudzaifah", SAT)

            runtime = self.enforcer.process_scheduled_interval(
                daily_limit=limit,
                legacy_used=0,
                slots=slots,
                slot_usage={"15:00-17:00": 0},
                start_second=10 * 3600,
                end_second=10 * 3600 + 5,
                minute_of_day=10 * 60,
            )

            self.assertTrue(runtime.terminate)
            self.assertEqual(runtime.reason, "outside_slot")
            self.assertEqual(runtime.charged_seconds, 0)

    def test_enforcer_allows_use_inside_the_weekday_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE, ACCEPTANCE_DAILY
            )
            limit, slots = tree.day_policy("hudzaifah", FRI)

            runtime = self.enforcer.process_scheduled_interval(
                daily_limit=limit,
                legacy_used=0,
                slots=slots,
                slot_usage={slot.slot_id: 0 for slot in slots},
                start_second=10 * 3600,
                end_second=10 * 3600 + 60,
                minute_of_day=10 * 60,
            )

            self.assertFalse(runtime.terminate)
            self.assertEqual(runtime.charged_seconds, 60)
            self.assertEqual(runtime.slot_usage["fri/09:00-12:00"], 60)


class InvalidPolicyFailsClosedTests(unittest.TestCase):
    def test_broken_schedule_denies_a_configured_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, "[hudzaifah]\nfunday/09:00-10:00=600\n"
            )
            result = tree.check_login("hudzaifah", at(MON, 16))
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "policy_error")

    def test_broken_weekday_limit_file_denies_a_configured_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(
                tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE,
                "[hudzaifah]\nfunday=7200\n",
            )
            result = tree.check_login("hudzaifah", at(MON, 16))
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "policy_error")

    def test_weekday_quota_over_its_ceiling_denies_a_configured_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No Friday override, so Friday schedules 2h against a 1h limit.
            tree = RuntimeTree(tmp, ACCEPTANCE_LIMITS, ACCEPTANCE_SCHEDULE)
            result = tree.check_login("hudzaifah", at(FRI, 10))
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "policy_error")

    def test_enforcer_fails_closed_on_broken_policy(self):
        source = ENFORCER.read_text()
        self.assertIn("schedule_policy_error = True", source)
        self.assertIn(
            "if schedule_policy_error:\n                    # We cannot know",
            source,
        )


class UnrelatedUserTests(unittest.TestCase):
    """Weekly scheduling must not change anyone it was not configured for."""

    LIMITS = "hudzaifah=3600\nazzahra=7200\nibrohim=5400\n"
    SCHEDULE = (
        "[hudzaifah]\n15:00-17:00=3600\nfri/09:00-12:00=3600\n"
        "[azzahra]\n09:00-12:00=3600\n15:00-17:00=3600\n"
    )

    def test_administrator_account_is_never_restricted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(tmp, self.LIMITS, self.SCHEDULE, ACCEPTANCE_DAILY)
            for weekday in range(7):
                for hour in (3, 10, 16, 22):
                    result = tree.check_login("root", at(weekday, hour))
                    self.assertTrue(result.allowed)
                    self.assertEqual(result.reason, "unconfigured")

    def test_azzahras_existing_schedule_behaves_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(tmp, self.LIMITS, self.SCHEDULE, ACCEPTANCE_DAILY)

            for weekday in range(7):
                limit, slots = tree.day_policy("azzahra", weekday)
                self.assertEqual(limit, 7200, core.WEEKDAY_NAMES[weekday])
                self.assertEqual(
                    [slot.slot_id for slot in slots],
                    ["09:00-12:00", "15:00-17:00"],
                    core.WEEKDAY_NAMES[weekday],
                )

                self.assertTrue(
                    tree.check_login("azzahra", at(weekday, 10)).allowed
                )
                self.assertTrue(
                    tree.check_login("azzahra", at(weekday, 16)).allowed
                )
                self.assertFalse(
                    tree.check_login("azzahra", at(weekday, 13)).allowed
                )

    def test_ibrohims_daily_quota_behaves_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(tmp, self.LIMITS, self.SCHEDULE, ACCEPTANCE_DAILY)

            for weekday in range(7):
                limit, slots = tree.day_policy("ibrohim", weekday)
                self.assertEqual(limit, 5400)
                self.assertEqual(slots, [])

                # No slot restrictions: any hour inside the access window.
                for hour in (1, 10, 16, 22):
                    result = tree.check_login("ibrohim", at(weekday, hour))
                    self.assertTrue(
                        result.allowed,
                        f"{core.WEEKDAY_NAMES[weekday]} {hour}:00",
                    )
                    self.assertIsNone(result.slot)

    def test_ibrohim_is_still_stopped_by_his_daily_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(tmp, self.LIMITS, self.SCHEDULE, ACCEPTANCE_DAILY)
            day = at(WED, 16).date().isoformat()
            tree.set_daily_used("ibrohim", day, 5400)

            result = tree.check_login("ibrohim", at(WED, 16))
            self.assertFalse(result.allowed)
            self.assertEqual(result.reason, "daily_exhausted")

    def test_another_users_weekday_override_does_not_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = RuntimeTree(tmp, self.LIMITS, self.SCHEDULE, ACCEPTANCE_DAILY)
            # Friday override belongs to hudzaifah only.
            self.assertEqual(tree.day_policy("hudzaifah", FRI)[0], 7200)
            self.assertEqual(tree.day_policy("azzahra", FRI)[0], 7200)
            self.assertEqual(tree.day_policy("ibrohim", FRI)[0], 5400)
            self.assertEqual(tree.day_policy("azzahra", MON)[0], 7200)


if __name__ == "__main__":
    unittest.main()
