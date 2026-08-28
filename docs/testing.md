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

## Acceptance criteria

A production deployment is accepted only when all of these are proven:

- usage state increases while the configured graphical session is active;
- quota exhaustion terminates the actual user session;
- the account cannot immediately start another session;
- same-day state survives reboot;
- the daemon starts automatically after reboot;
- yesterday's exhausted state does not consume today's quota;
- administrator accounts that are not configured remain unaffected.
