"""Organizations: orgs.asp search, org_profile.asp, org_about.asp, org_items.asp."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from elibrary_cli.errors import NotFound, ParseError
from elibrary_cli.http import encode_params
from elibrary_cli.models.base import PublicationHit, SearchPage
from elibrary_cli.models.organizations import (
    OrgHit,
    OrgProfile,
    OrgPublicationsPage,
    OrgQuery,
)
from elibrary_cli.parsers.organizations import (
    parse_org_about,
    parse_org_form_options,
    parse_org_profile,
    parse_org_search,
    parse_organization_publications,
)
from elibrary_cli.queries.organizations import (
    ORG_ITEMS_PATH,
    ORGS_PATH,
    RequestSpec,
    build_org_about_request,
    build_org_items_request,
    build_org_profile_request,
    build_org_search_form,
    build_org_search_request,
    load_org_options,
    resolve_country,
    resolve_region,
)

from .conftest import fixture_bytes

MGU_QUERY = "московский государственный университет"
# Body of the live request that produced orgs_results_mgu.html (scratchpad/re/pages/orgs_mgu.meta.json).
MGU_LIVE_BODY = (
    "pagenum=&orgname=%D0%BC%D0%BE%D1%81%D0%BA%D0%BE%D0%B2%D1%81%D0%BA%D0%B8%D0%B9+%D0%B3%D0%BE%D1%81%D1%83"
    "%D0%B4%D0%B0%D1%80%D1%81%D1%82%D0%B2%D0%B5%D0%BD%D0%BD%D1%8B%D0%B9+%D1%83%D0%BD%D0%B8%D0%B2%D0%B5%D1%80"
    "%D1%81%D0%B8%D1%82%D0%B5%D1%82&town=&regionid=0&countryid=&sortorder=3&order=1"
)


@pytest.fixture(scope="module")
def profile() -> OrgProfile:
    return parse_org_profile(fixture_bytes("org_profile_2541"))


@pytest.fixture(scope="module")
def mgu_results() -> SearchPage[OrgHit]:
    return parse_org_search(fixture_bytes("orgs_results_mgu"))


# --------------------------------------------------------------------------- OrgQuery


def test_org_query_normalisation():
    q = OrgQuery(name="  московский   университет ", town=" ", country="rus", region_id=0)
    assert q.name == "московский университет"
    assert q.town is None
    assert q.country == "RUS"
    assert q.region_id is None
    assert q.sort == "name" and q.page == 1
    assert OrgQuery(country="Россия").country == "Россия"  # names are resolved by the builder


@pytest.mark.parametrize(
    ("sort", "order", "expected"),
    [("name", None, "asc"), ("town", None, "asc"), ("publications", None, "desc"), ("citations", None, "desc"),
     ("publications", "asc", "asc"), ("name", "desc", "desc")],
)
def test_org_query_effective_order(sort, order, expected):
    assert OrgQuery(sort=sort, order=order).effective_order == expected


def test_org_query_rejects_bad_values():
    with pytest.raises(ValueError):
        OrgQuery(page=0)
    with pytest.raises(ValueError):
        OrgQuery(sort="rank")
    with pytest.raises(ValueError):
        OrgQuery(region_id=-1)


# --------------------------------------------------------------------------- query builders


def test_search_form_matches_live_request():
    q = OrgQuery(name=MGU_QUERY, sort="publications")
    fields = build_org_search_form(q)
    assert fields == [
        ("pagenum", ""),
        ("orgname", MGU_QUERY),
        ("town", ""),
        ("regionid", "0"),
        ("countryid", ""),
        ("sortorder", "3"),
        ("order", "1"),
    ]
    assert encode_params(fields) == MGU_LIVE_BODY


def test_search_form_field_order_is_browser_order():
    form_fields = parse_org_form_options(fixture_bytes("orgs_form"))["_meta"]["fields"]
    assert form_fields == ["pagenum", "orgname", "town", "regionid", "countryid", "sortorder", "order"]
    assert [name for name, _ in build_org_search_form(OrgQuery())] == form_fields


def test_search_form_defaults_and_paging():
    assert build_org_search_form(OrgQuery()) == [
        ("pagenum", ""), ("orgname", ""), ("town", ""), ("regionid", "0"), ("countryid", ""),
        ("sortorder", "0"), ("order", "0"),
    ]
    fields = dict(build_org_search_form(OrgQuery(name="институт", sort="citations", page=3)))
    assert fields["pagenum"] == "3"
    assert fields["sortorder"] == "4" and fields["order"] == "1"
    fields = dict(build_org_search_form(OrgQuery(sort="town", order="desc", town="Новосибирск")))
    assert fields["sortorder"] == "1" and fields["order"] == "1" and fields["town"] == "Новосибирск"


def test_search_form_resolves_region_and_country_names():
    fields = dict(build_org_search_form(OrgQuery(region="Новосибирская область", country="Россия")))
    assert fields["regionid"] == "51"
    assert fields["countryid"] == "RUS"
    fields = dict(build_org_search_form(OrgQuery(region_id=73, region="ignored when id is given")))
    assert fields["regionid"] == "73"


def test_resolve_country_and_region():
    assert resolve_country("RUS") == "RUS"
    assert resolve_country("blr") == "BLR"
    assert resolve_country("Беларусь") == "BLR"
    assert resolve_country("япония") == "JPN"
    assert resolve_country("XKX") == "XKX"  # plausible ISO3 not in the snapshot is passed through
    assert resolve_region("Москва") == 73
    assert resolve_region("санкт-петербург") == 72
    assert resolve_region("73") == 73
    assert resolve_region(48) == 48
    assert resolve_region("Республика Саха") == 16  # unique substring
    with pytest.raises(ValueError, match="Неизвестная страна"):
        resolve_country("Атлантида")
    with pytest.raises(ValueError, match="Неизвестный регион"):
        resolve_region("область")  # ambiguous


def test_search_request_spec():
    spec = build_org_search_request(OrgQuery(name="МГУ"))
    assert isinstance(spec, RequestSpec)
    assert spec.method == "POST" and spec.path == ORGS_PATH == "/orgs.asp"
    assert spec.params[1] == ("orgname", "МГУ")


def test_profile_about_items_requests():
    assert build_org_profile_request(2541) == RequestSpec("GET", "/org_profile.asp", [("id", "2541")])
    assert build_org_about_request(2541) == RequestSpec("GET", "/org_about.asp", [("orgsid", "2541")])
    assert build_org_items_request(2541) == RequestSpec("GET", ORG_ITEMS_PATH, [("orgsid", "2541")])
    spec = build_org_items_request(
        2541, 3, show_option=0, show_sotr=1, show_refs=False, years=[2024, 2025], sortorder="cited", order="desc"
    )
    assert spec.method == "GET"
    assert spec.params == [
        ("orgsid", "2541"), ("pagenum", "3"), ("show_option", "0"), ("show_sotr", "1"), ("show_refs", "0"),
        ("pubyear", "2024|2025"), ("sortorder", "3"), ("order", "1"),
    ]
    with pytest.raises(ValueError):
        build_org_items_request(2541, 0)


# --------------------------------------------------------------------------- orgs.asp results


def test_search_totals_and_paging(mgu_results):
    r = mgu_results
    assert r.total == 23
    assert r.total_in_database == 123983
    assert r.page == 1 and r.per_page == 20 and r.pages == 2
    assert r.has_next
    assert len(r.items) == 20
    assert [h.position for h in r.items] == list(range(1, 21))


def test_search_first_row(mgu_results):
    h = mgu_results.items[0]
    assert h.id == 2541
    assert h.name == "Московский государственный университет им. М.В. Ломоносова"
    assert h.town == "Москва"
    assert h.publications == 429371
    assert h.citations == 3938306
    assert h.url == "https://elibrary.ru/org_about.asp?orgsid=2541"
    assert h.profile_url == "https://elibrary.ru/org_profile.asp?id=2541"
    assert h.publications_url == "https://elibrary.ru/org_items.asp?orgsid=2541"
    assert h.citing_url == "https://elibrary.ru/cit_org_items.asp?id=2541"
    assert h.extra == {} and h.country is None and h.region is None


def test_search_other_rows(mgu_results):
    by_id = {h.id: h for h in mgu_results.items}
    bauman = by_id[665]
    assert bauman.name == (
        "Московский государственный технический университет им. Н.Э. Баумана (национальный исследовательский университет)"
    )
    assert (bauman.publications, bauman.citations, bauman.position) == (164752, 869838, 3)
    last = mgu_results.items[-1]
    assert (last.id, last.position, last.publications, last.citations) == (13783, 20, 16341, 88384)
    assert last.name == "Московский государственный университет геодезии и картографии"
    stankin = by_id[1488]
    assert stankin.name == 'Московский государственный технологический университет "Станкин"'
    # sorted by publications, descending, as requested
    pubs = [h.publications for h in mgu_results.items]
    assert pubs == sorted(pubs, reverse=True)
    assert all(h.town == "Москва" for h in mgu_results.items)


def test_search_query_echo_from_form(mgu_results):
    assert mgu_results.query == {"name": MGU_QUERY, "sort": "publications", "order": "desc"}


def test_search_query_from_argument_and_page_override():
    q = OrgQuery(name=MGU_QUERY, sort="publications", page=1)
    r = parse_org_search(fixture_bytes("orgs_results_mgu"), query=q, url="https://elibrary.ru/orgs.asp")
    assert r.query == {"name": MGU_QUERY, "sort": "publications", "page": 1}
    assert r.source_url == "https://elibrary.ru/orgs.asp"
    r2 = parse_org_search(fixture_bytes("orgs_results_mgu"), page=2, query={"name": "x"})
    assert r2.page == 2 and r2.query == {"name": "x"}
    assert not r2.has_next


def test_search_empty_form_page_is_empty_result():
    r = parse_org_search(fixture_bytes("orgs_form"))
    assert r.items == [] and r.total == 0 and r.pages == 0
    assert r.query == {"sort": "name", "order": "asc"}


@pytest.mark.parametrize("name", ["captcha", "author_profile_3771", "titles_results"])
def test_search_rejects_other_pages(name):
    with pytest.raises(ParseError):
        parse_org_search(fixture_bytes(name))


SYNTHETIC_RESULTS = """
<html><body>
<input type="hidden" name="pagenum" value="2">
<input type="text" name="orgname" value="институт">
Всего найдено организаций: <b>41</b> из <b>123983</b>. Показано на данной странице: с <b>21</b> по <b>40</b>.
<table id="restab">
<tr><td>№</td><td>Название организации</td><td>Город</td><td>Страна</td><td>Тип</td><td>Публ.</td><td>Цит.</td></tr>
<tr id="a77"><td>21</td><td><a href="org_about.asp?orgsid=77">Институт Икс</a></td><td>Минск</td>
<td>Беларусь</td><td>НИИ</td><td>0</td><td></td></tr>
</table>
<a href="javascript:goto_page(1)">1</a> <a href="javascript:goto_page(3)">3</a>
</body></html>
"""


def test_search_extra_columns_and_missing_links():
    r = parse_org_search(SYNTHETIC_RESULTS)
    assert (r.total, r.page, r.per_page, r.pages) == (41, 2, 20, 3)
    (h,) = r.items
    assert (h.id, h.name, h.town, h.country, h.position) == (77, "Институт Икс", "Минск", "Беларусь", 21)
    assert h.publications == 0 and h.citations is None
    assert h.extra == {"Тип": "НИИ"}
    # URLs default from the id when the row has no links
    assert h.publications_url == "https://elibrary.ru/org_items.asp?orgsid=77"
    assert h.profile_url == "https://elibrary.ru/org_profile.asp?id=77"
    assert r.query == {"name": "институт"}


def test_search_through_fake_http(http, site):
    site.page("orgs.asp", "orgs_results_mgu")
    spec = build_org_search_request(OrgQuery(name=MGU_QUERY, sort="publications"))
    page = http.post(spec.path, spec.params)
    result = parse_org_search(page.content, url=page.url)
    assert result.total == 23 and result.items[0].id == 2541
    (req,) = site.requests
    assert req.method == "POST" and str(req.url) == "https://elibrary.ru/orgs.asp"
    assert req.content.decode() == MGU_LIVE_BODY


# --------------------------------------------------------------------------- org_profile.asp


def test_profile_header(profile):
    assert profile.id == 2541
    assert profile.name == "Московский государственный университет им. М.В. Ломоносова"
    assert profile.town == "Москва"
    assert profile.country == "Россия"
    assert profile.url == "https://elibrary.ru/org_profile.asp?id=2541"
    assert profile.about_url == "https://elibrary.ru/org_about.asp?orgsid=2541"
    assert profile.publications_url == "https://elibrary.ru/org_items.asp?orgsid=2541"
    assert profile.citing_url == "https://elibrary.ru/cit_org_items.asp?id=2541"
    assert profile.updated == dt.date(2026, 6, 20)
    # card fields are not on this page
    assert profile.full_name is None and profile.address is None and profile.identifiers == {}


def test_profile_main_indicators(profile):
    p = profile
    assert (p.publications, p.publications_rinc, p.publications_core) == (502087, 429371, 216574)
    assert (p.citations, p.citations_rinc, p.citations_from_core) == (4303477, 3938306, 2726133)
    assert (p.h_index, p.h_index_rinc, p.h_index_core) == (496, 469, 400)
    assert (p.g_index, p.i_index) == (824, 68)
    assert (p.authors_count, p.authors_registered) == (41103, 12547)


def test_profile_metrics_dicts(profile):
    m = profile.metrics
    assert len(m) == 13
    assert m["Число публикаций на elibrary.ru"] == 502087
    assert m["g-индекс"] == 824
    assert m["Число авторов, зарегистрированных в Science Index"] == 12547
    m5 = profile.metrics_5y
    assert len(m5) == 32
    assert m5["Число публикаций на elibrary.ru"] == 142282
    assert m5["Число статей в зарубежных журналах"] == 14149
    assert m5["Число самоцитирований"] == 67345
    assert m5["Средневзвешенный импакт-фактор журналов, в которых были опубликованы статьи"] == pytest.approx(1.339)
    assert m5["Среднее число публикаций в расчете на одного автора"] == pytest.approx(5.70)
    assert m5["Среднее число цитирований в расчете на одного автора"] == pytest.approx(14.22)


def test_profile_five_year_typed(profile):
    p = profile
    assert p.period_5y == "2021-2025"
    assert (p.publications_5y, p.publications_rinc_5y, p.publications_core_5y) == (142282, 122452, 44995)
    assert p.citations_rinc_5y == 305602
    assert p.impact_factor_5y == pytest.approx(1.339)


def test_profile_indicator_rows(profile):
    inds = profile.indicators
    assert len(inds) == 13 + 32
    assert {i.section for i in inds} == {"ОБЩИЕ ПОКАЗАТЕЛИ", "ПОКАЗАТЕЛИ ЗА 5 ЛЕТ (2021-2025)"}
    first = inds[0]
    assert (first.section, first.name, first.value) == ("ОБЩИЕ ПОКАЗАТЕЛИ", "Число публикаций на elibrary.ru", 502087)
    assert first.url == "https://elibrary.ru/org_items.asp?orgsid=2541&show_sotr=1&show_refs=0&show_option=1"
    assert first.description is None
    core5 = next(i for i in inds if i.section.startswith("ПОКАЗАТЕЛИ ЗА 5") and i.name.endswith("ядро РИНЦ"))
    assert (core5.value, core5.percent, core5.text) == (44995, 36.7, "44995 (36,7%)")
    assert core5.url.endswith("show_option=2&pubyear=2021|2022|2023|2024|2025")
    foreign = next(i for i in inds if i.name == "Число статей в зарубежных журналах")
    assert (foreign.value, foreign.percent, foreign.url) == (14149, 11.6, None)
    h = next(i for i in inds if i.name == "Индекс Хирша по ядру РИНЦ")
    assert h.value == 400 and h.percent is None and h.url is None


def test_profile_descriptions_on_request():
    p = parse_org_profile(fixture_bytes("org_profile_2541"), with_descriptions=True)
    h = next(i for i in p.indicators if i.name == "Индекс Хирша по всем публикациям на elibrary.ru")
    assert h.description and h.description.startswith("Индекс Хирша вычисляется")
    assert "<p>" not in h.description
    assert p.by_year[0].description


def test_profile_by_year(profile):
    rows = {r.name: r for r in profile.by_year}
    assert len(profile.by_year) == 31
    first = profile.by_year[0]
    assert first.name == "Число публикаций на портале elibrary.ru"
    assert list(first.values) == list(range(2016, 2026))
    assert first.values[2016] == 24879 and first.values[2025] == 26298
    assert rows["Число грантов"].values[2023] == 729
    assert rows["Число статей в журналах, входящих в Белый список/ЕГПНИ"].values[2020] == 0
    impact = rows["Средневзвешенный импакт-фактор журналов, в которых были опубликованы статьи"]
    assert impact.values[2016] == pytest.approx(1.364) and impact.values[2025] == pytest.approx(1.087)
    assert rows["Число загрузок публикаций за год"].values[2025] == 581905


def test_profile_kbpr(profile):
    rows = {r.name: r for r in profile.kbpr_by_year}
    assert len(rows) == 12
    assert rows["Математика"].values[2016] == pytest.approx(3041.65)
    assert rows["Компьютерные и информационные науки"].values[2025] == pytest.approx(107.7)
    assert rows["Все направления"].values[2018] == 36885
    assert rows["Все направления"].values[2025] == pytest.approx(29728.2)


def test_profile_fields_tables(profile):
    assert profile.fields_year == 2025
    pubs = {r.name: r for r in profile.publications_by_field}
    assert len(profile.publications_by_field) == 11
    nat = pubs["Естественные науки"]
    assert nat.subfield is False
    assert nat.values == {
        "ЯДРО РИНЦ": 4951, "WOS": 3162, "SCOPUS": 3760, "RSCI": 2711, "ВАК": 4094, "РИНЦ": 5652, "БС ЕГПНИ": 5205,
    }
    mil = pubs["в т.ч. военные науки"]
    assert mil.subfield is True and mil.values["ВАК"] == 1 and mil.values["WOS"] == 0
    assert pubs["Всего статей организации за год"].values["РИНЦ"] == 13276
    assert pubs["Всего статей организации за 5 лет"].values["РИНЦ"] == 70994
    cits = {r.name: r for r in profile.citations_by_field}
    assert len(cits) == 11
    assert cits["Естественные науки"].values["ЯДРО РИНЦ"] == 41586
    assert cits["Всего цитирований статей организации за 5 лет"].values["БС ЕГПНИ"] == 200385
    assert cits["в т.ч. искусствоведение и культурология"].subfield is True


def test_profile_rosstat(profile):
    assert profile.rosstat_year == 2025
    rows = profile.rosstat
    assert [r.line for r in rows] == ["701", "702", "703", "704", "705", "706"]
    first = rows[0]
    assert first.name == "Опубликовано статей в рецензируемых научных журналах"
    assert (first.publications, first.staff, first.staff_under_39, first.subrow) == (13495, 5703, 2273, False)
    assert "show_option=0" in first.url and "pubyear=2025" in first.url
    white = rows[1]
    assert white.name == 'из них в научных журналах, входящих в "Белый список"'
    assert (white.publications, white.subrow) == (7354, True)
    assert (rows[3].name, rows[3].publications, rows[3].staff, rows[3].subrow) == (
        "Опубликовано научных монографий", 152, 288, False)
    assert (rows[4].publications, rows[4].staff, rows[4].staff_under_39, rows[4].url) == (3, 2, 0, None)


def test_profile_journals(profile):
    journals = profile.journals
    assert len(journals) == 88
    first = journals[0]
    assert (first.title_id, first.title, first.roles) == (161775, "Actographe", ["издательство"])
    assert first.url == "https://elibrary.ru/title_about_new.asp?id=161775"
    brics = next(j for j in journals if j.title == "BRICS Journal of Economics")
    assert brics.title_id == 75386
    assert brics.roles == ["учредитель", "редакция", "издательство", "место расположения редакции", "распространитель"]
    zoo = next(j for j in journals if j.title_id == 27879)
    assert zoo.title == "Сборник трудов Зоологического музея Московского государственного университета им. М.В. Ломоносова"


def test_profile_dissertation_councils(profile):
    councils = profile.dissertation_councils
    assert len(councils) == 103
    first = councils[0]
    assert first.code == "99.2.096.03" and first.council_type == "объединенный"
    assert len(first.specialties) == 3
    assert first.specialties[0].startswith("5.11.1. Теоретическая теология")
    phys = councils[1]
    assert (phys.code, phys.specialties, phys.council_type) == (
        "МГУ.011.2", ["1.3.3 Теоретическая физика (физ.-мат. науки)"], "самостоятельный")
    assert councils[-1].code == "МГУ.059.6"
    assert councils[-1].specialties == ["5.9.7. Классическая, византийская и новогреческая филология (филол. науки)"]


def test_profile_notes_and_no_unknown_tables(profile):
    assert len(profile.notes) == 4
    assert profile.notes[1] == "Показатели рассчитываются по базе данных РИНЦ, если в названии показателя не указано иное"
    assert profile.other_tables == []


def test_profile_json_roundtrip(profile):
    raw = profile.model_dump_json()
    data = json.loads(raw)
    assert data["by_year"][0]["values"]["2016"] == 24879
    assert data["updated"] == "2026-06-20"
    again = OrgProfile.model_validate_json(raw)
    assert again == profile
    assert OrgProfile.model_json_schema()["properties"]["publications_rinc"]["description"] == "Число публикаций в РИНЦ"


def test_profile_url_argument_kept():
    url = "https://elibrary.ru/org_profile.asp?id=2541"
    p = parse_org_profile(fixture_bytes("org_profile_2541"), url=url)
    assert p.url == url


@pytest.mark.parametrize("name", ["orgs_results_mgu", "captcha", "author_profile_3771"])
def test_profile_rejects_other_pages(name):
    with pytest.raises(ParseError):
        parse_org_profile(fixture_bytes(name))


def test_profile_empty_page_is_not_found():
    html = (
        '<html><head><title>Анализ публикационной активности</title></head><body>'
        '<b>АНАЛИЗ ПУБЛИКАЦИОННОЙ АКТИВНОСТИ ОРГАНИЗАЦИИ</b><input type="hidden" name="id" value="999999999">'
        "</body></html>"
    )
    with pytest.raises(NotFound):
        parse_org_profile(html)


def test_profile_unknown_section_kept_verbatim():
    html = """<html><head><title>Институт Игрек / анализ публикационной активности</title></head><body>
    <input type="hidden" name="id" value="5">
    <div style="width:540px; margin:0"><font color=#F26C4F><b>ИНСТИТУТ ИГРЕК</b></font><br><font color=#000000>Томск</font></div>
    <div class="midtext"><b>АНАЛИЗ ПУБЛИКАЦИОННОЙ АКТИВНОСТИ</b></div>
    <div class="midtext"><b>НОВАЯ ТАБЛИЦА</b></div>
    <table><tr><td></td><td><font color=#555555><b>Показатель</b></font></td><td><font color=#555555><b>Значение</b></font></td></tr>
    <tr><td colspan=3></td></tr>
    <tr><td><img class="help" title="x"></td><td>Что-то новое</td><td>12</td></tr></table>
    </body></html>"""
    p = parse_org_profile(html)
    assert (p.id, p.name, p.town, p.country) == (5, "Институт Игрек", "Томск", None)
    (table,) = p.other_tables
    assert table.title == "НОВАЯ ТАБЛИЦА"
    assert table.columns == ["Показатель", "Значение"]
    assert table.rows == [["Что-то новое", "12"]]


# --------------------------------------------------------------------------- org_about.asp (synthetic)

# Layout of org_about.asp as seen in a 2024 snapshot; all values below are made up.
ORG_ABOUT_HTML = """<html><head><title>Информация об организации</title></head><body>
<table><tr><td bgcolor=#dddddd>&nbsp;<font color=#000000>Вакансии:</font></td></tr>
<tr><td>info@elibrary.ru</td></tr></table>
<FORM method=post name="results" action="">
<input type="hidden" name="oper" value="">
<input type="hidden" name="orgsid" value="14074">
<table><tr><td><font color=#ffffff><b>КАРТОЧКА&nbsp;&nbsp;ОРГАНИЗАЦИИ</b></font></td></tr></table>
<table>
<tr valign=top><td width=110 bgcolor=#dddddd nowrap><font color=#000000>Полное название</font></td>
<td width=470 colspan=3 bgcolor=#eeeeee><font color=#00008f><b>Федеральное учреждение "Тестовый институт"</b></font></td></tr>
</table>
<table>
<tr><td bgcolor=#dddddd>Название на англ.</td><td bgcolor=#eeeeee colspan=3>Test Institute</td></tr>
<tr><td bgcolor=#dddddd>Сокращение</td><td bgcolor=#eeeeee>ТИ</td>
<td bgcolor=#dddddd>Сокращение англ.</td><td bgcolor=#eeeeee>TI</td></tr></table>
<table>
<tr><td bgcolor=#dddddd>Страна</td><td bgcolor=#eeeeee><a href="orgs.asp?countryid=RUS">Россия</a></td>
<td bgcolor=#dddddd>Регион</td><td bgcolor=#eeeeee><a href="orgs.asp?regionid=73">Москва</a></td></tr>
<tr><td bgcolor=#dddddd>Город</td><td bgcolor=#eeeeee><a href="orgs.asp?town=Москва">Москва</a></td>
<td bgcolor=#dddddd>Город на англ.</td><td bgcolor=#eeeeee>Moscow</td></tr></table>
<table>
<tr><td bgcolor=#dddddd>Почтовый адрес</td><td bgcolor=#eeeeee colspan=3>101000, г. Москва, ул. Примерная, д. 1</td></tr>
<tr><td bgcolor=#dddddd>Адрес на англ.</td><td bgcolor=#eeeeee colspan=3>1, Primernaya street, Moscow, 101000</td></tr>
<tr><td bgcolor=#dddddd>Юридический адрес</td><td bgcolor=#eeeeee colspan=3>101000, г. Москва, ул. Примерная, д. 2</td></tr>
</table>
<table><tr>
<td bgcolor=#dddddd>Ведомство</td><td bgcolor=#eeeeee><a href="orgs.asp?vedomid=8">Министерство науки и высшего образования Российской Федерации</a></td>
<td bgcolor=#dddddd>Тип</td><td bgcolor=#eeeeee>Научно-исследовательский институт</td></tr></table>
<table><tr><td bgcolor=#dddddd>Головная организация</td><td bgcolor=#eeeeee><a href="org_about.asp?orgsid=31">Головной центр</a></td></tr>
<tr><td bgcolor=#dddddd>ИНН</td><td bgcolor=#eeeeee>7700000000</td>
<td bgcolor=#dddddd>ROR</td><td bgcolor=#eeeeee>05dkdaa55</td></tr></table>
<table><tr><td bgcolor=#dddddd>Представитель (подписка)</td><td bgcolor=#eeeeee>Иванов Иван Иванович</td>
<td bgcolor=#dddddd>Должность</td><td bgcolor=#eeeeee>специалист</td></tr></table>
<table>
<tr><td bgcolor=#dddddd>Телефон</td><td bgcolor=#eeeeee>(7-495) 000-0000</td>
<td bgcolor=#dddddd>E-mail</td><td bgcolor=#eeeeee><a href="mailto:info@example.org">info@example.org</a></td></tr>
<tr><td bgcolor=#dddddd>Факс</td><td bgcolor=#eeeeee><font color=#00008f></font></td>
<td bgcolor=#dddddd>WWW</td><td bgcolor=#eeeeee><a href="http://example.org" target=_blank>example.org</a></td></tr>
</table>
</body></html>"""


def test_org_about_card_fields():
    c = parse_org_about(ORG_ABOUT_HTML)
    assert c.id == 14074
    assert c.name == c.full_name == 'Федеральное учреждение "Тестовый институт"'
    assert (c.name_en, c.abbreviation, c.abbreviation_en) == ("Test Institute", "ТИ", "TI")
    assert (c.country, c.country_code) == ("Россия", "RUS")
    assert (c.region, c.region_id) == ("Москва", 73)
    assert (c.town, c.town_en) == ("Москва", "Moscow")
    assert c.address == "101000, г. Москва, ул. Примерная, д. 1"
    assert c.address_en == "1, Primernaya street, Moscow, 101000"
    assert c.legal_address == "101000, г. Москва, ул. Примерная, д. 2"
    assert (c.department, c.department_id) == ("Министерство науки и высшего образования Российской Федерации", 8)
    assert c.org_type == "Научно-исследовательский институт"
    assert (c.parent_name, c.parent_id) == ("Головной центр", 31)
    assert c.identifiers == {"ИНН": "7700000000", "ROR": "05dkdaa55"}
    assert (c.phone, c.email, c.website, c.fax) == ("(7-495) 000-0000", "info@example.org", "http://example.org", None)
    assert c.extra == {"Представитель (подписка)": "Иванов Иван Иванович", "Должность": "специалист"}
    assert "Вакансии" not in c.extra  # left column before the card heading is ignored
    assert c.about_url == "https://elibrary.ru/org_about.asp?orgsid=14074"
    assert c.metrics == {} and c.publications is None


def test_org_about_keeps_only_the_organization_email():
    """The card's own "E-mail" is the organization's public contact; addresses of people (a
    representative's "E-mail ...", an address inside another value) are not kept."""
    html = ORG_ABOUT_HTML.replace(
        "<td bgcolor=#dddddd>Должность</td><td bgcolor=#eeeeee>специалист</td></tr></table>",
        "<td bgcolor=#dddddd>Должность</td><td bgcolor=#eeeeee>специалист</td></tr>"
        "<tr><td bgcolor=#dddddd>E-mail представителя</td><td bgcolor=#eeeeee>"
        "<a href=\"mailto:ivanov@example.org\">ivanov@example.org</a></td>"
        "<td bgcolor=#dddddd>Контакты</td><td bgcolor=#eeeeee>Петров П.П. (petrov@example.org)</td></tr></table>",
    )
    c = parse_org_about(html)
    assert c.email == "info@example.org"
    assert "E-mail представителя" not in c.extra
    assert c.extra["Контакты"] == "Петров П.П."
    dumped = c.model_dump_json()
    assert "ivanov@example.org" not in dumped and "petrov@example.org" not in dumped


def test_org_about_errors():
    with pytest.raises(ParseError):
        parse_org_about(fixture_bytes("org_profile_2541"))
    empty = '<html><body><input name="orgsid" value="5"><b>КАРТОЧКА ОРГАНИЗАЦИИ</b></body></html>'
    with pytest.raises(NotFound):
        parse_org_about(empty)


def test_merge_profile_with_card(profile):
    card = parse_org_about(ORG_ABOUT_HTML.replace('value="14074"', 'value="2541"'))
    merged = profile.merge(card)
    assert merged.id == 2541
    assert merged.name == profile.name  # the analysis page name wins
    assert merged.full_name == 'Федеральное учреждение "Тестовый институт"'
    assert merged.country == "Россия" and merged.country_code == "RUS"
    assert merged.publications == 502087 and merged.metrics == profile.metrics
    assert merged.identifiers["ROR"] == "05dkdaa55"
    assert merged.extra["Должность"] == "специалист"
    assert len(merged.dissertation_councils) == 103
    assert profile.full_name is None  # merge returns a copy


# --------------------------------------------------------------------------- org_items.asp


def test_publications_parser_on_author_items_analogue():
    """org_items.asp has the layout of author_items.asp (the real page org_items_18078 is covered by
    tests/test_live_fixtures.py)."""
    r = parse_organization_publications(fixture_bytes("author_items_3771"))
    assert isinstance(r, OrgPublicationsPage) and isinstance(r, SearchPage)
    assert r.total == 1094
    assert (r.page, r.per_page, r.pages) == (1, 100, 11)
    assert r.total_citations is None  # <span id="totalcited"></span> is filled by JS
    # 100 rows on the page; row 88 is a reference-only row (tr#brw..., no item.asp link, show_refs=1)
    # that parsers.rows does not turn into a PublicationHit
    assert len(r.items) == 99
    assert 88 not in [i.position for i in r.items]
    assert all(isinstance(i, PublicationHit) for i in r.items)
    assert r.items[0].position == 1 and r.items[-1].position == 100
    assert r.items[0].id == 91809593
    assert r.org_id is None
    assert r.query["show_refs"] == "1" and r.query["hide_doubles"] == "1"


ORG_ITEMS_HTML = """<!DOCTYPE html><html><head>
<title>Институт Зет РАН - Список публикаций организации</title></head><body>
<FORM method=post name="results" onSubmit="return false">
<input type="hidden" name="pagenum" value="2">
<input type="hidden" name="orgsid" value="102">
<input type="hidden" name="show_refs" value="0">
<input type="hidden" name="hide_doubles" value="1">
<div style="width:540px; margin:0 20px 20px 20px; text-align: center;">
<font color=#F26C4F><b>ИНСТИТУТ ЗЕТ РАН</b></font>  <br><font color=#000000>Москва</font></div>
<select id="show_option" name="show_option"><option value="1" >все<option value="3" selected>RSCI</select>
<select id="show_sotr" name="show_sotr"><option value="0" >явно<option value="1" selected>сотрудники</select>
<select id="sortorder" name="sortorder"><option value="0" selected>по дате выпуска</select>
<select id="order" name="order"><option value="0" >asc<option value="1" selected>desc</select>
<table><tr><td class="redref"><font color=#00008f>Всего найдено</font> <b><font color=#ff0000>143</font></b>
<font color=#00008f>публикации с общим количеством цитирований: </font><b><font color=#ff0000>635</font></b>
<font color=#00008f>.<br>Показано на данной странице: с <b>101</b> по <b>143</b>.</font></td></tr></table>
<table width=580 cellspacing=0 cellpadding=3 id="restab">
<tr align=center><td>№</td><td>Публикация</td><td>Цит.</td></tr>
<tr><td colspan=3><div class="table-line"></div></td></tr>
<tr valign=middle id="arw41420589">
<td align=center valign=top><font color=#00008f><b>101.</b></font><br><input type=checkbox name="tid41420589"></td>
<td align=left valign=top><a href="/item.asp?id=41420589"><b><span>ПЕРВАЯ СТАТЬЯ</span></b></a><br>
<font color=#00008f><i>Иванов А.В., Петров В.Н.</i></font><br>
<font color=#00008f><a href="/contents.asp?id=1">Клиническая геронтология</a>. 2019. Т. 25. <a href="/contents.asp?id=1&selid=2">№&nbsp;9-10</a>.  С. 65.
</td><td align=center valign=middle>0</td></tr>
<tr valign=middle id="arw37024879">
<td align=center valign=top><font color=#00008f><b>102.</b></font><br><input type=checkbox name="tid37024879"></td>
<td align=left valign=top><a href="/item.asp?id=37024879"><b><span>ВТОРАЯ СТАТЬЯ</span></b></a><br>
<font color=#00008f><i>Сидоров Е.С.</i></font><br>
<font color=#00008f><a href="/contents.asp?id=3">Акустический журнал</a>. 2019. Т. 65. №&nbsp;2.  С. 190-196.
<table width=100%><td bgcolor=#f5f5f5>Версии:</td><td><span class="menug"><a href="item.asp?id=41621286">
<span>SECOND ARTICLE</span><br><i>Sidorov E.S.</i><br>Acoustical Physics. 2019. Т. 65. №&nbsp;2. С. 165-170.</a></span>
</td></tr></table>
</td><td align=center valign=middle>8</td></tr>
</table>
<a href="javascript:goto_page(1)">В&nbsp;начало</a><a href="javascript:goto_page(1)">1</a>
</FORM></body></html>"""


def test_publications_parser_synthetic_org_items():
    r = parse_organization_publications(ORG_ITEMS_HTML, url="https://elibrary.ru/org_items.asp?orgsid=102&pagenum=2")
    assert (r.total, r.total_citations) == (143, 635)
    assert (r.page, r.per_page, r.pages) == (2, 100, 2)
    assert not r.has_next
    assert (r.org_id, r.org_name, r.org_town) == (102, "Институт Зет РАН", "Москва")
    assert r.query == {
        "org_id": 102, "show_option": "3", "show_sotr": "1", "sortorder": "0", "order": "1",
        "show_refs": "0", "hide_doubles": "1",
    }
    assert r.source_url.endswith("pagenum=2")
    first, second = r.items
    assert (first.id, first.title, first.authors, first.year, first.citations, first.position) == (
        41420589, "ПЕРВАЯ СТАТЬЯ", ["Иванов А.В.", "Петров В.Н."], 2019, 0, 101)
    assert first.source.startswith("Клиническая геронтология")
    assert (second.id, second.title, second.citations, second.position) == (37024879, "ВТОРАЯ СТАТЬЯ", 8, 102)


def test_publications_parser_empty_and_errors():
    empty = parse_organization_publications(
        '<html><body><input type="hidden" name="orgsid" value="7">СПИСОК ПУБЛИКАЦИЙ ОРГАНИЗАЦИИ</body></html>'
    )
    assert empty.items == [] and empty.total == 0 and empty.org_id == 7
    with pytest.raises(ParseError):
        parse_organization_publications(fixture_bytes("captcha"))


# --------------------------------------------------------------------------- reference data


def test_form_options_from_trimmed_fixture():
    opts = parse_org_form_options(fixture_bytes("orgs_form"))
    assert opts["countryid"][0] == {"value": "AUS", "label": "Австралия", "count": 1441}
    assert {"value": "RUS", "label": "Россия", "count": 15442} in opts["countryid"]
    assert opts["regionid"][0] == {"value": "1943", "label": "Aakkâr", "count": 1}
    assert opts["town"][0] == {"value": "‘Aiea", "label": "‘Aiea", "count": 4}
    assert {"value": "Aachen", "label": "Aachen", "count": 50} in opts["town"]
    assert opts["sortorder"] == [
        {"value": "0", "label": "по названию организации", "key": "name", "default_order": "0"},
        {"value": "1", "label": "по названию города", "key": "town", "default_order": "0"},
        {"value": "3", "label": "по числу статей", "key": "publications", "default_order": "1"},
        {"value": "4", "label": "по числу цитирований", "key": "citations", "default_order": "1"},
    ]
    assert opts["order"] == [
        {"value": "0", "label": "по возрастанию", "key": "asc"},
        {"value": "1", "label": "по убыванию", "key": "desc"},
    ]
    small = parse_org_form_options(fixture_bytes("orgs_form"), max_towns=10)
    assert "town" not in small and small["_meta"]["town_count"] == len(opts["town"])


def test_shipped_orgs_options_json():
    data = load_org_options()
    assert data["_meta"]["fields"] == ["pagenum", "orgname", "town", "regionid", "countryid", "sortorder", "order"]
    assert data["_meta"]["town_count"] == 16098
    assert "town" not in data
    assert len(data["countryid"]) == 230
    assert {"value": "RUS", "label": "Россия", "count": 15442} in data["countryid"]
    assert len(data["regionid"]) == 2207
    assert {"value": "73", "label": "Москва", "count": 5643} in data["regionid"]
    assert {"value": "51", "label": "Новосибирская область", "count": 333} in data["regionid"]
    assert [o["key"] for o in data["sortorder"]] == ["name", "town", "publications", "citations"]
    assert all(set(o) >= {"value", "label"} for o in data["countryid"] + data["regionid"])
