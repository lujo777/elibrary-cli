"""Reference data: the select options of the elibrary.ru search forms.

The option lists were captured from the live forms (2026-10) and ship with the package as
``data/<area>_options.json`` (``authors``, ``journals``, ``orgs``). The publication search form
(``querybox.asp``) has no data file: its few options are fixed and listed here, keyed by the values
:class:`elibrary_cli.models.publications.PublicationQuery` accepts.

Every list is addressed by a name ``"<area>.<field>"`` (see :data:`NAMES`), e.g. ``"journals.vak"`` or
``"orgs.regionid"``; friendly aliases such as ``"regions"`` or ``"vak"`` are accepted too
(:data:`ALIASES`). Each option is a dict ``{"value", "label"[, "count"][, "site_code"][, "form_field"]}``,
with one meaning in every list:

* ``value`` -- what the query model field named by the list's ``query_field`` (and the CLI option /
  MCP tool parameter of the same name) accepts: the readable value where the model has one
  (``JournalQuery(vak="k1")``, ``AuthorQuery(sort="citations")``, ``PublicationQuery(types=["article"])``,
  ``PublicationQuery(order="desc")``), otherwise the site code itself (ISO3 country code, GRNTI code,
  region id, ``"SPIN"`` ...); for the checkbox lists (``publications.flags``, ``journals.checkboxes``) the
  name of the boolean model field (``fuzzy``, ``core_rinc``);
* ``label`` -- the Russian text of the option;
* ``count`` -- the number of records the form showed next to the option, when it showed one;
* ``site_code`` -- the code the site's select sends for the option, only when it differs from ``value``
  (``vak`` ``"k1"`` -> ``"2"``, ``publications.order`` ``"desc"`` -> ``"rev"``); informational, the query
  builders translate ``value`` themselves;
* ``form_field`` -- the name of the form checkbox for the checkbox-like lists (``publications.types`` /
  ``where`` / ``flags``: ``type_article``, ``where_name``, ``search_morph``; ``journals.checkboxes``:
  ``corerisc``); informational as well.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

__all__ = [
    "ALIASES",
    "FILES",
    "NAMES",
    "describe_options",
    "find_options",
    "list_options",
    "resolve_name",
]

#: Data file of each area (in the ``elibrary_cli.data`` package).
FILES: dict[str, str] = {
    "authors": "authors_options.json",
    "journals": "journals_options.json",
    "orgs": "orgs_options.json",
}

# name -> (Russian description, query model field the values go to)
_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    # publication search (querybox.asp), values of PublicationQuery
    "publications.types": ("Типы публикаций расширенного поиска", "PublicationQuery.types"),
    "publications.where": ("Где искать текст запроса", "PublicationQuery.where"),
    "publications.period": ("Период поступления в eLIBRARY.RU", "PublicationQuery.period"),
    "publications.sort": ("Сортировка результатов поиска публикаций", "PublicationQuery.sort"),
    "publications.order": ("Порядок сортировки результатов поиска публикаций", "PublicationQuery.order"),
    "publications.flags": ("Дополнительные условия поиска публикаций (флажки)", "PublicationQuery.<поле>"),
    # authors.asp
    "authors.codetype": ("Тип идентификатора автора (SPIN, ORCID, ...)", "AuthorQuery.code_type"),
    "authors.countryid": ("Страны в поиске авторов", "AuthorQuery.country"),
    "authors.authorboxid": ("Справочные группы авторов", "AuthorQuery.author_group"),
    "authors.rubriccode": ("Тематические рубрики ГРНТИ в поиске авторов", "AuthorQuery.rubric_code"),
    "authors.metrics": ("Основа показателей автора (ядро РИНЦ, РИНЦ, eLIBRARY.RU)", "AuthorQuery.metrics"),
    "authors.sortorder": ("Сортировка списка авторов", "AuthorQuery.sort"),
    "authors.order": ("Порядок сортировки списка авторов", "AuthorQuery.order"),
    # titles.asp
    "journals.countryid": ("Страны в поиске журналов", "JournalQuery.country"),
    "journals.rubriccode": ("Тематические рубрики ГРНТИ в поиске журналов", "JournalQuery.rubric_code"),
    "journals.language": ("Языки журналов", "JournalQuery.language"),
    "journals.translated": ("Перевод журнала на английский язык", "JournalQuery.translated"),
    "journals.wos": ("Индексация в Web of Science", "JournalQuery.wos"),
    "journals.scopus": ("Индексация в Scopus", "JournalQuery.scopus"),
    "journals.white": ("Белый список / ЕГПНИ", "JournalQuery.white"),
    "journals.vak": ("Перечень ВАК", "JournalQuery.vak"),
    "journals.risc": ("Индексация в РИНЦ и квартиль Science Index", "JournalQuery.risc"),
    "journals.ftopen": ("Открытость полных текстов", "JournalQuery.ftopen"),
    "journals.sortorder": ("Сортировка списка журналов", "JournalQuery.sort"),
    "journals.order": ("Порядок сортировки списка журналов", "JournalQuery.order"),
    "journals.checkboxes": ("Флажки поиска журналов (RSCI, DOAJ, ядро РИНЦ, ...)", "JournalQuery.<поле>"),
    # orgs.asp
    "orgs.countryid": ("Страны в поиске организаций", "OrgQuery.country"),
    "orgs.regionid": ("Регионы в поиске организаций", "OrgQuery.region_id"),
    "orgs.sortorder": ("Сортировка списка организаций", "OrgQuery.sort"),
    "orgs.order": ("Порядок сортировки списка организаций", "OrgQuery.order"),
}

#: Every list name, ``"<area>.<field>"``.
NAMES: tuple[str, ...] = tuple(_DESCRIPTIONS)

#: Friendly names -> list names.
ALIASES: dict[str, str] = {
    "types": "publications.types",
    "publication_types": "publications.types",
    "where": "publications.where",
    "period": "publications.period",
    "publication_sort": "publications.sort",
    "flags": "publications.flags",
    "codetype": "authors.codetype",
    "author_codes": "authors.codetype",
    "author_countries": "authors.countryid",
    "author_groups": "authors.authorboxid",
    "author_rubrics": "authors.rubriccode",
    "metrics": "authors.metrics",
    "author_sort": "authors.sortorder",
    "journal_countries": "journals.countryid",
    "journal_rubrics": "journals.rubriccode",
    "rubrics": "journals.rubriccode",
    "languages": "journals.language",
    "translated": "journals.translated",
    "wos": "journals.wos",
    "scopus": "journals.scopus",
    "white": "journals.white",
    "white_list": "journals.white",
    "vak": "journals.vak",
    "risc": "journals.risc",
    "ftopen": "journals.ftopen",
    "open_access": "journals.ftopen",
    "journal_sort": "journals.sortorder",
    "journal_flags": "journals.checkboxes",
    "countries": "orgs.countryid",
    "org_countries": "orgs.countryid",
    "regions": "orgs.regionid",
    "org_sort": "orgs.sortorder",
}

# Options of querybox.asp (labels as on the form, 2026-10), values = PublicationQuery values.
_PUBLICATION_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "types": [
        {"value": "article", "label": "статьи в журналах", "form_field": "type_article"},
        {"value": "disser", "label": "диссертации", "form_field": "type_disser"},
        {"value": "book", "label": "книги", "form_field": "type_book"},
        {"value": "report", "label": "отчеты", "form_field": "type_report"},
        {"value": "conf", "label": "материалы конференций", "form_field": "type_conf"},
        {"value": "patent", "label": "патенты", "form_field": "type_patent"},
        {"value": "preprint", "label": "депонированные рукописи", "form_field": "type_preprint"},
        {"value": "grant", "label": "гранты", "form_field": "type_grant"},
        {"value": "dataset", "label": "наборы данных", "form_field": "type_dataset"},
    ],
    "where": [
        {"value": "name", "label": "в названии публикации", "form_field": "where_name"},
        {"value": "abstract", "label": "в аннотации", "form_field": "where_abstract"},
        {"value": "keywords", "label": "в ключевых словах", "form_field": "where_keywords"},
        {"value": "affiliation", "label": "в названии организаций авторов", "form_field": "where_affiliation"},
        {"value": "fulltext", "label": "в полном тексте (включает все остальные поля)", "form_field": "where_fulltext"},
    ],
    "period": [
        {"value": "all", "label": "за все время"},
        {"value": "m1", "label": "за последний месяц"},
        {"value": "m3", "label": "за последние 3 месяца"},
        {"value": "m6", "label": "за последние полгода"},
        {"value": "m12", "label": "за последний год"},
    ],
    "sort": [
        {"value": "rank", "label": "по релевантности"},
        {"value": "date", "label": "по дате выпуска"},
        {"value": "insdate", "label": "по дате добавления"},
        {"value": "name", "label": "по названию статьи"},
        {"value": "title", "label": "по названию журнала"},
        {"value": "cited", "label": "по числу цитирований"},
    ],
    "order": [
        {"value": "desc", "label": "по убыванию", "site_code": "rev"},
        {"value": "asc", "label": "по возрастанию", "site_code": "nrm"},
    ],
    "flags": [
        {"value": "morphology", "label": "искать с учетом морфологии", "form_field": "search_morph"},
        {"value": "fuzzy", "label": "искать похожий текст (включено по умолчанию)", "form_field": "search_freetext"},
        {"value": "fulltext_only", "label": "искать в публикациях, имеющих полный текст на eLibrary.Ru",
         "form_field": "search_fulltext"},
        {"value": "open_only", "label": "искать в публикациях, доступных для Вас", "form_field": "search_open"},
        {"value": "in_previous_results", "label": "искать в результатах предыдущего запроса",
         "form_field": "search_results"},
    ],
}


#: Lists of form checkboxes (the data file's ``value`` is the checkbox name -> ``form_field``).
_CHECKBOX_LISTS = frozenset({"journals.checkboxes"})


def _readable_keys() -> dict[str, dict[str, str]]:
    """``"<area>.<field>"`` -> {site code / checkbox name: model value} for coded selects."""
    from elibrary_cli.models import authors as ma
    from elibrary_cli.models import journals as mj

    def inverse(table: dict[str, str]) -> dict[str, str]:
        return {code: key for key, code in table.items()}

    out = {
        "authors.metrics": inverse(ma.METRICS_CODES),
        "authors.sortorder": inverse(ma.AUTHOR_SORT_CODES),
        "authors.order": inverse(ma.ORDER_CODES),
        "journals.sortorder": inverse(mj.SORT_CODES),
        "journals.order": inverse(mj.ORDER_CODES),
    }
    for field, table in mj.CODE_TABLES.items():
        name = f"journals.{field}"
        if name in _DESCRIPTIONS and name not in out:
            out[name] = inverse(table)
    checkbox_attrs = {"corerisc": "core_rinc", "isactive": "active", "isscientific": "scientific"}
    out["journals.checkboxes"] = {
        v: checkbox_attrs.get(v, v) for v in ("rsci", "doaj", "corerisc", "fulltext", "isactive", "isscientific")
    }
    return out


@cache
def _load_file(area: str) -> dict[str, Any]:
    text = resources.files("elibrary_cli.data").joinpath(FILES[area]).read_text(encoding="utf-8")
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def resolve_name(name: str) -> str:
    """Canonical list name for ``name`` (``"regions"`` -> ``"orgs.regionid"``).

    Accepts ``"<area>.<field>"``, ``"<area>/<field>"``, ``"<area>:<field>"`` and the aliases; ``"org"``
    / ``"organizations"`` / ``"author"`` / ``"journal"`` / ``"titles"`` / ``"publication"`` are accepted as area
    names. Raises ``ValueError`` (Russian) for an unknown name."""
    raw = (name or "").strip()
    key = raw.lower().replace("-", "_").replace("/", ".").replace(":", ".")
    if key in ALIASES:
        return ALIASES[key]
    if "." in key:
        area, _, field = key.partition(".")
        area = {
            "org": "orgs", "organizations": "orgs", "organisation": "orgs",
            "author": "authors", "journal": "journals", "titles": "journals",
            "publication": "publications", "search": "publications", "querybox": "publications",
        }.get(area, area)
        # field names of the data files are lower case (codetype, countryid ...)
        candidate = f"{area}.{field}"
        if candidate in _DESCRIPTIONS:
            return candidate
    raise ValueError(
        f"Неизвестный справочник {raw!r}. Доступны: {', '.join(NAMES)} "
        f"(а также сокращения: {', '.join(sorted(ALIASES))})"
    )


def list_options(name: str) -> list[dict[str, Any]]:
    """Options of the list ``name`` (see :data:`NAMES` / :data:`ALIASES`), in the order of the form.

    Returns fresh dicts ``{"value", "label"[, "count"][, "site_code"][, "form_field"]}`` (``value`` is
    always what the query model accepts, see the module docstring); raises ``ValueError`` for an
    unknown name."""
    canonical = resolve_name(name)
    area, _, field = canonical.partition(".")
    if area == "publications":
        return [dict(o) for o in _PUBLICATION_OPTIONS[field]]
    raw = _load_file(area).get(field)
    if not isinstance(raw, list):
        return []
    keys = _readable_keys().get(canonical, {})
    checkboxes = canonical in _CHECKBOX_LISTS
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or "value" not in item:
            continue
        code = str(item["value"])
        readable = str(item.get("key") or keys.get(code) or code)
        opt: dict[str, Any] = {"value": readable, "label": str(item.get("label", ""))}
        if item.get("count") is not None:
            opt["count"] = item["count"]
        if checkboxes:
            opt["form_field"] = code
        elif readable != code:
            opt["site_code"] = code
        out.append(opt)
    return out


def _norm(text: str) -> str:
    return " ".join(str(text).replace("\xa0", " ").replace("ё", "е").replace("Ё", "Е").split()).casefold()


def find_options(name: str, text: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Options of ``name`` whose value or label matches ``text``: exact matches first (of ``value``,
    ``label``, ``site_code`` or ``form_field``), then labels that start with it, then labels that contain
    it (case- and ё-insensitive), at most ``limit``."""
    want = _norm(text)
    if not want:
        return []
    exact, starts, contains = [], [], []
    for opt in list_options(name):
        label = _norm(opt.get("label", ""))
        codes = (_norm(opt["value"]), label, _norm(opt.get("site_code", "")), _norm(opt.get("form_field", "")))
        if want in codes:
            exact.append(opt)
        elif label.startswith(want):
            starts.append(opt)
        elif want in label:
            contains.append(opt)
    return (exact + starts + contains)[: max(0, limit)]


def describe_options() -> list[dict[str, Any]]:
    """One entry per list: ``{"name", "description", "query_field", "count", "aliases"}``."""
    aliases: dict[str, list[str]] = {}
    for alias, target in ALIASES.items():
        aliases.setdefault(target, []).append(alias)
    out = []
    for name in NAMES:
        description, field = _DESCRIPTIONS[name]
        out.append({
            "name": name,
            "description": description,
            "query_field": field,
            "count": len(list_options(name)),
            "aliases": sorted(aliases.get(name, [])),
        })
    return out
