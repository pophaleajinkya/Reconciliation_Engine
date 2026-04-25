"""
Deduction claim extractor.

Strategy:
  1. Deterministic text parse of the "Associated Deductions" table if present
     (Kroger/PRGX format produces readable text on the clean page).
  2. If text extraction returns nothing (e.g. package 2's garbled first page),
     fall back to vision extraction over all page images.
"""

from __future__ import annotations

import logging
import re

from reconcile.extract.base import parse_amount
from reconcile.ingest.renderer import RenderedPDF
from reconcile.llm.groq_client import extract_json_from_image
from reconcile.schemas import (
    ClaimLine,
    ClaimType,
    DeductionClaim,
    ExtractionMethod,
)

log = logging.getLogger("reconcile.extract.claim")


_INVOICE_RE = re.compile(r"KROGER\s+Invoice\s+number:\s*(\d+)", re.I)
_INV_HEADER_ALT_RE = re.compile(r"Invoice\s+number\s+(\d{7,})", re.I)
_PO_RE = re.compile(r"PO\s+Number\s+(\S+)", re.I)
_DEDUCTION_AMT_RE = re.compile(r"Deduction amount\s*\(\s*-\s*\)\s*\$\s*([\d,]+\.\d{2})", re.I)
_GROSS_AMT_RE = re.compile(r"Gross invoice amount\s*\(\s*\+\s*\)\s*\$\s*([\d,]+\.\d{2})", re.I)
_NET_AMT_RE = re.compile(r"Net invoice amount\s*\(\s*\+\s*\)\s*\$\s*([\d,]+\.\d{2})", re.I)
_DISC_AMT_RE = re.compile(r"Discount amount\s*\(\s*-\s*\)\s*\$\s*([\d,]+\.\d{2})", re.I)


_UPC_RE = re.compile(r"\b(\d{11,14})\b")
_QTY_UNIT_RE = re.compile(r"\b(\d{1,5})\s+\$\s*([\d,]+\.\d{2})\b")
_ADJ_AMT_INLINE_RE = re.compile(r"\(-\)\s*\$\s*([\d,]+\.\d{2})")
# pdfplumber sometimes splits "(-) $" from its amount across 2-3 lines of
# column-interleaved text. We accept any intervening content.
_ADJ_AMT_SPLIT_RE = re.compile(r"\(-\)\s*\$.*?([\d,]+,\d{3}\.\d{2})", re.DOTALL)
_ADJ_AMT_LINESTART_RE = re.compile(r"^\s*([\d,]+\.\d{2})\s*\(shortage\)", re.MULTILINE)
_REASON_TEXT_RE = re.compile(
    r"(Item\s*invoiced/?Not\s*received|shortage|pricing|compliance|damage|unsaleab)",
    re.I,
)
_CODE_AFTER_UNIT_RE = re.compile(r"\$\s*[\d,]+\.\d{2}\s+(\d{1,2})\b")


def _parse_deduction_rows(text: str) -> list[ClaimLine]:
    """
    pdfplumber column-extracts the Associated Deductions table so fields land
    interleaved. Strategy: find each UPC, take an ~18-line window around it,
    and pick out the structured fields with tight regexes. We require one of
    the reason keywords to appear in the window so we don't catch stray UPCs.
    """
    raw_lines = text.splitlines()
    results: list[ClaimLine] = []
    seen_upcs: set[str] = set()

    for i, line in enumerate(raw_lines):
        m = _UPC_RE.search(line)
        if not m:
            continue
        upc = m.group(1)
        if upc in seen_upcs:
            continue

        window_start = max(0, i - 6)
        window_end = min(len(raw_lines), i + 12)
        window_block = "\n".join(raw_lines[window_start:window_end])
        if not _REASON_TEXT_RE.search(window_block):
            continue

        seen_upcs.add(upc)

        qty: float | None = None
        unit_price: float | None = None
        qu = _QTY_UNIT_RE.search(window_block)
        if qu:
            qty = float(qu.group(1))
            unit_price = float(qu.group(2).replace(",", ""))

        adj_amount: float | None = None
        am = _ADJ_AMT_INLINE_RE.search(window_block)
        if am:
            try:
                adj_amount = -float(am.group(1).replace(",", ""))
            except ValueError:
                adj_amount = None
        if adj_amount is None:
            am2 = _ADJ_AMT_SPLIT_RE.search(window_block)
            if am2:
                adj_amount = -float(am2.group(1).replace(",", ""))
        if adj_amount is None:
            am3 = _ADJ_AMT_LINESTART_RE.search(window_block)
            if am3:
                adj_amount = -float(am3.group(1).replace(",", ""))

        code: str | None = None
        cm_code = _CODE_AFTER_UNIT_RE.search(window_block)
        if cm_code:
            code = cm_code.group(1)

        reason_m = _REASON_TEXT_RE.search(window_block)
        reason_text = reason_m.group(0) if reason_m else None

        # Description: lines in the window that are mostly uppercase letters,
        # excluding the UPC-bearing line itself.
        desc_candidates = []
        for rl in raw_lines[max(0, i - 3): i + 4]:
            if _UPC_RE.search(rl):
                continue
            if re.fullmatch(r"[A-Z0-9 \-/\.]{3,}", rl.strip()):
                desc_candidates.append(rl.strip())
        description = " ".join(desc_candidates[:3]) or None

        results.append(
            ClaimLine(
                upc=upc,
                description=description,
                adj_qty=qty,
                unit_price=unit_price,
                adj_amount=adj_amount,
                reason_code=code,
                reason_text=reason_text,
            )
        )
    return results


def _text_parse(rendered: RenderedPDF) -> DeductionClaim | None:
    text = rendered.full_text
    if not text or len(text.strip()) < 200:
        return None

    inv_m = _INVOICE_RE.search(text) or _INV_HEADER_ALT_RE.search(text)
    po_m = _PO_RE.search(text)
    ded_m = _DEDUCTION_AMT_RE.search(text)
    gross_m = _GROSS_AMT_RE.search(text)
    net_m = _NET_AMT_RE.search(text)
    disc_m = _DISC_AMT_RE.search(text)

    claim_lines = _parse_deduction_rows(text)
    if not claim_lines:
        return None

    # Fallback: if exactly one claim line is missing its amount and the header
    # shows a single deduction amount, adopt it (with a warning).
    warnings: list[str] = []
    if (
        len(claim_lines) == 1
        and claim_lines[0].adj_amount is None
        and ded_m
    ):
        amt = parse_amount(ded_m.group(0))
        if amt is not None:
            claim_lines[0].adj_amount = -abs(amt)
            warnings.append(
                "claim line adj_amount recovered from header deduction_amount "
                "(pdfplumber interleaved the inline amount)"
            )

    return DeductionClaim(
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=ExtractionMethod.TEXT_DETERMINISTIC,
        extraction_confidence=0.9,
        invoice_number=inv_m.group(1) if inv_m else None,
        po_number=po_m.group(1) if po_m else None,
        deduction_amount=parse_amount(ded_m.group(0)) if ded_m else None,
        gross_invoice_amount=parse_amount(gross_m.group(0)) if gross_m else None,
        net_invoice_amount=parse_amount(net_m.group(0)) if net_m else None,
        discount_amount=parse_amount(disc_m.group(0)) if disc_m else None,
        lines=claim_lines,
        parse_warnings=warnings,
    )


def _vision_parse(rendered: RenderedPDF) -> DeductionClaim:
    schema_hint = (
        "{"
        '"invoice_number": str|null,'
        '"po_number": str|null,'
        '"deduction_amount": number|null,'
        '"gross_invoice_amount": number|null,'
        '"net_invoice_amount": number|null,'
        '"discount_amount": number|null,'
        '"lines": ['
        '  {"upc": str|null, "description": str|null, "adj_qty": number|null,'
        '   "unit_price": number|null, "adj_amount": number|null,'
        '   "reason_code": str|null, "reason_text": str|null}'
        "]"
        "}"
    )
    imgs = rendered.image_paths()
    if not imgs:
        return DeductionClaim(
            source_path=str(rendered.source_path),
            pages=len(rendered.pages),
            extraction_method=ExtractionMethod.VISION_LLM,
            extraction_confidence=0.1,
            parse_warnings=["No page images rendered; vision extraction skipped."],
        )

    payload = extract_json_from_image(
        system_prompt=(
            "You are extracting structured data from a retailer deduction claim "
            "(Kroger/PRGX style). Find the Associated Deductions table and return "
            "every row. Use negative numbers for deduction amounts. The first page "
            "may be garbled; rely on the cleaner page(s)."
        ),
        schema_hint=schema_hint,
        image_paths=imgs,
        user_hint="Extract all claim lines. If unsure, return null rather than guessing.",
    )

    lines = [ClaimLine(**ln) for ln in payload.get("lines") or []]
    return DeductionClaim(
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=ExtractionMethod.VISION_LLM,
        extraction_confidence=0.7 if lines else 0.2,
        invoice_number=payload.get("invoice_number"),
        po_number=payload.get("po_number"),
        deduction_amount=payload.get("deduction_amount"),
        gross_invoice_amount=payload.get("gross_invoice_amount"),
        net_invoice_amount=payload.get("net_invoice_amount"),
        discount_amount=payload.get("discount_amount"),
        lines=lines,
    )


def extract_claim(rendered: RenderedPDF) -> DeductionClaim:
    # Tier 1: native text from pdfplumber.
    parsed = _text_parse(rendered)
    if parsed:
        _classify_all_lines(parsed, rendered.full_text)
        return parsed

    # Tier 2: Surya OCR over page images, same regex logic.
    rendered.ensure_ocr()
    if rendered.ocr_used:
        parsed_ocr = _text_parse(rendered)
        if parsed_ocr:
            parsed_ocr.extraction_method = ExtractionMethod.OCR_DETERMINISTIC
            parsed_ocr.extraction_confidence = min(0.85, parsed_ocr.extraction_confidence)
            parsed_ocr.parse_warnings.append("recovered via Surya OCR")
            _classify_all_lines(parsed_ocr, rendered.full_text)
            log.info("Deduction claim parsed via OCR tier.")
            return parsed_ocr

    # Tier 3: vision LLM.
    log.info("Deduction claim text + OCR parse insufficient; using vision fallback.")
    vparsed = _vision_parse(rendered)
    _classify_all_lines(vparsed, rendered.full_text)
    return vparsed


# ---------------------------------------------------------------------------
# Claim-type classifier.
#
# Per the case PDF, Curta handles many claim types: shortages, pricing,
# compliance fees (late delivery / routing), unsaleables, and "dozens of
# other reason codes". For the take-home scope we ship only the SHORTAGE
# rubric; the classifier tags every other claim so the decision layer can
# route it to NEEDS_HUMAN_REVIEW with a rubric-specific message.
#
# Design notes
# ------------
# 1. Kroger reason codes (numeric) are authoritative when present. Codes
#    2/4/5/6/72 are shortages in Kroger's taxonomy. We resolve by code
#    first because Kroger labels are compound — e.g. code 4's official
#    text is "Shortage/Damage/Do Not Stock", which contains the word
#    "damage" but is categorically a shortage code.
# 2. If the code is missing or ambiguous, we fall back to narrative
#    keyword matching with a "shortage beats everything" tiebreaker —
#    any mention of shortage/short-shipped/case short wins because that
#    is the only rubric we currently implement and false-positives on
#    SHORTAGE are safer than false-negatives (we'd run the rubric on a
#    bona-fide shortage claim either way).
# 3. If nothing matches, we return UNKNOWN (not OTHER). The decision
#    layer treats UNKNOWN as "classify-unable, human please look" which
#    is distinct from "we know it's not a shortage, please look".
# ---------------------------------------------------------------------------

# Kroger numeric reason codes → claim types. Source: the case-PDF
# glossary calls out codes 4 and 6 as shortages ("Shortage/Damage/Do Not
# Stock" and "Item invoiced/Not received"). The rest are Kroger's
# published taxonomy; we only key on codes we've seen or can document.
_KROGER_CODE_TO_TYPE: dict[str, ClaimType] = {
    "2": ClaimType.SHORTAGE,    # "Shortage (pre-receiving)"
    "4": ClaimType.SHORTAGE,    # "Shortage/Damage/Do Not Stock"
    "5": ClaimType.SHORTAGE,    # "Shortage/Damage"
    "6": ClaimType.SHORTAGE,    # "Item invoiced / Not received"
    "72": ClaimType.SHORTAGE,   # "Shortage (receiving)"
    "8": ClaimType.PRICING,     # "Price diff"
    "11": ClaimType.PRICING,    # "Cost variance"
    "18": ClaimType.PRICING,    # "Deal / billback pricing"
    "13": ClaimType.COMPLIANCE, # "Late delivery / OTIF"
    "14": ClaimType.COMPLIANCE, # "Routing violation"
    "16": ClaimType.COMPLIANCE, # "ASN / EDI failure"
    "9":  ClaimType.UNSALEABLES,  # "Damaged / unsaleable"
    "10": ClaimType.UNSALEABLES,  # "Expired"
}

# Narrative-text patterns. Order matters: shortage wins over damage wins
# over compliance. Each pattern carries a rationale string surfaced in
# the reasoning trace.
_NARRATIVE_PATTERNS: list[tuple[ClaimType, re.Pattern[str], str]] = [
    (
        ClaimType.SHORTAGE,
        re.compile(
            r"\b(shortage|short[\s-]?shipped|short[\s-]?ship|case[s]?\s+short|"
            r"item\s+invoiced\s*/?\s*not\s+received|not\s+received|"
            r"qty\s+short|under[\s-]?ship|missing\s+case)",
            re.I,
        ),
        "narrative contains shortage-family keyword",
    ),
    (
        ClaimType.PRICING,
        re.compile(
            r"\b(pricing|price\s+diff|cost\s+variance|cost\s+diff|overcharg|"
            r"billed?\s+price|incorrect\s+price|price\s+error|unit\s+price\s+mismatch)",
            re.I,
        ),
        "narrative contains pricing-family keyword",
    ),
    (
        ClaimType.COMPLIANCE,
        re.compile(
            r"\b(orad\s+late|late\s+delivery|on[\s-]?time\s+in[\s-]?full|otif|"
            r"routing\s+violation|asn\s+(fail|error)|edi\s+(fail|error)|"
            r"compliance\s+fee|late\s+ship|missed\s+appointment)",
            re.I,
        ),
        "narrative contains compliance/OTIF keyword",
    ),
    (
        ClaimType.UNSALEABLES,
        re.compile(
            r"\b(unsaleab|damaged?\s+goods|expired|spoil|broken\s+case|"
            r"product\s+damage)",
            re.I,
        ),
        "narrative contains unsaleables/damage keyword",
    ),
]


def classify_claim_type(
    line: ClaimLine,
    document_text: str | None = None,
) -> tuple[ClaimType, float, str]:
    """
    Classify a single claim line into a rubric family.

    Returns `(claim_type, confidence, rationale)`. The decision layer
    consumes all three so the UI and reasoning trace can explain exactly
    why the line is or isn't being run through the shortage rubric.

    Resolution order:
      1. Numeric reason code (highest confidence — it's the retailer's
         own taxonomy).
      2. Narrative text on the line (reason_text + description).
      3. Surrounding document text (e.g. for narrative-only deduction
         invoices that have no per-line reason codes).
      4. UNKNOWN with a flag for human review.
    """
    reason_code = (line.reason_code or "").strip()
    if reason_code and reason_code in _KROGER_CODE_TO_TYPE:
        t = _KROGER_CODE_TO_TYPE[reason_code]
        return t, 0.92, f"Kroger reason code {reason_code} → {t.value}"

    haystack = " ".join(
        filter(None, [line.reason_text, line.description, document_text or ""])
    )
    if not haystack.strip():
        return (
            ClaimType.UNKNOWN,
            0.0,
            "no reason code, no narrative text — classifier cannot decide",
        )

    for ctype, pattern, rationale in _NARRATIVE_PATTERNS:
        m = pattern.search(haystack)
        if m:
            # Narrative matches are lower-confidence than reason codes
            # because compound Kroger labels like "Shortage/Damage/Do
            # Not Stock" can legitimately trigger multiple patterns; the
            # ordering above (shortage first) prevents misclassification
            # of genuine shortages but we signal lower certainty.
            confidence = 0.75
            if line.reason_text and pattern.search(line.reason_text):
                confidence = 0.82
            rationale = f"{rationale} — matched {m.group(0)!r}"
            return ctype, confidence, rationale

    return (
        ClaimType.UNKNOWN,
        0.15,
        "reason code and narrative keywords did not match any known family",
    )


def _classify_all_lines(claim: DeductionClaim, document_text: str | None) -> None:
    """Tag every line on a parsed claim with its rubric family."""
    for ln in claim.lines:
        ctype, conf, rationale = classify_claim_type(ln, document_text)
        ln.claim_type = ctype
        ln.claim_type_confidence = conf
        ln.claim_type_rationale = rationale
        log.info(
            "Claim line %s classified as %s (confidence=%.2f): %s",
            ln.upc or "(no-upc)",
            ctype.value,
            conf,
            rationale,
        )
