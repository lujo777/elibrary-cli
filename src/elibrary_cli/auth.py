"""Session and authentication for eLIBRARY.RU.

What this module is built on (see ``docs/reverse-engineering.md`` §2 and ``docs/re/auth.md``):

* Every page of the site has a left panel with two boxes. ``#win_login`` ("ВХОД") holds the login
  form ``<form name="login">`` with the fields ``rpage`` (hidden, the page to return to),
  ``login``, ``password`` and the checkbox ``knowme`` ("Запомнить меня"); the JS ``check_all()``
  submits it to ``POST /start_session.asp``. ``#win_session`` ("СЕССИЯ" / "Текущая сессия") shows
  ``Имя пользователя:`` with ``Незарегистрированный пользователь`` for anonymous visitors or the
  account name (a link to ``author_info.asp?isold=1``) for signed-in readers, plus
  ``Начало работы`` / ``Время работы`` and, on some page families, ``IP-адрес компьютера`` and
  ``Название организации``. Logout is ``POST /end_session.asp`` (form ``session``, JS
  ``end_session()``). For a signed-in reader the JS first asks "Сохранить Ваши данные на этом
  компьютере?": ``saveuser=1`` closes the session but keeps the remembered login (with ``knowme`` the
  next request is signed in again — verified live 2026-10-14), ``saveuser=0`` forgets it.
* The ``<form>`` tags sit between table rows, so an HTML5 parser leaves the ``form`` element
  empty and the inputs outside it. The login form fields are therefore read from the raw markup
  (:func:`login_form_fields`), and the boxes are read as rows of label/value cells.
* Cookies ``SUserID`` and ``SCookieGUID`` are not HttpOnly. After a human passes the reCAPTCHA in
  a browser, the server marks ``SUserID`` as verified for every client on the same IP, which is
  why importing browser cookies (:func:`import_cookies`) or the headed browser hand-off
  (:func:`browser_session`) unlocks the CLI. The code never solves, clicks or automates the
  captcha and never types credentials into a browser: the human does that.

Passwords are never logged, cached or stored: :func:`login` posts them once with the page cache
disabled and keeps only the account name (``http.state["account"]``). When the login POST is answered
by a challenge page, the HTTP layer never follows it and stores a gate URL rebuilt without the form
(elibrary puts the whole form, password included, into ``rpage``); :func:`login` scrubs the error and
the stored breaker URL once more.

Pauses of the circuit breaker are lifted only by a human: a cookie import that carries ``SUserID``
lifts a captcha / ip_restricted pause, :func:`check_session` lifts one only when such an import
happened after the pause started, and a ban pause (ip_blocked, HTTP 403) only with ``force`` (CLI
``elibrary auth check --force``; the MCP tools never pass it).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import re
import threading
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from pydantic import Field

from elibrary_cli.errors import CaptchaRequired, DependencyMissing, ElibraryError, IpRestricted, LoginRequired
from elibrary_cli.http import (
    BAN_GATES,
    BREAKER_KEYS,
    SESSION_COOKIE,
    TTL_NONE,
    Gate,
    classify,
    parse_cookie_header,
)
from elibrary_cli.models.base import BASE_URL, ElibModel
from elibrary_cli.parsers.common import (
    abs_url,
    clean,
    label_pairs,
    node_text,
    parse_html,
    remove_noise,
    text_lines,
)

if TYPE_CHECKING:
    from selectolax.lexbor import LexborNode

    from elibrary_cli.http import ElibraryHTTP

log = logging.getLogger(__name__)

LOGIN_PATH = "start_session.asp"
LOGOUT_PATH = "end_session.asp"
DEFAULT_RPAGE = f"{BASE_URL}/defaultx.asp"
GUID_COOKIE = "SCookieGUID"

#: Text shown in ``#win_session`` instead of a name for anonymous visitors.
ANONYMOUS_USER = "Незарегистрированный пользователь"
#: Other page-level phrases that mean "not signed in".
NOT_AUTHORIZED = "Вы не авторизованы"
LOGIN_FIRST = "необходимо вначале войти в библиотеку"
#: Marker reported when the page carries the login form (it disappears after signing in).
LOGIN_FORM_MARKER = "form[name=login]"
IP_RESTRICTED_MARKER = "ip_restricted.asp"

SessionState = Literal["logged_in", "anonymous", "captcha", "unknown"]
BrowserPurpose = Literal["captcha", "login"]

# Breaker keys of ElibraryHTTP's state store (http.py). Used only to keep the breaker open when a
# browser hand-off ends without the captcha being passed (set_cookies() lifts a captcha pause when the
# browser's cookies carry SUserID). ``cookies.imported_at`` is kept: the human may have passed the check,
# so one `elibrary auth check` stays possible.
_BREAKER_KEYS = BREAKER_KEYS
_ORG_UNKNOWN = {"не определена", "не определено", "не определен", "нет", "-", "—"}
_LABELS = {
    "имя пользователя": "user_name",
    "ip-адрес компьютера": "ip_address",
    "ip-адрес": "ip_address",
    "название организации": "organization",
    "организация": "organization",
    "начало работы": "session_started",
    "время работы": "session_duration",
}
_BROWSER_CHANNELS: tuple[str | None, ...] = ("msedge", "chrome", None)
_POLL_INTERVAL_S = 1.0


class SessionInfo(ElibModel):
    """State of the eLIBRARY.RU session as shown in the page's "СЕССИЯ" box."""

    logged_in: bool = Field(False, description="True when the session box shows an account name (signed in)")
    state: SessionState = Field(
        "unknown",
        description="logged_in, anonymous (the page says 'Незарегистрированный пользователь' or shows the "
        "login form), captcha (the page is the reCAPTCHA 'Тест Тьюринга'), unknown (no session box: fragment "
        "or unexpected page)",
    )
    user_name: str | None = Field(None, description="Account name shown after 'Имя пользователя:' (None if anonymous)")
    organization: str | None = Field(
        None, description="'Название организации' shown in the session/login box (None when 'не определена')")
    ip_address: str | None = Field(None, description="'IP-адрес компьютера' as seen by eLIBRARY.RU")
    session_started: str | None = Field(None, description="'Начало работы': session start, 'DD.MM.YYYY HH:MM'")
    session_duration: str | None = Field(None, description="'Время работы': session duration, 'HH:MM'")
    user_card_url: str | None = Field(
        None, description="Link on the account name (personal card, author_info.asp?isold=1), when signed in")
    login_form_present: bool = Field(False, description="The page carries the login form (only anonymous pages do)")
    anonymous_markers_found: list[str] = Field(
        default_factory=list,
        description="Anonymous markers seen on the page: 'Незарегистрированный пользователь', 'Вы не авторизованы', "
        "'необходимо вначале войти в библиотеку', 'form[name=login]', 'ip_restricted.asp'",
    )
    extra: dict[str, str] = Field(default_factory=dict, description="Other label/value pairs of the session/login box")
    raw_text: str = Field("", description="Visible text of the session box, one line per row")
    url: str | None = Field(None, description="URL of the page the state was read from")
    outcome: str | None = Field(
        None,
        description="Result of the action that produced this state: logged_in, logged_out, checked, solved "
        "(captcha passed in the browser), closed (browser window closed), timeout",
    )
    message: str | None = Field(None, description="Human-readable summary (Russian)")


# ---------------------------------------------------------------------------- parsing
def _leaf_rows(box: LexborNode) -> list[LexborNode]:
    """Table rows of ``box`` that contain no nested rows (selectolax ``css`` includes the node itself)."""
    return [tr for tr in box.css("tr") if len(tr.css("tr")) == 1]


def _box_pairs(box: LexborNode) -> list[tuple[str, str]]:
    """Label/value pairs of a left-panel box: a row ending in ':' is a label, the next row its value.

    A label followed by a row with an input (the login/password field captions) is dropped; rows
    that are neither (links such as "Личный кабинет") are ignored.
    """
    rows = _leaf_rows(box)
    if not rows:
        return label_pairs(text_lines(box))
    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for tr in rows:
        text = node_text(tr)
        has_input = tr.css_first("input, select, textarea") is not None
        if text.endswith(":") and len(text) <= 80 and not has_input:
            if pending is not None:
                pairs.append((pending, ""))
            pending = text[:-1].strip()
            continue
        if pending is not None:
            if not (has_input and not text):
                pairs.append((pending, text))
            pending = None
    if pending is not None:
        pairs.append((pending, ""))
    return pairs


def _is_anonymous_name(value: str) -> bool:
    return ANONYMOUS_USER.lower() in value.lower()


def parse_session_info(html: str | bytes, *, url: str | None = None) -> SessionInfo:
    """Read the session state from any eLIBRARY.RU page (pure function, never fetches).

    Returns ``state="unknown"`` (not an error) for pages without the session box, e.g. AJAX
    fragments, and ``state="captcha"`` for the reCAPTCHA page.
    """
    raw = html if isinstance(html, bytes) else html.encode("utf-8")
    if classify(200, url or "", raw) is Gate.CAPTCHA:
        return SessionInfo(state="captcha", url=url, message="Страница проверки «Тест Тьюринга» (reCAPTCHA)")
    tree = remove_noise(parse_html(html))

    values: dict[str, str] = {}
    extra: dict[str, str] = {}
    markers: list[str] = []
    user_name: str | None = None
    user_card_url: str | None = None
    raw_text = ""

    page_text = node_text(tree.body).lower() if tree.body is not None else ""
    session = tree.css_first("#win_session")
    login_box = tree.css_first("#win_login")
    for box in (session, login_box):  # the session box wins over the login box
        if box is None:
            continue
        for label, value in _box_pairs(box):
            key = _LABELS.get(clean(label).lower())
            if box is login_box and key == "user_name":
                continue  # "Имя пользователя или адрес эл. почты" is the login field caption
            if key is None:
                extra.setdefault(clean(label), value)
            else:
                values.setdefault(key, value)

    if session is not None:
        raw_text = "\n".join(
            line for line in (clean(x) for x in session.text(deep=True, separator="\n").split("\n")) if line
        )[:4000]
        name = values.get("user_name", "")
        if name and _is_anonymous_name(name):
            markers.append(ANONYMOUS_USER)
        elif name:
            user_name = name
            link = session.css_first('a[href*="author_info.asp"]')
            if link is not None:
                user_card_url = abs_url(link.attributes.get("href"), url or BASE_URL)
    elif ANONYMOUS_USER.lower() in page_text:
        markers.append(ANONYMOUS_USER)

    if NOT_AUTHORIZED.lower() in page_text:
        markers.append(NOT_AUTHORIZED)
    if LOGIN_FIRST in page_text:
        markers.append(LOGIN_FIRST)
    login_form = tree.css_first('form[name="login"]') is not None or tree.css_first(
        'input[type="password"][name="password"]') is not None
    if login_form:
        markers.append(LOGIN_FORM_MARKER)
    if url and IP_RESTRICTED_MARKER in url.lower():
        markers.append(IP_RESTRICTED_MARKER)

    logged_in = bool(user_name) and ANONYMOUS_USER not in markers
    if logged_in:
        state: SessionState = "logged_in"
    elif markers:
        state = "anonymous"
    else:
        state = "unknown"
    org = values.get("organization") or None
    if org and org.lower() in _ORG_UNKNOWN:
        org = None
    return SessionInfo(
        logged_in=logged_in,
        state=state,
        user_name=user_name if logged_in else None,
        organization=org,
        ip_address=values.get("ip_address") or None,
        session_started=values.get("session_started") or None,
        session_duration=values.get("session_duration") or None,
        user_card_url=user_card_url if logged_in else None,
        login_form_present=login_form,
        anonymous_markers_found=markers,
        extra=extra,
        raw_text=raw_text,
        url=url,
    )


_FORM_RE = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)(?:</form\s*>|<form\b|\Z)", re.I | re.S)
_INPUT_RE = re.compile(r"<(?:input|select|textarea|button)\b(?P<attrs>[^>]*)>", re.I | re.S)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""", re.S)


def _attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(raw):
        name = m.group(1).lower()
        value = next((g for g in m.group(2, 3, 4) if g is not None), "")
        out.setdefault(name, value)
    return out


def login_form_fields(html: str | bytes, *, form_name: str = "login") -> list[tuple[str, str, str]]:
    """``(name, type, default value)`` of the controls of ``<form name=form_name>``, in submit order.

    Reads the raw markup between ``<form name=...>`` and ``</form>``, because an HTML5 parser moves
    these forms (placed between table rows) away from their inputs. Returns ``[]`` if the form is absent.
    """
    if isinstance(html, bytes):
        from elibrary_cli.http import decode_html

        html = decode_html(html)
    html = re.sub(r"<script\b.*?</script\s*>", "", html, flags=re.I | re.S)
    for m in _FORM_RE.finditer(html):
        if _attrs(m.group("attrs")).get("name", "").lower() != form_name.lower():
            continue
        fields = []
        for inp in _INPUT_RE.finditer(m.group("body")):
            a = _attrs(inp.group("attrs"))
            if a.get("name"):
                fields.append((a["name"], a.get("type", "text").lower(), a.get("value", "")))
        return fields
    return []


def build_login_form(login: str, password: str, *, remember: bool = True,
                     rpage: str = DEFAULT_RPAGE) -> list[tuple[str, str]]:
    """Fields of the login form exactly as the browser submits them (document order).

    ``rpage`` (hidden) comes first, then ``login``, ``password``; the checkbox ``knowme`` is sent as
    ``on`` only when checked ("Запомнить меня"), as browsers omit unchecked boxes.
    """
    fields = [("rpage", rpage), ("login", login.strip()), ("password", password)]
    if remember:
        fields.append(("knowme", "on"))
    return fields


_FAIL_RE = re.compile(
    r"неверн|неправильн|ошибк|не найден|не зарегистрирован|не совпада|не существует|не подтвержд|"
    r"заблокирован|incorrect|invalid|wrong", re.I)
_LOGIN_WORD_RE = re.compile(
    r"парол|логин|имя пользовател|имени пользовател|пользовател|читател|эл\.\s*почт|e-?mail|учетн|учётн|"
    r"вход|авторизац|password|login|user", re.I)
_ALERT_RE = re.compile(r"""alert\s*\(\s*(["'])(.*?)\1\s*\)""", re.S)


def login_failure_reason(html: str | bytes) -> str | None:
    """A sentence of the page that reports a failed login ("неверный пароль", "не найден", ...), or None.

    Defensive: the wrong-password page has not been captured yet. Looks for an error word next to a
    login word in the visible text outside the left-panel boxes and in ``alert("...")`` calls.
    """
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    for m in _ALERT_RE.finditer(text):
        msg = clean(m.group(2))
        if _FAIL_RE.search(msg) and _LOGIN_WORD_RE.search(msg):
            return msg[:300]
    tree = remove_noise(parse_html(text))
    for sel in ("#win_session", "#win_login", "#win_goto", "#win_contact"):
        for n in tree.css(sel):
            n.decompose()
    if tree.body is None:
        return None
    lines = [clean(x) for x in tree.body.text(deep=True, separator="\n").split("\n")]
    lines = [x for x in lines if x]
    for i, line in enumerate(lines):
        if not _FAIL_RE.search(line) or len(line) > 600:
            continue
        around = " ".join(lines[max(0, i - 1): i + 2])
        if _LOGIN_WORD_RE.search(line) or (_LOGIN_WORD_RE.search(around) and len(around) < 400):
            return line[:300]
    return None


# ---------------------------------------------------------------------------- actions over http
def _set_account(http: ElibraryHTTP, info: SessionInfo) -> None:
    if info.logged_in and info.user_name:
        http.state.set("account", info.user_name)
    elif info.state == "anonymous":
        http.state.delete("account")


def login(http: ElibraryHTTP, login: str, password: str, *, remember: bool = True,
          rpage: str | None = None) -> SessionInfo:
    """Sign in with a password: one ``POST /start_session.asp`` (no preliminary GET).

    Success is read from the page the server returns (``#win_session`` shows the account name).
    Raises :class:`LoginRequired` with a Russian message when the site rejects the credentials or
    the page still shows an anonymous session; propagates ``CaptchaRequired``/``IpRestricted``.
    The password is neither logged, cached nor stored; ``http.state["account"]`` keeps the name.
    """
    if not (login or "").strip() or not password:
        raise LoginRequired("Не указаны имя пользователя или пароль eLIBRARY.RU "
                            "(переменные ELIBRARY_LOGIN и ELIBRARY_PASSWORD).")
    rpage = rpage or http.absolute("defaultx.asp")
    form = build_login_form(login, password, remember=remember, rpage=rpage)
    secrets = (password, login.strip())
    log.info("Signing in to eLIBRARY.RU (POST %s)", LOGIN_PATH)
    try:
        page = http.post(LOGIN_PATH, form, referer=rpage, cache_ttl_hours=TTL_NONE)
    except CaptchaRequired as exc:
        captcha_url = _scrub_breaker_url(http, exc.captcha_url, secrets)
        raise CaptchaRequired(_scrub_url(exc.url, secrets), captcha_url=captcha_url) from None
    except IpRestricted as exc:
        _scrub_breaker_url(http, None, secrets)
        raise IpRestricted(exc.message, url=_scrub_url(exc.url, secrets)) from None
    # A new server-side session: the remembered search query no longer exists, and other processes must
    # reload the cookies the server set.
    http.state.delete("query.current")
    http.cookies_changed()
    page.url = _scrub_url(page.url, secrets) or page.url
    info = parse_session_info(page.content, url=page.url)
    if info.logged_in:
        _set_account(http, info)
        info.outcome = "logged_in"
        info.message = f"Вход выполнен: {info.user_name}"
        return info
    http.state.delete("account")
    reason = login_failure_reason(page.content)
    if reason:
        msg = (f"eLIBRARY.RU отклонил вход: «{reason}». Проверьте имя пользователя и пароль "
               "(ELIBRARY_LOGIN / ELIBRARY_PASSWORD). Повторные попытки автоматически не выполняются.")
    else:
        msg = ("eLIBRARY.RU не подтвердил вход: после отправки формы страница показывает анонимную сессию "
               f"({', '.join(info.anonymous_markers_found) or 'имя пользователя не найдено'}). Проверьте имя "
               "пользователя и пароль или войдите в браузере и импортируйте cookies (`elibrary auth cookies`).")
    raise LoginRequired(msg, url=page.url)


def _contains_secret(text: str, secrets: Iterable[str]) -> bool:
    for secret in secrets:
        if secret and len(secret) >= 3 and any(
                form in text for form in {secret, quote(secret, safe=""), quote_plus(secret), quote(secret)}):
            return True
    return False


def _scrub_url(url: str | None, secrets: Iterable[str]) -> str | None:
    """``url`` without its query and fragment when it contains one of ``secrets`` (raw or URL-encoded)."""
    if not url or not _contains_secret(url, secrets):
        return url
    return urlunsplit(urlsplit(url)._replace(query="", fragment=""))


def _scrub_breaker_url(http: ElibraryHTTP, url: str | None, secrets: Iterable[str]) -> str | None:
    """Defence in depth for :func:`login`: the breaker URL stored by the HTTP layer and ``url`` (the
    captcha URL of the error) must never carry the credentials. Returns the scrubbed ``url``."""
    stored = http.state.get("breaker.url")
    if isinstance(stored, str) and _contains_secret(stored, secrets):
        http.state.set("breaker.url", _scrub_url(stored, secrets))
    return _scrub_url(url, secrets)


_env_lock = threading.Lock()
_env_login_attempted = False


def login_from_env(http: ElibraryHTTP) -> SessionInfo | None:
    """Sign in with ``ELIBRARY_LOGIN``/``ELIBRARY_PASSWORD`` (``http.settings.login/password``).

    Tried at most once per process (repeated logins look like credential stuffing): returns None
    without any request when the credentials are not set or an attempt was already made; otherwise
    returns :func:`login`'s result or raises its :class:`LoginRequired`.
    """
    global _env_login_attempted
    user, password = http.settings.login, http.settings.password
    if not user or not password:
        return None
    with _env_lock:
        if _env_login_attempted:
            return None
        _env_login_attempted = True
    return login(http, user, password)


def _reset_env_login_attempt() -> None:
    """Forget that an environment login was attempted (tests only)."""
    global _env_login_attempted
    with _env_lock:
        _env_login_attempted = False


def logout(http: ElibraryHTTP, *, remember: bool = False) -> SessionInfo:
    """Close the session: ``POST /end_session.asp`` (form ``session`` of every page).

    ``remember=False`` sends ``saveuser=0`` — the site's "do not save my data on this computer" answer,
    which really signs out. ``saveuser=1`` (``remember=True``) only closes the current session: a login
    made with "Запомнить меня" is restored on the next request (verified live 2026-10-14).
    Clears the stored account name and the remembered search query; cookies are kept (the server
    replaces them with an anonymous session).
    """
    referer = http.absolute("defaultx.asp")
    saveuser = "1" if remember else "0"
    page = http.post(LOGOUT_PATH, [("saveuser", saveuser)], referer=referer, cache_ttl_hours=TTL_NONE)
    http.state.delete("account", "query.current")
    http.cookies_changed()
    info = parse_session_info(page.content, url=page.url)
    info.outcome = "logged_out"
    if not info.logged_in:
        info.message = "Сессия закрыта" + (" (данные для входа сохранены на сайте)" if remember else "")
    elif remember:
        info.message = (f"Сессия закрыта, но сайт снова вошёл под именем {info.user_name}: вход был запомнен "
                        "(«Запомнить меня»). Чтобы выйти совсем, выполните `elibrary auth logout` без --keep.")
    else:
        info.message = f"Сервер всё ещё показывает вход под именем {info.user_name}"
    return info


def check_session(http: ElibraryHTTP, *, probe_path: str = "defaultx.asp", force: bool = False) -> SessionInfo:
    """Make ONE uncached request to see where the session stands.

    An open circuit breaker is lifted for it only when that is safe (``http.release_breaker_for_check``):
    a captcha / ip_restricted pause when cookies with ``SUserID`` were imported after the pause started,
    a ban pause (ip_blocked, HTTP 403) never. Otherwise the breaker's error is raised without a request
    (``CaptchaRequired``, ``RateLimited``). ``force=True`` lifts any pause: only for a human's explicit
    decision (CLI ``elibrary auth check --force``), never for an agent. Re-raises ``CaptchaRequired``
    (the HTTP layer trips the breaker again) and the other gate errors. Updates ``http.state["account"]``.
    """
    url = http.absolute(probe_path)
    http.release_breaker_for_check(url, force=force)
    page = http.get(probe_path, cache_ttl_hours=TTL_NONE)
    info = parse_session_info(page.content, url=page.url)
    _set_account(http, info)
    info.outcome = "checked"
    if info.logged_in:
        info.message = f"Сессия активна, вход выполнен: {info.user_name}"
    elif info.state == "anonymous":
        info.message = "Сессия активна, анонимный доступ (вход не выполнен)"
    else:
        info.message = "Страница получена, но состояние сессии на ней не показано"
    return info


def import_cookies(http: ElibraryHTTP, raw: str, *, user_agent: str | None = None,
                   replace: bool = True) -> dict[str, Any]:
    """Install cookies copied from the user's browser (``document.cookie``, a ``Cookie:`` header,
    JSON, Playwright ``storage_state`` or ``cookies.txt``).

    A string with ``SUserID`` replaces the stored cookies (or is merged into them, ``replace=False``)
    and lifts a captcha / ip_restricted pause; a ban pause (ip_blocked, HTTP 403) stays. A string
    without ``SUserID`` never drops the stored session: it is merged into it and lifts nothing.

    Returns names only (never values): ``cookie_names``, ``count``, ``has_session_cookie``,
    ``has_guid_cookie``, ``session_cookie_imported`` (``SUserID`` was in the string), ``ignored``
    (analytics cookies dropped), ``user_agent_saved``, ``breaker_open`` / ``breaker_gate`` (the pause
    that is still on), ``warnings``.
    """
    try:
        parsed = parse_cookie_header(raw or "")
    except (ValueError, KeyError, TypeError) as exc:  # malformed JSON export
        raise ElibraryError("Не удалось разобрать строку cookies (некорректный JSON). Скопируйте результат "
                            "copy(document.cookie) или заголовок Cookie целиком.") from exc
    if not parsed:
        raise ElibraryError("В переданной строке нет cookies. На странице elibrary.ru выполните в консоли "
                            "разработчика (F12) copy(document.cookie) и передайте результат.")
    imported_session = bool(parsed.get(SESSION_COOKIE))
    stored = http.state.get("cookies", {}) or {}
    keep_session = replace and not imported_session and bool(stored.get(SESSION_COOKIE))
    merged = http.set_cookies(parsed, replace=replace and not keep_session, user_agent=user_agent)
    if not keep_session:
        http.state.delete("account")
    ignored = sorted(set(parsed) - set(merged))
    warnings: list[str] = []
    if keep_session:
        warnings.append(f"В строке нет cookie {SESSION_COOKIE}: сохранённая сессия оставлена, переданные cookies "
                        "добавлены к ней.")
    breaker = http.breaker()
    if breaker is not None:
        gate = breaker.get("gate")
        if gate in BAN_GATES:
            warnings.append(f"Пауза запросов после блокировки доступа ({gate}) не снята: импорт cookies её не "
                            "снимает. Не повторяйте запросы; напишите в support@elibrary.ru.")
        else:
            warnings.append(f"Пауза запросов после ответа «{gate}» не снята: в строке нет cookie {SESSION_COOKIE} "
                            "сессии, прошедшей проверку в браузере.")
    if SESSION_COOKIE not in merged:
        warnings.append(f"Нет cookie {SESSION_COOKIE} (идентификатор сессии eLIBRARY.RU): без него пройденная "
                        "проверка и вход не переносятся. Скопируйте cookies со страницы elibrary.ru после её загрузки.")
    if GUID_COOKIE not in merged:
        warnings.append(f"Нет cookie {GUID_COOKIE}; обычно браузер передаёт его вместе с {SESSION_COOKIE}.")
    if not user_agent:
        warnings.append("User-Agent браузера не передан (navigator.userAgent); будет использован User-Agent CLI.")
    return {
        "cookie_names": sorted(merged),
        "count": len(merged),
        "has_session_cookie": SESSION_COOKIE in merged,
        "has_guid_cookie": GUID_COOKIE in merged,
        "session_cookie_imported": imported_session,
        "ignored": ignored,
        "user_agent_saved": bool(user_agent),
        "breaker_open": breaker is not None,
        "breaker_gate": breaker.get("gate") if breaker is not None else None,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------- headed browser hand-off
def _base_host(http: ElibraryHTTP) -> str:
    return (urlsplit(http.settings.base_url).hostname or "elibrary.ru").lower()


def _cookie_domain(base_url: str) -> str:
    """Domain for cookies injected into the browser: ``https://elibrary.ru`` -> ``.elibrary.ru``."""
    host = (urlsplit(base_url).hostname or "elibrary.ru").lower()
    if host.startswith("www."):
        host = host[4:]
    return "." + host


def _is_site_url(url: str, base_host: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    root = base_host[4:] if base_host.startswith("www.") else base_host
    return parts.scheme in ("http", "https") and (
        host in (base_host, root, "elibrary.ru") or host.endswith(("." + root, ".elibrary.ru")))


def _browser_start_url(http: ElibraryHTTP, purpose: str, url: str | None) -> str:
    if url:
        target = http.absolute(url)
    elif purpose == "captcha" and (stored := http.state.get("breaker.url")) and "page_captcha.asp" in str(stored):
        target = str(stored)
    else:
        target = http.absolute("defaultx.asp")
    if not _is_site_url(target, _base_host(http)):
        raise ElibraryError(f"В браузере можно открыть только страницы eLIBRARY.RU, а не {target}")
    return target


def _purpose_reached(purpose: str, url: str, html: str | None, base_host: str) -> bool:
    """Poll predicate of :func:`browser_session` (read-only: URL and main-frame HTML)."""
    if not html or not _is_site_url(url, base_host):
        return False
    if purpose == "captcha":
        if "page_captcha.asp" in url.lower() or "check_captcha.asp" in url.lower():
            return False
        return classify(200, url, html.encode("utf-8", "replace")) is not Gate.CAPTCHA
    return parse_session_info(html, url=url).logged_in


def _site_cookies(cookies: Iterable[Mapping[str, Any]], seed: Mapping[str, str], base_host: str) -> dict[str, str]:
    """name -> value for the site's cookies from Playwright ``context.cookies()``.

    If a name exists for several domains, a value the server changed (differs from what was
    injected) wins over the injected copy.
    """
    root = base_host[4:] if base_host.startswith("www.") else base_host
    out: dict[str, str] = {}
    for c in cookies:
        domain = str(c.get("domain", "")).lstrip(".").lower()
        name, value = str(c.get("name", "")), str(c.get("value", ""))
        if not name or not (domain in (root, "elibrary.ru") or domain.endswith(("." + root, ".elibrary.ru"))):
            continue
        if name not in out or (out[name] == seed.get(name) and value != seed.get(name)):
            out[name] = value
    return out


def _launch_browser(p: Any, channel: str | None) -> Any:
    channels = (channel,) if channel else _BROWSER_CHANNELS
    errors: list[str] = []
    for ch in channels:
        try:
            if ch:
                return p.chromium.launch(headless=False, channel=ch)
            return p.chromium.launch(headless=False)
        except Exception as exc:  # browser not installed / failed to start
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            errors.append(f"{ch or 'chromium'}: {first[:200]}")
    raise ElibraryError(
        "Не удалось запустить окно браузера (пробовали: "
        + "; ".join(errors)
        + "). Установите Microsoft Edge или Google Chrome, либо встроенный Chromium: `playwright install chromium`."
    )


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


_OUTCOME_MESSAGES = {
    "solved": "Проверка пройдена в браузере, cookies сессии сохранены для CLI.",
    "logged_in": "Вход выполнен в браузере, cookies сессии сохранены для CLI.",
    "closed": "Окно браузера закрыто до завершения; cookies сохранены, но проверка/вход не подтверждены.",
    "timeout": "Время ожидания истекло; cookies сохранены, но проверка/вход не подтверждены.",
}


def _run_browser_session(http: ElibraryHTTP, sync_playwright: Any, target: str, purpose: str,
                         timeout_s: float, channel: str | None) -> SessionInfo:
    base_host = _base_host(http)
    ua = http.state.get("user_agent") or http.settings.user_agent
    seed = http.cookies()
    domain = _cookie_domain(http.settings.base_url)
    outcome = "timeout"
    last_url, last_html = target, None
    snapshot: list[dict[str, Any]] = []
    browser_ua: str | None = None
    with sync_playwright() as p:
        browser = _launch_browser(p, channel)
        try:
            context = browser.new_context(user_agent=ua, locale="ru-RU")
            if seed:
                context.add_cookies([{"name": k, "value": v, "domain": domain, "path": "/"} for k, v in seed.items()])
            page = context.new_page()
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:  # the window stays open: the human can reload
                log.info("Browser navigation to %s failed: %s", target, exc)
            deadline = time.monotonic() + max(1.0, float(timeout_s))
            while True:
                try:
                    pages = [pg for pg in context.pages if not pg.is_closed()]
                except Exception:
                    pages = []
                if not pages or not browser.is_connected():
                    outcome = "closed"
                    break
                reached = False
                with contextlib.suppress(Exception):
                    snapshot = list(context.cookies()) or snapshot
                for pg in pages:
                    try:
                        if browser_ua is None:
                            browser_ua = pg.evaluate("() => navigator.userAgent")
                        cur_url, cur_html = pg.url, pg.content()
                    except Exception:  # navigating or closing: read again on the next tick
                        continue
                    if pg is pages[0] or _is_site_url(cur_url, base_host):
                        last_url, last_html = cur_url, cur_html
                    if _purpose_reached(purpose, cur_url, cur_html, base_host):
                        last_url, last_html, reached = cur_url, cur_html, True
                        break
                if reached:
                    outcome = "solved" if purpose == "captcha" else "logged_in"
                    break
                if time.monotonic() >= deadline:
                    outcome = "timeout"
                    break
                try:
                    pages[0].wait_for_timeout(_POLL_INTERVAL_S * 1000)
                except Exception:
                    time.sleep(_POLL_INTERVAL_S)
            with contextlib.suppress(Exception):
                snapshot = list(context.cookies()) or snapshot
        finally:
            with contextlib.suppress(Exception):
                browser.close()

    success = outcome in ("solved", "logged_in")
    cookies = _site_cookies(snapshot, seed, base_host)
    if cookies:
        saved = {k: http.state.get(k) for k in _BREAKER_KEYS} if not success else {}
        http.set_cookies(cookies, user_agent=browser_ua or ua)
        for k, v in saved.items():  # captcha not passed: keep the breaker open
            if v is not None:
                http.state.set(k, v)
    info = parse_session_info(last_html, url=last_url) if last_html else SessionInfo(url=last_url)
    _set_account(http, info)
    info.outcome = outcome
    info.message = _OUTCOME_MESSAGES[outcome]
    if outcome == "logged_in" and info.user_name:
        info.message = f"Вход выполнен в браузере: {info.user_name}; cookies сессии сохранены для CLI."
    return info


def browser_session(http: ElibraryHTTP, *, url: str | None = None, purpose: BrowserPurpose = "captcha",
                    timeout_s: float = 600, channel: str | None = None) -> SessionInfo:
    """Open a HEADED browser with the CLI's session so that the human passes the captcha or signs in.

    Needs the optional extra ``[browser]`` (Playwright; raises ``DependencyMissing`` otherwise).
    Tries the ``msedge``, ``chrome`` channels, then Playwright's Chromium (or only ``channel``).
    The context gets the CLI's User-Agent and cookies (domain ``.elibrary.ru``) and opens ``url``
    (default: the captcha page that tripped the breaker, else ``defaultx.asp``). It then only
    watches, once a second, until the page is no longer a captcha (``purpose="captcha"``) or the
    session box shows an account (``purpose="login"``), the window is closed, or ``timeout_s``
    passes. The code never clicks the captcha, never touches the reCAPTCHA iframe and never types
    credentials. Finally the browser's cookies and ``navigator.userAgent`` are saved with
    ``http.set_cookies`` (the breaker stays open if the captcha was not passed).
    Blocking; when called inside a running event loop it runs in a worker thread.
    """
    if purpose not in ("captcha", "login"):
        raise ValueError(f"purpose: ожидается 'captcha' или 'login', получено {purpose!r}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DependencyMissing("browser", "playwright") from exc
    target = _browser_start_url(http, purpose, url)
    if _in_event_loop():  # Playwright's sync API refuses to run inside an asyncio loop
        with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="elibrary-browser") as ex:
            return ex.submit(_run_browser_session, http, sync_playwright, target, purpose, timeout_s,
                             channel).result()
    return _run_browser_session(http, sync_playwright, target, purpose, timeout_s, channel)


__all__ = [
    "ANONYMOUS_USER",
    "DEFAULT_RPAGE",
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "SessionInfo",
    "browser_session",
    "build_login_form",
    "check_session",
    "import_cookies",
    "login",
    "login_failure_reason",
    "login_form_fields",
    "login_from_env",
    "logout",
    "parse_session_info",
]
