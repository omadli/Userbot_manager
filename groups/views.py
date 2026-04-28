from django.shortcuts import redirect
from django.contrib import messages
from django.http import HttpResponse
from asgiref.sync import sync_to_async
from django.conf import settings
from .models import Group
from .filters import GroupFilter
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
    """Groups visible to the user = groups whose owning Account belongs to the user."""
    return (
        Group.objects.filter(owner__owner=user)
        .select_related('owner')
        .order_by('-created_at')
    )


@sync_to_async
def _process_groups(get_params, user):
    qs = _base_qs(user)
    filterset = GroupFilter(get_params, queryset=qs, user=user)
    _ = filterset.form  # force evaluation
    return filterset, list(filterset.qs)


@sync_to_async
def _export_groups(get_params, user):
    qs = _base_qs(user)
    filterset = GroupFilter(get_params, queryset=qs, user=user)
    rows = list(filterset.qs)
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="groups.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Nomi', 'Telegram ID', 'Akkaunt', 'Invite Link', 'Qo\'shilgan sana'])
    for g in rows:
        writer.writerow([
            g.id,
            g.name,
            g.telegram_id,
            str(g.owner),
            g.invite_link or '',
            g.created_at.strftime('%Y-%m-%d %H:%M'),
        ])
    return response


@sync_to_async
def _export_selected(user, ids):
    qs = _base_qs(user).filter(id__in=ids)
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="groups_selected.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Nomi', 'Telegram ID', 'Akkaunt', 'Invite Link', 'Qo\'shilgan sana'])
    for g in qs:
        writer.writerow([
            g.id, g.name, g.telegram_id, str(g.owner),
            g.invite_link or '', g.created_at.strftime('%Y-%m-%d %H:%M'),
        ])
    return response


@sync_to_async
def _delete_groups(ids, user):
    # Only delete rows the user actually owns.
    return Group.objects.filter(id__in=ids, owner__owner=user).delete()


async def group_list(request):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    action = request.POST.get('bulk_action') if request.method == 'POST' else None
    selected_ids = [int(i) for i in request.POST.getlist('selected')] if request.method == 'POST' else []

    if action == 'export_filtered':
        return await _export_groups(request.GET, user)

    if action == 'export_selected':
        return await _export_selected(user, selected_ids)

    if action == 'delete' and selected_ids:
        result = await _delete_groups(selected_ids, user)
        n = result[0] if result else 0
        messages.success(request, f"{n} ta guruh o'chirildi.")
        return redirect('groups:list')

    filterset, groups = await _process_groups(request.GET, user)
    return await render_async(request, 'groups/list.html', {
        'filterset': filterset,
        'groups': groups,
    })
