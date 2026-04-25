"""
Tier-2 OCR fallback using Surya (https://github.com/datalab-to/surya).

Surya is Apache-2.0 licensed and produces line-level OCR with bounding boxes
and confidences. It runs on CPU (slow) or GPU/MPS (fast). The first call
downloads ~1GB of weights automatically.

Design notes
------------
- Predictors are heavy; we cache a module-level singleton so multiple pages
  reuse the same loaded model.
- We degrade gracefully: if Surya isn't installed, or if anything goes wrong
  at init time, the callers transparently fall through to the vision LLM
  path (tier 3).
- We sort lines in reading order (top-to-bottom, then left-to-right) based
  on the returned bboxes, so downstream regex parsers see text in roughly
  the order a human would read it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("reconcile.ingest.ocr")


@dataclass
class OCRLine:
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2


@dataclass
class OCRPage:
    lines: list[OCRLine]
    mean_confidence: float

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


class _SuryaRunner:
    """Lazy singleton that loads Surya predictors on first use."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self._failed = False
        self._fail_reason: str | None = None
        self._recognition = None
        self._detection = None

    @property
    def available(self) -> bool:
        return not self._failed

    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        if self._failed:
            return False
        with self._lock:
            if self._loaded:
                return True
            if self._failed:
                return False
            try:
                from surya.detection import DetectionPredictor  # type: ignore
                from surya.foundation import FoundationPredictor  # type: ignore
                from surya.recognition import RecognitionPredictor  # type: ignore
            except Exception as e:
                self._failed = True
                self._fail_reason = f"surya-ocr not importable: {e}"
                log.warning(self._fail_reason)
                return False
            try:
                log.info("Loading Surya predictors (first run downloads weights)...")
                foundation = FoundationPredictor()
                self._recognition = RecognitionPredictor(foundation)
                self._detection = DetectionPredictor()
                self._loaded = True
                log.info("Surya predictors loaded.")
                return True
            except Exception as e:
                self._failed = True
                self._fail_reason = f"surya init failed: {e}"
                log.warning(self._fail_reason)
                return False

    def ocr_images(
        self,
        image_paths: list[Path],
        *,
        max_long_edge: int = 1500,
    ) -> list[OCRPage]:
        """
        Run Surya OCR on a batch of images.

        `max_long_edge` downscales images before recognition so Surya doesn't
        burn seconds-per-line on pages rendered at 220-400 dpi. Printed labels
        remain readable at ~1500 px and Surya's own line detector is most
        accurate in this range; above it, we pay a lot for diminishing gains.
        Handwriting — the only thing that actually needs more pixels — is
        routed to the vision LLM anyway.

        Bboxes returned are **scaled back** to the original image's coordinate
        system, so downstream code (e.g. stamp_reader) can pass them back into
        crops taken from the original full-resolution image.
        """
        if not self._ensure_loaded():
            return []
        try:
            from PIL import Image
        except Exception as e:
            log.warning("PIL unavailable for OCR: %s", e)
            return []
        images: list[Any] = []
        scales: list[float] = []  # per-image: original_edge / resized_edge
        for p in image_paths:
            try:
                im = Image.open(str(p)).convert("RGB")
            except Exception as e:
                log.warning("Failed to open page image %s: %s", p, e)
                continue
            w, h = im.size
            long_edge = max(w, h)
            if long_edge > max_long_edge:
                scale = max_long_edge / float(long_edge)
                im = im.resize(
                    (int(w * scale), int(h * scale)),
                    Image.LANCZOS,  # type: ignore[attr-defined]
                )
                scales.append(1.0 / scale)  # to map bbox back to original coords
            else:
                scales.append(1.0)
            images.append(im)
        if not images:
            return []

        try:
            predictions = self._recognition(images, det_predictor=self._detection)  # type: ignore
        except Exception as e:
            log.warning("Surya recognition failed: %s", e)
            return []

        pages: list[OCRPage] = []
        for pred, scale_back in zip(predictions, scales):
            lines = _extract_lines(pred)
            if not lines:
                pages.append(OCRPage(lines=[], mean_confidence=0.0))
                continue
            # Scale bboxes back to the original image's coordinate system so
            # callers can crop from the high-res source without re-aligning.
            if scale_back != 1.0:
                lines = [
                    OCRLine(
                        text=ln.text,
                        confidence=ln.confidence,
                        bbox=(
                            ln.bbox[0] * scale_back,
                            ln.bbox[1] * scale_back,
                            ln.bbox[2] * scale_back,
                            ln.bbox[3] * scale_back,
                        ),
                    )
                    for ln in lines
                ]
            lines.sort(key=lambda ln: (round(ln.bbox[1] / 8) * 8, ln.bbox[0]))
            confs = [ln.confidence for ln in lines if ln.confidence is not None]
            mean_conf = sum(confs) / len(confs) if confs else 0.0
            pages.append(OCRPage(lines=lines, mean_confidence=mean_conf))
        return pages


def _extract_lines(pred: Any) -> list[OCRLine]:
    """Surya returns an OCRResult-style object. Tolerate minor API shifts."""
    raw_lines = getattr(pred, "text_lines", None)
    if raw_lines is None and isinstance(pred, dict):
        raw_lines = pred.get("text_lines", [])
    out: list[OCRLine] = []
    for rl in raw_lines or []:
        text = getattr(rl, "text", None) if not isinstance(rl, dict) else rl.get("text")
        conf = getattr(rl, "confidence", None) if not isinstance(rl, dict) else rl.get("confidence")
        bbox = getattr(rl, "bbox", None) if not isinstance(rl, dict) else rl.get("bbox")
        if not text or bbox is None:
            continue
        try:
            bb = tuple(float(x) for x in bbox)  # (x1, y1, x2, y2)
            if len(bb) != 4:
                continue
        except Exception:
            continue
        out.append(OCRLine(text=str(text), confidence=float(conf or 0.0), bbox=bb))  # type: ignore[arg-type]
    return out


_runner = _SuryaRunner()


def ocr_pages(image_paths: list[Path]) -> list[OCRPage]:
    """Run OCR on a list of page images. Returns [] if OCR is unavailable."""
    if not image_paths:
        return []
    return _runner.ocr_images(image_paths)


def ocr_available() -> bool:
    """Cheap probe without actually loading the weights."""
    try:
        import importlib.util  # noqa: F401

        spec = importlib.util.find_spec("surya")
        return spec is not None and _runner.available
    except Exception:
        return False
