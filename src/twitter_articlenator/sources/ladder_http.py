"""Bounded HTTP transport for 13ft, with DNS-pinned public destinations."""

import http.client
import ipaddress
import json
import socket
import time
from contextvars import ContextVar
from types import SimpleNamespace
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

DEADLINE = ContextVar("ladder_deadline", default=None)
MAX_BYTES = 5 * 1024 * 1024


def validate_url(url):
    if not isinstance(url, str) or not url.strip() or len(url) > 8192:
        raise ValueError("Enter a website URL.")
    url = url.strip()
    if any(ord(c) < 32 for c in url) or "\\" in url:
        raise ValueError("Enter a valid HTTP or HTTPS website URL.")
    if "://" not in url:
        url = "https://" + url
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 80, 443}
    ):
        raise ValueError("Enter a public HTTP or HTTPS website URL without credentials.")
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Could not find that website. Check the URL.") from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Only public websites can be opened.")
    return url, host, port, addresses[0][4][0]


def get(url, headers=None, timeout=15, allow_redirects=True, params=None):
    """Requests-compatible subset used by the pinned upstream retrieval core."""
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    for _ in range(6):
        deadline = DEADLINE.get() or (time.monotonic() + 60)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Fetching took too long. Try again later.")
        url, host, port, address = validate_url(url)
        parsed = urlsplit(url)
        connection_type = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_type(host, port, timeout=min(timeout, remaining, 10))
        # Keep the original host for TLS certificate checks, SNI and Host, while
        # connecting only to the public address that was validated above.
        connection._create_connection = lambda target, timeout, source_address=None: (
            socket.create_connection((address, port), timeout, source_address)
        )
        try:
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request(
                "GET", path, headers={**(headers or {}), "Accept-Encoding": "identity"}
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308} and allow_redirects:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("The website returned an invalid redirect.")
                url = urljoin(url, location)
                continue
            content_type = response.getheader("Content-Type", "").lower()
            if content_type and not any(t in content_type for t in ("html", "json", "text/plain")):
                raise ValueError("That URL does not contain a web article.")
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Fetching took too long. Try again later.")
                if connection.sock:
                    connection.sock.settimeout(min(10, remaining))
                chunk = response.read1(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("That page is too large to open (5 MB limit).")
            if response.status in {404, 410}:
                raise ValueError("That article could not be found.")
            encoding = response.headers.get_content_charset() or "utf-8"
            try:
                text = data.decode(encoding, errors="replace")
            except LookupError:
                text = data.decode("utf-8", errors="replace")
            return SimpleNamespace(
                status_code=response.status,
                text=text,
                url=url,
                apparent_encoding=encoding,
                encoding=encoding,
                json=lambda: json.loads(text),
            )
        finally:
            connection.close()
    raise ValueError("The website redirected too many times.")
