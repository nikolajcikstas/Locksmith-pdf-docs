from __future__ import annotations

import json
import os
import re
import base64
import hashlib
import time
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageFilter, ImageOps

from locksmith_docs.parsing.canonical_models import CANONICAL_MODELS, canonicalize_model
SYSTEM_INSTRUCTIONS = """You complete, clean, and rewrite structured automotive locksmith reports created from OCR.
Preserve all factual identifiers exactly when they are plausible: years, make/model, part numbers, FCC IDs,
frequencies, keyways, code series, spacing/depth values, chip names, PIN notes, tools, and decoder names.
Fix OCR errors, broken words, awkward phrasing, and duplicate notes. Remove copyright/footer/watermark text.
Do not invent data that is not supported by the draft or source text. If a field is garbled and cannot be
confidently repaired, remove that field instead of publishing nonsense.
Use the full source excerpt to populate every supported section when evidence exists: vehicle_applications,
job essentials, remote
options, mechanical key and cut data, transponder, programming, making-key procedures, lock parts, warnings,
and troubleshooting. vehicle_applications is a list of objects with make, model, year_from and year_to for
every application explicitly listed for this system; it may contain low-confidence OCR hints. Correct damaged
OCR model/make text from the page image, remove ocr_confidence, and include every clearly listed application.
Rewrite narrative procedure notes into clear field instructions while preserving the
technical meaning and level of detail. Do not omit useful steps simply because the initial draft missed them.
Read and preserve all visible technical table cells that apply to the vehicle system, including style, card,
ILCO/keyway, MACS, start-cut/cut-to-cut values or n/a status, airbag/retainer notes, dealer/PIN/KESSY notes,
factory tool, programmer support statuses, cloning restrictions, decoder rows, and code availability.
When proximity is described as an optional equipment path, preserve that as key_remote.proximity_option and do
not mislabel the base system as a mandatory proximity key. Put dealer/PIN access limitations in
programming.pin_guidance and tool support cautions in programming.tool_guidance.
When the source lists remotes or fobs, key_remote.known_options must preserve the available model/year,
part/reference, FCC ID, frequency, button count, and notes per compatible option; do not leave FCC identifiers
only in a generic source-facts list. Never place a programmer or decoder model in the ILCO/keyway field.
The lock_parts section is inventory only: include a row only for explicit lock/cylinder/part availability,
supplier, or PIN-kit references. Never put key-making instructions, decoder procedures, or loose OCR text in it.
If an attached page contains an instructional visual that materially explains an operation (for example a
pin/wafer position map or a diagram relating door, trunk, ignition, valet, or glove-box positions), extract its
facts into an optional top-level procedure_diagrams array. Do not extract product photos, key/fob portraits,
decorative images, or text/table screenshots. Each diagram item must have title, placement
(mechanical_key, making_key, or programming), caption, and panels; each panel has label, columns, rows, and note.
The application redraws these facts as its own SVG and never publishes the source pixels. Study every attached
source-page image as well as the OCR excerpt: the page image is authoritative whenever OCR is damaged. Do not
return a shortened summary. Return every readable vehicle-specific technical fact supported by the source pages.
Return strict JSON only with the report schema plus optional procedure_diagrams."""

REVIEW_INSTRUCTIONS = """You are the final publication quality editor for an English automotive locksmith report.
The supplied JSON is incomplete or contains OCR corruption. Use the attached source-page images as the authority:
repair incorrect values and add every missing technical fact called out in detected_quality_problems. Do not
shorten the report. Preserve reliable identifiers, measurements and steps. Never return OCR fragments,
source header/footer labels, copyright text, pipe-delimited scan debris, or mixed-language garbage.
When a source visual maps lock cut positions, provide procedure_diagrams as structured facts for an original SVG,
including all visibly marked positions. Return strict JSON only with the same top-level schema and code."""

TECHNICAL_PATCH_INSTRUCTIONS = """You extract only missing automotive locksmith technical facts from source-page
images for an existing report. Return strict JSON with code and only fields that can be directly verified in the
attached pages. Do not rewrite narrative sections and do not guess. Allowed output fields are mechanical_key
(style, macs, start_cut, cut_to_cut, milling, air_bags, ignition_retainer, spacing, depths), programming
(pin_required, factory_tool, system_requirement, pin_guidance, tool_guidance, cloning, tools), transponder
(transponder_type, chip, reusable, test_key, cloner_tools), making_key (code_availability, methods), decoders,
lock_parts, and procedure_diagrams. spacing must be an object with positions 1 through 8; depths positions 1 through 4.
For cloner_tools, capture each visible column as one object with name, model and status; name is the visible
manufacturer heading. Do not separate a manufacturer from its model and do not return blank-name entries.
For lock_parts, return each visible row as a separate object preserving years, model, ignition_lock, door_lock,
trunk_lock, and pin_kit. For decoders, preserve each named decoder/reader and its exact reference.
For a lock-position map, return procedure_diagrams with columns ['1','2','3','4','5','6','7','8'] and rows as
arrays of nine cells: the lock label followed by eight values, using 'filled' only for visibly marked squares and
'' otherwise. Return no diagram if the exact squares are not readable."""

QUALITY_PATTERNS = (
    re.compile(r"[^\x00-\x7f]"),
    re.compile(r"\|"),
    re.compile(r"\b(?:AUTOSMART|MICHAEL\s+HYDE|CONTINUED\s*-|[A-Z]{2,6}-\d+\s+SECTION)\b", re.IGNORECASE),
    re.compile(r"\b(?:METH0D|L0CK|D0OR|SI5MHZ|DECBDER|REDDER|CYLIND\+)\b", re.IGNORECASE),
    re.compile(r"\b(?:THEXDBOT|DETERM[I1]NETHE|WCRKING|PROYESSION|T0OL)\b", re.IGNORECASE),
    re.compile(r"\b(?:ACJRA|SHE11|BE10W|ILC0|DTV|H[CO]NDA|TA[NH]GO)\b|:NFO\b", re.IGNORECASE),
    re.compile(r"\bFLlP\b"),
    re.compile(r"\b(?:NOCODESONANYLOCKS|USEA|DOORLOCK-TO|A=\s*TRACK|WW\s*2\s*TE)\b", re.IGNORECASE),
    re.compile(r"^[\s@_|=-]{1,5}[A-Za-z]"),
    re.compile(r"(?:\s>\s.*){2,}"),
)


def report_cleanup_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY")) and os.environ.get("REPORT_AI_CLEANUP", "1") != "0"


def report_api_key_fingerprint() -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return "not configured"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:10]


def require_report_ai_access() -> None:
    if not report_cleanup_enabled():
        raise RuntimeError(
            "AI report verification is not configured. Add OPENAI_API_KEY to the Docker .env file, "
            "restart the app, and run rebuild again."
        )
    api_key = os.environ["OPENAI_API_KEY"]
    request = Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=30):
            return
    except HTTPError as exc:
        detail = ""
        try:
            error_data = json.loads(exc.read().decode("utf-8"))
            detail = str((error_data.get("error") or {}).get("message") or "").strip()
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, AttributeError):
            detail = ""
        detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted key]", detail)[:240]
        suffix = f" API response: {detail}" if detail else ""
        raise RuntimeError(
            f"OpenAI verification startup check failed (HTTP {exc.code}). "
            f"Check the API key and project API access before rebuilding.{suffix}"
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise RuntimeError(
            "OpenAI verification startup check could not reach the API. "
            "Check network access and try rebuilding again."
        ) from exc


def clean_report_draft_with_ai(
    draft: dict[str, Any],
    source_text: str,
    source_images: list[Path] | None = None,
    model: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if not report_cleanup_enabled():
        return draft, False
    api_key = os.environ["OPENAI_API_KEY"]
    model = model or os.environ.get("OPENAI_REPORT_MODEL", os.environ.get("OPENAI_OCR_MODEL", "gpt-4o-mini"))
    draft = enrich_draft_with_targeted_ocr(draft, source_images or [])
    draft = merge_source_supported_facts(draft, source_text)
    cleaned = request_cleaned_report(
        api_key,
        model,
        SYSTEM_INSTRUCTIONS,
        {
            "draft_json": draft,
            "source_text_excerpt": source_text[:60000],
            "instruction": "Return a full technician report, not a synopsis. Recheck each visible table cell on the attached pages before returning JSON: applications, key type/style, code series, keyway, MACS, safety/retainer fields, remote rows, spacing/depth table, cutting-track note, chip/transponder/reusable/test-key fields, PIN and factory tool, every programmer/cloner status, code availability, decoders and each numbered method. The attached labeled images are enlarged working-detail crops from the source pages. Transcribe every visible chip identifier and every spacing/depth value. IMAGE: when a lock-position map is present, procedure_diagrams is mandatory. Add one item with placement='making_key', a panels array containing one panel labelled 'Lock positions', columns ['1','2','3','4','5','6','7','8'], and rows in the form ['Ignition','filled',...], ['Doors / trunk / hatch','filled',...], ['Glove box',...]; use 'filled' only where the source diagram has a marked square and '' elsewhere. This is rendered as an original SVG; do not include the source photograph. Fill missing structured sections only from supported source facts and retain detailed procedural guidance.",
        },
        source_images or [],
    )
    if not cleaned or cleaned.get("code") != draft.get("code"):
        return draft, False
    cleaned = merge_source_supported_facts(merge_reliable_extracted_facts(
        sanitize_structured_sections(remove_unpublishable_text(cleaned)),
        draft,
    ), source_text)
    cleaned = sanitize_structured_sections(cleaned)
    issues = report_quality_issues(cleaned) + report_completeness_issues(cleaned, source_text)
    if issues and os.environ.get("REPORT_AI_RETRY_ON_FAILURE", "0") == "1":
        try:
            patch = request_cleaned_report(
                api_key,
                model,
                TECHNICAL_PATCH_INSTRUCTIONS,
                {
                    "code": cleaned.get("code"),
                    "known_verified_facts": {
                        "vehicle_applications": cleaned.get("vehicle_applications"),
                        "key_remote": cleaned.get("key_remote"),
                        "mechanical_key": cleaned.get("mechanical_key"),
                        "transponder": cleaned.get("transponder"),
                        "programming": cleaned.get("programming"),
                    },
                    "source_text_excerpt": source_text[:12000],
                    "detected_quality_problems": issues[:16],
                    "instruction": "Return a compact patch only. Fill the missing fields called out in detected_quality_problems by reading the enlarged source-page images. Never copy source image pixels. The pin-position map crop is intentionally close: inspect every one of its eight columns. A map must list each of its eight marker cells explicitly for each visible row.",
                },
                source_images or [],
                technical_only=True,
            )
        except RuntimeError:
            patch = None
        if patch and (not patch.get("code") or patch.get("code") == draft.get("code")):
            reviewed = apply_technical_patch(cleaned, patch)
            cleaned = merge_source_supported_facts(merge_reliable_extracted_facts(
                sanitize_structured_sections(remove_unpublishable_text(reviewed)),
                draft,
            ), source_text)
            cleaned = sanitize_structured_sections(cleaned)
    return cleaned, True


def request_cleaned_report(
    api_key: str,
    model: str,
    system_instruction: str,
    user_payload: dict[str, Any],
    source_images: list[Path],
    technical_only: bool = False,
) -> dict[str, Any] | None:
    content: list[dict[str, str]] = [{"type": "input_text", "text": json.dumps(user_payload, ensure_ascii=False)}]
    for image_index, image_path in enumerate(source_images[:source_image_limit()]):
        image_data = technical_patch_data_urls(image_path, image_index) if technical_only else procedure_detail_data_urls(image_path, image_index)
        for label, data_url in image_data:
            content.append({"type": "input_text", "text": f"IMAGE: {label}"})
            content.append({"type": "input_image", "image_url": data_url, "detail": "high"})
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_output_tokens": int(os.environ.get("OPENAI_REPORT_MAX_OUTPUT_TOKENS", "12000")),
        "text": {"format": {"type": "json_object"}},
        "store": False,
    }
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    retry_count = max(0, min(3, int(os.environ.get("OPENAI_TRANSIENT_RETRIES", "2"))))
    data = None
    for attempt in range(retry_count + 1):
        try:
            with urlopen(
                request,
                timeout=max(120, int(os.environ.get("OPENAI_REPORT_TIMEOUT_SECONDS", "600"))),
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            detail = ""
            try:
                error_data = json.loads(exc.read().decode("utf-8"))
                detail = str((error_data.get("error") or {}).get("message") or "").strip()
            except (json.JSONDecodeError, OSError, UnicodeDecodeError, AttributeError):
                detail = ""
            if exc.code == 429 and attempt < retry_count:
                retry_after = exc.headers.get("Retry-After")
                delay_match = re.search(r"try again in\s+([\d.]+)s", detail, re.IGNORECASE)
                try:
                    delay = float(retry_after) if retry_after else float(delay_match.group(1)) if delay_match else 3.0
                except ValueError:
                    delay = 3.0
                time.sleep(min(30.0, max(1.0, delay + 0.5)))
                continue
            detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted key]", detail)[:300]
            suffix = f" API response: {detail}" if detail else ""
            raise RuntimeError(f"OpenAI report request failed (HTTP {exc.code}).{suffix}") from exc
        except (OSError, URLError, TimeoutError) as exc:
            if attempt < retry_count:
                time.sleep(min(30.0, 2.0 + attempt * 3.0))
                continue
            raise RuntimeError("OpenAI report request did not complete because the API could not be reached.") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("OpenAI returned a response envelope that could not be decoded as JSON.") from exc
    if data is None:
        raise RuntimeError("OpenAI report request did not produce a response.")
    text = extract_response_text(data)
    if not text:
        status = str(data.get("status") or "unknown")
        incomplete = data.get("incomplete_details") or {}
        reason = str(incomplete.get("reason") or "").strip()
        detail = f" Reason: {reason}." if reason else ""
        raise RuntimeError(f"OpenAI returned no report JSON (response status: {status}).{detail}")
    try:
        cleaned = json.loads(text)
    except json.JSONDecodeError as exc:
        status = str(data.get("status") or "unknown")
        raise RuntimeError(
            f"OpenAI returned incomplete or invalid report JSON (response status: {status}). "
            "No report was published; rerun this controlled batch after reviewing the configured output limit."
        ) from exc
    if not isinstance(cleaned, dict):
        raise RuntimeError("OpenAI report JSON was not an object; no report was published.")
    return cleaned


def procedure_detail_data_urls(image_path: Path, image_index: int = 0) -> list[tuple[str, str]]:
    try:
        image = Image.open(image_path).convert("RGB")
    except OSError:
        return []
    width, height = image.size
    crops: list[tuple[str, Image.Image]] = [("complete source page", image.copy())]
    if image_index == 0:
        crops.extend([
            ("vehicle application and specification header", image.crop((int(width * 0.04), int(height * 0.04), int(width * 0.92), int(height * 0.16)))),
            ("remote identifiers and VIN note", image.crop((int(width * 0.07), int(height * 0.14), int(width * 0.91), int(height * 0.31)))),
            ("mechanical spacing and depths table", image.crop((int(width * 0.04), int(height * 0.48), int(width * 0.72), int(height * 0.70)))),
            ("programmer and diagnostic tools table", image.crop((int(width * 0.04), int(height * 0.70), int(width * 0.92), int(height * 0.94)))),
        ])
    elif image_index == 1:
        # Separate crops keep text and the pin-position map large enough for
        # extraction instead of asking the model to read a compressed collage.
        crops.extend([
            ("chip information and cloner machine table", image.crop((int(width * 0.07), int(height * 0.04), int(width * 0.94), int(height * 0.34)))),
            ("codes decoders and numbered key-making methods", image.crop((int(width * 0.07), int(height * 0.35), int(width * 0.94), int(height * 0.55)))),
            ("lock-position map for original SVG reconstruction", image.crop((int(width * 0.18), int(height * 0.53), int(width * 0.72), int(height * 0.75)))),
            ("lock-parts inventory and PIN-kit table", image.crop((int(width * 0.07), int(height * 0.76), int(width * 0.94), int(height * 0.94)))),
        ])
    urls = []
    for label, crop in crops:
        crop.thumbnail((1800, 1200))
        buffer = BytesIO()
        crop.save(buffer, format="JPEG", quality=88, optimize=True)
        urls.append((label, "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")))
    return urls


def technical_patch_data_urls(image_path: Path, image_index: int = 0) -> list[tuple[str, str]]:
    """Use only tight enlarged technical regions for the low-cost missing-facts pass."""
    try:
        image = Image.open(image_path).convert("RGB")
    except OSError:
        return []
    width, height = image.size
    crops: list[tuple[str, Image.Image]] = []
    if image_index == 0:
        crops = [
            ("specification values: code series, style, card, ILT, MACS, start cut, cut-to-cut, air bags, ignition retainer", image.crop((int(width * 0.12), int(height * 0.05), int(width * 0.92), int(height * 0.15)))),
            ("mechanical spacing and depths values", image.crop((int(width * 0.04), int(height * 0.48), int(width * 0.72), int(height * 0.70)))),
            ("factory tool PIN and programmer support", image.crop((int(width * 0.04), int(height * 0.70), int(width * 0.92), int(height * 0.94)))),
        ]
    elif image_index == 1:
        crops = [
            ("chip reusable status and cloner support columns", image.crop((int(width * 0.07), int(height * 0.04), int(width * 0.94), int(height * 0.34)))),
            ("codes decoders and all numbered methods", image.crop((int(width * 0.07), int(height * 0.35), int(width * 0.94), int(height * 0.55)))),
            ("close pin-position map: transcribe every visible marked square by column", image.crop((int(width * 0.18), int(height * 0.53), int(width * 0.72), int(height * 0.75)))),
            ("lock-parts and PIN-kit rows", image.crop((int(width * 0.07), int(height * 0.76), int(width * 0.94), int(height * 0.94)))),
        ]
    urls: list[tuple[str, str]] = []
    for label, crop in crops:
        crop.thumbnail((2200, 1500))
        buffer = BytesIO()
        crop.save(buffer, format="JPEG", quality=92, optimize=True)
        urls.append((label, "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")))
    return urls


def source_image_limit() -> int:
    try:
        return max(1, min(12, int(os.environ.get("AI_REPORT_SOURCE_IMAGE_LIMIT", "12"))))
    except ValueError:
        return 12


def enrich_draft_with_targeted_ocr(draft: dict[str, Any], source_images: list[Path]) -> dict[str, Any]:
    """Recover compact technical-table facts that full-page OCR commonly misses."""
    if len(source_images) < 2:
        return draft
    try:
        import pytesseract
    except ImportError:
        return draft
    try:
        mechanical_page = Image.open(source_images[0]).convert("L")
        detail_page = Image.open(source_images[1]).convert("L")
    except OSError:
        return draft
    mechanical_full_text = _ocr_detail_region(mechanical_page, pytesseract)
    detail_full_text = _ocr_detail_region(detail_page, pytesseract)
    mw, mh = mechanical_page.size
    dw, dh = detail_page.size
    mechanical_keyref_text = _ocr_detail_region(
        mechanical_page.crop((int(mw * 0.02), int(mh * 0.10), int(mw * 0.98), int(mh * 0.42))),
        pytesseract,
    )
    mechanical_programming_text = _ocr_detail_region(
        mechanical_page.crop((int(mw * 0.04), int(mh * 0.70), int(mw * 0.94), int(mh * 0.94))),
        pytesseract,
    )
    detail_methods_text = _ocr_detail_region(
        detail_page.crop((int(dw * 0.04), int(dh * 0.34), int(dw * 0.96), int(dh * 0.58))),
        pytesseract,
    )
    detail_lockparts_text = _ocr_detail_region(
        detail_page.crop((int(dw * 0.04), int(dh * 0.72), int(dw * 0.96), int(dh * 0.96))),
        pytesseract,
    )
    width, height = detail_page.size
    chip_text = _ocr_detail_region(
        detail_page.crop((0, int(height * 0.04), width, int(height * 0.20))),
        pytesseract,
    )
    transponder = deepcopy(draft.get("transponder") if isinstance(draft.get("transponder"), dict) else {})
    chip = re.search(r"\bChip\s*:\s*(?P<chip>[A-Za-z0-9 ]{3,30}?)(?=\s+Reusable|\s*$)", chip_text, re.IGNORECASE)
    if chip:
        value = " ".join(chip.group("chip").split()).strip()
        if re.search(r"\b(?:Megamos|AES|Philips|Texas|Crypto|46|48)\b", value, re.IGNORECASE):
            transponder["chip"] = value
    key = re.search(r"\bTest\s*key\s*is\s*(?P<key>[A-Z0-9-]{2,12})\b", chip_text, re.IGNORECASE)
    if key and not transponder.get("test_key"):
        transponder["test_key"] = key.group("key").upper()
    cloner_tools = _extract_cloner_tools(detail_page, pytesseract)
    if cloner_tools and not transponder.get("cloner_tools"):
        transponder["cloner_tools"] = cloner_tools
    enriched = deepcopy(draft)
    if transponder:
        enriched["transponder"] = transponder
    key_options = _extract_key_reference_options(mechanical_keyref_text)
    if key_options:
        remote = deepcopy(enriched.get("key_remote") if isinstance(enriched.get("key_remote"), dict) else {})
        if not remote.get("known_options"):
            remote["known_options"] = key_options
        enriched["key_remote"] = remote
    decoder_rows = _extract_decoder_rows(mechanical_keyref_text)
    if decoder_rows:
        enriched["decoders"] = decoder_rows
    programming_patch = _extract_programming_patch(mechanical_programming_text)
    if programming_patch:
        programming = deepcopy(enriched.get("programming") if isinstance(enriched.get("programming"), dict) else {})
        programming.update({key: value for key, value in programming_patch.items() if value})
        enriched["programming"] = programming
    methods = _extract_numbered_methods(detail_methods_text)
    if methods:
        making_key = deepcopy(enriched.get("making_key") if isinstance(enriched.get("making_key"), dict) else {})
        making_key["methods"] = methods
        enriched["making_key"] = making_key
    lock_parts = _extract_lock_parts_rows(detail_lockparts_text)
    if lock_parts:
        enriched["lock_parts"] = lock_parts
    width, height = mechanical_page.size
    table_text = _ocr_detail_region(
        mechanical_page.crop((0, int(height * 0.48), width, int(height * 0.70))),
        pytesseract,
    )
    mechanical = deepcopy(enriched.get("mechanical_key") if isinstance(enriched.get("mechanical_key"), dict) else {})
    spec_cells = _extract_specification_cells(mechanical_page, pytesseract)
    spacing = _extract_ocr_spacing_table(table_text)
    depths = _extract_ocr_depth_table(table_text)
    fixed_spacing, fixed_depths = _extract_fixed_mechanical_cells(
        mechanical_page,
        pytesseract,
        cut_to_cut=spec_cells.get("cut_to_cut", ""),
    )
    if len(fixed_spacing) >= 8:
        spacing = fixed_spacing
    if len(fixed_depths) >= 4:
        depths = fixed_depths
    if len(spacing) >= 8:
        mechanical["spacing"] = spacing
    if len(depths) >= 4:
        mechanical["depths"] = depths
    mechanical.update({
        key: value
        for key, value in spec_cells.items()
        if value and not mechanical.get(key)
    })
    if mechanical:
        enriched["mechanical_key"] = mechanical
    enriched = merge_targeted_ocr_fields(enriched, mechanical_full_text, detail_full_text)
    return _extract_transponder_cells(enriched, detail_page, pytesseract)


def targeted_ocr_debug(source_images: list[Path]) -> dict[str, Any]:
    """Return local-only technical OCR results for administration diagnostics."""
    if len(source_images) < 2:
        return {"error": "At least two source pages are required."}
    try:
        import pytesseract
        mechanical_page = Image.open(source_images[0]).convert("L")
        detail_page = Image.open(source_images[1]).convert("L")
    except (ImportError, OSError) as exc:
        return {"error": str(exc)}
    spec_cells = _extract_specification_cells(mechanical_page, pytesseract)
    spacing, depths = _extract_fixed_mechanical_cells(
        mechanical_page,
        pytesseract,
        cut_to_cut=spec_cells.get("cut_to_cut", ""),
    )
    sample = _extract_transponder_cells({"transponder": {}}, detail_page, pytesseract)
    return {
        "source_images": [str(path) for path in source_images[:2]],
        "specification_cells": spec_cells,
        "fixed_spacing": spacing,
        "fixed_depths": depths,
        "transponder_cells": sample.get("transponder", {}),
        "spacing_region_text": _ocr_detail_region(
            mechanical_page.crop((0, int(mechanical_page.height * 0.46), mechanical_page.width, int(mechanical_page.height * 0.71))),
            pytesseract,
        )[:1000],
    }


def _ocr_detail_region(image: Image.Image, pytesseract: Any) -> str:
    texts = []
    images = [
        ImageOps.autocontrast(image),
        ImageOps.autocontrast(image).point(lambda value: 0 if value < 185 else 255),
    ]
    for source in images:
        resized = source.resize((source.width * 3, source.height * 3))
        for mode in ("--oem 1 --psm 6", "--oem 1 --psm 11"):
            texts.append(pytesseract.image_to_string(resized, lang="eng", config=mode))
    return "\n".join(texts)


def _extract_ocr_spacing_table(text: str) -> dict[str, str]:
    segment = re.split(r"\bDEPTHS?\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    decimals = re.findall(r"(?<!\d)[.]?\s*(\d{3})(?!\d)", segment)
    values = ["." + value for value in decimals if value not in {"811"}]
    for start in range(max(1, len(values) - 7)):
        candidate = values[start:start + 8]
        if len(candidate) == 8 and all(float(candidate[index]) > float(candidate[index + 1]) for index in range(7)):
            return {str(index + 1): value for index, value in enumerate(candidate)}
    return {}


def _extract_ocr_depth_table(text: str) -> dict[str, str]:
    depths: dict[str, str] = {}
    for match in re.finditer(r"\b([1-4])\s*[=:]\s*[.]?\s*(\d{3})\b", text):
        depths[match.group(1)] = "." + match.group(2)
    return depths


def _extract_fixed_mechanical_cells(
    image: Image.Image,
    pytesseract: Any,
    cut_to_cut: str = "",
) -> tuple[dict[str, str], dict[str, str]]:
    """Read spacing/depth cells from the repeated AutoSmart technical sheet layout."""
    width, height = image.size
    spacing: dict[str, str] = {}
    spacing_row = image.crop((int(width * 0.065), int(height * 0.565), int(width * 0.555), int(height * 0.626)))
    spacing_values = _ocr_decimal_values(spacing_row, pytesseract)
    if len(spacing_values) >= 8:
        spacing = {str(index + 1): value for index, value in enumerate(spacing_values[:8])}
    if not _is_descending_cut_table(spacing, 8):
        spacing = {}
        spacing_left = 0.065
        spacing_right = 0.555
        cell_width = (spacing_right - spacing_left) / 8
        for position in range(8):
            crop = image.crop(
                (
                    int(width * (spacing_left + position * cell_width)),
                    int(height * 0.555),
                    int(width * (spacing_left + (position + 1) * cell_width)),
                    int(height * 0.633),
                )
            )
            value = _ocr_decimal_cell(crop, pytesseract)
            if value:
                spacing[str(position + 1)] = value
    if not _is_descending_cut_table(spacing, 8):
        spacing_region = image.crop((int(width * 0.035), int(height * 0.515), int(width * 0.585), int(height * 0.665)))
        spacing = _derive_spacing_from_anchors(
            _ocr_decimal_texts(spacing_row, pytesseract, scale=6)
            + [_ocr_detail_region(spacing_region, pytesseract)],
            cut_to_cut,
        )
    depths: dict[str, str] = {}
    depth_block = image.crop((int(width * 0.600), int(height * 0.540), int(width * 0.720), int(height * 0.660)))
    depth_values = _ocr_decimal_values(depth_block, pytesseract)
    if len(depth_values) >= 4:
        depths = {str(index + 1): value for index, value in enumerate(depth_values[:4])}
    if not _is_descending_cut_table(depths, 4):
        depths = {}
        depth_rows = ((0.557, 0.581), (0.581, 0.604), (0.604, 0.628), (0.628, 0.652))
        for position, (top, bottom) in enumerate(depth_rows, start=1):
            crop = image.crop((int(width * 0.612), int(height * top), int(width * 0.704), int(height * bottom)))
            value = _ocr_decimal_cell(crop, pytesseract)
            if value:
                depths[str(position)] = value
    if not _is_descending_cut_table(spacing, 8):
        spacing = {}
    if not _is_descending_cut_table(depths, 4):
        depths = {}
    return spacing, depths


def _derive_spacing_from_anchors(texts: list[str], cut_to_cut: str) -> dict[str, str]:
    """Rebuild an evenly spaced table when OCR sees anchors but misses cells."""
    cut_match = re.search(r"[.]?\s*(\d{3})\b", cut_to_cut or "")
    if not cut_match:
        return {}
    step = int(cut_match.group(1)) / 1000
    if step <= 0 or step > 0.250:
        return {}
    anchors: set[float] = set()
    combined = "\n".join(texts)
    for match in re.finditer(r"(?<!\d)[.]?\s*(\d{3})(?!\d)", combined):
        value = int(match.group(1)) / 1000
        if 0.150 <= value <= 1.200 and abs(value - step) > 0.004:
            anchors.add(round(value, 3))
    for first in sorted(anchors, reverse=True):
        row = [round(first - step * index, 3) for index in range(8)]
        if min(row) <= 0:
            continue
        matched = sum(
            1
            for value in row
            if any(abs(anchor - value) <= 0.004 for anchor in anchors)
        )
        if matched >= 3 and all(row[index] > row[index + 1] for index in range(7)):
            return {str(index + 1): _format_decimal_value(value) for index, value in enumerate(row)}
    return {}


def _format_decimal_value(value: float) -> str:
    return f".{int(round(value * 1000)):03d}"


def _extract_specification_cells(image: Image.Image, pytesseract: Any) -> dict[str, str]:
    """Read fixed header cells used throughout the reference-book page layout."""
    width, height = image.size
    # Header values sit on the first technical row beneath the application title.
    # Crop individual cells so neighboring labels and image noise cannot confuse OCR.
    cell_regions = {
        "code_series": (0.148, 0.092, 0.283, 0.143),
        "card": (0.369, 0.092, 0.436, 0.143),
        "itl": (0.436, 0.092, 0.496, 0.143),
        "macs": (0.490, 0.086, 0.580, 0.150),
        "start_cut": (0.550, 0.086, 0.650, 0.150),
        "cut_to_cut": (0.630, 0.086, 0.733, 0.150),
        "air_bags": (0.722, 0.092, 0.805, 0.143),
        "ignition_retainer": (0.805, 0.092, 0.918, 0.143),
    }
    extracted: dict[str, str] = {}
    for key, (left, top, right, bottom) in cell_regions.items():
        crop = image.crop((int(width * left), int(height * top), int(width * right), int(height * bottom)))
        text = _ocr_detail_region(crop, pytesseract)
        compact = " ".join(text.split()).strip()
        if key == "code_series":
            match = re.search(r"\b([A-Z]{1,3}\s*\d+\s*[-–]\s*\d+)\b", compact, re.IGNORECASE)
            if match:
                extracted[key] = re.sub(r"\s+", " ", match.group(1).upper()).replace(" - ", "-")
        elif key == "card":
            match = re.search(r"\b([A-Z]{1,3}\d{2,5})\b", compact, re.IGNORECASE)
            if match:
                extracted[key] = match.group(1).upper()
        elif key == "itl":
            match = re.search(r"\b(\d{1,4})\b", compact)
            if match:
                extracted[key] = match.group(1)
        elif key == "macs":
            numeric = _ocr_numeric_cell(crop, pytesseract)
            match = re.search(r"\b([1-9]|1[0-2])\b", f"{compact} {numeric}")
            if match:
                extracted[key] = match.group(1)
        elif key in {"start_cut", "cut_to_cut"}:
            match = re.search(r"[.]?\s*(\d{3})\b", compact)
            if match:
                extracted[key] = "." + match.group(1)
            elif re.search(r"\bn/?a\b", compact, re.IGNORECASE):
                extracted[key] = "n/a"
        elif key == "air_bags":
            match = re.search(r"\b(Yes|No|N/?A|(?:19|20)?\d{2}\s*&?\s*up)\b", compact, re.IGNORECASE)
            if match:
                extracted[key] = match.group(1)
        else:
            match = re.search(r"\b(None|Spring|Clip|Pin|Ring|N/?A)\b", compact, re.IGNORECASE)
            if match:
                extracted[key] = match.group(1)
    row_text = _ocr_detail_region(
        image.crop((int(width * 0.130), int(height * 0.110), int(width * 0.930), int(height * 0.250))),
        pytesseract,
    )
    row_compact = " ".join(row_text.split())
    row_match = re.search(
        r"\b(?P<card>[A-Z]{1,4}\d{2,5})\s+(?P<itl>\d{1,4})\s+"
        r"(?P<macs>[1-9]|1[0-2])\s+(?P<start>\d{3})\s+(?P<cut>\d{3})\s+"
        r"(?P<air>Yes|No|N/?A)\s+(?P<retainer>None|Spring|Clip|Pin|Ring|N/?A)\b",
        row_compact,
        re.IGNORECASE,
    )
    if row_match:
        extracted.setdefault("card", row_match.group("card").upper())
        extracted.setdefault("itl", row_match.group("itl"))
        extracted.setdefault("macs", row_match.group("macs"))
        extracted.setdefault("start_cut", "." + row_match.group("start"))
        extracted.setdefault("cut_to_cut", "." + row_match.group("cut"))
        extracted.setdefault("air_bags", row_match.group("air").title())
        extracted.setdefault("ignition_retainer", row_match.group("retainer").title())
    return extracted


def _ocr_numeric_cell(image: Image.Image, pytesseract: Any) -> str:
    """Read a compact numeric field while excluding neighboring table cells."""
    return " ".join(
        pytesseract.image_to_string(
            variant,
            lang="eng",
            config=f"--oem 1 --psm {psm} -c tessedit_char_whitelist=0123456789",
        )
        for variant in _ocr_variants(image, scale=8)
        for psm in (10, 7, 13)
    )


def _ocr_decimal_cell(image: Image.Image, pytesseract: Any) -> str:
    text = " ".join(_ocr_decimal_texts(image, pytesseract, scale=8))
    match = re.search(r"[.]?\s*(\d{3})\b", text)
    return "." + match.group(1) if match else ""


def _ocr_decimal_values(image: Image.Image, pytesseract: Any) -> list[str]:
    values: list[str] = []
    for text in _ocr_decimal_texts(image, pytesseract, scale=6):
        values.extend("." + match.group(1) for match in re.finditer(r"[.]?\s*(\d{3})\b", text))
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _ocr_decimal_texts(image: Image.Image, pytesseract: Any, scale: int) -> list[str]:
    return [
        pytesseract.image_to_string(
            variant,
            lang="eng",
            config=f"--oem 1 --psm {psm} -c tessedit_char_whitelist=.0123456789",
        )
        for variant in _ocr_variants(image, scale=scale)
        for psm in (6, 7, 11, 13)
    ]


def _ocr_variants(image: Image.Image, scale: int) -> list[Image.Image]:
    base = ImageOps.autocontrast(image).resize((image.width * scale, image.height * scale))
    sharp = ImageOps.autocontrast(image).filter(ImageFilter.SHARPEN).resize((image.width * scale, image.height * scale))
    threshold = base.point(lambda value: 0 if value < 190 else 255)
    return [base, sharp, threshold]


def _extract_transponder_cells(draft: dict[str, Any], image: Image.Image, pytesseract: Any) -> dict[str, Any]:
    enriched = deepcopy(draft)
    width, height = image.size
    header_text = _ocr_detail_region(
        image.crop((int(width * 0.08), int(height * 0.055), int(width * 0.94), int(height * 0.145))),
        pytesseract,
    )
    transponder = enriched.setdefault("transponder", {})
    if isinstance(transponder, dict):
        chip = re.search(r"\bPhilips\s*46\b", header_text, re.IGNORECASE)
        if chip:
            transponder["chip"] = "Philips 46"
        reusable = re.search(r"\bReusable\s*[:|]?\s*(No|Yes)(?:\s*\(([^)]+)\))?", header_text, re.IGNORECASE)
        if reusable:
            value = reusable.group(1).title()
            if reusable.group(2):
                value += f" ({' '.join(reusable.group(2).split())})"
            transponder["reusable"] = value
        if not transponder.get("cloner_tools"):
            cloner_tools = _extract_cloner_tools(image, pytesseract)
            if cloner_tools:
                transponder["cloner_tools"] = cloner_tools
    return enriched


def _extract_cloner_tools(image: Image.Image, pytesseract: Any) -> list[dict[str, str]]:
    """Read the common cloner-machine table as structured rows."""
    width, height = image.size
    text = _ocr_detail_region(
        image.crop((int(width * 0.03), int(height * 0.02), int(width * 0.98), int(height * 0.36))),
        pytesseract,
    )
    upper = text.upper()
    if "CLONER" not in upper or not any(token in upper for token in ("RW4", "CN900", "TANGO", "MINI TOOL", "MIRACLONE")):
        return []
    note = ""
    if re.search(r"PHILIPS\s+CRYPTO\s+UPDATE", upper):
        note = "Yes; requires Philips Crypto update where noted"
    elif re.search(r"TEXAS\s+CRYPTO\s+UPDATE", upper):
        note = "Yes; requires crypto update where noted"
    else:
        note = "Listed as supported in the source cloner table"
    candidates = [
        ("Diag Box", "Clone Wizard", (r"DIAG\s*BOX", r"WIZARD")),
        ("China", "CN900", (r"CN\s*900|N900",)),
        ("ILCO / Silca", "RW4", (r"ILCO\s*/\s*SILCA", r"\bRW\s*4\b|RW4")),
        ("JMA USA", "TRS-5000 / EVO", (r"TRS\s*[-]?\s*5?000\s*/?\s*EVO|TRS",)),
        ("Keyline", "884 Ultegra", (r"884\s+ULTEGRA|ULTEGRA",)),
        ("L.D.", "Miraclone", (r"MIRACLONE",)),
        ("Scorpio", "Tango", (r"TANGO",)),
        ("VVDI", "Mini Tool", (r"VVDI|MINI\s+TOOL",)),
    ]
    rows: list[dict[str, str]] = []
    for name, model, patterns in candidates:
        if any(re.search(pattern, upper) for pattern in patterns):
            rows.append({"name": name, "model": model, "status": note})
    return rows


def _extract_decoder_rows(text: str) -> list[dict[str, str]]:
    source = " ".join(text.split())
    direct_patterns = [
        ("Determinator", r"Determinator\s*:\s*([A-Z0-9 ]{2,16}?)(?=\s+Lishi\s*:)", True),
        ("Lishi", r"Lishi\s*:\s*([A-Z0-9]{2,16}?)(?=\s+Accu\s*Reader\s*:|\s+AccuReader\s*:)", True),
        ("AccuReader", r"Accu\s*Reader\s*:\s*([A-Z0-9 ]{2,16}?)(?=\s+[_| -]*\s*EEZ\s+Reader\s*:)", True),
        ("EEZ Reader", r"EEZ\s+Reader\s*:\s*([A-Z0-9]{2,16}?)(?=\s+Cobra\s*:)", True),
        ("Cobra", r"Cobra\s*:\s*([A-Z0-9]{2,16})", True),
    ]
    direct_rows = []
    for tool, pattern, _ in direct_patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            reference = _clean_decoder_reference(match.group(1))
            if _is_clean_decoder_reference(reference):
                direct_rows.append({"tool": tool, "reference": reference})
    if len(direct_rows) >= 3:
        return direct_rows
    labels = ("Determinator", "Lishi", "AccuReader", "EEZ Reader", "Cobra")
    if not any(f"{label}:" in source for label in labels):
        return []
    rows: list[dict[str, str]] = []
    for index, label in enumerate(labels):
        next_labels = labels[index + 1 :]
        stop = "|".join(re.escape(f"{next_label}:") for next_label in next_labels) or r"$"
        match = re.search(rf"{re.escape(label)}\s*:\s*(.+?)(?=\s+(?:{stop})|$)", source, re.IGNORECASE)
        if not match:
            continue
        reference = " ".join(match.group(1).split()).strip(" .,:;|")
        reference = re.sub(r"\b(?:There|There\s+are|Programmer|Diagnostic)\b.*$", "", reference, flags=re.IGNORECASE).strip()
        if not reference:
            continue
        reference = _clean_decoder_reference(reference)
        if _is_clean_decoder_reference(reference):
            rows.append({"tool": label, "reference": reference})
    return rows


def _clean_decoder_reference(value: str) -> str:
    cleaned = " ".join(value.split()).strip(" .,:;|_->").upper()
    cleaned = re.sub(r"\b(CHR|CHRY)(\d)\b", r"\1 \2", cleaned)
    cleaned = re.sub(r"\b((?:KDC|Y|CY|HU|TOY|VWI|CHR|CHRY)[A-Z0-9 ]*?\d[A-Z0-9]*)(?:\s+A)+$", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _is_clean_decoder_reference(value: str) -> bool:
    cleaned = str(value or "").strip().upper()
    if not cleaned or len(cleaned) > 20:
        return False
    if cleaned in {"N/A", "NA", "NONE", "NO", "-", "--", "NIL", "NULL"}:
        return False
    if re.search(r"[|:>]", cleaned):
        return False
    return not re.search(
        r"\b(?:DETERMINATOR|LISHI|ACCU\s*READER|ACCUREADER|EEZ\s*READER|EEZREADER|COBRA|PROGRAMMER|FACTORY)\b",
        cleaned,
    )


def _extract_programming_patch(text: str) -> dict[str, Any]:
    source = " ".join(text.split())
    patch: dict[str, Any] = {}
    if re.search(r"\bFactory\s+Tool\b", source, re.IGNORECASE):
        tools = []
        if re.search(r"\bMicro\s*Pod\b|\bMicroPod\b", source, re.IGNORECASE):
            tools.append("MicroPod")
        if re.search(r"\bwi\s*TECH\b|\bwilTECH\b|\bwitech\b", source, re.IGNORECASE):
            tools.append("wiTECH")
        if tools:
            patch["factory_tool"] = " / ".join(tools)
    if re.search(r"\bPIN\s*CODE\b|\bPIN\b.{0,16}\bREQUIRED\b", source, re.IGNORECASE):
        patch["pin_required"] = "Yes"
    if re.search(r"\b4\s*[- ]?\s*digit\b", source, re.IGNORECASE):
        patch["pin_guidance"] = "4-digit PIN required."
    return patch


def _extract_numbered_methods(text: str) -> list[str]:
    source = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    canonical: list[str] = []
    upper = source.upper()
    if "LOCK DECODER/READER" in upper or "LOCK DECODER" in upper:
        canonical.append("Use a Lock Decoder/Reader tool to decode the door lock.")
    if "TRYOUT" in upper and ("TO-93" in upper or "T0-93" in upper or "SET 97" in upper):
        canonical.append("Use tryout keys, Baxter Systems Tryout keys set 97 All Locks, or Aero Lock tryout set TO-93.")
    if re.search(r"Remove\s+a\s+door\s+or\s+trunk\s+cylinder", source, re.IGNORECASE):
        canonical.append("Remove a door or trunk cylinder and decode it.")
    if len(canonical) >= 2:
        return canonical
    methods: list[str] = []
    for match in re.finditer(r"Method\s*#?\s*([1-9])\s+(.+?)(?=\n\s*Method\s*#?\s*[1-9]\s+|\Z)", source, re.IGNORECASE | re.DOTALL):
        detail = " ".join(match.group(2).split()).strip(" .")
        if detail and len(detail) > 12 and len(detail) < 180 and "Method #" not in detail:
            methods.append(detail + ".")
    return list(dict.fromkeys(methods))


def _extract_lock_parts_rows(text: str) -> list[dict[str, str]]:
    source = " ".join(text.split())
    rows: list[dict[str, str]] = []
    if re.search(r"2011\s*[-–]\s*12", source) and re.search(r"Locks\s+not\s+available\s+from\s+Strattec", source, re.IGNORECASE):
        pin = _first_match(source, r"\b(703927)\b")
        rows.append({
            "years": "2011-12",
            "models": "Routan" if "Routan" in source else "",
            "ignition_lock": "Locks not available from Strattec",
            "door_lock": "Locks not available from Strattec",
            "trunk_lock": "Locks not available from Strattec",
            "pin_kit": pin,
        })
    if re.search(r"2009\s*[-–]\s*10", source) and re.search(r"Fobik\s+ignition", source, re.IGNORECASE):
        rows.append({
            "years": "2009-10",
            "models": "Routan" if "Routan" in source else "",
            "ignition_lock": "Fobik ignition",
            "door_lock": _first_match(source, r"\b(704275)\b"),
            "trunk_lock": _first_match(source, r"\b(706371)\s*Liftgate\b") + " Liftgate" if _first_match(source, r"\b(706371)\s*Liftgate\b") else "",
            "pin_kit": _first_match(source, r"\b(703927)\b"),
        })
    return [row for row in rows if any(value for value in row.values())]


def _extract_key_reference_options(text: str) -> list[dict[str, str]]:
    source = " ".join(text.split())
    if not re.search(r"\bFOBIK\s+Remote\b", source, re.IGNORECASE):
        return []
    normalized = source.upper().replace("I", "1").replace("Z", "2").replace("S", "5")
    refs = []
    for canonical, pattern in (
        ("DGE-POD-KEY", r"\bDGE\s*[- ]\s*POD\s*[- ]\s*KEY\b"),
        ("BY170-PT", r"\bBY170\s*[- ]\s*PT\b"),
        ("Y170-PT", r"\bY170\s*[- ]\s*PT\b"),
        ("BY166-PHT", r"\bBY166\s*[- ]\s*PHT\b"),
        ("TP12CHR15P1", r"\bTP12CHR15P1\b"),
        ("5909874", r"\b5909874\b"),
    ):
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            refs.append(canonical)
    if not refs:
        return []
    return [{
        "models": "Routan" if "Routan" in source else "",
        "part": " / ".join(dict.fromkeys(refs)),
        "notes": "FOBIK remote; verify the correct part number by VIN and trim level with the dealer.",
    }]


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1) if match else ""


def merge_targeted_ocr_fields(draft: dict[str, Any], mechanical_text: str, detail_text: str) -> dict[str, Any]:
    """Carry legible table/header values from enlarged OCR into the structured draft."""
    enriched = deepcopy(draft)
    mechanical = enriched.setdefault("mechanical_key", {})
    if isinstance(mechanical, dict):
        code_series = re.search(
            r"\bCode\s*Series\s*[:|]?\s*([A-Z]{1,3}\s*\d+\s*[-–]\s*\d+)\b",
            mechanical_text,
            re.IGNORECASE,
        )
        if code_series and not mechanical.get("code_series"):
            mechanical["code_series"] = re.sub(r"\s+", " ", code_series.group(1).upper()).replace(" - ", "-")
        for field, pattern in (
            ("macs", r"\bMACS\s*[:|]?\s*(\d{1,2})\b"),
            ("air_bags", r"\bAir\s*Bags?\s*[:|]?\s*(Yes|No|N/?A)\b"),
            ("ignition_retainer", r"\bIgn(?:ition)?\s*Retainer\s*[:|]?\s*([A-Za-z][A-Za-z /-]{1,24})"),
        ):
            match = re.search(pattern, mechanical_text, re.IGNORECASE)
            if match and not mechanical.get(field):
                mechanical[field] = " ".join(match.group(1).split()).strip()
        if not mechanical.get("style") and re.search(r"\bHigh\s+Sec(?:urity)?\b", mechanical_text, re.IGNORECASE):
            mechanical["style"] = "High Security"
        if not mechanical.get("milling") and re.search(
            r"\bINTERNAL.{0,20}\bMILLING\b", mechanical_text, re.IGNORECASE | re.DOTALL
        ):
            mechanical["milling"] = "Internal milling"
        for field, label in (("start_cut", "StartCut"), ("cut_to_cut", "CutToCut")):
            if field not in mechanical:
                match = re.search(rf"\b{label}\s*[:|]?\s*(n/?a|\.\s*\d{{3}})", mechanical_text, re.IGNORECASE)
                if match:
                    mechanical[field] = match.group(1).replace(" ", "")
    programming = enriched.setdefault("programming", {})
    if isinstance(programming, dict) and not programming.get("factory_tool"):
        match = re.search(
            r"\bFactory\s+Tool\s*[:|]?\s*(.*?)(?=\bPIN\b|\bTCODE\b|\bMVP\b|$)",
            detail_text,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            value = " ".join(match.group(1).split()).strip(" -|,;")
            if 3 <= len(value) <= 80:
                programming["factory_tool"] = value
    return enriched


def report_quality_issues(value: Any, path: str = "report") -> list[str]:
    issues: list[str] = []
    if isinstance(value, str):
        if path.startswith("report.transponder.cloner_tools") and value.strip().casefold() == "tango":
            return issues
        if path.startswith("report.lock_parts") and re.search(
            r"\b(?:method|decode|decoder|reader|determine|working key|disassemble|no codes)\b",
            value,
            re.IGNORECASE,
        ):
            issues.append(f"{path}: procedure text is not lock-parts inventory")
            return issues
        for pattern in QUALITY_PATTERNS:
            if pattern.search(value):
                issues.append(f"{path}: {value[:120]}")
                break
        return issues
    if isinstance(value, dict):
        for key, item in value.items():
            issues.extend(report_quality_issues(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            issues.extend(report_quality_issues(item, f"{path}[{index}]"))
    return issues


def remove_unpublishable_text(value: Any) -> Any:
    if isinstance(value, str):
        return "" if report_quality_issues(value) else value
    if isinstance(value, list):
        values = [remove_unpublishable_text(item) for item in value]
        return [item for item in values if item not in ("", None, [], {})]
    if isinstance(value, dict):
        values = {key: remove_unpublishable_text(item) for key, item in value.items()}
        return {key: item for key, item in values.items() if key == "code" or item not in ("", None, [], {})}
    return value


def sanitize_structured_sections(draft: dict[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(draft)
    for name in ("job_essentials", "key_remote", "mechanical_key", "transponder", "programming", "making_key", "technician_checklist", "source_facts"):
        if name in cleaned and not isinstance(cleaned[name], dict):
            cleaned.pop(name, None)
    for name in ("vehicle_applications", "quick_answer", "decoders", "lock_parts", "troubleshooting", "warnings", "source_coverage"):
        if name in cleaned and not isinstance(cleaned[name], list):
            cleaned.pop(name, None)
    if isinstance(cleaned.get("vehicle_applications"), list):
        cleaned["vehicle_applications"] = sanitize_vehicle_applications(cleaned["vehicle_applications"])
    for name in ("decoders", "lock_parts", "troubleshooting"):
        if isinstance(cleaned.get(name), list):
            cleaned[name] = [item for item in cleaned[name] if isinstance(item, dict)]
    for name in ("quick_answer", "warnings", "source_coverage"):
        if isinstance(cleaned.get(name), list):
            cleaned[name] = [item for item in cleaned[name] if isinstance(item, str)]
    key_remote = cleaned.get("key_remote")
    if isinstance(key_remote, dict):
        if "known_options" in key_remote and not isinstance(key_remote["known_options"], list):
            key_remote.pop("known_options", None)
        elif isinstance(key_remote.get("known_options"), list):
            key_remote["known_options"] = [item for item in key_remote["known_options"] if isinstance(item, dict)]
    mechanical = cleaned.get("mechanical_key")
    if isinstance(mechanical, dict):
        for name in ("spacing", "depths", "cutting_setup"):
            if name in mechanical and not isinstance(mechanical[name], dict):
                mechanical.pop(name, None)
        if "cut_position_map" in mechanical and not isinstance(mechanical["cut_position_map"], list):
            mechanical.pop("cut_position_map", None)
    programming = cleaned.get("programming")
    if isinstance(programming, dict):
        if "tools" in programming and not isinstance(programming["tools"], list):
            programming.pop("tools", None)
        elif isinstance(programming.get("tools"), list):
            programming["tools"] = [item for item in programming["tools"] if isinstance(item, dict)]
    making_key = cleaned.get("making_key")
    if isinstance(making_key, dict):
        for name in ("methods", "field_workflow", "field_notes", "service_notes"):
            if name in making_key and not isinstance(making_key[name], list):
                making_key.pop(name, None)
        for name in ("methods", "field_notes", "service_notes"):
            if isinstance(making_key.get(name), list):
                making_key[name] = [item for item in making_key[name] if isinstance(item, str)]
        if isinstance(making_key.get("field_workflow"), list):
            making_key["field_workflow"] = [item for item in making_key["field_workflow"] if isinstance(item, dict)]
    transponder = cleaned.get("transponder")
    if isinstance(transponder, dict) and isinstance(transponder.get("cloner_tools"), list):
        transponder["cloner_tools"] = [
            item
            for item in transponder["cloner_tools"]
            if isinstance(item, dict)
            and str(item.get("name") or item.get("model") or "").strip()
            and str(item.get("status") or "").strip()
        ]
    diagrams = cleaned.get("procedure_diagrams")
    if isinstance(diagrams, list):
        normalized_diagrams = []
        for diagram in diagrams:
            if not isinstance(diagram, dict):
                continue
            normalized = deepcopy(diagram)
            panels = []
            raw_panels = normalized.get("panels")
            for panel in raw_panels if isinstance(raw_panels, list) else []:
                if not isinstance(panel, dict):
                    continue
                panel = deepcopy(panel)
                raw_columns = panel.get("columns")
                columns = [
                    str(value).strip()
                    for value in raw_columns if str(value).strip()
                ] if isinstance(raw_columns, list) else []
                if not columns and isinstance(raw_columns, str):
                    columns = re.findall(r"\b[1-8]\b", raw_columns)
                raw_rows = panel.get("rows")
                rows = normalize_diagram_rows(raw_rows, columns)
                panel["columns"] = columns
                panel["rows"] = rows
                panels.append(panel)
            normalized["panels"] = panels
            if is_informative_diagram(normalized):
                normalized_diagrams.append(normalized)
        cleaned["procedure_diagrams"] = normalized_diagrams
        _sync_mechanical_tables_from_diagrams(cleaned, normalized_diagrams)
    elif diagrams is not None:
        cleaned.pop("procedure_diagrams", None)
    lock_parts = cleaned.get("lock_parts")
    if isinstance(lock_parts, list):
        accepted = []
        for record in lock_parts:
            if not isinstance(record, dict):
                continue
            text = " ".join(str(value) for value in record.values()).lower()
            has_inventory_reference = any(
                token in text for token in ("strattec", "asp", "pin kit", "part", "available", "not available")
            ) or bool(re.search(r"\b70\d{4}\b", text))
            has_procedure_noise = any(
                token in text for token in ("method", "decoder", "reader", "determine", "working key", "disassemble")
            )
            if has_inventory_reference and not has_procedure_noise:
                accepted.append(record)
        if accepted:
            cleaned["lock_parts"] = accepted
        else:
            cleaned.pop("lock_parts", None)
    return cleaned


def sanitize_vehicle_applications(items: list[Any]) -> list[dict[str, Any]]:
    applications: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        make = re.sub(r"\s+", " ", str(item.get("make") or "")).strip()
        model = re.sub(r"\s+", " ", str(item.get("model") or "")).strip()
        try:
            year_from = int(item.get("year_from"))
            year_to = int(item.get("year_to"))
        except (TypeError, ValueError):
            continue
        if not make or not model or not (1900 <= year_from <= year_to <= 2035):
            continue
        if make in CANONICAL_MODELS:
            canonical_model, score = canonicalize_model(make, model)
            if score >= 0.78:
                model = canonical_model
        if report_quality_issues(make, "report.vehicle_applications.make") or report_quality_issues(model, "report.vehicle_applications.model"):
            continue
        key = (make.casefold(), model.casefold(), year_from, year_to)
        if key in seen:
            continue
        seen.add(key)
        applications.append({"make": make, "model": model, "year_from": year_from, "year_to": year_to})
    return applications


def normalize_diagram_rows(raw_rows: Any, columns: list[str]) -> list[list[str]]:
    """Accept strict arrays and recover the common string-row model response."""
    if not isinstance(raw_rows, list):
        return []
    normalized: list[list[str]] = []
    for raw_row in raw_rows:
        if isinstance(raw_row, list):
            values = [str(value).strip() for value in raw_row]
        elif isinstance(raw_row, str):
            text = " ".join(raw_row.split()).strip()
            match = re.match(
                r"(?P<label>Ignition|Doors?\s*/?\s*trunk\s*/?\s*hatch|Glove\s+box)\s*(?P<markers>.*)",
                text,
                re.IGNORECASE,
            )
            if not match:
                continue
            label = match.group("label")
            marker_positions = {
                int(position)
                for position in re.findall(
                    r"\b([1-8])\s*(?:=|:|-)\s*(?:filled|mark|solid|square|x|yes)\b",
                    match.group("markers"),
                    re.IGNORECASE,
                )
            }
            if not marker_positions:
                continue
            values = [label] + ["filled" if position in marker_positions else "" for position in range(1, 9)]
        else:
            continue
        if not values:
            continue
        expected_size = len(columns) + 1 if columns else len(values)
        if len(values) < expected_size:
            values.extend([""] * (expected_size - len(values)))
        normalized.append(values[:expected_size])
    return normalized


def merge_reliable_extracted_facts(cleaned: dict[str, Any], source_draft: dict[str, Any]) -> dict[str, Any]:
    """Keep deterministic identifiers that were already parsed before narrative rewriting."""
    merged = deepcopy(cleaned)
    source_remote = source_draft.get("key_remote") if isinstance(source_draft.get("key_remote"), dict) else {}
    target_remote = merged.setdefault("key_remote", {})
    if isinstance(target_remote, dict):
        source_options = source_remote.get("known_options") if isinstance(source_remote.get("known_options"), list) else []
        target_options = target_remote.get("known_options") if isinstance(target_remote.get("known_options"), list) else []
        if len(source_options) > len(target_options):
            target_remote["known_options"] = deepcopy(source_options)
        for name in ("frequency", "fcc_id", "remote_type"):
            if not target_remote.get(name) and source_remote.get(name):
                target_remote[name] = source_remote[name]
    source_decoders = source_draft.get("decoders") if isinstance(source_draft.get("decoders"), list) else []
    target_decoders = merged.get("decoders") if isinstance(merged.get("decoders"), list) else []
    if source_decoders:
        target_decoders = deepcopy(source_decoders)
    by_tool = {
        str(item.get("tool") or "").casefold(): item
        for item in target_decoders if isinstance(item, dict) and item.get("tool")
    }
    for item in source_decoders:
        if isinstance(item, dict) and item.get("tool") and str(item["tool"]).casefold() not in by_tool:
            target_decoders.append(deepcopy(item))
    if target_decoders:
        merged["decoders"] = target_decoders
    source_transponder = source_draft.get("transponder") if isinstance(source_draft.get("transponder"), dict) else {}
    target_transponder = merged.setdefault("transponder", {})
    if isinstance(target_transponder, dict):
        for name in ("test_key", "chip", "reusable", "transponder_type"):
            if not target_transponder.get(name) and source_transponder.get(name):
                target_transponder[name] = source_transponder[name]
        if source_transponder.get("cloner_tools"):
            target_transponder["cloner_tools"] = deepcopy(source_transponder["cloner_tools"])
    source_programming = source_draft.get("programming") if isinstance(source_draft.get("programming"), dict) else {}
    target_programming = merged.setdefault("programming", {})
    if isinstance(target_programming, dict):
        for name in ("pin_required", "factory_tool", "pin_guidance"):
            if source_programming.get(name):
                target_programming[name] = deepcopy(source_programming[name])
    source_making_key = source_draft.get("making_key") if isinstance(source_draft.get("making_key"), dict) else {}
    target_making_key = merged.setdefault("making_key", {})
    if isinstance(target_making_key, dict) and source_making_key.get("methods"):
        target_making_key["methods"] = deepcopy(source_making_key["methods"])
    if source_draft.get("lock_parts"):
        merged["lock_parts"] = deepcopy(source_draft["lock_parts"])
    source_mechanical = source_draft.get("mechanical_key") if isinstance(source_draft.get("mechanical_key"), dict) else {}
    target_mechanical = merged.setdefault("mechanical_key", {})
    if isinstance(target_mechanical, dict):
        source_spacing = source_mechanical.get("spacing") if isinstance(source_mechanical.get("spacing"), dict) else {}
        source_depths = source_mechanical.get("depths") if isinstance(source_mechanical.get("depths"), dict) else {}
        for name in (
            "code_series",
            "style",
            "card",
            "itl",
            "macs",
            "start_cut",
            "cut_to_cut",
            "air_bags",
            "ignition_retainer",
            "milling",
        ):
            if source_mechanical.get(name):
                target_mechanical[name] = deepcopy(source_mechanical[name])
        if _is_descending_cut_table(source_spacing, 8):
            target_mechanical["spacing"] = deepcopy(source_spacing)
        if _is_descending_cut_table(source_depths, 4):
            target_mechanical["depths"] = deepcopy(source_depths)
    return merged


def merge_source_supported_facts(draft: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Preserve readable source facts that should not rely on generative reconstruction."""
    merged = deepcopy(draft)
    source = " ".join(source_text.split())
    programming = merged.setdefault("programming", {})
    key_remote = merged.setdefault("key_remote", {})
    making_key = merged.setdefault("making_key", {})
    mechanical = merged.setdefault("mechanical_key", {})
    if isinstance(programming, dict) and re.search(r"Dealer\s+transponder\s+system", source, re.IGNORECASE):
        programming.setdefault(
            "system_requirement",
            "Dealer transponder system; specialized key programmer or EEPROM equipment is required.",
        )
    if isinstance(key_remote, dict) and re.search(r"\bKESSY\b", source, re.IGNORECASE):
        key_remote.setdefault("proximity_option", "Some models may be equipped with the KESSY proximity option.")
    if isinstance(making_key, dict) and re.search(
        r"No\s*codes\s*on\s*any\s*locks|Nocodesonanylocks",
        source,
        re.IGNORECASE,
    ):
        making_key["code_availability"] = "No codes on any locks are listed; decode an accessible cylinder to produce a working key."
    if isinstance(mechanical, dict) and re.search(
        r"INTERNAL.{0,24}(?:MILLING|MITUNG)",
        source,
        re.IGNORECASE | re.DOTALL,
    ):
        mechanical.setdefault("milling", "Internal milling")
    return merged


def apply_technical_patch(report: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Overlay only whitelisted technical fields returned by the focused vision pass."""
    merged = deepcopy(report)
    allowed = {
        "mechanical_key": {
            "style", "macs", "start_cut", "cut_to_cut", "milling", "air_bags",
            "ignition_retainer", "spacing", "depths",
        },
        "programming": {
            "pin_required", "factory_tool", "system_requirement", "pin_guidance",
            "tool_guidance", "cloning", "tools",
        },
        "transponder": {"transponder_type", "chip", "reusable", "test_key", "cloner_tools"},
        "making_key": {"code_availability", "methods"},
    }
    for section, fields in allowed.items():
        incoming = patch.get(section)
        if not isinstance(incoming, dict):
            continue
        target = merged.setdefault(section, {})
        if not isinstance(target, dict):
            target = {}
            merged[section] = target
        for field in fields:
            if field in incoming and incoming[field] not in (None, "", [], {}):
                target[field] = deepcopy(incoming[field])
    for section in ("decoders", "lock_parts"):
        incoming = patch.get(section)
        if isinstance(incoming, list) and incoming:
            merged[section] = deepcopy(incoming)
    diagrams = patch.get("procedure_diagrams")
    if isinstance(diagrams, list) and diagrams:
        merged["procedure_diagrams"] = deepcopy(diagrams)
    return merged


def _is_descending_cut_table(table: dict[str, Any], required_count: int) -> bool:
    if len(table) < required_count:
        return False
    values = []
    for position in range(1, required_count + 1):
        try:
            values.append(float(str(table[str(position)]).strip()))
        except (KeyError, TypeError, ValueError):
            return False
    return all(values[index] > values[index + 1] for index in range(len(values) - 1))


def is_informative_diagram(diagram: dict[str, Any]) -> bool:
    """Reject generic or low-data diagrams before publication."""
    if str(diagram.get("visual_type") or "") == "blade_orientation":
        return bool(str(diagram.get("blade_profile") or "").strip())
    panels = diagram.get("panels") if isinstance(diagram.get("panels"), list) else []
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        columns = [str(value).strip() for value in panel.get("columns", [])]
        if columns != [str(position) for position in range(1, 9)]:
            continue
        filled_by_row: list[int] = []
        for row in panel.get("rows", []):
            if not isinstance(row, list) or len(row) < 2:
                continue
            label = str(row[0] or "").upper()
            if not any(token in label for token in ("IGNITION", "DOOR", "TRUNK", "HATCH", "GLOVE")):
                continue
            count = sum(str(value).strip().lower() == "filled" for value in row[1:9])
            if count:
                filled_by_row.append(count)
        return sum(filled_by_row) >= 4 and max(filled_by_row, default=0) >= 2
    return False


def source_has_lock_position_visual(source: str) -> bool:
    """Detect actual lock-position visuals, not ordinary procedure prose."""
    normalized = re.sub(r"\s+", " ", source.upper())
    has_position_header = bool(re.search(r"\b(?:1\s+2\s+3\s+4\s+5\s+6\s+7\s+8|POSITIONS?|SPACES)\b", normalized))
    has_visual_lock_labels = bool(
        re.search(r"(?:>\s*)?IGNITION(?:\s*\([^)]+\))?.{0,80}(?:>\s*)?(?:DOORS?|TRUNK|HATCH)", normalized)
        or re.search(r"(?:DOORS?|TRUNK|HATCH).{0,80}IGNITION", normalized)
    )
    marked_grid_hint = bool(re.search(r"\b(?:FILLED|MARK(?:ED)?|SQUARE|WAFER|PIN)\b", normalized))
    compact_rows = len(re.findall(r"\b(?:IGNITION|DOORS?|TRUNK|HATCH|GLOVE\s+BOX)\b.{0,60}\b[1-8]\b", normalized)) >= 2
    return has_visual_lock_labels and (has_position_header or marked_grid_hint or compact_rows)


def _sync_mechanical_tables_from_diagrams(draft: dict[str, Any], diagrams: list[dict[str, Any]]) -> None:
    mechanical = draft.get("mechanical_key")
    if not isinstance(mechanical, dict):
        mechanical = {}
        draft["mechanical_key"] = mechanical
    for diagram in diagrams:
        if str(diagram.get("placement") or "") not in {"mechanical_key", "making_key"}:
            continue
        panels = diagram.get("panels")
        for panel in panels if isinstance(panels, list) else []:
            if not isinstance(panel, dict):
                continue
            label = str(panel.get("label") or "").strip().lower()
            target = "spacing" if label == "spacing" else "depths" if label in {"depth", "depths"} else ""
            columns = panel.get("columns") if isinstance(panel.get("columns"), list) else []
            rows = panel.get("rows") if isinstance(panel.get("rows"), list) else []
            if not target or not columns or not rows or not isinstance(rows[0], list):
                continue
            values = rows[0]
            if len(values) != len(columns) or not all(re.fullmatch(r"\.?\d+(?:\.\d+)?", str(value).strip()) for value in values):
                continue
            mechanical[target] = {
                str(column).strip(): str(value).strip()
                for column, value in zip(columns, values)
                if str(column).strip()
            }


def report_completeness_issues(draft: dict[str, Any], source_text: str) -> list[str]:
    source = source_text.upper()
    rendered = json.dumps(draft, ensure_ascii=False).upper()
    mechanical = draft.get("mechanical_key") if isinstance(draft.get("mechanical_key"), dict) else {}
    key_remote = draft.get("key_remote") if isinstance(draft.get("key_remote"), dict) else {}
    transponder = draft.get("transponder") if isinstance(draft.get("transponder"), dict) else {}
    programming = draft.get("programming") if isinstance(draft.get("programming"), dict) else {}
    known_options = key_remote.get("known_options") if isinstance(key_remote, dict) else []
    known_options = known_options if isinstance(known_options, list) else []
    applications = draft.get("vehicle_applications") if isinstance(draft.get("vehicle_applications"), list) else []
    issues: list[str] = []
    if re.search(r"\b(?:19|20)\d{2}\s*[-\u2013]\s*\d{2,4}\s+[A-Z]", source_text) and not applications:
        issues.append("Source lists vehicle applications, but the structured make/model/year mapping is missing.")
    if re.search(r"\bFCC(?:\s+INFO)?\s*[:#]", source):
        options_text = json.dumps(known_options, ensure_ascii=False).upper()
        if "FCC" not in options_text:
            issues.append("Source includes FCC information, but it is not associated with compatible remote options.")
        source_remote_count = len(re.findall(r"\b(?:FOB\s+)?REMOTE\s*:", source))
        if source_remote_count >= 2 and len(known_options) < source_remote_count:
            issues.append(
                f"Source lists {source_remote_count} remote options, but the structured table contains only {len(known_options)}."
            )
    expected_decoders = _extract_decoder_rows(source_text)
    expected_lishi = [
        decoder for decoder in expected_decoders
        if str(decoder.get("tool") or "").upper() == "LISHI"
    ]
    if expected_lishi:
        decoder_text = json.dumps(draft.get("decoders") or [], ensure_ascii=False).upper()
        if "LISHI" not in decoder_text:
            issues.append("Source includes a Lishi reference but the decoder table does not.")
    if expected_decoders:
        decoder_text = json.dumps(draft.get("decoders") or [], ensure_ascii=False).upper()
        for decoder in expected_decoders:
            reference = str(decoder.get("reference") or "").upper()
            tool = str(decoder.get("tool") or "").upper()
            if reference and (tool not in decoder_text or reference not in decoder_text):
                issues.append(f"Source decoder {tool} {reference} is missing or mismatched in the structured decoder table.")
                break
    if re.search(r"\bFactory\s+Tool\b", source, re.IGNORECASE) and not programming.get("factory_tool"):
        issues.append("Source includes a factory diagnostic tool, but programming.factory_tool is missing.")
    if re.search(r"\b(?:ILCO|KEYWAY)\s*[:#]", source) and not mechanical.get("ilco_keyway"):
        issues.append("Source includes blade/keyway identification but the structured report does not.")
    if re.search(r"\b(?:TDB1000|TCODE|MVP|AUTEL|SMART\s*PRO|AUTO\s*PRO\s*PAD)\b", str(mechanical.get("ilco_keyway") or ""), re.IGNORECASE):
        issues.append("A programmer/tool reference was incorrectly placed in the blade/keyway field.")
    if re.search(r"\bSPAC(?:ING|NG)\b", source) and not mechanical.get("spacing"):
        issues.append("Source includes spacing data but the structured report does not.")
    if "DEPTH" in source and not mechanical.get("depths"):
        issues.append("Source includes depth data but the structured report does not.")
    if re.search(r"\bEIGHT\s+SPACES\b", source) and len(mechanical.get("spacing") or {}) < 8:
        issues.append("Source specifies eight spacing positions, but the structured spacing table is incomplete.")
    if re.search(r"\bEIGHT\s+SPACES\b", source) and mechanical.get("spacing") and not _is_descending_cut_table(mechanical.get("spacing"), 8):
        issues.append("The eight-position spacing table is not in a valid handle-to-tip descending order.")
    if re.search(r"\bFOUR\s+DEPTHS\b", source) and len(mechanical.get("depths") or {}) < 4:
        issues.append("Source specifies four depths, but the structured depth table is incomplete.")
    if re.search(r"\bFOUR\s+DEPTHS\b", source) and mechanical.get("depths") and not _is_descending_cut_table(mechanical.get("depths"), 4):
        issues.append("The four-depth table is not in a valid descending order.")
    if "MACS" in source and not mechanical.get("macs"):
        issues.append("Source includes a MACS value, but the mechanical key data does not.")
    if re.search(r"\bSTYLE\b|STVIE", source) and not mechanical.get("style"):
        issues.append("Source includes a key style, but the mechanical key data does not.")
    if "STARTCUT" in source and "start_cut" not in mechanical:
        issues.append("Source includes a start-cut field, but the mechanical key data does not preserve it.")
    if "CUTTOCUT" in source and "cut_to_cut" not in mechanical:
        issues.append("Source includes a cut-to-cut field, but the mechanical key data does not preserve it.")
    if "INTERNAL" in source and ("MITUNG" in source or "MILLING" in source) and not mechanical.get("milling"):
        issues.append("Source identifies internal milling, but the mechanical key data does not.")
    if "AIRBAGS" in source and not mechanical.get("air_bags"):
        issues.append("Source includes airbag service data, but the mechanical key data does not.")
    if "RETAINER" in source and not mechanical.get("ignition_retainer"):
        issues.append("Source includes ignition-retainer data, but the mechanical key data does not.")
    if str(mechanical.get("ignition_retainer") or "").strip().lower() in {"on", "or", "in", "yes"}:
        issues.append("Ignition-retainer value appears corrupted and must be reread from the source.")
    if re.search(r"\bPIN\b", source) and "PIN" not in rendered:
        issues.append("Source includes PIN guidance but the structured report does not.")
    methods = (draft.get("making_key") or {}).get("methods") if isinstance(draft.get("making_key"), dict) else []
    methods = methods if isinstance(methods, list) else []
    if re.search(r"\b(?:METHOD|DECODER|CUTTING THE KEY|MAKING KEY)\b", source) and not methods:
        issues.append("Source includes a making-key procedure but the structured report does not.")
    source_method_count = len(re.findall(r"\bMETHOD\s*#?\s*\d+", source))
    if source_method_count >= 2 and len(methods) < source_method_count:
        issues.append(
            f"Source lists {source_method_count} key-making methods, but the structured report contains only {len(methods)}."
        )
    if "TRYOUT" in source and "TRYOUT" not in rendered:
        issues.append("Source includes a tryout-key method, but the structured procedure does not preserve it.")
    if re.search(r"\b(?:CHIP|TRANSPONDER)\b", source) and not draft.get("transponder"):
        issues.append("Source includes transponder details but the structured report does not.")
    if re.search(r"\bCHIP\s*INFORMATION\b", source) and not transponder.get("chip"):
        issues.append("Source includes a chip-information block, but the chip identifier is missing.")
    if re.search(r"\bTEST\s*KEY\s*IS\b|\bTESTKEYIS", source) and not transponder.get("test_key"):
        issues.append("Source identifies a test key, but the structured test-key field is missing.")
    if re.search(r"\b(?:PROGRAMMER|DIAGNOSTIC|SMART PRO|TCODE|MVP)\b", source) and not draft.get("programming"):
        issues.append("Source includes programming/tool guidance but the structured report does not.")
    if re.search(r"\bFACTORY\s+TOO[LI!]\b", source) and not (draft.get("programming") or {}).get("factory_tool"):
        issues.append("Source identifies a factory programming tool, but the structured report does not.")
    if "PROGRAMMER & DIAGNOSTIC TOOLS" in source and "SUBSCRIPTION" in source and not (draft.get("programming") or {}).get("factory_tool"):
        issues.append("Source visually identifies a factory-tool/subscription path, but the structured report does not.")
    if "DEALER TRANSPONDER SYSTEM" in source and not (draft.get("programming") or {}).get("system_requirement"):
        issues.append("Source identifies a dealer transponder/programmer requirement, but the structured report does not.")
    if re.search(r"\bCLONER\s*MACHINE\s*INFORMATION\b", source) and not transponder.get("cloner_tools"):
        issues.append("Source contains cloner-machine support statuses, but the structured report does not.")
    if re.search(r"\bCLONER\s*MACHINE\s*INFORMATION\b", source) and isinstance(transponder.get("cloner_tools"), list):
        invalid_cloner_rows = [
            item for item in transponder["cloner_tools"]
            if not isinstance(item, dict)
            or not str(item.get("name") or item.get("model") or "").strip()
            or not str(item.get("status") or "").strip()
        ]
        if invalid_cloner_rows:
            issues.append("The cloner-machine table contains incomplete tool rows.")
    if "KESSY" in source and "KESSY" not in rendered:
        issues.append("Source mentions the KESSY proximity option, but the report does not.")
    if "PROXIMITY OPTION" in source and str(draft.get("system_type") or "").strip().upper().startswith("PROXIMITY"):
        issues.append("Source presents proximity as an option, but the report labels the base system as proximity-only.")
    if "DEALER DOES NOT HAVE ACCESS TO PIN" in source and not (draft.get("programming") or {}).get("pin_guidance"):
        issues.append("Source includes PIN-access limitations, but the programming guidance does not preserve them.")
    if re.search(r"\bNOCODESONANYLOCKS\b", source) and "NO CODES ON ANY LOCKS" not in rendered:
        issues.append("Source states that no codes are available on any locks, but the report does not.")
    if re.search(r"\b(?:LOCKS?\s+NOT\s+AVAILABLE|PIN\s+KIT|STRATTEC\s+(?:LOCK|PART)|(?:IGNITION|DOOR|TRUNK)\s+LOCK\s*[:=-])\b", source) and not draft.get("lock_parts"):
        issues.append("Source includes lock-parts or supplier information but the structured report does not.")
    if "PIN KIT" in source and "PIN KIT" not in rendered:
        issues.append("Source includes a lock-parts PIN kit number, but the structured report does not.")
    if re.search(r"\b(?:PIN\s+KIT|STRATTEC|IGNITION\s+LOCK|DOOR\s+LOCK|TRUNK\s+LOCK|LIFTGATE)\b", source):
        for part_number in sorted(set(re.findall(r"\b70\d{4}\b", source))):
            if part_number not in rendered:
                issues.append(f"Source lock-parts table includes part number {part_number}, but it is missing from the structured report.")
                break
    if source_has_lock_position_visual(source_text):
        diagrams = draft.get("procedure_diagrams") if isinstance(draft.get("procedure_diagrams"), list) else []
        has_lock_diagram = False
        for diagram in diagrams:
            if not isinstance(diagram, dict) or not is_informative_diagram(diagram):
                continue
            panels = diagram.get("panels") if isinstance(diagram.get("panels"), list) else []
            for panel in panels:
                if not isinstance(panel, dict) or not isinstance(panel.get("rows"), list) or not panel["rows"]:
                    continue
                columns = [str(value).strip() for value in panel.get("columns", [])]
                expected_rows = [
                    label for label in ("IGNITION", "DOORS", "TRUNK", "GLOVE BOX")
                    if label in source
                ]
                rows_by_label = {
                    str(row[0]).upper(): row
                    for row in panel["rows"] if isinstance(row, list) and row
                }
                rows_complete = True
                for label in expected_rows:
                    row = next((value for key, value in rows_by_label.items() if label in key), None)
                    if not row or len(row) != 9 or not any(str(value).strip().lower() == "filled" for value in row[1:]):
                        rows_complete = False
                        break
                if columns == [str(position) for position in range(1, 9)] and rows_complete:
                    has_lock_diagram = True
                    break
            if has_lock_diagram:
                break
        if not has_lock_diagram:
            issues.append("Source includes an instructional lock-position visual, but no original lock-position diagram was produced.")
    return issues


def extract_response_text(data: dict[str, Any]) -> str | None:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
    clean = "\n".join(chunks).strip()
    return clean or None
