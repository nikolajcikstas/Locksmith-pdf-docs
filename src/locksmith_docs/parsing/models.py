from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VehicleApplicationCandidate:
    make: str
    model: str
    year_from: int
    year_to: int
    system_type: str
    system_code: str
    source_document: str
    source_page: int
    confidence: float


@dataclass
class SystemSectionCandidate:
    code: str
    title: str
    source_document: str
    page_start: int
    page_end: int | None
    raw_text: str
    fields: dict = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class AssetCandidate:
    system_code: str
    source_document: str
    source_page: int
    crop_box: tuple[int, int, int, int]
    kind: str
    usefulness_score: float
    reason: str


@dataclass(frozen=True)
class OcrWord:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float
    block_num: int
    par_num: int
    line_num: int
    word_num: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def mid_y(self) -> float:
        return self.top + self.height / 2

    @property
    def mid_x(self) -> float:
        return self.left + self.width / 2
