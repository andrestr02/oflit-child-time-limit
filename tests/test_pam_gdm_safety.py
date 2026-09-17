import unittest
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


class PamGdmSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INSTALLER.read_text()

    def test_child_time_pam_hook_targets_gdm_password(self):
        self.assertIn(
            'PAM_FILE="/etc/pam.d/gdm-password"',
            self.text,
        )
        assignments = [
            line.strip()
            for line in self.text.splitlines()
            if line.startswith("PAM_FILE=")
        ]
        self.assertEqual(
            assignments,
            ['PAM_FILE="/etc/pam.d/gdm-password"'],
        )

    def test_legacy_common_account_hook_is_migrated(self):
        self.assertIn(
            'LEGACY_PAM_FILE="/etc/pam.d/common-account"',
            self.text,
        )
        self.assertIn(
            'grep -Fqx "$PAM_RULE" "$LEGACY_PAM_FILE"',
            self.text,
        )
        self.assertIn(
            'sed -i "\\|^${PAM_RULE}$|d" "$LEGACY_PAM_FILE"',
            self.text,
        )

    def test_installer_adds_rule_only_to_target_pam_file(self):
        self.assertIn(
            'grep -Fqx "$PAM_RULE" "$PAM_FILE"',
            self.text,
        )
        self.assertIn('echo "$PAM_RULE"', self.text)
        self.assertIn('} >> "$PAM_FILE"', self.text)


if __name__ == "__main__":
    unittest.main()
