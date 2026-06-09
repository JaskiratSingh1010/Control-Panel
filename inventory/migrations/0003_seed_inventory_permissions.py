from django.db import migrations


def seed_inventory_permissions(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')

    content_type, _ = ContentType.objects.get_or_create(
        app_label='inventory',
        model='inventorypermission',
    )
    view_permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename='view_inventory',
        defaults={'name': 'Can view inventory dashboard'},
    )
    manage_permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename='manage_inventory',
        defaults={'name': 'Can manage inventory dashboard'},
    )

    inventory_viewer, _ = Group.objects.get_or_create(name='inventory_viewer')
    inventory_admin, _ = Group.objects.get_or_create(name='inventory_admin')

    inventory_viewer.permissions.add(view_permission)
    inventory_admin.permissions.add(view_permission, manage_permission)


class Migration(migrations.Migration):
    dependencies = [
        ('inventory', '0002_inventory_permissions'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(seed_inventory_permissions, migrations.RunPython.noop),
    ]
