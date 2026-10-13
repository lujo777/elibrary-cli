"""Citation export: CSL-JSON, BibTeX (biblatex flavour), RIS and native Zotero API item JSON.

All functions are pure: they take a parsed :class:`~elibrary_cli.models.publications.Publication`
(the ``item.asp`` card) or a :class:`~elibrary_cli.models.base.PublicationHit` (a row of a result
list) and never touch the network.

Both inputs are first normalised into one internal record (``_Record``); every format is rendered
from that record, so the four formats always agree. A ``PublicationHit`` only has a title, author
names and a bibliographic line (``source``), so the line is parsed best-effort (journal, volume,
issue, pages, proceedings title, place, patent number, application...).

Conventions (see ``docs/re/zotero.md``):

* Titles shown in capitals (rows, patents) are converted to sentence case (``fix_caps=True``):
  Latin words inside Cyrillic titles, words with digits and Roman numerals are kept as they are.
* Names: ``"БЛОХИНА Т.М."`` + tooltip ``"Блохина (Трубченкова) Таисия Михайловна"`` ->
  ``lastName="Блохина", firstName="Таисия Михайловна"``; without a tooltip ``firstName="Т. М."``.
* Every format carries the eLIBRARY ID, the EDN and the RSCI (РИНЦ) citation count where it has a
  place for them: Zotero ``extra``, CSL ``note``, RIS ``N1``/``AN``, BibTeX ``edn``.
* The Zotero JSON uses only the fields of Zotero schema v42 that are valid for the item type (the
  Web API answers 400 to anything else); values without a field go to ``extra`` as CSL variables
  (``event-title: ...``), which Zotero understands.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from elibrary_cli.models.base import PublicationHit
from elibrary_cli.models.publications import AuthorRef, Publication

Exportable = Publication | PublicationHit

# --------------------------------------------------------------------------- public constants

#: Canonical export formats.
FORMATS: tuple[str, ...] = ("bibtex", "ris", "csl", "zotero")
#: Accepted spellings of the formats.
FORMAT_ALIASES: dict[str, str] = {
    "bibtex": "bibtex", "bib": "bibtex", "biblatex": "bibtex",
    "ris": "ris",
    "csl": "csl", "csl-json": "csl", "csl_json": "csl", "csljson": "csl", "json": "csl",
    "zotero": "zotero", "zotero-json": "zotero", "zotero_json": "zotero", "zotero-api": "zotero",
}
FILE_EXTENSIONS: dict[str, str] = {"bibtex": ".bib", "ris": ".ris", "csl": ".json", "zotero": ".json"}
MEDIA_TYPES: dict[str, str] = {
    "bibtex": "application/x-bibtex",
    "ris": "application/x-research-info-systems",
    "csl": "application/vnd.citationstyles.csl+json",
    "zotero": "application/json",
}
#: Value of Zotero's ``libraryCatalog`` (the label Zotero's own eLibrary.ru translator uses).
LIBRARY_CATALOG = "eLibrary.ru"

#: Internal kind -> (CSL type, Zotero itemType, BibTeX entry type, RIS TY).
_TYPES: dict[str, tuple[str, str, str, str]] = {
    "article": ("article-journal", "journalArticle", "article", "JOUR"),
    "conference": ("paper-conference", "conferencePaper", "inproceedings", "CPAPER"),
    "proceedings": ("book", "book", "proceedings", "BOOK"),  # RIS CONF is a conferencePaper in Zotero
    "book": ("book", "book", "book", "BOOK"),
    "chapter": ("chapter", "bookSection", "incollection", "CHAP"),
    "thesis": ("thesis", "thesis", "thesis", "THES"),
    "patent": ("patent", "patent", "patent", "PAT"),
    "report": ("report", "report", "techreport", "RPRT"),
    "grant": ("report", "report", "techreport", "RPRT"),
    "manuscript": ("manuscript", "manuscript", "unpublished", "MANSCPT"),
    "preprint": ("article", "preprint", "misc", "UNPB"),
    "dataset": ("dataset", "dataset", "dataset", "DATA"),
    "other": ("document", "document", "misc", "GEN"),
}

# Zotero schema v42 (https://api.zotero.org/schema): valid fields per item type, in schema order.
_ZOTERO_FIELDS: dict[str, tuple[str, ...]] = {k: tuple(v.split()) for k, v in {
    "journalArticle": "title abstractNote publicationTitle volume issue pages date series seriesTitle "
                      "seriesText journalAbbreviation language DOI ISSN shortTitle url accessDate archive "
                      "archiveLocation libraryCatalog callNumber rights extra",
    "conferencePaper": "title abstractNote proceedingsTitle conferenceName publisher place date eventPlace "
                       "volume issue numberOfVolumes pages series seriesNumber language DOI ISBN ISSN "
                       "shortTitle url accessDate archive archiveLocation libraryCatalog callNumber rights "
                       "extra",
    "book": "title abstractNote series seriesNumber volume numberOfVolumes edition place publisher date "
            "numPages language ISBN shortTitle url accessDate archive archiveLocation libraryCatalog "
            "callNumber rights extra DOI ISSN",
    "bookSection": "title abstractNote bookTitle series seriesNumber volume numberOfVolumes edition place "
                   "publisher date pages language ISBN shortTitle url accessDate archive archiveLocation "
                   "libraryCatalog callNumber rights extra DOI ISSN",
    "thesis": "title abstractNote thesisType university place date numPages language shortTitle url "
              "accessDate archive archiveLocation libraryCatalog callNumber rights extra DOI ISBN ISSN "
              "series seriesNumber",
    "patent": "title abstractNote place country assignee issuingAuthority patentNumber filingDate pages "
              "applicationNumber priorityNumbers issueDate priorityDate references legalStatus language "
              "shortTitle url accessDate rights extra DOI",
    "report": "title abstractNote reportNumber reportType seriesTitle place institution date pages language "
              "shortTitle url accessDate archive archiveLocation libraryCatalog callNumber rights extra DOI "
              "ISBN ISSN seriesNumber",
    "manuscript": "title abstractNote manuscriptType place date numPages language shortTitle url accessDate "
                  "archive archiveLocation libraryCatalog callNumber rights extra institution number DOI",
    "preprint": "title abstractNote genre repository archiveID place date series seriesNumber DOI "
                "shortTitle url accessDate archive archiveLocation libraryCatalog callNumber rights extra "
                "language",
    "dataset": "title abstractNote identifier type versionNumber date repository repositoryLocation format "
               "DOI shortTitle url accessDate archive archiveLocation libraryCatalog callNumber rights "
               "extra language",
    "document": "title abstractNote type publisher place date language DOI shortTitle url accessDate archive "
                "archiveLocation libraryCatalog callNumber rights extra",
}.items()}

# Valid creator types per item type (schema v42); the first one is the primary type.
_ZOTERO_CREATORS: dict[str, tuple[str, ...]] = {
    "journalArticle": ("author", "contributor", "editor", "translator", "reviewedAuthor"),
    "conferencePaper": ("author", "contributor", "editor", "translator", "seriesEditor"),
    "book": ("author", "contributor", "editor", "translator", "seriesEditor"),
    "bookSection": ("author", "contributor", "editor", "bookAuthor", "translator", "seriesEditor"),
    "thesis": ("author", "contributor"),
    "patent": ("inventor", "attorneyAgent", "contributor"),
    "report": ("author", "editor", "contributor", "translator", "seriesEditor"),
    "manuscript": ("author", "contributor", "translator"),
    "preprint": ("author", "contributor", "editor", "translator", "reviewedAuthor"),
    "dataset": ("author", "contributor"),
    "document": ("author", "contributor", "editor", "translator", "reviewedAuthor"),
}

# Base field names (used while building) -> type-specific Zotero field names.
_ZOTERO_BASE: dict[str, dict[str, str]] = {
    "bookSection": {"publicationTitle": "bookTitle"},
    "conferencePaper": {"publicationTitle": "proceedingsTitle"},
    "thesis": {"publisher": "university", "type": "thesisType"},
    "patent": {"date": "issueDate", "number": "patentNumber", "authority": "issuingAuthority"},
    "report": {"number": "reportNumber", "type": "reportType", "publisher": "institution",
               "series": "seriesTitle"},
    "manuscript": {"type": "manuscriptType", "publisher": "institution"},
    "preprint": {"type": "genre", "publisher": "repository", "number": "archiveID"},
    "dataset": {"number": "identifier", "publisher": "repository", "place": "repositoryLocation"},
}

# A value with no Zotero field for the item type is kept in ``extra`` under its CSL variable name.
_ZOTERO_TO_CSL_VAR: dict[str, str] = {
    "publicationTitle": "container-title", "conferenceName": "event-title", "eventPlace": "event-place",
    "volume": "volume", "issue": "issue", "pages": "page", "numPages": "number-of-pages",
    "publisher": "publisher", "place": "publisher-place", "series": "collection-title",
    "edition": "edition", "ISSN": "ISSN", "ISBN": "ISBN", "DOI": "DOI", "type": "genre",
    "number": "number",
}

_LANG_BABEL = {"ru": "russian", "en": "english", "uk": "ukrainian", "be": "belarusian", "kk": "kazakh",
               "de": "german", "fr": "french", "es": "spanish", "it": "italian", "pl": "polish",
               "bg": "bulgarian", "sr": "serbian", "zh": "chinese", "tt": "tatar", "uz": "uzbek"}

# --------------------------------------------------------------------------- text helpers

_WS_RE = re.compile(r"\s+")
_CYR_RE = re.compile(r"[А-Яа-яЁё]")
_LAT_RE = re.compile(r"[A-Za-z]")
_ROMAN_RE = re.compile(r"^(?=[MDCLXVI])M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$")
_ORDINAL_RE = re.compile(r"^\d+(?:ST|ND|RD|TH|st|nd|rd|th)$")
# Frequent Russian acronyms kept in capitals when a title displayed in capitals is re-cased.
_RU_ACRONYMS = frozenset("""
    РФ РАН РАМН РАО СССР США ЕС СНГ КНР ООН ЮНЕСКО ВОЗ ЭВМ ПК ПО ОС ИИ ИТ ИКТ ИС АСУ АСУТП САПР ЧПУ ПЛИС ЦОД
    БД СУБД ГИС ДЗЗ БПЛА БАС ЛЭП АЭС ГЭС ТЭЦ ТЭС ЖКХ ДТП МЧС МВД ФСБ ВВП НДС ФЗ ГОСТ ОСТ СНИП ВАК РИНЦ НИР
    НИОКР ОКР НИИ ВУЗ ВУЗА ВУЗОВ МГУ СПБГУ МФТИ МГТУ МИФИ ВШЭ ДНК РНК МРНК ВИЧ СПИД ОРВИ COVID МРТ КТ ЭКГ ЭЭГ
    УЗИ ЯМР ЭПР ИК УФ СВЧ ВЧ НЧ АЦП ЦАП ДВС ГТД АПК ИПМ ЦНИГРИ ЕГЭ ОГЭ ФГОС СМИ IT
""".split())  # noqa: SIM905
_EN_SMALL = frozenset("a an the and or nor but of on in at to for by with from as via vs per into".split())  # noqa: SIM905


def _clean(text: Any) -> str:
    if text is None:
        return ""
    return _WS_RE.sub(" ", str(text).replace("\xa0", " ")).strip()


def _is_caps(text: str, min_letters: int = 4) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < min_letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.9


def _cap_first(word: str) -> str:
    """Upper-case the first letter, unless it is glued to digits (``12th``, ``3d``)."""
    for i, c in enumerate(word):
        if c.isalpha():
            if i and word[i - 1].isdigit():
                return word
            return word[:i] + c.upper() + word[i + 1:]
    return word


def _keep_token(core: str, mainly_cyrillic: bool) -> bool:
    """Tokens of an ALL-CAPS text that keep their case: words with digits (``γН2АХ``, ``3D``),
    Roman numerals (``XV``) and, inside a Cyrillic text, Latin words (``PYTHON``, ``MATLAB``)."""
    if not core:
        return True
    if _ORDINAL_RE.match(core):
        return False
    if any(c.isdigit() for c in core):
        return True
    if core.upper() in _RU_ACRONYMS:
        return True
    if _LAT_RE.search(core) and _CYR_RE.search(core):  # mixed script: an acronym or a brand
        return True
    if _ROMAN_RE.match(core.replace("Х", "X").replace("І", "I")):
        return True
    return bool(mainly_cyrillic and _LAT_RE.search(core) and not _CYR_RE.search(core))


def normalize_caps(text: str | None, *, mode: str = "sentence") -> str:
    """Convert a title displayed in capitals to a normal case; other texts are returned unchanged.

    ``mode``: ``"sentence"`` (titles: first word and words after ``. ! ?`` capitalised), ``"title"``
    (journal/proceedings/conference names: Title Case for Latin texts, sentence case for Cyrillic
    ones), ``"lower"`` (keywords/tags).
    """
    text = _clean(text)
    if not text or not _is_caps(text, 2 if mode == "lower" else 4):
        return text
    mainly_cyrillic = len(_CYR_RE.findall(text)) >= len(_LAT_RE.findall(text))
    title_case = mode == "title" and not mainly_cyrillic
    out: list[str] = []
    start = mode != "lower"
    after_colon = False
    for tok in text.split(" "):
        m = re.match(r"^(\W*)(.*?)(\W*)$", tok)
        lead, core, trail = (m.group(1), m.group(2), m.group(3)) if m else ("", tok, "")
        parts = [p for p in re.split(r"([-‐–/.])", core) if p]
        new = "".join(p if (p in "-‐–/." or _keep_token(p, mainly_cyrillic)) else p.lower() for p in parts)
        first_word = next((p for p in parts if any(c.isalpha() for c in p)), "")
        if not _keep_token(first_word, mainly_cyrillic) and (
                start or (title_case and (after_colon or new.lower() not in _EN_SMALL))):
            new = _cap_first(new)
        out.append(lead + new + trail)
        # a new sentence starts after "!", "?" or a full stop that does not end a short abbreviation
        start = mode != "lower" and (tok.endswith(("!", "?")) or (tok.endswith(".") and len(core) >= 4))
        after_colon = tok.endswith(":")
    return " ".join(out)


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya", "є": "ye", "і": "i", "ї": "yi", "ґ": "g", "ў": "u",
}


def transliterate(text: str) -> str:
    """Russian (and Ukrainian/Belarusian) Cyrillic -> Latin (BGN/PCGN-like, ASCII only), e.g.
    ``"Мифтахова"`` -> ``"Miftakhova"``. Other non-ASCII letters lose their diacritics or are dropped."""
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.capitalize() if ch != low and t else t)
        else:
            out.append(ch)
    return unicodedata.normalize("NFKD", "".join(out)).encode("ascii", "ignore").decode("ascii")


# --------------------------------------------------------------------------- names


@dataclass
class _Person:
    family: str = ""
    given: str = ""
    literal: str = ""

    def zotero(self, creator_type: str) -> dict[str, str]:
        if self.literal:
            return {"creatorType": creator_type, "name": self.literal}
        return {"creatorType": creator_type, "lastName": self.family, "firstName": self.given}

    def csl(self) -> dict[str, str]:
        if self.literal:
            return {"literal": self.literal}
        d = {"family": self.family}
        if self.given:
            d["given"] = self.given
        return d

    def inverted(self) -> str:
        if self.literal:
            return self.literal
        return f"{self.family}, {self.given}" if self.given else self.family


_INITIALS_RE = re.compile(
    r"^(?P<last>.+?)\s+(?P<init>(?:[A-ZА-ЯЁ][a-zа-яё]{0,2}\.\s*-?\s*)*(?:[A-ZА-ЯЁ][a-zа-яё]{0,2}\.|[A-ZА-ЯЁ]))$")
_INITIAL_RE = re.compile(r"[A-ZА-ЯЁ][a-zа-яё]{0,2}\.?")
_FULL_RE = re.compile(r"^(?P<last>[^\s()]+)\s*(?:\((?P<alt>[^)]*)\))?\s+(?P<given>[^()]+)$")


def _fix_name_case(name: str) -> str:
    """``"БЛОХИНА"`` -> ``"Блохина"``, ``"АЛЬ-МЕШХЕДАНИ"`` -> ``"Аль-Мешхедани"``; mixed case is kept."""
    if not any(c.isalpha() for c in name) or not name.isupper():
        return name
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), name)


def _initials(given: str) -> list[str]:
    return [w[0].upper() for w in re.split(r"[\s.\-]+", given) if w and w[0].isalpha()]


def split_person_name(display: str, full_name: str | None = None) -> dict[str, str]:
    """Split an eLIBRARY person name into Zotero creator parts.

    ``display`` is the name as shown (``"Иванов И.И."``, ``"БЛОХИНА Т.М."``, ``"Petrov V.A."``);
    ``full_name`` is the optional tooltip (``"Блохина (Трубченкова) Таисия Михайловна"``). The full
    given names are used when the tooltip belongs to the same person (surname or maiden name and the
    initials match). Returns ``{"lastName": ..., "firstName": ...}`` or ``{"name": ...}`` for
    organisations and other single-field names.
    """
    display = _clean(display)
    full = _clean(full_name)
    disp_last = disp_init = ""
    m = _INITIALS_RE.match(display)
    if m:
        disp_last = _fix_name_case(m.group("last").strip(" ,"))
        disp_init = " ".join(i if i.endswith(".") else i + "." for i in _INITIAL_RE.findall(m.group("init")))
    fm = _FULL_RE.match(full) if full else None
    if fm:
        f_last = _fix_name_case(fm.group("last"))
        f_alt = _fix_name_case(_clean(fm.group("alt"))) if fm.group("alt") else ""
        f_given = _fix_name_case(_clean(fm.group("given")))
        if not disp_last and not display:
            return {"lastName": f_last, "firstName": f_given}
        if disp_last:
            same_name = disp_last.casefold() in {f_last.casefold(), f_alt.casefold()}
            want = _initials(disp_init)
            have = _initials(f_given)
            if same_name and have[: len(want)] == want[: len(have)] and want[:1] == have[:1]:
                last = f_last if disp_last.casefold() == f_last.casefold() else disp_last
                return {"lastName": last, "firstName": f_given}
        elif display.casefold().split()[:1] == [f_last.casefold()]:
            return {"lastName": f_last, "firstName": f_given}
    if disp_last:
        return {"lastName": disp_last, "firstName": disp_init}
    words = display.split()
    if not words:
        return {"name": ""}
    looks_person = (
        2 <= len(words) <= 3
        and all(re.fullmatch(r"[^\W\d_]+(?:-[^\W\d_]+)*", w) for w in words)
        and (not display.isupper() or all(len(w) >= 4 for w in words))
    )
    if looks_person:
        return {"lastName": _fix_name_case(words[0]), "firstName": _fix_name_case(" ".join(words[1:]))}
    if len(words) == 1 and not (display.isupper() and len(display) <= 5):
        return {"lastName": _fix_name_case(display), "firstName": ""}
    return {"name": display}


def _person(display: str, full_name: str | None = None) -> _Person:
    parts = split_person_name(display, full_name)
    if "name" in parts:
        return _Person(literal=parts["name"])
    return _Person(family=parts["lastName"], given=parts["firstName"])


def _person_ref(a: AuthorRef) -> _Person:
    return _person(a.name, a.full_name)


# --------------------------------------------------------------------------- the record


@dataclass
class _Record:
    id: int
    url: str
    kind: str
    title: str
    edn: str | None = None
    doi: str | None = None
    title_en: str | None = None
    authors: list[_Person] = field(default_factory=list)
    editors: list[_Person] = field(default_factory=list)
    translators: list[_Person] = field(default_factory=list)
    contributors: list[_Person] = field(default_factory=list)
    container_authors: list[_Person] = field(default_factory=list)
    container_title: str | None = None
    conference_name: str | None = None
    event_place: str | None = None
    event_dates: str | None = None
    year: int | None = None
    issued: date | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    num_pages: int | None = None
    publisher: str | None = None
    place: str | None = None
    issn: str | None = None
    isbn: list[str] = field(default_factory=list)
    series: str | None = None
    edition: str | None = None
    language: str | None = None
    abstract: str | None = None
    keywords: list[str] = field(default_factory=list)
    genre: str | None = None
    number: str | None = None
    application_number: str | None = None
    filing_date: date | None = None
    priority_date: date | None = None
    assignees: list[str] = field(default_factory=list)
    country: str | None = None
    authority: str | None = None
    ipc: list[str] = field(default_factory=list)
    udk: str | None = None
    citations: int | None = None
    source: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def date_str(self) -> str:
        if self.issued:
            return self.issued.isoformat()
        return str(self.year) if self.year else ""

    def date_parts(self) -> list[int]:
        if self.issued:
            return [self.issued.year, self.issued.month, self.issued.day]
        return [self.year] if self.year else []

    def extra_lines(self) -> list[str]:
        lines = [f"eLIBRARY ID: {self.id}"]
        if self.edn:
            lines.append(f"EDN: {self.edn}")
        if self.citations is not None:
            lines.append(f"Цитирований в РИНЦ: {self.citations}")
        if self.udk:
            lines.append(f"УДК: {self.udk}")
        if self.ipc:
            lines.append("МПК: " + "; ".join(self.ipc))
        lines.extend(self.notes)
        if self.title_en and self.title_en.casefold() != self.title.casefold():
            lines.append(f"Название на английском: {self.title_en}")
        return lines


# ---- bibliographic line of result rows

_YEAR = r"(?:18|19|20)\d\d"
_JOURNAL_YEAR_RE = re.compile(rf"\.\s+({_YEAR})(?:\.|$)")
_VOLUME_RE = re.compile(r"(?:^|[\s.])(?:Т\.|Том)\s*([^.]+?)\.(?:\s|$)")
_ISSUE_RE = re.compile(r"(?:^|[\s.])№\s*([^.]+?)\.(?:\s|$)")
_VYP_RE = re.compile(r"(?:^|[\s.])Вып\.\s*([^.]+?)\.(?:\s|$)")
_PAGES_RE = re.compile(r"(?:^|[\s.])С\.\s*([A-Za-z]?\d\w*(?:\s*[-–—]\s*[A-Za-z]?\d\w*)?)")
_CITY = r"(?:г\.\s*)?(?P<city>[A-ZА-ЯЁ][a-zа-яё]+(?:[\s-][A-ZА-ЯЁ]?[a-zа-яё]+)*)"
_CITY_YEAR_RE = re.compile(rf"(?:^|[.:/]\s*){_CITY},\s*(?P<year>{_YEAR})\.?\s*$")
_DMY_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_PATENT_LINE_RE = re.compile(
    r"^(?P<genre>.*?)\s+(?P<number>[A-Z]{2}\s*[\dA-Z/]*\d[\dA-Z/]*(?:\s+[A-Z]\d?)?)\s*,\s*"
    r"(?P<date>\d{1,2}\.\d{1,2}\.\d{4})")
_APPLICATION_RE = re.compile(r"Заявка\s*№\s*(?P<number>[\w/.-]+?)\s+от\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})")
_IN_PREFIX_RE = re.compile(r"^\s*(?:В сборнике|В книге|В сб\.|In)\s*:\s*", re.I)
_AUTHORS_SEG_RE = re.compile(r"^(?P<names>(?:[A-ZА-ЯЁ][\w'-]+\s+(?:[A-ZА-ЯЁ][a-zа-яё]{0,2}\.\s?){1,3},?\s*)+)")
_TITLE_INFO_RE = re.compile(
    r"^(?:материал|сборник|сб\.|тезис|труды|доклад|статьи|научн|международн|всероссийск|межвузовск|"
    r"учебн|монограф|коллективн)", re.I)
# Editors listed in the nominative case ("Ответственный редактор: Иванов И.И., Петров П.П.");
# "под ред. Иванова И.И." is genitive and is not used.
_EDITORS_SEG_RE = re.compile(
    r"^(?:(?:Ответственный|Отв\.|Главный|Научный)\s+)?(?:редактор|редакторы|ред\.|составитель|составители)"
    r"\s*:\s*(?P<names>.+)$", re.I)
_NO_SPLIT_BEFORE = frozenset({"ред", "г", "им", "т", "вып", "сер", "изд", "гос", "ун-т", "акад", "науч",
                              "техн", "пр", "др", "с", "п", "ч", "кн", "отв", "сост", "св"})


def _split_segments(text: str) -> list[str]:
    """Split a bibliographic line on ``". "`` but not after initials and common abbreviations."""
    segs: list[str] = []
    start = 0
    for m in re.finditer(r"\.\s+", text):
        prev = re.search(r"([\w-]+)$", text[start:m.start()])
        word = prev.group(1) if prev else ""
        if (len(word) == 1 and word.isalpha()) or word.lower() in _NO_SPLIT_BEFORE:
            continue
        segs.append(text[start:m.start()].strip())
        start = m.end()
    tail = text[start:].strip().rstrip(".").strip()
    if tail:
        segs.append(tail)
    return [s for s in segs if s]


def _dmy(text: str | None) -> date | None:
    m = _DMY_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _norm_pages(pages: str | None) -> str | None:
    if not pages:
        return None
    return re.sub(r"\s*[-–—]\s*", "-", pages.strip())


def _city_year(text: str) -> tuple[str | None, int | None]:
    m = _CITY_YEAR_RE.search(text.strip())
    if m:
        return m.group("city"), int(m.group("year"))
    return None, None


def _parse_hit_source(r: _Record, source: str, fix: bool) -> None:
    """Fill ``r`` from the bibliographic line of a result row (best effort)."""
    s = _clean(source)
    if not s:
        return
    kind = r.kind
    if kind == "patent":
        m = _PATENT_LINE_RE.match(s)
        if m:
            r.genre = _clean(m.group("genre")) or None
            r.number = _clean(m.group("number"))
            r.issued = _dmy(m.group("date"))
            r.country = r.number[:2]
            if r.country == "RU":
                r.authority = "Роспатент"
        a = _APPLICATION_RE.search(s)
        if a:
            r.application_number = a.group("number")
            r.filing_date = _dmy(a.group("date"))
        return
    if kind == "thesis":
        genre, _, rest = s.partition(" / ")
        r.genre = _clean(genre).rstrip(".") or None
        if rest:
            m = re.match(rf"^(?P<univ>.*?)\.\s+{_CITY},\s*(?P<year>{_YEAR})\.?$", rest.strip())
            if m:
                r.publisher = _clean(m.group("univ")) or None
                r.place = m.group("city")
            else:
                r.publisher = _clean(rest).rstrip(".") or None
        return
    if kind == "grant":
        m = re.search(r"№\s*([^.\s]+(?:\s*[^.\s]+)*?)\.", s)
        if m:
            r.number = m.group(1)
        segs = _split_segments(s)
        r.genre = "НИР: грант" if re.match(r"^НИР:\s*грант", s, re.I) else (segs[0] if segs else None)
        for seg in segs[1:]:
            if not re.fullmatch(_YEAR, seg):
                r.publisher = seg
                break
        return
    pages = _PAGES_RE.findall(s)
    if pages and r.pages is None:
        r.pages = _norm_pages(pages[-1])
    if kind == "article":
        m = _JOURNAL_YEAR_RE.search(s)
        journal = s[: m.start()] if m else None
        if journal:
            r.container_title = normalize_caps(journal, mode="title") if fix else journal
        rest = s[m.end():] if m else s
        if v := _VOLUME_RE.search(rest):
            r.volume = v.group(1).strip()
        if i := (_ISSUE_RE.search(rest) or _VYP_RE.search(rest)):
            r.issue = i.group(1).strip()
        return
    body = _IN_PREFIX_RE.sub("", s)
    body = re.sub(r"(?:^|[\s.,])С\.\s*[A-Za-z]?\d\w*(?:\s*[-–—]\s*[A-Za-z]?\d\w*)?\.?\s*$", "", body).strip()
    city, _year = _city_year(body)
    if city:
        r.place = city
    if kind in ("conference", "chapter", "proceedings"):
        if not _IN_PREFIX_RE.match(s):
            return
        body = re.sub(rf"(?:[.,]\s*)?(?:г\.\s*)?(?:[A-ZА-ЯЁ][a-zа-яё]+(?:[\s-][A-ZА-ЯЁ]?[a-zа-яё]+)*,\s*)?{_YEAR}\.?$",
                      "", body).strip(" .,")
        segs = _split_segments(body)
        if not segs:
            return
        title = normalize_caps(segs[0], mode="title") if fix else segs[0]
        for seg in segs[1:]:
            em = _EDITORS_SEG_RE.match(seg)
            if em and not r.editors:
                r.editors = [_person(n) for n in re.split(r",\s*", em.group("names").strip(" ,.")) if n]
        for seg in segs[1:3]:
            am = _AUTHORS_SEG_RE.match(seg)
            if am and not r.container_authors:
                r.container_authors = [_person(n) for n in re.split(r",\s*", am.group("names").strip(" ,")) if n]
                continue
            if _TITLE_INFO_RE.match(seg) and ":" not in title[-2:]:
                info = normalize_caps(seg) if fix else seg
                title = f"{title}: {info[0].lower() + info[1:]}"
                break
            break
        r.container_title = title
        return
    if kind in ("book", "manuscript", "report", "preprint", "dataset", "other"):
        if m := re.search(r"ISBN[:\s]*([\dXx][\d\sXx-]{8,}[\dXx])", s):
            r.isbn = [m.group(1).replace(" ", "")]
        if m := re.search(r"(?:Изд-во|Издательство)\s*:?\s*([^.,]+)", s):
            r.publisher = _clean(m.group(1))


# ---- kinds


def _norm_kind(kind: str | None, type_raw: str | None = None, source: str | None = None,
               deposited: bool = False) -> str:
    k = (kind or "").lower()
    text = f"{type_raw or ''} {source or ''}".lower()
    if k in ("article", "conference", "proceedings", "book", "chapter", "patent", "report", "grant",
             "dataset"):
        return k
    if k in ("dissertation", "thesis"):
        return "thesis"
    if k == "abstract":
        return "conference"
    if k in ("preprint", "manuscript"):
        return "manuscript" if (deposited or "депонир" in text or "деп." in text) else "preprint"
    return "other"


def zotero_item_type(pub: Exportable) -> str:
    """Zotero ``itemType`` used for a publication (journalArticle, conferencePaper, book, ...)."""
    return _TYPES[_record(pub, fix=False).kind][1]


def _guess_language(text: str) -> str | None:
    cyr, lat = len(_CYR_RE.findall(text)), len(_LAT_RE.findall(text))
    if cyr and cyr >= lat:
        return "ru"
    if lat and not cyr:
        return "en"
    return None


_EDITOR_RE = re.compile(r"редак|ред\.|составит|editor", re.I)
_TRANSLATOR_RE = re.compile(r"перев|translat", re.I)


def _degree_genitive(degree: str) -> str:
    d = _clean(degree)
    for nom, gen in (("доктор ", "доктора "), ("кандидат ", "кандидата "), ("магистр ", "магистра ")):
        if d.lower().startswith(nom):
            return gen + d[len(nom):]
    return d


def _extra_get(extra: Mapping[str, str], *needles: str) -> str | None:
    for key, value in extra.items():
        low = key.lower()
        if any(n in low for n in needles) and value:
            return value
    return None


def _description_details(r: _Record, description: str | None) -> None:
    """Volume / series / place from the ИСТОЧНИК description ('Монография. Уфа, 2024')."""
    d = _clean(description)
    if not d:
        return
    if r.volume is None and (m := re.search(r"(?:Том|Т\.)\s*(\d+[\w-]*)", d)):
        r.volume = m.group(1)
    if r.series is None and (m := re.search(r"Сер\.\s*[\"«]([^\"»]+)[\"»]", d)):
        r.series = _clean(m.group(1))
    if r.place is None:
        city, _ = _city_year(d)
        r.place = city


def _record_from_publication(p: Publication, fix: bool) -> _Record:
    kind = _norm_kind(p.kind, p.type_raw, deposited=bool(p.dates.deposited))
    title = normalize_caps(p.title) if fix else p.title
    r = _Record(id=p.id, url=p.url, kind=kind, title=title, edn=p.edn, doi=p.doi,
                title_en=(normalize_caps(p.title_en) if fix else p.title_en) or None)
    r.authors = [_person_ref(a) for a in p.authors]
    for c in p.contributors:
        role = c.role or ""
        if _TRANSLATOR_RE.search(role):
            r.translators.append(_person_ref(c))
        elif _EDITOR_RE.search(role):
            r.editors.append(_person_ref(c))
        else:
            r.contributors.append(_person_ref(c))
    r.volume, r.issue, r.pages = p.volume, p.issue, _norm_pages(p.pages)
    r.language = p.language_code or (_guess_language(p.title) if not p.language else None)
    r.abstract = p.abstract
    r.keywords = list(p.keywords)
    r.udk = p.udk or (p.book.udk if p.book else None)
    r.citations = p.metrics.rinc_citations
    r.year = p.year
    if p.journal:
        j = p.journal
        name = j.name_cased or j.name
        r.container_title = (normalize_caps(name, mode="title") if fix else name) or None
        r.issn = j.issn or j.eissn
    if p.container:
        c = p.container
        if c.title:
            r.container_title = normalize_caps(c.title, mode="title") if fix else c.title
        r.container_authors = [_person(a) for a in c.authors]
        if c.publisher:
            r.publisher = c.publisher.name
        r.year = r.year or c.year
        _description_details(r, c.description)
    if p.conference:
        conf = p.conference
        r.conference_name = (normalize_caps(conf.name, mode="title") if fix else conf.name) or None
        r.event_place = conf.place
        r.event_dates = conf.dates
        if conf.dates:
            r.notes.append(f"Даты конференции: {conf.dates}")
    if p.book:
        b = p.book
        if b.publisher:
            r.publisher = b.publisher.name
        r.place = b.city or r.place
        r.year = r.year or b.year
        r.isbn = list(b.isbn)
        r.num_pages = b.pages_total
        r.series = b.series or r.series
        r.edition = b.edition
    if p.dissertation:
        d = p.dissertation
        doc = d.document or p.type_raw or "диссертация"
        r.genre = f"{doc} на соискание ученой степени {_degree_genitive(d.degree)}" if d.degree else doc
        if d.defense_place:
            r.publisher = d.defense_place.name
        r.place = d.city or r.place
        r.num_pages = d.pages_total or r.num_pages
        if d.specialty:
            r.notes.append(f"Специальность: {d.specialty}")
    if p.patent:
        pt = p.patent
        r.genre = pt.patent_type or p.type_raw
        r.number = pt.number
        r.application_number = pt.application_number
        r.filing_date = pt.registration_date
        r.priority_date = pt.priority_date
        r.issued = pt.publication_date
        r.year = pt.publication_year or r.year or (pt.publication_date.year if pt.publication_date else None)
        r.assignees = [h.name for h in pt.holders]
        r.country = pt.country_code or pt.office
        if (pt.country_code or "").upper() == "RU" or (pt.office or "").lower() == "россия":
            r.authority = "Роспатент"
        elif pt.office:
            r.authority = pt.office
        r.ipc = [i.code for i in pt.ipc]
    if kind in ("report", "grant", "manuscript", "preprint", "dataset", "other"):
        r.genre = r.genre or p.type_raw
        if kind == "manuscript":
            r.issued = r.issued or p.dates.deposited
            r.publisher = r.publisher or _extra_get(p.extra, "депонир", "организац")
        elif kind == "grant":
            r.publisher = r.publisher or _extra_get(p.extra, "фонд", "грантодат", "финансир")
        elif kind == "dataset":
            r.publisher = r.publisher or _extra_get(p.extra, "репозитор", "организац")
        else:
            r.publisher = r.publisher or _extra_get(p.extra, "организац", "исполнит", "репозитор")
        r.number = r.number or _extra_get(p.extra, "номер")
        if not r.publisher and kind in ("report", "grant") and p.affiliations:
            r.publisher = p.affiliations[0].name
    if r.year is None:
        for d in (p.dates.published, p.dates.defended, p.dates.deposited, p.dates.placed):
            if d:
                r.year = d.year
                break
    if kind == "chapter" and not r.authors:
        r.authors = list(r.container_authors)
    return r


def _record_from_hit(h: PublicationHit, fix: bool) -> _Record:
    kind = _norm_kind(h.kind, source=h.source)
    title = normalize_caps(h.title) if fix else h.title
    r = _Record(id=h.id, url=h.url, kind=kind, title=title, year=h.year, citations=h.citations,
                pages=_norm_pages(h.pages), source=_clean(h.source) or None)
    r.authors = [_person(a) for a in h.authors]
    r.language = _guess_language(h.title)
    _parse_hit_source(r, h.source or "", fix)
    if r.issued and not r.year:
        r.year = r.issued.year
    if r.source and (kind in ("report", "manuscript", "preprint", "dataset", "other", "grant")
                     or (kind == "book" and not r.place)):
        r.notes.append(f"Описание (eLIBRARY): {r.source}")
    if kind == "chapter" and not r.authors:
        r.authors = list(r.container_authors)
    return r


def _record(pub: Exportable, fix: bool = True) -> _Record:
    if isinstance(pub, Publication):
        return _record_from_publication(pub, fix)
    if isinstance(pub, PublicationHit):
        return _record_from_hit(pub, fix)
    raise TypeError(f"Ожидалась публикация (Publication или PublicationHit), получено: {type(pub).__name__}")


# --------------------------------------------------------------------------- citation keys

_STOPWORDS_TEXT = ("в во на о об обо и к ко с со по для от до из за при над под про у а но или не "
                   "the a an of on in at to for and by with from")
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


def _key_words(title: str) -> list[str]:
    words = []
    for w in re.findall(r"[^\W\d_]+", title):
        if w.lower() in _STOPWORDS:
            continue
        t = re.sub(r"[^a-z0-9]", "", transliterate(w).lower())
        if t:
            words.append(t)
    return words


def _citekey(r: _Record, with_edn: bool = False) -> str:
    person = next((p for p in (r.authors or r.container_authors)), None)
    surname = ""
    if person:
        surname = re.sub(r"[^a-z0-9]", "", transliterate(person.family or person.literal).lower())
    words = _key_words(r.title)
    year = str(r.year) if r.year else ""
    if surname:
        key = surname + year + (words[0] if words else "")
    elif words:
        key = words[0] + year + (words[1] if len(words) > 1 else "")
    else:
        key = ""
    if not key or key == year:
        key = f"elibrary{r.id}"
    if with_edn:
        key += "-" + (r.edn.lower() if r.edn else str(r.id))
    return key


def citekey(pub: Exportable, *, with_edn: bool = False) -> str:
    """Stable citation key ``<first author surname, transliterated><year><first title word>``, e.g.
    ``"blokhina2021dlitelnoe"``. ``with_edn=True`` appends the EDN (``"...-fyoakl"``) for globally
    unique keys. Records without authors use the first two title words."""
    return _citekey(_record(pub, fix=True), with_edn)


# --------------------------------------------------------------------------- Zotero JSON


def _as_date_str(value: date | str | None) -> str:
    if value is None:
        return ""
    return value.isoformat() if isinstance(value, date) else str(value)


def to_zotero_item(pub: Exportable, *, tags: Iterable[str] = (), keyword_tags: bool = True,
                   collections: Iterable[str] = (), fix_caps: bool = True,
                   access_date: date | str | None = None) -> dict[str, Any]:
    """Native Zotero API item JSON (``POST /users/<id>/items`` payload element).

    Only fields valid for the item type are emitted (schema v42); values that have no field go to
    ``extra`` as CSL variables. ``extra`` always starts with ``eLIBRARY ID: <id>`` and ``EDN: <EDN>``.
    Keywords become tags (``keyword_tags``), plus ``tags``; ``collections`` are collection keys.
    """
    r = _record(pub, fix_caps)
    itype = _TYPES[r.kind][1]
    valid = _ZOTERO_FIELDS[itype]
    creators_ok = _ZOTERO_CREATORS[itype]
    base: dict[str, str] = {
        "title": r.title,
        "abstractNote": r.abstract or "",
        "publicationTitle": r.container_title or "",
        "conferenceName": r.conference_name or "",
        "eventPlace": r.event_place or "",
        "date": r.date_str,
        "volume": r.volume or "",
        "issue": r.issue or "",
        "pages": r.pages or "",
        "numPages": str(r.num_pages) if r.num_pages else "",
        "publisher": r.publisher or "",
        "place": r.place or "",
        "series": r.series or "",
        "edition": r.edition or "",
        "ISSN": r.issn or "",
        "ISBN": " ".join(r.isbn),
        "DOI": r.doi or "",
        "url": r.url,
        "language": r.language or "",
        "libraryCatalog": LIBRARY_CATALOG,
        "type": r.genre or "",
        "number": r.number or "",
        "applicationNumber": r.application_number or "",
        "filingDate": _as_date_str(r.filing_date),
        "priorityDate": _as_date_str(r.priority_date),
        "assignee": "; ".join(r.assignees),
        "country": r.country or "",
        "authority": r.authority or "",
        "accessDate": _as_date_str(access_date),
    }
    rename = _ZOTERO_BASE.get(itype, {})
    fields: dict[str, str] = {}
    extra = r.extra_lines()
    for name, value in base.items():
        if not value:
            continue
        target = rename.get(name, name)
        if target in valid:
            fields[target] = value
        elif name in _ZOTERO_TO_CSL_VAR:
            extra.append(f"{_ZOTERO_TO_CSL_VAR[name]}: {value}")
    if r.source and itype == "patent" and not r.number:
        extra.append(f"Описание (eLIBRARY): {r.source}")
    fields["extra"] = "\n".join(extra)

    creators: list[dict[str, str]] = []
    primary = creators_ok[0]
    creators += [p.zotero(primary) for p in r.authors]
    for group, ctype in ((r.editors, "editor"), (r.translators, "translator"), (r.contributors, "contributor")):
        ctype = ctype if ctype in creators_ok else "contributor"
        creators += [p.zotero(ctype) for p in group]
    if itype == "bookSection":
        creators += [p.zotero("bookAuthor") for p in r.container_authors]

    tag_list: list[str] = []
    if keyword_tags:
        tag_list += [normalize_caps(k, mode="lower") if fix_caps else k for k in r.keywords]
    tag_list += [_clean(t) for t in tags]
    seen: set[str] = set()
    tag_objs = []
    for t in tag_list:
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            tag_objs.append({"tag": t})

    item: dict[str, Any] = {"itemType": itype, "title": fields.pop("title", r.title), "creators": creators}
    for name in valid:
        if name in fields:
            item[name] = fields[name]
    item["tags"] = tag_objs
    item["collections"] = [c for c in collections if c]
    item["relations"] = {}
    return item


# --------------------------------------------------------------------------- CSL-JSON

# CSL keys zotero-mcp's ``zotero_add_item(source_type="csl_json")`` handles (its ``handled`` set): any
# other key is dumped into Zotero ``extra`` as ``key: <JSON>``.
_ZOTERO_MCP_CSL_KEYS = frozenset({
    "type", "id", "title", "title-short", "author", "editor", "translator", "issued", "container-title",
    "collection-title", "collection-number", "volume", "issue", "page", "publisher", "publisher-place",
    "edition", "ISBN", "ISSN", "DOI", "URL", "language", "abstract", "number-of-pages", "number", "genre",
    "keyword", "note",
})
# But a handled key is written only when the item type's template (Web API ``/items/new``: type-specific
# names such as ``university``, ``institution``, ``issueDate``) has the field it targets; otherwise it is
# dropped silently (never copied into ``extra``). zotero-mcp's ``csl_json_to_zotero`` (0.11):
_ZOTERO_MCP_ALWAYS = frozenset({"type", "id", "author", "editor", "translator", "keyword", "note"})
_ZOTERO_MCP_TARGET: dict[str, str] = {
    "title": "title", "title-short": "shortTitle", "issued": "date", "collection-title": "series",
    "collection-number": "seriesNumber", "volume": "volume", "issue": "issue", "page": "pages",
    "publisher": "publisher", "publisher-place": "place", "edition": "edition", "ISBN": "ISBN", "ISSN": "ISSN",
    "DOI": "DOI", "URL": "url", "language": "language", "abstract": "abstractNote",
    "number-of-pages": "numPages",
}
_ZOTERO_MCP_CONTAINER: dict[str, str] = {
    "journalArticle": "publicationTitle", "preprint": "publicationTitle", "bookSection": "bookTitle",
    "conferencePaper": "proceedingsTitle",
}
_ZOTERO_MCP_CONTAINER_FALLBACK = ("publicationTitle", "bookTitle", "proceedingsTitle", "seriesTitle")
_ZOTERO_MCP_NUMBER: dict[str, str] = {"report": "reportNumber", "patent": "patentNumber"}
_ZOTERO_MCP_GENRE: dict[str, str] = {"thesis": "thesisType"}


def _zotero_mcp_field(key: str, item_type: str) -> str | None:
    """The Zotero field zotero-mcp writes CSL ``key`` to for ``item_type``, None when it cannot place it."""
    fields = _ZOTERO_FIELDS.get(item_type, ())
    if key == "container-title":
        first = _ZOTERO_MCP_CONTAINER.get(item_type)
        if first in fields:
            return first
        return next((f for f in _ZOTERO_MCP_CONTAINER_FALLBACK if f in fields), None)
    if key == "number":
        target = _ZOTERO_MCP_NUMBER.get(item_type)
    elif key == "genre":
        target = _ZOTERO_MCP_GENRE.get(item_type)
    else:
        target = _ZOTERO_MCP_TARGET.get(key)
    return target if target in fields else None


def _csl_value_text(value: Any) -> str:
    """A CSL value as a Zotero ``extra`` line value (names: ``Family || Given``, dates: ISO)."""
    if isinstance(value, dict) and "date-parts" in value:
        parts = value["date-parts"][0] if value["date-parts"] else []
        return "-".join(f"{int(x):02d}" if i else str(x) for i, x in enumerate(parts))
    if isinstance(value, list):
        names = []
        for n in value:
            if isinstance(n, dict):
                names.append(n.get("literal") or " || ".join(x for x in (n.get("family"), n.get("given")) if x))
            else:
                names.append(str(n))
        return "; ".join(names)
    return str(value)


def to_csl_json(pub: Exportable, *, citekey: str | None = None, fix_caps: bool = True,
                zotero_mcp: bool = False) -> dict[str, Any]:
    """CSL-JSON item (``id`` = citation key; eLIBRARY ID, EDN, RSCI citations in ``note``).

    ``zotero_mcp=True`` keeps only the keys zotero-mcp's ``zotero_add_item`` places in a field of the
    item type and moves the others into ``note`` as Zotero ``extra`` lines (``event-title: ...``):
    keys it does not handle (``event-title``, ``container-author``, ``submitted``...), which it would
    dump as JSON, and handled keys the type's template has no field for, which it would drop
    (``publisher`` of a thesis/report/manuscript/preprint/dataset, ``genre`` of anything but a thesis,
    ``issued`` of a patent, ``container-title`` of a book...).
    """
    r = _record(pub, fix_caps)
    d: dict[str, Any] = {"id": citekey or _citekey(r), "type": _TYPES[r.kind][0], "title": r.title}
    if r.authors:
        d["author"] = [p.csl() for p in r.authors]
    if r.editors:
        d["editor"] = [p.csl() for p in r.editors]
    if r.translators:
        d["translator"] = [p.csl() for p in r.translators]
    if r.container_authors and r.kind == "chapter":
        d["container-author"] = [p.csl() for p in r.container_authors]
    if parts := r.date_parts():
        d["issued"] = {"date-parts": [parts]}
    simple = {
        "container-title": r.container_title, "collection-title": r.series, "volume": r.volume,
        "issue": r.issue, "page": r.pages, "number-of-pages": r.num_pages, "edition": r.edition,
        "publisher": r.publisher, "publisher-place": r.place, "event-title": r.conference_name,
        "event-place": r.event_place, "genre": r.genre, "number": r.number, "authority": r.authority,
        "ISSN": r.issn, "ISBN": " ".join(r.isbn) or None, "DOI": r.doi, "URL": r.url,
        "language": r.language, "abstract": r.abstract,
        "keyword": ", ".join(normalize_caps(k, mode="lower") if fix_caps else k for k in r.keywords) or None,
        "source": LIBRARY_CATALOG,
    }
    for key, value in simple.items():
        if value not in (None, ""):
            d[key] = str(value) if isinstance(value, int) else value
    if r.filing_date:
        d["submitted"] = {"date-parts": [[r.filing_date.year, r.filing_date.month, r.filing_date.day]]}
    notes = r.extra_lines()
    if r.kind == "patent":
        if r.application_number:
            notes.append(f"Номер заявки: {r.application_number}")
        if r.assignees:
            notes.append("Патентообладатель: " + "; ".join(r.assignees))
        if r.country:
            notes.append(f"Страна: {r.country}")
        if r.priority_date:
            notes.append(f"Дата приоритета: {r.priority_date.isoformat()}")
    if zotero_mcp:
        d.pop("source", None)
        item_type = _TYPES[r.kind][1]  # = zotero-mcp's CSL_TYPE_MAP[d["type"]] ("document" for others)
        for key in [k for k in d if k not in _ZOTERO_MCP_ALWAYS and _zotero_mcp_field(k, item_type) is None]:
            notes.append(f"{key}: {_csl_value_text(d.pop(key))}")
    d["note"] = "\n".join(notes)
    return d


# --------------------------------------------------------------------------- BibTeX

# Braces become \textbraceleft{} / \textbraceright{}, not \{ / \}: BibTeX, biber (btparse) and
# bibtexparser count braces ignoring backslashes, so a lone "\{" would still unbalance the entry.
_BIB_SPECIAL = {"\\": r"\textbackslash{}", "{": r"\textbraceleft{}", "}": r"\textbraceright{}", "&": r"\&",
                "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}",
                "^": r"\textasciicircum{}"}


def bibtex_escape(text: str) -> str:
    """Escape LaTeX special characters; non-ASCII (Cyrillic) stays as UTF-8. The result never has
    unbalanced braces."""
    return "".join(_BIB_SPECIAL.get(c, c) for c in _clean(text))


def _braces_balanced(text: str) -> bool:
    depth = 0
    for c in text:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _bib_verbatim(text: str) -> str:
    """A verbatim value (``url``, ``doi``): kept as is unless its braces are unbalanced, in which case
    they are percent-encoded (``%7B``/``%7D``, the same URL/DOI) so that the entry stays parseable."""
    text = _clean(text)
    return text if _braces_balanced(text) else text.replace("{", "%7B").replace("}", "%7D")


def _bib_title(text: str) -> str:
    """Escaped title with words of two or more capitals (acronyms) protected by braces."""
    out = []
    for word in _clean(text).split(" "):
        esc = bibtex_escape(word)
        if sum(1 for c in word if c.isupper()) >= 2:
            esc = "{" + esc + "}"
        out.append(esc)
    return " ".join(out)


def _bib_names(people: list[_Person]) -> str:
    out = []
    for p in people:
        if p.literal:
            out.append("{" + bibtex_escape(p.literal) + "}")
        else:
            out.append(bibtex_escape(p.inverted()))
    return " and ".join(out)


def to_bibtex(pub: Exportable, *, citekey: str | None = None, with_edn_key: bool = False,
              fix_caps: bool = True) -> str:
    """One BibTeX/biblatex entry (UTF-8, LaTeX specials escaped, ``edn`` field, ``langid``).

    Entry types: article, inproceedings, proceedings, book, incollection, thesis, patent,
    techreport, unpublished, misc, dataset. The key is :func:`citekey` unless ``citekey`` is given.
    """
    r = _record(pub, fix_caps)
    etype = _TYPES[r.kind][2]
    key = citekey or _citekey(r, with_edn_key)
    f: list[tuple[str, str]] = []

    def add(name: str, value: Any, *, raw: bool = False) -> None:
        if value in (None, "", []):
            return
        f.append((name, str(value) if raw else bibtex_escape(str(value))))

    if r.authors:
        f.append(("author", _bib_names(r.authors)))
    if r.editors:
        f.append(("editor", _bib_names(r.editors)))
    if r.translators:
        f.append(("translator", _bib_names(r.translators)))
    if r.kind == "chapter" and r.container_authors:
        f.append(("bookauthor", _bib_names(r.container_authors)))
    f.append(("title", _bib_title(r.title)))
    if r.kind == "article":
        if r.container_title:
            f.append(("journal", _bib_title(r.container_title)))
    elif r.container_title:
        f.append(("booktitle", _bib_title(r.container_title)))
    if r.conference_name:
        f.append(("eventtitle", _bib_title(r.conference_name)))
    add("venue", r.event_place)
    add("year", r.year)
    if r.issued:
        add("date", r.issued.isoformat())
    add("volume", r.volume)
    if r.kind in ("article", "conference"):
        add("number", r.issue)
    else:
        add("issue", r.issue)
    if r.pages:
        f.append(("pages", bibtex_escape(r.pages).replace("-", "--")))
    add("pagetotal", r.num_pages)
    add("edition", r.edition)
    add("series", r.series)
    if r.kind == "thesis":
        add("school", r.publisher)
        add("type", r.genre)
    elif r.kind in ("report", "grant", "manuscript"):
        add("institution", r.publisher)
        add("type", r.genre)
        add("number", r.number)
    elif r.kind == "patent":
        add("type", r.genre)
        add("number", r.number)
        add("holder", " and ".join("{" + bibtex_escape(a) + "}" for a in r.assignees), raw=True)
        add("location", r.country)
        add("applicationnumber", r.application_number)
        add("filingdate", r.filing_date.isoformat() if r.filing_date else None)
        add("ipc", "; ".join(r.ipc))
    else:
        add("publisher", r.publisher)
        if r.kind in ("preprint", "dataset", "other"):
            add("type", r.genre)
            add("number", r.number)
    add("address", r.place)
    add("isbn", ", ".join(r.isbn))
    add("issn", r.issn)
    add("doi", _bib_verbatim(r.doi or ""), raw=True)
    add("url", _bib_verbatim(r.url or ""), raw=True)
    add("langid", _LANG_BABEL.get(r.language or ""))
    if r.keywords:
        add("keywords", ", ".join(normalize_caps(k, mode="lower") if fix_caps else k for k in r.keywords))
    add("abstract", r.abstract)
    add("edn", r.edn)
    if r.kind == "thesis" and r.notes:
        add("note", "; ".join(n for n in r.notes if n.startswith("Специальность")))
    width = max(len(n) for n, _ in f)
    body = ",\n".join(f"  {n.ljust(width)} = {{{v}}}" for n, v in f)
    return f"@{etype}{{{key},\n{body}\n}}\n"


# --------------------------------------------------------------------------- RIS


# RIS tag for the editors, per Zotero item type, as Zotero's RIS translator (RIS.js) imports it: A2 is
# the series editor of a book or report, A3 the editor of a book and a contributor of a thesis; a report
# has only the old ED tag. None: no tag (kept in N1).
_RIS_EDITOR_TAG: dict[str, str | None] = {"book": "A3", "thesis": "A3", "report": "ED", "patent": None,
                                          "manuscript": None, "dataset": None}
# Item types whose SP is numPages (not pages) in RIS.js.
_RIS_SP_NUM_PAGES = frozenset({"book", "thesis", "manuscript"})


def to_ris(pub: Exportable, *, fix_caps: bool = True) -> str:
    """One RIS record (``TY`` ... ``ER``) whose tags land in the right fields when Zotero imports it
    (the tables of Zotero's RIS translator, ``RIS.js``). Conference paper: ``T2`` conference name,
    ``C3`` proceedings title, ``T3`` series, ``C1`` place, ``CY`` conference location. Book: ``T2``
    series, ``A3`` editors. Chapter: ``T2`` book title, ``C2`` book authors, ``A2`` editors. Patent:
    ``SN`` number, ``M1`` application, ``C1`` filing date, ``PB`` assignee, ``C3`` country, ``A2``
    authority. Values with no tag for the type go to ``N1`` as ``variable: value``."""
    r = _record(pub, fix_caps)
    itype = _TYPES[r.kind][1]
    lines: list[tuple[str, str]] = [("TY", _TYPES[r.kind][3])]
    notes = r.extra_lines()

    def add(tag: str, value: Any) -> None:
        text = _clean(value)
        if text:
            lines.append((tag, text))

    def note(var: str, value: Any) -> None:
        text = _clean(value)
        if text:
            notes.append(f"{var}: {text}")

    for p in r.authors:
        add("AU", p.inverted())
    editor_tag = _RIS_EDITOR_TAG.get(itype, "A2")
    for p in r.editors:
        if editor_tag:
            add(editor_tag, p.inverted())
        else:
            note("editor", p.inverted())
    for p in r.translators:
        add("A4", p.inverted())
    add("TI", r.title)
    if itype == "conferencePaper":
        add("T2", r.conference_name)
        add("C3", r.container_title)
        add("T3", r.series)
    elif itype in ("book", "report"):
        add("T2", r.series)  # book: series, report: seriesTitle
        note("container-title", r.container_title)
    else:
        add("T2", r.container_title)
        add("T3", r.series)
    if itype == "bookSection":
        for p in r.container_authors:
            add("C2", p.inverted())
    add("PY", r.year)
    if r.issued:
        add("DA", r.issued.strftime("%Y/%m/%d"))
    if itype == "patent":
        note("volume", r.volume)
    else:
        add("VL", r.volume)
    if itype in ("bookSection", "dataset"):  # IS is numberOfVolumes / versionNumber there
        note("issue", r.issue)
    else:
        add("IS", r.issue)
    if itype in _RIS_SP_NUM_PAGES:
        add("SP", r.num_pages)
        note("page", r.pages)
    else:
        if r.pages:
            sp, _, ep = r.pages.partition("-")
            add("SP", sp)
            add("EP", ep)
        note("number-of-pages", r.num_pages)
    if itype == "patent":
        add("SN", r.number)
        add("M1", r.application_number)
        if r.filing_date:
            add("C1", r.filing_date.strftime("%Y/%m/%d"))
        add("PB", "; ".join(r.assignees))
        add("C3", r.country)
        add("A2", r.authority)
        add("M3", r.genre)
    else:
        if itype == "report":
            add("SN", r.number)
        else:
            if itype == "journalArticle":
                add("SN", r.issn or ", ".join(r.isbn))
            else:
                add("SN", ", ".join(r.isbn) or r.issn)
            note("number", r.number)
        add("PB", r.publisher)
        add("M3", r.genre)
    if itype == "conferencePaper":
        add("C1", r.place)  # Zotero: place; CY of a CPAPER is the conference location
        add("CY", r.event_place)
    else:
        add("CY", r.place)
        note("event-place", r.event_place)
    if itype == "dataset":
        note("edition", r.edition)
    else:
        add("ET", r.edition)
    add("LA", r.language)
    add("AB", r.abstract)
    for k in r.keywords:
        add("KW", normalize_caps(k, mode="lower") if fix_caps else k)
    add("DO", r.doi)
    add("UR", r.url)
    add("DB", "eLIBRARY.RU")
    add("AN", r.id)
    if r.conference_name and itype != "conferencePaper":
        notes.append(f"Конференция: {r.conference_name}")
    add("N1", "; ".join(notes))
    lines.append(("ER", ""))
    return "\n".join(f"{tag}  - {value}" for tag, value in lines) + "\n"


# --------------------------------------------------------------------------- batches


def normalize_format(fmt: str) -> str:
    """Canonical format name (``"bib"`` -> ``"bibtex"``, ``"csl-json"`` -> ``"csl"``...)."""
    key = (fmt or "").strip().lower()
    if key not in FORMAT_ALIASES:
        raise ValueError(f"Неизвестный формат экспорта {fmt!r}. Доступны: {', '.join(FORMATS)}")
    return FORMAT_ALIASES[key]


def _unique_keys(records: list[_Record], with_edn: bool) -> list[str]:
    keys = [_citekey(r, with_edn) for r in records]
    counts: dict[str, int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    out: list[str] = []
    used: set[str] = set()
    for r, k in zip(records, keys, strict=True):
        if counts[k] > 1 and not with_edn:
            k = f"{k}-{r.edn.lower() if r.edn else r.id}"
        base, n = k, 1
        while k in used:
            n += 1
            k = f"{base}-{n}"
        used.add(k)
        out.append(k)
    return out


def export(items: Exportable | Iterable[Exportable], fmt: str, *, fix_caps: bool = True,
           citekey_edn: bool = False, tags: Iterable[str] = (), zotero_mcp: bool = False) -> str:
    """Export one or several publications as text in ``fmt`` (see :data:`FORMATS`).

    ``bibtex``: entries separated by blank lines, keys unique within the batch (a clash gets the EDN
    appended); ``ris``: concatenated records; ``csl`` / ``zotero``: a JSON array (UTF-8, indented).
    ``tags`` are added to every Zotero item. ``zotero_mcp=True`` builds the CSL items with
    ``to_csl_json(..., zotero_mcp=True)`` (other formats ignore it).
    """
    fmt = normalize_format(fmt)
    seq: list[Exportable] = [items] if isinstance(items, (Publication, PublicationHit)) else list(items)
    tags = list(tags)
    if fmt == "zotero":
        data = [to_zotero_item(p, fix_caps=fix_caps, tags=tags) for p in seq]
        return json.dumps(data, ensure_ascii=False, indent=2)
    if fmt == "ris":
        return "\n".join(to_ris(p, fix_caps=fix_caps) for p in seq)
    keys = _unique_keys([_record(p, fix_caps) for p in seq], citekey_edn)
    if fmt == "csl":
        data = [to_csl_json(p, citekey=k, fix_caps=fix_caps, zotero_mcp=zotero_mcp)
                for p, k in zip(seq, keys, strict=True)]
        return json.dumps(data, ensure_ascii=False, indent=2)
    return "\n".join(to_bibtex(p, citekey=k, fix_caps=fix_caps) for p, k in zip(seq, keys, strict=True))


__all__ = [
    "FILE_EXTENSIONS", "FORMATS", "FORMAT_ALIASES", "LIBRARY_CATALOG", "MEDIA_TYPES", "Exportable",
    "bibtex_escape", "citekey", "export", "normalize_caps", "normalize_format", "split_person_name",
    "to_bibtex", "to_csl_json", "to_ris", "to_zotero_item", "transliterate", "zotero_item_type",
]
