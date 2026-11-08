from decimal import Decimal

from django.db import migrations, transaction


def recover_task_credits(apps, schema_editor):
    Transaction = apps.get_model("flower", "Transaction")
    User = apps.get_model("flower", "User")
    from django.utils import timezone
    now = timezone.now()
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT u.id,
              (SELECT COUNT(*) FROM flower_taskdone d WHERE d.user_id = u.id),
              (SELECT COUNT(*) FROM flower_transaction t WHERE t.user_id = u.id AND t.remark = 'flower' AND t.trx_type = '+')
            FROM flower_user u
            WHERE u.role = 'user'
            """
        )
        flower_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT u.id
            FROM flower_user u
            WHERE u.role = 'user' AND u.account_status = 'approved' AND u.plan_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM flower_taskdone d WHERE d.user_id = u.id)
              AND NOT EXISTS (SELECT 1 FROM flower_transaction t WHERE t.user_id = u.id AND t.remark = 'signup_bonus')
            """
        )
        bonus_ids = [row[0] for row in cursor.fetchall()]
    flower_added = 0
    for user_id, dones, flowers in flower_rows:
        missing = int(dones or 0) - int(flowers or 0)
        if missing <= 0:
            continue
        with transaction.atomic():
            User.objects.filter(pk=user_id).update(flower_balance=models_f(User, missing))
            Transaction.objects.bulk_create([
                Transaction(
                    user_id=user_id, amount=Decimal("200.00"), trx_type="+", title="Flower Earning",
                    remark="flower", plan_name="Recovered task flower", level=0, source_email="", created_at=now,
                )
                for _ in range(missing)
            ], batch_size=500)
        flower_added += missing
    for start in range(0, len(bonus_ids), 500):
        chunk = bonus_ids[start:start + 500]
        with transaction.atomic():
            User.objects.filter(pk__in=chunk).update(balance=models_bonus(User))
            Transaction.objects.bulk_create([
                Transaction(
                    user_id=user_id, amount=Decimal("50.00"), trx_type="+", title="Bonus",
                    remark="signup_bonus", plan_name="Account creation bonus", level=0, source_email="", created_at=now,
                )
                for user_id in chunk
            ], batch_size=500)
    print(f"Recovered flower credits {flower_added}; signup bonuses {len(bonus_ids)}")


def models_f(User, missing):
    from django.db.models import F
    return F("flower_balance") + (Decimal("200.00") * missing)


def models_bonus(User):
    from django.db.models import F
    return F("balance") + Decimal("50.00")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("flower", "0010_bonus_flower_gate"),
    ]

    operations = [
        migrations.RunPython(recover_task_credits, migrations.RunPython.noop),
    ]
