"""Tests of the full-text area: PDF extraction, the external HTTP client, open sources, elibrary PDFs
and the orchestrator. No network: elibrary.ru is the conftest FakeSite, external hosts are an
``httpx2.MockTransport`` serving real responses saved on 2026-10-13 (``tests/fixtures/external``)."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import tracemalloc
import zlib
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest

from elibrary_cli import fulltext as ft
from elibrary_cli.config import Settings
from elibrary_cli.errors import (
    DependencyMissing,
    ElibraryError,
    LoginRequired,
    NotFound,
    OfflineCacheMiss,
    ParseError,
)
from elibrary_cli.fulltext import elibrary as el
from elibrary_cli.fulltext import net as netmod
from elibrary_cli.fulltext import pdf as pdfmod
from elibrary_cli.fulltext import sources as src
from elibrary_cli.fulltext.net import (
    ExternalChallenge,
    ExternalHTTP,
    ExternalHTTPError,
    NotAPdf,
    RedirectToElibrary,
    ResponseTooLarge,
)
from tests.conftest import fixture_bytes

EXTERNAL = Path(__file__).parent / "fixtures" / "external"


def ext_bytes(name: str) -> bytes:
    return (EXTERNAL / name).read_bytes()


def ext_json(name: str):
    return json.loads(ext_bytes(name).decode("utf-8"))


# ------------------------------------------------------------------------------ PDF builder
_PDF_EXTRA = "АБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдежзийклмнопрстуфхцчшщъыьэюяЁё«»—–№"
_PDF_CODES = {chr(c): c for c in range(0x20, 0x7F)} | {ch: 0x80 + i for i, ch in enumerate(_PDF_EXTRA)}


def _pdf_string(line: str) -> str:
    out = []
    for b in bytes(_PDF_CODES[ch] for ch in line):
        if b in (0x28, 0x29, 0x5C):
            out.append("\\" + chr(b))
        elif 0x20 <= b < 0x7F:
            out.append(chr(b))
        else:
            out.append(f"\\{b:03o}")
    return "(" + "".join(out) + ")"


def make_pdf(pages: list[list[str]]) -> bytes:
    """A minimal, deterministic PDF: Helvetica with a single-byte encoding whose ToUnicode CMap maps
    codes 0x80.. to Cyrillic letters, so pypdf and pdfminer extract real Russian text."""
    items = sorted(_PDF_CODES.items(), key=lambda kv: kv[1])
    blocks = []
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        body = "\n".join(f"<{code:02X}> <{ord(ch):04X}>" for ch, code in chunk)
        blocks.append(f"{len(chunk)} beginbfchar\n{body}\nendbfchar")
    cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n" + "\n".join(blocks) +
            "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n").encode("ascii")
    n_pages = len(pages)
    page_ids = [6 + 2 * i for i in range(n_pages)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Kids [" + " ".join(f"{p} 0 R" for p in page_ids) + f"] /Count {n_pages} >>").encode(),
        # Not a standard-14 name: pdfminer would use built-in Helvetica metrics instead of /Widths.
        3: ("<< /Type /Font /Subtype /Type1 /BaseFont /ElibTestCyr /FirstChar 32 /LastChar 255 /Widths ["
            + " ".join(["500"] * 224) + "] /FontDescriptor 5 0 R /ToUnicode 4 0 R >>").encode(),
        4: b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"endstream",
        5: (b"<< /Type /FontDescriptor /FontName /ElibTestCyr /Flags 32 /FontBBox [0 -200 1000 900] "
            b"/ItalicAngle 0 /Ascent 900 /Descent -200 /CapHeight 700 /StemV 80 >>"),
    }
    for i, lines in enumerate(pages):
        ops = ["BT", "/F1 10 Tf", "13 TL", "40 800 Td"]
        for line in lines:
            if line:
                ops.append(f"{_pdf_string(line)} Tj")
            ops.append("T*")
        ops.append("ET")
        content = ("\n".join(ops) + "\n").encode("latin-1")
        objects[page_ids[i]] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_ids[i] + 1} 0 R >>").encode()
        objects[page_ids[i] + 1] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream"
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


ARTICLE_LINES = [
    "Идиопатическое сальниковое кровоизлияние как причина гемоперитонеума",
    "",
    "В.В. Рязанов, Г.К. Садыкова",
    "",
    "Кровоизлияние в большой сальник — общий термин для обозначения патологического состояния, при",
    "котором происходит разрыв сальниковой артерии. Для биологиче-",
    "ского объяснения этого явления предложено несколько гипотез, из-",
    "за чего диагностика остаётся сложной и требует компьютерной томографии.",
    "Короткая строка.",
    "Новый абзац начинается здесь и продолжается достаточно длинной строкой текста для проверки",
    "склейки строк в абзацы при извлечении текста из документа формата PDF без лишних переносов.",
    "- первый пункт списка;",
    "- второй пункт списка.",
]


def flate_pdf(contents: list[bytes]) -> bytes:
    """A PDF with one page per content stream, each stream FlateDecode-compressed (Helvetica)."""
    n = len(contents)
    page_ids = [4 + 2 * i for i in range(n)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Kids [" + " ".join(f"{p} 0 R" for p in page_ids) + f"] /Count {n} >>").encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for pid, content in zip(page_ids, contents, strict=True):
        comp = zlib.compress(content, 9)
        objects[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {pid + 1} 0 R "
                        "/Resources << /Font << /F1 3 0 R >> >> >>").encode()
        objects[pid + 1] = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp) + comp + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offsets[num]:010d} 00000 n \n".encode() for num in sorted(objects))
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def bomb_pdf(inflated: int, *, pages: int = 1, text: str = "Hello") -> bytes:
    """Decompression bomb: every page shows one line, then ``inflated`` bytes of spaces (which
    compress about 1000:1)."""
    return flate_pdf([f"BT /F1 12 Tf 72 720 Td ({text} {i + 1}) Tj ET\n".encode() + b" " * inflated
                      for i in range(pages)])


MIB = 1024 * 1024


def article_pdf(title_line: str | None = None, pages: int = 3) -> bytes:
    """A 3-page Russian 'article' with a running header and page numbers."""
    first = list(ARTICLE_LINES)
    if title_line is not None:
        first[0] = title_line
    body = [
        "Результаты исследования показали, что при ультразвуковом исследовании и компьютерной",
        "томографии выявляется гематома в проекции большого сальника у большинства пациентов, а",
        "лечение чаще всего консервативное, но в ряде случаев требуется оперативное вмешательство.",
    ]
    out = []
    for i in range(pages):
        lines = ["Известия Российской Военно-медицинской академии. 2023. Т. 42, № 3"]
        lines += first if i == 0 else body * 3
        lines += ["", str(319 + i)]
        out.append(lines)
    return make_pdf(out)


# ------------------------------------------------------------------------------ fake hosts
class ExtSite:
    """Routes external requests (by host + path substring) to canned responses; records requests."""

    def __init__(self) -> None:
        self.routes: list[tuple[Callable[[httpx2.Request], bool], Callable[[httpx2.Request], httpx2.Response]]] = []
        self.requests: list[httpx2.Request] = []

    def add(self, pred, handler) -> ExtSite:
        self.routes.append((pred, handler))
        return self

    def on(self, contains: str, *, body: bytes = b"", status: int = 200, ctype: str = "application/json",
           headers: dict | None = None, method: str | None = None) -> ExtSite:
        hdrs = {"Content-Type": ctype, **(headers or {})}
        return self.add(
            lambda r, c=contains, m=method: c in str(r.url) and (m is None or r.method == m),
            lambda r, b=body, s=status, h=hdrs: httpx2.Response(s, content=b, headers=h),
        )

    def json(self, contains: str, fixture: str, **kw) -> ExtSite:
        return self.on(contains, body=ext_bytes(fixture), **kw)

    def html(self, contains: str, body: bytes, **kw) -> ExtSite:
        return self.on(contains, body=body, ctype="text/html; charset=utf-8", **kw)

    def pdf(self, contains: str, body: bytes) -> ExtSite:
        return self.on(contains, body=body, ctype="application/pdf")

    def exact(self, url: str, body: bytes, *, ctype: str = "text/html; charset=utf-8", status: int = 200) -> ExtSite:
        """Serve ``body`` for exactly this URL (``on`` matches substrings: ``/x`` would also serve ``/x-1``)."""
        return self.add(lambda r, u=url: str(r.url) == u,
                        lambda r, b=body, s=status, c=ctype: httpx2.Response(s, content=b, headers={"Content-Type": c}))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        for pred, handler in self.routes:
            if pred(request):
                return handler(request)
        return httpx2.Response(404, content=b"Resource not found.", headers={"Content-Type": "text/plain"})

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


@pytest.fixture
def ext() -> ExtSite:
    return ExtSite()


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def net(settings: Settings, ext: ExtSite, fake_time: FakeTime):
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), sleep=fake_time.sleep,
                          clock=fake_time.clock)
    yield client
    client.close()


# =============================================================================== pdf.py
def test_extract_cyrillic_pdf_with_pypdf():
    res = pdfmod.extract_pdf_text(article_pdf())
    assert res.engine == "pypdf"
    assert res.page_count == 3
    text = res.text
    assert text.startswith("Идиопатическое сальниковое кровоизлияние как причина гемоперитонеума")
    # hyphenation at line ends: a broken word is joined, a real hyphen ("из-за") is kept
    assert "биологического объяснения" in text
    assert "из-за чего диагностика" in text
    # lines of one paragraph are joined with spaces; a short line ending a sentence closes it
    assert "при котором происходит разрыв" in text
    assert "Короткая строка.\n\nНовый абзац начинается здесь" in text
    # list items become paragraphs of their own
    assert "\n\n- первый пункт списка;\n\n- второй пункт списка." in text
    # running header and page numbers are removed
    assert "Военно-медицинской академии. 2023" not in text
    assert not re.search(r"(?m)^3(19|20|21)$", text)
    # the paragraph continuing on the next page is not split by a page break
    assert "\n\n\n" not in text


def test_extract_pdf_max_pages_and_markers(tmp_path: Path):
    path = tmp_path / "a.pdf"
    path.write_bytes(article_pdf())
    first = pdfmod.extract_pdf_text(path, max_pages=1)
    assert first.page_count == 3
    assert "Идиопатическое" in first.text and "Результаты исследования" not in first.text
    marked = pdfmod.extract_pdf_text(path, page_markers=True)
    assert marked.text.startswith("[стр. 1]\n\nИдиопатическое")
    assert "[стр. 2]\n\nРезультаты исследования" in marked.text
    assert "[стр. 3]" in marked.text


def test_pdfminer_engine_reads_the_same_cyrillic_text():
    pages, total = pdfmod._extract_pdfminer(article_pdf(), None)
    assert total == 3 and len(pages) == 3
    assert "Идиопатическое сальниковое кровоизлияние" in pages[0]
    assert "компьютерной" in pages[1]


def test_falls_back_to_pdfminer_when_pypdf_text_is_broken(monkeypatch):
    broken = ["Нейрон ы входного уровн я получ ают инф ормаци ю от друг их нейрон ов сет и " * 4] * 3
    monkeypatch.setattr(pdfmod, "_extract_pypdf", lambda data, max_pages: (broken, 3))
    assert pdfmod.text_quality(broken).broken
    res = pdfmod.extract_pdf_text(article_pdf())
    assert res.engine == "pdfminer"
    assert "биологического объяснения" in res.text


def test_keeps_pypdf_when_fallback_is_worse(monkeypatch):
    broken = ["Нейрон ы входного уровн я получ ают инф ормаци ю " * 30] * 3
    monkeypatch.setattr(pdfmod, "_extract_pypdf", lambda data, max_pages: (broken, 3))
    monkeypatch.setattr(pdfmod, "_extract_pdfminer", lambda data, max_pages: ([chr(0xFFFD) * 50] * 3, 3))
    assert pdfmod.extract_pdf_text(article_pdf()).engine == "pypdf"


def test_pdf_dependency_missing(monkeypatch):
    monkeypatch.setattr(pdfmod, "_engines", lambda: [])
    with pytest.raises(DependencyMissing) as info:
        pdfmod.extract_pdf_text(article_pdf())
    assert info.value.extra == "pdf" and info.value.package == "pypdf"
    assert "elibrary-cli[pdf]" in info.value.message


def test_not_a_pdf_raises_parse_error():
    with pytest.raises(ParseError):
        pdfmod.extract_pdf_text(b"<html><body>Paywall</body></html>")
    assert pdfmod.is_pdf(b"%PDF-1.7 ...") and not pdfmod.is_pdf(b"<html>")


def test_text_quality_flags_mojibake_and_fragments():
    clean_text = ["Нейронная сеть основана на том, как работает человеческий мозг. " * 10]
    q = pdfmod.text_quality(clean_text)
    assert q.ok and q.fragment_ratio == 0.0 and q.mojibake_ratio == 0.0
    moji = pdfmod.text_quality([("Ð¿Ñ€Ð¸Ð²ÐµÑ‚ " * 40)])
    assert moji.broken and moji.mojibake_ratio > 0.2
    cid = pdfmod.text_quality(["(cid:12)(cid:13) текст " * 30])
    assert cid.broken
    scan = pdfmod.text_quality(["", "12", ""])
    assert scan.too_short and not scan.ok
    # upper-case abbreviations and words split at a line end are not fragments
    assert pdfmod.text_quality(["При КТ и УЗИ выявлена ИБС, иссле-\nдование продолжено. " * 20]).fragment_ratio == 0.0


def test_clean_pages_rules():
    text = pdfmod.clean_pages(["Сердечно-\nсосудистых заболеваний по-\nказана роль, по-\nпрежнему атероген -\nность\nвысока",
                               "и продолжает расти. Конец."])
    assert "Сердечно-сосудистых заболеваний показана роль, по-прежнему атерогенность высока и продолжает расти." in text
    assert pdfmod.clean_pages(["Нейронные сети -\nэто модели."]) == "Нейронные сети - это модели."


@pytest.fixture
def small_pdf_limits(monkeypatch):
    """Resource limits of pdf.py scaled down so that a bomb of a few MiB trips them quickly."""
    monkeypatch.setattr(pdfmod, "MAX_STREAM_BYTES", 1 * MIB, raising=False)
    monkeypatch.setattr(pdfmod, "MAX_DECODED_BYTES", 2 * MIB, raising=False)


def _spy_pdfminer(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = pdfmod._extract_pdfminer

    def spy(data, max_pages):
        calls.append(1)
        return real(data, max_pages)

    monkeypatch.setattr(pdfmod, "_extract_pdfminer", spy)
    return calls


def test_pypdf_resource_limit_disables_the_pdfminer_fallback(monkeypatch, small_pdf_limits):
    """Regression: pypdf stopped at its limit, the page came out empty ("too short"), and the file was
    handed to pdfminer, which inflated every FlateDecode stream without any limit."""
    calls = _spy_pdfminer(monkeypatch)
    with pytest.raises(ParseError) as info:
        pdfmod.extract_pdf_text(bomb_pdf(3 * MIB))
    assert calls == [], "no fallback engine after a resource limit"
    assert isinstance(info.value, pdfmod.PdfResourceLimit) and "PDF-бомба" in info.value.message


def test_pdfminer_decompression_is_bounded(small_pdf_limits):
    """pdfminer alone (it is also the only engine when pypdf is missing): the stream is inflated only
    up to the limit, not to its full size."""
    bomb = bomb_pdf(16 * MIB)
    error: ParseError | None = None
    tracemalloc.start()
    try:
        pdfmod._extract_pdfminer(bomb, None)
    except ParseError as exc:
        error = exc
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    assert peak < 8 * MIB, f"peak {peak / MIB:.1f} MiB: the stream was inflated past the limit"
    assert isinstance(error, pdfmod.PdfResourceLimit) and "1 МБ" in error.reason


def test_pdfminer_bounded_decoders(small_pdf_limits):
    """The stand-ins installed into pdfminer.pdftypes: bounded inside a budget, unchanged outside."""
    import pdfminer.pdftypes as pt

    pdfmod._hook_pdfminer()
    flate = zlib.compress(b" " * (3 * MIB), 9)
    runlength = bytes([129, 32]) * 30000  # 128 spaces per 2 bytes: 3.7 MiB
    assert len(pt.zlib.decompress(flate)) == 3 * MIB  # no budget active: plain zlib
    assert len(pt.rldecode(runlength)) == 128 * 30000
    for decode in (lambda: pt.zlib.decompress(flate), lambda: pt.zlib.decompressobj().decompress(flate),
                   lambda: pt.rldecode(runlength)):
        with pdfmod._decode_budget(), pytest.raises(pdfmod._DecodeLimit):
            decode()
    with pdfmod._decode_budget() as budget:
        assert pt.zlib.decompress(zlib.compress(b"abc")) == b"abc" and budget.used == 3
        with pytest.raises(zlib.error):  # a truncated stream is reported as zlib does
            pt.zlib.decompress(zlib.compress(b"abc" * 100)[:-6])


def test_pypdf_budget_covers_the_whole_document(monkeypatch, small_pdf_limits):
    """Streams under the per-stream limit still add up: extraction stops once the document has
    inflated more than MAX_DECODED_BYTES; the pages read so far are kept, pdfminer is not run."""
    calls = _spy_pdfminer(monkeypatch)
    res = pdfmod.extract_pdf_text(bomb_pdf(MIB - 4096, pages=6, text="Block"))
    assert res.engine == "pypdf" and res.page_count == 6
    assert "Block 1" in res.text and "Block 2" in res.text
    assert "Block 4" not in res.text and "Block 6" not in res.text
    assert calls == []


def test_extracted_text_is_capped(monkeypatch):
    assert len(pdfmod.extract_pdf_text(article_pdf()).text) > 1000
    monkeypatch.setattr(pdfmod, "MAX_TEXT_CHARS", 300, raising=False)
    res = pdfmod.extract_pdf_text(article_pdf())
    assert 0 < len(res.text) <= 300 and "Идиопатическое сальниковое" in res.text and res.page_count == 3
    pages, total = pdfmod._extract_pdfminer(article_pdf(), None)
    assert total == 3 and 0 < sum(map(len, pages)) <= 300


def test_pdfminer_characters_per_page_are_capped(monkeypatch):
    monkeypatch.setattr(pdfmod, "MAX_PDFMINER_CHARS_PER_PAGE", 50, raising=False)
    with pytest.raises(pdfmod.PdfResourceLimit) as info:
        pdfmod._extract_pdfminer(article_pdf(), None)
    assert "больше 50 символов" in info.value.reason


# =============================================================================== net.py
def test_user_agent_is_honest(settings: Settings):
    generic = f"elibrary-cli/{netmod.__version__} (+https://github.com/lujo777/elibrary-cli)"
    assert netmod.build_user_agent(None) == generic
    settings.contact_email = "me@example.org"
    client = ExternalHTTP(settings, cache=False)
    # the e-mail goes only to the scholarly APIs that ask for a contact
    assert client.user_agent == generic
    assert client.contact_user_agent.endswith("(+https://github.com/lujo777/elibrary-cli; mailto:me@example.org)")
    assert client.contact_user_agent.startswith("elibrary-cli/")
    for url in ("https://api.crossref.org/works/10.1/a", "https://api.openalex.org/works/doi:10.1/a",
                "https://api.unpaywall.org/v2/10.1/a", "https://api.datacite.org/dois/10.24412/a"):
        assert client.user_agent_for(url) == client.contact_user_agent, url
    for url in ("https://journals.eco-vector.com/a.pdf", "https://cyberleninka.ru/article/n/x",
                "https://hdl.handle.net/api/handles/10.1/a", "https://crossref.org.evil.example/"):
        assert client.user_agent_for(url) == generic, url
    client.close()


def test_contact_email_is_not_sent_to_other_hosts(settings: Settings, ext: ExtSite):
    """Regression: the e-mail used to be the client-wide User-Agent, sent to every publisher,
    repository, redirect target and robots.txt host."""
    settings.contact_email = "researcher@example.org"
    ext.on("api.crossref.org/works/10.1/moved", status=302,
           headers={"Location": "https://some-repository.example.net/landing/2"})
    ext.json("api.crossref.org/works/10.1/a", "crossref_work_rmmar424450.json")
    ext.html("some-repository.example.net/landing", b"<html><body>landing</body></html>")
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), cache=False, default_delay=0)
    client.get_json("https://api.crossref.org/works/10.1/a")
    client.get_page("https://some-repository.example.net/landing/1", robots=True)
    client.get_page("https://api.crossref.org/works/10.1/moved")
    client.close()
    seen = [(str(r.url), r.headers["user-agent"]) for r in ext.requests]
    assert [u for u, _ in seen] == [
        "https://api.crossref.org/works/10.1/a",
        "https://some-repository.example.net/robots.txt",
        "https://some-repository.example.net/landing/1",
        "https://api.crossref.org/works/10.1/moved",
        "https://some-repository.example.net/landing/2",
    ]
    with_mail = [u for u, ua in seen if "researcher@example.org" in ua]
    assert with_mail == ["https://api.crossref.org/works/10.1/a", "https://api.crossref.org/works/10.1/moved"]
    assert all(ua.startswith("elibrary-cli/") for _, ua in seen)


def test_non_transient_transport_error_is_external_http_error(net: ExternalHTTP, ext: ExtSite):
    def proxy_fails(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ProxyError("proxy refused the connection")

    ext.add(lambda r: "api.crossref.org" in str(r.url), proxy_fails)
    with pytest.raises(ExternalHTTPError) as ei:
        net.get_json("https://api.crossref.org/works/10.1000/xyz")
    assert str(ei.value).startswith("Запрос к api.crossref.org не удался (ProxyError")
    assert net.requests_sent == 1, "a proxy error is not retried"


def test_refuses_elibrary_hosts(net: ExternalHTTP, ext: ExtSite, tmp_path: Path):
    for url in ("https://elibrary.ru/item.asp?id=1", "https://www.elibrary.ru/", "http://ELIBRARY.RU/x"):
        with pytest.raises(AssertionError):
            net.get_json(url)
    with pytest.raises(AssertionError):
        net.download("https://www.elibrary.ru/download/elibrary_1_2.pdf", tmp_path / "never.pdf")
    assert ext.requests == [] and not list(tmp_path.glob("never.pdf*"))
    assert netmod.is_elibrary_host("https://www.elibrary.ru/a") and not netmod.is_elibrary_host("notelibrary.ru")


def test_redirect_to_elibrary_is_stopped(net: ExternalHTTP, ext: ExtSite):
    ext.on("publisher.example/landing", status=302, headers={"Location": "https://elibrary.ru/item.asp?id=5"})
    with pytest.raises(RedirectToElibrary) as info:
        net.get_page("https://publisher.example/landing")
    assert info.value.location == "https://elibrary.ru/item.asp?id=5"
    assert ext.urls() == ["https://publisher.example/landing"]


def test_follows_external_redirects(net: ExternalHTTP, ext: ExtSite):
    ext.on("a.example/old", status=301, headers={"Location": "/new"})
    ext.html("a.example/new", b"<html><body>moved</body></html>")
    page = net.get_page("https://a.example/old")
    assert page.url == "https://a.example/new" and "moved" in page.text


def test_private_hosts_are_refused(net: ExternalHTTP):
    for url in ("http://127.0.0.1/x", "http://localhost:8080/", "http://10.1.2.3/", "ftp://example.org/a"):
        with pytest.raises(ExternalHTTPError):
            net.get_bytes(url)


def _fake_dns(monkeypatch, table: dict) -> list[str]:
    """Replace ``socket.getaddrinfo`` (no real DNS in tests): a name of ``table`` resolves to the listed
    addresses (or to what a callable returns), an IP literal to itself, anything else fails.
    Returns the list of looked-up names."""
    calls: list[str] = []

    def getaddrinfo(host, port, *args, **kwargs):
        host = host.decode() if isinstance(host, bytes) else host
        calls.append(host)
        addrs = table.get(host)
        if callable(addrs):
            addrs = addrs()
        if addrs is None:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise socket.gaierror(socket.EAI_NONAME, f"unknown host {host}") from None
            addrs = [host]
        out = []
        for a in addrs:
            if ":" in a:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, int(port or 0), 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, int(port or 0))))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return calls


def test_dns_names_of_private_addresses_are_refused(settings: Settings, ext: ExtSite, monkeypatch, tmp_path: Path):
    """Regression (SSRF): a public-looking name whose A/AAAA record is loopback, LAN or link-local
    (localtest.me, 127.0.0.1.nip.io, attacker DNS) used to be fetched; so was a redirect to one."""
    _fake_dns(monkeypatch, {
        "public.example": ["93.184.216.34"],
        "intranet.example": ["127.0.0.1"],
        "lan.example": ["10.0.0.1"],
        "metadata.example": ["169.254.169.254"],
        "mixed.example": ["93.184.216.34", "192.168.1.1"],
        "v6.example": ["::1"],
        "mapped.example": ["::ffff:10.0.0.7"],
    })
    ext.html("public.example/ok", b"<html>ok</html>")
    ext.on("public.example/go", status=302, headers={"Location": "http://intranet.example:8080/admin"})
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), cache=False, default_delay=0,
                          check_dns=True)
    for host in ("intranet.example", "lan.example", "metadata.example", "mixed.example", "v6.example",
                 "mapped.example"):
        with pytest.raises(netmod.PrivateAddress) as info:
            client.get_page(f"https://{host}/landing", robots=True)
        assert info.value.host == host and "непубличный адрес" in info.value.message
    with pytest.raises(netmod.PrivateAddress):
        client.download_pdf("http://lan.example/a.pdf", tmp_path / "a.pdf", robots=True)
    assert ext.requests == [], "neither robots.txt nor the page was requested"
    assert client.get_page("https://public.example/ok").text == "<html>ok</html>"
    with pytest.raises(netmod.PrivateAddress) as info:
        client.get_page("https://public.example/go", robots=True)
    assert info.value.url == "http://intranet.example:8080/admin"
    assert ext.urls() == ["https://public.example/ok", "https://public.example/robots.txt",
                          "https://public.example/go"]
    client.close()
    # the check is on by default for the real network only (test transports serve made-up hosts)
    assert ExternalHTTP(settings, cache=False).check_dns
    assert not ExternalHTTP(settings, transport=httpx2.MockTransport(ext), cache=False).check_dns


def test_real_client_connects_only_to_vetted_addresses(settings: Settings, monkeypatch):
    """DNS rebinding: the name is public for the pre-check, then resolves to loopback. The network
    backend resolves, checks and connects to the checked address itself (a local server stands in
    for the user's LAN; nothing leaves the machine)."""
    import http.server
    import threading

    monkeypatch.setattr("httpx2._utils.getproxies", lambda: {})  # no system proxy in this test
    hits: list[tuple[str | None, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append((self.headers.get("Host"), self.path))
            body = b"<html>internal</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    answers = iter([["93.184.216.34"]])
    calls = _fake_dns(monkeypatch, {"rebind.example": lambda: next(answers, ["127.0.0.1"])})
    client = ExternalHTTP(settings, cache=False, default_delay=0)
    try:
        url = f"http://rebind.example:{port}/internal"
        with pytest.raises(netmod.PrivateAddress) as info:
            client.get_page(url)
        assert client.pinned
        assert info.value.address == "127.0.0.1" and info.value.url == url
        assert hits == [] and calls.count("rebind.example") == 2
        # the backend connects to the address it has just checked, without another lookup of the name
        monkeypatch.setattr(netmod, "is_public_ip", lambda address: True)
        calls.clear()
        assert client.get_page(f"http://rebind.example:{port}/allowed").text == "<html>internal</html>"
        assert hits == [(f"rebind.example:{port}", "/allowed")]
        assert calls.count("rebind.example") == 2  # pre-check + backend; the socket got the IP itself
    finally:
        client.close()
        server.shutdown()
        server.server_close()


def test_per_host_throttle(settings: Settings, ext: ExtSite, fake_time: FakeTime):
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json")
    ext.json("api.crossref.org", "crossref_work_rmmar424450.json")
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), cache=False, sleep=fake_time.sleep,
                          clock=fake_time.clock)
    assert client.delay_for("https://cyberleninka.ru/api/search") == 3.0
    assert client.delay_for("api.crossref.org") == 1.0
    client.post_json("https://cyberleninka.ru/api/search", {"q": "a"})
    assert fake_time.sleeps == []  # the first request to a host goes at once
    client.post_json("https://cyberleninka.ru/api/search", {"q": "b"})
    assert fake_time.sleeps == [3.0]
    client.get_json("https://api.crossref.org/works/10.1/a")  # another host: no pause
    client.get_json("https://api.crossref.org/works/10.1/b")
    assert fake_time.sleeps == [3.0, 1.0]
    client.close()


def test_cache_refresh_and_offline(settings: Settings, ext: ExtSite):
    ext.json("api.crossref.org/works/10.17816", "crossref_work_rmmar424450.json")
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), default_delay=0)
    url = "https://api.crossref.org/works/10.17816/rmmar424450"
    first = client.get_json(url)
    second = client.get_json(url)
    assert first == second and first["message"]["DOI"] == "10.17816/rmmar424450"
    assert len(ext.requests) == 1
    assert (settings.cache_dir / "external-cache.sqlite3").exists()
    settings.refresh = True
    client.get_json(url)
    assert len(ext.requests) == 2
    settings.refresh = False
    settings.offline = True
    assert client.get_json(url)["message"]["volume"] == "42"
    with pytest.raises(OfflineCacheMiss):
        client.get_json("https://api.crossref.org/works/10.1/other")
    assert len(ext.requests) == 2
    client.close()


def test_post_cache_key_includes_body(net: ExternalHTTP, ext: ExtSite):
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json", method="POST")
    net.post_json("https://cyberleninka.ru/api/search", {"q": "a"})
    net.post_json("https://cyberleninka.ru/api/search", {"q": "a"})
    net.post_json("https://cyberleninka.ru/api/search", {"q": "b"})
    assert len(ext.requests) == 2
    assert json.loads(ext.requests[0].content) == {"q": "a"}
    assert ext.requests[0].headers["content-type"] == "application/json"


def test_size_cap(settings: Settings, ext: ExtSite):
    ext.on("big.example/declared", body=b"x" * 10, headers={"Content-Length": str(10**9)})
    ext.add(lambda r: "big.example/stream" in str(r.url),
            lambda r: httpx2.Response(200, content=iter([b"x" * 600, b"y" * 600]),
                                      headers={"Content-Type": "application/octet-stream"}))
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), cache=False, default_delay=0,
                          max_bytes=1000)
    with pytest.raises(ResponseTooLarge):
        client.get_bytes("https://big.example/declared")
    with pytest.raises(ResponseTooLarge):
        client.get_bytes("https://big.example/stream")
    client.close()


def test_challenge_page_stops_the_host(net: ExternalHTTP, ext: ExtSite):
    captcha = "<html><head><title>Проверка</title></head><body>Вы точно человек?</body></html>".encode()
    ext.html("cyberleninka.ru", captcha)
    with pytest.raises(ExternalChallenge) as info:
        net.post_json("https://cyberleninka.ru/api/search", {"mode": "articles", "q": "x"})
    assert info.value.host == "cyberleninka.ru"
    assert "Вы точно человек" in info.value.message
    with pytest.raises(ExternalChallenge):
        net.get_page("https://cyberleninka.ru/article/n/x")
    assert len(ext.requests) == 1  # the second call was not sent


def test_status_handling_and_retry(net: ExternalHTTP, ext: ExtSite, fake_time: FakeTime):
    with pytest.raises(NotFound):
        net.get_json("https://api.crossref.org/works/10.0/none")
    calls = {"n": 0}

    def flaky(r: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(429, headers={"Retry-After": "7"})
        return httpx2.Response(200, json={"ok": True})

    ext.add(lambda r: "api.openalex.org" in str(r.url), flaky)
    assert net.get_json("https://api.openalex.org/works/doi:10.1/a") == {"ok": True}
    assert 7.0 in fake_time.sleeps
    ext.on("api.unpaywall.org", status=422, body=b'{"error": true, "message": "Please use your own email"}')
    with pytest.raises(ExternalHTTPError) as info:
        net.get_json("https://api.unpaywall.org/v2/10.1/a", params={"email": "x@y.z"})
    assert info.value.status == 422
    ext.html("html.example", b"<html>not json</html>")
    with pytest.raises(ExternalHTTPError):
        net.get_json("https://html.example/api")


def test_download_pdf_checks_magic(net: ExternalHTTP, ext: ExtSite, tmp_path: Path):
    pdf_bytes = article_pdf()
    ext.pdf("pub.example/good.pdf", pdf_bytes)
    ext.html("pub.example/stub.pdf", b"<html><body>Please log in to read this article</body></html>" * 30)
    path = net.download_pdf("https://pub.example/good.pdf", tmp_path / "good.pdf", referer="https://pub.example/")
    assert path.read_bytes() == pdf_bytes
    assert ext.requests[-1].headers["referer"] == "https://pub.example/"
    with pytest.raises(NotAPdf):
        net.download_pdf("https://pub.example/stub.pdf", tmp_path / "stub.pdf")
    assert not (tmp_path / "stub.pdf").exists() and not (tmp_path / "stub.pdf.part").exists()


#: robots.txt of cyberleninka.ru for "User-Agent: *" (rules as fetched on 2026-10-13).
CL_ROBOTS = b"""User-Agent: *
Disallow: /search/*
Disallow: /search?
Disallow: /user/*
Disallow: /donate/*
Disallow: /article/n/*/viewer
Disallow: /article/v/*
Disallow: /api/
Disallow: /article/n/*/pdf
Disallow: /article/n/*.pdf
Disallow: /viewer_images/*
"""


def test_robots_rules_cyberleninka():
    rules = netmod.RobotsRules.from_response(200, CL_ROBOTS)
    ua = netmod.build_user_agent("me@example.org")
    base = "https://cyberleninka.ru"
    for path in ("/article/n/neyrony-v-neyronnyh-setyah", "/article/n/neyronnye-seti-1", "/journal/n/x", "/"):
        assert rules.can_fetch(ua, base + path), path
    for path in ("/api/search", "/search?q=x", "/search/x", "/article/n/neyrony-v-neyronnyh-setyah/pdf",
                 "/article/n/neyrony-v-neyronnyh-setyah.pdf", "/article/n/x/viewer", "/article/v/x",
                 "/user/1", "/donate/", "/viewer_images/19672049/1.png"):
        assert not rules.can_fetch(ua, base + path), path


def test_robots_rules_matching_and_statuses():
    ua = netmod.build_user_agent(None)
    rules = netmod.RobotsRules.from_response(200, (
        b"User-agent: *\nDisallow: /jour/\nAllow: /jour/article/view/\nDisallow: /*.pdf$\nDisallow: /tmp\n"
        b"Allow: /tmp\n\nUser-agent: elibrary-cli\nDisallow: /private/\n"))
    # our own group applies instead of "*"
    assert not rules.can_fetch(ua, "https://x.example/private/a") and rules.can_fetch(ua, "https://x.example/jour/")
    star = netmod.RobotsRules.from_response(200, b"User-agent: *\nDisallow: /jour/\nAllow: /jour/article/view/\n"
                                                 b"Disallow: /*.pdf$\nDisallow: /tmp\nAllow: /tmp\n")
    assert not star.can_fetch(ua, "https://x.example/jour/issue/1")
    assert star.can_fetch(ua, "https://x.example/jour/article/view/1610")  # the longer Allow wins
    assert not star.can_fetch(ua, "https://x.example/files/a.pdf") and star.can_fetch(ua, "https://x.example/a.pdf?x=1")
    assert star.can_fetch(ua, "https://x.example/tmp/x")  # equal length: Allow wins
    assert netmod.RobotsRules.from_response(404).can_fetch(ua, "https://x.example/a")
    assert netmod.RobotsRules.from_response(410).can_fetch(ua, "https://x.example/a")
    for status in (401, 403, 429, 500, 503):
        assert not netmod.RobotsRules.from_response(status).can_fetch(ua, "https://x.example/a"), status


def test_robots_txt_is_fetched_once_cached_and_enforced(settings: Settings, ext: ExtSite, fake_time: FakeTime):
    ext.on("pub.example/robots.txt", body=b"User-agent: *\nDisallow: /private/\n", ctype="text/plain")
    ext.html("pub.example/open", b"<html><body>open</body></html>")
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), sleep=fake_time.sleep, clock=fake_time.clock)
    with pytest.raises(netmod.RobotsDisallowed) as info:
        client.get_page("https://pub.example/private/x", robots=True)
    assert info.value.host == "pub.example" and "robots.txt" in info.value.message
    assert "запрещает" in info.value.message
    assert client.get_page("https://pub.example/open", robots=True).text.endswith("open</body></html>")
    with pytest.raises(netmod.RobotsDisallowed):
        client.download_pdf("https://pub.example/private/a.pdf", settings.cache_dir / "a.pdf", robots=True)
    assert ext.urls() == ["https://pub.example/robots.txt", "https://pub.example/open"]
    assert not (settings.cache_dir / "a.pdf.part").exists()
    client.close()
    # another process: robots.txt comes from the external cache
    other = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), sleep=fake_time.sleep, clock=fake_time.clock)
    assert other.robots_allowed("https://pub.example/x") and not other.robots_allowed("https://pub.example/private/")
    assert len(ext.requests) == 2
    other.close()
    # without robots=True nothing is checked (JSON APIs, explicit calls)
    ext.json("pub.example/private/api", "crossref_work_rmmar424450.json")
    fresh = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), cache=False, default_delay=0)
    assert fresh.get_json("https://pub.example/private/api")["status"] == "ok"
    assert ext.urls()[-1] == "https://pub.example/private/api"
    fresh.close()


def test_robots_txt_failures(settings: Settings, ext: ExtSite, fake_time: FakeTime):
    def no_connection(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    ext.add(lambda r: str(r.url) == "https://down.example/robots.txt", no_connection)
    ext.html("down.example/page", b"<html>page</html>")
    ext.on("busy.example/robots.txt", status=503, body=b"busy")
    ext.on("closed.example/robots.txt", status=403, body=b"forbidden")
    ext.html("nobots.example/page", b"<html>page</html>")  # robots.txt: 404
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), sleep=fake_time.sleep, clock=fake_time.clock)
    assert client.get_page("https://down.example/page", robots=True).text == "<html>page</html>"  # fail open
    assert client.get_page("https://nobots.example/page", robots=True).status == 200
    for host in ("busy.example", "closed.example"):
        with pytest.raises(netmod.RobotsDisallowed):
            client.get_page(f"https://{host}/page", robots=True)
    assert not any(u.endswith("/page") and ("busy" in u or "closed" in u) for u in ext.urls())
    # a network error or a 5xx is not remembered for a day: robots.txt is asked again after 10 minutes
    n = len(ext.requests)
    assert not client.robots_allowed("https://busy.example/page") and len(ext.requests) == n
    fake_time.now += netmod.ROBOTS_RETRY + 1
    assert not client.robots_allowed("https://busy.example/page") and len(ext.requests) > n
    client.close()


def test_robots_checked_on_every_redirect_hop(net: ExternalHTTP, ext: ExtSite):
    ext.on("doi-proxy.example/landing", status=302, headers={"Location": "https://pub.example/private/1"})
    ext.on("pub.example/robots.txt", body=b"User-agent: *\nDisallow: /private/\n", ctype="text/plain")
    with pytest.raises(netmod.RobotsDisallowed) as info:
        net.get_page("https://doi-proxy.example/landing", robots=True)
    assert info.value.url == "https://pub.example/private/1"
    assert "https://pub.example/private/1" not in ext.urls()


def test_download_size_cap_from_settings(settings: Settings, ext: ExtSite, tmp_path: Path):
    settings.max_download_mb = 0.001  # 1048 bytes
    ext.add(lambda r: "big.example/file.pdf" in str(r.url),
            lambda r: httpx2.Response(200, content=iter([b"%PDF-1.4 " + b"x" * 700, b"y" * 700]),
                                      headers={"Content-Type": "application/pdf"}))
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), cache=False, default_delay=0)
    assert client.max_download_bytes == 1048 and client.max_bytes == netmod.MAX_BYTES
    with pytest.raises(ResponseTooLarge) as info:
        client.download_pdf("https://big.example/file.pdf", tmp_path / "file.pdf")
    assert "ELIBRARY_MAX_DOWNLOAD_MB" in info.value.message and "удалён" in info.value.message
    assert list(tmp_path.iterdir()) == []
    client.close()


def test_cyberleninka_files_are_never_downloaded(net: ExternalHTTP, ext: ExtSite, tmp_path: Path):
    for url in ("https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah/pdf",
                "https://cyberleninka.ru/viewer_images/19672049/1.png"):
        with pytest.raises(netmod.RobotsDisallowed) as info:
            net.download(url, tmp_path / "x.pdf")
        assert "КиберЛенинки не скачиваются" in info.value.message
    ext.on("mirror.example/a.pdf", status=302, headers={"Location": "https://cyberleninka.ru/article/n/x/pdf"})
    with pytest.raises(netmod.RobotsDisallowed):
        net.download_pdf("https://mirror.example/a.pdf", tmp_path / "x.pdf")
    assert ext.urls() == ["https://mirror.example/a.pdf"] and not list(tmp_path.glob("x.pdf*"))


# =============================================================================== sources.py
def test_normalisation_helpers():
    assert src.normalize_doi("https://doi.org/10.18699/SSMJ20240404.") == "10.18699/ssmj20240404"
    assert src.normalize_doi("DOI: 10.24412/2071-6168-2023-2-438-443") == "10.24412/2071-6168-2023-2-438-443"
    assert src.normalize_doi("нет") is None
    assert src.find_dois("DOI: 10.1000/a, see https://doi.org/10.2000/B.") == ["10.1000/a", "10.2000/b"]
    assert src.strip_tags("<b>НЕЙРОНЫ</b> В <b>НЕЙРОННЫХ</b> СЕТЯХ") == "НЕЙРОНЫ В НЕЙРОННЫХ СЕТЯХ"
    # Latin "H" and "e" inside Cyrillic words, ё, quotes and punctuation
    assert src.normalize_title("Hейроны в «нейронных» сетях: ёжики!") == "нейроны в нейронных сетях ежики"
    assert src.normalize_title("Nеural networks") == "neural networks"


def test_title_similarity():
    assert src.title_similarity("НЕЙРОНЫ В НЕЙРОННЫХ СЕТЯХ", "Нейроны в нейронных сетях") == 1.0
    near = src.title_similarity("Модифицированный алгоритм обучения нейронных сетей",
                                "Модифицированный алгоритм обучения сверточных нейронных сетей")
    assert 0.8 < near < src.TITLE_THRESHOLD
    assert src.title_similarity("Нейронные сети", "Нейронные сети в медицине") < 0.7
    assert src.title_similarity("", "x") == 0.0


@pytest.mark.parametrize(("name", "expected"), [
    ("Петров В.А.", "Петров"), ("В.А. Петров", "Петров"), ("Панарин Владимир Михайлович", "Панарин"),
    ("Vladimir V. Ryazanov", "Ryazanov"), ("Ryazanov, Vladimir V.", "Ryazanov"), ("A. P. Vasiliev", "Vasiliev"),
    ("Anastasiya Ya. Latysheva", "Latysheva"), ("Vasiliev Alexander Petrovich", "Vasiliev"),
    ("Бекназаров С. Б.", "Бекназаров"),
])
def test_surname(name: str, expected: str):
    assert src.surname(name) == expected


def test_authors_match_across_scripts():
    assert src.authors_match(["Васильев А.П."], ["Vasiliev, A. P."]) is True
    assert src.authors_match(["Рязанов В.В."], ["Vladimir V. Ryazanov"]) is True
    assert src.authors_match(["Ходкевич И.С."], ["Ilya S. Khodkevich"]) is True
    assert src.authors_match(["Панарин В.М."], ["Владимир Панарин"]) is True  # unusual order in the candidate
    assert src.authors_match(["Панарин В.М."], ["Ксенофонтов Вадим Валерьевич"]) is False
    assert src.authors_match([], ["Петров В.А."]) is None


def test_match_score_rules():
    q = ft.FulltextRequest(title="Нейронные сети", authors=["Ксенофонтов В.В."], year=2020)
    hits = src.parse_cyberleninka_search(ext_json("cyberleninka_search_neurons.json"))
    assert src.match_score(q, titles=[hits[1].title], authors=hits[1].authors, year=hits[1].year) == 1.0
    # same generic title, other author: rejected
    other = ft.FulltextRequest(title="Нейронные сети", authors=["Иванов И.И."], year=2020)
    assert src.match_score(other, titles=[hits[1].title], authors=hits[1].authors, year=2020) == 0.0
    # year too far
    assert src.match_score(q, titles=[hits[1].title], authors=hits[1].authors, year=2016) == 0.0
    # no authors to compare: a strict title match is needed
    bare = ft.FulltextRequest(title="Нейронные сети")
    assert src.match_score(bare, titles=["НЕЙРОННЫЕ СЕТИ"]) == 1.0
    assert src.match_score(bare, titles=["Нейронные сети и их применение"]) == 0.0
    # DOI decides when both sides have one
    d = ft.FulltextRequest(doi="10.1000/A", title="x")
    assert src.match_score(d, titles=["other"], doi="https://doi.org/10.1000/a") == 1.0
    assert src.match_score(d, titles=["x"], doi="10.1000/b") == 0.0
    # English title of the request matches an English-only record
    en = ft.FulltextRequest(title="Противоречивость ...", title_en="Contradictions in traditional ideas",
                            authors=["Васильев А.П."], year=2024)
    assert src.match_score(en, titles=["Contradictions in traditional ideas"], authors=["Vasiliev, A. P."],
                           year=2024) == 1.0


def test_title_in_text():
    text = "УДК 616\n\nИДИОПАТИЧЕСКОЕ САЛЬНИКОВОЕ КРОВОИЗЛИЯНИЕ как при-\nчина гемоперитонеума\n\nТекст"
    assert src.title_in_text(["Идиопатическое сальниковое кровоизлияние как причина гемоперитонеума"],
                             pdfmod.clean_pages([text]))
    assert not src.title_in_text(["Нейроны в нейронных сетях"], text)


def test_parse_cyberleninka_search_fixtures():
    hits = src.parse_cyberleninka_search(ext_json("cyberleninka_search_neurons.json"))
    assert [h.title for h in hits] == ["НЕЙРОНЫ В НЕЙРОННЫХ СЕТЯХ", "НЕЙРОННЫЕ СЕТИ", "НЕЙРОННАЯ СЕТЬ"]
    assert hits[0].url == "https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah"
    assert hits[0].authors[0] == "Панарин Владимир Михайлович" and len(hits[0].authors) == 5
    assert hits[0].year == 2023
    assert hits[0].journal == "Известия Тульского государственного университета. Технические науки"
    assert hits[0].snippet and "<b>" not in hits[0].snippet
    assert hits[1].url.endswith("/article/n/neyronnye-seti-1") and hits[2].authors == ["Бекназаров С. Б."]
    atero = src.parse_cyberleninka_search(ext_json("cyberleninka_search_atero.json"))
    assert len(atero) == 5
    assert atero[0].title.startswith("Противоречивость традиционных представлений об атеросклерозе")
    assert atero[0].journal == "Сибирский научный медицинский журнал" and atero[0].year == 2024
    q = ft.FulltextRequest(
        title="Противоречивость традиционных представлений об атеросклерозе и эффективности "
              "гиполипидемической терапии. Перспективные направления",
        authors=["Васильев А.П.", "Стрельцова Н.Н."], year=2024)
    best = src.best_match(q, atero)
    assert best is atero[0] and best.score == 1.0
    assert all(h.score == 0.0 for h in atero[1:])


def test_parse_cyberleninka_article_trims_neighbours():
    html = ext_bytes("cyberleninka_article_neurons.html")
    art = src.parse_cyberleninka_article(html, title_hint="Нейроны в нейронных сетях")
    assert art.url == "https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah"
    assert art.title == "НЕЙРОНЫ В НЕЙРОННЫХ СЕТЯХ"
    assert art.authors[-1] == "Архипов Александр Викторович" and len(art.authors) == 5
    assert art.year == 2023 and art.license == "CC BY"
    assert art.journal == "Известия Тульского государственного университета. Технические науки"
    assert art.keywords == ["НЕЙРОННЫЕ СЕТИ", "ИСКУССТВЕННЫЕ НЕЙРОННЫЕ СЕТИ", "НЕЙРОН"]
    assert art.pdf_url == "https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah/pdf"
    assert (art.cyberleninka_id, art.pages, art.page_range) == (19672049, 6, "438-443")
    assert art.extra["citation_issn"] == "2071-6168"
    assert art.paragraphs_total == 103 and art.trimmed_head == 6 and art.trimmed_tail == 10
    assert art.text.startswith("УДК 004.032.26\n\nDOI: 10.24412/2071-6168-2023-2-438-443\n\nНЕЙРОНЫ В НЕЙРОННЫХ СЕТЯХ")
    assert "trademarks" not in art.text  # end of the previous article
    assert "ОСОБЕННОСТИ РАДИОКАНАЛОВ" not in art.text  # start of the next one
    assert "Arkhipov Alexander Viktorovich, postgraduate" in art.text
    assert "[email" not in art.text and "@mail.ru" in art.text  # Cloudflare e-mails decoded
    assert chr(0xFEFF) not in art.text
    assert art.dois_in_text == ["10.24412/2071-6168-2023-2-438-443"]
    full = src.parse_cyberleninka_article(html, trim=False)
    assert full.trimmed_head == 0 and full.trimmed_tail == 0 and "trademarks" in full.text
    with pytest.raises(ParseError):
        src.parse_cyberleninka_article("<html><body><p>nothing</p></body></html>")


def test_cf_decode_email():
    key = 0x42
    encoded = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in "test@example.org")
    assert src.cf_decode_email(encoded) == "test@example.org"
    assert src.cf_decode_email("zz") == ""


def test_crossref_record():
    rec = src.crossref_record(ext_json("crossref_work_rmmar424450.json")["message"])
    assert rec.doi == "10.17816/rmmar424450"
    assert rec.titles[0] == "Idiopathic omentum hemorrhage as a cause of hemoperitoneum"
    assert rec.authors[0] == "Ryazanov, Vladimir V." and rec.year == 2023
    assert rec.journal == "Russian Military Medical Academy Reports"
    assert rec.landing_url == "https://journals.eco-vector.com/RMMArep/article/view/424450"
    assert rec.pdf_urls == ["https://journals.eco-vector.com/RMMArep/article/viewFile/424450/pdf"]
    assert rec.license == "https://creativecommons.org/licenses/by-nc-nd/4.0"
    ssmj = src.crossref_record(ext_json("crossref_work_ssmj20240404.json")["message"])
    assert ssmj.journal == "Сибирский научный медицинский журнал"
    assert ssmj.pdf_urls == ["https://sibmed.elpub.ru/jour/article/viewFile/1610/649"]


def test_crossref_search_and_match(net: ExternalHTTP, ext: ExtSite):
    ext.json("api.crossref.org/works?", "crossref_search_atero.json")
    items = src.crossref_search(net, title="Contradictions in traditional ideas about atherosclerosis",
                                author="Vasilev", year=2024, email="me@example.org")
    params = ext.requests[0].url.params
    assert params["query.author"] == "Vasilev" and params["mailto"] == "me@example.org"
    assert params["filter"] == "from-pub-date:2023,until-pub-date:2025"
    assert [i["DOI"] for i in items] == ["10.18699/ssmj20240404", "10.52420/umj.23.2.131", "10.58676/sjmas.v1i7.41"]
    q = ft.FulltextRequest(title="Противоречивость традиционных представлений ...",
                           title_en="Contradictions in traditional ideas about atherosclerosis and the efficacy "
                                    "of lipid-lowering therapy. Promising directions",
                           authors=["Васильев А.П.", "Стрельцова Н.Н."], year=2024)
    scores = [src.match_score(q, titles=r.titles, authors=r.authors, year=r.year)
              for r in map(src.crossref_record, items)]
    assert scores[0] == 1.0 and scores[1] == 0.0 and scores[2] == 0.0


def test_unpaywall_and_openalex_records():
    up = src.unpaywall_record(ext_json("unpaywall_rmmar424450.json"))
    assert up.pdf_urls[0] == "https://journals.eco-vector.com/RMMArep/article/download/424450/pdf"
    assert up.is_oa is True and up.license == "cc-by-nc-nd" and up.landing_url is None  # doi.org skipped
    assert up.authors[0] == "Vladimir V. Ryazanov" and up.year == 2023
    ox = src.openalex_record(ext_json("openalex_work_rmmar424450.json"))
    assert ox.id == "https://openalex.org/W4387698866" and ox.doi == "10.17816/rmmar424450"
    assert ox.pdf_urls == ["https://journals.eco-vector.com/RMMArep/article/download/424450/pdf"]
    assert ox.cyberleninka_url is None
    ssmj = src.openalex_record(ext_json("openalex_work_ssmj20240404.json"))
    assert ssmj.pdf_urls[0] == "https://sibmed.elpub.ru/jour/article/download/1610/649"
    assert ssmj.authors == ["A. P. Vasiliev", "N. N. Streltsova"]
    found = src.openalex_record(ext_json("openalex_search_atero.json")["results"][0])
    assert found.doi is None and found.pdf_urls == []
    assert found.cyberleninka_url == ("https://cyberleninka.ru/article/n/protivorechivost-traditsionnyh-predstavleniy-"
                                      "ob-ateroskleroze-i-effektivnosti-gipolipidemicheskoy-terapii-perspektivnye")
    assert found.authors == ["Васильев Александр Петрович", "Стрельцова Нина Николаевна"]


def test_api_calls_build_the_right_urls(net: ExternalHTTP, ext: ExtSite):
    ext.json("api.unpaywall.org/v2/10.17816/rmmar424450", "unpaywall_rmmar424450.json")
    ext.json("api.openalex.org/works/doi:10.17816/rmmar424450", "openalex_work_rmmar424450.json")
    ext.json("api.openalex.org/works?", "openalex_search_atero.json")
    ext.json("api.datacite.org/dois/10.24412", "datacite_doi_24412.json")
    ext.on("hdl.handle.net/api/handles/10.18699", body=json.dumps(
        {"responseCode": 1, "values": [{"type": "URL", "data": {"value": "https://sibmed.elpub.ru/jour/article/view/1610"}}]}
    ).encode())
    assert src.unpaywall_lookup(net, "10.17816/rmmar424450", "me@example.org")["oa_status"] == "hybrid"
    assert "email=me%40example.org" in ext.urls()[-1]
    assert src.unpaywall_lookup(net, "10.1/missing", "me@example.org") is None
    assert src.openalex_work(net, "10.17816/rmmar424450")["publication_year"] == 2023
    works = src.openalex_search(net, "Противоречивость, традиционных: представлений", year=2024,
                                cyberleninka_only=True)
    assert works[0]["id"] == "https://openalex.org/W7119241675"
    flt = ext.requests[-1].url.params["filter"]
    assert flt == ("title.search:Противоречивость традиционных представлений,publication_year:2023-2025,"
                   "locations.source.id:S4306401404")
    assert src.datacite_url(net, "10.24412/2837-0759-2026-4022036") == \
        "https://cyberleninka.ru/article/n/sphero-conical-vessels-in-works-of-fine-art"
    assert src.handle_url(net, "10.18699/SSMJ20240404") == "https://sibmed.elpub.ru/jour/article/view/1610"
    ext.on("api.crossref.org/works/10.24412", status=404, body=ext_bytes("crossref_404.txt"), ctype="text/plain")
    assert ext_bytes("crossref_404.txt").startswith(b"Resource not found.")
    assert src.crossref_work(net, "10.24412/2071-6168-2023-2-438-443") is None  # unregistered CyberLeninka DOI


def test_cyberleninka_search_request(net: ExternalHTTP, ext: ExtSite):
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json", method="POST")
    hits = src.cyberleninka_search(net, "Нейроны в нейронных сетях", size=10, year_from=2022, year_to=2024)
    assert len(hits) == 3
    req = ext.requests[0]
    assert req.method == "POST" and str(req.url) == "https://cyberleninka.ru/api/search"
    assert json.loads(req.content) == {"mode": "articles", "q": "Нейроны в нейронных сетях", "size": 10,
                                       "from": 0, "year_from": 2022, "year_to": 2024}
    assert req.headers["user-agent"].startswith("elibrary-cli/")


#: Title -> slug of CyberLeninka articles seen in public web-search results on 2026-10-13 (not fetched
#: from cyberleninka.ru). Several are cut at CL_SLUG_MAX = 131 characters; one is a numbered duplicate.
CL_PUBLIC_SLUGS = [
    ("ХЕМОРЕАКТОМНЫЙ СКРИНИНГ ВОЗДЕЙСТВИЯ ФАРМАКОЛОГИЧЕСКИХ ПРЕПАРАТОВ НА SARS-COV-2 И ВИРОМ ЧЕЛОВЕКА КАК "
     "ИНФОРМАЦИОННАЯ ОСНОВА ДЛЯ ПРИНЯТИЯ РЕШЕНИЙ ПО ФАРМАКОТЕРАПИИ COVID-19",
     "hemoreaktomnyy-skrining-vozdeystviya-farmakologicheskih-preparatov-na-sars-cov-2-i-virom-cheloveka-kak-"
     "informatsionnaya-osnova-dlya"),
    ("САХАРНЫЙ ДИАБЕТ В РОССИЙСКОЙ ФЕДЕРАЦИИ: ДИНАМИКА ЭПИДЕМИОЛОГИЧЕСКИХ ПОКАЗАТЕЛЕЙ ПО ДАННЫМ ФЕДЕРАЛЬНОГО "
     "РЕГИСТРА САХАРНОГО ДИАБЕТА ЗА ПЕРИОД 2010–2022 ГГ.",
     "saharnyy-diabet-v-rossiyskoy-federatsii-dinamika-epidemiologicheskih-pokazateley-po-dannym-federalnogo-"
     "registra-saharnogo-diabeta"),
    ("ОСОБЕННОСТИ РЕВАСКУЛЯРИЗАЦИИ МИОКАРДА И АНАЛИЗ КОНЕЧНЫХ ТОЧЕК ЧЕРЕЗ ГОД НАБЛЮДЕНИЯ У БОЛЬНЫХ С ОСТРЫМИ И "
     "ХРОНИЧЕСКИМИ ФОРМАМИ ИШЕМИЧЕСКОЙ БОЛЕЗНИ СЕРДЦА В ЗАВИСИМОСТИ ОТ НАЛИЧИЯ ИЛИ ОТСУТСТВИЯ САХАРНОГО ДИАБЕТА",
     "osobennosti-revaskulyarizatsii-miokarda-i-analiz-konechnyh-tochek-cherez-god-nablyudeniya-u-bolnyh-s-"
     "ostrymi-i-hronicheskimi"),
    ("Действия производителей растительных и других широко востребованных лекарственных препаратов в рамках "
     "регистрационных процедур по праву ЕАЭС: практические рекомендации",
     "deystviya-proizvoditeley-rastitelnyh-i-drugih-shiroko-vostrebovannyh-lekarstvennyh-preparatov-v-ramkah-"
     "registratsionnyh-protsedur"),
    ("План управления рисками: экспертная оценка представления в зависимости от типа лекарственного препарата "
     "при регистрации в ЕАЭС (обзор)",
     "plan-upravleniya-riskami-ekspertnaya-otsenka-predstavleniya-v-zavisimosti-ot-tipa-lekarstvennogo-"
     "preparata-pri-registratsii-v-eaes"),
    ("Государственная фармакопея Российской Федерации: стандартизация лекарственных средств в условиях "
     "формирования единого фармацевтического рынка ЕАЭС",
     "gosudarstvennaya-farmakopeya-rossiyskoy-federatsii-standartizatsiya-lekarstvennyh-sredstv-v-usloviyah-"
     "formirovaniya-edinogo"),
    ("ПРОВЕДЕНИЕ ФАРМАЦЕВТИЧЕСКИХ ИНСПЕКЦИЙ НА СООТВЕТСТВИЕ ТРЕБОВАНИЯМ GCP ЕАЭС В РОССИЙСКОЙ ФЕДЕРАЦИИ "
     "ЯВЛЯЕТСЯ ОСОЗНАННОЙ НЕОБХОДИМОСТЬЮ",
     "provedenie-farmatsevticheskih-inspektsiy-na-sootvetstvie-trebovaniyam-gcp-eaes-v-rossiyskoy-federatsii-"
     "yavlyaetsya-osoznannoy"),
    ("Типовые инструкции по медицинскому применению взаимозаменяемых лекарственных препаратов: анализ "
     "современных документов, регулирующих их подготовку",
     "tipovye-instruktsii-po-meditsinskomu-primeneniyu-vzaimozamenyaemyh-lekarstvennyh-preparatov-analiz-"
     "sovremennyh-dokumentov"),
    ("ОТВЕТСТВЕННОСТЬ ЗАКАЗЧИКА И ПОСТАВЩИКА (ПОДРЯДЧИКА, ИСПОЛНИТЕЛЯ) ЗА НЕИСПОЛНЕНИЕ ИЛИ НЕНАДЛЕЖАЩЕЕ "
     "ИСПОЛНЕНИЕ ОБЯЗАТЕЛЬСТВ ПО КОНТРАКТУ",
     "otvetstvennost-zakazchika-i-postavschika-podryadchika-ispolnitelya-za-neispolnenie-ili-nenadlezhaschee-"
     "ispolnenie-obyazatelstv-po"),
    ('ОСОБЕННОСТИ ЗАКЛЮЧЕНИЯ ДОГОВОРОВ В РАМКАХ ФЕДЕРАЛЬНОГО ЗАКОНА "О КОНТРАКТНОЙ СИСТЕМЕ В СФЕРЕ ЗАКУПОК '
     'ТОВАРОВ, РАБОТ, УСЛУГ ДЛЯ ОБЕСПЕЧЕНИЯ ГОСУДАРСТВЕННЫХ И МУНИЦИПАЛЬНЫХ НУЖД"',
     "osobennosti-zaklyucheniya-dogovorov-v-ramkah-federalnogo-zakona-o-kontraktnoy-sisteme-v-sfere-zakupok-"
     "tovarov-rabot-uslug-dlya"),
    ("Практика Верховного Суда Российской Федерации в сфере публичных закупок: на пути к формированию единых "
     "подходов к применению законодательства о контрактной системе",
     "praktika-verhovnogo-suda-rossiyskoy-federatsii-v-sfere-publichnyh-zakupok-na-puti-k-formirovaniyu-"
     "edinyh-podhodov-k-primeneniyu"),
    ("Обновление клинических рекомендаций по хронической сердечной недостаточности. Современные принципы "
     "диагностики, формулировки диагноза и тактики лечения",
     "obnovlenie-klinicheskih-rekomendatsiy-po-hronicheskoy-serdechnoy-nedostatochnosti-sovremennye-printsipy-"
     "diagnostiki-formulirovki"),
    ("ЭМПАГЛИФЛОЗИН: ПУТЬ ОТ КОНТРОЛЯ ГЛИКЕМИИ К СНИЖЕНИЮ СЕРДЕЧНО-СОСУДИСТОЙ СМЕРТНОСТИ И ГОСПИТАЛИЗАЦИЙ ПО "
     "ПРИЧИНЕ СЕРДЕЧНОЙ НЕДОСТАТОЧНОСТИ",
     "empagliflozin-put-ot-kontrolya-glikemii-k-snizheniyu-serdechno-sosudistoy-smertnosti-i-gospitalizatsiy-po-"
     "prichine-serdechnoy"),
    ("Пандемия XXI века: хроническая сердечная недостаточность бремя современного общества. Эпидемиологические "
     "аспекты (обзор литературы)",
     "pandemiya-xxi-veka-hronicheskaya-serdechnaya-nedostatochnost-bremya-sovremennogo-obschestva-"
     "epidemiologicheskie-aspekty-obzor"),
    ("Новые данные о кардиопротекторных эффектах дапаглифлозина у больных с сахарным диабетом 2-го типа и "
     "сердечной недостаточностью",
     "novye-dannye-o-kardioprotektornyh-effektah-dapagliflozina-u-bolnyh-s-saharnym-diabetom-2-go-tipa-i-"
     "serdechnoy-nedostatochnostyu"),
    ("Фазово-структурное состояние и механические свойства стали 60С2ХФА в состоянии Q-n-P-T обработки",
     "fazovo-strukturnoe-sostoyanie-i-mehanicheskie-svoystva-stali-60s2hfa-v-sostoyanii-q-n-p-t-obrabotki"),
    ("АНАЛИЗ СТРУКТУРЫ И СВОЙСТВ СВАРНЫХ СОЕДИНЕНИЙ ИЗ СТАЛИ 10ХСНД ПРИ СВАРКЕ В УСЛОВИЯХ ОТРИЦАТЕЛЬНЫХ "
     "ТЕМПЕРАТУР",
     "analiz-struktury-i-svoystv-svarnyh-soedineniy-iz-stali-10hsnd-pri-svarke-v-usloviyah-otritsatelnyh-"
     "temperatur"),
    ("Исследование свариваемости высокопрочной стали твердостью 500 HBW",
     "issledovanie-svarivaemosti-vysokoprochnoy-stali-tverdostyu-500-hbw"),
    ("МАЛЬЧИКИ И ДЕВОЧКИ УЧАТСЯ ПО-РАЗНОМУ: УЧЁТ ПОЛОВЫХ РАЗЛИЧИЙ В ОБРАЗОВАТЕЛЬНОМ ПРОЦЕССЕ",
     "malchiki-i-devochki-uchatsya-po-raznomu-uchyot-polovyh-razlichiy-v-obrazovatelnom-protsesse"),
    ("ПОНЯТИЕ «ЗДОРОВЫЙ ОБРАЗ ЖИЗНИ» И ЕГО ФОРМИРОВАНИЕ У СТУДЕНЧЕСКОЙ МОЛОДЁЖИ СРЕДСТВАМИ ФИЗИЧЕСКОЙ КУЛЬТУРЫ",
     "ponyatie-zdorovyy-obraz-zhizni-i-ego-formirovanie-u-studencheskoy-molodyozhi-sredstvami-fizicheskoy-"
     "kultury"),
    ('Критика концепции "гражданское общество" в отечественном обществознании',
     "kritika-kontseptsii-grazhdanskoe-obschestvo-v-otechestvennom-obschestvoznanii"),
    ("СОВРЕМЕННЫЙ ПОДХОД К ПОНИМАНИЮ ОБЪЕКТА И ПРЕДМЕТА СОЦИАЛЬНОЙ ПСИХОЛОГИИ",
     "sovremennyy-podhod-k-ponimaniyu-obekta-i-predmeta-sotsialnoy-psihologii"),
    ("Единство микрокосма и макрокосма. Новый космологический подход к решению старого мировоззренческого "
     "вопроса",
     "edinstvo-mikrokosma-i-makrokosma-novyy-kosmologicheskiy-podhod-k-resheniyu-starogo-mirovozzrencheskogo-"
     "voprosa"),
    ("НЕЙРОКОГНИТИВНЫЕ НАРУШЕНИЯ У ПАЦИЕНТОВ, ПЕРЕНЕСШИХ COVID-19: ПАТОГЕНЕЗ И НАПРАВЛЕНИЯ РЕАБИЛИТАЦИИ",
     "neyrokognitivnye-narusheniya-u-patsientov-perenesshih-covid-19-patogenez-i-napravleniya-reabilitatsii"),
    ("ИНДУСТРИЯ 4.0 - ПОЛОЖИТЕЛЬНЫЕ И НЕГАТИВНЫЕ СТОРОНЫ РАЗВИТИЯ",
     "industriya-4-0-polozhitelnye-i-negativnye-storony-razvitiya"),
    ("Ключевые технологии Индустрии 4. 0, общества 5. 0, экономики 3. 0",
     "klyuchevye-tehnologii-industrii-4-0-obschestva-5-0-ekonomiki-3-0"),
    ("АКТУАЛЬНЫЕ ПРОБЛЕМЫ МЕХАНИЗМА ЗАКУПОК ПО 44-ФЗ И 223-ФЗ", "aktualnye-problemy-mehanizma-zakupok-po-44-fz-i-223-fz"),
    ("ОЦЕНКА ВАЖНОСТИ И САМООЦЕНКА РАЗВИТИЯ ЦИФРОВЫХ КОМПЕТЕНЦИЙ БУДУЩИХ HR-МЕНЕДЖЕРОВ",
     "otsenka-vazhnosti-i-samootsenka-razvitiya-tsifrovyh-kompetentsiy-buduschih-hr-menedzherov"),
    ("Дальтон-план Елены Паркхерст и другие зарубежные системы обучения",
     "dalton-plan-eleny-parkherst-i-drugie-zarubezhnye-sistemy-obucheniya"),
    ("Перспективные высокопрочные коррозионностойкие стали, легированные азотом (сравнительный анализ)",
     "perspektivnye-vysokoprochnye-korrozionnostoykie-stali-legirovannye-azotom-sravnitelnyy-analiz"),
    ("Новый институциональный подход: построение исследовательской схемы",
     "novyy-institutsionalnyy-podhod-postroenie-issledovatelskoy-shemy-1"),
]


def _assert_slug(title: str, link: str) -> None:
    """``link`` (``/article/n/<slug>`` or a full URL) is the derived slug, possibly with a ``-N`` suffix."""
    got = src.cyberleninka_slug(title)
    slug = link.rstrip("/").rsplit("/", 1)[-1]
    assert slug == got or re.fullmatch(re.escape(got) + r"-\d+", slug), (title, slug, got)


def test_cyberleninka_slug_matches_every_saved_link():
    pairs = 0
    for name in ("cyberleninka_search_neurons.json", "cyberleninka_search_atero.json"):
        for art in ext_json(name)["articles"]:
            _assert_slug(src.strip_tags(art["name"]), art["link"])
            pairs += 1
    assert pairs == 8
    for work in ext_json("openalex_search_atero.json")["results"]:
        _assert_slug(work["display_name"], src.openalex_record(work).cyberleninka_url)
    # the example of the saved elibrary card item_article_54076582 (its full text is on CyberLeninka)
    _assert_slug("Получение частиц диоксида титана (рутила) на поверхности восстановленного оксида графена в "
                 "сверхкритическом изопропаноле", "/article/n/poluchenie-chastits-dioksida-titana-rutila-na-"
                 "poverhnosti-vosstanovlennogo-oksida-grafena-v-sverhkriticheskom-izopropanole")
    assert src.cyberleninka_slug("НЕЙРОННЫЕ СЕТИ") == "neyronnye-seti"  # the saved link is the duplicate "-1"


@pytest.mark.parametrize(("title", "slug"), CL_PUBLIC_SLUGS)
def test_cyberleninka_slug_matches_public_links(title: str, slug: str):
    _assert_slug(title, slug)


def test_cyberleninka_slug_rules():
    slug = src.cyberleninka_slug
    assert slug("Гражданское общество: щит и ёж, объект, съезд") == "grazhdanskoe-obschestvo-schit-i-yozh-obekt-sezd"
    assert slug("Нейроны <b>в</b> SARS-CoV-2 (60С2ХФА)!") == "neyrony-v-sars-cov-2-60s2hfa"
    assert slug("Индустрия 4.0 — «новая» эра; № 5") == "industriya-4-0-novaya-era-5"
    assert slug("Café Ångström") == "cafe-angstrom"
    assert slug("") == "" and slug("…") == "" and src.cyberleninka_candidate_urls("—") == []
    long = slug("Слово " * 40)
    assert len(long) <= src.CL_SLUG_MAX and long.endswith("slovo") and long == "-".join(["slovo"] * 22)
    assert len(slug("а" * 200)) == src.CL_SLUG_MAX  # one endless word: hard cut
    assert src.cyberleninka_candidate_urls("Нейронные сети") == [
        "https://cyberleninka.ru/article/n/neyronnye-seti", "https://cyberleninka.ru/article/n/neyronnye-seti-1",
        "https://cyberleninka.ru/article/n/neyronnye-seti-2"]


def test_cyberleninka_page_url():
    page = "https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah"
    for url in (page, page + "/", page + "/pdf", page + ".pdf", page + "/viewer", page + "?utm=1#x",
                "http://www.cyberleninka.ru/article/n/Neyrony-v-neyronnyh-setyah"):
        assert src.cyberleninka_page_url(url) == page, url
    for url in ("https://cyberleninka.ru/api/search", "https://cyberleninka.ru/search?q=x",
                "https://cyberleninka.ru/article/v/abc", "https://cyberleninka.ru/journal/n/x",
                "https://example.org/article/n/x", None, ""):
        assert src.cyberleninka_page_url(url) is None, url


def test_parse_landing_meta():
    elpub = src.parse_landing_meta(ext_bytes("landing_elpub_1610.html"),
                                   base_url="https://sibmed.elpub.ru/jour/article/view/1610")
    assert elpub.pdf_urls == ["https://sibmed.elpub.ru/jour/article/download/1610/649"]
    assert elpub.doi == "10.18699/ssmj20240404" and elpub.language == "ru" and elpub.year == 2024
    assert elpub.fulltext_html_url is None  # it points to the landing page itself
    assert "Противоречивость традиционных представлений об атеросклерозе и эффективности гиполипидемической " \
           "терапии. Перспективные направления" in elpub.titles
    assert "А. П. Васильев" in elpub.authors
    eco = src.parse_landing_meta(ext_bytes("landing_ecovector_424450.html"),
                                 base_url="https://journals.eco-vector.com/RMMArep/article/view/424450")
    assert eco.pdf_urls == ["https://journals.eco-vector.com/RMMArep/article/download/424450/138215"]
    assert all("SuppFile" not in u for u in eco.pdf_urls)
    assert eco.doi == "10.17816/rmmar424450" and eco.year == 2023
    assert "Идиопатическое сальниковое кровоизлияние как причина гемоперитонеума" in eco.titles


def test_extract_html_text():
    body = "".join(f"<p>Абзац номер {i}: {'текст статьи ' * 20}</p>" for i in range(8))
    html = f"<html><body><nav><p>Меню сайта</p></nav><div itemprop='articleBody'><h2>Введение</h2>{body}</div></body></html>"
    text = src.extract_html_text(html)
    assert text.startswith("Введение\n\nАбзац номер 0:") and "Меню сайта" not in text
    assert src.extract_html_text("<html><body><p>коротко</p></body></html>") == ""


# =============================================================================== elibrary.py
SIGNED_IN_JS = """function load_article(id){
	document.forms["results"].action="full_text.asp?id="+id;
	document.forms["results"].target="_blank";
	document.forms["results"].submit()
}"""


def signed_in(html: bytes) -> bytes:
    """The anonymous fixture as a signed-in reader would see it (session box + load_article body as on a
    page saved by a signed-in user in 2025)."""
    text = html.decode("utf-8")
    anonymous = "<font color=#00008f>Незарегистрированный пользователь</font>"
    assert anonymous in text
    text = text.replace(anonymous, '<font color=#00008f><a href="/author_info.asp?isold=1">reader&nbsp;</a></font>')
    text = re.sub(r"function load_article\(iid\)\{.*?\n\}", lambda m: SIGNED_IN_JS, text, count=1, flags=re.S)
    return text.encode("utf-8")


def test_login_state_detection():
    anon = fixture_bytes("item_patent_47430099")
    assert el.is_logged_in(anon) is False and el.session_user(anon) is None
    assert el.is_logged_in(fixture_bytes("item_article_46371463")) is False
    page = signed_in(anon)
    assert el.is_logged_in(page) is True and el.session_user(page) == "reader"
    assert el.is_logged_in(fixture_bytes("ispdf_47430099")) is False  # fragment without a session box


def test_find_elibrary_pdf_links():
    assert el.find_elibrary_pdf_link(fixture_bytes("ispdf_47430099")) == "https://elibrary.ru/full_text.asp?id=47430099"
    patent = fixture_bytes("item_patent_47430099")
    # the anonymous function definitions (load_article(iid)) are not calls
    assert el.find_elibrary_pdf_links(patent, item_id=47430099) == [
        "https://elibrary.ru/file_article.asp?fileid=47430099&filenum=1"]
    assert el.find_elibrary_pdf_link(fixture_bytes("item_article_46371463")) is None
    html = (b'<a href="javascript:load_article(111)">x</a><a href="javascript:load_article(222)">y</a>'
            b'<a href="/download/elibrary_222_10974709.pdf">PDF</a>'
            b'<script>window.open("https://elibrary.ru/download/elibrary_222_1.pdf")</script>'
            b'<a href="https://publisher.example/file.pdf">publisher</a>')
    assert el.find_elibrary_pdf_links(html, item_id=222) == [
        "https://elibrary.ru/download/elibrary_222_10974709.pdf",
        "https://elibrary.ru/download/elibrary_222_1.pdf",
        "https://elibrary.ru/full_text.asp?id=222",
        "https://elibrary.ru/full_text.asp?id=111",
    ]
    refresh = b'<meta http-equiv="refresh" content="0; url=/download/elibrary_5_6.pdf">'
    assert el.find_elibrary_pdf_link(refresh) == "https://elibrary.ru/download/elibrary_5_6.pdf"


def test_fulltext_status():
    paid = el.fulltext_status(fixture_bytes("item_article_46371463"))
    assert paid["paid"] is True and paid["price_rub"] == 455 and paid["available"] is False
    patent = el.fulltext_status(fixture_bytes("item_patent_47430099"))
    assert patent["available"] is True and patent["paid"] is False


def _pdf_response(body: bytes):
    return lambda r: httpx2.Response(200, content=body, headers={"Content-Type": "application/pdf"})


def test_download_elibrary_pdf_requires_login(http, site, tmp_path: Path):
    with pytest.raises(LoginRequired) as info:
        el.download_elibrary_pdf(http, 47430099, fixture_bytes("item_patent_47430099"), tmp_path)
    assert "elibrary auth login" in info.value.message and "elibrary auth cookies" in info.value.message
    assert site.requests == []


def test_download_elibrary_pdf_signed_in(http, site, tmp_path: Path):
    pdf_bytes = article_pdf()
    site.add(lambda r: "full_text.asp?id=47430099" in str(r.url), _pdf_response(pdf_bytes))
    page = signed_in(fixture_bytes("item_patent_47430099"))
    path = el.download_elibrary_pdf(http, 47430099, page, tmp_path)
    assert path == tmp_path / "elibrary_47430099.pdf" and path.read_bytes() == pdf_bytes
    # file_article.asp (undocumented) is tried first, 404 -> the documented full_text.asp
    assert [str(r.url) for r in site.requests] == [
        "https://elibrary.ru/file_article.asp?fileid=47430099&filenum=1",
        "https://elibrary.ru/full_text.asp?id=47430099",
    ]
    assert site.requests[-1].headers["referer"] == "https://elibrary.ru/item.asp?id=47430099"
    # a valid file on disk is reused without a request
    assert el.download_elibrary_pdf(http, 47430099, page, tmp_path) == path
    assert len(site.requests) == 2


def test_download_elibrary_pdf_follows_download_link(http, site, tmp_path: Path):
    pdf_bytes = article_pdf()
    inner = b'<html><body><a href="/download/elibrary_47430099_12345678.pdf">PDF</a></body></html>'
    site.add(lambda r: "download/elibrary_47430099_12345678.pdf" in str(r.url), _pdf_response(pdf_bytes))
    site.add(lambda r: "full_text.asp" in str(r.url),
             lambda r: httpx2.Response(200, content=inner, headers={"Content-Type": "text/html; charset=utf-8"}))
    page = fixture_bytes("ispdf_47430099") + signed_in(fixture_bytes("item_patent_47430099"))
    path = el.download_elibrary_pdf(http, 47430099, page, tmp_path)
    assert path.read_bytes() == pdf_bytes
    assert str(site.requests[-1].url) == "https://elibrary.ru/download/elibrary_47430099_12345678.pdf"


def test_download_elibrary_pdf_login_page_and_paid(http, site, tmp_path: Path):
    must_login = "<html><body>Для доступа к полному тексту необходимо вначале войти в библиотеку!</body></html>"
    site.add(lambda r: "full_text.asp" in str(r.url),
             lambda r: httpx2.Response(200, content=must_login.encode(), headers={"Content-Type": "text/html"}))
    page = fixture_bytes("ispdf_47430099") + signed_in(fixture_bytes("item_patent_47430099"))
    with pytest.raises(LoginRequired):
        el.download_elibrary_pdf(http, 47430099, page, tmp_path)
    paid = signed_in_article = fixture_bytes("item_article_46371463").replace(
        "Незарегистрированный пользователь".encode(), b"reader")
    assert el.is_logged_in(signed_in_article)
    with pytest.raises(NotFound) as info:
        el.download_elibrary_pdf(http, 46371463, paid, tmp_path)
    assert "455 руб." in info.value.message


# =============================================================================== orchestrator
def test_slice_text_boundaries():
    paras = [f"Абзац {i}. " + "Предложение номер один. Предложение номер два. " * 5 for i in range(20)]
    text = "\n\n".join(paras)
    chunks, offset = [], 0
    while offset is not None:
        chunk, offset = ft.slice_text(text, offset, 1000)
        assert len(chunk) <= 1000
        chunks.append(chunk)
    assert "".join(chunks) == text
    assert all(c.endswith("\n\n") for c in chunks[:-1])
    one_para = "Первое предложение. " * 100
    chunk, nxt = ft.slice_text(one_para, 0, 333)
    assert chunk.endswith("предложение. ") and nxt == len(chunk)
    words = "слово " * 100
    chunk, _ = ft.slice_text(words, 0, 50)
    assert chunk.endswith(" ") and len(chunk) <= 50
    assert ft.slice_text("abc", 10, 5) == ("", None)
    with pytest.raises(ValueError):
        ft.slice_text("abc", 0, 0)


def test_request_normalisation_and_keys():
    req = ft.FulltextRequest(elibrary_id=47430099, edn=" fyoakl ", doi="https://doi.org/10.17816/RMMAR424450")
    assert req.doi == "10.17816/rmmar424450" and req.edn == "FYOAKL"
    keys = req.cache_keys()
    assert keys[0] == "elibrary-47430099" and keys[1] == "edn-FYOAKL" and keys[2].startswith("doi-")
    only_title = ft.FulltextRequest(title="Нейроны в нейронных сетях", year=2023)
    assert only_title.cache_keys()[0].startswith("title-")
    assert ft.FulltextRequest(title="НЕЙРОНЫ В НЕЙРОННЫХ СЕТЯХ!", year=2023).cache_keys() == only_title.cache_keys()
    pub = {"id": 46371463, "doi": "10.1000/X", "title": "Статья", "authors": [{"name": "Петров В.А."}, "Иванов И.И."],
           "year": "2021", "journal": {"title": "Вестник"}, "english": {"title": "Article"}, "unknown": 1}
    r = ft.FulltextRequest.from_publication(pub)
    assert (r.elibrary_id, r.doi, r.title, r.title_en, r.year, r.journal) == (46371463, "10.1000/x", "Статья", "Article",
                                                                              2021, "Вестник")
    assert r.authors == ["Петров В.А.", "Иванов И.И."]
    assert ft.FulltextRequest.from_publication(r).elibrary_id == 46371463


def test_sources_validation(monkeypatch, settings: Settings):
    with pytest.raises(ElibraryError):
        ft.get_fulltext({"title": "x"}, settings=settings, sources=["scihub"])
    monkeypatch.setenv("ELIBRARY_FULLTEXT_SOURCES", "openalex, crossref,openalex")
    assert ft.default_sources(settings) == ("openalex", "crossref")
    monkeypatch.delenv("ELIBRARY_FULLTEXT_SOURCES")
    assert ft.default_sources(settings) == ft.SOURCE_NAMES
    with pytest.raises(ElibraryError):
        ft.get_fulltext({}, settings=settings, sources=["crossref"])


NEURONS_REQ = {"elibrary_id": 50633001, "title": "Нейроны в нейронных сетях", "authors": ["Панарин В.М."],
               "year": 2023}


def test_get_fulltext_from_cyberleninka_with_paging(settings: Settings, net: ExternalHTTP, ext: ExtSite,
                                                    http, site):
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json", method="POST")
    ext.html("cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah", ext_bytes("cyberleninka_article_neurons.html"))
    res = ft.get_fulltext(NEURONS_REQ, http=http, net=net, settings=settings, max_chars=5000)
    assert res.source == "cyberleninka" and res.format == "html" and res.engine == "html"
    assert res.source_url == "https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah"
    assert res.match_score == 1.0 and res.license == "CC BY" and res.page_count == 6
    assert res.total_chars == 13315 and res.truncated and res.offset == 0
    assert res.text.startswith("УДК 004.032.26") and len(res.text) <= 5000
    assert res.next_offset == len(res.text) and not res.from_cache
    statuses = [(a.source, a.status) for a in res.attempts]
    assert statuses == [("elibrary", "skipped"), ("cyberleninka", "ok")]
    assert "вход в eLIBRARY.RU не выполнен" in res.attempts[0].detail
    assert site.requests == []  # anonymous elibrary session: no request spent
    # the page URL is derived from the title: no search API (robots.txt disallows /api/ for bots)
    assert [(r.method, str(r.url)) for r in ext.requests] == [
        ("GET", "https://cyberleninka.ru/robots.txt"),
        ("GET", "https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah")]
    sent = len(ext.requests)
    # the text is cached on disk: paging does not touch the network
    nxt = ft.get_fulltext(NEURONS_REQ, http=http, net=net, settings=settings, offset=res.next_offset,
                          max_chars=5000)
    assert nxt.from_cache and nxt.offset == res.next_offset and nxt.source == "cyberleninka"
    assert nxt.attempts[0].source == "cache" and ("cyberleninka", "ok") in [(a.source, a.status) for a in nxt.attempts]
    assert len(ext.requests) == sent
    whole = res.text + nxt.text
    while nxt.next_offset is not None:
        nxt = ft.get_fulltext(NEURONS_REQ, settings=settings, offset=nxt.next_offset, max_chars=5000)
        whole += nxt.text
    assert len(whole) == 13315 and not nxt.truncated
    cache_dir = ft.fulltext_cache_dir(settings)
    assert (cache_dir / "elibrary-50633001.txt").read_text(encoding="utf-8") == whole
    meta = json.loads((cache_dir / "elibrary-50633001.json").read_text(encoding="utf-8"))
    assert meta["source"] == "cyberleninka" and meta["text_file"] == "elibrary-50633001.txt"


def test_get_fulltext_rejects_cyberleninka_duplicate_title(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json", method="POST")
    req = {"title": "Нейронные сети", "authors": ["Сидоров С.С."], "year": 2020}
    with pytest.raises(NotFound) as info:
        ft.get_fulltext(req, net=net, settings=settings, sources=["cyberleninka"])
    assert info.value.attempts == [{"source": "cyberleninka", "status": "no_match",
                                    "detail": "на КиберЛенинке нет статьи с таким названием и авторами", "url": None}]
    assert "Полный текст не найден" in info.value.message


RMMAR_REQ = {"doi": "10.17816/rmmar424450",
             "title": "Идиопатическое сальниковое кровоизлияние как причина гемоперитонеума",
             "title_en": "Idiopathic omentum hemorrhage as a cause of hemoperitoneum",
             "authors": ["Рязанов В.В.", "Садыкова Г.К."], "year": 2023}


def test_get_fulltext_from_unpaywall_pdf(settings: Settings, ext: ExtSite, fake_time: FakeTime, tmp_path: Path):
    settings.contact_email = "reader@example.org"
    ext.json("api.unpaywall.org/v2/10.17816/rmmar424450", "unpaywall_rmmar424450.json")
    pdf_bytes = article_pdf()
    ext.pdf("journals.eco-vector.com/RMMArep/article/download/424450/pdf", pdf_bytes)
    client = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), sleep=fake_time.sleep, clock=fake_time.clock)
    res = ft.get_fulltext(RMMAR_REQ, net=client, settings=settings, sources=["unpaywall"], download_dir=tmp_path)
    client.close()
    assert res.source == "unpaywall" and res.format == "pdf" and res.engine == "pypdf"
    assert res.source_url == "https://journals.eco-vector.com/RMMArep/article/download/424450/pdf"
    assert res.match_score == 1.0 and res.page_count == 3 and res.license == "cc-by-nc-nd"
    assert res.doi == "10.17816/rmmar424450" and not res.truncated
    assert res.pdf_path and Path(res.pdf_path).parent == tmp_path and Path(res.pdf_path).read_bytes() == pdf_bytes
    assert res.text.startswith("Идиопатическое сальниковое кровоизлияние")
    assert ext.requests[0].url.params["email"] == "reader@example.org"
    assert ext.requests[0].headers["user-agent"].endswith("mailto:reader@example.org)")


def test_get_fulltext_cascade_rejects_bad_candidates(settings: Settings, net: ExternalHTTP, ext: ExtSite,
                                                     tmp_path: Path):
    """OpenAlex PDF link gives an HTML stub, the Crossref text-mining PDF is another document, the
    publisher landing page (Crossref resource) has the right PDF in citation_pdf_url."""
    ext.json("api.openalex.org/works/doi:10.17816/rmmar424450", "openalex_work_rmmar424450.json")
    ext.html("article/download/424450/pdf", b"<html><body>" + "Войдите, чтобы читать статью. ".encode() * 50 + b"</body></html>")
    ext.json("api.crossref.org/works/10.17816/rmmar424450", "crossref_work_rmmar424450.json")
    ext.pdf("article/viewFile/424450/pdf", article_pdf(title_line="Совсем другой документ о погоде на завтра"))
    ext.html("RMMArep/article/view/424450", ext_bytes("landing_ecovector_424450.html"))
    good = article_pdf()
    ext.pdf("article/download/424450/138215", good)
    res = ft.get_fulltext(RMMAR_REQ, net=net, settings=settings, sources=["openalex", "crossref", "publisher"],
                          download_dir=tmp_path)
    assert res.source == "publisher" and res.format == "pdf"
    assert res.source_url == "https://journals.eco-vector.com/RMMArep/article/download/424450/138215"
    assert Path(res.pdf_path).read_bytes() == good
    steps = [(a.source, a.status) for a in res.attempts]
    assert steps == [("openalex", "rejected"), ("crossref", "rejected"), ("publisher", "ok")]
    assert "не PDF" in res.attempts[0].detail
    assert "нет названия публикации" in res.attempts[1].detail
    assert not any(p.name.startswith("crossref_") for p in tmp_path.iterdir())  # rejected PDF removed
    landing_req = next(r for r in ext.requests if "article/download/424450/138215" in str(r.url))
    assert landing_req.headers["referer"] == "https://journals.eco-vector.com/RMMArep/article/view/424450"


def test_get_fulltext_rejects_a_pdf_bomb_and_tries_the_next_candidate(settings: Settings, net: ExternalHTTP,
                                                                      ext: ExtSite, tmp_path: Path, monkeypatch,
                                                                      small_pdf_limits):
    """A downloaded PDF is read with the default page limit; a decompression bomb is rejected (not
    inflated by a fallback engine) and the search goes on with the next candidate."""
    seen: list[int | None] = []
    real = ft.extract_pdf_text

    def spy(source, **kwargs):
        seen.append(kwargs.get("max_pages"))
        return real(source, **kwargs)

    monkeypatch.setattr(ft, "extract_pdf_text", spy)
    calls = _spy_pdfminer(monkeypatch)
    ext.json("api.crossref.org/works/10.17816/rmmar424450", "crossref_work_rmmar424450.json")
    ext.pdf("article/viewFile/424450/pdf", bomb_pdf(3 * MIB))
    ext.html("RMMArep/article/view/424450", ext_bytes("landing_ecovector_424450.html"))
    ext.pdf("article/download/424450/138215", article_pdf())
    res = ft.get_fulltext(RMMAR_REQ, net=net, settings=settings, sources=["crossref", "publisher"],
                          download_dir=tmp_path)
    assert res.source == "publisher" and res.text.startswith("Идиопатическое")
    bomb = next(a for a in res.attempts if a.url and "viewFile" in a.url)
    assert bomb.source == "crossref" and bomb.status == "rejected" and "PDF-бомба" in bomb.detail
    assert seen and set(seen) == {pdfmod.DEFAULT_MAX_PAGES} and pdfmod.DEFAULT_MAX_PAGES == 300
    assert calls == []
    assert not any(p.name.startswith("crossref_") for p in tmp_path.iterdir())  # rejected PDF removed


def test_get_fulltext_discovers_doi_by_crossref_search(settings: Settings, net: ExternalHTTP, ext: ExtSite,
                                                       tmp_path: Path):
    title = ("Противоречивость традиционных представлений об атеросклерозе и эффективности "
             "гиполипидемической терапии. Перспективные направления")
    req = {"title": title, "title_en": "Contradictions in traditional ideas about atherosclerosis and the "
                                       "efficacy of lipid-lowering therapy. Promising directions",
           "authors": ["Васильев А.П.", "Стрельцова Н.Н."], "year": 2024}
    ext.json("api.crossref.org/works?", "crossref_search_atero.json")
    ext.json("api.crossref.org/works/10.18699/ssmj20240404", "crossref_work_ssmj20240404.json")
    words = title.split()
    pdf_bytes = make_pdf([["УДК 612.397.81", " ".join(words[:6]), " ".join(words[6:]), "",
                           "А.П. Васильев, Н.Н. Стрельцова", ""] + ARTICLE_LINES[4:] * 3])
    ext.pdf("sibmed.elpub.ru/jour/article/viewFile/1610/649", pdf_bytes)
    res = ft.get_fulltext(req, net=net, settings=settings, sources=["crossref"], download_dir=tmp_path)
    assert res.source == "crossref" and res.doi == "10.18699/ssmj20240404"
    assert res.match_score == 1.0 and res.source_url == "https://sibmed.elpub.ru/jour/article/viewFile/1610/649"
    assert ("crossref", "info") in [(a.source, a.status) for a in res.attempts]
    assert "DOI найден по названию: 10.18699/ssmj20240404" in res.attempts[0].detail
    search = ext.requests[0]
    assert search.url.params["query.bibliographic"].startswith("Contradictions in traditional ideas")
    assert search.url.params["query.author"] == "vasilev"


ATERO_REQ = {"title": "Противоречивость традиционных представлений об атеросклерозе и эффективности "
                      "гиполипидемической терапии. Перспективные направления",
             "authors": ["Васильев А.П.", "Стрельцова Н.Н."], "year": 2024}
ATERO_CL = ("https://cyberleninka.ru/article/n/protivorechivost-traditsionnyh-predstavleniy-ob-ateroskleroze-"
            "i-effektivnosti-gipolipidemicheskoy-terapii-perspektivnye")


def _cl_page(title: str, authors: list[str], year: int, paragraphs: list[str]) -> bytes:
    metas = "".join(f'<meta name="citation_author" content="{a}" />' for a in authors)
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (f'<html><head><meta name="citation_title" content="{title}" />{metas}'
            f'<meta name="citation_publication_date" content="{year}" /></head><body>'
            f'<div class="ocr" itemprop="articleBody"><div class="ocr-banner"></div>{body}</div></body></html>').encode()


def test_openalex_title_search_leads_to_cyberleninka_copy(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("api.openalex.org/works?", "openalex_search_atero.json")
    paragraphs = ["УДК 612.397.81", ATERO_REQ["title"], "А.П. Васильев, Н.Н. Стрельцова",
                  *(f"Абзац {i}: холестерин липопротеидов низкой плотности и атерогенез. " * 4 for i in range(6))]
    ext.html(ATERO_CL, _cl_page(ATERO_REQ["title"], ["Васильев Александр Петрович", "Стрельцова Нина Николаевна"],
                                2024, paragraphs))
    res = ft.get_fulltext(ATERO_REQ, net=net, settings=settings, sources=["openalex", "cyberleninka"])
    assert res.source == "cyberleninka" and res.source_url == ATERO_CL and res.format == "html"
    assert res.match_score == 1.0 and res.text.startswith("УДК 612.397.81")
    steps = [(a.source, a.status) for a in res.attempts]
    assert steps == [("openalex", "info"), ("openalex", "ok")]
    assert "копия статьи найдена на КиберЛенинке" in res.attempts[0].detail
    flt = ext.requests[0].url.params["filter"]
    assert flt.startswith("title.search:Противоречивость традиционных") and flt.endswith("publication_year:2023-2025")


def test_cyberleninka_page_of_another_article_is_rejected(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("api.openalex.org/works?", "openalex_search_atero.json")
    # wrong page behind the link (exact URL: <slug>-1 and <slug>-2 do not exist)
    ext.exact(ATERO_CL, ext_bytes("cyberleninka_article_neurons.html"))
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_atero.json", method="POST")
    with pytest.raises(NotFound) as info:
        ft.get_fulltext(ATERO_REQ, net=net, settings=settings, sources=["openalex", "cyberleninka"])
    rejected = [a for a in info.value.attempts if a["status"] == "rejected"]
    assert len(rejected) == 1 and rejected[0]["url"] == ATERO_CL
    assert "другую статью («НЕЙРОНЫ В НЕЙРОННЫХ СЕТЯХ»)" in rejected[0]["detail"]
    # the same URL found again by the CyberLeninka search is not fetched twice
    assert sum(1 for u in ext.urls() if u == ATERO_CL) == 1
    assert not any("/api/" in u for u in ext.urls())


def test_cyberleninka_copy_not_used_when_source_excluded(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("api.openalex.org/works?", "openalex_search_atero.json")
    with pytest.raises(NotFound) as info:
        ft.get_fulltext(ATERO_REQ, net=net, settings=settings, sources=["openalex"])
    assert [(a["source"], a["status"]) for a in info.value.attempts] == [("openalex", "not_found")]
    assert not any("cyberleninka.ru" in u for u in ext.urls())


def test_get_fulltext_from_signed_in_elibrary(http, site, settings: Settings, tmp_path: Path):
    pdf_bytes = article_pdf()
    page = signed_in(fixture_bytes("ispdf_47430099") + fixture_bytes("item_patent_47430099"))
    site.add(lambda r: "item.asp?id=47430099" in str(r.url),
             lambda r: httpx2.Response(200, content=page, headers={"Content-Type": "text/html; Charset=UTF-8"}))
    site.add(lambda r: "full_text.asp?id=47430099" in str(r.url), _pdf_response(pdf_bytes))
    http.state.set("account", {"login": "reader"})
    res = ft.get_fulltext({"elibrary_id": 47430099}, http=http, settings=settings, sources=["elibrary"],
                          download_dir=tmp_path)
    assert res.source == "elibrary" and res.format == "pdf" and res.match_score == 1.0
    assert res.source_url == "https://elibrary.ru/item.asp?id=47430099"
    assert res.pdf_path == str(tmp_path / "elibrary_47430099.pdf")
    assert "биологического объяснения" in res.text
    assert [str(r.url) for r in site.requests] == ["https://elibrary.ru/item.asp?id=47430099",
                                                   "https://elibrary.ru/full_text.asp?id=47430099"]


def test_signed_in_card_replaces_the_cached_anonymous_copy(http, site, settings: Settings, tmp_path: Path):
    """Regression: with an anonymous copy of the card in the cache (viewed before `auth login`), every
    full-text call fetched item.asp again and dropped the signed-in page, so each retry (a paid
    article caches no text) and each item of `zotero add --with-pdf` cost one more request."""
    anon = fixture_bytes("item_article_46371463")
    signed = anon.replace("Незарегистрированный пользователь".encode(), b"reader")
    state = {"signed_in": False}
    site.add(lambda r: "item.asp?id=46371463" in str(r.url),
             lambda r: httpx2.Response(200, content=signed if state["signed_in"] else anon,
                                       headers={"Content-Type": "text/html; charset=utf-8"}))
    http.get("/item.asp?id=46371463", cache_ttl_hours=ft.TTL_ITEM)  # card viewed anonymously
    state["signed_in"] = True
    http.state.set("account", {"login": "reader"})
    for _ in range(3):
        with pytest.raises(NotFound) as info:
            ft.get_fulltext({"elibrary_id": 46371463}, http=http, settings=settings, sources=["elibrary"],
                            download_dir=tmp_path)
        attempt = info.value.attempts[0]
        assert attempt["status"] == "not_found" and "455 руб." in attempt["detail"]
    item_requests = [str(r.url) for r in site.requests if "item.asp" in str(r.url)]
    assert item_requests == ["https://elibrary.ru/item.asp?id=46371463"] * 2, "anonymous view + one refetch"
    cached = http.cached("GET", http.build_url("/item.asp?id=46371463"), ttl_hours=ft.TTL_ITEM)
    assert cached is not None and el.is_logged_in(cached.content)


def test_get_fulltext_elibrary_session_not_signed_in(http, site, settings: Settings, net: ExternalHTTP):
    site.page("item.asp?id=47430099", "item_patent_47430099")
    http.state.set("account", {"login": "stale"})
    with pytest.raises(NotFound) as info:
        ft.get_fulltext({"elibrary_id": 47430099}, http=http, net=net, settings=settings, sources=["elibrary"])
    attempt = info.value.attempts[0]
    assert attempt["source"] == "elibrary" and attempt["status"] == "login_required"
    assert "elibrary auth login" in attempt["detail"]
    assert len(site.requests) == 1


def test_get_fulltext_reports_all_attempts(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    req = {"doi": "10.1000/missing", "title": "Нет такой статьи", "authors": ["Петров В.А."], "year": 2020}
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json", method="POST")
    with pytest.raises(NotFound) as info:
        ft.get_fulltext(req, net=net, settings=settings)
    got = [(a["source"], a["status"]) for a in info.value.attempts]
    assert got == [("elibrary", "skipped"), ("cyberleninka", "no_match"), ("unpaywall", "skipped"),
                   ("openalex", "not_found"), ("crossref", "not_found"), ("publisher", "not_found")]
    assert "Unpaywall требует e-mail" in info.value.message
    assert not any("elibrary.ru" in u for u in ext.urls())


def test_refresh_ignores_text_cache(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json", method="POST")
    ext.html("cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah", ext_bytes("cyberleninka_article_neurons.html"))
    first = ft.get_fulltext(NEURONS_REQ, net=net, settings=settings, sources=["cyberleninka"])
    again = ft.get_fulltext(NEURONS_REQ, net=net, settings=settings, sources=["cyberleninka"], refresh=True)
    assert not first.from_cache and not again.from_cache and again.text == first.text


# ------------------------------------------------------------------- compliance (robots.txt, CyberLeninka)
CL_BASE = "https://cyberleninka.ru/article/n/neyronnye-seti"
CL_ROBOTS_URL = "https://cyberleninka.ru/robots.txt"
KSEN_REQ = {"title": "Нейронные сети", "authors": ["Ксенофонтов В.В."], "year": 2020}


def _paragraphs(topic: str, n: int = 6) -> list[str]:
    return [f"Абзац {i}: {topic} рассматривается на примерах из практики и сравнивается с другими подходами. " * 3
            for i in range(n)]


def _ksenofontov_page() -> bytes:
    return _cl_page("НЕЙРОННЫЕ СЕТИ", ["Ксенофонтов Вадим Валерьевич"], 2020,
                    ["НЕЙРОННЫЕ СЕТИ", "Ксенофонтов В.В.", *_paragraphs("обучение нейронных сетей")])


def test_cyberleninka_numbered_duplicate_found_by_slug(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json", method="POST")  # must stay unused
    ext.exact(CL_BASE, _cl_page("НЕЙРОННЫЕ СЕТИ", ["Петров Пётр Петрович"], 2019,
                                ["НЕЙРОННЫЕ СЕТИ", *_paragraphs("другая статья")]))
    ext.exact(CL_BASE + "-1", _ksenofontov_page())
    res = ft.get_fulltext(KSEN_REQ, net=net, settings=settings, sources=["cyberleninka"])
    assert res.source == "cyberleninka" and res.source_url == CL_BASE + "-1" and res.match_score == 1.0
    assert [(a.source, a.status) for a in res.attempts] == [("cyberleninka", "rejected"), ("cyberleninka", "ok")]
    assert res.attempts[0].url == CL_BASE and "другую статью" in res.attempts[0].detail
    assert ext.urls() == [CL_ROBOTS_URL, CL_BASE, CL_BASE + "-1"]  # no /api/, no /search, no PDF


def test_cyberleninka_missing_page_costs_one_request(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    with pytest.raises(NotFound) as info:
        ft.get_fulltext({"title": "Нет такой статьи", "year": 2020}, net=net, settings=settings,
                        sources=["cyberleninka"])
    assert info.value.attempts == [{"source": "cyberleninka", "status": "no_match",
                                    "detail": "на КиберЛенинке нет статьи с таким названием и авторами", "url": None}]
    # numbered duplicates exist only after the first article with that slug
    assert ext.urls() == [CL_ROBOTS_URL, "https://cyberleninka.ru/article/n/net-takoy-stati"]


def test_cyberleninka_search_api_is_opt_in(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("cyberleninka.ru/api/search", "cyberleninka_search_neurons.json", method="POST")
    ext.exact(CL_BASE + "-1", _ksenofontov_page())
    assert settings.cyberleninka_search is False
    with pytest.raises(NotFound):  # <slug> is missing, so the duplicate "-1" is not guessed
        ft.get_fulltext(KSEN_REQ, net=net, settings=settings, sources=["cyberleninka"])
    assert not any("/api/" in u for u in ext.urls())
    settings.cyberleninka_search = True  # ELIBRARY_CYBERLENINKA_SEARCH=1
    res = ft.get_fulltext(KSEN_REQ, net=net, settings=settings, sources=["cyberleninka"])
    assert res.source_url == CL_BASE + "-1" and res.match_score == 1.0
    posts = [r for r in ext.requests if r.method == "POST"]
    assert len(posts) == 1 and str(posts[0].url) == "https://cyberleninka.ru/api/search"
    assert json.loads(posts[0].content) == {"mode": "articles", "q": "Нейронные сети", "size": 10, "from": 0,
                                            "year_from": 2019, "year_to": 2021}
    assert not any(u.endswith("/pdf") for u in ext.urls())


def test_cyberleninka_robots_txt_is_honoured(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.on("cyberleninka.ru/robots.txt", body=CL_ROBOTS, ctype="text/plain")
    ext.html("cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah", ext_bytes("cyberleninka_article_neurons.html"))
    res = ft.get_fulltext(NEURONS_REQ, net=net, settings=settings, sources=["cyberleninka"])
    assert res.source == "cyberleninka"
    blocked = ExternalHTTP(settings, transport=httpx2.MockTransport(ext), cache=False, default_delay=0)
    ext.routes.insert(0, (lambda r: str(r.url) == CL_ROBOTS_URL,
                          lambda r: httpx2.Response(200, content=b"User-agent: *\nDisallow: /article/\n")))
    with pytest.raises(NotFound) as info:
        ft.get_fulltext(NEURONS_REQ, net=blocked, settings=settings, sources=["cyberleninka"], refresh=True)
    blocked.close()
    attempt = info.value.attempts[0]
    assert attempt["source"] == "cyberleninka" and attempt["status"] == "skipped"
    assert "robots.txt сайта cyberleninka.ru запрещает" in attempt["detail"]
    assert ext.urls().count("https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah") == 1


def test_cyberleninka_links_of_open_access_records_point_to_the_page():
    page = "https://cyberleninka.ru/article/n/neyrony-v-neyronnyh-setyah"
    up = src.unpaywall_record({
        "doi": "10.24412/2071-6168-2023-2-438-443", "is_oa": True,
        "best_oa_location": {"url_for_pdf": page + "/pdf", "url_for_landing_page": None},
        "oa_locations": [{"url_for_pdf": page + "/pdf", "version": "publishedVersion", "host_type": "repository"}]})
    assert up.pdf_urls == [] and up.cyberleninka_url == page and up.landing_url is None
    ox = src.openalex_record({"id": "W1", "open_access": {"is_oa": True, "oa_url": page + "/pdf"},
                              "primary_location": {"landing_page_url": page}, "locations": []})
    assert ox.cyberleninka_url == page and ox.pdf_urls == [] and ox.landing_url is None


RMMAR_LANDING = "https://journals.eco-vector.com/RMMArep/article/view/424450"
RMMAR_PDF = "https://journals.eco-vector.com/RMMArep/article/download/424450/138215"


def test_publisher_landing_disallowed_by_robots(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("api.crossref.org/works/10.17816/rmmar424450", "crossref_work_rmmar424450.json")
    ext.on("journals.eco-vector.com/robots.txt", body=b"User-agent: *\nDisallow: /RMMArep/\n", ctype="text/plain")
    ext.html("RMMArep/article/view/424450", ext_bytes("landing_ecovector_424450.html"))
    with pytest.raises(NotFound) as info:
        ft.get_fulltext(RMMAR_REQ, net=net, settings=settings, sources=["publisher"])
    skipped = [a for a in info.value.attempts if a["status"] == "skipped"]
    assert skipped and skipped[0]["url"] == RMMAR_LANDING
    assert "robots.txt сайта journals.eco-vector.com запрещает" in skipped[0]["detail"]
    assert RMMAR_LANDING not in ext.urls()


def test_publisher_pdf_disallowed_by_robots(settings: Settings, net: ExternalHTTP, ext: ExtSite, tmp_path: Path):
    ext.json("api.crossref.org/works/10.17816/rmmar424450", "crossref_work_rmmar424450.json")
    ext.on("journals.eco-vector.com/robots.txt", body=b"User-agent: *\nDisallow: /RMMArep/article/download/\n",
           ctype="text/plain")
    ext.html("RMMArep/article/view/424450", ext_bytes("landing_ecovector_424450.html"))
    ext.pdf("article/download/424450/138215", article_pdf())
    with pytest.raises(NotFound) as info:
        ft.get_fulltext(RMMAR_REQ, net=net, settings=settings, sources=["publisher"], download_dir=tmp_path)
    steps = [(a["source"], a["status"], a["url"]) for a in info.value.attempts]
    assert ("publisher", "skipped", RMMAR_PDF) in steps
    assert RMMAR_LANDING in ext.urls() and RMMAR_PDF not in ext.urls()
    assert not list(tmp_path.glob("*.pdf*"))
    # the JSON API (api.crossref.org) is not subject to robots.txt checks
    assert "https://api.crossref.org/robots.txt" not in ext.urls()


def test_publisher_never_downloads_cyberleninka_pdf(settings: Settings, net: ExternalHTTP, ext: ExtSite):
    ext.json("api.crossref.org/works/10.17816/rmmar424450", "crossref_work_rmmar424450.json")
    landing = ('<html><head><meta name="citation_doi" content="10.17816/rmmar424450">'
               '<meta name="citation_pdf_url" content="https://cyberleninka.ru/article/n/idiopaticheskoe/pdf">'
               '</head><body><p>Аннотация</p></body></html>').encode()
    ext.exact(RMMAR_LANDING, landing)
    with pytest.raises(NotFound) as info:
        ft.get_fulltext(RMMAR_REQ, net=net, settings=settings, sources=["publisher"])
    skipped = [a for a in info.value.attempts if a["status"] == "skipped"]
    assert skipped[0]["url"] == "https://cyberleninka.ru/article/n/idiopaticheskoe/pdf"
    assert "PDF КиберЛенинки не скачиваются" in skipped[0]["detail"]
    assert not any("cyberleninka.ru" in u for u in ext.urls())


RENSIT_CL = ("https://cyberleninka.ru/article/n/poluchenie-chastits-dioksida-titana-rutila-na-poverhnosti-"
             "vosstanovlennogo-oksida-grafena-v-sverhkriticheskom-izopropanole")


def test_saved_card_54076582_full_text_from_cyberleninka_end_to_end(http, site, settings: Settings,
                                                                     net: ExternalHTTP, ext: ExtSite):
    """The live flow: an anonymous card from elibrary.ru (saved 2026-10-13) -> no elibrary PDF request ->
    the CyberLeninka page derived from the title (synthetic page with the right citation_title)."""
    from elibrary_cli.client import ElibraryClient

    title = ("Получение частиц диоксида титана (рутила) на поверхности восстановленного оксида графена в "
             "сверхкритическом изопропаноле")
    site.page("item.asp?id=54076582", "item_article_54076582")
    paragraphs = ["УДК 544.023.2", "DOI: 10.17725/rensit.2023.15.153", title.upper(),
                  "Грошкова Ю.А., Краевский С.В., Буслаева Е.Ю.",
                  *_paragraphs("осаждение диоксида титана на восстановленный оксид графена", 8)]
    ext.exact(RENSIT_CL, _cl_page(title, ["Грошкова Ю. А.", "Краевский С. В.", "Буслаева Е. Ю."], 2023, paragraphs))
    client = ElibraryClient(http=http, net=net)
    res = client.get_fulltext(54076582, max_chars=1000)
    assert res.source == "cyberleninka" and res.source_url == RENSIT_CL and res.format == "html"
    assert res.match_score == 1.0  # the request's DOI is printed in the text
    assert res.doi == "10.17725/rensit.2023.15.153" and res.title == title
    assert res.text.startswith("УДК 544.023.2") and res.truncated
    assert [(a.source, a.status) for a in res.attempts] == [("elibrary", "skipped"), ("cyberleninka", "ok")]
    assert [str(r.url) for r in site.requests] == ["https://elibrary.ru/item.asp?id=54076582"]
    assert ext.urls() == [CL_ROBOTS_URL, RENSIT_CL]
    again = client.get_fulltext(54076582, offset=res.next_offset, max_chars=1000)
    assert again.from_cache and again.offset == res.next_offset
    assert len(site.requests) == 1 and len(ext.requests) == 2
    client.close()


def test_signed_in_card_links_to_full_text_asp():
    """Signed-in item page (live 2026-10-14): file_article(fid, n) -> file_article.asp, load_article(id) ->
    full_text.asp?id=<id>."""
    html = fixture_bytes("item_patent_47430099_signed_in")
    assert el.is_logged_in(html)
    links = el.find_elibrary_pdf_links(html, item_id=47430099)
    # the card's "Загрузить: Полный текст (PDF)" button (file_article) comes first — that is what
    # downloaded the PDF live; load_article's full_text.asp is the fallback
    assert links[0] == "https://elibrary.ru/file_article.asp?fileid=47430099&filenum=1"
    assert 'action="/full_text.asp?id=" + iid' in html.decode("utf-8")  # the signed-in load_article()
    assert not el.is_logged_in(fixture_bytes("item_patent_47430099"))
