#!/usr/bin/env bash
#
# Pre-deployment dry run. Runs the test suite, then validates the LIVE
# /etc policy using the NEW core from this checkout.
#
# Writes nothing. Installs nothing. Restarts nothing.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "== OFLIT Child Time Limit preflight =="
echo "checkout: ${ROOT_DIR}"
echo

echo "-- 1/3 syntax --"
for file in \
  src/child_time_core.py \
  src/child-time \
  src/child-time-enforcer \
  src/child-time-login-check \
  src/child-time-status
do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "${file}"
  echo "  ok ${file}"
done
bash -n install.sh && echo "  ok install.sh"
bash -n uninstall.sh && echo "  ok uninstall.sh"
echo

echo "-- 2/3 test suite --"
PYTHONPATH="${ROOT_DIR}" python3 -m unittest discover -s tests -p 'test_*.py'
echo

echo "-- 3/3 live policy validation (read-only) --"
if [[ ${EUID} -ne 0 ]]; then
  echo "  SKIPPED: re-run with sudo to validate /etc policy." >&2
  echo
  echo "Preflight finished (policy validation skipped)."
  exit 0
fi

python3 - <<'PYTHON'
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

# The wrapper cd's to the checkout root before invoking this.
core_path = Path.cwd() / "src" / "child_time_core.py"

loader = importlib.machinery.SourceFileLoader("child_time_core_preflight", str(core_path))
spec = importlib.util.spec_from_loader(loader.name, loader)
core = importlib.util.module_from_spec(spec)
loader.exec_module(core)

try:
    report = core.validate_policy()
except core.ChildTimeError as exc:
    print(f"  POLICY INVALID: {exc}", file=sys.stderr)
    raise SystemExit(1)

print(f"  policy files    : OK")
print(f"  access window   : {report['access_window'][0]}-{report['access_window'][1]}")
print(f"  configured users: {len(report['users'])}")
print(f"  schedules       : {report['schedule_count']}")
print(f"  weekday limits  : {report['override_count']}")
print()

limits, overrides, schedules = core.load_effective_policy()

for username in report["users"]:
    print(f"  {username}:")
    for weekday in range(7):
        limit = core.effective_daily_limit(username, weekday, limits, overrides)
        slots = core.slots_for_weekday(schedules.get(username, []), weekday)
        detail = ", ".join(
            f"{slot.slot_id}={core.human(slot.quota_seconds)}" for slot in slots
        ) or "no schedule"
        print(
            f"    {core.WEEKDAY_NAMES[weekday]} "
            f"{core.human(limit.seconds):>8} ({limit.source:<8}) {detail}"
        )
PYTHON

echo
echo "Preflight PASSED. Nothing was changed."
echo "Next: sudo ./scripts/child-time-backup.sh, then sudo ./install.sh"
