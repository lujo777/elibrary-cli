"""Request builders for the organization pages.

``orgs.asp`` (verified against ``tests/fixtures/html/orgs_form.html`` and a live request, 2026-10-13):
the form ``results`` (``<FORM method=post name="results" action="orgs.asp">``) holds, in document order,
``pagenum`` (hidden), ``orgname`` (text), ``town``, ``regionid``, ``countryid``, ``sortorder``, ``order``
(selects). JS ``org_search()`` sets ``pagenum=""`` and submits; ``goto_page(N)`` sets ``pagenum=N`` and
submits the same form, so pagination is stateless (every page re-sends the whole query). The live
request body for the fixture ``orgs_results_mgu.html`` was::

    pagenum=&orgname=московский государственный университет&town=&regionid=0&countryid=&sortorder=3&order=1

``org_items.asp`` (no fixture): the list is opened by a plain link ``org_items.asp?orgsid=ID``; the
browser pages through it by POSTing the form ``results`` (``goto_page``). Classic ASP reads ``Request()``
from the query string and the body alike, and third-party scrapers page with
``GET org_items.asp?orgsid=ID&pagenum=N``, so :func:`build_org_items_request` uses GET. Best guess, see
``docs/re/organizations.md``.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any, Literal, NamedTuple

from elibrary_cli.models.organizations import OrgQuery

ORGS_PATH = "/orgs.asp"
ORG_PROFILE_PATH = "/org_profile.asp"
ORG_ABOUT_PATH = "/org_about.asp"
ORG_ITEMS_PATH = "/org_items.asp"
ORG_CITING_ITEMS_PATH = "/cit_org_items.asp"

#: Rows per page of orgs.asp and org_items.asp.
ORGS_PER_PAGE = 20
ORG_ITEMS_PER_PAGE = 100

#: ``sortorder`` values of orgs.asp (2 is not offered by the form).
ORG_SORT_CODES: dict[str, str] = {"name": "0", "town": "1", "publications": "3", "citations": "4"}
#: ``order`` values: 0 ascending, 1 descending.
ORDER_CODES: dict[str, str] = {"asc": "0", "desc": "1"}

#: ``show_option`` values of org_items.asp (from the wayback snapshot and the profile links).
ORG_ITEMS_SHOW_OPTIONS: dict[str, str] = {
    "1": "все публикации организации на портале elibrary.ru",
    "0": "публикации организации, включенные в РИНЦ",
    "2": "публикации организации, входящие в ядро РИНЦ",
    "5": "статьи в российских журналах, включенных в текущий перечень ВАК",
    "3": "статьи в российских журналах, входящих в Russian Science Citation Index",
    "9": "статьи в журналах, входящих в Web of Science Core Collection",
    "10": "статьи в журналах, входящих в Scopus",
    "4": "статьи в журналах, входящих в Web of Science или Scopus",
    "6": "статьи в журналах с импакт-фактором РИНЦ >0.02",
    "7": "статьи в журналах с импакт-фактором РИНЦ >0.1",
    "8": "статьи в журналах с импакт-фактором РИНЦ >0.3",
    "11": "публикации, имеющие DOI",
    "12": "публикации, имеющие ISBN",
}
#: ``show_sotr`` values of org_items.asp.
ORG_ITEMS_SHOW_SOTR: dict[str, str] = {
    "0": "все публикации, в которых явно указана данная организация",
    "1": "публикации сотрудников в период их работы в организации",
    "2": "все публикации нынешних сотрудников организации",
}
#: ``sortorder`` values of org_items.asp.
ORG_ITEMS_SORT_CODES: dict[str, str] = {
    "date": "0",  # по дате выпуска
    "title": "1",  # по названию публикации
    "year_author": "2",  # по году и первому автору
    "author": "5",  # по первому автору
    "cited": "3",  # по числу цитирований
    "insdate": "4",  # по дате размещения
}

Method = Literal["GET", "POST"]
FormFields = list[tuple[str, str]]


class RequestSpec(NamedTuple):
    """What to send: ``http.get(path, params)`` for GET, ``http.post(path, params)`` for POST."""

    method: Method
    path: str
    params: FormFields


# --------------------------------------------------------------------------- reference data


@lru_cache(maxsize=1)
def load_org_options() -> dict[str, Any]:
    """Select options of orgs.asp shipped in ``data/orgs_options.json`` (countries, regions, sorts)."""
    text = resources.files("elibrary_cli.data").joinpath("orgs_options.json").read_text(encoding="utf-8")
    return json.loads(text)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("ё", "е").replace("Ё", "Е")).strip().casefold()


def resolve_country(value: str) -> str:
    """``"RUS"``/``"rus"`` -> ``"RUS"``; ``"Россия"`` -> ``"RUS"``. Raises ``ValueError`` if unknown."""
    v = value.strip()
    if not v:
        raise ValueError("Пустое название страны")
    options = load_org_options().get("countryid", [])
    codes = {o["value"].upper(): o["value"] for o in options}
    if v.upper() in codes:
        return codes[v.upper()]
    if len(v) == 3 and v.isascii() and v.isalpha():
        return v.upper()  # a valid-looking ISO3 code not present in the snapshot
    target = _norm(v)
    for o in options:
        if _norm(o["label"]) == target:
            return o["value"]
    candidates = [o for o in options if target in _norm(o["label"])]
    if len(candidates) == 1:
        return candidates[0]["value"]
    hint = ", ".join(f"{o['label']} ({o['value']})" for o in candidates[:10])
    raise ValueError(
        f"Неизвестная страна: {value!r}. Укажите трёхбуквенный код ISO 3166-1 (например RUS)"
        + (f"; похожие: {hint}" if hint else "")
    )


def resolve_region(value: str | int) -> int:
    """``73``/``"73"`` -> 73; ``"Москва"`` -> 73; ``"новосибирская"`` -> 51. Raises ``ValueError`` if unknown."""
    if isinstance(value, int):
        return value
    v = value.strip()
    if v.isdigit():
        return int(v)
    options = load_org_options().get("regionid", [])
    target = _norm(v)
    exact = [o for o in options if _norm(o["label"]) == target]
    if exact:
        return int(max(exact, key=lambda o: o.get("count") or 0)["value"])
    candidates = [o for o in options if target in _norm(o["label"])]
    if len(candidates) == 1:
        return int(candidates[0]["value"])
    hint = ", ".join(f"{o['label']} ({o['value']})" for o in candidates[:10])
    raise ValueError(
        f"Неизвестный регион: {value!r}. Укажите id региона elibrary.ru или точное название"
        + (f"; похожие: {hint}" if hint else "")
    )


# --------------------------------------------------------------------------- orgs.asp


def build_org_search_form(q: OrgQuery) -> FormFields:
    """Form fields of ``POST /orgs.asp`` in browser order:
    ``pagenum, orgname, town, regionid, countryid, sortorder, order``.

    Page 1 sends ``pagenum=""`` exactly like ``org_search()``; page N sends ``pagenum=N`` like
    ``goto_page(N)``. ``q.region`` / a country name in ``q.country`` are resolved through
    ``data/orgs_options.json`` (``ValueError`` if unknown).
    """
    region_id = q.region_id
    if region_id is None and q.region:
        region_id = resolve_region(q.region)
    country = resolve_country(q.country) if q.country else ""
    return [
        ("pagenum", str(q.page) if q.page > 1 else ""),
        ("orgname", q.name or ""),
        ("town", q.town or ""),
        ("regionid", str(region_id or 0)),
        ("countryid", country),
        ("sortorder", ORG_SORT_CODES[q.sort]),
        ("order", ORDER_CODES[q.effective_order]),
    ]


def build_org_search_request(q: OrgQuery) -> RequestSpec:
    """``POST /orgs.asp`` with :func:`build_org_search_form`."""
    return RequestSpec("POST", ORGS_PATH, build_org_search_form(q))


# --------------------------------------------------------------------------- single organization


def build_org_profile_request(org_id: int) -> RequestSpec:
    """``GET /org_profile.asp?id=ID`` (publication-activity analysis)."""
    return RequestSpec("GET", ORG_PROFILE_PATH, [("id", str(int(org_id)))])


def build_org_about_request(org_id: int) -> RequestSpec:
    """``GET /org_about.asp?orgsid=ID`` (organization card: names, address, type, contacts)."""
    return RequestSpec("GET", ORG_ABOUT_PATH, [("orgsid", str(int(org_id)))])


def build_org_items_request(
    org_id: int,
    page: int = 1,
    *,
    show_option: str | int | None = None,
    show_sotr: str | int | None = None,
    show_refs: bool | None = None,
    years: list[int] | None = None,
    sortorder: str | None = None,
    order: str | None = None,
) -> RequestSpec:
    """Publications of an organization: ``GET /org_items.asp?orgsid=ID[&pagenum=N][&...]``.

    Best guess (no fixture). Page 1 without options is exactly the site's own link
    ``org_items.asp?orgsid=ID``. Optional filters use the parameter names of the links on
    org_profile.asp (``show_option``, ``show_sotr``, ``show_refs``, ``pubyear=2021|2022``) and of the
    form (``sortorder``, ``order``):

    * ``show_option``: see :data:`ORG_ITEMS_SHOW_OPTIONS` (``"0"`` РИНЦ, ``"1"`` all, ``"2"`` ядро РИНЦ, ...);
    * ``show_sotr``: see :data:`ORG_ITEMS_SHOW_SOTR`;
    * ``sortorder``: a key of :data:`ORG_ITEMS_SORT_CODES` (``"date"``, ``"cited"``, ...) or a raw code;
    * ``order``: ``"asc"``/``"desc"`` or ``"0"``/``"1"``.

    Large organizations answer slowly (> 60 s for the last pages, see reverse-engineering notes).
    """
    if page < 1:
        raise ValueError("Номер страницы должен быть >= 1")
    params: FormFields = [("orgsid", str(int(org_id)))]
    if page > 1:
        params.append(("pagenum", str(page)))
    if show_option is not None:
        params.append(("show_option", str(show_option)))
    if show_sotr is not None:
        params.append(("show_sotr", str(show_sotr)))
    if show_refs is not None:
        params.append(("show_refs", "1" if show_refs else "0"))
    if years:
        params.append(("pubyear", "|".join(str(int(y)) for y in years)))
    if sortorder is not None:
        params.append(("sortorder", ORG_ITEMS_SORT_CODES.get(sortorder, sortorder)))
    if order is not None:
        params.append(("order", ORDER_CODES.get(order, order)))
    return RequestSpec("GET", ORG_ITEMS_PATH, params)
