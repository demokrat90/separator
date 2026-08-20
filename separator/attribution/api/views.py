"""Public API for the sites: hand out a click code, report health.

Auth is a single static key in the `X-Attr-Key` header (settings.ATTR_API_KEY).
Fail closed: with no key configured, every call is rejected.
"""

import hmac
import ipaddress
import json
import logging

from django.conf import settings
from django.db import IntegrityError
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from separator.attribution.models import SCHEMA_VERSION, ClickToken
from separator.attribution.tokens import (
    build_wa_text_with_token,
    generate_token,
    normalize_ga_client_id,
)

logger = logging.getLogger("django")

RATE_LIMIT_PER_MINUTE = 60
TOKEN_MAX_ATTEMPTS = 5

STRING_FIELDS = (
    "site",
    "landing_url",
    "referrer",
    "listing_id",
    "gclid",
    "gbraid",
    "wbraid",
    "yclid",
    "fbclid",
    "fbp",
    "fbc",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "ym_client_id",
)


def _client_ip(request):
    """IP for rate limiting.

    X-Forwarded-For is only consulted when the connection itself came from a
    private address, i.e. through our own reverse proxy (Caddy on the docker
    network). Of that header the LAST entry is used - the address the proxy
    actually saw; earlier entries are client supplied and can be forged.
    A request that reaches the app directly is rate limited by its real peer.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "")
    if not _is_private(remote_addr):
        return remote_addr
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    return parts[-1] if parts else remote_addr


def _is_private(address):
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


def _rate_limited(request):
    """60 requests per IP per minute. A broken cache must not lock the API out."""
    from django.core.cache import cache

    ip = _client_ip(request)
    if not ip:
        return False
    key = f"attribution:rl:{ip}"
    try:
        added = cache.add(key, 1, 60)
        if added:
            return False
        count = cache.incr(key)
        return count > RATE_LIMIT_PER_MINUTE
    except Exception as exc:
        logger.warning("attribution: rate limit check failed: %s", exc)
        return False


def _authorized(request):
    expected = getattr(settings, "ATTR_API_KEY", "") or ""
    provided = request.headers.get("X-Attr-Key", "") or ""
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(expected), str(provided))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "ok", "schema_version": SCHEMA_VERSION})


MAX_BODY_BYTES = 32 * 1024
MAX_RAW_BYTES = 32 * 1024
TRUE_VALUES = {"1", "true", "yes", "on"}

_MAX_LENGTHS = {
    field.name: field.max_length
    for field in ClickToken._meta.get_fields()
    if getattr(field, "max_length", None)
}


def _clean(value, field):
    """One scalar field: text only, trimmed to what the column can hold."""
    if value is None or isinstance(value, (dict, list)):
        return None
    value = str(value).strip()
    if not value:
        return None
    limit = _MAX_LENGTHS.get(field)
    return value[:limit] if limit else value


def _as_bool(value):
    """Strict: only an explicit truthy value is True, anything else is False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    return str(value).strip().lower() in TRUE_VALUES


def _bounded_raw(payload):
    """The site's payload is kept verbatim, but it may not grow unbounded."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_error": "payload not serializable"}
    if len(encoded.encode("utf-8")) > MAX_RAW_BYTES:
        return {"_truncated": True, "_keys": sorted(payload)[:100]}
    return payload


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def click(request):
    if not _authorized(request):
        return Response({"detail": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    if _rate_limited(request):
        return Response({"detail": "rate limited"}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES:
            return Response(
                {"detail": "payload too large"},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
    except (TypeError, ValueError):
        pass

    payload = request.data if isinstance(request.data, dict) else {}

    values = {field: _clean(payload.get(field), field) for field in STRING_FIELDS}
    values["ga_client_id"] = _clean(
        normalize_ga_client_id(payload.get("ga_client_id")), "ga_client_id"
    )
    values["is_test"] = _as_bool(payload.get("is_test", False))
    values["raw"] = _bounded_raw(payload)
    values["schema_version"] = SCHEMA_VERSION

    token = None
    for _attempt in range(TOKEN_MAX_ATTEMPTS):
        try:
            token = ClickToken.objects.create(token=generate_token(), **values)
            break
        except IntegrityError:
            continue
    if token is None:
        return Response(
            {"detail": "could not allocate token"}, status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # `listing` (a human readable title) is optional; listing_id is the fallback,
    # and `brand` lets a non-Photon site greet with its own name.
    listing = _clean(payload.get("listing"), "listing_id") or token.listing_id
    brand = _clean(payload.get("brand"), "site") or "Photon"
    return Response(
        {
            "token": token.token,
            "wa_text": build_wa_text_with_token(token.token, listing, brand=brand),
        },
        status=status.HTTP_201_CREATED,
    )
