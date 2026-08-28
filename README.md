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

Then edit:

```text
/etc/child-time-limit.conf
```

Example:

```ini
# username=seconds-per-day
child1=7200
child2=7200
```

`7200` seconds = 2 hours.

Restart after changing the policy:

```bash
sudo systemctl restart child-time-enforcer.service
```

## Check usage

```bash
sudo find /var/lib/child-time-limit \
  -maxdepth 1 \
  -type f \
  -name '*.state' \
  -exec sh -c 'printf "%s: " "$1"; cat "$1"' _ {} \;
```

A state file looks like:

```text
2026-08-28 210
```

which means 210 seconds have been charged on that local calendar date.

## Security model

Configuration and state are root-owned. Child accounts should be ordinary non-sudo users.

The login checker is intentionally **fail-open for users not listed in the policy**. A malformed configuration therefore should not accidentally lock out an administrator account.

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
4. Confirm the state file records `120`.
5. Confirm the PAM helper returns denial for that user.
6. Attempt a real login and confirm it is rejected.
7. Reboot and confirm the same-day denial persists.
8. Simulate or wait for the next calendar day and confirm a new daily quota starts.

See [`docs/testing.md`](docs/testing.md) for commands and expected results.

## Uninstall

```bash
sudo bash uninstall.sh
```

The uninstaller removes the service and OFLIT PAM line. It does not delete usage state or configuration unless you explicitly choose to remove them afterward.

## Project status

The core behavior has been acceptance-tested for:

- quota exhaustion;
- true session termination;
- same-day PAM re-login denial;
- persistence across reboot;
- daily reset semantics;
- persistent usage accounting.

This is still a small community project. Review the code and test it on your own distribution before using it as a safety-critical control.

## License

MIT License. See [`LICENSE`](LICENSE).

---

**OFLIT Child Time Limit** — practical Linux tooling from OFLIT.