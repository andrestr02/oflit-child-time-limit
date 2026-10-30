"""Packaging and migration contract for weekly scheduling.

These tests read install.sh / uninstall.sh / the shipped examples as
text. They never execute the installer and never touch /etc.
"""

import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_core():
    loader = importlib.machinery.SourceFileLoader(
        "child_time_core_packaging_test",
        str(ROOT / "src" / "child_time_core.py"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


core = load_core()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.installer = (ROOT / "install.sh").read_text()

    def test_weekday_limit_config_is_installed_when_absent(self):
        self.assertIn(
            'if [[ ! -e /etc/child-time-daily.conf ]]; then',
            self.installer,
        )
        self.assertIn(
            'install -m 0600 "$ROOT_DIR/config/child-time-daily.conf.example" '
            "/etc/child-time-daily.conf",
            self.installer,
        )

    def test_existing_weekday_limit_config_is_preserved_not_overwritten(self):
        """An upgrade must never clobber a configured policy."""
        # Split on a line-anchored "fi": a bare "fi" also matches inside
        # words such as "config".
        block = self.installer.split(
            "if [[ ! -e /etc/child-time-daily.conf ]]; then"
        )[1].split("\nfi\n")[0]
        self.assertIn("else", block)
        self.assertIn("chmod 0600 /etc/child-time-daily.conf", block)

    def test_every_policy_file_is_root_only(self):
        for name in (
            "child-time-limit.conf",
            "child-time-access.conf",
            "child-time-schedule.conf",
            "child-time-daily.conf",
        ):
            with self.subTest(name=name):
                self.assertIn(f"chmod 0600 /etc/{name}", self.installer)

    def test_installer_documents_the_weekly_commands(self):
        for fragment in (
            "child-time schedule add",
            "child-time set child1 2h --day fri",
            "child-time limits child1",
            "child-time validate",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.installer)

    def test_uninstaller_preserves_policy_files(self):
        uninstaller = (ROOT / "uninstall.sh").read_text()
        for name in (
            "/etc/child-time-limit.conf",
            "/etc/child-time-schedule.conf",
            "/etc/child-time-daily.conf",
            "/var/lib/child-time-limit/",
        ):
            with self.subTest(name=name):
                self.assertIn(name, uninstaller)
        # Policy and usage data must not be deleted by the uninstaller.
        self.assertNotIn("rm -f /etc/child-time-daily.conf", uninstaller)
        self.assertNotIn("rm -rf /var/lib/child-time-limit", uninstaller)


class ShippedExampleTests(unittest.TestCase):
    def test_examples_exist(self):
        for name in (
            "child-time-limit.conf.example",
            "child-time-access.conf.example",
            "child-time-schedule.conf.example",
            "child-time-daily.conf.example",
        ):
            with self.subTest(name=name):
                self.assertTrue((ROOT / "config" / name).is_file())

    def test_example_policy_files_parse_as_empty_policy(self):
        """Shipped examples are comment-only, so a fresh install is inert."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "child-time-limit.conf"
            schedule = root / "child-time-schedule.conf"
            daily = root / "child-time-daily.conf"

            config.write_text("child1=7200\n")
            schedule.write_text(
                (ROOT / "config" / "child-time-schedule.conf.example").read_text()
            )
            daily.write_text(
                (ROOT / "config" / "child-time-daily.conf.example").read_text()
            )

            _, limits, _ = core.load_config(config)
            self.assertEqual(limits, {"child1": 7200})
            self.assertEqual(
                core.load_daily_overrides(path=daily, limits=limits), {}
            )
            self.assertEqual(
                core.load_schedule_policy(path=schedule, limits=limits), {}
            )

    def test_schedule_example_documents_the_weekday_format(self):
        text = (ROOT / "config" / "child-time-schedule.conf.example").read_text()
        for fragment in (
            "[DAYS/]HH:MM-HH:MM=SECONDS",
            "mon-fri",
            "sat,sun",
            "fri-mon",
            "daily",
            "weekend",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_daily_example_documents_the_override_format(self):
        text = (ROOT / "config" / "child-time-daily.conf.example").read_text()
        self.assertIn("DAYS=SECONDS", text)
        self.assertIn("--day", text)


class MigrationTests(unittest.TestCase):
    def test_no_migration_is_needed_for_a_legacy_install(self):
        """A pre-weekday install stays valid with no new files at all."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "child-time-limit.conf"
            schedule = root / "child-time-schedule.conf"
            daily = root / "child-time-daily.conf"

            # Exactly what a v1.4 production host already has.
            config.write_text("azzahra=7200\nibrohim=5400\n")
            schedule.write_text(
                "[azzahra]\n09:00-12:00=3600\n15:00-17:00=3600\n"
            )
            # child-time-daily.conf deliberately does not exist.
            self.assertFalse(daily.exists())

            report = core.validate_policy(
                config_path=config,
                schedule_path=schedule,
                daily_path=daily,
            )

            self.assertEqual(report["users"], ["azzahra", "ibrohim"])
            self.assertEqual(report["scheduled_users"], ["azzahra"])
            self.assertEqual(report["override_users"], [])
            self.assertEqual(report["schedule_count"], 2)
            self.assertEqual(report["override_count"], 0)

    def test_adding_a_weekday_override_does_not_rewrite_the_limit_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "child-time-limit.conf"
            original = "# keep this comment\nazzahra=7200\nibrohim=5400\n"
            config.write_text(original)

            with mock.patch.object(
                core, "validate_username", lambda *a, **k: None
            ):
                core.apply_daily_override_transaction(
                    "azzahra", 4, 9000,
                    config_path=config,
                    state_dir=root,
                )

            # The legacy limit file is byte-for-byte untouched.
            self.assertEqual(config.read_text(), original)
            self.assertTrue((root / "child-time-daily.conf").exists())


class DeploymentScriptTests(unittest.TestCase):
    SCRIPTS = (
        "child-time-backup.sh",
        "child-time-restore.sh",
        "child-time-preflight.sh",
        "child-time-verify.sh",
    )

    def script(self, name):
        return (ROOT / "scripts" / name).read_text()

    def test_scripts_exist_and_are_executable(self):
        for name in self.SCRIPTS:
            with self.subTest(name=name):
                path = ROOT / "scripts" / name
                self.assertTrue(path.is_file(), name)
                self.assertTrue(path.stat().st_mode & 0o111, name)
                self.assertTrue(
                    self.script(name).startswith("#!/usr/bin/env bash"),
                    name,
                )

    def test_privileged_scripts_require_root(self):
        for name in (
            "child-time-backup.sh",
            "child-time-restore.sh",
            "child-time-verify.sh",
        ):
            with self.subTest(name=name):
                self.assertIn("${EUID} -ne 0", self.script(name))

    def test_backup_covers_policy_pam_and_usage(self):
        text = self.script("child-time-backup.sh")
        for fragment in (
            "child-time-limit.conf",
            "child-time-access.conf",
            "child-time-schedule.conf",
            "child-time-daily.conf",
            "/etc/pam.d/gdm-password",
            "*.state",
            "schedules",
            "MANIFEST",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)

    def test_backup_never_removes_policy_or_usage(self):
        text = self.script("child-time-backup.sh")
        # The only removal is of its own staging directory.
        removals = [
            line.strip()
            for line in text.splitlines()
            if "rm " in line or "rm -" in line
        ]
        for line in removals:
            with self.subTest(line=line):
                self.assertIn("STAGING", line)

    def test_backup_archive_is_root_only(self):
        self.assertIn('chmod 0600 "${ARCHIVE}"', self.script("child-time-backup.sh"))

    def test_restore_snapshots_current_state_before_overwriting(self):
        text = self.script("child-time-restore.sh")
        self.assertIn("child-time-backup.sh", text)
        self.assertIn("Backing up current state first", text)

    def test_restore_offers_config_only_and_confirms_by_default(self):
        text = self.script("child-time-restore.sh")
        self.assertIn("--config-only", text)
        self.assertIn("CONFIG_ONLY", text)
        self.assertIn("Proceed?", text)

    def test_restore_does_not_restart_services(self):
        text = self.script("child-time-restore.sh")
        self.assertNotIn("systemctl restart child-time", text.replace(
            'echo "  sudo systemctl restart child-time-enforcer.service   '
            '# requires approval"', ""
        ))

    def test_preflight_is_read_only_and_runs_the_suite(self):
        text = self.script("child-time-preflight.sh")
        self.assertIn("unittest discover", text)
        self.assertIn("validate_policy", text)
        self.assertIn("Writes nothing", text)
        for forbidden in ("install -m", "systemctl restart", "systemctl enable"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_verify_checks_service_policy_and_permissions(self):
        text = self.script("child-time-verify.sh")
        for fragment in (
            "is-enabled",
            "is-active",
            "child-time validate",
            "child-time status",
            "child-time-login-check",
            "mode 0600",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        self.assertIn("VERIFICATION FAILED", text)

    def test_no_script_deploys_automatically(self):
        """Deployment must stay an explicit, separate administrator step."""
        for name in self.SCRIPTS:
            with self.subTest(name=name):
                text = self.script(name)
                self.assertNotIn("./install.sh\n", text.replace("echo", "#"))


class DocumentationTests(unittest.TestCase):
    def test_scheduling_reference_exists_and_covers_the_contract(self):
        text = (ROOT / "docs" / "scheduling.md").read_text()
        for fragment in (
            "[DAYS/]HH:MM-HH:MM",
            "child-time schedule add",
            "child-time schedule edit",
            "child-time schedule remove",
            "child-time schedule list",
            "child-time limits",
            "child-time validate",
            "--day",
            "mon-fri",
            "weekend",
            "fri-mon",
            "Migration",
            "Failure behaviour",
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(
                    fragment in text, f"scheduling.md missing {fragment!r}"
                )

    def test_scheduling_reference_has_all_required_examples(self):
        text = (ROOT / "docs" / "scheduling.md").read_text()
        for heading in (
            "### One daily schedule",
            "### Multiple daily schedules",
            "### Weekday-only schedule",
            "### Weekend schedule",
            "### A different Friday quota",
            "### A user without scheduled restrictions",
        ):
            with self.subTest(heading=heading):
                self.assertTrue(
                    heading in text, f"scheduling.md missing {heading!r}"
                )

    def test_deployment_guide_covers_backup_validate_rollback_verify(self):
        text = (ROOT / "docs" / "deployment.md").read_text()
        for fragment in (
            "child-time-backup.sh",
            "child-time-preflight.sh",
            "child-time-verify.sh",
            "child-time-restore.sh",
            "## Rollback",
            "never touched",
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(
                    fragment in text, f"deployment.md missing {fragment!r}"
                )

    def test_readme_documents_the_weekly_commands(self):
        text = (ROOT / "README.md").read_text()
        for fragment in (
            "child-time schedule add",
            "child-time set",
            "--day",
            "child-time limits",
            "child-time validate",
            "docs/scheduling.md",
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(
                    fragment in text, f"README.md missing {fragment!r}"
                )

    def test_cli_help_mentions_weekly_commands(self):
        cli_source = (ROOT / "src" / "child-time").read_text()
        for fragment in (
            "manage a user's weekly scheduled quotas",
            "show the effective per-weekday policy",
            "validate every policy file",
            "weekday expression",
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(
                    fragment in cli_source,
                    f"CLI help missing {fragment!r}",
                )


class CoreContractTests(unittest.TestCase):
    def test_core_exposes_the_weekly_policy_surface(self):
        for name in (
            "WEEKDAY_NAMES",
            "ALL_WEEKDAYS",
            "parse_weekday",
            "parse_weekday_expression",
            "format_weekday_set",
            "schedule_slot_id",
            "slots_for_weekday",
            "load_daily_overrides",
            "effective_daily_limit",
            "load_effective_policy",
            "weekly_policy_view",
            "validate_weekly_schedule",
            "validate_daily_override_ceiling",
            "add_schedule",
            "edit_schedule",
            "remove_schedule",
            "mutate_schedule_policy",
            "apply_daily_override_transaction",
            "validate_policy",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(core, name), name)

    def test_core_stays_free_of_presentation_and_privilege_code(self):
        """The core's stated boundaries must hold in code, not just docs.

        The module docstring names these concerns, so match real usage
        (imports and calls) rather than the words themselves.
        """
        import ast

        path = ROOT / "src" / "child_time_core.py"
        tree = ast.parse(path.read_text())

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        for forbidden in ("argparse", "subprocess", "sys"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(
                    forbidden, imported, f"core must not import {forbidden}"
                )

        # Strip docstrings, then check for privileged/presentation calls.
        code = "\n".join(
            line
            for line in path.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        body = code.split('"""', 2)[-1]
        for forbidden in ("geteuid", "systemctl", "print("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(
                    forbidden, body, f"core must not use {forbidden}"
                )

    def test_scheduling_engine_contains_no_user_specific_rules(self):
        """No real account name may appear in the engine or front ends."""
        names = ("hudzaifah", "azzahra", "ibrohim")
        for relative in (
            "src/child_time_core.py",
            "src/child-time",
            "src/child-time-enforcer",
            "src/child-time-login-check",
        ):
            text = (ROOT / relative).read_text().lower()
            for name in names:
                with self.subTest(file=relative, name=name):
                    self.assertFalse(
                        name in text,
                        f"{relative} must not mention the account {name!r}",
                    )

    def test_weekday_names_cover_all_seven_days(self):
        self.assertEqual(
            core.WEEKDAY_NAMES,
            ("mon", "tue", "wed", "thu", "fri", "sat", "sun"),
        )
        self.assertEqual(len(core.ALL_WEEKDAYS), 7)


if __name__ == "__main__":
    unittest.main()
