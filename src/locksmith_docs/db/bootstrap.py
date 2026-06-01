from __future__ import annotations

import argparse
import subprocess
import sys

from locksmith_docs.db.init_schema import init_schema
from locksmith_docs.db.load_blog_seed import load_blog_seed
from locksmith_docs.db.load_imported_pages import load_imported_pages
from locksmith_docs.db.load_vehicle_candidates import main as load_vehicle_candidates_main
from locksmith_docs.reports.build_drafts import main as build_drafts_main
from locksmith_docs.videos.youtube_api import refresh_blog_videos


def run_module(module: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", module, *args], check=True)


def bootstrap(skip_ocr_pages: bool = False) -> None:
    print("Initializing database schema...")
    init_schema()

    if not skip_ocr_pages:
        print("Loading OCR pages...")
        load_imported_pages()

    print("Loading blog/video seed...")
    load_blog_seed()
    refresh_blog_videos()

    print("Building parser candidates...")
    run_module("locksmith_docs.parsing.build_candidates")

    print("Loading vehicle links and review candidates...")
    original_argv = sys.argv[:]
    try:
        sys.argv = ["load_vehicle_candidates", "--replace-source-docs", "--approve-min-confidence", "0.90"]
        load_vehicle_candidates_main()
    finally:
        sys.argv = original_argv

    print("Building report drafts...")
    try:
        sys.argv = ["build_drafts", "--to-db", "--publish", "--replace"]
        build_drafts_main()
    finally:
        sys.argv = original_argv

    print("Rendering original procedure diagrams from verified reports...")
    run_module("locksmith_docs.media.extract_assets", "--to-db", "--from-reports")

    print("Bootstrap complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize and load the Locksmith Docs development database.")
    parser.add_argument("--skip-ocr-pages", action="store_true", help="Skip imported_pages.json load for faster reruns.")
    args = parser.parse_args()
    bootstrap(skip_ocr_pages=args.skip_ocr_pages)


if __name__ == "__main__":
    main()
