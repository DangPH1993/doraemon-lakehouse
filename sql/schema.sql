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
  last_error text,
  etag text,
  last_modified text
);

insert into data_sources(name,feed_url,language,category,license_name,license_url,enabled)
values
 ('VnExpress - Tin mới nhất','https://vnexpress.net/rss/tin-moi-nhat.rss','vi','tổng hợp','RSS; chỉ dùng metadata theo điều khoản nhà xuất bản','https://vnexpress.net/rss',true),
 ('Tuổi Trẻ - Trang chủ','https://tuoitre.vn/home.rss','vi','tổng hợp','RSS; ghi nguồn; kiểm tra điều khoản hiện hành','https://tuoitre.vn/rss.htm',true),
 ('Thanh Niên - Trang chủ','https://thanhnien.vn/rss/home.rss','vi','tổng hợp','RSS; ghi nguồn; kiểm tra điều khoản hiện hành','https://thanhnien.vn/rss.html',true),
 ('Dân trí - Trang chủ','https://dantri.com.vn/rss/home.rss','vi','tổng hợp','RSS; ghi nguồn; không đăng lại toàn văn nếu chưa được phép','https://dantri.com.vn/rss.htm',true),
 ('VTV - Trang chủ','https://vtv.vn/rss/home.rss','vi','tổng hợp','RSS; attribution: Theo Báo điện tử VTV','https://vtv.vn/rss.htm',true),
 ('VietnamPlus - Trang chủ','https://www.vietnamplus.vn/rss/home.rss','vi','tổng hợp','RSS; chỉ dùng metadata; tái xuất bản cần chấp thuận','https://www.vietnamplus.vn/rss.vnp',true),
 ('Báo Tin tức & Dân tộc - Trang chủ','https://baotintuc.vn/rss/home.rss','vi','tổng hợp','RSS; cần xác nhận endpoint và điều khoản trước khi bật','https://baotintuc.vn/rss.html',false),
 ('Báo Quân đội nhân dân - RSS','https://www.qdnd.vn/rss','vi','quốc phòng; chưa xác nhận feed XML trực tiếp','RSS directory; chưa bật cho đến khi đặt URL feed trực tiếp','https://www.qdnd.vn/rss',false),
 ('Báo Văn hóa - RSS','https://baovanhoa.vn/rss.html','vi','văn hóa; chưa xác nhận feed XML trực tiếp','RSS directory; chưa bật cho đến khi đặt URL feed trực tiếp','https://baovanhoa.vn/rss.html',false),
 ('Infonet - Đời sống','https://infonet.vietnamnet.vn/rss/doi-song.rss','vi','đời sống','RSS; ghi nguồn và chỉ dùng theo điều khoản nhà xuất bản','https://infonet.vietnamnet.vn/rss',true)
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
  entries_skipped_existing int not null default 0,
  feeds_not_modified int not null default 0,
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
  event_topic_scanned_at timestamptz,
  ai_classified_at timestamptz,
  ai_classification jsonb not null default '{}'::jsonb,
  ai_classification_error text,
  unique(source_id, canonical_url)
);
create index if not exists idx_news_articles_published on news_articles(published_at desc nulls last);
create index if not exists idx_news_articles_collected on news_articles(collected_at desc);
create index if not exists idx_news_articles_headline_hash on news_articles(headline_hash);
create index if not exists idx_news_articles_source on news_articles(source_id, collected_at desc);
create index if not exists idx_news_articles_ai_pending on news_articles (coalesce(published_at, collected_at) desc) where ai_classified_at is null and is_excluded=false;

create table if not exists topics (
  topic_id bigserial primary key,
  slug text not null unique,
  name text not null,
  description text not null default '',
  keywords text[] not null default '{}',
  parent_topic_id bigint references topics(topic_id) on delete set null,
  topic_type text not null default 'category' check (topic_type in ('category','event')),
  event_key text unique,
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
  candidate_type text not null default 'event' check (candidate_type in ('category','event')),
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
create table if not exists fact_topic_trending (
  period_type text not null check (period_type in ('day','week','month')),
  period_start date not null,
  topic_id bigint not null references topics(topic_id) on delete cascade,
  article_count int not null default 0,
  source_count int not null default 0,
  previous_article_count int not null default 0,
  growth_pct numeric(10,2),
  hotness_score numeric(12,3) not null default 0,
  rank_position int not null default 0,
  updated_at timestamptz not null default now(),
  primary key(period_type, period_start, topic_id)
);
create index if not exists idx_fact_topic_trending_period_rank
  on fact_topic_trending(period_type, period_start, rank_position);

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

-- Starter topics: Vietnamese news categories with Vietnamese and English keywords.
insert into topics(slug,name,description,keywords,origin) values
 ('artificial-intelligence','Trí tuệ nhân tạo','AI, mô hình AI, học máy và ứng dụng AI',array['trí tuệ nhân tạo','AI','mô hình AI','học máy','AI tạo sinh','trợ lý AI','ChatGPT','OpenAI','LLM','artificial intelligence','machine learning','generative AI'],'manual'),
 ('technology','Công nghệ','Công nghệ, phần mềm, an ninh mạng, chip và sản phẩm số',array['công nghệ','phần mềm','an ninh mạng','bán dẫn','chip','điện thoại thông minh','điện toán đám mây','chuyển đổi số','technology','software','cybersecurity'],'manual'),
 ('economy-business','Kinh tế & doanh nghiệp','Kinh tế vĩ mô, thị trường, doanh nghiệp và đầu tư',array['kinh tế','lạm phát','lãi suất','GDP','chứng khoán','doanh nghiệp','đầu tư','xuất khẩu','nhập khẩu','thương mại','tăng trưởng kinh tế','economy','inflation','interest rate'],'manual'),
 ('politics-policy','Chính trị & chính sách','Chính phủ, chính sách công, bầu cử và nghị viện',array['chính phủ','chính sách','quốc hội','bầu cử','thủ tướng','chủ tịch nước','bộ trưởng','nghị định','luật mới','politics','government','election','policy'],'manual'),
 ('defence-security','Quốc phòng & an ninh','Quốc phòng, quân sự, an ninh và quan hệ chiến lược',array['quốc phòng','an ninh','quân sự','NATO','tên lửa','hạt nhân','biên giới','lực lượng vũ trang','defence','defense','military','missile'],'manual'),
 ('climate-environment','Môi trường & khí hậu','Khí hậu, môi trường, năng lượng tái tạo và thiên tai',array['môi trường','biến đổi khí hậu','khí hậu','phát thải','năng lượng tái tạo','điện mặt trời','điện gió','ô nhiễm','bão','lũ lụt','climate','emissions','renewable energy'],'manual'),
 ('health-science','Y tế, sức khỏe & khoa học','Sức khỏe, bệnh tật, y tế và nghiên cứu khoa học',array['sức khỏe','y tế','bệnh','vắc xin','dịch bệnh','bệnh viện','bác sĩ','nghiên cứu khoa học','thí nghiệm lâm sàng','health','disease','vaccine','science'],'manual'),
 ('social-daily-life','Xã hội & đời sống','An sinh, cộng đồng, đời sống dân sinh và các vấn đề xã hội',array['xã hội','đời sống','dân sinh','người dân','cộng đồng','an sinh xã hội','tai nạn','cháy nổ','xã hội hóa','social','community'],'manual'),
 ('education','Giáo dục','Trường học, đào tạo, tuyển sinh và chính sách giáo dục',array['giáo dục','trường học','học sinh','sinh viên','giáo viên','tuyển sinh','thi tốt nghiệp','đại học','học phí','education','school','students'],'manual'),
 ('sports','Thể thao','Bóng đá, thể thao thành tích cao và các giải đấu',array['thể thao','bóng đá','đội tuyển','giải đấu','vận động viên','World Cup','SEA Games','V-League','Olympic','sports','football'],'manual'),
 ('culture-tourism','Văn hóa & du lịch','Văn hóa, nghệ thuật, giải trí, du lịch và di sản',array['văn hóa','du lịch','di sản','nghệ thuật','điện ảnh','âm nhạc','lễ hội','khách du lịch','điểm đến','culture','tourism','heritage'],'manual'),
 ('real-estate','Bất động sản & hạ tầng','Nhà đất, xây dựng, giao thông và hạ tầng',array['bất động sản','nhà đất','chung cư','giá nhà','thị trường bất động sản','xây dựng','hạ tầng','giao thông','đường cao tốc','quy hoạch','real estate','infrastructure'],'manual')
on conflict(slug) do nothing;

insert into data_model_catalog(table_name,model_type,description,grain) values
 ('news_articles','operational','Clean-layer news items with canonical URL and quality status','One record per source + canonical URL'),
 ('raw_feed_items','operational','Raw parsed RSS entry payloads for traceability','One latest payload per source + entry key'),
 ('data_sources','dimension','Configured RSS source registry and sync status','One record per source'),
 ('topics','dimension','Approved topic taxonomy and matching keywords','One record per topic'),
 ('entities','dimension','Approved normalized entity catalog','One record per normalized entity + type'),
 ('entity_aliases','operational','Alternative names linked to approved entities','One record per entity + normalized alias'),
 ('article_topics','bridge','Article-to-topic relationship with confidence and method','One record per article + topic'),
 ('article_entities','bridge','Article-to-entity mentions, automatically normalized by GenAI','One record per article + entity + mention text'),
 ('news_articles.ai_classification','operational','GenAI event/topic/entity classification result and processing state','One classification result per article'),
 ('fact_news_article','fact','Article-level analytical metrics','One record per article'),
 ('fact_topic_daily','fact','Daily article counts by topic and source','One record per day + topic + source'),
 ('fact_topic_trending','fact','Specific event-topic heat ranking by day, week and month with source coverage and period-over-period change','One record per period type + period start + event topic'),
 ('fact_entity_daily','fact','Daily article counts by entity and source','One record per day + entity + source'),
 ('fact_source_daily','fact','Daily collection and quality metrics by source','One record per day + source')
on conflict(table_name) do update set description=excluded.description,grain=excluded.grain;

-- News Radar user accounts and per-user saved timeline preferences.
-- Safe to rerun; existing news/article/topic/entity data is not modified.

CREATE TABLE IF NOT EXISTS app_users (
    user_id BIGSERIAL PRIMARY KEY,
    username VARCHAR(32) NOT NULL,
    username_normalized VARCHAR(32) NOT NULL UNIQUE,
    email VARCHAR(254) NOT NULL,
    email_normalized VARCHAR(254) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    telegram_chat_id TEXT,
    telegram_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    telegram_notifications_since TIMESTAMPTZ,
    telegram_link_code TEXT,
    telegram_link_expires_at TIMESTAMPTZ,
    telegram_linked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_saved_timelines (
    saved_timeline_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
    timeline_type TEXT NOT NULL CHECK (timeline_type IN ('topic', 'entity')),
    topic_id BIGINT REFERENCES topics(topic_id) ON DELETE CASCADE,
    entity_id BIGINT REFERENCES entities(entity_id) ON DELETE CASCADE,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_saved_timeline_target CHECK (
        (timeline_type = 'topic' AND topic_id IS NOT NULL AND entity_id IS NULL)
        OR
        (timeline_type = 'entity' AND entity_id IS NOT NULL AND topic_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_saved_topic
    ON user_saved_timelines(user_id, topic_id) WHERE timeline_type = 'topic';
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_saved_entity
    ON user_saved_timelines(user_id, entity_id) WHERE timeline_type = 'entity';
CREATE INDEX IF NOT EXISTS idx_user_saved_timelines_user_order
    ON user_saved_timelines(user_id, display_order, saved_timeline_id);

-- Telegram notifications and per-user delivery deduplication (v0.6.4).
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_telegram_chat
    ON app_users(telegram_chat_id) WHERE telegram_chat_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_telegram_link_code
    ON app_users(telegram_link_code) WHERE telegram_link_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_app_users_telegram_active
    ON app_users(user_id) WHERE telegram_enabled = TRUE AND telegram_chat_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS user_telegram_deliveries (
    user_id BIGINT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
    article_id BIGINT NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_user_telegram_deliveries_sent_at
    ON user_telegram_deliveries(sent_at);
