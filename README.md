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

## Architecture

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

Policy changes are read automatically by the enforcer. **A service restart is not required after changing a limit.**

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

**v1.1.1 maintenance**

v1.1.0 has been released and production-accepted with the unified human-friendly administrator CLI, atomic policy transactions, concurrent-update protection, reduction guards, and persistent usage preservation.

v1.1.1 is a focused maintenance patch that ensures an upgraded enforcer is explicitly restarted so the running process uses the newly installed artifact, and aligns legacy `child-time-status` usage reporting with the unified CLI when consumed usage exceeds a force-reduced limit.

See [`CHANGELOG.md`](CHANGELOG.md) for version notes.

This is still a small community project. Review the code and test it on your own distribution before using it as a safety-critical control.

## License

MIT License. See [`LICENSE`](LICENSE).

---

**OFLIT Child Time Limit** — practical Linux tooling from OFLIT.