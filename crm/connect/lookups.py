"""What core tells connectors, so they never query core models themselves.

Some sources answer questions instead of sending new records: a case status
source reports the status of the receipt numbers it is asked about. The core
feature that owns those items registers a function listing the ones due for a
check, and connectors call ``due()`` from ``sync()``.

Core also registers named option lists, such as the contact fields a lead source
can fill, which connectors offer as setting choices through ``options()``.
"""

_providers = {}
_options = {}


def provide(capability, function):
    """Register ``function(limit=..., **filters)``, which returns the items due for a check,
    oldest first."""
    _providers[capability] = function


def due(capability, *, limit, **filters):
    """Up to ``limit`` items core wants checked for this capability, or none.

    ``filters`` narrow them where the capability allows, such as ``carrier`` for shipping.
    """
    function = _providers.get(capability)
    return list(function(limit=limit, **filters))[:limit] if function else []


def provide_options(name, function):
    """Register ``function()``, which returns ``(value, label)`` choices for a setting."""
    _options[name] = function


def options(name):
    """The choices core offers under ``name``, or none."""
    function = _options.get(name)
    return list(function()) if function else []
