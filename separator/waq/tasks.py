"""Celery tasks: confirmation send, reminder / no-answer scheduler, reply handling.

All waq tasks run on their own queue `waq` (own worker): a slow Claude or Graph
call never holds the single slot that the Bitrix open-line traffic uses.

Send tasks run with acks_late=False (at most once); the gate additionally
records every attempt before the request and never repeats it.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import alerts, classify, localtime
from .gate import KIND_CONFIRMATION, KIND_REMINDER, gate, post_message
from .models import WaqEvent, WaqLead, WaqPhone, WaqSettings

logger = logging.getLogger("django")

QUEUE = "waq"
OPEN_LINE_DELAY_SECONDS = 20

AUTOSTOP_NEGATIVE_ABS = 3
AUTOSTOP_MIN_CONFIRMATIONS = 5
AUTOSTOP_NEGATIVE_SHARE = 0.30

# What the deal timeline comment says for each class.
COMMENT_LABELS = {
    classify.QUAL: "подтвердил заявку",
    classify.QUAL_QUESTION: "задал вопрос по объекту",
    classify.BOT: "спросил, не бот ли",
}
MEDIA_LABEL = "медиа, не расшифровано"
NOT_LEAD_BUTTON_LABEL = "нажал «No, not me»"
NOT_LEAD_TEXT_LABEL = "по тексту ответа: не оставлял заявку / ошибся номером / спам / не по делу"
OUTCOME_COMMENTS = {
    classify.STOP: "AI: СТОП",
    classify.WHO: "AI: спросил „кто это“",
    classify.COMPANY: "AI: спросил „какая компания“",
}

# _send_reply() outcomes
REPLY_SENT = "sent"
REPLY_EMPTY = "empty"  # no text configured: nothing to send, by design
REPLY_MANUAL_STOP = "manual_stop"
REPLY_FAILED = "failed"
REPLY_CLOSED = "closed"  # another message closed the dialogue first
REPLY_NOTES = {
    REPLY_MANUAL_STOP: "ответ клиенту не отправлен: ручной стоп",
    REPLY_FAILED: "ответ клиенту не отправлен: ошибка отправки",
    REPLY_CLOSED: "ответ клиенту не отправлен: диалог уже закрыт",
}
CLOSED_STATES = (WaqLead.STATE_QUALIFIED, WaqLead.STATE_NOT_LEAD, WaqLead.STATE_STOPPED)


# --------------------------------------------------------------------------- send


@shared_task(queue=QUEUE, acks_late=False, ignore_result=True)
def send_confirmation(lead_id):
    result = gate(lead_id, KIND_CONFIRMATION)
    return result.status


@shared_task(queue=QUEUE, acks_late=False, ignore_result=True)
def run_schedule():
    """Every 5 minutes: mark no-answer leads, then send due reminders.

    Only leads that never answered at all (replied_at is empty) get a reminder
    or become no_answer. The local-hour window is checked here as a cheap
    filter and again by the gate, under its locks, right before sending.
    """
    cfg = WaqSettings.load()
    now = timezone.now()
    answer_deadline = now - timedelta(hours=cfg.no_answer_after_hours)

    # Counted from the first template attempt, so a confirmation with an
    # unknown outcome (timeout) also ends as no_answer.
    expired = WaqLead.objects.filter(
        Q(confirmation_attempted_at__lte=answer_deadline) | Q(confirmation_sent_at__lte=answer_deadline),
        state__in=WaqLead.WAITING_STATES,
        replied_at__isnull=True,
    ).update(state=WaqLead.STATE_NO_ANSWER)

    if not cfg.reminder_enabled:
        return {"no_answer": expired, "reminders": "disabled"}

    due = WaqLead.objects.filter(
        state=WaqLead.STATE_CONFIRMATION_SENT,
        replied_at__isnull=True,
        reminder_sent_at__isnull=True,
        reminder_attempted_at__isnull=True,
        confirmation_sent_at__lte=now - timedelta(minutes=cfg.reminder_after_min),
        confirmation_sent_at__gte=now - timedelta(minutes=cfg.reminder_before_min),
        confirmation_sent_at__gt=answer_deadline,
    ).order_by("confirmation_sent_at")

    outcomes = {}
    for lead in due:
        if not localtime.in_window(lead.phone, cfg.reminder_window_start, cfg.reminder_window_end, now):
            outcomes["outside_window"] = outcomes.get("outside_window", 0) + 1
            continue
        status = gate(lead.id, KIND_REMINDER).status
        outcomes[status] = outcomes.get(status, 0) + 1
    return {"no_answer": expired, **outcomes}


# -------------------------------------------------------------------------- reply


def _negative_event_data(lead, replied_at):
    seconds = None
    if lead.lead_created_at:
        seconds = int((replied_at - lead.lead_created_at).total_seconds())
    return {
        "deal_id": lead.deal_id,
        "form_type": lead.form_type,
        "preferred_channel": lead.preferred_channel,
        "seconds_from_lead": seconds,
    }


def _already_processed(lead_id, message_id):
    return WaqEvent.objects.filter(
        lead_id=lead_id, kind=WaqEvent.KIND_REPLY, data__message_id=message_id
    ).exists()


def _reply_text(cfg, lead, label):
    if label in (classify.QUAL, classify.QUAL_QUESTION, classify.BOT):
        return {
            classify.QUAL: cfg.reply_qual,
            classify.QUAL_QUESTION: cfg.reply_qual_question,
            classify.BOT: cfg.reply_bot,
        }[label]
    if label == classify.WHO:
        project = (lead.project or "").strip() or (cfg.project_fallback or "").strip()
        return (cfg.reply_who or "").replace("{{1}}", project)
    if label == classify.COMPANY:
        return cfg.reply_company
    if label == classify.STOP:
        return cfg.reply_stop
    if label == classify.NOT_LEAD:
        return cfg.reply_not_me
    return ""


def _opt_out(phone, reason, now):
    WaqPhone.objects.get_or_create(phone=phone)
    row = WaqPhone.objects.select_for_update().get(phone=phone)
    if not row.opted_out:
        row.opted_out = True
        row.opted_out_at = now
        row.opt_out_reason = reason
        row.save(update_fields=["opted_out", "opted_out_at", "opt_out_reason"])


@shared_task(queue=QUEUE, acks_late=False, ignore_result=True)
def process_reply(lead_id, message):
    message = message if isinstance(message, dict) else {}
    message_id = message.get("id")
    if not message_id:
        return "no message id"

    # Cheap pre-check before any paid call; repeated under the lock below.
    lead = WaqLead.objects.filter(pk=lead_id).first()
    if lead is None or lead.state not in WaqLead.WAITING_STATES:
        return "not waiting"
    if _already_processed(lead_id, message_id):
        return "duplicate"

    label, source = classify.classify_local(message)
    llm_error = None
    llm_error_kind = None
    if label is None:
        try:
            label = classify.classify_text(classify.message_text(message), lead.project)
            source = WaqLead.SOURCE_LLM
        except classify.ClassifierUnavailable as exc:
            llm_error = str(exc)[:1000]
            llm_error_kind = exc.kind
            label = classify.UNSURE
        except Exception as exc:  # belt and braces: never let the LLM step crash the reply
            llm_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            llm_error_kind = type(exc).__name__
            label = classify.UNSURE
        if label == classify.UNSURE and llm_error is None:
            llm_error = "model is not sure"
            llm_error_kind = "unsure"

    now = timezone.now()
    if label == classify.NOT_LEAD:
        WaqSettings.load()  # the row must exist before it is locked below
    with transaction.atomic():
        if label == classify.NOT_LEAD:
            # Lock order settings -> lead -> phone, as in the gate: the autostop
            # check below happens in this same transaction.
            locked_cfg = WaqSettings.objects.select_for_update(of=("self",)).get(pk=WaqSettings.SINGLETON_PK)
        lead = WaqLead.objects.select_for_update().filter(pk=lead_id).first()
        if lead is None or lead.state not in WaqLead.WAITING_STATES:
            return "not waiting"
        if _already_processed(lead_id, message_id):
            return "duplicate"
        WaqEvent.objects.create(
            lead=lead,
            kind=WaqEvent.KIND_REPLY,
            data={"message_id": message_id, "type": message.get("type"), "class": label, "source": source},
        )
        # Any inbound message is an answer: no reminder, no no_answer after it.
        update = []
        if lead.replied_at is None:
            lead.replied_at = now
            update.append("replied_at")
        if label in (classify.QUAL, classify.QUAL_QUESTION, classify.BOT):
            lead.state = WaqLead.STATE_QUALIFIED
            lead.outcome_source = source or ""
            update += ["state", "outcome_source"]
        elif label in (classify.NOT_LEAD, classify.STOP):
            is_stop = label == classify.STOP
            lead.state = WaqLead.STATE_STOPPED if is_stop else WaqLead.STATE_NOT_LEAD
            lead.outcome_source = source or ""
            update += ["state", "outcome_source"]
        if update:
            lead.save(update_fields=update)

        if label == classify.IGNORED:
            return "ignored"
        if label == classify.UNSURE:
            WaqEvent.objects.create(
                lead=lead,
                kind=WaqEvent.KIND_LLM_ERROR,
                data={"deal_id": lead.deal_id, "error_kind": llm_error_kind, "error": llm_error},
            )
        elif label in (classify.NOT_LEAD, classify.STOP):
            _opt_out(lead.phone, "stop" if is_stop else "not_me", now)
            WaqEvent.objects.create(
                lead=lead,
                kind=WaqEvent.KIND_STOP if is_stop else WaqEvent.KIND_NEGATIVE_REPLY,
                data=_negative_event_data(lead, now),
            )
            if not is_stop:
                # Before any external call: a crash later must not skip the autostop.
                _autostop_if_needed(locked_cfg, now)
        # WHO / COMPANY: the lead keeps waiting.

    cfg = WaqSettings.load()

    if label == classify.UNSURE:
        _timeline_comment(cfg, lead, "AI: не классифицировано")
        alerts.send_alert(
            f"LLM unavailable [{llm_error_kind}], reply not classified, no answer sent "
            f"(deal {lead.deal_id}): {llm_error}"
        )
        return "unclassified"

    # 1. Reply to the customer. Only the manual stop blocks it (autostop is
    #    about templates; the customer wrote to us first). The closing reply
    #    belongs to the state this message set; WHO/COMPANY only while waiting.
    is_qual = label in (classify.QUAL, classify.QUAL_QUESTION, classify.BOT)
    allowed_states = (lead.state,) if lead.state in CLOSED_STATES else WaqLead.WAITING_STATES
    reply = (_reply_text(cfg, lead, label) or "").strip()
    reply_status = _send_reply(lead.id, reply, allowed_states) if reply else REPLY_EMPTY
    # 2. The operator sees every message the bot actually sent.
    if reply_status == REPLY_SENT:
        _show_in_open_line(cfg, lead, reply)

    # 3. Deal: stage/responsible only for a qualified lead; a timeline comment
    #    for every outcome (Bitrix actions run even under the manual stop).
    if is_qual:
        _update_deal(cfg, lead)
        detail = MEDIA_LABEL if source == WaqLead.SOURCE_MEDIA else COMMENT_LABELS[label]
        parts = ["AI: КВАЛ", detail, lead.project or "", lead.form_type or ""]
    elif label == classify.NOT_LEAD:
        detail = NOT_LEAD_BUTTON_LABEL if source == WaqLead.SOURCE_BUTTON else NOT_LEAD_TEXT_LABEL
        parts = ["AI: НЕ ЛИД", detail]
    else:
        parts = [OUTCOME_COMMENTS[label]]
    if reply_status in REPLY_NOTES:
        parts.append(REPLY_NOTES[reply_status])
    _timeline_comment(cfg, lead, " · ".join(part for part in parts if part))

    if is_qual:
        return "qualified"
    if label == classify.NOT_LEAD:
        return "not_lead"
    if label == classify.STOP:
        return "stopped"
    return label.lower()


# ------------------------------------------------------------- reply side effects


def _record_failure(lead, kind, error):
    try:
        WaqEvent.objects.create(lead=lead, kind=kind, data={"deal_id": lead.deal_id, "error": str(error)[:1000]})
    except Exception as exc:
        logger.error("waq: could not record %s: %s", kind, exc)


def _send_reply(lead_id, text, allowed_states):
    """Session text message (the customer's reply opened the window).

    Runs under the lead's row lock, so two replies for one lead never race:
    the lead state and the manual stop are both read right before the request.
    Returns REPLY_SENT / REPLY_CLOSED / REPLY_MANUAL_STOP / REPLY_FAILED. The
    autostop does not apply here: it only blocks templates (see gate.py).
    """
    with transaction.atomic():
        lead = WaqLead.objects.select_for_update().get(pk=lead_id)
        if lead.state not in allowed_states:
            return REPLY_CLOSED
        cfg = WaqSettings.objects.select_related("phone", "phone__waba").get(pk=WaqSettings.SINGLETON_PK)
        if not cfg.enabled:
            # Manual stop is the emergency brake: no outbound WhatsApp at all.
            _record_failure(lead, WaqEvent.KIND_REPLY_ERROR, "suppressed: manual stop")
            return REPLY_MANUAL_STOP
        if cfg.phone is None or not cfg.phone.phone_id or not cfg.phone.waba_id:
            _record_failure(lead, WaqEvent.KIND_REPLY_ERROR, "no sender phone")
            return REPLY_FAILED
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": lead.phone.lstrip("+"),
            "type": "text",
            "text": {"body": text},
        }
        try:
            post_message(cfg.phone, payload)
            return REPLY_SENT
        except Exception as exc:
            logger.error("waq: bot reply failed for deal %s: %s", lead.deal_id, exc)
            _record_failure(lead, WaqEvent.KIND_REPLY_ERROR, exc)
            return REPLY_FAILED


def _app_instance(cfg):
    from separator.bitrix import utils as bitrix_utils

    line = cfg.phone.line if cfg.phone is not None else None
    if line is None:
        return None
    return bitrix_utils.get_line_app_instance(line, "waba")


def _show_in_open_line(cfg, lead, text):
    """System message in the open line chat, so the operator sees the bot reply."""
    try:
        from separator.bitrix import tasks as bitrix_tasks

        line = cfg.phone.line if cfg.phone is not None else None
        appinstance = _app_instance(cfg)
        if appinstance is None or line is None or line.connector_id is None:
            raise Exception("sender phone is not connected to a Bitrix open line")
        bitrix_tasks.send_messages.apply_async(
            args=[str(appinstance.id), lead.phone, text, line.connector.code, line.line_id],
            kwargs={"manager_id": 0},
            countdown=OPEN_LINE_DELAY_SECONDS,
        )
    except Exception as exc:
        logger.error("waq: open line message failed for deal %s: %s", lead.deal_id, exc)
        _record_failure(lead, WaqEvent.KIND_BITRIX_ERROR, f"open line: {exc}")


def _update_deal(cfg, lead):
    fields = {}
    if (cfg.qualified_stage_id or "").strip():
        fields["STAGE_ID"] = cfg.qualified_stage_id.strip()
    if cfg.responsible_user_id:
        fields["ASSIGNED_BY_ID"] = cfg.responsible_user_id
    if not fields:
        return
    try:
        from separator.bitrix.crest import call_method

        appinstance = _app_instance(cfg)
        if appinstance is None:
            raise Exception("no Bitrix app instance for the sender line")
        call_method(appinstance, "crm.deal.update", {"id": lead.deal_id, "fields": fields})
    except Exception as exc:
        logger.error("waq: crm.deal.update failed for deal %s: %s", lead.deal_id, exc)
        _record_failure(lead, WaqEvent.KIND_BITRIX_ERROR, f"crm.deal.update: {exc}")


def _timeline_comment(cfg, lead, comment):
    try:
        from separator.bitrix.crest import call_method

        appinstance = _app_instance(cfg)
        if appinstance is None:
            raise Exception("no Bitrix app instance for the sender line")
        call_method(
            appinstance,
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_ID": lead.deal_id, "ENTITY_TYPE": "deal", "COMMENT": comment}},
        )
    except Exception as exc:
        logger.error("waq: timeline comment failed for deal %s: %s", lead.deal_id, exc)
        _record_failure(lead, WaqEvent.KIND_BITRIX_ERROR, f"crm.timeline.comment.add: {exc}")


# ----------------------------------------------------------------------- autostop


def check_autostop():
    """Stand-alone check (own transaction); process_reply uses _autostop_if_needed."""
    WaqSettings.load()
    with transaction.atomic():
        cfg = WaqSettings.objects.select_for_update().get(pk=WaqSettings.SINGLETON_PK)
        return _autostop_if_needed(cfg, timezone.now())


def _autostop_if_needed(cfg, now):
    """After a NOT_LEAD: >=3 negative replies in 24h, or >=5 confirmations in
    24h with more than 30% negative replies -> stop templates until cleared by
    hand. `cfg` must be locked (select_for_update) by the caller's transaction."""
    if cfg.autostop_active:
        return False
    since = now - timedelta(hours=24)
    negatives = WaqEvent.objects.filter(kind=WaqEvent.KIND_NEGATIVE_REPLY, created_at__gte=since).count()
    confirmations = WaqLead.objects.filter(confirmation_sent_at__gte=since).count()
    reason = ""
    if negatives >= AUTOSTOP_NEGATIVE_ABS:
        reason = f"{negatives} negative replies in 24h"
    elif confirmations >= AUTOSTOP_MIN_CONFIRMATIONS and negatives / confirmations > AUTOSTOP_NEGATIVE_SHARE:
        reason = f"{negatives} negative replies to {confirmations} confirmations in 24h"
    if not reason:
        return False
    cfg.autostop_active = True
    cfg.autostop_reason = reason
    cfg.autostop_at = now
    cfg.save(update_fields=["autostop_active", "autostop_reason", "autostop_at"])
    WaqEvent.objects.create(
        kind=WaqEvent.KIND_AUTOSTOP,
        data={"reason": reason, "negatives": negatives, "confirmations": confirmations},
    )
    alerts.alert_on_commit(f"AUTOSTOP: {reason}. Templates stopped until cleared in the admin.")
    return True
