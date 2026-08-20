"""Entry points called from the live message flow.

Hard rule for everything here: attribution must never break message delivery.
Every public function swallows its own exceptions and returns a short status
string for the log; callers are not expected to check it.

Message text never leaves this module: the click code is extracted here, and
only the code, the length and the sha256 hash travel further (a Celery argument
is persisted in the broker and in the result metadata, so passing the text
itself would store it).
"""

import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.utils import timezone

from .models import DealAttribution, MessageEvent, StatusEvent, current_deal_id
from .tokens import extract_token, normalize_phone, text_stats

logger = logging.getLogger("django")

# A new inbound message starts a new attribution only if the number has not been
# attributed within this window.
FIRST_INBOUND_WINDOW_DAYS = 30


def _dt(value):
    """Meta timestamps are epoch seconds as a string."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def message_text(message):
    """Best-effort text of an inbound message (used for hash + code search)."""
    if not isinstance(message, dict):
        return None
    mtype = message.get("type")
    if mtype == "text":
        return (message.get("text") or {}).get("body")
    if mtype == "button":
        return (message.get("button") or {}).get("text")
    if mtype in ("image", "video", "audio", "document", "sticker"):
        return (message.get(mtype) or {}).get("caption")
    if mtype == "interactive":
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            reply = interactive.get(key) or {}
            if reply.get("title"):
                return reply.get("title")
        return None
    if mtype == "order":
        return (message.get("order") or {}).get("text")
    if mtype == "system":
        return (message.get("system") or {}).get("body")
    return None


def _customer_phone(value, message):
    contacts = value.get("contacts") or []
    wa_id = None
    if contacts:
        wa_id = contacts[0].get("wa_id") or contacts[0].get("user_id")
    return normalize_phone(wa_id or message.get("from"))


def _should_start_attribution(phone, app_instance_id):
    since = timezone.now() - timedelta(days=FIRST_INBOUND_WINDOW_DAYS)
    existing = DealAttribution.objects.filter(phone=phone, created_at__gte=since)
    if app_instance_id:
        existing = existing.filter(app_instance_id=app_instance_id)
    return not existing.exists()


def handle_inbound_value(value, app_instance_id=None, waba_phone_id=None):
    """Record inbound messages + delivery statuses from one webhook `value`."""
    try:
        from . import tasks

        handled = []
        for message in value.get("messages") or []:
            try:
                handled.append(
                    _record_inbound_message(value, message, app_instance_id, waba_phone_id, tasks)
                )
            except Exception as exc:  # never break the message flow
                logger.warning("attribution: inbound record failed: %s", exc)
        for item in value.get("statuses") or []:
            try:
                _record_status(item)
            except Exception as exc:
                logger.warning("attribution: status record failed: %s", exc)
        return handled
    except Exception as exc:
        logger.warning("attribution: handle_inbound_value failed: %s", exc)
        return []


def _record_inbound_message(value, message, app_instance_id, waba_phone_id, tasks):
    message_id = message.get("id")
    if not message_id:
        return "no message id"

    phone = _customer_phone(value, message)
    if not phone:
        return "no phone"

    text = message_text(message)
    text_len, text_hash = text_stats(text)
    code = extract_token(text)
    referral = message.get("referral") if isinstance(message.get("referral"), dict) else None
    ts = _dt(message.get("timestamp"))
    start_attribution = _should_start_attribution(phone, app_instance_id)

    event, created = MessageEvent.objects.get_or_create(
        message_id=message_id,
        defaults={
            "phone": phone,
            "app_instance_id": app_instance_id,
            "ts": ts,
            "direction": MessageEvent.DIRECTION_IN,
            "author_type": MessageEvent.AUTHOR_CUSTOMER,
            "reply_to_id": (message.get("context") or {}).get("id"),
            "text_len": text_len,
            "text_hash": text_hash,
            "referral": referral,
            "deal_id": current_deal_id(phone, app_instance_id),
            "raw_meta": {
                "type": message.get("type"),
                "waba_phone_id": waba_phone_id,
                # Our own code, not customer text: keeps resolve() replayable
                # from the database if the queue call below is lost.
                "click_code": code,
                "resolve_pending": start_attribution,
            },
        },
    )
    if not created:
        # Duplicate wamid: Meta re-delivery or a Celery retry of event_processing.
        return "duplicate"

    if not start_attribution:
        return "recorded"

    try:
        tasks.resolve.delay(
            phone,
            message_id,
            code,
            referral,
            message.get("timestamp"),
            app_instance_id=app_instance_id,
        )
    except Exception as exc:
        # The event row keeps resolve_pending=True; the sweeper picks it up.
        logger.error("attribution: could not queue resolve for %s: %s", message_id, exc)
        return "resolve not queued"

    MessageEvent.objects.filter(pk=event.pk).update(
        raw_meta={**(event.raw_meta or {}), "resolve_pending": False}
    )
    return "resolve queued"


def _record_status(item):
    message_id = item.get("id")
    status = item.get("status")
    if not message_id or not status:
        return "skipped"

    conversation = item.get("conversation") if isinstance(item.get("conversation"), dict) else None
    origin = (conversation or {}).get("origin") or {}
    StatusEvent.objects.get_or_create(
        message_id=message_id,
        status=status,
        defaults={
            "ts": _dt(item.get("timestamp")),
            "recipient_id": normalize_phone(item.get("recipient_id")),
            "conversation_id": (conversation or {}).get("id"),
            "conversation_origin_type": origin.get("type"),
            "conversation_expiration_at": _dt((conversation or {}).get("expiration_timestamp")),
            "conversation": conversation,
            "pricing": item.get("pricing") if isinstance(item.get("pricing"), dict) else None,
            "errors": item.get("errors"),
            "raw": item,
        },
    )
    return "recorded"


def handle_outbound_bitrix_message(
    app_instance_id=None,
    connector_code=None,
    line_id=None,
    chat=None,
    chat_id=None,
    message_id=None,
    text=None,
    bitrix_user_id=None,
    event_ts=None,
):
    """Bitrix operator (or bot) sent a message into the open line -> WhatsApp."""
    try:
        from . import tasks

        if not message_id:
            return "no message id"
        text_len, text_hash = text_stats(text)
        tasks.record_outbound.delay(
            app_instance_id=app_instance_id,
            phone=normalize_phone(chat),
            chat_ref=str(chat_id) if chat_id else None,
            # The wamid is only known after Graph answers; until then the row is
            # keyed by the Bitrix message id (see handle_outbound_sent).
            bitrix_message_id=str(message_id),
            text_len=text_len,
            text_hash=text_hash,
            bitrix_user_id=str(bitrix_user_id) if bitrix_user_id not in (None, "") else None,
            connector_code=connector_code,
            line_id=str(line_id) if line_id else None,
            event_ts=event_ts,
        )
        return "queued"
    except Exception as exc:
        logger.warning("attribution: outbound hook failed: %s", exc)
        return "failed"


def wamid_from_send_result(send_result):
    """`messages[0].id` of a Cloud API send response, or None on an error reply."""
    if not isinstance(send_result, dict):
        return None
    messages = send_result.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        return messages[0].get("id")
    return None


def handle_outbound_sent(
    app_instance_id=None, bitrix_message_id=None, chat=None, send_result=None, index=0
):
    """Graph accepted the outbound message: attach its wamid to our row.

    Without this the delivery statuses (which are keyed by wamid) have nothing
    to join to on the outbound side.
    """
    try:
        from . import tasks

        wamid = wamid_from_send_result(send_result)
        if not wamid or not bitrix_message_id:
            return "no wamid"
        tasks.attach_wamid.delay(
            app_instance_id=str(app_instance_id) if app_instance_id else None,
            bitrix_message_id=str(bitrix_message_id),
            wamid=wamid,
            phone=normalize_phone(chat),
            index=index,
        )
        return "queued"
    except Exception as exc:
        logger.warning("attribution: wamid hook failed: %s", exc)
        return "failed"
