from __future__ import annotations

import re


REPLACEMENTS = [
    (r"\bCheck with dealer for correct part # by VIN\b", "Verify the exact part number with a VIN-based parts lookup before ordering."),
    (r"\bPIN Required\b", "PIN access is required."),
    (r"\bNo Cloning for this System\b", "This system should be treated as non-clonable; plan for programmer-based enrollment."),
    (r"\bCheck with your key programmer supplier\b", "Confirm support in the programmer application or with the tool vendor before connecting."),
]


def rewrite_sentence(text: str) -> str:
    rewritten = " ".join(text.split())
    for pattern, replacement in REPLACEMENTS:
        rewritten = re.sub(pattern, replacement, rewritten, flags=re.IGNORECASE)
    return rewritten


def rewrite_steps(raw_lines: list[str]) -> list[str]:
    output: list[str] = []
    for line in raw_lines:
        clean = rewrite_sentence(line)
        if clean and clean not in output:
            output.append(clean)
    return output


def rewrite_policy() -> dict:
    return {
        "goal": "Preserve technical meaning while changing wording, structure, and presentation.",
        "rules": [
            "Do not publish raw OCR text as customer-facing copy.",
            "Extract facts into fields first: parts, FCC, chip, blade, tools, procedures, warnings.",
            "Rewrite procedural text into field-ready steps with prerequisites, action, verification and failure cases.",
            "Keep exact factual identifiers unchanged: part numbers, FCC IDs, keyways, tool names, code series, depths and spacing.",
            "Flag low-confidence OCR for review instead of publishing it.",
        ],
    }
