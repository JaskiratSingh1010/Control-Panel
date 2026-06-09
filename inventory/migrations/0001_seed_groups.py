from django.conf import settings
from django.db import migrations


def seed_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    User = apps.get_model(settings.AUTH_USER_MODEL.split('.')[0], settings.AUTH_USER_MODEL.split('.')[1])

    inventory_admin, _ = Group.objects.get_or_create(name='inventory_admin')
    Group.objects.get_or_create(name='inventory_viewer')

    admin = User.objects.filter(username='admin').first()
    if admin:
        admin.groups.add(inventory_admin)


class Migration(migrations.Migration):
    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(seed_groups, migrations.RunPython.noop),
    ]
