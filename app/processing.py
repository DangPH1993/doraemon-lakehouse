import hashlib, re, unicodedata
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from bs4 import BeautifulSoup

TRACKING_KEYS = {'fbclid','gclid','mc_cid','mc_eid','ref_src'}
STOP_ENTITIES = {'the','a','an','and','or','but','for','with','from','after','before','over','under','new','says','say','report','reports','what','how','why','when','where','this','that','its','their'}

def normalize_space(value: str | None) -> str:
    return re.sub(r'\s+', ' ', value or '').strip()

def clean_html(value: str | None) -> str:
    if not value: return ''
    soup = BeautifulSoup(value, 'html.parser')
    for tag in soup(['script','style','noscript','iframe','svg']):
        tag.decompose()
    return normalize_space(soup.get_text(' ', strip=True))

def canonicalize_url(url: str | None) -> str:
    if not url: return ''
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in {'http','https'} or not parts.netloc: return ''
        host = parts.netloc.lower()
        path = parts.path or '/'
        query = [(k,v) for k,v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in TRACKING_KEYS]
        return urlunsplit((parts.scheme.lower(), host, path, urlencode(query, doseq=True), ''))
    except Exception:
        return ''

def slugify(value: str) -> str:
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value).strip('-')
    return value[:90] or 'topic'

def norm_name(value: str) -> str:
    value = unicodedata.normalize('NFKC', value or '').casefold()
    value = re.sub(r'[^\w\s-]', ' ', value, flags=re.UNICODE)
    return normalize_space(value).strip('-')

def hash_text(value: str) -> str:
    return hashlib.sha256(normalize_space(value).casefold().encode('utf-8')).hexdigest()

def fallback_entities(text: str) -> list[tuple[str,str,float]]:
    # Low-confidence fallback when spaCy/model is not installed; candidates require review.
    spans = re.findall(r'\b(?:[A-Z][a-zA-Z0-9&.-]+)(?:\s+(?:of|the|and|[A-Z][a-zA-Z0-9&.-]+)){0,3}', text or '')
    result, seen = [], set()
    for span in spans:
        name = normalize_space(span).strip(' .,-:')
        norm = norm_name(name)
        if not norm or norm in STOP_ENTITIES or len(name) < 3 or len(name) > 80 or norm in seen:
            continue
        seen.add(norm)
        result.append((name, 'UNKNOWN', 0.45))
    return result[:30]

_NLP = None
_NLP_ATTEMPTED = False

def extract_entities(text: str) -> list[tuple[str,str,float]]:
    global _NLP, _NLP_ATTEMPTED
    if not _NLP_ATTEMPTED:
        _NLP_ATTEMPTED = True
        try:
            import spacy
            _NLP = spacy.load('en_core_web_sm', exclude=['parser','lemmatizer'])
        except Exception:
            _NLP = None
    if _NLP is None:
        return fallback_entities(text)
    doc = _NLP((text or '')[:5000])
    mapped = {'PERSON':'PERSON','ORG':'ORG','GPE':'LOCATION','LOC':'LOCATION','FAC':'LOCATION','NORP':'GROUP','PRODUCT':'PRODUCT','EVENT':'EVENT','WORK_OF_ART':'WORK'}
    result, seen = [], set()
    for ent in doc.ents:
        name = normalize_space(ent.text).strip(' .,-:')
        norm = norm_name(name)
        if ent.label_ not in mapped or len(name) < 2 or norm in STOP_ENTITIES or norm in seen:
            continue
        seen.add(norm)
        result.append((name, mapped[ent.label_], 0.82 if ent.label_ in {'PERSON','ORG','GPE'} else 0.72))
    return result[:30]

def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    low = (text or '').casefold()
    found = []
    for kw in keywords or []:
        k = normalize_space(kw).casefold()
        if not k: continue
        if len(k) <= 3:
            hit = re.search(rf'(?<!\w){re.escape(k)}(?!\w)', low) is not None
        else:
            hit = k in low
        if hit: found.append(kw)
    return found

def apply_cleaning_rules(title: str, url: str, source_id: int, rules: list[dict]) -> tuple[bool,str|None]:
    for rule in rules:
        if not rule.get('enabled'): continue
        typ, cfg = rule.get('rule_type'), rule.get('config') or {}
        if typ == 'exclude_title_contains':
            needles = cfg.get('keywords', [])
            if isinstance(needles, str): needles = [needles]
            if any(str(k).casefold() in title.casefold() for k in needles if str(k).strip()):
                return True, f"Cleaning rule: {rule.get('name','exclude_title_contains')}"
        elif typ == 'exclude_url_contains':
            needles = cfg.get('keywords', [])
            if isinstance(needles, str): needles = [needles]
            if any(str(k).casefold() in url.casefold() for k in needles if str(k).strip()):
                return True, f"Cleaning rule: {rule.get('name','exclude_url_contains')}"
        elif typ == 'min_title_length':
            try: min_len = int(cfg.get('min_length', 10))
            except Exception: min_len = 10
            if len(title) < max(1,min_len): return True, f"Title shorter than {min_len} characters"
        elif typ == 'exclude_source':
            try: ids = {int(x) for x in cfg.get('source_ids', [])}
            except Exception: ids = set()
            if source_id in ids: return True, f"Cleaning rule: {rule.get('name','exclude_source')}"
    return False, None


# Conservative lexicon for high-confidence actor/action/target event topics.
# It is intentionally a small deterministic first version; uncertain headlines remain
# under broad category topics or become clustering candidates for admin review.
_EVENT_ENTITY_ALIASES = {
    'Mỹ': ['Hoa Kỳ', 'United States of America', 'United States', 'U.S.', 'USA', 'US', 'Mỹ'],
    'Iran': ['Iran'],
    'Italy': ['Italy', 'Italian', 'Ý'],
    'Đức': ['Đức', 'Germany', 'German'],
    'Pháp': ['Pháp', 'France', 'French'],
    'Anh': ['Vương quốc Anh', 'United Kingdom', 'Britain', 'British', 'UK', 'Anh'],
    'Nhật Bản': ['Nhật Bản', 'Japan', 'Japanese'],
    'Úc': ['Úc', 'Australia', 'Australian'],
    'Canada': ['Canada', 'Canadian'],
    'Mexico': ['Mexico', 'Mexican'],
    'Brazil': ['Brazil', 'Brazilian'],
    'Thổ Nhĩ Kỳ': ['Thổ Nhĩ Kỳ', 'Turkey', 'Turkish'],
    'Ả Rập Saudi': ['Ả Rập Saudi', 'Saudi Arabia', 'Saudi'],
    'Israel': ['Israel', 'Israeli'],
    'Ukraine': ['Ukraine', 'Ukraina'],
    'Nga': ['Russian Federation', 'Russia', 'Russian', 'Nga'],
    'Trung Quốc': ['People\'s Republic of China', 'Trung Quốc', 'China', 'Chinese'],
    'Đài Loan': ['Đài Loan', 'Taiwan'],
    'Triều Tiên': ['Bắc Triều Tiên', 'Triều Tiên', 'North Korea', 'North Korean'],
    'Hàn Quốc': ['Hàn Quốc', 'South Korea', 'South Korean'],
    'NATO': ['NATO'],
    'EU': ['Liên minh châu Âu', 'European Union', 'EU'],
    'Liên Hợp Quốc': ['Liên Hợp Quốc', 'United Nations', 'UN'],
    'Ấn Độ': ['Ấn Độ', 'India', 'Indian'],
    'Pakistan': ['Pakistan'],
    'Iraq': ['Iraq'],
    'Syria': ['Syria'],
    'Yemen': ['Yemen'],
    'Palestine': ['Palestine', 'Palestinian'],
    'Hamas': ['Hamas'],
    'Hezbollah': ['Hezbollah'],
    'OpenAI': ['OpenAI'],
    'Microsoft': ['Microsoft'],
    'Google': ['Google'],
    'Apple': ['Apple'],
    'Nvidia': ['NVIDIA', 'Nvidia'],
    'Meta': ['Meta Platforms', 'Meta'],
    'Samsung': ['Samsung'],
    'Tesla': ['Tesla'],
    'VinFast': ['VinFast'],
    'Viettel': ['Viettel'],
    'FPT': ['FPT'],
    'Vingroup': ['Vingroup'],
    'Mỹ Latinh': ['Mỹ Latinh', 'Latin America'],
    'Bộ Công an': ['Bộ Công an', 'Ministry of Public Security'],
    'Quốc hội': ['Quốc hội', 'National Assembly'],
    'Chính phủ': ['Chính phủ', 'Government of Vietnam', 'Vietnamese government'],
    'Thủ tướng': ['Thủ tướng', 'Prime Minister'],
    'Bộ Tài chính': ['Bộ Tài chính', 'Ministry of Finance'],
    'Ngân hàng Nhà nước': ['Ngân hàng Nhà nước', 'State Bank of Vietnam'],
    'Bộ Y tế': ['Bộ Y tế', 'Ministry of Health'],
    'Bộ Giáo dục': ['Bộ Giáo dục và Đào tạo', 'Bộ Giáo dục', 'Ministry of Education'],
    'EVN': ['EVN', 'Vietnam Electricity'],
}

_EVENT_ACTIONS = [
    # (regex, stable key, display verb, parent category slug)
    (r'tấn công bằng tên lửa|không kích|oanh kích|tập kích|tấn công|đánh(?!\s+giá)|air\s*strike|airstrike|strikes?|attacks?|bombing|bombs?', 'attack', 'tấn công', 'defence-security'),
    (r'tập trận|diễn tập|military drill|military exercise|exercise|drill', 'exercise', 'tập trận', 'defence-security'),
    (r'tấn công mạng|cyberattack|cyber attack|hack(?:s|ed|ing)?', 'cyberattack', 'tấn công mạng', 'technology'),
    (r'đe doạ tấn công|đe dọa tấn công|doạ tấn công|dọa tấn công|threatens? to attack', 'threaten_attack', 'đe dọa tấn công', 'defence-security'),
    (r'trừng phạt|sanctions?|sanctioned', 'sanction', 'trừng phạt', 'politics-policy'),
    (r'tăng thuế(?: quan)?|áp thuế|đánh thuế|thuế quan|tariffs?|impose duties|raises? tariffs?', 'tariff', 'áp thuế', 'economy-business'),
    (r'ký thỏa thuận|ký kết thỏa thuận|đạt thỏa thuận|đàm phán|hội đàm|thương lượng|negotiat(?:e|es|ed|ing)|talks with|meet(?:s|ing)? with', 'talks', 'đàm phán', 'politics-policy'),
    (r'ra mắt|trình làng|unveils?|launch(?:es|ed)?', 'launch', 'ra mắt', 'technology'),
    (r'mua lại|thâu tóm|acquires?|acquisition of', 'acquire', 'mua lại', 'economy-business'),
    (r'khởi tố', 'prosecute_start', 'khởi tố', 'politics-policy'),
    (r'truy tố|prosecut(?:e|es|ed)', 'prosecute', 'truy tố', 'politics-policy'),
    (r'xét xử|đưa ra xét xử|trial begins', 'trial', 'xét xử', 'politics-policy'),
    (r'thông qua|phê duyệt|phê chuẩn|approve(?:s|d)?|passes? (?:a |the )?(?:bill|law|plan)', 'approve', 'thông qua', 'politics-policy'),
    (r'ban hành|ban bố|issues? (?:a |the )?(?:rule|law|decree)', 'issue_policy', 'ban hành', 'politics-policy'),
    (r'yêu cầu|calls? on|urges?|orders?', 'request', 'yêu cầu', 'politics-policy'),
    (r'đề xuất|proposes?|proposed', 'propose', 'đề xuất', 'politics-policy'),
    (r'tăng giá|giảm giá|price rises?|price falls?|raises? prices?|cuts? prices?', 'price_change', 'điều chỉnh giá', 'economy-business'),
    (r'bắt giữ|bị bắt|arrests?|arrested|detains?|detained', 'arrest', 'bắt giữ', 'politics-policy'),
    (r'từ chức|resigns?|steps down', 'resign', 'từ chức', 'politics-policy'),
    (r'tăng lãi suất|hạ lãi suất|cuts? interest rates?|raises? interest rates?|rate hike', 'rate_change', 'điều chỉnh lãi suất', 'economy-business'),
]


def extract_event_signature(title: str) -> dict | None:
    """Return a specific event topic only when a recognized actor/action/target pattern is clear.

    Examples: “Mỹ đánh Iran” -> “Mỹ tấn công Iran”; “Iran bị Mỹ không kích” -> same event.
    Broad categories are deliberately not returned as event topics.
    """
    import re
    text = normalize_space(title or '')
    if not text:
        return None

    entity_hits = []
    aliases = []
    for canonical, values in _EVENT_ENTITY_ALIASES.items():
        for alias in values:
            aliases.append((canonical, alias))
    aliases.sort(key=lambda x: len(x[1]), reverse=True)
    for canonical, alias in aliases:
        pattern = re.compile(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', re.IGNORECASE | re.UNICODE)
        for match in pattern.finditer(text):
            entity_hits.append({'canonical': canonical, 'start': match.start(), 'end': match.end(), 'alias': alias})
    # Resolve overlaps (e.g. “United States” before “US”); preserve repeated mentions.
    entity_hits.sort(key=lambda x: (x['start'], -(x['end'] - x['start'])))
    non_overlapping = []
    for hit in entity_hits:
        if any(hit['start'] < old['end'] and hit['end'] > old['start'] for old in non_overlapping):
            continue
        non_overlapping.append(hit)
    entity_hits = sorted(non_overlapping, key=lambda x: x['start'])
    if not entity_hits:
        return None

    action_hits = []
    for pattern, key, label, parent_slug in _EVENT_ACTIONS:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.UNICODE):
            action_hits.append({'start': match.start(), 'end': match.end(), 'key': key, 'label': label, 'raw': match.group(0), 'parent_slug': parent_slug})
    action_hits.sort(key=lambda x: (x['start'], -(x['end'] - x['start'])))
    for action in action_hits:
        before = [e for e in entity_hits if e['end'] <= action['start']]
        after = [e for e in entity_hits if e['start'] >= action['end']]
        actor = target = None
        if before and after:
            actor, target = before[-1], after[0]
        elif len(before) >= 2:
            # Passive Vietnamese headline: “Iran bị Mỹ tấn công”.
            first, second = before[-2], before[-1]
            between = text[first['end']:second['start']].casefold()
            if re.search(r'\b(bị|được)\b', between):
                target, actor = first, second
        # If two recognized entities surround the action, create a relational event label.
        if actor and target and actor['canonical'] != target['canonical']:
            between_actor_action = text[actor['end']:action['start']].casefold()
            relationship_context = between_actor_action
            # Passive headlines can put negation/modality before the actor: “Iran không bị Mỹ tấn công”.
            if target['end'] < actor['start']:
                relationship_context = (text[target['end']:actor['start']] + ' ' + between_actor_action).casefold()
            action_key, action_label = action['key'], action['label']
            if action_key == 'attack' and re.search(r'\b(đe dọa|đe doạ|doạ|dọa|threatens?)\b', relationship_context):
                action_key, action_label = 'threaten_attack', 'đe dọa tấn công'
            elif action_key == 'attack' and re.search(r'\b(cân nhắc|chuẩn bị|sẵn sàng|có thể|consider(?:s|ed)?|prepare(?:s|d)?|plans? to)\b', relationship_context):
                action_key, action_label = 'prepare_attack', 'chuẩn bị tấn công'
            elif action_key == 'attack' and re.search(r"\b(không|chưa|never|not|did not|does not|do not|will not|won't|doesn't|didn't)\b", relationship_context):
                action_key, action_label = 'deny_attack', 'không tấn công'
            actor_name, target_name = actor['canonical'], target['canonical']
            label = f'{actor_name} {action_label} {target_name}'
            signature = f'{norm_name(actor_name)}|{action_key}|{norm_name(target_name)}'
            return {
                'event_key': hash_text(signature), 'name': label, 'actor': actor_name,
                'action': action_label, 'target': target_name,
                'keywords': list(dict.fromkeys([actor_name, target_name, action_label, action['raw']])),
                'parent_slug': action['parent_slug'], 'confidence': 0.82,
            }

        # Fallback for a clear actor + action + descriptive object phrase, e.g. “OpenAI ra mắt mô hình AI mới”.
        # This keeps event labels detailed even when the object is not a recognized named entity.
        if before and not after:
            actor_one = before[-1]
            tail = text[action['end']:]
            tail = re.split(r'[|–—:;.!?]', tail, maxsplit=1)[0]
            tail = normalize_space(tail).strip(' ,.-')
            # Drop location-only tails and generic non-events.
            tail = re.sub(r'^(?:tại|ở|trong|sau|vào|on|in|at)\s+', '', tail, flags=re.IGNORECASE)
            if len(tail) >= 4 and len(tail.split()) >= 1:
                action_key, action_label = action['key'], action['label']
                between_actor_action = text[actor_one['end']:action['start']].casefold()
                if action_key == 'attack' and re.search(r'\b(đe dọa|đe doạ|doạ|dọa|threatens?)\b', between_actor_action):
                    action_key, action_label = 'threaten_attack', 'đe dọa tấn công'
                elif action_key == 'attack' and re.search(r'\b(cân nhắc|chuẩn bị|sẵn sàng|có thể|consider(?:s|ed)?|prepare(?:s|d)?|plans? to)\b', between_actor_action):
                    action_key, action_label = 'prepare_attack', 'chuẩn bị tấn công'
                elif action_key == 'attack' and re.search(r"\b(không|chưa|never|not|did not|does not|do not|will not|won't|doesn't|didn't)\b", between_actor_action):
                    action_key, action_label = 'deny_attack', 'không tấn công'
                detail = tail[:70].rstrip(' ,.-')
                actor_name = actor_one['canonical']
                label = f'{actor_name} {action_label} {detail}'[:180]
                signature = f'{norm_name(actor_name)}|{action_key}|{norm_name(detail)}'
                return {
                    'event_key': hash_text(signature), 'name': label, 'actor': actor_name,
                    'action': action_label, 'target': detail,
                    'keywords': list(dict.fromkeys([actor_name, action_label, action['raw']] + detail.split()[:5])),
                    'parent_slug': action['parent_slug'], 'confidence': 0.72,
                }
    return None
