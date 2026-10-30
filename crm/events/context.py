"""Where a change came from, so automations can skip changes they made themselves."""

from contextlib import contextmanager
from contextvars import ContextVar

_origin = ContextVar("crm_event_origin", default=None)


@contextmanager
def event_origin(**values):
    """Mark changes made inside the block, for example ``event_origin(via="api", ...)``."""
    token = _origin.set(values)
    try:
        yield
    finally:
        _origin.reset(token)


def current_origin():
    return dict(_origin.get() or {"via": "crm"})
