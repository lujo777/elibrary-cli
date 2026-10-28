"""Citation export (CSL-JSON, BibTeX, RIS, Zotero JSON) from the saved elibrary.ru pages."""

from __future__ import annotations

import json
import re
from datetime import date
from functools import cache

import pytest

from elibrary_cli import exports as ex
from elibrary_cli.models.base import PublicationHit
from elibrary_cli.models.publications import (
    AuthorRef,
    BookInfo,
    ConferenceInfo,
    ContainerRef,
    Dates,
    Metrics,
    OrgRef,
    Publication,
)
from elibrary_cli.parsers.common import parse_html
from elibrary_cli.parsers.item import parse_publication
from elibrary_cli.parsers.rows import parse_publication_rows
from tests.conftest import fixture_bytes


@cache
def card(name: str) -> Publication:
    return parse_publication(fixture_bytes(name))


@cache
def rows(name: str) -> dict[int, PublicationHit]:
    return {h.id: h for h in parse_publication_rows(parse_html(fixture_bytes(name)))}


def hit(name: str, item_id: int) -> PublicationHit:
    return rows(name)[item_id]


ARTICLE = "item_article_46371463"
BOOK = "item_book_76097861"
CHAPTER = "item_chapter_76098153"
DISSER = "item_disser_30339972"
PATENT = "item_patent_47430099"
CARDS = (ARTICLE, BOOK, CHAPTER, DISSER, PATENT)
ALL_TYPES = "query_results_all_types"


def creators(item: dict) -> list[tuple[str, str, str]]:
    return [(c["creatorType"], c.get("lastName", c.get("name", "")), c.get("firstName", "")) for c in item["creators"]]


# --------------------------------------------------------------------------- helpers


class TestFormats:
    def test_constants(self):
        assert ex.FORMATS == ("bibtex", "ris", "csl", "zotero")
        assert set(ex.FILE_EXTENSIONS) == set(ex.FORMATS) == set(ex.MEDIA_TYPES)
        assert ex.FILE_EXTENSIONS["bibtex"] == ".bib" and ex.FILE_EXTENSIONS["ris"] == ".ris"

    @pytest.mark.parametrize(("alias", "canon"), [
        ("bib", "bibtex"), ("BibLaTeX", "bibtex"), ("csl-json", "csl"), ("CSL_JSON", "csl"), ("json", "csl"),
        ("zotero-json", "zotero"), (" ris ", "ris")])
    def test_aliases(self, alias, canon):
        assert ex.normalize_format(alias) == canon

    def test_unknown_format(self):
        with pytest.raises(ValueError, match="Неизвестный формат экспорта 'docx'"):
            ex.normalize_format("docx")
        with pytest.raises(ValueError):
            ex.export(card(ARTICLE), "endnote")

    def test_wrong_input_type(self):
        with pytest.raises(TypeError, match="Ожидалась публикация"):
            ex.to_csl_json({"id": 1})  # type: ignore[arg-type]


class TestNames:
    @pytest.mark.parametrize(("display", "full", "expected"), [
        ("БЛОХИНА Т.М.", "Блохина (Трубченкова) Таисия Михайловна",
         {"lastName": "Блохина", "firstName": "Таисия Михайловна"}),
        ("ТРУБЧЕНКОВА Т.М.", "Блохина (Трубченкова) Таисия Михайловна",  # maiden name as displayed
         {"lastName": "Трубченкова", "firstName": "Таисия Михайловна"}),
        ("ЗОЗУЛЯ Ю.И.", None, {"lastName": "Зозуля", "firstName": "Ю. И."}),
        ("Суханова Н.В.", None, {"lastName": "Суханова", "firstName": "Н. В."}),
        ("Аль-Мешхедани А.Ф.С.", None, {"lastName": "Аль-Мешхедани", "firstName": "А. Ф. С."}),
        ("АЛЬ-МЕШХЕДАНИ А.Ф.", None, {"lastName": "Аль-Мешхедани", "firstName": "А. Ф."}),
        ("Petrov V.A.", None, {"lastName": "Petrov", "firstName": "V. A."}),
        ("Панова Е.Н", None, {"lastName": "Панова", "firstName": "Е. Н."}),
        # tooltip of another person / another script: keep what is displayed
        ("VAVILOVA D.", "Вавилова (Насридинова) Дайана Дамировна", {"lastName": "Vavilova", "firstName": "D."}),
        ("ИВАНОВ А.Б.", "Иванов Пётр Сергеевич", {"lastName": "Иванов", "firstName": "А. Б."}),
        ("Иванов Иван Иванович", None, {"lastName": "Иванов", "firstName": "Иван Иванович"}),
        ("", "Мифтахова Рамиля Габдулхаевна", {"lastName": "Мифтахова", "firstName": "Рамиля Габдулхаевна"}),
        ("ИПМ РАН", None, {"name": "ИПМ РАН"}),
        ("Институт проблем управления РАН", None, {"name": "Институт проблем управления РАН"}),
    ])
    def test_split_person_name(self, display, full, expected):
        assert ex.split_person_name(display, full) == expected


class TestCaseAndTranslit:
    @pytest.mark.parametrize(("text", "expected"), [
        ("ОТКАЗОУСТОЙЧИВАЯ НЕЙРОННАЯ СЕТЬ, СПОСОБ ОБНАРУЖЕНИЯ ОТКАЗОВ",
         "Отказоустойчивая нейронная сеть, способ обнаружения отказов"),
        ("НЕЙРОННЫЕ СЕТИ. РЕАЛИЗАЦИЯ НЕЙРОННЫХ СЕТЕЙ В MATLAB", "Нейронные сети. Реализация нейронных сетей в MATLAB"),
        ("ДЛИТЕЛЬНОЕ ПОДДЕРЖАНИЕ γН2АХ-ПОЗИТИВНЫХ ЛИМФОЦИТОВ", "Длительное поддержание γН2АХ-позитивных лимфоцитов"),
        ("МОНИТОРИНГ СЕТИ ЛЭП С ИСПОЛЬЗОВАНИЕМ БПЛА", "Мониторинг сети ЛЭП с использованием БПЛА"),
        ("СЧЕТЧИКОВ ЭЛ. ЭНЕРГИИ", "Счетчиков эл. энергии"),
        ("3.НЕЙРОННЫЕ СЕТИ", "3.Нейронные сети"),
        ("XV МЕЖДУНАРОДНАЯ КОНФЕРЕНЦИЯ", "XV международная конференция"),
        ("FAULT-TOLERANT NEURAL NETWORK", "Fault-tolerant neural network"),
        ("Уже нормальный Регистр", "Уже нормальный Регистр"),
        ("ДНК", "ДНК"),
    ])
    def test_sentence(self, text, expected):
        assert ex.normalize_caps(text) == expected

    def test_title_and_lower_modes(self):
        assert ex.normalize_caps("TRANSPORTATION RESEARCH PROCEDIA", mode="title") == "Transportation Research Procedia"
        assert (ex.normalize_caps("12TH INTERNATIONAL CONFERENCE ON TRANSPORT: TITDS 2021", mode="title")
                == "12th International Conference on Transport: Titds 2021")
        assert (ex.normalize_caps("БЮЛЛЕТЕНЬ ЭКСПЕРИМЕНТАЛЬНОЙ БИОЛОГИИ И МЕДИЦИНЫ", mode="title")
                == "Бюллетень экспериментальной биологии и медицины")
        assert [ex.normalize_caps(k, mode="lower") for k in ("ИОНИЗИРУЮЩЕЕ ИЗЛУЧЕНИЕ", "ВЕС", "ДНК", "γН2АХ")] == [
            "ионизирующее излучение", "вес", "ДНК", "γН2АХ"]

    def test_transliterate(self):
        assert ex.transliterate("Мифтахова") == "Miftakhova"
        assert ex.transliterate("Щукин Юрий Ёлкин") == "Shchukin Yuriy Elkin"
        assert ex.transliterate("Объёмный") == "Obemnyy"
        assert ex.transliterate("Їжак Ґанок café") == "Yizhak Ganok cafe"


class TestCitekeys:
    @pytest.mark.parametrize(("name", "key"), [
        (ARTICLE, "blokhina2021dlitelnoe"),
        (BOOK, "miftakhova2024obrabotka"),
        (CHAPTER, "miftakhova2024neyronnye"),  # no own authors: the book's author
        (DISSER, "zozulya2009strukturno"),
        (PATENT, "sukhanova2021otkazoustoychivaya"),
    ])
    def test_card_keys(self, name, key):
        assert ex.citekey(card(name)) == key

    def test_with_edn(self):
        assert ex.citekey(card(ARTICLE), with_edn=True) == "blokhina2021dlitelnoe-fyoakl"

    def test_hits(self):
        assert ex.citekey(hit(ALL_TYPES, 41770713)) == "zelinov2019izuchenie"
        # no authors: first title word + year + second word
        assert ex.citekey(hit("query_results_patents", 88918557)) == "vstroennoe2026programmnoe"
        # no EDN in a row: the eLIBRARY id is used
        assert ex.citekey(hit(ALL_TYPES, 41770713), with_edn=True) == "zelinov2019izuchenie-41770713"

    def test_fallback(self):
        assert ex.citekey(PublicationHit(id=5, title="123")) == "elibrary5"


# --------------------------------------------------------------------------- Zotero JSON


class TestZoteroCards:
    def test_article(self):
        z = ex.to_zotero_item(card(ARTICLE), tags=["eLIBRARY", "лимфоциты"], collections=["ABCD2345"])
        assert z["itemType"] == "journalArticle"
        assert z["title"].startswith("Длительное поддержание повышенного количества γН2АХ-позитивных лимфоцитов")
        assert creators(z) == [
            ("author", "Блохина", "Таисия Михайловна"), ("author", "Яшкина", "Елизавета Игоревна"),
            ("author", "Беляева", "Александра Григорьевна"), ("author", "Перевезенцев", "Александр Александрович"),
            ("author", "Штемберг", "Андрей Сергеевич"), ("author", "Осипов", "Андреян Николаевич")]
        assert (z["publicationTitle"], z["volume"], z["issue"], z["pages"], z["date"]) == (
            "Бюллетень экспериментальной биологии и медицины", "172", "7", "98-101", "2021")
        assert (z["ISSN"], z["DOI"], z["language"]) == ("0365-9615", "10.47056/0365-9615-2021-172-7-98-101", "ru")
        assert z["url"] == "https://elibrary.ru/item.asp?id=46371463"
        assert z["libraryCatalog"] == "eLibrary.ru"
        assert z["extra"].split("\n") == ["eLIBRARY ID: 46371463", "EDN: FYOAKL", "Цитирований в РИНЦ: 1"]
        assert [t["tag"] for t in z["tags"]] == [
            "γН2АХ", "лимфоциты", "обезьяны", "ионизирующее излучение", "гипогравитация", "eLIBRARY"]
        assert z["collections"] == ["ABCD2345"] and z["relations"] == {}

    def test_patent(self):
        z = ex.to_zotero_item(card(PATENT))
        assert z["itemType"] == "patent"
        assert z["title"] == ("Отказоустойчивая нейронная сеть, способ обнаружения отказов нейронной сети, "
                              "способ обеспечения отказоустойчивости нейронной сети")
        assert creators(z) == [("inventor", "Суханова", "Наталия Вячеславовна"),
                               ("inventor", "Шептунов", "Сергей Александрович")]
        assert (z["patentNumber"], z["applicationNumber"], z["filingDate"], z["issueDate"]) == (
            "RU 2760636 C2", "2018145179", "2018-12-19", "2021-11-29")
        assert (z["country"], z["issuingAuthority"]) == ("RU", "Роспатент")
        assert z["assignee"].startswith("Федеральное государственное автономное учреждение науки Институт "
                                        "конструкторско-технологической информатики")
        assert "date" not in z and "libraryCatalog" not in z  # not valid for patents (Web API -> 400)
        extra = z["extra"].split("\n")
        assert extra[:4] == ["eLIBRARY ID: 47430099", "EDN: HMNCEQ", "Цитирований в РИНЦ: 1",
                             "МПК: G06F 11/20; G06N 3/02"]
        assert "Название на английском: Fault-tolerant neural network, method for detecting neural network faults, " \
               "method for ensuring fault tolerance of neural network" in extra
        assert "genre: патент на изобретение" in extra

    def test_book(self):
        z = ex.to_zotero_item(card(BOOK))
        assert z["itemType"] == "book"
        assert creators(z) == [("author", "Мифтахова", "Рамиля Габдулхаевна"),
                               ("editor", "Морозкина", "Евгения Александровна")]
        assert (z["publisher"], z["place"], z["date"], z["numPages"], z["ISBN"]) == (
            "Уфимский университет науки и технологий", "Уфа", "2024", "156", "978-5-7477-5904-6")
        assert z["extra"].split("\n") == ["eLIBRARY ID: 76097861", "EDN: GVVMFP", "Цитирований в РИНЦ: 1", "УДК: 025.4"]
        assert [t["tag"] for t in z["tags"]][:3] == ["нейронные сети", "естественные языки", "обработка текстовых данных"]

    def test_chapter(self):
        z = ex.to_zotero_item(card(CHAPTER))
        assert z["itemType"] == "bookSection"
        assert z["title"] == "3.Нейронные сети и нейронная модель языка"
        assert creators(z) == [("author", "Мифтахова", "Р. Г."), ("bookAuthor", "Мифтахова", "Р. Г.")]
        assert (z["bookTitle"], z["publisher"], z["place"], z["pages"], z["date"]) == (
            "Обработка естественного языка с применением программных средств",
            "Уфимский университет науки и технологий", "Уфа", "108", "2024")
        assert z["tags"] == []

    def test_thesis(self):
        z = ex.to_zotero_item(card(DISSER))
        assert z["itemType"] == "thesis"
        assert creators(z) == [("author", "Зозуля", "Ю. И.")]
        assert z["thesisType"] == "автореферат диссертации на соискание ученой степени доктора технических наук"
        assert (z["university"], z["place"], z["date"], z["numPages"]) == ("Уфим. гос. авиац.-техн. ун-т", "Уфа",
                                                                           "2009", "32")
        assert "Специальность: 05.13.01 - Системный анализ, управление и обработка информации (по отраслям)" in z["extra"]
        assert "publisher" not in z

    @pytest.mark.parametrize("name", CARDS)
    def test_only_valid_fields(self, name):
        z = ex.to_zotero_item(card(name), access_date=date(2026, 10, 13))
        allowed = set(ex._ZOTERO_FIELDS[z["itemType"]]) | {"itemType", "creators", "tags", "collections", "relations"}
        assert set(z) <= allowed
        ok_creators = set(ex._ZOTERO_CREATORS[z["itemType"]])
        assert {c["creatorType"] for c in z["creators"]} <= ok_creators
        assert z["accessDate"] == "2026-10-13"
        assert all(v != "" for v in z.values())

    def test_item_types(self):
        assert [ex.zotero_item_type(card(n)) for n in CARDS] == [
            "journalArticle", "book", "bookSection", "thesis", "patent"]

    def test_no_keyword_tags_and_no_caps_fix(self):
        z = ex.to_zotero_item(card(PATENT), keyword_tags=False, fix_caps=False)
        assert z["title"].startswith("ОТКАЗОУСТОЙЧИВАЯ НЕЙРОННАЯ СЕТЬ")
        z = ex.to_zotero_item(card(ARTICLE), keyword_tags=False)
        assert z["tags"] == []


class TestZoteroHits:
    def test_patent_row(self):
        z = ex.to_zotero_item(hit(ALL_TYPES, 47430099))
        assert z["itemType"] == "patent"
        assert creators(z) == [("inventor", "Суханова", "Н. В."), ("inventor", "Шептунов", "С. А.")]
        assert (z["patentNumber"], z["issueDate"], z["applicationNumber"], z["filingDate"]) == (
            "RU 2760636 C2", "2021-11-29", "2018145179", "2018-12-19")
        assert (z["country"], z["issuingAuthority"]) == ("RU", "Роспатент")
        assert z["extra"].split("\n") == ["eLIBRARY ID: 47430099", "Цитирований в РИНЦ: 1",
                                          "genre: Патент на изобретение"]

    def test_software_certificate_row(self):
        z = ex.to_zotero_item(hit("query_results_patents", 88918718))
        assert z["itemType"] == "patent"
        assert z["title"].startswith("Программа многокритериальной оптимизации маршрута мониторинга сети ЛЭП "
                                     "с использованием БПЛА")
        assert (z["patentNumber"], z["issueDate"], z["applicationNumber"]) == ("RU 2026610196", "2026-01-13",
                                                                               "2025697041")
        assert "genre: Свидетельство о регистрации программы для ЭВМ" in z["extra"]

    def test_conference_row(self):
        z = ex.to_zotero_item(hit(ALL_TYPES, 41770713))
        assert z["itemType"] == "conferencePaper"
        assert z["proceedingsTitle"] == ("Гуманитарные науки в современном вузе: вчера, сегодня, завтра: "
                                         "материалы международной научной конференции")
        assert (z["pages"], z["date"]) == ("880-885", "2019")  # not the initials "С. И." of the editors
        assert creators(z) == [("author", "Зелинов", "М. А.")]
        z = ex.to_zotero_item(rows("author_items_3771")[next(
            i for i, h in rows("author_items_3771").items() if (h.source or "").startswith("В сборнике: Рудная"))])
        assert z["proceedingsTitle"] == ("Рудная школа ЦНИГРИ 2026: сборник тезисов докладов молодежной "
                                         "научно-образовательной конференции")
        assert z["place"] == "Москва"

    def test_conference_row_in_caps(self):
        z = ex.to_zotero_item(hit(ALL_TYPES, 32774667))
        assert z["proceedingsTitle"] == ("Шаг в будущее: искусственный интеллект и цифровая экономика: "
                                         "материалы 1-й Международной научно-практической конференции")
        assert z["pages"] == "130-135"

    def test_article_rows(self):
        z = ex.to_zotero_item(hit(ALL_TYPES, 35198182))
        assert (z["itemType"], z["publicationTitle"], z["volume"], z["issue"], z["pages"], z["date"]) == (
            "journalArticle", "Математические методы распознавания образов", "11", "1", "112-116", "2003")
        assert z["title"] == "Нейронные сети максимальной устойчивости как альтернатива робастным нейронным сетям"
        z = ex.to_zotero_item(hit(ALL_TYPES, 65662365))
        assert (z["publicationTitle"], z["issue"], z["pages"]) == ("Тенденции развития науки и образования",
                                                                   "106-11", "119-125")
        assert "volume" not in z
        assert z["extra"].split("\n") == ["eLIBRARY ID: 65662365", "Цитирований в РИНЦ: 12"]

    def test_english_article_row(self):
        z = ex.to_zotero_item(hit("author_items_3771", 92348100))
        assert (z["title"], z["language"]) == ("On the “flat odderon” model", "en")
        assert (z["publicationTitle"], z["volume"], z["pages"]) == ("Nuclear Physics A", "1073", "123436")
        assert creators(z) == [("author", "Petrov", "V. A."), ("author", "Tkachenko", "N. P.")]

    def test_chapter_rows(self):
        z = ex.to_zotero_item(hit(ALL_TYPES, 42637959))
        assert z["itemType"] == "bookSection"
        assert z["bookTitle"] == "Современные проблемы прикладной информатики: сборник научных трудов"
        assert creators(z) == [("author", "Голоскоков", "К. П."), ("editor", "Брусакова", "И. А."),
                               ("editor", "Панова", "Е. Н.")]
        z = ex.to_zotero_item(hit(ALL_TYPES, 76098153))
        assert z["bookTitle"] == "Обработка естественного языка с применением программных средств"
        assert creators(z) == [("author", "Мифтахова", "Р. Г."), ("bookAuthor", "Мифтахова", "Р. Г.")]
        assert (z["place"], z["pages"], z["title"]) == ("Уфа", "108", "3.Нейронные сети и нейронная модель языка")
        z = ex.to_zotero_item(hit(ALL_TYPES, 45586700))
        assert (z["place"], z["pages"]) == ("Донецк", "23-26")

    def test_book_row(self):
        z = ex.to_zotero_item(hit(ALL_TYPES, 61283635))
        assert (z["itemType"], z["place"], z["date"]) == ("book", "Липецк", "2023")
        assert z["title"] == "Нейронные сети и искусственный интеллект: основы нейронных сетей на языке PYTHON"
        z = ex.to_zotero_item(hit("author_items_3771", 59950405))
        assert z["place"] == "Москва" and z["creators"] == []

    def test_grant_row(self):
        z = ex.to_zotero_item(hit(ALL_TYPES, 50541112))
        assert (z["itemType"], z["reportType"], z["reportNumber"], z["institution"], z["date"]) == (
            "report", "НИР: грант", "95-01-01081", "Российский фонд фундаментальных исследований", "1995")
        assert ("Описание (eLIBRARY): НИР: грант № 95-01-01081. Российский фонд фундаментальных исследований. 1995."
                in z["extra"])

    def test_dissertation_row(self):
        z = ex.to_zotero_item(hit(ALL_TYPES, 30339972))
        assert (z["itemType"], z["thesisType"], z["university"], z["place"], z["date"]) == (
            "thesis", "автореферат диссертации на соискание ученой степени доктора технических наук",
            "Уфим. гос. авиац.-техн. ун-т", "Уфа", "2009")

    def test_row_without_source(self):
        h = rows("contents_8610")[91982812]
        z = ex.to_zotero_item(h)
        assert z["itemType"] == "document"  # kind unknown without a source line
        assert "pages" not in z  # no pages field on 'document': kept as a CSL variable in extra
        assert "page: 7-19" in z["extra"].split("\n")
        z = ex.to_zotero_item(h.model_copy(update={"kind": "article"}))
        assert (z["itemType"], z["pages"], z["title"]) == (
            "journalArticle", "7-19", "Корреляционно-регрессионный анализ временных рядов потребления природного газа")


# --------------------------------------------------------------------------- synthetic cards (types without fixtures)


def make_pub(**kw) -> Publication:
    base = {"id": 1000001, "title": "Тестовая публикация", "edn": "ABCDEF", "language": "русский",
            "language_code": "ru", "year": 2020,
            "authors": [AuthorRef(name="ИВАНОВ И.И.", full_name="Иванов Иван Иванович")],
            "metrics": Metrics(rinc_citations=3)}
    base.update(kw)
    return Publication(**base)


class TestOtherTypes:
    def test_conference_card(self):
        p = make_pub(kind="conference", type_raw="статья в сборнике трудов конференции", pages="29-35",
                     container=ContainerRef(id=48151008, title="TRANSPORTATION RESEARCH PROCEDIA",
                                            description='12. Сер. "12th International Conference" Том 61. 2022',
                                            publisher=OrgRef(name="Elsevier B.V.")),
                     conference=ConferenceInfo(name="12TH INTERNATIONAL CONFERENCE ON TRANSPORT", place="Irkutsk",
                                               dates="06–08 октября 2021 года"),
                     abstract="Первый абзац.\nВторой абзац.", doi="10.1016/j.trpro.2022.01.006")
        z = ex.to_zotero_item(p)
        assert z["itemType"] == "conferencePaper"
        assert (z["proceedingsTitle"], z["conferenceName"], z["eventPlace"]) == (
            "Transportation Research Procedia", "12th International Conference on Transport", "Irkutsk")
        assert (z["volume"], z["series"], z["publisher"], z["pages"]) == ("61", "12th International Conference",
                                                                          "Elsevier B.V.", "29-35")
        assert z["abstractNote"] == "Первый абзац.\nВторой абзац."
        assert "Даты конференции: 06–08 октября 2021 года" in z["extra"]
        csl = ex.to_csl_json(p)
        assert (csl["type"], csl["event-title"], csl["event-place"], csl["container-title"]) == (
            "paper-conference", "12th International Conference on Transport", "Irkutsk",
            "Transportation Research Procedia")
        assert csl["abstract"] == "Первый абзац.\nВторой абзац."
        bib = ex.to_bibtex(p)
        assert bib.startswith("@inproceedings{ivanov2020testovaya,")
        assert "  abstract   = {Первый абзац. Второй абзац.}," in bib
        assert "  eventtitle = {12th International Conference on Transport}," in bib
        ris = ex.to_ris(p)
        assert "TY  - CPAPER" in ris
        # Zotero's RIS.js: T2 conferenceName, C3 proceedingsTitle, T3 series, CY (excluded) the event place
        for line in ("T2  - 12th International Conference on Transport", "C3  - Transportation Research Procedia",
                     "T3  - 12th International Conference", "CY  - Irkutsk", "PB  - Elsevier B.V."):
            assert line in ris.splitlines()
        assert "AB  - Первый абзац. Второй абзац." in ris

    def test_proceedings_volume(self):
        p = make_pub(kind="proceedings", conference=ConferenceInfo(name="Конференция X"))
        z = ex.to_zotero_item(p)
        assert z["itemType"] == "book"
        assert "event-title: Конференция X" in z["extra"].split("\n")  # book has no conferenceName field
        assert ex.to_bibtex(p).startswith("@proceedings{")
        assert ex.to_ris(p).startswith("TY  - BOOK")  # Zotero imports TY CONF as a conferencePaper

    def test_deposited_manuscript(self):
        p = make_pub(kind="preprint", type_raw="депонированная рукопись", dates=Dates(deposited=date(2020, 5, 6)),
                     extra={"Место депонирования": "ВИНИТИ", "Номер депонирования": "123-В2020"})
        z = ex.to_zotero_item(p)
        assert (z["itemType"], z["manuscriptType"], z["institution"], z["number"], z["date"]) == (
            "manuscript", "депонированная рукопись", "ВИНИТИ", "123-В2020", "2020-05-06")
        assert ex.to_csl_json(p)["type"] == "manuscript"
        assert ex.to_bibtex(p).startswith("@unpublished{")
        assert ex.to_ris(p).startswith("TY  - MANSCPT")

    def test_preprint(self):
        p = make_pub(kind="preprint", type_raw="препринт", extra={"Репозиторий": "arXiv"})
        z = ex.to_zotero_item(p)
        assert (z["itemType"], z["genre"], z["repository"]) == ("preprint", "препринт", "arXiv")
        assert ex.to_csl_json(p)["type"] == "article"
        assert ex.to_ris(p).startswith("TY  - UNPB")

    def test_report_grant_dataset_other(self):
        rep = make_pub(kind="report", type_raw="отчет о НИР", extra={"Номер госрегистрации": "АААА-А19-1"},
                       affiliations=[])
        z = ex.to_zotero_item(rep)
        assert (z["itemType"], z["reportType"], z["reportNumber"]) == ("report", "отчет о НИР", "АААА-А19-1")
        assert ex.to_bibtex(rep).startswith("@techreport{")
        grant = make_pub(kind="grant", type_raw="грант", extra={"Фонд": "РНФ", "Номер": "19-11-00001"})
        z = ex.to_zotero_item(grant)
        assert (z["itemType"], z["institution"], z["reportNumber"]) == ("report", "РНФ", "19-11-00001")
        data = make_pub(kind="dataset", type_raw="набор данных", book=BookInfo(publisher=OrgRef(name="Zenodo")))
        z = ex.to_zotero_item(data)
        assert (z["itemType"], z["repository"], z["type"]) == ("dataset", "Zenodo", "набор данных")
        assert ex.to_ris(data).startswith("TY  - DATA") and ex.to_bibtex(data).startswith("@dataset{")
        other = make_pub(kind=None)
        z = ex.to_zotero_item(other)
        assert z["itemType"] == "document"
        assert ex.to_csl_json(other)["type"] == "document"
        assert ex.to_ris(other).startswith("TY  - GEN") and ex.to_bibtex(other).startswith("@misc{")

    def test_translators_and_reviewers(self):
        p = make_pub(kind="book", contributors=[AuthorRef(name="ПЕТРОВ П.П.", role="Перевод с английского"),
                                                AuthorRef(name="СИДОРОВ С.С.", role="Рецензенты")])
        z = ex.to_zotero_item(p)
        assert creators(z) == [("author", "Иванов", "Иван Иванович"), ("translator", "Петров", "П. П."),
                               ("contributor", "Сидоров", "С. С.")]
        assert ex.to_csl_json(p)["translator"] == [{"family": "Петров", "given": "П. П."}]


# --------------------------------------------------------------------------- CSL-JSON


class TestCsl:
    def test_article(self):
        c = ex.to_csl_json(card(ARTICLE))
        assert (c["id"], c["type"]) == ("blokhina2021dlitelnoe", "article-journal")
        assert c["author"][0] == {"family": "Блохина", "given": "Таисия Михайловна"}
        assert c["issued"] == {"date-parts": [[2021]]}
        assert (c["container-title"], c["volume"], c["issue"], c["page"]) == (
            "Бюллетень экспериментальной биологии и медицины", "172", "7", "98-101")
        assert (c["DOI"], c["URL"], c["ISSN"], c["language"]) == (
            "10.47056/0365-9615-2021-172-7-98-101", "https://elibrary.ru/item.asp?id=46371463", "0365-9615", "ru")
        assert c["keyword"] == "γН2АХ, лимфоциты, обезьяны, ионизирующее излучение, гипогравитация"
        assert c["note"] == "eLIBRARY ID: 46371463\nEDN: FYOAKL\nЦитирований в РИНЦ: 1"
        assert c["source"] == "eLibrary.ru"

    def test_patent(self):
        c = ex.to_csl_json(card(PATENT))
        assert (c["type"], c["number"], c["authority"], c["genre"]) == (
            "patent", "RU 2760636 C2", "Роспатент", "патент на изобретение")
        assert c["issued"] == {"date-parts": [[2021, 11, 29]]}
        assert c["submitted"] == {"date-parts": [[2018, 12, 19]]}
        assert "Номер заявки: 2018145179" in c["note"] and "Страна: RU" in c["note"]
        assert "Патентообладатель: Федеральное государственное автономное учреждение науки" in c["note"]

    def test_thesis_chapter_book(self):
        t = ex.to_csl_json(card(DISSER))
        assert (t["type"], t["genre"], t["publisher"], t["publisher-place"], t["number-of-pages"]) == (
            "thesis", "автореферат диссертации на соискание ученой степени доктора технических наук",
            "Уфим. гос. авиац.-техн. ун-т", "Уфа", "32")
        ch = ex.to_csl_json(card(CHAPTER))
        assert ch["type"] == "chapter"
        assert ch["container-author"] == [{"family": "Мифтахова", "given": "Р. Г."}]
        assert ch["container-title"] == "Обработка естественного языка с применением программных средств"
        b = ex.to_csl_json(card(BOOK))
        assert (b["type"], b["ISBN"], b["editor"]) == ("book", "978-5-7477-5904-6",
                                                       [{"family": "Морозкина", "given": "Евгения Александровна"}])

    def test_zotero_mcp_mode(self):
        c = ex.to_csl_json(card(PATENT), zotero_mcp=True)
        assert set(c) <= ex._ZOTERO_MCP_CSL_KEYS
        lines = c["note"].split("\n")
        assert "submitted: 2018-12-19" in lines and "authority: Роспатент" in lines
        ch = ex.to_csl_json(card(CHAPTER), zotero_mcp=True)
        assert "container-author: Мифтахова || Р. Г." in ch["note"].split("\n")
        assert "source" not in ch

    def test_hit(self):
        c = ex.to_csl_json(hit(ALL_TYPES, 41770713), citekey="custom")
        assert (c["id"], c["type"], c["page"]) == ("custom", "paper-conference", "880-885")
        assert c["note"] == "eLIBRARY ID: 41770713\nЦитирований в РИНЦ: 23"


# --------------------------------------------------------------------------- BibTeX


class TestBibtex:
    def test_article(self):
        b = ex.to_bibtex(card(ARTICLE))
        lines = b.splitlines()
        assert lines[0] == "@article{blokhina2021dlitelnoe,"
        assert lines[-1] == "}"
        assert ("  author   = {Блохина, Таисия Михайловна and Яшкина, Елизавета Игоревна and Беляева, "
                "Александра Григорьевна and Перевезенцев, Александр Александрович and Штемберг, Андрей Сергеевич "
                "and Осипов, Андреян Николаевич},") in lines
        assert "  journal  = {Бюллетень экспериментальной биологии и медицины}," in lines
        assert "  pages    = {98--101}," in lines
        assert "  number   = {7}," in lines and "  volume   = {172}," in lines
        assert "  doi      = {10.47056/0365-9615-2021-172-7-98-101}," in lines
        assert "  langid   = {russian}," in lines
        assert "  edn      = {FYOAKL}" in lines  # last field, no trailing comma
        assert "{γН2АХ-позитивных}" in b  # words with several capitals are protected

    def test_entry_types(self):
        heads = [ex.to_bibtex(card(n)).split("{", 1)[0] for n in CARDS]
        assert heads == ["@article", "@book", "@incollection", "@thesis", "@patent"]

    def test_patent_and_thesis_fields(self):
        b = ex.to_bibtex(card(PATENT))
        for line in ("  number            = {RU 2760636 C2},", "  type              = {патент на изобретение},",
                     "  date              = {2021-11-29},", "  applicationnumber = {2018145179},",
                     "  filingdate        = {2018-12-19},", "  location          = {RU},",
                     "  ipc               = {G06F 11/20; G06N 3/02},"):
            assert line in b.splitlines()
        assert "holder            = {{Федеральное государственное автономное учреждение науки" in b
        t = ex.to_bibtex(card(DISSER))
        assert "  school    = {Уфим. гос. авиац.-техн. ун-т}," in t.splitlines()
        assert "  type      = {автореферат диссертации на соискание ученой степени доктора технических наук}," in t
        assert "  bookauthor = {Мифтахова, Р. Г.}," in ex.to_bibtex(card(CHAPTER)).splitlines()

    def test_escaping(self):
        p = make_pub(title="Rock & Roll: 100% за $5 #1 a_b {x} ~^ \\ конец", kind="article")
        b = ex.to_bibtex(p)
        assert (r"Rock \& Roll: 100\% за \$5 \#1 a\_b \textbraceleft{}x\textbraceright{} "
                r"\textasciitilde{}\textasciicircum{} \textbackslash{} конец") in b
        assert ex.bibtex_escape("Иванов & Co_1") == r"Иванов \& Co\_1"

    def test_organisation_author(self):
        p = make_pub(authors=[AuthorRef(name="ИПМ РАН")], kind="report")
        b = ex.to_bibtex(p)
        assert b.startswith("@techreport{ipmran2020testovaya,\n  author = {{ИПМ РАН}},\n")
        assert ex.to_zotero_item(p)["creators"] == [{"creatorType": "author", "name": "ИПМ РАН"}]
        assert ex.to_csl_json(p)["author"] == [{"literal": "ИПМ РАН"}]

    def test_key_override_and_edn_key(self):
        assert ex.to_bibtex(card(BOOK), citekey="mykey").startswith("@book{mykey,")
        assert ex.to_bibtex(card(BOOK), with_edn_key=True).startswith("@book{miftakhova2024obrabotka-gvvmfp,")


# --------------------------------------------------------------------------- RIS


class TestRis:
    def test_article(self):
        r = ex.to_ris(card(ARTICLE))
        lines = r.splitlines()
        assert lines[0] == "TY  - JOUR" and lines[-1] == "ER  - "
        assert lines[1:7] == ["AU  - Блохина, Таисия Михайловна", "AU  - Яшкина, Елизавета Игоревна",
                              "AU  - Беляева, Александра Григорьевна", "AU  - Перевезенцев, Александр Александрович",
                              "AU  - Штемберг, Андрей Сергеевич", "AU  - Осипов, Андреян Николаевич"]
        for line in ("T2  - Бюллетень экспериментальной биологии и медицины", "PY  - 2021", "VL  - 172", "IS  - 7",
                     "SP  - 98", "EP  - 101", "SN  - 0365-9615", "DO  - 10.47056/0365-9615-2021-172-7-98-101",
                     "UR  - https://elibrary.ru/item.asp?id=46371463", "DB  - eLIBRARY.RU", "AN  - 46371463",
                     "N1  - eLIBRARY ID: 46371463; EDN: FYOAKL; Цитирований в РИНЦ: 1", "KW  - гипогравитация"):
            assert line in lines

    def test_patent(self):
        lines = ex.to_ris(card(PATENT)).splitlines()
        assert lines[0] == "TY  - PAT"
        for line in ("SN  - RU 2760636 C2", "M1  - 2018145179", "C1  - 2018/12/19", "DA  - 2021/11/29",
                     "C3  - RU", "A2  - Роспатент", "M3  - патент на изобретение"):
            assert line in lines
        assert any(x.startswith("PB  - Федеральное государственное автономное учреждение науки") for x in lines)

    def test_types(self):
        assert [ex.to_ris(card(n)).split("\n", 1)[0] for n in CARDS] == [
            "TY  - JOUR", "TY  - BOOK", "TY  - CHAP", "TY  - THES", "TY  - PAT"]
        lines = ex.to_ris(card(CHAPTER)).splitlines()
        assert "T2  - Обработка естественного языка с применением программных средств" in lines
        assert "C2  - Мифтахова, Р. Г." in lines and "SP  - 108" in lines  # C2 = bookAuthor (A3 = seriesEditor)
        assert not any(x.startswith("A3  - ") for x in lines)
        lines = ex.to_ris(card(DISSER)).splitlines()
        assert "PB  - Уфим. гос. авиац.-техн. ун-т" in lines and "SP  - 32" in lines and "CY  - Уфа" in lines


# --------------------------------------------------------------------------- batches


class TestExport:
    def test_bibtex_batch_unique_keys(self):
        a = card(ARTICLE)
        twin = a.model_copy(update={"id": 1, "edn": "QWERTY", "url": "https://elibrary.ru/item.asp?id=1"})
        text = ex.export([a, twin, card(BOOK)], "bib")
        heads = [line for line in text.splitlines() if line.startswith("@")]
        assert heads == ["@article{blokhina2021dlitelnoe-fyoakl,", "@article{blokhina2021dlitelnoe-qwerty,",
                         "@book{miftakhova2024obrabotka,"]
        same = ex.export([a, a], "bibtex")
        assert [x for x in same.splitlines() if x.startswith("@")] == [
            "@article{blokhina2021dlitelnoe-fyoakl,", "@article{blokhina2021dlitelnoe-fyoakl-2,"]
        assert ex.export(a, "bibtex", citekey_edn=True).startswith("@article{blokhina2021dlitelnoe-fyoakl,")

    def test_csl_and_zotero_json(self):
        data = json.loads(ex.export([card(n) for n in CARDS], "csl-json"))
        assert [d["type"] for d in data] == ["article-journal", "book", "chapter", "thesis", "patent"]
        assert len({d["id"] for d in data}) == 5
        items = json.loads(ex.export([card(PATENT), hit(ALL_TYPES, 41770713)], "zotero", tags=["eLIBRARY"]))
        assert [i["itemType"] for i in items] == ["patent", "conferencePaper"]
        assert all({"tag": "eLIBRARY"} in i["tags"] for i in items)
        assert "Блохина" in ex.export(card(ARTICLE), "zotero")  # UTF-8, not \\u escapes

    def test_ris_batch_and_hits(self):
        text = ex.export(list(rows(ALL_TYPES).values()), "ris")
        assert text.count("TY  - ") == 100 and text.count("ER  - ") == 100
        assert "TY  - PAT" in text and "TY  - CPAPER" in text and "TY  - RPRT" in text

    def test_every_row_exports_in_every_format(self):
        for name in (ALL_TYPES, "query_results_patents", "author_items_3771", "contents_8610"):
            for h in rows(name).values():
                for fmt in ex.FORMATS:
                    assert ex.export(h, fmt)


# --------------------------------------------------------------------------- what Zotero / zotero-mcp make of it

CARD_FIXTURES = ("item_article_46371463", "item_article_54076582", "item_book_76097861", "item_chapter_76098153",
                 "item_disser_30339972", "item_patent_47430099")
ROW_FIXTURES = ("author_items_17783_p2", "author_items_3771", "contents_8559", "contents_8610", "org_items_18078",
                "query_results_all_types", "query_results_page2_graphene", "query_results_patents")


def synthetic_pubs() -> list[Publication]:
    """Cards of the types (and fields) the fixtures do not have."""
    editor = AuthorRef(name="СИДОРОВ С.С.", role="Ответственный редактор")
    return [
        make_pub(kind="conference", pages="29-35", doi="10.1016/j.trpro.2022.01.006",
                 container=ContainerRef(title="TRANSPORTATION RESEARCH PROCEDIA", publisher=OrgRef(name="Elsevier B.V."),
                                        description='12. Сер. "12th International Conference" Том 61. Москва, 2022'),
                 conference=ConferenceInfo(name="12TH INTERNATIONAL CONFERENCE ON TRANSPORT", place="Irkutsk")),
        make_pub(kind="conference", pages="10-12", container=ContainerRef(title="Материалы конференции ИТ-2021"),
                 conference=ConferenceInfo(name="Международная конференция ИТ-2021", place="Москва")),
        make_pub(kind="proceedings", contributors=[editor], conference=ConferenceInfo(name="Конференция X", place="Казань"),
                 book=BookInfo(city="Казань", pages_total=300, publisher=OrgRef(name="КФУ"))),
        make_pub(kind="book", contributors=[editor], volume="2",
                 book=BookInfo(city="Москва", pages_total=200, series="Библиотека студента", edition="2-е изд.",
                               isbn=["978-5-00-000000-1"], publisher=OrgRef(name="Наука"))),
        make_pub(kind="chapter", pages="5-17", contributors=[editor],
                 container=ContainerRef(title="Сборник статей", authors=["Петров П.П."],
                                        publisher=OrgRef(name="Наука"), description="Москва, 2020")),
        make_pub(kind="report", type_raw="отчет о НИР", contributors=[editor],
                 extra={"Номер": "123-45", "Организация": "Институт проблем управления РАН"},
                 book=BookInfo(series="Препринты ИПУ")),
        make_pub(kind="preprint", type_raw="депонированная рукопись", dates=Dates(deposited=date(2020, 5, 6)),
                 extra={"Место депонирования": "ВИНИТИ", "Номер депонирования": "123-В2020"}),
        make_pub(kind="dataset", type_raw="набор данных", book=BookInfo(publisher=OrgRef(name="Zenodo"), city="Женева")),
        make_pub(kind="preprint", type_raw="препринт", extra={"Репозиторий": "arXiv"}),
        make_pub(kind=None, pages="7-19"),
    ]


def all_records() -> list[tuple[str, Publication | PublicationHit]]:
    out: list[tuple[str, Publication | PublicationHit]] = [(n, card(n)) for n in CARD_FIXTURES]
    out += [(f"synthetic-{i}", p) for i, p in enumerate(synthetic_pubs())]
    for name in ROW_FIXTURES:
        out += [(f"{name}#{h.id}", h) for h in rows(name).values()]
    return out


def extra_lines(item: dict) -> list[str]:
    return [x for x in (item.get("extra") or "").split("\n") if x]


# ---- zotero-mcp 0.11 ``zotero_add_item(source_type="csl_json")``: a port of citation_import.csl_json_to_zotero

ZMCP_TYPES = {"article-journal": "journalArticle", "article": "preprint", "book": "book", "chapter": "bookSection",
              "paper-conference": "conferencePaper", "thesis": "thesis", "report": "report", "patent": "patent",
              "manuscript": "manuscript", "dataset": "dataset"}  # anything else -> document
ZMCP_HANDLED = {"type", "id", "title", "title-short", "author", "editor", "translator", "issued", "container-title",
                "collection-title", "collection-number", "volume", "issue", "page", "publisher", "publisher-place",
                "edition", "ISBN", "ISSN", "DOI", "URL", "language", "abstract", "number-of-pages", "number", "genre",
                "keyword", "keywords", "note"}


def zmcp_template(item_type: str) -> dict:
    """What the Web API's /items/new returns: type-specific field names (university, issueDate...)."""
    t: dict = {"itemType": item_type, "title": "", "creators": []}
    for f in ex._ZOTERO_FIELDS[item_type]:
        t.setdefault(f, "")
    t.update(tags=[], collections=[], relations={})
    return t


def zotero_mcp_import(csl: dict) -> dict:
    zot_type = ZMCP_TYPES.get(csl.get("type", ""), "document")
    t = zmcp_template(zot_type)

    def text(v) -> str:
        return "" if v is None else str(v).strip()

    def put(f: str, v) -> None:
        if text(v) and f in t:
            t[f] = text(v)

    def add_extra(line: str) -> None:
        t["extra"] = f"{t['extra'].rstrip()}\n{line}" if t.get("extra") else line

    put("title", csl.get("title"))
    put("shortTitle", csl.get("title-short"))
    for key, ctype in (("author", "author"), ("editor", "editor"), ("translator", "translator")):
        for n in csl.get(key) or []:
            if n.get("literal"):
                t["creators"].append({"creatorType": ctype, "name": n["literal"]})
            else:
                t["creators"].append({"creatorType": ctype, "firstName": n.get("given", ""),
                                      "lastName": n.get("family", "")})
    issued = csl.get("issued") or {}
    if issued.get("date-parts"):
        parts = [str(p) for p in issued["date-parts"][0]]
        put("date", "-".join(p.zfill(2) if i else p for i, p in enumerate(parts)))
    container = {"journalArticle": "publicationTitle", "preprint": "publicationTitle", "bookSection": "bookTitle",
                 "conferencePaper": "proceedingsTitle"}.get(zot_type)
    if container not in t:
        container = next((f for f in ("publicationTitle", "bookTitle", "proceedingsTitle", "seriesTitle") if f in t),
                         None)
    if container and text(csl.get("container-title")):
        t[container] = text(csl["container-title"])
    for key, f in (("collection-title", "series"), ("collection-number", "seriesNumber"), ("volume", "volume"),
                   ("issue", "issue"), ("page", "pages"), ("publisher", "publisher"), ("publisher-place", "place"),
                   ("edition", "edition"), ("ISBN", "ISBN"), ("ISSN", "ISSN"), ("DOI", "DOI"), ("URL", "url"),
                   ("language", "language"), ("abstract", "abstractNote"), ("number-of-pages", "numPages")):
        put(f, csl.get(key))
    if zot_type == "report":
        put("reportNumber", csl.get("number"))
    elif zot_type == "patent":
        put("patentNumber", csl.get("number"))
    if zot_type == "thesis":
        put("thesisType", csl.get("genre"))
    if csl.get("keyword"):
        t["tags"] = [{"tag": k.strip()} for k in csl["keyword"].split(",") if k.strip()]
    if text(csl.get("id")):
        add_extra(f"Citation Key: {text(csl['id'])}")
    if text(csl.get("note")):
        add_extra(text(csl["note"]))
    for k, v in csl.items():
        if k in ZMCP_HANDLED or v in (None, "", [], {}):
            continue
        add_extra(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}")
    return t


# Native fields with no counterpart in CSL / zotero-mcp at all.
ZMCP_SKIP = {"itemType", "creators", "tags", "collections", "relations", "extra", "libraryCatalog", "accessDate"}


class TestZoteroMcpCsl:
    def test_nothing_is_lost_through_zotero_mcp(self):
        """Every value of the native Zotero item reaches the item zotero-mcp builds from our CSL: in its
        field or, when zotero-mcp cannot place it for the type, as an Extra line (never dropped)."""
        problems = []
        for name, pub in all_records():
            native = ex.to_zotero_item(pub)
            csl = ex.to_csl_json(pub, zotero_mcp=True)
            via = zotero_mcp_import(csl)
            assert via["itemType"] == native["itemType"], name
            values = {line.split(": ", 1)[1] for line in extra_lines(via) if ": " in line}
            for f, v in native.items():
                if f not in ZMCP_SKIP and via.get(f) != v and v not in values:
                    problems.append((name, f, v))
            via_extra = set(extra_lines(via))
            problems += [(name, "extra", line) for line in extra_lines(native)
                         if line not in via_extra and not line.startswith("Описание (eLIBRARY): ")]
            problems += [(name, "kept but dropped by zotero-mcp", k) for k in csl
                         if k in ZMCP_HANDLED and k not in ex._ZOTERO_MCP_ALWAYS
                         and not ex._zotero_mcp_field(k, native["itemType"])]
        assert problems == []

    def test_type_specific_fields(self):
        t = zotero_mcp_import(ex.to_csl_json(card(DISSER), zotero_mcp=True))
        assert t["thesisType"].startswith("автореферат диссертации") and t["place"] == "Уфа" and t["numPages"] == "32"
        assert "publisher: Уфим. гос. авиац.-техн. ун-т" in extra_lines(t)  # thesis template: university
        csl = ex.to_csl_json(card(PATENT), zotero_mcp=True)
        assert "issued" not in csl and "genre" not in csl and csl["number"] == "RU 2760636 C2"
        t = zotero_mcp_import(csl)
        assert t["patentNumber"] == "RU 2760636 C2"
        assert {"issued: 2021-11-29", "genre: патент на изобретение"} <= set(extra_lines(t))  # patent: issueDate
        rep = make_pub(kind="report", type_raw="отчет о НИР",
                       extra={"Номер": "123-45", "Организация": "Институт проблем управления РАН"})
        csl = ex.to_csl_json(rep, zotero_mcp=True)
        assert csl["number"] == "123-45" and "publisher" not in csl and "genre" not in csl
        assert {"publisher: Институт проблем управления РАН", "genre: отчет о НИР"} <= set(csl["note"].split("\n"))
        # kept as keys where zotero-mcp has the field
        art = ex.to_csl_json(card(ARTICLE), zotero_mcp=True)
        assert art["issued"] == {"date-parts": [[2021]]} and art["container-title"]
        book = ex.to_csl_json(card(BOOK), zotero_mcp=True)
        assert book["publisher"] == "Уфимский университет науки и технологий" and book["number-of-pages"] == "156"
        assert ex.to_csl_json(card(DISSER), zotero_mcp=True)["genre"].startswith("автореферат")
        # the plain CSL keeps everything
        assert ex.to_csl_json(card(DISSER))["publisher"] == "Уфим. гос. авиац.-техн. ун-т"


# ---- Zotero's RIS translator (RIS.js bundled with Zotero 10): a copy of its import tables for the tags we
# write ("fieldMap" and "degenerateImportFieldMap"; the second one only when the first has no mapping, its tags
# applied last) and of the import rules that matter here.

RIS_IMPORT_TYPES = {"JOUR": "journalArticle", "CPAPER": "conferencePaper", "CONF": "conferencePaper", "BOOK": "book",
                    "CHAP": "bookSection", "THES": "thesis", "PAT": "patent", "RPRT": "report",
                    "MANSCPT": "manuscript", "DATA": "dataset", "GEN": "journalArticle"}  # others: journalArticle
RIS_FIELD_MAP: dict[str, object] = {
    "AB": "abstractNote", "AN": "archiveLocation", "DB": "archive", "DO": "DOI", "KW": "tags", "N1": "notes",
    "UR": "url",
    "TI": {"__default": "title"},
    "T2": {"bookTitle": ["bookSection"], "conferenceName": ["conferencePaper"],
           "seriesTitle": ["computerProgram", "map", "report"], "series": ["book"],
           "publicationTitle": ["journalArticle", "magazineArticle", "newspaperArticle"]},
    "T3": {"series": ["bookSection", "conferencePaper", "journalArticle"]},
    "AU": {"__default": "creators/author", "creators/inventor": ["patent"]},
    "A2": {"creators/editor": ["journalArticle", "bookSection", "conferencePaper", "dictionaryEntry", "document",
                               "encyclopediaArticle"],
           "creators/seriesEditor": ["book", "report"], "issuingAuthority": ["patent"]},
    "A3": {"creators/contributor": ["thesis"], "creators/editor": ["book"],
           "creators/seriesEditor": ["bookSection", "conferencePaper", "dictionaryEntry", "encyclopediaArticle", "map"]},
    "A4": {"__default": "creators/translator", "creators/contributor": ["conferencePaper", "film", "dataset"]},
    "C1": {"filingDate": ["patent"], "place": ["conferencePaper"]},
    "C2": {"issueDate": ["patent"], "creators/bookAuthor": ["bookSection"]},
    "C3": {"proceedingsTitle": ["conferencePaper"], "country": ["patent"]},
    "CY": {"__default": "place", "repositoryLocation": ["dataset"], "__exclude": ["conferencePaper"]},
    "DA": {"__default": "date", "issueDate": ["patent"]},
    "ET": {"__default": "edition", "versionNumber": ["computerProgram", "dataset"]},
    "IS": {"__default": "issue", "numberOfVolumes": ["bookSection"], "__exclude": ["dataset"]},
    "LA": {"__default": "language"},
    "M1": {"seriesNumber": ["book"], "applicationNumber": ["patent"]},
    "M3": {"manuscriptType": ["manuscript"], "reportType": ["report"], "thesisType": ["thesis"]},
    "PB": {"__default": "publisher", "assignee": ["patent"], "institution": ["report"], "repository": ["dataset"],
           "university": ["thesis"]},
    "PY": {"__default": "date", "issueDate": ["patent"]},
    "SN": {"__default": "ISBN", "ISSN": ["journalArticle", "magazineArticle", "newspaperArticle"],
           "patentNumber": ["patent"], "reportNumber": ["report"]},
    "SP": {"__default": "pages", "numPages": ["book", "thesis", "manuscript"]},
    "VL": {"__default": "volume", "__exclude": ["patent", "webpage"]},
}
RIS_DEGENERATE_MAP: dict[str, object] = {
    "CY": "place", "ED": "creators/editor", "EP": "pages", "IS": {"versionNumber": ["dataset"]},
    "M1": {"__default": "extra"}, "M3": "DOI", "T2": "backupPublicationTitle", "T3": {"series": ["book"]},
    "VL": {"unsupported/Patent Version Number": ["patent"]},
}


def ris_field(item_type: str, tag: str) -> str | None:
    """RIS.js ``TagMapper.getField``."""
    for table in (RIS_FIELD_MAP, RIS_DEGENERATE_MAP):
        m = table.get(tag)
        field = None
        if isinstance(m, str):
            field = m
        elif isinstance(m, dict):
            field = next((f for f, types in m.items() if not f.startswith("__") and item_type in types), None)
            if field is None and m.get("__default") and item_type not in m.get("__exclude", []):
                field = m["__default"]
        if field:
            return field
    return None


def ris_import(text: str) -> dict:
    """What Zotero's RIS import makes of one of our records: {itemType, creators, notes, <fields>}."""
    lines = [x for x in text.splitlines() if x[2:6] == "  - " and x[:2] != "ER"]
    item_type = RIS_IMPORT_TYPES.get(lines[0][6:], "journalArticle")
    valid = set(ex._ZOTERO_FIELDS[item_type])
    item: dict = {"itemType": item_type, "creators": [], "notes": [], "tags": []}
    entries = [(x[:2], x[6:].strip()) for x in lines[1:]]
    ordered = ([e for e in entries if e[0] not in RIS_DEGENERATE_MAP]
               + [e for e in entries if e[0] in RIS_DEGENERATE_MAP])
    backup_date = None
    for tag, value in ordered:
        field = ris_field(item_type, tag) or "unknown"
        if tag == "PY":
            backup_date = (field, value)
            continue
        if tag == "EP":
            if item.get("pages") and "-" not in item["pages"]:
                item["pages"] += "-" + value
            continue
        if tag == "M3" and field == "DOI" and not value.startswith("10."):
            field = "unknown"
        if field.startswith("creators/"):
            last, _, first = value.partition(",")
            item["creators"].append((field.split("/")[1], last.strip(), first.strip()))
        elif field in ("tags", "notes"):
            item[field].append(value)
        elif field in ("unknown", "backupPublicationTitle", "extra") or field.startswith("unsupported/") or (
                field not in valid and field != "DOI"):
            item["notes"].append(f"{tag}: {value}")
        else:
            if field in ("date", "issueDate", "filingDate"):
                value = value.replace("/", "-")
            if item.get(field):
                if item[field] != value:  # never overwritten: a second value becomes a note
                    item["notes"].append(f"{field}: {value}")
            else:
                item[field] = value
    if backup_date and backup_date[0] in valid and not item.get(backup_date[0]):
        item[backup_date[0]] = backup_date[1]
    return item


# Native fields RIS (as Zotero reads it) has no tag for.
RIS_SKIP = {"itemType", "creators", "tags", "collections", "relations", "extra", "libraryCatalog", "accessDate",
            "eventPlace", "priorityDate", "shortTitle", "seriesNumber"}
RIS_SKIP_BY_TYPE = {"manuscript": {"institution", "number"}, "dataset": {"identifier", "type"}}


def _norm(field: str, value: str) -> str:
    value = ex._clean(value)
    return re.sub(r"[\s,]+", " ", value) if field == "ISBN" else value


def _creators(item: dict) -> list[tuple[str, str, str]]:
    return sorted((c["creatorType"], c.get("lastName", c.get("name", "")), c.get("firstName", ""))
                  for c in item["creators"])


class TestRisZoteroMapping:
    def test_tags_land_in_the_fields_of_the_zotero_item(self):
        """Importing our RIS with Zotero's RIS.js tables gives the item to_zotero_item builds."""
        checked = 0
        problems: list[tuple] = []
        for name, pub in all_records():
            native = ex.to_zotero_item(pub)
            if native["itemType"] in ("preprint", "document"):  # RIS has no such type (UNPB / GEN -> journalArticle)
                continue
            checked += 1
            got = ris_import(ex.to_ris(pub))
            assert got["itemType"] == native["itemType"], name
            skip = RIS_SKIP | RIS_SKIP_BY_TYPE.get(native["itemType"], set())
            for f, v in got.items():
                if f in ("itemType", "creators", "notes", "tags", "archive", "archiveLocation"):
                    continue
                want = native.get(f)
                if f == "place" and not native.get("place") and native.get("eventPlace"):
                    want = native["eventPlace"]  # CY of a CPAPER without a publisher place
                if want is None or _norm(f, v) != _norm(f, want):
                    problems.append((name, "wrong field", f, v, want))
            for f, v in native.items():
                if f in skip:
                    continue
                if _norm(f, got.get(f) or "") != _norm(f, v):
                    problems.append((name, "lost", f, v, got.get(f)))
            ris_creators = sorted(got["creators"])
            wanted = [c for c in _creators(native) if c[0] != "contributor" or c in ris_creators]
            if ris_creators != wanted:
                problems.append((name, "creators", ris_creators, _creators(native)))
        assert problems == []
        assert checked > 300

    def test_conference_book_and_chapter_tags(self):
        conf, _, proc, book, chapter, rep = synthetic_pubs()[:6]
        got = ris_import(ex.to_ris(conf))
        assert (got["conferenceName"], got["proceedingsTitle"], got["series"], got["place"]) == (
            "12th International Conference on Transport", "Transportation Research Procedia",
            "12th International Conference", "Москва")
        assert "place: Irkutsk" in got["notes"]  # the conference location (CY) after the publisher place (C1)
        got = ris_import(ex.to_ris(proc))
        assert got["itemType"] == "book" and ("editor", "Сидоров", "С. С.") in got["creators"]
        got = ris_import(ex.to_ris(book))
        assert (got["series"], got["numPages"], got["volume"]) == ("Библиотека студента", "200", "2")
        assert got["creators"][-1] == ("editor", "Сидоров", "С. С.")  # A3, not the series editor (A2)
        got = ris_import(ex.to_ris(chapter))
        assert ("bookAuthor", "Петров", "П. П.") in got["creators"] and ("editor", "Сидоров", "С. С.") in got["creators"]
        got = ris_import(ex.to_ris(rep))
        assert (got["seriesTitle"], got["institution"], got["reportNumber"], got["reportType"]) == (
            "Препринты ИПУ", "Институт проблем управления РАН", "123-45", "отчет о НИР")
        assert ("editor", "Сидоров", "С. С.") in got["creators"]
        assert ex.to_zotero_item(rep)["seriesTitle"] == "Препринты ИПУ"


# ---- BibTeX brace balance (BibTeX, biber/btparse and bibtexparser count braces and ignore backslashes)


def bib_fields(entry: str) -> list[tuple[str, str]]:
    lines = entry.strip("\n").split("\n")
    assert re.fullmatch(r"@\w+\{[a-z0-9-]+,", lines[0]) and lines[-1] == "}", entry
    out = []
    for line in lines[1:-1]:
        m = re.fullmatch(r"  (\w+)\s+= \{(.*)\},?", line)
        assert m, line
        out.append((m.group(1), m.group(2)))
    return out


def braces_balance(text: str) -> bool:
    depth = 0
    for c in text:
        depth += {"{": 1, "}": -1}.get(c, 0)
        if depth < 0:
            return False
    return depth == 0


class TestBibtexBraces:
    def test_every_exported_record_balances(self):
        records = all_records()
        assert len(records) > 500
        for name, pub in records:
            entry = ex.to_bibtex(pub)
            assert braces_balance(entry), name
            assert all(braces_balance(v) for _, v in bib_fields(entry)), name

    @pytest.mark.parametrize("title", ["Оценка множества {x : f(x) > 0", "Конец}", "}{", "a {b} c \\{ d"])
    def test_lone_braces(self, title):
        p = make_pub(title=title, kind="article", abstract=f"Аннотация {title}", keywords=[title],
                     authors=[AuthorRef(name="ИПМ {РАН")], url="https://elibrary.ru/item.asp?id=1&x={",
                     doi="10.1000/a{b")
        text = ex.export([p, make_pub(id=2, title="Нормальная статья")], "bibtex")
        entries = ["@" + e.lstrip("@") for e in text.split("\n@")]
        assert len(entries) == 2
        for e in entries:
            assert braces_balance(e)
            assert all(braces_balance(v) for _, v in bib_fields(e))
        first = dict(bib_fields(entries[0]))
        assert r"\textbraceleft{}" in first["title"] or r"\textbraceright{}" in first["title"]
        assert first["url"] == "https://elibrary.ru/item.asp?id=1&x=%7B" and first["doi"] == "10.1000/a%7Bb"
        assert "{10.1000/{ok}}" in ex.to_bibtex(make_pub(doi="10.1000/{ok}"))  # balanced: kept as is
