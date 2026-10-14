"""Canonical, type-qualified entity keys for News Radar.

Displayed canonical names remain readable (e.g. ``Đường Võ Nguyên Giáp``), while
``entities.normalized_name`` uses a structured identity key (``đường*_võ nguyên giáp``).
Aliases are kept as normalized surface forms, without the type prefix, so a headline can
resolve either the full structured name or a direct alias.
"""
from __future__ import annotations

from .processing import norm_name, normalize_space

# Longest subtype heads first. They distinguish named objects that share a person/brand name.
ENTITY_NAME_HEADS = tuple(sorted({
    'tòa án nhân dân', 'toà án nhân dân', 'viện kiểm sát', 'ủy ban nhân dân', 'uỷ ban nhân dân',
    'học viện', 'tổng công ty', 'liên đoàn', 'hiệp hội', 'ủy ban', 'uỷ ban', 'tòa án', 'toà án',
    'bộ', 'sở', 'quỹ', 'đảng',
    'đường cao tốc', 'tuyến cao tốc', 'đường vành đai', 'đường sắt đô thị', 'tuyến đường sắt', 'đường tỉnh', 'đường liên huyện', 'đường liên xã', 'tuyến xe buýt',
    'trung tâm thương mại', 'công ty cổ phần', 'công ty trách nhiệm hữu hạn', 'trung tâm y tế', 'trung tâm hội nghị',
    'công ty tnhh', 'hãng hàng không', 'khu công nghiệp', 'khu chế xuất',
    'nhà máy thủy điện', 'cảng hàng không', 'tuyến đường sắt', 'đường sắt',
    'tuyến metro', 'tỉnh lộ', 'quốc lộ', 'cao tốc', 'đại lộ', 'đường', 'phố',
    'cầu vượt', 'cầu cạn', 'cầu treo', 'cầu', 'tuyến đường', 'hầm',
    'cảng biển', 'bến cảng', 'cảng', 'nhà ga', 'sân bay quốc tế', 'hồ chứa', 'đập', 'cụm công nghiệp', 'khu kinh tế', 'nhà hát', 'bến xe khách', 'ga đường sắt', 'bến xe', 'bến phà', 'bến tàu', 'tàu điện', 'tàu', 'máy bay', 'xe buýt', 'nhà máy',
    'thủy điện', 'đập', 'hồ chứa', 'sân vận động', 'công viên', 'chùa', 'đền',
    'bệnh viện', 'trường đại học', 'trường thpt', 'nhà thờ', 'vịnh', 'đảo',
    'quảng trường', 'bảo tàng', 'khu du lịch', 'thác', 'núi', 'vườn quốc gia', 'khu bảo tồn', 'khu đô thị', 'khu dân cư', 'khu nghỉ dưỡng', 'chợ', 'bến xe', 'siêu thị',
    'cửa hàng', 'khách sạn', 'nhà hàng', 'công ty', 'tập đoàn', 'ngân hàng',
    'hãng xe', 'nhà mạng', 'nhà xuất bản', 'đài truyền hình', 'đài phát thanh',
    'báo', 'đại học', 'nhà sách', 'sàn thương mại điện tử', 'mạng xã hội',
    'vụ án', 'vụ kiện', 'giải đấu', 'giải vô địch', 'hội nghị', 'chiến dịch', 'cuộc thi', 'trận đấu', 'đại hội', 'chuyến công du', 'phiên tòa', 'phiên toà', 'vụ cháy', 'vụ tai nạn',
}, key=lambda x: len(norm_name(x)), reverse=True))


def named_head_and_tail(name: str) -> tuple[str, str] | None:
    """Return (named-object class, proper-name tail) when a specific head is present."""
    normalized = norm_name(normalize_space(name))
    if not normalized:
        return None
    for head in ENTITY_NAME_HEADS:
        head_norm = norm_name(head)
        if normalized.startswith(head_norm + ' '):
            tail = normalized[len(head_norm):].strip()
            if tail:
                return head_norm, tail
    return None


def entity_normalized_key(name: str, entity_type: str = 'UNKNOWN') -> str:
    """Return the stable ``<kind>*_<name>`` identity key used by the database.

    Specific named objects use their explicit subtype as the key prefix, e.g.
    ``đường*_võ nguyên giáp`` or ``siêu thị*_big c``. Other entities use their broad type,
    e.g. ``person*_donald trump`` and ``event*_world cup 2026``.
    """
    normalized = norm_name(normalize_space(name))
    if not normalized:
        return ''
    named = named_head_and_tail(normalized)
    if named:
        head, tail = named
        return f'{head}*_{tail}'
    typ = norm_name(str(entity_type or 'UNKNOWN').casefold()).replace(' ', '_') or 'unknown'
    return f'{typ}*_{normalized}'
