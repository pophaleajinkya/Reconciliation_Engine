"""Common helpers shared by all extractors."""

from __future__ import annotations

import re
from typing import Iterable


_AMOUNT_RE = re.compile(r"[-+]?\$?\s*([\d,]+\.\d{2})(-?)")


def parse_amount(value: str | None) -> float | None:
    """Parse an amount that may use trailing `-` for negatives (SAP/ACH style)."""
    if not value:
        return None
    m = _AMOUNT_RE.search(value)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    trailing_neg = m.group(2) == "-"
    leading_neg = value.strip().startswith("-") or value.strip().startswith("($")
    if trailing_neg or leading_neg:
        num = -num
    return num


def first_group(pattern: re.Pattern[str], text: str, default: str | None = None) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else default


def all_numeric(values: Iterable[float | int | None]) -> bool:
    return all(v is not None for v in values)
