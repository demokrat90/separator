"""Inbound recording: wamid dedup, status events, first-inbound gating."""

from datetime import timedelta

import pytest
from django.utils import timezone

from separator.attribution import hooks, tasks
from separator.attribution.models import DealAttribution, MessageEvent, StatusEvent

pytestmark = pytest.mark.django_db

WAMID = "wamid.HBgMOTcxNTIxODkyODA5"


@pytest.fixture()
def queued(monkeypatch):
    """Capture resolve.delay instead of shipping it to a broker."""
    calls = []
    monkeypatch.setattr(tasks.resolve, "delay", lambda *a, **kw: calls.append((a, kw)))
    return calls


def inbound_value(wamid=WAMID, text="Hello! #K7QX9M", referral=None, timestamp="1755600000"):
    message = {
        "id": wamid,
        "from": "971521892809",
        "timestamp": timestamp,
        "type": "text",
        "text": {"body": text},
        "context": {"id": "wamid.PREVIOUS"},
    }
    if referral:
        message["referral"] = referral
    return {
        "contacts": [{"wa_id": "971521892809", "profile": {"name": "Aleksandr"}}],
        "messages": [message],
    }


def test_inbound_message_is_recorded_without_text(queued):
    hooks.handle_inbound_value(inbound_value(), app_instance_id="app-1", waba_phone_id="139")

    event = MessageEvent.objects.get()
    assert event.direction == MessageEvent.DIRECTION_IN
    assert event.author_type == MessageEvent.AUTHOR_CUSTOMER
    assert event.phone == "+971521892809"
    assert event.reply_to_id == "wamid.PREVIOUS"
    assert event.text_len == len("Hello! #K7QX9M")
    assert event.text_hash and "K7QX9M" not in event.text_hash
    # ts is Meta's timestamp, not the moment we processed the webhook.
    assert int(event.ts.timestamp()) == 1755600000
    assert len(queued) == 1


def test_duplicate_wamid_is_ignored_and_does_not_requeue(queued):
    hooks.handle_inbound_value(inbound_value(), app_instance_id="app-1")
    hooks.handle_inbound_value(inbound_value(), app_instance_id="app-1")

    assert MessageEvent.objects.count() == 1
    assert len(queued) == 1


def test_referral_is_stored_whole(queued):
    referral = {"source_id": "1202", "ctwa_clid": "ARBxyz", "headline": "Dubai"}
    hooks.handle_inbound_value(inbound_value(referral=referral))
    assert MessageEvent.objects.get().referral == referral


def test_second_message_within_30_days_does_not_start_a_new_attribution(queued):
    DealAttribution.objects.create(
        phone="+971521892809",
        attribution_source=DealAttribution.SOURCE_NONE,
        attribution_status=DealAttribution.STATUS_CODE_MISSING,
    )
    hooks.handle_inbound_value(inbound_value())
    assert MessageEvent.objects.count() == 1
    assert queued == []


def test_attribution_restarts_after_the_30_day_window(queued):
    DealAttribution.objects.create(
        phone="+971521892809",
        attribution_source=DealAttribution.SOURCE_NONE,
        attribution_status=DealAttribution.STATUS_CODE_MISSING,
    )
    DealAttribution.objects.update(created_at=timezone.now() - timedelta(days=31))
    hooks.handle_inbound_value(inbound_value())
    assert len(queued) == 1


def test_status_event_keeps_conversation_and_pricing(queued):
    value = {
        "statuses": [
            {
                "id": "wamid.OUT1",
                "status": "delivered",
                "timestamp": "1755600100",
                "recipient_id": "971521892809",
                "conversation": {
                    "id": "conv-1",
                    "origin": {"type": "referral_conversion"},
                    "expiration_timestamp": "1755686500",
                },
                "pricing": {
                    "billable": True,
                    "pricing_model": "CBP",
                    "category": "referral_conversion",
                },
            }
        ]
    }
    hooks.handle_inbound_value(value)

    status = StatusEvent.objects.get()
    assert status.status == "delivered"
    assert status.recipient_id == "+971521892809"
    assert status.conversation_id == "conv-1"
    assert status.conversation_origin_type == "referral_conversion"
    assert int(status.conversation_expiration_at.timestamp()) == 1755686500
    assert status.pricing["pricing_model"] == "CBP"
    assert int(status.ts.timestamp()) == 1755600100


def test_repeated_status_payload_is_deduplicated(queued):
    value = {"statuses": [{"id": "wamid.OUT1", "status": "sent", "timestamp": "1755600100"}]}
    hooks.handle_inbound_value(value)
    hooks.handle_inbound_value(value)
    assert StatusEvent.objects.count() == 1

    value["statuses"][0]["status"] = "read"
    hooks.handle_inbound_value(value)
    assert StatusEvent.objects.count() == 2


def test_broken_payload_never_raises(queued):
    assert hooks.handle_inbound_value({"messages": [{"no": "id"}]}) == ["no message id"]
    assert hooks.handle_inbound_value({}) == []
    assert MessageEvent.objects.count() == 0
