"""
security.py — NO passlib. Uses bcrypt directly to avoid the passlib/bcrypt
version incompatibility (passlib 1.7.x + bcrypt 4.x breaks on 72-byte check).
"""
import hashlib
import bcrypt
from datetime import datetime, timedelta
from jose import jwt, JWTError
from cryptography.fernet import Fernet
from app.config import get_settings

settings = get_settings()
_fernet = Fernet(settings.encryption_key.encode())


# ── Password hashing (bcrypt direct — no passlib) ─────────────────────────────
def _prep(password: str) -> bytes:
    """SHA-256 pre-hash → fixed 64-char hex → safe for bcrypt (avoids 72-byte limit)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("utf-8")

def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prep(password), bcrypt.gensalt(rounds=12)).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prep(plain), hashed.encode("utf-8"))
    except Exception:
        return False


# ── JWT ───────────────────────────────────────────────────────────────────────
def create_access_token(data: dict) -> str:
    payload = {**data, "type": "access",
               "exp": datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)

def create_refresh_token(data: dict) -> str:
    payload = {**data, "type": "refresh",
               "exp": datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days)}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as e:
        raise ValueError(f"Invalid token: {e}")


# ── Fernet encryption (WhatsApp access tokens) ────────────────────────────────
def encrypt_token(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()

def decrypt_token(encrypted: str) -> str:
    return _fernet.decrypt(encrypted.encode()).decode()