"""Weekly scheduling engine: weekday parsing, policy resolution, validation.

Every test builds its own policy tree under a TemporaryDirectory, so no
test can read or write the production files in /etc or /var/lib.
"""

import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def load_core():
    loader = importlib.machinery.SourceFileLoader(
        "child_time_core_weekly_test",
        str(ROOT / "src" / "child_time_core.py"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


core = load_core()


class PolicyTree:
    """An isolated limits/schedule/daily/state tree for one test."""

    def __init__(self, directory, limits="hudzaifah=3600\n"):
        self.dir = Path(directory)
        self.config = self.dir / "child-time-limit.conf"
        self.schedule = self.dir / "child-time-schedule.conf"
        self.daily = self.dir / "child-time-daily.conf"
        self.state = self.dir / "state"
        self.schedule_state = self.state / "schedules"
        self.state.mkdir(parents=True, exist_ok=True)
        self.schedule_state.mkdir(parents=True, exist_ok=True)
        self.config.write_text(limits)

    def write_schedule(self, text):
        self.schedule.write_text(text)

    def write_daily(self, text):
        self.daily.write_text(text)

    @property
    def paths(self):
        return {"config_path": self.config}

    def load(self):
        return core.load_effective_policy(
            config_path=self.config,
            schedule_path=self.schedule,
            daily_path=self.daily,
        )


class WeekdayExpressionTests(unittest.TestCase):
    def test_single_days(self):
        for index, name in enumerate(core.WEEKDAY_NAMES):
            with self.subTest(name=name):
                self.assertEqual(
                    core.parse_weekday_expression(name),
                    frozenset({index}),
                )

    def test_long_names_and_case_are_accepted(self):
        self.assertEqual(
            core.parse_weekday_expression("Friday"),
            frozenset({FRI}),
        )
        self.assertEqual(core.parse_weekday_expression("MON"), frozenset({MON}))

    def test_inclusive_range(self):
        self.assertEqual(
            core.parse_weekday_expression("mon-fri"),
            frozenset({MON, TUE, WED, THU, FRI}),
        )

    def test_range_wraps_past_sunday(self):
        self.assertEqual(
            core.parse_weekday_expression("fri-mon"),
            frozenset({FRI, SAT, SUN, MON}),
        )

    def test_comma_list_and_mixed_terms(self):
        self.assertEqual(
            core.parse_weekday_expression("sat,sun"),
            frozenset({SAT, SUN}),
        )
        self.assertEqual(
            core.parse_weekday_expression("mon-wed,fri"),
            frozenset({MON, TUE, WED, FRI}),
        )

    def test_group_shorthands(self):
        self.assertEqual(core.parse_weekday_expression("daily"), core.ALL_WEEKDAYS)
        self.assertEqual(
            core.parse_weekday_expression("weekdays"),
            frozenset({MON, TUE, WED, THU, FRI}),
        )
        self.assertEqual(
            core.parse_weekday_expression("weekend"),
            frozenset({SAT, SUN}),
        )

    def test_all_seven_weekdays_are_supported(self):
        self.assertEqual(core.parse_weekday_expression("mon-sun"), core.ALL_WEEKDAYS)
        self.assertEqual(len(core.ALL_WEEKDAYS), 7)

    def test_invalid_expressions_are_rejected(self):
        for bad in ["", "   ", "funday", "mon-funday", "mon--fri", "mon,", ",", "8"]:
            with self.subTest(bad=bad):
                with self.assertRaises(core.ChildTimeError):
                    core.parse_weekday_expression(bad)

    def test_canonical_formatting_round_trips(self):
        for expression in [
            "fri",
            "mon-fri",
            "sat,sun",
            "daily",
            "mon-wed,fri",
            "fri-mon",
        ]:
            with self.subTest(expression=expression):
                days = core.parse_weekday_expression(expression)
                canonical = core.format_weekday_set(days)
                self.assertEqual(
                    core.parse_weekday_expression(canonical),
                    days,
                )

    def test_formatting_is_stable(self):
        self.assertEqual(core.format_weekday_set({MON, TUE, WED, THU, FRI}), "mon-fri")
        self.assertEqual(core.format_weekday_set({SAT, SUN}), "sat,sun")
        self.assertEqual(core.format_weekday_set({FRI}), "fri")
        self.assertEqual(core.format_weekday_set(core.ALL_WEEKDAYS), "daily")


class SlotIdentifierTests(unittest.TestCase):
    def test_all_week_slot_keeps_legacy_identifier(self):
        self.assertEqual(
            core.schedule_slot_id(core.ALL_WEEKDAYS, "15:00", "17:00"),
            "15:00-17:00",
        )

    def test_weekday_scoped_slot_is_prefixed(self):
        self.assertEqual(
            core.schedule_slot_id({FRI}, "09:00", "12:00"),
            "fri/09:00-12:00",
        )

    def test_identifier_is_stable_for_the_same_schedule(self):
        first = core.schedule_slot_id(
            core.parse_weekday_expression("mon-fri"), "08:00", "09:00"
        )
        second = core.schedule_slot_id(
            core.parse_weekday_expression("fri,mon,tue,wed,thu"), "08:00", "09:00"
        )
        self.assertEqual(first, second)


class WeekdayPolicyResolutionTests(unittest.TestCase):
    def test_acceptance_scenario_resolves_per_weekday(self):
        """Phase 4 scenario, asserted as a weekday-by-weekday matrix."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=3600\n")
            tree.write_schedule(
                "[hudzaifah]\n15:00-17:00=3600\nfri/09:00-12:00=3600\n"
            )
            tree.write_daily("[hudzaifah]\nfri=7200\n")

            limits, overrides, schedules = tree.load()

            expected = {
                MON: (3600, ["15:00-17:00"]),
                TUE: (3600, ["15:00-17:00"]),
                WED: (3600, ["15:00-17:00"]),
                THU: (3600, ["15:00-17:00"]),
                FRI: (7200, ["fri/09:00-12:00", "15:00-17:00"]),
                SAT: (3600, ["15:00-17:00"]),
                SUN: (3600, ["15:00-17:00"]),
            }

            for weekday, (limit, slot_ids) in expected.items():
                with self.subTest(weekday=core.WEEKDAY_NAMES[weekday]):
                    effective = core.effective_daily_limit(
                        "hudzaifah", weekday, limits, overrides
                    )
                    self.assertEqual(effective.seconds, limit)

                    applicable = core.slots_for_weekday(
                        schedules["hudzaifah"], weekday
                    )
                    self.assertEqual(
                        [slot.slot_id for slot in applicable],
                        slot_ids,
                    )

    def test_weekday_range_schedule_applies_only_in_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=7200\n")
            tree.write_schedule("[hudzaifah]\nmon-fri/08:00-09:00=1800\n")

            _, _, schedules = tree.load()

            for weekday in (MON, TUE, WED, THU, FRI):
                self.assertEqual(
                    len(core.slots_for_weekday(schedules["hudzaifah"], weekday)),
                    1,
                    core.WEEKDAY_NAMES[weekday],
                )
            for weekday in (SAT, SUN):
                self.assertEqual(
                    core.slots_for_weekday(schedules["hudzaifah"], weekday),
                    [],
                    core.WEEKDAY_NAMES[weekday],
                )

    def test_weekend_only_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=7200\n")
            tree.write_schedule("[hudzaifah]\nweekend/10:00-12:00=3600\n")

            _, _, schedules = tree.load()

            self.assertEqual(
                [slot.slot_id for slot in schedules["hudzaifah"]],
                ["sat,sun/10:00-12:00"],
            )
            self.assertEqual(
                core.slots_for_weekday(schedules["hudzaifah"], MON), []
            )
            self.assertEqual(
                len(core.slots_for_weekday(schedules["hudzaifah"], SAT)), 1
            )

    def test_different_quotas_per_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=7200\n")
            tree.write_schedule(
                "[hudzaifah]\nmon/08:00-09:00=1800\nmon/15:00-17:00=3600\n"
            )

            _, _, schedules = tree.load()
            quotas = {
                slot.slot_id: slot.quota_seconds
                for slot in core.slots_for_weekday(schedules["hudzaifah"], MON)
            }
            self.assertEqual(
                quotas, {"mon/08:00-09:00": 1800, "mon/15:00-17:00": 3600}
            )

    def test_duration_quota_is_accepted_in_hand_edited_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=7200\n")
            tree.write_schedule("[hudzaifah]\nfri/09:00-12:00=1h30m\n")

            _, _, schedules = tree.load()
            self.assertEqual(schedules["hudzaifah"][0].quota_seconds, 5400)


class DailyOverrideTests(unittest.TestCase):
    def test_override_applies_only_to_named_weekday(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=3600\n")
            tree.write_daily("[hudzaifah]\nfri=7200\n")

            limits, overrides, _ = tree.load()

            self.assertEqual(
                core.effective_daily_limit(
                    "hudzaifah", FRI, limits, overrides
                ),
                core.EffectiveLimit("hudzaifah", FRI, 7200, "override"),
            )
            self.assertEqual(
                core.effective_daily_limit(
                    "hudzaifah", MON, limits, overrides
                ).source,
                "base",
            )

    def test_override_expression_covers_several_weekdays(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=3600\n")
            tree.write_daily("[hudzaifah]\nweekend=7200\n")

            limits, overrides, _ = tree.load()

            for weekday in (SAT, SUN):
                self.assertEqual(
                    core.effective_daily_limit(
                        "hudzaifah", weekday, limits, overrides
                    ).seconds,
                    7200,
                )
            self.assertEqual(
                core.effective_daily_limit(
                    "hudzaifah", MON, limits, overrides
                ).seconds,
                3600,
            )

    def test_missing_override_file_means_no_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=3600\n")
            limits, overrides, _ = tree.load()
            self.assertEqual(overrides, {})
            for weekday in range(7):
                self.assertEqual(
                    core.effective_daily_limit(
                        "hudzaifah", weekday, limits, overrides
                    ).seconds,
                    3600,
                )

    def test_invalid_overrides_are_rejected(self):
        cases = [
            "[hudzaifah]\nfri=0\n",
            "[hudzaifah]\nfri=-1\n",
            "[hudzaifah]\nfri=abc\n",
            "[hudzaifah]\nfunday=3600\n",
            "[hudzaifah]\nfri=3600\nfri=1800\n",
            "[hudzaifah]\nmon-fri=3600\nfri=1800\n",
            "[hudzaifah]\n[hudzaifah]\nfri=3600\n",
            "fri=3600\n",
            "[nobody_at_all]\nfri=3600\n",
            "[hudzaifah]\nnoequals\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory() as tmp:
                    tree = PolicyTree(tmp, "hudzaifah=3600\n")
                    tree.write_daily(text)
                    with self.assertRaises(core.ChildTimeError):
                        tree.load()


class ScheduleValidationTests(unittest.TestCase):
    def _expect_rejected(self, schedule, limits="hudzaifah=3600\n", daily=None):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, limits)
            tree.write_schedule(schedule)
            if daily is not None:
                tree.write_daily(daily)
            with self.assertRaises(core.ChildTimeError):
                tree.load()

    def test_invalid_weekday_expression_is_rejected(self):
        self._expect_rejected("[hudzaifah]\nfunday/09:00-10:00=1800\n")

    def test_invalid_time_range_is_rejected(self):
        self._expect_rejected("[hudzaifah]\nfri/17:00-15:00=1800\n")
        self._expect_rejected("[hudzaifah]\nfri/15:00-15:00=1800\n")
        self._expect_rejected("[hudzaifah]\nfri/25:00-26:00=1800\n")
        self._expect_rejected("[hudzaifah]\nfri/0900-1200=1800\n")

    def test_zero_or_negative_quota_is_rejected(self):
        self._expect_rejected("[hudzaifah]\nfri/09:00-12:00=0\n")
        self._expect_rejected("[hudzaifah]\nfri/09:00-12:00=-60\n")

    def test_quota_longer_than_window_is_rejected(self):
        self._expect_rejected("[hudzaifah]\nfri/09:00-10:00=7200\n")

    def test_overlapping_applicable_schedules_are_rejected(self):
        # Both apply on Friday and overlap between 16:00 and 17:00.
        self._expect_rejected(
            "[hudzaifah]\nfri/15:00-17:00=1800\nmon-fri/16:00-18:00=1800\n",
            limits="hudzaifah=7200\n",
        )

    def test_non_overlapping_on_disjoint_weekdays_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=3600\n")
            # Same clock window, different weekdays: never both applicable.
            tree.write_schedule(
                "[hudzaifah]\nmon/15:00-17:00=3600\nsat/15:00-17:00=3600\n"
            )
            _, _, schedules = tree.load()
            self.assertEqual(len(schedules["hudzaifah"]), 2)

    def test_quota_exceeding_effective_daily_limit_is_rejected(self):
        # Friday would need 7200s but the base limit is 3600s.
        self._expect_rejected(
            "[hudzaifah]\n15:00-17:00=3600\nfri/09:00-12:00=3600\n",
            limits="hudzaifah=3600\n",
        )

    def test_quota_within_weekday_override_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=3600\n")
            tree.write_schedule(
                "[hudzaifah]\n15:00-17:00=3600\nfri/09:00-12:00=3600\n"
            )
            tree.write_daily("[hudzaifah]\nfri=7200\n")
            _, _, schedules = tree.load()
            self.assertEqual(len(schedules["hudzaifah"]), 2)

    def test_duplicate_schedule_identifier_is_rejected(self):
        self._expect_rejected("[hudzaifah]\nfri/09:00-10:00=600\nfri/09:00-10:00=600\n")
        # "daily" and an omitted DAYS produce the same identifier.
        self._expect_rejected("[hudzaifah]\n09:00-10:00=600\ndaily/09:00-10:00=600\n")

    def test_unknown_user_is_rejected(self):
        self._expect_rejected("[nobody_at_all]\nfri/09:00-10:00=600\n")

    def test_structurally_invalid_lines_are_rejected(self):
        self._expect_rejected("[hudzaifah]\nnoequals\n")
        self._expect_rejected("fri/09:00-10:00=600\n")
        self._expect_rejected("[]\nfri/09:00-10:00=600\n")
        self._expect_rejected("[hudzaifah]\n[hudzaifah]\nfri/09:00-10:00=600\n")


class LegacyCompatibilityTests(unittest.TestCase):
    def test_legacy_schedule_without_weekdays_applies_every_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "azzahra=7200\n")
            # Byte-for-byte a pre-weekday v1.4 schedule file.
            tree.write_schedule("[azzahra]\n09:00-12:00=3600\n15:00-17:00=3600\n")

            _, _, schedules = tree.load()
            slots = schedules["azzahra"]

            self.assertEqual(
                [slot.slot_id for slot in slots],
                ["09:00-12:00", "15:00-17:00"],
            )
            for slot in slots:
                self.assertEqual(slot.days, core.ALL_WEEKDAYS)

            for weekday in range(7):
                self.assertEqual(
                    [
                        slot.slot_id
                        for slot in core.slots_for_weekday(slots, weekday)
                    ],
                    ["09:00-12:00", "15:00-17:00"],
                )

    def test_legacy_slot_identifiers_are_unchanged(self):
        """Identifiers are the persisted usage keys; they must not move."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "azzahra=7200\n")
            tree.write_schedule("[azzahra]\n09:00-12:00=3600\n15:00-17:00=3600\n")
            _, _, schedules = tree.load()

            # Usage persisted by an earlier release is still attributed.
            (tree.schedule_state / "azzahra.state").write_text(
                f"{core.today()}\n09:00-12:00=1200\n15:00-17:00=600\n"
            )
            usage = core.read_schedule_used(
                "azzahra",
                schedules["azzahra"],
                state_dir=tree.schedule_state,
            )
            self.assertEqual(usage, {"09:00-12:00": 1200, "15:00-17:00": 600})

    def test_user_without_schedules_keeps_plain_daily_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "ibrohim=5400\n")
            limits, overrides, schedules = tree.load()

            self.assertEqual(limits["ibrohim"], 5400)
            self.assertEqual(schedules, {})
            self.assertEqual(overrides, {})
            for weekday in range(7):
                self.assertEqual(
                    core.effective_daily_limit(
                        "ibrohim", weekday, limits, overrides
                    ).seconds,
                    5400,
                )
                self.assertEqual(core.slots_for_weekday([], weekday), [])

    def test_missing_schedule_file_preserves_legacy_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "ibrohim=5400\n")
            self.assertEqual(
                core.load_schedule_policy(
                    path=tree.schedule,
                    limits={"ibrohim": 5400},
                ),
                {},
            )

    def test_unrelated_users_are_untouched_by_another_users_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = PolicyTree(tmp, "hudzaifah=3600\nazzahra=7200\nibrohim=5400\n")
            tree.write_schedule(
                "[hudzaifah]\n15:00-17:00=3600\nfri/09:00-12:00=3600\n"
                "[azzahra]\n09:00-12:00=3600\n15:00-17:00=3600\n"
            )
            tree.write_daily("[hudzaifah]\nfri=7200\n")

            limits, overrides, schedules = tree.load()

            # Azzahra keeps her original all-day slots and identifiers.
            self.assertEqual(
                [slot.slot_id for slot in schedules["azzahra"]],
                ["09:00-12:00", "15:00-17:00"],
            )
            for slot in schedules["azzahra"]:
                self.assertEqual(slot.days, core.ALL_WEEKDAYS)

            # Ibrohim has no schedules and no overrides on any weekday.
            self.assertNotIn("ibrohim", schedules)
            self.assertNotIn("ibrohim", overrides)
            for weekday in range(7):
                self.assertEqual(
                    core.effective_daily_limit(
                        "ibrohim", weekday, limits, overrides
                    ).seconds,
                    5400,
                )


class ScheduledSlotShapeTests(unittest.TestCase):
    def test_positional_construction_still_works(self):
        slot = core.ScheduledSlot("azzahra", 540, 720, 3600, "09:00-12:00")
        self.assertEqual(slot.days, core.ALL_WEEKDAYS)
        self.assertTrue(slot.applies_on(MON))
        self.assertTrue(slot.applies_on(SUN))

    def test_weekday_scoped_slot_applies_only_on_its_days(self):
        slot = core.ScheduledSlot(
            "hudzaifah", 540, 720, 3600, "fri/09:00-12:00", frozenset({FRI})
        )
        self.assertTrue(slot.applies_on(FRI))
        self.assertFalse(slot.applies_on(SAT))

    def test_slots_for_weekday_rejects_invalid_weekday(self):
        for bad in (-1, 7, "fri", None):
            with self.subTest(bad=bad):
                with self.assertRaises(core.ChildTimeError):
                    core.slots_for_weekday([], bad)


if __name__ == "__main__":
    unittest.main()
