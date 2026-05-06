"""Telegram-bot delivery for app notifications.

Both sync and async senders are exposed because notifications fire from
both request handlers (sync code paths) and runners (async). The HTTP
call is best-effort: a failed send is logged once and never retried, so
the originating workflow is never blocked by Telegram outages.
"""
import asyncio
import logging
from urllib.parse import quote

import httpx
from asgiref.sync import sync_to_async

from .models import NotificationConfig

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 8.0


def _format_message(event, payload):
    icons = {
        'task_completed': '✅',
        'task_failed': '❌',
        'task_paused': '⏸',
        'account_session_dead': '🔒',
        'account_banned': '🚫',
        'flood_wait_long': '⏳',
        'quota_exhausted': '📉',
    }
    icon = icons.get(event, 'ℹ️')
    title = NotificationConfig._meta.get_field('events').verbose_name
    label = dict(NotificationConfig.EVENT_CHOICES).get(event, event)

    lines = [f"{icon} <b>{label}</b>"]
    for k, v in (payload or {}).items():
        if v in (None, ''):
            continue
        lines.append(f"<b>{k}:</b> {v}")
    return "\n".join(lines)


def _resolve_config(user):
    if user is None:
        return None
    try:
        cfg = NotificationConfig.objects.get(user=user)
    except NotificationConfig.DoesNotExist:
        return None
    return cfg if cfg.is_configured else None


def send_notification_sync(user, event, **payload):
    cfg = _resolve_config(user)
    if cfg is None or not cfg.is_event_enabled(event):
        return False

    text = _format_message(event, payload)
    url = _TELEGRAM_API.format(token=cfg.bot_token)
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.post(url, json={
                'chat_id': cfg.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            })
        return r.status_code == 200
    except Exception as exc:
        logger.warning("Notification send failed: %s", exc)
        return False


async def send_notification(user, event, **payload):
    cfg = await sync_to_async(_resolve_config)(user)
    if cfg is None or not cfg.is_event_enabled(event):
        return False

    text = _format_message(event, payload)
    url = _TELEGRAM_API.format(token=cfg.bot_token)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(url, json={
                'chat_id': cfg.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            })
        return r.status_code == 200
    except Exception as exc:
        logger.warning("Notification send failed: %s", exc)
        return False


def send_notification_for_user_id_sync(user_id, event, **payload):
    """Convenience wrapper for hooks that only have a user_id."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return False
    return send_notification_sync(user, event, **payload)


async def test_send(cfg):
    """Send a test message immediately, ignoring `enabled` + `events`."""
    if not cfg.bot_token or not cfg.chat_id:
        return False, "Bot token yoki chat ID kiritilmagan"
    url = _TELEGRAM_API.format(token=cfg.bot_token)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(url, json={
                'chat_id': cfg.chat_id,
                'text': "✅ Userbot Manager — test bildirishnoma. Ulanish ishlaydi!",
            })
        if r.status_code == 200:
            return True, "Yuborildi"
        try:
            err = r.json().get('description', f"HTTP {r.status_code}")
        except Exception:
            err = f"HTTP {r.status_code}"
        return False, err
    except Exception as exc:
        return False, str(exc)
