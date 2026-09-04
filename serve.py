#!/usr/bin/env python
"""Run the control panel on a real web server instead of Django's runserver.

Why: `manage.py runserver` is a development tool. Django prints a warning about
it on every start. It is single-process, does no request queueing worth the
name, and is not meant to face real users.

This picks the right server for the machine it is on:
  * Windows  -> waitress  (gunicorn cannot run on Windows: it needs fork())
  * Linux    -> gunicorn  (several worker processes, the normal production choice)

Usage:
    python serve.py                 # 0.0.0.0:9080
    python serve.py 127.0.0.1:9090  # somewhere else
    WEB_WORKERS=8 python serve.py   # more workers (default: CPU count, max 8)
"""

import multiprocessing
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _bind():
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.environ.get('WEB_BIND', '0.0.0.0:9080')


def _workers():
    asked = os.environ.get('WEB_WORKERS')
    if asked:
        return int(asked)
    return min(8, max(2, multiprocessing.cpu_count()))


def main():
    bind = _bind()
    host, _, port = bind.rpartition(':')
    workers = _workers()

    from config.wsgi import application

    if os.name == 'nt':
        from waitress import serve
        print(f'waitress: http://{bind}  ({workers} threads)')
        # Waitress uses threads, not processes - the right model on Windows.
        serve(application, host=host or '0.0.0.0', port=int(port),
              threads=workers * 4, channel_timeout=120)
    else:
        # Import here so Windows never tries to load gunicorn.
        from gunicorn.app.base import BaseApplication

        class _App(BaseApplication):
            def load_config(self):
                self.cfg.set('bind', bind)
                self.cfg.set('workers', workers)
                # Threads matter here: most of a request is spent waiting on
                # SAP, not burning CPU, so each worker can handle several.
                self.cfg.set('threads', 4)
                self.cfg.set('worker_class', 'gthread')
                self.cfg.set('timeout', 120)
                self.cfg.set('graceful_timeout', 30)
                self.cfg.set('accesslog', '-')
                self.cfg.set('errorlog', '-')

            def load(self):
                return application

        print(f'gunicorn: http://{bind}  ({workers} workers x 4 threads)')
        _App().run()


if __name__ == '__main__':
    main()
