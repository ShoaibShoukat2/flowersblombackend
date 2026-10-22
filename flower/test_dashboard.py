from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from django.test import TestCase

from flower.models import Deposit, Plan, Transaction, User, Withdrawal


def stamp(model, pk, **fields):
    model.objects.filter(pk=pk).update(**fields)


class DashboardMathTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="deskadmin", email="deskadmin@flower.test", password="secret",
            referral_code="deskadmin", role="admin",
        )
        self.basic = Plan.objects.create(name="Basic", price=Decimal("600.00"), daily_tasks=1)
        self.gold = Plan.objects.create(name="Gold", price=Decimal("5000.00"), daily_tasks=2)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.now = timezone.now()

    def user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@flower.test", password="secret",
            referral_code=name, role="user", full_name=name,
        )

    def test_today_counts_follow_approval_not_request_date(self):
        first = self.user("amina")
        second = self.user("bilal")
        old = Deposit.objects.create(
            user=first, plan=self.basic, amount=Decimal("600.00"), method="JazzCash",
            sender="03001111111", status=Deposit.REJECTED, reject_reason="Old slip",
        )
        fresh = Deposit.objects.create(
            user=second, plan=self.gold, amount=Decimal("5000.00"), method="Easypaisa",
            sender="03002222222", status=Deposit.PENDING,
        )
        stamp(Deposit, old.pk, created_at=self.now - timedelta(days=20))
        stamp(Deposit, fresh.pk, created_at=self.now - timedelta(days=3))
        yesterday = Deposit.objects.create(
            user=self.user("cara"), plan=self.basic, amount=Decimal("600.00"), method="JazzCash",
            sender="03003333333", status=Deposit.APPROVED, approved_at=self.now - timedelta(days=1),
        )
        stamp(Deposit, yesterday.pk, created_at=self.now - timedelta(days=1), updated_at=self.now - timedelta(days=1))

        self.assertEqual(self.client.post(f"/api/admin/deposits/{old.id}", {"action": "approve"}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/admin/deposits/{fresh.id}", {"action": "approve"}, format="json").status_code, 200)

        early = timezone.localtime().replace(hour=0, minute=30, second=0, microsecond=0)
        night = Deposit.objects.create(
            user=self.user("danish"), plan=self.basic, amount=Decimal("600.00"), method="JazzCash",
            sender="03004444444", status=Deposit.APPROVED, approved_at=early,
        )
        stamp(Deposit, night.pk, created_at=early - timedelta(days=4), updated_at=early)

        summary = self.client.get("/api/admin/summary").json()
        money = summary["money"]
        self.assertEqual(money["approved_users_today"], 3)
        self.assertEqual(money["received_today"], "6200.00")
        self.assertEqual(summary["approved_today"], 3)

        listed = self.client.get("/api/admin/today-approved").json()
        self.assertEqual(listed["users"], 3)
        self.assertEqual(listed["receive"], "6200.00")
        self.assertEqual({row["email"] for row in listed["rows"]}, {"amina@flower.test", "bilal@flower.test", "danish@flower.test"})

    def test_task_manual_and_referral_dates_match_their_lists(self):
        person = self.user("esha")
        today_task = Transaction.objects.create(user=person, amount=Decimal("15.00"), trx_type="+", title="Task Earning", remark="ads")
        old_task = Transaction.objects.create(user=person, amount=Decimal("40.00"), trx_type="+", title="Task Earning", remark="ads")
        restore = Transaction.objects.create(user=person, amount=Decimal("999.00"), trx_type="+", title="Restore", remark="restore20")
        flower = Transaction.objects.create(user=person, amount=Decimal("200.00"), trx_type="+", title="Flower", remark="flower")
        add_today = Transaction.objects.create(user=person, amount=Decimal("50.00"), trx_type="+", title="Balance added", remark="balance_add")
        add_old = Transaction.objects.create(user=person, amount=Decimal("25.00"), trx_type="+", title="Balance added", remark="balance_add")
        sub_today = Transaction.objects.create(user=person, amount=Decimal("10.00"), trx_type="-", title="Balance subtracted", remark="balance_subtract")
        referral_today = Transaction.objects.create(user=person, amount=Decimal("7.00"), trx_type="+", title="Direct Referral Commission", remark="referral", level=1)
        stamp(Transaction, old_task.pk, created_at=self.now - timedelta(days=2))
        stamp(Transaction, add_old.pk, created_at=self.now - timedelta(days=2))
        stamp(Transaction, restore.pk, created_at=self.now)
        stamp(Transaction, flower.pk, created_at=self.now)

        money = self.client.get("/api/admin/summary").json()["money"]
        self.assertEqual(money["task"], "55.00")
        self.assertEqual(money["task_today"], "15.00")
        self.assertEqual(money["manual_add"], "75.00")
        self.assertEqual(money["manual_add_today"], "50.00")
        self.assertEqual(money["manual_sub"], "10.00")
        self.assertEqual(money["manual_sub_today"], "10.00")
        self.assertEqual(money["referral"], "7.00")
        self.assertEqual(money["referral_today"], "7.00")

        for category, today_total, overall in (
            ("ads", "15.00", "55.00"),
            ("manual_add", "50.00", "75.00"),
            ("manual_sub", "10.00", "10.00"),
            ("referral", "7.00", "7.00"),
        ):
            today = self.client.get(f"/api/admin/transactions?category={category}&date=today").json()
            everything = self.client.get(f"/api/admin/transactions?category={category}").json()
            self.assertEqual(today["total"], today_total, category)
            self.assertEqual(everything["total"], overall, category)
            self.assertTrue(all(row["remark"] != "flower" for row in today["rows"]))
            self.assertTrue(all(row["remark"] != "restore20" for row in everything["rows"]))

        paid = Withdrawal.objects.create(
            user=person, amount=Decimal("80.00"), method="JazzCash", account="03005555555", status=Withdrawal.PENDING,
        )
        stamp(Withdrawal, paid.pk, created_at=self.now - timedelta(days=6))
        self.assertEqual(self.client.post(f"/api/admin/withdrawals/{paid.id}", {"action": "approve"}, format="json").status_code, 200)
        older = Withdrawal.objects.create(
            user=person, amount=Decimal("30.00"), method="JazzCash", account="03005555555",
            status=Withdrawal.APPROVED, updated_at=self.now - timedelta(days=2),
        )
        money = self.client.get("/api/admin/summary").json()["money"]
        self.assertEqual(money["withdrawals_today"], "80.00")
        self.assertEqual(money["withdrawn"], "110.00")
        listed = self.client.get("/api/admin/withdrawals?status=approved&today=1").json()
        self.assertEqual([row["id"] for row in listed], [paid.id])
        self.assertNotEqual(older.amount, paid.amount)
