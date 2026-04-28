"""
Re-save every Account row so its secret fields (session_string, two_fa_password)
get re-encrypted under the current DB_ENCRYPTION_KEY.

Safe to run repeatedly. Already-encrypted rows stay encrypted (idempotent thanks
to the `fernet:v1:` prefix check in accounts.fields.encrypt_str).

Usage:
    python manage.py encrypt_existing
    python manage.py encrypt_existing --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Account
from accounts.fields import is_encrypted


class Command(BaseCommand):
    help = "Encrypt any legacy plaintext session_string / two_fa_password values in place."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Count how many rows would be re-encrypted without writing.",
        )

    def handle(self, *args, **options):
        dry = options['dry_run']

        total = Account.objects.count()
        would_encrypt_session = 0
        would_encrypt_2fa = 0

        # Pull raw values via `values` to bypass field decryption and spot legacy rows.
        for row in Account.objects.values('id', 'session_string', 'two_fa_password'):
            if row['session_string'] and not is_encrypted(row['session_string']):
                would_encrypt_session += 1
            if row['two_fa_password'] and not is_encrypted(row['two_fa_password']):
                would_encrypt_2fa += 1

        self.stdout.write(f"Scanned {total} account(s).")
        self.stdout.write(f"  session_string  — {would_encrypt_session} legacy plaintext")
        self.stdout.write(f"  two_fa_password — {would_encrypt_2fa} legacy plaintext")

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — no writes."))
            return

        if would_encrypt_session == 0 and would_encrypt_2fa == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to do — all rows already encrypted."))
            return

        # Re-save each affected account. Reading via the ORM auto-decrypts legacy
        # plaintext (passthrough) and saving re-writes with the fernet prefix.
        rewritten = 0
        with transaction.atomic():
            # Re-fetch as full models so get_prep_value runs on save.
            for acc in Account.objects.all().iterator(chunk_size=200):
                touched_fields = []
                if acc.session_string is not None:
                    touched_fields.append('session_string')
                if acc.two_fa_password is not None:
                    touched_fields.append('two_fa_password')
                if touched_fields:
                    acc.save(update_fields=touched_fields)
                    rewritten += 1

        self.stdout.write(self.style.SUCCESS(
            f"Re-saved {rewritten} account(s). All secrets are now Fernet-encrypted at rest."
        ))
