"""
Format-agnostic document loader.

`discover_documents()` walks a case folder and returns a triage list of:
  * supported files (with the right renderer attached)
  * skipped files (with a human-readable reason)

`load_document()` dispatches to the correct renderer for a single file.

This is the seam that turns the pipeline from "PDF-only" into
"bring-your-own-format." Adding a new format (e.g. `.xlsx`) is a matter
of writing a new renderer and registering its extensions below — nothing
downstream of this module needs to change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from reconcile.ingest.image_renderer import SUPPORTED_EXTS as IMAGE_EXTS
from reconcile.ingest.image_renderer import render_image_file
from reconcile.ingest.renderer import RenderedPDF, render_pdf
from reconcile.ingest.text_renderer import SUPPORTED_EXTS as TEXT_EXTS
from reconcile.ingest.text_renderer import render_text_file

log = logging.getLogger("reconcile.ingest")

PDF_EXTS: frozenset[str] = frozenset({".pdf"})

# Files we intentionally skip because they're never source evidence in a
# deduction reconciliation context (we surface this list to the user).
_IGNORED_EXTS: frozenset[str] = frozenset({
    ".ds_store",
    ".tmp",
    ".bak",
    ".swp",
    ".lock",
})

# Hidden / system files we silently skip.
_HIDDEN_PREFIXES: tuple[str, ...] = (".",)


@dataclass
class DiscoveredFile:
    path: Path
    kind: str  # one of: "pdf", "text", "image"
    loader: Callable[..., RenderedPDF]
    loader_kwargs: dict


@dataclass
class SkippedFile:
    path: Path
    reason: str


def _ext(p: Path) -> str:
    return p.suffix.lower()


def _is_supported(p: Path) -> bool:
    e = _ext(p)
    return e in PDF_EXTS or e in TEXT_EXTS or e in IMAGE_EXTS


def discover_documents(
    case_dir: Path,
    *,
    images_out_dir: Path,
) -> tuple[list[DiscoveredFile], list[SkippedFile]]:
    """
    Walk the (flat) case directory and return (supported, skipped).

    The walk is non-recursive on purpose: a case bundle is expected to be
    a flat folder of artefacts. Nested folders (typical of email archive
    exports) are a Tier-4 concern; we flag any subfolders as skipped for
    transparency.
    """
    supported: list[DiscoveredFile] = []
    skipped: list[SkippedFile] = []

    for entry in sorted(case_dir.iterdir()):
        name = entry.name
        if any(name.startswith(pfx) for pfx in _HIDDEN_PREFIXES):
            continue
        if entry.is_dir():
            skipped.append(
                SkippedFile(path=entry, reason="nested folders not yet supported")
            )
            continue
        if not entry.is_file():
            continue

        e = _ext(entry)
        if e in _IGNORED_EXTS:
            continue

        if e in PDF_EXTS:
            supported.append(
                DiscoveredFile(
                    path=entry,
                    kind="pdf",
                    loader=render_pdf,
                    loader_kwargs={"images_out_dir": images_out_dir},
                )
            )
        elif e in TEXT_EXTS:
            supported.append(
                DiscoveredFile(
                    path=entry,
                    kind="text",
                    loader=render_text_file,
                    loader_kwargs={},
                )
            )
        elif e in IMAGE_EXTS:
            supported.append(
                DiscoveredFile(
                    path=entry,
                    kind="image",
                    loader=render_image_file,
                    loader_kwargs={"images_out_dir": images_out_dir},
                )
            )
        else:
            # Unsupported but present — surface so the user knows it was
            # ignored instead of silently dropping evidence.
            skipped.append(
                SkippedFile(
                    path=entry,
                    reason=f"unsupported file type '{e or '(no extension)'}'",
                )
            )

    return supported, skipped


def load_document(spec: DiscoveredFile) -> RenderedPDF:
    """Dispatch a discovered file to its renderer."""
    return spec.loader(spec.path, **spec.loader_kwargs)
