"""
Background worker.

Run in a separate terminal:

    python manage.py run_worker

Polls the jobs_task table every `--interval` seconds. When a pending task
is found, it atomically claims it (status='pending' → 'running'), then
dispatches to the runner registered for that task kind.

Only one worker process at a time is supported. Running two concurrent
workers is safe (claim is atomic), but they'll contend unnecessarily.
"""
import asyncio
import signal
import copy

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from jobs.models import Task
from jobs.runners import RUNNERS


class Command(BaseCommand):
    help = "Process queued background tasks from jobs.Task"

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval', type=float, default=1.5,
            help="Seconds to wait between polls when the queue is empty (default: 1.5)",
        )
        parser.add_argument(
            '--reset-stuck', action='store_true',
            help="On startup, flip any 'running' tasks back to 'failed' (use after a crash)",
        )

    def handle(self, *args, **options):
        interval = options['interval']
        reset_stuck = options['reset_stuck']

        if reset_stuck:
            stuck = Task.objects.filter(status='running')
            n = stuck.count()
            if n:
                stuck.update(
                    status='failed',
                    error='Worker restarted while task was running',
                    finished_at=timezone.now(),
                )
                self.stdout.write(self.style.WARNING(
                    f"Reset {n} stuck running task(s) → failed"
                ))

        self.stdout.write(self.style.SUCCESS(
            f"Worker started. Polling every {interval}s. Press Ctrl+C to stop."
        ))

        try:
            asyncio.run(self._loop(interval))
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nStopped by user (Ctrl+C)"))

    async def _loop(self, interval):
        while True:
            task = await self._claim_next()
            if task is None:
                await asyncio.sleep(interval)
                continue

            runner_cls = RUNNERS.get(task.kind)
            ts = timezone.now().strftime('%H:%M:%S')

            if runner_cls is None:
                self.stdout.write(self.style.ERROR(
                    f"[{ts}] Task #{task.pk} has unknown kind '{task.kind}'"
                ))
                await Task.objects.filter(pk=task.pk).aupdate(
                    status='failed',
                    error=f"No runner registered for kind '{task.kind}'",
                    finished_at=timezone.now(),
                )
                continue

            self.stdout.write(f"[{ts}] ▶ Task #{task.pk} ({task.kind})")
            try:
                runner = runner_cls(task)
                await runner.run()
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f"[{timezone.now().strftime('%H:%M:%S')}] ✗ Task #{task.pk} crashed: {e!r}"
                ))
                await Task.objects.filter(pk=task.pk).aupdate(
                    status='failed',
                    error=f"{type(e).__name__}: {e}",
                    finished_at=timezone.now(),
                )
            else:
                done = timezone.now().strftime('%H:%M:%S')
                self.stdout.write(self.style.SUCCESS(f"[{done}] ✓ Task #{task.pk} done"))

            # Recurrence: if the just-finished task has a cron expression
            # and wasn't cancelled by the user, schedule the next occurrence.
            await self._schedule_next_occurrence(task.pk)

    async def _claim_next(self):
        """
        Atomically move the oldest claimable pending task to 'running'.

        Claimable = pending AND (scheduled_at IS NULL OR scheduled_at <= now()).
        Returns Task or None.

        We `select_related('owner')` so runners can read `task.owner` from
        their async run() without hitting Django's SynchronousOnlyOperation
        guard on a lazy FK fetch.
        """
        now = timezone.now()
        pending = await (
            Task.objects.filter(status='pending')
            .filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now))
            .order_by('scheduled_at', 'created_at')
            .afirst()
        )
        if pending is None:
            return None
        updated = await Task.objects.filter(
            pk=pending.pk, status='pending',
        ).aupdate(
            status='running',
            started_at=timezone.now(),
        )
        if updated == 0:
            return None  # Another worker grabbed it first.
        return await Task.objects.select_related('owner').aget(pk=pending.pk)

    async def _schedule_next_occurrence(self, finished_pk):
        """
        If the just-finished task has a `recurring_cron`, clone it with a
        fresh `scheduled_at` pointing at the next cron match. The clone
        inherits params/owner/kind and starts life in `pending`.

        The user-triggered cancel path skips cloning (so "Bekor qilish"
        actually ends the recurring chain).
        """
        finished = await Task.objects.filter(pk=finished_pk).afirst()
        if finished is None or not finished.recurring_cron:
            return
        if finished.status == 'cancelled' or finished.cancel_requested:
            return

        base = finished.finished_at or timezone.now()
        next_run = finished.next_cron_fire(base=base)
        if next_run is None:
            self.stdout.write(self.style.WARNING(
                f"[{timezone.now().strftime('%H:%M:%S')}] "
                f"Task #{finished_pk}: cron '{finished.recurring_cron}' yaroqsiz — takroriylik to'xtatildi"
            ))
            return

        # Point the clone at the original source (or the original itself
        # if this task was already a clone).
        parent_id = finished.recurring_parent_id or finished.pk

        clone = await Task.objects.acreate(
            kind=finished.kind,
            owner_id=finished.owner_id,
            params=copy.deepcopy(finished.params or {}),
            status='pending',
            scheduled_at=next_run,
            recurring_cron=finished.recurring_cron,
            recurring_parent_id=parent_id,
        )
        self.stdout.write(self.style.SUCCESS(
            f"[{timezone.now().strftime('%H:%M:%S')}] "
            f"↻ Task #{clone.pk} ({clone.kind}) scheduled for {next_run.isoformat(timespec='minutes')}"
        ))
