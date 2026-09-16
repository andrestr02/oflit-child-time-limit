# OFLIT Child Time Limit

A small, native Linux screen-time limiter for GNOME systems using **systemd-logind + PAM + persistent daily state**.

The project was created after testing desktop parental-control approaches that could visually return a child to the login screen without actually terminating the user session. OFLIT Child Time Limit is designed around a stricter requirement: when the daily quota is exhausted, the graphical user session is terminated and PAM rejects a new login until the local calendar date changes.

## What it does

- Tracks cumulative **active graphical-session time** per configured Linux user.
- Persists daily usage in `/var/lib/child-time-limit/`.
- Survives logout/login and reboot.
- Calls `loginctl terminate-user` when a configured user reaches the quota.
- Uses a PAM account check to reject later logins on the same day.
- Starts a fresh quota automatically when the local date changes.
- Only enforces usernames explicitly listed in the configuration file.
- Includes `child-time-status` for a simple daily usage summary.
- Includes `child-time` for human-friendly policy administration without manually calculating seconds.

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

See [`docs/testing.md`](docs/testing.md) for commands and expected results.

## Development tests

The CLI parser and atomic policy update behavior have standard-library unit tests:

```bash
python3 -m unittest tests/test_child_time_cli.py
```

## Uninstall

```bash
sudo bash uninstall.sh
```

The uninstaller removes the service, helper commands, and OFLIT PAM line. It does not delete usage state or configuration unless you explicitly choose to remove them afterward.

## Project status

**v1.3.0**

v1.3.0 makes the global access window administrator-configurable through
`sudo child-time window`, stored in `/etc/child-time-access.conf` and
observed immediately by both the PAM login gate and the running
enforcer, with same-day-only windows and a `09:00`–`17:00` fallback
default.

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

This is still a small community project. Review the code and test it on your own distribution before using it as a safety-critical control.

## License

MIT License. See [`LICENSE`](LICENSE).

---

**OFLIT Child Time Limit** — practical Linux tooling from OFLIT.