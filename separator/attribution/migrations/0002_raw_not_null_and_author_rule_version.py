from django.db import migrations, models


def backfill_raw(apps, schema_editor):
    """Rows created before `raw` became NOT NULL (shell/admin paths) get `{}`."""
    ClickToken = apps.get_model("attribution", "ClickToken")
    ClickToken.objects.filter(raw__isnull=True).update(raw={})


class Migration(migrations.Migration):

    dependencies = [
        ("attribution", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="messageevent",
            name="author_rule_version",
            field=models.CharField(default="usertype-v1", max_length=32),
        ),
        # Backfill first: AlterField below adds NOT NULL and would fail on NULLs.
        migrations.RunPython(backfill_raw, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="clicktoken",
            name="raw",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
