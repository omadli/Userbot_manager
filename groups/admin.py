from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from .models import Group

@admin.register(Group)
class GroupAdmin(ImportExportModelAdmin):
    list_display = ('name', 'telegram_id', 'owner', 'created_at')
    search_fields = ('name', 'telegram_id', 'owner__phone_number')
    list_filter = ('owner',)
