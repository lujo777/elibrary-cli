"""Command line interface ``elibrary`` (typer).

Command tree::

    elibrary [--offline] [--refresh] [--min-delay S] [--version]
        search  articles|patents|books|dissertations|conferences|publications|authors|orgs|journals
        get     publication|author|author-pubs|org|org-pubs|journal|issue
        resolve <identifier>
        fulltext <id|EDN|DOI>
        export  <id...> --format bibtex|ris|csl|zotero
        zotero  add|status|authorize
        auth    status|check|cookies|captcha|login|logout|clear
        cache   stats|clear
        refdata [<name>]
        mcp     [--transport stdio|streamable-http]

Entry point: :func:`main` (``elibrary`` and ``elibrary-cli`` console scripts).
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from elibrary_cli import __version__
from elibrary_cli.cli import common
from elibrary_cli.cli.get import get_app
from elibrary_cli.cli.search import search_app
from elibrary_cli.cli.session import auth_app, cache_app
from elibrary_cli.cli.tools import export_cmd, fulltext_cmd, mcp_cmd, refdata_cmd, resolve_cmd, zotero_app

__all__ = ["app", "main"]

app = typer.Typer(
    name="elibrary",
    help=(
        "eLIBRARY.RU (Научная электронная библиотека, РИНЦ) из командной строки: поиск публикаций, авторов, "
        "организаций и журналов, карточки, полные тексты, экспорт в BibTeX/RIS/CSL и Zotero. "
        "Сайт показывает капчу после ~10 быстрых запросов: CLI делает паузы, кэширует страницы и "
        "останавливается на первой проверке (см. `elibrary auth captcha`)."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"elibrary-cli {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    offline: Annotated[bool, typer.Option(
        "--offline", help="Не обращаться к сайту: только кэш (ELIBRARY_OFFLINE=1)")] = False,
    refresh: Annotated[bool, typer.Option(
        "--refresh", help="Не брать страницы из кэша (ELIBRARY_REFRESH=1)")] = False,
    min_delay: Annotated[float | None, typer.Option(
        "--min-delay", min=0, help="Пауза между запросами к сайту, секунд (ELIBRARY_MIN_DELAY, по умолчанию 5)",
        show_default=False)] = None,
    version: Annotated[bool | None, typer.Option(
        "--version", callback=_version, is_eager=True, help="Показать версию и выйти")] = None,
) -> None:
    """Корневые параметры действуют на все команды."""
    common.set_options(offline=offline, refresh=refresh, min_delay=min_delay)


app.add_typer(search_app, name="search")
app.add_typer(get_app, name="get")
app.command("resolve")(resolve_cmd)
app.command("fulltext")(fulltext_cmd)
app.command("export")(export_cmd)
app.add_typer(zotero_app, name="zotero")
app.add_typer(auth_app, name="auth")
app.add_typer(cache_app, name="cache")
app.command("refdata")(refdata_cmd)
app.command("mcp")(mcp_cmd)


def main() -> None:
    """Console-script entry point."""
    common.ensure_utf8(sys.stdout)
    common.ensure_utf8(sys.stderr)
    app()
