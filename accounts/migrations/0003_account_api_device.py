from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_account_avatar'),
    ]

    operations = [
        # Add new fields to Account
        migrations.AddField(
            model_name='account',
            name='api_id',
            field=models.IntegerField(
                blank=True, null=True, verbose_name='API ID',
                help_text="Bo'sh qoldirilsa global sozlamadan foydalaniladi"
            ),
        ),
        migrations.AddField(
            model_name='account',
            name='api_hash',
            field=models.CharField(
                max_length=64, blank=True, null=True, verbose_name='API Hash',
                help_text="Bo'sh qoldirilsa global sozlamadan foydalaniladi"
            ),
        ),
        migrations.AddField(
            model_name='account',
            name='device_setting',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='accounts',
                to='accounts.devicesetting',
                verbose_name='Qurilma sozlamasi',
                help_text="Bo'sh qoldirilsa 'default' sozlamasi ishlatiladi"
            ),
        ),
    ]
