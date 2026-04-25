"""
Receiving-stamp region detection.

PODs and stamped BOLs share a signature layout: a ~5-line labelled block
("RECEIVING STAMP" / "TOTAL CASES ON BOL" / "OVER SHORT CASES" /
"TOTAL CASES RECVD" / "RECEIVER PRINTED NAME" / "RECEIVER SIGNATURE") that the
receiver fills in with ink.  Feeding a VLM the *whole page* dilutes its
attention; feeding it *just the stamp area* yields significantly better
numeric readings, especially for faint handwriting.

Detection strategy (in order):

1. **OCR-anchored** — the preferred path.  We already run Surya on scanned
   pages; we reuse its line-level bboxes to locate phrases like "RECEIVING
   STAMP", "TOTAL CASES RECVD", or "RECEIVER SIGNATURE".  Take the union
   bounding box and pad ~40 px.

2. **Heuristic text-projection** — when OCR hasn't run, we look at row/column
   ink density on the grayscale image.  Receiving stamps are consistently in
   the middle third vertically (below the line-items table, above the
   "RECEIVED: Subject to..." legal blurb).  This gives us a decent bbox even
   without OCR text.

Both paths return a `StampRegion` with the crop coordinates.  The caller
does the actual `img[y1:y2, x1:x2]` slice.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

log = logging.getLogger("reconcile.stamp_detect")

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore


# Phrases that anchor a receiving stamp.  Regex allows for OCR noise
# (missing spaces, common character swaps).
_STAMP_ANCHORS = [
    re.compile(r"RECEIV(?:ING|ER)\s*STAMP", re.I),
    re.compile(r"TOTAL\s*#?\s*O?F?\s*CASES?\s*ON\s*BOL", re.I),
    re.compile(r"TOTAL\s*#?\s*O?F?\s*CASES?\s*REC[VD']*D", re.I),
    re.compile(r"OVER[\s/]*SHORT\s*CASES?", re.I),
    re.compile(r"RECEIVER\s*PRINTED\s*NAME", re.I),
    re.compile(r"RECEIVER\s*SIGNATURE", re.I),
]


@dataclass
class StampRegion:
    """Absolute pixel coordinates of the detected stamp box on a page image."""

    page_num: int
    x1: int
    y1: int
    x2: int
    y2: int
    detector: str  # "ocr-anchor" | "heuristic"
    anchors_matched: list[str]

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.x1, self.y1, self.x2, self.y2

    @property
    def w(self) -> int:
        return self.x2 - self.x1

    @property
    def h(self) -> int:
        return self.y2 - self.y1

    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)


# ---------------------------------------------------------------------------
# OCR-anchored detection
# ---------------------------------------------------------------------------


def detect_from_ocr_lines(
    page_num: int,
    ocr_lines: Sequence,
    page_size: tuple[int, int],
    pad: int = 40,
) -> StampRegion | None:
    """
    Find the stamp region by unioning bboxes of OCR lines that look like stamp
    anchor phrases.

    `ocr_lines` is expected to be a list of objects with `.text` (str) and
    `.bbox` (x1,y1,x2,y2) — matching reconcile.ingest.ocr.OCRLine.
    """
    if not ocr_lines:
        return None
    hits: list[tuple[tuple[float, float, float, float], str]] = []
    for ln in ocr_lines:
        text = getattr(ln, "text", "") or ""
        bbox = getattr(ln, "bbox", None)
        if not text or not bbox:
            continue
        for anc in _STAMP_ANCHORS:
            if anc.search(text):
                hits.append((bbox, text.strip()))
                break
    if not hits:
        return None

    pw, ph = page_size
    xs1 = [h[0][0] for h in hits]
    ys1 = [h[0][1] for h in hits]
    xs2 = [h[0][2] for h in hits]
    ys2 = [h[0][3] for h in hits]
    x1, y1, x2, y2 = min(xs1), min(ys1), max(xs2), max(ys2)

    # A Kroger/Albertsons receiving stamp is roughly 6-8 label lines tall
    # after its "RECEIVING STAMP" header, plus handwritten fill-ins and a
    # signature line below. Empirically this is ~12× the header height.
    header_h = max(1, y2 - y1)
    downward_expansion = int(header_h * 11) if len(hits) <= 2 else int(header_h * 2)

    # Expand outward. Bias expansion downward (stamp fields are below the
    # header) and slightly to the right (totals columns).
    x1 = max(0, int(x1) - pad * 2)
    y1 = max(0, int(y1) - pad)
    x2 = min(pw, int(x2) + int(pad * 4))
    y2 = min(ph, int(y2) + downward_expansion)

    # Also pull the right edge out: stamps often overflow their label bboxes.
    # Cap at 98% of page width.
    x2 = min(int(pw * 0.98), x2)
    # Cap total height at 35% of page height to avoid swallowing the whole doc.
    if (y2 - y1) > int(ph * 0.40):
        y2 = y1 + int(ph * 0.40)

    if (x2 - x1) < 100 or (y2 - y1) < 60:
        return None
    return StampRegion(
        page_num=page_num,
        x1=x1, y1=y1, x2=x2, y2=y2,
        detector="ocr-anchor",
        anchors_matched=[t for _, t in hits],
    )


# ---------------------------------------------------------------------------
# Heuristic projection-based detection
# ---------------------------------------------------------------------------


def detect_from_image(page_num: int, img: np.ndarray) -> StampRegion | None:
    """
    Pure image-based heuristic when OCR text isn't available.

    The approach: look for a rectangular band (at least 25% of page width,
    between the top 30% and bottom 15% of the page) with a burst of ink,
    often including a red/dark overlay that the scanner renders as darker
    grayscale pixels.  This is deliberately loose — its only job is to
    bracket the region for downstream vision/OCR.
    """
    if cv2 is None or img is None:
        return None
    h, w = img.shape[:2]
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Emphasize ink: invert and threshold.
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    row_sum = bw.sum(axis=1)  # per-row ink amount
    # Normalize.
    if row_sum.max() == 0:
        return None
    norm = row_sum.astype(np.float32) / float(row_sum.max())

    # Search only rows [0.30*h, 0.85*h] to avoid header & footer blurb.
    y_start = int(h * 0.30)
    y_end = int(h * 0.85)
    window = norm[y_start:y_end]
    if window.size == 0:
        return None

    # Smoothed rolling mean for stability.
    k = max(15, h // 80)
    kernel = np.ones(k, dtype=np.float32) / k
    smoothed = np.convolve(window, kernel, mode="same")

    # The receiving stamp is typically 6-10 text lines tall → ~180 px at
    # 200 dpi or ~270 px at 300 dpi.  Use a min band of h*0.06.
    min_band_h = max(80, int(h * 0.06))
    threshold = smoothed.mean() + 0.15 * (smoothed.max() - smoothed.mean())
    mask = smoothed > threshold
    # Find longest contiguous run.
    best_start, best_len = -1, 0
    i = 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            run = j - i
            if run > best_len:
                best_len = run
                best_start = i
            i = j
        else:
            i += 1
    if best_len < min_band_h:
        return None

    y1 = y_start + best_start
    y2 = y_start + best_start + best_len
    # Expand slightly so we catch labels sitting just above the ink band.
    y1 = max(0, y1 - int(h * 0.02))
    y2 = min(h, y2 + int(h * 0.02))
    # Horizontally we keep most of the right half of the page because the
    # stamp always overlaps the right columns where totals live.  This
    # matches both Kroger and Albertsons layouts observed in the bundles.
    x1 = int(w * 0.05)
    x2 = int(w * 0.98)
    return StampRegion(
        page_num=page_num,
        x1=x1, y1=y1, x2=x2, y2=y2,
        detector="heuristic",
        anchors_matched=[],
    )


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------


def detect(
    page_num: int,
    img: np.ndarray,
    ocr_lines: Sequence | None = None,
) -> StampRegion | None:
    """Try OCR-anchored detection first, fall back to heuristic."""
    if img is None:
        return None
    h, w = img.shape[:2]
    if ocr_lines:
        region = detect_from_ocr_lines(page_num, ocr_lines, page_size=(w, h))
        if region:
            return region
    return detect_from_image(page_num, img)


def crop(img: np.ndarray, region: StampRegion) -> np.ndarray:
    return img[region.y1 : region.y2, region.x1 : region.x2].copy()


def save_debug(region: StampRegion, crop_img: np.ndarray, out_dir: Path) -> dict[str, Path]:
    """Persist the crop + preprocessed variants to disk for inspection."""
    from reconcile.ingest import preprocess as pp

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    paths["raw"] = pp.write_image(out_dir / "stamp_raw.png", crop_img)
    paths["vision"] = pp.write_image(out_dir / "stamp_vision.jpg", pp.enhance_for_vision(crop_img))
    paths["ocr"] = pp.write_image(out_dir / "stamp_ocr.png", pp.enhance_for_ocr(crop_img))
    return paths
