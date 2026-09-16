# Changelog

All notable changes to OFLIT Child Time Limit will be documented in this file.

## [1.2.0] - 2026-09-17

Access-window and policy-core release.

### Added

- Global access window for all configured child accounts: `09:00` inclusive to `17:00` exclusive, using local system time.
- PAM login denial for configured child accounts outside the access window.
- Active graphical-session termination outside the access window.
- `child_time_core.py` as the shared policy core for administrator CLI operations.
- Installer and uninstaller support for `/usr/local/lib/child-time-limit/child_time_core.py`.
- Regression coverage for the policy core, CLI adapter, packaging, and access-window contract.

### Changed

- `child-time` now acts as a thin CLI adapter over the shared policy core.
- Access-window enforcement is additive to the existing cumulative daily active-use quota.
- The enforcer persists consumed active-use time and applies quota enforcement before access-window termination.

### Preserved behavior

- Daily quotas remain cumulative active-use quotas, not wall-clock quotas.
- `child-time until` retains its active-use semantics and does not become a wall-clock expiry mechanism.
- Unconfigured users retain the existing fail-open behavior.
- Daily state, local-date reset, multi-session deduplication, quota termination, PAM re-login denial, hot reload, atomic updates, locking, and `--force` semantics remain intact.
- Configuration remains `username=seconds-per-day`.
- Legacy `child-time-status` remains available.

### Acceptance-tested

Production and release reconciliation on `oflitlab-i3` verified:

- 38/38 automated tests passed.
- Access-window boundary tests passed for both runtime paths.
- Validated access-window artifacts matched the release worktree.
- Reconciled CLI and policy core matched the production baseline.
- PAM preserves unconfigured-user fail-open behavior and checks the access window before the quota gate.
- Enforcer ordering is `charge -> cache -> persist -> quota -> access-window -> previous_active`.
- Live PAM acceptance denied configured children outside the window while allowing an unconfigured administrator account.

## [1.1.1] - 2026-09-05

Maintenance patch following the v1.1.0 production release.

### Fixed

- Restart the enforcer explicitly during installation or upgrade so an already-running service uses the newly installed executable.
- Preserve actual consumed usage in the legacy `child-time-status` output when usage is greater than a force-reduced daily limit.
- Align repository version metadata with the published release series.

### Tests

- Added regression coverage for the installer restart requirement.
- Added regression coverage for legacy status reporting when consumed usage exceeds the configured limit.

## [1.1.0] - 2026-09-05

Administrator CLI release.

### Added

- Unified `child-time` administrator CLI.
- Human-friendly durations such as `30m`, `2h25m`, and `3h`.
- `status`, `set`, `add`, `subtract`, and `until` commands.
- Atomic policy updates protected by file locking.
- Reduction guard requiring explicit `--force` when lowering a limit below already-consumed usage.
- Concurrent policy update protection.
- Automated CLI and policy transaction tests.

### Changed

- Policy changes are reloaded by the running enforcer without requiring a service restart.
- Recorded daily usage is preserved independently from policy limit changes.
- Installer and uninstaller now manage the unified `child-time` CLI.
- The legacy `child-time-status` command remains available for compatibility.

### Acceptance-tested

Production acceptance on `oflitlab-i3` verified:

- 22/22 automated tests passed;
- installed source artifacts matched the reviewed source;
- policy add/subtract operations hot-reloaded without restarting the enforcer;
- configuration was restored byte-for-byte after acceptance;
- usage state remained unchanged by policy operations;
- PAM integration remained present exactly once;
- the enforcer remained active throughout policy mutation testing.

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
