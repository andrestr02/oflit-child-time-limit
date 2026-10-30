# Deployment and rollback

This describes deploying the weekly scheduling release to a production
host. Nothing here runs automatically.

**Deploy during a window when no child session is active**, because
activating the service ends sessions that fall outside the new policy.

## 0. Before you start

Collect:

- the repository checkout at the revision you intend to deploy;
- root access on the target host;
- a note of the current policy, so you can compare afterwards.

Record the current state for comparison:

```bash
sudo child-time status
sudo child-time window
sudo cat /etc/child-time-limit.conf
sudo cat /etc/child-time-schedule.conf 2>/dev/null
```

## 1. Back up configuration and usage data

`scripts/child-time-backup.sh` writes a timestamped, root-only archive
of every policy file, the PAM hook and the whole state tree:

```bash
sudo ./scripts/child-time-backup.sh
```

It prints the archive path, for example:

```text
/var/lib/child-time-limit/backups/child-time-20261030-104500.tar.gz
```

Keep that path; the rollback procedure needs it. The script never
deletes or modifies anything.

## 2. Validate the policy before activating it

Validate against the **currently installed** code first, to confirm the
existing policy is clean:

```bash
sudo child-time validate
```

Then dry-run the new code against the live policy files without
installing anything:

```bash
sudo ./scripts/child-time-preflight.sh
```

Preflight runs the repository's test suite and then validates the live
`/etc` policy using the **new** core, reporting the effective weekly
policy for every configured user. It writes nothing.

Do not continue unless preflight passes.

## 3. Install

```bash
sudo ./install.sh
```

The installer:

- backs up `/etc/pam.d/gdm-password` to
  `/var/lib/child-time-limit/backups/`;
- installs the core module, enforcer, login check, `child-time` and
  `child-time-status`;
- creates `/etc/child-time-daily.conf` from the example **only if it
  does not already exist**, and otherwise just re-asserts mode `0600`;
- leaves `/etc/child-time-limit.conf`, `/etc/child-time-access.conf`
  and `/etc/child-time-schedule.conf` untouched if they exist;
- never touches `/var/lib/child-time-limit/` usage data;
- reloads systemd, then enables and **restarts**
  `child-time-enforcer.service`.

> The installer restarts the enforcer. Run it only with explicit
> approval for a production host.

If the enforcer fails to start, the installer restores the PAM backup
and disables the service before exiting non-zero.

## 4. Verify the deployment

```bash
# Service health
systemctl is-enabled child-time-enforcer.service
systemctl is-active  child-time-enforcer.service
systemctl status     child-time-enforcer.service --no-pager
journalctl -u child-time-enforcer.service -n 50 --no-pager

# Policy health
sudo child-time validate
sudo child-time status
```

Expected: `enabled`, `active`, `Policy validation: OK`, and a status
table listing the same users with the same consumed time as before the
deployment.

Then confirm the effective policy for each configured user:

```bash
sudo child-time limits SOMEUSER
```

Check that:

- usage already consumed today is unchanged;
- users who had no schedules still show "no scheduled restriction";
- existing schedules appear with their original identifiers.

`scripts/child-time-verify.sh` performs these checks and exits
non-zero if any of them fail:

```bash
sudo ./scripts/child-time-verify.sh
```

## 5. Configure weekly policy

Only now add weekly rules, one command at a time, validating as you go:

```bash
sudo child-time set someuser 1h
sudo child-time set someuser 2h --day fri
sudo child-time schedule add someuser --start 15:00 --end 17:00 --quota 1h
sudo child-time schedule add someuser --days fri --start 09:00 --end 12:00 --quota 1h
sudo child-time limits someuser
sudo child-time validate
```

A rejected command changes nothing, so it is safe to retry after
fixing the input. No service restart is needed for policy changes.

## 6. Functional acceptance

Follow `docs/testing.md` with a disposable non-administrator account,
then additionally confirm:

- login is refused outside the configured windows on a normal weekday;
- login is allowed inside a window;
- the per-weekday override day grants the larger limit;
- logging out and back in does not restore consumed time;
- `systemctl restart child-time-enforcer.service` does not restore
  consumed time.

## Rollback

### Roll back policy only

If the code is fine but a policy change was wrong, use the CLI — it is
the fastest and safest route, and needs no restart:

```bash
sudo child-time schedule list   someuser
sudo child-time schedule remove someuser SCHEDULE_ID
sudo child-time set someuser 1h --day fri
sudo child-time validate
```

To restore the exact previous policy files from a backup archive:

```bash
sudo ./scripts/child-time-restore.sh /var/lib/child-time-limit/backups/child-time-TIMESTAMP.tar.gz --config-only
sudo child-time validate
```

`--config-only` restores the `/etc` policy files and leaves usage data
as it is, so children do not regain time already consumed today.

### Roll back code and policy

```bash
# 1. Restore the previous policy and PAM hook
sudo ./scripts/child-time-restore.sh /var/lib/child-time-limit/backups/child-time-TIMESTAMP.tar.gz

# 2. Reinstall the previous revision
git -C /path/to/checkout checkout <previous-tag-or-commit>
sudo ./install.sh

# 3. Verify
sudo ./scripts/child-time-verify.sh
```

`child-time-restore.sh` backs up the current state before replacing
anything, so a rollback is itself reversible. It restores usage data by
default; pass `--config-only` to keep current usage.

### Disable enforcement entirely

As an emergency measure, to stop enforcement without uninstalling:

```bash
sudo systemctl disable --now child-time-enforcer.service
```

The PAM login check stays installed, so logins are still evaluated. To
also stop login checks, run `sudo ./uninstall.sh`, which removes the
PAM rule and the installed programs but **deliberately preserves**
every policy file and all usage data.

## What is never touched

| Path | Deploy | Rollback |
| --- | --- | --- |
| `/etc/child-time-limit.conf` | preserved if present | restored from backup |
| `/etc/child-time-access.conf` | preserved if present | restored from backup |
| `/etc/child-time-schedule.conf` | preserved if present | restored from backup |
| `/etc/child-time-daily.conf` | created only if absent | restored from backup |
| `/var/lib/child-time-limit/*.state` | never modified | preserved unless explicitly restored |
| `/var/lib/child-time-limit/schedules/` | never modified | preserved unless explicitly restored |
