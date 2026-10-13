"""Request builders for the journal pages.

``titles.asp`` (verified against ``tests/fixtures/html/titles_form.html`` and a live request, 2026-10-13):
the form ``results`` (``<FORM method=post name="results">``, action set by JS to ``titles.asp``) holds,
in document order::

    titles_all, pagenum, titlebox_name            (hidden)
    titlename                                     (text: title, publisher or ISSN)
    countryid, rubriccode, language, translated,
    wos, scopus, white, vak, risc, ftopen         (selects)
    rsci, doaj, corerisc, fulltext,
    isactive, isscientific                        (checkboxes, sent as "on" only when checked)
    sortorder, order                              (selects)

JS ``title_search()`` sets ``pagenum=""`` and submits; ``goto_page(N)`` sets ``pagenum=N`` and submits the
same form, so pagination is stateless (every page re-sends the whole query). The live request body of the
fixture ``titles_results.html`` was::

    titles_all=&pagenum=&titlebox_name=&titlename=вычислительные технологии&countryid=&rubriccode=
    &language=&translated=&wos=&scopus=&white=&vak=&risc=&ftopen=&sortorder=1&order=1

On the result page the form also contains ``tid<TITLEID>`` checkboxes (unchecked, never sent).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

from elibrary_cli.models.base import BASE_URL
from elibrary_cli.models.journals import (
    CODE_TABLES,
    ORDER_CODES,
    SORT_CODES,
    JournalQuery,
)

TITLES_PATH = "/titles.asp"
JOURNAL_PROFILE_PATH = "/title_about_new.asp"
JOURNAL_ANALYSIS_PATH = "/title_profile.asp"
CONTENTS_PATH = "/contents.asp"

#: Checkboxes of the form in DOM order: form field -> JournalQuery attribute.
CHECKBOXES: tuple[tuple[str, str], ...] = (
    ("rsci", "rsci"),
    ("doaj", "doaj"),
    ("corerisc", "core_rinc"),
    ("fulltext", "fulltext"),
    ("isactive", "active"),
    ("isscientific", "scientific"),
)

#: Coded selects in DOM order: form field -> JournalQuery attribute.
CODED_SELECTS: tuple[str, ...] = ("translated", "wos", "scopus", "white", "vak", "risc", "ftopen")

FormFields = list[tuple[str, str]]

_OPTIONS_FILE = "journals_options.json"


def build_journal_search_form(q: JournalQuery) -> FormFields:
    """Ordered ``(name, value)`` pairs of ``POST /titles.asp`` exactly as the browser sends them."""
    fields: FormFields = [
        ("titles_all", ""),
        ("pagenum", "" if q.page <= 1 else str(q.page)),
        ("titlebox_name", ""),
        ("titlename", q.name or ""),
        ("countryid", q.country or ""),
        ("rubriccode", q.rubric_code or ""),
        ("language", q.language or ""),
    ]
    for name in CODED_SELECTS:
        value = getattr(q, name)
        fields.append((name, CODE_TABLES[name][value] if value else ""))
    for name, attr in CHECKBOXES:
        if getattr(q, attr):
            fields.append((name, "on"))
    fields.append(("sortorder", SORT_CODES[q.sort]))
    fields.append(("order", ORDER_CODES[q.effective_order]))
    return fields


def journal_search_url(*, base: str = BASE_URL) -> str:
    """URL the search form is posted to."""
    return f"{base.rstrip('/')}{TITLES_PATH}"


def journal_profile_url(journal_id: int, *, english: bool = False, base: str = BASE_URL) -> str:
    """Journal profile: ``title_about_new.asp?id=ID`` (``&langid=2`` for the English description)."""
    url = f"{base.rstrip('/')}{JOURNAL_PROFILE_PATH}?id={int(journal_id)}"
    return url + "&langid=2" if english else url


def journal_analysis_url(journal_id: int, *, base: str = BASE_URL) -> str:
    """Publication-activity analysis with impact factors: ``title_profile.asp?id=ID``."""
    return f"{base.rstrip('/')}{JOURNAL_ANALYSIS_PATH}?id={int(journal_id)}"


def issue_url(journal_id: int | None = None, issue_id: int | None = None, *, base: str = BASE_URL) -> str:
    """``contents.asp?id=ISSUEID`` for one issue, or ``contents.asp?titleid=ID`` for the latest issue
    of a journal. ``issue_id`` wins when both are given."""
    root = f"{base.rstrip('/')}{CONTENTS_PATH}"
    if issue_id:
        return f"{root}?id={int(issue_id)}"
    if journal_id:
        return f"{root}?titleid={int(journal_id)}"
    raise ValueError("Укажите journal_id (последний выпуск журнала) или issue_id (конкретный выпуск)")


# --------------------------------------------------------------------------------------------------
# Reference data: select options of titles.asp (data/journals_options.json)
# --------------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def journal_options() -> dict[str, list[dict[str, Any]]]:
    """Select options of the journal search form: ``{"countryid": [{"value", "label", "count"}...], ...}``.

    Keys are form field names (countryid, rubriccode, language, translated, wos, scopus, white, vak, risc,
    ftopen, sortorder, order) plus ``checkboxes`` (rsci, doaj, corerisc, fulltext, isactive, isscientific)."""
    try:
        raw = resources.files("elibrary_cli").joinpath("data", _OPTIONS_FILE).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):  # pragma: no cover - packaging error
        return {}
    return json.loads(raw)


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ").replace("ё", "е").replace("Ё", "Е")).strip().lower()


def find_option(field: str, text: str) -> str | None:
    """Site code of the option of ``field`` whose label is ``text`` (case-insensitive; a unique
    prefix/substring match is accepted too). ``find_option("countryid", "Германия") == "DEU"``."""
    options = journal_options().get(field) or []
    if not text:
        return None
    want = _norm_label(text)
    for opt in options:
        if want in (_norm_label(str(opt.get("value", ""))), _norm_label(str(opt.get("label", "")))):
            return str(opt["value"])
    starts = [o for o in options if _norm_label(str(o.get("label", ""))).startswith(want)]
    if len(starts) == 1:
        return str(starts[0]["value"])
    contains = [o for o in options if want in _norm_label(str(o.get("label", "")))]
    if len(contains) == 1:
        return str(contains[0]["value"])
    return None


def option_label(field: str, value: str) -> str | None:
    """Label of the option ``value`` of ``field`` (``option_label("vak", "2")`` -> 'Перечень ВАК (категория 1)')."""
    for opt in journal_options().get(field) or []:
        if str(opt.get("value")) == str(value):
            return str(opt.get("label"))
    return None


__all__ = [
    "CHECKBOXES",
    "CODED_SELECTS",
    "CONTENTS_PATH",
    "JOURNAL_ANALYSIS_PATH",
    "JOURNAL_PROFILE_PATH",
    "TITLES_PATH",
    "build_journal_search_form",
    "find_option",
    "issue_url",
    "journal_analysis_url",
    "journal_options",
    "journal_profile_url",
    "journal_search_url",
    "option_label",
]
