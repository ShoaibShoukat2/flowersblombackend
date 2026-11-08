from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from flower.levels import cycle_status
from flower.models import Deposit, DisplayBloom, Plan, TaskDone, Transaction, User


class RuleTests(TestCase):
    def setUp(self):
        self.sponsor = User.objects.create_user(
            username="sponsor", email="sponsor@flower.test", password="secret",
            referral_code="SPONSOR", role="user", full_name="Sana Sponsor", account_status="approved",
        )
        self.plan = Plan.objects.create(name="Gold", price=Decimal("5000.00"), daily_tasks=2)
        self.client = APIClient()

    def approved(self, name, when=None):
        user = User.objects.create_user(
            username=name, email=f"{name}@flower.test", password="secret",
            referral_code=name.upper(), role="user", full_name=name,
            plan=self.plan, account_status="approved", referred_by=self.sponsor,
            activated_at=when or timezone.now(),
        )
        return user

    def test_login_allows_repeated_success_and_limits_failures(self):
        person = self.approved("loginuser")
        for _ in range(6):
            ok = self.client.post("/api/auth/login", {"identity": person.email, "password": "secret", "role": "user"}, format="json")
            self.assertEqual(ok.status_code, 200)
        for _ in range(12):
            bad = self.client.post("/api/auth/login", {"identity": person.email, "password": "wrong", "role": "user"}, format="json")
            self.assertEqual(bad.status_code, 400)
        blocked = self.client.post("/api/auth/login", {"identity": person.email, "password": "wrong", "role": "user"}, format="json")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("15 minutes", blocked.json()["detail"])
        other = self.client.post("/api/auth/login", {"identity": self.sponsor.email, "password": "secret", "role": "user"}, format="json")
        self.assertEqual(other.status_code, 200)

    def test_referral_is_required_and_names_the_upline(self):
        missing = self.client.post("/api/auth/register", {
            "full_name": "New User", "email": "new@flower.test", "password": "secret12", "phone": "03001111111",
        }, format="json")
        self.assertEqual(missing.status_code, 400)
        named = self.client.get("/api/auth/referrer", {"code": "sponsor"}).json()
        self.assertEqual(named["name"], "Sana Sponsor")
        created = self.client.post("/api/auth/register", {
            "full_name": "New User", "email": "new@flower.test", "password": "secret12",
            "phone": "03001111111", "referral": "sponsor",
        }, format="json")
        self.assertEqual(created.status_code, 200)
        user = User.objects.get(email="new@flower.test")
        self.assertEqual(user.referred_by_id, self.sponsor.id)

    def test_first_task_bonus_and_flower_per_task_stay_separate(self):
        person = self.approved("earner")
        self.client.force_authenticate(user=person)
        rows = self.client.get("/api/user/tasks").json()["tasks"]
        self.assertGreaterEqual(len(rows), 2)
        first = self.client.post(f"/api/user/tasks/{rows[0]['id']}/complete", {}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["reward"], "7.00")
        self.assertEqual(first.json()["bonus"], "50.00")
        self.assertEqual(first.json()["flower"], "200.00")
        person.refresh_from_db()
        self.assertEqual(person.balance, Decimal("57.00"))
        self.assertEqual(person.flower_balance, Decimal("200.00"))
        second = self.client.post(f"/api/user/tasks/{rows[1]['id']}/complete", {}, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["bonus"], "0.00")
        person.refresh_from_db()
        self.assertEqual(person.balance, Decimal("64.00"))
        self.assertEqual(person.flower_balance, Decimal("400.00"))
        self.assertEqual(Transaction.objects.filter(user=person, remark="signup_bonus").count(), 1)
        self.assertEqual(Transaction.objects.filter(user=person, remark="flower").count(), 2)
        again = self.client.post(f"/api/user/tasks/{rows[0]['id']}/complete", {}, format="json")
        self.assertEqual(again.status_code, 400)
        person.refresh_from_db()
        self.assertEqual(person.flower_balance, Decimal("400.00"))
        history = self.client.get("/api/user/transactions").json()
        self.assertTrue(all(row["remark"] != "flower" for row in history))
        bonus = self.client.get("/api/user/transactions?filter=bonus").json()
        self.assertEqual(len(bonus), 1)
        self.assertEqual(bonus[0]["title"], "Bonus")
        self.assertEqual(bonus[0]["plan_name"], "")
        self.assertNotIn("Account creation bonus", str(bonus))
        extra = self.client.get("/api/user/tasks").json()["extras"]
        self.assertEqual(len(extra), 30)
        before = person.balance
        bloom = self.client.post(f"/api/user/tasks/{extra[0]['id']}/complete", {}, format="json")
        self.assertEqual(bloom.status_code, 200)
        self.assertTrue(bloom.json()["display_only"])
        person.refresh_from_db()
        self.assertEqual(person.balance, before)
        self.assertEqual(person.flower_balance, Decimal("400.00"))
        self.assertEqual(TaskDone.objects.filter(user=person).count(), 2)
        flower = extra[0]
        opened = self.client.post(f"/api/user/tasks/{flower['id']}/complete", {}, format="json")
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.json()["reward"], "0.00")
        self.assertEqual(opened.json()["flower"], "0.00")
        person.refresh_from_db()
        self.assertEqual(person.balance, before)
        self.assertEqual(Transaction.objects.filter(user=person, remark__in=["ads", "flower"], plan_name=flower["title"]).count(), 0)
        shown = self.client.get("/api/user/tasks").json()["extras"]
        self.assertTrue(next(row["done"] for row in shown if row["id"] == flower["id"]))
        DisplayBloom.objects.filter(user=person, task_id=flower["id"]).update(day=timezone.localdate() - timedelta(days=1))
        reset = self.client.get("/api/user/tasks").json()["extras"]
        self.assertFalse(next(row["done"] for row in reset if row["id"] == flower["id"]))
        reopened = self.client.post(f"/api/user/tasks/{flower['id']}/complete", {}, format="json")
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json()["reward"], "0.00")
        person.refresh_from_db()
        self.assertEqual(person.balance, before)

    def test_pending_account_stays_closed_until_admin_approval(self):
        person = User.objects.create_user(
            username="waiting", email="waiting@flower.test", password="secret",
            referral_code="WAITING", role="user", full_name="Waiting", account_status="pending",
            referred_by=self.sponsor,
        )
        self.client.force_authenticate(user=person)
        self.assertEqual(self.client.get("/api/user/home").status_code, 403)
        self.assertEqual(self.client.get("/api/user/tasks").status_code, 403)
        shot = SimpleUploadedFile("slip.jpg", b"\xff\xd8\xff\x00", content_type="image/jpeg")
        Deposit.objects.create(user=person, plan=self.plan, amount=self.plan.price, screenshot=shot, status=Deposit.PENDING)
        waiting = self.client.get("/api/user/gate").json()
        self.assertEqual(waiting["phase"], "timer")
        self.assertEqual(waiting["channels"], [])
        blocked = self.client.post("/api/user/channels/confirm", {"joined": True}, format="json")
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(self.client.get("/api/user/home").status_code, 403)
        person.account_status = "approved"
        person.plan = self.plan
        person.activated_at = timezone.now()
        person.save()
        opened = self.client.get("/api/user/gate").json()
        self.assertEqual(opened["phase"], "join_channels")
        self.assertGreaterEqual(len(opened["channels"]), 0)
        confirmed = self.client.post("/api/user/channels/confirm", {"joined": True}, format="json")
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["phase"], "approved")
        self.assertEqual(self.client.get("/api/user/home").status_code, 200)

    def test_fifteen_day_window_does_not_lock_before_level_one(self):
        now = timezone.now()
        fresh = cycle_status(0, 0, now - timedelta(days=3), None, now)
        self.assertFalse(fresh["locked"])
        self.assertEqual(fresh["days"], 15)
        expired = cycle_status(0, 0, now - timedelta(days=16), None, now)
        self.assertTrue(expired["locked"])
        kept = cycle_status(0, 1, now - timedelta(days=20), now - timedelta(days=10), now)
        self.assertFalse(kept["locked"])
        self.assertEqual(kept["days"], 15)
        level_one = cycle_status(1, 3, now - timedelta(days=40), now - timedelta(days=2), now)
        self.assertFalse(level_one["locked"])
        self.assertEqual(level_one["days"], 7)
        late = cycle_status(1, 3, now - timedelta(days=40), now - timedelta(days=8), now)
        self.assertTrue(late["locked"])
        person = self.approved("early", when=now - timedelta(days=20))
        friend = self.approved("friend", when=now - timedelta(days=4))
        friend.referred_by = person
        friend.save(update_fields=["referred_by"])
        self.client.force_authenticate(user=person)
        progress = self.client.get("/api/user/tasks").json()["progress"]
        self.assertFalse(progress["locked"])
        self.assertEqual(progress["cycle_days"], 15)

    def test_admin_bonus_keeps_the_reason_off_the_user_history(self):
        from flower.models import BalanceAdjustment
        person = self.approved("bonususer")
        admin = User.objects.create_user(
            username="rulesadmin", email="rulesadmin@flower.test", password="secret",
            referral_code="RULESADMIN", role="admin",
        )
        admin_client = APIClient()
        admin_client.force_authenticate(user=admin)
        saved = admin_client.post(f"/api/admin/users/{person.id}/adjust", {
            "kind": "balance_add", "amount": "25", "reason": "Eid gift", "request_key": "bonus-1",
        }, format="json")
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(BalanceAdjustment.objects.filter(user=person, reason="Eid gift").exists())
        self.client.force_authenticate(user=person)
        rows = self.client.get("/api/user/transactions?filter=bonus").json()
        self.assertEqual(rows[0]["title"], "Bonus")
        self.assertEqual(rows[0]["amount"], "25.00")
        self.assertNotIn("Eid gift", str(rows))
        admin_rows = admin_client.get(f"/api/admin/users/{person.id}/transactions?category=bonus").json()["rows"]
        self.assertEqual(admin_rows[0]["purpose"], "Eid gift")

    def test_signup_rejects_a_name_used_as_email(self):
        bad = self.client.post("/api/auth/register", {
            "full_name": "Ayaan", "email": "ayaan", "password": "secret1", "referral": self.sponsor.referral_code,
        }, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("valid email", bad.json()["detail"].lower())

    def test_level_three_blocks_plan_upgrade(self):
        person = self.approved("locked")
        for index in range(27):
            mate = self.approved(f"mate{index}")
            mate.referred_by = person
            mate.save(update_fields=["referred_by"])
        higher = Plan.objects.create(name="Premium", price=Decimal("10000.00"), daily_tasks=6)
        self.client.force_authenticate(user=person)
        shot = SimpleUploadedFile("slip.jpg", b"\xff\xd8\xff\x00", content_type="image/jpeg")
        blocked = self.client.post("/api/user/deposits", {
            "plan_id": higher.id, "sender": "03001111111", "sender_name": "Ayaan", "method": "JazzCash", "screenshot": shot,
        }, format="multipart")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("Level 3", blocked.json()["detail"])
        person.refresh_from_db()
        self.assertEqual(person.plan_id, self.plan.id)

    def test_ban_drops_upline_team_count_without_changing_balances(self):
        person = self.approved("banchild")
        before = self.sponsor.balance
        person.balance = Decimal("80.00")
        person.save(update_fields=["balance"])
        admin = User.objects.create_user(
            username="banadmin", email="banadmin@flower.test", password="secret",
            referral_code="BANADMIN", role="admin",
        )
        admin_client = APIClient()
        admin_client.force_authenticate(user=admin)
        banned = admin_client.post(f"/api/admin/users/{person.id}/ban", {"action": "ban", "reason": "Fake payment"}, format="json")
        self.assertEqual(banned.status_code, 200)
        self.sponsor.refresh_from_db()
        person.refresh_from_db()
        self.assertEqual(self.sponsor.team.filter(plan__isnull=False, account_status="approved").count(), 0)
        self.assertEqual(person.balance, Decimal("80.00"))
        self.assertEqual(self.sponsor.balance, before)
        self.client.force_authenticate(user=person)
        gate = self.client.get("/api/user/gate").json()
        self.assertEqual(gate["phase"], "banned")
        self.assertEqual(gate["reason"], "Fake payment")
        restored = admin_client.post(f"/api/admin/users/{person.id}/ban", {"action": "unban"}, format="json")
        self.assertEqual(restored.status_code, 200)
        self.sponsor.refresh_from_db()
        person.refresh_from_db()
        self.assertEqual(self.sponsor.team.filter(plan__isnull=False, account_status="approved").count(), 1)
        self.assertEqual(person.balance, Decimal("80.00"))
        self.assertEqual(self.sponsor.balance, before)
