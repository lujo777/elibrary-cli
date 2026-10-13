"""``elibrary search ...``: publications (articles, patents, books, dissertations, conference
papers, any type), authors, organizations and journals. One command = one result page = usually
one request to eLIBRARY.RU (cached for a day)."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from elibrary_cli.cli.common import (
    FORMAT_HELP,
    PAGE_HELP,
    OutputFormat,
    choice_enum,
    echo_err,
    emit,
    enum_values,
    run,
)
from elibrary_cli.models.authors import AuthorSort, MetricsBasis
from elibrary_cli.models.organizations import OrgSort
from elibrary_cli.models.publications import Period, PubType, SortField, SortOrder, WhereField

search_app = typer.Typer(
    help="Поиск в eLIBRARY.RU: публикации (статьи, патенты, книги, диссертации), авторы, организации, журналы.",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------- shared option types
WhereChoice = choice_enum("WhereChoice", WhereField)
TypeChoice = choice_enum("TypeChoice", PubType)

Query = Annotated[str | None, typer.Argument(
    help="Строка поиска (не короче 2 символов); можно не указывать, если заданы --author/--journal-id/--rubric")]
Where = Annotated[list[WhereChoice] | None, typer.Option(
    "--where", "-w", help="Где искать (можно несколько раз): name (название), abstract (аннотация), "
    "keywords (ключевые слова), affiliation (организации авторов), fulltext (полный текст). "
    "По умолчанию: name, abstract, keywords")]
YearFrom = Annotated[int | None, typer.Option("--year-from", help="Год публикации: с")]
YearTo = Annotated[int | None, typer.Option("--year-to", help="Год публикации: по")]
PeriodOpt = Annotated[Period, typer.Option(
    "--period", help="Поступившие в eLIBRARY.RU: all (за всё время), m1, m3, m6, m12 (за последние N месяцев)")]
SortOpt = Annotated[SortField, typer.Option(
    "--sort", help="Сортировка: rank (релевантность), date (дата выпуска), insdate (дата добавления), "
    "name (название статьи), title (название журнала), cited (цитирования)")]
OrderOpt = Annotated[SortOrder, typer.Option("--order", help="Порядок: desc (по убыванию) или asc (по возрастанию)")]
Morph = Annotated[bool, typer.Option("--morph", help="Искать с учётом морфологии")]
Fuzzy = Annotated[bool, typer.Option("--fuzzy/--no-fuzzy", help="Искать похожий текст (как на сайте, по умолчанию да)")]
FulltextOnly = Annotated[bool, typer.Option("--fulltext-only", help="Только публикации с полным текстом на eLIBRARY.RU")]
OpenOnly = Annotated[bool, typer.Option("--open-only", help="Только публикации, доступные вам (нужен вход)")]
Authors = Annotated[list[str] | None, typer.Option(
    "--author", "-a", help="Автор «Фамилия И О» (можно несколько раз)")]
JournalIds = Annotated[list[int] | None, typer.Option(
    "--journal-id", "-j", help="ID журнала в eLIBRARY.RU (можно несколько раз; см. `elibrary search journals`)")]
Rubrics = Annotated[list[str] | None, typer.Option(
    "--rubric", "-r", help="Код рубрики ГРНТИ, например 500000 (можно несколько раз; см. `elibrary refdata rubrics`)")]
Page = Annotated[int, typer.Option("--page", "-p", min=1, help=PAGE_HELP)]
Fmt = Annotated[OutputFormat | None, typer.Option("--format", "-f", help=FORMAT_HELP, show_default=False)]


def _publication_filters(*, where: list[Any] | None, year_from: int | None, year_to: int | None, period: str,
                         sort: str, order: str, morph: bool, fuzzy: bool, fulltext_only: bool, open_only: bool,
                         author: list[str] | None, journal_id: list[int] | None, rubric: list[str] | None,
                         page: int) -> dict[str, Any]:
    """PublicationQuery fields from the CLI options (unset options keep the model defaults)."""
    data: dict[str, Any] = {
        "year_from": year_from, "year_to": year_to, "period": period, "sort": sort, "order": order,
        "morphology": morph, "fuzzy": fuzzy, "fulltext_only": fulltext_only, "open_only": open_only,
        "page": page,
    }
    if where:
        data["where"] = enum_values(where)
    if author:
        data["author_names"] = list(author)
    if journal_id:
        data["journal_ids"] = list(journal_id)
    if rubric:
        data["rubric_codes"] = list(rubric)
    return {k: v for k, v in data.items() if v is not None}


def _make_publication_command(name: str, method: str, help_text: str) -> None:
    """Register ``elibrary search <name>`` calling ``ElibraryClient.<method>(text, **filters)``."""

    def command(
        query: Query = None,
        where: Where = None,
        year_from: YearFrom = None,
        year_to: YearTo = None,
        period: PeriodOpt = "all",
        sort: SortOpt = "rank",
        order: OrderOpt = "desc",
        morph: Morph = False,
        fuzzy: Fuzzy = True,
        fulltext_only: FulltextOnly = False,
        open_only: OpenOnly = False,
        author: Authors = None,
        journal_id: JournalIds = None,
        rubric: Rubrics = None,
        page: Page = 1,
        fmt: Fmt = None,
    ) -> None:
        filters = _publication_filters(where=where, year_from=year_from, year_to=year_to, period=period, sort=sort,
                                       order=order, morph=morph, fuzzy=fuzzy, fulltext_only=fulltext_only,
                                       open_only=open_only, author=author, journal_id=journal_id, rubric=rubric,
                                       page=page)
        result = run(lambda c: getattr(c, method)(query, **filters))
        emit(result, fmt)

    command.__doc__ = help_text
    command.__name__ = f"search_{name}"
    search_app.command(name)(command)


_make_publication_command(
    "articles", "search_articles",
    "Статьи в журналах. Пример: elibrary search articles \"нейронные сети\" --year-from 2020 --sort cited")
_make_publication_command(
    "patents", "search_patents",
    "Патенты и свидетельства о регистрации программ и баз данных.")
_make_publication_command(
    "books", "search_books",
    "Книги (монографии, учебники, сборники). Материалы конференций — `search conferences`; главы книг "
    "сайт относит к книгам или к материалам конференций.")
_make_publication_command(
    "dissertations", "search_dissertations",
    "Диссертации и авторефераты диссертаций.")
_make_publication_command(
    "conferences", "search_conference_papers",
    "Материалы конференций: сборники и доклады.")


@search_app.command("publications")
def search_publications(
    query: Query = None,
    types: Annotated[list[TypeChoice] | None, typer.Option(
        "--type", "-t", help="Тип публикаций (можно несколько раз): article, disser, book, report, conf, patent, "
        "preprint, grant, dataset. По умолчанию все")] = None,
    where: Where = None,
    year_from: YearFrom = None,
    year_to: YearTo = None,
    period: PeriodOpt = "all",
    sort: SortOpt = "rank",
    order: OrderOpt = "desc",
    morph: Morph = False,
    fuzzy: Fuzzy = True,
    fulltext_only: FulltextOnly = False,
    open_only: OpenOnly = False,
    author: Authors = None,
    journal_id: JournalIds = None,
    rubric: Rubrics = None,
    page: Page = 1,
    fmt: Fmt = None,
) -> None:
    """Расширенный поиск публикаций любых типов (статьи, книги, патенты, диссертации, отчёты...)."""
    filters = _publication_filters(where=where, year_from=year_from, year_to=year_to, period=period, sort=sort,
                                   order=order, morph=morph, fuzzy=fuzzy, fulltext_only=fulltext_only,
                                   open_only=open_only, author=author, journal_id=journal_id, rubric=rubric,
                                   page=page)
    if query is not None:
        filters["text"] = query
    if types:
        filters["types"] = enum_values(types)
    result = run(lambda c: c.search_publications(filters))
    emit(result, fmt)


# ---------------------------------------------------------------------------- authors
def _org_resolved_message(info: dict[str, Any]) -> str:
    """The stderr line about the organization ``--org`` was resolved to."""
    where = f", {info['town']}" if info.get("town") else ""
    count = info.get("publications")
    pubs = ", публикаций: " + f"{count:,}".replace(",", " ") if isinstance(count, int) else ""
    how = ("точное совпадение названия" if info.get("match") == "exact"
           else "больше всего публикаций среди найденных")
    n = info.get("candidates")
    found = f"; найдено организаций по запросу «{info.get('query')}»: {n}" if n is not None else ""
    return (f"Организация: {info.get('name')} (ID {info.get('id')}{where}{pubs}) — {how}{found}. "
            "Если это не та организация, найдите нужную (`elibrary search orgs`) и укажите её --org-id.")


@search_app.command("authors")
def search_authors(
    surname: Annotated[str | None, typer.Argument(help="Фамилия (можно «Фамилия Имя Отчество» или «Фамилия И.О.»)")] = None,
    spin: Annotated[str | None, typer.Option("--spin", help="SPIN-код, например 9846-9426")] = None,
    orcid: Annotated[str | None, typer.Option("--orcid", help="ORCID")] = None,
    researcherid: Annotated[str | None, typer.Option("--researcherid", help="ResearcherID (Web of Science)")] = None,
    scopus_id: Annotated[str | None, typer.Option("--scopus-id", help="Scopus Author ID")] = None,
    rsci_id: Annotated[str | None, typer.Option("--rsci-id", help="AuthorID РИНЦ (eLIBRARY.RU)")] = None,
    arxiv_id: Annotated[str | None, typer.Option("--arxiv-id", help="ArXiv ID")] = None,
    country: Annotated[str | None, typer.Option(
        "--country", help="Страна: код ISO3 (RUS) или название по-русски / по-английски (Россия, Russia)")] = None,
    town: Annotated[str | None, typer.Option("--town", help="Город, как на сайте (например, Москва)")] = None,
    org: Annotated[str | None, typer.Option(
        "--org", help="Название организации: без --org-id ищется среди организаций (1 доп. запрос), берётся "
        "точное совпадение или организация с наибольшим числом публикаций")] = None,
    org_id: Annotated[int | None, typer.Option(
        "--org-id", help="ID организации в eLIBRARY.RU (см. `elibrary search orgs`)")] = None,
    in_affiliation: Annotated[bool, typer.Option(
        "--in-affiliation", help="Также авторы, у которых организация указана в публикациях")] = False,
    rubric: Annotated[str | None, typer.Option("--rubric", "-r", help="Код рубрики ГРНТИ (6 цифр)")] = None,
    sort: Annotated[AuthorSort, typer.Option(
        "--sort", help="Сортировка: name, publications, citations, hindex")] = "citations",
    order: OrderOpt = "desc",
    metrics: Annotated[MetricsBasis, typer.Option(
        "--metrics", help="Основа показателей: core (ядро РИНЦ), rinc (РИНЦ), elibrary (все на eLIBRARY.RU)")] = "rinc",
    page: Page = 1,
    fmt: Fmt = None,
) -> None:
    """Поиск авторов по фамилии, SPIN/ORCID/ResearcherID/Scopus ID, организации.

    Сайт фильтрует по организации только по её ID: --org без --org-id сначала ищется среди организаций
    (один запрос, кэшируется), выбранная организация печатается в stderr."""
    codes = [(t, v) for t, v in (("SPIN", spin), ("ORCID", orcid), ("ResearcherID", researcherid),
                                 ("ScopusID", scopus_id), ("RSCI", rsci_id), ("ArXivID", arxiv_id)) if v]
    if len(codes) > 1:
        echo_err("Ошибка: укажите только один персональный идентификатор (--spin, --orcid, --researcherid, "
                 "--scopus-id, --rsci-id или --arxiv-id)")
        raise typer.Exit(1)

    def action(c: Any) -> Any:
        data: dict[str, Any] = {
            "surname": surname, "country": country, "town": town,
            "org_id": org_id, "org_name": org, "in_affiliation": in_affiliation, "rubric_code": rubric,
            "sort": sort, "order": order, "metrics": metrics, "page": page,
        }
        if codes:
            data["code_type"], data["code_value"] = codes[0]
        return c.search_authors({k: v for k, v in data.items() if v is not None})

    result = run(action)
    resolved = result.query.get("org_resolved") if isinstance(result.query, dict) else None
    if resolved:
        echo_err(_org_resolved_message(resolved))
    emit(result, fmt)


# ---------------------------------------------------------------------------- organizations
@search_app.command("orgs")
def search_orgs(
    name: Annotated[str | None, typer.Argument(help="Слова из названия организации")] = None,
    town: Annotated[str | None, typer.Option("--town", help="Город, как на сайте (например, Новосибирск)")] = None,
    country: Annotated[str | None, typer.Option("--country", help="Страна: код ISO3 (RUS) или название")] = None,
    region: Annotated[str | None, typer.Option(
        "--region", help="Регион: название или ID (см. `elibrary refdata regions`)")] = None,
    sort: Annotated[OrgSort, typer.Option("--sort", help="Сортировка: name, town, publications, citations")] = "name",
    order: Annotated[SortOrder | None, typer.Option(
        "--order", help="asc/desc (по умолчанию как на сайте)", show_default=False)] = None,
    page: Page = 1,
    fmt: Fmt = None,
) -> None:
    """Поиск организаций (20 на странице)."""
    data: dict[str, Any] = {"name": name, "town": town, "country": country, "sort": sort, "order": order,
                            "page": page}
    if region:
        if region.strip().isdigit():
            data["region_id"] = int(region)
        else:
            data["region"] = region
    emit(run(lambda c: c.search_organizations({k: v for k, v in data.items() if v is not None})), fmt)


# ---------------------------------------------------------------------------- journals
@search_app.command("journals")
def search_journals(
    name: Annotated[str | None, typer.Argument(help="Фрагмент названия журнала, издателя или ISSN")] = None,
    country: Annotated[str | None, typer.Option(
        "--country", help="Страна: RUS, nRUS (зарубежные), код ISO3 или название")] = None,
    rubric: Annotated[str | None, typer.Option(
        "--rubric", "-r", help="Рубрика ГРНТИ верхнего уровня: код (270000 или 27) или название")] = None,
    language: Annotated[str | None, typer.Option("--language", help="Язык: RU, EN, nRU, ML или название")] = None,
    vak: Annotated[str | None, typer.Option("--vak", help="Перечень ВАК: included, k1, k2, k3, not")] = None,
    scopus: Annotated[str | None, typer.Option(
        "--scopus", help="Scopus: indexed, translated, partial, q1..q4, not")] = None,
    wos: Annotated[str | None, typer.Option(
        "--wos", help="Web of Science: indexed, translated, partial, q1..q4, q1-q4, not")] = None,
    white: Annotated[str | None, typer.Option(
        "--white", help="Белый список / ЕГПНИ: included, l1..l4, not, egpni, egpni_l1.., any, any_l1..")] = None,
    risc: Annotated[str | None, typer.Option("--risc", help="РИНЦ / Science Index: indexed, not, q1..q4")] = None,
    open_access: Annotated[str | None, typer.Option(
        "--open-access", help="Полные тексты: open, partial, subscription")] = None,
    translated: Annotated[str | None, typer.Option("--translated", help="Перевод: full, partial, no")] = None,
    rsci: Annotated[bool, typer.Option("--rsci", help="Только журналы RSCI (Russian Science Citation Index)")] = False,
    doaj: Annotated[bool, typer.Option("--doaj", help="Только журналы из DOAJ")] = False,
    core_rinc: Annotated[bool, typer.Option("--core-rinc", help="Только журналы ядра РИНЦ")] = False,
    fulltext: Annotated[bool, typer.Option("--fulltext", help="Только журналы с полными текстами на eLIBRARY.RU")] = False,
    active: Annotated[bool, typer.Option("--active", help="Только издающиеся сейчас")] = False,
    scientific: Annotated[bool, typer.Option("--scientific", help="Только научные журналы")] = False,
    sort: Annotated[str, typer.Option(
        "--sort", help="Сортировка: name, articles, citations_rinc, citations_core, si_rating")] = "citations_core",
    order: Annotated[SortOrder | None, typer.Option(
        "--order", help="asc/desc (по умолчанию как на сайте)", show_default=False)] = None,
    page: Page = 1,
    fmt: Fmt = None,
) -> None:
    """Поиск журналов в каталоге eLIBRARY.RU. Коды фильтров: `elibrary refdata journals.<поле>`."""
    data: dict[str, Any] = {
        "name": name, "country": country, "rubric_code": rubric, "language": language, "vak": vak,
        "scopus": scopus, "wos": wos, "white": white, "risc": risc, "ftopen": open_access, "translated": translated,
        "rsci": rsci, "doaj": doaj, "core_rinc": core_rinc, "fulltext": fulltext, "active": active,
        "scientific": scientific, "sort": sort, "order": order, "page": page,
    }
    emit(run(lambda c: c.search_journals({k: v for k, v in data.items() if v is not None})), fmt)
