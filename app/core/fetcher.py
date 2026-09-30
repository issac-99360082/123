"""HTTP fetching with safety and crawler etiquette built in.

Three things this module refuses to do, by design:

1. Reach private/loopback/link-local addresses (SSRF guard). A monitoring
   service takes arbitrary URLs from users, which makes it a perfect SSRF
   pivot into the host's own network and into cloud metadata endpoints.
   Resolution happens here and every resolved IP is checked before connecting.
2. Ignore robots.txt. Enabled by default; the operator can disable it globally
   but the shipped default respects it.
3. Hammer a host. A process-wide per-host throttle enforces a minimum gap
   between requests to the same origin.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

_DEADLINE_MESSAGE = "Check exceeded its total time budget (redirects and throttling included)."

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_METHODS = {"GET", "HEAD", "POST"}

# Headers we never let a user set: they would let a monitor impersonate the
# service or smuggle a different target host past the SSRF check.
BLOCKED_REQUEST_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}


class FetchError(Exception):
    """A check failed. `kind` is a stable machine-readable reason."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class FetchResult:
    url: str
    status_code: int
    headers: dict[str, str]
    body: str
    elapsed_ms: float
    truncated: bool


# --------------------------------------------------------------------------
# URL validation / SSRF guard
# --------------------------------------------------------------------------

def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError("dns_error", f"Could not resolve host {host!r}: {exc}") from exc
    return sorted({info[4][0] for info in infos})


def validate_target_url(url: str) -> str:
    """Validate a user-supplied URL and return its normalised form.

    Raises FetchError with a stable `kind` on anything we will not fetch.
    """
    parsed = urlparse(url.strip())

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError("invalid_url", "Only http:// and https:// URLs can be monitored.")
    if not parsed.hostname:
        raise FetchError("invalid_url", "URL is missing a hostname.")
    if len(url) > 2048:
        raise FetchError("invalid_url", "URL exceeds 2048 characters.")

    host = parsed.hostname
    if settings.allow_private_network_targets:
        return urlunparse(parsed)

    # A literal private IP is rejected outright...
    try:
        ipaddress.ip_address(host)
        if not _is_public_ip(host):
            raise FetchError("blocked_target", f"Target {host} is not a public address.")
        return urlunparse(parsed)
    except ValueError:
        pass  # hostname, not a literal IP - resolve it

    # ...and so is any hostname that resolves to one (DNS rebinding / metadata).
    for ip in resolve_host(host):
        if not _is_public_ip(ip):
            raise FetchError(
                "blocked_target",
                f"Host {host!r} resolves to non-public address {ip}; refusing to fetch.",
            )
    return urlunparse(parsed)


def sanitize_headers(headers: dict | None) -> dict[str, str]:
    if not headers:
        return {}
    clean: dict[str, str] = {}
    for key, value in headers.items():
        k = str(key).strip()
        if not k or k.lower() in BLOCKED_REQUEST_HEADERS:
            continue
        if "\n" in k or "\r" in str(value):  # header injection
            continue
        clean[k] = str(value)[:1024]
    return clean


# --------------------------------------------------------------------------
# Per-host throttle
# --------------------------------------------------------------------------

class HostThrottle:
    """Enforces a minimum gap between requests to the same origin."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, max_wait: float = 30.0) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host)
            delay = 0.0
            if last is not None:
                delay = max(0.0, self.min_interval - (now - last))
            self._last[host] = now + delay
        if delay > 0:
            time.sleep(min(delay, max_wait))


_throttle = HostThrottle(settings.min_seconds_between_hits_per_host)


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

class RobotsCache:
    """robots.txt cache with a TTL, so we do not refetch it on every check."""

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
        self._lock = threading.Lock()

    def _load(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        parser = urllib.robotparser.RobotFileParser()
        try:
            resp = httpx.get(
                f"{origin}/robots.txt",
                timeout=10.0,
                follow_redirects=True,
                headers={"User-Agent": settings.user_agent},
            )
        except httpx.HTTPError as exc:
            log.warning("robots_fetch_failed", origin=origin, error=str(exc))
            return None  # unreachable robots.txt is treated as "no rules"
        if resp.status_code >= 400:
            return None
        parser.parse(resp.text.splitlines())
        return parser

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(origin)
            if cached and now - cached[0] < self.ttl:
                parser = cached[1]
            else:
                parser = None
                cached = None
        if cached is None:
            parser = self._load(origin)
            with self._lock:
                self._cache[origin] = (now, parser)
        if parser is None:
            return True
        return parser.can_fetch(settings.user_agent, url)


_robots = RobotsCache()


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def _classify_http_error(exc: Exception) -> str:
    """Map an httpx transport failure onto a stable, actionable error kind."""
    text = f"{exc.__class__.__name__}: {exc}".lower()
    if isinstance(exc, httpx.ConnectError):
        if "certificate" in text or "ssl" in text or "tls" in text:
            return "tls_error"
        if "name or service not known" in text or "temporary failure in name resolution" in text \
                or "nodename nor servname" in text or "getaddrinfo" in text:
            return "dns_error"
        if "refused" in text:
            return "connection_refused"
        return "connection_failed"
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return "connection_reset"
    if "certificate" in text or "ssl" in text:
        return "tls_error"
    return "network_error"


def fetch(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    *,
    respect_robots: bool | None = None,
    throttle: bool = True,
) -> FetchResult:
    """Fetch a URL, returning at most `fetch_max_bytes` of decoded body.

    Redirects are followed MANUALLY, one hop at a time, and every hop is
    re-validated. Letting httpx follow redirects automatically would defeat the
    SSRF guard entirely: an attacker-controlled public host can answer with
    `302 Location: http://169.254.169.254/`, and only the first URL would ever
    have been checked.
    """
    method = (method or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise FetchError("invalid_method", f"Method {method} is not supported.")

    if respect_robots is None:
        respect_robots = settings.respect_robots_txt

    request_headers = {
        "User-Agent": settings.user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        **sanitize_headers(headers),
    }

    current_url = url
    started = time.perf_counter()
    deadline = started + settings.fetch_total_deadline_seconds

    with httpx.Client(
        timeout=settings.fetch_timeout_seconds,
        follow_redirects=False,
        headers=request_headers,
    ) as client:
        for hop in range(settings.fetch_max_redirects + 1):
            # Re-validate on EVERY hop, not just the first.
            safe_url = validate_target_url(current_url)

            if respect_robots and not _robots.allowed(safe_url):
                raise FetchError(
                    "robots_disallowed", "robots.txt on the target site disallows this URL."
                )
            if throttle:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise FetchError("deadline_exceeded", _DEADLINE_MESSAGE)
                _throttle.wait(urlparse(safe_url).netloc, max_wait=remaining)

            if time.perf_counter() >= deadline:
                raise FetchError("deadline_exceeded", _DEADLINE_MESSAGE)

            # Only the first hop carries the body; a redirected request must not
            # replay a POST body at a different origin.
            hop_method = method if hop == 0 else ("GET" if method == "POST" else method)
            hop_body = body if hop == 0 else None

            try:
                hop_timeout = min(
                    settings.fetch_timeout_seconds, max(1.0, deadline - time.perf_counter())
                )
                with client.stream(
                    hop_method, safe_url, content=hop_body, timeout=hop_timeout
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(
                                "invalid_redirect", "Redirect response had no Location header."
                            )
                        # Resolve relative redirects against the current URL.
                        current_url = str(httpx.URL(safe_url).join(location))
                        continue

                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    for chunk in response.iter_bytes():
                        # Enforced DURING the body read, not only between hops:
                        # httpx timeouts are per socket operation, so a server
                        # trickling one small chunk forever never trips them.
                        # That is a slow-loris against our own worker pool.
                        if time.perf_counter() >= deadline:
                            raise FetchError("deadline_exceeded", _DEADLINE_MESSAGE)
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= settings.fetch_max_bytes:
                            truncated = True
                            break
                    raw = b"".join(chunks)[: settings.fetch_max_bytes]
                    encoding = response.encoding or "utf-8"
                    try:
                        text = raw.decode(encoding, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        text = raw.decode("utf-8", errors="replace")

                    return FetchResult(
                        url=safe_url,
                        status_code=response.status_code,
                        headers={k.lower(): v for k, v in response.headers.items()},
                        body=text,
                        elapsed_ms=(time.perf_counter() - started) * 1000,
                        truncated=truncated,
                    )
            except httpx.TimeoutException as exc:
                raise FetchError(
                    "timeout", f"Request timed out after {settings.fetch_timeout_seconds}s"
                ) from exc
            except httpx.HTTPError as exc:
                # Distinguish the common failures: an expired certificate, a
                # dead DNS record and a refused port need different fixes.
                raise FetchError(_classify_http_error(exc), f"{exc.__class__.__name__}: {exc}") from exc

    raise FetchError(
        "too_many_redirects",
        f"Exceeded {settings.fetch_max_redirects} redirects while fetching.",
    )
