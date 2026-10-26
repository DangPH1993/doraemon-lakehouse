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

# These words often become capitalized only because they occur at the start of a
# Vietnamese headline or are fragments of a longer phrase. Alone, they do not identify
# one specific person, organization, event, place or object (e.g. cloud bubbles "Chiến"
# and "Chủ"). Keep the filter exact-token and conservative so full names such as
# "Chiến Thắng", named organizations such as "Bộ Công an", and proper brands survive.
_GENERIC_SINGLE_TOKEN_ENTITIES = {
    'chiến', 'chủ', 'vụ', 'nhà', 'người', 'hàng', 'bộ', 'sở', 'ban', 'công', 'diễn', 'viên', 'mùa', 'thu', 'tập',
    'đội', 'đoàn', 'tuyến', 'giải', 'trận', 'phiên', 'cuộc', 'sự', 'việc', 'tin',
    'sản', 'quan', 'xã', 'huyện', 'tỉnh', 'thành', 'phố', 'quốc', 'gia', 'ngành',
    'địa', 'khu', 'vùng', 'lãnh', 'đạo', 'chính', 'phủ', 'thể', 'thao', 'kinh',
    'tế', 'pháp', 'luật', 'văn', 'hóa', 'đấu', 'cầu', 'xe', 'tàu', 'máy', 'đường',
    'cảng', 'sân', 'trường', 'làng', 'chợ', 'núi', 'sông', 'hồ', 'biển', 'nhân',
    'dân', 'thắng', 'thua', 'mới', 'cũ', 'lớn', 'nhỏ', 'cao', 'thấp', 'đẹp', 'xấu',
}
_GENERIC_SINGLE_TOKEN_FOLDED = {_fold(x) for x in _GENERIC_SINGLE_TOKEN_ENTITIES}

# Publisher/byline labels are not followable story entities. Keep this list deliberately
# conservative and supplement it with common Vietnamese publisher prefixes.
_MEDIA_SOURCE_NAMES = {
    'vtv', 'vtv vn', 'vtv vietnam', 'vtv go', 'vtv24', 'vtv 24',
    'bao thanh nien', 'thanh nien', 'bao 24h', 'bao 24 h',
    'vnexpress', 'vnexpress net', 'tuoi tre', 'tuoi tre online', 'dan tri',
    'vietnamnet', 'vietnam net', 'lao dong', 'cong an nhan dan',
    'bao nhan dan', 'nhan dan', 'bao chinh phu', 'bao moi', 'kenh 14',
    'zing news', 'zingnews', 'bbc', 'bbc news', 'cnn', 'reuters', 'associated press',
    'ap news', 'bloomberg', 'the guardian', 'the new york times', 'washington post',
    'financial times', 'al jazeera', 'the independent', 'abc news', 'cbs news',
    'fox news', 'sky news', 'nikkei asia', 'the times', 'the telegraph',
}
_MEDIA_SOURCE_FOLDED = {_fold(x) for x in _MEDIA_SOURCE_NAMES}
_MEDIA_SOURCE_TYPES = {'SOURCE', 'NEWS_SOURCE', 'MEDIA_SOURCE', 'MEDIA_OUTLET', 'NEWS_OUTLET', 'PUBLISHER', 'PUBLICATION'}


def is_media_source_entity(name: str, entity_type: str = 'UNKNOWN') -> bool:
    """Return True for publisher/source labels, which are not story-follow entities."""
    raw = normalize_space(str(name or '')).strip()
    folded = _fold(raw)
    if not folded:
        return False
    if str(entity_type or 'UNKNOWN').upper().strip() in _MEDIA_SOURCE_TYPES:
        return True
    if folded in _MEDIA_SOURCE_FOLDED:
        return True
    # Examples include "Báo Thanh Niên", "Báo 24h" and similar news outlets.
    if re.match(r'^(?:Báo|Kênh)\s+\S', raw, flags=re.IGNORECASE):
        return True
    # A bare publisher domain such as VTV.vn or VnExpress.net is a source label.
    if re.fullmatch(r'(?:www\.)?[a-z0-9-]+\.(?:vn|com|net|org|tv|news|io)', raw, flags=re.IGNORECASE):
        return True
    return False




# Person aliases need a stricter policy than generic entity aliases. In Vietnamese
# news, given-name tokens such as Lan, Nga, Anh, Mỹ, Nam, Hùng, etc. routinely appear
# inside unrelated people, organizations and place names. A single shared token is not
# sufficient evidence that two mentions refer to the same person.
_PERSON_ROLE_PREFIXES = tuple(sorted((
    'former president of the united states', 'president of the united states',
    'cựu tổng thống mỹ', 'tổng thống mỹ', 'cựu tổng thống', 'prime minister of',
    'vice president', 'secretary of state', 'prime minister', 'thủ tướng',
    'tổng thống', 'chủ tịch', 'bộ trưởng', 'phó tổng thống', 'former president',
    'president', 'secretary', 'senator', 'governor', 'chancellor', 'chairman',
    'chairwoman', 'king', 'queen', 'prince', 'princess', 'premier', 'minister',
    'tiktoker', 'ông', 'bà', 'ngài', 'mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'sir',
), key=len, reverse=True))
_SAFE_SHORT_PERSON_ALIASES = {
    # Explicit product rule already used by the Radar: bare Trump refers to Donald Trump.
    'donald trump': {'trump'},
}
# Folded tokens frequently present in Vietnamese full names. This also catches unaccented
# RSS text such as "Truong My Lan" and "Nguyen Thi Lan".
_VIETNAMESE_NAME_TOKENS = set('''
    nguyen tran le pham hoang huynh phan vu vo dang bui do ho ngo duong ly trinh dinh
    truong truong thi van my lan nga anh nam hung hùng son linh thanh huong mai tung minh
    phuc thao tuan khanh thuy trang van hai dung quang bao viet duc ngoc ha tam hieu yen
    loan diep hien bich thao thien nga quynh kien khoa khue kieu oanh phuong thuan toan
    nghi nhan nhat kha chau lam loc phuoc quy sang tuyen vy
'''.split())


def _strip_person_role_prefix(value: str) -> str:
    folded = _fold(normalize_space(str(value or '')))
    for _ in range(3):
        changed = folded
        for prefix in _PERSON_ROLE_PREFIXES:
            prefix_folded = _fold(prefix)
            if folded == prefix_folded or folded.startswith(prefix_folded + ' '):
                changed = folded[len(prefix_folded):].strip()
                break
        if changed == folded:
            break
        folded = changed
    return folded


def _person_identity_key(value: str) -> str:
    """Normalize a person surface, tolerating role prefixes and middle initials only."""
    folded = _strip_person_role_prefix(value)
    tokens = folded.split()
    # Donald J. Trump and Donald Trump are the same full-name form. Do not collapse
    # Vietnamese middle names (e.g. Trương Mỹ Lan), which are full name components.
    if len(tokens) >= 3 and all(len(token) == 1 for token in tokens[1:-1]):
        return f'{tokens[0]} {tokens[-1]}'
    return folded


def is_safe_person_alias(canonical_name: str, alias: str) -> bool:
    """Whether an alias is strong enough to identify this exact person.

    Role-prefixed and middle-initial variants of the full name are safe. Short final-name
    aliases remain available for non-Vietnamese-style names (e.g. Ronaldo, Trump), but are
    blocked for Vietnamese names, including unaccented forms, because given-name tokens
    often occur in unrelated names/places. Multi-word aliases must match the full identity.
    """
    canonical_key = _person_identity_key(canonical_name)
    alias_key = _person_identity_key(alias)
    if not canonical_key or not alias_key:
        return False
    if alias_key == canonical_key:
        return True
    safe_short = _SAFE_SHORT_PERSON_ALIASES.get(canonical_key, set())
    if alias_key in safe_short:
        return True
    alias_tokens = alias_key.split()
    canonical_tokens = canonical_key.split()
    if len(alias_tokens) != 1 or len(canonical_tokens) < 2:
        return False
    raw_canonical = str(canonical_name or '')
    # Any diacritic is treated conservatively, and unaccented Vietnamese surnames/given
    # names are caught by the token list above.
    has_diacritics = unicodedata.normalize('NFKD', raw_canonical) != raw_canonical
    vietnamese_style = has_diacritics or bool(set(canonical_tokens) & _VIETNAMESE_NAME_TOKENS)
    if vietnamese_style:
        return False
    # Preserve traditional last-name aliases for non-Vietnamese names, but never let a
    # first/middle token become the identity key by accident.
    return alias_tokens[0] == canonical_tokens[-1]


# A small set of culturally established one-token aliases for non-person entities.
# Every other multi-token entity must not be resolved or grounded through an ordinary
# one-word tail such as "Diễn" from "Cầu Diễn".
_SAFE_SHORT_ENTITY_ALIASES = {
    'sevilla fc': {'sevilla'},
    'valencia cf': {'valencia'},
}


def is_safe_entity_alias(canonical_name: str, alias: str, entity_type: str = 'UNKNOWN') -> bool:
    """Return whether an alias is distinctive enough to identify the canonical entity.

    Exact canonical forms and multi-token variants with at least two shared identity tokens
    are safe. A different one-token alias for a multi-token canonical name is rejected unless
    it is a curated alias or a distinctive acronym/CamelCase token already inside the canonical
    name. This prevents aliases like ``Diễn`` from attaching every article beginning with
    ``Diễn viên`` or ``Diễn đàn`` to the location entity ``Cầu Diễn``.
    """
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    if typ == 'PERSON':
        return is_safe_person_alias(canonical_name, alias)

    canonical_raw = normalize_space(str(canonical_name or '')).strip(' ,.;:-')
    alias_raw = normalize_space(str(alias or '')).strip(' ,.;:-')
    canonical_key = _fold(canonical_raw)
    alias_key = _fold(alias_raw)
    if not canonical_key or not alias_key:
        return False
    if alias_key == canonical_key:
        return True

    canonical_tokens = canonical_key.split()
    alias_tokens = alias_key.split()
    if len(alias_tokens) == 1 and len(canonical_tokens) > 1:
        if alias_key in _SAFE_SHORT_ENTITY_ALIASES.get(canonical_key, set()):
            return True
        # Allow acronyms or unmistakable brands only when that token is present in the
        # canonical name itself (e.g. "Tập đoàn OpenAI" -> "OpenAI", not "Cầu Diễn" -> "Diễn").
        token_is_acronym = alias_raw.isupper() and len(re.sub(r'[^A-Za-z0-9]', '', alias_raw)) >= 3
        token_is_camel_brand = (any(c.isupper() for c in alias_raw[1:])
                                and any(c.islower() for c in alias_raw)
                                and any(c.islower() for c in alias_raw[1:]))
        token_has_digit = any(c.isdigit() for c in alias_raw)
        return (alias_key in canonical_tokens
                and (token_is_acronym or token_is_camel_brand or token_has_digit))

    if len(alias_tokens) >= 2:
        # Require at least two identity-token overlaps. A single shared descriptor such as
        # "Cầu" is not enough to claim that "Cầu Rồng" and "Cầu Diễn" are the same entity.
        return len(set(alias_tokens) & set(canonical_tokens)) >= 2

    # A distinct one-token alias cannot safely identify a one-token canonical either.
    return False


def _contains_folded_phrase(source_folded: str, phrase: str) -> bool:
    """Match a full token-bounded phrase after accent/punctuation normalization."""
    phrase_folded = _fold(phrase)
    if not source_folded or not phrase_folded:
        return False
    return re.search(r'(?<![a-z0-9])' + re.escape(phrase_folded) + r'(?![a-z0-9])', source_folded) is not None


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
    if is_media_source_entity(name, entity_type):
        return False
    if is_numeric_or_date_entity(name):
        return False
    if folded in _GENERIC_FOLDED or folded in _NON_ENTITY_FOLDED or folded in _REGION_FOLDED:
        return False
    # Reject ambiguous standalone words regardless of the model-assigned type. Entity
    # type labels are often noisy, so classifying the generic word as ORG/EVENT must not
    # turn a fragment into a followable entity.
    if len(folded.split()) == 1 and folded in _GENERIC_SINGLE_TOKEN_FOLDED:
        return False
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    # A whole administrative country/place name must not become a PERSON merely because
    # an English spaCy/GenAI classifier assigned the wrong type (e.g. 'Ba Lan').
    if typ == 'PERSON' and is_administrative_location(name):
        return False
    if typ == 'PERSON' and len(folded.split()) == 1 and folded in _VIETNAMESE_NAME_TOKENS:
        # A single common Vietnamese given/family-name token (e.g. Lan, Nam, Hùng) is
        # too ambiguous to stand alone as a followable person entity.
        return False
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
    """Require exact, token-bounded source evidence; PERSON aliases are conservative.

    For people, the canonical full name (or a vetted full-name variant) must occur in
    this article's title/summary. A one-word alias is accepted only when explicitly
    allowlisted (currently Donald Trump -> Trump). Other entity types retain the existing
    proper-name checks after exact phrase matching.
    """
    name = normalize_space(str(name or ''))[:180]
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    if not should_track_entity(name, typ):
        return False
    source = normalize_space(str(article_text or '')[:20000])
    if not source:
        return False
    source_folded = _fold(source)
    try:
        from .entity_names import named_head_and_tail
        structured = named_head_and_tail(name)
    except Exception:
        structured = None

    candidates = [name]
    if isinstance(aliases, (list, tuple, set)):
        for value in aliases:
            candidate_alias = normalize_space(str(value or ''))[:180]
            if not candidate_alias:
                continue
            if not is_safe_entity_alias(name, candidate_alias, typ):
                continue
            candidates.append(candidate_alias)

    seen = set()
    for candidate in candidates:
        candidate = normalize_space(candidate)
        cn = _fold(candidate)
        if not cn or cn in seen or not _contains_folded_phrase(source_folded, candidate):
            continue
        if typ == 'PERSON':
            # Defense in depth: even when callers pass stale aliases, their names must be
            # identity-compatible with the canonical PERSON and appear as a full phrase.
            if candidate != name and not is_safe_person_alias(name, candidate):
                continue
            return True
        seen.add(cn)
        if structured:
            return True
        match = re.search(r'(?<!\w)' + re.escape(candidate) + r'(?!\w)', source, flags=re.IGNORECASE | re.UNICODE)
        if not match:
            # The normalized phrase was present (e.g. accents differ); non-person type
            # grounding can still rely on explicit phrase evidence without case heuristics.
            return True if typ in {'ORG', 'LOCATION', 'PRODUCT', 'EVENT', 'GROUP'} else False
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
        if token[:1].isupper() and len(token) >= 3 and typ in {'ORG', 'PRODUCT', 'EVENT', 'GROUP', 'UNKNOWN'}:
            return True
        if token[:1].isupper() and len(token) >= 3 and typ == 'LOCATION' and match.start() > 0:
            return True
    return False

