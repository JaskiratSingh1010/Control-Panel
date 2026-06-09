import calendar
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.shortcuts import render

from . import services

logger = logging.getLogger(__name__)

CACHE_TTL = 180  # 3 minutes

_KPI_FETCHERS = [
    ('total_sales_volume', services.get_total_sales_volume),
    ('avg_realisation',    services.get_avg_realisation),
    ('cogs',               services.get_cost_of_goods_sold),
    ('opex',               services.get_operating_expenses),
    ('salaries',           services.get_salary_expenditure),
    ('inventory',          services.get_inventory_value),
]


def _build_period_options():
    today = date.today()
    options = []
    year, month = today.year, today.month
    for _ in range(13):
        label = date(year, month, 1).strftime('%B %Y')
        options.append({'value': f'{year}-{month:02d}', 'label': label})
        month -= 1
        if month == 0:
            month = 12
            year -= 1
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


@login_required
def index(request):
    year, month = _resolve_period(request)
    nocache = request.GET.get('nocache') == '1'

    cache_key = f'home_kpis_{year}_{month:02d}'
    kpis = None if nocache else cache.get(cache_key)

    if kpis is None:
        kpis = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(fn, year, month): name
                for name, fn in _KPI_FETCHERS
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    kpis[name] = future.result()
                except Exception as e:
                    logger.error('[home] KPI "%s" raised: %s', name, e)
                    kpis[name] = services._stub_kpi(name, 'error', 'grey')
        cache.set(cache_key, kpis, CACHE_TTL)

    period_month_name = date(year, month, 1).strftime('%B')

    ctx = {
        'sidebar_active': 'home',
        'kpis':           kpis,
        'period_year':    year,
        'period_month':   month,
        'period_label':   f'{period_month_name} {year}',
        'period_options': _build_period_options(),
        'selected_period': f'{year}-{month:02d}',
    }
    return render(request, 'home/index.html', ctx)
