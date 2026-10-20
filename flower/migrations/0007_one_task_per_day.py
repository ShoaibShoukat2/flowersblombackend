from datetime import timedelta

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def claim_today(apps, schema_editor):
    TaskDone = apps.get_model("flower", "TaskDone")
    TaskClaim = apps.get_model("flower", "TaskClaim")
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    seen = set()
    claims = []
    pairs = TaskDone.objects.filter(created_at__gte=start, created_at__lt=end).values_list("user_id", "task_id")
    for user_id, task_id in pairs.iterator():
        key = (user_id, task_id)
        if key in seen:
            continue
        seen.add(key)
        claims.append(TaskClaim(user_id=user_id, task_id=task_id, day=start.date()))
    TaskClaim.objects.bulk_create(claims, batch_size=1000)


class Migration(migrations.Migration):

    dependencies = [
        ("flower", "0006_level_task_flower"),
    ]

    operations = [
        migrations.CreateModel(
            name="TaskClaim",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day", models.DateField()),
                ("task", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="flower.task")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="taskclaim",
            constraint=models.UniqueConstraint(fields=("user", "task", "day"), name="flower_one_task_per_day"),
        ),
        migrations.RunPython(claim_today, migrations.RunPython.noop),
    ]
