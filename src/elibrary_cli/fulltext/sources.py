"""Open full-text sources outside elibrary.ru and fuzzy matching of bibliographic records.

Sources (formats verified live on 2026-10-13, saved in ``tests/fixtures/external/``):

* **Crossref** ``GET https://api.crossref.org/works/<doi>`` and bibliographic search
  ``/works?query.bibliographic=...`` (``mailto`` puts us into the polite pool). Russian papers are
  often registered with the English title only, so match on ``title_en`` too.
* **Unpaywall** ``GET https://api.unpaywall.org/v2/<doi>?email=<e-mail>`` (a real e-mail is required).
* **OpenAlex** ``GET https://api.openalex.org/works/doi:<doi>`` (free) and
  ``/works?filter=title.search:<title>`` (USD 0.001 per call). OpenAlex indexes CyberLeninka as the
  repository source ``S4306401404``.
* **DataCite** ``GET https://api.datacite.org/dois/<doi>``: DOIs with the prefix 10.24412 are
  CyberLeninka's own, ``data.attributes.url`` is the article page.
* **CyberLeninka**: the article page ``/article/n/<slug>`` carries the whole OCR text in
  ``div.ocr[itemprop=articleBody] > p`` (including the tail of the previous and the head of the next
  article of the issue, trimmed here). Its robots.txt disallows ``/api/``, ``/search``, PDFs and the
  viewer for bots, so the page URL is derived from the title (:func:`cyberleninka_slug`: a fixed
  transliteration, at most 131 characters, duplicates get ``-1``, ``-2``). The search API
  ``POST https://cyberleninka.ru/api/search`` (JSON ``{"mode": "articles", "q": ..., "size": N,
  "from": M}`` plus optional ``year_from``/``year_to``) is used only when the user opts in
  (``ELIBRARY_CYBERLENINKA_SEARCH=1``).
* **Publisher landing pages**: ``citation_pdf_url`` / ``citation_fulltext_html_url`` meta tags (OJS,
  Elpub, Eco-Vector), OJS galley links.

All network calls go through :class:`elibrary_cli.fulltext.net.ExternalHTTP`; the ``parse_*``
functions are pure.
"""

from __future__ import annotations

import difflib
import html as htmllib
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlsplit

from pydantic import Field

from elibrary_cli.errors import NotFound, ParseError
from elibrary_cli.fulltext.net import TTL_PAGE, ExternalHTTP, host_of, is_cyberleninka_host
from elibrary_cli.models.base import ElibModel
from elibrary_cli.parsers.common import clean, first_year, node_text, parse_html, remove_noise

log = logging.getLogger(__name__)

CROSSREF_API = "https://api.crossref.org"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
OPENALEX_API = "https://api.openalex.org"
DATACITE_API = "https://api.datacite.org"
HANDLE_API = "https://hdl.handle.net/api/handles"
CYBERLENINKA = "https://cyberleninka.ru"
#: OpenAlex id of the CyberLeninka repository source.
OPENALEX_CYBERLENINKA_SOURCE = "S4306401404"
#: DOI prefix registered by CyberLeninka at DataCite.
CYBERLENINKA_DOI_PREFIX = "10.24412/"

#: Title similarity needed to accept a candidate when at least one author surname matches.
TITLE_THRESHOLD = 0.9
#: Title similarity needed when the candidate (or the query) has no authors to compare.
STRICT_TITLE_THRESHOLD = 0.95

_TAG_RE = re.compile(r"<[^>]+>")
_PUNCT_RE = re.compile(r"[^\w\s]|_", re.UNICODE)
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*|doi\s+)", re.I)
_CYR = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LAT_TO_CYR = str.maketrans("aceopxykmthb", "асеорхукмтнв")
_CYR_TO_LAT = str.maketrans("асеорхукмтнв", "aceopxykmthb")
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}
_UDC_RE = re.compile(r"^(?:УДК|UDC|ББК|DOI:?\s*10\.)", re.I)
_PREV_ARTICLE_END_RE = re.compile(
    r"Ключевые слова|Key\s?words|Список литературы|Библиографический список|References|Литература", re.I
)
_PAGE_NO_RE = re.compile(r"^\d{1,4}$")
_CF_EMAIL_RE = re.compile(
    r"<(a|span)\b[^>]*\bdata-cfemail=\"([0-9a-fA-F]+)\"[^>]*>.*?</\1>", re.S
)
_CL_VIEWER_RE = re.compile(r"id:\s*(\d+)\s*,\s*pages:\s*(\d+)")
_PDF_HREF_RE = re.compile(r"/article/(?:download|viewFile)/\d+/\d+|\.pdf(?:$|[?#])", re.I)


class BibQuery(Protocol):
    """What the matchers need from a request (``FulltextRequest`` satisfies it)."""

    doi: str | None
    title: str | None
    title_en: str | None
    authors: list[str]
    year: int | None
    journal: str | None


# --------------------------------------------------------------------------- normalisation
def normalize_doi(raw: str | None) -> str | None:
    """``"https://doi.org/10.1000/ABC."`` -> ``"10.1000/abc"`` (lower case); ``None`` if no DOI."""
    if not raw:
        return None
    text = _DOI_PREFIX_RE.sub("", htmllib.unescape(raw).strip())
    m = _DOI_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(".,;:)]}'").lower()


def find_dois(text: str) -> list[str]:
    """All distinct DOIs mentioned in a text, in order of appearance."""
    out: list[str] = []
    for m in _DOI_RE.finditer(text or ""):
        doi = normalize_doi(m.group(0))
        if doi and doi not in out:
            out.append(doi)
    return out


def strip_tags(text: str | None) -> str:
    """Remove HTML tags (e.g. CyberLeninka's ``<b>`` highlights) and entities, collapse spaces."""
    return clean(htmllib.unescape(_TAG_RE.sub("", text or "")))


def _fold_token(token: str) -> str:
    cyr = sum(1 for c in token if c in _CYR)
    lat = sum(1 for c in token if "a" <= c <= "z")
    if cyr and lat:
        return token.translate(_LAT_TO_CYR if cyr >= lat else _CYR_TO_LAT)
    return token


def normalize_title(text: str | None) -> str:
    """Case-, punctuation- and script-insensitive form of a title.

    Strips tags/entities, applies NFKC, case-folds, maps ``ё`` to ``е``, replaces punctuation with
    spaces and folds Latin/Cyrillic look-alike letters inside mixed-script words (``"Hейрон"`` with
    a Latin ``H`` becomes ``"нейрон"``).
    """
    s = strip_tags(text)
    s = unicodedata.normalize("NFKC", s).casefold().replace("ё", "е")
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(_fold_token(t) for t in s.split())


def title_similarity(a: str | None, b: str | None) -> float:
    """Similarity of two titles in 0..1: mean of difflib's ratio and the token Jaccard index.

    Both components penalise extra words, so a title that is a subset of another one ("Нейронные
    сети" vs "Нейронные сети в медицине") does not score high.
    """
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb)
    return round((ratio + jaccard) / 2, 4)


def transliterate(text: str) -> str:
    """Simple Russian -> Latin transliteration used to compare surnames across scripts."""
    return "".join(_TRANSLIT.get(c, c) for c in text.lower())


def _latin_key(word: str) -> str:
    word = unicodedata.normalize("NFKD", transliterate(word))
    word = "".join(c for c in word if c.isalpha() and not unicodedata.combining(c))
    return word.replace("kh", "h").replace("iy", "y").replace("ii", "y").replace("yi", "y")


def surname(name: str | None) -> str:
    """Best-effort surname of a person name in any of the forms seen in the sources.

    ``"Петров В.А."``, ``"В.А. Петров"``, ``"Панарин Владимир Михайлович"`` (Russian order: first
    word), ``"Vladimir V. Ryazanov"`` (Latin: last word), ``"Ryazanov, Vladimir V."`` (before the comma).
    """
    text = clean(name)
    if not text:
        return ""
    if "," in text:
        head = text.split(",", 1)[0].split()
        return head[-1].strip(".'’-") if head else ""
    tokens = text.replace(".", ". ").split()
    words = [t for t in tokens if not t.endswith(".") and len(t) > 1]  # initials end with "."
    if not words:
        words = [t.rstrip(".") for t in tokens if len(t.rstrip(".")) > 1] or [tokens[0].rstrip(".")]
    cyrillic = any(c.lower() in _CYR for c in text)
    # "Vasiliev Alexander Petrovich": Russian order in Latin script, recognised by the patronymic.
    patronymic = len(words) >= 3 and words[-1].lower().endswith(("vich", "vna", "ichna", "ich"))
    return (words[0] if cyrillic or patronymic else words[-1]).strip("'’-")


def surname_key(name: str | None) -> str:
    """Latin, lower-case, de-accented surname used for comparisons."""
    return _latin_key(surname(name))


def _similar_keys(x: str, y: str) -> bool:
    return x == y or (min(len(x), len(y)) >= 4 and difflib.SequenceMatcher(None, x, y).ratio() >= 0.8)


def authors_match(a: Iterable[str], b: Iterable[str]) -> bool | None:
    """``True`` if a surname of ``a`` (the query) matches an author of ``b`` (transliteration-aware),
    ``False`` if none does, ``None`` if either list is empty.

    Surnames are compared first; as a fallback every word of the ``b`` names is tried, so a
    candidate that writes names in an unexpected order ("Владимир Панарин") still matches.
    """
    a, b = [x for x in a if x], [x for x in b if x]
    ka = {k for k in (surname_key(x) for x in a) if len(k) >= 2}
    kb = {k for k in (surname_key(x) for x in b) if len(k) >= 2}
    if not ka or not kb:
        return None
    if any(_similar_keys(x, y) for x in ka for y in kb):
        return True
    words = {_latin_key(w) for name in b for w in re.split(r"[\s.,]+", name) if len(w) > 2}
    return any(_similar_keys(x, y) for x in ka for y in words if len(y) >= 3)


def match_score(query: BibQuery, *, titles: Sequence[str | None], authors: Sequence[str] = (),
                year: int | None = None, doi: str | None = None) -> float:
    """How well a candidate record matches the query, 0 (reject) .. 1.

    Same DOI -> 1.0; different DOIs -> 0. Otherwise the best title similarity against the query's
    ``title``/``title_en`` must reach :data:`TITLE_THRESHOLD` with a matching author surname, or
    :data:`STRICT_TITLE_THRESHOLD` when authors cannot be compared. Years must be within +-1 and a
    candidate with authors none of which match is rejected (generic titles are often duplicated).
    """
    qdoi, cdoi = normalize_doi(query.doi), normalize_doi(doi)
    if qdoi and cdoi:
        return 1.0 if qdoi == cdoi else 0.0
    wanted = [t for t in (query.title, query.title_en) if t]
    got = [t for t in titles if t]
    if not wanted or not got:
        return 0.0
    best = max(title_similarity(w, g) for w in wanted for g in got)
    if query.year and year and abs(int(query.year) - int(year)) > 1:
        return 0.0
    same_author = authors_match(query.authors or [], authors or [])
    if same_author is False:
        return 0.0
    threshold = TITLE_THRESHOLD if same_author else STRICT_TITLE_THRESHOLD
    return best if best >= threshold else 0.0


def title_in_text(titles: Sequence[str | None], text: str, *, head_chars: int = 8000,
                  min_coverage: float = 0.7) -> bool:
    """Does the beginning of ``text`` mention one of the titles (>= 70 % of its significant words)?

    Used as a sanity check for downloaded PDFs (wrong article, cover page, paywall stub).
    """
    head = set(normalize_title(text[:head_chars]).split())
    for title in titles:
        words = [w for w in normalize_title(title).split() if len(w) >= 4] or normalize_title(title).split()
        if words and sum(1 for w in words if w in head) / len(words) >= min_coverage:
            return True
    return False


# --------------------------------------------------------------------------------- records
class BibRecord(ElibModel):
    """A bibliographic record of an open-access API, normalised for matching and link discovery."""

    source: str = Field(description="crossref | unpaywall | openalex | datacite")
    id: str | None = Field(None, description="Record id in the source (OpenAlex W..., DOI...)")
    doi: str | None = None
    titles: list[str] = Field(default_factory=list, description="All known titles (original, translated)")
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    landing_url: str | None = Field(None, description="Publisher landing page")
    pdf_urls: list[str] = Field(default_factory=list, description="Candidate PDF links, best first")
    cyberleninka_url: str | None = Field(None, description="CyberLeninka copy of the work, if known")
    license: str | None = None
    is_oa: bool | None = None
    score: float | None = Field(None, description="Match score against the request (0..1)")


def _dedupe(urls: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    for u in urls:
        if u and u.startswith(("http://", "https://")) and u not in out:
            out.append(u)
    return out


def _date_year(obj: Mapping[str, Any] | None) -> int | None:
    try:
        return int(obj["date-parts"][0][0])  # type: ignore[index]
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def crossref_record(item: Mapping[str, Any]) -> BibRecord:
    """Normalise a Crossref ``message`` / search item."""
    titles = [*(item.get("title") or []), *(item.get("original-title") or []), *(item.get("short-title") or [])]
    subtitles = item.get("subtitle") or []
    if item.get("title") and subtitles:
        titles.append(f"{item['title'][0]}. {subtitles[0]}")
    authors = []
    for a in item.get("author") or []:
        family, given = a.get("family"), a.get("given")
        if family:
            authors.append(f"{family}, {given}" if given else family)
        elif a.get("name"):
            authors.append(a["name"])
    year = None
    for key in ("issued", "published", "published-print", "published-online"):
        year = _date_year(item.get(key))
        if year:
            break
    links = item.get("link") or []
    pdfs = [ln.get("URL") for ln in links if "pdf" in (ln.get("content-type") or "").lower()]
    pdfs += [ln.get("URL") for ln in links
             if (ln.get("content-type") or "unspecified") == "unspecified" and _PDF_HREF_RE.search(ln.get("URL") or "")]
    licenses = item.get("license") or []
    return BibRecord(
        source="crossref",
        id=item.get("DOI"),
        doi=normalize_doi(item.get("DOI")),
        titles=[strip_tags(t) for t in titles if t],
        authors=authors,
        year=year,
        journal=(item.get("container-title") or [None])[0],
        landing_url=((item.get("resource") or {}).get("primary") or {}).get("URL"),
        pdf_urls=_dedupe(pdfs),
        license=licenses[0].get("URL") if licenses else None,
    )


def unpaywall_record(data: Mapping[str, Any]) -> BibRecord:
    """Normalise an Unpaywall v2 response; PDF links: best location first, published versions first."""
    version_rank = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
    best = data.get("best_oa_location") or {}
    locations = sorted(
        data.get("oa_locations") or [],
        key=lambda loc: (version_rank.get(loc.get("version") or "", 3), loc.get("host_type") != "publisher"),
    )
    pdfs = [best.get("url_for_pdf"), *(loc.get("url_for_pdf") for loc in locations)]
    landing = [best.get("url_for_landing_page"), *(loc.get("url_for_landing_page") for loc in locations)]
    landing = [u for u in landing if u and "doi.org/" not in u]
    # A CyberLeninka copy is read from its HTML page: its PDFs are disallowed for bots.
    cl = next((p for p in (cyberleninka_page_url(u) for u in landing + _dedupe(pdfs)) if p), None)
    return BibRecord(
        source="unpaywall",
        id=data.get("doi"),
        doi=normalize_doi(data.get("doi")),
        titles=[data["title"]] if data.get("title") else [],
        authors=[a.get("raw_author_name") or " ".join(filter(None, (a.get("given"), a.get("family"))))
                 for a in data.get("z_authors") or []],
        year=data.get("year"),
        journal=data.get("journal_name"),
        landing_url=next((u for u in landing if not is_cyberleninka_host(u)), None),
        pdf_urls=[u for u in _dedupe(pdfs) if not is_cyberleninka_host(u)],
        cyberleninka_url=cl,
        license=best.get("license"),
        is_oa=data.get("is_oa"),
    )


def openalex_record(work: Mapping[str, Any]) -> BibRecord:
    """Normalise an OpenAlex work; a location of source S4306401404 gives the CyberLeninka copy."""
    best = work.get("best_oa_location") or {}
    oa = work.get("open_access") or {}
    locations = work.get("locations") or []
    pdfs = [best.get("pdf_url"), *(loc.get("pdf_url") for loc in locations)]
    oa_url = oa.get("oa_url")
    if oa_url and _PDF_HREF_RE.search(oa_url):
        pdfs.insert(1, oa_url)
    cl = None
    for loc in [best, *locations]:
        src = (loc.get("source") or {}).get("id") or ""
        page = loc.get("landing_page_url") or ""
        if src.endswith(OPENALEX_CYBERLENINKA_SOURCE) or is_cyberleninka_host(page):
            cl = cyberleninka_page_url(page) or cl
            break
    if not cl and oa_url and is_cyberleninka_host(oa_url):
        cl = cyberleninka_page_url(oa_url)
    primary = work.get("primary_location") or {}
    landing = primary.get("landing_page_url")
    authors = [a.get("raw_author_name") or (a.get("author") or {}).get("display_name") or ""
               for a in work.get("authorships") or []]
    titles = _dedupe_text([work.get("display_name"), work.get("title")])
    return BibRecord(
        source="openalex",
        id=work.get("id"),
        doi=normalize_doi(work.get("doi")),
        titles=titles,
        authors=[a for a in authors if a],
        year=work.get("publication_year"),
        journal=(primary.get("source") or {}).get("display_name"),
        landing_url=None if landing and is_cyberleninka_host(landing) else landing,
        pdf_urls=[u for u in _dedupe(pdfs) if not is_cyberleninka_host(u)],
        cyberleninka_url=cl,
        license=best.get("license"),
        is_oa=oa.get("is_oa"),
    )


def _dedupe_text(values: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(strip_tags(v))
    return out


# ------------------------------------------------------------------------------- API calls
def _polite(params: dict[str, str], email: str | None) -> dict[str, str]:
    if email:
        params["mailto"] = email
    return params


def _doi_path(doi: str) -> str:
    return quote(doi, safe="/:;()._-")


def crossref_work(net: ExternalHTTP, doi: str, *, email: str | None = None) -> dict[str, Any] | None:
    """Crossref ``message`` for a DOI, or ``None`` when Crossref does not know it."""
    try:
        data = net.get_json(f"{CROSSREF_API}/works/{_doi_path(doi)}", params=_polite({}, email))
    except NotFound:
        return None
    return data.get("message") if isinstance(data, dict) else None


_CROSSREF_SELECT = "DOI,title,original-title,subtitle,short-title,author,issued,published,container-title,score,type,link,resource,license"


def crossref_search(net: ExternalHTTP, *, title: str, author: str | None = None, year: int | None = None,
                    rows: int = 5, email: str | None = None) -> list[dict[str, Any]]:
    """Crossref bibliographic search (``query.bibliographic`` + ``query.author`` + year window)."""
    params = {"query.bibliographic": title, "rows": str(rows), "select": _CROSSREF_SELECT}
    if author:
        params["query.author"] = author
    if year:
        params["filter"] = f"from-pub-date:{year - 1},until-pub-date:{year + 1}"
    data = net.get_json(f"{CROSSREF_API}/works", params=_polite(params, email))
    return list(((data or {}).get("message") or {}).get("items") or [])


def unpaywall_lookup(net: ExternalHTTP, doi: str, email: str) -> dict[str, Any] | None:
    """Unpaywall v2 record for a DOI (``None`` when unknown). ``email`` is mandatory."""
    if not email:
        raise ValueError("Для Unpaywall нужен e-mail (ELIBRARY_CONTACT_EMAIL или UNPAYWALL_EMAIL)")
    try:
        data = net.get_json(f"{UNPAYWALL_API}/{_doi_path(doi)}", params={"email": email})
    except NotFound:
        return None
    return data if isinstance(data, dict) else None


def openalex_work(net: ExternalHTTP, doi: str, *, email: str | None = None) -> dict[str, Any] | None:
    """OpenAlex work for a DOI (free singleton call), or ``None``."""
    try:
        data = net.get_json(f"{OPENALEX_API}/works/doi:{_doi_path(doi)}", params=_polite({}, email))
    except NotFound:
        return None
    return data if isinstance(data, dict) else None


_OPENALEX_SELECT = ("id,doi,display_name,title,publication_year,language,authorships,open_access,"
                    "best_oa_location,primary_location,locations")


def openalex_search(net: ExternalHTTP, title: str, *, year: int | None = None, per_page: int = 5,
                    email: str | None = None, cyberleninka_only: bool = False) -> list[dict[str, Any]]:
    """OpenAlex ``title.search`` filter (costs USD 0.001 per call without a key)."""
    value = " ".join(re.sub(r"[,:|]+", " ", strip_tags(title)).split())
    filters = [f"title.search:{value}"]
    if year:
        filters.append(f"publication_year:{year - 1}-{year + 1}")
    if cyberleninka_only:
        filters.append(f"locations.source.id:{OPENALEX_CYBERLENINKA_SOURCE}")
    params = {"filter": ",".join(filters), "per-page": str(per_page), "select": _OPENALEX_SELECT}
    data = net.get_json(f"{OPENALEX_API}/works", params=_polite(params, email))
    return list((data or {}).get("results") or [])


def datacite_url(net: ExternalHTTP, doi: str) -> str | None:
    """Landing URL registered at DataCite (CyberLeninka DOIs 10.24412/...), or ``None``."""
    try:
        data = net.get_json(f"{DATACITE_API}/dois/{_doi_path(doi)}")
    except NotFound:
        return None
    return (((data or {}).get("data") or {}).get("attributes") or {}).get("url")


def handle_url(net: ExternalHTTP, doi: str) -> str | None:
    """Resolve a DOI through the Handle API (doi.org itself answers 403 to many clients)."""
    try:
        data = net.get_json(f"{HANDLE_API}/{_doi_path(doi)}", params={"type": "URL"})
    except NotFound:
        return None
    if not isinstance(data, dict) or data.get("responseCode") != 1:
        return None
    for value in data.get("values") or []:
        if value.get("type") == "URL":
            return (value.get("data") or {}).get("value")
    return None


# ---------------------------------------------------------------------------- CyberLeninka
#: Transliteration of CyberLeninka article slugs. Derived from 41 title -> URL pairs (the saved search
#: answers in ``tests/fixtures/external`` and public article URLs, see ``docs/re/fulltext.md``):
#: х -> h, ц -> ts, щ -> sch, ё -> yo, й/ы -> y, ь/ъ dropped. Ukrainian/Belarusian letters are a guess.
CL_TRANSLIT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
}
#: Longest CyberLeninka slug (without the "-N" duplicate suffix); longer ones are cut at a word boundary.
CL_SLUG_MAX = 131
#: How many article pages are tried for one title: ``<slug>``, ``<slug>-1``, ``<slug>-2``.
CL_SLUG_CANDIDATES = 3
_CL_ARTICLE_RE = re.compile(r"^/article/n/([a-z0-9][a-z0-9-]*?)(?:\.pdf|/(?:pdf|viewer))?/?$", re.I)


def _cl_letter(ch: str) -> str:
    if ch in CL_TRANSLIT:
        return CL_TRANSLIT[ch]
    if ch.isascii():
        return ch if ch.isalnum() else " "
    # Latin letters with diacritics -> base letter (unverified); any other character separates words
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if c.isascii() and c.isalnum())
    return base or " "


def cyberleninka_slug(title: str | None) -> str:
    """The slug CyberLeninka gives an article with this title (without the duplicate suffix).

    Lower case; Cyrillic transliterated with :data:`CL_TRANSLIT`; every run of other characters
    (spaces, punctuation, dashes) becomes one ``-``; a slug longer than :data:`CL_SLUG_MAX`
    characters is cut after the last whole word that fits. ``""`` when nothing is left.
    """
    # NFKC folds full-width letters and ligatures; "№" would become "No", keep it a separator (a guess)
    text = unicodedata.normalize("NFKC", strip_tags(title).replace("№", " ")).lower()
    words = re.split(r"[^a-z0-9]+", "".join(_cl_letter(c) for c in text))
    slug = "-".join(w for w in words if w)
    if len(slug) > CL_SLUG_MAX:
        cut = slug[:CL_SLUG_MAX + 1]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else slug[:CL_SLUG_MAX]
    return slug


def cyberleninka_candidate_urls(title: str | None, *, count: int = CL_SLUG_CANDIDATES) -> list[str]:
    """Article pages that may hold a title: ``/article/n/<slug>``, then ``<slug>-1``, ``<slug>-2``
    (CyberLeninka numbers the later articles with the same slug)."""
    slug = cyberleninka_slug(title)
    if not slug:
        return []
    return [f"{CYBERLENINKA}/article/n/{slug}" + (f"-{i}" if i else "") for i in range(max(0, count))]


def cyberleninka_page_url(url: str | None) -> str | None:
    """The HTML page of a CyberLeninka article link (``.../pdf``, ``.pdf``, ``.../viewer`` and query
    strings removed), or ``None`` when the link is not an article of cyberleninka.ru."""
    if not url or not is_cyberleninka_host(url):
        return None
    m = _CL_ARTICLE_RE.match(urlsplit(url).path)
    return f"{CYBERLENINKA}/article/n/{m.group(1).lower()}" if m else None


class CyberLeninkaHit(ElibModel):
    """One article of a CyberLeninka search result."""

    title: str
    url: str = Field(description="Article page, https://cyberleninka.ru/article/n/<slug>")
    authors: list[str] = Field(default_factory=list, description="Full names, 'Фамилия Имя Отчество'")
    year: int | None = None
    journal: str | None = None
    snippet: str | None = Field(None, description="Highlighted OCR fragments joined, tags removed")
    score: float | None = Field(None, description="Match score against the request (0..1)")


class CyberLeninkaArticle(ElibModel):
    """A parsed CyberLeninka article page with its OCR full text."""

    url: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    keywords: list[str] = Field(default_factory=list)
    license: str | None = Field(None, description="Licence badge, e.g. 'CC BY'")
    pdf_url: str | None = Field(None, description="citation_pdf_url (robots.txt disallows fetching it)")
    cyberleninka_id: int | None = Field(None, description="Internal numeric id of the viewer")
    pages: int | None = Field(None, description="Number of PDF pages")
    page_range: str | None = Field(None, description="Pages in the journal issue, e.g. '438-443'")
    text: str = Field("", description="Full text: paragraphs separated by blank lines")
    paragraphs_total: int = Field(0, description="Paragraphs on the page before trimming")
    trimmed_head: int = Field(0, description="Paragraphs dropped at the start (end of the previous article)")
    trimmed_tail: int = Field(0, description="Paragraphs dropped at the end (start of the next article)")
    dois_in_text: list[str] = Field(default_factory=list, description="DOIs printed in the OCR text")
    extra: dict[str, str] = Field(default_factory=dict, description="Other citation_*/eprints.* meta tags")


def parse_cyberleninka_search(data: Mapping[str, Any]) -> list[CyberLeninkaHit]:
    """Pure parser of a ``/api/search`` JSON answer."""
    hits = []
    for art in data.get("articles") or []:
        link = art.get("link")
        if not link:
            continue
        hits.append(CyberLeninkaHit(
            title=strip_tags(art.get("name")),
            url=urljoin(CYBERLENINKA + "/", link),
            authors=[strip_tags(a) for a in art.get("authors") or [] if a],
            year=art.get("year") if isinstance(art.get("year"), int) else first_year(str(art.get("year") or "")),
            journal=strip_tags(art.get("journal")) or None,
            snippet=" … ".join(strip_tags(s) for s in art.get("ocr") or [] if s) or None,
        ))
    return hits


def cyberleninka_search(net: ExternalHTTP, query: str, *, size: int = 10, offset: int = 0,
                        year_from: int | None = None, year_to: int | None = None) -> list[CyberLeninkaHit]:
    """Search CyberLeninka articles (title, abstract and OCR text are searched; a DOI works too).

    CyberLeninka's robots.txt disallows ``/api/`` for bots: the full-text search calls this only
    when the user opted in (``Settings.cyberleninka_search``, ``ELIBRARY_CYBERLENINKA_SEARCH=1``)."""
    payload: dict[str, Any] = {"mode": "articles", "q": query, "size": size, "from": offset}
    if year_from:
        payload["year_from"] = year_from
    if year_to:
        payload["year_to"] = year_to
    data = net.post_json(f"{CYBERLENINKA}/api/search", payload)
    return parse_cyberleninka_search(data if isinstance(data, dict) else {})


def cf_decode_email(hex_string: str) -> str:
    """Decode a Cloudflare-obfuscated e-mail (``data-cfemail``): first byte is the XOR key."""
    try:
        key = int(hex_string[:2], 16)
        return "".join(chr(int(hex_string[i:i + 2], 16) ^ key) for i in range(2, len(hex_string), 2))
    except ValueError:
        return ""


def _trim_neighbours(paras: list[str], titles: Sequence[str | None]) -> tuple[int, int]:
    """Indices ``[start, end)`` of the article inside the OCR paragraphs of a CyberLeninka page.

    CyberLeninka OCRs whole PDF pages, so the text may start with the end of the previous article
    of the issue and end with the beginning of the next one. The article starts at its title
    (moved back over up to three "УДК"/"DOI" lines); the head is dropped only if it looks like the
    end of another article (keywords / references). The article ends at the first "УДК"/"DOI"
    paragraph at least five paragraphs after the title.
    """
    wanted = [normalize_title(t) for t in titles if t]
    wanted = [w for w in wanted if w]
    if not wanted:
        return 0, len(paras)
    idx = None
    for i, para in enumerate(paras[:80]):
        cands = [normalize_title(para)]
        if i + 1 < len(paras):
            cands.append(normalize_title(f"{para} {paras[i + 1]}"))
        for w in wanted:
            if any(c and (c == w or title_similarity(w, c) >= 0.9 or (len(w) >= 15 and c.startswith(w)
                                                                       and len(c) < 1.6 * len(w)))
                   for c in cands):
                idx = i
                break
        if idx is not None:
            break
    if idx is None:
        return 0, len(paras)
    start = idx
    k = idx - 1
    while k >= 0 and idx - k <= 3 and _UDC_RE.match(paras[k]):
        start = k
        k -= 1
    if start > 0 and not _PREV_ARTICLE_END_RE.search(" ".join(paras[:start])):
        start = 0
    end = len(paras)
    for j in range(idx + 5, len(paras)):
        if _UDC_RE.match(paras[j]):
            end = j
            break
    return start, end


def parse_cyberleninka_article(html: str | bytes, *, url: str | None = None,
                               title_hint: str | Sequence[str] | None = None, trim: bool = True) -> CyberLeninkaArticle:
    """Pure parser of a CyberLeninka article page (``/article/n/<slug>``).

    ``title_hint`` (the expected title or titles) is used, together with the page's own title, to
    trim the neighbouring articles from the OCR text when ``trim`` is true.
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    html = _CF_EMAIL_RE.sub(lambda m: cf_decode_email(m.group(2)), html)
    tree = parse_html(html)
    metas: dict[str, list[str]] = {}
    for m in tree.css("meta"):
        name = m.attributes.get("name") or m.attributes.get("property")
        content = m.attributes.get("content")
        if name and content is not None:
            metas.setdefault(name, []).append(clean(content))

    def meta(name: str) -> str | None:
        vals = [v for v in metas.get(name, []) if v]
        return vals[0] if vals else None

    title = meta("citation_title") or node_text(tree.css_first('[itemprop="headline"]')) or None
    body = tree.css_first('div.ocr[itemprop="articleBody"]') or tree.css_first("div.ocr") \
        or tree.css_first('[itemprop="articleBody"]')
    if title is None and body is None:
        raise ParseError("Страница не похожа на статью КиберЛенинки (нет заголовка и текста).", url=url)
    paras: list[str] = []
    if body is not None:
        for banner in body.css("div.ocr-banner"):
            banner.decompose()
        for p in body.css("p"):
            text = clean(p.text(deep=True, separator=" ").replace("\N{ZERO WIDTH NO-BREAK SPACE}", ""))
            if text and not _PAGE_NO_RE.match(text):
                paras.append(text)
    hints: list[str | None] = [title_hint] if isinstance(title_hint, str) else list(title_hint or [])
    start, end = _trim_neighbours(paras, [title, *hints]) if trim else (0, len(paras))
    kept = paras[start:end]
    viewer = _CL_VIEWER_RE.search(html)
    year_node = tree.css_first('time[itemprop="datePublished"]')
    year = first_year(meta("citation_publication_date") or (year_node.attributes.get("datetime") if year_node else None))
    license_node = tree.css_first(".label-cc")
    keywords = meta("citation_keywords")
    known = {"citation_title", "citation_author", "citation_publication_date", "citation_journal_title",
             "citation_pdf_url", "citation_keywords", "eprints.pagerange", "og:url"}
    extra = {k: "; ".join(v) for k, v in metas.items()
             if k.startswith(("citation_", "eprints.")) and k not in known and any(v)}
    page_url = meta("og:url") or url
    return CyberLeninkaArticle(
        url=page_url,
        title=title,
        authors=metas.get("citation_author", []),
        year=year,
        journal=meta("citation_journal_title"),
        keywords=[k.strip() for k in keywords.split(",") if k.strip()] if keywords else [],
        license=node_text(license_node) or None,
        pdf_url=meta("citation_pdf_url"),
        cyberleninka_id=int(viewer.group(1)) if viewer else None,
        pages=int(viewer.group(2)) if viewer else None,
        page_range=meta("eprints.pagerange"),
        text="\n\n".join(kept),
        paragraphs_total=len(paras),
        trimmed_head=start,
        trimmed_tail=len(paras) - end,
        dois_in_text=find_dois(" ".join(kept)),
        extra=extra,
    )


def cyberleninka_article(net: ExternalHTTP, url: str, *, title_hint: str | Sequence[str] | None = None,
                         trim: bool = True, robots: bool = True) -> CyberLeninkaArticle:
    """Fetch (cached 30 days, robots.txt honoured) and parse a CyberLeninka article page.

    Links to the PDF or the viewer are turned into the article page first; anything that is not an
    article page of cyberleninka.ru raises :class:`ValueError` (no request is sent)."""
    page_url = cyberleninka_page_url(url)
    if page_url is None:
        raise ValueError(f"Не страница статьи КиберЛенинки: {url}")
    page = net.get_page(page_url, cache_ttl_hours=TTL_PAGE, robots=robots)
    article = parse_cyberleninka_article(page.content, url=page.url, title_hint=title_hint, trim=trim)
    if not article.url:
        article.url = page.url
    return article


def best_match(query: BibQuery, hits: Sequence[CyberLeninkaHit]) -> CyberLeninkaHit | None:
    """The best-scoring hit above the thresholds of :func:`match_score` (scores are stored on hits)."""
    best: CyberLeninkaHit | None = None
    for hit in hits:
        hit.score = match_score(query, titles=[hit.title], authors=hit.authors, year=hit.year)
        if hit.score and (best is None or (hit.score or 0) > (best.score or 0)):
            best = hit
    return best


def find_cyberleninka(net: ExternalHTTP, query: BibQuery, *, size: int = 10) -> CyberLeninkaHit | None:
    """Find the CyberLeninka copy of a work through the search API: by title (year +-1), then by DOI.

    Opt-in only (the API is disallowed for bots by robots.txt); the default way is
    :func:`cyberleninka_candidate_urls`."""
    title = query.title or query.title_en
    if not title:
        return None
    year = query.year
    hits = cyberleninka_search(net, title, size=size, year_from=year - 1 if year else None,
                               year_to=year + 1 if year else None)
    hit = best_match(query, hits)
    doi = normalize_doi(query.doi)
    if hit is None and doi:
        hit = best_match(query, cyberleninka_search(net, doi, size=5))
    return hit


# -------------------------------------------------------------------------- landing pages
class LandingMeta(ElibModel):
    """Full-text links and identification found on a publisher landing page."""

    url: str
    pdf_urls: list[str] = Field(default_factory=list, description="citation_pdf_url first, then galley links")
    fulltext_html_url: str | None = Field(None, description="citation_fulltext_html_url when it differs from the page")
    doi: str | None = None
    titles: list[str] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    language: str | None = None
    year: int | None = None


def parse_landing_meta(html: str | bytes, *, base_url: str) -> LandingMeta:
    """Pure parser of Highwire/Dublin Core meta tags and OJS galley links of a landing page."""
    tree = parse_html(html)
    metas: dict[str, list[str]] = {}
    for m in tree.css("meta"):
        name = (m.attributes.get("name") or m.attributes.get("property") or "").strip()
        content = m.attributes.get("content")
        if name and content:
            metas.setdefault(name.lower(), []).append(clean(content))
    pdfs = [urljoin(base_url, u) for u in metas.get("citation_pdf_url", [])]
    for link in tree.css('link[type="application/pdf"]'):
        if link.attributes.get("href"):
            pdfs.append(urljoin(base_url, link.attributes["href"] or ""))
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")) or "SuppFile" in href:
            continue
        label = node_text(a).lower()
        classes = (a.attributes.get("class") or "").lower()
        absolute = urljoin(base_url, href)
        if _PDF_HREF_RE.search(href):
            pdfs.append(absolute)
        elif "pdf" in label or "pdf" in classes:
            # OJS galley page /article/view/<id>/<galley> -> the file itself is /article/download/...
            m = re.search(r"/article/view/(\d+)/(\d+)", absolute)
            if m and m.group(2) != "0":
                pdfs.append(absolute.replace("/article/view/", "/article/download/"))
    doi = None
    for key in ("citation_doi", "dc.identifier.doi", "prism.doi", "bepress_citation_doi", "dc.identifier"):
        for value in metas.get(key, []):
            doi = normalize_doi(value)
            if doi:
                break
        if doi:
            break
    fulltext = (metas.get("citation_fulltext_html_url") or [None])[0]
    if fulltext:
        fulltext = urljoin(base_url, fulltext)
        if fulltext.rstrip("/") == base_url.rstrip("/"):
            fulltext = None
    titles = _dedupe_text([*metas.get("citation_title", []), *metas.get("dc.title", []), *metas.get("og:title", [])])
    year = None
    for key in ("citation_publication_date", "citation_date", "dc.date.issued", "dc.date"):
        if metas.get(key):
            year = first_year(metas[key][0])
            if year:
                break
    return LandingMeta(
        url=base_url,
        pdf_urls=[u for u in _dedupe(pdfs) if not host_of(u).endswith("elibrary.ru")],
        fulltext_html_url=fulltext,
        doi=doi,
        titles=titles,
        authors=_dedupe_text(metas.get("citation_author", [])),
        language=(metas.get("citation_language") or metas.get("dc.language") or [None])[0],
        year=year,
    )


_BODY_SELECTORS = (
    '[itemprop="articleBody"]', "div.ocr", "#article-body", ".article-body", ".article__body", ".article-text",
    "#article-text", ".fulltext", "#fulltext", ".full-text", "article", "main",
)


def extract_html_text(html: str | bytes, *, min_chars: int = 1500) -> str:
    """Best-effort article text of a publisher HTML page (``""`` when no body of ``min_chars``)."""
    tree = remove_noise(parse_html(html))
    for sel in ("nav", "header", "footer", "aside", "form", ".references", "#references"):
        for node in tree.css(sel):
            node.decompose()
    for sel in _BODY_SELECTORS:
        node = tree.css_first(sel)
        if node is None:
            continue
        parts = [node_text(p) for p in node.css("h1, h2, h3, h4, p, li")]
        text = "\n\n".join(p for p in parts if p)
        if len(text) >= min_chars:
            return text
    return ""
