"""Regression tests on the pages saved during the live test of 2026-10-13 (anonymous session; the
visitor's IP in the session box of every fixture was replaced with 192.0.2.1), plus privacy checks
over all fixtures: no real IP, no session cookie, no session-box data in the parsed models and no
personal e-mail addresses of editorial staff. Nothing here talks to elibrary.ru."""

from __future__ import annotations

import ipaddress
import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from elibrary_cli.auth import parse_session_info
from elibrary_cli.client import ElibraryClient
from elibrary_cli.errors import ParseError
from elibrary_cli.fulltext.net import ExternalHTTP
from elibrary_cli.models import IssueContents, OrgPublicationsPage, Publication, PublicationHit, SearchPage
from elibrary_cli.parsers import (
    parse_author_profile,
    parse_author_publications,
    parse_author_search,
    parse_fulltext_flag,
    parse_issue_contents,
    parse_journal_profile,
    parse_journal_search,
    parse_journal_search_options,
    parse_org_form_options,
    parse_org_profile,
    parse_org_search,
    parse_organization_publications,
    parse_publication,
    parse_search_results,
)

from .conftest import FIXTURES, fixture_bytes

EXTERNAL = FIXTURES.parent / "external"
DOC_IP = "192.0.2.1"  # TEST-NET-1 (RFC 5737): the only IP address allowed in the fixtures


def _no_external(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - must not be called
    raise AssertionError(f"unexpected external request {request.url}")


@pytest.fixture
def client(http, settings):
    net = ExternalHTTP(settings, transport=httpx2.MockTransport(_no_external), cache=False)
    c = ElibraryClient(http=http, net=net)
    yield c
    c.close()
    net.close()


# ============================================================================ query_results.asp, page 2


@pytest.fixture(scope="module")
def graphene() -> SearchPage[PublicationHit]:
    return parse_search_results(fixture_bytes("query_results_page2_graphene"),
                                url="https://elibrary.ru/query_results.asp?pagenum=2")


def test_search_page2_counters(graphene):
    """«графен», year >= 2023: "ВСЕГО НАЙДЕНО ПУБЛИКАЦИЙ: 1051 из 82293065", 11 pages of 100."""
    assert (graphene.page, graphene.pages, graphene.per_page) == (2, 11, 100)
    assert graphene.total == 1051
    assert graphene.total_in_database == 82293065
    assert len(graphene.items) == 100
    assert graphene.has_next
    assert graphene.source_url == "https://elibrary.ru/query_results.asp?pagenum=2"


def test_search_page2_page_number_comes_from_the_paginator():
    # the highlighted cell of tr.menus wins over the page the caller thinks it asked for
    page = parse_search_results(fixture_bytes("query_results_page2_graphene"), page=7, query={"text": "графен"})
    assert page.page == 2 and page.pages == 11
    assert page.query == {"text": "графен"}


def test_search_page2_first_and_last_rows(graphene):
    first = graphene.items[0]
    assert first.id == 92058009
    assert first.title == "ВЛИЯНИЕ ГИБРИДНОЙ СИСТЕМЫ МУНТ/ГРАФЕН НА ТЕНЗОЧУВСТВИТЕЛЬНОСТЬ ПОЛИМЕРНЫХ НАНОКОМПОЗИТОВ"
    assert first.title.startswith("ВЛИЯНИЕ ГИБРИДНОЙ СИСТЕМЫ МУНТ/ГРАФЕН")
    assert first.authors == ["Щегольков Ал. В.", "Щегольков А. В."]
    assert first.source == "Российский химический журнал. 2026. Т. 70. № 3. С. 75-79."
    assert (first.kind, first.year, first.citations, first.position) == ("article", 2026, 0, 101)
    assert first.url == "https://elibrary.ru/item.asp?id=92058009"
    assert first.pages is None and first.section is None and first.versions == []
    last = graphene.items[-1]
    assert (last.id, last.position, last.year) == (53742894, 200, 2023)
    assert last.title == ("ОРГАНИЧЕСКАЯ МАКРОМОЛЕКУЛА НА ГРАФЕНЕ СО СТРУКТУРНЫМИ ДЕФЕКТАМИ: "
                          "ОЦЕНКИ ПЕРЕХОДА ЗАРЯДА И ЭНЕРГИИ АДГЕЗИИ")
    assert last.authors == ["Давыдов С.Ю."]
    assert last.source == "Физика твердого тела. 2023. Т. 65. № 3. С. 497-501."


def test_search_page2_rows_numbering_kinds_and_years(graphene):
    assert [h.position for h in graphene.items] == list(range(101, 201))
    assert len({h.id for h in graphene.items}) == 100
    assert all(h.title and h.source for h in graphene.items)
    # one row has no author line at all (an unsigned article): no authors, source still found
    assert [h.id for h in graphene.items if not h.authors] == [83040860]
    no_authors = next(h for h in graphene.items if h.id == 83040860)
    assert no_authors.source == "Лакокрасочные материалы и их применение. 2025. № 10 (578). С. 22-24."
    # every hit of this query is a journal article of 2023..2026 (the query filtered year >= 2023)
    assert Counter(h.kind for h in graphene.items) == {"article": 100}
    assert Counter(h.year for h in graphene.items) == {2023: 31, 2024: 30, 2025: 24, 2026: 15}
    rows = {h.id: h for h in graphene.items}
    assert rows[63359592].citations == 8
    assert rows[54076582].source == ("Радиоэлектроника. Наносистемы. Информационные технологии. "
                                     "2023. Т. 15. № 2. С. 153-160.")


def test_search_page2_year_is_not_taken_from_the_page_range(graphene):
    """Regression: "… 2025. Т. 99. № 12. С. 1901-1909." used to give year 1909 (last year-like number)."""
    hit = next(h for h in graphene.items if h.id == 89070716)
    assert hit.source == "Журнал физической химии. 2025. Т. 99. № 12. С. 1901-1909."
    assert hit.year == 2025


# ============================================================================ query_results.asp, no hits


def test_search_empty_result_is_not_a_parse_error():
    """"Не найдено публикаций, соответствующих запросу": an empty page, not an unrecognised one."""
    page = parse_search_results(fixture_bytes("query_results_empty"), query={"text": "zzzz"})
    assert page.total == 0
    assert page.items == []
    assert (page.page, page.pages, page.per_page) == (1, 0, 100)
    assert page.total_in_database is None
    assert not page.has_next
    assert page.query == {"text": "zzzz"}


def test_search_empty_result_without_the_message_still_has_the_heading():
    html = fixture_bytes("query_results_empty").decode("utf-8")
    html = html.replace("Не найдено публикаций, соответствующих запросу", "")
    page = parse_search_results(html)
    assert page.total == 0 and page.items == []
    # ... while a page without heading, counter and result table is still rejected
    with pytest.raises(ParseError):
        parse_search_results(html.replace("РЕЗУЛЬТАТЫ ПОИСКОВОГО ЗАПРОСА", ""))


def test_client_search_with_no_hits(client, site):
    site.page("query_results.asp", "query_results_empty")
    page = client.search_publications(text="zzzz", year_from=2023)
    assert page.total == 0 and page.items == [] and page.pages == 0
    assert [r.url.path for r in site.requests] == ["/query_results.asp"]  # one run, no page fetches


# ============================================================================ org_items.asp


@pytest.fixture(scope="module")
def org_items() -> OrgPublicationsPage:
    return parse_organization_publications(fixture_bytes("org_items_18078"))


def test_org_items_counters(org_items):
    """"Всего найдено 49 публикаций с общим количеством цитирований: 12", no pager."""
    assert isinstance(org_items, OrgPublicationsPage)
    assert org_items.total == 49 and len(org_items.items) == 49
    assert (org_items.page, org_items.per_page, org_items.pages) == (1, 100, 1)
    assert not org_items.has_next
    assert org_items.total_citations == 12
    assert sum(h.citations or 0 for h in org_items.items) == 12
    assert (org_items.org_id, org_items.org_name, org_items.org_town) == (18078, "Томский политехнический техникум",
                                                                          "Томск")
    assert org_items.query == {"org_id": 18078, "show_option": "0", "show_sotr": "0", "sortorder": "0",
                               "order": "1", "orgdepid": "0", "show_refs": "0", "hide_doubles": "1"}


def test_org_items_rows(org_items):
    first = org_items.items[0]
    assert first.id == 89340898
    assert first.title == ("ФОРМИРОВАНИЕ ПРЕДПРИНИМАТЕЛЬСКИХ И ПРОФЕССИОНАЛЬНЫХ КОМПЕТЕНЦИЙ СТУДЕНТОВ ТГПУ "
                           "ЧЕРЕЗ КОМПЛЕКСНУЮ ПРАКТИЧЕСКУЮ ПОДГОТОВКУ В СПО И ВУЗЕ")
    assert first.authors == ["Копытова А.И.", "Мартыненко Д.А."]
    assert first.source.startswith("В сборнике: Организация проектной и исследовательской деятельности")
    assert first.source.endswith("Томск, 2026. С. 11-16.")
    assert (first.kind, first.year, first.citations, first.position) == ("conference", 2026, 0, 1)
    assert [h.position for h in org_items.items] == list(range(1, 50))
    assert Counter(h.kind for h in org_items.items) == {"conference": 45, "article": 4}
    assert [h.id for h in org_items.items if h.kind == "article"] == [82512258, 80427664, 42747676, 35349446]
    assert Counter(h.year for h in org_items.items) == {
        2026: 3, 2025: 2, 2024: 3, 2023: 3, 2022: 9, 2021: 9, 2020: 13, 2019: 6, 2017: 1}
    last = org_items.items[-1]
    assert (last.id, last.source, last.citations) == (35349446, "Булатовские чтения. 2017. Т. 1. С. 39-41.", 3)


def test_client_org_publications(client, site):
    site.page("org_items.asp", "org_items_18078")
    page = client.get_organization_publications(18078)
    assert page.total == 49 and page.items[0].id == 89340898 and page.org_name == "Томский политехнический техникум"
    assert site.requests[0].url.path == "/org_items.asp"
    assert ("orgsid", "18078") in list(site.requests[0].url.params.multi_items())


# ============================================================================ contents.asp


@pytest.fixture(scope="module")
def issue() -> IssueContents:
    return parse_issue_contents(fixture_bytes("contents_8559"), url="https://elibrary.ru/contents.asp?titleid=8559")


def test_issue_header(issue):
    assert issue.journal_id == 8559
    assert issue.journal_name == "Вестник Томского государственного университета"
    assert issue.publisher is not None
    assert (issue.publisher.id, issue.publisher.name, issue.publisher.city) == (
        366, "Национальный исследовательский Томский государственный университет", "Томск")
    assert issue.issue_id == 93447049
    assert issue.label == "№ 526 за 2026 год"
    assert (issue.year, issue.volume, issue.number) == (2026, None, "526")
    assert issue.edn is None  # the header of this issue shows no EDN
    assert issue.articles_count == 29
    assert issue.url == "https://elibrary.ru/contents.asp?id=93447049"
    assert issue.extra == {}


def test_issue_sections(issue):
    assert issue.sections == ["ФИЛОЛОГИЯ", "PHILOLOGY", "ФИЛОСОФИЯ", "СОЦИОЛОГИЯ И ПОЛИТОЛОГИЯ", "ИСТОРИЯ",
                              "ПЕДАГОГИКА", "ПРАВО"]
    assert Counter(h.section for h in issue.items) == {
        "ФИЛОЛОГИЯ": 5, "PHILOLOGY": 1, "ФИЛОСОФИЯ": 2, "СОЦИОЛОГИЯ И ПОЛИТОЛОГИЯ": 3, "ИСТОРИЯ": 8,
        "ПЕДАГОГИКА": 5, "ПРАВО": 5}


def test_issue_items(issue):
    assert len(issue.items) == 29
    assert all(h.kind == "article" and h.year == 2026 and h.source is None for h in issue.items)
    assert [h.position for h in issue.items] == list(range(1, 30))
    assert all(re.fullmatch(r"\d+-\d+", h.pages or "") for h in issue.items)
    first = issue.items[0]
    assert first.id == 93447050
    assert first.title == "НЕМЕЙНСТРИМНЫЙ ЖУРНАЛИСТСКИЙ ДИСКУРС ОБ ИСКУССТВЕННОМ ИНТЕЛЛЕКТЕ: СИСТЕМНО-КРИТИЧЕСКАЯ ПЕРСПЕКТИВА"
    assert (first.authors, first.pages, first.section, first.citations) == (
        ["Агнистикова Ольга Игоревна"], "5-12", "ФИЛОЛОГИЯ", 0)
    assert issue.items[3].authors == ["Руднев Дмитрий Владимирович", "Зеленин Александр Васильевич"]
    assert (issue.items[5].id, issue.items[5].section, issue.items[5].pages) == (93447055, "PHILOLOGY", "51-57")
    last = issue.items[-1]
    assert (last.id, last.pages, last.section) == (93447078, "267-273", "ПРАВО")


def test_issue_available_issues(issue):
    issues = issue.available_issues
    assert len(issues) == 279
    current = [r for r in issues if r.current]
    assert [(r.id, r.label, r.number, r.year, r.articles) for r in current] == [
        (93447049, "№ 526", "526", 2026, 29)]
    assert [(r.id, r.label, r.articles) for r in issues if r.year == 2026] == [
        (90510442, "№ 522", 29), (91759045, "№ 523", 30), (91762631, "№ 524", 27), (91772174, "№ 525", 30),
        (93447049, "№ 526", 29)]
    online_first = [r for r in issues if r.label == "Online First"]
    assert len(online_first) == 1 and online_first[0].year is None and online_first[0].id == 49632234
    years = [r.year for r in issues if r.year is not None]
    assert years == sorted(years, reverse=True)  # newest year first
    assert (issues[-1].id, issues[-1].label, issues[-1].year) == (34105784, "№ 266", 1998)
    assert issues[-1].url == "https://elibrary.ru/contents.asp?id=34105784"


def test_client_latest_issue(client, site):
    site.page("contents.asp", "contents_8559")
    contents = client.get_journal_issue(journal_id=8559)
    assert contents.issue_id == 93447049 and len(contents.items) == 29
    assert str(site.requests[0].url) == "https://elibrary.ru/contents.asp?titleid=8559"


# ============================================================================ item.asp (journal article)


@pytest.fixture(scope="module")
def article() -> Publication:
    return parse_publication(fixture_bytes("item_article_54076582"), url="https://elibrary.ru/item.asp?id=54076582")


def test_article_identity(article):
    assert article.id == 54076582
    assert article.edn == "IGWTTR"
    assert article.doi == "10.17725/rensit.2023.15.153"
    assert article.title == ("Получение частиц диоксида титана (рутила) на поверхности восстановленного оксида "
                             "графена в сверхкритическом изопропаноле")
    assert article.title_display == article.title.upper()
    assert article.title_en == ("OBTAINING OF TITANIUM DIOXIDE (RUTILE) PARTICLES ON THE SURFACE OF REDUCED "
                                "GRAPHENE OXIDE IN SUPERCRITICAL ISOPROPANOL")
    assert (article.kind, article.type_raw) == ("article", "статья в журнале - научная статья")
    assert (article.language, article.language_code) == ("русский", "ru")
    assert (article.year, article.volume, article.issue, article.pages) == (2023, "15", "2", "153-160")
    assert article.issue_id == 54076576
    assert article.udk == "546.2+546.7+546.05"
    assert article.funding == "Работа выполнена в рамках государственного задания ИОНХ РАН."
    assert article.extra == {}


def test_article_authors(article):
    assert [a.name for a in article.authors] == ["ГРОШКОВА Ю.А.", "КРАЕВСКИЙ С.В.", "БУСЛАЕВА Е.Ю."]
    assert [a.full_name for a in article.authors] == [
        "Грошкова Юлия Александровна", "Краевский Сергей Владимирович", "Буслаева Елена Юрьевна"]
    assert [a.author_id for a in article.authors] == [1261901, 603863, 108516]
    assert [a.affiliations for a in article.authors] == [[1], [2], [3]]
    assert article.authors[2].affiliation_names == ["Институт общей и неорганической химии им. Н.С. Курнакова РАН"]
    assert article.authors[1].organization == "Научно-исследовательский институт биомедицинской химии им. В.Н. Ореховича"
    # the "Об авторе" info icon next to each name (was lost before)
    assert [a.about for a in article.authors] == ["с.н.с.", "к.х.н.", "д.х.н."]
    assert not any(a.corresponding for a in article.authors)
    assert article.authors_en == ["GROSHKOVA YU.A.", "KRAEVSKII S.V.", "BUSLAEVA E.YU."]
    assert [(f.number, f.org_id, f.city) for f in article.affiliations] == [(1, None, None), (2, 648, "Москва"),
                                                                            (3, 503, "Москва")]
    assert article.affiliations[0].name == 'НПО "ГРАФЕНИКА"'
    assert article.affiliations[0].location == "Москва 107143, Российская Федерация"
    assert article.affiliations_en[2] == "Kurnakov Institute of General and Inorganic Chemistry of RAS"


def test_article_journal(article):
    j = article.journal
    assert j is not None
    assert (j.title_id, j.name, j.name_cased) == (
        32094, "РАДИОЭЛЕКТРОНИКА. НАНОСИСТЕМЫ. ИНФОРМАЦИОННЫЕ ТЕХНОЛОГИИ",
        "Радиоэлектроника. Наносистемы. Информационные технологии")
    assert (j.issn, j.eissn, j.publisher) == ("2218-3000", "2414-1267", "Российская академия естественных наук")
    assert j.contents_url == "https://elibrary.ru/contents.asp?id=54076576"
    ix = j.indexing
    assert ix is not None
    assert (ix.in_wos, ix.in_scopus, ix.scopus, ix.in_rsci) == (False, True, "да (квартиль 4)", False)
    assert (ix.white_list_level, ix.egpni_level, ix.in_vak, ix.si_percentile) == (4, 3, True, 19)


def test_article_journal_vak_category(article):
    # the card writes "Перечень ВАК: да (категория 2)", not "К2"
    ix = article.journal.indexing
    assert (ix.vak, ix.in_vak, ix.vak_category) == ("да (категория 2)", True, "К2")


def test_article_keywords_abstract_rubrics(article):
    assert article.keywords == ["ДИОКСИДЫ ТИТАНА", "РУТИЛ", "ОКСИД ГРАФЕНА", "ВОССТАНОВЛЕННЫЙ ОКСИД ГРАФЕНА"]
    assert article.keywords_en == []
    # the card has no "АННОТАЦИЯ" block (and no English abstract)
    assert article.abstract is None and article.abstract_en is None
    assert article.rubrics.oecd == ["Chemical sciences"]
    assert article.rubrics.asjc == [] and article.rubrics.grnti == [] and article.rubrics.vak == []


def test_article_metrics_dates_fulltext(article):
    m = article.metrics
    assert (m.in_rinc, m.rinc_citations, m.in_core, m.core_citations) == (True, 0, True, 0)
    assert (m.reviews, m.has_reviews) == ("есть", True)
    assert (m.views, m.views_registered, m.downloads, m.downloads_authors, m.collections) == (17, 7, 2, 1, 7)
    assert (m.ratings_count, m.rating_avg, m.comments_count) == (0, None, 0)
    assert m.extra == {}
    d = article.dates
    assert (str(d.received), str(d.accepted), str(d.placed)) == ("2023-06-20", "2023-06-25", "2023-07-02")
    # the card only offers to buy the article; the free copy on CyberLeninka is found by the
    # full-text module (by DOI / title), not shown on the card
    f = article.fulltext
    assert (f.available, f.access, f.price_rub) == (False, "paid", 333)
    assert f.files == [] and f.links == []
    assert f.notes == ["Приобрести эту публикацию за 333 руб."]


def test_article_matches_its_search_row(article, graphene):
    hit = next(h for h in graphene.items if h.id == article.id)
    assert hit.title == article.title_display
    assert hit.authors == ["Грошкова Ю.А.", "Краевский С.В.", "Буслаева Е.Ю."]
    assert hit.year == article.year


# ============================================================================ privacy

# fixture name prefix -> parser (every saved page that has a parser)
PARSERS: dict[str, Callable[[bytes], Any]] = {
    "author_items": parse_author_publications,
    "author_profile": parse_author_profile,
    "authors_results": parse_author_search,
    "contents": parse_issue_contents,
    "ispdf": parse_fulltext_flag,
    "item_": parse_publication,
    "org_items": parse_organization_publications,
    "org_profile": parse_org_profile,
    "orgs_form": parse_org_form_options,
    "orgs_results": parse_org_search,
    "query_results": parse_search_results,
    "title_about": parse_journal_profile,
    "titles_form": parse_journal_search_options,
    "titles_results": parse_journal_search,
}
HTML_FIXTURES = sorted(p.stem for p in FIXTURES.glob("*.html"))
PARSED_FIXTURES = [n for n in HTML_FIXTURES if any(n.startswith(k) for k in PARSERS)]
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
#: labels of the session box (``#win_session`` / login box): never part of a page model
SESSION_LABELS = ("IP-адрес", "Незарегистрированный пользователь", "Начало работы", "Время работы",
                  "Имя пользователя")
#: names of the eLIBRARY.RU session cookies (``auth.SESSION_COOKIE``, ``auth.GUID_COOKIE``) and ASP session ids
COOKIE_NAMES = ("SUserID", "SCookieGUID", "ASPSESSIONID")


def _parse(name: str) -> Any:
    fn = next(v for k, v in PARSERS.items() if name.startswith(k))
    return fn(fixture_bytes(name))


def _dump(result: Any) -> str:
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    return json.dumps(result, ensure_ascii=False, default=str)


def _all_fixture_files() -> list[Path]:
    return sorted([*FIXTURES.glob("*.html"), *EXTERNAL.iterdir()])


def test_fixture_inventory():
    assert {"query_results_page2_graphene", "query_results_empty", "org_items_18078", "contents_8559",
            "author_items_17783_p2", "author_profile_17783", "item_article_54076582"} <= set(HTML_FIXTURES)
    unparsed = set(HTML_FIXTURES) - set(PARSED_FIXTURES)
    assert unparsed == {"authors_form", "captcha", "querybox_form"}  # forms / captcha: no page model


@pytest.mark.parametrize("path", _all_fixture_files(), ids=lambda p: p.name)
def test_fixtures_contain_no_real_ip_and_no_cookies(path):
    text = path.read_bytes().decode("utf-8", "replace")
    for candidate in _IPV4_RE.findall(text):
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:  # "99.2.096.03" (a classification code), not an address
            continue
        assert str(ip) == DOC_IP, f"{path.name}: IP address {candidate}"
    for name in COOKIE_NAMES:
        assert name.lower() not in text.lower(), f"{path.name}: cookie {name}"
    assert not re.search(r"set-cookie\s*:", text, re.I)


@pytest.mark.parametrize("name", HTML_FIXTURES)
def test_session_box_of_every_fixture_is_anonymous(name):
    info = parse_session_info(fixture_bytes(name))
    assert info.state in ("anonymous", "captcha", "unknown")
    assert info.user_name is None and not info.logged_in
    assert info.ip_address in (None, DOC_IP)


@pytest.mark.parametrize("name", PARSED_FIXTURES)
def test_models_carry_no_session_data(name):
    """The session box (account, IP, organization by IP, session start/duration) and the server
    session number (hidden ``sid``) belong to ``auth.SessionInfo`` only, never to a page model."""
    raw = fixture_bytes(name).decode("utf-8")
    dumped = _dump(_parse(name))
    info = parse_session_info(raw)
    assert DOC_IP not in dumped
    for label in SESSION_LABELS:
        assert label not in dumped
    for value in (info.ip_address, info.session_started, info.user_name):
        if value:
            assert value not in dumped
    for sid in re.findall(r'name="sid" value="(\d+)"', raw):
        assert sid not in dumped
    for name_ in COOKIE_NAMES:
        assert name_ not in dumped


@pytest.mark.parametrize("name", PARSED_FIXTURES)
def test_models_carry_no_personal_emails(name):
    """The only e-mail addresses a page model may hold are public contacts of an organization or
    journal office; the site-wide footer addresses (support@elibrary.ru, ...) and the editorial
    staff's personal addresses (journal profile) are never copied."""
    found = _EMAIL_RE.findall(_dump(_parse(name)))
    allowed = {"title_about_8610": ["jct@ict.nsc.ru"]}  # 'КОНТАКТНАЯ ИНФОРМАЦИЯ' of the journal
    assert found == allowed.get(name, [])


def test_journal_profile_keeps_people_without_their_emails():
    raw = fixture_bytes("title_about_8610").decode("utf-8")
    personal = set(re.findall(r"mailto:([^'\"]+)", raw)) - {"jct@ict.nsc.ru"}
    assert len(personal) > 20  # the page links e-mails of the chief editor, secretary and board
    profile = parse_journal_profile(raw)
    dumped = profile.model_dump_json()
    assert not [e for e in personal if e in dumped]
    chief = profile.editor_in_chief
    assert chief is not None
    assert (chief.name, chief.role, chief.degree, chief.author_id) == (
        "Федорук Михаил Петрович", "ГЛАВНЫЙ РЕДАКТОР", "академик РАН", 4284)
    assert [a.id for a in chief.affiliations] == [34, 214]
    assert len(profile.editorial_board) == 25
    assert "email" not in type(chief).model_fields
