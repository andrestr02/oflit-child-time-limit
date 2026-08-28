# Changelog

All notable changes to OFLIT Child Time Limit will be documented in this file.

## [1.0.0] - 2026-08-28

Initial public release candidate.

### Added

- Native cumulative daily screen-time accounting for configured Linux users.
- Persistent daily usage state under `/var/lib/child-time-limit/`.
- GNOME graphical-session detection through `systemd-logind`.
- Hard session termination with `loginctl terminate-user` at quota exhaustion.
- PAM account gate to reject same-day re-login after quota exhaustion.
- Daily quota reset based on the local calendar date.
- Root-owned configuration and state.
- `install.sh` with PAM backup before modification.
- `uninstall.sh` that removes the OFLIT PAM rule and service while preserving state/configuration for recovery.
- `child-time-status` command showing Used, Remaining, Limit, and Status for every configured account.
- Architecture and acceptance-testing documentation.
- MIT License.

### Acceptance-tested

The initial implementation was tested on Ubuntu 26.04 with GNOME/Wayland for:

- quota exhaustion;
- actual user-session termination rather than a lock/switch screen;
- same-day re-login denial;
- persistence across logout/login;
- persistence across reboot;
- next-day reset semantics;
- persistent usage accounting.

### Compatibility

The implementation requires Python 3, systemd-logind, `loginctl`, PAM, and `pam_exec.so`. Other systemd-based distributions may work but have not yet received the same acceptance coverage.
