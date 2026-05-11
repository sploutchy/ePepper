"""Image processing: resize and dither photos for e-ink display."""

from io import BytesIO

from PIL import Image

from config import DISPLAY_WIDTH, RECIPE_HEIGHT


def process_photo(image_bytes: bytes) -> Image.Image:
    """Resize and dither an image for the e-ink display.

    Returns a 1-bit BMP-ready image sized to the full display (800x480).
    """
    img = Image.open(BytesIO(image_bytes))
    img = img.convert("RGB")

    # Resize to fit display, maintaining aspect ratio
    img = _resize_to_fit(img, DISPLAY_WIDTH, RECIPE_HEIGHT)

    # Center on white canvas
    canvas = Image.new("RGB", (DISPLAY_WIDTH, RECIPE_HEIGHT), (255, 255, 255))
    x = (DISPLAY_WIDTH - img.width) // 2
    y = (RECIPE_HEIGHT - img.height) // 2
    canvas.paste(img, (x, y))

    # Convert to 1-bit with Floyd-Steinberg dithering
    return canvas.convert("1")


def _resize_to_fit(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """Resize image to fit within max dimensions, keeping aspect ratio."""
    ratio = min(max_w / img.width, max_h / img.height)
    if ratio >= 1:
        return img
    new_size = (int(img.width * ratio), int(img.height * ratio))
    return img.resize(new_size, Image.Resampling.LANCZOS)
