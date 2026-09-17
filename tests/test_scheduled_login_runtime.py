import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOGIN = ROOT / "src/child-time-login-check"


class ScheduledLoginRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.source = LOGIN.read_text()
        self.tree = ast.parse(self.source)

    def test_runtime_defines_check_login(self):
        functions = {
            node.name
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("check_login", functions)

    def test_runtime_defines_main(self):
        functions = {
            node.name
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("main", functions)

    def test_runtime_uses_shared_schedule_policy(self):
        self.assertIn("load_schedule_policy", self.source)

    def test_runtime_uses_shared_schedule_state_reader(self):
        self.assertIn("read_schedule_used", self.source)

    def test_runtime_uses_shared_login_evaluator(self):
        self.assertIn("evaluate_login_eligibility", self.source)

    def test_runtime_does_not_move_pam_hook(self):
        installer = (ROOT / "install.sh").read_text()

        # v1.3.1 regression invariant:
        # active hook belongs only to gdm-password.
        self.assertIn(
            'PAM_FILE="/etc/pam.d/gdm-password"',
            installer,
        )

        # common-account must remain referenced only for legacy cleanup;
        # removing this migration would reintroduce upgrade risk.
        self.assertIn(
            'LEGACY_PAM_FILE="/etc/pam.d/common-account"',
            installer,
        )
        self.assertIn(
            'sed -i "\\|^${PAM_RULE}$|d" "$LEGACY_PAM_FILE"',
            installer,
        )


if __name__ == "__main__":
    unittest.main()
