"""Account health scoring.

Aggregates per-account signals into a single 0-100 score so the
dashboard can highlight risky accounts and the operator can
prioritize warming. Cheap to recompute; called from a periodic
management command.
"""
from datetime import timedelta

from django.db.models import Count, Max
from django.utils import timezone


def compute_health(account, *, now=None, flood_24h=0, last_success=None):
    now = now or timezone.now()
    score = 100

    if not account.is_active:
        score -= 60
    if account.is_spam:
        score -= 40

    age = now - account.created_at
    if age < timedelta(minutes=30):
        score -= 20
    elif age < timedelta(hours=2):
        score -= 10

    if flood_24h:
        score -= min(20, flood_24h * 2)

    if last_success is None:
        if age > timedelta(days=7):
            score -= 15
    else:
        idle = now - last_success
        if idle > timedelta(days=14):
            score -= 15
        elif idle > timedelta(days=7):
            score -= 8

    if account.daily_op_limit and account.quota_window_count >= account.daily_op_limit:
        score -= 5

    return max(0, min(100, score))


def recompute_for_user(user):
    """Bulk-compute health for every account owned by `user`."""
    from .models import Account
    from jobs.models import TaskEvent

    now = timezone.now()
    cutoff = now - timedelta(hours=24)

    accounts = list(Account.objects.filter(owner=user))
    if not accounts:
        return 0

    flood_counts = dict(
        TaskEvent.objects
        .filter(account__in=accounts, step='flood_wait', created_at__gte=cutoff)
        .values('account_id')
        .annotate(c=Count('id'))
        .values_list('account_id', 'c')
    )
    last_success = {
        r['account_id']: r['latest']
        for r in TaskEvent.objects
        .filter(account__in=accounts, level='success')
        .values('account_id')
        .annotate(latest=Max('created_at'))
    }

    for acc in accounts:
        flood_24 = flood_counts.get(acc.pk, 0)
        ls = last_success.get(acc.pk)
        score = compute_health(acc, now=now, flood_24h=flood_24, last_success=ls)
        Account.objects.filter(pk=acc.pk).update(
            health_score=score,
            health_score_at=now,
            flood_wait_count_24h=flood_24,
            last_successful_op_at=ls,
        )
    return len(accounts)


def recompute_all():
    from django.contrib.auth import get_user_model
    User = get_user_model()
    total = 0
    for user in User.objects.all():
        total += recompute_for_user(user)
    return total
