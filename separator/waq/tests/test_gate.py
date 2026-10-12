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
    assert WaqPhone.objects.get(phone=CUSTOMER).last_contacted_at == lead.confirmation_sent_at
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
    assert lead.state == WaqLead.STATE_RECEIVED


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
    for i in range(count):
        make_lead(phone=f"+1555010{1000 + i}", state=WaqLead.STATE_CONFIRMATION_SENT, **{field: timezone.now() - ago})


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


def test_reminder_uses_the_reminder_template(cfg, graph):
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
