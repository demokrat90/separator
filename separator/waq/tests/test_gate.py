"""gate(): every refusal reason, caps, dedup, stop list, Meta errors, concurrency."""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from separator.waba.models import Template
from separator.waq import gate as gate_module
from separator.waq.gate import KIND_CONFIRMATION, KIND_REMINDER, gate
from separator.waq.models import WaqEvent, WaqLead, WaqPhone, WaqSettings

from .conftest import CUSTOMER, CUSTOMER_2, make_lead

pytestmark = pytest.mark.django_db


def test_all_clear_sends_the_utility_template(cfg, graph, alerts_sent):
    lead = make_lead()
    result = gate(lead.id, KIND_CONFIRMATION)

    assert result.sent
    call = graph.calls[0]
    assert call["endpoint"] == "test-phone-id-1/messages"
    assert call["method"] == "post"
    payload = call["payload"]
    assert payload["to"] == CUSTOMER.lstrip("+")
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "request_confirmation"
    assert payload["template"]["language"] == {"code": "en"}
    assert payload["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Test Project"}]}
    ]

    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT
    assert lead.confirmation_wamid == "wamid.TEST1"
    assert lead.confirmation_sent_at is not None
    # Contact time = the attempt, recorded before the request.
    assert WaqPhone.objects.get(phone=CUSTOMER).last_contacted_at == lead.confirmation_attempted_at
    assert alerts_sent == []


def test_empty_project_uses_the_fallback(cfg, graph):
    lead = make_lead(project="")
    gate(lead.id, KIND_CONFIRMATION)
    params = graph.calls[0]["payload"]["template"]["components"][0]["parameters"]
    assert params == [{"type": "text", "text": "Test Project Fallback"}]


def test_utility_template_copy_is_not_rerouted_to_marketing_messages(cfg, graph, sender):
    Template.objects.create(
        id="tpl-1", waba=sender.waba, name="request_confirmation", lang="en", category="UTILITY", status="APPROVED"
    )
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).sent
    assert graph.calls[0]["endpoint"].endswith("/messages")
    assert "marketing" not in graph.calls[0]["endpoint"]


def test_marketing_template_copy_is_refused(cfg, graph, sender, alerts_sent, django_capture_on_commit_callbacks):
    Template.objects.create(
        id="tpl-2", waba=sender.waba, name="request_confirmation", lang="en", category="MARKETING", status="APPROVED"
    )
    lead = make_lead()
    with django_capture_on_commit_callbacks(execute=True):
        result = gate(lead.id, KIND_CONFIRMATION)
    assert result.status == gate_module.REASON_MISCONFIGURED
    assert graph.calls == []
    assert len(alerts_sent) == 1


def test_manual_stop_sends_nothing_and_keeps_the_lead(cfg, graph):
    cfg.enabled = False
    cfg.save()
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_MANUAL_STOP
    assert graph.calls == []
    lead.refresh_from_db()
    # Final: switching the bot on later never sends this lead's confirmation.
    assert lead.state == WaqLead.STATE_BLOCKED and lead.block_reason == "manual_stop"


def test_settings_are_off_by_default(db, graph, sender):
    settings_row = WaqSettings.load()
    assert settings_row.enabled is False
    settings_row.phone = sender
    settings_row.save()
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_MANUAL_STOP
    assert graph.calls == []


def test_autostop_sends_nothing(cfg, graph):
    cfg.autostop_active = True
    cfg.save()
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_AUTOSTOP
    assert graph.calls == []


def test_stop_list_blocks(cfg, graph):
    WaqPhone.objects.create(phone=CUSTOMER, opted_out=True, opted_out_at=timezone.now())
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_STOPLIST
    assert graph.calls == []
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_BLOCKED
    assert lead.block_reason == "stoplist"


def test_number_contacted_within_30_days_is_blocked(cfg, graph):
    WaqPhone.objects.create(phone=CUSTOMER, last_contacted_at=timezone.now() - timedelta(days=29))
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_CONTACTED_30D
    assert graph.calls == []
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_BLOCKED


def test_number_contacted_31_days_ago_is_sent(cfg, graph):
    WaqPhone.objects.create(phone=CUSTOMER, last_contacted_at=timezone.now() - timedelta(days=31))
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).sent


def _sent_leads(count, ago, field="confirmation_sent_at"):
    attempt_field = field.replace("_sent_at", "_attempted_at")
    for i in range(count):
        when = timezone.now() - ago
        make_lead(
            phone=f"+1555010{1000 + i}",
            state=WaqLead.STATE_CONFIRMATION_SENT,
            **{field: when, attempt_field: when},
        )


def test_hourly_cap_holds_the_lead_and_alerts_once_per_hour(
    cfg, graph, alerts_sent, django_capture_on_commit_callbacks
):
    _sent_leads(10, timedelta(minutes=30))
    first = make_lead()
    second = make_lead(phone=CUSTOMER_2)
    with django_capture_on_commit_callbacks(execute=True):
        assert gate(first.id, KIND_CONFIRMATION).status == gate_module.REASON_CAP
    with django_capture_on_commit_callbacks(execute=True):
        assert gate(second.id, KIND_CONFIRMATION).status == gate_module.REASON_CAP

    assert graph.calls == []
    first.refresh_from_db()
    assert first.state == WaqLead.STATE_HELD
    assert WaqEvent.objects.filter(kind=WaqEvent.KIND_CAP).count() == 2
    assert len(alerts_sent) == 1


def test_daily_cap(cfg, graph):
    _sent_leads(30, timedelta(hours=5))
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_CAP
    assert graph.calls == []


def test_reminders_count_toward_the_cap(cfg, graph):
    _sent_leads(10, timedelta(minutes=10), field="reminder_sent_at")
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_CAP


def test_below_the_cap_is_sent(cfg, graph):
    _sent_leads(9, timedelta(minutes=30))
    _sent_leads(1, timedelta(hours=2))
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).sent


def test_no_sender_phone_is_misconfigured(cfg, graph, alerts_sent, django_capture_on_commit_callbacks):
    cfg.phone = None
    cfg.save()
    lead = make_lead()
    with django_capture_on_commit_callbacks(execute=True):
        assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_MISCONFIGURED
    assert graph.calls == []
    assert len(alerts_sent) == 1


def test_no_template_name_is_misconfigured(cfg, graph):
    cfg.template_confirmation = ""
    cfg.save()
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_MISCONFIGURED
    assert graph.calls == []


def test_meta_error_marks_failed_and_keeps_contact_time(
    cfg, graph, alerts_sent, django_capture_on_commit_callbacks
):
    graph.error = "Re-engagement message"
    lead = make_lead()
    with django_capture_on_commit_callbacks(execute=True):
        assert gate(lead.id, KIND_CONFIRMATION).status == "failed"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_FAILED
    assert "Re-engagement" in lead.last_error
    assert WaqEvent.objects.filter(kind=WaqEvent.KIND_SEND_ERROR, lead=lead).exists()
    assert WaqPhone.objects.get(phone=CUSTOMER).last_contacted_at is None
    assert len(alerts_sent) == 1


def test_a_repeated_task_never_sends_twice(cfg, graph):
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).sent
    assert gate(lead.id, KIND_CONFIRMATION).status == "skipped"
    assert len(graph.calls) == 1


def test_reminder_uses_the_reminder_template(cfg, graph, hour):
    lead = make_lead(state=WaqLead.STATE_CONFIRMATION_SENT, confirmation_sent_at=timezone.now() - timedelta(hours=4))
    WaqPhone.objects.create(phone=CUSTOMER, last_contacted_at=lead.confirmation_sent_at)
    # The 30-day rule is for confirmations only: the reminder goes out.
    assert gate(lead.id, KIND_REMINDER).sent
    assert graph.calls[0]["payload"]["template"]["name"] == "request_reminder"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_REMINDER_SENT
    assert lead.reminder_wamid == "wamid.TEST1"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("known_number", [False, True], ids=["new-number", "number-seen-40-days-ago"])
def test_two_leads_for_one_number_at_once_send_exactly_one_template(sender, monkeypatch, known_number):
    """Real Postgres row locks, two threads, a slow Graph call in between.

    With a known number the WaqPhone row already exists, so the unique index
    cannot serialize the two sends - only the row locks can.
    """
    import time

    from separator.waba import utils as waba_utils

    settings_row = WaqSettings.load()
    settings_row.enabled = True
    settings_row.phone = sender
    settings_row.save()

    calls = []
    lock = threading.Lock()

    def slow_call_api(waba=None, endpoint=None, method="get", payload=None, **kwargs):
        with lock:
            calls.append(payload["to"])
        time.sleep(0.5)
        return {"messages": [{"id": f"wamid.C{len(calls)}"}]}

    monkeypatch.setattr(waba_utils, "call_api", slow_call_api)

    if known_number:
        WaqPhone.objects.create(phone=CUSTOMER, last_contacted_at=timezone.now() - timedelta(days=40))
    leads = [make_lead(), make_lead()]
    barrier = threading.Barrier(2)
    results = {}

    def run(lead_id):
        try:
            barrier.wait()
            results[lead_id] = gate(lead_id, KIND_CONFIRMATION).status
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(lead.id,)) for lead in leads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert connection.vendor == "postgresql"
    assert len(calls) == 1
    assert sorted(results.values()) == sorted(["sent", gate_module.REASON_CONTACTED_30D])
    states = sorted(WaqLead.objects.values_list("state", flat=True))
    assert states == sorted([WaqLead.STATE_CONFIRMATION_SENT, WaqLead.STATE_BLOCKED])


# ---------------------------------------------------- review fixes (at most once etc.)


def test_failed_attempts_count_toward_the_cap(cfg, graph):
    for i in range(10):
        make_lead(phone=f"+1555010{4000 + i}", state=WaqLead.STATE_FAILED, confirmation_attempted_at=timezone.now())
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_CAP


def test_stale_lead_is_never_confirmed(cfg, graph):
    lead = make_lead(received_at=timezone.now() - timedelta(minutes=31))
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_STALE
    assert graph.calls == []
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_BLOCKED and lead.block_reason == "stale"


def test_fresh_lead_is_not_stale(cfg, graph):
    lead = make_lead(received_at=timezone.now() - timedelta(minutes=29))
    assert gate(lead.id, KIND_CONFIRMATION).sent


def _timeout(monkeypatch):
    import requests

    from separator.waba import utils as waba_utils

    calls = []

    def timed_out(**kwargs):
        calls.append(kwargs)
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(waba_utils, "call_api", timed_out)
    return calls


def test_confirmation_timeout_is_final_and_counts_as_contact(cfg, monkeypatch, alerts_sent, django_capture_on_commit_callbacks):
    calls = _timeout(monkeypatch)
    lead = make_lead()
    with django_capture_on_commit_callbacks(execute=True):
        assert gate(lead.id, KIND_CONFIRMATION).status == "failed"
    assert gate(lead.id, KIND_CONFIRMATION).status == "skipped"
    assert len(calls) == 1
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_FAILED
    # Meta may have accepted it: the number stays "contacted" for the 30 days.
    assert WaqPhone.objects.get(phone=CUSTOMER).last_contacted_at is not None
    assert WaqEvent.objects.get(kind=WaqEvent.KIND_SEND_ERROR).data["ambiguous"] is True
    assert len(alerts_sent) == 1 and "not retried" in alerts_sent[0]


def test_reminder_after_a_timeout_is_never_sent_again(cfg, monkeypatch, hour):
    calls = _timeout(monkeypatch)
    lead = make_lead(state=WaqLead.STATE_CONFIRMATION_SENT, confirmation_sent_at=timezone.now() - timedelta(hours=4))
    assert gate(lead.id, KIND_REMINDER).status == "failed"
    # A second scheduler run (or a parallel one that waited on the lock).
    assert gate(lead.id, KIND_REMINDER).status == "skipped"
    assert len(calls) == 1
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT
    assert lead.reminder_attempted_at is not None


class WorkerKilled(BaseException):
    pass


def test_worker_dying_during_the_request_never_leads_to_a_resend(cfg, monkeypatch):
    from separator.waba import utils as waba_utils

    calls = []

    def dies(**kwargs):
        calls.append(kwargs)
        raise WorkerKilled()

    monkeypatch.setattr(waba_utils, "call_api", dies)
    lead = make_lead()
    with pytest.raises(WorkerKilled):
        gate(lead.id, KIND_CONFIRMATION)
    # Redelivered task: the attempt is on record, nothing is sent again.
    assert gate(lead.id, KIND_CONFIRMATION).status == "skipped"
    assert len(calls) == 1


def test_manual_stop_pressed_right_before_the_request_wins(cfg, graph, monkeypatch):
    real_build = gate_module.build_template_payload

    def build_then_stop(cfg_row, lead, kind):
        # The admin switches the bot off after the checks, before the POST.
        WaqSettings.objects.filter(pk=1).update(enabled=False)
        return real_build(cfg_row, lead, kind)

    monkeypatch.setattr(gate_module, "build_template_payload", build_then_stop)
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == gate_module.REASON_MANUAL_STOP
    assert graph.calls == []
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_BLOCKED
    assert WaqPhone.objects.get(phone=CUSTOMER).last_contacted_at is None


def test_reminder_window_is_rechecked_by_the_gate(cfg, graph, hour):
    hour["hours"] = [21]
    lead = make_lead(state=WaqLead.STATE_CONFIRMATION_SENT, confirmation_sent_at=timezone.now() - timedelta(hours=4))
    assert gate(lead.id, KIND_REMINDER).status == gate_module.REASON_OUTSIDE_WINDOW
    assert graph.calls == []
    lead.refresh_from_db()
    assert lead.reminder_attempted_at is None  # it can still go out tomorrow morning
