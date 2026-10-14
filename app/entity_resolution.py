"""Shared deterministic entity resolution for article ingestion and classification.

Canonical entity IDs are stable; surface forms such as "Donald Trump", "Trump",
"President Trump" and "ông Trump" are resolved through normalized aliases. Short
aliases are only accepted when they identify one compatible active entity, avoiding
silent merges when a surname is ambiguous.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

from .processing import normalize_space, norm_name
from .entity_names import entity_normalized_key

# Longest forms first. These are honorific/role prefixes, not identity-bearing words.
_PERSON_TITLE_PREFIXES = sorted((
    'cựu tổng thống mỹ', 'tổng thống mỹ', 'cựu tổng thống', 'former president of the united states',
    'former president', 'president of the united states', 'prime minister of', 'thủ tướng',
    'tổng thống', 'chủ tịch', 'bộ trưởng', 'phó tổng thống', 'vice president', 'prime minister',
    'secretary of state', 'secretary', 'senator', 'governor', 'chancellor', 'king', 'queen',
    'prince', 'princess', 'president', 'premier', 'minister', 'chairman', 'chairwoman', 'ceo',
    'tiktoker', 'ông', 'bà', 'ngài', 'mr.', 'mrs.', 'ms.', 'miss', 'dr.', 'prof.', 'sir',
    'mr', 'mrs', 'ms', 'dr', 'prof',
), key=len, reverse=True)
_SUFFIXES_NOT_NAMES = {'jr', 'sr', 'ii', 'iii', 'iv', 'phd'}


def strip_person_title(name: str) -> str:
    """Strip recognized leading honorific/role prefixes without changing the name."""
    value = normalize_space(str(name or '')).strip(' ,.;:-')
    for _ in range(4):
        low = value.casefold()
        stripped = value
        for prefix in _PERSON_TITLE_PREFIXES:
            if low == prefix or low.startswith(prefix + ' '):
                stripped = value[len(prefix):].lstrip(' ,.;:-')
                break
        if stripped == value:
            break
        value = stripped
    return normalize_space(value).strip(' ,.;:-')


def entity_name_variants(name: str, entity_type: str = 'UNKNOWN', aliases: Iterable[str] | None = None) -> list[str]:
    """Return ordered, de-duplicated surface forms from most to least specific.

    Last-name aliases are generated only for PERSON entities. They are not assumed to
    be globally unique: the resolver below requires a single compatible target.
    """
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    primary = normalize_space(str(name or '')).strip(' ,.;:-')
    supplied_aliases = list(aliases or [])
    candidates: list[str] = []

    def add(value: str) -> None:
        value = normalize_space(str(value or '')).strip(' ,.;:-')
        if value and norm_name(value) and all(norm_name(value) != norm_name(old) for old in candidates):
            candidates.append(value)

    if typ == 'PERSON':
        stripped = strip_person_title(primary)
        # In Vietnamese/English news headlines, bare “Trump” is a common editorial
        # reference to Donald Trump. Resolve this explicit high-frequency alias before
        # testing the short surface form, which may otherwise collide with family names.
        if norm_name(stripped) == 'trump':
            add('Donald Trump')
        add(primary)
        add(stripped)
        for alias in supplied_aliases:
            alias = normalize_space(str(alias or '')).strip(' ,.;:-')
            add(alias)
            add(strip_person_title(alias))
        # Generate common abbreviated forms (e.g. Donald J. Trump -> Donald Trump -> Trump).
        for person_name in [primary, stripped, *supplied_aliases]:
            clean = strip_person_title(person_name)
            tokens = re.findall(r"[^\W_]+(?:[-’'][^\W_]+)*", clean, flags=re.UNICODE)
            if len(tokens) >= 3:
                add(f'{tokens[0]} {tokens[-1]}')
            if len(tokens) >= 2 and tokens[-1].casefold().strip('.') not in _SUFFIXES_NOT_NAMES and len(tokens[-1]) >= 3:
                add(tokens[-1])
    else:
        add(primary)
        for alias in supplied_aliases:
            add(alias)
    return candidates[:30]


def _type_compatible(requested_type: str, candidate_type: str) -> bool:
    requested = str(requested_type or 'UNKNOWN').upper().strip()
    candidate = str(candidate_type or 'UNKNOWN').upper().strip()
    return requested == 'UNKNOWN' or candidate == 'UNKNOWN' or requested == candidate


def build_entity_lookup(entities: Iterable[dict[str, Any]], aliases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build indexed entity resolution structures from active entities and their aliases."""
    lookup: dict[str, Any] = {
        'entities_by_id': {},
        'canonical_to_ids': defaultdict(set),
        'alias_to_ids': defaultdict(set),
    }
    for entity in entities:
        if str(entity.get('status', 'active')).lower() != 'active':
            continue
        eid = int(entity['entity_id'])
        lookup['entities_by_id'][eid] = dict(entity)
        # Support both pre-v0.7.2 names and structured `<kind>*_<name>` keys during rollout.
        lookup['canonical_to_ids'][norm_name(str(entity.get('normalized_name') or ''))].add(eid)
        lookup['canonical_to_ids'][norm_name(str(entity.get('canonical_name') or ''))].add(eid)
        structured = entity_normalized_key(str(entity.get('canonical_name') or ''), str(entity.get('entity_type') or 'UNKNOWN'))
        if structured:
            lookup['canonical_to_ids'][structured].add(eid)
    for alias in aliases:
        eid = int(alias['entity_id'])
        if eid not in lookup['entities_by_id']:
            continue
        lookup['alias_to_ids'][norm_name(str(alias.get('normalized_alias') or alias.get('alias') or ''))].add(eid)
    return lookup


def add_entity_to_lookup(lookup: dict[str, Any], entity: dict[str, Any], alias_names: Iterable[str] = ()) -> None:
    eid = int(entity['entity_id'])
    entity = dict(entity)
    entity.setdefault('status', 'active')
    lookup['entities_by_id'][eid] = entity
    lookup['canonical_to_ids'][norm_name(str(entity.get('normalized_name') or ''))].add(eid)
    lookup['canonical_to_ids'][norm_name(str(entity.get('canonical_name') or ''))].add(eid)
    structured = entity_normalized_key(str(entity.get('canonical_name') or ''), str(entity.get('entity_type') or 'UNKNOWN'))
    if structured:
        lookup['canonical_to_ids'][structured].add(eid)
    for alias in alias_names:
        normalized = norm_name(str(alias or ''))
        if normalized:
            lookup['alias_to_ids'][normalized].add(eid)


def resolve_entity_in_lookup(name: str, entity_type: str, lookup: dict[str, Any], aliases: Iterable[str] | None = None) -> tuple[int | None, bool]:
    """Return (entity_id, ambiguous). A short alias is never guessed when it collides."""
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    input_norm = norm_name(name)
    preferred_norm = norm_name(strip_person_title(name)) if typ == 'PERSON' else input_norm
    variants = entity_name_variants(name, entity_type, aliases)
    for variant in variants:
        normalized_candidates = list(dict.fromkeys((
            norm_name(variant),
            entity_normalized_key(variant, entity_type),
        )))
        for normalized in normalized_candidates:
            if not normalized:
                continue
            ids = set(lookup['canonical_to_ids'].get(normalized, set()))
            ids.update(lookup['alias_to_ids'].get(normalized, set()))
            compatible = {
                eid for eid in ids
                if _type_compatible(entity_type, lookup['entities_by_id'][eid].get('entity_type', 'UNKNOWN'))
            }
            if len(compatible) == 1:
                return next(iter(compatible)), False
            if len(compatible) > 1:
                # A full multi-token name may still be a new, distinct person even when its
                # generated surname alias collides. A bare/role-prefixed surname stays ambiguous.
                if normalized in {input_norm, preferred_norm} or ' ' not in preferred_norm:
                    return None, True
                continue
    return None, False


def resolve_entity_in_database(conn, name: str, entity_type: str, aliases: Iterable[str] | None = None) -> tuple[dict[str, Any] | None, bool]:
    """Resolve a surface form against indexed canonical-name and alias columns in PostgreSQL."""
    variants = entity_name_variants(name, entity_type, aliases)
    normalized_variants = []
    for value in variants:
        for normalized in (norm_name(value), entity_normalized_key(value, entity_type)):
            if normalized and normalized not in normalized_variants:
                normalized_variants.append(normalized)
    if not normalized_variants:
        return None, False
    with conn.cursor() as cur:
        cur.execute("""WITH variants AS (
                         SELECT value AS variant_name, ordinality AS variant_rank
                         FROM unnest(%s::text[]) WITH ORDINALITY AS v(value, ordinality)
                       )
                       SELECT DISTINCT v.variant_rank,e.entity_id,e.canonical_name,e.normalized_name,e.entity_type,e.status
                       FROM variants v
                       JOIN entities e ON e.status='active'
                         AND (%s='UNKNOWN' OR e.entity_type=%s OR e.entity_type='UNKNOWN')
                       WHERE e.normalized_name=v.variant_name
                          OR EXISTS (SELECT 1 FROM entity_aliases a
                                     WHERE a.entity_id=e.entity_id AND a.normalized_alias=v.variant_name)
                       ORDER BY v.variant_rank,e.entity_id""", (normalized_variants, str(entity_type or 'UNKNOWN').upper().strip(), str(entity_type or 'UNKNOWN').upper().strip()))
        matches = cur.fetchall()
    by_rank: dict[int, dict[int, dict[str, Any]]] = {}
    for match in matches:
        by_rank.setdefault(int(match['variant_rank']), {})[int(match['entity_id'])] = dict(match)
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    input_norm = norm_name(name)
    preferred_norm = norm_name(strip_person_title(name)) if typ == 'PERSON' else input_norm
    for rank in sorted(by_rank):
        candidates = by_rank[rank]
        if len(candidates) == 1:
            row = dict(next(iter(candidates.values())))
            row['_resolution_method'] = 'exact'
            return row, False
        if len(candidates) > 1:
            variant_norm = normalized_variants[rank - 1] if 0 < rank <= len(normalized_variants) else ''
            if variant_norm in {input_norm, preferred_norm} or ' ' not in preferred_norm:
                return None, True
            # A generated short alias is ambiguous, but the full multi-token name may be new.
            continue

    # Approximate lookup is a fallback only after canonical-name/alias matching fails.
    # pg_trgm indexes keep this viable for large entity catalogs; short ambiguous names
    # (e.g. "Trump") are deliberately excluded from fuzzy matching.
    fuzzy_variants = list(dict.fromkeys(
        value for value in normalized_variants
        if len(value) >= 8 and (' ' in value or len(value) >= 10)
    ))
    if fuzzy_variants:
        with conn.cursor() as cur:
            cur.execute("""SELECT quote_ident(n.nspname) AS schema_ident
                           FROM pg_extension ext JOIN pg_namespace n ON n.oid=ext.extnamespace
                           WHERE ext.extname='pg_trgm' LIMIT 1""")
            extension = cur.fetchone()
        if extension:
            schema_ident = str(extension['schema_ident'])
            similarity_fn = f'{schema_ident}.similarity'
            trigram_operator = f'OPERATOR({schema_ident}.%%)'
            threshold = 0.78 if any(' ' in value for value in fuzzy_variants) else 0.90
            with conn.cursor() as cur:
                cur.execute(f"""WITH variants AS (
                                  SELECT value AS variant_name
                                  FROM unnest(%s::text[]) AS v(value)
                                ), candidate_scores AS (
                                  SELECT e.entity_id,e.canonical_name,e.normalized_name,e.entity_type,e.status,
                                         max(greatest(
                                           {similarity_fn}(e.normalized_name,v.variant_name),
                                           coalesce(alias_scores.alias_score,0)
                                         ))::float AS score
                                  FROM entities e
                                  CROSS JOIN variants v
                                  LEFT JOIN LATERAL (
                                    SELECT max({similarity_fn}(a.normalized_alias,v.variant_name)) AS alias_score
                                    FROM entity_aliases a WHERE a.entity_id=e.entity_id
                                  ) alias_scores ON true
                                  WHERE e.status='active'
                                    AND (%s='UNKNOWN' OR e.entity_type=%s OR e.entity_type='UNKNOWN')
                                    AND (
                                      e.normalized_name {trigram_operator} v.variant_name
                                      OR EXISTS (
                                        SELECT 1 FROM entity_aliases a
                                        WHERE a.entity_id=e.entity_id
                                          AND a.normalized_alias {trigram_operator} v.variant_name
                                      )
                                    )
                                  GROUP BY e.entity_id,e.canonical_name,e.normalized_name,e.entity_type,e.status
                                )
                                SELECT entity_id,canonical_name,normalized_name,entity_type,status,score
                                FROM candidate_scores
                                WHERE score >= %s
                                ORDER BY score DESC, entity_id
                                LIMIT 3""",
                            (fuzzy_variants, typ, typ, threshold))
                fuzzy_matches = cur.fetchall()
            if fuzzy_matches:
                top = dict(fuzzy_matches[0])
                second_score = float(fuzzy_matches[1]['score']) if len(fuzzy_matches) > 1 else 0.0
                top_score = float(top['score'])
                # Similar candidates are ambiguous; require a clear winner to avoid false merges.
                if top_score >= threshold and top_score - second_score >= 0.06:
                    top['_resolution_method'] = 'fuzzy'
                    top['_similarity'] = top_score
                    return top, False
                return None, True
    return None, False
