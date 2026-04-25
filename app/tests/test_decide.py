"""Unit tests for the shortage decision engine (no LLM, no PDFs)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reconcile.decide.shortage import decide_line
from reconcile.schemas import (
    BillOfLading,
    ClaimLine,
    Decision,
    DeductionClaim,
    ExtractionMethod,
    InvoiceLine,
    MatchedCase,
    MatchedClaimLine,
    ProofOfDelivery,
    ReceivingEvidence,
    SalesInvoice,
    ShippedLine,
)


def _stub_case(
    inv_line: InvoiceLine,
    claim: ClaimLine,
    bol_cases: float | None = None,
    aggregate_shipped: float | None = None,
    aggregate_received: float | None = None,
) -> tuple[MatchedCase, MatchedClaimLine]:
    invoice = SalesInvoice(
        source_path="/tmp/inv.pdf",
        pages=1,
        extraction_method=ExtractionMethod.TEXT_DETERMINISTIC,
        invoice_number="0090000001",
        lines=[inv_line],
    )
    bol_lines = []
    if bol_cases is not None:
        bol_lines = [
            ShippedLine(
                material_number=inv_line.material_number,
                cases=bol_cases,
            )
        ]
    bol = BillOfLading(
        source_path="/tmp/bol.pdf",
        pages=1,
        extraction_method=ExtractionMethod.TEXT_DETERMINISTIC,
        lines=bol_lines,
    )
    pod = None
    if aggregate_shipped is not None:
        pod = ProofOfDelivery(
            source_path="/tmp/pod.pdf",
            pages=1,
            extraction_method=ExtractionMethod.VISION_LLM,
            receiving=ReceivingEvidence(
                has_receiving_stamp=True,
                total_cases_shipped=aggregate_shipped,
                total_cases_received=aggregate_received,
                aggregate_shortage=(aggregate_received or 0) < aggregate_shipped,
            ),
        )
    claim_doc = DeductionClaim(
        source_path="/tmp/claim.pdf",
        pages=1,
        extraction_method=ExtractionMethod.TEXT_DETERMINISTIC,
        invoice_number="0090000001",
        lines=[claim],
    )
    matched_line = MatchedClaimLine(
        claim=claim,
        matched_invoice_line=inv_line,
        matched_invoice_line_score=0.95,
        matched_bol_lines=bol_lines,
    )
    case = MatchedCase(
        case_name="stub",
        invoice=invoice,
        bol=bol,
        pod=pod,
        claim=claim_doc,
        claim_lines=[matched_line],
    )
    return case, matched_line


def test_valid_line_level_shortage():
    inv_line = InvoiceLine(
        line_no="200",
        material_number="05278",
        description="NUT THINS ALM LOW SOD",
        quantity=66,
        unit_label="EA",
        unit_price=31.44,
        gross_value=2075.04,
        off_invoice_promo=-47.52,  # net unit = 31.44 - 47.52/66 ≈ 30.72
    )
    claim = ClaimLine(
        upc="1004157005278",
        adj_qty=66,
        unit_price=30.72,
        adj_amount=-2027.52,
        reason_code="6",
    )
    case, line = _stub_case(inv_line, claim, bol_cases=0)
    d = decide_line(case, line, 0)
    assert d.decision == Decision.VALID
    assert d.confidence >= 0.8


def test_invalid_when_full_qty_received():
    inv_line = InvoiceLine(
        material_number="05278",
        quantity=66,
        unit_label="EA",
        unit_price=31.44,
        gross_value=2075.04,
        off_invoice_promo=-47.52,
    )
    claim = ClaimLine(
        upc="1004157005278",
        adj_qty=66,
        unit_price=30.72,
        adj_amount=-2027.52,
    )
    case, line = _stub_case(inv_line, claim, bol_cases=66)
    d = decide_line(case, line, 0)
    assert d.decision == Decision.INVALID


def test_needs_review_when_aggregate_only():
    inv_line = InvoiceLine(
        material_number="11070",
        quantity=627,
        unit_label="EA",
        unit_price=67.02,
        off_invoice_promo=-376.20,
    )
    claim = ClaimLine(
        upc="1004157011070",
        adj_qty=10,
        unit_price=66.42,
        adj_amount=-664.20,
    )
    case, line = _stub_case(
        inv_line, claim, aggregate_shipped=4210, aggregate_received=4200
    )
    d = decide_line(case, line, 0)
    assert d.decision == Decision.NEEDS_HUMAN_REVIEW


def test_needs_review_when_no_receiving_evidence():
    inv_line = InvoiceLine(
        material_number="05278",
        quantity=66,
        unit_label="EA",
        unit_price=31.44,
    )
    claim = ClaimLine(
        upc="1004157005278",
        adj_qty=66,
        unit_price=30.72,
        adj_amount=-2027.52,
    )
    case, line = _stub_case(inv_line, claim)
    d = decide_line(case, line, 0)
    assert d.decision == Decision.NEEDS_HUMAN_REVIEW


if __name__ == "__main__":
    test_valid_line_level_shortage()
    test_invalid_when_full_qty_received()
    test_needs_review_when_aggregate_only()
    test_needs_review_when_no_receiving_evidence()
    print("all ok")
