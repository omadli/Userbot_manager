from django.contrib import admin

from .models import NamePool, RandomName, ScriptTemplate, Task, TaskEvent


@admin.register(ScriptTemplate)
class ScriptTemplateAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'owner', 'updated_at')
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at')


class RandomNameInline(admin.TabularInline):
    model = RandomName
    extra = 0
    fields = ('text', 'used_count')
    readonly_fields = ('used_count',)


@admin.register(NamePool)
class NamePoolAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category', 'owner', 'name_count', 'created_at')
    list_filter = ('category',)
    search_fields = ('name', 'owner__username')

    def name_count(self, obj):
        return obj.names.count()
    name_count.short_description = "Nomlar soni"


@admin.register(RandomName)
class RandomNameAdmin(admin.ModelAdmin):
    list_display = ('id', 'text', 'pool', 'used_count')
    list_filter = ('pool',)
    search_fields = ('text',)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'kind', 'owner', 'status', 'done', 'total', 'error_count', 'created_at')
    list_filter = ('status', 'kind')
    search_fields = ('owner__username',)
    readonly_fields = (
        'kind', 'owner', 'params', 'stats',
        'started_at', 'finished_at', 'created_at',
        'total', 'done', 'success_count', 'error_count',
    )


@admin.register(TaskEvent)
class TaskEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'account', 'level', 'step', 'telegram_error', 'created_at')
    list_filter = ('level', 'step', 'telegram_error')
    search_fields = ('message', 'telegram_error')
    readonly_fields = ('task', 'account', 'level', 'step', 'message', 'telegram_error', 'created_at')
