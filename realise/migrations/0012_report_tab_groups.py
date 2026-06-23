from django.db import migrations

# Dedicated viewer groups for the standalone report tabs, so an admin can grant a single
# tab to a user without giving them full inventory/realise access. Mirrors the existing
# stock_viewer group. The can_* flags in core.context_processors check these names.
REPORT_TAB_GROUPS = ['production_viewer', 'oih_vs_stock_viewer', 'compare_sales_viewer']


def create_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    for name in REPORT_TAB_GROUPS:
        Group.objects.get_or_create(name=name)


def remove_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name__in=REPORT_TAB_GROUPS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('realise', '0011_cityowner'),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
    ]
