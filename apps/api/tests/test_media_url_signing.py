from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import routes_media, routes_uploads
from app.services.media_url_signing import (
    MediaURLSigningError,
    sign_media_url,
    verify_media_url_signature,
)


def test_signed_media_url_is_short_lived_and_path_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAMA_MEDIA_URL_SIGNING_SECRET", "test-only-secret")
    signed = sign_media_url(
        "/api/media/proj-1/generated_images/source.png",
        now=1_000,
        ttl_seconds=120,
    )
    parsed = urlsplit(signed)
    query = parse_qs(parsed.query)

    assert query["expires"] == ["1120"]
    assert verify_media_url_signature(
        parsed.path,
        query["expires"][0],
        query["signature"][0],
        now=1_100,
    ) is True
    assert verify_media_url_signature(
        "/api/media/proj-2/generated_images/source.png",
        query["expires"][0],
        query["signature"][0],
        now=1_100,
    ) is False
    assert verify_media_url_signature(
        parsed.path,
        query["expires"][0],
        query["signature"][0],
        now=1_121,
    ) is False


def test_media_url_signing_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRAMA_MEDIA_URL_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD_HASH", raising=False)

    with pytest.raises(MediaURLSigningError):
        sign_media_url("/api/uploads/proj-1/file/uploads/source.png", now=1_000)


def test_media_url_signing_rejects_non_media_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAMA_MEDIA_URL_SIGNING_SECRET", "test-only-secret")

    with pytest.raises(MediaURLSigningError):
        sign_media_url("/api/projects/proj-1", now=1_000)


def _request_for_signed_url(url: str) -> Request:
    parsed = urlsplit(url)
    return Request({
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": parsed.path,
        "query_string": parsed.query.encode("ascii"),
        "headers": [],
        "server": ("studio.example", 443),
    })


def test_media_routes_reject_invalid_public_bypass_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRAMA_MEDIA_URL_SIGNING_SECRET", "test-only-secret")
    request = _request_for_signed_url(
        "/api/media/proj-1/generated_images/source.png?expires=9999999999&signature=bad"
    )

    with pytest.raises(HTTPException) as exc_info:
        routes_media._validate_signature_query(request)

    assert exc_info.value.status_code == 403


def test_upload_routes_accept_valid_provider_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAMA_MEDIA_URL_SIGNING_SECRET", "test-only-secret")
    signed = sign_media_url(
        "/api/uploads/proj-1/file/uploads/source.png",
        ttl_seconds=120,
    )

    routes_uploads._validate_signature_query(_request_for_signed_url(signed))
