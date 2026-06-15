import logging
import json
from datetime import date

from django.core.cache import cache

logger = logging.getLogger(__name__)

GROUP_PROFILE = {
    'realise_admin':     ('admin',  '',          True),
    'realise_premium':   ('viewer', 'PREMIUM',   False),
    'realise_commodity': ('viewer', 'COMMODITY', False),
}

INVENTORY_GROUPS = {'inventory_admin', 'inventory_viewer'}
REALISE_GROUPS = {'realise_admin', 'realise_premium', 'realise_commodity'}

MODULE_GROUPS = {
    'sales': {'sales_admin', 'sales_viewer'},
    'expenses': {'expenses_admin', 'expenses_viewer'},
    'salaries': {'salaries_admin', 'salaries_viewer', 'salary_admin', 'salary_viewer'},
    'cogs': {'cogs_admin', 'cogs_viewer'},
}


def get_user_groups(user):
    if not user.is_authenticated:
        return []
    return list(user.groups.order_by('name').values_list('name', flat=True))


def get_group_permission_codenames(user):
    if not user.is_authenticated:
        return []
    return sorted(user.get_group_permissions())


def get_permission_codenames(user):
    if not user.is_authenticated:
        return []
    return sorted(user.get_all_permissions())


def build_user_permissions(user):
    if not user.is_authenticated:
        return {
            'can_edit': False,
            'can_realise': False,
            'can_inventory': False,
            'inventory_can_edit': False,
            'can_sales': False,
            'can_expenses': False,
            'can_salaries': False,
            'can_cogs': False,
        }

    if user.is_superuser:
        return {
            'can_edit': True,
            'can_realise': True,
            'can_inventory': True,
            'inventory_can_edit': True,
            'can_sales': True,
            'can_expenses': True,
            'can_salaries': True,
            'can_cogs': True,
        }

    user_groups = set(get_user_groups(user))
    permission_codenames = set(get_permission_codenames(user))

    def has_app_permission(app_label):
        return any(perm.startswith(f'{app_label}.') for perm in permission_codenames)

    return {
        'can_edit': bool('realise_admin' in user_groups or user.has_perm('realise.change_monthlytarget')),
        'can_realise': bool(
            user_groups & REALISE_GROUPS
            or has_app_permission('realise')
        ),
        'can_inventory': bool(
            user_groups & INVENTORY_GROUPS
            or user.has_perm('inventory.view_inventory')
            or user.has_perm('inventory.manage_inventory')
        ),
        'inventory_can_edit': bool(
            'inventory_admin' in user_groups
            or user.has_perm('inventory.manage_inventory')
        ),
        'can_sales': bool(user_groups & MODULE_GROUPS['sales'] or has_app_permission('sales')),
        'can_expenses': bool(user_groups & MODULE_GROUPS['expenses'] or has_app_permission('dashboard')),
        'can_salaries': bool(user_groups & MODULE_GROUPS['salaries'] or has_app_permission('salaries')),
        'can_cogs': bool(user_groups & MODULE_GROUPS['cogs']),
    }


def build_login_permission_payload(user):
    role, type_filter, _ = derive_realise_profile(user)
    permissions = build_user_permissions(user)
    return {
        'id': user.pk,
        'username': user.get_username(),
        'display_name': user.get_full_name() or user.get_username(),
        'email': user.email,
        'is_staff': user.is_staff,
        'is_superuser': user.is_superuser,
        'groups': get_user_groups(user),
        'group_permission_codenames': get_group_permission_codenames(user),
        'permission_codenames': get_permission_codenames(user),
        'role': role,
        'type_filter': type_filter,
        'permissions': permissions,
        **permissions,
    }


def get_login_permission_payload(request):
    user = request.user
    if not user.is_authenticated:
        permissions = build_user_permissions(user)
        return {
            'id': None,
            'username': '',
            'display_name': '',
            'email': '',
            'is_staff': False,
            'is_superuser': False,
            'groups': [],
            'group_permission_codenames': [],
            'permission_codenames': [],
            'role': '',
            'type_filter': '',
            'permissions': permissions,
            **permissions,
        }

    payload = build_login_permission_payload(user)
    request.session['group_permissions'] = payload
    return payload


def _build_period_options():
    today = date.today()
    options = []
    y, m = today.year, today.month
    for _ in range(13):
        options.append({
            'value': f'{y}-{m:02d}',
            'label': date(y, m, 1).strftime('%B %Y'),
        })
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return options


def _resolve_period(request):
    raw = request.GET.get('period', '')
    today = date.today()
    try:
        parts = raw.split('-')
        y, m = int(parts[0]), int(parts[1])
        if 1 <= m <= 12 and 2000 <= y <= 2100:
            return y, m
    except (ValueError, IndexError, AttributeError):
        pass
    return today.year, today.month


def _build_ticker_items(year, month):
    items = []
    try:
        from home import services as home_services
        sales = home_services.get_total_sales_volume(year, month)
        if sales.get('has_data'):
            items.append({'label': 'SALES', 'value': sales.get('value', '—')})
        realise = home_services.get_avg_realisation(year, month)
        if realise.get('has_data'):
            items.append({'label': 'REALISE', 'value': realise.get('value', '—')})
        for key, label in (('opex', 'OPEX'), ('salaries', 'SALARY'),
                           ('cogs', 'COGS'), ('inventory', 'INVENTORY')):
            getter = getattr(home_services, {
                'opex':      'get_operating_expenses',
                'salaries':  'get_salary_expenditure',
                'cogs':      'get_cost_of_goods_sold',
                'inventory': 'get_inventory_value',
            }[key])
            kpi = getter(year, month)
            if kpi.get('has_data'):
                items.append({'label': label, 'value': kpi.get('value', '—')})
    except Exception as e:
        logger.warning('[context] ticker build failed: %s', e)
    return items


def _filter_ticker_items(items, permissions):
    allowed_by_label = {
        'SALES': permissions.get('can_sales'),
        'REALISE': permissions.get('can_realise'),
        'AVG REALISE': permissions.get('can_realise'),
        'OPEX': permissions.get('can_expenses'),
        'TOTAL OPEX': permissions.get('can_expenses'),
        'SALARY': permissions.get('can_salaries'),
        'COGS': permissions.get('can_cogs'),
        'INVENTORY': permissions.get('can_inventory'),
    }
    return [
        item for item in items
        if allowed_by_label.get(item.get('label'), True)
    ]


def derive_realise_profile(user):
    if not user.is_authenticated:
        return ('anonymous', '', False)
    if user.is_superuser:
        return ('admin', '', True)
    user_groups = set(user.groups.values_list('name', flat=True))
    for group_name in ('realise_admin', 'realise_premium', 'realise_commodity'):
        if group_name in user_groups:
            return GROUP_PROFILE[group_name]
    return ('viewer', '', False)


def user_profile(request):
    user = request.user

    year, month = _resolve_period(request)
    ticker_key = f'home_ticker_{year}_{month:02d}'
    ticker_items = cache.get(ticker_key)
    if ticker_items is None:
        ticker_items = _build_ticker_items(year, month)
        cache.set(ticker_key, ticker_items, 180)

    period_ctx = {
        'period_year':     year,
        'period_month':    month,
        'period_label':    date(year, month, 1).strftime('%B %Y'),
        'period_options':  _build_period_options(),
        'selected_period': f'{year}-{month:02d}',
        'ticker_items':    ticker_items,
    }

    login_payload = get_login_permission_payload(request)
    group_permissions = login_payload['permissions']
    user_groups = login_payload['groups']
    group_permission_codenames = login_payload['group_permission_codenames']
    permission_codenames = login_payload['permission_codenames']

    if not user.is_authenticated:
        return {
            'is_authenticated': False,
            'login_user': login_payload,
            'login_user_json': json.dumps(login_payload),
            'display_name': login_payload['display_name'],
            'role': login_payload['role'],
            'type_filter': login_payload['type_filter'],
            'user_groups': user_groups,
            'user_groups_json': json.dumps(user_groups),
            'group_permission_codenames': group_permission_codenames,
            'group_permission_codenames_json': json.dumps(group_permission_codenames),
            'permission_codenames': permission_codenames,
            'permission_codenames_json': json.dumps(permission_codenames),
            'group_permissions': group_permissions,
            'group_permissions_json': json.dumps(group_permissions),
            **group_permissions,
            **period_ctx,
        }

    period_ctx['ticker_items'] = _filter_ticker_items(period_ctx['ticker_items'], group_permissions)

    return {
        'is_authenticated': True,
        'login_user': login_payload,
        'login_user_json': json.dumps(login_payload),
        'display_name': login_payload['display_name'],
        'role': login_payload['role'],
        'type_filter': login_payload['type_filter'],
        'user_groups': user_groups,
        'user_groups_json': json.dumps(user_groups),
        'group_permission_codenames': group_permission_codenames,
        'group_permission_codenames_json': json.dumps(group_permission_codenames),
        'permission_codenames': permission_codenames,
        'permission_codenames_json': json.dumps(permission_codenames),
        'group_permissions': group_permissions,
        'group_permissions_json': json.dumps(group_permissions),
        **group_permissions,
        **period_ctx,
    }
