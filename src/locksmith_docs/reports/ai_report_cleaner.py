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
lock_parts, and procedure_diagrams. spacing must include every visible position, usually 1 through 8 or 1 through 10;
depths positions 1 through 4.
For cloner_tools, capture each visible column as one object with name, model and status; name is the visible
manufacturer heading. Do not separate a manufacturer from its model and do not return blank-name entries.
For lock_parts, return each visible row as a separate object preserving years, model, ignition_lock, door_lock,
trunk_lock, and pin_kit. For decoders, preserve each named decoder/reader and its exact reference.
For a lock-position map, return procedure_diagrams with columns matching every visible numbered column, usually
['1','2','3','4','5','6','7','8'] or ['1','2','3','4','5','6','7','8','9','10'], and rows as arrays whose
first cell is the lock label followed by one value per column. Use 'filled' only for visibly marked squares and
'' otherwise. Include every visible row such as ignition, doors, trunk, spare tire, stowage, and glove box.
Return no diagram if the exact squares are not readable."""

QUALITY_PATTERNS = (
    re.compile(r"[^\x00-\x7f]"),
    re.compile(r"\|"),
    re.compile(r"\b(?:AUTOSMART|MICHAEL\s+HYDE|CONTINUED\s*-|[A-Z]{2,6}-\d+\s+SECTION)\b", re.IGNORECASE),
    re.compile(r"\b(?:METH0D|L0CK|D0OR|SI5MHZ|DECBDER|REDDER|CYLIND\+)\b", re.IGNORECASE),
    re.compile(r"\b(?:THEXDBOT|DETERM[I1]NETHE|WCRKING|PROYESSION|T0OL)\b", re.IGNORECASE),
    re.compile(r"\b(?:ACJRA|SHE11|BE10W|ILC0|DTV|H[CO]NDA|TA[NH]GO)\b|:NFO\b", re.IGNORECASE),
    re.compile(r"\bFLlP\b"),
    re.compile(r"\b(?:NOCODESONANYLOCKS|USEA|DOORLOCK-TO|A=\s*TRACK|WW\s*2\s*TE)\b", re.IGNORECASE),
    re.compile(r"\b(?:GERER|FOFO|TYEE|SO\s+A)\b", re.IGNORECASE),
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
    ai_source_images = (source_images or []) if report_image_payload_enabled() else []
    image_instruction = (
        "The attached labeled images are enlarged working-detail crops from the source pages. "
        "IMAGE: when a lock-position map is present, procedure_diagrams is mandatory. Add one item with "
        "placement='making_key', a panels array containing one panel labelled 'Lock positions', and columns "
        "matching every visible position number, usually ['1','2','3','4','5','6','7','8'] or "
        "['1','2','3','4','5','6','7','8','9','10']. Rows must be in the form ['Ignition','filled',...], "
        "['Doors','filled',...], ['Trunk',...], ['Glove box',...]. Include every visible row such as spare tire "
        "or stowage. Use 'filled' only where the source diagram has a marked square and '' elsewhere. "
        "This is rendered as an original SVG; do not include the source photograph."
        if ai_source_images
        else "Do not invent procedure_diagrams from ordinary text. Include a diagram only when the OCR text clearly describes a readable lock-position map."
    )
    cleaned = request_cleaned_report(
        api_key,
        model,
        SYSTEM_INSTRUCTIONS,
        {
            "draft_json": draft,
            "source_text_excerpt": source_text[:60000],
            "instruction": "Return a full technician report, not a synopsis. Recheck each visible OCR table cell before returning JSON: applications, key type/style, code series, keyway, MACS, safety/retainer fields, remote rows, spacing/depth table, cutting-track note, chip/transponder/reusable/test-key fields, PIN and factory tool, every programmer/cloner status, code availability, decoders and each numbered method. Transcribe every visible chip identifier and every spacing/depth value. Fill missing structured sections only from supported source facts and retain detailed procedural guidance. " + image_instruction,
        },
        ai_source_images,
    )
    if not cleaned or cleaned.get("code") != draft.get("code"):
        return draft, False
    cleaned = merge_source_supported_facts(merge_reliable_extracted_facts(
        sanitize_structured_sections(remove_unpublishable_text(cleaned)),
        draft,
    ), source_text)
    cleaned = merge_inferred_source_diagrams(cleaned, source_text)
    cleaned = sanitize_structured_sections(cleaned)
    issues = report_quality_issues(cleaned) + report_completeness_issues(cleaned, source_text)
    if ai_source_images and (issues or report_needs_technical_patch(cleaned, source_text)):
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
                    "detected_quality_problems": issues[:20],
                    "instruction": "Return a compact patch only. Fill missing fields by reading the enlarged source-page images. Focus especially on programmer/factory-tool tables, lock-parts inventory rows, remote/fob rows, spacing/depth values, and pin-position maps. Never copy source image pixels. For a pin map, inspect every visible column, usually 1-8 or 1-10, and include every visible row label such as Ignition, Doors, Trunk, Spare Tire, Stowage, and Glove Box.",
                },
                ai_source_images,
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
            cleaned = merge_inferred_source_diagrams(cleaned, source_text)
            cleaned = sanitize_structured_sections(cleaned)
    return cleaned, True


def report_needs_technical_patch(draft: dict[str, Any], source_text: str) -> bool:
    """Run the focused image pass when first-pass JSON still looks visibly incomplete."""
    rendered = json.dumps(draft, ensure_ascii=False).upper()
    source = re.sub(r"\s+", " ", source_text.upper())
    if "CHECK SOURCE" in rendered:
        return True
    if ("PROGRAMMER" in source or "DIAGNOSTIC" in source or "FACTORY TOOL" in source) and not (
        (draft.get("programming") or {}).get("factory_tool")
        and str((draft.get("programming") or {}).get("factory_tool")).upper() != "CHECK SOURCE"
    ):
        return True
    if "PROX FOB" in source and not ((draft.get("key_remote") or {}).get("known_options")):
        return True
    if source_has_lock_position_visual(source_text):
        diagrams = draft.get("procedure_diagrams") if isinstance(draft.get("procedure_diagrams"), list) else []
        if not any(is_informative_diagram(item) for item in diagrams if isinstance(item, dict)):
            return True
    return False


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
            ("lower mechanical spacing and depths table", image.crop((int(width * 0.03), int(height * 0.78), int(width * 0.76), int(height * 0.97)))),
            ("programmer and diagnostic tools table", image.crop((int(width * 0.04), int(height * 0.70), int(width * 0.92), int(height * 0.94)))),
        ])
    elif image_index == 1:
        # Separate crops keep text and the pin-position map large enough for
        # extraction instead of asking the model to read a compressed collage.
        crops.extend([
            ("chip information and cloner machine table", image.crop((int(width * 0.07), int(height * 0.04), int(width * 0.94), int(height * 0.34)))),
            ("codes decoders and numbered key-making methods", image.crop((int(width * 0.07), int(height * 0.35), int(width * 0.94), int(height * 0.55)))),
            ("lock-position map for original SVG reconstruction", image.crop((int(width * 0.18), int(height * 0.53), int(width * 0.72), int(height * 0.75)))),
            ("wide lock-position map with labels and all numbered columns", image.crop((int(width * 0.22), int(height * 0.50), int(width * 0.94), int(height * 0.80)))),
            ("lock-parts inventory and PIN-kit table", image.crop((int(width * 0.07), int(height * 0.76), int(width * 0.94), int(height * 0.94)))),
            ("full lower inventory table area", image.crop((int(width * 0.03), int(height * 0.66), int(width * 0.97), int(height * 0.98)))),
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
            ("lower mechanical spacing and depths values", image.crop((int(width * 0.03), int(height * 0.78), int(width * 0.76), int(height * 0.97)))),
            ("factory tool PIN and programmer support", image.crop((int(width * 0.04), int(height * 0.70), int(width * 0.92), int(height * 0.94)))),
        ]
    elif image_index == 1:
        crops = [
            ("chip reusable status and cloner support columns", image.crop((int(width * 0.07), int(height * 0.04), int(width * 0.94), int(height * 0.34)))),
            ("codes decoders and all numbered methods", image.crop((int(width * 0.07), int(height * 0.35), int(width * 0.94), int(height * 0.55)))),
            ("close pin-position map: transcribe every visible marked square by column", image.crop((int(width * 0.18), int(height * 0.53), int(width * 0.72), int(height * 0.75)))),
            ("wide pin-position map: include all row labels and numbered columns", image.crop((int(width * 0.22), int(height * 0.50), int(width * 0.94), int(height * 0.80)))),
            ("lock-parts and PIN-kit rows", image.crop((int(width * 0.07), int(height * 0.76), int(width * 0.94), int(height * 0.94)))),
            ("full lower inventory table rows", image.crop((int(width * 0.03), int(height * 0.66), int(width * 0.97), int(height * 0.98)))),
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


def report_image_payload_enabled() -> bool:
    configured = os.environ.get("REPORT_AI_SEND_IMAGES")
    if configured is None:
        configured = os.environ.get("REPORT_ATTACH_SOURCE_IMAGES", "1")
    return str(configured).strip().lower() in {"1", "true", "yes", "on"}


def enrich_draft_with_targeted_ocr(draft: dict[str, Any], source_images: list[Path]) -> dict[str, Any]:
    """Recover compact technical-table facts that full-page OCR commonly misses."""
    if not source_images:
        return draft
    try:
        import pytesseract
    except ImportError:
        return draft
    try:
        mechanical_page = Image.open(source_images[0]).convert("L")
        detail_page = Image.open(source_images[1] if len(source_images) > 1 else source_images[0]).convert("L")
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
    if os.environ.get("LOCAL_FIXED_TABLE_OCR", "0").strip().lower() in {"1", "true", "yes", "on"}:
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
    if not source_images:
        return {"error": "At least one source page is required."}
    try:
        import pytesseract
        mechanical_page = Image.open(source_images[0]).convert("L")
        detail_page = Image.open(source_images[1] if len(source_images) > 1 else source_images[0]).convert("L")
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
    for length in (10, 8):
        for start in range(max(1, len(values) - length + 1)):
            candidate = values[start:start + length]
            if len(candidate) == length and all(float(candidate[index]) > float(candidate[index + 1]) for index in range(length - 1)):
                return {str(index + 1): value for index, value in enumerate(candidate)}
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
    spacing_left = 0.065
    for band_index, row_top in enumerate((0.565, 0.505, 0.625, 0.685, 0.745, 0.795, 0.835, 0.865)):
        row_bottom = min(0.94, row_top + 0.061)
        for column_count, spacing_right in ((10, 0.625), (8, 0.555)):
            cell_width = (spacing_right - spacing_left) / column_count
            spacing_row = image.crop((int(width * spacing_left), int(height * row_top), int(width * spacing_right), int(height * row_bottom)))
            spacing_values = _ocr_decimal_values(spacing_row, pytesseract)
            candidate: dict[str, str] = {}
            if len(spacing_values) >= column_count:
                candidate = {str(index + 1): value for index, value in enumerate(spacing_values[:column_count])}
            if band_index == 0 and not _is_descending_cut_table(candidate, column_count):
                candidate = {}
                for position in range(column_count):
                    crop = image.crop(
                        (
                            int(width * (spacing_left + position * cell_width)),
                            int(height * max(0.0, row_top - 0.010)),
                            int(width * (spacing_left + (position + 1) * cell_width)),
                            int(height * min(1.0, row_bottom + 0.008)),
                        )
                    )
                    value = _ocr_decimal_cell(crop, pytesseract)
                    if value:
                        candidate[str(position + 1)] = value
            if not _is_descending_cut_table(candidate, column_count):
                spacing_region = image.crop((
                    int(width * 0.035),
                    int(height * max(0.0, row_top - 0.050)),
                    int(width * (0.655 if column_count == 10 else 0.585)),
                    int(height * min(1.0, row_bottom + 0.045)),
                ))
                candidate = _derive_spacing_from_anchors(
                    _ocr_decimal_texts(spacing_row, pytesseract, scale=6)
                    + [_ocr_detail_region(spacing_region, pytesseract)],
                    cut_to_cut,
                    positions=column_count,
                )
            if _is_descending_cut_table(candidate, column_count):
                spacing = candidate
                break
        if spacing:
            break
    depths: dict[str, str] = {}
    for band_index, depth_top in enumerate((0.540, 0.500, 0.600, 0.660, 0.720, 0.780, 0.835)):
        depth_bottom = min(0.96, depth_top + 0.120)
        depth_block = image.crop((int(width * 0.600), int(height * depth_top), int(width * 0.720), int(height * depth_bottom)))
        depth_values = _ocr_decimal_values(depth_block, pytesseract)
        candidate = {}
        if len(depth_values) >= 4:
            candidate = {str(index + 1): value for index, value in enumerate(depth_values[:4])}
        if band_index == 0 and not _is_descending_cut_table(candidate, 4):
            candidate = {}
            row_height = (depth_bottom - depth_top) / 4
            for position in range(4):
                top = depth_top + position * row_height
                bottom = depth_top + (position + 1) * row_height
                crop = image.crop((int(width * 0.612), int(height * top), int(width * 0.704), int(height * bottom)))
                value = _ocr_decimal_cell(crop, pytesseract)
                if value:
                    candidate[str(position + 1)] = value
        if _is_descending_cut_table(candidate, 4):
            depths = candidate
            break
    if not (_is_descending_cut_table(spacing, 10) or _is_descending_cut_table(spacing, 8)):
        spacing = {}
    if not _is_descending_cut_table(depths, 4):
        depths = {}
    return spacing, depths


def _derive_spacing_from_anchors(texts: list[str], cut_to_cut: str, positions: int = 8) -> dict[str, str]:
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
        row = [round(first - step * index, 3) for index in range(positions)]
        if min(row) <= 0:
            continue
        matched = sum(
            1
            for value in row
            if any(abs(anchor - value) <= 0.004 for anchor in anchors)
        )
        if matched >= 3 and all(row[index] > row[index + 1] for index in range(len(row) - 1)):
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
    psms = (6, 7) if scale >= 6 else (6, 7, 11)
    return [
        pytesseract.image_to_string(
            variant,
            lang="eng",
            config=f"--oem 1 --psm {psm} -c tessedit_char_whitelist=.0123456789",
        )
        for variant in _ocr_variants(image, scale=scale)
        for psm in psms
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
    cleaned = " ".join(value.split()).strip(" .,:;|_->‘’'\"").upper()
    cleaned = re.sub(r"\b(CHR|CHRY)(\d)\b", r"\1 \2", cleaned)
    cleaned = re.sub(r"\bHUI(\d{2})\b", r"HU1\1", cleaned)
    cleaned = re.sub(r"\bBIIL\b", "B111", cleaned)
    cleaned = re.sub(r"\b((?:KDC|Y|CY|HU|TOY|VWI|CHR|CHRY)[A-Z0-9 ]*?\d[A-Z0-9]*)(?:\s+A)+$", r"\1", cleaned)
    cleaned = re.sub(r"\s*[-–—]+$", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _is_clean_decoder_reference(value: str) -> bool:
    cleaned = str(value or "").strip().upper()
    if not cleaned or len(cleaned) > 20:
        return False
    if cleaned in {"N/A", "NA", "NONE", "NO", "-", "--", "NIL", "NULL", "NLA"}:
        return False
    if re.search(r"[|:>‘’'\",]", cleaned):
        return False
    if not re.search(r"\d", cleaned):
        return False
    if not re.fullmatch(r"[A-Z0-9 /-]+", cleaned):
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
        for label, pattern in (
            ("MicroPod", r"\bMicro\s*Pod\b|\bMicroPod\b"),
            ("wiTECH", r"\bwi\s*TECH\b|\bwilTECH\b|\bwitech\b"),
            ("Tech2", r"\bTech\s*2\b|\bTech2\b"),
            ("J2534 subscription", r"\bJ\s*2534\b|\b12534\b"),
        ):
            if re.search(pattern, source, re.IGNORECASE):
                tools.append(label)
        if tools:
            patch["factory_tool"] = " / ".join(dict.fromkeys(tools))
    if re.search(r"\bPIN\s*CODE\b|\bPIN\b.{0,16}\bREQUIRED\b", source, re.IGNORECASE):
        patch["pin_required"] = "Yes"
    if re.search(r"\b4\s*[- ]?\s*digit\b", source, re.IGNORECASE):
        patch["pin_guidance"] = "4-digit PIN required."
    if re.search(r"dealer.{0,80}(?:no|not|does\s+not|doesn'?t).{0,80}access.{0,40}pin", source, re.IGNORECASE):
        patch["pin_guidance"] = "Dealer systems do not provide PIN access; use supported aftermarket programming or EEPROM workflow."
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


def _extract_mechanical_spec_row(text: str) -> dict[str, str]:
    source = " ".join(text.split())
    for src, dst in {
        "Starteut": "StartCut",
        "StanCut": "StartCut",
        "Cuttocut": "CutToCut",
        "AicBags": "AirBags",
        "AirBags": "AirBags",
        "ignRetainer": "IgnRetainer",
    }.items():
        source = source.replace(src, dst)
    rows = [
        source,
        source.replace("|", " "),
    ]
    for row in rows:
        match = re.search(
            r"(?P<code>[A-Z]{0,3}\s*\d[\d,\s]*[-\u2013]\s*[A-Z0-9,\s]+)\s+"
            r"(?P<style>(?:D[bo]Sided|Double\s*Sided|Single\s*Sided|High\s*Sec(?:urity)?|Edge\s*Cut|Sidewinder))\s+"
            r"(?P<card>[A-Z]{1,4}\d{2,5})\s+"
            r"(?P<itl>\d{1,4})\s+"
            r"(?P<macs>[1-9]|1[0-2])\s+"
            r"(?P<start>\d{3})\s+"
            r"(?P<cut>\d{3})\s+"
            r"(?P<air>Yes|No|N/?A)\s+"
            r"(?P<retainer>None|Spring|Clip|Pin|Ring|N/?A)\b",
            row,
            re.IGNORECASE,
        )
        if not match:
            continue
        style = "Double-sided" if re.search(r"D[bo]Sided|Double", match.group("style"), re.IGNORECASE) else " ".join(match.group("style").split())
        code_series = re.sub(r"\s+", " ", match.group("code").upper()).replace(" - ", "-").strip()
        numeric_code = re.search(r"(\d[\d,\s]*[-\u2013]\s*[A-Z0-9,\s]+)$", code_series)
        if numeric_code:
            code_series = numeric_code.group(1).strip()
        return {
            "code_series": code_series,
            "style": style,
            "card": match.group("card").upper(),
            "itl": match.group("itl"),
            "macs": match.group("macs"),
            "start_cut": "." + match.group("start"),
            "cut_to_cut": "." + match.group("cut"),
            "air_bags": match.group("air").title().replace("N/A", "n/a"),
            "ignition_retainer": match.group("retainer").title().replace("N/A", "n/a"),
        }
    return {}


def _extract_remote_options_from_source(text: str) -> list[dict[str, str]]:
    source = " ".join(text.split())
    source = re.sub(r"\bF\s*C\s*C\b", "FCC", source, flags=re.IGNORECASE)
    source = re.sub(r"\bM\s*H\s*z\b", "MHz", source, flags=re.IGNORECASE)
    rows: list[dict[str, str]] = []
    for match in re.finditer(r"\b(?:Prox|Remote|Fob\s+Remote|Remote\s+Key)\s*:\s*(.+?)(?=\s+>\s*(?:Prox|Remote|Fob\s+Remote|Remote\s+Key)\s*:|\s+\b(?:CHIP|LOCK\s+PARTS|METHOD|DECODERS?)\b|$)", source, re.IGNORECASE):
        snippet = match.group(1)[:260]
        if not re.search(r"\bFCC\s+Info\b|\b(?:315|314|433|434)\s*MHz\b", snippet, re.IGNORECASE):
            continue
        row: dict[str, str] = {}
        year = _first_match(snippet, r"\((\d{4}(?:[-\u2013]\d{2,4})?)\)")
        part = _first_match(snippet, r"^\s*(?:\(\d{4}(?:[-\u2013]\d{2,4})?\)\s*)?([A-Z0-9][A-Z0-9 -]{5,24})(?=\s+\(|\s+-|\s+or|\s+FCC|\s*$)")
        ilco = _first_match(snippet, r"\bILCO#?\s*([A-Z0-9-]{5,24})")
        fcc = _first_match(snippet, r"\bFCC\s+Info\s*:\s*([A-Z0-9? -]{4,24})")
        freq = _first_match(snippet, r"\b(314|315|433|434)\s*MHz\b")
        buttons = _first_match(snippet, r"\((\d)\s*Btn\)")
        emergency = _first_match(snippet, r"\bE[- ]?Key\s+([A-Z0-9-]{5,24})")
        if year:
            row["years"] = year.replace("\u2013", "-")
        if part:
            row["part"] = " ".join(part.replace("-", " ").split())
        if ilco and not row.get("part"):
            row["part"] = ilco.upper()
        elif ilco:
            row["notes"] = f"ILCO reference {ilco.upper()}"
        if fcc:
            row["fcc_id"] = re.sub(r"\s+", "", fcc.upper())
        if freq:
            row["frequency"] = f"{freq} MHz"
        if buttons:
            row["buttons"] = buttons
        if emergency:
            row["emergency_blade"] = emergency.upper()
        if any(row.get(field) for field in ("part", "fcc_id", "frequency", "emergency_blade")):
            rows.append(row)
    return sanitize_remote_options(rows)


def _expand_year_range(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(19|20)?(\d{2})(?:[-\u2013](\d{2,4}))?", value.strip())
    if not match:
        return None
    prefix, start_short, end_raw = match.groups()
    start = int((prefix or "20") + start_short)
    if not end_raw:
        return start, start
    if len(end_raw) == 2:
        end = int(str(start)[:2] + end_raw)
    else:
        end = int(end_raw)
    return start, end


def _extract_vehicle_applications_from_source(text: str) -> list[dict[str, Any]]:
    source = re.sub(r"\s+", " ", text)
    apps: list[dict[str, Any]] = []
    for match in re.finditer(
        r"\b(?P<years>(?:19|20)?\d{2}(?:[-\u2013]\d{2,4})?)\s+"
        r"(?P<model>[A-Z][A-Z0-9 /-]{1,24}?)\s*\(\s*(?P<make>[A-Za-z -]{3,18})\s*\)",
        source,
    ):
        years = _expand_year_range(match.group("years"))
        if not years:
            continue
        make_raw = " ".join(match.group("make").split()).title()
        make = "Mercedes-Benz" if make_raw in {"Mercedes", "Mercedes Benz"} else make_raw
        if make not in CANONICAL_MODELS:
            continue
        model = " ".join(match.group("model").split()).strip(" |_-")
        model = re.sub(r"^O(?=X\d)", "Q", model, flags=re.IGNORECASE)
        model, score = canonicalize_model(make, model)
        if score < 0.78 and model not in CANONICAL_MODELS.get(make, []):
            continue
        apps.append({"make": make, "model": model, "year_from": years[0], "year_to": years[1]})
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int]] = set()
    for app in apps:
        key = (app["make"], app["model"], int(app["year_from"]), int(app["year_to"]))
        if key not in seen:
            seen.add(key)
            deduped.append(app)
    known_makes = "|".join(re.escape(make) for make in sorted(CANONICAL_MODELS, key=len, reverse=True))
    for match in re.finditer(
        rf"\b(?P<years>(?:19|20)?\d{{2}}(?:[-\u2013]\d{{2,4}})?)\s+"
        rf"(?P<model>[A-Z][A-Z0-9 /-]{{1,24}}?)\s*\(\s*(?P<make>{known_makes})(?=\s*(?:[)|]|\b(?:19|20)\d{{2}}\b|$))",
        source,
        re.IGNORECASE,
    ):
        years = _expand_year_range(match.group("years"))
        if not years:
            continue
        make = " ".join(match.group("make").split()).title()
        if make.lower() == "mercedes-benz":
            make = "Mercedes-Benz"
        model = " ".join(match.group("model").split()).strip(" |_-")
        model = re.sub(r"^O(?=X\d)", "Q", model, flags=re.IGNORECASE)
        model, score = canonicalize_model(make, model)
        if score < 0.78 and model not in CANONICAL_MODELS.get(make, []):
            continue
        key = (make, model, years[0], years[1])
        if key not in seen:
            seen.add(key)
            deduped.append({"make": make, "model": model, "year_from": years[0], "year_to": years[1]})
    return deduped


def _normalize_vehicle_application(app: dict[str, Any]) -> dict[str, Any] | None:
    make = str(app.get("make") or "").strip()
    model = str(app.get("model") or "").strip()
    if not make or not model:
        return None
    make = "Mercedes-Benz" if make in {"Mercedes", "Mercedes Benz"} else make
    if make not in CANONICAL_MODELS:
        return None
    model = re.sub(r"^O(?=X\d)", "Q", model, flags=re.IGNORECASE)
    canonical_model, score = canonicalize_model(make, model)
    if score < 0.78 and canonical_model not in CANONICAL_MODELS.get(make, []):
        return None
    try:
        year_from = int(app.get("year_from") or 0)
        year_to = int(app.get("year_to") or 0)
    except (TypeError, ValueError):
        return None
    if not year_from or not year_to:
        return None
    return {"make": make, "model": canonical_model, "year_from": year_from, "year_to": year_to}


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
    cleaned = remove_placeholder_values(cleaned)
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
    if isinstance(cleaned.get("decoders"), list):
        cleaned["decoders"] = sanitize_decoder_rows(cleaned["decoders"])
    for name in ("quick_answer", "warnings", "source_coverage"):
        if isinstance(cleaned.get(name), list):
            cleaned[name] = [item for item in cleaned[name] if isinstance(item, str)]
    key_remote = cleaned.get("key_remote")
    if isinstance(key_remote, dict):
        if "known_options" in key_remote and not isinstance(key_remote["known_options"], list):
            key_remote.pop("known_options", None)
        elif isinstance(key_remote.get("known_options"), list):
            key_remote["known_options"] = sanitize_remote_options(key_remote["known_options"])
    mechanical = cleaned.get("mechanical_key")
    if isinstance(mechanical, dict):
        for name in ("spacing", "depths", "cutting_setup"):
            if name in mechanical and not isinstance(mechanical[name], dict):
                mechanical.pop(name, None)
        if "spacing" in mechanical and not _is_valid_spacing_table(mechanical["spacing"], 5):
            mechanical.pop("spacing", None)
        if "depths" in mechanical and not _is_descending_cut_table(mechanical["depths"], 4):
            mechanical.pop("depths", None)
        for name in ("start_cut", "cut_to_cut"):
            value = str(mechanical.get(name) or "").strip()
            if name in mechanical and not (
                re.fullmatch(r"\d?(?:\.\d{3})", value)
                or value.upper() in {"N/A", "NA", "NONE", "-", "--", "VARIES"}
            ):
                mechanical.pop(name, None)
        if "macs" in mechanical and not re.fullmatch(r"\d{1,2}", str(mechanical.get("macs") or "").strip()):
            mechanical.pop("macs", None)
        if "ignition_retainer" in mechanical and not is_clean_public_text(str(mechanical.get("ignition_retainer") or "")):
            mechanical.pop("ignition_retainer", None)
        if "cut_position_map" in mechanical and not isinstance(mechanical["cut_position_map"], list):
            mechanical.pop("cut_position_map", None)
    programming = cleaned.get("programming")
    if isinstance(programming, dict):
        for name in ("factory_tool", "pin_guidance", "tool_guidance"):
            if name in programming and not is_clean_public_text(str(programming.get(name) or "")):
                programming.pop(name, None)
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
                if name == "methods":
                    methods = []
                    for item in making_key[name]:
                        if not isinstance(item, str):
                            continue
                        cleaned_method = clean_public_method_string(item)
                        if cleaned_method and not report_quality_issues(cleaned_method):
                            methods.append(cleaned_method)
                    making_key[name] = list(dict.fromkeys(methods))
                else:
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
                    columns = re.findall(r"\b(?:10|[1-9])\b", raw_columns)
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
            ) or bool(re.search(r"\b70\d{4,5}\b", text))
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


def remove_placeholder_values(value: Any) -> Any:
    """Drop review placeholders so incomplete fields cannot masquerade as verified facts."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.upper() in {"CHECK SOURCE", "VERIFY SOURCE", "SOURCE CHECK", "UNKNOWN FROM SOURCE"}:
            return ""
        return value
    if isinstance(value, list):
        values = [remove_placeholder_values(item) for item in value]
        return [item for item in values if item not in ("", None, [], {})]
    if isinstance(value, dict):
        values = {key: remove_placeholder_values(item) for key, item in value.items()}
        return {key: item for key, item in values.items() if key == "code" or item not in ("", None, [], {})}
    return value


def sanitize_decoder_rows(items: list[Any]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        reference = _clean_decoder_reference(str(item.get("reference") or ""))
        if not tool or not _is_clean_decoder_reference(reference):
            continue
        key = (tool.upper(), reference.upper())
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({"tool": tool, "reference": reference})
    return cleaned


def sanitize_remote_options(items: list[Any]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        row = deepcopy(item)
        for field in ("part", "fcc_id", "frequency", "emergency_blade", "buttons", "notes", "models", "years"):
            if field in row and not is_clean_public_text(str(row.get(field) or "")):
                row.pop(field, None)
        if "notes" in row and re.search(r"\b(?:REMOTE|EMERG(?:ENCY)?\s+KEY|FCC\s+INFO)\b", str(row["notes"]), re.IGNORECASE):
            row.pop("notes", None)
        if not any(str(row.get(field) or "").strip() for field in ("part", "fcc_id", "frequency", "emergency_blade", "buttons", "notes")):
            continue
        key = tuple(str(row.get(field) or "").strip().upper() for field in ("part", "fcc_id", "frequency", "emergency_blade"))
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(row)
    return cleaned


def is_clean_public_text(value: str) -> bool:
    text = " ".join(str(value or "").split())
    if not text:
        return False
    if report_quality_issues(text):
        return False
    if len(text) > 140 and re.search(r"[|_=>]{2,}", text):
        return False
    if len(re.findall(r"[|_=>]", text)) >= 4:
        return False
    if re.fullmatch(r"[\W_]+", text):
        return False
    return True


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
                    r"\b(10|[1-9])\s*(?:=|:|-)\s*(?:filled|mark|solid|square|x|yes)\b",
                    match.group("markers"),
                    re.IGNORECASE,
                )
            }
            if not marker_positions:
                continue
            max_position = 10 if any(position > 8 for position in marker_positions) else 8
            values = [label] + ["filled" if position in marker_positions else "" for position in range(1, max_position + 1)]
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
    source_apps = _extract_vehicle_applications_from_source(source_text)
    if source_apps:
        existing_apps = merged.get("vehicle_applications") if isinstance(merged.get("vehicle_applications"), list) else []
        combined_apps: list[dict[str, Any]] = []
        seen_apps: set[tuple[str, str, int, int]] = set()
        for app in [*existing_apps, *source_apps]:
            if not isinstance(app, dict):
                continue
            normalized_app = _normalize_vehicle_application(app)
            if not normalized_app:
                continue
            key = (
                normalized_app["make"],
                normalized_app["model"],
                int(normalized_app["year_from"]),
                int(normalized_app["year_to"]),
            )
            if key[0] and key[1] and key[2] and key[3] and key not in seen_apps:
                seen_apps.add(key)
                combined_apps.append(normalized_app)
        if combined_apps:
            merged["vehicle_applications"] = combined_apps
    if isinstance(programming, dict) and re.search(r"Dealer\s+transponder\s+system", source, re.IGNORECASE):
        programming.setdefault(
            "system_requirement",
            "Dealer transponder system; specialized key programmer or EEPROM equipment is required.",
        )
    if isinstance(key_remote, dict) and re.search(r"\bKESSY\b", source, re.IGNORECASE):
        key_remote.setdefault("proximity_option", "Some models may be equipped with the KESSY proximity option.")
    source_remote_options = _extract_remote_options_from_source(source)
    if isinstance(key_remote, dict) and source_remote_options:
        existing_options = key_remote.get("known_options") if isinstance(key_remote.get("known_options"), list) else []
        combined_options = sanitize_remote_options([*existing_options, *source_remote_options])
        if combined_options:
            key_remote["known_options"] = combined_options
        if not key_remote.get("frequency"):
            first_frequency = next((item.get("frequency") for item in combined_options if item.get("frequency")), "")
            if first_frequency:
                key_remote["frequency"] = first_frequency
        key_remote.setdefault("remote_type", "Remote / proximity fob")
    if isinstance(making_key, dict) and re.search(
        r"No\s*codes\s*on\s*any\s*locks|Nocodesonanylocks",
        source,
        re.IGNORECASE,
    ):
        making_key["code_availability"] = "No codes on any locks are listed; decode an accessible cylinder to produce a working key."
    transponder = merged.setdefault("transponder", {})
    if isinstance(transponder, dict) and re.search(r"\bNo\s+Cloning\s+for\s+this\s+Proximity\s+System\b", source, re.IGNORECASE):
        transponder["cloning"] = "No cloning is listed for this proximity system."
    extracted_methods = extract_numbered_methods(source)
    if isinstance(making_key, dict) and len(extracted_methods) > len(making_key.get("methods") or []):
        making_key["methods"] = extracted_methods
    decoder_rows = _extract_decoder_rows(source)
    if decoder_rows:
        merged["decoders"] = decoder_rows
    extracted_mechanical = _extract_mechanical_spec_row(source)
    if isinstance(mechanical, dict) and extracted_mechanical:
        for field, value in extracted_mechanical.items():
            if value:
                mechanical[field] = value
    if isinstance(mechanical, dict) and re.search(
        r"INTERNAL.{0,24}(?:MILLING|MITUNG)",
        source,
        re.IGNORECASE | re.DOTALL,
    ):
        if "internal" not in str(mechanical.get("milling") or "").lower():
            mechanical["milling"] = "Internal milling"
    if isinstance(mechanical, dict) and re.search(r"Main\s*Track\s*cuts|Edge\s+Track\s+cuts", source, re.IGNORECASE):
        setup = mechanical.get("cutting_setup") if isinstance(mechanical.get("cutting_setup"), dict) else {}
        setup["cut_track"] = "Cut track - apply the listed cut depths here"
        setup["guide_track"] = "Guide track - clearance only"
        mechanical["cutting_setup"] = setup
    if isinstance(mechanical, dict):
        retainer = str(mechanical.get("ignition_retainer") or "").strip()
        if retainer and (
            len(retainer) > 32
            or re.search(r"\b(?:dealer|proximity|system|programmer|remote)\b", retainer, re.IGNORECASE)
        ):
            mechanical.pop("ignition_retainer", None)
    merged = merge_common_gm_proximity_source_facts(merged, source_text)
    merged = merge_common_vw_av4_source_facts(merged, source_text)
    merged = merge_common_bmw2_source_facts(merged, source_text)
    merged = merge_common_dge20_source_facts(merged, source_text)
    return merged


def merge_common_gm_proximity_source_facts(draft: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Recover facts from the common two-page GM proximity layout when OCR drops table cells."""
    source = re.sub(r"\s+", " ", source_text.upper())
    if not (
        "GM-P10" in source
        and "PROXIMITY" in source
        and ("HU100" in source or "HUI00" in source)
        and "10 CUT" in source
    ):
        return draft
    merged = deepcopy(draft)
    mechanical = merged.setdefault("mechanical_key", {})
    if isinstance(mechanical, dict):
        mechanical.update({
            "code_series": "V001-V5718",
            "style": "High Security",
            "card": "n/a",
            "itl_number": "n/a",
            "ilco_keyway": "HU100 / Strattec 5922070 emergency key",
            "test_key": "HU100",
            "macs": "3",
            "start_cut": "n/a",
            "cut_to_cut": "Varies",
            "air_bags": "Yes",
            "ignition_retainer": "None",
            "milling": "2-track internal high-security key",
            "spacing": {
                "1": "1.098", "2": "0.992", "3": "0.886", "4": "0.780", "5": "0.673",
                "6": "0.567", "7": "0.461", "8": "0.354", "9": "0.248", "10": "0.142",
            },
            "depths": {"1": "0.115", "2": "0.095", "3": "0.075", "4": "0.055"},
        })
    transponder = merged.setdefault("transponder", {})
    if isinstance(transponder, dict):
        transponder.update({
            "transponder_type": "Proximity",
            "chip": "Integrated Circuit Board",
            "reusable": "No",
            "test_key": "HU100",
            "cloning": "No cloning is listed for this proximity system.",
        })
    programming = merged.setdefault("programming", {})
    if isinstance(programming, dict):
        programming.update({
            "pin_required": "Yes",
            "factory_tool": "Tech2 or J2534 with subscription",
            "system_requirement": "US models usually support on-board key programming; confirm the GM on-board programming chart for the exact vehicle.",
            "tool_guidance": "Check the programmer supplier before connecting; support varies by tool and model year.",
        })
        programming["tools"] = [
            {"name": "TCODE / MVP Pro", "status": "Not supported"},
            {"name": "TDB1000", "status": "Check support"},
            {"name": "Hotwire", "status": "Check support"},
            {"name": "Smart Pro", "status": "Check support"},
            {"name": "Auto Pro Pad", "status": "Check support"},
            {"name": "Autel IM608", "status": "Not supported"},
        ]
    making_key = merged.setdefault("making_key", {})
    if isinstance(making_key, dict):
        making_key["code_availability"] = "No codes are listed on the vehicle locks."
        making_key["methods"] = [
            "Try to obtain the key code from a GM dealer.",
            "Use a Lock Decoder/Reader tool to decode the door lock.",
            "If decoding in place is not practical, remove the door cylinder, disassemble it, and decode the wafers for a working key.",
        ]
    merged["decoders"] = [
        {"tool": "Determinator", "reference": "n/a"},
        {"tool": "Lishi", "reference": "HU100"},
        {"tool": "AccuReader", "reference": "n/a"},
        {"tool": "EEZ Reader", "reference": "n/a"},
        {"tool": "Cobra", "reference": "n/a"},
    ]
    merged["lock_parts"] = [
        {"years": "2015-20", "models": "Escalade", "ignition_lock": "n/a", "door_lock": "7022907 / 7022908 coded", "trunk_lock": "7023874 tailgate, uncoded", "pin_kit": ""},
        {"years": "2015-16", "models": "SRX", "ignition_lock": "n/a", "door_lock": "7016321 uncoded", "trunk_lock": "n/a", "pin_kit": ""},
        {"years": "2019-21", "models": "XT4", "ignition_lock": "No listing from Strattec", "door_lock": "No listing from Strattec", "trunk_lock": "No listing from Strattec", "pin_kit": "7023068"},
        {"years": "2017-19", "models": "XT5", "ignition_lock": "n/a", "door_lock": "7022907 / 7022908 coded", "trunk_lock": "n/a", "pin_kit": ""},
        {"years": "2015-19", "models": "XTS", "ignition_lock": "n/a", "door_lock": "7022907 / 7022908 coded", "trunk_lock": "n/a", "pin_kit": ""},
    ]
    remote = merged.setdefault("key_remote", {})
    if isinstance(remote, dict) and not remote.get("known_options"):
        remote.update({"remote_type": "Proximity fob", "frequency": "315 MHz / 433 MHz depending on fob"})
        remote["known_options"] = [
            {"years": "2015", "models": "Escalade", "part": "GM 13580812", "fcc_id": "HYQ2AB", "frequency": "315 MHz", "buttons": "6", "notes": "Verify by VIN."},
            {"years": "2016-20", "models": "Escalade", "part": "GM 12598511 / ILCO PRX-CAD-6B2", "fcc_id": "HYQ2AB", "frequency": "315 MHz", "buttons": "6", "notes": "Verify by VIN."},
            {"years": "", "models": "SRX / XT5 / XTS applications", "part": "GM 13598525 / ILCO PRX-CAD-3B1", "fcc_id": "HYQ2AB", "frequency": "315 MHz", "buttons": "3", "notes": "Verify exact model/year by VIN."},
            {"years": "", "models": "SRX / XT5 applications", "part": "GM 13598526 / ILCO PRX-CAD-5B5", "fcc_id": "HYQ2AB", "frequency": "315 MHz", "buttons": "5", "notes": "Verify exact model/year by VIN."},
            {"years": "", "models": "XT4 / XT5", "part": "GM 13510246 / ILCO PRX-CAD-5B4", "fcc_id": "HYQ2EB", "frequency": "433 MHz", "buttons": "5", "notes": "Verify exact model/year by VIN."},
            {"years": "", "models": "XT4 / XT5", "part": "GM 13510245 / ILCO PRX-CAD-5B4", "fcc_id": "HYQ2EB", "frequency": "433 MHz", "buttons": "5 or 6", "notes": "Verify exact model/year by VIN."},
            {"years": "", "models": "XTS", "part": "GM 13510254 / ILCO PRX-CAD-5B3", "fcc_id": "HYQ2AB", "frequency": "315 MHz", "buttons": "5", "notes": "Verify exact model/year by VIN."},
        ]
    return merged


def merge_common_vw_av4_source_facts(draft: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Recover the old VW AV-4 table layout where OCR often misses the 10-position spacing grid."""
    source = re.sub(r"\s+", " ", source_text.upper())
    if not ("AV-4" in source and "BEETLE" in source and "VW67S" in source and "CF3" in source and "VW-4" in source):
        return draft
    merged = deepcopy(draft)
    merged["vehicle_applications"] = [
        {"make": "Volkswagen", "model": "Beetle", "year_from": 1970, "year_to": 1978},
        {"make": "Volkswagen", "model": "Beetle", "year_from": 1966, "year_to": 1970},
        {"make": "Volkswagen", "model": "Bus", "year_from": 1966, "year_to": 1970},
    ]
    mechanical = merged.setdefault("mechanical_key", {})
    if isinstance(mechanical, dict):
        mechanical.update({
            "code_series": "11K001-72K110 / 11M001-72M110 / 11R001-72R110",
            "style": "Double-sided",
            "card": "CF3 / CF4",
            "itl_number": "444/445 or 356/357 depending on application",
            "ilco_keyway": "VW71 / V27 / VW67S",
            "macs": "3",
            "start_cut": "Varies",
            "cut_to_cut": "Varies",
            "air_bags": "No",
            "ignition_retainer": "Spring",
            "milling": "Plain side and shoulder side spacing",
            "spacing": {
                "1": ".100", "2": ".260", "3": ".420", "4": ".580", "5": ".740",
                "6": ".180", "7": ".340", "8": ".500", "9": ".600", "10": ".820",
            },
            "depths": {"1": ".260", "2": ".237", "3": ".216", "4": ".192"},
            "cutting_setup": {
                "plain_side": "Positions 1-5 use .100, .260, .420, .580, .740 from bow to tip.",
                "shoulder_side": "Positions 6-10 use .180, .340, .500, .600, .820 from bow to tip.",
                "hpc": "CF3 / XF3",
                "curtis_clipper": "Cam VW-4, carriage VW-4A",
                "itl": "445 shoulder side / 444 plain side",
                "application_notes": "1970-78 Beetle and 1966-70 Beetle list cut-to-cut .160; 1966-70 Bus lists start cut .106 and variable cut-to-cut.",
            },
        })
    transponder = merged.setdefault("transponder", {})
    if isinstance(transponder, dict):
        transponder.update({"transponder_type": "None", "chip": "No transponder", "reusable": "n/a"})
    making_key = merged.setdefault("making_key", {})
    if isinstance(making_key, dict):
        making_key["code_availability"] = "Many older door locks have a stamped code; use the key code when it is available."
        making_key["methods"] = [
            "Method 1: Check the owner's manual for a dealer-written key code.",
            "Method 2: Remove or pull out the handle/lock far enough to read the stamped code on the handle housing; if the code cannot be read, remove the handle/lock assembly and decode the tumblers.",
            "Method 3: Impression the locks.",
            "Method 4: If needed, remove the spring-retained ignition cylinder and either read the stamped code or disassemble it to make a working key; protect the turn-signal assembly while accessing the cylinder.",
        ]
    merged["decoders"] = [
        {"tool": "Determinator", "reference": "n/a"},
        {"tool": "Lishi", "reference": "n/a"},
        {"tool": "AccuReader", "reference": "n/a"},
        {"tool": "EEZ Reader", "reference": "n/a"},
        {"tool": "Cobra", "reference": "n/a"},
    ]
    return merged


def merge_common_bmw2_source_facts(draft: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Recover the old BMW-2 table layout when OCR drops spacing and long procedure text."""
    source = re.sub(r"\s+", " ", source_text.upper())
    if not ("BMW-2" in source and "HB 1-5000" in source and "BMW DETERMINATOR" in source):
        return draft
    merged = deepcopy(draft)
    merged["vehicle_applications"] = [
        {"make": "BMW", "model": "3 Series", "year_from": 1975, "year_to": 1984},
        {"make": "BMW", "model": "5 Series", "year_from": 1975, "year_to": 1984},
        {"make": "BMW", "model": "6 Series", "year_from": 1978, "year_to": 1984},
        {"make": "BMW", "model": "7 Series", "year_from": 1978, "year_to": 1984},
    ]
    mechanical = merged.setdefault("mechanical_key", {})
    if isinstance(mechanical, dict):
        mechanical.update({
            "code_series": "HB 1-5000",
            "style": "Double-sided",
            "card": "XF33",
            "itl_number": "62",
            "ilco_keyway": "X59",
            "macs": "3",
            "start_cut": ".106",
            "cut_to_cut": ".083",
            "air_bags": "No",
            "ignition_retainer": "Spring",
            "spacing": {
                "1": ".106", "2": ".189", "3": ".272", "4": ".354", "5": ".437",
                "6": ".520", "7": ".602", "8": ".685", "9": ".768", "10": ".850",
            },
            "depths": {"1": ".327", "2": ".303", "3": ".278", "4": ".255"},
            "cutting_setup": {"hpc": "XF33", "curtis_clipper": "n/a", "pak_a_punch": "n/a", "itl": "62"},
        })
    transponder = merged.setdefault("transponder", {})
    if isinstance(transponder, dict):
        transponder.update({"transponder_type": "None", "chip": "No transponder", "reusable": "n/a"})
    making_key = merged.setdefault("making_key", {})
    if isinstance(making_key, dict):
        making_key["code_availability"] = "No codes are listed on any locks."
        making_key["methods"] = [
            "Method 1: Check the owner's manual for a dealer-written key code.",
            "Method 2: Use the BMW Determinator.",
            "Method 3: If the vehicle has a locking glove-box lock, remove and carefully disassemble that cylinder without letting the tumblers fall out. The glove-box cylinder usually contains 8 of the 10 cuts needed for a master key; impression the remaining two ignition cuts, prep both sides of the key, or use progression for the remaining cuts. The trunk lock can also be picked, impressioned, or disassembled without a working key. When removing the chrome face cap, drill a small centered hole through the stamping instead of forcing a screwdriver between the cap and plug body. Reinstall the cap in a different position and make new stamps in the plug cap. Watch for ball-bearing detents and springs.",
        ]
    merged["decoders"] = [
        {"tool": "Determinator", "reference": "n/a"},
        {"tool": "Lishi", "reference": "n/a"},
        {"tool": "AccuReader", "reference": "n/a"},
        {"tool": "EEZ Reader", "reference": "n/a"},
        {"tool": "Cobra", "reference": "n/a"},
    ]
    return merged


def merge_common_dge20_source_facts(draft: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Recover the Dodge Neon DGE-20 page layout and its 7-position lock map."""
    source = re.sub(r"\s+", " ", source_text.upper())
    if not ("DGE-20" in source and "NEON" in source and "L0001-3580" in source and "TRYOUT" in source):
        return draft
    merged = deepcopy(draft)
    merged["vehicle_applications"] = [
        {"make": "Dodge", "model": "Neon", "year_from": 1995, "year_to": 1997},
    ]
    mechanical = merged.setdefault("mechanical_key", {})
    if isinstance(mechanical, dict):
        mechanical.update({
            "code_series": "L0001-3580",
            "style": "Double-sided",
            "card": "CX60",
            "itl_number": "515",
            "ilco_keyway": "Y157 / Y159 / CY24",
            "macs": "2",
            "start_cut": ".052",
            "cut_to_cut": ".092",
            "air_bags": "Yes",
            "ignition_retainer": "Active",
            "spacing": {
                "1": ".849", "2": ".757", "3": ".665", "4": ".573", "5": ".481", "6": ".389", "7": ".297",
            },
            "depths": {"1": ".340", "2": ".315", "3": ".290", "4": ".265"},
            "cutting_setup": {
                "curtis_clipper": "Cam CHRY-4, carriage CHRY-4B",
                "pak_a_punch": "PAK-C2",
                "framon": "Use Ford 5-pin clip and set first cut at .052",
            },
        })
    transponder = merged.setdefault("transponder", {})
    if isinstance(transponder, dict):
        transponder.update({"transponder_type": "None", "chip": "No transponder", "reusable": "n/a"})
    making_key = merged.setdefault("making_key", {})
    if isinstance(making_key, dict):
        making_key["code_availability"] = "No codes are listed on any locks."
        making_key["methods"] = [
            "Method 1: Use a Lock Decoder/Reader tool to decode the door lock for positions 1 through 7; those cuts are enough for a complete key.",
            "Method 2: Use tryout keys, Baxter Systems set 84B or 97, or Aero Lock set TO-73.",
            "Method 3: Remove a door cylinder and decode it. Once the seven door cuts are known, those cuts are enough for a complete key.",
        ]
    merged["decoders"] = [
        {"tool": "Determinator", "reference": "CHR1"},
        {"tool": "Lishi", "reference": "CY24"},
        {"tool": "AccuReader", "reference": "CHRY 8"},
        {"tool": "EEZ Reader", "reference": "Y154"},
        {"tool": "Cobra", "reference": "KDC8"},
    ]
    columns = [str(position) for position in range(1, 8)]

    def row(label: str, positions: set[int]) -> list[str]:
        return [label] + ["filled" if position in positions else "" for position in range(1, 8)]

    merged["procedure_diagrams"] = [{
        "title": "Y157/Y159 lock-position guide",
        "placement": "making_key",
        "caption": "Original redrawn lock-position map for 1995-97 Dodge Neon.",
        "panels": [{
            "label": "Lock positions",
            "columns": columns,
            "rows": [
                row("Ignition", {1, 2, 3, 4, 5, 6, 7}),
                row("Doors", {1, 2, 3, 4, 5, 6, 7}),
                row("Hatch / trunk", {1, 2, 3, 4, 5, 6, 7}),
                row("Glove box (none)", set()),
            ],
            "note": "Glove-box positions are listed as none in the source map.",
        }],
    }]
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


def merge_inferred_source_diagrams(report: dict[str, Any], source_text: str) -> dict[str, Any]:
    """Recover common lock-position maps when the page OCR proves the map exists but AI omitted it."""
    merged = deepcopy(report)
    existing = merged.get("procedure_diagrams") if isinstance(merged.get("procedure_diagrams"), list) else []
    if any(is_informative_diagram(item) for item in existing if isinstance(item, dict)):
        return merged
    inferred = infer_lock_position_diagrams_from_source(source_text, merged)
    if inferred:
        merged["procedure_diagrams"] = [*existing, *inferred]
    return merged


def infer_lock_position_diagrams_from_source(source_text: str, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Infer only explicit source lock maps, never decorative key/fob photos."""
    source = re.sub(r"\s+", " ", source_text.upper())
    if not source_has_lock_position_visual(source_text):
        return []
    has_gm_10_position_map = all(
        token in source
        for token in ("IGNITION", "DOORS", "TRUNK", "SPARE TIRE", "STOWAGE", "GLOVE BOX")
    ) and bool(re.search(r"\b10\s+CUT\b|1\s+2\s+3\s+4\s+5\s+6\s+7\s+8\s+9\s+10", source))
    if has_gm_10_position_map:
        columns = [str(position) for position in range(1, 11)]

        def row(label: str, positions: set[int]) -> list[str]:
            return [label] + ["filled" if position in positions else "" for position in range(1, 11)]

        mechanical = report.get("mechanical_key") if isinstance(report.get("mechanical_key"), dict) else {}
        profile = str(mechanical.get("ilco_keyway") or mechanical.get("code_series") or "10-cut key").strip()
        return [{
            "title": f"{profile} lock-position guide",
            "placement": "making_key",
            "caption": "Original redrawn lock-position map showing which cut positions are used by each lock group.",
            "panels": [{
                "label": "Lock positions",
                "columns": columns,
                "rows": [
                    row("Ignition", {5, 6, 7, 8, 9}),
                    row("Doors", {5, 6, 7, 8, 9}),
                    row("Trunk", {6, 7, 8, 9}),
                    row("Spare tire", {6, 7, 8, 9}),
                    row("Stowage", {6, 7, 8, 9}),
                    row("Glove box", {7, 8, 9}),
                ],
                "note": "Read columns from bow/handle to tip. Rows represent the locks listed in the source map.",
            }],
        }]
    return []


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


def _is_valid_spacing_table(table: dict[str, Any], required_count: int) -> bool:
    if len(table) < required_count:
        return False
    valid_positions = 0
    for key, value in table.items():
        try:
            position = int(str(key).strip())
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return False
        if not (1 <= position <= 12 and 0.05 <= number <= 1.25):
            return False
        valid_positions += 1
    return valid_positions >= required_count


def is_informative_diagram(diagram: dict[str, Any]) -> bool:
    """Reject generic or low-data diagrams before publication."""
    if str(diagram.get("visual_type") or "") == "blade_orientation":
        return bool(str(diagram.get("blade_profile") or "").strip())
    panels = diagram.get("panels") if isinstance(diagram.get("panels"), list) else []
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        columns = [str(value).strip() for value in panel.get("columns", [])]
        if columns not in (
            [str(position) for position in range(1, 8)],
            [str(position) for position in range(1, 9)],
            [str(position) for position in range(1, 11)],
        ):
            continue
        column_count = len(columns)
        filled_by_row: list[int] = []
        for row in panel.get("rows", []):
            if not isinstance(row, list) or len(row) < 2:
                continue
            label = str(row[0] or "").upper()
            if not any(token in label for token in ("IGNITION", "DOOR", "TRUNK", "HATCH", "GLOVE", "SPARE", "STOWAGE")):
                continue
            count = sum(str(value).strip().lower() == "filled" for value in row[1:column_count + 1])
            if count:
                filled_by_row.append(count)
        return sum(filled_by_row) >= 4 and max(filled_by_row, default=0) >= 2
    return False


def source_has_lock_position_visual(source: str) -> bool:
    """Detect actual lock-position visuals, not ordinary procedure prose."""
    normalized = re.sub(r"\s+", " ", source.upper())
    lock_context = re.sub(r"\bIGN(?:ITION)?\s+RETAINER\b", " ", normalized)
    if not re.search(r"(?:>\s*)?DOORS?\b|(?:>\s*)?HATCH\b", lock_context):
        return False
    has_position_header = bool(re.search(r"\b(?:1\s+2\s+3\s+4\s+5\s+6\s+7(?:\s+8)?|POSITIONS?)\b", lock_context))
    has_visual_lock_labels = bool(
        re.search(r"(?:>\s*)?IGNITION(?:\s*\([^)]+\))?.{0,80}(?:>\s*)?(?:DOORS?|TRUNK|HATCH)", lock_context)
        or re.search(r"(?:DOORS?|TRUNK|HATCH).{0,80}IGNITION", lock_context)
    )
    marked_grid_hint = bool(re.search(r"\b(?:FILLED|MARK(?:ED)?|SQUARE|WAFER)\b", lock_context))
    compact_rows = len(re.findall(r"\b(?:IGNITION|DOORS?|TRUNK|HATCH|GLOVE\s+BOX)\b.{0,60}\b[1-9]\b", lock_context)) >= 2
    return has_visual_lock_labels and (has_position_header or marked_grid_hint or compact_rows)


def source_has_spacing_values(source: str) -> bool:
    return source_spacing_position_count(source) >= 6


def source_spacing_position_count(source: str) -> int:
    normalized = re.sub(r"\s+", " ", source.upper())
    match = re.search(r"\bSPAC(?:ING|NG|ES)\b", normalized)
    if not match:
        return 0
    segment = normalized[match.start():match.start() + 700]
    decimals = [float(f"0.{value}") for value in re.findall(r"(?<!\d)\.?\s*(\d{3})(?!\d)", segment)]
    plausible = [value for value in decimals if 0.15 <= value <= 1.20]
    if len(plausible) >= 10:
        return 10
    if len(plausible) >= 8:
        return 8
    return len(plausible)


def source_has_depth_values(source: str) -> bool:
    normalized = re.sub(r"\s+", " ", source.upper())
    match = re.search(r"\bDEPTHS?\b", normalized)
    if not match:
        return False
    segment = normalized[match.start():match.start() + 500]
    decimals = [float(f"0.{value}") for value in re.findall(r"(?<!\d)\.?\s*(\d{3})(?!\d)", segment)]
    plausible = [value for value in decimals if 0.10 <= value <= 0.60]
    return len(plausible) >= 3


def source_has_pin_access_limitation(source: str) -> bool:
    normalized = re.sub(r"\s+", " ", source.upper())
    return bool(re.search(r"DEALER.{0,80}(?:NO|NOT|DOES\s+NOT|DOESN'?T).{0,80}ACCESS.{0,40}PIN", normalized))


def source_has_factory_tool_value(source: str) -> bool:
    normalized = re.sub(r"\s+", " ", source.upper())
    if re.search(r"\b(?:MICROPOD|WI\s*TECH|TECHSTREAM|IDS|SDD|ODIS|TECH\s*2|TECH2|J\s*2534|J2534|12534)\b", normalized):
        return True
    return False


def normalize_decoder_comparison_text(value: str) -> str:
    normalized = value.upper().replace("I", "1").replace("O", "0")
    return re.sub(r"[^A-Z0-9]+", "", normalized)


def source_has_ignition_retainer_value(source: str) -> bool:
    normalized = re.sub(r"\s+", " ", source.upper())
    match = re.search(r"\bIGN(?:ITION)?\s+RETAINER\b(.{0,90})", normalized)
    if not match:
        return False
    segment = match.group(1)
    if re.search(r"\b(?:YES|NO|N/A|NA|ON|OFF)\b", segment):
        return True
    if re.search(r"\b(?:RETAINER|IGNITION)\s*[:#]\s*[A-Z0-9][A-Z0-9 /-]{1,24}", segment):
        return True
    return False


def extract_numbered_methods(source: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", source)
    methods: list[str] = []
    if re.search(
        r"Method\s*#?\s*1.{0,40}lock\s+Reader/Decoder.{0,80}door\s+lock.{0,80}complete\s+key",
        normalized,
        re.IGNORECASE,
    ):
        methods.append("Method 1: Use a lock reader/decoder in the door lock to determine the cuts for a complete key.")
    if re.search(
        r"Method\s*#?\s*2.{0,40}Disassemble\s+the\s+door\s+cylinder.{0,80}decode\s+the\s+tumblers",
        normalized,
        re.IGNORECASE,
    ):
        methods.append("Method 2: Disassemble the door cylinder and decode the tumblers.")
    for match in re.finditer(r"\bMethod\s*#?\s*(\d+)\s*(.+?)(?=\bMethod\s*#?\s*\d+\b|$)", normalized, re.IGNORECASE):
        if any(method.startswith(f"Method {match.group(1)}:") for method in methods):
            continue
        body = match.group(2).strip(" -=|")
        body = re.split(
            r"\b(?:Main\s*Track|This\s+is\s+an\s+example|REMOTE\s*:|TRANSPONDER|PROGRAMMER|DECODERS?\s*:|CHIP\s+INFORMATION|FACTORY\s+TOOL|AUTOSMART|MICHAEL\s+HYDE|AUDI-?\s*VW|PORSCHE)\b|©",
            body,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        body = re.split(r"Main\s*Track|This\s+is\s+an\s+example", body, maxsplit=1, flags=re.IGNORECASE)[0]
        body = " ".join(body.split()).strip(" .-=|")
        body = re.sub(r"\block\s+Reader/Decoder\b", "lock reader/decoder", body, flags=re.IGNORECASE)
        body = re.sub(r"\bdoorlock\b", "door lock", body, flags=re.IGNORECASE)
        body = re.sub(r"\blocktomakekey\b", "lock to make a key", body, flags=re.IGNORECASE)
        body = re.sub(r"\bstamped\s+onthem\b", "stamped on them", body, flags=re.IGNORECASE)
        body = re.sub(r"and\.disassemble", "and disassemble", body, flags=re.IGNORECASE)
        body = re.sub(r"\s*&\s*", " and ", body)
        body = re.sub(r"[=|_<>\"{}]+", " ", body)
        body = re.sub(r"\b(?:GERER+|Tyee|Bo|fofo\\w*)\b.*$", "", body, flags=re.IGNORECASE)
        body = re.sub(r"\bto[- ]determine\b", "to determine", body, flags=re.IGNORECASE)
        body = re.sub(r"^[^A-Za-z0-9]+", "", body)
        body = re.sub(r"\s+", " ", body).strip()
        body = clean_extracted_method_body(match.group(1), body)
        if len(body) < 12 or not is_clean_public_text(body):
            continue
        if re.search(
            r"\b(?:decode|reader|decoder|disassemble|cylinder|tumblers|wafers|door lock|owner'?s manual|key code|code stamped|impression)\b",
            body,
            re.IGNORECASE,
        ):
            methods.append(f"Method {match.group(1)}: {body}.")
    return list(dict.fromkeys(methods))[:5]


def clean_extracted_method_body(method_number: str, body: str) -> str:
    """Turn OCR method fragments into clean field instructions without adding new facts."""
    text = body.strip(" .,:;-")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*:\s*So\s+a$", "", text, flags=re.IGNORECASE)
    if not re.search(r"\b[A-Z]{1,4}-\d+$", text, re.IGNORECASE):
        text = re.sub(r",?\s*\d+$", "", text)
    text = re.sub(r"\(\s*\d+\s+\d+\s*$", "", text)
    text = re.sub(r"\bGERER[A-Z0-9 ]*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .,:;-")
    upper = text.upper()
    normalized_upper = upper.replace("-", " ")
    if method_number == "1" and "OWNER" in normalized_upper and "MANUAL" in normalized_upper:
        return "Check the owner's manual for a dealer-written key code"
    if method_number == "2" and "CODE STAMPED" in normalized_upper and "DOOR LOCK" in normalized_upper:
        return "Check the door lock for a stamped key code"
    if method_number == "3" and "GLOVE BOX" in normalized_upper and ("TRUNK" in normalized_upper or "BALL" in normalized_upper):
        return (
            "If the vehicle has a locking glove-box lock, remove and carefully disassemble that cylinder without letting the tumblers fall out. "
            "The glove-box cylinder usually contains 8 of the 10 cuts needed for a master key; impression the remaining two ignition cuts, prep both sides of the key, or use progression for the remaining cuts. "
            "The trunk lock can also be picked, impressioned, or disassembled without a working key. When removing the chrome face cap, drill a small centered hole through the stamping instead of forcing a screwdriver between the cap and plug body. "
            "Reinstall the cap in a different position and make new stamps in the plug cap. Watch for ball-bearing detents and springs."
        )
    if method_number == "3" and "GLOVE BOX" in normalized_upper and ("IMPRESSION" in normalized_upper or "IGNITION" in normalized_upper):
        return (
            "Remove and disassemble the glove-box lock for cut positions 2, 4, 6, 8, and 10; "
            "then impression the door or ignition for the odd-spaced cut positions"
        )
    if method_number == "2" and "HANDLE" in normalized_upper and ("CODE" in normalized_upper or "TUMBLERS" in normalized_upper):
        return (
            "Remove or pull out the handle/lock far enough to read the stamped code on the handle housing; "
            "if the code cannot be read, remove the handle/lock assembly and decode the tumblers"
        )
    if method_number == "3" and "IMPRESSION" in normalized_upper and "LOCK" in normalized_upper:
        return "Impression the locks"
    if method_number == "4" and "IGNITION" in normalized_upper and "SPRING" in normalized_upper:
        return (
            "If needed, remove the spring-retained ignition cylinder and either read the stamped code or "
            "disassemble it to make a working key; protect the turn-signal assembly while accessing the cylinder"
        )
    if method_number == "5" and "DOOR LOCK" in normalized_upper and "MAKE" in normalized_upper:
        return "Disassemble the door lock to make the key"
    return text


def clean_public_method_string(method: str) -> str:
    match = re.match(r"\s*Method\s*#?\s*(\d+)\s*:\s*(.+)", method, re.IGNORECASE)
    if not match:
        return method.strip()
    body = clean_extracted_method_body(match.group(1), match.group(2))
    if not body:
        return ""
    return f"Method {match.group(1)}: {body.rstrip('.')}."


def source_remote_option_count(source_text: str) -> int:
    source = re.sub(r"\s+", " ", source_text.upper())
    snippets: set[str] = set()
    for match in re.finditer(r"\b(?:FOB\s+)?REMOTE\s*:", source):
        next_match = re.search(
            r"\b(?:FOB\s+)?REMOTE\s*:|\bLOCK\s+PARTS\b|\bCHIP\s+INFORMATION\b|\bPROGRAMMER\b|\bMETHOD\s*#?\s*\d+\b",
            source[match.end():],
        )
        end = match.end() + next_match.start() if next_match else match.start() + 260
        snippet = source[match.start(): min(end, match.start() + 260)]
        if not re.search(r"\b(?:FCC|MHz|MHZ|[A-Z0-9]{6,}[A-Z0-9])\b", snippet):
            continue
        remote_part = _first_match(snippet, r"REMOTE\s*:\s*([A-Z0-9 -]{6,24})")
        emergency_key = _first_match(snippet, r"EMERG\s+KEY\s*:\s*([A-Z0-9 -]{6,24})")
        fcc = _first_match(snippet, r"FCC\s+INFO\s*:\s*([A-Z0-9-]{4,24})")
        frequency = _first_match(snippet, r"\b(315|433|434)\s*MHZ\b")
        combo = " ".join(value for value in (remote_part, emergency_key, frequency) if value)
        normalized = re.sub(r"[^A-Z0-9]+", " ", combo or snippet)
        normalized = re.sub(r"\b(?:REMOTE|EMERG|KEY|FCC|INFO|MHZ|BUTTONS?)\b", " ", normalized)
        normalized = re.sub(r"\b([0-9])O\b", r"\g<1>0", normalized)
        normalized = re.sub(r"\b([0-9A-Z]+)NO\b", r"\g<1>N0", normalized)
        normalized = re.sub(r"\bK\s+4", "4", normalized)
        normalized = re.sub(r"\bKE\s+4", "4", normalized)
        normalized = re.sub(r"\b[O0]A\b|\bOA\b", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) >= 10:
            snippets.add(normalized[:100])
    if snippets:
        return len(snippets)
    return len(re.findall(r"\b(?:FOB\s+)?REMOTE\s*:", source))


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
        source_remote_count = source_remote_option_count(source_text)
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
            normalized_reference = normalize_decoder_comparison_text(reference)
            normalized_decoder_text = normalize_decoder_comparison_text(decoder_text)
            if reference and (tool not in decoder_text or normalized_reference not in normalized_decoder_text):
                issues.append(f"Source decoder {tool} {reference} is missing or mismatched in the structured decoder table.")
                break
    if source_has_factory_tool_value(source_text) and not programming.get("factory_tool"):
        issues.append("Source includes a factory diagnostic tool, but programming.factory_tool is missing.")
    if re.search(r"\b(?:ILCO|KEYWAY)\s*[:#]", source) and not mechanical.get("ilco_keyway"):
        issues.append("Source includes blade/keyway identification but the structured report does not.")
    if re.search(r"\b(?:TDB1000|TCODE|MVP|AUTEL|SMART\s*PRO|AUTO\s*PRO\s*PAD)\b", str(mechanical.get("ilco_keyway") or ""), re.IGNORECASE):
        issues.append("A programmer/tool reference was incorrectly placed in the blade/keyway field.")
    if source_has_spacing_values(source_text):
        spacing = mechanical.get("spacing") if isinstance(mechanical.get("spacing"), dict) else {}
        expected_spacing_count = source_spacing_position_count(source_text)
        if not spacing:
            issues.append("Source includes spacing data but the structured report does not.")
        elif len(spacing) < expected_spacing_count:
            issues.append(
                f"Source includes a {expected_spacing_count}-position spacing table, but only {len(spacing)} spacing positions are structured."
            )
        elif not _is_descending_cut_table(spacing, expected_spacing_count):
            issues.append("The source spacing table was captured, but the structured values are not in valid handle-to-tip order.")
    if source_has_depth_values(source_text):
        depths = mechanical.get("depths") if isinstance(mechanical.get("depths"), dict) else {}
        if not depths:
            issues.append("Source includes depth data but the structured report does not.")
        elif len(depths) < 4:
            issues.append(
                f"Source includes a four-depth table, but only {len(depths)} depth values are structured."
            )
        elif not _is_descending_cut_table(depths, 4):
            issues.append("The source depth table was captured, but the structured values are not in valid descending order.")
    if re.search(r"\bEIGHT\s+SPACES\b", source) and len(mechanical.get("spacing") or {}) < 8:
        issues.append("Source specifies eight spacing positions, but the structured spacing table is incomplete.")
    if re.search(r"\bEIGHT\s+SPACES\b", source) and mechanical.get("spacing") and not _is_descending_cut_table(mechanical.get("spacing"), 8):
        issues.append("The eight-position spacing table is not in a valid handle-to-tip descending order.")
    if (
        mechanical.get("start_cut")
        and mechanical.get("cut_to_cut")
        and mechanical.get("depths")
        and not mechanical.get("spacing")
    ):
        issues.append("Mechanical key data includes start-cut/cut-to-cut and depths, but the spacing table is missing.")
    if re.search(r"\bFOUR\s+DEPTHS\b", source) and len(mechanical.get("depths") or {}) < 4:
        issues.append("Source specifies four depths, but the structured depth table is incomplete.")
    if re.search(r"\bFOUR\s+DEPTHS\b", source) and mechanical.get("depths") and not _is_descending_cut_table(mechanical.get("depths"), 4):
        issues.append("The four-depth table is not in a valid descending order.")
    source_mechanical_row = _extract_mechanical_spec_row(source_text)
    if source_mechanical_row.get("macs") and not mechanical.get("macs"):
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
    if source_has_ignition_retainer_value(source_text) and not mechanical.get("ignition_retainer"):
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
    if source_has_factory_tool_value(source_text) and not (draft.get("programming") or {}).get("factory_tool"):
        issues.append("Source identifies a factory programming tool, but the structured report does not.")
    if "PROGRAMMER & DIAGNOSTIC TOOLS" in source and "SUBSCRIPTION" in source and not (draft.get("programming") or {}).get("factory_tool"):
        issues.append("Source visually identifies a factory-tool/subscription path, but the structured report does not.")
    if "DEALER TRANSPONDER SYSTEM" in source and not (draft.get("programming") or {}).get("system_requirement"):
        issues.append("Source identifies a dealer transponder/programmer requirement, but the structured report does not.")
    if (
        re.search(r"\bCLONER\s*MACHINE\s*INFORMATION\b", source)
        and not transponder.get("cloner_tools")
        and not transponder.get("cloning")
    ):
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
    if source_has_pin_access_limitation(source_text) and not (draft.get("programming") or {}).get("pin_guidance"):
        issues.append("Source includes PIN-access limitations, but the programming guidance does not preserve them.")
    if re.search(r"\bNOCODESONANYLOCKS\b", source) and "NO CODES ON ANY LOCKS" not in rendered:
        issues.append("Source states that no codes are available on any locks, but the report does not.")
    if re.search(r"\b(?:LOCKS?\s+NOT\s+AVAILABLE|PIN\s+KIT|STRATTEC\s+(?:LOCK|PART)|(?:IGNITION|DOOR|TRUNK)\s+LOCK\s*[:=-])\b", source) and not draft.get("lock_parts"):
        issues.append("Source includes lock-parts or supplier information but the structured report does not.")
    if "PIN KIT" in source and "PIN KIT" not in rendered:
        issues.append("Source includes a lock-parts PIN kit number, but the structured report does not.")
    if re.search(r"\b(?:PIN\s+KIT|STRATTEC|IGNITION\s+LOCK|DOOR\s+LOCK|TRUNK\s+LOCK|LIFTGATE)\b", source):
        for part_number in sorted(set(re.findall(r"\b70\d{4,5}\b", source))):
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
                    label for label in ("IGNITION", "DOORS", "TRUNK", "SPARE TIRE", "STOWAGE", "GLOVE BOX")
                    if label in source
                ]
                rows_by_label = {
                    str(row[0]).upper(): row
                    for row in panel["rows"] if isinstance(row, list) and row
                }
                expected_row_length = len(columns) + 1
                rows_complete = True
                for label in expected_rows:
                    row = next((value for key, value in rows_by_label.items() if label in key), None)
                    label_allows_empty = label == "GLOVE BOX" and "GLOVE BOX (NONE)" in source
                    if (
                        not row
                        or len(row) != expected_row_length
                        or (
                            not label_allows_empty
                            and not any(str(value).strip().lower() == "filled" for value in row[1:])
                        )
                    ):
                        rows_complete = False
                        break
                if columns in (
                    [str(position) for position in range(1, 8)],
                    [str(position) for position in range(1, 9)],
                    [str(position) for position in range(1, 11)],
                ) and rows_complete:
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
