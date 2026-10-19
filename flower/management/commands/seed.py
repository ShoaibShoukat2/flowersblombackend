from decimal import Decimal
from django.core.management.base import BaseCommand
from flower.models import (
    Channel, ContactAdmin, Deposit, Gateway, Plan, Reward, SiteSetting,
    Task, Transaction, User, Withdrawal,
)


class Command(BaseCommand):
    help = "Load demo Flower data for the new React site. Does not touch Laravel."

    def handle(self, *args, **options):
        if User.objects.filter(email="admin@flower.test").exists():
            self.stdout.write("Demo data already exists.")
            return
        SiteSetting.current()
        plans = {}
        catalog = [
            ("Basic", "1500", 5, "#fff3cd", "#856404"),
            ("Standard", "3000", 8, "#e7f1ff", "#0b5ed7"),
            ("Gold", "5000", 10, "#ffe08a", "#7a5800"),
            ("Premium", "10000", 15, "#efe7ff", "#5b2d8e"),
            ("Super Premium", "20000", 20, "#fde8f3", "#9d174d"),
        ]
        for i, (name, price, tasks, bg, fg) in enumerate(catalog):
            plans[name] = Plan.objects.create(name=name, price=Decimal(price), daily_tasks=tasks, color=bg, text_color=fg, sort_order=i)
        admin = User(username="admin", email="admin@flower.test", full_name="Flower Admin", role="admin", referral_code="ADMIN", account_status="approved", is_staff=True)
        admin.set_password("Admin123")
        admin.save()

        def make(name, email, code, plan, parent=None, status="approved"):
            user = User(username=code, email=email, full_name=name, phone="03001234567", role="user", plan=plan, referral_code=code + "REF", referred_by=parent, account_status=status, level=1 if plan else 0, balance=Decimal("850"))
            user.set_password("Flower123")
            user.save()
            return user

        aisha = make("Aisha Khan", "aisha@flower.test", "X38D", plans["Premium"])
        hassan = make("Hassan Ali", "hassan@flower.test", "H91K", plans["Gold"], aisha)
        sara = make("Sara Malik", "sara@flower.test", "S22P", plans["Basic"], hassan)
        make("Bilal Raza", "bilal@flower.test", "B10Q", None, aisha, "pending")
        Deposit.objects.create(user=aisha, plan=plans["Premium"], amount=plans["Premium"].price, method="JazzCash", sender="03001111111", status=Deposit.APPROVED)
        Deposit.objects.create(user=hassan, plan=plans["Gold"], amount=plans["Gold"].price, method="Easypaisa", sender="03002222222", status=Deposit.APPROVED)
        Deposit.objects.create(user=sara, plan=plans["Basic"], amount=plans["Basic"].price, method="Easypaisa", sender="03003333333", status=Deposit.PENDING)
        Transaction.objects.create(user=aisha, amount=Decimal("125"), trx_type="+", title="Direct Referral Commission", level=1, plan_name="Gold", source_email="hassan@flower.test", remark="referral")
        Transaction.objects.create(user=aisha, amount=Decimal("15"), trx_type="+", title="Level 2 – Indirect Referral Commission", level=2, plan_name="Basic", source_email="sara@flower.test", remark="referral")
        Transaction.objects.create(user=hassan, amount=Decimal("37.50"), trx_type="+", title="Direct Referral Commission", level=1, plan_name="Basic", source_email="sara@flower.test", remark="referral")
        Transaction.objects.create(user=aisha, amount=Decimal("40"), trx_type="+", title="Ads Earning", remark="ads", plan_name="Premium")
        Withdrawal.objects.create(user=aisha, amount=Decimal("500"), method="JazzCash", account="03001111111", status=Withdrawal.PENDING)
        Task.objects.bulk_create([
            Task(title="Watch the training video", detail="Open the official guide and mark it complete.", reward=Decimal("20")),
            Task(title="Share your referral link", detail="Send your link to one new person.", reward=Decimal("15")),
            Task(title="Daily check-in", detail="Open Flower and confirm today's work.", reward=Decimal("10")),
        ])
        Channel.objects.create(title="Flowers Official Channel", purpose="Trainings | Guidelines | Earning Tips", description="Updates, rules, and daily earning tips.", link="https://flowersblom.com", sort_order=1)
        ContactAdmin.objects.create(name="Support Desk", role="Account help", whatsapp="923001234567", note="Payment and withdrawal questions")
        Gateway.objects.create(name="Easypaisa", account_name="Flower Earn", account_number="03001234567")
        Gateway.objects.create(name="JazzCash", account_name="Flower Earn", account_number="03007654321")
        Reward.objects.bulk_create([
            Reward(level=1, title="Level 1", required_team=3, prize="Bronze gift"),
            Reward(level=2, title="Level 2", required_team=12, prize="Silver gift"),
            Reward(level=3, title="Level 3", required_team=27, prize="Gold gift"),
        ])
        self.stdout.write("Demo ready. User aisha@flower.test / Flower123  Admin admin@flower.test / Admin123")
