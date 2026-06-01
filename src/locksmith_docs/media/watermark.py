from __future__ import annotations

from PIL import Image, ImageEnhance


def remove_light_watermark(image: Image.Image) -> Image.Image:
    """Remove pale colored watermark noise while preserving dark operational details."""
    image = image.convert("RGB")
    pixels = image.load()
    width, height = image.size

    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            brightness = (r + g + b) / 3
            spread = max(r, g, b) - min(r, g, b)
            is_pale_colored_watermark = brightness > 178 and min(r, g, b) > 138 and 8 < spread < 72
            is_faint_gray_background = brightness > 188 and spread <= 12
            is_near_white_noise = brightness > 222 and spread < 42
            if is_pale_colored_watermark or is_faint_gray_background or is_near_white_noise:
                pixels[x, y] = (255, 255, 255)

    return ImageEnhance.Contrast(image).enhance(1.12)
