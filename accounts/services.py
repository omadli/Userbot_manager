from telethon import TelegramClient, connection as tl_connection
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError
from django.conf import settings
import asyncio
from asgiref.sync import sync_to_async


def _get_default_device():
    """Returns DeviceSetting 'default' object (sync helper)."""
    from .models import DeviceSetting
    obj, _ = DeviceSetting.objects.get_or_create(name="default")
    return obj


@sync_to_async
def consume_quota(account_pk):
    """
    Reserve one operation against the account's daily budget.

    Returns (allowed, remaining, limit):
      allowed   — bool; False means the budget is exhausted
      remaining — int;  how many ops are still free today (0 when denied)
      limit     — int;  the daily_op_limit that produced this decision (0 = unlimited)

    Resets the day-bucket lazily when the first operation of a new day arrives.
    Concurrent workers are serialized by the transaction; SQLite ignores
    select_for_update() so only correct on a single worker process.
    """
    from django.utils import timezone
    from django.db import transaction
    from .models import Account

    today = timezone.localdate()
    with transaction.atomic():
        try:
            acc = Account.objects.select_for_update().only(
                'pk', 'daily_op_limit', 'quota_window_start', 'quota_window_count',
            ).get(pk=account_pk)
        except Account.DoesNotExist:
            return (False, 0, 0)

        limit = int(acc.daily_op_limit or 0)

        # Day-bucket reset.
        if acc.quota_window_start != today:
            acc.quota_window_start = today
            acc.quota_window_count = 0

        if limit > 0 and acc.quota_window_count >= limit:
            # Persist the date reset (if any) so the UI shows today's row.
            acc.save(update_fields=['quota_window_start', 'quota_window_count'])
            return (False, 0, limit)

        acc.quota_window_count += 1
        acc.save(update_fields=['quota_window_start', 'quota_window_count'])
        remaining = 0 if limit == 0 else max(0, limit - acc.quota_window_count)
        return (True, remaining, limit)


@sync_to_async
def reset_quota(account_pk):
    """Force-reset today's counter to zero. Called from account detail page."""
    from .models import Account
    Account.objects.filter(pk=account_pk).update(quota_window_count=0)


async def get_client_for_account(account, temp_session_string=None):
    """
    Convenience wrapper around get_client() that pulls per-account device,
    API creds, and proxy off the Account model. Use this everywhere you have
    a full Account instance — avoids forgetting to wire one of the pieces.
    """
    device = await sync_to_async(lambda a: a.device_setting)(account)
    proxy  = await sync_to_async(lambda a: a.proxy)(account)
    return await get_client(
        temp_session_string=temp_session_string or account.session_string,
        device_setting=device,
        api_id=account.api_id,
        api_hash=account.api_hash,
        proxy=proxy,
    )


async def get_client(temp_session_string=None, device_setting=None, api_id=None,
                     api_hash=None, proxy=None):
    """
    Create and connect a TelegramClient.

    - device_setting: a DeviceSetting ORM object (or None → use default)
    - api_id / api_hash: per-account credentials (or None → use global settings)
    - proxy: a `Proxy` ORM object (or None → direct connection)
    """
    resolved_api_id   = api_id   or (int(settings.TELEGRAM_API_ID) if settings.TELEGRAM_API_ID else None)
    resolved_api_hash = api_hash or settings.TELEGRAM_API_HASH

    if device_setting is None:
        device_setting = await sync_to_async(_get_default_device)()

    kwargs = dict(
        api_id=resolved_api_id,
        api_hash=resolved_api_hash,
        device_model=device_setting.device_model,
        system_version=device_setting.system_version,
        app_version=device_setting.app_version,
        lang_code=device_setting.lang_code,
        system_lang_code=device_setting.system_lang_code,
    )

    if proxy is not None:
        proxy_arg = proxy.as_telethon()
        if proxy.proxy_type == 'mtproto':
            kwargs['connection'] = tl_connection.ConnectionTcpMTProxyRandomizedIntermediate
            kwargs['proxy'] = proxy_arg
        else:  # socks5 (or future)
            kwargs['proxy'] = proxy_arg

    client = TelegramClient(
        StringSession(temp_session_string or ""),
        **kwargs,
    )
    await client.connect()
    return client


async def send_code(phone_number, device_setting=None, api_id=None, api_hash=None, proxy=None):
    client = await get_client(
        device_setting=device_setting,
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy,
    )
    try:
        sent_code = await client.send_code_request(phone_number)
        return {
            "success": True,
            "phone_code_hash": sent_code.phone_code_hash,
            "session_string": client.session.save(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


async def check_spam(session_str, device_setting=None, api_id=None, api_hash=None, proxy=None):
    """
    Standalone spam check — call separately after login, not during login.
    Returns True if account is spam-restricted.
    """
    client = await get_client(
        temp_session_string=session_str,
        device_setting=device_setting,
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy,
    )
    try:
        username = await client.get_entity("@spambot")
        await client.send_message(username, "/start")
        await asyncio.sleep(3)
        messages = await client.get_messages(username, limit=1)
        if not messages:
            return False
        text = messages[0].text or ""
        keywords = [
            "Dear", "Hurmatli", "Здравствуйте",
            "Unfortunately", "Afsuski", "К сожалению", "restricting",
        ]
        return any(kw in text for kw in keywords)
    except Exception:
        return False
    finally:
        await client.disconnect()


async def check_proxy(proxy, timeout=10):
    """
    Lightweight proxy health check.

    Opens a connection through the proxy to Telegram's DC2 (149.154.167.51:443)
    — if that handshake completes, the proxy is usable. Returns
    (ok: bool, error: str).
    """
    import socket
    # Lazy import so the rest of the module still loads when python-socks is missing.
    try:
        from python_socks.async_.asyncio import Proxy as AsyncProxy
    except ImportError:
        return (False, "python-socks o'rnatilmagan")

    tg_host, tg_port = '149.154.167.51', 443

    try:
        if proxy.proxy_type == 'socks5':
            pconn = AsyncProxy.from_url(
                f"socks5://{proxy.host}:{proxy.port}"
                if not (proxy.username or proxy.password)
                else f"socks5://{proxy.username}:{proxy.password}@{proxy.host}:{proxy.port}"
            )
            sock = await asyncio.wait_for(
                pconn.connect(dest_host=tg_host, dest_port=tg_port),
                timeout=timeout,
            )
            sock.close()
            return (True, "")

        if proxy.proxy_type == 'mtproto':
            # MTProxy can't be validated via plain TCP — we just check the proxy
            # host is reachable on that port.
            loop = asyncio.get_event_loop()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(proxy.host, int(proxy.port)),
                timeout=timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return (True, "TCP ulanish OK (MTProxy chuqur tekshiruv uchun Telethon session talab qilinadi)")

        return (False, f"Noma'lum proxy turi: {proxy.proxy_type}")

    except asyncio.TimeoutError:
        return (False, f"Timeout ({timeout}s)")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


async def check_session(session_str, device_setting=None, api_id=None, api_hash=None, proxy=None):
    """
    Checks whether a session is still authorized.
    Returns True if session is alive, False if terminated/invalid.
    """
    if not session_str:
        return False
    client = await get_client(
        temp_session_string=session_str,
        device_setting=device_setting,
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy,
    )
    try:
        me = await client.get_me()
        return me is not None
    except Exception:
        return False
    finally:
        await client.disconnect()



async def verify_login(phone_number, phone_code_hash, code, temp_session,
                       password=None, device_setting=None, api_id=None, api_hash=None,
                       proxy=None):
    client = await get_client(
        temp_session_string=temp_session,
        device_setting=device_setting,
        api_id=int(api_id) if api_id else None,
        api_hash=api_hash,
        proxy=proxy,
    )
    try:
        if password:
            # 2FA step: sign_in with password only (session already has code auth state)
            await client.sign_in(password=password)
        else:
            # Code step
            await client.sign_in(
                phone=phone_number,
                code=code,
                phone_code_hash=phone_code_hash,
            )

        me = await client.get_me()

        # Download avatar
        avatar_path = None
        import os
        from django.conf import settings as dj_settings
        media_avatars = os.path.join(dj_settings.MEDIA_ROOT, 'avatars')
        os.makedirs(media_avatars, exist_ok=True)
        try:
            downloaded = await client.download_profile_photo(
                'me', file=os.path.join(media_avatars, f"{phone_number}.jpg")
            )
            if downloaded:
                avatar_path = f"avatars/{phone_number}.jpg"
        except Exception:
            pass

        return {
            "success": True,
            "session_string": client.session.save(),
            "avatar": avatar_path,
            "user_id": getattr(me, 'id', None),
            "first_name": getattr(me, 'first_name', ''),
            "last_name": getattr(me, 'last_name', ''),
            "username": getattr(me, 'username', ''),
        }
    except SessionPasswordNeededError:
        return {"success": False, "needs_password": True, "session_string": client.session.save()}
    except PasswordHashInvalidError:
        return {"success": False, "error": "2FA parol noto'g'ri. Iltimos qayta urinib ko'ring.", "session_string": client.session.save()}
    except Exception as e:
        saved = ""
        try:
            saved = client.session.save()
        except Exception:
            pass
        return {"success": False, "error": str(e), "session_string": saved}
    finally:
        await client.disconnect()


async def get_dialogs(session_str):
    client = await get_client(temp_session_string=session_str)
    dialogs_list = []
    try:
        dialogs = await client.get_dialogs()
        for d in dialogs:
            if d.is_group or d.is_channel:
                dialogs_list.append({
                    "id": d.id,
                    "title": d.title,
                    "is_group": d.is_group,
                    "is_channel": d.is_channel,
                })
        return {"success": True, "dialogs": dialogs_list}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


async def get_and_download_avatar(session_str, file_path, device_setting=None,
                                  api_id=None, api_hash=None, proxy=None):
    client = await get_client(
        temp_session_string=session_str,
        device_setting=device_setting,
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy,
    )
    try:
        me = await client.get_me()
        if not me or not me.photo:
            return False
        downloaded = await client.download_profile_photo('me', file=file_path)
        return bool(downloaded)
    except Exception as e:
        print("Avatar extraction exception:", repr(e))
        return False
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Fetch the latest Telegram login code for this account.
#
# When the user is locked out of an active session (logged out from phone
# or PC), they can still receive the official Telegram login code via THIS
# account's saved session — it arrives as a message from user `777000`
# (Telegram service notifications). This helper:
#   1. Reads the last few messages from 777000 (in case the code is already
#      sitting in history from a recent login attempt)
#   2. If nothing matches, listens for a new message for up to wait_seconds
#   3. Extracts the 5-digit code from the message body
# ---------------------------------------------------------------------------

import re as _re
from datetime import datetime, timedelta, timezone as _dt_tz

TELEGRAM_SERVICE_USER_ID = 777000
_LOGIN_CODE_RE = _re.compile(r'(?<!\d)(\d{5})(?!\d)')


def _extract_code(text):
    if not text:
        return None
    m = _LOGIN_CODE_RE.search(text)
    return m.group(1) if m else None


async def fetch_telegram_login_code(account, *, wait_seconds=30, lookback_seconds=600):
    """Return the latest Telegram login code received by this account.

    Returns:
        {
          'success': bool,
          'code': str | None,         # 5-digit code if found
          'message': str,              # raw message body that contained it
          'sent_at': str (ISO),        # when Telegram sent it
          'source': 'history' | 'live' | None,
          'error': str (if not success),
        }
    """
    from telethon import events  # local: avoids circular import at module load

    device = await sync_to_async(lambda a: a.device_setting)(account)
    proxy  = await sync_to_async(lambda a: a.proxy)(account)
    client = await get_client(
        temp_session_string=account.session_string,
        device_setting=device,
        api_id=account.api_id,
        api_hash=account.api_hash,
        proxy=proxy,
    )

    try:
        me = await client.get_me()
        if not me:
            return {'success': False, 'code': None, 'message': '',
                    'sent_at': None, 'source': None,
                    'error': "Sessiya yaroqsiz — qayta kirish kerak"}

        # 1) Scan recent history from 777000
        cutoff = datetime.now(tz=_dt_tz.utc) - timedelta(seconds=lookback_seconds)
        try:
            async for msg in client.iter_messages(TELEGRAM_SERVICE_USER_ID, limit=20):
                if msg.date and msg.date < cutoff:
                    break  # older than lookback window
                code = _extract_code(msg.message or '')
                if code:
                    return {
                        'success': True, 'code': code,
                        'message': (msg.message or '')[:1000],
                        'sent_at': msg.date.isoformat() if msg.date else None,
                        'source': 'history',
                        'error': '',
                    }
        except Exception:
            # Don't fail the whole call — fall through to live listening
            pass

        # 2) Listen for a fresh one
        if wait_seconds <= 0:
            return {'success': False, 'code': None, 'message': '',
                    'sent_at': None, 'source': None,
                    'error': "Yangi kod hali kelmagan. Telegram'dan kodni qaytadan so'rang va shu sahifani yangilang."}

        future = asyncio.get_event_loop().create_future()

        @client.on(events.NewMessage(from_users=TELEGRAM_SERVICE_USER_ID))
        async def _handler(event):
            code = _extract_code(event.message.message or '')
            if code and not future.done():
                future.set_result({
                    'code': code,
                    'message': (event.message.message or '')[:1000],
                    'sent_at': event.message.date.isoformat() if event.message.date else None,
                })

        try:
            res = await asyncio.wait_for(future, timeout=wait_seconds)
            return {'success': True, **res, 'source': 'live', 'error': ''}
        except asyncio.TimeoutError:
            return {'success': False, 'code': None, 'message': '',
                    'sent_at': None, 'source': None,
                    'error': f"{wait_seconds} sekund kutildi, kod kelmadi"}
        finally:
            client.remove_event_handler(_handler)

    except Exception as e:
        return {'success': False, 'code': None, 'message': '',
                'sent_at': None, 'source': None,
                'error': f"Kutilmagan xato: {e}"}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
