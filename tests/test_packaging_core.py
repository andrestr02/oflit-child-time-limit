import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CorePackagingTests(unittest.TestCase):
    def test_core_exists_in_source_tree(self):
        self.assertTrue((ROOT / "src" / "child_time_core.py").is_file())

    def test_version_file_has_a_real_trailing_newline_not_an_escaped_literal(self):
        raw = (ROOT / "VERSION").read_bytes()
        self.assertNotIn(b"\\n", raw)
        self.assertEqual(raw, b"1.3.0\n")

    def test_installer_creates_default_access_config_on_fresh_install(self):
        installer = (ROOT / "install.sh").read_text()
        self.assertIn(
            'install -m 0600 "$ROOT_DIR/config/child-time-access.conf.example" '
            "/etc/child-time-access.conf",
            installer,
        )

    def test_installer_preserves_existing_access_config_on_upgrade(self):
        installer = (ROOT / "install.sh").read_text()
        self.assertIn("chmod 0600 /etc/child-time-access.conf", installer)

    def test_installer_creates_core_directory(self):
        installer = (ROOT / "install.sh").read_text()
        self.assertIn(
            "install -d -m 0755 /usr/local/lib/child-time-limit",
            installer,
        )

    def test_installer_installs_core(self):
        installer = (ROOT / "install.sh").read_text()
        self.assertIn(
            'install -m 0644 "$ROOT_DIR/src/child_time_core.py" '
            '/usr/local/lib/child-time-limit/child_time_core.py',
            installer,
        )

    def test_uninstaller_removes_core(self):
        uninstaller = (ROOT / "uninstall.sh").read_text()
        self.assertIn(
            "rm -f /usr/local/lib/child-time-limit/child_time_core.py",
            uninstaller,
        )

    def test_uninstaller_removes_empty_product_lib_directory(self):
        uninstaller = (ROOT / "uninstall.sh").read_text()
        self.assertIn(
            "rmdir /usr/local/lib/child-time-limit 2>/dev/null || true",
            uninstaller,
        )


if __name__ == "__main__":
    unittest.main()
