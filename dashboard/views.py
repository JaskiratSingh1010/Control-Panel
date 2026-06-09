import calendar
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt

from core.decorators import permission_flag_required
from .cogs_service import get_cogs_data, update_cogs_opt
from .expenses_service import get_expense_rows, get_expenses_by_category, format_compact_inr
from .models import ExpenseBudget
from .services.realise.sales import get_sales_comparison, get_sales_data

MONTH_NAMES = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

KEY_MAP = {
    'Salaries & HR': 'Salaries_HR',
    'Sales & Marketing': 'Sales_Marketing',
    'Operations & Factory': 'Operations_Factory',
    'Admin & General': 'Admin_General',
    'Finance & Statutory': 'Finance_Statutory',
}

DUMMY_DATA = {
    'Salaries & HR': 4630000,
    'Sales & Marketing': 1840000,
    'Operations & Factory': 2280000,
    'Admin & General': 860000,
    'Finance & Statutory': 1150000,
}


def _fmt_inr(n):
    if n is None:
        return '–'
    n = float(n)
    sign = '-' if n < 0 else ''
    n = abs(n)
    if n >= 1e7:
        return f'{sign}₹{n / 1e7:.2f} Cr'
    if n >= 1e5:
        return f'{sign}₹{n / 1e5:.1f} L'
    if n >= 1e3:
        return f'{sign}₹{n / 1e3:.1f}K'
    return f'{sign}₹{n:,.0f}'


def _pct_change(current, last):
    if not last:
        return 0
    return round((float(current or 0) - float(last)) / float(last) * 100, 1)


def _month_range(month, year):
    today = date.today()
    start = date(year, month, 1)
    end = today if today.year == year and today.month == month else date(year, month, calendar.monthrange(year, month)[1])
    return start.isoformat(), end.isoformat()


def _prev_month(month, year):
    return (12, year - 1) if month == 1 else (month - 1, year)


def _all_zero(data):
    return not data or all(float(v or 0) == 0 for v in data.values())


def _safe_expenses(month, year):
    try:
        data = get_expenses_by_category(month, year)
        return DUMMY_DATA.copy() if _all_zero(data) else data
    except Exception:
        return DUMMY_DATA.copy()


def _build_expenses_context(raw, raw_last, month, year):
    total = sum(float(raw.get(cat, 0) or 0) for cat in KEY_MAP)
    total_last = sum(float(raw_last.get(cat, 0) or 0) for cat in KEY_MAP)
    salary_current = float(raw.get('Salaries & HR', 0) or 0)
    salary_last = float(raw_last.get('Salaries & HR', 0) or 0)

    expenses = {}
    expenses_last = {}
    expenses_detail = {}
    for category, key in KEY_MAP.items():
        amount = float(raw.get(category, 0) or 0)
        last_amount = float(raw_last.get(category, 0) or 0)
        amount_lakhs = round(amount / 100000, 1)
        last_lakhs = round(last_amount / 100000, 1)
        expenses[key] = amount_lakhs
        expenses_last[key] = last_lakhs
        expenses_detail[key] = {
            'label': category,
            'amount_lakhs': amount_lakhs,
            'amount_display': format_compact_inr(amount),
            'pct': round((amount / total * 100), 1) if total else 0,
            'mom_change': _pct_change(amount, last_amount),
            'last_lakhs': last_lakhs,
            'last_display': format_compact_inr(last_amount),
        }

    total_ex_salary = total - salary_current
    total_last_ex_salary = total_last - salary_last
    return {
        'expenses': expenses,
        'expenses_last': expenses_last,
        'expenses_detail': expenses_detail,
        'total_opex_lakhs': round(total / 100000, 1),
        'total_opex_last_lakhs': round(total_last / 100000, 1),
        'opex_trend': _pct_change(total, total_last),
        'total_opex_display': format_compact_inr(total),
        'total_opex_last_display': format_compact_inr(total_last),
        'opex_excl_salary_trend': _pct_change(total_ex_salary, total_last_ex_salary),
        'total_opex_excl_salary_display': format_compact_inr(total_ex_salary),
        'total_opex_excl_salary_last_display': format_compact_inr(total_last_ex_salary),
        'month_label': f'{MONTH_NAMES[month]} {year}',
    }


@cache_page(60 * 3)
def index(request):
    today = date.today()
    explicit_period = request.GET.get('month') or request.GET.get('year')
    display_month = int(request.GET.get('month') or today.month)
    display_year = int(request.GET.get('year') or today.year)
    showing_last_month = False
    last_month_indicator = ''

    if today.day == 1 and not explicit_period:
        display_month, display_year = _prev_month(today.month, today.year)
        showing_last_month = True
        last_month_indicator = 'Showing last month because today is the first day of a new month.'

    last_month, last_year = _prev_month(display_month, display_year)
    start, end = _month_range(display_month, display_year)

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_exp = pool.submit(_safe_expenses, display_month, display_year)
        f_exp_last = pool.submit(_safe_expenses, last_month, last_year)
        f_sales = pool.submit(get_sales_comparison, display_month, display_year)
        f_cogs = pool.submit(get_cogs_data, start, end)
        raw = f_exp.result()
        raw_last = f_exp_last.result()
        sales_current, sales_last = f_sales.result()
        cogs = f_cogs.result()

    context = _build_expenses_context(raw, raw_last, display_month, display_year)
    sales_current = sales_current or {}
    sales_last = sales_last or {}
    salary_current = raw.get('Salaries & HR', 0)
    salary_last = raw_last.get('Salaries & HR', 0)
    opt_missing = bool(cogs.get('opt_missing'))

    context.update({
        'showing_last_month': showing_last_month,
        'last_month_indicator': last_month_indicator,
        'sales_current_tonnes': sales_current.get('total_tonnes', '–'),
        'sales_current_litres': sales_current.get('total_litres', '–'),
        'realise_current': sales_current.get('net_realise', '–'),
        'sales_revenue': sales_current.get('total_revenue', '–'),
        'sales_products': sales_current.get('products', []),
        'sales_last_tonnes': sales_last.get('total_tonnes', '–'),
        'sales_last_litres': sales_last.get('total_litres', '–'),
        'realise_last': sales_last.get('net_realise', '–'),
        'sales_trend': _pct_change(sales_current.get('total_litres'), sales_last.get('total_litres')),
        'realise_trend': _pct_change(sales_current.get('net_realise'), sales_last.get('net_realise')),
        'salary_current_display': format_compact_inr(salary_current),
        'salary_last_display': format_compact_inr(salary_last),
        'salary_trend': _pct_change(salary_current, salary_last),
        'cogs_total_liter': '–' if opt_missing else cogs.get('total_liter', 0),
        'cogs_per_liter': '–' if opt_missing else cogs.get('cogs_per_liter', 0),
        'cogs_total': '–' if opt_missing else cogs.get('total_cogs', 0),
        'cogs_opt_missing': opt_missing,
        'current_month_name': MONTH_NAMES[display_month],
        'last_month_name': MONTH_NAMES[last_month],
        'active_page': 'index',
    })
    return render(request, 'dashboard/index.html', context)


@permission_flag_required('can_expenses')
def expenses_dashboard(request):
    today = date.today()
    from_date = request.GET.get('from_date') or date(today.year, today.month, 1).isoformat()
    to_date = request.GET.get('to_date') or today.isoformat()
    return render(request, 'dashboard/expenses_dashboard.html', {
        'active_page': 'expenses',
        'sidebar_active': 'expenses',
        'from_date': from_date,
        'to_date': to_date,
    })


@permission_flag_required('can_salaries')
def salary_dashboard(request):
    today = date.today()
    from_date = request.GET.get('from_date') or date(today.year, today.month, 1).isoformat()
    to_date = request.GET.get('to_date') or today.isoformat()
    return render(request, 'dashboard/salary_dashboard.html', {
        'active_page': 'salary',
        'sidebar_active': 'salaries',
        'from_date': from_date,
        'to_date': to_date,
    })


@permission_flag_required('can_expenses', json_response=True)
def expenses_api(request):
    today = date.today()
    month = int(request.GET.get('month') or today.month)
    year = int(request.GET.get('year') or today.year)
    last_month, last_year = _prev_month(month, year)
    context = _build_expenses_context(_safe_expenses(month, year), _safe_expenses(last_month, last_year), month, year)
    return JsonResponse({
        'status': 'ok',
        'total_opex_lakhs': context['total_opex_lakhs'],
        'total_opex_display': context['total_opex_display'],
        'total_opex_excl_salary_display': context['total_opex_excl_salary_display'],
        'total_opex_excl_salary_last_display': context['total_opex_excl_salary_last_display'],
        'opex_excl_salary_trend': context['opex_excl_salary_trend'],
        'expenses': context['expenses'],
        'expenses_detail': context['expenses_detail'],
    })


def _validated_range(request):
    today = date.today()
    from_date = request.GET.get('from_date') or date(today.year, today.month, 1).isoformat()
    to_date = request.GET.get('to_date') or today.isoformat()
    datetime.fromisoformat(from_date)
    datetime.fromisoformat(to_date)
    return from_date, to_date


@permission_flag_required('can_salaries', json_response=True)
def salary_detail_api(request):
    try:
        from_date, to_date = _validated_range(request)
    except ValueError:
        return JsonResponse({'status': 'error', 'message': 'Invalid date'}, status=400)
    account_filter = (request.GET.get('account_name') or '').lower()
    all_rows = get_expense_rows(from_date, to_date)
    rows = [r for r in all_rows if r.get('is_salary')]
    if account_filter:
        rows = [r for r in rows if account_filter in str(r.get('account_name') or '').lower()]

    groups = {}
    for row in rows:
        account = row.get('account_name') or 'Unknown'
        groups.setdefault(account, {'account': account, 'total': 0.0, 'row_count': 0, 'rows': []})
        groups[account]['total'] += row['amount']
        groups[account]['row_count'] += 1
        groups[account]['rows'].append(row)

    group_list = sorted(groups.values(), key=lambda g: g['total'], reverse=True)
    for group in group_list:
        group['total_display'] = format_compact_inr(group['total'])
    total = sum(r['amount'] for r in rows)
    return JsonResponse({
        'status': 'ok',
        'filters': {'account_names': sorted({r.get('account_name') for r in all_rows if r.get('account_name')})},
        'kpis': {
            'total_salary': total,
            'total_salary_display': format_compact_inr(total),
            'row_count': len(rows),
            'account_count': len(groups),
        },
        'groups': group_list,
        'rows': rows,
        'debug': {
            'total_fetched': len(all_rows),
            'salary_rows': len(rows),
            'seen_budgets': list(dict.fromkeys(
                f"{r.get('budget')} / {r.get('account_name')}" for r in all_rows
            ))[:30],
        },
    })


@permission_flag_required('can_expenses', json_response=True)
def expenses_detail_api(request):
    try:
        from_date, to_date = _validated_range(request)
    except ValueError:
        return JsonResponse({'status': 'error', 'message': 'Invalid date'}, status=400)
    account_filter = (request.GET.get('account_name') or '').lower()
    variety_filter = (request.GET.get('variety') or '').upper()
    expense_type = (request.GET.get('expense_type') or 'all').lower()

    rows = [r for r in get_expense_rows(from_date, to_date) if not r.get('is_salary')]
    if account_filter:
        rows = [r for r in rows if account_filter in str(r.get('account_name') or '').lower()]
    if variety_filter:
        rows = [r for r in rows if str(r.get('variety') or '').upper() == variety_filter]
    if expense_type in ('direct', 'indirect'):
        rows = [r for r in rows if str(r.get('expense_type') or '').lower() == expense_type]

    from_dt = datetime.fromisoformat(from_date)
    stored_budgets = {
        b.budget_head: b.budget_amount
        for b in ExpenseBudget.objects.filter(month=from_dt.month, year=from_dt.year)
    }

    groups = {}
    for row in rows:
        budget_head = row.get('budget') or 'Unassigned'
        groups.setdefault(budget_head, {
            'category': budget_head,
            'total': 0.0,
            'budget_total': 0.0,
            'row_count': 0,
            'rows': [],
        })
        groups[budget_head]['total'] += row['amount']
        groups[budget_head]['budget_total'] += row.get('current_month_budget') or 0
        groups[budget_head]['row_count'] += 1
        groups[budget_head]['rows'].append(row)

    group_list = []
    for key, group in groups.items():
        if key in stored_budgets:
            group['budget_total'] = stored_budgets[key]
        group['variance'] = group['total'] - group['budget_total']
        group['total_display'] = format_compact_inr(group['total'])
        group['budget_total_display'] = format_compact_inr(group['budget_total'])
        group['variance_display'] = format_compact_inr(group['variance'])
        group_list.append(group)
    group_list.sort(key=lambda g: g['total'], reverse=True)

    total_spent = sum(g['total'] for g in group_list)
    budget_total = sum(g['budget_total'] for g in group_list)
    highest = group_list[0] if group_list else {}
    lowest = group_list[-1] if group_list else {}
    return JsonResponse({
        'status': 'ok',
        'filters': {
            'account_names': sorted({r.get('account_name') for r in rows if r.get('account_name')}),
            'varieties': sorted({r.get('variety') for r in rows if r.get('variety')}),
        },
        'kpis': {
            'total_spent': total_spent,
            'total_spent_display': format_compact_inr(total_spent),
            'highest_spend_display': format_compact_inr(highest.get('total')),
            'highest_spend_account': highest.get('category', ''),
            'lowest_spend_display': format_compact_inr(lowest.get('total')),
            'lowest_spend_account': lowest.get('category', ''),
            'budget_total_display': format_compact_inr(budget_total),
            'budget_vs_actual_display': format_compact_inr(total_spent - budget_total),
        },
        'groups': group_list,
    })


@permission_flag_required('can_cogs', json_response=True)
def cogs_api(request):
    today = date.today()
    from_date = request.GET.get('from_date') or date(today.year, today.month, 1).isoformat()
    to_date = request.GET.get('to_date') or today.isoformat()
    param_type = request.GET.get('param_type')
    if param_type is None:
        param_type = 'Y'
    otp = request.GET.get('otp') or ''
    cogs = get_cogs_data(from_date, to_date, param_type, otp)
    opt_missing = bool(cogs.get('opt_missing'))
    return JsonResponse({'status': 'ok', 'cogs': {
        'total_liter': cogs.get('total_liter', 0),
        'cogs_per_liter': cogs.get('cogs_per_liter', 0),
        'total_cogs': cogs.get('total_cogs', 0),
        'total_liter_display': f"{float(cogs.get('total_liter', 0) or 0):,.0f} L",
        'total_cogs_display': '–' if opt_missing else _fmt_inr(cogs.get('total_cogs', 0)),
        'cogs_per_liter_display': '–' if opt_missing else f"₹{float(cogs.get('cogs_per_liter', 0) or 0):.2f}/L",
        'opt_missing': opt_missing,
        'param_type': cogs.get('param_type', ''),
    }})


@csrf_exempt
@permission_flag_required('can_cogs', json_response=True)
def cogs_opt_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)
    body = json.loads(request.body or '{}')
    ok, message = update_cogs_opt(body.get('opt'))
    return JsonResponse({'status': 'ok' if ok else 'error', 'message': message})


def ticker_api(request):
    today = date.today()
    start = date(today.year, today.month, 1).isoformat()
    end = today.isoformat()
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_sales = pool.submit(get_sales_data, start, end)
        f_exp = pool.submit(_safe_expenses, today.month, today.year)
        sales = f_sales.result()
        expenses = f_exp.result()

    items = []
    if sales:
        products = sales.get('products', [])
        def realise_for(product_type):
            selected = [p for p in products if p.get('type') == product_type]
            litres = sum(p.get('litres', 0) for p in selected)
            revenue = sum(p.get('revenue', 0) for p in selected)
            return revenue / litres if litres else None

        premium_realise = realise_for('PREMIUM')
        commodity_realise = realise_for('COMMODITY')
        items += [
            {'label': 'TOTAL SALES', 'value': f"{sales.get('total_litres', 0):,.0f} L", 'dir': 'up'},
            {'label': 'AVG REALISE', 'value': f"₹{sales.get('net_realise', 0):.1f}/L", 'dir': 'up'},
            {'label': 'PREMIUM REALISE', 'value': f'₹{premium_realise:.1f}/L' if premium_realise else '–', 'dir': 'up'},
            {'label': 'COMMODITY REALISE', 'value': f'₹{commodity_realise:.1f}/L' if commodity_realise else '–', 'dir': 'neutral'},
            {'label': 'REVENUE', 'value': _fmt_inr(sales.get('total_revenue')), 'dir': 'up'},
        ]
    if expenses:
        items += [
            {'label': 'TOTAL OPEX', 'value': format_compact_inr(sum(expenses.values())), 'dir': 'down'},
            {'label': 'SALARY', 'value': format_compact_inr(expenses.get('Salaries & HR', 0)), 'dir': 'neutral'},
        ]
    return JsonResponse({'status': 'ok', 'items': items, 'month': f'{MONTH_NAMES[today.month]} {today.year}'})


@permission_flag_required('can_expenses', json_response=True)
def expenses_budgets_api(request):
    today = date.today()
    month = int(request.GET.get('month') or today.month)
    year = int(request.GET.get('year') or today.year)
    budgets = {
        row.budget_head: row.budget_amount
        for row in ExpenseBudget.objects.filter(month=month, year=year)
    }
    return JsonResponse({'status': 'ok', 'budgets': budgets})


@csrf_exempt
@permission_flag_required('can_expenses', json_response=True)
def expenses_budgets_update(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)
    body = json.loads(request.body or '{}')
    month = int(body.get('month'))
    year = int(body.get('year'))
    updated = 0
    for budget_head, value in (body.get('budgets') or {}).items():
        ExpenseBudget.objects.update_or_create(
            budget_head=budget_head,
            month=month,
            year=year,
            defaults={'budget_amount': float(value or 0)},
        )
        updated += 1
    return JsonResponse({'status': 'ok', 'updated': updated})
