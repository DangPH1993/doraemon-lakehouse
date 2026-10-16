"""Canonical editorial sections for Radar (separate from event topics)."""
from __future__ import annotations
import re
import unicodedata

FIELD_CATEGORIES = (
    'Thời sự', 'Thế giới', 'Kinh doanh', 'Bất động sản', 'Pháp luật', 'Giáo dục',
    'Sức khỏe', 'Đời sống', 'Du lịch', 'Khoa học', 'Số hóa', 'Xe', 'Thể thao',
    'Văn hóa - Giải trí', 'Môi trường', 'Ý kiến - Góc nhìn',
)

def _fold(value: str) -> str:
    value = str(value or '').replace('đ', 'd').replace('Đ', 'D')
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()

_CATEGORY_ALIASES = {
    _fold(label): label for label in FIELD_CATEGORIES
}
_CATEGORY_ALIASES.update({
    'van hoa du lich': 'Văn hóa - Giải trí',
    'van hoa va du lich': 'Văn hóa - Giải trí',
    'van hoa giai tri': 'Văn hóa - Giải trí',
    'cong nghe': 'Số hóa', 'cong nghe so': 'Số hóa', 'so hoa cong nghe': 'Số hóa',
    'y te': 'Sức khỏe', 'suc khoe khoa hoc': 'Sức khỏe',
    'chinh tri chinh sach': 'Thời sự', 'xa hoi doi song': 'Đời sống',
    'kinh te doanh nghiep': 'Kinh doanh', 'bat dong san ha tang': 'Bất động sản',
    'moi truong khi hau': 'Môi trường', 'y kien': 'Ý kiến - Góc nhìn',
    'goc nhin': 'Ý kiến - Góc nhìn', 'giao duc dao tao': 'Giáo dục',
})

# Priority-ordered terms also used to create a SQL fallback for legacy records that
# have not yet been through the new classifier. Keep phrases specific, not broad labels.

# Labels that appeared historically as Topics even though they are editorial fields.
# Keep these out of the event topic catalog and migrate their article links to field_category.
LEGACY_BROAD_TOPIC_LABELS = (
    'Thời sự', 'Thế giới', 'Kinh doanh', 'Bất động sản', 'Pháp luật', 'Giáo dục',
    'Sức khỏe', 'Đời sống', 'Du lịch', 'Khoa học', 'Số hóa', 'Xe', 'Thể thao',
    'Văn hóa - Giải trí', 'Môi trường', 'Ý kiến - Góc nhìn',
    'Văn hóa & du lịch', 'Văn hóa và du lịch', 'Văn hóa & giải trí', 'Văn hóa và giải trí',
    'Xã hội & đời sống', 'Xã hội và đời sống', 'Chính trị & chính sách', 'Chính trị và chính sách',
    'Công nghệ', 'Công nghệ số', 'Kinh tế & doanh nghiệp', 'Kinh doanh & thị trường',
    'Bất động sản & hạ tầng', 'Y tế & sức khỏe', 'Y tế, sức khỏe và khoa học',
    'Tin tức trong ngày', 'Thời sự và chính sách', 'Công nghệ và đời sống', 'Xã hội đời sống',
    'Văn hóa du lịch', 'Y tế, sức khỏe & khoa học', 'Tin nổi bật', 'Tin tức nổi bật',
    'Artificial Intelligence', 'Technology', 'Economy & Business', 'Politics & Policy',
    'Defence & Security', 'Climate & Environment', 'Health & Science',
)

FIELD_KEYWORDS = {
    'Thế giới': ('ngoại giao', 'đại sứ mỹ', 'nhà trắng', 'quốc hội mỹ', 'trump', 'putin', 'ukraine', 'nga tấn công', 'trung đông', 'iran', 'israel', 'triều tiên', 'hàn quốc', 'trung quốc', 'đài loan', 'bầu cử mỹ', 'world cup'),
    'Pháp luật': ('khởi tố', 'bắt tạm giam', 'tạm giam', 'truy tố', 'xét xử', 'kháng cáo', 'bản án', 'bị cáo', 'nghi can', 'nghi phạm', 'điều tra vụ án', 'lừa đảo', 'tử vong do', 'vi phạm pháp luật', 'công an điều tra'),
    'Thể thao': ('bóng đá', 'đội tuyển', 'huấn luyện viên', 'cầu thủ', 'world cup', 'olympic', 'sea games', 'vô địch', 'quả bóng vàng', 'tennis', 'bóng chuyền', 'bóng rổ'),
    'Sức khỏe': ('sức khỏe', 'bệnh viện', 'bác sĩ', 'bệnh nhân', 'ung thư', 'đột quỵ', 'vắc xin', 'dịch bệnh', 'điều trị', 'y tế', 'mắc bệnh', 'sốt xuất huyết'),
    'Giáo dục': ('giáo dục', 'học sinh', 'sinh viên', 'tuyển sinh', 'thi tốt nghiệp', 'điểm thi', 'trường học', 'học phí', 'giáo viên', 'đại học'),
    'Xe': ('ô tô', 'xe máy', 'xe điện', 'mẫu xe', 'hãng xe', 'đánh giá xe', 'đăng kiểm', 'xe mới', 'xe hơi'),
    'Bất động sản': ('bất động sản', 'nhà đất', 'chung cư', 'giá nhà', 'thị trường nhà đất', 'mở bán dự án', 'quy hoạch khu đô thị', 'sổ đỏ', 'sổ hồng', 'giá căn hộ'),
    'Kinh doanh': ('doanh nghiệp', 'cổ phiếu', 'chứng khoán', 'lợi nhuận', 'doanh thu', 'ngân hàng', 'lãi suất', 'xuất khẩu', 'nhập khẩu', 'thương mại', 'tỷ phú', 'tỉ phú', 'đầu tư', 'giá vàng', 'tỷ giá', 'tỉ giá', 'giá xăng'),
    'Số hóa': ('trí tuệ nhân tạo', 'công nghệ', 'phần mềm', 'điện thoại', 'smartphone', 'nvidia', 'chip', 'bán dẫn', 'internet', 'mạng xã hội', 'ứng dụng', 'dữ liệu', 'an ninh mạng', 'máy tính', 'robot', 'chatgpt'),
    'Khoa học': ('nghiên cứu khoa học', 'phát hiện khoa học', 'thí nghiệm', 'nhà khoa học', 'vũ trụ', 'thiên văn', 'sinh học', 'hóa học', 'vật lý', 'khoa học'),
    'Du lịch': ('du lịch', 'du khách', 'khách du lịch', 'điểm đến', 'tour du lịch', 'lữ hành', 'resort', 'khu nghỉ dưỡng', 'vé máy bay', 'khách sạn'),
    'Văn hóa - Giải trí': ('hoa hậu', 'ca sĩ', 'diễn viên', 'phim', 'âm nhạc', 'show diễn', 'giải trí', 'nghệ sĩ', 'sân khấu', 'triển lãm', 'lễ hội', 'người mẫu', 'mv ', 'album', 'văn hóa'),
    'Môi trường': ('môi trường', 'biến đổi khí hậu', 'ô nhiễm', 'nắng nóng', 'mưa lớn', 'bão số', 'lũ lụt', 'ngập lụt', 'sạt lở', 'thời tiết', 'không khí', 'hạn hán', 'động đất', 'thiên tai'),
    'Ý kiến - Góc nhìn': ('góc nhìn', 'ý kiến', 'bạn đọc', 'tôi cho rằng', 'theo chuyên gia', 'quan điểm', 'bình luận'),
    'Đời sống': ('đời sống', 'người dân', 'gia đình', 'hôn nhân', 'trẻ em', 'mẹ và bé', 'cộng đồng', 'việc làm', 'lương', 'an sinh', 'phát hiện con gái', 'phong tục', 'người dao', 'ẩm thực', 'món ăn'),
    'Thời sự': ('chính phủ', 'quốc hội', 'thủ tướng', 'bộ trưởng', 'ủy ban nhân dân', 'ubnd', 'hạ tầng', 'giao thông', 'cao tốc', 'đường sắt', 'đình công', 'chính sách', 'ngân sách', 'tỉnh ủy', 'xây dựng'),
}

def normalize_field_category(value: str | None, title: str = '', summary: str = '') -> str:
    key = _fold(value or '')
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    return classify_field_category(f'{title} {summary}')

def classify_field_category(text: str) -> str:
    folded = _fold(text)
    if not folded:
        return 'Thời sự'
    scored = []
    for index, category in enumerate(FIELD_CATEGORIES):
        terms = FIELD_KEYWORDS.get(category, ())
        hits = sum(1 for term in terms if _fold(term) in folded)
        if hits:
            # One specific key phrase is enough; tie-breaking follows editorial specificity order.
            specificity = max(len(_fold(term)) for term in terms if _fold(term) in folded)
            scored.append((hits, specificity, -index, category))
    return max(scored)[3] if scored else 'Thời sự'

def is_broad_field_topic(name: str | None) -> bool:
    """Reject field/category labels when a model tries to create them as event topics."""
    key = _fold(name or '')
    if not key:
        return False
    broad = set(_CATEGORY_ALIASES) | {_fold(x) for x in LEGACY_BROAD_TOPIC_LABELS} | {
        'xa hoi va doi song', 'xa hoi doi song', 'kinh te va doanh nghiep', 'bat dong san va ha tang',
        'chinh tri va chinh sach', 'y te suc khoe va khoa hoc', 'van hoa va giai tri',
        'van hoa du lich', 'tin tuc trong ngay', 'thoi su va chinh sach', 'cong nghe va doi song',
        'kinh doanh va thi truong', 'van hoa va du lich', 'thoi su', 'the gioi', 'kinh doanh',
        'bat dong san', 'phap luat', 'giao duc', 'suc khoe', 'doi song', 'du lich', 'khoa hoc',
        'so hoa', 'xe', 'the thao', 'van hoa giai tri', 'moi truong', 'y kien goc nhin',
    }
    return key in broad

def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

def field_category_sql_expression(alias: str = 'a') -> str:
    """SQL fallback for stored rows lacking a category; stored GenAI labels win."""
    text = f"lower(coalesce({alias}.title,'') || ' ' || coalesce({alias}.summary,'') || ' ' || left(coalesce({alias}.content_text,''),4000))"
    allowed = ','.join(_sql_literal(x) for x in FIELD_CATEGORIES)
    pieces = [f"WHEN {alias}.ai_classification->>'field_category' IN ({allowed}) THEN {alias}.ai_classification->>'field_category'"]
    for category, terms in FIELD_KEYWORDS.items():
        predicates = [f"position(lower({_sql_literal(term)}) in {text}) > 0" for term in terms]
        pieces.append(f"WHEN ({' OR '.join(predicates)}) THEN {_sql_literal(category)}")
    pieces.append("ELSE 'Thời sự'")
    return 'CASE ' + ' '.join(pieces) + ' END'
