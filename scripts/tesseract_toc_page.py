import argparse
import json
from pathlib import Path

from PIL import Image

from locksmith_docs.parsing.geometry_toc import parse_toc_words
from locksmith_docs.parsing.tesseract_ocr import preprocess_for_toc, run_tesseract_tsv, words_to_tsv_dicts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--page-image", required=True)
    parser.add_argument("--make", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--out", required=True)
    parser.add_argument("--keep-preprocessed", action="store_true")
    args = parser.parse_args()

    page_path = Path(args.page_image)
    processed = preprocess_for_toc(page_path)
    words = run_tesseract_tsv(processed)
    width = Image.open(processed).width
    candidates = parse_toc_words(
        words=words,
        page_width=width,
        make=args.make,
        source_document=args.document,
        source_page=args.page,
    )
    payload = {
        "preprocessed_image": str(processed) if args.keep_preprocessed else None,
        "word_count": len(words),
        "words": words_to_tsv_dicts(words),
        "candidates": [candidate.__dict__ for candidate in candidates],
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.keep_preprocessed:
        processed.unlink(missing_ok=True)
    print(args.out)
    print({"words": len(words), "candidates": len(candidates)})


if __name__ == "__main__":
    main()
