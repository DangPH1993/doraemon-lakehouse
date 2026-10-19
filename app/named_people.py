"""High precision recovery of named people explicitly introduced by a person role/title."""
from __future__ import annotations
import re
from .processing import normalize_space, norm_name

_ROLE = re.compile(r'(?i)(?<!\w)(tiền đạo|tiền vệ|hậu vệ|thủ môn|hoa hậu|á hậu|người mẫu|diễn viên|ca sĩ|rapper|tiktoker|tiktoker|nghệ sĩ|đạo diễn|nhà văn|nhà báo|mc|tỷ phú|tỉ phú|tổng thống|cựu tổng thống|thủ tướng|phó thủ tướng|bộ trưởng|chủ tịch|phó chủ tịch|giám đốc|ceo|đại sứ|nữ đại sứ|nam đại sứ|luật sư|bác sĩ|tiến sĩ|giáo sư|vận động viên|huấn luyện viên|hlv|cựu cầu thủ|cầu thủ|hoa khôi|nhà sáng lập|sáng lập viên|chuyên gia|ông|bà|ngài)\s+')
_TOKEN = re.compile(r"[^\W_]+(?:[.'’\-][^\W_]+)*", re.UNICODE)
_CONTEXT = {'mỹ', 'hoa kỳ', 'việt nam', 'trung quốc', 'nga', 'anh', 'hàn quốc', 'nhật bản'}
_NAME_PARTICLES = {'thị','văn','đức','ngọc','hữu','xuân','minh','quốc','thành','hoàng','kim','gia','bảo','tuấn','anh','mạnh','phúc','đình','công','thanh','khánh','phương','thu','thảo','diệu','trọng','như','bích','quỳnh','hải','trí','nhật'}

def extract_named_person_entities(text: str) -> list[dict]:
    text = normalize_space(text or '')[:20000]
    if not text:
        return []
    results, seen = [], set()
    for role_match in _ROLE.finditer(text):
        cursor = role_match.end()
        # Skip country/demonym context which sometimes sits between a role and person's name.
        look = text[cursor:cursor+80]
        for modifier in ('Mỹ ', 'Hoa Kỳ ', 'Việt Nam ', 'Trung Quốc ', 'Hàn Quốc ', 'Nhật Bản '):
            if look.casefold().startswith(modifier.casefold()):
                cursor += len(modifier)
                look = text[cursor:cursor+80]
                break
        tokens = []
        last_end = cursor
        for token_match in _TOKEN.finditer(text, cursor, min(len(text), cursor + 100)):
            token = token_match.group(0)
            gap = text[last_end:token_match.start()]
            if tokens and not re.fullmatch(r'[\s\-–—]*', gap):
                break
            if not tokens and not token[0].isupper():
                break
            if tokens and not token[0].isupper() and token.casefold() not in _NAME_PARTICLES:
                break
            # A name must not consume a new clause/action word even if sentence case capitalizes it.
            if tokens and token.casefold() in {'sau', 'trong', 'đang', 'bị', 'vừa', 'nói', 'cho', 'khi', 'về', 'tại'}:
                break
            tokens.append(token)
            last_end = token_match.end()
            if len(tokens) >= 5:
                break
        if len(tokens) < 2:
            continue
        name = normalize_space(' '.join(tokens)).strip(' .,:;-–—')
        normalized = norm_name(name)
        if len(normalized) < 5 or normalized in seen:
            continue
        # Ground the exact name in source text and require a role cue immediately before it.
        if normalized not in norm_name(text):
            continue
        seen.add(normalized)
        role = normalize_space(role_match.group(1))
        results.append({'name': name, 'type': 'PERSON', 'aliases': [name], 'source_role': role})
    return results[:10]
