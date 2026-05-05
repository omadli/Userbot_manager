"""
Background worker.

Runs in a separate terminal:

    python manage.py run_worker
    python manage.py run_worker --max-concurrency 10
    python manage.py run_worker --reset-stuck    # legacy: mark interrupted as failed

By default, on startup the worker resumes any tasks left in `running`
from a previous (crashed/restarted) worker — they get flipped back to
`pending`, their progress counters re-derived from TaskCheckpoint rows,
and the next claim picks them up. Runners check `is_completed(key)`
per item so already-finished work is skipped.

Up to `--max-concurrency` tasks run in parallel inside this worker
(each as its own asyncio.Task). The DB claim is atomic, so even
multiple worker processes are safe — they just contend less if you
bump in-process concurrency instead.
"""
import asyncio
import copy

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from jobs.models import Task, TaskCheckpoint
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
            help="Mark any 'running' tasks as failed instead of resuming them",
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
                    error='Worker restarted; --reset-stuck',
                    finished_at=timezone.now(),
                )
                self.stdout.write(self.style.WARNING(
                    f"Reset {n} running task(s) → failed (--reset-stuck)"
                ))
        else:
            self._resume_orphans()

        self.stdout.write(self.style.SUCCESS(
            f"Worker started — concurrency={max_concurrency}, idle poll={interval}s. "
            f"Ctrl+C to stop."
        ))

        try:
            asyncio.run(self._loop(interval, max_concurrency))
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nStopped by user (Ctrl+C)"))

    def _resume_orphans(self):
        """Tasks left in 'running' from a crashed worker → flip to 'pending'
        and re-derive done counters from checkpoints, so the runner picks
        them up and skips already-finished items via is_completed()."""
        orphans = list(Task.objects.filter(status='running'))
        if not orphans:
            return
        for t in orphans:
            n_done = TaskCheckpoint.objects.filter(task=t).count()
            Task.objects.filter(pk=t.pk).update(
                status='pending',
                started_at=None,
                done=n_done,
                success_count=n_done,
                error_count=0,
                pause_requested=False,
            )
        self.stdout.write(self.style.SUCCESS(
            f"Resumed {len(orphans)} interrupted task(s) — they'll continue "
            f"from where they stopped"
        ))

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
                await asyncio.wait(
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

        is_resume = (task.done > 0)
        prefix = "↻ resume" if is_resume else "▶"
        self.stdout.write(f"[{self._ts()}] {prefix} Task #{task.pk} ({task.kind}){' — ' + str(task.done) + ' bajarilgan' if is_resume else ''}")
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
        if finished.status in ('cancelled', 'paused') or finished.cancel_requested:
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
