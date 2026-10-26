"""Deterministic recovery of named objects missed by NER.

This high-precision supplement recognizes a typed head plus a proper-name span for
locations/infrastructure, facilities, organizations, business outlets and named events.
Generic mentions such as "một siêu thị" or "một cây cầu" are not emitted.
"""
from __future__ import annotations

import re
from typing import Any

from .processing import normalize_space, norm_name
from .entity_rules import is_administrative_location, should_track_entity, is_safe_entity_alias

# Match longer heads before shorter overlapping heads. Canonical heads are stable display
# forms, while the specific name comes from the actual article text.
_HEADS = {
    'đường cao tốc': 'Đường cao tốc',
    'tuyến cao tốc': 'Tuyến cao tốc',
    'đường tỉnh': 'Đường tỉnh',
    'đường liên huyện': 'Đường liên huyện',
    'đường liên xã': 'Đường liên xã',
    'tuyến xe buýt': 'Tuyến xe buýt',
    'tuyến cao tốc': 'Tuyến cao tốc',
    'đường tỉnh': 'Đường tỉnh',
    'đường liên huyện': 'Đường liên huyện',
    'đường liên xã': 'Đường liên xã',
    'đường vành đai': 'Đường vành đai',
    'đường': 'Đường',
    'đại lộ': 'Đại lộ',
    'phố': 'Phố',
    'tuyến phố': 'Tuyến phố',
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
    'sân bay quốc tế': 'Sân bay quốc tế',
    'sân bay': 'Sân bay',
    'bến xe khách': 'Bến xe khách',
    'bến xe': 'Bến xe',
    'bến phà': 'Bến phà',
    'bến tàu': 'Bến tàu',
    'tàu điện': 'Tàu điện',
    'tàu': 'Tàu',
    'máy bay': 'Máy bay',
    'xe buýt': 'Xe buýt',
    'bến cảng': 'Bến cảng',
    'cảng biển': 'Cảng biển',
    'cảng': 'Cảng',
    'nhà ga': 'Nhà ga',
    'ga đường sắt': 'Ga đường sắt',
    'cụm công nghiệp': 'Cụm công nghiệp',
    'khu kinh tế': 'Khu kinh tế',
    'khu công nghiệp': 'Khu công nghiệp',
    'khu chế xuất': 'Khu chế xuất',
    'nhà máy': 'Nhà máy',
    'nhà máy thủy điện': 'Nhà máy thủy điện',
    'thủy điện': 'Thủy điện',
    'đập': 'Đập',
    'hồ chứa': 'Hồ chứa',
    'sân vận động': 'Sân vận động',
    'nhà hát': 'Nhà hát',
    'công viên': 'Công viên',
    'vườn quốc gia': 'Vườn quốc gia',
    'khu bảo tồn': 'Khu bảo tồn',
    'khu đô thị': 'Khu đô thị',
    'khu dân cư': 'Khu dân cư',
    'khu nghỉ dưỡng': 'Khu nghỉ dưỡng',
    'chợ': 'Chợ',
    'chùa': 'Chùa',
    'đền': 'Đền',
    'bệnh viện': 'Bệnh viện',
    'trường đại học': 'Trường đại học',
    'nhà thờ': 'Nhà thờ',
    'vịnh': 'Vịnh',
    'đảo': 'Đảo',
    'quảng trường': 'Quảng trường',
    'bảo tàng': 'Bảo tàng',
    'khu du lịch': 'Khu du lịch',
    'thác': 'Thác',
    'núi': 'Núi',
    'vườn quốc gia': 'Vườn quốc gia',
}
# Named companies, outlets and institutions are proper entities too. The descriptor stays
# in the canonical name so e.g. ``Siêu thị Big C`` is not collapsed into an unrelated person.
_ORG_HEADS = {
    'clb': 'CLB',
    'công an tỉnh': 'Công an tỉnh',
    'công an thành phố': 'Công an thành phố',
    'công an huyện': 'Công an huyện',
    'công an xã': 'Công an xã',
    'bộ công an': 'Bộ Công an',
    'bộ': 'Bộ',
    'sở': 'Sở',
    'cục': 'Cục',
    'phòng cảnh sát': 'Phòng Cảnh sát',
    'phòng công an': 'Phòng Công an',
    'ủy ban nhân dân': 'Ủy ban nhân dân',
    'uỷ ban nhân dân': 'Ủy ban nhân dân',
    'công an': 'Công an',
    'viện': 'Viện',
    'học viện': 'Học viện',
    'báo': 'Báo',
    'đài truyền hình': 'Đài truyền hình',
    'siêu thị': 'Siêu thị',
    'trung tâm y tế': 'Trung tâm y tế',
    'trung tâm hội nghị': 'Trung tâm hội nghị',
    'cửa hàng': 'Cửa hàng',
    'trung tâm thương mại': 'Trung tâm thương mại',
    'tổng công ty': 'Tổng công ty',
    'ủy ban nhân dân': 'Ủy ban nhân dân',
    'uỷ ban nhân dân': 'Ủy ban nhân dân',
    'ubnd thành phố': 'UBND thành phố',
    'ubnd tỉnh': 'UBND tỉnh',
    'ubnd xã': 'UBND xã',
    'viện kiểm sát': 'Viện kiểm sát',
    'tòa án nhân dân': 'Tòa án nhân dân',
    'toà án nhân dân': 'Tòa án nhân dân',
    'khách sạn': 'Khách sạn',
    'nhà hàng': 'Nhà hàng',
    'nhà thuốc': 'Nhà thuốc',
    'công ty cổ phần': 'Công ty cổ phần',
    'công ty trách nhiệm hữu hạn': 'Công ty trách nhiệm hữu hạn',
    'công ty tnhh': 'Công ty TNHH',
    'công ty': 'Công ty',
    'tập đoàn': 'Tập đoàn',
    'ngân hàng': 'Ngân hàng',
    'hãng hàng không': 'Hãng hàng không',
    'hãng xe': 'Hãng xe',
    'nhà mạng': 'Nhà mạng',
    'nhà xuất bản': 'Nhà xuất bản',
    'đài truyền hình': 'Đài truyền hình',
    'đài phát thanh': 'Đài phát thanh',
    'báo': 'Báo',
    'trường đại học': 'Trường đại học',
    'trường thpt': 'Trường THPT',
    'đại học': 'Đại học',
    'nhà sách': 'Nhà sách',
    'sàn thương mại điện tử': 'Sàn thương mại điện tử',
    'mạng xã hội': 'Mạng xã hội',
}
_HEAD_TYPES = {key.casefold(): 'LOCATION' for key in _HEADS}
_HEAD_TYPES.update({key.casefold(): 'ORG' for key in _ORG_HEADS})
_HEADS.update(_ORG_HEADS)
_PRODUCT_HEADS = {'tàu điện', 'tàu', 'máy bay', 'xe buýt'}
_HEAD_TYPES.update({key.casefold(): 'PRODUCT' for key in _PRODUCT_HEADS})
STANDALONE_NAMED_ORGS = {'Bộ Công an', 'NASA', 'SpaceX'}

_EVENT_HEADS = {
    'vụ án': 'Vụ án', 'vụ kiện': 'Vụ kiện', 'giải đấu': 'Giải đấu',
    'giải vô địch': 'Giải vô địch', 'hội nghị': 'Hội nghị', 'chiến dịch': 'Chiến dịch',
    'cuộc thi': 'Cuộc thi', 'trận đấu': 'Trận đấu', 'đại hội': 'Đại hội',
    'chuyến công du': 'Chuyến công du', 'phiên tòa': 'Phiên tòa', 'phiên toà': 'Phiên tòa',
    'vụ cháy': 'Vụ cháy', 'vụ tai nạn': 'Vụ tai nạn',
}
_HEAD_TYPES.update({key.casefold(): 'EVENT' for key in _EVENT_HEADS})
_HEADS.update(_EVENT_HEADS)
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
    """Extract a full named-object phrase (class head + proper name) from text.

    Examples: "Đường Võ Nguyên Giáp", "Siêu thị Big C", "Vụ án Phạm Thoại".
    Bare generic nouns without a named suffix are not entities.
    """
    text = normalize_space(text or '')
    if not text:
        return []

    candidates: list[dict[str, Any]] = []
    for standalone in STANDALONE_NAMED_ORGS:
        for match in re.finditer(r'(?<!\w)' + re.escape(standalone) + r'(?!\w)', text, re.IGNORECASE | re.UNICODE):
            display = normalize_space(match.group(0))
            canonical = standalone if norm_name(display) == norm_name(standalone) else display
            if should_track_entity(canonical, 'ORG'):
                candidates.append({'name': canonical, 'type': 'ORG', 'aliases': [canonical], 'kind_head': '',
                                   'proper_name': canonical, 'start': match.start(), 'end': match.end(), 'person_suffix': None})
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
            if folded in _STOP_TAIL or not (token[0].isupper() or token[0].isdigit()):
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
        linear_head_keys = {'đường', 'phố', 'đại lộ', 'tuyến đường', 'quốc lộ', 'tỉnh lộ',
                            'đường tỉnh', 'đường cao tốc', 'đường vành đai', 'tuyến cao tốc'}
        if not raw_tail or (head_key in linear_head_keys and is_administrative_location(raw_tail)):
            # Prevent false linear objects like ``Phố TPHCM`` where a city abbreviation
            # was misread as a street name. Keep true named landmarks such as Vịnh Hạ Long.
            continue
        name = normalize_space(f'{display_head} {raw_tail}')[:180]
        if len(norm_name(name)) < 5:
            continue
        entity_type = _HEAD_TYPES.get(head_key, 'LOCATION')
        if not should_track_entity(name, entity_type):
            continue
        # Only suppress a PERSON extraction if the person name is exactly the named suffix
        # of a physical-location label (e.g. "Võ Văn Kiệt" inside "Cầu vượt Võ Văn Kiệt").
        person_suffix = None
        if entity_type == 'LOCATION' and '-' not in raw_tail and '–' not in raw_tail and '—' not in raw_tail and 2 <= len(tail_tokens) <= 4:
            person_suffix = normalize_space(raw_tail)
        aliases = [name]
        # Directly stated headless name forms are useful aliases (e.g. "Big C"), while
        # the canonical key always retains the class head ("Siêu thị Big C").
        if (raw_tail and norm_name(raw_tail) != norm_name(name)
                and is_safe_entity_alias(name, raw_tail, entity_type)):
            aliases.append(normalize_space(raw_tail))
        candidates.append({
            'name': name,
            'type': entity_type,
            'aliases': aliases,
            'kind_head': display_head,
            'proper_name': normalize_space(raw_tail),
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
    # Keep extraction bounded: RSS content bodies can be >100 KB, while the title,
    # summary and opening excerpt contain nearly all useful named-object mentions.
    text = normalize_space('. '.join(x for x in (title, summary, (content_text or '')[:5000]) if x))
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

    # High-value explicitly named objects go first so they cannot be crowded out by the
    # model's entity limit. A headless model extraction such as ORG ``Big C`` is folded into
    # the structured ``Siêu thị Big C`` entity when the article explicitly includes that head.
    combined: list[dict[str, Any]] = []
    seen = set()
    structured_aliases: dict[tuple[str, str], int] = {}
    for item in infrastructure:
        typ = str(item.get('type') or 'UNKNOWN').upper().strip()
        for alias in item.get('aliases') or []:
            structured_aliases[(typ, norm_name(str(alias)))] = len(combined)
        key = (typ, norm_name(str(item.get('name') or '')))
        if key not in seen:
            seen.add(key)
            combined.append({'name': item['name'], 'type': typ,
                             'aliases': list(item.get('aliases') or [])})
    for entity in existing:
        name = str(entity.get('name') or entity.get('canonical_name') or '').strip()
        typ = str(entity.get('type') or entity.get('entity_type') or 'UNKNOWN').upper().strip()
        key = (typ, norm_name(name))
        if not name or key in seen:
            continue
        matched_index = structured_aliases.get(key)
        if matched_index is not None:
            for alias in entity.get('aliases') or []:
                if normalize_space(str(alias)) and norm_name(str(alias)) not in {norm_name(str(x)) for x in combined[matched_index]['aliases']}:
                    combined[matched_index]['aliases'].append(normalize_space(str(alias)))
            continue
        seen.add(key)
        combined.append({'name': name, 'type': typ,
                         'aliases': entity.get('aliases') if isinstance(entity.get('aliases'), list) else []})
    return combined[:50], suppress_names
