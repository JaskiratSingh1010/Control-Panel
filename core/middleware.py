"""A stopwatch on every request.

Why this exists: when the control panel felt slow, nobody could say *which*
page was slow, or why. Every answer had to come from a one-off script. This
puts the measurement inside the app, permanently, so the question "what is
slow today?" is answered by reading a log instead of guessing.

For each request it records:

    2.31s  200  /realise/api/sales-data/   sap=1new+3reuse  sql=8  user=admin

  * how long the whole request took
  * how many brand-new SAP connections it needed (new ones are the expensive
    part - a healthy request should almost always show 0new)
  * how many pooled connections it reused
  * how many database queries it ran (a sudden jump here means an N+1 bug)

Only requests slower than SLOW_REQUEST_SECONDS are logged, so normal traffic
does not fill the disk. Set it to 0 to log everything while investigating.

Every response also carries an X-Response-Time header, so you can see the
number in your browser's Network tab without opening any log.
"""

import logging
import time

from django.conf import settings
from django.db import connection

logger = logging.getLogger('core.timing')

DEFAULT_SLOW_SECONDS = 1.0


class RequestTimingMiddleware:
    """Times every request and logs the slow ones."""

    def __init__(self, get_response):
        self.get_response = get_response

    def _threshold(self):
        return float(getattr(settings, 'SLOW_REQUEST_SECONDS', DEFAULT_SLOW_SECONDS))

    def __call__(self, request):
        from core import sap_connector

        before = sap_connector.get_stats()
        sql_before = len(connection.queries)
        started = time.perf_counter()

        response = self.get_response(request)

        took = time.perf_counter() - started
        response['X-Response-Time'] = f'{took:.3f}s'

        threshold = self._threshold()
        if took < threshold:
            return response

        after = sap_connector.get_stats()
        new = after['created'] - before['created']
        reused = after['reused'] - before['reused']
        # connection.queries is only filled when DEBUG is on, so this is a
        # best-effort number rather than something to rely on in production.
        sql = len(connection.queries) - sql_before

        user = getattr(request, 'user', None)
        who = user.username if user is not None and user.is_authenticated else 'anon'

        logger.warning(
            '%.2fs  %s  %s  sap=%dnew+%dreuse  sql=%d  user=%s',
            took, response.status_code, request.get_full_path()[:120],
            new, reused, sql, who,
        )
        return response
