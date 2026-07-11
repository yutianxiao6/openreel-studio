"""Short-lived signatures for media URLs shared with external providers."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_MEDIA_URL_TTL_SECONDS = 15 * 60
MAX_MEDIA_URL_TTL_SECONDS = 60 * 60
_ALLOWED_PATH_PREFIXES = ("/api/media/", "/api/uploads/")


class MediaURLSigningError(ValueError):
    """Raised when a local media URL cannot be signed safely."""


def _signing_secret() -> bytes:
    secret = (
        os.getenv("DRAMA_MEDIA_URL_SIGNING_SECRET", "").strip()
        or os.getenv("AUTH_PASSWORD_HASH", "").strip()
    )
    if not secret:
        raise MediaURLSigningError(
            "公网媒体 URL 模式需要配置 DRAMA_MEDIA_URL_SIGNING_SECRET，"
            "也可复用生产 AUTH_PASSWORD_HASH。"
        )
    return secret.encode("utf-8")


def _canonical_payload(path: str, expires: int) -> bytes:
    return f"{expires}\n{path}".encode("utf-8")


def sign_media_url(
    local_url: str,
    *,
    now: int | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Append a short-lived signature to a local media or upload URL."""
    parsed = urlsplit(str(local_url or "").strip())
    path = parsed.path
    if parsed.scheme or parsed.netloc or not path.startswith(_ALLOWED_PATH_PREFIXES):
        raise MediaURLSigningError(f"不支持签名的媒体路径: {local_url}")
    ttl = int(ttl_seconds or os.getenv("DRAMA_MEDIA_URL_TTL_SECONDS", DEFAULT_MEDIA_URL_TTL_SECONDS))
    ttl = max(30, min(MAX_MEDIA_URL_TTL_SECONDS, ttl))
    expires = int(now if now is not None else time.time()) + ttl
    signature = hmac.new(
        _signing_secret(),
        _canonical_payload(path, expires),
        hashlib.sha256,
    ).hexdigest()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"expires": str(expires), "signature": signature})
    return urlunsplit(("", "", path, urlencode(query), parsed.fragment))


def verify_media_url_signature(
    path: str,
    expires: str | int | None,
    signature: str | None,
    *,
    now: int | None = None,
) -> bool:
    """Validate a signed local path without accepting stale bearer URLs."""
    if not str(path or "").startswith(_ALLOWED_PATH_PREFIXES):
        return False
    try:
        expires_at = int(expires or 0)
    except (TypeError, ValueError):
        return False
    current = int(now if now is not None else time.time())
    if expires_at < current or expires_at > current + MAX_MEDIA_URL_TTL_SECONDS:
        return False
    candidate = str(signature or "").strip()
    if not candidate:
        return False
    try:
        expected = hmac.new(
            _signing_secret(),
            _canonical_payload(path, expires_at),
            hashlib.sha256,
        ).hexdigest()
    except MediaURLSigningError:
        return False
    return hmac.compare_digest(candidate, expected)
