"""Build a CaseReport from a MatchedCase + its line decisions."""

from __future__ import annotations

from reconcile.schemas import CaseReport, DocType, LineDecision, MatchedCase


def build_report(
    case: MatchedCase,
    case_path: str,
    decisions: list[LineDecision],
) -> CaseReport:
    seen: list[str] = []
    for doc in [case.invoice, case.bol, case.pod, case.remittance, case.claim]:
        if doc is not None:
            seen.append(f"{doc.doc_type.value} :: {doc.source_path}")

    expected = {
        DocType.SALES_INVOICE,
        DocType.BILL_OF_LADING,
        DocType.REMITTANCE_ADVICE,
        DocType.DEDUCTION_CLAIM,
    }
    present = {
        case.invoice.doc_type if case.invoice else None,
        case.bol.doc_type if case.bol else None,
        case.remittance.doc_type if case.remittance else None,
        case.claim.doc_type if case.claim else None,
    }
    missing = [dt.value for dt in expected if dt not in present]

    total_claimed = (
        case.claim.deduction_amount if case.claim and case.claim.deduction_amount else None
    )

    return CaseReport(
        case_name=case.case_name,
        case_path=case_path,
        invoice_number=(
            case.invoice.invoice_number
            if case.invoice and case.invoice.invoice_number
            else (case.claim.invoice_number if case.claim else None)
        ),
        total_deduction_claimed=total_claimed,
        documents_seen=seen,
        documents_missing=missing,
        line_decisions=decisions,
        global_warnings=case.global_warnings,
    )
