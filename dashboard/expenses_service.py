import calendar
import re
from datetime import date

from django.conf import settings

from dashboard.sap_connector import run_call, run_query

CATEGORIES = (
    'Salaries & HR',
    'Sales & Marketing',
    'Operations & Factory',
    'Admin & General',
    'Finance & Statutory',
)

CLASSIFICATION = {
    'SALARY EXPENSES': 'Salaries & HR',
    'BONUS EXPENSES': 'Salaries & HR',
    'INCENTIVE TO EMPLOYEES': 'Salaries & HR',
    'DIRECTOR REMUNERATION': 'Salaries & HR',
    'CASUAL LABOUR': 'Salaries & HR',
    'STAFF WELFARE': 'Salaries & HR',
    'SECURITY EXPENSES': 'Salaries & HR',
    'ADVERTISEMENT': 'Sales & Marketing',
    'BUSINESS PROMOTION': 'Sales & Marketing',
    'PROMOTIONAL DISCOUNT': 'Sales & Marketing',
    'MARKET COMISSION AND BROKERAGE': 'Sales & Marketing',
    'SAMPLING EXPENSES': 'Sales & Marketing',
    'EXHIBITION EXPENSES': 'Sales & Marketing',
    'STORAGE CHARGES INDIRECT': 'Sales & Marketing',
    'FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP': 'Sales & Marketing',
    'LOSS ON EXPIRED DAMAGED THEFT GOODS': 'Sales & Marketing',
    'ELECTRICITY': 'Operations & Factory',
    'FUEL - VEHICLES': 'Operations & Factory',
    'GENERATOR INDIRECT': 'Operations & Factory',
    'REPAIR AND MAINTENANCE PLANT & MACHINERY': 'Operations & Factory',
    'REPAIR & MAINTENANCE VEHICLE': 'Operations & Factory',
    'REPAIR & MAINTENANCE OFFICE & BUILDING': 'Operations & Factory',
    'AMC': 'Operations & Factory',
    'UNLOADING/LOADING CHARGES-INDIRECT EXPENSE': 'Operations & Factory',
    'FREIGHT INWARD-INDIRECT': 'Operations & Factory',
    'TOLL EXPENSE - VEHICLES': 'Operations & Factory',
    'LAB AND TESTING': 'Operations & Factory',
    'POLLUTION CONTROL': 'Operations & Factory',
    'CETP CHARGES': 'Operations & Factory',
    'COMPUTER AND HARDWARE': 'Operations & Factory',
    'SOFTWARE AND TECHNOLOGY': 'Operations & Factory',
    'RENT ON MACHINERY': 'Operations & Factory',
    'RENT': 'Admin & General',
    'INSURANCE INDIRECT': 'Admin & General',
    'FEES AND SUBSCRIPTION': 'Admin & General',
    'LEGAL AND PROFESSIONAL': 'Admin & General',
    'PRINTING AND STATIONERY': 'Admin & General',
    'POSTAGE & COURIER': 'Admin & General',
    'HOUSE KEEPING': 'Admin & General',
    'REFRESHMENT': 'Admin & General',
    'TELEPHONE MOBILE AND INTERNET': 'Admin & General',
    'BANK CHARGES': 'Admin & General',
    'CONVEYANCE': 'Admin & General',
    'NATIONAL TOUR AND TRAVELLING': 'Admin & General',
    'FOREIGN TOUR AND TRAVELLING': 'Admin & General',
    'TA AND DA': 'Admin & General',
    'FESTIVAL EXPENSE': 'Admin & General',
    'DONATION EXPENSE': 'Admin & General',
    'INTEREST ON BANK LOAN': 'Finance & Statutory',
    'INTEREST ON UNSECURED LOAN': 'Finance & Statutory',
    'INTEREST ON GST': 'Finance & Statutory',
    'PENALTY CHARGES': 'Finance & Statutory',
    'GST EXPENSE/INELIGIBLE CREDIT': 'Finance & Statutory',
    'INCOME TAX PREVIOUS PERIODS': 'Finance & Statutory',
}

CLASSIFICATION_ALIASES = {
    'SALARY EXPENSE': 'Salaries & HR',
    'ADVERTISEMNT': 'Sales & Marketing',
    'PROMOTIONAL DISCOUNT': 'Sales & Marketing',
    'PROMOTION AL DISCOUNT': 'Sales & Marketing',
}

BUDGET_CLASSIFICATION = {
    'SALES RE': 'Sales & Marketing',
    'DEL BKHP': 'Sales & Marketing',
    'SALES': 'Sales & Marketing',
    'MED MKT': 'Sales & Marketing',
    'OTE': 'Sales & Marketing',
    'FACTORY': 'Operations & Factory',
    'FACT COM': 'Operations & Factory',
    'NPD1': 'Operations & Factory',
    'NPD2': 'Operations & Factory',
    'TRANSPRT': 'Operations & Factory',
    'INTEREST': 'Finance & Statutory',
    'BACKOFF': 'Admin & General',
    'SAL CF': 'Salaries & HR',
}

SALARY_HINTS = (
    'SALARY', 'BONUS', 'INCENTIVE TO EMPLOYEES', 'DIRECTOR REMUNERATION',
    'CASUAL LABOUR', 'STAFF WELFARE', 'SECURITY EXPENSES', 'SAL CF',
)


def _norm(s):
    return re.sub(r'\s+', ' ', str(s or '').upper().strip().replace('&', 'AND').replace('-', ' ')).strip()


CLASSIFICATION_NORM = {
    _norm(key): value
    for source in (CLASSIFICATION, CLASSIFICATION_ALIASES)
    for key, value in source.items()
}

BUDGET_CLASSIFICATION_NORM = {_norm(key): value for key, value in BUDGET_CLASSIFICATION.items()}


def _to_float(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def format_compact_inr(n):
    n = _to_float(n)
    sign = '-' if n < 0 else ''
    n = abs(n)
    if n >= 1e7:
        return f'{sign}₹{n / 1e7:.1f} Cr'
    if n >= 1e5:
        return f'{sign}₹{n / 1e5:.1f} L'
    if n >= 1e3:
        return f'{sign}₹{n / 1e3:.0f}K'
    return f'{sign}₹{n:,.0f}'


def _is_salary_row(gl_norm, budget_norm):
    combined = f'{gl_norm} {budget_norm}'
    return any(hint in combined for hint in SALARY_HINTS)


def _schema():
    return settings.SAP_HANA.get('SCHEMA') or 'JIVO_OIL_HANADB'


def _month_bounds(month, year):
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start.isoformat(), end.isoformat()


def _first(row, *keys):
    for key in keys:
        if row.get(key) is not None:
            return row.get(key)
    return None


def _classify(gl_norm, budget_norm):
    if _is_salary_row(gl_norm, budget_norm):
        return 'Salaries & HR'
    return (
        CLASSIFICATION_NORM.get(gl_norm)
        or BUDGET_CLASSIFICATION_NORM.get(budget_norm)
        or 'Admin & General'
    )


def get_monthly_expenses(month, year):
    start_date, end_date = _month_bounds(month, year)
    schema = _schema()
    call_sql = f'CALL "{schema}"."REPORT _MONTHLY_EXPENSES"(?, ?)'
    try:
        raw = run_call(call_sql, [start_date, end_date])
        rows = []
        for r in raw:
            rows.append({
                'COST_CENTER': _first(r, 'Budget', 'BUDGET', 'SubBudget', 'SUBBUDGET'),
                'GL_ACCOUNT': _first(r, 'AcctName', 'ACCTNAME', 'Ledger', 'LEDGER'),
                'AMOUNT': _first(r, 'Amount', 'AMOUNT'),
            })
        if rows:
            return rows
    except Exception as e:
        print(f'get_monthly_expenses procedure error: {e}')

    try:
        sql = f"""
        SELECT COST_CENTER, GL_ACCOUNT, SUM(AMOUNT) AS AMOUNT
        FROM "{schema}"."MONTHLY_EXPENSES"
        WHERE MONTH(POSTING_DATE) = ? AND YEAR(POSTING_DATE) = ?
        GROUP BY COST_CENTER, GL_ACCOUNT
        """
        return run_query(sql, [month, year])
    except Exception as e:
        print(f'get_monthly_expenses fallback error: {e}')
        return []


def get_expenses_by_category(month, year):
    totals = {category: 0.0 for category in CATEGORIES}
    for row in get_monthly_expenses(month, year):
        gl_norm = _norm(row.get('GL_ACCOUNT'))
        budget_norm = _norm(row.get('COST_CENTER'))
        totals[_classify(gl_norm, budget_norm)] += _to_float(row.get('AMOUNT'))
    return totals


def get_expense_rows(from_date, to_date):
    schema = _schema()
    try:
        raw = run_call(f'CALL "{schema}"."REPORT _MONTHLY_EXPENSES"(?, ?)', [from_date, to_date])
    except Exception as e:
        print(f'get_expense_rows error: {e}')
        return []

    rows = []
    for r in raw:
        budget = str(r.get('Budget') or '').strip()
        account_name = str(r.get('AcctName') or '').strip()
        ledger = str(r.get('Ledger') or '').strip()
        gl_norm = _norm(account_name)
        budget_norm = _norm(budget)
        category = _classify(gl_norm, budget_norm)
        amount = _to_float(r.get('Amount'))
        doc_date = r.get('DocDate')
        if hasattr(doc_date, 'isoformat'):
            doc_date = doc_date.isoformat()[:10]
        rows.append({
            'branch': r.get('Branch'),
            'current_month': r.get('CURRENTMONTH'),
            'doc_date': doc_date,
            'trans_id': r.get('TransId'),
            'doc_num': r.get('DocNum'),
            'budget': budget,
            'sub_budget': r.get('SubBudget'),
            'variety': r.get('Variety'),
            'account_name': account_name,
            'ledger': ledger,
            'amount': amount,
            'amount_display': format_compact_inr(amount),
            'remarks': r.get('Remarks'),
            'owner_code': r.get('OwnerCode'),
            'current_month_budget': _to_float(r.get('Current_month_Budget')),
            'expense_type': 'Indirect' if 'INDIRECT' in _norm(f'{budget} {account_name} {ledger}') else 'Direct',
            'category': category,
            'is_salary': category == 'Salaries & HR',
        })
    return rows
