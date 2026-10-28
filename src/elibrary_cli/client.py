"""``ElibraryClient``: one method per user action, shared by the CLI and the MCP server.

The client combines the query builders (``queries/``), the single HTTP layer (``http.ElibraryHTTP``)
and the pure page parsers (``parsers/``) and owns the logic that depends on server-side state:

* **Publication search pagination.** ``query_results.asp`` keeps the *last query of the session* on
  the server: the query is run with ``changed=1`` (that request returns page 1), further pages are
  ``GET query_results.asp?pagenum=N`` of whatever query the session currently holds. The client
  remembers the signature of that query and when it was run in ``http.state["query.current"]``
  (``{"sig", "ts"}``, shared by the CLI and the MCP server, cleared by login/logout/cookie import) and
  caches every result page under ``search:<signature>:<page>``. The remembered query is trusted only
  while its page 1 is cached or it is younger than ``TTL_SEARCH`` (the same ``SUserID`` may be used in
  the browser meanwhile). A page that is not in the cache and whose query is not the current one costs
  two requests (re-run, then ``pagenum``); a page whose totals do not match page 1 of the same query
  causes one re-run; a page accepted without a known page 1 is returned but not cached.
* **Identifier resolution.** ``https://elibrary.ru/<DOI|ISBN|SPIN|ORCID|ISSN|ROR>`` redirects to the
  card; the client asks once without following the redirect, reads ``Location`` and caches the answer
  for a year (``TTL_IDENTIFIER``). EDN goes straight to ``item.asp?edn=`` (one request instead of a
  redirect chain).

Every method is synchronous and safe to call from several threads: requests are serialized by the
lock of the ``ElibraryHTTP`` instance, and the multi-request flows (search pagination, redirect
lookups) hold that lock for their whole duration. Requests are budgeted and throttled by ``http.py``;
nothing here retries on a captcha: ``CaptchaRequired`` is raised and only a human can pass the check.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from pydantic import BaseModel

from elibrary_cli import auth, exports, refdata
from elibrary_cli.config import Settings
from elibrary_cli.errors import (
    AccessBlocked,
    CaptchaRequired,
    ElibraryError,
    IpRestricted,
    NotFound,
    ParseError,
)
from elibrary_cli.http import (
    TTL_IDENTIFIER,
    TTL_ITEM,
    TTL_JOURNAL,
    TTL_NONE,
    TTL_PROFILE,
    TTL_SEARCH,
    ElibraryHTTP,
    Gate,
    Page,
    PageCache,
    encode_params,
)
from elibrary_cli.models.authors import (
    CONTEXT_RESOLVE_ORG_NAME,
    AuthorHit,
    AuthorProfile,
    AuthorPublicationsPage,
    AuthorPublicationsQuery,
    AuthorQuery,
)
from elibrary_cli.models.base import BASE_URL, PublicationHit, SearchPage
from elibrary_cli.models.journals import IssueContents, JournalHit, JournalProfile, JournalQuery
from elibrary_cli.models.organizations import OrgHit, OrgProfile, OrgPublicationsPage, OrgQuery
from elibrary_cli.models.publications import FulltextFlag, Publication, PublicationQuery
from elibrary_cli.parsers.authors import parse_author_profile, parse_author_publications, parse_author_search
from elibrary_cli.parsers.common import parse_html
from elibrary_cli.parsers.item import parse_fulltext_flag, parse_publication
from elibrary_cli.parsers.journals import (
    parse_issue_contents,
    parse_journal_indicators,
    parse_journal_profile,
    parse_journal_search,
)
from elibrary_cli.parsers.organizations import (
    parse_org_about,
    parse_org_profile,
    parse_org_search,
    parse_organization_publications,
)
from elibrary_cli.parsers.search import parse_search_results
from elibrary_cli.queries.authors import (
    AUTHOR_ITEMS_PATH,
    AUTHOR_PROFILE_PATH,
    AUTHORS_PATH,
    author_items_params,
    author_profile_params,
    build_author_publications_form,
    build_author_search_form,
)
from elibrary_cli.queries.journals import (
    CONTENTS_PATH,
    JOURNAL_ANALYSIS_PATH,
    JOURNAL_PROFILE_PATH,
    TITLES_PATH,
    build_journal_search_form,
)
from elibrary_cli.queries.organizations import (
    ORGS_PATH,
    build_org_about_request,
    build_org_items_request,
    build_org_profile_request,
    build_org_search_request,
)
from elibrary_cli.queries.publications import (
    ARTICLE_TYPES,
    BOOK_TYPES,
    CONFERENCE_TYPES,
    DISSERTATION_TYPES,
    FORM_PATH,
    PATENT_TYPES,
    SEARCH_PATH,
    build_search_form,
    page_params,
    search_signature,
)

if TYPE_CHECKING:  # pragma: no cover
    from elibrary_cli.auth import SessionInfo
    from elibrary_cli.fulltext import FullText
    from elibrary_cli.fulltext.net import ExternalHTTP
    from elibrary_cli.integrations.zotero import ZoteroWriter

log = logging.getLogger(__name__)

__all__ = [
    "ITEM_PATH",
    "STATE_QUERY",
    "TARGETS",
    "ElibraryClient",
    "WrongTarget",
    "elibrary_target",
    "parse_identifier",
]

ITEM_PATH = "/item.asp"
ISPDF_PATH = "/ispdf.asp"
#: ``http.state`` key with the query the server session currently holds: ``{"sig": <signature>, "ts": <unix
#: time of the run>}`` (a bare signature string written by older versions is treated as stale).
STATE_QUERY = "query.current"
#: What :meth:`ElibraryClient.resolve` can point to.
TARGETS: tuple[str, ...] = ("publication", "author", "journal", "organization", "issue")
#: Maximum number of non-followed redirects of one identifier lookup (session bootstrap included).
MAX_HOPS = 4

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DOI_SAFE = "/:;()[]._-~+,=@!$*'&"
#: Identifier kinds that never point to a publication (checked without a request).
_NON_PUBLICATION_KINDS: dict[str, str] = {"spin": "author", "orcid": "author", "issn": "journal",
                                          "ror": "organization"}
#: What a target is, in the accusative ("указывает на ...").
_TARGET_NAMES: dict[str, str] = {
    "publication": "публикацию",
    "author": "автора",
    "organization": "организацию",
    "journal": "журнал",
    "issue": "выпуск журнала",
}

Q = TypeVar("Q", bound=BaseModel)


class WrongTarget(ValueError):
    """An identifier points to another kind of eLIBRARY.RU object than the one asked for (an author
    profile URL given to :meth:`ElibraryClient.get_publication_any`, ...).

    ``target`` / ``target_id`` say where it points (``target`` may be None when unknown), ``expected``
    what was asked for. The CLI and the MCP server append a hint (which command / tool to use)."""

    def __init__(self, identifier: str, target: str | None, target_id: int | None = None,
                 expected: str = "publication") -> None:
        self.identifier = identifier
        self.target = target
        self.target_id = target_id
        self.expected = expected
        where = _TARGET_NAMES.get(target or "", target or "неизвестную страницу")
        super().__init__(f"Идентификатор {identifier!r} указывает на {where}, а не на "
                         f"{_TARGET_NAMES.get(expected, expected)}")


# ============================================================================ identifiers


def _issn_check(digits7: str) -> str:
    total = sum(int(d) * w for d, w in zip(digits7, range(8, 1, -1), strict=True))
    check = (11 - total % 11) % 11
    return "X" if check == 10 else str(check)


def _isbn_valid(compact: str) -> bool:
    if len(compact) == 13 and compact.isdigit() and compact[:3] in ("978", "979"):
        return sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(compact)) % 10 == 0
    if len(compact) == 10 and compact[:9].isdigit() and (compact[9].isdigit() or compact[9] == "X"):
        values = [int(d) for d in compact[:9]] + [10 if compact[9] == "X" else int(compact[9])]
        return sum(v * w for v, w in zip(values, range(10, 0, -1), strict=True)) % 11 == 0
    return False


_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.I)
_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", re.I)
_ISSN_LIKE_RE = re.compile(r"^(\d{4})-?(\d{3}[\dX])$", re.I)
_ROR_RE = re.compile(r"^0[0-9a-hjkmnp-tv-z]{6}\d{2}$", re.I)
_EDN_RE = re.compile(r"^[A-Za-z]{6}$")
_EDN_PREFIXED_RE = re.compile(r"^[A-Za-z0-9]{6}$")
_ISBN_CHARS_RE = re.compile(r"^[\d\- ]{9,17}[\dXx]$")
_PREFIX_RE = re.compile(r"^(?P<prefix>[A-Za-z_]+)\s*(?::\s*|\s+)(?P<value>\S.*)$")

_PREFIXES: dict[str, str] = {
    "edn": "edn", "doi": "doi", "isbn": "isbn", "spin": "spin", "orcid": "orcid", "issn": "issn",
    "eissn": "issn", "ror": "ror",
    "id": "publication", "item": "publication", "itemid": "publication", "elibrary": "publication",
    "elibrary_id": "publication", "eid": "publication", "publication": "publication",
    "author": "author", "authorid": "author",
    "org": "organization", "orgid": "organization", "orgsid": "organization", "organization": "organization",
    "journal": "journal", "titleid": "journal", "title": "journal",
    "issue": "issue", "issueid": "issue",
}


def _canonical_url(target: str, ident: int) -> str:
    return {
        "publication": f"{BASE_URL}/item.asp?id={ident}",
        "author": f"{BASE_URL}/author_profile.asp?id={ident}",
        "organization": f"{BASE_URL}/org_profile.asp?id={ident}",
        "journal": f"{BASE_URL}/title_about_new.asp?id={ident}",
        "issue": f"{BASE_URL}/contents.asp?id={ident}",
    }[target]


def _is_elibrary_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "elibrary.ru" or host.endswith(".elibrary.ru")


def elibrary_target(url: str) -> dict[str, Any] | None:
    """What an elibrary.ru page URL shows: ``{"target", "id", "url"}`` (plus ``"edn"`` for
    ``item.asp?edn=``), or None for other pages/hosts.

    Understands ``item.asp?id=`` / ``?edn=``, ``author_profile.asp?id=`` (``authorid=``),
    ``author_items.asp?authorid=``, ``org_profile.asp?id=``, ``org_about.asp?orgsid=``,
    ``org_items.asp?orgsid=``, ``title_about.asp`` / ``title_about_new.asp`` / ``title_profile.asp`` /
    ``title_items.asp?id=``, ``contents.asp?titleid=`` (journal) and ``contents.asp?id=`` (issue)."""
    parts = urlsplit(url.strip())
    if parts.hostname and not _is_elibrary_host(parts.hostname):
        return None
    page = parts.path.rsplit("/", 1)[-1].lower()
    qs = {k.lower(): v[0].strip() for k, v in parse_qs(parts.query).items() if v and v[0].strip()}

    def num(*names: str) -> int | None:
        for n in names:
            v = qs.get(n)
            if v and v.isdigit() and int(v) > 0:
                return int(v)
        return None

    target: str | None = None
    ident: int | None = None
    if page == "item.asp":
        ident = num("id")
        if ident is None and qs.get("edn"):
            edn = qs["edn"].upper()
            return {"target": "publication", "id": None, "edn": edn, "url": f"{BASE_URL}/item.asp?edn={edn}"}
        target = "publication"
    elif page in ("author_profile.asp", "author_items.asp", "cit_author_items.asp"):
        target, ident = "author", num("authorid", "id") if page != "cit_author_items.asp" else num("id")
    elif page in ("org_profile.asp", "org_about.asp", "org_items.asp", "cit_org_items.asp"):
        target, ident = "organization", num("orgsid", "id")
    elif page in ("title_about.asp", "title_about_new.asp", "title_profile.asp", "title_items.asp",
                  "cit_title_items.asp"):
        target, ident = "journal", num("id", "titleid")
    elif page == "contents.asp":
        if num("id") is not None:
            target, ident = "issue", num("id")
        else:
            target, ident = "journal", num("titleid")
    if target is None or ident is None:
        return None
    return {"target": target, "id": ident, "url": _canonical_url(target, ident)}


def _short_segment(segment: str) -> dict[str, Any]:
    """``https://elibrary.ru/<segment>`` with the site's semantics: digits are a SPIN code (or an ISBN)."""
    if segment.isdigit():
        if len(segment) in (10, 13) and _isbn_valid(segment):
            return parse_identifier("isbn:" + segment)
        return parse_identifier("spin:" + segment)
    return parse_identifier(segment)


def parse_identifier(identifier: str) -> dict[str, Any]:
    """Detect the type of an identifier without any request.

    Returns ``{"kind", "value", "target", "id", "url", "path"}``:

    * ``kind``: ``edn``, ``doi``, ``isbn``, ``spin``, ``orcid``, ``issn``, ``ror``, ``elibrary_id``
      (a bare number or ``id:``/``author:``/``org:``/``journal:``/``issue:`` + number) or ``url``
      (a recognised elibrary.ru page URL);
    * ``value``: the normalised identifier (EDN upper case, DOI lower case, ISBN without dashes,
      SPIN as 8 digits, ISSN as ``NNNN-NNNC``, ORCID upper case X, ROR lower case);
    * ``target`` / ``id`` / ``url``: filled when the identifier itself says where it points
      (``elibrary_id`` and ``url`` kinds); for ``edn`` the target is ``publication`` with ``id`` None and
      ``url`` = ``item.asp?edn=``; otherwise ``target`` and ``id`` are None and ``url`` is the site's
      short URL;
    * ``path``: the short-URL path to ask the site (``/<identifier>``), None when no request is needed
      (EDN is fetched as ``item.asp?edn=``).

    A bare number is an eLIBRARY item id; write ``spin:12345678`` or ``1234-5678`` for a SPIN code.
    ``NNNN-NNNN`` with a valid ISSN check digit is reported as ``issn`` (the site prefers the ISSN on
    such a collision), otherwise as ``spin``. Raises ``ValueError`` (Russian) for anything else.
    """
    raw = (identifier or "").strip().strip("<>\"'")
    if not raw:
        raise ValueError("Пустой идентификатор")
    text = raw

    def result(kind: str, value: str, *, target: str | None = None, ident: int | None = None,
               url: str | None = None, path: str | None = None) -> dict[str, Any]:
        if url is None:
            url = _canonical_url(target, ident) if target and ident else (BASE_URL + path if path else None)
        return {"kind": kind, "value": value, "target": target, "id": ident, "url": url, "path": path}

    # ---- URLs
    low = text.lower()
    if low.startswith(("http://", "https://", "www.", "elibrary.ru/", "doi.org/", "dx.doi.org/", "orcid.org/",
                       "ror.org/")):
        url = text if low.startswith(("http://", "https://")) else "https://" + text
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = unquote(parts.path).lstrip("/")
        if host in ("doi.org", "dx.doi.org", "www.doi.org"):
            return parse_identifier("doi:" + path)
        if host in ("orcid.org", "www.orcid.org"):
            return parse_identifier("orcid:" + path.strip("/"))
        if host in ("ror.org", "www.ror.org"):
            return parse_identifier("ror:" + path.strip("/"))
        if not _is_elibrary_host(host):
            raise ValueError(f"Ссылка {raw!r} ведёт не на eLIBRARY.RU; укажите DOI, EDN или ссылку на elibrary.ru")
        tgt = elibrary_target(url)
        if tgt is not None:
            if tgt.get("edn"):
                return result("edn", tgt["edn"], target="publication", url=tgt["url"])
            return result("url", url, target=tgt["target"], ident=tgt["id"], url=tgt["url"])
        if path.startswith("10."):
            return parse_identifier("doi:" + path)
        segment = path.strip("/")
        if segment and "/" not in segment and not segment.lower().endswith((".asp", ".aspx", ".html", ".htm")):
            return _short_segment(segment)
        raise ValueError(f"Не удалось распознать ссылку eLIBRARY.RU {raw!r}: ожидается item.asp?id=, "
                         "author_profile.asp?id=, org_profile.asp?id=, title_about_new.asp?id= или contents.asp")

    # ---- explicit prefixes: "doi:10...", "ISBN 978...", "author:3771"
    m = _PREFIX_RE.match(text)
    if m and m.group("prefix").lower() in _PREFIXES:
        kind = _PREFIXES[m.group("prefix").lower()]
        value = m.group("value").strip()
        if kind in TARGETS:
            if not value.isdigit() or int(value) <= 0:
                raise ValueError(f"После {m.group('prefix')}: ожидается числовой идентификатор eLIBRARY.RU, а не {value!r}")
            return result("elibrary_id", value, target=kind, ident=int(value))
        if kind == "edn":
            if not _EDN_PREFIXED_RE.match(value):
                raise ValueError(f"EDN состоит из 6 латинских букв (например FYOAKL), получено {value!r}")
            return result("edn", value.upper(), target="publication", url=f"{BASE_URL}/item.asp?edn={value.upper()}")
        if kind == "spin":
            digits = re.sub(r"[\s-]", "", value)
            if not (digits.isdigit() and 4 <= len(digits) <= 10):
                raise ValueError(f"SPIN-код состоит из цифр (например 1234-5678), получено {value!r}")
            return result("spin", digits, path=f"/{digits}")
        if kind == "issn":
            im = _ISSN_LIKE_RE.match(value)
            if not im:
                raise ValueError(f"ISSN имеет вид NNNN-NNNN, получено {value!r}")
            issn = f"{im.group(1)}-{im.group(2).upper()}"
            return result("issn", issn, path=f"/{issn}")
        text = value  # doi / isbn / orcid / ror: validated below
        low = text.lower()
        forced = kind
    else:
        forced = None

    if (forced in (None, "doi")) and _DOI_RE.match(text):
        doi = text.lower()
        return result("doi", doi, path="/" + quote(doi, safe=_DOI_SAFE))
    if forced == "doi":
        raise ValueError(f"DOI начинается с «10.» и содержит «/» (например 10.1234/abc), получено {raw!r}")
    if forced in (None, "orcid") and _ORCID_RE.match(text):
        orcid = text.upper()
        return result("orcid", orcid, path=f"/{orcid}")
    if forced == "orcid":
        raise ValueError(f"ORCID имеет вид 0000-0000-0000-0000, получено {raw!r}")
    if forced in (None, "ror") and _ROR_RE.match(text) and not text.isdigit():
        ror = text.lower()
        return result("ror", ror, path=f"/{ror}")
    if forced == "ror":
        raise ValueError(f"ROR ID имеет вид 0xxxxxxNN (9 символов), получено {raw!r}")
    if forced is None:
        if text.isdigit():
            if int(text) <= 0:
                raise ValueError(f"Некорректный идентификатор {raw!r}")
            if len(text) in (10, 13) and _isbn_valid(text):
                return result("isbn", text, path=f"/{text}")
            return result("elibrary_id", text, target="publication", ident=int(text))
        im = _ISSN_LIKE_RE.match(text)
        if im and "-" in text:
            first, rest = im.group(1), im.group(2).upper()
            if rest.endswith("X") or _issn_check(first + rest[:3]) == rest[3]:
                issn = f"{first}-{rest}"
                return result("issn", issn, path=f"/{issn}")
            return result("spin", first + rest, path=f"/{first + rest}")
    if forced in (None, "isbn") and _ISBN_CHARS_RE.match(text):
        compact = re.sub(r"[\s-]", "", text).upper()
        if _isbn_valid(compact):
            return result("isbn", compact, path=f"/{compact}")
    if forced == "isbn":
        raise ValueError(f"Некорректный ISBN (ошибка контрольной цифры): {raw!r}")
    if forced is None and _EDN_RE.match(text):
        edn = text.upper()
        return result("edn", edn, target="publication", url=f"{BASE_URL}/item.asp?edn={edn}")
    raise ValueError(
        f"Не удалось распознать идентификатор {raw!r}. Поддерживаются: eLIBRARY ID (число), EDN (6 букв), DOI, "
        "ISBN, SPIN (1234-5678), ORCID, ISSN, ROR, ссылки на страницы elibrary.ru, а также префиксы "
        "id:, author:, org:, journal:, issue:, spin:, issn:, doi:, edn:"
    )


# ============================================================================ helpers


def _as_query(model: type[Q], query: Q | Mapping[str, Any] | None, params: Mapping[str, Any]) -> Q:
    """Build/validate a query model from a model, a mapping and/or keyword arguments."""
    if isinstance(query, model) and not params:
        return query
    return model.model_validate(_merged_query(model, query, params))


def _merged_query(model: type[Q], query: Q | Mapping[str, Any] | None, params: Mapping[str, Any]) -> dict[str, Any]:
    """The field values :func:`_as_query` would validate (``query`` overridden by ``params``)."""
    if query is None:
        return dict(params)
    if isinstance(query, model):
        return {**query.model_dump(), **params}
    if isinstance(query, Mapping):
        return {**query, **params}
    raise TypeError(f"Ожидается {model.__name__} или dict, получено {type(query).__name__}")


_ORG_PUNCT_RE = re.compile(r"[\"'«»“”„`.,;:()\[\]\-–—/]+")
_ORG_PARENS_RE = re.compile(r"\(([^()]*)\)")


def _norm_org_name(text: str) -> str:
    text = str(text).replace("ё", "е").replace("Ё", "Е")
    return " ".join(_ORG_PUNCT_RE.sub(" ", text).split()).casefold()


def _org_name_keys(name: str) -> set[str]:
    """Normalised forms an organization name is matched by: the whole name, the name without the
    parenthesised parts and each parenthesised part ('Российский университет транспорта (МИИТ)')."""
    keys = {_norm_org_name(name), _norm_org_name(_ORG_PARENS_RE.sub(" ", name))}
    keys.update(_norm_org_name(m) for m in _ORG_PARENS_RE.findall(name))
    keys.discard("")
    return keys


def _as_list(items: Any) -> list[Any]:
    if items is None:
        return []
    if isinstance(items, SearchPage):  # a result page: its rows
        return list(items.items)
    if isinstance(items, (str, bytes, int, BaseModel)):
        return [items]
    if isinstance(items, Mapping):
        return [items]
    return list(items)


def _norm_edn(edn: str) -> str:
    value = re.sub(r"\s+", "", str(edn)).upper()
    if value.startswith("EDN:"):
        value = value[4:]
    if not _EDN_PREFIXED_RE.match(value):
        raise ValueError(f"EDN состоит из 6 латинских букв (например FYOAKL), получено {edn!r}")
    return value


def _norm_doi(doi: str) -> str:
    value = str(doi).strip()
    value = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi\s*:?\s*", "", value, flags=re.I).strip()
    if not _DOI_RE.match(value):
        raise ValueError(f"DOI начинается с «10.» и содержит «/» (например 10.1234/abc), получено {doi!r}")
    return value.lower()


def _sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _mark(result: Any, page: Page) -> Any:
    """Copy the cache provenance of ``page`` onto a ``SearchPage``."""
    if isinstance(result, SearchPage):
        result.from_cache = page.from_cache
        result.fetched_at = page.fetched_at
        if not result.source_url:
            result.source_url = page.url
    return result


# ============================================================================ the client



#: The search field each POST search form echoes back on its result page (also on an empty result).
_ECHO_FIELDS = {"orgs.asp": "orgname", "authors.asp": "surname", "titles.asp": "titlename"}


def _check_form_echo(path: str, form: list[tuple[str, str]], page: Page) -> None:
    """Raise ParseError when the server answered a search POST with the bare form.

    elibrary's result pages echo the searched text in the form (``orgname``, ``surname``, ``titlename``),
    even for an empty result. An empty echo for a non-empty query means the form never reached the
    server (e.g. a host-switch redirect turned the POST into a GET), and the page must not be read as
    "nothing found" — nor stay in the cache."""
    field = _ECHO_FIELDS.get(path.rsplit("/", 1)[-1].split("?", 1)[0])
    sent = dict(form).get(field, "").strip() if field else ""
    if not sent:
        return
    tree = parse_html(page.content)
    el = tree.css_first(f'input[name="{field}"], textarea[name="{field}"]')
    if el is None:
        return  # not a page with the form: the parser decides
    echoed = el.attributes.get("value") if el.tag == "input" else el.text()
    if not (echoed or "").strip():
        raise ParseError(
            "eLIBRARY.RU вернул пустую форму поиска вместо результатов: запрос не дошёл до сервера "
            "(вероятно, сайт перенаправил его на другой адрес). Повторите поиск; если ошибка повторяется, "
            "сообщите о ней.", url=page.url)

class ElibraryClient:
    """Facade over eLIBRARY.RU. ``settings`` default to ``http.settings`` or :meth:`Settings.from_env`.

    ``http``: an existing :class:`ElibraryHTTP` (not closed by :meth:`close`); otherwise the client
    creates and owns one. ``net``: an existing ``fulltext.net.ExternalHTTP`` for open-access sources
    (created lazily otherwise). ``zotero``: an existing ``integrations.zotero.ZoteroWriter`` (tests;
    created per call otherwise).
    """

    def __init__(self, settings: Settings | None = None, *, http: ElibraryHTTP | None = None,
                 net: ExternalHTTP | None = None, zotero: ZoteroWriter | None = None) -> None:
        if http is not None:
            self.http = http
            self._owns_http = False
            self.settings = settings or http.settings
        else:
            self.settings = settings or Settings.from_env()
            self.http = ElibraryHTTP(self.settings)
            self._owns_http = True
        self._net = net
        self._owns_net = False
        self._zotero = zotero
        lock = getattr(self.http, "_lock", None)
        #: The lock of the HTTP layer: held for multi-request flows that depend on server state.
        self.lock: threading.RLock = lock if lock is not None else threading.RLock()
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        """Close what the client created (the HTTP layer, the external client). Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._owns_net and self._net is not None:
            with contextlib.suppress(Exception):
                self._net.close()
            self._net = None
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> ElibraryClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def net(self) -> ExternalHTTP:
        """Client for the open-access sources (Crossref, OpenAlex, CyberLeninka ...), created on first use."""
        if self._net is None:
            from elibrary_cli.fulltext.net import ExternalHTTP

            self._net = ExternalHTTP(self.settings)
            self._owns_net = True
        return self._net

    # ------------------------------------------------------------------ cache primitives
    @staticmethod
    def _key(cache_key: str) -> str:
        return PageCache.make_key("KEY", cache_key, None)

    def _peek(self, cache_key: str, ttl_hours: float) -> Page | None:
        """A page stored under ``cache_key`` (the rules of ``http._request``: refresh/offline)."""
        s = self.http.settings
        if s.refresh and not s.offline:
            return None
        return self.http.cache.get(self._key(cache_key), float("inf") if s.offline else ttl_hours * 3600)

    def _store(self, cache_key: str, page: Page) -> None:
        if not page.from_cache:
            self.http.cache.put(self._key(cache_key), page)

    def _json_get(self, cache_key: str, ttl_hours: float) -> dict[str, Any] | None:
        page = self._peek(cache_key, ttl_hours)
        if page is None:
            return None
        try:
            data = json.loads(page.content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _json_put(self, cache_key: str, data: Mapping[str, Any]) -> None:
        body = json.dumps(dict(data), ensure_ascii=False).encode("utf-8")
        self.http.cache.put(self._key(cache_key), Page(url=f"cache:{cache_key}", status=200, content=body,
                                                       content_type="application/json"))

    def _url_key(self, path: str, params: Any = None) -> str:
        return PageCache.make_key("GET", self.http.build_url(path, params), None)

    def _get(self, path: str, params: Any, *, ttl: float, parse: Callable[[Page], Any],
             referer: str | None = None, timeout: float | None = None) -> tuple[Any, Page]:
        """GET a page (standard URL cache key) and parse it; a page that fails to parse is dropped
        from the cache so that the next call fetches it again. ``timeout``: read timeout for slow pages."""
        page = (self.http.get(path, params, referer=referer, cache_ttl_hours=ttl, timeout=timeout) if timeout
                else self.http.get(path, params, referer=referer, cache_ttl_hours=ttl))
        try:
            return parse(page), page
        except (ParseError, NotFound):
            self.http.cache.delete(self._url_key(path, params))
            raise

    def _post(self, path: str, form: list[tuple[str, str]], *, ttl: float, parse: Callable[[Page], Any],
              referer: str | None = None) -> tuple[Any, Page]:
        """POST a stateless form (authors.asp, orgs.asp, titles.asp, author_items.asp): the page
        depends only on the form, so it is cached under a key built from the encoded form."""
        cache_key = f"POST {self.http.absolute(path)}?{encode_params(form)}"
        page = self.http.post(path, form, referer=referer, cache_ttl_hours=ttl, cache_key=cache_key)
        try:
            _check_form_echo(path, form, page)
            return parse(page), page
        except (ParseError, NotFound):
            self.http.cache.delete(self._key(cache_key))
            raise

    # ================================================================== publications: search
    def search_publications(self, query: PublicationQuery | Mapping[str, Any] | None = None, /,
                            **params: Any) -> SearchPage[PublicationHit]:
        """Extended publication search (``querybox.asp`` -> ``query_results.asp``), one page of 100 hits.

        ``query`` is a :class:`PublicationQuery` or a dict; keyword arguments override its fields
        (``search_publications(text="нейронные сети", types="patent", page=2)``). See the module
        docstring for the request sequence; a page found in the cache costs no request."""
        q = _as_query(PublicationQuery, query, params)
        with self.lock:
            return self._search_publications(q)

    def search_articles(self, text: str | None = None, **filters: Any) -> SearchPage[PublicationHit]:
        """Journal articles only (``types=article``); ``filters`` are :class:`PublicationQuery` fields."""
        return self.search_publications(self._preset(text, ARTICLE_TYPES, filters))

    def search_patents(self, text: str | None = None, **filters: Any) -> SearchPage[PublicationHit]:
        """Patents and software/database registration certificates (``types=patent``)."""
        return self.search_publications(self._preset(text, PATENT_TYPES, filters))

    def search_books(self, text: str | None = None, **filters: Any) -> SearchPage[PublicationHit]:
        """Books (``types=book``: monographs, textbooks, collections).

        Conference proceedings volumes and the papers in them are listed under the ``conf`` type
        («материалы конференций»), and chapters may be filed under either, depending on the book they
        belong to: pass ``types=("book", "conf")`` (or use :meth:`search_publications`) to cover both."""
        return self.search_publications(self._preset(text, BOOK_TYPES, filters))

    def search_dissertations(self, text: str | None = None, **filters: Any) -> SearchPage[PublicationHit]:
        """Dissertations and their abstracts (авторефераты) (``types=disser``)."""
        return self.search_publications(self._preset(text, DISSERTATION_TYPES, filters))

    def search_conference_papers(self, text: str | None = None, **filters: Any) -> SearchPage[PublicationHit]:
        """Conference materials: proceedings and papers (``types=conf``)."""
        return self.search_publications(self._preset(text, CONFERENCE_TYPES, filters))

    @staticmethod
    def _preset(text: str | None, types: tuple[str, ...], filters: Mapping[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = {"types": list(types), **filters}
        if text is not None:
            data["text"] = text
        return data

    def search_key(self, q: PublicationQuery) -> str:
        """Cache/session signature of ``q``: :func:`search_signature`, extended with the session's
        current query for ``in_previous_results`` and with the account for ``open_only``/``itembox_id``."""
        sig = search_signature(q)
        extra: list[str] = []
        if q.in_previous_results:
            prev = self._current_query_sig()
            if not prev:
                raise ElibraryError(
                    "Поиск «в результатах предыдущего запроса» невозможен: в текущей сессии eLIBRARY.RU ещё не "
                    "выполнялся запрос (или сессия сменилась). Сначала выполните обычный поиск.")
            extra.append(f"previous={prev}")
        if q.open_only or q.itembox_id:
            extra.append(f"account={self.http.state.get('account') or ''}")
        return _sha(sig + "|" + "|".join(extra)) if extra else sig

    @staticmethod
    def _search_cache_key(sig: str, page: int) -> str:
        return f"search:{sig}:{page}"

    def _current_query(self) -> tuple[str, float] | None:
        """``(signature, run time)`` of the query the server session holds, as far as we know."""
        cur = self.http.state.get(STATE_QUERY)
        if isinstance(cur, str) and cur:  # written by an older version: age unknown
            return cur, 0.0
        if isinstance(cur, Mapping) and isinstance(cur.get("sig"), str):
            try:
                return cur["sig"], float(cur.get("ts") or 0.0)
            except (TypeError, ValueError):
                return cur["sig"], 0.0
        return None

    def _current_query_sig(self) -> str | None:
        """Signature of the session's query while it is younger than ``TTL_SEARCH`` (None otherwise)."""
        cur = self._current_query()
        if cur is None or time.time() - cur[1] >= TTL_SEARCH * 3600:
            return None
        return cur[0]

    def _parse_search(self, page: Page, q: PublicationQuery, n: int) -> SearchPage[PublicationHit]:
        return _mark(parse_search_results(page.content, page=n, query=q, url=page.url), page)

    def _run_query(self, q: PublicationQuery, sig: str) -> SearchPage[PublicationHit]:
        """Run the query on the server (``changed=1``): returns page 1 and makes ``sig`` current."""
        self.http.state.delete(STATE_QUERY)  # the session's query is unknown until this succeeds
        key1 = self._search_cache_key(sig, 1)
        page = self.http.get(SEARCH_PATH, build_search_form(q), referer=FORM_PATH, cache_ttl_hours=TTL_NONE,
                             cache_key=key1)
        first = self._parse_search(page, q, 1)
        if not page.from_cache:
            self._store(key1, page)
            self.http.state.set(STATE_QUERY, {"sig": sig, "ts": time.time()})
        return first

    def _fetch_page(self, q: PublicationQuery, sig: str, n: int) -> tuple[Page, SearchPage | None, ParseError | None]:
        page = self.http.get(SEARCH_PATH, page_params(n), referer=SEARCH_PATH, cache_ttl_hours=TTL_NONE,
                             cache_key=self._search_cache_key(sig, n))
        try:
            return page, self._parse_search(page, q, n), None
        except ParseError as exc:
            return page, None, exc

    @staticmethod
    def _consistent(result: SearchPage, first: SearchPage | None, n: int) -> bool:
        """Is ``result`` really page ``n`` of the query whose page 1 is ``first`` (None = unknown)?"""
        if result.page != n:
            return False
        if first is not None and first.total is not None and result.total is not None and result.total != first.total:
            return False
        if result.items:
            return True
        if first is None:
            # an empty page N is what a session that lost its query shows: verify with a fresh run
            return False
        per_page = result.per_page or 100
        return not (first.total is not None and first.total > (n - 1) * per_page)

    @staticmethod
    def _beyond_last(first: SearchPage, q: PublicationQuery, n: int) -> SearchPage[PublicationHit] | None:
        if first.pages is None or n <= first.pages:
            return None
        return SearchPage[PublicationHit](
            items=[], total=first.total, total_in_database=first.total_in_database, page=n,
            per_page=first.per_page, pages=first.pages, query=q.model_dump(mode="json"),
            source_url=None, from_cache=first.from_cache, fetched_at=first.fetched_at)

    def _search_publications(self, q: PublicationQuery) -> SearchPage[PublicationHit]:
        sig = self.search_key(q)
        n = q.page
        hit = self._peek(self._search_cache_key(sig, n), TTL_SEARCH)
        if hit is not None:
            return self._parse_search(hit, q, n)
        first: SearchPage | None = None
        fresh = False
        cur = self._current_query() if n > 1 else None
        current = cur is not None and cur[0] == sig
        cached_first = self._peek(self._search_cache_key(sig, 1), TTL_SEARCH) if current else None
        # The remembered query is trusted only with its page 1 at hand (totals check) or while it is fresh:
        # the same SUserID may have run another query in the browser since.
        if current and (cached_first is not None or time.time() - cur[1] < TTL_SEARCH * 3600):
            if cached_first is not None:
                first = self._parse_search(cached_first, q, 1)
        else:
            first = self._run_query(q, sig)
            fresh = True
            if n == 1:
                return first
        while True:
            if first is not None:
                empty = self._beyond_last(first, q, n)
                if empty is not None:
                    return empty
            page, result, error = self._fetch_page(q, sig, n)
            if result is not None and self._consistent(result, first, n):
                if first is not None:  # without page 1 the totals were not checked: do not keep the page
                    self._store(self._search_cache_key(sig, n), page)
                return result
            if fresh:
                if error is not None:
                    raise error
                raise ElibraryError(
                    f"eLIBRARY.RU вернул не ту страницу результатов (запрошена {n}, получена {result.page if result else '?'}, "
                    f"найдено {result.total if result else '?'} вместо {first.total if first else '?'}) даже после "
                    "повторного выполнения запроса. Возможно, тот же сеанс eLIBRARY.RU одновременно используется "
                    "в браузере или другим процессом; повторите позже.", url=page.url)
            log.info("Search page %s of query %s does not match the session state; re-running the query", n, sig)
            first = self._run_query(q, sig)
            fresh = True

    # ================================================================== publications: cards
    def get_publication(self, item_id: int | str | None = None, *, edn: str | None = None,
                        doi: str | None = None) -> Publication:
        """Publication card by eLIBRARY id (``item.asp?id=``), EDN (``item.asp?edn=``) or DOI (the
        site's ``/<DOI>`` redirect, resolved without following it and cached). Exactly one is needed.

        Raises ``NotFound`` when the card does not exist or the DOI is not in eLIBRARY.RU."""
        given = [v for v in (item_id, edn, doi) if v not in (None, "")]
        if len(given) != 1:
            raise ValueError("Укажите ровно один идентификатор публикации: item_id, edn или doi")
        if item_id not in (None, ""):
            return self._get_item(self._item_id(item_id))
        if edn:
            return self._get_item_by_edn(_norm_edn(edn))
        value = _norm_doi(str(doi))
        res = self._resolve_remote(parse_identifier("doi:" + value))
        return self._publication_from_resolution(res)

    def get_publication_any(self, identifier: str | int) -> Publication:
        """Publication card for any identifier: an eLIBRARY item id (``46371463``, ``id:46371463``), an
        EDN (``FYOAKL``, ``edn:...``, ``item.asp?edn=`` link), a DOI (bare, ``doi:`` or a doi.org link),
        an elibrary.ru item URL or another identifier the site resolves to a card (ISBN).

        Costs what :meth:`get_publication` costs (a DOI/ISBN lookup may add one cached request).
        Raises :class:`WrongTarget` (a ``ValueError``) without any request when the identifier points to
        an author, organization, journal or issue (URLs, ``author:``-style prefixes, SPIN, ORCID, ISSN,
        ROR); ``ValueError`` when it is not recognised; ``NotFound`` when the site does not know it."""
        if isinstance(identifier, bool):
            raise ValueError(f"Некорректный идентификатор публикации: {identifier!r}")
        if isinstance(identifier, int):
            return self._get_item(self._item_id(identifier))
        text = str(identifier).strip()
        # a digit string is an item id unless it is a valid ISBN-10/13 (parse_identifier decides)
        ident = parse_identifier(text)
        kind = ident["kind"]
        if kind == "edn":
            return self._get_item_by_edn(ident["value"])
        if kind in ("elibrary_id", "url"):
            if ident["target"] != "publication" or not ident["id"]:
                raise WrongTarget(text, ident["target"], ident["id"])
            return self._get_item(int(ident["id"]))
        if kind in _NON_PUBLICATION_KINDS:
            raise WrongTarget(text, _NON_PUBLICATION_KINDS[kind])
        res = self._resolve_remote(ident)  # doi, isbn
        return self._publication_from_resolution(res, identifier=text)

    @staticmethod
    def _item_id(item_id: int | str) -> int:
        text = str(item_id).strip()
        if not text.isdigit() or int(text) <= 0:
            raise ValueError(f"eLIBRARY ID публикации — положительное число, получено {item_id!r}")
        return int(text)

    def _get_item(self, item_id: int) -> Publication:
        pub, page = self._get(ITEM_PATH, [("id", str(item_id))], ttl=TTL_ITEM,
                              parse=lambda p: parse_publication(p.content, url=p.url))
        self._alias_item_page(pub, page)
        return pub

    def _get_item_by_edn(self, edn: str) -> Publication:
        pub, page = self._get(ITEM_PATH, [("edn", edn)], ttl=TTL_ITEM,
                              parse=lambda p: parse_publication(p.content, url=p.url))
        self._alias_item_page(pub, page)
        return pub

    def _alias_item_page(self, pub: Publication, page: Page) -> None:
        """Store a freshly fetched card under both ``item.asp?id=`` and ``item.asp?edn=`` so that asking
        for the same publication by the other identifier costs no request."""
        if page.from_cache or self.http.settings.offline:
            return
        aliases = [[("id", str(pub.id))]]
        if pub.edn and _EDN_PREFIXED_RE.match(pub.edn):
            aliases.append([("edn", pub.edn.upper())])
        for params in aliases:
            key = self._url_key(ITEM_PATH, params)
            if self.http.cache.get(key, TTL_ITEM * 3600) is None:
                self.http.cache.put(key, page)

    def _publication_from_resolution(self, res: Mapping[str, Any], *, identifier: str | None = None) -> Publication:
        if res.get("target") != "publication":
            raise WrongTarget(identifier or str(res.get("value")), res.get("target"), res.get("id"))
        if res.get("id"):
            return self._get_item(int(res["id"]))
        if res.get("edn"):
            return self._get_item_by_edn(str(res["edn"]))
        raise NotFound(f"Публикация {res.get('value')!r} не найдена на eLIBRARY.RU", url=res.get("url"))

    def get_fulltext_flag(self, item_id: int | str) -> FulltextFlag:
        """The full-text icon of a publication (``ispdf.asp?itemid=``, one request; cached 7 days)."""
        iid = self._item_id(item_id)
        flag, _ = self._get(ISPDF_PATH, [("itemid", str(iid))], ttl=TTL_PROFILE,
                            parse=lambda p: parse_fulltext_flag(p.content, item_id=iid),
                            referer=f"{ITEM_PATH}?id={iid}")
        return flag

    # ================================================================== identifiers
    def resolve(self, identifier: str) -> dict[str, Any]:
        """Where an identifier points: ``{"kind", "value", "target", "id", "url", "from_cache"}``.

        ``kind``: edn | doi | isbn | spin | orcid | issn | ror | elibrary_id | url (see
        :func:`parse_identifier`); ``target``: publication | author | journal | organization | issue.
        Bare ids and elibrary.ru page URLs are resolved locally (no request, existence not checked);
        EDN costs one request (``item.asp?edn=``, the card is cached for :meth:`get_publication`); the
        other identifiers cost one non-followed request to the short URL ``/<identifier>`` (more only
        while a new session is being set up). Answers are cached for a year, misses for a week.
        Raises ``NotFound`` when eLIBRARY.RU does not know the identifier, ``ValueError`` when it is
        not recognised."""
        ident = parse_identifier(identifier)
        if ident["kind"] in ("elibrary_id", "url"):
            return self._resolution(ident, ident["target"], ident["id"], from_cache=False)
        if ident["kind"] == "edn":
            return self._resolve_edn(ident)
        return self._resolve_remote(ident)

    @staticmethod
    def _resolution(ident: Mapping[str, Any], target: str | None, ident_id: int | None, *, from_cache: bool,
                    kind: str | None = None, url: str | None = None, **extra: Any) -> dict[str, Any]:
        out = {
            "kind": kind or ident["kind"],
            "value": ident["value"],
            "target": target,
            "id": ident_id,
            "url": url or (_canonical_url(target, ident_id) if target and ident_id else ident.get("url")),
            "from_cache": from_cache,
        }
        out.update({k: v for k, v in extra.items() if v is not None})
        return out

    def _resolve_edn(self, ident: Mapping[str, Any]) -> dict[str, Any]:
        key = f"resolve:edn:{ident['value']}"
        cached = self._json_get(key, TTL_IDENTIFIER)
        if cached is not None:
            return {**cached, "from_cache": True}
        pub = self._get_item_by_edn(ident["value"])
        res = self._resolution(ident, "publication", pub.id, from_cache=False)
        self._json_put(key, res)
        return res

    def _resolve_remote(self, ident: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve a short-URL identifier (DOI, ISBN, SPIN, ORCID, ISSN, ROR) via ``Location``."""
        kind, value = ident["kind"], ident["value"]
        hit_key, miss_key = f"resolve:{kind}:{value}", f"resolve-miss:{kind}:{value}"
        cached = self._json_get(hit_key, TTL_IDENTIFIER)
        if cached is not None:
            return {**cached, "from_cache": True}
        miss = self._json_get(miss_key, TTL_PROFILE)
        if miss is not None:
            raise NotFound(str(miss.get("message") or f"{value} не найден в eLIBRARY.RU"), url=miss.get("location"))
        with self.lock:
            res = self._follow_short_url(ident)
        if res.get("target") is None:
            message = self._miss_message(kind, value, res.get("location"))
            self._json_put(miss_key, {"message": message, "location": res.get("location")})
            raise NotFound(message, url=res.get("location"))
        self._json_put(hit_key, res)
        return res

    @staticmethod
    def _miss_message(kind: str, value: str, location: str | None) -> str:
        host = urlsplit(location).hostname if location else None
        where = f" (сайт перенаправляет на {host})" if host and not _is_elibrary_host(host) else ""
        return {
            "doi": f"Публикации с DOI {value} нет в eLIBRARY.RU{where}.",
            "isbn": f"Книги с ISBN {value} нет в eLIBRARY.RU{where}.",
            "orcid": f"Автора с ORCID {value} нет в eLIBRARY.RU{where}.",
            "spin": f"Автора с SPIN-кодом {value} нет в eLIBRARY.RU{where}.",
            "issn": f"Журнала с ISSN {value} нет в eLIBRARY.RU{where}.",
            "ror": f"Организации с ROR {value} нет в eLIBRARY.RU{where}.",
        }.get(kind, f"Идентификатор {value} не найден в eLIBRARY.RU{where}.")

    def _check_location_gate(self, page: Page, url: str) -> None:
        """A non-followed redirect to a challenge page is a challenge: trip the breaker like http.py does."""
        if not page.location:
            return
        target = urljoin(url, page.location)
        low = target.lower()
        trip = getattr(self.http, "_trip_breaker", None)
        if "page_captcha.asp" in low:
            if trip is not None:
                trip(Gate.CAPTCHA, target, self.http.settings.breaker_minutes)
            raise CaptchaRequired(url, captcha_url=target)
        if "ip_blocked.asp" in low:
            if trip is not None:
                trip(Gate.IP_BLOCKED, target, 24 * 60)
            raise AccessBlocked("elibrary.ru заблокировал доступ для этого IP (ip_blocked). Новые запросы "
                                "приостановлены на 24 ч; напишите в support@elibrary.ru.", url=url)
        if "ip_restricted.asp" in low:
            if trip is not None:
                trip(Gate.IP_RESTRICTED, target, self.http.settings.breaker_minutes)
            raise IpRestricted("elibrary.ru ограничил анонимный доступ с этого IP (ip_restricted.asp). Войдите: "
                               "`elibrary auth login` или импортируйте cookies вошедшей сессии.", url=url)

    def _follow_short_url(self, ident: Mapping[str, Any]) -> dict[str, Any]:
        """Ask ``/<identifier>`` without following redirects, at most :data:`MAX_HOPS` times (session
        bootstrap via ``start_session.asp`` included). Never requests a non-elibrary host."""
        url = self.http.absolute(ident["path"])
        visits: dict[str, int] = {}
        for _ in range(MAX_HOPS):
            visits[url] = visits.get(url, 0) + 1
            page = self.http.get(url, follow_redirects=False, cache_ttl_hours=TTL_NONE)
            self._check_location_gate(page, url)
            if page.status in _REDIRECT_STATUSES and page.location:
                loc = urljoin(url, page.location)
                host = (urlsplit(loc).hostname or "").lower()
                if not _is_elibrary_host(host):
                    return {"target": None, "location": loc}
                tgt = elibrary_target(loc)
                if tgt is not None:
                    kind = ident["kind"]
                    if kind in ("issn", "spin"):  # the site decides on an ISSN/SPIN collision
                        kind = "spin" if tgt["target"] == "author" else "issn" if tgt["target"] == "journal" else kind
                    return self._resolution(ident, tgt["target"], tgt.get("id"), from_cache=False, kind=kind,
                                            url=tgt["url"], edn=tgt.get("edn"), location=loc)
                page_name = urlsplit(loc).path.rsplit("/", 1)[-1].lower()
                # the session bootstrap returns to the short URL once; a second return is a loop
                if page_name in ("", "defaultx.asp", "default.asp", "index.asp") or visits.get(loc, 0) >= 2:
                    return {"target": None, "location": loc}
                url = loc  # e.g. start_session.asp?rpage=<short URL>: one more hop sets up the session
                continue
            if page.status == 200:
                return self._target_from_page(ident, page, url)
            return {"target": None, "location": url}
        raise ElibraryError(f"Слишком много перенаправлений при поиске {ident['value']} на eLIBRARY.RU", url=url)

    def _target_from_page(self, ident: Mapping[str, Any], page: Page, url: str) -> dict[str, Any]:
        """The short URL answered with a page instead of a redirect: recognise an item card."""
        try:
            pub = parse_publication(page.content, url=url)
        except (ParseError, NotFound):
            pub = None
        if pub is not None:
            self._alias_item_page(pub, page)
            return self._resolution(ident, "publication", pub.id, from_cache=False, location=url)
        m = re.search(r"AuthorID:\s*(?:<[^>]*>\s*)*(\d+)", page.text)
        if m:
            kind = "spin" if ident["kind"] in ("issn", "spin") else ident["kind"]
            return self._resolution(ident, "author", int(m.group(1)), from_cache=False, kind=kind, location=url)
        raise ParseError(f"Не удалось определить, какую страницу eLIBRARY.RU открывает {ident['value']}", url=url)

    # ================================================================== authors
    def search_authors(self, query: AuthorQuery | Mapping[str, Any] | None = None, /,
                       **params: Any) -> SearchPage[AuthorHit]:
        """Author search (``POST authors.asp``), 100 per page; paging re-sends the form (no server state).

        The site filters by organization only through ``orgid``: an ``org_name`` without ``org_id`` is
        resolved with **one** organization search (``orgs.asp``, sorted by publications, cached 24 h;
        see :meth:`_resolve_org_name`), then the form is sent with that id (and the organization's own
        name, as the browser does). The chosen organization is reported in
        ``result.query["org_resolved"]`` (``id``, ``name``, ``town``, ``publications``, ``match``,
        ``candidates``, ``query``); ``NotFound`` when no organization matches."""
        if isinstance(query, AuthorQuery) and not params:
            q = query
        else:
            data = _merged_query(AuthorQuery, query, params)
            q = AuthorQuery.model_validate(data, context={CONTEXT_RESOLVE_ORG_NAME: True})
        resolved: dict[str, Any] | None = None
        if q.org_name and not q.org_id:
            resolved = self._resolve_org_name(q.org_name)
            q = q.model_copy(update={"org_id": resolved["id"], "org_name": resolved["name"]})
        form = build_author_search_form(q)
        result, page = self._post(AUTHORS_PATH, form, ttl=TTL_SEARCH, referer=AUTHORS_PATH,
                                  parse=lambda p: parse_author_search(p.content, page=q.page, query=q, url=p.url))
        if resolved is not None:
            result.query["org_resolved"] = resolved
        return _mark(result, page)

    def _resolve_org_name(self, name: str) -> dict[str, Any]:
        """The organization an author search by organization name means: one ``search_organizations``
        call (``name``, sorted by publications, descending; page 1 = up to 20 hits). An organization
        whose normalised name (case, «ё», quotes, punctuation ignored; or its abbreviation in
        parentheses) equals ``name`` wins; otherwise the hit with the most publications.

        Returns ``{"id", "name", "town", "publications", "match": "exact"|"most_publications",
        "candidates", "query"}``; raises ``NotFound`` (Russian) when the search finds nothing."""
        orgs = self.search_organizations(name=name, sort="publications", order="desc")
        hits = list(orgs.items)
        if not hits:
            raise NotFound(
                f"Организация «{name}» не найдена в eLIBRARY.RU: уточните название (поиск организаций: "
                "`elibrary search orgs` / elibrary_search_organizations) или укажите ID организации (org_id).",
                url=orgs.source_url)
        want = _norm_org_name(name)
        exact = [h for h in hits if want in _org_name_keys(h.name)]
        best = max(exact or hits, key=lambda h: h.publications if h.publications is not None else -1)
        return {
            "id": best.id,
            "name": best.name,
            "town": best.town,
            "publications": best.publications,
            "match": "exact" if exact else "most_publications",
            "candidates": orgs.total if orgs.total is not None else len(hits),
            "query": name,
        }

    def get_author(self, author_id: int) -> AuthorProfile:
        """Author profile with bibliometric indicators (``author_profile.asp?id=``)."""
        params = author_profile_params(author_id)
        profile, _ = self._get(AUTHOR_PROFILE_PATH, params, ttl=TTL_PROFILE,
                               parse=lambda p: parse_author_profile(p.content, url=p.url))
        return profile

    def get_author_publications(self, author_id: int, page: int = 1, *, sort: str = "date", order: str = "desc",
                                include_references: bool = True, merge_versions: bool = True,
                                unlinked: bool = False) -> AuthorPublicationsPage:
        """Publications of an author (``author_items.asp``), 100 per page.

        The site's default view (page 1, newest first, reference-only records shown, versions merged)
        is the plain GET link of the profile; other pages/sortings POST the page's form, with the
        ``authorhash`` taken from a cached first page when there is one."""
        q = AuthorPublicationsQuery(author_id=author_id, page=page, sort=sort, order=order,  # type: ignore[arg-type]
                                    include_references=include_references, merge_versions=merge_versions,
                                    unlinked=unlinked)
        first_params = author_items_params(q.author_id, show_refs=q.include_references)
        default_view = q.sort == "date" and q.order == "desc" and q.merge_versions and not q.unlinked

        def parse(p: Page) -> AuthorPublicationsPage:
            return parse_author_publications(p.content, page=q.page, url=p.url)

        if q.page == 1 and default_view:
            result, pg = self._get(AUTHOR_ITEMS_PATH, first_params, ttl=TTL_PROFILE, parse=parse,
                                   referer=f"{AUTHOR_PROFILE_PATH}?id={q.author_id}")
            return _mark(result, pg)
        author_hash = ""
        cached_first = self.http.cached("GET", self.http.build_url(AUTHOR_ITEMS_PATH, first_params),
                                        ttl_hours=TTL_PROFILE)
        if cached_first is not None:
            with contextlib.suppress(ParseError, NotFound):
                author_hash = parse(cached_first).author_short_name or ""
        form = build_author_publications_form(q, author_hash=author_hash)
        result, pg = self._post(AUTHOR_ITEMS_PATH, form, ttl=TTL_PROFILE, parse=parse,
                                referer=self.http.build_url(AUTHOR_ITEMS_PATH, first_params))
        return _mark(result, pg)

    # ================================================================== organizations
    def search_organizations(self, query: OrgQuery | Mapping[str, Any] | None = None, /,
                             **params: Any) -> SearchPage[OrgHit]:
        """Organization search (``POST orgs.asp``), 20 per page; paging re-sends the form."""
        q = _as_query(OrgQuery, query, params)
        spec = build_org_search_request(q)
        result, page = self._post(spec.path, spec.params, ttl=TTL_SEARCH, referer=ORGS_PATH,
                                  parse=lambda p: parse_org_search(p.content, page=q.page, query=q, url=p.url))
        return _mark(result, page)

    def get_organization(self, org_id: int, *, with_card: bool = False,
                         with_descriptions: bool = False) -> OrgProfile:
        """Organization profile (``org_profile.asp?id=``: indicators, tables).

        ``with_card=True`` spends one more request on ``org_about.asp`` (full name, address, type,
        contacts) and merges it; ``with_descriptions=True`` keeps the indicator tooltips."""
        spec = build_org_profile_request(org_id)
        profile, _ = self._get(spec.path, spec.params, ttl=TTL_PROFILE,
                               parse=lambda p: parse_org_profile(p.content, url=p.url,
                                                                 with_descriptions=with_descriptions))
        if with_card:
            about = build_org_about_request(org_id)
            try:
                card, _ = self._get(about.path, about.params, ttl=TTL_PROFILE,
                                    parse=lambda p: parse_org_about(p.content, url=p.url))
            except ParseError as exc:
                log.warning("org_about.asp of %s not recognised: %s", org_id, exc)
                profile.notes.append(f"Карточка организации (org_about.asp) не распознана: {exc}")
            else:
                profile = profile.merge(card)
        return profile

    def get_organization_publications(self, org_id: int, page: int = 1, *, show_option: str | int | None = None,
                                      show_sotr: str | int | None = None, show_refs: bool | None = None,
                                      years: list[int] | None = None, sort: str | None = None,
                                      order: str | None = None) -> OrgPublicationsPage:
        """Publications of an organization (``org_items.asp?orgsid=``), 100 per page.
        Options: see :func:`queries.organizations.build_org_items_request`."""
        spec = build_org_items_request(org_id, page, show_option=show_option, show_sotr=show_sotr,
                                       show_refs=show_refs, years=years, sortorder=sort, order=order)
        # the last pages of large organizations take more than a minute: one long wait (never a retry)
        result, pg = self._get(spec.path, spec.params, ttl=TTL_PROFILE,
                               parse=lambda p: parse_organization_publications(p.content, page=page, url=p.url),
                               referer=f"/org_profile.asp?id={int(org_id)}",
                               timeout=max(self.settings.timeout, self.settings.heavy_timeout))
        return _mark(result, pg)

    # ================================================================== journals
    def search_journals(self, query: JournalQuery | Mapping[str, Any] | None = None, /,
                        **params: Any) -> SearchPage[JournalHit]:
        """Journal catalog search (``POST titles.asp``); paging re-sends the form."""
        q = _as_query(JournalQuery, query, params)
        form = build_journal_search_form(q)
        result, page = self._post(TITLES_PATH, form, ttl=TTL_SEARCH, referer=TITLES_PATH,
                                  parse=lambda p: parse_journal_search(p.content, page=q.page, query=q))
        return _mark(result, page)

    def get_journal(self, journal_id: int, *, english: bool = False, with_indicators: bool = False) -> JournalProfile:
        """Journal profile (``title_about_new.asp?id=``; ``english=True`` -> the English description).

        ``with_indicators=True`` spends one more request on ``title_profile.asp`` (impact factors,
        Science Index rating) and merges it into the profile."""
        params = [("id", str(int(journal_id)))] + ([("langid", "2")] if english else [])
        profile, _ = self._get(JOURNAL_PROFILE_PATH, params, ttl=TTL_JOURNAL,
                               parse=lambda p: parse_journal_profile(p.content, url=p.url))
        if with_indicators:
            try:
                ind, _ = self._get(JOURNAL_ANALYSIS_PATH, [("id", str(int(journal_id)))], ttl=TTL_JOURNAL,
                                   parse=lambda p: parse_journal_indicators(p.content, url=p.url),
                                   referer=f"{JOURNAL_PROFILE_PATH}?id={int(journal_id)}")
            except ParseError as exc:
                log.warning("title_profile.asp of %s not recognised: %s", journal_id, exc)
                profile.extra["Показатели (title_profile.asp)"] = f"не распознаны: {exc}"
            else:
                profile = profile.with_indicators(ind)
        return profile

    def get_journal_issue(self, *, journal_id: int | None = None, issue_id: int | None = None) -> IssueContents:
        """Contents of an issue: ``issue_id`` (``contents.asp?id=``) or the latest issue of
        ``journal_id`` (``contents.asp?titleid=``, cached for 7 days only)."""
        if issue_id:
            params, ttl = [("id", str(int(issue_id)))], TTL_JOURNAL
        elif journal_id:
            params, ttl = [("titleid", str(int(journal_id)))], TTL_PROFILE
        else:
            raise ValueError("Укажите journal_id (последний выпуск журнала) или issue_id (конкретный выпуск)")
        contents, _ = self._get(CONTENTS_PATH, params, ttl=ttl,
                                parse=lambda p: parse_issue_contents(p.content, url=p.url))
        return contents

    # ================================================================== full text, export, Zotero
    def _publication_ref(self, ref: Any, *, full: bool = True) -> Publication | PublicationHit:
        """A publication from an id, an identifier string, a dict with ``id`` or a model.
        ``full=True`` turns a ``PublicationHit`` into the full card."""
        if isinstance(ref, Publication):
            return ref
        if isinstance(ref, PublicationHit):
            return self._get_item(ref.id) if full else ref
        if isinstance(ref, Mapping):
            if ref.get("id") is None:
                raise ValueError("Ожидается публикация с полем id")
            return self._get_item(self._item_id(ref["id"]))
        if isinstance(ref, (int, str)):
            return self.get_publication_any(ref)
        raise ValueError(f"Некорректный идентификатор публикации: {ref!r}")

    def get_fulltext(self, item_id: int | str | Publication, *, offset: int = 0, max_chars: int = 20000,
                     sources: list[str] | tuple[str, ...] | None = None, refresh: bool = False) -> FullText:
        """Full text of a publication, one chunk of ``max_chars`` from ``offset``.

        Reads the card (cached 30 days), builds a ``FulltextRequest`` (eLIBRARY id, EDN, DOI, titles,
        authors, year, journal) and runs :func:`elibrary_cli.fulltext.get_fulltext` with this client's
        sessions: the eLIBRARY PDF only for a signed-in session, then the open sources. Later chunks
        come from the local text cache. Raises ``NotFound`` (with ``.attempts``) when nothing worked."""
        from elibrary_cli import fulltext as ft

        pub = self._publication_ref(item_id)
        req = ft.FulltextRequest.from_publication(pub)
        return ft.get_fulltext(req, http=self.http, net=self.net, settings=self.settings, sources=sources,
                               offset=offset, max_chars=max_chars, refresh=refresh)

    def export(self, item_ids: Any, fmt: str, *, as_text: bool = False, fix_caps: bool = True,
               citekey_edn: bool = False, tags: Iterable[str] = (),
               zotero_mcp: bool = False) -> str | list[dict[str, Any]]:
        """Export publications (ids, identifier strings, ``Publication``/``PublicationHit`` models or a whole
        ``SearchPage``) as ``bibtex``, ``ris``, ``csl`` or ``zotero``; rows are exported as they are,
        without fetching their cards.

        BibTeX/RIS are returned as text; CSL-JSON and Zotero JSON as a list of dicts unless
        ``as_text=True``. ``zotero_mcp=True`` makes CSL-JSON zotero-mcp friendly (only the keys its
        ``zotero_add_item`` maps, the rest moved into ``note``; see :func:`exports.to_csl_json`).
        Unknown formats raise ``ValueError`` before any request."""
        fmt_n = exports.normalize_format(fmt)
        pubs = [self._publication_ref(x, full=False) for x in _as_list(item_ids)]
        if not pubs:
            raise ValueError("Не указаны публикации для экспорта")
        text = exports.export(pubs, fmt_n, fix_caps=fix_caps, citekey_edn=citekey_edn, tags=tags,
                              zotero_mcp=zotero_mcp)
        if fmt_n in ("csl", "zotero") and not as_text:
            return json.loads(text)
        return text

    def zotero_add(self, item_ids: Any, *, collection: str | None = None, tags: Iterable[str] | None = None,
                   with_pdf: bool = False, dedupe: bool = True, backend: str | None = None) -> list[dict[str, Any]]:
        """Save publications to Zotero (local API, connector or Web API, see ``integrations.zotero``).

        ``with_pdf=True`` attaches the PDF found by :meth:`get_fulltext` when the text came from a PDF.
        Returns one dict per item (``ZoteroWriter.add`` results plus ``elibrary_id`` and ``pdf_note``).

        Zotero's availability is checked first, so an unreachable Zotero costs no eLIBRARY.RU requests."""
        ids = _as_list(item_ids)
        if not ids:
            raise ValueError("Не указаны публикации для сохранения в Zotero")
        writer = self._zotero
        owned = writer is None
        if writer is None:
            from elibrary_cli.integrations.zotero import ZoteroWriter

            writer = ZoteroWriter(settings=self.settings)
        try:
            self._zotero_preflight(writer, backend)
            return self._zotero_add(writer, ids, collection=collection, tags=tags, with_pdf=with_pdf,
                                    dedupe=dedupe, backend=backend)
        finally:
            if owned:
                writer.close()

    @staticmethod
    def _zotero_preflight(writer: Any, backend: str | None) -> None:
        """Raise ZoteroUnavailable when nothing can be written (same rule as the CLI and MCP tool)."""
        if not hasattr(writer, "ping"):
            return
        from elibrary_cli.integrations.zotero import ZoteroUnavailable

        state = writer.ping()
        available = list(state.get("available_backends") or [])
        if backend in (None, "auto"):
            ok = bool(available)
        else:
            local_off = (state.get("config") or {}).get("local") is False
            ok = backend in available or (backend != "web" and local_off)
        if ok:
            return
        hints = " ".join(dict.fromkeys(h for h in state.get("hints") or [] if h))
        head = (f"Способ записи в Zotero «{backend}» недоступен" if backend not in (None, "auto")
                else "Нет доступного способа записи в Zotero")
        raise ZoteroUnavailable(f"{head}; ничего не сохранено, запросов к eLIBRARY.RU не было. {hints}".rstrip())

    def _zotero_add(self, writer: Any, ids: list[Any], *, collection: str | None, tags: Iterable[str] | None,
                    with_pdf: bool, dedupe: bool, backend: str | None) -> list[dict[str, Any]]:
        pubs = [self._publication_ref(x) for x in ids]
        pdf_paths: dict[int, str] = {}
        notes: dict[int, str] = {}
        if with_pdf:
            for i, pub in enumerate(pubs):
                try:
                    text = self.get_fulltext(pub, max_chars=1000)
                except ElibraryError as exc:
                    notes[i] = f"PDF не найден: {exc.message.splitlines()[0][:300]}"
                    continue
                if text.pdf_path and Path(text.pdf_path).is_file():
                    pdf_paths[i] = text.pdf_path
                else:
                    notes[i] = f"Полный текст найден ({text.source}), но не в виде PDF: файл не прикреплён"
        results = writer.add(pubs, collection=collection, tags=list(tags or []), pdf_paths=pdf_paths,
                             dedupe=dedupe, backend=backend)
        for r in results:
            idx = r.get("index")
            if isinstance(idx, int) and 0 <= idx < len(pubs):
                r["elibrary_id"] = pubs[idx].id
                r["pdf_note"] = notes.get(idx)
        return results

    # ================================================================== session
    def status(self) -> dict[str, Any]:
        """HTTP layer state (cookies names, budgets, breaker, cache) + account and current query."""
        st = self.http.status()
        st["logged_in"] = bool(st.get("account"))
        cur = self._current_query()
        st["current_query"] = cur[0] if cur is not None else None
        st["breaker_url"] = self.http.state.get("breaker.url") if st.get("breaker_open") else None
        return st

    def set_cookies(self, raw: str, user_agent: str | None = None, *, replace: bool = True) -> dict[str, Any]:
        """Import cookies copied from the browser (``copy(document.cookie)``, header, JSON, cookies.txt).
        Returns cookie names and warnings only, never values."""
        return auth.import_cookies(self.http, raw, user_agent=user_agent, replace=replace)

    def check_session(self, *, force: bool = False) -> SessionInfo:
        """One uncached request to see the session state. An open circuit breaker is lifted for it only
        after a cookie import with ``SUserID`` (captcha / ip_restricted; never a ban) or with ``force``
        (a human's explicit decision, CLI ``elibrary auth check --force``); see :func:`auth.check_session`."""
        return auth.check_session(self.http, force=force)

    def login(self, login: str | None = None, password: str | None = None, *, remember: bool = True) -> SessionInfo:
        """Sign in with a password (defaults: ``ELIBRARY_LOGIN`` / ``ELIBRARY_PASSWORD``)."""
        user = login if login is not None else self.settings.login
        secret = password if password is not None else self.settings.password
        return auth.login(self.http, user or "", secret or "", remember=remember)

    def logout(self, *, remember: bool = False) -> SessionInfo:
        """Sign out (``end_session.asp``); ``remember=True`` keeps the site's remembered login."""
        return auth.logout(self.http, remember=remember)

    def browser_session(self, purpose: str = "captcha", *, url: str | None = None, timeout_s: float = 600,
                        channel: str | None = None) -> SessionInfo:
        """Open a visible browser with this session so that the *user* passes the captcha or signs in;
        the code only waits and then imports the browser's cookies (extra ``[browser]``)."""
        return auth.browser_session(self.http, url=url, purpose=purpose, timeout_s=timeout_s,  # type: ignore[arg-type]
                                    channel=channel)

    def clear_session(self) -> dict[str, Any]:
        """Forget cookies, account, current query and User-Agent; returns :meth:`status`."""
        self.http.clear_session()
        return self.status()

    def cache_stats(self) -> dict[str, Any]:
        """Sizes of the page cache, the full-text cache and the downloaded PDFs."""
        stats: dict[str, Any] = dict(self.http.cache.stats())
        stats["cache_db"] = str(self.settings.cache_db)
        ft_dir = self.settings.cache_dir / "fulltext"
        texts = list(ft_dir.glob("*.txt")) if ft_dir.is_dir() else []
        stats["fulltext_dir"] = str(ft_dir)
        stats["fulltext_texts"] = len(texts)
        stats["fulltext_bytes"] = sum(p.stat().st_size for p in texts)
        dl = self.settings.downloads_dir
        pdfs = list(dl.glob("*.pdf")) if dl.is_dir() else []
        stats["downloads_dir"] = str(dl)
        stats["downloads"] = len(pdfs)
        stats["downloads_bytes"] = sum(p.stat().st_size for p in pdfs)
        return stats

    def clear_cache(self, *, fulltext: bool = False, external: bool = False) -> dict[str, Any]:
        """Empty the page cache (and, on request, the extracted full texts and the open-access API cache).
        Downloaded PDFs are kept."""
        out: dict[str, Any] = {"pages_removed": self.http.cache.clear()}
        if fulltext:
            ft_dir = self.settings.cache_dir / "fulltext"
            removed = 0
            if ft_dir.is_dir():
                for p in list(ft_dir.glob("*.txt")) + list(ft_dir.glob("*.json")):
                    with contextlib.suppress(OSError):
                        p.unlink()
                        removed += p.suffix == ".txt"
            out["fulltext_removed"] = removed
        if external:
            if self._net is not None and getattr(self._net, "cache", None) is not None:
                out["external_removed"] = self._net.cache.clear()
            else:
                path = self.settings.cache_dir / "external-cache.sqlite3"
                if path.is_file():
                    cache = PageCache(path)
                    try:
                        out["external_removed"] = cache.clear()
                    finally:
                        cache.close()
                else:
                    out["external_removed"] = 0
        return out

    # ================================================================== reference data
    @staticmethod
    def list_options(name: str) -> list[dict[str, Any]]:
        """Options of a search-form select (see :mod:`elibrary_cli.refdata`)."""
        return refdata.list_options(name)

    @staticmethod
    def find_options(name: str, text: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Options of ``name`` matching ``text`` (exact, prefix, substring)."""
        return refdata.find_options(name, text, limit=limit)

    @staticmethod
    def option_lists() -> list[dict[str, Any]]:
        """All reference lists with descriptions (see :func:`refdata.describe_options`)."""
        return refdata.describe_options()
