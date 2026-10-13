"""Full-text PDFs of elibrary.ru itself (signed-in readers only).

What is known (see ``docs/re/fulltext.md``):

* Anonymous pages define ``load_article(id)``, ``file_article(fid, filenum)``, ``url_article(did,
  urlnum)`` as functions that only alert "необходимо вначале войти в библиотеку". The links are
  ``javascript:load_article(ID)`` (green icon from ``ispdf.asp``) or ``javascript:file_article(ID, N)``
  ("Загрузить: Полный текст (PDF)" on patent pages).
* For signed-in readers (page saved by a user in 2025) ``load_article(id)`` submits the ``results``
  form to ``full_text.asp?id=<id>``; ``url_article`` submits it to ``url_article.asp`` with
  ``did``/``urlnum``. Classic ASP reads the query string as well, and a GET of
  ``full_text.asp?id=<id>`` is what third-party scripts used. The answer is the PDF or a page with a
  link ``/download/elibrary_<id>_<n>.pdf`` (``<n>`` is a server-side number, not derivable).
* ``file_article`` for signed-in readers is not documented anywhere; by analogy with
  ``url_article`` and the hidden ``fileid``/``filenum`` inputs of the ``results`` form we try
  ``file_article.asp?fileid=<fid>&filenum=<n>`` (unverified).
* Session box ``#win_session``: "Имя пользователя: Незарегистрированный пользователь" for anonymous
  visitors, "Имя пользователя: <login>" when signed in; anonymous pages also carry the login form.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from elibrary_cli.errors import LoginRequired, NotFound
from elibrary_cli.http import ElibraryHTTP, decode_html
from elibrary_cli.models.base import BASE_URL
from elibrary_cli.parsers.common import clean, node_text, parse_html

log = logging.getLogger(__name__)

LOGIN_HELP = (
    "Полный текст на eLIBRARY.RU доступен только вошедшим читателям. Войдите: `elibrary auth login` "
    "(логин и пароль из ELIBRARY_LOGIN / ELIBRARY_PASSWORD) или импортируйте cookies вошедшей сессии "
    "браузера: `elibrary auth cookies \"<строка document.cookie>\"` (в MCP — инструмент "
    "elibrary_set_cookies), затем `elibrary auth check`. Без входа используются только открытые "
    "источники (КиберЛенинка, Unpaywall, OpenAlex, Crossref, сайт издателя)."
)

_DOWNLOAD_RE = re.compile(
    r"""(?:https?:)?(?://(?:www\.)?elibrary\.ru)?/download/[\w.%-]+?\.pdf(?:\?[^"'\s<>]*)?""", re.I
)
_LOAD_ARTICLE_RE = re.compile(r"""load_article\(\s*['"]?(\d+)['"]?\s*\)""")
_FILE_ARTICLE_RE = re.compile(r"""file_article\(\s*['"]?(\d+)['"]?\s*,\s*['"]?(\d+)['"]?\s*\)""")
_JS_URL_RE = re.compile(
    r"""(?:window\.open|location\.(?:replace|assign)|(?:window\.|document\.)?location(?:\.href)?\s*=)\s*\(?\s*['"]([^'"]+)['"]""",
)
_REFRESH_RE = re.compile(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["'][^"']*url=([^"'>]+)""", re.I)
_ANONYMOUS_RE = re.compile(r"Незарегистрированный пользователь|Вы не авторизованы", re.I)
_NAMED_SESSION_RE = re.compile(r"Имя пользователя\s*:\s*(\S.*)")
_MUST_LOGIN_RE = re.compile(r"необходимо (?:вначале )?войти в библиотеку", re.I)
_PRICE_RE = re.compile(r"Приобрести эту публикацию за\s*([\d\s]+)\s*руб", re.I)


def _is_elibrary(host: str | None) -> bool:
    host = (host or "").lower()
    return host in ("elibrary.ru", "localhost", "127.0.0.1") or host.endswith(".elibrary.ru")


def _absolute(href: str, base_url: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base_url.rstrip("/") + "/", href)


def find_elibrary_pdf_links(item_html: str | bytes, *, item_id: int | None = None,
                            base_url: str = BASE_URL) -> list[str]:
    """All full-text links of an elibrary page, best first (absolute URLs, no duplicates).

    Order: direct ``/download/....pdf`` links (``href``, ``meta refresh``, JS ``window.open`` /
    ``location``), other ``.pdf`` hrefs on elibrary.ru, ``full_text.asp?id=<id>`` for each
    ``load_article(<id>)`` call (``item_id``'s first) and ``file_article.asp?fileid=..&filenum=..``
    for each ``file_article(<fid>, <n>)`` call. The anonymous variants of these JS functions are
    function *definitions* (``function load_article(iid)``) and are not matched.
    """
    html = decode_html(item_html) if isinstance(item_html, bytes) else item_html
    direct: list[str] = []
    for m in _DOWNLOAD_RE.finditer(html):
        direct.append(_absolute(m.group(0), base_url))
    for m in _REFRESH_RE.finditer(html):
        direct.append(_absolute(m.group(1).strip(), base_url))
    for m in _JS_URL_RE.finditer(html):
        url = m.group(1)
        if "/download/" in url or url.lower().split("?")[0].endswith(".pdf") or "full_text.asp" in url:
            direct.append(_absolute(url, base_url))
    tree = parse_html(html)
    others: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = _absolute(href, base_url)
        parts = urlsplit(absolute)
        path = parts.path.lower()
        if _is_elibrary(parts.hostname) and (path.endswith(".pdf") or path.endswith("/full_text.asp")):
            others.append(absolute)
    ids = [int(x) for x in _LOAD_ARTICLE_RE.findall(html)]
    if item_id is not None and item_id in ids:
        ids.remove(item_id)
        ids.insert(0, item_id)
    full_text = [f"{base_url.rstrip('/')}/full_text.asp?id={i}" for i in ids]
    files = [f"{base_url.rstrip('/')}/file_article.asp?fileid={fid}&filenum={num}"
             for fid, num in _FILE_ARTICLE_RE.findall(html)]
    out: list[str] = []
    for url in [*direct, *others, *full_text, *files]:
        if url not in out and _is_elibrary(urlsplit(url).hostname):
            out.append(url)
    return out


def find_elibrary_pdf_link(item_html: str | bytes, *, item_id: int | None = None,
                           base_url: str = BASE_URL) -> str | None:
    """The best full-text link of an item page (see :func:`find_elibrary_pdf_links`), or ``None``."""
    links = find_elibrary_pdf_links(item_html, item_id=item_id, base_url=base_url)
    return links[0] if links else None


def session_user(html: str | bytes) -> str | None:
    """Login shown in the ``#win_session`` box ("Имя пользователя: ..."), ``None`` if anonymous."""
    tree = parse_html(html)
    box = tree.css_first("#win_session")
    if box is None:
        return None
    text = node_text(box, separator="\n")
    if _ANONYMOUS_RE.search(text):
        return None
    m = _NAMED_SESSION_RE.search(text)
    if not m:
        return None
    user = clean(m.group(1).split("\n")[0])
    # The value may be followed by the next label on the same line after whitespace collapsing.
    user = re.split(r"\s+(?:IP-адрес|Название организации|Начало работы|Время работы)", user)[0]
    return user or None


def is_logged_in(html: str | bytes) -> bool:
    """Is the page rendered for a signed-in reader?

    True when ``#win_session`` names a user; false when it says "Незарегистрированный
    пользователь" / "Вы не авторизованы", or when there is no session box and the page carries the
    login form (``input#login`` + ``input#password``).
    """
    tree = parse_html(html)
    box = tree.css_first("#win_session")
    if box is not None:
        return session_user(html) is not None
    # Without the session box the page cannot prove a signed-in session (fragments, error pages).
    return False


def fulltext_status(item_html: str | bytes) -> dict[str, object]:
    """What an item page says about its full text: ``{"available": bool, "paid": bool,
    "price_rub": int | None, "links": [...]}`` (links as from :func:`find_elibrary_pdf_links`)."""
    html = decode_html(item_html) if isinstance(item_html, bytes) else item_html
    price = _PRICE_RE.search(html)
    links = find_elibrary_pdf_links(html)
    green = "download_green" in html or "Доступ к полному тексту открыт" in html
    return {
        "available": bool(links) or green,
        "paid": price is not None or "order_item(" in html,
        "price_rub": int(re.sub(r"\s", "", price.group(1))) if price else None,
        "links": links,
    }


def _is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(1024)
        return b"%PDF" in head and path.stat().st_size > 1000
    except OSError:
        return False


def download_elibrary_pdf(http: ElibraryHTTP, item_id: int, item_html: str | bytes, dest_dir: Path) -> Path:
    """Download the full-text PDF of ``item_id`` through the signed-in elibrary session.

    ``item_html`` is the item page (``item.asp?id=``) as seen by the current session. Raises
    :class:`LoginRequired` (with instructions) when the page is anonymous or elibrary asks to sign
    in, :class:`NotFound` when the page has no full text (or only a paid one). Returns
    ``dest_dir / "elibrary_<id>.pdf"``; an existing valid file is reused without a request.
    Every request goes through ``http`` (throttle, budgets, captcha breaker).
    """
    if not is_logged_in(item_html):
        raise LoginRequired(LOGIN_HELP, url=f"{BASE_URL}/item.asp?id={item_id}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"elibrary_{item_id}.pdf"
    if _is_pdf_file(dest):
        return dest
    base = http.settings.base_url
    links = find_elibrary_pdf_links(item_html, item_id=item_id, base_url=base)
    if not links:
        status = fulltext_status(item_html)
        if status["paid"]:
            price = f" за {status['price_rub']} руб." if status["price_rub"] else ""
            raise NotFound(f"Полный текст публикации {item_id} на eLIBRARY.RU платный{price} (заказ — на сайте); "
                           "открытого PDF нет.", url=f"{base}/item.asp?id={item_id}")
        raise NotFound(f"На странице публикации {item_id} нет ссылки на полный текст eLIBRARY.RU.",
                       url=f"{base}/item.asp?id={item_id}")
    # The documented signed-in endpoint is a good second try when the page offers another link
    # (file_article on patent pages is not documented).
    full_text = f"{base.rstrip('/')}/full_text.asp?id={item_id}"
    if full_text not in links:
        links.insert(1, full_text)
    referer = f"/item.asp?id={item_id}"
    tmp = dest_dir / f"elibrary_{item_id}.download"
    tried: set[str] = set()
    queue = list(links[:3])
    hops = 0
    while queue and hops < 4:
        url = queue.pop(0)
        if url in tried:
            continue
        tried.add(url)
        hops += 1
        try:
            path, ctype = http.download(url, tmp, referer=referer)
        except NotFound:
            log.debug("elibrary %s: 404, trying the next link", url)
            continue
        if _is_pdf_file(path):
            path.replace(dest)
            return dest
        content = path.read_bytes()
        path.unlink(missing_ok=True)
        text = decode_html(content, ctype)
        if _MUST_LOGIN_RE.search(text) and not is_logged_in(text):
            raise LoginRequired(LOGIN_HELP, url=url)
        inner = [u for u in find_elibrary_pdf_links(text, item_id=item_id, base_url=base)
                 if u not in tried and ("/download/" in u or u.lower().split("?")[0].endswith(".pdf"))]
        log.debug("elibrary %s gave %s (%d bytes), inner links %s", url, ctype, len(content), inner)
        queue = inner + queue
    raise NotFound(f"eLIBRARY.RU не отдал PDF публикации {item_id} (проверены: {', '.join(sorted(tried))}).",
                   url=f"{base}/item.asp?id={item_id}")
