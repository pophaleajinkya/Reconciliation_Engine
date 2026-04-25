"""Dispatches a classified, rendered PDF to the right extractor."""

from __future__ import annotations

from reconcile.extract.bol import extract_bol
from reconcile.extract.claim import extract_claim
from reconcile.extract.invoice import extract_sales_invoice
from reconcile.extract.pod import extract_pod
from reconcile.extract.remittance import extract_remittance
from reconcile.ingest.classifier import Classification
from reconcile.ingest.renderer import RenderedPDF
from reconcile.schemas import BaseDocument, DocType, ExtractionMethod


def extract(rendered: RenderedPDF, cls: Classification) -> BaseDocument | None:
    dt = cls.doc_type
    if dt == DocType.SALES_INVOICE:
        return extract_sales_invoice(rendered)
    if dt == DocType.BILL_OF_LADING:
        return extract_bol(rendered)
    if dt == DocType.PROOF_OF_DELIVERY:
        return extract_pod(rendered)
    if dt == DocType.REMITTANCE_ADVICE:
        return extract_remittance(rendered)
    if dt == DocType.DEDUCTION_CLAIM:
        return extract_claim(rendered)

    # UNKNOWN: return a stub so the pipeline still reports visibility.
    return BaseDocument(
        doc_type=DocType.UNKNOWN,
        source_path=str(rendered.source_path),
        pages=len(rendered.pages),
        extraction_method=ExtractionMethod.TEXT_DETERMINISTIC,
        extraction_confidence=0.0,
        parse_warnings=[f"Unclassified: {cls.reason}"],
    )
