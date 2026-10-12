from django.contrib import admin

from .models import WaqEvent, WaqLead, WaqPhone, WaqSettings


@admin.register(WaqSettings)
class WaqSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "enabled", "autostop_active", "hourly_cap", "daily_cap", "phone")
    raw_id_fields = ("phone",)
    fieldsets = (
        ("Stop switches", {"fields": ("enabled", "autostop_active", "autostop_reason", "autostop_at")}),
        ("Caps", {"fields": ("hourly_cap", "daily_cap")}),
        (
            "Templates",
            {
                "fields": (
                    "phone",
                    "template_confirmation",
                    "template_reminder",
                    "template_language",
                    "project_fallback",
                )
            },
        ),
        (
            "Timing",
            {
                "fields": (
                    "reminder_enabled",
                    "reminder_after_min",
                    "reminder_before_min",
                    "reminder_window_start",
                    "reminder_window_end",
                    "no_answer_after_hours",
                )
            },
        ),
        ("Bitrix", {"fields": ("responsible_user_id", "qualified_stage_id", "deal_category_id")}),
        (
            "Bot replies (empty = do not reply)",
            {
                "fields": (
                    "reply_qual",
                    "reply_qual_question",
                    "reply_bot",
                    "reply_not_me",
                    "reply_stop",
                    "reply_who",
                    "reply_company",
                )
            },
        ),
        ("Alerts", {"fields": ("alert_chat_id",)}),
    )

    def has_add_permission(self, request):
        return not WaqSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class WaqEventInline(admin.TabularInline):
    model = WaqEvent
    extra = 0
    fields = ("created_at", "kind", "data")
    readonly_fields = ("created_at", "kind", "data")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WaqLead)
class WaqLeadAdmin(admin.ModelAdmin):
    list_display = (
        "deal_id",
        "state",
        "phone",
        "project",
        "form_type",
        "received_at",
        "confirmation_sent_at",
        "reminder_sent_at",
        "replied_at",
        "outcome_source",
    )
    list_filter = ("state", "outcome_source", "form_type", "site", "wa_allowed")
    search_fields = ("deal_id", "phone", "project", "confirmation_wamid", "reminder_wamid")
    readonly_fields = ("received_at",)
    inlines = [WaqEventInline]


@admin.register(WaqPhone)
class WaqPhoneAdmin(admin.ModelAdmin):
    list_display = ("phone", "last_contacted_at", "opted_out", "opted_out_at", "opt_out_reason")
    list_filter = ("opted_out", "opt_out_reason")
    search_fields = ("phone",)


@admin.register(WaqEvent)
class WaqEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "lead")
    list_filter = ("kind",)
    search_fields = ("lead__deal_id",)
    raw_id_fields = ("lead",)
    readonly_fields = ("created_at",)
