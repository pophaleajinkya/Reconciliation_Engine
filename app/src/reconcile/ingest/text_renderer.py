"""
Plain-text file renderer.

Produces a `RenderedPDF` (misnomer kept for ergonomic parity across the
ingest layer) from `.txt`, `.md`, `.log`, `.csv`, and similar UTF-8-ish
files so downstream classification + extraction can treat them identically
to native-text PDFs.

Design notes:
  * Text files have no "pages." We split on form-feed (`\x0c`, the POSIX
    page-break character) when present; otherwise we emit a single page.
    This keeps the `RenderedPage` abstraction honest while matching what
    multi-page text dumps actually look like in the wild.
  * There are no images to render, so OCR/vision tiers are no-ops on the
    resulting object — `looks_scanned` is always False and `ensure_ocr()`
    has nothing to do.
  * We detect encoding via a small sniff (UTF-8-SIG → UTF-8 → Latin-1) to
    stay robust to Windows / mainframe exports.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from reconcile.ingest.renderer import RenderedPage, RenderedPDF

log = logging.getLogger("reconcile.ingest")

# File extensions this renderer handles. CSV is included because small
# retailer claim files are often flat-CSV; a proper pandas path is a
# future upgrade (Tier 3), but even the raw text route lets the regex
# classifiers match and gives extractors something to work with.
SUPPORTED_EXTS: frozenset[str] = frozenset({
    ".txt",
    ".md",
    ".log",
    ".csv",
    ".tsv",
})


def _sniff_decode(data: bytes) -> str:
    """Decode bytes using a small cascade of plausible encodings."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # Last resort: replace undecodable bytes so we still get *something*.
    return data.decode("utf-8", errors="replace")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def render_text_file(path: Path) -> RenderedPDF:
    """
    Read a text file and wrap it as a `RenderedPDF`.

    If the file contains form-feed characters we treat each chunk as a
    separate page; otherwise the whole thing is page 1. Either way the
    returned object satisfies the `RenderedPDF` contract (native_text,
    full_text, pages, sha256, looks_scanned, etc.) so classifier and
    extractors consume it unchanged.
    """
    path = path.resolve()
    sha = _hash_file(path)
    raw = path.read_bytes()
    text = _sniff_decode(raw)

    chunks = text.split("\x0c") if "\x0c" in text else [text]
    pages = [
        RenderedPage(page_num=i + 1, text=chunk, image_path=None)
        for i, chunk in enumerate(chunks)
    ]
    log.info("loaded text file %s (%d page(s), %d chars)",
             path.name, len(pages), len(text))
    return RenderedPDF(source_path=path, pages=pages, sha256=sha)
