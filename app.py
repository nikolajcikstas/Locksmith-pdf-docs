import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
CATALOG_PATH = BASE_DIR / "data" / "catalog.json"
IMPORTED_PAGES_PATH = BASE_DIR / "data" / "imported_pages.json"


def load_catalog():
    with CATALOG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_imported_pages():
    if not IMPORTED_PAGES_PATH.exists():
        return []
    with IMPORTED_PAGES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def option_tags(values, selected=""):
    tags = ['<option value="">Select...</option>']
    for value in values:
        mark = " selected" if value == selected else ""
        tags.append(f'<option value="{escape(value)}"{mark}>{escape(value)}</option>')
    return "\n".join(tags)


def normalize_search(value):
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


def find_vehicle(catalog, make, model, year):
    try:
        year_int = int(year)
    except (TypeError, ValueError):
        return None

    for item in catalog["vehicles"]:
        if item["make"].lower() != make.lower():
            continue
        if item["model"].lower() != model.lower():
            continue
        if item["year_from"] <= year_int <= item["year_to"]:
            return item
    return None


def find_system(catalog, code):
    return next((s for s in catalog["systems"] if s["code"] == code), None)


def make_snippet(text, terms, size=300):
    lowered = text.lower()
    positions = [lowered.find(term.lower()) for term in terms if term and lowered.find(term.lower()) >= 0]
    start = max(min(positions) - 90, 0) if positions else 0
    snippet = text[start : start + size].replace("\n", " ")
    if start > 0:
        snippet = "..." + snippet
    if start + size < len(text):
        snippet += "..."
    return snippet


def find_ocr_matches(make, model, year, limit=10):
    terms = [term for term in [make, model, year] if term]
    normalized_terms = [normalize_search(term) for term in terms if normalize_search(term)]
    matches = []

    for doc in load_imported_pages():
        for page in doc.get("pages", []):
            text = page.get("ocr_text") or ""
            normalized_text = normalize_search(text)
            score = sum(1 for term in normalized_terms if term in normalized_text)
            if score < 2:
                continue
            matches.append(
                {
                    "document": doc["name"],
                    "page_number": page["page_number"],
                    "score": score,
                    "snippet": make_snippet(text, terms),
                }
            )

    matches.sort(key=lambda item: (-item["score"], item["document"], item["page_number"]))
    return matches[:limit]


def render_list(items):
    if items and isinstance(items[0], dict):
        keys = list(items[0].keys())
        headers = [(key, key.replace("_", " ").title()) for key in keys]
        return render_records_table(headers, items)
    return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"


def render_kv_table(mapping):
    rows = "".join(
        f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>"
        for key, value in mapping.items()
    )
    return f'<table class="compact-table">{rows}</table>'


def render_records_table(headers, records):
    head = "".join(f"<th>{escape(label)}</th>" for _key, label in headers)
    rows = "".join(
        "<tr>" + "".join(f"<td>{escape(record.get(key, ''))}</td>" for key, _label in headers) + "</tr>"
        for record in records
    )
    return f'<table class="compact-table"><tr>{head}</tr>{rows}</table>'


def render_steps(records):
    return (
        '<ol class="steps">'
        + "".join(
            f"<li><strong>{escape(item['step'])}</strong><p>{escape(item['detail'])}</p></li>"
            for item in records
        )
        + "</ol>"
    )


def render_checklist(groups):
    blocks = []
    for title, items in groups.items():
        blocks.append(
            f"""
            <div class="checklist-group">
              <h4>{escape(title)}</h4>
              {render_list(items)}
            </div>
            """
        )
    return '<div class="checklist-grid">' + "".join(blocks) + "</div>"


def render_cut_position_map(rows):
    columns = range(1, 9)
    body = ""
    for row in rows:
        active = set(row["positions"])
        cells = "".join(
            f"<td class=\"{'active' if col in active else 'empty'}\">{'x' if col in active else '-'}</td>"
            for col in columns
        )
        body += f"<tr><th>{escape(row['lock'])}</th>{cells}<td>{escape(row['note'])}</td></tr>"
    header = "".join(f"<th>{col}</th>" for col in columns)
    return f"""
    <table class="position-map">
      <tr><th>Lock</th>{header}<th>Field note</th></tr>
      {body}
    </table>
    """


def render_assets(system):
    cards = []
    for asset in system.get("assets", []):
        if asset.get("visibility") != "public":
            continue
        if asset.get("kind") != "procedure_image":
            continue
        cards.append(
            f"""
            <figure class="asset-card">
              <img src="{escape(asset['path'])}" alt="{escape(asset['title'])}">
              <figcaption>{escape(asset['title'])}</figcaption>
            </figure>
            """
        )
    if not cards:
        return ""
    return f"""
    <article class="panel">
      <h3>Procedure Images</h3>
      <p class="subline">Only images that clarify a physical operation are shown. Tables and notes are rendered as text.</p>
      <div class="asset-grid">{''.join(cards)}</div>
    </article>
    """


def render_report(vehicle, system):
    remote = system["key_remote"]
    key = system["mechanical_key"]
    transponder = system["transponder"]
    programming = system["programming"]

    return f"""
    <section class="report">
      <div class="report-header">
        <div>
          <p class="eyebrow">Matched vehicle</p>
          <h2>{escape(vehicle['year_from'])}-{escape(vehicle['year_to'])} {escape(vehicle['make'])} {escape(vehicle['model'])}</h2>
          <p class="subline">System {escape(system['code'])} - {escape(system['type'])} - source pages {escape(', '.join(str(page) for page in vehicle.get('source_pages', [])))}</p>
        </div>
      </div>

      <div class="grid two">
        <article class="panel">
          <h3>At a Glance</h3>
          {render_kv_table(system["job_essentials"])}
        </article>
        <article class="panel">
          <h3>Quick Answer</h3>
          {render_list(system["quick_answer"])}
        </article>
      </div>

      <article class="panel">
        <h3>Key / Remote Summary</h3>
        {render_kv_table({
          "Remote type": remote["remote_type"],
          "Frequency": remote["frequency"],
          "FCC ID": remote["fcc_id"]
        })}
      </article>

      <article class="panel">
        <h3>Compatible Remote Options</h3>
        {render_records_table([
          ("years", "Years"),
          ("models", "Models"),
          ("part", "Part / Reference"),
          ("buttons", "Buttons"),
          ("notes", "Notes")
        ], remote["known_options"])}
      </article>

      <article class="panel">
        <h3>All Keys Lost / Field Workflow</h3>
        {render_steps(system["making_key"]["field_workflow"])}
      </article>

      <article class="panel">
        <h3>Technician Checklist</h3>
        {render_checklist(system["technician_checklist"])}
      </article>

      <article class="panel">
        <h3>Emergency Blade / Mechanical Key</h3>
        {render_kv_table({
          "Code series": key["code_series"],
          "Style": key["style"],
          "Card": key["card"],
          "ILCO / keyway": key["ilco_keyway"],
          "MACS": key["macs"],
          "Start cut": key["start_cut"],
          "Cut-to-cut": key["cut_to_cut"]
        })}
        <div class="grid two">
          <div><h4>Spacing</h4>{render_kv_table(key["spacing"])}</div>
          <div><h4>Depths</h4>{render_kv_table(key["depths"])}</div>
        </div>
        <h4>Cutting Setup</h4>
        {render_kv_table(key["cutting_setup"])}
        <h4>Cut Position Map</h4>
        {render_cut_position_map(key["cut_position_map"])}
      </article>

      <div class="grid two">
        <article class="panel">
          <h3>Transponder</h3>
          {render_kv_table({
            "Transponder type": transponder["transponder_type"],
            "Chip": transponder["chip"],
            "Reusable": transponder["reusable"],
            "Test key": transponder["test_key"]
          })}
        </article>
        <article class="panel">
          <h3>Programming / Diagnostic</h3>
          {render_kv_table({
            "PIN required": programming["pin_required"],
            "Factory tool": programming["factory_tool"],
            "Cloning": programming["cloning"]
          })}
          {render_records_table([("name", "Tool"), ("status", "Status")], programming["tools"])}
        </article>
      </div>

      <div class="grid two">
        <article class="panel">
          <h3>Making a Working Key</h3>
          <p>{escape(system["making_key"]["code_availability"])}</p>
          {render_list(system["making_key"]["methods"])}
        </article>
        <article class="panel">
          <h3>Decoders / Readers</h3>
          {render_records_table([("tool", "Tool"), ("reference", "Reference")], system["decoders"])}
        </article>
      </div>

      <article class="panel">
        <h3>Troubleshooting</h3>
        {render_records_table([("issue", "Situation"), ("action", "Recommended action")], system["troubleshooting"])}
      </article>

      <article class="panel">
        <h3>Report Coverage</h3>
        {render_list(system["source_coverage"])}
      </article>

      {render_assets(system)}

      <div class="grid two">
        <article class="panel">
          <h3>Lock Parts</h3>
          {render_records_table([
            ("years", "Years"),
            ("models", "Models"),
            ("ignition_lock", "Ignition"),
            ("door_lock", "Door"),
            ("trunk_lock", "Trunk"),
            ("pin_kit", "PIN kit")
          ], system["lock_parts"])}
        </article>
        <article class="panel warning">
          <h3>Warnings</h3>
          {render_list(system["warnings"])}
        </article>
      </div>
    </section>
    """


def render_ocr_matches(matches, make, model, year):
    if not matches:
        return '<section class="empty">No matching structured report or OCR page match found yet.</section>'

    rows = "".join(
        f"""
        <tr>
          <td>{escape(match['document'])}</td>
          <td>{escape(match['page_number'])}</td>
          <td>{escape(match['score'])}</td>
          <td>{escape(match['snippet'])}</td>
        </tr>
        """
        for match in matches
    )
    return f"""
    <section class="panel">
      <p class="eyebrow">OCR corpus match</p>
      <h2>{escape(year)} {escape(make)} {escape(model)}</h2>
      <p class="subline">A structured report has not been approved for this exact vehicle yet. These OCR matches show likely source pages. The parser/review step converts them into the same full guide format used by approved reports.</p>
      <table class="compact-table">
        <tr><th>Document</th><th>Page</th><th>Score</th><th>OCR snippet</th></tr>
        {rows}
      </table>
    </section>
    """


def render_page(query):
    catalog = load_catalog()
    makes = sorted(set(catalog.get("makes", [])) | {v["make"] for v in catalog["vehicles"]})
    selected_make = query.get("make", [""])[0]
    selected_model = query.get("model", [""])[0]
    selected_year = query.get("year", [""])[0]
    models = sorted(
        {
            v["model"]
            for v in catalog["vehicles"]
            if not selected_make or v["make"].lower() == selected_make.lower()
        }
    )

    report = ""
    if selected_make and selected_model and selected_year:
        vehicle = find_vehicle(catalog, selected_make, selected_model, selected_year)
        if vehicle:
            system = find_system(catalog, vehicle["system_code"])
            report = render_report(vehicle, system)
        else:
            report = render_ocr_matches(
                find_ocr_matches(selected_make, selected_model, selected_year),
                selected_make,
                selected_model,
                selected_year,
            )

    return f"""<!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Locksmith Vehicle Docs</title>
        <link rel="stylesheet" href="/static/style.css">
      </head>
      <body>
        <main class="shell">
          <header class="topbar">
            <div>
              <p class="eyebrow">MVP knowledge base</p>
              <h1>Locksmith Vehicle Docs</h1>
            </div>
          </header>

          <form class="search-panel" method="get" action="/">
            <label>Make<select name="make" onchange="this.form.submit()">{option_tags(makes, selected_make)}</select></label>
            <label>Model<select name="model">{option_tags(models, selected_model)}</select></label>
            <label>Year<input name="year" inputmode="numeric" placeholder="2018" value="{escape(selected_year)}"></label>
            <button type="submit">Find report</button>
          </form>

          {report}
        </main>
      </body>
    </html>"""


class AppHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/static/"):
            self.directory = str(BASE_DIR)
            return super().do_GET()

        body = render_page(parse_qs(parsed.query)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    host = os.environ.get("LOCKSMITH_HOST", "127.0.0.1")
    port = int(os.environ.get("LOCKSMITH_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Locksmith MVP running at http://{host}:{port}")
    server.serve_forever()
