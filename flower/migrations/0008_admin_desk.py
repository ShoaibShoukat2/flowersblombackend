from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def stamp_approved_deposits(apps, schema_editor):
    Deposit = apps.get_model("flower", "Deposit")
    Deposit.objects.filter(status="approved", approved_at__isnull=True).update(approved_at=models.F("updated_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("flower", "0007_one_task_per_day"),
    ]

    operations = [
        migrations.AddField(model_name="user", name="ban_reason", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="user", name="banned_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="sitesetting", name="direct_label", field=models.CharField(default="Direct Referral Commission", max_length=80)),
        migrations.AddField(model_name="sitesetting", name="level2_label", field=models.CharField(default="Level 2 – Indirect Referral Commission", max_length=80)),
        migrations.AddField(model_name="sitesetting", name="level3_label", field=models.CharField(default="Level 3 – Indirect Referral Commission", max_length=80)),
        migrations.AddField(model_name="sitesetting", name="upgrade_commission_percent", field=models.DecimalField(decimal_places=2, default=20, max_digits=5)),
        migrations.AddField(model_name="contactadmin", name="purpose", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="contactadmin", name="number_type", field=models.CharField(default="whatsapp", max_length=20)),
        migrations.AddField(model_name="gateway", name="image", field=models.FileField(blank=True, null=True, upload_to="gateways/")),
        migrations.AddField(model_name="deposit", name="reject_reason", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="deposit", name="approved_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="withdrawal", name="admin_remark", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="upgraderequest", name="reject_reason", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="upgraderequest", name="pay_commission", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="upgraderequest", name="approved_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.CreateModel(
            name="BalanceAdjustment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=20)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("reason", models.TextField()),
                ("before_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("after_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("request_key", models.CharField(max_length=80, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("admin", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="adjustments_made", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="adjustments", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="ImpersonationGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField()),
                ("admin", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="impersonations_started", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="impersonations_received", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="AdminSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=64, unique=True)),
                ("user_agent", models.CharField(blank=True, max_length=255)),
                ("ip", models.CharField(blank=True, max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(auto_now=True)),
                ("revoked", models.BooleanField(default=False)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="admin_sessions", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.RunPython(stamp_approved_deposits, migrations.RunPython.noop),
    ]
