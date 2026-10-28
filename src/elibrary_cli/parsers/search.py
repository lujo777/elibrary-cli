"""Parser of the publication search results page ``query_results.asp``.

Layout (2026-10)::

    <b>ВСЕГО НАЙДЕНО ПУБЛИКАЦИЙ: <font>290705</font> из <font>82293065</font></b>
    <table id="restab"> ... tr#a<ID> rows (see parsers/rows.py), 100 per page ... </table>
    <table><tr class="menus">                                     # paginator, only if > 1 page
      <td><a href="query_results.asp?pagenum=1">В начало</a></td> <td><<</td>
      <td bgcolor=#5c6aa6><font>1</font></td>                     # current page: highlighted, no link
      <td><a href="query_results.asp?pagenum=2">2</a></td> ... <td><a ...pagenum=2>>></a></td>
      <td><a href="query_results.asp?pagenum=2908">В конец</a></td>
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from elibrary_cli.errors import CaptchaRequired, ParseError
from elibrary_cli.models.base import PublicationHit, SearchPage
from elibrary_cli.models.publications import PublicationQuery
from elibrary_cli.parsers.common import clean, int_param, node_text, parse_html, to_int
from elibrary_cli.parsers.item import is_captcha_page
from elibrary_cli.parsers.rows import parse_publication_rows

__all__ = ["PER_PAGE", "parse_search_results"]

PER_PAGE = 100

_TOTAL_RE = re.compile(r"ВСЕГО\s+НАЙДЕНО\s+ПУБЛИКАЦИЙ\s*:?\s*(\d+)(?:\s*из\s*(\d+))?", re.I)
_EMPTY_RE = re.compile(
    r"ничего\s+не\s+найдено|не\s+найдено\s+ни\s+одной|не\s+найдено\s+публикаций|публикаций\s+не\s+найдено|"
    r"по\s+вашему\s+запросу\s+(?:ничего\s+)?не\s+найден|нет\s+публикаций,\s+удовлетворяющих|"
    r"\bне\s+найдено\b|найдено\s+публикаций\s*:?\s*0\b",
    re.I)
_RESULTS_HEADING = "РЕЗУЛЬТАТЫ ПОИСКОВОГО ЗАПРОСА"


def _paginator_cells(tree: LexborHTMLParser):
    """Rows of the paginator table (``tr.menus`` with page links). The "Следующая страница" action
    in the right panel also links to ``pagenum=N`` and must not count."""
    for tr in tree.css("tr"):
        links = tr.css('a[href*="pagenum="]')
        if not links or tr.css_first("table") is not None:  # no links / an outer layout row
            continue
        pages = {int_param(a.attributes.get("href"), "pagenum") for a in links}
        if "menus" in (tr.attributes.get("class") or "") or len(pages) >= 2:
            yield tr


def _current_page(tree: LexborHTMLParser) -> int | None:
    for tr in _paginator_cells(tree):
        for td in tr.css("td"):
            bg = (td.attributes.get("bgcolor") or "").lower()
            if bg == "#5c6aa6" and td.css_first("a") is None:
                n = to_int(node_text(td))
                if n:
                    return n
    return None


def _last_page(tree: LexborHTMLParser) -> int | None:
    best: int | None = None
    for tr in _paginator_cells(tree):
        for a in tr.css('a[href*="pagenum="]'):
            n = int_param(a.attributes.get("href"), "pagenum")
            if n is None:
                continue
            if "конец" in clean(a.text()).lower():
                return n
            best = max(best or 0, n)
    return best


def parse_search_results(
    html: str | bytes,
    *,
    page: int | None = None,
    query: PublicationQuery | Mapping[str, Any] | None = None,
    url: str | None = None,
) -> SearchPage[PublicationHit]:
    """Parse one page of ``query_results.asp`` into ``SearchPage[PublicationHit]``.

    ``page`` is used only when the page has no paginator (a single page of results); ``query`` is
    copied into ``SearchPage.query``. An empty result returns ``total=0`` and no items.
    """
    tree = parse_html(html)
    if is_captcha_page(tree):
        raise CaptchaRequired(url)
    body = tree.body
    text = node_text(body) if body is not None else ""
    m = _TOTAL_RE.search(text)
    total = to_int(m.group(1)) if m else None
    in_db = to_int(m.group(2)) if m and m.group(2) else None
    has_table = tree.css_first("table#restab") is not None
    if m is None and not has_table:
        if _EMPTY_RE.search(text) or _RESULTS_HEADING in text:
            total = 0
        else:
            raise ParseError("Страница результатов поиска публикаций не распознана (query_results.asp)", url=url)

    items = parse_publication_rows(tree) if total != 0 else []
    current = _current_page(tree) or page or 1
    last = _last_page(tree)
    if total == 0:
        pages: int | None = 0
    elif last is not None:
        pages = max(last, current)
    elif total is not None:
        pages = max(1, math.ceil(total / PER_PAGE))
    else:
        pages = current

    if isinstance(query, PublicationQuery):
        q: dict[str, Any] = query.model_dump(mode="json")
    else:
        q = dict(query or {})
    return SearchPage[PublicationHit](
        items=items,
        total=total,
        total_in_database=in_db,
        page=current,
        per_page=PER_PAGE,
        pages=pages,
        query=q,
        source_url=url,
    )
