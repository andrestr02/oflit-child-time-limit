#!/usr/bin/env bash
#
# Back up OFLIT Child Time Limit configuration, PAM hook and usage data.
# Read-only with respect to everything it backs up: this script never
# modifies or deletes policy or state.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

STATE_DIR="/var/lib/child-time-limit"
BACKUP_DIR="${STATE_DIR}/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
STAGING="$(mktemp -d)"
ARCHIVE="${BACKUP_DIR}/child-time-${STAMP}.tar.gz"

cleanup() {
  rm -rf -- "${STAGING}"
}
trap cleanup EXIT

install -d -m 0700 "${BACKUP_DIR}"
install -d -m 0700 "${STAGING}/etc"
install -d -m 0700 "${STAGING}/state"

copied=0

for name in \
  child-time-limit.conf \
  child-time-access.conf \
  child-time-schedule.conf \
  child-time-daily.conf
do
  if [[ -e "/etc/${name}" ]]; then
    cp -a "/etc/${name}" "${STAGING}/etc/${name}"
    copied=$((copied + 1))
    echo "  config : /etc/${name}"
  else
    echo "  config : /etc/${name} (absent)"
  fi
done

if [[ -e /etc/pam.d/gdm-password ]]; then
  cp -a /etc/pam.d/gdm-password "${STAGING}/etc/gdm-password"
  echo "  pam    : /etc/pam.d/gdm-password"
fi

# Usage data: *.state plus the per-schedule state directory.
if [[ -d "${STATE_DIR}" ]]; then
  shopt -s nullglob
  for path in "${STATE_DIR}"/*.state; do
    cp -a "${path}" "${STAGING}/state/"
    echo "  usage  : ${path}"
  done
  if [[ -d "${STATE_DIR}/schedules" ]]; then
    cp -a "${STATE_DIR}/schedules" "${STAGING}/state/schedules"
    echo "  usage  : ${STATE_DIR}/schedules/"
  fi
  shopt -u nullglob
fi

{
  echo "created=${STAMP}"
  echo "host=$(hostname)"
  echo "config_files=${copied}"
  if command -v systemctl >/dev/null 2>&1; then
    echo "enforcer_active=$(systemctl is-active child-time-enforcer.service 2>/dev/null || true)"
    echo "enforcer_enabled=$(systemctl is-enabled child-time-enforcer.service 2>/dev/null || true)"
  fi
  if [[ -f /usr/local/lib/child-time-limit/child_time_core.py ]]; then
    echo "core_sha256=$(sha256sum /usr/local/lib/child-time-limit/child_time_core.py | cut -d' ' -f1)"
  fi
} > "${STAGING}/MANIFEST"

tar -czf "${ARCHIVE}" -C "${STAGING}" .
chmod 0600 "${ARCHIVE}"

echo
echo "Backup written: ${ARCHIVE}"
echo "Keep this path; the rollback procedure needs it."
