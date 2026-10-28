"""Shared test fixtures: saved elibrary.ru pages and a fake transport serving them."""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from pathlib import Path

# Typer forces rich "terminal" output (ANSI escapes) when GITHUB_ACTIONS / FORCE_COLOR / PY_COLORS is
# set, and it decides once, at import time. Tests compare plain help text, so switch that off before
# any test module imports the CLI. Tests compare plain text, so colour forcing is removed too.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ.setdefault("NO_COLOR", "1")
for _var in ("FORCE_COLOR", "PY_COLORS"):  # rich honours these at run time
    os.environ.pop(_var, None)

import httpx2  # noqa: E402
import pytest  # noqa: E402

from elibrary_cli.config import Settings  # noqa: E402
from elibrary_cli.http import ElibraryHTTP  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def fixture_bytes(name: str) -> bytes:
    """Raw bytes of ``tests/fixtures/html/<name>.html``."""
    return (FIXTURES / f"{name}.html").read_bytes()


_LOOPBACK = ("127.", "::1", "localhost")


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    """Tests never touch the network: elibrary.ru shows a captcha after ~10 page views, and every
    test must run offline. Any non-loopback connection or DNS lookup fails (``ELIBRARY_LIVE=1``
    lifts the guard for the maintainer's manual live checks)."""
    if os.environ.get("ELIBRARY_LIVE") == "1":
        yield
        return

    def guard(host: object) -> None:
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if not (isinstance(host, str) and host.startswith(_LOOPBACK)):
            raise OSError(f"tests must not use the network (blocked: {host!r})")

    orig_connect, orig_connect_ex = socket.socket.connect, socket.socket.connect_ex
    orig_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: object) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            guard(address[0] if isinstance(address, tuple) else address)
        return orig_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            guard(address[0] if isinstance(address, tuple) else address)
        return orig_connect_ex(self, address)

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> list:
        if host is not None:
            guard(host)
        return orig_getaddrinfo(host, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        mp.setattr(socket, "getaddrinfo", getaddrinfo)
        yield


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.cache_dir = tmp_path / "cache"
    s.min_delay = 0.0
    s.jitter = 0.0
    s.timeout = 5.0
    return s


Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeSite:
    """Routes requests to fixture files; records every request it receives."""

    def __init__(self) -> None:
        self.routes: list[tuple[Callable[[httpx2.Request], bool], Handler]] = []
        self.requests: list[httpx2.Request] = []

    def add(self, predicate: Callable[[httpx2.Request], bool], handler: Handler) -> FakeSite:
        self.routes.append((predicate, handler))
        return self

    def page(self, path_contains: str, fixture: str, *, status: int = 200) -> FakeSite:
        body = fixture_bytes(fixture)
        return self.add(
            lambda r, p=path_contains: p in str(r.url),
            lambda r, b=body, s=status: httpx2.Response(
                s, content=b, headers={"Content-Type": "text/html; Charset=UTF-8"}),
        )

    def captcha_for(self, path_contains: str) -> FakeSite:
        """Redirect matching requests to page_captcha.asp like the real site does."""
        body = fixture_bytes("captcha")

        def handler(r: httpx2.Request) -> httpx2.Response:
            if "page_captcha.asp" in str(r.url):
                return httpx2.Response(200, content=body, headers={"Content-Type": "text/html; Charset=UTF-8"})
            return httpx2.Response(302, headers={"Location": "/page_captcha.asp?rpage=" + str(r.url)})

        return self.add(lambda r, p=path_contains: p in str(r.url) or "page_captcha.asp" in str(r.url), handler)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        for pred, handler in self.routes:
            if pred(request):
                return handler(request)
        return httpx2.Response(404, content=b"not found")


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


@pytest.fixture
def http(settings: Settings, site: FakeSite):
    client = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    yield client
    client.close()
