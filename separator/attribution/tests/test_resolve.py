"""resolve(): the three attribution outcomes, plus the claim rules."""

from datetime import timedelta

import pytest
from django.utils import timezone

from separator.attribution.models import ClickToken, DealAttribution
from separator.attribution.tasks import resolve

pytestmark = pytest.mark.django_db

PHONE = "+971521892809"
REFERRAL = {
    "source_url": "https://fb.me/ad",
    "source_id": "120210000000000000",
    "source_type": "ad",
    "headline": "Dubai off-plan",
    "ctwa_clid": "ARBxyz",
}


def _resolve(code=None, referral=None, phone=PHONE, app_instance_id=None):
    """resolve() receives the already extracted code, never the message text."""
    return resolve(
        phone,
        f"wamid.{timezone.now().timestamp()}",
        code,
        referral,
        None,
        app_instance_id=app_instance_id,
    )


def test_meta_referral_wins_over_text():
    _resolve(code="ABC23", referral=REFERRAL)
    attribution = DealAttribution.objects.get()
    assert attribution.attribution_source == DealAttribution.SOURCE_META
    assert attribution.attribution_status == DealAttribution.STATUS_MATCHED
    assert attribution.referral["ctwa_clid"] == "ARBxyz"
    assert attribution.token is None


def test_site_code_matched_and_claimed():
    token = ClickToken.objects.create(token="K7QX9M", site="photon", gclid="Cj0KabC")
    _resolve(code="K7QX9M")

    token.refresh_from_db()
    attribution = DealAttribution.objects.get()
    assert attribution.attribution_source == DealAttribution.SOURCE_SITE
    assert attribution.attribution_status == DealAttribution.STATUS_MATCHED
    assert attribution.token_id == token.id
    assert token.claimed_at is not None
    assert token.phone == PHONE


def test_code_not_found_when_pattern_has_no_token():
    _resolve(code="ZZZZZZ")
    attribution = DealAttribution.objects.get()
    assert attribution.attribution_status == DealAttribution.STATUS_CODE_NOT_FOUND
    assert attribution.attribution_source == DealAttribution.SOURCE_NONE
    assert attribution.token is None


def test_code_missing_without_pattern():
    _resolve(code=None)
    attribution = DealAttribution.objects.get()
    assert attribution.attribution_status == DealAttribution.STATUS_CODE_MISSING
    assert attribution.attribution_source == DealAttribution.SOURCE_NONE


def test_second_claim_within_24h_does_not_create_a_second_row():
    ClickToken.objects.create(token="K7QX9M")
    _resolve(code="K7QX9M")
    _resolve(code="K7QX9M")
    assert DealAttribution.objects.count() == 1


def test_claim_after_24h_creates_a_new_row():
    ClickToken.objects.create(token="K7QX9M")
    _resolve(code="K7QX9M")
    DealAttribution.objects.update(created_at=timezone.now() - timedelta(hours=25))
    _resolve(code="K7QX9M")
    assert DealAttribution.objects.count() == 2
    # The token keeps its first claim; the reuse is recorded, not overwritten.
    token = ClickToken.objects.get(token="K7QX9M")
    assert token.phone == PHONE


def test_token_claimed_by_another_phone_is_still_attributed_but_flagged():
    token = ClickToken.objects.create(
        token="K7QX9M", claimed_at=timezone.now(), phone="+971500000000"
    )
    _resolve(code="K7QX9M")
    attribution = DealAttribution.objects.get()
    assert attribution.token_id == token.id
    assert attribution.attribution_status == DealAttribution.STATUS_MATCHED
    assert "already claimed" in (attribution.error or "")
    token.refresh_from_db()
    assert token.phone == "+971500000000"


def test_first_inbound_at_comes_from_meta_timestamp():
    resolve(PHONE, "wamid.1", None, None, "1755600000")
    attribution = DealAttribution.objects.get()
    assert int(attribution.first_inbound_at.timestamp()) == 1755600000


def test_push_is_queued_and_row_is_scoped_to_the_portal(monkeypatch):
    from separator.attribution import tasks

    queued = []
    monkeypatch.setattr(tasks.push_deal_attribution, "delay", lambda *a: queued.append(a))
    _resolve(code=None, app_instance_id="app-1")

    attribution = DealAttribution.objects.get()
    assert attribution.app_instance_id == "app-1"
    assert queued == [(attribution.id, "app-1")]


def test_another_portal_gets_its_own_attribution_for_the_same_phone(monkeypatch):
    from separator.attribution import tasks

    monkeypatch.setattr(tasks.push_deal_attribution, "delay", lambda *a: None)
    _resolve(app_instance_id="app-1")
    _resolve(app_instance_id="app-2")
    assert DealAttribution.objects.count() == 2
