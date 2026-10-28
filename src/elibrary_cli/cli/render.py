"""Human-readable (``--format table``) rendering of the models with rich.

Only presentation: every value comes from the models; nothing is fetched. Cell texts are passed as
``rich.text.Text`` so that brackets in titles (``[Электронный ресурс]``) are never read as markup.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from elibrary_cli.models.authors import AuthorHit, AuthorProfile, AuthorPublicationsPage
from elibrary_cli.models.base import PublicationHit, SearchPage
from elibrary_cli.models.journals import IssueContents, JournalHit, JournalProfile
from elibrary_cli.models.organizations import OrgHit, OrgProfile, OrgPublicationsPage
from elibrary_cli.models.publications import Publication

MAX_AUTHORS = 3
TITLE_CHARS = 160
SOURCE_CHARS = 110

#: Russian labels of dict keys shown by ``auth status``, ``cache stats``, ``resolve``, ``zotero`` ...
LABELS: dict[str, str] = {
    # resolve
    "kind": "Тип идентификатора",
    "value": "Значение",
    "target": "Объект",
    "id": "ID",
    "url": "Адрес",
    "edn": "EDN",
    "location": "Перенаправление",
    "from_cache": "Из кэша",
    # status
    "base_url": "Сайт",
    "has_session_cookie": "Есть cookie сессии (SUserID)",
    "cookie_names": "Cookies (имена)",
    "account": "Учётная запись",
    "logged_in": "Вход выполнен",
    "breaker_open": "Запросы приостановлены",
    "breaker_gate": "Причина приостановки",
    "breaker_until": "Приостановлено до (unix time)",
    "breaker_url": "Адрес проверки",
    "requests_last_hour": "Запросов за час",
    "requests_last_day": "Запросов за сутки",
    "max_per_hour": "Лимит в час",
    "max_per_day": "Лимит в сутки",
    "requests_total": "Всего запросов",
    "min_delay_s": "Пауза между запросами, с",
    "state_db": "Файл состояния",
    "cache_db": "Файл кэша",
    "cache": "Кэш",
    "offline": "Только кэш (offline)",
    "current_query": "Текущий запрос сессии",
    # cache
    "pages": "Страниц в кэше",
    "bytes": "Размер кэша, байт",
    "fulltext_dir": "Каталог полных текстов",
    "fulltext_texts": "Полных текстов",
    "fulltext_bytes": "Размер полных текстов, байт",
    "downloads_dir": "Каталог PDF",
    "downloads": "PDF-файлов",
    "downloads_bytes": "Размер PDF, байт",
    "pages_removed": "Удалено страниц",
    "fulltext_removed": "Удалено полных текстов",
    "external_removed": "Удалено ответов внешних API",
    # cookies
    "count": "Количество",
    "has_guid_cookie": "Есть cookie SCookieGUID",
    "ignored": "Пропущены",
    "user_agent_saved": "User-Agent сохранён",
    "warnings": "Предупреждения",
    # zotero
    "index": "№",
    "title": "Название",
    "key": "Ключ",
    "backend": "Способ",
    "status": "Статус",
    "message": "Сообщение",
    "attachment": "Вложение",
    "collection": "Коллекция",
    "elibrary_id": "eLIBRARY ID",
    "pdf_note": "PDF",
    "running": "Zotero запущен",
    "version": "Версия Zotero",
    "connector": "Коннектор",
    "local_api": "Локальный API",
    "local_api_writable": "Запись через локальный API",
    "local_api_key": "Ключ локального API",
    "local_key_source": "Источник ключа",
    "web_configured": "Web API настроен",
    "available_backends": "Доступные способы",
    "hints": "Подсказки",
    "config": "Настройки",
    # refdata
    "label": "Название",
    "site_code": "Код на сайте",
    "form_field": "Поле формы",
    "name": "Справочник",
    "description": "Описание",
    "query_field": "Поле запроса",
    "aliases": "Сокращения",
}

_TARGETS = {"publication": "публикация", "author": "автор", "organization": "организация",
            "journal": "журнал", "issue": "выпуск журнала"}


def console() -> Console:
    """A console on the current stdout (resolved at call time: tests swap ``sys.stdout``)."""
    return Console(file=sys.stdout, highlight=False, soft_wrap=False)


def _text(value: Any) -> Text:
    return Text("" if value is None else str(value))


def _title(title: str | Text | None) -> Text | None:
    """A table title as ``Text``: rich parses ``str`` titles as markup, so "[bmim]" in a publication
    title would vanish and "[/" would raise ``MarkupError``."""
    if title is None or isinstance(title, Text):
        return title
    return Text(str(title), style="table.title")  # rich styles only str titles itself


def _cut(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _num(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, int):
        # Russian typography groups digits only from five digits on (so years stay "2020")
        return f"{value:,}".replace(",", " ") if abs(value) >= 10000 else str(value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _value(value: Any) -> str:
    """A dict value as one cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, Mapping):
        return "; ".join(f"{k}: {_value(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_value(v) for v in value)
    if isinstance(value, BaseModel):
        return _value(value.model_dump(mode="json"))
    return str(value)


def _authors(names: Sequence[str]) -> str:
    if len(names) <= MAX_AUTHORS:
        return ", ".join(names)
    return ", ".join(names[:MAX_AUTHORS]) + f" и др. ({len(names)})"


def page_summary(page: SearchPage[Any], noun: str = "Найдено") -> str:
    """'Найдено: 290 705 из 82 293 065 · страница 1 из 2 908 · из кэша'."""
    parts = []
    if page.total is not None:
        total = f"{noun}: {_num(page.total)}"
        if page.total_in_database is not None:
            total += f" из {_num(page.total_in_database)}"
        parts.append(total)
    else:
        parts.append(f"Показано: {len(page.items)}")
    parts.append(f"страница {page.page}" + (f" из {_num(page.pages)}" if page.pages else ""))
    if page.from_cache:
        parts.append("из кэша")
    return " · ".join(parts)


def kv_table(rows: Iterable[tuple[str, Any]], title: str | None = None) -> Table:
    """Two-column 'label: value' table; empty values are skipped."""
    table = Table(title=_title(title), box=box.SIMPLE, show_header=False, title_justify="left", pad_edge=False)
    table.add_column("Поле", style="bold", no_wrap=True)
    table.add_column("Значение", overflow="fold")
    for label, value in rows:
        cell = value if isinstance(value, Text) else _value(value)
        if cell not in ("", None):
            table.add_row(_text(label), cell if isinstance(cell, Text) else _text(cell))
    return table


# ---------------------------------------------------------------------------- lists
def publications_table(items: Sequence[PublicationHit], title: str | None = None, *,
                       with_pages: bool = False) -> Table:
    """Compact table of publication rows: id, title, authors, source, year, citations."""
    table = Table(title=_title(title), box=box.SIMPLE_HEAD, title_justify="left", pad_edge=False)
    table.add_column("ID", justify="right", no_wrap=True)
    table.add_column("Название", ratio=5, overflow="fold")
    table.add_column("Авторы", ratio=2, overflow="fold")
    if with_pages:
        table.add_column("Стр.", no_wrap=True)
    else:
        table.add_column("Источник", ratio=4, overflow="fold")
    table.add_column("Год", justify="right", no_wrap=True)
    table.add_column("Цит.", justify="right", no_wrap=True)
    section = None
    for hit in items:
        if with_pages and hit.section and hit.section != section:
            section = hit.section
            table.add_row("", Text(section, style="bold"), "", "", "", "")
        table.add_row(
            _text(hit.id),
            _text(_cut(hit.title, TITLE_CHARS)),
            _text(_authors(hit.authors)),
            _text(hit.pages or "") if with_pages else _text(_cut(hit.source, SOURCE_CHARS)),
            _text(hit.year or ""),
            _text(_num(hit.citations)),
        )
    return table


def authors_table(items: Sequence[AuthorHit], title: str | None = None) -> Table:
    table = Table(title=_title(title), box=box.SIMPLE_HEAD, title_justify="left", pad_edge=False)
    table.add_column("ID", justify="right", no_wrap=True)
    table.add_column("ФИО", ratio=2, overflow="fold")
    table.add_column("Организация", ratio=4, overflow="fold")
    table.add_column("Публ.", justify="right", no_wrap=True)
    table.add_column("Цит.", justify="right", no_wrap=True)
    table.add_column("h", justify="right", no_wrap=True)
    for a in items:
        org = a.affiliation or ""
        if a.town:
            org = f"{org} ({a.town})" if org else a.town
        name = a.name + (" *" if a.science_index_registered else "")
        table.add_row(_text(a.id), _text(name), _text(_cut(org, SOURCE_CHARS)), _text(_num(a.publications)),
                      _text(_num(a.citations)), _text(_num(a.hindex)))
    return table


def orgs_table(items: Sequence[OrgHit], title: str | None = None) -> Table:
    table = Table(title=_title(title), box=box.SIMPLE_HEAD, title_justify="left", pad_edge=False)
    table.add_column("ID", justify="right", no_wrap=True)
    table.add_column("Название", ratio=5, overflow="fold")
    table.add_column("Город", ratio=1, overflow="fold")
    table.add_column("Публ.", justify="right", no_wrap=True)
    table.add_column("Цит.", justify="right", no_wrap=True)
    for o in items:
        table.add_row(_text(o.id), _text(o.name), _text(o.town or ""), _text(_num(o.publications)),
                      _text(_num(o.citations)))
    return table


def journals_table(items: Sequence[JournalHit], title: str | None = None) -> Table:
    table = Table(title=_title(title), box=box.SIMPLE_HEAD, title_justify="left", pad_edge=False)
    table.add_column("ID", justify="right", no_wrap=True)
    table.add_column("Название", ratio=3, overflow="fold")
    table.add_column("Издатель", ratio=3, overflow="fold")
    table.add_column("ISSN", no_wrap=True)
    table.add_column("Вып.", justify="right", no_wrap=True)
    table.add_column("Публ.", justify="right", no_wrap=True)
    table.add_column("Цит.", justify="right", no_wrap=True)
    for j in items:
        table.add_row(_text(j.id), _text(j.name), _text(_cut(j.publisher, SOURCE_CHARS)), _text(j.issn or ""),
                      _text(_num(j.issues)), _text(_num(j.articles)), _text(_num(j.citations)))
    return table


def records_table(rows: Sequence[Mapping[str, Any]], title: str | None = None) -> Table:
    """Generic table of dicts (zotero results, reference options...): one column per key."""
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    table = Table(title=_title(title), box=box.SIMPLE_HEAD, title_justify="left", pad_edge=False)
    for k in keys:
        table.add_column(_text(LABELS.get(k, k)), overflow="fold")
    for row in rows:
        table.add_row(*(_text(_value(row.get(k))) for k in keys))
    return table


def zotero_results_table(rows: Sequence[Mapping[str, Any]]) -> Table:
    """Outcome of ``elibrary zotero add``: one row per publication."""
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for name in ("eLIBRARY ID", "Название", "Статус", "Ключ", "Способ", "Вложение", "Сообщение"):
        table.add_column(name, overflow="fold")
    status_names = {"created": "добавлено", "duplicate": "уже есть", "failed": "ошибка"}
    for r in rows:
        status = r.get("status")
        message = "; ".join(x for x in (r.get("message"), r.get("pdf_note")) if x)
        table.add_row(_text(r.get("elibrary_id") or ""), _text(_cut(r.get("title"), 90)),
                      _text(f"{status_names.get(status, status)} ({status})" if status else ""),
                      _text(r.get("key") or ""), _text(r.get("backend") or ""), _text(r.get("attachment") or ""),
                      _text(message))
    return table


def render_zotero_results(rows: Sequence[Mapping[str, Any]]) -> None:
    console().print(zotero_results_table(rows))


# ---------------------------------------------------------------------------- cards
def publication_rows(pub: Publication) -> list[tuple[str, Any]]:
    journal = pub.journal
    authors = ", ".join(
        a.name + (f" [{a.author_id}]" if a.author_id else "") for a in pub.authors
    )
    source = None
    if journal and journal.name:
        source = journal.name + (f" (ID {journal.title_id})" if journal.title_id else "")
    elif pub.container and pub.container.title:
        source = pub.container.title + (f" (ID {pub.container.id})" if pub.container.id else "")
    rows: list[tuple[str, Any]] = [
        ("eLIBRARY ID", pub.id),
        ("EDN", pub.edn),
        ("DOI", pub.doi),
        ("Название", pub.title),
        ("Название (англ.)", pub.title_en),
        ("Тип", pub.type_raw or pub.kind),
        ("Язык", pub.language),
        ("Авторы", authors),
        ("Источник", source),
        ("Конференция", pub.conference.name if pub.conference else None),
        ("Год", pub.year),
        ("Том", pub.volume),
        ("Выпуск", pub.issue),
        ("Страницы", pub.pages),
        ("ISSN", ", ".join(x for x in (journal.issn, journal.eissn) if x) if journal else None),
        ("Ключевые слова", ", ".join(pub.keywords)),
        ("УДК", pub.udk),
    ]
    if pub.patent:
        rows += [("Номер патента", pub.patent.number), ("Патентообладатели",
                                                         ", ".join(h.name for h in pub.patent.holders))]
    if pub.book:
        rows += [("ISBN", ", ".join(pub.book.isbn)),
                 ("Издательство", pub.book.publisher.name if pub.book.publisher else None)]
    if pub.dissertation:
        rows += [("Учёная степень", pub.dissertation.degree), ("Специальность", pub.dissertation.specialty)]
    m = pub.metrics
    rows += [
        ("Входит в РИНЦ", m.in_rinc),
        ("Цитирований в РИНЦ", m.rinc_citations),
        ("Входит в ядро РИНЦ", m.in_core),
        ("Цитирований из ядра РИНЦ", m.core_citations),
        ("Полный текст", pub.fulltext.access or ("есть" if pub.fulltext.available else None)),
        ("Адрес", pub.url),
    ]
    return rows


def render_publication(pub: Publication, con: Console) -> None:
    con.print(kv_table(publication_rows(pub), title=pub.title_display or pub.title))
    if pub.abstract:
        con.print(Text("Аннотация", style="bold"))
        con.print(Text(pub.abstract))


def author_rows(a: AuthorProfile) -> list[tuple[str, Any]]:
    aff = a.affiliation
    org = None
    if aff:
        org = aff.name + (f" ({aff.town})" if aff.town else "") + (f", ID {aff.org_id}" if aff.org_id else "")

    def trio(*vals: Any) -> str:
        return " / ".join(_num(v) if v is not None else "—" for v in vals) if any(v is not None for v in vals) else ""

    return [
        ("AuthorID", a.id),
        ("ФИО", a.name + (" (зарегистрирован в SCIENCE INDEX)" if a.science_index_registered else "")),
        ("SPIN", a.spin),
        ("ORCID", a.orcid),
        ("ResearcherID", a.researcher_id),
        ("Scopus Author ID", a.scopus_id),
        ("Организация", org),
        ("Публикаций (eLIBRARY / РИНЦ / ядро)", trio(a.publications_elibrary, a.publications_rinc, a.publications_core)),
        ("Цитирований (eLIBRARY / РИНЦ / ядро)", trio(a.citations_elibrary, a.citations_rinc, a.citations_core)),
        ("Индекс Хирша (eLIBRARY / РИНЦ / ядро)", trio(a.hindex_elibrary, a.hindex_rinc, a.hindex_core)),
        ("Год первой публикации", a.first_publication_year),
        ("Основная рубрика (ГРНТИ)", a.main_rubric_grnti),
        ("Основная рубрика (OECD)", a.main_rubric_oecd),
        ("Профиль", a.url),
        ("Публикации", a.publications_url),
    ]


def org_rows(o: OrgProfile) -> list[tuple[str, Any]]:
    place = ", ".join(x for x in (o.town, o.region, o.country) if x)
    return [
        ("ID", o.id),
        ("Название", o.name),
        ("Полное название", o.full_name if o.full_name != o.name else None),
        ("Сокращение", o.abbreviation),
        ("Тип", o.org_type),
        ("Ведомство", o.department),
        ("Местоположение", place),
        ("Адрес", o.address),
        ("Сайт", o.website),
        ("Публикаций на eLIBRARY.RU", o.publications),
        ("Публикаций в РИНЦ", o.publications_rinc),
        ("Публикаций в ядре РИНЦ", o.publications_core),
        ("Цитирований (eLIBRARY.RU)", o.citations),
        ("Цитирований в РИНЦ", o.citations_rinc),
        ("Индекс Хирша", o.h_index),
        ("Авторов", o.authors_count),
        ("Дата обновления показателей", o.updated.isoformat() if o.updated else None),
        ("Профиль", o.url),
        ("Публикации", o.publications_url),
    ]


def journal_rows(j: JournalProfile) -> list[tuple[str, Any]]:
    ix = j.indexing
    marks = [name for name, on in (("РИНЦ", ix.risc), ("ядро РИНЦ", ix.core_rinc), ("RSCI", ix.rsci),
                                   ("ВАК", ix.vak), ("Белый список", ix.white_list), ("Scopus", ix.scopus),
                                   ("Web of Science", ix.wos), ("DOAJ", ix.doaj)) if on]
    return [
        ("ID", j.id),
        ("Название", j.name),
        ("Название (англ.)", j.name_en),
        ("ISSN", j.issn),
        ("eISSN", j.eissn),
        ("Издатель", j.publisher),
        ("Учредители", ", ".join(f.name for f in j.founders)),
        ("Страна, город", ", ".join(x for x in (j.country, j.city) if x)),
        ("Год основания", j.foundation_year),
        ("Выпусков в год", j.issues_per_year),
        ("Период выпуска", j.publication_period),
        ("Доступ к полным текстам", j.fulltext_access),
        ("Индексация", ", ".join(marks)),
        ("Категория ВАК", f"К{ix.vak_category}" if ix.vak_category else None),
        ("Двухлетний импакт-фактор РИНЦ", j.impact_factor_2y),
        ("Показатель в рейтинге SCIENCE INDEX", j.science_index_rating),
        ("Статей на eLIBRARY.RU", j.articles_total),
        ("Выпусков на eLIBRARY.RU", j.issues_total),
        ("Сайт", j.website),
        ("Профиль", j.url),
        ("Содержание выпусков", j.contents_url),
    ]


def render_issue(issue: IssueContents, con: Console) -> None:
    head = " · ".join(x for x in (issue.journal_name, issue.label) if x)
    rows = [("Журнал", f"{issue.journal_name or ''} (ID {issue.journal_id})" if issue.journal_id else issue.journal_name),
            ("Выпуск", issue.label), ("ID выпуска", issue.issue_id), ("EDN", issue.edn), ("Адрес", issue.url)]
    con.print(kv_table(rows, title=head or None))
    con.print(publications_table(issue.items, title=f"Статей: {len(issue.items)}", with_pages=True))


def render_search_page(page: SearchPage[Any], con: Console) -> None:
    items = page.items
    if isinstance(page, AuthorPublicationsPage):
        title = f"{page.author_name or 'Автор'} (AuthorID {page.author_id}) · {page_summary(page)}"
    elif isinstance(page, OrgPublicationsPage):
        title = f"{page.org_name or 'Организация'} (ID {page.org_id}) · {page_summary(page)}"
    else:
        title = page_summary(page)
    if not items:
        con.print(Text(title))
        con.print(Text("Ничего не найдено."))
        return
    first = items[0]
    if isinstance(first, PublicationHit):
        con.print(publications_table(items, title=title))
    elif isinstance(first, AuthorHit):
        con.print(authors_table(items, title=title))
    elif isinstance(first, OrgHit):
        con.print(orgs_table(items, title=title))
    elif isinstance(first, JournalHit):
        con.print(journals_table(items, title=title))
    else:  # pragma: no cover - every list model is handled above
        con.print(records_table([i.model_dump(mode="json") for i in items], title=title))
    if page.has_next:
        sys.stderr.write(f"Следующая страница: --page {page.page + 1}\n")


def render_resolution(res: Mapping[str, Any], con: Console) -> None:
    rows = [(LABELS.get(k, k), _TARGETS.get(v, v) if k == "target" else v) for k, v in res.items()]
    con.print(kv_table(rows))


def render_table(obj: Any, con: Console | None = None) -> None:
    """Render any result of a command as a table / card."""
    con = con or console()
    if isinstance(obj, Publication):
        render_publication(obj, con)
    elif isinstance(obj, AuthorProfile):
        con.print(kv_table(author_rows(obj), title=obj.name))
    elif isinstance(obj, OrgProfile):
        con.print(kv_table(org_rows(obj), title=obj.name))
    elif isinstance(obj, JournalProfile):
        con.print(kv_table(journal_rows(obj), title=obj.name))
    elif isinstance(obj, IssueContents):
        render_issue(obj, con)
    elif isinstance(obj, SearchPage):
        render_search_page(obj, con)
    elif isinstance(obj, BaseModel):
        data = obj.model_dump(mode="json")
        con.print(kv_table((LABELS.get(k, k), v) for k, v in data.items() if v not in (None, "", [], {})))
    elif isinstance(obj, Mapping):
        con.print(kv_table((LABELS.get(k, k), v) for k, v in obj.items()))
    elif isinstance(obj, (list, tuple)):
        if obj and all(isinstance(x, Mapping) for x in obj):
            con.print(records_table(list(obj)))
        else:
            for x in obj:
                con.print(_text(_value(x)))
    else:
        con.print(_text(obj))
