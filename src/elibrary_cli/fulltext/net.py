"""HTTP client for external hosts (Crossref, OpenAlex, Unpaywall, DataCite, CyberLeninka, publishers).

elibrary.ru is never contacted from here: :class:`ExternalHTTP` refuses elibrary hosts (also when a
redirect points there); use :class:`elibrary_cli.http.ElibraryHTTP` for elibrary.ru.

Politeness:

* honest User-Agent ``elibrary-cli/<version> (+https://github.com/lujo777/elibrary-cli)``; only requests
  to the scholarly APIs that ask for a contact (:data:`CONTACT_HOSTS`: Crossref, OpenAlex, Unpaywall,
  DataCite) carry ``; mailto:<e-mail>`` (``Settings.contact_email`` / ``ELIBRARY_CONTACT_EMAIL``),
  publishers, repositories, redirect targets and robots.txt hosts never see the e-mail;
* a minimum pause per host (1 s, cyberleninka.ru 3 s), one retry on 429/5xx/timeouts honouring
  ``Retry-After``;
* an on-disk cache (``<cache_dir>/external-cache.sqlite3``, :class:`elibrary_cli.http.PageCache`),
  respecting ``ELIBRARY_REFRESH`` and ``ELIBRARY_OFFLINE``;
* a response size cap (50 MB in memory; downloads ``Settings.max_download_mb``, 100 MB by default)
  and manual redirects (at most 5, every hop re-checked);
* ``robots.txt`` for page and file fetches (``robots=True``, used by the full-text sources for
  publisher pages, PDFs and CyberLeninka articles; JSON APIs made for programs — Crossref, OpenAlex,
  Unpaywall, DataCite, Handle — are not checked): parsed with :mod:`urllib.robotparser` and matched
  per RFC 9309 (``*``/``$`` wildcards, the longest rule wins), cached per origin in memory and in the
  external cache for 24 h; only a network error while fetching ``robots.txt`` lets the request
  through ("fail open"), 401/403/5xx forbid it;
* CyberLeninka PDFs are never downloaded (its robots.txt disallows ``/article/n/*/pdf``);
* anti-bot pages ("Вы точно человек?", Cloudflare challenges) raise :class:`ExternalChallenge` and
  stop further requests to that host for 15 minutes.

SSRF guard (the URLs come from untrusted metadata and publisher pages): only http(s), never
elibrary.ru, localhost or a non-global IP literal; before every hop (redirect targets and robots.txt
included) the host name is resolved and refused when any of its addresses is not global
(:class:`PrivateAddress`); the real network backend resolves again, checks and connects to the vetted
address itself (no second lookup, so DNS rebinding between the check and the connection is useless).
"""

from __future__ import annotations

import contextlib
import email.utils
import functools
import ipaddress
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.robotparser
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit, urlunparse

import httpcore2
import httpx2

from elibrary_cli import __version__
from elibrary_cli.config import Settings
from elibrary_cli.errors import AccessBlocked, NetworkError, NotFound, OfflineCacheMiss
from elibrary_cli.http import Page, PageCache, Params, encode_params

log = logging.getLogger(__name__)

PROJECT_URL = "https://github.com/lujo777/elibrary-cli"
#: Largest response body accepted, bytes.
MAX_BYTES = 50 * 1024 * 1024
#: Default minimum pause between two requests to the same host, seconds.
DEFAULT_DELAY = 1.0
#: Per-host minimum pauses (a host matches itself and its subdomains).
HOST_DELAYS: dict[str, float] = {"cyberleninka.ru": 3.0}
MAX_REDIRECTS = 5
#: Cache lifetimes, hours.
TTL_API = 24 * 7
TTL_PAGE = 24 * 30
#: How long a host stays blocked after an anti-bot page, seconds.
CHALLENGE_PAUSE = 15 * 60
#: Longest ``Retry-After`` we are willing to sleep, seconds.
MAX_RETRY_AFTER = 30.0
#: How long a fetched robots.txt verdict is reused (memory and on-disk cache), hours (RFC 9309: 24 h).
TTL_ROBOTS = 24.0
#: How long a robots.txt that could not be checked (network error, 5xx, 429) is remembered in memory, s.
ROBOTS_RETRY = 600.0
#: Largest robots.txt read, bytes (RFC 9309 asks for at least 500 KiB).
ROBOTS_MAX_BYTES = 512 * 1024
CYBERLENINKA_HOST = "cyberleninka.ru"
#: Hosts whose APIs ask for a contact e-mail ("polite pool"): only they get ``mailto:`` in the User-Agent.
CONTACT_HOSTS = frozenset({"api.crossref.org", "api.openalex.org", "api.unpaywall.org", "api.datacite.org"})
#: How many resolved addresses of a host the network backend tries to connect to.
MAX_CONNECT_ADDRESSES = 4

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_RETRY_CODES = {429, 500, 502, 503, 504}
_CHALLENGE_RE = re.compile(
    r"Вы точно человек|cf-chl-|challenge-platform|<title>\s*Just a moment|<title>\s*Attention Required"
    r"|Security Challenge|Checking your browser",
    re.I,
)
_CAPTCHA_RE = re.compile(r"captcha", re.I)


class ExternalHTTPError(NetworkError):
    """An external host answered with an unexpected status or an unusable body."""

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None) -> None:
        super().__init__(message, url=url)
        self.status = status


class ResponseTooLarge(ExternalHTTPError):
    """The response is larger than the size cap."""


class NotAPdf(ExternalHTTPError):
    """A URL that should give a PDF returned something else (HTML stub, paywall page...)."""


class PrivateAddress(ExternalHTTPError):
    """The host of a URL resolves to a loopback, private, link-local or otherwise non-global address
    (SSRF guard: links from metadata or publisher pages must not reach the user's own network)."""

    def __init__(self, host: str, address: str, *, url: str | None = None) -> None:
        super().__init__(
            f"Запрос к {url or host} запрещён: имя {host} указывает на непубличный адрес {address} "
            "(локальная сеть или этот компьютер).",
            url=url,
        )
        self.host = host
        self.address = address


class RedirectToElibrary(ExternalHTTPError):
    """A redirect chain of an external URL leads to elibrary.ru (handled by ElibraryHTTP only)."""

    def __init__(self, url: str, location: str) -> None:
        super().__init__(
            f"Ссылка {url} ведёт на eLIBRARY.RU ({location}); внешний клиент туда не обращается.", url=url
        )
        self.location = location


class RobotsDisallowed(ExternalHTTPError):
    """The site's robots.txt (or a hard-coded rule for CyberLeninka) forbids fetching this URL."""

    def __init__(self, url: str, *, reason: str | None = None) -> None:
        host = host_of(url)
        super().__init__(
            reason or f"robots.txt сайта {host} запрещает автоматическую загрузку {url} — адрес пропущен.",
            url=url,
        )
        self.host = host


class ExternalChallenge(AccessBlocked):
    """An external host answered with a captcha / anti-bot page."""

    def __init__(self, host: str, url: str | None = None) -> None:
        super().__init__(
            f"Сайт {host} показал проверку «Вы точно человек?» / антибот-страницу. Запросы к нему "
            f"приостановлены на {CHALLENGE_PAUSE // 60} мин. Откройте {url or host} в браузере, если нужен "
            "этот источник, или исключите его из списка источников.",
            url=url,
        )
        self.host = host


def host_of(url: str) -> str:
    """Lower-case host name of a URL (``""`` when there is none)."""
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def is_elibrary_host(host_or_url: str) -> bool:
    """``True`` for elibrary.ru and its subdomains (www.elibrary.ru...)."""
    host = host_of(host_or_url) if "/" in host_or_url else host_or_url.lower().rstrip(".")
    return host == "elibrary.ru" or host.endswith(".elibrary.ru")


def build_user_agent(contact_email: str | None) -> str:
    """Honest User-Agent with the project URL and, when given, the contact e-mail."""
    contact = f"; mailto:{contact_email}" if contact_email else ""
    return f"elibrary-cli/{__version__} (+{PROJECT_URL}{contact})"


def is_contact_host(host_or_url: str) -> bool:
    """``True`` for the scholarly APIs that get the contact e-mail (:data:`CONTACT_HOSTS`)."""
    host = host_of(host_or_url) if "/" in host_or_url else host_or_url.lower().rstrip(".")
    return host in CONTACT_HOSTS


def is_public_ip(address: str) -> bool:
    """``True`` when ``address`` is a global unicast IP (IPv4-mapped IPv6 is judged as IPv4)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int, *, url: str | None = None) -> list[tuple[int, str]]:
    """Resolve ``host`` and return its ``(family, address)`` pairs; raise :class:`PrivateAddress` when
    any address is not global (a name with one private address is refused as a whole). DNS errors
    propagate as :class:`socket.gaierror`."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[tuple[int, str]] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = str(sockaddr[0])
        if not is_public_ip(address):
            raise PrivateAddress(host, address, url=url)
        if (family, address) not in out:
            out.append((family, address))
    return out


class _PublicOnlyBackend(httpcore2.SyncBackend):
    """Network backend of the real client: resolves the host itself, refuses non-global addresses
    and connects to the vetted address (TLS SNI and certificate checks still use the host name, the
    origin is unchanged). The address that is checked is the address that is connected to, so a DNS
    answer that changes between the pre-check and the connection (rebinding) cannot reach the LAN."""

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                    socket_options: Any = None) -> httpcore2.NetworkStream:
        try:
            addresses = resolve_public(host, port)
        except OSError as exc:
            raise httpcore2.ConnectError(f"{host}: {exc}") from exc
        last: Exception = httpcore2.ConnectError(f"{host}: no IPv4/IPv6 address")
        for _family, address in addresses[:MAX_CONNECT_ADDRESSES]:
            try:
                return super().connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                           socket_options=socket_options)
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as exc:
                last = exc
        raise last


def _pin_public_addresses(client: httpx2.Client) -> bool:
    """Put :class:`_PublicOnlyBackend` into the direct-connection pool of a real ``httpx2.Client``.
    Proxied requests (``HTTPS_PROXY`` & co.) connect to the user's proxy and are left alone: the
    proxy resolves the target, only the pre-check of :meth:`ExternalHTTP._check_dns` applies to them."""
    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    if (isinstance(pool, httpcore2.ConnectionPool) and not isinstance(pool, (httpcore2.HTTPProxy,
                                                                            httpcore2.SOCKSProxy))
            and hasattr(pool, "_network_backend")):
        pool._network_backend = _PublicOnlyBackend()
        return True
    log.warning("could not pin connections to vetted addresses (httpx2/httpcore2 internals changed); "
                "only the DNS pre-check guards against private addresses")
    return False


def _check_public_host(url: str) -> None:
    """Refuse non-HTTP(S) URLs, elibrary.ru, localhost and private IP literals (SSRF guard, no DNS)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ExternalHTTPError(f"Неподдерживаемая схема URL: {url}", url=url)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ExternalHTTPError(f"В URL нет имени хоста: {url}", url=url)
    if is_elibrary_host(host):
        # Same intent as an assert, but it survives `python -O`.
        raise AssertionError(f"ExternalHTTP must not request elibrary.ru ({url}); use ElibraryHTTP")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ExternalHTTPError(f"Запрос к локальному адресу запрещён: {url}", url=url)
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(host.strip("[]"))
        if not ip.is_global:
            raise ExternalHTTPError(f"Запрос к непубличному IP-адресу запрещён: {url}", url=url)


def _retry_after(headers: Mapping[str, str], default: float) -> float:
    raw = (headers.get("retry-after") or "").strip()
    if not raw:
        return default
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(raw)))
    except ValueError:
        pass
    with contextlib.suppress(TypeError, ValueError):
        when = email.utils.parsedate_to_datetime(raw)
        return min(MAX_RETRY_AFTER, max(0.0, when.timestamp() - time.time()))
    return default


def _ssl_context() -> ssl.SSLContext:
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore is a hard dependency
        return ssl.create_default_context()


def is_cyberleninka_host(host_or_url: str) -> bool:
    """``True`` for cyberleninka.ru and its subdomains."""
    host = host_of(host_or_url) if "/" in host_or_url else host_or_url.lower().rstrip(".")
    return host == CYBERLENINKA_HOST or host.endswith("." + CYBERLENINKA_HOST)


@functools.lru_cache(maxsize=1024)
def _rule_regex(path: str) -> re.Pattern[str]:
    """Regex of a robots.txt rule path as quoted by :mod:`urllib.robotparser` (``*`` is ``%2A``,
    ``$`` is ``%24``): ``*`` matches any run of characters, a final ``$`` anchors the end."""
    anchored = path.endswith("%24")
    if anchored:
        path = path[:-3]
    pattern = ".*".join(re.escape(part) for part in path.split("%2A"))
    return re.compile(pattern + (r"\Z" if anchored else ""))


class RobotsRules(urllib.robotparser.RobotFileParser):
    """:class:`urllib.robotparser.RobotFileParser` (groups, user-agent matching) with RFC 9309 path
    matching: ``*`` and ``$`` wildcards, the longest matching rule wins, ``Allow`` wins a tie.

    (urllib itself compares literal prefixes in file order, so ``Disallow: /article/n/*/pdf`` would
    never match.) Build with :meth:`from_response`.
    """

    @classmethod
    def from_response(cls, status: int, content: bytes = b"") -> RobotsRules:
        """Rules for a robots.txt answer: 2xx is parsed; 401/403 forbid everything (as urllib does);
        other 4xx mean "no robots.txt" (everything allowed); 429/5xx mean "unreachable" (nothing
        allowed, RFC 9309 §2.3.1.4)."""
        rules = cls()
        if 200 <= status < 300:
            rules.parse(content.decode("utf-8-sig", "replace").splitlines())
        elif status in (401, 403):
            rules.disallow_all = True
        elif 400 <= status < 500 and status != 429:
            rules.allow_all = True
        else:
            rules.disallow_all = True
        return rules

    @classmethod
    def allow_everything(cls) -> RobotsRules:
        rules = cls()
        rules.allow_all = True
        return rules

    def can_fetch(self, useragent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        if not self.last_checked:
            return False
        # the same normalisation urllib applies to the rule paths
        parsed = urlparse(unquote(url))
        target = quote(urlunparse(("", "", parsed.path, parsed.params, parsed.query, parsed.fragment))) or "/"
        entry = next((e for e in self.entries if e.applies_to(useragent)), None) or self.default_entry
        if entry is None:
            return True
        best: tuple[int, bool] | None = None
        for line in entry.rulelines:
            if _rule_regex(line.path).match(target):
                key = (len(line.path), bool(line.allowance))
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]


class ExternalHTTP:
    """Polite, cached HTTP client for non-elibrary hosts.

    ``transport`` replaces the network (``httpx2.MockTransport`` in tests); ``cache=False`` disables
    the on-disk cache; ``default_delay`` / ``host_delays`` override the per-host pauses; ``max_bytes``
    caps every response (default: 50 MB in memory, ``settings.max_download_mb`` for downloads);
    ``check_dns`` resolves every host before a request and refuses non-global addresses (default: on
    for the real network, off with a custom ``transport``, whose hosts are not real);
    ``sleep`` and ``clock`` are injectable for tests.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx2.BaseTransport | None = None,
        cache: PageCache | bool = True,
        default_delay: float | None = None,
        host_delays: Mapping[str, float] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        check_dns: bool | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.default_delay = DEFAULT_DELAY if default_delay is None else max(0.0, default_delay)
        self.host_delays = dict(HOST_DELAYS if host_delays is None else host_delays)
        self.max_bytes = MAX_BYTES if max_bytes is None else max_bytes
        #: Size cap of :meth:`download` (``ELIBRARY_MAX_DOWNLOAD_MB``).
        self.max_download_bytes = self.settings.max_download_bytes if max_bytes is None else max_bytes
        self.timeout = timeout or min(self.settings.timeout, 60.0)
        #: User-Agent of every request: no e-mail (see :meth:`user_agent_for`).
        self.user_agent = build_user_agent(None)
        #: User-Agent with ``mailto:`` for :data:`CONTACT_HOSTS` (same as ``user_agent`` without an e-mail).
        self.contact_user_agent = build_user_agent(self.settings.contact_email)
        self.check_dns = transport is None if check_dns is None else check_dns
        #: ``True`` once the real client connects only to vetted addresses (see :class:`_PublicOnlyBackend`).
        self.pinned = False
        if isinstance(cache, PageCache):
            self.cache: PageCache | None = cache
        elif cache:
            self.cache = PageCache(self.settings.cache_dir / "external-cache.sqlite3")
        else:
            self.cache = None
        self._own_cache = cache is True
        self._transport = transport
        self._client: httpx2.Client | None = None
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.RLock()
        self._last: dict[str, float] = {}
        self._blocked: dict[str, float] = {}
        #: origin -> (valid until, by self._clock; rules)
        self._robots: dict[str, tuple[float, RobotsRules]] = {}
        self.requests_sent = 0

    # ------------------------------------------------------------------ plumbing
    @property
    def client(self) -> httpx2.Client:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "headers": {
                    "User-Agent": self.user_agent,
                    "Accept-Language": "ru,en;q=0.8",
                },
                "timeout": httpx2.Timeout(self.timeout, connect=min(self.timeout, 15.0)),
                "follow_redirects": False,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            else:
                kwargs["verify"] = _ssl_context()
            self._client = httpx2.Client(**kwargs)
            if self._transport is None:
                self.pinned = _pin_public_addresses(self._client)
        return self._client

    def user_agent_for(self, url: str) -> str:
        """User-Agent for one request: with the contact e-mail only for :data:`CONTACT_HOSTS`."""
        return self.contact_user_agent if is_contact_host(url) else self.user_agent

    def _check_dns(self, url: str) -> None:
        """Resolve the host of ``url`` and refuse it when an address is not global (no-op unless
        ``check_dns``). A name that does not resolve is let through: the connection fails on its own,
        and behind a proxy only the proxy resolves names."""
        if not self.check_dns:
            return
        try:
            parsed = httpx2.URL(url)
            host = parsed.raw_host.decode("ascii")
        except (httpx2.InvalidURL, UnicodeDecodeError, ValueError):
            return
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            resolve_public(host, port, url=url)
        except OSError as exc:
            log.debug("cannot resolve %s before the request: %s", host, exc)

    def _delay_key(self, host: str) -> tuple[str, float]:
        for name, delay in self.host_delays.items():
            if host == name or host.endswith("." + name):
                return name, delay
        return host, self.default_delay

    def delay_for(self, host_or_url: str) -> float:
        """Minimum pause between two requests to this host, seconds."""
        host = host_of(host_or_url) if "/" in host_or_url else host_or_url
        return self._delay_key(host)[1]

    def _throttle(self, host: str) -> None:
        key, delay = self._delay_key(host)
        with self._lock:
            now = self._clock()
            last = self._last.get(key)
            wait = 0.0 if last is None else max(0.0, last + delay - now)
            self._last[key] = now + wait
        if wait > 0:
            self._sleep(wait)

    def _check_blocked(self, host: str, url: str) -> None:
        key = self._delay_key(host)[0]
        until = self._blocked.get(key)
        if until is not None and self._clock() < until:
            raise ExternalChallenge(host, url)

    def _trip(self, host: str) -> None:
        self._blocked[self._delay_key(host)[0]] = self._clock() + CHALLENGE_PAUSE

    def _send_once(self, method: str, url: str, body: bytes | None, headers: dict[str, str],
                   max_bytes: int, sink: BinaryIO | None) -> tuple[int, httpx2.Headers, bytes, str]:
        """One HTTP exchange; the body goes to ``sink`` (2xx only) or to memory, capped."""
        with self.client.stream(method, url, content=body, headers=headers) as resp:
            status = resp.status_code
            length = resp.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ResponseTooLarge(
                    f"Ответ {url} слишком большой ({int(length) // 1048576} МБ > {max_bytes // 1048576} МБ)",
                    url=url, status=status)
            if status in _REDIRECT_CODES:
                return status, resp.headers, b"", str(resp.url)
            size = 0
            chunks: list[bytes] = []
            to_sink = sink is not None and 200 <= status < 300
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ResponseTooLarge(
                        f"Ответ {url} больше {max_bytes // 1048576} МБ, загрузка прервана", url=url, status=status)
                if to_sink:
                    sink.write(chunk)  # type: ignore[union-attr]
                    if len(chunks) < 1:  # keep the first chunk for content sniffing
                        chunks.append(chunk[:4096])
                else:
                    chunks.append(chunk)
            return status, resp.headers, b"".join(chunks), str(resp.url)

    def _fetch(self, method: str, url: str, body: bytes | None, headers: Mapping[str, str] | None,
               *, expect: str, max_bytes: int | None, sink: BinaryIO | None = None, robots: bool = False) -> Page:
        hdrs = dict(headers or {})
        current, m, b = url, method, body
        for _hop in range(MAX_REDIRECTS + 1):
            _check_public_host(current)
            host = host_of(current)
            if sink is not None and is_cyberleninka_host(host):
                raise RobotsDisallowed(current, reason=(
                    f"Файлы КиберЛенинки не скачиваются ({current}): robots.txt запрещает роботам PDF "
                    "(/article/n/*/pdf); текст берётся с HTML-страницы статьи."))
            self._check_blocked(host, current)
            self._check_dns(current)
            if robots and not self.robots_allowed(current):
                raise RobotsDisallowed(current)
            # per hop: a redirect from a contact API to another host must not carry the e-mail
            hdrs["User-Agent"] = self.user_agent_for(current)
            status, rheaders, content, final = self._exchange(m, current, b, hdrs, max_bytes or self.max_bytes, sink)
            if status in _REDIRECT_CODES and rheaders.get("location"):
                nxt = urljoin(current, rheaders["location"])
                if is_elibrary_host(nxt):
                    raise RedirectToElibrary(url, nxt)
                if status == 303 or (status in (301, 302) and m == "POST"):
                    m, b = "GET", None
                    hdrs.pop("Content-Type", None)
                current = nxt
                continue
            return self._finish(url, final, status, rheaders, content, expect)
        raise ExternalHTTPError(f"Слишком много перенаправлений: {url}", url=url)

    def _exchange(self, method: str, url: str, body: bytes | None, headers: dict[str, str], max_bytes: int,
                  sink: BinaryIO | None) -> tuple[int, httpx2.Headers, bytes, str]:
        """Send with one retry on timeouts, network errors, 429 and 5xx."""
        host = host_of(url)
        last_exc: Exception | None = None
        for attempt in range(2):
            self._throttle(host)
            if sink is not None:
                sink.seek(0)
                sink.truncate()
            try:
                self.requests_sent += 1
                status, rheaders, content, final = self._send_once(method, url, body, headers, max_bytes, sink)
            except PrivateAddress as exc:  # refused by the network backend at connect time
                raise PrivateAddress(exc.host, exc.address, url=url) from None
            except (httpx2.TimeoutException, httpx2.NetworkError, httpx2.RemoteProtocolError) as exc:
                last_exc = exc
                log.debug("%s %s failed: %s", method, url, exc)
                if attempt == 0:
                    self._sleep(2.0)
                continue
            except (httpx2.RequestError, httpx2.InvalidURL, httpx2.StreamError) as exc:
                # proxy errors, bad URLs, broken streams: a retry would not help
                raise ExternalHTTPError(f"Запрос к {host} не удался ({type(exc).__name__}: {exc})", url=url) from exc
            if status in _RETRY_CODES and attempt == 0:
                self._sleep(_retry_after(rheaders, 2.0))
                continue
            return status, rheaders, content, final
        raise ExternalHTTPError(f"Нет связи с {host} ({type(last_exc).__name__}: {last_exc})", url=url)

    def _finish(self, url: str, final: str, status: int, headers: httpx2.Headers, content: bytes,
                expect: str) -> Page:
        host = host_of(final)
        ctype = headers.get("content-type") or ""
        head = content[:30000].decode("utf-8", "replace") if ("html" in ctype or content[:1] == b"<") else ""
        if head and (_CHALLENGE_RE.search(head) or (expect in ("json", "pdf") and _CAPTCHA_RE.search(head))):
            self._trip(host)
            raise ExternalChallenge(host, final)
        if status in (404, 410):
            raise NotFound(f"Не найдено ({status}): {url}", url=url)
        if not 200 <= status < 300:
            raise ExternalHTTPError(f"{host} ответил HTTP {status}: {url}", url=url, status=status)
        return Page(url=final, status=status, content=content, content_type=ctype or None)

    # ------------------------------------------------------------------ robots.txt
    def robots_allowed(self, url: str) -> bool:
        """May our User-Agent fetch ``url`` according to the site's robots.txt?

        robots.txt is fetched once per origin (scheme + host + port) and reused for 24 h (memory and
        the external cache). Missing robots.txt (404 and other 4xx) allows everything, 401/403 and
        429/5xx forbid everything, a network error while fetching it allows the request (fail open,
        retried after 10 minutes). In offline mode an uncached robots.txt is not fetched and nothing
        is forbidden (no request can be sent anyway).
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        with self._lock:
            known = self._robots.get(origin)
        if known is not None and self._clock() < known[0]:
            return known[1].can_fetch(self.user_agent, url)
        rules, lifetime = self._load_robots(origin)
        with self._lock:
            self._robots[origin] = (self._clock() + lifetime, rules)
        return rules.can_fetch(self.user_agent, url)

    def _load_robots(self, origin: str) -> tuple[RobotsRules, float]:
        """Rules for an origin and how long (s) to keep them in memory."""
        robots_url = origin + "/robots.txt"
        key = PageCache.make_key("ROBOTS", robots_url, None)
        offline = self.settings.offline
        if self.cache is not None and (offline or not self.settings.refresh):
            cached = self.cache.get(key, float("inf") if offline else TTL_ROBOTS * 3600)
            if cached is not None:
                return RobotsRules.from_response(cached.status, cached.content), TTL_ROBOTS * 3600
        if offline:
            return RobotsRules.allow_everything(), ROBOTS_RETRY
        try:
            page = self._fetch("GET", robots_url, None, {"Accept": "text/plain,*/*;q=0.5"}, expect="any",
                               max_bytes=ROBOTS_MAX_BYTES)
            status, content = page.status, page.content
        except (NotFound, RedirectToElibrary):
            status, content = 404, b""
        except ResponseTooLarge:
            log.warning("robots.txt of %s is larger than %d bytes; treating the site as disallowed", origin,
                        ROBOTS_MAX_BYTES)
            return RobotsRules.from_response(500), ROBOTS_RETRY
        except (ExternalChallenge, PrivateAddress):
            raise
        except ExternalHTTPError as exc:
            if exc.status is None:  # no connection: fail open, try again later
                log.info("robots.txt of %s could not be fetched (%s); allowing the request", origin, exc)
                return RobotsRules.allow_everything(), ROBOTS_RETRY
            status, content = exc.status, b""
        rules = RobotsRules.from_response(status, content)
        if status >= 500 or status == 429:
            return rules, ROBOTS_RETRY  # "unreachable": do not remember for a day
        if self.cache is not None:
            self.cache.put(key, Page(url=robots_url, status=status, content=content, content_type="text/plain"))
        return rules, TTL_ROBOTS * 3600

    # ------------------------------------------------------------------ public API
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Params | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl_hours: float = 0.0,
        expect: str = "any",
        max_bytes: int | None = None,
        robots: bool = False,
    ) -> Page:
        """Send a request and return the final :class:`~elibrary_cli.http.Page` (2xx only).

        ``expect`` (``"json"``, ``"html"``, ``"pdf"``, ``"any"``) tunes challenge detection.
        ``robots=True`` checks every URL of the redirect chain against the site's robots.txt before
        it is requested (a cached answer needs no check: nothing is sent).
        Raises :class:`NotFound` on 404/410, :class:`RobotsDisallowed` when robots.txt forbids the URL,
        :class:`ExternalHTTPError` on other errors, :class:`ExternalChallenge` on anti-bot pages,
        :class:`AssertionError` for elibrary.ru URLs.
        """
        query = encode_params(params)
        if query:
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        _check_public_host(url)
        body = None
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        key = PageCache.make_key(method.upper(), url, body.decode("utf-8") if body else None)
        offline = self.settings.offline
        if self.cache is not None and (offline or (cache_ttl_hours > 0 and not self.settings.refresh)):
            cached = self.cache.get(key, float("inf") if offline else cache_ttl_hours * 3600)
            if cached is not None:
                return cached
        if offline:
            raise OfflineCacheMiss(f"Нет в кэше (ELIBRARY_OFFLINE=1): {url}", url=url)
        page = self._fetch(method.upper(), url, body, hdrs, expect=expect, max_bytes=max_bytes, robots=robots)
        if self.cache is not None and cache_ttl_hours > 0:
            self.cache.put(key, page)
        return page

    def get_page(self, url: str, *, params: Params | None = None, headers: Mapping[str, str] | None = None,
                 cache_ttl_hours: float = TTL_PAGE, robots: bool = False) -> Page:
        """GET an HTML page (cached for 30 days by default); ``robots=True`` honours robots.txt."""
        hdrs = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", **(headers or {})}
        return self.request("GET", url, params=params, headers=hdrs, cache_ttl_hours=cache_ttl_hours,
                            expect="html", robots=robots)

    def get_text(self, url: str, *, params: Params | None = None, headers: Mapping[str, str] | None = None,
                 cache_ttl_hours: float = TTL_PAGE, robots: bool = False) -> str:
        """GET a page and return it decoded (HTTP charset, ``<meta charset>``, UTF-8, cp1251)."""
        return self.get_page(url, params=params, headers=headers, cache_ttl_hours=cache_ttl_hours,
                             robots=robots).text

    def get_bytes(self, url: str, *, params: Params | None = None, headers: Mapping[str, str] | None = None,
                  cache_ttl_hours: float = 0.0, max_bytes: int | None = None, robots: bool = False) -> bytes:
        """GET raw bytes (not cached by default)."""
        return self.request("GET", url, params=params, headers=headers, cache_ttl_hours=cache_ttl_hours,
                            max_bytes=max_bytes, robots=robots).content

    def _json(self, page: Page) -> Any:
        try:
            return json.loads(page.content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExternalHTTPError(f"Ответ {page.url} не является JSON", url=page.url, status=page.status) from exc

    def get_json(self, url: str, *, params: Params | None = None, headers: Mapping[str, str] | None = None,
                 cache_ttl_hours: float = TTL_API) -> Any:
        """GET a JSON document (cached for 7 days by default)."""
        hdrs = {"Accept": "application/json", **(headers or {})}
        return self._json(self.request("GET", url, params=params, headers=hdrs, cache_ttl_hours=cache_ttl_hours,
                                       expect="json"))

    def post_json(self, url: str, payload: Any, *, headers: Mapping[str, str] | None = None,
                  cache_ttl_hours: float = TTL_API) -> Any:
        """POST a JSON body and parse the JSON answer (cached by URL + body)."""
        hdrs = {"Accept": "application/json", **(headers or {})}
        return self._json(self.request("POST", url, json_body=payload, headers=hdrs,
                                       cache_ttl_hours=cache_ttl_hours, expect="json"))

    def download(self, url: str, dest: Path, *, referer: str | None = None, max_bytes: int | None = None,
                 accept: str = "application/pdf,*/*;q=0.5", robots: bool = False) -> tuple[Path, str | None]:
        """Stream ``url`` into ``dest`` (via ``dest.part``); returns ``(dest, content_type)``. Not cached.

        At most ``max_bytes`` (default ``settings.max_download_mb``) are written: a bigger file raises
        :class:`ResponseTooLarge` and the partial file is deleted. CyberLeninka files are refused
        (:class:`RobotsDisallowed`)."""
        _check_public_host(url)
        if is_cyberleninka_host(url):
            raise RobotsDisallowed(url, reason=(
                f"Файлы КиберЛенинки не скачиваются ({url}): robots.txt запрещает роботам PDF "
                "(/article/n/*/pdf); текст берётся с HTML-страницы статьи."))
        if self.settings.offline:
            raise OfflineCacheMiss(f"Скачивание недоступно (ELIBRARY_OFFLINE=1): {url}", url=url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        headers = {"Accept": accept}
        if referer:
            headers["Referer"] = referer
        limit = max_bytes or self.max_download_bytes
        try:
            with tmp.open("w+b") as fh:
                try:
                    page = self._fetch("GET", url, None, headers, expect="pdf", max_bytes=limit, sink=fh,
                                       robots=robots)
                except ResponseTooLarge as exc:
                    raise ResponseTooLarge(
                        f"Файл {url} больше {limit / 1048576:.3g} МБ (предел ELIBRARY_MAX_DOWNLOAD_MB): "
                        "скачивание прервано, недокачанный файл удалён.", url=url, status=exc.status) from exc
                head = page.content[:4096]
            ctype = page.content_type
            if ctype and "html" in ctype.lower() and head and _CHALLENGE_RE.search(head.decode("utf-8", "replace")):
                self._trip(host_of(page.url))
                raise ExternalChallenge(host_of(page.url), page.url)
            tmp.replace(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return dest, ctype

    def download_pdf(self, url: str, dest: Path, *, referer: str | None = None,
                     max_bytes: int | None = None, robots: bool = False) -> Path:
        """Download a PDF and check it really is one (``%PDF`` magic, > 1000 bytes)."""
        path, ctype = self.download(url, dest, referer=referer, max_bytes=max_bytes, robots=robots)
        with path.open("rb") as fh:
            head = fh.read(1024)
        size = path.stat().st_size
        if b"%PDF" not in head or size < 1000:
            path.unlink(missing_ok=True)
            kind = ctype or "неизвестный тип"
            raise NotAPdf(f"По ссылке {url} не PDF ({kind}, {size} байт) — вероятно, страница-заглушка или "
                          "платный доступ", url=url)
        return path

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self.cache is not None and self._own_cache:
            self.cache.close()

    def __enter__(self) -> ExternalHTTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
