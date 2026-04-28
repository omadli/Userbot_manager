from django.contrib import admin
from django.utils.html import format_html
from import_export.admin import ImportExportModelAdmin
from unfold.admin import ModelAdmin
from unfold.contrib.import_export.forms import ExportForm, ImportForm

from .models import Channel


@admin.register(Channel)
class ChannelAdmin(ModelAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm

    list_display = ('id', 'name', 'telegram_id', 'owner', 'invite_link_short', 'created_at')
    search_fields = ('name', 'telegram_id', 'owner__phone_number', 'owner__username')
    list_filter = (
        ('owner', admin.RelatedOnlyFieldListFilter),
    )
    date_hierarchy = 'created_at'
    list_per_page = 50

    def invite_link_short(self, obj):
        if not getattr(obj, 'invite_link', None):
            return '—'
        return format_html('<a href="{}" target="_blank">link</a>', obj.invite_link)
    invite_link_short.short_description = "Taklif"
