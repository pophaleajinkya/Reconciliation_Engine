"""
Sales invoice extractor.

The Blue Diamond invoices in this bundle have a stable textual layout:

    10  11023 RSTD SLTD LOW SOD 12/12-1.5 OZ TUBE  4 EA  115.20  460.80 USD
    HS No: ...
    Off Invoice Pro Qty  46.08- USD
    20  11069 WHOLE NAT 6-25 OZ S BAG  80 EA  58.26  4,660.80 USD

so we parse them deterministically. If the deterministic pass yields no
lines (e.g. a different template), we fall back to the LLM.
"""

from __future__ import annotations

import logging
import re

from reconcile.extract.base import parse_amount
from reconcile.extract.invoice_edi import looks_like_edi_invoic, parse_edi_invoic
from reconcile.ingest.renderer import RenderedPDF
from reconcile.llm.groq_client import extract_json_from_text
from reconcile.schemas import (
    ExtractionMethod,
    InvoiceLine,
    SalesInvoice,
)

log = logging.getLogger("reconcile.extract.invoice")


# pdfplumber sometimes drops spaces between tokens ("DocumentNumber" instead
# of "Document Number"), so all header patterns use \s* between words.
_HEADER_PATTERNS = {
    "invoice_number": re.compile(r"Document\s*Number\s+(\d+)", re.I),
    "invoice_date": re.compile(r"Document\s*Date\s+([\d/]+)", re.I),
    "po_number": re.compile(r"Purchase\s*Order\s+(\S+)", re.I),
    "delivery_number": re.compile(r"Delivery\s*Number\s+(\S+)", re.I),
    "terms_of_payment": re.compile(r"Terms\s*of\s*Payment\s+(\S.*)", re.I),
    "carrier": re.compile(r"(?<!\S)Carrier\s+(\S.*)", re.I),
}

_SUBTOTAL_RE = re.compile(r"Subtotal\s+([\d,]+\.\d{2})", re.I)
_TOTAL_RE = re.compile(r"Total\s*Amount\s+([\d,]+\.\d{2})", re.I)

# Line item. pdfplumber outputs look like:
#   "10 11023RSTDSLTDLOWSOD12/12-1.5OZTUBE 4EA 115.20 460.80 USD"
# i.e. material number is glued to the description, qty is glued to EA.
_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<line_no>\d{2,4})\s+
    (?P<material>\d{4,6})
    (?P<desc>[^\n]*?)\s+
    (?P<qty>\d+(?:\.\d+)?)\s*EA\s+
    (?P<price>[\d,]+\.\d{2})\s+
    (?P<value>[\d,]+\.\d{2})\s+USD\s*$
    """,
    re.VERBOSE | re.MULTILINE,
)

_PROMO_RE = re.compile(
    r"Off\s*Invoice\s*Pro\s*Qty\s+([\d,]+\.\d{2})-\s*USD", re.I
)


def _parse_header(text: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for key, pat in _HEADER_PATTERNS.items():
        m = pat.search(text)
        out[key] = m.group(1).strip() if m else None
    return out


def _parse_lines(text: str) -> list[InvoiceLine]:
    """Parse line items and attach the immediately-following Off Invoice Pro promo."""
    lines: list[InvoiceLine] = []

    # Normalize tabs so the regex anchors work.
    norm = re.sub(r"[\t ]+", " ", text)

    # Find all line item matches with positions so we can scan forward for promos.
    matches = list(_LINE_RE.finditer(norm))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(norm)
        window = norm[start:end]
        promo_m = _PROMO_RE.search(window)
        promo_amt = float(promo_m.group(1).replace(",", "")) if promo_m else None

        qty = float(m.group("qty"))
        price = float(m.group("price").replace(",", ""))
        value = float(m.group("value").replace(",", ""))
        lines.append(
            InvoiceLine(
                line_no=m.group("line_no"),
                material_number=m.group("material"),
                description=m.group("desc").strip(),
                quantity=qty,
                unit_label="EA",
                unit_price=price,
                gross_value=value,
                off_invoice_promo=-promo_amt if promo_amt else None,
            )
        )
    return lines


def _llm_fallback(rendered: RenderedPDF) -> SalesInvoice:
    schema_hint = (
        "{"
        '"invoice_number": str|null,'
        '"invoice_date": str|null,'
        '"po_number": str|null,'
        '"delivery_number": str|null,'
        '"bill_to": str|null,'
        '"ship_to": str|null,'
        '"carrier": str|null,'
        '"terms_of_payment": str|null,'
        '"subtotal": number|null,'
        '"total_amount": number|null,'
        '"lines": ['
        '  {"line_no": str|null, "material_number": str|null, "description": str|null,'
        '   "quantity": number|null, "unit_label": str|null, "unit_price": number|null,'
        '   "gross_value": number|null, "off_invoice_promo": number|null}'
        "]"
        "}"
    )
    payload = extract_json_from_text(
        system_prompt=(
            "You are extracting line-item data from a CPG sales invoice. "
            "Include EVERY line item. Promo amounts are negative numbers."
        ),
        user_text=rendered.full_text[:20000],
        schema_hint=schema_hint,
    )
    lines = [InvoiceLine(**ln) for ln in payload.get("lines") or []]
    return SalesInvoice(
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=ExtractionMethod.TEXT_LLM,
        extraction_confidence=0.6 if lines else 0.3,
        invoice_number=payload.get("invoice_number"),
        invoice_date=payload.get("invoice_date"),
        po_number=payload.get("po_number"),
        delivery_number=payload.get("delivery_number"),
        bill_to=payload.get("bill_to"),
        ship_to=payload.get("ship_to"),
        carrier=payload.get("carrier"),
        terms_of_payment=payload.get("terms_of_payment"),
        subtotal=payload.get("subtotal"),
        total_amount=payload.get("total_amount"),
        lines=lines,
    )


def extract_sales_invoice(rendered: RenderedPDF) -> SalesInvoice:
    text = rendered.full_text

    # Tier 0: SAP EDI INVOIC02 (IDoc XML). Some retailers send the
    # invoice as a structured `.txt` IDoc rather than a PDF. The IDoc
    # segments are unambiguous, so when we detect the envelope we parse
    # it directly and bypass the PDF regex/OCR/LLM tiers entirely.
    if looks_like_edi_invoic(text):
        edi_invoice = parse_edi_invoic(text, source_path=rendered.source_path)
        if edi_invoice and edi_invoice.lines:
            log.info(
                "Invoice parsed as EDI INVOIC IDoc (%d lines).",
                len(edi_invoice.lines),
            )
            return edi_invoice
        log.info(
            "EDI INVOIC envelope detected but parse yielded no lines; "
            "falling back to PDF tiers."
        )

    hdr = _parse_header(text)
    lines = _parse_lines(text)

    # Tier 2: OCR over page images and retry the deterministic parser.
    if not lines:
        rendered.ensure_ocr()
        if rendered.ocr_used:
            text = rendered.full_text
            hdr = _parse_header(text)
            lines = _parse_lines(text)
            if lines:
                subtotal = parse_amount(_SUBTOTAL_RE.search(text).group(0) if _SUBTOTAL_RE.search(text) else None)
                total = parse_amount(_TOTAL_RE.search(text).group(0) if _TOTAL_RE.search(text) else None)
                log.info("Invoice recovered via OCR tier with %d lines.", len(lines))
                return SalesInvoice(
                    source_path=str(rendered.source_path),
                    pages=len(rendered.pages),
                    extraction_method=ExtractionMethod.OCR_DETERMINISTIC,
                    extraction_confidence=0.8,
                    invoice_number=hdr["invoice_number"],
                    invoice_date=hdr["invoice_date"],
                    po_number=hdr["po_number"],
                    delivery_number=hdr["delivery_number"],
                    carrier=hdr["carrier"],
                    terms_of_payment=hdr["terms_of_payment"],
                    lines=lines,
                    subtotal=subtotal,
                    total_amount=total,
                    parse_warnings=["recovered via Surya OCR"],
                )

    if not lines:
        log.info("Deterministic invoice parse found 0 lines; falling back to LLM.")
        return _llm_fallback(rendered)

    subtotal = parse_amount(_SUBTOTAL_RE.search(text).group(0) if _SUBTOTAL_RE.search(text) else None)
    total = parse_amount(_TOTAL_RE.search(text).group(0) if _TOTAL_RE.search(text) else None)

    return SalesInvoice(
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=ExtractionMethod.TEXT_DETERMINISTIC,
        extraction_confidence=0.95,
        invoice_number=hdr["invoice_number"],
        invoice_date=hdr["invoice_date"],
        po_number=hdr["po_number"],
        delivery_number=hdr["delivery_number"],
        carrier=hdr["carrier"],
        terms_of_payment=hdr["terms_of_payment"],
        lines=lines,
        subtotal=subtotal,
        total_amount=total,
    )
