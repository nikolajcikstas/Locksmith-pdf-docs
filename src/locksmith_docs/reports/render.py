from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from typing import Any

from locksmith_docs.media.extract_assets import render_original_diagram_svg
from locksmith_docs.web.rendering import esc


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def render_list(items: Sequence[Any]) -> str:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not items:
        return ""
    if isinstance(items[0], Mapping):
        keys = list(items[0].keys())
        return render_records_table([(key, key.replace("_", " ").title()) for key in keys], items)  # type: ignore[arg-type]
    return "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"


def render_kv_table(mapping: Mapping[str, Any]) -> str:
    if not isinstance(mapping, Mapping):
        return ""
    clean_mapping = {key: value for key, value in mapping.items() if has_value(value)}
    if not clean_mapping:
        return ""
    rows = "".join(f"<tr><th>{esc(key)}</th><td>{render_cell(value)}</td></tr>" for key, value in clean_mapping.items())
    return f'<div class="table-scroll"><table class="compact-table kv-table">{rows}</table></div>'


def render_records_table(headers: Sequence[tuple[str, str]], records: Sequence[Mapping[str, Any]]) -> str:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return ""
    records = [record for record in records if isinstance(record, Mapping)]
    if not records:
        return ""
    visible_headers = [
        (key, label)
        for key, label in headers
        if any(str(record.get(key, "") or "").strip() for record in records)
    ]
    if not visible_headers:
        return ""
    head = "".join(f"<th>{esc(label)}</th>" for _key, label in visible_headers)
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td class=\"{'notes-cell' if key in {'notes', 'action', 'reference'} else ''}\">{render_cell(record.get(key, ''))}</td>"
            for key, _label in visible_headers
        )
        + "</tr>"
        for record in records
    )
    layout_class = " wide-table" if len(visible_headers) >= 4 else ""
    return f'<div class="table-scroll"><table class="compact-table{layout_class}"><tr>{head}</tr>{rows}</table></div>'


def render_cell(value: Any) -> str:
    if value is None or value == "":
        return '<span class="empty-cell">-</span>'
    if isinstance(value, Mapping):
        return render_kv_table(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return ", ".join(esc(item) for item in value) or '<span class="empty-cell">-</span>'
    return esc(value)


def has_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, str):
        return value.strip() not in {"", "-", "Check source", "Check source.", "Check source pages", "None", "n/a", "N/A"}
    if isinstance(value, Mapping):
        return any(has_value(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(has_value(item) for item in value)
    return True


def render_steps(records: Sequence[Mapping[str, Any]]) -> str:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return ""
    records = [record for record in records if isinstance(record, Mapping)]
    if not records:
        return ""
    return (
        '<ol class="steps">'
        + "".join(
            f"<li><strong>{esc(item.get('step', 'Step'))}</strong><p>{esc(item.get('detail', ''))}</p></li>"
            for item in records
        )
        + "</ol>"
    )


def render_checklist(groups: Mapping[str, Sequence[Any]]) -> str:
    if not isinstance(groups, Mapping) or not groups:
        return ""
    blocks = []
    for title, items in groups.items():
        blocks.append(
            f"""
            <div class="checklist-group">
              <h4>{esc(title)}</h4>
              {render_list(items)}
            </div>
            """
        )
    return '<div class="checklist-grid">' + "".join(blocks) + "</div>"


def render_cut_position_map(rows: Sequence[Mapping[str, Any]]) -> str:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return ""
    rows = [row for row in rows if isinstance(row, Mapping)]
    if not rows:
        return ""
    columns = range(1, 9)
    body = ""
    for row in rows:
        active = set(row.get("positions", []))
        cells = "".join(
            f"<td class=\"{'active' if col in active else 'empty'}\">{'x' if col in active else '-'}</td>"
            for col in columns
        )
        body += f"<tr><th>{esc(row.get('lock', ''))}</th>{cells}<td>{esc(row.get('note', ''))}</td></tr>"
    header = "".join(f"<th>{col}</th>" for col in columns)
    return f"""
    <table class="position-map">
      <tr><th>Lock</th>{header}<th>Field note</th></tr>
      {body}
    </table>
    """


def render_asset_cards(assets: Sequence[Mapping[str, Any]], placement: str, system: Mapping[str, Any] | None = None) -> str:
    cards = []
    for asset in assets if isinstance(assets, Sequence) and not isinstance(assets, (str, bytes)) else []:
        if not isinstance(asset, Mapping):
            continue
        if asset.get("visibility") != "public" or asset.get("kind") != "procedure_image":
            continue
        if (asset.get("placement") or "") != placement:
            continue
        diagram_data = asset.get("diagram_data") if isinstance(asset.get("diagram_data"), Mapping) else {}
        embedded_svg = ""
        if diagram_data:
            schema = enrich_diagram_schema_for_system(dict(diagram_data), system or {}, placement)
            embedded_svg = render_original_diagram_svg(schema)
            caption = str(schema.get("caption") or schema.get("title") or asset.get("rewritten_caption") or asset.get("title") or "Procedure image")
        else:
            caption = str(asset.get("rewritten_caption") or asset.get("title") or "Procedure image")
        if not embedded_svg:
            embedded_svg = inline_svg_from_public_path(str(asset.get("public_path") or ""))
        visual = (
            f'<div class="diagram-frame">{embedded_svg}</div>'
            if embedded_svg
            else f'<img src="{esc(asset.get("public_path", ""))}" alt="{esc(asset.get("title", "Procedure image"))}">'
        )
        cards.append(
            f"""
            <figure class="asset-card inline-asset">
              {visual}
              <figcaption>{esc(caption)}</figcaption>
            </figure>
            """
        )
    if not cards:
        return ""
    return f'<div class="asset-strip">{"".join(cards)}</div>'


def enrich_diagram_schema_for_system(schema: dict[str, Any], system: Mapping[str, Any], placement: str) -> dict[str, Any]:
    if placement != "making_key":
        return schema
    profile = mechanical_profile(system)
    if not profile:
        return schema
    schema.setdefault("blade_profile", profile)
    title_text = str(schema.get("title") or "").strip().lower()
    if not title_text or "lock" in title_text or "position" in title_text:
        schema["title"] = f"{profile} lock-position guide"
        schema.setdefault("subtitle", f"{profile} position map. Read positions from handle to tip.")
    return schema


def inline_svg_from_public_path(public_path: str) -> str:
    """Embed local generated SVGs directly in the report page."""
    if not public_path.startswith("/static/") or not public_path.endswith(".svg"):
        return ""
    svg_path = PROJECT_ROOT / public_path.lstrip("/")
    try:
        svg = svg_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if "<svg" not in svg[:200].lower() or "</svg>" not in svg.lower():
        return ""
    return svg


def safe_lock_part_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    safe = []
    for record in records if isinstance(records, Sequence) and not isinstance(records, (str, bytes)) else []:
        if not isinstance(record, Mapping):
            continue
        text = " ".join(str(value or "") for value in record.values())
        if not (
            re.search(r"\b(?:STRATTEC|ASP|PIN\s*kit|part|available|not\s+available)\b", text, re.IGNORECASE)
            or re.search(r"\b70\d{4}\b", text)
        ):
            continue
        if re.search(r"\b(?:method|decode|decoder|reader|determine|working key|disassemble|no\s*codes)\b", text, re.IGNORECASE):
            continue
        safe.append(record)
    return safe


def render_assets(assets: Sequence[Mapping[str, Any]]) -> str:
    return ""


def render_procedure_diagrams(system: Mapping[str, Any], placement: str) -> str:
    diagrams = system.get("procedure_diagrams")
    if not isinstance(diagrams, Sequence) or isinstance(diagrams, (str, bytes)):
        return ""
    cards = []
    for diagram in diagrams:
        if not isinstance(diagram, Mapping):
            continue
        if str(diagram.get("placement") or "") != placement:
            continue
        schema = enrich_diagram_schema_for_system(dict(diagram), system, placement)
        svg = render_original_diagram_svg(schema)
        caption = str(diagram.get("caption") or diagram.get("title") or "Procedure reference").strip()
        cards.append(
            f"""
            <figure class="asset-card inline-asset inline-diagram">
              <div class="diagram-frame">{svg}</div>
              <figcaption>{esc(caption)}</figcaption>
            </figure>
            """
        )
    if not cards:
        return ""
    return f'<div class="asset-strip diagram-strip">{"".join(cards)}</div>'


def mechanical_profile(system: Mapping[str, Any]) -> str:
    mechanical = system.get("mechanical_key") if isinstance(system.get("mechanical_key"), Mapping) else {}
    transponder = system.get("transponder") if isinstance(system.get("transponder"), Mapping) else {}
    profile = str(
        mechanical.get("test_key")
        or mechanical.get("ilco_keyway")
        or transponder.get("test_key")
        or ""
    ).strip()
    if "/" in profile:
        profile = " / ".join(part.strip() for part in profile.split("/")[:2] if part.strip())
    return profile[:36]


def render_standard_mechanical_diagram(system: Mapping[str, Any]) -> str:
    """Render an original blade-orientation SVG from verified mechanical facts."""
    diagrams = system.get("procedure_diagrams")
    if isinstance(diagrams, Sequence) and not isinstance(diagrams, (str, bytes)):
        for diagram in diagrams:
            if (
                isinstance(diagram, Mapping)
                and str(diagram.get("placement") or "") == "mechanical_key"
                and str(diagram.get("visual_type") or "") == "blade_orientation"
            ):
                return ""
    mechanical = system.get("mechanical_key") if isinstance(system.get("mechanical_key"), Mapping) else {}
    setup = mechanical.get("cutting_setup") if isinstance(mechanical.get("cutting_setup"), Mapping) else {}
    setup_text = " ".join(str(value) for value in setup.values()).lower()
    milling = str(mechanical.get("milling") or "")
    has_track_facts = (
        "cut track" in setup_text
        or ("left track" in setup_text and "right track" in setup_text)
        or "guide track" in setup_text
    )
    if "internal" not in milling.lower() or not has_track_facts:
        return ""
    profile = str(
        mechanical.get("test_key")
        or mechanical.get("ilco_keyway")
        or (system.get("transponder") or {}).get("test_key")
        or "Mechanical key"
    ).strip()
    schema = {
        "title": f"{profile} mechanical blade orientation",
        "placement": "mechanical_key",
        "caption": f"{profile} orientation reference showing the source-listed cut track and guide track.",
        "visual_type": "blade_orientation",
        "blade_profile": profile,
        "milling": milling,
        "cut_track": str(setup.get("cut_track") or "Cut track - apply cut depths here"),
        "guide_track": str(setup.get("guide_track") or "Guide track - clearance only"),
        "note": "Orientation reference only. Use the spacing and depth tables for cutting values.",
    }
    svg = render_original_diagram_svg(schema)
    if not svg:
        return ""
    return f"""
    <div class="asset-strip diagram-strip">
      <figure class="asset-card inline-asset inline-diagram">
        <div class="diagram-frame">{svg}</div>
        <figcaption>{esc(schema["caption"])}</figcaption>
      </figure>
    </div>
    """


def has_mechanical_diagram_asset(assets: Sequence[Mapping[str, Any]]) -> bool:
    for asset in assets if isinstance(assets, Sequence) and not isinstance(assets, (str, bytes)) else []:
        if not isinstance(asset, Mapping) or str(asset.get("placement") or "") != "mechanical_key":
            continue
        text = " ".join(str(asset.get(key) or "") for key in ("title", "rewritten_caption", "public_path")).lower()
        data = asset.get("diagram_data") if isinstance(asset.get("diagram_data"), Mapping) else {}
        data_text = jsonish_text(data).lower()
        if any(token in f"{text} {data_text}" for token in ("orientation", "blade", "cut track", "guide track")):
            return True
    return False


def jsonish_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(jsonish_text(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(jsonish_text(item) for item in value)
    return str(value or "")


def panel(title: str, content: str, extra_class: str = "") -> str:
    if not content.strip():
        return ""
    return f'<article class="panel report-section {extra_class}"><h3>{esc(title)}</h3>{content}</article>'


def vehicle_display_title(vehicle: Mapping[str, Any]) -> str:
    requested_year = vehicle.get("requested_year") or vehicle.get("display_year")
    year_from = vehicle.get("year_from")
    year_to = vehicle.get("year_to")
    years = str(requested_year) if requested_year else str(year_from) if year_from == year_to else f"{year_from}-{year_to}"
    return " ".join(
        str(part).strip()
        for part in (years, vehicle.get("make"), vehicle.get("model"))
        if str(part).strip() and str(part).strip().lower() != "none"
    )


def render_vehicle_report(vehicle: Mapping[str, Any], system: Mapping[str, Any], assets: Sequence[Mapping[str, Any]] | None = None) -> str:
    assets = assets or []
    key_remote = system.get("key_remote") if isinstance(system.get("key_remote"), Mapping) else {}
    mechanical_key = system.get("mechanical_key") if isinstance(system.get("mechanical_key"), Mapping) else {}
    transponder = system.get("transponder") if isinstance(system.get("transponder"), Mapping) else {}
    programming = system.get("programming") if isinstance(system.get("programming"), Mapping) else {}
    making_key = system.get("making_key") if isinstance(system.get("making_key"), Mapping) else {}
    essentials = system.get("job_essentials") if isinstance(system.get("job_essentials"), Mapping) else {}
    essentials_html = render_kv_table(essentials)
    quick_html = render_list(system.get("quick_answer") or [])
    remote_summary_html = render_kv_table({
        "Remote type": key_remote.get("remote_type", ""),
        "Proximity option": key_remote.get("proximity_option", ""),
    })
    remote_html = remote_summary_html + render_records_table([
        ("years", "Years"), ("models", "Models"), ("part", "Part / Reference"),
        ("fcc_id", "FCC ID"), ("frequency", "Frequency"), ("emergency_blade", "Emergency blade"),
        ("buttons", "Buttons"), ("notes", "Notes")
    ], key_remote.get("known_options") or [])
    workflow_html = render_steps(making_key.get("field_workflow") or [])
    checklist_html = render_checklist(system.get("technician_checklist") if isinstance(system.get("technician_checklist"), Mapping) else {})
    source_facts_html = render_checklist(system.get("source_facts") if isinstance(system.get("source_facts"), Mapping) else {})
    mechanical_summary = render_kv_table({
        "Code series": mechanical_key.get("code_series", ""),
        "Style": mechanical_key.get("style", ""),
        "Card": mechanical_key.get("card", ""),
        "ITL #": mechanical_key.get("itl_number", ""),
        "Test key / blade reference": mechanical_key.get("test_key", ""),
        "ILCO / keyway": mechanical_key.get("ilco_keyway", ""),
        "MACS": mechanical_key.get("macs", ""),
        "Start cut": mechanical_key.get("start_cut", ""),
        "Cut-to-cut": mechanical_key.get("cut_to_cut", ""),
        "Milling": mechanical_key.get("milling", ""),
        "Air bags": mechanical_key.get("air_bags", ""),
        "Ignition retainer": mechanical_key.get("ignition_retainer", ""),
    })
    spacing_html = render_kv_table(mechanical_key.get("spacing") or {})
    depths_html = render_kv_table(mechanical_key.get("depths") or {})
    cutting_html = render_kv_table(mechanical_key.get("cutting_setup") or {})
    cut_map_html = render_cut_position_map(mechanical_key.get("cut_position_map") or [])
    mechanical_html = (
        mechanical_summary
        + (f'<div class="grid two"><div><h4>Spacing</h4>{spacing_html}</div><div><h4>Depths</h4>{depths_html}</div></div>' if spacing_html or depths_html else "")
        + (f"<h4>Cutting Setup</h4>{cutting_html}" if cutting_html else "")
        + (f"<h4>Cut Position Map</h4>{cut_map_html}" if cut_map_html else "")
    )
    transponder_summary = {
        key: value for key, value in transponder.items()
        if key != "cloner_tools"
    }
    cloner_html = render_records_table(
        [("name", "Manufacturer"), ("model", "Machine"), ("status", "Clone support")],
        transponder.get("cloner_tools") or [],
    )
    programming_html = render_kv_table({
        "PIN required": programming.get("pin_required", ""),
        "Factory tool": programming.get("factory_tool", ""),
        "System requirement": programming.get("system_requirement", ""),
        "PIN guidance": programming.get("pin_guidance", ""),
        "Tool guidance": programming.get("tool_guidance", ""),
        "Cloning": programming.get("cloning", ""),
    }) + render_records_table([("name", "Tool"), ("status", "Status")], programming.get("tools") or [])
    making_html = (f"<p>{esc(making_key.get('code_availability', ''))}</p>" if has_value(making_key.get("code_availability")) else "") + render_list(making_key.get("methods") or [])

    return f"""
    <section class="report">
      <div class="report-header">
        <div>
          <p class="eyebrow">Matched vehicle</p>
          <h2>{esc(vehicle_display_title(vehicle))}</h2>
          <p class="subline">System {esc(system.get('code'))} - {esc(system.get('system_type') or system.get('type'))}</p>
        </div>
        <div class="report-badges">
          <span>Field report</span>
          <span>Structured reference</span>
        </div>
      </div>

      <div class="grid two">
        {panel("At a Glance", essentials_html, "priority")}
        {panel("Quick Answer", quick_html)}
      </div>

      {panel("Compatible Remote Options", remote_html + render_asset_cards(assets, "key_remote") + render_procedure_diagrams(system, "key_remote"))}

      {panel("All Keys Lost / Field Workflow", workflow_html)}
      {panel("Technician Checklist", checklist_html)}
      {panel("Verified Identifiers", source_facts_html)}

      {panel("Emergency Blade / Mechanical Key", mechanical_html + render_asset_cards(assets, "mechanical_key", system) + ("" if has_mechanical_diagram_asset(assets) else render_standard_mechanical_diagram(system)) + render_procedure_diagrams(system, "mechanical_key"))}

      <div class="grid two">
        {panel("Transponder", render_kv_table(transponder_summary) + (f"<h4>Cloner Machine Information</h4>{cloner_html}" if cloner_html else ""))}
        {panel("Programming / Diagnostic", programming_html + render_asset_cards(assets, "programming") + render_procedure_diagrams(system, "programming"))}
      </div>

      {panel("Making a Working Key", making_html + render_asset_cards(assets, "making_key", system) + render_procedure_diagrams(system, "making_key"), "procedure-panel")}
      {panel("Decoders / Readers", render_records_table([("tool", "Tool"), ("reference", "Reference")], system.get("decoders") or []))}

      {panel("Field Notes", render_list(making_key.get("field_notes") or []))}
      {panel("Detailed Service Notes", render_list(making_key.get("service_notes") or []))}

      {panel("Troubleshooting", render_records_table([("issue", "Situation"), ("action", "Recommended action")], system.get("troubleshooting") or []))}
      {render_assets(assets)}

      <div class="grid two">
        {panel("Lock Parts", render_records_table([
            ("years", "Years"), ("models", "Models"), ("ignition_lock", "Ignition"),
            ("door_lock", "Door"), ("trunk_lock", "Trunk"), ("pin_kit", "PIN kit")
          ], safe_lock_part_records(system.get("lock_parts") or [])))}
        {panel("Warnings", render_list(system.get("warnings") or []), "warning")}
      </div>
    </section>
    """
