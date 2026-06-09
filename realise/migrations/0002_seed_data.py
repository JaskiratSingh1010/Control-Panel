from datetime import date
from django.db import migrations
from django.contrib.auth.hashers import make_password


DEFAULT_TARGETS = {
    'COMMODITY|BLENDED':          {'tgt_ltrs': 30000,   'tgt_rate': 130},
    'COMMODITY|COTTON SEED':      {'tgt_ltrs': 20000,   'tgt_rate': 130},
    'COMMODITY|MUSTARD':          {'tgt_ltrs': 625000,  'tgt_rate': 145},
    'COMMODITY|RICE BRAN':        {'tgt_ltrs': 25000,   'tgt_rate': 131},
    'COMMODITY|SOYABEAN':         {'tgt_ltrs': 400000,  'tgt_rate': 123},
    'COMMODITY|SUNFLOWER':        {'tgt_ltrs': 135000,  'tgt_rate': 145},
    'PREMIUM|BLENDED':            {'tgt_ltrs': 10000,   'tgt_rate': 190},
    'PREMIUM|CANOLA':             {'tgt_ltrs': 350000,  'tgt_rate': 205},
    'PREMIUM|COCONUT':            {'tgt_ltrs': 5000,    'tgt_rate': 449},
    'PREMIUM|EXTRA VIRGIN OLIVE': {'tgt_ltrs': 15000,   'tgt_rate': 500},
    'PREMIUM|GHEE':               {'tgt_ltrs': 15000,   'tgt_rate': 536},
    'PREMIUM|GROUNDNUT':          {'tgt_ltrs': 50000,   'tgt_rate': 175},
    'PREMIUM|OLIVE':              {'tgt_ltrs': 310000,  'tgt_rate': 253},
    'PREMIUM|SESAME':             {'tgt_ltrs': 5000,    'tgt_rate': 290},
    'PREMIUM|SLICED OLIVE':       {'tgt_ltrs': 0,       'tgt_rate': 0},
    'PREMIUM|YELLOW MUSTARD':     {'tgt_ltrs': 10000,   'tgt_rate': 180},
}

USERS = [
    {
        'username':     'admin',
        'password':     'jivoadmin',
        'first_name':   'Administrator',
        'last_name':    '',
        'is_staff':     True,
        'is_superuser': False,
        'group':        'realise_admin',
    },
    {
        'username':     'commodity',
        'password':     'commodity',
        'first_name':   'Commodity Team',
        'last_name':    '',
        'is_staff':     False,
        'is_superuser': False,
        'group':        'realise_commodity',
    },
    {
        'username':     'premium',
        'password':     'premium',
        'first_name':   'Premium Team',
        'last_name':    '',
        'is_staff':     False,
        'is_superuser': False,
        'group':        'realise_premium',
    },
]


def seed(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    User  = apps.get_model('auth', 'User')
    MonthlyTarget = apps.get_model('realise', 'MonthlyTarget')

    # 1. Create groups
    groups = {}
    for name in ('realise_admin', 'realise_premium', 'realise_commodity'):
        g, _ = Group.objects.get_or_create(name=name)
        groups[name] = g

    # 2. Create users
    for u in USERS:
        user, created = User.objects.get_or_create(username=u['username'])
        if created:
            user.password     = make_password(u['password'])
            user.first_name   = u['first_name']
            user.last_name    = u['last_name']
            user.is_staff     = u['is_staff']
            user.is_superuser = u['is_superuser']
            user.save()
        user.groups.add(groups[u['group']])

    # 3. Seed current month's MonthlyTarget rows
    today = date.today()
    month, year = today.month, today.year
    for key, vals in DEFAULT_TARGETS.items():
        parts = key.split('|', 1)
        if len(parts) != 2:
            continue
        product_type, sub_group = parts
        MonthlyTarget.objects.get_or_create(
            product_type=product_type,
            sub_group=sub_group,
            month=month,
            year=year,
            defaults={'tgt_ltrs': vals['tgt_ltrs'], 'tgt_rate': vals['tgt_rate']},
        )


def unseed(apps, schema_editor):
    pass  # intentional no-op — don't destroy data on reverse


class Migration(migrations.Migration):
    dependencies = [
        ('realise', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
