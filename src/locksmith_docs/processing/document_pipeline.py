from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import fitz
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from locksmith_docs.core.config import get_settings
from locksmith_docs.db.load_imported_pages import load_imported_pages
from locksmith_docs.db.load_vehicle_candidates import main as load_vehicle_candidates_main
from locksmith_docs.db.connection import get_connection
from locksmith_docs.db.init_schema import init_schema
from locksmith_docs.db.report_seed import ensure_bundled_parser_seed, ensure_bundled_report_seed, write_json_atomic
from locksmith_docs.db.repository import LocksmithRepository
from locksmith_docs.processing.ai_text_cleaner import clean_page_with_ai
from locksmith_docs.processing.job_status import has_running_job, update_job
from locksmith_docs.reports.ai_report_cleaner import merge_source_supported_facts, report_completeness_issues, report_quality_issues, require_report_ai_access, sanitize_structured_sections
from locksmith_docs.reports.build_drafts import import_to_database as import_report_drafts_to_database
from locksmith_docs.reports.build_drafts import main as build_drafts_main
from locksmith_docs.reports.build_drafts import report_is_publishable
from locksmith_docs.reports.verified_facts import apply_verified_facts


def safe_stem(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", path.stem).strip("_")
    return stem or "document"


def read_report_drafts_json(report_path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        ensure_bundled_report_seed()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def render_pdf_to_imported_document(
    pdf_path: Path,
    dpi: int = 220,
    progress: Callable[[int, int], None] | None = None,
    allow_ai_cleanup: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    doc = fitz.open(pdf_path)
    stem = safe_stem(pdf_path)
    page_dir = settings.storage_dir / "pages" / stem
    page_dir.mkdir(parents=True, exist_ok=True)
    pages = []

    for index, page in enumerate(doc, start=1):
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        image_path = page_dir / f"page_{index:04d}.png"
        pix.save(image_path)

        native_text = clean_ocr_text(page.get_text("text") or "")
        ocr_text = ocr_page_image(image_path)
        selected_text = choose_best_text(native_text, ocr_text)
        ai_text = clean_page_with_ai(selected_text) if allow_ai_cleanup else None
        merged_text = choose_best_text(selected_text, ai_text or "")
        pages.append(
            {
                "page_number": index,
                "image_path": str(image_path.relative_to(settings.project_root)),
                "native_text": native_text,
                "raw_ocr_text": ocr_text,
                "ocr_text": merged_text,
                "ocr_quality": clamp_quality(quality_score(merged_text)),
                "cleaning_method": "openai" if ai_text else "local",
            }
        )
        if progress:
            progress(index, len(doc))

    return {
        "name": pdf_path.name,
        "path": str(pdf_path),
        "pages_total": len(doc),
        "pages_imported": len(pages),
        "pages": pages,
    }


def ocr_page_image(image_path: Path) -> str:
    base = Image.open(image_path).convert("L")
    candidates = []
    for psm in (6, 4, 11):
        image = preprocess_for_ocr(base, psm)
        config = f"--oem 1 --psm {psm} -c preserve_interword_spaces=1"
        text = pytesseract.image_to_string(image, lang="eng", config=config)
        clean = clean_ocr_text(text)
        candidates.append((quality_score(clean), clean))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else ""


def preprocess_for_ocr(image: Image.Image, psm: int) -> Image.Image:
    prepared = ImageOps.autocontrast(image)
    prepared = ImageEnhance.Contrast(prepared).enhance(1.45 if psm != 11 else 1.8)
    prepared = prepared.filter(ImageFilter.SHARPEN)
    if psm == 11:
        prepared = prepared.point(lambda px: 255 if px > 180 else 0)
    return prepared


def clean_ocr_text(text: str) -> str:
    replacements = {
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "Р": "P",
        "С": "C",
        "А": "A",
        "О": "O",
        "Т": "T",
        "М": "M",
        "В": "B",
        "Н": "H",
        "К": "K",
        "х": "x",
        "а": "a",
        "е": "e",
        "о": "o",
        "с": "c",
        "і": "i",
        "™": "",
        "т": "t",
        "ч": "h",
        "ы": "b",
        "ь": "b",
        "р": "p",
        "н": "h",
        "и": "u",
        "г": "r",
        "л": "n",
        "б": "b",
        "д": "d",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"[^\S\r\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def choose_best_text(native_text: str, ocr_text: str) -> str:
    native_score = quality_score(native_text)
    ocr_score = quality_score(ocr_text)
    if native_score >= ocr_score * 0.85 and len(native_text) > 80:
        return native_text
    if ocr_score >= native_score and len(ocr_text) > 40:
        return ocr_text
    return native_text or ocr_text


def quality_score(text: str) -> float:
    if not text:
        return 0.0
    ascii_letters = len(re.findall(r"[A-Za-z]", text))
    bad = len(re.findall(r"[^\x00-\x7F]", text))
    noisy = len(re.findall(r"[±¤\[\]{}]|[A-Za-z]\d[A-Za-z]", text))
    spaces = text.count(" ")
    terms = len(re.findall(r"\b(?:FCC|MHz|Lishi|AccuReader|Determinator|Remote|Fob|Code Series|SPACING|DEPTHS|PIN|Transponder|Chip|Key|Door|Trunk|Ignition)\b", text, re.IGNORECASE))
    return ascii_letters + spaces * 0.15 + terms * 12.0 - bad * 8.0 - noisy * 5.0


def clamp_quality(value: float) -> float:
    return max(-999.99, min(999.99, round(value, 2)))


def merge_imported_document(document: dict[str, Any]) -> Path:
    settings = get_settings()
    manifest_path = settings.data_dir / "imported_pages.json"
    if manifest_path.exists():
        imported = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        imported = []
    imported = [item for item in imported if item.get("name") != document["name"]]
    imported.append(document)
    imported.sort(key=lambda item: item.get("name", ""))
    manifest_path.write_text(json.dumps(imported, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest_path


def save_uploaded_pdf(source_file, filename: str) -> Path:
    settings = get_settings()
    upload_dir = settings.storage_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_. -]+", "_", Path(filename).name).strip(" .")
    destination = upload_dir / (safe_name or "document.pdf")
    with destination.open("wb") as out:
        shutil.copyfileobj(source_file, out)
    return destination


def uploaded_pdf_paths() -> list[Path]:
    upload_dir = get_settings().storage_dir / "uploads"
    return sorted(upload_dir.glob("*.pdf"))


def reset_derived_data_for_pilot() -> Path:
    settings = get_settings()
    archive_dir = settings.storage_dir / "archives" / f"pilot_reset_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    data_archive = archive_dir / "data"
    data_archive.mkdir(parents=True, exist_ok=True)
    derived_data_names = (
        "ai_report_cache.json",
        "ai_diagram_cache.json",
        "asset_candidates.json",
        "imported_pages.json",
        "parser_candidates.json",
        "report_drafts.json",
    )
    for name in derived_data_names:
        source = settings.data_dir / name
        if source.exists():
            shutil.move(str(source), str(data_archive / name))
    for derived_dir in (settings.storage_dir / "pages", settings.project_root / "static" / "assets" / "candidates"):
        if derived_dir.exists():
            destination = archive_dir / derived_dir.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(derived_dir), str(destination))
        derived_dir.mkdir(parents=True, exist_ok=True)

    init_schema()
    with get_connection() as conn:
        conn.execute("DELETE FROM assets")
        conn.execute("DELETE FROM report_sections")
        conn.execute("DELETE FROM report_drafts")
        conn.execute("DELETE FROM vehicle_aliases")
        conn.execute("DELETE FROM vehicle_applications")
        conn.execute("DELETE FROM systems")
        conn.execute("DELETE FROM review_queue")
        conn.execute("DELETE FROM parser_jobs")
        conn.execute("DELETE FROM document_pages")
        conn.execute("DELETE FROM documents")
        conn.commit()
    return archive_dir


def rebuild_catalog(
    skip_ocr_pages: bool = False,
    regenerate_assets: bool = False,
    job_id: str | None = None,
    target_system_code: str | None = None,
) -> dict[str, int]:
    require_report_ai_access()
    init_schema()
    if not skip_ocr_pages:
        if job_id:
            update_job(job_id, "running", "Saving OCR pages and preparing vehicle mappings.")
        load_imported_pages()

    original_argv = sys.argv[:]
    candidates_path = get_settings().data_dir / "parser_candidates.json"
    if skip_ocr_pages and not candidates_path.exists():
        ensure_bundled_parser_seed()
    if not skip_ocr_pages or not candidates_path.exists():
        run_module("locksmith_docs.parsing.build_candidates")
        try:
            sys.argv = ["load_vehicle_candidates", "--replace-source-docs", "--approve-min-confidence", "0.90"]
            load_vehicle_candidates_main()
        finally:
            sys.argv = original_argv

    try:
        # Generate and validate first; a failed AI pass must not erase the last usable catalog.
        sys.argv = ["build_drafts", "--publish"]
        if job_id:
            sys.argv += ["--job-id", job_id]
        if target_system_code:
            sys.argv += ["--system-code", target_system_code]
        build_drafts_main()
    finally:
        sys.argv = original_argv

    report_path = get_settings().data_dir / "report_drafts.json"
    report_drafts = read_report_drafts_json(report_path)
    publishable = sum(
        bool(item.get("ai_verified"))
        and report_is_publishable(item.get("draft") or {}, item.get("publication_issues") or [])
        for item in report_drafts
    )
    awaiting = sum(
        any("Awaiting AI verification" in issue for issue in (item.get("publication_issues") or []))
        for item in report_drafts
    )
    rejected = sum(
        bool(item.get("ai_verified")) and bool(item.get("publication_issues"))
        for item in report_drafts
    )
    # Publish verified batches cumulatively. Cached approved reports cost nothing on later runs.
    # Existing public reports remain available while unprocessed sections wait for the next budgeted batch.
    withdrawn_codes = [
        str(item.get("system_code") or "")
        for item in report_drafts
        if item.get("ai_verified") and item.get("publication_issues")
    ]
    repo = LocksmithRepository()
    repo.retract_unpublishable_systems([code for code in withdrawn_codes if code])
    import_report_drafts_to_database(report_drafts, publish=True, replace=target_system_code is None)

    if regenerate_assets:
        if job_id:
            update_job(job_id, "running", "Rendering original procedure diagrams extracted during report verification.")
        asset_args = ["--from-reports", "--to-db"]
        if job_id:
            asset_args += ["--job-id", job_id]
        run_module("locksmith_docs.media.extract_assets", *asset_args)
    else:
        run_module("locksmith_docs.media.extract_assets", "--to-db", "--from-manifest", "--replace", "--clean-manifest")
    diagrams = 0
    asset_manifest = get_settings().data_dir / "asset_candidates.json"
    if asset_manifest.exists():
        try:
            diagrams = len(json.loads(asset_manifest.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            diagrams = 0
    return {
        "publishable_reports": publishable,
        "reports_awaiting_verification": awaiting,
        "rejected_reports": rejected,
        "withdrawn_reports": len(withdrawn_codes),
        "public_diagrams_in_batch": diagrams,
    }


def index_documents_without_ai(documents: list[dict[str, Any]], job_id: str | None = None) -> dict[str, int]:
    """Load OCR and vehicle mappings without producing or verifying customer reports."""
    init_schema()
    for document in documents:
        merge_imported_document(document)
    if job_id:
        update_job(job_id, "running", "Saving OCR pages and building the vehicle lookup index without AI requests.")
    load_imported_pages()
    run_module("locksmith_docs.parsing.build_candidates")

    original_argv = sys.argv[:]
    try:
        sys.argv = ["load_vehicle_candidates", "--replace-source-docs", "--approve-min-confidence", "0.90"]
        load_vehicle_candidates_main()
    finally:
        sys.argv = original_argv

    counts = LocksmithRepository().dashboard_counts()
    return {
        "documents": len(documents),
        "pages": sum(item["pages_imported"] for item in documents),
        "vehicle_links": counts.get("vehicle_links", 0),
        "mapped_system_codes": counts.get("mapped_system_codes", 0),
        "published_reports": counts.get("published_reports", 0),
    }


def index_all_uploaded_pdfs_without_ai(job_id: str | None = None) -> dict[str, int]:
    """Restore the active lookup catalog from saved PDFs with guaranteed zero API use."""
    documents = []
    for pdf_path in uploaded_pdf_paths():
        progress = (
            lambda done, total, name=pdf_path.name: update_job(
                job_id, "running", f"Local OCR index: {name}, page {done}/{total}. No AI requests are made."
            )
            if job_id
            else None
        )
        documents.append(render_pdf_to_imported_document(pdf_path, progress=progress, allow_ai_cleanup=False))
    return index_documents_without_ai(documents, job_id=job_id)


def import_prepared_assets() -> None:
    init_schema()
    run_module("locksmith_docs.media.extract_assets", "--to-db", "--from-manifest", "--clean-manifest")


def refresh_verified_output() -> dict[str, int]:
    """Rebuild already approved report presentation and SVGs without using AI."""
    init_schema()
    report_path = get_settings().data_dir / "report_drafts.json"
    if not report_path.exists():
        raise RuntimeError("No verified report data is available to refresh yet.")
    report_drafts = read_report_drafts_json(report_path)
    sections_by_code = {}
    candidates_path = get_settings().data_dir / "parser_candidates.json"
    if candidates_path.exists():
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        sections_by_code = {
            str(section.get("code") or ""): section
            for section in candidates.get("sections", [])
        }
    withdrawn = []
    verified_facts_path = get_settings().data_dir / "verified_report_facts.json"
    for item in report_drafts:
        if item.get("ai_verified") and isinstance(item.get("draft"), dict):
            item["draft"] = apply_verified_facts(
                item["draft"],
                str(item.get("system_code") or ""),
                verified_facts_path,
            )
            raw_source = str((sections_by_code.get(item.get("system_code")) or {}).get("raw_text") or "")
            item["draft"] = merge_source_supported_facts(item["draft"], raw_source)
            item["draft"] = sanitize_structured_sections(item["draft"])
            issues = report_quality_issues(item["draft"]) + report_completeness_issues(item["draft"], raw_source)
            item["publication_issues"] = issues
            if issues:
                withdrawn.append(str(item.get("system_code") or ""))
    write_json_atomic(report_path, report_drafts)
    repo = LocksmithRepository()
    repo.retract_unpublishable_systems([code for code in withdrawn if code])
    import_report_drafts_to_database(report_drafts, publish=True, replace=True)
    run_module("locksmith_docs.media.extract_assets", "--from-reports", "--to-db")
    return {
        "reports": sum(bool(item.get("ai_verified")) and not item.get("publication_issues") for item in report_drafts),
        "withdrawn": len(withdrawn),
        "diagrams": repo.dashboard_counts().get("public_diagrams", 0),
    }


def clear_rejected_report_cache() -> int:
    """Forget only failed AI drafts so they can be verified again without touching approved reports."""
    cache_path = get_settings().data_dir / "ai_report_cache.json"
    if not cache_path.exists():
        return 0
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    if not isinstance(cache, dict):
        return 0
    retained = {
        key: value
        for key, value in cache.items()
        if not (isinstance(value, dict) and value.get("__rejected__") is True)
    }
    removed = len(cache) - len(retained)
    if removed:
        cache_path.write_text(json.dumps(retained, indent=2, ensure_ascii=False), encoding="utf-8")
    return removed


def run_refresh_verified_output_job(job_id: str) -> None:
    try:
        update_job(job_id, "running", "Refreshing verified reports and drawing saved diagrams without AI requests.")
        result = refresh_verified_output()
        update_job(
            job_id,
            "complete",
            f"Refreshed {result['reports']} publishable report(s) and {result['diagrams']} saved diagram(s) without AI requests; withdrew {result['withdrawn']} incomplete report(s) for correction.",
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_retry_rejected_reports_job(job_id: str) -> None:
    try:
        update_job(job_id, "running", "Retrying rejected reports while preserving the most complete prior extraction.")
        result = rebuild_catalog(skip_ocr_pages=True, regenerate_assets=True, job_id=job_id)
        update_job(
            job_id,
            "complete",
            f"Retry complete. Published reports: {result['publishable_reports']}; "
            f"still rejected: {result['rejected_reports']}; waiting: {result['reports_awaiting_verification']}.",
            **result,
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def reprocess_uploaded_pdfs(job_id: str | None = None) -> dict[str, int]:
    documents = []
    for pdf_path in uploaded_pdf_paths():
        progress = (
            lambda done, total, name=pdf_path.name: update_job(
                job_id, "running", f"OCR: {name}, page {done}/{total}."
            )
            if job_id
            else None
        )
        documents.append(render_pdf_to_imported_document(pdf_path, progress=progress))
    for document in documents:
        merge_imported_document(document)
    result = rebuild_catalog(skip_ocr_pages=False, regenerate_assets=True, job_id=job_id)
    return {"documents": len(documents), "pages": sum(item["pages_imported"] for item in documents), **result}


def process_saved_pdfs(pdf_paths: list[Path], job_id: str | None = None) -> dict[str, int]:
    documents = []
    for path in pdf_paths:
        progress = (
            lambda done, total, name=path.name: update_job(
                job_id, "running", f"OCR: {name}, page {done}/{total}."
            )
            if job_id
            else None
        )
        documents.append(render_pdf_to_imported_document(path, progress=progress))
    for document in documents:
        merge_imported_document(document)
    result = rebuild_catalog(skip_ocr_pages=False, regenerate_assets=True, job_id=job_id)
    return {"documents": len(documents), "pages": sum(item["pages_imported"] for item in documents), **result}


def run_upload_job(job_id: str, pdf_paths: list[str]) -> None:
    paths = [Path(path) for path in pdf_paths]
    try:
        update_job(job_id, "running", f"Processing {len(paths)} uploaded PDF(s). OCR can take a while.")
        result = process_saved_pdfs(paths, job_id=job_id)
        update_job(
            job_id,
            "complete",
            f"Processed {result['documents']} document(s), {result['pages']} page(s). "
            f"Published reports: {result['publishable_reports']}; waiting for a later budgeted batch: {result['reports_awaiting_verification']}; "
            f"original diagrams in this batch: {result['public_diagrams_in_batch']}.",
            **result,
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_pilot_import_job(job_id: str, filename: str) -> None:
    source = next((path for path in uploaded_pdf_paths() if path.name == filename), None)
    if source is None:
        update_job(job_id, "failed", "The selected PDF is not stored on the server. Upload it first.")
        return
    try:
        update_job(job_id, "running", f"Archiving earlier derived data and starting a clean local pilot index for {filename}.")
        archive_dir = reset_derived_data_for_pilot()
        document = render_pdf_to_imported_document(
            source,
            progress=lambda done, total: update_job(
                job_id, "running", f"Local OCR pilot: {filename}, page {done}/{total}. No AI requests are made."
            ),
            allow_ai_cleanup=False,
        )
        result = index_documents_without_ai([document], job_id=job_id)
        update_job(
            job_id,
            "complete",
            f"Clean pilot index complete for {filename}: {result['pages']} page(s), "
            f"{result['vehicle_links']} vehicle link(s), {result['mapped_system_codes']} system(s). "
            "No AI requests were made. Press Publish next report only when you want to spend on verification.",
            archive_dir=str(archive_dir),
            **result,
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_reprocess_job(job_id: str) -> None:
    paths = uploaded_pdf_paths()
    if not paths:
        update_job(job_id, "failed", "No uploaded PDFs found. Upload documents first, then run Re-OCR.")
        return
    try:
        update_job(job_id, "running", f"Re-OCR is running for {len(paths)} uploaded PDF(s).")
        result = reprocess_uploaded_pdfs(job_id=job_id)
        update_job(
            job_id,
            "complete",
            f"Reprocessed {result['documents']} document(s), {result['pages']} page(s). "
            f"Published reports: {result['publishable_reports']}; waiting for a later budgeted batch: {result['reports_awaiting_verification']}; "
            f"original diagrams in this batch: {result['public_diagrams_in_batch']}.",
            **result,
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_full_catalog_index_job(job_id: str) -> None:
    paths = uploaded_pdf_paths()
    if not paths:
        update_job(job_id, "failed", "No stored PDFs found. Upload documents first.")
        return
    try:
        update_job(job_id, "running", f"Indexing {len(paths)} stored PDF(s) with local OCR only. This step makes no AI requests.")
        result = index_all_uploaded_pdfs_without_ai(job_id=job_id)
        update_job(
            job_id,
            "complete",
            f"Indexed {result['documents']} stored PDF(s), {result['pages']} page(s), "
            f"{result['vehicle_links']} vehicle link(s), and {result['mapped_system_codes']} report system(s). "
            f"Published verified reports retained: {result['published_reports']}. "
            "Use controlled report batches to publish the remaining reports.",
            **result,
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_owner_library_pipeline_job(job_id: str) -> None:
    paths = uploaded_pdf_paths()
    try:
        if paths:
            update_job(job_id, "running", f"Preparing {len(paths)} stored PDF(s): local OCR index first, no AI requests yet.")
            indexed = index_all_uploaded_pdfs_without_ai(job_id=job_id)
            update_job(
                job_id,
                "running",
                f"Index ready: {indexed['vehicle_links']} vehicle link(s), {indexed['mapped_system_codes']} system(s). "
                "Now verifying the next controlled report batch.",
                **indexed,
            )
        else:
            restored = ensure_bundled_parser_seed()
            candidates_path = get_settings().data_dir / "parser_candidates.json"
            if not candidates_path.exists():
                update_job(job_id, "failed", "No stored PDFs or packaged OCR catalog found. Upload documents first.")
                return
            indexed = LocksmithRepository().dashboard_counts()
            message = "Using packaged OCR catalog from deployment seed." if restored else "Using existing packaged OCR catalog."
            update_job(
                job_id,
                "running",
                f"{message} Now verifying the next controlled report batch.",
                **indexed,
            )
        result = rebuild_catalog(skip_ocr_pages=True, regenerate_assets=True, job_id=job_id)
        update_job(
            job_id,
            "complete",
            f"Library pipeline complete. Published reports: {result['publishable_reports']}; "
            f"waiting for later budgeted batches: {result['reports_awaiting_verification']}; "
            f"rejected for correction: {result['rejected_reports']}; original diagrams: {result['public_diagrams_in_batch']}.",
            **{**indexed, **result},
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_rebuild_job(job_id: str, target_system_code: str | None = None) -> None:
    try:
        target_message = f" for {target_system_code}" if target_system_code else ""
        update_job(job_id, "running", f"Building a verified structured report{target_message} from saved OCR pages.")
        # The catalog has already been indexed before reports enter the publish
        # queue. Re-importing every OCR page here makes a single report publish
        # appear stuck and needlessly rewrites the active lookup data.
        result = rebuild_catalog(skip_ocr_pages=True, regenerate_assets=True, job_id=job_id, target_system_code=target_system_code)
        update_job(
            job_id,
            "complete",
            f"Published reports: {result['publishable_reports']}; waiting for a later budgeted batch: {result['reports_awaiting_verification']}; "
            f"rejected for correction: {result['rejected_reports']}; original diagrams in this batch: {result['public_diagrams_in_batch']}.",
            **result,
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_publish_next_batch_job(job_id: str) -> None:
    """Publish the next budget-limited queued reports after the catalog is indexed."""
    try:
        if has_running_job("upload", "reprocess", "index", "pipeline"):
            update_job(
                job_id,
                "failed",
                "A document OCR/index job is still running. Publish a selected report from the existing index, or wait for indexing to finish.",
            )
            return
        update_job(job_id, "running", "Verifying the next queued report batch from saved OCR pages.")
        result = rebuild_catalog(skip_ocr_pages=True, regenerate_assets=True, job_id=job_id)
        update_job(
            job_id,
            "complete",
            f"Batch complete. Published reports: {result['publishable_reports']}; "
            f"waiting for later batches: {result['reports_awaiting_verification']}; "
            f"rejected for correction: {result['rejected_reports']}; "
            f"original diagrams available: {result['public_diagrams_in_batch']}.",
            **result,
        )
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_asset_import_job(job_id: str) -> None:
    try:
        update_job(job_id, "running", "Importing prepared public diagrams from the asset manifest.")
        import_prepared_assets()
        update_job(job_id, "complete", "Prepared public diagrams imported.")
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_asset_regeneration_job(job_id: str) -> None:
    try:
        update_job(job_id, "running", "Deep visual recovery is analyzing source pages with additional AI requests.")
        init_schema()
        run_module("locksmith_docs.media.extract_assets", "--to-db", "--job-id", job_id)
        update_job(job_id, "complete", "Additional original diagrams built and added without removing existing diagrams.")
    except Exception as exc:
        update_job(job_id, "failed", f"{type(exc).__name__}: {exc}")


def run_module(module: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", module, *args], check=True)


def ingest_uploaded_pdf(source_file, filename: str) -> dict[str, Any]:
    pdf_path = save_uploaded_pdf(source_file, filename)
    document = render_pdf_to_imported_document(pdf_path)
    merge_imported_document(document)
    rebuild_catalog(skip_ocr_pages=False)
    return {
        "name": document["name"],
        "pages": document["pages_imported"],
        "status": "processed",
    }
