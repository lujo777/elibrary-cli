"""Helpers shared by the page parsers (selectolax/Lexbor)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from elibrary_cli.models.base import BASE_URL

_WS_RE = re.compile(r"\s+")
_INT_RE = re.compile(r"-?\d[\d\s ]*")
_FLOAT_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
YEAR_RE = re.compile(r"(?<!\d)(1[89]\d\d|20\d\d)(?!\d)")


def parse_html(html: str | bytes) -> LexborHTMLParser:
    if isinstance(html, bytes):
        from elibrary_cli.http import decode_html

        html = decode_html(html)
    return LexborHTMLParser(html)


def clean(text: str | None) -> str:
    """Collapse whitespace (incl. &nbsp;) and strip."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text.replace(" ", " ")).strip()


def node_text(node: LexborNode | None, separator: str = " ") -> str:
    if node is None:
        return ""
    return clean(node.text(deep=True, separator=separator))


def to_int(text: str | None) -> int | None:
    """``"1 069"`` -> 1069; ``"нет"``/``""`` -> None."""
    if not text:
        return None
    m = _INT_RE.search(text.replace(" ", " "))
    if not m:
        return None
    try:
        return int(re.sub(r"[\s ]", "", m.group(0)))
    except ValueError:
        return None


def to_float(text: str | None) -> float | None:
    if not text:
        return None
    m = _FLOAT_RE.search(text.replace(" ", " ").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def yes_no(text: str | None) -> bool | None:
    """``"да"``/``"да (уровень 3)"`` -> True, ``"нет"`` -> False, other -> None."""
    t = clean(text).lower()
    if t.startswith("да") or t.startswith("yes"):
        return True
    if t.startswith("нет") or t.startswith("no"):
        return False
    return None


def abs_url(href: str | None, base: str = BASE_URL) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.lower().startswith("javascript:"):
        return None
    return urljoin(base.rstrip("/") + "/", href)


def query_param(href: str | None, name: str) -> str | None:
    if not href:
        return None
    qs = parse_qs(urlsplit(href).query)
    vals = qs.get(name) or qs.get(name.lower())
    return vals[0] if vals else None


def int_param(href: str | None, name: str) -> int | None:
    return to_int(query_param(href, name))


def first_year(text: str | None) -> int | None:
    if not text:
        return None
    years = YEAR_RE.findall(text)
    return int(years[-1]) if years else None


def text_lines(node: LexborNode | None) -> list[str]:
    """Visible text of a node as a list of non-empty, whitespace-normalised lines."""
    if node is None:
        return []
    out = []
    for raw in node.text(deep=True, separator="\n").split("\n"):
        line = clean(raw)
        if line:
            out.append(line)
    return out


def label_pairs(lines: Iterable[str]) -> list[tuple[str, str]]:
    """Turn ``["Тип:", "статья", "Год:", "2021"]`` into ``[("Тип", "статья"), ("Год", "2021")]``.

    A line ending in ``:`` is a label; the following line(s) up to the next label are its value.
    ``"Label: value"`` on one line is also split.
    """
    pairs: list[tuple[str, str]] = []
    label: str | None = None
    buf: list[str] = []
    for line in lines:
        if line.endswith(":") and len(line) <= 80:
            if label is not None:
                pairs.append((label, clean(" ".join(buf))))
            label, buf = line[:-1].strip(), []
        elif label is not None:
            buf.append(line)
    if label is not None:
        pairs.append((label, clean(" ".join(buf))))
    return pairs


def remove_noise(tree: LexborHTMLParser) -> LexborHTMLParser:
    for sel in ("script", "style", "noscript"):
        for n in tree.css(sel):
            n.decompose()
    return tree
