import json
from datetime import datetime
from decimal import Decimal

import pymysql
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from flower.models import (
    Channel, ContactAdmin, Deposit, Gateway, Plan, SiteSetting, Transaction, UpgradeRequest, User, Withdrawal,
)

COLORS = {
    "Basic": ("#fff3cd", "#856404"),
    "Gold": ("#ffe08a", "#7a5800"),
    "Standard": ("#e7f1ff", "#0b5ed7"),
    "Premium": ("#efe7ff", "#5b2d8e"),
    "Super Premium": ("#fde8f3", "#9d174d"),
}
STATUS = {1: "approved", 2: "pending", 3: "rejected"}
TITLES = {
    1: "Direct Referral Commission",
    2: "Level 2 – Indirect Referral Commission",
    3: "Level 3 – Indirect Referral Commission",
}


def aware(value):
    if not value:
        return timezone.now()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


def money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"))


def status_name(value):
    return STATUS.get(int(value or 0), "pending")


def password_hash(value):
    raw = value or ""
    if raw.startswith("$2"):
        return "bcrypt$" + raw
    return "!"


def person_name(row):
    first = (row.get("first_name") or row.get("firstname") or "").strip()
    last = (row.get("last_name") or row.get("lastname") or "").strip()
    return (f"{first} {last}".strip() or row.get("username") or "User")[:150]


def json_text(value):
    if not value:
        return ""
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return str(value)[:80]
    if isinstance(data, list):
        parts = []
        for item in data:
            if isinstance(item, dict) and item.get("value"):
                parts.append(str(item["value"]))
        return " ".join(parts)[:80]
    if isinstance(data, dict):
        for key in ("sender", "number", "account", "account_number", "mobile"):
            if data.get(key):
                return str(data[key])[:80]
        return ""
    return str(data)[:80]


class Command(BaseCommand):
    help = "Copy the latest flowersblom backup tables into the new app database."

    def add_arguments(self, parser):
        parser.add_argument("--source-db", default="flowers_src")

    def handle(self, *args, **options):
        source = pymysql.connect(database=options["source_db"], user="flowers_app", password=self._password(), unix_socket="/var/run/mysqld/mysqld.sock", charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
        with source.cursor() as cursor:
            self.import_all(cursor)
        source.close()

    def _password(self):
        import os
        return os.environ.get("DB_PASSWORD", "")

    def import_all(self, cursor):
        if User.objects.filter(role="user").count() >= 16500 and Deposit.objects.count() >= 24000 and Transaction.objects.count() >= 80000:
            self.stdout.write("Flower data is already imported.")
            return
        self.stdout.write("import plans")
        self.stdout.flush()
        plans = self.plans(cursor)
        self.stdout.write("import users")
        self.stdout.flush()
        users, parents = self.users(cursor, plans)
        self.stdout.write("import referrals")
        self.stdout.flush()
        self.parents(parents, users)
        self.stdout.write("import admins")
        self.stdout.flush()
        self.admins(cursor, users)
        self.stdout.write("import deposits")
        self.stdout.flush()
        self.deposits(cursor, users, plans)
        self.stdout.write("import withdrawals")
        self.stdout.flush()
        self.withdrawals(cursor, users)
        self.stdout.write("import upgrades")
        self.stdout.flush()
        self.upgrades(cursor, users, plans)
        self.stdout.write("import transactions")
        self.stdout.flush()
        self.transactions(cursor, users, plans)
        self.stdout.write("import extras")
        self.stdout.flush()
        self.extras(cursor)
        self.stdout.write(self.style.SUCCESS(f"Imported {len(users)} users from the flowersblom backup."))
        self.stdout.flush()

    def plans(self, cursor):
        saved = {plan.id: plan for plan in Plan.objects.all()}
        if saved:
            return saved
        cursor.execute("SELECT id, name, price, daily_limit FROM plans ORDER BY id")
        saved = {}
        for index, row in enumerate(cursor.fetchall()):
            bg, fg = COLORS.get(row["name"], ("#fff3cd", "#856404"))
            plan = Plan.objects.create(id=row["id"], name=row["name"][:80], price=money(row["price"]), daily_tasks=row["daily_limit"] or 1, color=bg, text_color=fg, sort_order=index)
            saved[plan.id] = plan
        return saved

    def users(self, cursor, plans):
        cursor.execute("SELECT * FROM users ORDER BY id")
        rows = cursor.fetchall()
        codes = {code.lower() for code in User.objects.values_list("referral_code", flat=True) if code}
        names = {name.lower() for name in User.objects.values_list("username", flat=True) if name}
        batch = []
        parents = []
        ids = set(User.objects.values_list("id", flat=True))
        for row in rows:
            parent = int(row.get("ref_by") or 0)
            if row["id"] in ids:
                if parent:
                    parents.append((row["id"], parent))
                continue
            username = (row.get("username") or f"user{row['id']}")[:150]
            base_name = username
            name_n = 1
            while username.lower() in names:
                username = f"{base_name[:140]}{name_n}"
                name_n += 1
            names.add(username.lower())
            code = (row.get("ref") or row.get("username") or f"U{row['id']}")[:64]
            base = code
            suffix = 1
            while code.lower() in codes:
                code = f"{base[:50]}{suffix}"
                suffix += 1
            codes.add(code.lower())
            plan_id = row.get("plan_id") or 0
            user = User(
                id=row["id"],
                username=username,
                email=(row.get("email") or "")[:254],
                password=password_hash(row.get("password")),
                full_name=person_name(row),
                phone=(row.get("mobile") or "")[:30],
                role="user",
                plan=plans.get(plan_id),
                balance=money(row.get("balance")),
                referral_code=code,
                account_status="rejected" if int(row.get("status") or 0) == 0 else ("approved" if plan_id else "pending"),
                level=row.get("level") or 0,
                is_active=int(row.get("status") or 0) == 1,
                date_joined=aware(row.get("created_at")),
            )
            batch.append(user)
            ids.add(user.id)
            if parent:
                parents.append((user.id, parent))
            if len(batch) >= 200:
                User.objects.bulk_create(batch, batch_size=200)
                batch = []
        if batch:
            User.objects.bulk_create(batch, batch_size=200)
        return ids, parents

    def parents(self, parents, users):
        pairs = [(user_id, parent) for user_id, parent in parents if parent in users and parent != user_id]
        for start in range(0, len(pairs), 800):
            chunk = pairs[start:start + 800]
            case_sql = " ".join(["WHEN %s THEN %s"] * len(chunk))
            params = []
            ids = []
            for user_id, parent in chunk:
                params.extend([user_id, parent])
                ids.append(user_id)
            params.extend(ids)
            marks = ",".join(["%s"] * len(ids))
            with connection.cursor() as db:
                db.execute(
                    f"UPDATE flower_user SET referred_by_id = CASE id {case_sql} END WHERE id IN ({marks})",
                    params,
                )

    def admins(self, cursor, users):
        cursor.execute("SELECT id, name, email, username, password, created_at FROM admins")
        taken = set(User.objects.values_list("username", flat=True))
        emails = {email.lower() for email in User.objects.exclude(email="").values_list("email", flat=True)}
        for row in cursor.fetchall():
            email = (row.get("email") or "").strip().lower()
            if email and email in emails:
                continue
            username = (row.get("username") or f"admin{row['id']}")[:140]
            if username in taken:
                username = f"admin-{row['id']}"
            taken.add(username)
            if email:
                emails.add(email)
            User.objects.create(
                username=username,
                email=email,
                password=password_hash(row.get("password")),
                full_name=(row.get("name") or username)[:150],
                role="admin",
                referral_code=f"ADMIN{row['id']}",
                account_status="approved",
                is_staff=True,
                is_active=True,
                date_joined=aware(row.get("created_at")),
            )
        if not User.objects.filter(email__iexact="shoaib@gmail.com").exists():
            admin = User(username="shoaib", email="shoaib@gmail.com", full_name="Shoaib", role="admin", referral_code="SHOAIBADMIN", account_status="approved", is_staff=True, is_active=True)
            admin.set_password("shoaib123")
            admin.save()

    def deposits(self, cursor, users, plans):
        methods = self.method_names(cursor)
        cursor.execute("SELECT id, user_id, plan_id, amount, status, detail, created_at, updated_at, method_code FROM deposits WHERE status IN (1,2,3)")
        seen = set(Deposit.objects.values_list("id", flat=True))
        batch = []
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                if row["user_id"] not in users or row["id"] in seen:
                    continue
                batch.append(Deposit(
                    id=row["id"], user_id=row["user_id"], plan=plans.get(row.get("plan_id")), amount=money(row["amount"]),
                    method=(methods.get(row.get("method_code")) or "")[:80], sender=json_text(row.get("detail")),
                    status=status_name(row["status"]), created_at=aware(row.get("created_at")), updated_at=aware(row.get("updated_at")),
                ))
                if len(batch) >= 300:
                    Deposit.objects.bulk_create(batch, batch_size=300)
                    batch = []
            if batch:
                Deposit.objects.bulk_create(batch, batch_size=300)
                batch = []

    def withdrawals(self, cursor, users):
        cursor.execute("SELECT w.id, w.user_id, w.amount, w.status, w.withdraw_information, w.created_at, m.name method_name FROM withdrawals w LEFT JOIN withdraw_methods m ON m.id = w.method_id WHERE w.status IN (1,2,3)")
        seen = set(Withdrawal.objects.values_list("id", flat=True))
        batch = []
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                if row["user_id"] not in users or row["id"] in seen:
                    continue
                batch.append(Withdrawal(
                    id=row["id"], user_id=row["user_id"], amount=money(row["amount"]), method=(row.get("method_name") or "Withdrawal")[:80],
                    account=json_text(row.get("withdraw_information")) or "—", status=status_name(row["status"]), created_at=aware(row.get("created_at")),
                ))
                if len(batch) >= 300:
                    Withdrawal.objects.bulk_create(batch, batch_size=300)
                    batch = []
            if batch:
                Withdrawal.objects.bulk_create(batch, batch_size=300)
                batch = []

    def upgrades(self, cursor, users, plans):
        cursor.execute("SELECT id, user_id, from_plan_id, to_plan_id, amount, sender_number, status, created_at FROM plan_upgrades WHERE status IN (1,2,3)")
        seen = set(UpgradeRequest.objects.values_list("id", flat=True))
        batch = []
        for row in cursor.fetchall():
            if row["user_id"] not in users or row["id"] in seen or not plans.get(row.get("to_plan_id")):
                continue
            batch.append(UpgradeRequest(
                id=row["id"], user_id=row["user_id"], from_plan=plans.get(row.get("from_plan_id")), to_plan=plans.get(row["to_plan_id"]),
                amount=money(row["amount"]), sender=(row.get("sender_number") or "")[:80], status=status_name(row["status"]), created_at=aware(row.get("created_at")),
            ))
        UpgradeRequest.objects.bulk_create(batch, batch_size=1000)

    def transactions(self, cursor, users, plans):
        levels = {}
        try:
            cursor.execute("SELECT trx, to_id, level, from_id FROM commission_logs")
            for row in cursor.fetchall():
                levels[(row.get("trx"), row.get("to_id"))] = (int(row.get("level") or 1), row.get("from_id"))
        except pymysql.err.ProgrammingError:
            levels = {}
        emails = dict(User.objects.exclude(email="").values_list("id", "email"))
        cursor.execute("SELECT id, user_id, amount, trx_type, trx, details, remark, created_at FROM transactions")
        seen = set(Transaction.objects.values_list("id", flat=True))
        batch = []
        while True:
            rows = cursor.fetchmany(500)
            if not rows:
                break
            for row in rows:
                if row["user_id"] not in users or row["id"] in seen:
                    continue
                remark = row.get("remark") or "other"
                level = 0
                source = ""
                title = (row.get("details") or remark or "Transaction")[:160]
                if remark in ("referral_commission", "package_upgrade_referral_commission"):
                    level_raw, source_id = levels.get((row.get("trx"), row["user_id"]), (1, None))
                    try:
                        level = int(level_raw)
                    except (TypeError, ValueError):
                        level = 1
                    level = level if level in (1, 2, 3) else 1
                    title = TITLES[level]
                    remark = "referral"
                    source = emails.get(source_id, "")
                elif remark == "ptc_earn":
                    title, remark = "Ads Earning", "ads"
                elif remark == "withdraw":
                    title, remark = "Withdraw", "withdraw"
                batch.append(Transaction(
                    id=row["id"], user_id=row["user_id"], amount=money(row["amount"]), trx_type=(row.get("trx_type") or "+")[:1],
                    title=title, level=level, source_email=source[:180], remark=remark[:60], created_at=aware(row.get("created_at")),
                ))
                if len(batch) >= 300:
                    Transaction.objects.bulk_create(batch, batch_size=300)
                    batch = []
            if batch:
                Transaction.objects.bulk_create(batch, batch_size=300)
                batch = []

    def extras(self, cursor):
        cursor.execute("SELECT site_name FROM general_settings LIMIT 1")
        row = cursor.fetchone()
        setting = SiteSetting.current()
        if row and row.get("site_name"):
            setting.site_name = row["site_name"][:80]
            setting.save(update_fields=["site_name"])
        cursor.execute("SELECT id, name, status FROM gateways")
        for gateway in cursor.fetchall():
            Gateway.objects.get_or_create(id=gateway["id"], defaults={"name": (gateway.get("name") or "Gateway")[:80], "active": bool(gateway.get("status"))})
        cursor.execute("SELECT id, title, purpose, description, link, status, sort_order FROM official_channels")
        for channel in cursor.fetchall():
            Channel.objects.get_or_create(id=channel["id"], defaults={"title": channel["title"][:150], "purpose": (channel.get("purpose") or "")[:255], "description": channel.get("description") or "", "link": channel["link"][:500], "status": bool(channel.get("status")), "sort_order": channel.get("sort_order") or 0})
        cursor.execute("SELECT id, name, category, instructions, whatsapp_number FROM contact_admins")
        for contact in cursor.fetchall():
            ContactAdmin.objects.get_or_create(id=contact["id"], defaults={"name": contact["name"][:120], "role": (contact.get("category") or "")[:120], "whatsapp": (contact.get("whatsapp_number") or "")[:40], "note": (contact.get("instructions") or "")[:255]})

    def method_names(self, cursor):
        try:
            cursor.execute("SELECT code, name FROM gateways")
            return {row["code"]: row["name"] for row in cursor.fetchall() if row.get("code") is not None}
        except pymysql.err.ProgrammingError:
            return {}
