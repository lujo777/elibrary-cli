"""Parsers for the organization pages: orgs.asp, org_profile.asp, org_about.asp, org_items.asp.

Markup notes (2026-10, details in ``docs/re/organizations.md``):

* ``orgs.asp``: counter ``Всего найдено организаций: <b>23</b> из <b>123983</b>. Показано на данной
  странице: с <b>1</b> по <b>20</b>``; ``table#restab`` with a header row (№ | Название организации |
  Город | Публ. | Цит.) and rows ``tr#a<ORGID>``; the name links to ``org_about.asp?orgsid=ID``, the
  publications cell to ``org_items.asp?orgsid=ID`` and ``org_profile.asp?id=ID``, the citations cell to
  ``cit_org_items.asp?id=ID``. Pagination: ``javascript:goto_page(N)``.
* ``org_profile.asp``: header ``div[style*=540px]`` (name in ``font b``, town in ``font[color=#000000]``,
  then ``, Country``); sections are ``div.midtext > b`` headings each followed by a ``table`` whose rows
  are ``td`` (help icon with the tooltip in ``title``) | ``td`` name | value cells.
* ``org_about.asp`` (no 2026 fixture; layout of a 2024 snapshot): pairs of cells
  ``td[bgcolor=#dddddd]`` (label) + ``td[bgcolor=#eeeeee]`` (value) after the heading "КАРТОЧКА ОРГАНИЗАЦИИ".
* ``org_items.asp`` (no fixture): same layout as ``author_items.asp``; rows are parsed by
  :func:`parsers.rows.parse_publication_rows`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import math
import re
from collections.abc import Mapping
from typing import Any

from selectolax.lexbor import LexborHTMLParser, LexborNode

from elibrary_cli.errors import NotFound, ParseError
from elibrary_cli.models.base import SearchPage
from elibrary_cli.models.organizations import (
    OrgDissertationCouncil,
    OrgFieldRow,
    OrgHit,
    OrgIndicator,
    OrgJournal,
    OrgProfile,
    OrgPublicationsPage,
    OrgQuery,
    OrgRosstatRow,
    OrgTable,
    OrgYearSeries,
)
from elibrary_cli.parsers.common import (
    abs_url,
    clean,
    int_param,
    node_text,
    parse_html,
    query_param,
    remove_noise,
    text_lines,
    to_int,
)
from elibrary_cli.parsers.journals import strip_emails
from elibrary_cli.parsers.rows import parse_publication_rows
from elibrary_cli.queries.organizations import (
    ORDER_CODES,
    ORG_ITEMS_PER_PAGE,
    ORG_SORT_CODES,
    ORGS_PER_PAGE,
)

__all__ = [
    "parse_org_about",
    "parse_org_form_options",
    "parse_org_profile",
    "parse_org_search",
    "parse_organization_publications",
]

_ORG_ROW_RE = re.compile(r"^a(\d+)$")
_TOTAL_ORGS_RE = re.compile(r"Всего найдено организаций:\s*([\d\s]+?)\s*(?:из\s*([\d\s]+?))?\s*(?:\.|$|Показано)", re.I)
_TOTAL_PUBS_RE = re.compile(r"Всего найдено\s*:?\s*([\d\s]+?)\s*публикац", re.I)
_TOTAL_CITED_RE = re.compile(r"общим количеством цитирований:\s*([\d\s]*\d)", re.I)
_RANGE_RE = re.compile(r"Показано на данной странице:\s*с\s*(\d+)\s*по\s*(\d+)", re.I)
_GOTO_RE = re.compile(r"goto_page\((\d+)\)")
_NUMBER_RE = re.compile(r"^\s*(-?\d[\d\s]*?)(?:[.,](\d+))?(?=\s*(?:\(|%|$))")
_PERCENT_RE = re.compile(r"\(\s*(-?\d+(?:[.,]\d+)?)\s*%\s*\)")
_YEAR_RANGE_RE = re.compile(r"(\d{4})\s*[-–]\s*(\d{4})")
_YEAR_RE = re.compile(r"ЗА\s+(\d{4})\s+ГОД", re.I)
_UPDATED_RE = re.compile(r"Дата обновления показателей организации:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_SUFFIX_RE = re.compile(
    r"\s*(?:/|-|–|—)\s*(?:анализ публикационной активности|список публикаций(?: организации)?)\s*$", re.I
)
_DEFAULT_ORDER_RE = re.compile(r'case\s*"(\d+)"\s*:\s*ord\.value\s*=\s*"(\d+)"')
_ORG_ITEMS_HREF_RE = re.compile(r"(?:^|/)org_items\.asp", re.I)

_SORT_KEYS = {v: k for k, v in ORG_SORT_CODES.items()}
_ORDER_KEYS = {v: k for k, v in ORDER_CODES.items()}


# --------------------------------------------------------------------------- small helpers


def _norm(text: str) -> str:
    return clean(text).replace("ё", "е").replace("Ё", "Е").casefold()


def _children(node: LexborNode, tag: str) -> list[LexborNode]:
    """Direct element children of ``node`` with the given tag (``tbody`` is looked through)."""
    out: list[LexborNode] = []
    for child in node.iter():
        if child.tag == tag:
            out.append(child)
        elif child.tag == "tbody" and tag == "tr":
            out.extend(c for c in child.iter() if c.tag == "tr")
    return out


def _closest(node: LexborNode | None, tag: str) -> LexborNode | None:
    node = node.parent if node is not None else None
    while node is not None and node.tag != tag:
        node = node.parent
    return node


def _own_rows(table: LexborNode) -> list[LexborNode]:
    """Rows of ``table`` itself (not of nested tables), in document order."""
    return [tr for tr in table.css("tr") if (t := _closest(tr, "table")) is not None and t.mem_id == table.mem_id]


def _cells(tr: LexborNode) -> list[LexborNode]:
    return _children(tr, "td") or _children(tr, "th")


def _parse_number(text: str | None) -> int | float | None:
    """``"502087"`` -> 502087, ``"1,339"`` -> 1.339, ``"44995 (36,7%)"`` -> 44995, ``"нет"`` -> None."""
    t = clean(text)
    if not t:
        return None
    m = _NUMBER_RE.match(t)
    if not m:
        return None
    whole = re.sub(r"\s", "", m.group(1))
    try:
        if m.group(2) is not None:
            return float(f"{whole}.{m.group(2)}")
        return int(whole)
    except ValueError:
        return None


def _percent(text: str | None) -> float | None:
    m = _PERCENT_RE.search(clean(text))
    return float(m.group(1).replace(",", ".")) if m else None


def _as_int(value: int | float | None) -> int | None:
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    return value


def _help_text(node: LexborNode | None) -> str | None:
    """Tooltip of the ``img.help`` icon inside ``node`` (HTML in ``title`` stripped)."""
    if node is None:
        return None
    img = node.css_first("img.help, .help[title]")
    if img is None:
        return None
    raw = img.attributes.get("title") or ""
    text = clean(_TAG_RE.sub(" ", raw))
    return text or None


def _int_or_none(text: str | None) -> int | None:
    return to_int(text) if text and re.search(r"\d", text) else None


def _selected(tree: LexborHTMLParser, name: str) -> str | None:
    sel = tree.css_first(f'select[name="{name}"]')
    if sel is None:
        return None
    for opt in sel.css("option"):
        if "selected" in opt.attributes:
            return opt.attributes.get("value") or ""
    return None


def _input_value(tree: LexborHTMLParser, name: str) -> str | None:
    node = tree.css_first(f'input[name="{name}"]')
    return node.attributes.get("value") if node is not None else None


def _page_text(tree: LexborHTMLParser) -> str:
    return node_text(tree.body or tree.root)


def _max_goto_page(tree: LexborHTMLParser) -> int | None:
    pages = [int(m.group(1)) for a in tree.css('a[href*="goto_page"]') if (m := _GOTO_RE.search(a.attributes.get("href") or ""))]
    return max(pages) if pages else None


def _paging(
    tree: LexborHTMLParser, text: str, total: int | None, page: int | None, default_per_page: int
) -> tuple[int, int, int | None]:
    """Return ``(page, per_page, pages)`` from the counter, the hidden ``pagenum`` and the pager links."""
    per_page = default_per_page
    start = end = None
    if m := _RANGE_RE.search(text):
        start, end = int(m.group(1)), int(m.group(2))
    if page is None:
        if start is not None:
            page = (start - 1) // per_page + 1
        else:
            page = to_int(_input_value(tree, "pagenum")) or 1
    if start is not None and end is not None and page == 1 and total is not None and end < total:
        per_page = max(1, end - start + 1)
    pages: int | None = None
    if total is not None:
        pages = math.ceil(total / per_page) if per_page else None
    goto = _max_goto_page(tree)
    if goto is not None:
        pages = max(pages or 0, goto, page)
    elif pages is None and start is not None:
        pages = page  # a single page without pager links
    return page, per_page, pages


def _header_block(tree: LexborHTMLParser) -> tuple[str | None, str | None, str | None, LexborNode | None]:
    """``(NAME, town, country, node)`` of the centered organization header (profile and items pages)."""
    for div in tree.css("div"):
        style = (div.attributes.get("style") or "").replace(" ", "")
        if "width:540px" not in style:
            continue
        b = div.css_first("font b") or div.css_first("b")
        if b is None:
            continue
        name = node_text(b) or None
        town_node = div.css_first('font[color="#000000"]')
        town = node_text(town_node) or None
        lines = text_lines(div)
        rest = [ln for ln in lines if ln not in (name, town)]
        country = clean(" ".join(rest)).strip(" ,;") or None
        return name, town, country, div
    return None, None, None, None


def _name_from_title(tree: LexborHTMLParser) -> str | None:
    """Mixed-case name from ``<title>NAME / анализ публикационной активности</title>`` (or ``- Список публикаций``)."""
    title = node_text(tree.css_first("title"))
    name = _TITLE_SUFFIX_RE.sub("", title).strip()
    if not title or name == title:  # generic title such as "Информация об организации"
        return None
    return name or None


def _form_controls(tree: LexborHTMLParser, form_name: str) -> list[str]:
    """Names of the controls of a form in document order.

    elibrary opens ``<FORM>`` directly inside ``<table>``; the HTML5 parser then leaves the ``form``
    element empty, so the controls are collected by document order up to the next form.
    """
    names: list[str] = []
    inside = False
    for node in tree.css("form, input, select, textarea"):
        if node.tag == "form":
            if inside:
                break
            inside = node.attributes.get("name") == form_name
            continue
        if inside and (name := node.attributes.get("name")) and name not in names:
            names.append(name)
    return names


# --------------------------------------------------------------------------- orgs.asp


def parse_org_search(
    html: str | bytes,
    *,
    page: int | None = None,
    query: OrgQuery | Mapping[str, Any] | None = None,
    url: str | None = None,
) -> SearchPage[OrgHit]:
    """Parse a result page of ``orgs.asp`` into ``SearchPage[OrgHit]`` (20 rows per page).

    ``query`` (the :class:`OrgQuery` sent) is echoed into ``SearchPage.query``; without it the query is
    read back from the form on the page. An empty result returns an empty page; a page that is not
    orgs.asp raises :class:`ParseError`.
    """
    tree = remove_noise(parse_html(html))
    text = _page_text(tree)
    table = tree.css_first("table#restab")
    m_total = _TOTAL_ORGS_RE.search(text)
    has_form = tree.css_first('input[name="orgname"]') is not None
    has_org_rows = table is not None and table.css_first('a[href*="org_about.asp"]') is not None
    if m_total is None and not has_form and not has_org_rows:
        raise ParseError("Страница не похожа на результаты поиска организаций (orgs.asp)", url=url)

    items: list[OrgHit] = []
    if table is not None:
        columns: list[str] = []
        for tr in _own_rows(table):
            cells = _cells(tr)
            m = _ORG_ROW_RE.match(tr.attributes.get("id") or "")
            if m is None:
                if not columns and cells and not tr.css_first('a[href*="org_about.asp"]'):
                    columns = [node_text(td) for td in cells]
                continue
            hit = _parse_org_row(tr, cells, columns, int(m.group(1)))
            if hit is not None:
                items.append(hit)

    total = total_db = None
    if m_total:
        total = to_int(m_total.group(1))
        total_db = to_int(m_total.group(2)) if m_total.group(2) else None
    elif table is None:
        total = 0
    if total is None and not items:
        total = 0
    page_no, per_page, pages = _paging(tree, text, total, page, ORGS_PER_PAGE)

    if isinstance(query, OrgQuery):
        q: dict[str, Any] = query.model_dump(exclude_none=True)
    elif query is not None:
        q = dict(query)
    else:
        q = _echo_org_query(tree)
    return SearchPage[OrgHit](
        items=items,
        total=total,
        total_in_database=total_db,
        page=page_no,
        per_page=per_page,
        pages=pages,
        query=q,
        source_url=url,
    )


def _parse_org_row(tr: LexborNode, cells: list[LexborNode], columns: list[str], org_id: int) -> OrgHit | None:
    data: dict[str, Any] = {"id": org_id}
    extra: dict[str, str] = {}
    for idx, td in enumerate(cells):
        col = columns[idx] if idx < len(columns) else ""
        key = _norm(col)
        txt = node_text(td)
        if key in ("№", "n", "no") or (not key and idx == 0):
            data["position"] = _int_or_none(txt)
        elif "назван" in key or "организац" in key:
            link = td.css_first('a[href*="org_about.asp"]')
            data["name"] = node_text(link) if link is not None else txt
        elif key.startswith("город"):
            data["town"] = txt or None
        elif key.startswith("регион"):
            data["region"] = txt or None
        elif key.startswith("стран"):
            data["country"] = txt or None
        elif key.startswith("публ"):
            data["publications"] = _int_or_none(txt)
        elif key.startswith("цит"):
            data["citations"] = _int_or_none(txt)
        elif txt:
            extra[col or f"col{idx + 1}"] = txt
    for a in tr.css("a[href]"):
        href = a.attributes.get("href") or ""
        if "org_about.asp" in href:
            data.setdefault("url", abs_url(href))
            if not data.get("name"):
                data["name"] = node_text(a)
        elif "cit_org_items.asp" in href:
            data.setdefault("citing_url", abs_url(href))
        elif _ORG_ITEMS_HREF_RE.search(href):
            data.setdefault("publications_url", abs_url(href))
        elif "org_profile.asp" in href:
            data.setdefault("profile_url", abs_url(href))
    if not data.get("name"):
        return None
    data["extra"] = extra
    return OrgHit(**data)


def _echo_org_query(tree: LexborHTMLParser) -> dict[str, Any]:
    """The query as the server echoes it in the form ``results``."""
    q: dict[str, Any] = {}
    if name := clean(_input_value(tree, "orgname")):
        q["name"] = name
    if town := clean(_selected(tree, "town")):
        q["town"] = town
    region = to_int(_selected(tree, "regionid"))
    if region:
        q["region_id"] = region
    if country := clean(_selected(tree, "countryid")):
        q["country"] = country
    if (sort := _selected(tree, "sortorder")) in _SORT_KEYS:
        q["sort"] = _SORT_KEYS[sort]
    if (order := _selected(tree, "order")) in _ORDER_KEYS:
        q["order"] = _ORDER_KEYS[order]
    return q


def parse_org_form_options(html: str | bytes, *, max_towns: int = 2000) -> dict[str, Any]:
    """Select options of the orgs.asp form in the format of ``data/orgs_options.json``.

    Lists of ``{"value", "label", "count"}`` for ``countryid``, ``regionid`` and (when it has at most
    ``max_towns`` entries) ``town``; ``sortorder``/``order`` with ``key`` (name/town/... and asc/desc).
    The empty "any" option is skipped. ``_meta`` holds the form field order and the town count.
    """
    tree = parse_html(html)
    out: dict[str, Any] = {}
    meta: dict[str, Any] = {
        "page": "https://elibrary.ru/orgs.asp",
        "fields": _form_controls(tree, "results")
        or ["pagenum", "orgname", "town", "regionid", "countryid", "sortorder", "order"],
    }
    for name in ("countryid", "regionid", "town"):
        sel = tree.css_first(f'select[name="{name}"]')
        if sel is None:
            continue
        opts = []
        for opt in sel.css("option"):
            value = opt.attributes.get("value")
            label = node_text(opt)
            if value in (None, "", "0") and not label:
                continue
            count = None
            if m := re.match(r"^(.*?)\s*\((\d+)\)\s*$", label):
                label, count = m.group(1).strip(), int(m.group(2))
            item: dict[str, Any] = {"value": value if value is not None else label, "label": label}
            if count is not None:
                item["count"] = count
            opts.append(item)
        if name == "town" and len(opts) > max_towns:
            meta["town_count"] = len(opts)
            meta["town_note"] = (
                "Список городов не включён (слишком большой). Поле town — строка: название города так, "
                "как его пишет eLIBRARY.RU (например 'Москва', 'Санкт-Петербург')."
            )
            continue
        out[name] = opts
    for name, keys in (("sortorder", _SORT_KEYS), ("order", _ORDER_KEYS)):
        sel = tree.css_first(f'select[name="{name}"]')
        if sel is None:
            continue
        # onChange='... case "3" : ord.value="1"; ...' -> default order per sort key
        defaults = dict(_DEFAULT_ORDER_RE.findall(sel.attributes.get("onchange") or ""))
        items = []
        for opt in sel.css("option"):
            value = opt.attributes.get("value") or ""
            item = {"value": value, "label": node_text(opt), "key": keys.get(value)}
            if value in defaults:
                item["default_order"] = defaults[value]
            items.append(item)
        out[name] = items
    out["_meta"] = meta
    return out


# --------------------------------------------------------------------------- org_profile.asp

_GENERAL_FIELDS = {
    "число публикаций на elibrary.ru": "publications",
    "число публикаций в ринц": "publications_rinc",
    "число публикаций, входящих в ядро ринц": "publications_core",
    "число цитирований публикаций на elibrary.ru": "citations",
    "число цитирований публикаций в ринц": "citations_rinc",
    "число цитирований из публикаций, входящих в ядро ринц": "citations_from_core",
    "индекс хирша по всем публикациям на elibrary.ru": "h_index",
    "индекс хирша по публикациям в ринц": "h_index_rinc",
    "индекс хирша по ядру ринц": "h_index_core",
    "g-индекс": "g_index",
    "i-индекс": "i_index",
    "число авторов": "authors_count",
    "число авторов, зарегистрированных в science index": "authors_registered",
}
_FIVE_YEAR_FIELDS = {
    "число публикаций на elibrary.ru": "publications_5y",
    "число публикаций в ринц": "publications_rinc_5y",
    "число публикаций, входящих в ядро ринц": "publications_core_5y",
    "число цитирований в ринц": "citations_rinc_5y",
    "средневзвешенный импакт-фактор журналов, в которых были опубликованы статьи": "impact_factor_5y",
}
_FLOAT_FIELDS = {"impact_factor_5y"}


class _Grid:
    """A profile table: expanded header labels and data rows (lists of cells)."""

    def __init__(self, table: LexborNode) -> None:
        self.columns: list[str] = []
        self.rows: list[list[LexborNode]] = []
        for tr in _own_rows(table):
            cells = _cells(tr)
            if not cells:
                continue
            if len(cells) == 1 and cells[0].attributes.get("colspan"):
                continue  # separators and spacer rows
            if not self.columns and not self.rows and tr.css_first('font[color="#555555"]') is not None:
                for td in cells:
                    span = to_int(td.attributes.get("colspan")) or 1
                    self.columns.extend([node_text(td)] * max(1, span))
                continue
            if not any(node_text(td) for td in cells):
                continue
            self.rows.append(cells)

    @staticmethod
    def name_index(cells: list[LexborNode]) -> int:
        """Index of the first cell with text (the first cell is usually the help icon)."""
        return next((i for i, td in enumerate(cells) if node_text(td)), 0)

    def column(self, idx: int) -> str:
        return self.columns[idx] if idx < len(self.columns) else f"col{idx + 1}"


def _section_tables(tree: LexborHTMLParser) -> list[tuple[str, LexborNode]]:
    """``(heading, table)`` for every ``div.midtext`` heading followed by a table."""
    out: list[tuple[str, LexborNode]] = []
    for div in tree.css("div.midtext"):
        b = div.css_first("b")
        if b is None:
            continue
        heading = node_text(b)
        if not heading:
            continue
        node = div.next
        while node is not None:
            if node.tag == "table":
                out.append((heading, node))
                break
            if node.tag == "div" and "midtext" in (node.attributes.get("class") or "") and node.css_first("b"):
                break
            node = node.next
    return out


def _is_subrow(cell: LexborNode, name: str) -> bool:
    if re.match(r"^(в т\.\s*ч\.|из них)", name, re.I):
        return True
    if cell.css_first('img[src*="1pix"][hspace]') is not None:
        return True
    div = cell.css_first("div[style]")
    return div is not None and "padding-left" in (div.attributes.get("style") or "")


def parse_org_profile(html: str | bytes, *, url: str | None = None, with_descriptions: bool = False) -> OrgProfile:
    """Parse ``org_profile.asp?id=ID`` ("Анализ публикационной активности организации").

    Fills the name, town, country and every indicator table. Card fields (full name, address, type,
    contacts) come from :func:`parse_org_about`; join both with :meth:`OrgProfile.merge`.
    ``with_descriptions=True`` also keeps the tooltip text of every indicator (long).
    """
    tree = remove_noise(parse_html(html))
    text = _page_text(tree)
    upper = text.upper()
    if "АНАЛИЗ ПУБЛИКАЦИОННОЙ АКТИВНОСТИ" not in upper and "ОБЩИЕ ПОКАЗАТЕЛИ" not in upper:
        raise ParseError("Страница не похожа на анализ публикационной активности организации (org_profile.asp)", url=url)
    header_name, town, country, header = _header_block(tree)
    org_id = to_int(_input_value(tree, "id")) or int_param(url, "id")
    if org_id is None and header is not None and (a := header.css_first('a[href*="org_about.asp"]')) is not None:
        org_id = int_param(a.attributes.get("href"), "orgsid")
    sections = _section_tables(tree)
    if org_id is None:
        raise ParseError("Не найден идентификатор организации на странице org_profile.asp", url=url)
    if not header_name and not sections:
        raise NotFound(f"Организация {org_id} не найдена на eLIBRARY.RU", url=url)

    data: dict[str, Any] = {
        "id": org_id,
        "name": _name_from_title(tree) or header_name or str(org_id),
        "town": town,
        "country": country,
    }
    if url:
        data["url"] = url
    if header is not None and (a := header.css_first('a[href*="org_about.asp"]')) is not None:
        data["about_url"] = abs_url(a.attributes.get("href"))

    metrics: dict[str, Any] = {}
    metrics_5y: dict[str, Any] = {}
    indicators: list[OrgIndicator] = []
    lists: dict[str, list[Any]] = {
        "by_year": [],
        "kbpr_by_year": [],
        "publications_by_field": [],
        "citations_by_field": [],
        "rosstat": [],
        "journals": [],
        "dissertation_councils": [],
        "other_tables": [],
    }
    for heading, table in sections:
        h = heading.upper()
        grid = _Grid(table)
        if h.startswith("ОБЩИЕ ПОКАЗАТЕЛИ"):
            _indicator_rows(grid, heading, metrics, indicators, data, _GENERAL_FIELDS, with_descriptions)
        elif h.startswith("ПОКАЗАТЕЛИ ЗА 5 ЛЕТ"):
            if m := _YEAR_RANGE_RE.search(heading):
                data["period_5y"] = f"{m.group(1)}-{m.group(2)}"
            _indicator_rows(grid, heading, metrics_5y, indicators, data, _FIVE_YEAR_FIELDS, with_descriptions)
        elif h.startswith("ПОКАЗАТЕЛИ ПО ГОДАМ"):
            lists["by_year"] = _year_rows(grid, with_descriptions)
        elif h.startswith("КБПР"):
            lists["kbpr_by_year"] = _year_rows(grid, with_descriptions)
        elif "ПО ОБЛАСТЯМ ЗНАНИЙ" in h and h.startswith("ПУБЛИКАЦИИ"):
            if m := _YEAR_RE.search(heading):
                data["fields_year"] = int(m.group(1))
            lists["publications_by_field"] = _field_rows(grid)
        elif "ПО ОБЛАСТЯМ ЗНАНИЙ" in h and h.startswith("ЦИТИРОВАНИЯ"):
            lists["citations_by_field"] = _field_rows(grid)
        elif h.startswith("ОТЧЕТ ДЛЯ РОССТАТА"):
            if m := _YEAR_RE.search(heading):
                data["rosstat_year"] = int(m.group(1))
            lists["rosstat"] = _rosstat_rows(grid)
        elif h.startswith("ИЗДАТЕЛЬСКАЯ ДЕЯТЕЛЬНОСТЬ"):
            lists["journals"] = _journal_rows(grid)
        elif h.startswith("ДИССЕРТАЦИОННЫЕ СОВЕТЫ"):
            lists["dissertation_councils"] = _council_rows(grid)
        elif h.startswith("СТАТИСТИЧЕСКИЕ ОТЧЕТЫ"):
            continue  # javascript:org_profile("rubrics") report links, no data
        else:
            lists["other_tables"].append(_generic_table(heading, grid))
    data.update(lists)
    data["metrics"] = metrics
    data["metrics_5y"] = metrics_5y
    data["indicators"] = indicators

    if m := _UPDATED_RE.search(text):
        with contextlib.suppress(ValueError):
            data["updated"] = _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    data["notes"] = _profile_notes(tree)
    return OrgProfile(**data)


def _indicator_rows(
    grid: _Grid,
    heading: str,
    metrics: dict[str, Any],
    indicators: list[OrgIndicator],
    data: dict[str, Any],
    typed: Mapping[str, str],
    with_descriptions: bool,
) -> None:
    for cells in grid.rows:
        ni = grid.name_index(cells)
        name = node_text(cells[ni])
        if not name or ni + 1 >= len(cells):
            continue
        cell = cells[ni + 1]
        raw = node_text(cell)
        value = _parse_number(raw)
        link = cell.css_first("a[href]")
        indicators.append(
            OrgIndicator(
                section=heading,
                name=name,
                value=value,
                percent=_percent(raw),
                text=raw,
                url=abs_url(link.attributes.get("href")) if link is not None else None,
                description=_help_text(cells[0]) if with_descriptions and ni > 0 else None,
            )
        )
        metrics.setdefault(name, value)
        field = typed.get(_norm(name))
        if field and field not in data:
            data[field] = float(value) if field in _FLOAT_FIELDS and value is not None else _as_int(value)


def _year_rows(grid: _Grid, with_descriptions: bool) -> list[OrgYearSeries]:
    out: list[OrgYearSeries] = []
    for cells in grid.rows:
        ni = grid.name_index(cells)
        name = node_text(cells[ni])
        values: dict[int, Any] = {}
        for idx in range(ni + 1, len(cells)):
            year = to_int(grid.column(idx))
            if year is None or not 1900 <= year <= 2100:
                continue
            values[year] = _parse_number(node_text(cells[idx]))
        out.append(
            OrgYearSeries(
                name=name, values=values, description=_help_text(cells[0]) if with_descriptions and ni > 0 else None
            )
        )
    return out


def _field_rows(grid: _Grid) -> list[OrgFieldRow]:
    out: list[OrgFieldRow] = []
    for cells in grid.rows:
        ni = grid.name_index(cells)
        name = node_text(cells[ni])
        values = {grid.column(idx): _as_int(_parse_number(node_text(cells[idx]))) for idx in range(ni + 1, len(cells))}
        out.append(OrgFieldRow(name=name, subfield=_is_subrow(cells[ni], name), values=values))
    return out


def _rosstat_rows(grid: _Grid) -> list[OrgRosstatRow]:
    out: list[OrgRosstatRow] = []
    for cells in grid.rows:
        ni = grid.name_index(cells)
        name = node_text(cells[ni])
        row: dict[str, Any] = {"name": name, "subrow": _is_subrow(cells[ni], name)}
        for idx in range(ni + 1, len(cells)):
            col = _norm(grid.column(idx))
            txt = node_text(cells[idx])
            if col.startswith("№") or "строк" in col:
                row["line"] = txt or None
            elif col.startswith("публ"):
                row["publications"] = _int_or_none(txt)
                if (a := cells[idx].css_first("a[href]")) is not None:
                    row["url"] = abs_url(a.attributes.get("href"))
            elif col.startswith("сотр"):
                row["staff"] = _int_or_none(txt)
            elif col.startswith("до"):
                row["staff_under_39"] = _int_or_none(txt)
        out.append(OrgRosstatRow(**row))
    return out


def _journal_rows(grid: _Grid) -> list[OrgJournal]:
    out: list[OrgJournal] = []
    for cells in grid.rows:
        ni = grid.name_index(cells)
        title_cell = cells[ni]
        link = title_cell.css_first("a[href]")
        href = link.attributes.get("href") if link is not None else None
        roles_text = " ".join(node_text(td) for td in cells[ni + 1 :])
        out.append(
            OrgJournal(
                title_id=int_param(href, "id") if href and "title_about" in href else None,
                title=node_text(title_cell),
                roles=[r.strip() for r in roles_text.split(",") if r.strip()],
                url=abs_url(href),
            )
        )
    return out


def _council_rows(grid: _Grid) -> list[OrgDissertationCouncil]:
    out: list[OrgDissertationCouncil] = []
    for cells in grid.rows:
        ni = grid.name_index(cells)
        rest = cells[ni + 1 :]
        out.append(
            OrgDissertationCouncil(
                code=node_text(cells[ni]),
                specialties=text_lines(rest[0]) if rest else [],
                council_type=(node_text(rest[1]) or None) if len(rest) > 1 else None,
            )
        )
    return out


def _generic_table(heading: str, grid: _Grid) -> OrgTable:
    rows = []
    for cells in grid.rows:
        ni = grid.name_index(cells)
        rows.append([node_text(td) for td in cells[ni:]])
    offset = 1 if grid.columns and not grid.columns[0] else 0
    return OrgTable(title=heading, columns=grid.columns[offset:], rows=rows)


def _profile_notes(tree: LexborHTMLParser) -> list[str]:
    notes: list[str] = []
    for img in tree.css('img[src*="but_orange_!"]'):
        td = _closest(img, "td")
        tr = _closest(td, "tr") if td is not None else None
        if tr is None:
            continue
        cells = _cells(tr)
        txt = " ".join(node_text(c) for c in cells if c.mem_id != td.mem_id)
        txt = clean(txt)
        if txt and not _UPDATED_RE.search(txt) and txt not in notes:
            notes.append(txt)
    return notes


# --------------------------------------------------------------------------- org_about.asp

_CARD_FIELDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^полное (название|наименование)$"), "full_name"),
    (re.compile(r"^(название|наименование)$"), "full_name"),
    (re.compile(r"^(название|наименование) (на англ|англ)"), "name_en"),
    (re.compile(r"^сокращение англ|^сокращенное (название|наименование) (на англ|англ)"), "abbreviation_en"),
    (re.compile(r"^сокращ"), "abbreviation"),
    (re.compile(r"^страна"), "country"),
    (re.compile(r"^регион|^субъект"), "region"),
    (re.compile(r"^город на англ|^город англ"), "town_en"),
    (re.compile(r"^(город|населенный пункт)"), "town"),
    (re.compile(r"^адрес на англ|^адрес англ"), "address_en"),
    (re.compile(r"^юридический адрес"), "legal_address"),
    (re.compile(r"^(почтовый адрес|адрес)"), "address"),
    (re.compile(r"^ведомство|^учредитель|^подчинение"), "department"),
    (re.compile(r"^(головная|вышестоящая|родительская) организац|^входит в"), "parent_name"),
    (re.compile(r"^тип"), "org_type"),
    (re.compile(r"^телефон"), "phone"),
    # only the organization's own address: "E-mail представителя" & co. are personal (see strip_emails)
    (re.compile(r"^(e-?mail|эл\.? ?почта|электронная почта|адрес электронной почты)$"), "email"),
    (re.compile(r"^факс"), "fax"),
    (re.compile(r"^(www|веб-?сайт|сайт|url)"), "website"),
]
_IDENTIFIER_RE = re.compile(r"^(инн|огрн|кпп|окпо|ror|grid|isni|wikidata|crossref|fundref|орган\w* id)\b", re.I)


def _card_pairs(tree: LexborHTMLParser) -> list[tuple[str, LexborNode]]:
    """``(label, value cell)`` pairs of the card: a label cell (``bgcolor=#dddddd`` or text ending in
    ``:``) followed in the same row by a value cell. Only rows after the "КАРТОЧКА ОРГАНИЗАЦИИ" heading."""
    pairs: list[tuple[str, LexborNode]] = []
    started = False
    for node in tree.css("b, tr"):  # document order
        if node.tag == "b":
            started = started or "КАРТОЧКА" in node_text(node).upper()
            continue
        if not started:
            continue
        cells = _cells(node)
        for label_cell, value_cell in zip(cells, cells[1:], strict=False):
            if _bgcolor(value_cell) == "#dddddd":
                continue
            label = node_text(label_cell)
            if not label or len(label) > 60:
                continue
            if _bgcolor(label_cell) == "#dddddd" or label.endswith(":"):
                pairs.append((label.rstrip(":").strip(), value_cell))
    return pairs


def _bgcolor(node: LexborNode) -> str:
    return (node.attributes.get("bgcolor") or "").strip().lower()


def parse_org_about(html: str | bytes, *, url: str | None = None) -> OrgProfile:
    """Parse the organization card ``org_about.asp?orgsid=ID`` ("КАРТОЧКА ОРГАНИЗАЦИИ").

    Untested against a 2026 page (layout taken from a 2024 snapshot). Returns an :class:`OrgProfile`
    with the card fields only; merge it into the analysis page with ``profile.merge(card)``.
    """
    tree = remove_noise(parse_html(html))
    text = _page_text(tree)
    if "КАРТОЧКА" not in text.upper() or "ОРГАНИЗАЦИИ" not in text.upper():
        raise ParseError("Страница не похожа на карточку организации (org_about.asp)", url=url)
    org_id = to_int(_input_value(tree, "orgsid")) or int_param(url, "orgsid")
    if org_id is None:
        raise ParseError("Не найден идентификатор организации на странице org_about.asp", url=url)
    pairs = _card_pairs(tree)
    if not pairs:
        raise NotFound(f"Карточка организации {org_id} пуста или не найдена", url=url)

    data: dict[str, Any] = {"id": org_id}
    extra: dict[str, str] = {}
    identifiers: dict[str, str] = {}
    for label, cell in pairs:
        value = node_text(cell)
        if not value:
            continue
        key = _norm(label)
        if _IDENTIFIER_RE.match(key):
            identifiers[label] = value
            continue
        field = next((f for rx, f in _CARD_FIELDS if rx.search(key)), None)
        if field is None or field in data:
            # unknown pairs are kept, but without e-mail addresses (a representative's address is personal)
            if v := strip_emails(value):
                extra[label] = v
            continue
        data[field] = value
        link = cell.css_first("a[href]")
        href = link.attributes.get("href") if link is not None else None
        if field == "country" and (code := query_param(href, "countryid")):
            data["country_code"] = code
        elif field == "region" and (rid := int_param(href, "regionid")):
            data["region_id"] = rid
        elif field == "department" and (vid := int_param(href, "vedomid")):
            data["department_id"] = vid
        elif field == "parent_name" and href and (pid := int_param(href, "orgsid") or int_param(href, "id")):
            data["parent_id"] = pid
        elif field == "website" and href and href.lower().startswith("http"):
            data["website"] = href.strip()
        elif field == "email" and href and href.lower().startswith("mailto:"):
            data["email"] = href[7:].strip() or value
    data["identifiers"] = identifiers
    data["extra"] = extra
    data["name"] = data.get("full_name") or data.get("abbreviation") or str(org_id)
    if url:
        data["about_url"] = url
    return OrgProfile(**data)


# --------------------------------------------------------------------------- org_items.asp


def parse_organization_publications(
    html: str | bytes, *, page: int | None = None, url: str | None = None
) -> OrgPublicationsPage:
    """Parse ``org_items.asp?orgsid=ID`` (publications of an organization), 100 rows per page.

    Same layout as ``author_items.asp``; checked against a live 2026-10 page (``org_items_18078.html``:
    "Всего найдено 49 публикаций с общим количеством цитирований: 12", hidden ``orgsid``, no pager).
    Rows are parsed by :func:`parsers.rows.parse_publication_rows`.
    """
    tree = remove_noise(parse_html(html))
    text = _page_text(tree)
    table = tree.css_first("table#restab")
    org_id = to_int(_input_value(tree, "orgsid")) or int_param(url, "orgsid")
    m_total = _TOTAL_PUBS_RE.search(text)
    if table is None and m_total is None and org_id is None and "СПИСОК ПУБЛИКАЦИЙ" not in text.upper():
        raise ParseError("Страница не похожа на список публикаций организации (org_items.asp)", url=url)
    items = parse_publication_rows(tree)
    total = to_int(m_total.group(1)) if m_total else (0 if not items else None)
    total_cited = None
    if m := _TOTAL_CITED_RE.search(text):
        total_cited = to_int(m.group(1))
    page_no, per_page, pages = _paging(tree, text, total, page, ORG_ITEMS_PER_PAGE)

    header_name, town, _country, _node = _header_block(tree)
    q: dict[str, Any] = {}
    if org_id is not None:
        q["org_id"] = org_id
    for name in ("show_option", "show_sotr", "sortorder", "order", "orgdepid"):
        if (v := _selected(tree, name)) not in (None, ""):
            q[name] = v
    for name in ("show_refs", "hide_doubles"):
        if (v := _input_value(tree, name)) not in (None, ""):
            q[name] = v
    return OrgPublicationsPage(
        items=items,
        total=total,
        page=page_no,
        per_page=per_page,
        pages=pages,
        query=q,
        source_url=url,
        org_id=org_id,
        org_name=_name_from_title(tree) or header_name,
        org_town=town,
        total_citations=total_cited,
    )

