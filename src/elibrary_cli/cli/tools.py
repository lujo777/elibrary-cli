"""``elibrary resolve``, ``fulltext``, ``export``, ``zotero ...``, ``refdata`` and ``mcp``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import typer

from elibrary_cli.cli.common import (
    FORMAT_HELP,
    OutputFormat,
    choice_enum,
    echo_err,
    emit,
    enum_values,
    handle_errors,
    make_settings,
    resolve_format,
    run,
    write_file,
    write_out,
)

Fmt = Annotated[OutputFormat | None, typer.Option("--format", "-f", help=FORMAT_HELP, show_default=False)]
FulltextSource = Literal["elibrary", "cyberleninka", "unpaywall", "openalex", "crossref", "publisher"]
ExportFormat = Literal["bibtex", "ris", "csl", "zotero"]
ZoteroBackend = Literal["auto", "local", "connector", "web"]
Transport = Literal["stdio", "streamable-http"]

SourceChoice = choice_enum("SourceChoice", FulltextSource)

zotero_app = typer.Typer(help="Сохранение публикаций в Zotero (локальный API, коннектор или Web API).",
                         no_args_is_help=True)


# ---------------------------------------------------------------------------- resolve
def resolve_cmd(
    identifier: Annotated[str, typer.Argument(
        help="eLIBRARY ID, EDN, DOI, ISBN, SPIN, ORCID, ISSN, ROR или ссылка на страницу elibrary.ru")],
    fmt: Fmt = None,
) -> None:
    """Определить, на что указывает идентификатор (публикация, автор, журнал, организация)."""
    result = run(lambda c: c.resolve(identifier))
    if resolve_format(fmt) == "table":
        from elibrary_cli.cli.render import console, render_resolution

        render_resolution(result, console())
    else:
        emit(result, fmt)


# ---------------------------------------------------------------------------- full text
def fulltext_cmd(
    identifier: Annotated[str, typer.Argument(help="eLIBRARY ID, EDN, DOI или ссылка на item.asp")],
    offset: Annotated[int, typer.Option("--offset", min=0, help="С какого символа")] = 0,
    max_chars: Annotated[int, typer.Option("--max-chars", min=100, help="Сколько символов вывести")] = 20000,
    all_text: Annotated[bool, typer.Option("--all", help="Вывести весь текст целиком")] = False,
    out: Annotated[Path | None, typer.Option(
        "--out", "-o", help="Записать текст в файл (UTF-8)", show_default=False)] = None,
    source: Annotated[list[SourceChoice] | None, typer.Option(
        "--source", "-s", help="Источник (можно несколько раз, в порядке перебора): elibrary, cyberleninka, "
        "unpaywall, openalex, crossref, publisher")] = None,
    refresh_text: Annotated[bool, typer.Option(
        "--refresh-text", help="Не брать текст из локального кэша полных текстов")] = False,
    fmt: Annotated[Literal["text", "json"], typer.Option(
        "--format", "-f", help="text (по умолчанию) или json (текст и сведения об источнике)")] = "text",
) -> None:
    """Полный текст публикации: PDF eLIBRARY.RU (после входа) или открытые источники.

    Текст выдаётся частями по --max-chars; продолжение: --offset <next_offset> (из кэша, без запросов)."""
    limit = 10**9 if (all_text or out is not None) else max_chars
    result = run(lambda c: c.get_fulltext(identifier, offset=offset, max_chars=limit,
                                          sources=enum_values(source) or None, refresh=refresh_text))
    where = result.source + (f" ({result.source_url})" if result.source_url else "")
    end = result.offset + len(result.text)
    summary = (f"Источник: {where}; символы {result.offset}–{end} из {result.total_chars}"
               + ("; из кэша" if result.from_cache else ""))
    if out is not None:
        with handle_errors():
            path = write_file(out, result.text)
        echo_err(f"{summary}. Записано в {path}")
        if fmt == "json":
            emit(result, "json")
        return
    if fmt == "json":
        emit(result, "json")
    else:
        write_out(result.text)
    if result.next_offset is not None:
        summary += f". Продолжение: --offset {result.next_offset}"
    echo_err(summary)


# ---------------------------------------------------------------------------- export
def export_cmd(
    identifiers: Annotated[list[str], typer.Argument(help="eLIBRARY ID, EDN, DOI или ссылки (несколько через пробел)")],
    fmt: Annotated[ExportFormat, typer.Option(
        "--format", "-f", help="bibtex, ris, csl (CSL-JSON) или zotero (JSON для Zotero API)")] = "bibtex",
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Записать в файл", show_default=False)] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Тег для записей Zotero (можно несколько раз)")] = None,
    fix_caps: Annotated[bool, typer.Option(
        "--fix-caps/--keep-caps", help="Исправлять НАЗВАНИЯ ЗАГЛАВНЫМИ буквами")] = True,
    citekey_edn: Annotated[bool, typer.Option("--citekey-edn", help="Добавлять EDN к ключу BibTeX")] = False,
) -> None:
    """Экспорт библиографических записей (BibTeX, RIS, CSL-JSON, Zotero JSON)."""
    text = run(lambda c: c.export(identifiers, fmt, as_text=True, fix_caps=fix_caps, citekey_edn=citekey_edn,
                                  tags=tag or ()))
    if out is not None:
        with handle_errors():
            path = write_file(out, str(text))
        echo_err(f"Записей: {len(identifiers)}; записано в {path}")
    else:
        write_out(str(text))


# ---------------------------------------------------------------------------- zotero
@zotero_app.command("add")
def zotero_add_cmd(
    identifiers: Annotated[list[str], typer.Argument(help="eLIBRARY ID, EDN, DOI или ссылки (несколько через пробел)")],
    collection: Annotated[str | None, typer.Option(
        "--collection", "-c", help="Коллекция: ключ, название или путь «Папка/Подпапка»", show_default=False)] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Тег (можно несколько раз)")] = None,
    with_pdf: Annotated[bool, typer.Option("--with-pdf", help="Прикрепить PDF, если полный текст найден в PDF")] = False,
    dedupe: Annotated[bool, typer.Option(
        "--dedupe/--no-dedupe", help="Пропускать записи, которые уже есть в библиотеке")] = True,
    backend: Annotated[ZoteroBackend, typer.Option("--backend", help="Способ записи: auto, local, connector, web")] = "auto",
    fmt: Fmt = None,
) -> None:
    """Сохранить публикации в Zotero (нужен запущенный Zotero или ZOTERO_API_KEY + ZOTERO_LIBRARY_ID)."""
    explicit = None if backend == "auto" else backend
    _zotero_preflight(explicit)
    results = run(lambda c: c.zotero_add(identifiers, collection=collection, tags=tag or None, with_pdf=with_pdf,
                                         dedupe=dedupe, backend=explicit))
    failed = [r for r in results if r.get("status") == "failed"]
    from elibrary_cli.cli.render import render_zotero_results

    emit(results, fmt, render=render_zotero_results)
    if failed:
        echo_err(f"Не сохранено: {len(failed)} из {len(results)}")
        raise typer.Exit(2)


def _zotero_call(action: Any) -> Any:
    from elibrary_cli.cli import common

    with handle_errors():
        writer = common.zotero_factory(make_settings())
        try:
            return action(writer)
        finally:
            writer.close()


def _zotero_preflight(backend: str | None) -> None:
    """Fail before any card is fetched when Zotero cannot be written to (same check as the MCP tool).

    ``zotero add`` fetches one card per identifier (plus the full-text lookups of ``--with-pdf``) and
    only then writes; without this check an unreachable Zotero would cost those eLIBRARY.RU page views
    (and possibly a captcha) for an operation that cannot finish. ``ping()`` only probes the local
    Zotero (the Web API is judged by its configuration)."""
    from elibrary_cli.integrations.zotero import ZoteroUnavailable

    state = _zotero_call(lambda w: w.ping())
    available = list(state.get("available_backends") or [])
    if backend is None:
        ok = bool(available)
    else:
        # ZOTERO_LOCAL=false hides the local backends from ping(); an explicit one is left to add()
        local_off = (state.get("config") or {}).get("local") is False
        ok = backend in available or (backend != "web" and local_off)
    if ok:
        return
    hints = " ".join(dict.fromkeys(h for h in state.get("hints") or [] if h))
    head = (f"Способ записи в Zotero «{backend}» недоступен" if backend
            else "Нет доступного способа записи в Zotero")
    head += "; ничего не сохранено, запросов к eLIBRARY.RU не было."
    with handle_errors():
        raise ZoteroUnavailable(f"{head} {hints}".rstrip())


@zotero_app.command("status")
def zotero_status_cmd(fmt: Fmt = None) -> None:
    """Доступен ли Zotero и какими способами можно записывать (без секретов)."""
    result = _zotero_call(lambda w: w.ping())
    for hint in result.get("hints") or []:
        echo_err(f"Подсказка: {hint}")
    emit(result, fmt)


@zotero_app.command("authorize")
def zotero_authorize_cmd(
    app_name: Annotated[str | None, typer.Option("--app-name", help="Имя приложения в диалоге Zotero")] = None,
    fmt: Fmt = None,
) -> None:
    """Запросить ключ локального API Zotero 10+ (в Zotero появится диалог; выберите «Всегда разрешать»)."""
    echo_err("В Zotero появится запрос доступа: выберите «Всегда разрешать» (Always Allow).")
    emit(_zotero_call(lambda w: w.authorize_local(app_name)), fmt)


# ---------------------------------------------------------------------------- reference data
def refdata_cmd(
    name: Annotated[str | None, typer.Argument(
        help="Справочник: например countries, regions, rubrics, vak, journals.wos, authors.codetype "
        "(без аргумента — список справочников)")] = None,
    find: Annotated[str | None, typer.Option("--find", help="Найти значения по тексту", show_default=False)] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Сколько значений показать при --find")] = 20,
    fmt: Fmt = None,
) -> None:
    """Коды справочников форм поиска (страны, регионы, рубрики, фильтры журналов...). Без запросов к сайту."""
    from elibrary_cli import refdata

    with handle_errors():
        if name is None:
            result: Any = refdata.describe_options()
        elif find:
            result = refdata.find_options(name, find, limit=limit)
        else:
            result = refdata.list_options(name)
    if not result and name is not None:
        echo_err("Ничего не найдено.")
    emit(result, fmt)


# ---------------------------------------------------------------------------- MCP server
def mcp_cmd(
    transport: Annotated[Transport, typer.Option("--transport", help="stdio или streamable-http")] = "stdio",
    host: Annotated[str, typer.Option("--host", help="Адрес для streamable-http")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Порт для streamable-http")] = 8000,
) -> None:
    """Запустить MCP-сервер (то же, что elibrary-mcp)."""
    from elibrary_cli import mcp_server

    mcp_server.run(transport, host, port, settings=make_settings())
