#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root: sudo ./install.sh" >&2
  exit 1
fi

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PAM_FILE="/etc/pam.d/common-account"
PAM_MARKER="# OFLIT Child Time Limit"
PAM_RULE="account required pam_exec.so quiet /usr/local/sbin/child-time-login-check"
BACKUP_DIR="/var/lib/child-time-limit/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"

for command in python3 loginctl systemctl install grep; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Missing required command: $command" >&2
    exit 1
  }
done

if [[ ! -f "$PAM_FILE" ]]; then
  echo "Required PAM file not found: $PAM_FILE" >&2
  exit 1
fi

if ! find /usr/lib /lib -type f -name pam_exec.so -print -quit 2>/dev/null | grep -q .; then
  echo "pam_exec.so was not found. Refusing to modify PAM." >&2
  exit 1
fi

install -d -m 0700 /var/lib/child-time-limit
install -d -m 0700 "$BACKUP_DIR"
cp -a "$PAM_FILE" "$BACKUP_DIR/common-account.$STAMP"

install -d -m 0755 /usr/local/lib/child-time-limit
install -m 0644 "$ROOT_DIR/src/child_time_core.py" /usr/local/lib/child-time-limit/child_time_core.py
install -m 0644 "$ROOT_DIR/src/child_time_operations.py" /usr/local/lib/child-time-limit/child_time_operations.py
install -m 0644 "$ROOT_DIR/src/child_time_backend.py" /usr/local/lib/child-time-limit/child_time_backend.py
install -m 0644 "$ROOT_DIR/src/child_time_polkit.py" /usr/local/lib/child-time-limit/child_time_polkit.py

install -m 0755 "$ROOT_DIR/src/child-time-enforcer" /usr/local/sbin/child-time-enforcer
install -m 0755 "$ROOT_DIR/src/child-time-login-check" /usr/local/sbin/child-time-login-check
install -m 0755 "$ROOT_DIR/src/child-time-status" /usr/local/sbin/child-time-status
install -m 0755 "$ROOT_DIR/src/child-time" /usr/local/sbin/child-time
install -m 0755 "$ROOT_DIR/src/child-time-backend" /usr/local/sbin/child-time-backend
install -m 0644 "$ROOT_DIR/systemd/child-time-enforcer.service" /etc/systemd/system/child-time-enforcer.service
install -m 0644 "$ROOT_DIR/systemd/child-time-backend.service" /etc/systemd/system/child-time-backend.service

install -d -m 0755 /usr/share/dbus-1/system.d
install -m 0644 "$ROOT_DIR/dbus/id.oflit.ChildTime1.conf" /usr/share/dbus-1/system.d/id.oflit.ChildTime1.conf
install -d -m 0755 /usr/share/dbus-1/system-services
install -m 0644 "$ROOT_DIR/dbus/id.oflit.ChildTime1.service" /usr/share/dbus-1/system-services/id.oflit.ChildTime1.service
install -d -m 0755 /usr/share/polkit-1/actions
install -m 0644 "$ROOT_DIR/polkit/id.oflit.ChildTime1.policy" /usr/share/polkit-1/actions/id.oflit.ChildTime1.policy

if [[ ! -e /etc/child-time-limit.conf ]]; then
  install -m 0600 "$ROOT_DIR/config/child-time-limit.conf.example" /etc/child-time-limit.conf
else
  chmod 0600 /etc/child-time-limit.conf
fi

if ! grep -Fqx "$PAM_RULE" "$PAM_FILE"; then
  {
    echo
    echo "$PAM_MARKER"
    echo "$PAM_RULE"
  } >> "$PAM_FILE"
fi

systemctl daemon-reload
systemctl enable child-time-enforcer.service
systemctl restart child-time-enforcer.service

if ! systemctl is-active --quiet child-time-enforcer.service; then
  echo "Enforcer failed to start. Restoring PAM backup." >&2
  cp -a "$BACKUP_DIR/common-account.$STAMP" "$PAM_FILE"
  systemctl disable --now child-time-enforcer.service 2>/dev/null || true
  exit 1
fi

# child-time-backend.service is D-Bus bus-activated (see
# /usr/share/dbus-1/system-services/id.oflit.ChildTime1.service); it is
# intentionally NOT enabled or started here. The system D-Bus daemon
# starts it on first call to id.oflit.ChildTime1 and it exits when idle.
# Reloading dbus/polkit lets an already-running daemon pick up the new
# bus policy and action definitions without waiting for its own file
# watch; failure to reload is non-fatal since both daemons also detect
# these files on their own.
systemctl reload dbus.service 2>/dev/null || true

cat <<'EOF'

OFLIT Child Time Limit installed.

NEXT STEPS:
1. Edit /etc/child-time-limit.conf once to replace the example account.
2. Keep at least one administrator account out of that file.
3. Manage configured limits with the human-friendly CLI, for example:
     sudo child-time status
     sudo child-time set child1 2h
     sudo child-time add child1 30m
4. Policy changes are reloaded automatically; a service restart is not required.
5. Perform the 120-second acceptance test documented in docs/testing.md.

The legacy read-only command `sudo child-time-status` remains installed for
compatibility.

If another parental-control time daemon is installed, disable its time-enforcement
path before treating OFLIT Child Time Limit as the source of truth.

A privileged D-Bus administration backend (id.oflit.ChildTime1) is now
installed alongside the CLI, for future GUI/remote-management front ends.
It is D-Bus-activated on demand and idle otherwise; it is not required for
the CLI, which continues to work exactly as before.
EOF
