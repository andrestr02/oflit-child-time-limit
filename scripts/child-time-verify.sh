#!/usr/bin/env bash
#
# Post-deployment health check. Read-only; exits non-zero on any failure.
set -uo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

failures=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "  PASS  ${label}"
  else
    echo "  FAIL  ${label}"
    failures=$((failures + 1))
  fi
}

echo "== OFLIT Child Time Limit post-deployment verification =="
echo

echo "-- installed files --"
for path in \
  /usr/local/lib/child-time-limit/child_time_core.py \
  /usr/local/sbin/child-time \
  /usr/local/sbin/child-time-enforcer \
  /usr/local/sbin/child-time-login-check \
  /etc/systemd/system/child-time-enforcer.service
do
  check "exists ${path}" test -f "${path}"
done
echo

echo "-- service health --"
check "enforcer enabled" systemctl is-enabled --quiet child-time-enforcer.service
check "enforcer active"  systemctl is-active  --quiet child-time-enforcer.service
echo

echo "-- PAM hook --"
check "gdm-password hook present" \
  grep -Fq "/usr/local/sbin/child-time-login-check" /etc/pam.d/gdm-password
echo

echo "-- policy file permissions --"
for name in \
  child-time-limit.conf \
  child-time-access.conf \
  child-time-schedule.conf \
  child-time-daily.conf
do
  if [[ -e "/etc/${name}" ]]; then
    mode="$(stat -c '%a' "/etc/${name}")"
    if [[ "${mode}" == "600" ]]; then
      echo "  PASS  /etc/${name} mode 0600"
    else
      echo "  FAIL  /etc/${name} mode ${mode} (expected 600)"
      failures=$((failures + 1))
    fi
  else
    echo "  SKIP  /etc/${name} (absent)"
  fi
done
echo

echo "-- policy validation --"
if child-time validate; then
  echo "  PASS  child-time validate"
else
  echo "  FAIL  child-time validate"
  failures=$((failures + 1))
fi
echo

echo "-- current usage (must match pre-deployment) --"
child-time status || failures=$((failures + 1))
echo

if [[ ${failures} -eq 0 ]]; then
  echo "VERIFICATION PASSED"
  exit 0
fi

echo "VERIFICATION FAILED: ${failures} check(s) failed." >&2
echo "Consider rolling back: see docs/deployment.md" >&2
exit 1
