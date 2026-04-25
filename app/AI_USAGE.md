# AI Usage Notes

## Tools / models

- **Cursor (Claude)** — pair-programmed the whole scaffold, schemas, extractors, matcher, decision engine, CLI, and Streamlit UI.
- **Groq API**:
  - **Text**: `llama-3.3-70b-versatile` — used only as a fallback when the deterministic regex extractor returns zero line items from a sales invoice or remittance advice.
  - **Vision**: `meta-llama/llama-4-scout-17b-16e-instruct` — used for scanned BOLs, PODs, and the garbled first page of the package-2 deduction PDF. Prompted to quote handwriting verbatim and to flag "content belongs to another shipment" cases rather than silently merging.

## What was hand-coded vs. AI-generated

| Piece | Style |
| --- | --- |
| Canonical Pydantic schemas | Hand-designed from the case PDF; these are the contract the rest of the system hangs off of. |
| Deterministic invoice/remittance/claim parsers | Regex-first; small, testable, cheap. These came out of reading the actual PDFs in the bundle. |
| BOL / POD vision extractors | AI-assisted, but the **prompts** are hand-tuned to avoid hallucinated quantities and to require null for unknowns. |
| Matching (UPC↔material, claim↔remittance CM) | Hand-coded. LLMs are unreliable for this kind of discrete linkage and it's easy to get wrong invisibly. |
| Shortage decision rubric | Hand-coded rules + arithmetic. **The LLM never produces a verdict.** |
| Reasoning trace | Built from extracted facts (deterministic). Each step cites the document, page/field, and a short snippet. |

## Where I'd expect the LLM to be wrong, and what I did about it

- **Arithmetic**: Moved out of the LLM. All shortage math is computed from extracted numbers. The LLM produces narrative, never the verdict.
- **Handwriting / stamps**: Low confidence by default (0.7). Aggregate-only receiving evidence is never treated as line-level proof — routed to `needs_human_review`.
- **Cross-doc identity**: UPC↔material is a deterministic function; we do not ask the LLM "is this the same product?" We flag mismatches rather than reconcile them silently.
- **JSON mode flakiness**: Groq JSON mode occasionally wraps output in code fences; the client strips those before parsing and retries with exponential backoff.

## Confidence model

Per line decision, confidence starts from the rubric branch (line-level proof → 0.85, aggregate → 0.55, missing → 0.4, …) and is **penalized** when:

- claim self-math is inconsistent (−0.10)
- unit price diverges from the invoice with no extracted promo to explain it (−0.05)
- BOL is flagged as potentially containing another shipment (−0.10)

Bands: high ≥ 0.80, medium ≥ 0.50, else low.

## Noteworthy moments

- The package-2 deduction PDF's first page is genuinely garbled on a text extract. The pipeline now **always** renders page images and the claim extractor falls back to vision when the text pass returns <200 chars of useful content. This is exactly the "messy real input" the case warned about.
- The package-1 BOL has effectively no extractable text (image-only). Same path: renderer always captures images so the vision extractor has something to read.
- Sales invoices turned out to be *very* stable textually, so a regex parser was both faster and more accurate than the LLM. LLM is the fallback, not the default.
