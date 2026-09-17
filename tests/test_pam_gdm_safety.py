import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
UNINSTALLER = ROOT / "uninstall.sh"


class PamGdmSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INSTALLER.read_text()
        cls.uninstaller_text = UNINSTALLER.read_text()

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


    def test_uninstaller_removes_current_and_legacy_pam_hooks(self):
        self.assertIn(
            'PAM_FILE="/etc/pam.d/gdm-password"',
            self.uninstaller_text,
        )
        self.assertIn(
            'LEGACY_PAM_FILE="/etc/pam.d/common-account"',
            self.uninstaller_text,
        )
        self.assertIn(
            '"$PAM_FILE" "$PAM_MARKER" "$PAM_RULE"',
            self.uninstaller_text,
        )
        self.assertIn(
            'sed -i "\\|^${PAM_RULE}$|d; \\|^${PAM_MARKER}$|d" "$LEGACY_PAM_FILE"',
            self.uninstaller_text,
        )


if __name__ == "__main__":
    unittest.main()
