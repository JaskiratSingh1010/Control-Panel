from django.db import migrations

# Dedicated viewer group for the standalone Open Payments report tab, so an admin can grant just
# this tab without giving full Realise/inventory access. Mirrors migration 0012's report-tab groups;
# the can_open_payments flag in core.context_processors checks this name.
OPEN_PAYMENTS_GROUP = 'open_payments_viewer'


def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.get_or_create(name=OPEN_PAYMENTS_GROUP)


def remove_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name=OPEN_PAYMENTS_GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('realise', '0021_claim_coop_no_claim_ref_inv_no'),
    ]

    operations = [
        migrations.RunPython(create_group, remove_group),
    ]
