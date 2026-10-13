"""Tests of the ``elibrary`` command line (``elibrary_cli.cli``).

Every command runs through ``typer.testing.CliRunner`` with :data:`elibrary_cli.cli.common.client_factory`
replaced by a factory that builds an :class:`ElibraryClient` over the conftest ``FakeSite`` (saved
elibrary.ru pages). Nothing is sent to elibrary.ru, Zotero or any open-access API."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
from typer.testing import CliRunner

from elibrary_cli import __version__
from elibrary_cli.cli import app, common
from elibrary_cli.client import ElibraryClient
from elibrary_cli.errors import DependencyMissing
from elibrary_cli.fulltext import FullText
from elibrary_cli.fulltext.net import ExternalHTTP
from elibrary_cli.http import BAN_BREAKER_MINUTES, ElibraryHTTP, Gate

from .conftest import FakeSite, fixture_bytes

HTML = {"Content-Type": "text/html; Charset=UTF-8"}
DOI = "10.47056/0365-9615-2021-172-7-98-101"
ANON_FONT = "<font color=#00008f>Незарегистрированный пользователь</font>"
USER_LINK = ('<font color=#00008f><a href="/author_info.asp?isold=1" title="Персональная карточка '
             'пользователя">ivanov_test&nbsp;</a></font>')


# ============================================================================ helpers and fixtures


def html(body: str | bytes, status: int = 200) -> httpx2.Response:
    content = fixture_bytes(body) if isinstance(body, str) else body
    return httpx2.Response(status, content=content, headers=HTML)


def params_of(req: httpx2.Request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(str(req.url)).query, keep_blank_values=True))


def body_of(req: httpx2.Request) -> dict[str, str]:
    return dict(parse_qsl(req.content.decode("ascii"), keep_blank_values=True))


def path_is(path: str):
    return lambda r: r.url.path == path


def logged_in_page(fixture: str = "item_article_46371463") -> bytes:
    """A signed-in variant of a saved anonymous page (same trick as tests/test_auth.py)."""
    import re

    text = fixture_bytes(fixture).decode("utf-8")
    text = text.replace(ANON_FONT, USER_LINK, 1)
    text = re.sub(r'<FORM[^>]*name="login".*?</FORM>', "", text, count=1, flags=re.S | re.I)
    return text.encode("utf-8")


def _no_external(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - must not be called
    raise AssertionError(f"unexpected external request {request.url}")


class FakeZotero:
    """Stands in for integrations.zotero.ZoteroWriter: records add() calls, never touches the network."""

    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.calls: list[dict] = []
        self.closed = False

    def ping(self, *, refresh: bool = True) -> dict:
        backends = ["connector"] if self.running else []
        return {"running": self.running, "version": "10.0.6" if self.running else None, "connector": self.running,
                "local_api": False, "available_backends": backends, "backend": backends[0] if backends else None,
                "hints": [] if self.running else ["Zotero не запущен или недоступен на http://127.0.0.1:23119"]}

    def authorize_local(self, app_name=None, **kw) -> dict:
        return {"remember": True, "single_use": False, "server_id": "abc", "saved_to": "zotero.json"}

    def add(self, items, *, collection=None, tags=None, pdf_paths=None, dedupe=True, backend=None, **kw):
        items = list(items)
        self.calls.append({"items": items, "collection": collection, "tags": tags, "backend": backend,
                           "dedupe": dedupe})
        return [{"index": i, "title": getattr(p, "title", ""), "url": getattr(p, "url", None), "key": f"KEY{i:05d}",
                 "backend": "connector", "status": "created", "message": None, "attachment": None,
                 "collection": collection} for i, p in enumerate(items)]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def net(settings):
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(_no_external), cache=False)
    yield client
    client.close()


@pytest.fixture
def zotero() -> FakeZotero:
    return FakeZotero()


@pytest.fixture
def clients(monkeypatch, http: ElibraryHTTP, net, zotero) -> list[ElibraryClient]:
    """Route every CLI command to a client over the fake site; returns the clients created."""
    created: list[ElibraryClient] = []

    def factory(settings):
        client = ElibraryClient(http=http, net=net, zotero=zotero)
        created.append(client)
        return client

    monkeypatch.setattr(common, "client_factory", factory)
    monkeypatch.setattr(common, "zotero_factory", lambda settings: zotero)
    for var in ("ELIBRARY_LOGIN", "ELIBRARY_PASSWORD", "ELIBRARY_OFFLINE", "ELIBRARY_REFRESH"):
        monkeypatch.delenv(var, raising=False)
    return created


@pytest.fixture
def runner(monkeypatch) -> CliRunner:
    monkeypatch.setenv("COLUMNS", "250")  # rich wraps tables at 80 columns when stdout is not a terminal
    return CliRunner()


def invoke(runner: CliRunner, *args: str, input: str | None = None):
    return runner.invoke(app, list(args), input=input)


def ok_json(result):
    assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr, result.exception)
    out = result.stdout
    # CliRunner echoes typed prompt answers to stdout; the JSON document follows them
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=0)
    return json.loads(out[start:])


# ============================================================================ root


def test_version(runner):
    r = invoke(runner, "--version")
    assert r.exit_code == 0
    assert r.stdout.strip() == f"elibrary-cli {__version__}"


def test_help_lists_command_groups_in_russian(runner):
    r = invoke(runner, "--help")
    assert r.exit_code == 0
    for name in ("search", "get", "resolve", "fulltext", "export", "zotero", "auth", "cache", "refdata", "mcp"):
        assert name in r.stdout
    assert "Научная электронная библиотека" in r.stdout
    assert "--offline" in r.stdout and "--min-delay" in r.stdout


def test_search_help_shows_choices(runner):
    r = invoke(runner, "search", "publications", "--help")
    assert r.exit_code == 0
    text = "".join(r.stdout.split())  # the metavars wrap in an 80-column terminal
    assert "<article|disser|book|report|conf|patent|preprint|grant|dataset>" in text.replace("│", "")
    assert "insdate" in text and "Типпубликаций" in text


# ============================================================================ search publications


def test_search_articles_json(runner, clients, site: FakeSite):
    site.page("query_results.asp", "query_results_all_types")
    data = ok_json(invoke(runner, "search", "articles", "нейронные сети", "-f", "json"))
    assert data["total"] == 290705
    assert data["total_in_database"] == 82293065
    assert data["pages"] == 2908 and data["page"] == 1
    assert len(data["items"]) == 100
    first = data["items"][0]
    assert first["id"] == 47430099
    assert first["authors"] == ["Суханова Н.В.", "Шептунов С.А."]
    assert first["kind"] == "patent" and first["year"] == 2021
    assert data["query"]["types"] == ["article"]
    # one request: the query run with changed=1 and only the article checkbox
    assert len(site.requests) == 1
    p = params_of(site.requests[0])
    assert p["changed"] == "1" and p["ftext"] == "нейронные сети"
    assert {k for k in p if k.startswith("type_")} == {"type_article"}
    assert {k for k in p if k.startswith("where_") and p[k] == "on"} == {"where_name", "where_abstract",
                                                                         "where_keywords"}


def test_default_format_is_json_when_not_a_terminal(runner, clients, site):
    site.page("query_results.asp", "query_results_all_types")
    data = ok_json(invoke(runner, "search", "articles", "нейронные сети"))
    assert data["items"][1]["id"] == 41770713


def test_search_results_are_cached(runner, clients, site):
    site.page("query_results.asp", "query_results_all_types")
    ok_json(invoke(runner, "search", "articles", "нейронные сети"))
    data = ok_json(invoke(runner, "search", "articles", "нейронные сети"))
    assert data["from_cache"] is True
    assert len(site.requests) == 1


def test_search_publications_filters(runner, clients, site):
    site.page("query_results.asp", "query_results_patents")
    data = ok_json(invoke(runner, "search", "publications", "сеть", "--type", "patent", "-w", "name",
                          "--year-from", "2020", "--year-to", "2026", "--sort", "date", "--order", "asc",
                          "--morph", "--no-fuzzy", "--author", "Борисов В В", "-j", "8610", "-r", "500000"))
    assert data["total"] == 27920
    assert data["items"][0]["id"] == 88918718
    p = params_of(site.requests[0])
    assert {k for k in p if k.startswith("type_")} == {"type_patent"}
    assert p["where_name"] == "on" and "where_abstract" not in p
    assert p["begin_year"] == "2020" and p["end_year"] == "2026"
    assert p["orderby"] == "date" and p["order"] == "nrm"
    assert p["search_morph"] == "on" and "search_freetext" not in p
    assert p["authors_all"] == "Борисов В В|"
    assert p["titles_all"] == "8610|" and p["rubrics_all"] == "500000|"
    assert data["query"]["author_names"] == ["Борисов В В"]


@pytest.mark.parametrize(("command", "types"), [
    ("patents", {"type_patent"}),
    ("books", {"type_book"}),
    ("dissertations", {"type_disser"}),
    ("conferences", {"type_conf"}),
])
def test_search_presets_set_types(runner, clients, site, command, types):
    site.page("query_results.asp", "query_results_patents")
    data = ok_json(invoke(runner, "search", command, "нейросеть"))
    assert data["items"][0]["kind"] == "patent"
    assert {k for k in params_of(site.requests[0]) if k.startswith("type_")} == types


def test_search_jsonl_one_line_per_hit(runner, clients, site):
    site.page("query_results.asp", "query_results_all_types")
    r = invoke(runner, "search", "articles", "нейронные сети", "-f", "jsonl")
    assert r.exit_code == 0
    lines = r.stdout.strip().splitlines()
    assert len(lines) == 100
    assert json.loads(lines[0])["id"] == 47430099
    assert json.loads(lines[2])["title"].startswith("НЕЙРОННЫЕ СЕТИ: ИСТОРИЧЕСКИЙ ОБЗОР")


def test_search_jsonl_without_hits_writes_nothing(runner, clients, site):
    # zero rows = zero lines: a lone "\n" breaks `for line in f: json.loads(line)` readers
    site.page("query_results.asp", "query_results_empty")
    r = invoke(runner, "search", "articles", "абвгдеёжз", "-f", "jsonl")
    assert r.exit_code == 0, r.stderr
    assert r.stdout == ""
    assert [json.loads(line) for line in r.stdout.splitlines(keepends=True)] == []


def test_emit_jsonl_empty_and_non_empty(capsys):
    from elibrary_cli.models.base import PublicationHit, SearchPage

    common.emit(SearchPage[PublicationHit](items=[], page=1), "jsonl")
    common.emit([], "jsonl")
    assert capsys.readouterr().out == ""
    common.emit([{"a": 1}, {"b": 2}], "jsonl")
    assert capsys.readouterr().out == '{"a": 1}\n{"b": 2}\n'


def test_search_table_output(runner, clients, site):
    site.page("query_results.asp", "query_results_all_types")
    r = invoke(runner, "search", "articles", "нейронные сети", "-f", "table")
    assert r.exit_code == 0
    assert "Найдено: 290 705 из 82 293 065" in r.stdout
    assert "страница 1 из 2908" in r.stdout  # 4-digit numbers are not grouped
    assert "47430099" in r.stdout and "Суханова Н.В." in r.stdout
    assert "Следующая страница: --page 2" in r.stderr


def page_two(fixture: str) -> bytes:
    """Page 2 of a saved result: only the highlighted paginator cell changes (as in test_client.py)."""
    raw = fixture_bytes(fixture)
    cur = b"<td width=5% bgcolor=#5c6aa6 nowrap><font color=#ffffff>1</font></td>"
    one = b'<td width=5% bgcolor=#555555 nowrap><a href="query_results.asp?pagenum=1">1</a></td>'
    link = b'<td width=5% bgcolor=#555555 nowrap><a href="query_results.asp?pagenum=2">2</a></td>'
    hl = b"<td width=5% bgcolor=#5c6aa6 nowrap><font color=#ffffff>2</font></td>"
    assert cur in raw and link in raw
    return raw.replace(cur, one).replace(link, hl)


def search_kinds(site: FakeSite) -> list[str]:
    return ["run" if params_of(q).get("changed") == "1" else f"p{params_of(q).get('pagenum')}"
            for q in site.requests if "query_results.asp" in str(q.url)]


def test_search_page_two_runs_query_then_pagenum(runner, clients, site):
    second = page_two("query_results_all_types")
    site.add(lambda r: "pagenum=2" in str(r.url), lambda r: html(second))
    site.page("query_results.asp", "query_results_all_types")
    data = ok_json(invoke(runner, "search", "articles", "нейронные сети", "-p", "2"))
    assert data["page"] == 2 and data["total"] == 290705
    assert search_kinds(site) == ["run", "p2"]
    assert site.requests[1].headers["Referer"] == "https://elibrary.ru/query_results.asp"
    # both pages are cached now
    assert ok_json(invoke(runner, "search", "articles", "нейронные сети", "-p", "1"))["page"] == 1
    assert len(site.requests) == 2


def test_search_inconsistent_page_is_reported(runner, clients, site):
    site.page("query_results.asp", "query_results_all_types")  # pagenum=2 answers with page 1
    r = invoke(runner, "search", "articles", "нейронные сети", "-p", "2")
    assert r.exit_code == 2
    assert "не ту страницу результатов" in r.stderr
    assert search_kinds(site) == ["run", "p2"]


def test_search_validation_error_exit_1(runner, clients, site):
    r = invoke(runner, "search", "articles", "x")
    assert r.exit_code == 1
    assert "не менее двух символов" in r.stderr
    assert site.requests == []


def test_search_without_criteria_exit_1(runner, clients, site):
    r = invoke(runner, "search", "publications")
    assert r.exit_code == 1
    assert "Не заданы условия поиска" in r.stderr


def test_search_bad_choice_is_usage_error(runner, clients, site):
    r = invoke(runner, "search", "articles", "нейроны", "--sort", "popularity")
    assert r.exit_code == 2
    assert site.requests == []


# ============================================================================ captcha and other errors


def test_captcha_exit_code_and_help(runner, clients, site):
    site.captcha_for("query_results.asp")
    r = invoke(runner, "search", "articles", "нейронные сети")
    assert r.exit_code == 2
    assert r.stdout == ""
    assert "Тест Тьюринга" in r.stderr
    assert "elibrary auth captcha" in r.stderr
    assert "copy(document.cookie)" in r.stderr
    assert "https://elibrary.ru/page_captcha.asp?rpage=" in r.stderr
    sent = len(site.requests)
    # the breaker is open: the next command fails at once, without a request
    r2 = invoke(runner, "get", "author", "3771")
    assert r2.exit_code == 2 and "Тест Тьюринга" in r2.stderr
    assert len(site.requests) == sent
    # and auth status explains what to do
    r3 = invoke(runner, "auth", "status", "-f", "json")
    status = json.loads(r3.stdout)
    assert status["breaker_open"] is True and status["breaker_gate"] == "captcha"
    assert "elibrary auth captcha" in r3.stderr


def test_network_error_exit_3(runner, clients, site, monkeypatch):
    monkeypatch.setattr("elibrary_cli.http.time.sleep", lambda s: None)

    def boom(request):
        raise httpx2.ConnectError("connection reset")

    site.add(lambda r: True, boom)
    r = invoke(runner, "get", "author", "3771")
    assert r.exit_code == 3
    assert "Нет связи с elibrary.ru" in r.stderr


def test_not_found_exit_2(runner, clients, site):
    r = invoke(runner, "get", "author", "3771")  # FakeSite answers 404
    assert r.exit_code == 2
    assert "Страница не найдена" in r.stderr


def test_offline_flag_uses_cache_only(runner, clients, site, http):
    r = invoke(runner, "--offline", "get", "author", "3771")
    assert r.exit_code == 2
    assert "Нет в кэше" in r.stderr
    assert site.requests == []
    assert http.settings.offline is True
    # once the page is cached, --offline serves it
    http.settings.offline = False
    site.page("author_profile.asp", "author_profile_3771")
    ok_json(invoke(runner, "get", "author", "3771"))
    data = ok_json(invoke(runner, "--offline", "get", "author", "3771"))
    assert data["spin"] == "9846-9426"
    assert len(site.requests) == 1


def test_min_delay_and_refresh_options(runner, clients, site, http):
    site.page("author_profile.asp", "author_profile_3771")
    ok_json(invoke(runner, "--min-delay", "0", "get", "author", "3771"))
    ok_json(invoke(runner, "--refresh", "--min-delay", "0", "get", "author", "3771"))
    assert http.settings.refresh is True and http.settings.min_delay == 0
    assert len(site.requests) == 2


# ============================================================================ authors, orgs, journals


def test_search_authors(runner, clients, site):
    site.page("authors.asp", "authors_results_petrov")
    data = ok_json(invoke(runner, "search", "authors", "Петров", "--country", "Россия", "--sort", "hindex"))
    assert data["total"] == 4342 and data["pages"] == 44
    first = data["items"][0]
    assert first["id"] == 3771 and first["name"] == "Петров Владимир Алексеевич"
    assert first["town"] == "Протвино" and first["hindex"] == 119
    req = site.requests[0]
    assert req.method == "POST"
    body = body_of(req)
    assert body["surname"] == "Петров" and body["countryid"] == "RUS"
    assert body["sortorder"] == "3"


def test_search_authors_by_spin(runner, clients, site):
    site.page("authors.asp", "authors_results_petrov")
    ok_json(invoke(runner, "search", "authors", "--spin", "9846-9426"))
    body = body_of(site.requests[0])
    assert body["codetype"] == "SPIN" and body["codevalue"] == "9846-9426"


def test_search_authors_two_identifiers_rejected(runner, clients, site):
    r = invoke(runner, "search", "authors", "--spin", "9846-9426", "--orcid", "0000-0001-2345-6789")
    assert r.exit_code == 1
    assert "только один персональный идентификатор" in r.stderr
    assert site.requests == []


def test_search_authors_org_name_is_resolved(runner, clients, site):
    # live bug 2026-10-13: --org without --org-id did not filter at all (the site filters by orgid only)
    site.page("orgs.asp", "orgs_results_mgu").page("authors.asp", "authors_results_petrov")
    r = invoke(runner, "search", "authors", "Петров", "--org", "московский государственный университет")
    assert r.exit_code == 0, r.stderr
    orgs_req, authors_req = site.requests
    assert orgs_req.url.path == "/orgs.asp"
    assert body_of(orgs_req)["orgname"] == "московский государственный университет"
    assert body_of(orgs_req)["sortorder"] == "3" and body_of(orgs_req)["order"] == "1"  # by publications, desc
    body = body_of(authors_req)
    assert body["orgid"] == "2541" and body["surname"] == "Петров"
    assert body["orgname"] == "Московский государственный университет им. М.В. Ломоносова"
    # which organization was used goes to stderr, the data stays clean JSON on stdout
    assert "Организация: Московский государственный университет им. М.В. Ломоносова (ID 2541, Москва" in r.stderr
    assert "публикаций: 429 371" in r.stderr and "найдено организаций" in r.stderr and ": 23" in r.stderr
    assert "--org-id" in r.stderr
    data = json.loads(r.stdout)
    assert data["query"]["org_id"] == 2541
    assert data["query"]["org_resolved"]["id"] == 2541 and data["query"]["org_resolved"]["candidates"] == 23


def test_search_authors_org_name_alone_and_org_id_wins(runner, clients, site):
    site.page("orgs.asp", "orgs_results_mgu").page("authors.asp", "authors_results_petrov")
    r = invoke(runner, "search", "authors", "--org", "МИИТ")  # no surname: the resolved org id is the criterion
    assert r.exit_code == 0, r.stderr
    assert body_of(site.requests[1])["orgid"] == "1229"  # "(МИИТ)" in the name is an exact match
    assert "точное совпадение" in r.stderr
    site.requests.clear()
    r = invoke(runner, "search", "authors", "Петров", "--org", "МГУ", "--org-id", "169")
    assert r.exit_code == 0
    (req,) = site.requests
    assert req.url.path == "/authors.asp" and body_of(req)["orgid"] == "169" and body_of(req)["orgname"] == "МГУ"
    assert "Организация:" not in r.stderr


def test_search_authors_org_name_not_found(runner, clients, site):
    site.page("orgs.asp", "orgs_form")  # the form page: no organizations found
    r = invoke(runner, "search", "authors", "Петров", "--org", "Несуществующий институт")
    assert r.exit_code == 2
    assert "Организация «Несуществующий институт» не найдена" in r.stderr
    assert [q.url.path for q in site.requests] == ["/orgs.asp"]  # the author search is not sent


def test_search_authors_country_names(runner, clients, site):
    site.page("authors.asp", "authors_results_petrov")
    ok_json(invoke(runner, "search", "authors", "Петров", "--country", "Kazakhstan"))
    assert body_of(site.requests[0])["countryid"] == "KAZ"
    r = invoke(runner, "search", "authors", "Петров", "--country", "Атлантида")
    assert r.exit_code == 1 and "неизвестная страна" in r.stderr
    assert len(site.requests) == 1


def test_search_authors_table(runner, clients, site):
    site.page("authors.asp", "authors_results_petrov")
    r = invoke(runner, "search", "authors", "Петров", "-f", "table")
    assert r.exit_code == 0
    assert "Петров Владимир Алексеевич *" in r.stdout
    assert "86 835" in r.stdout and "Протвино" in r.stdout


def test_search_orgs(runner, clients, site):
    site.page("orgs.asp", "orgs_results_mgu")
    data = ok_json(invoke(runner, "search", "orgs", "МГУ", "--region", "Москва", "--sort", "publications"))
    assert data["total"] == 23
    assert data["items"][0]["id"] == 2541
    assert data["items"][0]["name"] == "Московский государственный университет им. М.В. Ломоносова"
    body = body_of(site.requests[0])
    assert body["orgname"] == "МГУ" and body["regionid"] == "73"
    assert body["sortorder"] == "3" and body["order"] == "1"


def test_search_orgs_table(runner, clients, site):
    site.page("orgs.asp", "orgs_results_mgu")
    r = invoke(runner, "search", "orgs", "МГУ", "-f", "table")
    assert r.exit_code == 0
    assert "429 371" in r.stdout and "Москва" in r.stdout


def test_search_journals(runner, clients, site):
    site.page("titles.asp", "titles_results")
    data = ok_json(invoke(runner, "search", "journals", "вычислительные", "--vak", "k1", "--scopus", "q2",
                          "--rsci", "--sort", "name"))
    assert data["total"] == 4
    first = data["items"][0]
    assert first["id"] == 8610 and first["name"] == "Вычислительные технологии"
    assert first["articles"] == 2275
    body = body_of(site.requests[0])
    assert body["titlename"] == "вычислительные"
    assert body["vak"] == "2" and body["scopus"] == "5" and body["rsci"] == "on"
    assert body["sortorder"] == "0" and body["order"] == "0"


def test_search_journals_bad_code(runner, clients, site):
    r = invoke(runner, "search", "journals", "--vak", "k9")
    assert r.exit_code == 1
    assert "Недопустимое значение vak" in r.stderr
    assert site.requests == []


# ============================================================================ get


def test_get_publication_by_id(runner, clients, site):
    site.page("item.asp", "item_article_46371463")
    data = ok_json(invoke(runner, "get", "publication", "46371463"))
    assert data["id"] == 46371463 and data["edn"] == "FYOAKL" and data["doi"] == DOI
    assert data["year"] == 2021
    assert data["authors"][0]["name"] == "БЛОХИНА Т.М."
    assert data["journal"]["name"] == "БЮЛЛЕТЕНЬ ЭКСПЕРИМЕНТАЛЬНОЙ БИОЛОГИИ И МЕДИЦИНЫ"
    assert params_of(site.requests[0]) == {"id": "46371463"}


def test_get_publication_by_edn_and_url(runner, clients, site):
    site.page("item.asp", "item_article_46371463")
    data = ok_json(invoke(runner, "get", "publication", "fyoakl"))
    assert data["id"] == 46371463
    assert params_of(site.requests[0]) == {"edn": "FYOAKL"}
    # the card is cached under both keys: the URL form costs no request
    data = ok_json(invoke(runner, "get", "publication", "https://elibrary.ru/item.asp?id=46371463"))
    assert data["edn"] == "FYOAKL"
    assert len(site.requests) == 1


def test_get_publication_by_doi(runner, clients, site):
    site.add(lambda r: DOI in str(r.url),
             lambda r: httpx2.Response(302, headers={"Location": "/item.asp?id=46371463"}))
    site.page("item.asp", "item_article_46371463")
    data = ok_json(invoke(runner, "get", "publication", f"https://doi.org/{DOI}"))
    assert data["id"] == 46371463


def test_get_publication_table_title_is_not_markup(runner, clients, site):
    # rich reads str table titles as markup: "[bmim]" vanished and "[/" raised MarkupError
    page = fixture_bytes("item_article_46371463").decode("utf-8")
    page = page.replace("γН2АХ-ПОЗИТИВНЫХ", "γН2АХ [BMIM][BF4] [/] [/B] [IN RUSSIAN] ПОЗИТИВНЫХ", 1)
    body = page.encode("utf-8")
    site.add(lambda r: "item.asp" in str(r.url), lambda r: html(body))
    r = invoke(runner, "get", "publication", "46371463", "-f", "table")
    assert r.exit_code == 0, (r.stderr, r.exception)
    heading = r.stdout.splitlines()[0]
    assert "γН2АХ [BMIM][BF4] [/] [/B] [IN RUSSIAN] ПОЗИТИВНЫХ" in heading


@pytest.mark.parametrize("title", [
    "Ionic liquid [bmim][BF4] as solvent [in Russian]",
    "Fe/Cr [/] multilayers",
    "Tag [b]bold[/b] x",
    "Сборник [Электронный ресурс]",
])
def test_table_titles_keep_brackets(title):
    import io

    from rich.console import Console

    from elibrary_cli.cli import render
    from elibrary_cli.models.authors import AuthorProfile
    from elibrary_cli.models.base import PublicationHit
    from elibrary_cli.models.publications import Publication

    def shown(table) -> str:
        buf = io.StringIO()
        Console(file=buf, highlight=False, soft_wrap=False, width=300).print(table)
        return buf.getvalue()

    hit = PublicationHit(id=1, title="t")
    for out in (shown(render.kv_table([("ID", "1" * 80)], title=title)),
                shown(render.publications_table([hit], title=title)),
                shown(render.records_table([{title: 1}], title=title))):
        assert title in out
    buf = io.StringIO()
    con = Console(file=buf, highlight=False, soft_wrap=False, width=300)
    render.render_publication(Publication(id=1, title=title, url="https://elibrary.ru/item.asp?id=1"), con)
    assert buf.getvalue().splitlines()[0].strip() == title
    buf = io.StringIO()
    render.render_table(AuthorProfile(id=1, name=title), Console(file=buf, width=300))
    assert buf.getvalue().splitlines()[0].strip() == title


def test_get_publication_table(runner, clients, site):
    site.page("item.asp", "item_article_46371463")
    r = invoke(runner, "get", "publication", "46371463", "-f", "table")
    assert r.exit_code == 0
    assert "FYOAKL" in r.stdout and DOI in r.stdout
    assert "Ключевые слова" in r.stdout and "γН2АХ, ЛИМФОЦИТЫ, ОБЕЗЬЯНЫ" in r.stdout
    assert "БЛОХИНА Т.М." in r.stdout
    assert "Аннотация" not in r.stdout  # the saved card has no abstract


def test_get_publication_wrong_target(runner, clients, site):
    r = invoke(runner, "get", "publication", "https://elibrary.ru/author_profile.asp?id=3771")
    assert r.exit_code == 1
    assert "указывает на автора" in r.stderr and "elibrary get author" in r.stderr
    assert site.requests == []


def test_wrong_target_hints_name_the_right_command(runner, clients, site):
    r = invoke(runner, "get", "publication", "0000-0002-1984-3580")  # an ORCID never names a publication
    assert r.exit_code == 1
    assert "указывает на автора, а не на публикацию (используйте `elibrary get author`)" in r.stderr
    r = invoke(runner, "get", "author", "https://elibrary.ru/item.asp?id=46371463")
    assert r.exit_code == 1
    assert "указывает на публикацию, а не на автора (используйте `elibrary get publication`)" in r.stderr
    assert site.requests == []


def test_get_publication_unknown_identifier(runner, clients, site):
    r = invoke(runner, "get", "publication", "что-то странное")
    assert r.exit_code == 1
    assert "Не удалось распознать идентификатор" in r.stderr


def test_get_author(runner, clients, site):
    site.page("author_profile.asp", "author_profile_3771")
    data = ok_json(invoke(runner, "get", "author", "3771"))
    assert data["id"] == 3771 and data["name"] == "Петров Владимир Алексеевич"
    assert data["spin"] == "9846-9426"
    assert data["publications_rinc"] == 1069 and data["citations_rinc"] == 86835 and data["hindex_rinc"] == 119


def test_get_author_by_url_and_table(runner, clients, site):
    site.page("author_profile.asp", "author_profile_3771")
    r = invoke(runner, "get", "author", "https://elibrary.ru/author_profile.asp?authorid=3771", "-f", "table")
    assert r.exit_code == 0
    assert "Петров Владимир Алексеевич" in r.stdout and "9846-9426" in r.stdout
    assert "1069" in r.stdout and "86 835" in r.stdout  # grouping only from 5 digits
    assert params_of(site.requests[0]) == {"id": "3771"}


def test_get_author_publications(runner, clients, site):
    site.page("author_items.asp", "author_items_3771")
    data = ok_json(invoke(runner, "get", "author-pubs", "3771"))
    assert data["author_id"] == 3771 and data["total"] == 1094 and data["pages"] == 11
    assert data["items"][0]["id"] == 91809593
    assert params_of(site.requests[0]) == {"authorid": "3771", "show_refs": "1"}


def test_get_author_publications_table(runner, clients, site):
    site.page("author_items.asp", "author_items_3771")
    r = invoke(runner, "get", "author-pubs", "3771", "-f", "table")
    assert r.exit_code == 0
    assert "Петров Владимир Алексеевич (AuthorID 3771)" in r.stdout
    assert "91809593" in r.stdout


def test_get_org(runner, clients, site):
    site.page("org_profile.asp", "org_profile_2541")
    data = ok_json(invoke(runner, "get", "org", "2541"))
    assert data["id"] == 2541
    assert data["publications"] == 502087 and data["citations"] == 4303477 and data["h_index"] == 496
    r = invoke(runner, "get", "org", "2541", "-f", "table")
    assert "502 087" in r.stdout and "Московский государственный университет" in r.stdout


def test_get_org_publications_params(runner, clients, site):
    site.page("org_items.asp", "author_items_3771")  # no org_items fixture: only the request is checked
    invoke(runner, "get", "org-pubs", "2541", "-p", "2", "--year", "2024", "--year", "2025", "--sort", "cited",
           "--order", "desc", "--show", "0")
    p = params_of(site.requests[0])
    assert p["orgsid"] == "2541" and p["pagenum"] == "2" and p["pubyear"] == "2024|2025"
    assert p["sortorder"] == "3" and p["order"] == "1" and p["show_option"] == "0"


def test_get_org_publications_rejects_unknown_show(runner, clients, site):
    r = invoke(runner, "get", "org-pubs", "2541", "--show", "99")
    assert r.exit_code == 1
    assert "допустимые значения" in r.stderr and "2 — публикации организации, входящие в ядро РИНЦ" in r.stderr
    assert site.requests == []


def test_get_journal(runner, clients, site):
    site.page("title_about_new.asp", "title_about_8610")
    data = ok_json(invoke(runner, "get", "journal", "8610"))
    assert data["id"] == 8610 and data["name"] == "Вычислительные технологии"
    assert data["issn"] == "1560-7534" and data["eissn"] == "2313-691X"
    assert data["foundation_year"] == 1995
    r = invoke(runner, "get", "journal", "8610", "-f", "table")
    assert "1560-7534" in r.stdout


def test_get_issue_latest_of_journal(runner, clients, site):
    site.page("contents.asp", "contents_8610")
    data = ok_json(invoke(runner, "get", "issue", "--journal-id", "8610"))
    assert data["journal_id"] == 8610 and data["issue_id"] == 91982811
    assert data["label"] == "Т.31 № 4 за 2026 год"
    assert len(data["items"]) == 10
    assert data["items"][0]["pages"] == "7-19"
    assert params_of(site.requests[0]) == {"titleid": "8610"}


def test_get_issue_table_shows_sections(runner, clients, site):
    site.page("contents.asp", "contents_8610")
    r = invoke(runner, "get", "issue", "--issue-id", "91982811", "-f", "table")
    assert r.exit_code == 0
    assert "МАТЕМАТИЧЕСКОЕ МОДЕЛИРОВАНИЕ" in r.stdout and "7-19" in r.stdout
    assert params_of(site.requests[0]) == {"id": "91982811"}


def test_get_issue_needs_an_id(runner, clients, site):
    r = invoke(runner, "get", "issue")
    assert r.exit_code == 1
    assert "--journal-id" in r.stderr


# ============================================================================ resolve, export, fulltext


def test_resolve_local_id(runner, clients, site):
    data = ok_json(invoke(runner, "resolve", "46371463"))
    assert data["kind"] == "elibrary_id" and data["target"] == "publication" and data["id"] == 46371463
    assert data["url"] == "https://elibrary.ru/item.asp?id=46371463"
    assert site.requests == []


def test_resolve_doi_redirect_and_table(runner, clients, site):
    site.add(lambda r: DOI in str(r.url),
             lambda r: httpx2.Response(302, headers={"Location": "/item.asp?id=46371463"}))
    r = invoke(runner, "resolve", DOI, "-f", "table")
    assert r.exit_code == 0
    assert "публикация" in r.stdout and "46371463" in r.stdout
    data = ok_json(invoke(runner, "resolve", DOI))
    assert data["from_cache"] is True and data["id"] == 46371463
    assert len(site.requests) == 1


def test_export_bibtex_stdout(runner, clients, site):
    site.page("item.asp", "item_article_46371463")
    r = invoke(runner, "export", "46371463")
    assert r.exit_code == 0
    assert r.stdout.startswith("@article{blokhina2021dlitelnoe,")
    assert "journal  = {Бюллетень экспериментальной биологии и медицины}" in r.stdout


def test_export_csl_to_file(runner, clients, site, tmp_path: Path):
    site.page("item.asp", "item_article_46371463")
    out = tmp_path / "out" / "refs.json"
    r = invoke(runner, "export", "FYOAKL", "-f", "csl", "--out", str(out))
    assert r.exit_code == 0
    assert r.stdout == ""
    assert "записано в" in r.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data[0]["type"] == "article-journal" and data[0]["DOI"] == DOI


def test_export_unknown_format_is_usage_error(runner, clients):
    r = invoke(runner, "export", "46371463", "-f", "endnote")
    assert r.exit_code == 2


def _fake_fulltext(text: str):
    calls: list[dict] = []

    def get_fulltext(item, *, offset=0, max_chars=20000, sources=None, refresh=False):
        calls.append({"item": item, "offset": offset, "max_chars": max_chars, "sources": sources, "refresh": refresh})
        chunk = text[offset:offset + max_chars]
        nxt = offset + len(chunk) if offset + len(chunk) < len(text) else None
        return FullText(text=chunk, offset=offset, next_offset=nxt, total_chars=len(text), truncated=nxt is not None,
                        source="cyberleninka", source_url="https://cyberleninka.ru/article/n/x", format="html")

    return get_fulltext, calls


def test_fulltext_chunk_and_next_offset(runner, clients, monkeypatch):
    fake, calls = _fake_fulltext("А" * 250 + "Б" * 250)
    monkeypatch.setattr(ElibraryClient, "get_fulltext", lambda self, *a, **k: fake(*a, **k))
    r = invoke(runner, "fulltext", "46371463", "--max-chars", "300", "--source", "cyberleninka", "-s", "crossref")
    assert r.exit_code == 0
    assert r.stdout == "А" * 250 + "Б" * 50 + "\n"
    assert "Источник: cyberleninka (https://cyberleninka.ru/article/n/x)" in r.stderr
    assert "Продолжение: --offset 300" in r.stderr
    assert calls[0] == {"item": "46371463", "offset": 0, "max_chars": 300, "sources": ["cyberleninka", "crossref"],
                        "refresh": False}


def test_fulltext_all_to_file_and_json(runner, clients, monkeypatch, tmp_path):
    fake, calls = _fake_fulltext("Текст статьи. " * 100)
    monkeypatch.setattr(ElibraryClient, "get_fulltext", lambda self, *a, **k: fake(*a, **k))
    out = tmp_path / "text.txt"
    r = invoke(runner, "fulltext", "FYOAKL", "--out", str(out))
    assert r.exit_code == 0
    assert out.read_text(encoding="utf-8") == "Текст статьи. " * 100 + "\n"
    assert calls[0]["max_chars"] >= 10**9
    data = ok_json(invoke(runner, "fulltext", "FYOAKL", "--offset", "14", "--max-chars", "100", "-f", "json"))
    assert data["offset"] == 14 and data["next_offset"] == 114 and data["source"] == "cyberleninka"


def test_fulltext_not_found(runner, clients, monkeypatch):
    from elibrary_cli.errors import NotFound

    def missing(self, *a, **k):
        raise NotFound("Полный текст не найден ни в одном источнике. cyberleninka: нет совпадений")

    monkeypatch.setattr(ElibraryClient, "get_fulltext", missing)
    r = invoke(runner, "fulltext", "46371463")
    assert r.exit_code == 2
    assert "Полный текст не найден" in r.stderr


# ============================================================================ zotero


def test_zotero_add(runner, clients, site, zotero):
    site.page("item.asp", "item_article_46371463")
    data = ok_json(invoke(runner, "zotero", "add", "46371463", "--collection", "Статьи/РИНЦ", "--tag", "elibrary"))
    assert data[0]["status"] == "created" and data[0]["key"] == "KEY00000"
    assert data[0]["elibrary_id"] == 46371463
    call = zotero.calls[0]
    assert call["collection"] == "Статьи/РИНЦ" and call["tags"] == ["elibrary"] and call["backend"] is None
    assert call["items"][0].edn == "FYOAKL"


def test_zotero_add_table(runner, clients, site):
    site.page("item.asp", "item_article_46371463")
    r = invoke(runner, "zotero", "add", "46371463", "-f", "table")
    assert r.exit_code == 0
    assert "created" in r.stdout and "KEY00000" in r.stdout


def _refusing_zotero():
    """A real ZoteroWriter whose 127.0.0.1 probes fail (Zotero is not running); no socket is opened."""
    from elibrary_cli.integrations.zotero import ZoteroConfig, ZoteroWriter

    probed: list[str] = []

    def refused(request: httpx2.Request) -> httpx2.Response:
        probed.append(str(request.url))
        raise httpx2.ConnectError("connection refused", request=request)

    return ZoteroWriter(ZoteroConfig(), transport=httpx2.MockTransport(refused), use_pyzotero=False), probed


def test_zotero_add_checks_zotero_before_fetching_cards(runner, monkeypatch, http, net, site):
    # Zotero is down: `zotero add` must fail before it spends one item.asp view per identifier
    # (and the full-text lookups of --with-pdf) on a write that cannot happen
    writer, probed = _refusing_zotero()
    monkeypatch.setattr(common, "client_factory", lambda s: ElibraryClient(http=http, net=net, zotero=writer))
    monkeypatch.setattr(common, "zotero_factory", lambda s: _refusing_zotero()[0])
    site.page("item.asp", "item_article_46371463")
    r = invoke(runner, "zotero", "add", "46371463", "46371464", "46371465", "--with-pdf")
    assert r.exit_code == 2, (r.stdout, r.stderr)
    assert "Нет доступного способа записи в Zotero" in r.stderr
    assert "запросов к eLIBRARY.RU не было" in r.stderr and "Zotero не запущен" in r.stderr
    assert site.requests == []


def test_zotero_add_explicit_backend_checked_first(runner, clients, site, monkeypatch):
    # Zotero desktop is running (connector only), but the Web API is not configured
    site.page("item.asp", "item_article_46371463")
    r = invoke(runner, "zotero", "add", "46371463", "--backend", "web")
    assert r.exit_code == 2
    assert "Способ записи в Zotero «web» недоступен" in r.stderr
    assert site.requests == []
    # an available explicit backend goes on to the cards and the write
    data = ok_json(invoke(runner, "zotero", "add", "46371463", "--backend", "connector"))
    assert data[0]["status"] == "created" and len(site.requests) == 1


def test_zotero_status(runner, clients, zotero):
    data = ok_json(invoke(runner, "zotero", "status"))
    assert data["running"] is True and data["available_backends"] == ["connector"]
    assert zotero.closed is True


def test_zotero_status_hints_when_not_running(runner, clients, monkeypatch):
    down = FakeZotero(running=False)
    monkeypatch.setattr(common, "zotero_factory", lambda settings: down)
    r = invoke(runner, "zotero", "status")
    assert r.exit_code == 0
    assert "Подсказка: Zotero не запущен" in r.stderr
    assert json.loads(r.stdout)["running"] is False


def test_zotero_authorize(runner, clients):
    data = ok_json(invoke(runner, "zotero", "authorize"))
    assert data["remember"] is True and data["server_id"] == "abc"


# ============================================================================ auth and cache


def test_auth_status_no_request(runner, clients, site):
    data = ok_json(invoke(runner, "auth", "status"))
    assert data["has_session_cookie"] is False and data["logged_in"] is False
    assert data["breaker_open"] is False and data["requests_last_hour"] == 0
    assert site.requests == []


def test_auth_cookies_import_names_only(runner, clients, http):
    r = invoke(runner, "auth", "cookies", "SUserID=secretvalue1; SCookieGUID=guid2; _ym_uid=777",
               "--user-agent", "Mozilla/5.0 Test")
    data = ok_json(r)
    assert data["cookie_names"] == ["SCookieGUID", "SUserID"]
    assert data["has_session_cookie"] is True and data["user_agent_saved"] is True
    assert "secretvalue1" not in r.stdout and "secretvalue1" not in r.stderr
    assert http.cookies()["SUserID"] == "secretvalue1"


def test_auth_cookies_from_file_and_stdin(runner, clients, http, tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text("SUserID=fromfile; SCookieGUID=g", encoding="utf-8")
    r = invoke(runner, "auth", "cookies", f"@{f}")
    assert ok_json(r)["count"] == 2
    assert "Внимание: User-Agent браузера не передан" in r.stderr
    assert http.cookies()["SUserID"] == "fromfile"
    r = invoke(runner, "auth", "cookies", "-", input="SUserID=fromstdin; SCookieGUID=g\n")
    assert ok_json(r)["has_session_cookie"] is True
    assert http.cookies()["SUserID"] == "fromstdin"


def test_auth_cookies_reset_breaker_after_captcha(runner, clients, site, http):
    site.captcha_for("author_profile.asp")
    assert invoke(runner, "get", "author", "3771").exit_code == 2
    assert http.status()["breaker_open"] is True
    ok_json(invoke(runner, "auth", "cookies", "SUserID=passed; SCookieGUID=g"))
    assert http.status()["breaker_open"] is False


def test_auth_cookies_empty_string(runner, clients):
    r = invoke(runner, "auth", "cookies", "   ")
    assert r.exit_code == 2
    assert "нет cookies" in r.stderr


def test_auth_cookies_missing_file(runner, clients, tmp_path):
    r = invoke(runner, "auth", "cookies", f"@{tmp_path / 'nope.txt'}")
    assert r.exit_code == 1
    assert "Не удалось прочитать файл" in r.stderr


def test_auth_check_anonymous(runner, clients, site):
    site.add(path_is("/defaultx.asp"), lambda r: html("querybox_form"))
    data = ok_json(invoke(runner, "auth", "check"))
    assert data["state"] == "anonymous" and data["logged_in"] is False
    assert data["outcome"] == "checked"
    assert "анонимный доступ" in data["message"]


def test_auth_status_ban_pause_does_not_suggest_auth_check(runner, clients, site, http):
    http._trip_breaker(Gate.IP_BLOCKED, "https://elibrary.ru/ip_blocked.asp", BAN_BREAKER_MINUTES)
    r = invoke(runner, "auth", "status")
    data = ok_json(r)
    assert data["breaker_open"] is True and data["breaker_gate"] == "ip_blocked"
    assert "support@elibrary.ru" in r.stderr and "auth check" not in r.stderr
    assert site.requests == []


def test_auth_check_lifts_a_ban_pause_only_with_force(runner, clients, site, http):
    site.add(path_is("/defaultx.asp"), lambda r: html("querybox_form"))
    http._trip_breaker(Gate.IP_BLOCKED, "https://elibrary.ru/ip_blocked.asp", BAN_BREAKER_MINUTES)
    r = invoke(runner, "auth", "check")
    assert r.exit_code == 2 and "support@elibrary.ru" in r.stderr
    assert site.requests == [] and http.status()["breaker_open"] is True
    # --force is a human's decision in a terminal (the MCP tools never pass it): one request, pause lifted
    data = ok_json(invoke(runner, "auth", "check", "--force"))
    assert data["state"] == "anonymous" and data["outcome"] == "checked"
    assert [req.url.path for req in site.requests] == ["/defaultx.asp"]
    assert http.status()["breaker_open"] is False


def test_auth_login_prompts_and_hides_password(runner, clients, site, http):
    site.add(path_is("/start_session.asp"), lambda r: html(logged_in_page()))
    r = invoke(runner, "auth", "login", input="ivanov_test\ns3cret-pass\n")
    data = ok_json(r)
    assert data["logged_in"] is True and data["user_name"] == "ivanov_test"
    assert "s3cret-pass" not in r.stdout and "s3cret-pass" not in r.stderr
    body = body_of(site.requests[0])
    assert body["login"] == "ivanov_test" and body["password"] == "s3cret-pass" and body["knowme"] == "on"
    assert "Вход выполнен: ivanov_test" in r.stderr
    assert http.state.get("account") == "ivanov_test"


def test_auth_login_from_environment(runner, clients, site, monkeypatch):
    monkeypatch.setenv("ELIBRARY_LOGIN", "envuser")
    monkeypatch.setenv("ELIBRARY_PASSWORD", "envpass")
    site.add(path_is("/start_session.asp"), lambda r: html(logged_in_page()))
    r = invoke(runner, "auth", "login", "--no-remember")
    assert ok_json(r)["logged_in"] is True
    body = body_of(site.requests[0])
    assert body["login"] == "envuser" and body["password"] == "envpass" and "knowme" not in body
    assert "envpass" not in r.stdout + r.stderr


def test_auth_login_rejected(runner, clients, site):
    site.add(path_is("/start_session.asp"), lambda r: html("querybox_form"))
    r = invoke(runner, "auth", "login", input="u\np\n")
    assert r.exit_code == 2
    assert "не подтвердил вход" in r.stderr


def test_auth_captcha_without_playwright(runner, clients, site, monkeypatch):
    def no_browser(self, purpose="captcha", **kw):
        raise DependencyMissing("browser", "playwright")

    monkeypatch.setattr(ElibraryClient, "browser_session", no_browser)
    site.captcha_for("author_profile.asp")
    invoke(runner, "get", "author", "3771")
    r = invoke(runner, "auth", "captcha")
    assert r.exit_code == 1
    assert "elibrary-cli[browser]" in r.stderr
    assert "вручную" in r.stderr and "copy(document.cookie)" in r.stderr
    assert "https://elibrary.ru/page_captcha.asp?rpage=" in r.stderr


def test_auth_captcha_solved(runner, clients, monkeypatch):
    from elibrary_cli.auth import SessionInfo

    seen = {}

    def solved(self, purpose="captcha", *, url=None, timeout_s=600, channel=None):
        seen.update(purpose=purpose, url=url, timeout_s=timeout_s, channel=channel)
        return SessionInfo(state="anonymous", outcome="solved", message="Проверка пройдена в браузере")

    monkeypatch.setattr(ElibraryClient, "browser_session", solved)
    r = invoke(runner, "auth", "captcha", "--timeout", "30", "--channel", "chrome")
    assert ok_json(r)["outcome"] == "solved"
    assert seen == {"purpose": "captcha", "url": None, "timeout_s": 30.0, "channel": "chrome"}
    assert "Пройдите проверку" in r.stderr


def test_auth_captcha_timeout_exit_2(runner, clients, monkeypatch):
    from elibrary_cli.auth import SessionInfo

    monkeypatch.setattr(ElibraryClient, "browser_session",
                        lambda self, purpose="captcha", **kw: SessionInfo(outcome="timeout", message="Время истекло"))
    r = invoke(runner, "auth", "captcha")
    assert r.exit_code == 2
    assert json.loads(r.stdout)["outcome"] == "timeout"


def test_auth_logout_and_clear(runner, clients, site, http):
    site.add(path_is("/end_session.asp"), lambda r: html("querybox_form"))
    data = ok_json(invoke(runner, "auth", "logout"))
    assert data["outcome"] == "logged_out" and data["logged_in"] is False
    assert body_of(site.requests[0]) == {"saveuser": "1"}
    http.set_cookies({"SUserID": "x"})
    data = ok_json(invoke(runner, "auth", "clear"))
    assert data["has_session_cookie"] is False and data["cookie_names"] == []
    assert http.cookies() == {}


def test_cache_stats_and_clear(runner, clients, site):
    site.page("author_profile.asp", "author_profile_3771")
    ok_json(invoke(runner, "get", "author", "3771"))
    stats = ok_json(invoke(runner, "cache", "stats"))
    assert stats["pages"] == 1 and stats["bytes"] > 0
    assert stats["downloads"] == 0
    cleared = ok_json(invoke(runner, "cache", "clear"))
    assert cleared == {"pages_removed": 1}
    r = invoke(runner, "cache", "stats", "-f", "table")
    assert "Страниц в кэше" in r.stdout


# ============================================================================ refdata and mcp


def test_refdata_lists(runner, clients):
    data = ok_json(invoke(runner, "refdata"))
    names = {x["name"] for x in data}
    assert {"orgs.regionid", "journals.vak", "publications.types", "authors.codetype"} <= names
    vak = next(x for x in data if x["name"] == "journals.vak")
    assert "vak" in vak["aliases"]


def test_refdata_find_region(runner, clients):
    data = ok_json(invoke(runner, "refdata", "regions", "--find", "Новосиб"))
    assert data == [{"value": "51", "label": "Новосибирская область", "count": 333}]


def test_refdata_list_and_table(runner, clients):
    # value = what `search journals --vak` accepts; the site's code is a separate field
    data = ok_json(invoke(runner, "refdata", "vak"))
    assert {"value": "k1", "label": data[1]["label"], "site_code": "2"} == {
        k: data[1][k] for k in ("value", "label", "site_code")}
    assert "key" not in data[1]
    r = invoke(runner, "refdata", "publications.types", "-f", "table")
    assert "статьи в журналах" in r.stdout and "type_article" in r.stdout
    assert "Поле формы" in r.stdout
    r = invoke(runner, "refdata", "vak", "-f", "table")
    assert "Код на сайте" in r.stdout and "k1" in r.stdout


def test_refdata_values_work_as_search_options(runner, clients, site):
    # the documented contract: a listed value is accepted by the matching search option
    site.page("authors.asp", "authors_results_petrov")
    sort = next(o for o in ok_json(invoke(runner, "refdata", "authors.sortorder")) if o.get("site_code") == "2")
    r = invoke(runner, "search", "authors", "Петров", "--sort", sort["value"])
    assert r.exit_code == 0, r.stderr


def test_refdata_unknown(runner, clients):
    r = invoke(runner, "refdata", "nonsense")
    assert r.exit_code == 1
    assert "Неизвестный справочник" in r.stderr


def test_mcp_command_runs_server(runner, clients, monkeypatch):
    from elibrary_cli import mcp_server

    seen = {}
    monkeypatch.setattr(mcp_server, "run", lambda transport, host, port, *, settings=None: seen.update(
        transport=transport, host=host, port=port, offline=settings.offline))
    r = invoke(runner, "--offline", "mcp", "--transport", "streamable-http", "--port", "9123")
    assert r.exit_code == 0, r.stderr
    assert seen == {"transport": "streamable-http", "host": "127.0.0.1", "port": 9123, "offline": True}


# ============================================================================ helpers


def test_entity_id_resolves_spin(clients, site, http, net):
    site.add(lambda r: r.url.path == "/9846-9426" or r.url.path == "/98469426",
             lambda r: httpx2.Response(302, headers={"Location": "/author_profile.asp?id=3771"}))
    client = ElibraryClient(http=http, net=net)
    assert common.entity_id(client, "spin:9846-9426", "author") == 3771
    assert common.entity_id(client, "author:3771", "author") == 3771
    assert common.entity_id(client, "https://elibrary.ru/org_profile.asp?id=2541", "organization") == 2541
    with pytest.raises(ValueError, match="указывает на автора"):
        common.entity_id(client, "author:3771", "journal")
    with pytest.raises(ValueError, match="положительным"):
        common.entity_id(client, "0", "author")


@pytest.mark.parametrize(("command", "identifier"), [
    ("author", "FYOAKL"),                    # EDN
    ("author", "edn:FYOAKL"),
    ("org", "https://elibrary.ru/item.asp?edn=FYOAKL"),
    ("journal", DOI),                        # DOI
    ("author", "ISBN 978-5-7996-1352-5"),    # ISBN
])
def test_get_entity_refuses_publication_identifiers_without_a_request(runner, clients, site, command, identifier):
    site.page("item.asp", "item_article_46371463")
    r = invoke(runner, "get", command, identifier)
    assert r.exit_code == 1, (r.stdout, r.stderr)
    assert "указывает на публикацию, а не на" in r.stderr and "`elibrary get publication`" in r.stderr
    assert site.requests == []


@pytest.mark.parametrize(("command", "word", "search"), [
    ("author", "Ivanov", "elibrary search authors Ivanov"),
    ("journal", "Nature", "elibrary search journals Nature"),
    ("org", "Yandex", "elibrary search orgs Yandex"),
])
def test_get_entity_surname_like_word_hints_the_search(runner, clients, site, command, word, search):
    # a 6-letter Latin word parses as an EDN: no item.asp?edn= request, a hint to search instead
    r = invoke(runner, "get", command, word)
    assert r.exit_code == 1, (r.stdout, r.stderr)
    assert "Страница не найдена" not in r.stderr
    assert "читаются как EDN публикации" in r.stderr and f"`{search}`" in r.stderr
    assert site.requests == []
    r = invoke(runner, "get", command, word)  # a retry costs nothing either
    assert r.exit_code == 1 and site.requests == []


def test_entity_id_refuses_publication_kinds_locally(clients, site, http, net):
    from elibrary_cli.client import WrongTarget

    client = ElibraryClient(http=http, net=net)
    for ident in ("FYOAKL", DOI, "978-5-7996-1352-5"):
        with pytest.raises(WrongTarget) as ei:
            common.entity_id(client, ident, "author")
        assert (ei.value.target, ei.value.expected) == ("publication", "author")
    assert site.requests == []


def test_read_value_and_jsonl_rows(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("﻿a=1", encoding="utf-8")
    assert common.read_value(f"@{f}") == "a=1"
    assert common.read_value("a=2") == "a=2"
    assert common.jsonl_rows([1, 2]) == [1, 2]
    assert common.jsonl_rows({"a": 1}) == [{"a": 1}]


def test_resolve_format_default():
    assert common.resolve_format("table") == "table"
    assert common.resolve_format(None) == "json"  # pytest's stdout is not a terminal


def test_years_are_not_digit_grouped_in_tables():
    from elibrary_cli.cli.render import _num

    assert _num(2020) == "2020"
    assert _num(9999) == "9999"
    assert _num(10000) == "10 000"
    assert _num(1170544) == "1 170 544"
