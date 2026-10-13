"""Save items to Zotero.

Three backends, tried in this order by ``backend="auto"``:

``local``
    The local API of Zotero 10+ with write support (``http://127.0.0.1:23119/api/users/0/items``).
    Needs a *local* API key (``POST /api/local/authorize`` shows an Allow / Always Allow / Deny dialog
    in Zotero; :meth:`ZoteroWriter.authorize_local`), plus the ``Zotero-Server-ID`` header. Returns
    item keys, can create collections and upload PDFs into local storage.
``connector``
    The endpoints the browser connector uses (``/connector/saveItems``, ``/connector/saveAttachment``,
    ``/connector/updateSession``). Needs nothing but a running Zotero desktop. Items go to the
    collection selected in Zotero unless ``collection`` names an existing one (a group library's
    too). Dedupe and the keys of the new items go through the (keyless, read-only) local API of the
    library the items are saved to, when it is enabled; otherwise each result says the duplicate
    check was not done.
``web``
    The Zotero Web API (``https://api.zotero.org``) with ``ZOTERO_API_KEY`` + ``ZOTERO_LIBRARY_ID``.

``local`` and ``web`` speak the same API v3: pyzotero (extra ``zotero``) is used when it is
installed, otherwise a small built-in client on ``httpx2``. The connector never needs pyzotero.

Configuration follows zotero-mcp (same environment variables, and its config file
``~/.config/zotero-mcp/config.json``: ``client_env`` and ``local_api``), so one setup serves both.
Nothing here talks to elibrary.ru; PDFs are attached from local files only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx2

from elibrary_cli import __version__
from elibrary_cli.config import Settings
from elibrary_cli.errors import DependencyMissing, ElibraryError

log = logging.getLogger(__name__)

#: zotero-mcp's config file (``config.ZOTERO_MCP_CONFIG_PATH`` in zotero-mcp; same path on Windows).
ZOTERO_MCP_CONFIG_PATH = Path.home() / ".config" / "zotero-mcp" / "config.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 23119
WEB_API_URL = "https://api.zotero.org"
APP_NAME = "elibrary-cli"
#: Backend names in the order ``backend="auto"`` tries them.
BACKENDS: tuple[str, ...] = ("local", "connector", "web")
#: File (in ``Settings.data_dir``) where elibrary-cli keeps its own local API key.
KEY_FILE_NAME = "zotero.json"
#: Objects per write request (Web API and local API limit).
MAX_WRITE_ITEMS = 50
DEFAULT_ATTACHMENT_TITLE = "Полный текст (PDF)"
USER_AGENT = f"elibrary-cli/{__version__} (+https://github.com/lujo777/elibrary-cli)"

_CONNECTOR_HEADERS = {"X-Zotero-Connector-API-Version": "3", "Zotero-Allowed-Request": "1"}
_TRUTHY = {"1", "true", "yes", "on", "y", "да"}
_COLLECTION_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
_SKIP_TYPES = {"attachment", "note", "annotation"}

NOT_RUNNING = (
    "Zotero не запущен или недоступен на {url}: запустите Zotero (настольное приложение) и повторите. "
    "Либо задайте ZOTERO_API_KEY и ZOTERO_LIBRARY_ID для записи через Zotero Web API."
)
LOCAL_API_DISABLED = (
    "Локальный API Zotero выключен. Включите в Zotero: Настройки → Дополнительно → «Разрешить другим "
    "приложениям на этом компьютере взаимодействовать с Zotero» (Allow other applications on this "
    "computer to communicate with Zotero)."
)
DEDUPE_LOCAL_API_OFF = (
    "локальный API Zotero выключен (Zotero → Настройки → Дополнительно → «Разрешить другим приложениям на этом "
    "компьютере взаимодействовать с Zotero»)"
)
DEDUPE_SKIPPED = (
    "Проверка на дубликаты не выполнена: {reason}. Повторное сохранение создаст ещё одну копию записи."
)
LOCAL_KEY_REJECTED = (
    "Локальный API Zotero не принял ключ (401): ключ отозван или был одноразовым («Разрешить» вместо "
    "«Всегда разрешать»). Выполните `elibrary zotero authorize` и выберите «Всегда разрешать»."
)


class ZoteroError(ElibraryError):
    """A Zotero request failed (the message says why and what to do)."""


class ZoteroUnavailable(ZoteroError):
    """No usable backend: Zotero is not running, the local API is off, no web credentials..."""


class ZoteroAuthError(ZoteroError):
    """A key was rejected (local API key revoked / single-use, Web API key without write access)."""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _read_json(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- configuration


@dataclass
class ZoteroConfig:
    """Where and how to write. Build it with :meth:`from_env_and_zotero_mcp`."""

    #: ``ZOTERO_LOCAL``: True/False when set, None when not set (auto).
    local: bool | None = None
    api_key: str | None = None
    library_id: str | None = None
    library_type: str = "user"
    local_api_key: str | None = None
    local_server_id: str | None = None
    #: ``ZOTERO_LOCAL_WRITE``: unset/``auto``/true -> local API writes allowed.
    local_write: bool = True
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    web_url: str = WEB_API_URL
    timeout: float = 120.0
    app_name: str = APP_NAME
    #: Where :meth:`save_local_key` stores the key granted to elibrary-cli.
    key_store: Path | None = None
    #: ``env`` / ``elibrary-cli`` / ``zotero-mcp`` / ``authorize``: where the local key came from.
    local_key_source: str | None = None
    #: The zotero-mcp config file that was read (None when absent).
    zotero_mcp_config: Path | None = None

    @classmethod
    def from_env_and_zotero_mcp(cls, settings: Settings | None = None, *,
                                environ: Mapping[str, str] | None = None,
                                zotero_mcp_config: Path | None = None) -> ZoteroConfig:
        """Read the zotero-mcp variables (``ZOTERO_LOCAL``, ``ZOTERO_API_KEY``, ``ZOTERO_LIBRARY_ID``,
        ``ZOTERO_LIBRARY_TYPE``, ``ZOTERO_LOCAL_API_KEY``, ``ZOTERO_LOCAL_SERVER_ID``,
        ``ZOTERO_LOCAL_WRITE``, ``ZOTERO_LOCAL_PORT``) with zotero-mcp's precedence: values set on
        ``settings`` / the process environment first, then ``client_env`` of zotero-mcp's
        ``config.json``. The local API key: ``ZOTERO_LOCAL_API_KEY`` -> the key elibrary-cli was
        granted (``<data_dir>/zotero.json``) -> zotero-mcp's ``local_api.key``.
        """
        env = os.environ if environ is None else environ
        if settings is None:
            settings = Settings.from_env() if environ is None else Settings()
        path = zotero_mcp_config if zotero_mcp_config is not None else ZOTERO_MCP_CONFIG_PATH
        data = _read_json(path)
        client_env = data.get("client_env") if isinstance(data.get("client_env"), dict) else {}

        def get(name: str) -> str | None:
            raw = env.get(name)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
            raw = client_env.get(name)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
            return None

        cfg = cls()
        cfg.zotero_mcp_config = path if data else None
        raw_local = get("ZOTERO_LOCAL")
        cfg.local = _truthy(raw_local) if raw_local is not None else (True if settings.zotero_local else None)
        cfg.api_key = settings.zotero_api_key or get("ZOTERO_API_KEY")
        cfg.library_id = settings.zotero_library_id or get("ZOTERO_LIBRARY_ID")
        ltype = settings.zotero_library_type if settings.zotero_library_type != "user" else None
        ltype = (ltype or get("ZOTERO_LIBRARY_TYPE") or "user").lower()
        cfg.library_type = "group" if ltype.startswith("group") else "user"
        raw_write = (get("ZOTERO_LOCAL_WRITE") or "auto").lower()
        cfg.local_write = raw_write == "auto" or raw_write in _TRUTHY
        try:
            cfg.port = int(get("ZOTERO_LOCAL_PORT") or DEFAULT_PORT)
        except ValueError:
            cfg.port = DEFAULT_PORT
        cfg.key_store = Path(settings.data_dir) / KEY_FILE_NAME
        if key := get("ZOTERO_LOCAL_API_KEY"):
            cfg.local_api_key, cfg.local_server_id, cfg.local_key_source = key, get("ZOTERO_LOCAL_SERVER_ID"), "env"
        else:
            own = _read_json(cfg.key_store).get("local_api") or {}
            mcp = data.get("local_api") if isinstance(data.get("local_api"), dict) else {}
            for source, rec in (("elibrary-cli", own), ("zotero-mcp", mcp)):
                if isinstance(rec, dict) and rec.get("key"):
                    cfg.local_api_key = str(rec["key"])
                    cfg.local_server_id = str(rec["server_id"]) if rec.get("server_id") else None
                    cfg.local_key_source = source
                    break
        return cfg

    @property
    def local_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def web_ready(self) -> bool:
        """Web API credentials are complete (``ZOTERO_LIBRARY_ID=0`` is zotero-mcp's local placeholder)."""
        return bool(self.api_key and self.library_id and self.library_id != "0")

    def local_prefix(self) -> str:
        if self.library_type == "group":
            if not self.library_id or self.library_id == "0":
                raise ZoteroError("Для групповой библиотеки задайте ZOTERO_LIBRARY_ID (номер группы).")
            return f"/api/groups/{self.library_id}"
        return "/api/users/0"

    def web_prefix(self) -> str:
        return f"/{'groups' if self.library_type == 'group' else 'users'}/{self.library_id}"

    def describe(self) -> dict[str, Any]:
        """Configuration without secrets (keys are reported as present/absent)."""
        return {
            "local": self.local, "local_write": self.local_write, "local_url": self.local_url,
            "local_api_key": bool(self.local_api_key), "local_key_source": self.local_key_source,
            "web_configured": self.web_ready, "library_type": self.library_type,
            "library_id": self.library_id,
            "zotero_mcp_config": str(self.zotero_mcp_config) if self.zotero_mcp_config else None,
            "key_store": str(self.key_store) if self.key_store else None,
        }

    def save_local_key(self, key: str, server_id: str | None, *, app_name: str | None = None) -> Path | None:
        """Persist a local API key granted with "Always Allow" (file mode 0600 where supported)."""
        if not self.key_store:
            return None
        path = self.key_store
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_json(path)
        data["local_api"] = {
            "key": key, "server_id": server_id, "app_name": app_name or self.app_name, "remember": True,
            "granted": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path


# --------------------------------------------------------------------------- API v3 clients


class _Api(Protocol):
    backend: str

    def search(self, q: str, *, newest_first: bool = False) -> list[dict[str, Any]]: ...

    def collections(self) -> list[dict[str, Any]]: ...

    def create_collection(self, name: str, parent: str | None) -> str: ...

    def create_items(self, items: list[dict[str, Any]]) -> dict[str, Any]: ...

    def upload_attachment(self, parent_key: str, path: Path, title: str) -> str: ...


def _content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _attachment_item(parent_key: str, path: Path, title: str) -> dict[str, Any]:
    return {"itemType": "attachment", "parentItem": parent_key, "linkMode": "imported_file", "title": title,
            "contentType": _content_type(path), "charset": "", "filename": path.name, "tags": [],
            "relations": {}}


class _NativeApi:
    """Minimal Zotero API v3 client (Web API and the Zotero 10+ local API) on httpx2."""

    def __init__(self, http: httpx2.Client, base_url: str, prefix: str, *, backend: str,
                 api_key: str | None = None, server_id: str | None = None) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.prefix = prefix
        self.backend = backend
        self.local = backend != "web"
        self.api_key = api_key
        self.server_id = server_id

    # -- plumbing
    def _headers(self, write: bool) -> dict[str, str]:
        h = {"Zotero-API-Version": "3"}
        if self.local:
            if self.server_id:
                h["Zotero-Server-ID"] = self.server_id
            if write and self.api_key:
                h["Zotero-API-Key"] = self.api_key
        elif self.api_key:
            h["Zotero-API-Key"] = self.api_key
        return h

    def _send(self, method: str, path: str, *, write: bool = False, what: str = "запрос",
              headers: Mapping[str, str] | None = None, **kwargs: Any) -> httpx2.Response:
        url = path if path.startswith("http") else self.base_url + path
        try:
            resp = self.http.request(method, url, headers={**self._headers(write), **(headers or {})}, **kwargs)
        except httpx2.TimeoutException as exc:
            raise ZoteroError(f"Zotero не ответил вовремя ({what}): {exc}") from exc
        except httpx2.HTTPError as exc:
            if self.local:
                raise ZoteroUnavailable(NOT_RUNNING.format(url=self.base_url)) from exc
            raise ZoteroError(f"Нет связи с {self.base_url} ({what}): {exc}") from exc
        if self.local and not self.server_id and resp.headers.get("Zotero-Server-ID"):
            self.server_id = resp.headers["Zotero-Server-ID"]
        self._check(resp, what)
        return resp

    def _check(self, resp: httpx2.Response, what: str) -> None:
        code = resp.status_code
        if code < 400:
            return
        text = resp.text[:300].strip()
        if self.local:
            if code == 401:
                raise ZoteroAuthError(LOCAL_KEY_REJECTED)
            if code == 403 and "not enabled" in text.lower():
                raise ZoteroUnavailable(LOCAL_API_DISABLED)
            if code == 403:
                raise ZoteroAuthError(f"Zotero отказал в доступе к локальному API (403): {text}")
            if code == 412:
                raise ZoteroAuthError(
                    "Ключ локального API выдан другой установке Zotero (412: Zotero-Server-ID не совпадает). "
                    "Выполните `elibrary zotero authorize` заново.")
            if code == 428:
                raise ZoteroError("Zotero требует заголовок Zotero-Server-ID (428): обновите Zotero до версии 10+.")
        else:
            if code in (401, 403):
                raise ZoteroAuthError(
                    f"Zotero Web API отклонил ключ ZOTERO_API_KEY ({code}): проверьте ключ и право записи "
                    f"в библиотеку {self.prefix.strip('/')} (https://www.zotero.org/settings/keys).")
            if code == 404:
                raise ZoteroError(
                    f"Библиотека {self.prefix.strip('/')} не найдена в Zotero Web API: проверьте "
                    "ZOTERO_LIBRARY_ID и ZOTERO_LIBRARY_TYPE.")
            if code == 413:
                raise ZoteroError("Превышена квота файлового хранилища Zotero (413): файл не загружен.")
            if code == 429:
                wait = resp.headers.get("Retry-After") or resp.headers.get("Backoff") or "несколько"
                raise ZoteroError(f"Zotero Web API ограничил частоту запросов (429): повторите через {wait} с.")
        raise ZoteroError(f"Zotero ({self.backend}) ответил {code} на {what}: {text}")

    # -- operations
    def search(self, q: str, *, newest_first: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": q, "qmode": "everything", "limit": 25, "format": "json"}
        if newest_first:
            params.update(sort="dateAdded", direction="desc")
        data = self._send("GET", f"{self.prefix}/items", params=params, what="поиск").json()
        return data if isinstance(data, list) else []

    def collections(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            params = {"format": "json", "limit": 100, "start": start}
            resp = self._send("GET", f"{self.prefix}/collections", params=params, what="список коллекций")
            page = resp.json() if resp.content else []
            if not isinstance(page, list):
                break
            out.extend(page)
            total = resp.headers.get("Total-Results")
            if len(page) < 100 or (total and len(out) >= int(total)):
                break
            start += len(page)
        return out

    def create_collection(self, name: str, parent: str | None) -> str:
        body = [{"name": name, "parentCollection": parent or False}]
        data = self._send("POST", f"{self.prefix}/collections", write=True, json=body, what="создание коллекции",
                          headers={"Zotero-Write-Token": uuid.uuid4().hex}).json()
        key = (data.get("success") or {}).get("0")
        if not key:
            raise ZoteroError(f"Zotero не создал коллекцию «{name}»: {(data.get('failed') or {}).get('0')}")
        return str(key)

    def create_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        resp = self._send("POST", f"{self.prefix}/items", write=True, json=items, what="создание записей",
                          headers={"Zotero-Write-Token": uuid.uuid4().hex})
        return resp.json()

    def upload_attachment(self, parent_key: str, path: Path, title: str) -> str:
        created = self.create_items([_attachment_item(parent_key, path, title)])
        key = (created.get("success") or {}).get("0")
        if not key:
            raise ZoteroError(f"Zotero не создал вложение: {(created.get('failed') or {}).get('0')}")
        content = path.read_bytes()
        form = {
            "md5": hashlib.md5(content).hexdigest(),  # the file-upload protocol uses MD5
            "filename": path.name,
            "filesize": str(len(content)),
            "mtime": str(max(int(path.stat().st_mtime * 1000), 10_000_000_000)),
            "contentType": _content_type(path),
        }
        auth = self._send("POST", f"{self.prefix}/items/{key}/file", write=True, data=form,
                          headers={"If-None-Match": "*"}, what="авторизацию загрузки файла").json()
        if auth.get("exists"):
            return "exists"
        body = (auth.get("prefix") or "").encode("utf-8") + content + (auth.get("suffix") or "").encode("utf-8")
        up_headers = {"Content-Type": auth.get("contentType") or form["contentType"]}
        if self.local and self.server_id:
            up_headers["Zotero-Server-ID"] = self.server_id
        try:
            up = self.http.post(auth["url"], content=body, headers=up_headers)
        except httpx2.HTTPError as exc:
            raise ZoteroError(f"Не удалось передать файл в хранилище Zotero: {exc}") from exc
        if up.status_code >= 300:
            raise ZoteroError(f"Хранилище Zotero не приняло файл ({up.status_code}): {up.text[:200]}")
        self._send("POST", f"{self.prefix}/items/{key}/file", write=True, data={"upload": auth.get("uploadKey")},
                   headers={"If-None-Match": "*"}, what="регистрацию файла")
        return "uploaded"


class _PyzoteroApi:
    """The same operations through pyzotero (extra ``zotero``)."""

    def __init__(self, zot: Any, backend: str) -> None:
        self.zot = zot
        self.backend = backend

    def _call(self, what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ZoteroError:
            raise
        except httpx2.HTTPError as exc:
            if self.backend == "local":
                raise ZoteroUnavailable(NOT_RUNNING.format(url="127.0.0.1")) from exc
            raise ZoteroError(f"Нет связи с Zotero Web API ({what}): {exc}") from exc
        except Exception as exc:  # pyzotero raises its own exception classes
            name = type(exc).__name__
            if name in {"UserNotAuthorisedError", "LocalAPIKeyRequiredError", "LocalAPIDeniedError",
                        "MissingCredentialsError"}:
                if self.backend == "local":
                    raise ZoteroAuthError(LOCAL_KEY_REJECTED) from exc
                raise ZoteroAuthError(f"Zotero Web API отклонил ключ ZOTERO_API_KEY ({name}): {exc}") from exc
            raise ZoteroError(f"pyzotero ({self.backend}, {what}): {name}: {exc}") from exc

    def search(self, q: str, *, newest_first: bool = False) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"q": q, "qmode": "everything", "limit": 25}
        if newest_first:
            kwargs.update(sort="dateAdded", direction="desc")
        res = self._call("поиск", self.zot.items, **kwargs)
        return res if isinstance(res, list) else []

    def collections(self) -> list[dict[str, Any]]:
        res = self._call("список коллекций", lambda: self.zot.everything(self.zot.collections()))
        return res if isinstance(res, list) else []

    def create_collection(self, name: str, parent: str | None) -> str:
        data = self._call("создание коллекции", self.zot.create_collections,
                          [{"name": name, "parentCollection": parent or False}])
        key = (data.get("success") or {}).get("0") if isinstance(data, dict) else None
        if not key:
            raise ZoteroError(f"Zotero не создал коллекцию «{name}»: {data}")
        return str(key)

    def create_items(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._call("создание записей", self.zot.create_items, items)

    def upload_attachment(self, parent_key: str, path: Path, title: str) -> str:
        att = _attachment_item(parent_key, path, title)
        att.pop("parentItem")
        att["filename"] = str(path)
        res = self._call("загрузку файла", self.zot.upload_attachments, [att], parentid=parent_key)
        if isinstance(res, dict):
            if res.get("success"):
                return "uploaded"
            if res.get("unchanged"):
                return "exists"
            raise ZoteroError(f"pyzotero не загрузил файл: {res.get('failure')}")
        return "uploaded"


# --------------------------------------------------------------------------- matching helpers


def _extra_value(extra: str | None, label: str) -> str | None:
    m = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(\S+)\s*$", extra or "")
    return m.group(1) if m else None


_ELIB_URL_RE = re.compile(r"^elibrary\.ru/item\.asp\?(?P<query>[^#]*)")


def item_identifiers(item: Mapping[str, Any]) -> dict[str, str]:
    """``doi`` / ``edn`` / ``elibrary_id`` / ``url`` of a Zotero item (from fields and ``extra``; the
    eLIBRARY ID and the EDN also from an ``elibrary.ru/item.asp?id=…`` / ``?edn=…`` URL, which is all
    Zotero's own eLibrary.ru translator stores)."""
    data = item.get("data") if isinstance(item.get("data"), dict) else item
    extra = str(data.get("extra") or "")
    out: dict[str, str] = {}
    if doi := (data.get("DOI") or _extra_value(extra, "DOI")):
        out["doi"] = str(doi).strip().lower()
    if edn := _extra_value(extra, "EDN"):
        out["edn"] = edn.upper()
    if eid := _extra_value(extra, "eLIBRARY ID"):
        out["elibrary_id"] = eid
    if url := data.get("url"):
        out["url"] = _norm_url(str(url))
        if m := _ELIB_URL_RE.match(out["url"]):
            params = dict(p.partition("=")[::2] for p in m.group("query").split("&"))
            if params.get("id", "").isdigit():
                out.setdefault("elibrary_id", params["id"])
            if re.fullmatch(r"[a-z0-9]{6,}", params.get("edn", "")):
                out.setdefault("edn", params["edn"].upper())
    return out


def _norm_url(url: str) -> str:
    return re.sub(r"^https?://(?:www\.)?", "", url.strip().rstrip("/")).lower()


def _same_item(candidate: Mapping[str, Any], ids: Mapping[str, str]) -> bool:
    other = item_identifiers(candidate)
    return any(k in ids and other.get(k) == ids[k] for k in ("doi", "edn", "elibrary_id", "url"))


def _find_existing(api: _Api, item: Mapping[str, Any], *, newest_first: bool = False) -> str | None:
    ids = item_identifiers(item)
    queries = [q for q in (ids.get("edn"), ids.get("doi")) if q]
    if ids.get("elibrary_id"):
        # always: Zotero's eLibrary.ru translator (the browser button) saves the page URL, no EDN, and
        # many Russian items have no DOI; the EDN query also finds its "item.asp?edn=…" URLs
        queries.append(f"item.asp?id={ids['elibrary_id']}")
    for q in dict.fromkeys(queries):
        for cand in api.search(q, newest_first=newest_first):
            data = cand.get("data") if isinstance(cand.get("data"), dict) else cand
            if data.get("itemType") in _SKIP_TYPES or data.get("deleted"):
                continue
            if _same_item(data, ids):
                return str(cand.get("key") or data.get("key") or "") or None
    return None


def _batch_repeats(items: list[dict[str, Any]]) -> dict[int, int]:
    """``{index: index of the first item of the batch with the same DOI / EDN / eLIBRARY ID / URL}``."""
    seen: dict[tuple[str, str], int] = {}
    out: dict[int, int] = {}
    for i, it in enumerate(items):
        keys = [(k, v) for k, v in item_identifiers(it).items() if v]
        first = next((seen[k] for k in keys if k in seen), None)
        if first is not None:
            out[i] = first
        for k in keys:
            seen.setdefault(k, i if first is None else first)
    return out


def _collection_path(key: str, by_key: Mapping[str, Mapping[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    cur: str | None = key
    while cur and cur in by_key and cur not in seen:
        seen.add(cur)
        data = by_key[cur].get("data") or by_key[cur]
        names.insert(0, str(data.get("name") or ""))
        cur = data.get("parentCollection") or None
    return names


def _split_path(collection: str) -> list[str]:
    return [p.strip() for p in collection.strip().strip("/").split("/") if p.strip()]


# --------------------------------------------------------------------------- the writer


class ZoteroWriter:
    """Writes Zotero items (dicts from :func:`elibrary_cli.exports.to_zotero_item`) to Zotero.

    ``backend``: ``"auto"`` (local -> connector -> web, first usable), or one of :data:`BACKENDS`.
    ``transport`` replaces the network (tests). ``use_pyzotero``: None = when installed (and no
    ``transport`` is given), True = require it, False = built-in client.
    """

    def __init__(self, config: ZoteroConfig | None = None, *, backend: str = "auto",
                 transport: httpx2.BaseTransport | None = None, use_pyzotero: bool | None = None,
                 settings: Settings | None = None) -> None:
        if backend != "auto" and backend not in BACKENDS:
            raise ValueError(f"Неизвестный способ записи в Zotero {backend!r}: auto, {', '.join(BACKENDS)}")
        self.config = config or ZoteroConfig.from_env_and_zotero_mcp(settings)
        self.backend = backend
        self._transport = transport
        self._use_pyzotero = use_pyzotero
        self._local_http: httpx2.Client | None = None
        self._web_http: httpx2.Client | None = None
        self._probe_cache: dict[str, Any] | None = None

    # -- lifecycle
    def close(self) -> None:
        for client in (self._local_http, self._web_http):
            if client is not None:
                client.close()
        self._local_http = self._web_http = None

    def __enter__(self) -> ZoteroWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def local_http(self) -> httpx2.Client:
        if self._local_http is None:
            self._local_http = httpx2.Client(transport=self._transport, timeout=self.config.timeout,
                                             trust_env=False, headers={"User-Agent": USER_AGENT})
        return self._local_http

    @property
    def web_http(self) -> httpx2.Client:
        if self._web_http is None:
            self._web_http = httpx2.Client(transport=self._transport, timeout=self.config.timeout,
                                           follow_redirects=True, headers={"User-Agent": USER_AGENT})
        return self._web_http

    # -- discovery
    def _probe(self, refresh: bool = False) -> dict[str, Any]:
        if self._probe_cache is not None and not refresh:
            return self._probe_cache
        st: dict[str, Any] = {"connector": False, "local_api": False, "local_api_disabled": False,
                              "server_id": None, "version": None, "error": None}
        base = self.config.local_url
        try:
            r = self.local_http.get(f"{base}/connector/ping", headers=_CONNECTOR_HEADERS, timeout=3.0)
            st["connector"] = r.status_code == 200
            st["version"] = r.headers.get("X-Zotero-Version")
        except httpx2.HTTPError as exc:
            st["error"] = str(exc) or type(exc).__name__
        if st["connector"]:
            try:
                r = self.local_http.get(f"{base}/api/", headers={"Zotero-API-Version": "3"}, timeout=3.0)
                if r.status_code == 200:
                    st["local_api"] = True
                    st["server_id"] = r.headers.get("Zotero-Server-ID")
                    st["version"] = r.headers.get("X-Zotero-Version") or st["version"]
                elif r.status_code == 403:
                    st["local_api_disabled"] = True
            except httpx2.HTTPError as exc:
                st["error"] = str(exc) or type(exc).__name__
        self._probe_cache = st
        return st

    def _local_allowed(self) -> bool:
        return self.config.local is not False

    def _why_not(self, backend: str) -> str | None:
        """None when ``backend`` is usable, else the reason (Russian)."""
        cfg = self.config
        if backend == "web":
            return None if cfg.web_ready else (
                "Для записи через Zotero Web API задайте ZOTERO_API_KEY и ZOTERO_LIBRARY_ID "
                "(https://www.zotero.org/settings/keys).")
        st = self._probe()
        if not st["connector"]:
            return NOT_RUNNING.format(url=cfg.local_url)
        if backend == "connector":
            return None
        if st["local_api_disabled"] or not st["local_api"]:
            return LOCAL_API_DISABLED
        if not st["server_id"]:
            return "Эта версия Zotero не поддерживает запись через локальный API (нужен Zotero 10+)."
        if not cfg.local_write:
            return "Запись через локальный API выключена (ZOTERO_LOCAL_WRITE)."
        if not cfg.local_api_key:
            return ("Нет ключа локального API Zotero: выполните `elibrary zotero authorize` (или "
                    "`zotero-mcp authorize-local`) и выберите «Всегда разрешать».")
        return None

    def available_backends(self) -> list[str]:
        """Usable backends in the order ``auto`` tries them (probes 127.0.0.1 once, cached)."""
        out = []
        for b in BACKENDS:
            if b != "web" and not self._local_allowed():
                continue
            if self._why_not(b) is None:
                out.append(b)
        return out

    def ping(self, *, refresh: bool = True) -> dict[str, Any]:
        """State of Zotero and of each backend (no secrets), with Russian hints."""
        st = self._probe(refresh=refresh)
        available = self.available_backends()
        hints = [reason for b in BACKENDS if (reason := self._why_not(b)) and b not in available]
        return {
            "running": st["connector"],
            "version": st["version"],
            "connector": st["connector"],
            "local_api": st["local_api"],
            "local_api_writable": bool(st["local_api"] and st["server_id"]),
            "local_api_key": bool(self.config.local_api_key),
            "local_key_source": self.config.local_key_source,
            "web_configured": self.config.web_ready,
            "available_backends": available,
            "backend": available[0] if available else None,
            "hints": list(dict.fromkeys(hints)),
            "config": self.config.describe(),
        }

    # -- local API key
    def authorize_local(self, app_name: str | None = None, *, save: bool = True,
                        timeout: float = 125.0) -> dict[str, Any]:
        """Ask Zotero 10+ for a local API key (blocks until the user answers the dialog in Zotero).

        Returns ``{"remember", "single_use", "server_id", "saved_to"}``; the key itself is kept in
        the config (and saved to ``key_store`` when granted with "Always Allow" and ``save``).
        """
        st = self._probe(refresh=True)
        if not st["connector"]:
            raise ZoteroUnavailable(NOT_RUNNING.format(url=self.config.local_url))
        if not st["local_api"]:
            raise ZoteroUnavailable(LOCAL_API_DISABLED)
        sid = st["server_id"]
        if not sid:
            raise ZoteroError("Эта версия Zotero не поддерживает запись через локальный API (нужен Zotero 10+).")
        app = app_name or self.config.app_name
        try:
            r = self.local_http.post(f"{self.config.local_url}/api/local/authorize", json={"appName": app},
                                     headers={"Zotero-Server-ID": sid, "Zotero-API-Version": "3"}, timeout=timeout)
        except httpx2.TimeoutException as exc:
            raise ZoteroError("Zotero не дождался ответа в окне запроса доступа: повторите и нажмите кнопку в Zotero.") from exc
        except httpx2.HTTPError as exc:
            raise ZoteroUnavailable(NOT_RUNNING.format(url=self.config.local_url)) from exc
        if r.status_code == 403:
            raise ZoteroAuthError("Запрос доступа к локальному API отклонён в Zotero.")
        if r.status_code == 429:
            raise ZoteroError("Zotero ограничивает запросы доступа (не больше 5 в минуту): подождите минуту и повторите.")
        if r.status_code == 404:
            raise ZoteroError("Эта версия Zotero не поддерживает /api/local/authorize (нужен Zotero 10+).")
        if r.status_code >= 400:
            raise ZoteroError(f"Zotero ответил {r.status_code} на запрос доступа: {r.text[:200]}")
        data = r.json()
        key = data.get("key")
        if not key:
            raise ZoteroError("Zotero не выдал ключ локального API.")
        remember = bool(data.get("remember"))
        self.config.local_api_key = str(key)
        self.config.local_server_id = sid
        self.config.local_key_source = "authorize"
        saved = self.config.save_local_key(str(key), sid, app_name=app) if (save and remember) else None
        return {"remember": remember, "single_use": not remember, "server_id": sid,
                "saved_to": str(saved) if saved else None}

    # -- API clients
    def _pyzotero_module(self) -> Any | None:
        if self._use_pyzotero is False or (self._use_pyzotero is None and self._transport is not None):
            return None
        try:
            from pyzotero import zotero as pz  # optional extra "zotero"
        except ImportError as exc:
            if self._use_pyzotero:
                raise DependencyMissing("zotero", "pyzotero") from exc
            return None
        return pz

    def _api(self, backend: str) -> _Api:
        cfg = self.config
        pz = self._pyzotero_module()
        if backend == "web":
            if pz is not None:
                return _PyzoteroApi(pz.Zotero(cfg.library_id, cfg.library_type, cfg.api_key), "web")
            return _NativeApi(self.web_http, cfg.web_url, cfg.web_prefix(), backend="web", api_key=cfg.api_key)
        prefix = cfg.local_prefix()
        sid = cfg.local_server_id or self._probe()["server_id"]
        if pz is not None:
            lib_id = "0" if cfg.library_type == "user" else cfg.library_id
            try:
                zot = pz.Zotero(lib_id, cfg.library_type, local=True, local_api_key=cfg.local_api_key, server_id=sid)
                if cfg.port != DEFAULT_PORT or cfg.host != DEFAULT_HOST:  # pyzotero hardcodes 127.0.0.1:23119
                    zot.endpoint = f"{cfg.local_url}/api"
                return _PyzoteroApi(zot, "local")
            except TypeError:  # pyzotero < 1.14: no local writes
                log.debug("pyzotero has no local write support, using the built-in client")
        return _NativeApi(self.local_http, cfg.local_url, prefix, backend="local",
                          api_key=cfg.local_api_key, server_id=sid)

    def _reader(self) -> _Api | None:
        """Keyless read-only local API client (for dedupe/key lookup with the connector)."""
        st = self._probe()
        if not st["local_api"]:
            return None
        try:
            prefix = self.config.local_prefix()
        except ZoteroError:
            return None
        return _NativeApi(self.local_http, self.config.local_url, prefix, backend="connector",
                          server_id=st["server_id"])

    # -- public operations
    def find_existing(self, item: Mapping[str, Any] | Any, *, backend: str | None = None) -> str | None:
        """Key of an item already in the library with the same DOI / EDN / eLIBRARY ID / URL, if any."""
        zitem = self._as_item(item)
        b = backend or next(iter(self.available_backends()), None)
        if b is None:
            raise ZoteroUnavailable(self._unavailable_message())
        api = self._reader() if b == "connector" else self._api(b)
        return _find_existing(api, zitem) if api is not None else None

    def add(self, items: Iterable[Mapping[str, Any] | Any], *, collection: str | None = None,
            tags: Iterable[str] | None = None, pdf_paths: Mapping[int, str | os.PathLike[str]] | None = None,
            dedupe: bool = True, backend: str | None = None, create_collection: bool = True,
            attachment_title: str = DEFAULT_ATTACHMENT_TITLE) -> list[dict[str, Any]]:
        """Save items; returns one dict per item, in input order::

            {"index", "title", "url", "key" (None when unknown), "backend",
             "status": "created" | "duplicate" | "failed", "message",
             "attachment": None | "uploaded" | "exists" | "failed" | "unsupported", "collection"}

        ``items``: Zotero item dicts (or Publication / PublicationHit objects, converted with
        :func:`elibrary_cli.exports.to_zotero_item`). ``collection``: key, name or ``A/B`` path
        (created when missing and the backend can, unless ``create_collection=False``). ``tags`` are
        added as manual tags. ``pdf_paths``: item index -> local file. ``dedupe``: skip items whose
        DOI / EDN / eLIBRARY ID / URL is already in the library, and repeats of an item given earlier
        in ``items`` (``"duplicate"`` with the first one's key). The library check needs a backend that
        can search: with the connector and Zotero's local API off it is not done, and ``message`` of
        each saved item says so.
        """
        all_items = [self._as_item(x) for x in items]
        if not all_items:
            return []
        all_paths = {int(k): Path(v).expanduser() for k, v in (pdf_paths or {}).items()}
        same_as = _batch_repeats(all_items) if dedupe else {}
        unique = [i for i in range(len(all_items)) if i not in same_as]
        for i, first in same_as.items():  # a PDF given for a repeat goes to the item that is saved
            if i in all_paths and first not in all_paths:
                all_paths[first] = all_paths[i]
        pos = {orig: j for j, orig in enumerate(unique)}
        zitems = [all_items[i] for i in unique]
        paths = {pos[i]: p for i, p in all_paths.items() if i in pos}
        extra_tags = [t.strip() for t in (tags or []) if t and t.strip()]
        explicit = backend or (self.backend if self.backend != "auto" else None)
        if explicit:
            if explicit not in BACKENDS:
                raise ValueError(f"Неизвестный способ записи в Zotero {explicit!r}: {', '.join(BACKENDS)}")
            reason = self._why_not(explicit)
            if reason:
                raise ZoteroUnavailable(reason)
            order = [explicit]
        else:
            order = self.available_backends()
            if not order:
                raise ZoteroUnavailable(self._unavailable_message())
        skipped: list[str] = []
        for i, b in enumerate(order):
            try:
                if b == "connector":
                    results = self._add_connector(zitems, collection, extra_tags, paths, dedupe, attachment_title)
                else:
                    results = self._add_api(b, zitems, collection, extra_tags, paths, dedupe, create_collection,
                                            attachment_title)
            except (ZoteroAuthError, ZoteroUnavailable) as exc:
                if explicit or i == len(order) - 1:
                    raise
                log.info("Zotero backend %s failed before writing: %s", b, exc)
                skipped.append(f"{b}: {exc}")
                continue
            results = self._with_repeats(results, unique, same_as, all_items)
            if skipped:
                note = "Использован способ «" + b + "», так как " + "; ".join(skipped)
                for r in results:
                    r["message"] = f"{r['message']}. {note}" if r.get("message") else note
            return results
        raise ZoteroUnavailable(self._unavailable_message())  # pragma: no cover - loop always returns/raises

    # -- internals
    def _with_repeats(self, results: list[dict[str, Any]], unique: list[int], same_as: Mapping[int, int],
                      items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Results of the saved items (indexes into ``unique``) plus one row per repeat, in input order."""
        out: list[dict[str, Any] | None] = [None] * len(items)
        for r in results:
            i = unique[r["index"]]
            r["index"] = i
            out[i] = r
        for i, first in same_as.items():
            f = out[first]
            if f is None:  # pragma: no cover - every saved item has a result
                continue
            if f["status"] == "failed":
                out[i] = self._result(i, items[i], f["backend"], "failed", None,
                                      f"Та же публикация, что и №{first + 1} этого запроса; её запись не удалась.",
                                      f["collection"])
            else:
                out[i] = self._result(i, items[i], f["backend"], "duplicate", f["key"],
                                      f"Та же публикация, что и №{first + 1} этого запроса: не добавлено повторно.",
                                      f["collection"])
        return [r for r in out if r is not None]

    def _unavailable_message(self) -> str:
        reasons = [self._why_not(b) for b in BACKENDS if b == "web" or self._local_allowed()]
        if not self._local_allowed():
            reasons.insert(0, "ZOTERO_LOCAL=false: локальный Zotero не используется.")
        return "Нет доступного способа записи в Zotero. " + " ".join(dict.fromkeys(r for r in reasons if r))

    @staticmethod
    def _as_item(x: Mapping[str, Any] | Any) -> dict[str, Any]:
        if isinstance(x, Mapping):
            return json.loads(json.dumps(dict(x), ensure_ascii=False, default=str))
        from elibrary_cli.exports import to_zotero_item

        return to_zotero_item(x)

    @staticmethod
    def _result(i: int, item: Mapping[str, Any], backend: str, status: str, key: str | None = None,
                message: str | None = None, collection: str | None = None) -> dict[str, Any]:
        return {"index": i, "title": item.get("title") or "", "url": item.get("url"), "key": key,
                "backend": backend, "status": status, "message": message, "attachment": None,
                "collection": collection}

    @staticmethod
    def _with_tags(item: dict[str, Any], tags: list[str]) -> dict[str, Any]:
        have = [t if isinstance(t, dict) else {"tag": str(t)} for t in item.get("tags") or []]
        have = [t for t in have if t.get("tag")]
        seen = {str(t["tag"]).casefold() for t in have}
        for t in tags:
            if t.casefold() not in seen:
                seen.add(t.casefold())
                have.append({"tag": t})
        item["tags"] = have
        return item

    def _resolve_collection(self, api: _Api, collection: str, create: bool) -> str:
        cols = api.collections()
        by_key = {str(c.get("key") or (c.get("data") or {}).get("key")): c for c in cols}
        name = collection.strip()
        if name in by_key:
            return name
        parts = _split_path(name)
        if not parts:
            raise ZoteroError("Пустое имя коллекции.")
        want = [p.casefold() for p in parts]
        matches = [k for k in by_key if [n.casefold() for n in _collection_path(k, by_key)] == want]
        if len(parts) == 1 and not matches:
            matches = [k for k in by_key if _collection_path(k, by_key)[-1:] and
                       _collection_path(k, by_key)[-1].casefold() == want[0]]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ZoteroError(f"В Zotero несколько коллекций «{name}»: укажите путь («Родитель/{parts[-1]}») или ключ.")
        if not create:
            raise ZoteroError(f"Коллекция «{name}» не найдена в Zotero.")
        parent: str | None = None
        for depth, part in enumerate(parts, start=1):
            found = [k for k in by_key if [n.casefold() for n in _collection_path(k, by_key)] == want[:depth]]
            if found:
                parent = found[0]
                continue
            new_key = api.create_collection(part, parent)
            log.info("created Zotero collection %r (%s)", part, new_key)
            by_key[new_key] = {"key": new_key, "data": {"name": part, "parentCollection": parent or False}}
            parent = new_key
        assert parent is not None
        return parent

    def _add_api(self, backend: str, items: list[dict[str, Any]], collection: str | None, tags: list[str],
                 paths: Mapping[int, Path], dedupe: bool, create_collection: bool,
                 attachment_title: str) -> list[dict[str, Any]]:
        api = self._api(backend)
        coll_key = self._resolve_collection(api, collection, create_collection) if collection else None
        results: list[dict[str, Any] | None] = [None] * len(items)
        if dedupe:
            for i, it in enumerate(items):
                key = _find_existing(api, it)
                if key:
                    results[i] = self._result(i, it, backend, "duplicate", key,
                                              "Уже есть в библиотеке Zotero, не добавлено повторно.", coll_key)
        todo = [i for i, r in enumerate(results) if r is None]
        payload = []
        for i in todo:
            it = {k: v for k, v in items[i].items() if k not in ("key", "version")}
            self._with_tags(it, tags)
            if coll_key:
                it["collections"] = list(dict.fromkeys([*(it.get("collections") or []), coll_key]))
            payload.append(it)
        created_any = False
        for start in range(0, len(todo), MAX_WRITE_ITEMS):
            chunk_idx = todo[start:start + MAX_WRITE_ITEMS]
            try:
                resp = api.create_items(payload[start:start + MAX_WRITE_ITEMS])
            except (ZoteroAuthError, ZoteroUnavailable):
                if not created_any:
                    raise
                for i in chunk_idx:
                    results[i] = self._result(i, items[i], backend, "failed", None, "Запись прервана: ключ отклонён.")
                continue
            except ZoteroError as exc:
                for i in chunk_idx:
                    results[i] = self._result(i, items[i], backend, "failed", None, str(exc), coll_key)
                continue
            success = resp.get("success") or {}
            failed = resp.get("failed") or {}
            unchanged = resp.get("unchanged") or {}
            for j, i in enumerate(chunk_idx):
                key = success.get(str(j)) or unchanged.get(str(j))
                if key:
                    created_any = True
                    results[i] = self._result(i, items[i], backend, "created", str(key), None, coll_key)
                else:
                    err = failed.get(str(j)) or {}
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    results[i] = self._result(i, items[i], backend, "failed", None,
                                              f"Zotero не принял запись: {msg or 'причина не указана'}", coll_key)
        for i, path in paths.items():
            r = results[i] if 0 <= i < len(results) else None
            if r is None or r["status"] != "created":
                continue
            if not path.is_file():
                r["attachment"] = "failed"
                r["message"] = f"Файл не найден: {path}"
                continue
            try:
                r["attachment"] = api.upload_attachment(r["key"], path.resolve(), attachment_title)
            except ZoteroError as exc:
                r["attachment"] = "failed"
                r["message"] = f"Запись создана, но файл не загружен: {exc}"
        return [r for r in results if r is not None]

    # -- connector
    def _connector_post(self, path: str, *, what: str, **kwargs: Any) -> httpx2.Response:
        headers = {**_CONNECTOR_HEADERS, **kwargs.pop("headers", {})}
        try:
            return self.local_http.post(f"{self.config.local_url}{path}", headers=headers, **kwargs)
        except httpx2.TimeoutException as exc:
            raise ZoteroError(f"Zotero не ответил вовремя ({what}).") from exc
        except httpx2.HTTPError as exc:
            raise ZoteroUnavailable(NOT_RUNNING.format(url=self.config.local_url)) from exc

    def _selected(self) -> dict[str, Any]:
        """``/connector/getSelectedCollection``: the library/collection selected in Zotero (where
        ``saveItems`` saves) and every editable library and collection (``targets``)."""
        r = self._connector_post("/connector/getSelectedCollection", json={}, what="выбор коллекции")
        if r.status_code != 200:
            raise ZoteroError(f"Zotero не сообщил список коллекций ({r.status_code}).")
        sel = r.json()
        return sel if isinstance(sel, dict) else {}

    @staticmethod
    def _selected_library(sel: Mapping[str, Any]) -> str | None:
        return f"L{sel['libraryID']}" if sel.get("libraryID") is not None else None

    def _connector_target(self, collection: str | None, sel: Mapping[str, Any],
                          reader: _Api | None) -> tuple[str, str, str | None]:
        """``("C12", "Name", "L1")``: the target for ``collection`` (or the selected collection when None)
        and the library (``L<libraryID>``) it belongs to."""
        selected_lib = self._selected_library(sel)
        if collection is None:
            if sel.get("id"):
                return f"C{sel['id']}", str(sel.get("name") or ""), selected_lib
            return selected_lib or "L1", str(sel.get("libraryName") or ""), selected_lib
        name = collection.strip()
        paths: list[tuple[str, list[str], list[str], str | None]] = []
        stack: list[str] = []
        library: str | None = None
        for t in sel.get("targets") or []:
            level = int(t.get("level") or 0)
            stack = [*stack[:level], str(t.get("name") or "")]
            tid = str(t.get("id", ""))
            if level == 0:
                library = tid
            if tid.startswith("C"):
                paths.append((tid, stack[1:], stack, library))
        if re.fullmatch(r"[LC]\d+", name):
            if name.startswith("L"):
                return name, name, name
            return name, name, next((lib for tid, _, _, lib in paths if tid == name), None)
        parts = _split_path(name)
        if _COLLECTION_KEY_RE.match(name) and reader is not None:
            with contextlib.suppress(ZoteroError):
                cols = reader.collections()
                by_key = {str(c.get("key")): c for c in cols}
                if name in by_key:
                    parts = _collection_path(name, by_key)
        want = [p.casefold() for p in parts]
        matches = [(tid, lib) for tid, rel, full, lib in paths
                   if [n.casefold() for n in rel] == want or [n.casefold() for n in full] == want]
        if not matches and len(want) == 1:
            matches = [(tid, lib) for tid, rel, _, lib in paths if rel and rel[-1].casefold() == want[0]]
        if len(matches) == 1:
            return matches[0][0], name, matches[0][1]
        if len(matches) > 1:
            raise ZoteroError(f"В Zotero несколько коллекций «{name}»: укажите путь («Родитель/{parts[-1]}»).")
        raise ZoteroError(
            f"Коллекция «{name}» не найдена в Zotero. Через коннектор нельзя создать коллекцию: создайте её "
            "в Zotero или настройте запись через локальный API (`elibrary zotero authorize`).")

    def _group_id(self, name: str, server_id: str | None) -> str | None:
        """groupID of the group library named ``name`` (keyless ``GET /api/users/0/groups``)."""
        if not name:
            return None
        api = _NativeApi(self.local_http, self.config.local_url, "/api/users/0", backend="connector",
                         server_id=server_id)
        try:
            groups = api._send("GET", "/api/users/0/groups", params={"format": "json"}, what="список групп").json()
        except (ZoteroError, ValueError):
            return None
        found = []
        for g in groups if isinstance(groups, list) else []:
            data = g.get("data") if isinstance(g.get("data"), dict) else g
            if str(data.get("name") or "") == name and (gid := g.get("id") or data.get("id")):
                found.append(str(gid))
        return found[0] if len(found) == 1 else None

    def _library_reader(self, library: str | None, sel: Mapping[str, Any]) -> tuple[_Api | None, str | None]:
        """Keyless read-only local API client for the library the connector saves into (``L<id>``), for
        dedupe and the key lookup; or ``(None, reason)``. The user library is the first library of
        ``targets`` (Zotero lists it first); a group is found by its name among the local API's groups."""
        st = self._probe()
        if not st["local_api"]:
            return None, DEDUPE_LOCAL_API_OFF
        if library is None:
            return None, "Zotero не сообщил, в какую библиотеку сохраняются записи"
        libs = [(str(t.get("id")), str(t.get("name") or "")) for t in sel.get("targets") or []
                if int(t.get("level") or 0) == 0]
        if library == (libs[0][0] if libs else library):
            prefix = "/api/users/0"
        else:
            name = dict(libs).get(library) or (str(sel.get("libraryName") or "")
                                               if library == self._selected_library(sel) else "")
            group_id = self._group_id(name, st["server_id"])
            if group_id is None:
                return None, (f"записи сохраняются в групповую библиотеку «{name or library}», которую не удалось "
                              "найти через локальный API Zotero")
            prefix = f"/api/groups/{group_id}"
        return _NativeApi(self.local_http, self.config.local_url, prefix, backend="connector",
                          server_id=st["server_id"]), None

    def _add_connector(self, items: list[dict[str, Any]], collection: str | None, tags: list[str],
                       paths: Mapping[int, Path], dedupe: bool, attachment_title: str) -> list[dict[str, Any]]:
        if self._why_not("connector"):
            raise ZoteroUnavailable(NOT_RUNNING.format(url=self.config.local_url))
        if collection or tags:
            sel = self._selected()
        else:  # only needed to know the library saveItems writes to
            try:
                sel = self._selected()
            except ZoteroError:
                sel = {}
        target = self._connector_target(collection, sel, self._reader()) if collection else None
        # saveItems writes to the library selected in Zotero; updateSession then moves the items to the
        # target (into another library too): dedupe and the key lookup must read that library
        library = target[2] if target else self._selected_library(sel)
        reader, no_reader = self._library_reader(library, sel)
        results: list[dict[str, Any] | None] = [None] * len(items)
        if dedupe and reader is not None:
            for i, it in enumerate(items):
                with contextlib.suppress(ZoteroError):
                    key = _find_existing(reader, it)
                    if key:
                        results[i] = self._result(i, it, "connector", "duplicate", key,
                                                  "Уже есть в библиотеке Zotero, не добавлено повторно.", collection)
        todo = [i for i, r in enumerate(results) if r is None]
        if not todo:
            return [r for r in results if r is not None]
        session = uuid.uuid4().hex
        payload = []
        for i in todo:
            it = {k: v for k, v in items[i].items() if k not in ("collections", "relations", "key", "version")}
            it["id"] = f"el{i}"
            payload.append(it)
        uri = str(items[todo[0]].get("url") or "https://elibrary.ru/")
        r = self._connector_post("/connector/saveItems", what="сохранение", timeout=self.config.timeout,
                                 json={"sessionID": session, "uri": uri, "items": payload})
        if r.status_code == 500 and "libraryEditable" in r.text:
            raise ZoteroError("Выбранная в Zotero библиотека доступна только для чтения: выберите «Моя библиотека».")
        if r.status_code not in (200, 201):
            raise ZoteroError(f"Zotero (коннектор) ответил {r.status_code} на сохранение: {r.text[:200]}")
        target_name = target[1] if target else None
        for i in todo:
            results[i] = self._result(i, items[i], "connector", "created", None, None, target_name)
        for i, path in paths.items():
            res = results[i] if 0 <= i < len(results) else None
            if res is None or res["status"] != "created":
                continue
            if not path.is_file():
                res["attachment"], res["message"] = "failed", f"Файл не найден: {path}"
                continue
            meta = {"sessionID": session, "parentItemID": f"el{i}", "title": attachment_title,
                    "url": str(items[i].get("url") or uri)}
            try:
                a = self._connector_post(
                    "/connector/saveAttachment", what="загрузку файла", params={"sessionID": session},
                    content=path.read_bytes(), timeout=max(self.config.timeout, 300.0),
                    headers={"Content-Type": _content_type(path), "X-Metadata": json.dumps(meta, ensure_ascii=True)})
            except ZoteroError as exc:
                res["attachment"], res["message"] = "failed", f"Запись создана, но файл не загружен: {exc}"
                continue
            if a.status_code == 201:
                res["attachment"] = "uploaded"
            elif a.status_code == 404:
                res["attachment"] = "unsupported"
                res["message"] = ("Эта версия Zotero не принимает файлы через коннектор (нужен Zotero 8+): "
                                  "запись создана без PDF.")
            else:
                res["attachment"] = "failed"
                why = "файлы библиотеки недоступны для записи" if a.status_code == 200 else a.text[:200]
                res["message"] = f"Запись создана, но файл не загружен: {why}"
        if target or tags:
            if target is None:
                target = self._connector_target(None, sel, None)
            u = self._connector_post("/connector/updateSession", what="коллекцию и теги",
                                     json={"sessionID": session, "target": target[0], "tags": tags})
            if u.status_code != 200:
                for i in todo:
                    res = results[i]
                    assert res is not None
                    res["message"] = f"Записи сохранены, но коллекция/теги не применены ({u.status_code})."
        if reader is not None:
            for i in todo:
                res = results[i]
                assert res is not None
                with contextlib.suppress(ZoteroError):
                    res["key"] = _find_existing(reader, items[i], newest_first=True)
        elif dedupe:
            note = DEDUPE_SKIPPED.format(reason=no_reader)
            for i in todo:
                res = results[i]
                assert res is not None
                res["message"] = f"{res['message'].rstrip('.')}. {note}" if res.get("message") else note
        return [r for r in results if r is not None]


__all__ = [
    "APP_NAME", "BACKENDS", "DEFAULT_ATTACHMENT_TITLE", "DEFAULT_PORT", "KEY_FILE_NAME", "MAX_WRITE_ITEMS",
    "WEB_API_URL", "ZOTERO_MCP_CONFIG_PATH", "ZoteroAuthError", "ZoteroConfig", "ZoteroError",
    "ZoteroUnavailable", "ZoteroWriter", "item_identifiers",
]
