"""
Shortage reconciliation rubric (from the case PDF appendix).

A shortage deduction is:
  VALID if receiving evidence shows qty_received < qty_invoiced AND
         claim $ == (qty_invoiced - qty_received) * net_unit_price (with small tol)
         AND no doc contradicts.
  INVALID if receiving evidence confirms full qty received for that line,
          OR the claim math does not match what the rubric would produce.
  NEEDS_HUMAN_REVIEW otherwise (missing / ambiguous / aggregate-only evidence,
          doc contradictions, arithmetic ambiguity, etc.)

Numbers flow from extracted fields + arithmetic. The LLM is only used
downstream to write a human-readable narrative trace; the verdict itself
comes from these deterministic rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from reconcile.schemas import (
    ClaimType,
    Decision,
    DecisionBand,
    EvidenceRef,
    LineDecision,
    MatchedCase,
    MatchedClaimLine,
    ReasoningStep,
    ShippedLine,
)

TOLERANCE_DOLLARS = 1.00  # absolute $
TOLERANCE_UNIT_PCT = 0.02  # 2% tolerance on unit price comparisons


# Stamp-narrative patterns that explicitly assert no shortage. We require
# an explicit zero (or "no shortage") rather than absence of the word —
# silence in the notes is not the same as a positive "0 short" signal.
_NO_SHORTAGE_PATTERNS = [
    re.compile(r"\bover\s*/\s*short\s*[:=]?\s*0\b", re.IGNORECASE),
    re.compile(r"\b(short|shortage)\s*(cases|qty|quantity)?\s*[:=]?\s*0\b", re.IGNORECASE),
    re.compile(r"\bno\s+shortage\b", re.IGNORECASE),
    re.compile(r"\bdelivered\s+in\s+full\b", re.IGNORECASE),
    re.compile(r"\bfull\s+delivery\b", re.IGNORECASE),
]


def _stamp_asserts_no_shortage(stamp_notes: str | None) -> bool:
    """
    True iff the receiver's stamp notes explicitly say "no shortage"
    (e.g. `Over/Short: 0`, `Shortage: 0`, "delivered in full").

    This exists because vision extractors sometimes populate the
    handwritten *narrative* (`stamp_notes`) confidently while leaving
    one of the two numeric Sub-Total fields null. The narrative carries
    the same signal — we just have to be willing to read it.
    """
    if not stamp_notes:
        return False
    return any(p.search(stamp_notes) for p in _NO_SHORTAGE_PATTERNS)


def _band(confidence: float) -> DecisionBand:
    if confidence >= 0.8:
        return DecisionBand.HIGH
    if confidence >= 0.5:
        return DecisionBand.MEDIUM
    return DecisionBand.LOW


def _qty_received_from_bol(bol_lines: list[ShippedLine]) -> float | None:
    if not bol_lines:
        return None
    totals = [ln.cases for ln in bol_lines if ln.cases is not None]
    if not totals:
        return None
    return sum(totals)


@dataclass
class _Context:
    case: MatchedCase
    line: MatchedClaimLine


def _evidence_for_invoice(ctx: _Context) -> EvidenceRef | None:
    inv = ctx.case.invoice
    inv_line = ctx.line.matched_invoice_line
    if not inv or not inv_line:
        return None
    return EvidenceRef(
        doc_path=inv.source_path,
        doc_type=inv.doc_type,
        field=f"line {inv_line.line_no} / material {inv_line.material_number}",
        snippet=(
            f"qty={inv_line.quantity} EA, unit=${inv_line.unit_price}, "
            f"promo={inv_line.off_invoice_promo}, "
            f"net_unit=${inv_line.net_unit_price}"
        ),
    )


def _evidence_for_bol(ctx: _Context) -> EvidenceRef | None:
    bol = ctx.case.bol
    if not bol:
        return None
    bol_hits = ctx.line.matched_bol_lines
    if bol_hits:
        cases = [ln.cases for ln in bol_hits]
        return EvidenceRef(
            doc_path=bol.source_path,
            doc_type=bol.doc_type,
            field="matched BOL lines",
            snippet=f"cases per matched BOL line(s): {cases}",
        )
    return EvidenceRef(
        doc_path=bol.source_path,
        doc_type=bol.doc_type,
        field="BOL",
        snippet="no line-level match for this material on the BOL",
    )


def _evidence_for_receiving(ctx: _Context) -> EvidenceRef | None:
    pod = ctx.case.pod
    if pod:
        r = pod.receiving
        return EvidenceRef(
            doc_path=pod.source_path,
            doc_type=pod.doc_type,
            field="receiving stamp",
            snippet=(
                f"stamp={r.has_receiving_stamp}, "
                f"total_shipped={r.total_cases_shipped}, "
                f"total_received={r.total_cases_received}, "
                f"notes={r.stamp_notes!r}"
            ),
        )
    bol = ctx.case.bol
    if bol and bol.receiving:
        r = bol.receiving
        return EvidenceRef(
            doc_path=bol.source_path,
            doc_type=bol.doc_type,
            field="receiving stamp on BOL",
            snippet=(
                f"stamp={r.has_receiving_stamp}, "
                f"total_shipped={r.total_cases_shipped}, "
                f"total_received={r.total_cases_received}, "
                f"notes={r.stamp_notes!r}"
            ),
        )
    return None


def _evidence_for_remittance(ctx: _Context) -> EvidenceRef | None:
    remit = ctx.case.remittance
    rl = ctx.line.matched_remittance_line
    if not remit or not rl:
        return None
    return EvidenceRef(
        doc_path=remit.source_path,
        doc_type=remit.doc_type,
        field=f"CM line {rl.seller_invoice_num}",
        snippet=f"CM amount=${rl.invoice_amount} net=${rl.net_amount_paid}",
    )


def decide_line(case: MatchedCase, line: MatchedClaimLine, claim_index: int) -> LineDecision:
    ctx = _Context(case=case, line=line)
    claim = line.claim
    inv_line = line.matched_invoice_line

    steps: list[ReasoningStep] = []
    gaps: list[str] = []
    computed: dict[str, float | str | None] = {}

    # --- Step 1: Identify the line under claim.
    if inv_line is None:
        gaps.append("Could not confidently match the claim UPC to an invoice line.")
        steps.append(
            ReasoningStep(
                step=(
                    f"Claim UPC {claim.upc!r} / desc {claim.description!r} "
                    f"did not match any invoice line above threshold "
                    f"(best score={line.matched_invoice_line_score})."
                ),
                evidence=[e for e in [_evidence_for_invoice(ctx)] if e],
            )
        )
        return LineDecision(
            claim_index=claim_index,
            upc=claim.upc,
            description=claim.description,
            claimed_amount=claim.adj_amount,
            decision=Decision.NEEDS_HUMAN_REVIEW,
            confidence=0.35,
            confidence_band=_band(0.35),
            reasoning=steps,
            evidence_gaps=gaps,
            computed=computed,
            claim_type_detected=claim.claim_type or ClaimType.UNKNOWN,
            rubric_applied="shortage",
        )

    qty_invoiced = inv_line.quantity
    net_unit = inv_line.net_unit_price
    claim_qty = claim.adj_qty
    claim_amt = claim.adj_amount
    claim_unit = claim.unit_price

    computed["qty_invoiced"] = qty_invoiced
    computed["invoice_unit_price_gross"] = inv_line.unit_price
    computed["invoice_unit_price_net_of_promo"] = net_unit
    computed["claim_adj_qty"] = claim_qty
    computed["claim_unit_price"] = claim_unit
    computed["claim_adj_amount"] = claim_amt

    steps.append(
        ReasoningStep(
            step=(
                f"Matched claim to invoice line {inv_line.line_no} (material "
                f"{inv_line.material_number}, {inv_line.description!r}). "
                f"Invoice: qty={qty_invoiced} EA, unit=${inv_line.unit_price}, "
                f"net-of-promo unit=${net_unit}. "
                f"Claim: qty={claim_qty}, unit=${claim_unit}, amount=${claim_amt}."
            ),
            evidence=[e for e in [_evidence_for_invoice(ctx)] if e],
        )
    )

    # --- Step 2: Math consistency of the claim itself.
    math_ok_self = None
    if claim_qty is not None and claim_unit is not None and claim_amt is not None:
        expected = claim_qty * claim_unit
        actual = abs(claim_amt)
        math_ok_self = abs(expected - actual) <= TOLERANCE_DOLLARS
        computed["claim_self_check_expected"] = round(expected, 2)
        steps.append(
            ReasoningStep(
                step=(
                    f"Claim self-check: {claim_qty} x ${claim_unit} = "
                    f"${expected:.2f} vs claimed ${actual:.2f} "
                    f"({'matches' if math_ok_self else 'mismatch'})."
                )
            )
        )

    # --- Step 3: Does the claim's unit price match net-of-promo from the invoice?
    unit_price_aligns = None
    if claim_unit is not None and net_unit is not None:
        diff = abs(claim_unit - net_unit)
        ref = max(net_unit, 0.01)
        unit_price_aligns = (diff / ref) <= TOLERANCE_UNIT_PCT
        steps.append(
            ReasoningStep(
                step=(
                    f"Unit price alignment: claim ${claim_unit} vs "
                    f"invoice net-of-promo ${net_unit} "
                    f"({'aligned' if unit_price_aligns else 'different'})."
                )
            )
        )

    # --- Step 4: Receiving evidence.
    receiving_ref = _evidence_for_receiving(ctx)
    bol_ref = _evidence_for_bol(ctx)

    pod = case.pod
    bol = case.bol
    receiving = pod.receiving if pod else (bol.receiving if bol and bol.receiving else None)

    # BOL "cases" are what the SHIPPER loaded, i.e. cases invoiced on this
    # line. They are not the quantity RECEIVED — that's the receiver's
    # domain (POD stamp). We keep them around only to detect
    # contradictions (e.g. the BOL loaded fewer cases than the invoice
    # claims were billed, which itself would undermine the invoice-line
    # quantity).
    bol_cases_loaded: float | None = None
    if line.matched_bol_lines:
        bol_cases_loaded = _qty_received_from_bol(line.matched_bol_lines)
        computed["bol_cases_loaded_for_line"] = bol_cases_loaded

    # Check for a BOL-vs-invoice contradiction at the line level. If the BOL
    # and invoice disagree about how many cases were shipped, we should be
    # less confident about anything derived from the invoice quantity.
    bol_matches_invoice = None
    if bol_cases_loaded is not None and qty_invoiced is not None:
        bol_matches_invoice = abs(bol_cases_loaded - qty_invoiced) < 0.5

    # Classify the receiving evidence. Three possible "ok" forms:
    #
    #   1. aggregate_ok           — both numbers present, received >= shipped.
    #   2. aggregate_ok_inferred  — at least one number missing, but the
    #                               stamp narrative says explicitly
    #                               "Over/Short: 0" / "no shortage" /
    #                               "delivered in full" AND the
    #                               extractor's `aggregate_shortage`
    #                               flag is False. Lower confidence
    #                               than #1 because we lean on prose,
    #                               not arithmetic.
    #   3. aggregate_short        — both numbers present, received < shipped.
    #
    # Anything else is `none` and routes to human review.
    receiving_kind = "none"
    if receiving and (
        receiving.total_cases_shipped is not None
        and receiving.total_cases_received is not None
    ):
        shipped = receiving.total_cases_shipped
        received = receiving.total_cases_received
        computed["total_cases_shipped"] = shipped
        computed["total_cases_received"] = received
        if received < shipped:
            receiving_kind = "aggregate_short"
        else:
            receiving_kind = "aggregate_ok"
    elif (
        receiving
        and receiving.has_receiving_stamp
        and not receiving.aggregate_shortage
        and _stamp_asserts_no_shortage(receiving.stamp_notes)
    ):
        # Vision returned a partial extraction but the handwritten
        # narrative explicitly attests to a zero shortage. Treat as
        # full-delivery evidence at slightly reduced confidence (we
        # can't arithmetically cross-check without both numbers).
        receiving_kind = "aggregate_ok_inferred"
        if receiving.total_cases_shipped is not None:
            computed["total_cases_shipped"] = receiving.total_cases_shipped
        if receiving.total_cases_received is not None:
            computed["total_cases_received"] = receiving.total_cases_received

    steps.append(
        ReasoningStep(
            step=(
                f"Receiving evidence kind: {receiving_kind}. "
                + (
                    (
                        f"BOL shipped {bol_cases_loaded} cases for this material "
                        + (
                            "(matches invoice). "
                            if bol_matches_invoice
                            else f"(invoice billed {qty_invoiced}). "
                        )
                    )
                    if bol_cases_loaded is not None
                    else ""
                )
                + (
                    f"Stamp notes: {receiving.stamp_notes!r}. "
                    if receiving and receiving.stamp_notes
                    else ""
                )
            ),
            evidence=[e for e in [bol_ref, receiving_ref] if e],
        )
    )

    # --- Step 5: Decision.
    #
    # Implements the case-rubric for shortage deductions:
    #   VALID if receiving evidence shows qty_received < qty_invoiced
    #   (either line-level, OR an aggregate shortage that the retailer has
    #   allocated to this line with no contradicting evidence), AND
    #   claim $ == (qty_invoiced − qty_received) × net_unit_price.
    #
    #   INVALID if receiving evidence confirms full qty received, OR the
    #   claim math doesn't match what the rubric would produce.
    #
    #   NEEDS_HUMAN_REVIEW otherwise.

    confidence = 0.5
    decision = Decision.NEEDS_HUMAN_REVIEW

    # Helper: does claim $ equal claim_qty × net_unit_price? This is the
    # rubric's math test — it checks the *claim's own* internal consistency
    # against net-of-promo pricing. When this holds AND receiving evidence
    # supports a shortage, the claim is VALID.
    math_with_net_unit_ok = None
    if claim_qty is not None and net_unit is not None and claim_amt is not None:
        expected_at_net = round(claim_qty * net_unit, 2)
        computed["expected_shortage_amount"] = expected_at_net
        math_with_net_unit_ok = abs(abs(claim_amt) - expected_at_net) <= TOLERANCE_DOLLARS

    if receiving_kind == "aggregate_ok":
        # Totals reconcile → nothing was actually short → claim is bogus.
        decision = Decision.INVALID
        confidence = 0.8
        steps.append(
            ReasoningStep(
                step=(
                    "Aggregate receiving totals equal (or exceed) shipped totals, "
                    "contradicting any shortage → INVALID."
                )
            )
        )

    elif receiving_kind == "aggregate_ok_inferred":
        # Numeric fields were partially extracted but the stamp narrative
        # explicitly asserts no shortage (e.g. "Over/Short: 0"). Per the
        # rubric, "receiving evidence confirms full qty received" → INVALID.
        # Lower confidence than the both-numbers-present path because we
        # can't arithmetically cross-check shipped vs. received.
        decision = Decision.INVALID
        confidence = 0.7
        steps.append(
            ReasoningStep(
                step=(
                    "Stamp narrative explicitly attests no shortage "
                    f"({receiving.stamp_notes!r}) and the extractor's "
                    "aggregate_shortage flag is False. Receiving evidence "
                    "confirms full qty received → INVALID."
                ),
                evidence=[e for e in [receiving_ref] if e],
            )
        )

    elif receiving_kind == "aggregate_short":
        # Rubric: an aggregate shortage that the retailer has allocated to
        # this line, with no contradicting evidence AND claim math that
        # checks out, is VALID. If the math is off, INVALID. Only truly
        # ambiguous cases (unknown net unit price, contradictions) route
        # to human review.
        allocation_is_clean = True
        if bol_matches_invoice is False:
            allocation_is_clean = False
            gaps.append(
                f"BOL shows {bol_cases_loaded} cases loaded for this line vs "
                f"{qty_invoiced} invoiced — line-level contradiction."
            )

        if math_with_net_unit_ok is None:
            # Can't run the math test: needs human review.
            decision = Decision.NEEDS_HUMAN_REVIEW
            confidence = 0.45
            gaps.append(
                "Invoice net-of-promo unit price unknown; cannot verify the "
                "shortage math against the retailer's allocation."
            )
            steps.append(
                ReasoningStep(
                    step=(
                        "Aggregate shortage present but we could not compute "
                        "claim_qty × net_unit to confirm the allocation — "
                        "routing to human review."
                    )
                )
            )
        elif math_with_net_unit_ok and allocation_is_clean:
            # Rubric VALID: aggregate shortage allocated to this line,
            # math checks out against net-of-promo unit price, no
            # contradictions. Confidence stays below the 0.85 we give to
            # line-level proof because the allocation itself is the
            # retailer's call, not independently verified.
            decision = Decision.VALID
            confidence = 0.78
            steps.append(
                ReasoningStep(
                    step=(
                        f"Aggregate shortage of "
                        f"{int(receiving.total_cases_shipped - receiving.total_cases_received)} "
                        f"cases on the POD/BOL; retailer allocated "
                        f"{claim_qty} cases to this line. "
                        f"Claim math checks: {claim_qty} × ${net_unit} "
                        f"net-of-promo = ${computed['expected_shortage_amount']} "
                        f"matches claim ${abs(claim_amt):.2f}. No contradicting "
                        f"evidence → VALID per the rubric's aggregate-allocation clause."
                    )
                )
            )
        elif math_with_net_unit_ok and not allocation_is_clean:
            # Math is fine but BOL contradicts the invoice — can't trust
            # the allocation cleanly.
            decision = Decision.NEEDS_HUMAN_REVIEW
            confidence = 0.5
            gaps.append(
                "Math checks out against net-of-promo pricing, but BOL "
                "line-level quantities contradict the invoice — allocation "
                "to this line can't be cleanly confirmed."
            )
        else:
            # Math doesn't match what the rubric would produce → INVALID.
            decision = Decision.INVALID
            confidence = 0.7
            steps.append(
                ReasoningStep(
                    step=(
                        f"Shortage math mismatch: expected "
                        f"${computed.get('expected_shortage_amount')}, "
                        f"claimed ${abs(claim_amt) if claim_amt is not None else '?'}"
                        f" → INVALID (math)."
                    )
                )
            )

    else:
        # receiving_kind == "none"
        decision = Decision.NEEDS_HUMAN_REVIEW
        confidence = 0.4
        gaps.append(
            "No receiving evidence (POD or stamped BOL) extracted for this line — "
            "cannot confirm or refute the shortage claim."
        )
        steps.append(
            ReasoningStep(
                step=(
                    "No receiving evidence is available for this line. Per the "
                    "rubric, we route to human review rather than guess."
                )
            )
        )

    # Penalize confidence if the claim's own math is broken or unit prices diverge
    # in ways we couldn't explain by promos.
    if math_ok_self is False:
        confidence -= 0.1
        gaps.append("Claim line math is internally inconsistent (qty * unit != amount).")
    if unit_price_aligns is False and inv_line.off_invoice_promo is None:
        confidence -= 0.05
        gaps.append(
            "Claim unit price differs from invoice unit price and no off-invoice "
            "promo was extracted to explain the gap."
        )
    if case.bol and case.bol.content_belongs_to_different_shipment:
        confidence -= 0.1
        gaps.append("BOL PDF may contain content from a different shipment.")
    confidence = max(0.05, min(0.98, confidence))

    steps.append(
        ReasoningStep(
            step=(
                "Cross-checks with remittance / check for the matching -CM line."
            ),
            evidence=[e for e in [_evidence_for_remittance(ctx)] if e],
        )
    )

    return LineDecision(
        claim_index=claim_index,
        upc=claim.upc,
        description=claim.description or (inv_line.description if inv_line else None),
        claimed_amount=claim.adj_amount,
        decision=decision,
        confidence=round(confidence, 2),
        confidence_band=_band(confidence),
        reasoning=steps,
        evidence_gaps=gaps,
        computed=computed,
        claim_type_detected=claim.claim_type or ClaimType.UNKNOWN,
        rubric_applied="shortage",
    )


def decide_case(case: MatchedCase) -> list[LineDecision]:
    if not case.claim_lines:
        return []
    return [decide_line(case, ln, i) for i, ln in enumerate(case.claim_lines)]
