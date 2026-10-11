import base64, hashlib, hmac, json, time
from fastapi import Header, HTTPException
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
