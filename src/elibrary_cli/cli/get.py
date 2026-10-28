"""``elibrary get ...``: cards and lists of one object (publication, author, organization,
journal, issue). Identifiers may be numbers, elibrary.ru URLs or, where the site resolves them,
EDN / DOI / SPIN / ORCID / ISSN / ROR."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import typer

from elibrary_cli.cli.common import (
    FORMAT_HELP,
    PAGE_HELP,
    OutputFormat,
    emit,
    entity_id,
    run,
)
from elibrary_cli.models.authors import AuthorItemsSort
from elibrary_cli.models.publications import SortOrder
from elibrary_cli.queries.organizations import ORG_ITEMS_SHOW_OPTIONS

get_app = typer.Typer(
    help="Карточки eLIBRARY.RU: публикация, автор, организация, журнал, выпуск журнала.",
    no_args_is_help=True,
)

Fmt = Annotated[OutputFormat | None, typer.Option("--format", "-f", help=FORMAT_HELP, show_default=False)]
Page = Annotated[int, typer.Option("--page", "-p", min=1, help=PAGE_HELP)]


@get_app.command("publication")
def get_publication(
    identifier: Annotated[str, typer.Argument(
        help="eLIBRARY ID, EDN, DOI, ссылка на item.asp или другой идентификатор (ISBN)")],
    fmt: Fmt = None,
) -> None:
    """Карточка публикации: авторы, источник, аннотация, ключевые слова, показатели."""
    emit(run(lambda c: c.get_publication_any(identifier)), fmt)


@get_app.command("author")
def get_author(
    identifier: Annotated[str, typer.Argument(help="AuthorID, ссылка на профиль, SPIN-код или ORCID")],
    fmt: Fmt = None,
) -> None:
    """Профиль автора: идентификаторы, организация, публикации, цитирования, индекс Хирша."""
    emit(run(lambda c: c.get_author(entity_id(c, identifier, "author"))), fmt)


@get_app.command("author-pubs")
def get_author_publications(
    identifier: Annotated[str, typer.Argument(help="AuthorID, ссылка на профиль, SPIN-код или ORCID")],
    page: Page = 1,
    sort: Annotated[AuthorItemsSort, typer.Option(
        "--sort", help="Сортировка: date, title, year_author, year_title, first_author, citations, added, "
        "relevance")] = "date",
    order: Annotated[SortOrder, typer.Option("--order", help="desc или asc")] = "desc",
    refs: Annotated[bool, typer.Option(
        "--refs/--no-refs", help="Показывать публикации, известные только по спискам литературы")] = True,
    merge: Annotated[bool, typer.Option(
        "--merge/--no-merge", help="Объединять версии публикации (оригинал и перевод)")] = True,
    fmt: Fmt = None,
) -> None:
    """Список публикаций автора (100 на странице)."""

    def action(c: Any) -> Any:
        return c.get_author_publications(entity_id(c, identifier, "author"), page, sort=sort, order=order,
                                         include_references=refs, merge_versions=merge)

    emit(run(action), fmt)


@get_app.command("org")
def get_org(
    identifier: Annotated[str, typer.Argument(help="ID организации, ссылка на её страницу или ROR")],
    card: Annotated[bool, typer.Option(
        "--card", help="Также карточка организации (адрес, тип, сайт): ещё один запрос")] = False,
    descriptions: Annotated[bool, typer.Option(
        "--descriptions", help="Сохранить пояснения к показателям")] = False,
    fmt: Fmt = None,
) -> None:
    """Профиль организации: показатели публикационной активности."""
    emit(run(lambda c: c.get_organization(entity_id(c, identifier, "organization"), with_card=card,
                                          with_descriptions=descriptions)), fmt)


@get_app.command("org-pubs")
def get_org_publications(
    identifier: Annotated[str, typer.Argument(help="ID организации, ссылка на её страницу или ROR")],
    page: Page = 1,
    year: Annotated[list[int] | None, typer.Option("--year", "-y", help="Год публикации (можно несколько раз)")] = None,
    sort: Annotated[Literal["date", "title", "year_author", "author", "cited", "insdate"] | None, typer.Option(
        "--sort", help="Сортировка: date (дата выпуска), title, year_author, author, cited (цитирования), "
        "insdate (дата размещения)", show_default=False)] = None,
    order: Annotated[SortOrder | None, typer.Option("--order", help="desc или asc", show_default=False)] = None,
    show_option: Annotated[str | None, typer.Option(
        "--show", help="Какие публикации: 1 все, 0 РИНЦ, 2 ядро РИНЦ, 5 ВАК, 9 WoS, 10 Scopus, 11 с DOI ...",
        show_default=False)] = None,
    fmt: Fmt = None,
) -> None:
    """Список публикаций организации (100 на странице; у крупных организаций сайт отвечает долго)."""

    def action(c: Any) -> Any:
        if show_option is not None and show_option not in ORG_ITEMS_SHOW_OPTIONS:
            choices = "; ".join(f"{k} — {v}" for k, v in ORG_ITEMS_SHOW_OPTIONS.items())
            raise ValueError(f"--show {show_option!r}: допустимые значения: {choices}")
        return c.get_organization_publications(entity_id(c, identifier, "organization"), page,
                                               show_option=show_option, years=list(year) if year else None,
                                               sort=sort, order=order)

    emit(run(action), fmt)


@get_app.command("journal")
def get_journal(
    identifier: Annotated[str, typer.Argument(help="ID журнала (titleid), ссылка на его страницу или ISSN")],
    english: Annotated[bool, typer.Option("--english", help="Описание на английском языке")] = False,
    indicators: Annotated[bool, typer.Option(
        "--indicators", help="Также показатели (импакт-фактор, рейтинг SCIENCE INDEX): ещё один запрос")] = False,
    fmt: Fmt = None,
) -> None:
    """Профиль журнала: ISSN, издатель, индексация (ВАК, Scopus, WoS...), рубрики, редакция."""
    emit(run(lambda c: c.get_journal(entity_id(c, identifier, "journal"), english=english,
                                     with_indicators=indicators)), fmt)


@get_app.command("issue")
def get_issue(
    journal_id: Annotated[str | None, typer.Option(
        "--journal-id", "-j", help="Последний выпуск журнала: ID журнала, ссылка или ISSN", show_default=False)] = None,
    issue_id: Annotated[int | None, typer.Option(
        "--issue-id", "-i", help="Конкретный выпуск: ID выпуска (contents.asp?id=)", show_default=False)] = None,
    fmt: Fmt = None,
) -> None:
    """Содержание выпуска журнала (по --issue-id или последний выпуск по --journal-id)."""
    if not journal_id and not issue_id:
        typer.echo("Ошибка: укажите --journal-id (последний выпуск) или --issue-id (конкретный выпуск)", err=True)
        raise typer.Exit(1)

    def action(c: Any) -> Any:
        if issue_id:
            return c.get_journal_issue(issue_id=issue_id)
        return c.get_journal_issue(journal_id=entity_id(c, str(journal_id), "journal"))

    emit(run(action), fmt)
