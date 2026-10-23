import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from .models import (
    AdminSession, BalanceAdjustment, Channel, ContactAdmin, Deposit, Gateway,
    ImpersonationGrant, Plan, SiteSetting, Transaction, UpgradeRequest, User, Withdrawal,
)
from .views import activate_member, approved_deposits_on, image_upload, media_url, money, parse_day, person_brief, user_payload


def require_admin(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or getattr(user, "role", "") != "admin":
        return False
    key = request.headers.get("X-Admin-Session") or ""
    if key:
        row = AdminSession.objects.filter(key=key, user=user, revoked=False).first()
        if not row:
            return False
    return True


def _sum(qs, field="amount"):
    return qs.aggregate(total=Sum(field))["total"] or Decimal("0")


def parse_amount(raw):
    try:
        amount = Decimal(str(raw or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0:
        return None
    return amount


@api_view(["POST"])
def adjust_balance(request, pk):
    if not require_admin(request):
        return Response(status=403)
    kind = request.data.get("kind") or ""
    if kind not in ("balance_add", "balance_sub", "flower_add", "flower_sub"):
        return Response({"detail": "Choose add or subtract for balance or flower earnings."}, status=400)
    amount = parse_amount(request.data.get("amount"))
    reason = (request.data.get("reason") or "").strip()
    request_key = (request.data.get("request_key") or "").strip()
    if amount is None:
        return Response({"detail": "Enter an amount above zero."}, status=400)
    if not reason:
        return Response({"detail": "A reason is required."}, status=400)
    if not request_key:
        return Response({"detail": "Missing request key."}, status=400)
    if BalanceAdjustment.objects.filter(request_key=request_key).exists():
        return Response({"detail": "This adjustment was already saved."}, status=400)
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=pk, role="user").first()
        if not user:
            return Response(status=404)
        if BalanceAdjustment.objects.filter(request_key=request_key).exists():
            return Response({"detail": "This adjustment was already saved."}, status=400)
        flower = kind.startswith("flower")
        before = user.flower_balance if flower else user.balance
        if kind.endswith("_sub") and before < amount:
            name = "Flower Earnings" if flower else "balance"
            return Response({"detail": f"That amount is higher than the {name}."}, status=400)
        after = before + amount if kind.endswith("_add") else before - amount
        if flower:
            user.flower_balance = after
            user.save(update_fields=["flower_balance"])
            title = "Flower Earnings added" if kind.endswith("_add") else "Flower Earnings subtracted"
            remark = "flower_add" if kind.endswith("_add") else "flower_sub"
        else:
            user.balance = after
            user.save(update_fields=["balance"])
            title = "Balance added" if kind.endswith("_add") else "Balance subtracted"
            remark = "balance_add" if kind.endswith("_add") else "balance_sub"
        Transaction.objects.create(
            user=user, amount=amount, trx_type="+" if kind.endswith("_add") else "-",
            title=f"{title}: {reason[:80]}", remark=remark,
        )
        BalanceAdjustment.objects.create(
            user=user, admin=request.user, kind=kind, amount=amount, reason=reason,
            before_amount=before, after_amount=after, request_key=request_key,
        )
    return Response({"balance": money(user.balance), "flower_balance": money(user.flower_balance)})


@api_view(["POST"])
def ban_user(request, pk):
    if not require_admin(request):
        return Response(status=403)
    user = User.objects.filter(pk=pk, role="user").first()
    if not user:
        return Response(status=404)
    action = (request.data.get("action") or "ban").strip().lower()
    if action == "unban":
        user.account_status = "approved" if user.plan_id else "pending"
        user.ban_reason = ""
        user.banned_at = None
        user.save(update_fields=["account_status", "ban_reason", "banned_at"])
        return Response({"ok": True, "status": user.account_status, "banned": False})
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return Response({"detail": "A ban reason is required."}, status=400)
    user.account_status = "banned"
    user.ban_reason = reason
    user.banned_at = timezone.now()
    user.save(update_fields=["account_status", "ban_reason", "banned_at"])
    return Response({"ok": True, "status": "banned", "banned": True})


@api_view(["POST"])
def user_password(request, pk):
    if not require_admin(request):
        return Response(status=403)
    password = request.data.get("password") or ""
    if len(password) < 6:
        return Response({"detail": "Password must be at least 6 characters."}, status=400)
    user = User.objects.filter(pk=pk, role="user").first()
    if not user:
        return Response(status=404)
    user.set_password(password)
    user.save(update_fields=["password"])
    return Response({"ok": True})


@api_view(["POST"])
def impersonate(request, pk):
    if not require_admin(request):
        return Response(status=403)
    user = User.objects.filter(pk=pk, role="user").first()
    if not user:
        return Response(status=404)
    grant = ImpersonationGrant.objects.create(
        admin=request.user, user=user, code=secrets.token_urlsafe(24),
        expires_at=timezone.now() + timedelta(minutes=2),
    )
    return Response({"code": grant.code})


@api_view(["POST"])
def impersonate_exchange(request):
    code = (request.data.get("code") or "").strip()
    grant = ImpersonationGrant.objects.select_related("user").filter(code=code, used_at__isnull=True).first()
    if not grant or grant.expires_at < timezone.now():
        return Response({"detail": "This login link has expired."}, status=400)
    grant.used_at = timezone.now()
    grant.save(update_fields=["used_at"])
    from rest_framework.authtoken.models import Token
    token, _ = Token.objects.get_or_create(user=grant.user)
    return Response({"token": token.key, "user": user_payload(grant.user)})


@api_view(["GET", "POST"])
def admin_account(request):
    if not require_admin(request):
        return Response(status=403)
    admin = request.user
    if request.method == "GET":
        return Response({"email": admin.email, "username": admin.username})
    current = request.data.get("current_password") or ""
    if not admin.check_password(current):
        return Response({"detail": "Current password is incorrect."}, status=400)
    email = (request.data.get("email") or "").strip().lower()
    new_password = request.data.get("new_password") or ""
    if email and email != admin.email:
        if User.objects.filter(email__iexact=email).exclude(pk=admin.pk).exists():
            return Response({"detail": "That email is already used."}, status=400)
        admin.email = email
    if new_password:
        if len(new_password) < 6:
            return Response({"detail": "New password must be at least 6 characters."}, status=400)
        admin.set_password(new_password)
    admin.save()
    return Response({"email": admin.email})


def device_label(agent):
    text = (agent or "").lower()
    if "mobile" in text or "android" in text or "iphone" in text:
        return "Mobile"
    return "Desktop"


@api_view(["GET", "POST"])
def admin_sessions(request):
    if not require_admin(request):
        return Response(status=403)
    if request.method == "POST":
        key = (request.data.get("key") or "").strip()
        row = AdminSession.objects.filter(user=request.user, key=key).first()
        if not row:
            return Response(status=404)
        row.revoked = True
        row.save(update_fields=["revoked"])
        return Response({"ok": True})
    rows = []
    for row in request.user.admin_sessions.order_by("-id")[:30]:
        rows.append({
            "key": row.key,
            "email": request.user.email,
            "device": device_label(row.user_agent),
            "browser": row.user_agent[:120],
            "login_time": row.created_at.isoformat(),
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            "revoked": row.revoked,
        })
    return Response(rows)


def open_session(request, user):
    row = AdminSession.objects.create(
        user=user,
        key=secrets.token_urlsafe(24),
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:255],
        ip=(request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR") or "")[:80],
    )
    return row.key


@api_view(["GET"])
def today_approved(request):
    if not require_admin(request):
        return Response(status=403)
    raw_day = request.GET.get("date") or ""
    day = parse_day(raw_day) if raw_day else timezone.localdate()
    q = (request.GET.get("q") or "").strip()
    plan = (request.GET.get("plan") or "").strip()
    deposits = approved_deposits_on(day).select_related("user", "user__plan", "user__referred_by", "plan")
    if q:
        if "@" in q and " " not in q:
            deposits = deposits.filter(user__email__iexact=q)
        else:
            deposits = deposits.filter(Q(user__full_name__istartswith=q) | Q(user__username__istartswith=q) | Q(user__email__istartswith=q) | Q(user__phone__startswith=q))
    if plan:
        deposits = deposits.filter(plan__name__iexact=plan)
    from .views import slip_url
    rows = []
    receive = Decimal("0")
    plan_counts = {name: 0 for name in Plan.objects.values_list("name", flat=True)}
    for deposit in deposits.order_by("-approved_at", "-updated_at", "-id"):
        receive += deposit.amount
        name = deposit.plan.name if deposit.plan else ""
        if name in plan_counts:
            plan_counts[name] += 1
        rows.append({
            "id": deposit.id,
            "user_id": deposit.user_id,
            "full_name": deposit.user.display_name(),
            "username": deposit.user.username,
            "email": deposit.user.email,
            "phone": deposit.user.phone or "",
            "plan": name,
            "amount": money(deposit.amount),
            "method": deposit.method,
            "sender": deposit.sender,
            "status": "Approved",
            "approved_at": deposit.approved_at.isoformat() if deposit.approved_at else None,
            "created_at": deposit.created_at.isoformat(),
            "upliner": person_brief(deposit.user.referred_by),
            "screenshot": slip_url("deposits", deposit.id, deposit.screenshot),
        })
    return Response({
        "date": day.isoformat(),
        "receive": money(receive),
        "users": len(rows),
        "plans": [{"name": name, "count": plan_counts[name]} for name in plan_counts],
        "rows": rows,
    })


@api_view(["GET"])
def upgrade_board(request):
    if not require_admin(request):
        return Response(status=403)
    today = timezone.localdate()
    approved = UpgradeRequest.objects.filter(status=UpgradeRequest.APPROVED)
    approved_today = approved.filter(approved_at__date=today)
    return Response({
        "today_count": approved_today.count(),
        "today_receive": money(_sum(approved_today)),
        "pending": UpgradeRequest.objects.filter(status=UpgradeRequest.PENDING).count(),
        "approved": approved.count(),
        "rejected": UpgradeRequest.objects.filter(status=UpgradeRequest.REJECTED).count(),
        "commission_percent": money(SiteSetting.current().upgrade_commission_percent),
        "commission_base": "Additional payment = new plan price minus current plan price. 20% of that amount goes to the direct upliner only when Yes is selected.",
    })


@api_view(["POST"])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def channel_edit(request, pk):
    if not require_admin(request):
        return Response(status=403)
    channel = Channel.objects.filter(pk=pk).first()
    if not channel:
        return Response(status=404)
    if request.data.get("delete") in (True, "1", "true"):
        channel.delete()
        return Response({"ok": True})
    channel.title = request.data.get("title") or channel.title
    channel.purpose = request.data.get("purpose") if "purpose" in request.data else channel.purpose
    link = (request.data.get("link") or channel.link).strip()
    if link.startswith("http://") or link.startswith("https://"):
        channel.link = link
    picture = request.FILES.get("image")
    if picture:
        picture = image_upload(picture)
        if not picture:
            return Response({"detail": "Channel image must be a JPG, PNG, or WEBP image under 8 MB."}, status=400)
        channel.image = picture
    channel.save()
    return Response({"ok": True})


@api_view(["POST"])
def contact_save(request):
    if not require_admin(request):
        return Response(status=403)
    pk = request.data.get("id")
    if request.data.get("delete") in (True, "1", "true") and str(pk or "").isdigit():
        ContactAdmin.objects.filter(pk=pk).delete()
        return Response({"ok": True})
    number_type = request.data.get("number_type") or "whatsapp"
    if number_type not in ("whatsapp", "botim"):
        return Response({"detail": "Choose WhatsApp or Botim."}, status=400)
    if str(pk or "").isdigit():
        row = ContactAdmin.objects.filter(pk=pk).first()
    else:
        row = ContactAdmin()
    if not row:
        return Response(status=404)
    row.name = request.data.get("name") or row.name or "Admin"
    row.role = request.data.get("role") or request.data.get("instructions") or row.role
    row.note = request.data.get("instructions") or request.data.get("note") or row.note
    row.purpose = request.data.get("purpose") or row.purpose
    row.whatsapp = request.data.get("number") or request.data.get("whatsapp") or row.whatsapp
    row.number_type = number_type
    row.save()
    return Response({"id": row.id})


@api_view(["POST"])
def save_levels(request):
    if not require_admin(request):
        return Response(status=403)
    settings = SiteSetting.current()
    for field in ("direct_percent", "level2_percent", "level3_percent", "upgrade_commission_percent"):
        if request.data.get(field) not in (None, ""):
            try:
                setattr(settings, field, Decimal(str(request.data.get(field))).quantize(Decimal("0.01")))
            except (InvalidOperation, ValueError):
                return Response({"detail": "Enter a valid percentage."}, status=400)
    for field in ("direct_label", "level2_label", "level3_label"):
        if request.data.get(field):
            setattr(settings, field, str(request.data.get(field))[:80])
    settings.save()
    return Response({"ok": True})
