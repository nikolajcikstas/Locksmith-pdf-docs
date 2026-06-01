import argparse
import json
from pathlib import Path

from locksmith_docs.parsing.column_toc import candidates_from_columns, ocr_columns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--page-image", required=True)
    parser.add_argument("--make", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    columns = ocr_columns(Path(args.page_image))
    candidates = [
        item.__dict__
        for item in candidates_from_columns(
            columns=columns,
            make=args.make,
            source_document=args.document,
            source_page=args.page,
        )
    ]

    out = {"columns": columns, "candidates": candidates}
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(args.out)
    print({"candidates": len(candidates)})


if __name__ == "__main__":
    main()
