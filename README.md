# OFLIT Child Time Limit

A small, native Linux screen-time limiter for GNOME systems using **systemd-logind + PAM + persistent daily state**.

The project was created after testing desktop parental-control approaches that could visually return a child to the login screen without actually terminating the user session. OFLIT Child Time Limit is designed around a stricter requirement: when the daily quota is exhausted, the graphical user session is terminated and PAM rejects a new login until the local calendar date changes.

## What it does

- Tracks cumulative **active graphical-session time** per configured Linux user.
- Enforces a configurable global local-time access window for configured child accounts.
- Persists daily usage in `/var/lib/child-time-limit/`.
- Survives logout/login and reboot.
- Calls `loginctl terminate-user` when a configured user reaches the quota.
- Uses a PAM account check to reject later logins on the same day.
- Starts a fresh quota automatically when the local date changes.
- Only enforces usernames explicitly listed in the configuration file.
- Includes `child-time-status` for a simple daily usage summary.
- Includes `child-time` for human-friendly policy administration without manually calculating seconds.
- Supports **weekly schedules**: per-weekday access windows, each with its own independent active-use quota.
- Supports **per-weekday daily limits**, so (for example) Friday can allow more time than Monday.

## Access window

Configured child accounts are subject to two independent limits:

1. a cumulative daily active-use quota; and
2. a global local-time access window, **09:00 inclusive to 17:00 exclusive by default**.

A child may consume the configured daily quota at any time inside that
window. Unused quota does not permit access outside the window.

At or after the window's end, an active configured graphical session is
terminated even when daily quota remains. Before the window's start, PAM
rejects login for configured child accounts.

The access window applies only to usernames present in
`/etc/child-time-limit.conf`. Unconfigured users retain the existing
fail-open behavior.

The access window is stored separately from daily quotas, in
`/etc/child-time-access.conf`:

```ini
start=09:00
end=17:00
```

The daily-quota configuration file is unaffected and continues to
contain only `username=seconds-per-day`.

### Configure the access window

Show the current window:

```bash
sudo child-time window
```

```text
Access window: 09:00–17:00
```

Set a new window:

```bash
sudo child-time window 08:00 18:00
```

```text
Access window updated: 08:00–18:00
```

`START` and `END` use 24-hour `HH:MM` local time. Unlike `child-time
until`, these are recurring daily boundaries, not one-time deadlines --
you can configure a start time that has already passed today (for
example, setting `08:00` as the start at noon), because it takes effect
every day, not just today.

**Same-day windows only.** `START` must be strictly earlier than `END`;
overnight windows (e.g. `22:00` to `06:00`) are not supported.

If `/etc/child-time-access.conf` is missing or cannot be parsed, the
runtime falls back to the default `09:00`–`17:00` window. The change
takes effect immediately for both the PAM login gate and the running
enforcer; no service restart is required.

## Architecture

The administrator CLI is split into two layers:

- `src/child-time` provides command-line parsing and presentation.
- `src/child_time_core.py` provides shared policy operations including
  configuration parsing, state reads, locking, atomic policy updates,
  reduction guards, and status calculation.

When installed, the shared policy core is stored at
`/usr/local/lib/child-time-limit/child_time_core.py`.

Runtime enforcement remains separate:

- `child-time-enforcer` accounts active graphical usage, enforces quota
  exhaustion, and terminates configured graphical sessions outside the
  access window.
- `child-time-login-check` provides the PAM account gate for
  access-window and quota enforcement.

Both runtime components resolve policy through the **same** core
helpers the CLI uses (`load_daily_overrides`, `effective_daily_limit`,
`slots_for_weekday`), so the CLI, the login gate and the enforcer
cannot disagree about a day's rules.


```text
GNOME / graphical session
        |
        v
systemd-logind
        |
        v
child-time-enforcer daemon
        |
        +--> persistent daily state
        |
        +--> quota reached
                |
                v
        loginctl terminate-user
                |
                v
        PAM account gate blocks re-login
```

The important distinction is:

```text
lock screen != terminate session != reject re-login
```

OFLIT Child Time Limit deliberately implements the latter two.

## Requirements

Tested on Ubuntu 26.04 with GNOME/Wayland. The implementation expects:

- Python 3
- systemd
- systemd-logind / `loginctl`
- PAM with `pam_exec.so`
- GNOME graphical sessions reported by logind as `wayland` or `x11`

Other systemd-based Linux distributions may work, but should be acceptance-tested before relying on them.

## Quick install

Clone the repository and run:

```bash
git clone https://github.com/andrestr02/oflit-child-time-limit.git
cd oflit-child-time-limit
sudo bash install.sh
```

On first install, configure the child accounts in:

```text
/etc/child-time-limit.conf
```

Example:

```ini
# username=seconds-per-day
child1=7200
child2=7200
```

The low-level file format remains seconds for compatibility, but routine administration should use the `child-time` command instead of editing seconds manually.

## Manage time limits

Show all configured users:

```bash
sudo child-time status
```

Show one user:

```bash
sudo child-time status child1
```

Set a total daily limit using human-friendly durations:

```bash
sudo child-time set child1 2h25m
sudo child-time set child1 90m
sudo child-time set child1 3h
```

Add or subtract time:

```bash
sudo child-time add child1 30m
sudo child-time subtract child1 15m
```

If a reduction would put the new limit at or below time already consumed today, the CLI refuses it by default. An administrator may explicitly accept immediate exhaustion with `--force`.

Set enough remaining active-use quota to reach a local clock target:

```bash
sudo child-time until child1 10:45
```

`until` is intentionally defined in terms of **remaining active usage**. It converts the current wall-clock interval into quota. If the child logs out or becomes inactive, unused quota remains; this is not a hard wall-clock logout schedule.

Show or change the global access window (see [Access window](#access-window)):

```bash
sudo child-time window
sudo child-time window 08:00 18:00
```

Policy changes are read automatically by the enforcer. **A service restart is not required after changing a limit or the access window.**

## Weekly schedules and per-weekday limits

Beyond a single daily quota and the global access window, a user can
have a **weekly policy**: any number of scheduled windows, restricted to
chosen weekdays, each with its own independent quota, plus a different
total daily limit on chosen weekdays.

Everything is configurable through `child-time`. There are no
per-user rules in the source code, and you should not need to edit a
configuration file by hand.

```bash
sudo child-time schedule list   USER
sudo child-time schedule add    USER [--days DAYS] --start HH:MM --end HH:MM --quota DURATION
sudo child-time schedule edit   USER SCHEDULE_ID [--days DAYS] [--start HH:MM] [--end HH:MM] [--quota DURATION]
sudo child-time schedule remove USER SCHEDULE_ID

sudo child-time set    USER DURATION            # base daily limit
sudo child-time set    USER DURATION --day DAY  # one weekday only
sudo child-time limits USER                     # effective weekly policy
sudo child-time validate                        # check every policy file
```

`--days` accepts a weekday expression and defaults to every day:

| Form | Example | Means |
| --- | --- | --- |
| Single day | `fri` | Friday |
| Range | `mon-fri` | Mon–Fri |
| Wrapping range | `fri-mon` | Fri, Sat, Sun, Mon |
| List | `sat,sun` | Saturday and Sunday |
| Group | `daily` / `weekdays` / `weekend` | all / Mon–Fri / Sat–Sun |

All seven weekdays are supported, resolved against the local system
weekday.

### Example

One hour on most days, but two hours on Friday split between a morning
and an afternoon window:

```bash
sudo child-time set someuser 1h                 # base: 1 hour
sudo child-time set someuser 2h --day fri       # Friday: 2 hours
sudo child-time schedule add someuser --start 15:00 --end 17:00 --quota 1h
sudo child-time schedule add someuser --days fri --start 09:00 --end 12:00 --quota 1h
```

Inspect the result:

```bash
sudo child-time limits someuser
```

```text
Effective weekly policy for someuser
Base daily limit : 1h

DAY    DAILY LIMIT    SOURCE  SCHEDULES
------------------------------------------------------------------
mon       01:00:00      base  15:00-17:00 (1h)
tue       01:00:00      base  15:00-17:00 (1h)
wed       01:00:00      base  15:00-17:00 (1h)
thu       01:00:00      base  15:00-17:00 (1h)
fri *     02:00:00  override  fri/09:00-12:00 (1h), 15:00-17:00 (1h)
sat       01:00:00      base  15:00-17:00 (1h)
sun       01:00:00      base  15:00-17:00 (1h)
```

Monday–Thursday and the weekend allow afternoon access only, up to one
hour. Friday allows up to one hour in the morning and one hour in the
afternoon, capped at two hours for the day.

Each scheduled window has its **own** quota: unused morning time does
not roll over into the afternoon. All counters reset automatically at
local midnight.

Every schedule has a stable identifier (`15:00-17:00`,
`fri/09:00-12:00`) shown by `schedule list` and used by `schedule edit`
and `schedule remove`.

### Users without schedules

A user with no schedules keeps plain daily-quota behaviour: any time
inside the global access window until the quota is consumed. This is
what every existing user keeps after an upgrade — see
[Backward compatibility](#backward-compatibility).

### Validation

Mutations are validated before anything is written, under an exclusive
lock, and written atomically. A rejected command leaves every
configuration file byte-for-byte unchanged, so it is always safe to
retry after fixing the input.

```bash
sudo child-time validate
```

A non-zero exit means the policy is not safe to activate. For a
configured child, an invalid schedule is **fail-closed**: it never
grants unrestricted access.

Full reference, including the on-disk format, every validation rule and
more examples: [`docs/scheduling.md`](docs/scheduling.md).

## Backward compatibility

Weekly scheduling is additive. After upgrading:

- `/etc/child-time-limit.conf` is **not** modified; per-weekday
  overrides live in a separate `/etc/child-time-daily.conf`.
- Users with no schedules keep plain daily-quota behaviour on all seven
  days.
- Existing schedule entries with no weekday prefix are read as all-week
  schedules and **keep their original identifiers**, so existing
  per-schedule usage state stays correctly attributed.
- Existing usage history under `/var/lib/child-time-limit/` is never
  modified by the upgrade.
- Every pre-existing CLI command (`status`, `set`, `add`, `subtract`,
  `window`, `until`) behaves as before.

No migration step is required. Confirm with `sudo child-time validate`,
`sudo child-time status` and `sudo child-time limits USER`.

## Check usage

The legacy read-only command remains available:

```bash
sudo child-time-status
```

Example output:

```text
OFLIT Child Time Limit — 2026-08-28

USER                       USED  REMAINING      LIMIT     STATUS
------------------------------------------------------------------
child1                 00:03:30   01:56:30   02:00:00  AVAILABLE
child2                 02:00:00   00:00:00   02:00:00  EXHAUSTED
```

The raw state remains available under `/var/lib/child-time-limit/`. A state file looks like:

```text
2026-08-28 210
```

which means 210 seconds have been charged on that local calendar date.

## Security model

Configuration and state are root-owned. Child accounts should be ordinary non-sudo users.

The login checker is intentionally **fail-open for users not listed in the policy**. A malformed configuration therefore should not accidentally lock out an administrator account.

The management CLI requires root. Config updates are written atomically, preserve ownership and permissions, reject unknown/unconfigured users, and protect reductions below already-consumed time unless `--force` is explicitly supplied.

Before deployment, make sure at least one administrator/root recovery path remains available.

## Interaction with other parental-control software

Do not run multiple session-time enforcement systems at the same time unless you understand how they interact. Another daemon may lock, terminate, or account for sessions independently and make troubleshooting ambiguous.

If you currently use Malcontent or another session-limit system, disable its time-enforcement path before accepting OFLIT Child Time Limit as the source of truth.

## Acceptance testing

Do not call a deployment successful merely because the service starts. Verify end-to-end behavior.

Recommended acceptance sequence:

1. Temporarily configure a test account for `120` seconds.
2. Log in and use the account until quota exhaustion.
3. Confirm the session is actually gone from `loginctl list-sessions`.
4. Confirm the state file records the exhausted quota.
5. Confirm the PAM helper returns denial for that user.
6. Attempt a real login and confirm it is rejected.
7. Reboot and confirm the same-day denial persists.
8. Simulate or wait for the next calendar day and confirm a new daily quota starts.
9. Verify `child-time set/add/subtract/until` without restarting the service.
10. Set a temporary access window and confirm login is rejected before its start.
11. Confirm an active configured graphical session is terminated when the window ends.
12. Change the access window with `child-time window START END` and confirm the new policy takes effect without restarting the service.
13. Add a weekday-scoped schedule and confirm login is refused outside its window on that weekday.
14. Confirm a weekday with no applicable schedule refuses login all day.
15. Set a per-weekday limit with `child-time set USER DURATION --day DAY` and confirm `child-time limits USER` reports it.
16. Confirm each scheduled window is accounted independently, and that logout/login and a service restart do not restore consumed time.

See [`docs/testing.md`](docs/testing.md) for commands and expected results.

## Development tests

The CLI parser and atomic policy update behavior have standard-library unit tests:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'
```

Some test modules import the shared core as `src.child_time_core`, so
the repository root must be on `PYTHONPATH`. Every test builds its own
policy and state tree in a temporary directory; the suite never reads
or writes `/etc` or `/var/lib/child-time-limit`.

## Deployment

For a production host, use the staged procedure in
[`docs/deployment.md`](docs/deployment.md): back up, preflight, install,
verify, then configure policy. Helper scripts live in `scripts/`:

```bash
sudo ./scripts/child-time-backup.sh      # archive policy, PAM hook and usage data
sudo ./scripts/child-time-preflight.sh   # run tests + validate live policy, read-only
sudo ./install.sh                        # install (restarts the enforcer)
sudo ./scripts/child-time-verify.sh      # post-deployment health check
sudo ./scripts/child-time-restore.sh ARCHIVE [--config-only]   # rollback
```

## Uninstall

```bash
sudo bash uninstall.sh
```

The uninstaller removes the service, helper commands, and OFLIT PAM integration. It cleans the current `gdm-password` rule as well as legacy `common-account` remnants. It does not delete usage state or configuration unless you explicitly choose to remove them afterward.

## Project status

**v1.4.0**

v1.4.0 adds generic, administrator-configurable weekly scheduling: any
number of scheduled windows per user, restricted to chosen weekdays,
each with its own independent active-use quota, plus per-weekday daily
limit overrides. All of it is managed through `child-time schedule`,
`child-time set --day`, `child-time limits` and `child-time validate` —
no source changes and no manual configuration editing.

Weekly policy is additive and backward compatible: users without
schedules keep plain daily-quota behaviour, existing schedule entries
keep their identifiers and their recorded usage, and no migration step
is required. See [`docs/scheduling.md`](docs/scheduling.md).

v1.3.1 fixes the GDM PAM integration used by access-window enforcement.
The child-time login check is attached to `gdm-password` instead of the
global `common-account` stack, preventing it from blocking the GDM
greeter environment.

The installer migrates the legacy global PAM rule, and the uninstaller
cleans both the current `gdm-password` integration and legacy
`common-account` remnants.

v1.3.0 introduced the administrator-configurable global access window
through `sudo child-time window`, stored in
`/etc/child-time-access.conf` and observed immediately by both the PAM
login gate and the running enforcer.

v1.2.0 introduced the global access window itself and
`child_time_core.py` as the shared policy core. v1.1.0 introduced the
unified human-friendly administrator CLI, atomic policy transactions,
concurrent-update protection, reduction guards, and persistent usage
preservation. v1.1.1 was a focused maintenance patch that ensures an
upgraded enforcer is explicitly restarted so the running process uses
the newly installed artifact, and aligns legacy `child-time-status`
usage reporting with the unified CLI when consumed usage exceeds a
force-reduced limit.

See [`CHANGELOG.md`](CHANGELOG.md) for version notes.

This is still a small community project. Review the code and test it on
your own distribution before using it as a safety-critical control.

## License

MIT License. See [`LICENSE`](LICENSE).

---

**OFLIT Child Time Limit** — practical Linux tooling from OFLIT.