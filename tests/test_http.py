from __future__ import annotations

import os
import time

import httpx2
import pytest

from elibrary_cli.config import Settings
from elibrary_cli.errors import (
    AccessBlocked,
    CaptchaRequired,
    ElibraryError,
    IpRestricted,
    NetworkError,
    OfflineCacheMiss,
    RateLimited,
)
from elibrary_cli.http import Gate, captcha_return_url, classify, decode_html, parse_cookie_header

from .conftest import fixture_bytes


def test_classify_captcha_fixture():
    body = fixture_bytes("captcha")
    assert classify(200, "https://elibrary.ru/page_captcha.asp?rpage=x", body) is Gate.CAPTCHA
    # even when served from another URL, the markup alone is recognised
    assert classify(200, "https://elibrary.ru/item.asp?id=1", body) is Gate.CAPTCHA


def test_classify_normal_pages_are_ok():
    for name in ["item_article_46371463", "query_results_all_types", "authors_results_petrov"]:
        assert classify(200, "https://elibrary.ru/x.asp", fixture_bytes(name)) is Gate.OK


def test_classify_gates():
    assert classify(200, "https://elibrary.ru/ip_blocked.asp", b"<html></html>") is Gate.IP_BLOCKED
    assert classify(200, "https://elibrary.ru/x", b'<div id="blockedip">x</div>') is Gate.IP_BLOCKED
    assert classify(200, "https://elibrary.ru/ip_restricted.asp?rpage=x", b"") is Gate.IP_RESTRICTED
    assert classify(403, "https://elibrary.ru/x", b"403 - Forbidden: Access is denied.") is Gate.FORBIDDEN
    assert classify(500, "https://elibrary.ru/x", b"") is Gate.SERVER_ERROR


def test_captcha_return_url():
    url = "https://elibrary.ru/page_captcha.asp?rpage=https%3A%2F%2Felibrary%2Eru%2Fitem%2Easp%3Fid%3D1"
    assert captcha_return_url(url) == "https://elibrary.ru/item.asp?id=1"


def test_decode_utf8_and_cp1251():
    assert decode_html("привет".encode(), "text/html; Charset=UTF-8") == "привет"
    assert decode_html("привет".encode("cp1251"), None) == "привет"
    assert decode_html('<meta charset="windows-1251">тест'.encode("cp1251")).endswith("тест")


def test_parse_cookie_header_variants():
    assert parse_cookie_header("SUserID=1; SCookieGUID=AB%2D1; _ym_uid=5") == {
        "SUserID": "1", "SCookieGUID": "AB%2D1", "_ym_uid": "5"}
    assert parse_cookie_header("Cookie: SUserID=1") == {"SUserID": "1"}
    assert parse_cookie_header('[{"name":"SUserID","value":"7","domain":".elibrary.ru"},'
                               '{"name":"x","value":"1","domain":".google.com"}]') == {"SUserID": "7"}
    assert parse_cookie_header('{"cookies":[{"name":"SUserID","value":"8","domain":"elibrary.ru"}]}') == {
        "SUserID": "8"}
    assert parse_cookie_header(".elibrary.ru\tTRUE\t/\tFALSE\t0\tSUserID\t9") == {"SUserID": "9"}


def test_get_is_cached(http, site):
    site.page("item.asp", "item_article_46371463")
    p1 = http.get("item.asp", {"id": "46371463"})
    p2 = http.get("item.asp", {"id": "46371463"})
    assert not p1.from_cache and p2.from_cache
    assert len(site.requests) == 1
    assert "γН2АХ" in p2.text


def test_post_sends_form_and_referer(http, site):
    site.page("authors.asp", "authors_results_petrov")
    http.post("authors.asp", [("surname", "Петров"), ("pagenum", "")], referer="authors.asp")
    req = site.requests[0]
    assert req.method == "POST"
    assert req.headers["Referer"] == "https://elibrary.ru/authors.asp"
    assert b"surname=%D0%9F%D0%B5%D1%82%D1%80%D0%BE%D0%B2" in req.content


def test_captcha_trips_breaker_and_blocks_next_request(http, site):
    site.captcha_for("item.asp")
    with pytest.raises(CaptchaRequired) as ei:
        http.get("item.asp", {"id": "1"})
    assert "page_captcha.asp" in ei.value.captcha_url
    n = len(site.requests)
    with pytest.raises(CaptchaRequired):
        http.get("item.asp", {"id": "2"})
    assert len(site.requests) == n, "breaker must stop traffic without touching the network"
    http.set_cookies("SUserID=42; SCookieGUID=G")
    assert http.status()["breaker_open"] is False


def test_ip_blocked_raises(http, site):
    site.add(lambda r: True, lambda r: httpx2.Response(
        200, content='<div id="blockedip">Из-за нарушения правил пользования сайтом eLIBRARY.RU</div>'.encode()))
    with pytest.raises(AccessBlocked):
        http.get("item.asp", {"id": "1"})


def test_hourly_budget(http, site, settings):
    settings.max_per_hour = 2
    site.page("item.asp", "ispdf_47430099")
    http.get("item.asp", {"id": "1"}, cache_ttl_hours=0)
    http.get("item.asp", {"id": "2"}, cache_ttl_hours=0)
    with pytest.raises(RateLimited):
        http.get("item.asp", {"id": "3"}, cache_ttl_hours=0)


def test_offline_mode(http, site, settings):
    site.page("item.asp", "ispdf_47430099")
    http.get("item.asp", {"id": "1"})
    settings.offline = True
    assert http.get("item.asp", {"id": "1"}).from_cache
    with pytest.raises(OfflineCacheMiss):
        http.get("item.asp", {"id": "999"})


def test_cookies_persist_between_clients(settings, site):
    from elibrary_cli.http import ElibraryHTTP

    site.add(lambda r: True, lambda r: httpx2.Response(
        200, content=b"<html>ok</html>", headers={"Set-Cookie": "SUserID=777; domain=.elibrary.ru; path=/"}))
    a = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    a.get("defaultx.asp", cache_ttl_hours=0)
    a.close()
    b = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    assert b.cookies().get("SUserID") == "777"
    b.get("item.asp", {"id": "5"}, cache_ttl_hours=0)
    assert "SUserID=777" in site.requests[-1].headers.get("cookie", "")
    b.close()


# ---------------------------------------------------------------------------- QA regressions
def test_unfollowed_redirect_to_captcha_trips_breaker_and_is_not_cached(http, site):
    """A short-URL lookup (follow_redirects=False) answered with 302 -> page_captcha.asp is a captcha,
    even with a cache TTL: it must trip the breaker and never be stored as a normal redirect."""
    site.add(lambda r: "10.1000" in str(r.url), lambda r: httpx2.Response(
        302, headers={"Location": "/page_captcha.asp?rpage=https://elibrary.ru/10.1000/xyz"}))
    with pytest.raises(CaptchaRequired) as ei:
        http.get("/10.1000/xyz", follow_redirects=False, cache_ttl_hours=24)
    assert ei.value.captcha_url == "https://elibrary.ru/page_captcha.asp?rpage=https://elibrary.ru/10.1000/xyz"
    st = http.status()
    assert st["breaker_open"] is True and st["breaker_gate"] == "captcha"
    assert st["cache"]["pages"] == 0
    assert len(site.requests) == 1  # the captcha page itself is not fetched
    with pytest.raises(CaptchaRequired):  # the breaker now stops traffic
        http.get("/10.1000/xyz", follow_redirects=False, cache_ttl_hours=24)
    assert len(site.requests) == 1


def test_unfollowed_redirect_to_ip_restricted(http, site):
    site.add(lambda r: "9781234567897" in str(r.url), lambda r: httpx2.Response(
        302, headers={"Location": "https://elibrary.ru/ip_restricted.asp?rpage=x"}))
    with pytest.raises(IpRestricted):
        http.get("/9781234567897", follow_redirects=False, cache_ttl_hours=0)
    assert http.status()["breaker_gate"] == "ip_restricted"


def test_unfollowed_normal_redirect_is_returned(http, site):
    site.add(lambda r: "FYOAKL" in str(r.url), lambda r: httpx2.Response(
        302, headers={"Location": "/item.asp?edn=fyoakl"}))
    page = http.get("/FYOAKL", follow_redirects=False, cache_ttl_hours=0)
    assert (page.status, page.location) == (302, "/item.asp?edn=fyoakl")
    assert http.status()["breaker_open"] is False


def test_non_transient_transport_error_is_network_error(http, site):
    calls: list[str] = []

    def proxy_fails(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        raise httpx2.ProxyError("proxy refused the connection")

    site.add(lambda r: True, proxy_fails)
    with pytest.raises(NetworkError) as ei:
        http.get("item.asp", {"id": "1"}, cache_ttl_hours=0)
    assert "ProxyError" in str(ei.value) and "elibrary.ru" in str(ei.value)
    assert len(calls) == 1, "a proxy error is not retried"


def test_transient_transport_error_is_retried_once(http, site, monkeypatch):
    monkeypatch.setattr("elibrary_cli.http.time.sleep", lambda s: None)
    calls: list[str] = []

    def reset(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        raise httpx2.ConnectError("connection reset by peer")

    site.add(lambda r: True, reset)
    with pytest.raises(NetworkError) as ei:
        http.get("item.asp", {"id": "1"}, cache_ttl_hours=0)
    assert str(ei.value).startswith("Нет связи с elibrary.ru (ConnectError")
    assert len(calls) == 2


def test_network_is_blocked_in_tests():
    """The autouse guard of conftest.py: a real connection attempt fails before any packet is sent."""
    import os
    import socket

    if os.environ.get("ELIBRARY_LIVE") == "1":
        pytest.skip("live mode")
    with pytest.raises(OSError, match="tests must not use the network"):
        socket.getaddrinfo("elibrary.ru", 443)
    with pytest.raises(OSError, match="tests must not use the network"):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


# ---------------------------------------------------------------------------- download size cap
def test_download_writes_file_within_the_cap(http, site, tmp_path):
    body = b"%PDF-1.4 " + b"x" * 5000
    site.add(lambda r: "download/elibrary_1_2.pdf" in str(r.url),
             lambda r: httpx2.Response(200, content=body, headers={"Content-Type": "application/pdf"}))
    path, ctype = http.download("/download/elibrary_1_2.pdf", tmp_path / "a.pdf")
    assert path.read_bytes() == body and ctype == "application/pdf"
    assert not (tmp_path / "a.pdf.part").exists()


def test_download_aborts_on_declared_size(http, site, settings, tmp_path):
    settings.max_download_mb = 1.0
    site.add(lambda r: "download/" in str(r.url), lambda r: httpx2.Response(
        200, content=b"%PDF-1.4 small", headers={"Content-Type": "application/pdf",
                                                 "Content-Length": str(5 * 1024 * 1024)}))
    with pytest.raises(ElibraryError) as info:
        http.download("/download/elibrary_1_2.pdf", tmp_path / "a.pdf")
    msg = info.value.message
    assert "слишком большой" in msg and "ELIBRARY_MAX_DOWNLOAD_MB" in msg and "5.0 МБ" in msg and "предел 1.0 МБ" in msg
    assert not list(tmp_path.glob("a.pdf*"))
    assert http.status()["requests_total"] == 1  # the request was sent and is counted


def test_download_aborts_while_streaming_and_deletes_the_part_file(http, site, tmp_path):
    chunks_sent: list[int] = []

    def stream():
        for i in range(100):
            chunks_sent.append(i)
            yield b"y" * 600

    site.add(lambda r: "download/" in str(r.url),
             lambda r: httpx2.Response(200, content=stream(), headers={"Content-Type": "application/pdf"}))
    with pytest.raises(ElibraryError) as info:
        http.download("/download/elibrary_1_2.pdf", tmp_path / "a.pdf", max_bytes=1000)
    assert "прервано" in info.value.message and "удалён" in info.value.message
    assert not list(tmp_path.glob("a.pdf*"))
    assert len(chunks_sent) < 100, "the rest of the body is not read"


def test_settings_download_cap_and_cyberleninka_opt_in(monkeypatch):
    for name in ("ELIBRARY_MAX_DOWNLOAD_MB", "ELIBRARY_CYBERLENINKA_SEARCH"):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert s.max_download_mb == 100.0 and s.max_download_bytes == 100 * 1024 * 1024
    assert s.cyberleninka_search is False
    monkeypatch.setenv("ELIBRARY_MAX_DOWNLOAD_MB", "2,5")
    monkeypatch.setenv("ELIBRARY_CYBERLENINKA_SEARCH", "1")
    s = Settings.from_env()
    assert s.max_download_mb == 2.5 and s.max_download_bytes == int(2.5 * 1024 * 1024)
    assert s.cyberleninka_search is True
    monkeypatch.setenv("ELIBRARY_MAX_DOWNLOAD_MB", "0")
    assert Settings.from_env().max_download_mb == 100.0  # nonsense falls back to the default


# ---------------------------------------------------------------------------- review regressions (core safety)
BAN_PAGE = ("<html><body><div id='blockedip'>Из-за нарушения правил пользования сайтом eLIBRARY.RU доступ "
            "заблокирован</div></body></html>").encode()


def _gate_site(site, gate: str) -> None:
    """Every request is answered by the gate page (ip_blocked.asp / ip_restricted.asp) or IIS 403."""
    def handler(r: httpx2.Request) -> httpx2.Response:
        if gate == "forbidden":
            return httpx2.Response(403, content=b"403 - Forbidden: Access is denied.")
        if f"{gate}.asp" in str(r.url):
            return httpx2.Response(200, content=BAN_PAGE if gate == "ip_blocked" else b"<html>restricted</html>",
                                   headers={"Content-Type": "text/html; charset=utf-8"})
        return httpx2.Response(302, headers={"Location": f"/{gate}.asp"})

    site.add(lambda r: True, handler)


@pytest.mark.parametrize(("gate", "exc"), [("ip_blocked", AccessBlocked), ("forbidden", AccessBlocked),
                                           ("ip_restricted", IpRestricted)])
def test_download_gate_trips_breaker_and_stops_next_download(http, site, tmp_path, gate, exc):
    """#0: a ban / 403 / ip_restricted answer to a PDF download opens the breaker like a page request."""
    _gate_site(site, gate)
    with pytest.raises(exc):
        http.download("/file_article.asp?fileid=1&filenum=1", tmp_path / "a.pdf")
    st = http.status()
    assert st["breaker_open"] is True and st["breaker_gate"] == gate
    if gate != "ip_restricted":
        assert st["breaker_until"] - time.time() > 23 * 3600  # the 24 h ban pause
    n = len(site.requests)
    with pytest.raises(RateLimited):
        http.download("/file_article.asp?fileid=2&filenum=1", tmp_path / "b.pdf")
    assert len(site.requests) == n, "the open breaker stops the next download without a request"
    assert not list(tmp_path.glob("*.part"))


def test_download_offline_sends_nothing(http, site, settings, tmp_path):
    """#2: ELIBRARY_OFFLINE / --offline is strictly no-network, downloads included."""
    site.add(lambda r: True, lambda r: httpx2.Response(200, content=b"%PDF-1.4 " + b"x" * 2000))
    settings.offline = True
    with pytest.raises(OfflineCacheMiss) as ei:
        http.download("/full_text.asp?id=47430099", tmp_path / "a.pdf")
    assert "ELIBRARY_OFFLINE" in str(ei.value)
    assert site.requests == []
    assert not (tmp_path / "a.pdf").exists()


def test_cookie_import_in_another_process_is_not_clobbered(settings, site):
    """#3: a long-lived instance (MCP server) must pick up cookies imported by another process (CLI)
    and must not write its stale jar over them."""
    from elibrary_cli import auth
    from elibrary_cli.http import ElibraryHTTP

    site.add(lambda r: True, lambda r: httpx2.Response(200, content=fixture_bytes("item_article_46371463"),
                                                       headers={"Content-Type": "text/html; charset=utf-8"}))
    server = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    cli = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    try:
        server.set_cookies("SUserID=OLD_FLAGGED; SCookieGUID=G1")
        server.get("/item.asp?id=1", cache_ttl_hours=0)  # the server process has a live httpx client
        auth.import_cookies(cli, "SUserID=NEW_VERIFIED; SCookieGUID=G2")
        server.get("/item.asp?id=2", cache_ttl_hours=0)
        sent = site.requests[-1].headers.get("cookie", "")
        assert "SUserID=NEW_VERIFIED" in sent and "OLD_FLAGGED" not in sent
        assert server.state.get("cookies") == {"SUserID": "NEW_VERIFIED", "SCookieGUID": "G2"}
    finally:
        server.close()
        cli.close()


def test_server_set_cookies_reach_other_processes_and_only_they_are_saved(settings, site):
    """#3: only Set-Cookie of the response chain is stored (merged over the saved cookies), and another
    instance rebuilds its client from it before its next request."""
    from elibrary_cli.http import ElibraryHTTP

    def handler(r: httpx2.Request) -> httpx2.Response:
        if "login_redirect" in str(r.url):
            return httpx2.Response(302, headers={"Location": "/defaultx.asp",
                                                 "Set-Cookie": "SUserID=FRESH; domain=.elibrary.ru; path=/"})
        return httpx2.Response(200, content=b"<html>ok</html>")

    site.add(lambda r: True, handler)
    a = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    b = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    try:
        a.set_cookies("SUserID=S0; SCookieGUID=G0")
        b.get("/defaultx.asp", cache_ttl_hours=0)  # the client of b is built with S0
        a.state.set("cookies", {"SUserID": "S0", "SCookieGUID": "G0", "extra": "kept"})
        a.get("/login_redirect", cache_ttl_hours=0)
        assert a.state.get("cookies") == {"SUserID": "FRESH", "SCookieGUID": "G0", "extra": "kept"}
        b.get("/item.asp?id=3", cache_ttl_hours=0)
        assert "SUserID=FRESH" in site.requests[-1].headers.get("cookie", "")
    finally:
        a.close()
        b.close()


def test_breaker_rechecked_after_throttle_sleep(settings, site, monkeypatch):
    """#13: another process trips the shared breaker while this one sleeps on its throttle slot: the
    request must not be sent."""
    from elibrary_cli.http import ElibraryHTTP

    site.add(lambda r: True, lambda r: httpx2.Response(200, content=b"<html>ok</html>"))
    settings.min_delay = 5.0
    a = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    b = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:  # while b waits, a gets the captcha
        sleeps.append(seconds)
        a._trip_breaker(Gate.CAPTCHA, "https://elibrary.ru/page_captcha.asp?rpage=x", 15)

    try:
        a.state.set(a.THROTTLE_KEY, {"last": time.time(), "stamps": []})
        monkeypatch.setattr("elibrary_cli.http.time.sleep", sleep)
        with pytest.raises(CaptchaRequired):
            b.get("/item.asp?id=2", cache_ttl_hours=0)
        assert sleeps and site.requests == []
    finally:
        a.close()
        b.close()


def test_breaker_rechecked_before_the_retry(http, site, settings, monkeypatch):
    """#13: the retry after a connect error checks the breaker again (another process may have tripped it
    during the 10 s pause)."""
    from elibrary_cli.http import ElibraryHTTP

    other = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    calls: list[str] = []

    def refuse(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        raise httpx2.ConnectError("connection refused")

    def sleep(seconds: float) -> None:
        if seconds == 10:
            other._trip_breaker(Gate.IP_BLOCKED, "https://elibrary.ru/ip_blocked.asp", 24 * 60)

    site.add(lambda r: True, refuse)
    monkeypatch.setattr("elibrary_cli.http.time.sleep", sleep)
    try:
        with pytest.raises(RateLimited):
            http.get("item.asp", {"id": "1"}, cache_ttl_hours=0)
        assert len(calls) == 1
    finally:
        other.close()


def test_ban_breaker_message_does_not_suggest_auth_check(http, site):
    """#14: the pause after a ban says what to do (support, --offline) and never points at `auth check`."""
    _gate_site(site, "ip_blocked")
    with pytest.raises(AccessBlocked):
        http.get("item.asp", {"id": "1"})
    with pytest.raises(RateLimited) as ei:
        http.get("item.asp", {"id": "2"})
    msg = str(ei.value)
    assert "support@elibrary.ru" in msg and "--offline" in msg and "ip_blocked" in msg
    assert "auth check" not in msg


def test_set_cookies_never_lifts_a_ban(http, site):
    """#14: a cookie import (even with SUserID) keeps the 24 h ban pause."""
    _gate_site(site, "ip_blocked")
    with pytest.raises(AccessBlocked):
        http.get("item.asp", {"id": "1"})
    http.set_cookies("SUserID=1; SCookieGUID=G")
    st = http.status()
    assert st["breaker_open"] is True and st["breaker_gate"] == "ip_blocked"
    n = len(site.requests)
    with pytest.raises(RateLimited):
        http.get("item.asp", {"id": "2"})
    assert len(site.requests) == n


def test_set_cookies_without_session_cookie_keeps_captcha_pause(http, site):
    """#14: junk without SUserID cannot carry a passed check, so it lifts nothing."""
    site.captcha_for("item.asp")
    with pytest.raises(CaptchaRequired):
        http.get("item.asp", {"id": "1"})
    http.set_cookies("x=1")
    assert http.status()["breaker_open"] is True
    http.set_cookies("SUserID=passed; SCookieGUID=G")
    assert http.status()["breaker_open"] is False


def test_clear_session_keeps_the_breaker(http, site):
    """A fresh anonymous session is not a way around a captcha (session rotation)."""
    site.captcha_for("item.asp")
    with pytest.raises(CaptchaRequired):
        http.get("item.asp", {"id": "1"})
    http.clear_session()
    assert http.status()["breaker_open"] is True and http.cookies() == {}


def test_read_timeout_is_not_retried(http, site, monkeypatch):
    """#15: the server got the request (heavy page): no second computation, a clear message."""
    monkeypatch.setattr("elibrary_cli.http.time.sleep", lambda s: None)
    calls: list[str] = []

    def slow(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        raise httpx2.ReadTimeout("server still computing", request=request)

    site.add(lambda r: True, slow)
    with pytest.raises(NetworkError) as ei:
        http.get("org_items.asp", {"orgsid": "18078", "pagenum": "40"}, cache_ttl_hours=0)
    assert len(calls) == 1
    assert "ReadTimeout" in str(ei.value) and "не повторяется" in str(ei.value)
    assert http.status()["requests_last_hour"] == 1


@pytest.mark.parametrize("error", [httpx2.RemoteProtocolError, httpx2.ReadError])
def test_errors_after_sending_are_not_retried(http, site, monkeypatch, error):
    monkeypatch.setattr("elibrary_cli.http.time.sleep", lambda s: None)
    calls: list[str] = []

    def broken(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        raise error("server disconnected", request=request)

    site.add(lambda r: True, broken)
    with pytest.raises(NetworkError):
        http.get("item.asp", {"id": "1"}, cache_ttl_hours=0)
    assert len(calls) == 1


@pytest.mark.parametrize("answer", ["connect_error", "server_error"])
def test_post_is_never_retried(http, site, monkeypatch, answer):
    """#15: a POST (login, logout, search forms) is sent once, whatever happens."""
    monkeypatch.setattr("elibrary_cli.http.time.sleep", lambda s: None)
    calls: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.content)
        if answer == "connect_error":
            raise httpx2.ConnectError("connection refused")
        return httpx2.Response(500, content=b"<h1>Server Error</h1>")

    site.add(lambda r: True, handler)
    with pytest.raises(NetworkError):
        http.post("start_session.asp", [("login", "u"), ("password", "p")])
    assert len(calls) == 1


def test_get_timeout_override_reaches_the_request(http, site, settings):
    """#15: heavy pages get a longer read timeout instead of a retry."""
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.extensions.get("timeout") or {}))
        return httpx2.Response(200, content=b"<html>ok</html>")

    site.add(lambda r: True, handler)
    http.get("org_items.asp", {"orgsid": "1"}, cache_ttl_hours=0, timeout=240)
    http.get("item.asp", {"id": "1"}, cache_ttl_hours=0)
    assert seen[0]["read"] == 240 and seen[0]["connect"] == 20
    assert seen[1]["read"] == settings.timeout


def test_heavy_timeout_setting(monkeypatch):
    for name in ("ELIBRARY_TIMEOUT", "ELIBRARY_HEAVY_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().heavy_timeout == 180.0
    monkeypatch.setenv("ELIBRARY_HEAVY_TIMEOUT", "300")
    assert Settings.from_env().heavy_timeout == 300.0
    monkeypatch.setenv("ELIBRARY_TIMEOUT", "400")
    assert Settings.from_env().heavy_timeout == 400.0  # never shorter than the normal timeout


@pytest.mark.skipif(os.name == "posix", reason="real POSIX: fresh files are created private, so no chmod "
                    "happens; covered by test_state_files_are_private_on_posix")
def test_private_permissions_on_posix_are_requested(settings, monkeypatch):
    """#20: on POSIX the data/cache directories become 0700 and the SQLite files 0600 (the state holds
    the session cookies). Simulated on any OS: the chmod calls are recorded."""
    from elibrary_cli import http as http_mod

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(http_mod, "_POSIX", True)
    monkeypatch.setattr(http_mod.os, "chmod", lambda p, mode: calls.append((os.fspath(p), mode)))
    h = http_mod.ElibraryHTTP(settings, transport=httpx2.MockTransport(lambda r: httpx2.Response(200)))
    try:
        got = set(calls)
        assert (str(settings.data_dir), 0o700) in got and (str(settings.cache_dir), 0o700) in got
        assert (str(settings.state_db), 0o600) in got and (str(settings.cache_db), 0o600) in got
    finally:
        h.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_state_files_are_private_on_posix(settings):
    import stat

    from elibrary_cli.http import ElibraryHTTP

    old = os.umask(0o022)
    try:
        h = ElibraryHTTP(settings, transport=httpx2.MockTransport(lambda r: httpx2.Response(200)))
        h.state.set("cookies", {"SUserID": "1"})
        for d in (settings.data_dir, settings.cache_dir):
            assert stat.S_IMODE(d.stat().st_mode) == 0o700
        for f in (settings.state_db, settings.cache_db):
            for p in (f, f.with_name(f.name + "-wal"), f.with_name(f.name + "-shm")):
                if p.exists():
                    assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
        h.close()
    finally:
        os.umask(old)


def test_redirect_loop_is_cut_short(http, site):
    """A redirect loop must not turn into a burst of unthrottled requests."""
    from elibrary_cli.errors import NetworkError

    site.add(lambda r: "loop.asp" in str(r.url), lambda r: httpx2.Response(302, headers={"Location": "/loop.asp"}))
    with pytest.raises(NetworkError):
        http.get("loop.asp", cache_ttl_hours=0)
    assert len(site.requests) <= 7


def test_single_quoted_ban_div_is_recognised():
    assert classify(200, "https://elibrary.ru/x", b"<div id='blockedip'>x</div>") is Gate.IP_BLOCKED
