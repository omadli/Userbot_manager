from django.db import models
from django.conf import settings
from .fields import EncryptedTextField

class Tag(models.Model):
    name = models.CharField(max_length=50, verbose_name="Tag Name")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='tags',
        null=True, blank=True,
        verbose_name="Egasi",
    )

    class Meta:
        unique_together = [('owner', 'name')]
        ordering = ['name']

    def __str__(self):
        return self.name

class DeviceSetting(models.Model):
    name = models.CharField(max_length=50, default="default", unique=True, verbose_name="Setting Name")
    device_model = models.CharField(max_length=100, default="Samsung S27 ultra")
    system_version = models.CharField(max_length=50, default="Android 15")
    app_version = models.CharField(max_length=50, default="12.6.4")
    lang_code = models.CharField(max_length=10, default="uz")
    system_lang_code = models.CharField(max_length=10, default="uz")

    class Meta:
        verbose_name = "Device Setting"
        verbose_name_plural = "Device Settings"

    @classmethod
    def get_settings(cls):
        obj, created = cls.objects.get_or_create(name="default")
        return obj

    def __str__(self):
        return f"Device Setting ({self.name})"


class Proxy(models.Model):
    """
    Outbound proxy for Telethon clients. Each Account can point at one.

    We support two kinds:
      socks5  — generic SOCKS5, served to Telethon via python-socks
      mtproto — Telegram's MTProxy (secret is the "fake-TLS" token)
    """
    PROXY_TYPE_CHOICES = [
        ('socks5', 'SOCKS5'),
        ('mtproto', 'MTProxy'),
    ]
    name = models.CharField(max_length=100, verbose_name="Nomi")
    proxy_type = models.CharField(
        max_length=10, choices=PROXY_TYPE_CHOICES, default='socks5',
        verbose_name="Turi",
    )
    host = models.CharField(max_length=255, verbose_name="Host / IP")
    port = models.PositiveIntegerField(verbose_name="Port")
    username = models.CharField(max_length=255, blank=True, default='', verbose_name="Login")
    password = models.CharField(max_length=255, blank=True, default='', verbose_name="Parol")
    # MTProto needs a hex secret instead of user/pass.
    secret = models.CharField(
        max_length=255, blank=True, default='',
        verbose_name="MTProxy secret (faqat mtproto)",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='proxies', verbose_name="Egasi",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    last_checked_at = models.DateTimeField(null=True, blank=True, verbose_name="Oxirgi tekshiruv")
    last_check_ok = models.BooleanField(null=True, blank=True, verbose_name="Oxirgi tekshiruv natijasi")
    last_check_error = models.TextField(blank=True, default='', verbose_name="Oxirgi xato")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Proxy"
        verbose_name_plural = "Proxy'lar"

    def __str__(self):
        return f"{self.name} ({self.proxy_type}://{self.host}:{self.port})"

    def as_telethon(self):
        """
        Return the proxy argument structure expected by Telethon's get_client().

        For SOCKS5 — a tuple:
            ('socks5', host, port, True, username, password)
        For MTProto — a tuple:
            (host, port, secret)  plus connection class (handled in get_client)
        """
        if self.proxy_type == 'socks5':
            return (
                'socks5', self.host, int(self.port),
                True,  # rdns: resolve DNS on proxy side
                self.username or None,
                self.password or None,
            )
        if self.proxy_type == 'mtproto':
            return (self.host, int(self.port), self.secret or '')
        return None


class Account(models.Model):
    phone_number = models.CharField(max_length=20, unique=True, verbose_name="Phone Number")
    
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="accounts", verbose_name="Owner", null=True, blank=True)
    user_id = models.BigIntegerField(blank=True, null=True, verbose_name="Telegram User ID")
    
    # Telegram Info
    first_name = models.CharField(max_length=255, blank=True, null=True, verbose_name="First Name")
    last_name = models.CharField(max_length=255, blank=True, null=True, verbose_name="Last Name")
    username = models.CharField(max_length=255, blank=True, null=True, verbose_name="Username")
    
    # Per-account Telegram API credentials (optional — falls back to settings)
    api_id   = models.IntegerField(blank=True, null=True, verbose_name="API ID", help_text="Bo'sh qoldirilsa global sozlamadan foydalaniladi")
    api_hash = models.CharField(max_length=64, blank=True, null=True, verbose_name="API Hash", help_text="Bo'sh qoldirilsa global sozlamadan foydalaniladi")

    # Per-account device fingerprint (optional — falls back to DeviceSetting default)
    device_setting = models.ForeignKey(
        'DeviceSetting',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='accounts',
        verbose_name="Qurilma sozlamasi",
        help_text="Bo'sh qoldirilsa 'default' sozlamasi ishlatiladi"
    )

    # Optional proxy. When set, Telethon routes this account's traffic through it.
    proxy = models.ForeignKey(
        'Proxy',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='accounts',
        verbose_name="Proxy",
        help_text="Bo'sh qoldirilsa to'g'ridan-to'g'ri ulanish"
    )

    # Auth & Status
    email = models.EmailField(blank=True, null=True, verbose_name="Email")
    # Encrypted at rest via Fernet (see accounts.fields.EncryptedTextField).
    # Legacy rows stored as plaintext continue to read correctly and are
    # re-encrypted on the next save or via `manage.py encrypt_existing`.
    two_fa_password = EncryptedTextField(blank=True, null=True, verbose_name="2FA Password", help_text="Optional")
    session_string = EncryptedTextField(blank=True, null=True, verbose_name="Telethon Session String")
    avatar = models.CharField(max_length=500, blank=True, null=True, verbose_name="Avatar URL Path")
    is_active = models.BooleanField(default=True, verbose_name="Is Active")
    is_spam = models.BooleanField(default=False, verbose_name="Is Spam")
    
    tags = models.ManyToManyField(Tag, blank=True, related_name="accounts", verbose_name="Tags")

    # Daily rate-limit budget. 0 = unlimited. New accounts should start low (~50).
    # Old / established accounts can go up to 200-300.
    # window_start + window_count form a rolling day-bucket that resets lazily
    # (on the first operation after midnight).
    daily_op_limit = models.PositiveIntegerField(
        default=200, verbose_name="Kunlik limit",
        help_text="0 = cheksiz"
    )
    quota_window_start = models.DateField(null=True, blank=True, verbose_name="Byudjet oynasi boshlandi")
    quota_window_count = models.PositiveIntegerField(default=0, verbose_name="Bugun bajarilgan")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        name = self.first_name if self.first_name else ""
        if self.last_name:
            name += f" {self.last_name}"
        name = name.strip()
        return f"{self.phone_number}{' (' + name + ')' if name else ''}"

    @property
    def country_code(self):
        import phonenumbers
        try:
            pn = phonenumbers.parse(self.phone_number)
            return phonenumbers.region_code_for_number(pn)
        except:
            return None

    @property
    def country(self):
        import phonenumbers
        try:
            pn = phonenumbers.parse(self.phone_number)
            from phonenumbers.geocoder import country_name_for_number
            return country_name_for_number(pn, "en")
        except:
            return "Unknown"
