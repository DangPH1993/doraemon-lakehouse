# Doraemon Mini Lakehouse — MVP 0.1

Một project độc lập để thu thập RSS và xây kho dữ liệu tin tức nhỏ cho Doraemon. Không thay thế hoặc sửa server/web học tập Doraemon. Admin portal được FastAPI phục vụ từ cùng một Render Web Service; dữ liệu lưu ở PostgreSQL/Supabase; tác vụ theo lịch chạy từ GitHub Actions.

## Có gì trong bản này?

- Trang admin có đăng nhập: Overview, Data Sources, Raw Layer, Clean Articles, Cleaning Rules, Topics, Entities, FACT tables, Pipeline Runs, Data Quality.
- Quản lý nguồn RSS (thêm, bật/tắt, test, xóa) và trường giấy phép.
- Raw capture lưu JSON RSS gốc; Clean layer lưu trường đã chuẩn hóa, URL canonical, hash, chất lượng và quy tắc loại trừ.
- NER bằng `spaCy en_core_web_sm` trong workflow hằng ngày; regex fallback nếu model chưa có. Thực thể chưa biết đi vào candidate list để duyệt, không tự ý trở thành entity chính thức.
- Topic assignment dựa trên từ khóa trong catalog; các cụm tiêu đề chưa khớp được khám phá bằng TF-IDF + DBSCAN và tạo topic candidates để admin duyệt.
- FACT tables: `fact_news_article`, `fact_topic_daily`, `fact_entity_daily`, `fact_source_daily`; có `data_model_catalog` để quản lý metadata, mô tả và grain của các bảng.
- Cleaning rules: loại theo từ khóa tiêu đề, URL, độ dài tiêu đề hoặc source ID; có thể áp dụng lên dữ liệu đã lưu.
- Pipeline run logging và quality issues.
- GitHub Actions chạy hằng ngày lúc 08:10 giờ Việt Nam (`01:10 UTC`) và có thể chạy thủ công.
- Có sẵn một RSS nguồn Vocemundi Wire và 7 topic khởi đầu trong schema SQL; có thể xóa/thay trong giao diện.

## Kiến trúc / hạn chế MVP

- Đây là Mini Lakehouse, chưa phải Spark/Delta Lake/Iceberg lakehouse. PostgreSQL là kho Raw/Clean/Data Mart trong giai đoạn thử nghiệm.
- Discovery hiện tối ưu cho tiêu đề tiếng Anh. Model NER mặc định là English; nếu cấu hình nguồn tiếng Việt/Nhật, chất lượng entity sẽ thấp hơn cho đến khi thêm model ngôn ngữ tương ứng.
- Topic discovery dùng clustering trên tiêu đề + mô tả RSS; chủ đề mới là ứng viên cần duyệt, không phải kết luận chắc chắn rằng đó là chủ đề mới trên thế giới.
- Pipeline giới hạn 250 entries mỗi feed mỗi lần chạy; chỉnh bằng `MAX_FEED_ENTRIES_PER_SOURCE`.
- `fact_topic_daily`, `fact_entity_daily` và `fact_source_daily` được tái tính cho cửa sổ 30 ngày gần nhất; dữ liệu fact cũ hơn giữ nguyên. Data model catalog là metadata quản trị; nó không tự tạo/xóa bảng vật lý theo cấu hình UI.
- Admin bấm “Chạy pipeline” có thể khởi chạy pipeline trên Render; cho phân tích NER tốt nhất, ưu tiên workflow GitHub Actions vì workflow cài model spaCy tiếng Anh trước khi chạy.
- RSS và metadata không đồng nghĩa với quyền tái xuất bản toàn văn. Hãy cấu hình `license_name`/`license_url` cho từng nguồn, chỉ lưu và dùng nội dung theo điều khoản áp dụng.

## A. Tạo cơ sở dữ liệu Supabase (khuyên tạo project riêng)

1. Tạo một Supabase PostgreSQL project riêng cho Lakehouse để tách biệt dữ liệu ứng dụng Doraemon.
2. Vào **SQL Editor → New query**.
3. Mở `sql/schema.sql`, copy toàn bộ nội dung, paste vào SQL Editor và Run.
4. Kiểm tra các bảng `data_sources`, `raw_feed_items`, `news_articles`, `topics`, `entities`, `topic_candidates`, `entity_candidates`, `cleaning_rules`, `pipeline_runs`, `fact_*` đã được tạo.
5. Copy kết nối PostgreSQL từ Supabase Project Settings → Database → Connection string. Với runtime cloud, dùng hostname/port connection pooler phù hợp với hướng dẫn của Supabase và bắt buộc SSL (`sslmode=require`). Không đưa URL này vào code hoặc frontend.

## B. Deploy Admin Portal trên Render

1. Tạo repository GitHub riêng (ví dụ `doraemon-mini-lakehouse`) và upload toàn bộ nội dung project này, bao gồm các file ẩn `.github/workflows/daily-pipeline.yml` và `.gitignore`.
2. Trong Render chọn **New → Blueprint**, kết nối repository và deploy `render.yaml`.
3. Trong Environment của service, cấu hình:
   - `DATABASE_URL`: connection string PostgreSQL của Supabase.
   - `ADMIN_PASSWORD`: mật khẩu mạnh, riêng cho portal.
   - `SESSION_SECRET`: chuỗi ngẫu nhiên dài ít nhất 64 ký tự (nếu Render chưa tự tạo qua `generateValue`).
   - `ADMIN_USERNAME`: có thể giữ `admin` hoặc thay đổi.
   - `CORS_ORIGINS`: nếu dùng UI/API cùng Render origin thì không cần frontend origin khác; nếu host frontend riêng, nhập origin chính xác phân tách bằng dấu phẩy.
4. Mở URL Render. Đăng nhập bằng `ADMIN_USERNAME` và `ADMIN_PASSWORD`.
5. Nếu chưa vào được dữ liệu, mở `/api/health/ready`. Nếu trả lỗi DB, kiểm tra URL/SSL/quyền kết nối và đảm bảo đã chạy `sql/schema.sql`.

Lưu ý Render Free ngủ khi không có request và filesystem là ephemeral. Database luôn nằm trong Supabase, không lưu dữ liệu quan trọng vào local disk của Render.

## C. Bật GitHub Actions pipeline

Trong repository GitHub vào **Settings → Secrets and variables → Actions → New repository secret** rồi tạo:

- `DATABASE_URL` = cùng connection string PostgreSQL của Supabase (đừng đưa vào repository).

Mở tab **Actions → Mini Lakehouse Daily Pipeline → Run workflow** để thử lần đầu. Workflow cũng chạy hằng ngày lúc 08:10 giờ Việt Nam. Private repository có hạn mức Actions theo gói GitHub; theo dõi Billing/Actions usage, không giả định vô hạn.

Workflow cài spaCy và model `en_core_web_sm` rồi chạy:

```bash
python -m app.pipeline
```

## D. Cách dùng

1. Mở **Nguồn dữ liệu**, test feed Vocemundi Wire có sẵn hoặc thêm RSS đáng tin cậy khác.
2. Chạy workflow từ Actions hoặc bấm **Chạy pipeline** trong admin.
3. Xem **Bài báo & làm sạch** để kiểm tra Raw Layer, dữ liệu Clean và các quality issues.
4. Thêm quy tắc làm sạch ở tab tương ứng. Sau khi tạo/sửa rule, bấm **Áp dụng quy tắc hiện tại** để tính lại trạng thái excluded cho bài đã lưu, hoặc đợi pipeline kế tiếp.
5. Vào **Topic** và **Entity** để duyệt candidate. Cần kiểm tra mẫu bài và nguồn trước khi phê duyệt.
6. Vào **FACT tables** để kiểm tra grain/aggregates và Overview để xem xu hướng.

## Biến môi trường

| Biến | Bắt buộc | Mục đích |
|---|---|---|
| `DATABASE_URL` | Có | PostgreSQL/Supabase connection string |
| `ADMIN_USERNAME` | Có | Tên admin portal (mặc định `admin`) |
| `ADMIN_PASSWORD` | Có | Mật khẩu admin |
| `SESSION_SECRET` | Có | Ký session token |
| `CORS_ORIGINS` | Chỉ khi frontend tách domain | Origin frontend được phép gọi API |
| `PIPELINE_LOOKBACK_DAYS` | Không | Cửa sổ discovery, mặc định 14 ngày |
| `MAX_FEED_ENTRIES_PER_SOURCE` | Không | Giới hạn entry/feed/lần chạy, mặc định 250 |
| `REQUEST_TIMEOUT_SECONDS` | Không | Timeout HTTP feed, mặc định 25 giây |
| `TIMEZONE` | Không | Múi giờ cho FACT, mặc định `Asia/Ho_Chi_Minh` |

## Cấu trúc repository

```text
app/                  FastAPI, auth, processing, RSS pipeline
static/               Admin portal (HTML/CSS/JS, không cần frontend build)
sql/schema.sql        PostgreSQL schema + starter source/topics
.github/workflows/    Daily GitHub Actions pipeline
 tests/               Unit tests xử lý URL/HTML/rules
render.yaml           Render Blueprint
```

## Kiểm tra local

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # điền thông tin kết nối
uvicorn app.main:app --reload
python -m unittest discover -s tests -v
```

File `.env` được load tự động khi chạy local. Không commit file `.env`.

## Security checklist

- Dùng Supabase project riêng, không dùng credential của production Doraemon trong repo.
- Không commit `.env`, database password hoặc `SESSION_SECRET`.
- Dùng mật khẩu admin dài, thay `SESSION_SECRET` nếu nghi ngờ lộ.
- Chỉ thêm feed URL do bạn tin cậy; nguồn do admin nhập được máy chủ gọi HTTP.
- API quản trị đều yêu cầu token; không public `DATABASE_URL` hoặc `ADMIN_PASSWORD` ở frontend.
- Chạy `sql/schema.sql` với role có quyền DDL. Runtime role nên được thu hẹp quyền khi triển khai production.
