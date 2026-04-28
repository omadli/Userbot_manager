from django.shortcuts import redirect
from django.contrib import messages
from django.http import HttpResponse
from asgiref.sync import sync_to_async
from django.conf import settings
from .models import Channel
from .filters import ChannelFilter
import csv


async def render_async(request, template, context=None):
    from django.shortcuts import render
    return await sync_to_async(render)(request, template, context or {})


async def _require_login(request):
    user = await request.auser()
    if not user.is_authenticated:
        return None
    return user


def _base_qs(user):
    """Channels visible to the user = channels owned by an Account that belongs to the user."""
    return (
        Channel.objects.filter(owner__owner=user)
        .select_related('owner')
        .order_by('-created_at')
    )


@sync_to_async
def _process_channels(get_params, user):
    qs = _base_qs(user)
    filterset = ChannelFilter(get_params, queryset=qs, user=user)
    _ = filterset.form  # force evaluation
    return filterset, list(filterset.qs)


@sync_to_async
def _export_channels(get_params, user):
    qs = _base_qs(user)
    filterset = ChannelFilter(get_params, queryset=qs, user=user)
    rows = list(filterset.qs)
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="channels.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Nomi', 'Telegram ID', 'Akkaunt', 'Invite Link', 'Qo\'shilgan sana'])
    for c in rows:
        writer.writerow([
            c.id,
            c.name,
            c.telegram_id,
            str(c.owner),
            c.invite_link or '',
            c.created_at.strftime('%Y-%m-%d %H:%M'),
        ])
    return response


@sync_to_async
def _export_selected(user, ids):
    qs = _base_qs(user).filter(id__in=ids)
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="channels_selected.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Nomi', 'Telegram ID', 'Akkaunt', 'Invite Link', 'Qo\'shilgan sana'])
    for c in qs:
        writer.writerow([
            c.id, c.name, c.telegram_id, str(c.owner),
            c.invite_link or '', c.created_at.strftime('%Y-%m-%d %H:%M'),
        ])
    return response


@sync_to_async
def _delete_channels(ids, user):
    return Channel.objects.filter(id__in=ids, owner__owner=user).delete()


async def channel_list(request):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    action = request.POST.get('bulk_action') if request.method == 'POST' else None
    selected_ids = [int(i) for i in request.POST.getlist('selected')] if request.method == 'POST' else []

    if action == 'export_filtered':
        return await _export_channels(request.GET, user)

    if action == 'export_selected':
        return await _export_selected(user, selected_ids)

    if action == 'delete' and selected_ids:
        result = await _delete_channels(selected_ids, user)
        n = result[0] if result else 0
        messages.success(request, f"{n} ta kanal o'chirildi.")
        return redirect('channels:list')

    filterset, channels = await _process_channels(request.GET, user)
    return await render_async(request, 'channels/list.html', {
        'filterset': filterset,
        'channels': channels,
    })
