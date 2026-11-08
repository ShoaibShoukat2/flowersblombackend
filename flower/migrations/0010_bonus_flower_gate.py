from decimal import Decimal

from django.db import migrations, models


def recover_task_credits(apps, schema_editor):
    User = apps.get_model("flower", "User")
    TaskDone = apps.get_model("flower", "TaskDone")
    Transaction = apps.get_model("flower", "Transaction")
    flower_amount = Decimal("200.00")
    bonus = Decimal("50.00")
    for user in User.objects.filter(role="user").iterator():
        dones = TaskDone.objects.filter(user_id=user.id).count()
        flower_rows = Transaction.objects.filter(user_id=user.id, remark="flower", trx_type="+").count()
        missing = dones - flower_rows
        if missing > 0:
            user.flower_balance = (user.flower_balance or Decimal("0")) + (flower_amount * missing)
            user.save(update_fields=["flower_balance"])
            Transaction.objects.bulk_create([
                Transaction(
                    user_id=user.id, amount=flower_amount, trx_type="+", title="Flower Earning",
                    remark="flower", plan_name="Recovered task flower", level=0, source_email="",
                )
                for _ in range(missing)
            ])
        already = Transaction.objects.filter(user_id=user.id, remark="signup_bonus").exists()
        if user.account_status == "approved" and user.plan_id and dones > 0 and not already:
            user.balance = (user.balance or Decimal("0")) + bonus
            user.save(update_fields=["balance"])
            Transaction.objects.create(
                user_id=user.id, amount=bonus, trx_type="+", title="Bonus",
                remark="signup_bonus", plan_name="Account creation bonus", level=0, source_email="",
            )


class Migration(migrations.Migration):
    dependencies = [
        ("flower", "0009_search_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="channels_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="task",
            name="display_only",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="DisplayBloom",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("task", models.ForeignKey(on_delete=models.deletion.CASCADE, to="flower.task")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, to="flower.user")),
            ],
        ),
        migrations.AddConstraint(
            model_name="displaybloom",
            constraint=models.UniqueConstraint(fields=("user", "task"), name="flower_display_once"),
        ),
        migrations.RunPython(recover_task_credits, migrations.RunPython.noop),
    ]
