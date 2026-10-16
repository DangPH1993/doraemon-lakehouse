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
_NON_ENTITY_ACRONYMS = {'ram', 'cpu', 'gpu', 'ai', 'gdp', 'usd', 'vnd'}
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
        return any(c.isdigit() for c in token)
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
    i = 0
    while i < len(tokens):
        token_match = tokens[i]
        token = token_match.group(0)
        low = token.casefold()
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
        if count_named < 2 and not has_strong_single and not role_context and not cue_context:
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
        local_context = text[max(0, token_match.start() - 100):min(len(text), tokens[j - 1].end() + 100)].casefold()
        automotive_context = any(cue.casefold() in local_context for cue in _AUTOMOTIVE_CONTEXT_CUES)
        camel_case_brand = (' ' not in name and any(c.isupper() for c in name[1:]) and any(c.islower() for c in name))
        is_product_cue = bool(cue_words & _PRODUCT_CUES) or cue_context or any(ch.isdigit() for ch in name)
        is_org_cue = bool(cue_words & _ORG_CUES) or has_org_suffix or leading_cue in _ORG_CUES
        inferred_type = ('PERSON' if person_context else
                         'PRODUCT' if is_product_cue else
                         'ORG' if (is_org_cue or camel_case_brand) else
                         'PRODUCT' if (automotive_context and len(phrase_tokens) >= 2) else 'UNKNOWN')
        if not should_track_entity(name, inferred_type):
            i = max(i + 1, j)
            continue
        # Check the captured span against exact source text and named-entity quality rules.
        if is_grounded_entity(name, [name], text, inferred_type):
            seen.add(key)
            results.append({'name': name, 'type': inferred_type, 'aliases': [name], 'source': 'proper_noun_recovery'})
        i = max(i + 1, j)
    return results[:50]
