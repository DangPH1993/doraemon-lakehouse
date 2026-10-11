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
