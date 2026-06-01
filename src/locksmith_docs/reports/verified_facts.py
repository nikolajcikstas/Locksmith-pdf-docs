from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def load_verified_facts(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def apply_verified_facts(
    draft: dict[str, Any],
    system_code: str,
    facts_path: Path,
) -> dict[str, Any]:
    """Apply facts explicitly confirmed against the source page during QA."""
    facts = load_verified_facts(facts_path).get(system_code)
    if not isinstance(facts, dict):
        return draft
    merged = deepcopy(draft)
    return _merge(merged, facts)


def _merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            target[key] = _merge(target[key], value)
        else:
            target[key] = deepcopy(value)
    return target
