"""Attribution background jobs.

Queue: `bitrix` (consumed by separator-worker-misc on the Photon stack) - these
tasks talk to Bitrix, not to Meta.
"""

import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from celery import shared_task
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from separator.bitrix.crest import call_method
from separator.bitrix.models import AppInstance
from separator.bitrix.retry import RETRY_KWARGS

from .bitrix_fields import DEAL_FIELDS, build_deal_fields
from .models import ClickToken, DealAttribution, MessageEvent
from .tokens import extract_token, normalize_phone

logger = logging.getLogger("django")

# One claim per phone per day.
CLAIM_WINDOW_HOURS = 24
# A deal older than this cannot be the one this conversation created.
DEAL_FRESHNESS_MINUTES = 30
PUSH_MAX_RETRIES = 10
PUSH_BACKOFF_MAX = 600


def _ts_to_dt(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _result(response):
    if isinstance(response, dict):
        return response.get("result")
    return None


def _error(response):
    if isinstance(response, dict) and response.get("error"):
        return f"{response.get('error')}: {response.get('error_description')}"
    return None


@shared_task(queue="bitrix", **RETRY_KWARGS)
def resolve(phone, wamid, text, referral, ts, app_instance_id=None):
    """Decide where the first inbound message of a conversation came from."""
    phone = normalize_phone(phone)
    if not phone:
        return "no phone"

    first_inbound_at = _ts_to_dt(ts) or timezone.now()
    since = timezone.now() - timedelta(hours=CLAIM_WINDOW_HOURS)
    if DealAttribution.objects.filter(phone=phone, created_at__gte=since).exists():
        return "claim skipped: already attributed within 24h"

    source = DealAttribution.SOURCE_NONE
    status = DealAttribution.STATUS_CODE_MISSING
    token_obj = None
    note = None

    if isinstance(referral, dict) and referral:
        source = DealAttribution.SOURCE_META
        status = DealAttribution.STATUS_MATCHED
    else:
        code = extract_token(text)
        if code:
            token_obj = ClickToken.objects.filter(token=code).first()
            if token_obj is None:
                status = DealAttribution.STATUS_CODE_NOT_FOUND
            else:
                source = DealAttribution.SOURCE_SITE
                status = DealAttribution.STATUS_MATCHED
                if token_obj.claimed_at is None:
                    token_obj.claimed_at = timezone.now()
                    token_obj.phone = phone
                    token_obj.save(update_fields=["claimed_at", "phone"])
                elif token_obj.phone and token_obj.phone != phone:
                    # Shared link / forwarded message: the click data is still the
                    # best signal we have, but the original claim is not overwritten.
                    note = f"token {code} was already claimed by {token_obj.phone}"

    attribution = DealAttribution.objects.create(
        phone=phone,
        first_inbound_at=first_inbound_at,
        attribution_source=source,
        attribution_status=status,
        token=token_obj,
        referral=referral if isinstance(referral, dict) else None,
        error=note,
    )

    if app_instance_id:
        push_deal_attribution.delay(attribution.id, str(app_instance_id))
    return {"attribution_id": attribution.id, "source": source, "status": status}


def find_deal_id(app_instance, phone, not_before=None):
    """Newest deal of the contact with this phone.

    Returns (deal_id, is_fresh). `is_fresh` is False when the only deal found
    predates the conversation - the caller decides whether to keep waiting.
    """
    response = call_method(
        app_instance,
        "crm.duplicate.findbycomm",
        {"entity_type": "CONTACT", "type": "PHONE", "values": [phone]},
    )
    result = _result(response) or {}
    contact_ids = result.get("CONTACT") or []
    if isinstance(contact_ids, dict):
        contact_ids = list(contact_ids.values())
    if not contact_ids:
        return None, False

    best = None
    for contact_id in contact_ids:
        deals = call_method(
            app_instance,
            "crm.deal.list",
            {
                "filter": {"CONTACT_ID": contact_id},
                "order": {"ID": "DESC"},
                "select": ["ID", "DATE_CREATE"],
            },
        )
        for deal in _result(deals) or []:
            deal_id = str(deal.get("ID"))
            created = deal.get("DATE_CREATE")
            fresh = True
            if not_before and created:
                try:
                    fresh = datetime.fromisoformat(created) >= not_before
                except ValueError:
                    fresh = True
            if fresh:
                return deal_id, True
            if best is None:
                best = deal_id
    return best, False


def _existing_deal_fields(app_instance):
    """Field codes the portal actually has (cached), so unknown ones are dropped."""
    cache_key = f"attribution:deal_fields:{app_instance.id}"
    known = cache.get(cache_key)
    if known is None:
        response = call_method(app_instance, "crm.deal.fields", {})
        result = _result(response)
        known = list(result.keys()) if isinstance(result, dict) else []
        if known:
            cache.set(cache_key, known, 3600)
    return set(known or [])


PUSH_RETRY_KWARGS = {**RETRY_KWARGS, "max_retries": PUSH_MAX_RETRIES}


@shared_task(bind=True, queue="bitrix", **PUSH_RETRY_KWARGS)
def push_deal_attribution(self, attribution_id, app_instance_id):
    """Wait for Bitrix to create the deal, then write the attribution fields."""
    attribution = DealAttribution.objects.filter(id=attribution_id).first()
    if not attribution:
        return "attribution not found"
    if attribution.pushed_to_bitrix_at:
        return "already pushed"

    app_instance = AppInstance.objects.filter(id=app_instance_id).first()
    if not app_instance:
        return "app instance not found"

    attempt = self.request.retries or 0
    last_attempt = attempt >= PUSH_MAX_RETRIES
    not_before = None
    if attribution.first_inbound_at:
        not_before = attribution.first_inbound_at - timedelta(minutes=DEAL_FRESHNESS_MINUTES)

    deal_id, fresh = find_deal_id(app_instance, attribution.phone, not_before=not_before)

    if not deal_id or (not fresh and not last_attempt):
        if last_attempt:
            attribution.error = f"deal not found after {attempt + 1} attempts"
            attribution.save(update_fields=["error", "updated_at"])
            return "deal not found"
        raise self.retry(countdown=min(PUSH_BACKOFF_MAX, 30 * (2 ** attempt)))

    notes = [attribution.error] if attribution.error else []
    if not fresh:
        notes.append(f"fallback: no deal created after the first inbound, used newest deal {deal_id}")

    try:
        with transaction.atomic():
            attribution.deal_id = deal_id
            attribution.save(update_fields=["deal_id", "updated_at"])
    except IntegrityError:
        # Another conversation already owns this deal - keep our row unattached.
        attribution.refresh_from_db()
        attribution.error = "; ".join(notes + [f"deal {deal_id} already attributed"]) or None
        attribution.save(update_fields=["error", "updated_at"])
        return "deal already attributed"

    chat_response = call_method(
        app_instance,
        "imopenlines.crm.chat.getLastId",
        {"CRM_ENTITY_TYPE": "DEAL", "CRM_ENTITY": deal_id},
    )
    chat_ref = _result(chat_response)
    if chat_ref:
        attribution.chat_ref = str(chat_ref)

    fields = build_deal_fields(attribution)
    known = _existing_deal_fields(app_instance)
    if known:
        missing = [key for key in fields if key in DEAL_FIELDS and key not in known]
        if missing:
            notes.append(f"fields missing in portal: {', '.join(sorted(missing))}")
            fields = {key: value for key, value in fields.items() if key not in missing}

    if fields:
        response = call_method(
            app_instance, "crm.deal.update", {"id": deal_id, "fields": fields}
        )
        error = _error(response)
        if error:
            # An unknown field or a portal-side rejection must not kill the task.
            notes.append(f"crm.deal.update: {error}")
            logger.warning("attribution: crm.deal.update failed for deal %s: %s", deal_id, error)
        else:
            attribution.pushed_to_bitrix_at = timezone.now()

    attribution.error = "; ".join([note for note in notes if note]) or None
    attribution.save(update_fields=["chat_ref", "pushed_to_bitrix_at", "error", "updated_at"])

    events = MessageEvent.objects.filter(phone=attribution.phone, deal_id__isnull=True)
    if attribution.first_inbound_at:
        events = events.filter(created_at__gte=attribution.first_inbound_at - timedelta(hours=1))
    events.update(deal_id=deal_id)

    return {
        "deal_id": deal_id,
        "pushed": bool(attribution.pushed_to_bitrix_at),
        "error": attribution.error,
    }


def _author_type(app_instance, bitrix_user_id):
    """bot vs human_agent for an outbound open-line message.

    Bitrix does not label the author in the connector event, so the sender is
    resolved through user.get: USER_TYPE == 'bot' is Bitrix's own marker for
    chatbots (open-line welcome messages and bots run under such an account).
    An empty/zero user id is a system message, i.e. a bot as well.
    """
    if bitrix_user_id in (None, "", "0", 0):
        return MessageEvent.AUTHOR_BOT, None

    cache_key = f"attribution:user_type:{app_instance.id}:{bitrix_user_id}"
    user_type = cache.get(cache_key)
    if user_type is None:
        response = call_method(app_instance, "user.get", {"ID": bitrix_user_id})
        result = _result(response) or []
        user_type = (result[0].get("USER_TYPE") if result else "") or ""
        cache.set(cache_key, user_type, 24 * 3600)

    if user_type == "bot":
        return MessageEvent.AUTHOR_BOT, user_type
    if user_type in ("employee", "extranet"):
        return MessageEvent.AUTHOR_HUMAN, user_type
    return MessageEvent.AUTHOR_UNKNOWN, user_type


@shared_task(queue="bitrix", **RETRY_KWARGS)
def record_outbound(
    app_instance_id=None,
    phone=None,
    chat_ref=None,
    message_id=None,
    text_len=0,
    text_hash=None,
    bitrix_user_id=None,
    connector_code=None,
    line_id=None,
    event_ts=None,
):
    if not message_id or not phone:
        return "skipped"

    author_type = MessageEvent.AUTHOR_UNKNOWN
    user_type = None
    app_instance = AppInstance.objects.filter(id=app_instance_id).first() if app_instance_id else None
    if app_instance:
        try:
            author_type, user_type = _author_type(app_instance, bitrix_user_id)
        except Exception as exc:  # user.get is a nice-to-have, not a blocker
            logger.warning("attribution: user.get failed for %s: %s", bitrix_user_id, exc)

    MessageEvent.objects.get_or_create(
        message_id=message_id,
        defaults={
            "phone": phone,
            "chat_ref": chat_ref,
            # Bitrix sends `ts` with the event; fall back to receive time.
            "ts": _ts_to_dt(event_ts) or timezone.now(),
            "direction": MessageEvent.DIRECTION_OUT,
            "author_type": author_type,
            "text_len": text_len or 0,
            "text_hash": text_hash,
            "raw_meta": {
                "bitrix_user_id": bitrix_user_id,
                "bitrix_user_type": user_type,
                "connector": connector_code,
                "line": line_id,
            },
        },
    )
    return author_type
