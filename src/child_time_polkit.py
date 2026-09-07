"""Polkit authorization boundary for the OFLIT Child Time Limit D-Bus backend.

This module is the ONLY place in the project allowed to know about
Polkit. child_time_core, child_time_operations, and child_time_backend
never import it and never make an authorization decision themselves.

It wraps a CheckAuthorization call to org.freedesktop.PolicyKit1 over the
system bus. The design fails closed: any error talking to Polkit, any
unexpected reply shape, or any missing caller identity is treated as
"not authorized" -- never as "authorized by default".

Construction is split so tests never need a real system bus or a real
Polkit daemon: `PolkitAuthority(authority)` takes anything exposing a
`CheckAuthorization(...)` method with Polkit's D-Bus signature, and
`PolkitAuthority.system()` is the only place that touches a real
`dbus.SystemBus()`.
"""

import dbus

POLKIT_BUS_NAME = "org.freedesktop.PolicyKit1"
POLKIT_OBJECT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_INTERFACE = "org.freedesktop.PolicyKit1.Authority"

# AllowUserInteraction flag from the Polkit CheckAuthorizationFlags enum.
_ALLOW_USER_INTERACTION = 1


class PolkitAuthority:
    def __init__(self, authority):
        self._authority = authority

    @classmethod
    def system(cls):
        bus = dbus.SystemBus()
        proxy = bus.get_object(POLKIT_BUS_NAME, POLKIT_OBJECT_PATH)
        authority = dbus.Interface(proxy, dbus_interface=POLKIT_INTERFACE)
        return cls(authority)

    def check_authorization(self, sender_unique_name, action_id, allow_interactive=True):
        """Return True only if Polkit explicitly grants authorization.

        `sender_unique_name` must be the D-Bus unique connection name
        (e.g. ":1.42") of the caller, as delivered by dbus-python's
        `sender_keyword` on the service method -- never a caller-supplied
        value, since that would let a caller claim to be anyone.
        """
        if not sender_unique_name:
            return False

        subject = ("system-bus-name", {"name": sender_unique_name})
        details = {}
        flags = _ALLOW_USER_INTERACTION if allow_interactive else 0

        try:
            result = self._authority.CheckAuthorization(
                subject, action_id, details, flags, "",
            )
        except Exception:
            # Any transport/Polkit failure fails closed.
            return False

        try:
            is_authorized = bool(result[0])
        except (IndexError, TypeError, KeyError):
            return False

        return is_authorized
