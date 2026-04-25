"""
Document classifier.

We classify by a combination of filename hints and content signals so the
pipeline generalizes across new cases without hard-coded filenames. If the
heuristics are ambiguous or text is empty, we optionally ask the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from reconcile.ingest.renderer import RenderedPDF
from reconcile.schemas import DocType

# Content signatures — each is (regex, doc_type, strength).
_CONTENT_RULES: list[tuple[re.Pattern[str], DocType, float]] = [
    (re.compile(r"\bSTRAIGHT\s+BILL\s+OF\s+LADING\b", re.I), DocType.BILL_OF_LADING, 0.98),
    (re.compile(r"\bDELIVERY\s+ORDER\b", re.I), DocType.BILL_OF_LADING, 0.7),
    (re.compile(r"\bBOL\s*NUMBER\b", re.I), DocType.BILL_OF_LADING, 0.85),
    (re.compile(r"\bProof of Delivery\b", re.I), DocType.PROOF_OF_DELIVERY, 0.95),
    (re.compile(r"\bPOD\b"), DocType.PROOF_OF_DELIVERY, 0.5),
    (re.compile(r"\bREMITTANCE\s+INFORMATION\b", re.I), DocType.REMITTANCE_ADVICE, 0.98),
    (re.compile(r"\bEFFECTIVE\s+DATE\b.*\bCREDIT\b", re.I | re.S), DocType.REMITTANCE_ADVICE, 0.6),
    (re.compile(r"-CM\b"), DocType.REMITTANCE_ADVICE, 0.4),
    (re.compile(r"\bAssociated\s+Deductions\b", re.I), DocType.DEDUCTION_CLAIM, 0.95),
    (re.compile(r"\bAdjustment\s+reason\b", re.I), DocType.DEDUCTION_CLAIM, 0.8),
    (re.compile(r"\bKROGER\s+Invoice\s+number\b", re.I), DocType.DEDUCTION_CLAIM, 0.9),
    (re.compile(r"\bDeduction\s+amount\b", re.I), DocType.DEDUCTION_CLAIM, 0.5),
    (re.compile(r"\bBill-To-Party\b", re.I), DocType.SALES_INVOICE, 0.6),
    (re.compile(r"\bDocument\s+Number\b.*\bInvoice\b", re.I | re.S), DocType.SALES_INVOICE, 0.6),
    (re.compile(r"Off\s*Invoice\s*Pro", re.I), DocType.SALES_INVOICE, 0.8),
    (re.compile(r"Ref\.?\s*Order\s*Number", re.I), DocType.SALES_INVOICE, 0.5),
    (re.compile(r"Delivery\s*Number", re.I), DocType.SALES_INVOICE, 0.4),
]

# Filename hints — weaker than content but useful when text is garbled,
# missing entirely (scanned images), or served up as a non-PDF. We treat
# underscores and hyphens as word boundaries in addition to the usual
# `\b`, because real-world filenames love snake_case and kebab-case:
# "bol_page1.png", "pod-scan.jpg", "invoice_90403395.txt", etc.
_BOUND = r"(?:^|[^a-z0-9])"  # start-of-string OR any non-alnum char
_FILENAME_RULES: list[tuple[re.Pattern[str], DocType, float]] = [
    (re.compile(rf"sales.?invoice|invoice.*\d{{4,}}|{_BOUND}inv{_BOUND}", re.I), DocType.SALES_INVOICE, 0.7),
    (re.compile(rf"bill.?of.?lading|{_BOUND}bol{_BOUND}", re.I), DocType.BILL_OF_LADING, 0.8),
    (re.compile(rf"{_BOUND}pod{_BOUND}|proof.?of.?delivery|receiving", re.I), DocType.PROOF_OF_DELIVERY, 0.85),
    (re.compile(r"check|payment|remittance", re.I), DocType.REMITTANCE_ADVICE, 0.8),
    (re.compile(r"deduction|chargeback|debit.?note|claim", re.I), DocType.DEDUCTION_CLAIM, 0.8),
]


@dataclass
class Classification:
    doc_type: DocType
    confidence: float
    reason: str


def classify(rendered: RenderedPDF) -> Classification:
    text = rendered.full_text or ""
    name = rendered.source_path.name

    scores: dict[DocType, float] = {}
    reasons: dict[DocType, list[str]] = {}

    for pat, dt, strength in _CONTENT_RULES:
        if pat.search(text):
            scores[dt] = max(scores.get(dt, 0.0), strength)
            reasons.setdefault(dt, []).append(f"content~{pat.pattern[:40]}")

    for pat, dt, strength in _FILENAME_RULES:
        if pat.search(name):
            scores[dt] = max(scores.get(dt, 0.0), strength * 0.9)  # slightly discount filename-only
            reasons.setdefault(dt, []).append(f"filename~{pat.pattern[:40]}")

    if not scores:
        return Classification(DocType.UNKNOWN, 0.2, f"no rule matched for {name}")

    best = max(scores.items(), key=lambda kv: kv[1])
    dt, conf = best
    return Classification(dt, conf, "; ".join(reasons[dt])[:300])
