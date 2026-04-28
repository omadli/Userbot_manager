from django.core.management.base import BaseCommand
from accounts.models import DeviceSetting

class Command(BaseCommand):
    help = 'Create the default device setting profile'

    def handle(self, *args, **options):
        obj, created = DeviceSetting.objects.get_or_create(
            name="default",
            defaults={
                "device_model": "Samsung S26 ultra",
                "system_version": "Android 14",
                "app_version": "12.6.4",
                "lang_code": "uz",
                "system_lang_code": "uz"
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Successfully created 'default' DeviceSetting."))
        else:
            self.stdout.write(self.style.WARNING("'default' DeviceSetting already exists."))
