from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('inventory', '0001_seed_groups'),
    ]

    operations = [
        migrations.CreateModel(
            name='InventoryPermission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ],
            options={
                'verbose_name': 'inventory permission',
                'verbose_name_plural': 'inventory permissions',
                'permissions': (
                    ('view_inventory', 'Can view inventory dashboard'),
                    ('manage_inventory', 'Can manage inventory dashboard'),
                ),
                'default_permissions': (),
                'managed': False,
            },
        ),
    ]
