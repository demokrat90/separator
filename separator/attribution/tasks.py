"""Attribution background jobs.

Queue: `bitrix` (consumed by separator-worker-misc on the Photon stack) - these
tasks talk to Bitrix, not to Meta.

`resolve` receives the already extracted click code, never the message text:
Celery arguments live on in the broker and in the result metadata.
"""

import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from celery import shared_task
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from separator.bitrix.crest import call_method
from separator.bitrix.models import AppInstance
from separator.bitrix.retry import RETRY_KWARGS

from .bitrix_fields import DEAL_FIELDS, build_deal_fields
from .models import ClickToken, DealAttribution, MessageEvent
from .tokens import normalize_phone

logger = logging.getLogger("django")

# One claim per phone per day.
CLAIM_WINDOW_HOURS = 24
# A deal created before this margin cannot be the one this conversation started.
DEAL_FRESHNESS_MINUTES = 30
PUSH_MAX_RETRIES = 10
PUSH_BACKOFF_MAX = 600
# The sweeper only looks at recent work; older rows are a manual matter.
SWEEP_MAX_AGE_HOURS = 72
SWEEP_MIN_AGE_MINUTES = 15


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


def _lock_phone(phone):
    """Serialize concurrent first messages of one number across workers."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"attr:{phone}"])
    except Exception as exc:  # non-Postgres or permission issue: not fatal
        logger.warning("attribution: advisory lock unavailable: %s", exc)


@shared_task(queue="bitrix", **RETRY_KWARGS)
def resolve(phone, wamid, code, referral, ts, app_instance_id=None):
    """Decide where the first inbound message of a conversation came from.

    `code` is the click code already extracted from the message by the hook.
    """
    phone = normalize_phone(phone)
    if not phone:
        return "no phone"

    first_inbound_at = _ts_to_dt(ts) or timezone.now()

    with transaction.atomic():
        _lock_phone(phone)

        since = timezone.now() - timedelta(hours=CLAIM_WINDOW_HOURS)
        recent = DealAttribution.objects.filter(phone=phone, created_at__gte=since)
        if app_instance_id:
            recent = recent.filter(app_instance_id=app_instance_id)
        if recent.exists():
            return "claim skipped: already attributed within 24h"

        source = DealAttribution.SOURCE_NONE
        status = DealAttribution.STATUS_CODE_MISSING
        token_obj = None
        note = None

        if isinstance(referral, dict) and referral:
            source = DealAttribution.SOURCE_META
            status = DealAttribution.STATUS_MATCHED
        elif code:
            token_obj = ClickToken.objects.filter(token=code).first()
            if token_obj is None:
                status = DealAttribution.STATUS_CODE_NOT_FOUND
            else:
                source = DealAttribution.SOURCE_SITE
                status = DealAttribution.STATUS_MATCHED
                # Conditional update: whoever claims first wins, no lost update.
                claimed = ClickToken.objects.filter(pk=token_obj.pk, claimed_at__isnull=True).update(
                    claimed_at=timezone.now(), phone=phone
                )
                if not claimed:
                    token_obj.refresh_from_db()
                    if token_obj.phone and token_obj.phone != phone:
                        # Shared link / forwarded message: the click data is still
                        # the best signal, but the original claim is kept.
                        note = f"token {code} was already claimed by {token_obj.phone}"

        attribution = DealAttribution.objects.create(
            phone=phone,
            app_instance_id=app_instance_id,
            first_inbound_at=first_inbound_at,
            attribution_source=source,
            attribution_status=status,
            token=token_obj,
            referral=referral if isinstance(referral, dict) else None,
            error=note,
        )

    if app_instance_id:
        try:
            push_deal_attribution.delay(attribution.id, str(app_instance_id))
        except Exception as exc:
            # pushed_to_bitrix_at stays empty; the sweeper retries later.
            logger.error("attribution: could not queue push for %s: %s", attribution.id, exc)

    return {"attribution_id": attribution.id, "source": source, "status": status}


def find_deal_id(app_instance, phone, not_before=None):
    """Newest deal among all contacts holding this phone.

    Returns (deal_id, is_fresh). `is_fresh` is False when every candidate was
    created before the conversation started - such a deal is never written to.
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

    candidates = []
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
            created = None
            if deal.get("DATE_CREATE"):
                try:
                    created = datetime.fromisoformat(deal["DATE_CREATE"])
                except (TypeError, ValueError):
                    created = None
            candidates.append((deal_id, created))

    if not candidates:
        return None, False

    def sort_key(item):
        deal_id, created = item
        return (
            created.timestamp() if created else 0,
            int(deal_id) if deal_id.isdigit() else 0,
        )

    candidates.sort(key=sort_key, reverse=True)
    newest_id, newest_created = candidates[0]

    if not not_before:
        return newest_id, True
    for deal_id, created in candidates:
        # A deal with no parseable DATE_CREATE is treated as fresh rather than
        # blocking attribution forever.
        if created is None or created >= not_before:
            return deal_id, True
    return newest_id, False


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

    if not deal_id or not fresh:
        # Never write into a deal that existed before this conversation: that is
        # somebody else's deal. Keep waiting instead, then give up loudly.
        if last_attempt:
            reason = "no deal created for this conversation" if deal_id else "contact/deal not found"
            attribution.error = f"{reason} after {attempt + 1} attempts"
            attribution.save(update_fields=["error", "updated_at"])
            logger.warning(
                "attribution: %s for %s (attribution %s)",
                reason, attribution.phone, attribution.id,
            )
            return "deal not found"
        raise self.retry(countdown=min(PUSH_BACKOFF_MAX, 30 * (2 ** attempt)))

    notes = [attribution.error] if attribution.error else []

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
    else:
        notes.append("crm.deal.fields unavailable: field list not verified")

    if fields:
        response = call_method(
            app_instance, "crm.deal.update", {"id": deal_id, "fields": fields}
        )
        error = _error(response)
        if error:
            # An unknown field or a portal-side rejection must not kill the task.
            notes.append(f"crm.deal.update: {error}")
            logger.warning("attribution: crm.deal.update failed for deal %s: %s", deal_id, error)
            # The cached field list may be stale - re-read it next time.
            cache.delete(f"attribution:deal_fields:{app_instance.id}")
        else:
            attribution.pushed_to_bitrix_at = timezone.now()

    attribution.error = "; ".join([note for note in notes if note]) or None
    attribution.save(update_fields=["chat_ref", "pushed_to_bitrix_at", "error", "updated_at"])
    _link_messages_to_deal(attribution, deal_id)

    return {
        "deal_id": deal_id,
        "pushed": bool(attribution.pushed_to_bitrix_at),
        "error": attribution.error,
    }


def _link_messages_to_deal(attribution, deal_id):
    """Stamp the deal on the messages of this conversation (by payload time)."""
    events = MessageEvent.objects.filter(phone=attribution.phone, deal_id__isnull=True)
    if attribution.app_instance_id:
        events = events.filter(
            Q(app_instance_id=attribution.app_instance_id) | Q(app_instance_id__isnull=True)
        )
    if attribution.first_inbound_at:
        start = attribution.first_inbound_at - timedelta(hours=1)
        end = attribution.first_inbound_at + timedelta(days=30)
        events = events.filter(
            Q(ts__gte=start, ts__lte=end) | Q(ts__isnull=True, created_at__gte=start, created_at__lte=end)
        )
    events.update(deal_id=deal_id)


@shared_task(queue="bitrix", **RETRY_KWARGS)
def sweep_pending_attributions():
    """Recover work whose follow-up task was never queued (broker hiccup, crash).

    Two gaps are covered: an inbound message recorded without its resolve task,
    and an attribution row that never reached Bitrix.
    """
    now = timezone.now()
    oldest = now - timedelta(hours=SWEEP_MAX_AGE_HOURS)
    newest = now - timedelta(minutes=SWEEP_MIN_AGE_MINUTES)
    requeued = {"resolve": 0, "push": 0}

    pending_events = MessageEvent.objects.filter(
        direction=MessageEvent.DIRECTION_IN,
        created_at__gte=oldest,
        created_at__lte=newest,
        raw_meta__resolve_pending=True,
    )[:200]
    for event in pending_events:
        raw_meta = event.raw_meta or {}
        resolve.delay(
            event.phone,
            event.message_id,
            raw_meta.get("click_code"),
            event.referral,
            int(event.ts.timestamp()) if event.ts else None,
            app_instance_id=event.app_instance_id,
        )
        MessageEvent.objects.filter(pk=event.pk).update(
            raw_meta={**raw_meta, "resolve_pending": False}
        )
        requeued["resolve"] += 1

    unpushed = DealAttribution.objects.filter(
        pushed_to_bitrix_at__isnull=True,
        app_instance_id__isnull=False,
        created_at__gte=oldest,
        created_at__lte=newest,
    )[:200]
    for attribution in unpushed:
        push_deal_attribution.delay(attribution.id, attribution.app_instance_id)
        requeued["push"] += 1

    return requeued


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
            "app_instance_id": str(app_instance_id) if app_instance_id else None,
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
