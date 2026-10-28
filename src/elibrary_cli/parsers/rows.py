"""Parser for publication rows, shared by search results, author/org publication lists, journal
issue contents and the table of contents of books.

Row markup (2026-10)::

    <table id="restab">
      <tr id="a47430099"> | <tr id="arw91809593">            # search | author items / contents
        <td>№ ...<input type=checkbox name="tid47430099"><div id="pdf_47430099"></div></td>
        <td><a href="/item.asp?id=47430099"><b>TITLE</b></a><br>
            <font><i>Author A.A., Author B.B.</i></font><br>       # optional: chapters have none
            <font>Source line: journal, year, volume, pages / patent number ...</font></td>
        [<td>7-19</td>]                                          # pages column (issue contents, TOC)
        <td>citations</td>
      </tr>
      <tr><td colspan=4><b>SECTION HEADING</b></td></tr>       # rubric rows (issue contents)
      <tr id="brw1062348813">...</tr>                           # reference-only record (show_refs=1)

``brw`` rows are publications known only from cited-reference lists: they have no item card and no
``item.asp`` link, so :func:`parse_publication_rows` skips them (that is why author_items pages with
``show_refs=1`` can yield fewer than 100 hits per page); :func:`parse_reference_rows` returns them.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser, LexborNode

from elibrary_cli.models.base import PublicationHit
from elibrary_cli.models.publications import ReferenceHit
from elibrary_cli.parsers.common import clean, first_year, int_param, node_text, to_int

_ROW_ID_RE = re.compile(r"^(?:a|arw)(\d+)$")
_REF_ROW_ID_RE = re.compile(r"^brw(\d+)$")
_BLOCK_TAGS = frozenset({"br", "div", "p", "td", "tr", "table", "li"})
_DATE_RE = re.compile(r"(?<!\d)\d{1,2}\.\d{1,2}\.((?:18|19|20)\d\d)(?!\d)")
# page spans / page counts of a bibliographic line ("С. 1901-1909", "pp. 12-15", "P. 2010", "320 с."):
# their numbers can look like years and must not be taken for the publication year
_PAGES_RE = re.compile(
    r"(?<!\w)(?:С|C|P|PP|стр)\.\s*\d+(?:\s*[-–—]\s*\d+)?|(?<![\d.])\d+\s*(?:с|p|pp|стр)\.(?!\w)", re.I)
_VERSIONS_LABEL_RE = re.compile(r"^\s*Версии\s*:?\s*$", re.I)

# patent and thesis lines always start with their type ("Патент на изобретение RU ...",
# "Свидетельство о регистрации программы для ЭВМ RU ...", "диссертация на соискание ...",
# "автореферат диссертации ..."); anchored, so that a journal or book title such as
# "Патенты и лицензии" or "... диссертационные исследования" does not turn the row into one
_PATENT_RE = re.compile(
    r"^\s*(?:патент\b|свидетельство о (?:государственной )?регистрации|полезн\w+ модел|промышленн\w+ образ)",
    re.I)
_DISSER_RE = re.compile(r"^\s*(?:автореферат\b|диссертаци[яи]\b)", re.I)
_CHAPTER_RE = re.compile(r"^\s*В книге:", re.I)
_IN_COLLECTION_RE = re.compile(r"^\s*В сборнике:", re.I)
_CONF_RE = re.compile(
    r"конференц|симпозиум|форум|семинар|тезис|конгресс|съезд|школ[аыеи]|чтени[яй]|доклад|"
    r"conference|symposium|proceedings|workshop", re.I)
_REPORT_RE = re.compile(r"отч[её]т о (?:НИР|НИОКР)|\bотч[её]т\b", re.I)
_PREPRINT_RE = re.compile(r"депонирован|препринт", re.I)
_GRANT_RE = re.compile(r"\bгрант|РФФИ\s*№|РНФ\s*№", re.I)
_DATASET_RE = re.compile(r"набор данных|dataset", re.I)
_BOOK_RE = re.compile(
    r"монография|учебн\w* пособие|учебник|ISBN|издательство|справочник|сборник|словарь|энциклопеди|"
    r"практикум|хрестомати", re.I)
_CITY_YEAR_RE = re.compile(r"^[^,.:;\d]{2,40},\s*(?:18|19|20)\d\d\.?$")
_ARTICLE_RE = re.compile(r"№\s*\d|\bТ\.\s*\d|\bВып\.|\bС\.\s*\d|журнал|вестник|известия", re.I)


def infer_kind(source: str | None, *, journal_link: bool = False, default: str | None = None) -> str | None:
    """Best-effort publication type of a row from its bibliographic line.

    ``journal_link`` tells that the line links to a journal issue (``contents.asp``): such rows are
    journal articles even when the journal is called "Труды ... конференции". ``default`` is returned
    for rows without a source line (issue contents, book TOC).
    """
    source = clean(source)
    if not source:
        return default
    # structural markers first: a container ("В книге:", "В сборнике:") or a journal issue link
    # tells more than any word of the line
    if _CHAPTER_RE.search(source):
        return "chapter"
    if _IN_COLLECTION_RE.search(source):
        return "conference" if _CONF_RE.search(source) else "chapter"
    if not journal_link:
        if _PATENT_RE.search(source):
            return "patent"
        if _DISSER_RE.search(source):
            return "dissertation"
    if _REPORT_RE.search(source):
        return "report"
    if _PREPRINT_RE.search(source):
        return "preprint"
    if _GRANT_RE.search(source):
        return "grant"
    if _DATASET_RE.search(source):
        return "dataset"
    if journal_link:
        return "article"
    if _BOOK_RE.search(source) or _CITY_YEAR_RE.match(source):
        return "book"
    if _CONF_RE.search(source):
        return "conference"
    if _ARTICLE_RE.search(source):
        return "article"
    return "other"


def source_year(source: str | None) -> int | None:
    """Year of a bibliographic line: the last year-like number outside page spans, so that
    "Журнал физической химии. 2025. Т. 99. № 12. С. 1901-1909." gives 2025, not 1909."""
    if not source:
        return None
    return first_year(_PAGES_RE.sub(" ", source)) or first_year(source)


def row_year(source: str | None, kind: str | None = None) -> int | None:
    """Publication year of a row. Patents: the date right after the number (publication date), not
    the application date that follows it ("RU 2760636 C2, 29.11.2021. Заявка № ... от 19.12.2018.").
    Other rows: :func:`source_year` (page numbers such as "С. 1901-1909" are never the year)."""
    if not source:
        return None
    if kind == "patent":
        m = _DATE_RE.search(source)
        if m:
            return int(m.group(1))
    return source_year(source)


def _inline_text(node: LexborNode, skip: frozenset[int] = frozenset()) -> str:
    """Text of a node with spaces only where the markup has them (or at <br>/blocks).

    ``node.text(separator=" ")`` puts a space between every two text nodes, which turns
    ``<a>Journal</a>. 2024`` into ``Journal . 2024``. Descendants whose ``mem_id`` is in ``skip``
    are left out (the nested "Версии:" table of a row).
    """
    parts: list[str] = []

    def walk(n: LexborNode) -> None:
        for ch in n.iter(include_text=True):
            if ch.tag == "-text":
                parts.append(ch.text_content or "")
            elif ch.tag in ("script", "style", "input", "select"):
                continue
            elif ch.mem_id in skip:
                parts.append(" ")
            else:
                block = ch.tag in _BLOCK_TAGS
                if block:
                    parts.append(" ")
                walk(ch)
                if block:
                    parts.append(" ")

    walk(node)
    return clean("".join(parts))


def _split_authors(text: str) -> list[str]:
    text = clean(text)
    if not text:
        return []
    return [a.strip() for a in re.split(r",\s*(?=[^,]*[А-ЯЁA-Z])", text) if a.strip()]


def _cell_of(node: LexborNode | None) -> LexborNode | None:
    while node is not None and node.tag != "td":
        node = node.parent
    return node


def _versions_tables(cell: LexborNode) -> list[LexborNode]:
    """Nested ``<table>`` blocks labelled "Версии:" (other language versions of the publication,
    each a link to its own item card) inside a row's title cell."""
    out = []
    for table in cell.css("table"):
        if table.mem_id == cell.mem_id:
            continue
        first = table.css_first("td")
        if first is not None and _VERSIONS_LABEL_RE.match(node_text(first)):
            out.append(table)
    return out


def _version_ids(tables: list[LexborNode]) -> list[int]:
    ids: list[int] = []
    for table in tables:
        for a in table.css('a[href*="item.asp?id="]'):
            vid = int_param(a.attributes.get("href"), "id")
            if vid is not None and vid not in ids:
                ids.append(vid)
    return ids


def _authors_and_source(cell: LexborNode, title: str,
                        skip: frozenset[int] = frozenset()) -> tuple[list[str], str | None]:
    """Authors are the ``<i>`` right after the title; any later ``<i>`` (e.g. the book's author in
    "В книге: ... <i>Мифтахова Р.Г.</i> Монография") belongs to the source line. Nodes in ``skip``
    (the "Версии:" block) are not part of the source line."""
    full = _inline_text(cell, skip)
    start = 0
    if title:
        pos = full.find(title)
        start = pos + len(title) if pos >= 0 else 0
    rest = full[start:]
    authors: list[str] = []
    # the author list is the first <i> that starts right after the title; an <i> inside the title
    # ("AN <I>IN SILICO</I> PLATFORM", Latin species names) or the versions block is not it
    for ital in cell.css("i"):
        a_txt = _inline_text(ital)
        idx = rest.find(a_txt) if a_txt else -1
        if idx >= 0 and not rest[:idx].strip():
            authors = _split_authors(a_txt)
            rest = rest[idx + len(a_txt):]
            break
    return authors, clean(rest) or None


def parse_publication_row(tr: LexborNode, *, section: str | None = None,
                          default_kind: str | None = None) -> PublicationHit | None:
    """One ``tr#a<ID>`` / ``tr#arw<ID>`` row; ``None`` when the row has no item id."""
    m = _ROW_ID_RE.match(tr.attributes.get("id") or "")
    link = tr.css_first('a[href*="item.asp?id="]')
    item_id = int(m.group(1)) if m else (int_param(link.attributes.get("href"), "id") if link else None)
    if item_id is None:
        return None
    # the row's own cells: a "Версии:" block nests a table with more <td>s inside the title cell
    tds = [c for c in tr.iter() if c.tag in ("td", "th")] or tr.css("td")
    title = _inline_text(link) if link else ""
    authors: list[str] = []
    source = None
    journal_link = False
    versions: list[int] = []
    main_td = _cell_of(link) if link else None
    if main_td is not None:
        vtables = _versions_tables(main_td)
        skip = frozenset(t.mem_id for t in vtables)
        versions = _version_ids(vtables)
        authors, source = _authors_and_source(main_td, title, skip)
        # a journal-issue link of the row itself, not of one of its versions
        journal_link = any(_cell_of(a).mem_id == main_td.mem_id  # type: ignore[union-attr]
                           for a in main_td.css('a[href*="contents.asp"]'))
    position = None
    pages = None
    citations = None
    if tds:
        num = re.search(r"(\d+)\s*\.?", node_text(tds[0]))
        position = int(num.group(1)) if num else None
        citations = to_int(node_text(tds[-1]))
        if main_td is not None:
            idx = next((i for i, td in enumerate(tds) if td.mem_id == main_td.mem_id), None)
            if idx is not None and len(tds) - idx == 3:  # title | pages | citations
                pages = clean(node_text(tds[idx + 1])) or None
    kind = infer_kind(source, journal_link=journal_link, default=default_kind)
    return PublicationHit(
        id=item_id,
        title=title,
        authors=authors,
        source=source,
        kind=kind,
        year=row_year(source, kind),
        pages=pages,
        section=section,
        citations=citations,
        position=position,
        versions=versions,
    )


def parse_publication_rows(tree: LexborHTMLParser | LexborNode, *,
                           default_kind: str | None = None) -> list[PublicationHit]:
    """All publication rows of ``table#restab`` in document order (keeps section headings).

    ``tree`` may be a whole page or any node containing the table. ``default_kind`` is used for rows
    without a bibliographic line (journal issue contents: ``"article"``, book TOC: ``"chapter"``).
    """
    table = tree.css_first("table#restab")
    if table is None:
        return []
    out: list[PublicationHit] = []
    section: str | None = None
    for tr in table.css("tr"):
        rid = tr.attributes.get("id") or ""
        if _ROW_ID_RE.match(rid):
            hit = parse_publication_row(tr, section=section, default_kind=default_kind)
            if hit is not None:
                out.append(hit)
            continue
        if _REF_ROW_ID_RE.match(rid):
            continue
        tds = tr.css("td")
        if len(tds) == 1 and tds[0].attributes.get("colspan"):
            heading = node_text(tds[0])
            if heading and tds[0].css_first("b") is not None and not tr.css_first("input"):
                section = heading
    return out


def parse_reference_rows(tree: LexborHTMLParser | LexborNode) -> list[ReferenceHit]:
    """Reference-only rows (``tr#brw<ID>``) of ``table#restab``: publications without an eLIBRARY
    card that are known from cited-reference lists (author/org lists with ``show_refs=1``)."""
    table = tree.css_first("table#restab")
    if table is None:
        return []
    out: list[ReferenceHit] = []
    for tr in table.css("tr"):
        m = _REF_ROW_ID_RE.match(tr.attributes.get("id") or "")
        if not m:
            continue
        tds = tr.css("td")
        if len(tds) < 2:
            continue
        cell = tds[1]
        bold = cell.css_first("b")
        title = _inline_text(bold) if bold is not None else ""
        authors, source = _authors_and_source(cell, title)
        num = re.search(r"(\d+)\s*\.?", node_text(tds[0]))
        out.append(ReferenceHit(
            ref_id=int(m.group(1)),
            title=title,
            authors=authors,
            source=source,
            year=source_year(source),
            citations=to_int(node_text(tds[-1])) if len(tds) > 2 else None,
            position=int(num.group(1)) if num else None,
        ))
    return out
