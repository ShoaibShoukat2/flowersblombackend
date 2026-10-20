from datetime import timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .levels import FLOWER_AMOUNT, LEVEL_JOININGS, cycle_status, level_for_joinings, next_level_target, task_rate
from .models import (
    Channel, ContactAdmin, Deposit, Gateway, Plan, Reward, SiteSetting,
    Task, TaskClaim, TaskDone, Transaction, UpgradeRequest, User, Withdrawal,
)

LOGO = "/logo.png"


def money(value):
    return f"{Decimal(value):.2f}"


def media_url(file_field):
    if not file_field:
        return None
    return file_field.url


def slip_url(kind, pk, file_field):
    if not file_field:
        return None
    return f"/api/admin/{kind}/{pk}/slip"


def client_ip(request):
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return forwarded or request.META.get("REMOTE_ADDR") or "unknown"


def too_many(bucket, limit, seconds):
    key = f"guard:{bucket}"
    try:
        count = cache.get(key, 0) or 0
        if count >= limit:
            return True
        cache.set(key, count + 1, seconds)
    except Exception:
        return False
    return False


def image_upload(upload):
    if not upload:
        return None
    if upload.size > 8 * 1024 * 1024:
        return None
    head = upload.read(32)
    upload.seek(0)
    if head.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        ext = "webp"
    else:
        return None
    upload.name = f"{uuid4().hex}.{ext}"
    return upload


def send_slip(file_field):
    name = (file_field.name or "").lower()
    if name.endswith(".png"):
        content = "image/png"
    elif name.endswith(".webp"):
        content = "image/webp"
    else:
        content = "image/jpeg"
    try:
        handle = file_field.open("rb")
    except OSError:
        return Response(status=404)
    return FileResponse(handle, content_type=content)


def user_payload(user, request=None):
    return {
        "id": user.id,
        "full_name": user.display_name(),
        "username": user.username,
        "email": user.email,
        "phone": user.phone,
        "role": user.role,
        "balance": money(user.balance),
        "referral_code": user.referral_code,
        "account_status": user.account_status,
        "level": user.level,
        "plan": user.plan.name if user.plan else None,
        "plan_id": user.plan_id,
        "logo": LOGO,
    }


def require_admin(user):
    return user.is_authenticated and user.role == "admin"


def tx_public(tx, include_email=False):
    data = {
        "id": tx.id,
        "amount": money(tx.amount),
        "trx_type": tx.trx_type,
        "title": tx.title,
        "level": tx.level,
        "plan_name": tx.plan_name,
        "remark": tx.remark,
        "created_at": tx.created_at.isoformat(),
    }
    if include_email:
        data["source_email"] = tx.source_email
    return data


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    if too_many(f"login:{client_ip(request)}", 10, 600):
        return Response({"detail": "Too many sign-in attempts. Wait a few minutes and try again."}, status=429)
    ident = (request.data.get("identity") or "").strip()
    password = request.data.get("password") or ""
    role = request.data.get("role") or "user"
    user = User.objects.filter(email__iexact=ident).first() or User.objects.filter(username__iexact=ident).first() or User.objects.filter(phone=ident).first()
    if not user or not user.check_password(password) or user.role != role:
        return Response({"detail": "Email, username, or password is incorrect."}, status=400)
    Token.objects.filter(user=user).delete()
    token = Token.objects.create(user=user)
    return Response({"token": token.key, "user": user_payload(user)})


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    if too_many(f"register:{client_ip(request)}", 5, 3600):
        return Response({"detail": "Too many new accounts from this connection. Try again later."}, status=429)
    full_name = (request.data.get("full_name") or "").strip()
    email = (request.data.get("email") or "").strip().lower()
    phone = (request.data.get("phone") or "").strip()
    password = request.data.get("password") or ""
    code = (request.data.get("referral") or "").strip()
    if not full_name or not email or len(password) < 6:
        return Response({"detail": "Name, email, and a password of at least 6 characters are required."}, status=400)
    if User.objects.filter(email__iexact=email).exists():
        return Response({"detail": "This email is already registered."}, status=400)
    parent = User.objects.filter(referral_code__iexact=code, role="user").first() if code else None
    import secrets
    username = "X" + secrets.token_hex(2).upper()
    while User.objects.filter(username=username).exists():
        username = "X" + secrets.token_hex(2).upper()
    ref = secrets.token_hex(3)
    user = User(username=username, email=email, full_name=full_name, phone=phone, role="user", referral_code=ref, referred_by=parent, account_status="pending")
    user.set_password(password)
    user.save()
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "user": user_payload(user)})


@api_view(["GET"])
def me(request):
    settings = SiteSetting.current()
    data = user_payload(request.user)
    data["notice"] = settings.notice
    data["referral_link"] = request.build_absolute_uri(f"/register?ref={request.user.referral_code}")
    return Response(data)


def earning_total(qs):
    return money(qs.aggregate(total=Sum("amount"))["total"] or 0)


def approved_withdrawals_today(user):
    today = timezone.localdate()
    approved = user.withdrawals.filter(status=Withdrawal.APPROVED)
    return approved.filter(Q(updated_at__date=today) | Q(updated_at__isnull=True, created_at__date=today))


def approved_joinings(user):
    return user.team.filter(plan__isnull=False, account_status="approved")


def sync_level(user):
    if not user or getattr(user, "role", "") != "user":
        return 0
    count = approved_joinings(user).count()
    level = level_for_joinings(count)
    if user.level != level:
        user.level = level
        user.save(update_fields=["level"])
    return count


def credit_flower_once(user):
    if user.flower_credited:
        return
    user.flower_balance += FLOWER_AMOUNT
    user.flower_credited = True
    user.save(update_fields=["flower_balance", "flower_credited"])
    Transaction.objects.create(user=user, amount=FLOWER_AMOUNT, trx_type="+", title="Flower Earning", remark="flower")


def activate_member(user):
    if user.activated_at is None:
        user.activated_at = timezone.now()
        user.save(update_fields=["activated_at"])
    credit_flower_once(user)
    sync_level(user)
    if user.referred_by_id:
        sync_level(user.referred_by)


def joining_clock(user, count):
    latest = None
    for member in approved_joinings(user).only("activated_at", "date_joined"):
        stamp = member.activated_at or member.date_joined
        if latest is None or stamp > latest:
            latest = stamp
    started = user.activated_at or user.date_joined
    return cycle_status(user.level, count, started, latest, timezone.now())


def progress_payload(user, count):
    clock = joining_clock(user, count)
    next_level, required = next_level_target(user.level)
    if not user.plan_id:
        locked = True
        message = "Tasks are locked until your package payment is approved."
    elif clock["locked"]:
        locked = True
        message = f"Tasks are locked because no approved joining arrived in the last {clock['days']} days. When a new member's payment is approved, tasks unlock and the next cycle starts."
    else:
        locked = False
        message = f"Tasks are unlocked. Complete an approved joining within {clock['days']} days to keep them open."
    return {
        "level": user.level,
        "joinings": count,
        "next_level": next_level,
        "next_required": required,
        "task_rate": money(task_rate(user.level)),
        "daily_limit": user.plan.daily_tasks if user.plan else 0,
        "locked": locked,
        "task_status": "Locked" if locked else "Unlocked",
        "message": message,
        "cycle_days": clock["days"],
        "remaining_seconds": 0 if locked or not user.plan_id else clock["remaining"],
        "flower_balance": money(user.flower_balance),
        "flower_locked": user.level < 8,
    }
    today = timezone.localdate()
    approved = user.withdrawals.filter(status=Withdrawal.APPROVED)
    return approved.filter(Q(updated_at__date=today) | Q(updated_at__isnull=True, created_at__date=today))


def local_day_bounds():
    current = timezone.localtime()
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


@api_view(["GET"])
def user_home(request):
    user = request.user
    settings = SiteSetting.current()
    count = sync_level(user)
    day_start, day_end = local_day_bounds()
    done = TaskDone.objects.filter(user=user, created_at__gte=day_start, created_at__lt=day_end).count()
    txs = user.transactions.all()
    progress = progress_payload(user, count)
    return Response({
        "user": user_payload(user),
        "notice": settings.notice,
        "referral_link": request.build_absolute_uri(f"/register?ref={user.referral_code}"),
        "tasks_today": done,
        "daily_limit": progress["daily_limit"],
        "team_total": user.team.count(),
        "active_team": count,
        "progress": progress,
        "earnings": {
            "total": earning_total(txs.filter(trx_type="+")),
            "direct": earning_total(txs.filter(remark="referral", level=1)),
            "indirect": earning_total(txs.filter(remark="referral", level__in=[2, 3])),
            "ads": earning_total(txs.filter(remark="ads")),
            "withdrawn": earning_total(txs.filter(remark="withdraw")),
        },
        "deposits_total": earning_total(user.deposits.filter(status=Deposit.APPROVED)),
        "withdrawals_total": earning_total(user.withdrawals.filter(status=Withdrawal.APPROVED)),
        "transaction_count": user.transactions.count(),
        "upliner_name": user.referred_by.display_name() if user.referred_by_id else "",
        "wallet": {
            "withdrawn": earning_total(user.withdrawals.filter(status=Withdrawal.APPROVED)),
            "pending": earning_total(user.withdrawals.filter(status=Withdrawal.PENDING)),
            "rejected": earning_total(user.withdrawals.filter(status=Withdrawal.REJECTED)),
            "approved_today": earning_total(approved_withdrawals_today(user)),
            "staff": earning_total(txs.filter(remark="referral").exclude(level__in=[2, 3])),
            "indirect": earning_total(txs.filter(remark="referral", level__in=[2, 3])),
            "indirect_level2": earning_total(txs.filter(remark="referral", level=2)),
            "indirect_level3": earning_total(txs.filter(remark="referral", level=3)),
            "flowers": money(user.flower_balance),
            "flower_locked": user.level < 8,
        },
    })


TASK_CATALOG = [
    ("Rose", "Open the rose and collect this flower's earning."),
    ("Jasmine", "Open the jasmine and collect this flower's earning."),
    ("Tulip", "Open the tulip and collect this flower's earning."),
    ("Lily", "Open the lily and collect this flower's earning."),
    ("Sunflower", "Open the sunflower and collect this flower's earning."),
    ("Orchid", "Open the orchid and collect this flower's earning."),
    ("Daisy", "Open the daisy and collect this flower's earning."),
    ("Marigold", "Open the marigold and collect this flower's earning."),
    ("Lotus", "Open the lotus and collect this flower's earning."),
    ("Lavender", "Open the lavender and collect this flower's earning."),
    ("Peony", "Open the peony and collect this flower's earning."),
    ("Iris", "Open the iris and collect this flower's earning."),
]


def ensure_task_catalog():
    for title, detail in TASK_CATALOG:
        task, created = Task.objects.get_or_create(title=title, defaults={"detail": detail, "reward": 0, "active": True})
        if not created and (task.detail != detail or not task.active):
            task.detail = detail
            task.active = True
            task.save(update_fields=["detail", "active"])
    Task.objects.filter(title="Daily task").update(active=False)
    return list(Task.objects.filter(active=True).order_by("id"))


def today_tasks(limit):
    catalog = ensure_task_catalog()
    if not catalog or limit <= 0:
        return []
    start = timezone.localdate().toordinal() % len(catalog)
    seen = set()
    chosen = []
    for slot in range(limit):
        task = None
        for step in range(len(catalog)):
            candidate = catalog[(start + slot + step) % len(catalog)]
            if candidate.id not in seen:
                task = candidate
                break
        if task is None:
            break
        seen.add(task.id)
        chosen.append(task)
    return chosen


@api_view(["GET"])
def tasks(request):
    user = request.user
    count = sync_level(user)
    progress = progress_payload(user, count)
    day_start, day_end = local_day_bounds()
    done_today = TaskDone.objects.filter(user=user, created_at__gte=day_start, created_at__lt=day_end).count()
    opened = set(TaskDone.objects.filter(user=user, created_at__gte=day_start, created_at__lt=day_end).values_list("task_id", flat=True))
    limit = progress["daily_limit"]
    rate = progress["task_rate"]
    limit_full = done_today >= limit
    rows = []
    for slot, task in enumerate(today_tasks(limit)):
        rows.append({
            "id": task.id,
            "slot": slot,
            "title": task.title,
            "detail": task.detail,
            "reward": rate,
            "done": limit_full or task.id in opened,
        })
    return Response({"tasks": rows, "done_today": done_today, "progress": progress})


@api_view(["POST"])
def complete_task(request, pk):
    task = Task.objects.filter(pk=pk, active=True).first()
    if not task:
        return Response({"detail": "Task not found."}, status=404)
    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            count = sync_level(user)
            progress = progress_payload(user, count)
            if progress["locked"]:
                return Response({"detail": progress["message"]}, status=400)
            assigned = {item.id for item in today_tasks(progress["daily_limit"])}
            if task.id not in assigned:
                return Response({"detail": "This flower is not open today."}, status=400)
            day_start, day_end = local_day_bounds()
            done_today = TaskDone.objects.filter(user=user, created_at__gte=day_start, created_at__lt=day_end).count()
            if done_today >= progress["daily_limit"]:
                return Response({"detail": "Today's task limit is complete."}, status=400)
            if TaskClaim.objects.filter(user=user, task=task, day=day_start.date()).exists():
                return Response({"detail": "This flower is already open today."}, status=400)
            TaskClaim.objects.create(user=user, task=task, day=day_start.date())
            reward = task_rate(user.level)
            TaskDone.objects.create(user=user, task=task)
            user.balance += reward
            user.save(update_fields=["balance"])
            Transaction.objects.create(user=user, amount=reward, trx_type="+", title="Task Earning", remark="ads", plan_name=user.plan.name if user.plan else "")
    except IntegrityError:
        return Response({"detail": "This flower is already open today."}, status=400)
    return Response({"balance": money(user.balance), "reward": money(reward)})


@api_view(["GET"])
def transactions(request):
    qs = request.user.transactions.all()
    kind = request.GET.get("filter") or ""
    if kind == "ads":
        qs = qs.filter(remark="ads")
    elif kind == "direct":
        qs = qs.filter(remark="referral", level=1)
    elif kind == "level2":
        qs = qs.filter(remark="referral", level=2)
    elif kind == "level3":
        qs = qs.filter(remark="referral", level=3)
    elif kind == "indirect":
        qs = qs.filter(remark="referral", level__in=[2, 3])
    elif kind == "withdraw":
        qs = qs.filter(remark="withdraw")
    return Response([tx_public(tx, include_email=tx.level == 1) for tx in qs[:100]])


@api_view(["GET"])
def team(request):
    rows = []
    for member in request.user.team.select_related("plan").order_by("-id"):
        if not member.plan_id or member.account_status != "approved":
            continue
        rows.append({
            "id": member.id,
            "full_name": member.display_name(),
            "email": member.email,
            "level": member.level,
            "plan": member.plan.name if member.plan else None,
            "status": "Paid",
            "joined": member.date_joined.isoformat(),
        })
    return Response(rows)


@api_view(["GET"])
def rewards(request):
    user = request.user
    count = sync_level(user)
    items = []
    for step, required in LEVEL_JOININGS.items():
        items.append({
            "id": step,
            "level": step,
            "title": f"Level {step}",
            "required_team": required,
            "prize": f"Rs {task_rate(step)} per task",
            "reached": count >= required,
        })
    return Response({"active_team": count, "level": user.level, "task_rate": money(task_rate(user.level)), "items": items})


@api_view(["GET"])
def channels(request):
    rows = []
    for channel in Channel.objects.filter(status=True):
        rows.append({
            "id": channel.id,
            "title": channel.title,
            "purpose": channel.purpose,
            "description": channel.description,
            "link": channel.link,
            "image": media_url(channel.image),
        })
    return Response(rows)


@api_view(["GET", "POST"])
def withdrawals(request):
    if request.method == "POST":
        try:
            amount = Decimal(str(request.data.get("amount") or "0"))
        except (InvalidOperation, ValueError):
            return Response({"detail": "Enter a valid amount."}, status=400)
        method = (request.data.get("method") or "").strip()
        account_name = (request.data.get("account_name") or "").strip()
        account = (request.data.get("account") or "").strip()
        if amount <= 0 or not method or not account_name or not account:
            return Response({"detail": "Amount, method, account name, and account number are required."}, status=400)
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            sync_level(user)
            flower_available = user.flower_balance if user.level >= 8 else Decimal("0")
            if user.balance + flower_available < amount:
                return Response({"detail": "Not enough balance."}, status=400)
            flower_part = amount - user.balance if amount > user.balance else Decimal("0")
            user.balance -= amount - flower_part
            user.flower_balance -= flower_part
            user.save(update_fields=["balance", "flower_balance"])
            Withdrawal.objects.create(
                user=user, amount=amount, method=method, account_name=account_name,
                account=account, flower_part=flower_part, updated_at=timezone.now(),
            )
            Transaction.objects.create(user=user, amount=amount, trx_type="-", title="Withdraw", remark="withdraw")
        return Response({"balance": money(user.balance)})
    rows = [{
        "id": w.id, "amount": money(w.amount), "method": w.method,
        "account_name": w.account_name, "account": w.account, "status": w.status,
        "created_at": w.created_at.isoformat(),
    } for w in request.user.withdrawals.order_by("-id")]
    return Response(rows)


@api_view(["GET"])
def contacts(request):
    return Response([{"id": c.id, "name": c.name, "role": c.role, "whatsapp": c.whatsapp, "note": c.note} for c in ContactAdmin.objects.all()])


@api_view(["GET"])
def plans(request):
    user = request.user
    pending = user.deposits.filter(status=Deposit.PENDING).select_related("plan").order_by("-id").first()
    upgrade = UpgradeRequest.objects.filter(user=user, status=UpgradeRequest.PENDING).select_related("to_plan").order_by("-id").first()
    return Response({
        "current_plan_id": user.plan_id,
        "current_plan": user.plan.name if user.plan else None,
        "plans": [{"id": p.id, "name": p.name, "price": money(p.price), "daily_tasks": p.daily_tasks, "color": p.color, "text_color": p.text_color} for p in Plan.objects.all()],
        "gateways": [{"id": g.id, "name": g.name, "account_name": g.account_name, "account_number": g.account_number} for g in Gateway.objects.filter(active=True) if g.name.lower().replace(" ", "") in ("jazzcash", "easypaisa") and (g.account_number or "").strip()],
        "pending_deposit": None if not pending else {"id": pending.id, "plan_id": pending.plan_id, "plan": pending.plan.name if pending.plan else "", "amount": money(pending.amount), "method": pending.method, "sender": pending.sender, "status": pending.status, "has_screenshot": bool(pending.screenshot)},
        "pending_upgrade": None if not upgrade else {"id": upgrade.id, "plan_id": upgrade.to_plan_id, "plan": upgrade.to_plan.name if upgrade.to_plan else "", "amount": money(upgrade.amount), "method": upgrade.method, "sender": upgrade.sender, "status": upgrade.status, "has_screenshot": bool(upgrade.screenshot)},
    })


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def deposit_create(request):
    plan_id = request.data.get("plan_id")
    if not str(plan_id or "").isdigit():
        return Response({"detail": "Choose a plan."}, status=400)
    plan = Plan.objects.filter(pk=plan_id).first()
    if not plan:
        return Response({"detail": "Choose a plan."}, status=400)
    user = request.user
    sender = (request.data.get("sender") or "").strip()
    method = (request.data.get("method") or "").strip()
    if not sender or not method:
        return Response({"detail": "Choose JazzCash or Easypaisa and enter the sender number."}, status=400)
    shot = image_upload(request.FILES.get("screenshot"))
    if not shot:
        return Response({"detail": "Payment screenshot must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
    if user.plan_id:
        if plan.id == user.plan_id:
            return Response({"detail": "This is already your current plan."}, status=400)
        if user.plan and plan.price <= user.plan.price:
            return Response({"detail": "Choose a higher plan to upgrade."}, status=400)
        waiting = UpgradeRequest.objects.filter(user=user, status=UpgradeRequest.PENDING).order_by("-id").first()
        if waiting and waiting.screenshot:
            return Response({"detail": "An upgrade request is already waiting for admin approval."}, status=400)
        if waiting:
            waiting.from_plan = user.plan
            waiting.to_plan = plan
            waiting.amount = plan.price
            waiting.method = method
            waiting.sender = sender
            waiting.screenshot = shot
            waiting.save()
            return Response({"kind": "upgrade", "status": "pending"})
        UpgradeRequest.objects.create(
            user=user, from_plan=user.plan, to_plan=plan, amount=plan.price,
            method=method, sender=sender, screenshot=shot,
        )
        return Response({"kind": "upgrade", "status": "pending"})
    pending = user.deposits.filter(status=Deposit.PENDING).order_by("-id").first()
    if pending and pending.screenshot:
        return Response({"detail": "A plan payment is already waiting for admin approval."}, status=400)
    if pending:
        pending.plan = plan
        pending.amount = plan.price
        pending.method = method
        pending.sender = sender
        pending.screenshot = shot
        pending.save()
        return Response({"id": pending.id, "kind": "deposit", "status": pending.status})
    deposit = Deposit.objects.create(
        user=user, plan=plan, amount=plan.price, method=method, sender=sender,
        screenshot=shot, status=Deposit.PENDING,
    )
    if user.account_status != "approved":
        user.account_status = "pending"
        user.save(update_fields=["account_status"])
    return Response({"id": deposit.id, "kind": "deposit", "status": deposit.status})


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def upgrade_create(request):
    plan_id = request.data.get("plan_id")
    if not str(plan_id or "").isdigit():
        return Response({"detail": "Choose a plan."}, status=400)
    plan = Plan.objects.filter(pk=plan_id).first()
    if not plan:
        return Response({"detail": "Choose a plan."}, status=400)
    shot = image_upload(request.FILES.get("screenshot"))
    if not shot:
        return Response({"detail": "Payment screenshot must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
    UpgradeRequest.objects.create(
        user=request.user, from_plan=request.user.plan, to_plan=plan, amount=plan.price,
        method=(request.data.get("method") or "").strip(),
        sender=(request.data.get("sender") or "").strip(),
        screenshot=shot,
    )
    return Response({"ok": True})


def credit_referral_amount(user, amount, plan):
    settings = SiteSetting.current()
    percents = {1: settings.direct_percent, 2: settings.level2_percent, 3: settings.level3_percent}
    titles = {1: "Direct Referral Commission", 2: "Level 2 – Indirect Referral Commission", 3: "Level 3 – Indirect Referral Commission"}
    current = user.referred_by
    level = 1
    while current and level <= 3:
        commission = (Decimal(amount) * percents[level]) / Decimal("100")
        current.balance += commission
        current.save(update_fields=["balance"])
        Transaction.objects.create(
            user=current, amount=commission, trx_type="+", title=titles[level], level=level,
            plan_name=plan.name if plan else "", source_email=user.email, remark="referral",
        )
        current = current.referred_by
        level += 1


def credit_referral(deposit):
    credit_referral_amount(deposit.user, deposit.amount, deposit.plan)


@api_view(["GET"])
def admin_summary(request):
    if not require_admin(request.user):
        return Response({"detail": "Admin only."}, status=403)
    today = timezone.localdate()
    approved_deposits = Deposit.objects.filter(status=Deposit.APPROVED)
    approved_withdrawals = Withdrawal.objects.filter(status=Withdrawal.APPROVED)
    manual_add = Transaction.objects.filter(trx_type="+").exclude(remark__in=["referral", "ads"])
    manual_sub = Transaction.objects.filter(trx_type="-").exclude(remark="withdraw")
    task_earn = Transaction.objects.filter(remark="ads")
    return Response({
        "users": User.objects.filter(role="user").count(),
        "pending_deposits": Deposit.objects.filter(status=Deposit.PENDING).count(),
        "pending_withdrawals": Withdrawal.objects.filter(status=Withdrawal.PENDING).count(),
        "approved_today": Deposit.objects.filter(status=Deposit.APPROVED, updated_at__date=today).count(),
        "withdraw_today": approved_withdrawals.filter(created_at__date=today).count(),
        "pending_upgrades": UpgradeRequest.objects.filter(status=UpgradeRequest.PENDING).count(),
        "rejected_users": User.objects.filter(role="user", account_status="rejected").count(),
        "money": {
            "referral": money(_sum(Transaction.objects.filter(remark="referral"))),
            "deposited": money(_sum(approved_deposits)),
            "approved_users_today": User.objects.filter(role="user", deposits__status=Deposit.APPROVED, deposits__updated_at__date=today).distinct().count(),
            "withdrawals_today": money(_sum(approved_withdrawals.filter(created_at__date=today))),
            "received_today": money(_sum(approved_deposits.filter(updated_at__date=today))),
            "withdrawn": money(_sum(approved_withdrawals)),
            "task": money(_sum(task_earn)),
            "task_today": money(_sum(task_earn.filter(created_at__date=today))),
            "manual_add": money(_sum(manual_add)),
            "manual_add_today": money(_sum(manual_add.filter(created_at__date=today))),
            "manual_sub": money(_sum(manual_sub)),
            "manual_sub_today": money(_sum(manual_sub.filter(created_at__date=today))),
            "balances": money(_sum(User.objects.filter(role="user"), "balance")),
        },
    })


def _sum(qs, field="amount"):
    return qs.aggregate(total=Sum(field))["total"] or Decimal("0")


@api_view(["GET"])
def admin_analytics(request):
    if not require_admin(request.user):
        return Response(status=403)
    sales = _sum(Deposit.objects.filter(status=Deposit.APPROVED))
    direct = _sum(Transaction.objects.filter(remark="referral", level=1))
    indirect = _sum(Transaction.objects.filter(remark="referral", level__in=[2, 3]))
    ads = _sum(Transaction.objects.filter(remark="ads"))
    credited = direct + indirect + ads
    withdrawn = _sum(Withdrawal.objects.filter(status=Withdrawal.APPROVED))
    pending_withdraw = _sum(Withdrawal.objects.filter(status=Withdrawal.PENDING))
    balances = _sum(User.objects.filter(role="user"), "balance")
    profit = sales - credited
    today = timezone.localdate()
    days = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        days.append({
            "label": day.strftime("%a"),
            "sales": float(_sum(Deposit.objects.filter(status=Deposit.APPROVED, updated_at__date=day))),
            "earnings": float(_sum(Transaction.objects.filter(trx_type="+", created_at__date=day))),
            "withdrawals": float(_sum(Withdrawal.objects.filter(status=Withdrawal.APPROVED, created_at__date=day))),
        })
    return Response({
        "sales": money(sales),
        "direct": money(direct),
        "indirect": money(indirect),
        "ads": money(ads),
        "credited": money(credited),
        "withdrawn": money(withdrawn),
        "pending_withdraw": money(pending_withdraw),
        "balances": money(balances),
        "profit": money(abs(profit)),
        "result": "profit" if profit >= 0 else "loss",
        "days": days,
        "mix": [
            {"label": "Plan sales", "value": float(sales)},
            {"label": "Direct commission", "value": float(direct)},
            {"label": "Indirect commission", "value": float(indirect)},
            {"label": "Ads earning", "value": float(ads)},
            {"label": "Withdrawn", "value": float(withdrawn)},
        ],
    })


def person_brief(user):
    if not user:
        return None
    return {
        "id": user.id,
        "full_name": user.display_name(),
        "email": user.email,
        "phone": user.phone or "",
        "username": user.username,
        "plan": user.plan.name if user.plan_id else None,
        "level": user.level,
        "balance": money(user.balance),
        "status": user.account_status,
        "paid": bool(user.plan_id),
    }


def team_pack(user):
    members = []
    total = active = pending = 0
    for member in user.team.select_related("plan").order_by("-id"):
        total += 1
        if member.plan_id and member.account_status == "approved":
            active += 1
        elif member.account_status == "pending":
            pending += 1
        if len(members) < 400:
            members.append({
                "id": member.id,
                "full_name": member.display_name(),
                "email": member.email,
                "phone": member.phone or "",
                "plan": member.plan.name if member.plan_id else None,
                "level": member.level,
                "status": member.account_status,
                "paid": bool(member.plan_id),
                "joined": member.date_joined.isoformat(),
            })
    return {
        "total": total,
        "active": active,
        "pending": pending,
        "level2": User.objects.filter(referred_by__referred_by=user, role="user").count(),
        "level3": User.objects.filter(referred_by__referred_by__referred_by=user, role="user").count(),
        "shown": len(members),
        "members": members,
    }


def member_row(user):
    deposit = user.deposits.order_by("-id").first()
    when = deposit.updated_at if deposit and deposit.status == Deposit.APPROVED else user.date_joined
    return {
        "id": user.id,
        "full_name": user.display_name(),
        "email": user.email,
        "username": user.username,
        "plan": user.plan.name if user.plan else None,
        "plan_color": user.plan.color if user.plan else "#fff3cd",
        "plan_text": user.plan.text_color if user.plan else "#856404",
        "paid": bool(user.plan_id),
        "balance": money(user.balance),
        "level": user.level,
        "active_team": user.team.filter(plan__isnull=False).count(),
        "earnings": money(user.transactions.filter(trx_type="+").aggregate(s=Sum("amount"))["s"] or 0),
        "status": user.account_status,
        "date": when.isoformat(),
        "phone": user.phone,
    }


@api_view(["GET"])
def admin_users(request):
    if not require_admin(request.user):
        return Response(status=403)
    status = request.GET.get("status") or "approved"
    qs = User.objects.filter(role="user").select_related("plan")
    today = timezone.localdate()
    if status == "approved":
        qs = qs.filter(account_status="approved", plan__isnull=False)
    elif status == "pending":
        qs = qs.filter(account_status="pending")
    elif status == "rejected":
        qs = qs.filter(account_status="rejected")
    elif status == "today":
        qs = qs.filter(deposits__status=Deposit.APPROVED, deposits__updated_at__date=today).distinct()
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(email__icontains=q) | Q(phone__icontains=q) | Q(username__icontains=q) | Q(referral_code__icontains=q))
    return Response([member_row(u) for u in qs.order_by("-id")[:200]])


@api_view(["GET"])
def admin_user_detail(request, pk):
    if not require_admin(request.user):
        return Response(status=403)
    user = User.objects.select_related("plan", "referred_by", "referred_by__plan").filter(pk=pk, role="user").first()
    if not user:
        return Response(status=404)
    team = [member_row(m) for m in user.team.select_related("plan").order_by("-id")]
    txs = [tx_public(tx, include_email=True) for tx in user.transactions.all()[:40]]
    slips = []
    for deposit in user.deposits.select_related("plan").order_by("-id")[:12]:
        slips.append({
            "id": deposit.id,
            "plan": deposit.plan.name if deposit.plan else None,
            "amount": money(deposit.amount),
            "method": deposit.method,
            "sender": deposit.sender,
            "status": deposit.status,
            "created_at": deposit.created_at.isoformat(),
            "screenshot": slip_url("deposits", deposit.id, deposit.screenshot),
        })
    withdrawals = []
    for item in user.withdrawals.order_by("-id")[:12]:
        withdrawals.append({
            "id": item.id,
            "amount": money(item.amount),
            "method": item.method,
            "account_name": item.account_name,
            "account": item.account,
            "status": item.status,
            "created_at": item.created_at.isoformat(),
        })
    upgrades = []
    for row in UpgradeRequest.objects.filter(user=user).select_related("from_plan", "to_plan").order_by("-id")[:12]:
        upgrades.append({
            "id": row.id,
            "from_plan": row.from_plan.name if row.from_plan else "—",
            "to_plan": row.to_plan.name if row.to_plan else "—",
            "amount": money(row.amount),
            "method": row.method,
            "sender": row.sender,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
            "screenshot": slip_url("upgrades", row.id, row.screenshot),
        })
    return Response({
        "user": member_row(user),
        "profile": {
            "balance": money(user.balance),
            "deposits": money(_sum(user.deposits.filter(status=Deposit.APPROVED))),
            "withdrawals": money(_sum(user.withdrawals.filter(status=Withdrawal.APPROVED))),
            "transactions": user.transactions.count(),
            "team": len(team),
            "level": user.level,
            "phone": user.phone or "",
            "email": user.email,
            "full_name": user.display_name(),
            "referral_code": user.referral_code,
            "flower_balance": money(user.flower_balance),
            "status": user.account_status,
        },
        "upliner": person_brief(user.referred_by),
        "team": team,
        "transactions": txs,
        "deposits": slips,
        "withdrawals": withdrawals,
        "upgrades": upgrades,
    })


@api_view(["GET"])
def leaderboard(request):
    if not require_admin(request.user):
        return Response(status=403)
    users = User.objects.filter(role="user", plan__isnull=False).select_related("plan").annotate(active=Count("team"))
    rows = []
    for user in users:
        row = member_row(user)
        row["active_team"] = user.team.filter(plan__isnull=False).count()
        rows.append(row)
    rows.sort(key=lambda r: (-r["level"], -r["active_team"], -Decimal(r["earnings"])))
    return Response(rows)


@api_view(["GET"])
def admin_transactions(request):
    if not require_admin(request.user):
        return Response(status=403)
    qs = Transaction.objects.select_related("user").order_by("-id")
    category = request.GET.get("category") or ""
    if category == "direct_traffic":
        qs = qs.filter(remark="referral", level=1)
    elif category == "level_2":
        qs = qs.filter(level=2)
    elif category == "level_3":
        qs = qs.filter(level=3)
    elif category == "withdraw":
        qs = qs.filter(remark="withdraw")
    elif category == "ads":
        qs = qs.filter(remark="ads")
    elif category == "manual_add":
        qs = qs.filter(trx_type="+").exclude(remark__in=["referral", "ads"])
    elif category == "manual_sub":
        qs = qs.filter(trx_type="-").exclude(remark="withdraw")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(user__full_name__icontains=q) | Q(user__email__icontains=q) | Q(title__icontains=q) | Q(source_email__icontains=q))
    rows = []
    for tx in qs[:150]:
        item = tx_public(tx, include_email=True)
        item["user_id"] = tx.user_id
        item["user_name"] = tx.user.display_name()
        item["user_email"] = tx.user.email
        rows.append(item)
    return Response(rows)


@api_view(["GET"])
def admin_deposits(request):
    if not require_admin(request.user):
        return Response(status=403)
    status = request.GET.get("status") or "pending"
    qs = Deposit.objects.select_related("user", "user__referred_by", "user__referred_by__plan", "plan").order_by("-id")
    if status != "all":
        qs = qs.filter(status=status)
    if request.GET.get("today") == "1":
        qs = qs.filter(updated_at__date=timezone.localdate())
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(user__full_name__icontains=q) | Q(user__email__icontains=q) | Q(sender__icontains=q) | Q(method__icontains=q))
    return Response([{
        "id": d.id, "user_id": d.user_id, "full_name": d.user.display_name(), "email": d.user.email,
        "username": d.user.username, "phone": d.user.phone or "",
        "plan": d.plan.name if d.plan else None, "amount": money(d.amount), "method": d.method,
        "sender": d.sender, "status": d.status, "created_at": d.created_at.isoformat(),
        "screenshot": slip_url("deposits", d.id, d.screenshot),
        "upliner": person_brief(d.user.referred_by),
    } for d in qs])


@api_view(["GET"])
def deposit_slip(request, pk):
    if not require_admin(request.user):
        return Response(status=403)
    row = Deposit.objects.filter(pk=pk).first()
    if not row or not row.screenshot:
        return Response(status=404)
    return send_slip(row.screenshot)


@api_view(["GET"])
def upgrade_slip(request, pk):
    if not require_admin(request.user):
        return Response(status=403)
    row = UpgradeRequest.objects.filter(pk=pk).first()
    if not row or not row.screenshot:
        return Response(status=404)
    return send_slip(row.screenshot)


@api_view(["POST"])
def deposit_action(request, pk):
    if not require_admin(request.user):
        return Response(status=403)
    deposit = Deposit.objects.select_related("user", "plan").filter(pk=pk).first()
    if not deposit:
        return Response(status=404)
    action = request.data.get("action")
    if action == "approve" and deposit.status != Deposit.APPROVED:
        deposit.status = Deposit.APPROVED
        deposit.save()
        deposit.user.plan = deposit.plan
        deposit.user.account_status = "approved"
        deposit.user.save()
        activate_member(deposit.user)
        credit_referral(deposit)
    elif action == "reject":
        deposit.status = Deposit.REJECTED
        deposit.save()
        deposit.user.account_status = "rejected"
        deposit.user.save(update_fields=["account_status"])
    return Response({"ok": True})


@api_view(["GET"])
def admin_withdrawals(request):
    if not require_admin(request.user):
        return Response(status=403)
    status = request.GET.get("status") or "pending"
    qs = Withdrawal.objects.select_related("user").order_by("-id")
    if status != "all":
        qs = qs.filter(status=status)
    if request.GET.get("today") == "1":
        qs = qs.filter(created_at__date=timezone.localdate())
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(user__full_name__icontains=q) | Q(user__email__icontains=q) | Q(account__icontains=q) | Q(method__icontains=q))
    return Response([{
        "id": w.id, "user_id": w.user_id, "full_name": w.user.display_name(), "email": w.user.email,
        "username": w.user.username, "phone": w.user.phone or "",
        "amount": money(w.amount), "method": w.method, "account_name": w.account_name, "account": w.account, "status": w.status,
        "created_at": w.created_at.isoformat(),
    } for w in qs])


@api_view(["GET", "POST"])
def withdrawal_action(request, pk):
    if not require_admin(request.user):
        return Response(status=403)
    row = Withdrawal.objects.select_related("user", "user__plan", "user__referred_by", "user__referred_by__plan").filter(pk=pk).first()
    if not row:
        return Response(status=404)
    if request.method == "GET":
        return Response({
            "account": person_brief(row.user),
            "upliner": person_brief(row.user.referred_by),
            "team": team_pack(row.user),
        })
    action = request.data.get("action")
    if action == "approve":
        row.status = Withdrawal.APPROVED
        row.updated_at = timezone.now()
    elif action == "reject":
        if row.status == Withdrawal.PENDING:
            row.user.balance += row.amount - row.flower_part
            row.user.flower_balance += row.flower_part
            row.user.save(update_fields=["balance", "flower_balance"])
        row.status = Withdrawal.REJECTED
        row.updated_at = timezone.now()
    row.save()
    return Response({"ok": True})


@api_view(["GET"])
def admin_upgrades(request):
    if not require_admin(request.user):
        return Response(status=403)
    status = request.GET.get("status") or "pending"
    rows = UpgradeRequest.objects.select_related("user", "from_plan", "to_plan").order_by("-id")
    if status != "all":
        rows = rows.filter(status=status)
    q = (request.GET.get("q") or "").strip()
    if q:
        rows = rows.filter(Q(user__full_name__icontains=q) | Q(user__email__icontains=q) | Q(sender__icontains=q) | Q(method__icontains=q))
    return Response([{
        "id": r.id, "user_id": r.user_id, "full_name": r.user.display_name(), "email": r.user.email,
        "from_plan": r.from_plan.name if r.from_plan else "—", "to_plan": r.to_plan.name if r.to_plan else "—",
        "plan": r.to_plan.name if r.to_plan else "—", "amount": money(r.amount), "method": r.method,
        "sender": r.sender, "status": r.status,
        "screenshot": slip_url("upgrades", r.id, r.screenshot),
    } for r in rows])


@api_view(["POST"])
def upgrade_action(request, pk):
    if not require_admin(request.user):
        return Response(status=403)
    row = UpgradeRequest.objects.select_related("user", "to_plan").filter(pk=pk).first()
    if not row:
        return Response(status=404)
    if request.data.get("action") == "approve" and row.status != UpgradeRequest.APPROVED:
        row.status = UpgradeRequest.APPROVED
        row.user.plan = row.to_plan
        row.user.account_status = "approved"
        row.user.save(update_fields=["plan", "account_status", "activated_at", "flower_balance", "flower_credited", "level"])
        activate_member(row.user)
        credit_referral_amount(row.user, row.amount, row.to_plan)
    elif request.data.get("action") == "reject":
        row.status = UpgradeRequest.REJECTED
    row.save()
    return Response({"ok": True})


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def admin_channels(request):
    if not require_admin(request.user):
        return Response(status=403)
    if request.method == "POST":
        picture = request.FILES.get("image")
        if picture:
            picture = image_upload(picture)
            if not picture:
                return Response({"detail": "Channel image must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
        link = (request.data.get("link") or "https://flowersblom.com").strip()
        if not link.startswith("https://") and not link.startswith("http://"):
            link = "https://flowersblom.com"
        channel = Channel.objects.create(
            title=request.data.get("title") or "Channel",
            purpose=request.data.get("purpose") or "",
            description=request.data.get("description") or "",
            link=link,
            status=str(request.data.get("status", "1")) in ("1", "true", "True"),
            image=picture,
        )
        return Response({"id": channel.id})
    rows = []
    for channel in Channel.objects.all():
        rows.append({"id": channel.id, "title": channel.title, "purpose": channel.purpose, "description": channel.description, "link": channel.link, "status": channel.status, "image": media_url(channel.image)})
    return Response(rows)


@api_view(["GET"])
def admin_contacts(request):
    if not require_admin(request.user):
        return Response(status=403)
    return Response([{"id": c.id, "name": c.name, "role": c.role, "whatsapp": c.whatsapp, "note": c.note} for c in ContactAdmin.objects.all()])


@api_view(["GET", "POST"])
def admin_gateways(request):
    if not require_admin(request.user):
        return Response(status=403)
    if request.method == "POST":
        name = (request.data.get("name") or "").strip()
        account_name = (request.data.get("account_name") or "").strip()
        account_number = (request.data.get("account_number") or "").strip()
        if name not in ("JazzCash", "Easypaisa"):
            return Response({"detail": "Choose JazzCash or Easypaisa."}, status=400)
        if not account_number:
            return Response({"detail": "Enter the account number."}, status=400)
        gateway = None
        pk = request.data.get("id")
        if str(pk or "").isdigit():
            gateway = Gateway.objects.filter(pk=pk).first()
        if not gateway:
            key = name.lower()
            gateway = next((item for item in Gateway.objects.all() if item.name.lower().replace(" ", "") == key), None)
        if not gateway:
            gateway = Gateway(name=name)
        gateway.name = name
        gateway.account_name = account_name
        gateway.account_number = account_number
        gateway.active = True
        gateway.save()
    return Response([{"id": g.id, "name": g.name, "account_name": g.account_name, "account_number": g.account_number, "active": g.active} for g in Gateway.objects.all()])


@api_view(["GET", "POST"])
def admin_settings(request):
    if not require_admin(request.user):
        return Response(status=403)
    settings = SiteSetting.current()
    if request.method == "POST":
        settings.notice = request.data.get("notice") or settings.notice
        settings.site_name = request.data.get("site_name") or settings.site_name
        settings.save()
    return Response({"notice": settings.notice, "site_name": settings.site_name, "direct_percent": money(settings.direct_percent), "level2_percent": money(settings.level2_percent), "level3_percent": money(settings.level3_percent)})


@api_view(["GET"])
def admin_rewards(request):
    if not require_admin(request.user):
        return Response(status=403)
    return Response([{"id": r.id, "level": r.level, "title": r.title, "required_team": r.required_team, "prize": r.prize, "active": r.active} for r in Reward.objects.all()])


@api_view(["GET"])
def admin_levels(request):
    if not require_admin(request.user):
        return Response(status=403)
    settings = SiteSetting.current()
    return Response({"levels": [
        {"level": 1, "name": "Direct Referral Commission", "percent": money(settings.direct_percent)},
        {"level": 2, "name": "Level 2 – Indirect Referral Commission", "percent": money(settings.level2_percent)},
        {"level": 3, "name": "Level 3 – Indirect Referral Commission", "percent": money(settings.level3_percent)},
    ], "plans": [{"id": p.id, "name": p.name, "price": money(p.price)} for p in Plan.objects.all()]})
