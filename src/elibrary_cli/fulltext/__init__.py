"""Full text of a publication, paged and cached.

:func:`get_fulltext` tries the sources in order (default: ``elibrary`` when the session is signed
in, then ``cyberleninka``, ``unpaywall``, ``openalex``, ``crossref``, ``publisher``), stops at the
first text that passes the quality checks, caches the extracted text on disk
(``<cache_dir>/fulltext/<key>.txt`` + ``.json``) and returns one chunk of it
(:class:`FullText`, ``offset``/``max_chars``/``next_offset``). Paging calls hit the cache only.

Nothing here talks to elibrary.ru except through the :class:`~elibrary_cli.http.ElibraryHTTP`
passed in (``http``); everything else goes through :class:`~elibrary_cli.fulltext.net.ExternalHTTP`.
Publisher pages, PDFs and CyberLeninka articles are fetched with ``robots=True`` (robots.txt honoured;
a forbidden URL is recorded as a ``skipped`` attempt); the JSON APIs (Crossref, OpenAlex, Unpaywall,
DataCite, Handle) are not checked. CyberLeninka is read from the article page derived from the title;
its search API only with ``ELIBRARY_CYBERLENINKA_SEARCH=1`` and its PDFs never.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from elibrary_cli.config import Settings
from elibrary_cli.errors import (
    AccessBlocked,
    CaptchaRequired,
    ElibraryError,
    LoginRequired,
    NotFound,
    OfflineCacheMiss,
    ParseError,
    RateLimited,
)
from elibrary_cli.fulltext import sources as src
from elibrary_cli.fulltext.elibrary import (
    LOGIN_HELP,
    download_elibrary_pdf,
    find_elibrary_pdf_link,
    find_elibrary_pdf_links,
    fulltext_status,
    is_logged_in,
    session_user,
)
from elibrary_cli.fulltext.net import (
    ExternalChallenge,
    ExternalHTTP,
    ExternalHTTPError,
    NotAPdf,
    RedirectToElibrary,
    RobotsDisallowed,
    is_cyberleninka_host,
    is_elibrary_host,
)
from elibrary_cli.fulltext.pdf import DEFAULT_MAX_PAGES as PDF_MAX_PAGES
from elibrary_cli.fulltext.pdf import PdfText, extract_pdf_text
from elibrary_cli.http import TTL_ITEM, ElibraryHTTP, PageCache
from elibrary_cli.models.base import ElibModel

log = logging.getLogger(__name__)

SourceName = Literal["elibrary", "cyberleninka", "unpaywall", "openalex", "crossref", "publisher"]
#: All source names, in the default order.
SOURCE_NAMES: tuple[str, ...] = ("elibrary", "cyberleninka", "unpaywall", "openalex", "crossref", "publisher")
DEFAULT_SOURCES: tuple[str, ...] = SOURCE_NAMES
DEFAULT_MAX_CHARS = 20000
#: A text shorter than this is not accepted as a full text (abstract pages, stubs).
MIN_TEXT_CHARS = 800
#: Below this many characters per PDF page the PDF is treated as a scan without a text layer.
MIN_PDF_CHARS_PER_PAGE = 100

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_SOURCES",
    "LOGIN_HELP",
    "SOURCE_NAMES",
    "FullText",
    "FulltextAttempt",
    "FulltextRequest",
    "PdfText",
    "default_sources",
    "download_elibrary_pdf",
    "extract_pdf_text",
    "find_elibrary_pdf_link",
    "find_elibrary_pdf_links",
    "fulltext_cache_dir",
    "fulltext_status",
    "get_fulltext",
    "is_logged_in",
    "session_user",
    "slice_text",
]


# ------------------------------------------------------------------------------------ models
class FulltextRequest(ElibModel):
    """What is known about the publication whose full text is wanted (any subset works)."""

    elibrary_id: int | None = Field(None, description="eLIBRARY item id (item.asp?id=...)")
    edn: str | None = Field(None, description="EDN code, e.g. 'FYOAKL'")
    doi: str | None = Field(None, description="DOI, normalised to lower case without the doi.org prefix")
    title: str | None = Field(None, description="Title as on eLIBRARY.RU (usually Russian)")
    title_en: str | None = Field(
        None,
        description="English title (eLIBRARY block 'ОПИСАНИЕ НА АНГЛИЙСКОМ ЯЗЫКЕ'); Crossref and OpenAlex "
        "often know Russian papers only under it",
    )
    authors: list[str] = Field(default_factory=list, description="Authors, e.g. 'Петров В.А.' (any form works)")
    year: int | None = None
    journal: str | None = None
    language: str | None = Field(None, description="Language of the publication, e.g. 'русский' or 'ru'")

    @field_validator("doi")
    @classmethod
    def _norm_doi(cls, v: str | None) -> str | None:
        return src.normalize_doi(v) if v else None

    @field_validator("edn")
    @classmethod
    def _norm_edn(cls, v: str | None) -> str | None:
        return v.strip().upper() or None if v else None

    def cache_keys(self) -> list[str]:
        """File-system safe keys of the text cache, most specific first."""
        keys = []
        if self.elibrary_id:
            keys.append(f"elibrary-{self.elibrary_id}")
        if self.edn and re.fullmatch(r"[A-Z0-9]{3,12}", self.edn):
            keys.append(f"edn-{self.edn}")
        if self.doi:
            keys.append("doi-" + hashlib.sha1(self.doi.encode()).hexdigest()[:16])
        if not keys and (self.title or self.title_en):
            basis = f"{src.normalize_title(self.title or self.title_en)}|{self.year or ''}"
            keys.append("title-" + hashlib.sha1(basis.encode()).hexdigest()[:16])
        return keys

    @classmethod
    def from_publication(cls, obj: Any) -> FulltextRequest:
        """Build a request from a publication model or dict (best effort, unknown keys ignored).

        Understands ``id``/``elibrary_id``/``item_id``, ``edn``, ``doi``, ``title``, ``title_en`` (or
        ``english.title``), ``authors`` (strings or objects with ``name``/``full_name``), ``year``,
        ``journal`` (string or object with ``title``/``name``) / ``journal_title``, ``language``.
        """
        data: Mapping[str, Any]
        if isinstance(obj, BaseModel):
            data = obj.model_dump()
        elif isinstance(obj, Mapping):
            data = obj
        else:
            data = {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_")}

        def pick(*names: str) -> Any:
            for n in names:
                if data.get(n) not in (None, "", []):
                    return data[n]
            return None

        def name_of(x: Any) -> str | None:
            if isinstance(x, str):
                return x
            if isinstance(x, Mapping):
                return x.get("name") or x.get("full_name") or x.get("short_name")
            return getattr(x, "name", None)

        journal = pick("journal", "journal_title", "source_title")
        if isinstance(journal, Mapping):
            journal = journal.get("title") or journal.get("name")
        english = data.get("english") if isinstance(data.get("english"), Mapping) else {}
        year = pick("year")
        return cls(
            elibrary_id=pick("elibrary_id", "item_id", "id"),
            edn=pick("edn"),
            doi=pick("doi"),
            title=pick("title"),
            title_en=pick("title_en", "english_title") or (english or {}).get("title"),
            authors=[a for a in (name_of(x) for x in pick("authors") or []) if a],
            year=int(year) if isinstance(year, (int, str)) and str(year).isdigit() else None,
            journal=journal if isinstance(journal, str) else None,
            language=pick("language"),
        )


class FulltextAttempt(ElibModel):
    """One step of the search for a full text."""

    source: str = Field(description="elibrary | cyberleninka | unpaywall | openalex | crossref | publisher | cache")
    status: str = Field(
        description="ok | skipped | not_found | no_match | rejected | login_required | blocked | error | info"
    )
    detail: str = ""
    url: str | None = None


class FullText(ElibModel):
    """A chunk of the full text of a publication with its provenance."""

    text: str = Field(description="This chunk of the full text (paragraphs separated by blank lines)")
    offset: int = Field(0, description="Offset of this chunk in the whole text, characters")
    next_offset: int | None = Field(None, description="Pass as `offset` to get the next chunk; null at the end")
    total_chars: int = Field(description="Length of the whole text, characters")
    truncated: bool = Field(description="True when more text follows this chunk")
    source: SourceName = Field(description="Where the text comes from")
    source_url: str | None = Field(None, description="Page or file the text was taken from")
    pdf_path: str | None = Field(None, description="Local copy of the PDF, when the text comes from a PDF")
    format: Literal["pdf", "html"] = Field(description="pdf: extracted from a PDF; html: taken from a web page")
    match_score: float | None = Field(
        None, description="1.0 = identified by id/DOI; below 1 = fuzzy title/author/year match"
    )
    attempts: list[FulltextAttempt] = Field(default_factory=list, description="What was tried, in order")
    page_count: int | None = Field(None, description="Pages of the PDF (or of the source document)")
    engine: str | None = Field(None, description="Text extractor: pypdf, pdfminer or html")
    license: str | None = None
    doi: str | None = None
    title: str | None = Field(None, description="Title of the matched record in the source")
    from_cache: bool = Field(False, description="True when the text came from the local full-text cache")


# --------------------------------------------------------------------------------- helpers
_SENTENCE_END_RE = re.compile(r"[.!?…][»\")\]]*\s+")


def slice_text(text: str, offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, int | None]:
    """Cut ``text[offset:]`` to at most ``max_chars`` at a paragraph, line, sentence or word boundary.

    Returns ``(chunk, next_offset)``; ``next_offset`` is ``None`` at the end. Chunks are contiguous:
    concatenating all of them gives the text back. A boundary is searched in the second half of the
    window; without one the cut is hard.
    """
    if max_chars <= 0:
        raise ValueError("max_chars должен быть положительным числом")
    n = len(text)
    offset = min(max(0, offset), n)
    end = offset + max_chars
    if end >= n:
        return text[offset:], None
    window = text[offset:end]
    floor = max_chars // 2
    cut = -1
    for sep in ("\n\n", "\n"):
        i = window.rfind(sep, floor)
        if i != -1:
            cut = i + len(sep)
            break
    if cut == -1:
        ends = [m.end() for m in _SENTENCE_END_RE.finditer(window) if m.end() >= floor]
        if ends:
            cut = ends[-1]
    if cut == -1:
        i = window.rfind(" ", floor)
        cut = i + 1 if i != -1 else max_chars
    return text[offset:offset + cut], offset + cut


def fulltext_cache_dir(settings: Settings) -> Path:
    """Directory of the extracted-text cache."""
    return settings.cache_dir / "fulltext"


def default_sources(settings: Settings | None = None) -> tuple[str, ...]:
    """Source order: ``settings.fulltext_sources`` if present, else ``ELIBRARY_FULLTEXT_SOURCES``
    (comma separated), else :data:`DEFAULT_SOURCES`."""
    configured = getattr(settings, "fulltext_sources", None) if settings is not None else None
    if configured:
        return _validate_sources(configured)
    raw = os.environ.get("ELIBRARY_FULLTEXT_SOURCES", "").strip()
    if raw:
        return _validate_sources(raw.split(","))
    return DEFAULT_SOURCES


def _validate_sources(names: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(names, str):
        names = names.split(",")
    out: list[str] = []
    for raw in names:
        name = raw.strip().lower()
        if not name:
            continue
        if name not in SOURCE_NAMES:
            raise ElibraryError(
                f"Неизвестный источник полного текста: {raw!r}. Допустимые: {', '.join(SOURCE_NAMES)}."
            )
        if name not in out:
            out.append(name)
    if not out:
        raise ElibraryError("Список источников полного текста пуст.")
    return tuple(out)


class _TextStore:
    """Extracted texts on disk: ``<key>.txt`` (primary key) and ``<key>.json`` (one per alias key)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(self, keys: Sequence[str]) -> tuple[dict[str, Any], str] | None:
        for key in keys:
            meta_path = self.root / f"{key}.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                text = (self.root / meta["text_file"]).read_text(encoding="utf-8")
            except (OSError, ValueError, KeyError, TypeError):
                continue
            return meta, text
        return None

    def save(self, keys: Sequence[str], text: str, meta: dict[str, Any]) -> None:
        if not keys:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        text_file = f"{keys[0]}.txt"
        tmp = self.root / (text_file + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.root / text_file)
        payload = json.dumps({**meta, "text_file": text_file, "saved_at": time.time()}, ensure_ascii=False, indent=1)
        for key in keys:
            path = self.root / f"{key}.json"
            path.with_suffix(".json.tmp").write_text(payload, encoding="utf-8")
            path.with_suffix(".json.tmp").replace(path)


@dataclass
class _Found:
    text: str
    source: str
    source_url: str | None
    format: str
    match_score: float | None
    pdf_path: str | None = None
    page_count: int | None = None
    engine: str | None = None
    license: str | None = None
    doi: str | None = None
    title: str | None = None


# ------------------------------------------------------------------------------ the search
class _Run:
    """State of one :func:`get_fulltext` call: lazily created clients, discovered DOI, attempts."""

    def __init__(self, req: FulltextRequest, settings: Settings, http: ElibraryHTTP | None,
                 net: ExternalHTTP | None, order: Sequence[str], download_dir: Path) -> None:
        self.req = req
        self.settings = settings
        self.http = http
        self._net = net
        self._own_net = net is None
        self.order = list(order)
        self.download_dir = download_dir
        self.attempts: list[FulltextAttempt] = []
        self.doi: str | None = req.doi
        self.doi_score: float = 1.0
        self._doi_searched = False
        self.records: dict[str, src.BibRecord] = {}
        self.landings: list[str] = []
        self.tried_urls: set[str] = set()
        #: CyberLeninka pages that answered 404 in this run
        self.missing_urls: set[str] = set()
        self.email = settings.contact_email
        keys = req.cache_keys()
        self.file_key = keys[0] if keys else "item"

    # .................................................................. plumbing
    @property
    def net(self) -> ExternalHTTP:
        if self._net is None:
            self._net = ExternalHTTP(self.settings)
        return self._net

    def close(self) -> None:
        if self._own_net and self._net is not None:
            self._net.close()

    def note(self, source: str, status: str, detail: str = "", url: str | None = None) -> None:
        self.attempts.append(FulltextAttempt(source=source, status=status, detail=detail, url=url))

    def titles(self) -> list[str]:
        return [t for t in (self.req.title, self.req.title_en) if t]

    def add_landing(self, url: str | None, *, front: bool = False) -> None:
        if not url or url in self.landings or "doi.org/" in url:
            return
        if front:
            self.landings.insert(0, url)
        else:
            self.landings.append(url)

    def run(self) -> _Found | None:
        for name in self.order:
            handler = getattr(self, f"_src_{name}")
            try:
                found = handler()
            except LoginRequired as exc:
                self.note(name, "login_required", exc.message, exc.url)
            except (CaptchaRequired, AccessBlocked, RateLimited) as exc:
                self.note(name, "blocked", exc.message, exc.url)
            except OfflineCacheMiss as exc:
                self.note(name, "skipped", exc.message, exc.url)
            except NotFound as exc:
                self.note(name, "not_found", exc.message, exc.url)
            except RobotsDisallowed as exc:
                self.note(name, "skipped", exc.message, exc.url)
            except ElibraryError as exc:
                self.note(name, "error", exc.message, exc.url)
            except Exception as exc:  # a parser bug in one source must not hide the others
                log.exception("full-text source %s failed", name)
                self.note(name, "error", f"{type(exc).__name__}: {exc}")
            else:
                if found is not None:
                    detail = f"{found.format}, {len(found.text)} симв."
                    if found.match_score is not None and found.match_score < 1:
                        detail += f", совпадение {found.match_score:.2f}"
                    self.note(name, "ok", detail, found.source_url)
                    return found
        return None

    # .................................................................. checks
    def quality_problem(self, text: str, *, pages: int | None = None, check_title: bool = True) -> str | None:
        """Why a text is not accepted, or ``None`` when it is fine."""
        stripped = text.strip()
        if len(stripped) < MIN_TEXT_CHARS:
            return f"текст слишком короткий ({len(stripped)} симв.)"
        if pages and len(stripped) / pages < MIN_PDF_CHARS_PER_PAGE:
            return "почти нет текста на страницах — вероятно, скан без текстового слоя (нужно OCR)"
        if check_title and self.titles() and not src.title_in_text(self.titles(), stripped, head_chars=15000,
                                                                    min_coverage=0.6):
            return "в начале текста нет названия публикации — вероятно, другой документ или заглушка"
        return None

    def ensure_doi(self) -> str | None:
        """The request's DOI, or one found by a Crossref/OpenAlex title search (once per run)."""
        if self.doi or self._doi_searched:
            return self.doi
        self._doi_searched = True
        searcher = "crossref" if "crossref" in self.order else "openalex" if "openalex" in self.order else None
        if not self.titles() or searcher is None:
            return None
        first_author = src.transliterate(src.surname(self.req.authors[0])) if self.req.authors else None
        try:
            if searcher == "crossref":
                for title in dict.fromkeys([t for t in (self.req.title_en, self.req.title) if t]):
                    items = src.crossref_search(self.net, title=title, author=first_author, year=self.req.year,
                                                email=self.email)
                    best = self._best_record([src.crossref_record(i) for i in items])
                    if best is not None:
                        self._set_doi(best, "crossref")
                        break
            else:
                self._openalex_search()
        except ElibraryError as exc:
            self.note(searcher, "error", f"поиск DOI по названию не удался: {exc.message}", exc.url)
            return self.doi
        if not self.doi:
            self.note(searcher, "no_match", "DOI по названию и авторам не найден")
        return self.doi

    def _best_record(self, records: Sequence[src.BibRecord]) -> src.BibRecord | None:
        best = None
        for rec in records:
            rec.score = src.match_score(self.req, titles=rec.titles, authors=rec.authors, year=rec.year,
                                        doi=rec.doi)
            if rec.score and (best is None or rec.score > (best.score or 0)):
                best = rec
        return best

    def _set_doi(self, rec: src.BibRecord, source: str) -> None:
        if rec.doi and not self.doi:
            self.doi = rec.doi
            self.doi_score = rec.score or 0.0
            self.note(source, "info", f"DOI найден по названию: {rec.doi} (сходство {self.doi_score:.2f})")

    def _openalex_search(self) -> src.BibRecord | None:
        for title in dict.fromkeys([t for t in (self.req.title, self.req.title_en) if t]):
            works = src.openalex_search(self.net, title, year=self.req.year, email=self.email)
            best = self._best_record([src.openalex_record(w) for w in works])
            if best is not None:
                self.records["openalex"] = best
                self._set_doi(best, "openalex")
                return best
        return None

    # .................................................................. candidates
    def try_pdf(self, source: str, url: str, *, score: float | None, referer: str | None = None,
                license: str | None = None) -> _Found | None:
        if url in self.tried_urls:
            return None
        self.tried_urls.add(url)
        if is_elibrary_host(url):
            self.note(source, "skipped", "ссылка ведёт на eLIBRARY.RU — используется источник elibrary", url)
            return None
        if is_cyberleninka_host(url):
            # robots.txt of CyberLeninka disallows its PDFs: read the article page instead
            page_url = src.cyberleninka_page_url(url)
            if page_url and "cyberleninka" in self.order:
                return self.try_cyberleninka_quietly(page_url, score=score, via=source)
            self.note(source, "skipped", "PDF КиберЛенинки не скачиваются: robots.txt запрещает это роботам", url)
            return None
        digest = hashlib.sha1(url.encode()).hexdigest()[:8]
        dest = self.download_dir / f"{source}_{self.file_key}_{digest}.pdf"
        try:
            self.net.download_pdf(url, dest, referer=referer, robots=True)
        except RobotsDisallowed as exc:
            self.note(source, "skipped", exc.message, url)
            return None
        except (NotAPdf, NotFound, RedirectToElibrary) as exc:
            self.note(source, "rejected", exc.message, url)
            return None
        except ExternalChallenge as exc:
            self.note(source, "blocked", exc.message, url)
            return None
        except ExternalHTTPError as exc:
            self.note(source, "error", exc.message, url)
            return None
        try:
            pdf = extract_pdf_text(dest, max_pages=PDF_MAX_PAGES)
        except ParseError as exc:  # unreadable PDF or a resource limit (decompression bomb)
            self.note(source, "rejected", f"PDF отклонён: {exc.message}", url)
            dest.unlink(missing_ok=True)
            return None
        problem = self.quality_problem(pdf.text, pages=min(pdf.page_count, PDF_MAX_PAGES))
        if problem:
            self.note(source, "rejected", f"PDF отклонён: {problem}", url)
            dest.unlink(missing_ok=True)
            return None
        return _Found(text=pdf.text, source=source, source_url=url, format="pdf", match_score=score,
                      pdf_path=str(dest), page_count=pdf.page_count, engine=pdf.engine, license=license,
                      doi=self.doi)

    def by_request_doi(self, rec: src.BibRecord | None) -> bool:
        """Was the record looked up by the DOI given in the request (not a fuzzy-found one)?"""
        return bool(rec is not None and rec.doi and self.req.doi and rec.doi == self.req.doi)

    def try_cyberleninka_quietly(self, url: str, *, score: float | None, via: str | None = None,
                                 by_doi: bool = False) -> _Found | None:
        """:meth:`try_cyberleninka` that records a missing or robots-forbidden page and returns ``None``."""
        try:
            return self.try_cyberleninka(url, score=score, via=via, by_doi=by_doi)
        except NotFound as exc:
            self.missing_urls.add(src.cyberleninka_page_url(url) or url)
            self.note("cyberleninka", "not_found", exc.message, exc.url or url)
        except RobotsDisallowed as exc:
            self.note("cyberleninka", "skipped", exc.message, exc.url or url)
        return None

    def try_cyberleninka(self, url: str, *, score: float | None, via: str | None = None,
                         by_doi: bool = False) -> _Found | None:
        """Read a CyberLeninka article page (never its PDF). Raises ``NotFound`` for a missing page and
        ``RobotsDisallowed`` when robots.txt forbids it; a page of another article is rejected."""
        page_url = src.cyberleninka_page_url(url)
        if page_url is None:
            self.note("cyberleninka", "skipped", "ссылка не ведёт на страницу статьи КиберЛенинки", url)
            return None
        url = page_url
        if url in self.tried_urls:
            return None
        self.tried_urls.add(url)
        art = src.cyberleninka_article(self.net, url, title_hint=self.titles())
        if not by_doi:
            # Unless the URL comes from a lookup by the request's own DOI, the page itself must match.
            own = src.match_score(self.req, titles=[art.title], authors=art.authors, year=art.year)
            if not own:
                self.note("cyberleninka", "rejected",
                          f"страница КиберЛенинки описывает другую статью («{art.title}»)", url)
                return None
            score = min(score, own) if score else own
        doi = src.normalize_doi(self.req.doi)
        if doi and art.dois_in_text:
            if doi in art.dois_in_text:
                score = 1.0
            elif (score or 0) < 1 and not src.title_in_text(self.titles(), art.text):
                self.note("cyberleninka", "rejected", f"в тексте другой DOI ({art.dois_in_text[0]})", url)
                return None
        problem = self.quality_problem(art.text, check_title=False)
        if problem:
            self.note("cyberleninka", "rejected", problem, url)
            return None
        if via:
            self.note(via, "info", "копия статьи найдена на КиберЛенинке", url)
        return _Found(text=art.text, source="cyberleninka", source_url=art.url or url, format="html",
                      match_score=score, page_count=art.pages, engine="html", license=art.license,
                      doi=self.doi, title=art.title)

    # .................................................................. sources
    def _src_elibrary(self) -> _Found | None:
        http, item_id = self.http, self.req.elibrary_id
        if http is None:
            self.note("elibrary", "skipped", "клиент eLIBRARY.RU не передан")
            return None
        if not item_id:
            self.note("elibrary", "skipped", "не указан eLIBRARY ID публикации")
            return None
        path = f"/item.asp?id={item_id}"
        cached = http.cached("GET", http.build_url(path), ttl_hours=TTL_ITEM)
        cached_ok = cached is not None and is_logged_in(cached.content)
        if not cached_ok and not http.state.get("account"):
            # Spend no request on an anonymous session: elibrary gives PDFs to signed-in readers only.
            self.note("elibrary", "skipped", "вход в eLIBRARY.RU не выполнен. " + LOGIN_HELP)
            return None
        if cached_ok:
            page = cached
        elif cached is None:
            page = http.get(path, cache_ttl_hours=TTL_ITEM)
        else:
            # The cached card was shown to an anonymous visitor: fetch it again (the PDF links are
            # only on the signed-in page) and keep the signed-in copy, so that the next call (a
            # retry, another item of a batch) needs no request.
            page = http.get(path, cache_ttl_hours=0)
            if page.status == 200 and not page.from_cache and is_logged_in(page.content):
                http.cache.put(PageCache.make_key("GET", http.build_url(path), None), page)
        pdf_path = download_elibrary_pdf(http, item_id, page.content, self.download_dir)  # type: ignore[union-attr]
        pdf = extract_pdf_text(pdf_path, max_pages=PDF_MAX_PAGES)
        problem = self.quality_problem(pdf.text, pages=min(pdf.page_count, PDF_MAX_PAGES), check_title=False)
        if problem:
            self.note("elibrary", "rejected", f"PDF отклонён: {problem}", http.absolute(path))
            return None
        return _Found(text=pdf.text, source="elibrary", source_url=http.absolute(path), format="pdf",
                      match_score=1.0, pdf_path=str(pdf_path), page_count=pdf.page_count, engine=pdf.engine,
                      doi=self.doi)

    def _src_cyberleninka(self) -> _Found | None:
        """CyberLeninka copy: the DataCite record of a CyberLeninka DOI, the OpenAlex location, then the
        article page derived from the title (``/article/n/<slug>``, ``-1``, ``-2``). The search API
        (``/api/search``, disallowed for bots by robots.txt) only with ``ELIBRARY_CYBERLENINKA_SEARCH=1``."""
        doi = src.normalize_doi(self.req.doi)
        if doi and doi.startswith(src.CYBERLENINKA_DOI_PREFIX):
            url = src.datacite_url(self.net, doi)
            if url and is_cyberleninka_host(url):
                found = self.try_cyberleninka_quietly(url, score=1.0, by_doi=True)
                if found:
                    return found
        rec = self.records.get("openalex")
        if rec is not None and rec.cyberleninka_url:
            found = self.try_cyberleninka_quietly(rec.cyberleninka_url, score=rec.score, via="openalex",
                                                  by_doi=self.by_request_doi(rec))
            if found:
                return found
        title = self.req.title or self.req.title_en
        if not title:
            self.note("cyberleninka", "skipped", "нет названия для поиска")
            return None
        found = self.cyberleninka_by_title(title)
        if found:
            return found
        if getattr(self.settings, "cyberleninka_search", False):
            hit = src.find_cyberleninka(self.net, self.req)
            if hit is not None:
                return self.try_cyberleninka(hit.url, score=hit.score)
        self.note("cyberleninka", "no_match", "на КиберЛенинке нет статьи с таким названием и авторами")
        return None

    def cyberleninka_by_title(self, title: str) -> _Found | None:
        """Try the article pages whose slug CyberLeninka would give ``title``: ``<slug>``, then the
        duplicates ``<slug>-1``, ``<slug>-2`` (at most three requests). A page counts only if its own
        title/authors/year match the request; a missing page ends the search (duplicates are numbered
        after the first article with that slug)."""
        for url in src.cyberleninka_candidate_urls(title):
            if url in self.missing_urls:
                break
            try:
                found = self.try_cyberleninka(url, score=None)
            except NotFound:
                log.debug("cyberleninka: no page %s", url)
                self.missing_urls.add(url)
                break
            if found:
                return found
        return None

    def _src_unpaywall(self) -> _Found | None:
        if not self.email:
            self.note("unpaywall", "skipped", "Unpaywall требует e-mail: задайте ELIBRARY_CONTACT_EMAIL")
            return None
        doi = self.ensure_doi()
        if not doi:
            self.note("unpaywall", "skipped", "нет DOI")
            return None
        data = src.unpaywall_lookup(self.net, doi, self.email)
        if data is None:
            self.note("unpaywall", "not_found", f"Unpaywall не знает DOI {doi}")
            return None
        rec = src.unpaywall_record(data)
        self.records["unpaywall"] = rec
        self.add_landing(rec.landing_url)
        if not rec.pdf_urls and not rec.cyberleninka_url:
            self.note("unpaywall", "not_found", f"открытых копий нет (is_oa={rec.is_oa})")
            return None
        for url in rec.pdf_urls[:3]:
            found = self.try_pdf("unpaywall", url, score=self.doi_score, license=rec.license)
            if found:
                return found
        if rec.cyberleninka_url and "cyberleninka" in self.order:
            return self.try_cyberleninka(rec.cyberleninka_url, score=self.doi_score, via="unpaywall",
                                         by_doi=self.by_request_doi(rec))
        return None

    def _src_openalex(self) -> _Found | None:
        doi = self.doi
        rec = self.records.get("openalex")
        if rec is None and doi:
            work = src.openalex_work(self.net, doi, email=self.email)
            if work is None:
                self.note("openalex", "not_found", f"OpenAlex не знает DOI {doi}")
                return None
            rec = src.openalex_record(work)
            rec.score = self.doi_score
            self.records["openalex"] = rec
        elif rec is None:
            if not self.titles():
                self.note("openalex", "skipped", "нет ни DOI, ни названия")
                return None
            rec = self._openalex_search()
            if rec is None:
                self.note("openalex", "no_match", "в OpenAlex нет записи с таким названием и авторами")
                return None
        self.add_landing(rec.landing_url)
        for url in rec.pdf_urls[:3]:
            found = self.try_pdf("openalex", url, score=rec.score, license=rec.license)
            if found:
                return found
        if rec.cyberleninka_url and "cyberleninka" in self.order:
            found = self.try_cyberleninka(rec.cyberleninka_url, score=rec.score, via="openalex",
                                          by_doi=self.by_request_doi(rec))
            if found:
                return found
        if not rec.pdf_urls:
            self.note("openalex", "not_found", f"открытых PDF нет (is_oa={rec.is_oa})")
        return None

    def _src_crossref(self) -> _Found | None:
        doi = self.ensure_doi()
        if not doi:
            self.note("crossref", "skipped", "нет DOI")
            return None
        item = src.crossref_work(self.net, doi, email=self.email)
        if item is None:
            self.note("crossref", "not_found", f"Crossref не знает DOI {doi}")
            return None
        rec = src.crossref_record(item)
        self.records["crossref"] = rec
        self.add_landing(rec.landing_url, front=True)
        for url in rec.pdf_urls[:2]:
            found = self.try_pdf("crossref", url, score=self.doi_score, referer=rec.landing_url, license=rec.license)
            if found:
                return found
        if not rec.pdf_urls:
            self.note("crossref", "not_found", "в записи Crossref нет ссылок на PDF")
        return None

    def _src_publisher(self) -> _Found | None:
        doi = self.ensure_doi()
        if not self.landings and doi:
            rec = self.records.get("crossref")
            if rec is None and "crossref" not in self.order:
                item = src.crossref_work(self.net, doi, email=self.email)
                rec = src.crossref_record(item) if item else None
            self.add_landing(rec.landing_url if rec else None)
            if not self.landings:
                self.add_landing(src.handle_url(self.net, doi))
        if not self.landings:
            if doi:
                self.note("publisher", "not_found", f"страница издателя для DOI {doi} не найдена")
            else:
                self.note("publisher", "skipped", "страница издателя неизвестна: нет DOI")
            return None
        for landing in self.landings[:2]:
            if is_elibrary_host(landing):
                self.note("publisher", "skipped", "страница публикации находится на eLIBRARY.RU", landing)
                continue
            if is_cyberleninka_host(landing):
                self.note("publisher", "skipped", "копия на КиберЛенинке читается источником cyberleninka", landing)
                continue
            try:
                page = self.net.get_page(landing, robots=True)
            except (RedirectToElibrary, RobotsDisallowed) as exc:
                self.note("publisher", "skipped", exc.message, landing)
                continue
            meta = src.parse_landing_meta(page.content, base_url=page.url)
            if doi and meta.doi and meta.doi != doi:
                self.note("publisher", "rejected", f"DOI страницы ({meta.doi}) не совпадает с {doi}", page.url)
                continue
            for url in meta.pdf_urls[:3]:
                found = self.try_pdf("publisher", url, score=self.doi_score if doi else None, referer=page.url)
                if found:
                    return found
            html_url = meta.fulltext_html_url
            html_page = page
            if html_url and not is_elibrary_host(html_url) and not is_cyberleninka_host(html_url):
                try:
                    html_page = self.net.get_page(html_url, robots=True)
                except (RedirectToElibrary, RobotsDisallowed) as exc:
                    self.note("publisher", "skipped", exc.message, html_url)
            text = src.extract_html_text(html_page.content, min_chars=MIN_TEXT_CHARS)
            if text and not self.quality_problem(text):
                return _Found(text=text, source="publisher", source_url=html_page.url, format="html",
                              match_score=self.doi_score if doi else None, engine="html", doi=doi,
                              title=meta.titles[0] if meta.titles else None)
            self.note("publisher", "not_found", "на странице издателя нет открытого PDF или текста", page.url)
        return None


def _to_fulltext(text: str, meta: Mapping[str, Any], *, offset: int, max_chars: int, from_cache: bool,
                 attempts: Sequence[FulltextAttempt]) -> FullText:
    chunk, next_offset = slice_text(text, offset, max_chars)
    return FullText(
        text=chunk,
        offset=min(max(0, offset), len(text)),
        next_offset=next_offset,
        total_chars=len(text),
        truncated=next_offset is not None,
        source=meta["source"],
        source_url=meta.get("source_url"),
        pdf_path=meta.get("pdf_path"),
        format=meta.get("format") or "pdf",
        match_score=meta.get("match_score"),
        attempts=list(attempts),
        page_count=meta.get("page_count"),
        engine=meta.get("engine"),
        license=meta.get("license"),
        doi=meta.get("doi"),
        title=meta.get("title"),
        from_cache=from_cache,
    )


def get_fulltext(
    req: FulltextRequest | Mapping[str, Any],
    *,
    http: ElibraryHTTP | None = None,
    net: ExternalHTTP | None = None,
    settings: Settings | None = None,
    sources: Sequence[str] | None = None,
    offset: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
    download_dir: Path | None = None,
    refresh: bool = False,
) -> FullText:
    """Find, extract, cache and page the full text of a publication.

    ``http`` is the elibrary client (needed for the ``elibrary`` source; without a signed-in session
    that source is skipped without any request), ``net`` the external client (created and closed
    here when omitted). ``sources`` is an ordered subset of :data:`SOURCE_NAMES` (default:
    :func:`default_sources`). PDFs are kept in ``download_dir`` (default ``settings.downloads_dir``).
    ``refresh=True`` (or ``ELIBRARY_REFRESH``) ignores the text cache.

    Raises :class:`NotFound` (with ``.attempts``, a list of dicts) when no source gave a usable text.
    """
    if settings is None:
        settings = http.settings if http is not None else net.settings if net is not None else Settings.from_env()
    request = req if isinstance(req, FulltextRequest) else FulltextRequest.model_validate(req)
    order = _validate_sources(sources) if sources is not None else default_sources(settings)
    if max_chars <= 0:
        raise ElibraryError("max_chars должен быть положительным.")
    store = _TextStore(fulltext_cache_dir(settings))
    keys = request.cache_keys()
    if not (refresh or settings.refresh):
        hit = store.load(keys)
        if hit is not None:
            meta, text = hit
            cached_attempts = [FulltextAttempt(source="cache", status="ok", detail="текст из локального кэша")]
            for raw in meta.get("attempts") or []:
                try:
                    cached_attempts.append(FulltextAttempt.model_validate(raw))
                except ValueError:
                    continue
            return _to_fulltext(text, meta, offset=offset, max_chars=max_chars, from_cache=True,
                                attempts=cached_attempts)
    if not keys:
        raise ElibraryError("Нужен хотя бы один идентификатор публикации: eLIBRARY ID, EDN, DOI или название.")
    run = _Run(request, settings, http, net, order, download_dir or settings.downloads_dir)
    try:
        found = run.run()
    finally:
        run.close()
    if found is None:
        summary = "; ".join(f"{a.source}: {a.detail or a.status}" for a in run.attempts if a.status != "info")
        exc = NotFound("Полный текст не найден ни в одном источнике. " + summary)
        exc.attempts = [a.model_dump() for a in run.attempts]  # type: ignore[attr-defined]
        raise exc
    meta = {
        "source": found.source, "source_url": found.source_url, "pdf_path": found.pdf_path,
        "format": found.format, "match_score": found.match_score, "page_count": found.page_count,
        "engine": found.engine, "license": found.license, "doi": found.doi or run.doi, "title": found.title,
        "attempts": [a.model_dump() for a in run.attempts],
    }
    store.save(keys, found.text, meta)
    return _to_fulltext(found.text, meta, offset=offset, max_chars=max_chars, from_cache=False,
                        attempts=run.attempts)
