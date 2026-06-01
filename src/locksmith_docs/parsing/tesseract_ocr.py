from __future__ import annotations

import csv
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from locksmith_docs.parsing.models import OcrWord


def preprocess_for_toc(image_path: Path, scale: int = 3) -> Path:
    image = Image.open(image_path).convert("L")
    image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
    image = ImageEnhance.Contrast(image).enhance(1.9)
    image = image.filter(ImageFilter.SHARPEN)
    image = image.point(lambda px: 255 if px > 185 else 0)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    image.save(tmp_path)
    return tmp_path


def run_tesseract_tsv(image_path: Path, psm: int = 6) -> list[OcrWord]:
    result = subprocess.run(
        [
            "tesseract",
            str(image_path),
            "stdout",
            "--psm",
            str(psm),
            "-l",
            "eng",
            "tsv",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Tesseract failed")
    return parse_tsv(result.stdout)


def parse_tsv(tsv_text: str) -> list[OcrWord]:
    words: list[OcrWord] = []
    reader = csv.DictReader(tsv_text.splitlines(), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf") or -1)
        except ValueError:
            confidence = -1
        if confidence < 0:
            continue
        words.append(
            OcrWord(
                text=text,
                left=int(row["left"]),
                top=int(row["top"]),
                width=int(row["width"]),
                height=int(row["height"]),
                confidence=confidence,
                block_num=int(row.get("block_num") or 0),
                par_num=int(row.get("par_num") or 0),
                line_num=int(row.get("line_num") or 0),
                word_num=int(row.get("word_num") or 0),
            )
        )
    return words


def words_to_tsv_dicts(words: list[OcrWord]) -> list[dict]:
    return [word.__dict__ for word in words]
