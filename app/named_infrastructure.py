"""Deterministic recovery of named infrastructure/location entities missed by NER.

This is intentionally a high-precision supplement to GenAI extraction: a location head
(e.g. "cao tốc", "cầu vượt") must be followed by a proper-name span. Generic mentions
such as "một cây cầu đang thi công" are not emitted.
"""
from __future__ import annotations

import re
from typing import Any

from .processing import normalize_space, norm_name

# Match longer heads before shorter overlapping heads. Canonical heads are stable display
# forms, while the specific name comes from the actual article text.
_HEADS = {
    'đường cao tốc': 'Đường cao tốc',
    'cao tốc': 'Cao tốc',
    'quốc lộ': 'Quốc lộ',
    'tỉnh lộ': 'Tỉnh lộ',
    'cầu vượt': 'Cầu vượt',
    'cầu cạn': 'Cầu cạn',
    'cầu treo': 'Cầu treo',
    'cầu': 'Cầu',
    'tuyến đường sắt': 'Tuyến đường sắt',
    'đường sắt': 'Đường sắt',
    'tuyến metro': 'Tuyến metro',
    'tuyến đường': 'Tuyến đường',
    'hầm': 'Hầm',
    'cảng hàng không': 'Cảng hàng không',
    'sân bay': 'Sân bay',
    'bến cảng': 'Bến cảng',
    'cảng biển': 'Cảng biển',
    'cảng': 'Cảng',
    'nhà ga': 'Nhà ga',
    'ga đường sắt': 'Ga đường sắt',
    'khu công nghiệp': 'Khu công nghiệp',
    'khu chế xuất': 'Khu chế xuất',
    'nhà máy': 'Nhà máy',
    'nhà máy thủy điện': 'Nhà máy thủy điện',
    'thủy điện': 'Thủy điện',
    'đập': 'Đập',
    'hồ chứa': 'Hồ chứa',
    'sân vận động': 'Sân vận động',
    'công viên': 'Công viên',
    'chùa': 'Chùa',
    'đền': 'Đền',
    'bệnh viện': 'Bệnh viện',
    'trường đại học': 'Trường đại học',
    'nhà thờ': 'Nhà thờ',
    'vịnh': 'Vịnh',
    'đảo': 'Đảo',
}
_HEAD_PATTERN = re.compile(
    r'(?<!\w)(?P<head>' + '|'.join(re.escape(x) for x in sorted(_HEADS, key=len, reverse=True)) + r')(?!\w)',
    re.IGNORECASE | re.UNICODE,
)
_WORD_PATTERN = re.compile(r"[^\W_]+(?:[.'’][^\W_]+)*", re.UNICODE)
_STOP_TAIL = {
    'tạm', 'cấm', 'cấm', 'để', 'nhằm', 'bị', 'được', 'đang', 'sẽ', 'vừa', 'đã', 'phải',
    'khiến', 'gây', 'do', 'từ', 'trong', 'qua', 'tại', 'ở', 'trên', 'dưới', 'với',
    'theo', 'hôm', 'ngày', 'đêm', 'đoạn', 'các', 'một', 'nhiều', 'tiếp', 'đóng', 'mở',
    'xảy', 'xây', 'thi', 'công', 'đi', 'vào', 'ra', 'là', 'và', 'sau', 'trước',
}
_ALLOWED_GAPS = re.compile(r'^[\s\-–—/]*$', re.UNICODE)


def extract_named_infrastructure_entities(text: str) -> list[dict[str, Any]]:
    """Extract explicitly named physical locations/infrastructure as LOCATION entities.

    Examples:
      * "Cao tốc Mỹ Thuận - Cần Thơ tạm cấm xe" -> "Cao tốc Mỹ Thuận - Cần Thơ"
      * "lắp dầm cầu vượt Võ Văn Kiệt" -> "Cầu vượt Võ Văn Kiệt"

    The function does not create entities for bare generic infrastructure words.
    """
    text = normalize_space(text or '')
    if not text:
        return []

    candidates: list[dict[str, Any]] = []
    for head_match in _HEAD_PATTERN.finditer(text):
        head_key = head_match.group('head').casefold()
        display_head = _HEADS.get(head_key)
        if not display_head:
            # Case-folding can change some Unicode forms; resolve against the static keys.
            display_head = next((v for k, v in _HEADS.items() if k.casefold() == head_key), None)
        if not display_head:
            continue

        tail_tokens = []
        cursor = head_match.end()
        last_end = cursor
        for token_match in _WORD_PATTERN.finditer(text, cursor, min(len(text), cursor + 150)):
            gap = text[last_end:token_match.start()]
            if not _ALLOWED_GAPS.fullmatch(gap):
                break
            token = token_match.group(0)
            folded = token.casefold()
            if folded in _STOP_TAIL or not token[0].isupper():
                break
            tail_tokens.append(token)
            last_end = token_match.end()
            if len(tail_tokens) >= 7:
                break

        # A named location must have at least one proper-name token after the head. This
        # admits names such as "Cầu Rồng" but rejects generic "cầu vượt đang thi công".
        if not tail_tokens:
            continue
        raw_tail = text[head_match.end():last_end].strip(' \t-–—/')
        if not raw_tail:
            continue
        name = normalize_space(f'{display_head} {raw_tail}')[:180]
        if len(norm_name(name)) < 5:
            continue
        # Only suppress a PERSON extraction if the person name is exactly the named suffix
        # of an infrastructure label (e.g. "Võ Văn Kiệt" inside "Cầu vượt Võ Văn Kiệt").
        person_suffix = None
        if '-' not in raw_tail and '–' not in raw_tail and '—' not in raw_tail and 2 <= len(tail_tokens) <= 4:
            person_suffix = normalize_space(raw_tail)
        candidates.append({
            'name': name,
            'type': 'LOCATION',
            'aliases': [name],
            'start': head_match.start(),
            'end': last_end,
            'person_suffix': person_suffix,
        })

    # Prefer the longest head/name span and discard nested matches ("đường cao tốc X"
    # must not also produce "cao tốc X").
    candidates.sort(key=lambda x: (x['start'], -(x['end'] - x['start'])))
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        if any(candidate['start'] < old['end'] and candidate['end'] > old['start'] for old in selected):
            continue
        if any(norm_name(candidate['name']) == norm_name(old['name']) for old in selected):
            continue
        selected.append(candidate)
    return selected


def augment_entities_with_named_infrastructure(
    title: str, summary: str, existing_entities: list[dict[str, Any]] | None = None,
    content_text: str = '',
) -> tuple[list[dict[str, Any]], set[str]]:
    """Add named infrastructure and return safe PERSON names to suppress for this article."""
    text = normalize_space('. '.join(x for x in (title, summary, content_text) if x))
    infrastructure = extract_named_infrastructure_entities(text)
    suppress_names: set[str] = set()
    for item in infrastructure:
        suffix = item.get('person_suffix')
        if not suffix:
            continue
        # If the same name also appears elsewhere outside the named infrastructure span,
        # preserve it as a legitimate person entity. Otherwise it is only the place's name.
        occurrences = list(re.finditer(r'(?<!\w)' + re.escape(suffix) + r'(?!\w)', text, re.IGNORECASE | re.UNICODE))
        if occurrences and all(item['start'] <= m.start() and m.end() <= item['end'] for m in occurrences):
            suppress_names.add(norm_name(suffix))

    existing: list[dict[str, Any]] = []
    for entity in existing_entities or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get('name') or entity.get('canonical_name') or '').strip()
        typ = str(entity.get('type') or entity.get('entity_type') or 'UNKNOWN').upper().strip()
        if typ == 'PERSON' and norm_name(name) in suppress_names:
            continue
        existing.append(entity)

    # High-value named infrastructure goes first so it cannot be crowded out by the model's
    # 10-entity limit. The upsert layer will resolve aliases to an existing canonical ID.
    combined: list[dict[str, Any]] = []
    seen = set()
    for entity in infrastructure + existing:
        name = str(entity.get('name') or entity.get('canonical_name') or '').strip()
        typ = str(entity.get('type') or entity.get('entity_type') or 'UNKNOWN').upper().strip()
        key = (typ, norm_name(name))
        if not name or key in seen:
            continue
        seen.add(key)
        combined.append({
            'name': name,
            'type': typ,
            'aliases': entity.get('aliases') if isinstance(entity.get('aliases'), list) else [],
        })
    return combined[:10], suppress_names
