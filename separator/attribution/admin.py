from django.contrib import admin

from .models import ClickToken, DealAttribution, MessageEvent, StatusEvent


@admin.register(ClickToken)
class ClickTokenAdmin(admin.ModelAdmin):
    list_display = ("token", "site", "listing_id", "created_at", "claimed_at", "phone", "is_test")
    list_filter = ("site", "is_test")
    search_fields = ("token", "phone", "listing_id", "gclid", "utm_campaign")
    readonly_fields = ("created_at",)


@admin.register(MessageEvent)
class MessageEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "direction", "author_type", "phone", "deal_id", "message_id")
    list_filter = ("direction", "author_type")
    search_fields = ("phone", "message_id", "deal_id", "chat_ref")
    readonly_fields = ("created_at",)


@admin.register(StatusEvent)
class StatusEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "status", "recipient_id", "message_id", "conversation_origin_type")
    list_filter = ("status", "conversation_origin_type")
    search_fields = ("message_id", "recipient_id", "conversation_id")
    readonly_fields = ("created_at",)


@admin.register(DealAttribution)
class DealAttributionAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "phone",
        "deal_id",
        "attribution_source",
        "attribution_status",
        "pushed_to_bitrix_at",
    )
    list_filter = ("attribution_source", "attribution_status")
    search_fields = ("phone", "deal_id", "token__token")
    readonly_fields = ("created_at", "updated_at")
