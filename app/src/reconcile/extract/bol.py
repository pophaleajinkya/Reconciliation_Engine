"""
BOL / POD extractor.

Bill of Lading text quality varies wildly — package 1's BOL renders to nearly
no text, package 2's BOL renders cleanly. We use text parse when we can see
line data, otherwise we fall back to vision (which is also where we look for
receiving stamps, handwritten shortage notes, etc.).
"""

from __future__ import annotations

import logging
import re

from reconcile.ingest.renderer import RenderedPDF
from reconcile.llm.groq_client import extract_json_from_image
from reconcile.schemas import (
    BillOfLading,
    ExtractionMethod,
    ReceivingEvidence,
    ShippedLine,
)

log = logging.getLogger("reconcile.extract.bol")

_BOL_NUM_RE = re.compile(r"BOL\s*NUMBER:?[^\d]*(\d{5,})", re.I)
_PRO_RE = re.compile(r"PRO\s*#:?\s*(\d{5,})", re.I)
_PO_RE = re.compile(r"Customer\s+PO\s*#:?\s*(\S+)", re.I)
_SHIP_DATE_RE = re.compile(r"SHIP\s+DATE:?\s*\S*\s*([\d/]+)", re.I)

# Line: "<material>  <desc>  <lot>  <qty>  <weight>  <cases>  <sku>"
_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<material>\d{4,6})\s+
    (?P<desc>.+?)\s+
    (?P<lot>\d{8,})\s+
    (?P<qty>\d+(?:\.\d+)?)\s+
    (?P<weight>[\d,]+\.\d+)\s+
    (?P<cases>\d+(?:\.\d+)?)\s+
    (?P<sku>\S+)\s*$
    """,
    re.VERBOSE | re.MULTILINE,
)


def _text_parse(rendered: RenderedPDF) -> BillOfLading | None:
    text = rendered.full_text
    if rendered.text_len < 300:
        return None

    norm = re.sub(r"[\t ]+", " ", text)
    lines = []
    for m in _LINE_RE.finditer(norm):
        lines.append(
            ShippedLine(
                material_number=m.group("material"),
                description=m.group("desc").strip(),
                cases=float(m.group("cases")),
                weight=float(m.group("weight").replace(",", "")),
                customer_sku=m.group("sku"),
            )
        )
    if not lines:
        return None

    bol_m = _BOL_NUM_RE.search(text)
    pro_m = _PRO_RE.search(text)
    po_m = _PO_RE.search(text)
    date_m = _SHIP_DATE_RE.search(text)

    return BillOfLading(
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=ExtractionMethod.TEXT_DETERMINISTIC,
        extraction_confidence=0.85,
        bol_number=bol_m.group(1) if bol_m else None,
        pro_number=pro_m.group(1) if pro_m else None,
        po_number=po_m.group(1) if po_m else None,
        ship_date=date_m.group(1) if date_m else None,
        lines=lines,
        total_cases=sum(ln.cases or 0 for ln in lines) or None,
    )


def _vision_parse(rendered: RenderedPDF) -> BillOfLading:
    schema_hint = (
        "{"
        '"bol_number": str|null,'
        '"pro_number": str|null,'
        '"po_number": str|null,'
        '"ship_date": str|null,'
        '"ship_to": str|null,'
        '"carrier": str|null,'
        '"content_belongs_to_different_shipment": bool,'
        '"lines": ['
        '  {"material_number": str|null, "customer_sku": str|null,'
        '   "description": str|null, "cases": number|null, "weight": number|null}'
        "],"
        '"total_cases": number|null,'
        '"receiving": {'
        '  "has_receiving_stamp": bool,'
        '  "stamp_notes": str|null,'
        '  "total_cases_shipped": number|null,'
        '  "total_cases_received": number|null,'
        '  "aggregate_shortage": bool,'
        '  "line_level_exceptions": [str]'
        "}"
        "}"
    )
    imgs = rendered.image_paths()
    if not imgs:
        return BillOfLading(
            source_path=str(rendered.source_path),
            pages=len(rendered.pages),
            extraction_method=ExtractionMethod.VISION_LLM,
            extraction_confidence=0.1,
            parse_warnings=["No page images; vision skipped."],
        )

    payload = extract_json_from_image(
        system_prompt=(
            "You are extracting data from a Bill of Lading / Delivery Order. "
            "IMPORTANT: Look for any receiving stamp, signature, or handwritten "
            "notes about totals, shortages, or damages. If the BOL text includes "
            "an unrelated shipment (wrong BOL number, wrong ship-to, wrong PO), "
            "set content_belongs_to_different_shipment=true and explain in "
            "receiving.stamp_notes. Never invent line items."
        ),
        schema_hint=schema_hint,
        image_paths=imgs,
        user_hint=(
            "Return receiving evidence even if partial. Prefer aggregate "
            "totals_cases_received when handwritten on the stamp."
        ),
    )

    lines = [ShippedLine(**ln) for ln in payload.get("lines") or []]
    recv_payload = payload.get("receiving") or {}
    receiving = ReceivingEvidence(**recv_payload) if recv_payload else None

    return BillOfLading(
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=ExtractionMethod.VISION_LLM,
        extraction_confidence=0.7 if (lines or receiving) else 0.2,
        bol_number=payload.get("bol_number"),
        pro_number=payload.get("pro_number"),
        po_number=payload.get("po_number"),
        ship_to=payload.get("ship_to"),
        carrier=payload.get("carrier"),
        ship_date=payload.get("ship_date"),
        content_belongs_to_different_shipment=bool(
            payload.get("content_belongs_to_different_shipment")
        ),
        lines=lines,
        total_cases=payload.get("total_cases"),
        receiving=receiving,
    )


# Handwritten receiving counts on a stamp are almost always 2-4 digits.
# Anything 5+ digits is overwhelmingly a PRO/tracking/invoice number that
# leaked in from the printed surround, not a real case count. We apply this
# sanity guard at every merge boundary so one hallucinated number doesn't
# quietly overwrite a plausible one from another source.
# Must match the ceiling in stamp_reader._PLAUSIBLE_MAX. Handwritten
# stamp counts above this almost always indicate a PRO/tracking number
# misread as a case count.
_MAX_PLAUSIBLE_CASES = 20000


def _is_plausible_cases(n) -> bool:
    try:
        return n is not None and 0 <= int(n) < _MAX_PLAUSIBLE_CASES
    except (TypeError, ValueError):
        return False


def _merge_receiving(stamp_focused: ReceivingEvidence | None,
                     full_page: ReceivingEvidence | None) -> ReceivingEvidence | None:
    """
    Merge the two receiving-evidence sources for a BOL:

    * `stamp_focused` — from the high-DPI stamp crop pipeline. This is the
      authoritative source for numeric stamp fields (shipped/received/short)
      because it sees the crop at ~3× the pixel density of the full page,
      with CLAHE contrast lift applied.
    * `full_page` — from the full-page vision pass (or a prior OCR extractor).
      Used as a fallback for fields the focused pass couldn't recover.

    Numeric stamp fields come from the focused pass when populated; narrative
    fields (notes, line-level exceptions) are unioned. Implausibly large
    numbers from EITHER source are rejected — they're PRO/tracking numbers
    that bled in, not case counts.
    """
    if stamp_focused is None and full_page is None:
        return None

    # Reject implausible numeric fields at the source so downstream merge
    # logic never sees them.
    def _sanitize(ev: ReceivingEvidence | None) -> ReceivingEvidence | None:
        if ev is None:
            return None
        ev = ev.model_copy(deep=True)
        if ev.total_cases_shipped is not None and not _is_plausible_cases(ev.total_cases_shipped):
            log.info("receiving: dropping implausible shipped=%s", ev.total_cases_shipped)
            ev.total_cases_shipped = None
        if ev.total_cases_received is not None and not _is_plausible_cases(ev.total_cases_received):
            log.info("receiving: dropping implausible received=%s", ev.total_cases_received)
            ev.total_cases_received = None
        return ev

    stamp_focused = _sanitize(stamp_focused)
    full_page = _sanitize(full_page)

    if stamp_focused is None:
        return full_page
    if full_page is None:
        return stamp_focused
    # Start from the stamp-focused record — trust its numbers.
    merged = stamp_focused.model_copy(deep=True)
    # Fill in null numeric fields from full_page, but only if focused truly
    # had nothing there.
    if merged.total_cases_shipped is None:
        merged.total_cases_shipped = full_page.total_cases_shipped
    if merged.total_cases_received is None:
        merged.total_cases_received = full_page.total_cases_received
    # Union notes (they can be complementary).
    if full_page.stamp_notes and full_page.stamp_notes not in (merged.stamp_notes or ""):
        merged.stamp_notes = (
            f"{merged.stamp_notes}; {full_page.stamp_notes}"
            if merged.stamp_notes
            else full_page.stamp_notes
        )
    if not merged.has_receiving_stamp and full_page.has_receiving_stamp:
        merged.has_receiving_stamp = True
    # Re-derive aggregate_shortage from final numbers when both present.
    if merged.total_cases_shipped is not None and merged.total_cases_received is not None:
        merged.aggregate_shortage = (
            int(merged.total_cases_received) < int(merged.total_cases_shipped)
        )
    elif full_page.aggregate_shortage and not merged.aggregate_shortage:
        merged.aggregate_shortage = True
    # Union line-level exceptions.
    if full_page.line_level_exceptions:
        existing = set(merged.line_level_exceptions or [])
        for ex in full_page.line_level_exceptions:
            if ex not in existing:
                merged.line_level_exceptions.append(ex)
    return merged


def _maybe_stamp_pass(rendered: RenderedPDF, bol: BillOfLading) -> BillOfLading:
    """
    Run the dedicated high-DPI stamp-reading pipeline and merge its findings
    into whatever receiving evidence we already have. Always safe to call —
    returns the original doc unchanged when no stamp region is found or the
    preprocessing stack isn't available.
    """
    try:
        from reconcile.extract.stamp_reader import read_stamps
    except Exception as e:
        log.warning("stamp_reader unavailable: %s", e)
        return bol
    focused = read_stamps(rendered)
    if focused is None:
        return bol
    # stamp_focused is the primary source for receiving fields.
    merged = _merge_receiving(focused, bol.receiving)
    if merged is None:
        return bol
    bol_copy = bol.model_copy(deep=True)
    bol_copy.receiving = merged
    bol_copy.parse_warnings = list(bol.parse_warnings) + ["stamp-focused pass applied"]
    # Bump confidence slightly when the focused pass gave us real numbers.
    if merged.total_cases_shipped or merged.total_cases_received:
        bol_copy.extraction_confidence = max(bol_copy.extraction_confidence, 0.8)
    return bol_copy


def extract_bol(rendered: RenderedPDF) -> BillOfLading:
    # Tier 1: native text for the line-item table.
    parsed = _text_parse(rendered)
    if parsed and parsed.lines:
        log.info("BOL lines parsed from native text (%d rows).", len(parsed.lines))
        # Only OCR stamp-carrier pages (usually page 1 and the last page) for
        # stamp-anchor detection. On a scanned 5-page BOL this saves ~60s/page.
        _ocr_stamp_pages_only(rendered)
        return _maybe_stamp_pass(rendered, parsed)

    # Scanned / unstructured BOL path.
    #
    # Historically we ran a full-document Surya pass here and re-ran the
    # line-item regex over the OCR text. In practice that regex never
    # recovers a BOL's columnar table from scan-OCR noise (column alignment
    # breaks), so we were paying ~60s/page for no benefit. Instead:
    #   * Send the page images to the vision LLM for the line items (cheap
    #     and much more accurate for semi-structured layouts), and
    #   * Only OCR the stamp-carrier pages, so stamp_detect still has the
    #     Surya line-bbox anchors it needs.
    log.info("BOL native text insufficient; using full-page vision + scoped OCR.")
    _ocr_stamp_pages_only(rendered)
    full = _vision_parse(rendered)
    return _maybe_stamp_pass(rendered, full)


def _ocr_stamp_pages_only(rendered: RenderedPDF) -> None:
    """
    Run Surya OCR only on the page(s) most likely to carry the receiving
    stamp — specifically page 1 and the last page. This is what stamp_detect
    needs for its "anchor phrase" lookup (e.g. 'RECEIVING STAMP'); the rest
    of the BOL doesn't benefit from OCR (vision handles the line table).
    """
    n = len(rendered.pages)
    if n == 0:
        return
    wanted = {1, n}  # 1-indexed; set dedupes when n == 1.
    try:
        rendered.ensure_ocr(pages=sorted(wanted))
    except TypeError:
        # Backwards compat if ensure_ocr signature changes.
        rendered.ensure_ocr()
