"""Tests of the publications area: query model + form builder, search results, rows, item cards,
full-text flags. All pages are fixtures saved from elibrary.ru on 2026-10-13 (no network)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError

from elibrary_cli.errors import CaptchaRequired, NotFound, ParseError
from elibrary_cli.models.base import SearchPage
from elibrary_cli.models.publications import ALL_TYPES, Publication, PublicationQuery
from elibrary_cli.parsers.common import parse_html
from elibrary_cli.parsers.item import normalize_kind, parse_fulltext_flag, parse_publication
from elibrary_cli.parsers.rows import (
    infer_kind,
    parse_publication_rows,
    parse_reference_rows,
    row_year,
    source_year,
)
from elibrary_cli.parsers.search import parse_search_results
from elibrary_cli.queries.publications import (
    ARTICLE_TYPES,
    PATENT_TYPES,
    SEARCH_PATH,
    build_search_form,
    effective_where,
    normalize_author_name,
    page_params,
    search_signature,
)
from tests.conftest import fixture_bytes

# =========================================================================== query model


class TestPublicationQuery:
    def test_defaults_match_fresh_site_form(self):
        q = PublicationQuery(text="нейронные сети")
        assert q.where == ["name", "abstract", "keywords"]
        assert q.types == list(ALL_TYPES)
        assert q.fuzzy is True and q.morphology is False
        assert (q.period, q.sort, q.order, q.page) == ("all", "rank", "desc", 1)

    def test_text_is_stripped_and_collapsed(self):
        assert PublicationQuery(text="  нейронные \n  сети ").text == "нейронные сети"

    @pytest.mark.parametrize("kwargs, message", [
        ({"text": "x"}, "не менее двух символов"),
        ({}, "Не заданы условия поиска"),
        ({"text": "   "}, "Не заданы условия поиска"),
        ({"text": "сети", "where": []}, "по каким полям"),
        ({"text": "сети", "types": []}, "по каким типам"),
        ({"text": "сети", "year_from": 2020, "year_to": 2010}, "больше конечного"),
    ])
    def test_validation_errors_are_russian(self, kwargs, message):
        with pytest.raises(ValidationError) as ei:
            PublicationQuery(**kwargs)
        assert message in str(ei.value)

    def test_bad_literals_and_ranges(self):
        with pytest.raises(ValidationError):
            PublicationQuery(text="сети", types=["novel"])
        with pytest.raises(ValidationError):
            PublicationQuery(text="сети", where=["body"])
        with pytest.raises(ValidationError):
            PublicationQuery(text="сети", page=0)
        with pytest.raises(ValidationError):
            PublicationQuery(text="сети", year_from=1500)

    def test_filters_without_text_are_enough(self):
        assert PublicationQuery(author_names=["Петров В А"]).text is None
        assert PublicationQuery(journal_ids=[8610]).journal_ids == [8610]
        assert PublicationQuery(rubric_codes="500000").rubric_codes == ["500000"]
        assert PublicationQuery(in_previous_results=True).in_previous_results

    def test_aliases_strings_and_dedupe(self):
        q = PublicationQuery(text="сети", where="title,annotation,title", types="patents articles",
                             order="rev", author_names="Петров В.А.|Иванов И И|Петров В.А.")
        assert q.where == ["name", "abstract"]
        assert q.types == ["patent", "article"]
        assert q.order == "desc"
        assert q.author_names == ["Петров В.А.", "Иванов И И"]
        assert PublicationQuery(text="сети", types="all").types == list(ALL_TYPES)
        assert PublicationQuery(text="сети", order="nrm").order == "asc"


# =========================================================================== form builder


class TestBuildSearchForm:
    def test_default_form_exact_browser_order(self):
        form = build_search_form(PublicationQuery(text="нейронные сети"))
        assert form == [
            ("querybox_name", ""), ("authors_all", ""), ("titles_all", ""), ("rubrics_all", ""),
            ("changed", "1"), ("queryid", ""), ("ftext", "нейронные сети"),
            ("where_name", "on"), ("where_abstract", "on"), ("where_keywords", "on"),
            ("where_references", ""),
            ("type_article", "on"), ("type_disser", "on"), ("type_book", "on"), ("type_report", "on"),
            ("type_conf", "on"), ("type_patent", "on"), ("type_preprint", "on"), ("type_grant", "on"),
            ("type_dataset", "on"),
            ("search_itemboxid", ""), ("search_freetext", "on"),
            ("begin_year", "0"), ("end_year", "0"), ("issues", "all"), ("orderby", "rank"),
            ("order", "rev"), ("queryboxid", "0"), ("save_queryboxid", "0"),
        ]

    def test_patent_query_like_the_saved_fixture(self):
        # query_results_patents.html was produced by this query (sorted by date, patents only)
        q = PublicationQuery(text="нейронные сети", types=PATENT_TYPES, sort="date")
        form = dict(build_search_form(q))
        assert form["type_patent"] == "on"
        assert "type_article" not in form and "type_book" not in form
        assert (form["orderby"], form["order"]) == ("date", "rev")
        assert form["changed"] == "1"

    def test_all_options(self):
        q = PublicationQuery(
            text="машинное обучение", where=["keywords", "affiliation", "name"], types=["conf", "article"],
            year_from=2020, year_to=2025, period="m6", sort="cited", order="asc", morphology=True, fuzzy=False,
            fulltext_only=True, open_only=True, in_previous_results=True, itembox_id=123,
            author_names=["Петров В.А.", "Иванов  И. И."], journal_ids=[8610, 7745], rubric_codes=["500000", "270000"],
        )
        form = build_search_form(q)
        names = [k for k, _ in form]
        # DOM order: name, affiliation, abstract, fulltext, keywords; article before conf
        assert names[7:11] == ["where_name", "where_affiliation", "where_keywords", "where_references"]
        assert names[11:13] == ["type_article", "type_conf"]
        d = dict(form)
        assert d["authors_all"] == "Петров В А|Иванов И И|"
        assert d["titles_all"] == "8610|7745|"
        assert d["rubrics_all"] == "500000|270000|"
        assert d["search_itemboxid"] == "123"
        assert [k for k in names if k.startswith("search_") and k != "search_itemboxid"] == [
            "search_morph", "search_fulltext", "search_open", "search_results"]
        assert (d["begin_year"], d["end_year"], d["issues"], d["orderby"], d["order"]) == (
            "2020", "2025", "m6", "cited", "nrm")

    def test_fulltext_checks_every_field_like_the_site(self):
        q = PublicationQuery(text="сети", where=["fulltext"])
        assert effective_where(q) == ["name", "affiliation", "abstract", "fulltext", "keywords"]
        names = [k for k, _ in build_search_form(q)]
        assert names[7:13] == ["where_name", "where_affiliation", "where_abstract", "where_fulltext",
                               "where_keywords", "where_references"]

    def test_author_only_query(self):
        d = dict(build_search_form(PublicationQuery(author_names=["Петров В А"])))
        assert d["ftext"] == "" and d["authors_all"] == "Петров В А|"

    def test_page_is_not_part_of_the_form_and_signature(self):
        a = PublicationQuery(text="нейронные сети", page=1)
        b = PublicationQuery(text="нейронные сети", page=7)
        assert build_search_form(a) == build_search_form(b)
        assert search_signature(a) == search_signature(b)
        assert len(search_signature(a)) == 16
        assert search_signature(a) != search_signature(PublicationQuery(text="нейронные сети", sort="date"))
        assert search_signature(a) != search_signature(PublicationQuery(text="нейронная сеть"))

    def test_page_params_and_constants(self):
        assert page_params(3) == [("pagenum", "3")]
        with pytest.raises(ValueError):
            page_params(0)
        assert SEARCH_PATH == "/query_results.asp"
        assert ARTICLE_TYPES == ("article",)
        assert normalize_author_name(" Петров  В.А. ") == "Петров В А"

    def test_form_urlencodes_utf8(self):
        body = urlencode(build_search_form(PublicationQuery(text="сети")))
        assert "ftext=%D1%81%D0%B5%D1%82%D0%B8" in body
        assert body.startswith("querybox_name=&authors_all=&titles_all=&rubrics_all=&changed=1&queryid=&ftext=")


# =========================================================================== search results


@pytest.fixture(scope="module")
def page() -> SearchPage:
    q = PublicationQuery(text="нейронные сети")
    return parse_search_results(fixture_bytes("query_results_all_types"), query=q,
                                url="https://elibrary.ru/query_results.asp")


class TestSearchResults:
    def test_counters_and_paging(self, page):
        assert page.total == 290705
        assert page.total_in_database == 82293065
        assert (page.page, page.per_page, page.pages) == (1, 100, 2908)
        assert page.has_next is True
        assert len(page.items) == 100
        assert page.query["text"] == "нейронные сети"
        assert page.source_url == "https://elibrary.ru/query_results.asp"
        assert [h.position for h in page.items] == list(range(1, 101))

    def test_patent_row(self, page):
        h = page.items[0]
        assert h.id == 47430099
        assert h.title.startswith("ОТКАЗОУСТОЙЧИВАЯ НЕЙРОННАЯ СЕТЬ")
        assert h.authors == ["Суханова Н.В.", "Шептунов С.А."]
        assert h.source == "Патент на изобретение RU 2760636 C2, 29.11.2021. Заявка № 2018145179 от 19.12.2018."
        assert h.kind == "patent"
        assert h.year == 2021  # publication date, not the application date (2018)
        assert h.citations == 1
        assert h.url == "https://elibrary.ru/item.asp?id=47430099"

    def test_journal_row_has_no_spurious_spaces(self, page):
        h = page.items[2]
        assert h.id == 65662365
        assert h.source == "Тенденции развития науки и образования. 2024. № 106-11. С. 119-125."
        assert (h.kind, h.year, h.citations) == ("article", 2024, 12)

    def test_conference_paper_row(self, page):
        h = page.items[1]
        assert h.id == 41770713
        assert h.authors == ["Зелинов М.А."]
        assert h.source.startswith("В сборнике: Гуманитарные науки в современном вузе")
        assert (h.kind, h.year, h.citations) == ("conference", 2019, 23)

    def test_chapter_row_without_authors(self, page):
        h = next(x for x in page.items if x.id == 76098153)
        assert h.position == 70
        assert h.authors == []  # the <i> inside the source is the book's author
        assert h.source == ("В книге: Обработка естественного языка с применением программных средств. "
                            "Мифтахова Р.Г. Монография. Уфа, 2024. С. 108.")
        assert (h.kind, h.year) == ("chapter", 2024)

    def test_other_kinds(self, page):
        by_id = {h.id: h for h in page.items}
        assert by_id[30339972].kind == "dissertation" and by_id[30339972].year == 2009
        assert by_id[50541112].kind == "grant" and by_id[50541112].year == 1995
        assert by_id[61283635].kind == "book" and by_id[61283635].source == "Липецк, 2023."
        assert by_id[48372694].kind == "patent" and by_id[48372694].year == 2022
        # journal named "Труды ... конференции" linked to an issue -> journal article
        graphicon = next(h for h in page.items if h.source and "Графикон" in h.source)
        assert graphicon.kind == "article"
        counts = Counter(h.kind for h in page.items)
        assert counts == {"conference": 46, "article": 41, "patent": 7, "chapter": 3, "book": 1,
                          "grant": 1, "dissertation": 1}

    def test_patents_only_page(self):
        sp = parse_search_results(fixture_bytes("query_results_patents"))
        assert (sp.total, sp.total_in_database, sp.page, sp.pages) == (27920, 82293065, 1, 280)
        assert len(sp.items) == 100
        assert {h.kind for h in sp.items} == {"patent"}
        first = sp.items[0]
        assert first.id == 88918718
        assert first.source.startswith("Свидетельство о регистрации программы для ЭВМ RU 2026610196, 13.01.2026.")
        assert first.year == 2026  # not 2025 (application year)
        assert first.authors == ["Борисов В.В.", "Булыгина О.В.", "Зубарева В.Н.", "Соколов А.М."]
        assert sp.items[1].authors == []

    def test_current_page_from_highlighted_cell(self):
        html = fixture_bytes("query_results_all_types").decode("utf-8")
        html = html.replace('<td width=5% bgcolor=#5c6aa6 nowrap><font color=#ffffff>1</font></td>',
                            '<td width=5% bgcolor=#555555 nowrap><a href="query_results.asp?pagenum=1">1</a></td>')
        html = html.replace('<td width=5% bgcolor=#555555 nowrap><a href="query_results.asp?pagenum=3">3</a></td>',
                            '<td width=5% bgcolor=#5c6aa6 nowrap><font color=#ffffff>3</font></td>')
        sp = parse_search_results(html, page=1)
        assert sp.page == 3 and sp.pages == 2908

    def test_single_page_without_paginator(self):
        html = fixture_bytes("query_results_patents").decode("utf-8")
        start = html.index('<tr align=center valign=middle class="menus">')
        end = html.index("</tr></table>", start)
        html = html[:start] + html[end:]
        html = html.replace("<font color=#ff0000>27920</font>", "<font color=#ff0000>57</font>")
        sp = parse_search_results(html, page=1)
        assert (sp.total, sp.pages, sp.page, sp.has_next) == (57, 1, 1, False)

    @pytest.mark.parametrize("snippet", [
        "<b>ВСЕГО НАЙДЕНО ПУБЛИКАЦИЙ: <font>0</font> из <font>82293065</font></b>",
        "<b>РЕЗУЛЬТАТЫ ПОИСКОВОГО ЗАПРОСА</b><p>По Вашему запросу ничего не найдено</p>",
        "<p>Не найдено ни одной публикации, удовлетворяющей условиям запроса</p>",
    ])
    def test_empty_results(self, snippet):
        sp = parse_search_results(f"<html><body>{snippet}</body></html>")
        assert sp.total == 0 and sp.items == [] and sp.pages == 0 and sp.has_next is False

    def test_wrong_pages(self):
        with pytest.raises(CaptchaRequired):
            parse_search_results(fixture_bytes("captcha"))
        with pytest.raises(ParseError):
            parse_search_results(fixture_bytes("item_article_46371463"))
        with pytest.raises(ParseError):
            parse_search_results(fixture_bytes("querybox_form"))


# =========================================================================== shared rows


class TestRows:
    def test_author_items_has_99_cards_and_one_reference_row(self):
        tree = parse_html(fixture_bytes("author_items_3771"))
        hits = parse_publication_rows(tree)
        assert len(hits) == 99
        positions = [h.position for h in hits]
        assert 88 not in positions and positions[:3] == [1, 2, 3] and positions[-1] == 100
        refs = parse_reference_rows(tree)
        assert len(refs) == 1
        r = refs[0]
        assert (r.ref_id, r.position, r.year, r.citations) == (1062348813, 88, 2023, 5)
        assert r.title == "ПУТИ СОВЕРШЕНСТВОВАНИЯ НАУЧНО-МЕТОДИЧЕСКИХ ОСНОВ ПОИСКОВЫХ РАБОТ НА ТВЕРДЫЕ ПОЛЕЗНЫЕ ИСКОПАЕМЫЕ"
        assert r.authors == ["Машковцев Г.А.", "Петров В.А."]
        assert r.source == "2023."

    def test_author_items_kinds(self):
        hits = parse_publication_rows(parse_html(fixture_bytes("author_items_3771")))
        assert Counter(h.kind for h in hits) == {"conference": 50, "article": 48, "book": 1}
        first = hits[0]
        assert first.id == 91809593 and first.kind == "conference" and first.year == 2026
        book = next(h for h in hits if h.id == 59950405)
        assert book.kind == "book" and book.authors == []

    def test_issue_contents_default_kind(self):
        tree = parse_html(fixture_bytes("contents_8610"))
        assert {h.kind for h in parse_publication_rows(tree)} == {None}  # backward compatible
        hits = parse_publication_rows(tree, default_kind="article")
        assert len(hits) == 10 and {h.kind for h in hits} == {"article"}
        assert hits[0].id == 91982812
        assert hits[0].pages == "7-19"
        assert hits[0].section == "МАТЕМАТИЧЕСКОЕ МОДЕЛИРОВАНИЕ"
        assert hits[0].authors == ["Башарина О.Ю."]

    @pytest.mark.parametrize("source, kind", [
        ("Патент на изобретение RU 2797777 C1, 08.06.2023.", "patent"),
        ("Свидетельство о регистрации программы для ЭВМ RU 2025684703, 16.09.2025.", "patent"),
        ("автореферат диссертации на соискание ученой степени кандидата наук", "dissertation"),
        ("В книге: Название. Монография. Уфа, 2024. С. 108.", "chapter"),
        ("В сборнике: Информационные системы. Материалы IV Международной конференции. 2018. С. 79-83.", "conference"),
        ("В сборнике: Современные проблемы прикладной информатики. Сборник научных трудов. 2006. С. 116-120.",
         "chapter"),
        ("НИР: грант № 95-01-01081. Российский фонд фундаментальных исследований. 1995.", "grant"),
        ("Монография. Москва, 2020.", "book"),
        ("Липецк, 2023.", "book"),
        ("Фундаментальные исследования. 2011. № 4. С. 108-114.", "article"),
        ("Что-то непонятное", "other"),
        (None, None),
    ])
    def test_infer_kind(self, source, kind):
        assert infer_kind(source) == kind

    def test_infer_kind_options(self):
        assert infer_kind("Труды конференции «Графикон». 2023. № 33.", journal_link=True) == "article"
        assert infer_kind("Труды конференции «Графикон». 2023. № 33.") == "conference"
        assert infer_kind("", default="chapter") == "chapter"

    @pytest.mark.parametrize("source, journal_link, kind", [
        # words of a container or journal title are not the publication type
        ("В книге: Патентное право. Учебник. Москва, 2020. С. 5-30.", False, "chapter"),
        ("В сборнике: Подготовка научных кадров и диссертационные исследования. Материалы всероссийской "
         "конференции. Москва, 2021. С. 10-12.", False, "conference"),
        ("В сборнике: Автореферативный сборник трудов. 2019. С. 3-7.", False, "chapter"),
        ("Патенты и лицензии. Интеллектуальные права. 2024. № 11. С. 119-125.", True, "article"),
        ("Патенты и лицензии. Интеллектуальные права. 2024. № 11. С. 119-125.", False, "article"),
        ("Диссертационные исследования. 2020. № 1. С. 5-9.", False, "article"),
        ("Вестник патентного права и диссертационной практики. 2020. № 1. С. 5-9.", False, "article"),
        # the real patent / thesis lines start with their type
        ("Патент на полезную модель RU 240761 U1, 29.01.2026. Заявка № 2024140240 от 05.12.2024.", False,
         "patent"),
        ("Свидетельство о государственной регистрации базы данных RU 2026620112, 14.01.2026.", False, "patent"),
        ("диссертация на соискание ученой степени кандидата технических наук / Москва, 2020", False,
         "dissertation"),
        ("Диссертация на соискание ученой степени доктора наук", False, "dissertation"),
    ])
    def test_infer_kind_structural_markers_beat_title_words(self, source, journal_link, kind):
        assert infer_kind(source, journal_link=journal_link) == kind

    def test_journal_called_patents_stays_an_article(self):
        # a row linked to a journal issue whose journal is named "Патенты и лицензии ..." (a real
        # eLIBRARY journal) is a journal article, not a patent
        from elibrary_cli import exports

        html = fixture_bytes("query_results_all_types").decode("utf-8")
        orig = next(h for h in parse_publication_rows(parse_html(html)) if h.id == 65662365)
        assert orig.kind == "article"
        renamed = html.replace("Тенденции развития науки и образования",
                               "Патенты и лицензии. Интеллектуальные права", 1)
        hit = next(h for h in parse_publication_rows(parse_html(renamed)) if h.id == 65662365)
        assert hit.source.startswith("Патенты и лицензии. Интеллектуальные права. 2024.")
        assert hit.kind == "article"
        assert exports.zotero_item_type(hit) == "journalArticle"

    def test_row_year(self):
        src = "Патент на изобретение RU 2797777 C1, 08.06.2023. Заявка № 2022119359 от 16.11.2020."
        assert row_year(src, "patent") == 2023
        assert row_year(src, "article") == 2020
        assert row_year(None) is None

    @pytest.mark.parametrize(("source", "year"), [
        # live 2026-10 row (query_results_page2_graphene): the page range looks like a year
        ("Журнал физической химии. 2025. Т. 99. № 12. С. 1901-1909.", 2025),
        ("Физика твердого тела. 2023. Т. 65. № 3. С. 497-501.", 2023),
        ("Proceedings of the Conference. 2021. pp. 2001-2010.", 2021),
        ("Journal of Physics. 2019. Vol. 3. P. 2015.", 2019),
        ("Москва, 2020. 1984 с.", 2020),  # page count of a book
        ("В сборнике: К 75-летию Победы 1945 г. Томск, 2020. С. 5-7.", 2020),
        ("Сборник. С. 2015.", 2015),  # nothing else looks like a year: fall back to the page number
        ("Без года. С. 12-15.", None),
    ])
    def test_row_year_ignores_page_ranges(self, source, year):
        assert row_year(source) == year
        assert source_year(source) == year


# =========================================================================== item cards


@pytest.fixture(scope="module")
def article() -> Publication:
    return parse_publication(fixture_bytes("item_article_46371463"),
                             url="https://elibrary.ru/item.asp?id=46371463")


@pytest.fixture(scope="module")
def patent() -> Publication:
    return parse_publication(fixture_bytes("item_patent_47430099"))


@pytest.fixture(scope="module")
def book() -> Publication:
    return parse_publication(fixture_bytes("item_book_76097861"))


@pytest.fixture(scope="module")
def chapter() -> Publication:
    return parse_publication(fixture_bytes("item_chapter_76098153"))


@pytest.fixture(scope="module")
def disser() -> Publication:
    return parse_publication(fixture_bytes("item_disser_30339972"))


class TestArticleCard:
    def test_identifiers_and_title(self, article):
        assert article.id == 46371463
        assert article.edn == "FYOAKL"
        assert article.doi == "10.47056/0365-9615-2021-172-7-98-101"
        assert article.url == "https://elibrary.ru/item.asp?id=46371463"
        assert article.title.startswith("Длительное поддержание повышенного количества γН2АХ-позитивных")
        assert article.title_display.startswith("ДЛИТЕЛЬНОЕ ПОДДЕРЖАНИЕ ПОВЫШЕННОГО КОЛИЧЕСТВА γН2АХ")
        assert article.title_info is None

    def test_type_and_bibliographic_fields(self, article):
        assert article.type_raw == "статья в журнале - научная статья"
        assert article.kind == "article"
        assert (article.language, article.language_code) == ("русский", "ru")
        assert (article.volume, article.issue, article.issue_id, article.year, article.pages) == (
            "172", "7", 46371444, 2021, "98-101")
        assert article.dates.received == date(2021, 4, 30)
        assert article.dates.placed == date(2021, 8, 3)

    def test_authors(self, article):
        names = [a.name for a in article.authors]
        assert names == ["БЛОХИНА Т.М.", "ЯШКИНА Е.И.", "БЕЛЯЕВА А.Г.", "ПЕРЕВЕЗЕНЦЕВ А.А.", "ШТЕМБЕРГ А.С.",
                         "ОСИПОВ А.Н."]
        first = article.authors[0]
        assert first.full_name == "Блохина (Трубченкова) Таисия Михайловна"
        assert first.author_id == 1005793
        assert first.affiliations == [1, 2]
        assert first.affiliation_names == [
            "ГНЦ РФ - Федеральный медицинский биофизический центр им. А.И.Бурназяна ФМБА России",
            "ФИЦ химической физики им. Н.Н.Семенова РАН"]
        assert first.organization == "Федеральный медицинский биофизический центр им. А.И. Бурназяна ФМБА России"
        assert first.url == "https://elibrary.ru/author_profile.asp?authorid=1005793"
        assert first.role is None and first.corresponding is False
        assert [a.author_id for a in article.authors] == [1005793, 1026953, 183649, 1092583, 180081, 95135]
        assert article.authors[2].affiliations == [3]
        assert article.contributors == []

    def test_affiliations(self, article):
        affs = article.affiliations
        assert [(a.number, a.org_id) for a in affs] == [(1, 6841), (2, 81), (3, 623)]
        assert affs[0].name == "ГНЦ РФ - Федеральный медицинский биофизический центр им. А.И.Бурназяна ФМБА России"
        assert affs[0].full_name == "Федеральный медицинский биофизический центр им. А.И. Бурназяна ФМБА России"
        assert affs[0].location == "Москва, РФ"
        assert affs[0].city == "Москва"
        assert affs[1].location is None
        assert affs[2].url == "https://elibrary.ru/org_profile.asp?id=623"

    def test_journal_block(self, article):
        j = article.journal
        assert j.name == "БЮЛЛЕТЕНЬ ЭКСПЕРИМЕНТАЛЬНОЙ БИОЛОГИИ И МЕДИЦИНЫ"
        assert j.name_cased == "Бюллетень экспериментальной биологии и медицины"
        assert j.title_id == 8465
        assert j.url == "https://elibrary.ru/title_about_new.asp?id=8465"
        assert (j.issn, j.eissn) == ("0365-9615", "2413-1008")
        assert j.founders == 'АНО "Издательство Российской академии медицинских наук"'
        assert j.contents_url == "https://elibrary.ru/contents.asp?id=46371444"
        ix = j.indexing
        assert (ix.wos, ix.in_wos, ix.scopus, ix.in_scopus) == ("нет", False, "нет", False)
        assert (ix.white_list, ix.in_white_list, ix.white_list_level) == ("да (уровень 3)", True, 3)
        assert (ix.egpni, ix.in_egpni, ix.egpni_level) == ("да (уровень 1)", True, 1)
        assert (ix.vak, ix.in_vak, ix.vak_category) == ("да", True, None)
        assert (ix.rsci, ix.in_rsci) == ("да", True)
        assert (ix.science_index_rating, ix.si_percentile) == ("да (процентиль 11)", 11)
        assert ix.extra == {} and j.extra == {}

    def test_keywords_rubrics_metrics(self, article):
        assert article.keywords == ["γН2АХ", "ЛИМФОЦИТЫ", "ОБЕЗЬЯНЫ", "ИОНИЗИРУЮЩЕЕ ИЗЛУЧЕНИЕ", "ГИПОГРАВИТАЦИЯ"]
        r = article.rubrics
        assert r.oecd == ["Biological sciences"]
        assert r.grnti == ["Биология / Радиационная биология. Радиоэкология. Радиология"]
        assert r.asjc == [] and r.vak == [] and r.sntr == []
        m = article.metrics
        assert (m.in_rinc, m.rinc_citations, m.in_core, m.core_citations) == (True, 1, True, 0)
        assert (m.reviews, m.has_reviews) == ("нет данных", None)
        assert (m.views, m.views_registered, m.downloads, m.downloads_authors) == (48, 25, 3, 3)
        assert (m.collections, m.ratings_count, m.rating_avg, m.comments_count) == (41, 0, None, 0)
        assert m.extra == {}

    def test_funding_translation_fulltext(self, article):
        assert article.funding == "Работа выполнена в рамках темы РАН № 65.2."
        assert len(article.related) == 1
        tr = article.related[0]
        assert (tr.relation, tr.id) == ("translation", 47529716)
        assert tr.title.startswith("LONG-TERM PERSISTENCE OF INCREASED NUMBER OF γH2AX+ PERIPHERAL BLOOD")
        assert tr.authors[:2] == ["Blokhinа T.M.", "Yashkina E.I."] and len(tr.authors) == 6
        assert tr.source == "Bulletin of Experimental Biology and Medicine. 2021. Т. 172. № 1. С. 81-84."
        ft = article.fulltext
        assert (ft.available, ft.access, ft.price_rub) == (False, "paid", 455)
        assert ft.files == []
        assert [(x.kind, x.label, x.host, x.js) for x in ft.links] == [
            ("url", "Версия на другом языке (EN), на сайте", "elibrary.ru", "url_article(46371463, 1)")]
        assert "Приобрести эту публикацию за 455 руб." in ft.notes
        assert article.cover_url.startswith("https://elibrary.ru/itemfiles/0/4/6/3/7/1/4/4/4/")
        assert article.abstract is None and article.extra == {}

    def test_json_round_trip(self, article):
        data = json.loads(article.model_dump_json())
        assert data["dates"]["received"] == "2021-04-30"
        assert Publication.model_validate(data) == article


class TestPatentCard:
    def test_main_fields(self, patent):
        assert (patent.id, patent.edn, patent.doi) == (47430099, "HMNCEQ", None)
        assert patent.kind == "patent" and patent.type_raw == "патент на изобретение"
        assert patent.year == 2021
        assert patent.title_en == ("FAULT-TOLERANT NEURAL NETWORK, METHOD FOR DETECTING NEURAL NETWORK FAULTS, "
                                   "METHOD FOR ENSURING FAULT TOLERANCE OF NEURAL NETWORK")
        assert [a.name for a in patent.authors] == ["СУХАНОВА Н.В.", "ШЕПТУНОВ С.А."]
        assert [a.author_id for a in patent.authors] == [606676, 480550]
        assert patent.authors_en == ["SUKHANOVA N.V.", "SHEPTUNOV S.A."]
        assert patent.affiliations == []
        assert patent.journal is None and patent.book is None and patent.dissertation is None

    def test_patent_info(self, patent):
        p = patent.patent
        assert (p.number, p.country_code, p.kind_code, p.office) == ("RU 2760636 C2", "RU", "C2", "Россия")
        assert p.application_number == "2018145179"
        assert p.registration_date == date(2018, 12, 19)
        assert p.publication_date == date(2021, 11, 29)
        assert p.publication_year == 2021
        assert p.patent_type == "патент на изобретение"
        assert len(p.holders) == 1
        h = p.holders[0]
        assert h.name.startswith("Федеральное государственное автономное учреждение науки Институт конструкторско")
        assert (h.full_name, h.org_id, h.city) == ("Институт конструкторско-технологической информатики РАН",
                                                   7371, "Москва")
        assert [c.code for c in p.ipc] == ["G06F 11/20", "G06N 3/02"]
        assert p.ipc[1].description == ("Компьютерные системы, основанные на биологических моделях / "
                                        "использующие модели нейронных сетей")
        assert p.ipc[0].description.startswith("Обнаружение ошибок; исправление ошибок; контроль / Реагирование")
        assert patent.dates.published == date(2021, 11, 29)

    def test_metrics_and_fulltext(self, patent):
        m = patent.metrics
        assert (m.in_rinc, m.rinc_citations, m.in_core, m.core_citations) == (True, 1, False, 1)
        assert (m.views, m.views_registered, m.downloads, m.downloads_authors, m.collections) == (
            365, 163, 93, 17, 50)
        assert patent.rubrics.oecd == ["Electrical engineering, electronic engineering"]
        ft = patent.fulltext
        assert (ft.available, ft.access, ft.colour) == (True, "open", "green")
        assert [(f.kind, f.number, f.label, f.format, f.js) for f in ft.files] == [
            ("file", 1, "Полный текст (PDF)", "PDF", "file_article(47430099, 1)")]
        assert patent.dates.placed == date(2021, 12, 30)
        assert patent.extra == {}


class TestBookCard:
    def test_main_fields(self, book):
        assert (book.id, book.edn, book.kind, book.type_raw) == (76097861, "GVVMFP", "book", "монография")
        assert book.title == "Обработка естественного языка с применением программных средств"
        assert book.title_info == "Монография"
        assert book.year == 2024 and book.udk == "025.4"
        assert [(a.name, a.author_id, a.affiliations) for a in book.authors] == [("МИФТАХОВА Р.Г.", 676418, [1])]
        assert [(c.name, c.role, c.author_id) for c in book.contributors] == [
            ("МОРОЗКИНА Е.А.", "Научное редактирование", 674655)]
        assert book.contributors[0].affiliation_names == ["Уфимский университет науки и технологий"]
        assert [(a.number, a.org_id, a.city) for a in book.affiliations] == [(1, 20179, "Уфа")]

    def test_book_info(self, book):
        b = book.book
        assert b.isbn == ["978-5-7477-5904-6"]
        assert (b.city, b.year, b.pages_total, b.udk) == ("Уфа", 2024, 156, "025.4")
        assert (b.publisher.name, b.publisher.org_id, b.publisher.city) == (
            "Уфимский университет науки и технологий", 20179, "Уфа")

    def test_keywords_metrics_funding(self, book):
        assert book.keywords[:2] == ["НЕЙРОННЫЕ СЕТИ", "ЕСТЕСТВЕННЫЕ ЯЗЫКИ"] and len(book.keywords) == 7
        assert (book.metrics.reviews, book.metrics.has_reviews, book.metrics.rinc_citations) == ("есть", True, 1)
        assert book.rubrics.oecd == ["Media and communication"]
        assert book.rubrics.grnti == ["Информатика / Информационный поиск / Информационно-поисковые языки"]
        assert book.funding.startswith("Печатается по решению кафедры лингводидактики")

    def test_table_of_contents(self, book):
        toc = book.contents
        assert len(toc) == 22
        assert (toc[0].id, toc[0].title, toc[0].pages, toc[0].kind) == (
            76097979, "ПРОГРАММНЫЙ АНАЛИЗ ТЕКСТА", "4-21", "chapter")
        ch = next(h for h in toc if h.id == 76098153)
        assert (ch.title, ch.pages, ch.citations) == ("3.НЕЙРОННЫЕ СЕТИ И НЕЙРОННАЯ МОДЕЛЬ ЯЗЫКА", "108", 0)
        assert toc[-1].pages == "152-153"
        assert book.fulltext.available is False and book.fulltext.access is None


class TestChapterCard:
    def test_chapter(self, chapter):
        assert (chapter.id, chapter.edn, chapter.kind, chapter.type_raw) == (
            76098153, "RNESTV", "chapter", "глава в книге")
        assert chapter.title == "3.Нейронные сети и нейронная модель языка"
        assert (chapter.year, chapter.pages, chapter.language) == (2024, "108", "русский")
        assert chapter.authors == [] and chapter.book is None
        c = chapter.container
        assert (c.id, c.url) == (76097861, "https://elibrary.ru/item.asp?id=76097861")
        assert c.title == "ОБРАБОТКА ЕСТЕСТВЕННОГО ЯЗЫКА С ПРИМЕНЕНИЕМ ПРОГРАММНЫХ СРЕДСТВ"
        assert c.authors == ["Мифтахова Р.Г."]
        assert (c.description, c.year) == ("Монография. Уфа, 2024", 2024)
        assert (c.publisher.name, c.publisher.org_id) == ("Уфимский университет науки и технологий", 20179)
        assert chapter.fulltext.can_add_link is True and chapter.fulltext.available is False
        assert chapter.metrics.collections == 4
        assert chapter.rubrics.oecd == ["Languages and literature"]


class TestDissertationCard:
    def test_dissertation(self, disser):
        assert (disser.id, disser.edn, disser.kind, disser.year) == (30339972, "ZNYEDZ", "dissertation", 2009)
        assert disser.title.startswith("Структурно-функциональная организация нейронных сетей")
        assert [(a.name, a.author_id) for a in disser.authors] == [("ЗОЗУЛЯ Ю.И.", None)]
        d = disser.dissertation
        assert (d.document, d.is_autoreferat) == ("автореферат диссертации", True)
        assert d.degree == "доктор технических наук"
        assert d.specialty == "05.13.01 - Системный анализ, управление и обработка информации (по отраслям)"
        assert d.specialty_code == "05.13.01"
        assert (d.city, d.pages_total) == ("Уфа", 32)
        assert (d.defense_place.name, d.defense_place.org_id, d.defense_place.full_name) == (
            "Уфим. гос. авиац.-техн. ун-т", 20179, "Уфимский университет науки и технологий")
        assert disser.book is None
        assert disser.keywords == ["РАДИОЭЛЕКТРОНИКА", "КИБЕРНЕТИКА", "БИОНИКА", "НЕЙРОННЫЕ СЕТИ"]
        ft = disser.fulltext
        assert ft.access == "external" and ft.available is False
        assert [(x.label, x.host) for x in ft.links] == [("Полный текст, на сайте", "dlib.rsl.ru")]
        assert disser.metrics.collections == 141
        assert disser.extra == {}


# --------------------------------------------------------------------------- synthetic card

_TIP_A = ("<b><font color=#F26C4F>Вавилова (Насридинова) Дайана Дамировна</font></b><br>Ижевский государственный "
          "технический университет<br><a href=\"author_profile.asp?authorid=706281\">Профиль автора в SCIENCE "
          "INDEX</a>")
_TIP_O = ("<b><font color=#F26C4F>Ижевский государственный технический университет им. М.Т. Калашникова</font>"
          "</b> (Ижевск)<br><a href=\"org_profile.asp?id=7608\">Профиль организации</a>")

# Built from the structure of real cards saved by other projects (conference paper with abstract,
# English description, editors/reviewers sharing one cell): no such page is in the fixtures yet.
SYNTHETIC_CARD = f"""<html><head><title>Analysis of Passenger Traffic in the City of Izhevsk</title></head><body>
<table><tr><td align=left valign=top>
<table width=550><tr valign=middle><td width=2%></td>
<td>eLIBRARY ID:&nbsp;<font color=#00008f><a href="/item.asp?id=48182825">48182825</a></font></td>
<td>EDN:&nbsp;<font color=#00008f><a href="/isfsxs">ISFSXS</a></font></td>
<td>DOI:&nbsp;<font color=#00008f><a href=https://doi.org/10.1016/j.trpro.2022.01.006>10.1016/j.trpro.2022.01.006</a></font></td>
</tr></table>
<table width=550><tr><td width=534><span><b><p class='bigtext'>ANALYSIS OF PASSENGER TRAFFIC IN THE CITY OF IZHEVSK</p></b></span></td></tr></table>
<div style="width:580px;">
<table width=550><tr><td width=24></td><td width=514>
<div style='display: inline-block; white-space: nowrap'><b><font color=#00008f>KETOVA&nbsp;K.</font></b><font color=#000000><sup>1</sup></font>,</div>
<div style='display: inline-block; white-space: nowrap'><span class='help pointer' title='{_TIP_A}'><b><font color=#00008f>VAVILOVA&nbsp;D.</font></b></span><span class='astmd help' title='Автор, отвечающий за переписку'>*</span><font color=#000000><sup>1</sup></font></div>
<div style="height:10px;"></div>
<font color=#000000><sup>1</sup></font>&nbsp;<span class='help1 pointer' title='{_TIP_O}'><font color=#00008f>Kalashnikov Izhevsk State Technical University</font></span>, Izhevsk<br>
</td></tr></table>
<table width=550><tr><td width=24></td><td width=514>
<div style='height:5px;'></div>Ответственный редактор: <div style='display: inline-block; white-space: nowrap'><font color=#00008f>РОСТОВСКАЯ&nbsp;Т.К.</font><font color=#000000><sup>1</sup></font></div>
<div style="height:10px;"></div>
<font color=#000000><sup>1</sup></font>&nbsp;<font color=#00008f>Институт демографических исследований ФНИСЦ РАН</font><br><div style='height:5px;'></div>Рецензенты: <div style='display: inline-block; white-space: nowrap'><font color=#00008f>РЯЗАНЦЕВ&nbsp;С.В.</font><font color=#000000><sup>1</sup></font>,</div> <div style='display: inline-block; white-space: nowrap'><font color=#00008f>ЛАВРИКОВА&nbsp;Ю.Г.</font><font color=#000000><sup>2</sup></font></div>
<div style="height:10px;"></div>
<font color=#000000><sup>1</sup></font>&nbsp;<font color=#00008f>Институт демографических исследований ФНИСЦ РАН</font><br><font color=#000000><sup>2</sup></font>&nbsp;<font color=#00008f>Институт экономики УрО РАН</font><br>
</td></tr></table>
<table width=580><tr><td width=574 align=center>
Тип:&nbsp;<font color=#00008f>статья в сборнике трудов конференции</font>
<span style="margin-left:20px;"></span>Язык:&nbsp;<font color=#00008f>английский</font>
<span style="margin-left:20px;"></span>Год издания:&nbsp;<font color=#00008f>2022</font>
</td></tr><tr><td>
<div style='display: inline-block; white-space: nowrap'>Страницы:&nbsp;<font color=#00008f>29-35</font></div>
<span style="margin-left:20px;"></span>Тираж:&nbsp;<font color=#00008f>500</font>
<span style="margin-left:20px;"></span>Принята к публикации:&nbsp;<font color=#00008f>01.02.2022</font>
<span style="margin-left:20px;"></span>Особая отметка:&nbsp;<font color=#00008f>есть</font>
</td></tr></table>
<table width=550><tr><td colspan=2 align=left><font color=#000000>ИСТОЧНИК:</font></td></tr>
<tr><td width=34>&nbsp;</td><td width=504>
<a href="item.asp?id=48151008&selid=48182825">TRANSPORTATION RESEARCH PROCEDIA</a><br>
12. Сер. "12th International Conference: Territory Development" Том 61. 2022
<div style='height:5px;'></div>Издательство:&nbsp;<a href="publisher_about.asp?pubsid=21265">Elsevier B.V.</a>
</td></tr></table>
<table width=550><tr><td colspan=2 align=left><font color=#000000>КОНФЕРЕНЦИЯ:</font></td></tr>
<tr><td width=34>&nbsp;</td><td width=504>
<font color=#00008f>12TH INTERNATIONAL CONFERENCE ON TRANSPORT INFRASTRUCTURE: TITDS 2021</font><br>
<font color=#00008f>Irkutsk-Krasnoyarsk, 06–08 октября 2021 года</font>
</td></tr></table>
<table width=550><tr><td colspan=2 align=left><font color=#000000>КЛЮЧЕВЫЕ СЛОВА:</font></td></tr>
<tr><td width=34>&nbsp;</td><td width=504>
<a href="keyword_items.asp?id=1">BIG DATA</a>, <a href="keyword_items.asp?id=2">ROUTE</a>
</td></tr></table>
<table width=550><tr><td colspan=2 align=left><font color=#000000>АННОТАЦИЯ:</font></td></tr>
<tr><td width=34>&nbsp;</td><td width=504>
<div id="abstract1"><p align=justify>A technique is proposed that
allows you to analyze public transport routes.</p><p>Second paragraph.</p></div>
</td></tr></table>
<table width=550><tr><td colspan=2 align=left><font color=#000000>ОПИСАНИЕ НА АНГЛИЙСКОМ ЯЗЫКЕ:</font></td></tr>
<tr><td width=34>&nbsp;</td><td width=504 align=center>
<div style="height:5px;"></div><font color=#F26C4F>ANALYSIS OF PASSENGER TRAFFIC</font>
<div style="height:5px;"></div>
<div style='display: inline-block; white-space: nowrap'><font color=#00008f>KETOVA&nbsp;K.</font><font color=#000000><sup>1</sup></font></div>
<br><div style="height:5px;"></div><font color=#000000><sup>1</sup></font>&nbsp;<font color=#00008f>Izhevsk State Technical University</font><br>
</td></tr>
<tr><td width=34>&nbsp;</td><td width=504><div id="eabstract1"><p align=justify>English abstract text.</p></div></td></tr>
<tr><td width=34>&nbsp;</td><td width=504><p>Keywords: <a href="keyword_items.asp?id=3">ROUTE OPTIMIZATION</a>, <a href="keyword_items.asp?id=4">PUBLIC TRANSPORT</a></td></tr>
</table>
<table width=550><tr><td colspan=2 align=left><font color=#000000>СПИСОК ЛИТЕРАТУРЫ:</font></td></tr>
<tr><td width=34>&nbsp;</td><td width=504><table><tr><td>1. Ivanov I. First reference. 2020.</td></tr><tr><td>2. Second reference.</td></tr></table></td></tr>
</table>
<table width=550><tr><td colspan=2 align=left><font color=#000000>НОВЫЙ БЛОК:</font></td></tr>
<tr><td width=34>&nbsp;</td><td width=504>Что-то новое на сайте</td></tr></table>
</div>
</td>
<td align=right valign=top><div class="right-panel"><table>
<tr><td><a href="javascript:url_article(48182825, 1)"><img src="/images/download_blue.png"></a></td>
<td>Полный текст, на сайте <br><span><a href="javascript:url_article(48182825, 1)" class="help">www.sciencedirect.com</a></span></td></tr>
<tr><td></td><td><font color=#00008f>Дата размещения:</font><br><font color=#000000>15.03.2022</font></td></tr>
</table></div></td></tr></table></body></html>"""


@pytest.fixture(scope="module")
def pub() -> Publication:
    return parse_publication(SYNTHETIC_CARD)


class TestSyntheticConferenceCard:
    def test_ids_kind_dates(self, pub):
        assert (pub.id, pub.edn, pub.doi) == (48182825, "ISFSXS", "10.1016/j.trpro.2022.01.006")
        assert pub.title == "Analysis of Passenger Traffic in the City of Izhevsk"
        assert (pub.kind, pub.language_code, pub.year, pub.pages) == ("conference", "en", 2022, "29-35")
        assert pub.dates.accepted == date(2022, 2, 1)
        assert pub.dates.placed == date(2022, 3, 15)

    def test_unknown_labels_and_sections_are_kept(self, pub):
        assert pub.book is None
        assert pub.extra["Тираж"] == "500"  # book-only label on a non-book card
        assert pub.extra["Особая отметка"] == "есть"
        assert pub.extra["Новый блок"] == "Что-то новое на сайте"

    def test_authors_corresponding_and_groups(self, pub):
        assert [a.name for a in pub.authors] == ["KETOVA K.", "VAVILOVA D."]
        v = pub.authors[1]
        assert (v.corresponding, v.author_id, v.full_name) == (True, 706281, "Вавилова (Насридинова) Дайана Дамировна")
        assert pub.authors[0].corresponding is False and pub.authors[0].author_id is None
        assert [(a.number, a.org_id, a.city, a.location) for a in pub.affiliations] == [(1, 7608, "Ижевск", "Izhevsk")]
        assert [(c.name, c.role, c.affiliation_names) for c in pub.contributors] == [
            ("РОСТОВСКАЯ Т.К.", "Ответственный редактор", ["Институт демографических исследований ФНИСЦ РАН"]),
            ("РЯЗАНЦЕВ С.В.", "Рецензенты", ["Институт демографических исследований ФНИСЦ РАН"]),
            ("ЛАВРИКОВА Ю.Г.", "Рецензенты", ["Институт экономики УрО РАН"]),
        ]

    def test_container_and_conference(self, pub):
        c = pub.container
        assert (c.id, c.title, c.year) == (48151008, "TRANSPORTATION RESEARCH PROCEDIA", 2022)
        assert c.description == '12. Сер. "12th International Conference: Territory Development" Том 61. 2022'
        assert (c.publisher.name, c.publisher.publisher_id) == ("Elsevier B.V.", 21265)
        conf = pub.conference
        assert conf.name == "12TH INTERNATIONAL CONFERENCE ON TRANSPORT INFRASTRUCTURE: TITDS 2021"
        assert (conf.place, conf.dates, conf.year) == ("Irkutsk-Krasnoyarsk", "06–08 октября 2021 года", 2021)

    def test_abstracts_keywords_english(self, pub):
        assert pub.abstract == "A technique is proposed that allows you to analyze public transport routes.\nSecond paragraph."
        assert pub.keywords == ["BIG DATA", "ROUTE"]
        assert pub.title_en == "ANALYSIS OF PASSENGER TRAFFIC"
        assert pub.authors_en == ["KETOVA K."]
        assert pub.affiliations_en == ["Izhevsk State Technical University"]
        assert pub.abstract_en == "English abstract text."
        assert pub.keywords_en == ["ROUTE OPTIMIZATION", "PUBLIC TRANSPORT"]
        assert pub.references == ["Ivanov I. First reference. 2020.", "Second reference."]

    def test_external_fulltext(self, pub):
        ft = pub.fulltext
        assert (ft.access, ft.colour, ft.available) == ("external", "blue", False)
        assert [(x.host, x.colour, x.js) for x in ft.links] == [
            ("www.sciencedirect.com", "blue", "url_article(48182825, 1)")]


class TestCardErrors:
    def test_captcha(self):
        with pytest.raises(CaptchaRequired):
            parse_publication(fixture_bytes("captcha"), url="https://elibrary.ru/item.asp?id=41770713")

    def test_not_an_item_page(self):
        with pytest.raises(ParseError):
            parse_publication(fixture_bytes("query_results_all_types"))
        with pytest.raises(ParseError):
            parse_publication("<html><body><p>hello</p></body></html>")

    def test_not_found(self):
        html = "<html><body><table><tr><td>Публикация не найдена. Проверьте ссылку.</td></tr></table></body></html>"
        with pytest.raises(NotFound):
            parse_publication(html, url="https://elibrary.ru/item.asp?id=1")


@pytest.mark.parametrize("raw, flags, kind", [
    ("статья в журнале - научная статья", {}, "article"),
    ("статья в журнале - материалы конференции", {"journal": True}, "article"),
    ("обзорная статья", {"journal": True}, "article"),
    ("статья в сборнике трудов конференции", {}, "conference"),
    ("тезисы доклада на конференции", {}, "conference"),
    ("сборник трудов конференции", {}, "proceedings"),
    ("статья в сборнике статей", {}, "chapter"),
    ("глава в книге", {}, "chapter"),
    ("монография", {}, "book"),
    ("учебное пособие", {}, "book"),
    ("патент на изобретение", {}, "patent"),
    ("свидетельство о государственной регистрации программы для ЭВМ", {}, "patent"),
    ("автореферат диссертации", {}, "dissertation"),
    ("диссертация", {}, "dissertation"),
    ("отчет о НИР", {}, "report"),
    ("депонированная рукопись", {}, "preprint"),
    ("статья в открытом архиве", {}, "preprint"),
    ("грант", {}, "grant"),
    ("набор данных", {}, "dataset"),
    (None, {"journal": True}, "article"),
    (None, {"container": True}, "chapter"),
    (None, {}, "other"),
])
def test_normalize_kind(raw, flags, kind):
    assert normalize_kind(raw, **flags) == kind


# =========================================================================== ispdf.asp


class TestFulltextFlag:
    def test_fixture_green(self):
        f = parse_fulltext_flag(fixture_bytes("ispdf_47430099"))
        assert (f.item_id, f.available, f.colour, f.access) == (47430099, True, "green", "open")
        assert (f.title, f.action, f.price_rub) == ("Доступ к полному тексту открыт", "load_article", None)

    def test_empty_fragment(self):
        f = parse_fulltext_flag(b"", item_id=5)
        assert (f.item_id, f.available, f.colour, f.access) == (5, False, None, None)

    def test_yellow_with_price_and_blue(self):
        f = parse_fulltext_flag('<a href="javascript:load_article(80640587)"><img src="/images/download_yellow.png" '
                                'title="Можно приобрести полный текст этой публикации за 250 руб."></a>')
        assert (f.colour, f.access, f.price_rub, f.item_id) == ("yellow", "order", 250, 80640587)
        f = parse_fulltext_flag('<a href="javascript:url_article(80640587, 1)"><img src="/images/download_blue.png" '
                                'title="Полный текст доступен на внешнем сайте"></a>')
        assert (f.colour, f.access, f.action) == ("blue", "external", "url_article")

    def test_captcha(self):
        with pytest.raises(CaptchaRequired):
            parse_fulltext_flag(fixture_bytes("captcha"))


# =========================================================================== QA: "Версии:" blocks
def test_rows_keep_versions_out_of_the_source_line():
    """author_items rows of translated articles nest a "Версии:" table (title, authors and source of the
    English version, linked to its own card). It must not leak into source/pages/year of the row."""
    rows = {h.id: h for h in parse_publication_rows(parse_html(fixture_bytes("author_items_3771")))}
    atlas = rows[67961789]
    assert atlas.source == "Физика элементарных частиц и атомного ядра. 2023. Т. 54. № 6. С. 1241-1258."
    assert (atlas.versions, atlas.year, atlas.kind, atlas.citations) == ([64054831], 2023, "article", 7)
    ore = rows[83127498]
    assert ore.source == "Геология рудных месторождений. 2025. Т. 67. № 5. С. 614-640."
    assert ore.versions == [83114068]
    with_versions = [h for h in rows.values() if h.versions]
    assert len(with_versions) == 8
    assert not any("Версии" in (h.source or "") or "Petrov V.A." in (h.source or "") for h in rows.values())
    assert rows[91809593].versions == []  # rows without the block


def test_versions_do_not_change_exported_pages():
    from elibrary_cli import exports

    rows = {h.id: h for h in parse_publication_rows(parse_html(fixture_bytes("author_items_3771")))}
    ris = exports.export([rows[67961789]], "ris")
    assert "SP  - 1241\nEP  - 1258" in ris and "1152" not in ris
    assert "T2  - Физика элементарных частиц и атомного ядра" in ris


def test_italic_inside_title_is_not_the_author_list():
    """Row 81 of author_items_3771: the title has <I>IN SILICO</I>; the authors are the next <i>."""
    rows = {h.id: h for h in parse_publication_rows(parse_html(fixture_bytes("author_items_3771")))}
    crispr = rows[66475015]
    assert crispr.title.startswith("CRISPR-COPIES: AN IN SILICO PLATFORM FOR DISCOVERY")
    assert crispr.authors == ["Boob A.G.", "Zhu Zh.", "Intasian P.", "Jain M.", "Petrov V.A.", "Lane S.T.",
                              "Tan Sh.I.", "Xun G.", "Zhao H."]
    assert crispr.source == "Nucleic Acids Research. 2024. Т. 52. № 6. С. e30."
    assert (crispr.year, crispr.kind) == (2024, "article")
    # every row of the author list whose markup has an author <i> gets its authors
    assert [h.id for h in rows.values() if not h.authors] == [59950405]  # a collection without authors
