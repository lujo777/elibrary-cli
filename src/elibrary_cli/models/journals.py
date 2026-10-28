"""Journal models: catalog search, catalog rows, journal profile, indicators and issue contents.

Pages (2026-10):

* ``titles.asp`` -- journal catalog search (POST form ``results``), rows ``tr#a<TITLEID>``.
* ``title_about_new.asp?id=TITLEID`` -- "ИНФОРМАЦИЯ ОБ ИЗДАНИИ": characteristics, founders, ISSN,
  indexing (WoS/Scopus/VAK/RSCI/white list...), contacts, editorial board, rubrics.
  ``&langid=2`` shows the English description.
* ``title_profile.asp?id=TITLEID`` -- "Анализ публикационной активности журнала": impact factors and
  the other bibliometric indicators (no fixture yet, parsed defensively).
* ``contents.asp?titleid=TITLEID`` (latest issue) / ``contents.asp?id=ISSUEID`` -- issue contents
  with section headings, page ranges and the list of all issues of the journal.

``JournalQuery`` accepts readable values (``vak="k1"``, ``scopus="q2"``) and also the raw site codes
(``vak="2"``, ``scopus="5"``); the validators normalise both to the readable form, and
:func:`elibrary_cli.queries.journals.build_journal_search_form` maps them back to the site codes.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, ValidationInfo, field_validator

from elibrary_cli.models.base import BASE_URL, ElibModel, PublicationHit

# --------------------------------------------------------------------------------------------------
# Site codes of the titles.asp form (verified against tests/fixtures/html/titles_form.html)
# --------------------------------------------------------------------------------------------------

#: ``translated``: сведения о переводе.
TRANSLATED_CODES: dict[str, str] = {"full": "1", "partial": "2", "no": "0"}
#: ``wos``: Web of Science.
WOS_CODES: dict[str, str] = {
    "indexed": "1",  # индексируется в Web of Science
    "translated": "2",  # индексируется переводная версия
    "partial": "3",  # индексируется частично
    "q1": "4",
    "q2": "5",
    "q3": "6",
    "q4": "7",
    "q1-q4": "8",  # Web of Science (квартили 1-4)
    "not": "9",  # не индексируется
}
#: ``scopus``: Scopus (no "8" option in the form).
SCOPUS_CODES: dict[str, str] = {
    "indexed": "1",
    "translated": "2",
    "partial": "3",
    "q1": "4",
    "q2": "5",
    "q3": "6",
    "q4": "7",
    "not": "9",
}
#: ``white``: ЕГПНИ / Белый список.
WHITE_CODES: dict[str, str] = {
    "included": "01",  # входит в Белый список
    "l1": "02",
    "l2": "03",
    "l3": "04",
    "l4": "05",
    "not": "06",  # не входит в Белый список
    "egpni": "07",  # входит в ЕГПНИ
    "egpni_l1": "08",
    "egpni_l2": "09",
    "egpni_l3": "10",
    "egpni_l4": "11",
    "egpni_not": "12",
    "any": "13",  # входит в ЕГПНИ/Белый список
    "any_l1": "14",
    "any_l2": "15",
    "any_l3": "16",
    "any_l4": "17",
    "any_not": "18",
}
#: ``vak``: Перечень ВАК.
VAK_CODES: dict[str, str] = {"included": "1", "k1": "2", "k2": "3", "k3": "4", "not": "9"}
#: ``risc``: РИНЦ / Science Index (quartiles of the Science Index rating).
RISC_CODES: dict[str, str] = {"indexed": "0", "not": "2", "q1": "3", "q2": "4", "q3": "5", "q4": "6"}
#: ``ftopen``: доступ к полным текстам.
FTOPEN_CODES: dict[str, str] = {"open": "1", "partial": "2", "subscription": "3"}
#: ``sortorder``.
SORT_CODES: dict[str, str] = {
    "name": "0",  # по названию журнала
    "articles": "3",  # по числу статей
    "citations_rinc": "4",  # по числу цитирований в РИНЦ
    "citations_core": "1",  # по числу цитирований из ядра РИНЦ (default of the form)
    "si_rating": "7",  # по рейтингу Science Index
}
#: ``order``: 0 ascending, 1 descending.
ORDER_CODES: dict[str, str] = {"asc": "0", "desc": "1"}

#: Readable value -> site code, per JournalQuery field.
CODE_TABLES: dict[str, dict[str, str]] = {
    "translated": TRANSLATED_CODES,
    "wos": WOS_CODES,
    "scopus": SCOPUS_CODES,
    "white": WHITE_CODES,
    "vak": VAK_CODES,
    "risc": RISC_CODES,
    "ftopen": FTOPEN_CODES,
    "sort": SORT_CODES,
    "order": ORDER_CODES,
}

#: Extra spellings accepted for some readable values.
_ALIASES: dict[str, dict[str, str]] = {
    "translated": {"yes": "full", "none": "no", "not": "no"},
    "wos": {"no": "not", "none": "not", "yes": "indexed", "quartile": "q1-q4", "any_quartile": "q1-q4"},
    "scopus": {"no": "not", "none": "not", "yes": "indexed"},
    "white": {"yes": "included", "no": "not", "none": "not"},
    "vak": {"yes": "included", "no": "not", "none": "not", "к1": "k1", "к2": "k2", "к3": "k3"},
    "risc": {"yes": "indexed", "no": "not", "none": "not"},
    "ftopen": {"paid": "subscription", "yes": "open"},
    "sort": {"title": "name", "citations": "citations_core", "rating": "si_rating", "science_index": "si_rating"},
    "order": {"ascending": "asc", "descending": "desc", "rev": "desc", "nrm": "asc"},
}

TranslatedFilter = Literal["full", "partial", "no"]
WosFilter = Literal["indexed", "translated", "partial", "q1", "q2", "q3", "q4", "q1-q4", "not"]
ScopusFilter = Literal["indexed", "translated", "partial", "q1", "q2", "q3", "q4", "not"]
WhiteFilter = Literal[
    "included", "l1", "l2", "l3", "l4", "not",
    "egpni", "egpni_l1", "egpni_l2", "egpni_l3", "egpni_l4", "egpni_not",
    "any", "any_l1", "any_l2", "any_l3", "any_l4", "any_not",
]
VakFilter = Literal["included", "k1", "k2", "k3", "not"]
RiscFilter = Literal["indexed", "not", "q1", "q2", "q3", "q4"]
FtOpenFilter = Literal["open", "partial", "subscription"]
JournalSort = Literal["name", "articles", "citations_rinc", "citations_core", "si_rating"]
SortOrder = Literal["asc", "desc"]

_COUNTRY_ALIASES = {
    "russia": "RUS", "ru": "RUS", "rus": "RUS", "россия": "RUS", "рф": "RUS",
    "foreign": "nRUS", "abroad": "nRUS", "nrus": "nRUS", "зарубежные": "nRUS", "зарубежные страны": "nRUS",
}
_LANGUAGE_ALIASES = {
    "russian": "RU", "foreign": "nRU", "nru": "nRU", "multi": "ML", "multilingual": "ML",
    "english": "EN", "иностранный": "nRU",
}


def normalize_code(field: str, value: Any) -> str | None:
    """Readable value or raw site code -> readable value for a coded field of :class:`JournalQuery`.

    ``normalize_code("vak", "2") == "k1"``, ``normalize_code("white", "5") == "l4"``,
    ``normalize_code("scopus", "Q2") == "q2"``. Raises ``ValueError`` (Russian message) otherwise.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{field}: ожидается строка, получено {value!r}")
    table = CODE_TABLES[field]
    raw = str(value).strip()
    if not raw:
        return None
    low = raw.lower().replace(" ", "")
    if low in table:
        return low
    alias = _ALIASES.get(field, {}).get(low)
    if alias:
        return alias
    by_code = {code: name for name, code in table.items()}
    if raw in by_code:
        return by_code[raw]
    if raw.isdigit():
        for code, name in by_code.items():
            if int(code) == int(raw):
                return name
    allowed = ", ".join(f"{name} ({code})" for name, code in table.items())
    raise ValueError(f"Недопустимое значение {field}={value!r}. Допустимые значения (код сайта): {allowed}")


def _clean_str(v: Any) -> Any:
    if isinstance(v, str):
        v = " ".join(v.replace("\xa0", " ").split())
        return v or None
    return v


def _lookup(field: str, text: str) -> str | None:
    # Lazy import: queries.journals imports this module.
    from elibrary_cli.queries.journals import find_option

    return find_option(field, text)


def journal_url(journal_id: int, *, english: bool = False) -> str:
    """Journal profile ``title_about_new.asp?id=ID`` (``&langid=2``: English description)."""
    return f"{BASE_URL}/title_about_new.asp?id={journal_id}" + ("&langid=2" if english else "")


def journal_contents_url(journal_id: int) -> str:
    """Contents of the latest issue (and the list of issues): ``contents.asp?titleid=ID``."""
    return f"{BASE_URL}/contents.asp?titleid={journal_id}"


def journal_items_url(journal_id: int) -> str:
    """All articles of the journal: ``title_items.asp?id=ID``."""
    return f"{BASE_URL}/title_items.asp?id={journal_id}"


def _journal_analysis_url(journal_id: int) -> str:
    """Publication-activity analysis (impact factors...): ``title_profile.asp?id=ID``."""
    return f"{BASE_URL}/title_profile.asp?id={journal_id}"


def issue_contents_url(issue_id: int) -> str:
    """Contents of one issue: ``contents.asp?id=ISSUEID``."""
    return f"{BASE_URL}/contents.asp?id={issue_id}"


# --------------------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------------------


class JournalQuery(ElibModel):
    """Journal catalog search (``titles.asp``). All criteria are optional and combined with AND.

    Coded filters accept the readable values listed in each description and also the raw site codes
    (e.g. ``vak="2"`` is the same as ``vak="k1"``)."""

    name: str | None = Field(
        None, description="Fragment of the journal title, publisher name or ISSN (form field 'titlename')"
    )
    country: str | None = Field(
        None,
        description="Country of the journal: 'RUS' (Россия), 'nRUS' (зарубежные страны), an ISO 3166-1 alpha-3 "
        "code ('DEU') or a Russian country name ('Германия') (field 'countryid')",
    )
    rubric_code: str | None = Field(
        None,
        description="Top-level GRNTI rubric: 6-digit code ('500000', '270000'; '27' is expanded), "
        "'1'/'2'/'3' for multidisciplinary journals, or the rubric name ('Математика') (field 'rubriccode')",
    )
    language: str | None = Field(
        None,
        description="Publication language: 'RU', 'nRU' (иностранный), 'ML' (несколько языков), 'EN', other "
        "2-letter site codes, or a Russian language name ('английский') (field 'language')",
    )
    translated: TranslatedFilter | None = Field(
        None, description="Translation: full (переводится полностью), partial (отдельные статьи), no (не переводится)"
    )
    wos: WosFilter | None = Field(
        None,
        description="Web of Science: indexed, translated (переводная версия), partial, q1..q4 (квартиль), "
        "q1-q4 (любой квартиль), not (не индексируется)",
    )
    scopus: ScopusFilter | None = Field(
        None, description="Scopus: indexed, translated, partial, q1..q4 (квартиль), not"
    )
    white: WhiteFilter | None = Field(
        None,
        description="Белый список / ЕГПНИ: included, l1..l4 (уровень Белого списка), not; egpni, egpni_l1..egpni_l4, "
        "egpni_not; any (ЕГПНИ или Белый список), any_l1..any_l4, any_not",
    )
    vak: VakFilter | None = Field(
        None, description="Перечень ВАК: included (входит), k1/k2/k3 (категория К1..К3), not (не входит)"
    )
    risc: RiscFilter | None = Field(
        None, description="РИНЦ / Science Index: indexed (в РИНЦ), not (не в РИНЦ), q1..q4 (квартиль рейтинга Science Index)"
    )
    ftopen: FtOpenFilter | None = Field(
        None, description="Full texts: open (открытый доступ), partial (частично), subscription (по подписке)"
    )
    rsci: bool = Field(False, description="Only journals of the RSCI database (Russian Science Citation Index on WoS)")
    doaj: bool = Field(False, description="Only journals indexed in DOAJ")
    core_rinc: bool = Field(False, description="Only journals of the RSCI core (ядро РИНЦ)")
    fulltext: bool = Field(False, description="Only journals with full texts on eLIBRARY.RU")
    active: bool = Field(False, description="Only journals published now (not discontinued or renamed)")
    scientific: bool = Field(False, description="Only scientific journals (no abstract/popular-science periodicals)")
    sort: JournalSort = Field(
        "citations_core",
        description="Sort key: name, articles (число статей), citations_rinc (цитирования в РИНЦ), "
        "citations_core (цитирования из ядра РИНЦ, default of the site), si_rating (рейтинг Science Index)",
    )
    order: SortOrder | None = Field(
        None, description="asc/desc; default as in the browser: asc for name, desc for the other sort keys"
    )
    page: int = Field(1, ge=1, description="Result page")

    @field_validator("name", "country", "rubric_code", "language", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        return _clean_str(v)

    @field_validator("translated", "wos", "scopus", "white", "vak", "risc", "ftopen", "sort", "order", mode="before")
    @classmethod
    def _codes(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None and info.field_name == "sort":
            return "citations_core"
        return normalize_code(info.field_name, v)

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        if not v:
            return None
        low = v.lower()
        if low in _COUNTRY_ALIASES:
            return _COUNTRY_ALIASES[low]
        if re.fullmatch(r"[A-Za-z]{3}", v):
            return v.upper()
        code = _lookup("countryid", v)
        if code:
            return code
        raise ValueError(
            f"Неизвестная страна {v!r}: укажите код ISO 3166-1 alpha-3 (RUS, DEU, ...), nRUS (зарубежные) "
            "или название страны, как на сайте (Россия, Германия, ...)"
        )

    @field_validator("language")
    @classmethod
    def _language(cls, v: str | None) -> str | None:
        if not v:
            return None
        low = v.lower()
        if low in _LANGUAGE_ALIASES:
            return _LANGUAGE_ALIASES[low]
        if re.fullmatch(r"[A-Za-z]{2}", v):
            return v.upper()
        code = _lookup("language", v)
        if code:
            return code
        raise ValueError(
            f"Неизвестный язык {v!r}: укажите код сайта (RU, EN, nRU, ML, ...) или название языка (русский, английский, ...)"
        )

    @field_validator("rubric_code")
    @classmethod
    def _rubric(cls, v: str | None) -> str | None:
        if not v:
            return None
        digits = v.replace(".", "").replace(" ", "")
        if digits.isdigit():
            if digits in {"1", "2", "3"} or len(digits) == 6:
                return digits
            if len(digits) <= 2:
                return f"{int(digits):02d}0000"
            if len(digits) == 4:
                return digits + "00"
        code = _lookup("rubriccode", v)
        if code:
            return code
        raise ValueError(
            f"Неизвестная рубрика {v!r}: укажите код ГРНТИ верхнего уровня (например, 270000 или 27), "
            "1/2/3 для мультидисциплинарных журналов или название рубрики"
        )

    @property
    def effective_order(self) -> SortOrder:
        """The order the browser would send for this sort key when ``order`` is not given
        (the form's JS sets 'по возрастанию' for name and 'по убыванию' for the others)."""
        if self.order:
            return self.order
        return "asc" if self.sort == "name" else "desc"


class JournalHit(ElibModel):
    """One row of the journal catalog search results (``titles.asp``)."""

    id: int = Field(description="eLIBRARY journal id (titleid, title_about_new.asp?id=...)")
    name: str
    publisher: str | None = Field(None, description="Publisher / founder line shown under the title")
    issn: str | None = Field(None, description="ISSN, when the row shows one")
    issues: int | None = Field(None, description="Issues of the journal on eLIBRARY.RU (column 'Вып.')")
    articles: int | None = Field(None, description="Articles of the journal in RSCI (РИНЦ) (column 'Публ.')")
    citations: int | None = Field(None, description="Citations shown in the column 'Цит.' (see citations_kind)")
    citations_kind: str | None = Field(
        None, description="What the citations column counts (its header tooltip), e.g. 'Число цитирований статей из ядра РИНЦ'"
    )
    position: int | None = Field(None, description="Row number in the result list")
    extra: dict[str, str] = Field(default_factory=dict, description="Other columns: header -> cell text")
    url: str = Field("", description="Journal profile (title_about_new.asp?id=ID)")
    contents_url: str = Field("", description="Latest issue and list of issues (contents.asp?titleid=ID)")
    items_url: str = Field("", description="All articles of the journal (title_items.asp?id=ID)")

    def model_post_init(self, __context: Any) -> None:
        self.url = self.url or journal_url(self.id)
        self.contents_url = self.contents_url or journal_contents_url(self.id)
        self.items_url = self.items_url or journal_items_url(self.id)


# --------------------------------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------------------------------


class JournalOrgRef(ElibModel):
    """An organization mentioned on a journal page (founder, publisher, editorial office, affiliation)."""

    id: int | None = Field(None, description="eLIBRARY organization id (org_profile.asp?id=...), when linked")
    name: str
    city: str | None = Field(None, description="City shown in parentheses after the name")
    url: str | None = None


class EditorialMember(ElibModel):
    """A person of the editorial staff (chief editor, deputies, secretary, board).

    Personal e-mail addresses shown next to the names (envelope links) are deliberately not kept;
    the only e-mail of a journal profile is the editorial office contact (``JournalProfile.email``)."""

    name: str
    role: str | None = Field(
        None, description="Block heading the person is listed under, e.g. 'ГЛАВНЫЙ РЕДАКТОР', 'РЕДАКЦИОННАЯ КОЛЛЕГИЯ'"
    )
    author_id: int | None = Field(None, description="eLIBRARY AuthorID (author_profile.asp?authorid=...), when linked")
    degree: str | None = Field(None, description="Degree / title from the info icon tooltip, e.g. 'д.т.н.', 'академик РАН'")
    affiliations: list[JournalOrgRef] = Field(default_factory=list)
    url: str | None = Field(None, description="Author profile URL, when linked")


class Rubric(ElibModel):
    """A subject classification entry (GRNTI, OECD, VAK specialty...)."""

    code: str | None = Field(None, description="Code as shown without the trailing dot, e.g. '270000', '101'")
    name: str


class JournalSection(ElibModel):
    """A main section (rubric) of the journal ('ОСНОВНЫЕ РАЗДЕЛЫ')."""

    id: int | None = Field(None, description="Section id (title_items.asp?id=TITLEID&section=ID)")
    name: str
    url: str | None = Field(None, description="Articles of this section")


class JournalFile(ElibModel):
    """An additional file of the journal ('ДОПОЛНИТЕЛЬНЫЕ ФАЙЛЫ'); opening it requires signing in."""

    kind: str | None = Field(None, description="Purpose, e.g. 'Правила для авторов', 'Положение об издательской этике'")
    name: str = Field(description="File name as shown")
    file_id: int | None = Field(None, description="Number passed to show_titlefile(N) on the page")


class PeerReview(ElibModel):
    """The 'РЕЦЕНЗИРОВАНИЕ' block."""

    type: str | None = Field(None, description="'Вид', e.g. 'одностороннее слепое рецензирование'")
    reviewers: int | None = Field(None, description="'Число рецензентов'")
    rejection_rate: float | None = Field(None, description="'Доля отклоненных рукописей', percent")
    publication_time: str | None = Field(None, description="'Срок публикации' as shown, e.g. '120 дней'")
    publication_days: int | None = Field(None, description="'Срок публикации' in days, when given in days")
    performed_by: str | None = Field(None, description="'Рецензирование осуществляется'")
    extra: dict[str, str] = Field(default_factory=dict)


class JournalIndexingInfo(ElibModel):
    """The 'ИНДЕКСАЦИЯ' block of the journal profile."""

    elibrary: bool | None = Field(None, description="Licence agreement with eLIBRARY.RU ('eLIBRARY.RU: да')")
    elibrary_contract: str | None = Field(None, description="Agreement number, e.g. '290-10/2022'")
    risc: bool | None = Field(None, description="Indexed in RSCI (РИНЦ)")
    rsci: bool | None = Field(None, description="In the Russian Science Citation Index database on Web of Science")
    core_rinc: bool | None = Field(None, description="In the RSCI core (ядро РИНЦ)")
    doaj: bool | None = None
    wos: bool | None = Field(None, description="Indexed in Web of Science Core Collection")
    wos_quartile: int | None = Field(None, description="Web of Science quartile 1..4")
    scopus: bool | None = None
    scopus_quartile: int | None = Field(None, description="Scopus (Scimago) quartile 1..4")
    white_list: bool | None = Field(None, description="In the Russian 'Белый список'")
    white_list_level: int | None = Field(None, description="Level in the white list, 1..4")
    egpni: bool | None = Field(None, description="In ЕГПНИ (Единый государственный перечень научных изданий)")
    egpni_level: int | None = Field(None, description="Level in ЕГПНИ, 1..4")
    vak: bool | None = Field(None, description="In the VAK list (Перечень ВАК РФ)")
    vak_category: int | None = Field(None, description="VAK category К1..К3 as 1..3")
    science_index: bool | None = Field(None, description="Takes part in the Science Index journal rating")
    science_index_percentile: int | None = Field(None, description="Percentile in the Science Index rating")
    crossref: bool | None = Field(None, description="Agreement with CrossRef for DOI registration")
    doi_prefix: str | None = Field(None, description="DOI prefix of the publisher/journal, e.g. '10.25743/ICT.'")
    databases: list[str] = Field(default_factory=list, description="'Базы данных': databases that index the journal")
    extra: dict[str, str] = Field(default_factory=dict, description="Other indexing rows: label -> value")


class IssueRef(ElibModel):
    """A reference to an issue of a journal (issue list of contents.asp)."""

    id: int | None = Field(None, description="Issue id (contents.asp?id=...)")
    year: int | None = None
    volume: str | None = Field(None, description="Volume ('Т.'), as shown")
    number: str | None = Field(None, description="Issue number ('№'), as shown: '4', 'S1', '1-2'")
    label: str = Field(description="Issue label as shown, e.g. 'Т. 31 № 4', 'Online First'")
    articles: int | None = Field(None, description="Number of articles shown next to the issue ('(9 ст.)')")
    current: bool = Field(False, description="True for the issue the page shows")
    url: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.url and self.id:
            self.url = issue_contents_url(self.id)


#: Typed indicators of title_profile.asp: field -> (label regex, kind). Matched against labels.
INDICATOR_FIELDS: dict[str, tuple[str, str]] = {
    "impact_factor_2y": (r"^двухлетний импакт-фактор ринц$", "float"),
    "impact_factor_2y_no_self": (r"^двухлетний импакт-фактор ринц без самоцитирования$", "float"),
    "impact_factor_5y": (r"^пятилетний импакт-фактор ринц$", "float"),
    "impact_factor_core": (r"^двухлетний импакт-фактор по ядру ринц", "float"),
    "science_index_rating": (r"^показатель журнала в рейтинге science index", "float"),
    "science_index_rank": (r"^место в общем рейтинге science index", "int"),
    "h_index": (r"^индекс хирша$", "int"),
    "citations_total": (r"^суммарное число цитирований журнала в ринц", "int"),
    "self_citation_percent": (r"самоцитировани.*%|^процент самоцитирования|^доля самоцитирования", "float"),
}


class JournalIndicators(ElibModel):
    """Bibliometric indicators of a journal (``title_profile.asp?id=ID``).

    Typed fields are filled from well-known rows; ``metrics`` keeps every row as shown."""

    journal_id: int | None = None
    journal_name: str | None = None
    year: int | None = Field(None, description="Year the indicators are calculated for, when shown")
    impact_factor_2y: float | None = Field(None, description="Двухлетний импакт-фактор РИНЦ")
    impact_factor_2y_no_self: float | None = Field(None, description="Двухлетний импакт-фактор РИНЦ без самоцитирования")
    impact_factor_5y: float | None = Field(None, description="Пятилетний импакт-фактор РИНЦ")
    impact_factor_core: float | None = Field(None, description="Двухлетний импакт-фактор по ядру РИНЦ")
    science_index_rating: float | None = Field(None, description="Показатель журнала в рейтинге SCIENCE INDEX")
    science_index_rank: int | None = Field(None, description="Место в общем рейтинге SCIENCE INDEX")
    h_index: int | None = Field(None, description="Индекс Хирша журнала")
    citations_total: int | None = Field(None, description="Суммарное число цитирований журнала в РИНЦ")
    self_citation_percent: float | None = Field(None, description="Self-citation share, percent")
    metrics: dict[str, str] = Field(
        default_factory=dict,
        description="Every indicator row: name as shown -> value as shown ('Section: name' when a name repeats)",
    )
    url: str | None = None


class JournalProfile(ElibModel):
    """Journal profile (``title_about_new.asp?id=ID``); indicators can be merged from
    ``title_profile.asp`` with :meth:`with_indicators`."""

    id: int = Field(description="eLIBRARY journal id (titleid)")
    name: str = Field(description="Journal title (from the page title; the heading shows it in capitals)")
    name_en: str | None = Field(None, description="English title (filled when the English description, langid=2, is parsed)")
    description_languages: list[str] = Field(
        default_factory=list, description="'Язык описания': languages the profile is available in"
    )
    english_url: str | None = Field(None, description="URL of the English description (title_about_new.asp?id=ID&langid=2)")
    serial_type: str | None = Field(None, description="'Тип сериального издания', e.g. 'периодическое издание'")
    serial_elements: str | None = Field(None, description="'Элементы сериального издания', e.g. 'выпуск журнала'")
    purpose: str | None = Field(None, description="'Назначение издания', e.g. 'научное'")
    distribution: str | None = Field(None, description="'Способ распространения'")
    fulltext_access: str | None = Field(None, description="'Доступ к полным текстам' as shown")
    open_access: Literal["open", "partial", "subscription"] | None = Field(
        None, description="Open access derived from 'Доступ к полным текстам'"
    )
    funding: str | None = Field(None, description="'Основной источник финансирования'")
    multidisciplinary: bool | None = Field(None, description="'Мультидисциплинарность'")
    languages: list[str] = Field(default_factory=list, description="'Язык публикаций'")
    founders: list[JournalOrgRef] = Field(default_factory=list, description="'УЧРЕДИТЕЛИ'")
    publishers: list[JournalOrgRef] = Field(default_factory=list, description="'ИЗДАТЕЛЬСТВО' / 'ИЗДАТЕЛЬ', when shown")
    publisher: str | None = Field(None, description="Name of the first publisher, when shown")
    editorial_office: list[JournalOrgRef] = Field(default_factory=list, description="'МЕСТО РАСПОЛОЖЕНИЯ РЕДАКЦИИ'")
    issn: str | None = Field(None, description="ISSN of the print version")
    eissn: str | None = Field(None, description="ISSN of the electronic version")
    issues_per_year: int | None = Field(None, description="'Число выпусков в год' (frequency)")
    foundation_year: int | None = Field(None, description="'Год основания'")
    articles_per_issue: int | None = Field(None, description="'Число статей в выпуске'")
    pages_per_issue: int | None = Field(None, description="'Число страниц в выпуске'")
    publication_period: str | None = Field(None, description="'Период выпуска', e.g. '1996-...'")
    active: bool | None = Field(None, description="Still published: the period has no end year")
    archive: str | None = Field(None, description="'Архив на eLIBRARY.RU': years available, e.g. '1996-2026'")
    articles_total: int | None = Field(None, description="'Всего статей на eLIBRARY.RU'")
    issues_total: int | None = Field(None, description="'Всего выпусков на eLIBRARY.RU'")
    media_registration: str | None = Field(None, description="'Свидетельство о регистрации СМИ'")
    editorial_policy: str | None = Field(None, description="'РЕДАКЦИОННАЯ ПОЛИТИКА' text")
    indexing: JournalIndexingInfo = Field(default_factory=JournalIndexingInfo)
    country: str | None = None
    city: str | None = None
    address: str | None = None
    email: str | None = Field(
        None, description="Contact e-mail of the editorial office ('КОНТАКТНАЯ ИНФОРМАЦИЯ' block only)"
    )
    phone: str | None = None
    website: str | None = None
    editor_in_chief: EditorialMember | None = None
    deputy_editors: list[EditorialMember] = Field(default_factory=list)
    executive_secretaries: list[EditorialMember] = Field(default_factory=list, description="'ОТВЕТСТВЕННЫЙ СЕКРЕТАРЬ'")
    editorial_board: list[EditorialMember] = Field(default_factory=list, description="'РЕДАКЦИОННАЯ КОЛЛЕГИЯ'")
    other_staff: dict[str, list[EditorialMember]] = Field(
        default_factory=dict, description="Other people sections: heading -> members"
    )
    peer_review: PeerReview | None = None
    rubrics_grnti: list[Rubric] = Field(default_factory=list, description="'РУБРИКИ ГРНТИ'")
    rubrics_oecd: list[Rubric] = Field(default_factory=list, description="'РУБРИКИ OECD'")
    vak_specialties: list[Rubric] = Field(default_factory=list, description="'СПЕЦИАЛЬНОСТИ ВАК'")
    other_rubrics: dict[str, list[Rubric]] = Field(
        default_factory=dict, description="Other classification blocks: heading -> entries"
    )
    sections: list[JournalSection] = Field(default_factory=list, description="'ОСНОВНЫЕ РАЗДЕЛЫ'")
    files: list[JournalFile] = Field(default_factory=list, description="'ДОПОЛНИТЕЛЬНЫЕ ФАЙЛЫ'")
    history: str | None = Field(None, description="'ИСТОРИЯ ПЕРЕИМЕНОВАНИЙ И ПЕРЕВОДА'")
    last_modified: str | None = Field(None, description="'Дата последнего изменения данных об издании' (ДД.ММ.ГГГГ)")
    impact_factor_2y: float | None = Field(None, description="Двухлетний импакт-фактор РИНЦ (from title_profile.asp)")
    impact_factor_5y: float | None = Field(None, description="Пятилетний импакт-фактор РИНЦ (from title_profile.asp)")
    science_index_rating: float | None = Field(None, description="Показатель в рейтинге SCIENCE INDEX (title_profile.asp)")
    h_index: int | None = Field(None, description="Индекс Хирша журнала (title_profile.asp)")
    metrics_year: int | None = Field(None, description="Year of the indicators, when shown")
    metrics: dict[str, str] = Field(
        default_factory=dict, description="All indicator rows (title_profile.asp): name -> value as shown"
    )
    latest_issue: IssueRef | None = Field(None, description="Latest issue, when the page links one")
    extra: dict[str, str] = Field(default_factory=dict, description="Other label/value pairs and unknown blocks")
    url: str = ""
    contents_url: str = Field("", description="contents.asp?titleid=ID: latest issue and list of issues")
    items_url: str = Field("", description="title_items.asp?id=ID: all articles")
    analysis_url: str = Field("", description="title_profile.asp?id=ID: publication-activity analysis")

    def model_post_init(self, __context: Any) -> None:
        self.url = self.url or journal_url(self.id)
        self.contents_url = self.contents_url or journal_contents_url(self.id)
        self.items_url = self.items_url or journal_items_url(self.id)
        self.analysis_url = self.analysis_url or _journal_analysis_url(self.id)

    def with_indicators(self, ind: JournalIndicators) -> JournalProfile:
        """A copy of the profile with the indicators of ``title_profile.asp`` merged in."""
        metrics = {**self.metrics, **ind.metrics}
        update: dict[str, Any] = {"metrics": metrics}
        for f in ("impact_factor_2y", "impact_factor_5y", "science_index_rating", "h_index"):
            val = getattr(ind, f)
            if val is not None:
                update[f] = val
        if ind.year is not None:
            update["metrics_year"] = ind.year
        return self.model_copy(update=update)


# --------------------------------------------------------------------------------------------------
# Issue contents
# --------------------------------------------------------------------------------------------------


class IssueContents(ElibModel):
    """Contents of a journal issue (``contents.asp``)."""

    journal_id: int | None = Field(None, description="eLIBRARY journal id (titleid)")
    journal_name: str | None = None
    publisher: JournalOrgRef | None = Field(None, description="Organization shown under the journal title")
    issue_id: int | None = Field(None, description="Issue id (contents.asp?id=...)")
    edn: str | None = Field(None, description="EDN of the issue")
    label: str | None = Field(None, description="Issue label from the page title, e.g. 'Т.31 № 4 за 2026 год'")
    year: int | None = None
    volume: str | None = Field(None, description="Volume ('Том'), as shown")
    number: str | None = Field(None, description="Issue number ('Номер'), as shown")
    articles_count: int | None = Field(None, description="Number of articles of the issue shown in the issue list")
    sections: list[str] = Field(default_factory=list, description="Section headings in page order")
    items: list[PublicationHit] = Field(default_factory=list, description="Articles with section and page range")
    available_issues: list[IssueRef] = Field(
        default_factory=list, description="All issues listed on the page (newest year first), including this one"
    )
    extra: dict[str, str] = Field(default_factory=dict, description="Other values shown on the page (e.g. issue price)")
    url: str | None = Field(None, description="Canonical issue URL (contents.asp?id=ISSUEID)")

    def model_post_init(self, __context: Any) -> None:
        if not self.url and self.issue_id:
            self.url = issue_contents_url(self.issue_id)


__all__ = [
    "CODE_TABLES",
    "FTOPEN_CODES",
    "INDICATOR_FIELDS",
    "ORDER_CODES",
    "RISC_CODES",
    "SCOPUS_CODES",
    "SORT_CODES",
    "TRANSLATED_CODES",
    "VAK_CODES",
    "WHITE_CODES",
    "WOS_CODES",
    "EditorialMember",
    "IssueContents",
    "IssueRef",
    "JournalFile",
    "JournalHit",
    "JournalIndexingInfo",
    "JournalIndicators",
    "JournalProfile",
    "JournalQuery",
    "JournalSection",
    "JournalOrgRef",
    "PeerReview",
    "Rubric",
    "issue_contents_url",
    "journal_contents_url",
    "journal_items_url",
    "journal_url",
    "normalize_code",
]
