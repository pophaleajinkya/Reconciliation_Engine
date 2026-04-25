"""
PDF rendering and text extraction.

We ALWAYS capture both:
  - deterministic text via pdfplumber (may be empty for scanned/garbled PDFs)
  - page images via PyMuPDF (used by vision extractors when text is weak)

This lets downstream extractors decide which path to use without re-opening
the file repeatedly.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber

log = logging.getLogger("reconcile.ingest")


def _is_readable_char(c: str) -> bool:
    """
    True iff `c` is likely to be a real, readable glyph in extracted text.

    pdfplumber often returns `\x01` (SOH) for glyphs with no Unicode mapping —
    this happens on scanned PDFs where an OCR layer wasn't embedded. We treat
    those and other control characters as *missing* so downstream heuristics
    (`looks_scanned`, OCR fallback) trigger correctly.
    """
    if c.isspace():
        return False
    # Control characters (C0/C1) including the SOH (\x01) pdfplumber emits.
    o = ord(c)
    if o < 0x20 or 0x7F <= o < 0xA0:
        return False
    return True


def _has_real_text(s: str | None, min_chars: int = 40) -> bool:
    """True iff `s` contains at least `min_chars` readable characters."""
    if not s:
        return False
    n = 0
    for c in s:
        if _is_readable_char(c):
            n += 1
            if n >= min_chars:
                return True
    return False

# The default image is used for classification, OCR, and full-page vision.
# 220 DPI is the sweet spot for clean scans of 8.5"x11" receiving BOLs:
# legible handwriting and stamp text, file size stays <2 MB per page.
DEFAULT_DPI = 220
# High-quality pass: rendered lazily, only for stamp crops / hard-to-read
# regions where the ~2x pixel density translates directly into better
# handwriting recognition.
HIGH_QUALITY_DPI = 400


OCR_FALLBACK_CHAR_THRESHOLD = 120  # avg chars/page below this → try OCR


@dataclass
class RenderedPage:
    page_num: int  # 1-indexed
    text: str
    image_path: Path | None = None
    hq_image_path: Path | None = None  # lazily rendered at HIGH_QUALITY_DPI
    ocr_text: str | None = None
    ocr_confidence: float | None = None
    ocr_lines: list = field(default_factory=list)  # raw OCRLine objects for stamp anchoring


@dataclass
class RenderedPDF:
    source_path: Path
    pages: list[RenderedPage] = field(default_factory=list)
    sha256: str = ""
    ocr_attempted: bool = False
    ocr_used: bool = False

    @property
    def full_text(self) -> str:
        """Concatenated per-page text. Prefers OCR text when native text was empty."""
        parts: list[str] = []
        for p in self.pages:
            chosen = p.text if _has_real_text(p.text) else (p.ocr_text or "")
            parts.append(f"[page {p.page_num}]\n{chosen}")
        return "\n\n".join(parts)

    @property
    def native_text(self) -> str:
        """Original pdfplumber text only (no OCR mixed in). Useful for debugging."""
        return "\n\n".join(f"[page {p.page_num}]\n{p.text}" for p in self.pages)

    @property
    def text_len(self) -> int:
        """Total count of NON-WHITESPACE characters in native pdfplumber text.

        Some PDFs extract with hundreds of whitespace runs and near-zero real
        content; counting `.strip()` length mistakes those for real text and
        skips the OCR fallback. We filter whitespace here so scanned pages
        are reliably detected.
        """
        total = 0
        for p in self.pages:
            total += sum(1 for c in (p.text or "") if _is_readable_char(c))
        return total

    @property
    def effective_text_len(self) -> int:
        """Readable chars after OCR fallback (if it ran)."""
        total = 0
        for p in self.pages:
            chosen = p.text if p.text and _has_real_text(p.text) else (p.ocr_text or "")
            total += sum(1 for c in chosen if _is_readable_char(c))
        return total

    @property
    def looks_scanned(self) -> bool:
        """Heuristic: almost no extractable native text -> likely a scan."""
        if not self.pages:
            return True
        avg = self.text_len / max(1, len(self.pages))
        return avg < OCR_FALLBACK_CHAR_THRESHOLD

    def image_paths(self) -> list[Path]:
        return [p.image_path for p in self.pages if p.image_path]

    def ensure_hq_page(self, page_num: int, dpi: int = HIGH_QUALITY_DPI) -> Path | None:
        """
        Render a specific page at a higher DPI than the default. Used by stamp
        detection / focused vision passes that need more pixel density than the
        default pass can provide. Result is cached on the `RenderedPage`.
        """
        if page_num < 1 or page_num > len(self.pages):
            return None
        page = self.pages[page_num - 1]
        if page.hq_image_path and page.hq_image_path.exists():
            return page.hq_image_path
        if not page.image_path:
            return None
        try:
            with fitz.open(str(self.source_path)) as doc:
                pdf_page = doc[page_num - 1]
                mat = fitz.Matrix(dpi / 72, dpi / 72)
                pix = pdf_page.get_pixmap(matrix=mat, alpha=False)
                out = page.image_path.parent / f"page_{page_num:02d}__{dpi}dpi.png"
                if not out.exists():
                    pix.save(str(out))
                page.hq_image_path = out
                return out
        except Exception as e:
            log.warning("HQ render failed for %s page %d: %s", self.source_path.name, page_num, e)
            return None

    def ensure_ocr(self, *, pages: list[int] | None = None) -> None:
        """
        Tier-2 fallback: if native text extraction was too sparse, run Surya OCR
        on the page images to fill in `ocr_text`.

        Idempotent per page: only OCRs pages that haven't been OCR'd yet. When
        `pages` is given (1-indexed page numbers), restrict OCR to that subset
        — this lets callers like BOL extraction save seconds by only OCRing
        the pages that tend to carry the receiving stamp (first & last).
        """
        try:
            from reconcile.ingest.ocr import ocr_available, ocr_pages
        except Exception:
            return
        if not ocr_available():
            if not self.ocr_attempted:
                log.info("OCR not available; skipping Surya fallback for %s",
                         self.source_path.name)
            self.ocr_attempted = True
            return

        # Determine candidate pages (scanned + not yet OCR'd + in-scope).
        def _want(p: RenderedPage) -> bool:
            if not p.image_path or _has_real_text(p.text) or p.ocr_text:
                return False
            if pages is not None and p.page_num not in pages:
                return False
            return True

        # If no page-scope was given and the whole PDF doesn't look scanned,
        # preserve the original guard behaviour (don't burn CPU on text PDFs).
        if pages is None and not self.looks_scanned:
            self.ocr_attempted = True
            return

        targets = [p for p in self.pages if _want(p)]
        if not targets:
            if pages is None:
                self.ocr_attempted = True
            return

        image_paths = [p.image_path for p in targets]  # type: ignore[misc]
        try:
            ocr_results = ocr_pages(image_paths)  # type: ignore[arg-type]
        except Exception as e:
            log.warning("OCR run failed for %s: %s", self.source_path.name, e)
            return
        if not ocr_results:
            return
        for target, ocr_page in zip(targets, ocr_results):
            if ocr_page and ocr_page.text.strip():
                target.ocr_text = ocr_page.text
                target.ocr_confidence = ocr_page.mean_confidence
                target.ocr_lines = list(ocr_page.lines)
                self.ocr_used = True
        if pages is None:
            self.ocr_attempted = True
        if self.ocr_used:
            log.info("OCR filled in %d page(s) for %s",
                     sum(1 for p in self.pages if p.ocr_text),
                     self.source_path.name)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def render_pdf(
    pdf_path: Path,
    *,
    images_out_dir: Path,
    dpi: int = DEFAULT_DPI,
    render_images: bool = True,
) -> RenderedPDF:
    """Render a PDF to text per page and (optionally) PNG images per page."""
    pdf_path = pdf_path.resolve()
    sha = _hash_file(pdf_path)

    pages: list[RenderedPage] = []

    # Text pass with pdfplumber.
    texts: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for pg in pdf.pages:
                try:
                    texts.append(pg.extract_text() or "")
                except Exception as e:  # pragma: no cover
                    log.warning("pdfplumber page extract failed for %s: %s", pdf_path.name, e)
                    texts.append("")
    except Exception as e:
        log.warning("pdfplumber open failed for %s: %s", pdf_path.name, e)
        texts = []

    # Image pass with PyMuPDF (also gives us a reliable page count).
    image_paths: list[Path | None] = []
    try:
        with fitz.open(str(pdf_path)) as doc:
            page_count = doc.page_count
            if len(texts) < page_count:
                texts.extend([""] * (page_count - len(texts)))
            if render_images:
                target_dir = images_out_dir / f"{pdf_path.stem}__{sha}"
                target_dir.mkdir(parents=True, exist_ok=True)
                for i, page in enumerate(doc):
                    out = target_dir / f"page_{i + 1:02d}.png"
                    if not out.exists():
                        mat = fitz.Matrix(dpi / 72, dpi / 72)
                        pix = page.get_pixmap(matrix=mat, alpha=False)
                        pix.save(str(out))
                    image_paths.append(out)
            else:
                image_paths = [None] * page_count
    except Exception as e:
        log.warning("PyMuPDF open failed for %s: %s", pdf_path.name, e)
        image_paths = [None] * len(texts)

    for i, text in enumerate(texts):
        pages.append(
            RenderedPage(
                page_num=i + 1,
                text=text,
                image_path=image_paths[i] if i < len(image_paths) else None,
            )
        )

    return RenderedPDF(source_path=pdf_path, pages=pages, sha256=sha)
