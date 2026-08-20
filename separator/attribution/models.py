"""Attribution of WhatsApp leads.

The app is intentionally decoupled from separator.waba / separator.bitrix models:
it stores phone numbers and Bitrix ids as plain values so that a broken FK or a
deleted WABA entity can never take attribution data (or the message flow) down.
Message text is never stored - only its length and sha256 hash.
"""

from django.db import models

SCHEMA_VERSION = 1


class ClickToken(models.Model):
    """A code handed out to a site visitor before they open WhatsApp.

    No TTL: the code stays valid forever, `claimed_at` only records the first use.
    """

    token = models.CharField(max_length=16, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    phone = models.CharField(max_length=32, null=True, blank=True, db_index=True)

    site = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    landing_url = models.TextField(null=True, blank=True)
    referrer = models.TextField(null=True, blank=True)
    listing_id = models.CharField(max_length=128, null=True, blank=True)

    gclid = models.CharField(max_length=512, null=True, blank=True)
    gbraid = models.CharField(max_length=512, null=True, blank=True)
    wbraid = models.CharField(max_length=512, null=True, blank=True)
    yclid = models.CharField(max_length=512, null=True, blank=True)
    fbclid = models.CharField(max_length=512, null=True, blank=True)
    fbp = models.CharField(max_length=512, null=True, blank=True)
    fbc = models.CharField(max_length=512, null=True, blank=True)

    utm_source = models.CharField(max_length=512, null=True, blank=True)
    utm_medium = models.CharField(max_length=512, null=True, blank=True)
    utm_campaign = models.CharField(max_length=512, null=True, blank=True)
    utm_content = models.CharField(max_length=512, null=True, blank=True)
    utm_term = models.CharField(max_length=512, null=True, blank=True)

    ga_client_id = models.CharField(max_length=128, null=True, blank=True)
    ym_client_id = models.CharField(max_length=128, null=True, blank=True)

    is_test = models.BooleanField(default=False)
    raw = models.JSONField(null=True, blank=True)
    schema_version = models.PositiveIntegerField(default=SCHEMA_VERSION)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.token


class MessageEvent(models.Model):
    """One WhatsApp message, inbound or outbound. Text itself is never stored."""

    DIRECTION_IN = "in"
    DIRECTION_OUT = "out"
    DIRECTION_CHOICES = [(DIRECTION_IN, "in"), (DIRECTION_OUT, "out")]

    AUTHOR_CUSTOMER = "customer"
    AUTHOR_BOT = "bot"
    AUTHOR_HUMAN = "human_agent"
    AUTHOR_UNKNOWN = "unknown"
    AUTHOR_CHOICES = [
        (AUTHOR_CUSTOMER, "customer"),
        (AUTHOR_BOT, "bot"),
        (AUTHOR_HUMAN, "human_agent"),
        (AUTHOR_UNKNOWN, "unknown"),
    ]

    phone = models.CharField(max_length=32, db_index=True)
    chat_ref = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    # Time reported by Meta / Bitrix in the payload, NOT the time we received it.
    ts = models.DateTimeField(null=True, blank=True, db_index=True)
    direction = models.CharField(max_length=8, choices=DIRECTION_CHOICES)
    author_type = models.CharField(max_length=16, choices=AUTHOR_CHOICES)
    # wamid for WhatsApp messages, Bitrix message id for outbound connector events.
    message_id = models.CharField(max_length=255, unique=True)
    # id of the message this one replies to (WhatsApp `context.id`).
    reply_to_id = models.CharField(max_length=255, null=True, blank=True)
    text_len = models.PositiveIntegerField(default=0)
    text_hash = models.CharField(max_length=64, null=True, blank=True)
    # Meta CTWA referral object, stored as received.
    referral = models.JSONField(null=True, blank=True)
    # Raw identity fields the author_type was derived from (audit trail).
    raw_meta = models.JSONField(null=True, blank=True)
    deal_id = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["phone", "direction", "ts"])]

    def __str__(self):
        return f"{self.direction} {self.phone} {self.message_id}"


class StatusEvent(models.Model):
    """Delivery status of an outbound message (webhook `statuses[]`)."""

    message_id = models.CharField(max_length=255, db_index=True)
    status = models.CharField(max_length=32)
    ts = models.DateTimeField(null=True, blank=True, db_index=True)
    recipient_id = models.CharField(max_length=32, null=True, blank=True, db_index=True)

    conversation_id = models.CharField(max_length=128, null=True, blank=True)
    conversation_origin_type = models.CharField(max_length=64, null=True, blank=True)
    conversation_expiration_at = models.DateTimeField(null=True, blank=True)
    conversation = models.JSONField(null=True, blank=True)
    pricing = models.JSONField(null=True, blank=True)
    errors = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["message_id", "status"], name="attribution_status_unique"
            )
        ]

    def __str__(self):
        return f"{self.message_id} {self.status}"


class DealAttribution(models.Model):
    """One row per attributed deal (deal_id is filled asynchronously)."""

    SOURCE_META = "meta_referral"
    SOURCE_SITE = "site_code"
    SOURCE_NONE = "none"
    SOURCE_CHOICES = [
        (SOURCE_META, "meta_referral"),
        (SOURCE_SITE, "site_code"),
        (SOURCE_NONE, "none"),
    ]

    STATUS_MATCHED = "matched"
    STATUS_CODE_MISSING = "code_missing"
    STATUS_CODE_NOT_FOUND = "code_not_found"
    STATUS_CHOICES = [
        (STATUS_MATCHED, "matched"),
        (STATUS_CODE_MISSING, "code_missing"),
        (STATUS_CODE_NOT_FOUND, "code_not_found"),
    ]

    deal_id = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    phone = models.CharField(max_length=32, db_index=True)
    chat_ref = models.CharField(max_length=64, null=True, blank=True)
    first_inbound_at = models.DateTimeField(null=True, blank=True, db_index=True)
    attribution_source = models.CharField(max_length=32, choices=SOURCE_CHOICES)
    attribution_status = models.CharField(max_length=32, choices=STATUS_CHOICES)
    token = models.ForeignKey(
        ClickToken,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deal_attributions",
    )
    referral = models.JSONField(null=True, blank=True)
    pushed_to_bitrix_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["deal_id"],
                condition=models.Q(deal_id__isnull=False),
                name="attribution_deal_unique",
            )
        ]

    def __str__(self):
        return f"{self.phone} -> {self.deal_id or 'pending'}"
