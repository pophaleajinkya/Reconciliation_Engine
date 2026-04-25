"""
Standalone image file renderer.

Wraps a loose `.png` / `.jpg` / `.tiff` / `.webp` as a single-page
`RenderedPDF` so the existing OCR + vision tiers can read it without
special casing. The file is treated as a scanned page: native text is
empty, `looks_scanned` is True, and `ensure_ocr()` runs Surya against the
image just as it would against a scanned PDF page.

Multi-page TIFFs are fully supported — every frame becomes its own
`RenderedPage` with its own extracted PNG so downstream code (stamp
detection, vision LLM) can address pages 1..N just like a PDF.

We intentionally copy/convert to PNG in the images output directory so
there's one canonical path the rest of the pipeline can rely on, and so
exotic formats (webp/tiff) are normalised to something every consumer
(OpenCV, Surya, vision APIs) handles cleanly.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from PIL import Image, ImageSequence

from reconcile.ingest.renderer import RenderedPage, RenderedPDF

log = logging.getLogger("reconcile.ingest")

# File extensions this renderer handles. These are all formats PIL can
# open and that OCR / vision LLMs accept after normalisation to PNG.
SUPPORTED_EXTS: frozenset[str] = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".webp",
    ".bmp",
})


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def render_image_file(img_path: Path, *, images_out_dir: Path) -> RenderedPDF:
    """Wrap a loose image file as a single-or-multi-page `RenderedPDF`.

    For single-frame images (PNG, JPG, WebP, BMP, single-page TIFF) we copy
    or transcode the source to PNG in the pipeline's canonical location.
    Multi-frame TIFFs are exploded frame-by-frame so downstream code can
    address them as regular pages.
    """
    img_path = img_path.resolve()
    sha = _hash_file(img_path)
    target_dir = images_out_dir / f"{img_path.stem}__{sha}"
    target_dir.mkdir(parents=True, exist_ok=True)

    pages: list[RenderedPage] = []
    try:
        with Image.open(str(img_path)) as im:
            frames = list(ImageSequence.Iterator(im))
            for i, frame in enumerate(frames):
                out = target_dir / f"page_{i + 1:02d}.png"
                if not out.exists():
                    frame.convert("RGB").save(str(out), format="PNG")
                pages.append(
                    RenderedPage(
                        page_num=i + 1,
                        text="",  # no native text — OCR / vision will fill this in
                        image_path=out,
                    )
                )
    except Exception as e:
        # Last-ditch: fall back to a raw byte copy so the file is at least
        # referenced by path; OCR may still handle it even if PIL choked.
        log.warning("PIL failed to open %s: %s — copying raw bytes", img_path.name, e)
        out = target_dir / f"page_01{img_path.suffix.lower()}"
        if not out.exists():
            shutil.copy2(img_path, out)
        pages.append(RenderedPage(page_num=1, text="", image_path=out))

    log.info("loaded image file %s (%d page(s))", img_path.name, len(pages))
    return RenderedPDF(source_path=img_path, pages=pages, sha256=sha)
