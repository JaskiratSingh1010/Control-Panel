from django.db import migrations

# Dedicated viewer group for the standalone Realise Calculator report tab, so an admin can grant
# just this tab without giving full Realise access. Mirrors migration 0023's Dispatch Details group;
# the can_realise_calculator flag in core.context_processors checks this name.
REALISE_CALCULATOR_GROUP = 'realise_calculator_viewer'


def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.get_or_create(name=REALISE_CALCULATOR_GROUP)


def remove_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name=REALISE_CALCULATOR_GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('realise', '0023_dispatch_details_viewer_group'),
    ]

    operations = [
        migrations.RunPython(create_group, remove_group),
    ]
