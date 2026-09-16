import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CorePackagingTests(unittest.TestCase):
    def test_core_exists_in_source_tree(self):
        self.assertTrue((ROOT / "src" / "child_time_core.py").is_file())

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
