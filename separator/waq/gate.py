"""The single "may we send?" check, run right before every template request to Meta.

Everything happens in one transaction:
  1. lock WaqSettings (pk=1) - serializes every template send, so the caps are
     counted without races;
  2. lock the lead and re-check its state - a re-delivered or duplicated task
     can never send a second template;
  3. lock the customer's WaqPhone row - dedup (30 days) and the stop list.
Lock order is always settings -> lead -> phone (process_reply takes lead -> phone
and touches settings only in a separate transaction), so there is no cycle.

The Graph request itself is made inside the transaction, as the spec requires:
while one template is in flight nobody else can pass the gate.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import alerts
from .models import WaqEvent, WaqLead, WaqPhone, WaqSettings

logger = logging.getLogger("django")

KIND_CONFIRMATION = "confirmation"
KIND_REMINDER = "reminder"

DEDUP_DAYS = 30
CAP_ALERT_EVERY = timedelta(hours=1)

# Refusal reasons, in the order they are checked.
REASON_MANUAL_STOP = "manual_stop"
REASON_AUTOSTOP = "autostop"
REASON_STOPLIST = "stoplist"
REASON_CONTACTED_30D = "contacted_30d"
REASON_CAP = "cap"
REASON_MISCONFIGURED = "misconfigured"


@dataclass
class GateResult:
    status: str  # "sent", "failed", "skipped" or one of the REASON_* values
    detail: str = ""

    @property
    def sent(self):
        return self.status == "sent"


def _expected_state(lead, kind):
    if kind == KIND_CONFIRMATION:
        return lead.state == WaqLead.STATE_RECEIVED and lead.confirmation_sent_at is None
    return (
        lead.state == WaqLead.STATE_CONFIRMATION_SENT
        and lead.reminder_sent_at is None
        and lead.replied_at is None
    )


def templates_sent_since(since):
    """Templates of both kinds actually sent since `since` (by *_sent_at)."""
    confirmations = WaqLead.objects.filter(confirmation_sent_at__gte=since).count()
    reminders = WaqLead.objects.filter(reminder_sent_at__gte=since).count()
    return confirmations + reminders


def _cap_alert_due(now):
    return not WaqEvent.objects.filter(
        kind=WaqEvent.KIND_CAP, created_at__gte=now - CAP_ALERT_EVERY, data__alerted=True
    ).exists()


def _misconfiguration(cfg, kind):
    """Why the sender cannot be used, or '' if it can."""
    phone = cfg.phone
    if phone is None:
        return "no sender phone"
    if not phone.phone_id or not phone.waba_id:
        return "sender phone has no phone_id or waba"
    if phone.date_end and timezone.now() > phone.date_end:
        return "sender phone tariff expired"
    name = (cfg.template_for(kind) or "").strip()
    if not name or not (cfg.template_language or "").strip():
        return f"no {kind} template configured"
    # The templates must be UTILITY. A copy of the template that Meta (or the
    # sync) marked MARKETING means a category change we have not accepted:
    # refuse instead of sending a marketing message.
    from separator.waba.models import Template

    marketing = Template.objects.filter(
        waba_id=phone.waba_id,
        name=name,
        lang=cfg.template_language,
        category__iexact="MARKETING",
    ).exists()
    if marketing:
        return f"template {name} is MARKETING in the local copy"
    return ""


def build_template_payload(cfg, lead, kind):
    project = (lead.project or "").strip() or (cfg.project_fallback or "").strip()
    template = {
        "name": cfg.template_for(kind).strip(),
        "language": {"code": cfg.template_language.strip()},
    }
    if project:
        template["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": project}]}
        ]
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": lead.phone.lstrip("+"),
        "type": "template",
        "template": template,
    }


def post_message(sender_phone, payload):
    """POST /{phone_id}/messages through the existing Graph helper.

    Deliberately not send_message_from_phone(): that helper re-routes templates
    whose local copy is MARKETING to /marketing_messages. Our endpoint is fixed.
    Raises on any error (HTTP errors come back as Exception(dict)).
    """
    from separator.waba import utils as waba_utils

    return waba_utils.call_api(
        waba=sender_phone.waba,
        endpoint=f"{sender_phone.phone_id}/messages",
        method="post",
        payload=payload,
    )


def wamid_of(response):
    if not isinstance(response, dict):
        return None
    messages = response.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        return messages[0].get("id")
    return None


def gate(lead_id, kind):
    """Check every rule and, if all pass, send the template. Returns GateResult."""
    if kind not in (KIND_CONFIRMATION, KIND_REMINDER):
        raise ValueError(f"unknown kind {kind}")
    WaqSettings.load()  # make sure the row exists before locking it
    with transaction.atomic():
        # of=("self",): lock only the settings row, not the joined phone/waba.
        cfg = (
            WaqSettings.objects.select_for_update(of=("self",))
            .select_related("phone", "phone__waba")
            .get(pk=WaqSettings.SINGLETON_PK)
        )
        lead = WaqLead.objects.select_for_update().filter(pk=lead_id).first()
        if lead is None:
            return GateResult("skipped", "lead not found")
        if not _expected_state(lead, kind):
            return GateResult("skipped", f"lead state {lead.state}")

        WaqPhone.objects.get_or_create(phone=lead.phone)
        phone_row = WaqPhone.objects.select_for_update().get(phone=lead.phone)
        now = timezone.now()

        # 1. Manual stop: nothing is sent, the lead keeps its state.
        if not cfg.enabled:
            if kind == KIND_CONFIRMATION:
                _set(lead, block_reason=REASON_MANUAL_STOP)
            return GateResult(REASON_MANUAL_STOP)

        # 2. Automatic stop.
        if cfg.autostop_active:
            if kind == KIND_CONFIRMATION:
                _set(lead, block_reason=REASON_AUTOSTOP)
            return GateResult(REASON_AUTOSTOP)

        # 3. Stop list.
        if phone_row.opted_out:
            if kind == KIND_CONFIRMATION:
                _set(lead, state=WaqLead.STATE_BLOCKED, block_reason=REASON_STOPLIST)
            return GateResult(REASON_STOPLIST)

        # 4. One confirmation per number per 30 days.
        if kind == KIND_CONFIRMATION and phone_row.last_contacted_at and (
            phone_row.last_contacted_at >= now - timedelta(days=DEDUP_DAYS)
        ):
            _set(lead, state=WaqLead.STATE_BLOCKED, block_reason=REASON_CONTACTED_30D)
            return GateResult(REASON_CONTACTED_30D)

        # 5. Hourly / daily caps over both template kinds.
        last_hour = templates_sent_since(now - timedelta(hours=1))
        last_day = templates_sent_since(now - timedelta(hours=24))
        if last_hour >= cfg.hourly_cap or last_day >= cfg.daily_cap:
            detail = f"sent last hour {last_hour}/{cfg.hourly_cap}, last 24h {last_day}/{cfg.daily_cap}"
            alert_due = _cap_alert_due(now)
            WaqEvent.objects.create(
                lead=lead,
                kind=WaqEvent.KIND_CAP,
                data={"deal_id": lead.deal_id, "kind": kind, "detail": detail, "alerted": alert_due},
            )
            if kind == KIND_CONFIRMATION:
                # Held for manual review. A reminder is not held: the scheduler
                # simply tries it again on its next run.
                _set(lead, state=WaqLead.STATE_HELD, block_reason=REASON_CAP)
            if alert_due:
                alerts.alert_on_commit(f"cap reached, {kind} not sent (deal {lead.deal_id}): {detail}")
            return GateResult(REASON_CAP, detail)

        # 6. Sender / template configuration.
        problem = _misconfiguration(cfg, kind)
        if problem:
            WaqEvent.objects.create(
                lead=lead,
                kind=WaqEvent.KIND_MISCONFIGURED,
                data={"deal_id": lead.deal_id, "kind": kind, "detail": problem},
            )
            if kind == KIND_CONFIRMATION:
                _set(lead, block_reason=REASON_MISCONFIGURED)
            alerts.alert_on_commit(f"misconfigured, {kind} not sent (deal {lead.deal_id}): {problem}")
            return GateResult(REASON_MISCONFIGURED, problem)

        # All clear: send inside the same transaction.
        payload = build_template_payload(cfg, lead, kind)
        try:
            response = post_message(cfg.phone, payload)
            wamid = wamid_of(response)
            if not wamid:
                raise Exception(f"no wamid in Graph response: {str(response)[:500]}")
        except Exception as exc:
            error = str(exc)[:2000]
            if kind == KIND_CONFIRMATION:
                _set(lead, state=WaqLead.STATE_FAILED, last_error=f"{kind}: {error}")
            else:
                # The confirmation is out and its reply is still awaited: a failed
                # reminder must not take the lead out of the waiting state. The
                # send_error event below stops the scheduler from retrying it.
                _set(lead, last_error=f"{kind}: {error}")
            WaqEvent.objects.create(
                lead=lead,
                kind=WaqEvent.KIND_SEND_ERROR,
                data={"deal_id": lead.deal_id, "kind": kind, "error": error},
            )
            alerts.alert_on_commit(f"{kind} send failed (deal {lead.deal_id}): {error[:500]}")
            return GateResult("failed", error)

        sent_at = timezone.now()
        if kind == KIND_CONFIRMATION:
            _set(
                lead,
                state=WaqLead.STATE_CONFIRMATION_SENT,
                confirmation_wamid=wamid,
                confirmation_sent_at=sent_at,
                block_reason="",
            )
        else:
            _set(
                lead,
                state=WaqLead.STATE_REMINDER_SENT,
                reminder_wamid=wamid,
                reminder_sent_at=sent_at,
            )
        phone_row.last_contacted_at = sent_at
        phone_row.save(update_fields=["last_contacted_at"])
        return GateResult("sent", wamid)


def _set(lead, **fields):
    for name, value in fields.items():
        setattr(lead, name, value)
    lead.save(update_fields=list(fields))
