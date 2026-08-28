#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this uninstaller as root: sudo bash uninstall.sh" >&2
  exit 1
fi

PAM_FILE="/etc/pam.d/common-account"
PAM_MARKER="# OFLIT Child Time Limit"
PAM_RULE="account required pam_exec.so quiet /usr/local/sbin/child-time-login-check"

systemctl disable --now child-time-enforcer.service 2>/dev/null || true
rm -f /etc/systemd/system/child-time-enforcer.service
systemctl daemon-reload
systemctl reset-failed child-time-enforcer.service 2>/dev/null || true

if [[ -f "$PAM_FILE" ]]; then
  python3 - "$PAM_FILE" "$PAM_MARKER" "$PAM_RULE" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
marker = sys.argv[2]
rule = sys.argv[3]
lines = path.read_text().splitlines()
filtered = [line for line in lines if line not in (marker, rule)]
path.write_text("\n".join(filtered) + "\n")
PY
fi

rm -f /usr/local/sbin/child-time-enforcer
rm -f /usr/local/sbin/child-time-login-check
rm -f /usr/local/sbin/child-time-status

cat <<'EOF'
OFLIT Child Time Limit has been disabled and its PAM rule removed.

For safety, these are intentionally preserved:
  /etc/child-time-limit.conf
  /var/lib/child-time-limit/

After verifying that login works normally, you may delete them manually if you
no longer need the configuration, state, or PAM backups.
EOF
