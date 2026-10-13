"""Tests of the client facade (``client.py``), the reference data (``refdata.py``) and the package
re-exports. All traffic goes to the conftest ``FakeSite`` (saved elibrary.ru pages); nothing is sent
to elibrary.ru, Zotero or any open-access API."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx2
import pytest

from elibrary_cli import exports, refdata
from elibrary_cli.client import ElibraryClient, WrongTarget, elibrary_target, parse_identifier
from elibrary_cli.errors import (
    CaptchaRequired,
    ElibraryError,
    LoginRequired,
    NotFound,
    OfflineCacheMiss,
    ParseError,
)
from elibrary_cli.fulltext.net import ExternalHTTP
from elibrary_cli.http import ElibraryHTTP
from elibrary_cli.models import (
    ALL_TYPES,
    AuthorProfile,
    AuthorPublicationsPage,
    IssueContents,
    JournalProfile,
    OrgProfile,
    OrgPublicationsPage,
    Publication,
    PublicationHit,
    PublicationQuery,
    SearchPage,
)
from elibrary_cli.queries.publications import search_signature

from .conftest import FakeSite, fixture_bytes

HTML = {"Content-Type": "text/html; Charset=UTF-8"}
DOI = "10.47056/0365-9615-2021-172-7-98-101"
ALL_TOTAL, PATENT_TOTAL = 290705, 27920
EMPTY_RESULTS = "<html><body><b>РЕЗУЛЬТАТЫ ПОИСКОВОГО ЗАПРОСА</b> Ничего не найдено</body></html>".encode()


# ============================================================================ helpers


def html(name: str | bytes, status: int = 200) -> httpx2.Response:
    body = fixture_bytes(name) if isinstance(name, str) else name
    return httpx2.Response(status, content=body, headers=HTML)


def redirect(location: str, status: int = 302) -> httpx2.Response:
    return httpx2.Response(status, headers={"Location": location}, content=b"<html>Object moved</html>")


def params_of(req: httpx2.Request) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(str(req.url)).query, keep_blank_values=True)


def body_of(req: httpx2.Request) -> list[tuple[str, str]]:
    return parse_qsl(req.content.decode("ascii"), keep_blank_values=True)


def page_of(fixture: str, n: int) -> bytes:
    """A results page ``n`` (2..10) made from page 1 of a saved result: only the paginator changes."""
    raw = fixture_bytes(fixture)
    cur = b"<td width=5% bgcolor=#5c6aa6 nowrap><font color=#ffffff>1</font></td>"
    one = b'<td width=5% bgcolor=#555555 nowrap><a href="query_results.asp?pagenum=1">1</a></td>'
    link = f'<td width=5% bgcolor=#555555 nowrap><a href="query_results.asp?pagenum={n}">{n}</a></td>'.encode()
    hl = f"<td width=5% bgcolor=#5c6aa6 nowrap><font color=#ffffff>{n}</font></td>".encode()
    assert cur in raw and link in raw
    return raw.replace(cur, one).replace(link, hl)


class SearchServer:
    """Fake query_results.asp that keeps the session's current query like the real server:
    ``changed=1`` runs a query (page 1), ``pagenum=N`` shows page N of the current one."""

    FIXTURES = {"all": "query_results_all_types", "patents": "query_results_patents"}

    def __init__(self) -> None:
        self.current: str | None = None
        self.lock = threading.Lock()

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        params = dict(params_of(request))
        with self.lock:
            if params.get("changed") == "1":
                types = {k for k in params if k.startswith("type_")}
                self.current = "patents" if types == {"type_patent"} else "all"
                return html(self.FIXTURES[self.current])
            n = int(params["pagenum"])
            if self.current is None:  # the session holds no query (expired / new session)
                return html(EMPTY_RESULTS)
            fixture = self.FIXTURES[self.current]
            return html(fixture if n == 1 else page_of(fixture, n))


def search_requests(site: FakeSite) -> list[str]:
    """'run' for a query run (changed=1), 'p<N>' for a pagenum request, in order."""
    out = []
    for r in site.requests:
        if "query_results.asp" not in str(r.url):
            continue
        p = dict(params_of(r))
        out.append("run" if p.get("changed") == "1" else f"p{p['pagenum']}")
    return out


@pytest.fixture
def server(site: FakeSite) -> SearchServer:
    srv = SearchServer()
    site.add(lambda r: "query_results.asp" in str(r.url), srv)
    return srv


def _no_external(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - must not be called
    raise AssertionError(f"unexpected external request {request.url}")


@pytest.fixture
def client(http: ElibraryHTTP, settings):
    net = ExternalHTTP(settings, transport=httpx2.MockTransport(_no_external), cache=False)
    c = ElibraryClient(http=http, net=net)
    yield c
    c.close()
    net.close()


def item_route(site: FakeSite) -> FakeSite:
    """item.asp?id=46371463 and item.asp?edn=FYOAKL -> the article card."""
    return site.add(lambda r: "item.asp" in str(r.url) and ("46371463" in str(r.url) or "FYOAKL" in str(r.url)),
                    lambda r: html("item_article_46371463"))


# ============================================================================ identifiers


@pytest.mark.parametrize(("text", "kind", "value", "target", "ident", "path"), [
    ("46371463", "elibrary_id", "46371463", "publication", 46371463, None),
    ("id:46371463", "elibrary_id", "46371463", "publication", 46371463, None),
    ("author:3771", "elibrary_id", "3771", "author", 3771, None),
    ("org 2541", "elibrary_id", "2541", "organization", 2541, None),
    ("journal:8610", "elibrary_id", "8610", "journal", 8610, None),
    ("issue:91982811", "elibrary_id", "91982811", "issue", 91982811, None),
    ("FYOAKL", "edn", "FYOAKL", "publication", None, None),
    ("fyoakl", "edn", "FYOAKL", "publication", None, None),
    ("EDN: FYOAKL", "edn", "FYOAKL", "publication", None, None),
    (DOI, "doi", DOI, None, None, "/" + DOI),
    ("doi:10.1000/ABC(1)", "doi", "10.1000/abc(1)", None, None, "/10.1000/abc(1)"),
    ("https://doi.org/10.1000/ABC", "doi", "10.1000/abc", None, None, "/10.1000/abc"),
    ("10.1000/a b#c", "doi", None, None, None, None),  # not a DOI (space) -> error, see below
    ("978-5-7996-1352-5", "isbn", "9785799613525", None, None, "/9785799613525"),
    ("ISBN 978-5-7996-1352-5", "isbn", "9785799613525", None, None, "/9785799613525"),
    ("0-306-40615-2", "isbn", "0306406152", None, None, "/0306406152"),
    ("0000-0002-4244-4270", "orcid", "0000-0002-4244-4270", None, None, "/0000-0002-4244-4270"),
    ("https://orcid.org/0000-0002-4244-427x", "orcid", "0000-0002-4244-427X", None, None, "/0000-0002-4244-427X"),
    ("1560-7534", "issn", "1560-7534", None, None, "/1560-7534"),
    ("2313-691x", "issn", "2313-691X", None, None, "/2313-691X"),
    ("9846-9426", "spin", "98469426", None, None, "/98469426"),
    ("spin:1560-7534", "spin", "15607534", None, None, "/15607534"),
    ("05dkdaa55", "ror", "05dkdaa55", None, None, "/05dkdaa55"),
    ("https://ror.org/05DKDAA55", "ror", "05dkdaa55", None, None, "/05dkdaa55"),
])
def test_parse_identifier_kinds(text, kind, value, target, ident, path):
    if value is None:
        with pytest.raises(ValueError):
            parse_identifier(text)
        return
    r = parse_identifier(text)
    assert (r["kind"], r["value"], r["target"], r["id"], r["path"]) == (kind, value, target, ident, path)
    if path:
        assert r["url"] == "https://elibrary.ru" + path


@pytest.mark.parametrize(("url", "target", "ident", "canonical"), [
    ("https://elibrary.ru/item.asp?id=46371463", "publication", 46371463, "https://elibrary.ru/item.asp?id=46371463"),
    ("https://www.elibrary.ru/author_profile.asp?id=3771", "author", 3771, "https://elibrary.ru/author_profile.asp?id=3771"),
    ("https://elibrary.ru/author_profile.asp?authorid=3771", "author", 3771, None),
    ("https://elibrary.ru/author_items.asp?authorid=3771&show_refs=1", "author", 3771, None),
    ("elibrary.ru/org_profile.asp?id=2541", "organization", 2541, "https://elibrary.ru/org_profile.asp?id=2541"),
    ("https://elibrary.ru/org_about.asp?orgsid=2541", "organization", 2541, None),
    ("https://elibrary.ru/org_items.asp?orgsid=2541", "organization", 2541, None),
    ("https://elibrary.ru/title_about.asp?id=8610", "journal", 8610, "https://elibrary.ru/title_about_new.asp?id=8610"),
    ("https://elibrary.ru/title_about_new.asp?id=8610&langid=2", "journal", 8610, None),
    ("https://elibrary.ru/title_profile.asp?id=8610", "journal", 8610, None),
    ("https://elibrary.ru/contents.asp?titleid=8610", "journal", 8610, None),
    ("https://elibrary.ru/contents.asp?id=91982811", "issue", 91982811, "https://elibrary.ru/contents.asp?id=91982811"),
])
def test_parse_identifier_elibrary_urls(url, target, ident, canonical):
    r = parse_identifier(url)
    assert (r["kind"], r["target"], r["id"], r["path"]) == ("url", target, ident, None)
    if canonical:
        assert r["url"] == canonical


def test_parse_identifier_short_urls_follow_site_semantics():
    assert parse_identifier("https://elibrary.ru/fyoakl")["kind"] == "edn"
    assert parse_identifier("https://elibrary.ru/item.asp?edn=fyoakl")["value"] == "FYOAKL"
    assert parse_identifier("https://elibrary.ru/67859959")["kind"] == "spin"  # digits = SPIN on the site
    assert parse_identifier("https://elibrary.ru/9785799613525")["kind"] == "isbn"
    assert parse_identifier("https://elibrary.ru/10.3748/wjg.v20.i38.13842")["value"] == "10.3748/wjg.v20.i38.13842"
    assert parse_identifier("https://elibrary.ru/1997-7298")["kind"] == "issn"


@pytest.mark.parametrize("bad", ["", "   ", "hello world", "https://google.com/x", "id:abc", "doi:abc",
                                 "https://elibrary.ru/querybox.asp", "ror:12", "isbn:978-5-7996-1352-0", "edn:AB"])
def test_parse_identifier_rejects(bad):
    with pytest.raises(ValueError):
        parse_identifier(bad)


def test_elibrary_target_ignores_other_hosts_and_pages():
    assert elibrary_target("https://doi.org/item.asp?id=1") is None
    assert elibrary_target("https://elibrary.ru/defaultx.asp") is None
    assert elibrary_target("https://elibrary.ru/item.asp?id=0") is None
    assert elibrary_target("/item.asp?id=5") == {"target": "publication", "id": 5,
                                                 "url": "https://elibrary.ru/item.asp?id=5"}


# ============================================================================ publication search


def test_search_page1_runs_query_once_and_caches(client, site, server, http):
    page = client.search_publications(text="нейронные сети")
    assert isinstance(page, SearchPage)
    assert (page.total, page.total_in_database, page.page, page.pages, len(page.items)) == (
        ALL_TOTAL, 82293065, 1, 2908, 100)
    assert page.items[0].id == 47430099 and page.items[0].kind == "patent"
    assert page.query["text"] == "нейронные сети" and not page.from_cache
    (req,) = site.requests
    assert req.method == "GET" and req.headers["Referer"] == "https://elibrary.ru/querybox.asp"
    params = params_of(req)
    assert ("changed", "1") in params and ("ftext", "нейронные сети") in params
    assert [k for k, _ in params][:7] == ["querybox_name", "authors_all", "titles_all", "rubrics_all", "changed",
                                          "queryid", "ftext"]
    sig = search_signature(PublicationQuery(text="нейронные сети"))
    assert http.state.get("query.current")["sig"] == sig == client.search_key(PublicationQuery(text="нейронные сети"))
    again = client.search_publications(PublicationQuery(text="нейронные сети"))
    assert again.from_cache and again.total == ALL_TOTAL and len(site.requests) == 1


def test_search_page2_runs_query_then_pagenum_and_caches(client, site, server):
    page2 = client.search_publications(text="нейронные сети", page=2)
    assert (page2.page, page2.total, page2.pages, len(page2.items)) == (2, ALL_TOTAL, 2908, 100)
    assert search_requests(site) == ["run", "p2"]
    run, p2 = site.requests
    assert params_of(p2) == [("pagenum", "2")]
    assert p2.headers["Referer"] == "https://elibrary.ru/query_results.asp"
    assert run.headers["Referer"] == "https://elibrary.ru/querybox.asp"
    # page 2 again and page 1 (stored by the run) come from the cache
    again = client.search_publications(text="нейронные сети", page=2)
    first = client.search_publications(text="нейронные сети")
    assert again.from_cache and again.page == 2 and first.from_cache and first.page == 1
    assert search_requests(site) == ["run", "p2"]


def test_search_next_page_reuses_the_session_query(client, site, server):
    client.search_publications(text="нейронные сети")
    p3 = client.search_publications(text="нейронные сети", page=3)
    assert p3.page == 3 and p3.total == ALL_TOTAL
    assert search_requests(site) == ["run", "p3"]


def test_search_different_query_reruns(client, site, server, http):
    client.search_publications(text="нейронные сети")
    patents = client.search_patents("нейронные сети", page=2)
    assert (patents.page, patents.total, patents.pages) == (2, PATENT_TOTAL, 280)
    assert patents.items[0].id == 88918718
    assert search_requests(site) == ["run", "run", "p2"]
    types = [k for k, _ in params_of(site.requests[1]) if k.startswith("type_")]
    assert types == ["type_patent"]
    assert http.state.get("query.current")["sig"] == search_signature(PublicationQuery(text="нейронные сети",
                                                                                        types=["patent"]))
    # back to the first query: page 1 is cached, but the session holds the patents -> re-run for page 2
    p2 = client.search_publications(text="нейронные сети", page=2)
    assert p2.total == ALL_TOTAL and p2.page == 2
    assert search_requests(site) == ["run", "run", "p2", "run", "p2"]


def test_search_inconsistent_page_triggers_one_rerun(client, site, server):
    client.search_publications(text="нейронные сети")
    server.current = "patents"  # another process / the browser ran a query in the same session
    p2 = client.search_publications(text="нейронные сети", page=2)
    assert p2.total == ALL_TOTAL and p2.page == 2
    assert search_requests(site) == ["run", "p2", "run", "p2"]


def test_search_lost_session_query_is_detected(client, site, server, http):
    # the client believes the session holds the query (run a minute ago, page 1 no longer cached), but the
    # server forgot it (session expired)
    http.state.set("query.current", {"sig": search_signature(PublicationQuery(text="нейронные сети")),
                                     "ts": time.time() - 60})
    p2 = client.search_publications(text="нейронные сети", page=2)
    assert (p2.page, p2.total, len(p2.items)) == (2, ALL_TOTAL, 100)
    assert search_requests(site) == ["p2", "run", "p2"]


def test_search_inconsistent_after_fresh_run_raises(client, site):
    def stubborn(request: httpx2.Request) -> httpx2.Response:
        if dict(params_of(request)).get("changed") == "1":
            return html("query_results_all_types")
        return html(page_of("query_results_patents", 2))  # always the wrong query

    site.add(lambda r: "query_results.asp" in str(r.url), stubborn)
    with pytest.raises(ElibraryError) as ei:
        client.search_publications(text="нейронные сети", page=2)
    assert "не ту страницу" in str(ei.value)
    assert search_requests(site) == ["run", "p2"]
    # the bad page is not cached
    with pytest.raises(ElibraryError):
        client.search_publications(text="нейронные сети", page=2)


def test_search_page_beyond_last_costs_nothing(client, site, server):
    client.search_publications(text="нейронные сети")
    beyond = client.search_publications(text="нейронные сети", page=5000)
    assert (beyond.items, beyond.page, beyond.pages, beyond.total) == ([], 5000, 2908, ALL_TOTAL)
    assert not beyond.has_next
    assert search_requests(site) == ["run"]


@pytest.mark.parametrize(("method", "types"), [
    ("search_articles", ["type_article"]),
    ("search_patents", ["type_patent"]),
    ("search_books", ["type_book"]),
    ("search_dissertations", ["type_disser"]),
    ("search_conference_papers", ["type_conf"]),
])
def test_search_wrappers_set_types(client, site, method, types):
    site.page("query_results.asp", "query_results_patents")
    page = getattr(client, method)("нейронные сети", year_from=2020)
    assert page.total == PATENT_TOTAL
    params = params_of(site.requests[0])
    assert [k for k, _ in params if k.startswith("type_")] == types
    assert ("begin_year", "2020") in params and ("ftext", "нейронные сети") in params


def test_search_wrapper_types_can_be_overridden(client, site):
    site.page("query_results.asp", "query_results_patents")
    client.search_books("нейронные сети", types=["book", "conf"])
    assert [k for k, _ in params_of(site.requests[0]) if k.startswith("type_")] == ["type_book", "type_conf"]


def test_search_validation_happens_before_any_request(client, site):
    with pytest.raises(ValueError):
        client.search_publications(text="x")
    with pytest.raises(ValueError):
        client.search_publications()
    with pytest.raises(ElibraryError):  # nothing to search in yet
        client.search_publications(text="сети", in_previous_results=True)
    assert site.requests == []


def test_search_in_previous_results_depends_on_session_query(client, site, server, http):
    client.search_publications(text="нейронные сети")
    sig_prev = http.state.get("query.current")["sig"]
    q = PublicationQuery(text="патент", in_previous_results=True)
    key = client.search_key(q)
    assert key != search_signature(q)
    client.search_publications(q)
    assert ("search_results", "on") in params_of(site.requests[-1])
    assert http.state.get("query.current")["sig"] == key != sig_prev


def test_search_captcha_clears_session_query_and_trips_breaker(client, site, http):
    site.captcha_for("query_results.asp")
    with pytest.raises(CaptchaRequired):
        client.search_publications(text="нейронные сети")
    assert http.state.get("query.current") is None
    n = len(site.requests)
    with pytest.raises(CaptchaRequired):
        client.search_publications(text="нейронные сети", page=2)
    assert len(site.requests) == n  # the breaker stops traffic
    assert client.status()["breaker_open"] is True
    assert "page_captcha.asp" in client.status()["breaker_url"]


def test_search_offline_uses_cache_only(client, site, server, http):
    client.search_publications(text="нейронные сети")
    http.settings.offline = True
    try:
        assert client.search_publications(text="нейронные сети").from_cache
        with pytest.raises(OfflineCacheMiss):
            client.search_publications(text="нейронные сети", page=2)
    finally:
        http.settings.offline = False
    assert search_requests(site) == ["run"]


def test_concurrent_searches_do_not_interleave(client, site, server):
    results: dict[str, SearchPage] = {}
    errors: list[BaseException] = []

    def run(name: str, **kw):
        try:
            results[name] = client.search_publications(text="нейронные сети", page=2, **kw)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=("all",)),
               threading.Thread(target=run, args=("patents",), kwargs={"types": "patent"})]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert results["all"].total == ALL_TOTAL and results["patents"].total == PATENT_TOTAL
    seq = search_requests(site)
    assert seq == ["run", "p2", "run", "p2"]  # each flow ran as a block


# ============================================================================ publication cards


def test_get_publication_by_id_is_cached(client, site):
    item_route(site)
    pub = client.get_publication(46371463)
    assert isinstance(pub, Publication)
    assert (pub.id, pub.edn, pub.doi, pub.kind, pub.year) == (46371463, "FYOAKL", DOI, "article", 2021)
    assert pub.authors[0].name == "БЛОХИНА Т.М."
    assert client.get_publication("46371463").id == 46371463
    assert len(site.requests) == 1
    assert str(site.requests[0].url) == "https://elibrary.ru/item.asp?id=46371463"


def test_get_publication_by_edn_aliases_the_id_page(client, site):
    item_route(site)
    pub = client.get_publication(edn="fyoakl")
    assert pub.id == 46371463
    assert str(site.requests[0].url) == "https://elibrary.ru/item.asp?edn=FYOAKL"
    assert client.get_publication(46371463).edn == "FYOAKL"
    assert len(site.requests) == 1  # the id page came from the alias


def test_get_publication_by_doi_reads_location(client, site):
    site.add(lambda r: r.url.path.startswith("/10.47056/"), lambda r: redirect("/item.asp?id=46371463"))
    item_route(site)
    pub = client.get_publication(doi="https://doi.org/" + DOI.upper())
    assert pub.id == 46371463 and pub.doi == DOI
    assert [str(r.url) for r in site.requests] == [f"https://elibrary.ru/{DOI}",
                                                   "https://elibrary.ru/item.asp?id=46371463"]
    assert client.get_publication(doi=DOI).id == 46371463
    assert len(site.requests) == 2  # resolution and card cached


def test_get_publication_doi_not_in_elibrary(client, site):
    site.add(lambda r: r.url.path.startswith("/10.1000/"), lambda r: redirect("https://doi.org/10.1000/xyz"))
    with pytest.raises(NotFound) as ei:
        client.get_publication(doi="10.1000/xyz")
    assert "нет в eLIBRARY.RU" in str(ei.value) and "doi.org" in str(ei.value)
    assert all(r.url.host == "elibrary.ru" for r in site.requests)
    with pytest.raises(NotFound):  # the miss is cached too
        client.get_publication(doi="10.1000/xyz")
    assert len(site.requests) == 1


def test_get_publication_doi_redirect_to_captcha_trips_breaker(client, site, http):
    site.add(lambda r: r.url.path.startswith("/10.1000/"),
             lambda r: redirect("/page_captcha.asp?rpage=https://elibrary.ru/10.1000/xyz"))
    with pytest.raises(CaptchaRequired) as ei:
        client.get_publication(doi="10.1000/xyz")
    assert "page_captcha.asp" in ei.value.captcha_url
    assert http.status()["breaker_open"] is True
    with pytest.raises(CaptchaRequired):
        client.get_publication(46371463)
    assert len(site.requests) == 1


@pytest.mark.parametrize("kwargs", [{}, {"item_id": 1, "edn": "FYOAKL"}, {"edn": "FYOAKL", "doi": DOI}])
def test_get_publication_needs_exactly_one_identifier(client, site, kwargs):
    with pytest.raises(ValueError):
        client.get_publication(**kwargs)
    assert site.requests == []


def test_get_publication_bad_values(client, site):
    for kwargs in ({"item_id": "abc"}, {"item_id": -5}, {"edn": "toolongedn"}, {"doi": "not-a-doi"}):
        with pytest.raises(ValueError):
            client.get_publication(**kwargs)
    assert site.requests == []


@pytest.mark.parametrize("ident", [
    "46371463", 46371463, "id:46371463", "FYOAKL", "edn:fyoakl", "https://elibrary.ru/item.asp?id=46371463",
    "https://elibrary.ru/item.asp?edn=FYOAKL", "elibrary.ru/fyoakl", DOI, "doi:" + DOI.upper(),
    "https://doi.org/" + DOI,
])
def test_get_publication_any(client, site, ident):
    item_route(site)
    site.add(lambda r: r.url.path == "/" + DOI, lambda r: redirect("/item.asp?id=46371463"))
    pub = client.get_publication_any(ident)
    assert (pub.id, pub.edn, pub.doi) == (46371463, "FYOAKL", DOI)
    assert len(site.requests) <= 2  # the card (+ one non-followed DOI lookup)


def test_get_publication_any_isbn_and_unknown_doi(client, site):
    site.add(lambda r: r.url.path == "/9785799613525",
             lambda r: redirect("https://www.elibrary.ru/item.asp?id=76097861"))
    site.add(lambda r: r.url.path == "/item.asp" and "76097861" in str(r.url), lambda r: html("item_book_76097861"))
    assert client.get_publication_any("978-5-7996-1352-5").id == 76097861
    site.add(lambda r: r.url.path == "/10.1000/xyz", lambda r: redirect("https://doi.org/10.1000/xyz"))
    with pytest.raises(NotFound, match="DOI 10.1000/xyz"):
        client.get_publication_any("10.1000/xyz")


@pytest.mark.parametrize(("ident", "target", "target_id"), [
    ("https://elibrary.ru/author_profile.asp?id=3771", "author", 3771),
    ("author:3771", "author", 3771),
    ("https://elibrary.ru/title_about_new.asp?id=8610", "journal", 8610),
    ("https://elibrary.ru/contents.asp?id=1234", "issue", 1234),
    ("org:2541", "organization", 2541),
    ("spin:9846-9426", "author", None),
    ("0000-0002-1984-3580", "author", None),  # ORCID
    ("issn:1560-7534", "journal", None),
    ("ror:05dkdaa55", "organization", None),
])
def test_get_publication_any_wrong_target_costs_no_request(client, site, ident, target, target_id):
    with pytest.raises(WrongTarget) as ei:
        client.get_publication_any(ident)
    exc = ei.value
    assert isinstance(exc, ValueError)
    assert (exc.target, exc.target_id, exc.expected) == (target, target_id, "publication")
    assert "не на публикацию" in str(exc)
    assert site.requests == []


def test_get_publication_any_identifier_resolving_elsewhere(client, site):
    # an ISBN the site answers with an author profile (made-up redirect): no card request after it
    site.add(lambda r: r.url.path == "/9785799613525", lambda r: redirect("/author_profile.asp?id=3771"))
    with pytest.raises(WrongTarget, match="указывает на автора, а не на публикацию") as ei:
        client.get_publication_any("9785799613525")
    assert (ei.value.target, ei.value.target_id) == ("author", 3771)
    assert len(site.requests) == 1
    with pytest.raises(ValueError, match="Не удалось распознать"):
        client.get_publication_any("что-то странное")
    with pytest.raises(ValueError):
        client.get_publication_any(True)  # type: ignore[arg-type]


def test_unrecognised_page_is_not_kept_in_cache(client, site):
    site.page("item.asp", "authors_form")
    with pytest.raises(ParseError):
        client.get_publication(1)
    with pytest.raises(ParseError):
        client.get_publication(1)
    assert len(site.requests) == 2


def test_get_fulltext_flag(client, site):
    site.page("ispdf.asp", "ispdf_47430099")
    flag = client.get_fulltext_flag(47430099)
    assert (flag.item_id, flag.available, flag.colour, flag.access, flag.action) == (
        47430099, True, "green", "open", "load_article")
    assert str(site.requests[0].url) == "https://elibrary.ru/ispdf.asp?itemid=47430099"


# ============================================================================ resolve()


@pytest.mark.parametrize("ident", ["46371463", "https://elibrary.ru/author_profile.asp?id=3771", "org:2541",
                                   "https://elibrary.ru/contents.asp?titleid=8610"])
def test_resolve_local_costs_no_request(client, site, ident):
    r = client.resolve(ident)
    assert r["target"] in ("publication", "author", "organization", "journal") and r["id"]
    assert r["url"].startswith("https://elibrary.ru/") and site.requests == []


def test_resolve_edn_fetches_the_card_once(client, site):
    item_route(site)
    r = client.resolve("FYOAKL")
    assert r == {"kind": "edn", "value": "FYOAKL", "target": "publication", "id": 46371463,
                 "url": "https://elibrary.ru/item.asp?id=46371463", "from_cache": False}
    again = client.resolve("edn:fyoakl")
    assert again["id"] == 46371463 and again["from_cache"] is True
    assert client.get_publication(46371463).edn == "FYOAKL"
    assert len(site.requests) == 1


@pytest.mark.parametrize(("ident", "path", "location", "kind", "target", "target_id"), [
    (DOI, "/" + DOI, "/item.asp?id=46371463", "doi", "publication", 46371463),
    ("978-5-7996-1352-5", "/9785799613525", "https://www.elibrary.ru/item.asp?id=76097861", "isbn", "publication",
     76097861),
    ("9846-9426", "/98469426", "author_profile.asp?id=3771", "spin", "author", 3771),
    ("0000-0002-4244-4270", "/0000-0002-4244-4270", "/author_profile.asp?authorid=3771", "orcid", "author", 3771),
    ("1560-7534", "/1560-7534", "/title_about_new.asp?id=8610", "issn", "journal", 8610),
    ("1560-7534", "/1560-7534", "/author_profile.asp?id=3771", "spin", "author", 3771),  # no such ISSN: a SPIN
    ("05dkdaa55", "/05dkdaa55", "/org_profile.asp?id=2541", "ror", "organization", 2541),
])
def test_resolve_short_url_identifiers(client, site, ident, path, location, kind, target, target_id):
    site.add(lambda r: r.url.path == path, lambda r: redirect(location))
    r = client.resolve(ident)
    assert (r["kind"], r["target"], r["id"], r["from_cache"]) == (kind, target, target_id, False)
    assert r["url"].startswith("https://elibrary.ru/") and str(target_id) in r["url"]
    assert [r_.url.path for r_ in site.requests] == [path]
    cached = client.resolve(ident)
    assert cached["from_cache"] is True and cached["id"] == target_id
    assert len(site.requests) == 1


def test_resolve_orcid_unknown_falls_back_to_registry(client, site):
    site.add(lambda r: r.url.path == "/0000-0002-4244-4270",
             lambda r: redirect("https://orcid.org/0000-0002-4244-4270"))
    with pytest.raises(NotFound) as ei:
        client.resolve("https://orcid.org/0000-0002-4244-4270")
    assert "ORCID" in str(ei.value) and "orcid.org" in str(ei.value)
    assert len(site.requests) == 1


def test_resolve_sets_up_a_session_without_following_redirects(client, site):
    calls = {"n": 0}

    def short(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:  # fresh session: the site first sends us through start_session.asp
            return redirect("https://www.elibrary.ru/start_session.asp?rpage=https%3A%2F%2Felibrary.ru%2F05dkdaa55")
        return redirect("/org_profile.asp?id=2541")

    site.add(lambda r: r.url.path == "/05dkdaa55", short)
    site.add(lambda r: "start_session.asp" in str(r.url), lambda r: redirect("https://elibrary.ru/05dkdaa55"))
    r = client.resolve("05dkdaa55")
    assert (r["target"], r["id"]) == ("organization", 2541)
    assert [x.url.path for x in site.requests] == ["/05dkdaa55", "/start_session.asp", "/05dkdaa55"]


def test_resolve_redirect_to_home_page_is_not_found(client, site):
    site.add(lambda r: r.url.path == "/9785799613525", lambda r: redirect("/defaultx.asp"))
    with pytest.raises(NotFound) as ei:
        client.resolve("9785799613525")
    assert "ISBN" in str(ei.value)
    assert len(site.requests) == 1


def test_resolve_short_url_answered_with_the_card(client, site):
    site.add(lambda r: r.url.path.startswith("/10.47056/"), lambda r: html("item_article_46371463"))
    r = client.resolve(DOI)
    assert (r["target"], r["id"]) == ("publication", 46371463)
    assert client.get_publication(46371463).id == 46371463
    assert len(site.requests) == 1  # the card served by the short URL was stored under item.asp?id=


def test_resolve_unrecognised_identifier(client, site):
    with pytest.raises(ValueError):
        client.resolve("что-то непонятное")
    assert site.requests == []


# ============================================================================ authors


AUTHORS_LIVE_BODY = [
    ("authors_all", ""), ("pagenum", ""), ("authorbox_name", ""), ("selid", ""), ("orgid", ""), ("orgadminid", ""),
    ("townid", ""), ("regionid", ""), ("surname", "Петров"), ("codetype", "SPIN"), ("codevalue", ""),
    ("countryid", ""), ("town", ""), ("orgname", ""), ("authorboxid", ""), ("rubriccode", ""), ("metrics", "1"),
    ("sortorder", "2"), ("order", "1"),
]


def test_search_authors_posts_the_live_form_and_caches(client, site):
    site.page("authors.asp", "authors_results_petrov")
    page = client.search_authors(surname="Петров")
    assert (page.total, page.total_in_database, page.page, page.pages, len(page.items)) == (4342, 1170544, 1, 44, 100)
    assert page.items[0].id == 3771 and page.items[0].name == "Петров Владимир Алексеевич"
    (req,) = site.requests
    assert req.method == "POST" and req.headers["Referer"] == "https://elibrary.ru/authors.asp"
    assert req.content.decode("ascii") == urlencode(AUTHORS_LIVE_BODY)
    again = client.search_authors({"surname": "Петров"})
    assert again.from_cache and len(site.requests) == 1
    client.search_authors(surname="Петров", page=2)
    assert len(site.requests) == 2 and ("pagenum", "2") in body_of(site.requests[1])


def test_search_authors_validation(client, site):
    with pytest.raises(ValueError):
        client.search_authors(country="RUS")  # not a selective criterion
    assert site.requests == []


MGU_ORG_FORM = [("pagenum", ""), ("orgname", "московский государственный университет"), ("town", ""),
                ("regionid", "0"), ("countryid", ""), ("sortorder", "3"), ("order", "1")]


def test_search_authors_resolves_org_name_with_one_org_search(client, site):
    # live bug 2026-10-13: orgname without orgid is ignored by the site (all 8423 Ivanovs came back)
    site.page("orgs.asp", "orgs_results_mgu").page("authors.asp", "authors_results_petrov")
    page = client.search_authors(surname="Петров", org_name="московский государственный университет")
    orgs_req, authors_req = site.requests
    assert (orgs_req.method, orgs_req.url.path) == ("POST", "/orgs.asp")
    assert body_of(orgs_req) == MGU_ORG_FORM  # one organization search, by publications, descending
    body = dict(body_of(authors_req))
    assert body["orgid"] == "2541" and body["surname"] == "Петров"
    assert body["orgname"] == "Московский государственный университет им. М.В. Ломоносова"  # as the browser sends
    assert page.query["org_id"] == 2541
    assert page.query["org_resolved"] == {
        "id": 2541, "name": "Московский государственный университет им. М.В. Ломоносова", "town": "Москва",
        "publications": 429371, "match": "most_publications", "candidates": 23,
        "query": "московский государственный университет"}
    # both pages are cached: the same search again (and its page 2) costs at most the new author page
    again = client.search_authors({"surname": "Петров", "org_name": "московский государственный университет"})
    assert again.from_cache and again.query["org_resolved"]["id"] == 2541 and len(site.requests) == 2
    client.search_authors(surname="Петров", org_name="московский государственный университет", page=2)
    assert [r.url.path for r in site.requests] == ["/orgs.asp", "/authors.asp", "/authors.asp"]
    assert dict(body_of(site.requests[2]))["orgid"] == "2541"


@pytest.mark.parametrize(("org_name", "org_id"), [
    ("МИИТ", 1229),  # the abbreviation in parentheses: "Российский университет транспорта (МИИТ)"
    ("Московский педагогический государственный университет", 924),  # exact name, not the most published hit
    ("«Московский  политехнический университет»", 14985),  # quotes and spaces are ignored
    ("МГУ", 2541),  # no exact match: the most published hit
])
def test_search_authors_org_name_best_hit(client, site, org_name, org_id):
    site.page("orgs.asp", "orgs_results_mgu").page("authors.asp", "authors_results_petrov")
    page = client.search_authors(org_name=org_name)  # the org name alone is a criterion here
    assert page.query["org_resolved"]["id"] == org_id
    assert page.query["org_resolved"]["match"] == ("most_publications" if org_name == "МГУ" else "exact")
    assert dict(body_of(site.requests[1]))["orgid"] == str(org_id)


def test_search_authors_org_name_not_found(client, site):
    site.page("orgs.asp", "orgs_form")
    with pytest.raises(NotFound, match="Организация «Институт Атлантиды» не найдена"):
        client.search_authors(surname="Петров", org_name="Институт Атлантиды")
    assert [r.url.path for r in site.requests] == ["/orgs.asp"]


def test_search_authors_org_id_wins_and_validation_comes_first(client, site):
    site.page("authors.asp", "authors_results_petrov")
    page = client.search_authors(surname="Петров", org_id=169, org_name="ИФВЭ")
    (req,) = site.requests
    assert (dict(body_of(req))["orgid"], dict(body_of(req))["orgname"]) == ("169", "ИФВЭ")
    assert "org_resolved" not in page.query
    with pytest.raises(ValueError, match="неизвестная страна"):  # nothing is sent for an invalid query
        client.search_authors(surname="Петров", org_name="МГУ", country="Атлантида")
    with pytest.raises(ValueError, match="Укажите фамилию автора"):
        client.search_authors(country="Russia")
    assert len(site.requests) == 1


def test_search_authors_country_name(client, site):
    site.page("authors.asp", "authors_results_petrov")
    client.search_authors(surname="Петров", country="Kazakhstan")
    assert ("countryid", "KAZ") in body_of(site.requests[0])


def test_get_author(client, site):
    site.page("author_profile.asp", "author_profile_3771")
    prof = client.get_author(3771)
    assert isinstance(prof, AuthorProfile)
    assert (prof.id, prof.name, prof.spin, prof.publications_rinc, prof.hindex_rinc) == (
        3771, "Петров Владимир Алексеевич", "9846-9426", 1069, 119)
    assert str(site.requests[0].url) == "https://elibrary.ru/author_profile.asp?id=3771"


def test_get_author_publications_first_page_get_then_post(client, site):
    site.page("author_items.asp", "author_items_3771")
    first = client.get_author_publications(3771)
    assert isinstance(first, AuthorPublicationsPage)
    assert (first.author_id, first.author_short_name, first.total, first.pages, len(first.items)) == (
        3771, "Петров В А", 1094, 11, 99)
    assert first.items[0].id == 91809593 and len(first.references) == 1
    req = site.requests[0]
    assert req.method == "GET" and str(req.url) == "https://elibrary.ru/author_items.asp?authorid=3771&show_refs=1"
    client.get_author_publications(3771, page=2)
    post = site.requests[1]
    assert post.method == "POST"
    body = dict(body_of(post))
    assert (body["authorid"], body["authorhash"], body["pagenum"], body["sortorder"], body["order"]) == (
        "3771", "Петров В А", "2", "0", "1")
    assert body["check_show_refs"] == "on" and body["check_hide_doubles"] == "on"
    assert post.headers["Referer"] == "https://elibrary.ru/author_items.asp?authorid=3771&show_refs=1"
    client.get_author_publications(3771, page=2)
    assert len(site.requests) == 2  # cached


def test_get_author_publications_later_page_has_the_total(client, site):
    # live 2026-10-13: page 2 of AuthorID 17783 came back with total=None ("Всего найдена 2581 публикация")
    site.page("author_items.asp", "author_items_17783_p2")
    page = client.get_author_publications(17783, page=2)
    (req,) = site.requests
    assert req.method == "POST" and dict(body_of(req))["pagenum"] == "2"
    assert (page.total, page.page, page.pages, page.per_page, len(page.items)) == (2581, 2, 26, 100, 100)
    assert (page.author_id, page.total_citations, page.items[0].position) == (17783, 19381, 101)


def test_get_author_publications_sorted_first_page_is_a_post(client, site):
    site.page("author_items.asp", "author_items_3771")
    client.get_author_publications(3771, sort="citations", include_references=False)
    (req,) = site.requests
    body = dict(body_of(req))
    assert req.method == "POST" and body["sortorder"] == "3" and body["show_refs"] == "0"
    assert "check_show_refs" not in body and body["authorhash"] == ""


# ============================================================================ organizations


def test_search_organizations_live_form(client, site):
    site.page("orgs.asp", "orgs_results_mgu")
    page = client.search_organizations(name="московский государственный университет", sort="publications")
    assert (page.total, page.total_in_database, page.page, page.pages, page.per_page) == (23, 123983, 1, 2, 20)
    assert page.items[0].id == 2541
    assert page.items[0].name == "Московский государственный университет им. М.В. Ломоносова"
    (req,) = site.requests
    assert req.content.decode("ascii") == urlencode([
        ("pagenum", ""), ("orgname", "московский государственный университет"), ("town", ""), ("regionid", "0"),
        ("countryid", ""), ("sortorder", "3"), ("order", "1")])
    assert req.headers["Referer"] == "https://elibrary.ru/orgs.asp"


def test_get_organization_and_card_fallback(client, site):
    site.page("org_profile.asp", "org_profile_2541")
    site.page("org_about.asp", "authors_form")  # not a card: the profile is still returned
    prof = client.get_organization(2541)
    assert isinstance(prof, OrgProfile)
    assert (prof.id, prof.publications, prof.h_index, prof.town) == (2541, 502087, 496, "Москва")
    assert len(site.requests) == 1
    with_card = client.get_organization(2541, with_card=True)
    assert any("org_about.asp" in n for n in with_card.notes)
    assert [r.url.path for r in site.requests] == ["/org_profile.asp", "/org_about.asp"]


def test_get_organization_publications(client, site):
    site.page("org_items.asp", "author_items_3771")  # same list layout (no org_items fixture)
    page = client.get_organization_publications(2541, page=2, show_option=0)
    assert isinstance(page, OrgPublicationsPage)
    assert page.total == 1094 and len(page.items) == 99
    assert str(site.requests[0].url) == "https://elibrary.ru/org_items.asp?orgsid=2541&pagenum=2&show_option=0"


# ============================================================================ journals


def test_search_journals_live_form(client, site):
    site.page("titles.asp", "titles_results")
    page = client.search_journals(name="вычислительные технологии")
    assert [h.id for h in page.items] == [8610, 8746, 68559, 37522] and page.total == 4
    assert page.items[0].name == "Вычислительные технологии" and page.source_url
    (req,) = site.requests
    assert req.content.decode("ascii") == urlencode([
        ("titles_all", ""), ("pagenum", ""), ("titlebox_name", ""), ("titlename", "вычислительные технологии"),
        ("countryid", ""), ("rubriccode", ""), ("language", ""), ("translated", ""), ("wos", ""), ("scopus", ""),
        ("white", ""), ("vak", ""), ("risc", ""), ("ftopen", ""), ("sortorder", "1"), ("order", "1")])


def test_get_journal_and_indicator_fallback(client, site):
    site.page("title_about_new.asp", "title_about_8610")
    site.page("title_profile.asp", "authors_form")
    j = client.get_journal(8610)
    assert isinstance(j, JournalProfile)
    assert (j.id, j.name, j.issn, j.eissn) == (8610, "Вычислительные технологии", "1560-7534", "2313-691X")
    j2 = client.get_journal(8610, with_indicators=True)
    assert "не распознаны" in j2.extra["Показатели (title_profile.asp)"]
    assert [str(r.url) for r in site.requests] == ["https://elibrary.ru/title_about_new.asp?id=8610",
                                                   "https://elibrary.ru/title_profile.asp?id=8610"]
    client.get_journal(8610, english=True)
    assert str(site.requests[-1].url) == "https://elibrary.ru/title_about_new.asp?id=8610&langid=2"


def test_get_journal_issue(client, site):
    site.page("contents.asp", "contents_8610")
    latest = client.get_journal_issue(journal_id=8610)
    assert isinstance(latest, IssueContents)
    assert (latest.journal_id, latest.issue_id, latest.year, latest.edn, len(latest.items)) == (
        8610, 91982811, 2026, "MABYSK", 10)
    assert latest.items[0].id == 91982812
    client.get_journal_issue(issue_id=91982811)
    assert [str(r.url) for r in site.requests] == ["https://elibrary.ru/contents.asp?titleid=8610",
                                                   "https://elibrary.ru/contents.asp?id=91982811"]
    with pytest.raises(ValueError):
        client.get_journal_issue()


# ============================================================================ full text, export, Zotero


def test_get_fulltext_builds_the_request(client, site, monkeypatch):
    import elibrary_cli.fulltext as ft

    item_route(site)
    seen = {}

    def fake(req, **kw):
        seen["req"], seen["kw"] = req, kw
        return ft.FullText(text="Текст статьи", total_chars=12, truncated=False, source="crossref", format="html")

    monkeypatch.setattr(ft, "get_fulltext", fake)
    out = client.get_fulltext(46371463, offset=10, max_chars=500, sources=["crossref"])
    assert out.text == "Текст статьи"
    req = seen["req"]
    assert (req.elibrary_id, req.edn, req.doi, req.year) == (46371463, "FYOAKL", DOI, 2021)
    assert req.title.startswith("Длительное поддержание") and req.authors[0] == "БЛОХИНА Т.М."
    assert req.journal == "БЮЛЛЕТЕНЬ ЭКСПЕРИМЕНТАЛЬНОЙ БИОЛОГИИ И МЕДИЦИНЫ"
    kw = seen["kw"]
    assert kw["http"] is client.http and kw["net"] is client.net
    assert (kw["offset"], kw["max_chars"], kw["sources"]) == (10, 500, ["crossref"])
    assert len(site.requests) == 1


def test_get_fulltext_anonymous_elibrary_source_is_skipped(client, site):
    item_route(site)
    with pytest.raises(NotFound) as ei:
        client.get_fulltext(46371463, sources=["elibrary"])
    attempts = ei.value.attempts  # type: ignore[attr-defined]
    assert attempts[0]["source"] == "elibrary" and attempts[0]["status"] == "skipped"
    assert len(site.requests) == 1  # only the card; no PDF attempt without a signed-in session


def test_export_bibtex_csl_ris_zotero(client, site):
    item_route(site)
    bib = client.export([46371463], "bib")
    assert isinstance(bib, str) and bib.startswith("@article{blokhina2021dlitelnoe,")
    assert "FYOAKL" in bib and DOI in bib
    csl = client.export(46371463, "csl")
    assert isinstance(csl, list) and csl[0]["type"] == "article-journal" and csl[0]["DOI"] == DOI
    ris = client.export("FYOAKL", "ris")
    assert ris.startswith("TY  - JOUR") and "AN  - 46371463" in ris
    zot = client.export(["46371463"], "zotero", as_text=True)
    assert json.loads(zot)[0]["itemType"] == "journalArticle"
    assert len(site.requests) == 1  # one card for all four exports


def test_export_csl_zotero_mcp_variant(client, site):
    site.page("id=76098153", "item_chapter_76098153")
    plain = client.export(76098153, "csl")
    friendly = client.export(76098153, "csl", zotero_mcp=True)
    assert plain[0]["source"] == exports.LIBRARY_CATALOG and "container-author" in plain[0]
    assert "source" not in friendly[0] and "container-author" not in friendly[0]
    assert "container-author: Мифтахова || Р. Г." in friendly[0]["note"]
    assert {k: v for k, v in friendly[0].items() if k != "note"} == {
        k: v for k, v in plain[0].items() if k not in ("note", "source", "container-author")}
    # the flag only changes CSL
    assert client.export(76098153, "ris", zotero_mcp=True) == client.export(76098153, "ris")
    assert len(site.requests) == 1


def test_export_hits_without_card_requests(client, site, server):
    page = client.search_publications(text="нейронные сети")
    ris = client.export(page.items[:3], "ris")
    assert ris.count("TY  - ") == 3 and "TY  - PAT" in ris
    csl = client.export(page, "csl")
    assert len(csl) == 100 and csl[0]["type"] == "patent"
    assert search_requests(site) == ["run"] and len(site.requests) == 1


def test_search_empty_result(client, site):
    site.page("query_results.asp", "query_results_patents")  # replaced below for the run
    site.routes.insert(0, (lambda r: "query_results.asp" in str(r.url), lambda r: html(EMPTY_RESULTS)))
    empty = client.search_publications(text="абракадабра несуществующая")
    assert (empty.total, empty.items, empty.pages, empty.has_next) == (0, [], 0, False)
    beyond = client.search_publications(text="абракадабра несуществующая", page=2)
    assert beyond.items == [] and beyond.total == 0
    assert search_requests(site) == ["run"]


def test_export_rejects_bad_format_before_requests(client, site):
    with pytest.raises(ValueError):
        client.export([46371463], "docx")
    with pytest.raises(ValueError):
        client.export([], "bibtex")
    assert site.requests == []


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add(self, items, **kw):
        items = list(items)
        self.calls.append({"items": items, **kw})
        return [{"index": i, "title": p.title, "url": p.url, "key": f"KEY{i}", "backend": "local",
                 "status": "created", "message": None, "attachment": None, "collection": kw.get("collection")}
                for i, p in enumerate(items)]

    def close(self) -> None:  # pragma: no cover - injected writers are not closed by the client
        raise AssertionError("must not be closed")


def test_zotero_add_passes_publications(http, settings, site):
    item_route(site)
    writer = FakeWriter()
    with ElibraryClient(http=http, zotero=writer) as c:
        res = c.zotero_add(46371463, collection="eLIBRARY/Тест", tags=["elibrary"])
    (call,) = writer.calls
    assert isinstance(call["items"][0], Publication) and call["items"][0].id == 46371463
    assert (call["collection"], call["tags"], call["pdf_paths"]) == ("eLIBRARY/Тест", ["elibrary"], {})
    assert res[0]["elibrary_id"] == 46371463 and res[0]["status"] == "created" and res[0]["pdf_note"] is None


def test_zotero_add_with_pdf(http, settings, site, monkeypatch, tmp_path: Path):
    import elibrary_cli.fulltext as ft

    item_route(site)
    pdf = tmp_path / "elibrary_46371463.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(ft, "get_fulltext", lambda req, **kw: ft.FullText(
        text="x", total_chars=1, truncated=False, source="elibrary", format="pdf", pdf_path=str(pdf)))
    writer = FakeWriter()
    with ElibraryClient(http=http, zotero=writer) as c:
        c.zotero_add([46371463], with_pdf=True)

        def missing(req, **kw):
            raise NotFound("Полный текст не найден ни в одном источнике.")

        monkeypatch.setattr(ft, "get_fulltext", missing)
        res = c.zotero_add([46371463], with_pdf=True)
    assert writer.calls[0]["pdf_paths"] == {0: str(pdf)}
    assert writer.calls[1]["pdf_paths"] == {}
    assert res[0]["pdf_note"].startswith("PDF не найден")


# ============================================================================ session and cache


def test_status_and_cookies(client, site, http):
    st = client.status()
    assert st["logged_in"] is False and st["current_query"] is None and st["breaker_open"] is False
    http.state.set("query.current", "abc")
    info = client.set_cookies("SUserID=123; SCookieGUID=G; _ym_uid=5", user_agent="UA/1")
    assert info["cookie_names"] == ["SCookieGUID", "SUserID"] and info["ignored"] == ["_ym_uid"]
    assert "123" not in json.dumps(info)
    st = client.status()
    assert st["has_session_cookie"] is True and st["current_query"] is None
    cleared = client.clear_session()
    assert cleared["has_session_cookie"] is False and cleared["cookie_names"] == []
    assert site.requests == []


def test_login_without_credentials_sends_nothing(client, site):
    with pytest.raises(LoginRequired):
        client.login()
    assert site.requests == []


def test_check_session_and_logout(client, site, http):
    site.page("defaultx.asp", "item_article_46371463")
    site.page("end_session.asp", "item_article_46371463")
    info = client.check_session()
    assert info.state == "anonymous" and info.outcome == "checked"
    out = client.logout()
    assert out.outcome == "logged_out"
    assert [r.url.path for r in site.requests] == ["/defaultx.asp", "/end_session.asp"]
    assert site.requests[1].method == "POST" and site.requests[1].content == b"saveuser=1"


def test_cache_stats_and_clear(client, site, settings):
    item_route(site)
    client.get_publication(46371463)
    stats = client.cache_stats()
    # the card is stored under item.asp?id= and under its EDN (item.asp?edn=FYOAKL)
    assert stats["pages"] == 2 and stats["bytes"] > 1000 and stats["fulltext_texts"] == 0
    ft_dir = settings.cache_dir / "fulltext"
    ft_dir.mkdir(parents=True)
    (ft_dir / "elibrary-1.txt").write_text("текст", encoding="utf-8")
    (ft_dir / "elibrary-1.json").write_text("{}", encoding="utf-8")
    assert client.cache_stats()["fulltext_texts"] == 1
    out = client.clear_cache(fulltext=True, external=True)
    assert out == {"pages_removed": 2, "fulltext_removed": 1, "external_removed": 0}
    assert not any(ft_dir.iterdir())
    client.get_publication(46371463)
    assert len(site.requests) == 2


def test_client_owns_or_borrows_http(settings, site):
    http = ElibraryHTTP(settings, transport=httpx2.MockTransport(site))
    with ElibraryClient(http=http) as c:
        assert c.http is http and c.settings is settings and c.lock is http._lock
    item_route(site)
    assert http.get("item.asp", {"id": "46371463"}).status == 200  # still usable
    http.close()
    owned = ElibraryClient(settings)
    assert owned.http.settings is settings
    owned.close()
    owned.close()  # idempotent


# ============================================================================ reference data


def test_refdata_names_all_listable():
    assert len(refdata.NAMES) == 30
    for name in refdata.NAMES:
        opts = refdata.list_options(name)
        assert opts, name
        assert all(set(o) <= {"value", "label", "count", "site_code", "form_field"} and o["label"] for o in opts), name
    assert all(target in refdata.NAMES for target in refdata.ALIASES.values())


def test_refdata_counts_and_values():
    counts = {d["name"]: d["count"] for d in refdata.describe_options()}
    assert (counts["authors.countryid"], counts["journals.countryid"], counts["orgs.countryid"]) == (84, 158, 230)
    assert (counts["orgs.regionid"], counts["journals.white"], counts["authors.rubriccode"]) == (2207, 18, 70)
    vak = refdata.list_options("vak")
    assert vak[1] == {"value": "k1", "label": "Перечень ВАК (категория 1)", "count": 772, "site_code": "2"}
    assert refdata.list_options("authors.sortorder")[2] == {"value": "citations", "label": "по числу цитирований",
                                                            "site_code": "2"}
    assert refdata.list_options("orgs/sortorder")[2]["value"] == "publications"
    assert refdata.list_options("journal_flags")[2]["value"] == "core_rinc"
    assert refdata.list_options("author:codetype")[0] == {"value": "SPIN", "label": "SPIN-код"}
    assert refdata.find_options("regions", "новосибирская") == [
        {"value": "51", "label": "Новосибирская область", "count": 333}]
    assert refdata.find_options("regions", "Москва")[0]["value"] == "73"
    assert refdata.find_options("journal_countries", "Германия")[0]["value"] == "DEU"


def test_refdata_publication_options_match_the_query_model():
    types = [o["value"] for o in refdata.list_options("types")]
    assert tuple(types) == ALL_TYPES and PublicationQuery(text="ab", types=types).types == list(ALL_TYPES)
    for o in refdata.list_options("publications.sort"):
        assert PublicationQuery(text="ab", sort=o["value"]).sort == o["value"]
    for o in refdata.list_options("period"):
        PublicationQuery(text="ab", period=o["value"])
    for o in refdata.list_options("flags"):
        assert o["value"] in PublicationQuery.model_fields


def test_refdata_unknown_name():
    with pytest.raises(ValueError) as ei:
        refdata.list_options("orgs._meta")
    assert "orgs.regionid" in str(ei.value)
    with pytest.raises(ValueError):
        refdata.list_options("nothing")
    assert ElibraryClient.list_options("wos")[0]["value"] == "indexed"
    assert ElibraryClient.option_lists()[0]["name"] == "publications.types"
    assert ElibraryClient.find_options("vak", "категория 2")[0]["value"] == "k2"


# ============================================================================ re-exports


def test_package_reexports():
    import elibrary_cli.models as models
    import elibrary_cli.parsers as parsers
    import elibrary_cli.queries as queries

    for mod in (models, parsers, queries):
        for name in mod.__all__:
            assert getattr(mod, name) is not None, (mod.__name__, name)
    from elibrary_cli.auth import SessionInfo
    from elibrary_cli.fulltext import FullText

    assert models.FullText is FullText and models.SessionInfo is SessionInfo
    assert models.PublicationHit is PublicationHit
    with pytest.raises(AttributeError):
        models.DoesNotExist  # noqa: B018
    assert parsers.parse_search_results.__module__ == "elibrary_cli.parsers.search"
    assert queries.build_search_form.__module__ == "elibrary_cli.queries.publications"


# ============================================================================ review regressions (core safety)


def test_search_stale_session_query_without_page1_reruns(client, site, server, http):
    """#16: a remembered query whose page 1 is gone and whose age is unknown (a bare signature, as older
    versions stored it) is not trusted: the session may hold another query (the browser shares SUserID)."""
    server.current = "patents"  # e.g. the user searched patents in the browser with the same session
    http.state.set("query.current", search_signature(PublicationQuery(text="нейронные сети")))
    p2 = client.search_publications(text="нейронные сети", page=2)
    assert (p2.page, p2.total) == (2, ALL_TOTAL)
    assert search_requests(site) == ["run", "p2"]


def test_search_old_session_query_is_not_trusted(client, site, server, http):
    """#16: a remembered query older than TTL_SEARCH is re-run even when the signature matches."""
    server.current = "patents"
    http.state.set("query.current", {"sig": search_signature(PublicationQuery(text="нейронные сети")),
                                     "ts": time.time() - 2 * 86400})
    p2 = client.search_publications(text="нейронные сети", page=2)
    assert p2.total == ALL_TOTAL and search_requests(site) == ["run", "p2"]


def test_search_page_accepted_without_page1_is_not_cached(client, site, server, http):
    """#16: after `cache clear` page N of the remembered query cannot be checked against page 1: it is
    returned, but never cached for 24 h under that query."""
    client.search_publications(text="нейронные сети")
    client.clear_cache()
    server.current = "patents"  # the session now holds another query (browser)
    client.search_publications(text="нейронные сети", page=2)
    again = client.search_publications(text="нейронные сети", page=2)
    assert again.from_cache is False
    assert search_requests(site) == ["run", "p2", "p2"]
    assert client.status()["current_query"] == search_signature(PublicationQuery(text="нейронные сети"))


def test_organization_publications_use_the_heavy_timeout(client, site, settings):
    """#15: org_items.asp gets one long read timeout instead of a retry after a timeout."""
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.extensions.get("timeout") or {}))
        return html("author_items_3771")

    site.add(lambda r: "org_items.asp" in str(r.url), handler)
    client.get_organization_publications(2541, page=40)
    assert seen[0]["read"] == max(settings.timeout, settings.heavy_timeout) == 180.0


def test_check_session_keeps_ban_pause(client, site, http):
    """#14: ElibraryClient.check_session (used by the MCP tool) never lifts a ban pause; force does."""
    from elibrary_cli.errors import RateLimited
    from elibrary_cli.http import Gate

    site.page("defaultx.asp", "item_article_46371463")
    http._trip_breaker(Gate.IP_BLOCKED, "https://elibrary.ru/ip_blocked.asp", 24 * 60)
    with pytest.raises(RateLimited):
        client.check_session()
    assert site.requests == []
    assert client.check_session(force=True).outcome == "checked" and len(site.requests) == 1


class _DeadZotero:
    """A writer whose Zotero is not running: nothing can be written."""

    def __init__(self) -> None:
        self.added = False

    def ping(self) -> dict:
        return {"available_backends": [], "hints": ["Zotero не запущен."], "config": {"local": True}}

    def add(self, *a, **kw):  # pragma: no cover - must never be reached
        self.added = True
        return []

    def close(self) -> None:
        pass


def test_zotero_add_checks_zotero_before_fetching_cards(http, settings, site):
    from elibrary_cli.integrations.zotero import ZoteroUnavailable

    site.page("item.asp", "item_article_46371463")
    dead = _DeadZotero()
    c = ElibraryClient(http=http, zotero=dead)
    with pytest.raises(ZoteroUnavailable, match="запросов к eLIBRARY.RU не было"):
        c.zotero_add([46371463, 54076582], with_pdf=True)
    assert site.requests == [], "no card may be fetched when Zotero is unreachable"
    assert not dead.added
