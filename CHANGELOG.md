# Changelog

All notable changes to OFLIT Child Time Limit will be documented in this file.

## [Unreleased]

Phase 2A: privileged D-Bus administration backend (code gate only; not yet
production-deployed or production-accepted).

### Added

- `child_time_operations.py`: shared per-command (`set`/`add`/`subtract`/`until`)
  policy-calculation helpers, factored out for reuse by any future
  privileged front end.
- `child_time_backend.py`: transport-agnostic administration API wrapping
  the reusable core. Never accepts a caller-controlled config/state path.
  Classifies domain failures into a small, stable exception vocabulary.
- `child_time_polkit.py`: Polkit (`org.freedesktop.PolicyKit1`)
  authorization wrapper. Fails closed on any error or missing caller
  identity.
- `child-time-backend`: D-Bus system-bus service (`id.oflit.ChildTime1`),
  bus-activated on demand. Exposes `Status`, `SetLimit`, `AddLimit`,
  `SubtractLimit`, and `Until` with structured arguments/results and
  stable `id.oflit.ChildTime1.Error.*` error names.
- D-Bus bus policy, D-Bus service-activation file, Polkit action
  definitions, and a `Type=dbus` systemd unit for the new backend.
- Installer/uninstaller support for all of the above; upgrade-safe and
  idempotent, matching the existing installer's config/state preservation
  guarantees. The backend service is intentionally not enabled or started
  eagerly (bus-activated only).

### Tests

- 61 new automated tests: shared-operations unit tests, backend unit
  tests (including a hygiene check that no method accepts a config/state
  path), Polkit-wrapper unit tests (fail-closed paths), a real D-Bus
  integration suite (private throwaway bus, injected fake Polkit
  authority), and static installer/uninstaller packaging checks.
- All existing tests continue to pass unmodified (120/120 total).

### Notes

- The `child-time` CLI is unchanged and does not depend on the new
  backend; both share `child_time_core.py`/`child_time_operations.py`
  without either depending on the other.
- Polkit authorization granularity (`status` open to any active session,
  `manage` requiring interactive admin authentication) is a reasoned
  default pending confirmation against the project's SSOT.

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
