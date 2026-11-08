from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("flower", "0011_recover_task_credits"),
    ]

    operations = [
        migrations.AddField(
            model_name="displaybloom",
            name="day",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RemoveConstraint(
            model_name="displaybloom",
            name="flower_display_once",
        ),
        migrations.AddConstraint(
            model_name="displaybloom",
            constraint=models.UniqueConstraint(fields=("user", "task", "day"), name="flower_display_daily"),
        ),
        migrations.AddField(
            model_name="gateway",
            name="instructions",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="deposit",
            name="sender_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="deposit",
            name="trx_no",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AddField(
            model_name="upgraderequest",
            name="sender_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="upgraderequest",
            name="trx_no",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
    ]
