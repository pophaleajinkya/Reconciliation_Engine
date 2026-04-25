"""
Entity resolution.

Given the canonical documents for a case, this module links:
  - the deduction claim invoice number to the sales invoice
  - each claim line (UPC) to an invoice line (material number) using a crosswalk
  - each claim line to BOL lines (material or customer SKU)
  - the claim to the right remittance CM line

The matcher does not make business decisions; it only produces
`MatchedClaimLine` rows with notes for the decision engine.
"""

from __future__ import annotations

import logging

from reconcile.schemas import (
    BillOfLading,
    ClaimLine,
    DeductionClaim,
    InvoiceLine,
    MatchedCase,
    MatchedClaimLine,
    ProofOfDelivery,
    RemittanceAdvice,
    RemittanceLine,
    SalesInvoice,
    ShippedLine,
)

log = logging.getLogger("reconcile.match")


def _upc_suffix_matches_material(upc: str | None, material: str | None) -> str | None:
    """
    Blue Diamond UPCs embed the material number as a suffix. We've observed two
    variants in the wild:

    * Full 5-digit match:   UPC 1004157005278 ↔ material 05278
    * Last-4 match:         UPC 100415701070  ↔ material 11070
      (The leading digit of the material is a pack-count prefix that some
      retailers drop when printing the UPC on deduction invoices.)

    Returns a short human-readable label describing which rule matched, or
    None if no match.
    """
    if not upc or not material:
        return None
    digits_upc = "".join(ch for ch in upc if ch.isdigit())
    digits_mat = "".join(ch for ch in material if ch.isdigit())
    if not digits_upc or not digits_mat:
        return None
    mat5 = digits_mat.zfill(5)[-5:]
    if digits_upc.endswith(mat5):
        return f"UPC suffix={mat5} == material {digits_mat}"
    mat4 = digits_mat.zfill(5)[-4:]
    if digits_upc.endswith(mat4):
        return f"UPC last-4={mat4} matches material {digits_mat} (pack-prefix dropped)"
    return None


def _score_invoice_match(claim: ClaimLine, inv_line: InvoiceLine) -> tuple[float, list[str]]:
    score = 0.0
    notes: list[str] = []

    upc_match = _upc_suffix_matches_material(claim.upc, inv_line.material_number)
    if upc_match:
        # Full 5-digit match is stronger than last-4 (which can be ambiguous
        # across the catalog). The caller resolves ambiguity via price.
        score += 0.7 if "==" in upc_match else 0.45
        notes.append(upc_match)
    if claim.unit_price is not None and inv_line.unit_price is not None:
        if abs(claim.unit_price - inv_line.unit_price) < 0.01:
            score += 0.15
            notes.append("claim unit price == invoice gross unit price")
        elif (
            inv_line.net_unit_price is not None
            and abs(claim.unit_price - inv_line.net_unit_price) < 0.02
        ):
            score += 0.3
            notes.append(
                f"claim unit price ${claim.unit_price:.2f} matches "
                f"invoice net-of-promo unit price ${inv_line.net_unit_price:.2f}"
            )
    if claim.description and inv_line.description:
        cw = set(_normalize_desc(claim.description).split())
        iw = set(_normalize_desc(inv_line.description).split())
        overlap = len(cw & iw)
        if overlap >= 2:
            score += 0.05
            notes.append(f"description overlap={overlap}")
    return min(score, 1.0), notes


_DESC_NORMALIZE_CHARS = str.maketrans({"&": " ", "-": " ", "/": " ", ",": " ", ".": " "})


def _normalize_desc(s: str) -> str:
    return s.upper().translate(_DESC_NORMALIZE_CHARS)


def _find_invoice_line(
    claim: ClaimLine, invoice: SalesInvoice | None
) -> tuple[InvoiceLine | None, float, list[str]]:
    if not invoice or not invoice.lines:
        return None, 0.0, ["no sales invoice extracted"]
    scored = [(ln, *_score_invoice_match(claim, ln)) for ln in invoice.lines]
    scored.sort(key=lambda t: t[1], reverse=True)
    best, score, notes = scored[0]
    if score < 0.3:
        return None, score, ["no confident invoice-line match"]
    return best, score, notes


def _find_bol_lines(
    claim: ClaimLine, invoice_line: InvoiceLine | None, bol: BillOfLading | None
) -> list[ShippedLine]:
    if not bol or not bol.lines:
        return []
    mat = invoice_line.material_number if invoice_line else None
    hits: list[ShippedLine] = []
    for ln in bol.lines:
        # Lines extracted from a different-shipment page are kept on the
        # BOL document for UI transparency, but must NOT influence
        # matching / qty-received math for this case.
        if not ln.belongs_to_primary_shipment:
            continue
        if mat and ln.material_number and ln.material_number == mat:
            hits.append(ln)
        elif _upc_suffix_matches_material(claim.upc, ln.material_number) is not None:
            hits.append(ln)
    return hits


def _find_remittance_cm(
    claim_amount: float | None,
    invoice_number: str | None,
    remit: RemittanceAdvice | None,
) -> RemittanceLine | None:
    if not remit or not remit.lines:
        return None
    for ln in remit.lines:
        if not ln.is_credit_memo:
            continue
        if invoice_number and ln.seller_invoice_num and invoice_number in ln.seller_invoice_num:
            return ln
    # Fall back to amount match.
    if claim_amount is not None:
        target = -abs(claim_amount)
        for ln in remit.lines:
            if not ln.is_credit_memo:
                continue
            amt = ln.invoice_amount
            if amt is not None and abs(amt - target) < 0.01:
                return ln
    return None


def build_matched_case(
    case_name: str,
    *,
    invoice: SalesInvoice | None,
    bol: BillOfLading | None,
    pod: ProofOfDelivery | None,
    remittance: RemittanceAdvice | None,
    claim: DeductionClaim | None,
) -> MatchedCase:
    warnings: list[str] = []

    # Cross-check that invoice numbers agree where we have them.
    inv_numbers = {
        "invoice": invoice.invoice_number if invoice else None,
        "claim": claim.invoice_number if claim else None,
    }
    if inv_numbers["invoice"] and inv_numbers["claim"]:
        a = inv_numbers["invoice"].lstrip("0")
        b = inv_numbers["claim"].lstrip("0")
        if a != b:
            warnings.append(
                f"Invoice number mismatch: invoice={inv_numbers['invoice']} vs "
                f"claim={inv_numbers['claim']}"
            )

    if bol and bol.content_belongs_to_different_shipment:
        warnings.append(
            "BOL content flagged as possibly including another shipment; "
            "receiving evidence should be treated cautiously."
        )

    matched_lines: list[MatchedClaimLine] = []
    if claim and claim.lines:
        for c in claim.lines:
            inv_line, inv_score, inv_notes = _find_invoice_line(c, invoice)
            bol_lines = _find_bol_lines(c, inv_line, bol)
            remit_line = _find_remittance_cm(
                c.adj_amount, claim.invoice_number if claim else None, remittance
            )
            notes = list(inv_notes)
            if bol_lines:
                notes.append(f"BOL match: {len(bol_lines)} line(s) via material {inv_line.material_number if inv_line else '?'}")
            else:
                notes.append("no BOL line matched for this material")
            if remit_line:
                notes.append(f"matched remittance CM {remit_line.seller_invoice_num}")
            matched_lines.append(
                MatchedClaimLine(
                    claim=c,
                    matched_invoice_line=inv_line,
                    matched_invoice_line_score=round(inv_score, 3),
                    matched_bol_lines=bol_lines,
                    matched_remittance_line=remit_line,
                    match_notes=notes,
                )
            )

    return MatchedCase(
        case_name=case_name,
        invoice=invoice,
        bol=bol,
        pod=pod,
        remittance=remittance,
        claim=claim,
        claim_lines=matched_lines,
        global_warnings=warnings,
    )
