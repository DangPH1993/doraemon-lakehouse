"""Conservative recovery of named-entity surface forms from article title/summary.

GenAI remains the primary classifier. This deterministic supplement recovers obvious
proper-name spans (brands, products, organizations, projects and people) missed by the
English spaCy fallback, while the same eligibility/grounding gates reject generic nouns,
administrative places and broad editorial fields.
"""
from __future__ import annotations

import re
from typing import Any

from .processing import normalize_space, norm_name
from .entity_rules import should_track_entity, is_grounded_entity
from .field_categories import is_broad_field_topic
from .named_infrastructure import extract_named_infrastructure_entities
from .named_people import extract_named_person_entities

_TOKEN = re.compile(r"[^\W_]+(?:[.'’\-][^\W_]+)*", re.UNICODE)
_CONNECTORS = {
    'of', 'the', 'and', 'de', 'del', 'la', 'le', 'van', 'von', 'da', 'di', 'al', 'el',
    'bin', 'ibn', '&',
}
_ROLE_PREFIXES = {
    'ceo', 'cfo', 'cto', 'coo', 'president', 'prime minister', 'minister', 'chairman',
    'chairwoman', 'chairperson', 'senator', 'governor', 'director', 'chief', 'tổng thống',
    'thủ tướng', 'chủ tịch', 'bộ trưởng', 'giám đốc', 'đại sứ', 'hoa hậu', 'ca sĩ',
    'diễn viên', 'tiktoker', 'bác sĩ', 'luật sư', 'tỷ phú', 'tỉ phú', 'nhà sáng lập',
}
_PERSON_CUES = {
    'hoa hậu', 'á hậu', 'người mẫu', 'diễn viên', 'ca sĩ', 'rapper', 'tiktoker', 'nghệ sĩ',
    'tiền đạo', 'tiền vệ', 'hậu vệ', 'thủ môn', 'cầu thủ', 'vận động viên',
    'đạo diễn', 'nhà văn', 'nhà báo', 'tỷ phú', 'tỉ phú', 'tổng thống', 'cựu tổng thống',
    'thủ tướng', 'phó thủ tướng', 'bộ trưởng', 'chủ tịch', 'phó chủ tịch', 'giám đốc',
    'đại sứ', 'luật sư', 'bác sĩ', 'tiến sĩ', 'giáo sư', 'vận động viên', 'huấn luyện viên',
    'cựu cầu thủ', 'cầu thủ', 'hoa khôi', 'nhà sáng lập', 'chuyên gia', 'ông', 'bà', 'ngài',
}
_SINGLE_NAME_CUES = {
    'tàu', 'tên lửa', 'vệ tinh', 'mẫu xe', 'xe điện', 'xe', 'mẫu', 'chip', 'hãng',
    'thương hiệu', 'sản phẩm', 'dòng xe', 'điện thoại', 'mô hình', 'dự án', 'robot',
    'máy bay', 'tàu vũ trụ', 'tên lửa đẩy', 'phần mềm', 'hệ điều hành', 'tương tự',
}
_ORG_CUES = {'công ty', 'tập đoàn', 'hãng', 'nhà sản xuất', 'doanh nghiệp', 'nhà phát triển', 'startup', 'nhà cung cấp', 'ceo', 'chipmaker', 'nhà sản xuất chip', 'dịch vụ', 'nhãn hiệu'}
_ORG_SUFFIXES = {'aerospace', 'technologies', 'technology', 'systems', 'semiconductor', 'semiconductors', 'corporation', 'company', 'inc', 'ltd', 'group', 'holdings', 'labs', 'laboratories', 'university'}
_NON_ENTITY_ACRONYMS = {'ram', 'cpu', 'gpu', 'ai', 'gdp', 'usd', 'vnd', 'clb', 'hlv'}
# A city name may also denote its football club. Only canonicalize these forms when
# the surrounding sentence clearly describes a football match/team, not a location.
_SPORTS_ORG_CANONICALS = {'sevilla': 'Sevilla FC', 'valencia': 'Valencia CF'}

# Common headline words are capitalized by sentence position but are not proper names.
# Keep this list deliberately focused on ordinary Vietnamese/English editorial vocabulary;
# candidate entity identity is subsequently checked against article context by GenAI.
_COMMON_SINGLE_WORDS = set("""
    bắt bị được đang sẽ có vì khi sau trước hơn nhiều một các người người dân xe hàng giá
    theo từ tại phát tiếp đề cảnh những vẫn lần trong ngoài giữa dưới trên của với cho là và
    nhưng không chưa đã đang từng vừa khiến giúp thắng thua đậm cứu lần lượt hôm nay ngày
    tiền đạo hậu vệ thủ môn cầu thủ huấn luyện viên đội tuyển đội bóng câu lạc bộ quốc gia
    bóng đá bóng bàn trận đấu vòng bàn thắng thất bại thành công tăng giảm giảm mạnh tăng mạnh
    nói cho biết khẳng định cảnh báo công bố công an bộ ngành cơ quan nhà nước chính phủ
    doanh nghiệp công ty hãng ngân hàng thị trường cổ phiếu giá vàng chứng khoán kinh tế
    thế giới thời sự thể thao trong nước ngoài nước việt nam thành phố tỉnh quốc gia
    diễn viên diễn đàn diễn biến diễn ra mùa thu mùa đông mùa xuân mùa hè tập đoàn tập trung
    this that these those when where why what how after before during amid says said report
    reports wins win defeat beats helps helped rescues faces amid over under new latest
    the a an and or of in on at to for from with by as its their his her
""".split())

# Well-known named competitions whose Vietnamese spelling contains lower-case words
# or connector words that a capitalization-only recognizer would split or miss.
_SPECIAL_NAMED_PHRASES = (
    (re.compile(r'(?<!\w)la\s+liga(?!\w)', re.IGNORECASE | re.UNICODE), 'La Liga', 'EVENT'),
    (re.compile(r'(?<!\w)ngoại\s+hạng\s+anh(?!\w)', re.IGNORECASE | re.UNICODE), 'Ngoại hạng Anh', 'EVENT'),
    (re.compile(r'(?<!\w)premier\s+league(?!\w)', re.IGNORECASE | re.UNICODE), 'Premier League', 'EVENT'),
    (re.compile(r'(?<!\w)champions\s+league(?!\w)', re.IGNORECASE | re.UNICODE), 'Champions League', 'EVENT'),
    (re.compile(r'(?<!\w)europa\s+league(?!\w)', re.IGNORECASE | re.UNICODE), 'Europa League', 'EVENT'),
    (re.compile(r'(?<!\w)conference\s+league(?!\w)', re.IGNORECASE | re.UNICODE), 'Conference League', 'EVENT'),
    (re.compile(r'(?<!\w)world\s+cup(?!\w)', re.IGNORECASE | re.UNICODE), 'World Cup', 'EVENT'),
    (re.compile(r'(?<!\w)afc\s+champions\s+league(?!\w)', re.IGNORECASE | re.UNICODE), 'AFC Champions League', 'EVENT'),
    (re.compile(r'(?<!\w)copa\s+del\s+rey(?!\w)', re.IGNORECASE | re.UNICODE), 'Copa del Rey', 'EVENT'),
)
_PRODUCT_CUES = {'tàu', 'tên lửa', 'vệ tinh', 'mẫu xe', 'xe điện', 'xe', 'mẫu', 'chip', 'sản phẩm', 'dòng xe', 'điện thoại', 'mô hình', 'robot', 'máy bay', 'tàu vũ trụ', 'tên lửa đẩy', 'phần mềm', 'hệ điều hành'}
_AUTOMOTIVE_CONTEXT_CUES = ('hyundai', 'toyota', 'ford', 'vinfast', 'kia', 'mazda', 'mẫu xe', 'hãng xe', 'xe mới', 'ô tô', 'xe điện', 'SUV', 'sedan', 'ra mắt')
# Tokens often capitalized as the first word of a Vietnamese headline but not proper names.
_HEADLINE_START_WORDS = {
    'bắt', 'bị', 'được', 'đang', 'sẽ', 'có', 'vì', 'khi', 'sau', 'trong', 'trước',
    'hơn', 'nhiều', 'một', 'các', 'người', 'xe', 'hàng', 'giá', 'theo', 'từ', 'tại',
    'ông', 'bà', 'nữ', 'nam', 'phát', 'tiếp', 'đề', 'cảnh', 'những', 'vẫn', 'lần',
}


def _proper_token(token: str) -> bool:
    if not token:
        return False
    letters = [c for c in token if c.isalpha()]
    if not letters:
        # Pure scores, round numbers and dates (4-1, 7, 2026) must not be
        # concatenated into neighbouring names such as 'Sevilla 4-1' or '7 Ngoại'.
        # Mixed alpha-numeric identifiers (F-16, GPT-5, 3M) remain valid.
        return False
    if len(letters) >= 3 and token.isupper():
        return True  # CXMT, NASA, BYD
    if any(c.isdigit() for c in token):
        return True  # Bayon2027, GPT-5
    if any(c.isupper() for c in token[1:]) and any(c.islower() for c in token):
        return True  # OpenAI, SpaceX, VnExpress
    return token[0].isupper() and any(c.islower() for c in token[1:])


def extract_named_proper_noun_entities(text: str) -> list[dict[str, Any]]:
    """Return conservative UNKNOWN candidates for clear proper-name spans.

    Examples: Hyundai Bayon 2027, Santa Fe, Deep Blue Aerospace, CXMT, SpaceX.
    Named type-head phrases such as ``Cao tốc Mỹ Thuận - Cần Thơ`` are handled by
    ``named_infrastructure`` to preserve their complete typed canonical names.
    """
    text = normalize_space(text or '')[:20000]
    if not text:
        return []

    infra = extract_named_infrastructure_entities(text)
    infra_tails = [norm_name(str(item.get('proper_name') or '')) for item in infra]
    infra_full_names = {norm_name(str(item.get('name') or '')) for item in infra}
    tokens = list(_TOKEN.finditer(text))
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    special_spans: list[tuple[int, int]] = []
    for pattern, canonical, entity_type in _SPECIAL_NAMED_PHRASES:
        for match in pattern.finditer(text):
            special_spans.append((match.start(), match.end()))
            key = norm_name(canonical)
            if key not in seen and should_track_entity(canonical, entity_type) and is_grounded_entity(canonical, [canonical], text, entity_type):
                results.append({'name': canonical, 'type': entity_type, 'aliases': [canonical], 'source': 'known_proper_phrase'})
                seen.add(key)
    i = 0
    while i < len(tokens):
        token_match = tokens[i]
        token = token_match.group(0)
        low = token.casefold()
        if any(start <= token_match.start() < end for start, end in special_spans):
            i += 1
            continue
        if not _proper_token(token):
            i += 1
            continue
        start = i
        phrase_tokens = [token]
        j = i + 1
        while j < len(tokens) and len(phrase_tokens) < 7:
            previous_end = tokens[j - 1].end()
            gap = text[previous_end:tokens[j].start()]
            next_token = tokens[j].group(0)
            # A hyphen often separates two adjacent proper names in a headline
            # (e.g. 'Hyundai Bayon 2027 - Santa Fe'); don't merge both entities.
            if not re.fullmatch(r'[\s/&]+', gap):
                break
            if next_token.casefold() in _CONNECTORS:
                phrase_tokens.append(next_token)
                j += 1
                continue
            # Four-digit model years are part of automotive product names only when a
            # multi-token name is already in progress and local text confirms car context.
            # Never absorb score/round numbers such as 4-1 or 7 into nearby entities.
            if re.fullmatch(r'(?:19|20)\d{2}', next_token) and len(phrase_tokens) >= 2:
                year_context = text[max(0, token_match.start() - 80):min(len(text), tokens[j].end() + 80)].casefold()
                if any(cue.casefold() in year_context for cue in _AUTOMOTIVE_CONTEXT_CUES):
                    phrase_tokens.append(next_token)
                    j += 1
                    continue
            if not _proper_token(next_token):
                break
            phrase_tokens.append(next_token)
            j += 1
        # A single title-case token is too risky (it may just be sentence case); accept
        # only unmistakable acronyms/CamelCase/numeric brand tokens or tokens after a role cue.
        has_strong_single = (len(phrase_tokens) == 1 and (
            (token.isupper() and len(token) >= 3)
            or (any(c.isupper() for c in token[1:]) and any(c.islower() for c in token))
            or any(c.isdigit() for c in token)
        ))
        previous_words = [m.group(0).casefold() for m in tokens[max(0, start - 3):start]]
        previous_phrase = ' '.join(previous_words[-3:])
        leading_cue = phrase_tokens[0].casefold() if phrase_tokens and phrase_tokens[0].casefold() in _ROLE_PREFIXES else ''
        role_context = bool(leading_cue) or any(word in _ROLE_PREFIXES for word in previous_words[-2:]) or any(cue in previous_phrase for cue in _ROLE_PREFIXES if ' ' in cue)
        person_context = any(cue in previous_phrase for cue in _PERSON_CUES) or leading_cue in _PERSON_CUES
        cue_context = any(word in _SINGLE_NAME_CUES for word in previous_words[-2:]) or any(cue in previous_phrase for cue in _SINGLE_NAME_CUES if ' ' in cue)
        count_named = sum(1 for part in phrase_tokens if _proper_token(part) and part.casefold() not in _CONNECTORS)
        sentence_prefix = text[:token_match.start()].rstrip()
        sentence_initial = (not sentence_prefix or sentence_prefix[-1] in '.!?…;:\n')
        single_word_candidate = (
            len(phrase_tokens) == 1
            and low not in _CONNECTORS
            and low not in _NON_ENTITY_ACRONYMS
            and low not in _COMMON_SINGLE_WORDS
            and len(re.sub(r'[^A-Za-zÀ-ỹ]', '', token)) >= 3
            and (not sentence_initial or len(re.findall(r'(?<!\w)' + re.escape(token) + r'(?!\w)', text, re.IGNORECASE | re.UNICODE)) >= 2
                 or has_strong_single or role_context or cue_context)
        )
        if count_named < 2 and not has_strong_single and not role_context and not cue_context and not single_word_candidate:
            i += 1
            continue
        # If a role abbreviation was grouped with a named span (e.g. 'CEO Intel'),
        # discard the role token but retain its type cue.
        if len(phrase_tokens) > 1 and phrase_tokens[0].casefold() in _ROLE_PREFIXES:
            leading_cue = phrase_tokens[0].casefold()
            phrase_tokens = phrase_tokens[1:]
        name = normalize_space(' '.join(phrase_tokens)).strip(' .,:;-–—/&')[:180]
        key = norm_name(name)
        if not key or key in seen or len(key) < 3:
            i = max(i + 1, j)
            continue
        # Never emit field labels or a generic headline-leading verb as an entity.
        if is_broad_field_topic(name) or (start == 0 and token.casefold() in _HEADLINE_START_WORDS):
            i = max(i + 1, j)
            continue
        # Avoid duplicating the headless proper-name suffix already represented by a typed
        # infrastructure entity. Its canonical name retains class + proper name, as required.
        if key in infra_full_names or any(tail and (key == tail or f' {key} ' in f' {tail} ' or f' {tail} ' in f' {key} ') for tail in infra_tails):
            i = max(i + 1, j)
            continue
        # Infer type before eligibility filtering: a product called 'Santa Fe' must not
        # be rejected merely because Santa Fe is also an administrative-place name.
        cue_words = set(previous_words[-3:])
        phrase_folded = norm_name(name)
        if phrase_folded in _NON_ENTITY_ACRONYMS:
            i = max(i + 1, j)
            continue
        # Known corporate suffixes are strong type cues even when the noun is not preceded
        # by 'company/group'. Person-role context outranks product/org cues.
        has_org_suffix = bool(set(phrase_folded.split()) & _ORG_SUFFIXES)
        local_context = text[max(0, token_match.start() - 140):min(len(text), tokens[j - 1].end() + 140)].casefold()
        automotive_context = any(cue.casefold() in local_context for cue in _AUTOMOTIVE_CONTEXT_CUES)
        camel_case_brand = (' ' not in name and any(c.isupper() for c in name[1:]) and any(c.islower() for c in name))
        is_product_cue = bool(cue_words & _PRODUCT_CUES) or cue_context or any(ch.isdigit() for ch in name)
        is_org_cue = bool(cue_words & _ORG_CUES) or has_org_suffix or leading_cue in _ORG_CUES
        sports_context = any(cue in local_context for cue in (
            'bóng đá', 'đội khách', 'đội bóng', 'ghi bàn', 'cú đúp', 'vô địch',
            'ngoại hạng', 'la liga', 'premier league', 'vòng ', 'trận đấu', 'cầu thủ',
        ))
        inferred_type = ('PERSON' if person_context else
                         'PRODUCT' if is_product_cue else
                         'ORG' if (is_org_cue or camel_case_brand or (sports_context and len(phrase_tokens) == 1)) else
                         'PRODUCT' if (automotive_context and len(phrase_tokens) >= 2) else 'UNKNOWN')
        source_surface = name
        aliases = [name]
        # Names such as Sevilla are both administrative places and football clubs. In a
        # clearly football-specific context, retain the club as an organization rather than
        # discarding it as a city; keep the exact source surface as a grounded alias.
        if (not should_track_entity(name, inferred_type) and inferred_type == 'ORG' and sports_context):
            canonical_club = _SPORTS_ORG_CANONICALS.get(norm_name(name))
            if canonical_club:
                name = canonical_club
                key = norm_name(name)
                aliases = [canonical_club, source_surface]
        # The same dual-use city/team name may occur more than once in a headline and
        # summary. Keep one canonical candidate and union its grounded surface aliases.
        duplicate = next((old for old in results if norm_name(str(old.get('name') or '')) == key), None)
        if duplicate is not None:
            old_aliases = duplicate.get('aliases') if isinstance(duplicate.get('aliases'), list) else []
            for alias in aliases:
                if all(norm_name(str(existing)) != norm_name(str(alias)) for existing in old_aliases):
                    old_aliases.append(alias)
            duplicate['aliases'] = old_aliases
            seen.add(key)
            i = max(i + 1, j)
            continue
        if not should_track_entity(name, inferred_type):
            i = max(i + 1, j)
            continue
        # Check the captured span against exact source text and named-entity quality rules.
        if is_grounded_entity(name, aliases, text, inferred_type):
            seen.add(key)
            results.append({'name': name, 'type': inferred_type, 'aliases': aliases, 'source': 'proper_noun_recovery'})
        i = max(i + 1, j)

    # When a headline uses a short surname ("Mbappe") and the summary includes the full
    # name ("Kylian Mbappe"), retain one canonical candidate and attach the short mention
    # as an alias. This prevents the surname from becoming a duplicate UNKNOWN/ORG entity.
    single_candidates = [item for item in results if len(norm_name(str(item.get('name') or '')).split()) == 1]
    remove_keys: set[str] = set()
    for single in single_candidates:
        short_name = normalize_space(str(single.get('name') or ''))
        short_key = norm_name(short_name)
        occurrence_count = len(re.findall(r'(?<!\w)' + re.escape(short_name) + r'(?!\w)', text, re.IGNORECASE | re.UNICODE))
        if occurrence_count < 2:
            continue
        for longer in results:
            longer_name = normalize_space(str(longer.get('name') or ''))
            longer_parts = norm_name(longer_name).split()
            if len(longer_parts) < 2 or longer_parts[-1] != short_key:
                continue
            if norm_name(longer_name) == short_key:
                continue
            aliases = longer.get('aliases') if isinstance(longer.get('aliases'), list) else []
            if all(norm_name(str(alias)) != short_key for alias in aliases):
                aliases.append(short_name)
            longer['aliases'] = aliases
            remove_keys.add(short_key)
            break
    results = [item for item in results if norm_name(str(item.get('name') or '')) not in remove_keys]

    # High-precision role-based person recovery supplies the PERSON type for full names
    # such as "Tiền đạo Kylian Mbappe" even if the general proper-name scanner saw UNKNOWN.
    role_people = {norm_name(str(item.get('name') or '')): item for item in extract_named_person_entities(text)}
    for item in results:
        person = role_people.get(norm_name(str(item.get('name') or '')))
        if person:
            item['type'] = 'PERSON'
            aliases = item.get('aliases') if isinstance(item.get('aliases'), list) else []
            for alias in person.get('aliases') or []:
                if all(norm_name(str(old)) != norm_name(str(alias)) for old in aliases):
                    aliases.append(alias)
            item['aliases'] = aliases
    return results[:50]
