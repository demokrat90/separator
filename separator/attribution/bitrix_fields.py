"""Mapping of an attribution row to the UF_CRM_WA_* deal fields."""

from .models import SCHEMA_VERSION

# All user fields this app writes. Kept as a tuple so the deploy checklist and
# the runtime filter (crm.deal.fields) speak about the same list.
DEAL_FIELDS = (
    "UF_CRM_WA_PHONE",
    "UF_CRM_WA_FIRST_INBOUND_AT",
    "UF_CRM_WA_CLICK_TOKEN",
    "UF_CRM_WA_ATTR_SOURCE",
    "UF_CRM_WA_ATTR_STATUS",
    "UF_CRM_WA_GCLID",
    "UF_CRM_WA_GBRAID",
    "UF_CRM_WA_WBRAID",
    "UF_CRM_WA_YCLID",
    "UF_CRM_WA_FBCLID",
    "UF_CRM_WA_CTWA_CLID",
    "UF_CRM_WA_FBP",
    "UF_CRM_WA_FBC",
    "UF_CRM_WA_UTM_SOURCE",
    "UF_CRM_WA_UTM_MEDIUM",
    "UF_CRM_WA_UTM_CAMPAIGN",
    "UF_CRM_WA_UTM_CONTENT",
    "UF_CRM_WA_UTM_TERM",
    "UF_CRM_WA_LANDING_URL",
    "UF_CRM_WA_REFERRER",
    "UF_CRM_WA_LISTING_ID",
    "UF_CRM_WA_GA_CLIENT_ID",
    "UF_CRM_WA_YM_CLIENT_ID",
    "UF_CRM_WA_META_AD_ID",
    "UF_CRM_WA_META_SOURCE_TYPE",
    "UF_CRM_WA_META_HEADLINE",
    "UF_CRM_WA_IS_TEST",
    "UF_CRM_WA_SCHEMA_VERSION",
)


def build_deal_fields(attribution):
    """Bitrix payload for one DealAttribution. Empty values are omitted.

    Values are written as they arrived from the site / from Meta - no cleanup,
    no re-encoding; ga_client_id was already normalized on intake.
    """
    token = attribution.token
    referral = attribution.referral or {}

    fields = {
        "UF_CRM_WA_PHONE": attribution.phone,
        "UF_CRM_WA_ATTR_SOURCE": attribution.attribution_source,
        "UF_CRM_WA_ATTR_STATUS": attribution.attribution_status,
        "UF_CRM_WA_CTWA_CLID": referral.get("ctwa_clid"),
        "UF_CRM_WA_META_AD_ID": referral.get("source_id"),
        "UF_CRM_WA_META_SOURCE_TYPE": referral.get("source_type"),
        "UF_CRM_WA_META_HEADLINE": referral.get("headline"),
        "UF_CRM_WA_SCHEMA_VERSION": SCHEMA_VERSION,
    }

    if attribution.first_inbound_at:
        fields["UF_CRM_WA_FIRST_INBOUND_AT"] = attribution.first_inbound_at.isoformat()

    if token:
        fields.update(
            {
                "UF_CRM_WA_CLICK_TOKEN": token.token,
                "UF_CRM_WA_GCLID": token.gclid,
                "UF_CRM_WA_GBRAID": token.gbraid,
                "UF_CRM_WA_WBRAID": token.wbraid,
                "UF_CRM_WA_YCLID": token.yclid,
                "UF_CRM_WA_FBCLID": token.fbclid,
                "UF_CRM_WA_FBP": token.fbp,
                "UF_CRM_WA_FBC": token.fbc,
                "UF_CRM_WA_UTM_SOURCE": token.utm_source,
                "UF_CRM_WA_UTM_MEDIUM": token.utm_medium,
                "UF_CRM_WA_UTM_CAMPAIGN": token.utm_campaign,
                "UF_CRM_WA_UTM_CONTENT": token.utm_content,
                "UF_CRM_WA_UTM_TERM": token.utm_term,
                "UF_CRM_WA_LANDING_URL": token.landing_url,
                "UF_CRM_WA_REFERRER": token.referrer,
                "UF_CRM_WA_LISTING_ID": token.listing_id,
                "UF_CRM_WA_GA_CLIENT_ID": token.ga_client_id,
                "UF_CRM_WA_YM_CLIENT_ID": token.ym_client_id,
            }
        )

    fields = {key: value for key, value in fields.items() if value not in (None, "")}
    # Boolean: 0 is a value, not an empty one, so it is set after the filter.
    fields["UF_CRM_WA_IS_TEST"] = 1 if (token and token.is_test) else 0
    return fields
