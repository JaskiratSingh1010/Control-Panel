import os
import sys
import threading

from django.apps import AppConfig


class RealiseConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'realise'

    def ready(self):
        # Pre-warm the expensive SAP sales cache in the background so the first dashboard
        # load after a server start is instant instead of a ~25s cold proc. Only under a
        # real server run (runserver/gunicorn) — never during migrate/makemigrations/etc.
        # and never in the autoreloader's parent process (RUN_MAIN guards the worker).
        argv = ' '.join(sys.argv)
        is_server = ('runserver' in argv) or ('gunicorn' in argv) or ('waitress' in argv)
        is_management = any(c in argv for c in (
            'migrate', 'makemigrations', 'collectstatic', 'shell', 'test', 'createsuperuser'))
        if not is_server or is_management:
            return
        # Under `runserver` WITH autoreload there are two processes; only the worker
        # (RUN_MAIN=true) should warm. With `--noreload` (or gunicorn) there's a single
        # process, so warm it directly.
        if 'runserver' in argv and '--noreload' not in argv and os.environ.get('RUN_MAIN') != 'true':
            return   # autoreload parent — let the worker do it

        def _warm():
            try:
                from . import services
                services.prewarm_sales_cache()
            except Exception:
                pass

        threading.Thread(target=_warm, daemon=True).start()
