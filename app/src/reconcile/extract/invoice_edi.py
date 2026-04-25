"""
SAP EDI INVOIC02 (IDoc XML) sales-invoice extractor.

Some retailers send invoices as **SAP IDoc XML** rather than human-
readable PDFs. The structure is fully specified — segment names like
`E1EDK01`, `E1EDP01`, `E1EDP19` always carry the same fields — so we
can parse them deterministically with high confidence (>= the PDF
regex path) and don't need to round-trip through an LLM.

We intentionally support only `INVOIC02` (the message type used by
`test_case-3/0090407673_Sales_Invoice.txt`), which is by far the most
common SAP outbound invoice IDoc. Other IDoc message types
(e.g. `INVOIC01`, `ORDERS05`) are returned as `None` so the caller
can fall through to the next extraction tier.

Field map (only the segments we actually need):

    E1EDK01.BELNR            -> invoice number
    E1EDK02 QUALF=009 DATUM  -> invoice date    (YYYYMMDD)
    E1EDK02 QUALF=001 BELNR  -> PO number
    E1EDK02 QUALF=012 BELNR  -> delivery / BOL number
    E1EDK18 QUALF=005 ZTERM_TXT -> terms-of-payment text
    E1EDKA1 PARVW=AG  NAME1  -> bill-to (sold-to)
    E1EDKA1 PARVW=RG  NAME1  -> alt bill-to (payer)
    E1EDKA1 PARVW=WE  NAME1  -> header-level ship-to (when present)
    E1EDS01 SUMID=011 SUMME  -> subtotal (gross of discount)
    E1EDS01 SUMID=Z01 SUMME  -> net payable (after discount)

Per-line `E1EDP01`:

    POSEX                       -> line number
    MENGE / MENEE               -> quantity / UoM
    PSTYV                       -> item category. We skip non-good
                                   service lines (e.g. `YB99`) which
                                   carry MENGE=0.
    E1EDP19 QUALF=001 IDTNR     -> customer material / SKU
    E1EDP19 QUALF=002 IDTNR     -> seller material number
    E1EDP19 QUALF=002 KTEXT     -> description
    E1EDP19 QUALF=002 IDTNR_EXTERNAL -> GTIN (for retailer UPC fallback)
    E1EDP05 KOTXT="Gross Value"      KRATE -> gross unit price
    E1EDP05 KOTXT="Off Invoice Pro*" KRATE -> per-unit promo amount
    E1EDP05 KOTXT="Net Value for Item" KRATE -> net unit price
    E1EDP26 QUALF=003 BETRG          -> net line amount (after promo)
    E1EDP26 QUALF=010 BETRG          -> gross line amount
    E1EDPA1 PARVW=WE NAME1           -> per-line ship-to (when present)

Note on signs: SAP IDocs use trailing-minus for negatives
(e.g. `2.40-` means -2.40). `parse_amount` already handles that.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from reconcile.schemas import (
    ExtractionMethod,
    InvoiceLine,
    SalesInvoice,
)

log = logging.getLogger("reconcile.extract.invoice_edi")


# Matches an EDI INVOIC envelope so we don't try to parse arbitrary XML.
# We accept either a real `<?xml?>` declaration or just the bare envelope
# element (some upstream systems strip the prolog).
_ENVELOPE_RE = re.compile(
    r"<\s*INVOIC0\d\b|<\s*IDOC\b[^>]*>\s*<\s*EDI_DC40\b",
    re.IGNORECASE,
)


def looks_like_edi_invoic(raw: str | None) -> bool:
    """Cheap pre-check: does this text contain an INVOIC IDoc envelope?"""
    if not raw:
        return False
    if "<INVOIC02" not in raw and "<INVOIC01" not in raw and "<EDI_DC40" not in raw:
        return False
    return _ENVELOPE_RE.search(raw) is not None


# We strip everything up to either the XML prolog (`<?xml`) or the
# outermost envelope element (`<INVOIC0…>` / `<IDOC>`), and everything
# after the matching close tag. This lets us accept text that the
# document loader has decorated with page banners (`[page 1]\n`) or
# other padding without confusing ElementTree.
_ENVELOPE_OPEN_RE = re.compile(
    r"<\s*\?xml\b|<\s*INVOIC0\d\b|<\s*IDOC\b", re.IGNORECASE
)


def _isolate_xml_envelope(raw: str) -> str | None:
    """Return the substring starting at the first XML prolog/envelope tag.

    Trims any leading text the renderer may have prepended (e.g.
    `[page 1]\\n`) so ElementTree sees a valid root element. Trailing
    text after the envelope is harmless to ElementTree.
    """
    m = _ENVELOPE_OPEN_RE.search(raw)
    if not m:
        return None
    return raw[m.start():]


# --------------------------------------------------------------------------
# Low-level XML helpers
# --------------------------------------------------------------------------


def _text(el: ET.Element | None, tag: str) -> str | None:
    """Return the stripped text of the first child `<tag>` of `el`, or None."""
    if el is None:
        return None
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    s = child.text.strip()
    return s or None


def _findall(parent: ET.Element, tag: str) -> list[ET.Element]:
    return list(parent.findall(tag))


def _parse_amount(text: str | None) -> float | None:
    """SAP-style numeric: trailing minus = negative."""
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    neg = s.endswith("-")
    if neg:
        s = s[:-1].strip()
    try:
        v = float(s.replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


def _seg_with_qualf(parent: ET.Element, tag: str, qualf: str) -> ET.Element | None:
    """Return the first `<tag>` child whose `<QUALF>` text equals `qualf`."""
    for el in parent.findall(tag):
        if (_text(el, "QUALF") or "").strip() == qualf:
            return el
    return None


def _seg_with_sumid(parent: ET.Element, tag: str, sumid: str) -> ET.Element | None:
    """Return the first `<tag>` child whose `<SUMID>` text equals `sumid`.

    Used for `E1EDS01` summary segments which key on `<SUMID>` rather than
    the more common `<QUALF>`.
    """
    for el in parent.findall(tag):
        if (_text(el, "SUMID") or "").strip() == sumid:
            return el
    return None


def _seg_with_partner(parent: ET.Element, tag: str, parvw: str) -> ET.Element | None:
    """Find a partner segment (`E1EDKA1`/`E1EDPA1`) whose `<PARVW>` equals `parvw`."""
    for el in parent.findall(tag):
        if (_text(el, "PARVW") or "").strip() == parvw:
            return el
    return None


def _format_partner(p: ET.Element | None) -> str | None:
    """Compact 'NAME1, STRAS, ORT01 REGIO PSTLZ' for header display."""
    if p is None:
        return None
    city_line = " ".join(
        x for x in (
            _text(p, "ORT01"),
            _text(p, "REGIO"),
            _text(p, "PSTLZ"),
        )
        if x
    )
    parts = [_text(p, "NAME1"), _text(p, "STRAS"), city_line or None]
    cleaned = [s for s in parts if s]
    text = ", ".join(cleaned)
    return text or None


def _format_yyyymmdd(s: str | None) -> str | None:
    """`20250720` -> `2025-07-20`. Returns input unchanged when not 8 digits."""
    if not s:
        return None
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


# --------------------------------------------------------------------------
# Line-level parsing
# --------------------------------------------------------------------------


# Item categories we treat as "real goods" lines. Anything else (e.g.
# `YB99` = freight/service flag, `TANN` = free-of-charge sample) is
# either a synthetic line with MENGE=0 or a freight pass-through; we
# don't want them in the line-item table because they have no price
# and would just be noise on the UI.
_REAL_GOODS_PSTYV = {"TAN", "ZTAN", "TAS"}


def _is_real_goods_line(p: ET.Element) -> bool:
    """True iff this `E1EDP01` is a sellable goods line, not a service flag."""
    pstyv = (_text(p, "PSTYV") or "").upper()
    if pstyv and pstyv not in _REAL_GOODS_PSTYV:
        return False
    qty_text = _text(p, "MENGE")
    qty = _parse_amount(qty_text)
    if qty is None or qty == 0:
        # Genuine 0-qty real-goods lines are rare; SAP usually reserves
        # them for service/promo flags, so skipping is safe.
        return False
    return True


def _line_pricing(p: ET.Element) -> dict[str, float | None]:
    """Pull gross/promo/net unit prices from the per-line `E1EDP05` segments.

    Returns:
        - gross_unit: Gross Value KRATE
        - promo_unit: per-unit promo (negative when present)
        - net_unit:   Net Value for Item KRATE
    """
    gross_unit: float | None = None
    promo_unit: float | None = None
    net_unit: float | None = None
    for el in p.findall("E1EDP05"):
        kotxt = (_text(el, "KOTXT") or "").lower()
        krate = _parse_amount(_text(el, "KRATE"))
        if krate is None:
            continue
        if "gross value" in kotxt:
            gross_unit = krate
        elif "off invoice" in kotxt or "off-invoice" in kotxt or "promo" in kotxt:
            # ALCKZ='-' is the canonical signal but not all senders set it
            # consistently; the KRATE text itself carries the sign.
            promo_unit = krate
        elif "net value" in kotxt:
            net_unit = krate
    return {
        "gross_unit": gross_unit,
        "promo_unit": promo_unit,
        "net_unit": net_unit,
    }


def _line_amounts(p: ET.Element) -> dict[str, float | None]:
    """Pull line-level totals from `E1EDP26` segments.

    QUALF map (only what we need):
      003 -> Net line amount (after promo)
      010 -> Gross line amount
    """
    gross_total = _parse_amount(_text(_seg_with_qualf(p, "E1EDP26", "010"), "BETRG"))
    net_total = _parse_amount(_text(_seg_with_qualf(p, "E1EDP26", "003"), "BETRG"))
    return {"gross_total": gross_total, "net_total": net_total}


def _parse_invoice_line(p: ET.Element) -> InvoiceLine | None:
    if not _is_real_goods_line(p):
        return None

    line_no = _text(p, "POSEX")
    qty = _parse_amount(_text(p, "MENGE"))
    uom = _text(p, "MENEE") or "EA"

    seg_seller = _seg_with_qualf(p, "E1EDP19", "002")
    seg_buyer = _seg_with_qualf(p, "E1EDP19", "001")

    material = _text(seg_seller, "IDTNR") if seg_seller is not None else None
    description = _text(seg_seller, "KTEXT") if seg_seller is not None else None
    gtin = (
        _text(seg_seller, "IDTNR_EXTERNAL") if seg_seller is not None else None
    )
    customer_sku = _text(seg_buyer, "IDTNR") if seg_buyer is not None else None

    pricing = _line_pricing(p)
    amounts = _line_amounts(p)

    # Prefer net unit price as the "unit_price" we hand to the rubric.
    # The PDF-format invoices we already support give us NET unit price
    # (after promo) under the Off-Invoice line — so to keep the rubric
    # math consistent, treat IDoc's "Net Value for Item" as `unit_price`
    # and IDoc's "Gross Value" minus "Net Value" as the per-unit promo,
    # then multiply by qty for the dollar promo amount.
    unit_price = pricing["net_unit"] or pricing["gross_unit"]
    promo_total: float | None = None
    if pricing["promo_unit"] is not None and qty:
        # `promo_unit` from the IDoc is the per-unit reduction; we want
        # a negative line-level dollar amount in `off_invoice_promo`.
        per_unit = abs(pricing["promo_unit"])
        promo_total = -round(per_unit * qty, 2)
    elif (
        pricing["gross_unit"] is not None
        and pricing["net_unit"] is not None
        and qty
    ):
        # Fallback when the explicit promo line is missing but there's
        # a gross-vs-net delta we can derive.
        delta = round(pricing["gross_unit"] - pricing["net_unit"], 4)
        if delta > 0:
            promo_total = -round(delta * qty, 2)

    gross_value = amounts["gross_total"] or amounts["net_total"]

    return InvoiceLine(
        line_no=line_no,
        material_number=material,
        description=description,
        quantity=qty,
        unit_label=uom,
        unit_price=unit_price,
        gross_value=gross_value,
        off_invoice_promo=promo_total,
        raw={
            "customer_sku": customer_sku,
            "gtin": gtin,
            "gross_unit": pricing["gross_unit"],
            "promo_unit_per_ea": pricing["promo_unit"],
            "net_unit": pricing["net_unit"],
            "gross_line_total": amounts["gross_total"],
            "net_line_total": amounts["net_total"],
        },
    )


# --------------------------------------------------------------------------
# Top-level
# --------------------------------------------------------------------------


def _iter_idocs(root: ET.Element) -> Iterable[ET.Element]:
    """Yield every `<IDOC>` in the doc, regardless of envelope nesting."""
    if root.tag.upper() == "IDOC":
        yield root
    for idoc in root.iter("IDOC"):
        yield idoc


def _is_invoice_idoc(idoc: ET.Element) -> bool:
    dc = idoc.find("EDI_DC40")
    if dc is None:
        return False
    mestyp = (_text(dc, "MESTYP") or "").upper()
    idoctyp = (_text(dc, "IDOCTYP") or "").upper()
    return mestyp == "INVOIC" or idoctyp.startswith("INVOIC")


def parse_edi_invoic(raw_text: str, *, source_path: Path) -> SalesInvoice | None:
    """Parse an EDI INVOIC IDoc XML string into a `SalesInvoice`.

    Returns `None` (not an exception) if the text isn't an INVOIC IDoc,
    so callers can fall through to the next tier.
    """
    if not looks_like_edi_invoic(raw_text):
        return None

    # Caller may pass `rendered.full_text`, which prepends a `[page 1]`
    # header — strip everything before the IDoc envelope so ElementTree
    # only sees valid XML.
    xml_text = _isolate_xml_envelope(raw_text)
    if xml_text is None:
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("EDI invoice parse failed for %s: %s", source_path.name, e)
        return None

    invoice_idoc: ET.Element | None = None
    for idoc in _iter_idocs(root):
        if _is_invoice_idoc(idoc):
            invoice_idoc = idoc
            break
    if invoice_idoc is None:
        return None

    k01 = invoice_idoc.find("E1EDK01")
    if k01 is None:
        log.info("INVOIC IDoc missing E1EDK01; skipping EDI parse.")
        return None

    invoice_number = _text(k01, "BELNR")

    # Reference numbers (`E1EDK02` is repeated — one row per qualifier).
    invoice_date = _format_yyyymmdd(
        _text(_seg_with_qualf(invoice_idoc, "E1EDK02", "009"), "DATUM")
    )
    po_number = _text(_seg_with_qualf(invoice_idoc, "E1EDK02", "001"), "BELNR")
    delivery_number = _text(
        _seg_with_qualf(invoice_idoc, "E1EDK02", "012"), "BELNR"
    )

    # Terms of payment — prefer the human-readable `QUALF=005`.
    terms_seg = (
        _seg_with_qualf(invoice_idoc, "E1EDK18", "005")
        or invoice_idoc.find("E1EDK18")
    )
    terms_of_payment = _text(terms_seg, "ZTERM_TXT") if terms_seg is not None else None

    # Partners.
    bill_to_partner = (
        _seg_with_partner(invoice_idoc, "E1EDKA1", "RG")
        or _seg_with_partner(invoice_idoc, "E1EDKA1", "AG")
    )
    bill_to = _format_partner(bill_to_partner)
    ship_to_partner = _seg_with_partner(invoice_idoc, "E1EDKA1", "WE")

    # Lines.
    lines: list[InvoiceLine] = []
    line_ship_tos: set[str] = set()
    for p in invoice_idoc.findall("E1EDP01"):
        ln = _parse_invoice_line(p)
        if ln is None:
            continue
        lines.append(ln)
        line_ship = _format_partner(_seg_with_partner(p, "E1EDPA1", "WE"))
        if line_ship:
            line_ship_tos.add(line_ship)

    # If the header has no ship-to but every real-goods line ships to the
    # same address, lift it to the header so downstream UI / matching has
    # a single value to display.
    ship_to = _format_partner(ship_to_partner)
    if not ship_to and len(line_ship_tos) == 1:
        ship_to = next(iter(line_ship_tos))

    # Summary totals. `E1EDS01` keys on `<SUMID>` (per SAP spec), not
    # `<QUALF>` like most other segments.
    #   SUMID 011 -> total before discount (subtotal)
    #   SUMID Z01 -> net payable (after cash discount)
    #   SUMID 010 -> grand total (fallback for non-Blue-Diamond senders
    #               that don't emit the Z01 custom segment)
    subtotal = _parse_amount(
        _text(_seg_with_sumid(invoice_idoc, "E1EDS01", "011"), "SUMME")
    )
    total_amount = _parse_amount(
        _text(_seg_with_sumid(invoice_idoc, "E1EDS01", "Z01"), "SUMME")
    ) or _parse_amount(
        _text(_seg_with_sumid(invoice_idoc, "E1EDS01", "010"), "SUMME")
    )

    parse_warnings: list[str] = []
    if len(line_ship_tos) > 1:
        parse_warnings.append(
            "Invoice lines reference multiple ship-to partners; only header-level "
            "ship-to was retained."
        )

    log.info(
        "EDI INVOIC02 parsed: %s with %d real-goods lines",
        invoice_number,
        len(lines),
    )

    return SalesInvoice(
        source_path=str(source_path),
        pages=1,  # EDI is a single logical envelope; UI shows it as one "page"
        extraction_method=ExtractionMethod.TEXT_DETERMINISTIC,
        # High confidence — IDoc segments are unambiguous. We dock 0.05
        # vs the PDF regex path purely as a tie-breaker so duplicate
        # extractions from the same case (PDF + IDoc) prefer the PDF
        # human-readable copy when it exists.
        extraction_confidence=0.92,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        po_number=po_number,
        delivery_number=delivery_number,
        bill_to=bill_to,
        ship_to=ship_to,
        carrier=None,  # Not represented as a partner role in INVOIC02
        terms_of_payment=terms_of_payment,
        lines=lines,
        subtotal=subtotal,
        total_amount=total_amount,
        parse_warnings=parse_warnings,
    )


__all__ = ["parse_edi_invoic", "looks_like_edi_invoic"]
