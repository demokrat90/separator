"""Inbound hook + the WABA webhook branches added for waq."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from separator.waba import utils as waba_utils
from separator.waba.models import Phone
from separator.waq import hooks, tasks
from separator.waq.models import WaqEvent, WaqLead

from .conftest import CUSTOMER, make_lead

pytestmark = pytest.mark.django_db


@pytest.fixture()
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks.process_reply, "apply_async", lambda *a, **kw: calls.append(kw))
    return calls


def inbound(text="Yes", sender_digits=CUSTOMER.lstrip("+"), message_id="wamid.IN1"):
    return {
        "metadata": {"display_phone_number": "15550100000", "phone_number_id": "test-phone-id-1"},
        "contacts": [{"wa_id": sender_digits, "profile": {"name": "Test Person"}}],
        "messages": [
            {
                "id": message_id,
                "from": sender_digits,
                "timestamp": "1791800000",
                "type": "text",
                "text": {"body": text},
            }
        ],
    }


def waiting_lead(**overrides):
    values = {
        "state": WaqLead.STATE_CONFIRMATION_SENT,
        "confirmation_sent_at": timezone.now() - timedelta(hours=1),
    }
    values.update(overrides)
    return make_lead(**values)


def test_unknown_number_queues_nothing(cfg, queued):
    assert hooks.handle_inbound_value(inbound(), cfg.phone) == ["not ours"]
    assert queued == []


def test_waiting_lead_gets_its_reply_queued(cfg, queued):
    lead = waiting_lead()
    assert hooks.handle_inbound_value(inbound(), cfg.phone) == ["queued"]
    kwargs = queued[0]
    assert kwargs["retry"] is False
    lead_id, message = kwargs["args"]
    assert lead_id == lead.id
    assert message == {
        "id": "wamid.IN1",
        "type": "text",
        "timestamp": "1791800000",
        "text": "Yes",
        "payload": None,
    }
    # The customer's profile name never travels into the task.
    assert "Test Person" not in json.dumps(kwargs["args"])


def test_qualified_or_old_leads_are_not_picked_up(cfg, queued):
    waiting_lead(state=WaqLead.STATE_QUALIFIED)
    waiting_lead(confirmation_sent_at=timezone.now() - timedelta(days=8))
    assert hooks.handle_inbound_value(inbound(), cfg.phone) == ["not ours"]
    assert queued == []


def test_reply_to_another_business_number_is_ignored(cfg, queued, sender):
    waiting_lead()
    other = Phone.objects.create(phone="+15550100099", phone_id="test-phone-id-2", waba=sender.waba)
    assert hooks.handle_inbound_value(inbound(), other) == ["other sender"]
    assert queued == []


def test_button_reply_is_slimmed(cfg, queued):
    waiting_lead()
    value = inbound()
    value["messages"][0] = {
        "id": "wamid.IN2",
        "from": CUSTOMER.lstrip("+"),
        "type": "button",
        "button": {"text": "Test Button Label", "payload": "waq:yes"},
        "context": {"id": "wamid.OUT"},
    }
    hooks.handle_inbound_value(value, cfg.phone)
    message = queued[0]["args"][1]
    assert message["type"] == "button"
    assert message["text"] == "Test Button Label"
    assert message["payload"] == "waq:yes"


def test_exception_inside_the_hook_never_escapes(cfg, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(WaqLead.objects, "filter", boom)
    assert hooks.handle_inbound_value(inbound(), cfg.phone) == ["error"]
    monkeypatch.setattr(hooks, "normalize_phone", boom)
    assert hooks.handle_inbound_value(inbound(), cfg.phone) == ["error"]


def test_queue_failure_never_escapes(cfg, monkeypatch):
    waiting_lead()

    def broker_down(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.process_reply, "apply_async", broker_down)
    assert hooks.handle_inbound_value(inbound(), cfg.phone) == ["error"]


def _webhook(field, value, waba_id="test-waba-1"):
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"id": waba_id, "changes": [{"field": field, "value": value}]}]})


@pytest.fixture()
def unsigned(settings, monkeypatch):
    settings.WABA_VERIFY_SIGNATURE = False
    from separator.attribution import tasks as attribution_tasks

    monkeypatch.setattr(attribution_tasks.resolve, "delay", lambda *a, **kw: None)


def test_failing_waq_hook_does_not_stop_the_bitrix_path(cfg, unsigned, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("waq exploded")

    monkeypatch.setattr(hooks, "handle_inbound_value", boom)
    result = waba_utils.event_processing(raw_body=_webhook("messages", inbound()))
    # The flow went past the hook and reached the open-line step.
    assert result == "WABA phone is not connected to Bitrix line"


def test_event_processing_calls_the_waq_hook(cfg, unsigned, queued):
    waiting_lead()
    waba_utils.event_processing(raw_body=_webhook("messages", inbound()))
    assert len(queued) == 1


def test_quality_update_is_recorded_not_raised(cfg, unsigned, alerts_sent):
    value = {"display_phone_number": "15550100000", "event": "FLAGGED", "current_limit": "TIER_1K"}
    result = waba_utils.event_processing(raw_body=_webhook("phone_number_quality_update", value))
    assert result == "phone quality update recorded"
    event = WaqEvent.objects.get(kind=WaqEvent.KIND_QUALITY)
    assert event.data["event"] == "FLAGGED"
    assert len(alerts_sent) == 1 and "FLAGGED" in alerts_sent[0]


def test_quality_update_survives_an_alert_failure(cfg, unsigned, monkeypatch):
    from separator.waq import alerts

    monkeypatch.setattr(alerts, "send_alert", lambda text: (_ for _ in ()).throw(RuntimeError("tg down")))
    value = {"display_phone_number": "15550100000", "event": "DOWNGRADE"}
    assert waba_utils.event_processing(raw_body=_webhook("phone_number_quality_update", value))


def _template_value(name, event="REJECTED"):
    return {
        "event": event,
        "message_template_id": 123456,
        "message_template_name": name,
        "message_template_language": "en",
        "reason": "INCORRECT_CATEGORY",
    }


def test_rejected_waq_template_is_recorded_not_raised(cfg, unsigned, alerts_sent):
    raw = _webhook("message_template_status_update", _template_value("request_details_offer"))
    result = waba_utils.event_processing(raw_body=raw)
    assert "waq template status recorded" in result
    event = WaqEvent.objects.get(kind=WaqEvent.KIND_TEMPLATE_STATUS)
    assert event.data["event"] == "REJECTED"
    assert len(alerts_sent) == 1 and "request_details_offer" in alerts_sent[0]


def test_paused_reminder_template_is_recorded(cfg, unsigned, alerts_sent):
    raw = _webhook("message_template_status_update", _template_value("request_reminder", "PAUSED"))
    waba_utils.event_processing(raw_body=raw)
    assert WaqEvent.objects.filter(kind=WaqEvent.KIND_TEMPLATE_STATUS).count() == 1


def test_other_templates_keep_the_old_behaviour(cfg, unsigned, alerts_sent):
    raw = _webhook("message_template_status_update", _template_value("someone_elses_template"))
    with pytest.raises(Exception):
        waba_utils.event_processing(raw_body=raw)
    assert WaqEvent.objects.count() == 0
    assert alerts_sent == []


# ------------------------------------------------------------ broker publish


def test_hook_publishes_with_short_broker_timeouts(cfg, monkeypatch):
    """A real kombu publish (in-memory broker): the message lands, and the
    connection the hook used carries 1 s timeouts and no publish retries."""
    waiting_lead()
    app = tasks.process_reply.app
    seen = {}
    real_connection_for_write = app.connection_for_write

    def recording_connection_for_write(*args, **kwargs):
        seen.update(kwargs.get("transport_options") or {})
        return real_connection_for_write("memory://", **kwargs)

    monkeypatch.setattr(app, "connection_for_write", recording_connection_for_write)
    published = []
    real_apply_async = tasks.process_reply.apply_async

    def recording_apply_async(*args, **kwargs):
        published.append(kwargs)
        return real_apply_async(*args, **kwargs)

    monkeypatch.setattr(tasks.process_reply, "apply_async", recording_apply_async)

    assert hooks.handle_inbound_value(inbound(), cfg.phone) == ["queued"]
    assert seen["socket_timeout"] == 1 and seen["socket_connect_timeout"] == 1
    assert seen["max_retries"] == 0 and seen["retry_on_timeout"] is False
    assert published[0]["retry"] is False
    assert published[0]["connection"] is not None


def test_reply_after_a_timed_out_confirmation_is_still_handled(cfg, monkeypatch, alerts_sent):
    """Meta may have delivered a confirmation whose request timed out: the
    customer's "Yes" must still reach process_reply and qualify the lead."""
    import requests

    from separator.waba import utils as waba_utils
    from separator.waq.gate import KIND_CONFIRMATION, gate

    def timed_out(**kwargs):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(waba_utils, "call_api", timed_out)
    lead = make_lead()
    assert gate(lead.id, KIND_CONFIRMATION).status == "failed"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_CONFIRMATION_UNKNOWN
    assert lead.confirmation_sent_at is None

    queued = []
    monkeypatch.setattr(tasks.process_reply, "apply_async", lambda *a, **kw: queued.append(kw["args"]))
    value = inbound()
    value["messages"][0] = {
        "id": "wamid.YES",
        "from": CUSTOMER.lstrip("+"),
        "type": "button",
        "button": {"text": "Test Button Label", "payload": "waq:yes"},
    }
    assert hooks.handle_inbound_value(value, cfg.phone) == ["queued"]

    monkeypatch.setattr(tasks, "_send_reply", lambda *a, **kw: tasks.REPLY_EMPTY)
    monkeypatch.setattr(tasks, "_update_deal", lambda *a, **kw: None)
    monkeypatch.setattr(tasks, "_timeline_comment", lambda *a, **kw: None)
    assert tasks.process_reply(*queued[0]) == "qualified"
    lead.refresh_from_db()
    assert lead.state == WaqLead.STATE_QUALIFIED


def test_definitely_failed_confirmation_is_not_waited_on(cfg, queued):
    make_lead(state=WaqLead.STATE_FAILED, confirmation_attempted_at=timezone.now())
    assert hooks.handle_inbound_value(inbound(), cfg.phone) == ["not ours"]
