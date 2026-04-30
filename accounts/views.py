from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import aget_user
from .models import Account, DeviceSetting, Proxy
from groups.models import Group
from channels.models import Channel
from .filters import AccountFilter
from django.contrib import messages
from asgiref.sync import sync_to_async
from .services import (
    send_code, verify_login, get_dialogs, get_and_download_avatar,
    check_spam, check_session, check_proxy, reset_quota,
    fetch_telegram_login_code,
)
import csv
from django.http import HttpResponse, JsonResponse
from django.db import connection
from django.db.models import Count, Q
from django.utils import timezone
import os
from django.conf import settings


@sync_to_async
def _save_user_profile(user, post):
    """Apply username/name/email changes (sync — touches User model)."""
    from django.contrib.auth.models import User as DjangoUser
    new_username = (post.get('username') or '').strip()
    new_email = (post.get('email') or '').strip()
    new_first = (post.get('first_name') or '').strip()
    new_last = (post.get('last_name') or '').strip()

    if not new_username:
        return False, "Username bo'sh bo'lmasin"
    if new_username != user.username:
        if DjangoUser.objects.filter(username=new_username).exclude(pk=user.pk).exists():
            return False, f"'{new_username}' allaqachon band"
        user.username = new_username
    user.email = new_email
    user.first_name = new_first[:150]
    user.last_name = new_last[:150]
    user.save()
    return True, None


@sync_to_async
def _change_user_password(user, old, new1, new2):
    """Validate + persist a password change. Returns (ok, message)."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    if not user.check_password(old):
        return False, "Hozirgi parol noto'g'ri"
    if new1 != new2:
        return False, "Yangi parollar mos kelmaydi"
    try:
        validate_password(new1, user=user)
    except ValidationError as e:
        return False, " ".join(e.messages)
    user.set_password(new1)
    user.save()
    return True, None


async def profile(request):
    """Self-service profile editor: username, names, email, password."""
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_profile':
            ok, err = await _save_user_profile(user, request.POST)
            if ok:
                messages.success(request, "Profil ma'lumotlari yangilandi")
            else:
                messages.error(request, err)
        elif action == 'change_password':
            ok, err = await _change_user_password(
                user,
                request.POST.get('old_password', ''),
                request.POST.get('new_password1', ''),
                request.POST.get('new_password2', ''),
            )
            if ok:
                messages.success(
                    request,
                    "Parol o'zgartirildi — yangi sessiya uchun qayta kiring",
                )
                # Force re-login: drop the session
                from django.contrib.auth import alogout
                await alogout(request)
                return redirect('login')
            else:
                messages.error(request, err)
        return redirect('accounts:profile')

    return await render_async(request, 'accounts/profile.html', {})


@sync_to_async
def _update_account_tags(user, pk, tag_ids):
    """Replace an account's tag set. Caller already auth-scoped via owner=user;
    Tag rows are also filtered to the same owner so users can't attach
    another tenant's tag to their account."""
    from .models import Tag as _Tag
    try:
        acc = Account.objects.select_related().get(pk=pk, owner=user)
    except Account.DoesNotExist:
        return None
    valid = list(_Tag.objects.filter(owner=user, pk__in=tag_ids))
    acc.tags.set(valid)
    return list(acc.tags.all())


async def account_tags_set(request, pk):
    """AJAX endpoint for inline tag editing on the dashboard.

    POST: tag_ids = [1, 2, 3]  (form-encoded list)
    Response: {ok: true, tags: [{id, name}, ...]}
    """
    user = await _require_login(request)
    if user is None:
        return JsonResponse({'error': 'auth'}, status=401)
    if request.method != 'POST':
        return JsonResponse({'error': 'method'}, status=405)

    raw = request.POST.getlist('tag_ids')
    try:
        tag_ids = [int(x) for x in raw if x]
    except ValueError:
        return JsonResponse({'error': 'bad_ids'}, status=400)

    tags = await _update_account_tags(user, pk, tag_ids)
    if tags is None:
        return JsonResponse({'error': 'not_found'}, status=404)
    return JsonResponse({
        'ok': True,
        'tags': [{'id': t.pk, 'name': t.name} for t in tags],
    })


async def healthz(request):
    """Liveness/readiness probe for host nginx and Docker healthcheck.

    Pings the DB with `SELECT 1` and returns 200 on success, 503 otherwise.
    No auth — safe to expose because it returns no app data.
    """
    @sync_to_async
    def _ping():
        with connection.cursor() as c:
            c.execute("SELECT 1")
            row = c.fetchone()
        return row and row[0] == 1

    try:
        ok = await _ping()
    except Exception as e:
        return JsonResponse({'status': 'fail', 'error': str(e)[:120]}, status=503)
    return JsonResponse({'status': 'ok'}, status=200 if ok else 503)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def render_async(*args, **kwargs):
    """Async-safe shortcut for render()."""
    return await sync_to_async(render)(*args, **kwargs)


async def _require_login(request):
    """
    Async replacement for @login_required.
    Returns the authenticated user or None.
    If None, the caller should redirect to login.
    """
    user = await request.auser()
    if not user.is_authenticated:
        return None
    return user


@sync_to_async
def process_dashboard_sync(request_get, request_post, action, selected_ids, user):
    # Owner-scoped base queryset — never leak other users' accounts.
    accounts_list = Account.objects.filter(owner=user).prefetch_related('tags').annotate(
        groups_count=Count('groups', distinct=True),
        channels_count=Count('channels', distinct=True)
    ).order_by('-created_at')

    filterset = AccountFilter(request_get, queryset=accounts_list, user=user)
    _ = filterset.form  # force form evaluation
    qs = filterset.qs

    if action == 'export_filtered':
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = 'attachment; filename="userbots_filtered.csv"'
        writer = csv.writer(response)
        writer.writerow(['ID', 'Phone Number', 'First Name', 'Last Name', 'Username', 'Country', 'Groups', 'Channels', 'Active', 'Spam'])
        for bot in qs:
            writer.writerow([bot.id, bot.phone_number, bot.first_name, bot.last_name, bot.username, bot.country_code, bot.groups_count, bot.channels_count, bot.is_active, bot.is_spam])
        return response, None

    if action == 'export_selected':
        target_qs = qs.filter(id__in=selected_ids)
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = 'attachment; filename="userbots_selected.csv"'
        writer = csv.writer(response)
        writer.writerow(['ID', 'Phone Number', 'First Name', 'Last Name', 'Username', 'Country', 'Groups', 'Channels', 'Active', 'Spam'])
        for bot in target_qs:
            writer.writerow([bot.id, bot.phone_number, bot.first_name, bot.last_name, bot.username, bot.country_code, bot.groups_count, bot.channels_count, bot.is_active, bot.is_spam])
        return response, None

    # "Select all matching filter" — POST flag overrides the per-row checkboxes.
    # Used by the dashboard's "Filterdagi hammasini" button and by the Quick
    # Action cards (which apply a fixed filter and submit immediately).
    select_all_filter = (request_post.get('select_all_filter') == '1') if request_post else False
    if select_all_filter:
        target = list(qs)
    else:
        target = list(qs.filter(id__in=selected_ids))

    return None, {
        "filterset": filterset,
        "accounts": list(qs),
        "target_qs": target,
        "filter_count": qs.count(),
    }


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

async def dashboard(request):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    action = request.POST.get('bulk_action') if request.method == 'POST' else None
    selected_ids = request.POST.getlist('selected') if request.method == 'POST' else []

    response, ctx = await process_dashboard_sync(request.GET, request.POST, action, selected_ids, user)
    if response:
        return response

    if request.method == "POST":
        # When "select all matching filter" is on (Quick Actions or the
        # smart-select button), selected_ids is empty but target_qs holds
        # everyone matching the GET filter — derive the action ids from there.
        select_all_filter = request.POST.get('select_all_filter') == '1'
        if select_all_filter:
            selected_ids = [str(a.id) for a in ctx["target_qs"]]

        if not selected_ids:
            messages.warning(request, "Hech qanday akkaunt tanlanmagan.")
            return redirect('accounts:dashboard')

        if action in ('create_groups', 'create_channels', 'join_channel',
                      'leave_groups', 'leave_channels',
                      'send_message', 'update_profile', 'view_stories',
                      'mark_all_read', 'set_2fa_password',
                      'boost_views', 'react_to_post', 'vote_poll',
                      'press_start', 'run_script', 'account_warming'):
            from urllib.parse import urlencode
            from django.urls import reverse
            # run_script is admin-only; let the jobs view itself reject if not.
            qs = urlencode([('account_ids', i) for i in selected_ids])
            url_map = {
                'create_groups':    'jobs:task_create_groups',
                'create_channels':  'jobs:task_create_channels',
                'join_channel':     'jobs:task_create_join_channel',
                'leave_groups':     'jobs:task_create_leave_groups',
                'leave_channels':   'jobs:task_create_leave_channels',
                'send_message':     'jobs:task_create_send_message',
                'update_profile':   'jobs:task_create_update_profile',
                'view_stories':     'jobs:task_create_view_stories',
                'mark_all_read':    'jobs:task_create_mark_all_read',
                'set_2fa_password': 'jobs:task_create_set_2fa_password',
                'boost_views':      'jobs:task_create_boost_views',
                'react_to_post':    'jobs:task_create_react_to_post',
                'vote_poll':        'jobs:task_create_vote_poll',
                'press_start':      'jobs:task_create_press_start',
                'run_script':       'jobs:task_create_run_script',
                'account_warming':  'jobs:task_create_account_warming',
            }
            return redirect(f"{reverse(url_map[action])}?{qs}")

        target_qs = ctx["target_qs"]
        ids = [a.id for a in target_qs]

        if action == 'delete':
            await Account.objects.filter(id__in=ids, owner=user).adelete()
            messages.success(request, f"{len(ids)} ta akkaunt o'chirildi.")
        elif action == 'deactivate':
            await Account.objects.filter(id__in=ids, owner=user).aupdate(is_active=False)
            messages.success(request, f"{len(ids)} ta akkaunt faolsizlantirildi.")
        elif action == 'check_spam':
            spam_count = 0
            ok_count = 0
            for account in target_qs:
                if not account.session_string:
                    continue
                device = await sync_to_async(lambda a: a.device_setting)(account)
                is_spam = await check_spam(
                    account.session_string,
                    device_setting=device,
                    api_id=account.api_id,
                    api_hash=account.api_hash,
                )
                await Account.objects.filter(pk=account.pk).aupdate(is_spam=is_spam)
                if is_spam:
                    spam_count += 1
                else:
                    ok_count += 1
            messages.info(
                request,
                f"Spam tekshiruvi tugadi: {spam_count} ta spam, {ok_count} ta toza."
            )

        return redirect('accounts:dashboard')

    @sync_to_async
    def _user_tags():
        from .models import Tag as _T
        return list(_T.objects.filter(owner=user).order_by('name'))

    return await render_async(request, 'accounts/dashboard.html', {
        'filterset': ctx['filterset'],
        'accounts': ctx['accounts'],
        'filter_count': ctx.get('filter_count', 0),
        'all_tags': await _user_tags(),
    })


@sync_to_async
def _get_device_settings():
    return list(DeviceSetting.objects.order_by('name'))


@sync_to_async
def _get_device_by_pk(pk):
    try:
        return DeviceSetting.objects.get(pk=int(pk))
    except (DeviceSetting.DoesNotExist, ValueError, TypeError):
        return None


# Session keys used by the multi-step Telethon login (initiate → verify).
# Centralized so cancel_login + initiate can wipe the same set without drift.
_LOGIN_SESSION_KEYS = (
    'phone_number',
    'phone_code_hash',
    'temp_session_string',
    'needs_password',
    'device_setting_id',
    'account_api_id',
    'account_api_hash',
    'relogin_pk',
)


async def cancel_login(request):
    """Wipe an in-progress Telethon login (e.g., stuck on the 2FA password
    prompt for a phone number that's been blocked) so the user can start
    fresh with a different phone."""
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")
    for key in _LOGIN_SESSION_KEYS:
        request.session.pop(key, None)
    await sync_to_async(request.session.save)()
    messages.info(request, "Login sessiyasi bekor qilindi. Yangi akkaunt qo'shishingiz mumkin.")
    return redirect('accounts:add_account')


async def initiate_telethon_login(request):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    # Defensive: any stale state from a previous (abandoned) login flow is
    # wiped on every visit to the form. Without this, `needs_password=True`
    # from a blocked-phone attempt survives and shows the wrong UI.
    if request.method == "GET":
        for key in _LOGIN_SESSION_KEYS:
            request.session.pop(key, None)
        await sync_to_async(request.session.save)()

    device_settings = await _get_device_settings()

    if request.method == "POST":
        phone_number     = request.POST.get('phone_number')
        device_setting_id = request.POST.get('device_setting_id') or None
        api_id_raw       = request.POST.get('api_id', '').strip()
        api_hash_raw     = request.POST.get('api_hash', '').strip()

        if not phone_number:
            messages.error(request, "Iltimos, telefon raqamini kiriting.")
            return await render_async(request, 'accounts/initiate_login.html', {'device_settings': device_settings})

        exists = await Account.objects.filter(phone_number=phone_number).aexists()
        if exists:
            messages.error(request, "Ushbu raqam bazada allaqachon mavjud!")
            return await render_async(request, 'accounts/initiate_login.html', {'device_settings': device_settings})

        # Resolve optional device setting
        device_setting = await _get_device_by_pk(device_setting_id) if device_setting_id else None
        api_id   = int(api_id_raw)   if api_id_raw.isdigit() else None
        api_hash = api_hash_raw      if api_hash_raw         else None

        result = await send_code(phone_number, device_setting=device_setting, api_id=api_id, api_hash=api_hash)
        if result["success"]:
            request.session['phone_number']        = phone_number
            request.session['phone_code_hash']     = result["phone_code_hash"]
            request.session['temp_session_string'] = result["session_string"]
            request.session['device_setting_id']   = device_setting.pk if device_setting else None
            request.session['account_api_id']      = api_id
            request.session['account_api_hash']    = api_hash
            await sync_to_async(request.session.save)()
            messages.success(request, f"Kod yuborildi: {phone_number}.")
            return redirect('accounts:verify_login')
        else:
            messages.error(request, f"Xatolik: {result['error']}")

    return await render_async(request, 'accounts/initiate_login.html', {'device_settings': device_settings})


@sync_to_async
def fetch_account_email_sync(phone):
    acc = Account.objects.filter(phone_number=phone).first()
    return acc.email if acc else ""


async def verify_telethon_login(request):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    phone_number = request.session.get('phone_number')
    phone_code_hash = request.session.get('phone_code_hash')
    temp_session = request.session.get('temp_session_string')
    needs_password = request.session.get('needs_password', False)

    # Also read device/API from session
    device_setting_id = request.session.get('device_setting_id')
    account_api_id    = request.session.get('account_api_id')
    account_api_hash  = request.session.get('account_api_hash')
    device_setting    = await _get_device_by_pk(device_setting_id) if device_setting_id else None

    if not phone_number or not temp_session:
        return redirect('accounts:add_account')

    if request.method == "POST":
        code     = request.POST.get('code')
        password = request.POST.get('password')
        email    = request.POST.get('email')

        if needs_password and not password:
            messages.error(request, "2FA parolini kiritish majburiy.")
            return await render_async(request, 'accounts/verify_login.html', {
                "phone_number": phone_number, "needs_password": True, "email": email
            })

        result = await verify_login(
            phone_number, phone_code_hash, code, temp_session, password,
            device_setting=device_setting,
            api_id=account_api_id,
            api_hash=account_api_hash,
        )

        if result["success"]:
            relogin_pk = request.session.get('relogin_pk')

            if relogin_pk:
                # ── Relogin: update only session_string + is_active ──
                await Account.objects.filter(pk=relogin_pk).aupdate(
                    session_string=result["session_string"],
                    is_active=True,
                )
                cleanup_keys = [
                    'phone_number', 'phone_code_hash', 'temp_session_string',
                    'needs_password', 'device_setting_id', 'account_api_id',
                    'account_api_hash', 'relogin_pk',
                ]
                for key in cleanup_keys:
                    request.session.pop(key, None)
                await sync_to_async(request.session.save)()
                messages.success(request, "Sessiya muvaffaqiyatli yangilandi!")
                return redirect('accounts:account_detail', pk=relogin_pk)

            else:
                # ── Fresh login: create or update full account ──
                await Account.objects.aupdate_or_create(
                    phone_number=phone_number,
                    defaults={
                        "session_string":  result["session_string"],
                        "user_id":         result.get("user_id"),
                        "first_name":      result.get("first_name", ""),
                        "last_name":       result.get("last_name", ""),
                        "username":        result.get("username", ""),
                        "avatar":          result.get("avatar"),
                        "two_fa_password": password,
                        "email":           email,
                        "owner_id":        user.id,
                        "device_setting":  device_setting,
                        "api_id":          account_api_id,
                        "api_hash":        account_api_hash,
                    }
                )
                cleanup_keys = [
                    'phone_number', 'phone_code_hash', 'temp_session_string',
                    'needs_password', 'device_setting_id', 'account_api_id', 'account_api_hash',
                ]
                for key in cleanup_keys:
                    request.session.pop(key, None)
                await sync_to_async(request.session.save)()
                messages.success(request, "Muvaffaqiyatli ulashildi va akkaunt saqlandi!")
                return redirect('accounts:dashboard')

        elif result.get("needs_password"):
            request.session['needs_password']      = True
            request.session['temp_session_string'] = result.get("session_string", temp_session)
            await sync_to_async(request.session.save)()
            messages.warning(request, "Ikki bosqichli autentifikatsiya (2FA) paroli kerak.")
            return await render_async(request, 'accounts/verify_login.html', {
                "phone_number": phone_number, "needs_password": True, "email": email
            })
        else:
            err = result.get('error', "Noma'lum xatolik")
            messages.error(request, f"Xatolik: {err}")
            if "session_string" in result:
                request.session['temp_session_string'] = result["session_string"]
                await sync_to_async(request.session.save)()

    email_val = await fetch_account_email_sync(phone_number)
    return await render_async(request, 'accounts/verify_login.html', {
        "phone_number": phone_number, "needs_password": needs_password, "email": email_val
    })


@sync_to_async
def get_object_sync(model, pk, **filters):
    return get_object_or_404(model, pk=pk, **filters)


@sync_to_async
def _load_account_for_relogin(pk, user):
    """Returns (phone, device_setting, api_id, api_hash) — all resolved in sync context.
    404 if the account doesn't belong to the given user."""
    account = get_object_or_404(
        Account.objects.filter(owner=user).select_related('device_setting'),
        pk=pk,
    )
    return (
        account.phone_number,
        account.device_setting,   # already loaded by select_related
        account.api_id,
        account.api_hash,
    )


async def relogin_account(request, pk):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    phone, device, api_id, api_hash = await _load_account_for_relogin(pk, user)

    result = await send_code(
        phone,
        device_setting=device,
        api_id=api_id,
        api_hash=api_hash,
    )

    if result["success"]:
        # Clear ALL stale session state first
        stale_keys = [
            'phone_number', 'phone_code_hash', 'temp_session_string',
            'needs_password', 'device_setting_id', 'account_api_id',
            'account_api_hash', 'relogin_pk',
        ]
        for key in stale_keys:
            request.session.pop(key, None)

        request.session['phone_number']        = phone
        request.session['phone_code_hash']     = result["phone_code_hash"]
        request.session['temp_session_string'] = result["session_string"]
        request.session['device_setting_id']   = device.pk if device else None
        request.session['account_api_id']      = int(api_id) if api_id else None
        request.session['account_api_hash']    = api_hash
        request.session['relogin_pk']          = pk
        await sync_to_async(request.session.save)()
        messages.success(request, f"Tasdiqlash kodi yuborildi: {phone}")
        return redirect('accounts:verify_login')
    else:
        messages.error(request, f"Qayta kirishda xatolik: {result['error']}")
        return redirect('accounts:account_detail', pk=pk)


async def edit_account(request, pk):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    account = await get_object_sync(Account, pk, owner=user)
    if request.method == "POST":
        account.email = request.POST.get('email') or None
        account.two_fa_password = request.POST.get('two_fa_password') or None
        await account.asave()
        messages.success(request, f"{account.phone_number} ma'lumotlari yangilandi.")
    return redirect('accounts:dashboard')


def _attach_or_create_tag(account_pk, name, user):
    """Create (or reuse) a tag owned by `user` and attach to their account."""
    from .models import Tag
    try:
        account = Account.objects.get(pk=account_pk, owner=user)
    except Account.DoesNotExist:
        return
    tag, _ = Tag.objects.get_or_create(owner=user, name=name)
    account.tags.add(tag)


def _attach_tag_by_id(account_pk, tag_id, user):
    from .models import Tag
    try:
        tag = Tag.objects.get(pk=tag_id, owner=user)
        account = Account.objects.get(pk=account_pk, owner=user)
        account.tags.add(tag)
    except (Tag.DoesNotExist, Account.DoesNotExist):
        pass


def _detach_tag(account_pk, tag_id, user):
    from .models import Tag
    try:
        tag = Tag.objects.get(pk=tag_id, owner=user)
        account = Account.objects.get(pk=account_pk, owner=user)
        account.tags.remove(tag)
    except (Tag.DoesNotExist, Account.DoesNotExist):
        pass


@sync_to_async
def _load_account_detail(pk, user):
    """Load account with device_setting, device options, groups, channels, and tags — owner-scoped."""
    from .models import Tag
    account = get_object_or_404(
        Account.objects.filter(owner=user)
        .select_related('device_setting', 'proxy')
        .prefetch_related('tags'),
        pk=pk,
    )
    device_settings  = list(DeviceSetting.objects.order_by('name'))
    proxies          = list(Proxy.objects.filter(owner=user).order_by('name'))
    groups           = list(account.groups.all().order_by('-created_at'))
    channels         = list(account.channels.all().order_by('-created_at'))
    all_tags         = list(Tag.objects.filter(owner=user).order_by('name'))
    account_tag_ids  = list(account.tags.values_list('id', flat=True))
    return account, device_settings, proxies, groups, channels, all_tags, account_tag_ids


@sync_to_async
def _save_account_fields(pk, data, user):
    account = Account.objects.filter(owner=user).select_related('device_setting', 'proxy').get(pk=pk)
    account.email           = data.get('email') or None
    account.two_fa_password = data.get('two_fa_password') or None

    api_id_raw  = str(data.get('api_id', '')).strip()
    api_hash_raw = str(data.get('api_hash', '')).strip()
    account.api_id   = int(api_id_raw)  if api_id_raw.isdigit() else None
    account.api_hash = api_hash_raw     if api_hash_raw          else None

    # Daily quota — 0 means unlimited.
    limit_raw = str(data.get('daily_op_limit', '')).strip()
    if limit_raw.isdigit():
        account.daily_op_limit = int(limit_raw)

    ds_id = data.get('device_setting_id')
    if ds_id:
        try:
            account.device_setting = DeviceSetting.objects.get(pk=int(ds_id))
        except (DeviceSetting.DoesNotExist, ValueError):
            account.device_setting = None
    else:
        account.device_setting = None

    proxy_id = data.get('proxy_id')
    if proxy_id:
        try:
            account.proxy = Proxy.objects.get(pk=int(proxy_id), owner=user)
        except (Proxy.DoesNotExist, ValueError):
            account.proxy = None
    else:
        account.proxy = None

    account.save()
    return account


async def account_detail(request, pk):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    account, device_settings, proxies, groups, channels, all_tags, account_tag_ids = await _load_account_detail(pk, user)

    if request.method == "POST":
        action = request.POST.get('action')

        # ── Check session ──────────────────────────────────────────────
        if action == 'check_session':
            device = await sync_to_async(lambda: account.device_setting)()
            alive = await check_session(
                account.session_string,
                device_setting=device,
                api_id=account.api_id,
                api_hash=account.api_hash,
            )
            if alive:
                await Account.objects.filter(pk=pk, owner=user).aupdate(is_active=True)
                messages.success(request, "✅ Sessiya faol — akkaunt aktivlashtirildi.")
            else:
                await Account.objects.filter(pk=pk, owner=user).aupdate(is_active=False)
                messages.warning(request, "⚠️ Sessiya tugagan — akkaunt nofaol qilindi.")
            return redirect('accounts:account_detail', pk=pk)

        # ── Check spam ─────────────────────────────────────────────────
        elif action == 'check_spam':
            if not account.session_string:
                messages.error(request, "Sessiya mavjud emas.")
                return redirect('accounts:account_detail', pk=pk)
            device = await sync_to_async(lambda: account.device_setting)()
            is_spam = await check_spam(
                account.session_string,
                device_setting=device,
                api_id=account.api_id,
                api_hash=account.api_hash,
            )
            await Account.objects.filter(pk=pk, owner=user).aupdate(is_spam=is_spam)
            if is_spam:
                messages.error(request, "🚫 Akkaunt SPAM chekloviga duchor bo'lgan.")
            else:
                messages.success(request, "✅ Akkaunt spam emas.")
            return redirect('accounts:account_detail', pk=pk)

        # ── Save fields ────────────────────────────────────────────────
        elif action == 'save':
            await _save_account_fields(pk, request.POST, user)
            messages.success(request, "Ma'lumotlar saqlandi.")
            return redirect('accounts:account_detail', pk=pk)

        # ── Reset daily quota ──────────────────────────────────────────
        elif action == 'reset_quota':
            await reset_quota(pk)
            messages.success(request, "Kunlik byudjet qayta boshlandi.")
            return redirect('accounts:account_detail', pk=pk)

        # ── Add tag ───────────────────────────────────────────────────
        elif action == 'add_tag':
            tag_id = request.POST.get('tag_id')
            new_tag_name = request.POST.get('new_tag_name', '').strip()
            if new_tag_name:
                await sync_to_async(_attach_or_create_tag)(pk, name=new_tag_name, user=user)
                messages.success(request, f"'{new_tag_name}' tegi qo'shildi.")
            elif tag_id:
                await sync_to_async(_attach_tag_by_id)(pk, int(tag_id), user=user)
                messages.success(request, "Teg biriktirildi.")
            return redirect('accounts:account_detail', pk=pk)

        # ── Remove tag ─────────────────────────────────────────────────
        elif action == 'remove_tag':
            tag_id = request.POST.get('tag_id')
            if tag_id:
                await sync_to_async(_detach_tag)(pk, int(tag_id), user=user)
                messages.success(request, "Teg o'chirildi.")
            return redirect('accounts:account_detail', pk=pk)

    # Refresh avatar
    account, device_settings, proxies, groups, channels, all_tags, account_tag_ids = await _load_account_detail(pk, user)
    if account.session_string:
        try:
            media_avatars = os.path.join(settings.MEDIA_ROOT, 'avatars')
            os.makedirs(media_avatars, exist_ok=True)
            safe_filename = f"{str(account.phone_number).replace('+', '')}.jpg"
            file_path = os.path.join(media_avatars, safe_filename)
            downloaded = await get_and_download_avatar(account.session_string, file_path)
            if downloaded and os.path.exists(file_path):
                account.avatar = f"avatars/{safe_filename}?v={os.path.getmtime(file_path)}"
            else:
                account.avatar = None
            await account.asave(update_fields=['avatar'])
        except Exception:
            pass

    return await render_async(request, 'accounts/detail.html', {
        'account': account,
        'device_settings': device_settings,
        'proxies': proxies,
        'groups': groups,
        'channels': channels,
        'all_tags': all_tags,
        'account_tag_ids': account_tag_ids,
    })


@sync_to_async
def get_dialogs_sync(account):
    return (list(account.groups.all().order_by('-created_at')), list(account.channels.all().order_by('-created_at')))


async def account_get_code(request, pk):
    """Page that fetches the latest Telegram login code via this account's
    saved session. Useful when the user lost their phone/PC session and
    needs to log back in elsewhere using a code from another active
    session — this app holds an active one in the DB."""
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    account = await get_object_sync(Account, pk, owner=user)

    if not account.session_string:
        messages.error(request, "Akkauntda sessiya yo'q — kod olib bo'lmaydi")
        return redirect('accounts:account_detail', pk=pk)

    # GET-with-?wait=1 (default) → block + listen up to 30s, then render.
    # GET-with-?wait=0 → only check history, return immediately.
    wait_param = request.GET.get('wait', '1')
    wait_seconds = 30 if wait_param != '0' else 0

    result = await fetch_telegram_login_code(account, wait_seconds=wait_seconds)

    return await render_async(request, 'accounts/get_code.html', {
        'account': account,
        'result': result,
        'waited': wait_seconds,
    })


async def account_live_chats(request, pk):
    """Live view of all groups/channels the account is a member of, with
    bulk-leave checkboxes. Hits the Telegram API on every load — slow but
    always fresh. POST queues a leave_chats task with the selected IDs."""
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    account = await get_object_sync(Account, pk, owner=user)

    if request.method == 'POST':
        from jobs.models import Task as JobTask
        raw_ids = request.POST.getlist('chat_ids')
        try:
            chat_ids = [int(x) for x in raw_ids]
        except (TypeError, ValueError):
            chat_ids = []
        if not chat_ids:
            messages.warning(request, "Hech qanday chat tanlanmagan")
            return redirect('accounts:account_live_chats', pk=pk)

        params = {
            'account_ids': [account.pk],
            'chat_ids': chat_ids,
            'delay_min_sec': 2,
            'delay_max_sec': 6,
            'concurrency': 1,
            'skip_inactive': False,  # acting on this exact account, ignore filter
            'skip_spam': False,
        }
        task = await JobTask.objects.acreate(
            kind='leave_groups',  # runner ignores `kind` when chat_ids set
            owner=user,
            params=params,
        )
        messages.success(
            request,
            f"Vazifa #{task.pk} navbatga qo'yildi — {len(chat_ids)} ta chatdan chiqish.",
        )
        return redirect('jobs:task_detail', pk=task.pk)

    if not account.session_string:
        messages.error(request, "Akkauntda sessiya yo'q — chatlarni o'qib bo'lmaydi")
        return redirect('accounts:account_detail', pk=pk)

    from jobs.services import list_dialogs_for_account
    result = await list_dialogs_for_account(account)
    if not result['success']:
        messages.error(request, f"Chatlarni olishda xato: {result['error']}")
        return redirect('accounts:account_detail', pk=pk)

    return await render_async(request, 'accounts/live_chats.html', {
        'account': account,
        'groups': result['groups'],
        'channels': result['channels'],
    })


async def account_chat_detail(request, pk, chat_id):
    """Telegram-style chat viewer: last ~40 messages from one chat, plus
    a textbox to send a new message. For broadcast channels with a linked
    discussion group, each post grows a "Comment" inline form that posts
    via comment_to=<msg_id>.

    Hits the Telegram API on every load — typically <2s for chats with
    cached entities. Posting is also synchronous; the result message
    flashes back via Django messages.
    """
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    account = await get_object_sync(Account, pk, owner=user)
    if not account.session_string:
        messages.error(request, "Akkauntda sessiya yo'q — chatni ochib bo'lmaydi")
        return redirect('accounts:account_detail', pk=pk)

    from jobs.services import (
        fetch_chat_messages_for_account, send_chat_message_for_account,
    )

    if request.method == 'POST':
        text = (request.POST.get('text') or '').strip()
        comment_to = request.POST.get('comment_to') or None
        if comment_to and comment_to.isdigit():
            comment_to = int(comment_to)
        else:
            comment_to = None

        if not text:
            messages.warning(request, "Xabar bo'sh")
            return redirect('accounts:account_chat_detail', pk=pk, chat_id=chat_id)

        result = await send_chat_message_for_account(
            account, chat_id, text, comment_to=comment_to,
        )
        if result['success']:
            messages.success(
                request,
                "Sharh yuborildi" if comment_to else "Xabar yuborildi",
            )
        else:
            messages.error(request, f"Xato: {result['error']}")
        return redirect('accounts:account_chat_detail', pk=pk, chat_id=chat_id)

    result = await fetch_chat_messages_for_account(account, chat_id)
    if not result['success']:
        messages.error(request, f"Chatni ochishda xato: {result['error']}")
        return redirect('accounts:account_live_chats', pk=pk)

    return await render_async(request, 'accounts/chat_detail.html', {
        'account': account,
        'chat': result['chat'],
        'messages_list': result['messages'],
    })


async def account_dialogs(request, pk):
    """Kept for URL compatibility — redirects to the unified detail page."""
    return redirect('accounts:account_detail', pk=pk)

    account = await get_object_sync(Account, pk)

    if account.session_string:
        try:
            media_avatars = os.path.join(settings.MEDIA_ROOT, 'avatars')
            os.makedirs(media_avatars, exist_ok=True)
            safe_filename = f"{str(account.phone_number).replace('+', '')}.jpg"
            file_path = os.path.join(media_avatars, safe_filename)

            downloaded = await get_and_download_avatar(account.session_string, file_path)
            if downloaded and os.path.exists(file_path):
                account.avatar = f"avatars/{safe_filename}?v={os.path.getmtime(file_path)}"
            else:
                account.avatar = None
            await account.asave(update_fields=['avatar'])
        except Exception:
            pass

    groups, channels = await get_dialogs_sync(account)

    return await render_async(request, 'accounts/dialogs.html', {
        "account": account,
        "groups": groups,
        "channels": channels
    })


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@sync_to_async
def _get_all_tags(user):
    from .models import Tag
    # Count only the user's own accounts — don't leak other users' usage stats.
    return list(
        Tag.objects.filter(owner=user).annotate(
            account_count=Count('accounts', filter=Q(accounts__owner=user), distinct=True)
        ).order_by('name')
    )


@sync_to_async
def _create_tag(name, user):
    from .models import Tag
    tag, created = Tag.objects.get_or_create(owner=user, name=name.strip())
    return tag, created


@sync_to_async
def _delete_tag(pk, user):
    from .models import Tag
    Tag.objects.filter(pk=pk, owner=user).delete()


async def tag_list(request):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    if request.method == "POST":
        action = request.POST.get('action')
        if action == 'create':
            name = request.POST.get('name', '').strip()
            if name:
                tag, created = await _create_tag(name, user)
                if created:
                    messages.success(request, f"'{tag.name}' tegi yaratildi.")
                else:
                    messages.warning(request, f"'{tag.name}' tegi allaqachon mavjud.")
            else:
                messages.error(request, "Teg nomi bo'sh bo'lishi mumkin emas.")
        elif action == 'delete':
            pk = request.POST.get('pk')
            if pk:
                await _delete_tag(int(pk), user)
                messages.success(request, "Teg o'chirildi.")
        return redirect('accounts:tag_list')

    tags = await _get_all_tags(user)
    return await render_async(request, 'accounts/tags.html', {'tags': tags})


# ---------------------------------------------------------------------------
# Proxies
# ---------------------------------------------------------------------------

@sync_to_async
def _list_proxies(user):
    return list(
        Proxy.objects.filter(owner=user)
        .annotate(account_count=Count('accounts', distinct=True))
        .order_by('-created_at')
    )


@sync_to_async
def _get_proxy(user, pk):
    return get_object_or_404(Proxy, pk=pk, owner=user)


def _proxy_from_post(post, user, existing=None):
    """Build or update a Proxy from form POST. Returns (proxy, error_msg)."""
    name = (post.get('name') or '').strip()
    proxy_type = post.get('proxy_type') or 'socks5'
    host = (post.get('host') or '').strip()
    port_raw = post.get('port') or ''
    username = (post.get('username') or '').strip()
    password = post.get('password') or ''
    secret = (post.get('secret') or '').strip()
    is_active = post.get('is_active') == 'on'

    if not name:
        return None, "Nom bo'sh"
    if not host:
        return None, "Host bo'sh"
    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        return None, "Port 1-65535 oralig'ida bo'lsin"
    if proxy_type not in ('socks5', 'mtproto'):
        return None, "Proxy turi noto'g'ri"
    if proxy_type == 'mtproto' and not secret:
        return None, "MTProxy uchun secret kerak"

    if existing is None:
        proxy = Proxy(owner=user)
    else:
        proxy = existing
    proxy.name = name
    proxy.proxy_type = proxy_type
    proxy.host = host
    proxy.port = port
    proxy.username = username
    proxy.password = password
    proxy.secret = secret
    proxy.is_active = is_active
    return proxy, None


async def proxy_list(request):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            proxy, err = _proxy_from_post(request.POST, user)
            if err:
                messages.error(request, err)
            else:
                await proxy.asave()
                messages.success(request, f"'{proxy.name}' qo'shildi")
                return redirect('accounts:proxy_list')
        elif action == 'delete':
            pk = request.POST.get('pk')
            if pk and pk.isdigit():
                await Proxy.objects.filter(pk=int(pk), owner=user).adelete()
                messages.success(request, "Proxy o'chirildi")
        elif action == 'check':
            pk = request.POST.get('pk')
            if pk and pk.isdigit():
                proxy = await _get_proxy(user, int(pk))
                ok, err = await check_proxy(proxy)
                await Proxy.objects.filter(pk=proxy.pk).aupdate(
                    last_checked_at=timezone.now(),
                    last_check_ok=ok,
                    last_check_error=err or '',
                )
                if ok:
                    messages.success(request, f"✓ {proxy.name}: ulanish muvaffaqiyatli ({err})" if err else f"✓ {proxy.name}: OK")
                else:
                    messages.error(request, f"✗ {proxy.name}: {err}")
        return redirect('accounts:proxy_list')

    proxies = await _list_proxies(user)
    return await render_async(request, 'accounts/proxy_list.html', {'proxies': proxies})


async def proxy_detail(request, pk):
    user = await _require_login(request)
    if user is None:
        return redirect(f"{settings.LOGIN_URL}?next={request.path}")

    proxy = await _get_proxy(user, pk)

    if request.method == 'POST':
        updated, err = _proxy_from_post(request.POST, user, existing=proxy)
        if err:
            messages.error(request, err)
        else:
            await updated.asave()
            messages.success(request, "Saqlandi")
        return redirect('accounts:proxy_detail', pk=pk)

    return await render_async(request, 'accounts/proxy_detail.html', {'proxy': proxy})
