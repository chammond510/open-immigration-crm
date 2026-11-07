"""Access logs deliberately exclude URLs, referrers, cookies, and other headers.

Intake, invitation, reset, and webhook paths contain bearer secrets. Search query
strings can contain client information. Keep operational metrics without either.
"""

from gunicorn.glogging import Logger

from crm.logging import PrivateGunicornRequestFilter


class PrivateLogger(Logger):
    def setup(self, cfg):
        super().setup(cfg)
        self.error_log.addFilter(PrivateGunicornRequestFilter())


logger_class = "config.gunicorn.PrivateLogger"
access_log_format = '%(h)s %(t)s "%(m)s" %(s)s %(b)s %(L)s'
