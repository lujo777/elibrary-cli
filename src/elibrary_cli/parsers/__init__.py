"""Pure HTML -> model parsers, one module per page family.

Re-exports the public parse functions of ``parsers/search.py``, ``item.py``, ``rows.py``,
``authors.py``, ``organizations.py`` and ``journals.py``; ``parsers.common`` holds the shared helpers.
"""

from __future__ import annotations

from elibrary_cli.parsers.authors import parse_author_profile, parse_author_publications, parse_author_search
from elibrary_cli.parsers.item import (
    COLOUR_ACCESS,
    is_captcha_page,
    normalize_kind,
    parse_fulltext_flag,
    parse_publication,
)
from elibrary_cli.parsers.journals import (
    journal_id_from_url,
    parse_issue_contents,
    parse_journal_indicators,
    parse_journal_profile,
    parse_journal_search,
    parse_journal_search_options,
    split_issue_label,
)
from elibrary_cli.parsers.organizations import (
    parse_org_about,
    parse_org_form_options,
    parse_org_profile,
    parse_org_search,
    parse_organization_publications,
)
from elibrary_cli.parsers.rows import (
    infer_kind,
    parse_publication_row,
    parse_publication_rows,
    parse_reference_rows,
    row_year,
)
from elibrary_cli.parsers.search import parse_search_results

__all__ = [
    "COLOUR_ACCESS",
    "infer_kind",
    "is_captcha_page",
    "journal_id_from_url",
    "normalize_kind",
    "parse_author_profile",
    "parse_author_publications",
    "parse_author_search",
    "parse_fulltext_flag",
    "parse_issue_contents",
    "parse_journal_indicators",
    "parse_journal_profile",
    "parse_journal_search",
    "parse_journal_search_options",
    "parse_org_about",
    "parse_org_form_options",
    "parse_org_profile",
    "parse_org_search",
    "parse_organization_publications",
    "parse_publication",
    "parse_publication_row",
    "parse_publication_rows",
    "parse_reference_rows",
    "parse_search_results",
    "row_year",
    "split_issue_label",
]
