from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("flower", "0004_referral_code_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="withdrawal",
            name="account_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="withdrawal",
            name="updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
