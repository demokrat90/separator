"""The single "may we send?" check, run right before every template request to Meta.

Two short transactions around one Graph request, at most once per template:

  A. lock WaqSettings (pk=1), the lead, the customer's WaqPhone row (always in
     this order - process_reply uses the same order), check every rule, then
     RECORD THE ATTEMPT (`*_attempted_at`, WaqPhone.last_contacted_at) and commit.
     The caps count attempts, so they stay race-free without holding the
     settings lock during the HTTP call.
  B. lock the lead and the number again, take a last look (manual stop,
     autostop, opt-out, lead state - all read fresh), POST, record the outcome.
     No automatic retry ever: an attempt that is on record is never repeated,
     whatever happened to it (timeout, crash, Meta error).

Only a definite rejection by Meta (an HTTP 4xx error answer) means "failed":
it also restores the number's last_contacted_at. A timeout, a connection
error, a 5xx or a 200 without a message id leave the outcome unknown: the
confirmation lead becomes `confirmation_unknown` and the customer's replies to
it are still handled; the number counts as contacted.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

import requests
from django.db import transaction
from django.utils import timezone

from . import alerts, localtime
from .models import WaqEvent, WaqLead, WaqPhone, WaqSettings

logger = logging.getLogger("django")

KIND_CONFIRMATION = "confirmation"
KIND_REMINDER = "reminder"

DEDUP_DAYS = 30
CAP_ALERT_EVERY = timedelta(hours=1)
# A confirmation for a lead received longer ago than this is never sent.
STALE_AFTER = timedelta(minutes=30)

# Refusal reasons, in the order they are checked.
REASON_STALE = "stale"
REASON_MANUAL_STOP = "manual_stop"
REASON_AUTOSTOP = "autostop"
REASON_STOPLIST = "stoplist"
REASON_CONTACTED_30D = "contacted_30d"
REASON_OUTSIDE_WINDOW = "outside_window"
REASON_CAP = "cap"
REASON_MISCONFIGURED = "misconfigured"
REASON_REMINDER_DISABLED = "reminder_disabled"
REASON_REMINDER_EXPIRED = "reminder_expired"
REASON_LEAD_CHANGED = "lead_changed"


@dataclass
class GateResult:
    status: str  # "sent", "failed", "skipped" or one of the REASON_* values
    detail: str = ""

    @property
    def sent(self):
        return self.status == "sent"


def _attempt_field(kind):
    return "confirmation_attempted_at" if kind == KIND_CONFIRMATION else "reminder_attempted_at"


def _expected_state(lead, kind):
    if getattr(lead, _attempt_field(kind)) is not None:
        return False  # at most once: an attempt on record is final
    if kind == KIND_CONFIRMATION:
        return lead.state == WaqLead.STATE_RECEIVED and lead.confirmation_sent_at is None
    return (
        lead.state == WaqLead.STATE_CONFIRMATION_SENT
        and lead.reminder_sent_at is None
        and lead.replied_at is None
    )


def templates_attempted_since(since):
    """Templates of both kinds attempted since `since` (sent, in flight or failed)."""
    confirmations = WaqLead.objects.filter(confirmation_attempted_at__gte=since).count()
    reminders = WaqLead.objects.filter(reminder_attempted_at__gte=since).count()
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

    # ---- A. checks + attempt record, under locks
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

        # 0. A confirmation is for a fresh lead only.
        if kind == KIND_CONFIRMATION and lead.received_at < now - STALE_AFTER:
            _set(lead, state=WaqLead.STATE_BLOCKED, block_reason=REASON_STALE)
            return GateResult(REASON_STALE)

        # 1. Manual stop. A confirmation refused here is final (never sent later).
        if not cfg.enabled:
            if kind == KIND_CONFIRMATION:
                _set(lead, state=WaqLead.STATE_BLOCKED, block_reason=REASON_MANUAL_STOP)
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

        # 4a. Reminder: switched on, still within its time frame, and the
        #     local-hour window - all checked here, right before sending.
        if kind == KIND_REMINDER and not cfg.reminder_enabled:
            return GateResult(REASON_REMINDER_DISABLED)
        if kind == KIND_REMINDER and lead.confirmation_sent_at < now - timedelta(minutes=cfg.reminder_before_min):
            return GateResult(REASON_REMINDER_EXPIRED)
        if kind == KIND_REMINDER and not localtime.in_window(
            lead.phone, cfg.reminder_window_start, cfg.reminder_window_end, now
        ):
            return GateResult(REASON_OUTSIDE_WINDOW)

        # 5. Hourly / daily caps over both template kinds (attempts count).
        last_hour = templates_attempted_since(now - timedelta(hours=1))
        last_day = templates_attempted_since(now - timedelta(hours=24))
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

        # All clear: record the attempt BEFORE the request.
        payload = build_template_payload(cfg, lead, kind)
        sender = cfg.phone
        _set(lead, **{_attempt_field(kind): now})
        previous_contact = phone_row.last_contacted_at
        phone_row.last_contacted_at = now
        phone_row.save(update_fields=["last_contacted_at"])
        deal_id = lead.deal_id

    # ---- B. last look, request and outcome, holding the lead and number rows
    # (not the settings row): a STOP/QUAL or an opt-out of this number cannot
    # commit while the template is in flight; settings are re-read fresh.
    with transaction.atomic():
        lead = WaqLead.objects.select_for_update().get(pk=lead_id)
        phone_row = WaqPhone.objects.select_for_update().get(phone=lead.phone)
        reason = _last_look(lead, phone_row, kind)
        if reason:
            if kind == KIND_CONFIRMATION and lead.state == WaqLead.STATE_RECEIVED:
                _set(lead, state=WaqLead.STATE_BLOCKED, block_reason=reason)
            else:
                _set(lead, last_error=f"{kind} not sent: {reason}")
            _restore_contact(phone_row, now, previous_contact)  # nothing went out
            return GateResult(reason, "refused right before the request")

        error = None
        definite = False
        wamid = None
        try:
            wamid = wamid_of(post_message(sender, payload))
            if not wamid:
                error = "no wamid in Graph response"  # 200 without an id: outcome unknown
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            definite = is_definite_rejection(exc)

        if error is None:
            sent_at = timezone.now()
            if kind == KIND_CONFIRMATION:
                fields = {"confirmation_wamid": wamid, "confirmation_sent_at": sent_at, "block_reason": ""}
                if lead.state == WaqLead.STATE_RECEIVED:
                    fields["state"] = WaqLead.STATE_CONFIRMATION_SENT
            else:
                fields = {"reminder_wamid": wamid, "reminder_sent_at": sent_at}
                if lead.state == WaqLead.STATE_CONFIRMATION_SENT:
                    fields["state"] = WaqLead.STATE_REMINDER_SENT
            _set(lead, **fields)
            return GateResult("sent", wamid)

        error = error[:2000]
        if kind == KIND_CONFIRMATION:
            # failed = Meta answered with a 4xx error: nothing was delivered.
            # Anything else (timeout, 5xx, no id) may have reached the customer:
            # their replies must still be picked up.
            state = WaqLead.STATE_FAILED if definite else WaqLead.STATE_CONFIRMATION_UNKNOWN
            _set(lead, state=state, last_error=f"{kind}: {error}")
        else:
            # The confirmation is out and its reply is still awaited: a failed
            # reminder must not take the lead out of the waiting state.
            _set(lead, last_error=f"{kind}: {error}")
        if definite:
            _restore_contact(phone_row, now, previous_contact)
        WaqEvent.objects.create(
            lead=lead,
            kind=WaqEvent.KIND_SEND_ERROR,
            data={"deal_id": deal_id, "kind": kind, "error": error, "ambiguous": not definite},
        )
        alerts.alert_on_commit(
            f"{kind} send failed{'' if definite else ' (outcome unknown, may have been delivered)'}, "
            f"not retried (deal {deal_id}): {error[:500]}"
        )
    return GateResult("failed", error)


def is_definite_rejection(exc):
    """True only for an HTTP 4xx error answer from Graph (call_api raises
    Exception(details) from the HTTPError). Timeouts, connection errors, 5xx
    and anything unexpected leave the outcome unknown."""
    cause = exc.__cause__
    if isinstance(cause, requests.HTTPError) and cause.response is not None:
        return 400 <= cause.response.status_code < 500
    return False


def _last_look(lead, phone_row, kind):
    """Everything that may have changed since the attempt was recorded."""
    fresh = (
        WaqSettings.objects.filter(pk=WaqSettings.SINGLETON_PK)
        .values("enabled", "autostop_active", "reminder_enabled")
        .first()
        or {}
    )
    if not fresh.get("enabled"):
        return REASON_MANUAL_STOP
    if fresh.get("autostop_active"):
        return REASON_AUTOSTOP
    if kind == KIND_REMINDER and not fresh.get("reminder_enabled"):
        return REASON_REMINDER_DISABLED
    if phone_row.opted_out:
        return REASON_STOPLIST
    if kind == KIND_CONFIRMATION:
        ok = lead.state == WaqLead.STATE_RECEIVED and lead.confirmation_sent_at is None
    else:
        ok = (
            lead.state == WaqLead.STATE_CONFIRMATION_SENT
            and lead.reminder_sent_at is None
            and lead.replied_at is None
        )
    return "" if ok else REASON_LEAD_CHANGED


def _restore_contact(phone_row, attempted_at, previous):
    """Undo our last_contacted_at (row locked by the caller) if nothing has
    overwritten it since."""
    if phone_row.last_contacted_at == attempted_at:
        phone_row.last_contacted_at = previous
        phone_row.save(update_fields=["last_contacted_at"])


def _set(lead, **fields):
    for name, value in fields.items():
        setattr(lead, name, value)
    lead.save(update_fields=list(fields))
