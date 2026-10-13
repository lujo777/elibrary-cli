"""Tests of the MCP server (``elibrary_cli.mcp_server``) through the in-memory ``mcp.Client``.

The shared client of the server is replaced (``mcp_server.set_client``) by an ``ElibraryClient`` over
the conftest ``FakeSite``; the Zotero probe uses a fake writer. Nothing is sent to elibrary.ru,
Zotero or any open-access API."""

from __future__ import annotations

import json
import threading
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
from mcp import Client

from elibrary_cli import mcp_server
from elibrary_cli.client import ElibraryClient
from elibrary_cli.fulltext import FullText
from elibrary_cli.fulltext.net import ExternalHTTP

from .conftest import FakeSite, fixture_bytes

pytestmark = pytest.mark.anyio

HTML = {"Content-Type": "text/html; Charset=UTF-8"}
DOI = "10.47056/0365-9615-2021-172-7-98-101"

EXPECTED_TOOLS = {
    "elibrary_search_publications", "elibrary_search_articles", "elibrary_search_patents", "elibrary_search_books",
    "elibrary_search_authors", "elibrary_search_organizations", "elibrary_search_journals",
    "elibrary_get_publication", "elibrary_get_author", "elibrary_get_author_publications",
    "elibrary_get_organization", "elibrary_get_organization_publications", "elibrary_get_journal",
    "elibrary_get_journal_issue", "elibrary_resolve_identifier", "elibrary_get_fulltext",
    "elibrary_export_citation", "elibrary_zotero_add", "elibrary_session_status", "elibrary_session_check",
    "elibrary_set_cookies", "elibrary_reference_data",
}
NOT_READ_ONLY = {"elibrary_set_cookies", "elibrary_zotero_add", "elibrary_session_check"}
LOCAL_ONLY = {"elibrary_session_status", "elibrary_set_cookies", "elibrary_reference_data"}


def params_of(req: httpx2.Request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(str(req.url)).query, keep_blank_values=True))


def body_of(req: httpx2.Request) -> dict[str, str]:
    return dict(parse_qsl(req.content.decode("ascii"), keep_blank_values=True))


def _no_external(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - must not be called
    raise AssertionError(f"unexpected external request {request.url}")


class FakeZotero:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.calls: list[dict] = []
        self.closed = 0

    def ping(self, *, refresh: bool = True) -> dict:
        return {"running": self.running, "available_backends": ["connector"] if self.running else [],
                "hints": [] if self.running else ["Zotero не запущен или недоступен на http://127.0.0.1:23119"]}

    def add(self, items, *, collection=None, tags=None, pdf_paths=None, dedupe=True, backend=None, **kw):
        items = list(items)
        self.calls.append({"items": items, "collection": collection, "tags": tags})
        return [{"index": i, "title": p.title, "url": p.url, "key": "ABCD1234", "backend": "connector",
                 "status": "duplicate" if i else "created", "message": None, "attachment": None,
                 "collection": collection} for i, p in enumerate(items)]

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def zotero() -> FakeZotero:
    return FakeZotero()


@pytest.fixture
def client(http, settings, zotero, monkeypatch):
    net = ExternalHTTP(settings, transport=httpx2.MockTransport(_no_external), cache=False)
    c = ElibraryClient(http=http, net=net, zotero=zotero)
    mcp_server.set_client(c)
    monkeypatch.setattr(mcp_server, "zotero_factory", lambda: zotero)
    yield c
    mcp_server.set_client(None)
    c.close()
    net.close()


@pytest.fixture
async def mcp(client):
    async with Client(mcp_server.mcp, raise_exceptions=True) as c:
        yield c


def text_of(result) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)


# ============================================================================ tool list


async def test_list_tools_names_annotations_schemas(mcp):
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.annotations.read_only_hint is (name not in NOT_READ_ONLY), name
        assert tool.annotations.open_world_hint is (name not in LOCAL_ONLY), name
        assert tool.meta["anthropic/maxResultSizeChars"] == 200_000
        assert tool.output_schema is not None, name
        assert len(tool.description or "") <= 2048
        assert all(1 <= len(p) <= 64 for p in tool.input_schema.get("properties", {}))
    assert tools["elibrary_zotero_add"].annotations.destructive_hint is False
    titles = {name: t.output_schema.get("title") for name, t in tools.items()}
    assert titles["elibrary_search_articles"] == "SearchPage[PublicationHit]"
    assert titles["elibrary_search_authors"] == "SearchPage[AuthorHit]"
    assert titles["elibrary_search_organizations"] == "SearchPage[OrgHit]"
    assert titles["elibrary_search_journals"] == "SearchPage[JournalHit]"
    assert titles["elibrary_get_publication"] == "Publication"
    assert titles["elibrary_get_author"] == "AuthorProfile"
    assert titles["elibrary_get_author_publications"] == "AuthorPublicationsPage"
    assert titles["elibrary_get_organization"] == "OrgProfile"
    assert titles["elibrary_get_journal_issue"] == "IssueContents"
    assert titles["elibrary_get_fulltext"] == "FullText"
    assert titles["elibrary_resolve_identifier"] == "IdentifierResolution"
    assert titles["elibrary_export_citation"] == "CitationExport"
    assert titles["elibrary_session_check"] == "SessionInfo"
    assert "next_offset" in tools["elibrary_get_fulltext"].output_schema["properties"]


async def test_input_schemas_have_enums_and_descriptions(mcp):
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    props = tools["elibrary_search_publications"].input_schema["properties"]
    assert {"query", "types", "where", "year_from", "year_to", "sort", "order", "page"} <= set(props)
    types_schema = json.dumps(props["types"])
    for value in ("article", "disser", "book", "conf", "patent"):
        assert f'"{value}"' in types_schema
    assert "description" in props["query"]
    assert "types" not in tools["elibrary_search_articles"].input_schema["properties"]
    export = tools["elibrary_export_citation"]
    assert "zotero_add_item" in export.description and "csl_json" in export.description
    assert "bibtex" in export.description
    fmt = json.dumps(export.input_schema["properties"]["format"])
    assert all(v in fmt for v in ("csl_json", "bibtex", "ris", "zotero_json"))
    assert tools["elibrary_get_fulltext"].input_schema["properties"]["max_chars"]["maximum"] == 100_000


def test_instructions_are_concise_and_mention_captcha():
    text = mcp_server.INSTRUCTIONS
    assert len(text) <= 2048
    assert "elibrary auth captcha" in text and "elibrary_set_cookies" in text
    assert "Russian" in text and "rate-limited" in text
    assert mcp_server.mcp.name == "elibrary"


# ============================================================================ searches


async def test_search_articles(mcp, site: FakeSite):
    site.page("query_results.asp", "query_results_all_types")
    r = await mcp.call_tool("elibrary_search_articles", {"query": "нейронные сети", "year_from": 2020,
                                                         "sort": "cited"})
    assert r.is_error is False
    data = r.structured_content
    assert data["total"] == 290705 and data["pages"] == 2908
    assert data["items"][0]["id"] == 47430099
    assert data["items"][0]["title"].startswith("ОТКАЗОУСТОЙЧИВАЯ НЕЙРОННАЯ СЕТЬ")
    p = params_of(site.requests[0])
    assert p["ftext"] == "нейронные сети" and p["begin_year"] == "2020" and p["orderby"] == "cited"
    assert {k for k in p if k.startswith("type_")} == {"type_article"}


async def test_search_publications_types_and_filters(mcp, site):
    site.page("query_results.asp", "query_results_patents")
    r = await mcp.call_tool("elibrary_search_publications", {
        "query": "сеть", "types": ["patent", "disser"], "where": ["name"], "authors": ["Борисов В В"],
        "journal_ids": [8610], "rubric_codes": ["500000"], "fuzzy": False, "open_only": True})
    assert r.is_error is False
    assert r.structured_content["total"] == 27920
    p = params_of(site.requests[0])
    assert {k for k in p if k.startswith("type_")} == {"type_patent", "type_disser"}
    assert p["authors_all"] == "Борисов В В|" and p["titles_all"] == "8610|" and p["rubrics_all"] == "500000|"
    assert p["search_open"] == "on" and "search_freetext" not in p


@pytest.mark.parametrize(("tool", "types"), [
    ("elibrary_search_patents", {"type_patent"}),
    ("elibrary_search_books", {"type_book"}),
])
async def test_search_presets(mcp, site, tool, types):
    site.page("query_results.asp", "query_results_patents")
    r = await mcp.call_tool(tool, {"query": "нейросеть"})
    assert r.is_error is False
    assert r.structured_content["items"][0]["id"] == 88918718
    assert {k for k in params_of(site.requests[0]) if k.startswith("type_")} == types


async def test_search_validation_errors_are_tool_errors(mcp, site):
    r = await mcp.call_tool("elibrary_search_articles", {"query": "x"})
    assert r.is_error is True
    assert "не менее двух символов" in text_of(r)
    r = await mcp.call_tool("elibrary_search_articles", {"query": "нейроны", "page": 0})
    assert r.is_error is True
    r = await mcp.call_tool("elibrary_search_publications", {"query": "нейроны", "types": ["poem"]})
    assert r.is_error is True
    assert site.requests == []


async def test_search_authors(mcp, site):
    site.page("authors.asp", "authors_results_petrov")
    r = await mcp.call_tool("elibrary_search_authors", {"surname": "Петров", "country": "Россия",
                                                        "identifier_type": "SPIN", "identifier": "9846-9426"})
    assert r.is_error is False
    first = r.structured_content["items"][0]
    assert first["id"] == 3771 and first["hindex"] == 119 and first["town"] == "Протвино"
    body = body_of(site.requests[0])
    assert body["surname"] == "Петров" and body["countryid"] == "RUS"
    assert body["codetype"] == "SPIN" and body["codevalue"] == "9846-9426"


async def test_search_authors_org_name_is_resolved_to_an_org_id(mcp, site):
    site.page("orgs.asp", "orgs_results_mgu").page("authors.asp", "authors_results_petrov")
    r = await mcp.call_tool("elibrary_search_authors", {"surname": "Петров", "country": "Russia",
                                                        "org_name": "московский государственный университет"})
    assert r.is_error is False, text_of(r)
    orgs_req, authors_req = site.requests
    assert orgs_req.url.path == "/orgs.asp"
    assert body_of(orgs_req)["orgname"] == "московский государственный университет"
    assert body_of(orgs_req)["sortorder"] == "3"  # by publications
    body = body_of(authors_req)
    assert (body["orgid"], body["countryid"], body["surname"]) == ("2541", "RUS", "Петров")
    resolved = r.structured_content["query"]["org_resolved"]
    assert resolved["id"] == 2541 and resolved["name"].startswith("Московский государственный университет")
    assert resolved["match"] == "most_publications" and resolved["candidates"] == 23
    # org_id wins over org_name: no organization search
    site.requests.clear()
    r = await mcp.call_tool("elibrary_search_authors", {"surname": "Петров", "org_id": 169, "org_name": "МГУ"})
    assert r.is_error is False and [q.url.path for q in site.requests] == ["/authors.asp"]
    assert "org_resolved" not in r.structured_content["query"]


async def test_search_authors_org_name_not_found(mcp, site):
    site.page("orgs.asp", "orgs_form")
    r = await mcp.call_tool("elibrary_search_authors", {"org_name": "Институт Атлантиды"})
    assert r.is_error is True
    assert "Организация «Институт Атлантиды» не найдена" in text_of(r)
    assert [q.url.path for q in site.requests] == ["/orgs.asp"]


async def test_search_authors_describes_the_org_name_lookup(mcp):
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    tool = tools["elibrary_search_authors"]
    assert "org_name" in tool.description and "one extra request" in tool.description
    props = tool.input_schema["properties"]
    assert "ONE extra request" in props["org_name"]["description"]
    assert "Russia" in props["country"]["description"]


async def test_search_authors_needs_a_criterion(mcp, site):
    r = await mcp.call_tool("elibrary_search_authors", {"town": "Москва"})
    assert r.is_error is True
    assert "Укажите фамилию автора" in text_of(r)
    assert site.requests == []


async def test_search_organizations(mcp, site):
    site.page("orgs.asp", "orgs_results_mgu")
    r = await mcp.call_tool("elibrary_search_organizations", {"name": "МГУ", "region": "73"})
    assert r.is_error is False
    assert r.structured_content["total"] == 23
    assert r.structured_content["items"][0]["publications"] == 429371
    assert body_of(site.requests[0])["regionid"] == "73"


async def test_search_journals(mcp, site):
    site.page("titles.asp", "titles_results")
    r = await mcp.call_tool("elibrary_search_journals", {"name": "вычислительные", "vak": "k1",
                                                         "white_list": "l2", "with_fulltext": True})
    assert r.is_error is False
    hit = r.structured_content["items"][0]
    assert hit["id"] == 8610 and hit["issues"] == 222 and hit["citations"] == 6570
    body = body_of(site.requests[0])
    assert body["vak"] == "2" and body["fulltext"] == "on"


# ============================================================================ cards


async def test_get_publication_by_id_edn_doi(mcp, site):
    site.add(lambda r: DOI in str(r.url),
             lambda r: httpx2.Response(302, headers={"Location": "/item.asp?id=46371463"}))
    site.page("item.asp", "item_article_46371463")
    r = await mcp.call_tool("elibrary_get_publication", {"identifier": "46371463"})
    assert r.is_error is False
    pub = r.structured_content
    assert pub["id"] == 46371463 and pub["edn"] == "FYOAKL" and pub["doi"] == DOI
    assert pub["journal"]["issn"] is not None
    # EDN: served from the cache (the card is stored under both keys)
    r = await mcp.call_tool("elibrary_get_publication", {"identifier": "FYOAKL"})
    assert r.structured_content["id"] == 46371463
    r = await mcp.call_tool("elibrary_get_publication", {"identifier": DOI})
    assert r.structured_content["edn"] == "FYOAKL"
    assert [r.url.path for r in site.requests] == ["/item.asp", f"/{DOI}"]


async def test_get_publication_wrong_target(mcp, site):
    r = await mcp.call_tool("elibrary_get_publication",
                            {"identifier": "https://elibrary.ru/title_about_new.asp?id=8610"})
    assert r.is_error is True
    assert "не на публикацию" in text_of(r) and "elibrary_get_journal" in text_of(r)
    assert "elibrary_get_journal(journal_id=8610)" in text_of(r)
    r = await mcp.call_tool("elibrary_get_publication", {"identifier": "author:3771"})
    assert r.is_error is True and "elibrary_get_author(author_id=3771)" in text_of(r)
    r = await mcp.call_tool("elibrary_get_publication", {"identifier": "0000-0002-1984-3580"})  # ORCID
    assert r.is_error is True and "указывает на автора" in text_of(r) and "elibrary_get_author" in text_of(r)
    assert site.requests == []


async def test_get_author_and_publications(mcp, site):
    site.page("author_profile.asp", "author_profile_3771")
    site.page("author_items.asp", "author_items_3771")
    r = await mcp.call_tool("elibrary_get_author", {"author_id": 3771})
    assert r.is_error is False
    assert r.structured_content["spin"] == "9846-9426"
    assert r.structured_content["citations_rinc"] == 86835
    r = await mcp.call_tool("elibrary_get_author_publications", {"author_id": 3771})
    assert r.structured_content["total"] == 1094
    assert r.structured_content["items"][0]["id"] == 91809593


async def test_get_organization_and_journal(mcp, site):
    site.page("org_profile.asp", "org_profile_2541")
    site.page("title_about_new.asp", "title_about_8610")
    r = await mcp.call_tool("elibrary_get_organization", {"org_id": 2541})
    assert r.structured_content["h_index"] == 496 and r.structured_content["town"] == "Москва"
    r = await mcp.call_tool("elibrary_get_journal", {"journal_id": 8610})
    assert r.structured_content["issn"] == "1560-7534"
    assert r.structured_content["issues_per_year"] == 6


async def test_get_organization_publications_params(mcp, site):
    site.page("org_items.asp", "author_items_3771")
    await mcp.call_tool("elibrary_get_organization_publications",
                        {"org_id": 2541, "page": 3, "years": [2024], "show": "core", "sort": "cited"})
    p = params_of(site.requests[0])
    assert p == {"orgsid": "2541", "pagenum": "3", "show_option": "2", "pubyear": "2024", "sortorder": "3"}


async def test_get_journal_issue(mcp, site):
    site.page("contents.asp", "contents_8610")
    r = await mcp.call_tool("elibrary_get_journal_issue", {"journal_id": 8610})
    assert r.is_error is False
    data = r.structured_content
    assert data["issue_id"] == 91982811 and data["year"] == 2026 and len(data["items"]) == 10
    assert data["items"][0]["section"] == "МАТЕМАТИЧЕСКОЕ МОДЕЛИРОВАНИЕ"
    r = await mcp.call_tool("elibrary_get_journal_issue", {})
    assert r.is_error is True
    assert "journal_id" in text_of(r)


async def test_resolve_identifier(mcp, site):
    site.add(lambda r: DOI in str(r.url),
             lambda r: httpx2.Response(302, headers={"Location": "/item.asp?id=46371463"}))
    r = await mcp.call_tool("elibrary_resolve_identifier", {"identifier": "author:3771"})
    assert r.structured_content == {"kind": "elibrary_id", "value": "3771", "target": "author", "id": 3771,
                                    "url": "https://elibrary.ru/author_profile.asp?id=3771", "edn": None,
                                    "location": None, "from_cache": False}
    assert site.requests == []
    r = await mcp.call_tool("elibrary_resolve_identifier", {"identifier": DOI})
    assert r.structured_content["target"] == "publication" and r.structured_content["id"] == 46371463
    assert r.structured_content["kind"] == "doi"
    r = await mcp.call_tool("elibrary_resolve_identifier", {"identifier": "???"})
    assert r.is_error is True and "Не удалось распознать" in text_of(r)


# ============================================================================ captcha and session


async def test_captcha_is_tool_error_with_instructions(mcp, site):
    site.captcha_for("query_results.asp")
    r = await mcp.call_tool("elibrary_search_articles", {"query": "нейронные сети"})
    assert r.is_error is True
    text = text_of(r)
    assert "CAPTCHA" in text and "Never try to solve" in text
    assert "elibrary auth captcha" in text and "elibrary_set_cookies" in text
    assert "https://elibrary.ru/page_captcha.asp?rpage=" in text
    assert "Тест Тьюринга" in text  # the Russian help text follows
    sent = len(site.requests)
    # the breaker is open: no request until the user passes the check
    r = await mcp.call_tool("elibrary_get_author", {"author_id": 3771})
    assert r.is_error is True and "CAPTCHA" in text_of(r)
    assert len(site.requests) == sent
    status = (await mcp.call_tool("elibrary_session_status", {})).structured_content
    assert status["breaker_open"] is True and status["breaker_gate"] == "captcha"
    assert "page_captcha.asp" in status["breaker_url"]
    # cookies from the user's browser close the breaker
    r = await mcp.call_tool("elibrary_set_cookies", {"cookies": "SUserID=passed123; SCookieGUID=guid"})
    assert r.is_error is False
    assert r.structured_content["has_session_cookie"] is True
    assert "passed123" not in text_of(r)
    status = (await mcp.call_tool("elibrary_session_status", {})).structured_content
    assert status["breaker_open"] is False and status["has_session_cookie"] is True


async def test_session_status_without_request(mcp, site):
    r = await mcp.call_tool("elibrary_session_status", {})
    assert r.is_error is False
    data = r.structured_content
    assert data["has_session_cookie"] is False and data["logged_in"] is False
    assert data["max_per_hour"] == 60 and data["requests_last_hour"] == 0
    assert "state_db" not in data["extra"]
    assert site.requests == []


async def test_session_check(mcp, site):
    site.add(lambda r: r.url.path == "/defaultx.asp",
             lambda r: httpx2.Response(200, content=fixture_bytes("querybox_form"), headers=HTML))
    r = await mcp.call_tool("elibrary_session_check", {})
    assert r.is_error is False
    assert r.structured_content["state"] == "anonymous" and r.structured_content["outcome"] == "checked"


async def test_set_cookies_warnings(mcp, http):
    r = await mcp.call_tool("elibrary_set_cookies", {"cookies": "SCookieGUID=only", "user_agent": "Mozilla/5.0 X"})
    data = r.structured_content
    assert data["has_session_cookie"] is False and data["user_agent_saved"] is True
    assert any("SUserID" in w for w in data["warnings"])
    assert "без SUserID" in data["message"]
    assert http.state.get("user_agent") == "Mozilla/5.0 X"
    r = await mcp.call_tool("elibrary_set_cookies", {"cookies": "   ;  "})
    assert r.is_error is True


# ============================================================================ full text, export, zotero


async def test_get_fulltext_paging(mcp, monkeypatch):
    text = "Абзац первый.\n\n" + "Б" * 3000
    calls = []

    def fake(self, item, *, offset=0, max_chars=20000, sources=None, refresh=False):
        calls.append((item, offset, max_chars, sources, refresh))
        chunk = text[offset:offset + max_chars]
        nxt = offset + len(chunk) if offset + len(chunk) < len(text) else None
        return FullText(text=chunk, offset=offset, next_offset=nxt, total_chars=len(text), truncated=nxt is not None,
                        source="openalex", format="pdf", pdf_path=None)

    monkeypatch.setattr(ElibraryClient, "get_fulltext", fake)
    r = await mcp.call_tool("elibrary_get_fulltext", {"identifier": "46371463", "max_chars": 1000,
                                                      "sources": ["openalex"]})
    assert r.is_error is False
    data = r.structured_content
    assert data["next_offset"] == 1000 and data["truncated"] is True and data["total_chars"] == len(text)
    r = await mcp.call_tool("elibrary_get_fulltext", {"identifier": "46371463", "offset": 3000, "max_chars": 1000})
    assert r.structured_content["next_offset"] is None
    assert calls == [("46371463", 0, 1000, ["openalex"], False), ("46371463", 3000, 1000, None, False)]


async def test_get_fulltext_not_found(mcp, monkeypatch):
    from elibrary_cli.errors import NotFound

    def missing(self, *a, **k):
        raise NotFound("Полный текст не найден ни в одном источнике. crossref: нет PDF")

    monkeypatch.setattr(ElibraryClient, "get_fulltext", missing)
    r = await mcp.call_tool("elibrary_get_fulltext", {"identifier": "46371463"})
    assert r.is_error is True and "Полный текст не найден" in text_of(r)


async def test_export_citation_csl_and_bibtex(mcp, site):
    site.page("item.asp", "item_article_46371463")
    r = await mcp.call_tool("elibrary_export_citation", {"identifiers": ["46371463"]})
    assert r.is_error is False
    data = r.structured_content
    assert data["format"] == "csl_json" and data["count"] == 1 and data["zotero_mcp_source_type"] == "csl_json"
    csl = json.loads(data["text"])
    assert csl[0]["type"] == "article-journal" and csl[0]["DOI"] == DOI
    r = await mcp.call_tool("elibrary_export_citation", {"identifiers": ["FYOAKL"], "format": "bibtex"})
    assert r.structured_content["text"].startswith("@article{blokhina2021dlitelnoe,")
    assert r.structured_content["zotero_mcp_source_type"] == "bibtex"
    r = await mcp.call_tool("elibrary_export_citation", {"identifiers": ["46371463"], "format": "ris"})
    assert r.structured_content["text"].startswith("TY  - JOUR")
    assert r.structured_content["zotero_mcp_source_type"] is None
    assert len(site.requests) == 1


async def test_export_citation_csl_is_zotero_mcp_friendly_by_default(mcp, site):
    site.page("id=76098153", "item_chapter_76098153")
    r = await mcp.call_tool("elibrary_export_citation", {"identifiers": ["76098153"]})
    assert r.is_error is False
    friendly = json.loads(r.structured_content["text"])[0]
    assert r.structured_content["zotero_mcp_source_type"] == "csl_json"
    # only keys zotero-mcp's zotero_add_item maps; the rest goes into the note (Zotero "extra")
    assert "source" not in friendly and "container-author" not in friendly
    assert "container-author: Мифтахова || Р. Г." in friendly["note"]
    assert friendly["type"] == "chapter" and friendly["title"]
    r = await mcp.call_tool("elibrary_export_citation", {"identifiers": ["76098153"], "plain_csl": True})
    plain = json.loads(r.structured_content["text"])[0]
    assert plain["source"] and plain["container-author"] == [{"family": "Мифтахова", "given": "Р. Г."}]
    assert "container-author:" not in plain["note"]
    assert len(site.requests) == 1


async def test_export_citation_needs_identifiers(mcp):
    r = await mcp.call_tool("elibrary_export_citation", {"identifiers": []})
    assert r.is_error is True


async def test_zotero_add(mcp, site, zotero):
    site.page("item.asp", "item_article_46371463")
    r = await mcp.call_tool("elibrary_zotero_add", {"identifiers": ["46371463"], "collection": "РИНЦ",
                                                    "tags": ["elibrary"]})
    assert r.is_error is False
    data = r.structured_content
    assert data["created"] == 1 and data["failed"] == 0
    assert data["items"][0]["key"] == "ABCD1234" and data["items"][0]["elibrary_id"] == 46371463
    assert zotero.calls[0]["collection"] == "РИНЦ" and zotero.calls[0]["tags"] == ["elibrary"]
    assert zotero.closed == 1  # the probe writer is closed (the client's own writer is the same fake here)


async def test_zotero_add_unreachable(mcp, site, monkeypatch):
    down = FakeZotero(running=False)
    monkeypatch.setattr(mcp_server, "zotero_factory", lambda: down)
    r = await mcp.call_tool("elibrary_zotero_add", {"identifiers": ["46371463"]})
    assert r.is_error is True
    text = text_of(r)
    assert "Zotero is not reachable" in text and "zotero_add_item" in text and "csl_json" in text
    assert "Zotero не запущен" in text
    assert site.requests == []  # nothing fetched from elibrary.ru


# ============================================================================ reference data


async def test_reference_data(mcp, site):
    r = await mcp.call_tool("elibrary_reference_data", {})
    lists = {x["name"]: x for x in r.structured_content["lists"]}
    assert "orgs.regionid" in lists and "regions" in lists["orgs.regionid"]["aliases"]
    r = await mcp.call_tool("elibrary_reference_data", {"name": "regions", "find": "Новосиб"})
    data = r.structured_content
    assert data["name"] == "orgs.regionid"
    assert data["options"] == [{"value": "51", "label": "Новосибирская область", "count": 333,
                                "site_code": None, "form_field": None}]
    r = await mcp.call_tool("elibrary_reference_data", {"name": "vak"})
    assert {"value": "k1", "site_code": "2"}.items() <= r.structured_content["options"][1].items()
    r = await mcp.call_tool("elibrary_reference_data", {"name": "nonsense"})
    assert r.is_error is True and "Неизвестный справочник" in text_of(r)
    assert site.requests == []


# ============================================================================ lifecycle and entry point


def test_shared_client_is_created_once(monkeypatch):
    mcp_server.set_client(None)
    made = []

    class Dummy:
        closed = 0

        def close(self):
            Dummy.closed += 1

    def factory():
        made.append(Dummy())
        return made[-1]

    monkeypatch.setattr(mcp_server, "client_factory", factory)
    got: list = []
    threads = [threading.Thread(target=lambda: got.append(mcp_server.get_client())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 1 and all(g is made[0] for g in got)
    mcp_server.close_client()
    assert Dummy.closed == 1
    # an installed client is never closed by the server
    installed = Dummy()
    mcp_server.set_client(installed)  # type: ignore[arg-type]
    assert mcp_server.get_client() is installed
    mcp_server.set_client(None)
    assert Dummy.closed == 1


def test_configure_sets_settings_for_the_factory(monkeypatch, settings):
    mcp_server.set_client(None)
    seen = []
    monkeypatch.setattr(mcp_server, "client_factory", lambda: seen.append(mcp_server._settings) or object())
    mcp_server.configure(settings)
    mcp_server.get_client()
    assert seen == [settings]
    mcp_server.set_client(None)
    mcp_server.configure(None)


def test_main_parses_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **k: calls.append((a, k)))
    mcp_server.main([])
    mcp_server.main(["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9001"])
    assert calls == [((), {}), (("streamable-http",), {"host": "0.0.0.0", "port": 9001})]
    with pytest.raises(ValueError, match="Неизвестный транспорт"):
        mcp_server.run("sse")


# ============================================================================ review regressions (core safety)


async def test_session_tool_annotations_are_accurate(mcp):
    """#21: session_check sends a request and changes local state; set_cookies replaces the stored session."""
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    check = tools["elibrary_session_check"].annotations
    assert check.read_only_hint is False and check.idempotent_hint is False and check.destructive_hint is False
    assert tools["elibrary_set_cookies"].annotations.destructive_hint is True


def test_instructions_have_a_ban_rule():
    text = mcp_server.INSTRUCTIONS
    assert "ip_blocked" in text and "support@elibrary.ru" in text and "never try to lift" in text


async def test_agent_cannot_lift_a_captcha_pause_by_polling_session_check(mcp, site):
    """#21: after a captcha, elibrary_session_check fails without any request until the user imports
    cookies with SUserID; junk cookies change nothing."""
    site.captcha_for("query_results.asp")
    site.add(lambda r: r.url.path == "/defaultx.asp",
             lambda r: httpx2.Response(200, content=fixture_bytes("querybox_form"), headers=HTML))
    r = await mcp.call_tool("elibrary_search_articles", {"query": "нейронные сети"})
    assert r.is_error is True and "CAPTCHA" in text_of(r)
    sent = len(site.requests)
    for _ in range(3):
        r = await mcp.call_tool("elibrary_session_check", {})
        assert r.is_error is True and "CAPTCHA" in text_of(r)
    r = await mcp.call_tool("elibrary_set_cookies", {"cookies": "a=b"})
    assert r.structured_content["breaker_open"] is True and "пауза запросов не снята" in r.structured_content["message"]
    r = await mcp.call_tool("elibrary_session_check", {})
    assert r.is_error is True
    assert len(site.requests) == sent
    r = await mcp.call_tool("elibrary_set_cookies", {"cookies": "SUserID=passed; SCookieGUID=g"})
    assert r.structured_content["breaker_open"] is False and r.structured_content["session_cookie_imported"] is True
    r = await mcp.call_tool("elibrary_session_check", {})
    assert r.is_error is False and len(site.requests) == sent + 1


async def test_agent_cannot_lift_a_ban_pause(mcp, site, http):
    """#14/#21: after ip_blocked neither MCP tool lifts the 24 h pause, and the error text gives the ban
    rule instead of pointing at `elibrary auth check`."""
    site.add(lambda r: True, lambda r: httpx2.Response(
        200, content='<div id="blockedip">Из-за нарушения правил пользования сайтом eLIBRARY.RU</div>'.encode(),
        headers=HTML))
    r = await mcp.call_tool("elibrary_get_publication", {"identifier": "46371463"})
    assert r.is_error is True and "support@elibrary.ru" in text_of(r)
    sent = len(site.requests)
    r = await mcp.call_tool("elibrary_set_cookies", {"cookies": "SUserID=x; SCookieGUID=y"})
    assert r.structured_content["breaker_open"] is True and r.structured_content["breaker_gate"] == "ip_blocked"
    assert "заблокирован" in r.structured_content["message"]
    r = await mcp.call_tool("elibrary_session_check", {})
    assert r.is_error is True and "auth check" not in text_of(r) and "support@elibrary.ru" in text_of(r)
    r = await mcp.call_tool("elibrary_get_publication", {"identifier": "46371463"})
    assert r.is_error is True and "auth check" not in text_of(r)
    assert len(site.requests) == sent
    status = (await mcp.call_tool("elibrary_session_status", {})).structured_content
    assert status["breaker_open"] is True and status["breaker_gate"] == "ip_blocked"
