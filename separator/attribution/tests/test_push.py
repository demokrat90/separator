"""push_deal_attribution(): deal lookup, field payload, error tolerance."""

from datetime import timedelta

import pytest
from django.utils import timezone

from separator.attribution import tasks
from separator.attribution.bitrix_fields import build_deal_fields
from separator.attribution.models import ClickToken, DealAttribution
from separator.bitrix.models import AppInstance, Bitrix

pytestmark = pytest.mark.django_db

PHONE = "+971521892809"
ALL_FIELDS = {code: {} for code in tasks.DEAL_FIELDS}


@pytest.fixture()
def app_instance():
    portal = Bitrix.objects.create(member_id="m1", domain="photon.bitrix24.ae")
    return AppInstance.objects.create(portal=portal, auth_status="L")


@pytest.fixture()
def attribution():
    token = ClickToken.objects.create(
        token="K7QX9M",
        site="photon",
        listing_id="PH-204",
        gclid="Cj0KCQjw",
        utm_source="google",
        landing_url="https://photon.estate/x",
        ga_client_id="1234567.7654321",
        is_test=True,
    )
    return DealAttribution.objects.create(
        phone=PHONE,
        first_inbound_at=timezone.now(),
        app_instance_id="app-1",
        attribution_source=DealAttribution.SOURCE_SITE,
        attribution_status=DealAttribution.STATUS_MATCHED,
        token=token,
    )


def fake_bitrix(responses, calls):
    def _call(app_instance, method, data=None, **kwargs):
        calls.append((method, data))
        value = responses.get(method)
        return value(data) if callable(value) else value

    return _call


def test_deal_fields_payload_skips_empty_and_keeps_booleans(attribution):
    fields = build_deal_fields(attribution)
    assert fields["UF_CRM_WA_PHONE"] == PHONE
    assert fields["UF_CRM_WA_CLICK_TOKEN"] == "K7QX9M"
    assert fields["UF_CRM_WA_ATTR_SOURCE"] == "site_code"
    assert fields["UF_CRM_WA_ATTR_STATUS"] == "matched"
    assert fields["UF_CRM_WA_GCLID"] == "Cj0KCQjw"
    assert fields["UF_CRM_WA_IS_TEST"] == 1
    assert fields["UF_CRM_WA_SCHEMA_VERSION"] == 1
    assert "UF_CRM_WA_YCLID" not in fields  # empty values are not sent
    assert "T" in fields["UF_CRM_WA_FIRST_INBOUND_AT"]  # ISO datetime


def test_push_writes_fields_to_the_freshly_created_deal(monkeypatch, app_instance, attribution):
    calls = []
    monkeypatch.setattr(
        tasks,
        "call_method",
        fake_bitrix(
            {
                "crm.duplicate.findbycomm": {"result": {"CONTACT": [9014]}},
                "crm.deal.list": {
                    "result": [{"ID": "617", "DATE_CREATE": timezone.now().isoformat()}]
                },
                "imopenlines.crm.chat.getLastId": {"result": 1234},
                "crm.deal.fields": {"result": ALL_FIELDS},
                "crm.deal.update": {"result": True},
            },
            calls,
        ),
    )

    tasks.push_deal_attribution(attribution.id, str(app_instance.id))

    attribution.refresh_from_db()
    assert attribution.deal_id == "617"
    assert attribution.chat_ref == "1234"
    assert attribution.pushed_to_bitrix_at is not None
    assert attribution.error is None

    update = dict(calls)["crm.deal.update"]
    assert update["id"] == "617"
    assert update["fields"]["UF_CRM_WA_CLICK_TOKEN"] == "K7QX9M"


def test_unknown_portal_field_is_dropped_not_fatal(monkeypatch, app_instance, attribution):
    calls = []
    known = {code: {} for code in tasks.DEAL_FIELDS if code != "UF_CRM_WA_GCLID"}
    monkeypatch.setattr(
        tasks,
        "call_method",
        fake_bitrix(
            {
                "crm.duplicate.findbycomm": {"result": {"CONTACT": [9014]}},
                "crm.deal.list": {
                    "result": [{"ID": "617", "DATE_CREATE": timezone.now().isoformat()}]
                },
                "imopenlines.crm.chat.getLastId": {"result": None},
                "crm.deal.fields": {"result": known},
                "crm.deal.update": {"result": True},
            },
            calls,
        ),
    )

    tasks.push_deal_attribution(attribution.id, str(app_instance.id))

    attribution.refresh_from_db()
    assert attribution.deal_id == "617"
    assert "UF_CRM_WA_GCLID" in attribution.error
    assert "UF_CRM_WA_GCLID" not in dict(calls)["crm.deal.update"]["fields"]


def test_bitrix_error_is_stored_and_does_not_raise(monkeypatch, app_instance, attribution):
    calls = []
    monkeypatch.setattr(
        tasks,
        "call_method",
        fake_bitrix(
            {
                "crm.duplicate.findbycomm": {"result": {"CONTACT": [9014]}},
                "crm.deal.list": {
                    "result": [{"ID": "617", "DATE_CREATE": timezone.now().isoformat()}]
                },
                "imopenlines.crm.chat.getLastId": {"result": None},
                "crm.deal.fields": {"result": ALL_FIELDS},
                "crm.deal.update": {
                    "error": "ERROR_CORE",
                    "error_description": "Unknown field UF_CRM_WA_PHONE",
                },
            },
            calls,
        ),
    )

    tasks.push_deal_attribution(attribution.id, str(app_instance.id))

    attribution.refresh_from_db()
    assert attribution.pushed_to_bitrix_at is None
    assert "Unknown field" in attribution.error


def test_missing_deal_retries(monkeypatch, app_instance, attribution):
    calls = []
    monkeypatch.setattr(
        tasks,
        "call_method",
        fake_bitrix({"crm.duplicate.findbycomm": {"result": {}}}, calls),
    )
    retries = []
    monkeypatch.setattr(
        tasks.push_deal_attribution,
        "retry",
        lambda **kw: retries.append(kw) or RuntimeError("retry"),
    )

    with pytest.raises(RuntimeError):
        tasks.push_deal_attribution(attribution.id, str(app_instance.id))

    assert retries and retries[0]["countdown"] == 30
    attribution.refresh_from_db()
    assert attribution.deal_id is None


def test_newest_deal_across_all_contacts_wins(monkeypatch, app_instance, attribution):
    calls = []
    now = timezone.now()

    def deal_list(data):
        contact = data["filter"]["CONTACT_ID"]
        if contact == 9014:
            return {"result": [{"ID": "500", "DATE_CREATE": (now - timedelta(minutes=5)).isoformat()}]}
        return {"result": [{"ID": "617", "DATE_CREATE": now.isoformat()}]}

    monkeypatch.setattr(
        tasks,
        "call_method",
        fake_bitrix(
            {
                "crm.duplicate.findbycomm": {"result": {"CONTACT": [9014, 9015]}},
                "crm.deal.list": deal_list,
                "imopenlines.crm.chat.getLastId": {"result": None},
                "crm.deal.fields": {"result": ALL_FIELDS},
                "crm.deal.update": {"result": True},
            },
            calls,
        ),
    )

    tasks.push_deal_attribution(attribution.id, str(app_instance.id))
    attribution.refresh_from_db()
    assert attribution.deal_id == "617"


def test_deal_older_than_the_conversation_is_never_written(monkeypatch, app_instance, attribution):
    """A pre-existing deal belongs to somebody else - wait, then give up."""
    calls = []
    stale = (timezone.now() - timedelta(days=5)).isoformat()
    monkeypatch.setattr(
        tasks,
        "call_method",
        fake_bitrix(
            {
                "crm.duplicate.findbycomm": {"result": {"CONTACT": [9014]}},
                "crm.deal.list": {"result": [{"ID": "42", "DATE_CREATE": stale}]},
            },
            calls,
        ),
    )
    retries = []
    monkeypatch.setattr(
        tasks.push_deal_attribution,
        "retry",
        lambda **kw: retries.append(kw) or RuntimeError("retry"),
    )

    with pytest.raises(RuntimeError):
        tasks.push_deal_attribution(attribution.id, str(app_instance.id))

    assert retries
    assert "crm.deal.update" not in dict(calls)
    attribution.refresh_from_db()
    assert attribution.deal_id is None


def test_sweeper_requeues_lost_work(monkeypatch, app_instance):
    from separator.attribution.models import MessageEvent

    resolved, pushed = [], []
    monkeypatch.setattr(tasks.resolve, "delay", lambda *a, **kw: resolved.append((a, kw)))
    monkeypatch.setattr(tasks.push_deal_attribution, "delay", lambda *a: pushed.append(a))

    event = MessageEvent.objects.create(
        phone=PHONE,
        app_instance_id=str(app_instance.id),
        direction=MessageEvent.DIRECTION_IN,
        author_type=MessageEvent.AUTHOR_CUSTOMER,
        message_id="wamid.LOST",
        raw_meta={"click_code": "K7QX9M", "resolve_pending": True},
    )
    stuck = DealAttribution.objects.create(
        phone=PHONE,
        app_instance_id=str(app_instance.id),
        attribution_source=DealAttribution.SOURCE_NONE,
        attribution_status=DealAttribution.STATUS_CODE_MISSING,
    )
    old = timezone.now() - timedelta(hours=1)
    MessageEvent.objects.filter(pk=event.pk).update(created_at=old)
    DealAttribution.objects.filter(pk=stuck.pk).update(created_at=old)

    assert tasks.sweep_pending_attributions() == {"resolve": 1, "push": 1}
    assert resolved[0][0][2] == "K7QX9M"
    assert pushed == [(stuck.id, str(app_instance.id))]

    # Already swept: neither stage may be queued a second time (the lease holds).
    resolved.clear()
    pushed.clear()
    assert tasks.sweep_pending_attributions() == {"resolve": 0, "push": 0}
    assert resolved == [] and pushed == []
    stuck.refresh_from_db()
    assert stuck.push_state == DealAttribution.PUSH_QUEUED


def test_terminal_rows_are_not_swept_again(monkeypatch, app_instance):
    pushed = []
    monkeypatch.setattr(tasks.push_deal_attribution, "delay", lambda *a: pushed.append(a))
    failed = DealAttribution.objects.create(
        phone=PHONE,
        app_instance_id=str(app_instance.id),
        attribution_source=DealAttribution.SOURCE_NONE,
        attribution_status=DealAttribution.STATUS_CODE_MISSING,
        push_state=DealAttribution.PUSH_FAILED,
        error="contact/deal not found after 11 attempts",
    )
    DealAttribution.objects.filter(pk=failed.pk).update(
        created_at=timezone.now() - timedelta(hours=1)
    )
    assert tasks.sweep_pending_attributions()["push"] == 0
    assert pushed == []


def test_push_marks_terminal_state(monkeypatch, app_instance, attribution):
    calls = []
    monkeypatch.setattr(
        tasks,
        "call_method",
        fake_bitrix(
            {
                "crm.duplicate.findbycomm": {"result": {"CONTACT": [9014]}},
                "crm.deal.list": {
                    "result": [{"ID": "617", "DATE_CREATE": timezone.now().isoformat()}]
                },
                "imopenlines.crm.chat.getLastId": {"result": None},
                "crm.deal.fields": {"result": ALL_FIELDS},
                "crm.deal.update": {"result": True},
            },
            calls,
        ),
    )
    tasks.push_deal_attribution(attribution.id, str(app_instance.id))
    attribution.refresh_from_db()
    assert attribution.push_state == DealAttribution.PUSH_DONE


def test_outbound_event_records_the_author_rule_version(monkeypatch, app_instance):
    from separator.attribution.models import AUTHOR_RULE_VERSION, MessageEvent

    monkeypatch.setattr(
        tasks, "call_method", fake_bitrix({"user.get": {"result": [{"USER_TYPE": "employee"}]}}, [])
    )
    tasks.record_outbound(
        app_instance_id=str(app_instance.id),
        phone=PHONE,
        bitrix_message_id="42",
        bitrix_user_id="9",
    )
    event = MessageEvent.objects.get(direction=MessageEvent.DIRECTION_OUT)
    assert event.author_type == MessageEvent.AUTHOR_HUMAN
    assert event.author_rule_version == AUTHOR_RULE_VERSION == "usertype-v1"
