"""Image preprocessing for the OCR (image → recipe) LLM path.

Goals:
- Bound the vision-token budget (a 4000 px-wide cookbook scan would be
  tiled into many image tokens; downscaling to 1600 px keeps cost
  predictable without losing legibility of recipe text).
- Strip EXIF (and apply EXIF orientation) so the model sees a properly
  rotated image and we don't leak GPS metadata to the API.
- Normalise to JPEG bytes; OpenAI-compatible vision endpoints accept
  base64 data URLs of common formats but JPEG is the safe lowest
  common denominator.
"""

from io import BytesIO

from PIL import Image, ImageOps

# Long-edge ceiling for the image we send to the LLM. 1600 px keeps a
# typical cookbook scan readable while limiting vision-token usage on
# providers that tile (most do, with ~512 px tiles).
_MAX_LONG_EDGE = 1600

# JPEG quality knob — 85 is the usual sweet spot for photographic content.
_JPEG_QUALITY = 85


def encode_for_ocr(image_bytes: bytes) -> bytes:
    """Normalise an arbitrary upload into a compact JPEG suitable for OCR.

    Accepts whatever Pillow can open (JPEG / PNG / WebP / HEIC if the
    Pillow build has the plugin). Applies EXIF orientation, drops the
    rest of the metadata, downscales the long edge to `_MAX_LONG_EDGE`,
    and re-encodes as a quality-85 JPEG.
    """
    img = Image.open(BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    img = _shrink_to_long_edge(img, _MAX_LONG_EDGE)

    out = BytesIO()
    img.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _shrink_to_long_edge(img: Image.Image, max_edge: int) -> Image.Image:
    long_edge = max(img.width, img.height)
    if long_edge <= max_edge:
        return img
    ratio = max_edge / long_edge
    new_size = (int(img.width * ratio), int(img.height * ratio))
    return img.resize(new_size, Image.Resampling.LANCZOS)
