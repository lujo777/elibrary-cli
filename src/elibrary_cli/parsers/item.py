"""Parser of the publication card ``item.asp?id=...`` and of the ``ispdf.asp`` full-text icon.

Card layout (2026-10, one template for every publication type)::

    td (central column)
      table   eLIBRARY ID: <a>ID</a> | EDN: <a>EDN</a> | DOI: <a href=doi.org/..>DOI</a>
      table   <p class="bigtext">TITLE</p> [<br>Монография]
      div width:580px
        table  author group: div[inline-block] per author (span.help.pointer[title=tooltip], <sup>1,2</sup>),
               then numbered affiliations (<sup>1</sup> span.help1[title=tooltip] ", City")
        table  [more groups: "Научное редактирование:", "Ответственный редактор:", "Рецензенты:"]
        table  label/value flow: "Тип: <font>..</font> <span margin-left> Язык: ..." (Том, Выпуск, Год,
               Страницы, ISBN, Номер патента, Место защиты, ...); may nest the IPC section
        table  section: <tr><td colspan=2>HEADING:</td></tr><tr><td></td><td>content</td></tr>
               ЖУРНАЛ, ИСТОЧНИК, КОНФЕРЕНЦИЯ, КЛЮЧЕВЫЕ СЛОВА, АННОТАЦИЯ, БИБЛИОМЕТРИЧЕСКИЕ ПОКАЗАТЕЛИ,
               ТЕМАТИЧЕСКИЕ НАПРАВЛЕНИЯ, АЛЬТМЕТРИКИ, ИНФОРМАЦИЯ О ФИНАНСОВОЙ ПОДДЕРЖКЕ, ПЕРЕВОДНАЯ ВЕРСИЯ,
               ОПИСАНИЕ НА АНГЛИЙСКОМ ЯЗЫКЕ, ОГЛАВЛЕНИЕ (table#restab), МЕЖДУНАРОДНАЯ ПАТЕНТНАЯ КЛАССИФИКАЦИЯ
    div.right-panel  ИНСТРУМЕНТЫ: full text (file_article / url_article JS links), price, Дата размещения

Text is turned into "lines" (a break at <br>, block elements and the ``margin-left`` spacer spans), and
lines into label/value fields ("Label: value" + continuation lines). Known labels go to typed fields,
everything else to ``extra`` so nothing shown on the card is lost.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from selectolax.lexbor import LexborHTMLParser, LexborNode

from elibrary_cli.errors import CaptchaRequired, NotFound, ParseError
from elibrary_cli.models.base import BASE_URL
from elibrary_cli.models.publications import (
    Affiliation,
    AuthorRef,
    BookInfo,
    ConferenceInfo,
    ContainerRef,
    DissertationInfo,
    FulltextFlag,
    FulltextLink,
    IpcCode,
    JournalIndexing,
    JournalRef,
    OrgRef,
    PatentInfo,
    Publication,
    RelatedItem,
)
from elibrary_cli.parsers.common import (
    abs_url,
    clean,
    first_year,
    int_param,
    node_text,
    parse_html,
    text_lines,
    to_float,
    to_int,
    yes_no,
)
from elibrary_cli.parsers.rows import parse_publication_rows, source_year

__all__ = [
    "COLOUR_ACCESS",
    "is_captcha_page",
    "normalize_kind",
    "parse_fulltext_flag",
    "parse_publication",
]

#: Full-text icon colour -> access level (legend of the result lists, 2026-10).
COLOUR_ACCESS: dict[str, str] = {
    "green": "open",      # "Доступ к полному тексту документа открыт"
    "blue": "external",   # "Полный текст доступен на сайте издателя / на внешнем сайте"
    "yellow": "order",    # "Полный текст может быть получен через систему заказа" / "Можно приобрести"
    "orange": "closed",   # "Доступ к полному тексту закрыт"
    "red": "paid",        # older icon set (pdf_red.gif)
}

LANGUAGE_CODES: dict[str, str] = {
    "русский": "ru", "английский": "en", "украинский": "uk", "белорусский": "be", "казахский": "kk",
    "немецкий": "de", "французский": "fr", "испанский": "es", "итальянский": "it", "китайский": "zh",
    "японский": "ja", "польский": "pl", "чешский": "cs", "болгарский": "bg", "сербский": "sr",
    "узбекский": "uz", "киргизский": "ky", "кыргызский": "ky", "таджикский": "tg", "армянский": "hy",
    "грузинский": "ka", "азербайджанский": "az", "татарский": "tt", "башкирский": "ba", "латинский": "la",
    "португальский": "pt", "турецкий": "tr", "арабский": "ar", "корейский": "ko", "монгольский": "mn",
    "якутский": "sah", "чувашский": "cv", "румынский": "ro", "литовский": "lt", "латышский": "lv",
    "эстонский": "et", "финский": "fi", "венгерский": "hu", "греческий": "el", "вьетнамский": "vi",
    "персидский": "fa", "хинди": "hi", "иврит": "he",
}

_BLOCK_TAGS = frozenset({
    "br", "div", "p", "tr", "td", "th", "table", "tbody", "thead", "tfoot", "li", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "center", "hr", "blockquote",
})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "select", "option", "input", "textarea", "button"})
_LABEL_RE = re.compile(r"^([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё .()/\-]{0,58}?)\s*:\s*(.*)$")
_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_JS_CALL_RE = re.compile(r"javascript:\s*(\w+)\s*\(([^)]*)\)", re.I)
_COLOUR_RE = re.compile(r"(?:download|pdf)_(green|blue|yellow|orange|red)", re.I)
_PRICE_RE = re.compile(r"за\s*(\d[\d\s ]*)\s*руб", re.I)
_NOT_FOUND_RE = re.compile(
    r"публикаци\w* не найден|не найден\w* публикаци|такой публикации нет|публикация удалена|"
    r"не существует|запрашиваемая страница не найдена|нет данных о публикации",
    re.I,
)
_GENERIC_TITLES = ("научная электронная библиотека", "elibrary.ru", "elibrary")


# --------------------------------------------------------------------------- text helpers


@dataclass
class _Line:
    text: str
    nodes: list[LexborNode] = field(default_factory=list)

    def hrefs(self) -> list[str]:
        """Links of the line: ``<a href>`` plus links inside SCIENCE INDEX tooltips."""
        out: list[str] = []
        for n in self.nodes:
            if n.tag == "a" and n.attributes.get("href"):
                out.append(n.attributes["href"] or "")
            tip = n.attributes.get("title") if n.tag == "span" else None
            if tip and "<a" in tip:
                out.extend(_tooltip(tip).hrefs)
        return out

    def tooltip(self) -> _Tip | None:
        for n in self.nodes:
            tip = n.attributes.get("title") if n.tag == "span" else None
            if tip and "<" in tip:
                return _tooltip(tip)
        return None


@dataclass
class _Field:
    label: str
    values: list[str]
    lines: list[_Line]

    @property
    def key(self) -> str:
        return _norm_label(self.label)

    @property
    def value(self) -> str:
        return clean(" ".join(self.values))

    def hrefs(self) -> list[str]:
        return [h for ln in self.lines for h in ln.hrefs()]

    def tooltip(self) -> _Tip | None:
        for ln in self.lines:
            tip = ln.tooltip()
            if tip is not None:
                return tip
        return None


@dataclass
class _Tip:
    """Parsed SCIENCE INDEX tooltip (``title`` attribute holding HTML)."""

    name: str | None
    lines: list[str]
    hrefs: list[str]

    @property
    def author_id(self) -> int | None:
        for h in self.hrefs:
            if "authorid=" in h:
                return int_param(h, "authorid")
        return None

    @property
    def org_id(self) -> int | None:
        for h in self.hrefs:
            if "org_profile" in h:
                return int_param(h, "id")
            if "orgsid=" in h:
                return int_param(h, "orgsid")
        return None

    @property
    def city(self) -> str | None:
        for ln in self.lines[1:3]:
            m = re.fullmatch(r"\((.+)\)", ln)
            if m:
                return clean(m.group(1))
        return None

    @property
    def second_line(self) -> str | None:
        for ln in self.lines[1:2]:
            if not re.match(r"^(Профиль|Список публикаций|\()", ln):
                return ln
        return None


def _tooltip(html: str) -> _Tip:
    t = LexborHTMLParser(html)
    lines = text_lines(t.body)
    font = t.css_first("font")
    name = node_text(font) if font is not None else (lines[0] if lines else None)
    hrefs = [a.attributes.get("href") or "" for a in t.css("a[href]")]
    return _Tip(name=name or None, lines=lines, hrefs=hrefs)


def _render(node: LexborNode | None, skip: frozenset[int] | set[int] = frozenset()) -> list[_Line]:
    """Visible text of ``node`` as lines, keeping the element nodes that produced each line."""
    lines: list[_Line] = []
    if node is None:
        return lines
    buf: list[str] = []
    nodes: list[LexborNode] = []

    def flush() -> None:
        text = clean("".join(buf))
        if text:
            lines.append(_Line(text, list(nodes)))
        buf.clear()
        nodes.clear()

    def walk(n: LexborNode) -> None:
        for ch in n.iter(include_text=True):
            tag = ch.tag or ""
            if tag == "-text":
                buf.append(ch.text_content or "")
                continue
            if tag in _SKIP_TAGS or tag.startswith(("-", "_", "!")) or ch.mem_id in skip:
                continue
            style = (ch.attributes.get("style") or "") if tag == "span" else ""
            block = tag in _BLOCK_TAGS or "margin-left" in style
            if block:
                flush()
            nodes.append(ch)
            walk(ch)
            if block:
                flush()

    walk(node)
    flush()
    return lines


def _inline(node: LexborNode | None) -> str:
    """Text of a node without inventing spaces between adjacent inline elements."""
    return " ".join(ln.text for ln in _render(node))


def _norm_label(label: str) -> str:
    return clean(label).lower().replace("ё", "е")


def _fields(lines: Iterable[_Line]) -> tuple[list[_Field], list[_Line]]:
    """``"Label: value"`` lines (+ continuation lines) -> fields; lines before the first label are
    returned separately."""
    fields: list[_Field] = []
    orphans: list[_Line] = []
    cur: _Field | None = None
    for ln in lines:
        m = _LABEL_RE.match(ln.text)
        if m:
            value = clean(m.group(2))
            cur = _Field(label=clean(m.group(1)), values=[value] if value else [], lines=[ln])
            fields.append(cur)
        elif cur is not None:
            cur.values.append(ln.text)
            cur.lines.append(ln)
        else:
            orphans.append(ln)
    return fields, orphans


def _to_date(text: str | None) -> date | None:
    if not text:
        return None
    m = _DATE_RE.search(text)
    try:
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = _ISO_DATE_RE.search(text)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return None


def _closest(node: LexborNode | None, tag: str) -> LexborNode | None:
    node = node.parent if node is not None else None
    while node is not None and node.tag != tag:
        node = node.parent
    return node


def _is_inside(node: LexborNode, ancestor_ids: set[int]) -> bool:
    n: LexborNode | None = node
    while n is not None:
        if n.mem_id in ancestor_ids:
            return True
        n = n.parent
    return False


def _children(node: LexborNode, tag: str) -> list[LexborNode]:
    return [ch for ch in node.iter() if ch.tag == tag]


def _numbers(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", text or "")]


def _none_if_no(value: str | None) -> str | None:
    v = clean(value)
    return None if not v or v.lower() in {"нет", "нет данных", "-"} else v


def _org_from_field(f: _Field) -> OrgRef | None:
    value = f.value
    if not value:
        return None
    tip = f.tooltip()
    name = value
    city = tip.city if tip else None
    m = re.match(r"^(.*\S)\s*\(([^()]+)\)$", name)
    if m and (city is None or clean(m.group(2)) == city):
        name, city = clean(m.group(1)), city or clean(m.group(2))
    publisher_id = None
    url = None
    for h in f.hrefs():
        if "pubsid=" in h:
            publisher_id = int_param(h, "pubsid")
            url = abs_url(h)
    org_id = tip.org_id if tip else None
    if org_id and not url:
        url = f"{BASE_URL}/org_profile.asp?id={org_id}"
    return OrgRef(name=name, full_name=tip.name if tip else None, org_id=org_id, city=city,
                  publisher_id=publisher_id, url=url)


def _orgs_from_field(f: _Field) -> list[OrgRef]:
    """One OrgRef per line of a multi-valued field (Патентообладатели)."""
    out: list[OrgRef] = []
    for i, ln in enumerate(f.lines):
        text = ln.text
        if i == 0:
            m = _LABEL_RE.match(text)
            text = clean(m.group(2)) if m else text
        if not text:
            continue
        sub = _Field(label=f.label, values=[text], lines=[ln])
        org = _org_from_field(sub)
        if org is not None:
            out.append(org)
    return out


# --------------------------------------------------------------------------- page checks


def is_captcha_page(tree: LexborHTMLParser) -> bool:
    title = node_text(tree.css_first("title")).lower()
    if "тест тьюринга" in title:
        return True
    if tree.css_first('form[action*="check_captcha"]') is not None:
        return True
    return tree.css_first("div.g-recaptcha") is not None and "необычно много запросов" in node_text(tree.body)


def _page_title(tree: LexborHTMLParser) -> str | None:
    og = tree.css_first('meta[property="og:title"]')
    for raw in ((og.attributes.get("content") if og is not None else None), node_text(tree.css_first("title"))):
        t = clean(raw)
        if t and t.lower() not in _GENERIC_TITLES and not t.lower().startswith(_GENERIC_TITLES):
            return t
    return None


def _alnum(text: str) -> str:
    return re.sub(r"\W+", "", text.casefold().replace("ё", "е"))


# --------------------------------------------------------------------------- sections


@dataclass
class _Section:
    name: str
    header: LexborNode
    table: LexborNode | None
    cells: list[LexborNode]

    def lines(self) -> list[_Line]:
        return [ln for c in self.cells for ln in _render(c)]

    def text(self) -> str:
        return clean(" ".join(ln.text for ln in self.lines()))


def _find_sections(content: LexborNode) -> list[_Section]:
    out: list[_Section] = []
    for td in content.css("td"):
        if td.attributes.get("colspan") != "2":
            continue
        heading = node_text(td)
        if not (heading.endswith(":") and 3 <= len(heading) <= 80 and heading == heading.upper()
                and re.search(r"[А-ЯЁA-Z]{3}", heading)):
            continue
        tr = td.parent
        if tr is None or tr.tag != "tr":
            continue
        cells: list[LexborNode] = []
        sib = tr.next
        while sib is not None:
            if sib.tag == "tr":
                tds = _children(sib, "td")
                if tds:
                    cells.append(tds[-1])
            sib = sib.next
        out.append(_Section(name=clean(heading[:-1]), header=td, table=_closest(td, "table"), cells=cells))
    return out


# --------------------------------------------------------------------------- authors


def _is_author_div(div: LexborNode) -> bool:
    if "inline-block" not in (div.attributes.get("style") or ""):
        return False
    text = node_text(div)
    return bool(text) and ":" not in text and div.css_first("font, b, span") is not None


def _about_author(div: LexborNode) -> str | None:
    """Text of the "Об авторе" info icon (``img[src*=about_author]``, HTML tooltip
    ``'Об авторе: <font>к.х.н.</font>'``): position / degree as entered by the author."""
    for img in div.css("img"):
        if "about_author" not in (img.attributes.get("src") or ""):
            continue
        title = img.attributes.get("title") or ""
        text = node_text(LexborHTMLParser(title).body) if "<" in title else clean(title)
        text = re.sub(r"^Об\s+авторе\s*:\s*", "", text, flags=re.I).strip()
        if text:
            return text
    return None


def _parse_author_div(div: LexborNode) -> AuthorRef:
    span = next((s for s in div.css("span") if "pointer" in (s.attributes.get("class") or "")), None)
    name_node = span or div.css_first("b") or div.css_first("font")
    name = _inline(name_node) if name_node is not None else _inline(div)
    name = clean(name.rstrip(",*").strip())
    sups = [node_text(s) for s in div.css("sup")]
    numbers = [n for s in sups for n in _numbers(s)]
    corresponding = any("astmd" in (s.attributes.get("class") or "") for s in div.css("span"))
    tip = _tooltip(span.attributes.get("title") or "") if span is not None and span.attributes.get("title") else None
    author_id = tip.author_id if tip else None
    return AuthorRef(
        name=name,
        full_name=tip.name if tip else None,
        author_id=author_id,
        affiliations=numbers,
        organization=tip.second_line if tip else None,
        about=_about_author(div),
        corresponding=corresponding,
        url=f"{BASE_URL}/author_profile.asp?authorid={author_id}" if author_id else None,
    )


def _segments(tokens: list[LexborNode]) -> list[list[LexborNode]]:
    """Affiliation lines: ``tokens`` (children of the author cell after the authors) split at <br>
    and <div>."""
    segs: list[list[LexborNode]] = [[]]
    for ch in tokens:
        if ch.tag in ("br", "div", "p", "table"):
            segs.append([])
            if ch.tag != "br" and node_text(ch):  # a block holding the text itself
                segs[-1].append(ch)
                segs.append([])
            continue
        segs[-1].append(ch)
    return [s for s in segs if s]


def _seg_text(seg: list[LexborNode]) -> str:
    parts = []
    for n in seg:
        parts.append((n.text_content or "") if n.tag == "-text" else _inline(n))
    return clean("".join(parts))


def _parse_affiliation_segment(seg: list[LexborNode]) -> Affiliation | None:
    text = _seg_text(seg)
    if not text or text in {",", "."}:
        return None
    number = None
    name_node = None
    tip = None
    for n in seg:
        if n.tag == "-text":
            continue
        sup = n if n.tag == "sup" else n.css_first("sup")
        if sup is not None and number is None:
            nums = _numbers(node_text(sup))
            number = nums[0] if nums else None
            continue
        if name_node is None:
            span = n if n.tag == "span" and n.attributes.get("title") else n.css_first("span[title]")
            if span is not None:
                name_node = span
                tip = _tooltip(span.attributes.get("title") or "")
            elif n.tag in ("font", "a", "span", "b", "i"):
                name_node = n
    name = _inline(name_node) if name_node is not None else re.sub(r"^\d+\s*", "", text)
    if not name:
        return None
    pos = text.find(name)
    rest = text[pos + len(name):] if pos >= 0 else ""
    location = clean(rest.lstrip(" ,;.")) or None
    org_id = tip.org_id if tip else None
    return Affiliation(
        number=number, name=name, full_name=tip.name if tip else None, org_id=org_id,
        city=tip.city if tip else None, location=location,
        url=f"{BASE_URL}/org_profile.asp?id={org_id}" if org_id else None,
    )


@dataclass
class _AuthorGroup:
    role: str | None
    authors: list[AuthorRef]
    affiliations: list[Affiliation]


def _split_cell(td: LexborNode) -> list[tuple[str | None, list[LexborNode], list[LexborNode]]]:
    """Split an author cell into ``(role, author divs, tail tokens)`` groups.

    One cell may hold several groups: ``Ответственный редактор: <div>A</div>, <div>B</div>
    <div spacer/> 1 Org <br> <div spacer/> Рецензенты: <div>C</div> ...``. A group is a run of author
    divs (separated by commas); its role is the text right before it, back to the previous
    <br>/<div>; its affiliations are the tokens up to the next group's role label.
    """
    tokens = list(td.iter(include_text=True))
    is_author = [t.tag == "div" and _is_author_div(t) for t in tokens]
    clusters: list[tuple[int, int]] = []
    i, n = 0, len(tokens)
    while i < n:
        if not is_author[i]:
            i += 1
            continue
        j = k = i
        while k + 1 < n:
            nxt = tokens[k + 1]
            if is_author[k + 1]:
                j = k = k + 1
            elif nxt.tag == "-text" and not clean(nxt.text_content).strip(", "):
                k += 1
            else:
                break
        clusters.append((i, j))
        i = j + 1
    prefix_starts: list[int] = []
    roles: list[str | None] = []
    for ci, (s, _e) in enumerate(clusters):
        lower = clusters[ci - 1][1] + 1 if ci else 0
        p = s - 1
        while p >= lower and tokens[p].tag not in ("br", "div", "p", "table"):
            p -= 1
        prefix = tokens[p + 1:s]
        prefix_starts.append(p + 1)
        text = clean("".join((t.text_content or "") if t.tag == "-text" else _inline(t) for t in prefix))
        roles.append(text.rstrip(":").strip() or None)
    out = []
    for ci, (s, e) in enumerate(clusters):
        end = prefix_starts[ci + 1] if ci + 1 < len(clusters) else n
        divs = [tokens[x] for x in range(s, e + 1) if is_author[x]]
        out.append((roles[ci], divs, tokens[e + 1:end]))
    return out


def _author_groups(content: LexborNode, exclude: set[int]) -> list[_AuthorGroup]:
    cells: list[LexborNode] = []
    seen: set[int] = set()
    for div in content.css("div"):
        if not _is_author_div(div) or _is_inside(div, exclude):
            continue
        td = _closest(div, "td")
        if td is None or td.mem_id in seen:
            continue
        seen.add(td.mem_id)
        cells.append(td)
    groups: list[_AuthorGroup] = []
    for td in cells:
        for role, divs, tail in _split_cell(td):
            authors = [_parse_author_div(d) for d in divs]
            affs = [a for a in (_parse_affiliation_segment(s) for s in _segments(tail)) if a]
            by_number = {a.number: a.name for a in affs if a.number is not None}
            for a in authors:
                a.role = role
                a.affiliation_names = [by_number[x] for x in a.affiliations if x in by_number]
            groups.append(_AuthorGroup(role=role, authors=authors, affiliations=affs))
    return groups


# --------------------------------------------------------------------------- kinds


def normalize_kind(type_raw: str | None, *, journal: bool = False, container: bool = False,
                   conference: bool = False) -> str:
    """Normalised publication kind from the card's ``Тип:`` (and, when it is missing, from the blocks
    present on the card). Values: article, conference, proceedings, book, chapter, dissertation,
    patent, report, preprint, grant, dataset, other."""
    t = clean(type_raw).lower().replace("ё", "е")
    if not t:
        if conference:
            return "conference"
        if journal:
            return "article"
        return "chapter" if container else "other"
    if re.search(r"патент|свидетельство|полезн\w+ модел|промышленн\w+ образ|топологи", t):
        return "patent"
    if "диссертац" in t:
        return "dissertation"
    if t.startswith("статья в журнале"):
        return "article"
    if "глава" in t or "раздел в книге" in t or "статья в книге" in t:
        return "chapter"
    if re.match(r"^(сборник|материалы|труды)\b.*(конференц|симпозиум|конгресс|съезд|форум|семинар)", t):
        return "proceedings"
    if "тезис" in t or "конференц" in t or "доклад" in t:
        return "conference"
    if "статья в сборнике" in t:
        return "chapter"
    if "отчет" in t:
        return "report"
    if "депонирован" in t or "препринт" in t or "открытом архиве" in t:
        return "preprint"
    if "грант" in t:
        return "grant"
    if "набор данных" in t or "датасет" in t or "dataset" in t:
        return "dataset"
    if re.search(r"монограф|пособие|учебник|книга|сборник|справочник|словарь|энциклопед|практикум|"
                 r"брошюра|методическ|хрестомат|атлас|каталог", t):
        return "book"
    if re.search(r"стать|рецензи|обзор|редакционн|персоналия|интервью|письмо|заметка|хроника", t):
        return "article" if journal or not container else "chapter"
    if conference:
        return "conference"
    if journal:
        return "article"
    return "chapter" if container else "other"


# --------------------------------------------------------------------------- blocks


def _parse_indexing(fields: list[_Field]) -> JournalIndexing | None:
    if not fields:
        return None
    idx = JournalIndexing()
    for f in fields:
        k, v = f.key, f.value
        if k == "web of science":
            idx.wos, idx.in_wos = v, yes_no(v)
        elif k == "scopus":
            idx.scopus, idx.in_scopus = v, yes_no(v)
        elif k == "белый список":
            idx.white_list, idx.in_white_list = v, yes_no(v)
            m = re.search(r"уров\w*\s*(\d+)", v, re.I)
            idx.white_list_level = int(m.group(1)) if m else None
        elif k == "егпни":
            idx.egpni, idx.in_egpni = v, yes_no(v)
            m = re.search(r"уров\w*\s*(\d+)", v, re.I)
            idx.egpni_level = int(m.group(1)) if m else None
        elif k in ("перечень вак", "вак"):
            idx.vak, idx.in_vak = v, yes_no(v)
            # the card writes "да (категория 2)"; accept the "К2" spelling as well
            m = re.search(r"(?:категори\w*\s*|\b[КK]\s*)([1-3])\b", v, re.I)
            idx.vak_category = f"К{m.group(1)}" if m else None
        elif k == "rsci":
            idx.rsci, idx.in_rsci = v, yes_no(v)
        elif k in ("рейтинг science index", "рейтинг si"):
            idx.science_index_rating = v
            m = re.search(r"процентил\w*\s*(\d+)", v, re.I)
            idx.si_percentile = int(m.group(1)) if m else None
        else:
            idx.extra[f.label] = v
    return idx


def _parse_journal(sec: _Section, tree: LexborHTMLParser) -> JournalRef:
    lines = sec.lines()
    link = None
    for c in sec.cells:
        link = c.css_first('a[href*="contents.asp"]') or c.css_first('a[href*="title_about"]')
        if link is not None:
            break
    name = _inline(link) if link is not None else (lines[0].text if lines else None)
    if lines and name and _alnum(lines[0].text) == _alnum(name):
        lines = lines[1:]
    elif lines and not _LABEL_RE.match(lines[0].text):
        name = name or lines[0].text
        lines = lines[1:]
    fields, _ = _fields(lines)
    j = JournalRef(name=name or None)
    if link is not None and "contents.asp" in (link.attributes.get("href") or ""):
        j.contents_url = abs_url(link.attributes.get("href"))
    index_fields: list[_Field] = []
    for f in fields:
        k, v = f.key, f.value
        if k == "учредители":
            j.founders = v or None
        elif k == "издательство":
            j.publisher = v or None
        elif k == "issn":
            j.issn = v or None
        elif k == "eissn":
            j.eissn = v or None
        elif k in ("web of science", "scopus", "белый список", "егпни", "перечень вак", "вак", "rsci",
                   "рейтинг science index", "рейтинг si"):
            index_fields.append(f)
        else:
            j.extra[f.label] = v
    j.indexing = _parse_indexing(index_fields)
    cover = tree.css_first('a[href*="title_about_new.asp"]') or tree.css_first('a[href*="title_about.asp"]')
    if cover is not None:
        j.title_id = int_param(cover.attributes.get("href"), "id")
        if j.title_id:
            j.url = f"{BASE_URL}/title_about_new.asp?id={j.title_id}"
        img = cover.css_first("img[title]")
        m = re.search(r"О журнале\s*[«\"](.+)[»\"]\s*$", (img.attributes.get("title") or "") if img else "")
        if m:
            j.name_cased = clean(m.group(1))
    return j


def _parse_container(sec: _Section) -> ContainerRef:
    c = ContainerRef()
    link = None
    for cell in sec.cells:
        link = cell.css_first('a[href*="item.asp?id="]')
        if link is not None:
            break
    lines = sec.lines()
    if link is not None:
        c.id = int_param(link.attributes.get("href"), "id")
        c.title = _inline(link) or None
        c.url = f"{BASE_URL}/item.asp?id={c.id}" if c.id else None
        if lines and c.title and _alnum(lines[0].text) == _alnum(c.title):
            lines = lines[1:]
    ital = next((i for cell in sec.cells for i in cell.css("i")), None)
    if ital is not None:
        a_txt = _inline(ital)
        c.authors = [a.strip() for a in re.split(r",\s*", a_txt) if a.strip()]
        lines = [ln for ln in lines if ln.text != a_txt]
    fields, orphans = _fields(lines)
    desc: list[str] = [ln.text for ln in orphans]
    for f in fields:
        if f.key == "издательство":
            c.publisher = _org_from_field(f)
        elif f.key in {"сер", "серия"} or not f.value:
            desc.append(f"{f.label}: {f.value}".strip(": "))
        else:
            c.extra[f.label] = f.value
    c.description = clean(" ".join(desc)) or None
    c.year = source_year(c.description)
    return c


def _parse_conference(sec: _Section) -> ConferenceInfo:
    lines = [ln.text for ln in sec.lines()]
    info = ConferenceInfo(raw=clean(" ".join(lines)) or None)
    if not lines:
        return info
    info.name = lines[0]
    rest = clean(" ".join(lines[1:]))
    if rest:
        parts = [p.strip() for p in rest.split(",")]
        idx = next((i for i, p in enumerate(parts) if p[:1].isdigit()), None)
        if idx is None:
            info.place = rest
        else:
            info.place = ", ".join(parts[:idx]) or None
            info.dates = ", ".join(parts[idx:]) or None
    info.year = first_year(rest) or first_year(info.name)
    return info


def _keywords(cells: Iterable[LexborNode]) -> list[str]:
    out: list[str] = []
    cells = list(cells)
    for c in cells:
        out.extend(_inline(a) for a in c.css('a[href*="keyword_items"]'))
    if not out:
        text = clean(" ".join(_inline(c) for c in cells))
        text = re.sub(r"^(Keywords|Ключевые слова)\s*:\s*", "", text, flags=re.I)
        out = [k.strip() for k in re.split(r"[,;]\s*", text) if k.strip()]
    seen: set[str] = set()
    return [k for k in out if k and not (k in seen or seen.add(k))]


def _abstract_text(node: LexborNode) -> str | None:
    ps = node.css("p")
    text = "\n".join(t for t in (_inline(p) for p in ps) if t) if ps else _inline(node)
    text = re.sub(r"\s*(Показать полностью|Полностью|Скрыть)\s*$", "", text or "")
    return text.strip() or None


def _parse_abstract(cells: list[LexborNode], prefix: str = "abstract") -> str | None:
    divs = [d for c in cells for d in c.css(f'div[id^="{prefix}"]')]
    if divs:
        texts = [t for t in (_abstract_text(d) for d in divs) if t]
        return max(texts, key=len) if texts else None
    texts = [t for t in (_abstract_text(c) for c in cells) if t]
    return "\n".join(texts) if texts else None


def _parse_english(sec: _Section, pub: Publication) -> None:
    for cell in sec.cells:
        title_font = cell.css_first('font[color="#f26c4f" i]')
        if title_font is not None and not pub.title_en:
            pub.title_en = _inline(title_font) or None
        for _role, divs, tail in _split_cell(cell):
            pub.authors_en.extend(_parse_author_div(d).name for d in divs)
            affs = [a for a in (_parse_affiliation_segment(s) for s in _segments(tail)) if a]
            pub.affiliations_en.extend(a.name for a in affs)
        if cell.css_first('div[id^="eabstract"]') is not None:
            pub.abstract_en = _parse_abstract([cell], "eabstract")
        if cell.css_first('a[href*="keyword_items"]') is not None or re.match(r"\s*Keywords\s*:", node_text(cell)):
            pub.keywords_en = _keywords([cell])


def _parse_related(sec: _Section) -> list[RelatedItem]:
    name = sec.name.upper()
    relation = "translation" if "ПЕРЕВОД" in name else "original" if "ОРИГИНАЛ" in name else sec.name.lower()
    items: list[RelatedItem] = []
    cur: RelatedItem | None = None
    source: list[str] = []
    for ln in sec.lines():
        item_href = next((h for h in ln.hrefs() if "item.asp?id=" in h), None)
        is_title = item_href is not None and any(n.tag == "a" and "item.asp" in (n.attributes.get("href") or "")
                                                 and _alnum(_inline(n)) == _alnum(ln.text) for n in ln.nodes)
        if is_title:
            if cur is not None:
                cur.source = clean(" ".join(source)) or None
                items.append(cur)
            iid = int_param(item_href, "id")
            cur = RelatedItem(relation=relation, id=iid, title=ln.text,
                              url=f"{BASE_URL}/item.asp?id={iid}" if iid else None)
            source = []
        elif cur is not None and not cur.authors and not source and any(n.tag == "i" for n in ln.nodes):
            cur.authors = [a.strip() for a in re.split(r",\s*", ln.text) if a.strip()]
        elif cur is not None:
            source.append(ln.text)
    if cur is not None:
        cur.source = clean(" ".join(source)) or None
        items.append(cur)
    return items


def _parse_ipc(sec: _Section) -> list[IpcCode]:
    out: list[IpcCode] = []
    for cell in sec.cells:
        for tr in cell.css("tr"):
            tds = _children(tr, "td")
            if len(tds) >= 2:
                code = _inline(tds[0])
                if code:
                    out.append(IpcCode(code=code, description=_inline(tds[1]) or None))
    return out


def _parse_references(sec: _Section) -> list[str]:
    out: list[str] = []
    rows = [tr for c in sec.cells for tr in c.css("tr")]
    texts = [_inline(tr) for tr in rows] if rows else [ln.text for ln in sec.lines()]
    for t in texts:
        t = re.sub(r"^\d+\.\s*", "", clean(t))
        if t:
            out.append(t)
    return out


def _apply_metrics(pub: Publication, fields: list[_Field]) -> None:
    m = pub.metrics
    for f in fields:
        k, v = f.key, f.value
        if k == "входит в ринц":
            m.in_rinc = yes_no(v)
        elif k == "цитирований в ринц":
            m.rinc_citations = to_int(v)
        elif k == "входит в ядро ринц":
            m.in_core = yes_no(v)
        elif k == "цитирований из ядра ринц":
            m.core_citations = to_int(v)
        elif k == "рецензии":
            m.reviews = v or None
            low = v.lower()
            m.has_reviews = True if low.startswith(("есть", "да")) else False if low.startswith("нет") and "данных" not in low else None
        elif k.startswith("процентиль журнала"):
            m.journal_si_percentile = to_int(v)
        elif k == "просмотров":
            nums = _numbers(v)
            m.views = nums[0] if nums else None
            m.views_registered = nums[1] if len(nums) > 1 else None
        elif k == "загрузок":
            nums = _numbers(v)
            m.downloads = nums[0] if nums else None
            m.downloads_authors = nums[1] if len(nums) > 1 else None
        elif k == "включено в подборки":
            m.collections = to_int(v)
        elif k == "всего оценок":
            m.ratings_count = to_int(v)
        elif k == "средняя оценка":
            m.rating_avg = to_float(v)
        elif k == "всего отзывов":
            m.comments_count = to_int(v)
        elif v or f.label:
            m.extra[f.label] = v


_RUBRIC_KEYS = {
    "рубрика oecd": "oecd", "рубрика asjc": "asjc", "рубрика грнти": "grnti", "специальность вак": "vak",
    "приоритет снтр рф": "sntr", "приоритет снтр": "sntr",
}


def _apply_rubrics(pub: Publication, fields: list[_Field]) -> None:
    for f in fields:
        attr = _RUBRIC_KEYS.get(f.key)
        values = [v for v in (_none_if_no(x) for x in f.values) if v]
        if attr:
            setattr(pub.rubrics, attr, values)
        elif values:
            pub.rubrics.extra[f.label] = "; ".join(values)


def _parse_tools(tree: LexborHTMLParser, pub: Publication) -> None:
    panel = tree.css_first("div.right-panel")
    if panel is None:
        return
    ft = pub.fulltext
    seen: set[str] = set()
    for tr in panel.css("tr"):
        anchors = tr.css("a[href]")
        calls = []
        for a in anchors:
            m = _JS_CALL_RE.match(a.attributes.get("href") or "")
            if m and m.group(1) in ("file_article", "load_article", "url_article"):
                calls.append((m, a))
        direct = [a for a in anchors if re.search(r"\.pdf\b|full_text\.asp|/download/", a.attributes.get("href") or "", re.I)]
        if not calls and not direct:
            continue
        tds = _children(tr, "td")
        text_td = tds[-1] if tds else tr
        img = tr.css_first("img[src]")
        cm = _COLOUR_RE.search(img.attributes.get("src") or "") if img is not None else None
        colour = cm.group(1).lower() if cm else None
        for m, _a in calls:
            js = clean(f"{m.group(1)}({m.group(2)})")
            if js in seen:
                continue
            seen.add(js)
            args = _numbers(m.group(2))
            number = args[1] if len(args) > 1 else None
            if m.group(1) == "url_article":
                host_a = next((x for x in text_td.css("a[href]") if _inline(x)), None)
                host = _inline(host_a) if host_a is not None else None
                label = _inline(text_td)
                if host and label.endswith(host):
                    label = label[: -len(host)].strip()
                ft.links.append(FulltextLink(kind="url", number=number, label=label.rstrip(" ,") or None,
                                             host=host, colour=colour, js=js))
            else:
                label = _inline(text_td) or None
                fm = re.search(r"\((PDF|DJVU|DOC|DOCX|HTML|ZIP|RAR|EPUB)\)", label or "", re.I)
                ft.files.append(FulltextLink(kind="file", number=number, label=label,
                                             format=fm.group(1).upper() if fm else None, colour=colour, js=js))
        for a in direct:
            href = abs_url(a.attributes.get("href"))
            if href and href not in seen:
                seen.add(href)
                ft.files.append(FulltextLink(kind="file", label=_inline(a) or None, url=href, colour=colour))
    lines = _render(panel)
    text = " ".join(ln.text for ln in lines)
    pm = _PRICE_RE.search(text)
    if pm:
        ft.price_rub = to_int(pm.group(1))
    ft.can_add_link = "Добавить ссылку на полный текст" in text
    ft.notes = [ln.text for ln in lines if re.search(
        r"полн\w+ текст|приобрести|заказ|на сайте|загрузить|связанные документы|по подписке", ln.text, re.I)]
    colours = [x.colour for x in ft.files + ft.links if x.colour]
    ft.colour = colours[0] if colours else None
    ft.available = bool(ft.files)
    if ft.files:
        ft.access = COLOUR_ACCESS.get(ft.colour or "green", "open")  # type: ignore[assignment]
    elif ft.price_rub is not None:
        ft.access = "paid"
    elif any(re.search(r"полн\w+ текст", x.label or "", re.I) for x in ft.links):
        ft.access = "external"
    elif ft.colour:
        ft.access = COLOUR_ACCESS.get(ft.colour)  # type: ignore[assignment]
    fields, _ = _fields(lines)
    for f in fields:
        if f.key == "дата размещения":
            pub.dates.placed = _to_date(f.value)
    for a in panel.css('a[href*="contents.asp?id="]'):
        pub.issue_id = pub.issue_id or int_param(a.attributes.get("href"), "id")


# --------------------------------------------------------------------------- label/value block


@dataclass
class _Ctx:
    book: dict = field(default_factory=dict)
    patent: dict = field(default_factory=dict)
    disser: dict = field(default_factory=dict)
    #: (label, value, groups) of labels that only feed BookInfo/PatentInfo/DissertationInfo; they
    #: go to ``extra`` when none of their info blocks is created, so nothing is lost.
    raw: list[tuple[str, str, frozenset[str]]] = field(default_factory=list)


_BOOK, _PATENT, _DISSER = frozenset({"book"}), frozenset({"patent"}), frozenset({"disser"})
_FIELD_GROUPS: dict[str, frozenset[str]] = {
    "isbn": _BOOK, "место издания": _BOOK, "издательство": _BOOK, "серия": _BOOK, "издание": _BOOK,
    "номер издания": _BOOK, "тираж": _BOOK, "ббк": _BOOK,
    "число страниц": _BOOK | _DISSER, "город": _BOOK | _DISSER,
    "номер патента": _PATENT, "номер свидетельства": _PATENT, "номер документа": _PATENT,
    "патентное ведомство": _PATENT, "номер заявки": _PATENT, "дата регистрации": _PATENT,
    "дата приоритета": _PATENT, "приоритет": _PATENT, "патентообладатели": _PATENT,
    "патентообладатель": _PATENT, "правообладатели": _PATENT, "правообладатель": _PATENT,
    "место защиты": _DISSER, "специальность": _DISSER, "ученая степень": _DISSER, "степень": _DISSER,
}


def _apply_main_fields(pub: Publication, fields: list[_Field], ctx: _Ctx) -> None:
    for f in fields:
        k, v = f.key, f.value
        if k == "elibrary id":
            continue
        groups = _FIELD_GROUPS.get(k)
        if groups:
            ctx.raw.append((f.label, v, groups))
        if k == "edn":
            pub.edn = pub.edn or (v.upper() or None)
        elif k == "doi":
            pub.doi = pub.doi or (v or None)
        elif k == "тип":
            pub.type_raw = v or None
        elif k == "язык":
            pub.language = v or None
            pub.language_code = LANGUAGE_CODES.get(v.lower()) if v else None
        elif k == "том":
            pub.volume = v or None
        elif k in ("выпуск", "номер"):
            pub.issue = v or None
            for h in f.hrefs():
                if "contents.asp" in h:
                    pub.issue_id = int_param(h, "id")
        elif k in ("год", "год издания", "год публикации"):
            pub.year = pub.year or to_int(v)
            if k == "год публикации":
                ctx.patent["publication_year"] = to_int(v)
            if k == "год издания":
                ctx.book["year"] = to_int(v)
        elif k == "страницы":
            pub.pages = v or None
        elif k == "поступила в редакцию":
            pub.dates.received = _to_date(v)
        elif k.startswith("принята"):
            pub.dates.accepted = _to_date(v)
        elif k == "дата публикации":
            pub.dates.published = _to_date(v)
            ctx.patent["publication_date"] = _to_date(v)
        elif k == "дата депонирования":
            pub.dates.deposited = _to_date(v)
        elif k == "дата защиты":
            pub.dates.defended = _to_date(v)
            ctx.disser["defense_date"] = _to_date(v)
        elif k == "isbn":
            ctx.book["isbn"] = [x for x in re.split(r"[,;]\s*|\s+(?=97[89])", v) if x.strip()]
        elif k == "место издания":
            ctx.book["city"] = v or None
        elif k == "число страниц":
            ctx.book["pages_total"] = to_int(v)
            ctx.disser["pages_total"] = to_int(v)
        elif k == "издательство":
            ctx.book["publisher"] = _org_from_field(f)
        elif k == "серия":
            ctx.book["series"] = v or None
        elif k in ("издание", "номер издания"):
            ctx.book["edition"] = v or None
        elif k == "тираж":
            ctx.book["circulation"] = to_int(v)
        elif k == "удк":
            pub.udk = v or None
            ctx.book["udk"] = v or None
        elif k == "ббк":
            ctx.book["bbk"] = v or None
        elif k in ("номер патента", "номер свидетельства", "номер документа"):
            ctx.patent["number"] = v or None
        elif k == "патентное ведомство":
            ctx.patent["office"] = v or None
        elif k == "номер заявки":
            ctx.patent["application_number"] = v or None
        elif k == "дата регистрации":
            ctx.patent["registration_date"] = _to_date(v)
        elif k in ("дата приоритета", "приоритет"):
            ctx.patent["priority_date"] = _to_date(v)
        elif k in ("патентообладатели", "патентообладатель", "правообладатели", "правообладатель"):
            ctx.patent["holders"] = _orgs_from_field(f)
        elif k == "место защиты":
            ctx.disser["defense_place"] = _org_from_field(f)
        elif k == "город":
            ctx.disser["city"] = v or None
            ctx.book.setdefault("city", v or None)
        elif k == "специальность":
            ctx.disser["specialty"] = v or None
            m = re.match(r"^\s*([\d.]+\d)", v)
            ctx.disser["specialty_code"] = m.group(1) if m else None
        elif k in ("ученая степень", "степень"):
            ctx.disser["degree"] = v or None
        else:
            pub.extra[f.label] = v


def _finish_kind_specific(pub: Publication, ctx: _Ctx) -> None:
    if pub.kind == "patent" or ctx.patent.get("number") or ctx.patent.get("holders"):
        number = ctx.patent.get("number")
        country = kind_code = None
        if number:
            m = re.match(r"^([A-Z]{2})\s+(.+?)(?:\s+([A-Z]\d?))?$", number)
            if m:
                country, kind_code = m.group(1), m.group(3)
        pub.patent = PatentInfo(
            patent_type=pub.type_raw, country_code=country, kind_code=kind_code,
            **{k: v for k, v in ctx.patent.items() if v is not None},
        )
    if pub.kind == "dissertation" or ctx.disser.get("degree") or ctx.disser.get("defense_place"):
        raw = pub.type_raw or None
        pub.dissertation = DissertationInfo(
            document=raw, is_autoreferat=bool(raw and "автореферат" in raw.lower()),
            **{k: v for k, v in ctx.disser.items() if v is not None},
        )
    book_keys = {k for k, v in ctx.book.items() if v}
    bookish = pub.kind in ("book", "proceedings") or bool(
        book_keys & {"isbn", "publisher", "pages_total", "series", "edition"})
    if bookish and (pub.kind != "dissertation" or book_keys & {"isbn", "publisher"}):
        data = {k: v for k, v in ctx.book.items() if v is not None}
        if pub.kind == "dissertation":
            data.pop("pages_total", None)
            data.pop("city", None)
        data.setdefault("year", pub.year)
        pub.book = BookInfo(**data)
    created = {name for name, obj in (("book", pub.book), ("patent", pub.patent), ("disser", pub.dissertation))
               if obj is not None}
    for label, value, groups in ctx.raw:
        if not groups & created:
            pub.extra.setdefault(label, value)


# --------------------------------------------------------------------------- entry points


def _content_root(tree: LexborHTMLParser) -> LexborNode | None:
    title_p = tree.css_first("p.bigtext")
    if title_p is not None:
        table = _closest(title_p, "table")
        td = _closest(table, "td") if table is not None else None
        if td is not None:
            return td
    for td in tree.css("td"):
        if node_text(td).startswith("eLIBRARY ID:"):
            return _closest(_closest(td, "table"), "td") or td
    return None


def parse_publication(html: str | bytes, *, url: str | None = None) -> Publication:
    """Parse ``item.asp?id=...`` into a :class:`Publication`.

    Raises :class:`CaptchaRequired` for the captcha page, :class:`NotFound` when the page says the
    publication does not exist and :class:`ParseError` for any other page.
    """
    tree = parse_html(html)
    if is_captcha_page(tree):
        raise CaptchaRequired(url)
    for sel in ("script", "style", "noscript"):
        for n in tree.css(sel):
            n.decompose()
    content = _content_root(tree)
    body_text = node_text(tree.body)
    id_match = re.search(r"eLIBRARY ID:\s*(\d+)", body_text)
    if content is None or (id_match is None and tree.css_first("p.bigtext") is None):
        if _NOT_FOUND_RE.search(body_text):
            raise NotFound("Публикация не найдена на eLIBRARY.RU", url=url)
        raise ParseError("Страница не похожа на карточку публикации eLIBRARY.RU (item.asp)", url=url)

    # ---- identifiers
    item_id = to_int(id_match.group(1)) if id_match else None
    if item_id is None:
        inp = tree.css_first('input[name="id"]')
        item_id = to_int(inp.attributes.get("value")) if inp is not None else None
    if item_id is None:
        canon = tree.css_first('link[rel="canonical"]')
        item_id = int_param(canon.attributes.get("href") if canon is not None else None, "id")
    if item_id is None:
        item_id = int_param(url, "id")
    if item_id is None:
        raise ParseError("Не найден eLIBRARY ID на карточке публикации", url=url)

    title_p = tree.css_first("p.bigtext")
    display = _inline(title_p) if title_p is not None else ""
    page_title = _page_title(tree)
    title = page_title or display
    title_info = None
    if title_p is not None:
        holder = _closest(title_p, "span") or _closest(title_p, "td")
        if holder is not None:
            full = _inline(holder)
            if display and full.startswith(display):
                title_info = clean(full[len(display):]) or None
    if not title:
        raise ParseError("На карточке публикации нет названия", url=url)

    pub = Publication(id=item_id, title=title, title_display=display or None, title_info=title_info,
                      url=f"{BASE_URL}/item.asp?id={item_id}")
    doi_a = content.css_first('a[href*="doi.org/"]')
    if doi_a is not None:
        pub.doi = _inline(doi_a) or None
    if not pub.doi:
        meta = tree.css_first('meta[name="doi"]')
        if meta is not None:
            pub.doi = clean(meta.attributes.get("content")) or None
    edn_m = re.search(r"EDN:\s*([A-Z0-9]{4,})", body_text)
    pub.edn = edn_m.group(1) if edn_m else None

    sections = _find_sections(content)
    section_tables = {s.table.mem_id for s in sections if s.table is not None}

    # ---- label/value block ("Тип: ..."), skipping nested sections (IPC table of patents)
    label_table = None
    for td in content.css("td"):
        if re.match(r"^(Тип|Язык|Год( издания| публикации)?|Номер патента)\s*:", node_text(td)) \
                and not _is_inside(td, section_tables):
            label_table = _closest(td, "table")
            break
    exclude = set(section_tables)
    ctx = _Ctx()
    if label_table is not None:
        exclude.add(label_table.mem_id)
        nested = frozenset(section_tables)
        fields, orphans = _fields(_render(label_table, skip=nested))
        _apply_main_fields(pub, fields, ctx)
        if orphans:
            pub.extra["Описание"] = clean(" ".join(ln.text for ln in orphans))

    # ---- authors and affiliations
    groups = _author_groups(content, exclude)
    for g in groups:
        if g.role is None and not pub.authors:
            pub.authors = g.authors
            pub.affiliations = g.affiliations
        else:
            pub.contributors.extend(g.authors)

    # ---- sections
    for sec in sections:
        name = sec.name.upper()
        if name in ("ВАША ЗАМЕТКА", "ОБСУЖДЕНИЕ"):
            continue
        if name == "ЖУРНАЛ":
            pub.journal = _parse_journal(sec, tree)
            if pub.issue_id is None and pub.journal.contents_url:
                pub.issue_id = int_param(pub.journal.contents_url, "id")
        elif name == "ИСТОЧНИК":
            pub.container = _parse_container(sec)
        elif name == "КОНФЕРЕНЦИЯ":
            pub.conference = _parse_conference(sec)
        elif name == "КЛЮЧЕВЫЕ СЛОВА":
            pub.keywords = _keywords(sec.cells)
        elif name in ("АННОТАЦИЯ", "РЕФЕРАТ", "ОПИСАНИЕ"):
            pub.abstract = _parse_abstract(sec.cells)
        elif name.startswith("БИБЛИОМЕТРИЧЕСКИЕ") or name == "АЛЬТМЕТРИКИ":
            _apply_metrics(pub, _fields(sec.lines())[0])
        elif name.startswith("ТЕМАТИЧЕСКИЕ"):
            _apply_rubrics(pub, _fields(sec.lines())[0])
        elif "ФИНАНСОВОЙ ПОДДЕРЖК" in name or name == "ФИНАНСИРОВАНИЕ":
            pub.funding = sec.text() or None
        elif "ВЕРСИЯ" in name or "ВЕРСИИ" in name:
            pub.related.extend(_parse_related(sec))
        elif "НА АНГЛИЙСКОМ" in name:
            _parse_english(sec, pub)
        elif "ПАТЕНТНАЯ КЛАССИФИКАЦИЯ" in name or name == "МПК":
            ctx.patent["ipc"] = _parse_ipc(sec)
        elif name in ("ОГЛАВЛЕНИЕ", "СОДЕРЖАНИЕ"):
            pass  # needs the kind; handled below
        elif "ЛИТЕРАТУР" in name or "ЦИТИРУЕМ" in name:
            pub.references = _parse_references(sec)
        else:
            text = sec.text()
            if text:
                pub.extra[sec.name.capitalize()] = text

    pub.kind = normalize_kind(pub.type_raw, journal=pub.journal is not None,
                              container=pub.container is not None, conference=pub.conference is not None)
    for sec in sections:
        if sec.name.upper() in ("ОГЛАВЛЕНИЕ", "СОДЕРЖАНИЕ") and sec.table is not None:
            default = "conference" if pub.kind == "proceedings" else "chapter"
            pub.contents = [h for c in sec.cells for h in parse_publication_rows(c, default_kind=default)]

    _finish_kind_specific(pub, ctx)
    if pub.year is None:
        pub.year = (ctx.patent.get("publication_year")
                    or (pub.dates.published.year if pub.dates.published else None)
                    or (pub.container.year if pub.container else None))

    _parse_tools(tree, pub)
    cover = tree.css_first('img[src*="/itemfiles/"]')
    if cover is not None:
        pub.cover_url = abs_url(cover.attributes.get("src"))
    return pub


def parse_fulltext_flag(html: str | bytes, *, item_id: int | None = None) -> FulltextFlag:
    """Parse the ``ispdf.asp?itemid=ID`` fragment (the full-text icon of a result row).

    An empty fragment means there is no full text in eLIBRARY's records.
    """
    tree = parse_html(html)
    if is_captcha_page(tree):
        raise CaptchaRequired()
    flag = FulltextFlag(item_id=item_id)
    img = tree.css_first("img[src]")
    link = tree.css_first("a[href]")
    if link is not None:
        m = _JS_CALL_RE.match(link.attributes.get("href") or "")
        if m:
            flag.action = m.group(1)
            nums = _numbers(m.group(2))
            if nums and flag.item_id is None:
                flag.item_id = nums[0]
    if img is None:
        return flag
    cm = _COLOUR_RE.search(img.attributes.get("src") or "")
    if cm is None:
        return flag
    flag.available = True
    flag.colour = cm.group(1).lower()
    flag.access = COLOUR_ACCESS.get(flag.colour)  # type: ignore[assignment]
    flag.title = clean(img.attributes.get("title")) or None
    pm = _PRICE_RE.search(flag.title or "")
    if pm:
        flag.price_rub = to_int(pm.group(1))
    return flag
