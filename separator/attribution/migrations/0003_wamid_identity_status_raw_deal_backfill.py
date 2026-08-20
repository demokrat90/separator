from datetime import timedelta

from django.db import migrations, models


def split_synthetic_ids(apps, schema_editor):
    """Rows written before wamid became the identity.

    Their message_id was `b24:<app instance>:<bitrix id>`, which no delivery
    status could ever join to. Move the Bitrix id into its own column and clear
    message_id: those rows never got a wamid and must not pretend otherwise.
    """
    MessageEvent = apps.get_model("attribution", "MessageEvent")
    for event in MessageEvent.objects.filter(message_id__startswith="b24:"):
        event.bitrix_message_id = event.message_id.rsplit(":", 1)[-1]
        event.message_id = None
        event.save(update_fields=["bitrix_message_id", "message_id"])


def backfill_deal_id(apps, schema_editor):
    """Stamp already recorded messages with the deal their number belongs to."""
    MessageEvent = apps.get_model("attribution", "MessageEvent")
    DealAttribution = apps.get_model("attribution", "DealAttribution")

    for event in MessageEvent.objects.filter(deal_id__isnull=True):
        query = DealAttribution.objects.filter(
            phone=event.phone, push_state="done", deal_id__isnull=False
        )
        if event.app_instance_id:
            query = query.filter(app_instance_id=event.app_instance_id)
        if event.created_at:
            query = query.filter(created_at__gte=event.created_at - timedelta(days=30))
        attribution = query.order_by("-created_at").first()
        if attribution:
            event.deal_id = attribution.deal_id
            event.save(update_fields=["deal_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("attribution", "0002_raw_not_null_and_author_rule_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="messageevent",
            name="bitrix_message_id",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="statusevent",
            name="raw",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name="messageevent",
            name="message_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        # Data moves while the old unique index is gone and before the new
        # partial ones are added.
        migrations.RunPython(split_synthetic_ids, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="messageevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(("message_id__isnull", False)),
                fields=("message_id",),
                name="attribution_message_wamid_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="messageevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(("bitrix_message_id__isnull", False)),
                fields=("app_instance_id", "bitrix_message_id"),
                name="attribution_message_b24_unique",
            ),
        ),
        migrations.RunPython(backfill_deal_id, migrations.RunPython.noop),
    ]
