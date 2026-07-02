from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory, TestCase, TransactionTestCase

from .context_processors import user_alerts
from .models import Account


def _seed_accounts(user):
    """For `user`: 1 inactive, 1 spam, 1 healthy."""
    Account.objects.create(phone_number='+100', owner=user, is_active=False)
    Account.objects.create(phone_number='+101', owner=user, is_spam=True)
    Account.objects.create(phone_number='+102', owner=user)


def _request(user):
    request = RequestFactory().get('/notifications/')
    request.user = user
    return request


class UserAlertsContextProcessorTests(TestCase):
    """Sync-context behaviour of the sidebar alert counters."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='alerts-user', password='x')
        _seed_accounts(cls.user)
        # A different user's inactive account must never leak into the counts.
        other = User.objects.create_user(username='other-user', password='x')
        Account.objects.create(phone_number='+200', owner=other, is_active=False)

    def test_counts_are_owner_scoped(self):
        ctx = user_alerts(_request(self.user))
        self.assertEqual(ctx['inactive_accounts_count'], 1)
        self.assertEqual(ctx['spam_accounts_count'], 1)

    def test_anonymous_user_gets_zeros(self):
        ctx = user_alerts(_request(AnonymousUser()))
        self.assertEqual(
            ctx,
            {'inactive_accounts_count': 0, 'spam_accounts_count': 0},
        )


class UserAlertsAsyncContextTests(TransactionTestCase):
    """
    Regression for SynchronousOnlyOperation: `user_alerts` runs during every
    template render, including from `async def` (ASGI) views where a loop is
    live on the current thread. It used to hit the ORM there and crash.

    TransactionTestCase (not TestCase) so the seeded rows are committed and
    visible to the worker thread the processor offloads the ORM to — under
    SQLite an open TestCase transaction would otherwise lock the table.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='async-user', password='x')
        _seed_accounts(self.user)

    async def test_async_context_does_not_raise_and_counts(self):
        # A loop is live on this thread while the coroutine body runs — exactly
        # the situation an async view renders a template in.
        ctx = user_alerts(_request(self.user))
        self.assertEqual(ctx['inactive_accounts_count'], 1)
        self.assertEqual(ctx['spam_accounts_count'], 1)
