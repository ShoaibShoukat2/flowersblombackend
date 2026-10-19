from datetime import timedelta
from decimal import Decimal

TASK_RATES = {
    0: Decimal("7"),
    1: Decimal("10"),
    2: Decimal("15"),
    3: Decimal("20"),
    4: Decimal("25"),
    5: Decimal("30"),
    6: Decimal("35"),
    7: Decimal("40"),
    8: Decimal("100"),
}
LEVEL_JOININGS = {1: 3, 2: 12, 3: 27, 4: 150, 5: 220, 6: 370, 7: 500, 8: 1000}
FLOWER_AMOUNT = Decimal("200.00")
PLAN_TASKS = {"basic": 1, "gold": 2, "standard": 4, "premium": 6, "super premium": 10}


def level_for_joinings(count):
    level = 0
    for step in range(1, 9):
        if count >= LEVEL_JOININGS[step]:
            level = step
    return level


def next_level_target(level):
    if level >= 8:
        return None, None
    step = level + 1
    return step, LEVEL_JOININGS[step]


def task_rate(level):
    return TASK_RATES[level if level in TASK_RATES else 0]


def cycle_status(level, joining_count, started_at, latest_joining_at, now):
    if joining_count <= 0 and level < 3:
        days = 15
        start = started_at
        kind = "initial"
    else:
        days = 7
        start = latest_joining_at or started_at
        kind = "joining"
    end = start + timedelta(days=days)
    remaining = max(0, int((end - now).total_seconds()))
    return {"locked": now > end, "days": days, "ends": end, "remaining": remaining, "kind": kind}
