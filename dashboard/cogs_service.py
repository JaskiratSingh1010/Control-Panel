from django.conf import settings

from dashboard.sap_connector import run_query


PROC_NAME = 'REPORT_SALES_COGS_SUMMARY'


def _schema():
    return settings.SAP_HANA.get('SCHEMA') or 'JIVO_OIL_HANADB'


def _first(row, *keys):
    for key in keys:
        if row.get(key) is not None:
            return row.get(key)
    return None


def _to_float(value):
    try:
        if value is None or value == '':
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def get_cogs_data(from_date, to_date, param_type='Y', otp=''):
    param_type = (param_type if param_type is not None else 'Y').strip().upper()
    if param_type not in ('', 'A', 'Y'):
        param_type = ''

    otp_value = str(otp or '').strip()
    otp_param = int(otp_value) if otp_value.isdigit() else None

    rows = run_query(
        f'SELECT * FROM "{_schema()}"."{PROC_NAME}"(?, ?, ?, ?)',
        [from_date, to_date, param_type, otp_param],
    )
    row = rows[0] if rows else {}

    total_liter = _to_float(_first(row, 'TotalLiter', 'TOTALLITER', 'total_liter')) or 0.0
    cogs_per_liter = _to_float(_first(row, 'CogsPerLiter', 'COGSPERLITER', 'cogs_per_liter'))
    total_cogs = total_liter * cogs_per_liter if cogs_per_liter is not None else 0.0

    return {
        'total_liter': total_liter,
        'cogs_per_liter': cogs_per_liter,
        'total_cogs': total_cogs,
        'opt_missing': cogs_per_liter is None,
        'param_type': param_type,
        'otp_supplied': bool(otp_value),
    }


def get_cogs_current_month():
    return get_cogs_data('', '')


def update_cogs_opt(opt):
    return True, 'COGS OTP is entered from the KPI card.'
