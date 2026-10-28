"""Models of the publications area: the search query, the publication card (``item.asp``), the
full-text flag fragment (``ispdf.asp``) and reference-only rows of publication lists.

Publications are journal articles, conference papers, books and chapters, dissertations, patents
(and software/database registration certificates), reports, deposited manuscripts, grants and
datasets: everything elibrary.ru shows on ``item.asp``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from elibrary_cli.models.base import BASE_URL, ElibModel, PublicationHit

# --------------------------------------------------------------------------- search query

WhereField = Literal["name", "abstract", "keywords", "affiliation", "fulltext"]
PubType = Literal["article", "disser", "book", "report", "conf", "patent", "preprint", "grant", "dataset"]
Period = Literal["all", "m1", "m3", "m6", "m12"]
SortField = Literal["rank", "date", "insdate", "name", "title", "cited"]
SortOrder = Literal["desc", "asc"]

#: All "where to search" checkboxes of querybox.asp.
ALL_WHERE: tuple[str, ...] = ("name", "abstract", "keywords", "affiliation", "fulltext")
#: Checkboxes that are on in a fresh search form (the site's ``clear_query()``).
DEFAULT_WHERE: tuple[str, ...] = ("name", "abstract", "keywords")
#: All publication type checkboxes of querybox.asp (all are on in a fresh form).
ALL_TYPES: tuple[str, ...] = ("article", "disser", "book", "report", "conf", "patent", "preprint", "grant", "dataset")

_WHERE_ALIASES = {
    "title": "name", "titles": "name", "название": "name",
    "annotation": "abstract", "abstracts": "abstract", "аннотация": "abstract",
    "keyword": "keywords", "kw": "keywords", "ключевые слова": "keywords",
    "affiliations": "affiliation", "org": "affiliation", "orgs": "affiliation", "organization": "affiliation",
    "организации": "affiliation",
    "full_text": "fulltext", "full-text": "fulltext", "text": "fulltext", "полный текст": "fulltext",
}
_TYPE_ALIASES = {
    "articles": "article", "journal": "article", "journal_article": "article", "статьи": "article",
    "dissertation": "disser", "dissertations": "disser", "thesis": "disser", "диссертации": "disser",
    "books": "book", "chapter": "book", "chapters": "book", "книги": "book",
    "reports": "report", "отчеты": "report",
    "conference": "conf", "conferences": "conf", "proceedings": "conf", "конференции": "conf",
    "patents": "patent", "патенты": "patent",
    "preprints": "preprint", "manuscript": "preprint", "manuscripts": "preprint",
    "grants": "grant", "гранты": "grant",
    "datasets": "dataset", "data": "dataset",
}


def _as_list(value: Any) -> Any:
    """Accept ``"a,b"`` / ``"a b"`` / a single value / a list for list fields."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in re.split(r"[,;\s]+", value) if v]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _dedupe(values: list) -> list:
    seen: set = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


class PublicationQuery(ElibModel):
    """Parameters of the extended publication search (querybox.asp -> query_results.asp)."""

    text: str | None = Field(None, description="Search string (ftext), at least 2 characters")
    where: list[WhereField] = Field(
        default_factory=lambda: list(DEFAULT_WHERE),
        description="Where to search: name (title), abstract, keywords, affiliation (authors' organizations), "
        "fulltext. Selecting fulltext also selects all the others, like the site does",
    )
    types: list[PubType] = Field(
        default_factory=lambda: list(ALL_TYPES),
        description="Publication types: article (journal articles), disser (dissertations), book, report, "
        "conf (conference materials), patent, preprint (deposited manuscripts), grant, dataset",
    )
    year_from: int | None = Field(None, ge=1800, le=2100, description="First publication year (inclusive)")
    year_to: int | None = Field(None, ge=1800, le=2100, description="Last publication year (inclusive)")
    period: Period = Field("all", description="Added to eLIBRARY within: all, m1, m3, m6, m12 months")
    sort: SortField = Field(
        "rank",
        description="Sort by: rank (relevance), date (issue date), insdate (date added), name (title), "
        "title (journal title), cited (citations)",
    )
    order: SortOrder = "desc"
    morphology: bool = Field(False, description="Search with Russian morphology (search_morph)")
    fuzzy: bool = Field(True, description="'Search similar text' (search_freetext); on by default on the site")
    fulltext_only: bool = Field(False, description="Only publications with full text on eLIBRARY.RU")
    open_only: bool = Field(False, description="Only publications available to you (search_open)")
    author_names: list[str] = Field(default_factory=list, description="Author filters as 'Фамилия И О'")
    journal_ids: list[int] = Field(default_factory=list, description="Journal filters (titleid)")
    rubric_codes: list[str] = Field(default_factory=list, description="GRNTI rubric codes, e.g. '500000'")
    in_previous_results: bool = Field(False, description="Search within the previous query's results")
    itembox_id: int | None = Field(None, description="Search within a personal collection (needs login)")
    page: int = Field(1, ge=1, description="Result page (100 hits per page)")

    @field_validator("text", mode="before")
    @classmethod
    def _strip_text(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = re.sub(r"\s+", " ", v).strip()
            return v or None
        return v

    @field_validator("where", mode="before")
    @classmethod
    def _norm_where(cls, v: Any) -> Any:
        items = [_WHERE_ALIASES.get(str(x).strip().lower(), str(x).strip().lower()) for x in _as_list(v)]
        return _dedupe(items)

    @field_validator("types", mode="before")
    @classmethod
    def _norm_types(cls, v: Any) -> Any:
        items = _as_list(v)
        if any(str(x).strip().lower() == "all" for x in items):
            return list(ALL_TYPES)
        return _dedupe([_TYPE_ALIASES.get(str(x).strip().lower(), str(x).strip().lower()) for x in items])

    @field_validator("author_names", "rubric_codes", mode="before")
    @classmethod
    def _norm_str_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [x for x in re.split(r"[|;]", v)]
        items = [re.sub(r"\s+", " ", str(x).replace("|", " ")).strip() for x in (v or [])]
        return _dedupe([x for x in items if x])

    @field_validator("journal_ids", mode="before")
    @classmethod
    def _norm_ids(cls, v: Any) -> Any:
        return _dedupe(_as_list(v))

    @field_validator("order", mode="before")
    @classmethod
    def _norm_order(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"rev": "desc", "nrm": "asc", "descending": "desc", "ascending": "asc"}.get(v.lower(), v.lower())
        return v

    @model_validator(mode="after")
    def _check(self) -> PublicationQuery:
        has_filters = bool(self.author_names or self.journal_ids or self.rubric_codes
                           or self.in_previous_results or self.itembox_id)
        if not self.text and not has_filters:
            raise ValueError("Не заданы условия поиска: укажите текст запроса, авторов, журналы или рубрики")
        if self.text is not None and len(self.text) < 2:
            raise ValueError("Введите не менее двух символов в поисковой строке")
        if self.text and not self.where:
            raise ValueError("Укажите, по каким полям необходимо провести поиск (where)")
        if not self.types:
            raise ValueError("Укажите, по каким типам публикаций необходимо провести поиск (types)")
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError(f"Начальный год {self.year_from} больше конечного {self.year_to}")
        return self


# --------------------------------------------------------------------------- card parts


class OrgRef(ElibModel):
    """An organization mentioned on a card: publisher, patent holder, place of defence."""

    name: str = Field(description="Name as displayed on the card")
    full_name: str | None = Field(None, description="Full name from the SCIENCE INDEX tooltip")
    org_id: int | None = Field(None, description="eLIBRARY organization id (org_profile.asp?id=)")
    city: str | None = Field(None, description="City from the tooltip, e.g. 'Москва'")
    publisher_id: int | None = Field(None, description="Publisher id (publisher_about.asp?pubsid=)")
    url: str | None = None


class Affiliation(ElibModel):
    """A numbered affiliation under the author list."""

    number: int | None = Field(None, description="Superscript number used by the authors")
    name: str = Field(description="Organization name as written in the publication")
    full_name: str | None = Field(None, description="Normalised organization name from the tooltip")
    org_id: int | None = Field(None, description="eLIBRARY organization id, when linked")
    city: str | None = Field(None, description="City of the linked organization (tooltip)")
    location: str | None = Field(None, description="Text after the name: department, city, country")
    url: str | None = None


class AuthorRef(ElibModel):
    """An author (or editor/reviewer) of the publication."""

    name: str = Field(description="Name as displayed, e.g. 'БЛОХИНА Т.М.'")
    full_name: str | None = Field(None, description="Full name from the tooltip, e.g. 'Блохина Таисия Михайловна'")
    author_id: int | None = Field(None, description="eLIBRARY AuthorID (author_profile.asp?authorid=)")
    affiliations: list[int] = Field(default_factory=list, description="Superscript affiliation numbers")
    affiliation_names: list[str] = Field(default_factory=list, description="Affiliations resolved by number")
    organization: str | None = Field(None, description="Current organization from the author's profile tooltip")
    about: str | None = Field(
        None, description="'Об авторе' info icon of the card: position / degree, e.g. 'к.х.н.', 'с.н.с.'"
    )
    corresponding: bool = Field(False, description="Marked with * as the corresponding author")
    role: str | None = Field(None, description="None for authors; label for others, e.g. 'Научное редактирование'")
    url: str | None = None


class JournalIndexing(ElibModel):
    """Journal indexing block of an article card (raw text + parsed values)."""

    wos: str | None = Field(None, description="Web of Science, raw text ('да', 'нет', 'да (Q2)')")
    in_wos: bool | None = None
    scopus: str | None = Field(None, description="Scopus, raw text")
    in_scopus: bool | None = None
    white_list: str | None = Field(None, description="Белый список, raw text, e.g. 'да (уровень 3)'")
    in_white_list: bool | None = None
    white_list_level: int | None = None
    egpni: str | None = Field(None, description="ЕГПНИ (unified state list), raw text")
    in_egpni: bool | None = None
    egpni_level: int | None = None
    vak: str | None = Field(None, description="Перечень ВАК, raw text")
    in_vak: bool | None = None
    vak_category: str | None = Field(None, description="ВАК category К1/К2/К3 when shown")
    rsci: str | None = Field(None, description="RSCI, raw text")
    in_rsci: bool | None = None
    science_index_rating: str | None = Field(None, description="Рейтинг Science Index, raw text")
    si_percentile: int | None = Field(None, description="Percentile in the Science Index journal rating")
    extra: dict[str, str] = Field(default_factory=dict)


class JournalRef(ElibModel):
    """The journal block (ЖУРНАЛ) of an article card."""

    title_id: int | None = Field(None, description="eLIBRARY journal id (title_about_new.asp?id=)")
    name: str | None = Field(None, description="Journal name as displayed (usually in capitals)")
    name_cased: str | None = Field(None, description="Journal name in normal case (cover tooltip)")
    issn: str | None = None
    eissn: str | None = None
    founders: str | None = Field(None, description="Учредители")
    publisher: str | None = Field(None, description="Издательство, when shown")
    indexing: JournalIndexing | None = None
    contents_url: str | None = Field(None, description="Link to the issue contents (contents.asp?id=)")
    url: str | None = Field(None, description="Journal card URL, when the titleid is known")
    extra: dict[str, str] = Field(default_factory=dict)


class ContainerRef(ElibModel):
    """The parent item (ИСТОЧНИК) of a chapter or a conference paper."""

    id: int | None = Field(None, description="eLIBRARY id of the book / proceedings")
    title: str | None = None
    authors: list[str] = Field(default_factory=list, description="Authors/editors of the parent as shown")
    description: str | None = Field(None, description="Rest of the bibliographic description, e.g. 'Монография. Уфа, 2024'")
    year: int | None = None
    publisher: OrgRef | None = None
    url: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)


class ConferenceInfo(ElibModel):
    """The КОНФЕРЕНЦИЯ block of a conference paper."""

    name: str | None = None
    place: str | None = Field(None, description="City/place, e.g. 'Ижевск'")
    dates: str | None = Field(None, description="Dates as shown, e.g. '15–16 июня 2021 года'")
    year: int | None = None
    raw: str | None = None


class Rubrics(ElibModel):
    """Subject classification (ТЕМАТИЧЕСКИЕ НАПРАВЛЕНИЯ). Each value is a rubric path 'A / B / C'."""

    oecd: list[str] = Field(default_factory=list)
    asjc: list[str] = Field(default_factory=list)
    grnti: list[str] = Field(default_factory=list)
    vak: list[str] = Field(default_factory=list, description="Специальность ВАК")
    sntr: list[str] = Field(default_factory=list, description="Приоритет СНТР РФ (national S&T strategy priority)")
    extra: dict[str, str] = Field(default_factory=dict)


class Metrics(ElibModel):
    """Bibliometric indicators and altmetrics of a publication card."""

    in_rinc: bool | None = Field(None, description="Входит в РИНЦ (counted in RSCI)")
    rinc_citations: int | None = Field(None, description="Цитирований в РИНЦ")
    in_core: bool | None = Field(None, description="Входит в ядро РИНЦ")
    core_citations: int | None = Field(None, description="Цитирований из ядра РИНЦ")
    reviews: str | None = Field(None, description="Рецензии, raw text ('есть', 'нет данных')")
    has_reviews: bool | None = None
    journal_si_percentile: int | None = Field(None, description="Процентиль журнала в рейтинге SI")
    views: int | None = Field(None, description="Page views")
    views_registered: int | None = Field(None, description="Views by registered users (number in brackets)")
    downloads: int | None = Field(None, description="Full-text downloads")
    downloads_authors: int | None = Field(None, description="Unique authors who downloaded (number in brackets)")
    collections: int | None = Field(None, description="Number of personal collections including the item")
    ratings_count: int | None = Field(None, description="Всего оценок")
    rating_avg: float | None = Field(None, description="Средняя оценка (1-5)")
    comments_count: int | None = Field(None, description="Всего отзывов")
    extra: dict[str, str] = Field(default_factory=dict)


class IpcCode(ElibModel):
    code: str = Field(description="IPC code, e.g. 'G06N 3/02'")
    description: str | None = Field(None, description="Hierarchy of the IPC class as shown, 'A / B'")


class PatentInfo(ElibModel):
    """Patent / registration certificate data. Inventors are the publication's authors."""

    patent_type: str | None = Field(None, description="Raw type, e.g. 'патент на изобретение'")
    number: str | None = Field(None, description="Patent number, e.g. 'RU 2760636 C2'")
    country_code: str | None = Field(None, description="Country prefix of the number, e.g. 'RU'")
    kind_code: str | None = Field(None, description="Kind code of the number, e.g. 'C2'")
    office: str | None = Field(None, description="Патентное ведомство, e.g. 'Россия'")
    application_number: str | None = None
    registration_date: date | None = Field(None, description="Дата регистрации (filing/registration)")
    publication_date: date | None = None
    publication_year: int | None = None
    priority_date: date | None = None
    holders: list[OrgRef] = Field(default_factory=list, description="Патентообладатели / правообладатели")
    ipc: list[IpcCode] = Field(default_factory=list, description="International Patent Classification codes")
    extra: dict[str, str] = Field(default_factory=dict)


class BookInfo(ElibModel):
    isbn: list[str] = Field(default_factory=list)
    publisher: OrgRef | None = None
    city: str | None = Field(None, description="Место издания")
    year: int | None = None
    pages_total: int | None = Field(None, description="Число страниц")
    series: str | None = None
    edition: str | None = None
    circulation: int | None = Field(None, description="Тираж")
    udk: str | None = None
    bbk: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)


class DissertationInfo(ElibModel):
    document: str | None = Field(None, description="Raw type: 'диссертация' or 'автореферат диссертации'")
    is_autoreferat: bool = Field(False, description="The record is the author's abstract (автореферат)")
    degree: str | None = Field(None, description="Ученая степень, e.g. 'доктор технических наук'")
    specialty: str | None = Field(None, description="Специальность as shown")
    specialty_code: str | None = Field(None, description="Specialty code, e.g. '05.13.01'")
    defense_place: OrgRef | None = Field(None, description="Место защиты")
    city: str | None = None
    pages_total: int | None = None
    defense_date: date | None = None
    extra: dict[str, str] = Field(default_factory=dict)


class Dates(ElibModel):
    received: date | None = Field(None, description="Поступила в редакцию")
    accepted: date | None = Field(None, description="Принята к публикации")
    published: date | None = Field(None, description="Дата публикации")
    deposited: date | None = Field(None, description="Дата депонирования (deposited manuscripts)")
    defended: date | None = Field(None, description="Дата защиты (dissertations)")
    placed: date | None = Field(None, description="Дата размещения: when the record appeared on eLIBRARY.RU")


class FulltextLink(ElibModel):
    """A full-text file or a related link from the ИНСТРУМЕНТЫ panel."""

    kind: Literal["file", "url"] = Field(description="file: file stored on eLIBRARY; url: link to a site")
    number: int | None = Field(None, description="File/link number (2nd argument of the JS call)")
    label: str | None = Field(None, description="Text shown, e.g. 'Полный текст (PDF)'")
    host: str | None = Field(None, description="Target site of a url link, e.g. 'dlib.rsl.ru'")
    format: str | None = Field(None, description="File format when shown, e.g. 'PDF'")
    colour: str | None = Field(None, description="Icon colour when shown (green/blue/yellow/orange/red)")
    js: str | None = Field(None, description="JavaScript call behind the link, e.g. 'file_article(47430099, 1)'")
    url: str | None = Field(None, description="Direct URL when the page shows one (signed-in sessions)")


class FulltextInfo(ElibModel):
    """What the card says about the full text."""

    available: bool = Field(False, description="A full-text file is stored on eLIBRARY.RU (download needs login)")
    access: Literal["open", "external", "order", "closed", "paid"] | None = Field(
        None, description="open: free on eLIBRARY; external: on another site; order: via the order system; "
        "closed: closed access; paid: can be bought on eLIBRARY")
    colour: str | None = Field(None, description="Colour of the full-text icon when visible")
    price_rub: int | None = Field(None, description="Price in roubles when the item can be bought")
    files: list[FulltextLink] = Field(default_factory=list)
    links: list[FulltextLink] = Field(default_factory=list, description="Links to other sites / related documents")
    can_add_link: bool = Field(False, description="The card offers 'Добавить ссылку на полный текст' (none known)")
    notes: list[str] = Field(default_factory=list, description="Raw lines of the panel about the full text")


class FulltextFlag(ElibModel):
    """The ``ispdf.asp?itemid=`` fragment (full-text icon of a result row)."""

    item_id: int | None = None
    available: bool = Field(False, description="An icon is shown (some form of full text exists)")
    colour: str | None = Field(None, description="green / blue / yellow / orange / red")
    access: Literal["open", "external", "order", "closed", "paid"] | None = None
    title: str | None = Field(None, description="Icon tooltip, e.g. 'Доступ к полному тексту открыт'")
    action: str | None = Field(None, description="JS function behind the icon: load_article, url_article, ...")
    price_rub: int | None = Field(None, description="Price from the tooltip ('Можно приобрести ... за 250 руб.')")


class RelatedItem(ElibModel):
    """Another version of the publication (e.g. the translated version)."""

    relation: str = Field(description="translation, original or the section name in lower case")
    id: int | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    source: str | None = None
    url: str | None = None


class ReferenceHit(ElibModel):
    """A row of an author/organization publication list that is only known from cited-reference
    lists (``tr#brw<ID>``, shown with ``show_refs=1``): it has no eLIBRARY card."""

    ref_id: int = Field(description="Reference record id (not an item id)")
    title: str
    authors: list[str] = Field(default_factory=list)
    source: str | None = None
    year: int | None = None
    citations: int | None = None
    position: int | None = Field(None, description="Row number on the page")


# --------------------------------------------------------------------------- the card


class Publication(ElibModel):
    """Full publication card (item.asp?id=...)."""

    id: int = Field(description="eLIBRARY item id")
    edn: str | None = Field(None, description="EDN identifier, e.g. 'FYOAKL'")
    doi: str | None = None
    url: str = ""
    title: str = Field(description="Title (normal case when the page provides it)")
    title_display: str | None = Field(None, description="Title as displayed on the card (usually capitals)")
    title_info: str | None = Field(None, description="Text under the title, e.g. 'Монография'")
    title_en: str | None = None
    kind: str | None = Field(
        None,
        description="Normalised type: article, conference, proceedings, book, chapter, dissertation, patent, "
        "report, preprint, grant, dataset, other",
    )
    type_raw: str | None = Field(None, description="'Тип:' as shown, e.g. 'статья в журнале - научная статья'")
    language: str | None = Field(None, description="Language as shown, e.g. 'русский'")
    language_code: str | None = Field(None, description="ISO 639-1 code when known, e.g. 'ru'")
    authors: list[AuthorRef] = Field(default_factory=list)
    contributors: list[AuthorRef] = Field(default_factory=list, description="Editors, reviewers, supervisors...")
    affiliations: list[Affiliation] = Field(default_factory=list)
    authors_en: list[str] = Field(default_factory=list, description="Author names from the English description")
    affiliations_en: list[str] = Field(default_factory=list)
    year: int | None = None
    volume: str | None = None
    issue: str | None = Field(None, description="Issue number ('Выпуск'/'Номер')")
    issue_id: int | None = Field(None, description="eLIBRARY issue id (contents.asp?id=)")
    pages: str | None = None
    journal: JournalRef | None = None
    container: ContainerRef | None = None
    conference: ConferenceInfo | None = None
    abstract: str | None = None
    abstract_en: str | None = None
    keywords: list[str] = Field(default_factory=list)
    keywords_en: list[str] = Field(default_factory=list)
    rubrics: Rubrics = Field(default_factory=Rubrics)
    metrics: Metrics = Field(default_factory=Metrics)
    patent: PatentInfo | None = None
    book: BookInfo | None = None
    dissertation: DissertationInfo | None = None
    dates: Dates = Field(default_factory=Dates)
    udk: str | None = None
    funding: str | None = Field(None, description="ИНФОРМАЦИЯ О ФИНАНСОВОЙ ПОДДЕРЖКЕ")
    related: list[RelatedItem] = Field(default_factory=list)
    contents: list[PublicationHit] = Field(default_factory=list, description="Table of contents of a book / proceedings")
    references: list[str] = Field(default_factory=list, description="Cited references when the card lists them")
    fulltext: FulltextInfo = Field(default_factory=FulltextInfo)
    cover_url: str | None = None
    extra: dict[str, str] = Field(default_factory=dict, description="Every other label/value seen on the card")

    def model_post_init(self, __context: Any) -> None:
        if not self.url:
            self.url = f"{BASE_URL}/item.asp?id={self.id}"
