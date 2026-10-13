"""Pydantic models returned by the client, the CLI (--format json) and the MCP tools.

Everything public of ``models/base.py``, ``models/publications.py``, ``models/authors.py``,
``models/organizations.py`` and ``models/journals.py`` is re-exported here. Models that live outside the
``models`` package are re-exported lazily (importing them eagerly would create import cycles):
``FullText``, ``FulltextRequest``, ``FulltextAttempt`` (``elibrary_cli.fulltext``) and ``SessionInfo``
(``elibrary_cli.auth``).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from elibrary_cli.models.authors import (
    AuthorAffiliation,
    AuthorHit,
    AuthorMetric,
    AuthorProfile,
    AuthorPublicationsPage,
    AuthorPublicationsQuery,
    AuthorQuery,
    AuthorReferenceHit,
    AuthorReport,
    AuthorYearlyMetric,
)
from elibrary_cli.models.base import BASE_URL, ElibModel, PublicationHit, SearchPage
from elibrary_cli.models.journals import (
    EditorialMember,
    IssueContents,
    IssueRef,
    JournalFile,
    JournalHit,
    JournalIndexingInfo,
    JournalIndicators,
    JournalOrgRef,
    JournalProfile,
    JournalQuery,
    JournalSection,
    PeerReview,
    Rubric,
)
from elibrary_cli.models.organizations import (
    OrgDissertationCouncil,
    OrgFieldRow,
    OrgHit,
    OrgIndicator,
    OrgJournal,
    OrgProfile,
    OrgPublicationsPage,
    OrgQuery,
    OrgRosstatRow,
    OrgTable,
    OrgYearSeries,
)
from elibrary_cli.models.publications import (
    ALL_TYPES,
    ALL_WHERE,
    DEFAULT_WHERE,
    Affiliation,
    AuthorRef,
    BookInfo,
    ConferenceInfo,
    ContainerRef,
    Dates,
    DissertationInfo,
    FulltextFlag,
    FulltextInfo,
    FulltextLink,
    IpcCode,
    JournalIndexing,
    JournalRef,
    Metrics,
    OrgRef,
    PatentInfo,
    Publication,
    PublicationQuery,
    ReferenceHit,
    RelatedItem,
    Rubrics,
)

#: Lazily re-exported names -> defining module.
_LAZY: dict[str, str] = {
    "FullText": "elibrary_cli.fulltext",
    "FulltextAttempt": "elibrary_cli.fulltext",
    "FulltextRequest": "elibrary_cli.fulltext",
    "SessionInfo": "elibrary_cli.auth",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


__all__ = [
    # base
    "BASE_URL",
    "ElibModel",
    "PublicationHit",
    "SearchPage",
    # publications
    "ALL_TYPES",
    "ALL_WHERE",
    "DEFAULT_WHERE",
    "Affiliation",
    "AuthorRef",
    "BookInfo",
    "ConferenceInfo",
    "ContainerRef",
    "Dates",
    "DissertationInfo",
    "FulltextFlag",
    "FulltextInfo",
    "FulltextLink",
    "IpcCode",
    "JournalIndexing",
    "JournalRef",
    "Metrics",
    "OrgRef",
    "PatentInfo",
    "Publication",
    "PublicationQuery",
    "ReferenceHit",
    "RelatedItem",
    "Rubrics",
    # authors
    "AuthorAffiliation",
    "AuthorHit",
    "AuthorMetric",
    "AuthorProfile",
    "AuthorPublicationsPage",
    "AuthorPublicationsQuery",
    "AuthorQuery",
    "AuthorReferenceHit",
    "AuthorReport",
    "AuthorYearlyMetric",
    # organizations
    "OrgDissertationCouncil",
    "OrgFieldRow",
    "OrgHit",
    "OrgIndicator",
    "OrgJournal",
    "OrgProfile",
    "OrgPublicationsPage",
    "OrgQuery",
    "OrgRosstatRow",
    "OrgTable",
    "OrgYearSeries",
    # journals
    "EditorialMember",
    "IssueContents",
    "IssueRef",
    "JournalFile",
    "JournalHit",
    "JournalIndexingInfo",
    "JournalIndicators",
    "JournalOrgRef",
    "JournalProfile",
    "JournalQuery",
    "JournalSection",
    "PeerReview",
    "Rubric",
    # lazy (other packages)
    "FullText",
    "FulltextAttempt",
    "FulltextRequest",
    "SessionInfo",
]
