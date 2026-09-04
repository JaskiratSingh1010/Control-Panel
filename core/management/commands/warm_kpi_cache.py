"""Fill the KPI caches ahead of time so no real visitor pays the SAP wait.

Run it on a schedule (every 5 minutes is a good start):

    python manage.py warm_kpi_cache

It builds the top-strip ticker and the home-page KPI cards for the current
month, and by default the previous month too, because every "vs last month"
figure needs it.
"""

import time
from datetime import date

from django.core.management.base import BaseCommand

from core.context_processors import get_ticker_items
from home import services as home_services
from home.views import _KPI_FETCHERS, CACHE_TTL
from django.core.cache import cache


def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


class Command(BaseCommand):
    help = 'Pre-build the ticker and home KPI caches so pages load instantly.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--months', type=int, default=2,
            help='How many months back to warm, counting this one (default 2).',
        )

    def handle(self, *args, **options):
        today = date.today()
        year, month = today.year, today.month

        for _ in range(max(1, options['months'])):
            started = time.time()

            # 1. the top strip
            get_ticker_items(year, month, blocking=True)

            # 2. the home page cards
            kpis = {}
            for name, fetcher in _KPI_FETCHERS:
                try:
                    kpis[name] = fetcher(year, month)
                except Exception as e:
                    self.stderr.write(f'  {name} failed: {e}')
            if kpis:
                cache.set(f'home_kpis_{year}_{month:02d}', kpis, CACHE_TTL)

            self.stdout.write(self.style.SUCCESS(
                f'warmed {year}-{month:02d} in {time.time() - started:.1f}s'
            ))
            year, month = _prev_month(year, month)
