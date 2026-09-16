"""Reusable management core for OFLIT Child Time Limit.

This module contains ONLY policy-management semantics shared by every
front end that needs to inspect or mutate daily time-limit policy: the
`child-time` CLI today, and a future privileged D-Bus backend.

It must stay free of:
  - argparse / CLI printing / sys.exit
  - euid/root checks
  - subprocess / shell execution / systemctl calls
  - PAM, D-Bus, Polkit, or GUI code
  - any mutation of usage state (usage state is read-only here)

Every public function is a pure computation or a single guarded
filesystem transaction. Nothing in this module prints.
"""

import fcntl
import os
import pwd
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional

CONFIG = Path("/etc/child-time-limit.conf")
ACCESS_CONFIG = Path("/etc/child-time-access.conf")
DEFAULT_ACCESS_START = "09:00"
DEFAULT_ACCESS_END = "17:00"
STATE_DIR = Path("/var/lib/child-time-limit")

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.IGNORECASE)
DURATION_RE = re.compile(
    r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$",
    re.IGNORECASE,
)


class ChildTimeError(Exception):
    pass


class StatusRow(NamedTuple):
    username: str
    used: int
    remaining: int
    limit: int
    status: str


class LimitMutationResult(NamedTuple):
    username: str
    used: int
    old_limit: int
    new_limit: int
    remaining: int
    reason: Optional[str] = None


def local_now():
    return datetime.now().astimezone()


def today():
    return local_now().date().isoformat()


def parse_duration(value):
    raw = value.strip().lower()
    if raw.isdigit():
        seconds = int(raw)
    else:
        match = DURATION_RE.fullmatch(raw)
        if not match or not any(match.groupdict().values()):
            raise ChildTimeError(
                "Invalid duration. Use forms such as 90m, 2h25m, 3h, or 30s."
            )
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        secs = int(match.group("seconds") or 0)
        seconds = hours * 3600 + minutes * 60 + secs

    if seconds <= 0:
        raise ChildTimeError("Duration must be greater than zero.")
    return seconds


def parse_clock(value, now=None):
    now = now or local_now()
    match = re.fullmatch(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})", value.strip())
    if not match:
        raise ChildTimeError("Clock time must use HH:MM, for example 10:45.")

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ChildTimeError("Clock time is outside the valid 00:00-23:59 range.")

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        raise ChildTimeError("Until time must be later than the current local time.")
    return target


def parse_clock_of_day(value):
    """Validate an HH:MM time-of-day value, independent of "future today".

    Unlike parse_clock (used by `until`, a one-time wall-clock deadline),
    an access-window boundary is a recurring daily policy: 08:00 is a
    valid start even when configured at 12:00 today.
    """
    match = re.fullmatch(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})", value.strip())
    if not match:
        raise ChildTimeError("Clock time must use HH:MM, for example 10:45.")

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ChildTimeError("Clock time is outside the valid 00:00-23:59 range.")

    return f"{hour:02d}:{minute:02d}"


def fmt(seconds):
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def human(seconds):
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


def validate_username(username, require_local=True):
    if not USERNAME_RE.fullmatch(username):
        raise ChildTimeError(f"Invalid Linux username: {username}")
    if require_local:
        try:
            pwd.getpwnam(username)
        except KeyError as exc:
            raise ChildTimeError(f"Local user does not exist: {username}") from exc


def load_access_window(path=None):
    """Load the global local-time access window.

    Missing config intentionally preserves the v1.2.0 default 09:00-17:00.
    """
    if path is None:
        path = ACCESS_CONFIG

    values = {
        "start": DEFAULT_ACCESS_START,
        "end": DEFAULT_ACCESS_END,
    }

    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise ChildTimeError(f"Cannot read {path}: {exc}") from exc

    seen = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ChildTimeError(f"Invalid access-window line in {path}: {raw}")
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in values:
            raise ChildTimeError(f"Unknown access-window key in {path}: {key}")
        if key in seen:
            raise ChildTimeError(f"Duplicate access-window key in {path}: {key}")
        seen.add(key)
        values[key] = value

    start = parse_clock_of_day(values["start"])
    end = parse_clock_of_day(values["end"])

    if start >= end:
        raise ChildTimeError("Access-window start must be earlier than end.")

    return start, end


def within_access_window(now, start, end):
    current = now.strftime("%H:%M")
    return start <= current < end


def _atomic_update_access_window_locked(start, end, path):
    start = parse_clock_of_day(start)
    end = parse_clock_of_day(end)

    if start >= end:
        raise ChildTimeError("Access-window start must be earlier than end.")

    payload = (
        "# Global child access window (local system time)\n"
        f"start={start}\n"
        f"end={end}\n"
    )

    fd = None
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        with os.fdopen(fd, "w") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise ChildTimeError(f"Cannot update {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    return start, end


def atomic_update_access_window(start, end, path=None):
    if path is None:
        path = ACCESS_CONFIG

    path.parent.mkdir(parents=True, exist_ok=True)

    with policy_lock(path):
        return _atomic_update_access_window_locked(start, end, path)


def load_config(path=None):
    if path is None:
        path = CONFIG
    try:
        raw_lines = path.read_text().splitlines()
    except OSError as exc:
        raise ChildTimeError(f"Cannot read {path}: {exc}") from exc

    limits = {}
    line_indexes = {}
    for index, raw in enumerate(raw_lines):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, seconds_raw = line.split("=", 1)
        name = name.strip()
        try:
            seconds = int(seconds_raw.strip())
        except ValueError as exc:
            raise ChildTimeError(f"Invalid limit for {name!r} in {path}") from exc
        if not name or seconds <= 0:
            raise ChildTimeError(f"Invalid policy line in {path}: {raw}")
        if name in limits:
            raise ChildTimeError(f"Duplicate configured user in {path}: {name}")
        limits[name] = seconds
        line_indexes[name] = index

    return raw_lines, limits, line_indexes


def read_used(username, day=None, state_dir=None):
    """Read-only lookup of today's authoritative usage. Never writes."""
    if state_dir is None:
        state_dir = STATE_DIR
    day = day or today()
    path = Path(state_dir) / f"{username}.state"
    try:
        parts = path.read_text().strip().split()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise ChildTimeError(f"Cannot read {path}: {exc}") from exc

    if len(parts) < 2 or parts[0] != day:
        return 0
    try:
        return max(0, int(float(parts[1])))
    except ValueError as exc:
        raise ChildTimeError(f"Invalid state in {path}") from exc


@contextmanager
def policy_lock(path=None):
    if path is None:
        path = CONFIG
    lock_path = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        raise ChildTimeError(f"Cannot lock {lock_path}: {exc}") from exc

    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_update_limit_locked(username, new_limit, path):
    raw_lines, limits, line_indexes = load_config(path)
    if username not in limits:
        raise ChildTimeError(f"User is not configured: {username}")
    if new_limit <= 0:
        raise ChildTimeError("Limit must be greater than zero.")

    try:
        st = path.stat()
    except OSError as exc:
        raise ChildTimeError(f"Cannot inspect {path}: {exc}") from exc
    raw_lines[line_indexes[username]] = f"{username}={int(new_limit)}"
    payload = "\n".join(raw_lines) + "\n"

    fd = None
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        with os.fdopen(fd, "w") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, st.st_mode & 0o7777)
        os.chown(temporary, st.st_uid, st.st_gid)
        os.replace(temporary, path)
        temporary = None
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise ChildTimeError(f"Cannot update {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    return limits[username], int(new_limit)


def atomic_update_limit(username, new_limit, path=None):
    if path is None:
        path = CONFIG
    with policy_lock(path):
        return _atomic_update_limit_locked(username, new_limit, path)


def confirm_reduction(username, used, old_limit, new_limit, force=False):
    if new_limit > used or force:
        return
    raise ChildTimeError(
        f"Refusing to set {username} to {human(new_limit)} because {human(used)} "
        f"has already been used today. Re-run with --force to exhaust the quota "
        f"immediately. Current limit: {human(old_limit)}."
    )


def status_rows(selected=None, config_path=None, state_dir=None):
    _, limits, _ = load_config(config_path)
    if selected is not None:
        if selected not in limits:
            raise ChildTimeError(f"User is not configured: {selected}")
        names = [selected]
    else:
        names = sorted(limits)

    rows = []
    for username in names:
        limit = limits[username]
        used = read_used(username, state_dir=state_dir)
        remaining = max(0, limit - used)
        status = "EXHAUSTED" if used >= limit else "AVAILABLE"
        rows.append(StatusRow(username, used, remaining, limit, status))
    return rows


def apply_limit_transaction(
    username,
    calculate_limit,
    force=False,
    reason=None,
    config_path=None,
    state_dir=None,
):
    """Authoritative transactional policy mutation.

    CRITICAL INVARIANT: everything from reloading the config through the
    atomic replacement happens inside ONE exclusive policy lock acquired
    below. `calculate_limit` receives the config's current limit and the
    freshly-read authoritative usage, both obtained *after* the lock is
    held, so add/subtract/until always recompute from live state rather
    than a stale pre-lock snapshot. Prints nothing; returns a
    LimitMutationResult.
    """
    validate_username(username)
    if config_path is None:
        config_path = CONFIG

    with policy_lock(config_path):
        _, limits, _ = load_config(config_path)
        if username not in limits:
            raise ChildTimeError(f"User is not configured: {username}")
        old_limit = limits[username]
        used = read_used(username, state_dir=state_dir)
        new_limit = (
            calculate_limit(old_limit, used)
            if callable(calculate_limit)
            else calculate_limit
        )
        if new_limit <= 0:
            raise ChildTimeError("Limit must be greater than zero.")
        confirm_reduction(username, used, old_limit, new_limit, force=force)
        old_limit, new_limit = _atomic_update_limit_locked(
            username, new_limit, config_path
        )

    remaining = max(0, new_limit - used)
    return LimitMutationResult(
        username=username,
        used=used,
        old_limit=old_limit,
        new_limit=new_limit,
        remaining=remaining,
        reason=reason,
    )
