import json
from pathlib import Path

from pypdf import PdfReader


PDF_NAMES = [
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


def outline_titles(reader, limit=20):
    items = []

    def walk(nodes):
        for node in nodes:
            if len(items) >= limit:
                return
            if isinstance(node, list):
                walk(node)
                continue
            title = getattr(node, "title", None)
            if not title:
                continue
            try:
                page = reader.get_destination_page_number(node) + 1
            except Exception:
                page = None
            items.append({"title": title, "page": page})

    try:
        walk(reader.outline)
    except Exception:
        pass
    return items


def main():
    docs_dir = Path(
        r"C:\Users\User\AppData\Local\Packages\38833FF26BA1D.UnigramPreview_g9c9v27vpyspw\LocalState\6\documents"
    )
    manifest = []
    for name in PDF_NAMES:
        path = docs_dir / name
        if not path.exists():
            manifest.append({"name": name, "exists": False})
            continue
        reader = PdfReader(str(path))
        manifest.append(
            {
                "name": name,
                "exists": True,
                "bytes": path.stat().st_size,
                "pages": len(reader.pages),
                "outline": outline_titles(reader),
            }
        )

    out_path = Path(__file__).resolve().parents[1] / "data" / "pdf_manifest.json"
    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
