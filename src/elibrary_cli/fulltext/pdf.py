"""PDF -> plain text for the full-text tools (optional extra ``pdf``: pypdf + pdfminer.six).

pypdf is tried first (fast, pure Python). Its output is checked for the two failure modes seen on
Russian PDFs (see ``docs/re/fulltext.md``):

* words broken by spurious spaces ("Нейрон ы", "инф орматики") on CyberLeninka-generated PDFs;
* mojibake: U+FFFD, private-use glyphs, ``(cid:N)`` placeholders, control characters, UTF-8 read
  as cp1252 ("Ð¿Ñ€Ð¸").

If the text looks broken, or has almost no text per page, pdfminer.six is run as well and the better
result wins. Both libraries are imported lazily; when neither is installed
:class:`~elibrary_cli.errors.DependencyMissing` is raised.

Resource limits (PDFs come from arbitrary hosts; a few MB of FlateDecode can inflate to many GB):
every decompressed stream is counted against a per-document budget (:data:`MAX_STREAM_BYTES` per
stream, :data:`MAX_DECODED_BYTES` in all) in both libraries — pypdf through its configuration and
``decode_stream_data``, pdfminer through bounded stand-ins for ``zlib``/LZW/RunLength/CCITT in
``pdfminer.pdftypes`` (active only inside :func:`extract_pdf_text`); pdfminer may lay out at most
:data:`MAX_PDFMINER_CHARS_PER_PAGE` characters per page; the text is cut at :data:`MAX_TEXT_CHARS`.
An engine that hits a limit stops (:class:`PdfResourceLimit`), and **no other engine is tried after
it**: the pages read so far are used when they hold text, otherwise the PDF is refused.

Post-processing joins hyphenated words at line ends, removes running headers/footers and lone page
numbers, and joins lines into paragraphs separated by blank lines.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import io
import logging
import re
import statistics
import threading
import zlib
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, NamedTuple

from elibrary_cli.errors import DependencyMissing, ParseError

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF"
_MIB = 1024 * 1024

#: Pages read by the full-text sources (``get_fulltext``); ``page_count`` still reports all pages.
DEFAULT_MAX_PAGES = 300
#: Longest text kept from one PDF, characters (a longer one is cut, not refused).
MAX_TEXT_CHARS = 3_000_000
#: Largest decompressed stream, bytes (pypdf's own default is 75 MB, pdfminer has none).
MAX_STREAM_BYTES = 32 * _MIB
#: Decompressed stream data per document and engine, bytes.
MAX_DECODED_BYTES = 200 * _MIB
#: Characters pdfminer may lay out on one page (each one is a layout object of about 1 KB).
MAX_PDFMINER_CHARS_PER_PAGE = 50_000


class PdfResourceLimit(ParseError):
    """Text extraction stopped at a resource limit (decompression bomb, absurd page).

    ``pages`` / ``total`` are what the engine read before it stopped (used when they hold text)."""

    def __init__(self, reason: str, pages: Sequence[str] = (), total: int = 0) -> None:
        super().__init__(
            f"Извлечение текста из PDF остановлено: {reason}. Возможно, это «PDF-бомба» (сжатые данные, "
            "которые распаковываются в гигабайты); файл не обрабатывается."
        )
        self.reason = reason
        self.pages = list(pages)
        self.total = total


class _DecodeLimit(Exception):
    """Raised inside the PDF libraries when the decoding budget of the document is exhausted."""


class _Budget:
    """Decompressed bytes of one document and engine (see :func:`_decode_budget`)."""

    def __init__(self, total: int, per_stream: int) -> None:
        self.total = total
        self.per_stream = per_stream
        self.used = 0
        self.reason: str | None = None

    @property
    def exceeded(self) -> bool:
        return self.reason is not None

    def allowance(self, stream_used: int = 0) -> int:
        """How many more bytes the current stream may produce."""
        return max(0, min(self.per_stream - stream_used, self.total - self.used))

    def fail(self, reason: str) -> None:
        self.reason = self.reason or reason
        raise _DecodeLimit(self.reason)

    def check(self) -> None:
        if self.reason is not None:
            raise _DecodeLimit(self.reason)

    def consume(self, n: int, *, stream_size: int | None = None) -> None:
        self.used += n
        if (n if stream_size is None else stream_size) > self.per_stream:
            self.fail(f"поток PDF распаковывается больше чем в {self.per_stream // _MIB} МБ")
        if self.used > self.total:
            self.fail(f"распакованные данные PDF больше {self.total // _MIB} МБ")


_BUDGET: ContextVar[_Budget | None] = ContextVar("elibrary_pdf_decode_budget", default=None)
_HOOK_LOCK = threading.Lock()


@contextlib.contextmanager
def _decode_budget() -> Iterator[_Budget]:
    """Activate a fresh budget for the current context (thread / task)."""
    budget = _Budget(MAX_DECODED_BYTES, MAX_STREAM_BYTES)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def _is_resource_limit(exc: BaseException, budget: _Budget) -> bool:
    """Was ``exc`` caused by a decoding budget or a pypdf ``LimitReachedError`` (also when wrapped)?"""
    if budget.exceeded:
        return True
    try:
        from pypdf.errors import LimitReachedError
    except ImportError:  # pragma: no cover - pypdf without the error class / not installed
        LimitReachedError = _DecodeLimit  # noqa: N806
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (_DecodeLimit, LimitReachedError)):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _cap_text(pages: Sequence[str], limit: int | None = None) -> list[str]:
    """Pages cut so that their total length is at most :data:`MAX_TEXT_CHARS`."""
    room = MAX_TEXT_CHARS if limit is None else limit
    out: list[str] = []
    for page in pages:
        if room <= 0:
            break
        out.append(page[:room])
        room -= len(out[-1])
    return out


# ------------------------------------------------------------------- pypdf: bounded decoding
def _hook_pypdf() -> None:
    """Count every stream pypdf decodes against the active budget (no budget: unchanged)."""
    import pypdf.filters as filters  # lazy: optional extra

    with _HOOK_LOCK:
        original = getattr(filters, "decode_stream_data", None)
        if original is None or getattr(original, "_elibrary_bounded", False):
            return

        @functools.wraps(original)
        def decode_stream_data(stream: Any) -> bytes:
            budget = _BUDGET.get()
            if budget is None:
                return original(stream)
            budget.check()
            data = original(stream)
            budget.consume(len(data))
            return data

        decode_stream_data._elibrary_bounded = True  # type: ignore[attr-defined]
        filters.decode_stream_data = decode_stream_data


def _pypdf_limits() -> contextlib.AbstractContextManager[Any]:
    """pypdf's per-stream output limits lowered to :data:`MAX_STREAM_BYTES` (never raised)."""
    import pypdf

    apply = getattr(pypdf, "apply_configuration", None)
    config_cls = getattr(pypdf, "Configuration", None)
    if apply is None or config_cls is None or not dataclasses.is_dataclass(config_cls):
        return contextlib.nullcontext()  # older pypdf: its module constants (75 MB) apply
    current = pypdf.get_configuration()
    names = ("zlib_maximum_output_length", "lzw_maximum_output_length", "run_length_maximum_output_length",
             "array_based_stream_maximum_output_length", "brotli_maximum_output_length",
             "jbig2_maximum_output_length")
    fields = {f.name for f in dataclasses.fields(config_cls)}
    wanted = {n: min(MAX_STREAM_BYTES, getattr(current, n)) for n in names if n in fields}
    return apply(**wanted)


# ---------------------------------------------------------------- pdfminer: bounded decoding
class _BoundedDecompressor:
    """``zlib.decompressobj()`` whose output is counted against the budget."""

    def __init__(self, inner: Any, budget: _Budget) -> None:
        self._inner = inner
        self._budget = budget
        self._size = 0

    def decompress(self, data: bytes, max_length: int = 0) -> bytes:
        self._budget.check()
        cap = self._budget.allowance(self._size) + 1
        out = self._inner.decompress(data, min(max_length, cap) if max_length else cap)
        self._size += len(out)
        self._budget.consume(len(out), stream_size=self._size)
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _BoundedZlib:
    """Stand-in for the ``zlib`` module inside ``pdfminer.pdftypes`` (whose ``PDFStream.decode``
    calls ``zlib.decompress(data)`` with no output limit): bounded while a budget is active."""

    error = zlib.error

    def decompress(self, data: bytes, wbits: int = zlib.MAX_WBITS, bufsize: int = zlib.DEF_BUF_SIZE) -> bytes:
        budget = _BUDGET.get()
        if budget is None:
            return zlib.decompress(data, wbits, bufsize)
        budget.check()
        cap = budget.allowance()
        d = zlib.decompressobj(wbits)
        out = d.decompress(data, cap + 1)
        budget.consume(len(out))  # raises past the cap
        if not d.eof:  # what zlib.decompress() says about a truncated stream
            raise zlib.error("Error -5 while decompressing data: incomplete or truncated stream")
        return out

    def decompressobj(self, *args: Any, **kwargs: Any) -> Any:
        inner = zlib.decompressobj(*args, **kwargs)
        budget = _BUDGET.get()
        return inner if budget is None else _BoundedDecompressor(inner, budget)

    def __getattr__(self, name: str) -> Any:
        return getattr(zlib, name)


def _runlength_size(data: bytes, cap: int) -> int:
    """Decoded size of RunLengthDecode data (stops counting once past ``cap``)."""
    i = n = 0
    end = len(data)
    while i < end and n <= cap:
        b = data[i]
        if b == 128:
            break
        if b < 128:
            n += b + 1
            i += b + 2
        else:
            n += 257 - b
            i += 2
    return n


def _int_param(params: Any, name: str, default: int) -> int:
    try:
        return int(params.get(name, default))
    except (AttributeError, TypeError, ValueError):
        return default


def _hook_pdfminer() -> None:
    """Install the bounded decoders into ``pdfminer.pdftypes`` once (no budget: unchanged)."""
    import pdfminer.pdftypes as pt  # lazy: optional extra

    with _HOOK_LOCK:
        if isinstance(getattr(pt, "zlib", None), _BoundedZlib):
            return
        lzw, rl, ccitt = getattr(pt, "lzwdecode", None), getattr(pt, "rldecode", None), getattr(pt, "ccittfaxdecode", None)

        def lzwdecode(data: bytes) -> bytes:
            budget = _BUDGET.get()
            if budget is None or lzw is None:
                return lzw(data)  # type: ignore[misc]
            from pdfminer.lzw import LZWDecoder

            budget.check()
            cap = budget.allowance()
            parts: list[bytes] = []
            size = 0
            for chunk in LZWDecoder(io.BytesIO(data)).run():
                size += len(chunk)
                if size > cap:
                    budget.consume(size)  # raises
                parts.append(chunk)
            budget.consume(size)
            return b"".join(parts)

        def rldecode(data: bytes) -> bytes:
            budget = _BUDGET.get()
            if budget is not None:
                budget.check()
                size = _runlength_size(data, budget.allowance())
                if size > budget.allowance():
                    budget.consume(size)  # raises
            out = rl(data)  # type: ignore[misc]
            if budget is not None:
                budget.consume(len(out))
            return out

        def ccittfaxdecode(data: bytes, params: Any) -> bytes:
            budget = _BUDGET.get()
            if budget is not None:
                budget.check()
                # G4 needs at least one bit per row: at most 8 rows per input byte
                estimate = (max(1, _int_param(params, "Columns", 1728)) + 7) // 8 * 8 * len(data)
                if estimate > budget.allowance():
                    budget.consume(estimate)  # raises
            out = ccitt(data, params)  # type: ignore[misc]
            if budget is not None:
                budget.consume(len(out))
            return out

        if lzw is not None:
            pt.lzwdecode = lzwdecode
        if rl is not None:
            pt.rldecode = rldecode
        if ccitt is not None:
            pt.ccittfaxdecode = ccittfaxdecode
        pt.zlib = _BoundedZlib()  # last: marks the hooks as installed

#: Below this many non-space characters per page the text layer is considered missing (scan).
MIN_CHARS_PER_PAGE = 80
#: Share of suspicious characters above which a text is considered mojibake.
MAX_MOJIBAKE_RATIO = 0.02
#: Share of Cyrillic word fragments (short non-words) above which a text is considered broken.
#: Calibrated on three Russian PDFs: clean engines give 0.3-1.1 %, pypdf on a CyberLeninka PDF 2.3 %.
MAX_FRAGMENT_RATIO = 0.018

# Short Russian words (1-3 letters) and abbreviations that are legitimate tokens; any other
# lower-case token that short is counted as a fragment of a broken word.
_SHORT_RU_WORDS = """
    а б в г д е ж з и й к л м н о п р с т у ф х ц ч ш щ э ю я ё
    ад аз ах бы во вы да до её ее ей ем ею же за из им их ко ли мы на не ни но ну об ой он от
    по со та те то ту ты уж ум юг яд ох эх ай
    без бег бой бок бор был вам вас век вес вид вне вод воз всё все всю вся газ где год гол два
    дал дел для дни дно дня док дом дух его ему ещё еще жар жив жил зал зло иго или имя иск как
    кем код кол ком кон кто лес лет лик лоб лук май мал мел мер мир мне мог мои мой мол мор муж
    над нам нас наш нее неё нею нем нём нет них ним нож нос оба она они оно опт оси ось пар пик
    под пол пор пот при про пул пут раз ран рис род рол рот ряд сад сам сей сие сил сих сок сон
    суд сыр так там тем тех тип той ток том тон тот три тут уже ума ухо фон час чей чем чин что
    чья чье чьё чью шаг шум щит эра эта эти это эту юга юге юго яма ярд
    см мм км кг мг мл гг вв рр сс тт др пр ст ул зав доц чел руб млн тыс экз кн изд
    гл ср тж тд тп вып ред сост пер авт гос мин мкм нм мкг ед сек
"""
_SHORT_RU = frozenset(_SHORT_RU_WORDS.split())
_CYR_TOKEN_RE = re.compile(r"[А-Яа-яЁё]+")
# "Ð¿Ñ€Ð¸": UTF-8 Cyrillic decoded as cp1252 / latin-1 (lead byte D0/D1 + continuation byte).
_CP1252_CONT = "".join(chr(c) for c in range(0xA0, 0xC0)) + "".join(
    chr(c) for c in (0x152, 0x153, 0x160, 0x161, 0x178, 0x17D, 0x17E, 0x192, 0x2C6, 0x2DC, 0x2013, 0x2014,
                     0x2018, 0x2019, 0x201A, 0x201C, 0x201D, 0x201E, 0x2020, 0x2021, 0x2022, 0x2026, 0x2030,
                     0x2039, 0x203A, 0x20AC, 0x2122)
)
_UTF8_AS_CP1252_RE = re.compile("[" + chr(0xD0) + chr(0xD1) + "][" + re.escape(_CP1252_CONT) + "]")
_WORD_CHAR = r"[^\W\d_]"
_HYPHENS = "-\N{HYPHEN}\N{NON-BREAKING HYPHEN}"
_HYPHEN_BREAK_RE = re.compile(rf"({_WORD_CHAR}+)([ \t]*)[{_HYPHENS}][ \t]*\n[ \t]*({_WORD_CHAR}+)")
_SOFT_HYPHEN_RE = re.compile("\xad\\s*")
_SPACES_RE = re.compile("[ \t\N{THIN SPACE}\N{HAIR SPACE}\N{NARROW NO-BREAK SPACE}\N{FIGURE SPACE}]+")
_PAGE_NUMBER_RE = re.compile(r"^\s*(?:[-–—]\s*)?(?:стр\.\s*|с\.\s*|page\s*)?\d{1,4}(?:\s*[-–—])?\s*$", re.I)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-–—•●▪◦*]\s|\d{1,3}[.)]\s|[a-zа-я][)]\s|\[\d{1,3}\]\s)", re.I)
_SENTENCE_END = (".", "!", "?", ":", ";", "…", "»", '"', ")")
# Russian words written with a real hyphen ("из-за", "кто-то", "по-русски").
_HYPHEN_KEEP_LEFT = frozenset({"кое", "кой", "северо", "юго", "из"})
_HYPHEN_KEEP_RIGHT = frozenset({"то", "либо", "нибудь", "таки", "за", "под", "де", "ка"})
_ADJ_ENDINGS = ("ий", "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ого", "его", "ому", "ему",
                "ым", "им", "ыми", "ими", "ую", "юю")


def _real_hyphen(left: str, right: str) -> bool:
    """Is ``left-<newline>right`` a hyphenated compound rather than a word broken at a line end?"""
    lo, ro = left.lower(), right.lower()
    if lo in _HYPHEN_KEEP_LEFT or ro in _HYPHEN_KEEP_RIGHT:
        return True
    if lo == "по":  # "по-прежнему", "по-русски" vs "по-казана"
        return ro.endswith(("ому", "ему", "ски", "цки"))
    if lo == "во":  # "во-первых"
        return ro.endswith(("ых", "их"))
    # "сердечно-сосудистый", "научно-технических": adverb-like first part + full adjective.
    return len(lo) >= 5 and lo.endswith("о") and len(ro) >= 6 and ro.endswith(_ADJ_ENDINGS)


class PdfText(NamedTuple):
    """Result of :func:`extract_pdf_text` (a tuple ``(text, page_count, engine)``)."""

    text: str
    page_count: int
    engine: str


class TextQuality(NamedTuple):
    """Heuristic quality figures of an extracted text (see :func:`text_quality`)."""

    chars: int
    chars_per_page: float
    mojibake_ratio: float
    fragment_ratio: float

    @property
    def broken(self) -> bool:
        return self.mojibake_ratio > MAX_MOJIBAKE_RATIO or self.fragment_ratio > MAX_FRAGMENT_RATIO

    @property
    def too_short(self) -> bool:
        return self.chars_per_page < MIN_CHARS_PER_PAGE

    @property
    def ok(self) -> bool:
        return not self.broken and not self.too_short

    def badness(self) -> float:
        """Lower is better; used to choose between engines (1.0 = at a threshold)."""
        return (self.mojibake_ratio / MAX_MOJIBAKE_RATIO + self.fragment_ratio / MAX_FRAGMENT_RATIO
                + (1.0 if self.too_short else 0.0))


def is_pdf(data: bytes) -> bool:
    """``True`` when ``data`` has the ``%PDF`` magic within its first KiB."""
    return PDF_MAGIC in data[:1024]


def _is_mojibake_char(ch: str) -> bool:
    if ch in "\n\t\r\f":
        return False
    o = ord(ch)
    return o < 32 or o == 0xFFFD or 0xE000 <= o <= 0xF8FF or 0x80 <= o < 0xA0


def text_quality(pages: Sequence[str]) -> TextQuality:
    """Measure characters per page, the mojibake share and the share of broken Cyrillic words."""
    joined = "\n".join(pages)
    non_space = [c for c in joined if not c.isspace()]
    n = len(non_space)
    bad = sum(1 for c in non_space if _is_mojibake_char(c)) + 5 * joined.count("(cid:")
    bad += 2 * len(_UTF8_AS_CP1252_RE.findall(joined))
    # Count on de-hyphenated text: a word split at a line end is not a broken word. Upper-case
    # tokens ("КТ", "ИБС") are abbreviations, not fragments.
    tokens = _CYR_TOKEN_RE.findall(_dehyphenate(_normalise_page(joined)))
    if tokens:
        fragments = sum(
            1 for t in tokens if len(t) <= 3 and not (len(t) > 1 and t.isupper()) and t.lower() not in _SHORT_RU
        )
        fragment_ratio = fragments / len(tokens)
    else:
        fragment_ratio = 0.0
    return TextQuality(
        chars=n,
        chars_per_page=n / max(1, len(pages)),
        mojibake_ratio=bad / n if n else 0.0,
        fragment_ratio=fragment_ratio,
    )


# ---------------------------------------------------------------------------------------- engines
def _load_bytes(source: str | Path | bytes | bytearray) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    return Path(source).read_bytes()


def _limit_reason(exc: BaseException, budget: _Budget) -> str:
    return budget.reason or f"сработало ограничение pypdf ({exc})"


def _extract_pypdf(data: bytes, max_pages: int | None) -> tuple[list[str], int]:
    """pypdf, page by page. A page that hits a pypdf limit is left empty and the document is marked
    as limited; an exhausted budget stops the extraction. Either way :class:`PdfResourceLimit` is
    raised at the end with the pages read (no fallback engine after that)."""
    import pypdf  # lazy: optional extra

    _hook_pypdf()
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    pages: list[str] = []
    limited: str | None = None
    with _decode_budget() as budget, _pypdf_limits():
        try:
            reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception as exc:  # pragma: no cover - depends on the file / cryptography
                    raise ParseError(f"PDF зашифрован, извлечь текст нельзя: {exc}") from exc
            total = len(reader.pages)
        except ParseError:
            raise
        except Exception as exc:
            if _is_resource_limit(exc, budget):
                raise PdfResourceLimit(_limit_reason(exc, budget)) from exc
            raise
        limit = total if max_pages is None else min(total, max(0, max_pages))
        chars = 0
        for i in range(limit):
            if chars >= MAX_TEXT_CHARS:
                break
            try:
                text = reader.pages[i].extract_text() or ""
            except Exception as exc:  # one bad page must not kill the document
                if _is_resource_limit(exc, budget):
                    limited = limited or _limit_reason(exc, budget)
                log.debug("pypdf failed on page %s: %s", i + 1, exc)
                text = ""
            if budget.exceeded:  # also when pypdf swallowed the error
                limited = limited or budget.reason
            text = text[:MAX_TEXT_CHARS - chars]
            chars += len(text)
            pages.append(text)
            if budget.exceeded:
                break
    if limited:
        raise PdfResourceLimit(limited, pages, total)
    return pages, total


def _extract_pdfminer(data: bytes, max_pages: int | None) -> tuple[list[str], int]:
    """pdfminer.six (what ``pdfminer.high_level.extract_text`` does, in one pass that also counts the
    pages), with bounded decoding and at most :data:`MAX_PDFMINER_CHARS_PER_PAGE` characters a page."""
    from pdfminer.converter import TextConverter  # lazy: optional extra
    from pdfminer.layout import LAParams
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage

    _hook_pdfminer()
    logging.getLogger("pdfminer").setLevel(logging.ERROR)

    class _Converter(TextConverter):  # type: ignore[misc]
        page_chars = 0
        chars_out = 0

        def begin_page(self, page: Any, ctm: Any) -> None:
            self.page_chars = 0
            super().begin_page(page, ctm)

        def render_char(self, *args: Any, **kwargs: Any) -> Any:
            self.page_chars += 1
            if self.page_chars > MAX_PDFMINER_CHARS_PER_PAGE:
                raise _DecodeLimit(f"на странице PDF больше {MAX_PDFMINER_CHARS_PER_PAGE} символов")
            return super().render_char(*args, **kwargs)

        def write_text(self, text: str) -> None:
            self.chars_out += len(text)
            super().write_text(text)

    limit = None if max_pages is None else max(0, max_pages)
    out = io.StringIO()
    total = processed = 0
    with _decode_budget() as budget:
        rsrcmgr = PDFResourceManager(caching=True)
        device = _Converter(rsrcmgr, out, codec="utf-8", laparams=LAParams())
        interpreter = PDFPageInterpreter(rsrcmgr, device)
        try:
            for page in PDFPage.get_pages(io.BytesIO(data), caching=True):
                total += 1
                if (limit is None or processed < limit) and device.chars_out < MAX_TEXT_CHARS:
                    interpreter.process_page(page)
                    processed += 1
                budget.check()  # pdfminer may have swallowed a _DecodeLimit
        except Exception as exc:
            if _is_resource_limit(exc, budget):
                # a page is written out only when it is complete: the one being read is not in `out`
                done = out.getvalue().split("\f")[:processed]
                raise PdfResourceLimit(budget.reason or str(exc), _cap_text(done), total) from exc
            raise
    pages = out.getvalue().split("\f")[:processed]  # every page ends with a form feed
    return _cap_text(pages), total


Engine = Callable[[bytes, "int | None"], "tuple[list[str], int]"]


def _engines() -> list[tuple[str, Engine]]:
    """Installed engines in order of preference."""
    out: list[tuple[str, Engine]] = []
    try:
        import pypdf  # noqa: F401

        out.append(("pypdf", _extract_pypdf))
    except ImportError:
        pass
    try:
        import pdfminer.high_level  # noqa: F401

        out.append(("pdfminer", _extract_pdfminer))
    except ImportError:
        pass
    return out


def extract_pdf_text(
    source: str | Path | bytes | bytearray,
    *,
    max_pages: int | None = None,
    page_markers: bool = False,
) -> PdfText:
    """Extract clean text from a PDF given as a path or bytes.

    Returns ``PdfText(text, page_count, engine)``: ``page_count`` is the number of pages of the
    document (not only of the extracted ones), ``engine`` is ``"pypdf"`` or ``"pdfminer"``.
    ``max_pages`` limits extraction to the first N pages (the full-text sources pass
    :data:`DEFAULT_MAX_PAGES`). With ``page_markers=True`` every page starts with a ``[стр. N]``
    paragraph. The text is cut at :data:`MAX_TEXT_CHARS`; decoding is bounded (module docstring).

    Raises :class:`DependencyMissing` when neither pypdf nor pdfminer.six is installed,
    :class:`PdfResourceLimit` (a :class:`ParseError`) when a resource limit stopped the extraction
    before any text was read, and :class:`ParseError` when the data is not a readable PDF.
    """
    data = _load_bytes(source)
    if not is_pdf(data):
        raise ParseError("Файл не является PDF (нет сигнатуры %PDF).")
    engines = _engines()
    if not engines:
        raise DependencyMissing("pdf", "pypdf")
    results: list[tuple[str, list[str], int, TextQuality]] = []
    errors: list[str] = []
    limited: PdfResourceLimit | None = None
    for name, engine in engines:
        try:
            pages, total = engine(data, max_pages)
        except PdfResourceLimit as exc:
            # A decompression bomb or an absurd page: never hand the file to the next engine.
            log.warning("PDF text extraction (%s) stopped: %s", name, exc.reason)
            limited = exc
            if any(p.strip() for p in exc.pages):
                pages = _cap_text(exc.pages)
                results.append((name, pages, exc.total, text_quality(pages)))
            break
        except ParseError:
            raise
        except Exception as exc:
            log.debug("%s failed: %s", name, exc)
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        pages = _cap_text(pages)
        quality = text_quality(pages)
        log.debug("%s: %s", name, quality)
        results.append((name, pages, total, quality))
        if quality.ok:
            break
    if limited is not None and not any(r[3].chars for r in results):
        raise limited
    if not results:
        raise ParseError("Не удалось извлечь текст из PDF (" + "; ".join(errors) + ")")
    best = results[0]
    for cand in results[1:]:
        # Prefer the fallback when it is cleaner and not much shorter (or the first had no text).
        if cand[3].badness() < best[3].badness() and (cand[3].chars >= 0.7 * best[3].chars or best[3].too_short):
            best = cand
    name, pages, total, _quality = best
    text = clean_pages(pages, page_markers=page_markers)[:MAX_TEXT_CHARS]  # markers/joins add a little
    return PdfText(text=text, page_count=total, engine=name)


# ----------------------------------------------------------------------------------- cleaning
def _normalise_page(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # \x02 marks joins made by pdfium; \xa0 is NBSP; U+FEFF is a BOM some producers leave in text.
    text = text.replace("\x02", "").replace("\xa0", " ").replace("\N{ZERO WIDTH NO-BREAK SPACE}", "")
    text = _SOFT_HYPHEN_RE.sub("", text)
    text = _SPACES_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.split("\n"))


def _line_key(line: str) -> str:
    return re.sub(r"\d+", "#", line.casefold()).strip()


def _strip_running_lines(pages: list[list[str]]) -> list[list[str]]:
    """Remove running headers/footers and lone page numbers.

    A running line is a line (digits normalised) that occurs on at least 40 % of the pages (and on
    3 pages at least). Headers alternate between odd and even pages in many journals, and pypdf
    emits them wherever they are in the content stream, so the whole page is checked, not only its
    first and last lines. Page numbers are removed only among the first/last two lines of a page.
    """

    def edges(lines: list[str]) -> list[int]:
        idx = [i for i, ln in enumerate(lines) if ln]
        return idx[:2] + idx[-2:] if len(idx) > 4 else idx

    counts: Counter[str] = Counter()
    if len(pages) >= 3:
        for lines in pages:
            counts.update({_line_key(ln) for ln in lines if 8 <= len(ln) < 160})
    threshold = max(3, -(-len(pages) * 2 // 5))
    out = []
    for lines in pages:
        drop = {i for i in edges(lines) if _PAGE_NUMBER_RE.match(lines[i])}
        if counts:
            drop |= {i for i, ln in enumerate(lines) if 8 <= len(ln) < 160 and counts[_line_key(ln)] >= threshold}
        out.append([ln for i, ln in enumerate(lines) if i not in drop])
    return out


def _dehyphenate(text: str) -> str:
    """Join ``"биологиче-\\nского"`` into ``"биологического"``; keep real hyphens (``"из-за"``)."""

    def repl(m: re.Match[str]) -> str:
        left, space, right = m.group(1), m.group(2), m.group(3)
        if space:
            # "атероген -<newline>ность" (pypdf) is a broken word, "сети -<newline>это" is a dash.
            if right.lower() in _SHORT_RU or not right[0].islower() or not left[-1].islower():
                return f"{left} - {right}"
            return left + right
        if not (left[-1].islower() and right[0].islower()):
            return f"{left}-{right}"
        if _real_hyphen(left, right):
            return f"{left}-{right}"
        return left + right

    return _HYPHEN_BREAK_RE.sub(repl, text)


def _paragraphs(lines: list[str], typical: float) -> list[str]:
    paras: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            paras.append(" ".join(buf))
            buf.clear()

    prev = ""
    for line in lines:
        if not line:
            flush()
            prev = ""
            continue
        if buf and (
            _LIST_ITEM_RE.match(line)
            or (prev.endswith(_SENTENCE_END) and len(prev) < 0.8 * typical)
            or (len(prev) < 0.5 * typical and line[:1].isupper())
        ):
            flush()
        buf.append(line)
        prev = line
    flush()
    return paras


def clean_pages(pages: Sequence[str], *, page_markers: bool = False) -> str:
    """Normalise raw per-page text: de-hyphenate, drop running headers, join lines into paragraphs
    separated by blank lines. Paragraphs that continue on the next page are joined."""
    norm = [_dehyphenate(_normalise_page(p)) for p in pages]
    split = _strip_running_lines([p.split("\n") for p in norm])
    lengths = [len(ln) for lines in split for ln in lines if len(ln) > 20]
    typical = statistics.median(lengths) if lengths else 80.0
    out: list[str] = []
    for num, lines in enumerate(split, 1):
        paras = _paragraphs(lines, typical)
        if page_markers:
            out.append(f"[стр. {num}]")
        elif out and paras and not out[-1].endswith(_SENTENCE_END) and paras[0][:1].islower():
            last = out.pop()
            if last.endswith(("-", "\N{HYPHEN}")) and last[-2:-1].islower():
                out.append(last[:-1] + paras[0])
            else:
                out.append(f"{last} {paras[0]}")
            paras = paras[1:]
        out.extend(paras)
    text = "\n\n".join(p for p in out if p.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()
