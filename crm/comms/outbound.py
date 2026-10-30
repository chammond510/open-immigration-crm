"""Which connections a staff member can send messages through."""

from crm.connect.models import Connection
from crm.connect.services import visible_connections


def connections_for(user, action, capability):
    """Active connections with this outbound action that ``user`` may use: firm and own."""
    found = []
    for connection in visible_connections(user).filter(status=Connection.Status.ACTIVE):
        cls = connection.connector_class
        if (
            cls is not None
            and capability in connection.enabled_capabilities
            and cls.outbound_actions.get(action) == capability
            and connection.owner_id in (None, user.pk)
        ):
            found.append(connection)
    return found
