"""POST /api/waq/lead/ - the site hands over a fresh lead.

Auth: X-Photon-Timestamp (unix seconds) + X-Photon-Signature
("sha256=" + lowercase hex HMAC-SHA256 of f"{timestamp}.{raw_body}" with
settings.WAQ_HMAC_SECRET). Fail closed: no secret configured -> 503 for all.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import time
from datetime import datetime
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from separator.waq.models import WaqEvent, WaqLead, WaqSettings

logger = logging.getLogger("django")

MAX_SKEW_SECONDS = 300
MAX_BODY_BYTES = 32 * 1024
RATE_LIMIT_PER_MINUTE = 60
REQUIRED = ("deal_id", "phone", "wa_allowed", "created_at")

_MAX_LENGTHS = {
    field.name: field.max_length
    for field in WaqLead._meta.get_fields()
    if getattr(field, "max_length", None)
}


def _json(body, status):
    return JsonResponse(body, status=status)


# ------------------------------------------------------------------ rate limit
# Same rules as separator/attribution/api/views.py (60 per IP per minute; the
# proxy-seen address is used only when the peer is our own reverse proxy).


def _is_private(address):
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


def _client_ip(request):
    remote_addr = request.META.get("REMOTE_ADDR", "")
    if not _is_private(remote_addr):
        return remote_addr
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    return parts[-1] if parts else remote_addr


def _rate_limited(request):
    from django.core.cache import cache

    ip = _client_ip(request)
    if not ip:
        return False
    key = f"waq:rl:{ip}"
    try:
        if cache.add(key, 1, 60):
            return False
        return cache.incr(key) > RATE_LIMIT_PER_MINUTE
    except Exception as exc:
        logger.warning("waq: rate limit check failed: %s", exc)
        return False


# ------------------------------------------------------------------------ auth


def signature_for(secret, timestamp, raw_body):
    message = str(timestamp).encode("utf-8") + b"." + raw_body
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _signature_ok(request, secret):
    timestamp = (request.headers.get("X-Photon-Timestamp") or "").strip()
    provided = (request.headers.get("X-Photon-Signature") or "").strip()
    if not timestamp or not provided:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > MAX_SKEW_SECONDS:
        return False
    expected = signature_for(secret, timestamp, request.body)
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


# ------------------------------------------------------------------ validation


class Invalid(Exception):
    pass


def _text(value, field):
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    value = str(value).strip()
    limit = _MAX_LENGTHS.get(field)
    return value[:limit] if limit else value


def _deal_id(value):
    if isinstance(value, bool):
        raise Invalid("deal_id must be a positive integer")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise Invalid("deal_id must be a positive integer") from None
    if number <= 0:
        raise Invalid("deal_id must be a positive integer")
    return number


def _phone(value):
    import phonenumbers

    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise Invalid("phone is empty")
    try:
        parsed = phonenumbers.parse(f"+{digits}", None)
    except phonenumbers.NumberParseException:
        raise Invalid("phone is not a valid international number") from None
    if not phonenumbers.is_possible_number(parsed):
        raise Invalid("phone is not a valid international number")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _created_at(value):
    """ISO 8601 (naive = UTC) or unix seconds."""
    if isinstance(value, bool) or value in (None, ""):
        raise Invalid("created_at is required")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=dt_timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise Invalid("created_at is not a valid timestamp") from None
    text = str(value).strip()
    if text.isdigit():
        return _created_at(int(text))
    try:
        parsed = parse_datetime(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        parsed = None
    if parsed is None:
        raise Invalid("created_at must be ISO 8601 or unix seconds")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _bounded_payload(payload):
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_error": "payload not serializable"}
    if len(encoded.encode("utf-8")) > MAX_BODY_BYTES:
        return {"_truncated": True, "_keys": sorted(payload)[:100]}
    return payload


def _parse(payload):
    if not isinstance(payload, dict):
        raise Invalid("body must be a JSON object")
    missing = [field for field in REQUIRED if payload.get(field) in (None, "")]
    if missing:
        raise Invalid(f"missing required fields: {', '.join(missing)}")
    if not isinstance(payload["wa_allowed"], bool):
        raise Invalid("wa_allowed must be a JSON boolean")
    consent = payload.get("consent")
    preferred = _text(payload.get("preferred_channel"), "preferred_channel")
    return {
        "deal_id": _deal_id(payload["deal_id"]),
        "phone": _phone(payload["phone"]),
        "wa_allowed": payload["wa_allowed"],
        "lead_created_at": _created_at(payload["created_at"]),
        "name": _text(payload.get("name"), "name"),
        "project": _text(payload.get("project"), "project"),
        "page_url": _text(payload.get("page_url"), "page_url"),
        "site": _text(payload.get("site"), "site"),
        "lang": _text(payload.get("lang"), "lang"),
        "form_type": _text(payload.get("form_type"), "form_type"),
        "preferred_channel": preferred or None,
        "wa_reason": _text(payload.get("wa_reason"), "wa_reason"),
        "consent": consent if isinstance(consent, (dict, list)) else None,
        "payload": _bounded_payload(payload),
    }


# ------------------------------------------------------------------------ view


def _queue_confirmation(lead_id):
    from separator.waq import tasks

    try:
        # No publish retries: a broker outage must not hold the site's request.
        tasks.send_confirmation.apply_async(args=[lead_id], retry=False)
    except Exception as exc:
        # The lead stays `received`; nothing re-sends it automatically.
        logger.error("waq: could not queue confirmation for lead %s: %s", lead_id, exc)
        try:
            WaqEvent.objects.create(lead_id=lead_id, kind=WaqEvent.KIND_QUEUE_ERROR, data={"error": str(exc)[:500]})
        except Exception:
            pass


@csrf_exempt
@require_POST
def lead(request):
    secret = getattr(settings, "WAQ_HMAC_SECRET", "") or ""
    if not secret:
        return _json({"detail": "not configured"}, 503)
    if _rate_limited(request):
        return _json({"detail": "rate limited"}, 429)
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES:
            return _json({"detail": "payload too large"}, 413)
    except (TypeError, ValueError):
        pass
    if not _signature_ok(request, secret):
        return _json({"detail": "unauthorized"}, 401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _json({"detail": "invalid JSON"}, 400)
    try:
        values = _parse(payload)
    except Invalid as exc:
        return _json({"detail": str(exc)}, 400)

    if WaqLead.objects.filter(deal_id=values["deal_id"]).exists():
        return _json({"status": "duplicate"}, 202)

    blocked = not values["wa_allowed"]
    if blocked:
        values["state"] = WaqLead.STATE_BLOCKED
        values["block_reason"] = values["wa_reason"]
    elif not WaqSettings.load().enabled:
        # Manual stop at the moment of arrival: recorded only, never sent later.
        blocked = True
        values["state"] = WaqLead.STATE_BLOCKED
        values["block_reason"] = "manual_stop"
    try:
        with transaction.atomic():
            lead_obj = WaqLead.objects.create(**values)
    except IntegrityError:
        # The same deal arrived twice at the same moment.
        return _json({"status": "duplicate"}, 202)

    if blocked:
        return _json({"status": "blocked", "reason": values["block_reason"]}, 202)

    # ATOMIC_REQUESTS: queue only after the lead row is committed.
    transaction.on_commit(lambda: _queue_confirmation(lead_obj.id))
    return _json({"status": "queued"}, 202)
