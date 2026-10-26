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
from .entity_rules import is_administrative_location, is_safe_person_alias

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
    """Return ordered, de-duplicated, high-precision surface forms.

    Vietnamese multi-token people names do not receive generated one-token surname/given
    name aliases. Such aliases (Lan, Nga, Anh, Mỹ, Nam, etc.) cause unrelated articles to
    be attached to people. Middle-initial variants remain supported, and only explicitly
    allowlisted short aliases (currently Donald Trump -> Trump) are generated.
    """
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    primary = normalize_space(str(name or '')).strip(' ,.;:-')
    supplied_aliases = list(aliases or [])
    candidates: list[str] = []

    def add(value: str) -> None:
        value = normalize_space(str(value or '')).strip(' ,.;:-')
        if typ == 'PERSON' and len(norm_name(value).split()) == 1 and is_administrative_location(value):
            return
        if value and norm_name(value) and all(norm_name(value) != norm_name(old) for old in candidates):
            candidates.append(value)

    if typ == 'PERSON':
        stripped = strip_person_title(primary)
        # Explicit editorial rule: a bare Trump in PERSON context maps to Donald Trump.
        if norm_name(stripped) == 'trump':
            add('Donald Trump')
        add(primary)
        add(stripped)
        for alias_value in supplied_aliases:
            alias = normalize_space(str(alias_value or '')).strip(' ,.;:-')
            if alias and is_safe_person_alias(primary, alias):
                add(alias)
                add(strip_person_title(alias))

        for person_name in [primary, stripped]:
            clean = strip_person_title(person_name)
            tokens = re.findall(r"[^\W_]+(?:[-’'][^\W_]+)*", clean, flags=re.UNICODE)
            abbreviated = None
            if len(tokens) >= 3 and all(len(token.rstrip('.')) == 1 for token in tokens[1:-1]):
                # Preserve the established middle-initial variant: Donald J. Trump -> Donald Trump.
                abbreviated = f'{tokens[0]} {tokens[-1]}'
                add(abbreviated)
            if abbreviated and is_safe_person_alias(abbreviated, tokens[-1]):
                add(tokens[-1])
            elif len(tokens) >= 2 and is_safe_person_alias(clean, tokens[-1]):
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
        entity = lookup['entities_by_id'][eid]
        alias_text = str(alias.get('alias') or alias.get('normalized_alias') or '')
        if str(entity.get('entity_type') or 'UNKNOWN').upper().strip() == 'PERSON':
            if not is_safe_person_alias(str(entity.get('canonical_name') or ''), alias_text):
                continue  # Ignore unsafe legacy aliases already present in Neon.
        normalized = norm_name(str(alias.get('normalized_alias') or alias.get('alias') or ''))
        if normalized:
            lookup['alias_to_ids'][normalized].add(eid)
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
        if str(entity.get('entity_type') or 'UNKNOWN').upper().strip() == 'PERSON':
            if not is_safe_person_alias(str(entity.get('canonical_name') or ''), str(alias or '')):
                continue
        normalized = norm_name(str(alias or ''))
        if normalized:
            lookup['alias_to_ids'][normalized].add(eid)


def resolve_entity_in_lookup(name: str, entity_type: str, lookup: dict[str, Any], aliases: Iterable[str] | None = None) -> tuple[int | None, bool]:
    """Return (entity_id, ambiguous). A short alias is never guessed when it collides."""
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    input_norm = norm_name(name)
    preferred_norm = norm_name(strip_person_title(name)) if typ == 'PERSON' else input_norm
    # A bare country/place name must never resolve to a PERSON through a surname alias.
    if typ == 'PERSON' and is_administrative_location(name):
        return None, True
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
            collision_prone_place_alias = len(norm_name(variant).split()) == 1 and is_administrative_location(variant)
            compatible = {
                eid for eid in ids
                if _type_compatible(entity_type, lookup['entities_by_id'][eid].get('entity_type', 'UNKNOWN'))
                and not (collision_prone_place_alias and str(lookup['entities_by_id'][eid].get('entity_type') or 'UNKNOWN').upper().strip() == 'PERSON')
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
    """Resolve exact canonical/full-name aliases only; avoid fuzzy PERSON identity merges."""
    typ = str(entity_type or 'UNKNOWN').upper().strip()
    input_norm = norm_name(name)
    if typ == 'PERSON' and is_administrative_location(name):
        return None, True
    variants = entity_name_variants(name, entity_type, aliases)
    variant_meta: list[tuple[str, str]] = []  # normalized form, original surface form
    normalized_seen = set()
    for value in variants:
        for normalized in (norm_name(value), entity_normalized_key(value, entity_type)):
            if normalized and normalized not in normalized_seen:
                normalized_seen.add(normalized)
                variant_meta.append((normalized, value))
    normalized_variants = [item[0] for item in variant_meta]
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
                       ORDER BY v.variant_rank,e.entity_id""", (normalized_variants, typ, typ))
        matches = cur.fetchall()
    by_rank: dict[int, dict[int, dict[str, Any]]] = {}
    for match in matches:
        row = dict(match)
        rank = int(row['variant_rank'])
        raw_variant = variant_meta[rank - 1][1] if 0 < rank <= len(variant_meta) else ''
        row_type = str(row.get('entity_type') or 'UNKNOWN').upper().strip()
        if row_type == 'PERSON':
            canonical = str(row.get('canonical_name') or '')
            normalized_name = str(row.get('normalized_name') or '')
            variant_norm = norm_name(raw_variant)
            # A normalized primary key match is authoritative. An alias match must pass
            # the same identity guard as the in-memory resolver, even if a stale alias
            # row still exists in the database.
            canonical_match = (
                variant_norm == norm_name(canonical)
                or norm_name(raw_variant) == norm_name(normalized_name)
                or norm_name(raw_variant) == norm_name(entity_normalized_key(canonical, row_type))
            )
            if not canonical_match and not is_safe_person_alias(canonical, raw_variant):
                continue
        by_rank.setdefault(rank, {})[int(row['entity_id'])] = row
    preferred_norm = norm_name(strip_person_title(name)) if typ == 'PERSON' else input_norm
    for rank in sorted(by_rank):
        candidates = by_rank[rank]
        variant_norm = normalized_variants[rank - 1] if 0 < rank <= len(normalized_variants) else ''
        collision_prone_place_alias = len(norm_name(variant_norm).split()) == 1 and is_administrative_location(variant_norm)
        if collision_prone_place_alias:
            candidates = {eid: row for eid, row in candidates.items()
                          if str(row.get('entity_type') or 'UNKNOWN').upper().strip() != 'PERSON'}
        if not candidates:
            continue
        if len(candidates) == 1:
            row = dict(next(iter(candidates.values())))
            row.pop('variant_rank', None)
            row['_resolution_method'] = 'exact'
            return row, False
        if len(candidates) > 1:
            if variant_norm in {input_norm, preferred_norm} or ' ' not in preferred_norm:
                return None, True
            continue

    # Fuzzy resolution is deliberately disabled for PERSON entities. Similarity of short
    # Vietnamese names/aliases is not sufficient proof of identity; use exact full-name
    # normalization, explicit trusted aliases, and periodic GenAI reconciliation instead.
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
                                    AND e.entity_type <> 'PERSON'
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
                if top_score >= threshold and top_score - second_score >= 0.06:
                    top['_resolution_method'] = 'fuzzy'
                    top['_similarity'] = top_score
                    return top, False
                return None, True
    return None, False

