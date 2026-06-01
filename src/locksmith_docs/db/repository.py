from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from locksmith_docs.db.connection import get_connection
from locksmith_docs.parsing.canonical_models import CANONICAL_MODELS, canonicalize_model


@dataclass(frozen=True)
class VehicleQuery:
    make: str
    model: str
    year: int


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def clamp_ocr_quality(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(-999.99, min(999.99, round(score, 2)))


def clamp_numeric_5_2(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(-999.99, min(999.99, round(score, 2)))


def system_quality_score(system: dict[str, Any]) -> int:
    score = 0
    for key in ("job_essentials", "mechanical_key", "transponder", "programming"):
        value = system.get(key) or {}
        if isinstance(value, dict):
            score += sum(1 for item in value.values() if item not in (None, "", [], {}, "Check source"))
    key_remote = system.get("key_remote") or {}
    if isinstance(key_remote, dict):
        known_options = key_remote.get("known_options")
        score += len(known_options if isinstance(known_options, list) else []) * 3
        score += sum(1 for item in (key_remote.get("remote_type"), key_remote.get("frequency"), key_remote.get("fcc_id")) if item)
    making_key = system.get("making_key") or {}
    if isinstance(making_key, dict):
        score += len(making_key.get("methods") if isinstance(making_key.get("methods"), list) else []) * 2
        score += len(making_key.get("field_workflow") if isinstance(making_key.get("field_workflow"), list) else []) * 2
        score += len(making_key.get("service_notes") if isinstance(making_key.get("service_notes"), list) else [])
    source_facts = system.get("source_facts") or {}
    if isinstance(source_facts, dict):
        score += sum(len(items) for items in source_facts.values() if isinstance(items, list))
    score += len(system.get("decoders") if isinstance(system.get("decoders"), list) else []) * 2
    score += len(system.get("lock_parts") if isinstance(system.get("lock_parts"), list) else []) * 3
    score += len(system.get("troubleshooting") if isinstance(system.get("troubleshooting"), list) else [])
    return score


def clean_model_for_display(make: str, model: str) -> str:
    if is_suspicious_display_model(model):
        fixed, score = canonicalize_model(make, model)
        if score >= 0.78:
            return fixed
        return ""
    if make in CANONICAL_MODELS:
        fixed, score = canonicalize_model(make, model)
        if score >= 0.78:
            return fixed
        return ""
    return model


def is_suspicious_display_model(model: str) -> bool:
    lowered = model.lower()
    if any(token in lowered for token in ("wp", "etow", "stancut", "cutto", "fcc", "reader", "code", "macs")):
        return True
    if re.search(r"[(){}]", model):
        return True
    if len(model) > 36:
        return True
    return False


class LocksmithRepository:
    def list_makes(self) -> list[str]:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT make FROM vehicle_applications ORDER BY make"
            ).fetchall()
        return [row["make"] for row in rows if self.list_models(row["make"])]

    def list_models(self, make: str) -> list[str]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT model
                FROM vehicle_applications
                WHERE lower(make) = lower(%s)
                  AND model !~ '[{}]'
                  AND length(model) <= 36
                ORDER BY model
                """,
                (make,),
            ).fetchall()
        models = []
        for row in rows:
            model = clean_model_for_display(make, row["model"])
            if model and model not in models:
                models.append(model)
        return sorted(models)

    def list_report_targets(self) -> list[dict[str, Any]]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT make, model, year_from, year_to, system_code
                FROM vehicle_applications
                WHERE system_code <> 'UNMATCHED'
                ORDER BY make, model, year_from, year_to, system_code
                """
            ).fetchall()
        targets = []
        seen: set[tuple[str, str, int, int, str]] = set()
        for row in rows:
            model = clean_model_for_display(row["make"], row["model"])
            if not model:
                continue
            key = (row["make"], model, int(row["year_from"]), int(row["year_to"]), row["system_code"])
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "make": key[0],
                    "model": key[1],
                    "year_from": key[2],
                    "year_to": key[3],
                    "system_code": key[4],
                }
            )
        return targets

    def find_vehicle(self, query: VehicleQuery) -> dict[str, Any] | None:
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM vehicle_applications
                WHERE lower(make) = lower(%s)
                  AND lower(model) = lower(%s)
                  AND year_from <= %s
                  AND year_to >= %s
                ORDER BY year_to - year_from ASC
                LIMIT 1
                """,
                (query.make, query.model, query.year, query.year),
            ).fetchone()

    def find_vehicle_best_effort(self, query: VehicleQuery) -> dict[str, Any] | None:
        exact = self.find_vehicle(query)
        if exact:
            return exact
        display_match = self.find_vehicle_by_display_model(query)
        if display_match:
            return display_match
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM vehicle_applications
                WHERE lower(make) = lower(%s)
                  AND lower(model) = lower(%s)
                ORDER BY
                  CASE WHEN year_from <= %s AND year_to >= %s THEN 0 ELSE 1 END,
                  abs(year_from - %s) ASC,
                  year_to DESC
                LIMIT 1
                """,
                (query.make, query.model, query.year, query.year, query.year),
            ).fetchone()

    def find_vehicle_by_display_model(self, query: VehicleQuery) -> dict[str, Any] | None:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM vehicle_applications
                WHERE lower(make) = lower(%s)
                ORDER BY
                  CASE WHEN year_from <= %s AND year_to >= %s THEN 0 ELSE 1 END,
                  abs(year_from - %s) ASC,
                  year_to DESC
                """,
                (query.make, query.year, query.year, query.year),
            ).fetchall()
        wanted = query.model.casefold()
        for row in rows:
            display = clean_model_for_display(query.make, row["model"])
            if display and display.casefold() == wanted:
                return row
        return None

    def get_system(self, system_code: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            return conn.execute(
                "SELECT * FROM systems WHERE code = %s AND status = 'approved' AND publication_source = 'ai_verified'",
                (system_code,),
            ).fetchone()

    def get_published_or_draft_system(self, system_code: str) -> dict[str, Any] | None:
        # Drafts remain available for processing diagnostics, never for customers.
        return self.get_system(system_code)

    def list_assets_for_system(self, system_code: str, public_only: bool = True) -> list[dict[str, Any]]:
        visibility_clause = "AND visibility = 'public'" if public_only else ""
        with get_connection() as conn:
            return conn.execute(
                f"""
                SELECT *
                FROM assets
                WHERE system_code = %s
                  {visibility_clause}
                ORDER BY usefulness_score DESC NULLS LAST, title
                """,
                (system_code,),
            ).fetchall()

    def system_exists(self, system_code: str) -> bool:
        if not system_code or system_code == "UNMATCHED":
            return False
        with get_connection() as conn:
            row = conn.execute("SELECT 1 FROM systems WHERE code = %s", (system_code,)).fetchone()
        return row is not None

    def clear_public_assets(self) -> None:
        with get_connection() as conn:
            conn.execute("DELETE FROM assets WHERE visibility = 'public'")
            conn.commit()

    def clear_public_assets_for_systems(self, system_codes: list[str]) -> None:
        if not system_codes:
            return
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM assets WHERE visibility = 'public' AND system_code = ANY(%s)",
                (system_codes,),
            )
            conn.commit()

    def retract_unpublishable_systems(self, system_codes: list[str]) -> None:
        if not system_codes:
            return
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE systems
                SET status = 'retracted'
                WHERE code = ANY(%s) AND publication_source = 'ai_verified'
                """,
                (system_codes,),
            )
            conn.execute("DELETE FROM assets WHERE system_code = ANY(%s)", (system_codes,))
            conn.commit()

    def dashboard_counts(self) -> dict[str, int]:
        queries = {
            "documents": "SELECT count(*) FROM documents",
            "ocr_pages": "SELECT count(*) FROM document_pages",
            "vehicle_links": "SELECT count(*) FROM vehicle_applications",
            "mapped_system_codes": "SELECT count(DISTINCT system_code) FROM vehicle_applications WHERE system_code <> 'UNMATCHED'",
            "published_reports": "SELECT count(*) FROM systems WHERE status = 'approved' AND publication_source = 'ai_verified'",
            "report_drafts": "SELECT count(*) FROM report_drafts",
            "reports_waiting": "SELECT count(*) FROM report_drafts WHERE status = 'awaiting_verification'",
            "reports_rejected": "SELECT count(*) FROM report_drafts WHERE status = 'rejected'",
            "open_review_items": "SELECT count(*) FROM review_queue WHERE status = 'open'",
            "public_diagrams": "SELECT count(*) FROM assets WHERE visibility = 'public'",
            "pending_assets": "SELECT count(*) FROM assets WHERE review_status = 'pending'",
        }
        counts: dict[str, int] = {}
        with get_connection() as conn:
            for key, sql in queries.items():
                row = conn.execute(sql).fetchone()
                counts[key] = int(row["count"])
        return counts

    def enqueue_review(self, item_type: str, item_ref: str, title: str, payload: dict[str, Any]) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO review_queue (item_type, item_ref, title, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (item_type, item_ref)
                DO UPDATE SET title = EXCLUDED.title,
                              payload = EXCLUDED.payload
                """,
                (item_type, item_ref, title, _json(payload)),
            )
            conn.commit()

    def list_review_items(self, status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM review_queue
                WHERE status = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (status, limit),
            ).fetchall()

    def get_review_item(self, item_id: int) -> dict[str, Any] | None:
        with get_connection() as conn:
            return conn.execute("SELECT * FROM review_queue WHERE id = %s", (item_id,)).fetchone()

    def update_review_status(self, item_id: int, status: str) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE review_queue
                SET status = %s,
                    reviewed_at = CASE WHEN %s IN ('approved', 'rejected') THEN now() ELSE reviewed_at END
                WHERE id = %s
                """,
                (status, status, item_id),
            )
            conn.commit()

    def create_report_draft(
        self,
        draft: dict[str, Any],
        source: dict[str, Any],
        confidence: float,
        ai_verified: bool = False,
        publication_issues: list[str] | None = None,
        status: str = "needs_review",
    ) -> int:
        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO report_drafts
                  (system_code, source_document, source_pages, draft, confidence, ai_verified, publication_issues, status)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)
                ON CONFLICT (system_code, source_document, source_pages)
                DO UPDATE SET draft = EXCLUDED.draft,
                              confidence = EXCLUDED.confidence,
                              ai_verified = EXCLUDED.ai_verified,
                              publication_issues = EXCLUDED.publication_issues,
                              status = EXCLUDED.status,
                              updated_at = now()
                RETURNING id
                """,
                (
                    draft["code"],
                    source.get("source_document"),
                    [source.get("page_start"), source.get("page_end")],
                    _json(draft),
                    confidence,
                    ai_verified,
                    _json(publication_issues or []),
                    status,
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])

    def clear_report_drafts(self) -> None:
        with get_connection() as conn:
            conn.execute("DELETE FROM report_drafts")
            conn.commit()

    def list_report_drafts(self, status: str = "needs_review", limit: int = 100) -> list[dict[str, Any]]:
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM report_drafts
                WHERE status = %s
                ORDER BY confidence DESC, created_at DESC
                LIMIT %s
                """,
                (status, limit),
            ).fetchall()

    def get_report_draft(self, draft_id: int) -> dict[str, Any] | None:
        with get_connection() as conn:
            return conn.execute("SELECT * FROM report_drafts WHERE id = %s", (draft_id,)).fetchone()

    def approve_report_draft(self, draft_id: int) -> str:
        row = self.get_report_draft(draft_id)
        if not row:
            raise ValueError(f"Report draft {draft_id} not found")

        draft = row["draft"]
        system = dict(draft)
        system["type"] = system.get("system_type") or system.get("type") or ""
        self.upsert_catalog_system(system)

        with get_connection() as conn:
            conn.execute(
                """
                UPDATE report_drafts
                SET status = 'approved', updated_at = now()
                WHERE id = %s
                """,
                (draft_id,),
            )
            conn.commit()
        return str(draft["code"])

    def approve_review_item(self, item_id: int) -> str:
        item = self.get_review_item(item_id)
        if not item:
            raise ValueError(f"Review item {item_id} not found")

        payload = item["payload"]
        if item["item_type"] == "vehicle_application":
            self.upsert_vehicle(
                {
                    "make": payload["make"],
                    "model": payload["model"],
                    "year_from": payload["year_from"],
                    "year_to": payload["year_to"],
                    "system_code": payload["system_code"],
                    "type": payload.get("system_type"),
                    "source_document": payload.get("source_document"),
                    "source_pages": [payload.get("source_page")] if payload.get("source_page") else [],
                }
            )
            self.update_review_status(item_id, "approved")
            return "vehicle_application"

        if item["item_type"] == "system_section":
            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO systems (code, title, system_type, source_coverage, status)
                    VALUES (%s, %s, %s, %s::jsonb, 'draft')
                    ON CONFLICT (code) DO NOTHING
                    """,
                    (
                        payload["code"],
                        payload.get("title") or f"{payload['code']} system",
                        payload.get("fields", {}).get("system_type", ""),
                        _json([f"Imported candidate from {payload['source_document']} pages {payload['page_start']}-{payload.get('page_end')}"]),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO report_sections
                      (system_code, section_key, title, body, sort_order, source_document, source_pages, review_status)
                    VALUES (%s, %s, %s, %s::jsonb, 0, %s, %s, 'approved')
                    ON CONFLICT (system_code, section_key)
                    DO UPDATE SET body = EXCLUDED.body,
                                  source_document = EXCLUDED.source_document,
                                  source_pages = EXCLUDED.source_pages,
                                  review_status = EXCLUDED.review_status
                    """,
                    (
                        payload["code"],
                        "raw_import",
                        "Imported OCR candidate",
                        _json(payload.get("fields", {})),
                        payload.get("source_document"),
                        [payload.get("page_start"), payload.get("page_end")],
                    ),
                )
                conn.commit()
            self.update_review_status(item_id, "approved")
            return "system_section"

        raise ValueError(f"Unsupported review item type: {item['item_type']}")

    def search_pages(self, make: str, model: str, year: str, limit: int = 10) -> list[dict[str, Any]]:
        query = " ".join(part for part in [make, model, year] if part)
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT d.name AS document, p.page_number,
                       ts_rank_cd(p.search_vector, websearch_to_tsquery('english', %s)) AS rank,
                       ts_headline('english', coalesce(p.ocr_text, ''), websearch_to_tsquery('english', %s),
                                   'MaxWords=35, MinWords=12') AS snippet
                FROM document_pages p
                JOIN documents d ON d.id = p.document_id
                WHERE p.search_vector @@ websearch_to_tsquery('english', %s)
                   OR p.ocr_text %% %s
                ORDER BY rank DESC NULLS LAST, d.name, p.page_number
                LIMIT %s
                """,
                (query, query, query, query, limit),
            ).fetchall()

    def list_videos_for_vehicle(self, make: str, model: str, year: int | None, limit: int = 12) -> list[dict[str, Any]]:
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM videos
                WHERE status = 'published'
                  AND (%s = '' OR lower(make) = lower(%s) OR make IS NULL)
                  AND (%s = '' OR lower(model) = lower(%s) OR model IS NULL)
                  AND (%s IS NULL OR year_from IS NULL OR year_from <= %s)
                  AND (%s IS NULL OR year_to IS NULL OR year_to >= %s)
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (make, make, model, model, year, year, year, year, limit),
            ).fetchall()

    def list_blog_posts(self, limit: int = 20) -> list[dict[str, Any]]:
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM blog_posts
                WHERE status = 'published'
                ORDER BY published_at DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

    def get_blog_post(self, slug: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            return conn.execute(
                "SELECT * FROM blog_posts WHERE slug = %s AND status = 'published'",
                (slug,),
            ).fetchone()

    def list_blog_videos(self, limit: int = 12) -> list[dict[str, Any]]:
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM videos
                WHERE status = 'published'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

    def update_video_from_youtube(self, video_id: int, youtube_video_id: str, title: str, description: str) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE videos
                SET youtube_video_id = %s,
                    title = %s,
                    description = %s
                WHERE id = %s
                """,
                (youtube_video_id, title, description, video_id),
            )
            conn.commit()

    def delete_imported_vehicle_links(self) -> None:
        with get_connection() as conn:
            conn.execute("DELETE FROM vehicle_applications WHERE source_document IS NOT NULL")
            conn.commit()

    def delete_generated_system_reports(self) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                DELETE FROM systems
                WHERE source_coverage::text ILIKE %s
                """,
                ("%Source coverage: pages%",),
            )
            conn.commit()

    def upsert_document(self, name: str, source_path: str, pages_total: int) -> int:
        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO documents (name, source_path, pages_total)
                VALUES (%s, %s, %s)
                ON CONFLICT (name)
                DO UPDATE SET source_path = EXCLUDED.source_path,
                              pages_total = EXCLUDED.pages_total
                RETURNING id
                """,
                (name, source_path, pages_total),
            ).fetchone()
            conn.commit()
            return int(row["id"])

    def upsert_page(self, document_id: int, page: dict[str, Any]) -> None:
        ocr_text = page.get("ocr_text") or ""
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO document_pages
                  (document_id, page_number, image_path, ocr_text, ocr_quality, search_vector)
                VALUES
                  (%s, %s, %s, %s, %s, to_tsvector('english', unaccent(%s)))
                ON CONFLICT (document_id, page_number)
                DO UPDATE SET image_path = EXCLUDED.image_path,
                              ocr_text = EXCLUDED.ocr_text,
                              ocr_quality = EXCLUDED.ocr_quality,
                              search_vector = EXCLUDED.search_vector
                """,
                (
                    document_id,
                    page["page_number"],
                    page.get("image_path"),
                    ocr_text,
                    clamp_ocr_quality(page.get("ocr_quality")),
                    ocr_text,
                ),
            )
            conn.commit()

    def upsert_asset_candidate(self, asset: dict[str, Any]) -> bool:
        if not self.system_exists(asset.get("system_code", "")):
            return False
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO assets
                  (id, system_code, title, kind, public_path, source_document,
                   source_page, crop_box, extracted_text, diagram_data, rewritten_caption, placement,
                   usefulness_score, watermark_removed, visibility, review_status)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id)
                DO UPDATE SET system_code = EXCLUDED.system_code,
                              title = EXCLUDED.title,
                              kind = EXCLUDED.kind,
                              public_path = EXCLUDED.public_path,
                              source_document = EXCLUDED.source_document,
                              source_page = EXCLUDED.source_page,
                              crop_box = EXCLUDED.crop_box,
                              extracted_text = EXCLUDED.extracted_text,
                              diagram_data = EXCLUDED.diagram_data,
                              rewritten_caption = EXCLUDED.rewritten_caption,
                              placement = EXCLUDED.placement,
                              usefulness_score = EXCLUDED.usefulness_score,
                              watermark_removed = EXCLUDED.watermark_removed,
                              visibility = EXCLUDED.visibility,
                              review_status = EXCLUDED.review_status
                """,
                (
                    asset["id"],
                    asset["system_code"],
                    asset["title"],
                    asset["kind"],
                    asset.get("public_path"),
                    asset.get("source_document"),
                    asset.get("source_page"),
                    asset.get("crop_box", []),
                    asset.get("extracted_text"),
                    _json(asset.get("diagram_data") or {}),
                    asset.get("rewritten_caption"),
                    asset.get("placement"),
                    clamp_numeric_5_2(asset.get("usefulness_score")),
                    bool(asset.get("watermark_removed")),
                    asset.get("visibility", "internal"),
                    asset.get("review_status", "pending"),
                ),
            )
            conn.commit()
        return True

    def upsert_catalog_system(self, system: dict[str, Any], publication_source: str = "legacy") -> None:
        with get_connection() as conn:
            existing = conn.execute("SELECT * FROM systems WHERE code = %s", (system["code"],)).fetchone()
        if existing and existing.get("publication_source") == "ai_verified" and publication_source != "ai_verified":
            return
        if (
            existing
            and publication_source != "ai_verified"
            and system_quality_score(dict(existing)) > system_quality_score(system) + 4
        ):
            return
        search_blob = " ".join(
            [
                system.get("code", ""),
                system.get("title", ""),
                system.get("type", ""),
                _json(system.get("job_essentials", {})),
                _json(system.get("key_remote", {})),
                _json(system.get("mechanical_key", {})),
                _json(system.get("transponder", {})),
            ]
        )
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO systems
                  (code, title, system_type, job_essentials, quick_answer, key_remote, mechanical_key,
                   transponder, programming, making_key, technician_checklist, source_facts, decoders, lock_parts, troubleshooting,
                   warnings, source_coverage, search_vector, publication_source, status)
                VALUES
                  (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                   %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                   %s::jsonb, %s::jsonb, to_tsvector('english', unaccent(%s)), %s, 'approved')
                ON CONFLICT (code)
                DO UPDATE SET title = EXCLUDED.title,
                              system_type = EXCLUDED.system_type,
                              job_essentials = EXCLUDED.job_essentials,
                              quick_answer = EXCLUDED.quick_answer,
                              key_remote = EXCLUDED.key_remote,
                              mechanical_key = EXCLUDED.mechanical_key,
                              transponder = EXCLUDED.transponder,
                              programming = EXCLUDED.programming,
                              making_key = EXCLUDED.making_key,
                              technician_checklist = EXCLUDED.technician_checklist,
                              source_facts = EXCLUDED.source_facts,
                              decoders = EXCLUDED.decoders,
                              lock_parts = EXCLUDED.lock_parts,
                              troubleshooting = EXCLUDED.troubleshooting,
                              warnings = EXCLUDED.warnings,
                              source_coverage = EXCLUDED.source_coverage,
                              search_vector = EXCLUDED.search_vector,
                              publication_source = EXCLUDED.publication_source,
                              status = EXCLUDED.status
                """,
                (
                    system["code"],
                    system["title"],
                    system["type"],
                    _json(system.get("job_essentials", {})),
                    _json(system.get("quick_answer", [])),
                    _json(system.get("key_remote", {})),
                    _json(system.get("mechanical_key", {})),
                    _json(system.get("transponder", {})),
                    _json(system.get("programming", {})),
                    _json(system.get("making_key", {})),
                    _json(system.get("technician_checklist", {})),
                    _json(system.get("source_facts", {})),
                    _json(system.get("decoders", [])),
                    _json(system.get("lock_parts", [])),
                    _json(system.get("troubleshooting", [])),
                    _json(system.get("warnings", [])),
                    _json(system.get("source_coverage", [])),
                    search_blob,
                    publication_source,
                ),
            )
            conn.commit()

    def upsert_vehicle(self, vehicle: dict[str, Any]) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO vehicle_applications
                  (make, model, year_from, year_to, system_code, system_type, source_document, source_pages)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT ((lower(make)), (lower(model)), year_from, year_to, system_code)
                DO UPDATE SET system_type = EXCLUDED.system_type,
                              source_document = EXCLUDED.source_document,
                              source_pages = EXCLUDED.source_pages
                """,
                (
                    vehicle["make"],
                    vehicle["model"],
                    vehicle["year_from"],
                    vehicle["year_to"],
                    vehicle["system_code"],
                    vehicle.get("type"),
                    vehicle.get("source_document"),
                    vehicle.get("source_pages", []),
                ),
            )
            conn.commit()

    def replace_verified_vehicle_applications(
        self,
        system_code: str,
        applications: list[dict[str, Any]],
        source_document: str | None,
        source_pages: list[int],
        system_type: str | None = None,
    ) -> None:
        if not applications:
            return
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM vehicle_applications WHERE system_code = %s AND source_document = %s",
                (system_code, source_document),
            )
            conn.commit()
        for application in applications:
            self.upsert_vehicle(
                {
                    "make": application["make"],
                    "model": application["model"],
                    "year_from": application["year_from"],
                    "year_to": application["year_to"],
                    "system_code": system_code,
                    "type": system_type,
                    "source_document": source_document,
                    "source_pages": source_pages,
                }
            )
