"""
Streamlit UI for the reconciliation engine.

Design goals for this revision:

  * A single focused dark theme with a purposeful accent palette — no stray
    colors, no per-card gradients. Gradients are reserved for the hero and
    the primary action button so they *mean* something.
  * A command bar at the top so the user always knows which case is loaded
    and has one-click access to run / re-run without scrolling.
  * A split layout under the hero: line decisions on the left (main content),
    a sticky evidence panel on the right (documents, receiving-stamp readout,
    audit summary). The audit-heavy right rail is what makes this feel like
    a product and not a Streamlit demo.
  * Dedicated receiving-stamp card that surfaces the shipped / received /
    delta numbers extracted by the new stamp-focused pipeline — the killer
    demo visual.
  * Proper empty, loading, and error states.

Everything below the "# UI" marker is presentation. The reconciliation logic
lives in `reconcile.pipeline`; this file must not implement business rules.

Run with:  streamlit run webapp.py
"""

from __future__ import annotations

import html
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import streamlit as st  # noqa: E402
import streamlit.components.v1 as components  # noqa: E402

from reconcile.config import SETTINGS  # noqa: E402
from reconcile.pipeline import run_case  # noqa: E402
from reconcile.schemas import ClaimType, Decision, DocType  # noqa: E402


st.set_page_config(
    page_title="Reconcile · Curta Case",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =============================================================================
# Style
# =============================================================================

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --bg-0: #07070d;
  --bg-1: #0c0c17;
  --bg-2: #12121f;
  --bg-3: #171727;
  --border: #22223a;
  --border-strong: #2e2e4a;
  --text-0: #f4f4f9;
  --text-1: #c9c9d6;
  --text-2: #8b8ba3;
  --text-3: #5c5c72;
  --accent: #818cf8;
  --accent-2: #c084fc;
  --green: #22c55e;
  --green-soft: #86efac;
  --red: #ef4444;
  --red-soft: #fca5a5;
  --amber: #f59e0b;
  --amber-soft: #fcd34d;
  --cyan: #22d3ee;
}

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
    letter-spacing: -0.011em;
    color: var(--text-0);
}

.stApp { background: var(--bg-0); }

.block-container {
    padding-top: 1.2rem !important;
    padding-bottom: 4rem !important;
    max-width: 1360px !important;
}

#MainMenu, footer, header {visibility: hidden;}

/* ---------------------------------------------------------------
 * Sidebar
 * --------------------------------------------------------------- */
section[data-testid="stSidebar"] {
    background: #08080f !important;
    border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] .stRadio > label {
    font-size: 13px !important;
    color: var(--text-1) !important;
}
section[data-testid="stSidebar"] .stRadio > div[role="radiogroup"] > label {
    background: #0e0e1a;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 12px !important;
    margin-bottom: 6px !important;
    transition: all .15s ease;
}
section[data-testid="stSidebar"] .stRadio > div[role="radiogroup"] > label:hover {
    border-color: var(--border-strong);
    transform: translateX(2px);
}

/* Keep Streamlit's native collapse/expand handle visible */
button[data-testid="stSidebarCollapsedControl"],
button[data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] {
    display: flex !important;
    visibility: visible !important;
    opacity: 1 !important;
    background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%) !important;
    color: #ffffff !important;
    border: 1px solid rgba(255,255,255,0.18) !important;
    border-radius: 10px !important;
    box-shadow: 0 8px 22px rgba(99,102,241,0.35) !important;
    width: 40px !important; height: 40px !important;
    align-items: center !important; justify-content: center !important;
    position: fixed !important; top: 14px !important; left: 14px !important;
    z-index: 999999 !important;
}
[data-testid="stSidebarCollapsedControl"] svg { fill: #fff !important; width: 20px !important; height: 20px !important; }

/* ---------------------------------------------------------------
 * Top command bar
 * --------------------------------------------------------------- */
.cmd-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 14px 18px;
    background: linear-gradient(90deg, rgba(99,102,241,0.05), rgba(139,92,246,0.02));
    border: 1px solid var(--border);
    border-radius: 14px;
    margin-bottom: 20px;
}
.cmd-bar .crumbs {
    display: flex; align-items: center; gap: 10px;
    font-size: 13px; color: var(--text-2);
    font-family: 'JetBrains Mono', monospace;
}
.cmd-bar .crumbs .sep { color: var(--text-3); }
.cmd-bar .crumbs .here { color: var(--text-0); }
.cmd-bar .env-chips { display: flex; gap: 8px; flex-wrap: wrap; }
.env-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 600; letter-spacing: 0.02em;
    background: #0e0e1a; border: 1px solid var(--border); color: var(--text-1);
}
.env-chip .cdot { width: 6px; height: 6px; border-radius: 50%; }
.env-chip.ok .cdot   { background: var(--green); box-shadow: 0 0 6px var(--green); }
.env-chip.warn .cdot { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
.env-chip.off .cdot  { background: var(--text-3); }

/* ---------------------------------------------------------------
 * Hero
 * --------------------------------------------------------------- */
.hero {
    position: relative;
    background:
        radial-gradient(1200px 280px at 0% 0%, rgba(129,140,248,0.18), transparent 60%),
        radial-gradient(900px 280px at 100% 100%, rgba(192,132,252,0.14), transparent 60%),
        linear-gradient(135deg, #111127 0%, #0a0a18 100%);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 26px 30px;
    margin-bottom: 22px;
    overflow: hidden;
}
.hero::after {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(180deg, transparent 60%, rgba(0,0,0,0.18));
    pointer-events: none;
}
.hero-row { display: flex; align-items: center; gap: 28px; position: relative; z-index: 1; }
.hero-left { flex: 1; min-width: 0; }
.hero-eyebrow {
    text-transform: uppercase;
    letter-spacing: 0.22em;
    color: var(--accent);
    font-size: 10px; font-weight: 700;
    margin-bottom: 10px;
}
.hero-title {
    font-size: 30px; font-weight: 800; color: var(--text-0);
    letter-spacing: -0.02em;
    margin: 0; line-height: 1.15;
}
.hero-meta {
    display: flex; gap: 22px; margin-top: 14px; flex-wrap: wrap;
    color: var(--text-2); font-size: 13px;
}
.hero-meta .k { color: var(--text-3); text-transform: uppercase; letter-spacing: 0.12em; font-size: 10px; }
.hero-meta .v { color: var(--text-0); font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 14px; margin-top: 2px;}
.hero-meta .v.money { color: var(--red-soft); }

.hero-right {
    flex-shrink: 0;
    display: flex; gap: 16px; align-items: center;
}

/* Donut-ish outcome ring */
.ring-wrap {
    width: 120px; height: 120px;
    position: relative;
    display: flex; align-items: center; justify-content: center;
}
.ring-wrap svg { transform: rotate(-90deg); }
.ring-label {
    position: absolute; inset: 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    text-align: center;
}
.ring-label .big {
    font-family: 'JetBrains Mono', monospace;
    font-size: 26px; font-weight: 700; color: var(--text-0);
    line-height: 1;
}
.ring-label .small {
    color: var(--text-2); font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.14em; margin-top: 4px;
}

/* ---------------------------------------------------------------
 * Metric tiles
 * --------------------------------------------------------------- */
.metric-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 22px;
}
.metric {
    background: linear-gradient(180deg, var(--bg-2) 0%, var(--bg-1) 100%);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    transition: border-color .15s ease, transform .15s ease;
    position: relative;
    overflow: hidden;
}
.metric:hover { border-color: var(--border-strong); transform: translateY(-1px); }
.metric::before {
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
    background: linear-gradient(180deg, var(--accent), var(--accent-2));
    opacity: 0.6;
}
.metric .k {
    color: var(--text-2); font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.14em; margin-bottom: 8px;
}
.metric .v {
    font-family: 'JetBrains Mono', monospace;
    font-size: 24px; font-weight: 700; color: var(--text-0);
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
}
.metric .sub { color: var(--text-2); font-size: 11px; margin-top: 6px; }
.metric .sub strong { color: var(--text-1); font-weight: 600; }
.metric.accent-green .v { color: var(--green-soft); }
.metric.accent-red   .v { color: var(--red-soft); }
.metric.accent-amber .v { color: var(--amber-soft); }

/* ---------------------------------------------------------------
 * Two-column layout: line cards (left), evidence rail (right)
 * --------------------------------------------------------------- */
.panel-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--accent);
    font-weight: 700;
    margin: 6px 0 10px 0;
    display: flex; align-items: center; gap: 8px;
}
.panel-title .count {
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-2); font-size: 11px; font-weight: 600;
    background: var(--bg-2); border: 1px solid var(--border);
    padding: 1px 8px; border-radius: 999px;
}

/* --- Line card ------------------------------------------------- */
.line-card {
    background: var(--bg-1);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px 22px;
    margin-bottom: 14px;
    transition: border-color .15s ease;
    position: relative;
}
.line-card:hover { border-color: var(--border-strong); }
.line-card::before {
    content: ""; position: absolute; left: 0; top: 14px; bottom: 14px;
    width: 3px; border-radius: 0 3px 3px 0;
}
.line-card.valid::before    { background: var(--red); }     /* valid deduction = money lost */
.line-card.invalid::before  { background: var(--green); }   /* invalid deduction = money recovered */
.line-card.review::before   { background: var(--amber); }

.line-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.line-idx { font-family: 'JetBrains Mono', monospace; color: var(--text-3); font-size: 12px; }
.line-upc {
    font-family: 'JetBrains Mono', monospace;
    background: var(--bg-2); border: 1px solid var(--border);
    color: #c7d2fe; padding: 3px 8px; border-radius: 6px; font-size: 12px;
}
.line-desc { color: var(--text-0); font-weight: 500; font-size: 15px; flex: 1; min-width: 240px; }
.line-amt {
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-0); font-size: 16px; font-weight: 600;
}

/* Pill */
.pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    border: 1px solid transparent;
}
.pill .dot { width: 7px; height: 7px; border-radius: 50%; box-shadow: 0 0 8px currentColor; }
.pill.valid    { background: rgba(239,68,68,0.1);  border-color: rgba(239,68,68,0.4);  color: var(--red-soft); }
.pill.valid .dot { background: var(--red); }
.pill.invalid  { background: rgba(34,197,94,0.1);  border-color: rgba(34,197,94,0.4);  color: var(--green-soft); }
.pill.invalid .dot { background: var(--green); }
.pill.review   { background: rgba(245,158,11,0.1); border-color: rgba(245,158,11,0.4); color: var(--amber-soft); }
.pill.review .dot { background: var(--amber); }

/* Claim-type pill — informational, distinct from decision pills.
   Lets a reviewer see at a glance which rubric family the line was
   classified as (shortage / pricing / compliance / unsaleables / other). */
.ctype-pill {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 9px; border-radius: 999px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.10em;
    background: var(--bg-2); border: 1px solid var(--border);
    color: var(--text-1);
}
.ctype-pill.shortage  { color: #c7d2fe; border-color: rgba(99,102,241,0.3); background: rgba(99,102,241,0.07); }
.ctype-pill.pricing   { color: #fcd34d; border-color: rgba(245,158,11,0.3); background: rgba(245,158,11,0.07); }
.ctype-pill.compliance{ color: #67e8f9; border-color: rgba(34,211,238,0.3); background: rgba(34,211,238,0.07); }
.ctype-pill.unsaleables { color: #fda4af; border-color: rgba(244,114,182,0.3); background: rgba(244,114,182,0.07); }
.ctype-pill.other     { color: var(--text-1); }
.ctype-pill.unknown   { color: var(--text-2); border-style: dashed; }
.ctype-pill .ctype-tag { font-size: 9px; opacity: 0.65; margin-right: 2px; }
.ctype-pill .ctype-conf {
    font-size: 9px; opacity: 0.7; padding-left: 5px; margin-left: 4px;
    border-left: 1px solid currentColor;
}

/* Confidence rail */
.conf-row { display: flex; align-items: center; gap: 12px; margin: 14px 0 6px; }
.conf-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.14em; color: var(--text-2); font-weight: 700; }
.conf-bar {
    flex: 1; max-width: 340px; height: 6px;
    background: var(--bg-3); border-radius: 999px; overflow: hidden;
    border: 1px solid var(--border);
}
.conf-fill {
    height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, var(--accent), var(--cyan));
    box-shadow: 0 0 12px rgba(129,140,248,0.5);
}
.conf-val { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-1); }
.conf-band {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px; color: var(--text-3);
    text-transform: uppercase; letter-spacing: 0.12em;
}

/* Sub-sections inside a line card */
.sub-title {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.16em;
    color: var(--accent); font-weight: 700;
    margin: 18px 0 8px 0; display: flex; align-items: center; gap: 8px;
}
.sub-title::before {
    content: ""; width: 14px; height: 1px; background: var(--accent); opacity: 0.6;
}

/* Reasoning timeline */
.timeline { border-left: 2px solid var(--border); padding-left: 16px; margin-left: 4px; }
.t-item { position: relative; padding: 4px 0 14px; }
.t-item:last-child { padding-bottom: 0; }
.t-item::before {
    content: ""; position: absolute;
    left: -22px; top: 11px;
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 3px rgba(129,140,248,0.18);
}
.t-step { color: var(--text-0); font-size: 13.5px; line-height: 1.55; }
.t-evidence-row { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }
.t-evidence {
    display: inline-flex; align-items: center; gap: 6px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; color: var(--text-2);
    background: var(--bg-2); border: 1px solid var(--border);
    padding: 3px 8px; border-radius: 6px;
}
.t-evidence .tag { color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; font-size: 10px; }

/* Gap */
.gap {
    background: rgba(245,158,11,0.06);
    border: 1px solid rgba(245,158,11,0.3);
    color: var(--amber-soft);
    padding: 10px 14px;
    border-radius: 10px;
    font-size: 13px;
    margin-top: 6px;
    display: flex; align-items: flex-start; gap: 10px;
}
.gap::before {
    content: "!"; display: inline-block;
    width: 18px; height: 18px; border-radius: 50%;
    background: var(--amber); color: #09090b;
    text-align: center; font-weight: 800; font-size: 11px; line-height: 18px;
    flex-shrink: 0; margin-top: 1px;
}

/* Computed values grid */
.kv-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 8px;
}
.kv-card {
    background: var(--bg-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px;
}
.kv-k { color: var(--text-3); font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em; }
.kv-v { color: var(--text-0); font-family: 'JetBrains Mono', monospace; font-size: 13px; margin-top: 3px; font-weight: 600; }

/* ---------------------------------------------------------------
 * Right rail — evidence / docs / stamp
 * --------------------------------------------------------------- */
.rail-card {
    background: var(--bg-1);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    margin-bottom: 14px;
}
.rail-title {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.16em;
    color: var(--accent); font-weight: 700;
    margin: 0 0 10px 0;
}

.doc-row {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 8px 0; border-bottom: 1px dashed var(--border);
}
.doc-row:last-child { border-bottom: none; }
.doc-row .dr-left { min-width: 0; flex: 1; }
.doc-row .dr-label {
    font-size: 11px; color: var(--text-2);
    text-transform: uppercase; letter-spacing: 0.1em;
}
.doc-row .dr-file {
    color: var(--text-0); font-family: 'JetBrains Mono', monospace;
    font-size: 11.5px; margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.doc-row .dr-status { flex-shrink: 0; }
.doc-row.missing .dr-file { color: var(--red-soft); }

.method-chip {
    display: inline-block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px; font-weight: 600;
    padding: 2px 8px; border-radius: 6px;
    background: var(--bg-2); border: 1px solid var(--border);
    color: var(--text-2);
}
.method-chip.text   { color: var(--green-soft); border-color: rgba(34,197,94,0.25); background: rgba(34,197,94,0.06); }
.method-chip.ocr    { color: var(--cyan);       border-color: rgba(34,211,238,0.25); background: rgba(34,211,238,0.06); }
.method-chip.vision { color: #d8b4fe;           border-color: rgba(192,132,252,0.25); background: rgba(192,132,252,0.06); }

/* Receiving stamp card */
.stamp-card {
    background: linear-gradient(180deg, var(--bg-1) 0%, var(--bg-2) 100%);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    margin-bottom: 14px;
}
.stamp-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 12px;
}
.stamp-nums { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.stamp-num {
    background: var(--bg-3); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; text-align: center;
}
.stamp-num .k { color: var(--text-3); font-size: 10px; text-transform: uppercase; letter-spacing: 0.14em; }
.stamp-num .v {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700; font-size: 22px; color: var(--text-0); margin-top: 4px;
}
.stamp-delta {
    margin-top: 10px; padding: 8px 12px;
    border-radius: 10px; font-size: 12px;
    display: flex; align-items: center; justify-content: space-between;
    font-family: 'JetBrains Mono', monospace;
}
.stamp-delta.ok  { background: rgba(34,197,94,0.08); color: var(--green-soft); border: 1px solid rgba(34,197,94,0.3); }
.stamp-delta.bad { background: rgba(239,68,68,0.08); color: var(--red-soft);   border: 1px solid rgba(239,68,68,0.3); }

.stamp-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.stamp-meta-chip {
    font-size: 11px; color: var(--text-2);
    background: var(--bg-3); border: 1px solid var(--border);
    padding: 4px 8px; border-radius: 6px;
}

/* Stamp status pill: describes what the stamp SAYS, not the line verdict.
   Keeping it visually distinct from decision pills avoids the "receiving
   says invalid / line says needs review" contradiction that confuses
   users. */
.stamp-status {
    display: inline-flex; align-items: center; gap: 6px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; font-weight: 600; letter-spacing: 0.06em;
    padding: 4px 10px; border-radius: 999px;
    border: 1px solid var(--border);
}
.stamp-status.full     { color: var(--green-soft); border-color: rgba(34,197,94,0.3);  background: rgba(34,197,94,0.08); }
.stamp-status.short    { color: var(--red-soft);   border-color: rgba(239,68,68,0.3);  background: rgba(239,68,68,0.08); }
.stamp-status.partial  { color: var(--amber);      border-color: rgba(245,158,11,0.3); background: rgba(245,158,11,0.08); }
.stamp-status.unread   { color: var(--text-3);     border-color: var(--border);        background: var(--bg-3); }

/* ---------------------------------------------------------------
 * Buttons
 * --------------------------------------------------------------- */
.stButton > button {
    white-space: nowrap !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
    letter-spacing: 0.01em !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(90deg, #6366f1 0%, #8b5cf6 100%) !important;
    border: 1px solid rgba(255,255,255,0.1) !important;
    color: white !important;
    padding: 10px 26px !important;
    box-shadow: 0 6px 24px rgba(99,102,241,0.35) !important;
    min-width: 220px !important;
}
.stButton > button[kind="primary"]:hover { filter: brightness(1.08); transform: translateY(-1px); }
.stButton > button[kind="secondary"] {
    background: var(--bg-2) !important;
    border: 1px solid var(--border-strong) !important;
    color: var(--text-0) !important;
    padding: 10px 20px !important;
    min-width: 160px !important;
}

/* ---------------------------------------------------------------
 * Loading skeleton
 * --------------------------------------------------------------- */
.skeleton {
    background: linear-gradient(90deg, var(--bg-2) 0%, var(--bg-3) 50%, var(--bg-2) 100%);
    background-size: 200% 100%;
    animation: shimmer 1.4s infinite linear;
    border-radius: 10px;
}
@keyframes shimmer { 0% { background-position: -200% 0; } 100% { background-position: 200% 0; } }

/* ---------------------------------------------------------------
 * Misc
 * --------------------------------------------------------------- */
.section-title {
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.16em;
    color: var(--accent); font-weight: 700;
    margin: 18px 0 10px 0;
}

details.raw-doc {
    background: var(--bg-1); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 14px; margin-top: 8px;
}
details.raw-doc summary {
    cursor: pointer; color: var(--text-2); font-size: 12px;
    font-family: 'JetBrains Mono', monospace;
}
details.raw-doc[open] summary { color: var(--text-0); margin-bottom: 8px; }

.empty-hero {
    padding: 40px 32px;
    background: var(--bg-1); border: 1px dashed var(--border-strong);
    border-radius: 16px; text-align: center;
}
.empty-hero .big { color: var(--text-0); font-size: 18px; font-weight: 600; margin-bottom: 6px; }
.empty-hero .sub { color: var(--text-2); font-size: 13px; }

/* ----------------------------- Debug doc tables ----------------------------
 * Used by the "raw extracted documents" expander to render each doc's
 * structured contents as a clean table instead of a JSON blob.
 * --------------------------------------------------------------------- */
.dbg-meta {
    font-family: 'JetBrains Mono', monospace; font-size: 11px;
    color: var(--text-2); margin: 0 0 10px 0;
    display: flex; flex-wrap: wrap; gap: 14px; row-gap: 4px;
}
.dbg-meta .k { color: var(--text-3); margin-right: 6px; }
.dbg-meta .v { color: var(--text-1); }
.dbg-section {
    margin: 12px 0 4px 0;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;
    color: var(--text-3); font-weight: 700;
}
.dbg-kv {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 8px 14px;
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 14px;
    margin-bottom: 10px;
}
.dbg-kv .row { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.dbg-kv .row .k {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.10em;
    color: var(--text-3);
}
.dbg-kv .row .v {
    font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-0);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.dbg-kv .row .v.muted { color: var(--text-3); }
.dbg-table {
    width: 100%; border-collapse: collapse; margin: 4px 0 12px 0;
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
}
.dbg-table th {
    text-align: left; padding: 7px 10px; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.10em; color: var(--text-3);
    border-bottom: 1px solid var(--border); background: rgba(255,255,255,0.02);
    font-weight: 600;
}
.dbg-table td {
    padding: 6px 10px; font-size: 12px; color: var(--text-1);
    border-bottom: 1px solid rgba(255,255,255,0.04);
    vertical-align: top;
}
.dbg-table tr:last-child td { border-bottom: none; }
.dbg-table td.mono { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-0); }
.dbg-table td.num  { font-family: 'JetBrains Mono', monospace; font-size: 11px; text-align: right; color: var(--text-0); }
.dbg-table td.neg  { color: #fca5a5; }
.dbg-table td.muted { color: var(--text-3); }
.dbg-table tr.cross td { opacity: 0.65; text-decoration: line-through; }
.dbg-pill {
    display: inline-block; padding: 1px 8px; border-radius: 999px;
    font-family: 'JetBrains Mono', monospace; font-size: 10px;
    border: 1px solid var(--border); color: var(--text-2);
    background: rgba(255,255,255,0.03);
}
.dbg-pill.warn { color: #fbbf24; border-color: rgba(245,158,11,0.45); background: rgba(245,158,11,0.08); }
.dbg-pill.ok   { color: var(--green-soft); border-color: rgba(34,197,94,0.30); background: rgba(34,197,94,0.06); }
.dbg-pill.info { color: var(--cyan); border-color: rgba(34,211,238,0.30); background: rgba(34,211,238,0.06); }
.dbg-banner {
    border-radius: 10px; padding: 10px 14px; margin: 6px 0 14px 0;
    font-size: 12px;
}
.dbg-banner.warn {
    background: rgba(245,158,11,0.10);
    border: 1px solid rgba(245,158,11,0.45);
    color: #fbbf24;
}
.dbg-page-title {
    margin: 14px 0 6px 0; font-size: 12px; color: var(--text-1); font-weight: 600;
}
.dbg-page-title .tag-cross {
    font-size: 11px; color: #fbbf24; margin-left: 10px; font-weight: 500;
}
.dbg-page-title .tag-primary {
    font-size: 11px; color: var(--text-3); margin-left: 10px; font-weight: 500;
}
.dbg-page-title .tag-meta {
    font-size: 11px; color: var(--text-3); margin-left: 10px;
}
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# =============================================================================
# Helpers
# =============================================================================

_DECISION_TO_KIND = {
    Decision.VALID: ("valid", "Valid", "Deduction is justified by evidence"),
    Decision.INVALID: ("invalid", "Invalid", "Deduction not supported — recoverable"),
    Decision.NEEDS_HUMAN_REVIEW: ("review", "Needs review", "Evidence incomplete or conflicting"),
}


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _html(markup: str) -> None:
    """
    Render raw HTML via `st.markdown` *without* Streamlit's markdown parser
    misinterpreting the Python-source indentation as a code block.

    Any line with 4+ leading spaces is treated as code by CommonMark. Since
    our f-string HTML is indented inside functions, we strip all common
    leading whitespace *and* any per-line leading indent before rendering.
    This keeps the HTML source readable in `webapp.py` while still producing
    clean, styled cards in the UI.
    """
    cleaned = textwrap.dedent(markup).strip("\n")
    # textwrap.dedent only removes the *common* leading whitespace; when the
    # triple-quoted block mixes indents (e.g. nested blocks) some lines can
    # still start with 4+ spaces and get eaten as a code block. Strip every
    # line's leading whitespace — HTML is whitespace-insensitive so this is
    # safe.
    cleaned = "\n".join(ln.lstrip() for ln in cleaned.splitlines())
    st.markdown(cleaned, unsafe_allow_html=True)


def _money(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "—"
    if sign:
        return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"
    return f"${abs(v):,.2f}"


def _method_chip(method_value: str) -> str:
    tag = method_value.lower()
    if tag.startswith("text"):
        cls = "text"
    elif tag.startswith("ocr"):
        cls = "ocr"
    else:
        cls = "vision"
    return f"<span class='method-chip {cls}'>{_esc(method_value)}</span>"


def _outcome_ring(valid: int, invalid: int, review: int) -> str:
    total = max(1, valid + invalid + review)
    # We frame the case outcome from a "recovered dollars" perspective:
    # invalid = deduction rejected = recovered for the supplier.
    # We show the largest segment's % in the ring center, biased toward
    # the recovery narrative.
    c = 50  # circumference/100 → stroke dasharray base
    r = 42
    circ = 2 * 3.14159 * r
    seg_invalid = circ * (invalid / total)
    seg_valid = circ * (valid / total)
    seg_review = circ * (review / total)
    offset_v = seg_invalid
    offset_r = seg_invalid + seg_valid

    # Headline number: % auto-decided.
    auto = valid + invalid
    pct = int(round((auto / total) * 100))
    return f"""
    <div class="ring-wrap">
      <svg width="120" height="120" viewBox="0 0 120 120">
        <circle cx="60" cy="60" r="{r}" fill="none" stroke="#1a1a2a" stroke-width="10" />
        <circle cx="60" cy="60" r="{r}" fill="none" stroke="#22c55e" stroke-width="10"
                stroke-dasharray="{seg_invalid:.2f} {circ - seg_invalid:.2f}"
                stroke-dashoffset="0" stroke-linecap="butt" />
        <circle cx="60" cy="60" r="{r}" fill="none" stroke="#ef4444" stroke-width="10"
                stroke-dasharray="{seg_valid:.2f} {circ - seg_valid:.2f}"
                stroke-dashoffset="-{offset_v:.2f}" stroke-linecap="butt" />
        <circle cx="60" cy="60" r="{r}" fill="none" stroke="#f59e0b" stroke-width="10"
                stroke-dasharray="{seg_review:.2f} {circ - seg_review:.2f}"
                stroke-dashoffset="-{offset_r:.2f}" stroke-linecap="butt" />
      </svg>
      <div class="ring-label">
        <div class="big">{pct}%</div>
        <div class="small">Auto-decided</div>
      </div>
    </div>
    """


# =============================================================================
# Components
# =============================================================================

def render_command_bar(case_path: Path | None, ocr_ok: bool) -> None:
    llm_state = "ok" if SETTINGS.has_llm else "warn"
    llm_label = (
        f"{SETTINGS.provider_display} · {SETTINGS.text_model.split('/')[-1]}"
        if SETTINGS.has_llm
        else f"No {SETTINGS.provider_display} key"
    )
    ocr_state = "ok" if ocr_ok else "off"
    ocr_label = "Surya OCR" if ocr_ok else "OCR disabled"

    crumbs = "<span class='sep'>◇</span> <span>reconcile</span>"
    if case_path:
        crumbs += (
            f" <span class='sep'>/</span> "
            f"<span class='here'>{_esc(case_path.name)}</span>"
        )

    _html(f"""
        <div class="cmd-bar">
          <div class="crumbs">{crumbs}</div>
          <div class="env-chips">
            <div class="env-chip {llm_state}"><span class="cdot"></span>{_esc(llm_label)}</div>
            <div class="env-chip {ocr_state}"><span class="cdot"></span>{_esc(ocr_label)}</div>
          </div>
        </div>
    """)


def render_hero(report) -> None:
    decisions = report.line_decisions
    valid = sum(1 for d in decisions if d.decision == Decision.VALID)
    invalid = sum(1 for d in decisions if d.decision == Decision.INVALID)
    review = sum(1 for d in decisions if d.decision == Decision.NEEDS_HUMAN_REVIEW)

    inv_num = report.invoice_number or "—"
    ded = _money(report.total_deduction_claimed) if report.total_deduction_claimed else "—"

    ring_html = _outcome_ring(valid, invalid, review)

    _html(f"""
        <div class="hero">
          <div class="hero-row">
            <div class="hero-left">
              <div class="hero-eyebrow">Reconciliation case</div>
              <div class="hero-title">{_esc(report.case_name)}</div>
              <div class="hero-meta">
                <div>
                  <div class="k">Invoice</div>
                  <div class="v">{_esc(inv_num)}</div>
                </div>
                <div>
                  <div class="k">Claimed deduction</div>
                  <div class="v money">{_esc(ded)}</div>
                </div>
                <div>
                  <div class="k">Lines</div>
                  <div class="v">{len(decisions)}</div>
                </div>
                <div>
                  <div class="k">Recoverable</div>
                  <div class="v">{invalid}/{len(decisions)}</div>
                </div>
              </div>
            </div>
            <div class="hero-right">
              {ring_html}
            </div>
          </div>
        </div>
    """)


def render_metrics(report) -> None:
    decisions = report.line_decisions
    valid = sum(1 for d in decisions if d.decision == Decision.VALID)
    invalid = sum(1 for d in decisions if d.decision == Decision.INVALID)
    review = sum(1 for d in decisions if d.decision == Decision.NEEDS_HUMAN_REVIEW)
    total_dollars = sum(abs(d.claimed_amount or 0) for d in decisions)
    recoverable = sum(
        abs(d.claimed_amount or 0) for d in decisions if d.decision == Decision.INVALID
    )

    _html(f"""
        <div class="metric-grid">
          <div class="metric">
            <div class="k">Lines claimed</div>
            <div class="v">{len(decisions)}</div>
            <div class="sub">from deduction doc</div>
          </div>
          <div class="metric accent-red">
            <div class="k">$ in dispute</div>
            <div class="v">${total_dollars:,.2f}</div>
            <div class="sub">across all lines</div>
          </div>
          <div class="metric accent-green">
            <div class="k">$ recoverable</div>
            <div class="v">${recoverable:,.2f}</div>
            <div class="sub"><strong>{invalid}</strong> invalid deduction{'s' if invalid != 1 else ''}</div>
          </div>
          <div class="metric accent-amber">
            <div class="k">Needs review</div>
            <div class="v">{review}</div>
            <div class="sub">evidence gaps surfaced</div>
          </div>
        </div>
    """)


def render_pill(decision: Decision) -> str:
    kind, label, _ = _DECISION_TO_KIND[decision]
    return f"<span class='pill {kind}'><span class='dot'></span>{label}</span>"


_CLAIM_TYPE_LABEL = {
    ClaimType.SHORTAGE: "Shortage",
    ClaimType.PRICING: "Pricing",
    ClaimType.COMPLIANCE: "Compliance",
    ClaimType.UNSALEABLES: "Unsaleables",
    ClaimType.OTHER: "Other",
    ClaimType.UNKNOWN: "Unknown",
}


def render_claim_type_pill(d) -> str:
    """
    Show what rubric family the line was classified as. Always rendered
    so reviewers can see at a glance whether the shortage rubric was
    actually applicable. Uses a neutral, informational style so it
    doesn't compete with the decision pill for attention.
    """
    ctype = getattr(d, "claim_type_detected", None) or ClaimType.UNKNOWN
    label = _CLAIM_TYPE_LABEL.get(ctype, ctype.value)
    rubric = getattr(d, "rubric_applied", "shortage")
    # When we ran the shortage rubric we don't show "rubric: shortage"
    # again — it'd be redundant with the type pill. We *do* call out the
    # case where we recognised the type but had no rubric to apply, so
    # the reviewer understands why the verdict is human-review.
    extra = ""
    if rubric == "unsupported":
        extra = "<span class='ctype-conf'>no rubric · routed for review</span>"
    return (
        f"<span class='ctype-pill {ctype.value}' "
        f"title='Detected claim type: {_esc(label)}'>"
        f"<span class='ctype-tag'>type</span>{_esc(label)}{extra}"
        f"</span>"
    )


def render_line_card(d) -> None:
    kind, label, _ = _DECISION_TO_KIND[d.decision]
    amt = _money(d.claimed_amount, sign=True) if d.claimed_amount is not None else "—"
    conf_pct = int(round((d.confidence or 0) * 100))

    head = f"""
    <div class="line-head">
      <span class="line-idx">LINE #{d.claim_index:02d}</span>
      <span class="line-upc">UPC {_esc(d.upc or '—')}</span>
      <span class="line-desc">{_esc(d.description or '(no description extracted)')}</span>
      <span class="line-amt">{amt}</span>
      {render_claim_type_pill(d)}
      {render_pill(d.decision)}
    </div>
    <div class="conf-row">
      <span class="conf-label">Confidence</span>
      <div class="conf-bar"><div class="conf-fill" style="width:{conf_pct}%;"></div></div>
      <span class="conf-val">{d.confidence:.2f}</span>
      <span class="conf-band">· {_esc(d.confidence_band.value)}</span>
    </div>
    """

    # Reasoning timeline
    timeline_html = ""
    if d.reasoning:
        steps_html = []
        for step in d.reasoning:
            ev_bits = []
            for ev in step.evidence:
                snippet = (f" · {_esc(ev.snippet)}" if ev.snippet else "")
                field = _esc(ev.field or "")
                ev_bits.append(
                    f"<span class='t-evidence'><span class='tag'>"
                    f"{_esc(ev.doc_type.value)}</span>{field}{snippet}</span>"
                )
            ev_row = (
                f"<div class='t-evidence-row'>{''.join(ev_bits)}</div>"
                if ev_bits else ""
            )
            steps_html.append(
                f"<div class='t-item'><div class='t-step'>{_esc(step.step)}</div>"
                f"{ev_row}</div>"
            )
        timeline_html = (
            "<div class='sub-title'>Reasoning trace</div>"
            f"<div class='timeline'>{''.join(steps_html)}</div>"
        )

    # Evidence gaps
    gaps_html = ""
    if d.evidence_gaps:
        items = "".join(
            f"<div class='gap'>{_esc(g)}</div>" for g in d.evidence_gaps
        )
        gaps_html = (
            "<div class='sub-title'>Evidence gaps</div>"
            f"<div>{items}</div>"
        )

    # Computed values
    kv_html = ""
    if d.computed:
        cards = []
        for k, v in d.computed.items():
            if isinstance(v, float):
                vv = f"{v:,.2f}"
            elif isinstance(v, int):
                vv = f"{v:,}"
            else:
                vv = str(v)
            cards.append(
                f"<div class='kv-card'><div class='kv-k'>{_esc(k)}</div>"
                f"<div class='kv-v'>{_esc(vv)}</div></div>"
            )
        kv_html = (
            "<div class='sub-title'>Computed values</div>"
            f"<div class='kv-grid'>{''.join(cards)}</div>"
        )

    st.markdown(
        f"<div class='line-card {kind}'>{head}{timeline_html}{gaps_html}{kv_html}</div>",
        unsafe_allow_html=True,
    )


def render_docs_rail(matched) -> None:
    rows_html: list[str] = []

    def _doc_row(label: str, doc) -> None:
        if doc is None:
            rows_html.append(
                f"""
                <div class="doc-row missing">
                  <div class="dr-left">
                    <div class="dr-label">{_esc(label)}</div>
                    <div class="dr-file">— not in bundle —</div>
                  </div>
                  <div class="dr-status">
                    <span class="method-chip">missing</span>
                  </div>
                </div>
                """
            )
            return
        fname = Path(doc.source_path).name
        rows_html.append(
            f"""
            <div class="doc-row">
              <div class="dr-left">
                <div class="dr-label">{_esc(label)}</div>
                <div class="dr-file" title="{_esc(fname)}">{_esc(fname)}</div>
              </div>
              <div class="dr-status">
                {_method_chip(doc.extraction_method.value)}
              </div>
            </div>
            """
        )

    _doc_row("Sales invoice", matched.invoice)
    _doc_row("Bill of lading", matched.bol)
    _doc_row("Proof of delivery", matched.pod)
    _doc_row("Remittance", matched.remittance)
    _doc_row("Deduction claim", matched.claim)

    _html(f"""
        <div class="rail-card">
          <div class="rail-title">Documents in bundle</div>
          {''.join(rows_html)}
        </div>
    """)


def render_stamp_card(matched) -> None:
    """Surface the receiving-stamp numbers from BOL or POD when present."""
    receiving = None
    source_doc = None
    source_label = None

    if matched.bol and matched.bol.receiving and (
        matched.bol.receiving.total_cases_shipped
        or matched.bol.receiving.total_cases_received
        or matched.bol.receiving.has_receiving_stamp
    ):
        receiving = matched.bol.receiving
        source_doc = matched.bol
        source_label = "BOL stamp"
    elif matched.pod and matched.pod.receiving and (
        matched.pod.receiving.total_cases_shipped
        or matched.pod.receiving.total_cases_received
    ):
        receiving = matched.pod.receiving
        source_doc = matched.pod
        source_label = "POD"

    if not receiving:
        return

    shipped = receiving.total_cases_shipped
    received = receiving.total_cases_received
    shipped_txt = f"{int(shipped):,}" if shipped is not None else "—"
    received_txt = f"{int(received):,}" if received is not None else "—"

    # Delta
    delta_html = ""
    if shipped is not None and received is not None:
        delta = int(received) - int(shipped)
        if delta >= 0:
            delta_html = (
                f"<div class='stamp-delta ok'>"
                f"<span>Δ received − shipped</span>"
                f"<span>+{delta:,} · full delivery</span></div>"
            )
        else:
            delta_html = (
                f"<div class='stamp-delta bad'>"
                f"<span>Δ received − shipped</span>"
                f"<span>{delta:,} · shortage</span></div>"
            )

    chips: list[str] = []
    if receiving.has_receiving_stamp:
        chips.append("<span class='stamp-meta-chip'>✔ stamped</span>")
    if receiving.stamp_notes:
        note = receiving.stamp_notes
        if len(note) > 100:
            note = note[:97] + "…"
        chips.append(f"<span class='stamp-meta-chip'>{_esc(note)}</span>")
    if source_doc is not None:
        chips.append(
            f"<span class='stamp-meta-chip'>"
            f"source · {_esc(source_label)} ({_esc(source_doc.extraction_method.value)})"
            f"</span>"
        )

    # Status pill describes what THE STAMP SAYS (a per-document signal), not
    # the case verdict. Using a separate visual vocabulary from the
    # line-level decision pills prevents the "receiving says invalid / line
    # says needs review" contradiction when the two answer different
    # questions.
    if shipped is None or received is None:
        if receiving.has_receiving_stamp:
            status_html = "<span class='stamp-status partial'>● stamped · numbers unclear</span>"
        else:
            status_html = "<span class='stamp-status unread'>● no stamp read</span>"
    elif shipped == received:
        status_html = "<span class='stamp-status full'>● full delivery</span>"
    elif received < shipped:
        status_html = "<span class='stamp-status short'>● aggregate shortage</span>"
    else:
        status_html = "<span class='stamp-status partial'>● over-receipt</span>"

    _html(f"""
        <div class="stamp-card">
          <div class="stamp-head">
            <div class="rail-title" style="margin:0;">Receiving evidence</div>
            {status_html}
          </div>
          <div class="stamp-nums">
            <div class="stamp-num">
              <div class="k">Cases shipped</div>
              <div class="v">{shipped_txt}</div>
            </div>
            <div class="stamp-num">
              <div class="k">Cases received</div>
              <div class="v">{received_txt}</div>
            </div>
          </div>
          {delta_html}
          <div class="stamp-meta">{''.join(chips)}</div>
        </div>
    """)


def render_audit_rail(report, matched) -> None:
    """Tiny card summarizing how the extraction went — confidence per doc."""
    rows = []
    for label, doc in [
        ("Invoice", matched.invoice),
        ("BOL", matched.bol),
        ("POD", matched.pod),
        ("Remittance", matched.remittance),
        ("Claim", matched.claim),
    ]:
        if doc is None:
            continue
        conf_pct = int(round(doc.extraction_confidence * 100))
        rows.append(
            f"""
            <div style="display:flex;align-items:center;gap:10px;margin:8px 0;">
              <div style="flex:0 0 80px;color:var(--text-2);font-size:11px;
                          text-transform:uppercase;letter-spacing:0.12em;">{_esc(label)}</div>
              <div style="flex:1;height:5px;background:var(--bg-3);border-radius:999px;
                          overflow:hidden;border:1px solid var(--border);">
                <div style="height:100%;width:{conf_pct}%;background:linear-gradient(90deg,var(--accent),var(--cyan));"></div>
              </div>
              <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text-1);
                          min-width:34px;text-align:right;">{doc.extraction_confidence:.2f}</div>
            </div>
            """
        )
    if not rows:
        return
    _html(f"""
        <div class="rail-card">
          <div class="rail-title">Extraction confidence</div>
          {''.join(rows)}
        </div>
    """)


# =============================================================================
# Case discovery
# =============================================================================

# Filename keywords used to identify what kind of document a PDF is. The
# discovery filter requires a directory to cover at least two of these
# categories before considering it a "case bundle" — this prevents the parent
# "Curta Take Home Challenge" folder (which only holds a single case-prompt
# PDF) from showing up as a selectable bundle.
_DOC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "invoice": ("invoice", "inv_", "inv "),
    "bol": ("bill of lading", "bol", "b.o.l", "delivery order"),
    "pod": ("pod", "proof of delivery", "receiving"),
    "remit": ("check", "remit", "payment", "remittance"),
    "claim": ("deduction", "claim", "chargeback", "debit note"),
}


def _categorize_pdf(name: str) -> str | None:
    """Best-guess the document kind from a PDF filename."""
    n = name.lower()
    for kind, keywords in _DOC_KEYWORDS.items():
        for kw in keywords:
            if kw in n:
                return kind
    return None


# Path-segment denylist: any directory whose path contains one of these
# segments is treated as noise (never a case bundle). Keeps virtualenvs,
# git internals, byte-compiled caches, and Python package metadata folders
# from polluting the sidebar dropdown.
_NOISE_DIR_NAMES: frozenset[str] = frozenset({
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".cursor",
    "build",
    "dist",
    ".tox",
    "site-packages",
    "smoke_pages",
})

_NOISE_SUFFIXES: tuple[str, ...] = (
    ".dist-info",
    ".egg-info",
)


def _is_noise_dir(p: Path) -> bool:
    """True if any path segment matches a denylisted dir or suffix."""
    for seg in p.parts:
        if seg in _NOISE_DIR_NAMES:
            return True
        for suf in _NOISE_SUFFIXES:
            if seg.endswith(suf):
                return True
    return False


# Files whose presence strongly indicates a *source-code project root*
# rather than a deduction case bundle. If we see one of these alongside
# the supported docs we filter the folder out — saves users from seeing
# `/app/` (requirements.txt + README.md) or any other repo we happen to
# walk into.
_PROJECT_ROOT_MARKERS: frozenset[str] = frozenset({
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "go.mod",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
    "gemfile",
    "dockerfile",
    "makefile",
})


def _looks_like_project_root(d: Path) -> bool:
    """True if the directory contains a code-project marker."""
    try:
        for entry in d.iterdir():
            if entry.is_file() and entry.name.lower() in _PROJECT_ROOT_MARKERS:
                return True
    except Exception:
        pass
    return False


def _find_case_folders() -> list[Path]:
    """
    Walk the workspace looking for case bundles.

    A directory qualifies as a bundle when it contains **at least two**
    files we know how to read (any supported format — PDF, text, or
    image). We deliberately do NOT require filenames to match category
    keywords, because real-world bundles often have opaque names like
    `87213712507144.pdf` or `0090407673.txt`. The parent
    `Curta Take Home Challenge` folder still gets filtered out
    automatically because it only holds a single case-prompt PDF.

    Noise directories (virtualenvs, git internals, package metadata,
    byte-compile caches, build artefacts, our own output dir) are
    pruned via a path-segment denylist so the dropdown only shows real
    candidate bundles.
    """
    from reconcile.ingest.document_loader import (  # noqa: WPS433 - lazy import
        IMAGE_EXTS,
        PDF_EXTS,
        TEXT_EXTS,
    )
    supported_exts = PDF_EXTS | TEXT_EXTS | IMAGE_EXTS

    parent = ROOT.parent
    bundles: list[Path] = []
    seen: set[Path] = set()
    for entry in parent.rglob("*"):
        if not entry.is_dir() or entry in seen:
            continue
        # Skip our own output directory and its descendants.
        if SETTINGS.output_dir == entry or SETTINGS.output_dir in entry.parents:
            continue
        # Skip well-known noise directories (.venv, .git, dist-info, etc.).
        if _is_noise_dir(entry):
            continue
        # Skip code-project roots (anything containing requirements.txt,
        # pyproject.toml, package.json, etc.).
        if _looks_like_project_root(entry):
            continue
        try:
            files = [
                p for p in entry.iterdir()
                if p.is_file() and p.suffix.lower() in supported_exts
            ]
        except Exception:
            continue
        if len(files) < 2:
            continue
        bundles.append(entry)
        seen.add(entry)
    return sorted(bundles)


# =============================================================================
# Sidebar
# =============================================================================

def sidebar() -> tuple[Path | None, bool]:
    """Returns (selected_case_path, ocr_ok)."""
    with st.sidebar:
        _html("""
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
              <div style="width:28px;height:28px;border-radius:8px;
                          background:linear-gradient(135deg,#6366f1,#8b5cf6);
                          display:flex;align-items:center;justify-content:center;
                          font-weight:700;color:#fff;">◆</div>
              <div>
                <div style="font-weight:700;color:#f5f5ff;font-size:16px;line-height:1;">Reconcile</div>
                <div style="color:#6b7280;font-size:11px;margin-top:2px;">CPG deduction validation</div>
              </div>
            </div>
            <div style="margin:14px 0;height:1px;background:#1a1a2e;"></div>
        """)

        # Environment probes --------------------------------------------------
        try:
            from reconcile.ingest.ocr import ocr_available  # noqa: WPS433
            ocr_ok = ocr_available()
        except Exception:
            ocr_ok = False

        _html(f"""
            <div style="font-size:10px;color:#6b7280;text-transform:uppercase;
                        letter-spacing:0.14em;margin-bottom:6px;">Environment</div>
            <div class="env-chip {'ok' if SETTINGS.has_llm else 'warn'}"
                 style="margin-bottom:6px;display:flex;justify-content:flex-start;width:fit-content;">
              <span class="cdot"></span>
              {SETTINGS.provider_display + ' · ' + SETTINGS.text_model.split('/')[-1] if SETTINGS.has_llm else 'No API key set'}
            </div>
            <div class="env-chip {'ok' if ocr_ok else 'off'}" style="display:flex;justify-content:flex-start;width:fit-content;">
              <span class="cdot"></span>
              {'Surya OCR available' if ocr_ok else 'Surya OCR disabled'}
            </div>
            <div style="margin:14px 0;height:1px;background:#1a1a2e;"></div>
        """)

        # Case picker ---------------------------------------------------------
        st.markdown(
            "<div style='font-size:10px;color:#6b7280;text-transform:uppercase;"
            "letter-spacing:0.14em;margin-bottom:8px;'>Case bundles</div>",
            unsafe_allow_html=True,
        )

        discovered = _find_case_folders()
        labels = [
            str(p.relative_to(ROOT.parent)) if ROOT.parent in p.parents else str(p)
            for p in discovered
        ]

        if labels:
            choice = st.radio(
                "Select a case",
                options=labels,
                index=0,
                label_visibility="collapsed",
            )
        else:
            choice = None
            st.caption("No case bundles auto-detected.")

        st.markdown(
            "<div style='margin:14px 0 4px 0;font-size:10px;color:#6b7280;"
            "text-transform:uppercase;letter-spacing:0.14em;'>Or specify a path</div>",
            unsafe_allow_html=True,
        )
        manual = st.text_input(
            " ", value="", placeholder="/path/to/case/folder", label_visibility="collapsed"
        )

        # Models panel --------------------------------------------------------
        _html(f"""
            <div style="margin-top:20px;padding:12px;background:#0e0e1a;border:1px solid #1a1a2e;
                        border-radius:10px;">
              <div style="font-size:10px;color:#6b7280;text-transform:uppercase;
                          letter-spacing:0.14em;margin-bottom:8px;">Models</div>
              <div style="font-family:'JetBrains Mono',monospace;font-size:10.5px;color:#9ca3af;line-height:1.7;">
                text · {_esc(SETTINGS.text_model)}<br/>
                vision · {_esc(SETTINGS.vision_model)}<br/>
                ocr · surya-ocr (local)
              </div>
            </div>
        """)

    if manual:
        return Path(manual).expanduser().resolve(), ocr_ok
    if labels and choice in labels:
        return discovered[labels.index(choice)], ocr_ok
    return None, ocr_ok


# =============================================================================
# Main
# =============================================================================

def render_report(result: dict) -> None:
    report = result["report"]
    matched = result["matched"]

    render_hero(report)
    render_metrics(report)

    # Two-column layout below the hero
    left, right = st.columns([7, 4], gap="large")

    with left:
        # Global warnings first
        if report.global_warnings:
            st.markdown("<div class='panel-title'>Case-level warnings</div>", unsafe_allow_html=True)
            for w in report.global_warnings:
                st.markdown(f"<div class='gap'>{_esc(w)}</div>", unsafe_allow_html=True)

        st.markdown(
            f"<div class='panel-title'>Line decisions"
            f"<span class='count'>{len(report.line_decisions)}</span></div>",
            unsafe_allow_html=True,
        )
        if not report.line_decisions:
            st.markdown(
                "<div class='gap'>No claim lines were extracted for this case. "
                "This usually means the deduction document is image-only or its "
                "layout is new — add a Groq key and re-run to use vision.</div>",
                unsafe_allow_html=True,
            )
        else:
            for d in report.line_decisions:
                render_line_card(d)

    with right:
        render_docs_rail(matched)
        render_stamp_card(matched)
        render_audit_rail(report, matched)

    # Debug expander at the bottom — every document rendered as clean
    # tables, with raw JSON tucked behind a nested expander per tab.
    with st.expander("Debug · raw extracted documents", expanded=False):
        tab_labels = ["Invoice", "BOL", "POD", "Remittance", "Claim"]
        tabs = st.tabs(tab_labels)
        docs = [
            matched.invoice,
            matched.bol,
            matched.pod,
            matched.remittance,
            matched.claim,
        ]
        for tab, doc, label in zip(tabs, docs, tab_labels):
            with tab:
                render_doc_debug(label, doc)


# =============================================================================
# Debug · tabular renderers
# =============================================================================
#
# The "Debug · raw extracted documents" expander used to dump each
# document as a JSON blob (`st.json`). That worked for sanity-checking
# but was hostile to skim during a live walkthrough — the structured
# fields that matter for reconciliation (line items, prices, stamp
# numbers) were buried in 30+ keys per doc.
#
# These helpers render every extracted document as a small set of
# coordinated tables: a header card (key/value), one or more item
# tables (lines / receiving), and any supplementary structured fields.
# Raw JSON stays available behind a nested "Show raw JSON" expander.


def _fmt_num(v, fmt: str = "{:g}") -> str:
    if v is None:
        return "—"
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return _esc(v)


def _fmt_money(v) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return _esc(v)
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


def _fmt_text(v, *, dash_when_empty: bool = True) -> str:
    if v is None:
        return "—" if dash_when_empty else ""
    s = str(v).strip()
    if not s:
        return "—" if dash_when_empty else ""
    return _esc(s)


def _kv_grid(items: list[tuple[str, Any]]) -> None:
    """Render a responsive key/value grid for document headers."""
    rows: list[str] = []
    for key, val in items:
        if val is None or (isinstance(val, str) and not val.strip()):
            v_html = "<div class='v muted'>—</div>"
        elif isinstance(val, bool):
            v_html = (
                "<div class='v'>" + ("yes" if val else "no") + "</div>"
            )
        elif isinstance(val, (int, float)):
            v_html = f"<div class='v'>{_esc(val)}</div>"
        else:
            v_html = f"<div class='v' title='{_esc(val)}'>{_esc(val)}</div>"
        rows.append(
            f"<div class='row'><div class='k'>{_esc(key)}</div>{v_html}</div>"
        )
    if not rows:
        return
    _html(f"<div class='dbg-kv'>{''.join(rows)}</div>")


def _section(title: str) -> None:
    _html(f"<div class='dbg-section'>{_esc(title)}</div>")


def _table(
    columns: list[tuple[str, str]],  # (label, css class for cells: "", "mono", "num")
    rows: list[list[tuple[str, str]]],  # per-cell (text, extra-class)
    *,
    empty_msg: str = "No rows.",
) -> None:
    if not rows:
        _html(f"<div class='dbg-meta'><span class='v muted'>{_esc(empty_msg)}</span></div>")
        return
    head = "".join(
        f"<th style='text-align:{'right' if cls == 'num' else 'left'}'>{_esc(label)}</th>"
        for label, cls in columns
    )
    body_rows: list[str] = []
    for row in rows:
        # row may be a list of (text, extra) cells, or the special form
        # [("__row_class__", "cross"), ...cells]
        row_cls = ""
        cells = row
        if cells and cells[0] and cells[0][0] == "__row_class__":
            row_cls = cells[0][1]
            cells = cells[1:]
        cell_html = []
        for (text, extra), (_, base_cls) in zip(cells, columns):
            classes = " ".join(c for c in (base_cls, extra) if c)
            cell_html.append(
                f"<td class='{classes}'>{text}</td>" if classes else f"<td>{text}</td>"
            )
        body_rows.append(
            f"<tr class='{row_cls}'>{''.join(cell_html)}</tr>" if row_cls
            else f"<tr>{''.join(cell_html)}</tr>"
        )
    _html(
        "<table class='dbg-table'>"
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )


def _render_doc_meta(doc) -> None:
    """One-line metadata strip at the top of each debug tab."""
    method = _esc(doc.extraction_method.value)
    conf = doc.extraction_confidence
    pages = getattr(doc, "pages", None)
    src = Path(doc.source_path).name
    parse_warnings = getattr(doc, "parse_warnings", None) or []
    pieces = [
        f"<span><span class='k'>method</span><span class='v'>{method}</span></span>",
        f"<span><span class='k'>confidence</span><span class='v'>{conf:.2f}</span></span>",
    ]
    if pages:
        pieces.append(
            f"<span><span class='k'>pages</span><span class='v'>{pages}</span></span>"
        )
    pieces.append(
        f"<span><span class='k'>file</span><span class='v' title='{_esc(doc.source_path)}'>"
        f"{_esc(src)}</span></span>"
    )
    _html(f"<div class='dbg-meta'>{''.join(pieces)}</div>")
    if parse_warnings:
        items = "<br/>• ".join(_esc(w) for w in parse_warnings)
        _html(
            f"<div class='dbg-banner warn'><strong>Parse notes</strong><br/>• {items}</div>"
        )


# ----------------------------- Invoice ----------------------------------------


def render_invoice_debug(inv) -> None:
    if inv is None:
        return
    _section("Header")
    _kv_grid([
        ("Invoice #", inv.invoice_number),
        ("Invoice date", inv.invoice_date),
        ("PO #", inv.po_number),
        ("Delivery #", inv.delivery_number),
        ("Carrier", inv.carrier),
        ("Terms", inv.terms_of_payment),
        ("Bill to", inv.bill_to),
        ("Ship to", inv.ship_to),
        ("Subtotal", _fmt_money(inv.subtotal)),
        ("Total", _fmt_money(inv.total_amount)),
    ])

    _section(f"Line items ({len(inv.lines)})")
    cols = [
        ("#", "mono"),
        ("Material", "mono"),
        ("Description", ""),
        ("Qty", "num"),
        ("UoM", "mono"),
        ("Unit $", "num"),
        ("Promo $", "num"),
        ("Net unit $", "num"),
        ("Gross $", "num"),
    ]
    rows: list[list[tuple[str, str]]] = []
    for ln in inv.lines:
        promo = ln.off_invoice_promo
        promo_cls = "neg" if (promo is not None and promo < 0) else ""
        rows.append([
            (_fmt_text(ln.line_no), ""),
            (_fmt_text(ln.material_number), ""),
            (_fmt_text(ln.description), ""),
            (_fmt_num(ln.quantity), ""),
            (_fmt_text(ln.unit_label), ""),
            (_fmt_money(ln.unit_price), ""),
            (_fmt_money(promo), promo_cls),
            (_fmt_money(ln.net_unit_price), ""),
            (_fmt_money(ln.gross_value), ""),
        ])
    _table(cols, rows, empty_msg="No invoice lines extracted.")


# ----------------------------- BOL --------------------------------------------


def render_bol_debug(bol) -> None:
    """
    Render a BOL document as: header card + per-page line tables (with
    cross-shipment lines visually flagged) + receiving evidence table.
    """
    if bol is None:
        return

    _section("Header")
    _kv_grid([
        ("BOL #", bol.bol_number),
        ("PRO #", bol.pro_number),
        ("PO #", bol.po_number),
        ("Ship date", bol.ship_date),
        ("Carrier", bol.carrier),
        ("Ship to", bol.ship_to),
        ("Total cases", _fmt_num(bol.total_cases)),
        ("Pages (primary)", ", ".join(map(str, bol.primary_shipment_pages or [])) or "—"),
        ("Pages (cross-ship)", ", ".join(map(str, bol.cross_shipment_pages or [])) or "—"),
    ])

    cross_pages = set(bol.cross_shipment_pages or [])
    cross_details = {
        d.get("page_number"): d
        for d in (bol.cross_shipment_details or [])
        if isinstance(d, dict)
    }

    if bol.content_belongs_to_different_shipment and cross_pages:
        cross_list = sorted(cross_pages)
        _html(
            "<div class='dbg-banner warn'>"
            "<strong>Cross-shipment content detected.</strong> "
            f"Page(s) {cross_list} carry a different BOL/PO/ship-to than "
            "page 1. Their lines are shown below for transparency but are "
            "<u>excluded</u> from the reconciliation rubric."
            "</div>"
        )

    if bol.lines:
        _section(f"Line items ({len(bol.lines)})")

        # Order pages in the order they first appear so we don't reshuffle.
        pages_in_order: list[int] = []
        seen: set[int] = set()
        for ln in bol.lines:
            pn = ln.page_number if ln.page_number is not None else 1
            if pn not in seen:
                seen.add(pn)
                pages_in_order.append(pn)

        cols = [
            ("Material", "mono"),
            ("SKU", "mono"),
            ("Description", ""),
            ("Cases", "num"),
            ("Weight", "num"),
        ]
        for pn in pages_in_order:
            page_lines = [ln for ln in bol.lines if (ln.page_number or 1) == pn]
            is_cross = pn in cross_pages
            if is_cross:
                detail = cross_details.get(pn) or {}
                meta_bits = []
                for k in ("bol_number", "po_number", "ship_to"):
                    v = detail.get(k)
                    if v:
                        meta_bits.append(f"{k.replace('_', ' ')}: {v}")
                tag = "<span class='tag-cross'>↪ different shipment</span>"
                meta = (
                    f"<span class='tag-meta'>({_esc(' · '.join(meta_bits))})</span>"
                    if meta_bits else ""
                )
            else:
                tag = "<span class='tag-primary'>primary shipment</span>"
                meta = ""
            _html(
                f"<div class='dbg-page-title'>Page {pn}{tag}{meta}</div>"
            )
            rows = []
            for ln in page_lines:
                row: list[tuple[str, str]] = []
                if is_cross:
                    row.append(("__row_class__", "cross"))
                row.extend([
                    (_fmt_text(ln.material_number), ""),
                    (_fmt_text(ln.customer_sku), ""),
                    (_fmt_text(ln.description), ""),
                    (_fmt_num(ln.cases), ""),
                    (_fmt_num(ln.weight, "{:,.2f}"), ""),
                ])
                rows.append(row)
            _table(cols, rows, empty_msg="No lines on this page.")

    _render_receiving(bol.receiving)


# ----------------------------- POD --------------------------------------------


def render_pod_debug(pod) -> None:
    if pod is None:
        return
    _section("Header")
    _kv_grid([
        ("Referenced BOL", pod.referenced_bol),
        ("Referenced PO", pod.referenced_po),
    ])
    _render_receiving(pod.receiving)


def _render_receiving(receiving) -> None:
    """Shared receiving-evidence renderer used by BOL and POD."""
    if receiving is None:
        _section("Receiving evidence")
        _html("<div class='dbg-meta'><span class='v muted'>None captured.</span></div>")
        return

    has_stamp = bool(receiving.has_receiving_stamp)
    short = bool(receiving.aggregate_shortage)
    pill_stamp = (
        "<span class='dbg-pill ok'>stamp present</span>" if has_stamp
        else "<span class='dbg-pill'>no stamp</span>"
    )
    pill_short = (
        "<span class='dbg-pill warn'>aggregate shortage</span>" if short
        else "<span class='dbg-pill ok'>no aggregate shortage</span>"
    )
    _html(
        f"<div class='dbg-section' style='display:flex;align-items:center;"
        f"justify-content:space-between;'>"
        f"<span>Receiving evidence</span>"
        f"<span style='display:inline-flex;gap:6px;'>{pill_stamp}{pill_short}</span>"
        f"</div>"
    )

    shipped = receiving.total_cases_shipped
    received = receiving.total_cases_received
    delta_v = None
    if shipped is not None and received is not None:
        try:
            delta_v = float(received) - float(shipped)
        except (TypeError, ValueError):
            delta_v = None
    _kv_grid([
        ("Cases shipped", _fmt_num(shipped)),
        ("Cases received", _fmt_num(received)),
        ("Delta", _fmt_num(delta_v) if delta_v is not None else "—"),
        ("Stamp notes", receiving.stamp_notes),
    ])

    excs = list(receiving.line_level_exceptions or [])
    if excs:
        _section("Line-level exceptions")
        cols = [("Note", "")]
        rows = [[(_fmt_text(e), "")] for e in excs]
        _table(cols, rows)


# ----------------------------- Remittance -------------------------------------


def render_remit_debug(remit) -> None:
    if remit is None:
        return
    _section("Header")
    _kv_grid([
        ("Originator", remit.originator),
        ("Effective date", remit.effective_date),
        ("Lines", len(remit.lines)),
    ])

    _section(f"Remittance lines ({len(remit.lines)})")
    cols = [
        ("Seller invoice #", "mono"),
        ("Type", ""),
        ("Invoice $", "num"),
        ("Terms disc $", "num"),
        ("Net paid $", "num"),
    ]
    rows = []
    for ln in remit.lines:
        kind = (
            "<span class='dbg-pill warn'>credit memo</span>"
            if ln.is_credit_memo
            else "<span class='dbg-pill info'>invoice</span>"
        )
        amt_cls = "neg" if (ln.invoice_amount is not None and ln.invoice_amount < 0) else ""
        rows.append([
            (_fmt_text(ln.seller_invoice_num), ""),
            (kind, ""),
            (_fmt_money(ln.invoice_amount), amt_cls),
            (_fmt_money(ln.terms_discount), ""),
            (_fmt_money(ln.net_amount_paid), ""),
        ])
    _table(cols, rows, empty_msg="No remittance lines extracted.")


# ----------------------------- Claim ------------------------------------------


def render_claim_debug(claim) -> None:
    if claim is None:
        return
    _section("Header")
    _kv_grid([
        ("Invoice #", claim.invoice_number),
        ("PO #", claim.po_number),
        ("Deduction $", _fmt_money(claim.deduction_amount)),
        ("Gross invoice $", _fmt_money(claim.gross_invoice_amount)),
        ("Net invoice $", _fmt_money(claim.net_invoice_amount)),
        ("Discount $", _fmt_money(claim.discount_amount)),
    ])

    _section(f"Claim lines ({len(claim.lines)})")
    cols = [
        ("UPC", "mono"),
        ("Description", ""),
        ("Qty", "num"),
        ("Unit $", "num"),
        ("Amount", "num"),
        ("Reason", "mono"),
        ("Type", ""),
    ]
    rows = []
    for ln in claim.lines:
        amt_cls = "neg" if (ln.adj_amount is not None and ln.adj_amount < 0) else ""
        ctype = ln.claim_type.value if hasattr(ln.claim_type, "value") else str(ln.claim_type)
        ctype_pill = (
            f"<span class='dbg-pill info' title='confidence "
            f"{ln.claim_type_confidence:.2f}'>{_esc(ctype)}</span>"
        )
        reason = (
            ln.reason_code or "—"
        )
        if ln.reason_text:
            reason = f"{reason} · {ln.reason_text}"
        rows.append([
            (_fmt_text(ln.upc), ""),
            (_fmt_text(ln.description), ""),
            (_fmt_num(ln.adj_qty), ""),
            (_fmt_money(ln.unit_price), ""),
            (_fmt_money(ln.adj_amount), amt_cls),
            (_fmt_text(reason), ""),
            (ctype_pill, ""),
        ])
    _table(cols, rows, empty_msg="No claim lines extracted.")


# ----------------------------- Dispatch ---------------------------------------


def render_doc_debug(label: str, doc) -> None:
    """Dispatch a document to its tabular renderer based on label."""
    if doc is None:
        st.caption(f"No {label.lower()} document in this bundle.")
        return
    _render_doc_meta(doc)
    if label == "Invoice":
        render_invoice_debug(doc)
    elif label == "BOL":
        render_bol_debug(doc)
    elif label == "POD":
        render_pod_debug(doc)
    elif label == "Remittance":
        render_remit_debug(doc)
    elif label == "Claim":
        render_claim_debug(doc)
    else:
        st.json(doc.model_dump())
        return
    with st.expander("Show raw JSON", expanded=False):
        st.json(doc.model_dump())


def render_empty_state() -> None:
    _html("""
        <div class="empty-hero">
          <div class="big">Pick a case bundle to get started</div>
          <div class="sub">Use the sidebar to select package 1 or package 2 —
          or paste a path to any folder containing an invoice, BOL, POD,
          remittance, and deduction PDF.</div>
        </div>
    """)


def render_loading_state(case_path: Path) -> None:
    _html(f"""
        <div class="rail-card" style="text-align:center;padding:26px 20px;">
          <div style="color:var(--text-0);font-size:15px;font-weight:600;margin-bottom:4px;">
            Reconciling {_esc(case_path.name)}…
          </div>
          <div style="color:var(--text-2);font-size:12px;margin-bottom:16px;">
            Classifying PDFs → extracting lines → matching → deciding
          </div>
          <div class="skeleton" style="height:18px;margin-bottom:10px;"></div>
          <div class="skeleton" style="height:12px;width:70%;margin:0 auto 6px;"></div>
          <div class="skeleton" style="height:12px;width:55%;margin:0 auto;"></div>
        </div>
    """)


def _sidebar_reopener_html() -> None:
    """Inject a floating 'Show sidebar' button that works even when Streamlit's
    native collapse control is hidden. Stable DOM selectors with a CSS
    fallback."""
    components.html(
        """
        <style>
          .reopen-sb-btn {
            position: fixed; top: 10px; left: 10px; z-index: 2147483647;
            background: linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%);
            color: #fff; border: 1px solid rgba(255,255,255,0.2); border-radius: 10px;
            padding: 8px 14px; font: 600 12px/1 'Inter',system-ui,sans-serif;
            box-shadow: 0 6px 22px rgba(99,102,241,0.45); cursor: pointer;
          }
          .reopen-sb-btn:hover { filter: brightness(1.12); }
        </style>
        <button class="reopen-sb-btn" id="__reopen_sb">☰ Show sidebar</button>
        <script>
          const btn = document.getElementById('__reopen_sb');
          btn.addEventListener('click', () => {
            const doc = window.parent && window.parent.document;
            if (!doc) return;
            const selectors = [
              'button[data-testid="stSidebarCollapsedControl"]',
              'button[data-testid="collapsedControl"]',
              '[data-testid="stSidebarCollapsedControl"]',
              '[data-testid="collapsedControl"]',
              'button[kind="header"][aria-label*="sidebar" i]',
              'button[aria-label*="expand sidebar" i]',
              'button[aria-label*="open sidebar" i]',
            ];
            for (const s of selectors) {
              const el = doc.querySelector(s);
              if (el) { el.click(); return; }
            }
            const sb = doc.querySelector('section[data-testid="stSidebar"]');
            if (sb) {
              sb.style.transform = 'none';
              sb.style.marginLeft = '0';
              sb.style.width = '336px';
              sb.style.minWidth = '244px';
              sb.style.visibility = 'visible';
              sb.setAttribute('aria-expanded', 'true');
            }
          });
        </script>
        """,
        height=52,
    )


def main() -> None:
    _sidebar_reopener_html()

    case_path, ocr_ok = sidebar()
    render_command_bar(case_path, ocr_ok)

    # Sub-hero header (always visible so the app has a "brand" feel)
    st.markdown(
        """
        <div style="margin-bottom:18px;">
          <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.22em;
                      color:#8b8ba3;font-weight:700;">Curta · Founding-Engineer Case</div>
          <div style="font-size:34px;font-weight:800;color:#f5f5ff;
                      letter-spacing:-0.02em;margin-top:4px;">Reconciliation Engine</div>
          <div style="color:#a1a1aa;font-size:14px;max-width:720px;margin-top:8px;">
            Ingests invoice / BOL / POD / remittance / deduction bundles and decides
            <span style="color:#fca5a5;font-weight:600;">valid</span>,
            <span style="color:#86efac;font-weight:600;">invalid</span>, or
            <span style="color:#fcd34d;font-weight:600;">needs review</span> —
            with a full evidence-backed audit trail.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not case_path:
        render_empty_state()
        st.stop()

    if not case_path.exists():
        st.markdown(
            f"<div class='gap'>Path does not exist: <code>{_esc(case_path)}</code></div>",
            unsafe_allow_html=True,
        )
        st.stop()

    # Action bar -------------------------------------------------------------
    c1, c2, c3 = st.columns([3, 2, 5])
    run_btn = c1.button("▶  Run reconciliation", type="primary", use_container_width=False)
    rerun = c2.button("↻  Re-run (clear cache)", type="secondary", use_container_width=False)

    cached = st.session_state.get("last_result")
    cached_path = st.session_state.get("last_path")

    if rerun:
        st.session_state.pop("last_result", None)
        st.session_state.pop("last_path", None)
        cached = None
        cached_path = None

    should_run = run_btn or (cached is not None and cached_path == str(case_path))
    if not should_run:
        _html(f"""
            <div style="color:#8b8ba3;font-size:13px;margin-top:14px;
                        display:flex;align-items:center;gap:8px;">
              <span style="opacity:0.7;">Ready:</span>
              <span style="color:#c7d2fe;font-family:'JetBrains Mono',monospace;
                           background:#12121f;border:1px solid #22223a;padding:3px 10px;
                           border-radius:6px;font-size:12px;">{_esc(case_path)}</span>
            </div>
        """)
        st.stop()

    if cached is None or cached_path != str(case_path):
        with st.spinner(""):
            render_loading_state(case_path)
            result = run_case(case_path)
            st.session_state["last_result"] = result
            st.session_state["last_path"] = str(case_path)
        st.rerun()
    else:
        result = cached

    render_report(result)


if __name__ == "__main__":
    main()
