"""WhatsApp lead qualification (waq).

A site sends a lead -> one confirmation template -> at most one reminder ->
the customer's reply is classified and acted upon in Bitrix.

The fork is public: nothing customer facing lives in code. Reply texts, the
responsible user, the stage and the alert chat are configured in the admin
(WaqSettings, a singleton), and all of them default to empty.
"""

from django.db import models
from django.utils import timezone


class WaqSettings(models.Model):
    SINGLETON_PK = 1

    # Manual stop. False = nothing is sent to Meta, leads are only recorded.
    enabled = models.BooleanField(default=False)
    # Automatic stop (too many negative replies). Cleared only by hand.
    autostop_active = models.BooleanField(default=False)
    autostop_reason = models.CharField(max_length=255, blank=True, default="")
    autostop_at = models.DateTimeField(null=True, blank=True)

    hourly_cap = models.PositiveIntegerField(default=10)
    daily_cap = models.PositiveIntegerField(default=30)

    phone = models.ForeignKey(
        "waba.Phone",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Sender number for the templates and the bot replies",
    )
    template_confirmation = models.CharField(max_length=255, default="request_details_offer")
    template_reminder = models.CharField(max_length=255, default="request_reminder")
    template_language = models.CharField(max_length=16, default="en")
    # Fallback recognition of the template's quick-reply buttons by their
    # visible text, one text per line (the payload is checked first).
    button_yes_texts = models.TextField(blank=True, default="", help_text="One text per line")
    button_no_texts = models.TextField(blank=True, default="", help_text="One text per line")
    project_fallback = models.CharField(
        max_length=255, blank=True, default="", help_text="Template {{1}} when the lead has no project"
    )

    # Reminders are off until switched on in the admin.
    reminder_enabled = models.BooleanField(default=False)
    reminder_after_min = models.PositiveIntegerField(default=180)
    # No reminder later than this after the confirmation (e.g. not the next
    # morning when the evening window closed in between).
    reminder_before_min = models.PositiveIntegerField(default=240)
    reminder_window_start = models.PositiveSmallIntegerField(default=9)
    reminder_window_end = models.PositiveSmallIntegerField(default=20)
    no_answer_after_hours = models.PositiveIntegerField(default=24)

    responsible_user_id = models.PositiveIntegerField(null=True, blank=True)
    qualified_stage_id = models.CharField(max_length=64, blank=True, default="")
    deal_category_id = models.PositiveIntegerField(default=0)

    # Bot replies. Empty text = the reply is not sent, the actions still run.
    reply_qual = models.TextField(blank=True, default="")
    reply_qual_question = models.TextField(blank=True, default="")
    reply_bot = models.TextField(blank=True, default="")
    reply_not_me = models.TextField(blank=True, default="")
    reply_stop = models.TextField(blank=True, default="")
    reply_who = models.TextField(blank=True, default="", help_text="{{1}} is replaced with the project")
    reply_company = models.TextField(blank=True, default="")

    alert_chat_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        verbose_name = "WAQ settings"
        verbose_name_plural = "WAQ settings"

    def __str__(self):
        return "WAQ settings"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj

    def template_for(self, kind):
        return self.template_confirmation if kind == "confirmation" else self.template_reminder


class WaqLead(models.Model):
    STATE_RECEIVED = "received"
    STATE_BLOCKED = "blocked"
    STATE_HELD = "held"
    STATE_CONFIRMATION_SENT = "confirmation_sent"
    # The request timed out / failed without a definite rejection: Meta may
    # have delivered it, so replies are still handled.
    STATE_CONFIRMATION_UNKNOWN = "confirmation_unknown"
    STATE_REMINDER_SENT = "reminder_sent"
    STATE_QUALIFIED = "qualified"
    STATE_NOT_LEAD = "not_lead"
    STATE_STOPPED = "stopped"
    STATE_NO_ANSWER = "no_answer"
    STATE_FAILED = "failed"
    STATE_CHOICES = [
        (STATE_RECEIVED, "received"),
        (STATE_BLOCKED, "blocked"),
        (STATE_HELD, "held (cap, manual review)"),
        (STATE_CONFIRMATION_SENT, "confirmation sent"),
        (STATE_CONFIRMATION_UNKNOWN, "confirmation outcome unknown"),
        (STATE_REMINDER_SENT, "reminder sent"),
        (STATE_QUALIFIED, "qualified"),
        (STATE_NOT_LEAD, "not a lead"),
        (STATE_STOPPED, "stopped"),
        (STATE_NO_ANSWER, "no answer"),
        (STATE_FAILED, "failed"),
    ]
    # The dialogue is over: no reply of the customer is handled any more.
    # `failed` = Meta rejected the template with a 4xx, nothing was delivered.
    REPLY_CLOSED_STATES = (
        STATE_QUALIFIED,
        STATE_NOT_LEAD,
        STATE_STOPPED,
        STATE_NO_ANSWER,
        STATE_BLOCKED,
        STATE_FAILED,
    )

    SOURCE_BUTTON = "button"
    SOURCE_LLM = "llm"
    SOURCE_MEDIA = "media"
    SOURCE_CHOICES = [
        (SOURCE_BUTTON, "button"),
        (SOURCE_LLM, "llm"),
        (SOURCE_MEDIA, "media"),
    ]

    deal_id = models.PositiveBigIntegerField(unique=True)
    phone = models.CharField(max_length=32, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    project = models.CharField(max_length=255, blank=True, default="")
    page_url = models.TextField(blank=True, default="")
    site = models.CharField(max_length=64, blank=True, default="")
    lang = models.CharField(max_length=16, blank=True, default="")
    form_type = models.CharField(max_length=64, blank=True, default="")
    preferred_channel = models.CharField(max_length=32, null=True, blank=True)
    wa_allowed = models.BooleanField()
    wa_reason = models.CharField(max_length=255, blank=True, default="")
    consent = models.JSONField(null=True, blank=True)
    lead_created_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField(default=dict, blank=True)

    state = models.CharField(max_length=24, choices=STATE_CHOICES, default=STATE_RECEIVED, db_index=True)
    block_reason = models.CharField(max_length=255, blank=True, default="")
    # Set under the gate's locks BEFORE the Graph request: a template whose
    # attempt is recorded is never sent again, even after an error or timeout.
    confirmation_attempted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reminder_attempted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmation_wamid = models.CharField(max_length=255, blank=True, default="")
    confirmation_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    reminder_wamid = models.CharField(max_length=255, blank=True, default="")
    reminder_sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    outcome_source = models.CharField(max_length=16, choices=SOURCE_CHOICES, blank=True, default="")
    last_error = models.TextField(blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["phone", "state"], name="waq_lead_phone_state")]

    def __str__(self):
        return f"deal {self.deal_id} ({self.state})"

    # A lead awaits the customer's reply as soon as a template was ATTEMPTED and
    # until the dialogue is closed - whatever the state says about the POST: the
    # request may still be in flight (state still `received`) or the worker may
    # have died after Meta delivered the message.
    @classmethod
    def awaiting_reply_q(cls):
        attempted = (
            models.Q(confirmation_attempted_at__isnull=False)
            | models.Q(reminder_attempted_at__isnull=False)
            | models.Q(confirmation_sent_at__isnull=False)
        )
        return attempted & ~models.Q(state__in=cls.REPLY_CLOSED_STATES)

    def awaits_reply(self):
        attempted = self.confirmation_attempted_at or self.reminder_attempted_at or self.confirmation_sent_at
        return attempted is not None and self.state not in self.REPLY_CLOSED_STATES


class WaqPhone(models.Model):
    """One row per customer number: atomic dedup (30 days) and the stop list."""

    phone = models.CharField(max_length=32, unique=True)
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    opted_out = models.BooleanField(default=False)
    opted_out_at = models.DateTimeField(null=True, blank=True)
    opt_out_reason = models.CharField(max_length=64, blank=True, default="")

    def __str__(self):
        return self.phone


class WaqEvent(models.Model):
    KIND_NEGATIVE_REPLY = "negative_reply"
    KIND_STOP = "stop"
    KIND_AUTOSTOP = "autostop"
    KIND_CAP = "cap"
    KIND_QUALITY = "quality"
    KIND_TEMPLATE_STATUS = "template_status"
    KIND_SEND_ERROR = "send_error"
    KIND_REPLY = "reply"  # one per processed inbound message id (dedup of Meta re-deliveries)
    KIND_LLM_ERROR = "llm_error"
    KIND_BITRIX_ERROR = "bitrix_error"
    KIND_REPLY_ERROR = "reply_error"
    KIND_MISCONFIGURED = "misconfigured"
    KIND_QUEUE_ERROR = "queue_error"

    lead = models.ForeignKey(
        WaqLead, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    kind = models.CharField(max_length=32, db_index=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["kind", "created_at"], name="waq_event_kind_created")]

    def __str__(self):
        return f"{self.kind} @ {self.created_at:%Y-%m-%d %H:%M}"
