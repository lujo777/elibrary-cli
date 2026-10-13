"""Authors area: query models, form builders, parsers of authors.asp / author_profile.asp /
author_items.asp, and the authors form reference data. Offline: fixtures only."""

from __future__ import annotations

import json
import re
from datetime import date
from importlib.resources import files
from typing import get_args
from urllib.parse import parse_qsl

import pytest
from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser

from elibrary_cli.errors import NotFound, ParseError
from elibrary_cli.http import encode_params
from elibrary_cli.models.authors import (
    AUTHOR_ITEMS_SORT_CODES,
    AUTHOR_SORT_CODES,
    CONTEXT_RESOLVE_ORG_NAME,
    COUNTRY_ALIASES,
    METRICS_CODES,
    AuthorHit,
    AuthorProfile,
    AuthorPublicationsPage,
    AuthorPublicationsQuery,
    AuthorQuery,
    CodeType,
)
from elibrary_cli.models.base import PublicationHit, SearchPage
from elibrary_cli.parsers.authors import (
    parse_author_profile,
    parse_author_publications,
    parse_author_search,
)
from elibrary_cli.queries.authors import (
    AUTHOR_ITEMS_PATH,
    AUTHORS_PATH,
    author_items_params,
    author_items_url,
    author_profile_params,
    author_profile_url,
    build_author_items_form,
    build_author_publications_form,
    build_author_search_form,
)

from .conftest import fixture_bytes

# The body of the POST that was sent live on 2026-10-13 and returned authors_results_petrov.html.
LIVE_PETROV_BODY = (
    "authors_all=&pagenum=&authorbox_name=&selid=&orgid=&orgadminid=&townid=&regionid="
    "&surname=%D0%9F%D0%B5%D1%82%D1%80%D0%BE%D0%B2&codetype=SPIN&codevalue=&countryid=&town=&orgname="
    "&authorboxid=&rubriccode=&metrics=1&sortorder=2&order=1"
)


def _form_controls(html: bytes, form_name: str) -> list[tuple[str, str, str]]:
    """(name, tag/type, value) of the controls associated with ``form_name``, in DOM order.

    The FORM tags of elibrary sit inside tables, so the parser closes them at once; like a browser we
    associate every control that follows the form start tag (up to the next form) with it."""
    tree = LexborHTMLParser(html.decode("utf-8"))
    out: list[tuple[str, str, str]] = []
    active = False
    for el in tree.css("form, input, select, textarea"):
        if el.tag == "form":
            active = el.attributes.get("name") == form_name
            continue
        if not active:
            continue
        name = el.attributes.get("name") or ""
        kind = el.tag if el.tag != "input" else (el.attributes.get("type") or "text").lower()
        out.append((name, kind, el.attributes.get("value") or ""))
    return out


@pytest.fixture(scope="module")
def petrov() -> SearchPage[AuthorHit]:
    return parse_author_search(fixture_bytes("authors_results_petrov"))


@pytest.fixture(scope="module")
def profile() -> AuthorProfile:
    return parse_author_profile(fixture_bytes("author_profile_3771"))


@pytest.fixture(scope="module")
def items() -> AuthorPublicationsPage:
    return parse_author_publications(fixture_bytes("author_items_3771"))


# --------------------------------------------------------------------------- AuthorQuery


def test_query_defaults_match_the_form():
    q = AuthorQuery(surname="Петров")
    assert (q.code_type, q.metrics, q.sort, q.order, q.page) == ("SPIN", "rinc", "citations", "desc", 1)
    assert not (q.in_affiliation or q.rubric_in_profile or q.only_with_publications)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"country": "RUS"},
        {"town": "Москва", "rubric_code": "290000"},
        {"org_name": "МГУ"},
        {"surname": "   "},
        {"code_type": "ORCID"},
    ],
)
def test_query_needs_a_selective_criterion(kwargs):
    with pytest.raises(ValidationError) as exc:
        AuthorQuery(**kwargs)
    assert "Укажите фамилию автора" in str(exc.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"surname": "Петров"},
        {"code_type": "SPIN", "code_value": "9846-9426"},
        {"code_type": "RSCI", "code_value": "3771"},
        {"org_id": 169},
        {"author_group": "r5"},
    ],
)
def test_query_accepts_each_criterion(kwargs):
    AuthorQuery(**kwargs)


def test_query_normalisation_and_validation():
    q = AuthorQuery(surname="  Петров \xa0 Владимир  ", country="rus", rubric_code=290000, code_value=" ")
    assert q.surname == "Петров Владимир"
    assert q.country == "RUS"
    assert q.rubric_code == "290000"
    assert q.code_value is None
    assert AuthorQuery(surname="X", rubric_code="050000").rubric_code == "050000"
    # country names are resolved to the code (see test_query_country_names); unknown ones are rejected
    assert AuthorQuery(surname="Петров", country="Russia").country == "RUS"
    with pytest.raises(ValidationError, match="ISO 3166"):
        AuthorQuery(surname="Петров", country="Атлантида")
    with pytest.raises(ValidationError, match="ГРНТИ"):
        AuthorQuery(surname="Петров", rubric_code="29")
    with pytest.raises(ValidationError, match="только из цифр"):
        AuthorQuery(code_type="RSCI", code_value="12a")
    with pytest.raises(ValidationError):
        AuthorQuery(surname="Петров", page=0)
    with pytest.raises(ValidationError):
        AuthorQuery(surname="Петров", code_type="WOS")
    with pytest.raises(ValidationError):
        AuthorQuery(surname="Петров", sort="year")


@pytest.mark.parametrize(
    ("given", "code"),
    [
        ("RUS", "RUS"), ("kaz", "KAZ"), ("Россия", "RUS"), ("россия", "RUS"), ("Беларусь", "BLR"),
        ("Белоруссия", "BLR"), ("Республика Корея", "KOR"), ("Корея", "KOR"), ("США", "USA"),
        ("Великобритания", "GBR"), ("Южно-Африканская Республика", "ZAF"),
        ("Russia", "RUS"), ("russian federation", "RUS"), ("Kazakhstan", "KAZ"), ("Germany", "DEU"),
        ("United States", "USA"), ("UK", "GBR"), ("UAE", "ARE"), ("South Korea", "KOR"), ("Czechia", "CZE"),
        ("Kyrgyzstan", "KGZ"), ("  Viet  Nam ", "VNM"),
        ("Казах", "KAZ"), ("Uzbek", "UZB"),  # a unique prefix
        ("XKX", "XKX"),  # a valid-looking ISO3 code missing from the snapshot is passed through
    ],
)
def test_query_country_names(given, code):
    assert AuthorQuery(surname="Петров", country=given).country == code
    assert dict(build_author_search_form(AuthorQuery(surname="Петров", country=given)))["countryid"] == code


def test_query_country_unknown_or_ambiguous():
    with pytest.raises(ValidationError, match="ISO 3166") as exc:
        AuthorQuery(surname="Петров", country="Ир")  # Ирак, Иран, Ирландия
    assert "Иран (IRN)" in str(exc.value) and "Ирак (IRQ)" in str(exc.value)
    with pytest.raises(ValidationError, match="неизвестная страна"):
        AuthorQuery(surname="Петров", country="Narnia")
    with pytest.raises(ValidationError):
        AuthorQuery(surname="Петров", country="R1")


def test_every_form_country_label_resolves_to_its_code():
    opts = json.loads(files("elibrary_cli.data").joinpath("authors_options.json").read_text(encoding="utf-8"))
    for o in opts["countryid"]:
        assert AuthorQuery(surname="Петров", country=o["label"]).country == o["value"], o
    assert set(COUNTRY_ALIASES) == {o["value"] for o in opts["countryid"]}  # an English name for every country


def test_query_org_name_counts_only_with_the_resolving_context():
    with pytest.raises(ValidationError, match="Укажите фамилию автора"):
        AuthorQuery(org_name="Томский политехнический университет")
    q = AuthorQuery.model_validate({"org_name": "Томский политехнический университет"},
                                   context={CONTEXT_RESOLVE_ORG_NAME: True})
    assert (q.org_name, q.org_id) == ("Томский политехнический университет", None)
    with pytest.raises(ValidationError, match="Укажите фамилию автора"):  # the context alone is not a criterion
        AuthorQuery.model_validate({"town": "Томск"}, context={CONTEXT_RESOLVE_ORG_NAME: True})


# --------------------------------------------------------------------------- build_author_search_form


def test_search_form_reproduces_the_live_request():
    form = build_author_search_form(AuthorQuery(surname="Петров"))
    assert encode_params(form) == LIVE_PETROV_BODY
    assert AUTHORS_PATH == "/authors.asp"


def test_search_form_field_order_is_the_dom_order_of_authors_form():
    controls = _form_controls(fixture_bytes("authors_form"), "results")
    dom_names = [name for name, _kind, _value in controls]
    assert dom_names == [
        "authors_all", "pagenum", "authorbox_name", "selid", "orgid", "orgadminid", "townid", "regionid",
        "surname", "codetype", "codevalue", "countryid", "town", "inaffil", "orgname", "authorboxid",
        "rubricinreg", "rubriccode", "metrics", "show_nopubl", "sortorder", "order",
    ]
    checkboxes = {name for name, kind, _ in controls if kind == "checkbox"}
    assert checkboxes == {"inaffil", "rubricinreg", "show_nopubl"}
    q = AuthorQuery(surname="Петров", in_affiliation=True, rubric_in_profile=True, only_with_publications=True)
    assert [name for name, _ in build_author_search_form(q)] == dom_names
    # without checkboxes they are simply absent, everything else keeps its order
    plain = [name for name, _ in build_author_search_form(AuthorQuery(surname="Петров"))]
    assert plain == [n for n in dom_names if n not in checkboxes]


def test_search_form_values():
    q = AuthorQuery(
        surname="Петров В А", code_type="ORCID", code_value="0000-0002-1984-3580", country="RUS",
        town="Протвино", town_id=1234, region_id=50, org_id=169, org_name="ИФВЭ", in_affiliation=True,
        author_group="r4", rubric_code="290000", rubric_in_profile=True, metrics="core", sort="hindex",
        order="asc", only_with_publications=True, page=3,
    )
    form = build_author_search_form(q)
    assert form == [
        ("authors_all", ""), ("pagenum", "3"), ("authorbox_name", ""), ("selid", ""), ("orgid", "169"),
        ("orgadminid", ""), ("townid", "1234"), ("regionid", "50"), ("surname", "Петров В А"),
        ("codetype", "ORCID"), ("codevalue", "0000-0002-1984-3580"), ("countryid", "RUS"),
        ("town", "Протвино"), ("inaffil", "on"), ("orgname", "ИФВЭ"), ("authorboxid", "r4"),
        ("rubricinreg", "on"), ("rubriccode", "290000"), ("metrics", "0"), ("show_nopubl", "on"),
        ("sortorder", "3"), ("order", "0"),
    ]
    # explicit page argument wins; page 1 is sent as an empty pagenum like author_search()
    assert dict(build_author_search_form(q, page=1))["pagenum"] == ""
    assert dict(build_author_search_form(q, page=44))["pagenum"] == "44"
    by_name = dict(build_author_search_form(AuthorQuery(surname="Петров", sort="name", metrics="elibrary")))
    assert (by_name["sortorder"], by_name["metrics"], by_name["order"]) == ("0", "2", "1")


def test_search_form_select_values_exist_in_the_form():
    controls = _form_controls(fixture_bytes("authors_form"), "results")
    assert [n for n, k, _ in controls if k == "select"] == [
        "codetype", "countryid", "authorboxid", "rubriccode", "metrics", "sortorder", "order"]
    tree = LexborHTMLParser(fixture_bytes("authors_form").decode("utf-8"))

    def values(name: str) -> set[str]:
        sel = tree.css(f'select[name="{name}"]')[-1]
        return {o.attributes.get("value") or "" for o in sel.css("option")}

    assert set(get_args(CodeType)) == values("codetype")
    assert set(METRICS_CODES.values()) == values("metrics")
    assert set(AUTHOR_SORT_CODES.values()) == values("sortorder")


# --------------------------------------------------------------------------- author_items builders


def test_author_items_form_matches_the_page_form():
    controls = _form_controls(fixture_bytes("author_items_3771"), "results")
    page_fields = [(n, k, v) for n, k, v in controls if not re.match(r"^(tid|pid)\d+$", n)]
    assert [n for n, _, _ in page_fields] == [
        "itembox_name", "auth", "authorid", "authorhash", "pagenum", "add_all", "paysum", "slight",
        "show_refs", "hide_doubles", "items_all", "publs_all", "did", "urlnum", "listid", "rubric_order",
        "title_order", "org_order", "author_order", "year_order", "type_order", "cat_order", "role_order",
        "keyword_order", "show_hash", "check_show_refs", "check_hide_doubles", "sortorder", "order",
    ]
    form = build_author_items_form(3771, 2, author_hash="Петров В А")
    assert [n for n, _ in form] == [n for n, _, _ in page_fields]
    values = dict(form)
    hidden = {n: v for n, k, v in page_fields if k == "hidden"}
    for name, value in hidden.items():
        expected = "2" if name == "pagenum" else value
        assert values[name] == expected, name
    assert values["check_show_refs"] == values["check_hide_doubles"] == "on"
    assert (values["sortorder"], values["order"], values["show_hash"]) == ("0", "1", "0")
    assert (values["year_order"], values["cat_order"], values["rubric_order"]) == ("1", "1", "0")


def test_author_items_form_options():
    form = build_author_items_form(
        3771, 5, sort="citations", order="asc", include_references=False, merge_versions=False, unlinked=True)
    values = dict(form)
    names = [n for n, _ in form]
    assert "check_show_refs" not in names and "check_hide_doubles" not in names
    assert (values["pagenum"], values["show_refs"], values["hide_doubles"], values["show_hash"]) == ("5", "0", "0", "1")
    assert (values["sortorder"], values["order"]) == ("3", "0")
    assert dict(build_author_items_form(3771, 0))["pagenum"] == "1"
    with pytest.raises(ValueError):
        build_author_items_form(3771, sort="hindex")
    with pytest.raises(ValueError):
        build_author_items_form(3771, order="up")
    q = AuthorPublicationsQuery(author_id=3771, page=4, sort="year_title", unlinked=True)
    assert build_author_publications_form(q, author_hash="Петров В А") == build_author_items_form(
        3771, 4, sort="year_title", unlinked=True, author_hash="Петров В А")
    assert set(AUTHOR_ITEMS_SORT_CODES.values()) == {"0", "1", "2", "3", "4", "5", "6", "7"}
    assert AUTHOR_ITEMS_PATH == "/author_items.asp"


def test_author_items_and_profile_get_params():
    assert author_items_params(3771) == [("authorid", "3771"), ("show_refs", "1")]
    params = author_items_params(3771, pubcat="risc", pubrole=100, pubyear=[2021, 2022, 2023])
    assert dict(params) == {"authorid": "3771", "pubyear": "2021|2022|2023", "pubcat": "risc",
                            "show_refs": "1", "pubrole": "100"}
    assert dict(parse_qsl(encode_params(params)))["pubyear"] == "2021|2022|2023"
    assert dict(author_items_params(3771, pubyear=2025, show_refs=False))["show_refs"] == "0"
    assert author_items_url(3771) == "https://elibrary.ru/author_items.asp?authorid=3771&show_refs=1"
    assert author_profile_params(3771) == [("id", "3771")]
    assert author_profile_url(3771) == "https://elibrary.ru/author_profile.asp?id=3771"


# --------------------------------------------------------------------------- parse_author_search


def test_search_totals_and_paging(petrov):
    assert petrov.total == 4342
    assert petrov.total_in_database == 1170544
    assert petrov.page == 1
    assert petrov.pages == 44
    assert petrov.per_page == 100
    assert len(petrov.items) == 100
    assert petrov.has_next
    assert [h.position for h in petrov.items] == list(range(1, 101))
    assert petrov.source_url == "https://elibrary.ru/authors.asp"


def test_search_first_row(petrov):
    h = petrov.items[0]
    assert h.id == 3771
    assert h.name == "Петров Владимир Алексеевич"
    assert h.short_name == "Петров В А"
    assert h.science_index_registered is True
    assert h.affiliation == 'Институт физики высоких энергий им. А.А. Логунова НИЦ "Курчатовский институт"'
    assert h.town == "Протвино"
    assert (h.publications, h.citations, h.hindex) == (1069, 86835, 119)
    assert h.url == h.profile_url == "https://elibrary.ru/author_profile.asp?id=3771"
    assert h.publications_url == "https://elibrary.ru/author_items.asp?authorid=3771&show_refs=1"
    assert h.wikipedia_url is None


def test_search_row_variants(petrov):
    by_id = {h.id: h for h in petrov.items}
    m = by_id[20587]
    assert (m.name, m.science_index_registered, m.town) == ("Петров Михаил Петрович", False, "Санкт-Петербург")
    assert m.affiliation == "Физико-технический институт им. А.Ф. Иоффе РАН"
    w = by_id[1173040]
    assert w.wikipedia_url == "https://ru.wikipedia.org/wiki/Петровский,_Артур_Владимирович"
    assert w.short_name == "Петровский А В"
    # parentheses inside the organization name are kept, only the trailing town is split off
    assert (by_id[664590].affiliation, by_id[664590].town) == ("Университет Уэйна (Детройт, США)", "Детройт")
    assert by_id[407440].affiliation == "Российский университет транспорта (МИИТ)"
    assert by_id[184626].affiliation.endswith('"ЛЭТИ" им. В.И. Ульянова (Ленина)')
    last = petrov.items[-1]
    assert (last.id, last.name, last.position) == (53238, "Петров Александр Анатольевич", 100)
    assert (last.publications, last.citations, last.hindex, last.town) == (171, 2083, 14, "Санкт-Петербург")


def test_search_aggregates(petrov):
    assert sum(h.science_index_registered for h in petrov.items) == 73
    assert sum(bool(h.wikipedia_url) for h in petrov.items) == 14
    assert sum(h.town == "Москва" for h in petrov.items) == 42
    assert all(h.short_name and h.affiliation and h.town for h in petrov.items)
    # sortorder=2 order=1: by citations, descending
    cits = [h.citations for h in petrov.items]
    assert cits == sorted(cits, reverse=True)
    assert len({h.id for h in petrov.items}) == 100


def test_search_keeps_the_query_and_page_hint():
    q = AuthorQuery(surname="Петров")
    page = parse_author_search(fixture_bytes("authors_results_petrov"), page=7, query=q)
    assert page.query["surname"] == "Петров"
    assert page.query["sort"] == "citations"
    assert page.page == 1  # the page itself says it is page 1
    page = parse_author_search(fixture_bytes("authors_results_petrov"), query={"surname": "Петров"},
                               url="https://elibrary.ru/authors.asp?x=1")
    assert page.query == {"surname": "Петров"}
    assert page.source_url == "https://elibrary.ru/authors.asp?x=1"


def test_search_empty_form_page():
    page = parse_author_search(fixture_bytes("authors_form"))
    assert page.items == []
    assert page.total == 0
    assert page.pages == 0
    assert not page.has_next


def test_search_later_page_synthetic():
    html = """<html><head><title>Поиск авторов</title></head><body>
    <form name="results"><input type="text" name="surname" value="Петров"></form>
    <table><tr><td><font>Всего найдено авторов: <b><font>4342</font></b> из <b>1170544</b>.
    Показано на данной странице: с <b>201</b> по <b>300</b>.</font></td></tr></table>
    <table id="restab">
    <tr valign=top id="a42"><td><b>201.</b></td><td><input type=checkbox name="tid42"></td>
    <td><font><b>Петрова&nbsp; Анна&nbsp; Сергеевна</b></font><br>
    Институт (Москва)</td>
    <td><a href="author_items.asp?authorid=42&show_refs=1">10</a><a href="author_profile.asp?id=42">
    <img src="/pic/stat.gif"></a></td><td><a href="javascript:show_author_refs(42, 'risc')">5</a></td>
    <td>2</td></tr>
    <input type="hidden" name="hid42" value="Петрова А С">
    </table>
    <div id="pages"><table><tr>
    <td><a href="javascript:goto_page(1)">В&nbsp;начало</a></td>
    <td><a href="javascript:goto_page(2)">&lt;&lt;</a></td>
    <td><a href="javascript:goto_page(2)">2</a></td><td><font color=#F26C4F>3</font></td>
    <td><a href="javascript:goto_page(4)">4</a></td>
    <td><a href="javascript:goto_page(44)">В&nbsp;конец</a></td></tr></table></div>
    </body></html>"""
    page = parse_author_search(html)
    assert (page.page, page.pages, page.per_page, page.total) == (3, 44, 100, 4342)
    (hit,) = page.items
    assert (hit.id, hit.name, hit.short_name, hit.position) == (42, "Петрова Анна Сергеевна", "Петрова А С", 201)
    assert (hit.affiliation, hit.town, hit.science_index_registered) == ("Институт", "Москва", False)
    assert (hit.publications, hit.citations, hit.hindex) == (10, 5, 2)


@pytest.mark.parametrize(
    "fixture", ["query_results_all_types", "orgs_results_mgu", "item_article_46371463", "captcha",
                "author_profile_3771"],
)
def test_search_rejects_other_pages(fixture):
    with pytest.raises(ParseError):
        parse_author_search(fixture_bytes(fixture))


# --------------------------------------------------------------------------- parse_author_profile


def test_profile_identity(profile):
    assert profile.id == 3771
    assert profile.name == "Петров Владимир Алексеевич"
    assert profile.science_index_registered is True
    assert profile.spin == "9846-9426"
    assert profile.orcid == "0000-0002-1984-3580"
    assert profile.scopus_id == "13310351400"
    assert profile.researcher_id is None
    assert profile.external_profiles == {
        "ORCID": "https://orcid.org/0000-0002-1984-3580",
        "Scopus": "https://www.scopus.com/authid/detail.url?authorId=13310351400",
    }
    assert profile.url == "https://elibrary.ru/author_profile.asp?id=3771"
    assert profile.publications_url == "https://elibrary.ru/author_items.asp?authorid=3771&show_refs=1"
    assert profile.citing_publications_url == "https://elibrary.ru/cit_author_items.asp?id=3771"
    assert profile.metrics_updated == date(2026, 10, 7)
    assert profile.extra == {}


def test_profile_header_affiliation(profile):
    aff = profile.affiliation
    assert aff is not None
    assert aff.name == 'Институт физики высоких энергий им. А.А. Логунова НИЦ "Курчатовский институт"'
    assert (aff.org_id, aff.town, aff.department) == (169, "Протвино", None)
    assert aff.url == "https://elibrary.ru/org_profile.asp?id=169"


def test_profile_affiliations_table(profile):
    affs = profile.affiliations
    assert len(affs) == 20
    first = affs[0]
    assert first.name == "Институт геологии рудных месторождений, петрографии, минералогии и геохимии РАН"
    assert (first.org_id, first.town, first.period, first.year_from, first.year_to, first.publications) == (
        386, "Москва", "2021-2026", 2021, 2026, 86)
    assert first.publications_url == "https://elibrary.ru/author_items.asp?orgsid=386"
    ihep = next(a for a in affs if a.org_id == 169)
    assert (ihep.period, ihep.publications, ihep.town) == ("1973-2023", 654, "Протвино")
    single = next(a for a in affs if a.org_id == 174)
    assert (single.period, single.year_from, single.year_to) == ("2023", 2023, 2023)
    assert next(a for a in affs if a.org_id == 116915).name == "European Organization for Nuclear Research"
    assert affs[-1].name == "Новосибирский институт органической химии им. Н.Н. Ворожцова СО РАН"
    assert (affs[-1].org_id, affs[-1].town, affs[-1].publications) == (444, "Новосибирск", 3)
    assert sum(a.publications for a in affs) == 880


def test_profile_main_indicators(profile):
    p = profile
    assert (p.publications_elibrary, p.publications_rinc, p.publications_core) == (1094, 1069, 979)
    assert (p.citations_elibrary, p.citations_rinc, p.citations_core) == (87466, 86835, 84993)
    assert (p.hindex_elibrary, p.hindex_rinc, p.hindex_core) == (119, 119, 117)
    assert (p.citing_publications, p.top_publication_citations, p.cited_publications) == (51648, 7925, 929)
    assert p.citations_per_publication == pytest.approx(83.23)
    assert (p.hindex_without_self_citations, p.hindex_core_without_self_citations,
            p.hindex_journal_articles) == (114, 112, 109)
    assert p.first_publication_year == 1972
    assert (p.self_citations, p.coauthor_citations, p.coauthors) == (3866, 47049, 12774)
    assert (p.articles_foreign_journals, p.articles_russian_journals, p.articles_vak_journals,
            p.articles_translated_journals, p.articles_impact_factor_journals) == (849, 150, 137, 52, 886)
    assert (p.citations_from_foreign_journals, p.citations_from_russian_journals, p.citations_from_vak_journals,
            p.citations_from_translated_journals, p.citations_from_impact_factor_journals) == (
        74759, 2545, 2456, 1625, 68461)
    assert p.weighted_impact_factor_published == pytest.approx(4.383)
    assert p.weighted_impact_factor_cited == pytest.approx(4.616)
    assert p.five_year_period == "2021-2025"
    assert (p.publications_rinc_5y, p.publications_core_5y, p.citations_rinc_5y, p.citations_core_5y,
            p.citations_all_5y) == (88, 50, 3063, 2995, 24009)
    assert p.main_rubric_grnti == "290000. Физика"
    assert p.main_rubric_oecd == "103. Physical sciences and astronomy"
    assert p.core_percentile == 1
    assert p.roles == {"автор": 1046, "редактор": 1, "научный руководитель": 1, "научный консультант": 1}


def test_profile_keeps_every_indicator_row(profile):
    names = [m.name for m in profile.metrics]
    assert len(names) == 45
    assert names[0] == "Число публикаций на elibrary.ru"
    assert "Число публикаций в РИНЦ за последние 5 лет (2021-2025)" in names
    assert names[-5:] == [
        "Участие в публикациях", "Участие в публикациях: автор", "Участие в публикациях: редактор",
        "Участие в публикациях: научный руководитель", "Участие в публикациях: научный консультант",
    ]
    by_name = {m.name: m for m in profile.metrics}
    cited = by_name["Число публикаций автора, процитированных хотя бы один раз"]
    assert (cited.value, cited.number, cited.percent, cited.url) == ("929 (86,9%)", 929, 86.9, None)
    vak = by_name["Число статей в российских журналах из перечня ВАК"]
    assert (vak.value, vak.number, vak.percent) == ("137 (12,8%)", 137, 12.8)
    assert vak.url == "https://elibrary.ru/author_items.asp?authorid=3771&pubrole=100&show_refs=1&pubcat=vak"
    core = by_name["Число публикаций, входящих в ядро РИНЦ"]
    assert core.url == "https://elibrary.ru/author_items.asp?authorid=3771&pubcat=corerisc&show_refs=1&pubrole=100"
    assert by_name["Среднее число цитирований в расчете на одну публикацию"].number == pytest.approx(83.23)
    rubric = by_name["Основная рубрика (ГРНТИ)"]
    assert (rubric.value, rubric.number) == ("290000. Физика", None)
    assert by_name["Участие в публикациях: автор"].url == (
        "https://elibrary.ru/author_items.asp?authorid=3771&pubrole=100&show_refs=0&pubcat=risc")


def test_profile_yearly_table(profile):
    assert profile.years[:3] == [2025, 2024, 2023]
    assert profile.years[-1] == 1972
    assert len(profile.years) == 54
    names = [y.name for y in profile.yearly]
    assert names == [
        "Число публикаций в РИНЦ", "Число публикаций в ядре РИНЦ", "Число цитирований в РИНЦ",
        "Число цитирований из ядра РИНЦ", "Число публикаций в РИНЦ за 5 лет",
        "Число публикаций в ядре РИНЦ за 5 лет", "Число цитирований в РИНЦ за 5 лет",
        "Число цитирований из ядра РИНЦ за 5 лет", "Индекс Хирша в РИНЦ", "Индекс Хирша по ядру РИНЦ",
        "Процентиль по ядру РИНЦ",
    ]
    rows = {y.name: y.values for y in profile.yearly}
    pubs = rows["Число публикаций в РИНЦ"]
    assert (pubs[2025], pubs[2019], pubs[1979], pubs[1972]) == (24, 77, 0, 1)
    assert len(pubs) == 54
    assert rows["Число цитирований в РИНЦ"][2025] == 3310
    assert rows["Индекс Хирша в РИНЦ"][2025] == profile.hindex_rinc == 119
    assert rows["Число публикаций в РИНЦ за 5 лет"][2025] == profile.publications_rinc_5y
    pct = rows["Процентиль по ядру РИНЦ"]
    assert pct[2025] == 1 and pct[1972] is None
    # JSON round trip keeps the year keys
    dumped = json.loads(profile.model_dump_json())
    assert dumped["yearly"][0]["values"]["2025"] == 24


def test_profile_reports(profile):
    keys = [r.key for r in profile.reports]
    assert len(keys) == 22 and len(set(keys)) == 22
    assert keys[:3] == ["rubrics", "keywords", "titles"]
    assert "ref_rubrics" in keys and "cit_types" in keys
    first = profile.reports[0]
    assert first.title == "Распределение публикаций по тематике"
    assert first.url == "https://elibrary.ru/author_profile_new_rubrics.asp?id=3771"
    ref_rubrics = next(r for r in profile.reports if r.key == "ref_rubrics")
    assert ref_rubrics.title == "Распределение цитирований по тематике цитирующих публикаций"


@pytest.fixture(scope="module")
def ivanov() -> AuthorProfile:
    return parse_author_profile(fixture_bytes("author_profile_17783"))


def test_profile_17783_identity(ivanov):
    p = ivanov
    assert (p.id, p.name, p.science_index_registered) == (17783, "Иванов Юрий Федорович", True)
    assert (p.spin, p.orcid, p.researcher_id, p.scopus_id) == (
        "7576-4810", "0000-0001-8022-7958", "A-9268-2014", "56197197400")
    assert p.external_profiles == {
        "ORCID": "https://orcid.org/0000-0001-8022-7958",
        "ResearcherID": "https://www.webofscience.com/wos/author/record/A-9268-2014",
        "Scopus": "https://www.scopus.com/authid/detail.url?authorId=56197197400",
    }
    assert p.url == "https://elibrary.ru/author_profile.asp?id=17783"
    assert p.citing_publications_url == "https://elibrary.ru/cit_author_items.asp?id=17783"
    assert p.metrics_updated == date(2026, 7, 7)
    assert p.extra == {}
    # header: organization link, department after it, town in parentheses
    aff = p.affiliation
    assert (aff.name, aff.org_id, aff.town) == ("Институт сильноточной электроники СО РАН", 116, "Томск")
    assert aff.department == "лаборатория плазменной эмиссионной электроники"


def test_profile_17783_affiliations(ivanov):
    affs = ivanov.affiliations
    assert len(affs) == 21
    assert [a.org_id for a in affs[:4]] == [116, 573, 915, 2378]
    by_id = {a.org_id: a for a in affs}
    tpu = by_id[573]
    assert tpu.name == "Национальный исследовательский Томский политехнический университет"
    assert (tpu.town, tpu.period, tpu.year_from, tpu.year_to, tpu.publications) == ("Томск", "1995-2024", 1995, 2024, 396)
    assert tpu.publications_url == "https://elibrary.ru/author_items.asp?orgsid=573"
    assert tpu.url == "https://elibrary.ru/org_profile.asp?id=573"
    hcei = by_id[116]
    assert (hcei.period, hcei.publications, hcei.department) == ("1992-2026", 1679, None)
    assert (by_id[915].name, by_id[915].town, by_id[915].publications) == (
        "Сибирский государственный индустриальный университет", "Новокузнецк", 124)
    assert (by_id[2378].name, by_id[2378].town, by_id[2378].period) == ("Сибирское отделение РАН", "Новосибирск",
                                                                        "1995-2023")
    assert (by_id[401].period, by_id[401].year_from, by_id[401].year_to) == ("2023", 2023, 2023)
    assert (affs[-1].org_id, affs[-1].name, affs[-1].town, affs[-1].publications) == (
        369, "Омский государственный университет им. Ф.М. Достоевского", "Омск", 1)
    assert sum(a.publications for a in affs) == 2507


def test_profile_17783_metrics(ivanov):
    p = ivanov
    assert (p.publications_elibrary, p.publications_rinc, p.publications_core) == (2583, 2515, 1500)
    assert (p.citations_elibrary, p.citations_rinc, p.citations_core) == (21818, 20878, 12859)
    assert (p.hindex_elibrary, p.hindex_rinc, p.hindex_core) == (51, 50, 39)
    assert (p.citing_publications, p.top_publication_citations, p.cited_publications) == (8633, 653, 1655)
    assert p.citations_per_publication == pytest.approx(7.38)
    assert (p.hindex_without_self_citations, p.hindex_core_without_self_citations,
            p.hindex_journal_articles) == (41, 31, 37)
    assert (p.first_publication_year, p.self_citations, p.coauthor_citations, p.coauthors) == (1982, 7764, 15134, 1775)
    assert (p.articles_foreign_journals, p.articles_russian_journals, p.articles_vak_journals,
            p.articles_translated_journals, p.articles_impact_factor_journals) == (263, 1203, 1170, 473, 1353)
    assert (p.citations_from_foreign_journals, p.citations_from_russian_journals, p.citations_from_vak_journals,
            p.citations_from_translated_journals, p.citations_from_impact_factor_journals) == (
        4847, 7917, 7685, 2425, 11590)
    assert p.weighted_impact_factor_published == pytest.approx(0.811)
    assert p.weighted_impact_factor_cited == pytest.approx(1.528)
    assert (p.five_year_period, p.publications_rinc_5y, p.publications_core_5y, p.citations_rinc_5y,
            p.citations_core_5y, p.citations_all_5y) == ("2021-2025", 556, 310, 1897, 1124, 6023)
    assert (p.main_rubric_grnti, p.main_rubric_oecd, p.core_percentile) == (
        "290000. Физика", "103. Physical sciences and astronomy", 2)
    assert p.roles == {"автор": 2570, "ответственный редактор": 2, "редактор": 4, "рецензент": 5,
                       "научный руководитель": 15, "официальный оппонент": 33, "руководитель НИР": 6,
                       "патентообладатель": 1}
    assert len(p.metrics) == 49
    assert (len(p.years), p.years[0], p.years[-1]) == (44, 2025, 1982)
    rows = {y.name: y.values for y in p.yearly}
    assert len(rows) == 11
    pubs = rows["Число публикаций в РИНЦ"]
    assert (pubs[2025], pubs[2024], pubs[1983], pubs[1982]) == (29, 93, 0, 1)
    assert rows["Индекс Хирша в РИНЦ"][2025] == p.hindex_rinc == 50
    assert len(p.reports) == 22


def test_profile_url_argument():
    p = parse_author_profile(fixture_bytes("author_profile_3771"), url="https://elibrary.ru/author_profile.asp?id=3771")
    assert p.url == "https://elibrary.ru/author_profile.asp?id=3771"


_PROFILE_VARIANT = """<html><head><title>Ситковский Арсений Михайлович / анализ публикационной активности</title>
</head><body>
<div class="header-panel"><b>АНАЛИЗ ПУБЛИКАЦИОННОЙ АКТИВНОСТИ АВТОРА</b></div>
<div style="width:540px;">
<font color=#F26C4F><b>СИТКОВСКИЙ АРСЕНИЙ МИХАЙЛОВИЧ</b></font>
<br><a href="org_profile.asp?id=1432">Федеральный научно-исследовательский социологический центр РАН</a>, Институт социальной демографии (Москва)
<br>AuthorID: <font color=#00008f>1012909</font>
</div>
<div class="midtext"><font><b>ОБЩИЕ ПОКАЗАТЕЛИ</b></font></div>
<table>
<tr><td></td><td><b>Название показателя</b></td><td><b>Значение</b></td></tr>
<tr><td><img src="/q.gif"></td><td><font>Число публикаций на elibrary.ru</font></td><td><a href="author_items.asp?authorid=1012909&show_refs=1">60</a></td></tr>
<tr><td><img src="/q.gif"></td><td><font>Число цитирований из зарубежных журналов на публикации автор</font></td><td>5 (1,7%)</td></tr>
<tr><td><img src="/q.gif"></td><td><font>Новый показатель</font></td><td>42</td></tr>
<tr><td><img src="/q.gif"></td><td><font>Число публикаций в РИНЦ за последние 5 лет (2020-2024)</font></td><td>20 (34,5%)</td></tr>
</table>
<a href="https://www.webofscience.com/wos/author/record/AAG-1530-2021">Профиль автора в ResearcherID</a>
<font>Дата обновления показателей автора: 29.05.2026</font>
</body></html>"""


def test_profile_variant_department_researcherid_no_spin():
    p = parse_author_profile(_PROFILE_VARIANT)
    assert (p.id, p.name, p.spin, p.science_index_registered) == (
        1012909, "Ситковский Арсений Михайлович", None, False)
    assert p.affiliation.name == "Федеральный научно-исследовательский социологический центр РАН"
    assert (p.affiliation.org_id, p.affiliation.department, p.affiliation.town) == (
        1432, "Институт социальной демографии", "Москва")
    assert p.researcher_id == "AAG-1530-2021"
    assert p.external_profiles == {"ResearcherID": "https://www.webofscience.com/wos/author/record/AAG-1530-2021"}
    assert p.publications_elibrary == 60
    assert p.citations_from_foreign_journals == 5  # truncated label is still recognised
    assert p.extra == {"Новый показатель": "42"}  # unknown labels are kept
    assert (p.five_year_period, p.publications_rinc_5y) == ("2020-2024", 20)
    assert p.metrics_updated == date(2026, 5, 29)
    assert p.affiliations == [] and p.yearly == [] and p.years == []


def test_profile_name_falls_back_to_the_header():
    html = _PROFILE_VARIANT.replace(
        "<title>Ситковский Арсений Михайлович / анализ публикационной активности</title>", "")
    assert parse_author_profile(html).name == "Ситковский Арсений Михайлович"


def test_profile_errors():
    with pytest.raises(NotFound):
        parse_author_profile("<html><body><p>Автор не найден</p></body></html>")
    for fixture in ("org_profile_2541", "item_article_46371463", "captcha", "authors_results_petrov"):
        with pytest.raises(ParseError):
            parse_author_profile(fixture_bytes(fixture))
    no_id = _PROFILE_VARIANT.replace("<br>AuthorID: <font color=#00008f>1012909</font>", "")
    with pytest.raises(ParseError):
        parse_author_profile(no_id)
    assert parse_author_profile(no_id, url="https://elibrary.ru/author_profile.asp?id=77").id == 77


# --------------------------------------------------------------------------- parse_author_publications


def test_publications_totals(items):
    assert isinstance(items, SearchPage)
    assert items.total == 1094
    assert items.total_citations == 86020
    assert (items.page, items.pages, items.per_page) == (1, 11, 100)
    assert items.has_next
    assert items.author_id == 3771
    assert items.author_name == "Петров Владимир Алексеевич"
    assert items.author_short_name == "Петров В А"
    assert items.affiliation.org_id == 169
    assert items.affiliation.town == "Протвино"
    assert items.source_url == "https://elibrary.ru/author_items.asp?authorid=3771&show_refs=1"
    assert items.query == {"author_id": 3771, "page": 1}


def test_publications_rows(items):
    assert len(items.items) == 99
    assert all(isinstance(i, PublicationHit) for i in items.items)
    first = items.items[0]
    assert first.id == 91809593
    assert first.title.startswith("МОДЕЛИРОВАНИЕ СТРУКТУРНО-ПРОНИЦАЕМЫХ ЗОН")
    assert first.authors == ["Шухов В.В.", "Устинов С.А.", "Петров В.А.", "Минаев В.А.", "Свечеревский А.Д."]
    assert (first.year, first.citations, first.position) == (2026, 0, 1)
    assert first.url == "https://elibrary.ru/item.asp?id=91809593"
    odderon = items.items[4]
    assert (odderon.id, odderon.authors, odderon.year) == (92348100, ["Petrov V.A.", "Tkachenko N.P."], 2026)
    assert "Nuclear Physics A" in odderon.source
    last = items.items[-1]
    assert (last.id, last.title, last.citations, last.position) == (
        67961789, "ATLAS И TOTEM: ТРЕВОЖНОЕ РАСХОЖДЕНИЕ", 7, 100)
    positions = [i.position for i in items.items]
    assert 88 not in positions and positions[:3] == [1, 2, 3]
    assert len({i.id for i in items.items}) == 99


def test_publications_reference_only_rows(items):
    (ref,) = items.references
    assert ref.ref_id == 1062348813
    assert ref.title == "ПУТИ СОВЕРШЕНСТВОВАНИЯ НАУЧНО-МЕТОДИЧЕСКИХ ОСНОВ ПОИСКОВЫХ РАБОТ НА ТВЕРДЫЕ ПОЛЕЗНЫЕ ИСКОПАЕМЫЕ"
    assert ref.authors == ["Машковцев Г.А.", "Петров В.А."]
    assert (ref.source, ref.year, ref.citations, ref.position) == ("2023.", 2023, 5, 88)


def test_publications_page_hint_and_url():
    page = parse_author_publications(fixture_bytes("author_items_3771"), page=3,
                                     url="https://elibrary.ru/author_items.asp")
    assert page.page == 1  # the paging block marks page 1
    assert page.source_url == "https://elibrary.ru/author_items.asp"


def test_publications_later_page_synthetic():
    html = """<html><head><title>eLIBRARY.RU - Марков Антон Владимирович - Список публикаций</title></head>
    <body><form name="results">
    <input type="hidden" name="authorid" value="679517"><input type="hidden" name="authorhash" value="Марков А В">
    </form>
    <div><font color="#F26C4F"><b>МАРКОВ АНТОН ВЛАДИМИРОВИЧ</b></font> <span class="aster">*</span>
    <br><a href="org_about.asp?orgsid=519">Томский национальный исследовательский медицинский центр РАН</a>, Научно-исследовательский институт медицинской генетики (Томск)
    </div>
    <font>Всего найдено</font> <b>116</b> <font>публикаций с общим количеством цитирований: </font>
    <b><span id="totalcited">128</span></b><font>.<br>Показано на данной странице: с <b>41</b> по <b>60</b>.</font>
    <table id="restab">
    <tr valign=middle id="arw41376400"><td><b>41.</b></td><td>
    <a href="/item.asp?id=41376400"><b>TITLE ONE</b></a><br><font><i>Марков А.В., Иванов И.И.</i></font><br>
    <font>Генетика. 2019. Т. 55. № 3. С. 1-10.</font></td><td>4</td></tr>
    </table>
    <div id="pages"><table><tr>
    <td><a href="javascript:goto_page(2)">2</a></td><td><font color="#F26C4F">3</font></td>
    <td><a href="javascript:goto_page(6)">В&nbsp;конец</a></td></tr></table></div>
    </body></html>"""
    page = parse_author_publications(html)
    assert (page.page, page.pages, page.per_page, page.total, page.total_citations) == (3, 6, 20, 116, 128)
    assert (page.author_id, page.author_name, page.author_short_name) == (
        679517, "Марков Антон Владимирович", "Марков А В")
    assert (page.affiliation.org_id, page.affiliation.town) == (519, "Томск")
    assert page.affiliation.department == "Научно-исследовательский институт медицинской генетики"
    (row,) = page.items
    assert (row.id, row.position, row.citations, row.year) == (41376400, 41, 4, 2019)
    assert page.references == []


def test_publications_reference_row_year_ignores_page_range():
    # reference-only rows use the same year rule as the other rows: "С. 1901-1909" is never the year
    html = """<html><head><title>eLIBRARY.RU - Иванов Иван Иванович - Список публикаций</title></head>
    <body><form name="results">
    <input type="hidden" name="authorid" value="1"><input type="hidden" name="authorhash" value="Иванов И И">
    </form><table id="restab">
    <tr valign=middle id="brw1062348813"><td><b>7.</b></td><td>
    <b>REFERENCE TITLE</b><br><font><i>Иванов И.И.</i></font><br>
    <font>Журнал физической химии. 2025. Т. 99. № 12. С. 1901-1909.</font></td><td>3</td></tr>
    </table></body></html>"""
    (ref,) = parse_author_publications(html).references
    assert (ref.ref_id, ref.year, ref.citations, ref.position) == (1062348813, 2025, 3, 7)


def test_publications_page2_of_17783():
    # live page 2 of 26 (POST author_items.asp, 2026-10-13); "Всего найдена 2581 публикация" (feminine form):
    # the total was None live because only "Всего найдено" was recognised
    page = parse_author_publications(fixture_bytes("author_items_17783_p2"))
    assert (page.total, page.page, page.pages, page.per_page) == (2581, 2, 26, 100)
    assert page.total_citations == 19381  # from the script that fills span#totalcited
    assert page.has_next
    assert (page.author_id, page.author_name, page.author_short_name) == (17783, "Иванов Юрий Федорович", "Иванов Ю Ф")
    aff = page.affiliation
    assert (aff.name, aff.org_id, aff.town) == ("Институт сильноточной электроники СО РАН", 116, "Томск")
    assert aff.department == "лаборатория плазменной эмиссионной электроники"
    assert page.query == {"author_id": 17783, "page": 2}
    assert len(page.items) == 100 and page.references == []
    assert [i.position for i in page.items] == list(range(101, 201))
    assert len({i.id for i in page.items}) == 100
    first = page.items[0]
    assert (first.id, first.year, first.citations) == (80407957, 2024, 8)
    assert first.title.startswith("STRUCTURE, PHASE COMPOSITION, AND SURFACE PROPERTIES OF R2M9 HIGH-SPEED STEEL")
    assert first.authors[:3] == ["Ivanov Yu.F.", "Gromov V.E.", "Yuryev A.B."]
    assert first.source.startswith("Journal of Surface Investigation: X-Ray, Synchrotron and Neutron Techniques. 202")
    last = page.items[-1]
    assert (last.id, last.year, last.citations, last.position) == (50304991, 2023, 5, 200)
    assert last.title == "ДЕФОРМАЦИОННОЕ УПРОЧНЕНИЕ РЕЛЬСОВОЙ СТАЛИ"
    assert last.authors[:3] == ["Порфирьев М.А.", "Иванов Ю.Ф.", "Попова Н.А."]
    assert last.source == "Деформация и разрушение материалов. 2023. № 2. С. 20-26."
    # the first page of the same list keeps parsing as before ("Всего найдено 1094 публикации")
    assert parse_author_publications(fixture_bytes("author_items_3771")).total == 1094


@pytest.mark.parametrize(
    ("line", "total"),
    [
        ("Всего найдено</font> <b>1094</b> <font>публикации с общим количеством цитирований", 1094),
        ("Всего найдена</font> <b>2581</b> <font>публикация с общим количеством цитирований", 2581),
        ("Всего найден <b>1</b> публикация", 1),
        ("Всего найдено <b>12 345</b> публикаций", 12345),
        ("Всего найдено публикаций: <b>77</b>", 77),
    ],
)
def test_publications_total_phrasings(line, total):
    html = (f'<html><body><b>СПИСОК ПУБЛИКАЦИЙ АВТОРА</b><input name="authorid" value="5">'
            f'<input name="authorhash" value="Иванов И И"><font>{line}</font></body></html>')
    assert parse_author_publications(html).total == total


def test_publications_empty_and_errors():
    empty = parse_author_publications(
        '<html><body><b>СПИСОК ПУБЛИКАЦИЙ АВТОРА</b><input name="authorid" value="5"></body></html>')
    assert (empty.total, empty.items, empty.pages, empty.author_id) == (0, [], 0, 5)
    for fixture in ("author_profile_3771", "query_results_all_types", "authors_results_petrov", "captcha"):
        with pytest.raises(ParseError):
            parse_author_publications(fixture_bytes(fixture))


# --------------------------------------------------------------------------- data/authors_options.json


def _options() -> dict[str, list[dict]]:
    return json.loads(files("elibrary_cli.data").joinpath("authors_options.json").read_text(encoding="utf-8"))


def test_options_json_content():
    opts = _options()
    assert set(opts) == {"codetype", "countryid", "authorboxid", "rubriccode", "metrics", "sortorder", "order"}
    assert {k: len(v) for k, v in opts.items()} == {
        "codetype": 6, "countryid": 84, "authorboxid": 5, "rubriccode": 70, "metrics": 3, "sortorder": 4,
        "order": 2}
    countries = {o["value"]: o for o in opts["countryid"]}
    assert countries["RUS"] == {"value": "RUS", "label": "Россия", "count": 1084120}
    assert countries["BLR"]["label"] == "Беларусь" and countries["BLR"]["count"] == 26478
    assert countries["ZAF"]["label"] == "Южно-Африканская Республика"
    assert all(re.fullmatch(r"[A-Z]{3}", c) for c in countries)
    rubrics = {o["value"]: o for o in opts["rubriccode"]}
    assert rubrics["290000"] == {"value": "290000", "label": "Физика", "count": 64206}
    assert rubrics["000000"]["label"] == "Общественные науки в целом"
    assert all(re.fullmatch(r"\d{6}", r) for r in rubrics)
    assert opts["codetype"][0] == {"value": "SPIN", "label": "SPIN-код"}
    assert opts["codetype"][-1] == {"value": "RSCI", "label": "РИНЦ Author ID"}
    assert opts["authorboxid"][-1] == {"value": "r5", "label": "Актуальный список членов РАН", "count": 858}
    assert opts["metrics"] == [{"value": "0", "label": "по ядру РИНЦ"}, {"value": "1", "label": "по РИНЦ"},
                               {"value": "2", "label": "по elibrary.ru"}]
    assert [o["value"] for o in opts["sortorder"]] == ["0", "1", "2", "3"]
    assert opts["order"] == [{"value": "0", "label": "по возрастанию"}, {"value": "1", "label": "по убыванию"}]


def test_options_json_matches_the_form_fixture():
    tree = LexborHTMLParser(fixture_bytes("authors_form").decode("utf-8"))
    for field, options in _options().items():
        sel = tree.css(f'select[name="{field}"]')[-1]
        page = []
        for o in sel.css("option"):
            value = o.attributes.get("value") or ""
            if not value:
                continue
            label = re.sub(r"\s+", " ", o.text().replace("\xa0", " ")).strip()
            m = re.match(r"^(.*?)\s*\((\d+)\)$", label)
            page.append((value, m.group(1) if m else label.rstrip(":"), int(m.group(2)) if m else None))
        assert [(o["value"], o["label"], o.get("count")) for o in options] == page, field


def test_options_cover_the_query_model():
    opts = _options()
    assert {o["value"] for o in opts["codetype"]} == set(get_args(CodeType))
    assert {o["value"] for o in opts["metrics"]} == set(METRICS_CODES.values())
    assert {o["value"] for o in opts["sortorder"]} == set(AUTHOR_SORT_CODES.values())
    for o in opts["authorboxid"]:
        AuthorQuery(author_group=o["value"])
    for o in opts["countryid"][:5]:
        AuthorQuery(surname="Петров", country=o["value"])


# --------------------------------------------------------------------------- through the HTTP layer (fake site)


def test_search_round_trip_through_fake_site(http, site):
    site.page("authors.asp", "authors_results_petrov")
    q = AuthorQuery(surname="Петров")
    page = http.post(AUTHORS_PATH, build_author_search_form(q))
    (req,) = site.requests
    assert req.method == "POST"
    assert str(req.url) == "https://elibrary.ru/authors.asp"
    assert req.content.decode("ascii") == LIVE_PETROV_BODY
    result = parse_author_search(page.content, query=q)
    assert result.total == 4342 and result.items[0].id == 3771


def test_profile_and_items_round_trip_through_fake_site(http, site):
    site.page("author_profile.asp", "author_profile_3771").page("author_items.asp", "author_items_3771")
    prof = parse_author_profile(http.get("/author_profile.asp", author_profile_params(3771)).content)
    assert prof.hindex_rinc == 119
    page2 = http.post(AUTHOR_ITEMS_PATH, build_author_items_form(3771, 2, author_hash="Петров В А"))
    req = site.requests[-1]
    body = dict(parse_qsl(req.content.decode("ascii"), keep_blank_values=True))
    assert (body["authorid"], body["pagenum"], body["authorhash"]) == ("3771", "2", "Петров В А")
    assert parse_author_publications(page2.content).total == 1094
    assert str(site.requests[0].url) == "https://elibrary.ru/author_profile.asp?id=3771"


def test_models_produce_json_schemas_and_round_trip(petrov, profile, items):
    for model in (AuthorQuery, AuthorPublicationsQuery, AuthorProfile, AuthorPublicationsPage,
                  SearchPage[AuthorHit]):
        schema = model.model_json_schema()
        assert schema["type"] == "object"
    props = AuthorProfile.model_json_schema()["properties"]
    assert props["hindex_rinc"]["description"] == "Индекс Хирша по публикациям в РИНЦ"
    assert AuthorProfile.model_validate_json(profile.model_dump_json()) == profile
    assert AuthorPublicationsPage.model_validate_json(items.model_dump_json()) == items
    assert SearchPage[AuthorHit].model_validate_json(petrov.model_dump_json()) == petrov
