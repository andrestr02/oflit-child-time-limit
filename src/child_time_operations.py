"""Shared per-command policy-calculation semantics.

Factored out so any privileged front end (today: nothing; planned: the
D-Bus backend in child_time_backend.py) that needs to compute a new daily
limit for `set` / `add` / `subtract` / `until` semantics can reuse
identical calculation logic instead of reimplementing it inline.

This module depends only on the public API of child_time_core. It
contains no argparse, D-Bus, Polkit, or presentation code, and mutates
nothing itself: it only builds the `calculate_limit` value/callable that
a caller passes to `child_time_core.apply_limit_transaction`. The actual
transactional invariant (lock, reload, validate, read usage, guard,
atomic replace) lives entirely in that one function in child_time_core.
"""

import importlib.machinery
import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve()
_SIBLING_CORE = _SCRIPT_PATH.parent / "child_time_core.py"
_INSTALLED_CORE = Path("/usr/local/lib/child-time-limit/child_time_core.py")


def _load_core():
    """Resolve child_time_core.py with the same precedence used by the CLI:
    an available source-tree sibling always wins over the installed copy."""
    if _SIBLING_CORE.is_file():
        core_path = _SIBLING_CORE
    elif _INSTALLED_CORE.is_file():
        core_path = _INSTALLED_CORE
    else:
        raise ImportError(
            "Cannot locate child_time_core.py beside "
            f"{_SCRIPT_PATH} or at {_INSTALLED_CORE}"
        )

    loader = importlib.machinery.SourceFileLoader("child_time_core", str(core_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"Cannot create import spec for {core_path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


core = _load_core()


def build_set(duration_text):
    """Return (new_limit, reason) for an absolute 'set' operation."""
    return core.parse_duration(duration_text), None


def build_add(duration_text):
    """Return (calculate_limit, reason) for an 'add' operation."""
    delta = core.parse_duration(duration_text)
    reason = f"add {core.human(delta)}"

    def calculate_limit(old_limit, used):
        return old_limit + delta

    return calculate_limit, reason


def build_subtract(duration_text):
    """Return (calculate_limit, reason) for a 'subtract' operation."""
    delta = core.parse_duration(duration_text)
    reason = f"subtract {core.human(delta)}"

    def calculate_limit(old_limit, used):
        new_limit = old_limit - delta
        if new_limit <= 0:
            raise core.ChildTimeError(
                "Subtract would make the daily limit zero or negative."
            )
        return new_limit

    return calculate_limit, reason


def build_until(clock_text, now=None):
    """Return (calculate_limit, reason) for an 'until' operation.

    Mirrors the CLI's semantics exactly: the wall-clock target is
    converted into an amount of remaining active-use quota at build time
    (outside any lock), but the new limit is still computed from
    authoritative usage inside the transaction lock, since
    `calculate_limit` only runs after `apply_limit_transaction` acquires
    the policy lock and re-reads usage.
    """
    now = now or core.local_now()
    target = core.parse_clock(clock_text, now=now)
    seconds_until = int((target - now).total_seconds())
    reason = f"continuous active use until {target.strftime('%H:%M')}"

    def calculate_limit(old_limit, used):
        return used + seconds_until

    return calculate_limit, reason
