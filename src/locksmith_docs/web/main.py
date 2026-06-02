from __future__ import annotations

import json
import os
from urllib.parse import quote_plus

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from locksmith_docs.billing.plans import PLANS, get_plan, subscription_bypass_enabled
from locksmith_docs.billing.stripe_checkout import StripeNotConfigured, create_checkout_session
from locksmith_docs.core.config import get_settings
from locksmith_docs.db.init_schema import init_schema
from locksmith_docs.db.report_seed import ensure_bundled_parser_seed, ensure_bundled_report_seed, export_published_report_seed
from locksmith_docs.db.repository import LocksmithRepository, VehicleQuery
from locksmith_docs.processing.document_pipeline import refresh_verified_output, run_asset_import_job, run_asset_regeneration_job, run_full_catalog_index_job, run_owner_library_pipeline_job, run_pilot_import_job, run_publish_next_batch_job, run_rebuild_job, run_refresh_verified_output_job, run_reprocess_job, run_retry_rejected_reports_job, run_upload_job, save_uploaded_pdf, uploaded_pdf_paths
from locksmith_docs.processing.job_status import latest_jobs, start_job
from locksmith_docs.reports.ai_report_cleaner import report_api_key_fingerprint, report_cleanup_enabled, require_report_ai_access, targeted_ocr_debug
from locksmith_docs.reports.build_drafts import load_page_image_lookup
from locksmith_docs.reports.render import render_vehicle_report, vehicle_display_title
from locksmith_docs.web.rendering import esc, is_search_video_id, render_video_card


settings = get_settings()
app = FastAPI(title="Locksmith Vehicle Docs")
app.mount("/static", StaticFiles(directory=settings.project_root / "static"), name="static")

_SHOPIFY_ORIGINS = [
    "https://s1zifj-t0.myshopify.com",
    "https://bestkeyshop.com",
    "https://www.bestkeyshop.com",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_SHOPIFY_ORIGINS,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
def ensure_database_schema() -> None:
    init_schema()
    ensure_bundled_parser_seed()
    ensure_bundled_report_seed()
    if (settings.data_dir / "report_drafts.json").exists():
        refresh_verified_output()


@app.middleware("http")
async def block_source_image_crops(request: Request, call_next):
    path = request.url.path.lower()
    if path.startswith("/static/assets/") and path.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return Response(status_code=404)
    return await call_next(request)


def site_header(active: str = "lookup") -> str:
    if active == "admin":
        return """
        <header class="site-header admin-header">
          <a class="brand" href="/" aria-label="Locksmith Docs home">
            <span class="brand-mark"><span></span></span>
            <span><strong>Locksmith Docs</strong><small>Admin console</small></span>
          </a>
          <nav class="main-nav admin-nav" aria-label="Admin navigation">
            <a href="/">Open lookup</a>
            <a class="active" href="/admin">Library</a>
          </nav>
        </header>
        """
    lookup_class = "active" if active == "lookup" else ""
    blog_class = "active" if active == "blog" else ""
    pricing_class = "active" if active == "pricing" else ""
    return f"""
    <header class="site-header">
      <a class="brand" href="/" aria-label="Locksmith Docs home">
        <span class="brand-mark"><span></span></span>
        <span><strong>Locksmith Docs</strong><small>Vehicle reference</small></span>
      </a>
      <nav class="main-nav" aria-label="Main navigation">
        <a class="{lookup_class}" href="/">Lookup</a>
        <a class="{blog_class}" href="/blog">Training</a>
        <a class="{pricing_class}" href="/pricing">Plans</a>
      </nav>
      <div class="auth-actions">
        <a class="ghost-link" href="#">Sign in</a>
        <a class="button-link" href="/pricing">Get access</a>
      </div>
    </header>
    """


def page(title: str, body: str, active: str = "lookup") -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{esc(title)}</title>
            <link rel="stylesheet" href="/static/style.css">
          </head>
          <body>
            {site_header(active)}
            <main class="shell">{body}</main>
            <footer class="site-footer"><span>Locksmith Docs</span><span>Vehicle key and programming reference</span></footer>
            <script src="/static/app.js" defer></script>
          </body>
        </html>"""
    )


def clean_param(value: str | None) -> str:
    return (value or "").strip()


def parse_year(value: str | None) -> int | None:
    clean = clean_param(value)
    if not clean:
        return None
    try:
        return int(clean)
    except ValueError:
        return None


def shopify_report_page_url(report_id: str) -> str:
    return f"/pages/key-report?report={quote_plus((report_id or '').lower())}"


def absolutize_static_urls(html: str, base_url: str) -> str:
    base = base_url.rstrip("/")
    return html.replace('src="/static/', f'src="{base}/static/').replace('href="/static/', f'href="{base}/static/')


def build_report_preview_payload(vehicle: dict, system: dict, *, year: int | None = None) -> dict:
    code = vehicle["system_code"]
    essentials = system.get("job_essentials") or {}
    quick_answer_raw = system.get("quick_answer") or []
    mechanical = system.get("mechanical_key") or {}
    key_remote = system.get("key_remote") or {}
    programming = system.get("programming") or {}
    decoders_raw = system.get("decoders") or []

    at_a_glance: list[dict] = []

    def _add(label: str, value) -> None:
        v = str(value or "").strip()
        if v and v not in {"-", "n/a", "N/A", "None"}:
            at_a_glance.append({"label": label, "value": v})

    pin = programming.get("pin_required") or essentials.get("pin_required") or essentials.get("PIN")
    _add("PIN", "Required" if str(pin or "").lower() in {"yes", "required"} else pin)

    freq = essentials.get("frequency") or essentials.get("Frequency")
    if not freq and isinstance(key_remote.get("known_options"), list):
        for opt in key_remote["known_options"]:
            if isinstance(opt, dict) and opt.get("frequency"):
                freq = opt["frequency"]
                break
    _add("Frequency", freq)
    _add("Code series", mechanical.get("code_series") or essentials.get("code_series"))

    lishi_tools = [
        d.get("tool") or d.get("name") or ""
        for d in (decoders_raw if isinstance(decoders_raw, list) else [])
        if isinstance(d, dict) and "lishi" in str(d.get("tool", "") + str(d.get("name", ""))).lower()
    ]
    _add("Lishi / decoder", lishi_tools[0] if lishi_tools else essentials.get("lishi") or essentials.get("decoder"))

    prog_path = (
        programming.get("programming_path")
        or programming.get("method")
        or essentials.get("programming_path")
    )
    if not prog_path and programming.get("tools"):
        tools = programming["tools"]
        if isinstance(tools, list) and tools:
            first = tools[0]
            prog_path = first.get("name") or str(first)
    _add("Programming path", prog_path)

    covered_keys = {"pin_required", "pin", "frequency", "code_series", "lishi", "decoder", "programming_path", "summary"}
    for k, v in essentials.items():
        if k.lower() not in covered_keys and isinstance(v, str) and v.strip():
            at_a_glance.append({"label": k.replace("_", " ").title(), "value": v.strip()})

    quick_answer: list[str] = []
    for item in (quick_answer_raw if isinstance(quick_answer_raw, list) else []):
        if isinstance(item, str):
            quick_answer.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("answer") or item.get("detail") or ""
            if text:
                quick_answer.append(str(text))

    intro = str(essentials.get("summary") or essentials.get("intro") or "").strip()
    if not intro and quick_answer:
        intro = " ".join(quick_answer[:2])

    display_vehicle = dict(vehicle)
    if year is not None:
        display_vehicle["requested_year"] = year

    return {
        "found": True,
        "report_id": code.lower(),
        "title": vehicle_display_title(display_vehicle) or f"{vehicle.get('year_from')}-{vehicle.get('year_to')} {vehicle.get('make')} {vehicle.get('model')}",
        "subtitle": f"System {code} · {system.get('system_type') or ''}".rstrip(" ·"),
        "intro": intro,
        "at_a_glance": at_a_glance,
        "quick_answer": quick_answer,
        "report_url": shopify_report_page_url(code.lower()),
        "vehicle": {
            "make": vehicle["make"],
            "model": vehicle["model"],
            "year_from": vehicle["year_from"],
            "year_to": vehicle["year_to"],
            "system_code": code,
        },
    }


def render_markdown_light(text: str) -> str:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    return "".join(f"<p>{esc(paragraph)}</p>" for paragraph in paragraphs)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    make = clean_param(request.query_params.get("make"))
    model = clean_param(request.query_params.get("model"))
    year = parse_year(request.query_params.get("year"))
    repo = LocksmithRepository()
    makes = repo.list_makes()
    models = repo.list_models(make) if make else []
    counts = repo.dashboard_counts()

    make_options = "".join(
        f'<option value="{esc(item)}" {"selected" if item == make else ""}>{esc(item)}</option>'
        for item in makes
    )
    model_options = "".join(
        f'<option value="{esc(item)}" {"selected" if item == model else ""}>{esc(item)}</option>'
        for item in models
    )

    result = ""
    if make and model and year is not None:
        requested = VehicleQuery(make=make, model=model, year=year)
        vehicle = repo.find_vehicle_best_effort(requested)
        if vehicle:
            display_vehicle = dict(vehicle)
            if vehicle["year_from"] <= year <= vehicle["year_to"]:
                display_vehicle["requested_year"] = year
            system = repo.get_published_or_draft_system(vehicle["system_code"])
            assets = repo.list_assets_for_system(vehicle["system_code"])
            year_note = ""
            if not (vehicle["year_from"] <= year <= vehicle["year_to"]):
                year_note = f"""
                <section class="panel warning">
                  <h3>Closest Available Match</h3>
                  <p>The exact year is not fully mapped yet. Showing the closest verified system for {esc(vehicle['year_from'])}-{esc(vehicle['year_to'])} {esc(vehicle['make'])} {esc(vehicle['model'])}.</p>
                </section>
                """
            result = year_note + (
                render_vehicle_report(display_vehicle, system, assets)
                if system
                else """
                <section class="panel empty-result">
                  <p class="eyebrow">Import in progress</p>
                  <h2>This vehicle is indexed, but its verified report is not published yet.</h2>
                  <p class="subline">The source document is stored. A controlled verification batch must complete before technical instructions are shown to technicians.</p>
                </section>
                """
            )
        else:
            result = f"""
            <section class="panel empty-result">
              <p class="eyebrow">Vehicle search</p>
              <h2>No verified field report is available yet.</h2>
              <p class="subline">We only display vehicle guidance after its identifiers, procedures, and technical values have passed verification.</p>
            </section>
            """

    body = f"""
    <section class="page-heading lookup-heading">
      <div>
        <h1>Vehicle lookup</h1>
        <p class="page-copy">Find verified key, blade and programming data by vehicle.</p>
      </div>
      <div class="catalog-summary" aria-label="Catalog status">
        <span><strong>{esc(len(makes))}</strong> makes</span>
        <span><strong>{esc(counts.get("published_reports", 0))}</strong> verified reports</span>
      </div>
    </section>
    <form class="search-panel lookup-card" method="get">
      <label>Make<select name="make" id="make-select"><option value="">Select make...</option>{make_options}</select></label>
      <label>Model<select name="model" id="model-select" data-selected="{esc(model)}"><option value="">Select model...</option>{model_options}</select></label>
      <label>Year<input name="year" inputmode="numeric" value="{esc(year or '')}" placeholder="2018"></label>
      <button class="primary-button">Find report</button>
    </form>
    {result}
    """
    return page("Locksmith Vehicle Docs", body, active="lookup")


@app.get("/blog", response_class=HTMLResponse)
def blog():
    repo = LocksmithRepository()
    posts = repo.list_blog_posts()
    videos = repo.list_blog_videos()
    cards = "".join(
        f'<article class="blog-post-card"><p class="eyebrow">Field article</p><h2><a href="/blog/{esc(post["slug"])}">{esc(post["title"])}</a></h2><p>{esc(post.get("summary") or "")}</p></article>'
        for post in posts
    )
    video_cards = "".join(render_video_card(video) for video in videos)
    body = f"""
    <section class="page-heading">
      <h1>Training</h1>
      <p class="page-copy">Field articles and repair videos for working locksmiths.</p>
    </section>
    <section class="blog-posts">{cards}</section>
    <section class="video-section">
      <div class="section-heading">
        <div><p class="eyebrow">YouTube training</p><h2>Repair Videos</h2></div>
        <span class="pill">6 topics</span>
      </div>
      <div class="video-grid" data-video-api="/api/blog/videos">{video_cards}</div>
    </section>
    """
    return page("Blog", body, active="blog")


@app.get("/blog/{slug}", response_class=HTMLResponse)
def blog_detail(slug: str):
    repo = LocksmithRepository()
    post = repo.get_blog_post(slug)
    if not post:
        return page("Blog", "<section class=\"empty\">Article not found.</section>", active="blog")
    videos = repo.list_blog_videos()
    video_cards = "".join(render_video_card(video) for video in videos)
    body = f"""
    <article class="panel article">
      <p class="eyebrow">Training Blog</p>
      <h1>{esc(post["title"])}</h1>
      <p class="subline">{esc(post.get("summary") or "")}</p>
      <div class="article-body">{render_markdown_light(post.get("body_md") or "")}</div>
    </article>
    <h2 class="section-title">Related Videos</h2>
    <div class="video-grid">{video_cards}</div>
    """
    return page(post["title"], body, active="blog")


@app.get("/pricing", response_class=HTMLResponse)
def pricing():
    cards = []
    for plan in PLANS:
        features = "".join(f"<li>{esc(feature)}</li>" for feature in plan.features)
        cards.append(
            f"""
            <article class="pricing-card">
              <div>
                <p class="eyebrow">{esc(plan.name)}</p>
                <h2>{esc(plan.price_label)}</h2>
                <p>{esc(plan.description)}</p>
              </div>
              <ul>{features}</ul>
              <form action="/billing/checkout/{esc(plan.id)}" method="post">
                <button class="primary-button">Choose {esc(plan.name)}</button>
              </form>
            </article>
            """
        )
    body = f"""
    <section class="page-heading">
      <h1>Plans</h1>
      <p class="page-copy">Reference access for individual locksmiths and shops.</p>
    </section>
    <section class="pricing-grid">{''.join(cards)}</section>
    """
    return page("Pricing", body, active="pricing")


@app.post("/billing/checkout/{plan_id}")
def billing_checkout(plan_id: str, request: Request):
    plan = get_plan(plan_id)
    if not plan:
        return RedirectResponse(url="/pricing?message=Plan not found.", status_code=303)
    base_url = str(request.base_url).rstrip("/")
    try:
        checkout_url = create_checkout_session(
            plan,
            success_url=f"{base_url}/billing/success?plan={quote_plus(plan.id)}",
            cancel_url=f"{base_url}/pricing",
        )
    except StripeNotConfigured:
        return RedirectResponse(url="/pricing?message=Stripe is not configured yet.", status_code=303)
    return RedirectResponse(url=checkout_url, status_code=303)


@app.get("/billing/success", response_class=HTMLResponse)
def billing_success(plan: str = ""):
    body = f"""
    <section class="panel success">
      <p class="eyebrow">Subscription</p>
      <h1>Access is ready</h1>
      <p>Your {esc(plan or 'selected')} membership checkout completed. Account provisioning will be connected to the Stripe webhook before production launch.</p>
      <p><a class="button-link" href="/">Open vehicle lookup</a></p>
    </section>
    """
    return page("Subscription", body, active="pricing")


@app.get("/api/blog/videos")
def blog_videos_api():
    repo = LocksmithRepository()
    videos = repo.list_blog_videos(limit=6)
    payload = []
    for video in videos:
        video_id = video.get("youtube_video_id") or ""
        query = video.get("search_query") or video.get("title") or "automotive locksmith training"
        encoded_query = quote_plus(query)
        payload.append(
            {
                "title": video.get("title") or "",
                "description": video.get("description") or "",
                "youtube_video_id": video_id,
                "search_query": query,
                "make": video.get("make"),
                "model": video.get("model"),
                "watch_url": (
                    f"https://www.youtube.com/results?search_query={encoded_query}"
                    if is_search_video_id(video_id)
                    else f"https://www.youtube.com/watch?v={video_id}"
                ),
                "embed_url": (
                    f"https://www.youtube-nocookie.com/embed?listType=search&list={encoded_query}"
                    if is_search_video_id(video_id)
                    else f"https://www.youtube-nocookie.com/embed/{video_id}"
                ),
                "thumbnail_url": "" if is_search_video_id(video_id) else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            }
        )
    return JSONResponse(payload)


@app.get("/api/models")
def models_api(make: str = ""):
    repo = LocksmithRepository()
    return JSONResponse(repo.list_models(clean_param(make)))


@app.get("/api/report/preview")
def report_preview_api(
    make: str = "",
    model: str = "",
    year: int | None = Query(default=None),
    report: str = "",
):
    """Return a structured JSON preview of a key report for a given vehicle or report id."""
    repo = LocksmithRepository()
    report_code = clean_param(report).upper()
    if report_code:
        vehicle = repo.find_vehicle_for_system(report_code)
        if not vehicle:
            return JSONResponse({"found": False}, status_code=404)
        system = repo.get_published_or_draft_system(report_code)
        if not system:
            return JSONResponse({"found": False}, status_code=404)
        return JSONResponse(build_report_preview_payload(vehicle, system))

    make_c = clean_param(make)
    model_c = clean_param(model)
    if not (make_c and model_c and year is not None):
        return JSONResponse({"found": False, "error": "make, model and year are required"}, status_code=400)

    query = VehicleQuery(make=make_c, model=model_c, year=year)
    vehicle = repo.find_vehicle_best_effort(query)
    if not vehicle:
        return JSONResponse({"found": False}, status_code=404)

    system = repo.get_published_or_draft_system(vehicle["system_code"])
    if not system:
        return JSONResponse({"found": False}, status_code=404)

    return JSONResponse(build_report_preview_payload(vehicle, system, year=year))


@app.get("/api/report/full")
def report_full_api(request: Request, report: str = ""):
    """Return full report HTML for Shopify (access is gated on the storefront)."""
    code = clean_param(report).upper()
    if not code:
        return JSONResponse({"found": False, "error": "report is required"}, status_code=400)

    repo = LocksmithRepository()
    vehicle = repo.find_vehicle_for_system(code)
    if not vehicle:
        return JSONResponse({"found": False}, status_code=404)

    system = repo.get_published_or_draft_system(code)
    if not system:
        return JSONResponse({"found": False}, status_code=404)

    assets = repo.list_assets_for_system(code)
    html = render_vehicle_report(vehicle, system, assets)
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "found": True,
        "report_id": code.lower(),
        "title": vehicle_display_title(vehicle),
        "html": absolutize_static_urls(html, base),
    })


@app.get("/videos", response_class=HTMLResponse)
def videos(make: str = "", model: str = "", year: str | None = Query(default=None)):
    return RedirectResponse(url="/blog", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(message: str = ""):
    repo = LocksmithRepository()
    counts = repo.dashboard_counts()
    rows = "".join(f"<tr><th>{esc(key.replace('_', ' ').title())}</th><td>{esc(value)}</td></tr>" for key, value in counts.items())
    rejected_reports = repo.list_report_drafts(status="rejected", limit=8)
    rejected_rows = "".join(
        f"<tr><td>{esc(report.get('system_code', ''))}</td><td>{esc('; '.join(report.get('publication_issues') or []) or 'Verification incomplete.')}</td></tr>"
        for report in rejected_reports
    )
    jobs = latest_jobs()
    job_rows = "".join(
        f"<tr><td>{esc(job.get('status', ''))}</td><td>{esc(job.get('label', ''))}</td><td>{esc(job.get('message', ''))}</td><td>{esc(job.get('updated_at', ''))}</td></tr>"
        for job in jobs
    )
    running = any(job.get("status") == "running" for job in jobs)
    refresh = '<meta http-equiv="refresh" content="12">' if running else ""
    notice = f'<section class="alert success"><p>{esc(message)}</p></section>' if message else ""
    ai_notice = "" if report_cleanup_enabled() else """
    <section class="alert warning">
      <h2>AI verification is not connected</h2>
      <p class="subline">Reports and original diagrams cannot be published until an OpenAI API key is supplied to the app environment and the app is restarted.</p>
    </section>
    """
    report_batch_limit = esc(os.environ.get("AI_MAX_NEW_REPORTS_PER_RUN", "1"))
    diagram_batch_limit = esc(os.environ.get("AI_MAX_NEW_DIAGRAMS_PER_RUN", "5"))
    ai_key_fingerprint = esc(report_api_key_fingerprint())
    stored_pdf_count = len(uploaded_pdf_paths())
    active_document_count = int(counts.get("documents", 0))
    inactive_pdf_count = max(0, stored_pdf_count - active_document_count)
    catalog_notice = ""
    if inactive_pdf_count:
        catalog_notice = f"""
        <section class="alert warning">
          <h2>Only a pilot catalog is active</h2>
          <p class="subline"><strong>{active_document_count}</strong> of <strong>{stored_pdf_count}</strong> stored PDF books are indexed in the live lookup. The other <strong>{inactive_pdf_count}</strong> books are still safely stored, but their models and reports are not active after the pilot reset.</p>
          <form action="/admin/index-all-documents" method="post">
            <button class="primary-button" data-busy-text="Indexing books...">Index all stored PDFs</button>
          </form>
          <p class="subline">This restores makes and models using local OCR only. It makes no OpenAI requests and does not publish unverified technician instructions.</p>
        </section>
        """
    stored_pdf_options = "".join(
        f'<option value="{esc(path.name)}" {"selected" if path.name == "Audi-VW-Porsche.pdf" else ""}>{esc(path.name)}</option>'
        for path in uploaded_pdf_paths()
    )
    report_target_options = "".join(
        f'<option value="{esc(target["system_code"])}">{esc(target["make"])} {esc(target["model"])} '
        f'{esc(target["year_from"])}-{esc(target["year_to"])} ({esc(target["system_code"])})</option>'
        for target in repo.list_report_targets()
    )
    body = f"""
    {refresh}
    <section class="page-heading admin-heading">
      <div>
        <h1>Library</h1>
        <p class="page-copy">Import books and publish verified vehicle reports.</p>
      </div>
      <div class="status-dot {"running" if running else "ready"}">{"Processing" if running else "Ready"}</div>
    </section>
    {notice}
    {ai_notice}
    {catalog_notice}
    <section class="admin-stats" aria-label="Library statistics">
      <div><strong>{esc(counts.get("documents", 0))}</strong><span>Books</span></div>
      <div><strong>{esc(counts.get("vehicle_links", 0))}</strong><span>Applications</span></div>
      <div><strong>{esc(counts.get("mapped_system_codes", 0))}</strong><span>Systems</span></div>
      <div><strong>{esc(counts.get("published_reports", 0))}</strong><span>Published</span></div>
      <div><strong>{esc(counts.get("reports_waiting", 0))}</strong><span>Waiting</span></div>
    </section>
    <section class="admin-columns">
      <div class="admin-column">
        <h2>Documents</h2>
        <form class="panel admin-card primary-task" action="/admin/upload" method="post" enctype="multipart/form-data">
        <h3>Upload books</h3>
        <p class="subline">Add one or more PDF reference books for processing.</p>
        <label>PDF documents<input type="file" name="files" accept="application/pdf" multiple required></label>
        <div class="upload-status" data-upload-status hidden>
          <div class="upload-meter"><span data-upload-meter></span></div>
          <p data-upload-message>Waiting for files...</p>
        </div>
        <button class="primary-button" data-busy-text="Uploading...">Upload PDFs</button>
        </form>
      </div>
      <div class="admin-column">
        <h2>Publishing</h2>
        <form class="panel admin-card primary-task" action="/admin/publish-next-batch" method="post">
          <h3>Publish next queued reports</h3>
          <p class="subline">Verify and publish the next <strong>{report_batch_limit}</strong> queued report(s). Previously approved reports remain available and are reused without new AI requests.</p>
          <button class="primary-button" data-busy-text="Publishing...">Publish next batch</button>
        </form>
        <form class="panel admin-card primary-task" action="/admin/process-library" method="post">
          <h3>Process uploaded library</h3>
          <p class="subline">One-button owner workflow: rebuild the lookup index locally, then verify and publish the next controlled report batch with original diagrams.</p>
          <button class="primary-button" data-busy-text="Processing...">Process library</button>
        </form>
        <form class="panel admin-card primary-task" action="/admin/rebuild" method="post">
          <h3>Publish a selected report</h3>
          <p class="subline">Select one indexed vehicle system for AI verification and original diagram creation. The pilot budget limit is <strong>{report_batch_limit}</strong> report per run.</p>
          <label>Vehicle report
            <select name="system_code" required>
              <option value="">Select a vehicle system...</option>
              {report_target_options}
            </select>
          </label>
          <button class="primary-button" data-busy-text="Processing...">Verify and publish</button>
        </form>
        {""
        if not rejected_reports else
        f'''<form class="panel admin-card" action="/admin/retry-rejected" method="post">
          <h3>Retry rejected reports</h3>
          <p class="subline">Re-run automated verification only for failed reports after the quality rules or source extraction have improved.</p>
          <button class="secondary-button" data-busy-text="Retrying...">Retry failed reports</button>
        </form>'''}
        <form class="panel admin-card" action="/admin/refresh-verified" method="post">
          <h3>Refresh approved output</h3>
          <p class="subline">Re-render approved reports and diagrams without API usage.</p>
          <button class="secondary-button" data-busy-text="Refreshing...">Refresh output</button>
        </form>
        <form class="panel admin-card" action="/admin/export-report-seed" method="post">
          <h3>Package approved reports</h3>
          <p class="subline">Save already published reports into the deployable app package. Source OCR and PDFs are not included.</p>
          <button class="secondary-button" data-busy-text="Packaging...">Package reports</button>
        </form>
      </div>
    </section>
    <section class="panel admin-table">
      <div class="section-heading"><h2>Recent activity</h2><span class="muted-note">Updates automatically while processing</span></div>
      <div class="table-scroll"><table class="compact-table"><tr><th>Status</th><th>Job</th><th>Message</th><th>Updated</th></tr>{job_rows or '<tr><td colspan="4">No jobs yet.</td></tr>'}</table></div>
    </section>
    {""
    if not rejected_reports else
    f'''<section class="panel admin-table">
      <div class="section-heading"><h2>Reports blocked from publication</h2><span class="muted-note">Nothing incomplete is shown to customers</span></div>
      <div class="table-scroll"><table class="compact-table"><tr><th>System</th><th>Reason</th></tr>{rejected_rows}</table></div>
    </section>'''}
    <details class="advanced-panel">
      <summary>Advanced operations</summary>
      <div class="advanced-grid">
        <form class="panel admin-card" action="/admin/check-ai" method="post">
          <h3>Connection check</h3>
          <p class="subline">Verify the configured AI connection without generating content. Key fingerprint: <strong>{ai_key_fingerprint}</strong>.</p>
          <button class="secondary-button" data-busy-text="Testing...">Test connection</button>
        </form>
        <form class="panel admin-card" action="/admin/index-all-documents" method="post">
          <h3>Re-index stored PDFs</h3>
          <p class="subline">Rebuild the vehicle index locally with no API spending.</p>
          <button class="secondary-button" data-busy-text="Indexing...">Index stored PDFs</button>
        </form>
        <form class="panel admin-card" action="/admin/regenerate-assets" method="post">
          <h3>Recover missed diagrams</h3>
          <p class="subline">Scan up to <strong>{diagram_batch_limit}</strong> additional pages for instructional visuals.</p>
          <button class="secondary-button" data-busy-text="Analyzing...">Find diagrams</button>
        </form>
        <form class="panel admin-card" action="/admin/reprocess" method="post">
          <h3>Run OCR again</h3>
          <p class="subline">Reprocess every stored PDF when source extraction needs correction.</p>
          <button class="secondary-button" data-busy-text="Starting OCR...">Reprocess books</button>
        </form>
      </div>
      <details class="pilot-details">
        <summary>Clean single-book pilot</summary>
        <form class="pilot-form" action="/admin/pilot-import" method="post">
          <label>Test document
            <select name="filename" required>
              <option value="">Select a stored PDF...</option>
              {stored_pdf_options}
            </select>
          </label>
          <button class="secondary-button" data-busy-text="Starting...">Start clean pilot</button>
        </form>
      </details>
      <div class="panel data-table"><h3>Database status</h3><div class="table-scroll"><table class="compact-table kv-table">{rows}</table></div></div>
    </details>
    """
    return page("Admin", body, active="admin")


@app.post("/admin/upload")
def admin_upload(request: Request, background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    valid_files = [file for file in files if file.filename and file.filename.lower().endswith(".pdf")]
    if not valid_files:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JSONResponse({"ok": False, "message": "Only PDF files are supported."}, status_code=400)
        return RedirectResponse(url="/admin?message=Only PDF files are supported.", status_code=303)
    saved_paths = [save_uploaded_pdf(file.file, file.filename) for file in valid_files]
    job_id = start_job("upload", f"Upload {len(saved_paths)} PDF(s)")
    background_tasks.add_task(run_upload_job, job_id, [str(path) for path in saved_paths])
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"ok": True, "job_id": job_id, "message": f"Upload saved. Background OCR job started: {job_id}."})
    return RedirectResponse(url=f"/admin?message=Upload saved. Background OCR job started: {job_id}.", status_code=303)


@app.post("/admin/rebuild")
def admin_rebuild(background_tasks: BackgroundTasks, system_code: str = Form(...)):
    clean_system_code = clean_param(system_code)
    job_id = start_job("rebuild", f"Publish report: {clean_system_code}")
    background_tasks.add_task(run_rebuild_job, job_id, clean_system_code)
    return RedirectResponse(url=f"/admin?message=Report verification started: {job_id}.", status_code=303)


@app.get("/admin/debug-technical")
def admin_debug_technical(system_code: str = Query(...)):
    clean_system_code = clean_param(system_code).upper()
    candidates_path = settings.data_dir / "parser_candidates.json"
    if not candidates_path.exists():
        return JSONResponse({"ok": False, "message": "No indexed report candidates found."}, status_code=404)
    sections = json.loads(candidates_path.read_text(encoding="utf-8")).get("sections", [])
    section = next((item for item in sections if str(item.get("code") or "").upper() == clean_system_code), None)
    if not section:
        return JSONResponse({"ok": False, "message": "System code not found."}, status_code=404)
    lookup = load_page_image_lookup(settings.data_dir / "imported_pages.json")
    pages = [
        lookup[(str(section.get("source_document") or ""), number)]
        for number in range(int(section.get("page_start") or 0), int(section.get("page_end") or 0) + 1)
        if (str(section.get("source_document") or ""), number) in lookup
    ]
    return JSONResponse({"ok": True, "system_code": clean_system_code, **targeted_ocr_debug(pages)})


@app.post("/admin/publish-next-batch")
def admin_publish_next_batch(background_tasks: BackgroundTasks):
    job_id = start_job("publish", "Publish next queued report batch")
    background_tasks.add_task(run_publish_next_batch_job, job_id)
    return RedirectResponse(url=f"/admin?message=Queued report batch started: {job_id}.", status_code=303)


@app.post("/admin/process-library")
def admin_process_library(background_tasks: BackgroundTasks):
    job_id = start_job("pipeline", "Process uploaded library")
    background_tasks.add_task(run_owner_library_pipeline_job, job_id)
    return RedirectResponse(url=f"/admin?message=Library processing started: {job_id}.", status_code=303)


@app.post("/admin/check-ai")
def admin_check_ai():
    fingerprint = report_api_key_fingerprint()
    try:
        require_report_ai_access()
    except RuntimeError as exc:
        return RedirectResponse(
            url=f"/admin?message={quote_plus(f'AI connection failed for key fingerprint {fingerprint}: {exc}')}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin?message={quote_plus(f'AI connection verified for key fingerprint {fingerprint}.')}",
        status_code=303,
    )


@app.post("/admin/export-report-seed")
def admin_export_report_seed():
    path = export_published_report_seed()
    return RedirectResponse(
        url=f"/admin?message={quote_plus(f'Packaged approved reports into {path.relative_to(settings.project_root)}.')}",
        status_code=303,
    )


@app.post("/admin/refresh-verified")
def admin_refresh_verified(background_tasks: BackgroundTasks):
    job_id = start_job("refresh", "Refresh approved reports")
    background_tasks.add_task(run_refresh_verified_output_job, job_id)
    return RedirectResponse(url=f"/admin?message=Approved report refresh started: {job_id}.", status_code=303)


@app.post("/admin/retry-rejected")
def admin_retry_rejected(background_tasks: BackgroundTasks):
    job_id = start_job("retry", "Retry rejected reports")
    background_tasks.add_task(run_retry_rejected_reports_job, job_id)
    return RedirectResponse(url=f"/admin?message=Rejected report retry started: {job_id}.", status_code=303)


@app.post("/admin/pilot-import")
def admin_pilot_import(background_tasks: BackgroundTasks, filename: str = Form(...)):
    stored_names = {path.name for path in uploaded_pdf_paths()}
    if filename not in stored_names:
        return RedirectResponse(url="/admin?message=Selected PDF is not stored on the server.", status_code=303)
    job_id = start_job("pilot", f"Clean pilot: {filename}")
    background_tasks.add_task(run_pilot_import_job, job_id, filename)
    return RedirectResponse(url=f"/admin?message=Clean pilot import started: {job_id}.", status_code=303)


@app.post("/admin/import-assets")
def admin_import_assets(background_tasks: BackgroundTasks):
    job_id = start_job("assets", "Import prepared diagrams")
    background_tasks.add_task(run_asset_import_job, job_id)
    return RedirectResponse(url=f"/admin?message=Diagram import started: {job_id}.", status_code=303)


@app.post("/admin/regenerate-assets")
def admin_regenerate_assets(background_tasks: BackgroundTasks):
    job_id = start_job("assets", "Build original procedure diagrams")
    background_tasks.add_task(run_asset_regeneration_job, job_id)
    return RedirectResponse(url=f"/admin?message=Diagram build started: {job_id}.", status_code=303)


@app.post("/admin/reprocess")
def admin_reprocess(background_tasks: BackgroundTasks):
    job_id = start_job("reocr", "Re-OCR uploaded PDFs")
    background_tasks.add_task(run_reprocess_job, job_id)
    return RedirectResponse(url=f"/admin?message=Re-OCR job started: {job_id}.", status_code=303)


@app.post("/admin/index-all-documents")
def admin_index_all_documents(background_tasks: BackgroundTasks):
    job_id = start_job("index", "Index all stored PDFs without AI")
    background_tasks.add_task(run_full_catalog_index_job, job_id)
    return RedirectResponse(
        url=f"/admin?message=Full catalog indexing started without API spending: {job_id}.",
        status_code=303,
    )


@app.get("/admin/{path:path}")
def admin_removed_nested(path: str):
    return RedirectResponse(url="/admin", status_code=303)


if __name__ == "__main__":
    uvicorn.run("locksmith_docs.web.main:app", host=settings.app_host, port=settings.app_port, reload=False)
