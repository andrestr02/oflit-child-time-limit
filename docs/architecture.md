# Architecture

OFLIT Child Time Limit uses two independent enforcement layers that share one persistent state format.

## Runtime accounting

`child-time-enforcer` runs as a systemd service. It polls `systemd-logind` once per second and identifies configured users with an active graphical session whose type is `wayland` or `x11` and class is `user`.

For each active configured user, elapsed monotonic time is accumulated. Usage is persisted under:

```text
/var/lib/child-time-limit/<username>.state
```

The file format is intentionally simple:

```text
YYYY-MM-DD SECONDS
```

The write is performed through a temporary file, `fsync()`, and atomic `os.replace()` before enforcement. This ordering is deliberate: if the machine reboots immediately after quota exhaustion, the consumed quota should already be durable.

## Quota enforcement

When usage reaches the configured limit, the daemon runs:

```bash
loginctl terminate-user <username>
```

This is stronger than locking the screen or switching to the display manager. The goal is to remove the user's logind session rather than leave the original desktop alive behind a lock screen.

## Login gate

The installer adds this PAM account rule to `/etc/pam.d/common-account`:

```text
account required pam_exec.so quiet /usr/local/sbin/child-time-login-check
```

The checker reads the configured quota and today's state. If the same-day usage is already at or above the quota, it exits non-zero and PAM rejects the account phase.

The checker only enforces usernames explicitly present in `/etc/child-time-limit.conf`. Unconfigured users are allowed. This reduces the risk that an administrator is accidentally locked out by an unrelated configuration error.

## Daily reset

The current local calendar date is part of the state key. A state file from yesterday does not count toward today. The daemon begins a new in-memory accumulator when the local date changes and writes a new dated state when usage resumes.

No scheduled midnight reset job is required.

## Reboot behavior

The monotonic clock is only used to measure elapsed time within one running daemon instance. Persisted usage is stored as whole seconds. After reboot, the daemon loads the persisted count and continues from there.

At most a sub-second fraction can be lost when the daemon restarts. A login/logout transition can differ by at most one polling interval.

## Threat model

This project assumes child accounts:

- are ordinary non-root users;
- do not have sudo privileges;
- cannot modify `/etc/child-time-limit.conf`;
- cannot modify `/usr/local/sbin/child-time-*`;
- cannot modify `/var/lib/child-time-limit/`;
- cannot disable or replace the systemd service.

It is not intended to defend against a user with root access, physical disk write access, recovery-console control, or the ability to boot another operating system.

## Multiple simultaneous sessions

Usage is deduplicated by username. Multiple active graphical sessions for the same configured username do not multiply the charged rate.

## Why two layers?

The daemon answers: "Should this currently running session be terminated?"

PAM answers: "Should this user be allowed to start another login after quota exhaustion?"

Using both prevents a simple logout/login or reboot workflow from restoring same-day access.
