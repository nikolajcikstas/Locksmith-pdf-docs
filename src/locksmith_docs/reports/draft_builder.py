from __future__ import annotations

import re
from typing import Any

from locksmith_docs.parsing.rewrite import rewrite_sentence
from locksmith_docs.parsing.section_parser import extract_section_fields
from locksmith_docs.parsing.section_vehicle_parser import extract_vehicle_applications_from_sections


PART_RE = re.compile(
    r"\b(?:[A-Z]{2,5}#?\s*)?(?P<part>\d{7,9}[A-Z]{0,2}|\d{5}-\d{5}|\d{5}-[A-Z0-9]{2,4}-[A-Z0-9]{2,4})\b",
    re.IGNORECASE,
)
ILCO_RE = re.compile(r"\b(?:ILCO#?\s*)?(?P<ilco>PRX-[A-Z0-9-]+|[A-Z]{1,3}\d{2,4}[A-Z]?)\b", re.IGNORECASE)
VAG_REMOTE_PART_RE = re.compile(r"\b(?P<part>[0-9A-Z]{2,3}[0O]\s*\d{3}\s*\d{3}\s*[A-Z]{0,2})\b", re.IGNORECASE)
LISHI_RE = re.compile(r"\bLishi[:\s|]+(?P<lishi>HU\d{1,4}(?:\s*v\d+)?)", re.IGNORECASE)
DETERMINATOR_RE = re.compile(r"\bDeterminator[:\s]+(?P<det>[A-Z0-9 ]+?)(?=\s+Lishi|\s+AccuReader|\s+EEZ|\s+Cobra|$)", re.IGNORECASE)
DEPTH_RE = re.compile(r"(?P<num>[1-9])\s*=\s*[.](?P<value>\d{3})")
SPACING_RE = re.compile(r"\.(?P<value>\d{3})")
BAD_TEXT_RE = re.compile(r"[±¤ёЁЇїЈЉЊЋЌЍЎЏА-Яа-я]{2,}|Michael\s+Hyde|AutoSmart|NATIONAL", re.IGNORECASE)
BAD_TEXT_RE = re.compile(
    r"[^\x00-\x7f]|Michael\s+Hyde|AutoSmart|NATIONAL|\biock\b|\bdaors?\b|Decoder/Reade|Meth0d|Lopk|SECTlON|continued\s*-",
    re.IGNORECASE,
)


def build_report_draft(section: dict[str, Any]) -> dict[str, Any]:
    raw_text = repair_ocr_text(section.get("raw_text") or "")
    fields = sanitize_fields(section.get("fields") or extract_section_fields(raw_text))
    system_code = normalize_system_code(section["code"])

    remote_options = extract_remote_options(raw_text)
    decoders = extract_decoders(raw_text)
    emergency_key = extract_emergency_key(raw_text)
    field_notes = extract_field_notes(raw_text)
    service_notes = extract_service_notes(raw_text)
    vehicle_applications = [
        {
            "make": item.make,
            "model": item.model,
            "year_from": item.year_from,
            "year_to": item.year_to,
            "ocr_confidence": item.confidence,
        }
        for item in extract_vehicle_applications_from_sections([section])
    ]

    draft = {
        "code": system_code,
        "title": f"{system_code} Field Reference",
        "system_type": infer_system_type(raw_text),
        "vehicle_applications": vehicle_applications,
        "job_essentials": {
            "Emergency blade / keyway": emergency_key,
            "Code series": fields.get("code_series", ""),
            "Lishi / decoder": format_decoder_summary(decoders),
            "FCC ID": ", ".join(fields.get("fcc_ids", [])),
            "Frequency": ", ".join(fields.get("frequencies", [])),
            "Chip / transponder": fields.get("chip", ""),
            "PIN": "Required" if "pin" in raw_text.lower() else "Check source",
            "Programming path": infer_programming_path(raw_text),
        },
        "quick_answer": build_quick_answer(raw_text, fields),
        "key_remote": {
            "remote_type": infer_remote_type(raw_text),
            "frequency": ", ".join(fields.get("frequencies", [])),
            "fcc_id": ", ".join(fields.get("fcc_ids", [])),
            "known_options": remote_options,
            "proximity_option": extract_proximity_option(raw_text),
        },
        "mechanical_key": {
            "code_series": fields.get("code_series", ""),
            "style": infer_style(raw_text),
            "card": extract_card(raw_text),
            "ilco_keyway": emergency_key or infer_keyway(raw_text),
            "macs": extract_macs(raw_text),
            "start_cut": extract_decimal_after(raw_text, "Start"),
            "cut_to_cut": extract_decimal_after(raw_text, "Cut"),
            "milling": extract_milling(raw_text),
            "spacing": extract_spacing(raw_text),
            "depths": extract_depths(raw_text),
            "cutting_setup": extract_cutting_setup(raw_text),
            "cut_position_map": [],
        },
        "transponder": {
            "transponder_type": infer_transponder(raw_text),
            "chip": fields.get("chip", ""),
            "reusable": extract_reusable(raw_text),
            "test_key": extract_test_key(raw_text),
        },
        "programming": {
            "pin_required": "Yes" if "pin" in raw_text.lower() else "Check source",
            "factory_tool": extract_factory_tool(raw_text),
            "system_requirement": extract_programming_requirement(raw_text),
            "cloning": "No cloning for this system." if "no cloning" in raw_text.lower() else "Check source",
            "tools": extract_programmer_tools(raw_text),
        },
        "making_key": {
            "code_availability": infer_code_availability(raw_text),
            "methods": extract_methods(raw_text),
            "field_workflow": build_field_workflow(raw_text),
            "field_notes": field_notes or extract_general_notes(raw_text, limit=10),
            "service_notes": service_notes or build_source_service_guide(raw_text),
        },
        "technician_checklist": build_checklist(raw_text),
        "source_facts": extract_source_facts(raw_text),
        "decoders": decoders,
        "lock_parts": extract_lock_parts(raw_text),
        "troubleshooting": build_troubleshooting(raw_text),
        "warnings": build_warnings(raw_text),
        "procedure_diagrams": [],
        "source_coverage": [
            f"Source coverage: pages {section.get('page_start')}-{section.get('page_end')}.",
        ],
    }
    return sanitize_public_payload(draft)


def normalize_system_code(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9-]", "", str(value or "").upper()).strip("-")
    if not clean or clean.isdigit() or clean.startswith("-"):
        return ""
    if "-" not in clean and re.fullmatch(r"[A-Z]{2,6}\d{1,3}", clean):
        clean = re.sub(r"([A-Z]+)(\d+)", r"\1-\2", clean)
    return clean if re.fullmatch(r"[A-Z]{2,10}-[A-Z0-9]{1,4}", clean) else ""


def sanitize_public_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean for key, item in value.items() if has_public_value(clean := sanitize_public_payload(item))}
    if isinstance(value, list):
        return [clean for item in value if has_public_value(clean := sanitize_public_payload(item))]
    if isinstance(value, str):
        return clean_public_text(value)
    return value


def has_public_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def clean_public_text(value: str) -> str:
    value = normalize_note_line(repair_ocr_text(value))
    value = re.sub(r"\s+", " ", value).strip()
    if not value or BAD_TEXT_RE.search(value):
        return ""
    if re.search(r"\b[A-Z]{1,5}-?\d{1,3}\s+SECTION\b", value, re.IGNORECASE):
        return ""
    letters = re.sub(r"[^A-Za-z]", "", value)
    if len(value) > 24 and len(letters) / max(len(value), 1) < 0.42:
        return ""
    return value


def extract_remote_options(text: str) -> list[dict[str, str]]:
    options = []
    for line in split_sentences(text):
        if "FCC" not in line and "Fob" not in line and "F0b" not in line and "Remote" not in line:
            continue
        parts = [normalize_vag_part(match.group("part")) for match in VAG_REMOTE_PART_RE.finditer(line)]
        parts.extend(match.group("part") for match in PART_RE.finditer(line) if match.group("part") not in parts)
        if not parts:
            continue
        option = {
            "years": extract_year_hint(line),
            "models": "",
            "part": " / ".join(dict.fromkeys(parts[:2])),
            "fcc_id": extract_remote_fcc(line),
            "frequency": extract_remote_frequency(line),
            "emergency_blade": extract_remote_emergency_key(line),
            "buttons": extract_button_count(line),
            "notes": summarize_remote_note(line),
        }
        options.append({key: value for key, value in option.items() if value})
    return dedupe_remote_options(options)


def dedupe_remote_options(options: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for option in options:
        marker = tuple(
            str(option.get(key) or "").casefold()
            for key in ("models", "years", "part", "fcc_id", "frequency", "buttons", "notes")
        )
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(option)
    return unique


def normalize_vag_part(value: str) -> str:
    groups = value.upper().split()
    if groups:
        groups[0] = groups[0].replace("O", "0")
    return " ".join(groups)


def extract_remote_fcc(line: str) -> str:
    match = re.search(r"\bFCC\s*Info\s*[:#-]?\s*(?P<value>[A-Z0-9-]{5,24})", line, re.IGNORECASE)
    return match.group("value").upper() if match and is_valid_fcc(match.group("value")) else ""


def extract_remote_frequency(line: str) -> str:
    match = re.search(r"\b(?P<value>315|433|434|868|902)\s*MHz\b", line, re.IGNORECASE)
    return f"{match.group('value')} MHz" if match else ""


def extract_remote_emergency_key(line: str) -> str:
    match = re.search(r"\b(?:Emerg(?:ency)?\s+Key|E-Key)\s*[:#-]?\s*(?P<value>[0-9A-Z ]{8,20})", line, re.IGNORECASE)
    return normalize_vag_part(match.group("value")) if match else ""


def extract_source_facts(text: str) -> dict[str, list[str]]:
    part_numbers = sorted(set(match.group("part") for match in PART_RE.finditer(text)))
    fcc_ids = sorted(set(match.group(1) for match in re.finditer(r"\bFCC(?: Info)?[:\s]+([A-Z0-9-]{5,24})", text, re.IGNORECASE)))
    frequencies = sorted(set(match.group(0).upper().replace(" ", "") for match in re.finditer(r"\b(?:315|433|434|868|902)\s*MHz\b", text, re.IGNORECASE)))
    keyways = sorted(set(match.group("ilco") for match in ILCO_RE.finditer(text) if is_keyway_identifier(match.group("ilco"))))
    chips = sorted(set(match.group(1).strip() for match in re.finditer(r"\bChip[:\s]+([A-Za-z0-9 /-]{2,42})", text, re.IGNORECASE)))
    tools = [item["name"] for item in extract_programmer_tools(text)]
    code_series = extract_code_series_text(text)
    return {
        "Part numbers": [item for item in part_numbers if is_good_identifier(item)][:30],
        "FCC IDs": [item for item in fcc_ids if is_valid_fcc(item)][:20],
        "Frequencies": frequencies[:8],
        "Blades / keyways": keyways[:20],
        "Chip references": [item for item in (clean_chip_reference(item) for item in chips) if item][:10],
        "Programming tools": tools[:12],
        "Code series": [code_series] if code_series else [],
    }


def is_good_identifier(value: str) -> bool:
    value = value.strip()
    if BAD_TEXT_RE.search(value):
        return False
    if not re.search(r"\d", value):
        return False
    if value.upper() in {"111FO", "1NFO", "T001S", "D00R"}:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9#./ -]{2,32}", value))


def is_valid_fcc(value: str) -> bool:
    value = value.strip().upper()
    if value in {"111FO", "1NFO", "INFO", "FCC"}:
        return False
    if BAD_TEXT_RE.search(value):
        return False
    return bool(re.fullmatch(r"[A-Z0-9-]{5,24}", value) and re.search(r"[A-Z]", value) and re.search(r"\d", value))


def is_keyway_identifier(value: str) -> bool:
    value = value.strip().upper()
    if not is_good_identifier(value):
        return False
    return bool(re.fullmatch(r"(?:PRX|RHK|RKE|FLIP)-[A-Z0-9-]+|(?:TOY|HON|NIS|NSN|HU|CY|HD|DA|TR|Y|B)\d{1,5}[A-Z]?", value))


def clean_chip_reference(value: str) -> str:
    value = clean_short_value(value, max_len=48, allow_spaces=True)
    if not value:
        return ""
    if not re.search(r"\b(?:Philips|Megamos|Texas|Crypto|AES|Wedge|4D|46|48|ID)\b", value, re.IGNORECASE):
        return ""
    return value


def extract_decoders(text: str) -> list[dict[str, str]]:
    decoders = []
    lishi = LISHI_RE.search(text)
    if lishi:
        reference = clean_decoder_reference(lishi.group("lishi"))
        if reference:
            decoders.append({"tool": "Lishi", "reference": reference})
    determinator = DETERMINATOR_RE.search(text)
    if determinator:
        reference = clean_decoder_reference(determinator.group("det"))
        if reference:
            decoders.append({"tool": "Determinator", "reference": reference})
    for label in ("AccuReader", "EEZ Reader", "Cobra"):
        value = clean_decoder_reference(extract_after_label(text, label))
        if value:
            decoders.append({"tool": label, "reference": value})
    return dedupe_dicts(decoders, "tool")


def clean_decoder_reference(value: str) -> str:
    value = clean_short_value(value, max_len=18, allow_spaces=True)
    if not value or re.match(r"^(?:n/?a|nia|none)\b", value, re.IGNORECASE):
        return ""
    if not re.search(r"\d|[A-Z]{2,}", value):
        return ""
    return value


def format_decoder_summary(decoders: list[dict[str, str]]) -> str:
    if not decoders:
        return "Check source pages"
    return "; ".join(
        f"{item.get('tool')}: {item.get('reference')}"
        for item in decoders
        if item.get("reference")
    )


def extract_depths(text: str) -> dict[str, str]:
    return {match.group("num"): "." + match.group("value") for match in DEPTH_RE.finditer(text)}


def extract_spacing(text: str) -> dict[str, str]:
    if "SPACING" not in text.upper():
        return {}
    segment = re.split(r"\bDEPTHS?\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    values = ["." + value for value in re.findall(r"(?<!\d)[.]?\s*(\d{3})(?!\d)", segment)]
    for start in range(max(1, len(values) - 7)):
        candidate = values[start:start + 8]
        if len(candidate) == 8 and all(float(candidate[index]) > float(candidate[index + 1]) for index in range(7)):
            return {str(index + 1): value for index, value in enumerate(candidate)}
    return {}


def extract_programmer_tools(text: str) -> list[dict[str, str]]:
    known = ["Smart Pro", "TCODE", "MVP", "TDB1000", "Hotwire", "Auto Pro Pad", "Autel", "IM608"]
    tools = []
    lower = text.lower()
    for tool in known:
        if tool.lower() in lower:
            tools.append({"name": tool, "status": "Check support" if "?" in text else "Mentioned"})
    return dedupe_dicts(tools, "name")


def extract_methods(text: str) -> list[str]:
    lower = text.lower()
    methods = []
    if "owner" in lower and "manual" in lower:
        methods.append("Check the owner's manual or dealer-written code record before removing parts.")
    if "lock decoder" in lower or "reader" in lower or "decode" in lower:
        methods.append("Use a lock decoder/reader on an accessible door lock to determine the cuts for a working key.")
    if "door cylinder" in lower or "door lock" in lower:
        methods.append("Remove the door cylinder only when decoding in-place is not practical, then decode the wafers/tumblers.")
    if "trunk" in lower:
        methods.append("If the door does not provide enough information, use the trunk/hatch cylinder when the vehicle layout supports it.")
    if "dealer" in lower and "vin" in lower:
        methods.append("Verify VIN-based key, fob, or proximity data with the dealer/source before ordering parts.")
    if "tryout" in lower:
        methods.append("Use the referenced tryout set only after confirming the correct system and keyway.")
    if not methods:
        methods.append("Verify keyway and system data, then decode an accessible lock or use the listed source method for the vehicle.")
    return dedupe_strings(methods)[:6]


def extract_field_notes(text: str) -> list[str]:
    lower = text.lower()
    notes = []
    if "fcc" in lower or "mhz" in lower:
        notes.append("Match FCC ID, frequency, button count, and part number before programming or selling a remote.")
    if "pin" in lower:
        notes.append("PIN access is mentioned for this system; confirm the PIN workflow before connecting a programmer.")
    if "dealer" in lower or "vin" in lower:
        notes.append("VIN or dealer verification is mentioned; verify fitment before ordering key, fob, or proximity parts.")
    if "decode" in lower or "lishi" in lower or "reader" in lower:
        notes.append("A lock reader/decoder path is available or referenced; use the listed decoder data before removing parts.")
    if "door" in lower:
        notes.append("Door lock/cylinder information is relevant for making a working mechanical key.")
    if "trunk" in lower or "hatch" in lower:
        notes.append("Trunk or hatch lock information may help when the door lock does not provide enough usable cuts.")
    if "no cloning" in lower:
        notes.append("No-cloning language is present; plan to program the key/fob rather than clone it.")
    if "reusable" in lower:
        notes.append("Reusable status is mentioned; verify whether used keys/fobs are allowed for this system.")
    if "airbag" in lower or "retainer" in lower:
        notes.append("Airbag or retainer safety language is present; follow safe removal procedure before servicing lock components.")
    return dedupe_strings(notes)[:14]


def extract_service_notes(text: str) -> list[str]:
    return build_source_service_guide(text)


def extract_general_notes(text: str, limit: int = 12) -> list[str]:
    notes = []
    for line in meaningful_source_lines(text):
        lowered = line.lower()
        if any(token in lowered for token in ("copyright", "autosmart", "table of contents")):
            continue
        notes.append(rewrite_sentence(line[:320]))
    return dedupe_strings(notes)[:limit]


def build_source_service_guide(text: str) -> list[str]:
    lower = text.lower()
    guide = [
        "Use this system record as the job control sheet: verify vehicle identity, blade data, remote/FCC data, and programmer support before work begins.",
    ]
    code_series = extract_code_series_text(text)
    if code_series:
        guide.append(f"Mechanical key work should use the listed code series: {code_series}.")
    if "spacing" in lower or "depth" in lower:
        guide.append("Use the spacing/depth data in the mechanical key section when cutting the blade; do not rely on screenshot tables.")
    if "remote" in lower or "fob" in lower or "proximity" in lower:
        guide.append("For remote or proximity systems, verify FCC ID, frequency, button count, and part number before programming.")
    if "chip" in lower or "transponder" in lower:
        guide.append("Check the transponder/chip section before choosing a reusable, clone, or program-new workflow.")
    if "pin" in lower:
        guide.append("PIN access is required or referenced; confirm the PIN source and programmer support before attempting programming.")
    if "no cloning" in lower:
        guide.append("The source indicates no cloning for this system; use a programming workflow instead of cloning.")
    if "dealer" in lower or "vin" in lower:
        guide.append("Dealer or VIN verification is referenced; confirm exact part fitment before ordering.")
    if "decode" in lower or "door lock" in lower or "door cylinder" in lower:
        guide.append("When all keys are lost, decode the door lock/cylinder or use the listed lock reader path to recover cuts.")
    if "trunk" in lower or "hatch" in lower:
        guide.append("If supported by the vehicle, the trunk/hatch cylinder can be used as an alternate decoding source.")
    if "strattec" in lower or "asp" in lower:
        guide.append("Lock part availability is mentioned; verify lock, cylinder, and pin-kit information before parts ordering.")
    return dedupe_strings(guide)[:16]


def sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean = dict(fields)
    clean["code_series"] = clean_code_series(str(clean.get("code_series") or ""))
    clean["chip"] = clean_short_value(str(clean.get("chip") or ""), max_len=48, allow_spaces=True)
    clean["fcc_ids"] = [
        value
        for value in clean.get("fcc_ids", [])
        if isinstance(value, str) and is_valid_fcc(value)
    ]
    clean["frequencies"] = [
        value
        for value in clean.get("frequencies", [])
        if isinstance(value, str) and re.fullmatch(r"(315|433|434|868|902) MHz", value.strip(), re.IGNORECASE)
    ]
    return clean


def clean_short_value(value: str, max_len: int = 32, allow_spaces: bool = False) -> str:
    value = normalize_note_line(repair_ocr_text(value))
    value = re.sub(r"\s+", " ", value).strip(" -,:;.")
    if not value or len(value) > max_len or BAD_TEXT_RE.search(value):
        return ""
    pattern = r"[A-Za-z0-9 /#().-]+" if allow_spaces else r"[A-Za-z0-9#.-]+"
    if not re.fullmatch(pattern, value):
        return ""
    return value


def clean_code_series(value: str) -> str:
    value = clean_short_value(value, max_len=40, allow_spaces=True)
    if not value:
        return ""
    if len(value) < 3 or not re.search(r"\d", value):
        return ""
    return value


def extract_code_series_text(text: str) -> str:
    fields = extract_section_fields(text)
    return clean_code_series(str(fields.get("code_series") or ""))


def meaningful_source_lines(text: str) -> list[str]:
    candidates = []
    for line in split_sentences(text):
        clean = normalize_note_line(line)
        clean = re.sub(r"\s+", " ", clean).strip(" -|")
        if len(clean) < 28 or len(clean) > 520:
            continue
        if BAD_TEXT_RE.search(clean):
            continue
        if is_low_value_ocr_line(clean):
            continue
        if clean.count(" ") < 3:
            continue
        candidates.append(clean)
    return dedupe_strings(candidates)


def extract_lock_parts(text: str) -> list[dict[str, str]]:
    records = []
    for line in meaningful_source_lines(text):
        lowered = line.lower()
        if not any(token in lowered for token in ("strattec", "asp", "pin kit", "lock part", "lock available", "lock not available")):
            continue
        summary = rewrite_sentence(line[:180])
        if BAD_TEXT_RE.search(summary) or looks_like_instruction_text(summary):
            continue
        records.append(
            {
                "years": extract_year_hint(line),
                "models": "",
                "ignition_lock": summary if "ignition" in lowered or "lock" in lowered else "",
                "door_lock": summary if "door" in lowered else "",
                "trunk_lock": summary if "trunk" in lowered else "",
                "pin_kit": extract_pin_kit(line),
            }
        )
    return dedupe_dicts(records, "ignition_lock")[:12]


def looks_like_instruction_text(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:method|decode|decoder|reader|determine|disassemble|working key|making a key|no codes)\b",
            text,
            re.IGNORECASE,
        )
    )


def build_troubleshooting(text: str) -> list[dict[str, str]]:
    lower = text.lower()
    rows = [
        {
            "issue": "Vehicle or trim mismatch",
            "action": "Confirm make, model, year, system code, FCC/frequency, and VIN-based part fitment before ordering or programming.",
        }
    ]
    if "pin" in lower:
        rows.append({"issue": "PIN access required", "action": "Obtain or calculate the correct PIN before programming; stop if the tool cannot support the PIN workflow for this vehicle."})
    if "gateway" in lower:
        rows.append({"issue": "Security gateway blocks programming", "action": "Prepare the required gateway bypass/access path before connecting the programmer."})
    if "dealer" in lower or "vin" in lower:
        rows.append({"issue": "Part number uncertainty", "action": "Verify the remote or proximity fob by VIN when the source indicates dealer/VIN confirmation."})
    if "no codes" in lower or "keycodes" in lower:
        rows.append({"issue": "No usable lock code available", "action": "Decode an accessible lock or remove and decode the cylinder, then cut the mechanical key from the recovered cuts."})
    if "reusable" in lower:
        rows.append({"issue": "Used remote or chip may fail", "action": "Check reusable status before selling or programming a used fob/transponder."})
    if "airbag" in lower or "retainer" in lower:
        rows.append({"issue": "Column or lock removal risk", "action": "Follow airbag and retainer safety steps before removing ignition or column components."})
    return rows[:8]


def summarize_remote_note(line: str) -> str:
    clean = normalize_note_line(line)
    if BAD_TEXT_RE.search(clean):
        clean = re.sub(r"[^\x00-\x7f]+", " ", clean)
    fragments = []
    for keyword in ("FCC", "MHz", "Remote", "Fob", "F0b", "Proximity", "Emergency", "Smart"):
        match = re.search(rf".{{0,40}}\b{keyword}\b.{{0,70}}", clean, flags=re.IGNORECASE)
        if match:
            fragments.append(match.group(0).strip())
    if not fragments:
        return "Verify fitment by VIN before ordering."
    note = " ".join(dict.fromkeys(fragments))
    note = re.sub(r"\b(?:SECTION|CODES?|SPACING|DEPTHS|MACS|StartCut|CuttoCut).*$", "", note, flags=re.IGNORECASE)
    note = " ".join(note.split()).strip(" -,:;")
    if len(note) < 8 or BAD_TEXT_RE.search(note):
        return "Verify fitment by VIN before ordering."
    return rewrite_sentence(note[:120])


def is_low_value_ocr_line(line: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", line)
    if len(letters) < 18:
        return True
    if len(line) > 90 and (len(letters) / max(len(line), 1)) < 0.45:
        return True
    return False


def normalize_note_line(line: str) -> str:
    replacements = {
        "РљРµСѓ": "key",
        "Рљеу": "key",
        "СЃР°Рі": "vehicle",
        "СѓРѕРё": "you",
        "Р’РѕС…": "box",
        "РІРѕС…": "box",
        "С‚Р°РєРµ": "make",
    }
    for src, dst in replacements.items():
        line = line.replace(src, dst)
    line = line.replace("танпга1", "manual").replace("тапиа1", "manual")
    line = line.replace("deаIer", "dealer").replace("dea[er", "dealer").replace("dea1er", "dealer")
    line = line.replace("Iock", "lock").replace("d00r", "door").replace("d0or", "door")
    line = line.replace("Kеу", "Key").replace("Кеу", "Key")
    return " ".join(line.split())


def repair_ocr_text(text: str) -> str:
    replacements = {
        "вЂ“": "-",
        "вЂ”": "-",
        "вЂ™": "'",
        "Р ": " ",
        "РЎ": " ",
        "С™": "",
        "C0DES": "CODES",
        "COOES": "CODES",
        "СODES": "CODES",
        "1gn": "Ign",
        "IgnRelainer": "Ign Retainer",
        "Relainer": "Retainer",
        "Retalner": "Retainer",
        "StanCut": "StartCut",
        "S1artCut": "StartCut",
        "Cuttocut": "CutToCut",
        "CuttoCut": "CutToCut",
        "F0b": "Fob",
        "FO8": "FOB",
        "Llshi": "Lishi",
        "Lishl": "Lishi",
        "AccuReade": "AccuReader",
        "Determjnator": "Determinator",
        "Determinatr": "Determinator",
        "KEYИME": "KEYNAME",
        "MACs": "MACS",
        "MAcs": "MACS",
        "M0re": "More",
        "1nfo": "Info",
        "HUS6": "HU66",
        "Testkeyis": "Test Key is ",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    for src, dst in {
        "Nocodesonanylocks": "No codes on any locks",
        "NoCodesonanylocks": "No codes on any locks",
        "onlyaguide": "only a guide",
        "itneedstoworkinthelocks": "it needs to work in the locks",
        "donot": "do not",
        "Thisisagamble": "This is a gamble",
        "mitung": "milling",
        "AUTELIMGDS": "AUTEL IM608",
    }.items():
        text = text.replace(src, dst)
    text = re.sub(r"(?<=\d)\s*[-]\s*(?=\d{2,4})", "-", text)
    text = re.sub(r"\bF\s*C\s*C\b", "FCC", text, flags=re.IGNORECASE)
    text = re.sub(r"\bM\s*H\s*z\b", "MHz", text, flags=re.IGNORECASE)
    return text


def build_field_workflow(text: str) -> list[dict[str, str]]:
    return [
        {"step": "Confirm vehicle match", "detail": "Verify make, model, year, system code and trim before ordering parts."},
        {"step": "Verify parts", "detail": "Confirm FCC ID, frequency, button count and part number before programming."},
        {"step": "Prepare access", "detail": infer_programming_path(text)},
        {"step": "Create mechanical key", "detail": "Use approved blade, spacing, depths and decoder data from this report."},
        {"step": "Program and test", "detail": "Program the remote/transponder and test every customer-facing function before release."},
    ]


def infer_code_availability(text: str) -> str:
    lower = text.lower()
    if "no codes" in lower or "no code" in lower:
        return "No usable lock code is listed for this system; decode an accessible lock or cylinder when making a mechanical key."
    if "code is stamped" in lower or "codes are stamped" in lower:
        return "A lock code may be stamped on the referenced lock/cylinder; verify before decoding manually."
    if "dealer" in lower and "vin" in lower:
        return "VIN/dealer verification is mentioned for this system; verify exact key/fob data before ordering."
    return "Use the blade, decoder, and method notes below to determine the working key path."


def build_checklist(text: str) -> dict[str, list[str]]:
    return {
        "Bring to the job": ["Supported programmer", "Decoder/reader tools", "Correct blade/keyway", "PIN access if required"],
        "Verify before ordering": ["VIN-based part number", "FCC ID", "Frequency", "Chip/transponder type"],
        "Final tests": ["Mechanical key operation", "Remote lock/unlock", "Start authorization", "Trunk/remote start if equipped"],
    }


def build_quick_answer(text: str, fields: dict[str, Any]) -> list[str]:
    answers = []
    if fields.get("chip"):
        answers.append(f"Transponder/chip data detected: {fields['chip']}.")
    if fields.get("fcc_ids"):
        answers.append(f"FCC ID detected: {', '.join(fields['fcc_ids'])}.")
    if "pin" in text.lower():
        answers.append("PIN access appears to be required or mentioned for this system.")
    answers.append("Use the sections below to verify blade, FCC/frequency, programming path, decoder references, and final tests before releasing the vehicle.")
    return answers


def build_warnings(text: str) -> list[str]:
    warnings = ["Verify exact part numbers by VIN before ordering.", "Confirm programmer support before connecting to the vehicle."]
    if "gateway" in text.lower():
        warnings.append("Security gateway bypass may be required.")
    if "risk" in text.lower():
        warnings.append("Vehicle electronics risk is mentioned in source material; follow tool/vendor safety guidance.")
    return warnings


def infer_system_type(text: str) -> str:
    lower = text.lower()
    if "proximity" in lower:
        return "Proximity / Smart Key"
    if "fobik" in lower:
        return "Fobik"
    if "transponder" in lower:
        return "Transponder Key"
    return "Key"


def infer_remote_type(text: str) -> str:
    if "proximity" in text.lower():
        return "Proximity fob"
    if "remote" in text.lower():
        return "Remote"
    return "Key"


def infer_transponder(text: str) -> str:
    if "proximity" in text.lower():
        return "Proximity"
    if "transponder" in text.lower():
        return "Transponder"
    return ""


def infer_programming_path(text: str) -> str:
    parts = ["Use a supported programmer for the exact vehicle."]
    if "pin" in text.lower():
        parts.append("PIN access is required or mentioned.")
    if "gateway" in text.lower():
        parts.append("Check whether gateway bypass is required.")
    return " ".join(parts)


def infer_style(text: str) -> str:
    if "dbl" in text.lower() or "double" in text.lower():
        return "Double-sided"
    return ""


def infer_keyway(text: str) -> str:
    matches = [match.group("ilco") for match in ILCO_RE.finditer(text)]
    filtered = [value for value in matches if is_keyway_identifier(value)]
    return " / ".join(dict.fromkeys(filtered[:4]))


def extract_emergency_key(text: str) -> str:
    match = re.search(
        r"Emergency.{0,30}?(?P<value>\d{7,9}[A-Z]{0,2}|\d{5}-\d{5}|\d{5}-[A-Z0-9]{2,4}-[A-Z0-9]{2,4}|[A-Z]{1,3}\d{2,4}[A-Z]?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    value = match.group("value").strip()
    if value.upper() in {"WILL", "WI11", "T001S", "D00R", "F05"}:
        return ""
    return value


def extract_reusable(text: str) -> str:
    match = re.search(r"Reusable[:\s]+(?P<value>.{1,80})", text, re.IGNORECASE)
    if not match:
        return ""
    value = clean_short_value(match.group("value"), max_len=60, allow_spaces=True)
    return rewrite_sentence(value) if value else ""


def extract_test_key(text: str) -> str:
    match = re.search(r"Test\s*Key\s*is\s*(?P<value>[A-Z0-9 /-]+)", text, re.IGNORECASE)
    if not match:
        return ""
    return clean_short_value(match.group("value"), max_len=32, allow_spaces=True)


def extract_factory_tool(text: str) -> str:
    match = re.search(r"Factory Tool\s+(?P<value>.{1,80}?)(?:PIN|TCODE|SMART|$)", text, re.IGNORECASE)
    if not match:
        return ""
    value = clean_short_value(match.group("value"), max_len=64, allow_spaces=True)
    return rewrite_sentence(value) if value else ""


def extract_programming_requirement(text: str) -> str:
    if re.search(r"Dealer\s+transponder\s+system", " ".join(text.split()), re.IGNORECASE):
        return "Dealer transponder system; specialized key programmer or EEPROM equipment is required."
    return ""


def extract_proximity_option(text: str) -> str:
    if re.search(r"\bKESSY\b", text, re.IGNORECASE):
        return "Some models may be equipped with the KESSY proximity option."
    return ""


def extract_cutting_setup(text: str) -> dict[str, str]:
    setup = {}
    normalized = " ".join(text.split())
    if re.search(r"left\s+track.{0,24}(?:cuts?|cutting)", normalized, re.IGNORECASE):
        setup["cut_track"] = "The left track contains the cuts."
    if re.search(r"right\s+track.{0,24}(?:guide|clearance)", normalized, re.IGNORECASE):
        setup["guide_track"] = "The right track is used only as a guide/clearance track."
    if re.search(r"pre-?cut\s+the\s+left\s+track.{0,48}number\s+one\s+depths", normalized, re.IGNORECASE):
        setup["preparation"] = "Pre-cut the left track to all number 1 depths before applying the working-key cut values."
    for label in ("Curtis Clipper", "Pak-A-Punch", "FRAMON"):
        if label.lower() in text.lower():
            setup[label] = "Mentioned in source; verify exact setup on source page during review."
    return setup


def extract_milling(text: str) -> str:
    if re.search(r"\bINTERNAL.{0,24}\bMILLING\b", text, re.IGNORECASE | re.DOTALL):
        return "Internal milling"
    if re.search(r"\bEXTERNAL.{0,24}\bMILLING\b", text, re.IGNORECASE | re.DOTALL):
        return "External milling"
    return ""


def extract_card(text: str) -> str:
    value = extract_after_label(text, "CARD#") or extract_after_label(text, "CARD")
    value = clean_short_value(value, max_len=16, allow_spaces=False)
    if not value or not re.search(r"\d", value):
        return ""
    return value


def extract_macs(text: str) -> str:
    match = re.search(r"\bMACS[:\s]+(?P<value>\d{1,2})\b", text, re.IGNORECASE)
    if not match:
        return ""
    value = int(match.group("value"))
    if 1 <= value <= 12:
        return str(value)
    return ""


def extract_after_label(text: str, label: str) -> str:
    match = re.search(re.escape(label) + r"[:#\s]+(?P<value>[A-Z0-9 /.-]{1,24})", text, re.IGNORECASE)
    if not match:
        return ""
    value = clean_short_value(match.group("value"), max_len=24, allow_spaces=True)
    return value


def extract_decimal_after(text: str, label: str) -> str:
    match = re.search(re.escape(label) + r".{0,20}?(?P<value>\.\d{3})", text, re.IGNORECASE)
    return match.group("value") if match else ""


def extract_year_hint(text: str) -> str:
    match = re.search(r"(?:19|20)\d{2}\s*[-–]\s*\d{2,4}", text)
    return match.group(0) if match else ""


def extract_pin_kit(text: str) -> str:
    match = re.search(r"\b(?:pin kit|pin)\s*[:#]?\s*(?P<kit>\d{4,8}[A-Z]?)\b", text, flags=re.IGNORECASE)
    return match.group("kit") if match else ""


def extract_button_count(text: str) -> str:
    match = re.search(r"(?P<count>\d)\s*Btn", text, re.IGNORECASE)
    return match.group("count") if match else ""


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n\r]+|(?<=\.)\s+", text) if part.strip()]


def dedupe_dicts(items: list[dict[str, str]], key: str) -> list[dict[str, str]]:
    seen = set()
    output = []
    for item in items:
        marker = item.get(key, "")
        if marker in seen:
            continue
        seen.add(marker)
        output.append(item)
    return output


def dedupe_strings(items: list[str]) -> list[str]:
    seen = set()
    output = []
    for item in items:
        marker = item.lower()
        if marker in seen:
            continue
        seen.add(marker)
        output.append(item)
    return output
