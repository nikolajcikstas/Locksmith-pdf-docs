from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from locksmith_docs.core.config import get_settings


def build_quality_report(candidates_path: Path) -> str:
    data = json.loads(candidates_path.read_text(encoding="utf-8"))
    candidates = data.get("vehicle_applications", [])
    page_stats = data.get("page_stats", [])
    section_stats = data.get("stats", {})

    by_make = Counter(candidate["make"] for candidate in candidates)
    by_system_prefix = Counter(candidate["system_code"].split("-")[0] for candidate in candidates)
    by_page = defaultdict(int)
    low_confidence = []
    suspicious = []

    for candidate in candidates:
        by_page[(candidate["source_document"], candidate["source_page"])] += 1
        if float(candidate.get("confidence") or 0) < 0.65:
            low_confidence.append(candidate)
        if is_suspicious_candidate(candidate):
            suspicious.append(candidate)

    weak_pages = [
        stat for stat in page_stats
        if stat.get("candidates", 0) == 0 or stat.get("words", 0) < 20 or stat.get("error")
    ]

    lines = [
        "# Parser Quality Report",
        "",
        f"Source: `{candidates_path}`",
        "",
        "## Summary",
        "",
        f"- Pages processed: {len(page_stats)}",
        f"- Section candidates: {section_stats.get('section_candidates', 0)}",
        f"- Vehicle candidates: {len(candidates)}",
        f"- Low-confidence candidates: {len(low_confidence)}",
        f"- Suspicious model strings: {len(suspicious)}",
        f"- Weak pages: {len(weak_pages)}",
        "",
        "## Candidates By Make",
        "",
    ]

    for make, count in by_make.most_common():
        lines.append(f"- {make}: {count}")

    lines.extend(["", "## Candidates By System Prefix", ""])
    for prefix, count in by_system_prefix.most_common():
        lines.append(f"- {prefix}: {count}")

    lines.extend(["", "## Weak Pages", ""])
    if not weak_pages:
        lines.append("No weak pages detected.")
    else:
        for stat in weak_pages:
            lines.append(
                f"- {stat.get('document')} page {stat.get('page')}: "
                f"{stat.get('candidates', 0)} candidates, {stat.get('words', 0)} words"
                + (f", error={stat.get('error')}" if stat.get("error") else "")
            )

    lines.extend(["", "## Low Confidence Samples", ""])
    for candidate in low_confidence[:25]:
        lines.append(
            f"- {candidate['make']} {candidate['model']} {candidate['year_from']}-{candidate['year_to']} "
            f"-> {candidate['system_code']} ({candidate['confidence']})"
        )

    lines.extend(["", "## Suspicious Model Samples", ""])
    if not suspicious:
        lines.append("No suspicious model strings detected by the current heuristics.")
    for candidate in suspicious[:40]:
        lines.append(
            f"- {candidate['make']} {candidate['model']} {candidate['year_from']}-{candidate['year_to']} "
            f"-> {candidate['system_code']}"
        )

    return "\n".join(lines) + "\n"


def is_suspicious_candidate(candidate: dict) -> bool:
    model = candidate.get("model") or ""
    lowered = model.lower()
    bad_tokens = ("fcc", "mhz", "part", "code", "card", "startcut", "cutto", "reader", "decoder")
    if any(token in lowered for token in bad_tokens):
        return True
    if any(char.isdigit() for char in model) and len(model) > 18:
        return True
    if model.count("(") != model.count(")"):
        return True
    return len(model) < 2 or len(model) > 45


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    settings = get_settings()
    default_name = "parser_candidates.json" if (settings.data_dir / "parser_candidates.json").exists() else "toc_candidates.json"
    input_path = Path(args.input) if args.input else settings.data_dir / default_name
    out_path = Path(args.out) if args.out else settings.project_root / "docs" / "PARSER_QUALITY.md"
    out_path.write_text(build_quality_report(input_path), encoding="utf-8")
    print(out_path)


if __name__ == "__main__":
    main()
