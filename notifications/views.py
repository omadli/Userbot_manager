import asyncio

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render

from accounts.views import _require_login
from .models import NotificationConfig
from .services import test_send


async def settings_page(request):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    cfg = await _get_or_create(user)

    if request.method == 'POST':
        action = request.POST.get('action', 'save')

        if action == 'save':
            cfg.bot_token = (request.POST.get('bot_token') or '').strip()
            cfg.chat_id = (request.POST.get('chat_id') or '').strip()
            cfg.enabled = bool(request.POST.get('enabled'))
            cfg.events = request.POST.getlist('events')
            await cfg.asave()
            messages.success(request, "Bildirishnoma sozlamalari saqlandi")

        elif action == 'test':
            cfg.bot_token = (request.POST.get('bot_token') or cfg.bot_token).strip()
            cfg.chat_id = (request.POST.get('chat_id') or cfg.chat_id).strip()
            ok, info = await test_send(cfg)
            if ok:
                messages.success(request, f"Test xabari yuborildi: {info}")
            else:
                messages.error(request, f"Test muvaffaqiyatsiz: {info}")

        return redirect('notifications:settings')

    return render(request, 'notifications/settings.html', {
        'cfg': cfg,
        'event_choices': NotificationConfig.EVENT_CHOICES,
        'enabled_events': set(cfg.events or []),
    })


async def _get_or_create(user):
    cfg = await NotificationConfig.objects.filter(user=user).afirst()
    if cfg is None:
        cfg = await NotificationConfig.objects.acreate(
            user=user,
            events=NotificationConfig.DEFAULT_EVENTS,
        )
    return cfg
