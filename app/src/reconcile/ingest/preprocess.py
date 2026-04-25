"""
Image preprocessing for hard-to-read regions (stamps, handwriting, faint ink).

Pipeline applied in order:

1. Load as grayscale (BGR→gray).
2. Deskew — tiny rotations due to scanner feed; uses minAreaRect on ink pixels.
3. Contrast enhancement — CLAHE (Contrast Limited Adaptive Histogram
   Equalization). This is the single most impactful step for faint stamp
   impressions because it lifts local contrast without blowing out darks.
4. Denoise — bilateral filter preserves edges while smoothing paper grain.
5. Sharpen — unsharp mask pass so thin handwriting strokes survive later
   downscaling.
6. Optional binarization — adaptive-Gaussian threshold for OCR-friendly
   black/white output. We keep a color copy for the vision LLM (it uses color
   cues like red stamp ink).
7. Upscale to a minimum long-edge if requested — lets small stamp regions be
   fed at high effective DPI without re-rendering the PDF.

Everything here is a plain NumPy/OpenCV function so we can call it on both
full pages and stamp crops.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("reconcile.preprocess")

try:
    import cv2  # type: ignore
except Exception as e:  # pragma: no cover
    cv2 = None  # type: ignore
    log.warning("OpenCV not available: %s — preprocessing will be a no-op.", e)


# ---------------------------------------------------------------------------
# Primitive steps
# ---------------------------------------------------------------------------


def to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def deskew(gray: np.ndarray, max_deg: float = 8.0) -> tuple[np.ndarray, float]:
    """Rotate `gray` so text baselines are horizontal.

    Returns (deskewed_gray, angle_degrees).  No-op if the estimated angle is
    larger than `max_deg` (likely a false positive from sparse ink) or cv2
    is unavailable.
    """
    if cv2 is None:
        return gray, 0.0
    # Work on inverted binary so ink = foreground.
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(bw > 0))
    if coords.size < 500:  # too little ink to estimate reliably
        return gray, 0.0
    rect = cv2.minAreaRect(coords)
    angle = rect[-1]
    # minAreaRect returns angle in [-90, 0); normalize to small rotation.
    angle = -(90 + angle) if angle < -45 else -angle
    if abs(angle) > max_deg:
        return gray, 0.0
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(
        gray, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return rotated, angle


def apply_clahe(gray: np.ndarray, clip_limit: float = 3.0, tile: int = 8) -> np.ndarray:
    if cv2 is None:
        return gray
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(tile, tile))
    return clahe.apply(gray)


def unsharp(gray: np.ndarray, amount: float = 0.8, sigma: float = 1.2) -> np.ndarray:
    """Unsharp mask. Values tuned for 300 DPI scans of printed receipts."""
    if cv2 is None:
        return gray
    blurred = cv2.GaussianBlur(gray, ksize=(0, 0), sigmaX=sigma)
    sharp = cv2.addWeighted(gray, 1 + amount, blurred, -amount, 0)
    return sharp


def denoise(gray: np.ndarray) -> np.ndarray:
    """Edge-preserving smoothing — kills scanner grain without smearing strokes."""
    if cv2 is None:
        return gray
    return cv2.bilateralFilter(gray, d=5, sigmaColor=35, sigmaSpace=35)


def binarize_adaptive(gray: np.ndarray, block: int = 35, c: int = 11) -> np.ndarray:
    """Adaptive Gaussian threshold — robust to uneven scan lighting.

    The block size should roughly match the stroke width times a small factor.
    block=35, c=11 works well for receipt-grade scans at 300 DPI.
    """
    if cv2 is None:
        return gray
    # Ensure odd block size.
    if block % 2 == 0:
        block += 1
    return cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block, c,
    )


def upscale_to_min_edge(img: np.ndarray, min_long_edge: int) -> np.ndarray:
    """Upscale so the longer edge >= `min_long_edge`, using Lanczos interpolation."""
    if cv2 is None:
        return img
    h, w = img.shape[:2]
    long_edge = max(h, w)
    if long_edge >= min_long_edge:
        return img
    scale = min_long_edge / float(long_edge)
    return cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LANCZOS4)


# ---------------------------------------------------------------------------
# Combined presets
# ---------------------------------------------------------------------------


def enhance_for_vision(img_bgr: np.ndarray) -> np.ndarray:
    """
    Produce a *color* version tuned for a multimodal LLM.  We keep it color
    because VLMs use red-stamp / highlighter cues, but we still:

      - lift contrast (CLAHE on the luminance channel of LAB)
      - sharpen
      - upscale to a minimum long edge of 1600 px
    """
    if cv2 is None:
        return img_bgr
    if img_bgr.ndim == 2:
        img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = apply_clahe(l, clip_limit=2.0, tile=8)
    l = unsharp(l, amount=0.6, sigma=1.1)
    lab = cv2.merge([l, a, b])
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    return upscale_to_min_edge(out, min_long_edge=1600)


def enhance_for_ocr(img: np.ndarray) -> np.ndarray:
    """
    Produce a binarized, deskewed, sharpened grayscale image for OCR engines.
    """
    gray = to_gray(img)
    gray, angle = deskew(gray)
    if abs(angle) > 0.1:
        log.debug("deskew applied: %.2f deg", angle)
    gray = apply_clahe(gray, clip_limit=3.0, tile=8)
    gray = denoise(gray)
    gray = unsharp(gray, amount=1.0, sigma=1.2)
    gray = upscale_to_min_edge(gray, min_long_edge=2400)
    return binarize_adaptive(gray)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def read_image(path: Path) -> np.ndarray | None:
    """
    Load an image as BGR numpy.

    Large PNGs (>~8MB) can trip OpenCV's chunk-size guard, so on failure we
    fall back to PIL → numpy, which reads them fine.
    """
    if cv2 is None:
        return None
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is not None:
        return img
    log.info("cv2.imread failed for %s (likely large PNG); trying PIL fallback.", path.name)
    try:
        from PIL import Image

        with Image.open(str(path)) as im:
            im = im.convert("RGB")
            arr = np.asarray(im)
        # PIL returns RGB; cv2 expects BGR.
        return arr[:, :, ::-1].copy()
    except Exception as e:
        log.warning("PIL fallback also failed for %s: %s", path, e)
        return None


def write_image(path: Path, img: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if cv2 is None:
        # Fall back to PIL if absolutely needed.
        from PIL import Image

        if img.ndim == 2:
            Image.fromarray(img).save(str(path))
        else:
            Image.fromarray(img[:, :, ::-1]).save(str(path))
        return path
    # JPEG for color vision (smaller payloads); PNG for binary OCR.
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    else:
        cv2.imwrite(str(path), img)
    return path


def available() -> bool:
    return cv2 is not None
