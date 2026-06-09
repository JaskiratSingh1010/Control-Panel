import calendar
import logging
from datetime import date

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _indian_grouping(n: int) -> str:
    s = str(abs(int(n)))
    if len(s) <= 3:
        return ('-' if n < 0 else '') + s
    result = s[-3:]
    s = s[:-3]
    while s:
        result = s[-2:] + ',' + result
        s = s[:-2]
    return ('-' if n < 0 else '') + result


def _format_litres(value: float) -> str:
    return f'{_indian_grouping(round(value))} L'


def _format_inr_compact(value: float) -> str:
    v = abs(value)
    prefix = '-' if value < 0 else ''
    if v < 1_000:
        return f'{prefix}₹{round(v)}'
    if v < 1_00_000:
        return f'{prefix}₹{v / 1_000:.1f}K'
    if v < 1_00_00_000:
        return f'{prefix}₹{v / 1_00_000:.2f} L'
    return f'{prefix}₹{v / 1_00_00_000:.2f} Cr'


def _pct_change(curr: float, prev: float):
    if not prev:
        return None
    return round((curr - prev) / prev * 100, 1)


def _diff_display(current: float, previous: float, formatter) -> str:
    diff = float(current or 0) - float(previous or 0)
    if diff > 0:
        return f'+{formatter(diff)}'
    if diff < 0:
        return f'-{formatter(abs(diff))}'
    return formatter(0)


def _glimpse_row(label: str, current: float, previous: float, formatter) -> dict:
    diff = float(current or 0) - float(previous or 0)
    return {
        'label': label,
        'current': formatter(float(current or 0)),
        'last': formatter(float(previous or 0)),
        'diff': _diff_display(current, previous, formatter),
        'trend': _pct_change(float(current or 0), float(previous or 0)),
        'raw_diff': diff,
        'direction': 'up' if float(current or 0) > float(previous or 0) else 'down' if float(current or 0) < float(previous or 0) else 'flat',
    }


def _sales_category_rows(curr_products, prev_products, value_key, formatter, limit=6):
    grouped = {}
    for prefix, products in (('current', curr_products or []), ('last', prev_products or [])):
        for product in products:
            product_type = str(product.get('type') or 'Other').title()
            sub_group = str(product.get('sub_group') or '').strip().title()
            label = f'{product_type} - {sub_group}' if sub_group else product_type
            grouped.setdefault(label, {'label': label, 'current': 0.0, 'last': 0.0})
            grouped[label][prefix] += float(product.get(value_key) or 0)

    rows = sorted(grouped.values(), key=lambda row: row['current'], reverse=True)[:limit]
    return [_glimpse_row(row['label'], row['current'], row['last'], formatter) for row in rows]


def _realisation_category_rows(curr_products, prev_products, limit=6):
    grouped = {}
    for prefix, products in (('current', curr_products or []), ('last', prev_products or [])):
        for product in products:
            product_type = str(product.get('type') or 'Other').title()
            sub_group = str(product.get('sub_group') or '').strip().title()
            label = f'{product_type} - {sub_group}' if sub_group else product_type
            grouped.setdefault(label, {'label': label, 'current_litres': 0.0, 'current_revenue': 0.0, 'last_litres': 0.0, 'last_revenue': 0.0})
            grouped[label][f'{prefix}_litres'] += float(product.get('litres') or 0)
            grouped[label][f'{prefix}_revenue'] += float(product.get('revenue') or 0)

    def rate(row, prefix):
        litres = row[f'{prefix}_litres']
        return row[f'{prefix}_revenue'] / litres if litres else 0.0

    rows = sorted(grouped.values(), key=lambda row: row['current_revenue'], reverse=True)[:limit]
    return [
        _glimpse_row(row['label'], rate(row, 'current'), rate(row, 'last'), lambda value: f'₹{value:.2f}/L')
        for row in rows
    ]


def _expense_category_rows(curr, prev, include=None, exclude=None):
    include = set(include or [])
    exclude = set(exclude or [])
    labels = set((curr or {}).keys()) | set((prev or {}).keys())
    if include:
        labels &= include
    labels -= exclude
    rows = [
        _glimpse_row(label, float((curr or {}).get(label, 0) or 0), float((prev or {}).get(label, 0) or 0), _format_inr_compact)
        for label in sorted(labels)
    ]
    return sorted(rows, key=lambda row: abs(row['raw_diff']), reverse=True)


def _month_range(year: int, month: int):
    today = date.today()
    if today.year == year and today.month == month:
        end_date = today.isoformat()
    else:
        last_day = calendar.monthrange(year, month)[1]
        end_date = f'{year:04d}-{month:02d}-{last_day:02d}'
    return (f'{year:04d}-{month:02d}-01', end_date)


def _prev_month(year: int, month: int):
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _stub_kpi(label, icon, gradient, extra=None):
    return {
        'label':       label,
        'icon':        icon,
        'gradient':    gradient,
        'value':       '—',
        'sub_value':   'Data not yet connected',
        'trend':       None,
        'trend_label': 'vs last month',
        'has_data':    False,
        'status':      'Data not yet connected',
        'extra':       extra or {},
    }


# ---------------------------------------------------------------------------
# Real KPI fetchers
# ---------------------------------------------------------------------------

def get_total_sales_volume(year: int, month: int) -> dict:
    from dashboard.services.realise.sales import get_sales_data

    start, end = _month_range(year, month)
    py, pm = _prev_month(year, month)
    prev_start, prev_end = _month_range(py, pm)

    try:
        curr_result = get_sales_data(start, end) or {}
        prev_result = get_sales_data(prev_start, prev_end) or {}
    except Exception as e:
        logger.error('[home] get_total_sales_volume failed: %s', e)
        return _stub_kpi('Total Sales Volume', 'local_shipping', 'blue')

    curr_ltrs = curr_result.get('total_litres', 0) or 0
    prev_ltrs = prev_result.get('total_litres', 0) or 0
    curr_rev  = curr_result.get('total_revenue', 0) or 0

    commodity_ltrs = sum(
        p.get('litres', 0) for p in curr_result.get('products', [])
        if p.get('type') == 'COMMODITY'
    )
    premium_ltrs = sum(
        p.get('litres', 0) for p in curr_result.get('products', [])
        if p.get('type') == 'PREMIUM'
    )

    return {
        'label':       'Total Sales Volume',
        'icon':        'local_shipping',
        'gradient':    'blue',
        'value':       _format_litres(curr_ltrs),
        'current_value': _format_litres(curr_ltrs),
        'last_value':  _format_litres(prev_ltrs),
        'sub_value':   f'Revenue: {_format_inr_compact(curr_rev)}',
        'trend':       _pct_change(curr_ltrs, prev_ltrs),
        'trend_label': 'vs last month',
        'has_data':    True,
        'extra': {
            'glimpse_id': 'kpi-glimpse-total-sales-volume',
            'glimpse_title': 'Total Sales Volume',
            'glimpse_rows': _sales_category_rows(
                curr_result.get('products', []),
                prev_result.get('products', []),
                'litres',
                _format_litres,
            ),
            'commodity_ltrs': _format_litres(commodity_ltrs),
            'premium_ltrs':   _format_litres(premium_ltrs),
        },
    }


def get_avg_realisation(year: int, month: int) -> dict:
    from dashboard.services.realise.sales import get_sales_data

    start, end = _month_range(year, month)
    py, pm = _prev_month(year, month)
    prev_start, prev_end = _month_range(py, pm)

    try:
        curr_result = get_sales_data(start, end) or {}
        prev_result = get_sales_data(prev_start, prev_end) or {}
    except Exception as e:
        logger.error('[home] get_avg_realisation failed: %s', e)
        return _stub_kpi('Avg. Realisation', 'show_chart', 'green')

    curr_ltrs = curr_result.get('total_litres', 0) or 0
    curr_rev  = curr_result.get('total_revenue', 0) or 0
    prev_ltrs = prev_result.get('total_litres', 0) or 0
    prev_rev  = prev_result.get('total_revenue', 0) or 0

    curr_rate = (curr_rev / curr_ltrs) if curr_ltrs else 0
    prev_rate = (prev_rev / prev_ltrs) if prev_ltrs else 0

    commodity_products = [p for p in curr_result.get('products', []) if p.get('type') == 'COMMODITY']
    premium_products   = [p for p in curr_result.get('products', []) if p.get('type') == 'PREMIUM']

    def _avg_rate(products):
        total_l = sum(p.get('litres', 0) for p in products)
        total_r = sum(p.get('revenue', 0) for p in products)
        return (total_r / total_l) if total_l else 0

    return {
        'label':       'Avg. Realisation',
        'icon':        'show_chart',
        'gradient':    'green',
        'value':       f'₹{curr_rate:.2f}/L',
        'current_value': f'₹{curr_rate:.2f}/L',
        'last_value':  f'₹{prev_rate:.2f}/L',
        'sub_value':   f'Revenue: {_format_inr_compact(curr_rev)}',
        'trend':       _pct_change(curr_rate, prev_rate),
        'trend_label': 'vs last month',
        'has_data':    True,
        'extra': {
            'glimpse_id': 'kpi-glimpse-avg-realisation',
            'glimpse_title': 'Avg. Realisation',
            'glimpse_rows': _realisation_category_rows(
                curr_result.get('products', []),
                prev_result.get('products', []),
            ),
            'commodity_rate': f'₹{_avg_rate(commodity_products):.2f}/L',
            'premium_rate':   f'₹{_avg_rate(premium_products):.2f}/L',
        },
    }


# ---------------------------------------------------------------------------
# Stub KPI fetchers (data not yet connected)
# ---------------------------------------------------------------------------

def get_cost_of_goods_sold(year: int, month: int) -> dict:
    from dashboard.cogs_service import get_cogs_data

    start, end = _month_range(year, month)
    py, pm = _prev_month(year, month)
    prev_start, prev_end = _month_range(py, pm)

    try:
        curr = get_cogs_data(start, end, 'Y') or {}
        prev = get_cogs_data(prev_start, prev_end, 'Y') or {}
    except Exception as e:
        logger.error('[home] get_cost_of_goods_sold failed: %s', e)
        return _stub_kpi('Cost of Goods Sold', 'receipt_long', 'orange')

    curr_total = curr.get('total_cogs', 0) or 0
    prev_total = prev.get('total_cogs', 0) or 0
    curr_per_l = curr.get('cogs_per_liter', 0) or 0
    curr_total_liter = curr.get('total_liter', 0) or 0
    opt_missing = curr.get('opt_missing')
    unlocked = not opt_missing

    return {
        'label':       'Cost of Goods Sold',
        'icon':        'receipt_long',
        'gradient':    'orange',
        'value':       _format_inr_compact(curr_total) if unlocked else _format_litres(curr_total_liter),
        'current_value': _format_inr_compact(curr_total) if unlocked else None,
        'last_value':  _format_inr_compact(prev_total) if unlocked else None,
        'sub_value':   f'₹{curr_per_l:.2f}/L · {_format_litres(curr_total_liter)}' if unlocked else 'Enter OTP to view COGS',
        'trend':       _pct_change(curr_total, prev_total) if unlocked else None,
        'trend_label': 'vs last month',
        'has_data':    bool(curr_total_liter or unlocked),
        'extra': {
            'is_cogs': True,
            'glimpse_id': 'kpi-glimpse-cogs',
            'glimpse_title': 'Cost of Goods Sold',
            'glimpse_rows': [_glimpse_row('COGS', curr_total, prev_total, _format_inr_compact)] if unlocked else [],
            'from_date': start,
            'to_date': end,
            'total_liter': _format_litres(curr_total_liter),
            'cogs_locked': opt_missing,
            'param_type': curr.get('param_type', ''),
        },
    }


def get_operating_expenses(year: int, month: int) -> dict:
    from dashboard.expenses_service import get_expenses_by_category

    py, pm = _prev_month(year, month)
    
    try:
        curr = get_expenses_by_category(month, year) or {}
        prev = get_expenses_by_category(pm, py) or {}
    except Exception as e:
        logger.error('[home] get_operating_expenses failed: %s', e)
        return _stub_kpi('Operating Expenses', 'account_balance_wallet', 'red')

    curr_total = sum(curr.values()) if curr else 0
    prev_total = sum(prev.values()) if prev else 0

    return {
        'label':       'Operating Expenses',
        'icon':        'account_balance_wallet',
        'gradient':    'red',
        'value':       _format_inr_compact(curr_total),
        'current_value': _format_inr_compact(curr_total),
        'last_value':  _format_inr_compact(prev_total),
        'sub_value':   f"Salary: {_format_inr_compact(curr.get('Salaries & HR', 0)) if curr else '₹0'}",
        'trend':       _pct_change(curr_total, prev_total),
        'trend_label': 'vs last month',
        'has_data':    True,
        'extra': {
            'glimpse_id': 'kpi-glimpse-opex',
            'glimpse_title': 'Operating Expenses',
            'glimpse_rows': _expense_category_rows(curr, prev),
        },
    }


def get_salary_expenditure(year: int, month: int) -> dict:
    from dashboard.expenses_service import get_expenses_by_category

    py, pm = _prev_month(year, month)
    
    try:
        curr = get_expenses_by_category(month, year) or {}
        prev = get_expenses_by_category(pm, py) or {}
    except Exception as e:
        logger.error('[home] get_salary_expenditure failed: %s', e)
        return _stub_kpi('Salary Expenditure', 'people', 'purple')

    curr_salary = curr.get('Salaries & HR', 0) if curr else 0
    prev_salary = prev.get('Salaries & HR', 0) if prev else 0

    return {
        'label':       'Salary Expenditure',
        'icon':        'people',
        'gradient':    'purple',
        'value':       _format_inr_compact(curr_salary),
        'current_value': _format_inr_compact(curr_salary),
        'last_value':  _format_inr_compact(prev_salary),
        'sub_value':   'All locations & depts',
        'trend':       _pct_change(curr_salary, prev_salary),
        'trend_label': 'vs last month',
        'has_data':    True,
        'extra': {
            'glimpse_id': 'kpi-glimpse-salaries',
            'glimpse_title': 'Salary Expenditure',
            'glimpse_rows': _expense_category_rows(curr, prev, include={'Salaries & HR'}),
        },
    }


def get_inventory_value(year: int, month: int) -> dict:
    try:
        from inventory.services.beverages import get_kpi as bev_kpi
        from inventory.services.beverages import get_warehouses as bev_warehouses
        from inventory.services.oils import get_kpi as oils_kpi
        from inventory.services.oils import get_warehouses as oils_warehouses

        oil_row = (oils_kpi() or [{}])[0]
        bev_row = (bev_kpi() or [{}])[0]
        total_value = (oil_row.get('TotalValue') or 0) + (bev_row.get('TotalValue') or 0)
        total_skus = (oil_row.get('TotalSKUs') or 0) + (bev_row.get('TotalSKUs') or 0)

        warehouses = {
            row.get('WhsCode')
            for row in (oils_warehouses() or []) + (bev_warehouses() or [])
            if row.get('WhsCode')
        }

        return {
            'label':       'Inventory Value',
            'icon':        'inventory_2',
            'gradient':    'indigo',
            'value':       _format_inr_compact(total_value),
            'sub_value':   f'{_indian_grouping(round(total_skus))} SKUs',
            'trend':       None,
            'trend_label': 'point-in-time',
            'has_data':    True,
            'extra': {
                'sku_count': round(total_skus),
                'warehouse_count': len(warehouses),
                'unit_count': 2,
            },
        }
    except Exception as e:
        logger.error('[home] get_inventory_value failed: %s', e)
        return _stub_kpi(
            'Inventory Value', 'inventory_2', 'indigo',
            extra={'sku_count': None, 'warehouse_count': 0, 'unit_count': None},
        )
