"""
Task runners.

A runner takes a Task row (already marked `running` by the worker) and drives
the actual work. It:
  - reads params off the task
  - loads the target accounts / pool
  - dispatches per-account coroutines with a Semaphore to bound concurrency
  - writes TaskEvent rows for every meaningful step
  - updates Task.done / success_count / error_count atomically (F() expressions)
  - checks cancel_requested periodically

Per-account rules enforced here (see memory: feedback_session_safety):
  - warm-up gate: if account too fresh, skip
  - session_dead → mark inactive, don't retry
  - banned → mark inactive + is_spam, don't retry
  - FloodWait → sleep exact seconds and retry same item (bounded)
"""
import asyncio
import random
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.db.models import F
from django.utils import timezone
from telethon.errors import FloodWaitError

from accounts.models import Account
from accounts.services import get_client_for_account, consume_quota
from channels.models import Channel
from groups.models import Group
from .models import NamePool, RandomName, ScriptTemplate, Task, TaskEvent
from .services import (
    create_group_for_account,
    join_chat_for_account,
    boost_views_for_account,
    react_to_message_for_account,
    vote_poll_for_account,
    press_start_for_account,
)


# How many times to retry a single item on FloodWait before giving up.
MAX_FLOOD_RETRIES = 3
# If FloodWait asks for longer than this (seconds), treat as unrecoverable.
FLOOD_HARD_CAP = 3600


class TaskRunner:
    """Base class for runners. Subclasses implement `run()`."""

    def __init__(self, task: Task):
        self.task = task
        self.params = dict(task.params or {})

    # ---- logging -----------------------------------------------------------

    async def log(self, level, message, account=None, step='', telegram_error=''):
        await TaskEvent.objects.acreate(
            task=self.task,
            account=account,
            level=level,
            step=step,
            message=message[:4000],
            telegram_error=telegram_error[:100] if telegram_error else '',
        )

    # ---- task state --------------------------------------------------------

    async def update_progress(self, **fields):
        await Task.objects.filter(pk=self.task.pk).aupdate(**fields)

    async def incr_done(self, success=True):
        if success:
            await Task.objects.filter(pk=self.task.pk).aupdate(
                done=F('done') + 1,
                success_count=F('success_count') + 1,
            )
        else:
            await Task.objects.filter(pk=self.task.pk).aupdate(
                done=F('done') + 1,
                error_count=F('error_count') + 1,
            )
        await self._sample_eta_rate()

    async def _sample_eta_rate(self):
        """Fold a Δelapsed/Δdone sample into an EMA stored in task.stats.

        Why: cumulative `elapsed/done` is noisy with random sleep delays
        and concurrency bursts; eta_seconds prefers this smoothed rate.
        Read-modify-write may drop one sample under a race between two
        parallel completions — the next call's Δdone covers both items,
        so the EMA stays accurate.
        """
        snap = await Task.objects.filter(pk=self.task.pk).values(
            'stats', 'done', 'started_at',
        ).afirst()
        if not snap or not snap['started_at']:
            return
        elapsed = (timezone.now() - snap['started_at']).total_seconds()
        done = snap['done']
        stats = dict(snap['stats'] or {})
        last_elapsed = stats.get('_eta_last_elapsed')
        last_done = stats.get('_eta_last_done')
        if last_elapsed is not None and last_done is not None and done > last_done:
            d_elapsed = elapsed - last_elapsed
            d_done = done - last_done
            if d_elapsed > 0:
                instant_rate = d_elapsed / d_done
                prev = stats.get('_eta_rate_ema')
                alpha = 0.2
                stats['_eta_rate_ema'] = (
                    instant_rate if prev is None
                    else alpha * instant_rate + (1 - alpha) * prev
                )
        stats['_eta_last_elapsed'] = elapsed
        stats['_eta_last_done'] = done
        await Task.objects.filter(pk=self.task.pk).aupdate(stats=stats)

    async def is_cancelled(self):
        val = await Task.objects.filter(pk=self.task.pk).values_list(
            'cancel_requested', flat=True,
        ).afirst()
        return bool(val)

    async def cancellable_sleep(self, seconds):
        """Sleep that wakes early when the user clicks "Bekor qilish".

        Returns True if cancellation was observed — caller MUST stop work
        immediately. Polls cancel_requested every ~2s instead of blocking
        through the full duration; previously a 90s inter-item pause kept
        the runner unresponsive to cancellation for the whole window.
        """
        if seconds <= 0:
            return await self.is_cancelled()
        poll = 2.0
        elapsed = 0.0
        while elapsed < seconds:
            chunk = min(poll, seconds - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk
            if await self.is_cancelled():
                return True
        return False

    async def quota_ok(self, account):
        """
        Reserve one quota slot before an operation. When the task opted out
        (params['respect_quota']=False), passes unconditionally.

        Returns True when the operation may proceed. On denial, emits a
        warning log — callers should skip the work AND stop the account's
        remaining items so progress still advances.
        """
        if not self.params.get('respect_quota', True):
            return True
        allowed, remaining, limit = await consume_quota(account.pk)
        if not allowed:
            await self.log(
                'warning',
                f"Kunlik byudjet to'ldi ({limit} ops/kun) — akkaunt chetlab o'tildi",
                account=account, step='quota_exhausted',
            )
        return allowed

    # ---- main --------------------------------------------------------------

    async def run(self):
        raise NotImplementedError


class CreateGroupsRunner(TaskRunner):
    """Create N megagroups per selected account, with delays + concurrency.

    Subclass for broadcast channels by overriding `_chat_model` and
    `_default_megagroup` — see CreateChannelsRunner below.
    """

    # Which Django model to persist the created chat into.
    _chat_model = Group
    # Default for the `megagroup` param when the form didn't send it.
    _default_megagroup = True

    async def run(self):
        p = self.params
        account_ids = list(p.get('account_ids') or [])
        count_per_account = int(p.get('count_per_account', 1))
        pool_id = p.get('pool_id')
        delay_min = float(p.get('delay_min_sec', 30))
        delay_max = float(p.get('delay_max_sec', 90))
        concurrency = max(1, int(p.get('concurrency', 5)))
        min_age_minutes = int(p.get('min_account_age_minutes', 0))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))
        megagroup = bool(p.get('megagroup', self._default_megagroup))
        # Default ON: empty groups look automated, so post a tiny greeting
        # right after creation. Toggle off via the form to skip.
        send_welcome = bool(p.get('send_welcome_message', True))

        if delay_max < delay_min:
            delay_max = delay_min

        # --- Load target accounts (scoped to owner already in the view) ---
        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)

        accounts = await sync_to_async(list)(accounts_qs)

        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(
                status='failed',
                error="No eligible accounts",
                finished_at=timezone.now(),
            )
            return

        # --- Load pool + names ---
        pool = await NamePool.objects.filter(
            pk=pool_id, owner=self.task.owner,
        ).afirst()
        if pool is None:
            await self.log('error', "Nom pool topilmadi")
            await self.update_progress(
                status='failed',
                error="Pool not found",
                finished_at=timezone.now(),
            )
            return

        all_names = await sync_to_async(list)(
            RandomName.objects.filter(pool=pool).order_by('used_count', 'id')
        )
        if not all_names:
            await self.log('error', "Pool bo'sh — nomlar yo'q")
            await self.update_progress(
                status='failed',
                error="Pool is empty",
                finished_at=timezone.now(),
            )
            return

        # --- Announce ---
        total = len(accounts) * count_per_account
        await self.update_progress(total=total)
        await self.log(
            'info',
            f"{len(accounts)} ta akkaunt × {count_per_account} = {total} guruh yaratiladi. "
            f"Pause: {delay_min}-{delay_max}s, parallel: {concurrency}",
        )

        # --- Assign a unique batch of names to each account ---
        random.shuffle(all_names)
        name_plans = []
        cursor = 0
        for acc in accounts:
            need = count_per_account
            chunk = []
            while len(chunk) < need:
                slice_take = all_names[cursor:cursor + (need - len(chunk))]
                if not slice_take:
                    # Ran out — wrap around (allowing re-use across accounts).
                    cursor = 0
                    slice_take = all_names[:need - len(chunk)]
                    if not slice_take:
                        break
                chunk.extend(slice_take)
                cursor += len(slice_take)
            name_plans.append((acc, [n.text for n in chunk[:need]], [n.pk for n in chunk[:need]]))

        sem = asyncio.Semaphore(concurrency)

        async def process_account(account, titles, name_pks):
            async with sem:
                if await self.is_cancelled():
                    await self.log('warning', "Bekor qilingan — boshlanmadi",
                                   account=account, step='cancelled')
                    return

                await self.log('info', "Boshlandi", account=account, step='start')

                # Warm-up gate: account created in DB too recently?
                if min_age_minutes > 0:
                    age_min = (timezone.now() - account.created_at).total_seconds() / 60
                    if age_min < min_age_minutes:
                        await self.log(
                            'warning',
                            f"Akkaunt yangi ({int(age_min)} daq < {min_age_minutes} daq) — chetlab o'tildi",
                            account=account, step='warmup_skip',
                        )
                        for _ in titles:
                            await self.incr_done(success=False)
                        return

                if not account.session_string:
                    await self.log('error', "Sessiya string yo'q", account=account, step='no_session')
                    for _ in titles:
                        await self.incr_done(success=False)
                    return

                for idx, (title, name_pk) in enumerate(zip(titles, name_pks), start=1):
                    if await self.is_cancelled():
                        await self.log('warning', f"Bekor qilindi ({idx-1}/{len(titles)} bajarildi)",
                                       account=account, step='cancelled')
                        return

                    await self._create_one(account, title, name_pk, idx, len(titles), megagroup, send_welcome)

                    if idx < len(titles):
                        delay = random.uniform(delay_min, delay_max)
                        await self.log(
                            'info', f"{delay:.1f}s kutilmoqda…",
                            account=account, step='sleep',
                        )
                        if await self.cancellable_sleep(delay):
                            return

                await self.log('info', f"Yakunlandi ({len(titles)} urinish)",
                               account=account, step='finished')

        await asyncio.gather(
            *[process_account(acc, titles, pks) for acc, titles, pks in name_plans],
            return_exceptions=True,
        )

        # --- Finalize ---
        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")

    async def _create_one(self, account, title, name_pk, idx, total, megagroup, send_welcome=True):
        """Create one group, with FloodWait retry. Writes events + increments counters."""
        if not await self.quota_ok(account):
            await self.incr_done(success=False)
            raise _AccountStopped()
        flood_retries = 0
        while True:
            await self.log(
                'info', f"[{idx}/{total}] '{title}' yaratilmoqda",
                account=account, step='creating',
            )
            try:
                from .welcome import pick_welcome_message
                welcome = pick_welcome_message() if send_welcome else None
                result = await create_group_for_account(
                    account, title, megagroup=megagroup, welcome_message=welcome,
                )
            except FloodWaitError as e:
                wait = int(getattr(e, 'seconds', 0) or 0)
                if wait > FLOOD_HARD_CAP or flood_retries >= MAX_FLOOD_RETRIES:
                    await self.log(
                        'error',
                        f"FloodWait juda uzoq ({wait}s) — tashlab ketildi",
                        account=account, step='flood_wait_giveup',
                        telegram_error='FloodWaitError',
                    )
                    await self.incr_done(success=False)
                    return
                flood_retries += 1
                await self.log(
                    'warning',
                    f"FloodWait: {wait}s kutilmoqda (urinish {flood_retries}/{MAX_FLOOD_RETRIES})",
                    account=account, step='flood_wait',
                    telegram_error='FloodWaitError',
                )
                if await self.cancellable_sleep(wait + 1):
                    return
                continue

            if result['success']:
                try:
                    await self._chat_model.objects.acreate(
                        name=title,
                        telegram_id=result['telegram_id'],
                        invite_link=result.get('invite_link'),
                        owner=account,
                    )
                except Exception as e:
                    await self.log(
                        'warning',
                        f"Chat yaratildi, lekin DB ga yozishda xato: {e}",
                        account=account, step='db_save_error',
                    )
                await RandomName.objects.filter(pk=name_pk).aupdate(used_count=F('used_count') + 1)
                msg = f"✓ '{title}' yaratildi"
                if result.get('invite_link'):
                    msg += f" → {result['invite_link']}"
                if result.get('welcome_sent'):
                    msg += " · welcome xabar yuborildi"
                elif send_welcome:
                    msg += " · welcome yuborilmadi"
                await self.log('success', msg, account=account, step='created')
                await self.incr_done(success=True)
                return

            # Non-success result
            await self.log(
                'error',
                result.get('error') or "Noma'lum xato",
                account=account, step='create_failed',
                telegram_error=result.get('error_type', ''),
            )
            await self.incr_done(success=False)

            if result.get('stop_account'):
                await self.log(
                    'warning',
                    "Bu akkaunt uchun vazifa to'xtatildi",
                    account=account, step='account_stopped',
                )
                # Mark the rest of this account's items as failed so progress advances.
                remaining = total - idx
                for _ in range(remaining):
                    await self.incr_done(success=False)
                # Signal the outer loop to stop — raise a special exception
                raise _AccountStopped()
            return


class _AccountStopped(Exception):
    """Internal: raised to unwind the per-account loop when the session is dead."""


class CreateChannelsRunner(CreateGroupsRunner):
    """Create N broadcast channels per selected account.

    Shares all loop / retry / warm-up logic with CreateGroupsRunner. Only
    the target model (channels.Channel) and the default `megagroup` value
    differ — the service layer already supports both shapes via the
    `megagroup` argument in CreateChannelRequest.
    """
    _chat_model = Channel
    _default_megagroup = False


class JoinChannelRunner(TaskRunner):
    """Make each selected account join each target chat, with pauses.

    Params:
      account_ids (list[int])
      targets     (list[str])   @username / t.me/... / t.me/+hash
      delay_min_sec, delay_max_sec, concurrency
      min_account_age_minutes, skip_inactive, skip_spam
    """

    async def run(self):
        p = self.params
        account_ids = list(p.get('account_ids') or [])
        raw_targets = p.get('targets') or []
        delay_min = float(p.get('delay_min_sec', 45))
        delay_max = float(p.get('delay_max_sec', 120))
        concurrency = max(1, int(p.get('concurrency', 5)))
        min_age_minutes = int(p.get('min_account_age_minutes', 0))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        if delay_max < delay_min:
            delay_max = delay_min

        # Normalize targets: dedupe, preserve order, strip whitespace/blank lines
        seen = set()
        targets = []
        for t in raw_targets:
            s = (t or '').strip()
            if not s or s in seen:
                continue
            seen.add(s)
            targets.append(s)

        if not targets:
            await self.log('error', "Hech bir chat ko'rsatilmagan")
            await self.update_progress(status='failed', error="No targets",
                                       finished_at=timezone.now())
            return

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)

        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed', error="No eligible accounts",
                                       finished_at=timezone.now())
            return

        total = len(accounts) * len(targets)
        await self.update_progress(total=total)
        await self.log(
            'info',
            f"{len(accounts)} ta akkaunt × {len(targets)} ta chat = {total} qo'shilish. "
            f"Pause: {delay_min}-{delay_max}s, parallel: {concurrency}",
        )

        sem = asyncio.Semaphore(concurrency)

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return

                await self.log('info', "Boshlandi", account=account, step='start')

                if min_age_minutes > 0:
                    age_min = (timezone.now() - account.created_at).total_seconds() / 60
                    if age_min < min_age_minutes:
                        await self.log(
                            'warning',
                            f"Akkaunt yangi ({int(age_min)} daq) — chetlab o'tildi",
                            account=account, step='warmup_skip',
                        )
                        for _ in targets:
                            await self.incr_done(success=False)
                        return

                if not account.session_string:
                    await self.log('error', "Sessiya string yo'q", account=account, step='no_session')
                    for _ in targets:
                        await self.incr_done(success=False)
                    return

                joined = 0
                for idx, target in enumerate(targets, start=1):
                    if await self.is_cancelled():
                        return

                    try:
                        await self._join_one(account, target, idx, len(targets))
                        joined += 1  # attempted; success flag handled inside
                    except _AccountStopped:
                        remaining = len(targets) - idx
                        for _ in range(remaining):
                            await self.incr_done(success=False)
                        return

                    if idx < len(targets):
                        delay = random.uniform(delay_min, delay_max)
                        await self.log(
                            'info', f"{delay:.1f}s kutilmoqda…",
                            account=account, step='sleep',
                        )
                        if await self.cancellable_sleep(delay):
                            return

                await self.log('info', f"Yakunlandi ({joined}/{len(targets)} urinish)",
                               account=account, step='finished')

        await asyncio.gather(
            *[process_account(a) for a in accounts],
            return_exceptions=True,
        )

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")

    async def _join_one(self, account, target, idx, total):
        if not await self.quota_ok(account):
            await self.incr_done(success=False)
            raise _AccountStopped()
        flood_retries = 0
        while True:
            await self.log(
                'info', f"[{idx}/{total}] '{target}' ga qo'shilinmoqda",
                account=account, step='joining',
            )
            try:
                result = await join_chat_for_account(account, target)
            except FloodWaitError as e:
                wait = int(getattr(e, 'seconds', 0) or 0)
                if wait > FLOOD_HARD_CAP or flood_retries >= MAX_FLOOD_RETRIES:
                    await self.log(
                        'error', f"FloodWait juda uzoq ({wait}s) — tashlab ketildi",
                        account=account, step='flood_wait_giveup',
                        telegram_error='FloodWaitError',
                    )
                    await self.incr_done(success=False)
                    return
                flood_retries += 1
                await self.log(
                    'warning',
                    f"FloodWait: {wait}s (urinish {flood_retries}/{MAX_FLOOD_RETRIES})",
                    account=account, step='flood_wait',
                    telegram_error='FloodWaitError',
                )
                if await self.cancellable_sleep(wait + 1):
                    return
                continue

            if result['success']:
                if result.get('already_member'):
                    msg = f"✓ Allaqachon a'zo: {target}"
                    step = 'already_member'
                elif result.get('request_sent'):
                    title = result.get('chat_title') or target
                    msg = f"📨 So'rov yuborildi (admin tasdig'ini kutyapti): {title}"
                    step = 'request_sent'
                else:
                    title = result.get('chat_title') or target
                    msg = f"✓ Qo'shildi: {title}"
                    step = 'joined'
                await self.log('success', msg, account=account, step=step)
                await self.incr_done(success=True)
                return

            await self.log(
                'error', result.get('error') or "Noma'lum xato",
                account=account, step='join_failed',
                telegram_error=result.get('error_type', ''),
            )
            await self.incr_done(success=False)

            if result.get('stop_account'):
                await self.log(
                    'warning', "Bu akkaunt uchun vazifa to'xtatildi",
                    account=account, step='account_stopped',
                )
                raise _AccountStopped()
            return


class LeaveChatsRunner(TaskRunner):
    """Leave every group/channel where the account is not creator/admin.

    Params:
      account_ids (list[int])
      kind        ('group' | 'channel')   defaults to 'group'
      delay_min_sec, delay_max_sec        per-leave pause
      concurrency                          parallel accounts
      skip_inactive, skip_spam, min_account_age_minutes
      max_chats   (int | None)             cap chats per account (default unlimited)

    Progress total is the number of accounts (not chats — chat count is
    only known after enumerating). Each account contributes 1 to `done`
    when its enumeration+leave loop finishes.
    """

    async def run(self):
        from .services import (
            leave_non_admin_chats_for_account,
            leave_specific_chats_for_account,
        )

        p = self.params
        account_ids = list(p.get('account_ids') or [])
        # Optional per-account explicit list of chat IDs. When provided,
        # we skip the admin filter and only leave the listed chats.
        # `chat_ids_per_account` shape: {account_id: [chat_id, ...]}
        # `chat_ids` (flat list) is used when running across many accounts
        # and the same target list applies to each.
        chat_ids_per_account = p.get('chat_ids_per_account') or {}
        chat_ids_flat = p.get('chat_ids') or []
        kind = p.get('kind', 'group')
        if kind not in ('group', 'channel'):
            kind = 'group'
        delay_min = float(p.get('delay_min_sec', 2))
        delay_max = float(p.get('delay_max_sec', 6))
        concurrency = max(1, int(p.get('concurrency', 3)))
        min_age_minutes = int(p.get('min_account_age_minutes', 0))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))
        max_chats = p.get('max_chats')
        if max_chats is not None:
            try:
                max_chats = int(max_chats)
                if max_chats <= 0:
                    max_chats = None
            except (TypeError, ValueError):
                max_chats = None

        if delay_max < delay_min:
            delay_max = delay_min

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)

        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(
                status='failed', error="No eligible accounts",
                finished_at=timezone.now(),
            )
            return

        await self.update_progress(total=len(accounts))
        kind_label = 'guruh' if kind == 'group' else 'kanal'
        await self.log(
            'info',
            f"{len(accounts)} ta akkaunt × admin emas {kind_label}lardan chiqish. "
            f"Pause: {delay_min}-{delay_max}s, parallel: {concurrency}",
        )

        sem = asyncio.Semaphore(concurrency)

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return

                await self.log('info', "Boshlandi", account=account, step='start')

                if min_age_minutes > 0:
                    age_min = (timezone.now() - account.created_at).total_seconds() / 60
                    if age_min < min_age_minutes:
                        await self.log(
                            'warning',
                            f"Akkaunt yangi ({int(age_min)} daq) — chetlab o'tildi",
                            account=account, step='warmup_skip',
                        )
                        await self.incr_done(success=False)
                        return

                if not account.session_string:
                    await self.log('error', "Sessiya string yo'q",
                                   account=account, step='no_session')
                    await self.incr_done(success=False)
                    return

                if not await self.quota_ok(account):
                    await self.incr_done(success=False)
                    return

                try:
                    # Pick mode: explicit list of chat_ids vs admin-filter sweep
                    explicit = chat_ids_per_account.get(str(account.pk)) \
                        or chat_ids_per_account.get(account.pk) \
                        or chat_ids_flat
                    if explicit:
                        results = await leave_specific_chats_for_account(
                            account, explicit,
                            delay_min=delay_min, delay_max=delay_max,
                        )
                    else:
                        results = await leave_non_admin_chats_for_account(
                            account, kind=kind,
                            delay_min=delay_min, delay_max=delay_max,
                            max_chats=max_chats,
                        )
                except Exception as e:
                    await self.log(
                        'error', f"Kutilmagan xato: {e}",
                        account=account, step='unexpected_error',
                        telegram_error=type(e).__name__,
                    )
                    await self.incr_done(success=False)
                    return

                # Tally + log per-chat events
                left = kept = errors = 0
                for r in results:
                    if r['action'] == 'left':
                        left += 1
                        await self.log(
                            'success',
                            f"✓ chiqildi: {r['title']}",
                            account=account, step='left',
                        )
                    elif r['action'] == 'kept_admin':
                        kept += 1
                        # Don't spam the log with every admin chat — counter only
                    elif r['action'] == 'error':
                        errors += 1
                        await self.log(
                            'warning',
                            f"{r['title']}: {r['reason']}",
                            account=account, step='leave_failed',
                            telegram_error=r.get('error_type', ''),
                        )

                await self.log(
                    'info' if errors == 0 else 'warning',
                    f"Yakunlandi — {left} ta chiqildi, {kept} ta admin (saqlandi), "
                    f"{errors} ta xato",
                    account=account, step='finished',
                )
                await self.incr_done(success=(errors == 0))

        await asyncio.gather(
            *[process_account(acc) for acc in accounts],
            return_exceptions=True,
        )

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")


class BoostViewsRunner(TaskRunner):
    """Increment view counts on messages from each selected account.

    Each "pass" loops through message URLs for one account. Progress total
    = accounts × rounds (each round is one boost pass).

    Params:
      account_ids (list[int])
      message_urls (list[str])  t.me/<chan>/<id>  or  t.me/c/<int>/<id>
      rounds (int)              how many times each account re-views the set
      delay_min_sec, delay_max_sec, concurrency
      min_account_age_minutes, skip_inactive, skip_spam
    """

    async def run(self):
        p = self.params
        account_ids = list(p.get('account_ids') or [])
        raw_urls = p.get('message_urls') or []
        rounds = max(1, int(p.get('rounds', 1)))
        delay_min = float(p.get('delay_min_sec', 5))
        delay_max = float(p.get('delay_max_sec', 15))
        concurrency = max(1, int(p.get('concurrency', 10)))
        min_age_minutes = int(p.get('min_account_age_minutes', 0))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        if delay_max < delay_min:
            delay_max = delay_min

        urls = [(u or '').strip() for u in raw_urls]
        urls = [u for u in urls if u]
        if not urls:
            await self.log('error', "Xabar URL'lari yo'q")
            await self.update_progress(status='failed', error="No URLs",
                                       finished_at=timezone.now())
            return

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)

        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed', error="No eligible accounts",
                                       finished_at=timezone.now())
            return

        total = len(accounts) * rounds
        await self.update_progress(total=total)
        await self.log(
            'info',
            f"{len(accounts)} akkaunt × {rounds} raund × {len(urls)} xabar "
            f"= {len(accounts) * rounds * len(urls)} view boost. "
            f"Pause: {delay_min}-{delay_max}s, parallel: {concurrency}",
        )

        sem = asyncio.Semaphore(concurrency)

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return

                if min_age_minutes > 0:
                    age_min = (timezone.now() - account.created_at).total_seconds() / 60
                    if age_min < min_age_minutes:
                        await self.log(
                            'warning',
                            f"Akkaunt yangi ({int(age_min)} daq) — chetlab o'tildi",
                            account=account, step='warmup_skip',
                        )
                        for _ in range(rounds):
                            await self.incr_done(success=False)
                        return

                if not account.session_string:
                    await self.log('error', "Sessiya string yo'q", account=account, step='no_session')
                    for _ in range(rounds):
                        await self.incr_done(success=False)
                    return

                for r in range(1, rounds + 1):
                    if await self.is_cancelled():
                        return
                    try:
                        stop = await self._boost_one(account, urls, r, rounds)
                    except _AccountStopped:
                        remaining = rounds - r
                        for _ in range(remaining):
                            await self.incr_done(success=False)
                        return

                    if r < rounds:
                        delay = random.uniform(delay_min, delay_max)
                        await self.log(
                            'info', f"{delay:.1f}s kutilmoqda (raund {r}/{rounds})",
                            account=account, step='sleep',
                        )
                        if await self.cancellable_sleep(delay):
                            return

        await asyncio.gather(
            *[process_account(a) for a in accounts],
            return_exceptions=True,
        )

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")

    async def _boost_one(self, account, urls, round_idx, total_rounds):
        if not await self.quota_ok(account):
            await self.incr_done(success=False)
            raise _AccountStopped()
        flood_retries = 0
        while True:
            try:
                result = await boost_views_for_account(account, urls)
            except FloodWaitError as e:
                wait = int(getattr(e, 'seconds', 0) or 0)
                if wait > FLOOD_HARD_CAP or flood_retries >= MAX_FLOOD_RETRIES:
                    await self.log(
                        'error', f"FloodWait juda uzoq ({wait}s)",
                        account=account, step='flood_wait_giveup',
                        telegram_error='FloodWaitError',
                    )
                    await self.incr_done(success=False)
                    return
                flood_retries += 1
                await self.log(
                    'warning',
                    f"FloodWait: {wait}s (urinish {flood_retries}/{MAX_FLOOD_RETRIES})",
                    account=account, step='flood_wait',
                    telegram_error='FloodWaitError',
                )
                if await self.cancellable_sleep(wait + 1):
                    return
                continue

            if result['success']:
                viewed = result.get('viewed_count', 0)
                failed = result.get('failed_targets') or []
                msg = f"✓ [R{round_idx}/{total_rounds}] {viewed} ta xabar ko'rildi"
                if failed:
                    msg += f", {len(failed)} ta muvaffaqiyatsiz"
                await self.log('success', msg, account=account, step='boosted')
                for url, reason in failed[:5]:  # limit noise
                    await self.log('warning', f"  • {url}: {reason}",
                                   account=account, step='target_failed')
                await self.incr_done(success=True)
                return

            await self.log(
                'error', result.get('error') or "Noma'lum xato",
                account=account, step='boost_failed',
                telegram_error=result.get('error_type', ''),
            )
            await self.incr_done(success=False)

            if result.get('stop_account'):
                await self.log('warning', "Bu akkaunt uchun vazifa to'xtatildi",
                               account=account, step='account_stopped')
                raise _AccountStopped()
            return


class ReactToPostRunner(TaskRunner):
    """Each account sends a random reaction to each message URL.

    Params:
      account_ids (list[int])
      message_urls (list[str])
      emojis (list[str])            emoji pool; runner picks 1 per action
      probability (float 0-1)       per-action skip roll (1.0 = always react)
      delay_min_sec, delay_max_sec, concurrency
      min_account_age_minutes, skip_inactive, skip_spam
    """

    async def run(self):
        p = self.params
        account_ids = list(p.get('account_ids') or [])
        raw_urls = p.get('message_urls') or []
        emojis = [e for e in (p.get('emojis') or ['👍']) if e]
        probability = float(p.get('probability', 1.0))
        delay_min = float(p.get('delay_min_sec', 10))
        delay_max = float(p.get('delay_max_sec', 30))
        concurrency = max(1, int(p.get('concurrency', 5)))
        min_age_minutes = int(p.get('min_account_age_minutes', 0))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        if delay_max < delay_min:
            delay_max = delay_min
        probability = max(0.0, min(1.0, probability))

        urls = [u.strip() for u in raw_urls if u and u.strip()]
        if not urls:
            await self.log('error', "Xabar URL'lari yo'q")
            await self.update_progress(status='failed', error="No URLs",
                                       finished_at=timezone.now())
            return
        if not emojis:
            emojis = ['👍']

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)
        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Akkaunt topilmadi")
            await self.update_progress(status='failed', error="No accounts",
                                       finished_at=timezone.now())
            return

        total = len(accounts) * len(urls)
        await self.update_progress(total=total)
        await self.log(
            'info',
            f"{len(accounts)} akkaunt × {len(urls)} xabar = {total} reaksiya. "
            f"Emojilar: {' '.join(emojis)}, ehtimollik: {probability:.0%}",
        )

        sem = asyncio.Semaphore(concurrency)

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return

                if min_age_minutes > 0:
                    age_min = (timezone.now() - account.created_at).total_seconds() / 60
                    if age_min < min_age_minutes:
                        await self.log('warning',
                            f"Akkaunt yangi ({int(age_min)} daq) — chetlab o'tildi",
                            account=account, step='warmup_skip')
                        for _ in urls:
                            await self.incr_done(success=False)
                        return

                if not account.session_string:
                    await self.log('error', "Sessiya yo'q", account=account, step='no_session')
                    for _ in urls:
                        await self.incr_done(success=False)
                    return

                for idx, url in enumerate(urls, start=1):
                    if await self.is_cancelled():
                        return
                    # Per-action probability roll — some accounts skip
                    # individual messages to look less uniform.
                    if random.random() > probability:
                        await self.log('info',
                            f"[{idx}/{len(urls)}] o'tkazib yuborildi (ehtimollik)",
                            account=account, step='prob_skip')
                        await self.incr_done(success=True)
                        continue
                    try:
                        await self._react_one(account, url, emojis, idx, len(urls))
                    except _AccountStopped:
                        remaining = len(urls) - idx
                        for _ in range(remaining):
                            await self.incr_done(success=False)
                        return

                    if idx < len(urls):
                        delay = random.uniform(delay_min, delay_max)
                        await self.log('info', f"{delay:.1f}s pauza",
                                       account=account, step='sleep')
                        if await self.cancellable_sleep(delay):
                            return

        await asyncio.gather(*[process_account(a) for a in accounts], return_exceptions=True)

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")

    async def _react_one(self, account, url, emojis, idx, total):
        if not await self.quota_ok(account):
            await self.incr_done(success=False)
            raise _AccountStopped()
        flood_retries = 0
        while True:
            try:
                result = await react_to_message_for_account(account, url, emojis)
            except FloodWaitError as e:
                wait = int(getattr(e, 'seconds', 0) or 0)
                if wait > FLOOD_HARD_CAP or flood_retries >= MAX_FLOOD_RETRIES:
                    await self.log('error', f"FloodWait {wait}s — tashlandi",
                                   account=account, step='flood_wait_giveup',
                                   telegram_error='FloodWaitError')
                    await self.incr_done(success=False)
                    return
                flood_retries += 1
                await self.log('warning', f"FloodWait {wait}s",
                               account=account, step='flood_wait',
                               telegram_error='FloodWaitError')
                if await self.cancellable_sleep(wait + 1):
                    return
                continue

            if result['success']:
                emoji = result.get('emoji') or '—'
                note = " (allaqachon)" if result.get('already_reacted') else ""
                await self.log('success',
                    f"✓ [{idx}/{total}] {emoji}{note} → {url}",
                    account=account, step='reacted')
                await self.incr_done(success=True)
                return

            await self.log('error',
                result.get('error') or "Noma'lum xato",
                account=account, step='react_failed',
                telegram_error=result.get('error_type', ''))
            await self.incr_done(success=False)

            if result.get('stop_account'):
                await self.log('warning', "Akkaunt uchun to'xtatildi",
                               account=account, step='account_stopped')
                raise _AccountStopped()
            return


class _SimplePerAccountRunner(TaskRunner):
    """Shared skeleton for runners that do 1 operation per account (vote, /start, etc.).

    Subclasses implement `_do_one(account)` returning the same result dict
    shape as the services layer. Total is always len(accounts).
    """

    _label = "vazifa"

    async def _do_one(self, account):
        raise NotImplementedError

    async def _load_accounts(self):
        p = self.params
        account_ids = list(p.get('account_ids') or [])
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))
        qs = Account.objects.filter(id__in=account_ids, owner=self.task.owner)
        if skip_inactive:
            qs = qs.filter(is_active=True)
        if skip_spam:
            qs = qs.filter(is_spam=False)
        return await sync_to_async(list)(qs.select_related('device_setting'))

    async def run(self):
        p = self.params
        delay_min = float(p.get('delay_min_sec', 10))
        delay_max = float(p.get('delay_max_sec', 30))
        concurrency = max(1, int(p.get('concurrency', 5)))
        min_age_minutes = int(p.get('min_account_age_minutes', 0))
        if delay_max < delay_min:
            delay_max = delay_min

        accounts = await self._load_accounts()
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt yo'q")
            await self.update_progress(status='failed', error="No accounts",
                                       finished_at=timezone.now())
            return

        await self.update_progress(total=len(accounts))
        await self.log(
            'info',
            f"{len(accounts)} akkauntda '{self._label}' bajariladi. "
            f"Pause: {delay_min}-{delay_max}s, parallel: {concurrency}",
        )

        sem = asyncio.Semaphore(concurrency)

        async def worker(account, order):
            async with sem:
                if await self.is_cancelled():
                    return
                # Stagger the start slightly so all workers don't fire at t=0
                if order > 0:
                    if await self.cancellable_sleep(random.uniform(delay_min, delay_max)):
                        return

                if min_age_minutes > 0:
                    age_min = (timezone.now() - account.created_at).total_seconds() / 60
                    if age_min < min_age_minutes:
                        await self.log('warning',
                            f"Akkaunt yangi ({int(age_min)} daq) — chetlab o'tildi",
                            account=account, step='warmup_skip')
                        await self.incr_done(success=False)
                        return

                if not account.session_string:
                    await self.log('error', "Sessiya yo'q",
                                   account=account, step='no_session')
                    await self.incr_done(success=False)
                    return

                if not await self.quota_ok(account):
                    await self.incr_done(success=False)
                    return

                flood_retries = 0
                while True:
                    try:
                        result = await self._do_one(account)
                    except FloodWaitError as e:
                        wait = int(getattr(e, 'seconds', 0) or 0)
                        if wait > FLOOD_HARD_CAP or flood_retries >= MAX_FLOOD_RETRIES:
                            await self.log('error',
                                f"FloodWait {wait}s — tashlandi",
                                account=account, step='flood_wait_giveup',
                                telegram_error='FloodWaitError')
                            await self.incr_done(success=False)
                            return
                        flood_retries += 1
                        await self.log('warning', f"FloodWait {wait}s",
                                       account=account, step='flood_wait',
                                       telegram_error='FloodWaitError')
                        if await self.cancellable_sleep(wait + 1):
                            return
                        continue

                    if result['success']:
                        await self._log_success(account, result)
                        await self.incr_done(success=True)
                    else:
                        await self.log('error',
                            result.get('error') or "Noma'lum xato",
                            account=account, step='failed',
                            telegram_error=result.get('error_type', ''))
                        await self.incr_done(success=False)
                    return

        await asyncio.gather(
            *[worker(a, i) for i, a in enumerate(accounts)],
            return_exceptions=True,
        )

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")

    async def _log_success(self, account, result):
        """Subclasses override to emit a more specific success line."""
        await self.log('success', "✓ Muvaffaqiyat",
                       account=account, step='done')


class VotePollRunner(_SimplePerAccountRunner):
    """Each account votes on the given poll message.

    Params:
      account_ids (list[int])
      poll_url (str)              t.me/<chan>/<id>
      strategy ('random'|'fixed') default 'random'
      option_index (int)          only used when strategy='fixed'
      delay_min_sec, delay_max_sec, concurrency
      min_account_age_minutes, skip_inactive, skip_spam
    """

    _label = "ovoz berish"

    async def _do_one(self, account):
        p = self.params
        return await vote_poll_for_account(
            account,
            poll_url=p.get('poll_url', ''),
            strategy=p.get('strategy', 'random'),
            option_index=int(p.get('option_index', 0)),
        )

    async def _log_success(self, account, result):
        idx = result.get('chosen_index')
        text = result.get('chosen_text') or '(matnsiz)'
        already = " (allaqachon)" if result.get('already_voted') else ""
        await self.log('success',
            f"✓ Ovoz berildi: #{idx} — {text!r}{already}",
            account=account, step='voted')


class PressStartRunner(_SimplePerAccountRunner):
    """Each account presses /start on the given bot, optionally with a referral code.

    `start_param` can be:
      - a plain string: shared by all accounts
      - a JSON dict {account_id: param}: per-account overrides
      - empty: plain /start message
    """

    _label = "/start bosish"

    async def _do_one(self, account):
        p = self.params
        bot = p.get('bot_username', '')
        start_param = p.get('start_param', '')
        # Per-account override map (optional).
        per_account = p.get('per_account_params') or {}
        if per_account:
            start_param = per_account.get(str(account.pk), start_param)
        return await press_start_for_account(
            account, bot_username=bot, start_param=start_param,
        )

    async def _log_success(self, account, result):
        bot = result.get('bot')
        sp = result.get('start_param') or ''
        silent = " (bot javob bermadi)" if result.get('bot_silent') else ""
        param_note = f" param={sp!r}" if sp else ""
        await self.log('success',
            f"✓ /start bosildi: @{bot}{param_note}{silent}",
            account=account, step='started')


class RunScriptRunner(TaskRunner):
    """
    Executes a user-authored `async def main(client, account, params)`
    across the selected accounts.

    SECURITY: Arbitrary Python execution. Even though the views gate on
    `is_superuser`, the runner re-checks task.owner.is_superuser and
    refuses to proceed otherwise. Never remove this check.

    Params:
      account_ids (list[int])
      script_id (int)            ScriptTemplate pk
      script_params (dict)       passed through to the user function
      delay_min_sec, delay_max_sec, concurrency
      min_account_age_minutes, skip_inactive, skip_spam
    """

    async def run(self):
        p = self.params
        account_ids = list(p.get('account_ids') or [])
        script_id = p.get('script_id')
        script_params = p.get('script_params') or {}
        delay_min = float(p.get('delay_min_sec', 15))
        delay_max = float(p.get('delay_max_sec', 45))
        concurrency = max(1, int(p.get('concurrency', 3)))
        min_age_minutes = int(p.get('min_account_age_minutes', 30))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        if delay_max < delay_min:
            delay_max = delay_min

        # Defense-in-depth superuser check. The view already gates this.
        owner = await sync_to_async(lambda t: t.owner)(self.task)
        is_super = await sync_to_async(lambda u: u.is_superuser)(owner)
        if not is_super:
            await self.log('error',
                "Skript ishga tushirish faqat adminlarga ruxsat etilgan",
                step='permission_denied')
            await self.update_progress(status='failed',
                error="Admin only", finished_at=timezone.now())
            return

        script = await ScriptTemplate.objects.filter(pk=script_id).afirst()
        if script is None:
            await self.log('error', "Skript topilmadi")
            await self.update_progress(status='failed',
                error="Script not found", finished_at=timezone.now())
            return

        # Compile once — runtime errors only surface per-account.
        try:
            compiled = compile(script.code, f'<script:{script.pk}>', 'exec')
            ns = {}
            exec(compiled, ns)
        except Exception as e:
            await self.log('error', f"Skript kompilyatsiyasida xato: {e!r}")
            await self.update_progress(status='failed',
                error=f"Compile error: {e}", finished_at=timezone.now())
            return

        main_fn = ns.get('main')
        if not (callable(main_fn) and asyncio.iscoroutinefunction(main_fn)):
            await self.log('error',
                "Skriptda `async def main(client, account, params)` aniqlanmagan")
            await self.update_progress(status='failed',
                error="No async main()", finished_at=timezone.now())
            return

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)
        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed',
                error="No accounts", finished_at=timezone.now())
            return

        await self.update_progress(total=len(accounts))
        await self.log(
            'info',
            f"Skript '{script.name}' — {len(accounts)} akkauntda ishga tushiriladi. "
            f"Parallel: {concurrency}, pause: {delay_min}-{delay_max}s",
        )

        sem = asyncio.Semaphore(concurrency)

        async def worker(account, order):
            async with sem:
                if await self.is_cancelled():
                    return
                if order > 0:
                    if await self.cancellable_sleep(random.uniform(delay_min, delay_max)):
                        return

                if min_age_minutes > 0:
                    age_min = (timezone.now() - account.created_at).total_seconds() / 60
                    if age_min < min_age_minutes:
                        await self.log('warning',
                            f"Akkaunt yangi ({int(age_min)} daq) — chetlab",
                            account=account, step='warmup_skip')
                        await self.incr_done(success=False)
                        return

                if not account.session_string:
                    await self.log('error', "Sessiya yo'q",
                                   account=account, step='no_session')
                    await self.incr_done(success=False)
                    return

                client = None
                try:
                    client = await get_client_for_account(account)
                except Exception as e:
                    await self.log('error', f"Ulanib bo'lmadi: {e!r}",
                                   account=account, step='connect_failed',
                                   telegram_error=type(e).__name__)
                    await self.incr_done(success=False)
                    return

                try:
                    await self.log('info', "Skript boshlanmoqda",
                                   account=account, step='script_start')
                    result = await main_fn(client, account, dict(script_params))
                    summary = repr(result)[:500] if result is not None else ""
                    await self.log('success',
                        f"✓ Skript tugadi" + (f": {summary}" if summary else ""),
                        account=account, step='script_done')
                    await self.incr_done(success=True)
                except FloodWaitError as e:
                    wait = int(getattr(e, 'seconds', 0) or 0)
                    await self.log('warning', f"Skript FloodWait {wait}s",
                                   account=account, step='flood_wait',
                                   telegram_error='FloodWaitError')
                    await self.incr_done(success=False)
                except Exception as e:
                    await self.log('error',
                        f"Skriptda istisno: {type(e).__name__}: {e}",
                        account=account, step='script_error',
                        telegram_error=type(e).__name__)
                    await self.incr_done(success=False)
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

        await asyncio.gather(
            *[worker(a, i) for i, a in enumerate(accounts)],
            return_exceptions=True,
        )

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")


class AccountWarmingRunner(TaskRunner):
    """
    Organic-looking activity for cold accounts. For a bounded duration, each
    account fetches dialogs and reads random messages with human-ish pauses.

    Unlike other runners this is time-bounded (not count-bounded) — progress
    ticks once per account when its warming window completes. Read-only, so
    quota is not consumed.

    Params:
      account_ids (list[int])
      duration_minutes (int)         how long to warm each account
      intensity ('low'|'medium'|'high')  pause range between reads
      concurrency (int)              parallel account warmers
      skip_inactive, skip_spam
    """

    INTENSITY = {
        'low':    (40, 90),
        'medium': (20, 60),
        'high':   (8, 25),
    }

    async def run(self):
        p = self.params
        account_ids = list(p.get('account_ids') or [])
        duration_min = max(1, int(p.get('duration_minutes', 15)))
        intensity = p.get('intensity', 'medium')
        concurrency = max(1, int(p.get('concurrency', 3)))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        pause_min, pause_max = self.INTENSITY.get(intensity, self.INTENSITY['medium'])

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting', 'proxy')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)
        accounts = await sync_to_async(list)(accounts_qs)

        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed', error="No accounts",
                                       finished_at=timezone.now())
            return

        await self.update_progress(total=len(accounts))
        await self.log(
            'info',
            f"{len(accounts)} akkaunt × {duration_min} daq warming "
            f"(intensivlik: {intensity}, pause {pause_min}-{pause_max}s)",
        )

        sem = asyncio.Semaphore(concurrency)

        async def warm_one(account):
            async with sem:
                if await self.is_cancelled():
                    return

                if not account.session_string:
                    await self.log('error', "Sessiya string yo'q",
                                   account=account, step='no_session')
                    await self.incr_done(success=False)
                    return

                try:
                    client = await get_client_for_account(account)
                except Exception as e:
                    await self.log('error', f"Ulanib bo'lmadi: {e!r}",
                                   account=account, step='connect_failed',
                                   telegram_error=type(e).__name__)
                    await self.incr_done(success=False)
                    return

                ops_done = 0
                try:
                    me = await client.get_me()
                    if not me:
                        await self.log('error', "Sessiya yaroqsiz",
                                       account=account, step='no_session')
                        await self.incr_done(success=False)
                        return

                    await self.log('info', "Warming boshlandi",
                                   account=account, step='warming_start')

                    try:
                        dialogs = await client.get_dialogs(limit=50)
                    except FloodWaitError as e:
                        await self.log('warning',
                            f"Dialoglarni olishda FloodWait {e.seconds}s",
                            account=account, step='flood_wait',
                            telegram_error='FloodWaitError')
                        if await self.cancellable_sleep(int(e.seconds) + 1):
                            return
                        dialogs = []
                    ops_done += 1

                    if not dialogs:
                        await self.log('warning',
                            "Dialog yo'q — yangi akkaunt, faqat get_me bilan warming",
                            account=account, step='no_dialogs')

                    end_time = timezone.now() + timedelta(minutes=duration_min)

                    while timezone.now() < end_time:
                        if await self.is_cancelled():
                            break

                        if dialogs:
                            dialog = random.choice(dialogs)
                            entity = getattr(dialog, 'entity', dialog)
                            try:
                                await client.get_messages(entity, limit=random.randint(5, 20))
                                ops_done += 1
                            except FloodWaitError as e:
                                wait = int(e.seconds)
                                await self.log('warning',
                                    f"FloodWait {wait}s",
                                    account=account, step='flood_wait',
                                    telegram_error='FloodWaitError')
                                if await self.cancellable_sleep(wait + 1):
                                    return
                                continue
                            except Exception:
                                # Dialog might have become inaccessible — skip silently
                                pass

                            if ops_done % 10 == 0:
                                await self.log('info',
                                    f"… {ops_done} o'qish",
                                    account=account, step='reading')

                        pause = random.uniform(pause_min, pause_max)
                        if await self.cancellable_sleep(pause):
                            return

                    await self.log('success',
                        f"✓ Warming yakunlandi: {ops_done} o'qish",
                        account=account, step='warming_done')
                    await self.incr_done(success=True)

                except SESSION_DEAD_EXCEPTIONS_IMPORTED as e:
                    from .services import _mark_session_dead
                    await _mark_session_dead(account.pk)
                    await self.log('error', "Sessiya chiqarib yuborilgan",
                                   account=account, step='session_dead',
                                   telegram_error=type(e).__name__)
                    await self.incr_done(success=False)
                except ACCOUNT_BANNED_EXCEPTIONS_IMPORTED as e:
                    from .services import _mark_account_banned
                    await _mark_account_banned(account.pk)
                    await self.log('error', "Akkaunt bloklangan",
                                   account=account, step='banned',
                                   telegram_error=type(e).__name__)
                    await self.incr_done(success=False)
                except Exception as e:
                    await self.log('error', f"Warming xato: {e!r}",
                                   account=account, step='warming_error',
                                   telegram_error=type(e).__name__)
                    await self.incr_done(success=False)
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

        await asyncio.gather(*[warm_one(a) for a in accounts], return_exceptions=True)

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Warming vazifasi yakunlandi")


# Re-export fatal-exception tuples for AccountWarmingRunner (single-source-of-truth
# lives in services.py — we import them here so a new runner doesn't re-derive them).
from .services import (
    SESSION_DEAD_EXCEPTIONS as SESSION_DEAD_EXCEPTIONS_IMPORTED,
    ACCOUNT_BANNED_EXCEPTIONS as ACCOUNT_BANNED_EXCEPTIONS_IMPORTED,
)


# Map task.kind → runner class. The worker uses this.
class SendMessageRunner(TaskRunner):
    """Send a message from each account to each target.

    Params:
      account_ids   list[int]
      targets       list[str]   @user / t.me/... per line
      message       str         body text (max 4096)
      delay_min_sec, delay_max_sec, concurrency
      skip_inactive, skip_spam, min_account_age_minutes
    """

    async def run(self):
        from .services import send_message_for_account

        p = self.params
        account_ids = list(p.get('account_ids') or [])
        raw_targets = p.get('targets') or []
        message = (p.get('message') or '').strip()
        delay_min = float(p.get('delay_min_sec', 30))
        delay_max = float(p.get('delay_max_sec', 90))
        concurrency = max(1, int(p.get('concurrency', 3)))
        min_age_minutes = int(p.get('min_account_age_minutes', 0))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        if delay_max < delay_min:
            delay_max = delay_min

        seen, targets = set(), []
        for t in raw_targets:
            s = (t or '').strip()
            if s and s not in seen:
                seen.add(s)
                targets.append(s)

        if not message:
            await self.log('error', "Xabar matni bo'sh")
            await self.update_progress(status='failed', error="Empty message",
                                       finished_at=timezone.now())
            return
        if not targets:
            await self.log('error', "Target ro'yxati bo'sh")
            await self.update_progress(status='failed', error="No targets",
                                       finished_at=timezone.now())
            return

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)
        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed', error="No accounts",
                                       finished_at=timezone.now())
            return

        total = len(accounts) * len(targets)
        await self.update_progress(total=total)
        await self.log('info',
            f"{len(accounts)} ta akkaunt × {len(targets)} ta target = {total} xabar")

        sem = asyncio.Semaphore(concurrency)

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return
                if min_age_minutes > 0:
                    age_min = (timezone.now() - account.created_at).total_seconds() / 60
                    if age_min < min_age_minutes:
                        await self.log('warning',
                            f"Akkaunt yangi ({int(age_min)} daq) — chetlab o'tildi",
                            account=account, step='warmup_skip')
                        for _ in targets:
                            await self.incr_done(success=False)
                        return
                if not account.session_string:
                    await self.log('error', "Sessiya yo'q", account=account, step='no_session')
                    for _ in targets:
                        await self.incr_done(success=False)
                    return

                for idx, target in enumerate(targets, start=1):
                    if await self.is_cancelled():
                        return
                    if not await self.quota_ok(account):
                        await self.incr_done(success=False)
                        continue

                    flood_retries = 0
                    while True:
                        try:
                            result = await send_message_for_account(account, target, message)
                        except FloodWaitError as e:
                            wait = int(getattr(e, 'seconds', 0) or 0)
                            if wait > FLOOD_HARD_CAP or flood_retries >= MAX_FLOOD_RETRIES:
                                await self.log('error',
                                    f"FloodWait juda uzoq ({wait}s) — tashlandi",
                                    account=account, step='flood_giveup',
                                    telegram_error='FloodWaitError')
                                await self.incr_done(success=False)
                                break
                            flood_retries += 1
                            if await self.cancellable_sleep(wait + 1):
                                return
                            continue

                        if result['success']:
                            await self.log('success',
                                f"✓ {target} ga yuborildi",
                                account=account, step='sent')
                            await self.incr_done(success=True)
                        else:
                            await self.log('error',
                                f"{target}: {result['error']}",
                                account=account, step='send_failed',
                                telegram_error=result.get('error_type', ''))
                            await self.incr_done(success=False)
                            if result.get('stop_account'):
                                remaining = len(targets) - idx
                                for _ in range(remaining):
                                    await self.incr_done(success=False)
                                return
                        break

                    if idx < len(targets):
                        if await self.cancellable_sleep(random.uniform(delay_min, delay_max)):
                            return

        await asyncio.gather(*[process_account(a) for a in accounts], return_exceptions=True)

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")


class UpdateProfileRunner(TaskRunner):
    """Set first_name/last_name/about/username on each account.

    Params:
      account_ids        list[int]
      mode               'fixed' | 'pool'  — fixed values vs random from a NamePool
      first_name, last_name, about, username  (when mode='fixed', any can be empty
        string '' to leave unchanged — represented as None below)
      first_name_pool_id, last_name_pool_id, username_pool_id (when mode='pool')
      delay_min_sec, delay_max_sec, concurrency, skip_inactive, skip_spam
    """

    async def run(self):
        from .services import update_profile_for_account

        p = self.params
        account_ids = list(p.get('account_ids') or [])
        mode = p.get('mode', 'fixed')
        delay_min = float(p.get('delay_min_sec', 5))
        delay_max = float(p.get('delay_max_sec', 15))
        concurrency = max(1, int(p.get('concurrency', 3)))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)
        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed', error="No accounts",
                                       finished_at=timezone.now())
            return

        # Resolve pools when mode='pool'
        first_pool = last_pool = uname_pool = None
        if mode == 'pool':
            from .models import NamePool
            for key, target in (('first_name_pool_id', 'first'),
                                 ('last_name_pool_id', 'last'),
                                 ('username_pool_id', 'uname')):
                pid = p.get(key)
                if pid:
                    pool = await NamePool.objects.filter(pk=pid, owner=self.task.owner).afirst()
                    if pool is None:
                        await self.log('warning', f"Pool {pid} topilmadi ({key})")
                        continue
                    names = await sync_to_async(list)(pool.names.all().values_list('text', flat=True))
                    if not names:
                        await self.log('warning', f"Pool {pool.name} bo'sh ({key})")
                        continue
                    if target == 'first':
                        first_pool = names
                    elif target == 'last':
                        last_pool = names
                    else:
                        uname_pool = names

        await self.update_progress(total=len(accounts))
        await self.log('info', f"{len(accounts)} ta akkaunt profilini yangilash")
        sem = asyncio.Semaphore(concurrency)
        rng = random.SystemRandom()

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return
                if not account.session_string:
                    await self.log('error', "Sessiya yo'q", account=account, step='no_session')
                    await self.incr_done(success=False)
                    return

                # Resolve fields per account
                if mode == 'pool':
                    fn = rng.choice(first_pool) if first_pool else None
                    ln = rng.choice(last_pool) if last_pool else None
                    un = rng.choice(uname_pool) if uname_pool else None
                    ab = None  # bio not pool-driven (would need its own pool)
                else:
                    fn = p.get('first_name')
                    ln = p.get('last_name')
                    ab = p.get('about')
                    un = p.get('username')
                    # '' from form means "don't change" — only non-empty / explicitly cleared
                    fn = fn if fn != '' else None
                    ln = ln if ln != '' else None
                    ab = ab if ab != '' else None
                    un = un if un != '' else None

                try:
                    result = await update_profile_for_account(
                        account, first_name=fn, last_name=ln, about=ab, username=un,
                    )
                except FloodWaitError as e:
                    wait = int(getattr(e, 'seconds', 0) or 0)
                    await self.log('warning', f"FloodWait {wait}s",
                                   account=account, step='flood_wait')
                    await self.incr_done(success=False)
                    return

                if result['success']:
                    bits = []
                    if fn is not None: bits.append(f"ism='{fn}'")
                    if ln is not None: bits.append(f"familiya='{ln}'")
                    if ab is not None: bits.append("bio")
                    if un is not None: bits.append(f"username={result.get('username_status')}")
                    await self.log('success',
                        "✓ profil yangilandi: " + ", ".join(bits or ['—']),
                        account=account, step='updated')
                    await self.incr_done(success=True)
                else:
                    await self.log('error', result['error'],
                                   account=account, step='update_failed',
                                   telegram_error=result.get('error_type', ''))
                    await self.incr_done(success=False)

                if await self.cancellable_sleep(random.uniform(delay_min, delay_max)):
                    return

        await asyncio.gather(*[process_account(a) for a in accounts], return_exceptions=True)

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")


class ViewStoriesRunner(TaskRunner):
    """View (and optionally react to) stories from subscribed peers.

    Params:
      account_ids, react_chance (0..1), max_peers, concurrency,
      skip_inactive, skip_spam
    """

    async def run(self):
        from .services import view_and_react_stories_for_account

        p = self.params
        account_ids = list(p.get('account_ids') or [])
        react_chance = float(p.get('react_chance', 0))
        max_peers = int(p.get('max_peers', 50))
        concurrency = max(1, int(p.get('concurrency', 3)))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)
        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed', error="No accounts",
                                       finished_at=timezone.now())
            return

        await self.update_progress(total=len(accounts))
        await self.log('info', f"{len(accounts)} ta akkaunt × stories ko'rish")
        sem = asyncio.Semaphore(concurrency)

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return
                if not account.session_string:
                    await self.log('error', "Sessiya yo'q",
                                   account=account, step='no_session')
                    await self.incr_done(success=False)
                    return

                try:
                    result = await view_and_react_stories_for_account(
                        account, react_chance=react_chance, max_peers=max_peers,
                    )
                except FloodWaitError as e:
                    wait = int(getattr(e, 'seconds', 0) or 0)
                    await self.log('warning', f"FloodWait {wait}s",
                                   account=account, step='flood_wait')
                    await self.incr_done(success=False)
                    return

                if result.get('success'):
                    await self.log('success',
                        f"✓ {result['peers_seen']} peer, {result['stories_seen']} stor., "
                        f"{result['reactions_sent']} reaksiya, {result['errors']} xato",
                        account=account, step='stories_done')
                    await self.incr_done(success=True)
                else:
                    await self.log('error', result.get('error', 'Noma\'lum xato'),
                                   account=account, step='stories_failed',
                                   telegram_error=result.get('error_type', ''))
                    await self.incr_done(success=False)

        await asyncio.gather(*[process_account(a) for a in accounts], return_exceptions=True)

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")


class MarkAllReadRunner(TaskRunner):
    """For each account, send_read_acknowledge across all unread dialogs."""

    async def run(self):
        from .services import mark_all_read_for_account

        p = self.params
        account_ids = list(p.get('account_ids') or [])
        max_dialogs = int(p.get('max_dialogs', 500))
        concurrency = max(1, int(p.get('concurrency', 3)))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)
        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed', error="No accounts",
                                       finished_at=timezone.now())
            return

        await self.update_progress(total=len(accounts))
        await self.log('info', f"{len(accounts)} ta akkaunt × dialog'larni o'qish")
        sem = asyncio.Semaphore(concurrency)

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return
                if not account.session_string:
                    await self.log('error', "Sessiya yo'q",
                                   account=account, step='no_session')
                    await self.incr_done(success=False)
                    return
                try:
                    result = await mark_all_read_for_account(account, max_dialogs=max_dialogs)
                except FloodWaitError as e:
                    wait = int(getattr(e, 'seconds', 0) or 0)
                    await self.log('warning', f"FloodWait {wait}s",
                                   account=account, step='flood_wait')
                    await self.incr_done(success=False)
                    return

                if result.get('success'):
                    await self.log('success',
                        f"✓ {result['read']} ta o'qildi, {result['skipped']} ta o'tkazildi",
                        account=account, step='read_done')
                    await self.incr_done(success=True)
                else:
                    await self.log('error', result.get('error'),
                                   account=account, step='read_failed',
                                   telegram_error=result.get('error_type', ''))
                    await self.incr_done(success=False)

        await asyncio.gather(*[process_account(a) for a in accounts], return_exceptions=True)

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")


class Set2FAPasswordRunner(TaskRunner):
    """Set or change the 2FA password on each selected account.

    Params:
      account_ids
      new_password (str)        — required, will be applied to all accounts
      hint (str)                — optional shared hint
      use_db_current (bool)     — if True, current_password is read from
                                  Account.two_fa_password (default True)
      concurrency, skip_inactive, skip_spam
    """

    async def run(self):
        from .services import set_2fa_password_for_account

        p = self.params
        account_ids = list(p.get('account_ids') or [])
        new_password = (p.get('new_password') or '').strip()
        hint = (p.get('hint') or '').strip()
        concurrency = max(1, int(p.get('concurrency', 2)))
        skip_inactive = bool(p.get('skip_inactive', True))
        skip_spam = bool(p.get('skip_spam', True))

        if not new_password:
            await self.log('error', "Yangi parol bo'sh")
            await self.update_progress(status='failed', error="Empty password",
                                       finished_at=timezone.now())
            return
        if len(new_password) < 1:  # Telegram requires at least 1 char
            await self.log('error', "Parol juda qisqa")
            await self.update_progress(status='failed', error="Password too short",
                                       finished_at=timezone.now())
            return

        accounts_qs = Account.objects.filter(
            id__in=account_ids, owner=self.task.owner,
        ).select_related('device_setting')
        if skip_inactive:
            accounts_qs = accounts_qs.filter(is_active=True)
        if skip_spam:
            accounts_qs = accounts_qs.filter(is_spam=False)
        accounts = await sync_to_async(list)(accounts_qs)
        if not accounts:
            await self.log('error', "Filtrga mos akkaunt topilmadi")
            await self.update_progress(status='failed', error="No accounts",
                                       finished_at=timezone.now())
            return

        await self.update_progress(total=len(accounts))
        await self.log('info', f"{len(accounts)} ta akkaunt × 2FA parol o'rnatish")
        sem = asyncio.Semaphore(concurrency)

        async def process_account(account):
            async with sem:
                if await self.is_cancelled():
                    return
                if not account.session_string:
                    await self.log('error', "Sessiya yo'q",
                                   account=account, step='no_session')
                    await self.incr_done(success=False)
                    return
                try:
                    result = await set_2fa_password_for_account(
                        account, new_password=new_password, hint=hint,
                    )
                except FloodWaitError as e:
                    wait = int(getattr(e, 'seconds', 0) or 0)
                    await self.log('warning', f"FloodWait {wait}s",
                                   account=account, step='flood_wait')
                    await self.incr_done(success=False)
                    return

                if result['success']:
                    await self.log('success',
                        "✓ 2FA parol o'rnatildi",
                        account=account, step='2fa_set')
                    await self.incr_done(success=True)
                else:
                    await self.log('error', result['error'],
                                   account=account, step='2fa_failed',
                                   telegram_error=result.get('error_type', ''))
                    await self.incr_done(success=False)

        await asyncio.gather(*[process_account(a) for a in accounts], return_exceptions=True)

        if await self.is_cancelled():
            await self.update_progress(status='cancelled', finished_at=timezone.now())
            await self.log('warning', "Vazifa bekor qilindi")
        else:
            await self.update_progress(status='completed', finished_at=timezone.now())
            await self.log('success', "Vazifa yakunlandi")


RUNNERS = {
    'create_groups': CreateGroupsRunner,
    'create_channels': CreateChannelsRunner,
    'join_channel': JoinChannelRunner,
    'leave_groups': LeaveChatsRunner,
    'leave_channels': LeaveChatsRunner,
    'send_message': SendMessageRunner,
    'update_profile': UpdateProfileRunner,
    'view_stories': ViewStoriesRunner,
    'mark_all_read': MarkAllReadRunner,
    'set_2fa_password': Set2FAPasswordRunner,
    'boost_views': BoostViewsRunner,
    'react_to_post': ReactToPostRunner,
    'vote_poll': VotePollRunner,
    'press_start': PressStartRunner,
    'run_script': RunScriptRunner,
    'account_warming': AccountWarmingRunner,
}
