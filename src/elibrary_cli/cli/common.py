"""Shared plumbing of the ``elibrary`` commands: global options, the client factory, error
mapping to exit codes and the JSON/JSONL output.

Conventions (see ``docs/re/cli_mcp.md``):

* data goes to stdout, messages (errors, hints, progress) to stderr;
* ``--format/-f`` is ``table``, ``json`` or ``jsonl``; without it the output is a table when stdout
  is a terminal and JSON otherwise (pipes, files, agents);
* exit codes: 0 OK, 1 invalid arguments / missing optional dependency, 2 eLIBRARY.RU error
  (captcha, not found, login required, rate limit ...), 3 network error, 130 interrupted.

Tests replace :data:`client_factory` (and :data:`zotero_factory`) with factories that build
clients over a fake site; nothing here talks to the network by itself.
"""

from __future__ import annotations

import contextlib
import enum
import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar, get_args

import typer
from pydantic import BaseModel, ValidationError

from elibrary_cli.config import Settings
from elibrary_cli.errors import CaptchaRequired, ElibraryError

if TYPE_CHECKING:  # pragma: no cover
    from elibrary_cli.client import ElibraryClient
    from elibrary_cli.integrations.zotero import ZoteroWriter

T = TypeVar("T")

OutputFormat = Literal["table", "json", "jsonl"]

#: Exit code for invalid arguments (``ValueError`` / pydantic validation errors).
EXIT_USAGE = 1
#: Exit code after Ctrl+C.
EXIT_INTERRUPTED = 130

FORMAT_HELP = "Формат вывода: table, json или jsonl (по умолчанию table в терминале, json в конвейере)"
PAGE_HELP = "Номер страницы результатов"


@dataclass
class GlobalOptions:
    """Options of the root command (``elibrary --offline --refresh --min-delay 8 ...``)."""

    offline: bool = False
    refresh: bool = False
    min_delay: float | None = None


#: Options of the current invocation (set by the root callback before every command).
OPTIONS = GlobalOptions()


def set_options(*, offline: bool = False, refresh: bool = False, min_delay: float | None = None) -> None:
    """Store the root options for the commands of this invocation."""
    OPTIONS.offline = bool(offline)
    OPTIONS.refresh = bool(refresh)
    OPTIONS.min_delay = None if min_delay is None else max(0.0, float(min_delay))


def apply_options(settings: Settings) -> Settings:
    """Apply the root options that were given explicitly to ``settings`` (in place)."""
    if OPTIONS.offline:
        settings.offline = True
    if OPTIONS.refresh:
        settings.refresh = True
    if OPTIONS.min_delay is not None:
        settings.min_delay = OPTIONS.min_delay
    return settings


def make_settings() -> Settings:
    """``Settings.from_env()`` with the root options applied."""
    return apply_options(Settings.from_env())


def _default_client_factory(settings: Settings) -> ElibraryClient:
    from elibrary_cli.client import ElibraryClient

    return ElibraryClient(settings)


def _default_zotero_factory(settings: Settings) -> ZoteroWriter:
    from elibrary_cli.integrations.zotero import ZoteroWriter

    return ZoteroWriter(settings=settings)


#: Builds the client of one command. Replaced in tests.
client_factory: Callable[[Settings], ElibraryClient] = _default_client_factory
#: Builds the Zotero writer of ``elibrary zotero status|authorize``. Replaced in tests.
zotero_factory: Callable[[Settings], ZoteroWriter] = _default_zotero_factory


@contextlib.contextmanager
def open_client() -> Iterator[ElibraryClient]:
    """A client for one command (closed afterwards); the root options are applied to it."""
    client = client_factory(make_settings())
    for s in {id(client.settings): client.settings, id(client.http.settings): client.http.settings}.values():
        apply_options(s)
    try:
        yield client
    finally:
        client.close()


def choice_enum(name: str, literal: Any) -> type[enum.Enum]:
    """A ``str`` Enum with the values of a ``Literal`` (typer accepts ``list[Enum]`` options, not
    ``list[Literal]``)."""
    return enum.Enum(name, [(v, v) for v in get_args(literal)], type=str)  # type: ignore[return-value]


def enum_values(values: Iterable[Any] | None) -> list[str]:
    """Plain values of Enum members (or strings)."""
    return [getattr(v, "value", v) for v in values or []]


# ---------------------------------------------------------------------------- messages and errors
def echo_err(message: str) -> None:
    """A human message to stderr (no markup interpretation)."""
    typer.echo(message, err=True)


def validation_message(exc: ValidationError) -> str:
    """A compact Russian rendering of a pydantic validation error."""
    parts = []
    for err in exc.errors():
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "Неверные параметры: " + "; ".join(parts)


def error_exit(exc: BaseException) -> typer.Exit:
    """Print ``exc`` to stderr and return the ``typer.Exit`` with its exit code."""
    if isinstance(exc, CaptchaRequired):
        echo_err(str(exc))
        return typer.Exit(exc.exit_code)
    if isinstance(exc, ElibraryError):
        echo_err(f"Ошибка: {exc}")
        return typer.Exit(exc.exit_code)
    if isinstance(exc, ValidationError):
        echo_err(validation_message(exc))
        return typer.Exit(EXIT_USAGE)
    if isinstance(exc, ValueError):
        echo_err(f"Неверные параметры: {exc}{target_hint(exc)}")
        return typer.Exit(EXIT_USAGE)
    if isinstance(exc, KeyboardInterrupt):
        echo_err("Прервано.")
        return typer.Exit(EXIT_INTERRUPTED)
    raise exc  # pragma: no cover - only expected errors are passed here


@contextlib.contextmanager
def handle_errors() -> Iterator[None]:
    """Map the library errors raised inside the block to messages and exit codes."""
    try:
        yield
    except (typer.Exit, typer.Abort):
        raise
    except (ElibraryError, ValueError, KeyboardInterrupt) as exc:
        raise error_exit(exc) from None


def run(action: Callable[[ElibraryClient], T]) -> T:
    """Open a client, run ``action(client)``, close the client; errors become exit codes."""
    with handle_errors(), open_client() as client:
        return action(client)


# ---------------------------------------------------------------------------- output
def ensure_utf8(stream: Any) -> None:
    """Switch a redirected stream to UTF-8 (Windows consoles default to cp1251/cp866)."""
    try:
        if stream.isatty():
            return
    except (AttributeError, ValueError):
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(Exception):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            reconfigure(encoding="utf-8", errors="replace")


def resolve_format(fmt: str | None) -> OutputFormat:
    """``fmt`` or the default: ``table`` for a terminal, ``json`` otherwise."""
    if fmt:
        return fmt  # type: ignore[return-value]
    try:
        return "table" if sys.stdout.isatty() else "json"
    except (AttributeError, ValueError):
        return "json"


def to_jsonable(obj: Any) -> Any:
    """Models -> dicts (``mode="json"``), recursively through lists and dicts."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def dumps(obj: Any, *, indent: int | None = 2) -> str:
    return json.dumps(to_jsonable(obj), ensure_ascii=False, indent=indent, default=str)


def jsonl_rows(obj: Any) -> list[Any]:
    """Rows of a JSONL output: the ``items`` of a result page, the elements of a list, or ``obj``."""
    if isinstance(obj, (list, tuple)):
        return list(obj)
    items = getattr(obj, "items", None)
    if isinstance(obj, BaseModel) and isinstance(items, list):
        return items
    return [obj]


def write_out(text: str) -> None:
    """Write data to stdout (UTF-8 when redirected)."""
    ensure_utf8(sys.stdout)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    with contextlib.suppress(Exception):
        sys.stdout.flush()


def emit(obj: Any, fmt: str | None, *, render: Callable[[Any], None] | None = None) -> None:
    """Print ``obj`` in the requested format; ``render`` overrides the table renderer."""
    fmt = resolve_format(fmt)
    if fmt == "json":
        write_out(dumps(obj))
    elif fmt == "jsonl":
        rows = jsonl_rows(obj)
        if rows:  # no rows -> no output at all: a blank line is not a valid JSONL record
            write_out("\n".join(dumps(row, indent=None) for row in rows))
        else:
            with contextlib.suppress(Exception):
                sys.stdout.flush()
    else:
        if render is None:
            from elibrary_cli.cli.render import render_table

            render = render_table
        ensure_utf8(sys.stdout)
        render(obj)


def read_value(value: str) -> str:
    """``@path`` -> the file's text, ``-`` -> stdin, anything else as is (cookie strings ...)."""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@") and len(value) > 1:
        path = Path(value[1:]).expanduser()
        try:
            return path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"Не удалось прочитать файл {path}: {exc}") from exc
    return value


def write_file(path: Path, text: str) -> Path:
    """Write ``text`` (UTF-8) to ``path``, creating the parent directory."""
    path = path.expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Не удалось записать файл {path}: {exc}") from exc
    return path


# ---------------------------------------------------------------------------- identifiers
#: The command that shows each kind of object (hint of a ``WrongTarget`` error).
TARGET_COMMANDS = {
    "publication": "elibrary get publication",
    "author": "elibrary get author",
    "organization": "elibrary get org",
    "journal": "elibrary get journal",
    "issue": "elibrary get issue --issue-id",
}


#: The command that searches each kind of object by name (hint for a word read as an EDN).
SEARCH_COMMANDS = {
    "author": "elibrary search authors",
    "organization": "elibrary search orgs",
    "journal": "elibrary search journals",
    "issue": "elibrary search journals",
}
#: Identifier kinds that can only point to a publication (refused locally for other targets).
_PUBLICATION_ONLY_KINDS = frozenset({"edn", "doi", "isbn"})
_BARE_EDN_RE = re.compile(r"^[A-Za-z]{6}$")


def target_hint(exc: BaseException) -> str:
    """`` (используйте `elibrary get author`)`` for a ``client.WrongTarget`` that points to a known
    kind of object, ``""`` otherwise; plus the search hint of a word read as an EDN."""
    from elibrary_cli.client import WrongTarget

    if isinstance(exc, WrongTarget) and exc.target in TARGET_COMMANDS:
        hint = f" (используйте `{TARGET_COMMANDS[exc.target]}`)"
        search = getattr(exc, "search_hint", None)
        return f"{hint}. {search}" if search else hint
    return ""


def entity_id(client: ElibraryClient, identifier: str | int, target: str) -> int:
    """The eLIBRARY id of an author / organization / journal / issue from a number, an elibrary.ru
    URL, a prefixed id (``author:3771``) or an identifier the site resolves (SPIN, ORCID, ISSN, ROR).

    A bare number costs no request; SPIN/ORCID/ISSN/ROR cost one (cached) lookup. An identifier of
    another kind of object raises ``client.WrongTarget``; EDN, DOI and ISBN can only name a
    publication, so they are refused without a request (a 6-letter word such as ``Ivanov`` parses as
    an EDN: the error then suggests the search command). Publications: ``client.get_publication_any``."""
    from elibrary_cli.client import WrongTarget, parse_identifier

    text = str(identifier).strip()
    if text.isdigit():
        if int(text) <= 0:
            raise ValueError(f"Идентификатор должен быть положительным числом, получено {identifier!r}")
        return int(text)
    ident = parse_identifier(text)
    if ident["kind"] in ("elibrary_id", "url"):
        if ident["target"] != target or not ident["id"]:
            raise WrongTarget(text, ident["target"], ident["id"], expected=target)
        return int(ident["id"])
    if ident["kind"] in _PUBLICATION_ONLY_KINDS and target != "publication":
        exc = WrongTarget(text, "publication", expected=target)
        if ident["kind"] == "edn" and _BARE_EDN_RE.match(text) and target in SEARCH_COMMANDS:
            what = "фамилия" if target == "author" else "название"
            exc.search_hint = (  # type: ignore[attr-defined]
                f"Шесть латинских букв читаются как EDN публикации; если {text!r} — {what}, "
                f"используйте поиск: `{SEARCH_COMMANDS[target]} {text}`")
        raise exc
    res = client.resolve(text)
    if res.get("target") != target or not res.get("id"):
        raise WrongTarget(text, res.get("target"), res.get("id"), expected=target)
    return int(res["id"])
