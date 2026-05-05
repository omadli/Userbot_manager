"""
Background worker.

Runs in a separate terminal:

    python manage.py run_worker
    python manage.py run_worker --max-concurrency 10

Polls jobs_task. Up to `--max-concurrency` tasks run in parallel (each
spawned as its own asyncio.Task on the worker's event loop). The DB
claim is atomic (`aupdate(status='running')`), so even running multiple
worker processes is safe — they just contend less if you bump the
in-process concurrency instead.
"""
import asyncio
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
            help="Seconds between polls when idle (default: 1.5)",
        )
        parser.add_argument(
            '--max-concurrency', type=int, default=5,
            help="Max tasks running in parallel inside this worker (default: 5)",
        )
        parser.add_argument(
            '--reset-stuck', action='store_true',
            help="On startup, flip any 'running' tasks back to 'failed'",
        )

    def handle(self, *args, **options):
        interval = options['interval']
        max_concurrency = max(1, options['max_concurrency'])
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
            f"Worker started — concurrency={max_concurrency}, idle poll={interval}s. "
            f"Ctrl+C to stop."
        ))

        try:
            asyncio.run(self._loop(interval, max_concurrency))
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nStopped by user (Ctrl+C)"))

    async def _loop(self, interval, max_concurrency):
        running: set[asyncio.Task] = set()

        while True:
            for t in [t for t in running if t.done()]:
                running.discard(t)
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None:
                    self.stdout.write(self.style.ERROR(
                        f"[{self._ts()}] ✗ wrapper crash: {exc!r}"
                    ))

            while len(running) < max_concurrency:
                claimed = await self._claim_next()
                if claimed is None:
                    break
                aio_task = asyncio.create_task(
                    self._run_one(claimed),
                    name=f"task-{claimed.pk}",
                )
                running.add(aio_task)

            if running:
                done, _ = await asyncio.wait(
                    running, timeout=min(interval, 1.0),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(interval)

    async def _run_one(self, task):
        runner_cls = RUNNERS.get(task.kind)
        if runner_cls is None:
            self.stdout.write(self.style.ERROR(
                f"[{self._ts()}] Task #{task.pk} has unknown kind '{task.kind}'"
            ))
            await Task.objects.filter(pk=task.pk).aupdate(
                status='failed',
                error=f"No runner registered for kind '{task.kind}'",
                finished_at=timezone.now(),
            )
            return

        self.stdout.write(f"[{self._ts()}] ▶ Task #{task.pk} ({task.kind})")
        try:
            runner = runner_cls(task)
            await runner.run()
        except Exception as e:
            self.stdout.write(self.style.ERROR(
                f"[{self._ts()}] ✗ Task #{task.pk} crashed: {e!r}"
            ))
            await Task.objects.filter(pk=task.pk).aupdate(
                status='failed',
                error=f"{type(e).__name__}: {e}",
                finished_at=timezone.now(),
            )
        else:
            self.stdout.write(self.style.SUCCESS(
                f"[{self._ts()}] ✓ Task #{task.pk} done"
            ))

        await self._schedule_next_occurrence(task.pk)

    @staticmethod
    def _ts():
        return timezone.now().strftime('%H:%M:%S')

    async def _claim_next(self):
        """Atomically move the oldest claimable pending task to 'running'."""
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
            return None
        return await Task.objects.select_related('owner').aget(pk=pending.pk)

    async def _schedule_next_occurrence(self, finished_pk):
        finished = await Task.objects.filter(pk=finished_pk).afirst()
        if finished is None or not finished.recurring_cron:
            return
        if finished.status == 'cancelled' or finished.cancel_requested:
            return

        base = finished.finished_at or timezone.now()
        next_run = finished.next_cron_fire(base=base)
        if next_run is None:
            self.stdout.write(self.style.WARNING(
                f"[{self._ts()}] Task #{finished_pk}: "
                f"cron '{finished.recurring_cron}' yaroqsiz — takroriylik to'xtatildi"
            ))
            return

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
            f"[{self._ts()}] ↻ Task #{clone.pk} ({clone.kind}) "
            f"scheduled for {next_run.isoformat(timespec='minutes')}"
        ))
