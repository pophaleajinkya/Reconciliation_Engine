"""End-to-end pipeline: a case folder in, a CaseReport + artifacts out."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Document-level cache keyed by (sha256, doc_type). Lets repeat "Run
# Reconciliation" clicks on the same bundle skip OCR/vision entirely.
# Intentionally process-local: fresh interpreter = fresh cache.
_DOC_CACHE: dict[tuple[str, str], Any] = {}

from reconcile.config import SETTINGS
from reconcile.decide import decide_case
from reconcile.extract.dispatcher import extract
from reconcile.ingest.classifier import classify
from reconcile.ingest.document_loader import (
    DiscoveredFile,
    SkippedFile,
    discover_documents,
    load_document,
)
from reconcile.match.resolver import build_matched_case
from reconcile.report.builder import build_report
from reconcile.schemas import (
    BaseDocument,
    BillOfLading,
    CaseReport,
    DeductionClaim,
    DocType,
    ProofOfDelivery,
    RemittanceAdvice,
    SalesInvoice,
)

log = logging.getLogger("reconcile.pipeline")


def run_case(case_dir: Path, *, write_artifacts: bool = True) -> dict[str, Any]:
    """
    Process one case folder. Returns a dict with:
      - report: CaseReport (pydantic)
      - documents: list[BaseDocument]
      - classifications: list[{path, doc_type, confidence, reason, kind}]
      - skipped: list[{path, reason}]
      - artifacts: list of output file paths written

    Accepts any mix of supported formats (PDFs, plain text, images). Files
    of unsupported formats are surfaced via `skipped` and as case-level
    warnings on the report rather than being silently dropped.
    """
    case_dir = case_dir.resolve()
    case_name = case_dir.name
    out_root = SETTINGS.output_dir / case_name
    out_root.mkdir(parents=True, exist_ok=True)
    images_dir = out_root / "pages"

    supported, skipped = discover_documents(case_dir, images_out_dir=images_dir)
    if not supported:
        # Preserve the old error shape so callers that catch FileNotFoundError
        # still get a sensible message, but include a hint about what we did find.
        detail = (
            f"No supported documents found in {case_dir}. "
            f"Supported formats: .pdf, .txt, .md, .csv, .png, .jpg, .tiff, .webp. "
            f"Skipped: {[s.path.name for s in skipped] or 'nothing'}."
        )
        raise FileNotFoundError(detail)

    documents: list[BaseDocument] = []
    classifications: list[dict[str, Any]] = []

    invoice: SalesInvoice | None = None
    bol: BillOfLading | None = None
    pod: ProofOfDelivery | None = None
    remit: RemittanceAdvice | None = None
    claim: DeductionClaim | None = None

    # Step 1 — render + classify every supported file in parallel.
    # Rendering is I/O-bound (pdfplumber + PyMuPDF for PDFs, PIL for
    # images, straight read for text) and thread-safe. The heavy work
    # (OCR, vision LLM) happens later inside `extract()` and we keep
    # that sequential to stay friendly to the Surya singleton predictor.
    def _render_one(spec: DiscoveredFile):
        log.info("rendering %s (%s)", spec.path.name, spec.kind)
        rendered = load_document(spec)
        cls = classify(rendered)
        return spec, rendered, cls

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(supported)))) as pool:
        rendered_pairs = list(pool.map(_render_one, supported))

    for spec, rendered, cls in rendered_pairs:
        pdf = spec.path
        classifications.append(
            {
                "path": str(pdf),
                "kind": spec.kind,
                "doc_type": cls.doc_type.value,
                "confidence": cls.confidence,
                "reason": cls.reason,
                "pages": len(rendered.pages),
                "text_len": rendered.text_len,
                "looks_scanned": rendered.looks_scanned,
            }
        )
        cache_key = (rendered.sha256, cls.doc_type.value)
        cached = _DOC_CACHE.get(cache_key)
        if cached is not None:
            log.info("cache hit for %s (sha=%s)", pdf.name, rendered.sha256[:8])
            doc = cached.model_copy(deep=True)
        else:
            log.info("extracting %s (doctype=%s)", pdf.name, cls.doc_type.value)
            doc = extract(rendered, cls)
            if doc is not None:
                _DOC_CACHE[cache_key] = doc.model_copy(deep=True)
        if doc is None:
            continue
        documents.append(doc)

        # Latch last-seen of each type; if duplicates show up, prefer the higher
        # extraction confidence.
        def better(existing: BaseDocument | None, candidate: BaseDocument) -> BaseDocument:
            if existing is None:
                return candidate
            return candidate if candidate.extraction_confidence > existing.extraction_confidence else existing

        if isinstance(doc, SalesInvoice):
            invoice = better(invoice, doc)  # type: ignore[assignment]
        elif isinstance(doc, BillOfLading):
            bol = better(bol, doc)  # type: ignore[assignment]
        elif isinstance(doc, ProofOfDelivery):
            pod = better(pod, doc)  # type: ignore[assignment]
        elif isinstance(doc, RemittanceAdvice):
            remit = better(remit, doc)  # type: ignore[assignment]
        elif isinstance(doc, DeductionClaim):
            claim = better(claim, doc)  # type: ignore[assignment]

    matched = build_matched_case(
        case_name=case_name,
        invoice=invoice,
        bol=bol,
        pod=pod,
        remittance=remit,
        claim=claim,
    )
    decisions = decide_case(matched)
    report = build_report(matched, case_path=str(case_dir), decisions=decisions)

    # Surface anything we had to skip as a case-level warning so it shows
    # up in the UI's "Case-level warnings" rail and in the JSON report.
    # This is the Tier-1 safety net: non-PDF / unsupported files never
    # disappear silently anymore.
    for sk in skipped:
        report.global_warnings.append(
            f"Skipped '{sk.path.name}': {sk.reason}."
        )

    artifacts: list[Path] = []
    if write_artifacts:
        (out_root / "report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        artifacts.append(out_root / "report.json")

        (out_root / "extractions.json").write_text(
            json.dumps(
                {
                    "classifications": classifications,
                    "documents": [d.model_dump() for d in documents],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        artifacts.append(out_root / "extractions.json")

        (out_root / "matched.json").write_text(
            matched.model_dump_json(indent=2), encoding="utf-8"
        )
        artifacts.append(out_root / "matched.json")

    return {
        "report": report,
        "matched": matched,
        "documents": documents,
        "classifications": classifications,
        "skipped": [{"path": str(s.path), "reason": s.reason} for s in skipped],
        "artifacts": [str(p) for p in artifacts],
        "output_dir": str(out_root),
    }
