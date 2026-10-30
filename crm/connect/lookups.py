"""What core asks lookup connectors to check.

Some sources answer questions instead of sending new records: a case status
source reports the status of the receipt numbers it is asked about. The core
feature that owns those items registers a function listing the ones due for a
check, and connectors call ``due()`` from ``sync()``. Connectors never query
core models themselves.
"""

_providers = {}


def provide(capability, function):
    """Register ``function(limit=...)``, which returns the items due for a check, oldest first."""
    _providers[capability] = function


def due(capability, *, limit):
    """Up to ``limit`` items core wants checked for this capability, or none."""
    function = _providers.get(capability)
    return list(function(limit=limit))[:limit] if function else []
