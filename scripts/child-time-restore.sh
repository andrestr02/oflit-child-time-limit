#!/usr/bin/env bash
#
# Restore OFLIT Child Time Limit configuration (and optionally usage
# data) from an archive created by child-time-backup.sh.
#
# The current state is backed up first, so a restore is reversible.
# This script never restarts a service.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: child-time-restore.sh ARCHIVE [--config-only] [--yes]

  ARCHIVE        archive created by child-time-backup.sh
  --config-only  restore /etc policy files only; leave usage data as-is
  --yes          do not prompt for confirmation

Restoring usage data can give back time already consumed today.
Prefer --config-only unless you intend to restore usage too.
USAGE
  exit 2
}

[[ $# -ge 1 ]] || usage

ARCHIVE=""
CONFIG_ONLY=0
ASSUME_YES=0

for argument in "$@"; do
  case "${argument}" in
    --config-only) CONFIG_ONLY=1 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) usage ;;
    -*) echo "Unknown option: ${argument}" >&2; usage ;;
    *)
      if [[ -n "${ARCHIVE}" ]]; then
        echo "Only one archive may be given." >&2
        usage
      fi
      ARCHIVE="${argument}"
      ;;
  esac
done

[[ -n "${ARCHIVE}" ]] || usage

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0 ..." >&2
  exit 1
fi

if [[ ! -f "${ARCHIVE}" ]]; then
  echo "Archive not found: ${ARCHIVE}" >&2
  exit 1
fi

STATE_DIR="/var/lib/child-time-limit"
STAGING="$(mktemp -d)"
cleanup() { rm -rf -- "${STAGING}"; }
trap cleanup EXIT

tar -xzf "${ARCHIVE}" -C "${STAGING}"

if [[ ! -f "${STAGING}/MANIFEST" ]]; then
  echo "Archive does not look like a child-time backup (no MANIFEST)." >&2
  exit 1
fi

echo "Archive manifest:"
sed 's/^/  /' "${STAGING}/MANIFEST"
echo

echo "Will restore:"
for name in \
  child-time-limit.conf \
  child-time-access.conf \
  child-time-schedule.conf \
  child-time-daily.conf
do
  [[ -e "${STAGING}/etc/${name}" ]] && echo "  /etc/${name}"
done
[[ -e "${STAGING}/etc/gdm-password" ]] && echo "  /etc/pam.d/gdm-password"

if [[ ${CONFIG_ONLY} -eq 1 ]]; then
  echo "  (usage data: left unchanged)"
else
  echo "  ${STATE_DIR}/ usage data  <-- consumed time will be overwritten"
fi
echo

if [[ ${ASSUME_YES} -ne 1 ]]; then
  read -r -p "Proceed? [y/N] " reply
  case "${reply}" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

# Safety net: snapshot the current state before overwriting it.
echo "Backing up current state first..."
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"${SELF_DIR}/child-time-backup.sh" >/dev/null
echo "  done (see ${STATE_DIR}/backups/)"

for name in \
  child-time-limit.conf \
  child-time-access.conf \
  child-time-schedule.conf \
  child-time-daily.conf
do
  if [[ -e "${STAGING}/etc/${name}" ]]; then
    install -m 0600 -o root -g root "${STAGING}/etc/${name}" "/etc/${name}"
    echo "restored /etc/${name}"
  fi
done

if [[ -e "${STAGING}/etc/gdm-password" ]]; then
  install -m 0644 -o root -g root \
    "${STAGING}/etc/gdm-password" /etc/pam.d/gdm-password
  echo "restored /etc/pam.d/gdm-password"
fi

if [[ ${CONFIG_ONLY} -ne 1 ]]; then
  install -d -m 0700 "${STATE_DIR}"
  install -d -m 0700 "${STATE_DIR}/schedules"
  shopt -s nullglob
  for path in "${STAGING}"/state/*.state; do
    install -m 0600 -o root -g root "${path}" "${STATE_DIR}/"
    echo "restored ${STATE_DIR}/$(basename "${path}")"
  done
  for path in "${STAGING}"/state/schedules/*.state; do
    install -m 0600 -o root -g root "${path}" "${STATE_DIR}/schedules/"
    echo "restored ${STATE_DIR}/schedules/$(basename "${path}")"
  done
  shopt -u nullglob
fi

echo
echo "Restore complete. Now validate, then decide about the service:"
echo "  sudo child-time validate"
echo "  sudo systemctl restart child-time-enforcer.service   # requires approval"
