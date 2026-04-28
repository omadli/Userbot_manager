"""
Template context processors for the `accounts` app.

`user_alerts` adds counters used by the sidebar/dashboard to surface
accounts that need attention without requiring every view to load them.
"""
from .models import Account


def user_alerts(request):
    """
    Injects:
      inactive_accounts_count — how many of this user's accounts are
                                marked `is_active=False` (needs relogin)
      spam_accounts_count     — how many are flagged `is_spam=True`

    Runs only for authenticated users; otherwise zeros are injected so
    templates don't need to guard.
    """
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
