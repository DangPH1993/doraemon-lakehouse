"""Deterministic eligibility rules for followable News Radar entities.

The bundled administrative gazetteer is static; runtime does not depend on pycountry or
network access. Country/province/city names are excluded regardless of model label (except a real PERSON name), while specific named points
of interest such as Chùa Hương, Chùa Bái Đính and Vịnh Hạ Long remain valid entities.
"""
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


def _fold(value: str) -> str:
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode('ascii')
    value = value.casefold()
    value = re.sub(r'[^\w\s-]', ' ', value, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', value).strip().strip('-')


def is_administrative_location(name: str) -> bool:
    """Return True for country/province/city names, not named places/landmarks."""
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
    return any(candidate in _ADMIN_LOCATIONS for candidate in candidates if candidate)


def should_track_entity(name: str, entity_type: str = 'UNKNOWN') -> bool:
    """Reject vague nouns and administrative geography; retain specifically named objects."""
    name = normalize_space(str(name or ''))[:180]
    normalized = norm_name(name)
    folded = _fold(normalized)
    if len(normalized) < 2 or len(normalized) > 180:
        return False
    if folded in _GENERIC_FOLDED:
        return False
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    if typ != 'PERSON' and is_administrative_location(name):
        return False
    return True
