from __future__ import annotations

import re
from typing import Iterable

from locksmith_docs.parsing.models import SystemSectionCandidate, VehicleApplicationCandidate
from locksmith_docs.parsing.canonical_models import CANONICAL_MODELS, canonicalize_model
from locksmith_docs.parsing.toc_parser import detect_type


YEAR_RANGE_RE = re.compile(r"\b(?P<start>(?:19|20)\d{2})\s*[-\u2013]\s*(?P<end>\d{0,4})\b")
SECTION_STOP_RE = re.compile(
    r"\b(?:CODES?|COOES|CARD#?|CARO#?|STYLE|STYI_E|MACS|StartCut|S1artCut|CuttoCut|AirBags|Ign|Retainer|KEYS?|MASTER|"
    r"Chip|Programmer|Diagnostic|Cloner|Decoders?|Lishi|Determinator|AccuReader|SPACING|DEPTHS)\b",
    re.IGNORECASE,
)
PAREN_MAKE_RE = re.compile(r"\((?P<make>[A-Za-z][A-Za-z -]{1,24})\)")


PREFIX_MAKE = {
    "ACU": "Acura",
    "HON": "Honda",
    "ISUZU": "Isuzu",
    "LSUZU": "Isuzu",
    "SUZL": "Suzuki",
    "MAZ": "Mazda",
    "MITS": "Mitsubishi",
    "MLTS": "Mitsubishi",
    "WTS": "Mitsubishi",
    "NIS": "Nissan",
    "NLS": "Nissan",
    "SB": "Subaru",
    "LEX": "Lexus",
    "TOY": "Toyota",
    "HK": "Kia",
    "CHRY": "Chrysler",
    "DGE": "Dodge",
    "DODGE": "Dodge",
    "DCE": "Dodge",
    "FORD": "Ford",
    "GM": "Chevrolet",
    "CM": "Chevrolet",
    "VOL": "Volvo",
    "AV": "Audi",
    "BMW": "BMW",
    "MB": "Mercedes-Benz",
}

KNOWN_MAKES = {
    "Acura",
    "Audi",
    "BMW",
    "Buick",
    "Cadillac",
    "Chevrolet",
    "Chrysler",
    "Dodge",
    "Ford",
    "GMC",
    "Honda",
    "Hummer",
    "Infiniti",
    "Isuzu",
    "Jaguar",
    "Jeep",
    "Kia",
    "Land Rover",
    "Lexus",
    "Lincoln",
    "Mazda",
    "Mercedes",
    "Mercedes-Benz",
    "Mercury",
    "Mini",
    "Mitsubishi",
    "Nissan",
    "Oldsmobile",
    "Plymouth",
    "Pontiac",
    "Porsche",
    "Saab",
    "Saturn",
    "Scion",
    "Subaru",
    "Suzuki",
    "Toyota",
    "Volkswagen",
    "Volvo",
    "Fiat",
    "Alfa Romeo",
}

DOCUMENT_MAKES = {
    "asian": {
        "Acura", "Honda", "Infiniti", "Isuzu", "Kia", "Lexus", "Mazda",
        "Mitsubishi", "Nissan", "Scion", "Subaru", "Suzuki", "Toyota",
    },
    "domestic": {
        "Buick", "Cadillac", "Chevrolet", "Chrysler", "Dodge", "Ford", "GMC",
        "Jeep", "Lincoln", "Mercury", "Oldsmobile", "Pontiac", "Saturn",
    },
    "audi": {"Audi", "Volkswagen", "Porsche"},
    "bmw": {"BMW", "Mini"},
    "fiat": {"Fiat", "Alfa Romeo"},
    "jaguar": {"Jaguar", "Land Rover"},
    "mercedes": {"Mercedes-Benz"},
    "saab": {"Saab"},
    "volvo": {"Volvo"},
}


def extract_vehicle_applications_from_sections(
    sections: Iterable[SystemSectionCandidate | dict],
) -> list[VehicleApplicationCandidate]:
    candidates: list[VehicleApplicationCandidate] = []
    seen: set[tuple[str, str, int, int, str]] = set()

    for section in sections:
        data = section.__dict__ if hasattr(section, "__dict__") else dict(section)
        raw_text = data.get("raw_text") or ""
        code = data.get("code") or ""
        source_document = data.get("source_document") or ""
        source_page = int(data.get("page_start") or 0)
        prefix = code.split("-")[0].upper()
        default_make = infer_default_make(prefix, source_document)

        for year_from, year_to, make, model in extract_canonical_applications(raw_text, source_document, default_make):
            key = (make.lower(), model.lower(), year_from, year_to, code)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                VehicleApplicationCandidate(
                    make=make,
                    model=model,
                    year_from=year_from,
                    year_to=year_to,
                    system_type=detect_type(raw_text[:900]),
                    system_code=code,
                    source_document=source_document,
                    source_page=source_page,
                    confidence=0.92,
                )
            )

        for year_from, year_to, model_text in extract_year_model_chunks(raw_text):
            make, model = split_make_model(model_text, default_make)
            if not make or not model or is_bad_model(model):
                continue
            model, canonical_score = canonicalize_model(make, model)
            key = (make.lower(), model.lower(), year_from, year_to, code)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                VehicleApplicationCandidate(
                    make=make,
                    model=model,
                    year_from=year_from,
                    year_to=year_to,
                    system_type=detect_type(raw_text[:900]),
                    system_code=code,
                    source_document=source_document,
                    source_page=source_page,
                    confidence=score_vehicle_candidate(make, model, default_make, canonical_score),
                )
            )

    return candidates


def extract_canonical_applications(raw_text: str, source_document: str, default_make: str) -> list[tuple[int, int, str, str]]:
    text = normalize_vehicle_scan_text(raw_text[:1800])
    possible_makes = possible_makes_for_document(source_document, default_make)
    matches: list[tuple[int, int, str, str]] = []
    for make in possible_makes:
        models = sorted(CANONICAL_MODELS.get(make, []), key=len, reverse=True)
        for model in models:
            model_re = model_regex(model)
            pattern = re.compile(
                rf"(?P<years>(?:19|20)\d{{2}}\s*[-\u2013]\s*\d{{2,4}}).{{0,20}}?(?P<model>{model_re})(?=\s|\(|\||,|$)",
                re.IGNORECASE,
            )
            for match in pattern.finditer(text):
                year_from, year_to = expand_year_range_from_text(match.group("years"))
                if not year_from:
                    continue
                nearby = text[match.end(): match.end() + 30]
                named_make = normalize_make_from_nearby(nearby)
                if named_make and named_make != make:
                    continue
                if not named_make and make != default_make:
                    continue
                if len(re.sub(r"[^A-Za-z0-9]", "", model)) <= 2 and not named_make:
                    continue
                matches.append((year_from, year_to, make, model))
    return matches


def possible_makes_for_document(source_document: str, default_make: str) -> set[str]:
    lower = source_document.lower()
    for marker, makes in DOCUMENT_MAKES.items():
        if marker in lower:
            return makes
    return {default_make} if default_make else set()


def normalize_vehicle_scan_text(text: str) -> str:
    text = normalize_ocr_text(text)
    replacements = {
        "ЗОО": "300",
        "З00": "300",
        "0ut": "Out",
        "1mpala": "Impala",
        "GiuIa": "Giulia",
        "TIХ": "TLX",
        "М0Х": "MDX",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = text.replace("(WW)", "(VW)").replace("(WwW)", "(VW)")
    text = text.replace("(Aud)", "(Audi)").replace("(Au)", "(Audi)")
    text = re.sub(r"\bAS(?=\s*\(Audi\))", "A5", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAG\s*/\s*S[E6](?=\s*\(Audi\))", "A6 / S6", text, flags=re.IGNORECASE)
    return text


def model_regex(model: str) -> str:
    pieces = []
    for char in model:
        if char.isspace():
            pieces.append(r"\s*")
        elif char == "-":
            pieces.append(r"[-\s]?")
        elif char == "/":
            pieces.append(r"\s*/\s*")
        elif char == "0":
            pieces.append(r"[0O]")
        elif char == "1":
            pieces.append(r"[1I]")
        else:
            pieces.append(re.escape(char))
    return "".join(pieces)


def expand_year_range_from_text(text: str) -> tuple[int, int]:
    match = YEAR_RANGE_RE.search(text)
    if not match:
        return 0, 0
    return expand_year_range(match.group("start"), match.group("end"))


def normalize_make_from_nearby(text: str) -> str:
    paren = PAREN_MAKE_RE.search(text)
    return normalize_make(paren.group("make")) if paren else ""


def extract_year_model_chunks(raw_text: str) -> list[tuple[int, int, str]]:
    header = normalize_ocr_text(raw_text[:1000])
    first_operational_block = re.search(r"\b(?:KEYS?|MASTER|Chip|Programmer|Diagnostic|Cloner)\b", header, re.IGNORECASE)
    if first_operational_block:
        header = header[: first_operational_block.start()]
    matches = list(YEAR_RANGE_RE.finditer(header))
    chunks: list[tuple[int, int, str]] = []
    for index, match in enumerate(matches):
        year_from, year_to = expand_year_range(match.group("start"), match.group("end"))
        if not year_from:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(header)
        chunk = header[start:end]
        stop = SECTION_STOP_RE.search(chunk)
        if stop:
            chunk = chunk[: stop.start()]
        model = clean_model_text(chunk)
        if model:
            chunks.append((year_from, year_to, model))
    return chunks


def expand_year_range(raw_start: str, raw_end: str) -> tuple[int, int]:
    start = int(raw_start)
    if not 1900 <= start <= 2035:
        return 0, 0
    if not raw_end:
        return start, start
    if len(raw_end) == 4:
        end = int(raw_end)
        if end > 2035:
            end = int(str(start)[:2] + raw_end[:2])
        return start, end
    if len(raw_end) == 2:
        end = int(str(start)[:2] + raw_end)
        if end < start:
            end += 100
        return (0, 0) if end > 2035 or end - start > 50 else (start, end)
    if len(raw_end) == 3:
        # OCR often joins a two-digit end year with the first digit of the
        # next model, for example "1975-845Series" for "1975-84 5 Series".
        end = int(str(start)[:2] + raw_end[:2])
        return (start, end) if start <= end <= min(2035, start + 50) else (0, 0)
    end = int(raw_end)
    return (start, end) if start <= end <= min(2035, start + 50) else (0, 0)


def normalize_ocr_text(text: str) -> str:
    replacements = {
        "\u0421": "C",
        "\u0441": "c",
        "\u0410": "A",
        "\u0430": "a",
        "\u0415": "E",
        "\u0435": "e",
        "\u041e": "O",
        "\u043e": "o",
        "\u0420": "P",
        "\u0440": "p",
        "\u0425": "X",
        "\u0445": "x",
        "\u041d": "H",
        "\u043d": "h",
        "\u041a": "K",
        "\u043a": "k",
        "\u041c": "M",
        "\u043c": "m",
        "\u0422": "T",
        "\u0442": "t",
        "\u0456": "i",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return " ".join(text.replace("\n", " ").split())


def clean_model_text(text: str) -> str:
    text = re.sub(r"\b(?:19|20)\d{2}\s*[-\u2013]\s*\d{2,4}\b", " ", text)
    text = re.sub(r"\b(?:prox|proximity|remote|fob|f0b|key|transponder|high sec|dbl sided|sided|ptox|ptox|p ox|p x|moxe?)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^A-Za-z0-9() /&.-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -/,")
    return text[:80].strip()


def split_make_model(model_text: str, default_make: str) -> tuple[str, str]:
    make = default_make
    model_text = fix_broken_parenthetical_makes(model_text)
    paren = PAREN_MAKE_RE.search(model_text)
    if paren:
        maybe_make = normalize_make(paren.group("make"))
        if maybe_make in KNOWN_MAKES:
            make = maybe_make
        model_text = PAREN_MAKE_RE.sub("", model_text)

    model_text = re.sub(r"\b(?:Acura|Honda|Nissan|Infiniti|Toyota|Lexus|Scion|Ford|Lincoln|Mercury|Chevrolet|Cadillac|Buick|GMC|Dodge|Chrysler|Jeep|Plymouth|Subaru|Mazda|Mitsubishi|Suzuki|Isuzu|Volvo|Saab|BMW|Mini|Mercedes|Audi|Volkswagen|Porsche)\b", "", model_text, flags=re.IGNORECASE)
    model = re.sub(r"\s+", " ", model_text).strip(" -/,")
    model = normalize_model(model)
    make, model = override_make_from_model(make, model)
    return make, model


def normalize_make(value: str) -> str:
    aliases = {
        "Chevy": "Chevrolet",
        "Gmc": "GMC",
        "Vw": "Volkswagen",
        "Vwy": "Volkswagen",
        "V": "Volkswagen",
        "Onfniti": "Infiniti",
        "Linc": "Lincoln",
        "Lincf": "Lincoln",
        "LincOl": "Lincoln",
        "Co": "Lincoln",
        "Co1": "Lincoln",
        "Emc": "GMC",
        "Olds": "Oldsmobile",
        "Ge": "Geo",
        "Mercedes": "Mercedes-Benz",
        "Benz": "Mercedes-Benz",
    }
    clean = re.sub(r"[^A-Za-z -]+", "", value).strip().title()
    return aliases.get(clean, clean)


def normalize_model(value: str) -> str:
    replacements = {
        "1LX": "ILX",
        "T X": "TLX",
        "C1arity": "Clarity",
        "CIarity": "Clarity",
        "FueI ce": "Fuel Cell",
        "P1 In": "Plug-In",
        "PiIot": "Pilot",
        "E1ement": "Element",
        "SoIara": "Solara",
        "PreIude": "Prelude",
        "Odyseey": "Odyssey",
        "0dyseey": "Odyssey",
        "GaIant": "Galant",
        "Ga1ant": "Galant",
        "GaIar- t": "Galant",
        "AT o": "Amigo",
        "EcIipse": "Eclipse",
        "0utback": "Outback",
        "0utIander": "Outlander",
        "ExpIorer": "Explorer",
        "EscaIade": "Escalade",
        "BonneviIIe": "Bonneville",
        "Fu11": "Full",
        "Pick p": "Pick Up",
        "1-Miev": "i-MiEV",
        "WPh etow": "Phaeton",
        "To a e m": "Touareg",
        "JetW": "Jetta",
        "JettaMvw": "Jetta",
        "Coteadoxvw": "Corrado",
        "Passatxvw": "Passat",
        "Passatww": "Passat",
        "Tiguaniw": "Tiguan",
        "Tiguan(w": "Tiguan",
        "Touareg) eS copes ste carve me": "Touareg",
        "WA4/S4": "A4 / S4",
        "A4MS4": "A4 / S4",
        "A4 S4": "A4 / S4",
        "A S0": "A6 / S6",
        "A6 S6": "A6 / S6",
        "QOOT-04TT": "TT",
        "T2Caymam": "Cayman",
        "Cabriolet(vww t": "Cabriolet",
        "GoIf/GTi": "Golf / GTI",
        "Golf/GTi": "Golf / GTI",
        "FX35 FX45": "FX35 / FX45",
        "G35 (2DR": "G35",
        "G35 (4DR": "G35",
        "1S 350C": "IS 350C",
        "B1ackwood": "Blackwood",
        "Town Ca": "Town Car",
        "S15 sonoma p": "Sonoma",
        "Sierra Full Size Pick Up": "Sierra",
        "Savanna": "Savana",
        "S01stice": "Solstice",
        "Continental (": "Continental",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    value = re.sub(r"\bCI\b", "CL", value)
    value = re.sub(r"\b(?:AW|AV|AVB)?\.?\d{1,2}\b", "", value)
    value = re.sub(r"\b(?:P x|p ox|CA ITL|StanCHt|Star1Cu|S1anCut|SIanCut).*", "", value)
    value = re.sub(r"\((?:ho|honda|ford|chevy|emc|co|vw|m)[^)]*\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:St3rtCut|Cutt0CUhA|Ceterminatr|Air a|CX2|111-pt|EurovaMvw).*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\bTouareg\b.*", "Touareg", value, flags=re.IGNORECASE)
    value = re.sub(r"\bTiguan\b.*", "Tiguan", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:GoIf|Golf)\s*/\s*GTI.*", "Golf / GTI", value, flags=re.IGNORECASE)
    value = re.sub(r"\bA4\s*/\s*S4.*", "A4 / S4", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    if value.lower() == "cc":
        return "CC"
    return value.strip(" -/()")


def fix_broken_parenthetical_makes(value: str) -> str:
    replacements = {
        "(vwY": "(VW)",
        "(vwy": "(VW)",
        "(vww": "(VW)",
        "xvw)": "(VW)",
        "onfniti)": "(Infiniti)",
        "Linc01f)": "(Lincoln)",
        "Emc)": "(GMC)",
        "GE0)": "(Geo)",
        "01ds)": "(Oldsmobile)",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    return value


def override_make_from_model(make: str, model: str) -> tuple[str, str]:
    overrides = {
        "A3": "Audi",
        "A4": "Audi",
        "A4 / S4": "Audi",
        "A5": "Audi",
        "A6 / S6": "Audi",
        "A7": "Audi",
        "A8 / S8": "Audi",
        "Q3": "Audi",
        "Q5": "Audi",
        "Q7": "Audi",
        "Q8": "Audi",
        "TT": "Audi",
        "Beetle": "Volkswagen",
        "Cabriolet": "Volkswagen",
        "CC": "Volkswagen",
        "Corrado": "Volkswagen",
        "EOS": "Volkswagen",
        "Eurovan": "Volkswagen",
        "Golf": "Volkswagen",
        "Golf / GTI": "Volkswagen",
        "Jetta": "Volkswagen",
        "Passat": "Volkswagen",
        "Rabbit": "Volkswagen",
        "Tiguan": "Volkswagen",
        "Phaeton": "Volkswagen",
        "Touareg": "Volkswagen",
        "928": "Porsche",
        "911": "Porsche",
        "718 Boxster": "Porsche",
        "718 Cayman": "Porsche",
        "Boxster": "Porsche",
        "Cayman": "Porsche",
        "Cayenne": "Porsche",
        "Macan": "Porsche",
        "Panamera": "Porsche",
        "Taycan": "Porsche",
        "FX35 / FX45": "Infiniti",
        "G35": "Infiniti",
        "Blackwood": "Lincoln",
        "Town Car": "Lincoln",
        "Sonoma": "GMC",
        "Sierra": "GMC",
        "SRX": "Cadillac",
        "DTS": "Cadillac",
    }
    return overrides.get(model, make), model


def score_vehicle_candidate(make: str, model: str, default_make: str, canonical_score: float = 0.0) -> float:
    score = 0.76
    if canonical_score >= 0.78:
        score += 0.08
    if make and default_make and make != default_make:
        score += 0.08
    if any(char.isdigit() for char in model):
        score -= 0.06
    if len(model) > 24:
        score -= 0.12
    if re.search(r"[.)]\s*$|\b(?:0sm|onfniti|Emc|Linc01f|GE0)\b", model, re.IGNORECASE):
        score -= 0.16
    return max(0.35, min(0.9, round(score, 2)))


def infer_default_make(prefix: str, source_document: str) -> str:
    if prefix in PREFIX_MAKE:
        return PREFIX_MAKE[prefix]
    for token in KNOWN_MAKES:
        if token.lower() in source_document.lower():
            return token
    return ""


def is_bad_model(model: str) -> bool:
    lowered = model.lower()
    if len(model) < 2 or len(model) > 45:
        return True
    bad_tokens = (
        "code series",
        "depth",
        "spacing",
        "method",
        "factory",
        "reader",
        "decoder",
        "fcc",
        "mhz",
        "part",
        "section",
        "codes",
        "cooes",
        "caro",
        "startcut",
        "cutto",
        "cuttc",
        "cut10",
        "stancut",
        ".",
        "airbags",
        "air bags",
        "ignretainer",
        "itl",
        "mode1s",
        "optional",
        "0ptional",
        "overlap",
        "overiap",
        "tid-year",
        "cjlt",
    )
    if any(token in lowered for token in bad_tokens):
        return True
    letters = re.sub(r"[^A-Za-z]", "", model)
    if len(letters) < 2:
        return True
    if re.fullmatch(r"[PpOoXx0-9 /.-]+", model):
        return True
    return False
