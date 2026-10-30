"""One HTTPS client for every connector.

It adds timeouts, retries that honour ``Retry-After`` on 429 and 5xx answers, a
response size limit, and a pagination helper. Failures become ``ConnectorError``
messages that are safe to show staff. Tests swap the transport for
``httpx.MockTransport`` with ``use_transport``; nothing reaches the network.
"""

import ipaddress
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from django.utils import timezone

from .base import ConnectorError

DEFAULT_TIMEOUT = 15
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_WAIT = 60
USER_AGENT = "OpenImmigrationCRM/1"

# Replaced in tests; see ``use_transport``.
_transport = None
sleep = time.sleep


@contextmanager
def use_transport(transport):
    """Send every request through ``transport``, such as ``httpx.MockTransport(handler)``."""
    global _transport
    previous, _transport = _transport, transport
    try:
        yield
    finally:
        _transport = previous


def is_local(host):
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def check_url(url, *, allow_local_http=False):
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if allow_local_http and parts.scheme == "http" and is_local(parts.hostname or ""):
        return
    raise ConnectorError("Provider addresses must use HTTPS.")


def retry_wait(response, attempt):
    """Seconds to wait before retrying: the provider's Retry-After, or a backoff."""
    value = response.headers.get("Retry-After", "") if response is not None else ""
    if value.isdigit():
        return min(int(value), MAX_RETRY_WAIT)
    if value:
        try:
            when = parsedate_to_datetime(value)
            return min(max((when - timezone.now()).total_seconds(), 0), MAX_RETRY_WAIT)
        except (TypeError, ValueError):
            pass
    return min(2**attempt, MAX_RETRY_WAIT)


class ProviderClient:
    """A small wrapper around ``httpx.Client`` for one provider."""

    def __init__(
        self,
        base_url="",
        *,
        name="The provider",
        headers=None,
        auth=None,
        timeout=DEFAULT_TIMEOUT,
        retries=3,
        allow_local_http=False,
        max_bytes=MAX_RESPONSE_BYTES,
    ):
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self.auth = auth
        self.timeout = timeout
        self.retries = retries
        self.allow_local_http = allow_local_http
        self.max_bytes = max_bytes

    def url(self, path):
        return path if path.startswith(("http://", "https://")) else self.base_url + path

    def request(self, method, path, *, expected=(200, 201, 202, 204), **kwargs):
        """Send a request and return the ``httpx.Response``; raise ConnectorError on failure."""
        url = self.url(path)
        check_url(url, allow_local_http=self.allow_local_http)
        headers = {**self.headers, **kwargs.pop("headers", {})}
        attempt = 0
        with httpx.Client(
            timeout=self.timeout, transport=_transport, follow_redirects=False
        ) as client:
            while True:
                try:
                    with client.stream(
                        method, url, headers=headers, auth=self.auth, **kwargs
                    ) as streamed:
                        response = httpx.Response(
                            streamed.status_code,
                            headers=streamed.headers,
                            content=self.read_limited(streamed),
                            request=streamed.request,
                        )
                except httpx.TimeoutException:
                    response, failure = None, f"{self.name} did not answer in time."
                except httpx.HTTPError:
                    response, failure = None, f"{self.name} could not be reached."
                else:
                    if response.status_code in expected:
                        return response
                    failure = f"{self.name} answered HTTP {response.status_code}."
                    if response.status_code not in RETRY_STATUSES:
                        raise ConnectorError(failure, status=response.status_code)
                if attempt >= self.retries:
                    raise ConnectorError(
                        failure, status=response.status_code if response is not None else None
                    )
                sleep(retry_wait(response, attempt))
                attempt += 1

    def read_limited(self, response):
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self.max_bytes:
                raise ConnectorError(f"{self.name} sent a response that was too large.")
            chunks.append(chunk)
        return b"".join(chunks)

    def json(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise ConnectorError(f"{self.name} sent a response that was not understood.") from None

    def get(self, path, **kwargs):
        return self.json("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.json("POST", path, **kwargs)

    def paginate(self, path, *, items, next_request, params=None, limit=10_000):
        """Yield items across pages.

        ``items(page)`` returns a page's items. ``next_request(page)`` returns the next
        page's ``(path, params)``, or None on the last page. Stops after ``limit``
        items as a guard against a provider that never ends.
        """
        count = 0
        while path:
            page = self.get(path, params=params)
            for item in items(page):
                yield item
                count += 1
                if count >= limit:
                    return
            following = next_request(page)
            path, params = following if following else (None, None)
