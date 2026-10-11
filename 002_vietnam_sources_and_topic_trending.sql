-- Migration 002: Vietnamese RSS sources, Vietnamese topics, topic hotness FACT.
-- Safe to run again. Does not delete articles already collected from Vocemundi.
BEGIN;

UPDATE data_sources
SET enabled = false,
    name = CASE WHEN name ILIKE '%(đã ngừng)%' THEN name ELSE name || ' (đã ngừng)' END,
    updated_at = now()
WHERE feed_url = 'https://vocemundi.com/wire/feeds/en/' OR name = 'Vocemundi Wire';

INSERT INTO data_sources(name,feed_url,language,category,license_name,license_url,enabled)
VALUES
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
ON CONFLICT (feed_url) DO UPDATE SET
 name=EXCLUDED.name, language=EXCLUDED.language, category=EXCLUDED.category,
 license_name=EXCLUDED.license_name, license_url=EXCLUDED.license_url, updated_at=now();

INSERT INTO topics(slug,name,description,keywords,origin) VALUES
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
ON CONFLICT(slug) DO UPDATE SET
 name=EXCLUDED.name,
 description=EXCLUDED.description,
 keywords=(SELECT ARRAY(SELECT DISTINCT u.keyword FROM unnest(topics.keywords || EXCLUDED.keywords) AS u(keyword) WHERE trim(u.keyword) <> '')),
 updated_at=now();

CREATE TABLE IF NOT EXISTS fact_topic_trending (
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
CREATE INDEX IF NOT EXISTS idx_fact_topic_trending_period_rank
  ON fact_topic_trending(period_type, period_start, rank_position);

INSERT INTO data_model_catalog(table_name,model_type,description,grain)
VALUES ('fact_topic_trending','fact','Hot-topic ranking by day, week and month; article volume, source coverage and period growth','One record per period type + period start + topic')
ON CONFLICT(table_name) DO UPDATE SET description=EXCLUDED.description, grain=EXCLUDED.grain, updated_at=now();

COMMIT;
