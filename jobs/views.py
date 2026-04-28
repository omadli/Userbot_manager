from django.conf import settings
from django.contrib import messages
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone, dateparse
from asgiref.sync import sync_to_async
from urllib.parse import urlencode

from accounts.models import Account
import json
from .models import NamePool, RandomName, ScriptTemplate, Task, TaskEvent


def _parse_schedule(post):
    """
    Read optional scheduler fields from a POST. Returns a dict suitable to
    splat into `Task.objects.acreate(**task_kwargs)`:

        {'scheduled_at': datetime|None, 'recurring_cron': str}

    - `scheduled_at`: HTML datetime-local value ("YYYY-MM-DDTHH:MM"). If the
      project's TIME_ZONE is set we make it aware. Past timestamps are
      allowed (task runs immediately).
    - `recurring_cron`: silently dropped when croniter rejects it, so a bad
      input doesn't stop task creation.
    """
    raw_at = (post.get('scheduled_at') or '').strip()
    raw_cron = (post.get('recurring_cron') or '').strip()

    scheduled_at = None
    if raw_at:
        scheduled_at = dateparse.parse_datetime(raw_at)
        if scheduled_at and timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())

    if raw_cron:
        try:
            from croniter import croniter
            croniter(raw_cron, timezone.now())
        except Exception:
            raw_cron = ''  # drop invalid expressions silently

    return {'scheduled_at': scheduled_at, 'recurring_cron': raw_cron}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def render_async(*args, **kwargs):
    return await sync_to_async(render)(*args, **kwargs)


async def _require_login(request):
    user = await request.auser()
    if not user.is_authenticated:
        return None
    return user


async def _require_superuser(request):
    """Gate for script CRUD and script execution. Returns (user, response)."""
    user = await _require_login(request)
    if user is None:
        return None, _login_redirect(request)
    if not user.is_superuser:
        messages.error(request, "Bu sahifa faqat adminlar uchun")
        return None, redirect('accounts:dashboard')
    return user, None


def _login_redirect(request):
    return redirect(f"{settings.LOGIN_URL}?next={request.path}")


# ---------------------------------------------------------------------------
# Name pools
# ---------------------------------------------------------------------------

@sync_to_async
def _list_pools(user):
    return list(
        NamePool.objects.filter(owner=user)
        .annotate(count=Count('names'))
        .order_by('-created_at')
    )


async def pool_list(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            name = (request.POST.get('name') or '').strip()
            category = request.POST.get('category') or 'any'
            if not name:
                messages.error(request, "Pool nomi bo'sh bo'lmasin")
            else:
                pool = await NamePool.objects.acreate(
                    name=name, category=category, owner=user,
                )
                messages.success(request, f"'{pool.name}' yaratildi")
                return redirect('jobs:pool_detail', pk=pool.pk)
        elif action == 'delete':
            pk = request.POST.get('pk')
            if pk and pk.isdigit():
                await NamePool.objects.filter(pk=int(pk), owner=user).adelete()
                messages.success(request, "Pool o'chirildi")
        return redirect('jobs:pool_list')

    pools = await _list_pools(user)
    return await render_async(request, 'jobs/pool_list.html', {'pools': pools})


@sync_to_async
def _load_pool(user, pk):
    return get_object_or_404(NamePool, pk=pk, owner=user)


@sync_to_async
def _names_page(pool, search, page=1, per_page=500):
    qs = RandomName.objects.filter(pool=pool)
    if search:
        qs = qs.filter(text__icontains=search)
    total = qs.count()
    names = list(qs.order_by('id')[(page - 1) * per_page: page * per_page])
    return names, total


@sync_to_async
def _bulk_insert_names(pool, lines):
    existing = set(
        RandomName.objects.filter(pool=pool).values_list('text', flat=True)
    )
    to_create = []
    seen = set()
    duplicates = 0
    for text in lines:
        text = text[:255]
        if text in existing or text in seen:
            duplicates += 1
            continue
        seen.add(text)
        to_create.append(RandomName(pool=pool, text=text))
    RandomName.objects.bulk_create(to_create, ignore_conflicts=True)
    return len(to_create), duplicates


async def pool_detail(request, pk):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    pool = await _load_pool(user, pk)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'edit_pool':
            pool.name = (request.POST.get('name') or pool.name).strip()
            pool.category = request.POST.get('category') or pool.category
            pool.description = request.POST.get('description') or ''
            await pool.asave()
            messages.success(request, "Pool sozlamalari saqlandi")

        elif action == 'bulk_add':
            raw = request.POST.get('bulk_names') or ''
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if not lines:
                messages.warning(request, "Matn bo'sh")
            else:
                added, dups = await _bulk_insert_names(pool, lines)
                messages.success(
                    request,
                    f"{added} ta nom qo'shildi" + (f" ({dups} ta duplikat)" if dups else ""),
                )

        elif action == 'add_one':
            text = (request.POST.get('text') or '').strip()
            if text:
                try:
                    await RandomName.objects.acreate(pool=pool, text=text[:255])
                    messages.success(request, "Qo'shildi")
                except Exception:
                    messages.warning(request, "Bunday nom allaqachon mavjud")

        elif action == 'delete_name':
            name_pk = request.POST.get('name_pk')
            if name_pk and name_pk.isdigit():
                await RandomName.objects.filter(pk=int(name_pk), pool=pool).adelete()

        elif action == 'clear_all':
            n = await RandomName.objects.filter(pool=pool).acount()
            await RandomName.objects.filter(pool=pool).adelete()
            messages.success(request, f"{n} ta nom o'chirildi")

        elif action == 'generate_uzbek':
            from .wordlist import generate_names
            try:
                count = max(1, min(int(request.POST.get('count', '50')), 5000))
            except (TypeError, ValueError):
                count = 50
            try:
                words_per = max(1, min(int(request.POST.get('words_per_name', '2')), 5))
            except (TypeError, ValueError):
                words_per = 2
            script = 'cyrillic' if request.POST.get('script') == 'cyrillic' else 'latin'
            case = request.POST.get('case') or 'title'
            sep_choice = request.POST.get('separator') or 'space'
            sep = {'space': ' ', 'dash': '-', 'underscore': '_', 'none': ''}.get(sep_choice, ' ')

            try:
                lines = await sync_to_async(generate_names, thread_sensitive=False)(
                    count, words_per_name=words_per, script=script,
                    case=case, separator=sep,
                )
            except Exception as e:
                messages.error(request, f"Wordlist yuklab bo'lmadi: {e}")
                return redirect('jobs:pool_detail', pk=pk)

            if not lines:
                messages.warning(request, "Hech narsa generatsiya qilinmadi")
            else:
                added, dups = await _bulk_insert_names(pool, lines)
                messages.success(
                    request,
                    f"{added} ta nom generatsiya qilindi" + (f" ({dups} ta duplikat)" if dups else ""),
                )

        return redirect('jobs:pool_detail', pk=pk)

    search = (request.GET.get('q') or '').strip()
    names, total_names = await _names_page(pool, search)
    return await render_async(request, 'jobs/pool_detail.html', {
        'pool': pool,
        'names': names,
        'total_names': total_names,
        'search': search,
        'shown_limit': 500,
    })


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@sync_to_async
def _list_tasks(user):
    return list(Task.objects.filter(owner=user).order_by('-created_at')[:200])


@sync_to_async
def _compute_stats(user):
    """
    Aggregate dashboard metrics for the given user.

    Returns a dict the template can render directly. All counts are scoped
    to `user` (multi-tenant) and pulled in one pass each — no N+1.
    """
    from datetime import timedelta
    from django.db.models import Sum, Count, Q
    from django.db.models.functions import TruncDate

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = today_start - timedelta(days=30)

    # Summary
    task_qs = Task.objects.filter(owner=user)
    event_qs = TaskEvent.objects.filter(task__owner=user)

    today_events = event_qs.filter(created_at__gte=today_start)

    totals = {
        'total_tasks': task_qs.count(),
        'tasks_today': task_qs.filter(created_at__gte=today_start).count(),
        'ops_today': today_events.count(),
        'errors_today': today_events.filter(level='error').count(),
        'running_tasks': task_qs.filter(status='running').count(),
        'scheduled_tasks': task_qs.filter(
            status='pending', scheduled_at__gt=now,
        ).count(),
    }

    # Time-series: one point per day for last 30 days, split by level.
    series_raw = list(
        event_qs
        .filter(created_at__gte=thirty_days_ago)
        .annotate(date=TruncDate('created_at'))
        .values('date', 'level')
        .annotate(count=Count('id'))
        .order_by('date')
    )
    # Pivot to {date: {success, error, warning, info}}
    pivot = {}
    for row in series_raw:
        d = row['date'].isoformat()
        pivot.setdefault(d, {'success': 0, 'error': 0, 'warning': 0, 'info': 0})
        pivot[d][row['level']] = row['count']

    # Fill in missing days with zeros so the chart shows a continuous line.
    days = []
    cur = thirty_days_ago.date()
    end = today_start.date()
    while cur <= end:
        d = cur.isoformat()
        entry = pivot.get(d, {'success': 0, 'error': 0, 'warning': 0, 'info': 0})
        days.append({'date': d, **entry})
        cur += timedelta(days=1)

    # Per-kind breakdown
    kind_stats = list(
        task_qs.values('kind')
        .annotate(
            task_count=Count('id'),
            total_ops=Sum('done'),
            success=Sum('success_count'),
            errors=Sum('error_count'),
        )
        .order_by('-task_count')
    )
    # Add display label + rate.
    kind_map = dict(Task.KIND_CHOICES)
    for r in kind_stats:
        r['label'] = kind_map.get(r['kind'], r['kind'])
        done = (r['success'] or 0) + (r['errors'] or 0)
        r['success_rate'] = round(100 * (r['success'] or 0) / done, 1) if done else 0

    # Top accounts by operation volume.
    top_accounts = list(
        event_qs.filter(account__isnull=False)
        .values('account__id', 'account__phone_number',
                'account__first_name', 'account__is_active', 'account__is_spam')
        .annotate(
            total=Count('id'),
            success=Count('id', filter=Q(level='success')),
            errors=Count('id', filter=Q(level='error')),
            last_active=Sum('id'),  # placeholder; we'll fetch max separately
        )
        .order_by('-total')[:20]
    )
    for r in top_accounts:
        done = r['success'] + r['errors']
        r['success_rate'] = round(100 * r['success'] / done, 1) if done else 0

    # Status breakdown
    status_stats = list(
        task_qs.values('status')
        .annotate(count=Count('id'))
        .order_by('-count')
    )
    status_map = dict(Task.STATUS_CHOICES)
    for r in status_stats:
        r['label'] = status_map.get(r['status'], r['status'])

    return {
        'totals': totals,
        'days': days,
        'kind_stats': kind_stats,
        'top_accounts': top_accounts,
        'status_stats': status_stats,
    }


async def stats_dashboard(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    ctx = await _compute_stats(user)
    # Pre-serialize time-series for Chart.js (avoid template-side JSON headaches).
    ctx['days_json'] = json.dumps(ctx['days'])
    return await render_async(request, 'jobs/stats_dashboard.html', ctx)


async def task_list(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    if request.method == 'POST':
        action = request.POST.get('action')
        pk = request.POST.get('pk')
        if pk and pk.isdigit():
            pk = int(pk)
            if action == 'delete':
                task = await Task.objects.filter(pk=pk, owner=user).afirst()
                if task and task.status != 'running':
                    await Task.objects.filter(pk=pk, owner=user).adelete()
                    messages.success(request, f"Task #{pk} o'chirildi")
                else:
                    messages.warning(request, "Ishlab turgan taskni o'chirib bo'lmaydi")
            elif action == 'cancel':
                await Task.objects.filter(pk=pk, owner=user, status__in=['pending', 'running']).aupdate(
                    cancel_requested=True
                )
                messages.success(request, f"Task #{pk} bekor qilish so'raldi")
        return redirect('jobs:task_list')

    tasks = await _list_tasks(user)
    return await render_async(request, 'jobs/task_list.html', {'tasks': tasks})


@sync_to_async
def _load_accounts_for_task(user, account_ids):
    return list(
        Account.objects.filter(id__in=account_ids, owner=user)
        .order_by('phone_number')
    )


@sync_to_async
def _list_pools_for_group_task(user):
    return list(
        NamePool.objects.filter(owner=user, category__in=['group', 'any'])
        .annotate(count=Count('names'))
        .order_by('-created_at')
    )


async def task_create_groups(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    # account_ids can come from GET (redirected from accounts bulk action) or POST
    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []

    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan. Avval akkauntlar ro'yxatidan tanlang.")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi (yoki boshqa foydalanuvchiga tegishli)")
        return redirect('accounts:dashboard')

    pools = await _list_pools_for_group_task(user)

    if request.method == 'POST':
        try:
            pool_id = int(request.POST.get('pool_id') or 0)
            count_per = int(request.POST.get('count_per_account') or 1)
            delay_min = float(request.POST.get('delay_min_sec') or 30)
            delay_max = float(request.POST.get('delay_max_sec') or 90)
            concurrency = int(request.POST.get('concurrency') or 5)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato bor")
            return redirect(
                f"{reverse('jobs:task_create_groups')}?{urlencode([('account_ids', i) for i in account_ids])}"
            )

        if count_per < 1 or count_per > 50:
            messages.error(request, "Akkauntga guruhlar soni 1-50 oralig'ida bo'lsin")
            return redirect(
                f"{reverse('jobs:task_create_groups')}?{urlencode([('account_ids', i) for i in account_ids])}"
            )
        if delay_min < 0 or delay_max < delay_min:
            messages.error(request, "Delay qiymatlari noto'g'ri (min ≤ max, min ≥ 0)")
            return redirect(
                f"{reverse('jobs:task_create_groups')}?{urlencode([('account_ids', i) for i in account_ids])}"
            )
        if concurrency < 1 or concurrency > 50:
            messages.error(request, "Parallel qiymati 1-50 oralig'ida bo'lsin")
            return redirect(
                f"{reverse('jobs:task_create_groups')}?{urlencode([('account_ids', i) for i in account_ids])}"
            )

        megagroup = request.POST.get('megagroup') == 'on'
        skip_inactive = request.POST.get('skip_inactive') == 'on'
        skip_spam = request.POST.get('skip_spam') == 'on'
        send_welcome = request.POST.get('send_welcome_message') == 'on'

        params = {
            'account_ids': account_ids,
            'count_per_account': count_per,
            'pool_id': pool_id,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': skip_inactive,
            'skip_spam': skip_spam,
            'megagroup': megagroup,
            'send_welcome_message': send_welcome,
        }
        task = await Task.objects.acreate(
            kind='create_groups',
            owner=user,
            params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(
            request,
            f"Vazifa #{task.pk} navbatga qo'yildi. Worker ishlayotgan bo'lsa ijro boshlanadi.",
        )
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_groups.html', {
        'accounts': accounts,
        'pools': pools,
        'account_ids': account_ids,
    })


@sync_to_async
def _list_pools_for_channel_task(user):
    return list(
        NamePool.objects.filter(owner=user, category__in=['channel', 'any'])
        .annotate(count=Count('names'))
        .order_by('-created_at')
    )


async def task_create_channels(request):
    """Broadcast-channel creation task form. Same shape as task_create_groups
    but the pool dropdown is filtered to channel-compatible pools and the
    megagroup flag is fixed to False (runner forces broadcast)."""
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []

    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan. Avval akkauntlar ro'yxatidan tanlang.")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi (yoki boshqa foydalanuvchiga tegishli)")
        return redirect('accounts:dashboard')

    pools = await _list_pools_for_channel_task(user)

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_channels')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        try:
            pool_id = int(request.POST.get('pool_id') or 0)
            count_per = int(request.POST.get('count_per_account') or 1)
            delay_min = float(request.POST.get('delay_min_sec') or 30)
            delay_max = float(request.POST.get('delay_max_sec') or 90)
            concurrency = int(request.POST.get('concurrency') or 5)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato bor")
            return _back()

        if count_per < 1 or count_per > 50:
            messages.error(request, "Akkauntga kanallar soni 1-50 oralig'ida bo'lsin")
            return _back()
        if delay_min < 0 or delay_max < delay_min:
            messages.error(request, "Delay qiymatlari noto'g'ri (min ≤ max, min ≥ 0)")
            return _back()
        if concurrency < 1 or concurrency > 50:
            messages.error(request, "Parallel qiymati 1-50 oralig'ida bo'lsin")
            return _back()

        skip_inactive = request.POST.get('skip_inactive') == 'on'
        skip_spam = request.POST.get('skip_spam') == 'on'
        send_welcome = request.POST.get('send_welcome_message') == 'on'

        params = {
            'account_ids': account_ids,
            'count_per_account': count_per,
            'pool_id': pool_id,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': skip_inactive,
            'skip_spam': skip_spam,
            'send_welcome_message': send_welcome,
            # Runner forces this anyway, but pin it in params for auditability.
            'megagroup': False,
        }
        task = await Task.objects.acreate(
            kind='create_channels',
            owner=user,
            params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(
            request,
            f"Vazifa #{task.pk} navbatga qo'yildi. Worker ishlayotgan bo'lsa ijro boshlanadi.",
        )
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_channels.html', {
        'accounts': accounts,
        'pools': pools,
        'account_ids': account_ids,
    })


async def task_create_join_channel(request):
    """Bulk-join chats. Target list comes from a textarea (one per line)."""
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []

    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan. Avval akkauntlar ro'yxatidan tanlang.")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_join_channel')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        raw_targets = request.POST.get('targets') or ''
        targets = [ln.strip() for ln in raw_targets.splitlines() if ln.strip()]
        if not targets:
            messages.error(request, "Hech bo'lmaganda bitta chat kiriting")
            return _back()
        if len(targets) > 500:
            messages.error(request, "Bir safarda 500 tadan ortiq chat bo'lmasin")
            return _back()

        try:
            delay_min = float(request.POST.get('delay_min_sec') or 45)
            delay_max = float(request.POST.get('delay_max_sec') or 120)
            concurrency = int(request.POST.get('concurrency') or 5)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()

        if delay_min < 0 or delay_max < delay_min:
            messages.error(request, "Delay qiymatlari noto'g'ri")
            return _back()
        if concurrency < 1 or concurrency > 50:
            messages.error(request, "Parallel 1-50 oralig'ida bo'lsin")
            return _back()

        skip_inactive = request.POST.get('skip_inactive') == 'on'
        skip_spam = request.POST.get('skip_spam') == 'on'

        params = {
            'account_ids': account_ids,
            'targets': targets,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': skip_inactive,
            'skip_spam': skip_spam,
        }
        task = await Task.objects.acreate(
            kind='join_channel',
            owner=user,
            params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(
            request,
            f"Vazifa #{task.pk} navbatga qo'yildi ({len(accounts)} akkaunt × {len(targets)} chat).",
        )
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_join_channel.html', {
        'accounts': accounts,
        'account_ids': account_ids,
    })


async def task_create_leave_chats(request, kind):
    """Form for bulk-leaving non-admin groups/channels.

    `kind` is bound by the URL pattern: 'group' or 'channel'.
    """
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    if kind not in ('group', 'channel'):
        messages.error(request, "Noto'g'ri kategoriya")
        return redirect('accounts:dashboard')

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []

    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    url_name = 'jobs:task_create_leave_groups' if kind == 'group' else 'jobs:task_create_leave_channels'

    def _back():
        return redirect(
            f"{reverse(url_name)}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        try:
            delay_min = float(request.POST.get('delay_min_sec') or 2)
            delay_max = float(request.POST.get('delay_max_sec') or 6)
            concurrency = int(request.POST.get('concurrency') or 3)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
            max_chats_raw = (request.POST.get('max_chats') or '').strip()
            max_chats = int(max_chats_raw) if max_chats_raw else None
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()

        if delay_min < 0 or delay_max < delay_min:
            messages.error(request, "Delay qiymatlari noto'g'ri")
            return _back()
        if concurrency < 1 or concurrency > 20:
            messages.error(request, "Parallel 1-20 oralig'ida bo'lsin")
            return _back()
        if max_chats is not None and max_chats < 1:
            max_chats = None

        skip_inactive = request.POST.get('skip_inactive') == 'on'
        skip_spam = request.POST.get('skip_spam') == 'on'

        params = {
            'account_ids': account_ids,
            'kind': kind,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': skip_inactive,
            'skip_spam': skip_spam,
            'max_chats': max_chats,
        }
        task = await Task.objects.acreate(
            kind=f'leave_{kind}s',
            owner=user,
            params=params,
            **_parse_schedule(request.POST),
        )
        kind_label = 'guruh' if kind == 'group' else 'kanal'
        messages.success(
            request,
            f"Vazifa #{task.pk} navbatga qo'yildi — {len(accounts)} akkaunt admin emas {kind_label}lardan chiqadi.",
        )
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_leave_chats.html', {
        'accounts': accounts,
        'account_ids': account_ids,
        'kind': kind,
        'kind_label': 'Guruh' if kind == 'group' else 'Kanal',
    })


async def task_create_send_message(request):
    """Bulk-send a message from each selected account to a list of targets."""
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []
    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_send_message')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        message = (request.POST.get('message') or '').strip()
        raw_targets = request.POST.get('targets') or ''
        targets = [ln.strip() for ln in raw_targets.splitlines() if ln.strip()]
        if not message:
            messages.error(request, "Xabar matni bo'sh")
            return _back()
        if not targets:
            messages.error(request, "Targetlar ro'yxati bo'sh")
            return _back()
        if len(targets) > 500:
            messages.error(request, "Bir safarda 500 tadan ortiq target bo'lmasin")
            return _back()
        try:
            delay_min = float(request.POST.get('delay_min_sec') or 30)
            delay_max = float(request.POST.get('delay_max_sec') or 90)
            concurrency = int(request.POST.get('concurrency') or 3)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()
        if delay_min < 0 or delay_max < delay_min:
            messages.error(request, "Delay qiymatlari noto'g'ri")
            return _back()

        params = {
            'account_ids': account_ids,
            'targets': targets,
            'message': message,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(
            kind='send_message', owner=user, params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(request,
            f"Vazifa #{task.pk} navbatga qo'yildi ({len(accounts)} × {len(targets)} xabar).")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_send_message.html', {
        'accounts': accounts, 'account_ids': account_ids,
    })


async def task_create_update_profile(request):
    """Bulk-update Telegram profile fields (fixed values or random from a NamePool)."""
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []
    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    pools = await sync_to_async(list)(
        NamePool.objects.filter(owner=user).order_by('-created_at')
    )

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_update_profile')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        mode = request.POST.get('mode', 'fixed')
        if mode not in ('fixed', 'pool'):
            mode = 'fixed'

        try:
            concurrency = int(request.POST.get('concurrency') or 3)
            delay_min = float(request.POST.get('delay_min_sec') or 5)
            delay_max = float(request.POST.get('delay_max_sec') or 15)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()

        params = {
            'account_ids': account_ids,
            'mode': mode,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        if mode == 'fixed':
            for key in ('first_name', 'last_name', 'about', 'username'):
                val = request.POST.get(key, '').strip()
                # Empty string = leave unchanged. Use a separate "clear" checkbox
                # if a future version needs to actively clear a field.
                params[key] = val
        else:
            for key in ('first_name_pool_id', 'last_name_pool_id', 'username_pool_id'):
                pid = request.POST.get(key) or ''
                params[key] = int(pid) if pid.isdigit() else None

        any_field = (
            params.get('first_name') or params.get('last_name')
            or params.get('about') or params.get('username')
            or params.get('first_name_pool_id') or params.get('last_name_pool_id')
            or params.get('username_pool_id')
        )
        if not any_field:
            messages.error(request, "Hech bo'lmaganda bitta maydonni to'ldiring")
            return _back()

        task = await Task.objects.acreate(
            kind='update_profile', owner=user, params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(request, f"Vazifa #{task.pk} navbatga qo'yildi ({len(accounts)} akkaunt).")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_update_profile.html', {
        'accounts': accounts, 'account_ids': account_ids, 'pools': pools,
    })


async def task_create_view_stories(request):
    """Mark stories as seen + (optional) random reactions."""
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []
    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')
    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_view_stories')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        try:
            react_chance = float(request.POST.get('react_chance') or 0)
            max_peers = int(request.POST.get('max_peers') or 50)
            concurrency = int(request.POST.get('concurrency') or 3)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()
        if not (0 <= react_chance <= 1):
            messages.error(request, "react_chance 0..1 oralig'ida bo'lsin")
            return _back()

        params = {
            'account_ids': account_ids,
            'react_chance': react_chance,
            'max_peers': max_peers,
            'concurrency': concurrency,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(
            kind='view_stories', owner=user, params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(request, f"Vazifa #{task.pk} navbatga qo'yildi.")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_view_stories.html', {
        'accounts': accounts, 'account_ids': account_ids,
    })


async def task_create_mark_all_read(request):
    """Mark all unread dialogs as read for each selected account."""
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []
    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')
    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    if request.method == 'POST':
        try:
            max_dialogs = int(request.POST.get('max_dialogs') or 500)
            concurrency = int(request.POST.get('concurrency') or 3)
        except (ValueError, TypeError):
            max_dialogs, concurrency = 500, 3

        params = {
            'account_ids': account_ids,
            'max_dialogs': max_dialogs,
            'concurrency': concurrency,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(
            kind='mark_all_read', owner=user, params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(request, f"Vazifa #{task.pk} navbatga qo'yildi.")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_mark_all_read.html', {
        'accounts': accounts, 'account_ids': account_ids,
    })


async def task_create_set_2fa_password(request):
    """Set / change 2FA cloud password on each selected account."""
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []
    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')
    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_set_2fa_password')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        new_password = (request.POST.get('new_password') or '').strip()
        confirm = (request.POST.get('confirm_password') or '').strip()
        hint = (request.POST.get('hint') or '').strip()
        try:
            concurrency = int(request.POST.get('concurrency') or 2)
        except (ValueError, TypeError):
            concurrency = 2

        if not new_password:
            messages.error(request, "Yangi parol bo'sh")
            return _back()
        if new_password != confirm:
            messages.error(request, "Parollar mos kelmadi")
            return _back()
        if len(new_password) < 4:
            messages.error(request, "Parol kamida 4 belgili bo'lsin")
            return _back()

        params = {
            'account_ids': account_ids,
            'new_password': new_password,
            'hint': hint,
            'concurrency': concurrency,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(
            kind='set_2fa_password', owner=user, params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(request, f"Vazifa #{task.pk} navbatga qo'yildi.")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_set_2fa_password.html', {
        'accounts': accounts, 'account_ids': account_ids,
    })


async def task_create_boost_views(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []

    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Tanlangan akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_boost_views')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        raw_urls = request.POST.get('message_urls') or ''
        urls = [ln.strip() for ln in raw_urls.splitlines() if ln.strip()]
        if not urls:
            messages.error(request, "Hech bo'lmaganda bitta xabar URL kiriting")
            return _back()
        if len(urls) > 200:
            messages.error(request, "200 dan ortiq xabar URL bo'lmasin")
            return _back()

        try:
            rounds = int(request.POST.get('rounds') or 1)
            delay_min = float(request.POST.get('delay_min_sec') or 5)
            delay_max = float(request.POST.get('delay_max_sec') or 15)
            concurrency = int(request.POST.get('concurrency') or 10)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()

        if rounds < 1 or rounds > 100:
            messages.error(request, "Raundlar soni 1-100")
            return _back()
        if delay_min < 0 or delay_max < delay_min:
            messages.error(request, "Delay qiymatlari noto'g'ri")
            return _back()
        if concurrency < 1 or concurrency > 100:
            messages.error(request, "Parallel 1-100")
            return _back()

        params = {
            'account_ids': account_ids,
            'message_urls': urls,
            'rounds': rounds,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(kind='boost_views', owner=user, params=params, **_parse_schedule(request.POST))
        messages.success(
            request,
            f"Vazifa #{task.pk} navbatga qo'yildi ({len(accounts)}×{rounds} raund).",
        )
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_boost_views.html', {
        'accounts': accounts,
        'account_ids': account_ids,
    })


async def task_create_react_to_post(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []

    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_react_to_post')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        raw_urls = request.POST.get('message_urls') or ''
        urls = [ln.strip() for ln in raw_urls.splitlines() if ln.strip()]
        if not urls:
            messages.error(request, "Hech bo'lmaganda bitta xabar URL kiriting")
            return _back()

        raw_emojis = (request.POST.get('emojis') or '👍').strip()
        # Accept comma-separated, space-separated, or raw concatenated emojis.
        emojis = [e.strip() for e in raw_emojis.replace(',', ' ').split() if e.strip()]
        if not emojis:
            emojis = ['👍']

        try:
            probability = float(request.POST.get('probability') or 1.0)
            delay_min = float(request.POST.get('delay_min_sec') or 10)
            delay_max = float(request.POST.get('delay_max_sec') or 30)
            concurrency = int(request.POST.get('concurrency') or 5)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()

        if not (0 < probability <= 1.0):
            messages.error(request, "Ehtimollik 0 < p ≤ 1 oralig'ida")
            return _back()
        if delay_min < 0 or delay_max < delay_min:
            messages.error(request, "Delay qiymatlari noto'g'ri")
            return _back()

        params = {
            'account_ids': account_ids,
            'message_urls': urls,
            'emojis': emojis,
            'probability': probability,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(kind='react_to_post', owner=user, params=params, **_parse_schedule(request.POST))
        messages.success(request, f"Vazifa #{task.pk} navbatga qo'yildi.")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_react_to_post.html', {
        'accounts': accounts,
        'account_ids': account_ids,
    })


# ---------------------------------------------------------------------------
# Scripts — admin only
# ---------------------------------------------------------------------------

from .script_templates import STARTER_TEMPLATES

DEFAULT_SCRIPT_TEMPLATE = STARTER_TEMPLATES[0]['code']  # `get_me` — safest starter


@sync_to_async
def _list_scripts(user):
    return list(ScriptTemplate.objects.filter(owner=user).order_by('-updated_at'))


async def script_list(request):
    user, resp = await _require_superuser(request)
    if resp:
        return resp

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            name = (request.POST.get('name') or '').strip()
            template_key = request.POST.get('template_key') or ''
            if not name:
                messages.error(request, "Nom bo'sh")
            else:
                # Seed the new script from a starter template if one was picked.
                template = next(
                    (t for t in STARTER_TEMPLATES if t['key'] == template_key),
                    None,
                )
                code = template['code'] if template else DEFAULT_SCRIPT_TEMPLATE
                desc = template['description'] if template else ''
                script = await ScriptTemplate.objects.acreate(
                    owner=user, name=name, code=code, description=desc,
                )
                messages.success(request, f"'{script.name}' yaratildi")
                return redirect('jobs:script_detail', pk=script.pk)
        elif action == 'delete':
            pk = request.POST.get('pk')
            if pk and pk.isdigit():
                await ScriptTemplate.objects.filter(pk=int(pk), owner=user).adelete()
                messages.success(request, "Skript o'chirildi")
        return redirect('jobs:script_list')

    scripts = await _list_scripts(user)
    return await render_async(request, 'jobs/script_list.html', {
        'scripts': scripts,
        'starter_templates': STARTER_TEMPLATES,
    })


@sync_to_async
def _load_script(user, pk):
    return get_object_or_404(ScriptTemplate, pk=pk, owner=user)


async def script_detail(request, pk):
    user, resp = await _require_superuser(request)
    if resp:
        return resp

    script = await _load_script(user, pk)

    if request.method == 'POST':
        script.name = (request.POST.get('name') or script.name).strip()
        script.description = request.POST.get('description') or ''
        script.code = request.POST.get('code') or ''
        try:
            compile(script.code, f'<script:{script.pk}>', 'exec')
        except SyntaxError as e:
            messages.error(request, f"Syntax xato: qator {e.lineno}: {e.msg}")
        else:
            await script.asave()
            messages.success(request, "Skript saqlandi")
        return redirect('jobs:script_detail', pk=pk)

    return await render_async(request, 'jobs/script_detail.html', {'script': script})


@sync_to_async
def _filter_accounts(user, get_params):
    """
    Run AccountFilter against the user's accounts, mirroring the dashboard.
    Returns (filterset, list[Account]).
    """
    from accounts.filters import AccountFilter
    from django.db.models import Count

    qs = (
        Account.objects.filter(owner=user)
        .prefetch_related('tags')
        .annotate(
            groups_count=Count('groups', distinct=True),
            channels_count=Count('channels', distinct=True),
        )
        .order_by('-created_at')
    )
    filterset = AccountFilter(get_params, queryset=qs, user=user)
    _ = filterset.form  # force evaluation
    return filterset, list(filterset.qs)


async def task_create_run_script(request):
    user, resp = await _require_superuser(request)
    if resp:
        return resp

    # Seed selection: when arriving from the dashboard's bulk action, the
    # IDs come as ?account_ids=...&account_ids=... — pre-check those.
    seed_ids = set()
    for raw in request.GET.getlist('account_ids'):
        if raw.isdigit():
            seed_ids.add(int(raw))

    scripts = await _list_scripts(user)
    if not scripts:
        messages.warning(request, "Avval skript yarating")
        return redirect('jobs:script_list')

    if request.method == 'POST':
        # Account IDs come from the checkbox table (`name="selected"`).
        # Fall back to `account_ids` for legacy URL-only callers.
        raw_selected = request.POST.getlist('selected') or request.POST.getlist('account_ids')
        try:
            account_ids = [int(x) for x in raw_selected]
        except (TypeError, ValueError):
            account_ids = []

        # Preserve the user's filter so the redirect-back keeps the table.
        filter_qs = urlencode([(k, v) for k, v in request.GET.lists() for v in v]) if request.GET else ''

        def _back():
            url = reverse('jobs:task_create_run_script')
            return redirect(f"{url}?{filter_qs}" if filter_qs else url)

        if not account_ids:
            messages.error(request, "Hech bo'lmaganda 1 ta akkauntni belgilang")
            return _back()

        # Defense: only accept IDs that actually belong to this user.
        owned_ids = await sync_to_async(list)(
            Account.objects.filter(owner=user, id__in=account_ids).values_list('id', flat=True)
        )
        if not owned_ids:
            messages.error(request, "Tanlangan akkauntlar topilmadi")
            return _back()

        try:
            script_id = int(request.POST.get('script_id') or 0)
            delay_min = float(request.POST.get('delay_min_sec') or 15)
            delay_max = float(request.POST.get('delay_max_sec') or 45)
            concurrency = int(request.POST.get('concurrency') or 3)
            min_age = int(request.POST.get('min_account_age_minutes') or 30)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()

        raw_params = (request.POST.get('script_params_json') or '{}').strip()
        try:
            script_params = json.loads(raw_params) if raw_params else {}
            if not isinstance(script_params, dict):
                raise ValueError("JSON dict kutildi")
        except (json.JSONDecodeError, ValueError) as e:
            messages.error(request, f"Params JSON xato: {e}")
            return _back()

        if not await ScriptTemplate.objects.filter(pk=script_id, owner=user).aexists():
            messages.error(request, "Skript topilmadi")
            return _back()

        params = {
            'account_ids': owned_ids,
            'script_id': script_id,
            'script_params': script_params,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(
            kind='run_script', owner=user, params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(
            request,
            f"Vazifa #{task.pk} navbatga qo'yildi ({len(owned_ids)} akkauntda).",
        )
        return redirect('jobs:task_detail', pk=task.pk)

    # GET — show filter + checkbox table.
    filterset, accounts = await _filter_accounts(user, request.GET)

    # If the user came in with seed_ids but they don't all match the current
    # filter, expand the visible list to include them so nothing is silently
    # dropped after a filter change.
    visible_ids = {a.id for a in accounts}
    if seed_ids - visible_ids:
        extras = await sync_to_async(list)(
            Account.objects.filter(
                owner=user, id__in=(seed_ids - visible_ids),
            ).prefetch_related('tags')
        )
        accounts = list(extras) + accounts

    return await render_async(request, 'jobs/task_create_run_script.html', {
        'accounts': accounts,
        'seed_ids': seed_ids,
        'filterset': filterset,
        'scripts': scripts,
    })


async def task_create_account_warming(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []
    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_account_warming')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        try:
            duration = int(request.POST.get('duration_minutes') or 15)
            concurrency = int(request.POST.get('concurrency') or 3)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()
        intensity = request.POST.get('intensity') or 'medium'
        if intensity not in ('low', 'medium', 'high'):
            intensity = 'medium'
        if not (1 <= duration <= 480):
            messages.error(request, "Davomiylik 1-480 daqiqa oralig'ida")
            return _back()
        if not (1 <= concurrency <= 20):
            messages.error(request, "Parallel 1-20 oralig'ida")
            return _back()

        params = {
            'account_ids': account_ids,
            'duration_minutes': duration,
            'intensity': intensity,
            'concurrency': concurrency,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(
            kind='account_warming', owner=user, params=params,
            **_parse_schedule(request.POST),
        )
        messages.success(request, f"Vazifa #{task.pk} navbatga qo'yildi.")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_account_warming.html', {
        'accounts': accounts, 'account_ids': account_ids,
    })


async def task_create_vote_poll(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []
    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_vote_poll')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        poll_url = (request.POST.get('poll_url') or '').strip()
        if not poll_url:
            messages.error(request, "So'rovnoma URL kiriting")
            return _back()
        strategy = request.POST.get('strategy') or 'random'
        if strategy not in ('random', 'fixed'):
            strategy = 'random'
        try:
            option_index = int(request.POST.get('option_index') or 0)
            delay_min = float(request.POST.get('delay_min_sec') or 10)
            delay_max = float(request.POST.get('delay_max_sec') or 30)
            concurrency = int(request.POST.get('concurrency') or 5)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()

        params = {
            'account_ids': account_ids,
            'poll_url': poll_url,
            'strategy': strategy,
            'option_index': option_index,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(kind='vote_poll', owner=user, params=params, **_parse_schedule(request.POST))
        messages.success(request, f"Vazifa #{task.pk} navbatga qo'yildi.")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_vote_poll.html', {
        'accounts': accounts, 'account_ids': account_ids,
    })


async def task_create_press_start(request):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    raw_ids = request.GET.getlist('account_ids') or request.POST.getlist('account_ids')
    try:
        account_ids = [int(x) for x in raw_ids]
    except (TypeError, ValueError):
        account_ids = []
    if not account_ids:
        messages.error(request, "Akkauntlar tanlanmagan")
        return redirect('accounts:dashboard')

    accounts = await _load_accounts_for_task(user, account_ids)
    if not accounts:
        messages.error(request, "Akkauntlar topilmadi")
        return redirect('accounts:dashboard')

    def _back():
        return redirect(
            f"{reverse('jobs:task_create_press_start')}?{urlencode([('account_ids', i) for i in account_ids])}"
        )

    if request.method == 'POST':
        bot = (request.POST.get('bot_username') or '').strip().lstrip('@')
        if not bot:
            messages.error(request, "Bot username kiriting")
            return _back()
        start_param = (request.POST.get('start_param') or '').strip()

        # Optional per-account mapping: one per line, "<account_id>=<param>"
        per_account_params = {}
        raw_map = request.POST.get('per_account_params') or ''
        for line in raw_map.splitlines():
            line = line.strip()
            if not line or '=' not in line:
                continue
            aid, _, pv = line.partition('=')
            aid = aid.strip()
            if aid.isdigit():
                per_account_params[aid] = pv.strip()

        try:
            delay_min = float(request.POST.get('delay_min_sec') or 10)
            delay_max = float(request.POST.get('delay_max_sec') or 30)
            concurrency = int(request.POST.get('concurrency') or 5)
            min_age = int(request.POST.get('min_account_age_minutes') or 0)
        except (ValueError, TypeError):
            messages.error(request, "Parametrlarda xato")
            return _back()

        params = {
            'account_ids': account_ids,
            'bot_username': bot,
            'start_param': start_param,
            'per_account_params': per_account_params,
            'delay_min_sec': delay_min,
            'delay_max_sec': delay_max,
            'concurrency': concurrency,
            'min_account_age_minutes': min_age,
            'skip_inactive': request.POST.get('skip_inactive') == 'on',
            'skip_spam': request.POST.get('skip_spam') == 'on',
        }
        task = await Task.objects.acreate(kind='press_start', owner=user, params=params, **_parse_schedule(request.POST))
        messages.success(request, f"Vazifa #{task.pk} navbatga qo'yildi.")
        return redirect('jobs:task_detail', pk=task.pk)

    return await render_async(request, 'jobs/task_create_press_start.html', {
        'accounts': accounts, 'account_ids': account_ids,
    })


@sync_to_async
def _load_task(user, pk):
    return get_object_or_404(Task, pk=pk, owner=user)


async def task_detail(request, pk):
    user = await _require_login(request)
    if user is None:
        return _login_redirect(request)

    task = await _load_task(user, pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'cancel' and task.status in ('pending', 'running'):
            await Task.objects.filter(pk=pk, owner=user).aupdate(cancel_requested=True)
            messages.success(request, "Bekor qilish so'raldi — worker keyingi qadamda to'xtaydi")
        elif action == 'delete' and task.status != 'running':
            await Task.objects.filter(pk=pk, owner=user).adelete()
            messages.success(request, "Task o'chirildi")
            return redirect('jobs:task_list')
        return redirect('jobs:task_detail', pk=pk)

    return await render_async(request, 'jobs/task_detail.html', {'task': task})


# ---------------------------------------------------------------------------
# JSON endpoints for live polling
# ---------------------------------------------------------------------------

async def task_progress_json(request, pk):
    user = await _require_login(request)
    if user is None:
        return JsonResponse({'error': 'auth'}, status=401)

    task = await Task.objects.filter(pk=pk, owner=user).afirst()
    if task is None:
        return JsonResponse({'error': 'not_found'}, status=404)

    return JsonResponse({
        'id': task.pk,
        'status': task.status,
        'status_display': task.get_status_display(),
        'total': task.total,
        'done': task.done,
        'success_count': task.success_count,
        'error_count': task.error_count,
        'percent': task.percent,
        'elapsed_seconds': task.elapsed_seconds,
        'eta_seconds': task.eta_seconds,
        'error': task.error,
        'cancel_requested': task.cancel_requested,
        'is_finished': task.is_finished,
    })


@sync_to_async
def _fetch_events(task, after, level, account_id, limit):
    qs = TaskEvent.objects.filter(task=task).select_related('account')
    if after:
        qs = qs.filter(id__gt=after)
    if level:
        qs = qs.filter(level=level)
    if account_id:
        qs = qs.filter(account_id=account_id)
    return list(qs.order_by('id')[:limit])


async def task_events_json(request, pk):
    user = await _require_login(request)
    if user is None:
        return JsonResponse({'error': 'auth'}, status=401)

    task = await Task.objects.filter(pk=pk, owner=user).afirst()
    if task is None:
        return JsonResponse({'error': 'not_found'}, status=404)

    try:
        after = int(request.GET.get('after') or 0)
    except ValueError:
        after = 0
    level = request.GET.get('level') or ''
    account_id = request.GET.get('account_id') or ''
    try:
        limit = min(500, int(request.GET.get('limit') or 200))
    except ValueError:
        limit = 200

    events = await _fetch_events(
        task, after, level,
        int(account_id) if account_id.isdigit() else None,
        limit,
    )

    return JsonResponse({
        'events': [{
            'id': e.id,
            'level': e.level,
            'step': e.step,
            'message': e.message,
            'telegram_error': e.telegram_error,
            'account': str(e.account) if e.account else None,
            'account_id': e.account_id,
            'at': e.created_at.isoformat(),
        } for e in events]
    })
