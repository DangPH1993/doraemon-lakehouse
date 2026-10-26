import base64, hashlib, hmac, json, secrets, time
from datetime import datetime, timezone
from fastapi import Cookie, Header, HTTPException
from .config import settings

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')

def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))

def issue_token(username: str, lifetime_seconds: int = 8 * 60 * 60) -> str:
    if not settings.session_secret:
        raise RuntimeError('SESSION_SECRET is not configured')
    body = _b64(json.dumps({'sub': username, 'exp': int(time.time()) + lifetime_seconds}, separators=(',', ':')).encode())
    sig = _b64(hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).digest())
    return f'{body}.{sig}'

def valid_token(token: str) -> bool:
    try:
        body, sig = token.split('.', 1)
        expected = _b64(hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            return False
        claims = json.loads(_unb64(body))
        return claims.get('sub') == settings.admin_username and int(claims.get('exp', 0)) > int(time.time())
    except Exception:
        return False

async def require_admin(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Admin login required')
    if not valid_token(authorization[7:].strip()):
        raise HTTPException(status_code=401, detail='Session expired. Please log in again.')
    return True


# Separate user sessions from the existing administrator token format.
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    rounds = 310_000
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, rounds)
    return f"pbkdf2_sha256${rounds}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds_raw, salt_raw, digest_raw = stored.split('$', 3)
        if scheme != 'pbkdf2_sha256':
            return False
        rounds = int(rounds_raw)
        if rounds < 100_000 or rounds > 2_000_000:
            return False
        salt = _unb64(salt_raw)
        expected = _unb64(digest_raw)
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def issue_user_token(user_id: int, username: str, lifetime_seconds: int = 7 * 24 * 60 * 60) -> str:
    if not settings.session_secret:
        raise RuntimeError('SESSION_SECRET is not configured')
    body = _b64(json.dumps({
        'sub': str(user_id), 'username': username, 'typ': 'news-user',
        'exp': int(time.time()) + lifetime_seconds,
    }, separators=(',', ':')).encode())
    sig = _b64(hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).digest())
    return f'{body}.{sig}'


def read_user_token(token: str) -> dict | None:
    try:
        body, sig = token.split('.', 1)
        expected = _b64(hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            return None
        claims = json.loads(_unb64(body))
        if claims.get('typ') != 'news-user' or int(claims.get('exp', 0)) <= int(time.time()):
            return None
        claims['sub'] = int(claims['sub'])
        return claims
    except Exception:
        return None


async def require_user(
    authorization: str | None = Header(default=None),
    news_radar_session: str | None = Cookie(default=None),
):
    token = None
    if authorization and authorization.startswith('Bearer '):
        token = authorization[7:].strip()
    elif news_radar_session:
        token = news_radar_session
    claims = read_user_token(token) if token else None
    if not claims:
        raise HTTPException(status_code=401, detail='Vui lòng đăng nhập để sử dụng chức năng này.')
    from .db import fetch_one
    user = fetch_one(
        'SELECT user_id, username, email, plan, plan_expires_at FROM app_users WHERE user_id=%s AND is_active=true',
        (claims['sub'],),
    )
    if not user:
        raise HTTPException(status_code=401, detail='Tài khoản không tồn tại hoặc đã bị khóa.')
    # Enforce expiry immediately for this account without writing on every request.
    # The hourly GitHub Actions workflow handles accounts that are not active.
    expires_at = user.get('plan_expires_at')
    if user.get('plan') == 'Pro' and expires_at is not None and expires_at <= datetime.now(timezone.utc):
        from .subscriptions import expire_pro_plans
        expire_pro_plans(user_id=claims['sub'])
        user['plan'] = 'Free'
        user['plan_expires_at'] = None
    return user
