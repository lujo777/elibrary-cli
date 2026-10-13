from __future__ import annotations

import re
import sqlite3
import sys
from urllib.parse import parse_qsl, quote, quote_plus, urlencode

import httpx2
import pytest

from elibrary_cli import auth
from elibrary_cli.auth import (
    ANONYMOUS_USER,
    LOGIN_FIRST,
    LOGIN_FORM_MARKER,
    NOT_AUTHORIZED,
    SessionInfo,
    browser_session,
    build_login_form,
    check_session,
    import_cookies,
    login,
    login_failure_reason,
    login_form_fields,
    login_from_env,
    logout,
    parse_session_info,
)
from elibrary_cli.errors import CaptchaRequired, DependencyMissing, ElibraryError, IpRestricted, LoginRequired

from .conftest import FIXTURES, fixture_bytes

HTML = {"Content-Type": "text/html; Charset=UTF-8"}
ANON_FONT = "<font color=#00008f>Незарегистрированный пользователь</font>"
USER_LINK = ('<font color=#00008f><a href="/author_info.asp?isold=1" title="Персональная карточка '
             'пользователя">ivanov_test&nbsp;</a></font>')
SECRET = "s3cr#t pa&ss"
ALL_FIXTURES = sorted(p.stem for p in FIXTURES.glob("*.html"))


def _drop_login_form(text: str) -> str:
    """Signed-in pages have no login form (see the 2025 logged-in snapshot in docs/re/auth.md)."""
    return re.sub(r'<FORM[^>]*name="login".*?</FORM>', "", text, count=1, flags=re.S | re.I)


def logged_in_page(fixture: str = "item_article_46371463", *, org: str | None = None) -> bytes:
    """A signed-in variant of a saved anonymous page (the session box shows an account name)."""
    text = fixture_bytes(fixture).decode("utf-8")
    assert ANON_FONT in text
    text = _drop_login_form(text.replace(ANON_FONT, USER_LINK, 1))
    if org:
        # old layout: the organization row of the session box
        text = text.replace("<font color=#00008f>не определена</font>", f"<font color=#00008f>{org}</font>")
    return text.encode("utf-8")


def with_message(fixture: str, message_html: str) -> bytes:
    """Append a message to the end of a saved page (the fixtures end with ``</html>``, no ``</body>``)."""
    text = fixture_bytes(fixture).decode("utf-8")
    end = text.lower().rfind("</html>")
    assert end > 0
    return (text[:end] + message_html + text[end:]).encode("utf-8")


def respond(body: bytes, status: int = 200, headers: dict | None = None):
    return lambda r: httpx2.Response(status, content=body, headers={**HTML, **(headers or {})})


def path_is(path: str):
    return lambda r: r.url.path == path


@pytest.fixture(autouse=True)
def _fresh_env_login():
    auth._reset_env_login_attempt()
    yield
    auth._reset_env_login_attempt()


def _state_dump(http) -> str:
    db = sqlite3.connect(str(http.settings.state_db))
    try:
        return "\n".join(f"{k}={v}" for k, v in db.execute("SELECT key, value FROM kv"))
    finally:
        db.close()


# ---------------------------------------------------------------------------- parse_session_info
def test_parse_anonymous_item_page():
    info = parse_session_info(fixture_bytes("item_article_46371463"), url="https://elibrary.ru/item.asp?id=46371463")
    assert info.state == "anonymous"
    assert info.logged_in is False
    assert info.user_name is None
    assert info.ip_address == "192.0.2.1"  # from the login box (new layout)
    assert info.organization is None  # "не определена"
    assert info.session_started == "13.10.2026 14:43"
    assert info.session_duration == "00:02"
    assert info.login_form_present is True
    assert info.anonymous_markers_found == [ANONYMOUS_USER, LOGIN_FORM_MARKER]
    assert info.raw_text.splitlines()[:6] == [
        "Имя пользователя:", "Незарегистрированный пользователь", "Начало работы:", "13.10.2026 14:43",
        "Время работы:", "00:02"]
    assert "Закрыть сессию" in info.raw_text
    assert info.extra == {}
    assert info.user_card_url is None
    assert info.url == "https://elibrary.ru/item.asp?id=46371463"


@pytest.mark.parametrize(("fixture", "duration"), [
    ("authors_form", "00:10"), ("querybox_form", "00:10"), ("titles_form", "00:10"),
    ("author_items_3771", "00:17"), ("contents_8610", "00:19"), ("orgs_results_mgu", "00:17"),
])
def test_parse_anonymous_forms(fixture, duration):
    info = parse_session_info(fixture_bytes(fixture))
    assert info.state == "anonymous" and not info.logged_in
    assert info.user_name is None
    assert info.session_started == "13.10.2026 14:43"
    assert info.session_duration == duration
    assert info.ip_address == "192.0.2.1"
    assert ANONYMOUS_USER in info.anonymous_markers_found
    assert LOGIN_FORM_MARKER in info.anonymous_markers_found
    assert info.login_form_present


def test_parse_old_layout_session_box_has_ip_and_org():
    info = parse_session_info(fixture_bytes("querybox_form"))
    lines = info.raw_text.splitlines()
    # the old layout (querybox/orgs/query_results) repeats IP and organization inside #win_session
    assert lines[:6] == ["Имя пользователя:", "Незарегистрированный пользователь", "IP-адрес компьютера:",
                         "192.0.2.1", "Название организации:", "не определена"]
    assert info.organization is None
    assert info.extra == {}


def test_parse_captcha_page():
    info = parse_session_info(fixture_bytes("captcha"), url="https://elibrary.ru/page_captcha.asp?rpage=x")
    assert info.state == "captcha"
    assert info.logged_in is False
    assert info.login_form_present is False


def test_parse_fragment_is_unknown():
    info = parse_session_info(fixture_bytes("ispdf_47430099"))
    assert info.state == "unknown"
    assert info.logged_in is False
    assert info.anonymous_markers_found == []
    assert info.raw_text == ""


def test_parse_logged_in_new_layout():
    info = parse_session_info(logged_in_page(), url="https://elibrary.ru/item.asp?id=46371463")
    assert info.state == "logged_in"
    assert info.logged_in is True
    assert info.user_name == "ivanov_test"  # &nbsp; after the name is stripped
    assert info.user_card_url == "https://elibrary.ru/author_info.asp?isold=1"
    assert info.login_form_present is False
    assert info.anonymous_markers_found == []
    assert info.session_started == "13.10.2026 14:43"
    assert info.ip_address is None  # the new layout shows the IP only in the (now absent) login box
    assert info.raw_text.splitlines()[:2] == ["Имя пользователя:", "ivanov_test"]


def test_parse_logged_in_old_layout_with_organization():
    info = parse_session_info(logged_in_page("querybox_form", org="Тестовый государственный университет"))
    assert info.logged_in and info.user_name == "ivanov_test"
    assert info.ip_address == "192.0.2.1"
    assert info.organization == "Тестовый государственный университет"
    assert info.session_duration == "00:10"


def test_parse_page_level_markers_and_ip_restricted():
    page = with_message("titles_form", "<p>Вы не авторизованы. Для доступа к этой странице необходимо вначале "
                                        "войти в библиотеку.</p>")
    info = parse_session_info(page, url="https://elibrary.ru/ip_restricted.asp?rpage=x")
    assert info.state == "anonymous"
    assert info.anonymous_markers_found == [ANONYMOUS_USER, NOT_AUTHORIZED, LOGIN_FIRST, LOGIN_FORM_MARKER,
                                            "ip_restricted.asp"]


def test_parse_page_without_boxes_but_with_markers():
    info = parse_session_info("<html><body><p>Вы не авторизованы</p></body></html>")
    assert info.state == "anonymous" and info.anonymous_markers_found == [NOT_AUTHORIZED]


def test_parse_unknown_label_goes_to_extra():
    text = fixture_bytes("item_article_46371463").decode("utf-8")
    extra_rows = ('<tr><td>&nbsp;<font color=#000000>Подписка:</font></td></tr>'
                  '<tr><td><font color=#00008f>до 31.12.2026</font></td></tr>')
    # insert the rows before the "Время работы:" row of the session box
    text, n = re.subn(r"<tr>(?:(?!<tr>).)*?Время работы:", lambda m: extra_rows + m.group(0), text, count=1,
                      flags=re.S)
    assert n == 1
    info = parse_session_info(text)
    assert info.extra == {"Подписка": "до 31.12.2026"}
    assert info.session_started == "13.10.2026 14:43"


def test_parse_accepts_str_and_bytes_equally():
    raw = fixture_bytes("authors_form")
    assert parse_session_info(raw) == parse_session_info(raw.decode("utf-8"))


# ---------------------------------------------------------------------------- login form
@pytest.mark.parametrize(("fixture", "rpage"), [
    ("item_article_46371463", "https://elibrary.ru/item.asp?id=46371463"),
    ("authors_form", "https://elibrary.ru/authors.asp"),
    ("querybox_form", "https://elibrary.ru/querybox.asp"),
    ("titles_form", "https://elibrary.ru/titles.asp"),
])
def test_login_form_fields_from_fixtures(fixture, rpage):
    fields = login_form_fields(fixture_bytes(fixture))
    assert fields == [("rpage", "hidden", rpage), ("login", "text", ""), ("password", "password", ""),
                      ("knowme", "checkbox", "")]
    # build_login_form sends exactly these names in this order
    assert [n for n, _ in build_login_form("u", "p", rpage=rpage)] == [n for n, _, _ in fields]


def test_logout_form_fields_from_fixture():
    assert login_form_fields(fixture_bytes("item_article_46371463"), form_name="session") == [
        ("saveuser", "hidden", "1")]
    assert login_form_fields(fixture_bytes("captcha")) == []


def test_build_login_form_exact():
    assert build_login_form(" user@example.org ", SECRET) == [
        ("rpage", "https://elibrary.ru/defaultx.asp"),
        ("login", "user@example.org"),
        ("password", SECRET),  # passwords are never stripped
        ("knowme", "on"),
    ]
    assert build_login_form("u", "p", remember=False, rpage="https://elibrary.ru/querybox.asp") == [
        ("rpage", "https://elibrary.ru/querybox.asp"), ("login", "u"), ("password", "p")]


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_login_failure_reason_none_on_normal_pages(fixture):
    # the pages' own JS alerts ("Не указан пароль!") and "Забыли пароль?" must not count as failures
    assert login_failure_reason(fixture_bytes(fixture)) is None


@pytest.mark.parametrize("message", [
    "Неверное имя пользователя или пароль",
    "Ошибка: неправильно указан пароль",
    "Пользователь с таким именем не найден",
])
def test_login_failure_reason_detects_messages(message):
    page = with_message("item_article_46371463", f'<div class="redref"><b>{message}</b></div>')
    assert login_failure_reason(page) == message


def test_login_failure_reason_in_alert_and_adjacent_line():
    page = with_message("titles_form", '<script>alert("Неверный пароль или имя пользователя!")</script>')
    assert login_failure_reason(page) == "Неверный пароль или имя пользователя!"
    page = with_message("titles_form", "<div>Ошибка</div><div>Проверьте пароль</div>")
    assert login_failure_reason(page) == "Ошибка"


# ---------------------------------------------------------------------------- login over the fake site
def test_login_success_single_post(http, site):
    site.add(path_is("/start_session.asp"), respond(logged_in_page()))
    info = login(http, "user@example.org", SECRET)
    assert info.logged_in and info.user_name == "ivanov_test"
    assert info.outcome == "logged_in"
    assert info.message == "Вход выполнен: ivanov_test"
    assert len(site.requests) == 1, "login must be one POST without a preliminary GET"
    req = site.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://elibrary.ru/start_session.asp"
    assert req.headers["Referer"] == "https://elibrary.ru/defaultx.asp"
    assert req.headers["Origin"] == "https://elibrary.ru"
    assert req.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qsl(req.content.decode("ascii"), keep_blank_values=True) == [
        ("rpage", "https://elibrary.ru/defaultx.asp"), ("login", "user@example.org"),
        ("password", SECRET), ("knowme", "on")]
    assert http.state.get("account") == "ivanov_test"
    assert http.status()["account"] == "ivanov_test"
    # the password is never cached or stored
    assert http.cache.stats()["pages"] == 0
    assert SECRET not in _state_dump(http)
    assert "s3cr" not in _state_dump(http)


def test_login_follows_redirect_and_keeps_cookies(http, site):
    site.add(path_is("/start_session.asp"), lambda r: httpx2.Response(
        302, headers={"Location": "/defaultx.asp", "Set-Cookie": "SUserID=900; domain=.elibrary.ru; path=/"}))
    site.add(path_is("/defaultx.asp"), respond(logged_in_page("titles_form")))
    http.state.set("query.current", {"q": "old"})
    info = login(http, "user", SECRET, remember=False)
    assert info.logged_in and info.url == "https://elibrary.ru/defaultx.asp"
    assert [r.method for r in site.requests] == ["POST", "GET"]
    assert "knowme" not in site.requests[0].content.decode()
    assert http.cookies()["SUserID"] == "900"
    assert http.state.get("query.current") is None


def test_login_wrong_password_raises_with_site_message(http, site):
    http.state.set("account", "someone")
    page = with_message("item_article_46371463", "<div><font color=red>Неверное имя пользователя или пароль"
                                                 "</font></div>")
    site.add(path_is("/start_session.asp"), respond(page))
    with pytest.raises(LoginRequired) as ei:
        login(http, "user", SECRET)
    msg = str(ei.value)
    assert "Неверное имя пользователя или пароль" in msg
    assert "отклонил вход" in msg
    assert SECRET not in msg
    assert http.state.get("account") is None
    assert len(site.requests) == 1


def test_login_still_anonymous_raises(http, site):
    site.add(path_is("/start_session.asp"), respond(fixture_bytes("item_article_46371463")))
    with pytest.raises(LoginRequired) as ei:
        login(http, "user", SECRET)
    assert "не подтвердил вход" in str(ei.value)
    assert ANONYMOUS_USER in str(ei.value)
    assert ei.value.url == "https://elibrary.ru/start_session.asp"


def test_login_requires_credentials_without_request(http, site):
    with pytest.raises(LoginRequired):
        login(http, "  ", SECRET)
    with pytest.raises(LoginRequired):
        login(http, "user", "")
    assert site.requests == []


def test_login_captcha_propagates(http, site):
    site.captcha_for("start_session.asp")
    with pytest.raises(CaptchaRequired):
        login(http, "user", SECRET)
    assert http.status()["breaker_open"] is True


def test_login_ip_restricted_is_login_required(http, site):
    site.add(path_is("/start_session.asp"), lambda r: httpx2.Response(
        302, headers={"Location": "/ip_restricted.asp?rpage=x"}))
    site.add(path_is("/ip_restricted.asp"), respond(b"<html><body>restricted</body></html>"))
    with pytest.raises(IpRestricted):
        login(http, "user", SECRET)


def test_login_from_env_at_most_once(http, site, settings):
    assert login_from_env(http) is None  # no credentials configured
    settings.login, settings.password = "user", SECRET
    site.add(path_is("/start_session.asp"), respond(logged_in_page()))
    info = login_from_env(http)
    assert info is not None and info.logged_in
    assert login_from_env(http) is None, "second attempt in the same process must not hit the site"
    assert len(site.requests) == 1


def test_login_from_env_failure_is_not_retried(http, site, settings):
    settings.login, settings.password = "user", SECRET
    site.add(path_is("/start_session.asp"), respond(fixture_bytes("item_article_46371463")))
    with pytest.raises(LoginRequired):
        login_from_env(http)
    assert login_from_env(http) is None
    assert len(site.requests) == 1


# ---------------------------------------------------------------------------- logout / check / cookies
def test_logout_posts_saveuser_and_clears_account(http, site):
    http.state.set("account", "ivanov_test")
    http.state.set("query.current", {"q": 1})
    site.add(path_is("/end_session.asp"), lambda r: httpx2.Response(302, headers={"Location": "/defaultx.asp"}))
    site.add(path_is("/defaultx.asp"), respond(fixture_bytes("titles_form")))
    info = logout(http)
    req = site.requests[0]
    assert req.method == "POST" and req.url.path == "/end_session.asp"
    # saveuser=0 = "do not save my data": saveuser=1 keeps a remembered login (live finding 2026-10-14)
    assert req.content == b"saveuser=0"
    assert req.headers["Referer"] == "https://elibrary.ru/defaultx.asp"
    assert info.outcome == "logged_out" and info.state == "anonymous"
    assert info.message == "Сессия закрыта"
    assert http.state.get("account") is None
    assert http.state.get("query.current") is None


def test_check_session_resets_breaker_and_reports_login(http, site):
    site.captcha_for("item.asp")
    with pytest.raises(CaptchaRequired):
        http.get("item.asp", {"id": "1"})
    assert http.status()["breaker_open"]
    site.add(path_is("/defaultx.asp"), respond(logged_in_page()))
    n = len(site.requests)
    # without a cookie import after the captcha only a human's explicit --force lifts the pause
    info = check_session(http, force=True)
    assert len(site.requests) == n + 1, "exactly one request"
    assert site.requests[-1].url.path == "/defaultx.asp"
    assert info.logged_in and info.outcome == "checked"
    assert info.message == "Сессия активна, вход выполнен: ivanov_test"
    assert http.state.get("account") == "ivanov_test"
    assert http.status()["breaker_open"] is False
    check_session(http)  # never served from the cache
    assert len(site.requests) == n + 2
    assert http.cache.stats()["pages"] == 0


def test_check_session_anonymous_clears_account(http, site):
    http.state.set("account", "old")
    site.add(path_is("/querybox.asp"), respond(fixture_bytes("querybox_form")))
    info = check_session(http, probe_path="querybox.asp")
    assert info.state == "anonymous" and not info.logged_in
    assert info.message == "Сессия активна, анонимный доступ (вход не выполнен)"
    assert http.state.get("account") is None


def test_check_session_captcha_reraises_and_retrips(http, site):
    site.captcha_for("defaultx.asp")
    with pytest.raises(CaptchaRequired) as ei:
        check_session(http)
    assert "page_captcha.asp" in ei.value.captcha_url
    assert http.status()["breaker_open"] is True
    assert http.status()["breaker_gate"] == "captcha"


def test_import_cookies_names_only(http, site):
    site.captcha_for("item.asp")
    with pytest.raises(CaptchaRequired):
        http.get("item.asp", {"id": "1"})
    http.state.set("account", "old")
    res = import_cookies(http, "SUserID=12345; SCookieGUID=GUID-777; _ym_uid=5", user_agent="Mozilla/5.0 Test")
    assert res["cookie_names"] == ["SCookieGUID", "SUserID"]
    assert res["count"] == 2
    assert res["has_session_cookie"] and res["has_guid_cookie"]
    assert res["ignored"] == ["_ym_uid"]
    assert res["user_agent_saved"] is True
    assert res["warnings"] == []
    assert "12345" not in repr(res) and "GUID-777" not in repr(res)
    assert http.cookies() == {"SUserID": "12345", "SCookieGUID": "GUID-777"}
    assert http.state.get("user_agent") == "Mozilla/5.0 Test"
    assert http.state.get("account") is None
    assert http.status()["breaker_open"] is False


def test_import_cookies_warns_without_suserid_and_ua(http):
    res = import_cookies(http, '[{"name":"SCookieGUID","value":"G","domain":".elibrary.ru"}]')
    assert res["cookie_names"] == ["SCookieGUID"]
    assert res["has_session_cookie"] is False
    assert len(res["warnings"]) == 2
    assert res["warnings"][0].startswith("Нет cookie SUserID")
    assert "User-Agent" in res["warnings"][1]


def test_import_cookies_rejects_garbage(http):
    with pytest.raises(ElibraryError):
        import_cookies(http, "   ")
    with pytest.raises(ElibraryError):
        import_cookies(http, "{not json")
    with pytest.raises(ElibraryError):
        import_cookies(http, "no cookies here")


# ---------------------------------------------------------------------------- browser hand-off (not executed)
def test_browser_session_without_playwright(http, site, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(DependencyMissing) as ei:
        browser_session(http)
    assert ei.value.extra == "browser" and ei.value.package == "playwright"
    assert "elibrary-cli[browser]" in str(ei.value)
    assert site.requests == []


def test_browser_session_rejects_bad_purpose(http):
    with pytest.raises(ValueError):
        browser_session(http, purpose="solve")  # type: ignore[arg-type]


def test_browser_start_url(http, site):
    assert auth._browser_start_url(http, "captcha", None) == "https://elibrary.ru/defaultx.asp"
    site.captcha_for("item.asp")
    with pytest.raises(CaptchaRequired):
        http.get("item.asp", {"id": "7"})
    start = auth._browser_start_url(http, "captcha", None)
    assert start.startswith("https://elibrary.ru/page_captcha.asp?rpage=")
    assert auth._browser_start_url(http, "login", None) == "https://elibrary.ru/defaultx.asp"
    assert auth._browser_start_url(http, "login", "item.asp?id=1") == "https://elibrary.ru/item.asp?id=1"
    with pytest.raises(ElibraryError):
        auth._browser_start_url(http, "captcha", "https://evil-elibrary.ru.example.com/")
    with pytest.raises(ElibraryError):
        auth._browser_start_url(http, "captcha", "https://notelibrary.ru/")


def test_browser_helpers():
    assert auth._cookie_domain("https://elibrary.ru") == ".elibrary.ru"
    assert auth._cookie_domain("https://www.elibrary.ru/") == ".elibrary.ru"
    assert auth._is_site_url("https://www.elibrary.ru/item.asp?id=1", "elibrary.ru")
    assert not auth._is_site_url("about:blank", "elibrary.ru")
    assert not auth._is_site_url("chrome-error://chromewebdata/", "elibrary.ru")
    captcha = fixture_bytes("captcha").decode("utf-8")
    item = fixture_bytes("item_article_46371463").decode("utf-8")
    signed = logged_in_page().decode("utf-8")
    url = "https://elibrary.ru/item.asp?id=46371463"
    assert not auth._purpose_reached("captcha", "https://elibrary.ru/page_captcha.asp?rpage=x", captcha, "elibrary.ru")
    assert not auth._purpose_reached("captcha", url, captcha, "elibrary.ru")  # captcha markup at another URL
    assert auth._purpose_reached("captcha", url, item, "elibrary.ru")
    assert not auth._purpose_reached("captcha", "https://example.com/", item, "elibrary.ru")
    assert not auth._purpose_reached("login", url, item, "elibrary.ru")
    assert auth._purpose_reached("login", url, signed, "elibrary.ru")


def test_site_cookies_prefers_server_values():
    seed = {"SUserID": "old", "SCookieGUID": "G"}
    cookies = [
        {"name": "SUserID", "value": "old", "domain": ".elibrary.ru"},
        {"name": "SUserID", "value": "new", "domain": "elibrary.ru"},
        {"name": "SCookieGUID", "value": "G", "domain": ".elibrary.ru"},
        {"name": "_ga", "value": "x", "domain": ".google.com"},
        {"name": "NID", "value": "y", "domain": ".notelibrary.ru"},
    ]
    assert auth._site_cookies(cookies, seed, "elibrary.ru") == {"SUserID": "new", "SCookieGUID": "G"}


def test_session_info_schema_has_descriptions():
    schema = SessionInfo.model_json_schema()
    for name in ("logged_in", "state", "user_name", "anonymous_markers_found", "outcome"):
        assert schema["properties"][name].get("description")


# ---------------------------------------------------------------------------- review regressions (core safety)
def _post_to_captcha_like_the_site(site, gate_page: str = "page_captcha.asp") -> None:
    """docs/reverse-engineering.md §2: a POST answered by the captcha becomes a GET with ALL its fields
    inside ``rpage`` (``page_captcha.asp?rpage=<url>?<form>``)."""
    captcha = fixture_bytes("captcha")

    def handler(r: httpx2.Request) -> httpx2.Response:
        if gate_page in str(r.url):
            return httpx2.Response(200, content=captcha if "captcha" in gate_page else b"<html>restricted</html>",
                                   headers=HTML)
        if r.method == "POST":
            fields = parse_qsl(r.content.decode("ascii"), keep_blank_values=True)
            return httpx2.Response(302, headers={"Location": f"/{gate_page}?rpage={r.url}?{urlencode(fields)}"})
        return httpx2.Response(404)

    site.add(lambda r: True, handler)


def _leaks(text: object) -> bool:
    text = str(text)
    return any(form in text for form in ("s3cr", quote_plus(SECRET), quote(SECRET, safe="")))


def test_login_captcha_never_leaks_password(http, site, caplog):
    """#1: the captcha URL elibrary builds from the login POST holds the password: it must reach neither the
    error text, the state database, status(), later breaker errors, the browser start URL nor the logs."""
    from elibrary_cli.client import ElibraryClient
    from elibrary_cli.mcp_server import SessionStatus

    _post_to_captcha_like_the_site(site)
    caplog.set_level("DEBUG")
    with pytest.raises(CaptchaRequired) as ei:
        login(http, "reader@example.org", SECRET)
    exc = ei.value
    assert not _leaks(str(exc)) and not _leaks(exc.captcha_url) and not _leaks(exc.url)
    assert exc.captcha_url.startswith("https://elibrary.ru/page_captcha.asp?rpage=")
    assert exc.__cause__ is None and not _leaks(exc.__context__)
    assert not _leaks(_state_dump(http))
    st = ElibraryClient(http=http).status()
    assert st["breaker_open"] is True and st["breaker_gate"] == "captcha"
    assert not _leaks(st["breaker_url"]) and not _leaks(SessionStatus.from_status(st).breaker_url)
    with pytest.raises(CaptchaRequired) as again:  # the open breaker repeats the stored URL
        http.get("item.asp", {"id": "1"})
    assert not _leaks(str(again.value))
    start = auth._browser_start_url(http, "captcha", None)
    assert "page_captcha.asp" in start and not _leaks(start)
    # the captcha page itself was not requested (that GET would carry the password in its query string)
    assert [r.method for r in site.requests] == ["POST"]
    assert not any(_leaks(rec.getMessage()) for rec in caplog.records)


def test_login_ip_restricted_never_leaks_password(http, site):
    _post_to_captcha_like_the_site(site, "ip_restricted.asp")
    with pytest.raises(IpRestricted) as ei:
        login(http, "reader@example.org", SECRET)
    assert not _leaks(str(ei.value)) and not _leaks(ei.value.url)
    assert not _leaks(_state_dump(http))
    assert http.status()["breaker_gate"] == "ip_restricted"


def test_login_scrubs_a_leaky_gate_url(http, site, monkeypatch):
    """#1, defence in depth: even if the HTTP layer stored the raw gate URL, login scrubs it."""
    _post_to_captcha_like_the_site(site)
    monkeypatch.setattr(type(http), "_gate_url", lambda self, gate, url, final_url, *, posted: final_url)
    with pytest.raises(CaptchaRequired) as ei:
        login(http, "reader@example.org", SECRET)
    assert not _leaks(str(ei.value)) and not _leaks(ei.value.captcha_url)
    assert not _leaks(_state_dump(http))
    assert "page_captcha.asp" in http.state.get("breaker.url")


def test_login_and_logout_tell_other_processes(http, site):
    """#3: a login/logout replaces the server session: the cookie version moves so that other processes
    rebuild their HTTP clients."""
    site.add(path_is("/start_session.asp"), respond(logged_in_page()))
    site.add(path_is("/end_session.asp"), respond(fixture_bytes("titles_form")))
    v0 = http.state.get("cookies.version", 0)
    login(http, "user", SECRET)
    v1 = http.state.get("cookies.version", 0)
    logout(http)
    assert v0 < v1 < http.state.get("cookies.version", 0)


def _trip(http, gate: str) -> None:
    from elibrary_cli.http import Gate

    http._trip_breaker(Gate(gate), f"https://elibrary.ru/{'page_captcha' if gate == 'captcha' else gate}.asp", 15)


@pytest.mark.parametrize("gate", ["ip_blocked", "forbidden"])
def test_check_session_never_lifts_a_ban(http, site, gate):
    """#14/#21: an agent cannot lift the 24 h ban pause by checking the session or importing cookies."""
    from elibrary_cli.errors import RateLimited

    site.add(path_is("/defaultx.asp"), respond(logged_in_page()))
    _trip(http, gate)
    import_cookies(http, "SUserID=passed; SCookieGUID=G")
    with pytest.raises(RateLimited) as ei:
        check_session(http)
    assert "support@elibrary.ru" in str(ei.value) and "auth check" not in str(ei.value)
    assert site.requests == [] and http.status()["breaker_open"] is True
    info = check_session(http, force=True)  # CLI `elibrary auth check --force`: a human decision
    assert info.logged_in and len(site.requests) == 1


def test_check_session_lifts_captcha_only_after_a_cookie_import(http, site):
    """#21: after a captcha the check sends nothing until cookies with SUserID were imported after the trip."""
    site.captcha_for("item.asp")
    site.add(path_is("/defaultx.asp"), respond(logged_in_page()))
    with pytest.raises(CaptchaRequired):
        http.get("item.asp", {"id": "1"})
    n = len(site.requests)
    for _ in range(3):  # a polling agent
        with pytest.raises(CaptchaRequired):
            check_session(http)
    import_cookies(http, "x=1")  # junk without SUserID lifts nothing
    with pytest.raises(CaptchaRequired):
        check_session(http)
    assert len(site.requests) == n
    # `elibrary auth captcha` ended without confirmation: cookies saved, breaker kept
    saved = {k: http.state.get(k) for k in auth._BREAKER_KEYS}
    http.set_cookies("SUserID=from-browser; SCookieGUID=G")
    for k, v in saved.items():
        if v is not None:
            http.state.set(k, v)
    assert http.status()["breaker_open"] is True
    info = check_session(http)  # the import happened after the trip: one request
    assert info.logged_in and len(site.requests) == n + 1
    assert http.status()["breaker_open"] is False


def test_import_without_session_cookie_keeps_the_stored_session(http, site):
    """#21: a string without SUserID never drops the stored (signed-in) session."""
    http.set_cookies("SUserID=SIGNED; SCookieGUID=G")
    http.state.set("account", "ivanov_test")
    res = import_cookies(http, "a=b")
    assert http.cookies() == {"SUserID": "SIGNED", "SCookieGUID": "G", "a": "b"}
    assert http.state.get("account") == "ivanov_test"
    assert res["session_cookie_imported"] is False and res["has_session_cookie"] is True
    assert any("сохранённая сессия оставлена" in w for w in res["warnings"])
    res = import_cookies(http, "SUserID=OTHER")
    assert http.cookies() == {"SUserID": "OTHER"} and http.state.get("account") is None
    assert res["session_cookie_imported"] is True


def test_import_cookies_reports_a_pause_that_stays(http, site):
    _trip(http, "ip_blocked")
    res = import_cookies(http, "SUserID=1; SCookieGUID=G", user_agent="UA")
    assert res["breaker_open"] is True and res["breaker_gate"] == "ip_blocked"
    assert any("не снята" in w for w in res["warnings"])


def test_logout_keep_sends_saveuser_1_and_explains_remembered_login(http, site):
    """With remember=True the site restores a "Запомнить меня" login on the next page (live 2026-10-14)."""
    site.add(path_is("/end_session.asp"), respond(fixture_bytes("item_patent_47430099_signed_in")))
    info = logout(http, remember=True)
    assert site.requests[0].content == b"saveuser=1"
    assert info.logged_in and info.user_name == "reader"
    assert "без --keep" in info.message


def test_signed_in_fixture_session_box():
    """A real signed-in page (2026-10-14; login, SPIN and IP replaced by placeholders)."""
    info = parse_session_info(fixture_bytes("item_patent_47430099_signed_in"))
    assert info.logged_in and info.state == "logged_in"
    assert info.user_name == "reader"
    assert info.ip_address == "192.0.2.1"
    assert not info.login_form_present
