"""Organization models: search query, search hits and the organization profile.

Pages (2026-10):

* ``orgs.asp`` -- organization search (POST form ``results``), 20 rows per page.
* ``org_about.asp?orgsid=ID`` -- "КАРТОЧКА ОРГАНИЗАЦИИ": names, address, ministry, type, contacts.
* ``org_profile.asp?id=ID`` -- "АНАЛИЗ ПУБЛИКАЦИОННОЙ АКТИВНОСТИ ОРГАНИЗАЦИИ": bibliometric indicators.
* ``org_items.asp?orgsid=ID`` -- publications of the organization (``PublicationHit`` rows).

``OrgProfile`` combines the card and the analysis page: :func:`parsers.organizations.parse_org_profile`
fills the indicators, :func:`parsers.organizations.parse_org_about` fills the card fields, and
:meth:`OrgProfile.merge` joins both.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from elibrary_cli.models.base import BASE_URL, ElibModel, PublicationHit, SearchPage

OrgSort = Literal["name", "town", "publications", "citations"]
SortOrder = Literal["asc", "desc"]


def org_about_url(org_id: int) -> str:
    """Organization card: ``org_about.asp?orgsid=ID``."""
    return f"{BASE_URL}/org_about.asp?orgsid={org_id}"


def org_profile_url(org_id: int) -> str:
    """Publication-activity analysis: ``org_profile.asp?id=ID``."""
    return f"{BASE_URL}/org_profile.asp?id={org_id}"


def org_items_url(org_id: int) -> str:
    """Publications of the organization: ``org_items.asp?orgsid=ID``."""
    return f"{BASE_URL}/org_items.asp?orgsid={org_id}"


def org_citing_items_url(org_id: int) -> str:
    """Publications citing the organization: ``cit_org_items.asp?id=ID``."""
    return f"{BASE_URL}/cit_org_items.asp?id={org_id}"


class OrgQuery(ElibModel):
    """Organization search (``orgs.asp``). All criteria are optional and combined with AND."""

    name: str | None = Field(None, description="Words of the organization name (form field 'orgname')")
    town: str | None = Field(
        None, description="Town exactly as elibrary.ru spells it, e.g. 'Москва', 'Новосибирск' (field 'town')"
    )
    region_id: int | None = Field(
        None, ge=0, description="elibrary region id (field 'regionid'), e.g. 73 = Москва, 51 = Новосибирская область"
    )
    region: str | None = Field(
        None, description="Region name; resolved to region_id with data/orgs_options.json by the query builder"
    )
    country: str | None = Field(
        None, description="ISO 3166-1 alpha-3 code such as 'RUS', or a Russian country name ('Россия')"
    )
    sort: OrgSort = Field(
        "name", description="Sort key: name, town, publications (число статей), citations (число цитирований)"
    )
    order: SortOrder | None = Field(
        None, description="asc/desc; default as in the browser: asc for name/town, desc for publications/citations"
    )
    page: int = Field(1, ge=1, description="Result page (20 organizations per page)")

    @field_validator("name", "town", "region", "country", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = " ".join(v.split())
            return v or None
        return v

    @field_validator("country")
    @classmethod
    def _country_code(cls, v: str | None) -> str | None:
        if v and len(v) == 3 and v.isascii() and v.isalpha():
            return v.upper()
        return v

    @field_validator("region_id")
    @classmethod
    def _region_zero(cls, v: int | None) -> int | None:
        return v or None

    @property
    def effective_order(self) -> SortOrder:
        """The order the browser would send for this sort key when ``order`` is not given."""
        if self.order:
            return self.order
        return "desc" if self.sort in ("publications", "citations") else "asc"


class OrgHit(ElibModel):
    """One row of the organization search results."""

    id: int = Field(description="eLIBRARY organization id (orgsid)")
    name: str
    town: str | None = None
    region: str | None = Field(None, description="Region, when the list shows it")
    country: str | None = Field(None, description="Country, when the list shows it")
    publications: int | None = Field(None, description="Publications of the organization in RSCI (РИНЦ), column 'Публ.'")
    citations: int | None = Field(None, description="Citations of the organization's publications, column 'Цит.'")
    position: int | None = Field(None, description="Row number in the result list")
    extra: dict[str, str] = Field(default_factory=dict, description="Other columns: header -> cell text")
    url: str = Field("", description="Organization card (org_about.asp?orgsid=ID)")
    profile_url: str = Field("", description="Publication-activity analysis (org_profile.asp?id=ID)")
    publications_url: str = Field("", description="List of publications (org_items.asp?orgsid=ID)")
    citing_url: str = Field("", description="Publications citing the organization (cit_org_items.asp?id=ID)")

    def model_post_init(self, __context: Any) -> None:
        self.url = self.url or org_about_url(self.id)
        self.profile_url = self.profile_url or org_profile_url(self.id)
        self.publications_url = self.publications_url or org_items_url(self.id)
        self.citing_url = self.citing_url or org_citing_items_url(self.id)


Number = int | float


class OrgIndicator(ElibModel):
    """A row of the 'ОБЩИЕ ПОКАЗАТЕЛИ' or 'ПОКАЗАТЕЛИ ЗА 5 ЛЕТ' tables of org_profile.asp."""

    section: str = Field(description="Table heading, e.g. 'ОБЩИЕ ПОКАЗАТЕЛИ', 'ПОКАЗАТЕЛИ ЗА 5 ЛЕТ (2021-2025)'")
    name: str = Field(description="Indicator name as shown, e.g. 'Число публикаций в РИНЦ'")
    value: Number | None = Field(None, description="Numeric value (decimal comma converted)")
    percent: float | None = Field(None, description="Share shown in parentheses, e.g. 36.7 for '(36,7%)'")
    text: str = Field("", description="Raw cell text")
    url: str | None = Field(None, description="Link to the matching publication list (org_items.asp...), if any")
    description: str | None = Field(None, description="Tooltip text explaining the indicator (only on request)")


class OrgYearSeries(ElibModel):
    """A row of the 'ПОКАЗАТЕЛИ ПО ГОДАМ' / 'КБПР ПО ГОДАМ' tables: one indicator over years."""

    name: str
    values: dict[int, Number | None] = Field(default_factory=dict, description="Year -> value")
    description: str | None = None


class OrgFieldRow(ElibModel):
    """A row of the tables 'ПУБЛИКАЦИИ ЗА <год> ГОД ПО ОБЛАСТЯМ ЗНАНИЙ' and
    'ЦИТИРОВАНИЯ ПУБЛИКАЦИЙ ЗА 5 ЛЕТ ПО ОБЛАСТЯМ ЗНАНИЙ'."""

    name: str = Field(description="Field of science, or a total row ('Всего статей организации за год')")
    subfield: bool = Field(False, description="True for 'в т.ч. ...' rows (part of the row above)")
    values: dict[str, int | None] = Field(
        default_factory=dict,
        description="Column -> number of articles/citations; columns: ЯДРО РИНЦ, WOS, SCOPUS, RSCI, ВАК, РИНЦ, БС ЕГПНИ",
    )


class OrgRosstatRow(ElibModel):
    """A row of 'ОТЧЕТ ДЛЯ РОССТАТА' (form 2-наука, section on publications)."""

    name: str
    line: str | None = Field(None, description="Line number of the statistical form ('№ строки')")
    publications: int | None = Field(None, description="'Публ.' column")
    staff: int | None = Field(None, description="'Сотр.': researchers who had these results")
    staff_under_39: int | None = Field(None, description="'До 39 лет': of them younger than 39")
    subrow: bool = Field(False, description="True for 'из них ...' rows")
    url: str | None = None


class OrgJournal(ElibModel):
    """A journal from 'ИЗДАТЕЛЬСКАЯ ДЕЯТЕЛЬНОСТЬ' and the organization's roles for it."""

    title_id: int | None = Field(None, description="Journal id (title_about_new.asp?id=...)")
    title: str
    roles: list[str] = Field(
        default_factory=list, description="учредитель, редакция, издательство, место расположения редакции, ..."
    )
    url: str | None = None


class OrgDissertationCouncil(ElibModel):
    """A row of 'ДИССЕРТАЦИОННЫЕ СОВЕТЫ'."""

    code: str = Field(description="Council code ('Шифр совета'), e.g. 'МГУ.011.2'")
    specialties: list[str] = Field(default_factory=list, description="Specialties, e.g. '1.3.3 Теоретическая физика ...'")
    council_type: str | None = Field(None, description="самостоятельный / объединенный / ...")


class OrgTable(ElibModel):
    """A table of org_profile.asp whose heading is not recognised (kept verbatim)."""

    title: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class OrgProfile(ElibModel):
    """Organization: card (org_about.asp) + publication-activity analysis (org_profile.asp)."""

    id: int = Field(description="eLIBRARY organization id (orgsid)")
    name: str = Field(description="Name as used by elibrary.ru")
    # --- card (org_about.asp) ---
    full_name: str | None = Field(None, description="Full official name ('Полное название')")
    name_en: str | None = Field(None, description="English name")
    abbreviation: str | None = Field(None, description="Short name ('Сокращение'), e.g. 'МГУ'")
    abbreviation_en: str | None = None
    org_type: str | None = Field(None, description="Type, e.g. 'Высшее учебное заведение'")
    department: str | None = Field(None, description="Ministry / agency ('Ведомство')")
    department_id: int | None = Field(None, description="elibrary ministry id (orgs.asp?vedomid=...)")
    parent_id: int | None = Field(None, description="Parent organization id, when the card links one")
    parent_name: str | None = None
    address: str | None = Field(None, description="Postal address")
    address_en: str | None = None
    legal_address: str | None = None
    town: str | None = None
    town_en: str | None = None
    region: str | None = None
    region_id: int | None = Field(None, description="elibrary region id (orgs.asp?regionid=...)")
    country: str | None = None
    country_code: str | None = Field(None, description="ISO3 country code (orgs.asp?countryid=...)")
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    fax: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict, description="ROR, ИНН, ОГРН, ... when shown")
    # --- main indicators (ОБЩИЕ ПОКАЗАТЕЛИ) ---
    publications: int | None = Field(None, description="Число публикаций на elibrary.ru")
    publications_rinc: int | None = Field(None, description="Число публикаций в РИНЦ")
    publications_core: int | None = Field(None, description="Число публикаций, входящих в ядро РИНЦ")
    citations: int | None = Field(None, description="Число цитирований публикаций на elibrary.ru")
    citations_rinc: int | None = Field(None, description="Число цитирований публикаций в РИНЦ")
    citations_from_core: int | None = Field(None, description="Число цитирований из публикаций, входящих в ядро РИНЦ")
    h_index: int | None = Field(None, description="Индекс Хирша по всем публикациям на elibrary.ru")
    h_index_rinc: int | None = Field(None, description="Индекс Хирша по публикациям в РИНЦ")
    h_index_core: int | None = Field(None, description="Индекс Хирша по ядру РИНЦ")
    g_index: int | None = None
    i_index: int | None = None
    authors_count: int | None = Field(None, description="Число авторов")
    authors_registered: int | None = Field(None, description="Число авторов, зарегистрированных в Science Index")
    # --- 5-year indicators (ПОКАЗАТЕЛИ ЗА 5 ЛЕТ) ---
    period_5y: str | None = Field(None, description="Years of the 5-year table, e.g. '2021-2025'")
    publications_5y: int | None = Field(None, description="Публикаций на elibrary.ru за 5 лет")
    publications_rinc_5y: int | None = Field(None, description="Публикаций в РИНЦ за 5 лет")
    publications_core_5y: int | None = Field(None, description="Публикаций в ядре РИНЦ за 5 лет")
    citations_rinc_5y: int | None = Field(None, description="Число цитирований в РИНЦ (публикаций за 5 лет)")
    impact_factor_5y: float | None = Field(
        None, description="Средневзвешенный импакт-фактор журналов, в которых были опубликованы статьи (5 лет)"
    )
    # --- every row ---
    metrics: dict[str, Number | None] = Field(
        default_factory=dict, description="Every row of 'ОБЩИЕ ПОКАЗАТЕЛИ': indicator name -> value"
    )
    metrics_5y: dict[str, Number | None] = Field(
        default_factory=dict, description="Every row of 'ПОКАЗАТЕЛИ ЗА 5 ЛЕТ': indicator name -> value"
    )
    indicators: list[OrgIndicator] = Field(
        default_factory=list, description="Rows of both indicator tables with percentages and list links"
    )
    by_year: list[OrgYearSeries] = Field(default_factory=list, description="'ПОКАЗАТЕЛИ ПО ГОДАМ'")
    kbpr_by_year: list[OrgYearSeries] = Field(
        default_factory=list, description="'КБПР ПО ГОДАМ': комплексный балл публикационной результативности"
    )
    fields_year: int | None = Field(None, description="Year of the 'ПУБЛИКАЦИИ ЗА <год> ГОД ПО ОБЛАСТЯМ ЗНАНИЙ' table")
    publications_by_field: list[OrgFieldRow] = Field(default_factory=list)
    citations_by_field: list[OrgFieldRow] = Field(
        default_factory=list, description="Citations of the publications of the last 5 years, by field"
    )
    rosstat_year: int | None = None
    rosstat: list[OrgRosstatRow] = Field(default_factory=list, description="'ОТЧЕТ ДЛЯ РОССТАТА'")
    journals: list[OrgJournal] = Field(default_factory=list, description="'ИЗДАТЕЛЬСКАЯ ДЕЯТЕЛЬНОСТЬ'")
    dissertation_councils: list[OrgDissertationCouncil] = Field(default_factory=list)
    other_tables: list[OrgTable] = Field(default_factory=list, description="Tables with unrecognised headings")
    updated: _dt.date | None = Field(None, description="'Дата обновления показателей организации'")
    notes: list[str] = Field(default_factory=list, description="Calculation remarks shown next to the indicators")
    extra: dict[str, str] = Field(default_factory=dict, description="Other label/value pairs of the card")
    url: str = Field("", description="org_profile.asp?id=ID")
    about_url: str = Field("", description="org_about.asp?orgsid=ID")
    publications_url: str = Field("", description="org_items.asp?orgsid=ID")
    citing_url: str = Field("", description="cit_org_items.asp?id=ID")

    @model_validator(mode="after")
    def _urls(self) -> OrgProfile:
        self.url = self.url or org_profile_url(self.id)
        self.about_url = self.about_url or org_about_url(self.id)
        self.publications_url = self.publications_url or org_items_url(self.id)
        self.citing_url = self.citing_url or org_citing_items_url(self.id)
        return self

    def merge(self, other: OrgProfile) -> OrgProfile:
        """Return a copy where empty fields are filled from ``other`` (e.g. the card from org_about.asp).

        Values of ``self`` win; dicts are merged (``self`` wins per key); lists of ``self`` are kept
        unless empty. ``name`` stays ``self.name``.
        """
        data = self.model_dump()
        for key, value in other.model_dump().items():
            if key in ("id",):
                continue
            mine = data.get(key)
            if isinstance(mine, dict) and isinstance(value, dict):
                data[key] = {**value, **mine}
            elif mine in (None, "", [], {}):
                data[key] = value
        return OrgProfile.model_validate(data)


class OrgPublicationsPage(SearchPage[PublicationHit]):
    """A page of org_items.asp: publication rows plus the organization header and totals."""

    org_id: int | None = Field(None, description="Organization id (orgsid)")
    org_name: str | None = None
    org_town: str | None = None
    total_citations: int | None = Field(None, description="'с общим количеством цитирований: N', when shown")
