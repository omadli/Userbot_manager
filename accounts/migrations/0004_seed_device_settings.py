from django.db import migrations

DEVICES = [
    {
        "name": "Samsung Galaxy S23",
        "device_model": "Samsung Galaxy S23",
        "system_version": "Android 13",
        "app_version": "10.5.0",
        "lang_code": "en",
        "system_lang_code": "en",
    },
    {
        "name": "Xiaomi 13 Pro",
        "device_model": "Xiaomi 13 Pro",
        "system_version": "Android 13",
        "app_version": "10.6.2",
        "lang_code": "en",
        "system_lang_code": "en",
    },
    {
        "name": "Redmi Note 12",
        "device_model": "Redmi Note 12",
        "system_version": "Android 12",
        "app_version": "10.4.1",
        "lang_code": "uz",
        "system_lang_code": "uz",
    },
    {
        "name": "OPPO Reno8",
        "device_model": "OPPO Reno8",
        "system_version": "Android 12",
        "app_version": "10.3.2",
        "lang_code": "uz",
        "system_lang_code": "uz",
    },
    {
        "name": "OnePlus 11",
        "device_model": "OnePlus 11",
        "system_version": "Android 13",
        "app_version": "10.7.0",
        "lang_code": "ru",
        "system_lang_code": "ru",
    },
    {
        "name": "iPhone 15 Pro",
        "device_model": "iPhone 15 Pro",
        "system_version": "iOS 17.0",
        "app_version": "10.6.1",
        "lang_code": "en",
        "system_lang_code": "en",
    },
    {
        "name": "Vivo V25",
        "device_model": "Vivo V25",
        "system_version": "Android 12",
        "app_version": "10.4.0",
        "lang_code": "uz",
        "system_lang_code": "uz",
    },
    {
        "name": "Realme 10 Pro",
        "device_model": "Realme 10 Pro",
        "system_version": "Android 13",
        "app_version": "10.5.2",
        "lang_code": "ru",
        "system_lang_code": "ru",
    },
]


def seed_devices(apps, schema_editor):
    DeviceSetting = apps.get_model('accounts', 'DeviceSetting')
    for data in DEVICES:
        DeviceSetting.objects.get_or_create(name=data['name'], defaults=data)


def unseed_devices(apps, schema_editor):
    DeviceSetting = apps.get_model('accounts', 'DeviceSetting')
    names = [d['name'] for d in DEVICES]
    DeviceSetting.objects.filter(name__in=names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_account_api_device'),
    ]

    operations = [
        migrations.RunPython(seed_devices, reverse_code=unseed_devices),
    ]
