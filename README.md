# Doraemon Mini Lakehouse — MVP 0.2

Một project độc lập để thu thập RSS và xây kho dữ liệu tin tức nhỏ cho Doraemon. Không thay thế hoặc sửa server/web học tập Doraemon. Admin portal được FastAPI phục vụ từ cùng một Render Web Service; dữ liệu lưu ở PostgreSQL/Supabase; tác vụ theo lịch chạy từ GitHub Actions.

## Có gì trong bản này?

- Trang admin có đăng nhập: Overview, Data Sources, Raw Layer, Clean Articles, Cleaning Rules, Topics, Entities, FACT tables, Pipeline Runs, Data Quality.
- Quản lý nguồn RSS (thêm, bật/tắt, test, xóa) và trường giấy phép.
- Raw capture lưu JSON RSS gốc; Clean layer lưu trường đã chuẩn hóa, URL canonical, hash, chất lượng và quy tắc loại trừ.
- NER bằng `spaCy en_core_web_sm` trong workflow hằng ngày; regex fallback nếu model chưa có. Thực thể chưa biết đi vào candidate list để duyệt, không tự ý trở thành entity chính thức.
- Topic assignment dựa trên từ khóa trong catalog; các cụm tiêu đề chưa khớp được khám phá bằng TF-IDF + DBSCAN và tạo topic candidates để admin duyệt.
- FACT tables: `fact_news_article`, `fact_topic_daily`, `fact_entity_daily`, `fact_source_daily`, `fact_topic_trending`; bảng mới xếp hạng topic nóng theo ngày, tuần, tháng với số bài, độ phủ nguồn, so sánh kỳ trước và hotness score. Admin dashboard hiển thị 3 bảng xếp hạng.
- Cleaning rules: loại theo từ khóa tiêu đề, URL, độ dài tiêu đề hoặc source ID; có thể áp dụng lên dữ liệu đã lưu.
- Pipeline run logging và quality issues.
- GitHub Actions chạy hằng ngày lúc 08:10 giờ Việt Nam (`01:10 UTC`) và có thể chạy thủ công.
- Cấu hình sẵn các nguồn tin Việt Nam: VnExpress, Tuổi Trẻ, Thanh Niên, Dân trí, VTV, VietnamPlus, Báo Tin tức & Dân tộc, Báo Quân đội nhân dân, Báo Văn hóa và Infonet. Vocemundi Wire được loại khỏi nguồn hoạt động; migration tắt nguồn cũ nhưng giữ lịch sử đã nạp. Một số nguồn được để tắt nếu URL RSS trực tiếp cần xác minh.

## Kiến trúc / hạn chế MVP

- Đây là Mini Lakehouse, chưa phải Spark/Delta Lake/Iceberg lakehouse. PostgreSQL là kho Raw/Clean/Data Mart trong giai đoạn thử nghiệm.
- Topic keyword matching và phân cụm tiêu đề đã hỗ trợ văn bản tiếng Việt và tiếng Anh ở mức cơ bản. Model NER mặc định vẫn là English (`en_core_web_sm`), vì vậy việc nhận diện người/tổ chức/địa điểm trong tin tiếng Việt chưa tối ưu và cần được đánh giá trước khi dùng cho phân tích quan trọng.
- Topic discovery dùng clustering trên tiêu đề + mô tả RSS; chủ đề mới là ứng viên cần duyệt, không phải kết luận chắc chắn rằng đó là chủ đề mới trên thế giới.
- Pipeline ingest các entries hiện có trong RSS feed theo thứ tự nguồn trả về, tối đa 250 entries/feed/lần chạy; không tự giới hạn bài xuất bản trong ngày. RSS thường chỉ cung cấp một cửa sổ các bài gần đây, không phải kho lưu trữ toàn bộ lịch sử. Có thể chỉnh giới hạn bằng `MAX_FEED_ENTRIES_PER_SOURCE`.
- `fact_topic_daily`, `fact_entity_daily` và `fact_source_daily` được tái tính cho cửa sổ 30 ngày gần nhất; dữ liệu fact cũ hơn giữ nguyên. Data model catalog là metadata quản trị; nó không tự tạo/xóa bảng vật lý theo cấu hình UI.
- Admin bấm “Chạy pipeline” có thể khởi chạy pipeline trên Render; cho phân tích NER tốt nhất, ưu tiên workflow GitHub Actions vì workflow cài model spaCy tiếng Anh trước khi chạy.
- RSS và metadata không đồng nghĩa với quyền tái xuất bản toàn văn. Hãy cấu hình `license_name`/`license_url` cho từng nguồn, chỉ lưu và dùng nội dung theo điều khoản áp dụng.

## A. Tạo cơ sở dữ liệu Supabase (khuyên tạo project riêng)

1. Tạo một Supabase PostgreSQL project riêng cho Lakehouse để tách biệt dữ liệu ứng dụng Doraemon.
2. Vào **SQL Editor → New query**.
3. Nếu đây là database hoàn toàn mới, mở `sql/schema.sql`, copy toàn bộ nội dung, paste vào SQL Editor và Run.
4. Nếu đã triển khai v0.1/v0.1.1 và đang có dữ liệu, KHÔNG chạy lại toàn bộ schema để cập nhật; thay vào đó chạy một lần `sql/migrations/002_vietnam_sources_and_topic_trending.sql` trong SQL Editor. Migration idempotent, thêm nguồn/topic Việt Nam, tắt Vocemundi Wire và tạo FACT mới nhưng giữ các bài cũ.
5. Kiểm tra các bảng `data_sources`, `raw_feed_items`, `news_articles`, `topics`, `entities`, `topic_candidates`, `entity_candidates`, `cleaning_rules`, `pipeline_runs`, `fact_*` đã được tạo.
6. Copy kết nối PostgreSQL từ Supabase Project Settings → Database → Connection string. Với runtime cloud, dùng hostname/port connection pooler phù hợp với hướng dẫn của Supabase và bắt buộc SSL (`sslmode=require`). Không đưa URL này vào code hoặc frontend.

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

Mở tab **Actions → Mini Lakehouse Daily Pipeline → Run workflow** để chạy thủ công. Workflow cũng chạy hằng ngày lúc 08:10 giờ Việt Nam. Trước khi chạy, cần commit phiên bản code mới nhất để tránh lỗi SQL refresh FACT đã được sửa trong v0.1.1. Private repository có hạn mức Actions theo gói GitHub; theo dõi Billing/Actions usage, không giả định vô hạn.

Workflow cài spaCy và model `en_core_web_sm` rồi chạy:

```bash
python -m app.pipeline
```

## D. Cách dùng

1. Mở **Nguồn dữ liệu**, bấm Test RSS cho các nguồn bật sẵn. Các nguồn đang tắt cần chỉnh URL RSS trực tiếp nếu feed directory không trả XML rồi mới bật.
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

## Topic nóng theo ngày/tuần/tháng

Bảng `fact_topic_trending` có grain một dòng cho mỗi `period_type` (`day`, `week`, `month`) + `period_start` + topic. `article_count` là số bài không bị loại trừ có liên kết với topic trong kỳ; `source_count` là số nguồn khác nhau; `growth_pct` so với ngày/tuần/tháng liền trước khi kỳ trước có dữ liệu. `hotness_score` = bài viết + `2 * ln(1 + số nguồn)` + bonus tăng trưởng dương bị giới hạn tối đa 2 điểm. Đây là chỉ số xu hướng truyền thông, không phải đánh giá độ đúng hoặc độ quan trọng của tin. Dashboard xếp hạng theo `hotness_score`, sau đó số bài và số nguồn.

## Cấu trúc repository

```text
app/                  FastAPI, auth, processing, RSS pipeline
static/               Admin portal (HTML/CSS/JS, không cần frontend build)
sql/schema.sql        PostgreSQL schema + starter source/topics
.github/workflows/    Daily GitHub Actions pipeline
 tests/               Unit tests xử lý URL/HTML/rules
render.yaml           Render Blueprint
```

## Chỉ số độ nóng topic

Dashboard hiển thị 3 bảng xếp hạng độc lập: **Hôm nay** (ngày lịch theo `TIMEZONE`), **Tuần này** (tuần bắt đầu thứ Hai), và **Tháng này** (tháng lịch). Mỗi bảng hiển thị số bài không bị loại trừ đã gắn topic, số nguồn độc lập và phần trăm thay đổi so với ngày/tuần/tháng trước. Nếu kỳ trước chưa có dữ liệu, tăng trưởng hiện là `—`. FACT mới được tính lại cuối mỗi pipeline; sau khi chạy migration, cần chạy pipeline để backfill.

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
