# Acceptance testing

Do not rely only on `systemctl status`. Verify the complete path from session accounting to forced termination and same-day login denial.

Use a disposable or child test account that is **not an administrator**.

## 1. Configure a short quota

Edit `/etc/child-time-limit.conf` and temporarily set the test account to 120 seconds:

```ini
testchild=120
```

Clear only that account's previous test state:

```bash
sudo rm -f /var/lib/child-time-limit/testchild.state
sudo systemctl restart child-time-enforcer.service
```

Confirm the daemon is live:

```bash
systemctl is-enabled child-time-enforcer.service
systemctl is-active child-time-enforcer.service
```

Expected:

```text
enabled
active
```

## 2. Use the graphical session

Log out of the administrator account, log in as the test child, and actively use the desktop. Do not manually log out or lock the test session.

At roughly 120 seconds, the user should be returned to the display manager because `loginctl terminate-user` removed the account's session.

## 3. Verify true termination

Log in as the administrator and run:

```bash
loginctl list-sessions --no-legend
loginctl list-users --no-legend
```

The test child should not retain a graphical user session.

Verify state:

```bash
sudo cat /var/lib/child-time-limit/testchild.state
```

Expected form:

```text
YYYY-MM-DD 120
```

## 4. Verify the login gate

Run the helper directly as root for a functional check:

```bash
set +e
sudo env PAM_USER=testchild /usr/local/sbin/child-time-login-check
rc=$?
set -e
echo "login_check_exit=$rc"
```

Expected:

```text
login_check_exit=1
```

Then perform the more important integration test: try a real graphical login as the test child. It should be rejected on the same day.

## 5. Verify reboot persistence

Do not delete the state file. Reboot:

```bash
sudo reboot
```

After boot, try the child login again. It should still be rejected.

As administrator, confirm:

```bash
sudo cat /var/lib/child-time-limit/testchild.state
systemctl is-active child-time-enforcer.service
```

The same-day state must still be at the quota and the daemon must be active.

## 6. Verify next-day semantics without changing the system clock

Write an exhausted state dated yesterday:

```bash
YESTERDAY="$(date -d yesterday +%F)"
echo "$YESTERDAY 120" | sudo tee /var/lib/child-time-limit/testchild.state >/dev/null
sudo chmod 600 /var/lib/child-time-limit/testchild.state
```

Run the helper:

```bash
set +e
sudo env PAM_USER=testchild /usr/local/sbin/child-time-login-check
rc=$?
set -e
echo "new_day_login_exit=$rc"
```

Expected:

```text
new_day_login_exit=0
```

Log in briefly as the child, log out normally, and inspect the state again. It should now contain today's date and a small positive usage value.

## 7. Restore the real policy

After all gates pass, restore the intended quota. For two hours per day:

```ini
testchild=7200
```

Remove any artificial test state if you want the production quota to start from zero on that day:

```bash
sudo rm -f /var/lib/child-time-limit/testchild.state
sudo systemctl restart child-time-enforcer.service
```


## Automated regression suite

Before release, run:

    python3 -m unittest discover -v tests

The v1.3.0 release gate contains 52 automated tests covering:

- daily quota policy behavior;
- atomic and concurrent policy updates;
- reduction and `--force` semantics;
- state and status behavior;
- CLI-to-core adapter behavior;
- `child-time until` active-use semantics;
- policy-core packaging;
- access-window boundaries, including a configurable window;
- concurrent, lock-protected access-window updates;
- access-window clock parsing independent of "future today" semantics.

Default access-window boundaries (`09:00`-`17:00`):

    08:59:59 -> denied
    09:00:00 -> allowed
    16:59:59 -> allowed
    17:00:00 -> denied

A configured window (for example `08:00`-`18:00`, set with
`sudo child-time window 08:00 18:00`) is enforced with the same
inclusive-start/exclusive-end boundary semantics.

Automated tests complement live PAM and graphical-session acceptance
testing; they do not replace it.

## Weekly schedules and per-weekday limits

Weekly policy changes the *shape* of the quota, not the enforcement
mechanism, so the checks above still apply. Verify these additionally.

Set up a disposable, non-administrator test account with a short
policy. Pick **today's** weekday for `DAY` so the test is immediate:

```bash
sudo child-time set testchild 4m
sudo child-time schedule add testchild --days DAY --start HH:MM --end HH:MM --quota 2m
sudo child-time limits testchild
sudo child-time validate
```

Choose a window that starts a couple of minutes from now so you can
observe the boundary.

### 1. Weekday restriction

```bash
# A weekday the schedule does NOT cover must refuse login all day.
sudo PAM_USER=testchild /usr/local/sbin/child-time-login-check; echo "exit=$?"
```

On a covered weekday inside the window, expect `exit=0`. On a
non-covered weekday, or outside the window, expect `exit=1`.

Then attempt a real graphical login and confirm it matches.

### 2. Per-slot quota is independent

Configure two windows on the same weekday, each with its own quota:

```bash
sudo child-time schedule add testchild --days DAY --start HH:MM --end HH:MM --quota 2m
sudo child-time schedule add testchild --days DAY --start HH:MM --end HH:MM --quota 2m
```

Use the first window to exhaustion. Confirm:

- the session is terminated when that window's quota runs out;
- login is refused for the rest of that window;
- login is allowed again when the **second** window opens, with its own
  full quota;
- unused time from the first window did **not** extend the second.

Inspect the per-slot counters:

```bash
sudo cat /var/lib/child-time-limit/schedules/testchild.state
```

The first line is the local date; each following line is
`SCHEDULE_ID=seconds`.

### 3. Per-weekday daily limit

```bash
sudo child-time set testchild 6m --day DAY
sudo child-time limits testchild
```

Confirm the chosen weekday reports the larger limit with source
`override`, and every other weekday still reports `base`. Then confirm
the day's total consumption stops at the override, even if a later
window still has slot quota left.

### 4. No bypass

With quota partly consumed inside a window:

```bash
# Logout / login must not restore time.
sudo loginctl terminate-user testchild
# log back in, then re-check:
sudo child-time status testchild

# Service restart must not restore time.
sudo systemctl restart child-time-enforcer.service
sudo child-time status testchild
```

`USED` must not decrease in either case.

### 5. Day rollover

Confirm that after the local date changes, both the day total and every
per-slot counter start from zero, and that the new day's applicable
schedules are the ones for that new weekday. Verify a
Friday-to-Saturday rollover specifically if the policy differs between
them.

### 6. Invalid policy fails closed

```bash
# Temporarily corrupt the schedule file for a CONFIGURED child.
sudo cp /etc/child-time-schedule.conf /tmp/schedule.bak
sudo sh -c 'printf "[testchild]\nfunday/09:00-10:00=600\n" > /etc/child-time-schedule.conf'
sudo child-time validate; echo "exit=$?"
sudo PAM_USER=testchild /usr/local/sbin/child-time-login-check; echo "exit=$?"
```

Expect `validate` to exit non-zero and the login check to exit `1`
(denied). An active session for that child must be terminated.

Confirm an **unconfigured** administrator account is still allowed:

```bash
sudo PAM_USER=youradminuser /usr/local/sbin/child-time-login-check; echo "exit=$?"
```

Expect `exit=0`.

Restore immediately:

```bash
sudo cp /tmp/schedule.bak /etc/child-time-schedule.conf
sudo child-time validate
```

### 7. Rejected changes do not alter configuration

```bash
sudo cp /etc/child-time-schedule.conf /tmp/before.conf
sudo child-time schedule add testchild --days funday --start 09:00 --end 10:00 --quota 5m
sudo diff /tmp/before.conf /etc/child-time-schedule.conf && echo "unchanged: OK"
```

## Acceptance criteria

A production deployment is accepted only when all of these are proven:

- usage state increases while the configured graphical session is active;
- quota exhaustion terminates the actual user session;
- the account cannot immediately start another session;
- same-day state survives reboot;
- the daemon starts automatically after reboot;
- yesterday's exhausted state does not consume today's quota;
- administrator accounts that are not configured remain unaffected.

With weekly policy configured, additionally:

- login is refused on a weekday with no applicable schedule;
- login is refused outside every applicable window on a covered weekday;
- each scheduled window enforces its own independent quota;
- unused quota never carries between windows or between days;
- a per-weekday limit override caps that whole day;
- logout/login and a service restart never restore consumed time;
- all counters reset at local midnight, including across a weekday
  boundary where the policy differs;
- an invalid schedule or weekday-limit file denies the configured child
  rather than granting unrestricted access;
- a rejected `child-time` command leaves configuration unchanged.
