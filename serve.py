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
    python serve.py --https         # HTTPS on 0.0.0.0:9443 using certs/

HTTPS
-----
Chrome refuses to save a file downloaded from a plain http:// page ("blocked
because the site isn't using a secure connection"), so every Export Excel on a
LAN-shared panel fails until the site is served over https.

Waitress has no TLS of its own and this panel must install without reaching the
internet, so rather than add a dependency the TLS is terminated here, with the
standard library: waitress keeps serving plain HTTP on the loopback interface,
and a small front-end accepts TLS on the public port and pipes the bytes through.
It is a byte pipe, not an HTTP proxy - it never parses or rewrites the request,
so there is nothing to get wrong about headers, chunking or streaming downloads.

Generate the certificate with deploy/make_cert.sh. The .crt is handed to users to
trust once; the .key stays on the server and is git-ignored.
"""

import multiprocessing
import os
import socket
import ssl
import sys
import threading

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



def _https_opts():
    """(certfile, keyfile) when HTTPS is asked for, else None.

    Switched on by --https or WEB_CERT/WEB_KEY. Defaults to the pair that
    deploy/make_cert.sh writes, so `python serve.py --https` needs no arguments.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cert = os.environ.get('WEB_CERT') or os.path.join(here, 'certs', 'control-panel.crt')
    key = os.environ.get('WEB_KEY') or os.path.join(here, 'certs', 'control-panel.key')
    want = '--https' in sys.argv or bool(os.environ.get('WEB_CERT'))
    if not want:
        return None
    if not (os.path.exists(cert) and os.path.exists(key)):
        sys.exit('No certificate found.\n  looked for: %s\n              %s\n'
                 'Create one with:  bash deploy/make_cert.sh' % (cert, key))
    return cert, key


def _pipe(a, b):
    """Shovel bytes one way until the source closes, then half-close the target so
    the other direction can still finish (a download in flight, for instance)."""
    try:
        while True:
            chunk = a.recv(32768)
            if not chunk:
                break
            b.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _serve_tls(listen_host, listen_port, upstream_port, cert, key):
    """Accept TLS on the public port and pipe each connection to plain HTTP on
    loopback. Blocks forever."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    # TLS 1.2 is the floor: everything since IE11 speaks it, and 1.0/1.1 are dead.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_host or '0.0.0.0', listen_port))
    srv.listen(128)

    def handle(raw, addr):
        inner = None
        try:
            tls = ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            # a browser that hung up mid-handshake, or plain http sent to the TLS
            # port - not worth a stack trace
            try:
                raw.close()
            except OSError:
                pass
            return
        try:
            inner = socket.create_connection(('127.0.0.1', upstream_port))
            t = threading.Thread(target=_pipe, args=(tls, inner), daemon=True)
            t.start()
            _pipe(inner, tls)
            t.join(timeout=30)
        except OSError:
            pass
        finally:
            for sck in (tls, inner):
                try:
                    if sck:
                        sck.close()
                except OSError:
                    pass

    while True:
        try:
            raw, addr = srv.accept()
        except OSError:
            continue
        threading.Thread(target=handle, args=(raw, addr), daemon=True).start()


def main():
    bind = _bind()
    host, _, port = bind.rpartition(':')
    workers = _workers()
    tls = _https_opts()

    from config.wsgi import application

    if tls:
        # The public port serves TLS; waitress moves to loopback behind it. Default
        # 9443 rather than 9080 so an http instance can keep running on the old port
        # while people move over.
        public = int(os.environ.get('WEB_HTTPS_PORT') or
                     (port if '--https' not in sys.argv else 9443))
        inner = int(os.environ.get('WEB_INNER_PORT') or (public + 1))
        cert, key = tls
        print(f'https://{host or "0.0.0.0"}:{public}   (TLS -> waitress on 127.0.0.1:{inner})')
        print(f'certificate: {cert}')
        # waitress runs in the background; the TLS front-end owns the main thread.
        from waitress import serve as wserve
        threading.Thread(
            target=wserve,
            kwargs=dict(app=application, host='127.0.0.1', port=inner,
                        threads=workers * 4, channel_timeout=120),
            daemon=True).start()
        _serve_tls(host or '0.0.0.0', public, inner, cert, key)
        return

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
