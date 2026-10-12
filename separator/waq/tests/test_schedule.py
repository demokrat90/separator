"""run_schedule(): reminder timing, local window, no repeats, no-answer marking."""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.utils import timezone

from separator.waq import localtime, tasks
from separator.waq.models import WaqEvent, WaqLead

from .conftest import make_lead

pytestmark = pytest.mark.django_db


def sent_lead(minutes_ago, **overrides):
    values = {
        "state": WaqLead.STATE_CONFIRMATION_SENT,
        "confirmation_sent_at": timezone.now() - timedelta(minutes=minutes_ago),
    }
    values.update(overrides)
    return make_lead(**values)


def test_reminder_after_180_minutes_inside_the_window(cfg, graph, hour):
    lead = sent_lead(181)
    result = tasks.run_schedule()
    assert result["sent"] == 1
    assert graph.calls[0]["payload"]["template"]["name"] == "request_reminder"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_REMINDER_SENT


def test_no_reminder_before_180_minutes(cfg, graph, hour):
    sent_lead(179)
    tasks.run_schedule()
    assert graph.calls == []


@pytest.mark.parametrize("local", [8, 20, 23, 0])
def test_no_reminder_outside_9_to_20(cfg, graph, hour, local):
    hour["hours"] = [local]
    sent_lead(200)
    assert tasks.run_schedule()["outside_window"] == 1
    assert graph.calls == []


@pytest.mark.parametrize("local", [9, 19])
def test_window_edges_are_inside(cfg, graph, hour, local):
    hour["hours"] = [local]
    sent_lead(200)
    tasks.run_schedule()
    assert len(graph.calls) == 1


def test_reminder_is_never_repeated(cfg, graph, hour):
    sent_lead(200)
    tasks.run_schedule()
    tasks.run_schedule()
    assert len(graph.calls) == 1


def test_replied_lead_gets_no_reminder(cfg, graph, hour):
    sent_lead(200, state=WaqLead.STATE_QUALIFIED, replied_at=timezone.now())
    tasks.run_schedule()
    assert graph.calls == []


def test_reminder_respects_the_manual_stop(cfg, graph, hour):
    cfg.enabled = False
    cfg.save()
    lead = sent_lead(200)
    tasks.run_schedule()
    assert graph.calls == []
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT


def test_failed_reminder_keeps_waiting_and_is_not_retried(cfg, graph, hour):
    graph.error = "temporary failure"
    lead = sent_lead(200)
    tasks.run_schedule()
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT  # the reply is still awaited
    assert WaqEvent.objects.filter(kind=WaqEvent.KIND_SEND_ERROR).count() == 1
    graph.error = None
    tasks.run_schedule()
    assert len(graph.calls) == 1  # never retried


def test_no_answer_after_24_hours(cfg, graph, hour):
    first = sent_lead(24 * 60 + 1)
    second = sent_lead(25 * 60, state=WaqLead.STATE_REMINDER_SENT, reminder_sent_at=timezone.now())
    fresh = sent_lead(23 * 60)
    assert tasks.run_schedule()["no_answer"] == 2
    for lead in (first, second):
        lead.refresh_from_db()
        assert lead.state == WaqLead.STATE_NO_ANSWER
    fresh.refresh_from_db()
    assert fresh.state != WaqLead.STATE_NO_ANSWER


def test_local_time_zone_from_the_number():
    assert str(localtime.lead_timezone("+971500000000")) == "Asia/Dubai"
    assert str(localtime.lead_timezone("+12125550100")).startswith("America/")
    # Unknown or unparseable -> Dubai.
    assert str(localtime.lead_timezone("+15550100001")) == "Asia/Dubai"
    assert str(localtime.lead_timezone("garbage")) == "Asia/Dubai"


def test_local_hour_uses_the_zone():
    noon_utc = datetime(2026, 10, 12, 12, 0, tzinfo=dt_timezone.utc)
    assert localtime.local_hour("+971500000000", noon_utc) == 16


def test_window_closing_during_the_run_stops_the_reminder(cfg, graph, hour):
    # 19:59 when the scheduler looks, 20:00 when the gate is about to send.
    hour["hours"] = [19, 20]
    lead = sent_lead(200)
    assert tasks.run_schedule().get("outside_window") == 1
    assert graph.calls == []
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_SENT


@pytest.mark.parametrize("label_message", ["who", "company", "media", "sticker", "unclassified"])
def test_any_answer_cancels_the_reminder_and_no_answer(cfg, graph, hour, monkeypatch, settings, label_message):
    from separator.waq import classify

    settings.ANTHROPIC_API_KEY = ""
    monkeypatch.setattr(tasks, "_timeline_comment", lambda *a, **kw: None)
    monkeypatch.setattr(tasks, "_show_in_open_line", lambda *a, **kw: None)
    monkeypatch.setattr(tasks.alerts, "send_alert", lambda text: True)
    if label_message in ("who", "company"):
        label = classify.WHO if label_message == "who" else classify.COMPANY
        monkeypatch.setattr(classify, "classify_text", lambda text, project: label)
    message = {
        "who": {"id": "wamid.W", "type": "text", "text": "who is this"},
        "company": {"id": "wamid.C", "type": "text", "text": "which company"},
        "media": {"id": "wamid.M", "type": "audio"},
        "sticker": {"id": "wamid.S", "type": "sticker"},
        "unclassified": {"id": "wamid.U", "type": "text", "text": "hm"},
    }[label_message]
    lead = sent_lead(200)
    tasks.process_reply(lead.id, message)
    lead.refresh_from_db()
    assert lead.replied_at is not None
    graph.calls.clear()
    tasks.run_schedule()
    assert [c for c in graph.calls if c["payload"]["type"] == "template"] == []
    old = sent_lead(25 * 60, phone="+15550100009")
    tasks.process_reply(old.id, {**message, "id": message["id"] + "-old"})
    tasks.run_schedule()
    old.refresh_from_db()
    assert old.state != WaqLead.STATE_NO_ANSWER


def test_reminders_off_send_nothing_but_no_answer_still_works(cfg, graph, hour):
    cfg.reminder_enabled = False
    cfg.save()
    due = sent_lead(200)
    old = sent_lead(25 * 60, phone="+15550100008")
    result = tasks.run_schedule()
    assert result["no_answer"] == 1
    assert result["reminders"] == "disabled"  # the scheduler does not even ask the gate
    assert graph.calls == []
    due.refresh_from_db()
    old.refresh_from_db()
    assert due.state == WaqLead.STATE_CONFIRMATION_SENT
    assert old.state == WaqLead.STATE_NO_ANSWER


def test_no_late_reminder_after_its_time_frame(cfg, graph, hour):
    # The evening window closed at 3 h; by the next morning it is too late.
    sent_lead(5 * 60)
    tasks.run_schedule()
    assert graph.calls == []


def test_unknown_outcome_lead_ends_as_no_answer(cfg, graph, hour):
    lead = make_lead(
        state=WaqLead.STATE_CONFIRMATION_UNKNOWN,
        confirmation_attempted_at=timezone.now() - timedelta(hours=25),
    )
    tasks.run_schedule()
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_NO_ANSWER
