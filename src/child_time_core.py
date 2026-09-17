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
SCHEDULE_CONFIG = Path("/etc/child-time-schedule.conf")
DEFAULT_ACCESS_START = "09:00"
DEFAULT_ACCESS_END = "17:00"
STATE_DIR = Path("/var/lib/child-time-limit")
SCHEDULE_STATE_DIR = STATE_DIR / "schedules"

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


class ScheduledStatusDetail(NamedTuple):
    mode: str
    slot_id: Optional[str]
    used: int
    remaining: int


class LimitMutationResult(NamedTuple):
    username: str
    used: int
    old_limit: int
    new_limit: int
    remaining: int
    reason: Optional[str] = None


class ScheduledSlot(NamedTuple):
    username: str
    start_minute: int
    end_minute: int
    quota_seconds: int
    slot_id: str


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


def _clock_to_minute(value):
    normalized = parse_clock_of_day(value)
    hour, minute = (int(part) for part in normalized.split(":", 1))
    return normalized, hour * 60 + minute


def load_schedule_policy(path=None, limits=None, config_path=None):
    """Load optional per-user scheduled active-use quotas.

    Missing schedule config means no users are scheduled and therefore
    preserves legacy v1.3.x behavior.
    """
    if path is None:
        path = SCHEDULE_CONFIG
    path = Path(path)

    if limits is None:
        _, limits, _ = load_config(config_path)

    try:
        raw_lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ChildTimeError(f"Cannot read {path}: {exc}") from exc

    policy = {}
    current_user = None

    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        if line.startswith("[") and line.endswith("]"):
            username = line[1:-1].strip()
            if not username:
                raise ChildTimeError(f"Empty schedule section in {path}")
            validate_username(username, require_local=False)
            if username not in limits:
                raise ChildTimeError(
                    f"Scheduled user is not configured in daily policy: {username}"
                )
            if username in policy:
                raise ChildTimeError(
                    f"Duplicate schedule section in {path}: {username}"
                )
            policy[username] = []
            current_user = username
            continue

        if current_user is None:
            raise ChildTimeError(
                f"Schedule entry appears before a user section in {path}: {raw}"
            )

        if "=" not in line:
            raise ChildTimeError(f"Invalid schedule line in {path}: {raw}")

        window_raw, quota_raw = (part.strip() for part in line.split("=", 1))
        match = re.fullmatch(
            r"(?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2})",
            window_raw,
        )
        if not match:
            raise ChildTimeError(f"Invalid schedule slot in {path}: {window_raw}")

        start_text, start_minute = _clock_to_minute(match.group("start"))
        end_text, end_minute = _clock_to_minute(match.group("end"))

        if start_minute >= end_minute:
            raise ChildTimeError(
                f"Schedule slot start must be earlier than end: {window_raw}"
            )

        try:
            quota_seconds = int(quota_raw)
        except ValueError as exc:
            raise ChildTimeError(
                f"Invalid scheduled quota for {current_user}: {quota_raw}"
            ) from exc

        if quota_seconds <= 0:
            raise ChildTimeError("Scheduled quota must be greater than zero.")

        wall_seconds = (end_minute - start_minute) * 60
        if quota_seconds > wall_seconds:
            raise ChildTimeError(
                f"Scheduled quota exceeds slot duration: {window_raw}"
            )

        slot = ScheduledSlot(
            username=current_user,
            start_minute=start_minute,
            end_minute=end_minute,
            quota_seconds=quota_seconds,
            slot_id=f"{start_text}-{end_text}",
        )
        policy[current_user].append(slot)

    for username, slots in policy.items():
        slots.sort(key=lambda item: (item.start_minute, item.end_minute))

        previous = None
        for slot in slots:
            if previous is not None and slot.start_minute < previous.end_minute:
                raise ChildTimeError(
                    f"Overlapping schedule slots for {username}: "
                    f"{previous.slot_id} and {slot.slot_id}"
                )
            previous = slot

        scheduled_total = sum(slot.quota_seconds for slot in slots)
        if scheduled_total > limits[username]:
            raise ChildTimeError(
                f"Scheduled quota for {username} exceeds daily limit."
            )

    return policy


def resolve_active_slot(slots, minute_of_day):
    """Return the active slot using START-inclusive, END-exclusive semantics."""
    for slot in slots:
        if slot.start_minute <= minute_of_day < slot.end_minute:
            return slot
    return None


def resolve_next_slot(slots, minute_of_day):
    """Return the next slot whose START is later than minute_of_day."""
    for slot in slots:
        if slot.start_minute > minute_of_day:
            return slot
    return None


def scheduled_status_detail(
    slots,
    slot_usage,
    minute_of_day,
):
    """Return current/next scheduled quota status for display."""
    try:
        minute_of_day = int(minute_of_day)
    except (TypeError, ValueError) as exc:
        raise ChildTimeError(
            "Minute of day must be an integer."
        ) from exc

    if minute_of_day < 0 or minute_of_day > 1439:
        raise ChildTimeError(
            "Minute of day must be between 0 and 1439."
        )

    active = resolve_active_slot(
        slots,
        minute_of_day,
    )

    if active is not None:
        used = int(
            slot_usage.get(active.slot_id, 0)
        )

        if used < 0 or used > active.quota_seconds:
            raise ChildTimeError(
                f"Invalid scheduled usage for slot: "
                f"{active.slot_id}"
            )

        remaining = max(
            0,
            active.quota_seconds - used,
        )

        mode = (
            "EXHAUSTED"
            if remaining == 0
            else "ACTIVE"
        )

        return ScheduledStatusDetail(
            mode,
            active.slot_id,
            used,
            remaining,
        )

    next_slot = resolve_next_slot(
        slots,
        minute_of_day,
    )

    if next_slot is not None:
        used = int(
            slot_usage.get(next_slot.slot_id, 0)
        )

        if used < 0 or used > next_slot.quota_seconds:
            raise ChildTimeError(
                f"Invalid scheduled usage for slot: "
                f"{next_slot.slot_id}"
            )

        return ScheduledStatusDetail(
            "NEXT",
            next_slot.slot_id,
            used,
            max(
                0,
                next_slot.quota_seconds - used,
            ),
        )

    return ScheduledStatusDetail(
        "DONE",
        None,
        0,
        0,
    )


def read_schedule_used(
    username,
    slots,
    day=None,
    state_dir=None,
):
    """Read persisted per-slot usage for one scheduled user.

    Missing or stale state represents zero usage for the requested day.
    State entries that no longer exist in the current policy are ignored,
    so changing a slot never manufactures usage for a different slot.
    """
    if state_dir is None:
        state_dir = SCHEDULE_STATE_DIR

    day = day or today()
    path = Path(state_dir) / f"{username}.state"
    known_slots = {slot.slot_id for slot in slots}
    usage = {slot.slot_id: 0 for slot in slots}

    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return usage
    except OSError as exc:
        raise ChildTimeError(f"Cannot read {path}: {exc}") from exc

    meaningful = [
        raw.strip()
        for raw in lines
        if raw.strip() and not raw.strip().startswith("#")
    ]

    if not meaningful or meaningful[0] != day:
        return usage

    seen = set()
    for line in meaningful[1:]:
        if "=" not in line:
            raise ChildTimeError(f"Invalid scheduled state in {path}: {line}")

        slot_id, used_raw = (part.strip() for part in line.split("=", 1))

        if slot_id in seen:
            raise ChildTimeError(
                f"Duplicate scheduled state slot in {path}: {slot_id}"
            )
        seen.add(slot_id)

        try:
            used = int(used_raw)
        except ValueError as exc:
            raise ChildTimeError(
                f"Invalid scheduled usage in {path}: {used_raw}"
            ) from exc

        if used < 0:
            raise ChildTimeError(
                f"Scheduled usage cannot be negative in {path}: {slot_id}"
            )

        if slot_id in known_slots:
            usage[slot_id] = used

    return usage


def write_schedule_used(
    username,
    slots,
    slot_usage,
    day=None,
    state_dir=None,
):
    """Atomically persist current per-slot usage for one scheduled user."""
    if state_dir is None:
        state_dir = SCHEDULE_STATE_DIR

    day = day or today()
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{username}.state"

    known_slots = {slot.slot_id for slot in slots}
    supplied_slots = set(slot_usage)

    unknown = supplied_slots - known_slots
    if unknown:
        raise ChildTimeError(
            f"Unknown scheduled state slot for {username}: {sorted(unknown)[0]}"
        )

    normalized = {}
    for slot in slots:
        raw_used = slot_usage.get(slot.slot_id, 0)
        try:
            used = int(raw_used)
        except (TypeError, ValueError) as exc:
            raise ChildTimeError(
                f"Invalid scheduled usage for {username}: {slot.slot_id}"
            ) from exc

        if used < 0:
            raise ChildTimeError(
                f"Scheduled usage cannot be negative: {slot.slot_id}"
            )

        if used > slot.quota_seconds:
            raise ChildTimeError(
                f"Scheduled usage exceeds slot quota: {slot.slot_id}"
            )

        normalized[slot.slot_id] = used

    payload = day + "\n"
    payload += "".join(
        f"{slot.slot_id}={normalized[slot.slot_id]}\n"
        for slot in slots
    )

    fd = None
    temporary = None

    try:
        fd, temporary = tempfile.mkstemp(
            prefix=path.name + ".",
            dir=str(state_dir),
        )

        with os.fdopen(fd, "w") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None

        dir_fd = os.open(state_dir, os.O_DIRECTORY)
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

    return normalized


def scheduled_total_used(slot_usage):
    """Return total persisted usage represented by current scheduled slots."""
    return sum(max(0, int(value)) for value in slot_usage.values())


def reconcile_daily_used(legacy_used, slot_usage):
    """Never let recovery report less usage than either persisted view."""
    return max(
        max(0, int(legacy_used)),
        scheduled_total_used(slot_usage),
    )


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


def status_rows(
    selected=None,
    config_path=None,
    state_dir=None,
    schedule_path=None,
    schedule_state_dir=None,
):
    _, limits, _ = load_config(config_path)

    if selected is not None:
        if selected not in limits:
            raise ChildTimeError(f"User is not configured: {selected}")
        names = [selected]
    else:
        names = sorted(limits)

    schedules = load_schedule_policy(
        path=schedule_path,
        limits=limits,
    )

    rows = []

    for username in names:
        limit = limits[username]
        legacy_used = read_used(
            username,
            state_dir=state_dir,
        )

        slots = schedules.get(username, [])

        if slots:
            slot_usage = read_schedule_used(
                username,
                slots,
                state_dir=schedule_state_dir,
            )
            used = reconcile_daily_used(
                legacy_used,
                slot_usage,
            )
        else:
            used = legacy_used

        remaining = max(0, limit - used)
        status = "EXHAUSTED" if used >= limit else "AVAILABLE"

        rows.append(
            StatusRow(
                username,
                used,
                remaining,
                limit,
                status,
            )
        )

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

        candidate_limits = dict(limits)
        candidate_limits[username] = new_limit
        schedules = load_schedule_policy(limits=candidate_limits)
        validate_scheduled_daily_ceiling(
            username,
            new_limit,
            schedules,
        )

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



def validate_scheduled_daily_ceiling(username, candidate_limit, schedules):
    """Reject a daily ceiling below the user's aggregate scheduled quota."""
    validate_username(username)

    try:
        candidate_limit = int(candidate_limit)
    except (TypeError, ValueError):
        raise ChildTimeError("Daily limit must be an integer.")

    if candidate_limit <= 0:
        raise ChildTimeError("Daily limit must be greater than zero.")

    slots = schedules.get(username, [])
    if not slots:
        return

    aggregate = sum(slot.quota_seconds for slot in slots)

    if candidate_limit < aggregate:
        raise ChildTimeError(
            f"Daily limit for {username} cannot be below scheduled quota "
            f"aggregate ({aggregate} seconds)."
        )


class LoginEligibility(NamedTuple):
    allowed: bool
    reason: str
    slot: Optional[ScheduledSlot]


def evaluate_login_eligibility(
    daily_limit,
    daily_used,
    slots,
    slot_usage,
    minute_of_day,
):
    """Evaluate daily/scheduled quota eligibility without performing I/O.

    Global access-window eligibility remains the caller's responsibility.
    An empty slot list preserves legacy daily-quota behavior.
    """
    try:
        daily_limit = int(daily_limit)
        daily_used = int(daily_used)
        minute_of_day = int(minute_of_day)
    except (TypeError, ValueError) as exc:
        raise ChildTimeError("Invalid login eligibility input.") from exc

    if daily_limit <= 0:
        raise ChildTimeError("Daily limit must be greater than zero.")
    if daily_used < 0:
        raise ChildTimeError("Daily usage cannot be negative.")
    if not 0 <= minute_of_day < 24 * 60:
        raise ChildTimeError("Minute of day must be between 0 and 1439.")

    known_slots = {slot.slot_id: slot for slot in slots}
    for slot_id, raw_used in slot_usage.items():
        if slot_id not in known_slots:
            raise ChildTimeError(f"Unknown scheduled state slot: {slot_id}")
        try:
            used = int(raw_used)
        except (TypeError, ValueError) as exc:
            raise ChildTimeError(
                f"Invalid scheduled usage for {slot_id}: {raw_used}"
            ) from exc
        if used < 0:
            raise ChildTimeError(f"Scheduled usage cannot be negative: {slot_id}")
        if used > known_slots[slot_id].quota_seconds:
            raise ChildTimeError(
                f"Scheduled usage exceeds slot quota: {slot_id}"
            )

    effective_daily_used = reconcile_daily_used(daily_used, slot_usage)

    if effective_daily_used >= daily_limit:
        return LoginEligibility(False, "daily_exhausted", None)

    if not slots:
        return LoginEligibility(True, "allowed", None)

    slot = resolve_active_slot(slots, minute_of_day)
    if slot is None:
        return LoginEligibility(False, "outside_slot", None)

    used = slot_usage.get(slot.slot_id, 0)
    if used >= slot.quota_seconds:
        return LoginEligibility(False, "slot_exhausted", slot)

    return LoginEligibility(True, "allowed", slot)


def scheduled_interval_charge(slot, start_second, end_second):
    """Return the portion of a wall-clock interval inside one scheduled slot.

    The interval and slot use START-inclusive / END-exclusive semantics.
    Values are seconds since local midnight.
    """
    try:
        start_second = float(start_second)
        end_second = float(end_second)
    except (TypeError, ValueError) as exc:
        raise ChildTimeError("Invalid scheduled accounting interval.") from exc

    day_seconds = 24 * 60 * 60

    if not 0 <= start_second <= day_seconds:
        raise ChildTimeError("Interval start is outside the local day.")

    if not 0 <= end_second <= day_seconds:
        raise ChildTimeError("Interval end is outside the local day.")

    if end_second < start_second:
        raise ChildTimeError("Interval end cannot precede interval start.")

    slot_start = slot.start_minute * 60
    slot_end = slot.end_minute * 60

    overlap_start = max(start_second, slot_start)
    overlap_end = min(end_second, slot_end)

    return max(0.0, overlap_end - overlap_start)


class ScheduledChargeResult(NamedTuple):
    charged_seconds: float
    daily_used: float
    slot_used: float
    daily_exhausted: bool
    slot_exhausted: bool


def apply_scheduled_charge(
    daily_limit,
    daily_used,
    slot,
    slot_used,
    charge_seconds,
):
    """Apply one active-use charge without performing persistence."""

    try:
        daily_limit = float(daily_limit)
        daily_used = float(daily_used)
        slot_used = float(slot_used)
        charge_seconds = float(charge_seconds)
    except (TypeError, ValueError) as exc:
        raise ChildTimeError("Invalid scheduled charge input.") from exc

    if daily_limit <= 0:
        raise ChildTimeError("Daily limit must be greater than zero.")

    if daily_used < 0:
        raise ChildTimeError("Daily usage cannot be negative.")

    if slot_used < 0:
        raise ChildTimeError("Scheduled usage cannot be negative.")

    if charge_seconds < 0:
        raise ChildTimeError("Scheduled charge cannot be negative.")

    if slot_used > slot.quota_seconds:
        raise ChildTimeError(
            f"Scheduled usage exceeds slot quota: {slot.slot_id}"
        )

    daily_remaining = max(0.0, daily_limit - daily_used)
    slot_remaining = max(0.0, slot.quota_seconds - slot_used)

    charged = min(
        charge_seconds,
        daily_remaining,
        slot_remaining,
    )

    new_daily_used = daily_used + charged
    new_slot_used = slot_used + charged

    return ScheduledChargeResult(
        charged_seconds=charged,
        daily_used=new_daily_used,
        slot_used=new_slot_used,
        daily_exhausted=new_daily_used >= daily_limit,
        slot_exhausted=new_slot_used >= slot.quota_seconds,
    )


def persist_scheduled_accounting(*, write_slot, write_daily):
    """Persist scheduled accounting in crash-safe order.

    Slot state must become durable before the legacy daily compatibility
    state. If slot persistence fails, daily persistence must not run.
    """
    write_slot()
    write_daily()


class ScheduledIntervalResult(NamedTuple):
    charged_seconds: float
    daily_used: float
    slot_usage: dict
    daily_exhausted: bool
    slot_exhausted: bool


def account_scheduled_interval(
    *,
    daily_limit,
    daily_used,
    slots,
    slot_usage,
    start_second,
    end_second,
):
    """Account one wall-clock interval across scheduled slots."""

    usage = dict(slot_usage)
    current_daily = float(daily_used)
    total_charged = 0.0
    any_slot_exhausted = False

    known_slot_ids = {slot.slot_id for slot in slots}

    unknown = set(usage) - known_slot_ids
    if unknown:
        raise ChildTimeError(
            "Scheduled usage contains unknown slot state."
        )

    for slot in slots:
        current_slot_used = usage.get(slot.slot_id, 0.0)

        interval_charge = scheduled_interval_charge(
            slot,
            start_second,
            end_second,
        )

        result = apply_scheduled_charge(
            daily_limit=daily_limit,
            daily_used=current_daily,
            slot=slot,
            slot_used=current_slot_used,
            charge_seconds=interval_charge,
        )

        usage[slot.slot_id] = result.slot_used
        current_daily = result.daily_used
        total_charged += result.charged_seconds

        if interval_charge > 0 and result.slot_exhausted:
            any_slot_exhausted = True

        if result.daily_exhausted:
            break

    return ScheduledIntervalResult(
        charged_seconds=total_charged,
        daily_used=current_daily,
        slot_usage=usage,
        daily_exhausted=current_daily >= float(daily_limit),
        slot_exhausted=any_slot_exhausted,
    )


class ScheduledEnforcementResult(NamedTuple):
    terminate: bool
    reason: str
    slot: Optional[ScheduledSlot]


def evaluate_scheduled_enforcement(
    *,
    daily_limit,
    daily_used,
    slots,
    slot_usage,
    minute_of_day,
):
    """Translate shared login eligibility into enforcer action."""

    eligibility = evaluate_login_eligibility(
        daily_limit=daily_limit,
        daily_used=daily_used,
        slots=slots,
        slot_usage=slot_usage,
        minute_of_day=minute_of_day,
    )

    return ScheduledEnforcementResult(
        terminate=not eligibility.allowed,
        reason=eligibility.reason,
        slot=eligibility.slot,
    )
