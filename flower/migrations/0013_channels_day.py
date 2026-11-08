from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("flower", "0012_dashboard_updates"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="channels_day",
            field=models.DateField(blank=True, null=True),
        ),
    ]
