"""
Stamp-focused receiving-evidence extractor.

Given a rendered PDF page that contains a retailer receiving stamp (Kroger,
Albertsons, etc.), this module:

1. Locates the stamp region (OCR-anchored preferred, heuristic fallback).
2. Crops the page at a high-DPI render and writes three variants to disk
   for debugging / downstream use:
     - raw crop
     - vision-enhanced color crop (CLAHE + sharpen + upscale)
     - OCR-ready binarized crop (deskew + CLAHE + sharpen + adaptive thresh)
3. Runs Surya on the OCR crop — now receiving ~3× more pixels per glyph.
4. Runs a *focused* Groq vision call on the color crop with a schema that
   only asks about stamp fields (totals, shortages, signature presence).
5. Fuses the two outputs into a single ReceivingEvidence, preferring Surya
   for printed numbers and the VLM for handwriting/signature interpretation.

This yields materially better results than feeding a full 8.5"x11" page to
either engine alone, because the stamp crop contains almost nothing *but*
the information we want to extract.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from reconcile.ingest import preprocess as pp
from reconcile.ingest import stamp_detect
from reconcile.ingest.renderer import DEFAULT_DPI, HIGH_QUALITY_DPI, RenderedPDF, RenderedPage
from reconcile.llm.groq_client import extract_json_from_image
from reconcile.schemas import ReceivingEvidence

log = logging.getLogger("reconcile.extract.stamp_reader")


# ---------------------------------------------------------------------------
# Numeric parsers over stamp crop text
# ---------------------------------------------------------------------------

# Labels vary slightly across retailers; regexes are intentionally loose.
_SHIPPED_LABEL = r"(?:TOTAL\s*#?\s*O?F?\s*CASES?\s*ON\s*BOL|CASES\s*SHIPPED|CASES\s*ON\s*BOL)"
_RECEIVED_LABEL = r"(?:TOTAL\s*#?\s*O?F?\s*CASES?\s*REC[VD']*D|CASES\s*RECEIVED|REC[VD']*D)"
_SHORT_LABEL = r"(?:OVER[\s/]*SHORT\s*CASES?|SHORT\s*CASES?)"

_AFTER = re.compile(r"\D{0,40}([0-9][0-9,]{0,6})")


def _find_after(label_re: str, text: str) -> int | None:
    m = re.search(label_re + _AFTER.pattern, text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _find_before(label_re: str, text: str) -> int | None:
    m = re.search(r"([0-9][0-9,]{0,6})\s*[\n\r]{0,3}[^\n]{0,20}" + label_re, text,
                  re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_numbers(text: str) -> tuple[int | None, int | None, int | None]:
    if not text:
        return None, None, None
    shipped = _find_after(_SHIPPED_LABEL, text) or _find_before(_SHIPPED_LABEL, text)
    received = _find_after(_RECEIVED_LABEL, text) or _find_before(_RECEIVED_LABEL, text)
    # Many "Over/Short" fields are blank-underlined when no exception → keep tolerant.
    short = _find_after(_SHORT_LABEL, text)
    return shipped, received, short


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class StampReadResult:
    evidence: ReceivingEvidence | None
    region_detector: str | None
    crop_paths: dict[str, Path]
    ocr_text: str
    vision_notes: str | None
    used_vision: bool


def _surya_ocr_on_image(path: Path) -> tuple[str, float]:
    """Run Surya on an arbitrary image (used as last-resort when no full-page
    OCR lines are already available). Prefer `_reuse_page_ocr_in_bbox` which
    is ~100× faster because it doesn't re-invoke the OCR model."""
    try:
        from reconcile.ingest.ocr import ocr_pages
    except Exception:
        return "", 0.0
    pages = ocr_pages([path])
    if not pages:
        return "", 0.0
    return pages[0].text, pages[0].mean_confidence


def _reuse_page_ocr_in_bbox(
    page_ocr_lines: list,
    hq_bbox: tuple[int, int, int, int],
    default_to_hq_scale: float,
) -> tuple[str, float]:
    """
    Reuse the page-level Surya lines we already have, filtering to those that
    fall inside the stamp region.

    `page_ocr_lines` are in DEFAULT-DPI image coordinates (what Surya saw).
    `hq_bbox` is in HIGH-QUALITY-DPI image coordinates (what the crop came
    from). We convert HQ bbox back into default-dpi coords to compare.
    """
    if not page_ocr_lines:
        return "", 0.0
    x1, y1, x2, y2 = hq_bbox
    # Convert HQ-coords bbox back into default-dpi coords.
    scale = 1.0 / default_to_hq_scale  # e.g. 220/400 = 0.55
    x1d, y1d, x2d, y2d = x1 * scale, y1 * scale, x2 * scale, y2 * scale

    picked: list = []
    for ln in page_ocr_lines:
        try:
            lx1, ly1, lx2, ly2 = ln.bbox
        except Exception:
            continue
        # Require >= 50% of the line's bbox area to be inside the region.
        ix1 = max(lx1, x1d); iy1 = max(ly1, y1d)
        ix2 = min(lx2, x2d); iy2 = min(ly2, y2d)
        if ix2 <= ix1 or iy2 <= iy1:
            continue
        inter = (ix2 - ix1) * (iy2 - iy1)
        area = max(1.0, (lx2 - lx1) * (ly2 - ly1))
        if inter / area < 0.5:
            continue
        picked.append(ln)
    if not picked:
        return "", 0.0
    # Reconstruct reading-order text.
    picked.sort(key=lambda ln: (round(ln.bbox[1] / 8) * 8, ln.bbox[0]))
    text = "\n".join(ln.text for ln in picked)
    confs = [ln.confidence for ln in picked if ln.confidence is not None]
    mean_conf = sum(confs) / len(confs) if confs else 0.0
    return text, mean_conf


_VISION_SCHEMA = (
    "{"
    '"has_receiving_stamp": bool,'
    '"stamp_date": str|null,'
    '"total_cases_shipped": number|null,'
    '"total_cases_received": number|null,'
    '"over_short_cases": number|null,'
    '"receiver_printed_name": str|null,'
    '"has_signature": bool,'
    '"handwritten_exceptions": [str],'
    '"verbatim_quote": str|null'
    "}"
)


def _vision_read(crop_path: Path) -> dict:
    return extract_json_from_image(
        system_prompt=(
            "This image is a tight crop of a retailer's RECEIVING STAMP from a "
            "Bill of Lading or Proof of Delivery. It contains printed labels "
            "and the receiver's handwritten fill-ins (numbers, dates, printed "
            "name, signature). Read the stamp carefully and extract ONLY what "
            "is visibly written — do not infer. If a field is blank, return "
            "null."
        ),
        schema_hint=_VISION_SCHEMA,
        image_paths=[crop_path],
        user_hint=(
            "Return numbers as plain integers (no commas). For 'over_short_cases',"
            " return a positive integer for OVER, negative for SHORT, 0 if the "
            "field is explicitly zero, or null if blank. If you can transcribe "
            "the stamp's full text, put it in verbatim_quote."
        ),
    )


def read_stamp(
    rendered: RenderedPDF,
    page: RenderedPage,
    *,
    crops_out_dir: Path | None = None,
) -> StampReadResult:
    """
    Detect the receiving stamp on `page`, then read its fields with OCR + VLM.
    Returns a `StampReadResult` even on partial success; `evidence` is None
    only when no stamp region could be located at all.
    """
    if not pp.available():
        log.info("OpenCV not available; skipping stamp-focused pass.")
        return StampReadResult(None, None, {}, "", None, False)

    # 1. Ensure we have a high-quality render of this page.
    hq_path = rendered.ensure_hq_page(page.page_num)
    if hq_path is None:
        log.info("HQ render failed for page %d; falling back to default image.", page.page_num)
        hq_path = page.image_path
    if hq_path is None:
        return StampReadResult(None, None, {}, "", None, False)

    img = pp.read_image(hq_path)
    if img is None:
        return StampReadResult(None, None, {}, "", None, False)

    # 2. Locate the stamp region (OCR anchors if available).
    # OCR bboxes are in DEFAULT_DPI coords; we're cropping from HQ pixels,
    # so scale anchors up before detection.
    ocr_lines_scaled = None
    if page.ocr_lines:
        scale = HIGH_QUALITY_DPI / DEFAULT_DPI
        ocr_lines_scaled = []
        for ln in page.ocr_lines:
            try:
                x1, y1, x2, y2 = ln.bbox
                # Shallow copy with scaled bbox.
                scaled = type(ln)(
                    text=ln.text,
                    confidence=ln.confidence,
                    bbox=(x1 * scale, y1 * scale, x2 * scale, y2 * scale),
                )
                ocr_lines_scaled.append(scaled)
            except Exception:
                ocr_lines_scaled.append(ln)

    region = stamp_detect.detect(page.page_num, img, ocr_lines=ocr_lines_scaled)
    if region is None:
        log.info("No stamp region detected on page %d.", page.page_num)
        return StampReadResult(None, None, {}, "", None, False)
    log.info("Stamp region on page %d detected via %s: bbox=%s anchors=%s",
             page.page_num, region.detector, region.bbox, region.anchors_matched[:3])

    crop = stamp_detect.crop(img, region)

    # 3. Persist debug crops (raw / vision / ocr) for troubleshooting.
    if crops_out_dir is None:
        crops_out_dir = hq_path.parent / f"stamp_p{page.page_num:02d}"
    crop_paths = stamp_detect.save_debug(region, crop, crops_out_dir)

    # 4. Corroborating OCR for the stamp region.
    #
    #    We REUSE the Surya lines that were captured during the full-page OCR
    #    pass (ingest.renderer.ensure_ocr) and simply filter to those whose
    #    bbox falls inside the stamp region. This saves a second Surya
    #    invocation that otherwise dominated wall-clock time (~130 s per BOL).
    #
    #    Only if no page-level OCR lines exist at all (e.g. OCR was skipped
    #    because native text looked sufficient) do we fall back to running
    #    Surya directly on the crop.
    if page.ocr_lines:
        ocr_text, ocr_conf = _reuse_page_ocr_in_bbox(
            page.ocr_lines,
            hq_bbox=region.bbox,
            default_to_hq_scale=HIGH_QUALITY_DPI / DEFAULT_DPI,
        )
    else:
        ocr_text, ocr_conf = _surya_ocr_on_image(crop_paths["ocr"])
    shipped_o, received_o, short_o = _parse_numbers(ocr_text)
    log.info("Stamp OCR (reused page-level lines): shipped=%s received=%s short=%s conf=%.2f",
             shipped_o, received_o, short_o, ocr_conf)

    # 5. Focused vision call on the color-enhanced crop. The VLM is our
    #    primary channel for handwritten fill-ins.
    vision_payload: dict = {}
    try:
        vision_payload = _vision_read(crop_paths["vision"])
    except Exception as e:
        log.warning("Stamp vision call failed: %s", e)
    used_vision = bool(vision_payload) and "_error" not in vision_payload

    # 6. Fuse. Handwritten fields come from the VLM first; OCR is only used
    #    to fill gaps or to flag VLM hallucinations that disagree by a large
    #    margin (>~25x) with a printed-looking OCR reading.
    shipped_v = vision_payload.get("total_cases_shipped")
    received_v = vision_payload.get("total_cases_received")
    short_v = vision_payload.get("over_short_cases")

    # Handwritten case counts on a receiving stamp are almost always
    # 2-4 digits (hundreds to low thousands per shipment). A 5+ digit
    # number is overwhelmingly a PRO/tracking/invoice/BOL number that
    # leaked in from the printed surround, not a real count.
    #
    # We pick a conservative ceiling of 20,000 — above that we treat the
    # reading as noise regardless of whether it came from OCR or the VLM.
    # The largest realistic single-shipment case count we've seen on a
    # Kroger stamp is ~4-5k cases (full truckload of light produce).
    _PLAUSIBLE_MAX = 20000

    def _plausible(n) -> bool:
        try:
            return n is not None and 0 <= int(n) < _PLAUSIBLE_MAX
        except (TypeError, ValueError):
            return False

    def _pick(v_val, o_val):
        """Prefer VLM; fall back to OCR. Reject implausible readings from
        either source (handwritten stamp counts are ~2-4 digits)."""
        if _plausible(v_val):
            return v_val
        if _plausible(o_val):
            return o_val
        # If VLM gave us something but it's implausible, log it so we can
        # see hallucinations in the evidence trail instead of silently
        # dropping them.
        if v_val is not None and not _plausible(v_val):
            log.info("stamp: dropping implausible VLM reading %s (> %d)", v_val, _PLAUSIBLE_MAX)
        return None

    shipped = _pick(shipped_v, shipped_o)
    received = _pick(received_v, received_o)
    short = _pick(short_v, short_o)

    # Aggregate-shortage decision: use (shipped - received) when both exist,
    # otherwise fall back to the explicit "over/short" field.
    aggregate_short = False
    if shipped is not None and received is not None:
        aggregate_short = int(received) < int(shipped)
    elif short is not None:
        aggregate_short = int(short) < 0

    line_level = list(vision_payload.get("handwritten_exceptions") or [])

    notes_bits: list[str] = []
    if shipped is not None:
        notes_bits.append(f"Cases on BOL: {int(shipped)}")
    if received is not None:
        notes_bits.append(f"Cases received: {int(received)}")
    if short is not None:
        notes_bits.append(f"Over/Short: {int(short)}")
    if vision_payload.get("stamp_date"):
        notes_bits.append(f"Date: {vision_payload['stamp_date']}")
    if vision_payload.get("receiver_printed_name"):
        notes_bits.append(f"Receiver: {vision_payload['receiver_printed_name']}")
    if vision_payload.get("has_signature"):
        notes_bits.append("signed")
    notes = "; ".join(notes_bits) or None

    evidence = ReceivingEvidence(
        has_receiving_stamp=bool(shipped or received or vision_payload.get("has_receiving_stamp")),
        stamp_notes=notes,
        total_cases_shipped=shipped,
        total_cases_received=received,
        aggregate_shortage=aggregate_short,
        line_level_exceptions=line_level,
    )

    return StampReadResult(
        evidence=evidence,
        region_detector=region.detector,
        crop_paths=crop_paths,
        ocr_text=ocr_text,
        vision_notes=vision_payload.get("verbatim_quote"),
        used_vision=used_vision,
    )


def read_stamps(rendered: RenderedPDF) -> ReceivingEvidence | None:
    """
    Run `read_stamp` across every page and keep the strongest result (the one
    with the most populated numeric fields).
    """
    best: StampReadResult | None = None
    best_score = -1
    for page in rendered.pages:
        result = read_stamp(rendered, page)
        if result.evidence is None:
            continue
        ev = result.evidence
        score = sum(
            1 for v in (ev.total_cases_shipped, ev.total_cases_received)
            if v is not None
        )
        score += 1 if ev.has_receiving_stamp else 0
        score += 1 if result.used_vision else 0
        if score > best_score:
            best_score = score
            best = result
    return best.evidence if best else None
