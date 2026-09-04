"""One shared cache in front of the three heavy SAP pulls.

The problem this solves
-----------------------
Building the navbar ticker asked SAP for the *same* data four times:

    get_total_sales_volume  -> get_sales_data(this month) + get_sales_data(last month)
    get_avg_realisation     -> get_sales_data(this month) + get_sales_data(last month)

Each of those calls REPORT_SALES_ANALYSIS, which takes 1.5 seconds. Four calls
= 6 seconds, for two distinct answers. The same thing happened with expenses
(operating expenses + salaries both pull get_expenses_by_category twice), and
the home page then repeated the whole lot a second time for its KPI cards.

What this module does
---------------------
It wraps those three functions. The wrappers have the SAME name, the SAME
arguments and return the SAME data - only now a repeat call within the cache
window is answered from memory instead of from SAP.

Three behaviours worth knowing:

1. Duplicate calls collapse. The four sales pulls become two.
2. Single flight. If ten requests arrive with a cold cache, one goes to SAP
   and the other nine wait for its answer, instead of ten stampeding SAP.
3. Serve stale, refresh behind. Once a value is older than its "fresh" window
   the old value is handed back immediately and a background thread fetches a
   new one. Nobody waits for a refresh.

Nothing here changes any SQL, any stored procedure, or any calculation. Every
caller gets a deep copy, so no caller can accidentally corrupt another's data.
"""

import copy
import logging
import threading
import time
from datetime import date

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# How long an answer counts as fresh.
# The current month keeps moving, so it gets a short window. Finished months
# change rarely, so they can be held much longer.
DEFAULT_TTL_CURRENT_MONTH = 300     # 5 minutes
DEFAULT_TTL_PAST_MONTH = 1800       # 30 minutes

# How long a stale answer may still be handed out while a refresh runs behind.
DEFAULT_STALE_FOR = 3600            # 1 hour

_PREFIX = 'kpisrc'

# One lock per cache key, so only one thread fetches a given key at a time.
_key_locks = {}
# Keys currently being refreshed in the background.
_refreshing = set()
_guard = threading.Lock()


def _setting(name, default):
    return getattr(settings, name, default)


def _ttl_for_month(year, month):
    today = date.today()
    if (year, month) >= (today.year, today.month):
        return _setting('KPI_CACHE_TTL_CURRENT_MONTH', DEFAULT_TTL_CURRENT_MONTH)
    return _setting('KPI_CACHE_TTL_PAST_MONTH', DEFAULT_TTL_PAST_MONTH)


def _ttl_for_range(start_date, end_date):
    """Work out the month a date range belongs to, then pick its TTL."""
    try:
        year, month = int(str(start_date)[:4]), int(str(start_date)[5:7])
        return _ttl_for_month(year, month)
    except (ValueError, TypeError, IndexError):
        return _setting('KPI_CACHE_TTL_CURRENT_MONTH', DEFAULT_TTL_CURRENT_MONTH)


def _lock_for(key):
    with _guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock


def _store(key, value, ttl):
    stale_for = _setting('KPI_CACHE_STALE_FOR', DEFAULT_STALE_FOR)
    cache.set(
        key,
        {'value': value, 'fresh_until': time.time() + ttl},
        ttl + stale_for,
    )


def _refresh_behind(key, ttl, producer, is_usable):
    """Fetch a new value in the background. Only one refresh per key at a time."""
    with _guard:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def work():
        try:
            value = producer()
            # Never overwrite a good cached value with a failure.
            if is_usable(value):
                _store(key, value, ttl)
        except Exception as e:
            logger.warning('[kpi_cache] background refresh of %s failed: %s', key, e)
        finally:
            with _guard:
                _refreshing.discard(key)

    threading.Thread(target=work, daemon=True, name=f'kpi-refresh-{key}').start()


def _usable(value):
    """A failed pull returns None (or nothing at all) - do not cache that."""
    return value is not None and value != {} and value != []


def _cached(key, ttl, producer, is_usable=_usable):
    entry = cache.get(key)

    if entry is not None:
        if time.time() < entry['fresh_until']:
            return copy.deepcopy(entry['value'])
        # Stale but usable: hand it over now, get a fresh one behind the scenes.
        _refresh_behind(key, ttl, producer, is_usable)
        return copy.deepcopy(entry['value'])

    # Nothing cached. Only one thread should go to SAP for this key.
    with _lock_for(key):
        entry = cache.get(key)
        if entry is not None:
            return copy.deepcopy(entry['value'])
        value = producer()
        if is_usable(value):
            _store(key, value, ttl)
        return copy.deepcopy(value)


def ttl_for_range(start_date, end_date):
    """Public: how long an answer for this date range may be held.

    A range inside the current month gets the short window; a finished month
    gets the long one, because its numbers barely move.
    """
    return _ttl_for_range(start_date, end_date)


def remember(key, ttl, producer, is_usable=None):
    """Public version of the cache-with-single-flight helper above.

    Anything expensive and read-only can use it:

        rows = remember('mykey', 120, lambda: run_the_slow_thing())

    A repeat call inside the window is answered from the cache; concurrent
    callers with a cold cache wait for one fetch instead of all fetching; and
    once the value goes stale the old one is returned instantly while a fresh
    one is fetched behind the scenes.
    """
    return _cached(key, ttl, producer, is_usable or _usable)


# ---------------------------------------------------------------------------
# The three wrapped pulls. Same names, same arguments, same return values.
# ---------------------------------------------------------------------------

def get_sales_data(start_date, end_date):
    """Cached REPORT_SALES_ANALYSIS (the 1.5 second one)."""
    from dashboard.services.realise.sales import get_sales_data as _raw

    key = f'{_PREFIX}:sales:{start_date}:{end_date}'
    return _cached(key, _ttl_for_range(start_date, end_date),
                   lambda: _raw(start_date, end_date))


def get_expenses_by_category(month, year):
    """Cached expenses-by-category pull."""
    from dashboard.expenses_service import get_expenses_by_category as _raw

    key = f'{_PREFIX}:expenses:{year}-{month:02d}'
    return _cached(key, _ttl_for_month(year, month),
                   lambda: _raw(month, year))


def get_cogs_data(start_date, end_date, flag='Y'):
    """Cached cost-of-goods-sold pull."""
    from dashboard.cogs_service import get_cogs_data as _raw

    key = f'{_PREFIX}:cogs:{start_date}:{end_date}:{flag}'
    return _cached(key, _ttl_for_range(start_date, end_date),
                   lambda: _raw(start_date, end_date, flag))


def clear():
    """Drop everything this module cached (used by tests)."""
    # Redis backends can delete by pattern; the plain in-memory one cannot,
    # so there we clear the whole cache.
    if hasattr(cache, 'delete_pattern'):
        for name in ('sales', 'expenses', 'cogs'):
            cache.delete_pattern(f'{_PREFIX}:{name}:*')
    else:
        cache.clear()
