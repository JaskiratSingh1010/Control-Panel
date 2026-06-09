from contextlib import contextmanager
from django.conf import settings


def get_connection():
    from hdbcli import dbapi
    cfg = settings.SAP_HANA
    return dbapi.connect(
        address=cfg['HOST'],
        port=cfg['PORT'],
        user=cfg['USER'],
        password=cfg['PASSWORD'],
        timeout=5,
    )


@contextmanager
def connection():
    conn = get_connection()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def execute_query(sql, params=()):
    with connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        cursor.close()
    return [dict(zip(columns, row)) for row in rows]


def call_procedure(schema, name, params=()):
    sql = f'CALL "{schema}"."{name}"({", ".join(["?"] * len(params))})'
    with connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        cursor.close()
    return [dict(zip(columns, row)) for row in rows]


def health_check():
    try:
        rows = execute_query('SELECT 1 AS one FROM DUMMY')
        if rows and rows[0].get('one') == 1:
            return True, 'SAP Connected'
        return True, 'SAP Connected'
    except Exception as e:
        return False, str(e)
