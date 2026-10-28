"""Form builders for the author pages.

* ``POST /authors.asp`` (form ``results``, JS ``author_search()`` / ``goto_page(N)``). The server keeps
  no search state: every result page is the same POST with ``pagenum=N``.
* ``GET /author_profile.asp?id=ID``.
* ``author_items.asp``: the first page is a plain GET (``author_items.asp?authorid=ID&show_refs=1``,
  the link of the search results); further pages are ``POST /author_items.asp`` of the form
  ``results`` with ``pagenum=N`` (JS ``goto_page(N)``). The profile page also links filtered GET
  lists (``pubcat``, ``pubrole``, ``pubyear``), see :func:`author_items_params`.

Field order is the DOM order of the form (that is what ``FormData`` / a browser submit sends),
checked against ``tests/fixtures/html/authors_form.html`` and ``author_items_3771.html``.
Checkboxes are sent as ``on`` only when set, at their DOM position.
"""

from __future__ import annotations

from elibrary_cli.models.authors import (
    AUTHOR_ITEMS_SORT_CODES,
    AUTHOR_SORT_CODES,
    METRICS_CODES,
    ORDER_CODES,
    AuthorPublicationsQuery,
    AuthorQuery,
)
from elibrary_cli.models.base import BASE_URL

AUTHORS_PATH = "/authors.asp"
AUTHOR_PROFILE_PATH = "/author_profile.asp"
AUTHOR_ITEMS_PATH = "/author_items.asp"

Form = list[tuple[str, str]]


def _s(value: object) -> str:
    return "" if value is None else str(value)


def build_author_search_form(q: AuthorQuery, *, page: int | None = None) -> Form:
    """Ordered form fields of ``POST /authors.asp`` for ``q`` (page ``page`` or ``q.page``).

    The first page sends ``pagenum=""`` like ``author_search()``; later pages send the number
    like ``goto_page(N)``."""
    pnum = page if page is not None else q.page
    form: Form = [
        ("authors_all", ""),
        ("pagenum", str(pnum) if pnum and pnum > 1 else ""),
        ("authorbox_name", ""),
        ("selid", ""),
        ("orgid", _s(q.org_id)),
        ("orgadminid", ""),
        ("townid", _s(q.town_id)),
        ("regionid", _s(q.region_id)),
        ("surname", _s(q.surname)),
        ("codetype", q.code_type),
        ("codevalue", _s(q.code_value)),
        ("countryid", _s(q.country)),
        ("town", _s(q.town)),
    ]
    if q.in_affiliation:
        form.append(("inaffil", "on"))
    form += [
        ("orgname", _s(q.org_name)),
        ("authorboxid", _s(q.author_group)),
    ]
    if q.rubric_in_profile:
        form.append(("rubricinreg", "on"))
    form += [
        ("rubriccode", _s(q.rubric_code)),
        ("metrics", METRICS_CODES[q.metrics]),
    ]
    if q.only_with_publications:
        form.append(("show_nopubl", "on"))
    form += [
        ("sortorder", AUTHOR_SORT_CODES[q.sort]),
        ("order", ORDER_CODES[q.order]),
    ]
    return form


def author_profile_params(author_id: int) -> Form:
    """Query string of ``GET /author_profile.asp``."""
    return [("id", str(int(author_id)))]


def author_profile_url(author_id: int) -> str:
    return f"{BASE_URL}{AUTHOR_PROFILE_PATH}?id={int(author_id)}"


def author_items_params(
    author_id: int,
    *,
    show_refs: bool = True,
    pubcat: str | None = None,
    pubrole: int | None = None,
    pubyear: int | str | list[int] | None = None,
) -> Form:
    """Query string of ``GET /author_items.asp`` (first page of a publication list).

    ``pubcat`` (``risc``, ``corerisc``, ``vak``), ``pubrole`` (``100`` author, ``0`` editor, ``26``
    supervisor, ``27`` consultant) and ``pubyear`` (year or ``|``-joined years) are the filters used
    by the links of the profile page."""
    params: Form = [("authorid", str(int(author_id)))]
    if pubyear is not None:
        years = "|".join(str(y) for y in pubyear) if isinstance(pubyear, list) else str(pubyear)
        params.append(("pubyear", years))
    if pubcat:
        params.append(("pubcat", pubcat))
    params.append(("show_refs", "1" if show_refs else "0"))
    if pubrole is not None:
        params.append(("pubrole", str(pubrole)))
    return params


def author_items_url(author_id: int, *, show_refs: bool = True) -> str:
    """The link used by the search results: ``author_items.asp?authorid=ID&show_refs=1``."""
    return f"{BASE_URL}{AUTHOR_ITEMS_PATH}?authorid={int(author_id)}&show_refs={1 if show_refs else 0}"


def build_author_items_form(
    author_id: int,
    page: int = 1,
    *,
    sort: str = "date",
    order: str = "desc",
    include_references: bool = True,
    merge_versions: bool = True,
    unlinked: bool = False,
    author_hash: str = "",
) -> Form:
    """Ordered form fields of ``POST /author_items.asp`` (form ``results``, ``goto_page(N)`` /
    ``pub_search()``).

    ``author_hash`` is the hidden ``authorhash`` of the page ('Петров В А'); the browser sends it,
    the server identifies the author by ``authorid``. Facet selections (rubrics, journals, years ...)
    are loaded by AJAX into the form and are not supported here. The per-row checkboxes
    (``tid<ID>``/``pid<ID>``) are unchecked and therefore not sent."""
    if sort not in AUTHOR_ITEMS_SORT_CODES:
        raise ValueError(f"неизвестная сортировка списка публикаций: {sort!r}")
    if order not in ORDER_CODES:
        raise ValueError(f"неизвестный порядок сортировки: {order!r}")
    form: Form = [
        ("itembox_name", ""),
        ("auth", ""),
        ("authorid", str(int(author_id))),
        ("authorhash", author_hash or ""),
        ("pagenum", str(max(1, int(page)))),
        ("add_all", ""),
        ("paysum", ""),
        ("slight", ""),
        ("show_refs", "1" if include_references else "0"),
        ("hide_doubles", "1" if merge_versions else "0"),
        ("items_all", ""),
        ("publs_all", ""),
        ("did", "1"),
        ("urlnum", "1"),
        ("listid", ""),
        # facet sort selects, defaults as selected on the page
        ("rubric_order", "0"),
        ("title_order", "0"),
        ("org_order", "0"),
        ("author_order", "0"),
        ("year_order", "1"),
        ("type_order", "0"),
        ("cat_order", "1"),
        ("role_order", "0"),
        ("keyword_order", "0"),
        ("show_hash", "1" if unlinked else "0"),
    ]
    if include_references:
        form.append(("check_show_refs", "on"))
    if merge_versions:
        form.append(("check_hide_doubles", "on"))
    form += [
        ("sortorder", AUTHOR_ITEMS_SORT_CODES[sort]),
        ("order", ORDER_CODES[order]),
    ]
    return form


def build_author_publications_form(q: AuthorPublicationsQuery, *, author_hash: str = "") -> Form:
    """:func:`build_author_items_form` for an :class:`AuthorPublicationsQuery`."""
    return build_author_items_form(
        q.author_id,
        q.page,
        sort=q.sort,
        order=q.order,
        include_references=q.include_references,
        merge_versions=q.merge_versions,
        unlinked=q.unlinked,
        author_hash=author_hash,
    )
