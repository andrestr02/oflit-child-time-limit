# Weekly schedules and per-weekday limits

This is the configuration reference for the weekly scheduling engine.

Everything described here is configurable with the `child-time` CLI.
There are no per-user rules in the source code, and an administrator
should never need to edit a configuration file by hand.

## Concepts

The policy for a given user on a given day is built from three layers:

| Layer | File | Managed with |
| --- | --- | --- |
| Base daily limit | `/etc/child-time-limit.conf` | `child-time set USER DURATION` |
| Per-weekday daily limit override | `/etc/child-time-daily.conf` | `child-time set USER DURATION --day DAY` |
| Weekly schedules | `/etc/child-time-schedule.conf` | `child-time schedule ...` |
| Global access window | `/etc/child-time-access.conf` | `child-time window START END` |

Resolution for one user on one weekday:

1. **Effective daily limit** — the weekday's override if one exists,
   otherwise the base daily limit.
2. **Applicable schedules** — every schedule whose days include that
   weekday, in start-time order.
3. If the user has no applicable schedules, only the effective daily
   limit and the global access window apply. This is exactly the
   behaviour of releases before weekly scheduling.
4. If the user has applicable schedules, time is only usable inside
   those windows, each window has its own independent quota, and the
   sum charged across the day is still capped by the effective daily
   limit.

The global access window is the outer guard and is applied to every
configured user regardless of schedules.

## Weekday expressions

A weekday expression is used by `--days`, by `--day`, and on disk.
Case is ignored and whitespace is allowed.

```text
expression := term ("," term)*
term       := group | day | day "-" day
```

| Form | Example | Means |
| --- | --- | --- |
| Single day | `fri` | Friday |
| Long name | `friday` | Friday |
| Inclusive range | `mon-fri` | Mon, Tue, Wed, Thu, Fri |
| Wrapping range | `fri-mon` | Fri, Sat, Sun, Mon |
| List | `sat,sun` | Saturday and Sunday |
| Mixed | `mon-wed,fri` | Mon, Tue, Wed, Fri |
| Group | `daily`, `all`, `everyday` | all seven days |
| Group | `weekdays`, `weekday` | `mon-fri` |
| Group | `weekend`, `weekends` | `sat,sun` |

All seven weekdays are supported. A range walks forward through
Mon→Sun and wraps past Sunday, so `fri-mon` is four days, not an error.

Weekdays are resolved against the **local system weekday**, the same
clock the daily usage reset and the access window already use.

## Schedule identifiers

Every schedule has a stable identifier, shown by
`child-time schedule list` and accepted by `schedule edit` and
`schedule remove`. The identifier is the schedule's canonical form:

```text
HH:MM-HH:MM            # applies every day
DAYS/HH:MM-HH:MM       # applies on the listed weekdays
```

Examples: `15:00-17:00`, `fri/09:00-12:00`, `mon-fri/08:00-09:00`.

Two consequences worth knowing:

- An all-week schedule keeps the plain `HH:MM-HH:MM` identifier, which
  is the same identifier earlier releases used. Existing per-slot usage
  state therefore keeps being attributed correctly after an upgrade.
- The identifier encodes the schedule's days and window. Changing the
  **quota** keeps the identifier, so today's usage is preserved.
  Changing the **days or the window** produces a new identifier, and
  that schedule's usage for today restarts from zero. `schedule edit`
  prints the old and new identifier when this happens.

## File format

### `/etc/child-time-schedule.conf`

```ini
[someuser]
15:00-17:00=3600
fri/09:00-12:00=3600
mon-fri/08:00-09:00=1800
```

- One section per user; the user must also exist in
  `/etc/child-time-limit.conf`.
- Each entry is `[DAYS/]HH:MM-HH:MM=QUOTA`.
- `QUOTA` is active-use seconds. A duration such as `1h30m` is also
  accepted for hand-edited files; the CLI always writes seconds.
- Omitting `DAYS/` means every day.
- Comments (`#` or `;`) are allowed. Comments above the first section
  are preserved by the CLI; comments inside a section are not.

### `/etc/child-time-daily.conf`

```ini
[someuser]
fri=7200
weekend=5400
```

- One section per user, one line per weekday expression.
- Each weekday selected by the expression takes that value as the
  whole day's limit.
- A missing file means no overrides, which is the pre-weekly behaviour.

## Validation rules

A policy is rejected — with nothing written — when:

- a weekday expression is invalid or selects no days;
- a time is not `HH:MM`, or is outside `00:00`–`23:59`;
- a schedule's start is not earlier than its end (overnight schedules
  are not supported);
- a quota is zero or negative;
- a quota is longer than its own window;
- two schedules that apply on the **same weekday** overlap in time
  (the same window on different weekdays is fine);
- the total quota of one weekday's schedules exceeds that weekday's
  effective daily limit;
- a daily limit or override would fall below that weekday's scheduled
  total;
- a schedule identifier is duplicated for one user;
- a section names a user that is not configured in
  `/etc/child-time-limit.conf`;
- a weekday is listed twice for one user in the override file.

Run `child-time validate` at any time to check every file without
changing anything. A non-zero exit means the policy is not safe to
activate.

Invalid policy never widens access: see
[Failure behaviour](#failure-behaviour).

## Commands

```bash
child-time schedule list   USER
child-time schedule add    USER [--days DAYS] --start HH:MM --end HH:MM --quota DURATION
child-time schedule edit   USER SCHEDULE_ID [--days DAYS] [--start HH:MM] [--end HH:MM] [--quota DURATION]
child-time schedule remove USER SCHEDULE_ID

child-time set    USER DURATION            # base daily limit
child-time set    USER DURATION --day DAY  # one weekday only
child-time limits USER                     # effective weekly policy
child-time validate                        # check every policy file
child-time status [USER]                   # today's usage and schedule
```

`--days` defaults to every day when omitted. `DURATION` accepts the
existing forms: `2h`, `90m`, `1h30m`, `45s`.

All mutations take an exclusive lock on the policy, re-read every file
under that lock, validate the result, and only then replace the file
atomically. A failed validation leaves every file byte-for-byte
unchanged, and concurrent `child-time` invocations serialise safely.

Policy changes are picked up automatically. **A service restart is not
required.**

## Examples

### One daily schedule

Afternoons only, one hour of actual use, every day:

```bash
sudo child-time set someuser 1h
sudo child-time schedule add someuser --start 15:00 --end 17:00 --quota 1h
```

### Multiple daily schedules

A morning and an afternoon window, each with its own independent hour,
two hours total per day:

```bash
sudo child-time set someuser 2h
sudo child-time schedule add someuser --start 09:00 --end 11:00 --quota 1h
sudo child-time schedule add someuser --start 15:00 --end 17:00 --quota 1h
```

Unused morning quota does **not** roll over into the afternoon.

### Weekday-only schedule

School days only; no computer at the weekend:

```bash
sudo child-time set someuser 1h
sudo child-time schedule add someuser --days mon-fri --start 16:00 --end 18:00 --quota 1h
```

On Saturday and Sunday the user has no applicable schedule, so login is
refused all day.

### Weekend schedule

Longer windows at the weekend, nothing during the week:

```bash
sudo child-time set someuser 2h
sudo child-time schedule add someuser --days weekend --start 09:00 --end 12:00 --quota 2h
```

### A different Friday quota

One hour on most days, two hours on Friday split across a morning and
an afternoon window:

```bash
sudo child-time set someuser 1h                 # base: 1 hour
sudo child-time set someuser 2h --day fri       # Friday: 2 hours
sudo child-time schedule add someuser --start 15:00 --end 17:00 --quota 1h
sudo child-time schedule add someuser --days fri --start 09:00 --end 12:00 --quota 1h
```

Resulting policy:

```text
$ sudo child-time limits someuser
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

Note the ordering: add the Friday override **before** the second Friday
schedule, otherwise the second schedule is rejected for exceeding
Friday's 1-hour limit. The rejection is safe — nothing is written — and
you can simply set the override and retry.

### A user without scheduled restrictions

Give a daily quota and no schedules at all:

```bash
sudo child-time set someuser 90m
sudo child-time schedule list someuser     # "someuser has no schedules."
```

The user may log in at any time inside the global access window until
the 90 minutes are consumed. This is the pre-weekly behaviour and is
what every existing user keeps after an upgrade.

### Removing weekly behaviour again

```bash
sudo child-time schedule remove someuser fri/09:00-12:00
sudo child-time set someuser 1h --day fri    # or edit the override
```

## Usage accounting

- Quota is **active-use seconds**, not wall-clock time. A logged-out or
  inactive session is not charged.
- Each schedule has its own counter, keyed by its identifier.
- Usage is stamped with the local calendar date, so all counters reset
  automatically at local midnight. There is nothing to run on a timer.
- Per-schedule usage lives in
  `/var/lib/child-time-limit/schedules/USER.state`; the day-total
  compatibility counter stays in `/var/lib/child-time-limit/USER.state`.
- Only schedules that apply **today** are written to today's state, so
  Friday's morning counter does not appear in Saturday's state.
- The authoritative day total is the larger of the day-total counter
  and the sum of today's per-schedule counters. Logging out and back
  in, or restarting the service, therefore cannot reset consumed time.
- A polling interval that crosses local midnight is not charged to
  either day; the session is ended instead.

## Failure behaviour

The engine is deliberately asymmetric, and this is a security property:

| Situation | Behaviour |
| --- | --- |
| User not listed in `/etc/child-time-limit.conf` | Always allowed. Administrators and the GDM greeter are never restricted. |
| `/etc/child-time-limit.conf` unreadable or malformed | Fail **open** (unchanged from v1.3.1). No account is enforced. |
| Schedule or weekday-limit file malformed, for a configured user | Fail **closed**: login refused, active session ended. |
| Per-schedule usage state malformed, for a configured user | Fail **closed**. |
| A weekday's scheduled quota exceeds that weekday's limit | Fail **closed** for that user. |
| Outside the global access window | Session ended, login refused. |

An invalid configuration never grants unrestricted access to a
configured child. Use `child-time validate` before and after changes.

## Migration from earlier releases

No migration step is required.

- `/etc/child-time-limit.conf` is **not** touched by weekly
  scheduling. Per-weekday overrides live in a separate file.
- `/etc/child-time-daily.conf` is created on first use (or installed as
  a comment-only example). While it does not exist, every user uses
  their base daily limit on all seven days.
- Existing schedule entries without a `DAYS/` prefix are read as
  all-week schedules and keep their original identifiers, so existing
  per-schedule usage state continues to apply.
- Users with no schedule section keep plain daily-quota behaviour.
- Every pre-existing CLI command behaves as before.

After upgrading, confirm with:

```bash
sudo child-time validate
sudo child-time status
sudo child-time limits SOMEUSER
```
