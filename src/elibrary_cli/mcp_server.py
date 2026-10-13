"""MCP server ``elibrary-mcp`` (official MCP Python SDK 2.x, ``MCPServer``).

Thin layer over :class:`elibrary_cli.client.ElibraryClient`: every tool is a plain ``def`` (the SDK
runs it in a worker thread), takes typed arguments (``Annotated[..., Field(description=...)]``,
``Literal`` enums) and returns a pydantic model, which becomes the tool's output schema. Library
errors become ``ToolError`` with the Russian message of the error (for ``CaptchaRequired`` preceded by
an English instruction for the agent); bad arguments become ``ToolError`` too.

One client is shared by all sessions of the process. It is created lazily (thread-safe) by
:data:`client_factory` and closed when the server stops; :func:`set_client` installs an existing
client instead (tests, embedding). Nothing is printed: stdout is the stdio transport.
"""

from __future__ import annotations

import argparse
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from elibrary_cli import __version__
from elibrary_cli.auth import SessionInfo
from elibrary_cli.config import Settings
from elibrary_cli.errors import CaptchaRequired, ElibraryError
from elibrary_cli.fulltext import FullText, SourceName
from elibrary_cli.http import BAN_GATES
from elibrary_cli.models.authors import (
    AuthorHit,
    AuthorItemsSort,
    AuthorProfile,
    AuthorPublicationsPage,
    AuthorSort,
    CodeType,
    MetricsBasis,
)
from elibrary_cli.models.base import ElibModel, PublicationHit, SearchPage
from elibrary_cli.models.journals import (
    FtOpenFilter,
    IssueContents,
    JournalHit,
    JournalProfile,
    JournalSort,
    RiscFilter,
    ScopusFilter,
    TranslatedFilter,
    VakFilter,
    WhiteFilter,
    WosFilter,
)
from elibrary_cli.models.organizations import OrgHit, OrgProfile, OrgPublicationsPage, OrgSort
from elibrary_cli.models.publications import Period, Publication, PubType, SortField, SortOrder, WhereField

if TYPE_CHECKING:  # pragma: no cover
    from elibrary_cli.client import ElibraryClient
    from elibrary_cli.integrations.zotero import ZoteroWriter

log = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "INSTRUCTIONS",
    "CitationExport",
    "CookieImportResult",
    "IdentifierResolution",
    "ReferenceData",
    "ReferenceList",
    "ReferenceOption",
    "SessionStatus",
    "ZoteroAddItem",
    "ZoteroAddResult",
    "client_factory",
    "close_client",
    "configure",
    "get_client",
    "main",
    "mcp",
    "run",
    "set_client",
    "zotero_factory",
]

INSTRUCTIONS = """\
eLIBRARY.RU (Russian Scientific Electronic Library, home of the RSCI/РИНЦ index): \
Russian journal articles, conference papers, books, dissertations, patents, and author, organization and \
journal profiles with citation metrics (RSCI citations, h-index, VAK, Scopus/WoS, Science Index). \
Most content is Russian: write queries in Russian (English titles also work).

Which tool:
- publications: elibrary_search_articles / _patents / _books, or elibrary_search_publications for any \
type (dissertations, conference papers...); a known id/EDN/DOI/URL: elibrary_get_publication.
- people: elibrary_search_authors (surname, SPIN, ORCID) -> elibrary_get_author, \
elibrary_get_author_publications. Organizations, journals: elibrary_search_organizations / \
elibrary_search_journals -> elibrary_get_organization(_publications), elibrary_get_journal(_issue). \
SPIN, ORCID, ISSN, ISBN, ROR: elibrary_resolve_identifier.
- full text: elibrary_get_fulltext (paged: pass next_offset as offset). Citations: \
elibrary_export_citation (CSL JSON / BibTeX for zotero-mcp) or elibrary_zotero_add.
- filter codes (countries, regions, rubrics...): elibrary_reference_data (no network).

Cost: every uncached call costs real page views on a rate-limited site that shows a captcha after about \
10 quick views; requests are throttled and capped. Prefer narrow queries (years, types, \
journal), reuse ids from earlier results, do not page through or loop over many items: search hits \
already carry title, authors, source, year and citations.

Captcha: when a tool reports CAPTCHA, stop calling eLIBRARY tools. Ask the user to run \
`elibrary auth captcha` in a terminal, or to pass the check at the URL from the error and give you \
copy(document.cookie) from the browser console on elibrary.ru; then call elibrary_set_cookies and retry \
once. Never try to solve the captcha.
Ban: if a tool says the IP is blocked (ip_blocked, HTTP 403), stop for the day, tell the user \
(support@elibrary.ru), never try to lift the pause. elibrary_session_status: limits and pause, no request.\
"""

#: English lead of the captcha error, for the agent (the Russian help text of the error follows it).
CAPTCHA_AGENT_HINT = (
    "CAPTCHA: eLIBRARY.RU asks for a human check («Тест Тьюринга»); further requests are paused. Stop calling "
    "eLIBRARY tools and ask the user to pass the check: run `elibrary auth captcha` in a terminal, or open "
    "{url} in a browser, pass the check, run copy(document.cookie) in the browser console (F12) on elibrary.ru "
    "and give you the string; then call elibrary_set_cookies(cookies=...) and retry once. Never try to solve "
    "the captcha yourself."
)
ZOTERO_UNAVAILABLE_HINT = (
    "Zotero is not reachable, nothing was saved. Ask the user to start the Zotero desktop app (or to set "
    "ZOTERO_API_KEY and ZOTERO_LIBRARY_ID), or call elibrary_export_citation(format='csl_json') and pass its "
    "text to zotero-mcp's zotero_add_item(source=<text>, source_type='csl_json')."
)
#: Claude Code keeps tool results up to this size inline (default 50 000 characters).
RESULT_META = {"anthropic/maxResultSizeChars": 200_000}

# ============================================================================ shared client
_client_lock = threading.RLock()
_client: ElibraryClient | None = None
_client_owned = False
_settings: Settings | None = None


def _default_client_factory() -> ElibraryClient:
    from elibrary_cli.client import ElibraryClient

    return ElibraryClient(_settings)


def _default_zotero_factory() -> ZoteroWriter:
    from elibrary_cli.integrations.zotero import ZoteroWriter

    return ZoteroWriter(settings=get_client().settings)


#: Creates the shared client on first use (``ElibraryClient(settings)``). Replaced in tests.
client_factory: Callable[[], ElibraryClient] = _default_client_factory
#: Creates the Zotero writer used to check that Zotero is reachable. Replaced in tests.
zotero_factory: Callable[[], ZoteroWriter] = _default_zotero_factory


def configure(settings: Settings | None) -> None:
    """Settings for the client created by the default factory (``None``: ``Settings.from_env()``).
    Closes an already created own client so that the next call uses the new settings."""
    global _settings
    with _client_lock:
        _settings = settings
        close_client()


def get_client() -> ElibraryClient:
    """The shared client, created on first use."""
    global _client, _client_owned
    with _client_lock:
        if _client is None:
            _client = client_factory()
            _client_owned = True
        return _client


def set_client(client: ElibraryClient | None) -> None:
    """Install ``client`` as the shared client (not closed by the server); ``None`` resets."""
    global _client, _client_owned
    with _client_lock:
        close_client()
        _client = client
        _client_owned = False


def close_client() -> None:
    """Close the shared client if the server created it (an installed client is only forgotten)."""
    global _client, _client_owned
    with _client_lock:
        client, owned = _client, _client_owned
        _client, _client_owned = None, False
    if client is not None and owned:
        try:
            client.close()
        except Exception:  # pragma: no cover - closing must never break shutdown
            log.exception("Closing the eLIBRARY client failed")


@asynccontextmanager
async def _lifespan(server: MCPServer[Any]) -> AsyncIterator[dict[str, Any]]:
    try:
        yield {}
    finally:
        with _client_lock:
            owned = _client_owned
        if owned:
            close_client()


# ============================================================================ error mapping
def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "Неверные параметры: " + "; ".join(parts)


def captcha_message(exc: CaptchaRequired) -> str:
    """English instruction for the agent followed by the Russian help text (with the captcha URL)."""
    return CAPTCHA_AGENT_HINT.format(url=exc.captcha_url) + "\n\n" + str(exc)


def _call(action: Callable[[ElibraryClient], T]) -> T:
    """Run ``action`` with the shared client; library errors become ``ToolError``."""
    try:
        return action(get_client())
    except ToolError:
        raise
    except CaptchaRequired as exc:
        raise ToolError(captcha_message(exc)) from exc
    except ElibraryError as exc:
        raise ToolError(str(exc)) from exc
    except ValidationError as exc:
        raise ToolError(_validation_message(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ============================================================================ output models
class IdentifierResolution(ElibModel):
    """Where an identifier points on eLIBRARY.RU."""

    kind: str = Field(description="Identifier type: edn, doi, isbn, spin, orcid, issn, ror, elibrary_id, url")
    value: str = Field(description="Normalised identifier (EDN upper case, DOI lower case, ISSN NNNN-NNNC ...)")
    target: str | None = Field(None, description="publication, author, journal, organization or issue")
    id: int | None = Field(None, description="eLIBRARY id of the target (item id, AuthorID, titleid, orgsid, issue id)")
    url: str | None = Field(None, description="Canonical eLIBRARY.RU page of the target")
    edn: str | None = Field(None, description="EDN, when known")
    location: str | None = Field(None, description="Redirect target the site answered with, when one was followed")
    from_cache: bool = Field(False, description="True when the answer came from the local cache (no request)")


class CitationExport(ElibModel):
    """Bibliographic records of one or several publications."""

    format: Literal["csl_json", "bibtex", "ris", "zotero_json"]
    count: int = Field(description="Number of records")
    text: str = Field(description="The records: BibTeX / RIS text or a JSON array (CSL JSON, Zotero API JSON)")
    zotero_mcp_source_type: Literal["csl_json", "bibtex"] | None = Field(
        None, description="source_type to use with zotero-mcp's zotero_add_item(source=text, ...); null for "
        "RIS and Zotero API JSON")


class ZoteroAddItem(ElibModel):
    """Outcome of saving one publication to Zotero."""

    index: int | None = Field(None, description="Position in the request")
    title: str | None = None
    url: str | None = None
    key: str | None = Field(None, description="Zotero item key, when known")
    backend: str | None = Field(None, description="local, connector or web")
    status: str = Field(description="created, duplicate or failed")
    message: str | None = None
    attachment: str | None = Field(None, description="PDF: uploaded, exists, failed, unsupported or null")
    collection: str | None = None
    elibrary_id: int | None = None
    pdf_note: str | None = Field(None, description="Why no PDF was attached, when with_pdf was requested")


class ZoteroAddResult(ElibModel):
    items: list[ZoteroAddItem] = Field(default_factory=list)
    created: int = 0
    duplicates: int = 0
    failed: int = 0


class SessionStatus(ElibModel):
    """Local state of the eLIBRARY.RU session (no request is made). Cookie values are never shown."""

    base_url: str | None = None
    has_session_cookie: bool = Field(False, description="The SUserID session cookie is present")
    cookie_names: list[str] = Field(default_factory=list)
    account: str | None = Field(None, description="Signed-in account name, when known")
    logged_in: bool = False
    breaker_open: bool = Field(False, description="Requests are paused after a captcha / ban page")
    breaker_gate: str | None = Field(None, description="What paused the requests: captcha, ip_blocked, ...")
    breaker_until: float | None = Field(None, description="Unix time when the pause ends")
    breaker_url: str | None = Field(None, description="The captcha page to pass, while paused by a captcha")
    requests_last_hour: int | None = None
    requests_last_day: int | None = None
    max_per_hour: int | None = Field(None, description="Local request budget per hour")
    max_per_day: int | None = Field(None, description="Local request budget per day")
    requests_total: int | None = None
    min_delay_s: float | None = Field(None, description="Minimum pause between requests, seconds")
    current_query: str | None = Field(None, description="Signature of the search query the server session holds")
    offline: bool = Field(False, description="Only cached pages are used (ELIBRARY_OFFLINE)")
    cache: dict[str, Any] = Field(default_factory=dict, description="Page cache size: pages, bytes")
    extra: dict[str, Any] = Field(default_factory=dict, description="Other status values")

    @classmethod
    def from_status(cls, status: dict[str, Any]) -> SessionStatus:
        known = {k: v for k, v in status.items() if k in cls.model_fields and k != "extra"}
        rest = {k: v for k, v in status.items() if k not in cls.model_fields and k not in ("state_db", "cache_db")}
        return cls.model_validate({**known, "extra": rest})


class CookieImportResult(ElibModel):
    """Result of a cookie import (names only, never values)."""

    cookie_names: list[str] = Field(default_factory=list)
    count: int = 0
    has_session_cookie: bool = Field(False, description="SUserID present: a passed captcha / login carries over")
    has_guid_cookie: bool = False
    session_cookie_imported: bool = Field(
        False, description="The string itself contained SUserID (only then a captcha pause is cleared)")
    ignored: list[str] = Field(default_factory=list, description="Analytics cookies that were dropped")
    user_agent_saved: bool = False
    breaker_open: bool = Field(False, description="Requests are still paused after the import")
    breaker_gate: str | None = Field(None, description="What still pauses the requests: captcha, ip_blocked, ...")
    warnings: list[str] = Field(default_factory=list)
    message: str | None = None


class ReferenceOption(ElibModel):
    value: str = Field(description="Value accepted by the list's query_field and the matching search tool "
                       "parameter (same meaning in every list): the readable value where the tool has one ('k1', "
                       "'citations', 'article', 'desc'), otherwise the site code ('RUS', '73', '500000', 'SPIN'); "
                       "for checkbox lists (publications.flags, journals.checkboxes) the boolean field's name")
    label: str = Field(description="Russian label of the option")
    count: int | None = Field(None, description="Records shown next to the option on the form, when shown")
    site_code: str | None = Field(None, description="Code the site's form sends for this option, only when it "
                                  "differs from value (informational; the tools translate value themselves)")
    form_field: str | None = Field(None, description="Site form checkbox of this option (publications.types/where/"
                                   "flags, journals.checkboxes), informational")


class ReferenceList(ElibModel):
    name: str = Field(description="List name to pass as `name`, e.g. 'orgs.regionid'")
    description: str
    query_field: str = Field(description="Query model field the option values go to, e.g. 'JournalQuery.vak'; the "
                             "search tool parameter has the same name or a close one (code_type: identifier_type, "
                             "white: white_list, ftopen: open_access, rubric_code: rubric, region_id: region)")
    count: int
    aliases: list[str] = Field(default_factory=list)


class ReferenceData(ElibModel):
    """Reference lists of the search forms (no network)."""

    name: str | None = Field(None, description="Canonical list name, when a list was requested")
    lists: list[ReferenceList] = Field(default_factory=list, description="All lists (when no name was given)")
    options: list[ReferenceOption] = Field(default_factory=list)


# ============================================================================ server and tools
mcp: MCPServer[Any] = MCPServer("elibrary", instructions=INSTRUCTIONS, version=__version__, lifespan=_lifespan)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
READ_LOCAL = ToolAnnotations(read_only_hint=True, open_world_hint=False)

Page = Annotated[int, Field(ge=1, le=10000, description="Result page (1-based)")]
Order = Annotated[SortOrder, Field(description="desc (descending) or asc (ascending)")]
Text = Annotated[str | None, Field(
    description="Search string (at least 2 characters), best in Russian. May be omitted when authors, "
    "journal_ids or rubric_codes are given")]
Where = Annotated[list[WhereField] | None, Field(
    description="Fields to search: name (title), abstract, keywords, affiliation (authors' organizations), "
    "fulltext (also selects all others). Default: name, abstract, keywords")]
Types = Annotated[list[PubType] | None, Field(
    description="Publication types: article (journal articles), disser (dissertations), book, report, conf "
    "(conference materials), patent, preprint (deposited manuscripts), grant, dataset. Default: all")]
YearFrom = Annotated[int | None, Field(ge=1800, le=2100, description="First publication year (inclusive)")]
YearTo = Annotated[int | None, Field(ge=1800, le=2100, description="Last publication year (inclusive)")]
PeriodArg = Annotated[Period, Field(description="Added to eLIBRARY within: all, m1, m3, m6, m12 months")]
Sort = Annotated[SortField, Field(
    description="rank (relevance), date (issue date), insdate (date added), name (title), title (journal "
    "title), cited (citations)")]
Morph = Annotated[bool, Field(description="Use Russian morphology (word forms)")]
Fuzzy = Annotated[bool, Field(description="'Search similar text' as on the site (default on)")]
FulltextOnly = Annotated[bool, Field(description="Only publications with a full text on eLIBRARY.RU")]
AuthorNames = Annotated[list[str] | None, Field(description="Author filters, 'Фамилия И О' (e.g. 'Петров В А')")]
JournalIds = Annotated[list[int] | None, Field(description="Journal filters: eLIBRARY journal ids (titleid)")]
RubricCodes = Annotated[list[str] | None, Field(description="GRNTI rubric codes, e.g. '500000'")]


def _tool(title: str, annotations: ToolAnnotations = READ) -> Callable[[Callable[..., T]], Callable[..., T]]:
    return mcp.tool(title=title, annotations=annotations, meta=dict(RESULT_META))


def _pub_filters(*, where: list[str] | None, year_from: int | None, year_to: int | None, period: str, sort: str,
                 order: str, morphology: bool, fuzzy: bool, fulltext_only: bool, authors: list[str] | None,
                 journal_ids: list[int] | None, rubric_codes: list[str] | None, page: int) -> dict[str, Any]:
    data: dict[str, Any] = {
        "year_from": year_from, "year_to": year_to, "period": period, "sort": sort, "order": order,
        "morphology": morphology, "fuzzy": fuzzy, "fulltext_only": fulltext_only, "page": page,
        "where": where or None, "author_names": authors or None, "journal_ids": journal_ids or None,
        "rubric_codes": rubric_codes or None,
    }
    return {k: v for k, v in data.items() if v is not None}


@_tool("Search eLIBRARY publications (any type)")
def elibrary_search_publications(
    query: Text = None,
    types: Types = None,
    where: Where = None,
    year_from: YearFrom = None,
    year_to: YearTo = None,
    period: PeriodArg = "all",
    sort: Sort = "rank",
    order: Order = "desc",
    morphology: Morph = False,
    fuzzy: Fuzzy = True,
    fulltext_only: FulltextOnly = False,
    open_only: Annotated[bool, Field(description="Only publications available to the signed-in user")] = False,
    authors: AuthorNames = None,
    journal_ids: JournalIds = None,
    rubric_codes: RubricCodes = None,
    page: Page = 1,
) -> SearchPage[PublicationHit]:
    """Extended search of eLIBRARY.RU publications of any type: journal articles, dissertations, books,
    reports, conference materials, patents, deposited manuscripts, grants, datasets.

    Returns one page of up to 100 hits (id, title, authors, bibliographic source, year, RSCI citations) and
    the total. A new query costs 1 request, every further page 1 more (cached 24 h). Use the hit `id` with
    elibrary_get_publication for the full card only when needed."""
    filters = _pub_filters(where=where, year_from=year_from, year_to=year_to, period=period, sort=sort,
                           order=order, morphology=morphology, fuzzy=fuzzy, fulltext_only=fulltext_only,
                           authors=authors, journal_ids=journal_ids, rubric_codes=rubric_codes, page=page)
    if query is not None:
        filters["text"] = query
    if types:
        filters["types"] = types
    if open_only:
        filters["open_only"] = True
    return _call(lambda c: c.search_publications(filters))


@_tool("Search eLIBRARY journal articles")
def elibrary_search_articles(
    query: Text = None,
    where: Where = None,
    year_from: YearFrom = None,
    year_to: YearTo = None,
    period: PeriodArg = "all",
    sort: Sort = "rank",
    order: Order = "desc",
    morphology: Morph = False,
    fuzzy: Fuzzy = True,
    fulltext_only: FulltextOnly = False,
    authors: AuthorNames = None,
    journal_ids: JournalIds = None,
    rubric_codes: RubricCodes = None,
    page: Page = 1,
) -> SearchPage[PublicationHit]:
    """Search journal articles on eLIBRARY.RU (type 'article'). One page of up to 100 hits with id, title,
    authors, source (journal, year, volume, issue, pages), year and RSCI citations. 1 request per page
    (cached 24 h)."""
    filters = _pub_filters(where=where, year_from=year_from, year_to=year_to, period=period, sort=sort,
                           order=order, morphology=morphology, fuzzy=fuzzy, fulltext_only=fulltext_only,
                           authors=authors, journal_ids=journal_ids, rubric_codes=rubric_codes, page=page)
    return _call(lambda c: c.search_articles(query, **filters))


@_tool("Search eLIBRARY patents")
def elibrary_search_patents(
    query: Text = None,
    where: Where = None,
    year_from: YearFrom = None,
    year_to: YearTo = None,
    period: PeriodArg = "all",
    sort: Sort = "rank",
    order: Order = "desc",
    morphology: Morph = False,
    fuzzy: Fuzzy = True,
    authors: AuthorNames = None,
    page: Page = 1,
) -> SearchPage[PublicationHit]:
    """Search patents and software/database registration certificates on eLIBRARY.RU (type 'patent').
    The source line shows the patent number, dates and application number. 1 request per page."""
    filters = _pub_filters(where=where, year_from=year_from, year_to=year_to, period=period, sort=sort,
                           order=order, morphology=morphology, fuzzy=fuzzy, fulltext_only=False,
                           authors=authors, journal_ids=None, rubric_codes=None, page=page)
    return _call(lambda c: c.search_patents(query, **filters))


@_tool("Search eLIBRARY books")
def elibrary_search_books(
    query: Text = None,
    where: Where = None,
    year_from: YearFrom = None,
    year_to: YearTo = None,
    period: PeriodArg = "all",
    sort: Sort = "rank",
    order: Order = "desc",
    morphology: Morph = False,
    fuzzy: Fuzzy = True,
    fulltext_only: FulltextOnly = False,
    authors: AuthorNames = None,
    rubric_codes: RubricCodes = None,
    page: Page = 1,
) -> SearchPage[PublicationHit]:
    """Search books on eLIBRARY.RU (type 'book': monographs, textbooks, collections). Conference proceedings
    and their papers are filed under 'conf', and chapters under either: use elibrary_search_publications
    with types ['book', 'conf'] to cover both. 1 request per page."""
    filters = _pub_filters(where=where, year_from=year_from, year_to=year_to, period=period, sort=sort,
                           order=order, morphology=morphology, fuzzy=fuzzy, fulltext_only=fulltext_only,
                           authors=authors, journal_ids=None, rubric_codes=rubric_codes, page=page)
    return _call(lambda c: c.search_books(query, **filters))


@_tool("Search eLIBRARY authors")
def elibrary_search_authors(
    surname: Annotated[str | None, Field(
        description="Surname in Russian; 'Фамилия Имя Отчество' or 'Фамилия И.О.' narrows the search")] = None,
    identifier_type: Annotated[CodeType, Field(
        description="Type of `identifier`: SPIN, ORCID, ResearcherID, ScopusID, ArXivID, RSCI (eLIBRARY AuthorID)")
    ] = "SPIN",
    identifier: Annotated[str | None, Field(description="Personal identifier value, e.g. SPIN '9846-9426'")] = None,
    country: Annotated[str | None, Field(
        description="Country: ISO3 code (RUS, BLR, ...) or a Russian / English name ('Россия', 'Russia')")] = None,
    town: Annotated[str | None, Field(description="Town as spelled on the site, e.g. 'Москва'")] = None,
    org_id: Annotated[int | None, Field(
        ge=1, description="eLIBRARY organization id (from elibrary_search_organizations); preferred")] = None,
    org_name: Annotated[str | None, Field(
        description="Organization name, used only without org_id: resolved to an org id with ONE extra request "
        "(organization search sorted by publications; exact name match first, otherwise the organization with "
        "the most publications). The organization used is returned in query.org_resolved")] = None,
    rubric_code: Annotated[str | None, Field(description="GRNTI rubric code of the main topic, 6 digits")] = None,
    metrics: Annotated[MetricsBasis, Field(
        description="Basis of the shown counts: core (RSCI core), rinc (RSCI), elibrary (all)")] = "rinc",
    sort: Annotated[AuthorSort, Field(description="name, publications, citations or hindex")] = "citations",
    order: Order = "desc",
    page: Page = 1,
) -> SearchPage[AuthorHit]:
    """Search authors on eLIBRARY.RU by surname, personal identifier (SPIN, ORCID, ...), organization,
    town or rubric. Returns up to 100 authors per page with AuthorID, organization, publication and citation
    counts and h-index. 1 request per page (cached 24 h). At least surname, identifier, org_id or org_name is
    needed. The site filters by organization id only: org_name (without org_id) is resolved to an org id with
    one extra request (cached); check query.org_resolved and pass org_id when another organization is meant."""

    def action(c: ElibraryClient) -> SearchPage[AuthorHit]:
        data: dict[str, Any] = {
            "surname": surname, "country": country, "town": town, "org_id": org_id, "org_name": org_name,
            "rubric_code": rubric_code, "metrics": metrics, "sort": sort, "order": order, "page": page,
        }
        if identifier:
            data["code_type"], data["code_value"] = identifier_type, identifier
        return c.search_authors({k: v for k, v in data.items() if v is not None})

    return _call(action)


@_tool("Search eLIBRARY organizations")
def elibrary_search_organizations(
    name: Annotated[str | None, Field(description="Words of the organization name, e.g. 'МГУ' or 'университет'")] = None,
    town: Annotated[str | None, Field(description="Town as spelled on the site, e.g. 'Новосибирск'")] = None,
    region: Annotated[str | None, Field(
        description="Region name or id (see elibrary_reference_data name='regions')")] = None,
    country: Annotated[str | None, Field(description="Country: ISO3 code ('RUS') or Russian name")] = None,
    sort: Annotated[OrgSort, Field(description="name, town, publications or citations")] = "name",
    order: Annotated[SortOrder | None, Field(description="asc/desc; default as on the site")] = None,
    page: Page = 1,
) -> SearchPage[OrgHit]:
    """Search organizations (universities, institutes, companies) on eLIBRARY.RU. Returns 20 per page with
    organization id, town, publication and citation counts. 1 request per page (cached 24 h)."""
    data: dict[str, Any] = {"name": name, "town": town, "country": country, "sort": sort, "order": order,
                            "page": page}
    if region:
        if region.strip().isdigit():
            data["region_id"] = int(region)
        else:
            data["region"] = region
    return _call(lambda c: c.search_organizations({k: v for k, v in data.items() if v is not None}))


@_tool("Search eLIBRARY journals")
def elibrary_search_journals(
    name: Annotated[str | None, Field(description="Fragment of the journal title, publisher or ISSN")] = None,
    country: Annotated[str | None, Field(
        description="RUS (Russia), nRUS (foreign), an ISO3 code or a Russian country name")] = None,
    rubric: Annotated[str | None, Field(
        description="Top-level GRNTI rubric: code ('270000' or '27') or Russian name ('Математика')")] = None,
    language: Annotated[str | None, Field(description="RU, EN, nRU (foreign), ML (multilingual) or a name")] = None,
    vak: Annotated[VakFilter | None, Field(description="VAK list: included, k1, k2, k3 (category), not")] = None,
    scopus: Annotated[ScopusFilter | None, Field(description="Scopus: indexed, translated, partial, q1..q4, not")] = None,
    wos: Annotated[WosFilter | None, Field(
        description="Web of Science: indexed, translated, partial, q1..q4, q1-q4 (any quartile), not")] = None,
    white_list: Annotated[WhiteFilter | None, Field(
        description="Белый список / ЕГПНИ: included, l1..l4 (level), not; egpni*, any*")] = None,
    risc: Annotated[RiscFilter | None, Field(description="RSCI: indexed, not, q1..q4 (Science Index quartile)")] = None,
    open_access: Annotated[FtOpenFilter | None, Field(description="Full texts: open, partial, subscription")] = None,
    translated: Annotated[TranslatedFilter | None, Field(description="English translation: full, partial, no")] = None,
    rsci: Annotated[bool, Field(description="Only journals of the RSCI database on Web of Science")] = False,
    doaj: Annotated[bool, Field(description="Only journals indexed in DOAJ")] = False,
    core_rinc: Annotated[bool, Field(description="Only journals of the RSCI core (ядро РИНЦ)")] = False,
    with_fulltext: Annotated[bool, Field(description="Only journals with full texts on eLIBRARY.RU")] = False,
    active_only: Annotated[bool, Field(description="Only journals published now")] = False,
    scientific_only: Annotated[bool, Field(description="Only scientific journals")] = False,
    sort: Annotated[JournalSort, Field(
        description="name, articles, citations_rinc, citations_core (default), si_rating")] = "citations_core",
    order: Annotated[SortOrder | None, Field(description="asc/desc; default as on the site")] = None,
    page: Page = 1,
) -> SearchPage[JournalHit]:
    """Search the eLIBRARY.RU journal catalog by title/ISSN and by indexing filters (VAK list, Scopus/WoS
    quartiles, white list, RSCI, open access). Returns journal ids, publishers, numbers of issues, articles
    and citations. 1 request per page (cached 24 h)."""
    data: dict[str, Any] = {
        "name": name, "country": country, "rubric_code": rubric, "language": language, "vak": vak,
        "scopus": scopus, "wos": wos, "white": white_list, "risc": risc, "ftopen": open_access,
        "translated": translated, "rsci": rsci, "doaj": doaj, "core_rinc": core_rinc, "fulltext": with_fulltext,
        "active": active_only, "scientific": scientific_only, "sort": sort, "order": order, "page": page,
    }
    return _call(lambda c: c.search_journals({k: v for k, v in data.items() if v is not None}))


#: The tool (and its id argument) that shows each kind of object: hint of a ``WrongTarget`` error.
TARGET_TOOLS: dict[str, tuple[str, str]] = {
    "author": ("elibrary_get_author", "author_id"),
    "organization": ("elibrary_get_organization", "org_id"),
    "journal": ("elibrary_get_journal", "journal_id"),
    "issue": ("elibrary_get_journal_issue", "issue_id"),
}


def _wrong_target_hint(target: str | None, target_id: int | None) -> str:
    """``"; используйте elibrary_get_author(author_id=3771)"`` for a known target, else a generic hint."""
    if target not in TARGET_TOOLS:
        return "; проверьте идентификатор с помощью elibrary_resolve_identifier"
    tool, arg = TARGET_TOOLS[target]
    return f"; используйте {tool}" + (f"({arg}={target_id})" if target_id else "")


@_tool("Get an eLIBRARY publication card")
def elibrary_get_publication(
    identifier: Annotated[str, Field(
        min_length=1, description="eLIBRARY item id (e.g. '46371463'), EDN ('FYOAKL'), DOI ('10.xxxx/...'), an "
        "elibrary.ru item URL, or an ISBN")],
) -> Publication:
    """Full card of one publication: title (Russian and English), authors with AuthorIDs and affiliations,
    journal/book/conference, year, volume, issue, pages, DOI, EDN, abstract, keywords, rubrics, RSCI
    metrics, patent/book/dissertation data and full-text availability. 1 request (cached 30 days); a DOI
    lookup may cost 1 more."""

    def action(c: ElibraryClient) -> Publication:
        from elibrary_cli.client import WrongTarget

        try:
            return c.get_publication_any(identifier)
        except WrongTarget as exc:
            raise ToolError(f"{exc}{_wrong_target_hint(exc.target, exc.target_id)}") from exc

    return _call(action)


@_tool("Get an eLIBRARY author profile")
def elibrary_get_author(
    author_id: Annotated[int, Field(ge=1, description="eLIBRARY AuthorID (from elibrary_search_authors)")],
) -> AuthorProfile:
    """Author profile: SPIN/ORCID/ResearcherID/Scopus ids, organization, numbers of publications and
    citations (eLIBRARY / RSCI / RSCI core), h-index, yearly indicators. 1 request (cached 7 days)."""
    return _call(lambda c: c.get_author(author_id))


@_tool("List publications of an eLIBRARY author")
def elibrary_get_author_publications(
    author_id: Annotated[int, Field(ge=1, description="eLIBRARY AuthorID")],
    page: Page = 1,
    sort: Annotated[AuthorItemsSort, Field(
        description="date, title, year_author, year_title, first_author, citations, added, relevance")] = "date",
    order: Order = "desc",
    include_references: Annotated[bool, Field(
        description="Also rows known only from reference lists (no eLIBRARY card)")] = True,
    merge_versions: Annotated[bool, Field(description="Merge versions (original and translation)")] = True,
) -> AuthorPublicationsPage:
    """Publications of an author, 100 per page, with ids, sources, years and citations. 1 request per page
    (cached 7 days)."""
    return _call(lambda c: c.get_author_publications(author_id, page, sort=sort, order=order,
                                                     include_references=include_references,
                                                     merge_versions=merge_versions))


@_tool("Get an eLIBRARY organization profile")
def elibrary_get_organization(
    org_id: Annotated[int, Field(ge=1, description="eLIBRARY organization id (from elibrary_search_organizations)")],
    with_card: Annotated[bool, Field(
        description="Also read the organization card (full name, address, type, website): 1 more request")] = False,
) -> OrgProfile:
    """Organization profile with publication-activity indicators (publications and citations in eLIBRARY /
    RSCI / RSCI core, h-index, authors, yearly series). 1 request (cached 7 days), 2 with with_card."""
    return _call(lambda c: c.get_organization(org_id, with_card=with_card))


#: Readable names of the ``show_option`` codes of org_items.asp.
ORG_SHOW = {"all": "1", "rinc": "0", "core": "2", "vak": "5", "rsci": "3", "wos": "9", "scopus": "10",
            "wos_or_scopus": "4", "doi": "11", "isbn": "12"}


@_tool("List publications of an eLIBRARY organization")
def elibrary_get_organization_publications(
    org_id: Annotated[int, Field(ge=1, description="eLIBRARY organization id")],
    page: Page = 1,
    years: Annotated[list[int] | None, Field(description="Publication years to keep, e.g. [2023, 2024]")] = None,
    show: Annotated[Literal["all", "rinc", "core", "vak", "rsci", "wos", "scopus", "wos_or_scopus", "doi", "isbn"]
                    | None, Field(description="Which publications: all, rinc (in RSCI), core (RSCI core), vak, rsci, "
                                  "wos, scopus, wos_or_scopus, doi (with DOI), isbn; default as on the site")] = None,
    sort: Annotated[Literal["date", "title", "year_author", "author", "cited", "insdate"] | None, Field(
        description="date, title, year_author, author, cited, insdate")] = None,
    order: Annotated[SortOrder | None, Field(description="desc or asc")] = None,
) -> OrgPublicationsPage:
    """Publications of an organization, 100 per page. Large organizations answer slowly. 1 request per page
    (cached 7 days)."""
    return _call(lambda c: c.get_organization_publications(
        org_id, page, show_option=ORG_SHOW[show] if show else None, years=years or None, sort=sort, order=order))


@_tool("Get an eLIBRARY journal profile")
def elibrary_get_journal(
    journal_id: Annotated[int, Field(ge=1, description="eLIBRARY journal id (titleid, from elibrary_search_journals)")],
    english: Annotated[bool, Field(description="Read the English description of the journal")] = False,
    with_indicators: Annotated[bool, Field(
        description="Also read impact factors and the Science Index rating: 1 more request")] = False,
) -> JournalProfile:
    """Journal profile: ISSN/eISSN, publisher, founders, indexing (RSCI, VAK category, white list, Scopus/WoS
    quartiles), rubrics, editorial board, frequency, archive. 1 request (cached 30 days)."""
    return _call(lambda c: c.get_journal(journal_id, english=english, with_indicators=with_indicators))


@_tool("Get the contents of an eLIBRARY journal issue")
def elibrary_get_journal_issue(
    journal_id: Annotated[int | None, Field(ge=1, description="Journal id: returns its latest issue")] = None,
    issue_id: Annotated[int | None, Field(ge=1, description="Issue id (contents.asp?id=): this issue")] = None,
) -> IssueContents:
    """Contents of a journal issue (articles with sections and pages) and the list of the journal's issues.
    Give issue_id for a given issue or journal_id for the latest one. 1 request."""
    return _call(lambda c: c.get_journal_issue(journal_id=journal_id, issue_id=issue_id))


@_tool("Resolve an identifier on eLIBRARY")
def elibrary_resolve_identifier(
    identifier: Annotated[str, Field(
        min_length=1, description="eLIBRARY id, EDN, DOI, ISBN, SPIN, ORCID, ISSN, ROR or an elibrary.ru page URL")],
) -> IdentifierResolution:
    """Find what an identifier points to on eLIBRARY.RU (publication, author, journal, organization, issue)
    and its eLIBRARY id. Ids and elibrary.ru URLs cost no request; EDN/DOI/ISBN/SPIN/ORCID/ISSN/ROR cost 1
    (answers are cached for a year)."""
    return _call(lambda c: IdentifierResolution.model_validate(c.resolve(identifier)))


@_tool("Get the full text of an eLIBRARY publication")
def elibrary_get_fulltext(
    identifier: Annotated[str, Field(min_length=1, description="eLIBRARY item id, EDN, DOI or item URL")],
    offset: Annotated[int, Field(ge=0, description="Start of the chunk (pass next_offset of the previous call)")] = 0,
    max_chars: Annotated[int, Field(ge=500, le=100_000, description="Chunk size, characters")] = 20_000,
    sources: Annotated[list[SourceName] | None, Field(
        description="Sources to try, in order: elibrary (PDF, needs a signed-in session), cyberleninka, "
        "unpaywall, openalex, crossref, publisher. Default: all")] = None,
    refresh: Annotated[bool, Field(description="Ignore the local full-text cache")] = False,
) -> FullText:
    """Full text of a publication, one chunk at a time: the eLIBRARY.RU PDF when the session is signed in,
    otherwise open sources (CyberLeninka, Unpaywall, OpenAlex, Crossref, the publisher's page). The first
    call reads the card (1 request, cached) and the sources; further chunks (offset = next_offset) come from
    the local cache. next_offset is null at the end."""
    return _call(lambda c: c.get_fulltext(identifier, offset=offset, max_chars=max_chars, sources=sources,
                                          refresh=refresh))


_EXPORT_FORMATS = {"csl_json": "csl", "bibtex": "bibtex", "ris": "ris", "zotero_json": "zotero"}


@_tool("Export citations of eLIBRARY publications")
def elibrary_export_citation(
    identifiers: Annotated[list[str], Field(
        min_length=1, max_length=50, description="eLIBRARY item ids, EDNs, DOIs or item URLs")],
    format: Annotated[Literal["csl_json", "bibtex", "ris", "zotero_json"], Field(
        description="csl_json (CSL JSON array), bibtex, ris, zotero_json (Zotero API item JSON)")] = "csl_json",
    fix_caps: Annotated[bool, Field(description="Convert titles shown in CAPITALS to normal case")] = True,
    plain_csl: Annotated[bool, Field(
        description="csl_json only: full CSL JSON with every CSL key (event-title, container-author, source...) "
        "instead of the default zotero-mcp friendly variant, which keeps only the keys zotero_add_item maps to "
        "Zotero fields and moves the others into `note`")] = False,
) -> CitationExport:
    """Bibliographic records of publications built from their eLIBRARY.RU cards (1 cached request per card).
    The `text` of csl_json or bibtex output can be passed to zotero-mcp's zotero_add_item as
    source=<text> with source_type "csl_json" or "bibtex" (see zotero_mcp_source_type) to save the items to
    Zotero; csl_json is shaped for that by default (plain_csl=true gives standard CSL JSON for other tools).
    Or use elibrary_zotero_add, which writes to Zotero directly and keeps more fields."""
    fmt = _EXPORT_FORMATS[format]

    def action(c: ElibraryClient) -> CitationExport:
        text = c.export(identifiers, fmt, as_text=True, fix_caps=fix_caps,
                        zotero_mcp=format == "csl_json" and not plain_csl)
        source_type = {"csl_json": "csl_json", "bibtex": "bibtex"}.get(format)
        return CitationExport(format=format, count=len(identifiers), text=str(text),
                              zotero_mcp_source_type=source_type)  # type: ignore[arg-type]

    return _call(action)


@_tool("Save eLIBRARY publications to Zotero",
       ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True))
def elibrary_zotero_add(
    identifiers: Annotated[list[str], Field(
        min_length=1, max_length=50, description="eLIBRARY item ids, EDNs, DOIs or item URLs")],
    collection: Annotated[str | None, Field(
        description="Zotero collection: key, name or 'Parent/Child' path (created when missing)")] = None,
    tags: Annotated[list[str] | None, Field(description="Tags to add to every item")] = None,
    with_pdf: Annotated[bool, Field(
        description="Also look for the full text and attach the PDF when one is found (more requests)")] = False,
) -> ZoteroAddResult:
    """Save publications to the user's Zotero library (Zotero desktop local API / connector, or the Zotero
    Web API with ZOTERO_API_KEY). Skips items already in the library (DOI/EDN/eLIBRARY ID/URL) and repeats;
    with the connector and Zotero's local API off there is no duplicate check (the item message says so),
    so a retry adds another copy. Works only when Zotero is reachable; otherwise fails without saving and
    suggests elibrary_export_citation + zotero-mcp."""
    try:
        writer = zotero_factory()
    except ElibraryError as exc:
        raise ToolError(f"{ZOTERO_UNAVAILABLE_HINT}\n\n{exc}") from exc
    try:
        state = writer.ping()
    except ElibraryError as exc:
        raise ToolError(f"{ZOTERO_UNAVAILABLE_HINT}\n\n{exc}") from exc
    finally:
        writer.close()
    if not state.get("available_backends"):
        hints = "\n".join(state.get("hints") or [])
        raise ToolError(f"{ZOTERO_UNAVAILABLE_HINT}\n\n{hints}".rstrip())

    def action(c: ElibraryClient) -> ZoteroAddResult:
        rows = c.zotero_add(identifiers, collection=collection, tags=tags, with_pdf=with_pdf)
        items = [ZoteroAddItem.model_validate({k: v for k, v in r.items() if k in ZoteroAddItem.model_fields})
                 for r in rows]
        return ZoteroAddResult(items=items, created=sum(i.status == "created" for i in items),
                               duplicates=sum(i.status == "duplicate" for i in items),
                               failed=sum(i.status == "failed" for i in items))

    return _call(action)


@_tool("eLIBRARY session status (no request)", READ_LOCAL)
def elibrary_session_status() -> SessionStatus:
    """Local session state without any request: session cookie present (names only), signed-in account,
    requests in the last hour/day against the local limits, and whether requests are paused after a captcha
    (breaker_open, breaker_url)."""
    return _call(lambda c: SessionStatus.from_status(c.status()))


@_tool("Check the eLIBRARY session",
       ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True))
def elibrary_session_check() -> SessionInfo:
    """Make ONE request to see the session state (signed in, anonymous or captcha) and update the stored
    account. Use once after the user signed in in the browser and you imported the cookies. Never lifts a
    pause after an IP ban (ip_blocked, HTTP 403); lifts a captcha pause only when cookies with SUserID were
    imported after it started, otherwise fails without a request. Do not call it repeatedly."""
    return _call(lambda c: c.check_session())


@_tool("Import eLIBRARY browser cookies",
       ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False))
def elibrary_set_cookies(
    cookies: Annotated[str, Field(
        min_length=3, description="Cookie string from the user's browser: the output of copy(document.cookie) on "
        "elibrary.ru, a 'Cookie:' header, a JSON export or cookies.txt content")],
    user_agent: Annotated[str | None, Field(
        description="The browser's navigator.userAgent, when the user gives it")] = None,
) -> CookieImportResult:
    """Install the cookies the user copied from their browser after passing the captcha or signing in there.
    Replaces the stored session cookies (a string without SUserID is added to the stored session instead).
    Clears a captcha pause only when the string contains SUserID; never clears a pause after an IP ban.
    Makes no request. Returns cookie names and warnings, never values."""

    def action(c: ElibraryClient) -> CookieImportResult:
        res = CookieImportResult.model_validate(c.set_cookies(cookies, user_agent))
        if res.breaker_open and res.breaker_gate in BAN_GATES:
            res.message = ("Cookies сохранены, но доступ с этого IP заблокирован: пауза запросов не снята. Не "
                           "повторяйте запросы; пользователь может написать в support@elibrary.ru.")
        elif res.breaker_open:
            res.message = ("Cookies сохранены, но пауза запросов не снята: в строке нет SUserID сессии, прошедшей "
                           "проверку. Попросите пользователя пройти проверку в браузере и передать cookies заново.")
        elif res.session_cookie_imported:
            res.message = ("Cookies сохранены; повторите прерванный запрос один раз. Если пользователь вошёл в "
                           "библиотеку в браузере, вызовите elibrary_session_check (1 запрос), чтобы стали доступны "
                           "PDF eLIBRARY.RU.")
        else:
            res.message = "Cookies сохранены, но без SUserID пройденная проверка не перенесётся."
        return res

    return _call(action)


@_tool("eLIBRARY reference data (filter codes)", READ_LOCAL)
def elibrary_reference_data(
    name: Annotated[str | None, Field(
        description="List name or alias, e.g. 'countries', 'regions', 'rubrics', 'vak', 'journals.wos', "
        "'authors.codetype'. Omit to get all list names")] = None,
    find: Annotated[str | None, Field(description="Return only options matching this text (e.g. 'Новосиб')")] = None,
    limit: Annotated[int, Field(ge=1, le=1000, description="Maximum number of options returned")] = 100,
) -> ReferenceData:
    """Codes of the search-form options (countries, regions, GRNTI rubrics, journal filters, author id types,
    sort orders...) shipped with the package. No network request."""
    from elibrary_cli import refdata

    def action(_c: Any) -> ReferenceData:
        if not name:
            return ReferenceData(lists=[ReferenceList.model_validate(x) for x in refdata.describe_options()])
        canonical = refdata.resolve_name(name)
        options = refdata.find_options(canonical, find, limit=limit) if find else refdata.list_options(canonical)
        return ReferenceData(name=canonical, options=[ReferenceOption.model_validate(o) for o in options[:limit]])

    try:
        return action(None)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ============================================================================ entry point
def run(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8000, *,
        settings: Settings | None = None) -> None:
    """Run the server (blocking). ``settings`` replace ``Settings.from_env()`` for the shared client."""
    if settings is not None:
        configure(settings)
    if transport == "stdio":
        mcp.run()
    elif transport == "streamable-http":
        mcp.run("streamable-http", host=host, port=port)
    else:
        raise ValueError(f"Неизвестный транспорт {transport!r}: stdio или streamable-http")


def main(argv: list[str] | None = None) -> None:
    """``elibrary-mcp`` console script."""
    parser = argparse.ArgumentParser(prog="elibrary-mcp", description="MCP-сервер eLIBRARY.RU")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio",
                        help="транспорт MCP (по умолчанию stdio)")
    parser.add_argument("--host", default="127.0.0.1", help="адрес для streamable-http")
    parser.add_argument("--port", type=int, default=8000, help="порт для streamable-http")
    args = parser.parse_args(argv)
    run(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()
