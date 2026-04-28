from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User, Group as AuthGroup
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from import_export import resources, fields
from import_export.admin import ImportExportModelAdmin
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import (
    BooleanRadioFilter, RangeNumericFilter, ChoicesDropdownFilter,
)
from unfold.contrib.import_export.forms import ExportForm, ImportForm
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from .models import Tag, Account, DeviceSetting, Proxy


# --- Re-register the auth models so they pick up Unfold's styling ---------

admin.site.unregister(User)
admin.site.unregister(AuthGroup)


@admin.register(User)
class UserAdmin(DjangoUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    list_display = ('username', 'email', 'first_name', 'last_name', 'is_staff', 'is_active', 'date_joined')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'groups')


@admin.register(AuthGroup)
class AuthGroupAdmin(ModelAdmin):
    search_fields = ('name',)
    filter_horizontal = ('permissions',)


# --- Tag, DeviceSetting, Proxy --------------------------------------------

@admin.register(Tag)
class TagAdmin(ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    list_display = ('id', 'name', 'account_count')
    search_fields = ('name',)

    def account_count(self, obj):
        return obj.accounts.count()
    account_count.short_description = "Akkauntlar"


@admin.register(DeviceSetting)
class DeviceSettingAdmin(ModelAdmin):
    list_display = ('name', 'device_model', 'system_version', 'app_version', 'lang_code', 'use_count')
    search_fields = ('name', 'device_model')

    def use_count(self, obj):
        return obj.accounts.count()
    use_count.short_description = "Foydalanilgan"


@admin.register(Proxy)
class ProxyAdmin(ModelAdmin):
    list_display = (
        'id', 'name', 'proxy_type', 'host', 'port',
        'owner', 'is_active', 'health_dot', 'last_checked_at',
    )
    list_filter = (
        ('proxy_type', ChoicesDropdownFilter),
        ('is_active', BooleanRadioFilter),
        ('last_check_ok', BooleanRadioFilter),
    )
    search_fields = ('name', 'host', 'owner__username')
    readonly_fields = ('last_checked_at', 'last_check_ok', 'last_check_error', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'

    def health_dot(self, obj):
        if obj.last_checked_at is None:
            return mark_safe('<span style="color:#888">●</span> tekshirilmagan')
        color = '#28a745' if obj.last_check_ok else '#dc3545'
        text = 'OK' if obj.last_check_ok else 'FAIL'
        return format_html('<span style="color:{}">●</span> {}', color, text)
    health_dot.short_description = "Holat"


# --- Account: encrypted fields exposed in CSV/XLSX export -----------------

class AccountResource(resources.ModelResource):
    """Export every column including the decrypted session_string + 2FA.

    EncryptedTextField returns plaintext on attribute access, so the default
    field declarations already get the readable values. We list them
    explicitly so future field additions don't quietly leak.
    """
    session_string = fields.Field(attribute='session_string', column_name='session_string')
    two_fa_password = fields.Field(attribute='two_fa_password', column_name='two_fa_password')
    owner_username = fields.Field(attribute='owner__username', column_name='owner_username')

    class Meta:
        model = Account
        fields = (
            'id', 'phone_number', 'owner_username', 'user_id',
            'first_name', 'last_name', 'username', 'email',
            'api_id', 'api_hash',
            'session_string', 'two_fa_password',
            'is_active', 'is_spam',
            'daily_op_limit', 'quota_window_start', 'quota_window_count',
            'created_at',
        )
        export_order = fields


@admin.register(Account)
class AccountAdmin(ModelAdmin, ImportExportModelAdmin):
    resource_classes = [AccountResource]
    import_form_class = ImportForm
    export_form_class = ExportForm

    list_display = (
        'id', 'phone_number', 'first_name', 'username', 'owner',
        'status_badge', 'spam_badge', 'session_dot', 'quota_text', 'created_at',
    )
    list_filter = (
        ('is_active', BooleanRadioFilter),
        ('is_spam', BooleanRadioFilter),
        ('owner', admin.RelatedOnlyFieldListFilter),
        'tags',
        ('daily_op_limit', RangeNumericFilter),
    )
    search_fields = ('phone_number', 'first_name', 'last_name', 'username', 'email', 'owner__username')
    filter_horizontal = ('tags',)
    readonly_fields = ('session_string', 'two_fa_password', 'session_status_note', 'created_at')
    date_hierarchy = 'created_at'
    list_per_page = 50

    fieldsets = (
        ("Account Info", {
            'fields': ('owner', 'user_id', 'phone_number', 'first_name', 'last_name', 'username', 'email'),
        }),
        ("Tunings", {
            'classes': ('collapse',),
            'fields': ('api_id', 'api_hash', 'device_setting', 'proxy', 'daily_op_limit'),
        }),
        ("Secrets (read-only, encrypted at rest)", {
            'classes': ('collapse',),
            'description': (
                "Session string va 2FA parol DB da shifrlangan. "
                "Bu yerda ko'rinadigan qiymatlar dekriptlangan — ehtiyotkorlik bilan ishlang."
            ),
            'fields': ('session_status_note', 'two_fa_password', 'session_string'),
        }),
        ("Status", {
            'fields': ('is_active', 'is_spam', 'tags'),
        }),
        ("Quota", {
            'classes': ('collapse',),
            'fields': ('quota_window_start', 'quota_window_count'),
        }),
    )

    actions = ['mark_active', 'mark_inactive', 'reset_daily_quota']

    @admin.action(description="Belgilanganlarni faollashtirish")
    def mark_active(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, f"{n} ta akkaunt faollashtirildi")

    @admin.action(description="Belgilanganlarni o'chirish (is_active=False)")
    def mark_inactive(self, request, queryset):
        n = queryset.update(is_active=False)
        self.message_user(request, f"{n} ta akkaunt to'xtatildi")

    @admin.action(description="Bugungi quota counter'ni nolga tushirish")
    def reset_daily_quota(self, request, queryset):
        n = queryset.update(quota_window_count=0)
        self.message_user(request, f"{n} ta akkaunt quota'si reset qilindi")

    def status_badge(self, obj):
        color = '#28a745' if obj.is_active else '#6c757d'
        text = 'aktiv' if obj.is_active else "to'xtatilgan"
        return format_html('<span style="color:{};font-weight:600">{}</span>', color, text)
    status_badge.short_description = "Status"

    def spam_badge(self, obj):
        if not obj.is_spam:
            return mark_safe('<span style="color:#28a745">✓ toza</span>')
        return mark_safe('<span style="color:#dc3545;font-weight:600">⚠ spam</span>')
    spam_badge.short_description = "Spam"

    def session_dot(self, obj):
        if not obj.session_string:
            return mark_safe('<span style="color:#dc3545">●</span> yo\'q')
        return mark_safe('<span style="color:#28a745">●</span> bor')
    session_dot.short_description = "Sessiya"

    def quota_text(self, obj):
        limit = obj.daily_op_limit
        used = obj.quota_window_count or 0
        if not limit:
            return mark_safe('<span style="color:#888">cheksiz</span>')
        ratio = used / limit
        color = '#28a745' if ratio < 0.7 else ('#ffc107' if ratio < 0.95 else '#dc3545')
        return format_html('<span style="color:{}">{} / {}</span>', color, used, limit)
    quota_text.short_description = "Quota (bugun)"

    def session_status_note(self, obj):
        if not obj or not obj.session_string:
            return "— sessiya yo'q"
        return f"Sessiya bor ({len(obj.session_string)} belgi, dekriptlangan)"
    session_status_note.short_description = "Sessiya holati"
