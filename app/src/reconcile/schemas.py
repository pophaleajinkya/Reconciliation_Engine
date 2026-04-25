"""
Canonical document and decision schemas.

These are the contract between extraction, matching, and decision layers.
Every extractor (text or vision) must emit one of the `*Document` models.
The decision engine consumes only these models plus `MatchedCase`, never raw
PDF text. This is what lets us swap any layer without touching the others.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DocType(str, Enum):
    SALES_INVOICE = "sales_invoice"
    BILL_OF_LADING = "bill_of_lading"
    PROOF_OF_DELIVERY = "proof_of_delivery"
    REMITTANCE_ADVICE = "remittance_advice"
    DEDUCTION_CLAIM = "deduction_claim"
    UNKNOWN = "unknown"


class ClaimType(str, Enum):
    """
    The rubric family a deduction claim falls under.

    Per the case PDF, Curta handles many claim types in production
    (shortages, pricing, compliance fees, unsaleables, and dozens of
    other reason codes). For the take-home scope we ship only the
    SHORTAGE rubric and route everything else to NEEDS_HUMAN_REVIEW
    with a rubric-specific gap message. The classifier that tags the
    claim lives in `extract.claim.classify_claim_type`.
    """

    SHORTAGE = "shortage"
    PRICING = "pricing"
    COMPLIANCE = "compliance"  # late delivery, OTIF, routing, etc.
    UNSALEABLES = "unsaleables"  # damaged / expired goods
    OTHER = "other"
    UNKNOWN = "unknown"


class ExtractionMethod(str, Enum):
    TEXT_DETERMINISTIC = "text_deterministic"
    OCR_DETERMINISTIC = "ocr_deterministic"  # Surya OCR + regex
    TEXT_LLM = "text_llm"
    VISION_LLM = "vision_llm"
    MIXED = "mixed"


class EvidenceRef(BaseModel):
    """A pointer to a specific fact inside a source document, for audit trails."""

    doc_path: str
    doc_type: DocType
    page: int | None = None
    field: str | None = None
    snippet: str | None = None
    note: str | None = None


# ---------- Line items ----------


class InvoiceLine(BaseModel):
    line_no: str | None = None
    material_number: str | None = None
    description: str | None = None
    quantity: float | None = None
    unit_label: str | None = None  # e.g. "EA" (often means cases per case notes)
    unit_price: float | None = None
    gross_value: float | None = None
    off_invoice_promo: float | None = None  # dollar amount (negative = discount)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def net_unit_price(self) -> float | None:
        """Unit price net of off-invoice promo, when computable."""
        if self.unit_price is None:
            return None
        if not self.quantity or self.quantity == 0:
            return self.unit_price
        if self.off_invoice_promo is None:
            return self.unit_price
        promo_per_unit = abs(self.off_invoice_promo) / self.quantity
        return round(self.unit_price - promo_per_unit, 6)


class ShippedLine(BaseModel):
    """A line as it appears on a BOL or POD (shipped / received quantities)."""

    material_number: str | None = None
    customer_sku: str | None = None
    description: str | None = None
    cases: float | None = None
    weight: float | None = None
    # Source page (1-indexed) this line was extracted from. Lets the UI
    # group lines by page and lets the cross-shipment filter route only
    # primary-shipment lines into the rubric.
    page_number: int | None = None
    # False when the line came from a page whose header (BOL #, ship-to,
    # PO) disagrees with the primary shipment's header. Such lines are
    # surfaced in the UI with a "different shipment" flag but excluded
    # from matching / decision math.
    belongs_to_primary_shipment: bool = True
    raw: dict[str, Any] = Field(default_factory=dict)


class ReceivingEvidence(BaseModel):
    """What the POD / stamped BOL tells us about what was actually received."""

    has_receiving_stamp: bool = False
    stamp_notes: str | None = None
    total_cases_shipped: float | None = None
    total_cases_received: float | None = None
    aggregate_shortage: bool = False
    line_level_exceptions: list[str] = Field(default_factory=list)


class RemittanceLine(BaseModel):
    seller_invoice_num: str | None = None
    is_credit_memo: bool = False
    invoice_amount: float | None = None
    net_amount_paid: float | None = None
    terms_discount: float | None = None


class ClaimLine(BaseModel):
    """A single line on the retailer's deduction / claim."""

    upc: str | None = None
    description: str | None = None
    adj_qty: float | None = None
    unit_price: float | None = None
    adj_amount: float | None = None
    reason_code: str | None = None
    reason_text: str | None = None
    # Rubric family this line falls under. Populated by the claim-type
    # classifier in `extract.claim`. Defaults to UNKNOWN so the decision
    # engine can tell "not classified yet" from "genuinely other".
    claim_type: "ClaimType" = Field(default_factory=lambda: ClaimType.UNKNOWN)
    claim_type_confidence: float = 0.0
    claim_type_rationale: str | None = None


# ---------- Document-level models ----------


class BaseDocument(BaseModel):
    doc_type: DocType
    source_path: str
    pages: int = 0
    extraction_method: ExtractionMethod
    extraction_confidence: float = 0.5  # 0-1
    parse_warnings: list[str] = Field(default_factory=list)


class SalesInvoice(BaseDocument):
    doc_type: DocType = DocType.SALES_INVOICE
    invoice_number: str | None = None
    invoice_date: str | None = None
    po_number: str | None = None
    delivery_number: str | None = None
    bill_to: str | None = None
    ship_to: str | None = None
    carrier: str | None = None
    terms_of_payment: str | None = None
    lines: list[InvoiceLine] = Field(default_factory=list)
    subtotal: float | None = None
    total_amount: float | None = None


class BillOfLading(BaseDocument):
    doc_type: DocType = DocType.BILL_OF_LADING
    bol_number: str | None = None
    pro_number: str | None = None
    ship_to: str | None = None
    po_number: str | None = None
    ship_date: str | None = None
    carrier: str | None = None
    lines: list[ShippedLine] = Field(default_factory=list)
    total_cases: float | None = None
    receiving: ReceivingEvidence | None = None
    content_belongs_to_different_shipment: bool = False
    # Page-level breakdown of cross-shipment detection. Page 1's header
    # (BOL number, ship-to, PO number) is treated as the primary
    # shipment; pages whose header disagrees end up in
    # `cross_shipment_pages` with their own header in
    # `cross_shipment_details`. The UI uses these to render flagged
    # lines under their own page banner.
    primary_shipment_pages: list[int] = Field(default_factory=list)
    cross_shipment_pages: list[int] = Field(default_factory=list)
    cross_shipment_details: list[dict[str, Any]] = Field(default_factory=list)


class ProofOfDelivery(BaseDocument):
    doc_type: DocType = DocType.PROOF_OF_DELIVERY
    referenced_bol: str | None = None
    referenced_po: str | None = None
    receiving: ReceivingEvidence = Field(default_factory=ReceivingEvidence)


class RemittanceAdvice(BaseDocument):
    doc_type: DocType = DocType.REMITTANCE_ADVICE
    originator: str | None = None
    effective_date: str | None = None
    lines: list[RemittanceLine] = Field(default_factory=list)


class DeductionClaim(BaseDocument):
    doc_type: DocType = DocType.DEDUCTION_CLAIM
    invoice_number: str | None = None
    po_number: str | None = None
    deduction_amount: float | None = None
    gross_invoice_amount: float | None = None
    net_invoice_amount: float | None = None
    discount_amount: float | None = None
    lines: list[ClaimLine] = Field(default_factory=list)


AnyDocument = (
    SalesInvoice
    | BillOfLading
    | ProofOfDelivery
    | RemittanceAdvice
    | DeductionClaim
)


# ---------- Matching & decision ----------


class MatchedClaimLine(BaseModel):
    """A single claim line paired with its supporting evidence after matching."""

    claim: ClaimLine
    matched_invoice_line: InvoiceLine | None = None
    matched_invoice_line_score: float = 0.0
    matched_bol_lines: list[ShippedLine] = Field(default_factory=list)
    matched_remittance_line: RemittanceLine | None = None
    match_notes: list[str] = Field(default_factory=list)


class MatchedCase(BaseModel):
    case_name: str
    invoice: SalesInvoice | None = None
    bol: BillOfLading | None = None
    pod: ProofOfDelivery | None = None
    remittance: RemittanceAdvice | None = None
    claim: DeductionClaim | None = None
    claim_lines: list[MatchedClaimLine] = Field(default_factory=list)
    global_warnings: list[str] = Field(default_factory=list)


class Decision(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class DecisionBand(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReasoningStep(BaseModel):
    step: str
    evidence: list[EvidenceRef] = Field(default_factory=list)


class LineDecision(BaseModel):
    """The final output for a single claimed deduction line."""

    claim_index: int
    upc: str | None = None
    description: str | None = None
    claimed_amount: float | None = None
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_band: DecisionBand
    reasoning: list[ReasoningStep] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    computed: dict[str, Any] = Field(default_factory=dict)
    # Which rubric family the decision engine picked, plus which rubric it
    # actually ran. For SHORTAGE claims these are the same; for PRICING /
    # COMPLIANCE / UNSALEABLES we log `detected=<type>, applied=none` so
    # the UI can explain "classified but routed to human — no rubric yet".
    claim_type_detected: ClaimType = ClaimType.UNKNOWN
    rubric_applied: str = "shortage"  # "shortage" | "unsupported" | ...


class CaseReport(BaseModel):
    case_name: str
    case_path: str
    invoice_number: str | None = None
    total_deduction_claimed: float | None = None
    documents_seen: list[str] = Field(default_factory=list)
    documents_missing: list[str] = Field(default_factory=list)
    line_decisions: list[LineDecision] = Field(default_factory=list)
    global_warnings: list[str] = Field(default_factory=list)
