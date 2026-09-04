"""SAP HANA access with a small connection pool.

Why a pool: opening a HANA connection costs 75-1100 ms, while the query itself
costs ~15 ms. The old code opened and closed a connection for every single
query, so a page that asked 18 questions paid the connect cost 18 times.

The pool keeps a few live connections in a bag and hands them out. Callers do
not change: get_connection() still returns something with .cursor() and
.close(), and .close() now just puts the connection back in the bag.

No SQL, no query and no calculation is changed by this module.
"""

import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager

from django.conf import settings

logger = logging.getLogger(__name__)

# How many idle connections to keep. Bursts above this are still served (a new
# connection is created), the extras are just closed instead of pooled.
DEFAULT_MAX_IDLE = 5

# A connection idle longer than this is checked with ping() before reuse.
# Below it we trust isconnected(), which is free (0-1 ms) instead of 9-18 ms.
DEFAULT_PING_AFTER = 30.0

# Drop a pooled connection older than this so we never sit on a stale session.
DEFAULT_MAX_AGE = 900.0


def _cfg(key, default):
    return settings.SAP_HANA.get(key, default)


def _raw_connect():
    from hdbcli import dbapi
    cfg = settings.SAP_HANA
    return dbapi.connect(
        address=cfg['HOST'],
        port=cfg['PORT'],
        user=cfg['USER'],
        password=cfg['PASSWORD'],
        timeout=5,
    )


def _hard_close(raw):
    try:
        raw.close()
    except Exception:
        pass


def _is_sql_error(exc):
    """True when the error is about the SQL, not about the connection.

    A typo in a query leaves the connection perfectly healthy, so it should go
    back in the pool. A dropped socket must not.
    """
    try:
        from hdbcli import dbapi
    except Exception:
        return False
    return isinstance(exc, (
        dbapi.ProgrammingError,
        dbapi.DataError,
        dbapi.IntegrityError,
        dbapi.NotSupportedError,
    ))


class _Entry:
    """One pooled connection plus the bookkeeping we need to trust it."""

    __slots__ = ('raw', 'born', 'idle_since', 'pid')

    def __init__(self, raw):
        now = time.monotonic()
        self.raw = raw
        self.born = now
        self.idle_since = now
        self.pid = os.getpid()


class _Pool:
    def __init__(self):
        self._free = deque()
        self._lock = threading.Lock()
        # Counters, for measuring the win. Read them with get_stats().
        self.created = 0
        self.reused = 0
        self.discarded = 0

    # -- helpers ----------------------------------------------------------
    def _usable(self, entry):
        """Is this pooled connection still safe to hand out?"""
        # Never reuse a connection inherited through a fork (gunicorn workers):
        # parent and child would share one socket and corrupt each other.
        if entry.pid != os.getpid():
            return False
        now = time.monotonic()
        if now - entry.born > _cfg('POOL_MAX_AGE', DEFAULT_MAX_AGE):
            return False
        try:
            if not entry.raw.isconnected():
                return False
            if now - entry.idle_since > _cfg('POOL_PING_AFTER', DEFAULT_PING_AFTER):
                entry.raw.ping()
        except Exception:
            return False
        return True

    # -- borrow / return --------------------------------------------------
    def acquire(self):
        while True:
            with self._lock:
                entry = self._free.pop() if self._free else None
            if entry is None:
                break
            if self._usable(entry):
                with self._lock:
                    self.reused += 1
                return entry
            _hard_close(entry.raw)
            with self._lock:
                self.discarded += 1

        entry = _Entry(_raw_connect())
        with self._lock:
            self.created += 1
        return entry

    def release(self, entry, broken=False):
        if entry is None:
            return
        if broken or entry.pid != os.getpid():
            _hard_close(entry.raw)
            with self._lock:
                self.discarded += 1
            return
        entry.idle_since = time.monotonic()
        with self._lock:
            if len(self._free) < _cfg('POOL_MAX_IDLE', DEFAULT_MAX_IDLE):
                self._free.append(entry)
                return
        _hard_close(entry.raw)

    def close_all(self):
        with self._lock:
            entries, self._free = list(self._free), deque()
        for entry in entries:
            _hard_close(entry.raw)

    def stats(self):
        with self._lock:
            return {
                'idle': len(self._free),
                'created': self.created,
                'reused': self.reused,
                'discarded': self.discarded,
            }


_pool = _Pool()


class PooledConnection:
    """Looks like an hdbcli connection; .close() returns it to the pool.

    Every existing caller does conn = get_connection() ... conn.close(), so
    wrapping keeps all of them working with no edit.
    """

    def __init__(self, entry, pool):
        self.__dict__['_entry'] = entry
        self.__dict__['_pool'] = pool
        self.__dict__['_released'] = False
        self.__dict__['_broken'] = False

    # The two methods callers actually use.
    def cursor(self):
        try:
            return self._entry.raw.cursor()
        except Exception:
            self.__dict__['_broken'] = True
            raise

    def close(self):
        if self._released:
            return
        self.__dict__['_released'] = True
        self._pool.release(self._entry, broken=self._broken)

    # Mark the connection unusable so close() throws it away instead of
    # putting a half-dead socket back in the bag.
    def mark_broken(self):
        self.__dict__['_broken'] = True

    # Anything else (commit, rollback, ping, ...) goes to the real connection.
    def __getattr__(self, name):
        entry = self.__dict__.get('_entry')
        if entry is None:
            raise AttributeError(name)
        return getattr(entry.raw, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and not _is_sql_error(exc):
            self.mark_broken()
        self.close()
        return False


def get_connection():
    """Borrow a connection from the pool. Call .close() to give it back."""
    return PooledConnection(_pool.acquire(), _pool)


@contextmanager
def connection():
    conn = get_connection()
    try:
        yield conn
    except Exception as exc:
        if not _is_sql_error(exc):
            conn.mark_broken()
        raise
    finally:
        conn.close()


def execute_query(sql, params=()):
    with connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
        finally:
            try:
                cursor.close()
            except Exception:
                pass
    return [dict(zip(columns, row)) for row in rows]


def call_procedure(schema, name, params=()):
    sql = f'CALL "{schema}"."{name}"({", ".join(["?"] * len(params))})'
    return execute_query(sql, params)


def health_check():
    try:
        rows = execute_query('SELECT 1 AS one FROM DUMMY')
        if rows and rows[0].get('one') == 1:
            return True, 'SAP Connected'
        return True, 'SAP Connected'
    except Exception as e:
        return False, str(e)


def get_stats():
    """Pool counters — handy for proving the speed-up. Read-only."""
    return _pool.stats()


def close_all():
    """Drop every idle pooled connection (used by tests and on shutdown)."""
    _pool.close_all()
