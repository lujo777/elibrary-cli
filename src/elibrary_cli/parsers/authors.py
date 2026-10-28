"""Parsers of the author pages (markup of 2026-10, see ``docs/re/authors.md``).

* ``authors.asp`` (search results)::

      Всего найдено авторов: <b>4342</b> из <b>1170544</b>. Показано на данной странице: с <b>1</b> по <b>100</b>.
      <table id="restab">
        <tr id="a3771">  №. | checkbox tid3771 |
            <font><b>Петров&nbsp; Владимир&nbsp; Алексеевич</b></font><span class="aster">*</span>
            [<img data-tooltip="https://ru.wikipedia.org/...">]<br>ОРГАНИЗАЦИЯ (Город) |
            <a href="author_items.asp?authorid=3771&show_refs=1">1069</a><a href="author_profile.asp?id=3771"> |
            <a href="javascript:show_author_refs(3771, 'risc')">86835</a> | 119
        <input type=hidden name="hid3771" value="Петров В А">
      <div id="pages"> ... <font color=#F26C4F>1</font> ... <a href="javascript:goto_page(44)">В конец</a>

* ``author_profile.asp?id=ID``: header (name, SCIENCE INDEX star, organization, SPIN, AuthorID), tables
  "МЕСТО РАБОТЫ", "ОБЩИЕ ПОКАЗАТЕЛИ" (label | value), "ПОКАЗАТЕЛИ ПО ГОДАМ" (label | one cell per
  year), report links ``javascript:author_profile("key")``, external profile links (ORCID, Scopus,
  ResearcherID) and "Дата обновления показателей автора".
* ``author_items.asp?authorid=ID``: publication rows (``tr#arw<ID>``, parsed by
  :mod:`elibrary_cli.parsers.rows`) and rows known only from reference lists (``tr#brw<ID>``).
"""

from __future__ import annotations

import re
from datetime import date

from selectolax.lexbor import LexborHTMLParser, LexborNode

from elibrary_cli.errors import NotFound, ParseError
from elibrary_cli.models.authors import (
    AuthorAffiliation,
    AuthorHit,
    AuthorMetric,
    AuthorProfile,
    AuthorPublicationsPage,
    AuthorQuery,
    AuthorReferenceHit,
    AuthorReport,
    AuthorYearlyMetric,
)
from elibrary_cli.models.base import BASE_URL, SearchPage
from elibrary_cli.parsers.common import (
    abs_url,
    clean,
    int_param,
    node_text,
    parse_html,
    query_param,
    remove_noise,
    text_lines,
    to_float,
    to_int,
)
from elibrary_cli.parsers.rows import parse_publication_rows, source_year

DEFAULT_PER_PAGE = 100

_AUTHOR_ROW_RE = re.compile(r"^a(\d+)$")
_REF_ROW_RE = re.compile(r"^brw(\d+)$")
_GOTO_RE = re.compile(r"goto_page\((\d+)\)")
_TOWN_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_RANGE_RE = re.compile(r"Показано на данной странице:\s*с\s*(\d+)\s*по\s*(\d+)")
_AUTHORS_TOTAL_RE = re.compile(r"Всего найдено авторов:\s*(\d[\d\s]*?)\s*из\s*(\d[\d\s]*)")
# "Всего найдено 1094 публикации", "Всего найдена 2581 публикация", "Всего найден ..." (the verb agrees
# with the number); a colon variant "Всего найдено публикаций: N" is accepted too.
_ITEMS_TOTAL_RE = re.compile(
    r"Всего\s+найден\w*\s*:?\s*(\d[\d\s]*?)\s+публикац|Всего\s+найден\w*\s+публикац\w*\s*:\s*(\d[\d\s]*\d|\d)")
_TOTALCITED_JS_RE = re.compile(r"getElementById\(['\"]totalcited['\"]\)\.innerHTML\s*=\s*['\"]?(\d+)")
_SPIN_RE = re.compile(r"SPIN-код:\s*([\d-]+)")
_AUTHORID_RE = re.compile(r"AuthorID:\s*(\d+)")
_UPDATED_RE = re.compile(r"Дата обновления показателей автора:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})")
_REPORT_RE = re.compile(r"author_profile\(\s*['\"](\w+)['\"]")
_METRIC_VALUE_RE = re.compile(r"^(-?\d[\d\s]*(?:[.,]\d+)?)\s*(?:\(\s*(-?\d+(?:[.,]\d+)?)\s*%\s*\))?$")
_FIVE_YEARS_RE = re.compile(r"\s*\((\d{4})\s*-\s*(\d{4})\)\s*$")
_ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TITLE_NOISE = ("список публикаций", "анализ публикационной", "поиск авторов", "научная электронная")
_NOT_FOUND_RE = re.compile(r"автор\w*\s+не\s+найден|не\s+найден\w*\s+автор|не\s+существует", re.I)

#: "ОБЩИЕ ПОКАЗАТЕЛИ" label -> typed field of AuthorProfile. Labels are matched after removing a
#: trailing ":" and a trailing "(YYYY-YYYY)" (the 5-year window).
_PROFILE_FIELDS: dict[str, str] = {
    "Число публикаций на elibrary.ru": "publications_elibrary",
    "Число публикаций в РИНЦ": "publications_rinc",
    "Число публикаций, входящих в ядро РИНЦ": "publications_core",
    "Число цитирований из публикаций на elibrary.ru": "citations_elibrary",
    "Число цитирований из публикаций, входящих в РИНЦ": "citations_rinc",
    "Число цитирований из публикаций, входящих в ядро РИНЦ": "citations_core",
    "Индекс Хирша по всем публикациям на elibrary.ru": "hindex_elibrary",
    "Индекс Хирша по публикациям в РИНЦ": "hindex_rinc",
    "Индекс Хирша по ядру РИНЦ": "hindex_core",
    "Число публикаций, процитировавших работы автора": "citing_publications",
    "Число ссылок на самую цитируемую публикацию": "top_publication_citations",
    "Число публикаций автора, процитированных хотя бы один раз": "cited_publications",
    "Среднее число цитирований в расчете на одну публикацию": "citations_per_publication",
    "Индекс Хирша без учета самоцитирований": "hindex_without_self_citations",
    "Индекс Хирша по ядру РИНЦ без учета самоцитирований": "hindex_core_without_self_citations",
    "Индекс Хирша с учетом только статей в журналах": "hindex_journal_articles",
    "Год первой публикации": "first_publication_year",
    "Число самоцитирований": "self_citations",
    "Число цитирований соавторами": "coauthor_citations",
    "Число соавторов": "coauthors",
    "Число статей в зарубежных журналах": "articles_foreign_journals",
    "Число статей в российских журналах": "articles_russian_journals",
    "Число статей в российских журналах из перечня ВАК": "articles_vak_journals",
    "Число статей в российских переводных журналах": "articles_translated_journals",
    "Число статей в журналах с ненулевым импакт-фактором": "articles_impact_factor_journals",
    "Число цитирований из зарубежных журналов на публикации автора": "citations_from_foreign_journals",
    "Число цитирований из зарубежных журналов": "citations_from_foreign_journals",
    "Число цитирований из российских журналов": "citations_from_russian_journals",
    "Число цитирований из российских журналов из перечня ВАК": "citations_from_vak_journals",
    "Число цитирований из российских переводных журналов": "citations_from_translated_journals",
    "Число цитирований из журналов с ненулевым импакт-фактором": "citations_from_impact_factor_journals",
    "Средневзвешенный импакт-фактор журналов, в которых были опубликованы статьи": "weighted_impact_factor_published",
    "Средневзвешенный импакт-фактор журналов, в которых были процитированы статьи": "weighted_impact_factor_cited",
    "Число публикаций в РИНЦ за последние 5 лет": "publications_rinc_5y",
    "Число публикаций в ядре РИНЦ за последние 5 лет": "publications_core_5y",
    "Число ссылок из РИНЦ на работы, опубликованные за последние 5 лет": "citations_rinc_5y",
    "Число ссылок из ядра РИНЦ на работы, опубликованные за последние 5 лет": "citations_core_5y",
    "Число ссылок на работы автора из всех публикаций за последние 5 лет": "citations_all_5y",
    "Основная рубрика (ГРНТИ)": "main_rubric_grnti",
    "Основная рубрика (OECD)": "main_rubric_oecd",
    "Процентиль по ядру РИНЦ": "core_percentile",
}
#: Labels seen truncated or reworded on some pages: prefix -> field.
_PROFILE_PREFIXES: list[tuple[str, str]] = [
    ("Число цитирований из зарубежных журналов", "citations_from_foreign_journals"),
]
_FLOAT_FIELDS = {"citations_per_publication", "weighted_impact_factor_published", "weighted_impact_factor_cited"}
_TEXT_FIELDS = {"main_rubric_grnti", "main_rubric_oecd"}

_PROFILE_LINK_RE = re.compile(r"^Профиль автора в\s+(.+?)\s*$")


# --------------------------------------------------------------------------- helpers


def _children(node: LexborNode, tag: str) -> list[LexborNode]:
    """Direct element children of ``node`` with the given tag."""
    return [c for c in node.iter() if c.tag == tag]


def _split_town(text: str) -> tuple[str, str | None]:
    """``"Институт (МИИТ) (Москва)"`` -> ``("Институт (МИИТ)", "Москва")``."""
    text = clean(text)
    m = _TOWN_RE.match(text)
    if m and m.group(2).strip():
        return clean(m.group(1)), clean(m.group(2))
    return text, None


def _split_authors(text: str) -> list[str]:
    text = clean(text)
    if not text:
        return []
    return [a.strip() for a in re.split(r",\s*(?=[^,]*[А-ЯЁA-Z])", text) if a.strip()]


def _num(text: str | None) -> int | float | None:
    t = clean(text)
    if not t:
        return None
    if re.fullmatch(r"-?\d[\d\s]*", t):
        return int(re.sub(r"\s", "", t))
    f = to_float(t)
    if f is None:
        return None
    return int(f) if f.is_integer() and not re.search(r"[.,]", t) else f


def _page_title_name(tree: LexborHTMLParser) -> str | None:
    """Author name from ``<title>``: 'Петров Владимир Алексеевич / анализ публикационной
    активности', 'Петров Владимир Алексеевич - Список публикаций' or (older pages)
    'eLIBRARY.RU - Марков Антон Владимирович - Список публикаций'."""
    t = tree.css_first("title")
    if t is None:
        return None
    parts = [p.strip() for p in re.split(r"\s+[/\-–—]\s+", node_text(t))]
    for part in parts:
        low = part.lower()
        if not part or low in ("elibrary.ru", "elibrary") or low.startswith(_TITLE_NOISE):
            continue
        return part
    return None


def _enclosing(node: LexborNode | None, tag: str) -> LexborNode | None:
    while node is not None and node.tag != tag:
        node = node.parent
    return node


def _paging(tree: LexborHTMLParser, text: str) -> tuple[int | None, int | None, int | None, int | None]:
    """(current page, number of pages, first row, last row) from the ``#pages`` block and the
    "Показано на данной странице: с a по b" line."""
    current = None
    pages = None
    box = tree.css_first("#pages")
    if box is not None:
        for f in box.css("font"):
            t = node_text(f)
            if t.isdigit():
                current = int(t)
                break
        nums = [int(n) for a in box.css('a[href^="javascript:goto_page"]')
                for n in _GOTO_RE.findall(a.attributes.get("href") or "")]
        pages = max(nums + ([current] if current else []), default=None)
    rng = _RANGE_RE.search(text)
    rfrom, rto = (int(rng.group(1)), int(rng.group(2))) if rng else (None, None)
    return current, pages, rfrom, rto


def _resolve_paging(tree: LexborHTMLParser, text: str, page_hint: int | None, total: int | None,
                    n_rows: int) -> tuple[int, int | None, int]:
    current, pages, rfrom, rto = _paging(tree, text)
    per_page = DEFAULT_PER_PAGE
    if current is None:
        current = page_hint
    if current is None and rfrom is not None:
        current = (rfrom - 1) // DEFAULT_PER_PAGE + 1
    current = current or 1
    if rfrom is not None and rto is not None:
        if current > 1 and rfrom > 1:
            per_page = (rfrom - 1) // (current - 1)
        elif pages and pages > 1:
            per_page = rto - rfrom + 1
    if pages is None:
        if total == 0 or (total is None and n_rows == 0):
            pages = 0
        elif total is not None:
            pages = max(1, -(-total // per_page))
        else:
            pages = current
    return current, pages, per_page


def _parse_affiliation_fragment(node: LexborNode) -> AuthorAffiliation | None:
    """Affiliation from a header line: ``<a href="org_profile.asp?id=169">NAME</a>[, DEPT] (TOWN)``."""
    link = node.css_first('a[href*="org_profile.asp"], a[href*="org_about.asp"]')
    full = node_text(node)
    if not full:
        return None
    if link is not None:
        name = node_text(link)
        href = link.attributes.get("href") or ""
        org_id = int_param(href, "id") or int_param(href, "orgsid")
        idx = full.find(name)
        rest = full[idx + len(name):] if idx >= 0 else ""
        dept, town = _split_town(rest)
        dept = dept.strip(" ,;") or None
        return AuthorAffiliation(name=name, org_id=org_id, town=town, department=dept,
                                 url=f"{BASE_URL}/org_profile.asp?id={org_id}" if org_id else None)
    name, town = _split_town(full)
    return AuthorAffiliation(name=name, town=town)


def _header_div(tree: LexborHTMLParser, name_hint: str | None) -> LexborNode | None:
    if name_hint:
        upper = name_hint.upper()
        for b in tree.css("font > b"):
            if node_text(b).upper() == upper:
                div = _enclosing(b, "div")
                if div is not None:
                    return div
    found = None
    for div in tree.css("div"):
        txt = node_text(div)
        if "AuthorID:" in txt and len(txt) < 2000:
            found = div  # keep the innermost (the last in document order)
    return found


def _parse_header(div: LexborNode) -> tuple[str | None, bool, AuthorAffiliation | None, dict[str, str]]:
    """(upper-case name, SCIENCE INDEX star, affiliation, {label: value}) of the author header."""
    name_b = div.css_first("font > b") or div.css_first("b")
    name = node_text(name_b) or None
    registered = div.css_first("span.aster") is not None
    affiliation = None
    fields: dict[str, str] = {}
    extra_lines: list[str] = []
    for i, frag in enumerate(_BR_RE.split(div.inner_html or "")):
        if i == 0:
            continue
        sub = LexborHTMLParser(f"<div>{frag}</div>").css_first("div")
        if sub is None:
            continue
        txt = node_text(sub)
        if not txt:
            continue
        if "AuthorID:" in txt or "SPIN-код:" in txt:
            if m := _SPIN_RE.search(txt):
                fields["SPIN"] = m.group(1)
            if m := _AUTHORID_RE.search(txt):
                fields["AuthorID"] = m.group(1)
            continue
        if affiliation is None:
            affiliation = _parse_affiliation_fragment(sub)
        else:
            extra_lines.append(txt)
    if extra_lines:
        fields["header"] = " | ".join(extra_lines)
    return name, registered, affiliation, fields


def _section_table(tree: LexborHTMLParser, heading: str) -> LexborNode | None:
    """The first table that follows the section heading ``<b>HEADING</b>``."""
    for b in tree.css("b"):
        if node_text(b) != heading:
            continue
        start = _enclosing(b, "div")
        if start is None:
            continue
        node = start.next
        while node is not None:
            if node.tag == "table":
                return node
            if node.tag == "div":
                inner = node.css_first("table")
                if inner is not None:
                    return inner
                if node.css_first("b") is not None and node_text(node):
                    return None  # next section
            node = node.next
    return None


# --------------------------------------------------------------------------- authors.asp


def _parse_author_row(tr: LexborNode, short_names: dict[int, str]) -> AuthorHit | None:
    m = _AUTHOR_ROW_RE.match(tr.attributes.get("id") or "")
    if not m:
        return None
    author_id = int(m.group(1))
    tds = _children(tr, "td")
    main_idx = None
    for i, td in enumerate(tds):
        b = td.css_first("b")
        if b is not None and not re.fullmatch(r"\d+\.?", node_text(b)):
            main_idx = i
            break
    if main_idx is None:
        return None
    main = tds[main_idx]
    name_node = main.css_first("b")
    name = node_text(name_node)
    lines = text_lines(main)
    rest = [ln for ln in lines if ln != name and ln != "*"]
    if rest and rest[0] == name:
        rest = rest[1:]
    affiliation_text = clean(" ".join(rest))
    affiliation, town = _split_town(affiliation_text) if affiliation_text else (None, None)
    wiki = main.css_first("img[data-tooltip]")
    position = to_int(node_text(tds[0])) if main_idx > 0 else None

    def cell(offset: int) -> LexborNode | None:
        j = main_idx + offset
        return tds[j] if j < len(tds) else None

    pubs_link = tr.css_first('a[href*="author_items.asp"]')
    profile_link = tr.css_first('a[href*="author_profile.asp"]')
    publications = to_int(node_text(pubs_link)) if pubs_link is not None else to_int(node_text(cell(1)))
    return AuthorHit(
        id=author_id,
        name=name,
        short_name=short_names.get(author_id),
        science_index_registered=main.css_first("span.aster") is not None,
        affiliation=affiliation or None,
        town=town,
        publications=publications,
        citations=to_int(node_text(cell(2))),
        hindex=to_int(node_text(cell(3))),
        position=position,
        wikipedia_url=(wiki.attributes.get("data-tooltip") or None) if wiki is not None else None,
        profile_url=(abs_url(profile_link.attributes.get("href")) or "") if profile_link is not None else "",
        publications_url=(abs_url(pubs_link.attributes.get("href")) or "") if pubs_link is not None else "",
    )


def parse_author_search(
    html: str | bytes,
    *,
    page: int | None = None,
    query: AuthorQuery | dict | None = None,
    url: str | None = None,
) -> SearchPage[AuthorHit]:
    """Parse an ``authors.asp`` result page (also the empty form page: no results)."""
    tree = remove_noise(parse_html(html))
    text = node_text(tree.body)
    table = tree.css_first("table#restab")
    is_authors_page = (
        tree.css_first('input[name="surname"]') is not None
        or "Всего найдено авторов" in text
        or "Не найдено авторов" in text
    )
    if not is_authors_page:
        raise ParseError("Страница не похожа на результаты поиска авторов eLIBRARY.RU", url=url)
    qdict = query.model_dump(exclude_none=True) if isinstance(query, AuthorQuery) else dict(query or {})
    source_url = url or f"{BASE_URL}/authors.asp"
    total = total_db = None
    if m := _AUTHORS_TOTAL_RE.search(text):
        total, total_db = to_int(m.group(1)), to_int(m.group(2))
    items: list[AuthorHit] = []
    if table is not None:
        short_names: dict[int, str] = {}
        for inp in tree.css('input[name^="hid"]'):
            key = (inp.attributes.get("name") or "")[3:]
            if key.isdigit():
                short_names[int(key)] = clean(inp.attributes.get("value"))
        for tr in table.css("tr"):
            hit = _parse_author_row(tr, short_names)
            if hit is not None:
                items.append(hit)
    if not items and total is None and ("Не найдено авторов" in text or table is None):
        total = 0
    current, pages, per_page = _resolve_paging(tree, text, page, total, len(items))
    return SearchPage[AuthorHit](
        items=items,
        total=total,
        total_in_database=total_db,
        page=current,
        per_page=per_page,
        pages=pages,
        query=qdict,
        source_url=source_url,
    )


# --------------------------------------------------------------------------- author_profile.asp


def _metric_number(value: str) -> tuple[int | float | None, float | None]:
    """``'929 (86,9%)'`` -> ``(929, 86.9)``; ``'83,23'`` -> ``(83.23, None)``; text -> ``(None, None)``."""
    m = _METRIC_VALUE_RE.match(clean(value))
    if not m:
        return None, None
    number = _num(m.group(1))
    percent = to_float(m.group(2)) if m.group(2) else None
    return number, percent


def _parse_metrics(table: LexborNode) -> tuple[list[AuthorMetric], dict[str, int]]:
    metrics: list[AuthorMetric] = []
    roles: dict[str, int] = {}
    parent: str | None = None
    for tr in table.css("tr"):
        tds = _children(tr, "td")
        if len(tds) != 3:
            continue
        label = node_text(tds[1])
        if not label or label in ("Название показателя",):
            continue
        has_help = tds[0].css_first("img") is not None
        vcell = tds[2]
        value = node_text(vcell)
        link = vcell.css_first("a[href]")
        href = abs_url(link.attributes.get("href")) if link is not None else None
        if has_help:
            parent = None
        if has_help and label.endswith(":") and not value:
            parent = label[:-1].strip()
            metrics.append(AuthorMetric(name=parent, value=""))
            continue
        name = label
        if parent and not has_help:
            name = f"{parent}: {label}"
            if (n := to_int(value)) is not None:
                roles[label] = n
        number, percent = _metric_number(value)
        metrics.append(AuthorMetric(name=name, value=value, number=number, percent=percent, url=href))
    return metrics, roles


def _apply_metrics(data: dict, metrics: list[AuthorMetric], extra: dict[str, str]) -> None:
    for mt in metrics:
        if ": " in mt.name and mt.name.split(": ", 1)[0] == "Участие в публикациях":
            continue
        if mt.name == "Участие в публикациях":
            continue
        label = mt.name.rstrip(":").strip()
        five = _FIVE_YEARS_RE.search(label)
        if five:
            label = label[: five.start()].strip()
            data.setdefault("five_year_period", f"{five.group(1)}-{five.group(2)}")
        field = _PROFILE_FIELDS.get(label)
        if field is None:
            field = next((f for prefix, f in _PROFILE_PREFIXES if label.startswith(prefix)), None)
        if field is None or field in data:
            extra[mt.name] = mt.value
            continue
        if field in _TEXT_FIELDS:
            data[field] = mt.value or None
        elif field in _FLOAT_FIELDS:
            data[field] = mt.number
        else:
            data[field] = to_int(mt.value)


def _parse_yearly(table: LexborNode) -> tuple[list[int], list[AuthorYearlyMetric]]:
    years: list[int] = []
    rows: list[AuthorYearlyMetric] = []
    for tr in table.css("tr"):
        cells = [c for c in tr.iter() if c.tag in ("td", "th")]
        if len(cells) < 2:
            continue
        label = node_text(cells[0])
        values = [node_text(c) for c in cells[1:]]
        if not years and label == "Название показателя":
            years = [int(v) for v in values if v.isdigit()]
            continue
        if not label or not years:
            continue
        rows.append(AuthorYearlyMetric(name=label, values={y: _num(v) for y, v in zip(years, values, strict=False)}))
    return years, rows


def _parse_affiliations_table(table: LexborNode) -> list[AuthorAffiliation]:
    out: list[AuthorAffiliation] = []
    for tr in table.css("tr"):
        tds = _children(tr, "td")
        link = tr.css_first('a[href*="org_profile.asp"]')
        if link is None or len(tds) < 3:
            continue
        org_cell = _enclosing(link, "td")
        idx = next((i for i, td in enumerate(tds) if org_cell is not None and td.mem_id == org_cell.mem_id), None)
        if idx is None:
            continue
        aff = _parse_affiliation_fragment(tds[idx])
        if aff is None:
            continue
        rest = tds[idx + 1:]
        period = node_text(rest[0]) if rest else ""
        years = [int(y) for y in re.findall(r"\d{4}", period)]
        count_cell = rest[1] if len(rest) > 1 else None
        count_link = count_cell.css_first("a[href]") if count_cell is not None else None
        aff.period = period or None
        aff.year_from = years[0] if years else None
        aff.year_to = years[-1] if years else None
        aff.publications = to_int(node_text(count_cell)) if count_cell is not None else None
        aff.publications_url = abs_url(count_link.attributes.get("href")) if count_link is not None else None
        out.append(aff)
    return out


def _external_profiles(tree: LexborHTMLParser) -> dict[str, str]:
    """Links 'Профиль автора в ORCID / Scopus / ResearcherID ...' of the tools panel: name -> URL."""
    out: dict[str, str] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        m = _PROFILE_LINK_RE.match(node_text(a))
        if m and href.startswith("http"):
            out.setdefault(m.group(1), href)
    return out


def _external_url(external: dict[str, str], *markers: str) -> str | None:
    return next((u for u in external.values() if any(mk in u.lower() for mk in markers)), None)


def parse_author_profile(html: str | bytes, *, url: str | None = None) -> AuthorProfile:
    """Parse ``author_profile.asp?id=ID`` (publication-activity analysis of an author)."""
    tree = parse_html(html)
    name_hint = _page_title_name(tree)
    remove_noise(tree)
    text = node_text(tree.body)
    is_profile = "АНАЛИЗ ПУБЛИКАЦИОННОЙ АКТИВНОСТИ АВТОРА" in text or (
        "AuthorID:" in text and "ОБЩИЕ ПОКАЗАТЕЛИ" in text)
    if not is_profile:
        if _NOT_FOUND_RE.search(text):
            raise NotFound("Автор не найден в eLIBRARY.RU", url=url)
        raise ParseError("Страница не похожа на профиль автора eLIBRARY.RU (author_profile.asp)", url=url)
    header = _header_div(tree, name_hint)
    upper_name, registered, affiliation, hfields = _parse_header(header) if header is not None else (
        None, False, None, {})
    author_id = to_int(hfields.get("AuthorID"))
    if author_id is None:
        inp = tree.css_first('input[name="authorid"]')
        author_id = to_int(inp.attributes.get("value")) if inp is not None else None
    if author_id is None and url:
        author_id = int_param(url, "id")
    if author_id is None:
        raise ParseError("Не удалось определить AuthorID на странице профиля автора", url=url)
    name = name_hint
    if not name and upper_name:
        name = upper_name.title()
    if not name:
        raise ParseError("Не найдено имя автора на странице профиля", url=url)

    data: dict = {}
    extra: dict[str, str] = {}
    if hfields.get("header"):
        extra["Заголовок"] = hfields["header"]
    metrics: list[AuthorMetric] = []
    roles: dict[str, int] = {}
    mtable = _section_table(tree, "ОБЩИЕ ПОКАЗАТЕЛИ")
    if mtable is not None:
        metrics, roles = _parse_metrics(mtable)
        _apply_metrics(data, metrics, extra)
    years: list[int] = []
    yearly: list[AuthorYearlyMetric] = []
    ytable = tree.css_first("#years_ind table") or _section_table(tree, "ПОКАЗАТЕЛИ ПО ГОДАМ")
    if ytable is not None:
        years, yearly = _parse_yearly(ytable)
    affiliations: list[AuthorAffiliation] = []
    atable = _section_table(tree, "МЕСТО РАБОТЫ")
    if atable is not None:
        affiliations = _parse_affiliations_table(atable)

    reports: list[AuthorReport] = []
    seen: set[str] = set()
    for a in tree.css('a[href*="author_profile("]'):
        title = node_text(a)
        m = _REPORT_RE.search(a.attributes.get("href") or "")
        if not title or not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        reports.append(AuthorReport(
            key=m.group(1), title=title,
            url=f"{BASE_URL}/author_profile_new_{m.group(1)}.asp?id={author_id}"))

    external = _external_profiles(tree)
    orcid = None
    if (orcid_url := _external_url(external, "orcid.org")) and (m := _ORCID_RE.search(orcid_url)):
        orcid = m.group(0)
    scopus_url = _external_url(external, "scopus.com")
    scopus_id = (query_param(scopus_url, "authorId") or query_param(scopus_url, "authorID")) if scopus_url else None
    researcher_id = None
    if rid_url := _external_url(external, "webofscience.com", "researcherid.com", "publons.com"):
        researcher_id = rid_url.rstrip("/").rsplit("/", 1)[-1] or None

    updated = None
    if m := _UPDATED_RE.search(text):
        try:
            updated = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            updated = None
    citing = tree.css_first('a[href*="cit_author_items.asp"]')
    return AuthorProfile(
        id=author_id,
        name=name,
        science_index_registered=registered,
        spin=hfields.get("SPIN"),
        orcid=orcid,
        researcher_id=researcher_id,
        scopus_id=scopus_id,
        external_profiles=external,
        affiliation=affiliation,
        affiliations=affiliations,
        roles=roles,
        metrics=metrics,
        years=years,
        yearly=yearly,
        reports=reports,
        metrics_updated=updated,
        extra=extra,
        url=url or "",
        citing_publications_url=(abs_url(citing.attributes.get("href")) or "") if citing is not None else "",
        **data,
    )


# --------------------------------------------------------------------------- author_items.asp


def _parse_reference_row(tr: LexborNode) -> AuthorReferenceHit | None:
    m = _REF_ROW_RE.match(tr.attributes.get("id") or "")
    if not m:
        return None
    tds = _children(tr, "td")
    if len(tds) < 2:
        return None
    main = tds[1]
    title_node = main.css_first("b")
    title = node_text(title_node)
    ital = main.css_first("i")
    authors_txt = node_text(ital)
    full = node_text(main)
    rest = full
    if title and rest.startswith(title):
        rest = rest[len(title):]
    if authors_txt and (idx := rest.find(authors_txt)) >= 0:
        rest = rest[idx + len(authors_txt):]
    source = clean(rest) or None
    num = re.search(r"(\d+)", node_text(tds[0]))
    return AuthorReferenceHit(
        ref_id=int(m.group(1)),
        title=title,
        authors=_split_authors(authors_txt),
        source=source,
        year=source_year(source),
        citations=to_int(node_text(tds[-1])) if len(tds) > 2 else None,
        position=int(num.group(1)) if num else None,
    )


def parse_author_publications(
    html: str | bytes, *, page: int | None = None, url: str | None = None
) -> AuthorPublicationsPage:
    """Parse ``author_items.asp`` (publication list of an author)."""
    raw = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    tree = parse_html(html)
    name_hint = _page_title_name(tree)
    total_cited = None
    span = tree.css_first("span#totalcited")
    if span is not None:
        total_cited = to_int(node_text(span))
    if total_cited is None and (m := _TOTALCITED_JS_RE.search(raw)):
        total_cited = int(m.group(1))
    remove_noise(tree)
    text = node_text(tree.body)
    author_input = tree.css_first('input[name="authorid"]')
    is_items = "СПИСОК ПУБЛИКАЦИЙ АВТОРА" in text or (
        author_input is not None and tree.css_first('input[name="authorhash"]') is not None)
    if not is_items:
        raise ParseError("Страница не похожа на список публикаций автора eLIBRARY.RU (author_items.asp)", url=url)
    author_id = to_int(author_input.attributes.get("value")) if author_input is not None else None
    if author_id is None and url:
        author_id = int_param(url, "authorid")
    hash_input = tree.css_first('input[name="authorhash"]')
    short_name = clean(hash_input.attributes.get("value")) if hash_input is not None else None
    name = name_hint
    header = _header_div(tree, name)
    affiliation = None
    if header is not None:
        upper_name, _registered, affiliation, _fields = _parse_header(header)
        if not name and upper_name:
            name = upper_name.title()

    total = None
    if m := _ITEMS_TOTAL_RE.search(text):
        total = to_int(m.group(1) or m.group(2))
    items = parse_publication_rows(tree)
    references: list[AuthorReferenceHit] = []
    table = tree.css_first("table#restab")
    if table is not None:
        for tr in table.css("tr"):
            ref = _parse_reference_row(tr)
            if ref is not None:
                references.append(ref)
    if total is None and not items and not references:
        total = 0
    current, pages, per_page = _resolve_paging(tree, text, page, total, len(items) + len(references))
    source_url = url or (f"{BASE_URL}/author_items.asp?authorid={author_id}&show_refs=1" if author_id else None)
    return AuthorPublicationsPage(
        items=items,
        total=total,
        page=current,
        per_page=per_page,
        pages=pages,
        query={"author_id": author_id, "page": current} if author_id else {"page": current},
        source_url=source_url,
        author_id=author_id,
        author_name=name,
        author_short_name=short_name or None,
        affiliation=affiliation,
        total_citations=total_cited,
        references=references,
    )
