CREATE TABLE IF NOT EXISTS documents (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  source_path TEXT NOT NULL,
  pages_total INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TABLE IF NOT EXISTS document_pages (
  id BIGSERIAL PRIMARY KEY,
  document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_number INTEGER NOT NULL,
  image_path TEXT,
  ocr_text TEXT,
  ocr_quality NUMERIC(5,2),
  search_vector TSVECTOR,
  UNIQUE(document_id, page_number)
);

CREATE INDEX IF NOT EXISTS document_pages_search_idx
  ON document_pages USING GIN(search_vector);

CREATE INDEX IF NOT EXISTS document_pages_ocr_trgm_idx
  ON document_pages USING GIN(ocr_text gin_trgm_ops);

CREATE TABLE IF NOT EXISTS vehicle_applications (
  id BIGSERIAL PRIMARY KEY,
  make TEXT NOT NULL,
  model TEXT NOT NULL,
  year_from INTEGER NOT NULL,
  year_to INTEGER NOT NULL,
  system_code TEXT NOT NULL,
  system_type TEXT,
  source_document TEXT,
  source_pages INTEGER[] DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS vehicle_lookup_idx
  ON vehicle_applications (lower(make), lower(model), year_from, year_to);

CREATE INDEX IF NOT EXISTS vehicle_make_model_idx
  ON vehicle_applications (lower(make), lower(model));

CREATE INDEX IF NOT EXISTS vehicle_system_code_idx
  ON vehicle_applications (system_code);

CREATE UNIQUE INDEX IF NOT EXISTS vehicle_unique_system_idx
  ON vehicle_applications (lower(make), lower(model), year_from, year_to, system_code);

CREATE INDEX IF NOT EXISTS vehicle_make_trgm_idx
  ON vehicle_applications USING GIN(make gin_trgm_ops);

CREATE INDEX IF NOT EXISTS vehicle_model_trgm_idx
  ON vehicle_applications USING GIN(model gin_trgm_ops);

CREATE TABLE IF NOT EXISTS vehicle_aliases (
  id BIGSERIAL PRIMARY KEY,
  make TEXT NOT NULL,
  model TEXT NOT NULL,
  alias TEXT NOT NULL,
  UNIQUE(make, model, alias)
);

CREATE TABLE IF NOT EXISTS systems (
  code TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  system_type TEXT,
  job_essentials JSONB NOT NULL DEFAULT '{}',
  quick_answer JSONB NOT NULL DEFAULT '[]',
  key_remote JSONB NOT NULL DEFAULT '{}',
  mechanical_key JSONB NOT NULL DEFAULT '{}',
  transponder JSONB NOT NULL DEFAULT '{}',
  programming JSONB NOT NULL DEFAULT '{}',
  making_key JSONB NOT NULL DEFAULT '{}',
  technician_checklist JSONB NOT NULL DEFAULT '{}',
  source_facts JSONB NOT NULL DEFAULT '{}',
  decoders JSONB NOT NULL DEFAULT '[]',
  lock_parts JSONB NOT NULL DEFAULT '[]',
  troubleshooting JSONB NOT NULL DEFAULT '[]',
  warnings JSONB NOT NULL DEFAULT '[]',
  source_coverage JSONB NOT NULL DEFAULT '[]',
  search_vector TSVECTOR,
  publication_source TEXT NOT NULL DEFAULT 'legacy',
  status TEXT NOT NULL DEFAULT 'draft',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE systems ADD COLUMN IF NOT EXISTS job_essentials JSONB NOT NULL DEFAULT '{}';
ALTER TABLE systems ADD COLUMN IF NOT EXISTS technician_checklist JSONB NOT NULL DEFAULT '{}';
ALTER TABLE systems ADD COLUMN IF NOT EXISTS source_facts JSONB NOT NULL DEFAULT '{}';
ALTER TABLE systems ADD COLUMN IF NOT EXISTS troubleshooting JSONB NOT NULL DEFAULT '[]';
ALTER TABLE systems ADD COLUMN IF NOT EXISTS publication_source TEXT NOT NULL DEFAULT 'legacy';

CREATE INDEX IF NOT EXISTS systems_search_idx
  ON systems USING GIN(search_vector);

CREATE INDEX IF NOT EXISTS systems_status_idx
  ON systems(status);

CREATE INDEX IF NOT EXISTS systems_publication_source_idx
  ON systems(publication_source, status);

CREATE TABLE IF NOT EXISTS report_sections (
  id BIGSERIAL PRIMARY KEY,
  system_code TEXT NOT NULL REFERENCES systems(code) ON DELETE CASCADE,
  section_key TEXT NOT NULL,
  title TEXT NOT NULL,
  body JSONB NOT NULL DEFAULT '{}',
  sort_order INTEGER NOT NULL DEFAULT 0,
  source_document TEXT,
  source_pages INTEGER[] DEFAULT '{}',
  review_status TEXT NOT NULL DEFAULT 'draft',
  UNIQUE(system_code, section_key)
);

CREATE TABLE IF NOT EXISTS report_drafts (
  id BIGSERIAL PRIMARY KEY,
  system_code TEXT NOT NULL,
  source_document TEXT,
  source_pages INTEGER[] DEFAULT '{}',
  draft JSONB NOT NULL DEFAULT '{}',
  confidence NUMERIC(5,2) NOT NULL DEFAULT 0,
  ai_verified BOOLEAN NOT NULL DEFAULT FALSE,
  publication_issues JSONB NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'needs_review',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE report_drafts ADD COLUMN IF NOT EXISTS ai_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE report_drafts ADD COLUMN IF NOT EXISTS publication_issues JSONB NOT NULL DEFAULT '[]';

CREATE INDEX IF NOT EXISTS report_drafts_system_idx
  ON report_drafts(system_code, status);

CREATE UNIQUE INDEX IF NOT EXISTS report_drafts_source_idx
  ON report_drafts(system_code, source_document, source_pages);

CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  system_code TEXT NOT NULL REFERENCES systems(code) ON DELETE CASCADE,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  public_path TEXT,
  source_document TEXT,
  source_page INTEGER,
  crop_box INTEGER[],
  extracted_text TEXT,
  diagram_data JSONB NOT NULL DEFAULT '{}',
  rewritten_caption TEXT,
  placement TEXT,
  usefulness_score NUMERIC(5,2),
  watermark_removed BOOLEAN NOT NULL DEFAULT FALSE,
  visibility TEXT NOT NULL DEFAULT 'internal',
  review_status TEXT NOT NULL DEFAULT 'pending'
);

ALTER TABLE assets ADD COLUMN IF NOT EXISTS placement TEXT;
ALTER TABLE assets ADD COLUMN IF NOT EXISTS diagram_data JSONB NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS assets_system_idx ON assets(system_code);
CREATE INDEX IF NOT EXISTS assets_kind_idx ON assets(kind, review_status, visibility);
CREATE INDEX IF NOT EXISTS assets_public_system_idx
  ON assets(system_code, usefulness_score DESC)
  WHERE visibility = 'public';

CREATE TABLE IF NOT EXISTS parser_jobs (
  id BIGSERIAL PRIMARY KEY,
  document_id BIGINT REFERENCES documents(id) ON DELETE SET NULL,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  details JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS blog_posts (
  id BIGSERIAL PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  summary TEXT,
  body_md TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS videos (
  id BIGSERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  youtube_video_id TEXT NOT NULL,
  description TEXT,
  search_query TEXT,
  make TEXT,
  model TEXT,
  year_from INTEGER,
  year_to INTEGER,
  system_code TEXT REFERENCES systems(code) ON DELETE SET NULL,
  tags TEXT[] DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE videos ADD COLUMN IF NOT EXISTS search_query TEXT;

CREATE INDEX IF NOT EXISTS videos_vehicle_idx
  ON videos (lower(make), lower(model), year_from, year_to);

CREATE INDEX IF NOT EXISTS videos_published_idx
  ON videos(status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS videos_unique_seed_idx
  ON videos(youtube_video_id, system_code, make, model) NULLS NOT DISTINCT;

CREATE UNIQUE INDEX IF NOT EXISTS videos_search_query_idx
  ON videos(search_query)
  WHERE search_query IS NOT NULL;

CREATE TABLE IF NOT EXISTS rewrite_jobs (
  id BIGSERIAL PRIMARY KEY,
  source_hash TEXT NOT NULL,
  source_text TEXT NOT NULL,
  rewritten_text TEXT,
  rules JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',
  reviewer_note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS review_queue (
  id BIGSERIAL PRIMARY KEY,
  item_type TEXT NOT NULL,
  item_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS review_queue_item_ref_idx
  ON review_queue(item_type, item_ref);

CREATE TABLE IF NOT EXISTS subscription_plans (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  interval TEXT NOT NULL DEFAULT 'month',
  stripe_price_id TEXT,
  features JSONB NOT NULL DEFAULT '[]',
  sort_order INTEGER NOT NULL DEFAULT 0,
  active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS customer_subscriptions (
  id BIGSERIAL PRIMARY KEY,
  email TEXT NOT NULL,
  plan_id TEXT REFERENCES subscription_plans(id) ON DELETE SET NULL,
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  status TEXT NOT NULL DEFAULT 'inactive',
  current_period_end TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS customer_subscriptions_email_idx
  ON customer_subscriptions(lower(email), status);
