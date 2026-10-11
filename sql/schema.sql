-- Doraemon Mini Lakehouse MVP schema (PostgreSQL / Supabase)
-- Run this file once in Supabase SQL Editor.

create table if not exists data_sources (
  source_id bigserial primary key,
  name text not null,
  feed_url text not null unique,
  language text not null default 'en',
  category text not null default 'general',
  license_name text not null default 'check_required',
  license_url text,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  last_run_at timestamptz,
  last_status text,
  last_error text
);

insert into data_sources(name,feed_url,language,category,license_name,license_url,enabled)
values('Vocemundi Wire','https://vocemundi.com/wire/feeds/en/','en','international news','CC BY-ND 4.0 text; verify media separately','https://vocemundi.com/republish/',true)
on conflict(feed_url) do nothing;

create table if not exists pipeline_runs (
  run_id bigserial primary key,
  trigger_type text not null default 'scheduled',
  status text not null default 'running',
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  source_count int not null default 0,
  entries_seen int not null default 0,
  articles_inserted int not null default 0,
  articles_updated int not null default 0,
  entities_found int not null default 0,
  entity_candidates int not null default 0,
  topic_candidates int not null default 0,
  quality_issues int not null default 0,
  message text,
  details jsonb not null default '{}'::jsonb
);

create table if not exists raw_feed_items (
  raw_id bigserial primary key,
  source_id bigint not null references data_sources(source_id) on delete cascade,
  entry_key text not null,
  item_url text,
  raw_payload jsonb not null default '{}'::jsonb,
  fetched_at timestamptz not null default now(),
  unique(source_id, entry_key)
);
create index if not exists idx_raw_feed_items_fetched on raw_feed_items(fetched_at desc);

create table if not exists news_articles (
  article_id bigserial primary key,
  source_id bigint not null references data_sources(source_id) on delete cascade,
  raw_id bigint references raw_feed_items(raw_id) on delete set null,
  external_id text,
  url text not null,
  canonical_url text not null,
  title text not null,
  summary text not null default '',
  content_text text not null default '',
  author text,
  language text not null default 'en',
  published_at timestamptz,
  collected_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  headline_hash text not null,
  content_hash text,
  is_excluded boolean not null default false,
  exclude_reason text,
  quality_score numeric(5,2) not null default 100,
  unique(source_id, canonical_url)
);
create index if not exists idx_news_articles_published on news_articles(published_at desc nulls last);
create index if not exists idx_news_articles_collected on news_articles(collected_at desc);
create index if not exists idx_news_articles_headline_hash on news_articles(headline_hash);
create index if not exists idx_news_articles_source on news_articles(source_id, collected_at desc);

create table if not exists topics (
  topic_id bigserial primary key,
  slug text not null unique,
  name text not null,
  description text not null default '',
  keywords text[] not null default '{}',
  parent_topic_id bigint references topics(topic_id) on delete set null,
  status text not null default 'active' check (status in ('active','archived')),
  origin text not null default 'manual' check (origin in ('manual','discovered')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists entities (
  entity_id bigserial primary key,
  canonical_name text not null,
  normalized_name text not null,
  entity_type text not null,
  external_id text,
  description text not null default '',
  status text not null default 'active' check (status in ('active','archived')),
  origin text not null default 'manual' check (origin in ('manual','discovered')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(normalized_name, entity_type)
);

create table if not exists entity_aliases (
  alias_id bigserial primary key,
  entity_id bigint not null references entities(entity_id) on delete cascade,
  alias text not null,
  normalized_alias text not null,
  unique(entity_id, normalized_alias),
  unique(normalized_alias, entity_id)
);

create table if not exists article_topics (
  article_id bigint not null references news_articles(article_id) on delete cascade,
  topic_id bigint not null references topics(topic_id) on delete cascade,
  confidence numeric(5,4) not null default 1,
  assignment_method text not null default 'keyword',
  created_at timestamptz not null default now(),
  primary key(article_id, topic_id)
);
create index if not exists idx_article_topics_topic on article_topics(topic_id, article_id);

create table if not exists article_entities (
  article_id bigint not null references news_articles(article_id) on delete cascade,
  entity_id bigint not null references entities(entity_id) on delete cascade,
  mention_text text not null,
  confidence numeric(5,4) not null default 0.8,
  created_at timestamptz not null default now(),
  primary key(article_id, entity_id, mention_text)
);
create index if not exists idx_article_entities_entity on article_entities(entity_id, article_id);

create table if not exists entity_candidates (
  candidate_id bigserial primary key,
  canonical_name text not null,
  normalized_name text not null,
  entity_type text not null,
  status text not null default 'pending' check (status in ('pending','approved','rejected','merged')),
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  unique(normalized_name, entity_type)
);
create table if not exists entity_candidate_occurrences (
  candidate_id bigint not null references entity_candidates(candidate_id) on delete cascade,
  article_id bigint not null references news_articles(article_id) on delete cascade,
  source_id bigint not null references data_sources(source_id) on delete cascade,
  mention_text text not null,
  created_at timestamptz not null default now(),
  primary key(candidate_id, article_id)
);

create table if not exists topic_candidates (
  candidate_id bigserial primary key,
  cluster_key text not null unique,
  suggested_name text not null,
  keywords text[] not null default '{}',
  status text not null default 'pending' check (status in ('pending','approved','rejected','merged')),
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);
create table if not exists topic_candidate_occurrences (
  candidate_id bigint not null references topic_candidates(candidate_id) on delete cascade,
  article_id bigint not null references news_articles(article_id) on delete cascade,
  source_id bigint not null references data_sources(source_id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key(candidate_id, article_id)
);

create table if not exists cleaning_rules (
  rule_id bigserial primary key,
  name text not null,
  rule_type text not null check (rule_type in ('exclude_title_contains','exclude_url_contains','min_title_length','exclude_source')),
  config jsonb not null default '{}'::jsonb,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists data_quality_issues (
  issue_id bigserial primary key,
  article_id bigint references news_articles(article_id) on delete cascade,
  source_id bigint references data_sources(source_id) on delete cascade,
  issue_type text not null,
  severity text not null default 'warning' check (severity in ('info','warning','error')),
  details text not null,
  detected_at timestamptz not null default now(),
  resolved_at timestamptz
);
create index if not exists idx_quality_open on data_quality_issues(resolved_at, severity, detected_at desc);

create table if not exists data_model_catalog (
  model_id bigserial primary key,
  table_name text not null unique,
  model_type text not null check (model_type in ('fact','dimension','bridge','operational')),
  description text not null default '',
  grain text not null default '',
  owner text not null default 'Data Platform',
  active boolean not null default true,
  updated_at timestamptz not null default now()
);

-- FACT tables: explicit, documented grain.
create table if not exists fact_news_article (
  article_id bigint primary key references news_articles(article_id) on delete cascade,
  source_id bigint not null references data_sources(source_id) on delete cascade,
  published_date date,
  title_length int not null,
  word_count int not null,
  detected_entity_count int not null default 0,
  detected_topic_count int not null default 0,
  quality_score numeric(5,2) not null default 100,
  is_excluded boolean not null default false,
  loaded_at timestamptz not null default now()
);
create table if not exists fact_topic_daily (
  stat_date date not null,
  topic_id bigint not null references topics(topic_id) on delete cascade,
  source_id bigint not null references data_sources(source_id) on delete cascade,
  article_count int not null default 0,
  updated_at timestamptz not null default now(),
  primary key(stat_date, topic_id, source_id)
);
create table if not exists fact_entity_daily (
  stat_date date not null,
  entity_id bigint not null references entities(entity_id) on delete cascade,
  source_id bigint not null references data_sources(source_id) on delete cascade,
  article_count int not null default 0,
  updated_at timestamptz not null default now(),
  primary key(stat_date, entity_id, source_id)
);
create table if not exists fact_source_daily (
  stat_date date not null,
  source_id bigint not null references data_sources(source_id) on delete cascade,
  article_count int not null default 0,
  excluded_count int not null default 0,
  avg_quality_score numeric(5,2) not null default 100,
  updated_at timestamptz not null default now(),
  primary key(stat_date, source_id)
);

-- Starter topics; change keywords from the admin portal at any time.
insert into topics(slug,name,description,keywords,origin) values
 ('artificial-intelligence','Artificial Intelligence','AI, machine learning, and AI products', array['artificial intelligence','AI model','machine learning','generative AI','AI agent','ChatGPT','OpenAI','LLM'], 'manual'),
 ('technology','Technology','Technology products and industry', array['technology','software','cybersecurity','semiconductor','chipmaker','smartphone','cloud computing'], 'manual'),
 ('economy-business','Economy & Business','Markets, companies, economic policy', array['economy','inflation','interest rate','GDP','stock market','earnings','investment','trade'], 'manual'),
 ('politics-policy','Politics & Policy','Government, elections and public policy', array['election','government','parliament','president','minister','policy','sanction'], 'manual'),
 ('defence-security','Defence & Security','Military, defence and security', array['NATO','military','defence','defense','nuclear deterrence','missile','armed forces','security'], 'manual'),
 ('climate-environment','Climate & Environment','Climate, energy and environment', array['climate','emissions','renewable energy','solar power','wind power','fossil fuel','biodiversity'], 'manual'),
 ('health-science','Health & Science','Health, research and science', array['health','disease','clinical trial','research','scientists','vaccine','study finds'], 'manual')
on conflict(slug) do nothing;


insert into data_model_catalog(table_name,model_type,description,grain) values
 ('news_articles','operational','Clean-layer news items with canonical URL and quality status','One record per source + canonical URL'),
 ('raw_feed_items','operational','Raw parsed RSS entry payloads for traceability','One latest payload per source + entry key'),
 ('data_sources','dimension','Configured RSS source registry and sync status','One record per source'),
 ('topics','dimension','Approved topic taxonomy and matching keywords','One record per topic'),
 ('entities','dimension','Approved normalized entity catalog','One record per normalized entity + type'),
 ('entity_aliases','operational','Alternative names linked to approved entities','One record per entity + normalized alias'),
 ('article_topics','bridge','Article-to-topic relationship with confidence and method','One record per article + topic'),
 ('article_entities','bridge','Article-to-entity mentions','One record per article + entity + mention text'),
 ('fact_news_article','fact','Article-level analytical metrics','One record per article'),
 ('fact_topic_daily','fact','Daily article counts by topic and source','One record per day + topic + source'),
 ('fact_entity_daily','fact','Daily article counts by entity and source','One record per day + entity + source'),
 ('fact_source_daily','fact','Daily collection and quality metrics by source','One record per day + source')
on conflict(table_name) do update set description=excluded.description,grain=excluded.grain;
