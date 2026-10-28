"""Polite HTTP layer for elibrary.ru.

Facts this module is built on (found while reverse engineering the site, 2026-10):

* The TLS front end of elibrary.ru offers only CBC-SHA cipher suites for TLS 1.2 and silently
  stalls the handshake (no alert) when a client does not offer them. Python's default context
  excludes them (``!SHA1``), so every request would time out. We therefore use a context with
  ``set_ciphers("DEFAULT")``. This is a compatibility setting, not a fingerprint trick.
* An anonymous session gets a reCAPTCHA page (``/page_captcha.asp?rpage=...``, title
  "Тест Тьюринга", HTTP 200, ~1.1 KB) after roughly 10-13 page views. Once a human passes it, the
  server marks the ``SUserID`` cookie as verified, and that cookie works from any HTTP client on
  the same IP. Other gates: ``ip_blocked.asp`` / ``div#blockedip`` (ban), ``ip_restricted.asp``
  (anonymous access restricted, log in), IIS ``403 - Forbidden``.
* Search pagination (``query_results.asp?pagenum=N``) is stored server side per session, so all
  requests of one process go through one lock, and the "current query" is remembered.

The state (cookies, throttle clock, budgets, circuit breaker) lives in a small SQLite file shared
by the CLI and the MCP server, so two processes never hammer the site in parallel. Each process
re-checks the shared breaker right before every request (after its throttle pause) and rebuilds its
HTTP client when another process changed the cookies (``cookies.version``).
"""

from __future__ import annotations

import contextlib
import enum
import hashlib
import json
import os
import random
import re
import sqlite3
import ssl
import stat
import threading
import time
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx2

from elibrary_cli.config import Settings
from elibrary_cli.errors import (
    BAN_HELP,
    BAN_PAUSE_HELP,
    IP_RESTRICTED_HELP,
    AccessBlocked,
    CaptchaRequired,
    ElibraryError,
    IpRestricted,
    NetworkError,
    NotFound,
    OfflineCacheMiss,
    RateLimited,
)

Params = Mapping[str, str] | Sequence[tuple[str, str]]

# Cache lifetimes per page type, hours (see docs/reverse-engineering.md).
TTL_ITEM = 24 * 30
TTL_PROFILE = 24 * 7
TTL_JOURNAL = 24 * 30
TTL_SEARCH = 24
TTL_IDENTIFIER = 24 * 365
TTL_NONE = 0.0

_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.I)
#: Transport errors raised before the request reached the server (elibrary.ru often refuses or resets
#: connections): a GET is retried once after them. Errors after that point (``ReadTimeout``,
#: ``RemoteProtocolError``, ``ReadError``...) are never retried: the server may already have done the work
#: (a heavy ``org_items.asp`` page, a login), and a POST is never re-sent at all.
_RETRYABLE_ERRORS = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Hosts that serve the same site; elibrary.ru switches its canonical host between them (2026-10:
#: ``elibrary.ru`` -> ``www.elibrary.ru`` redirects appeared), see :meth:`ElibraryHTTP.origin`.
_ELIBRARY_HOSTS = frozenset({"elibrary.ru", "www.elibrary.ru"})


def _host_switch(src: str, dst: str) -> str | None:
    """The new origin when ``dst`` is ``src`` on the other elibrary host (same path), else ``None``."""
    a, b = urlsplit(src), urlsplit(dst)
    ha, hb = (a.hostname or "").lower(), (b.hostname or "").lower()
    if ha == hb or ha not in _ELIBRARY_HOSTS or hb not in _ELIBRARY_HOSTS:
        return None
    if (a.path or "/") != (b.path or "/") or a.query.rstrip("&") != b.query.rstrip("&"):
        return None
    return f"{b.scheme or 'https'}://{hb}"
#: Redirect hops of a POST followed by hand (see :meth:`ElibraryHTTP._exchange`).
#: elibrary chains are short (defaultx -> start_session -> defaultx -> page_captcha); longer chains are
#: loops, and every hop is a real request that bypasses the throttle.
_MAX_REDIRECTS = 6
_MAX_POST_REDIRECTS = _MAX_REDIRECTS

#: ``ElibraryHTTP.state`` keys of the circuit breaker.
BREAKER_KEYS = ("breaker.until", "breaker.gate", "breaker.url", "breaker.tripped_at")
#: Counter bumped whenever the stored cookies change; a process rebuilds its HTTP client when it moves.
COOKIES_VERSION_KEY = "cookies.version"
#: Unix time of the last cookie import that carried a session cookie (``SUserID``).
COOKIES_IMPORTED_KEY = "cookies.imported_at"
SESSION_COOKIE = "SUserID"
#: Pause after a ban page / IIS 403, minutes.
BAN_BREAKER_MINUTES = 24 * 60


class Gate(str, enum.Enum):
    """Classification of an elibrary.ru response."""

    OK = "ok"
    CAPTCHA = "captcha"
    IP_BLOCKED = "ip_blocked"
    IP_RESTRICTED = "ip_restricted"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"


#: Gates that open the circuit breaker (challenge and ban pages).
CHALLENGE_GATES = frozenset({Gate.CAPTCHA, Gate.IP_BLOCKED, Gate.IP_RESTRICTED, Gate.FORBIDDEN})
#: Breaker gates of an IP ban: never lifted early by a cookie import or a session check.
BAN_GATES = frozenset({Gate.IP_BLOCKED.value, Gate.FORBIDDEN.value})
#: Breaker gates a cookie import with ``SUserID`` (a check passed / a login in the browser) lifts.
SESSION_GATES = frozenset({Gate.CAPTCHA.value, Gate.IP_RESTRICTED.value})
#: Pages a challenge gate is served from, for the gate URLs rebuilt after a POST.
_GATE_PAGES = {Gate.CAPTCHA: "page_captcha.asp", Gate.IP_BLOCKED: "ip_blocked.asp",
               Gate.IP_RESTRICTED: "ip_restricted.asp"}
_POSIX = os.name == "posix"


def _private_dir(path: Path) -> None:
    """Create ``path`` (with parents). On POSIX it is made owner-only (0700): the state database holds the
    session cookies of a possibly signed-in account. The user's home and sticky shared directories
    (``/tmp``) are never chmod-ed. No-op on Windows (per-user profile ACLs)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _POSIX:
        return
    with contextlib.suppress(OSError, RuntimeError):
        mode = path.stat().st_mode
        if mode & stat.S_ISVTX or path.resolve() == Path.home().resolve():
            return
        if stat.S_IMODE(mode) & 0o077:
            os.chmod(path, stat.S_IMODE(mode) & 0o700)


def _private_db_file(path: Path) -> None:
    """On POSIX create the SQLite file as 0600 before SQLite opens it (SQLite gives its ``-wal`` / ``-shm``
    files the mode of the database) and tighten existing files of older versions to 0600."""
    if not _POSIX:
        return
    with contextlib.suppress(OSError):
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        with contextlib.suppress(OSError):
            if p.exists() and stat.S_IMODE(p.stat().st_mode) & 0o077:
                os.chmod(p, 0o600)


def make_ssl_context() -> ssl.SSLContext:
    """TLS context that can talk to elibrary.ru (see module docstring)."""
    try:
        import truststore

        ctx: ssl.SSLContext = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore is a hard dependency, but stay robust
        ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT")
    return ctx


def decode_html(content: bytes, content_type: str | None = None) -> str:
    """Decode a page using the HTTP charset, then ``<meta charset>``, then UTF-8/cp1251."""
    charset = None
    if content_type:
        m = re.search(r"charset\s*=\s*([\w-]+)", content_type, re.I)
        if m:
            charset = m.group(1).lower()
    if not charset:
        m = _CHARSET_RE.search(content[:4096])
        if m:
            charset = m.group(1).decode("ascii", "ignore").lower()
    for enc in filter(None, (charset, "utf-8")):
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        return content.decode("cp1251")
    except UnicodeDecodeError:  # pragma: no cover
        return content.decode("utf-8", errors="replace")


def classify(status: int, final_url: str, content: bytes) -> Gate:
    """Detect challenge/ban pages. elibrary serves most of them with HTTP 200."""
    u = final_url.lower()
    head = content[:20000]
    hl = decode_html(head).lower() if len(content) < 60000 or b"recaptcha" in head else ""
    if "page_captcha.asp" in u or "<title>тест тьюринга" in hl or "check_captcha.asp" in hl or (
        "g-recaptcha" in hl and "необычно много запросов" in hl
    ):
        return Gate.CAPTCHA
    if "ip_blocked.asp" in u or 'id="blockedip"' in hl or "id=blockedip" in hl or "id='blockedip'" in hl or (
        "нарушения правил пользования сайтом elibrary" in hl and "заблокирован" in hl
    ):
        return Gate.IP_BLOCKED
    if "ip_restricted.asp" in u:
        return Gate.IP_RESTRICTED
    if status == 403 or "403 - forbidden" in hl:
        return Gate.FORBIDDEN
    if status == 404:
        return Gate.NOT_FOUND
    if status >= 500 or re.search(r"<h1>\s*server error", hl):
        return Gate.SERVER_ERROR
    return Gate.OK


def captcha_return_url(final_url: str) -> str | None:
    """The page the user wanted, from ``page_captcha.asp?rpage=...``."""
    qs = parse_qs(urlsplit(final_url).query)
    return unquote(qs["rpage"][0]) if qs.get("rpage") else None


def _megabytes(n: int) -> str:
    """Bytes as megabytes for messages: ``104857600`` -> ``"100"``."""
    mb = n / 1048576
    return f"{mb:.0f}" if mb >= 10 else f"{mb:.1f}" if mb >= 0.1 else f"{mb:.3f}"


@dataclass
class Page:
    """A fetched page (possibly from the cache)."""

    url: str
    status: int
    content: bytes
    content_type: str | None = None
    from_cache: bool = False
    fetched_at: float = field(default_factory=time.time)
    #: ``Location`` header of a redirect (only when fetched with ``follow_redirects=False``).
    location: str | None = None
    _text: str | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = decode_html(self.content, self.content_type)
        return self._text


class StateStore:
    """Tiny key-value store in SQLite, safe across threads and processes."""

    def __init__(self, path: Path) -> None:
        _private_dir(path.parent)
        _private_db_file(path)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=60, check_same_thread=False, isolation_level=None)
        with contextlib.suppress(sqlite3.DatabaseError):
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        _private_db_file(path)

    def get(self, key: str, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def delete(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._db.execute("DELETE FROM kv WHERE key=?", (k,))

    def incr(self, key: str) -> int:
        """Atomically (across processes) add 1 to an integer value (missing or invalid = 0); returns it."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                try:
                    value = int(json.loads(row[0])) + 1 if row else 1
                except (TypeError, ValueError):
                    value = 1
                self._db.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
                self._db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.OperationalError):
                    self._db.execute("ROLLBACK")
                raise
        return value

    def reserve_slot(self, key: str, min_delay: float, jitter: float,
                     max_per_hour: int, max_per_day: int) -> float:
        """Atomically book the next request time and count it against the budgets.

        Returns how long the caller must sleep. Raises :class:`RateLimited` if a budget is spent.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                data = json.loads(row[0]) if row else {}
                if not isinstance(data, dict):
                    data = {}
                now = time.time()
                last = float(data.get("last", 0.0))
                stamps = [t for t in data.get("stamps", []) if now - t < 86400]
                hour = [t for t in stamps if now - t < 3600]
                if len(hour) >= max_per_hour or len(stamps) >= max_per_day:
                    self._db.execute("ROLLBACK")
                    oldest = min(hour) if len(hour) >= max_per_hour else min(stamps)
                    window = 3600 if len(hour) >= max_per_hour else 86400
                    retry = int(oldest + window - now) + 1
                    raise RateLimited(
                        f"Исчерпан локальный лимит запросов к elibrary.ru ({len(hour)}/ч, {len(stamps)}/сут; "
                        f"ELIBRARY_MAX_PER_HOUR={max_per_hour}, ELIBRARY_MAX_PER_DAY={max_per_day}). "
                        f"Повторите через ~{retry // 60 + 1} мин. Кэшированные страницы доступны (--offline)."
                    )
                slot = max(now, last + min_delay + (random.uniform(0, jitter) if jitter else 0.0))
                stamps.append(slot)
                data = {"last": slot, "stamps": stamps}
                self._db.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(data)),
                )
                self._db.execute("COMMIT")
            except RateLimited:
                raise
            except BaseException:
                with contextlib.suppress(sqlite3.OperationalError):
                    self._db.execute("ROLLBACK")
                raise
        return max(0.0, slot - now)

    def close(self) -> None:
        with self._lock, contextlib.suppress(Exception):
            self._db.close()


class PageCache:
    """Compressed page cache in SQLite (key = method + URL + body)."""

    def __init__(self, path: Path) -> None:
        _private_dir(path.parent)  # pages rendered for a signed-in session
        _private_db_file(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=60, check_same_thread=False, isolation_level=None)
        with contextlib.suppress(sqlite3.DatabaseError):
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, url TEXT, status INTEGER, "
            "content_type TEXT, content BLOB, fetched_at REAL, location TEXT)"
        )
        _private_db_file(path)

    @staticmethod
    def make_key(method: str, url: str, body: str | None) -> str:
        return hashlib.sha256(f"{method}\n{url}\n{body or ''}".encode()).hexdigest()

    def get(self, key: str, max_age_s: float) -> Page | None:
        if max_age_s <= 0:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT url, status, content_type, content, fetched_at, location FROM pages WHERE key=?", (key,)
            ).fetchone()
        if not row or time.time() - row[4] > max_age_s:
            return None
        return Page(url=row[0], status=row[1], content_type=row[2], content=zlib.decompress(row[3]),
                    from_cache=True, fetched_at=row[4], location=row[5])

    def put(self, key: str, page: Page) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pages(key, url, status, content_type, content, fetched_at, location) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (key, page.url, page.status, page.content_type, zlib.compress(page.content, 6), page.fetched_at,
                 page.location),
            )

    def delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pages WHERE key=?", (key,))

    def clear(self) -> int:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            self._db.execute("DELETE FROM pages")
        return n

    def stats(self) -> dict:
        with self._lock:
            n, size = self._db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0) FROM pages").fetchone()
        return {"pages": n, "bytes": size}

    def close(self) -> None:
        with self._lock, contextlib.suppress(Exception):
            self._db.close()


def encode_params(params: Params | None) -> str:
    if not params:
        return ""
    items = params.items() if isinstance(params, Mapping) else params
    return urlencode([(k, "" if v is None else str(v)) for k, v in items])


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse ``"a=1; b=2"`` (e.g. the output of ``copy(document.cookie)``) into a dict.

    Also accepts a JSON object, a JSON list of ``{"name", "value"}`` objects (browser extension
    exports), a Playwright ``storage_state`` file and Netscape ``cookies.txt`` content.
    """
    raw = raw.strip()
    if not raw:
        return {}
    if raw[0] in "[{":
        data = json.loads(raw)
        if isinstance(data, dict) and "cookies" in data:  # playwright storage_state
            data = data["cookies"]
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return {
            str(c["name"]): str(c["value"])
            for c in data
            if isinstance(c, dict) and "name" in c and "elibrary" in str(c.get("domain", "elibrary"))
        }
    if "\t" in raw:  # Netscape cookies.txt
        out = {}
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) >= 7 and "elibrary" in parts[0]:
                out[parts[5]] = parts[6].strip()
        return out
    out = {}
    text = raw[7:] if raw.lower().startswith("cookie:") else raw
    for part in text.replace("\n", ";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


class ElibraryHTTP:
    """Single-flight, throttled, cached HTTP client for elibrary.ru."""

    THROTTLE_KEY = "throttle.elibrary"

    def __init__(self, settings: Settings | None = None, *, transport: httpx2.BaseTransport | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.state = StateStore(self.settings.state_db)
        self.cache = PageCache(self.settings.cache_db)
        self._lock = threading.RLock()
        self._transport = transport
        self._client: httpx2.Client | None = None
        #: ``cookies.version`` the current client was built from (see :meth:`_sync_client`).
        self._client_version = 0

    # ------------------------------------------------------------------ session / cookies
    def _cookies_version(self) -> int:
        try:
            return int(self.state.get(COOKIES_VERSION_KEY, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _new_client(self) -> httpx2.Client:
        # the version first: a change between the two reads only causes one more rebuild
        self._client_version = self._cookies_version()
        ua = self.state.get("user_agent") or self.settings.user_agent
        kwargs: dict = {
            "headers": {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
            "timeout": httpx2.Timeout(self.settings.timeout, connect=20.0),
            "follow_redirects": True,
            "max_redirects": _MAX_REDIRECTS,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            kwargs["verify"] = make_ssl_context()
        client = httpx2.Client(**kwargs)
        for name, value in self.cookies().items():
            client.cookies.set(name, value, domain=".elibrary.ru", path="/")
        return client

    @property
    def client(self) -> httpx2.Client:
        if self._client is None:
            self._client = self._new_client()
        return self._client

    def _sync_client(self) -> None:
        """Drop the HTTP client when another process (or :meth:`set_cookies`) changed the stored cookies
        since it was built, so that the next request goes out with the current session."""
        if self._client is not None and self._cookies_version() != self._client_version:
            self._reset_client()

    def _bump_cookies_version(self, *, client_current: bool = False) -> None:
        """Tell every process that the stored cookies changed. ``client_current``: this process's client
        already holds them (it received them from the server), so it needs no rebuild."""
        new = self.state.incr(COOKIES_VERSION_KEY)
        if client_current and self._client is not None and new == self._client_version + 1:
            self._client_version = new

    def cookies_changed(self) -> None:
        """The server replaced the session (``auth.login`` / ``auth.logout``): other processes rebuild
        their HTTP clients from the stored cookies before their next request."""
        with self._lock:
            self._bump_cookies_version(client_current=True)

    def cookies(self) -> dict[str, str]:
        """Cookies of the current session (env ELIBRARY_COOKIES wins over the saved session)."""
        saved = self.state.get("cookies", {}) or {}
        if self.settings.cookies:
            saved = {**saved, **parse_cookie_header(self.settings.cookies)}
        return saved

    def set_cookies(self, cookies: Mapping[str, str] | str, *, replace: bool = True,
                    user_agent: str | None = None) -> dict[str, str]:
        """Install cookies copied from the user's browser.

        An import that carries ``SUserID`` (the session a human verified in the browser) closes a
        captcha / ip_restricted breaker and is recorded as ``cookies.imported_at``; a ban breaker
        (ip_blocked, HTTP 403) is never closed here, and an import without ``SUserID`` closes nothing."""
        new = parse_cookie_header(cookies) if isinstance(cookies, str) else dict(cookies)
        # Analytics cookies are useless to us; keep the session ones.
        new = {k: v for k, v in new.items() if not k.startswith(("_ym", "_ga", "_gid", "tmr_"))}
        with self._lock:
            merged = new if replace else {**(self.state.get("cookies", {}) or {}), **new}
            self.state.set("cookies", merged)
            if user_agent:
                self.state.set("user_agent", user_agent)
            if new.get(SESSION_COOKIE):
                self.state.set(COOKIES_IMPORTED_KEY, time.time())
                self.reset_breaker(SESSION_GATES)
            self.state.delete("query.current")
            self._bump_cookies_version()
            self._reset_client()
        return merged

    def clear_session(self) -> None:
        """Forget cookies, account, current query and User-Agent. The circuit breaker stays: a fresh
        anonymous session is not a way around a captcha or a ban."""
        with self._lock:
            self.state.delete("cookies", "query.current", "account", "user_agent")
            self._bump_cookies_version()
            self._reset_client()

    # ------------------------------------------------------------------ circuit breaker
    def breaker(self) -> dict[str, Any] | None:
        """The open circuit breaker (``gate``, ``until``, ``url``, ``tripped_at``) or None."""
        until = self.state.get("breaker.until")
        try:
            if not until or time.time() >= float(until):
                return None
        except (TypeError, ValueError):
            return None
        return {"gate": self.state.get("breaker.gate"), "until": float(until), "url": self.state.get("breaker.url"),
                "tripped_at": self.state.get("breaker.tripped_at")}

    def reset_breaker(self, gates: Iterable[str] | None = None) -> bool:
        """Close the breaker; with ``gates``, only when the open breaker's gate is one of them.
        Returns True when an open breaker was closed."""
        current = self.breaker()
        if gates is not None and current is not None and current["gate"] not in set(gates):
            return False
        self.state.delete(*BREAKER_KEYS)
        return current is not None

    def release_breaker_for_check(self, url: str, *, force: bool = False) -> None:
        """Lift the breaker for one session check (``auth.check_session``) or raise its error.

        ``force`` (CLI ``elibrary auth check --force``: a human decision) lifts any pause. Otherwise a
        ban pause (ip_blocked, HTTP 403) is never lifted early, and a captcha / ip_restricted pause only
        when cookies with ``SUserID`` were imported after it started (``cookies.imported_at`` >
        ``breaker.tripped_at``, e.g. ``elibrary auth captcha`` ended without confirmation)."""
        current = self.breaker()
        if current is None:
            return
        if force:
            self.reset_breaker()
            return
        if current["gate"] in SESSION_GATES:
            imported, tripped = self.state.get(COOKIES_IMPORTED_KEY), current.get("tripped_at")
            with contextlib.suppress(TypeError, ValueError):
                if imported is not None and tripped is not None and float(imported) > float(tripped):
                    self.reset_breaker()
                    return
        self._check_breaker(url)

    def _trip_breaker(self, gate: Gate, url: str, minutes: float) -> None:
        now = time.time()
        self.state.set("breaker.until", now + minutes * 60)
        self.state.set("breaker.gate", gate.value)
        self.state.set("breaker.url", url)
        self.state.set("breaker.tripped_at", now)

    def _check_breaker(self, url: str) -> None:
        current = self.breaker()
        if current is None:
            return
        gate = current["gate"]
        if gate == Gate.CAPTCHA.value:
            raise CaptchaRequired(url, captcha_url=current["url"])
        mins = int((current["until"] - time.time()) // 60) + 1
        if gate in BAN_GATES:
            reason = "ip_blocked" if gate == Gate.IP_BLOCKED.value else "HTTP 403"
            raise RateLimited(BAN_PAUSE_HELP.format(minutes=mins, reason=reason), url=url)
        if gate == Gate.IP_RESTRICTED.value:
            raise RateLimited(
                f"Запросы к elibrary.ru приостановлены ещё на ~{mins} мин: сайт ограничил анонимный доступ с этого "
                "IP (ip_restricted.asp). Войдите в библиотеку в браузере и импортируйте cookies вошедшей сессии "
                "(`elibrary auth cookies`) или дождитесь окончания паузы.", url=url)
        raise RateLimited(f"Запросы к elibrary.ru приостановлены ещё на ~{mins} мин после ответа «{gate}».", url=url)

    def _raise_for_gate(self, gate: Gate, url: str, gate_url: str) -> None:
        """Challenge and ban pages (captcha, ip_blocked, HTTP 403, ip_restricted): open the circuit breaker
        and raise. Other gates return. Shared by :meth:`_send` and :meth:`download`."""
        if gate is Gate.CAPTCHA:
            self._trip_breaker(gate, gate_url, self.settings.breaker_minutes)
            raise CaptchaRequired(url, captcha_url=gate_url)
        if gate in (Gate.IP_BLOCKED, Gate.FORBIDDEN):
            self._trip_breaker(gate, gate_url, BAN_BREAKER_MINUTES)
            raise AccessBlocked(BAN_HELP.format(reason="ip_blocked" if gate is Gate.IP_BLOCKED else "HTTP 403"),
                                url=url)
        if gate is Gate.IP_RESTRICTED:
            self._trip_breaker(gate, gate_url, self.settings.breaker_minutes)
            raise IpRestricted(IP_RESTRICTED_HELP, url=url)

    def _gate_url(self, gate: Gate, url: str, final_url: str, *, posted: bool) -> str:
        """The URL stored in the breaker and shown to the user for a challenge page.

        After a POST it is rebuilt from the request URL without its query: elibrary turns a POST answered
        by the captcha into ``page_captcha.asp?rpage=<url>?<all form fields>``, which for the login form
        holds the password (it would end up in the state database, in error messages, in
        ``elibrary_session_status`` and in the browser opened by ``elibrary auth captcha``)."""
        if not posted:
            return final_url
        bare = urlunsplit(urlsplit(url)._replace(query="", fragment=""))
        page = _GATE_PAGES.get(gate)
        if page is None:
            return bare
        return f"{self.origin}/{page}?rpage={quote(bare, safe='')}"

    def _reset_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _save_cookies(self, responses: Sequence[httpx2.Response]) -> None:
        """Store the cookies the server set in this response chain over the saved ones.

        Only ``Set-Cookie`` of this exchange is written, never the client's whole jar: the jar may be
        older than the stored cookies (another process imported new ones), and writing it back would
        undo that import. Nothing is written when the cookies were replaced while the request was in
        flight (the answer belongs to the superseded session)."""
        new: dict[str, str] = {}
        for resp in responses:
            for c in resp.cookies.jar:
                if "elibrary" in (c.domain or "elibrary"):
                    new[c.name] = c.value or ""
        if not new or self._cookies_version() != self._client_version:
            return
        current = self.state.get("cookies", {}) or {}
        merged = {**current, **new}
        if merged != current:
            self.state.set("cookies", merged)
            self._bump_cookies_version(client_current=True)

    # ------------------------------------------------------------------ requests
    @property
    def origin(self) -> str:
        """Origin all requests go to: ``settings.base_url``, or the elibrary host the site redirected us
        to (``elibrary.ru`` <-> ``www.elibrary.ru``). Following that redirect on every request would
        double the page views, and a 302 drops the body of a POST form (the search would silently show
        the empty form)."""
        base = self.settings.base_url
        learned = self.state.get("origin.canonical")
        if learned and (urlsplit(base).hostname or "").lower() in _ELIBRARY_HOSTS:
            return str(learned)
        return base

    def _learn_origin(self, chain: list[httpx2.Response]) -> None:
        for r in chain[:-1]:
            loc = r.headers.get("location")
            if r.status_code in _REDIRECT_STATUSES and loc:
                new = _host_switch(str(r.url), urljoin(str(r.url), loc))
                if new and new != self.state.get("origin.canonical"):
                    self.state.set("origin.canonical", new)

    def absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return urljoin(self.origin + "/", path_or_url.lstrip("/"))

    def build_url(self, path: str, params: Params | None = None) -> str:
        url = self.absolute(path)
        query = encode_params(params)
        if query:
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        return url

    def get(self, path: str, params: Params | None = None, *, referer: str | None = None,
            cache_ttl_hours: float | None = None, follow_redirects: bool = True,
            cache_key: str | None = None, timeout: float | None = None) -> Page:
        """GET a page. ``cache_key`` replaces the URL-derived cache key; use it for URLs whose
        content depends on server-side session state (``query_results.asp?pagenum=N``).
        ``timeout``: read timeout for this request, seconds (heavy pages; default ``settings.timeout``)."""
        return self._request("GET", self.build_url(path, params), None, referer, cache_ttl_hours,
                             follow_redirects, cache_key, timeout)

    def post(self, path: str, data: Params, *, referer: str | None = None,
             cache_ttl_hours: float | None = TTL_NONE, cache_key: str | None = None) -> Page:
        return self._request("POST", self.absolute(path), encode_params(data), referer, cache_ttl_hours, True,
                             cache_key)

    def cached(self, method: str, url: str, body: str | None = None, ttl_hours: float | None = None) -> Page | None:
        ttl = self.settings.cache_ttl_hours if ttl_hours is None else ttl_hours
        return self.cache.get(PageCache.make_key(method, url, body), ttl * 3600)

    def _request(self, method: str, url: str, body: str | None, referer: str | None,
                 cache_ttl_hours: float | None, follow_redirects: bool, cache_key: str | None = None,
                 timeout: float | None = None) -> Page:
        ttl_h = self.settings.cache_ttl_hours if cache_ttl_hours is None else cache_ttl_hours
        key = PageCache.make_key("KEY", cache_key, None) if cache_key else PageCache.make_key(method, url, body)
        if not self.settings.refresh or self.settings.offline:
            cached = self.cache.get(key, float("inf") if self.settings.offline else ttl_h * 3600)
            if cached is not None:
                return cached
        if self.settings.offline:
            raise OfflineCacheMiss(f"Нет в кэше (ELIBRARY_OFFLINE=1): {url}", url=url)
        with self._lock:
            page = self._send(method, url, body, referer, follow_redirects, timeout)
        if page.status in (200, 301, 302) and ttl_h > 0:
            self.cache.put(key, page)
        return page

    def _before_send(self, url: str) -> None:
        """Book a throttle slot, sleep, then re-check the shared state right before the request: another
        process may have hit a captcha / ban (breaker) or imported cookies while this one slept."""
        self._check_breaker(url)
        wait = self.state.reserve_slot(self.THROTTLE_KEY, self.settings.min_delay, self.settings.jitter,
                                       self.settings.max_per_hour, self.settings.max_per_day)
        if wait > 0:
            time.sleep(wait)
        self._check_breaker(url)
        self._sync_client()

    def _exchange(self, method: str, url: str, content: bytes | None, headers: dict[str, str],
                  follow_redirects: bool, timeout: float | None) -> list[httpx2.Response]:
        """Send one request; returns the response chain (redirects first, the final response last).

        The redirects of a POST are followed here hop by hop, and a hop to a challenge page
        (page_captcha.asp, ip_blocked.asp, ip_restricted.asp) is classified without being requested:
        elibrary puts the whole form into that URL (the password of the login form), and following it
        would send the password in a query string and write it to the httpx log."""
        extra: dict[str, Any] = {}
        if timeout:
            extra["timeout"] = httpx2.Timeout(timeout, connect=20.0)
        if content is None:
            resp = self.client.request(method, url, headers=headers, follow_redirects=follow_redirects, **extra)
            return [*resp.history, resp]
        resp = self.client.request(method, url, content=content, headers=headers, follow_redirects=False, **extra)
        chain = [resp]
        resent = False
        while follow_redirects and resp.next_request is not None and len(chain) <= _MAX_POST_REDIRECTS:
            nxt = resp.next_request
            if classify(200, str(nxt.url), b"") in CHALLENGE_GATES:
                break
            new_origin = None if resent else _host_switch(str(resp.request.url), str(nxt.url))
            if new_origin:
                # elibrary.ru <-> www.elibrary.ru: a 301/302 turns the POST into a GET and drops the form,
                # so the same form is sent again to the other host (once)
                resent = True
                hop_headers = {**headers, "Origin": new_origin}
                if "Referer" in hop_headers:
                    hop_headers["Referer"] = urlunsplit(
                        urlsplit(hop_headers["Referer"])._replace(netloc=urlsplit(new_origin).netloc))
                resp = self.client.request(method, str(nxt.url), content=content, headers=hop_headers,
                                           follow_redirects=False, **extra)
            else:
                resp = self.client.send(nxt, follow_redirects=False)
            chain.append(resp)
        return chain

    def _send(self, method: str, url: str, body: str | None, referer: str | None,
              follow_redirects: bool, timeout: float | None = None) -> Page:
        headers = {"Referer": self.absolute(referer)} if referer else {}
        posted = body is not None
        if posted:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = self.origin
        # A POST is sent once (the server may have processed it; a second login looks like credential
        # stuffing); a GET is retried once after a connect-phase error or an HTTP 5xx page.
        attempts = 1 if posted else 2
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self._before_send(url)
            try:
                chain = self._exchange(method, url, body.encode("ascii") if body else None, headers,
                                       follow_redirects, timeout)
            except _RETRYABLE_ERRORS as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(10)
                continue
            except httpx2.TimeoutException as exc:  # read/write timeout: the server has the request
                limit = timeout or self.settings.timeout
                raise NetworkError(
                    f"elibrary.ru не ответил за {limit:.0f} с ({type(exc).__name__}). Запрос не повторяется "
                    "автоматически: сервер мог его уже получить и выполнить. Повторите позже; для тяжёлых страниц "
                    "увеличьте ELIBRARY_TIMEOUT / ELIBRARY_HEAVY_TIMEOUT.", url=url) from exc
            except (httpx2.ReadError, httpx2.WriteError, httpx2.RemoteProtocolError) as exc:
                raise NetworkError(
                    f"Связь с elibrary.ru оборвалась во время запроса ({type(exc).__name__}: {exc}). Запрос не "
                    "повторяется автоматически: сервер мог его уже получить. elibrary часто сбрасывает "
                    "соединения с зарубежных/облачных IP и VPN.", url=url) from exc
            except (httpx2.RequestError, httpx2.InvalidURL) as exc:
                # proxy errors, redirect loops, bad URLs...: retrying would not help
                raise NetworkError(f"Запрос к elibrary.ru не удался ({type(exc).__name__}: {exc})", url=url) from exc
            resp = chain[-1]
            self._save_cookies(chain)
            self._learn_origin(chain)
            self.state.set("requests.count", int(self.state.get("requests.count", 0)) + 1)
            final_url = str(resp.url)
            gate = classify(resp.status_code, final_url, resp.content)
            location = resp.headers.get("location")
            if gate is Gate.OK and location and resp.status_code in _REDIRECT_STATUSES:
                # a redirect that was not followed can still point at a challenge page: classify its target
                target = urljoin(final_url, location)
                target_gate = classify(200, target, b"")
                if target_gate in CHALLENGE_GATES:
                    gate, final_url = target_gate, target
            if gate in CHALLENGE_GATES:
                self._raise_for_gate(gate, url, self._gate_url(gate, url, final_url, posted=posted))
            if gate is Gate.NOT_FOUND:
                raise NotFound(f"Страница не найдена: {url}", url=url)
            if gate is Gate.SERVER_ERROR:
                last_exc = NetworkError(f"elibrary.ru вернул ошибку сервера (HTTP {resp.status_code})", url=url)
                if attempt + 1 < attempts:
                    time.sleep(10)
                continue
            return Page(url=final_url, status=resp.status_code, content=resp.content,
                        content_type=resp.headers.get("content-type"), location=location)
        if isinstance(last_exc, NetworkError):
            raise last_exc
        raise NetworkError(
            f"Нет связи с elibrary.ru ({type(last_exc).__name__}: {last_exc}). Если это первая попытка — "
            "elibrary часто сбрасывает соединения с зарубежных/облачных IP и VPN.", url=url)

    def download(self, url: str, dest: Path, *, referer: str | None = None,
                 max_bytes: int | None = None) -> tuple[Path, str | None]:
        """Download a binary file (PDF) through the same session; returns (path, content-type).

        The body is streamed into ``dest.part`` and renamed at the end. A file larger than ``max_bytes``
        (default ``settings.max_download_mb``, ``ELIBRARY_MAX_DOWNLOAD_MB``) is aborted — by the declared
        ``Content-Length`` before anything is written, otherwise as soon as the limit is crossed — the
        partial file is deleted and :class:`ElibraryError` is raised. In offline mode nothing is sent
        (:class:`OfflineCacheMiss`). A captcha, ban, 403 or ip_restricted answer opens the circuit breaker
        exactly like a page request does."""
        url = self.absolute(url)
        if self.settings.offline:
            raise OfflineCacheMiss(f"Скачивание недоступно в офлайн-режиме (ELIBRARY_OFFLINE=1): {url}", url=url)
        limit = max_bytes if max_bytes is not None else self.settings.max_download_bytes
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        too_large: str | None = None
        with self._lock:
            self._before_send(url)
            headers = {"Referer": self.absolute(referer)} if referer else {}
            try:
                with self.client.stream("GET", url, headers=headers) as resp:
                    chain = [*resp.history, resp]
                    ctype = resp.headers.get("content-type")
                    final_url = str(resp.url)
                    status = resp.status_code
                    declared = (resp.headers.get("content-length") or "").strip()
                    if declared.isdigit() and int(declared) > limit:
                        too_large = f"{_megabytes(int(declared))} МБ по заголовку Content-Length"
                    else:
                        size = 0
                        with tmp.open("wb") as fh:
                            for chunk in resp.iter_bytes():
                                size += len(chunk)
                                if size > limit:
                                    too_large = f"больше {_megabytes(limit)} МБ"
                                    break
                                fh.write(chunk)
            except (httpx2.RequestError, httpx2.InvalidURL, httpx2.StreamError) as exc:
                tmp.unlink(missing_ok=True)
                raise NetworkError(f"Не удалось скачать {url}: {exc}", url=url) from exc
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            self._save_cookies(chain)
            self.state.set("requests.count", int(self.state.get("requests.count", 0)) + 1)
        if too_large is not None:
            tmp.unlink(missing_ok=True)
            raise ElibraryError(
                f"Файл {url} слишком большой ({too_large}; предел {_megabytes(limit)} МБ задаётся "
                "ELIBRARY_MAX_DOWNLOAD_MB). Скачивание прервано, недокачанный файл удалён.", url=url)
        with tmp.open("rb") as fh:
            head = fh.read(20000)
        gate = classify(status, final_url, head)
        if gate is not Gate.OK:
            tmp.unlink(missing_ok=True)
            self._raise_for_gate(gate, url, final_url)
            if gate is Gate.NOT_FOUND:
                raise NotFound(f"Файл не найден: {url}", url=url)
            raise AccessBlocked(f"Скачивание отклонено elibrary.ru ({gate.value}): {url}", url=url)
        tmp.replace(dest)
        return dest, ctype

    # ------------------------------------------------------------------ misc
    def status(self) -> dict:
        cookies = self.cookies()
        until = self.state.get("breaker.until")
        throttle = self.state.get(self.THROTTLE_KEY, {}) or {}
        now = time.time()
        stamps = [t for t in (throttle.get("stamps", []) if isinstance(throttle, dict) else []) if now - t < 86400]
        return {
            "base_url": self.settings.base_url,
            "origin": self.origin,
            "has_session_cookie": bool(cookies.get("SUserID")),
            "cookie_names": sorted(cookies),
            "account": self.state.get("account"),
            "breaker_open": bool(until and now < float(until)),
            "breaker_gate": self.state.get("breaker.gate") if until and now < float(until) else None,
            "breaker_until": until if until and now < float(until) else None,
            "requests_last_hour": sum(1 for t in stamps if now - t < 3600),
            "requests_last_day": len(stamps),
            "max_per_hour": self.settings.max_per_hour,
            "max_per_day": self.settings.max_per_day,
            "requests_total": self.state.get("requests.count", 0),
            "min_delay_s": self.settings.min_delay,
            "state_db": str(self.settings.state_db),
            "cache_db": str(self.settings.cache_db),
            "cache": self.cache.stats(),
            "offline": self.settings.offline,
        }

    def close(self) -> None:
        self._reset_client()
        self.state.close()
        self.cache.close()

    def __enter__(self) -> ElibraryHTTP:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
