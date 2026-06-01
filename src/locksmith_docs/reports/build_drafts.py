from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from locksmith_docs.reports.draft_builder import build_report_draft
from locksmith_docs.parsing.section_vehicle_parser import extract_vehicle_applications_from_sections
from locksmith_docs.reports.verified_facts import apply_verified_facts
from locksmith_docs.reports.ai_report_cleaner import (
    clean_report_draft_with_ai,
    report_cleanup_enabled,
    report_completeness_issues,
    report_quality_issues,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "parser_candidates.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "report_drafts.json"
DEFAULT_CACHE = PROJECT_ROOT / "data" / "ai_report_cache.json"
DEFAULT_PAGES_INPUT = PROJECT_ROOT / "data" / "imported_pages.json"
DEFAULT_VERIFIED_FACTS = PROJECT_ROOT / "data" / "verified_report_facts.json"
CACHE_VERSION = "complete-report-v29-title-diagrams-cloners"


def load_sections(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("sections", []))


def maximum_source_pages_per_report() -> int:
    try:
        return max(1, int(os.environ.get("AI_REPORT_MAX_SOURCE_PAGES", "12")))
    except ValueError:
        return 12


def has_safe_source_span(section: dict[str, Any]) -> bool:
    start = int(section.get("page_start") or 0)
    end = int(section.get("page_end") or start)
    return start > 0 and end >= start and (end - start + 1) <= maximum_source_pages_per_report()


def load_page_image_lookup(path: Path) -> dict[tuple[str, int], Path]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    lookup: dict[tuple[str, int], Path] = {}
    for document in payload if isinstance(payload, list) else payload.get("documents", []):
        name = str(document.get("name") or "")
        for page in document.get("pages", []):
            image_path = page.get("image_path")
            if not image_path:
                continue
            resolved = PROJECT_ROOT / image_path
            if resolved.exists():
                lookup[(name, int(page.get("page_number") or 0))] = resolved
    return lookup


def cache_key(section: dict[str, Any]) -> str:
    model = os.environ.get("OPENAI_REPORT_MODEL", os.environ.get("OPENAI_OCR_MODEL", "gpt-4o-mini"))
    mode = "ai" if report_cleanup_enabled() else "local"
    raw = f"{CACHE_VERSION}|{mode}|{model}|{section.get('code')}|{section.get('raw_text') or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def new_ai_request_limit() -> int:
    try:
        return max(0, int(os.environ.get("AI_MAX_NEW_REPORTS_PER_RUN", "1")))
    except ValueError:
        return 1


def source_image_input_enabled() -> bool:
    return os.environ.get("REPORT_ATTACH_SOURCE_IMAGES", "1") != "0"


def source_image_limit() -> int:
    try:
        return max(1, min(12, int(os.environ.get("AI_REPORT_SOURCE_IMAGE_LIMIT", "12"))))
    except ValueError:
        return 12


def draft_issue_count(draft: dict[str, Any], source_text: str) -> int:
    return len(report_quality_issues(draft) + report_completeness_issues(draft, source_text))


def load_prior_draft_candidates(
    output_path: Path,
    cache_path: Path,
) -> dict[str, list[dict[str, Any]]]:
    """Retain useful partial extraction work across budget-controlled AI passes."""
    candidates: dict[str, list[dict[str, Any]]] = {}
    if output_path.exists():
        try:
            prior_output = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prior_output = []
        for item in prior_output if isinstance(prior_output, list) else []:
            draft = item.get("draft") if isinstance(item, dict) else None
            code = str(item.get("system_code") or "") if isinstance(item, dict) else ""
            if code and isinstance(draft, dict):
                candidates.setdefault(code, []).append(draft)
    for cached_value in load_cache(cache_path).values():
        if not isinstance(cached_value, dict):
            continue
        draft = cached_value.get("draft") if cached_value.get("__rejected__") is True else cached_value
        code = str(draft.get("code") or "") if isinstance(draft, dict) else ""
        if code and isinstance(draft, dict):
            candidates.setdefault(code, []).append(draft)
    return candidates


def section_application_confidence(section: dict[str, Any]) -> float:
    applications = extract_vehicle_applications_from_sections([section])
    return max((float(item.confidence) for item in applications), default=0.0)


def build_drafts(
    sections: list[dict[str, Any]],
    min_confidence: float,
    output_path: Path = DEFAULT_OUTPUT,
    cache_path: Path = DEFAULT_CACHE,
    page_image_lookup: dict[tuple[str, int], Path] | None = None,
    progress: Callable[[int, int, int, int], None] | None = None,
    publishable_system_codes: set[str] | None = None,
) -> list[dict[str, Any]]:
    drafts = []
    prior_problem_codes: set[str] = set()
    prior_draft_candidates = load_prior_draft_candidates(output_path, cache_path)
    if output_path.exists():
        try:
            prior_output = json.loads(output_path.read_text(encoding="utf-8"))
            prior_problem_codes = {
                str(item.get("system_code") or "")
                for item in prior_output if isinstance(prior_output, list)
                if item.get("ai_verified") and item.get("publication_issues")
            }
        except json.JSONDecodeError:
            prior_problem_codes = set()
    eligible = sorted(
        [
            section
            for section in sections
            if float(section.get("confidence") or 0) >= min_confidence
            and (
                publishable_system_codes is None
                or str(section.get("code") or "") in publishable_system_codes
            )
            and has_safe_source_span(section)
        ],
        key=lambda section: (
            str(section.get("code") or "") not in prior_problem_codes,
            -section_application_confidence(section),
            int(section.get("page_end") or section.get("page_start") or 0) - int(section.get("page_start") or 0),
            str(section.get("code") or ""),
        ),
    )
    repeated_codes = {
        code for code, count in Counter(str(section.get("code") or "") for section in eligible).items()
        if code and count > 1
    }
    ai_enabled = report_cleanup_enabled()
    cache = load_cache(cache_path) if ai_enabled else {}
    reused = 0
    new_requests = 0
    request_limit = new_ai_request_limit()
    unsaved_ai_results = 0
    retry_mode = os.environ.get("REPORT_AI_RETRY_ON_FAILURE", "0")
    page_image_lookup = page_image_lookup or {}
    for index, section in enumerate(eligible, start=1):
        confidence = float(section.get("confidence") or 0)
        draft = build_report_draft(section)
        if not draft.get("code"):
            if progress:
                progress(index, len(eligible), reused, new_requests)
            continue
        source_text = section.get("raw_text") or ""
        previous_candidates = prior_draft_candidates.get(str(draft.get("code") or ""), [])
        if previous_candidates:
            best_previous = min(previous_candidates, key=lambda item: draft_issue_count(item, source_text))
            if draft_issue_count(best_previous, source_text) < draft_issue_count(draft, source_text):
                draft = deepcopy(best_previous)
        key = cache_key(section)
        ai_verified = False
        rejected_cached = False
        processed_by_ai = False
        if key in cache:
            cached_value = cache[key]
            if cached_value.get("__rejected__") is True and isinstance(cached_value.get("draft"), dict):
                if str(cached_value.get("retry_mode", "0")) != retry_mode:
                    cache.pop(key, None)
                    cached_value = None
                else:
                    draft = cached_value["draft"]
                    rejected_cached = True
                    reused += 1
                    cached_value = None
            else:
                cached_draft = cached_value
            if cached_value is not None and not rejected_cached:
                cached_issues = report_quality_issues(cached_draft) + report_completeness_issues(cached_draft, source_text)
                if not cached_issues:
                    draft = cached_draft
                    reused += 1
                    ai_verified = True
                else:
                    cache.pop(key, None)
        if not ai_verified and not rejected_cached and (request_limit == 0 or new_requests < request_limit):
            source_images = [
                page_image_lookup[(str(section.get("source_document") or ""), page_number)]
                for page_number in range(int(section.get("page_start") or 0), int(section.get("page_end") or 0) + 1)
                if (str(section.get("source_document") or ""), page_number) in page_image_lookup
            ][:source_image_limit()] if source_image_input_enabled() else []
            draft, cleaned_by_ai = clean_report_draft_with_ai(draft, source_text, source_images)
            ai_verified = cleaned_by_ai
            if ai_enabled:
                new_requests += 1
                processed_by_ai = True
        draft = apply_verified_facts(draft, str(draft.get("code") or ""), DEFAULT_VERIFIED_FACTS)
        publication_issues = report_quality_issues(draft) + report_completeness_issues(draft, source_text)
        if rejected_cached:
            publication_issues.append("AI verification previously rejected this draft; excluded until it is reprocessed.")
        elif processed_by_ai and not ai_verified:
            publication_issues.append("AI verification did not produce an approvable report; excluded from publication.")
        elif not ai_verified and request_limit and new_requests >= request_limit:
            publication_issues.append("Awaiting AI verification in a subsequent cost-controlled rebuild.")
        if str(section.get("code") or "") in repeated_codes:
            publication_issues.append(
                "System code occurs in separated source sections and must be resolved before publication."
            )
        if ai_enabled and ai_verified and not publication_issues:
            cache[key] = draft
            unsaved_ai_results += 1
            if unsaved_ai_results >= 5:
                save_cache(cache_path, cache)
                unsaved_ai_results = 0
        elif ai_enabled and processed_by_ai:
            cache[key] = {"__rejected__": True, "retry_mode": retry_mode, "draft": draft}
            unsaved_ai_results += 1
            if unsaved_ai_results >= 5:
                save_cache(cache_path, cache)
                unsaved_ai_results = 0
        drafts.append(
            {
                "system_code": draft["code"],
                "source_document": section.get("source_document"),
                "source_pages": [section.get("page_start"), section.get("page_end")],
                "confidence": confidence,
                "ai_verified": ai_verified,
                "publication_issues": publication_issues,
                "draft": draft,
            }
        )
        if index % 10 == 0 or index == len(eligible):
            write_drafts(output_path, drafts)
        if progress:
            progress(index, len(eligible), reused, new_requests)
    if ai_enabled and unsaved_ai_results:
        save_cache(cache_path, cache)
    return drafts


def write_drafts(path: Path, drafts: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(drafts, indent=2, ensure_ascii=False), encoding="utf-8")


def import_to_database(drafts: list[dict[str, Any]], publish: bool = False, replace: bool = False) -> None:
    from locksmith_docs.db.repository import LocksmithRepository

    repo = LocksmithRepository()
    if replace:
        repo.clear_report_drafts()
    for item in drafts:
        issues = item.get("publication_issues") or []
        can_publish = (
            publish
            and bool(item.get("ai_verified"))
            and report_is_publishable(item["draft"], issues)
        )
        if can_publish:
            draft_status = "published"
        elif any("Awaiting AI verification" in issue for issue in issues):
            draft_status = "awaiting_verification"
        elif item.get("ai_verified") or any("previously rejected" in issue for issue in issues):
            draft_status = "rejected"
        else:
            draft_status = "needs_review"
        repo.create_report_draft(
            draft=item["draft"],
            source={
                "source_document": item.get("source_document"),
                "page_start": item.get("source_pages", [None, None])[0],
                "page_end": item.get("source_pages", [None, None])[1],
            },
            confidence=float(item.get("confidence") or 0),
            ai_verified=bool(item.get("ai_verified")),
            publication_issues=issues,
            status=draft_status,
        )
        if can_publish:
            system = dict(item["draft"])
            system["type"] = system.get("system_type") or system.get("type") or ""
            repo.upsert_catalog_system(system, publication_source="ai_verified")
            repo.replace_verified_vehicle_applications(
                system_code=system["code"],
                applications=system.get("vehicle_applications") or [],
                source_document=item.get("source_document"),
                source_pages=item.get("source_pages") or [],
                system_type=system.get("type"),
            )


def report_is_publishable(draft: dict[str, Any], publication_issues: list[str]) -> bool:
    code = str(draft.get("code") or "")
    if not code or not re.fullmatch(r"[A-Z]{2,10}-[A-Z0-9]{1,4}", code):
        return False
    if publication_issues or report_quality_issues(draft):
        return False
    useful_sections = (
        draft.get("job_essentials"),
        draft.get("key_remote"),
        draft.get("mechanical_key"),
        draft.get("programming"),
        draft.get("making_key"),
    )
    return bool(draft.get("vehicle_applications")) and sum(bool(item) for item in useful_sections) >= 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Build structured report drafts from OCR section candidates.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--pages-input", type=Path, default=DEFAULT_PAGES_INPUT)
    parser.add_argument("--min-confidence", type=float, default=0.4)
    parser.add_argument("--to-db", action="store_true", help="Insert generated drafts into report_drafts.")
    parser.add_argument("--publish", action="store_true", help="Also publish drafts into systems for customer lookup.")
    parser.add_argument("--replace", action="store_true", help="Clear stale report drafts before inserting generated reports.")
    parser.add_argument("--job-id", help="Processing job id to update while AI reports are built.")
    parser.add_argument("--system-code", help="Verify only one selected indexed system code.")
    args = parser.parse_args()

    progress = None
    if args.job_id:
        from locksmith_docs.processing.job_status import update_job

        def progress(done: int, total: int, reused: int, new_requests: int) -> None:
            update_job(args.job_id, "running", f"Building reports: {done}/{total} checked, {reused} reused, {new_requests} new AI requests in this run.")

    drafts = build_drafts(
        load_sections(args.input),
        args.min_confidence,
        args.output,
        args.cache,
        load_page_image_lookup(args.pages_input),
        progress,
        {args.system_code} if args.system_code else None,
    )
    write_drafts(args.output, drafts)
    if args.to_db:
        import_to_database(drafts, publish=args.publish, replace=args.replace)
    print(f"Built {len(drafts)} report drafts -> {args.output}")


if __name__ == "__main__":
    main()
