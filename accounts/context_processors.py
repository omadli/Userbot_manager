"""
Template context processors for the `accounts` app.

`user_alerts` adds counters used by the sidebar/dashboard to surface
accounts that need attention without requiring every view to load them.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections

from .models import Account

# Async (ASGI) views render their templates on the event-loop thread, where
# the Django ORM refuses to run (raises SynchronousOnlyOperation). A context
# processor can't be async, so when we detect a running loop we run the sync
# ORM on a worker thread — which has no event loop, so the ORM guard doesn't
# trip. Small pool, reused across requests.
_orm_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ctxproc-orm")


def _compute_alerts(request):
    """The synchronous ORM work. Safe on any thread with no running loop."""
    try:
        if not hasattr(request, 'user') or not request.user.is_authenticated:
            return {
                'inactive_accounts_count': 0,
                'spam_accounts_count': 0,
            }
        qs = Account.objects.filter(owner=request.user)
        return {
            'inactive_accounts_count': qs.filter(is_active=False).count(),
            'spam_accounts_count': qs.filter(is_spam=True).count(),
        }
    finally:
        # On the async path this runs on a pooled worker thread that lives
        # outside Django's request/response cycle, so the request_finished
        # signal never closes its DB connection. Close obsolete ones here
        # (respects CONN_MAX_AGE) so pooled threads don't leak connections.
        close_old_connections()


def user_alerts(request):
    """
    Injects:
      inactive_accounts_count — how many of this user's accounts are
                                marked `is_active=False` (needs relogin)
      spam_accounts_count     — how many are flagged `is_spam=True`

    Runs only for authenticated users; otherwise zeros are injected so
    templates don't need to guard.

    Safe from both sync views and async (ASGI) views — see `_orm_pool`.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Sync context (WSGI, or a plain `def` view) — run the ORM inline.
        return _compute_alerts(request)
    # Async context (an `async def` view rendering this template). Hop off the
    # event-loop thread so the ORM is allowed to run. Blocking on the result
    # is fine — context processors are synchronous and this is two COUNT()s.
    return _orm_pool.submit(_compute_alerts, request).result()
