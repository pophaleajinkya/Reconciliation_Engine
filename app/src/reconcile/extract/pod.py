"""
Proof of Delivery extractor.

PODs are normally image-only scans. We use a three-tier approach:

1. Native PDF text (rare, but e.g. some EDI-generated PODs have it).
2. **Surya OCR** over page images, then regex over the reconstructed text —
   receiving stamps frequently include crisp printed labels like
   `Total # of Cases on BOL 4210` and `TOTAL # Of Cases RECVD 4200`, which
   Surya reads well and we can parse deterministically.
3. Vision LLM — last resort, and always used for handwriting/signature
   narrative extraction.
"""

from __future__ import annotations

import logging
import re

from reconcile.ingest.renderer import RenderedPDF
from reconcile.llm.groq_client import extract_json_from_image
from reconcile.schemas import ExtractionMethod, ProofOfDelivery, ReceivingEvidence

log = logging.getLogger("reconcile.extract.pod")


# Kroger-style stamp patterns — tolerant of OCR artefacts (e.g. "OTY" for QTY,
# missing colons, stray punctuation).  We intentionally keep these loose;
# matching is best-effort and evidence still gets confirmed by the vision LLM
# when it runs.
_SHIPPED_LABEL = r"(?:Total\s*#\s*of\s*Cases?\s*on\s*BOL|CASES\s*SHIPPED|CASES\s*ON\s*BOL)"
_RECEIVED_LABEL = r"(?:TOTAL\s*#?\s*O?f?\s*Cases?\s*REC[VD']*D?|CASES\s*RECEIVED|REC[VD']?VD)"

# Numbers can appear either right after the label or on the line(s) immediately
# before/after (Kroger-style stamps lay this out as a 2-column grid and OCR
# linearizes it in reading order).
_SHIPPED_RE = re.compile(rf"{_SHIPPED_LABEL}\D{{0,30}}(\d{{2,6}})", re.IGNORECASE)
_SHIPPED_BEFORE_RE = re.compile(rf"(\d{{2,6}})\s*\n[^\n]{{0,20}}{_SHIPPED_LABEL}", re.IGNORECASE)
_RECEIVED_RE = re.compile(rf"{_RECEIVED_LABEL}\D{{0,30}}(\d{{2,6}})", re.IGNORECASE)
_RECEIVED_BEFORE_RE = re.compile(rf"(\d{{2,6}})\s*\n[^\n]{{0,20}}{_RECEIVED_LABEL}", re.IGNORECASE)
# Only match "Short N" / "Short: N" on the same line — underscores are often
# just blank-fill placeholders in the stamp and we don't want to grab the next
# line's number by mistake.
_SHORT_RE = re.compile(r"Short[ \t]*[:\-]?[ \t]*(\d{1,6})\b", re.IGNORECASE)
_OVER_RE = re.compile(r"Over[ \t]*[:\-]?[ \t]*(\d{1,6})\b", re.IGNORECASE)
_PO_RE = re.compile(r"(?:PO|Customer\s*PO)\s*#?\s*[:\-]?\s*(\d{4,10})", re.IGNORECASE)
_BOL_RE = re.compile(r"BOL\s*(?:NUMBER|#|No)?\s*[:\-]?\s*(\d{4,12})", re.IGNORECASE)


def _parse_pod_from_text(text: str) -> ReceivingEvidence | None:
    """Return a ReceivingEvidence if the text contains clear numeric totals."""
    if not text:
        return None
    shipped = _match_int(_SHIPPED_RE, text) or _match_int(_SHIPPED_BEFORE_RE, text)
    received = _match_int(_RECEIVED_RE, text) or _match_int(_RECEIVED_BEFORE_RE, text)
    if shipped is None and received is None:
        return None
    short = _match_int(_SHORT_RE, text)
    over = _match_int(_OVER_RE, text)

    aggregate_shortage = False
    if shipped is not None and received is not None and received < shipped:
        aggregate_shortage = True
    elif short and short > 0:
        aggregate_shortage = True

    notes_parts: list[str] = []
    if shipped is not None:
        notes_parts.append(f"Cases on BOL: {shipped}")
    if received is not None:
        notes_parts.append(f"Cases received: {received}")
    if short:
        notes_parts.append(f"Short: {short}")
    if over:
        notes_parts.append(f"Over: {over}")

    return ReceivingEvidence(
        has_receiving_stamp=True,
        stamp_notes="; ".join(notes_parts) or None,
        total_cases_shipped=shipped,
        total_cases_received=received,
        aggregate_shortage=aggregate_shortage,
    )


def _match_int(pattern: re.Pattern[str], text: str) -> int | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None


def extract_pod(rendered: RenderedPDF) -> ProofOfDelivery:
    # Tier 1 + 2 rolled together: get the best available text (native if
    # present, otherwise fall back to Surya OCR) and parse deterministically.
    text = rendered.full_text
    if not text.strip() or len(text.strip()) < 80:
        # PODs usually have the receiving stamp on page 1 or the last page.
        # Surya on a 3-page POD costs ~3 × 60 s on CPU, so scope to those two
        # pages first and only expand to the middle pages if we still don't
        # see any stamp numbers.
        n = len(rendered.pages)
        first_and_last = sorted({1, n}) if n else [1]
        rendered.ensure_ocr(pages=first_and_last)
        text = rendered.full_text
        if not _parse_pod_from_text(text) and n > 2:
            # Fall back to full OCR if the stamp wasn't on the edges.
            rendered.ensure_ocr()
            text = rendered.full_text

    deterministic = _parse_pod_from_text(text)
    po_m = _PO_RE.search(text)
    bol_m = _BOL_RE.search(text)

    if deterministic and (deterministic.total_cases_shipped or deterministic.total_cases_received):
        method = (
            ExtractionMethod.OCR_DETERMINISTIC
            if rendered.ocr_used
            else ExtractionMethod.TEXT_DETERMINISTIC
        )
        log.info(
            "POD parsed deterministically via %s: shipped=%s received=%s",
            method.value,
            deterministic.total_cases_shipped,
            deterministic.total_cases_received,
        )
        return ProofOfDelivery(
            source_path=str(rendered.source_path),
            pages=len(rendered.pages),
            extraction_method=method,
            extraction_confidence=0.75,
            referenced_bol=bol_m.group(1) if bol_m else None,
            referenced_po=po_m.group(1) if po_m else None,
            receiving=deterministic,
        )

    # Tier 3: vision LLM — still needed for handwriting and signatures.
    schema_hint = (
        "{"
        '"referenced_bol": str|null,'
        '"referenced_po": str|null,'
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
        return ProofOfDelivery(
            source_path=str(rendered.source_path),
            pages=len(rendered.pages),
            extraction_method=ExtractionMethod.VISION_LLM,
            extraction_confidence=0.1,
            parse_warnings=["No page images; vision skipped."],
        )

    payload = extract_json_from_image(
        system_prompt=(
            "You are reading a Proof of Delivery (often a scanned, stamped BOL). "
            "Extract: receiving stamp/signature presence, any date/time, "
            "total cases shipped vs received (numeric), any handwritten "
            "shortage/damage notes (quote them), and whether the shortage is "
            "aggregate (totals mismatch) or line-level (specific items called out)."
        ),
        schema_hint=schema_hint,
        image_paths=imgs,
        user_hint="Quote handwriting verbatim in stamp_notes; do not interpret.",
    )

    recv = payload.get("receiving") or {}
    receiving = ReceivingEvidence(**recv) if recv else ReceivingEvidence()

    return ProofOfDelivery(
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=ExtractionMethod.VISION_LLM,
        extraction_confidence=0.7 if receiving.has_receiving_stamp else 0.4,
        referenced_bol=payload.get("referenced_bol"),
        referenced_po=payload.get("referenced_po"),
        receiving=receiving,
    )
