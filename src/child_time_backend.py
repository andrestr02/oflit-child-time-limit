"""Transport-agnostic administration backend for OFLIT Child Time Limit.

Wraps child_time_core + child_time_operations behind a small, stable
Python API intended for a privileged D-Bus service
(child_time_dbus_service, installed as child-time-backend) to call.

This module contains:
  - no D-Bus code
  - no Polkit code
  - no argparse / CLI printing / sys.exit
  - no subprocess / shell execution / systemctl calls
  - no caller-controlled filesystem paths

Every operation always targets the single production config/state
location owned by child_time_core (its module-level CONFIG/STATE_DIR
defaults). Callers can never redirect a privileged operation at an
arbitrary filesystem path, because no method here accepts one.

Authorization is NOT decided here. The caller (the D-Bus transport
layer) must have already authorized the operation via Polkit before
calling any mutating method. This module only classifies domain
failures into a small, stable set of exception types so the transport
layer can map them to deterministic, machine-readable D-Bus errors.
"""

import importlib.machinery
import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve()
_LIB_DIR_CANDIDATES = (
    _SCRIPT_PATH.parent,
    Path("/usr/local/lib/child-time-limit"),
)


def _load_sibling_or_installed(filename, module_name):
    """Resolve a project module with the same source-tree-wins-over-
    installed precedence used throughout this project's entry points."""
    for directory in _LIB_DIR_CANDIDATES:
        candidate = directory / filename
        if candidate.is_file():
            loader = importlib.machinery.SourceFileLoader(module_name, str(candidate))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            if spec is None:
                raise RuntimeError(f"Cannot create import spec for {candidate}")
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
    raise ImportError(
        f"Cannot locate {filename} in any of: "
        + ", ".join(str(d) for d in _LIB_DIR_CANDIDATES)
    )


core = _load_sibling_or_installed("child_time_core.py", "child_time_core")
ops = _load_sibling_or_installed("child_time_operations.py", "child_time_operations")

# child_time_operations.py resolves child_time_core.py itself, independently
# of the load above, which would otherwise produce a second, distinct
# ChildTimeError class object -- making `except core.ChildTimeError` below
# silently fail to catch exceptions raised through `ops`. Force both
# modules to share this exact loaded core instance.
ops.core = core


class BackendError(Exception):
    """Base class for all backend-domain errors.

    `code` is a stable, machine-readable identifier the transport layer
    uses to pick a D-Bus error name. It must never change meaning once
    published, only gain new siblings.
    """

    code = "internal-error"


class UnknownUserError(BackendError):
    code = "unknown-user"


class InvalidDurationError(BackendError):
    code = "invalid-duration"


class InvalidLimitError(BackendError):
    code = "invalid-limit"


class ReductionRequiresForceError(BackendError):
    code = "reduction-requires-force"


class AuthorizationDeniedError(BackendError):
    """Raised by the transport layer, not by this module, when Polkit
    denies an operation. Defined here so both layers share one error
    vocabulary."""

    code = "authorization-denied"


class MalformedRequestError(BackendError):
    """Raised by the transport layer for requests that are structurally
    invalid before they ever reach domain logic (e.g. an unidentifiable
    caller). Defined here for the same reason as AuthorizationDeniedError."""

    code = "malformed-request"


class InternalError(BackendError):
    code = "internal-error"


def _classify(phase, exc):
    text = str(exc)

    if phase == "validate_username":
        return UnknownUserError(text)

    if phase == "duration":
        return InvalidDurationError(text)

    if phase == "status":
        if "not configured" in text:
            return UnknownUserError(text)
        return InternalError(text)

    if phase == "transaction":
        if text.startswith("Refusing to set"):
            return ReductionRequiresForceError(text)
        if "not configured" in text:
            return UnknownUserError(text)
        if "must be greater than zero" in text or "zero or negative" in text:
            return InvalidLimitError(text)
        return InternalError(text)

    return InternalError(text)


class ChildTimeBackend:
    """Pure administration operations.

    The caller (the D-Bus transport layer) is responsible for
    authorization *before* invoking any method here. Every method either
    returns a child_time_core value (StatusRow list / LimitMutationResult)
    or raises a BackendError subclass -- never a bare child_time_core.
    ChildTimeError.
    """

    def _call(self, phase, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except core.ChildTimeError as exc:
            raise _classify(phase, exc) from exc

    def status(self, username=None):
        """Return a list of core.StatusRow for one user, or all configured
        users if `username` is falsy."""
        target = username or None
        if target is not None:
            self._call("validate_username", core.validate_username, target)
        return self._call("status", core.status_rows, selected=target)

    def set_limit(self, username, duration_text, force=False):
        self._call("validate_username", core.validate_username, username)
        limit, reason = self._call("duration", ops.build_set, duration_text)
        return self._call(
            "transaction",
            core.apply_limit_transaction,
            username,
            limit,
            force=force,
            reason=reason,
        )

    def add_limit(self, username, duration_text):
        self._call("validate_username", core.validate_username, username)
        calculate_limit, reason = self._call("duration", ops.build_add, duration_text)
        return self._call(
            "transaction",
            core.apply_limit_transaction,
            username,
            calculate_limit,
            reason=reason,
        )

    def subtract_limit(self, username, duration_text, force=False):
        self._call("validate_username", core.validate_username, username)
        calculate_limit, reason = self._call(
            "duration", ops.build_subtract, duration_text
        )
        return self._call(
            "transaction",
            core.apply_limit_transaction,
            username,
            calculate_limit,
            force=force,
            reason=reason,
        )

    def until(self, username, clock_text):
        self._call("validate_username", core.validate_username, username)
        calculate_limit, reason = self._call("duration", ops.build_until, clock_text)
        return self._call(
            "transaction",
            core.apply_limit_transaction,
            username,
            calculate_limit,
            reason=reason,
        )
