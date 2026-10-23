from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from django.core.files.uploadedfile import SimpleUploadedFile
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
        rows = listed["rows"] if isinstance(listed, dict) else listed
        self.assertEqual([row["id"] for row in rows], [paid.id])
        self.assertNotEqual(older.amount, paid.amount)

    def test_exact_email_search_does_not_mix_accounts(self):
        keep = self.user("rejectedone")
        other = self.user("rejectedtwo")
        Deposit.objects.create(user=keep, plan=self.basic, amount=Decimal("600.00"), method="JazzCash", sender="03001", status=Deposit.REJECTED, reject_reason="slip")
        Deposit.objects.create(user=other, plan=self.gold, amount=Decimal("5000.00"), method="JazzCash", sender="03002", status=Deposit.REJECTED, reject_reason="slip")
        found = self.client.get("/api/admin/deposits", {"status": "rejected", "q": "rejectedone@flower.test"}).json()
        self.assertEqual([row["email"] for row in found["rows"]], ["rejectedone@flower.test"])
        missing = self.client.get("/api/admin/deposits", {"status": "rejected", "q": "nobody@flower.test"}).json()
        self.assertEqual(missing["rows"], [])
        users = self.client.get("/api/admin/users", {"status": "all", "q": "rejectedone@flower.test"}).json()
        self.assertEqual([row["email"] for row in users["rows"]], ["rejectedone@flower.test"])

    def test_ban_and_unban_round_trip(self):
        person = self.user("banned")
        person.plan = self.basic
        person.account_status = "approved"
        person.save(update_fields=["plan", "account_status"])
        banned = self.client.post(f"/api/admin/users/{person.id}/ban", {"action": "ban", "reason": "Duplicate payment"}, format="json")
        self.assertEqual(banned.status_code, 200)
        person.refresh_from_db()
        self.assertEqual(person.account_status, "banned")
        detail = self.client.get(f"/api/admin/users/{person.id}").json()
        self.assertTrue(detail["profile"]["banned"])
        self.assertEqual(detail["profile"]["ban_reason"], "Duplicate payment")
        opened = self.client.post(f"/api/admin/users/{person.id}/ban", {"action": "unban"}, format="json")
        self.assertEqual(opened.status_code, 200)
        person.refresh_from_db()
        self.assertEqual(person.account_status, "approved")
        self.assertIsNone(person.banned_at)

    def test_withdraw_summary_splits_pending_and_rejected(self):
        person = self.user("teller")
        Withdrawal.objects.create(user=person, amount=Decimal("40.00"), method="JazzCash", account="03001", status=Withdrawal.PENDING)
        Withdrawal.objects.create(user=person, amount=Decimal("15.00"), method="JazzCash", account="03001", status=Withdrawal.PENDING)
        Withdrawal.objects.create(user=person, amount=Decimal("9.00"), method="JazzCash", account="03002", status=Withdrawal.REJECTED)
        Withdrawal.objects.create(user=person, amount=Decimal("100.00"), method="JazzCash", account="03003", status=Withdrawal.APPROVED)
        totals = self.client.get("/api/admin/withdrawals/summary").json()
        self.assertEqual(totals["pending"], "55.00")
        self.assertEqual(totals["rejected"], "9.00")

    def test_user_transaction_filters_and_source_profile(self):
        owner = self.user("owner")
        source = self.user("source")
        Transaction.objects.create(user=owner, amount=Decimal("12.00"), trx_type="+", title="Task Earning", remark="ads")
        Transaction.objects.create(user=owner, amount=Decimal("8.00"), trx_type="+", title="Ads Earning", remark="ads")
        Transaction.objects.create(user=owner, amount=Decimal("20.00"), trx_type="+", title="Balance added", remark="balance_add")
        Transaction.objects.create(user=owner, amount=Decimal("5.00"), trx_type="+", title="Direct Referral Commission", remark="referral", level=1, source_email=source.email)
        Transaction.objects.create(user=owner, amount=Decimal("3.00"), trx_type="+", title="Indirect", remark="referral", level=2, source_email=source.email)
        history = self.client.get(f"/api/admin/users/{owner.id}/transactions", {"category": "balance_add"}).json()
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["rows"][0]["amount"], "20.00")
        direct = self.client.get(f"/api/admin/users/{owner.id}/transactions", {"category": "direct"}).json()
        self.assertEqual(direct["rows"][0]["source_user_id"], source.id)
        task = self.client.get(f"/api/admin/users/{owner.id}/transactions", {"category": "task"}).json()
        self.assertEqual([row["title"] for row in task["rows"]], ["Task Earning"])
        ads = self.client.get(f"/api/admin/users/{owner.id}/transactions", {"category": "ads_earn"}).json()
        self.assertEqual([row["title"] for row in ads["rows"]], ["Ads Earning"])

    def test_every_admin_account_shows_on_the_user_payment_page(self):
        first = self.client.post("/api/admin/gateways", {"name": "JazzCash", "account_name": "Flower One", "account_number": "03001111111", "active": "1"}, format="multipart").json()
        second = self.client.post("/api/admin/gateways", {"name": "JazzCash", "account_name": "Flower Two", "account_number": "03002222222", "active": "1"}, format="multipart").json()
        third = self.client.post("/api/admin/gateways", {"name": "Easypaisa", "account_name": "Flower Easy", "account_number": "03003333333", "active": "1"}, format="multipart").json()
        numbers = [row["account_number"] for row in third]
        self.assertEqual(numbers, ["03001111111", "03002222222", "03003333333"])
        self.assertEqual(len({row["id"] for row in second}), 2)
        hidden = self.client.post("/api/admin/gateways", {"name": "JazzCash", "account_name": "Hidden", "account_number": "03004444444", "active": "0"}, format="multipart").json()
        self.assertEqual(len(hidden), 4)

        payer = self.user("payer")
        user_client = APIClient()
        user_client.force_authenticate(user=payer)
        shown = user_client.get("/api/user/plans").json()["gateways"]
        self.assertEqual([row["account_number"] for row in shown], ["03001111111", "03002222222", "03003333333"])
        self.assertEqual([row["name"] for row in shown], ["JazzCash", "JazzCash", "Easypaisa"])

        plan = Plan.objects.create(name="Shown", price=Decimal("700.00"), daily_tasks=1)
        slip = SimpleUploadedFile("slip.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, content_type="image/png")
        created = user_client.post("/api/user/deposits", {"plan_id": plan.id, "sender": "03005555555", "method": "JazzCash", "gateway_id": first[0]["id"], "screenshot": slip}, format="multipart")
        self.assertEqual(created.status_code, 200, created.content)
        saved = Deposit.objects.get(user=payer)
        self.assertEqual(saved.method, "JazzCash · 03001111111")
        self.assertEqual(first[0]["account_name"], "Flower One")
        choices = user_client.get("/api/user/withdrawals").json()["methods"]
        self.assertEqual(choices, ["JazzCash", "Easypaisa"])
        blocked = user_client.post("/api/user/withdrawals", {"amount": "50", "method": "Stripe", "account_name": "Aisha", "account": "0300"}, format="json")
        self.assertEqual(blocked.status_code, 400)

    def test_admin_can_add_any_withdrawal_method(self):
        self.client.post("/api/admin/gateways", {"name": "JazzCash", "account_name": "One", "account_number": "03001111111", "active": "1"}, format="multipart")
        self.client.post("/api/admin/gateways", {"name": "jazz cash", "account_name": "Two", "account_number": "03002222222", "active": "1"}, format="multipart")
        saved = self.client.post("/api/admin/gateways", {"name": "NayaPay", "account_name": "Flower Naya", "account_number": "03008887777", "active": "1"}, format="multipart").json()
        self.assertEqual([row["name"] for row in saved], ["JazzCash", "JazzCash", "NayaPay"])
        payer = self.user("chooser")
        user_client = APIClient()
        user_client.force_authenticate(user=payer)
        self.assertEqual(user_client.get("/api/user/withdrawals").json()["methods"], ["JazzCash", "NayaPay"])
        self.client.post("/api/admin/gateways", {"name": "Upaisa", "account_name": "", "account_number": "", "active": "1"}, format="multipart")
        self.assertEqual(user_client.get("/api/user/withdrawals").json()["methods"], ["JazzCash", "NayaPay", "Upaisa"])
        deposit_numbers = [row["account_number"] for row in user_client.get("/api/user/plans").json()["gateways"]]
        self.assertNotIn("", deposit_numbers)
        shown = user_client.get("/api/user/plans").json()["gateways"]
        self.assertEqual([row["account_number"] for row in shown], ["03001111111", "03002222222", "03008887777"])
