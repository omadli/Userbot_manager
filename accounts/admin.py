from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from .models import Tag, Account, DeviceSetting, Proxy


@admin.register(Proxy)
class ProxyAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'proxy_type', 'host', 'port', 'owner', 'is_active', 'last_check_ok')
    list_filter = ('proxy_type', 'is_active')
    search_fields = ('name', 'host')
    readonly_fields = ('last_checked_at', 'last_check_ok', 'last_check_error', 'created_at', 'updated_at')

@admin.register(Tag)
class TagAdmin(ImportExportModelAdmin):
    list_display = ('id', 'name')
    search_fields = ('name',)

@admin.register(DeviceSetting)
class DeviceSettingAdmin(admin.ModelAdmin):
    list_display = ('name', 'device_model', 'system_version', 'app_version', 'lang_code')

@admin.register(Account)
class AccountAdmin(ImportExportModelAdmin):
    list_display = ('id', 'phone_number', 'first_name', 'username', 'is_active', 'is_spam', 'created_at')
    list_filter = ('is_active', 'is_spam', 'tags')
    # Deliberately not searching by session_string — the column stores ciphertext
    # and a LIKE on it leaks nothing useful anyway.
    search_fields = ('phone_number', 'first_name', 'last_name', 'username')
    filter_horizontal = ('tags',)
    # Encrypted secrets are read-only in admin to prevent accidental edits
    # that would silently re-encrypt or wipe them.
    readonly_fields = ('session_string', 'two_fa_password', 'session_status_note')

    fieldsets = (
        ('Account Info', {
            'fields': ('owner', 'user_id', 'phone_number', 'first_name', 'last_name', 'username', 'email')
        }),
        ('Secrets (read-only, encrypted at rest)', {
            'classes': ('collapse',),
            'description': (
                "Session string va 2FA parol DB da shifrlangan. "
                "Bu yerda ko'rinadigan qiymatlar dekriptlangan — ehtiyotkorlik bilan ishlang."
            ),
            'fields': ('session_status_note', 'two_fa_password', 'session_string'),
        }),
        ('Status', {
            'fields': ('is_active', 'is_spam', 'tags')
        }),
    )

    def session_status_note(self, obj):
        if not obj or not obj.session_string:
            return "— sessiya yo'q"
        return f"Sessiya bor ({len(obj.session_string)} belgi, dekriptlangan)"
    session_status_note.short_description = "Sessiya holati"
