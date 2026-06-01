import argparse
from pathlib import Path

from PIL import Image, ImageEnhance
from pypdf import PdfReader


def remove_light_watermark(image: Image.Image) -> Image.Image:
    """Suppress pale cyan/pink page watermarks while preserving dark technical marks."""
    image = image.convert("RGB")
    pixels = image.load()
    width, height = image.size

    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            is_pale = r > 135 and g > 135 and b > 135
            is_light_background_noise = r > 150 and g > 150 and b > 150
            is_colored_watermark = max(r, g, b) - min(r, g, b) > 10
            is_not_dark_ink = r > 100 and g > 100 and b > 100
            if is_not_dark_ink and (is_light_background_noise or (is_pale and is_colored_watermark)):
                pixels[x, y] = (255, 255, 255)

    image = ImageEnhance.Contrast(image).enhance(1.25)
    return image


def extract_page_image(pdf_path: Path, page_number: int) -> Image.Image:
    reader = PdfReader(str(pdf_path))
    page = reader.pages[page_number - 1]
    images = list(page.images)
    if not images:
        raise RuntimeError(f"No raster image found on page {page_number}")
    return images[0].image.convert("RGB")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--crop", required=True, help="left,top,right,bottom in page image pixels")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    out_path = Path(args.out)
    crop = tuple(int(part) for part in args.crop.split(","))

    page_image = extract_page_image(pdf_path, args.page)
    cropped = page_image.crop(crop)
    cleaned = remove_light_watermark(cropped)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned.save(out_path)
    print(out_path)


if __name__ == "__main__":
    main()
