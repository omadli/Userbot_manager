from django.db import models

class Group(models.Model):
    name = models.CharField(max_length=255, verbose_name="Guruh nomi")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Ochilgan sanasi")
    telegram_id = models.BigIntegerField(verbose_name="Guruh ID si")
    invite_link = models.URLField(max_length=500, blank=True, null=True, verbose_name="Invite link")
    owner = models.ForeignKey('accounts.Account', on_delete=models.CASCADE, related_name="groups", verbose_name="Egasi (Account)")

    class Meta:
        verbose_name = "Guruh"
        verbose_name_plural = "Guruhlar"
        
    def __str__(self):
        return f"{self.name} ({self.telegram_id})"
