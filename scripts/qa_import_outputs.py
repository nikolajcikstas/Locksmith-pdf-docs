from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from locksmith_docs.parsing.canonical_models import CANONICAL_MODELS, canonicalize_model
from locksmith_docs.reports.ai_report_cleaner import report_quality_issues, report_completeness_issues

BAD_TEXT_RE = re.compile(
    r"[^\x00-\x7f]|Meth0|L0ck|d0or|cylind\+|AutoSmart|Michael Hyde|[A-Z]{2,5}-\d+\s+SECTION|continued\s*-|\||SI5MHz|Decbder|theXdbot|determinethe|wcrking|proyession",
    re.IGNORECASE,
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(walk_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(walk_strings(item))
        return out
    return []


def diagram_quality(path: Path, asset: dict[str, Any]) -> dict[str, Any]:
    if path.suffix.lower() != ".svg":
        return {"ok": False, "reason": "public source/raster image is not permitted"}
    if not path.exists():
        return {"ok": False, "reason": "missing generated diagram"}
    svg = path.read_text(encoding="utf-8")
    if "<svg" not in svg or "<image" in svg.lower():
        return {"ok": False, "reason": "invalid or embedded-source SVG"}
    diagram_data = asset.get("diagram_data") or {}
    if not isinstance(diagram_data, dict) or not diagram_data.get("panels"):
        return {"ok": False, "reason": "missing structured diagram_data"}
    if not any(
        isinstance(panel, dict)
        and panel.get("label")
        and len([row for row in (panel.get("rows") or []) if isinstance(row, list) and any(str(cell or "").strip() for cell in row)]) >= 2
        for panel in diagram_data.get("panels", [])
    ):
        return {"ok": False, "reason": "diagram has no usable panel rows"}
    return {"ok": True}


def main() -> None:
    drafts_path = ROOT / "data" / "report_drafts.json"
    assets_path = ROOT / "data" / "asset_candidates.json"
    candidates_path = ROOT / "data" / "parser_candidates.json"
    drafts = load_json(drafts_path) if drafts_path.exists() else []
    assets = load_json(assets_path) if assets_path.exists() else []
    candidates = load_json(candidates_path) if candidates_path.exists() else {}

    bad_drafts = []
    sections_by_key = {
        (item.get("code"), item.get("source_document")): item.get("raw_text", "")
        for item in candidates.get("sections", [])
    }
    for item in drafts:
        draft = item.get("draft", item)
        strings = walk_strings(draft)
        bad_strings = [text for text in strings if BAD_TEXT_RE.search(text)]
        bad_strings.extend(report_quality_issues(draft))
        bad_strings.extend(
            report_completeness_issues(
                draft,
                sections_by_key.get((item.get("system_code"), item.get("source_document")), ""),
            )
        )
        if not item.get("ai_verified"):
            bad_strings.append("Report was not verified against source page images by AI.")
        if bad_strings:
            bad_drafts.append(
                {
                    "system_code": item.get("system_code") or draft.get("code"),
                    "examples": bad_strings[:3],
                }
            )

    bad_assets = []
    ok_assets = 0
    for asset in assets:
        public_path = str(asset.get("public_path") or "")
        if not public_path.startswith("/static/assets/"):
            continue
        path = ROOT / public_path.lstrip("/")
        result = diagram_quality(path, asset)
        text_blob = " ".join(str(asset.get(key) or "") for key in ("title", "rewritten_caption", "extracted_text"))
        looks_like_header = bool(re.search(r"\b(?:SECTION|continued)\b", text_blob, re.IGNORECASE))
        if result["ok"] and not looks_like_header:
            ok_assets += 1
        else:
            bad_assets.append(
                {
                    "id": asset.get("id"),
                    "system_code": asset.get("system_code"),
                    "public_path": public_path,
                    "reason": "section/header text" if looks_like_header else result.get("reason"),
                }
            )

    cleaned_models: dict[str, set[str]] = defaultdict(set)
    for vehicle in candidates.get("vehicle_applications", []):
        make = str(vehicle.get("make") or "")
        model = str(vehicle.get("model") or "")
        if make not in CANONICAL_MODELS:
            continue
        fixed, score = canonicalize_model(make, model)
        if score >= 0.78:
            cleaned_models[make].add(fixed)
    missing_model_makes = sorted(make for make in CANONICAL_MODELS if make not in cleaned_models)

    print(json.dumps(
        {
            "drafts_total": len(drafts),
            "drafts_with_suspicious_text": len(bad_drafts),
            "draft_examples": bad_drafts[:10],
            "assets_total": len(assets),
            "assets_passing_basic_quality": ok_assets,
            "asset_failures": bad_assets[:20],
            "canonical_makes_without_parsed_models": missing_model_makes,
            "cleaned_model_counts": {make: len(models) for make, models in sorted(cleaned_models.items())},
        },
        indent=2,
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
