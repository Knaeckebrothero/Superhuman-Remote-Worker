"""Downscale / re-encode images before they reach a multimodal LLM.

Two levers, applied at the single point every image funnels through
(`make_multimodal_user_message` in `src/services/image_content.py`):

* **Resolution** is the real cost lever — image tokens scale with pixel
  dimensions (`src/core/image_tokens.py`), and above a model family's
  tiling cap extra resolution is invisible (the model downsamples). So we
  always downscale to the *tier's* max longest-edge, clamped to the
  family cap.
* **Bytes** is the proxy/OOM lever (a handful of ~4 MB phone photos become
  a ~30 MB request that OOM'd the codex-proxy). We re-encode above a byte
  threshold — but **format-aware**, never blanket JPEG: JPEG artifacts on
  text/line edges hurt exactly the High-quality cases (OCR, charts,
  frontend screenshots) and JPEG has no alpha. Rule: lossy re-encode only
  photographs (JPEG source); keep PNG/alpha lossless.

Design contract: **fail-open**. If an image cannot be decoded (unknown
format, corrupt bytes, missing Pillow) the original bytes are returned
unchanged — image delivery must never break because of the size guard.

See `knowledge-base/knowledge/issues/session_turn_hard_fails_on_transient_llm_outage.md`
(Track 1) for the incident and the tier design.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

# Image-quality tier -> nominal max longest-edge in px. The value is clamped
# to the model family's own cap at resolve time (above the cap the model
# downsamples anyway, so sending more is pure token/byte waste).
ECONOMY = "economy"
STANDARD = "standard"
HIGH = "high"
DEFAULT_TIER = STANDARD
VALID_TIERS = (ECONOMY, STANDARD, HIGH)

_TIER_NOMINAL_EDGE = {
    ECONOMY: 768,  # few/single tile — coarse "what is this"
    STANDARD: 1568,  # ~1.15 MP; where major vision models plateau
    HIGH: 1_000_000,  # sentinel -> resolves to the family cap as-is
}

# Fallback family cap when the model family declares no image_tokens.max_edge.
DEFAULT_FAMILY_MAX_EDGE_FALLBACK = 2048

# Re-encode (byte guard) above this size even if already within the max edge.
DEFAULT_BYTE_THRESHOLD = 1_000_000

_JPEG_QUALITY = 82


def normalize_tier(tier: object) -> str:
    """Coerce an arbitrary value to a valid tier, defaulting to STANDARD."""
    if isinstance(tier, str) and tier.lower() in VALID_TIERS:
        return tier.lower()
    return DEFAULT_TIER


def resolve_max_edge(tier: object, family_max_edge: int | None) -> int:
    """Longest-edge cap (px) for a tier, clamped to the model family cap.

    ``family_max_edge`` is the family's ``image_tokens.max_edge`` (or None /
    0 when the family doesn't declare one, in which case a conservative
    universal default is used). "high" resolves to exactly the family cap.
    """
    cap = (
        int(family_max_edge)
        if isinstance(family_max_edge, (int, float)) and family_max_edge > 0
        else DEFAULT_FAMILY_MAX_EDGE_FALLBACK
    )
    nominal = _TIER_NOMINAL_EDGE.get(
        normalize_tier(tier), _TIER_NOMINAL_EDGE[DEFAULT_TIER]
    )
    return min(nominal, cap)


def _has_alpha(im) -> bool:
    return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def downscale_image_bytes(
    data: bytes,
    mime_type: str,
    max_edge: int,
    byte_threshold: int = DEFAULT_BYTE_THRESHOLD,
) -> Tuple[bytes, str]:
    """Downscale/re-encode raw image bytes for delivery to an LLM.

    Returns ``(bytes, mime_type)``. Returns the *same* ``data`` object
    (identity-comparable) when the image is left untouched, so callers can
    cheaply detect a no-op. Fail-open: any decode/encode error returns the
    original bytes + mime.
    """
    if max_edge <= 0:
        return data, mime_type
    over_bytes = len(data) > byte_threshold

    try:
        from PIL import Image, ImageOps
    except Exception:  # pragma: no cover - Pillow is a hard dep, defensive only
        return data, mime_type

    try:
        with Image.open(io.BytesIO(data)) as opened:
            im = ImageOps.exif_transpose(opened)  # honor phone EXIF orientation
            w, h = im.size
            longest = max(w, h)
            needs_resize = longest > max_edge

            # Fast path: small enough in both dimensions and bytes -> untouched.
            if not needs_resize and not over_bytes:
                return data, mime_type

            if needs_resize:
                scale = max_edge / float(longest)
                im = im.resize(
                    (max(1, round(w * scale)), max(1, round(h * scale))),
                    Image.Resampling.LANCZOS,
                )

            src = (mime_type or "").lower()
            buf = io.BytesIO()
            if _has_alpha(im):
                # Transparency: JPEG would flatten the alpha -> keep PNG lossless.
                im.save(buf, format="PNG", optimize=True)
                out_mime = "image/png"
            elif "jpeg" in src or "jpg" in src:
                # Photograph: lossy re-encode is the big byte win, no text to
                # artifact on.
                im.convert("RGB").save(
                    buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True
                )
                out_mime = "image/jpeg"
            else:
                # Non-photo, no alpha (PNG/WebP/GIF screenshots, charts, code):
                # keep lossless to protect text/line edges; downscaling already
                # cut the bytes.
                im.convert("RGB").save(buf, format="PNG", optimize=True)
                out_mime = "image/png"

            out = buf.getvalue()
            # Never hand back something bigger than we were given when we only
            # re-encoded (a byte-guard pass on an already-optimal file).
            if not needs_resize and len(out) >= len(data):
                return data, mime_type
            return out, out_mime
    except Exception as e:
        logger.warning(
            "image downscale failed (%s: %s); sending original image",
            type(e).__name__,
            e,
        )
        return data, mime_type


def downscale_image_b64(
    b64: str,
    mime_type: str,
    max_edge: int,
    byte_threshold: int = DEFAULT_BYTE_THRESHOLD,
) -> Tuple[str, str]:
    """Base64-in / base64-out wrapper around :func:`downscale_image_bytes`.

    Returns the *same* ``b64`` string object when the image is untouched.
    """
    if max_edge <= 0:
        return b64, mime_type
    try:
        data = base64.b64decode(b64)
    except Exception:
        return b64, mime_type
    out, out_mime = downscale_image_bytes(data, mime_type, max_edge, byte_threshold)
    if out is data:
        return b64, mime_type
    return base64.b64encode(out).decode(), out_mime
