from django.db import migrations, models


class Migration(migrations.Migration):
    """Photon fork: default Graph API version 20 -> 21.

    Only changes the default for newly created App rows; existing rows keep
    whatever version they were configured with (change those in the admin).
    """

    dependencies = [
        ('waba', '0037_app_fallback_app'),
    ]

    operations = [
        migrations.AlterField(
            model_name='app',
            name='api_version',
            field=models.IntegerField(default=21),
        ),
    ]
