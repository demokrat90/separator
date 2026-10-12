"""Entry points called from the WABA webhook flow (separator/waba/utils.py).

Hard rule: never break and never delay the path to Bitrix. Every function here
swallows its own exceptions, does only short database work and queues tasks
with 1 s broker timeouts and no publish retries. No HTTP call (Telegram, Graph,
Claude) is ever made from here: alerts are delivered by the waq worker.
"""

import logging
import re
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger("django")

REPLY_LOOKBACK_DAYS = 7
# Broker timeouts for the hook's own publish. The rest of the webhook path
# publishes with the global settings (5 s, with retries); the hook gets 1 s and
# no retries, so a sick Redis costs the Bitrix path at most a couple of seconds.
PUBLISH_TIMEOUT_SECONDS = 1


def publish(task, args):
    """Queue `task` with short broker timeouts and no publish retries."""
    app = task.app
    with app.connection_for_write(
        transport_options={
            "socket_timeout": PUBLISH_TIMEOUT_SECONDS,
            "socket_connect_timeout": PUBLISH_TIMEOUT_SECONDS,
            "retry_on_timeout": False,
            "max_retries": 0,
        }
    ) as connection:
        task.apply_async(args=args, retry=False, connection=connection)


def queue_alert(text):
    """Alert from the webhook path: queued to the waq worker, never sent here
    (a slow Telegram must not hold worker-waba). Never raises."""
    try:
        from . import tasks

        publish(tasks.deliver_alert, [text])
        return True
    except Exception as exc:
        logger.error("waq: alert could not be queued (%s): %s", type(exc).__name__, text[:300])
        return False


def message_time(message):
    """Meta's unix `timestamp` of a message as an aware datetime, or None."""
    try:
        from datetime import datetime
        from datetime import timezone as dt_timezone

        return datetime.fromtimestamp(int(message.get("timestamp")), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError, AttributeError):
        return None


def normalize_phone(value):
    """Meta `from` (digits) -> E.164 with '+'. None if there are no digits."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    return f"+{digits}" if digits else None


def slim_message(message):
    """What process_reply needs: id, type, the visible text and a button payload.

    Media captions, contacts and profile names are not passed on.
    """
    mtype = message.get("type")
    text = None
    payload = None
    if mtype == "text":
        text = (message.get("text") or {}).get("body")
    elif mtype == "button":
        button = message.get("button") or {}
        text = button.get("text")
        payload = button.get("payload")
    elif mtype == "interactive":
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            reply = interactive.get(key) or {}
            if reply.get("title") or reply.get("id"):
                text = reply.get("title")
                payload = reply.get("id")
                break
    return {
        "id": message.get("id"),
        "type": mtype,
        "timestamp": message.get("timestamp"),
        "text": text,
        "payload": payload,
    }


def handle_inbound_value(value, phone=None):
    """Queue process_reply for messages from numbers we are waiting on.

    `phone` is the business waba.Phone the webhook arrived on (may be None).
    Returns a list of short statuses for the log; never raises.
    """
    try:
        messages = value.get("messages") or []
        if not messages:
            return []
        from .models import WaqLead, WaqSettings

        cfg = WaqSettings.objects.filter(pk=WaqSettings.SINGLETON_PK).only("phone_id").first()
        if cfg and cfg.phone_id and phone is not None and phone.id != cfg.phone_id:
            # A reply to another business number: the bot could not answer from
            # our sender anyway (no open window there).
            return ["other sender"]

        from . import tasks

        since = timezone.now() - timedelta(days=REPLY_LOOKBACK_DAYS)
        handled = []
        for message in messages:
            try:
                customer = normalize_phone(message.get("from"))
                if not customer or not message.get("id"):
                    handled.append("no phone or id")
                    continue
                # A template was attempted within the window and the dialogue is
                # not closed (see WaqLead.awaiting_reply_q). A plain SELECT: the
                # hook never waits on the row lock the gate holds during a POST.
                sent_at = message_time(message)
                # Also a lead the scheduler marked no_answer AFTER this message
                # was sent (a late webhook delivery).
                late_but_in_time = (
                    Q(state=WaqLead.STATE_NO_ANSWER, no_answer_at__gt=sent_at) if sent_at else Q(pk__in=[])
                )
                lead = (
                    WaqLead.objects.filter(
                        WaqLead.awaiting_reply_q() | late_but_in_time,
                        Q(confirmation_attempted_at__gte=since)
                        | Q(reminder_attempted_at__gte=since)
                        | Q(confirmation_sent_at__gte=since),
                        phone=customer,
                    )
                    .order_by(
                        F("confirmation_attempted_at").desc(nulls_last=True),
                        F("confirmation_sent_at").desc(nulls_last=True),
                    )
                    .only("id")
                    .first()
                )
                if lead is None:
                    handled.append("not ours")
                    continue
                publish(tasks.process_reply, [lead.id, slim_message(message)])
                handled.append("queued")
            except Exception as exc:
                logger.error("waq: inbound message hook failed: %s", exc)
                handled.append("error")
        return handled
    except Exception as exc:
        logger.error("waq: handle_inbound_value failed: %s", exc)
        return ["error"]


def handle_quality_update(value):
    """phone_number_quality_update webhook: record + queue an alert. Never raises."""
    try:
        from .models import WaqEvent

        data = value if isinstance(value, dict) else {"raw": str(value)}
        WaqEvent.objects.create(kind=WaqEvent.KIND_QUALITY, data=data)
        parts = [f"{key}={data.get(key)}" for key in sorted(data) if data.get(key) not in (None, "")]
        queue_alert("phone quality update: " + ", ".join(parts))
        return True
    except Exception as exc:
        logger.error("waq: quality update hook failed: %s", exc)
        return False


def waq_template_names():
    from .models import WaqSettings

    cfg = WaqSettings.load()
    return {name.strip() for name in (cfg.template_confirmation, cfg.template_reminder) if name}


def handle_template_status(value):
    """message_template_status_update for one of our templates (any status other
    than APPROVED / PENDING_DELETION): record + queue an alert.

    Returns True if the event was ours and recorded, False otherwise.
    """
    try:
        name = (value or {}).get("message_template_name")
        if not name or name not in waq_template_names():
            return False
        from .models import WaqEvent

        WaqEvent.objects.create(kind=WaqEvent.KIND_TEMPLATE_STATUS, data=value)
        queue_alert(
            f"template {name} ({value.get('message_template_language')}): "
            f"{value.get('event')} reason={value.get('reason')}"
        )
        return True
    except Exception as exc:
        logger.error("waq: template status hook failed: %s", exc)
        return False
