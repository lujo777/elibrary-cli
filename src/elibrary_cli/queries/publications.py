"""Form builder for the extended publication search (``querybox.asp`` -> ``query_results.asp``).

The field order is the DOM order of ``form[name=results]`` on ``querybox.asp`` (2026-10), which is the
order ``FormData`` (and therefore the browser) uses::

    querybox_name authors_all titles_all rubrics_all changed queryid ftext
    where_name where_affiliation where_abstract where_fulltext where_keywords where_references
    type_article type_disser type_book type_report type_conf type_patent type_preprint type_grant
    type_dataset search_itemboxid search_morph search_freetext search_fulltext search_open
    search_results begin_year end_year issues orderby order queryboxid save_queryboxid

Unchecked checkboxes are not sent, checked ones are sent as ``on``. The empty multi-selects
``rubrics``/``authors``/``titles`` are not sent (their values travel in the ``*_all`` hidden fields,
filled by the page's ``query_message()``). ``changed=1`` makes the server run a new query instead of
re-showing the session's last one. Result pages 2..N are fetched with
``GET /query_results.asp?pagenum=N`` in the same session (see :func:`page_params`).
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlencode

from elibrary_cli.models.publications import ALL_TYPES, ALL_WHERE, DEFAULT_WHERE, PublicationQuery

SEARCH_PATH = "/query_results.asp"
FORM_PATH = "/querybox.asp"
PER_PAGE = 100

# Type presets for client/CLI shortcuts (values of PublicationQuery.types).
ARTICLE_TYPES: tuple[str, ...] = ("article",)
DISSERTATION_TYPES: tuple[str, ...] = ("disser",)
BOOK_TYPES: tuple[str, ...] = ("book",)
REPORT_TYPES: tuple[str, ...] = ("report",)
CONFERENCE_TYPES: tuple[str, ...] = ("conf",)
PATENT_TYPES: tuple[str, ...] = ("patent",)
PREPRINT_TYPES: tuple[str, ...] = ("preprint",)
GRANT_TYPES: tuple[str, ...] = ("grant",)
DATASET_TYPES: tuple[str, ...] = ("dataset",)
#: Journal articles + conference materials: what most people mean by "papers".
PAPER_TYPES: tuple[str, ...] = ("article", "conf")
ALL_PUBLICATION_TYPES: tuple[str, ...] = ALL_TYPES

# DOM order of the checkboxes on querybox.asp (differs from the visual reading order).
_WHERE_ORDER = ("name", "affiliation", "abstract", "fulltext", "keywords")
_TYPE_ORDER = ("article", "disser", "book", "report", "conf", "patent", "preprint", "grant", "dataset")
_ORDER_MAP = {"desc": "rev", "asc": "nrm"}

__all__ = [
    "ALL_PUBLICATION_TYPES",
    "ALL_TYPES",
    "ALL_WHERE",
    "ARTICLE_TYPES",
    "BOOK_TYPES",
    "CONFERENCE_TYPES",
    "DATASET_TYPES",
    "DEFAULT_WHERE",
    "DISSERTATION_TYPES",
    "FORM_PATH",
    "GRANT_TYPES",
    "PAPER_TYPES",
    "PATENT_TYPES",
    "PER_PAGE",
    "PREPRINT_TYPES",
    "REPORT_TYPES",
    "SEARCH_PATH",
    "build_search_form",
    "effective_where",
    "normalize_author_name",
    "page_params",
    "search_signature",
]


def normalize_author_name(name: str) -> str:
    """``"Петров В.А."`` -> ``"Петров В А"`` (the form keeps initials without dots)."""
    return re.sub(r"\s+", " ", name.replace(".", " ").replace("|", " ")).strip()


def effective_where(q: PublicationQuery) -> list[str]:
    """Search fields actually checked: ticking "fulltext" ticks all the others on the site."""
    where = set(q.where)
    if "fulltext" in where:
        where |= {"name", "abstract", "keywords", "affiliation"}
    return [w for w in _WHERE_ORDER if w in where]


def build_search_form(q: PublicationQuery) -> list[tuple[str, str]]:
    """Ordered ``(name, value)`` pairs of ``POST /query_results.asp`` for ``q`` (page 1).

    ``q.page`` is ignored here: further pages are requested with :func:`page_params`.
    """
    authors_all = "".join(f"{normalize_author_name(a)}|" for a in q.author_names if normalize_author_name(a))
    titles_all = "".join(f"{int(t)}|" for t in q.journal_ids)
    rubrics_all = "".join(f"{str(r).strip()}|" for r in q.rubric_codes if str(r).strip())

    form: list[tuple[str, str]] = [
        ("querybox_name", ""),
        ("authors_all", authors_all),
        ("titles_all", titles_all),
        ("rubrics_all", rubrics_all),
        ("changed", "1"),
        ("queryid", ""),
        ("ftext", q.text or ""),
    ]
    where = set(effective_where(q))
    for w in _WHERE_ORDER:
        if w in where:
            form.append((f"where_{w}", "on"))
        if w == "keywords":
            form.append(("where_references", ""))
    types = set(q.types)
    form.extend((f"type_{t}", "on") for t in _TYPE_ORDER if t in types)
    form.append(("search_itemboxid", str(q.itembox_id) if q.itembox_id else ""))
    for name, flag in (
        ("search_morph", q.morphology),
        ("search_freetext", q.fuzzy),
        ("search_fulltext", q.fulltext_only),
        ("search_open", q.open_only),
        ("search_results", q.in_previous_results),
    ):
        if flag:
            form.append((name, "on"))
    form.extend([
        ("begin_year", str(q.year_from or 0)),
        ("end_year", str(q.year_to or 0)),
        ("issues", q.period),
        ("orderby", q.sort),
        ("order", _ORDER_MAP[q.order]),
        ("queryboxid", "0"),
        ("save_queryboxid", "0"),
    ])
    return form


def page_params(page: int) -> list[tuple[str, str]]:
    """GET parameters of result page ``page`` of the session's current query."""
    if page < 1:
        raise ValueError("Номер страницы должен быть не меньше 1")
    return [("pagenum", str(page))]


def search_signature(q: PublicationQuery) -> str:
    """Stable hash of the search form (without the page number).

    The client remembers the signature of the query the server session currently holds; when it
    matches, page N can be fetched with ``pagenum=N`` without re-running the query.
    """
    payload = urlencode(build_search_form(q))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
