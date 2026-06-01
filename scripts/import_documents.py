import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


DOCS_DIR = Path(
    r"C:\Users\User\AppData\Local\Packages\38833FF26BA1D.UnigramPreview_g9c9v27vpyspw\LocalState\6\documents"
)

TARGET_DOCS = [
    "2021 AutoSmart ASIAN Book.pdf",
    "2021 AutoSmart Domestic US Book.pdf",
    "Volvo.pdf",
    "Saab.pdf",
    "Mercedes.pdf",
    "Jaguar-Land Rover.pdf",
    "Introduction.pdf",
    "Fiat-Alfa Romeo.pdf",
    "BMW-Mini.pdf",
    "Audi-VW-Porsche.pdf",
]


@dataclass
class ImportConfig:
    base_dir: Path
    max_pages: int | None
    run_ocr: bool


def extract_page_image(reader: PdfReader, page_index: int, out_path: Path) -> bool:
    page = reader.pages[page_index]
    images = list(page.images)
    if not images:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(images[0].data)
    return True


def run_windows_ocr(base_dir: Path, image_path: Path) -> str:
    script = base_dir / "scripts" / "windows_ocr.ps1"
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ImagePath",
            str(image_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if result.returncode != 0:
        return f"[OCR ERROR] {result.stderr.strip()}"
    return result.stdout.strip()


def import_document(pdf_path: Path, config: ImportConfig) -> dict:
    reader = PdfReader(str(pdf_path))
    doc_slug = pdf_path.stem.replace(" ", "_").replace("/", "_")
    page_count = len(reader.pages)
    limit = min(page_count, config.max_pages) if config.max_pages else page_count
    pages = []

    for page_index in range(limit):
        page_number = page_index + 1
        image_path = config.base_dir / "storage" / "pages" / doc_slug / f"page_{page_number:04d}.jpg"
        has_image = extract_page_image(reader, page_index, image_path)
        ocr_text = ""
        if config.run_ocr and has_image:
            ocr_text = run_windows_ocr(config.base_dir, image_path)

        pages.append(
            {
                "page_number": page_number,
                "image_path": str(image_path.relative_to(config.base_dir)) if has_image else None,
                "ocr_text": ocr_text,
            }
        )
        print(f"{pdf_path.name}: page {page_number}/{limit}")

    return {
        "name": pdf_path.name,
        "path": str(pdf_path),
        "pages_total": page_count,
        "pages_imported": limit,
        "pages": pages,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--ocr", action="store_true")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parents[1]
    config = ImportConfig(base_dir=base_dir, max_pages=args.max_pages, run_ocr=args.ocr)
    imported = []

    for name in TARGET_DOCS:
        pdf_path = DOCS_DIR / name
        if not pdf_path.exists():
            imported.append({"name": name, "missing": True})
            continue
        imported.append(import_document(pdf_path, config))

    out_path = base_dir / "data" / "imported_pages.json"
    out_path.write_text(json.dumps(imported, indent=2, ensure_ascii=False), encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
