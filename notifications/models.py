from django.conf import settings
from django.db import models


class NotificationConfig(models.Model):
    EVENT_CHOICES = [
        ('task_completed', "Vazifa yakunlandi"),
        ('task_failed', "Vazifa xatolik bilan tugadi"),
        ('task_paused', "Vazifa pauza qilindi"),
        ('account_session_dead', "Akkaunt sessiyasi chiqarib yuborildi"),
        ('account_banned', "Akkaunt bloklandi"),
        ('flood_wait_long', "Uzoq FloodWait (>5 daqiqa)"),
        ('quota_exhausted', "Kunlik byudjet tugadi"),
    ]
    DEFAULT_EVENTS = [k for k, _ in EVENT_CHOICES]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_config',
        verbose_name="Foydalanuvchi",
    )
    bot_token = models.CharField(
        max_length=128, blank=True, default='',
        verbose_name="Bot token",
        help_text="@BotFather'da yangi bot yaratib, tokenini yopishtiring",
    )
    chat_id = models.CharField(
        max_length=64, blank=True, default='',
        verbose_name="Chat ID",
        help_text="Bildirishnoma yuboriladigan chat ID (foydalanuvchi yoki guruh)",
    )
    events = models.JSONField(
        default=list, blank=True,
        verbose_name="Yoqilgan eventlar",
    )
    enabled = models.BooleanField(default=True, verbose_name="Yoqilgan")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Bildirishnoma sozlamasi"
        verbose_name_plural = "Bildirishnoma sozlamalari"

    def __str__(self):
        return f"NotificationConfig({self.user})"

    @property
    def is_configured(self):
        return bool(self.enabled and self.bot_token and self.chat_id)

    def is_event_enabled(self, event):
        return self.enabled and event in (self.events or [])
