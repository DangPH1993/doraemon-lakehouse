"""Deterministic eligibility and source-grounding rules for followable Radar entities."""
from __future__ import annotations
import json
import logging
import re
import unicodedata
from pathlib import Path
from .processing import normalize_space, norm_name

log = logging.getLogger('mini-lakehouse.entity-rules')
try:
    _ADMIN_LOCATIONS = set(json.loads(Path(__file__).with_name('admin_location_names.json').read_text(encoding='utf-8')))
except Exception as exc:
    log.warning('Could not load administrative-location lookup: %s', exc)
    _ADMIN_LOCATIONS = set()

_GENERIC_ENTITIES = {
    'nguoi dan', 'nguoi lao dong', 'nguoi tieu dung', 'nguoi mua', 'nguoi ban',
    'siêu thị', 'sieu thi', 'cửa hàng', 'cua hang', 'nhà hàng', 'nha hang',
    'đường', 'phố', 'cao tốc', 'cầu', 'cầu vượt', 'tàu', 'máy bay', 'xe buýt',
    'hồ chứa', 'đập', 'nhà hát', 'cụm công nghiệp', 'khu kinh tế', 'bến xe khách',
    'bộ', 'sở', 'ủy ban', 'uỷ ban', 'tòa án', 'toà án', 'học viện', 'viện kiểm sát',
    'liên đoàn', 'hiệp hội', 'quỹ', 'đảng', 'tổng công ty', 'trung tâm y tế',
    'trung tâm hội nghị', 'vụ án', 'vụ kiện', 'giải đấu', 'giải vô địch', 'hội nghị',
    'chiến dịch', 'cuộc thi', 'trận đấu', 'đại hội', 'chuyến công du', 'phiên tòa', 'vụ cháy', 'vụ tai nạn',
    'bệnh viện', 'benh vien', 'trường học', 'truong hoc', 'trường đại học', 'truong dai hoc',
    'công ty', 'cong ty', 'doanh nghiệp', 'doanh nghiep', 'ngân hàng', 'ngan hang',
    'chùa', 'đền', 'miếu', 'di tích', 'danh lam thắng cảnh', 'sân vận động', 'san van dong',
    'sân bay', 'san bay', 'cảng', 'công viên', 'cong vien', 'nhà máy', 'nha may',
    'khu công nghiệp', 'khu đô thị', 'khu dân cư', 'thành phố', 'tỉnh', 'quốc gia',
    'đất nước', 'địa phương', 'khu vực', 'vùng miền', 'city', 'country', 'province', 'state',
    'region', 'district', 'hospital', 'supermarket', 'shop', 'store', 'company', 'bank',
    'school', 'university', 'temple', 'pagoda', 'stadium', 'airport', 'port', 'factory',
    'industrial park', 'residents', 'people', 'consumers', 'government',
}
_ADMIN_PREFIXES = (
    'thanh pho ', 'tp ', 'tinh ', 'city of ', 'province of ', 'state of ',
    'municipality of ', 'region of ', 'quoc gia ', 'country of ',
)
_GENERIC_FOLDED = {unicodedata.normalize('NFKD', n).encode('ascii', 'ignore').decode('ascii').casefold() for n in _GENERIC_ENTITIES}
_ADMIN_SUFFIXES = (
    ' province', ' state', ' city', ' municipality', ' prefecture', ' region',
    ' county', ' district', ' metropolitan city', ' metropolitan municipality',
    ' autonomous region', ' territory',
)
_ADMIN_LOCATION_ALIASES = {
    'tphcm', 'tp hcm', 'tp. hcm', 'tp ho chi minh', 'tp. ho chi minh',
    'hcm', 'hcm city', 'saigon', 'sai gon', 'sai gon city',
    'ha noi city', 'danang', 'da nang city', 'ho chi minh city',
}
_BROAD_REGIONS = {
    'nam bo', 'dong nam bo', 'tay nam bo', 'bac bo', 'tay bac bo', 'dong bac bo',
    'mien bac', 'mien trung', 'mien nam', 'tay nguyen', 'dong bang song hong',
    'dong bang song cuu long', 'red river delta', 'mekong delta', 'central vietnam',
    'northern vietnam', 'southern vietnam', 'southeast asia',
}
_NON_ENTITY_PHRASES = {
    'anh ve', 'hoa sua', 'nguoi dao', 'nguoi kinh', 'nguoi tay', 'nguoi thai',
    'nguoi nung', 'nguoi mong', 'nguoi h mong', 'nguoi hmong', 'nguoi dan',
    'nguoi dan ong', 'nguoi phu nu', 'nguoi tre', 'nguoi cao tuoi',
    'pho tphcm', 'pho ho chi minh', 'mot sieu thi', 'mot benh vien',
    'mot cay cau', 'một siêu thị', 'một bệnh viện', 'một cây cầu',
}
def _fold(value: str) -> str:
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode('ascii')
    value = value.casefold()
    value = re.sub(r'[^\w\s-]', ' ', value, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', value).strip().strip('-')


_ENGLISH_MONTHS = (
    'jan(?:uary)?', 'feb(?:ruary)?', 'mar(?:ch)?', 'apr(?:il)?', 'may', 'jun(?:e)?',
    'jul(?:y)?', 'aug(?:ust)?', 'sep(?:t(?:ember)?)?', 'oct(?:ober)?',
    'nov(?:ember)?', 'dec(?:ember)?',
)
_ENGLISH_MONTH_PATTERN = '(?:' + '|'.join(_ENGLISH_MONTHS) + ')'


def is_numeric_or_date_entity(name: str) -> bool:
    """Reject standalone numbers and date-like strings, without rejecting named IDs.

    Examples rejected: ``100``, ``16.10``, ``2-11``, ``16/10/2026``,
    ``ngày 16 tháng 10`` and ``October 16, 2026``. Named products/events such as
    ``3M``, ``F-16``, ``7-Eleven`` and ``World Cup 2026`` remain eligible because
    their surface form contains meaningful letters.
    """
    raw = normalize_space(str(name or '')).strip(' .,:;')
    if not raw or not re.search(r'\d', raw):
        return False

    # Numeric values and numeric-only dates with punctuation (including locale-specific
    # decimal/date separators) are not named entities.
    compact = re.sub(r'[\s\W_]+', '', raw, flags=re.UNICODE)
    if compact.isdigit():
        return True

    folded = _fold(raw)
    # Vietnamese date fragments commonly found in news headlines and summaries.
    if re.fullmatch(
        r'(?:ngay\s*)?\d{1,2}(?:\s*[-/.]?\s*(?:thang\s*)?\d{1,2})'
        r'(?:\s*[-/.]?\s*(?:nam\s*)?\d{2,4})?',
        folded,
    ):
        return True
    if re.fullmatch(r'ngay\s+\d{1,2}', folded):
        return True
    if re.fullmatch(r'thang\s+\d{1,2}(?:\s+nam\s+\d{4})?', folded):
        return True

    # English dates, e.g. ``Oct 16``, ``October 16, 2026`` or ``16 October 2026``.
    month_first = rf'{_ENGLISH_MONTH_PATTERN}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?'
    day_first = rf'\d{{1,2}}(?:st|nd|rd|th)?\s+{_ENGLISH_MONTH_PATTERN}(?:\s+\d{{4}})?'
    return bool(re.fullmatch(month_first, folded) or re.fullmatch(day_first, folded))


_NON_ENTITY_FOLDED = {_fold(x) for x in _NON_ENTITY_PHRASES}
_REGION_FOLDED = {_fold(x) for x in _BROAD_REGIONS}


def is_administrative_location(name: str) -> bool:
    """Return True for countries/provinces/cities, not specifically named landmarks."""
    normalized = norm_name(normalize_space(name))
    folded = _fold(normalized)
    if not folded:
        return False
    candidates = {folded}
    for _ in range(2):
        for candidate in list(candidates):
            for prefix in _ADMIN_PREFIXES:
                if candidate.startswith(prefix):
                    candidates.add(candidate[len(prefix):].strip())
            for suffix in _ADMIN_SUFFIXES:
                if candidate.endswith(suffix):
                    candidates.add(candidate[:-len(suffix)].strip())
    return any(candidate in _ADMIN_LOCATIONS or candidate in _ADMIN_LOCATION_ALIASES
               for candidate in candidates if candidate)


def should_track_entity(name: str, entity_type: str = 'UNKNOWN') -> bool:
    """Reject vague nouns, broad regions and administrative geography; keep named objects."""
    name = normalize_space(str(name or ''))[:180]
    normalized = norm_name(name)
    folded = _fold(normalized)
    if len(normalized) < 2 or len(normalized) > 180:
        return False
    if is_numeric_or_date_entity(name):
        return False
    if folded in _GENERIC_FOLDED or folded in _NON_ENTITY_FOLDED or folded in _REGION_FOLDED:
        return False
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    if typ not in {'PERSON', 'PRODUCT'} and is_administrative_location(name):
        return False
    try:
        from .entity_names import named_head_and_tail
        typed_name = named_head_and_tail(name)
        # An administrative name used as the whole tail of a road/street label is
        # usually a bad NER fragment ("Đường TPHCM"), but the same tail can be a valid
        # named landmark ("Vịnh Hạ Long"). Only suppress linear transport labels here.
        linear_heads = {norm_name(x) for x in ('đường', 'phố', 'đại lộ', 'tuyến đường', 'quốc lộ', 'tỉnh lộ',
                                                'đường tỉnh', 'đường cao tốc', 'đường vành đai', 'tuyến cao tốc')}
        if typ not in {'PERSON', 'PRODUCT'} and typed_name and norm_name(typed_name[0]) in linear_heads and is_administrative_location(typed_name[1]):
            return False
    except Exception:
        pass
    # Ethnic-population mentions ("người Dao", "người Kinh") in prose are context,
    # not a specific followable individual/object.
    if re.match(r'^nguoi\s+(dao|kinh|tay|thai|nung|mong|hmong|h mong|muong|cham|hoa|khmer)$', normalized, re.I):
        return False
    return True


def is_grounded_entity(name: str, aliases, article_text: str, entity_type: str = 'UNKNOWN') -> bool:
    """Require evidence in this article and a proper-name-like surface form.

    Typed objects such as ``Đường Võ Nguyên Giáp`` are accepted when their full normalized
    name appears in the text. Untyped model candidates must be source-grounded; multi-word
    names need multiple capitalized tokens (or a strong acronym/number) to avoid promoting
    fragments such as ``anh về`` or ``người Dao`` into entities.
    """
    name = normalize_space(str(name or ''))[:180]
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    if not should_track_entity(name, typ):
        return False
    source = normalize_space(str(article_text or '')[:20000])
    if not source:
        return False
    source_norm = norm_name(source)
    try:
        from .entity_names import named_head_and_tail
        structured = named_head_and_tail(name)
    except Exception:
        structured = None
    candidates = [name]
    if isinstance(aliases, (list, tuple, set)):
        candidates.extend(str(x)[:180] for x in aliases if str(x).strip())
    seen = set()
    for candidate in candidates:
        candidate = normalize_space(candidate)
        cn = norm_name(candidate)
        if not cn or cn in seen or cn not in source_norm:
            continue
        seen.add(cn)
        if structured:
            return True
        match = re.search(re.escape(candidate), source, flags=re.IGNORECASE | re.UNICODE)
        if not match:
            continue
        if typ == 'PRODUCT' and is_administrative_location(candidate):
            local = source[max(0, match.start() - 100):min(len(source), match.end() + 100)].casefold()
            product_context = ('hyundai', 'toyota', 'ford', 'kia', 'mazda', 'vinfast', 'mẫu xe', 'hãng xe', 'xe mới', 'ô tô', 'xe điện', 'suv', 'sedan', 'dòng xe')
            if not any(cue in local for cue in product_context):
                continue
        surface = match.group(0)
        tokens = re.findall(r"[^\W_]+(?:[.'’][^\W_]+)*", surface, flags=re.UNICODE)
        if not tokens:
            continue
        if any((len(t) >= 2 and t.isupper()) or any(ch.isdigit() for ch in t) for t in tokens):
            return True
        capitalized = sum(1 for t in tokens if t[:1].isupper())
        if len(tokens) >= 2:
            if capitalized >= 2:
                return True
            continue
        token = tokens[0]
        if token[:1].isupper() and len(token) >= 3 and typ in {'PERSON', 'ORG', 'PRODUCT', 'EVENT', 'GROUP', 'UNKNOWN'}:
            return True
        if token[:1].isupper() and len(token) >= 3 and typ == 'LOCATION' and match.start() > 0:
            return True
    return False
