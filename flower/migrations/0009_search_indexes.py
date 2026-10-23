from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("flower", "0008_admin_desk"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="user",
            index=models.Index(fields=["email"], name="flower_user_email_idx"),
        ),
        migrations.AddIndex(
            model_name="user",
            index=models.Index(fields=["account_status"], name="flower_user_acct_status"),
        ),
        migrations.AddIndex(
            model_name="deposit",
            index=models.Index(fields=["status", "id"], name="flower_deposit_status_id"),
        ),
        migrations.AddIndex(
            model_name="withdrawal",
            index=models.Index(fields=["status", "id"], name="flower_withdraw_status_id"),
        ),
        migrations.AddIndex(
            model_name="transaction",
            index=models.Index(fields=["user", "remark", "id"], name="flower_tx_user_remark"),
        ),
    ]
