import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("flower", "0013_channels_day"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactadmin",
            name="botim",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="contactadmin",
            name="whatsapp_icon",
            field=models.FileField(blank=True, null=True, upload_to="contacts/"),
        ),
        migrations.AddField(
            model_name="contactadmin",
            name="botim_icon",
            field=models.FileField(blank=True, null=True, upload_to="contacts/"),
        ),
        migrations.AddField(
            model_name="upgraderequest",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, null=True),
        ),
        migrations.CreateModel(
            name="DepositEditHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("plan_name", models.CharField(blank=True, max_length=80)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("method", models.CharField(blank=True, max_length=80)),
                ("sender", models.CharField(blank=True, max_length=80)),
                ("sender_name", models.CharField(blank=True, max_length=120)),
                ("trx_no", models.CharField(blank=True, max_length=80)),
                ("screenshot", models.FileField(blank=True, null=True, upload_to="payments/history/")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("deposit", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="edit_history", to="flower.deposit")),
                ("editor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
