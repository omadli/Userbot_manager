from django.core.management.base import BaseCommand

from accounts.health import recompute_all


class Command(BaseCommand):
    help = "Recompute health_score for every account across all users"

    def handle(self, *args, **options):
        n = recompute_all()
        self.stdout.write(self.style.SUCCESS(f"Updated {n} accounts"))
