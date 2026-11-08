from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from uuid import uuid4

from django.core.cache import cache
from django.db import IntegrityError, transaction
import re

from django.db.models import Case, Count, DecimalField, F, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Lower
from django.http import FileResponse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .levels import FLOWER_AMOUNT, LEVEL_JOININGS, SIGNUP_BONUS, cycle_status, level_for_joinings, next_level_target, task_rate
from .models import (
    Channel, ContactAdmin, Deposit, DepositEditHistory, DisplayBloom, Gateway, Plan, Reward, SiteSetting,
    Task, TaskClaim, TaskDone, Transaction, UpgradeRequest, User, Withdrawal,
)

LOGO = "/logo.png"
EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.IGNORECASE)


def valid_email(value):
    return bool(EMAIL_RE.match((value or "").strip()))


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


def guard_row(bucket):
    key = f"guard:{bucket}"
    now = timezone.now().timestamp()
    try:
        row = cache.get(key)
    except Exception:
        return now, None, key
    if not isinstance(row, dict) or row.get("until", 0) <= now:
        return now, None, key
    return now, row, key


def guard_blocked(bucket, limit):
    _now, row, _key = guard_row(bucket)
    return bool(row and row.get("count", 0) >= limit)


def guard_fail(bucket, limit, seconds):
    now, row, key = guard_row(bucket)
    try:
        if row is None:
            cache.set(key, {"count": 1, "until": now + seconds}, seconds)
            return 1 >= limit
        if row.get("count", 0) >= limit:
            return True
        row["count"] = int(row.get("count", 0)) + 1
        cache.set(key, row, max(1, int(row["until"] - now)))
        return row["count"] >= limit
    except Exception:
        return False


def guard_clear(bucket):
    try:
        cache.delete(f"guard:{bucket}")
    except Exception:
        return


def too_many(bucket, limit, seconds):
    if guard_blocked(bucket, limit):
        return True
    guard_fail(bucket, limit, seconds)
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
        "plan_price": money(user.plan.price) if user.plan_id else "0.00",
        "logo": LOGO,
    }


def require_admin(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or user.role != "admin":
        return False
    key = request.headers.get("X-Admin-Session") or ""
    if key:
        from .models import AdminSession
        row = AdminSession.objects.filter(key=key, user=user, revoked=False).first()
        if not row:
            return False
        row.save(update_fields=["last_seen"])
    return True


def banned_response(user):
    if not user or not user.is_authenticated:
        return None
    if user.account_status == "banned" or user.banned_at:
        return Response({"detail": "Your Account Is Banned", "reason": user.ban_reason or ""}, status=403)
    return None


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
    ident = (request.data.get("identity") or "").strip()
    password = request.data.get("password") or ""
    role = request.data.get("role") or "user"
    ip = client_ip(request)
    ip_key = f"signin-ip:{ip}"
    ident_key = f"signin:{ip}:{ident.lower()}"
    if guard_blocked(ip_key, 40) or (ident and guard_blocked(ident_key, 12)):
        return Response({"detail": "Too many sign-in attempts. Please wait 15 minutes and try again."}, status=429)
    user = User.objects.filter(email__iexact=ident).first() or User.objects.filter(username__iexact=ident).first() or User.objects.filter(phone=ident).first()
    if not user or not user.check_password(password) or user.role != role:
        guard_fail(ip_key, 40, 900)
        if ident:
            guard_fail(ident_key, 12, 900)
        return Response({"detail": "Email, username, or password is incorrect."}, status=400)
    guard_clear(ident_key)
    if user.account_status == "banned" or user.banned_at:
        return Response({"detail": "Your Account Is Banned", "reason": user.ban_reason or ""}, status=403)
    token, _ = Token.objects.get_or_create(user=user)
    payload = {"token": token.key, "user": user_payload(user)}
    if user.role == "admin":
        from .desk import open_session
        payload["session_key"] = open_session(request, user)
    return Response(payload)


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
    if not valid_email(email):
        return Response({"detail": "Enter a valid email address, for example name@gmail.com."}, status=400)
    if User.objects.filter(email__iexact=email).exists():
        return Response({"detail": "This email is already registered."}, status=400)
    parent = User.objects.filter(referral_code__iexact=code, role="user").first() if code else None
    if not parent:
        return Response({"detail": "A valid referral code is required."}, status=400)
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
@permission_classes([AllowAny])
def referrer(request):
    code = (request.GET.get("code") or "").strip()
    parent = User.objects.filter(referral_code__iexact=code, role="user").first() if code else None
    if not parent:
        return Response({"detail": "Referral code not found."}, status=404)
    return Response({"name": parent.display_name(), "code": parent.referral_code})


@api_view(["GET"])
def me(request):
    blocked = banned_response(request.user)
    if blocked:
        return blocked
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


def level_thresholds():
    thresholds = dict(LEVEL_JOININGS)
    for reward in Reward.objects.filter(level__gte=1, level__lte=10, required_team__gt=0):
        thresholds[reward.level] = reward.required_team
    return thresholds


def sync_level(user):
    if not user or getattr(user, "role", "") != "user":
        return 0
    count = approved_joinings(user).count()
    level = level_for_joinings(count, level_thresholds())
    if user.level != level:
        user.level = level
        user.save(update_fields=["level"])
    return count


def activate_member(user):
    if user.activated_at is None:
        user.activated_at = timezone.now()
        user.save(update_fields=["activated_at"])
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
    thresholds = level_thresholds()
    next_level, required = next_level_target(user.level, thresholds)
    if not user.plan_id:
        locked = True
        message = "Tasks are locked until your package payment is approved."
    elif clock["locked"]:
        locked = True
        message = f"Tasks are locked because no approved joining arrived in the last {clock['days']} days. When a new member's payment is approved, tasks unlock and the next cycle starts."
    else:
        locked = False
        if clock.get("kept_open"):
            message = "Tasks are unlocked. An approved joining is on your team, so tasks stay open until you reach Level 1."
        else:
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
        "remaining_joinings": None if required is None else max(0, required - count),
    }


def local_day_bounds():
    current = timezone.localtime()
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def day_window(day=None):
    if day is None:
        return local_day_bounds()
    start = timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())
    return start, start + timedelta(days=1)


def parse_day(raw):
    text = (raw or "").strip()
    if not text or text == "today":
        return timezone.localdate()
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return timezone.localdate()


def between(qs, field, day=None):
    start, end = day_window(day)
    return qs.filter(**{f"{field}__gte": start, f"{field}__lt": end})


def approved_deposits_on(day=None):
    """Joining plans approved on this local day, including older pending or rejected slips."""
    start, end = day_window(day)
    return Deposit.objects.filter(status=Deposit.APPROVED).filter(
        Q(approved_at__gte=start, approved_at__lt=end)
        | Q(approved_at__isnull=True, updated_at__gte=start, updated_at__lt=end)
    )


def approved_withdrawals_on(day=None):
    start, end = day_window(day)
    return Withdrawal.objects.filter(status=Withdrawal.APPROVED).filter(
        Q(updated_at__gte=start, updated_at__lt=end)
        | Q(updated_at__isnull=True, created_at__gte=start, created_at__lt=end)
    )


def growth_percent(current, previous):
    current = Decimal(current or 0)
    previous = Decimal(previous or 0)
    if previous == 0:
        return 0
    return int(((current - previous) / previous * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def user_guard(request):
    blocked = banned_response(request.user)
    if blocked:
        return blocked
    if getattr(request.user, "account_status", "") != "approved":
        return Response({"detail": "Your account is waiting for admin approval.", "hold": True}, status=403)
    return None


def submitted_payment(user):
    return user.deposits.filter(status=Deposit.PENDING).exclude(screenshot="").exists()


def latest_reject_reason(user):
    row = user.deposits.filter(status=Deposit.REJECTED).order_by("-updated_at", "-id").first()
    return (row.reject_reason if row else "") or ""


def account_phase(user):
    if user.account_status == "banned" or user.banned_at:
        return "banned"
    if user.account_status == "approved":
        if not user.channels_confirmed_at:
            return "join_channels"
        return "approved"
    submitted = submitted_payment(user)
    if user.account_status == "rejected" and not submitted:
        return "rejected"
    if not submitted:
        return "choose_plan"
    return "timer"


def review_remaining(user):
    deposit = user.deposits.filter(status=Deposit.PENDING).exclude(screenshot="").order_by("-id").first()
    if not deposit:
        return 0
    end = deposit.created_at + timedelta(hours=4)
    return max(0, int((end - timezone.now()).total_seconds()))


def official_channels():
    rows = []
    for channel in Channel.objects.filter(status=True).order_by("sort_order", "id")[:2]:
        rows.append({
            "id": channel.id,
            "title": channel.title,
            "purpose": channel.purpose,
            "description": channel.description,
            "link": channel.link,
            "image": media_url(channel.image),
        })
    return rows


def gate_payload(user):
    phase = account_phase(user)
    remaining = review_remaining(user) if phase == "timer" else 0
    show_channels = phase == "join_channels"
    return {
        "phase": phase,
        "account_status": user.account_status,
        "reject_reason": user.ban_reason or "" if phase == "banned" else latest_reject_reason(user),
        "reason": user.ban_reason or "" if phase == "banned" else "",
        "channels": official_channels() if show_channels else [],
        "confirmed": bool(user.channels_confirmed_at),
        "remaining_seconds": remaining,
        "timer_complete": phase == "timer" and remaining == 0,
    }


@api_view(["GET"])
def account_gate(request):
    user = request.user
    if user.account_status == "banned" or user.banned_at:
        return Response(gate_payload(user))
    return Response(gate_payload(user))


@api_view(["POST"])
def confirm_channels(request):
    blocked = banned_response(request.user)
    if blocked:
        return blocked
    if not request.data.get("joined"):
        return Response({"detail": "Confirm that you joined both official channels."}, status=400)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if user.account_status != "approved":
            return Response({"detail": "Official channels open after an admin approves your account."}, status=400)
        if user.channels_confirmed_at is None:
            user.channels_confirmed_at = timezone.now()
            user.save(update_fields=["channels_confirmed_at"])
    user.refresh_from_db()
    return Response(gate_payload(user))


@api_view(["POST"])
def confirm_daily_channels(request):
    blocked = user_guard(request)
    if blocked:
        return blocked
    if not request.data.get("joined"):
        return Response({"detail": "Confirm that you joined both official channels."}, status=400)
    user = request.user
    if not user.channels_confirmed_at:
        return Response({"detail": "Join the official channels after approval before opening the task room."}, status=400)
    user.channels_day = timezone.localdate()
    user.save(update_fields=["channels_day"])
    return Response({"ok": True, "channels_due": False})


@api_view(["GET"])
def user_home(request):
    blocked = user_guard(request)
    if blocked:
        return blocked
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
    ("Anemone", "Open the anemone and collect this flower's earning."),
    ("Aster", "Open the aster and collect this flower's earning."),
    ("Buttercup", "Open the buttercup and collect this flower's earning."),
    ("Cosmos", "Open the cosmos and collect this flower's earning."),
    ("Foxglove", "Open the foxglove and collect this flower's earning."),
    ("Geranium", "Open the geranium and collect this flower's earning."),
    ("Hollyhock", "Open the hollyhock and collect this flower's earning."),
    ("Hydrangea", "Open the hydrangea and collect this flower's earning."),
    ("Larkspur", "Open the larkspur and collect this flower's earning."),
    ("Morning Glory", "Open the morning glory and collect this flower's earning."),
    ("Petunia", "Open the petunia and collect this flower's earning."),
    ("Phlox", "Open the phlox and collect this flower's earning."),
    ("Sweet Pea", "Open the sweet pea and collect this flower's earning."),
    ("Yarrow", "Open the yarrow and collect this flower's earning."),
    ("Calendula", "Open the calendula and collect this flower's earning."),
    ("Chrysanthemum", "Open the chrysanthemum and collect this flower's earning."),
    ("Forget-Me-Not", "Open the forget-me-not and collect this flower's earning."),
    ("Gentian", "Open the gentian and collect this flower's earning."),
]


DISPLAY_FLOWERS = [
    ("Camellia", "hibiscus", "#e11d48"), ("Bluebell", "tulip", "#60a5fa"), ("Daffodil", "sun", "#f5b301"),
    ("Gardenia", "white", "#fff7fb"), ("Hibiscus", "hibiscus", "#ff4f9a"), ("Magnolia", "white", "#fde68a"),
    ("Poppy", "tulip", "#ef4444"), ("Violet", "hibiscus", "#a78bfa"), ("Zinnia", "sun", "#fb7185"),
    ("Azalea", "white", "#fda4af"), ("Begonia", "hibiscus", "#f43f5e"), ("Carnation", "tulip", "#fb7185"),
    ("Dahlia", "sun", "#f97316"), ("Freesia", "white", "#fef3c7"), ("Gerbera", "hibiscus", "#ec4899"),
    ("Heather", "tulip", "#c084fc"), ("Ixora", "sun", "#f59e0b"), ("Jonquil", "white", "#fff1f2"),
    ("Kalmia", "hibiscus", "#db2777"), ("Lilac", "tulip", "#d946ef"), ("Mimosa", "sun", "#facc15"),
    ("Narcissus", "white", "#ffe4e6"), ("Oleander", "hibiscus", "#be185d"), ("Primrose", "tulip", "#f9a8d4"),
    ("Quince", "sun", "#fdba74"), ("Ranunculus", "white", "#ffffff"), ("Snapdragon", "hibiscus", "#e879f9"),
    ("Tuberose", "tulip", "#86efac"), ("Verbena", "sun", "#fb923c"), ("Wisteria", "hibiscus", "#818cf8"),
]


def ensure_task_catalog():
    for title, detail in TASK_CATALOG:
        task, created = Task.objects.get_or_create(title=title, defaults={"detail": detail, "reward": 0, "active": True, "display_only": False})
        if not created and not task.display_only and (task.detail != detail or not task.active):
            task.detail = detail
            task.active = True
            task.save(update_fields=["detail", "active"])
    Task.objects.filter(title="Daily task").update(active=False)
    return list(Task.objects.filter(active=True, display_only=False).order_by("id"))


def ensure_display_flowers():
    rows = []
    for title, kind, tone in DISPLAY_FLOWERS:
        task, created = Task.objects.get_or_create(
            title=title,
            defaults={"detail": "A garden flower you can bloom. It does not pay a reward.", "reward": 0, "active": True, "display_only": True},
        )
        if created or task.display_only:
            rows.append((task, kind, tone))
    return rows


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
    blocked = user_guard(request)
    if blocked:
        return blocked
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
            "display_only": False,
        })
    channels_due = user.channels_day != timezone.localdate()
    bloomed = set(DisplayBloom.objects.filter(user=user, day=timezone.localdate()).values_list("task_id", flat=True))
    extras = []
    for offset, (task, kind, tone) in enumerate(ensure_display_flowers()):
        extras.append({
            "id": task.id,
            "slot": 100 + offset,
            "title": task.title,
            "detail": task.detail,
            "reward": "0.00",
            "done": task.id in bloomed,
            "display_only": True,
            "kind": kind,
            "tone": tone,
        })
    return Response({
        "tasks": rows,
        "extras": extras,
        "done_today": done_today,
        "progress": progress,
        "channels_due": channels_due,
        "channels": official_channels() if channels_due else [],
    })


@api_view(["POST"])
def complete_task(request, pk):
    blocked = user_guard(request)
    if blocked:
        return blocked
    task = Task.objects.filter(pk=pk, active=True).first()
    if not task:
        return Response({"detail": "Task not found."}, status=404)
    if task.display_only:
        DisplayBloom.objects.get_or_create(user=request.user, task=task, day=timezone.localdate())
        return Response({
            "balance": money(request.user.balance),
            "reward": "0.00",
            "flower": "0.00",
            "bonus": "0.00",
            "display_only": True,
        })
    reward = Decimal("0")
    bonus = Decimal("0")
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
            user.flower_balance += FLOWER_AMOUNT
            bonus = Decimal("0")
            first_task = TaskDone.objects.filter(user=user).count() == 1
            already_bonus = Transaction.objects.filter(user=user, remark="signup_bonus").exists()
            if first_task and not already_bonus:
                bonus = SIGNUP_BONUS
                user.balance += bonus
            user.save(update_fields=["balance", "flower_balance"])
            Transaction.objects.create(user=user, amount=reward, trx_type="+", title="Task Earning", remark="ads", plan_name=user.plan.name if user.plan else "")
            Transaction.objects.create(user=user, amount=FLOWER_AMOUNT, trx_type="+", title="Flower Earning", remark="flower", plan_name=task.title)
            if bonus:
                Transaction.objects.create(
                    user=user, amount=bonus, trx_type="+", title="Bonus", remark="signup_bonus",
                    plan_name="Account creation bonus",
                )
    except IntegrityError:
        return Response({"detail": "This flower is already open today."}, status=400)
    return Response({
        "balance": money(user.balance),
        "reward": money(reward),
        "flower": money(FLOWER_AMOUNT),
        "bonus": money(bonus),
        "display_only": False,
    })


def tx_for_user(tx):
    data = tx_public(tx, include_email=tx.level == 1)
    if tx.remark in ("balance_add", "signup_bonus"):
        data["title"] = "Bonus"
        data["plan_name"] = ""
        data.pop("source_email", None)
    return data


@api_view(["GET"])
def transactions(request):
    blocked = user_guard(request)
    if blocked:
        return blocked
    qs = request.user.transactions.exclude(remark__in=["flower", "flower_add", "flower_sub", "balance_sub", "balance_subtract"])
    kind = request.GET.get("filter") or ""
    if kind in ("balance_sub", "balance_subtract", "manual_sub"):
        qs = qs.none()
    elif kind == "ads":
        qs = qs.filter(remark="ads")
    elif kind == "direct":
        qs = qs.filter(remark="referral", level=1)
    elif kind == "level2":
        qs = qs.filter(remark="referral", level=2)
    elif kind == "level3":
        qs = qs.filter(remark="referral", level=3)
    elif kind == "indirect":
        qs = qs.filter(remark="referral", level__in=[2, 3])
    elif kind in ("upgrade_referral", "plan_upgrade_referral"):
        qs = qs.filter(remark="upgrade_referral", trx_type="+")
    elif kind == "withdraw":
        qs = qs.filter(remark="withdraw")
    elif kind == "bonus":
        qs = qs.filter(remark__in=["balance_add", "signup_bonus"], trx_type="+")
    return Response([tx_for_user(tx) for tx in qs[:100]])


@api_view(["GET"])
def team(request):
    blocked = user_guard(request)
    if blocked:
        return blocked
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
    blocked = user_guard(request)
    if blocked:
        return blocked
    user = request.user
    count = sync_level(user)
    thresholds = level_thresholds()
    items = []
    for step, required in sorted(thresholds.items()):
        items.append({
            "id": step,
            "level": step,
            "title": f"Level {step}",
            "required_team": required,
            "prize": f"Rs {task_rate(step)} per task",
            "reached": count >= required,
        })
    next_level, required = next_level_target(user.level, thresholds)
    return Response({
        "active_team": count,
        "level": user.level,
        "task_rate": money(task_rate(user.level)),
        "next_level": next_level,
        "next_required": required,
        "remaining_joinings": None if required is None else max(0, required - count),
        "items": items,
    })


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


def mask_account(value):
    raw = "".join(ch for ch in (value or "") if ch.isalnum())
    if len(raw) <= 4:
        return "••••"
    return f"••••{raw[-4:]}"


def withdrawal_rule(user):
    approved = user.withdrawals.filter(status=Withdrawal.APPROVED).exists()
    minimum = Decimal("500.00") if approved else Decimal("50.00")
    message = "After your first approved withdrawal, the minimum is Rs 500." if approved else "Your first withdrawal must be at least Rs 50."
    flower_available = user.flower_balance if user.level >= 8 else Decimal("0")
    return minimum, message, user.balance + flower_available


@api_view(["GET", "POST"])
def withdrawals(request):
    blocked = user_guard(request)
    if blocked:
        return blocked
    if request.method == "POST":
        try:
            amount = Decimal(str(request.data.get("amount") or "0"))
        except (InvalidOperation, ValueError):
            return Response({"detail": "Enter a valid amount."}, status=400)
        method = (request.data.get("method") or "").strip()
        account_name = (request.data.get("account_name") or "").strip()
        account = (request.data.get("account") or "").strip()
        if amount <= 0 or not method or not account_name or not account:
            return Response({"detail": "Enter an amount above zero, plus the method, account name, and account number."}, status=400)
        wallet = other_wallet_label(method)
        method = wallet or canonical_gateway_name(method)
        if not wallet and method not in withdrawal_methods():
            return Response({"detail": "That payment method is not available."}, status=400)
        amount = amount.quantize(Decimal("0.01"))
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            sync_level(user)
            minimum, message, withdrawable = withdrawal_rule(user)
            if amount < minimum:
                return Response({"detail": message}, status=400)
            if amount > withdrawable:
                return Response({"detail": "That amount is higher than the balance you can withdraw."}, status=400)
            flower_part = amount - user.balance if amount > user.balance else Decimal("0")
            user.balance -= amount - flower_part
            user.flower_balance -= flower_part
            user.save(update_fields=["balance", "flower_balance"])
            Withdrawal.objects.create(
                user=user, amount=amount, method=method, account_name=account_name,
                account=account, flower_part=flower_part,
            )
        return Response({"balance": money(user.balance)})
    sync_level(request.user)
    minimum, message, withdrawable = withdrawal_rule(request.user)
    rows = []
    for item in request.user.withdrawals.order_by("-id"):
        rows.append({
            "id": item.id,
            "amount": money(item.amount),
            "method": item.method,
            "account_mask": mask_account(item.account),
            "status": item.status,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "approved_at": item.updated_at.isoformat() if item.status == Withdrawal.APPROVED and item.updated_at else None,
        })
    return Response({"rows": rows, "minimum": money(minimum), "minimum_message": message, "withdrawable": money(withdrawable), "methods": withdrawal_methods(), "other_wallets": list(OTHER_WALLETS)})


@api_view(["GET"])
def contacts(request):
    blocked = user_guard(request)
    if blocked:
        return blocked
    rows = []
    for contact in ContactAdmin.objects.all():
        legacy_botim = contact.number_type == "botim" and not contact.botim
        rows.append({
            "id": contact.id,
            "name": contact.name,
            "role": contact.role,
            "whatsapp": "" if legacy_botim else contact.whatsapp,
            "botim": contact.botim or (contact.whatsapp if legacy_botim else ""),
            "note": contact.note,
            "purpose": contact.purpose,
            "number_type": contact.number_type or "whatsapp",
            "whatsapp_icon": media_url(contact.whatsapp_icon),
            "botim_icon": media_url(contact.botim_icon),
        })
    return Response(rows)


def canonical_gateway_name(name):
    raw = " ".join((name or "").split())
    key = raw.lower().replace(" ", "")
    if key == "jazzcash":
        return "JazzCash"
    if key == "easypaisa":
        return "Easypaisa"
    if not raw or len(raw) > 80:
        return ""
    return raw


OTHER_WALLETS = ("UPaisa", "SadaPay", "NayaPay")


def wallet_key(name):
    return "".join((name or "").lower().split())


def is_other_wallet(name):
    return wallet_key(name) in {"upaisa", "sadapay", "nayapay"}


def other_wallet_label(name):
    return {"upaisa": "UPaisa", "sadapay": "SadaPay", "nayapay": "NayaPay"}.get(wallet_key(name), "")


def withdrawal_methods():
    names = []
    for gateway in payment_gateways(require_number=False):
        label = canonical_gateway_name(gateway.name)
        if label and not is_other_wallet(label) and label not in names:
            names.append(label)
    return names


def payment_gateways(active_only=True, require_number=True):
    rows = []
    for gateway in Gateway.objects.all().order_by("id"):
        if not canonical_gateway_name(gateway.name):
            continue
        if active_only and not gateway.active:
            continue
        if require_number and not (gateway.account_number or "").strip():
            continue
        rows.append(gateway)
    return rows


def gateway_public(gateway):
    return {
        "id": gateway.id,
        "name": canonical_gateway_name(gateway.name),
        "account_name": gateway.account_name,
        "account_number": gateway.account_number,
        "instructions": gateway.instructions or "",
        "image": media_url(gateway.image),
        "active": gateway.active,
    }


def plan_upgrade_locked(user):
    if not user or not getattr(user, "plan_id", None):
        return False
    sync_level(user)
    return user.level >= 3


@api_view(["GET"])
def plans(request):
    user = request.user
    pending = user.deposits.filter(status=Deposit.PENDING).select_related("plan").order_by("-id").first()
    upgrade = UpgradeRequest.objects.filter(user=user, status=UpgradeRequest.PENDING).select_related("to_plan").order_by("-id").first()
    locked = plan_upgrade_locked(user)
    return Response({
        "current_plan_id": user.plan_id,
        "current_plan": user.plan.name if user.plan else None,
        "upgrade_locked": locked,
        "level": user.level,
        "plans": [{"id": p.id, "name": p.name, "price": money(p.price), "daily_tasks": p.daily_tasks, "color": p.color, "text_color": p.text_color} for p in Plan.objects.all()],
        "gateways": [gateway_public(gateway) for gateway in payment_gateways()],
        "current_price": money(user.plan.price) if user.plan_id else "0.00",
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
    if user.plan_id and plan_upgrade_locked(user):
        return Response({"detail": "Plan upgrade is locked because Level 3 is already unlocked. You can upgrade only before Level 3."}, status=400)
    sender = (request.data.get("sender") or "").strip()
    sender_name = (request.data.get("sender_name") or "").strip()[:120]
    trx_no = (request.data.get("trx_no") or "").strip()[:80]
    method = (request.data.get("method") or "").strip()
    if not sender or not method:
        return Response({"detail": "Choose JazzCash or Easypaisa and enter the sender number."}, status=400)
    if not sender_name:
        return Response({"detail": "Enter the sender name."}, status=400)
    shot = image_upload(request.FILES.get("screenshot"))
    if not shot:
        return Response({"detail": "Payment screenshot must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
    accounts = payment_gateways()
    gateway_id = request.data.get("gateway_id")
    account = None
    if str(gateway_id or "").isdigit():
        account = next((item for item in accounts if item.id == int(gateway_id)), None)
    if account is None:
        number = method.split("·")[-1].strip()
        account = next((item for item in accounts if item.account_number == number or item.account_number == method), None)
    if account is None:
        key = canonical_gateway_name(method.split("·")[0])
        named = [item for item in accounts if canonical_gateway_name(item.name) == key]
        if len(named) == 1:
            account = named[0]
    if account is None:
        return Response({"detail": "That payment account is not available."}, status=400)
    method = f"{canonical_gateway_name(account.name)} · {account.account_number}"
    if user.plan_id:
        if plan.id == user.plan_id:
            return Response({"detail": "This is already your current plan."}, status=400)
        if user.plan and plan.price <= user.plan.price:
            return Response({"detail": "Choose a higher plan to upgrade."}, status=400)
        extra = (plan.price - user.plan.price).quantize(Decimal("0.01"))
        waiting = UpgradeRequest.objects.filter(user=user, status=UpgradeRequest.PENDING).order_by("-id").first()
        if waiting and waiting.screenshot:
            return Response({"detail": "An upgrade request is already waiting for admin approval."}, status=400)
        if waiting:
            waiting.from_plan = user.plan
            waiting.to_plan = plan
            waiting.amount = extra
            waiting.method = method
            waiting.sender = sender
            waiting.sender_name = sender_name
            waiting.trx_no = trx_no
            waiting.screenshot = shot
            waiting.save()
            return Response({"kind": "upgrade", "status": "pending"})
        UpgradeRequest.objects.create(
            user=user, from_plan=user.plan, to_plan=plan, amount=extra,
            method=method, sender=sender, sender_name=sender_name, trx_no=trx_no, screenshot=shot,
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
        pending.sender_name = sender_name
        pending.trx_no = trx_no
        pending.screenshot = shot
        pending.save()
        return Response({"id": pending.id, "kind": "deposit", "status": pending.status})
    deposit = Deposit.objects.create(
        user=user, plan=plan, amount=plan.price, method=method, sender=sender,
        sender_name=sender_name, trx_no=trx_no, screenshot=shot, status=Deposit.PENDING,
    )
    if user.account_status != "approved":
        user.account_status = "pending"
        user.save(update_fields=["account_status"])
    return Response({"id": deposit.id, "kind": "deposit", "status": deposit.status})


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def upgrade_create(request):
    blocked = user_guard(request)
    if blocked:
        return blocked
    if plan_upgrade_locked(request.user):
        return Response({"detail": "Plan upgrade is locked because Level 3 is already unlocked. You can upgrade only before Level 3."}, status=400)
    plan_id = request.data.get("plan_id")
    if not str(plan_id or "").isdigit():
        return Response({"detail": "Choose a plan."}, status=400)
    plan = Plan.objects.filter(pk=plan_id).first()
    if not plan:
        return Response({"detail": "Choose a plan."}, status=400)
    shot = image_upload(request.FILES.get("screenshot"))
    if not shot:
        return Response({"detail": "Payment screenshot must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
    current = request.user.plan.price if request.user.plan_id else Decimal("0")
    extra = (plan.price - current).quantize(Decimal("0.01"))
    if extra <= 0:
        return Response({"detail": "Choose a higher plan to upgrade."}, status=400)
    UpgradeRequest.objects.create(
        user=request.user, from_plan=request.user.plan, to_plan=plan, amount=extra,
        method=(request.data.get("method") or "").strip(),
        sender=(request.data.get("sender") or "").strip(),
        sender_name=(request.data.get("sender_name") or "").strip()[:120],
        trx_no=(request.data.get("trx_no") or "").strip()[:80],
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


def task_earnings():
    # Completed task credits only. Plan payments, referrals, flower, and restorations stay out.
    return Transaction.objects.filter(remark="ads", trx_type="+")


def manual_additions():
    return Transaction.objects.filter(remark="balance_add", trx_type="+")


def manual_subtractions():
    return Transaction.objects.filter(remark__in=["balance_sub", "balance_subtract"], trx_type="-")


def referral_commissions():
    return Transaction.objects.filter(remark="referral", trx_type="+")


@api_view(["GET"])
def admin_summary(request):
    if not require_admin(request):
        return Response({"detail": "Admin only."}, status=403)
    approved_deposits = Deposit.objects.filter(status=Deposit.APPROVED)
    approved_withdrawals = Withdrawal.objects.filter(status=Withdrawal.APPROVED)
    approved_today = approved_deposits_on()
    paid_today = approved_withdrawals_on()
    task_earn = task_earnings()
    manual_add = manual_additions()
    manual_sub = manual_subtractions()
    referral = referral_commissions()
    week_start, _end = day_window()
    week_start = week_start - timedelta(days=7)
    users = User.objects.filter(role="user")
    users_then = users.filter(date_joined__lt=week_start).count()
    deposited_then = _sum(approved_deposits.filter(
        Q(approved_at__lt=week_start) | Q(approved_at__isnull=True, updated_at__lt=week_start)
    ))
    deposited_now = _sum(approved_deposits)
    users_now = users.count()
    return Response({
        "users": users_now,
        "pending_deposits": Deposit.objects.filter(status=Deposit.PENDING).count(),
        "pending_withdrawals": Withdrawal.objects.filter(status=Withdrawal.PENDING).count(),
        "approved_today": approved_today.count(),
        "withdraw_today": paid_today.count(),
        "pending_upgrades": UpgradeRequest.objects.filter(status=UpgradeRequest.PENDING).count(),
        "rejected_users": User.objects.filter(role="user", account_status="rejected").count(),
        "users_week_percent": growth_percent(users_now, users_then),
        "deposited_week_percent": growth_percent(deposited_now, deposited_then),
        "money": {
            "referral": money(_sum(referral)),
            "referral_today": money(_sum(between(referral, "created_at"))),
            "deposited": money(deposited_now),
            "approved_users_today": approved_today.count(),
            "withdrawals_today": money(_sum(paid_today)),
            "withdraw_today_count": paid_today.count(),
            "received_today": money(_sum(approved_today)),
            "pending_withdraw_amount": money(_sum(Withdrawal.objects.filter(status=Withdrawal.PENDING))),
            "withdrawn": money(_sum(approved_withdrawals)),
            "task": money(_sum(task_earn)),
            "task_today": money(_sum(between(task_earn, "created_at"))),
            "manual_add": money(_sum(manual_add)),
            "manual_add_today": money(_sum(between(manual_add, "created_at"))),
            "manual_sub": money(_sum(manual_sub)),
            "manual_sub_today": money(_sum(between(manual_sub, "created_at"))),
            "balances": money(_sum(User.objects.filter(role="user"), "balance")),
        },
    })


def _sum(qs, field="amount"):
    return qs.aggregate(total=Sum(field))["total"] or Decimal("0")


def _period(qs, start, end, field="created_at"):
    if start:
        qs = qs.filter(**{f"{field}__date__gte": start})
    if end:
        qs = qs.filter(**{f"{field}__date__lte": end})
    return qs


def analytics_pack(start="", end=""):
    earnings = Transaction.objects.filter(trx_type="+", remark__in=["ads", "referral", "restore20", "upgrade_referral"])
    manual = Transaction.objects.filter(remark="balance_add")
    paid = Withdrawal.objects.filter(status=Withdrawal.APPROVED)
    tasks = TaskDone.objects.all()
    users = User.objects.filter(role="user", account_status="approved")
    return {
        "earnings": money(_sum(_period(earnings, start, end))),
        "manual_added": money(_sum(_period(manual, start, end))),
        "withdrawals": money(_sum(_period(paid, start, end, "updated_at"))),
        "tasks": _period(tasks, start, end).count(),
        "approved_users": _period(users, start, end, "activated_at").count() if (start or end) else users.count(),
        "wallet_balance": money(_sum(User.objects.filter(role="user"), "balance")),
    }


@api_view(["GET"])
def admin_analytics(request):
    if not require_admin(request):
        return Response(status=403)
    start = request.GET.get("start") or ""
    end = request.GET.get("end") or ""
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
        "overall": analytics_pack(),
        "period": analytics_pack(start, end) if (start or end) else None,
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
    qs = user.team.select_related("plan")
    members = []
    for member in qs.order_by("-id")[:40]:
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
        "total": qs.count(),
        "active": qs.filter(plan__isnull=False, account_status="approved").count(),
        "pending": qs.filter(account_status="pending").count(),
        "level2": User.objects.filter(referred_by__referred_by=user, role="user").count(),
        "level3": User.objects.filter(referred_by__referred_by__referred_by=user, role="user").count(),
        "shown": len(members),
        "members": members,
    }


def looks_like_email(raw):
    text = (raw or "").strip()
    return "@" in text and " " not in text and len(text) <= 180


def account_search(qs, raw, email_field="email"):
    text = (raw or "").strip()
    if not text:
        return qs
    if looks_like_email(text):
        return qs.filter(**{f"{email_field}__iexact": text})
    return qs.filter(
        Q(**{f"{email_field}__istartswith": text})
        | Q(full_name__istartswith=text)
        | Q(username__istartswith=text)
        | Q(phone__startswith=text)
    )


def related_search(qs, raw, extras=()):
    text = (raw or "").strip()
    if not text:
        return qs
    if looks_like_email(text):
        return qs.filter(user__email__iexact=text)
    cond = (
        Q(user__email__istartswith=text)
        | Q(user__full_name__istartswith=text)
        | Q(user__username__istartswith=text)
        | Q(user__phone__startswith=text)
    )
    if any(ch.isdigit() for ch in text):
        for field in extras:
            cond |= Q(**{f"{field}__icontains": text})
    return qs.filter(cond)


def page_bounds(request, default=40, cap=60):
    try:
        page = max(1, int(request.GET.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.GET.get("limit") or default)
    except (TypeError, ValueError):
        size = default
    return page, max(1, min(size, cap))


def slice_page(qs, page, size):
    count = qs.count()
    pages = max(1, (count + size - 1) // size) if count else 1
    page = min(page, pages)
    start = (page - 1) * size
    return qs[start:start + size], count, page, pages


def paged(rows, count, page, pages):
    return {"rows": rows, "count": count, "page": page, "pages": pages}


def apply_day_range(qs, request, field="created_at"):
    raw = (request.GET.get("date") or "").strip()
    if raw:
        chosen = parse_day(raw)
        return between(qs, field, chosen) if chosen else qs
    start = (request.GET.get("start") or "").strip()
    end = (request.GET.get("end") or "").strip()
    if start:
        day = parse_day(start)
        if day:
            qs = qs.filter(**{f"{field}__date__gte": day})
    if end:
        day = parse_day(end)
        if day:
            qs = qs.filter(**{f"{field}__date__lte": day})
    return qs


def source_ids(emails):
    cleaned = sorted({(email or "").strip().lower() for email in emails if email and "@" in email})
    if not cleaned:
        return {}
    found = User.objects.annotate(mail=Lower("email")).filter(mail__in=cleaned).values_list("mail", "id")
    return {mail: pk for mail, pk in found}


def payment_shots(user_ids):
    best = {}
    ids = [pk for pk in user_ids if pk]
    if not ids:
        return best
    for row in Deposit.objects.filter(user_id__in=ids).exclude(screenshot="").only("id", "user_id", "screenshot", "created_at"):
        prev = best.get(row.user_id)
        if not prev or (row.created_at and row.created_at >= prev[0]):
            best[row.user_id] = (row.created_at, slip_url("deposits", row.id, row.screenshot))
    for row in UpgradeRequest.objects.filter(user_id__in=ids).exclude(screenshot="").only("id", "user_id", "screenshot", "created_at"):
        prev = best.get(row.user_id)
        if not prev or (row.created_at and row.created_at >= prev[0]):
            best[row.user_id] = (row.created_at, slip_url("upgrades", row.id, row.screenshot))
    return {key: value[1] for key, value in best.items()}


def team_card(user):
    return {
        "id": user.id,
        "full_name": user.display_name(),
        "email": user.email,
        "phone": user.phone or "",
        "username": user.username,
        "plan": user.plan.name if user.plan_id else None,
        "plan_color": user.plan.color if user.plan_id else "#fff3cd",
        "plan_text": user.plan.text_color if user.plan_id else "#856404",
        "paid": bool(user.plan_id),
        "balance": money(user.balance),
        "level": user.level,
        "status": user.account_status,
        "date": (user.activated_at or user.date_joined).isoformat(),
        "joined": user.date_joined.isoformat(),
        "approved_at": user.activated_at.isoformat() if user.activated_at else None,
    }


def member_row(user, light=False):
    when = user.date_joined
    if not light:
        deposit = user.deposits.order_by("-id").first()
        if deposit and deposit.status == Deposit.APPROVED:
            when = deposit.updated_at
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
        "active_team": 0 if light else user.team.filter(plan__isnull=False).count(),
        "earnings": "0.00" if light else money(user.transactions.filter(trx_type="+").aggregate(s=Sum("amount"))["s"] or 0),
        "status": user.account_status,
        "date": when.isoformat() if when else None,
        "phone": user.phone,
        "banned": user.account_status == "banned" or bool(user.banned_at),
    }


@api_view(["GET"])
def admin_users(request):
    if not require_admin(request):
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
    qs = account_search(qs, request.GET.get("q"))
    qs = qs.order_by("-id")
    page, size = page_bounds(request, 40, 40)
    if request.GET.get("suggest") == "1":
        size = min(size, 8)
        return Response([
            {"id": u.id, "full_name": u.display_name(), "email": u.email, "phone": u.phone or "", "status": u.account_status}
            for u in qs[:size]
        ])
    chunk, count, page, pages = slice_page(qs, page, size)
    return Response(paged([member_row(u, light=True) for u in chunk], count, page, pages))


def deposit_slip_row(deposit):
    return {
        "id": deposit.id,
        "plan": deposit.plan.name if deposit.plan_id else None,
        "amount": money(deposit.amount),
        "method": deposit.method,
        "sender": deposit.sender,
        "sender_name": deposit.sender_name or "",
        "trx_no": deposit.trx_no or "",
        "status": deposit.status,
        "reject_reason": deposit.reject_reason or "",
        "created_at": deposit.created_at.isoformat(),
        "screenshot": slip_url("deposits", deposit.id, deposit.screenshot),
    }


def account_approved_stamp(user):
    stamp = (
        user.deposits.filter(status=Deposit.APPROVED)
        .exclude(approved_at__isnull=True)
        .order_by("approved_at")
        .values_list("approved_at", flat=True)
        .first()
    )
    if stamp:
        return stamp
    joined = user.date_joined
    if user.activated_at and joined and user.activated_at > joined:
        return user.activated_at
    return None


@api_view(["GET"])
def admin_user_detail(request, pk):
    if not require_admin(request):
        return Response(status=403)
    user = User.objects.select_related("plan", "referred_by", "referred_by__plan").filter(pk=pk, role="user").first()
    if not user:
        return Response(status=404)
    team_qs = user.team.select_related("plan")
    paid_team = team_qs.filter(plan__isnull=False).exclude(account_status="banned").count()
    unpaid_team = team_qs.filter(plan__isnull=True).count()
    members = list(team_qs.order_by("-id")[:6])
    preview = [team_card(m) for m in members]
    shots = payment_shots([user.id] + [m.id for m in members])
    for card in preview:
        card["screenshot"] = shots.get(card["id"])
    slips = [deposit_slip_row(deposit) for deposit in user.deposits.select_related("plan").order_by("-id")[:20]]
    banned = user.account_status == "banned" or bool(user.banned_at)
    approved_stamp = account_approved_stamp(user)
    return Response({
        "user": member_row(user, light=True),
        "profile": {
            "balance": money(user.balance),
            "deposits": money(_sum(user.deposits.filter(status=Deposit.APPROVED))),
            "withdrawals": money(_sum(user.withdrawals.filter(status=Withdrawal.APPROVED))),
            "transactions": user.transactions.count(),
            "team": paid_team + unpaid_team,
            "paid_team": paid_team,
            "unpaid_team": unpaid_team,
            "approved_team": paid_team,
            "level": user.level,
            "phone": user.phone or "",
            "email": user.email,
            "full_name": user.display_name(),
            "referral_code": user.referral_code,
            "flower_balance": money(user.flower_balance),
            "status": "banned" if banned else user.account_status,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "ban_reason": user.ban_reason or "",
            "banned": banned,
            "plan": user.plan.name if user.plan else "",
            "plan_amount": money(user.plan.price) if user.plan_id else money(_sum(user.deposits.filter(status=Deposit.APPROVED))),
            "payment_screenshot": shots.get(user.id),
            "approved_at": approved_stamp.isoformat() if approved_stamp else None,
        },
        "upliner": person_brief(user.referred_by),
        "team": preview,
        "deposits": slips,
        "flower_rows": [tx_public(tx) for tx in user.transactions.filter(remark__in=["flower", "flower_add", "flower_sub"]).order_by("-id")[:8]],
    })


def user_tx_category(qs, category):
    if category in ("", "all"):
        return qs
    if category == "overall":
        return qs.filter(trx_type="+", remark__in=["ads", "referral", "flower", "flower_add", "restore20", "upgrade_referral"])
    if category == "ads_earn":
        return qs.filter(remark="ads", trx_type="+", title__icontains="ads")
    if category == "task":
        return qs.filter(remark="ads", trx_type="+").exclude(title__icontains="ads")
    if category == "flower":
        return qs.filter(remark__in=["flower", "flower_add", "flower_sub"])
    if category in ("balance_add", "manual_add"):
        return qs.filter(remark="balance_add", trx_type="+")
    if category == "bonus":
        return qs.filter(remark__in=["balance_add", "signup_bonus"], trx_type="+")
    if category in ("upgrade_referral", "plan_upgrade_referral"):
        return qs.filter(remark="upgrade_referral", trx_type="+")
    if category in ("balance_sub", "manual_sub"):
        return qs.filter(remark__in=["balance_sub", "balance_subtract"], trx_type="-")
    if category in ("direct", "direct_traffic"):
        return qs.filter(remark="referral", level=1)
    if category in ("indirect_1", "level_2"):
        return qs.filter(remark="referral", level=2)
    if category in ("indirect_2", "level_3"):
        return qs.filter(remark="referral", level=3)
    if category == "referral":
        return qs.filter(remark="referral", trx_type="+")
    if category == "withdraw":
        return qs.filter(remark="withdraw")
    if category == "ads":
        return qs.filter(remark="ads", trx_type="+")
    return qs


def tx_page(qs, request):
    q = (request.GET.get("q") or "").strip()
    if q:
        if looks_like_email(q):
            qs = qs.filter(Q(user__email__iexact=q) | Q(source_email__iexact=q))
        else:
            qs = qs.filter(Q(user__email__istartswith=q) | Q(user__full_name__istartswith=q) | Q(title__icontains=q) | Q(source_email__istartswith=q))
    qs = apply_day_range(qs, request, "created_at")
    page, size = page_bounds(request, 40, 80)
    total = money(_sum(qs))
    count = qs.count()
    pages = max(1, (count + size - 1) // size) if count else 1
    page = min(page, pages)
    start = (page - 1) * size
    chosen = parse_day((request.GET.get("date") or "").strip()) if (request.GET.get("date") or "").strip() else None
    chunk = list(qs.select_related("user")[start:start + size])
    found = source_ids([tx.source_email for tx in chunk])
    rows = []
    for tx in chunk:
        item = tx_public(tx, include_email=True)
        item["user_id"] = tx.user_id
        item["user_name"] = tx.user.display_name()
        item["user_email"] = tx.user.email
        item["source_user_id"] = found.get((tx.source_email or "").strip().lower())
        if tx.remark in ("balance_add", "signup_bonus"):
            item["purpose"] = tx.plan_name or (tx.title.split(":", 1)[1].strip() if ":" in tx.title else "")
        rows.append(item)
    return {
        "total": total,
        "count": count,
        "page": page,
        "pages": pages,
        "date": chosen.isoformat() if chosen else "",
        "rows": rows,
    }


@api_view(["GET"])
def admin_user_transactions(request, pk):
    if not require_admin(request):
        return Response(status=403)
    if not User.objects.filter(pk=pk, role="user").exists():
        return Response(status=404)
    qs = user_tx_category(Transaction.objects.filter(user_id=pk).select_related("user"), request.GET.get("category") or "")
    return Response(tx_page(qs.order_by("-id"), request))


@api_view(["GET"])
def admin_user_withdrawals(request, pk):
    if not require_admin(request):
        return Response(status=403)
    user = User.objects.filter(pk=pk, role="user").first()
    if not user:
        return Response(status=404)
    qs = user.withdrawals.order_by("-id")
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(account__icontains=q) | Q(account_name__icontains=q) | Q(method__icontains=q))
    qs = apply_day_range(qs, request, "created_at")
    page, size = page_bounds(request, 40, 80)
    chunk, count, page, pages = slice_page(qs, page, size)
    rows = [{
        "id": item.id,
        "user_id": user.id,
        "email": user.email,
        "amount": money(item.amount),
        "method": item.method,
        "account_name": item.account_name,
        "account": item.account,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    } for item in chunk]
    return Response(paged(rows, count, page, pages))


@api_view(["GET"])
def admin_user_team(request, pk):
    if not require_admin(request):
        return Response(status=403)
    user = User.objects.filter(pk=pk, role="user").first()
    if not user:
        return Response(status=404)
    qs = user.team.select_related("plan")
    paid = (request.GET.get("paid") or "").strip()
    if paid == "1":
        qs = qs.filter(plan__isnull=False).exclude(account_status="banned")
    elif paid == "0":
        qs = qs.filter(plan__isnull=True)
    plan = (request.GET.get("plan") or "").strip()
    if plan == "none":
        qs = qs.filter(plan__isnull=True)
    elif plan:
        qs = qs.filter(plan__name__iexact=plan)
    qs = account_search(qs, request.GET.get("q"))
    approved = (request.GET.get("approved") or "").strip()
    rejected = (request.GET.get("rejected") or "").strip()
    if approved:
        day = parse_day(approved)
        if day:
            qs = qs.filter(activated_at__date=day)
    if rejected:
        day = parse_day(rejected)
        if day:
            rejected_ids = Deposit.objects.filter(status=Deposit.REJECTED, updated_at__date=day, user__referred_by=user).values("user_id")
            qs = qs.filter(id__in=rejected_ids)
    counts = []
    for row in user.team.values("plan__name").annotate(count=Count("id")).order_by("plan__name"):
        counts.append({"name": row["plan__name"] or "No plan", "count": row["count"]})
    page, size = page_bounds(request, 40, 80)
    chunk, count, page, pages = slice_page(qs.order_by("-id"), page, size)
    cards = [team_card(m) for m in chunk]
    shots = payment_shots([card["id"] for card in cards])
    for card in cards:
        card["screenshot"] = shots.get(card["id"])
    payload = paged(cards, count, page, pages)
    payload["plans"] = counts
    payload["paid"] = user.team.filter(plan__isnull=False).exclude(account_status="banned").count()
    payload["unpaid"] = user.team.filter(plan__isnull=True).count()
    payload["total"] = payload["paid"] + payload["unpaid"]
    return Response(payload)


@api_view(["GET"])
def leaderboard(request):
    if not require_admin(request):
        return Response(status=403)
    level_counts = {step: 0 for step in range(1, 11)}
    team_count = User.objects.filter(
        referred_by=OuterRef("pk"), plan__isnull=False, account_status="approved",
    ).order_by().values("referred_by").annotate(total=Count("id")).values("total")
    paid_out = Withdrawal.objects.filter(
        user=OuterRef("pk"), status=Withdrawal.APPROVED,
    ).order_by().values("user").annotate(total=Sum("amount")).values("total")
    thresholds = level_thresholds()
    level_cases = [
        When(active_team__gte=thresholds[step], then=Value(step))
        for step in sorted(thresholds, reverse=True)
    ]
    users = User.objects.filter(role="user", plan__isnull=False, account_status="approved").select_related("plan").annotate(
        active_team=Coalesce(Subquery(team_count, output_field=IntegerField()), 0),
        earning=Coalesce(Subquery(paid_out, output_field=DecimalField(max_digits=12, decimal_places=2)), Decimal("0")),
    ).annotate(
        eligible_level=Case(*level_cases, default=Value(0), output_field=IntegerField()),
    )
    for row in users.values("eligible_level").annotate(total=Count("id")):
        step = row["eligible_level"] or 0
        if 1 <= step <= 10:
            level_counts[step] = row["total"]
    raw_level = (request.GET.get("level") or "").strip()
    if raw_level.isdigit():
        users = users.filter(eligible_level=int(raw_level))
    raw_day = (request.GET.get("date") or "").strip()
    if raw_day:
        users = between(users, "date_joined", parse_day(raw_day))
    q = (request.GET.get("q") or "").strip()
    if q:
        users = account_search(users, q)
    sort = (request.GET.get("sort") or "earnings").strip()
    if sort == "earnings_low":
        users = users.order_by("earning", "eligible_level", "id")
    elif sort == "level_low":
        users = users.order_by("eligible_level", "earning", "id")
    elif sort == "level":
        users = users.order_by("-eligible_level", "-earning", "id")
    else:
        users = users.order_by("-earning", "-eligible_level", "id")
    page, size = page_bounds(request, 30, 40)
    chunk, count, page, pages = slice_page(users, page, size)
    rows = []
    for index, user in enumerate(chunk):
        rows.append({
            "id": user.id,
            "full_name": user.display_name(),
            "email": user.email,
            "level": user.eligible_level,
            "active_team": user.active_team,
            "earnings": money(user.earning),
            "serial": (page - 1) * size + index + 1,
            "joined": user.date_joined.isoformat(),
        })
    return Response({
        "levels": [{"level": step, "count": level_counts[step]} for step in range(1, 11)],
        "rows": rows,
        "count": count,
        "page": page,
        "pages": pages,
    })


@api_view(["GET"])
def admin_transactions(request):
    if not require_admin(request):
        return Response(status=403)
    qs = user_tx_category(Transaction.objects.all(), request.GET.get("category") or "")
    return Response(tx_page(qs.order_by("-id"), request))


@api_view(["GET"])
def admin_deposits(request):
    if not require_admin(request):
        return Response(status=403)
    status = request.GET.get("status") or "pending"
    qs = Deposit.objects.select_related("user", "user__referred_by", "user__referred_by__plan", "plan").order_by("-id")
    if request.GET.get("today") == "1":
        raw_day = request.GET.get("date") or ""
        qs = approved_deposits_on(parse_day(raw_day) if raw_day else None).select_related("user", "user__referred_by", "user__referred_by__plan", "plan").order_by("-approved_at", "-id")
    elif status != "all":
        qs = qs.filter(status=status)
    if request.GET.get("date") and request.GET.get("today") != "1":
        qs = between(qs, "created_at", parse_day(request.GET.get("date")))
    if request.GET.get("plan"):
        qs = qs.filter(plan__name__iexact=request.GET.get("plan"))
    qs = related_search(qs, request.GET.get("q"), extras=("sender",))
    if request.GET.get("suggest") == "1":
        seen = []
        out = []
        for row in qs[:40]:
            if row.user_id in seen:
                continue
            seen.append(row.user_id)
            out.append({"id": row.user_id, "full_name": row.user.display_name(), "email": row.user.email, "phone": row.user.phone or "", "status": row.status})
            if len(out) >= 8:
                break
        return Response(out)
    page, size = page_bounds(request, 40, 40)
    chunk, count, page, pages = slice_page(qs, page, size)
    rows = [{
        "id": d.id, "user_id": d.user_id, "full_name": d.user.display_name(), "email": d.user.email,
        "username": d.user.username, "code": d.user.referral_code, "phone": d.user.phone or "",
        "plan_id": d.plan_id, "plan": d.plan.name if d.plan else None, "amount": money(d.amount), "method": d.method,
        "sender": d.sender, "sender_name": d.sender_name or "", "trx_no": d.trx_no or "",
        "status": d.status, "created_at": d.created_at.isoformat(),
        "reject_reason": d.reject_reason, "approved_at": d.approved_at.isoformat() if d.approved_at else None,
        "screenshot": slip_url("deposits", d.id, d.screenshot),
        "upliner": person_brief(d.user.referred_by),
    } for d in chunk]
    payload = paged(rows, count, page, pages)
    payload["plans"] = [{"id": plan.id, "name": plan.name, "price": money(plan.price)} for plan in Plan.objects.all()]
    return Response(payload)


@api_view(["GET"])
def deposit_slip(request, pk):
    if not require_admin(request):
        return Response(status=403)
    row = Deposit.objects.filter(pk=pk).first()
    if not row or not row.screenshot:
        return Response(status=404)
    return send_slip(row.screenshot)


@api_view(["GET"])
def upgrade_slip(request, pk):
    if not require_admin(request):
        return Response(status=403)
    row = UpgradeRequest.objects.filter(pk=pk).first()
    if not row or not row.screenshot:
        return Response(status=404)
    return send_slip(row.screenshot)


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def deposit_action(request, pk):
    if not require_admin(request):
        return Response(status=403)
    action = request.data.get("action")
    reason = (request.data.get("reason") or "").strip()
    if action == "reject" and not reason:
        return Response({"detail": "A rejection reason is required."}, status=400)
    with transaction.atomic():
        deposit = Deposit.objects.select_for_update().select_related("user", "plan").filter(pk=pk).first()
        if not deposit:
            return Response(status=404)
        if action == "edit":
            if deposit.status not in (Deposit.PENDING, Deposit.REJECTED):
                return Response({"detail": "Only pending or rejected payments can be edited."}, status=400)
            plan_id = request.data.get("plan_id")
            plan = Plan.objects.filter(pk=plan_id).first() if str(plan_id or "").isdigit() else None
            if not plan:
                return Response({"detail": "Choose a valid plan."}, status=400)
            try:
                amount = Decimal(str(request.data.get("amount") or "")).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                return Response({"detail": "Enter a valid payment amount."}, status=400)
            if not amount.is_finite() or amount <= 0 or amount > Decimal("9999999999.99"):
                return Response({"detail": "Payment amount must be between 0.01 and 9,999,999,999.99."}, status=400)
            screenshot = request.FILES.get("screenshot")
            if screenshot:
                screenshot = image_upload(screenshot)
                if not screenshot:
                    return Response({"detail": "Payment screenshot must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
            DepositEditHistory.objects.create(
                deposit=deposit,
                editor=request.user,
                plan_name=deposit.plan.name if deposit.plan else "",
                amount=deposit.amount,
                method=deposit.method,
                sender=deposit.sender,
                sender_name=deposit.sender_name,
                trx_no=deposit.trx_no,
                screenshot=deposit.screenshot.name if deposit.screenshot else None,
            )
            deposit.plan = plan
            deposit.amount = amount
            deposit.method = (request.data.get("method") or "").strip()[:80]
            deposit.sender = (request.data.get("sender") or "").strip()[:80]
            deposit.sender_name = (request.data.get("sender_name") or "").strip()[:120]
            deposit.trx_no = (request.data.get("trx_no") or "").strip()[:80]
            if screenshot:
                deposit.screenshot = screenshot
            deposit.save(update_fields=[
                "plan", "amount", "method", "sender", "sender_name", "trx_no",
                "screenshot", "updated_at",
            ])
            return Response({"ok": True, "status": deposit.status})
        if action == "approve":
            if deposit.status == Deposit.APPROVED:
                return Response({"detail": "This payment is already approved."}, status=400)
            deposit.status = Deposit.APPROVED
            deposit.approved_at = timezone.now()
            deposit.reject_reason = ""
            deposit.save(update_fields=["status", "approved_at", "reject_reason", "updated_at"])
            owner = User.objects.select_for_update().get(pk=deposit.user_id)
            owner.plan = deposit.plan
            owner.account_status = "approved"
            owner.ban_reason = ""
            owner.banned_at = None
            owner.save()
            activate_member(owner)
            already = Transaction.objects.filter(remark="referral", source_email=owner.email, plan_name=deposit.plan.name if deposit.plan else "").exists()
            if not already:
                credit_referral(deposit)
        elif action == "reject":
            if deposit.status == Deposit.APPROVED:
                return Response({"detail": "An approved payment cannot be rejected from here."}, status=400)
            deposit.status = Deposit.REJECTED
            deposit.reject_reason = reason
            deposit.save(update_fields=["status", "reject_reason", "updated_at"])
            if deposit.user.account_status != "approved":
                deposit.user.account_status = "rejected"
                deposit.user.save(update_fields=["account_status"])
        else:
            return Response({"detail": "Choose approve or reject."}, status=400)
    return Response({"ok": True})


@api_view(["GET"])
def admin_withdrawals(request):
    if not require_admin(request):
        return Response(status=403)
    status = request.GET.get("status") or "pending"
    qs = Withdrawal.objects.select_related("user").order_by("-id")
    if status != "all":
        qs = qs.filter(status=status)
    if request.GET.get("today") == "1" or request.GET.get("date"):
        qs = approved_withdrawals_on(parse_day(request.GET.get("date") or "today")).select_related("user").order_by("-updated_at", "-id")
    qs = related_search(qs, request.GET.get("q"), extras=("account",))
    if request.GET.get("suggest") == "1":
        seen = []
        out = []
        for row in qs[:40]:
            if row.user_id in seen:
                continue
            seen.append(row.user_id)
            out.append({"id": row.user_id, "full_name": row.user.display_name(), "email": row.user.email, "phone": row.user.phone or ""})
            if len(out) >= 8:
                break
        return Response(out)
    page, size = page_bounds(request, 40, 40)
    chunk, count, page, pages = slice_page(qs, page, size)
    rows = [{
        "id": w.id, "user_id": w.user_id, "full_name": w.user.display_name(), "email": w.user.email,
        "username": w.user.username, "phone": w.user.phone or "",
        "amount": money(w.amount), "method": w.method, "account_name": w.account_name, "account": w.account, "status": w.status,
        "admin_remark": w.admin_remark, "created_at": w.created_at.isoformat(),
        "approved_at": w.updated_at.isoformat() if w.status == Withdrawal.APPROVED and w.updated_at else None,
    } for w in chunk]
    return Response(paged(rows, count, page, pages))


@api_view(["GET"])
def withdrawal_summary(request):
    if not require_admin(request):
        return Response(status=403)
    return Response({
        "pending": money(_sum(Withdrawal.objects.filter(status=Withdrawal.PENDING))),
        "rejected": money(_sum(Withdrawal.objects.filter(status=Withdrawal.REJECTED))),
    })


@api_view(["GET", "POST"])
def withdrawal_action(request, pk):
    if not require_admin(request):
        return Response(status=403)
    row = Withdrawal.objects.select_related("user", "user__plan", "user__referred_by", "user__referred_by__plan").filter(pk=pk).first()
    if not row:
        return Response(status=404)
    if request.method == "GET":
        team = row.user.team
        approved = team.filter(plan__isnull=False, account_status="approved").count()
        paid = team.filter(plan__isnull=False).exclude(account_status="banned").count()
        unpaid = team.filter(plan__isnull=True).count()
        return Response({
            "account": person_brief(row.user),
            "upliner": person_brief(row.user.referred_by),
            "plan": row.user.plan.name if row.user.plan_id else "No plan",
            "joined": row.user.date_joined.isoformat(),
            "requested_at": row.created_at.isoformat(),
            "team": {"approved": approved, "paid": paid, "unpaid": unpaid, "total": approved},
        })
    action = request.data.get("action")
    remark = (request.data.get("reason") or request.data.get("remark") or "").strip()
    if action == "reject" and not remark:
        return Response({"detail": "A rejection reason is required."}, status=400)
    with transaction.atomic():
        locked = Withdrawal.objects.select_for_update().select_related("user").filter(pk=row.pk).first()
        if not locked or locked.status != Withdrawal.PENDING:
            return Response({"detail": "This withdrawal is already processed."}, status=400)
        owner = User.objects.select_for_update().get(pk=locked.user_id)
        if action == "approve":
            locked.status = Withdrawal.APPROVED
        elif action == "reject":
            owner.balance += locked.amount - locked.flower_part
            owner.flower_balance += locked.flower_part
            owner.save(update_fields=["balance", "flower_balance"])
            locked.status = Withdrawal.REJECTED
        else:
            return Response({"detail": "Choose approve or reject."}, status=400)
        locked.admin_remark = remark
        locked.updated_at = timezone.now()
        locked.save(update_fields=["status", "updated_at", "admin_remark"])
    return Response({"ok": True})


@api_view(["GET"])
def admin_upgrades(request):
    if not require_admin(request):
        return Response(status=403)
    status = request.GET.get("status") or "pending"
    rows = UpgradeRequest.objects.select_related("user", "user__referred_by", "from_plan", "to_plan").order_by("-id")
    if status != "all":
        rows = rows.filter(status=status)
    raw_day = (request.GET.get("date") or "").strip()
    if raw_day:
        chosen = parse_day(raw_day)
        start, end = day_window(chosen)
        approved_date = (
            Q(approved_at__gte=start, approved_at__lt=end)
            | Q(approved_at__isnull=True, updated_at__gte=start, updated_at__lt=end)
            | Q(approved_at__isnull=True, updated_at__isnull=True, created_at__gte=start, created_at__lt=end)
        )
        rejected_date = (
            Q(updated_at__gte=start, updated_at__lt=end)
            | Q(updated_at__isnull=True, created_at__gte=start, created_at__lt=end)
        )
        if status == "approved":
            rows = rows.filter(approved_date)
        elif status == "rejected":
            rows = rows.filter(rejected_date)
        elif status == "pending":
            rows = between(rows, "created_at", chosen)
        else:
            rows = rows.filter(
                Q(status=UpgradeRequest.PENDING, created_at__gte=start, created_at__lt=end)
                | (Q(status=UpgradeRequest.APPROVED) & approved_date)
                | (Q(status=UpgradeRequest.REJECTED) & rejected_date)
            )
    rows = related_search(rows, request.GET.get("q"), extras=("sender",))
    if request.GET.get("suggest") == "1":
        seen = []
        out = []
        for row in rows[:40]:
            if row.user_id in seen:
                continue
            seen.append(row.user_id)
            out.append({"id": row.user_id, "full_name": row.user.display_name(), "email": row.user.email, "phone": row.user.phone or ""})
            if len(out) >= 8:
                break
        return Response(out)
    page, size = page_bounds(request, 40, 40)
    chunk, count, page, pages = slice_page(rows, page, size)
    return Response(paged([{
        "id": r.id, "user_id": r.user_id, "full_name": r.user.display_name(), "email": r.user.email,
        "to_plan_id": r.to_plan_id,
        "from_plan": r.from_plan.name if r.from_plan else "—", "to_plan": r.to_plan.name if r.to_plan else "—",
        "plan": r.to_plan.name if r.to_plan else "—", "amount": money(r.amount), "method": r.method,
        "sender": r.sender, "sender_name": r.sender_name or "", "trx_no": r.trx_no or "",
        "status": r.status, "reject_reason": r.reject_reason,
        "old_paid": money(r.from_plan.price) if r.from_plan else "0.00",
        "additional": money((r.to_plan.price - r.from_plan.price) if r.from_plan and r.to_plan and r.to_plan.price > r.from_plan.price else r.amount),
        "username": r.user.username, "phone": r.user.phone or "",
        "upliner": person_brief(r.user.referred_by),
        "screenshot": slip_url("upgrades", r.id, r.screenshot),
    } for r in chunk], count, page, pages))


@api_view(["POST"])
def upgrade_action(request, pk):
    if not require_admin(request):
        return Response(status=403)
    action = request.data.get("action")
    reason = (request.data.get("reason") or "").strip()
    if action == "reject" and not reason:
        return Response({"detail": "A rejection reason is required."}, status=400)
    with transaction.atomic():
        row = UpgradeRequest.objects.select_for_update().select_related("user", "from_plan", "to_plan").filter(pk=pk).first()
        if not row:
            return Response(status=404)
        if action == "approve":
            if row.status == UpgradeRequest.APPROVED:
                return Response({"detail": "This upgrade is already approved."}, status=400)
            payable = row.amount
            if row.from_plan_id and row.to_plan_id:
                difference = (row.to_plan.price - row.from_plan.price).quantize(Decimal("0.01"))
                if difference > 0:
                    payable = difference
            pay = str(request.data.get("pay_commission") or "").lower() in ("1", "true", "yes")
            row.status = UpgradeRequest.APPROVED
            row.pay_commission = pay
            row.approved_at = timezone.now()
            owner = User.objects.select_for_update().get(pk=row.user_id)
            owner.plan = row.to_plan
            owner.account_status = "approved"
            owner.save(update_fields=["plan", "account_status"])
            activate_member(owner)
            if pay and payable > 0 and owner.referred_by_id:
                percent = SiteSetting.current().upgrade_commission_percent or Decimal("20")
                commission = (Decimal(payable) * percent / Decimal("100")).quantize(Decimal("0.01"))
                plan_name = row.to_plan.name if row.to_plan else ""
                already = Transaction.objects.filter(remark="upgrade_referral", source_email=owner.email, plan_name=plan_name).exists()
                if not already and commission > 0:
                    upliner = User.objects.select_for_update().get(pk=owner.referred_by_id)
                    upliner.balance += commission
                    upliner.save(update_fields=["balance"])
                    Transaction.objects.create(
                        user=upliner, amount=commission, trx_type="+", title="Upgrade Referral Commission",
                        level=1, plan_name=plan_name, source_email=owner.email, remark="upgrade_referral",
                    )
        elif action == "reject":
            if row.status == UpgradeRequest.APPROVED:
                return Response({"detail": "An approved upgrade cannot be rejected from here."}, status=400)
            row.status = UpgradeRequest.REJECTED
            row.reject_reason = reason
        else:
            return Response({"detail": "Choose approve or reject."}, status=400)
        row.save()
    return Response({"ok": True})


@api_view(["GET", "POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def admin_channels(request):
    if not require_admin(request):
        return Response(status=403)
    if request.method == "POST":
        picture = request.FILES.get("image")
        if picture:
            picture = image_upload(picture)
            if not picture:
                return Response({"detail": "Channel image must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
        link = (request.data.get("link") or "https://flowerblom.com").strip()
        if not link.startswith("https://") and not link.startswith("http://"):
            link = "https://flowerblom.com"
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
    if not require_admin(request):
        return Response(status=403)
    rows = []
    for contact in ContactAdmin.objects.all():
        legacy_botim = contact.number_type == "botim" and not contact.botim
        rows.append({
            "id": contact.id,
            "name": contact.name,
            "role": contact.role,
            "whatsapp": "" if legacy_botim else contact.whatsapp,
            "botim": contact.botim or (contact.whatsapp if legacy_botim else ""),
            "note": contact.note,
            "purpose": contact.purpose,
            "number_type": contact.number_type or "whatsapp",
            "whatsapp_icon": media_url(contact.whatsapp_icon),
            "botim_icon": media_url(contact.botim_icon),
        })
    return Response(rows)


@api_view(["GET", "POST"])
def admin_gateways(request):
    if not require_admin(request):
        return Response(status=403)
    if request.method == "POST":
        name = canonical_gateway_name(request.data.get("name") or "")
        account_name = (request.data.get("account_name") or "").strip()
        account_number = (request.data.get("account_number") or "").strip()
        pk = request.data.get("id")
        if str(request.data.get("delete") or "") in ("1", "true", "yes"):
            if str(pk or "").isdigit():
                Gateway.objects.filter(pk=pk).delete()
            return Response([gateway_public(gateway) for gateway in payment_gateways(active_only=False)])
        if not name:
            return Response({"detail": "Enter the method name, such as JazzCash or Easypaisa."}, status=400)
        gateway = Gateway.objects.filter(pk=pk).first() if str(pk or "").isdigit() else None
        if gateway is None:
            gateway = Gateway(name=name)
        gateway.name = name
        gateway.account_name = account_name
        gateway.account_number = account_number
        if "instructions" in request.data:
            gateway.instructions = request.data.get("instructions") or ""
        if "active" in request.data:
            gateway.active = str(request.data.get("active")).lower() in ("1", "true", "on", "yes")
        picture = request.FILES.get("image")
        if picture:
            picture = image_upload(picture)
            if not picture:
                return Response({"detail": "Payment icon must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
            gateway.image = picture
        gateway.save()
    return Response([gateway_public(gateway) for gateway in payment_gateways(active_only=False)])


@api_view(["GET", "POST"])
def admin_settings(request):
    if not require_admin(request):
        return Response(status=403)
    settings = SiteSetting.current()
    if request.method == "POST":
        settings.notice = request.data.get("notice") or settings.notice
        settings.site_name = request.data.get("site_name") or settings.site_name
        settings.save()
    return Response({"notice": settings.notice, "site_name": settings.site_name, "direct_percent": money(settings.direct_percent), "level2_percent": money(settings.level2_percent), "level3_percent": money(settings.level3_percent)})


@api_view(["GET"])
def admin_rewards(request):
    if not require_admin(request):
        return Response(status=403)
    return Response([{"id": r.id, "level": r.level, "title": r.title, "required_team": r.required_team, "prize": r.prize, "active": r.active} for r in Reward.objects.all()])


@api_view(["GET"])
def admin_levels(request):
    if not require_admin(request):
        return Response(status=403)
    settings = SiteSetting.current()
    return Response({"levels": [
        {"level": 1, "name": settings.direct_label or "Direct Referral Commission", "percent": money(settings.direct_percent)},
        {"level": 2, "name": settings.level2_label or "Level 2 – Indirect Referral Commission", "percent": money(settings.level2_percent)},
        {"level": 3, "name": settings.level3_label or "Level 3 – Indirect Referral Commission", "percent": money(settings.level3_percent)},
    ], "direct_label": settings.direct_label, "level2_label": settings.level2_label, "level3_label": settings.level3_label,
        "direct": money(settings.direct_percent), "level2": money(settings.level2_percent), "level3": money(settings.level3_percent),
        "upgrade_commission_percent": money(settings.upgrade_commission_percent),
        "plans": [{"id": p.id, "name": p.name, "price": money(p.price)} for p in Plan.objects.all()]})
