from decimal import Decimal

from django.db import migrations, models
from django.db.models import Count, Q
from django.utils import timezone


def level_for_joinings(count):
    level = 0
    targets = {1: 3, 2: 12, 3: 27, 4: 150, 5: 220, 6: 370, 7: 500, 8: 1000}
    for step in range(1, 9):
        if count >= targets[step]:
            level = step
    return level


def apply_rules(apps, schema_editor):
    Plan = apps.get_model("flower", "Plan")
    User = apps.get_model("flower", "User")
    Transaction = apps.get_model("flower", "Transaction")
    tasks = {"basic": 1, "gold": 2, "standard": 4, "premium": 6, "super premium": 10}
    for plan in Plan.objects.all():
        daily = tasks.get((plan.name or "").strip().lower())
        if daily and plan.daily_tasks != daily:
            plan.daily_tasks = daily
            plan.save(update_fields=["daily_tasks"])

    User.objects.filter(account_status="approved", plan__isnull=False, activated_at__isnull=True).update(activated_at=models.F("date_joined"))

    pending_ids = list(
        User.objects.filter(role="user", account_status="approved", plan__isnull=False, flower_credited=False).values_list("id", flat=True)
    )
    now = timezone.now()
    for start in range(0, len(pending_ids), 500):
        chunk = pending_ids[start:start + 500]
        User.objects.filter(id__in=chunk).update(flower_balance=Decimal("200.00"), flower_credited=True)
        Transaction.objects.bulk_create([
            Transaction(
                user_id=user_id, amount=Decimal("200.00"), trx_type="+",
                title="Flower Earning", remark="flower", level=0, created_at=now,
            )
            for user_id in chunk
        ])

    counted = User.objects.filter(role="user").annotate(
        joins=Count("team", filter=Q(team__plan__isnull=False, team__account_status="approved"))
    )
    buckets = {}
    for user in counted.iterator():
        level = level_for_joinings(user.joins)
        if user.level != level:
            buckets.setdefault(level, []).append(user.id)
    for level, ids in buckets.items():
        for start in range(0, len(ids), 500):
            User.objects.filter(id__in=ids[start:start + 500]).update(level=level)


class Migration(migrations.Migration):

    dependencies = [
        ("flower", "0005_withdrawal_account_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="flower_balance",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="user",
            name="flower_credited",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="user",
            name="activated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="withdrawal",
            name="flower_part",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AlterUniqueTogether(
            name="taskdone",
            unique_together=set(),
        ),
        migrations.AddIndex(
            model_name="taskdone",
            index=models.Index(fields=["user", "created_at"], name="flower_taskdone_user_created"),
        ),
        migrations.RunPython(apply_rules, migrations.RunPython.noop),
    ]
