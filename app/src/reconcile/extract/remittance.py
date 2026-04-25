"""Remittance advice extractor (deterministic over the tabular ACH format)."""

from __future__ import annotations

import logging
import re

from reconcile.ingest.renderer import RenderedPDF
from reconcile.schemas import (
    ExtractionMethod,
    RemittanceAdvice,
    RemittanceLine,
)

log = logging.getLogger("reconcile.extract.remittance")


_REMIT_ROW_RE = re.compile(
    r"""
    ^\s*
    \d{5}\s+                                  # line number (e.g. 00001)
    (?P<inv>\S+)\s+
    \$?(?P<inv_amt>[\d,]+\.\d{2})(?P<inv_neg>-?)\s+
    \$?(?P<net>[\d,]+\.\d{2})(?P<net_neg>-?)\s*
    (?:\s+[A-Z]\b)?                           # trailing marker like "D"
    \s*$
    """,
    re.VERBOSE | re.MULTILINE,
)

_DETAIL_TERMS_RE = re.compile(
    r"SELLER INVOICE NUM:\s*(\S+).*?TERMS DISC AMT:\s*\$?([\d,.]+)",
    re.S,
)

_EFFECTIVE_DATE_RE = re.compile(r"EFFECTIVE\s+DATE:\s*([\d/]+)", re.I)
_ORIGINATOR_RE = re.compile(r"ORIGINATOR:\s*(\S+)", re.I)


def _parse_terms_disc(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in _DETAIL_TERMS_RE.finditer(text):
        inv = m.group(1).strip()
        amt = float(m.group(2).replace(",", ""))
        out[inv] = amt
    return out


def _parse_remittance_from_text(text: str) -> tuple[list[RemittanceLine], str | None, str | None]:
    terms = _parse_terms_disc(text)
    lines: list[RemittanceLine] = []
    for m in _REMIT_ROW_RE.finditer(text):
        inv = m.group("inv")
        inv_amt = float(m.group("inv_amt").replace(",", ""))
        net = float(m.group("net").replace(",", ""))
        if m.group("inv_neg") == "-":
            inv_amt = -abs(inv_amt)
        if m.group("net_neg") == "-":
            net = -abs(net)
        is_cm = "-CM" in inv
        lines.append(
            RemittanceLine(
                seller_invoice_num=inv,
                is_credit_memo=is_cm,
                invoice_amount=inv_amt,
                net_amount_paid=net,
                terms_discount=terms.get(inv),
            )
        )
    eff_m = _EFFECTIVE_DATE_RE.search(text)
    orig_m = _ORIGINATOR_RE.search(text)
    return lines, (eff_m.group(1) if eff_m else None), (orig_m.group(1) if orig_m else None)


def extract_remittance(rendered: RenderedPDF) -> RemittanceAdvice:
    text = rendered.full_text
    lines, effective, originator = _parse_remittance_from_text(text)
    method = ExtractionMethod.TEXT_DETERMINISTIC
    warnings: list[str] = []

    # Tier 2: OCR fallback if nothing was found in native text.
    if not lines:
        rendered.ensure_ocr()
        if rendered.ocr_used:
            text = rendered.full_text
            lines, effective, originator = _parse_remittance_from_text(text)
            if lines:
                method = ExtractionMethod.OCR_DETERMINISTIC
                warnings.append("recovered via Surya OCR")

    return RemittanceAdvice(
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=method,
        extraction_confidence=0.9 if lines else 0.2,
        effective_date=effective,
        originator=originator,
        lines=lines,
        parse_warnings=warnings,
    )
