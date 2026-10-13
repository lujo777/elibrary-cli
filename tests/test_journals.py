"""Journal area: query model, form builder, reference data and the four journal page parsers.

All pages are fixtures saved from elibrary.ru on 2026-10-13; nothing here touches the network."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from elibrary_cli.errors import NotFound, ParseError
from elibrary_cli.http import encode_params
from elibrary_cli.models.journals import (
    IssueContents,
    IssueRef,
    JournalHit,
    JournalIndicators,
    JournalProfile,
    JournalQuery,
    normalize_code,
)
from elibrary_cli.parsers.common import parse_html, remove_noise
from elibrary_cli.parsers.journals import (
    _indicator_rows,
    journal_id_from_url,
    parse_issue_contents,
    parse_journal_indicators,
    parse_journal_profile,
    parse_journal_search,
    parse_journal_search_options,
    split_issue_label,
    strip_emails,
)
from elibrary_cli.queries.journals import (
    build_journal_search_form,
    find_option,
    issue_url,
    journal_analysis_url,
    journal_options,
    journal_profile_url,
    journal_search_url,
    option_label,
)

from .conftest import fixture_bytes

DATA_FILE = Path(__file__).parents[1] / "src" / "elibrary_cli" / "data" / "journals_options.json"

#: Body of the live request that produced tests/fixtures/html/titles_results.html (browser order).
LIVE_BODY = (
    "titles_all=&pagenum=&titlebox_name=&titlename=%D0%B2%D1%8B%D1%87%D0%B8%D1%81%D0%BB%D0%B8%D1%82%D0%B5"
    "%D0%BB%D1%8C%D0%BD%D1%8B%D0%B5+%D1%82%D0%B5%D1%85%D0%BD%D0%BE%D0%BB%D0%BE%D0%B3%D0%B8%D0%B8&countryid="
    "&rubriccode=&language=&translated=&wos=&scopus=&white=&vak=&risc=&ftopen=&sortorder=1&order=1"
)


# ==================================================================================================
# JournalQuery
# ==================================================================================================


def test_query_defaults():
    q = JournalQuery()
    assert q.sort == "citations_core"
    assert q.order is None
    assert q.effective_order == "desc"
    assert q.page == 1
    assert not any([q.rsci, q.doaj, q.core_rinc, q.fulltext, q.active, q.scientific])


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("vak", "k1", "k1"),
        ("vak", "2", "k1"),
        ("vak", 4, "k3"),
        ("vak", "К2", "k2"),  # Cyrillic К
        ("vak", "9", "not"),
        ("vak", "no", "not"),
        ("vak", "1", "included"),
        ("scopus", "Q2", "q2"),
        ("scopus", "5", "q2"),
        ("scopus", "9", "not"),
        ("wos", "8", "q1-q4"),
        ("wos", "translated", "translated"),
        ("white", "5", "l4"),
        ("white", "05", "l4"),
        ("white", "13", "any"),
        ("white", "egpni_l2", "egpni_l2"),
        ("risc", "0", "indexed"),
        ("risc", 0, "indexed"),
        ("risc", "6", "q4"),
        ("ftopen", "3", "subscription"),
        ("ftopen", "paid", "subscription"),
        ("translated", "0", "no"),
        ("translated", "1", "full"),
        ("sort", "7", "si_rating"),
        ("sort", "0", "name"),
        ("order", "0", "asc"),
        ("order", "desc", "desc"),
    ],
)
def test_query_accepts_readable_values_and_site_codes(field, value, expected):
    assert getattr(JournalQuery(**{field: value}), field) == expected
    assert normalize_code(field, value) == expected


@pytest.mark.parametrize(("field", "value"), [("vak", "k4"), ("scopus", "8"), ("risc", "1"), ("wos", "q5"),
                                              ("white", "19"), ("sort", "cited"), ("ftopen", True)])
def test_query_rejects_unknown_codes(field, value):
    with pytest.raises(ValidationError) as exc:
        JournalQuery(**{field: value})
    assert field in str(exc.value)


def test_query_error_message_is_russian_and_lists_codes():
    with pytest.raises(ValidationError) as exc:
        JournalQuery(vak="k9")
    msg = str(exc.value)
    assert "Недопустимое значение vak" in msg
    assert "k1 (2)" in msg and "not (9)" in msg


@pytest.mark.parametrize(
    ("value", "expected"),
    [("RUS", "RUS"), ("rus", "RUS"), ("Россия", "RUS"), ("nrus", "nRUS"), ("foreign", "nRUS"),
     ("зарубежные страны", "nRUS"), ("deu", "DEU"), ("Германия", "DEU"), ("германия", "DEU"), (None, None)],
)
def test_query_country(value, expected):
    assert JournalQuery(country=value).country == expected


def test_query_country_unknown_name():
    with pytest.raises(ValidationError, match="Неизвестная страна"):
        JournalQuery(country="Атлантида")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("RU", "RU"), ("en", "EN"), ("nru", "nRU"), ("английский", "EN"), ("русский", "RU"), ("ml", "ML"),
     ("на нескольких языках", "ML"), ("english", "EN")],
)
def test_query_language(value, expected):
    assert JournalQuery(language=value).language == expected


def test_query_language_unknown():
    with pytest.raises(ValidationError, match="Неизвестный язык"):
        JournalQuery(language="клингонский")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("500000", "500000"), ("27", "270000"), ("02", "020000"), ("2", "2"), ("1", "1"), (270000, "270000"),
     ("27.00.00", "270000"), ("Математика", "270000"), ("информатика", "200000"),
     ("Мультидисциплинарные журналы по всем направлениям науки", "1")],
)
def test_query_rubric(value, expected):
    assert JournalQuery(rubric_code=value).rubric_code == expected


def test_query_rubric_unknown():
    with pytest.raises(ValidationError, match="Неизвестная рубрика"):
        JournalQuery(rubric_code="Алхимия")


def test_query_effective_order_follows_the_form_js():
    assert JournalQuery(sort="name").effective_order == "asc"
    for key in ("articles", "citations_rinc", "citations_core", "si_rating"):
        assert JournalQuery(sort=key).effective_order == "desc"
    assert JournalQuery(sort="name", order="desc").effective_order == "desc"


def test_query_strips_whitespace_and_validates_page():
    assert JournalQuery(name="  вычислительные \xa0 технологии ").name == "вычислительные технологии"
    assert JournalQuery(name="   ").name is None
    with pytest.raises(ValidationError):
        JournalQuery(page=0)


# ==================================================================================================
# Form builder and URLs
# ==================================================================================================


def test_form_matches_live_request_byte_for_byte():
    q = JournalQuery(name="вычислительные технологии")
    assert encode_params(build_journal_search_form(q)) == LIVE_BODY


def test_form_default_order_of_fields():
    names = [n for n, _ in build_journal_search_form(JournalQuery())]
    assert names == ["titles_all", "pagenum", "titlebox_name", "titlename", "countryid", "rubriccode", "language",
                     "translated", "wos", "scopus", "white", "vak", "risc", "ftopen", "sortorder", "order"]


def _results_form_names_in_dom_order() -> list[str]:
    """Names of the controls of form 'results' in titles_form.html, in source order (the HTML5 parser
    moves the <FORM> out of its table, so read the raw markup)."""
    html = fixture_bytes("titles_form").decode("utf-8")
    start = html.index('<FORM method=post name="results"')
    end = html.index("</FORM>", start)
    return re.findall(r'<(?:input|select)[^>]*\bname="([^"]+)"', html[start:end])


def test_form_checkbox_positions_match_the_dom():
    q = JournalQuery(rsci=True, doaj=True, core_rinc=True, fulltext=True, active=True, scientific=True)
    fields = build_journal_search_form(q)
    assert [n for n, _ in fields] == _results_form_names_in_dom_order()
    assert dict(fields)["rsci"] == "on"
    assert [n for n, v in fields if v == "on"] == ["rsci", "doaj", "corerisc", "fulltext", "isactive", "isscientific"]


def test_form_only_checked_boxes_are_sent():
    fields = build_journal_search_form(JournalQuery(core_rinc=True, scientific=True))
    names = [n for n, _ in fields]
    assert "corerisc" in names and "isscientific" in names
    assert "rsci" not in names and "doaj" not in names and "fulltext" not in names and "isactive" not in names
    assert names.index("ftopen") < names.index("corerisc") < names.index("isscientific") < names.index("sortorder")


def test_form_maps_readable_values_to_site_codes():
    q = JournalQuery(
        name="1560-7534", country="Россия", rubric_code="Математика", language="английский", translated="partial",
        wos="q1-q4", scopus="q4", white="l4", vak="k1", risc="indexed", ftopen="open", sort="si_rating", page=3,
    )
    f = dict(build_journal_search_form(q))
    assert f == {
        "titles_all": "", "pagenum": "3", "titlebox_name": "", "titlename": "1560-7534", "countryid": "RUS",
        "rubriccode": "270000", "language": "EN", "translated": "2", "wos": "8", "scopus": "7", "white": "05",
        "vak": "2", "risc": "0", "ftopen": "1", "sortorder": "7", "order": "1",
    }


def test_form_raw_codes_round_trip():
    q = JournalQuery(vak="3", white="09", scopus="4", risc="5", translated="0", sort="3", order="0")
    f = dict(build_journal_search_form(q))
    assert (f["vak"], f["white"], f["scopus"], f["risc"], f["translated"]) == ("3", "09", "4", "5", "0")
    assert (f["sortorder"], f["order"]) == ("3", "0")


def test_form_sort_by_name_is_ascending_and_page_one_sends_empty_pagenum():
    f = dict(build_journal_search_form(JournalQuery(sort="name", page=1)))
    assert (f["sortorder"], f["order"], f["pagenum"]) == ("0", "0", "")
    f = dict(build_journal_search_form(JournalQuery(sort="articles", page=2)))
    assert (f["sortorder"], f["order"], f["pagenum"]) == ("3", "1", "2")


def test_urls():
    assert issue_url(journal_id=8610) == "https://elibrary.ru/contents.asp?titleid=8610"
    assert issue_url(issue_id=91982811) == "https://elibrary.ru/contents.asp?id=91982811"
    assert issue_url(8610, 91982811) == "https://elibrary.ru/contents.asp?id=91982811"
    assert issue_url(issue_id=5, base="http://localhost:8000/") == "http://localhost:8000/contents.asp?id=5"
    with pytest.raises(ValueError, match="journal_id"):
        issue_url()
    assert journal_search_url() == "https://elibrary.ru/titles.asp"
    assert journal_profile_url(8610) == "https://elibrary.ru/title_about_new.asp?id=8610"
    assert journal_profile_url(8610, english=True) == "https://elibrary.ru/title_about_new.asp?id=8610&langid=2"
    assert journal_analysis_url(8610) == "https://elibrary.ru/title_profile.asp?id=8610"


def test_journal_id_from_url():
    assert journal_id_from_url("https://elibrary.ru/contents.asp?titleid=8610") == 8610
    assert journal_id_from_url("https://elibrary.ru/title_about_new.asp?id=8746&langid=2") == 8746
    assert journal_id_from_url("title_profile.asp?id=68559") == 68559
    assert journal_id_from_url("https://elibrary.ru/contents.asp?id=91982811") is None
    assert journal_id_from_url(None) is None


# ==================================================================================================
# Reference data (data/journals_options.json)
# ==================================================================================================


def test_options_file_is_the_parse_of_the_form_fixture():
    on_disk = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    assert on_disk == parse_journal_search_options(fixture_bytes("titles_form"))
    assert journal_options() == on_disk


def test_options_content():
    opts = journal_options()
    assert set(opts) == {"countryid", "rubriccode", "language", "translated", "wos", "scopus", "white", "vak",
                         "risc", "ftopen", "sortorder", "order", "checkboxes"}
    assert opts["countryid"][0] == {"value": "RUS", "label": "Россия", "count": 18260}
    assert opts["countryid"][1] == {"value": "nRUS", "label": "зарубежные страны", "count": 69616}
    assert len(opts["countryid"]) == 158
    assert {"value": "500000", "label": "Автоматика. Вычислительная техника", "count": 1692} in opts["rubriccode"]
    assert len(opts["rubriccode"]) == 72
    assert opts["language"][:3] == [
        {"value": "RU", "label": "русский", "count": 20856},
        {"value": "nRU", "label": "иностранный", "count": 34157},
        {"value": "ML", "label": "на нескольких языках", "count": 10891},
    ]
    assert {"value": "PT", "label": "португальский", "count": 277} in opts["language"]  # trailing space trimmed
    assert [o["value"] for o in opts["white"]] == [f"{i:02d}" for i in range(1, 19)]
    assert [o["value"] for o in opts["vak"]] == ["1", "2", "3", "4", "9"]
    assert [o["value"] for o in opts["wos"]] == ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
    assert [o["value"] for o in opts["scopus"]] == ["1", "2", "3", "4", "5", "6", "7", "9"]
    assert [o["value"] for o in opts["risc"]] == ["0", "2", "3", "4", "5", "6"]
    assert opts["wos"][7] == {"value": "8", "label": "Web of Science (квартили 1-4)", "count": 19893}
    assert opts["sortorder"] == [
        {"value": "0", "label": "по названию журнала"},
        {"value": "3", "label": "по числу статей"},
        {"value": "4", "label": "по числу цитирований в РИНЦ"},
        {"value": "1", "label": "по числу цитирований из ядра РИНЦ"},
        {"value": "7", "label": "по рейтингу Science Index"},
    ]
    assert opts["order"] == [{"value": "0", "label": "по возрастанию"}, {"value": "1", "label": "по убыванию"}]
    assert opts["checkboxes"] == [
        {"value": "rsci", "label": "входит в базу данных RSCI", "count": 1084},
        {"value": "doaj", "label": "индексируется в DOAJ", "count": 9923},
        {"value": "corerisc", "label": "входит в ядро РИНЦ", "count": 33441},
        {"value": "fulltext", "label": "с полными текстами", "count": 9650},
        {"value": "isactive", "label": "выходит в настоящее время", "count": 145310},
        {"value": "isscientific", "label": "только научные журналы", "count": 162557},
    ]


def test_code_tables_cover_every_form_option():
    """Every option value of the coded selects has a readable alias, and vice versa."""
    from elibrary_cli.models.journals import CODE_TABLES

    opts = journal_options()
    for field, site_field in [("translated", "translated"), ("wos", "wos"), ("scopus", "scopus"),
                              ("white", "white"), ("vak", "vak"), ("risc", "risc"), ("ftopen", "ftopen"),
                              ("sort", "sortorder"), ("order", "order")]:
        assert sorted(CODE_TABLES[field].values()) == sorted(o["value"] for o in opts[site_field]), field


def test_find_option_and_label():
    assert find_option("countryid", "Казахстан") == "KAZ"
    assert find_option("rubriccode", "Физика") == "290000"
    assert find_option("language", "немецкий") == "DE"
    assert find_option("countryid", "") is None
    assert find_option("countryid", "Нарния") is None
    assert option_label("vak", "2") == "Перечень ВАК (категория 1)"
    assert option_label("white", "09") == "ЕГПНИ (уровень 2)"
    assert option_label("vak", "7") is None


def test_parse_options_rejects_other_pages():
    with pytest.raises(ParseError):
        parse_journal_search_options(fixture_bytes("captcha"))


# ==================================================================================================
# titles.asp results
# ==================================================================================================


def test_search_results_fixture():
    page = parse_journal_search(fixture_bytes("titles_results"))
    assert page.total == 4
    assert page.total_in_database == 164076
    assert page.page == 1
    assert page.pages == 1
    assert page.has_next is False
    assert [h.id for h in page.items] == [8610, 8746, 68559, 37522]
    first = page.items[0]
    assert isinstance(first, JournalHit)
    assert first.name == "Вычислительные технологии"
    assert first.publisher == "Федеральный исследовательский центр информационных и вычислительных технологий"
    assert (first.issues, first.articles, first.citations, first.position) == (222, 2275, 6570, 1)
    assert first.citations_kind == "Число цитирований статей из ядра РИНЦ"
    assert first.url == "https://elibrary.ru/title_about_new.asp?id=8610"
    assert first.contents_url == "https://elibrary.ru/contents.asp?titleid=8610"
    assert first.items_url == "https://elibrary.ru/title_items.asp?id=8610"
    assert first.issn is None and first.extra == {}
    second = page.items[1]
    assert second.name == "Информационные технологии и вычислительные системы"
    assert second.publisher == 'Федеральный исследовательский центр "Информатика и управление" РАН'
    assert (second.issues, second.articles, second.citations) == (98, 877, 1632)
    third = page.items[2]
    assert (third.name, third.publisher) == ("Высокопроизводительные вычислительные системы и технологии",
                                             "Алтайский государственный университет")
    assert (third.issues, third.articles, third.citations, third.position) == (17, 419, 64, 3)
    last = page.items[3]
    assert last.name == "Информационно-вычислительные технологии в науке"
    assert last.publisher is None  # empty <font></font>
    assert (last.issues, last.articles, last.citations, last.position) == (0, 0, 0, 4)
    assert last.contents_url == "https://elibrary.ru/contents.asp?titleid=37522"  # default (the row has no link)


def test_search_echoed_query_and_explicit_query():
    page = parse_journal_search(fixture_bytes("titles_results"))
    assert page.query == {"name": "вычислительные технологии"}
    q = JournalQuery(name="вычислительные технологии", vak="k1")
    page = parse_journal_search(fixture_bytes("titles_results"), query=q, page=1)
    assert page.query == {"name": "вычислительные технологии", "vak": "k1"}
    page = parse_journal_search(fixture_bytes("titles_results"), query={"raw": "x"})
    assert page.query == {"raw": "x"}


def test_search_empty_form_page():
    page = parse_journal_search(fixture_bytes("titles_form"))
    assert page.items == []
    assert page.total is None
    assert page.pages is None
    assert page.query == {}


def _paged_results(total: int, shown_from: int, shown_to: int, last: int, current: int) -> bytes:
    html = fixture_bytes("titles_results").decode("utf-8")
    html = html.replace(
        "Всего найдено журналов: <b><font color=#ff0000>4</font></b> из <b>164076</b>. &nbsp;&nbsp;&nbsp;"
        "Показано на данной странице: с <b>1</b> по <b>4</b>.",
        f"Всего найдено журналов: <b><font color=#ff0000>{total}</font></b> из <b>164076</b>. &nbsp;&nbsp;&nbsp;"
        f"Показано на данной странице: с <b>{shown_from}</b> по <b>{shown_to}</b>.",
    )
    html = html.replace('<input type="hidden" name="pagenum" value="1">',
                        f'<input type="hidden" name="pagenum" value="{current}">')
    links = "".join(f'<td><a href="javascript:goto_page({n})">{n}</a></td>' for n in range(1, last + 1) if n != current)
    pager = (f'<div id="pages"><table><tr>{links}<td><a href="javascript:goto_page({last})">В&nbsp;конец</a></td>'
             "</tr></table></div>")
    assert f'value="{current}"' in html and f"по <b>{shown_to}</b>" in html
    end = html.index("</table>", html.index('id="restab"')) + len("</table>")
    html = html[:end] + pager + html[end:]
    return html.encode("utf-8")


def test_search_pagination_first_page():
    page = parse_journal_search(_paged_results(250, 1, 100, 3, 1))
    assert (page.total, page.page, page.per_page, page.pages) == (250, 1, 100, 3)
    assert page.has_next is True
    assert len(page.items) == 4


def test_search_pagination_last_page():
    page = parse_journal_search(_paged_results(250, 201, 250, 3, 3))
    assert (page.total, page.page, page.per_page, page.pages) == (250, 3, 100, 3)
    assert page.has_next is False
    assert page.query == {"name": "вычислительные технологии", "page": 3}


def test_search_pagination_prefers_the_pager_links():
    page = parse_journal_search(_paged_results(250, 1, 100, 5, 1))
    assert (page.per_page, page.pages) == (100, 5)


def test_search_explicit_page_wins():
    page = parse_journal_search(_paged_results(250, 101, 200, 3, 2), page=2)
    assert (page.page, page.per_page, page.pages) == (2, 100, 3)


def test_search_rejects_other_pages():
    with pytest.raises(ParseError):
        parse_journal_search(fixture_bytes("captcha"))
    with pytest.raises(ParseError):
        parse_journal_search(fixture_bytes("item_article_46371463"))


def test_search_over_fake_site(http, site):
    """The builder + ElibraryHTTP send exactly the browser body; the reply parses."""
    site.page("titles.asp", "titles_results")
    q = JournalQuery(name="вычислительные технологии")
    page = http.post("/titles.asp", build_journal_search_form(q))
    assert site.requests[-1].method == "POST"
    assert site.requests[-1].content.decode("ascii") == LIVE_BODY
    result = parse_journal_search(page.content, query=q)
    assert result.total == 4 and result.items[0].id == 8610


# ==================================================================================================
# title_about_new.asp
# ==================================================================================================


@pytest.fixture(scope="module")
def profile() -> JournalProfile:
    return parse_journal_profile(fixture_bytes("title_about_8610"),
                                 url="https://elibrary.ru/title_about_new.asp?id=8610")


def test_profile_identity(profile):
    assert profile.id == 8610
    assert profile.name == "Вычислительные технологии"
    assert profile.name_en is None
    assert profile.description_languages == ["русский", "английский"]
    assert profile.english_url == "https://elibrary.ru/title_about_new.asp?id=8610&langid=2"
    assert profile.url == "https://elibrary.ru/title_about_new.asp?id=8610"
    assert profile.contents_url == "https://elibrary.ru/contents.asp?titleid=8610"
    assert profile.items_url == "https://elibrary.ru/title_items.asp?id=8610"
    assert profile.analysis_url == "https://elibrary.ru/title_profile.asp?id=8610"


def test_profile_main_characteristics(profile):
    assert profile.serial_type == "периодическое издание"
    assert profile.serial_elements == "выпуск журнала"
    assert profile.purpose == "научное"
    assert profile.distribution == "в печатном и электронном виде"
    assert profile.fulltext_access == "текущие выпуски в платном доступе, архивные выпуски - в открытом"
    assert profile.open_access == "partial"
    assert profile.funding == "учредитель"
    assert profile.multidisciplinary is False
    assert profile.languages == ["русский", "английский"]


def test_profile_founders_and_office(profile):
    assert len(profile.founders) == 1
    founder = profile.founders[0]
    assert founder.id == 34
    assert founder.name == "Федеральный исследовательский центр информационных и вычислительных технологий"
    assert founder.city == "Новосибирск"
    assert founder.url == "https://elibrary.ru/org_profile.asp?id=34"
    assert profile.editorial_office == profile.founders
    assert profile.publishers == [] and profile.publisher is None


def test_profile_publication_data(profile):
    assert profile.issn == "1560-7534"
    assert profile.eissn == "2313-691X"
    assert profile.issues_per_year == 6
    assert profile.foundation_year == 1995
    assert profile.articles_per_issue == 9
    assert profile.pages_per_issue == 135
    assert profile.publication_period == "1996-..."
    assert profile.active is True
    assert profile.archive == "1996-2026"
    assert profile.articles_total == 2275
    assert profile.issues_total == 222
    assert profile.media_registration == "№ 013787 от 05.06.1995"


def test_profile_indexing(profile):
    ix = profile.indexing
    assert ix.elibrary is True and ix.elibrary_contract == "290-10/2022"
    assert ix.risc is True
    assert ix.rsci is True
    assert ix.core_rinc is True
    assert ix.doaj is False
    assert ix.wos is False and ix.wos_quartile is None
    assert ix.scopus is True and ix.scopus_quartile == 4
    assert ix.white_list is True and ix.white_list_level == 4
    assert ix.egpni is True and ix.egpni_level == 2
    assert ix.vak is True and ix.vak_category is None
    assert ix.science_index is True and ix.science_index_percentile == 14
    assert ix.crossref is True and ix.doi_prefix == "10.25743/ICT."
    assert ix.databases == [
        "Scopus",
        "AMS MathSciNet",
        "Ulrich's Periodicals Directory",
        "Web of Science: RSCI (Russian Science Citation Index at Web of Science)",
        "РИНЦ",
        "Перечень ВАК РФ (список изданий, входящих в международные базы)",
    ]
    assert ix.extra == {}


def test_profile_contacts(profile):
    assert profile.country == "Россия"
    assert profile.city == "Новосибирск"
    assert profile.address == "630090, г. Новосибирск, пр-т Академика Лаврентьева, 6"
    assert profile.email == "jct@ict.nsc.ru"
    assert profile.phone == "+7(383) 334-91-23"
    assert profile.website == "http://www.ict.nsc.ru/jct/main"


def test_profile_editors(profile):
    chief = profile.editor_in_chief
    assert chief is not None
    assert chief.name == "Федорук Михаил Петрович"
    assert chief.author_id == 4284
    assert chief.url == "https://elibrary.ru/author_profile.asp?authorid=4284"
    assert chief.degree == "академик РАН"
    assert chief.role == "ГЛАВНЫЙ РЕДАКТОР"
    assert [(a.id, a.city) for a in chief.affiliations] == [(34, "Новосибирск"), (214, "Новосибирск")]
    assert chief.affiliations[1].name == "Новосибирский национальный исследовательский государственный университет"
    assert [(m.name, m.author_id, m.degree) for m in profile.deputy_editors] == [
        ("Медведев Сергей Борисович", 60351, "д.ф.-м.н.")
    ]
    assert [(m.name, m.author_id, m.degree) for m in profile.executive_secretaries] == [
        ("Барахнин Владимир Борисович", 10510, "д.т.н.")
    ]
    assert profile.other_staff == {}


def test_profile_drops_personal_emails(profile):
    """Envelope links of the editorial staff (mailto:) are personal data: only the office contact
    e-mail of the 'КОНТАКТНАЯ ИНФОРМАЦИЯ' block is kept; name, role, degree and affiliations stay."""
    people = [profile.editor_in_chief, *profile.deputy_editors, *profile.executive_secretaries,
              *profile.editorial_board]
    assert len(people) == 28
    assert all("email" not in m.model_dump() for m in people)
    dumped = profile.model_dump_json()
    for personal in ("person1@example.org", "person3@example.org", "person4@example.org", "person17@example.org"):
        assert personal not in dumped
    assert re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", dumped) == ["jct@ict.nsc.ru"]
    assert {m.role for m in profile.editorial_board} == {"РЕДАКЦИОННАЯ КОЛЛЕГИЯ"}
    assert [m.role for m in profile.deputy_editors] == ["ЗАМЕСТИТЕЛИ ГЛАВНОГО РЕДАКТОРА"]
    # people are still recognised by the envelope icon even when it is the only marker
    assert all(m.affiliations for m in profile.editorial_board)


def test_profile_email_only_from_the_contact_block():
    html = fixture_bytes("title_about_8610").decode("utf-8")
    anchor = "<td><img src=\"/images/but_orange_question.gif\" class=\"imghelp help\" title='<p>Если издание"
    assert html.count(anchor) == 1  # the "Год основания" cell of "СВЕДЕНИЯ ОБ ИЗДАНИИ"
    # an "Email:" pair and an unknown pair with an address outside "КОНТАКТНАЯ ИНФОРМАЦИЯ"
    html = html.replace(anchor, (
        "<td>&nbsp;&nbsp;Email:&nbsp;<font color=#00008f>secretary@example.org</font></td></tr>"
        "<tr><td>&nbsp;&nbsp;Контакт секретаря:&nbsp;<font color=#00008f>Иванов И.И., ivanov@example.org</font>"
        "</td></tr><tr align=left valign=bottom>" + anchor
    ), 1)
    p = parse_journal_profile(html)
    assert p.email == "jct@ict.nsc.ru"  # the contact block still wins
    assert p.foundation_year == 1995
    assert p.extra["Контакт секретаря"] == "Иванов И.И."
    dumped = p.model_dump_json()
    assert "secretary@example.org" not in dumped and "ivanov@example.org" not in dumped
    # without the contact block e-mail, the other one is still not used
    p = parse_journal_profile(html.replace("Email:&nbsp;<font color=#00008f>jct@ict.nsc.ru</font>", "", 1))
    assert p.email is None


def test_strip_emails():
    assert strip_emails("Рукописи: a@x.ru, b@y.ru.") == "Рукописи."
    assert strip_emails("Иванов (ivanov@mail.ru), Петров") == "Иванов, Петров"
    text = "Абзац 1\n\nПишите: mailto:x@y.org\n\nАбзац 3"
    assert strip_emails(text) == "Абзац 1\n\nПишите\n\nАбзац 3"
    assert strip_emails("Меди@льманах") == "Меди@льманах"  # not an address
    assert strip_emails(None) == ""


def test_profile_editorial_board(profile):
    board = profile.editorial_board
    assert len(board) == 25
    assert board[0].name == "Баутин Сергей Петрович"
    assert board[0].author_id == 3369
    assert board[0].degree == "professor"
    assert board[0].affiliations[0].id is None  # organization without a profile link
    assert board[0].affiliations[0].city == "Снежинск"
    # members without an author profile link are still recognised as people
    zh = board[1]
    assert (zh.name, zh.author_id, zh.url) == ("Жумагулов Бакытжан Турсынович", None, None)
    assert zh.degree == "академик НИА РК"
    assert [(a.id, a.name, a.city) for a in zh.affiliations] == [
        (17537, "Национальная инженерная академия Республики Казахстан", "Алматы")
    ]
    kr = next(m for m in board if m.name == "Крейнович Владислав Яковлевич")
    assert [(a.name, a.city) for a in kr.affiliations] == [("Техасский университет в Эль-Пасо", "Техас")]
    tem = next(m for m in board if m.name == "Темирбеков Нурлан Муханович")
    assert [a.id for a in tem.affiliations] == [17537, 729]
    assert board[-1].name == "Шрёдер Вольфганг"
    assert board[-1].affiliations[0].city == "Ахен"
    assert all(m.affiliations for m in board)


def test_profile_review_and_rubrics(profile):
    pr = profile.peer_review
    assert pr is not None
    assert pr.type == "одностороннее слепое рецензирование"
    assert pr.reviewers == 2
    assert pr.rejection_rate == 24.0
    assert pr.publication_time == "120 дней"
    assert pr.publication_days == 120
    assert pr.performed_by == "внешними экспертами, отобранными редакцией"
    assert [(r.code, r.name) for r in profile.rubrics_grnti] == [
        ("200000", "Информатика"), ("500000", "Автоматика. Вычислительная техника"), ("270000", "Математика")
    ]
    assert [(r.code, r.name) for r in profile.rubrics_oecd] == [
        ("101", "Mathematics"), ("102", "Computer and information sciences")
    ]
    assert [(r.code, r.name) for r in profile.vak_specialties] == [
        ("010000", "Естественные науки"), ("020300", "Информационные технологии и телекоммуникации")
    ]
    assert profile.other_rubrics == {}


def test_profile_sections_files_texts(profile):
    assert [(s.id, s.name) for s in profile.sections] == [
        (351749, "Математическое моделирование"), (351750, "Вычислительные технологии"),
        (351751, "Информационные технологии"),
    ]
    assert profile.sections[0].url == "https://elibrary.ru/title_items.asp?id=8610&section=351749"
    assert len(profile.files) == 7
    assert (profile.files[0].kind, profile.files[0].name, profile.files[0].file_id) == (
        "Положение о порядке рецензирования", "Положение о рецензировании статей ЖВТ.rtf", 2)
    assert (profile.files[-1].kind, profile.files[-1].name, profile.files[-1].file_id) == (
        "Типовой авторский договор", "License_contract_ru.pdf", 8)
    assert [f.kind for f in profile.files].count("Выходные данные") == 2
    assert profile.editorial_policy is not None
    assert profile.editorial_policy.startswith("Вычислительные и информационные технологии играют важную роль")
    assert profile.editorial_policy.count("\n\n") == 2  # three paragraphs
    assert profile.history is not None and profile.history.startswith("Предшественником журнала являлся")
    assert profile.last_modified == "10.03.2026"
    assert profile.latest_issue is None  # the profile page does not link issues
    assert profile.metrics == {} and profile.impact_factor_2y is None
    assert profile.extra == {}


def test_profile_english_variant_by_url():
    p = parse_journal_profile(fixture_bytes("title_about_8610"),
                              url="https://elibrary.ru/title_about_new.asp?id=8610&langid=2")
    assert p.name_en == "Вычислительные технологии"  # the name shown on the page that was parsed
    assert p.url.endswith("langid=2")


def test_profile_without_url_and_json_round_trip(profile):
    p = parse_journal_profile(fixture_bytes("title_about_8610").decode("utf-8"))
    assert p.id == 8610 and p.url == "https://elibrary.ru/title_about_new.asp?id=8610"
    again = JournalProfile.model_validate_json(profile.model_dump_json())
    assert again == profile


def test_profile_unknown_blocks_go_to_extra():
    html = fixture_bytes("title_about_8610").decode("utf-8")
    html = html.replace("Мультидисциплинарность:", "Новый признак:")
    html = html.replace("ИСТОРИЯ ПЕРЕИМЕНОВАНИЙ И ПЕРЕВОДА:", "НОВЫЙ БЛОК:")
    p = parse_journal_profile(html)
    assert p.extra["Новый признак"] == "не является мультидисциплинарным"
    assert p.extra["НОВЫЙ БЛОК"].startswith("Предшественником журнала")
    assert p.multidisciplinary is None and p.history is None


def test_profile_errors():
    with pytest.raises(ParseError):
        parse_journal_profile(fixture_bytes("captcha"))
    with pytest.raises(ParseError):
        parse_journal_profile(fixture_bytes("titles_results"))
    with pytest.raises(NotFound):
        parse_journal_profile("<html><body><table><tr><td>Журнал не найден</td></tr></table></body></html>")


# ==================================================================================================
# title_profile.asp (no fixture: synthetic page in the markup of org_profile.asp)
# ==================================================================================================

_ROW = ('<tr><td align=center valign=top><img src="/images/but_orange_question.gif" class="imghelp help"></td>'
        '<td align=left valign=top class="midtext"><font color=#00008f>{}</font></td>'
        '<td align=center class="midtext"><font color=#000000>{}</font></td></tr>')

SYNTHETIC_TITLE_PROFILE = (
    "<html><head><title>Вычислительные технологии / анализ публикационной активности журнала</title></head>"
    "<body><table width=580><tr><td>"
    '<a href="title_about_new.asp?id=8610"><b>ВЫЧИСЛИТЕЛЬНЫЕ ТЕХНОЛОГИИ</b></a>'
    '<select name="yearpubl"><option value="2025" selected>2025<option value="2024">2024</select>'
    '<div><font color=#000000><b>ПОКАЗАТЕЛИ ЖУРНАЛА</b></font></div>'
    '<table width=580 cellspacing=0 cellpadding=3>'
    '<tr><td width=20></td><td class="midtext"><font color=#555555>&nbsp;<b>Название показателя</b></font></td>'
    '<td class="midtext"><font color=#555555><b>Значение</b></font></td></tr>'
    '<tr><td colspan=3><div class="table-line"></div></td></tr><tr><td colspan=3></td></tr>'
    + _ROW.format("Общее число статей в РИНЦ", '<a href="title_items.asp?id=8610">2275</a>')
    + _ROW.format("Суммарное число цитирований журнала в РИНЦ", "12345")
    + _ROW.format("Двухлетний импакт-фактор РИНЦ", "0,657")
    + _ROW.format("Двухлетний импакт-фактор РИНЦ без самоцитирования", "0,580")
    + _ROW.format("Пятилетний импакт-фактор РИНЦ", "0,712")
    + _ROW.format("Индекс Хирша", "25")
    + _ROW.format("Место в общем рейтинге SCIENCE INDEX за 2025 год", "512")
    + _ROW.format("Показатель журнала в рейтинге SCIENCE INDEX", "1,234")
    + _ROW.format("Процент самоцитирования", "12,5%")
    + "</table>"
    '<div><font color=#000000><b>ДРУГИЕ ПОКАЗАТЕЛИ</b></font></div>'
    '<table><tr><td></td><td>Название показателя</td><td>Значение</td></tr>'
    + _ROW.format("Индекс Хирша", "30")
    + "</table></td></tr></table></body></html>"
)


def test_indicators_synthetic_page():
    ind = parse_journal_indicators(SYNTHETIC_TITLE_PROFILE, url="https://elibrary.ru/title_profile.asp?id=8610")
    assert isinstance(ind, JournalIndicators)
    assert ind.journal_id == 8610
    assert ind.journal_name == "Вычислительные технологии"
    assert ind.year == 2025
    assert ind.impact_factor_2y == pytest.approx(0.657)
    assert ind.impact_factor_2y_no_self == pytest.approx(0.580)
    assert ind.impact_factor_5y == pytest.approx(0.712)
    assert ind.h_index == 25  # the first table wins
    assert ind.science_index_rank == 512
    assert ind.science_index_rating == pytest.approx(1.234)
    assert ind.citations_total == 12345
    assert ind.self_citation_percent == pytest.approx(12.5)
    assert ind.metrics["Общее число статей в РИНЦ"] == "2275"
    assert ind.metrics["Двухлетний импакт-фактор РИНЦ"] == "0,657"
    assert ind.metrics["ДРУГИЕ ПОКАЗАТЕЛИ: Индекс Хирша"] == "30"  # repeated name keeps its section
    assert len(ind.metrics) == 10
    assert ind.url == "https://elibrary.ru/title_profile.asp?id=8610"


def test_indicators_merge_into_profile(profile):
    ind = parse_journal_indicators(SYNTHETIC_TITLE_PROFILE)
    merged = profile.with_indicators(ind)
    assert merged.impact_factor_2y == pytest.approx(0.657)
    assert merged.impact_factor_5y == pytest.approx(0.712)
    assert merged.h_index == 25
    assert merged.science_index_rating == pytest.approx(1.234)
    assert merged.metrics_year == 2025
    assert merged.metrics["Индекс Хирша"] == "25"
    assert merged.issn == profile.issn and profile.metrics == {}  # original untouched


def test_indicator_tables_on_real_analysis_markup():
    """The generic 'Название показателя' table reader works on the real org_profile.asp layout."""
    tree = remove_noise(parse_html(fixture_bytes("org_profile_2541")))
    rows = _indicator_rows(tree)
    assert rows[0] == ("ОБЩИЕ ПОКАЗАТЕЛИ", "Число публикаций на elibrary.ru", "502087")
    assert ("ОБЩИЕ ПОКАЗАТЕЛИ", "Индекс Хирша по публикациям в РИНЦ", "469") in rows
    assert ("ПОКАЗАТЕЛИ ЗА 5 ЛЕТ (2021-2025)", "Число публикаций, входящих в ядро РИНЦ", "44995 (36,7%)") in rows
    assert ("ПОКАЗАТЕЛИ ПО ГОДАМ", "Число публикаций на портале elibrary.ru (2016)", "24879") in rows


def test_indicators_errors():
    with pytest.raises(ParseError):
        parse_journal_indicators(fixture_bytes("title_about_8610"))


# ==================================================================================================
# contents.asp
# ==================================================================================================


@pytest.fixture(scope="module")
def issue() -> IssueContents:
    return parse_issue_contents(fixture_bytes("contents_8610"), url="https://elibrary.ru/contents.asp?titleid=8610")


def test_issue_header(issue):
    assert issue.journal_id == 8610
    assert issue.journal_name == "Вычислительные технологии"
    assert issue.issue_id == 91982811
    assert issue.edn == "MABYSK"
    assert issue.label == "Т.31 № 4 за 2026 год"
    assert (issue.year, issue.volume, issue.number) == (2026, "31", "4")
    assert issue.articles_count == 10
    assert issue.url == "https://elibrary.ru/contents.asp?id=91982811"
    assert issue.publisher is not None
    assert (issue.publisher.id, issue.publisher.city) == (34, "Новосибирск")
    assert issue.publisher.name == "Федеральный исследовательский центр информационных и вычислительных технологий"
    assert issue.extra == {
        "Стоимость выпуска": "1633 руб.",
        "Подписка на журнал": "2027 год - 8000 руб.; 2026 год - 6500 руб.; 2025 год - 6500 руб.; 2024 год - 6500 руб.",
    }


def test_issue_items(issue):
    assert len(issue.items) == 10
    assert issue.sections == ["МАТЕМАТИЧЕСКОЕ МОДЕЛИРОВАНИЕ", "ВЫЧИСЛИТЕЛЬНЫЕ ТЕХНОЛОГИИ", "ИНФОРМАЦИОННЫЕ ТЕХНОЛОГИИ"]
    first = issue.items[0]
    assert first.id == 91982812
    assert first.title == "КОРРЕЛЯЦИОННО-РЕГРЕССИОННЫЙ АНАЛИЗ ВРЕМЕННЫХ РЯДОВ ПОТРЕБЛЕНИЯ ПРИРОДНОГО ГАЗА"
    assert first.authors == ["Башарина О.Ю."]
    assert (first.pages, first.section, first.kind, first.year, first.citations, first.position) == (
        "7-19", "МАТЕМАТИЧЕСКОЕ МОДЕЛИРОВАНИЕ", "article", 2026, 0, 1)
    assert first.url == "https://elibrary.ru/item.asp?id=91982812"
    assert issue.items[1].authors == ["Жарков М.Л.", "Казаков А.Л.", "Лемперт А.А."]
    assert issue.items[1].section == "МАТЕМАТИЧЕСКОЕ МОДЕЛИРОВАНИЕ"
    assert (issue.items[2].id, issue.items[2].section, issue.items[2].pages) == (
        91982814, "ВЫЧИСЛИТЕЛЬНЫЕ ТЕХНОЛОГИИ", "31-44")
    assert issue.items[3].authors == ["Бычков И.В.", "Москвичев В.В.", "Постникова У.С.", "Тасейко О.В.",
                                      "Чернякова Н.А."]
    last = issue.items[-1]
    assert (last.id, last.pages, last.section, last.position) == (91982821, "123-131", "ИНФОРМАЦИОННЫЕ ТЕХНОЛОГИИ", 10)
    assert [h.section for h in issue.items].count("ИНФОРМАЦИОННЫЕ ТЕХНОЛОГИИ") == 7
    assert all(h.kind == "article" and h.year == 2026 for h in issue.items)


def test_issue_list(issue):
    issues = issue.available_issues
    assert len(issues) == 223  # Online First + the 222 issues of 'Всего выпусков на eLIBRARY.RU'
    assert issues[0] == IssueRef(id=49632283, year=None, volume=None, number=None, label="Online First", articles=0,
                                 url="https://elibrary.ru/contents.asp?id=49632283")
    assert issues[1] == IssueRef(id=89118995, year=2026, volume="31", number="1", label="Т. 31 № 1", articles=9,
                                 url="https://elibrary.ru/contents.asp?id=89118995")
    current = [r for r in issues if r.current]
    assert len(current) == 1
    assert current[0] == IssueRef(id=91982811, year=2026, volume="31", number="4", label="Т. 31 № 4", articles=10,
                                  current=True, url="https://elibrary.ru/contents.asp?id=91982811")
    assert [r.label for r in issues if r.year == 2026] == ["Т. 31 № 1", "Т. 31 № 2", "Т. 31 № 3", "Т. 31 № 4"]
    s1 = next(r for r in issues if r.id == 34480313)
    assert (s1.year, s1.volume, s1.number, s1.articles) == (2017, "22", "S1", 9)
    assert issues[-1] == IssueRef(id=33386263, year=1996, volume="1", number="3", label="Т. 1 № 3", articles=14,
                                  url="https://elibrary.ru/contents.asp?id=33386263")
    years = [r.year for r in issues if r.year]
    assert years == sorted(years, reverse=True)
    assert len({r.id for r in issues}) == 223


def test_issue_contents_without_url():
    c = parse_issue_contents(fixture_bytes("contents_8610").decode("utf-8"))
    assert c.issue_id == 91982811 and c.url == "https://elibrary.ru/contents.asp?id=91982811"


def test_issue_errors():
    with pytest.raises(ParseError):
        parse_issue_contents(fixture_bytes("titles_results"))
    with pytest.raises(ParseError):
        parse_issue_contents(fixture_bytes("captcha"))


@pytest.mark.parametrize(
    ("label", "expected"),
    [("Т. 31 № 4", ("31", "4")), ("Т.31 № 4 за 2026 год", ("31", "4")), ("Т. 22 № S1", ("22", "S1")),
     ("№ 3", (None, "3")), ("Вып. 5", (None, "5")), ("Т. 5, № 1-2", ("5", "1-2")), ("Online First", (None, None)),
     ("", (None, None)), (None, (None, None))],
)
def test_split_issue_label(label, expected):
    assert split_issue_label(label) == expected
