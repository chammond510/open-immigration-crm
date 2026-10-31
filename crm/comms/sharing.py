"""Keeping communications out of AI features, the MCP server, and translation.

Some providers' terms limit sharing what they bring in with other services, such as
AI models and translation services. Their connectors say so in ``sharing_limits``.
The communications such a connection brought in are then left out of everything that
sends text to an AI model or a translation service: the MCP server and the matter
assistant's tools, task suggestions, consult prep memos, and machine translation.
Someone who manages connections may allow it for one connection, after the firm has
weighed the terms.
"""

from crm.connect.models import Connection


def withheld_connections():
    """Connections whose communications are kept out, by primary key, with the reason."""
    return {
        connection.pk: cls.sharing_limits
        for connection in Connection.objects.filter(ai_use_allowed=False)
        if (cls := connection.connector_class) is not None and cls.sharing_limits
    }


def shareable(communications, withheld=None):
    """The communications that may be sent to AI models and translation services."""
    withheld = withheld_connections() if withheld is None else withheld
    return (
        communications.exclude(source_connection__in=list(withheld)) if withheld else communications
    )


def withheld_reason(communication, withheld=None):
    """Why a communication is kept out of AI and translation, or "" when it is not."""
    withheld = withheld_connections() if withheld is None else withheld
    return withheld.get(communication.source_connection_id, "")
