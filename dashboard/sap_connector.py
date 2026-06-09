from core.sap_connector import execute_query


def run_query(sql, params=()):
    return execute_query(sql, params)


def run_call(sql, params=()):
    return execute_query(sql, params)
