from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from locksmith_docs.core.config import get_settings


def status_path() -> Path:
    path = get_settings().storage_dir / "processing_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_jobs() -> list[dict[str, Any]]:
    path = status_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    status_path().write_text(json.dumps(jobs[-20:], indent=2, ensure_ascii=False), encoding="utf-8")


def start_job(kind: str, label: str) -> str:
    jobs = load_jobs()
    job_id = f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    jobs.append(
        {
            "id": job_id,
            "kind": kind,
            "label": label,
            "status": "running",
            "message": "Queued for processing.",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    )
    save_jobs(jobs)
    return job_id


def update_job(job_id: str, status: str, message: str, **extra: Any) -> None:
    jobs = load_jobs()
    for job in jobs:
        if job.get("id") == job_id:
            job["status"] = status
            job["message"] = message
            job["updated_at"] = now_iso()
            job.update(extra)
            break
    save_jobs(jobs)


def latest_jobs(limit: int = 6) -> list[dict[str, Any]]:
    jobs = load_jobs()
    changed = False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
    for index, job in enumerate(jobs):
        if job.get("status") != "running":
            continue
        try:
            updated_at = datetime.fromisoformat(str(job.get("updated_at") or ""))
        except ValueError:
            updated_at = cutoff - timedelta(seconds=1)
        later_finished_job_exists = any(
            next_job.get("status") in {"complete", "failed", "interrupted"}
            for next_job in jobs[index + 1:]
        )
        if updated_at < cutoff or later_finished_job_exists:
            job["status"] = "interrupted"
            job["message"] = "Processing stopped before completion. Start a new rebuild to continue."
            job["updated_at"] = now_iso()
            changed = True
    if changed:
        save_jobs(jobs)
    return list(reversed(jobs))[:limit]


def interrupt_running_jobs(message: str = "Processing stopped before completion. Start a new rebuild to continue.") -> None:
    jobs = load_jobs()
    changed = False
    for job in jobs:
        if job.get("status") == "running":
            job["status"] = "interrupted"
            job["message"] = message
            job["updated_at"] = now_iso()
            changed = True
    if changed:
        save_jobs(jobs)


def has_running_job(*kinds: str) -> bool:
    wanted = {kind for kind in kinds if kind}
    for job in load_jobs():
        if job.get("status") != "running":
            continue
        if not wanted or str(job.get("kind") or "") in wanted:
            return True
    return False
