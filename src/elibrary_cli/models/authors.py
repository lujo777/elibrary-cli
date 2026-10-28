"""Models of the author pages: ``authors.asp`` (search), ``author_profile.asp`` (publication-activity
analysis) and ``author_items.asp`` (publication list of an author)."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator

from elibrary_cli.models.base import BASE_URL, ElibModel, PublicationHit, SearchPage

CodeType = Literal["SPIN", "ORCID", "ResearcherID", "ScopusID", "ArXivID", "RSCI"]
MetricsBasis = Literal["core", "rinc", "elibrary"]
AuthorSort = Literal["name", "publications", "citations", "hindex"]
SortDirection = Literal["asc", "desc"]
AuthorItemsSort = Literal[
    "date", "title", "year_author", "year_title", "first_author", "citations", "added", "relevance"
]

#: ``metrics`` select of authors.asp: basis of the publication / citation / h-index columns.
METRICS_CODES: dict[str, str] = {"core": "0", "rinc": "1", "elibrary": "2"}
#: ``sortorder`` select of authors.asp.
AUTHOR_SORT_CODES: dict[str, str] = {"name": "0", "publications": "1", "citations": "2", "hindex": "3"}
#: ``order`` select of authors.asp and author_items.asp.
ORDER_CODES: dict[str, str] = {"asc": "0", "desc": "1"}
#: ``sortorder`` select of author_items.asp (values as on the page, 2026-10).
AUTHOR_ITEMS_SORT_CODES: dict[str, str] = {
    "date": "0",          # по дате выпуска
    "title": "1",         # по названию публикации
    "year_author": "2",   # по году и первому автору
    "year_title": "6",    # по году и названию
    "first_author": "5",  # по первому автору
    "citations": "3",     # по числу цитирований
    "added": "4",         # по дате размещения
    "relevance": "7",     # по тематической близости
}

_ISO3_RE = re.compile(r"^[A-Z]{3}$")
_RUBRIC_RE = re.compile(r"^\d{6}$")
_WS_RE = re.compile(r"\s+")

#: Validation context key: ``org_name`` counts as a selective criterion, because the caller
#: (``ElibraryClient.search_authors``) resolves it to ``org_id`` before the form is sent.
CONTEXT_RESOLVE_ORG_NAME = "resolve_org_name"

#: English names (and common aliases in both languages) of the countries of the ``countryid`` select
#: of authors.asp (``data/authors_options.json``; the Russian labels come from that file).
COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "RUS": ("Russia", "Russian Federation", "RF", "РФ", "Российская Федерация"),
    "ABH": ("Abkhazia",), "AUS": ("Australia",), "AUT": ("Austria",), "AZE": ("Azerbaijan",),
    "ALB": ("Albania",), "DZA": ("Algeria",), "AGO": ("Angola",), "ARM": ("Armenia",), "AFG": ("Afghanistan",),
    "BLR": ("Belarus", "Belorussia", "Byelorussia", "Белоруссия"), "BEL": ("Belgium",), "BGR": ("Bulgaria",),
    "BIH": ("Bosnia and Herzegovina", "Bosnia"), "BRA": ("Brazil",),
    "GBR": ("United Kingdom", "UK", "Great Britain", "Britain", "England", "Англия", "Соединенное Королевство"),
    "HUN": ("Hungary",), "VNM": ("Vietnam", "Viet Nam"), "GHA": ("Ghana",), "DEU": ("Germany", "ФРГ"),
    "HKG": ("Hong Kong",), "GRC": ("Greece",), "GEO": ("Georgia",), "DNK": ("Denmark",), "EGY": ("Egypt",),
    "ISR": ("Israel",), "IND": ("India",), "IDN": ("Indonesia",), "JOR": ("Jordan",), "IRQ": ("Iraq",),
    "IRN": ("Iran",), "IRL": ("Ireland",), "ESP": ("Spain",), "ITA": ("Italy",), "YEM": ("Yemen",),
    "KAZ": ("Kazakhstan",), "CAN": ("Canada",), "CYP": ("Cyprus",),
    "KGZ": ("Kyrgyzstan", "Kirghizia", "Kyrgyz Republic", "Кыргызстан"), "CHN": ("China", "PRC", "КНР"),
    "COL": ("Colombia",), "LVA": ("Latvia",), "LBN": ("Lebanon",), "LTU": ("Lithuania",), "MYS": ("Malaysia",),
    "MLI": ("Mali",), "MEX": ("Mexico",), "MDA": ("Moldova", "Moldavia", "Молдавия"), "MNG": ("Mongolia",),
    "NGA": ("Nigeria",), "NLD": ("Netherlands", "Holland", "Голландия"), "NOR": ("Norway",),
    "ARE": ("United Arab Emirates", "UAE", "Emirates", "ОАЭ"), "PAK": ("Pakistan",), "PER": ("Peru",),
    "POL": ("Poland",), "PRT": ("Portugal",),
    "KOR": ("South Korea", "Korea", "Republic of Korea", "Корея", "Южная Корея"), "ROU": ("Romania",),
    "MKD": ("North Macedonia", "Macedonia", "Македония"), "SRB": ("Serbia",), "SGP": ("Singapore",),
    "SYR": ("Syria",), "SVK": ("Slovakia",),
    "USA": ("United States", "United States of America", "US", "America", "Соединенные Штаты", "Америка"),
    "TJK": ("Tajikistan",), "THA": ("Thailand",), "TUN": ("Tunisia",), "TKM": ("Turkmenistan", "Туркменистан"),
    "TUR": ("Turkey", "Türkiye", "Turkiye"), "UZB": ("Uzbekistan",), "UKR": ("Ukraine",), "FIN": ("Finland",),
    "FRA": ("France",), "HRV": ("Croatia",), "MNE": ("Montenegro",),
    "CZE": ("Czech Republic", "Czechia", "Чешская Республика"), "CHE": ("Switzerland",), "SWE": ("Sweden",),
    "ECU": ("Ecuador",), "EST": ("Estonia",), "OST": ("South Ossetia",),
    "ZAF": ("South Africa", "ЮАР"), "JPN": ("Japan",),
}


def _strip(v: object) -> object:
    if isinstance(v, str):
        v = _WS_RE.sub(" ", v.replace("\xa0", " ")).strip()
        return v or None
    return v


def _norm_country(text: str) -> str:
    text = text.replace("ё", "е").replace("Ё", "Е").replace(".", " ").replace("-", " ")
    return _WS_RE.sub(" ", text).strip().casefold()


def resolve_country(value: str) -> str:
    """ISO 3166-1 alpha-3 code of the ``countryid`` select of ``authors.asp`` for a code (``rus``), a
    Russian country name as in the form (``Россия``, ``Республика Корея``), an English name or a common
    alias (``Russia``, ``Korea``, ``UAE``). A label that starts with / contains the text is accepted when
    it is the only one. A valid-looking ISO3 code that is not in the snapshot is passed through.

    Raises ``ValueError`` (Russian message) for an unknown or ambiguous name."""
    from elibrary_cli import refdata  # lazy: refdata reads this module's code tables

    text = str(_strip(value) or "")
    if not text:
        raise ValueError("пустое название страны")
    options = refdata.list_options("authors.countryid")
    codes = {o["value"].upper(): o["value"] for o in options}
    if text.upper() in codes:
        return codes[text.upper()]
    names: dict[str, str] = {_norm_country(o["label"]): o["value"] for o in options}
    for code, aliases in COUNTRY_ALIASES.items():
        for alias in aliases:
            names.setdefault(_norm_country(alias), code)
    key = _norm_country(text)
    if key in names:
        return names[key]
    if text.isascii() and _ISO3_RE.match(text.upper()):
        return text.upper()
    # a unique label/name that starts with the text, else a unique one that contains it
    found = {code for name, code in names.items() if name.startswith(key)}
    if not found:
        found = {code for name, code in names.items() if key in name}
    if len(found) == 1:
        return found.pop()
    labels = {o["value"]: o["label"] for o in options}
    hint = ", ".join(f"{labels.get(c, c)} ({c})" for c in sorted(found, key=lambda c: labels.get(c, c))[:10])
    raise ValueError(
        f"неизвестная страна {value!r}: укажите трёхбуквенный код ISO 3166 (например, RUS) или название "
        "страны по-русски или по-английски (Россия, Russia)" + (f"; подходят: {hint}" if hint else "")
    )


class AuthorQuery(ElibModel):
    """Parameters of the author search form (``POST /authors.asp``).

    At least one selective criterion is required: ``surname``, ``code_value``, ``org_id`` or
    ``author_group``. ``org_name`` alone is not enough: the site filters by ``orgid``; the name is
    only the text of the read-only form field. ``ElibraryClient.search_authors`` resolves an
    ``org_name`` without ``org_id`` to an organization id (validation context
    :data:`CONTEXT_RESOLVE_ORG_NAME`), so there the name alone is accepted."""

    surname: str | None = Field(
        None,
        description="Фамилия автора на русском языке; можно 'Фамилия Имя Отчество' или 'Фамилия И.О.'",
    )
    code_type: CodeType = Field(
        "SPIN",
        description="Type of the personal identifier in code_value: SPIN, ORCID, ResearcherID, ScopusID, "
        "ArXivID or RSCI (RSCI = eLIBRARY/РИНЦ AuthorID)",
    )
    code_value: str | None = Field(None, description="Personal identifier value, e.g. SPIN '9846-9426'")
    country: str | None = Field(
        None,
        description="Country: ISO 3166 alpha-3 code (RUS, BLR, KAZ, ...) or a Russian / English name "
        "('Россия', 'Russia'); stored as the code",
    )
    town: str | None = Field(None, description="Town name as typed into the 'Город' field")
    town_id: int | None = Field(None, description="eLIBRARY town id (from town_autocomplete2.asp), optional")
    region_id: int | None = Field(None, description="eLIBRARY region id (from town_autocomplete2.asp), optional")
    org_id: int | None = Field(None, description="eLIBRARY organization id (org_profile.asp?id=...)")
    org_name: str | None = Field(
        None,
        description="Organization name shown in the form; the site ignores it (filters by org_id), "
        "ElibraryClient.search_authors resolves it to org_id when org_id is not given",
    )
    in_affiliation: bool = Field(
        False, description="Also match authors who have a publication with this organization in the affiliation"
    )
    author_group: str | None = Field(
        None,
        description="Reference group (authorboxid): r1 Профессора МГУ, r2 Выдающиеся профессора МГУ, "
        "r3 Заслуженные профессора МГУ, r4 Члены РАН за всю историю, r5 Актуальный список членов РАН",
    )
    rubric_code: str | None = Field(None, description="GRNTI rubric code of the main topic, 6 digits (e.g. 290000)")
    rubric_in_profile: bool = Field(
        False, description="Also use the rubrics from the author's registration form (rubricinreg)"
    )
    metrics: MetricsBasis = Field(
        "rinc", description="Basis of the shown publications/citations/h-index: core (ядро РИНЦ), rinc, elibrary"
    )
    sort: AuthorSort = Field("citations", description="Sort by: name, publications, citations, hindex")
    order: SortDirection = "desc"
    only_with_publications: bool = Field(
        False,
        description="Show only authors who have at least one publication (checkbox show_nopubl; note the "
        "site's field name is misleading)",
    )
    page: int = Field(1, ge=1, description="Result page, 100 authors per page")

    @field_validator("surname", "code_value", "town", "org_name", "author_group", mode="before")
    @classmethod
    def _strip_text(cls, v: object) -> object:
        return _strip(v)

    @field_validator("country", mode="before")
    @classmethod
    def _country(cls, v: object) -> object:
        v = _strip(v)
        if isinstance(v, str):
            return resolve_country(v)
        return v

    @field_validator("rubric_code", mode="before")
    @classmethod
    def _rubric(cls, v: object) -> object:
        v = _strip(v)
        if isinstance(v, int):
            v = f"{v:06d}"
        if isinstance(v, str) and not _RUBRIC_RE.match(v):
            raise ValueError("код рубрики ГРНТИ — 6 цифр (например, 290000)")
        return v

    @model_validator(mode="after")
    def _need_criterion(self, info: ValidationInfo) -> AuthorQuery:
        if self.code_value and self.code_type == "RSCI" and not self.code_value.isdigit():
            raise ValueError("РИНЦ AuthorID состоит только из цифр")
        org_name_resolved = bool(isinstance(info.context, dict) and info.context.get(CONTEXT_RESOLVE_ORG_NAME))
        if not (self.surname or self.code_value or self.org_id or self.author_group
                or (org_name_resolved and self.org_name)):
            raise ValueError(
                "Укажите фамилию автора, персональный идентификатор (SPIN, ORCID, ...), организацию "
                "(org_id) или референтную группу"
            )
        return self


class AuthorPublicationsQuery(ElibModel):
    """Parameters of an author's publication list (``author_items.asp``)."""

    author_id: int = Field(ge=1, description="eLIBRARY AuthorID")
    page: int = Field(1, ge=1)
    sort: AuthorItemsSort = Field(
        "date",
        description="date (дата выпуска), title, year_author, year_title, first_author, citations, "
        "added (дата размещения), relevance",
    )
    order: SortDirection = "desc"
    include_references: bool = Field(
        True, description="Include publications extracted from reference lists (show_refs)"
    )
    merge_versions: bool = Field(
        True, description="Merge original and translated versions / reprints (hide_doubles)"
    )
    unlinked: bool = Field(
        False, description="Show not-yet-linked publications that may belong to the author (show_hash=1)"
    )


class AuthorHit(ElibModel):
    """An author row of the author search results."""

    id: int = Field(description="eLIBRARY AuthorID")
    name: str = Field(description="Full name as shown, e.g. 'Петров Владимир Алексеевич'")
    short_name: str | None = Field(
        None,
        description="Surname and initials without dots ('Петров В А'): the key the site uses for "
        "the authors filter of the publication search (authors_all)",
    )
    science_index_registered: bool = Field(False, description="The author is registered in SCIENCE INDEX (*)")
    affiliation: str | None = Field(None, description="Organization shown in the row, without the town")
    town: str | None = Field(None, description="Town from the trailing parentheses of the affiliation")
    publications: int | None = Field(None, description="Number of publications (basis = query metrics)")
    citations: int | None = Field(None, description="Number of citations (basis = query metrics)")
    hindex: int | None = Field(None, description="h-index (basis = query metrics)")
    position: int | None = Field(None, description="Row number in the result list")
    wikipedia_url: str | None = None
    url: str = Field("", description="author_profile.asp page of the author")
    profile_url: str = ""
    publications_url: str = ""

    def model_post_init(self, __context: object) -> None:
        if not self.profile_url:
            self.profile_url = f"{BASE_URL}/author_profile.asp?id={self.id}"
        if not self.url:
            self.url = self.profile_url
        if not self.publications_url:
            self.publications_url = f"{BASE_URL}/author_items.asp?authorid={self.id}&show_refs=1"


class AuthorAffiliation(ElibModel):
    """An organization of the author (header or the 'МЕСТО РАБОТЫ' table of the profile)."""

    name: str
    org_id: int | None = Field(None, description="eLIBRARY organization id")
    town: str | None = None
    department: str | None = Field(None, description="Department / subdivision shown after the organization")
    period: str | None = Field(None, description="Years of publications with this affiliation, e.g. '1973-2023'")
    year_from: int | None = None
    year_to: int | None = None
    publications: int | None = Field(None, description="Publications of the author with this affiliation")
    publications_url: str | None = None
    url: str | None = Field(None, description="org_profile.asp page of the organization")


class AuthorMetric(ElibModel):
    """One row of the 'ОБЩИЕ ПОКАЗАТЕЛИ' table, exactly as shown."""

    name: str = Field(description="Indicator name as shown on the page")
    value: str = Field(description="Value as shown, e.g. '929 (86,9%)'")
    number: int | float | None = Field(None, description="Numeric part of the value")
    percent: float | None = Field(None, description="Percentage in parentheses, if shown")
    url: str | None = Field(None, description="Link of the value (publication / citation list), if any")


class AuthorYearlyMetric(ElibModel):
    """One row of the 'ПОКАЗАТЕЛИ ПО ГОДАМ' table."""

    name: str = Field(description="Indicator name, e.g. 'Число публикаций в РИНЦ'")
    values: dict[int, int | float | None] = Field(
        default_factory=dict, description="Year -> value (None for an empty cell)"
    )


class AuthorReport(ElibModel):
    """A link of the 'СТАТИСТИЧЕСКИЕ ОТЧЕТЫ' block (distribution reports, opened in an iframe)."""

    key: str = Field(description="Report key, e.g. rubrics, titles, cit_years")
    title: str
    url: str


class AuthorProfile(ElibModel):
    """``author_profile.asp?id=ID``: identity, affiliations and every bibliometric indicator."""

    id: int = Field(description="eLIBRARY AuthorID")
    name: str = Field(description="Full name, e.g. 'Петров Владимир Алексеевич'")
    science_index_registered: bool = Field(False, description="The author is registered in SCIENCE INDEX (*)")
    spin: str | None = Field(None, description="SPIN code, e.g. '9846-9426'")
    orcid: str | None = None
    researcher_id: str | None = Field(None, description="Web of Science ResearcherID")
    scopus_id: str | None = Field(None, description="Scopus Author ID")
    external_profiles: dict[str, str] = Field(
        default_factory=dict, description="Links of the tools panel: 'ORCID' / 'Scopus' / 'ResearcherID' -> URL"
    )
    affiliation: AuthorAffiliation | None = Field(None, description="Main (current) organization from the header")
    affiliations: list[AuthorAffiliation] = Field(
        default_factory=list, description="The 'МЕСТО РАБОТЫ' table: organizations, periods, publication counts"
    )

    publications_elibrary: int | None = Field(None, description="Число публикаций на elibrary.ru")
    publications_rinc: int | None = Field(None, description="Число публикаций в РИНЦ")
    publications_core: int | None = Field(None, description="Число публикаций, входящих в ядро РИНЦ")
    citations_elibrary: int | None = Field(None, description="Число цитирований из публикаций на elibrary.ru")
    citations_rinc: int | None = Field(None, description="Число цитирований из публикаций, входящих в РИНЦ")
    citations_core: int | None = Field(None, description="Число цитирований из публикаций, входящих в ядро РИНЦ")
    hindex_elibrary: int | None = Field(None, description="Индекс Хирша по всем публикациям на elibrary.ru")
    hindex_rinc: int | None = Field(None, description="Индекс Хирша по публикациям в РИНЦ")
    hindex_core: int | None = Field(None, description="Индекс Хирша по ядру РИНЦ")
    citing_publications: int | None = Field(None, description="Число публикаций, процитировавших работы автора")
    top_publication_citations: int | None = Field(None, description="Число ссылок на самую цитируемую публикацию")
    cited_publications: int | None = Field(
        None, description="Число публикаций автора, процитированных хотя бы один раз"
    )
    citations_per_publication: float | None = Field(
        None, description="Среднее число цитирований в расчете на одну публикацию"
    )
    hindex_without_self_citations: int | None = Field(None, description="Индекс Хирша без учета самоцитирований")
    hindex_core_without_self_citations: int | None = Field(
        None, description="Индекс Хирша по ядру РИНЦ без учета самоцитирований"
    )
    hindex_journal_articles: int | None = Field(None, description="Индекс Хирша с учетом только статей в журналах")
    first_publication_year: int | None = Field(None, description="Год первой публикации")
    self_citations: int | None = Field(None, description="Число самоцитирований")
    coauthor_citations: int | None = Field(None, description="Число цитирований соавторами")
    coauthors: int | None = Field(None, description="Число соавторов")
    articles_foreign_journals: int | None = Field(None, description="Число статей в зарубежных журналах")
    articles_russian_journals: int | None = Field(None, description="Число статей в российских журналах")
    articles_vak_journals: int | None = Field(None, description="Число статей в российских журналах из перечня ВАК")
    articles_translated_journals: int | None = Field(None, description="Число статей в российских переводных журналах")
    articles_impact_factor_journals: int | None = Field(
        None, description="Число статей в журналах с ненулевым импакт-фактором"
    )
    citations_from_foreign_journals: int | None = Field(None, description="Число цитирований из зарубежных журналов")
    citations_from_russian_journals: int | None = Field(None, description="Число цитирований из российских журналов")
    citations_from_vak_journals: int | None = Field(
        None, description="Число цитирований из российских журналов из перечня ВАК"
    )
    citations_from_translated_journals: int | None = Field(
        None, description="Число цитирований из российских переводных журналов"
    )
    citations_from_impact_factor_journals: int | None = Field(
        None, description="Число цитирований из журналов с ненулевым импакт-фактором"
    )
    weighted_impact_factor_published: float | None = Field(
        None, description="Средневзвешенный импакт-фактор журналов, в которых были опубликованы статьи"
    )
    weighted_impact_factor_cited: float | None = Field(
        None, description="Средневзвешенный импакт-фактор журналов, в которых были процитированы статьи"
    )
    five_year_period: str | None = Field(None, description="The 'last 5 years' window, e.g. '2021-2025'")
    publications_rinc_5y: int | None = Field(None, description="Число публикаций в РИНЦ за последние 5 лет")
    publications_core_5y: int | None = Field(None, description="Число публикаций в ядре РИНЦ за последние 5 лет")
    citations_rinc_5y: int | None = Field(
        None, description="Число ссылок из РИНЦ на работы, опубликованные за последние 5 лет"
    )
    citations_core_5y: int | None = Field(
        None, description="Число ссылок из ядра РИНЦ на работы, опубликованные за последние 5 лет"
    )
    citations_all_5y: int | None = Field(
        None, description="Число ссылок на работы автора из всех публикаций за последние 5 лет"
    )
    main_rubric_grnti: str | None = Field(None, description="Основная рубрика (ГРНТИ), e.g. '290000. Физика'")
    main_rubric_oecd: str | None = Field(None, description="Основная рубрика (OECD)")
    core_percentile: int | None = Field(None, description="Процентиль по ядру РИНЦ (1 = top percent)")
    roles: dict[str, int] = Field(
        default_factory=dict, description="'Участие в публикациях': role (автор, редактор, ...) -> count"
    )

    metrics: list[AuthorMetric] = Field(
        default_factory=list, description="Every row of the 'ОБЩИЕ ПОКАЗАТЕЛИ' table in page order"
    )
    years: list[int] = Field(default_factory=list, description="Columns of the 'ПОКАЗАТЕЛИ ПО ГОДАМ' table")
    yearly: list[AuthorYearlyMetric] = Field(
        default_factory=list, description="Every row of the 'ПОКАЗАТЕЛИ ПО ГОДАМ' table"
    )
    reports: list[AuthorReport] = Field(default_factory=list, description="Statistical report links")
    metrics_updated: date | None = Field(None, description="'Дата обновления показателей автора'")
    extra: dict[str, str] = Field(
        default_factory=dict, description="Indicators and header lines without a typed field (label -> value)"
    )
    url: str = ""
    publications_url: str = ""
    citing_publications_url: str = Field("", description="cit_author_items.asp: publications citing the author")

    def model_post_init(self, __context: object) -> None:
        if not self.url:
            self.url = f"{BASE_URL}/author_profile.asp?id={self.id}"
        if not self.publications_url:
            self.publications_url = f"{BASE_URL}/author_items.asp?authorid={self.id}&show_refs=1"
        if not self.citing_publications_url:
            self.citing_publications_url = f"{BASE_URL}/cit_author_items.asp?id={self.id}"


class AuthorReferenceHit(ElibModel):
    """A row of an author's publication list that is known only from reference lists
    ('Публикация из списков цитируемой литературы', ``tr#brw<ID>``): it has no item.asp page."""

    ref_id: int = Field(description="Id of the reference record (not an item id)")
    title: str
    authors: list[str] = Field(default_factory=list)
    source: str | None = None
    year: int | None = None
    citations: int | None = None
    position: int | None = Field(None, description="Row number on the page")


class AuthorPublicationsPage(SearchPage[PublicationHit]):
    """One page of ``author_items.asp``: publication rows plus author info and totals."""

    author_id: int | None = None
    author_name: str | None = None
    author_short_name: str | None = Field(None, description="'authorhash' of the page, e.g. 'Петров В А'")
    affiliation: AuthorAffiliation | None = None
    total_citations: int | None = Field(None, description="Total citations of all found publications")
    references: list[AuthorReferenceHit] = Field(
        default_factory=list, description="Rows known only from reference lists (no item.asp page)"
    )
