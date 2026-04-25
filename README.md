# Reconciliation Engine

> **Repo:** [github.com/pophaleajinkya/Reconciliation_Engine](https://github.com/pophaleajinkya/Reconciliation_Engine)

Prototype CPG deduction / chargeback reconciliation agent for the Curta founding-engineer case.
Given a case folder with an invoice bundle (sales invoice, BOL, POD, remittance, deduction claim), it outputs, **per claimed line**:

- `decision`: `valid` / `invalid` / `needs_human_review`
- `confidence` (0–1) + band (`low` / `medium` / `high`)
- `reasoning_trace` with citations back to the source document, page, and field
- `evidence_gaps` — what a human would need to resolve

The system is **not hard-coded to any case**. Every layer is behind an interface and swappable.

---

## Architecture

```
case folder (PDFs)
      │
      ▼
┌─────────────┐   text via pdfplumber + page images via PyMuPDF
│  Ingestion  │   (renderer.py, classifier.py)
└─────────────┘
      │
      ▼
┌─────────────┐   per doc-type extractor; text-first, vision fallback
│ Extraction  │   (extract/*.py + llm/groq_client.py)
└─────────────┘
      │
      ▼
┌─────────────┐   UPC↔material crosswalk, invoice↔BOL↔remit↔claim linkage
│  Matching   │   (match/resolver.py)
└─────────────┘
      │
      ▼
┌─────────────┐   deterministic shortage rubric (valid/invalid/needs_review),
│  Decision   │   confidence, reasoning, evidence gaps
└─────────────┘   (decide/shortage.py)
      │
      ▼
┌─────────────┐   JSON report + Streamlit UI
│   Report    │   (report/builder.py, webapp.py, __main__.py)
└─────────────┘
```

**Key design choices:**

| Layer | Style | Why |
| --- | --- | --- |
| Ingestion | Deterministic | File classification should not cost LLM calls. |
| Invoice/Remittance extraction | Regex deterministic with OCR + LLM fallbacks | Formats are stable and high-signal; heavier tools only when regex returns 0 rows. |
| BOL / POD / garbled Deduction pages | 3-tier: text → Surya OCR → Vision LLM | Offline OCR handles scanned pages without any API key; vision LLM reserved for stamps/handwriting. |
| Matching | Deterministic | Reproducible; uses UPC-suffix ↔ material-number crosswalk explicitly. |
| Shortage decision | **Deterministic math + rules** | LLMs are bad at arithmetic. Verdicts are derived, not generated. |
| Narrative trace | Deterministic builder | Every reasoning step is tied to a fact we extracted. |

### Architecture classification — Workflow, not Agent

It's worth being precise here because the labels matter for governance,
and "agent" gets thrown around as a buzzword.

**This system is a deterministic *workflow*, not an autonomous agent,
not RAG, and not an MCP agent.** Specifically, by [Anthropic's
taxonomy](https://www.anthropic.com/research/building-effective-agents)
it combines two named workflow patterns: **Routing** (the
native-text → Surya OCR → vision-LLM ladder, where the routing
decision is deterministic, not model-driven) and **Prompt Chaining**
(sequential LLM calls inside each extractor).

Why those other labels don't apply:

| Pattern | What defines it | Why this isn't that |
| --- | --- | --- |
| **RAG** | A retriever pulls top-k passages from a corpus; the LLM *generates* the answer from them | No retriever, no corpus, no generated verdict. The case bundle *is* the input — there's nothing to retrieve. |
| **MCP / autonomous agent** | LLM decides which tool to call next in a planning loop | Pipeline order is fixed in code (ingest → extract → match → decide → report). The LLM never picks the next step. |
| **ML pipeline** | A model outputs the final classification | The verdict comes from a rule-based rubric. No model is in the decision path. |

**Why we chose Workflow over Agent:** for a finance-compliance use
case, *"the LLM decided X"* is not an answer a CFO will accept.
*"The rule said X because the BOL stamp said `Over/Short: 0` and the
math (`66 × $30.72 = $2,027.52`) verified the claim allocation"* is.
Workflows give predictable cost, reproducible verdicts, and a
line-by-line audit trail. Agents give flexibility we don't need —
the steps are already known.

**The architecture is agent-ready, though.** Each extractor is a
typed function with a schema (`RenderedPDF → BaseDocument`); the
deterministic orchestrator could be swapped for an agent loop, or the
extractors exposed as MCP tools to a third-party agent, without
changing the extractor implementations themselves. We chose not to
today — the seams are clean if that ever changes.

The only place an LLM is allowed to "speak" is the post-decision
analyst memo in the experimental `app2/` branch. It runs strictly
*after* the verdict is sealed, is decision-aware (different prompts
for VALID / INVALID / NEEDS_HUMAN_REVIEW), and cannot alter the
decision — separating the deterministic verdict from its
human-readable explanation.

### Extraction fallback ladder

Every extractor runs through the same three tiers automatically:

1. **Native text** via `pdfplumber`. Fast, zero cost, works on well-formed PDFs.
2. **Local OCR** via [Surya](https://github.com/datalab-to/surya) (Apache-2.0). Triggered only when per-page native text averages < 120 chars. First run downloads ~1GB of weights; runs on CPU or GPU/MPS. No API key required.
3. **Vision LLM** via Groq (Llama 4 Scout). Used when OCR still can't find key fields, and always for stamp/handwriting extraction where structured OCR isn't enough.

The `extraction_method` field on each document tells you which tier fired (`text_deterministic`, `ocr_deterministic`, `text_llm`, or `vision_llm`).

### How the three models actually work (and why we layer them)

A common point of confusion: "Llama 4 Scout is a vision + language model,
so the vision part must produce text that the language part then reasons
over." That mental model is intuitive but **not** how modern multimodal
LLMs work. Each tier in our ladder uses a fundamentally different
architecture, and the ladder exists precisely because each one fails
differently. Worth being precise about, since it shapes every guard in
the pipeline.

**1. Surya (Tier 2) — specialist OCR pipeline, *not* an LLM.**
Surya is a stack of small purpose-built models: layout detector → line
detector → text recogniser. It transcribes pixels into characters with
per-line bounding boxes and confidence scores. It has no language model,
no reasoning, and no notion of "what the document means." It cannot
invent words — at worst it returns garbled characters with low
confidence. We use it as the honest middle tier: when `pdfplumber`
returns nothing, we'd rather have noisy-but-truthful transcription than
smooth-but-fabricated narrative.

**2. Llama 3.3-70B (text extraction) — text-only LLM.**
This is the model we use to coerce extracted text into structured JSON
(invoice lines, claim lines, remittance entries). It is **text-only**;
it never sees images. It can hallucinate fields that aren't present in
the input text, which is why every text-LLM call goes through a
JSON-mode prompt with explicit "use null when uncertain, never invent
values" instructions, and why we always run regex first and only fall
back to the LLM when regex returns zero rows.

**3. Llama 4 Scout / Claude Sonnet 4.5 (Tier 3) — natively multimodal LLM.**
These are end-to-end vision-language models. There is **no internal
"OCR step then LLM step"** — the vision encoder turns pixels into
embeddings, the text tokenizer turns words into embeddings, and a
single transformer reasons over both streams simultaneously. The
practical consequence: the model can read handwriting and odd layouts
brilliantly, *and* it can confidently invent numbers from ambiguous
pixels because there's no intermediate text representation we can
verify. (We saw this in practice: Sonnet misread the `1425 / 1425`
Kroger stamp on Package 1 as `286 / 142` — fluent, confident, wrong.)
This is exactly why the rubric never trusts a single VLM call: every
vision output passes through plausibility guards (e.g. case counts
greater than 20 000 are dropped as PRO-number leakage) and falls back
to a deterministic narrative-pattern check (`Over/Short: 0`) when
numeric fields come back partial.

**The trust ladder, in one line each:**

| Tool | What it promises | What it never does |
| --- | --- | --- |
| `pdfplumber` (Tier 1) | Bytes-faithful text from the PDF text layer | Hallucinate — it's deterministic |
| Surya (Tier 2) | Transcribe pixels with confidence + bboxes | Generate or interpret |
| Llama 3.3-70B (text) | Coerce clean text into JSON shape | See images |
| Llama 4 Scout / Sonnet (Tier 3) | Read handwriting, signatures, weird layouts | Refuse to guess when pixels are ambiguous |
| Deterministic rubric | Decide VALID/INVALID/REVIEW from structured fields | Talk to any LLM in the decision path |

The rubric itself is the reason all of this is auditable: **no LLM
output ever reaches the verdict directly.** Every number that drives a
decision is either deterministic or has been through a plausibility
filter, and every reasoning step in the trace cites the document,
field, and tier that produced it.

---

## Setup

All Python code lives under `app/`. From the repo root:

```bash
cd app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — set OPENROUTER_API_KEY (or GROQ_API_KEY)
```

> The Curta case bundles are not committed to this repo (they're
> Curta-confidential). To run end-to-end, place the case folders at any
> path on disk and pass that path to the CLI / UI.

Models used (overridable via env):

- `GROQ_TEXT_MODEL=llama-3.3-70b-versatile`
- `GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct`

---

## Run

### CLI

```bash
cd app
PYTHONPATH=src python -m reconcile /path/to/case_folder -v

# Multiple cases at once:
PYTHONPATH=src python -m reconcile /path/to/case_1 /path/to/case_2 -v
```

Artifacts are written to `app/output/<case_name>/`:

- `report.json` — final `CaseReport` (decisions + reasoning)
- `matched.json` — canonical docs + entity-resolved claim lines
- `extractions.json` — raw per-document extraction payloads
- `pages/` — page images used by the vision extractors

### Streamlit UI

```bash
cd app
PYTHONPATH=src streamlit run webapp.py
```

- Pick a case folder from the sidebar (auto-discovered) or paste a path.
- Click **Run reconciliation**.
- Each line decision expands to show reasoning, evidence, gaps, and computed math.
- A debug tab at the bottom shows raw extracted documents per type.

---

## Shortage rubric (matches the appendix in the case PDF)

- **VALID** — line-level receiving evidence shows received < invoiced AND claim $ equals `(qty_invoiced − qty_received) × net_unit_price` within tolerance.
- **INVALID** — receiving confirms full qty, OR claim math contradicts the rubric.
- **NEEDS HUMAN REVIEW** — receiving is missing / ambiguous / aggregate-only, docs contradict, or the math is interpretable multiple ways.

Confidence starts at the decision's base, then is penalized for:

- claim-line self-math inconsistencies,
- unit-price divergence not explained by extracted promo,
- BOL flagged as possibly containing another shipment.

---

## Generalization / what's not hard-coded

- Nothing in the extractors, matchers, or decision engine checks case filenames.
- UPC ↔ material linkage is a **function**, not a lookup table — any Blue Diamond UPC with the material number as the last 5 digits resolves automatically.
- New document types plug in by (a) adding a `DocType` enum value, (b) a content/filename rule to the classifier, and (c) an extractor implementing the same `RenderedPDF → BaseDocument` shape.
- New deduction reason codes plug in by adding rule branches in `decide/`; the schema already carries `reason_code` and `reason_text` through the pipeline.

---

## Assumptions

Things I had to commit to in order to ship a tight prototype. Each one is
recoverable — the goal is to make them visible rather than buried.

1. **UPC ↔ Material number convention.** Blue Diamond's UPCs embed the
   material number as a suffix. The matcher accepts a full 5-digit suffix
   match (e.g. `1004157005278` ↔ material `05278`) and a last-4 fallback
   for cases where retailers drop the pack-count prefix
   (e.g. `100415701070` ↔ material `11070`). Any vendor that doesn't
   follow this convention would need an explicit crosswalk source — the
   matcher is the only place this assumption lives.
2. **"EA" on Blue Diamond invoices means cases for case-packed items.**
   Per the case-PDF glossary (`12-6 OZ CN` = 12 cans × 6 oz), the EA
   quantity reconciles 1-to-1 with the BOL's `CASES` column. The rubric
   compares invoice EA against BOL/POD case counts directly; it does
   **not** multiply by pack count.
3. **Net-of-promo unit price is the rubric's reference.** Off-invoice
   promos are subtracted per-unit (`unit_price − promo / qty`) and that
   net price is what the math test compares the claim's unit price
   against. This matches the case-PDF glossary's note that retailers
   typically calculate deductions on the net-of-promo price.
4. **Aggregate shortage allocated to a single line is treated as VALID
   when the math checks out, with no contradicting evidence.** The case
   PDF's rubric explicitly allows this ("an aggregate shortage that the
   retailer has allocated to that line, with no contradicting
   evidence"). I gate it at confidence 0.78 — below the 0.85 we'd give
   line-level proof — because the allocation itself is the retailer's
   call, not independently verified.
5. **A receiving stamp narrative that explicitly attests "Over/Short:
   0", "no shortage", or "delivered in full" counts as full-delivery
   evidence even when one of the two numeric fields is null.** Vision
   extractors sometimes recover the prose confidently while leaving a
   numeric field blank; ignoring the prose would route a clear
   no-shortage case to human review unnecessarily. Confidence drops to
   0.7 (vs. 0.8 for both-numbers-present) because we can't
   arithmetically cross-check.
6. **Remittance covers many invoices.** Each `-CM` line is matched to
   its claim by `seller_invoice_num` first, then by amount as a fallback.
   Case 2's remittance covers seven invoices and this works without any
   per-case logic.
7. **Terms-of-payment discount (e.g. `2%10 N30`) is legitimate, separate
   from any deduction.** The rubric never treats the terms discount as a
   shortage; it's parsed and surfaced as `terms_discount` per
   remittance line.
8. **Document classification is heuristic, not perfect.** Filename hints
   are weighted lower than content signatures; if both are silent we
   tag UNKNOWN and surface it in the report rather than guessing. New
   formats fail loudly, not silently.
9. **Surya OCR weights are downloaded once on first run** (~1 GB). The
   pipeline doesn't require GPU; CPU/MPS works. If Surya isn't
   available, the pipeline still runs — vision LLM is the next tier.
10. **The shortage rubric is the only rubric implemented.** Pricing,
    compliance, and unsaleables claims are detected and tagged
    (`ClaimType` enum + Kroger reason-code map) but routed to
    `needs_human_review` with a "no rubric yet" gap message. The schema
    and decision-layer plumbing for additional rubrics already exists.

---

## Known limitations

- POD/BOL stamp quantities rely on the vision model reading handwriting. The system flags aggregate-only stamps rather than trusting line allocations.
- Claim-line-to-invoice-line matching relies on the UPC-suffix convention; a vendor with a different UPC scheme would need a crosswalk source.
- Remittance parsing assumes the ACH text layout seen in Blue Diamond's samples; an EDI 820/812 source would need its own extractor (which is why the pipeline stays behind a `DocType` interface).

## What I'd build next with another week

1. **Human-in-the-loop queue UI**: surface `needs_human_review` lines as work items with one-click accept/dispute + feedback captured back into extractor eval.
2. **EDI 820 / 812 ingestion** alongside PDFs — same canonical schema.
3. **Extraction eval harness**: a few labeled cases, automated diff between regex and LLM, and confidence calibration.
4. **Promo / vendor-agreement integration**: net-of-promo pricing is brittle today; pulling promotional calendars eliminates the biggest ambiguity.
5. **Vision model abstraction** to A/B Llama 4 Scout against other providers for scanned BOLs.
