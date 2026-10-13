"""Zotero writer: config resolution and the local / connector / web backends against a fake Zotero.

Every request goes to ``httpx2.MockTransport``: nothing reaches 127.0.0.1:23119, api.zotero.org or a
real Zotero library.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs

import httpx2
import pytest

from elibrary_cli import exports as ex
from elibrary_cli.config import Settings
from elibrary_cli.errors import DependencyMissing
from elibrary_cli.integrations import zotero as zt
from elibrary_cli.integrations.zotero import (
    ZoteroAuthError,
    ZoteroConfig,
    ZoteroError,
    ZoteroUnavailable,
    ZoteroWriter,
    item_identifiers,
)
from elibrary_cli.parsers.item import parse_publication
from tests.conftest import fixture_bytes

LOCAL_HOST = "127.0.0.1"
WEB_HOST = "api.zotero.org"
S3_URL = "https://zotero-storage.s3.amazonaws.com/upload"
ALLOWED_HOSTS = {LOCAL_HOST, WEB_HOST, "zotero-storage.s3.amazonaws.com"}


class FakeZotero:
    """A small in-memory Zotero: connector endpoints, local API (Zotero 10+) and Web API."""

    def __init__(self) -> None:
        self.running = True
        self.local_api = True
        self.server_id: str | None = "SRV-1"
        self.valid_keys = {"LOCALKEY"}
        self.web_key = "WEBKEY"
        self.zotero7 = False
        self.read_only = False
        self.reject_titles: set[str] = set()
        self.items: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.uploads: dict[str, str] = {}
        self.sessions: dict[str, dict] = {}
        self.requests: list[httpx2.Request] = []
        self.authorize_answer: tuple[int, dict] = (200, {"key": "NEWKEY", "remember": True})
        self.selected: int | None = None
        self.selected_library = 1  # libraryID selected in the Zotero pane (1 = My Library)
        #: group libraries: groupID -> {"libraryID", "name", "items"} (listed by /api/users/0/groups)
        self.groups: dict[int, dict] = {}
        self.list_groups = True  # False: /api/users/0/groups does not list them
        self.targets = [
            {"id": "L1", "name": "Моя библиотека", "level": 0},
            {"id": "C5", "name": "Нейросети", "level": 1},
            {"id": "C6", "name": "Статьи", "level": 2},
            {"id": "C8", "name": "Другое", "level": 1},
            {"id": "C7", "name": "Статьи", "level": 2},
        ]
        self._n = 0

    # -- helpers
    def new_key(self, prefix: str = "K") -> str:
        self._n += 1
        return f"{prefix}{self._n:07d}"[:8]

    def store(self, library_id: int) -> dict[str, dict]:
        if library_id == 1:
            return self.items
        return next(g["items"] for g in self.groups.values() if g["libraryID"] == library_id)

    def add_item(self, data: dict, key: str | None = None, library: int = 1) -> str:
        key = key or self.new_key("I")
        self.store(library)[key] = {"key": key, "version": 1, "data": {**data, "key": key}}
        return key

    def add_group(self, group_id: int, library_id: int, name: str, collections: dict[str, str]) -> None:
        """A group library in Zotero: its targets (``L<libraryID>`` + ``C<id>`` collections) are listed by
        getSelectedCollection, its items are read through /api/groups/<groupID>."""
        self.groups[group_id] = {"libraryID": library_id, "name": name, "items": {}}
        self.targets += [{"id": f"L{library_id}", "name": name, "level": 0},
                         *({"id": cid, "name": cname, "level": 1} for cid, cname in collections.items())]

    def library_of(self, target: str) -> int:
        lib = 1
        for t in self.targets:
            if t["level"] == 0:
                lib = int(t["id"][1:])
            if t["id"] == target:
                return lib
        assert target == f"C{self.selected}", f"unknown target {target}"
        return self.selected_library  # the selected collection (not in the targets of this fake)

    def add_collection(self, name: str, parent: str | None = None, key: str | None = None) -> str:
        key = key or self.new_key("C")
        self.collections[key] = {"key": key, "version": 1, "data": {"key": key, "name": name,
                                                                    "parentCollection": parent or False}}
        return key

    @staticmethod
    def json(code: int, data, headers: dict | None = None) -> httpx2.Response:
        return httpx2.Response(code, json=data, headers=headers or {})

    def _search(self, params: dict[str, list[str]], store: dict[str, dict]) -> list[dict]:
        q = (params.get("q") or [""])[0].lower()
        found = [it for it in store.values() if q in json.dumps(it["data"], ensure_ascii=False).lower()]
        if (params.get("direction") or [""])[0] == "desc":
            found = list(reversed(found))
        return found

    # -- dispatcher
    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        assert host in ALLOWED_HOSTS, f"unexpected host {host}"
        if host == LOCAL_HOST:
            if not self.running:
                raise httpx2.ConnectError("connection refused", request=request)
            assert not request.headers.get("user-agent", "").startswith("Mozilla/")
            if path.startswith("/connector/"):
                return self.connector(request, path)
            if path.startswith("/api"):
                return self.local(request, path)
        if host == WEB_HOST:
            return self.web(request, path)
        if str(request.url) == S3_URL:
            body = request.content
            assert body.startswith(b"PREFIX") and body.endswith(b"SUFFIX")
            self.files["S3"] = body[len(b"PREFIX"):-len(b"SUFFIX")]
            return httpx2.Response(201)
        return httpx2.Response(404)

    # -- connector
    def connector(self, request: httpx2.Request, path: str) -> httpx2.Response:
        if path == "/connector/ping":
            return httpx2.Response(200, text="Zotero is running", headers={"X-Zotero-Version": "10.0.6"})
        assert request.headers["X-Zotero-Connector-API-Version"] == "3"
        if path == "/connector/getSelectedCollection":
            lib_name = next(t["name"] for t in self.targets if t["id"] == f"L{self.selected_library}")
            sel = {"libraryID": self.selected_library, "libraryName": lib_name, "libraryEditable": not self.read_only,
                   "filesEditable": True, "editable": True, "targets": self.targets}
            if self.selected:
                sel.update(id=self.selected, name="Выбранная")
            else:
                sel.update(id=None, name=lib_name)
            return self.json(200, sel)
        if path == "/connector/saveItems":
            body = json.loads(request.content)
            if body["sessionID"] in self.sessions:
                return self.json(409, {"error": "SESSION_EXISTS"})
            if self.read_only:
                return self.json(500, {"libraryEditable": False})
            ids = {}
            for it in body["items"]:
                data = {k: v for k, v in it.items() if k != "id"}
                ids[it["id"]] = self.add_item(data, library=self.selected_library)
            self.sessions[body["sessionID"]] = {"payload": body, "ids": ids, "attachments": [], "update": None,
                                                "library": self.selected_library}
            return httpx2.Response(201, headers={"Content-Type": "application/json"})
        if path == "/connector/saveAttachment":
            if self.zotero7:
                return httpx2.Response(404, text="No endpoint found")
            meta = json.loads(request.headers["X-Metadata"])
            sid = parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes)
                           else request.url.query).get("sessionID", [None])[0]
            assert sid == meta["sessionID"]
            session = self.sessions.get(sid)
            if session is None:
                return self.json(400, {"error": "SESSION_NOT_FOUND"})
            session["attachments"].append((meta, request.headers["Content-Type"], request.content))
            return httpx2.Response(201)
        if path == "/connector/updateSession":
            body = json.loads(request.content)
            session = self.sessions[body["sessionID"]]
            assert body["target"][0] in "LC"
            session["update"] = body
            library = self.library_of(body["target"])
            if library != session["library"]:
                old, new = self.store(session["library"]), self.store(library)
                for key in session["ids"].values():
                    new[key] = old.pop(key)
                session["library"] = library
            return self.json(200, {})
        return httpx2.Response(404)

    # -- local API
    def _check_local_write(self, request: httpx2.Request) -> httpx2.Response | None:
        sid = request.headers.get("Zotero-Server-ID")
        if not sid:
            return httpx2.Response(428, text="Zotero-Server-ID not provided")
        if sid != self.server_id:
            return httpx2.Response(412, text="Zotero-Server-ID mismatch")
        if request.headers.get("Zotero-API-Key") not in self.valid_keys:
            return httpx2.Response(401, text="Invalid local API key")
        return None

    def local(self, request: httpx2.Request, path: str) -> httpx2.Response:
        if not self.local_api:
            return httpx2.Response(403, text="Local API is not enabled")
        headers = {"X-Zotero-Version": "10.0.6", "Zotero-API-Version": "3"}
        if self.server_id:
            headers["Zotero-Server-ID"] = self.server_id
        if path == "/api/":
            return httpx2.Response(200, text="Nothing to see here.", headers=headers)
        if path == "/api/local/authorize":
            assert request.headers.get("Zotero-Server-ID") == self.server_id
            assert json.loads(request.content) == {"appName": "elibrary-cli"}
            code, body = self.authorize_answer
            return self.json(code, body, headers)
        if request.method != "GET" and not path.startswith("/api/local/uploads/"):
            denied = self._check_local_write(request)
            if denied is not None:
                return denied
        if path == "/api/users/0/groups":
            return self.json(200, [{"id": gid, "version": 1, "data": {"id": gid, "name": g["name"]}}
                                   for gid, g in self.groups.items() if self.list_groups], headers)
        if m := re.match(r"^/api/groups/(\d+)(/.*)$", path):
            group = self.groups.get(int(m.group(1)))
            if group is None:
                return httpx2.Response(404, text="Group not found", headers=headers)
            return self.api(request, m.group(2), headers, local=True, store=group["items"])
        return self.api(request, path.removeprefix("/api").removeprefix("/users/0"), headers, local=True)

    # -- Web API
    def web(self, request: httpx2.Request, path: str) -> httpx2.Response:
        if request.headers.get("Zotero-API-Key") != self.web_key:
            return httpx2.Response(403, text="Forbidden")
        assert path.startswith("/users/123")
        return self.api(request, path.removeprefix("/users/123"), {"Zotero-API-Version": "3"}, local=False)

    # -- shared API v3
    def api(self, request: httpx2.Request, path: str, headers: dict, *, local: bool,
            store: dict[str, dict] | None = None) -> httpx2.Response:
        params = parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes) else request.url.query)
        if path == "/items" and request.method == "GET":
            return self.json(200, self._search(params, self.items if store is None else store), headers)
        if path == "/collections" and request.method == "GET":
            cols = list(self.collections.values())
            start = int((params.get("start") or ["0"])[0])
            limit = int((params.get("limit") or ["100"])[0])
            return self.json(200, cols[start:start + limit], {**headers, "Total-Results": str(len(cols))})
        if path == "/collections" and request.method == "POST":
            assert request.headers.get("Zotero-Write-Token")
            body = json.loads(request.content)
            key = self.add_collection(body[0]["name"], body[0]["parentCollection"] or None)
            return self.json(200, {"success": {"0": key}, "failed": {}, "unchanged": {}}, headers)
        if path == "/items" and request.method == "POST":
            assert request.headers.get("Zotero-Write-Token")
            body = json.loads(request.content)
            assert len(body) <= 50
            success, failed = {}, {}
            for i, it in enumerate(body):
                if it.get("title") in self.reject_titles:
                    failed[str(i)] = {"key": "", "code": 400, "message": "'foo' is not a valid field for type"}
                    continue
                success[str(i)] = self.add_item(it)
            return self.json(200, {"success": success, "successful": {}, "failed": failed, "unchanged": {}}, headers)
        if path.startswith("/items/") and path.endswith("/file") and request.method == "POST":
            key = path.split("/")[2]
            assert request.headers.get("If-None-Match") == "*"
            form = parse_qs(request.content.decode())
            if "upload" in form:
                assert self.uploads.get(form["upload"][0]) == key
                return httpx2.Response(204, headers=headers)
            content_md5 = form["md5"][0]
            if any(hashlib.md5(b).hexdigest() == content_md5 for b in self.files.values()):
                return self.json(200, {"exists": 1}, headers)
            assert int(form["mtime"][0]) > 10_000_000_000  # milliseconds
            upload_key = f"UP{key}"
            self.uploads[upload_key] = key
            self.items[key]["data"]["md5"] = content_md5
            if local:
                return self.json(200, {"url": f"http://127.0.0.1:23119/api/local/uploads/{upload_key}",
                                       "uploadKey": upload_key, "contentType": form["contentType"][0],
                                       "prefix": "", "suffix": ""}, headers)
            return self.json(200, {"url": S3_URL, "uploadKey": upload_key, "contentType": "application/pdf",
                                   "prefix": "PREFIX", "suffix": "SUFFIX"}, headers)
        if path.startswith("/local/uploads/") and request.method == "POST":
            assert request.headers.get("Zotero-Server-ID") == self.server_id
            self.files[path.rsplit("/", 1)[1]] = request.content
            return httpx2.Response(201, headers=headers)
        return httpx2.Response(404, text=f"no route {request.method} {path}")

    # -- inspection
    def posts(self, path_part: str) -> list[httpx2.Request]:
        return [r for r in self.requests if r.method == "POST" and path_part in r.url.path]


@pytest.fixture
def fake() -> FakeZotero:
    return FakeZotero()


def make_writer(fake: FakeZotero, **cfg) -> ZoteroWriter:
    config = ZoteroConfig(**{"local_api_key": "LOCALKEY", **cfg})
    return ZoteroWriter(config, transport=httpx2.MockTransport(fake))


@pytest.fixture
def items() -> list[dict]:
    article = parse_publication(fixture_bytes("item_article_46371463"))
    patent = parse_publication(fixture_bytes("item_patent_47430099"))
    return [ex.to_zotero_item(article), ex.to_zotero_item(patent)]


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    p = tmp_path / "Блохина_2021_eLIBRARY-46371463.pdf"
    p.write_bytes(b"%PDF-1.4 fake pdf bytes")
    return p


# --------------------------------------------------------------------------- configuration


def write_mcp_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "zotero-mcp" / "config.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestConfig:
    def test_zotero_mcp_client_env_and_local_api(self, tmp_path, settings):
        path = write_mcp_config(tmp_path, {
            "client_env": {"ZOTERO_LOCAL": "true", "ZOTERO_API_KEY": "cfgkey", "ZOTERO_LIBRARY_ID": "777",
                           "ZOTERO_LIBRARY_TYPE": "user"},
            "local_api": {"key": "mcp-local-key", "server_id": "SRV-1", "remember": True},
            "semantic_search": {}})
        cfg = ZoteroConfig.from_env_and_zotero_mcp(settings, environ={}, zotero_mcp_config=path)
        assert (cfg.local, cfg.api_key, cfg.library_id, cfg.library_type) == (True, "cfgkey", "777", "user")
        assert (cfg.local_api_key, cfg.local_server_id, cfg.local_key_source) == ("mcp-local-key", "SRV-1", "zotero-mcp")
        assert cfg.zotero_mcp_config == path and cfg.web_ready
        assert cfg.key_store == settings.data_dir / "zotero.json"
        d = cfg.describe()
        assert d["local_api_key"] is True and "mcp-local-key" not in json.dumps(d) and "cfgkey" not in json.dumps(d)

    def test_environment_wins_over_config_file(self, tmp_path, settings):
        path = write_mcp_config(tmp_path, {"client_env": {"ZOTERO_API_KEY": "cfgkey", "ZOTERO_LIBRARY_ID": "777",
                                                          "ZOTERO_LOCAL": "true"}})
        env = {"ZOTERO_API_KEY": "envkey", "ZOTERO_LIBRARY_ID": "42", "ZOTERO_LIBRARY_TYPE": "group",
               "ZOTERO_LOCAL": "false", "ZOTERO_LOCAL_API_KEY": "envlocal", "ZOTERO_LOCAL_SERVER_ID": "S9",
               "ZOTERO_LOCAL_WRITE": "0", "ZOTERO_LOCAL_PORT": "23120"}
        cfg = ZoteroConfig.from_env_and_zotero_mcp(settings, environ=env, zotero_mcp_config=path)
        assert (cfg.api_key, cfg.library_id, cfg.library_type, cfg.local) == ("envkey", "42", "group", False)
        assert (cfg.local_api_key, cfg.local_server_id, cfg.local_key_source) == ("envlocal", "S9", "env")
        assert cfg.local_write is False and cfg.port == 23120 and cfg.local_url == "http://127.0.0.1:23120"
        assert cfg.local_prefix() == "/api/groups/42" and cfg.web_prefix() == "/groups/42"

    def test_settings_values_and_defaults(self, tmp_path, settings):
        settings.zotero_api_key, settings.zotero_library_id = "setkey", "5"
        cfg = ZoteroConfig.from_env_and_zotero_mcp(settings, environ={}, zotero_mcp_config=tmp_path / "missing.json")
        assert (cfg.api_key, cfg.library_id, cfg.local, cfg.local_write, cfg.port) == ("setkey", "5", None, True, 23119)
        assert cfg.zotero_mcp_config is None and cfg.local_api_key is None
        assert cfg.local_prefix() == "/api/users/0" and cfg.web_prefix() == "/users/5"
        settings.zotero_local = True
        assert ZoteroConfig.from_env_and_zotero_mcp(settings, environ={}, zotero_mcp_config=tmp_path / "x").local

    def test_zotero_mcp_local_placeholder_is_not_web(self, tmp_path, settings):
        cfg = ZoteroConfig.from_env_and_zotero_mcp(
            settings, environ={"ZOTERO_API_KEY": "k", "ZOTERO_LIBRARY_ID": "0"}, zotero_mcp_config=tmp_path / "x")
        assert cfg.web_ready is False

    def test_own_key_store_wins_over_zotero_mcp(self, tmp_path, settings):
        path = write_mcp_config(tmp_path, {"local_api": {"key": "mcp-local-key", "server_id": "SRV-1"}})
        cfg = ZoteroConfig.from_env_and_zotero_mcp(settings, environ={}, zotero_mcp_config=path)
        saved = cfg.save_local_key("own-key", "SRV-2")
        assert saved == settings.data_dir / "zotero.json"
        stored = json.loads(saved.read_text(encoding="utf-8"))["local_api"]
        assert (stored["key"], stored["server_id"], stored["app_name"], stored["remember"]) == (
            "own-key", "SRV-2", "elibrary-cli", True)
        again = ZoteroConfig.from_env_and_zotero_mcp(settings, environ={}, zotero_mcp_config=path)
        assert (again.local_api_key, again.local_server_id, again.local_key_source) == ("own-key", "SRV-2",
                                                                                        "elibrary-cli")

    def test_group_without_id(self):
        with pytest.raises(ZoteroError, match="ZOTERO_LIBRARY_ID"):
            ZoteroConfig(library_type="group").local_prefix()

    def test_default_settings_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(zt, "ZOTERO_MCP_CONFIG_PATH", tmp_path / "none.json")
        monkeypatch.setenv("ZOTERO_API_KEY", "fromenv")
        monkeypatch.setenv("ELIBRARY_HOME", str(tmp_path / "home"))
        cfg = ZoteroConfig.from_env_and_zotero_mcp()
        assert cfg.api_key == "fromenv" and cfg.key_store == tmp_path / "home" / "zotero.json"


# --------------------------------------------------------------------------- discovery


class TestDiscovery:
    def test_all_backends(self, fake):
        w = make_writer(fake, api_key="WEBKEY", library_id="123")
        assert w.available_backends() == ["local", "connector", "web"]
        info = w.ping()
        assert (info["running"], info["version"], info["local_api"], info["local_api_writable"]) == (
            True, "10.0.6", True, True)
        assert info["backend"] == "local" and info["hints"] == []
        assert info["config"]["local_api_key"] is True and "LOCALKEY" not in json.dumps(info)

    def test_without_local_key(self, fake):
        w = make_writer(fake, local_api_key=None)
        assert w.available_backends() == ["connector"]
        hints = w.ping()["hints"]
        assert any("elibrary zotero authorize" in h for h in hints)
        assert any("ZOTERO_API_KEY" in h for h in hints)

    def test_not_running(self, fake):
        fake.running = False
        w = make_writer(fake)
        assert w.available_backends() == []
        info = w.ping()
        assert info["running"] is False and info["backend"] is None
        assert any("Zotero не запущен" in h for h in info["hints"])
        with pytest.raises(ZoteroUnavailable, match="Zotero не запущен"):
            w.add([{"itemType": "book", "title": "X"}])

    def test_local_api_disabled_and_old_zotero(self, fake):
        fake.local_api = False
        w = make_writer(fake)
        assert w.available_backends() == ["connector"]
        assert any("Разрешить другим" in h for h in w.ping()["hints"])
        fake.local_api, fake.server_id = True, None
        assert make_writer(fake).available_backends() == ["connector"]

    def test_zotero_local_false_and_local_write_off(self, fake):
        assert make_writer(fake, local=False, api_key="WEBKEY", library_id="123").available_backends() == ["web"]
        assert make_writer(fake, local_write=False).available_backends() == ["connector"]

    def test_bad_backend_name(self, fake):
        with pytest.raises(ValueError):
            ZoteroWriter(ZoteroConfig(), backend="ftp", transport=httpx2.MockTransport(fake))
        with pytest.raises(ValueError):
            make_writer(fake).add([{"title": "x"}], backend="ftp")


# --------------------------------------------------------------------------- local API backend


class TestLocalBackend:
    def test_create_with_tags_collection_and_pdf(self, fake, items, pdf):
        coll = fake.add_collection("eLIBRARY")
        w = make_writer(fake)
        res = w.add(items, collection="elibrary", tags=["eLIBRARY", "лимфоциты"], pdf_paths={0: pdf})
        assert [(r["status"], r["backend"], r["collection"]) for r in res] == [
            ("created", "local", coll), ("created", "local", coll)]
        assert [r["index"] for r in res] == [0, 1]
        assert res[0]["title"].startswith("Длительное поддержание") and res[0]["url"].endswith("id=46371463")
        assert res[0]["attachment"] == "uploaded" and res[1]["attachment"] is None
        post = fake.posts("/api/users/0/items")[0]
        assert post.headers["Zotero-API-Key"] == "LOCALKEY" and post.headers["Zotero-Server-ID"] == "SRV-1"
        sent = json.loads(post.content)
        assert [it["itemType"] for it in sent] == ["journalArticle", "patent"]
        assert all(it["collections"] == [coll] for it in sent)
        tags0 = [t["tag"] for t in sent[0]["tags"]]
        assert tags0.count("лимфоциты") == 1 and tags0[-1] == "eLIBRARY"
        stored = fake.items[res[0]["key"]]["data"]
        assert stored["extra"].startswith("eLIBRARY ID: 46371463\nEDN: FYOAKL")
        att = [it["data"] for it in fake.items.values() if it["data"].get("itemType") == "attachment"]
        assert len(att) == 1
        assert (att[0]["parentItem"], att[0]["linkMode"], att[0]["title"], att[0]["contentType"], att[0]["filename"]) == (
            res[0]["key"], "imported_file", "Полный текст (PDF)", "application/pdf", pdf.name)
        assert fake.files[f"UP{att[0]['key']}"] == pdf.read_bytes()
        assert len(fake.posts(f"/items/{att[0]['key']}/file")) == 2  # authorize + register

    def test_collection_path_is_created(self, fake, items):
        w = make_writer(fake)
        res = w.add(items[:1], collection="Проекты/eLIBRARY")
        cols = {c["data"]["name"]: c["data"] for c in fake.collections.values()}
        assert set(cols) == {"Проекты", "eLIBRARY"}
        assert cols["eLIBRARY"]["parentCollection"] == cols["Проекты"]["key"]
        assert res[0]["collection"] == cols["eLIBRARY"]["key"]
        # the second time the path exists: nothing new is created
        w.add([{**items[1]}], collection="проекты/elibrary")
        assert len(fake.collections) == 2

    def test_collection_rules(self, fake, items):
        a = fake.add_collection("A")
        a_child = fake.add_collection("Статьи", a)
        fake.add_collection("Статьи", fake.add_collection("B"))
        w = make_writer(fake)
        with pytest.raises(ZoteroError, match="несколько коллекций «Статьи»"):
            w.add(items[:1], collection="Статьи")
        with pytest.raises(ZoteroError, match="не найдена"):
            w.add(items[:1], collection="Нет такой", create_collection=False)
        assert fake.posts("/items") == []
        res = w.add(items[:1], collection="A/Статьи")
        assert (res[0]["collection"], res[0]["status"]) == (a_child, "created")
        assert w.add([items[1]], collection=a_child)[0]["collection"] == a_child  # a key is accepted as is
        top = fake.add_collection("Статьи")  # a top-level collection matches the bare name exactly
        assert w.add([{"itemType": "book", "title": "Z"}], collection="Статьи")[0]["collection"] == top

    def test_dedupe_by_edn_and_doi(self, fake, items):
        old = fake.add_item({"itemType": "journalArticle", "title": "Старое", "extra": "EDN: FYOAKL"})
        decoy = fake.add_item({"itemType": "journalArticle", "title": "Цитирует HMNCEQ в тексте",
                               "extra": "EDN: ZZZZZZ"})
        fake.add_item({"itemType": "attachment", "title": "pdf", "extra": "EDN: HMNCEQ"})
        w = make_writer(fake)
        res = w.add(items)
        assert (res[0]["status"], res[0]["key"]) == ("duplicate", old)
        assert "Уже есть в библиотеке" in res[0]["message"]
        assert res[1]["status"] == "created" and res[1]["key"] != decoy
        assert len(json.loads(fake.posts("/api/users/0/items")[0].content)) == 1
        doi_item = {"itemType": "journalArticle", "title": "X", "DOI": "10.1000/ABC"}
        fake.add_item({"itemType": "journalArticle", "title": "Y", "DOI": "10.1000/abc"}, key="DOIKEY01")
        assert w.add([doi_item])[0]["key"] == "DOIKEY01"
        assert w.find_existing(items[0]) == old
        assert w.add([items[0]], dedupe=False)[0]["status"] == "created"

    def test_rejected_item_and_missing_pdf(self, fake, items, tmp_path):
        fake.reject_titles = {items[1]["title"]}
        res = make_writer(fake).add(items, pdf_paths={0: tmp_path / "nope.pdf"})
        assert res[0]["status"] == "created" and res[0]["attachment"] == "failed"
        assert res[0]["message"].startswith("Файл не найден:")
        assert res[1]["status"] == "failed" and res[1]["key"] is None
        assert "Zotero не принял запись: 'foo' is not a valid field" in res[1]["message"]

    def test_existing_file_is_not_uploaded_twice(self, fake, items, pdf):
        w = make_writer(fake)
        w.add(items[:1], pdf_paths={0: pdf})
        res = w.add([items[1]], pdf_paths={0: pdf})
        assert res[0]["attachment"] == "exists"

    def test_rejected_key_falls_back_to_connector(self, fake, items):
        w = make_writer(fake, local_api_key="REVOKED")
        res = w.add(items)
        assert [r["backend"] for r in res] == ["connector", "connector"]
        assert "Использован способ «connector»" in res[0]["message"] and "401" in res[0]["message"]
        with pytest.raises(ZoteroAuthError, match="401"):
            make_writer(fake, local_api_key="REVOKED").add(items, backend="local", dedupe=False)

    def test_server_id_mismatch(self, fake, items):
        w = make_writer(fake, local_server_id="OTHER")
        with pytest.raises(ZoteroAuthError, match="Zotero-Server-ID не совпадает"):
            w.add(items[:1], backend="local")

    def test_explicit_backend_unavailable(self, fake, items):
        with pytest.raises(ZoteroUnavailable, match="ZOTERO_API_KEY"):
            make_writer(fake).add(items, backend="web")
        with pytest.raises(ZoteroUnavailable, match="authorize"):
            make_writer(fake, local_api_key=None).add(items, backend="local")

    def test_publications_are_converted(self, fake):
        pub = parse_publication(fixture_bytes("item_book_76097861"))
        res = make_writer(fake).add([pub])
        stored = fake.items[res[0]["key"]]["data"]
        assert (stored["itemType"], stored["publisher"], stored["ISBN"]) == (
            "book", "Уфимский университет науки и технологий", "978-5-7477-5904-6")
        assert make_writer(fake).add([]) == []


# --------------------------------------------------------------------------- connector backend


class TestConnectorBackend:
    def test_save_with_collection_tags_pdf_and_key_lookup(self, fake, items, pdf):
        w = make_writer(fake, local_api_key=None)
        res = w.add(items, collection="Нейросети", tags=["eLIBRARY"], pdf_paths={1: pdf})
        assert [(r["status"], r["backend"], r["collection"]) for r in res] == [
            ("created", "connector", "Нейросети"), ("created", "connector", "Нейросети")]
        (session,) = fake.sessions.values()
        payload = session["payload"]
        assert payload["uri"] == "https://elibrary.ru/item.asp?id=46371463"
        assert [it["id"] for it in payload["items"]] == ["el0", "el1"]
        assert all("collections" not in it and "relations" not in it for it in payload["items"])
        assert payload["items"][1]["patentNumber"] == "RU 2760636 C2"
        assert session["update"] == {"sessionID": payload["sessionID"], "target": "C5", "tags": ["eLIBRARY"]}
        (meta, ctype, body), = session["attachments"]
        assert (meta["parentItemID"], meta["title"], meta["url"], ctype, body) == (
            "el1", "Полный текст (PDF)", "https://elibrary.ru/item.asp?id=47430099", "application/pdf",
            pdf.read_bytes())
        assert res[1]["attachment"] == "uploaded" and res[0]["attachment"] is None
        # keys found afterwards through the keyless local API
        assert [r["key"] for r in res] == [session["ids"]["el0"], session["ids"]["el1"]]
        assert all("Zotero-API-Key" not in r.headers for r in fake.requests)

    def test_tags_only_use_selected_collection(self, fake, items):
        fake.selected = 42
        make_writer(fake, local_api_key=None).add(items[:1], tags=["t1"])
        (session,) = fake.sessions.values()
        assert session["update"]["target"] == "C42"
        fake.selected = None
        make_writer(fake, local_api_key=None).add([items[1]], tags=["t2"])
        assert list(fake.sessions.values())[1]["update"]["target"] == "L1"

    def test_no_update_session_without_collection_or_tags(self, fake, items):
        make_writer(fake, local_api_key=None).add(items[:1])
        assert next(iter(fake.sessions.values()))["update"] is None

    def test_collection_paths_and_errors(self, fake, items):
        w = make_writer(fake, local_api_key=None)
        assert w.add(items[:1], collection="Нейросети/Статьи")[0]["status"] == "created"
        assert list(fake.sessions.values())[-1]["update"]["target"] == "C6"
        with pytest.raises(ZoteroError, match="несколько коллекций «Статьи»"):
            w.add(items[:1], collection="Статьи", dedupe=False)
        with pytest.raises(ZoteroError, match="нельзя создать коллекцию"):
            w.add([items[1]], collection="Новая", dedupe=False)
        assert len(fake.sessions) == 1  # nothing saved on errors
        assert w.add([items[1]], collection="C7")[0]["status"] == "created"
        assert list(fake.sessions.values())[-1]["update"]["target"] == "C7"
        w.add([{"itemType": "book", "title": "Z"}], collection="Моя библиотека/Другое/Статьи")
        assert list(fake.sessions.values())[-1]["update"]["target"] == "C7"

    def test_collection_key_through_local_api(self, fake, items):
        root = fake.add_collection("Нейросети", key="ROOTKEY1")
        fake.add_collection("Статьи", root, key="CHILDKY1")
        make_writer(fake, local_api_key=None).add(items[:1], collection="CHILDKY1")
        assert next(iter(fake.sessions.values()))["update"]["target"] == "C6"

    def test_dedupe_via_local_read_api(self, fake, items):
        old = fake.add_item({"itemType": "patent", "title": "Старый патент", "extra": "eLIBRARY ID: 1\nEDN: HMNCEQ"})
        res = make_writer(fake, local_api_key=None).add(items)
        assert (res[1]["status"], res[1]["key"]) == ("duplicate", old)
        (session,) = fake.sessions.values()
        assert [it["id"] for it in session["payload"]["items"]] == ["el0"]
        assert make_writer(fake, local_api_key=None).add([items[1]])[0]["status"] == "duplicate"
        assert len(fake.sessions) == 1  # all duplicates: no save session at all

    def test_without_local_api(self, fake, items):
        fake.local_api = False
        res = make_writer(fake).add(items)
        assert [(r["status"], r["key"]) for r in res] == [("created", None), ("created", None)]

    def test_zotero7_attachment_unsupported(self, fake, items, pdf):
        fake.zotero7 = True
        res = make_writer(fake, local_api_key=None).add(items[:1], pdf_paths={0: pdf})
        assert res[0]["status"] == "created" and res[0]["attachment"] == "unsupported"
        assert "Zotero 8+" in res[0]["message"]

    def test_read_only_library(self, fake, items):
        fake.read_only = True
        with pytest.raises(ZoteroError, match="только для чтения"):
            make_writer(fake, local_api_key=None).add(items[:1])


# --------------------------------------------------------------------------- Web API backend


class TestWebBackend:
    def test_native_client(self, fake, items, pdf):
        fake.running = False
        for i in range(120):
            fake.add_collection(f"Коллекция {i}")
        target = fake.add_collection("eLIBRARY")  # on the second page of /collections
        w = make_writer(fake, api_key="WEBKEY", library_id="123")
        res = w.add(items, collection="eLIBRARY", tags=["web"], pdf_paths={0: pdf})
        assert [(r["status"], r["backend"], r["collection"]) for r in res] == [
            ("created", "web", target), ("created", "web", target)]
        assert res[0]["attachment"] == "uploaded"
        assert fake.files["S3"] == pdf.read_bytes()
        post = [r for r in fake.posts("/users/123/items") if r.url.path == "/users/123/items"][0]
        assert post.headers["Zotero-API-Key"] == "WEBKEY" and post.headers["Zotero-API-Version"] == "3"
        assert "Zotero-API-Key" not in [r for r in fake.requests if str(r.url) == S3_URL][0].headers
        starts = [r.url.params.get("start") for r in fake.requests if r.url.path == "/users/123/collections"]
        assert starts == ["0", "100"]

    def test_bad_key(self, fake, items):
        fake.running = False
        w = make_writer(fake, api_key="WRONG", library_id="123")
        with pytest.raises(ZoteroAuthError, match="ZOTERO_API_KEY"):
            w.add(items)

    def test_find_existing(self, fake, items):
        fake.running = False
        key = fake.add_item({"itemType": "journalArticle", "title": "A", "DOI": "10.47056/0365-9615-2021-172-7-98-101"})
        w = make_writer(fake, api_key="WEBKEY", library_id="123")
        assert w.find_existing(items[0]) == key
        assert w.find_existing(items[1]) is None


# --------------------------------------------------------------------------- pyzotero adapter


class FakePyzotero:
    instances: list[FakePyzotero] = []

    def __init__(self, library_id, library_type, api_key=None, local=False, local_api_key=None, server_id=None):
        self.args = (library_id, library_type, api_key, local, local_api_key, server_id)
        self.calls: list[tuple] = []
        FakePyzotero.instances.append(self)

    def items(self, **kw):
        self.calls.append(("items", kw))
        return []

    def collections(self):
        return "collections-iterable"

    def everything(self, what):
        self.calls.append(("everything", what))
        return [{"key": "COLL0001", "data": {"name": "eLIBRARY", "parentCollection": False}}]

    def create_collections(self, payload):
        self.calls.append(("create_collections", payload))
        return {"success": {"0": "NEWCOLL1"}}

    def create_items(self, payload):
        self.calls.append(("create_items", payload))
        return {"success": {str(i): f"PZ{i:06d}" for i in range(len(payload))}, "failed": {}}

    def upload_attachments(self, attachments, parentid=None):
        self.calls.append(("upload_attachments", attachments, parentid))
        return {"success": attachments, "failure": [], "unchanged": []}


@pytest.fixture
def fake_pyzotero(monkeypatch):
    FakePyzotero.instances = []
    pkg = types.ModuleType("pyzotero")
    mod = types.ModuleType("pyzotero.zotero")
    mod.Zotero = FakePyzotero
    pkg.zotero = mod
    monkeypatch.setitem(sys.modules, "pyzotero", pkg)
    monkeypatch.setitem(sys.modules, "pyzotero.zotero", mod)
    return FakePyzotero


class TestPyzotero:
    def test_web_through_pyzotero(self, fake, fake_pyzotero, items, pdf):
        fake.running = False
        w = ZoteroWriter(ZoteroConfig(api_key="WEBKEY", library_id="123"), transport=httpx2.MockTransport(fake),
                         use_pyzotero=True)
        res = w.add(items, collection="eLIBRARY", pdf_paths={1: pdf})
        (zot,) = fake_pyzotero.instances
        assert zot.args[:3] == ("123", "user", "WEBKEY")
        assert [r["key"] for r in res] == ["PZ000000", "PZ000001"]
        assert [r["collection"] for r in res] == ["COLL0001", "COLL0001"]
        # dedupe: EDN, DOI (the article only) and the eLIBRARY URL of each item
        assert [c[1]["q"] for c in zot.calls if c[0] == "items"] == [
            "FYOAKL", "10.47056/0365-9615-2021-172-7-98-101", "item.asp?id=46371463", "HMNCEQ", "item.asp?id=47430099"]
        created = next(c for c in zot.calls if c[0] == "create_items")[1]
        assert all(it["collections"] == ["COLL0001"] for it in created)
        upload = next(c for c in zot.calls if c[0] == "upload_attachments")
        assert upload[2] == "PZ000001" and upload[1][0]["filename"] == str(pdf.resolve())
        assert res[1]["attachment"] == "uploaded"
        assert [r for r in fake.requests if r.url.host == WEB_HOST] == []

    def test_local_through_pyzotero(self, fake, fake_pyzotero, items):
        w = ZoteroWriter(ZoteroConfig(local_api_key="LOCALKEY"), transport=httpx2.MockTransport(fake),
                         use_pyzotero=True)
        res = w.add(items[:1], backend="local", collection="Новая")
        (zot,) = fake_pyzotero.instances
        assert zot.args == ("0", "user", None, True, "LOCALKEY", "SRV-1")
        assert ("create_collections", [{"name": "Новая", "parentCollection": False}]) in zot.calls
        assert res[0]["collection"] == "NEWCOLL1" and res[0]["key"] == "PZ000000"

    def test_pyzotero_errors_are_mapped(self, fake, fake_pyzotero, items):
        class UserNotAuthorisedError(Exception):
            pass

        def boom(payload):
            raise UserNotAuthorisedError("403 Forbidden")

        fake.running = False
        w = ZoteroWriter(ZoteroConfig(api_key="K", library_id="1"), transport=httpx2.MockTransport(fake),
                         use_pyzotero=True)
        api = w._api("web")
        api.zot.create_items = boom
        with pytest.raises(ZoteroAuthError, match="ZOTERO_API_KEY"):
            api.create_items([{}])

    def test_required_but_missing(self, fake, monkeypatch, items):
        monkeypatch.setitem(sys.modules, "pyzotero", None)
        fake.running = False
        w = ZoteroWriter(ZoteroConfig(api_key="WEBKEY", library_id="123"), transport=httpx2.MockTransport(fake),
                         use_pyzotero=True)
        with pytest.raises(DependencyMissing, match="pyzotero"):
            w.add(items)

    def test_not_used_with_transport_by_default(self, fake, fake_pyzotero, items):
        make_writer(fake).add(items[:1])
        assert fake_pyzotero.instances == []


# --------------------------------------------------------------------------- local API key


class TestAuthorize:
    def test_always_allow_is_saved(self, fake, settings):
        cfg = ZoteroConfig.from_env_and_zotero_mcp(settings, environ={}, zotero_mcp_config=settings.data_dir / "x")
        w = ZoteroWriter(cfg, transport=httpx2.MockTransport(fake))
        assert w.available_backends() == ["connector"]
        out = w.authorize_local()
        assert out == {"remember": True, "single_use": False, "server_id": "SRV-1",
                       "saved_to": str(settings.data_dir / "zotero.json")}
        assert "NEWKEY" not in json.dumps(out)
        assert (cfg.local_api_key, cfg.local_server_id, cfg.local_key_source) == ("NEWKEY", "SRV-1", "authorize")
        stored = json.loads((settings.data_dir / "zotero.json").read_text(encoding="utf-8"))
        assert stored["local_api"]["key"] == "NEWKEY"
        fake.valid_keys.add("NEWKEY")
        assert w.available_backends() == ["local", "connector"]

    def test_allow_once_is_not_saved(self, fake, settings):
        fake.authorize_answer = (200, {"key": "ONCE", "remember": False})
        cfg = ZoteroConfig(key_store=settings.data_dir / "zotero.json")
        out = ZoteroWriter(cfg, transport=httpx2.MockTransport(fake)).authorize_local()
        assert out["single_use"] is True and out["saved_to"] is None
        assert cfg.local_api_key == "ONCE" and not (settings.data_dir / "zotero.json").exists()

    @pytest.mark.parametrize(("code", "exc", "text"), [
        (403, ZoteroAuthError, "отклонён"), (429, ZoteroError, "5 в минуту"), (404, ZoteroError, "Zotero 10+")])
    def test_errors(self, fake, code, exc, text):
        fake.authorize_answer = (code, {})
        with pytest.raises(exc, match=text):
            make_writer(fake).authorize_local()

    def test_preconditions(self, fake):
        fake.running = False
        with pytest.raises(ZoteroUnavailable, match="не запущен"):
            make_writer(fake).authorize_local()
        fake.running, fake.local_api = True, False
        with pytest.raises(ZoteroUnavailable, match="Локальный API Zotero выключен"):
            make_writer(fake).authorize_local()


# --------------------------------------------------------------------------- helpers


def test_item_identifiers():
    item = {"itemType": "journalArticle", "DOI": "10.1/ABC", "url": "http://www.elibrary.ru/item.asp?id=5/",
            "extra": "eLIBRARY ID: 5\nEDN: abcdef\nDOI: ignored"}
    assert item_identifiers(item) == {"doi": "10.1/abc", "edn": "ABCDEF", "elibrary_id": "5",
                                      "url": "elibrary.ru/item.asp?id=5"}
    assert item_identifiers({"data": {"extra": "DOI: 10.2/X"}}) == {"doi": "10.2/x"}


def test_context_manager_closes_clients(fake):
    with make_writer(fake) as w:
        w.ping()
        assert w._local_http is not None
    assert w._local_http is None


def test_no_request_left_the_fake(fake, items, pdf):
    w = make_writer(fake, api_key="WEBKEY", library_id="123")
    w.add(items, pdf_paths={0: pdf})
    assert {r.url.host for r in fake.requests} <= ALLOWED_HOSTS
    assert isinstance(Settings(), Settings)


# --------------------------------------------------------------------------- dedupe edge cases


def card_item(name: str) -> dict:
    return ex.to_zotero_item(parse_publication(fixture_bytes(name)))


def gets(fake: FakeZotero) -> list[tuple[str, str | None]]:
    return [(r.url.path, r.url.params.get("q")) for r in fake.requests if r.method == "GET" and "/api/" in r.url.path
            and r.url.path != "/api/"]


class TestDedupeByElibraryUrl:
    """Items saved earlier with Zotero's own eLibrary.ru translator (the browser button) have the page URL
    and the RSCI count, no EDN, and often no DOI."""

    TRANSLATOR_ITEM = {"itemType": "thesis", "title": "Структурно-параметрический синтез", "libraryCatalog": "eLibrary.ru",
                       "url": "https://www.elibrary.ru/item.asp?id=30339972", "extra": "Цитируемость в РИНЦ: 0"}

    @pytest.mark.parametrize("backend", ["local", "connector"])
    def test_found_by_item_url(self, fake, backend):
        old = fake.add_item(dict(self.TRANSLATOR_ITEM))
        w = make_writer(fake)
        item = card_item("item_disser_30339972")
        assert item["extra"].split("\n")[1] == "EDN: ZNYEDZ" and "DOI" not in item
        assert w.find_existing(item, backend=backend) == old
        res = w.add([item], backend=backend)
        assert [(r["status"], r["key"]) for r in res] == [("duplicate", old)]
        assert len(fake.items) == 1 and fake.posts("/connector/saveItems") == [] and fake.posts("/api/users/0/items") == []
        assert ("/api/users/0/items", "item.asp?id=30339972") in gets(fake)

    def test_found_by_edn_url(self, fake):
        old = fake.add_item({**self.TRANSLATOR_ITEM, "url": "https://elibrary.ru/item.asp?edn=znyedz"})
        res = make_writer(fake).add([card_item("item_disser_30339972")])
        assert [(r["status"], r["key"]) for r in res] == [("duplicate", old)]

    def test_identifiers_from_elibrary_url(self):
        assert item_identifiers({"url": "https://www.elibrary.ru/item.asp?id=30339972"}) == {
            "url": "elibrary.ru/item.asp?id=30339972", "elibrary_id": "30339972"}
        assert item_identifiers({"url": "https://elibrary.ru/item.asp?edn=znyedz&x=1"})["edn"] == "ZNYEDZ"
        assert "elibrary_id" not in item_identifiers({"url": "https://example.org/item.asp?id=5"})
        # the extra wins over the URL
        assert item_identifiers({"url": "https://elibrary.ru/item.asp?id=1", "extra": "eLIBRARY ID: 2"})[
            "elibrary_id"] == "2"

    def test_other_item_with_similar_url_is_not_a_duplicate(self, fake):
        fake.add_item({**self.TRANSLATOR_ITEM, "url": "https://elibrary.ru/item.asp?id=303399721"})
        assert make_writer(fake).add([card_item("item_disser_30339972")])[0]["status"] == "created"


class TestRepeatsInOneBatch:
    @pytest.mark.parametrize("backend", ["local", "connector"])
    def test_same_publication_twice(self, fake, backend, pdf):
        article, patent = card_item("item_article_46371463"), card_item("item_patent_47430099")
        by_edn = {**article, "url": "https://elibrary.ru/item.asp?edn=FYOAKL"}  # same card, found by its EDN
        res = make_writer(fake).add([article, patent, dict(article), by_edn], backend=backend, pdf_paths={2: pdf})
        assert [r["index"] for r in res] == [0, 1, 2, 3]
        assert [r["status"] for r in res] == ["created", "created", "duplicate", "duplicate"]
        assert res[0]["key"] and res[0]["key"] != res[1]["key"]
        assert res[2]["key"] == res[3]["key"] == res[0]["key"]
        assert all("№1 этого запроса" in r["message"] for r in res[2:])
        assert len([it for it in fake.items.values() if it["data"]["itemType"] != "attachment"]) == 2
        assert res[0]["attachment"] == "uploaded"  # the PDF given for the repeat goes to the saved item

    def test_failed_first_copy(self, fake, items):
        fake.reject_titles = {items[0]["title"]}
        res = make_writer(fake).add([items[0], dict(items[0])])
        assert [r["status"] for r in res] == ["failed", "failed"] and res[1]["key"] is None
        assert "запись не удалась" in res[1]["message"]

    def test_no_dedupe_keeps_repeats(self, fake, items):
        res = make_writer(fake).add([items[0], dict(items[0])], dedupe=False)
        assert [r["status"] for r in res] == ["created", "created"] and len(fake.items) == 2

    def test_items_without_identifiers_are_not_repeats(self, fake):
        res = make_writer(fake).add([{"itemType": "book", "title": "Z"}, {"itemType": "book", "title": "Z"}])
        assert [r["status"] for r in res] == ["created", "created"]


class TestConnectorDedupeReporting:
    def test_local_api_off_is_reported_on_every_saved_item(self, fake, items, pdf):
        fake.local_api = False  # the Zotero default: "Allow other applications..." unchecked
        w = make_writer(fake, local_api_key=None)
        for _ in range(2):
            res = w.add(items, pdf_paths={0: pdf})
            assert [(r["status"], r["backend"], r["key"]) for r in res] == [("created", "connector", None)] * 2
            assert all(r["message"] and "Проверка на дубликаты не выполнена" in r["message"]
                       and "Разрешить другим приложениям" in r["message"] for r in res)
        assert len(fake.sessions) == 2
        assert all("message" not in r or r["message"] is None for r in w.add(items, dedupe=False))

    def test_dedupe_done_has_no_note(self, fake, items):
        res = make_writer(fake, local_api_key=None).add(items)
        assert [r["message"] for r in res] == [None, None]

    def test_mcp_tool_is_not_idempotent(self):
        import anyio

        from elibrary_cli import mcp_server

        tool = next(t for t in anyio.run(mcp_server.mcp.list_tools) if t.name == "elibrary_zotero_add")
        assert tool.annotations.idempotent_hint is False
        assert "duplicate check" in tool.description


GROUP = (77, 3, "Лаборатория (группа)")


class TestConnectorGroupLibrary:
    @pytest.fixture
    def group_fake(self, fake) -> FakeZotero:
        fake.add_group(*GROUP, {"C9": "Входящие"})
        return fake

    def test_target_in_group_is_deduped_against_the_group(self, group_fake, items):
        fake = group_fake
        in_my_library = fake.add_item(items[0])  # the same article, but in My Library
        w = make_writer(fake, local_api_key=None)
        res = w.add(items[:1], collection="Лаборатория (группа)/Входящие")
        (session,) = fake.sessions.values()
        group_items = fake.groups[77]["items"]
        assert [(r["status"], r["collection"]) for r in res] == [("created", "Лаборатория (группа)/Входящие")]
        assert session["update"]["target"] == "C9" and list(group_items) == [session["ids"]["el0"]]
        assert res[0]["key"] == session["ids"]["el0"] != in_my_library and res[0]["message"] is None
        reads = {path for path, _ in gets(fake)}
        assert reads == {"/api/users/0/groups", "/api/groups/77/items"}  # never My Library
        # now it is in the group: a duplicate there, with the group's key
        res = make_writer(fake, local_api_key=None).add(items[:1], collection="Входящие")
        assert [(r["status"], r["key"]) for r in res] == [("duplicate", session["ids"]["el0"])]
        assert len(fake.sessions) == 1

    def test_group_selected_in_zotero(self, group_fake, items):
        fake = group_fake
        old = fake.add_item(items[1], library=3)
        fake.add_item(items[0])  # My Library
        fake.selected_library = 3
        res = make_writer(fake, local_api_key=None).add(items)
        assert [(r["status"], r["key"]) for r in res][1] == ("duplicate", old)
        assert res[0]["status"] == "created"  # in My Library only: not a duplicate for the group
        (session,) = fake.sessions.values()
        assert session["library"] == 3 and res[0]["key"] == session["ids"]["el0"]

    def test_my_library_target_ignores_group_items(self, group_fake, items):
        group_fake.add_item(items[0], library=3)
        group_fake.selected_library = 3
        res = make_writer(group_fake, local_api_key=None).add(items[:1], collection="Нейросети")
        assert res[0]["status"] == "created" and res[0]["key"] == group_fake.sessions[
            next(iter(group_fake.sessions))]["ids"]["el0"]
        assert group_fake.store(1)[res[0]["key"]]  # moved to My Library by updateSession

    def test_unknown_group_skips_dedupe_and_says_so(self, group_fake, items):
        fake = group_fake
        fake.list_groups = False  # the group is not found through the local API
        fake.add_item(items[0])  # in My Library: must not make the group save a "duplicate"
        res = make_writer(fake, local_api_key=None).add(items[:1], collection="Лаборатория (группа)/Входящие")
        assert (res[0]["status"], res[0]["key"]) == ("created", None)
        assert "групповую библиотеку «Лаборатория (группа)»" in res[0]["message"]
        assert "Проверка на дубликаты не выполнена" in res[0]["message"]
        assert len(fake.posts("/connector/saveItems")) == 1
        assert "/api/users/0/items" not in {path for path, _ in gets(fake)}
