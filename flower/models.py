from django.contrib.auth.models import AbstractUser
from django.db import models


class Plan(models.Model):
    name = models.CharField(max_length=80, unique=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    daily_tasks = models.PositiveIntegerField(default=5)
    color = models.CharField(max_length=20, default="#fff3cd")
    text_color = models.CharField(max_length=20, default="#856404")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name


class User(AbstractUser):
    full_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    role = models.CharField(max_length=10, default="user")
    plan = models.ForeignKey(Plan, null=True, blank=True, on_delete=models.SET_NULL)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    referral_code = models.CharField(max_length=64, unique=True)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="team")
    account_status = models.CharField(max_length=20, default="approved")
    level = models.PositiveIntegerField(default=0)
    flower_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    flower_credited = models.BooleanField(default=False)
    activated_at = models.DateTimeField(null=True, blank=True)

    def display_name(self):
        return self.full_name or self.username


class SiteSetting(models.Model):
    notice = models.TextField(default="Welcome to Flower. Complete your tasks and grow your team.")
    site_name = models.CharField(max_length=80, default="Flower")
    direct_percent = models.DecimalField(max_digits=5, decimal_places=2, default=25)
    level2_percent = models.DecimalField(max_digits=5, decimal_places=2, default=3)
    level3_percent = models.DecimalField(max_digits=5, decimal_places=2, default=1)

    @classmethod
    def current(cls):
        row, _ = cls.objects.get_or_create(pk=1)
        return row


class Channel(models.Model):
    title = models.CharField(max_length=150)
    purpose = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    link = models.URLField(max_length=500)
    image = models.FileField(upload_to="channels/", blank=True, null=True)
    status = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class ContactAdmin(models.Model):
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=120, blank=True)
    whatsapp = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=255, blank=True)


class Gateway(models.Model):
    name = models.CharField(max_length=80)
    account_name = models.CharField(max_length=120, blank=True)
    account_number = models.CharField(max_length=80, blank=True)
    active = models.BooleanField(default=True)


class Reward(models.Model):
    level = models.PositiveIntegerField()
    title = models.CharField(max_length=120)
    required_team = models.PositiveIntegerField(default=0)
    prize = models.CharField(max_length=120)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["level", "id"]


class Task(models.Model):
    title = models.CharField(max_length=160)
    detail = models.CharField(max_length=255, blank=True)
    reward = models.DecimalField(max_digits=10, decimal_places=2, default=10)
    active = models.BooleanField(default=True)


class TaskDone(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "created_at"], name="flower_taskdone_user_created")]


class TaskClaim(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    day = models.DateField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "task", "day"], name="flower_one_task_per_day"),
        ]


class Deposit(models.Model):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="deposits")
    plan = models.ForeignKey(Plan, null=True, blank=True, on_delete=models.SET_NULL)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=80, blank=True)
    sender = models.CharField(max_length=80, blank=True)
    screenshot = models.FileField(upload_to="payments/", blank=True, null=True)
    status = models.CharField(max_length=20, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Withdrawal(models.Model):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="withdrawals")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=80)
    account_name = models.CharField(max_length=120, blank=True, default="")
    account = models.CharField(max_length=80)
    flower_part = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(null=True, blank=True)


class UpgradeRequest(models.Model):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    from_plan = models.ForeignKey(Plan, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    to_plan = models.ForeignKey(Plan, null=True, on_delete=models.SET_NULL, related_name="+")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    method = models.CharField(max_length=80, blank=True)
    sender = models.CharField(max_length=80, blank=True)
    screenshot = models.FileField(upload_to="payments/", blank=True, null=True)
    status = models.CharField(max_length=20, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)


class Transaction(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="transactions")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    trx_type = models.CharField(max_length=1, default="+")
    title = models.CharField(max_length=160)
    level = models.PositiveIntegerField(default=0)
    plan_name = models.CharField(max_length=80, blank=True)
    source_email = models.CharField(max_length=180, blank=True)
    remark = models.CharField(max_length=60, default="other")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
