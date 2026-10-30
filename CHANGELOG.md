# Changelog

## v1.4.0 — 2026-10-30

Generic weekly scheduling and per-weekday daily quota release.

Weekly policy is fully administrator-configurable through `child-time`.
No usernames, weekdays, access windows or quotas are hardcoded in
application logic.

### Added

- **Weekly schedules.** A user may have any number of scheduled
  windows, each restricted to chosen weekdays and each carrying its own
  independent active-use quota.
- **Weekday expressions** for `--days` / `--day` and on disk: single
  days (`fri`), long names (`friday`), inclusive ranges (`mon-fri`),
  ranges that wrap past Sunday (`fri-mon`), lists (`sat,sun`) and the
  groups `daily` / `all` / `everyday` / `weekdays` / `weekend`. All
  seven weekdays are supported, resolved from the local system weekday.
- **Per-weekday daily limit overrides**, stored in a new
  `/etc/child-time-daily.conf`, so a single weekday can allow more (or
  less) total time than the base daily limit.
- **Stable schedule identifiers.** Each schedule is addressed by its
  canonical `[DAYS/]HH:MM-HH:MM` form, shown by `schedule list` and
  accepted by `schedule edit` / `schedule remove`.
- New CLI commands:
  - `child-time schedule list USER`
  - `child-time schedule add USER [--days DAYS] --start HH:MM --end HH:MM --quota DURATION`
  - `child-time schedule edit USER SCHEDULE_ID [--days|--start|--end|--quota]`
  - `child-time schedule remove USER SCHEDULE_ID`
  - `child-time set USER DURATION --day DAY`
  - `child-time limits USER` — the effective per-weekday policy
  - `child-time validate` — validate every policy file, changing nothing
- `child-time status` now reports today's applicable schedules and
  flags a weekday limit override.
- Shared core helpers used by the CLI, the PAM login gate and the
  enforcer alike: `parse_weekday_expression`, `format_weekday_set`,
  `schedule_slot_id`, `slots_for_weekday`, `load_daily_overrides`,
  `effective_daily_limit`, `load_effective_policy`,
  `weekly_policy_view`, `validate_weekly_schedule`,
  `validate_daily_override_ceiling`, `validate_policy`, and the
  `add_schedule` / `edit_schedule` / `remove_schedule` /
  `apply_daily_override_transaction` mutations.
- `config/child-time-daily.conf.example`, installed only when
  `/etc/child-time-daily.conf` does not already exist.
- Deployment tooling in `scripts/`: `child-time-backup.sh`,
  `child-time-preflight.sh`, `child-time-verify.sh` and
  `child-time-restore.sh`.
- `docs/scheduling.md` (configuration reference, examples, migration)
  and `docs/deployment.md` (staged deployment and rollback).
- 130 new deterministic tests covering the weekday engine, CLI
  mutations, atomicity under failure, genuinely concurrent processes,
  midnight and Friday-to-Saturday transitions, logout/login and service
  restart, invalid policy, legacy configurations, packaging and docs.

### Changed

- Schedule validation is now **per weekday**: two schedules conflict
  only on a day where both apply, and a day's total scheduled quota is
  checked against that day's effective daily limit.
- `child-time set USER DURATION` validates only weekdays that actually
  fall back to the base limit; a weekday carrying its own override is
  validated when that override changes.
- `status_rows` resolves the weekday once and uses it for both the
  effective limit and the applicable schedules.
- `StatusRow` gained `weekday` and `limit_source`; `ScheduledSlot`
  gained `days`. Both are appended with defaults, so existing
  positional construction is unaffected.
- The enforcer and the PAM login check resolve the day's ceiling and
  windows through the shared core instead of reading the limit directly.
- A malformed `/etc/child-time-daily.conf` is treated exactly like a
  malformed schedule file: fail-closed for configured children.

### Fixed

- The enforcer defined `ScheduledRuntimeResult` and
  `process_scheduled_interval` twice, verbatim. Removed the duplicate.
- `apply_daily_override_transaction` read usage from the production
  state directory regardless of the caller's `state_dir`.

### Compatibility

- `/etc/child-time-limit.conf` is not modified by weekly scheduling.
- Schedule entries with no weekday prefix are read as all-week
  schedules and keep their original `HH:MM-HH:MM` identifiers, so
  existing per-slot usage state stays correctly attributed.
- Users with no schedules keep plain daily-quota behaviour on all seven
  days.
- Existing usage history is never modified by the upgrade.
- All pre-existing CLI commands behave as before.
- No migration step is required.


## v1.3.1 — 2026-09-17

### Fixed

- Move the PAM login check from global `common-account` to `gdm-password`.
- Prevent `child-time-login-check` from blocking the GDM greeter environment.
- Migrate the legacy `common-account` PAM rule installed by v1.3.0 and earlier.
- Add regression coverage for safe GDM PAM integration.


All notable changes to OFLIT Child Time Limit will be documented in this file.

## [1.3.0] - 2026-09-17

Configurable access window release.

### Added

- `sudo child-time window` shows the current global access window.
- `sudo child-time window START END` (24-hour `HH:MM`) sets a new window.
- Separate access-window configuration file, `/etc/child-time-access.conf`
  (`start=HH:MM` / `end=HH:MM`), independent from the existing
  `/etc/child-time-limit.conf` quota format.
- `config/child-time-access.conf.example`, installed as the default
  `09:00`-`17:00` window on a fresh install.
- Regression coverage for CLI window get/set, concurrent atomic
  access-window updates, and access-window clock parsing.

### Changed

- Both the PAM login gate and the active-session enforcer now read the
  configured access window from `/etc/child-time-access.conf` at each
  check, falling back to the v1.2.0 default `09:00`-`17:00` when the
  file is missing or fails to parse. No second daemon or timer was
  introduced; hot reload continues to require no service restart.

### Fixed

- `child-time-enforcer` and `child-time-login-check` were missing the
  `pathlib` import their access-window parsing relied on, which would
  have raised `NameError` at process start -- crashing the enforcer
  daemon and failing every PAM login check. Both scripts now import
  `Path` correctly.
- Access-window clock parsing no longer reuses `child-time until`'s
  "must be later than now" rule. An access window is a recurring daily
  policy, so setting `start=08:00` after 08:00 has already passed today
  (for example, at noon) is valid and no longer rejected.
- `atomic_update_access_window` no longer writes through a single fixed
  temporary filename with no locking, which could let concurrent
  `child-time window` invocations race, corrupt, or fail to write the
  config. It now uses a uniquely-named temporary file plus the same
  exclusive-lock/atomic-replace/fsync strategy as daily-quota updates.
- The shipped `config/child-time-access.conf.example` and `VERSION`
  contained a literal backslash-`n` sequence instead of real newlines;
  both now contain actual line breaks.

### Preserved behavior

- Same-day windows only: `start` must be strictly earlier than `end`;
  overnight windows are intentionally unsupported.
- Daily active-use quota semantics, `/etc/child-time-limit.conf` format,
  local-date reset, state persistence, multi-session deduplication,
  quota exhaustion termination, PAM re-login denial, fail-open behavior
  for unconfigured/admin users, atomic quota updates and locking, and
  the legacy `child-time-status` command are all unchanged.
- `child-time until` continues to mean remaining active-use quota, not
  wall-clock access-window expiry.
- Installer/uninstaller safety: a fresh install creates the default
  access config; an upgrade preserves an administrator's existing
  `/etc/child-time-access.conf` and only re-applies its `0600`
  permissions.

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
