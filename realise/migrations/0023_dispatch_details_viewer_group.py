from django.db import migrations

# Dedicated viewer group for the standalone Dispatch Details report tab, so an admin can grant just
# this tab without giving full Realise access. Mirrors migration 0022's Open Payments group;
# the can_dispatch_details flag in core.context_processors checks this name.
DISPATCH_DETAILS_GROUP = 'dispatch_details_viewer'


def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.get_or_create(name=DISPATCH_DETAILS_GROUP)


def remove_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name=DISPATCH_DETAILS_GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('realise', '0022_open_payments_viewer_group'),
    ]

    operations = [
        migrations.RunPython(create_group, remove_group),
    ]
