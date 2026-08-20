"""Outbound identity: the row must end up keyed by the wamid, not by Bitrix."""

import pytest
from django.utils import timezone

from separator.attribution import hooks, tasks
from separator.attribution.models import DealAttribution, MessageEvent
from separator.bitrix.models import AppInstance, Bitrix

pytestmark = pytest.mark.django_db

PHONE = "+971521892809"
WAMID = "wamid.HBgMOTcxNTIxODkyODA5FQIAERgSQkRFMTFGODZCOEI2M0ZDMjI4AA=="
SEND_OK = {"messaging_product": "whatsapp", "messages": [{"id": WAMID}]}


@pytest.fixture()
def app_instance():
    portal = Bitrix.objects.create(member_id="m1", domain="photon.bitrix24.ae")
    return AppInstance.objects.create(portal=portal, auth_status="L")


@pytest.fixture()
def no_user_lookup(monkeypatch):
    monkeypatch.setattr(
        tasks, "call_method", lambda *a, **kw: {"result": [{"USER_TYPE": "employee"}]}
    )


def _record(app_instance, bitrix_message_id="2217"):
    tasks.record_outbound(
        app_instance_id=str(app_instance.id),
        phone=PHONE,
        chat_ref="37",
        bitrix_message_id=bitrix_message_id,
        text_len=3,
        bitrix_user_id="1",
        connector_code="gulin_x",
        line_id="3",
    )


def _attach(app_instance, bitrix_message_id="2217", wamid=WAMID, index=0):
    tasks.attach_wamid(
        app_instance_id=str(app_instance.id),
        bitrix_message_id=bitrix_message_id,
        wamid=wamid,
        phone=PHONE,
        index=index,
    )


def test_wamid_lands_on_the_row_recorded_before_the_send(app_instance, no_user_lookup):
    _record(app_instance)
    _attach(app_instance)

    event = MessageEvent.objects.get(direction=MessageEvent.DIRECTION_OUT)
    assert event.message_id == WAMID
    assert event.bitrix_message_id == "2217"
    assert event.author_type == MessageEvent.AUTHOR_HUMAN
    assert event.chat_ref == "37"


def test_order_of_the_two_tasks_does_not_matter(app_instance, no_user_lookup):
    """Graph can answer before the pre-send task has been picked up."""
    _attach(app_instance)
    _record(app_instance)

    assert MessageEvent.objects.count() == 1
    event = MessageEvent.objects.get()
    assert event.message_id == WAMID
    assert event.author_type == MessageEvent.AUTHOR_HUMAN


def test_statuses_join_to_the_outbound_message(app_instance, no_user_lookup):
    _record(app_instance)
    _attach(app_instance)
    hooks.handle_inbound_value(
        {"statuses": [{"id": WAMID, "status": "delivered", "timestamp": "1755700000",
                       "recipient_id": "971521892809",
                       "pricing": {"billable": False, "pricing_model": "PMP",
                                   "category": "service", "type": "free_customer_service"}}]}
    )
    from separator.attribution.models import StatusEvent

    status = StatusEvent.objects.get()
    assert MessageEvent.objects.filter(message_id=status.message_id).exists()
    # Per-message pricing sends no `conversation` object at all.
    assert status.conversation is None and status.conversation_id is None
    assert status.pricing["pricing_model"] == "PMP"
    assert status.raw["status"] == "delivered"
    assert status.raw["pricing"]["type"] == "free_customer_service"


def test_every_attachment_send_gets_its_own_wamid_row(app_instance, no_user_lookup):
    _record(app_instance)
    _attach(app_instance, wamid="wamid.FILE0", index=0)
    _attach(app_instance, wamid="wamid.FILE1", index=1)

    rows = MessageEvent.objects.order_by("bitrix_message_id")
    assert [r.bitrix_message_id for r in rows] == ["2217", "2217#1"]
    assert [r.message_id for r in rows] == ["wamid.FILE0", "wamid.FILE1"]
    # The sibling inherits what the base row knows about the author.
    assert {r.author_type for r in rows} == {MessageEvent.AUTHOR_HUMAN}


def test_send_error_leaves_the_row_without_a_wamid(app_instance, no_user_lookup):
    _record(app_instance)
    assert hooks.handle_outbound_sent(
        app_instance_id=str(app_instance.id), bitrix_message_id="2217", chat=PHONE,
        send_result={"error": {"message": "Unsupported post request"}},
    ) == "no wamid"
    assert MessageEvent.objects.get().message_id is None


def test_wamid_extraction():
    assert hooks.wamid_from_send_result(SEND_OK) == WAMID
    assert hooks.wamid_from_send_result({"error": {"code": 100}}) is None
    assert hooks.wamid_from_send_result({"messages": []}) is None
    assert hooks.wamid_from_send_result(None) is None


def test_events_are_stamped_with_the_current_deal(app_instance, no_user_lookup):
    DealAttribution.objects.create(
        phone=PHONE,
        app_instance_id=str(app_instance.id),
        deal_id="617",
        attribution_source=DealAttribution.SOURCE_SITE,
        attribution_status=DealAttribution.STATUS_MATCHED,
        push_state=DealAttribution.PUSH_DONE,
        pushed_to_bitrix_at=timezone.now(),
    )

    hooks.handle_inbound_value(
        {"contacts": [{"wa_id": "971521892809"}],
         "messages": [{"id": "wamid.IN1", "from": "971521892809", "timestamp": "1755700000",
                       "type": "text", "text": {"body": "hi"}}]},
        app_instance_id=str(app_instance.id),
    )
    _record(app_instance)

    assert MessageEvent.objects.get(message_id="wamid.IN1").deal_id == "617"
    assert MessageEvent.objects.get(direction=MessageEvent.DIRECTION_OUT).deal_id == "617"


def test_unpushed_attribution_does_not_stamp_a_deal(app_instance, no_user_lookup):
    DealAttribution.objects.create(
        phone=PHONE,
        app_instance_id=str(app_instance.id),
        deal_id="617",
        attribution_source=DealAttribution.SOURCE_SITE,
        attribution_status=DealAttribution.STATUS_MATCHED,
        push_state=DealAttribution.PUSH_PENDING,
    )
    _record(app_instance)
    assert MessageEvent.objects.get().deal_id is None
