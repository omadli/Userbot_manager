from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import (
    ChoicesDropdownFilter, RangeDateFilter,
)

from .models import NamePool, RandomName, ScriptTemplate, Task, TaskEvent


@admin.register(ScriptTemplate)
class ScriptTemplateAdmin(ModelAdmin):
    list_display = ('id', 'name', 'owner', 'updated_at')
    search_fields = ('name', 'owner__username')
    list_filter = (('owner', admin.RelatedOnlyFieldListFilter),)
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'updated_at'


class RandomNameInline(TabularInline):
    model = RandomName
    extra = 0
    fields = ('text', 'used_count')
    readonly_fields = ('used_count',)


@admin.register(NamePool)
class NamePoolAdmin(ModelAdmin):
    list_display = ('id', 'name', 'category', 'owner', 'name_count', 'created_at')
    list_filter = (
        ('category', ChoicesDropdownFilter),
        ('owner', admin.RelatedOnlyFieldListFilter),
    )
    search_fields = ('name', 'owner__username')
    inlines = [RandomNameInline]
    date_hierarchy = 'created_at'

    def name_count(self, obj):
        return obj.names.count()
    name_count.short_description = "Nomlar soni"


@admin.register(RandomName)
class RandomNameAdmin(ModelAdmin):
    list_display = ('id', 'text', 'pool', 'used_count')
    list_filter = (('pool', admin.RelatedOnlyFieldListFilter),)
    search_fields = ('text',)


@admin.register(Task)
class TaskAdmin(ModelAdmin):
    list_display = (
        'id', 'kind', 'owner', 'status_badge', 'progress_bar',
        'success_count', 'error_count', 'created_at',
    )
    list_filter = (
        ('status', ChoicesDropdownFilter),
        ('kind', ChoicesDropdownFilter),
        ('owner', admin.RelatedOnlyFieldListFilter),
        ('created_at', RangeDateFilter),
    )
    search_fields = ('owner__username', 'kind', 'error')
    readonly_fields = (
        'kind', 'owner', 'params', 'stats',
        'started_at', 'finished_at', 'created_at',
        'total', 'done', 'success_count', 'error_count',
        'recurring_parent', 'recurring_cron',
    )
    date_hierarchy = 'created_at'
    list_per_page = 50

    def status_badge(self, obj):
        colors = {
            'pending': '#6c757d',
            'running': '#007bff',
            'completed': '#28a745',
            'failed': '#dc3545',
            'cancelled': '#ffc107',
        }
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            colors.get(obj.status, '#000'), obj.get_status_display(),
        )
    status_badge.short_description = "Status"

    def progress_bar(self, obj):
        if obj.total <= 0:
            return '—'
        percent = round(100.0 * obj.done / obj.total, 1)
        color = '#28a745' if obj.status == 'completed' else (
            '#dc3545' if obj.status == 'failed' else '#007bff'
        )
        return format_html(
            '<div style="background:#eee;border-radius:4px;width:120px;height:14px;position:relative">'
            '<div style="width:{}%;background:{};height:100%;border-radius:4px"></div>'
            '<span style="position:absolute;top:-2px;left:50%;transform:translateX(-50%);'
            'font-size:11px;color:#222">{}/{}</span></div>',
            percent, color, obj.done, obj.total,
        )
    progress_bar.short_description = "Progress"


@admin.register(TaskEvent)
class TaskEventAdmin(ModelAdmin):
    list_display = ('id', 'task', 'account', 'level_badge', 'step', 'telegram_error', 'created_at')
    list_filter = (
        ('level', ChoicesDropdownFilter),
        'step',
        ('telegram_error', ChoicesDropdownFilter),
    )
    search_fields = ('message', 'telegram_error', 'task__id', 'account__phone_number')
    readonly_fields = ('task', 'account', 'level', 'step', 'message', 'telegram_error', 'created_at')
    date_hierarchy = 'created_at'
    list_per_page = 100

    def level_badge(self, obj):
        colors = {
            'info': '#0dcaf0', 'success': '#28a745',
            'warning': '#ffc107', 'error': '#dc3545',
        }
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            colors.get(obj.level, '#000'), obj.get_level_display(),
        )
    level_badge.short_description = "Daraja"
