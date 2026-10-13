"""Runtime settings, read from environment variables.

Every setting has an ``ELIBRARY_*`` environment variable. Zotero settings reuse the variable
names of zotero-mcp (``ZOTERO_LOCAL``, ``ZOTERO_API_KEY``, ``ZOTERO_LIBRARY_ID``,
``ZOTERO_LIBRARY_TYPE``) so one configuration serves both servers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

APP_NAME = "elibrary-cli"
DEFAULT_BASE_URL = "https://elibrary.ru"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y", "да"}


def _env_str(name: str) -> str | None:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else None


@dataclass
class Settings:
    """All tunables of the client. Construct with :meth:`from_env`."""

    base_url: str = DEFAULT_BASE_URL
    data_dir: Path = field(default_factory=lambda: Path(platformdirs.user_data_dir(APP_NAME, appauthor=False)))
    cache_dir: Path = field(default_factory=lambda: Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False)))
    #: Minimum pause between two requests to elibrary.ru, seconds. elibrary shows a reCAPTCHA
    #: after roughly a dozen quick page views from an anonymous session, so keep this generous.
    min_delay: float = 5.0
    #: Random extra pause added to ``min_delay`` (uniform 0..jitter), seconds.
    jitter: float = 2.0
    timeout: float = 60.0
    #: Read timeout for pages the server computes slowly (the last pages of a large organization's
    #: ``org_items.asp`` answer in more than 60 s), seconds (``ELIBRARY_HEAVY_TIMEOUT``). Such requests are
    #: never retried after a timeout, so they get one long wait instead.
    heavy_timeout: float = 180.0
    #: Default cache lifetime for pages, hours (0 disables the cache). Callers pass a TTL per page
    #: type (item pages 30 days, profiles 7 days, searches 1 day); this is the fallback.
    cache_ttl_hours: float = 72.0
    #: Cache lifetime for search result pages, hours.
    search_cache_ttl_hours: float = 24.0
    #: Local request budgets (protect against runaway agents and the elibrary ban).
    max_per_hour: int = 60
    max_per_day: int = 300
    #: After a captcha/ban page no request is sent for this many minutes unless the user
    #: imports cookies or runs ``elibrary auth check``.
    breaker_minutes: float = 15.0
    #: Bypass the cache and always fetch fresh pages.
    refresh: bool = False
    user_agent: str = DEFAULT_USER_AGENT
    #: Cookie header to force (e.g. ``"SUserID=...; SCookieGUID=..."``), overrides the saved session.
    cookies: str | None = None
    login: str | None = None
    password: str | None = None
    #: Contact e-mail sent to open-access APIs (Unpaywall requires one; Crossref/OpenAlex "polite pool").
    contact_email: str | None = None
    offline: bool = False
    #: Largest file (PDF) a download may write, MB; bigger downloads are aborted and the partial file
    #: is deleted (``ELIBRARY_MAX_DOWNLOAD_MB``). Applies to elibrary.ru and to external hosts.
    max_download_mb: float = 100.0
    #: Opt-in: let the ``cyberleninka`` full-text source call CyberLeninka's search API
    #: (``POST /api/search``) when the article page cannot be derived from the title. CyberLeninka's
    #: robots.txt disallows ``/api/`` for bots, so this is off by default (``ELIBRARY_CYBERLENINKA_SEARCH``).
    cyberleninka_search: bool = False

    # Zotero (same variables as zotero-mcp)
    zotero_local: bool = False
    zotero_api_key: str | None = None
    zotero_library_id: str | None = None
    zotero_library_type: str = "user"

    @classmethod
    def from_env(cls) -> Settings:
        s = cls()
        s.base_url = (_env_str("ELIBRARY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if home := _env_str("ELIBRARY_HOME"):
            s.data_dir = Path(home).expanduser()
            s.cache_dir = Path(home).expanduser() / "cache"
        if cache := _env_str("ELIBRARY_CACHE_DIR"):
            s.cache_dir = Path(cache).expanduser()
        s.min_delay = max(0.0, _env_float("ELIBRARY_MIN_DELAY", s.min_delay))
        s.jitter = max(0.0, _env_float("ELIBRARY_JITTER", s.jitter))
        s.timeout = max(5.0, _env_float("ELIBRARY_TIMEOUT", s.timeout))
        s.heavy_timeout = max(s.timeout, _env_float("ELIBRARY_HEAVY_TIMEOUT", s.heavy_timeout))
        s.cache_ttl_hours = max(0.0, _env_float("ELIBRARY_CACHE_TTL", s.cache_ttl_hours))
        s.search_cache_ttl_hours = max(0.0, _env_float("ELIBRARY_SEARCH_CACHE_TTL", s.search_cache_ttl_hours))
        s.max_per_hour = int(max(1, _env_float("ELIBRARY_MAX_PER_HOUR", s.max_per_hour)))
        s.max_per_day = int(max(1, _env_float("ELIBRARY_MAX_PER_DAY", s.max_per_day)))
        s.breaker_minutes = max(0.0, _env_float("ELIBRARY_BREAKER_MINUTES", s.breaker_minutes))
        s.refresh = _env_bool("ELIBRARY_REFRESH")
        s.user_agent = _env_str("ELIBRARY_USER_AGENT") or s.user_agent
        s.cookies = _env_str("ELIBRARY_COOKIES")
        s.login = _env_str("ELIBRARY_LOGIN")
        s.password = _env_str("ELIBRARY_PASSWORD")
        s.contact_email = _env_str("ELIBRARY_CONTACT_EMAIL") or _env_str("UNPAYWALL_EMAIL")
        s.offline = _env_bool("ELIBRARY_OFFLINE")
        max_mb = _env_float("ELIBRARY_MAX_DOWNLOAD_MB", s.max_download_mb)
        s.max_download_mb = max_mb if max_mb > 0 else s.max_download_mb
        s.cyberleninka_search = _env_bool("ELIBRARY_CYBERLENINKA_SEARCH")
        s.zotero_local = _env_bool("ZOTERO_LOCAL")
        s.zotero_api_key = _env_str("ZOTERO_API_KEY")
        s.zotero_library_id = _env_str("ZOTERO_LIBRARY_ID")
        s.zotero_library_type = _env_str("ZOTERO_LIBRARY_TYPE") or "user"
        return s

    @property
    def state_db(self) -> Path:
        """SQLite file with the session cookies, throttle clock and HTTP cache."""
        return self.data_dir / "state.sqlite3"

    @property
    def cache_db(self) -> Path:
        return self.cache_dir / "http-cache.sqlite3"

    @property
    def downloads_dir(self) -> Path:
        return self.cache_dir / "downloads"

    @property
    def max_download_bytes(self) -> int:
        """:attr:`max_download_mb` in bytes."""
        return int(self.max_download_mb * 1024 * 1024)
