"""run_schedule(): reminder timing, local window, no repeats, no-answer marking."""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.utils import timezone

from separator.waq import tasks
from separator.waq.models import WaqEvent, WaqLead

from .conftest import make_lead

pytestmark = pytest.mark.django_db


@pytest.fixture()
def hour(monkeypatch):
    """Pin the lead's local hour."""
    state = {"hour": 10}
    monkeypatch.setattr(tasks, "local_hour", lambda phone, now=None: state["hour"])
    return state


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
    hour["hour"] = local
    sent_lead(200)
    assert tasks.run_schedule()["outside_window"] == 1
    assert graph.calls == []


@pytest.mark.parametrize("local", [9, 19])
def test_window_edges_are_inside(cfg, graph, hour, local):
    hour["hour"] = local
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
    assert tasks.run_schedule().get("failed_before") == 1
    assert len(graph.calls) == 1


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
    assert str(tasks.lead_timezone("+971500000000")) == "Asia/Dubai"
    assert str(tasks.lead_timezone("+12125550100")).startswith("America/")
    # Unknown or unparseable -> Dubai.
    assert str(tasks.lead_timezone("+15550100001")) == "Asia/Dubai"
    assert str(tasks.lead_timezone("garbage")) == "Asia/Dubai"


def test_local_hour_uses_the_zone():
    noon_utc = datetime(2026, 10, 12, 12, 0, tzinfo=dt_timezone.utc)
    assert tasks.local_hour("+971500000000", noon_utc) == 16
