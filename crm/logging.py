"""Keep request URLs and exception messages out of HTTP diagnostic logs."""

import logging


class PrivateRequestFormatter(logging.Formatter):
    def format(self, record):
        status = getattr(record, "status_code", None)
        status = status if isinstance(status, int) else "unknown"
        error = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "none"
        # Do not format record.msg/args/traceback: Django's request, security, and
        # runserver messages can contain complete bearer URLs or submitted values.
        return (
            f"{self.formatTime(record)} {record.levelname} {record.name} "
            f"status={status} error={error}"
        )


class PrivateGunicornRequestFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if message.startswith(("Error handling request", "Invalid request from ip=")):
            error = (
                record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else "none"
            )
            record.msg = f"HTTP request failed; error={error}"
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True
