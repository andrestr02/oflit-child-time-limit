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
DAILY_CONFIG = Path("/etc/child-time-daily.conf")
DEFAULT_ACCESS_START = "09:00"
DEFAULT_ACCESS_END = "17:00"
STATE_DIR = Path("/var/lib/child-time-limit")
SCHEDULE_STATE_DIR = STATE_DIR / "schedules"

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$", re.IGNORECASE)
DURATION_RE = re.compile(
    r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$",
    re.IGNORECASE,
)


# Weekday engine
# ---------------
# Weekdays are represented internally as Python's date.weekday() index:
# 0=Monday .. 6=Sunday. WEEKDAY_NAMES is the canonical on-disk spelling.
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_INDEX = {name: index for index, name in enumerate(WEEKDAY_NAMES)}
ALL_WEEKDAYS = frozenset(range(7))

# Accepted aliases for one weekday. Keep this table data-only: the
# scheduling engine must never contain per-user rules.
WEEKDAY_ALIASES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

WEEKDAY_GROUPS = {
    "daily": ALL_WEEKDAYS,
    "all": ALL_WEEKDAYS,
    "everyday": ALL_WEEKDAYS,
    "weekday": frozenset({0, 1, 2, 3, 4}),
    "weekdays": frozenset({0, 1, 2, 3, 4}),
    "weekend": frozenset({5, 6}),
    "weekends": frozenset({5, 6}),
}


class ChildTimeError(Exception):
    pass


def parse_weekday(token):
    """Resolve one weekday token to its 0=Monday..6=Sunday index."""
    key = str(token).strip().lower()
    if key in WEEKDAY_INDEX:
        return WEEKDAY_INDEX[key]
    if key in WEEKDAY_ALIASES:
        return WEEKDAY_ALIASES[key]
    raise ChildTimeError(
        f"Invalid weekday {token!r}. Use one of: "
        f"{', '.join(WEEKDAY_NAMES)}."
    )


def parse_weekday_expression(expression):
    """Parse a weekday expression into a frozenset of weekday indexes.

    Deterministic grammar (case-insensitive, whitespace ignored):

        expression := term ("," term)*
        term       := group | day | day "-" day

    `group` is one of the documented shorthands (daily, all, everyday,
    weekdays, weekend). A `day "-" day` range is inclusive and walks
    forward through Mon..Sun, wrapping past Sunday when the end day is
    earlier than the start day, so `fri-mon` means Fri, Sat, Sun, Mon.
    """
    if expression is None:
        raise ChildTimeError("Weekday expression must not be empty.")

    text = str(expression).strip()
    if not text:
        raise ChildTimeError("Weekday expression must not be empty.")

    days = set()

    for raw_term in text.split(","):
        term = raw_term.strip().lower()
        if not term:
            raise ChildTimeError(
                f"Empty weekday term in expression: {expression!r}"
            )

        if term in WEEKDAY_GROUPS:
            days |= WEEKDAY_GROUPS[term]
            continue

        if "-" in term:
            parts = term.split("-")
            if len(parts) != 2:
                raise ChildTimeError(
                    f"Invalid weekday range: {raw_term.strip()!r}"
                )
            start = parse_weekday(parts[0])
            end = parse_weekday(parts[1])
            # Inclusive forward walk, wrapping past Sunday.
            index = start
            while True:
                days.add(index)
                if index == end:
                    break
                index = (index + 1) % 7
            continue

        days.add(parse_weekday(term))

    if not days:
        raise ChildTimeError(
            f"Weekday expression selects no days: {expression!r}"
        )

    return frozenset(days)


def format_weekday_set(days):
    """Render a weekday set as its canonical, round-trippable spelling.

    Consecutive runs in Mon..Sun order collapse into ranges, so the
    output is stable for a given set and re-parses to the same set.
    """
    selected = sorted(frozenset(days))

    if not selected:
        raise ChildTimeError("Weekday set must not be empty.")

    for index in selected:
        if index not in ALL_WEEKDAYS:
            raise ChildTimeError(f"Invalid weekday index: {index}")

    if frozenset(selected) == ALL_WEEKDAYS:
        return "daily"

    runs = []
    for index in selected:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])

    parts = []
    for run in runs:
        if len(run) == 1:
            parts.append(WEEKDAY_NAMES[run[0]])
        elif len(run) == 2:
            parts.append(WEEKDAY_NAMES[run[0]])
            parts.append(WEEKDAY_NAMES[run[1]])
        else:
            parts.append(f"{WEEKDAY_NAMES[run[0]]}-{WEEKDAY_NAMES[run[-1]]}")

    return ",".join(parts)


class StatusRow(NamedTuple):
    username: str
    used: int
    remaining: int
    limit: int
    status: str
    weekday: Optional[int] = None
    limit_source: str = "base"


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
    """One scheduled active-use quota window for one user.

    `days` is appended last and defaults to every weekday so that
    existing positional construction -- and any legacy all-day slot --
    keeps working unchanged.
    """

    username: str
    start_minute: int
    end_minute: int
    quota_seconds: int
    slot_id: str
    days: frozenset = ALL_WEEKDAYS

    def applies_on(self, weekday):
        return int(weekday) in self.days


class ScheduleMutationResult(NamedTuple):
    username: str
    action: str
    slot_id: str
    previous_slot_id: Optional[str] = None


class EffectiveLimit(NamedTuple):
    username: str
    weekday: int
    seconds: int
    source: str


def schedule_slot_id(days, start_text, end_text):
    """Build the stable identifier for one schedule.

    An all-week schedule keeps the legacy `HH:MM-HH:MM` identifier, so
    schedules written by v1.4 and earlier address the same persisted
    per-slot usage state. Weekday-scoped schedules are prefixed with
    their canonical weekday expression.
    """
    window = f"{start_text}-{end_text}"
    if frozenset(days) == ALL_WEEKDAYS:
        return window
    return f"{format_weekday_set(days)}/{window}"


def slots_for_weekday(slots, weekday):
    """Return the schedules applicable on one weekday, in start order.

    This is the single point where a weekly policy becomes a concrete
    day policy. Every front end (CLI, login check, enforcer) filters
    through here so they cannot disagree about a day's rules.
    """
    try:
        weekday = int(weekday)
    except (TypeError, ValueError) as exc:
        raise ChildTimeError("Weekday must be an integer.") from exc

    if weekday not in ALL_WEEKDAYS:
        raise ChildTimeError(
            f"Weekday must be between 0 (Monday) and 6 (Sunday): {weekday}"
        )

    applicable = [slot for slot in slots if weekday in slot.days]
    applicable.sort(key=lambda item: (item.start_minute, item.end_minute))
    return applicable


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


def load_daily_overrides(path=None, limits=None, config_path=None):
    """Load optional per-weekday daily-limit overrides.

    Format (INI-style, one section per user, one line per weekday):

        [someuser]
        fri=7200

    The key is any weekday expression; every weekday it selects takes
    the given number of seconds as that day's total daily limit. A
    missing file means no overrides, which preserves the behaviour of
    releases that only had a single daily limit per user.

    Returns {username: {weekday_index: seconds}}.
    """
    if path is None:
        path = DAILY_CONFIG
    path = Path(path)

    if limits is None:
        _, limits, _ = load_config(config_path)

    try:
        raw_lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ChildTimeError(f"Cannot read {path}: {exc}") from exc

    overrides = {}
    current_user = None

    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        if line.startswith("[") and line.endswith("]"):
            username = line[1:-1].strip()
            if not username:
                raise ChildTimeError(f"Empty daily-limit section in {path}")
            validate_username(username, require_local=False)
            if username not in limits:
                raise ChildTimeError(
                    "Daily-limit override for a user that is not configured "
                    f"in daily policy: {username}"
                )
            if username in overrides:
                raise ChildTimeError(
                    f"Duplicate daily-limit section in {path}: {username}"
                )
            overrides[username] = {}
            current_user = username
            continue

        if current_user is None:
            raise ChildTimeError(
                f"Daily-limit entry appears before a user section in {path}: {raw}"
            )

        if "=" not in line:
            raise ChildTimeError(f"Invalid daily-limit line in {path}: {raw}")

        days_raw, seconds_raw = (part.strip() for part in line.split("=", 1))
        days = parse_weekday_expression(days_raw)

        try:
            seconds = int(seconds_raw)
        except ValueError as exc:
            raise ChildTimeError(
                f"Invalid daily-limit override for {current_user}: {seconds_raw}"
            ) from exc

        if seconds <= 0:
            raise ChildTimeError(
                "Daily-limit override must be greater than zero."
            )

        for weekday in sorted(days):
            if weekday in overrides[current_user]:
                raise ChildTimeError(
                    f"Duplicate daily-limit override for {current_user} on "
                    f"{WEEKDAY_NAMES[weekday]}."
                )
            overrides[current_user][weekday] = seconds

    return overrides


def effective_daily_limit(username, weekday, limits, overrides=None):
    """Return the total daily limit that applies to one user on one day.

    A weekday override wins over the user's base daily limit. This is
    the only place that decision is made, so the CLI, the login check
    and the enforcer always agree on a day's ceiling.
    """
    try:
        weekday = int(weekday)
    except (TypeError, ValueError) as exc:
        raise ChildTimeError("Weekday must be an integer.") from exc

    if weekday not in ALL_WEEKDAYS:
        raise ChildTimeError(
            f"Weekday must be between 0 (Monday) and 6 (Sunday): {weekday}"
        )

    if username not in limits:
        raise ChildTimeError(f"User is not configured: {username}")

    base = int(limits[username])
    user_overrides = (overrides or {}).get(username, {})

    if weekday in user_overrides:
        return EffectiveLimit(
            username,
            weekday,
            int(user_overrides[weekday]),
            "override",
        )

    return EffectiveLimit(username, weekday, base, "base")


def _clock_to_minute(value):
    normalized = parse_clock_of_day(value)
    hour, minute = (int(part) for part in normalized.split(":", 1))
    return normalized, hour * 60 + minute


def load_schedule_policy(
    path=None,
    limits=None,
    config_path=None,
    overrides=None,
    daily_path=None,
):
    """Load optional per-user scheduled active-use quotas.

    Policy format (deterministic; see docs/configuration.md):

        [someuser]
        15:00-17:00=3600
        fri/09:00-12:00=3600
        mon-fri/08:00-09:00=1800

    Each entry is `[DAYS/]HH:MM-HH:MM=QUOTA`. `DAYS` is any weekday
    expression accepted by parse_weekday_expression; when omitted the
    schedule applies on every weekday and keeps the legacy
    `HH:MM-HH:MM` identifier. QUOTA is active-use seconds (an integer)
    or a duration such as `1h30m`.

    Missing schedule config means no users are scheduled and therefore
    preserves legacy v1.3.x behavior.
    """
    if path is None:
        path = SCHEDULE_CONFIG
    path = Path(path)

    if limits is None:
        _, limits, _ = load_config(config_path)

    # Keep custom/test policy trees isolated: weekday limit overrides
    # are resolved next to whichever schedule config we were handed.
    if overrides is None:
        if daily_path is None:
            daily_path = path.parent / DAILY_CONFIG.name
        overrides = load_daily_overrides(path=daily_path, limits=limits)

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
            r"(?:(?P<days>[^/]+)/)?"
            r"(?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2})",
            window_raw,
        )
        if not match:
            raise ChildTimeError(f"Invalid schedule slot in {path}: {window_raw}")

        days_raw = match.group("days")
        days = (
            ALL_WEEKDAYS
            if days_raw is None
            else parse_weekday_expression(days_raw)
        )

        start_text, start_minute = _clock_to_minute(match.group("start"))
        end_text, end_minute = _clock_to_minute(match.group("end"))

        if start_minute >= end_minute:
            raise ChildTimeError(
                f"Schedule slot start must be earlier than end: {window_raw}"
            )

        try:
            quota_seconds = int(quota_raw)
        except ValueError:
            # Durations such as `1h30m` are accepted for hand-edited files.
            try:
                quota_seconds = parse_duration(quota_raw)
            except ChildTimeError as exc:
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
            slot_id=schedule_slot_id(days, start_text, end_text),
            days=days,
        )
        policy[current_user].append(slot)

    for username, slots in policy.items():
        slots.sort(key=lambda item: (item.start_minute, item.end_minute))

        seen_ids = set()
        for slot in slots:
            if slot.slot_id in seen_ids:
                raise ChildTimeError(
                    f"Duplicate schedule identifier for {username}: {slot.slot_id}"
                )
            seen_ids.add(slot.slot_id)

        validate_weekly_schedule(username, slots, limits, overrides)

    return policy


def validate_weekly_schedule(username, slots, limits, overrides=None):
    """Reject a weekly schedule set that cannot be enforced coherently.

    Both checks are per weekday, because two schedules only conflict on
    a day where both apply:
      - no two applicable schedules may overlap in time;
      - that day's scheduled quota must fit inside that day's effective
        daily limit.
    """
    for weekday in sorted(ALL_WEEKDAYS):
        applicable = slots_for_weekday(slots, weekday)
        if not applicable:
            continue

        previous = None
        for slot in applicable:
            if previous is not None and slot.start_minute < previous.end_minute:
                raise ChildTimeError(
                    f"Overlapping schedule slots for {username} on "
                    f"{WEEKDAY_NAMES[weekday]}: "
                    f"{previous.slot_id} and {slot.slot_id}"
                )
            previous = slot

        ceiling = effective_daily_limit(
            username,
            weekday,
            limits,
            overrides,
        ).seconds

        scheduled_total = sum(slot.quota_seconds for slot in applicable)
        if scheduled_total > ceiling:
            raise ChildTimeError(
                f"Scheduled quota for {username} exceeds the "
                f"{WEEKDAY_NAMES[weekday]} daily limit "
                f"({scheduled_total} > {ceiling} seconds)."
            )


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


def _atomic_write_text(path, payload):
    """Replace one policy file atomically, preserving its permissions.

    A file that does not exist yet is created 0600 root-owned (the same
    posture install.sh gives the other policy files), because these
    files describe who may use the computer.
    """
    path = Path(path)
    directory = path.parent

    try:
        st = path.stat()
        mode = st.st_mode & 0o7777
        owner = (st.st_uid, st.st_gid)
    except FileNotFoundError:
        mode = 0o600
        owner = None
    except OSError as exc:
        raise ChildTimeError(f"Cannot inspect {path}: {exc}") from exc

    fd = None
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=path.name + ".",
            dir=str(directory),
        )
        with os.fdopen(fd, "w") as handle:
            fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(temporary, mode)
        if owner is not None:
            os.chown(temporary, owner[0], owner[1])

        os.replace(temporary, path)
        temporary = None

        dir_fd = os.open(directory, os.O_DIRECTORY)
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


def _config_header(path, default_header):
    """Return the comment block above the first section of a config file."""
    try:
        raw_lines = Path(path).read_text().splitlines()
    except (FileNotFoundError, OSError):
        return list(default_header)

    header = []
    for raw in raw_lines:
        if raw.strip().startswith("[") and raw.strip().endswith("]"):
            break
        header.append(raw)

    while header and not header[-1].strip():
        header.pop()

    return header or list(default_header)


SCHEDULE_HEADER = [
    "# OFLIT Child Time Limit - per-user weekly scheduled quotas",
    "#",
    "# Managed by `child-time schedule`. Entries are rewritten in",
    "# canonical form, so hand-written comments inside a [user] section",
    "# are not preserved.",
    "#",
    "# Format: [DAYS/]HH:MM-HH:MM=SECONDS",
    "#   DAYS omitted  -> applies every day",
    "#   DAYS examples -> fri | mon-fri | sat,sun | daily",
]

DAILY_HEADER = [
    "# OFLIT Child Time Limit - per-weekday daily limit overrides",
    "#",
    "# Managed by `child-time set USER DURATION --day DAY`.",
    "# A weekday listed here overrides that user's base daily limit",
    "# from /etc/child-time-limit.conf for that weekday only.",
    "#",
    "# Format: DAYS=SECONDS",
]


def _serialize_schedule_policy(policy, header):
    lines = list(header)

    for username in sorted(policy):
        slots = sorted(
            policy[username],
            key=lambda item: (item.start_minute, item.end_minute, item.slot_id),
        )
        if not slots:
            continue
        lines.append("")
        lines.append(f"[{username}]")
        for slot in slots:
            lines.append(f"{slot.slot_id}={slot.quota_seconds}")

    return "\n".join(lines) + "\n"


def _serialize_daily_overrides(overrides, header):
    lines = list(header)

    for username in sorted(overrides):
        user_days = overrides[username]
        if not user_days:
            continue
        lines.append("")
        lines.append(f"[{username}]")
        # Group weekdays that share a value so the file stays compact and
        # round-trips through parse_weekday_expression unchanged.
        by_value = {}
        for weekday, seconds in user_days.items():
            by_value.setdefault(int(seconds), set()).add(int(weekday))
        for seconds in sorted(by_value):
            lines.append(f"{format_weekday_set(by_value[seconds])}={seconds}")

    return "\n".join(lines) + "\n"


def _resolve_policy_paths(config_path=None, schedule_path=None, daily_path=None):
    """Resolve the three policy files as one coherent set.

    Passing a custom config_path keeps an entire policy tree isolated
    (used by tests and by `--config`), while production callers that
    pass nothing continue to use the authoritative /etc files.
    """
    if config_path is None:
        config_path = CONFIG
        if schedule_path is None:
            schedule_path = SCHEDULE_CONFIG
        if daily_path is None:
            daily_path = DAILY_CONFIG
    else:
        config_path = Path(config_path)
        if schedule_path is None:
            schedule_path = config_path.parent / SCHEDULE_CONFIG.name
        if daily_path is None:
            daily_path = config_path.parent / DAILY_CONFIG.name

    return Path(config_path), Path(schedule_path), Path(daily_path)


def load_effective_policy(
    config_path=None,
    schedule_path=None,
    daily_path=None,
):
    """Load limits, weekday overrides and schedules as one validated view."""
    config_path, schedule_path, daily_path = _resolve_policy_paths(
        config_path, schedule_path, daily_path
    )

    _, limits, _ = load_config(config_path)
    overrides = load_daily_overrides(path=daily_path, limits=limits)
    schedules = load_schedule_policy(
        path=schedule_path,
        limits=limits,
        overrides=overrides,
    )

    return limits, overrides, schedules


def weekly_policy_view(username, limits, overrides, schedules):
    """Return the per-weekday effective policy for one user.

    This is what `child-time limits` prints, so administrators inspect
    exactly the numbers the enforcer will use.
    """
    if username not in limits:
        raise ChildTimeError(f"User is not configured: {username}")

    view = []
    for weekday in sorted(ALL_WEEKDAYS):
        limit = effective_daily_limit(username, weekday, limits, overrides)
        applicable = slots_for_weekday(schedules.get(username, []), weekday)
        view.append((weekday, limit, applicable))

    return view


def mutate_schedule_policy(
    username,
    mutate,
    config_path=None,
    schedule_path=None,
    daily_path=None,
):
    """Apply one schedule mutation transactionally.

    CRITICAL INVARIANT: the limits, the weekday overrides and the
    schedule file are all re-read *after* the exclusive policy lock is
    taken, the mutated policy is fully re-validated, and only then is
    the file replaced atomically. A mutation that fails validation
    leaves every policy file byte-for-byte unchanged.

    The lock is taken on the daily-limit config, which is the same lock
    apply_limit_transaction uses, so a concurrent `set` and
    `schedule add` can never interleave into an invalid combination.

    `mutate` receives the mutable {username: [ScheduledSlot]} policy and
    returns a ScheduleMutationResult.
    """
    validate_username(username)

    config_path, schedule_path, daily_path = _resolve_policy_paths(
        config_path, schedule_path, daily_path
    )

    with policy_lock(config_path):
        _, limits, _ = load_config(config_path)

        if username not in limits:
            raise ChildTimeError(f"User is not configured: {username}")

        overrides = load_daily_overrides(path=daily_path, limits=limits)
        policy = load_schedule_policy(
            path=schedule_path,
            limits=limits,
            overrides=overrides,
        )
        policy.setdefault(username, [])

        result = mutate(policy)

        # Re-validate the whole user before anything touches the disk.
        validate_weekly_schedule(
            username,
            policy[username],
            limits,
            overrides,
        )

        seen = set()
        for slot in policy[username]:
            if slot.slot_id in seen:
                raise ChildTimeError(
                    f"Duplicate schedule identifier for {username}: "
                    f"{slot.slot_id}"
                )
            seen.add(slot.slot_id)

        payload = _serialize_schedule_policy(
            policy,
            _config_header(schedule_path, SCHEDULE_HEADER),
        )
        _atomic_write_text(schedule_path, payload)

    return result


def build_schedule_slot(username, days, start, end, quota_seconds):
    """Validate one schedule's fields and build its ScheduledSlot."""
    day_set = (
        ALL_WEEKDAYS
        if days is None
        else parse_weekday_expression(days)
    )

    start_text, start_minute = _clock_to_minute(start)
    end_text, end_minute = _clock_to_minute(end)

    if start_minute >= end_minute:
        raise ChildTimeError(
            f"Schedule start must be earlier than end: {start_text}-{end_text}"
        )

    try:
        quota_seconds = int(quota_seconds)
    except (TypeError, ValueError) as exc:
        raise ChildTimeError("Scheduled quota must be an integer.") from exc

    if quota_seconds <= 0:
        raise ChildTimeError("Scheduled quota must be greater than zero.")

    wall_seconds = (end_minute - start_minute) * 60
    if quota_seconds > wall_seconds:
        raise ChildTimeError(
            f"Scheduled quota ({human(quota_seconds)}) exceeds the length of "
            f"{start_text}-{end_text} ({human(wall_seconds)})."
        )

    return ScheduledSlot(
        username=username,
        start_minute=start_minute,
        end_minute=end_minute,
        quota_seconds=quota_seconds,
        slot_id=schedule_slot_id(day_set, start_text, end_text),
        days=day_set,
    )


def add_schedule(
    username,
    days,
    start,
    end,
    quota_seconds,
    config_path=None,
    schedule_path=None,
    daily_path=None,
):
    """Add one schedule for a user."""
    slot = build_schedule_slot(username, days, start, end, quota_seconds)

    def mutate(policy):
        for existing in policy[username]:
            if existing.slot_id == slot.slot_id:
                raise ChildTimeError(
                    f"Schedule already exists for {username}: {slot.slot_id}"
                )
        policy[username].append(slot)
        return ScheduleMutationResult(username, "added", slot.slot_id)

    return mutate_schedule_policy(
        username,
        mutate,
        config_path=config_path,
        schedule_path=schedule_path,
        daily_path=daily_path,
    )


def edit_schedule(
    username,
    slot_id,
    days=None,
    start=None,
    end=None,
    quota_seconds=None,
    config_path=None,
    schedule_path=None,
    daily_path=None,
):
    """Edit one existing schedule, identified by its stable identifier.

    Fields left as None keep their current value. Changing the days or
    the window changes the schedule's identity, and therefore its
    identifier; the result reports both the old and the new one.
    """
    if days is None and start is None and end is None and quota_seconds is None:
        raise ChildTimeError(
            "Nothing to change. Pass at least one of --days, --start, "
            "--end or --quota."
        )

    def mutate(policy):
        slots = policy[username]
        matches = [
            index
            for index, slot in enumerate(slots)
            if slot.slot_id == slot_id
        ]

        if not matches:
            known = ", ".join(sorted(slot.slot_id for slot in slots)) or "none"
            raise ChildTimeError(
                f"No schedule {slot_id!r} for {username}. "
                f"Known schedules: {known}."
            )

        index = matches[0]
        current = slots[index]

        updated = build_schedule_slot(
            username,
            format_weekday_set(current.days) if days is None else days,
            f"{current.start_minute // 60:02d}:{current.start_minute % 60:02d}"
            if start is None
            else start,
            f"{current.end_minute // 60:02d}:{current.end_minute % 60:02d}"
            if end is None
            else end,
            current.quota_seconds if quota_seconds is None else quota_seconds,
        )

        for position, slot in enumerate(slots):
            if position != index and slot.slot_id == updated.slot_id:
                raise ChildTimeError(
                    f"Schedule already exists for {username}: {updated.slot_id}"
                )

        slots[index] = updated
        return ScheduleMutationResult(
            username,
            "edited",
            updated.slot_id,
            previous_slot_id=slot_id,
        )

    return mutate_schedule_policy(
        username,
        mutate,
        config_path=config_path,
        schedule_path=schedule_path,
        daily_path=daily_path,
    )


def remove_schedule(
    username,
    slot_id,
    config_path=None,
    schedule_path=None,
    daily_path=None,
):
    """Remove one schedule by its stable identifier."""

    def mutate(policy):
        slots = policy[username]
        remaining = [slot for slot in slots if slot.slot_id != slot_id]

        if len(remaining) == len(slots):
            known = ", ".join(sorted(slot.slot_id for slot in slots)) or "none"
            raise ChildTimeError(
                f"No schedule {slot_id!r} for {username}. "
                f"Known schedules: {known}."
            )

        policy[username] = remaining
        return ScheduleMutationResult(username, "removed", slot_id)

    return mutate_schedule_policy(
        username,
        mutate,
        config_path=config_path,
        schedule_path=schedule_path,
        daily_path=daily_path,
    )


def apply_daily_override_transaction(
    username,
    weekday,
    new_limit,
    clear=False,
    config_path=None,
    schedule_path=None,
    daily_path=None,
    state_dir=None,
):
    """Set or clear one weekday's daily-limit override, transactionally.

    Same invariant as mutate_schedule_policy: everything is re-read
    under the exclusive policy lock, the candidate override is validated
    against that weekday's schedules, and the file is only replaced once
    validation has passed.
    """
    validate_username(username)

    config_path, schedule_path, daily_path = _resolve_policy_paths(
        config_path, schedule_path, daily_path
    )

    try:
        weekday = int(weekday)
    except (TypeError, ValueError) as exc:
        raise ChildTimeError("Weekday must be an integer.") from exc

    if weekday not in ALL_WEEKDAYS:
        raise ChildTimeError(
            f"Weekday must be between 0 (Monday) and 6 (Sunday): {weekday}"
        )

    with policy_lock(config_path):
        _, limits, _ = load_config(config_path)

        if username not in limits:
            raise ChildTimeError(f"User is not configured: {username}")

        overrides = load_daily_overrides(path=daily_path, limits=limits)
        schedules = load_schedule_policy(
            path=schedule_path,
            limits=limits,
            overrides=overrides,
        )

        previous = effective_daily_limit(
            username, weekday, limits, overrides
        )

        candidate = dict(overrides)
        user_days = dict(candidate.get(username, {}))

        if clear:
            user_days.pop(weekday, None)
            resolved = int(limits[username])
        else:
            try:
                resolved = int(new_limit)
            except (TypeError, ValueError) as exc:
                raise ChildTimeError("Daily limit must be an integer.") from exc
            if resolved <= 0:
                raise ChildTimeError("Daily limit must be greater than zero.")
            user_days[weekday] = resolved

        candidate[username] = user_days

        # A cleared override falls back to the base limit, which still
        # has to cover that weekday's schedules.
        validate_daily_override_ceiling(
            username,
            weekday,
            resolved,
            schedules,
        )

        payload = _serialize_daily_overrides(
            candidate,
            _config_header(daily_path, DAILY_HEADER),
        )
        _atomic_write_text(daily_path, payload)

    used = read_used(username, state_dir=state_dir)

    return LimitMutationResult(
        username=username,
        used=used,
        old_limit=previous.seconds,
        new_limit=resolved,
        remaining=max(0, resolved - used),
        reason=(
            f"cleared {WEEKDAY_NAMES[weekday]} override"
            if clear
            else f"{WEEKDAY_NAMES[weekday]} override"
        ),
    )


def validate_policy(config_path=None, schedule_path=None, daily_path=None):
    """Validate every policy file and return a human-readable report.

    Raises ChildTimeError on the first problem found, so a non-zero exit
    from `child-time validate` means the policy is not safe to activate.
    """
    config_path, schedule_path, daily_path = _resolve_policy_paths(
        config_path, schedule_path, daily_path
    )

    limits, overrides, schedules = load_effective_policy(
        config_path=config_path,
        schedule_path=schedule_path,
        daily_path=daily_path,
    )

    start, end = load_access_window(config_path.parent / ACCESS_CONFIG.name)

    for username in sorted(schedules):
        validate_weekly_schedule(
            username,
            schedules[username],
            limits,
            overrides,
        )

    for username in sorted(overrides):
        for weekday in sorted(overrides[username]):
            validate_daily_override_ceiling(
                username,
                weekday,
                overrides[username][weekday],
                schedules,
            )

    return {
        "config": str(config_path),
        "schedule_config": str(schedule_path),
        "daily_config": str(daily_path),
        "access_window": (start, end),
        "users": sorted(limits),
        "scheduled_users": sorted(
            name for name, slots in schedules.items() if slots
        ),
        "override_users": sorted(
            name for name, days in overrides.items() if days
        ),
        "schedule_count": sum(len(slots) for slots in schedules.values()),
        "override_count": sum(len(days) for days in overrides.values()),
    }


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
    daily_path=None,
    weekday=None,
):
    """Today's usage against today's effective policy.

    The weekday is resolved once here and used for both the effective
    daily limit and the applicable schedules, so a status row can never
    mix one day's limit with another day's schedules.
    """
    config_path, schedule_path, daily_path = _resolve_policy_paths(
        config_path, schedule_path, daily_path
    )

    _, limits, _ = load_config(config_path)

    if selected is not None:
        if selected not in limits:
            raise ChildTimeError(f"User is not configured: {selected}")
        names = [selected]
    else:
        names = sorted(limits)

    if weekday is None:
        weekday = local_now().weekday()

    overrides = load_daily_overrides(path=daily_path, limits=limits)

    schedules = load_schedule_policy(
        path=schedule_path,
        limits=limits,
        overrides=overrides,
    )

    rows = []

    for username in names:
        effective = effective_daily_limit(
            username,
            weekday,
            limits,
            overrides,
        )
        limit = effective.seconds

        legacy_used = read_used(
            username,
            state_dir=state_dir,
        )

        slots = slots_for_weekday(schedules.get(username, []), weekday)

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
                weekday,
                effective.source,
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
    schedule_path=None,
    daily_path=None,
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

    # Keep custom/test policy trees isolated from production.
    # Production callers that omit config_path continue to use the
    # authoritative /etc schedule configuration.
    config_path, schedule_path, daily_path = _resolve_policy_paths(
        config_path, schedule_path, daily_path
    )

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
        overrides = load_daily_overrides(
            path=daily_path,
            limits=candidate_limits,
        )
        schedules = load_schedule_policy(
            path=schedule_path,
            limits=candidate_limits,
            overrides=overrides,
        )
        validate_scheduled_daily_ceiling(
            username,
            new_limit,
            schedules,
            overrides,
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



def validate_scheduled_daily_ceiling(
    username,
    candidate_limit,
    schedules,
    overrides=None,
):
    """Reject a base daily ceiling below what some weekday already schedules.

    Only weekdays that actually fall back to the base limit are checked:
    a weekday carrying its own override is unaffected by a change to the
    base limit, and is validated when that override is changed instead.
    """
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

    user_overrides = (overrides or {}).get(username, {})

    for weekday in sorted(ALL_WEEKDAYS):
        if weekday in user_overrides:
            continue

        applicable = slots_for_weekday(slots, weekday)
        if not applicable:
            continue

        aggregate = sum(slot.quota_seconds for slot in applicable)

        if candidate_limit < aggregate:
            raise ChildTimeError(
                f"Daily limit for {username} cannot be below the "
                f"{WEEKDAY_NAMES[weekday]} scheduled quota aggregate "
                f"({aggregate} seconds)."
            )


def validate_daily_override_ceiling(
    username,
    weekday,
    candidate_limit,
    schedules,
):
    """Reject a weekday override below what that same weekday schedules."""
    validate_username(username)

    try:
        candidate_limit = int(candidate_limit)
    except (TypeError, ValueError):
        raise ChildTimeError("Daily limit must be an integer.")

    if candidate_limit <= 0:
        raise ChildTimeError("Daily limit must be greater than zero.")

    applicable = slots_for_weekday(schedules.get(username, []), weekday)
    if not applicable:
        return

    aggregate = sum(slot.quota_seconds for slot in applicable)

    if candidate_limit < aggregate:
        raise ChildTimeError(
            f"Daily limit for {username} on {WEEKDAY_NAMES[int(weekday)]} "
            f"cannot be below that day's scheduled quota aggregate "
            f"({aggregate} seconds)."
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
