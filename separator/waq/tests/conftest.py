"""Shared fixtures. All numbers are fictional (NANP 555-01xx range is reserved
for fiction); no network call can leave a test."""

from datetime import timedelta

import pytest
from django.utils import timezone

from separator.waba.models import App, Phone, Waba
from separator.waq.models import WaqLead, WaqSettings

SENDER = "+15550100000"
CUSTOMER = "+15550100001"
CUSTOMER_2 = "+15550100002"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests

    def blocked(*args, **kwargs):
        raise RuntimeError("network is blocked in waq tests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture(autouse=True)
def no_waba_subscription(monkeypatch):
    # Waba post_save queues a Graph subscription on commit; not under test here.
    from separator.waba import tasks as waba_tasks

    monkeypatch.setattr(waba_tasks.waba_subscription, "delay", lambda *a, **kw: None)


@pytest.fixture()
def sender(db):
    app = App.objects.create(
        name="test-app", api_version=21, client_secret="test-secret", access_token="test-token"
    )
    waba = Waba.objects.create(app=app, waba_id="test-waba-1", access_token="test-token")
    return Phone.objects.create(phone=SENDER, phone_id="test-phone-id-1", waba=waba)


@pytest.fixture()
def cfg(sender):
    settings_row = WaqSettings.load()
    settings_row.enabled = True
    settings_row.phone = sender
    settings_row.project_fallback = "Test Project Fallback"
    settings_row.save()
    return settings_row


@pytest.fixture()
def graph(monkeypatch):
    """Fake Graph API: records every call, answers with a wamid."""
    from separator.waba import utils as waba_utils

    calls = []

    def fake_call_api(waba=None, endpoint=None, method="get", payload=None, **kwargs):
        calls.append({"endpoint": endpoint, "method": method, "payload": payload})
        if fake_call_api.error:
            raise Exception({"error": {"message": fake_call_api.error, "code": 131000}})
        return {"messages": [{"id": f"wamid.TEST{len(calls)}"}]}

    fake_call_api.error = None
    fake_call_api.calls = calls
    monkeypatch.setattr(waba_utils, "call_api", fake_call_api)
    return fake_call_api


@pytest.fixture()
def alerts_sent(monkeypatch):
    from separator.waq import alerts

    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda text: sent.append(text) or True)
    return sent


_deal_ids = iter(range(900001, 999999))


def make_lead(**overrides):
    values = {
        "deal_id": next(_deal_ids),
        "phone": CUSTOMER,
        "name": "Test Person",
        "project": "Test Project",
        "site": "test-site",
        "lang": "en",
        "form_type": "test_form",
        "wa_allowed": True,
        "lead_created_at": timezone.now() - timedelta(minutes=1),
        "payload": {},
    }
    values.update(overrides)
    return WaqLead.objects.create(**values)


@pytest.fixture()
def hour(monkeypatch):
    """Pin the lead's local hour (a list = successive answers, last one repeats)."""
    from separator.waq import localtime

    state = {"hours": [10]}

    def fake_local_hour(phone, now=None):
        hours = state["hours"]
        return hours.pop(0) if len(hours) > 1 else hours[0]

    monkeypatch.setattr(localtime, "local_hour", fake_local_hour)
    return state
