from django.db import models
from django.conf import settings
from django.utils import timezone


class NamePool(models.Model):
    CATEGORY_CHOICES = [
        ('group', 'Guruh'),
        ('channel', 'Kanal'),
        ('any', 'Har qanday'),
    ]
    name = models.CharField(max_length=100, verbose_name="Pool nomi")
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default='any',
        verbose_name="Kategoriya",
    )
    description = models.TextField(blank=True, default='', verbose_name="Izoh")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='name_pools', null=True, blank=True,
        verbose_name="Egasi",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Nom pool"
        verbose_name_plural = "Nom pool'lar"

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"


class ScriptTemplate(models.Model):
    """
    User-authored async Telethon snippet. Bodies follow the contract:

        async def main(client, account, params):
            # client: connected TelegramClient
            # account: accounts.Account model instance
            # params: dict from the task form
            ...
            return {...}  # optional; serialized into the success log

    **Admin-only.** Running arbitrary Python is a full code-execution
    primitive; the runner and every view guard on `is_superuser`.
    Multi-tenant non-admin users must NEVER be allowed to create or
    execute scripts through the web UI.
    """
    name = models.CharField(max_length=200, verbose_name="Nomi")
    description = models.TextField(blank=True, default='', verbose_name="Izoh")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='script_templates', verbose_name="Yaratgan",
    )
    code = models.TextField(verbose_name="Kod (Python async)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = "Skript shabloni"
        verbose_name_plural = "Skript shablonlari"

    def __str__(self):
        return f"{self.name} (#{self.pk})"


class RandomName(models.Model):
    pool = models.ForeignKey(
        NamePool, on_delete=models.CASCADE, related_name='names',
        verbose_name="Pool",
    )
    text = models.CharField(max_length=255, verbose_name="Matn")
    used_count = models.PositiveIntegerField(default=0, verbose_name="Ishlatilgan")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('pool', 'text')]
        ordering = ['id']
        verbose_name = "Random nom"
        verbose_name_plural = "Random nomlar"

    def __str__(self):
        return self.text


class Task(models.Model):
    KIND_CHOICES = [
        ('create_groups', 'Guruh yaratish'),
        ('create_channels', 'Kanal yaratish'),
        ('join_channel', "Kanalga qo'shilish"),
        ('leave_groups', "Guruhlardan chiqish (admin emas)"),
        ('leave_channels', "Kanallardan chiqish (admin emas)"),
        ('send_message', "Xabar yuborish"),
        ('update_profile', "Profilni yangilash"),
        ('view_stories', "Stories ko'rish"),
        ('mark_all_read', "Hammasini o'qilgan deb belgilash"),
        ('set_2fa_password', "2FA parolni o'rnatish"),
        ('boost_views', 'View oshirish'),
        ('react_to_post', 'Reaksiya qo\'yish'),
        ('vote_poll', 'Ovoz berish'),
        ('press_start', 'Botga /start'),
        ('run_script', 'Skript ishga tushirish'),
        ('account_warming', 'Akkaunt warming'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Kutmoqda'),
        ('running', 'Ishlamoqda'),
        ('completed', 'Yakunlandi'),
        ('failed', 'Xato'),
        ('cancelled', 'Bekor qilindi'),
        ('paused', 'Pauza'),
    ]

    kind = models.CharField(max_length=32, choices=KIND_CHOICES, verbose_name="Turi")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='jobs', verbose_name="Egasi",
    )
    params = models.JSONField(default=dict, blank=True, verbose_name="Parametrlar")

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending',
        verbose_name="Holat",
    )

    # Scheduling
    # When `scheduled_at` is set in the future, the worker leaves the task
    # as `pending` until that time. `recurring_cron` (5-field crontab syntax)
    # turns it into a template: every time the task finishes, the worker
    # clones it with a fresh `scheduled_at` derived from the expression.
    scheduled_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name="Belgilangan vaqt",
        help_text="Bo'sh qoldirilsa darhol ishga tushadi",
    )
    recurring_cron = models.CharField(
        max_length=100, blank=True, default='',
        verbose_name="Cron (takroriy)",
        help_text="Masalan: '*/30 * * * *' har 30 daqiqada. Bo'sh — bir martalik.",
    )
    # Links clones back to their source, so task_list can group them.
    recurring_parent = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='recurring_children',
        verbose_name="Takroriy ota-task",
    )
    total = models.IntegerField(default=0, verbose_name="Jami")
    done = models.IntegerField(default=0, verbose_name="Bajarilgan")
    success_count = models.IntegerField(default=0, verbose_name="Muvaffaqiyat")
    error_count = models.IntegerField(default=0, verbose_name="Xatolar")

    stats = models.JSONField(default=dict, blank=True, verbose_name="Statistika")
    error = models.TextField(blank=True, default='', verbose_name="Umumiy xato")

    cancel_requested = models.BooleanField(default=False, verbose_name="Bekor qilish so'raldi")
    pause_requested = models.BooleanField(default=False, verbose_name="Pauza so'raldi")

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Vazifa"
        verbose_name_plural = "Vazifalar"

    def __str__(self):
        return f"#{self.pk} {self.get_kind_display()} — {self.get_status_display()}"

    @property
    def percent(self):
        if self.total <= 0:
            return 0.0
        return round(100.0 * self.done / self.total, 1)

    @property
    def elapsed_seconds(self):
        if not self.started_at:
            return 0
        end = self.finished_at or timezone.now()
        return max(0, int((end - self.started_at).total_seconds()))

    @property
    def eta_seconds(self):
        if self.status != 'running':
            return None
        if self.done <= 0:
            return None
        remaining = self.total - self.done
        if remaining <= 0:
            return 0

        p = self.params or {}
        delay_min = float(p.get('delay_min_sec', 0) or 0)
        delay_max = float(p.get('delay_max_sec', 0) or 0)
        avg_pause = (delay_min + delay_max) / 2
        concurrency = max(1, int(p.get('concurrency', 1) or 1))
        floor_rate = avg_pause / concurrency if avg_pause > 0 else 0

        rate_ema = (self.stats or {}).get('_eta_rate_ema')
        if rate_ema and rate_ema > 0:
            rate = max(rate_ema, floor_rate)
        else:
            avg_rate = self.elapsed_seconds / self.done
            rate = max(avg_rate, floor_rate)
        return int(rate * remaining)

    @property
    def is_finished(self):
        return self.status in ('completed', 'failed', 'cancelled')

    @property
    def is_paused(self):
        return self.status == 'paused'

    @property
    def can_pause(self):
        return self.status in ('pending', 'running')

    @property
    def can_resume(self):
        return self.status == 'paused'

    @property
    def is_scheduled_future(self):
        """True when the task has a scheduled_at in the future (= shouldn't be claimed yet)."""
        if self.status != 'pending' or not self.scheduled_at:
            return False
        return self.scheduled_at > timezone.now()

    def next_cron_fire(self, base=None):
        """
        Resolve the next fire time for `recurring_cron`, or None if the
        field is empty or the expression is invalid.

        `base` defaults to now — pass the last run's end time to prevent
        drift when resolving the successor of a finished task.
        """
        if not self.recurring_cron:
            return None
        try:
            from croniter import croniter
        except ImportError:
            return None
        base = base or timezone.now()
        try:
            return croniter(self.recurring_cron, base).get_next(type(base))
        except Exception:
            return None


class TaskEvent(models.Model):
    LEVEL_CHOICES = [
        ('info', "Ma'lumot"),
        ('success', 'Muvaffaqiyat'),
        ('warning', 'Ogohlantirish'),
        ('error', 'Xato'),
    ]
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name='events',
        verbose_name="Vazifa",
    )
    account = models.ForeignKey(
        'accounts.Account', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='task_events',
        verbose_name="Akkaunt",
    )
    level = models.CharField(
        max_length=16, choices=LEVEL_CHOICES, default='info',
        verbose_name="Daraja",
    )
    step = models.CharField(max_length=64, blank=True, default='', verbose_name="Qadam")
    message = models.TextField(verbose_name="Xabar")
    telegram_error = models.CharField(
        max_length=100, blank=True, default='',
        verbose_name="Telegram xato kodi",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['task', 'id']),
            models.Index(fields=['task', 'level']),
        ]
        verbose_name = "Vazifa hodisasi"
        verbose_name_plural = "Vazifa hodisalari"

    def __str__(self):
        return f"[{self.level}] {self.message[:60]}"


class TaskCheckpoint(models.Model):
    """Per-item completion marker so a task can resume after a worker
    restart without redoing already-finished items.

    `key` is runner-defined — typically '<account_id>-<item_idx>' or
    just '<account_id>' for per-account-only runners. The unique
    constraint makes concurrent inserts safe under parallel workers.
    """
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name='checkpoints',
        verbose_name="Vazifa",
    )
    key = models.CharField(max_length=128, verbose_name="Kalit")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('task', 'key')]
        indexes = [models.Index(fields=['task', 'key'])]
        verbose_name = "Vazifa checkpoint"
        verbose_name_plural = "Vazifa checkpointlari"
