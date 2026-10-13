"""Parsers for the journal pages: catalog search, journal profile, indicators and issue contents.

Markup (2026-10, see ``docs/re/journals.md``):

* ``titles.asp`` -- counter "Всего найдено журналов: <b>N</b> из <b>M</b>. Показано на данной странице:
  с <b>a</b> по <b>b</b>", ``table#restab`` with a header row (№ | checkbox | Журнал | Вып. | Публ. | Цит.)
  and rows ``tr#a<TITLEID>``; pagination links ``javascript:goto_page(N)``.
* ``title_about_new.asp`` -- blocks ``<table><tr><td colspan=2><font color=#000000>HEADING:</font></td></tr>
  <tr><td>&nbsp;</td><td>CONTENT</td></tr></table>``; inside the content, cells "Label:&nbsp;<font>value</font>".
* ``title_profile.asp`` -- (no fixture) tables headed "Название показателя | Значение" like org_profile.asp.
* ``contents.asp`` -- issue header (eLIBRARY ID, EDN, journal link, "Том: N Номер: N Год: N"),
  ``table#restab`` with rubric rows ``<tr><td colspan=4><b>SECTION</b></td></tr>`` and rows ``tr#arw<ID>``
  (title | pages | citations), and the list of issues: ``<tbody id="issueyearYYYY">`` blocks.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser, LexborNode

from elibrary_cli.errors import NotFound, ParseError
from elibrary_cli.models.base import SearchPage
from elibrary_cli.models.journals import (
    INDICATOR_FIELDS,
    EditorialMember,
    IssueContents,
    IssueRef,
    JournalFile,
    JournalHit,
    JournalIndexingInfo,
    JournalIndicators,
    JournalOrgRef,
    JournalProfile,
    JournalQuery,
    JournalSection,
    PeerReview,
    Rubric,
    issue_contents_url,
)
from elibrary_cli.parsers.common import (
    abs_url,
    clean,
    int_param,
    node_text,
    parse_html,
    query_param,
    remove_noise,
    to_float,
    to_int,
    yes_no,
)
from elibrary_cli.parsers.rows import parse_publication_rows

log = logging.getLogger(__name__)

_TOTAL_RE = re.compile(r"Всего найдено журналов:\s*(\d[\d\s]*?)\s*из\s*(\d[\d\s]*\d|\d)", re.I)
_TOTAL_ONLY_RE = re.compile(r"Всего найдено журналов:\s*(\d+)", re.I)
_SHOWN_RE = re.compile(r"Показано на данной странице:\s*с\s*(\d[\d\s]*?)\s*по\s*(\d[\d\s]*)", re.I)
_GOTO_RE = re.compile(r"goto_page\((\d+)\)")
_ISSN_RE = re.compile(r"(?<![\d-])\d{4}-\d{3}[\dXxХх](?![\d-])")
_HIT_ROW_RE = re.compile(r"^a(\d+)$")
_COUNT_RE = re.compile(r"^(.*?)\s*\((\d[\d\s]*)\)\s*$")
_PAIR_RE = re.compile(r"^([^:]{1,100}?):\s*(.*)$", re.S)
_ARTICLES_RE = re.compile(r"\(\s*(\d+)\s*ст\.?\s*\)")
_VOLUME_RE = re.compile(r"(?:\bТом|Т\.)\s*([^\s,№]+)")
_NUMBER_RE = re.compile(r"(?:№|\bВып\.|\bВыпуск|\bНомер)\s*([^\s,]+)")
_ISSUE_HEADER_RE = re.compile(r"^(?:(?:Том|Номер|Выпуск|Часть|Год):\s*\S+\s*)+$")
_ISSUE_HEADER_PAIR_RE = re.compile(r"(Том|Номер|Выпуск|Часть|Год):\s*(\S+)")
#: E-mail addresses in free text. Personal addresses (editorial staff) are never kept; the only e-mail of
#: a journal profile is the office contact of the "КОНТАКТНАЯ ИНФОРМАЦИЯ" block.
_EMAIL_RE = re.compile(r"(?:[,;]\s*)?(?:mailto:)?[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)

#: Select fields of the titles.asp form, in DOM order.
SEARCH_SELECTS: tuple[str, ...] = (
    "countryid", "rubriccode", "language", "translated", "wos", "scopus", "white", "vak", "risc", "ftopen",
    "sortorder", "order",
)
_CHECKBOX_FIELDS: tuple[tuple[str, str], ...] = (
    ("rsci", "rsci"), ("doaj", "doaj"), ("corerisc", "core_rinc"), ("fulltext", "fulltext"),
    ("isactive", "active"), ("isscientific", "scientific"),
)
_SELECT_ATTRS: dict[str, str] = {
    "countryid": "country", "rubriccode": "rubric_code", "language": "language", "translated": "translated",
    "wos": "wos", "scopus": "scopus", "white": "white", "vak": "vak", "risc": "risc", "ftopen": "ftopen",
    "sortorder": "sort", "order": "order",
}


# --------------------------------------------------------------------------------------------------
# DOM helpers
# --------------------------------------------------------------------------------------------------


def _children(node: LexborNode, *tags: str) -> list[LexborNode]:
    out: list[LexborNode] = []
    child = node.child
    while child is not None:
        if child.is_element_node and (not tags or child.tag in tags):
            out.append(child)
        child = child.next
    return out


def _has_desc(node: LexborNode, selector: str) -> bool:
    """True when a descendant matches ``selector`` (lexbor's ``css()`` also matches the node itself)."""
    return any(n.mem_id != node.mem_id for n in node.css(selector))


def _rows(table: LexborNode) -> list[LexborNode]:
    """Direct rows of a table (through the implicit/explicit tbody), not rows of nested tables."""
    rows: list[LexborNode] = []
    for c in _children(table):
        if c.tag == "tr":
            rows.append(c)
        elif c.tag in ("tbody", "thead", "tfoot"):
            rows.extend(_children(c, "tr"))
    return rows


def _cells(tr: LexborNode) -> list[LexborNode]:
    return _children(tr, "td", "th")


def _ancestor(node: LexborNode | None, tag: str) -> LexborNode | None:
    while node is not None and node.tag != tag:
        node = node.parent
    return node


def _next_element(node: LexborNode | None, tag: str | None = None) -> LexborNode | None:
    node = node.next if node is not None else None
    while node is not None and (not node.is_element_node or (tag and node.tag != tag)):
        node = node.next
    return node


def _page_title(tree: LexborHTMLParser) -> str:
    t = tree.css_first("title")
    return node_text(t) if t is not None else ""


def _body_text(tree: LexborHTMLParser) -> str:
    return node_text(tree.body) if tree.body is not None else ""


def _split_count(text: str) -> tuple[str, int | None]:
    text = clean(text)
    m = _COUNT_RE.match(text)
    if m:
        return clean(m.group(1)), to_int(m.group(2))
    return text, None


def _selected_value(select: LexborNode | None) -> str | None:
    if select is None:
        return None
    options = select.css("option")
    for opt in options:
        if "selected" in opt.attributes:
            return opt.attributes.get("value") or ""
    return (options[0].attributes.get("value") or "") if options else None


def _split_outside_parens(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth:
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [clean(p) for p in parts if clean(p)]


def split_issue_label(label: str | None) -> tuple[str | None, str | None]:
    """``"Т. 31 № 4"`` -> ``("31", "4")``; ``"Т.22 № S1"`` -> ``("22", "S1")``; ``"№ 3"`` -> ``(None, "3")``;
    ``"Online First"`` -> ``(None, None)``."""
    if not label:
        return None, None
    text = clean(label)
    vol = _VOLUME_RE.search(text)
    num = _NUMBER_RE.search(text)
    return (vol.group(1) if vol else None), (num.group(1) if num else None)


# --------------------------------------------------------------------------------------------------
# titles.asp: search results and form options
# --------------------------------------------------------------------------------------------------


def _is_titles_page(tree: LexborHTMLParser) -> bool:
    if tree.css_first("table#restab") is not None and tree.css_first('a[href*="title_about"]') is not None:
        return True
    if tree.css_first('select[name="wos"]') is not None and tree.css_first('select[name="vak"]') is not None:
        return True
    return "КАТАЛОГ ЖУРНАЛОВ" in _body_text(tree)


def _column_key(label: str) -> str:
    low = label.lower().strip()
    if low in ("№", "no", "n"):
        return "position"
    if low.startswith("журнал") or low.startswith("название"):
        return "journal"
    if low.startswith("вып"):
        return "issues"
    if low.startswith("публ") or low.startswith("стат"):
        return "articles"
    if low.startswith("цит"):
        return "citations"
    if not low:
        return "skip"
    return "extra"


_DEFAULT_COLUMNS = ["position", "skip", "journal", "issues", "articles", "citations"]


def _parse_hit(tr: LexborNode, jid: int, keys: list[str], labels: list[str], tips: list[str]) -> JournalHit:
    cells = _cells(tr)
    if len(keys) != len(cells):
        keys = _DEFAULT_COLUMNS[: len(cells)] + ["extra"] * max(0, len(cells) - len(_DEFAULT_COLUMNS))
        labels = labels if len(labels) == len(cells) else [""] * len(cells)
        tips = tips if len(tips) == len(cells) else [""] * len(cells)
    data: dict[str, Any] = {"id": jid, "name": "", "extra": {}}
    for i, cell in enumerate(cells):
        key = keys[i]
        text = node_text(cell)
        if key == "skip":
            continue
        if key == "position":
            data["position"] = to_int(text)
        elif key == "journal":
            link = cell.css_first('a[href*="title_about"]') or cell.css_first("a")
            name = node_text(link) if link is not None else text
            data["name"] = name
            pub = " ".join(node_text(f) for f in _children(cell, "font")).strip()
            if not pub and name and text.startswith(name):
                pub = text[len(name):].strip()
            data["publisher"] = pub or None
            m = _ISSN_RE.search(text)
            if m:
                data["issn"] = m.group(0).upper().replace("Х", "X")
        elif key in ("issues", "articles", "citations"):
            data[key] = to_int(text)
            if key == "issues":
                a = cell.css_first('a[href*="contents.asp"]')
                if a is not None:
                    data["contents_url"] = abs_url(a.attributes.get("href")) or ""
            if key == "articles":
                a = cell.css_first('a[href*="title_items.asp"]')
                if a is not None:
                    data["items_url"] = abs_url(a.attributes.get("href")) or ""
            if key == "citations":
                data["citations_kind"] = tips[i] or None
        else:
            label = labels[i] or f"column {i + 1}"
            if text:
                data["extra"][label] = text
    if not data["name"]:
        link = tr.css_first('a[href*="title_about"]')
        data["name"] = node_text(link) if link is not None else ""
    return JournalHit.model_validate(data)


def _echoed_query(tree: LexborHTMLParser) -> dict[str, Any]:
    """The query the result page echoes in its form, in JournalQuery terms."""
    state: dict[str, Any] = {}
    inp = tree.css_first('input[name="titlename"]')
    if inp is not None and (inp.attributes.get("value") or "").strip():
        state["name"] = clean(inp.attributes.get("value"))
    for field in SEARCH_SELECTS:
        val = _selected_value(tree.css_first(f'select[name="{field}"]'))
        if val:
            state[_SELECT_ATTRS[field]] = val
    for name, attr in _CHECKBOX_FIELDS:
        cb = tree.css_first(f'input[name="{name}"]')
        if cb is not None and "checked" in cb.attributes:
            state[attr] = True
    pagenum = tree.css_first('input[name="pagenum"]')
    pn = to_int(pagenum.attributes.get("value")) if pagenum is not None else None
    if pn and pn > 1:
        state["page"] = pn
    try:
        q = JournalQuery.model_validate(state)
    except ValidationError:
        return state
    if q.order is not None and q.order == q.model_copy(update={"order": None}).effective_order:
        q = q.model_copy(update={"order": None})
    return q.model_dump(exclude_defaults=True)


def parse_journal_search(
    html: str | bytes, *, page: int | None = None, query: JournalQuery | dict[str, Any] | None = None
) -> SearchPage[JournalHit]:
    """Parse a ``titles.asp`` result page into a :class:`SearchPage` of :class:`JournalHit`.

    ``query`` (the query that produced the page) is stored in ``SearchPage.query``; when omitted, the
    query echoed by the page's form is used. Raises ``ParseError`` when the page is not the catalog."""
    tree = parse_html(html)
    remove_noise(tree)
    if not _is_titles_page(tree):
        raise ParseError("Страница не похожа на каталог журналов eLIBRARY.RU (titles.asp)")

    total = total_db = shown_from = shown_to = None
    counter = next((t for t in (node_text(td) for td in tree.css("td.redref")) if "найдено журналов" in t.lower()), None)
    if counter is None:
        counter = _body_text(tree)
    m = _TOTAL_RE.search(counter)
    if m:
        total, total_db = to_int(m.group(1)), to_int(m.group(2))
    else:
        m = _TOTAL_ONLY_RE.search(counter)
        total = to_int(m.group(1)) if m else None
    m = _SHOWN_RE.search(counter)
    if m:
        shown_from, shown_to = to_int(m.group(1)), to_int(m.group(2))

    items: list[JournalHit] = []
    table = tree.css_first("table#restab")
    if table is not None:
        rows = _rows(table)
        keys: list[str] = []
        labels: list[str] = []
        tips: list[str] = []
        for tr in rows:
            if _HIT_ROW_RE.match(tr.attributes.get("id") or ""):
                break
            cells = _cells(tr)
            if len(cells) >= 3 and any(_column_key(node_text(c)) == "journal" for c in cells):
                labels = [node_text(c) for c in cells]
                keys = [_column_key(lbl) for lbl in labels]
                tips = []
                for c in cells:
                    span = c.css_first("span[title]")
                    tips.append(clean(span.attributes.get("title")) if span is not None else "")
                break
        for tr in rows:
            m = _HIT_ROW_RE.match(tr.attributes.get("id") or "")
            if not m:
                continue
            try:
                items.append(_parse_hit(tr, int(m.group(1)), keys, labels, tips))
            except ValidationError as exc:  # pragma: no cover - defensive
                log.warning("skipping journal row %s: %s", m.group(1), exc)

    if total is None and table is None:
        txt = _body_text(tree).lower()
        if "не найден" in txt or "ничего не найдено" in txt:
            total = 0

    last_page = None
    for a in tree.css('a[href^="javascript:goto_page("]'):
        m = _GOTO_RE.search(a.attributes.get("href") or "")
        if m:
            last_page = max(last_page or 0, int(m.group(1)))

    per_page = None
    if shown_from and shown_to and total is not None and shown_to < total:
        per_page = shown_to - shown_from + 1

    cur_page = page
    if cur_page is None and shown_from and per_page:
        cur_page = (shown_from - 1) // per_page + 1
    if cur_page is None:
        pagenum = tree.css_first('input[name="pagenum"]')
        cur_page = to_int(pagenum.attributes.get("value")) if pagenum is not None else None
    cur_page = cur_page or 1
    if per_page is None and cur_page > 1 and shown_from:
        per_page = (shown_from - 1) // (cur_page - 1) or None

    pages = None
    if last_page:
        pages = max(last_page, cur_page)
    elif total is not None and per_page:
        pages = max(1, -(-total // per_page))
    elif total is not None and (total == 0 or (shown_to is not None and shown_to >= total)):
        pages = 1 if total else 0

    if isinstance(query, JournalQuery):
        qdict = query.model_dump(exclude_defaults=True)
    elif isinstance(query, dict):
        qdict = dict(query)
    else:
        qdict = _echoed_query(tree)

    return SearchPage[JournalHit](
        items=items,
        total=total,
        total_in_database=total_db,
        page=cur_page,
        per_page=per_page,
        pages=pages,
        query=qdict,
    )


def parse_journal_search_options(html: str | bytes) -> dict[str, list[dict[str, Any]]]:
    """All select options (and checkbox labels) of the ``titles.asp`` form, in the format of
    ``data/journals_options.json``: ``{"field": [{"value", "label", "count"?}, ...]}``."""
    tree = parse_html(html)
    remove_noise(tree)
    out: dict[str, list[dict[str, Any]]] = {}
    for field in SEARCH_SELECTS:
        sel = tree.css_first(f'select[name="{field}"]')
        if sel is None:
            continue
        opts: list[dict[str, Any]] = []
        for o in sel.css("option"):
            value = o.attributes.get("value") or ""
            if value == "":
                continue
            label, count = _split_count(o.text())
            item: dict[str, Any] = {"value": value, "label": label}
            if count is not None:
                item["count"] = count
            opts.append(item)
        out[field] = opts
    boxes: list[dict[str, Any]] = []
    for name, _attr in _CHECKBOX_FIELDS:
        cb = tree.css_first(f'input[name="{name}"]')
        if cb is None:
            continue
        td = _ancestor(cb, "td")
        label_td = _next_element(td, "td") if td is not None else None
        label, count = _split_count(node_text(label_td).lstrip("-– ").strip()) if label_td is not None else (name, None)
        item = {"value": name, "label": label}
        if count is not None:
            item["count"] = count
        boxes.append(item)
    if boxes:
        out["checkboxes"] = boxes
    if not out:
        raise ParseError("На странице нет формы поиска журналов (titles.asp)")
    return out


# --------------------------------------------------------------------------------------------------
# title_about_new.asp: journal profile
# --------------------------------------------------------------------------------------------------

_PROFILE_LABELS: dict[str, tuple[str, str]] = {
    "тип сериального издания": ("serial_type", "str"),
    "элементы сериального издания": ("serial_elements", "str"),
    "назначение издания": ("purpose", "str"),
    "способ распространения": ("distribution", "str"),
    "доступ к полным текстам": ("fulltext_access", "str"),
    "основной источник финансирования": ("funding", "str"),
    "мультидисциплинарность": ("multidisciplinary", "multi"),
    "язык публикаций": ("languages", "list"),
    "issn печатной версии": ("issn", "issn"),
    "issn": ("issn", "issn"),
    "issn электронной версии": ("eissn", "issn"),
    "issn онлайновой версии": ("eissn", "issn"),
    "eissn": ("eissn", "issn"),
    "число выпусков в год": ("issues_per_year", "int"),
    "год основания": ("foundation_year", "int"),
    "число статей в выпуске": ("articles_per_issue", "int"),
    "число страниц в выпуске": ("pages_per_issue", "int"),
    "период выпуска": ("publication_period", "str"),
    "архив на elibrary.ru": ("archive", "str"),
    "всего статей на elibrary.ru": ("articles_total", "int"),
    "всего выпусков на elibrary.ru": ("issues_total", "int"),
    "свидетельство о регистрации сми": ("media_registration", "str"),
    "страна": ("country", "str"),
    "город": ("city", "str"),
    "адрес": ("address", "str"),
    "email": ("email", "str"),
    "e-mail": ("email", "str"),
    "телефон": ("phone", "str"),
    "сайт": ("website", "url"),
}

_INDEXING_LABELS: dict[str, tuple[str, str]] = {
    "elibrary.ru": ("elibrary", "bool"),
    "ринц": ("risc", "bool"),
    "rsci": ("rsci", "bool"),
    "ядро ринц": ("core_rinc", "bool"),
    "doaj": ("doaj", "bool"),
    "web of science": ("wos", "bool"),
    "квартиль в web of science": ("wos_quartile", "int"),
    "scopus": ("scopus", "bool"),
    "квартиль в scopus": ("scopus_quartile", "int"),
    "белый список": ("white_list", "bool"),
    "уровень в белом списке": ("white_list_level", "int"),
    "егпни": ("egpni", "bool"),
    "уровень в егпни": ("egpni_level", "int"),
    "перечень вак рф": ("vak", "bool"),
    "перечень вак": ("vak", "bool"),
    "категория в перечне вак": ("vak_category", "int"),
    "рейтинг science index": ("science_index", "bool"),
    "процентиль в рейтинге science index": ("science_index_percentile", "int"),
    "crossref": ("crossref", "bool"),
    "префикс doi": ("doi_prefix", "str"),
    "базы данных": ("databases", "list"),
}

_REVIEW_LABELS: dict[str, tuple[str, str]] = {
    "вид": ("type", "str"),
    "число рецензентов": ("reviewers", "int"),
    "доля отклоненных рукописей": ("rejection_rate", "float"),
    "срок публикации": ("publication_time", "str"),
    "рецензирование осуществляется": ("performed_by", "str"),
}


def _profile_sections(tree: LexborHTMLParser) -> list[tuple[str, LexborNode]]:
    """``(HEADING, content cell)`` for every block of the profile, in page order."""
    out: list[tuple[str, LexborNode]] = []
    seen: set[int] = set()
    for f in tree.css("font"):
        if (f.attributes.get("color") or "").lower() != "#000000":
            continue
        heading = node_text(f)
        if not heading.endswith(":") or len(heading) < 3 or heading != heading.upper():
            continue
        if not re.search(r"[А-ЯЁA-Z]", heading):
            continue
        tr = _ancestor(f, "tr")
        nxt = _next_element(tr, "tr") if tr is not None else None
        if nxt is None:
            continue
        cells = _cells(nxt)
        if not cells or cells[-1].mem_id in seen:
            continue
        seen.add(cells[-1].mem_id)
        out.append((heading[:-1].strip(), cells[-1]))
    return out


def _pairs(content: LexborNode) -> list[tuple[str, str, LexborNode]]:
    """``Label: value`` pairs of the leaf cells of a block."""
    out: list[tuple[str, str, LexborNode]] = []
    cells = [td for td in content.css("td") if not _has_desc(td, "td")]
    for td in cells or [content]:
        text = node_text(td)
        m = _PAIR_RE.match(text)
        if m and clean(m.group(1)):
            out.append((clean(m.group(1)), clean(m.group(2)), td))
    return out


def strip_emails(text: str | None) -> str:
    """``text`` without e-mail addresses (and the separators left dangling by their removal).
    Paragraph breaks (``\n``) are kept."""
    if not text or "@" not in text:
        return text or ""
    lines = []
    for line in text.split("\n"):
        if "@" in line:
            line = _EMAIL_RE.sub("", line)
            line = re.sub(r"\(\s*\)", "", line)
            line = re.sub(r"\s+([,;.)])", r"\1", line)
            line = clean(re.sub(r"[:,;]+\s*(?=[.)]|$)", "", line.rstrip()))
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _paragraphs(content: LexborNode) -> str:
    paras = [node_text(p) for p in content.css("p")]
    paras = [p for p in paras if p]
    return "\n\n".join(paras) if paras else node_text(content)


def _org_ref(font: LexborNode) -> JournalOrgRef | None:
    name = node_text(font)
    if not name:
        return None
    a = font.css_first('a[href*="org_profile"]')
    href = a.attributes.get("href") if a is not None else None
    return JournalOrgRef(id=int_param(href, "id"), name=name, url=abs_url(href) if href else None)


def _city(text: str) -> str | None:
    m = re.search(r"\(([^()]+)\)", text)
    return clean(m.group(1)) if m else None


def _parse_orgs(content: LexborNode) -> list[JournalOrgRef]:
    orgs: list[JournalOrgRef] = []
    last_was_org = False
    for node in content.iter(include_text=True):
        if node.tag == "font":
            ref = _org_ref(node)
            if ref is not None:
                orgs.append(ref)
                last_was_org = True
        elif node.tag in ("-text", "i") and orgs and last_was_org:
            city = _city(node.text(deep=True))
            if city:
                orgs[-1].city = city
                last_was_org = False
        elif node.tag == "br":
            continue
        elif node.is_element_node:
            last_was_org = False
    return orgs


def _is_person_marker(node: LexborNode | None) -> bool:
    if node is None:
        return False
    if node.tag == "a" and (node.attributes.get("href") or "").lower().startswith("mailto:"):
        return True
    return node.tag == "img" and "about_author" in (node.attributes.get("src") or "")


def _parse_people(content: LexborNode, role: str | None = None) -> list[EditorialMember]:
    """People of an editorial block. The envelope link (``mailto:``) only marks a person; the
    address itself is personal data and is not kept."""
    members: list[EditorialMember] = []
    current: EditorialMember | None = None
    last_org: JournalOrgRef | None = None
    new_block = True
    for node in content.iter(include_text=True):
        tag = node.tag
        if tag == "div":
            new_block = True
            last_org = None
            continue
        if tag == "font":
            name = strip_emails(node_text(node))  # an address written out as text is not kept either
            if not name:
                continue
            author = node.css_first('a[href*="author_profile"]')
            org = node.css_first('a[href*="org_profile"]')
            marker = _is_person_marker(_next_element(node))
            if author is not None or (org is None and (new_block or marker or current is None)):
                href = author.attributes.get("href") if author is not None else None
                current = EditorialMember(
                    name=name,
                    role=role,
                    author_id=int_param(href, "authorid") or int_param(href, "id"),
                    url=abs_url(href) if href else None,
                )
                members.append(current)
                new_block = False
                last_org = None
            elif current is not None:
                ref = _org_ref(node)
                if ref is not None:
                    current.affiliations.append(ref)
                    last_org = ref
        elif tag == "img" and current is not None:
            if "about_author" in (node.attributes.get("src") or ""):
                current.degree = strip_emails(clean(node.attributes.get("title"))) or None
        elif tag in ("-text", "i") and last_org is not None:
            city = _city(node.text(deep=True))
            if city:
                last_org.city = city
                last_org = None
    return members


def _parse_rubrics(content: LexborNode) -> list[Rubric]:
    out: list[Rubric] = []
    for tr in content.css("tr"):
        if _has_desc(tr, "tr"):
            continue
        texts = [t for t in (node_text(c) for c in _cells(tr)) if t]
        if not texts:
            continue
        if len(texts) >= 2 and re.fullmatch(r"[\dA-Za-z.\-]+", texts[0]):
            out.append(Rubric(code=texts[0].rstrip("."), name=texts[-1]))
            continue
        m = re.match(r"^([\d.]+)\s+(.+)$", " ".join(texts))
        if m:
            out.append(Rubric(code=m.group(1).rstrip("."), name=m.group(2)))
        else:
            out.append(Rubric(name=" ".join(texts)))
    return out


def _parse_sections(content: LexborNode) -> list[JournalSection]:
    out: list[JournalSection] = []
    links = content.css('a[href*="title_items.asp"]')
    if links:
        for a in links:
            href = a.attributes.get("href")
            name = node_text(a)
            if name:
                out.append(JournalSection(id=int_param(href, "section"), name=name, url=abs_url(href)))
        return out
    for td in content.css("td"):
        if not _has_desc(td, "td") and node_text(td):
            out.append(JournalSection(name=node_text(td)))
    return out


def _parse_files(content: LexborNode) -> list[JournalFile]:
    out: list[JournalFile] = []
    for td in content.css("td"):
        if _has_desc(td, "td"):
            continue
        text = node_text(td)
        if not text:
            continue
        a = td.css_first("a")
        name = node_text(a) if a is not None else text
        kind = None
        m = _PAIR_RE.match(text)
        if m:
            kind = clean(m.group(1))
            name = name if a is not None else clean(m.group(2))
        fid = None
        if a is not None:
            fm = re.search(r"show_titlefile\((\d+)\)", a.attributes.get("href") or "")
            fid = int(fm.group(1)) if fm else None
        out.append(JournalFile(kind=kind, name=name, file_id=fid))
    return out


def _open_access(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    has_open = "открыт" in t or "свободн" in t or "бесплатн" in t
    has_paid = re.search(r"(?<!бес)платн", t) is not None or "подписк" in t
    if (has_open and has_paid) or "частично" in t:
        return "partial"
    if has_open:
        return "open"
    if has_paid:
        return "subscription"
    return None


def _convert(kind: str, value: str, td: LexborNode | None = None) -> Any:
    if kind == "int":
        return to_int(value)
    if kind == "float":
        return to_float(value)
    if kind == "bool":
        return yes_no(value)
    if kind == "list":
        return _split_outside_parens(value)
    if kind == "issn":
        m = _ISSN_RE.search(value)
        return m.group(0).upper() if m else (value or None)
    if kind == "multi":
        low = value.lower()
        if low.startswith("не "):
            return False
        if "является" in low or "мультидисциплинар" in low:
            return True
        return yes_no(value)
    if kind == "url" and td is not None:
        a = td.css_first('a[href^="http"]')
        if a is not None:
            return a.attributes.get("href")
    return value or None


def _indicator_rows(tree: LexborHTMLParser) -> list[tuple[str | None, str, str]]:
    """``(table heading, indicator name, value)`` of tables headed 'Название показателя'."""
    out: list[tuple[str | None, str, str]] = []
    for table in tree.css("table"):
        rows = _rows(table)
        header_idx = name_col = None
        value_cols: list[str] = []
        for i, tr in enumerate(rows[:4]):
            cells = _cells(tr)
            texts = [node_text(c) if c.css_first("table") is None else "" for c in cells]
            for j, t in enumerate(texts):
                if t.lower().startswith("название показателя"):
                    header_idx, name_col = i, j
                    value_cols = texts[j + 1:]
                    break
            if header_idx is not None:
                break
        if header_idx is None or name_col is None:
            continue
        heading = _table_heading(table)
        for tr in rows[header_idx + 1:]:
            cells = _cells(tr)
            if len(cells) < 2:
                continue
            offset = name_col if len(cells) > name_col + 1 else len(cells) - 2
            label = node_text(cells[offset])
            vals = [node_text(c) for c in cells[offset + 1:]]
            if not label or not any(vals):
                continue
            if len(vals) == 1 or len(value_cols) <= 1:
                out.append((heading, label, " | ".join(v for v in vals if v)))
            else:
                for col, v in zip(value_cols, vals, strict=False):
                    if v:
                        out.append((heading, f"{label} ({col})" if col else label, v))
    return out


def _table_heading(table: LexborNode) -> str | None:
    node = table.prev
    steps = 0
    while node is not None and steps < 8:
        steps += 1
        if node.is_element_node:
            txt = node_text(node)
            if txt:
                return txt if len(txt) <= 150 else None
        node = node.prev
    return None


def _apply_indicators(rows: list[tuple[str | None, str, str]], target: dict[str, Any]) -> dict[str, str]:
    """Fill typed indicator fields into ``target`` (first match wins) and return the metrics dict."""
    metrics: dict[str, str] = {}
    for heading, label, value in rows:
        key = label if label not in metrics else f"{heading}: {label}" if heading else label
        if key in metrics:
            continue
        metrics[key] = value
        low = label.lower()
        for field, (pattern, kind) in INDICATOR_FIELDS.items():
            if field in target or not re.search(pattern, low):
                continue
            target[field] = to_int(value) if kind == "int" else to_float(value)
    return metrics


def _profile_id(tree: LexborHTMLParser, url: str | None) -> int | None:
    for td in tree.css("td"):
        if _has_desc(td, "td"):
            continue
        txt = node_text(td)
        if txt.startswith("eLIBRARY ID:"):
            a = td.css_first("a")
            jid = int_param(a.attributes.get("href"), "id") if a is not None else None
            return jid or to_int(txt.split(":", 1)[1])
    jid = int_param(url, "id")
    if jid:
        return jid
    for sel, param in (('a[href*="contents.asp?titleid="]', "titleid"), ('a[href*="title_items.asp?id="]', "id")):
        a = tree.css_first(sel)
        if a is not None:
            return int_param(a.attributes.get("href"), param)
    return None


def parse_journal_profile(html: str | bytes, *, url: str | None = None) -> JournalProfile:
    """Parse ``title_about_new.asp?id=ID`` into a :class:`JournalProfile`.

    Raises ``NotFound`` for a "journal not found" page and ``ParseError`` for an unrecognised page."""
    tree = parse_html(html)
    remove_noise(tree)
    sections = _profile_sections(tree)
    title = _page_title(tree)
    big = tree.css_first("span.bigtext")
    heading_name = node_text(big) if big is not None else ""
    name = title.rpartition(" / ")[0].strip() if " / " in title else ""
    name = name or heading_name
    jid = _profile_id(tree, url)
    if jid is None or not name or not sections:
        body = _body_text(tree).lower()
        if "не найден" in body or "не существует" in body:
            raise NotFound("Журнал не найден на eLIBRARY.RU", url=url)
        raise ParseError("Страница не похожа на карточку журнала eLIBRARY.RU (title_about_new.asp)", url=url)

    data: dict[str, Any] = {"id": jid, "name": name}
    extra: dict[str, str] = {}
    indexing: dict[str, Any] = {}
    indexing_extra: dict[str, str] = {}

    # "Язык описания: русский, английский" (the other languages are links, e.g. &langid=2)
    english = bool(url and "langid=2" in url)
    for td in tree.css("td"):
        if _has_desc(td, "td"):
            continue
        txt = node_text(td)
        if txt.startswith("Язык описания:"):
            data["description_languages"] = _split_outside_parens(txt.split(":", 1)[1])
            linked = {node_text(a).lower() for a in td.css("a")}
            for a in td.css("a"):
                href = a.attributes.get("href") or ""
                if "langid=2" in href:
                    data["english_url"] = abs_url(href)
            current = [lang for lang in data["description_languages"] if lang.lower() not in linked]
            if current and current[0].lower().startswith("англ"):
                english = True
            break
    if english:
        data["name_en"] = name

    staff: dict[str, list[EditorialMember]] = {}
    other_rubrics: dict[str, list[Rubric]] = {}
    for heading, content in sections:
        h = heading.upper()
        if "ПОЛИТИКА" in h:
            data["editorial_policy"] = strip_emails(_paragraphs(content)) or None
        elif h.startswith("ИСТОРИЯ"):
            data["history"] = strip_emails(_paragraphs(content)) or None
        elif h.startswith("УЧРЕДИТЕЛ"):
            data["founders"] = _parse_orgs(content)
        elif h.startswith("ИЗДАТЕЛ"):
            data["publishers"] = _parse_orgs(content)
        elif h.startswith("МЕСТО РАСПОЛОЖЕНИЯ"):
            data["editorial_office"] = _parse_orgs(content)
        elif (
            any(k in h for k in ("РЕДАКТОР", "СЕКРЕТАР", "КОЛЛЕГИЯ", "СОВЕТ", "РЕДАКЦИЯ"))
            or content.css_first('a[href*="author_profile"]') is not None
            or content.css_first('img[src*="about_author"]') is not None
        ):
            people = _parse_people(content, role=heading)
            if h.startswith("ГЛАВН") and "РЕДАКТОР" in h and people:
                data["editor_in_chief"] = people[0]
                if len(people) > 1:
                    staff[heading] = people[1:]
            elif h.startswith("ЗАМЕСТИТЕЛ"):
                data["deputy_editors"] = people
            elif "СЕКРЕТАР" in h:
                data["executive_secretaries"] = people
            elif h == "РЕДАКЦИОННАЯ КОЛЛЕГИЯ":
                data["editorial_board"] = people
            else:
                staff[heading] = people
        elif h.startswith("РУБРИКИ") or h.startswith("СПЕЦИАЛЬНОСТИ") or "КЛАССИФИКАЦ" in h:
            rubrics = _parse_rubrics(content)
            if "ГРНТИ" in h:
                data["rubrics_grnti"] = rubrics
            elif "OECD" in h:
                data["rubrics_oecd"] = rubrics
            elif "ВАК" in h:
                data["vak_specialties"] = rubrics
            else:
                other_rubrics[heading] = rubrics
        elif "РАЗДЕЛ" in h:
            data["sections"] = _parse_sections(content)
        elif "ФАЙЛ" in h:
            data["files"] = _parse_files(content)
        elif h.startswith("РЕЦЕНЗИРОВАНИЕ"):
            review: dict[str, Any] = {"extra": {}}
            for label, value, _td in _pairs(content):
                spec = _REVIEW_LABELS.get(label.lower())
                if spec:
                    review[spec[0]] = _convert(spec[1], value)
                elif v := strip_emails(value):
                    review["extra"][label] = v
            pt = review.get("publication_time")
            if pt and re.search(r"дн", pt):
                review["publication_days"] = to_int(pt)
            data["peer_review"] = PeerReview.model_validate(review)
        elif h.startswith("ИНДЕКСАЦИЯ"):
            for label, value, td in _pairs(content):
                spec = _INDEXING_LABELS.get(label.lower())
                if spec is None:
                    if v := strip_emails(value):
                        indexing_extra[label] = v
                    continue
                indexing[spec[0]] = _convert(spec[1], value, td)
                if spec[0] == "elibrary":
                    m = re.search(r"договор\s*([^\s)]+)", value)
                    if m:
                        indexing["elibrary_contract"] = m.group(1)
        else:
            pairs = _pairs(content)
            if not pairs:
                text = strip_emails(_paragraphs(content))
                if text:
                    extra[heading] = text
                continue
            contacts = "КОНТАКТ" in h
            for label, value, td in pairs:
                spec = _PROFILE_LABELS.get(label.lower())
                if spec is not None and spec[0] == "email" and not contacts:
                    continue  # an e-mail outside the contact block may be a person's: not kept
                if spec is None:
                    if v := strip_emails(value):
                        extra[label] = v
                elif spec[0] not in data:
                    data[spec[0]] = _convert(spec[1], value, td)

    if staff:
        data["other_staff"] = staff
    if other_rubrics:
        data["other_rubrics"] = other_rubrics
    indexing["extra"] = indexing_extra
    data["indexing"] = JournalIndexingInfo.model_validate(indexing)
    data["open_access"] = _open_access(data.get("fulltext_access"))
    period = data.get("publication_period")
    if period:
        if re.search(r"(\.\.\.|…)\s*$", period):
            data["active"] = True
        elif re.search(r"\d{4}\s*[-–]\s*\d{4}\s*$", period):
            data["active"] = False
    if data.get("publishers"):
        data["publisher"] = data["publishers"][0].name

    body = _body_text(tree)
    m = re.search(r"Дата последнего изменения данных об издании:\s*(\d{2}\.\d{2}\.\d{4})", body)
    if m:
        data["last_modified"] = m.group(1)

    latest = tree.css_first('a[href*="contents.asp?id="]')
    if latest is not None:
        href = latest.attributes.get("href")
        label = node_text(latest)
        vol, num = split_issue_label(label)
        data["latest_issue"] = IssueRef(id=int_param(href, "id"), label=label or "", volume=vol, number=num,
                                        url=abs_url(href))

    rows = _indicator_rows(tree)
    if rows:
        typed: dict[str, Any] = {}
        data["metrics"] = _apply_indicators(rows, typed)
        for f in ("impact_factor_2y", "impact_factor_5y", "science_index_rating", "h_index"):
            if typed.get(f) is not None:
                data[f] = typed[f]

    data["extra"] = extra
    if url:
        data["url"] = url
    return JournalProfile.model_validate(data)


# --------------------------------------------------------------------------------------------------
# title_profile.asp: indicators (no fixture: parsed defensively)
# --------------------------------------------------------------------------------------------------


def parse_journal_indicators(html: str | bytes, *, url: str | None = None) -> JournalIndicators:
    """Parse ``title_profile.asp?id=ID`` (publication-activity analysis of a journal) into
    :class:`JournalIndicators`. Every row of the 'Название показателя | Значение' tables goes to
    ``metrics``; well-known rows (impact factors, Science Index, h-index) also to typed fields."""
    tree = parse_html(html)
    remove_noise(tree)
    rows = _indicator_rows(tree)
    if not rows:
        raise ParseError("На странице нет таблицы показателей журнала (title_profile.asp)", url=url)
    data: dict[str, Any] = {}
    data["metrics"] = _apply_indicators(rows, data)
    jid = int_param(url, "id")
    name = None
    a = tree.css_first('a[href*="title_about"]')
    if a is not None:
        jid = jid or int_param(a.attributes.get("href"), "id")
        name = node_text(a) or None
    title = _page_title(tree)
    if " / " in title:
        name = title.rpartition(" / ")[0].strip() or name
    data["journal_id"] = jid
    data["journal_name"] = name
    year = None
    for sel in tree.css("select"):
        val = _selected_value(sel)
        if val and re.fullmatch(r"(19|20)\d\d", val) and "year" in (sel.attributes.get("name") or "").lower():
            year = int(val)
            break
    if year is None:
        for heading, _label, _value in rows:
            m = re.search(r"\bза\s+((?:19|20)\d\d)\s+год", heading or "", re.I)
            if m:
                year = int(m.group(1))
                break
    if year is None:
        m = re.search(r"показател\w*[^.]{0,60}?\bза\s+((?:19|20)\d\d)\s+год", _body_text(tree), re.I)
        if m:
            year = int(m.group(1))
    data["year"] = year
    data["url"] = url
    return JournalIndicators.model_validate(data)


# --------------------------------------------------------------------------------------------------
# contents.asp: issue contents
# --------------------------------------------------------------------------------------------------


def _issue_ref_from_row(tr: LexborNode, year: int | None, current_id: int | None) -> IssueRef | None:
    cells = _cells(tr)
    cell = cells[-1] if cells else tr
    text = node_text(cell)
    a = cell.css_first('a[href*="contents.asp?id="]')
    m_cnt = _ARTICLES_RE.search(text)
    if a is not None:
        href = a.attributes.get("href")
        label = node_text(a)
        iid = int_param(href, "id")
        current = bool(current_id and iid == current_id)
    elif m_cnt and re.search(r"№|Т\.|Вып|Online|выпуск", text, re.I):
        label = clean(text[: m_cnt.start()])
        iid, href, current = current_id, None, True
    else:
        return None
    if not label:
        return None
    vol, num = split_issue_label(label)
    return IssueRef(
        id=iid,
        year=year,
        volume=vol,
        number=num,
        label=label,
        articles=int(m_cnt.group(1)) if m_cnt else None,
        current=current,
        url=abs_url(href) if href else None,
    )


def _parse_issue_list(tree: LexborHTMLParser, current_id: int | None) -> list[IssueRef]:
    out: list[IssueRef] = []
    tbodies = tree.css('tbody[id^="issueyear"]')
    if tbodies:
        table = _ancestor(tbodies[0].parent, "table")
        if table is not None:
            for tr in table.css("tr"):
                parent = tr.parent
                year = None
                pid = (parent.attributes.get("id") or "") if parent is not None else ""
                if parent is not None and parent.tag == "tbody" and pid.lower().startswith("issueyear"):
                    year = to_int(pid[9:])
                ref = _issue_ref_from_row(tr, year, current_id)
                if ref is not None:
                    out.append(ref)
            return out
    # Fallback: any issue link outside the header.
    for a in tree.css('a[href*="contents.asp?id="]'):
        label = node_text(a)
        if not label or label.isdigit():
            continue
        href = a.attributes.get("href")
        vol, num = split_issue_label(label)
        iid = int_param(href, "id")
        out.append(IssueRef(id=iid, label=label, volume=vol, number=num, current=bool(current_id and iid == current_id),
                            url=abs_url(href)))
    return out


def parse_issue_contents(html: str | bytes, *, url: str | None = None) -> IssueContents:
    """Parse ``contents.asp?titleid=ID`` / ``contents.asp?id=ISSUEID`` into :class:`IssueContents`.

    Rows become :class:`PublicationHit` with ``section`` (the rubric heading above them), ``pages`` and
    ``kind="article"``; ``available_issues`` lists every issue of the issue list (newest year first)."""
    tree = parse_html(html)
    remove_noise(tree)
    title = _page_title(tree)
    body = _body_text(tree)
    is_contents = (
        "содержание выпуска" in title.lower()
        or "СОДЕРЖАНИЕ ВЫПУСКА" in body
        or tree.css_first('tbody[id^="issueyear"]') is not None
    )
    if not is_contents:
        low = body.lower()
        if "не найден" in low or "не существует" in low:
            raise NotFound("Выпуск журнала не найден на eLIBRARY.RU", url=url)
        raise ParseError("Страница не похожа на содержание выпуска журнала (contents.asp)", url=url)

    data: dict[str, Any] = {}
    extra: dict[str, str] = {}
    journal_name = None
    label = None
    m = re.match(r"^(.*)\s/\s*содержание выпуска\s*(.*)$", title, re.I)
    if m:
        journal_name = clean(m.group(1)) or None
        label = clean(m.group(2)) or None
    elif " / " in title:
        journal_name = title.rpartition(" / ")[0].strip() or None

    issue_id = None
    edn = None
    for td in tree.css("td"):
        if _has_desc(td, "td"):
            continue
        txt = node_text(td)
        if txt.startswith("eLIBRARY ID:") and issue_id is None:
            a = td.css_first("a")
            issue_id = int_param(a.attributes.get("href"), "id") if a is not None else None
            issue_id = issue_id or to_int(txt.split(":", 1)[1])
        elif txt.startswith("EDN:") and edn is None:
            edn = clean(txt.split(":", 1)[1]).upper() or None
    if issue_id is None and url and "titleid=" not in url:
        issue_id = int_param(url, "id")

    jlink = tree.css_first('a[href*="title_about"]')
    journal_id = None
    tid = tree.css_first('input[name="titleid"]')
    if tid is not None:
        journal_id = to_int(tid.attributes.get("value"))
    if jlink is not None:
        journal_id = journal_id or int_param(jlink.attributes.get("href"), "id")
        journal_name = journal_name or node_text(jlink) or None
        container = jlink.parent
        while container is not None and container.tag != "div":
            container = container.parent
        if container is not None:
            org_a = container.css_first('a[href*="org_profile"]')
            span = container.css_first("span")
            if org_a is not None:
                href = org_a.attributes.get("href")
                ital = container.css_first("i")
                data["publisher"] = JournalOrgRef(
                    id=int_param(href, "id"),
                    name=node_text(org_a),
                    city=_city(node_text(ital)) if ital is not None else None,
                    url=abs_url(href),
                )
            elif span is not None and node_text(span):
                ital = span.css_first("i")
                city = _city(node_text(ital)) if ital is not None else None
                name = node_text(span)
                if ital is not None:
                    name = clean(name.replace(node_text(ital), ""))
                data["publisher"] = JournalOrgRef(name=name, city=city)
    if journal_id is None and url:
        journal_id = int_param(url, "titleid")

    year = volume = number = None
    for div in tree.css("div"):
        txt = node_text(div)
        if txt and _ISSUE_HEADER_RE.match(txt):
            for key, value in _ISSUE_HEADER_PAIR_RE.findall(txt):
                if key == "Год":
                    year = to_int(value)
                elif key == "Том":
                    volume = value
                elif key in ("Номер", "Выпуск"):
                    number = value
                else:
                    extra[key] = value
            break
    if label and (volume is None or number is None):
        v, n = split_issue_label(label)
        volume = volume or v
        number = number or n
    if label and year is None:
        m = re.search(r"за\s+((?:19|20)\d\d)\s+год", label)
        year = int(m.group(1)) if m else None

    items = parse_publication_rows(tree)
    for i, hit in enumerate(items):
        if hit.kind is None:
            hit.kind = "article"
        if hit.year is None:
            hit.year = year
        if hit.position is None:
            hit.position = i + 1
    sections: list[str] = []
    table = tree.css_first("table#restab")
    if table is not None:
        for tr in table.css("tr"):
            tds = tr.css("td")
            if len(tds) == 1 and tds[0].attributes.get("colspan") and tds[0].css_first("b") is not None:
                heading = node_text(tds[0])
                if heading and heading not in sections:
                    sections.append(heading)
    for hit in items:
        if hit.section and hit.section not in sections:
            sections.append(hit.section)

    issues = _parse_issue_list(tree, issue_id)
    current = next((r for r in issues if r.current), None)

    m = re.search(r"Приобрести этот выпуск\s*за\s*(\d[\d\s]*руб\.?)", body)
    if m:
        extra["Стоимость выпуска"] = clean(m.group(1))
    subs = tree.css_first('select[name="subs_year"]')
    if subs is not None:
        offers = [clean(o.text()) for o in subs.css("option") if clean(o.text())]
        if offers:
            extra["Подписка на журнал"] = "; ".join(offers)

    data.update(
        journal_id=journal_id,
        journal_name=journal_name,
        issue_id=issue_id,
        edn=edn,
        label=label or (current.label if current else None),
        year=year,
        volume=volume,
        number=number,
        articles_count=current.articles if current else None,
        sections=sections,
        items=items,
        available_issues=issues,
        extra=extra,
        url=issue_contents_url(issue_id) if issue_id else (url or None),
    )
    return IssueContents.model_validate(data)


def journal_id_from_url(url: str | None) -> int | None:
    """Journal id of a journal-page URL (``title_about_new.asp?id=``, ``contents.asp?titleid=``,
    ``title_profile.asp?id=``, ``title_items.asp?id=``)."""
    if not url:
        return None
    low = url.lower()
    if "titleid=" in low:
        return to_int(query_param(url, "titleid"))
    if any(p in low for p in ("title_about", "title_profile", "title_items")):
        return to_int(query_param(url, "id"))
    return None


__all__ = [
    "SEARCH_SELECTS",
    "journal_id_from_url",
    "parse_issue_contents",
    "parse_journal_indicators",
    "parse_journal_profile",
    "parse_journal_search",
    "parse_journal_search_options",
    "split_issue_label",
    "strip_emails",
]
