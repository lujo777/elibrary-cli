# Development guide (contracts for contributors)

This document fixes the architecture and the conventions of `elibrary-cli`. Read it before changing code.

## Layers

```
cli/ (typer)          mcp_server.py (MCPServer)        <- thin presentation layers, no parsing
          \              /
           client.py  (ElibraryClient facade: one method per user action)
          /      |       \
 queries/*.py   parsers/*.py   fulltext/, exports.py, integrations/zotero.py, auth.py
 (build form     (pure HTML ->
  params)         pydantic models)
          \      |
           http.py (ElibraryHTTP: throttle, budgets, circuit breaker, cache, cookies)
```

* `http.py` is the **only** module that talks to elibrary.ru. Never create another client for elibrary.ru.
  External sites (Crossref, OpenAlex, Unpaywall, CyberLeninka, publishers) use `fulltext/net.py`.
* Parsers are **pure functions** `parse_xxx(html: str | bytes, *, url: str | None = None, ...) -> Model`.
  They never fetch. They take raw page text (use `parsers.common.parse_html`) and return models.
  They raise `errors.ParseError` when the page is not the expected page (e.g. no title / no result table
  where one is required) and return empty results when the page legitimately has none.
* Query builders (`queries/*.py`) turn a pydantic query model into the ordered list of `(name, value)` form
  fields exactly as the browser would send them (see `docs/reverse-engineering.md`).
* `client.py` combines builders + `http` + parsers; it owns the server-side search-session logic.

## Files and ownership

| Area | Files |
|---|---|
| Core (do not change without need) | `config.py`, `errors.py`, `http.py`, `models/base.py`, `parsers/common.py` |
| Publications (articles, patents, books, dissertations, conference papers...) | `models/publications.py`, `queries/publications.py`, `parsers/search.py`, `parsers/item.py`, `parsers/rows.py` |
| Authors | `models/authors.py`, `queries/authors.py`, `parsers/authors.py` |
| Organizations | `models/organizations.py`, `queries/organizations.py`, `parsers/organizations.py` |
| Journals | `models/journals.py`, `queries/journals.py`, `parsers/journals.py` |
| Reference data (select options from the forms) | `data/*.json`, `refdata.py` |
| Full text | `fulltext/*.py` |
| Export + Zotero | `exports.py`, `integrations/zotero.py` |
| Session / auth | `auth.py` |
| Facade, CLI, MCP | `client.py`, `cli/*.py`, `mcp_server.py` |

Each area has its own tests: `tests/test_<area>.py`. `models/__init__.py` and `parsers/__init__.py` are
updated only by the integration step.

## Conventions

* Python >= 3.10, `from __future__ import annotations`, type hints everywhere, ruff clean (`ruff check`).
* Code, comments and docstrings in English. **User-facing text** (CLI help, error messages, table headers)
  in Russian.
* Models: subclass `models.base.ElibModel`; every field optional unless it is always present; numbers as
  `int | None` / `float | None`; booleans for "да/нет"; keep unknown label/value pairs in an
  `extra: dict[str, str]` field so nothing shown on the page is lost. Add `Field(description=...)` for
  non-obvious fields: the descriptions become the MCP output schema.
* Every model that corresponds to a page has `id`, `url` and (if applicable) `edn`, `doi`.
* Lists use `models.base.SearchPage[T]` with `total`, `total_in_database`, `page`, `per_page`, `pages`.
* Publication rows anywhere (search, author/org lists, issue contents) are parsed with
  `parsers.rows.parse_publication_rows` into `models.base.PublicationHit`.
* Errors: raise the classes in `errors.py` (`NotFound`, `ParseError`, `LoginRequired`, ...). Never return
  error strings. The CLI maps them to exit codes, the MCP server to `ToolError`.
* No `print()` outside `cli/`. Use `logging.getLogger(__name__)`.
* Lexbor quirk: `node.css(sel)` / `node.css_first(sel)` also match the node itself, so
  `td.css_first("td")` is never `None` and `tr.css("td")` also returns the cells of tables nested in the
  row. Compare `mem_id` or iterate direct children (`node.iter()`) when only descendants / own cells count.

## Tests

* Fixtures are real pages saved from elibrary.ru in 2026-10: `tests/fixtures/html/*.html` (raw bytes,
  UTF-8). Load with `tests.conftest.fixture_bytes(name)`.
* `tests/conftest.py` provides `settings` (temp dirs, no delays), `site` (a fake elibrary that serves
  fixtures, records requests) and `http` (an `ElibraryHTTP` over that fake site).
* **Never send requests to elibrary.ru from tests or while developing** (the site shows a captcha after
  ~10 page views per session). Live checks are done by the maintainer, by hand, with `-m live`.
  `tests/conftest.py` enforces this: an autouse session fixture makes every non-loopback connection and
  DNS lookup fail (`ELIBRARY_LIVE=1` lifts it). Use `httpx2.MockTransport` (`site` / `http` fixtures).
* Do not write to a real Zotero library from tests; mock HTTP.
