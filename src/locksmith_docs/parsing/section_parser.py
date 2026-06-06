from __future__ import annotations

import re
from collections import defaultdict

from locksmith_docs.parsing.models import SystemSectionCandidate


SECTION_HEADER_RE = re.compile(
    r"\b(?P<code>[A-Z]{2,5}[-.]?[A-Z0-9]{1,4})\s*[-.]?\s*S[E3]CT(?:I|1|\[)?(?:O|0)?N?\b",
    re.IGNORECASE,
)
INVALID_SECTION_CODES = {"THIS", "OFTHIS", "THE", "NATIONAL"}
DOCUMENT_PREFIX_REPAIRS = {
    "asian": {
        "ACJ": "ACU", "NLS": "NIS", "LSUZU": "ISUZU", "TSUZU": "ISUZU",
        "MLTS": "MITS", "WTS": "MITS", "SUZL": "SUZU", "SUZI": "SUZU",
        "SUA": "SUZU", "MFA": "MAZ", "Y": "TOY", "SUN": "SUB",
    },
    "domestic": {"DCE": "DGE", "CM": "GM"},
    "audi": {"AVA": "AV"},
    "jaguar": {"JUR": "JLR", "ULR": "JLR", "JER": "JLR"},
}
FCC_RE = re.compile(r"\bFCC[:\s]+(?P<fcc>[A-Z0-9-]{5,})", re.IGNORECASE)
FREQ_RE = re.compile(r"\b(?P<freq>315|433|434|868|902)\s*MHz\b", re.IGNORECASE)
CHIP_RE = re.compile(
    r"\bChip[:\s]+(?P<chip>[A-Za-z0-9 ()/-]+?)(?=\s+(?:Reusable|Code Series|FCC|PIN|$))",
    re.IGNORECASE,
)
CODE_SERIES_RE = re.compile(r"\bCode Series[:\s]+(?P<series>[A-Z0-9-]+)", re.IGNORECASE)


def split_sections(pages: list[dict], source_document: str) -> list[SystemSectionCandidate]:
    sections: list[SystemSectionCandidate] = []
    current: SystemSectionCandidate | None = None

    for index, page in enumerate(pages):
        text = page.get("ocr_text") or ""
        code = find_section_code(text, source_document)
        if not code and is_section_boundary_page(text) and not (current and is_technical_continuation_page(text)):
            if current:
                current.page_end = page["page_number"] - 1
                current.fields = extract_section_fields(current.raw_text)
                sections.append(current)
                current = None
            continue
        if current and not code and index + 1 < len(pages):
            next_text = pages[index + 1].get("ocr_text") or ""
            next_code = find_section_code(next_text, source_document)
            if next_code and next_code != current.code and is_continuation_page(next_text):
                # A section's first page may lose its heading in OCR, while the following
                # page clearly says "continued - XX-N SECTION". Do not append that first
                # page to the preceding system.
                current.page_end = page["page_number"] - 1
                current.fields = extract_section_fields(current.raw_text)
                sections.append(current)
                current = SystemSectionCandidate(
                    code=next_code,
                    title=f"{next_code} system",
                    source_document=source_document,
                    page_start=page["page_number"],
                    page_end=None,
                    raw_text=text,
                    confidence=0.6,
                )
                continue
        if current and not code:
            suffix = find_corrupted_section_suffix(text)
            current_suffix = numeric_suffix(current.code)
            if looks_like_unlabeled_system_first_page(text):
                code = increment_variant_code(current.code)
            elif suffix and (current_suffix is None or int(suffix) > current_suffix):
                code = f"{section_code_stem(current.code)}{suffix}"
            elif looks_like_corrupted_new_section(text) and current_suffix is not None:
                code = f"{section_code_stem(current.code)}{current_suffix + 1}"
            elif looks_like_corrupted_new_section(text):
                code = increment_variant_code(current.code)
        if code:
            if code.replace("-", "") in INVALID_SECTION_CODES:
                continue
            if current and code == current.code:
                current.raw_text += "\n" + text
                continue
            if current:
                current.page_end = page["page_number"] - 1
                current.fields = extract_section_fields(current.raw_text)
                sections.append(current)
            raw_text = text
            page_start = page["page_number"]
            if "continued" in text.lower() and index > 0:
                previous = pages[index - 1]
                previous_text = previous.get("ocr_text") or ""
                if previous_text and not find_section_code(previous_text, source_document):
                    raw_text = previous_text + "\n" + text
                    page_start = previous["page_number"]
            current = SystemSectionCandidate(
                code=code,
                title=f"{code} system",
                source_document=source_document,
                page_start=page_start,
                page_end=None,
                raw_text=raw_text,
                confidence=0.6,
            )
        elif current:
            current.raw_text += "\n" + text

    if current:
        current.page_end = pages[-1]["page_number"] if pages else current.page_start
        current.fields = extract_section_fields(current.raw_text)
        sections.append(current)

    return sections


def is_section_boundary_page(text: str) -> bool:
    normalized = normalize_section_text(text).upper()
    section_markers = (
        "TABLE OF CONTENTS",
        "TAЫLE OF CONTENTS",
        "ТАЫЕ OF CONTENTS",
        "ON-BOARD TRANSPONDER PROGRAMMING",
        "PROXIMITY PROGRAMMING INSTRUCTIONS",
        "CUT PROGRESSION CHART",
        "PASSLOCK",
        "VATS",
        "THE EVOLUTION OF THE LEXUS",
        "PROGRAM GRID",
        "MISC INFO - LEXUS",
        "TOYOTA CODE SERIES M.A.C.S.",
        "NATIONAL AUTO LOCK SERVICE",
        "TRANSPONDER INFORMATION - FORD",
        "ACURA - HONDA - TRANSPONDER INFORMATION",
        "PROGRESSION CHART",
        "ANTI-SCAN MODE",
    )
    return any(marker in normalized for marker in section_markers) or bool(
        re.search(r"ACURA\s*-?\s*HON\s*DA?\s*-?\s*TRANSPONDER INFORMATION", normalized)
    )


def is_continuation_page(text: str) -> bool:
    head = normalize_section_text(text)[:180].upper()
    return (
        "CONTINUED" in head
        or "PROGRAMMER & DIAGNOSTIC" in head
        or "DIAGNOSTIC TOOLS" in head
        or "CHIP INFORMATION" in head
    )


def is_technical_continuation_page(text: str) -> bool:
    normalized = normalize_section_text(text).upper()
    signals = ("CODE SERIES", "SPACING", "DEPTHS", "DECODERS:", "LISHI:", "ACCUREADER:")
    return sum(signal in normalized for signal in signals) >= 2


def find_corrupted_section_suffix(text: str) -> str:
    normalized = normalize_section_text(text)[:180]
    match = re.search(r"(?:\?{2,}|[-•])?[.]?(?P<suffix>\d{1,3})\s+SECTION\b", normalized, re.IGNORECASE)
    return match.group("suffix") if match else ""


def looks_like_corrupted_new_section(text: str) -> bool:
    head = normalize_section_text(text)[:180].upper()
    return "CONTINUED" not in head and "SECTION" in head and head.count("?") >= 2


def looks_like_unlabeled_system_first_page(text: str) -> bool:
    """Detect a new vehicle-system cover page when OCR loses its section code."""
    head = normalize_section_text(text)[:1100].upper()
    if "CONTINUED" in head:
        return False
    has_application_year = bool(re.search(r"\b(?:19|20)\d{2}\s*[-–]\s*\d{2,4}\b", head))
    specification_headers = (
        "CODES",
        "STYLE",
        "CARD",
        "MACS",
        "STARTCUT",
        "CUTTOCUT",
        "AIRBAGS",
        "IGN RETAINER",
    )
    compact_head = re.sub(r"\s+", "", head)
    marker_count = sum(marker.replace(" ", "") in compact_head for marker in specification_headers)
    return has_application_year and marker_count >= 4


def numeric_suffix(code: str) -> int | None:
    match = re.search(r"-(\d+)$", code)
    return int(match.group(1)) if match else None


def section_code_stem(code: str) -> str:
    return re.sub(r"\d+$", "", code)


def increment_variant_code(code: str) -> str:
    match = re.match(r"^(?P<prefix>[A-Z]{2,10}-[A-Z]?)(?P<number>\d+)$", code)
    if not match:
        return ""
    return f"{match.group('prefix')}{int(match.group('number')) + 1}"


def find_section_code(text: str, source_document: str) -> str:
    normalized = normalize_section_text(text)
    header = SECTION_HEADER_RE.search(normalized.replace(".", "-"))
    if header:
        return normalize_section_code(header.group("code"), source_document, normalized)

    fallback = re.search(
        r"\b(?P<code>[A-Z0-9А-Яа-яЈЬ•._-]{2,14})\s*S[E3]CT",
        normalized,
        re.IGNORECASE,
    )
    if fallback:
        return normalize_section_code(fallback.group("code"), source_document, normalized)
    return ""


def normalize_section_text(text: str) -> str:
    replacements = {
        "SECTlON": "SECTION",
        "SECT10N": "SECTION",
        "SECTI0N": "SECTION",
        "SECTAON": "SECTION",
        "SECTTO": "SECTIO",
        "SEGHON": "SECTION",
        "SЕCT": "SECT",
        "ВМУЬ": "BMW-",
        "ВМ\\ЈЪ": "BMW-",
        "ВМЊ": "BMW-",
        "вм": "BM",
        "МВ": "MB",
        "мв": "MB",
        "П-": "FA-",
        "ПИ": "FA-",
        "Пиб": "FA-6",
        "ПИЗ": "FA-3",
        "АСИ": "ACU",
    }
    replacements.update(
        {
            "АСИ": "ACU", "Асига": "Acura", "НОН": "HON", "НК": "HK", "нк": "HK", "6М": "GM", "ЁМ": "GM",
            "ЈЕЕР": "JEEP", "ТОУ": "TOY", "0n-Board": "On-Board", "lnformation": "Information",
        }
    )
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = text.replace("•", "-").replace("·", "-")
    text = normalize_gm_section_headers(text)
    text = normalize_toyota_section_headers(text)
    text = re.sub(r"\bHON[.\s-]*Р(?=\d)", "HON-P", text)
    text = re.sub(r"\bHK([.-]?)2З\b", r"HK\g<1>23", text)
    text = re.sub(r"\bHK([.-]?)З1\b", r"HK\g<1>31", text)
    text = re.sub(r"\bHK([.-]?)З([6-8])\b", r"HK\g<1>3\2", text)
    text = re.sub(r"\bHK([.-]?)ЗД\b", r"HK\g<1>39", text)
    text = re.sub(r"\bHK([.-]?)4О\b", r"HK\g<1>40", text)
    text = re.sub(r"\bHK([.-]?)4Д\b", r"HK\g<1>49", text)
    text = re.sub(r"\bHK([.-]?)З\b", r"HK\g<1>3", text)
    text = re.sub(r"\bHK([.-]?)1З\b", r"HK\g<1>13", text)
    text = re.sub(r"\bHK([.-]?)Л\b", r"HK\g<1>7", text)
    text = re.sub(r"\bHK([.-]?)Б\b", r"HK\g<1>6", text)
    text = re.sub(r"\bJEEP([.-]?)З\b", r"JEEP\g<1>3", text)
    text = re.sub(r"\bJEEP([.-]?)1О\b", r"JEEP\g<1>10", text)
    return text


def normalize_gm_section_headers(text: str) -> str:
    families = {
        "F": "F", "B": "B", "K": "K", "P": "P",
        "В": "B", "К": "K", "Р": "P", "в": "B", "к": "K", "р": "P",
    }

    def restore_family(match: re.Match[str]) -> str:
        family = families.get(match.group("family").upper(), match.group("family").upper())
        number = match.group("number").translate(str.maketrans({"О": "0", "З": "3", "Д": "9", "о": "0", "з": "3", "д": "9"}))
        return f"GM-{family}{number} SECTION"

    text = re.sub(
        r"\b(?:GM|СМ|см|ЗМ|зм)[.\s-]*(?P<family>[FBKPВКРвкр])\s*(?P<number>[0-9ОЗДозд]{1,3})\s+SECTION\b",
        restore_family,
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("СМ.Ь«З SECTION", "GM-K13 SECTION")

    def restore_ambiguous_family(match: re.Match[str]) -> str:
        number = match.group("number").translate(str.maketrans({"О": "0", "З": "3", "о": "0", "з": "3"}))
        if number == "3":
            number = "13" if "proximity" in text.lower() else "33"
        family = "P" if "proximity" in text.lower() else "F"
        return f"GM-{family}{number} SECTION"

    return re.sub(
        r"\b(?:GM|СМ|см|ЗМ|зм)[.\s-]*[Нн](?P<number>[Оо0Зз3]{1,2})\s+SECTION\b",
        restore_ambiguous_family,
        text,
        flags=re.IGNORECASE,
    )


def normalize_toyota_section_headers(text: str) -> str:
    def restore_number(match: re.Match[str]) -> str:
        number = match.group("number").translate(str.maketrans({"О": "0", "З": "3", "Д": "9"}))
        return f"TOY-{number} SECTION"

    return re.sub(
        r"\bTOY[.\s-]*(?P<number>[0-9ОЗД]{1,3})\s+SECTION\b",
        restore_number,
        text,
        flags=re.IGNORECASE,
    )


def normalize_section_code(value: str, source_document: str = "", context: str = "") -> str:
    code = value.replace(".", "-").upper().strip("-")
    code = code.replace("•", "-").replace("_", "-")
    code = code.replace("В", "B").replace("М", "M").replace("У", "Y").replace("Ь", "W").replace("Ј", "J")
    code = code.replace("Л", "A").replace("И", "N").replace("П", "FA")
    code = re.sub(r"[^A-Z0-9-]+", "", code)
    if source_document.lower().startswith("bmw") and code.startswith(("BMYW", "BMWB", "BMV", "BMX")):
        suffix = re.sub(r"^[A-Z-]+", "", code)
        code = f"BMW-{suffix or ocr_suffix_to_digit(code[-1:])}"
    if "fiat" in source_document.lower() and not code.startswith("FA"):
        suffix = re.sub(r"[^0-9A-Z]+", "", code)
        suffix = ocr_suffix_to_digit(suffix[-1:]) if suffix else ""
        if suffix:
            code = f"FA-{suffix}"
    if source_document.lower().startswith("mercedes") and code.startswith("MB") and "-" not in code:
        code = "MB-" + ocr_suffix_to_digit(code[2:])
    if "asian" in source_document.lower() and re.fullmatch(r"F\d{1,3}", code) and "ACU" in context.upper():
        code = f"ACU-{code}"
    if "asian" in source_document.lower() and re.fullmatch(r"R\d{1,3}", code):
        code = f"HON-{code}"
    if "asian" in source_document.lower() and code == "SUZU-3" and "ASCENDER" in context.upper():
        code = "ISUZU-3"
    if "-" not in code:
        match = re.match(r"(?P<prefix>[A-Z]{2,5})(?P<num>[A-Z]?\d{1,4})$", code)
        if match:
            prefix = match.group("prefix")
            num = match.group("num")
            if prefix in {"AVB", "AW"}:
                prefix = "AV"
            code = f"{prefix}-{num}"
    if "domestic" in source_document.lower():
        gm_continued = re.fullmatch(r"GM-([FBKP])4(\d{2})", code)
        if gm_continued and "CONTINU" in context.upper():
            code = f"GM-{gm_continued.group(1)}{int(gm_continued.group(2))}"
    lower_document = source_document.lower()
    for marker, repairs in DOCUMENT_PREFIX_REPAIRS.items():
        if marker in lower_document and "-" in code:
            prefix, suffix = code.split("-", 1)
            if marker == "audi" and prefix == "AVA" and suffix == "5":
                suffix = "15"
            code = f"{repairs.get(prefix, prefix)}-{suffix}"
            break
    if "CONTINU" in code or not re.search(r"\d", code):
        return ""
    if not re.fullmatch(r"[A-Z]{2,10}-[A-Z0-9]{1,4}", code):
        return ""
    return code


def ocr_suffix_to_digit(value: str) -> str:
    table = {"T": "1", "I": "1", "L": "1", "O": "0", "Q": "0", "S": "5", "B": "8"}
    return "".join(table.get(char, char) for char in value)


def extract_section_fields(text: str) -> dict:
    fields: dict[str, object] = defaultdict(list)

    fields["fcc_ids"] = sorted(set(match.group("fcc") for match in FCC_RE.finditer(text)))
    fields["frequencies"] = sorted(set(match.group("freq") + " MHz" for match in FREQ_RE.finditer(text)))

    chip = CHIP_RE.search(text)
    if chip:
        fields["chip"] = chip.group("chip").strip()

    code_series = CODE_SERIES_RE.search(text)
    if code_series:
        fields["code_series"] = code_series.group("series").strip()

    for word in ("PIN", "Gateway", "Lishi", "Determinator", "AccuReader", "STRATTEC"):
        if word.lower() in text.lower():
            fields["detected_topics"].append(word)

    return dict(fields)
