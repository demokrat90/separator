"""POST /api/waq/lead/: signature, validation, idempotency, blocked leads."""

import hashlib
import hmac
import json
import time

import pytest
from django.urls import reverse

from separator.waq import tasks
from separator.waq.models import WaqLead

from .conftest import CUSTOMER

pytestmark = pytest.mark.django_db

SECRET = "test-waq-secret"

BODY = {
    "deal_id": 4242,
    "phone": "+1 555 010 0001",
    "wa_allowed": True,
    "created_at": "2026-10-12T10:00:00Z",
    "name": "Test Person",
    "project": "Test Project",
    "page_url": "https://example.test/listing",
    "site": "test-site",
    "lang": "en",
    "form_type": "test_form",
    "preferred_channel": "whatsapp",
    "consent": {"whatsapp": True},
}


@pytest.fixture(autouse=True)
def _secret(settings):
    settings.WAQ_HMAC_SECRET = SECRET
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks.send_confirmation, "apply_async", lambda *a, **kw: calls.append((a, kw))
    )
    return calls


def sign(raw, ts=None, secret=SECRET, body_only=False, upper=False):
    ts = str(int(time.time()) if ts is None else ts)
    message = raw if body_only else ts.encode() + b"." + raw
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    if upper:
        digest = digest.upper()
    return {"x-photon-timestamp": ts, "x-photon-signature": f"sha256={digest}"}


def post(client, body=BODY, headers=None, raw=None):
    raw = raw if raw is not None else json.dumps(body).encode()
    return client.post(
        reverse("waq:lead"),
        data=raw,
        content_type="application/json",
        headers=headers if headers is not None else sign(raw),
    )


def test_valid_signature_queues_the_confirmation(client, queued, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        response = post(client)
    assert response.status_code == 202
    assert response.json() == {"status": "queued"}

    lead = WaqLead.objects.get(deal_id=4242)
    assert lead.phone == CUSTOMER  # normalized to E.164
    assert lead.state == WaqLead.STATE_RECEIVED
    assert lead.project == "Test Project"
    assert lead.preferred_channel == "whatsapp"
    assert lead.consent == {"whatsapp": True}
    assert lead.payload["deal_id"] == 4242
    assert lead.lead_created_at.isoformat().startswith("2026-10-12T10:00:00")
    assert queued == [((), {"args": [lead.id], "retry": False})]


def test_wrong_signature_is_rejected(client, queued):
    raw = json.dumps(BODY).encode()
    response = post(client, raw=raw, headers=sign(raw, secret="another-secret"))
    assert response.status_code == 401
    assert WaqLead.objects.count() == 0


def test_missing_headers_are_rejected(client, queued):
    response = post(client, headers={})
    assert response.status_code == 401


def test_old_timestamp_is_rejected(client, queued):
    raw = json.dumps(BODY).encode()
    response = post(client, raw=raw, headers=sign(raw, ts=int(time.time()) - 301))
    assert response.status_code == 401
    assert WaqLead.objects.count() == 0


def test_future_timestamp_is_rejected(client, queued):
    raw = json.dumps(BODY).encode()
    response = post(client, raw=raw, headers=sign(raw, ts=int(time.time()) + 301))
    assert response.status_code == 401


def test_signature_over_body_only_is_rejected(client, queued):
    raw = json.dumps(BODY).encode()
    response = post(client, raw=raw, headers=sign(raw, body_only=True))
    assert response.status_code == 401
    assert WaqLead.objects.count() == 0


def test_uppercase_hex_is_rejected(client, queued):
    raw = json.dumps(BODY).encode()
    response = post(client, raw=raw, headers=sign(raw, upper=True))
    assert response.status_code == 401


def test_empty_secret_rejects_everything(client, queued, settings):
    settings.WAQ_HMAC_SECRET = ""
    raw = json.dumps(BODY).encode()
    response = post(client, raw=raw, headers=sign(raw, secret=""))
    assert response.status_code == 503
    assert WaqLead.objects.count() == 0


def test_duplicate_deal_id_does_not_queue_a_second_template(
    client, queued, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        first = post(client)
    with django_capture_on_commit_callbacks(execute=True):
        second = post(client, body={**BODY, "phone": "+1 555 010 0002"})
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == {"status": "duplicate"}
    assert WaqLead.objects.count() == 1
    assert len(queued) == 1


def test_wa_not_allowed_is_recorded_as_blocked(client, queued, django_capture_on_commit_callbacks):
    body = {**BODY, "wa_allowed": False, "wa_reason": "prefers_email"}
    with django_capture_on_commit_callbacks(execute=True):
        response = post(client, body=body)
    assert response.status_code == 202
    assert response.json() == {"status": "blocked", "reason": "prefers_email"}
    lead = WaqLead.objects.get()
    assert lead.state == WaqLead.STATE_BLOCKED
    assert lead.block_reason == "prefers_email"
    assert queued == []


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        json.dumps([1, 2]).encode(),
        json.dumps({**BODY, "deal_id": None}).encode(),
        json.dumps({k: v for k, v in BODY.items() if k != "phone"}).encode(),
        json.dumps({k: v for k, v in BODY.items() if k != "wa_allowed"}).encode(),
        json.dumps({k: v for k, v in BODY.items() if k != "created_at"}).encode(),
        json.dumps({**BODY, "wa_allowed": "true"}).encode(),
        json.dumps({**BODY, "phone": "not a phone"}).encode(),
        json.dumps({**BODY, "deal_id": "abc"}).encode(),
        json.dumps({**BODY, "created_at": "yesterday"}).encode(),
    ],
)
def test_bad_bodies_are_400(client, queued, raw):
    response = post(client, raw=raw, headers=sign(raw))
    assert response.status_code == 400
    assert WaqLead.objects.count() == 0


def test_unix_created_at_is_accepted(client, queued):
    response = post(client, body={**BODY, "created_at": 1791800000})
    assert response.status_code == 202
    assert int(WaqLead.objects.get().lead_created_at.timestamp()) == 1791800000


def test_get_is_not_allowed(client):
    assert client.get(reverse("waq:lead")).status_code == 405


def test_rate_limited_per_ip(client, queued):
    codes = set()
    for i in range(62):
        codes.add(post(client, body={**BODY, "deal_id": 7000 + i}).status_code)
    assert codes == {202, 429}
