"""POST /api/attr/click/ and GET /api/attr/health/."""

import pytest
from django.urls import reverse

from separator.attribution.models import ClickToken

pytestmark = pytest.mark.django_db

KEY = "test-attr-key"

PAYLOAD = {
    "site": "photon",
    "landing_url": "https://photon.estate/dubai/binghatti?utm_source=google",
    "referrer": "https://www.google.com/",
    "listing_id": "PH-204",
    "gclid": "Cj0KCQjw",
    "utm_source": "google",
    "utm_medium": "cpc",
    "utm_campaign": "dubai-offplan",
    "ga_client_id": "GA1.1.1234567.7654321",
    "ym_client_id": "1755600000123456",
    "fbp": "fb.1.1755600000.1234567890",
    "is_test": True,
}


def test_health_needs_no_key(client):
    response = client.get(reverse("attribution:health"))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_click_creates_token_and_wa_text(client, settings):
    settings.ATTR_API_KEY = KEY
    response = client.post(
        reverse("attribution:click"),
        data=PAYLOAD,
        content_type="application/json",
        headers={"x-attr-key": KEY},
    )
    assert response.status_code == 201
    body = response.json()

    token = ClickToken.objects.get(token=body["token"])
    assert body["wa_text"] == f"Hello Photon! I'm interested in PH-204. #{token.token}"
    assert token.site == "photon"
    assert token.gclid == "Cj0KCQjw"
    assert token.utm_campaign == "dubai-offplan"
    # Raw _ga cookie is trimmed to the client id; everything else is kept verbatim.
    assert token.ga_client_id == "1234567.7654321"
    assert token.is_test is True
    assert token.claimed_at is None and token.phone is None
    assert token.raw["landing_url"] == PAYLOAD["landing_url"]
    assert token.schema_version == 1


def test_click_without_listing_uses_generic_subject(client, settings):
    settings.ATTR_API_KEY = KEY
    response = client.post(
        reverse("attribution:click"),
        data={"site": "photon"},
        content_type="application/json",
        headers={"x-attr-key": KEY},
    )
    assert response.status_code == 201
    assert "interested in your properties." in response.json()["wa_text"]


def test_click_rejects_wrong_or_missing_key(client, settings):
    settings.ATTR_API_KEY = KEY
    url = reverse("attribution:click")

    assert client.post(url, data={}, content_type="application/json").status_code == 403
    wrong = client.post(
        url, data={}, content_type="application/json", headers={"x-attr-key": "nope"}
    )
    assert wrong.status_code == 403
    assert ClickToken.objects.count() == 0


def test_click_fails_closed_when_no_key_configured(client, settings):
    settings.ATTR_API_KEY = ""
    response = client.post(
        reverse("attribution:click"),
        data={},
        content_type="application/json",
        headers={"x-attr-key": ""},
    )
    assert response.status_code == 403


def test_click_is_rate_limited_per_ip(client, settings):
    settings.ATTR_API_KEY = KEY
    from django.core.cache import cache

    cache.clear()
    url = reverse("attribution:click")
    codes = set()
    for _ in range(62):
        codes.add(
            client.post(
                url, data={}, content_type="application/json", headers={"x-attr-key": KEY}
            ).status_code
        )
    assert codes == {201, 429}
    cache.clear()


def test_click_truncates_long_values_and_parses_booleans(client, settings):
    settings.ATTR_API_KEY = KEY
    response = client.post(
        reverse("attribution:click"),
        data={
            "site": "x" * 500,
            "gclid": "g" * 900,
            "landing_url": "https://photon.estate/" + "a" * 5000,
            "is_test": "false",
        },
        content_type="application/json",
        headers={"x-attr-key": KEY},
    )
    assert response.status_code == 201

    token = ClickToken.objects.get(token=response.json()["token"])
    assert len(token.site) == 64
    assert len(token.gclid) == 512
    assert len(token.landing_url) > 512  # TextField: kept whole
    assert token.is_test is False


def test_rate_limit_counts_the_proxy_seen_ip_not_the_forged_one(client, settings):
    settings.ATTR_API_KEY = KEY
    from django.core.cache import cache

    cache.clear()
    url = reverse("attribution:click")
    codes = set()
    for i in range(62):
        codes.add(
            client.post(
                url,
                data={},
                content_type="application/json",
                headers={"x-attr-key": KEY, "x-forwarded-for": f"1.2.3.{i}, 10.0.0.9"},
            ).status_code
        )
    # The spoofed first entry changes every time; the limit still bites.
    assert 429 in codes
    cache.clear()
