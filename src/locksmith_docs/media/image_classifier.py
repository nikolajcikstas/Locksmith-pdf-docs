from __future__ import annotations

from dataclasses import dataclass


TEXT_HEAVY_KINDS = {
    "table",
    "programmer_matrix",
    "strattec_table",
    "spacing_depth_chart",
    "paragraph_note",
}


@dataclass(frozen=True)
class ImageDecision:
    public_kind: str
    publish_image: bool
    extract_text: bool
    render_as_schema: bool
    reason: str


def decide_image_use(kind: str, has_text: bool, explains_physical_step: bool) -> ImageDecision:
    if kind in TEXT_HEAVY_KINDS or has_text:
        return ImageDecision(
            public_kind="structured_data",
            publish_image=False,
            extract_text=True,
            render_as_schema=True,
            reason="Text/table content must be extracted and rendered as HTML or a generated schema.",
        )

    if explains_physical_step:
        return ImageDecision(
            public_kind="procedure_image",
            publish_image=True,
            extract_text=False,
            render_as_schema=False,
            reason="Image clarifies a physical operation that text alone may not explain.",
        )

    return ImageDecision(
        public_kind="discard",
        publish_image=False,
        extract_text=False,
        render_as_schema=False,
        reason="Image is decorative or redundant for field work.",
    )
