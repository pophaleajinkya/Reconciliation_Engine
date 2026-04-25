"""
Decision dispatcher.

The case PDF lists many deduction claim types in production — shortages,
pricing disputes, compliance fees, unsaleables, and "dozens of other
reason codes". For the take-home scope we ship only the SHORTAGE rubric;
this dispatcher routes every other claim type to a deterministic
`NEEDS_HUMAN_REVIEW` with a rubric-specific gap message.

The split is deliberate: it keeps the proven shortage logic untouched,
makes it obvious to a reviewer where new rubrics plug in, and prevents us
from silently running the wrong rubric on a pricing or compliance claim.

Wiring:
  * `pipeline.py` calls `decide_case()` from this module.
  * Per-line dispatch goes here (`dispatch_decision`) which selects the
    rubric based on `ClaimLine.claim_type`.
  * Shortage logic stays in `decide.shortage` and is the only rubric
    actually implemented today.
"""

from __future__ import annotations

import logging

from reconcile.decide.shortage import decide_line as shortage_decide_line
from reconcile.schemas import (
    ClaimType,
    Decision,
    DecisionBand,
    LineDecision,
    MatchedCase,
    MatchedClaimLine,
    ReasoningStep,
)

log = logging.getLogger("reconcile.decide")


# Per-rubric gap-message templates for unsupported claim types. These are
# the strings a human analyst sees when they pick up a non-shortage claim
# from the queue. They're worded as instructions ("here's what you need
# to verify") rather than apologies ("sorry, we don't handle this") so
# the analyst can act immediately.
_UNSUPPORTED_RUBRIC_MESSAGES: dict[ClaimType, str] = {
    ClaimType.PRICING: (
        "Pricing dispute detected. The shortage rubric does not apply — "
        "verifying this claim requires the contracted price list / deal sheet "
        "for this SKU and the period of the shipment, plus any "
        "off-invoice or off-contract promo allowances. Routing to human review."
    ),
    ClaimType.COMPLIANCE: (
        "Compliance / on-time-in-full (OTIF) claim detected. The shortage "
        "rubric does not apply — verifying this claim requires the vendor "
        "agreement's OTIF terms (penalty rate, on-time window, exemptions), "
        "carrier delivery confirmation, and the original required arrival "
        "date (ORAD). Routing to human review."
    ),
    ClaimType.UNSALEABLES: (
        "Unsaleables / damaged-goods claim detected. The shortage rubric "
        "does not apply — verifying this claim requires return-merchandise-"
        "authorization (RMA) records, damage photos, and the vendor's "
        "unsaleables agreement. Routing to human review."
    ),
    ClaimType.OTHER: (
        "Claim recognized as a non-shortage type that this prototype does "
        "not yet have a rubric for. Routing to human review."
    ),
    ClaimType.UNKNOWN: (
        "Claim type could not be classified from the reason code or "
        "narrative. A human analyst should identify the rubric and "
        "verify accordingly."
    ),
}


def _band(confidence: float) -> DecisionBand:
    if confidence >= 0.8:
        return DecisionBand.HIGH
    if confidence >= 0.5:
        return DecisionBand.MEDIUM
    return DecisionBand.LOW


def _unsupported_decision(
    line: MatchedClaimLine, claim_index: int, claim_type: ClaimType
) -> LineDecision:
    """Build a NEEDS_HUMAN_REVIEW decision for a non-shortage claim type."""
    claim = line.claim
    msg = _UNSUPPORTED_RUBRIC_MESSAGES.get(
        claim_type, _UNSUPPORTED_RUBRIC_MESSAGES[ClaimType.OTHER]
    )
    rationale = claim.claim_type_rationale or "no classifier rationale recorded"
    # Confidence on the *route-to-human* call. We are highly confident
    # that this is not a shortage when the reason code resolved cleanly
    # (≥0.82); we are less confident when only narrative matched. Either
    # way the verdict is the same, but the analyst sees how trusted the
    # classification was.
    type_conf = claim.claim_type_confidence or 0.0
    routing_conf = max(0.4, min(0.7, 0.4 + 0.3 * type_conf))

    steps: list[ReasoningStep] = [
        ReasoningStep(
            step=(
                f"Claim line classified as {claim_type.value} "
                f"(classifier confidence {type_conf:.2f}). "
                f"Rationale: {rationale}."
            )
        ),
        ReasoningStep(step=msg),
    ]
    return LineDecision(
        claim_index=claim_index,
        upc=claim.upc,
        description=claim.description,
        claimed_amount=claim.adj_amount,
        decision=Decision.NEEDS_HUMAN_REVIEW,
        confidence=round(routing_conf, 2),
        confidence_band=_band(routing_conf),
        reasoning=steps,
        evidence_gaps=[msg],
        computed={"claim_type": claim_type.value},
        claim_type_detected=claim_type,
        rubric_applied="unsupported",
    )


def dispatch_decision(
    case: MatchedCase, line: MatchedClaimLine, claim_index: int
) -> LineDecision:
    """
    Select the right rubric for a claim line and return its decision.

    SHORTAGE and UNKNOWN both go through the shortage rubric — UNKNOWN
    because in our two reference bundles every claim is a shortage and
    we want sane behaviour even if classification fails (the rubric
    itself surfaces "no receiving evidence" if it really is something
    else). All other claim types route to a structured human-review
    decision with a rubric-specific gap message.
    """
    claim_type = line.claim.claim_type or ClaimType.UNKNOWN

    if claim_type in (ClaimType.SHORTAGE, ClaimType.UNKNOWN):
        decision = shortage_decide_line(case, line, claim_index)
        # Stamp the detected type onto the decision so the UI can show it
        # even when the shortage rubric ran. rubric_applied stays at its
        # default "shortage" set by the schema.
        decision.claim_type_detected = claim_type
        return decision

    log.info(
        "Claim line %s (UPC=%s) is %s — routing to human review (no rubric).",
        claim_index,
        line.claim.upc,
        claim_type.value,
    )
    return _unsupported_decision(line, claim_index, claim_type)


def decide_case(case: MatchedCase) -> list[LineDecision]:
    if not case.claim_lines:
        return []
    return [dispatch_decision(case, ln, i) for i, ln in enumerate(case.claim_lines)]
