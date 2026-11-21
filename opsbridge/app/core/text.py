"""HTML -> plain text conversion for Teams message bodies (stdlib only)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# --------------------------------------------------------------------------------------
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS and tag != "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(content: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(content)
    extractor.close()
    text = html.unescape("".join(extractor.parts)).replace("\xa0", " ").replace("\r\n", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --------------------------------------------------------------------------------------
# Search tokenisation (shared by keyword search, mock embeddings, hint extraction)
# --------------------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be but by for from has have how i in is it its my of on or our that the their them this "
    "to was we were what when where which who why will with you your can could does did do not no any after "
    "before again still please help there here into about".split()
)


def normalise_tokens(text: str) -> list[str]:
    """Lowercase words minus stopwords, with a light suffix stemmer (failing/failed/fails -> fail)."""
    out = []
    for w in _WORD.findall((text or "").lower()):
        if w in STOPWORDS or len(w) < 2:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out
