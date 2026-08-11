import csv
import io
import json
import logging
import re
import time
from datetime import datetime

from django.http import JsonResponse, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.cache import never_cache

from core.decorators import group_required, permission_flag_required, any_permission_flag
from core import sap_connector
from core.simple_xlsx import build_workbook
from . import services

logger = logging.getLogger(__name__)

REALISE_GROUPS = ('realise_admin', 'realise_premium', 'realise_commodity')

# In-memory cache: stores the last fetched raw SAP rows per session is not enough;
# we use a module-level cache keyed by (start_date, end_date).
_raw_cache = {'key': None, 'rows': [], 'columns': []}

# channel_rows / channel_month_rows are a pure function of the (already cached) raw SAP
# rows, but were re-aggregated on every /api/sales-data/ hit — two full O(n) passes over
# tens of thousands of invoice lines (plus a strptime per row in the month pass) before the
# response could be sent. Memoize them by date range so repeat loads / Fetch clicks within
# the SAP cache window skip the rework. Same shape/TTL as services._SALES_CACHE.
_CHANNEL_AGG_CACHE = {}        # 'start|end' -> (expires_at, channel_rows, channel_month_rows)
_CHANNEL_AGG_TTL = 90          # seconds

EDIT_PIN = 'gill'


def _get_type_filter(request):
    from core.context_processors import derive_realise_profile
    _, type_filter, _ = derive_realise_profile(request.user)
    return type_filter


def _parse_body(request):
    try:
        return json.loads(request.body)
    except Exception:
        return {}


@never_cache
@group_required(*REALISE_GROUPS, json_response=False)
def dashboard(request):
    # never_cache: the territory_payload (person map + per-channel state whitelist) is
    # baked into the HTML at render time, so the browser must re-fetch the page after a
    # mapping change instead of serving a stale copy (else newly-assigned states like a
    # freshly-added ECOM/NAGALAND wouldn't appear until a hard refresh).
    return render(request, 'realise/dashboard.html', {
        'sidebar_active': 'realise',
        'territory_payload': json.dumps(services.get_territory_dashboard_payload()),
    })


@permission_flag_required('can_oih_vs_stock')
def oih_vs_stock(request):
    """Standalone tab: open-order litres (OIH) vs warehouse stock per product, with the
    Required (OIH − Stock) gap. Reuses the /api/oih-breakdown/ data (OIH rows + per-item
    on-hand stock across the three warehouses)."""
    return render(request, 'realise/oih_vs_stock.html', {'sidebar_active': 'oih_vs_stock'})


@permission_flag_required('can_compare_sales')
def compare_sales(request):
    """Standalone tab: month-wise sales pivot (rows = chosen dimension, columns = months)
    with a Main Group filter (compare groups for the same period) and a Compare selector
    (Litres / Realise / Both). Reuses /api/sales-data/ channel_month_rows."""
    return render(request, 'realise/compare_sales.html', {
        'sidebar_active': 'compare_sales',
        'territory_payload': json.dumps(services.get_territory_dashboard_payload()),
    })


@permission_flag_required('can_sales_cn')
def sales_cn(request):
    """Standalone tab: gross Sales vs Credit Notes. Rows = a chosen dimension (main group /
    state / sales person / product / item / customer); columns = Total Sales, Total CN (split
    into CN for Goods and Claim for Services) and Net Sales = Total Sales − Total CN. Filters:
    company (Oil / Beverages), Revenue vs quantity (Litres/Boxes), Premium/Commodity (oil),
    and a date range. Data via /realise/api/sales-cn/. The territory payload lets the client
    resolve the Contact Person dimension to the mapped territory owner (same as Compare Sales)."""
    return render(request, 'realise/sales_cn.html', {
        'sidebar_active': 'sales_cn',
        'territory_payload': json.dumps(services.get_territory_dashboard_payload()),
    })


@permission_flag_required('can_customer_aging')
def customer_aging(request):
    """Standalone tab: customer-receivables aging pivot (FORMAT → customers) with the five
    aging buckets, computed live from SAP (B1 reconciliation logic) as of a selectable
    date (?as_of=YYYY-MM-DD, default today)."""
    from datetime import date, datetime
    today = date.today()
    try:
        aging_date = datetime.strptime(request.GET.get('as_of', ''), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        aging_date = today
    if aging_date > today:                  # no aging into the future
        aging_date = today
    remark_idx = services.get_aging_remark_index()
    return render(request, 'realise/customer_aging.html', {
        'sidebar_active': 'customer_aging',
        # raw dict — the template's |json_script does the JSON serialization (passing a
        # pre-dumped string here would double-encode and JSON.parse would yield a string).
        'aging_payload': services.get_customer_aging(aging_date),
        'aging_remark_index': remark_idx['index'],      # {card_code: [remark tokens]}
        'aging_remark_options': remark_idx['options'],  # sorted master list for the filter
        'aging_date': aging_date.isoformat(),
        'aging_today': today.isoformat(),
    })


@permission_flag_required('can_customer_aging')
def customer_aging_detail(request):
    """Full-page per-document detail behind one customer's Balance Due, with an editable
    Remarks column and a Pivot/Unpivot toggle. ?code=<CardCode>&name=<CardName>&as_of=YYYY-MM-DD."""
    from datetime import date, datetime
    today = date.today()
    try:
        aging_date = datetime.strptime(request.GET.get('as_of', ''), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        aging_date = today
    if aging_date > today:
        aging_date = today
    code = (request.GET.get('code') or '').strip()
    name = (request.GET.get('name') or '').strip() or code
    # Company: 'oil' (default) uses SAP_SCHEMA; 'mart'/'bev' swap to their company schema and
    # prefix stored remarks ('MART:' / 'BEV:') so they never collide with oil's (or each other's)
    # for the same CardCode/TransId. Only oil runs the PREMIUM/COMMODITY item-type classify.
    company = (request.GET.get('company') or '').strip().lower()
    if company not in ('mart', 'bev'):
        company = 'oil'
    schema = {'mart': services.MART_SCHEMA, 'bev': services.BEVERAGES_SCHEMA}.get(company)
    rk_prefix = {'mart': 'MART:', 'bev': 'BEV:'}.get(company, '')
    return render(request, 'realise/customer_aging_detail.html', {
        'sidebar_active': 'customer_aging',
        'detail_payload': {'code': code, 'name': name, 'aging_date': aging_date.isoformat(),
                           'company': company,
                           'categories': services.AGING_REMARK_CATEGORIES,
                           'grace_days': services.get_aging_grace_days(code) if code else 0,
                           'rows': services.get_customer_aging_detail(
                               code, aging_date, schema=schema, row_key_prefix=rk_prefix,
                               classify=(company == 'oil')) if code else []},
        'aging_date': aging_date.isoformat(),
        'aging_today': today.isoformat(),
    })


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['GET'])
def api_customer_aging_beverages(request):
    """Raw open-invoice aging rows for the Jivo Beverages company, for the Beverages toggle on
    Customer Aging. The client pivots them (Sales Person → Customer) and offers the per-day
    multi-select + Excel-like raw drill. ?as_of=YYYY-MM-DD (default today)."""
    from datetime import date, datetime
    today = date.today()
    try:
        aging_date = datetime.strptime(request.GET.get('as_of', ''), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        aging_date = today
    if aging_date > today:
        aging_date = today
    return JsonResponse({'status': 'ok', **services.get_customer_aging_beverages(aging_date)})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['GET'])
def api_customer_aging_oil_ar(request):
    """Oil open-invoice RAW DATA rows (same shape as the Beverages endpoint) that back the oil
    RAW DATA workspace on Customer Aging. ?as_of=YYYY-MM-DD (default today)."""
    from datetime import date, datetime
    today = date.today()
    try:
        aging_date = datetime.strptime(request.GET.get('as_of', ''), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        aging_date = today
    if aging_date > today:
        aging_date = today
    return JsonResponse({'status': 'ok', **services.get_customer_aging_oil_ar(aging_date)})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['GET'])
def api_customer_aging_beverages_ar(request):
    """Beverages open-invoice RAW DATA rows (dispatch/bilty, Actual Sales Person + Remarks) that
    back the Beverages RAW DATA workspace on Customer Aging. Separate from the reconciliation aging
    pivot. ?as_of=YYYY-MM-DD (default today)."""
    from datetime import date, datetime
    today = date.today()
    try:
        aging_date = datetime.strptime(request.GET.get('as_of', ''), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        aging_date = today
    if aging_date > today:
        aging_date = today
    return JsonResponse({'status': 'ok', **services.get_customer_aging_beverages_ar(aging_date)})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['GET'])
def api_customer_aging_mart(request):
    """Raw open-invoice aging rows for the Jivo Mart company, for the Mart toggle on Customer
    Aging. Same shape/behaviour as the Beverages endpoint. ?as_of=YYYY-MM-DD (default today)."""
    from datetime import date, datetime
    today = date.today()
    try:
        aging_date = datetime.strptime(request.GET.get('as_of', ''), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        aging_date = today
    if aging_date > today:
        aging_date = today
    return JsonResponse({'status': 'ok', **services.get_customer_aging_mart(aging_date)})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_remark(request):
    """Save (or clear) one per-document remark on the Customer Aging detail page."""
    body = _parse_body(request)
    code = body.get('code', '')
    row_key = body.get('row_key', '')
    if not code or not row_key:
        return JsonResponse({'status': 'error', 'error': 'code and row_key required'}, status=400)
    services.save_aging_remark(code, row_key, body.get('remark', ''))
    return JsonResponse({'status': 'ok'})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_due_days(request):
    """Set (or clear) the NOT DUE grace period for one customer on the Customer Aging detail
    page. Body: {code:<CardCode>, days:<int>}. days<=0 turns the auto NOT DUE/OVERDUE off."""
    body = _parse_body(request)
    code = (body.get('code') or '').strip()
    if not code:
        return JsonResponse({'status': 'error', 'error': 'code required'}, status=400)
    days = services.save_aging_grace_days(code, body.get('days'), user=request.user)
    return JsonResponse({'status': 'ok', 'days': days})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_remark_lines(request):
    """Replace the split breakdown (TDS / RTV / Claim / …) behind one open document on the
    Customer Aging detail page. Body: {code, row_key, lines:[{category,amount,remark}, ...]}."""
    body = _parse_body(request)
    code = body.get('code', '')
    row_key = body.get('row_key', '')
    if not code or not row_key:
        return JsonResponse({'status': 'error', 'error': 'code and row_key required'}, status=400)
    lines = body.get('lines', [])
    if not isinstance(lines, list):
        return JsonResponse({'status': 'error', 'error': 'lines must be a list'}, status=400)
    services.save_aging_remark_lines(code, row_key, lines)
    return JsonResponse({'status': 'ok'})


def _norm_docno(v):
    """A cell value → a clean Doc No string (Excel often reads doc numbers as floats)."""
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    if s.endswith('.0') and s[:-2].isdigit():
        s = s[:-2]
    return s


def _parse_amount(v):
    """A cell → float; tolerates ₹, thousands commas and blanks (else 0.0)."""
    s = str('' if v is None else v).replace('₹', '').replace(',', '').strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _extract_doc_entries(table):
    """A sheet (list of rows) → {doc_no: {'remark': str|None, 'splits': [{category, amount,
    remark}, ...]}}, or None if it has no header row with a Doc-No-like column plus a Remark or a
    Category column. The Doc column matches any header containing 'doc' (not a date); 'remark' any
    'remark' header, 'category' any 'categ' header, 'amount' an exact 'amount'/'amt' (or a contains
    match that isn't a document-total column like Original/Balance/Total Amount).

    Each document's Remark/Category value becomes BOTH the note AND a split whose category is that
    value — so uploading a 'Remark / Category' column fills the note and the on-screen Category
    together. A split's amount is taken from an explicit Amount column when present, else left None
    = 'allocate the document's full Balance Due' (resolved in bulk_update_aging_remarks). Document-
    total columns (Original/Balance/Total Amount) are NOT used as the split amount, so an exported
    aging sheet re-uploaded with categories still allocates the balance, not the original.

    An optional 'Actual Sales Person' column (Beverages raw drill) is captured per doc as
    entry['actual_sp'] when present, so a remark upload can also re-assign the actual sales
    person; if the column is absent it is simply skipped (no actual_sp key set)."""
    doc_i = rem_i = cat_i = amt_i = asp_i = header_idx = None
    for idx, row in enumerate(table):
        cols = [str(c or '').strip().lower() for c in row]
        d = next((j for j, c in enumerate(cols) if 'doc' in c and 'date' not in c), None)
        if d is None:
            continue
        r = next((j for j, c in enumerate(cols) if 'remark' in c), None)
        cat = next((j for j, c in enumerate(cols) if 'categ' in c), None)
        # Optional 'Actual Sales Person' override column — matched on 'actual' + sales/sp/person.
        asp = next((j for j, c in enumerate(cols)
                    if 'actual' in c and ('sales' in c or 'sp' in c or 'person' in c)), None)
        # Amount: prefer an exact split-amount header; else a contains-match that is NOT a
        # document-total column (Original/Balance/Total/Gross Amount) — so an EXPORTED aging
        # sheet (which carries Original/Balance amounts) is never mistaken for a split sheet.
        amt = next((j for j, c in enumerate(cols)
                    if c in ('amount', 'amt', 'amount (₹)', 'amount(₹)', 'split amount', 'amount to allocate')), None)
        if amt is None:
            amt = next((j for j, c in enumerate(cols) if 'amount' in c
                        and not any(w in c for w in ('original', 'balance', 'total', 'gross'))), None)
        # A real header row = a Doc column plus a Remark or a Category column. An Amount column
        # ALONE does not make it a header (exported sheets carry Original/Balance amounts).
        if r is not None or cat is not None:
            doc_i, rem_i, cat_i, amt_i, asp_i, header_idx = d, r, cat, amt, asp, idx
            break
    if header_idx is None:
        return None
    # The note (AgingRemark) comes from the Remarks column, falling back to Category; the split
    # CATEGORY comes from the Category column, falling back to Remarks. So a single "Remark /
    # Category" column drives BOTH the note and the on-screen Category. With no explicit Amount
    # column a split's amount is left None = "allocate the document's full Balance Due".
    note_i = rem_i if rem_i is not None else cat_i
    cat_src = cat_i if cat_i is not None else rem_i

    def cell(row, i):
        return '' if i is None or i >= len(row) or row[i] is None else str(row[i]).strip()

    out = {}
    for row in table[header_idx + 1:]:
        doc = _norm_docno(row[doc_i]) if doc_i < len(row) else ''
        if not doc:
            continue
        entry = out.setdefault(doc, {'remark': None, 'splits': []})
        note = cell(row, note_i)
        catv = cell(row, cat_src)
        asp = cell(row, asp_i)                            # optional Actual Sales Person override
        if note:
            entry['remark'] = note
        if asp:
            entry['actual_sp'] = asp
        if catv:                                          # the value → a split (its category label)
            amt = None if amt_i is None else _parse_amount(row[amt_i] if amt_i < len(row) else '')
            entry['splits'].append({'category': catv, 'amount': amt, 'remark': ''})
    return out


def _parse_remark_upload(uploaded):
    """Read an uploaded .xlsx/.csv into {doc_no: {'remark', 'splits'}} (see _extract_doc_entries).
    Scans EVERY sheet (a 'Data' sheet first) for the one carrying a Doc No column alongside
    Remarks and/or Category+Amount columns — so both our own export and hand-kept books (data on a
    later sheet next to a pivot/summary) work. Raises ValueError if no sheet matches."""
    raw = uploaded.read()
    name = (getattr(uploaded, 'name', '') or '').lower()
    tables = []
    if name.endswith('.csv'):
        text = raw.decode('utf-8-sig', errors='replace')
        tables = [list(csv.reader(io.StringIO(text)))]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        order = sorted(wb.sheetnames, key=lambda n: n.strip().lower() != 'data')   # 'Data' first
        tables = [[list(r) for r in wb[sn].iter_rows(values_only=True)] for sn in order]
    for table in tables:
        parsed = _extract_doc_entries(table)
        if parsed is not None:
            return parsed
    raise ValueError('Could not find a "Doc No" column with "Remarks" and/or "Category" + "Amount" columns')


# ── Claims bulk upload (same columns as the Claims Excel export) ──────────────────────────────
_CLAIM_HEADER_ALIASES = {
    'claim_date':      ('receiving date', 'claim date', 'date'),
    'party_name':      ('party name', 'party', 'customer', 'customer name'),
    'claim_pass_date': ('claim pass date', 'pass date'),
    'claim_month':     ('claim month year', 'claim month and year', 'claim month', 'month year', 'month'),
    'claim_type':      ('claim type', 'type'),
    'ref_inv_no':      ('ref inv no', 'ref inv', 'ref invoice no', 'reference inv no', 'ref no'),
    'coop_no':         ('coop no', 'coop', 'co op no'),
    'claim_hold':      ('claim hold', 'hold status', 'on hold'),
    'claim_amount':    ('claim amount', 'amount'),
    'claim_passed':    ('claim passed manual', 'claim passed', 'passed'),
    'reason_of_hold':  ('reason of hold manual', 'reason of hold', 'reason'),
    'main_group':      ('main group', 'group', 'channel'),
}
_CLAIM_HEADER_TO_FIELD = {alias: field for field, aliases in _CLAIM_HEADER_ALIASES.items() for alias in aliases}


def _norm_claim_header(h):
    return re.sub(r'[^a-z0-9]+', ' ', str(h if h is not None else '').strip().lower()).strip()


def _extract_claim_rows(table):
    """A sheet (list of rows) → list of {field: raw_value} claim records, or None if it has no
    header row exposing at least a Receiving Date + Party Name column. Headers are matched
    case-insensitively on the export's column names; blank / TOTAL rows are dropped."""
    if not table:
        return None
    header_idx, colmap = None, {}
    for i, row in enumerate(table[:20]):
        m = {}
        for j, cell in enumerate(row or []):
            field = _CLAIM_HEADER_TO_FIELD.get(_norm_claim_header(cell))
            if field and field not in m:
                m[field] = j
        if 'claim_date' in m and 'party_name' in m:
            header_idx, colmap = i, m
            break
    if header_idx is None:
        return None
    out = []
    for row in table[header_idx + 1:]:
        if not row:
            continue
        rec = {field: (row[j] if j < len(row) else None) for field, j in colmap.items()}
        pname = str(rec.get('party_name') or '').strip()
        if not pname or pname.upper() == 'TOTAL':
            continue
        out.append(rec)
    return out


def _parse_claim_upload(uploaded):
    """Read an uploaded .xlsx/.csv (Claims export format) into a list of {field: raw_value} records.
    Scans every sheet for the one carrying the claim columns. Raises ValueError if none match."""
    raw = uploaded.read()
    name = (getattr(uploaded, 'name', '') or '').lower()
    if name.endswith('.csv'):
        text = raw.decode('utf-8-sig', errors='replace')
        tables = [list(csv.reader(io.StringIO(text)))]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        tables = [[list(r) for r in wb[sn].iter_rows(values_only=True)] for sn in wb.sheetnames]
    for table in tables:
        rows = _extract_claim_rows(table)
        if rows is not None:
            return rows
    raise ValueError('Could not find the claim columns — the file needs a "Receiving Date" and a "Party Name" column (export a claim file to see the format).')


@permission_flag_required('can_claims', json_response=True)
@require_http_methods(['POST'])
def api_claim_upload(request):
    """Bulk-create claims from an uploaded .xlsx/.csv in the Claims export format.
    multipart: file=<xlsx/csv>. Each data row becomes a NEW claim (needs a valid Receiving Date +
    Party Name); it adds rows and does not update existing claims."""
    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'status': 'error', 'error': 'file is required'}, status=400)
    try:
        rows = _parse_claim_upload(upload)
    except Exception as exc:
        return JsonResponse({'status': 'error', 'error': str(exc)}, status=400)
    result = services.bulk_upload_claims(rows, user=request.user)
    return JsonResponse({'status': 'ok', **result})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_remark_upload(request):
    """Bulk-update per-document Remarks from an uploaded .xlsx/.csv, matching on Doc No.
    multipart: file=<xlsx/csv>, code=<CardCode>, as_of=YYYY-MM-DD (the aging date the sheet
    was exported for). Updates every open line sharing a Doc No; blanks are left unchanged."""
    code = (request.POST.get('code') or '').strip()
    upload = request.FILES.get('file')
    if not code or not upload:
        return JsonResponse({'status': 'error', 'error': 'code and file are required'}, status=400)
    try:
        doc_remarks = _parse_remark_upload(upload)
    except Exception as exc:
        return JsonResponse({'status': 'error', 'error': str(exc)}, status=400)
    result = services.bulk_update_aging_remarks(code, _parse_as_of(request.POST.get('as_of')), doc_remarks)
    return JsonResponse({'status': 'ok', **result})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_remark_upload_beverages(request):
    """Bulk-update Beverages open-invoice Remarks from an uploaded .xlsx/.csv, matching on Doc No
    (no customer needed — the code for each Doc No is resolved from the beverages aging rows).
    multipart: file=<xlsx/csv>, as_of=YYYY-MM-DD."""
    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'status': 'error', 'error': 'file is required'}, status=400)
    try:
        doc_remarks = _parse_remark_upload(upload)
    except Exception as exc:
        return JsonResponse({'status': 'error', 'error': str(exc)}, status=400)
    result = services.bulk_update_beverages_remarks(_parse_as_of(request.POST.get('as_of')), doc_remarks)
    status = 'error' if result.get('error') else 'ok'
    return JsonResponse({'status': status, **result})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_remark_upload_oil(request):
    """Bulk-update Oil RAW DATA open-invoice Remarks from an uploaded .xlsx/.csv, matched on Doc No
    (the code for each Doc No is resolved from the oil raw-invoice rows). multipart: file, as_of."""
    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'status': 'error', 'error': 'file is required'}, status=400)
    try:
        doc_remarks = _parse_remark_upload(upload)
    except Exception as exc:
        return JsonResponse({'status': 'error', 'error': str(exc)}, status=400)
    result = services.bulk_update_oil_ar_remarks(_parse_as_of(request.POST.get('as_of')), doc_remarks)
    status = 'error' if result.get('error') else 'ok'
    return JsonResponse({'status': status, **result})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_remark_clear(request):
    """Clear all saved per-document Remarks for one customer (split breakdowns are kept).
    Body: {code:<CardCode>}."""
    body = _parse_body(request)
    code = (body.get('code') or '').strip()
    if not code:
        return JsonResponse({'status': 'error', 'error': 'code required'}, status=400)
    return JsonResponse({'status': 'ok', 'cleared': services.clear_aging_remarks(code)})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_remark_clear_beverages(request):
    """Wipe Beverages RAW DATA per-invoice overrides company-wide, so stale Remarks / Actual Sales
    Person values from an earlier upload can be erased before re-uploading. Body:
    {what: 'remarks'|'sp'|'both'} (default 'both')."""
    what = str(_parse_body(request).get('what') or 'both').strip().lower()
    return JsonResponse({'status': 'ok', 'cleared': services.clear_ar_company_overrides('bev', what),
                         'what': what})


@permission_flag_required('can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_aging_remark_clear_oil(request):
    """Wipe Oil RAW DATA per-invoice overrides company-wide. Body: {what: 'remarks'|'sp'|'both'}
    (default 'both')."""
    what = str(_parse_body(request).get('what') or 'both').strip().lower()
    return JsonResponse({'status': 'ok', 'cleared': services.clear_ar_company_overrides('oil', what),
                         'what': what})


@any_permission_flag('can_realise', 'can_customer_aging', 'can_oih_vs_stock', 'can_compare_sales',
                     'can_claims', 'can_sales_cn', 'can_hidden_sales', 'can_sales_flow',
                     'can_open_payments',
                     json_response=True)
@require_http_methods(['POST'])
def api_export_xlsx(request):
    """Build a multi-sheet .xlsx from client-supplied sheets and stream it back.
    Body: {filename, sheets:[{name, rows:[[cell, ...], ...]}, ...]} where each cell is a
    scalar (numbers become real numeric cells) or {value, style, colspan}. Generic — powers
    the Customer Aging detail 'Export Excel' (Pivot + Data sheets in one file)."""
    body = _parse_body(request)
    sheets_in = body.get('sheets') or []
    if not isinstance(sheets_in, list) or not sheets_in:
        return JsonResponse({'error': 'sheets required'}, status=400)
    sheets = []
    for s in sheets_in:
        if not isinstance(s, dict):
            continue
        rows = s.get('rows')
        if isinstance(rows, list) and rows:
            sheets.append((str(s.get('name') or 'Sheet'), rows))
    if not sheets:
        return JsonResponse({'error': 'no rows to export'}, status=400)
    content = build_workbook(sheets)
    filename = re.sub(r'[^A-Za-z0-9._ -]', '_', str(body.get('filename') or 'export'))[:120]
    if not filename.lower().endswith('.xlsx'):
        filename += '.xlsx'
    response = HttpResponse(
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _parse_as_of(s):
    """Parse a ?as_of=YYYY-MM-DD query param into a date, or None (→ today) if absent/bad."""
    try:
        return datetime.strptime((s or '').strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


@any_permission_flag('can_realise', 'can_customer_aging', json_response=True)
@require_http_methods(['POST'])
def api_export_aging_detail(request):
    """Whole-book Customer Aging detail export: every open document — with its per-document
    Remark and split breakdown — for the parties the client passes (the currently-filtered
    set), in one sheet. All detail is fetched in a single bulk SAP query."""
    body = _parse_body(request)
    parties = body.get('parties') or []
    if not isinstance(parties, list) or not parties:
        return JsonResponse({'error': 'parties required'}, status=400)
    codes, meta = [], {}
    for p in parties:
        if not isinstance(p, dict):
            continue
        code = str(p.get('code') or '').strip()
        if not code or code in meta:
            continue
        meta[code] = (str(p.get('name') or code).strip(), str(p.get('format') or '').strip())
        codes.append(code)
    if not codes:
        return JsonResponse({'error': 'no valid parties'}, status=400)

    as_of = _parse_as_of(body.get('as_of'))
    detail = services.get_customer_aging_detail_bulk(codes, as_of)

    def famt(a):
        try:
            return '{:,.0f}'.format(float(a or 0))
        except (TypeError, ValueError):
            return '0'

    hdr = {'bold': True, 'fill': '0F172A', 'color': 'FFFFFF'}
    headers = ['Format', 'Customer', 'Doc No', 'Type', 'Posting Date', 'Due Date', 'Branch',
               'Original', 'Balance Due', '0-30', '31-60', '61-90', '91-120', '121+', 'Remark', 'Splits']
    rows = [[{'value': h, **hdr} for h in headers]]
    tot = {k: 0.0 for k in ('original', 'balance_due', 'b0_30', 'b31_60', 'b61_90', 'b91_120', 'b121')}
    for code in codes:
        name, fmt = meta[code]
        for d in detail.get(code, []):
            splits = '; '.join(
                (('%s: %s' % (s.get('category') or '?', famt(s.get('amount'))))
                 + ((' (%s)' % s['remark']) if s.get('remark') else ''))
                for s in (d.get('splits') or []))
            rows.append([
                fmt, name, d['doc_no'], d['type'], d['posting_date'], d['due_date'], d['branch'],
                d['original'], d['balance_due'], d['b0_30'], d['b31_60'], d['b61_90'],
                d['b91_120'], d['b121'], d['remark'], splits,
            ])
            for k in tot:
                tot[k] += d.get(k, 0) or 0
    if len(rows) == 1:
        return JsonResponse({'error': 'No open documents for the selected parties'}, status=400)

    def tcell(v):
        return {'value': round(v, 2), 'bold': True, 'fill': 'E2E8F0'}
    rows.append([{'value': 'TOTAL', 'bold': True, 'fill': 'E2E8F0'}] + ['' ] * 6
                + [tcell(tot['original']), tcell(tot['balance_due']), tcell(tot['b0_30']),
                   tcell(tot['b31_60']), tcell(tot['b61_90']), tcell(tot['b91_120']), tcell(tot['b121'])]
                + ['', ''])

    content = build_workbook([('Aging Detail', rows)])
    fname = 'Customer Aging Detail %s.xlsx' % (as_of or datetime.now().date()).strftime('%d.%m.%Y')
    resp = HttpResponse(
        content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="%s"' % fname
    return resp


@permission_flag_required('can_required_credit_limit')
def required_credit_limit(request):
    """Standalone tab: Required Credit Limit — live Order-in-Hand grouped by ASM
    (territory owner) → party, in the closing-sheet layout. Each party row shows open
    litres + open value (₹), a Premium/Commodity filter at the top, and a frontend-
    editable delivery remark (the only writable column; persisted to ClosingRemark).
    ?as_of=YYYY-MM-DD (default today) drives the Ledger Amt / Payment Done date view."""
    return render(request, 'realise/required_credit_limit.html', {
        'sidebar_active': 'required_credit_limit',
        # raw dict — the template's |json_script does the JSON serialization.
        'credit_payload': services.get_required_credit_rows(as_of_date=_parse_as_of(request.GET.get('as_of'))),
    })


@permission_flag_required('can_required_credit_limit', json_response=True)
@require_http_methods(['POST'])
def api_save_closing_remark(request):
    """Save the editable delivery remark for one party on the Required Credit Limit tab."""
    body = _parse_body(request)
    card_code = body.get('card_code', '')
    if not card_code:
        return JsonResponse({'status': 'error', 'error': 'card_code required'}, status=400)
    services.save_closing_remark(card_code, body.get('remark', ''), request.user)
    return JsonResponse({'status': 'ok'})


@permission_flag_required('can_required_credit_limit', json_response=True)
@require_http_methods(['POST'])
def api_credit_lock(request):
    """Freeze Total Outstanding + Required Limit at their current values for the chosen
    number of days. Snapshots every party row; returns the new lock state."""
    body = _parse_body(request)
    lock = services.create_credit_lock(body.get('days', 30), request.user)
    return JsonResponse({'status': 'ok', 'lock': lock})


@permission_flag_required('can_required_credit_limit', json_response=True)
@require_http_methods(['POST'])
def api_credit_unlock(request):
    """Lift the active lock early — the columns revert to live SAP immediately."""
    services.clear_credit_lock()
    return JsonResponse({'status': 'ok', 'lock': None})


@permission_flag_required('can_required_credit_limit')
@require_http_methods(['GET'])
def export_required_credit(request):
    """Download the Required Credit Limit data as an .xlsx in the CLOSING SHEET layout.
    ?type=P|C|P+C (repeatable) scopes to the on-screen Type filter; ?asm=<name> to one ASM."""
    type_filters = [t.strip() for t in request.GET.getlist('type') if t.strip() in ('P', 'C', 'P+C')]
    asms = [a.strip() for a in request.GET.getlist('asm') if a.strip()]
    payload = services.get_required_credit_rows(as_of_date=_parse_as_of(request.GET.get('as_of')))
    if asms:                     # scope the export to the on-screen ASM selection
        chosen = set(asms)
        payload = {'asms': [g for g in payload.get('asms', []) if g.get('asm') in chosen],
                   'total': payload.get('total', {})}
    content = services.build_closing_sheet_xlsx(payload, type_filters)
    parts = []
    if asms:
        parts.append(asms[0] if len(asms) == 1 else f'{len(asms)} ASMs')
    type_names = {'P': 'Premium', 'C': 'Commodity', 'P+C': 'Prem+Comm'}
    parts.append('+'.join(type_names[t] for t in type_filters) if type_filters else 'All')
    response = HttpResponse(
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="CLOSING SHEET ({" - ".join(parts)}).xlsx"'
    return response


def _aggregate_channel_rows(raw_rows):
    """Collapse raw SAP transaction rows to distinct
    (type, sub_group, main_group, state, sales_person, card_name, item_name) buckets
    with summed litres/revenue. Slide 2 only ever SUMS these dimensions, so this is
    lossless for every card / drill / commodity aggregation while still collapsing
    the many invoice LINES per customer-order into one bucket. card_name powers the
    Customer drill and item_name the Item Name drill in the channel detail modal."""
    agg = {}
    for row in raw_rows:
        sales_person = ''
        for k in ('U_SALES_PERSON', 'U_Sales_Person', 'SALES_PERSON', 'SalesPerson', 'SlpName'):
            v = str(row.get(k, '') or '').strip().upper()
            if v:
                sales_person = v
                break
        u_type = str(row.get('U_TYPE', '') or '').strip().upper()
        u_sub = str(row.get('U_Sub_Group', '') or '').strip().upper()
        u_main = str(row.get('U_Main_Group', '') or '').strip().upper()
        state = str(row.get('State', '') or '').strip().upper()
        card_name = str(row.get('CardName', '') or '').strip().upper()
        item_name = services._item_label(str(row.get('ItemCode', '') or '').strip().upper(),
                                          str(row.get('ItemName', '') or '').strip().upper())
        # SKU = OITM.U_SKU (pack size, e.g. '1 LTR' / '500 MLS'), returned by the proc as
        # "SKU". Carried per row so the Item-first drill can filter by SKU / Product.
        sku = str(row.get('SKU', '') or '').strip().upper()
        key = (u_type, u_sub, u_main, state, sales_person, card_name, item_name, sku)
        bucket = agg.get(key)
        if bucket is None:
            bucket = agg[key] = {
                'u_type': u_type, 'u_sub_group': u_sub, 'u_main_group': u_main,
                'state': state, 'sales_person': sales_person, 'card_name': card_name,
                'item_name': item_name, 'sku': sku,
                'liter': 0.0, 'line_total': 0.0,
            }
        bucket['liter'] += float(row.get('Liter', 0) or 0)
        bucket['line_total'] += float(row.get('LineTotal', 0) or 0)
    out = list(agg.values())
    for b in out:
        b['liter'] = round(b['liter'], 2)
        b['line_total'] = round(b['line_total'], 2)
    return out


def _aggregate_channel_month_rows(raw_rows):
    """Month-level channel buckets for the OILS month-wise pivot: distinct
    (type, main_group, state, sales_person, sub_group, item_name, card_name, ym) with
    summed litres. Carries every dimension the pivot's Drill By offers (State / Contact
    Person / Product / Item Name / Customer) plus the month, so the States x Months pivot
    can re-pivot its rows by any of them while months stay in the columns. `ym` is a
    sortable 'YYYY-MM'; `mlabel` is the display label ('JUL 2025')."""
    agg = {}
    for row in raw_rows:
        mon, year = services._parse_doc_date(row.get('DocDate', ''))
        if not mon or not year:
            continue
        try:
            mnum = datetime.strptime(mon, '%b').month
        except ValueError:
            continue
        sales_person = ''
        for k in ('U_SALES_PERSON', 'U_Sales_Person', 'SALES_PERSON', 'SalesPerson', 'SlpName'):
            v = str(row.get(k, '') or '').strip().upper()
            if v:
                sales_person = v
                break
        u_type = str(row.get('U_TYPE', '') or '').strip().upper()
        u_main = str(row.get('U_Main_Group', '') or '').strip().upper()
        u_sub = str(row.get('U_Sub_Group', '') or '').strip().upper()
        state = str(row.get('State', '') or '').strip().upper()
        item_name = services._item_label(str(row.get('ItemCode', '') or '').strip().upper(),
                                          str(row.get('ItemName', '') or '').strip().upper())
        card_name = str(row.get('CardName', '') or '').strip().upper()
        sku = str(row.get('SKU', '') or '').strip().upper()   # OITM.U_SKU pack size
        variety = str(row.get('U_Variety', '') or '').strip().upper()   # OITM.U_Variety (KACCHI GHANI / REFINED / …)
        # Finished-goods flag so the Compare Sales pivot can drop packaging (PM…) / raw
        # material (RM…) lines and keep only FG items. FG ItemCodes map 1:1 to the
        # 'FINISHED' item group; accept either signal.
        item_code = str(row.get('ItemCode', '') or '').strip().upper()
        grp_name = str(row.get('ItmsGrpNam', '') or '').strip().upper()
        is_fg = (grp_name == 'FINISHED') or item_code.startswith('FG')
        ym = '%s-%02d' % (year, mnum)
        key = (u_type, u_main, state, sales_person, u_sub, item_name, card_name, ym, sku, variety)
        bucket = agg.get(key)
        if bucket is None:
            bucket = agg[key] = {
                'u_type': u_type, 'main_group': u_main, 'state': state,
                'sales_person': sales_person, 'u_sub_group': u_sub, 'item_name': item_name,
                'card_name': card_name, 'sku': sku, 'u_variety': variety, 'is_fg': is_fg,
                'ym': ym, 'mlabel': '%s %s' % (mon, year),
                'liter': 0.0, 'line_total': 0.0, 'pcs': 0.0,
            }
        bucket['liter'] += float(row.get('Liter', 0) or 0)
        bucket['line_total'] += float(row.get('LineTotal', 0) or 0)
        bucket['pcs'] += float(row.get('Quantity', 0) or 0)   # selling units (bottles/cans) → Pieces / Rate view
    out = list(agg.values())
    for b in out:
        b['liter'] = round(b['liter'], 2)
        b['line_total'] = round(b['line_total'], 2)
        b['pcs'] = round(b['pcs'], 2)
    return out


def _channel_aggregates(start_date, end_date, raw_rows):
    """Memoized (channel_rows, channel_month_rows) for a date range. Only recomputes when
    the SAP cache window has rolled over; otherwise returns the prior aggregation so a
    cached sales-data load doesn't re-walk the full raw set on every request."""
    key = f'{start_date}|{end_date}'
    now = time.time()
    hit = _CHANNEL_AGG_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    channel_rows = _aggregate_channel_rows(raw_rows)
    channel_month_rows = _aggregate_channel_month_rows(raw_rows)
    if raw_rows:                          # only cache successful, non-empty pulls
        _CHANNEL_AGG_CACHE[key] = (now + _CHANNEL_AGG_TTL, channel_rows, channel_month_rows)
        for k in [k for k, v in _CHANNEL_AGG_CACHE.items() if v[0] <= now]:
            _CHANNEL_AGG_CACHE.pop(k, None)
    return channel_rows, channel_month_rows


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_health(request):
    ok, message = sap_connector.health_check()
    from core.context_processors import derive_realise_profile
    role, _, _ = derive_realise_profile(request.user)
    return JsonResponse({
        'sap_connected': ok,
        'message': message,
        'username': request.user.username,
        'role': role,
    })


@any_permission_flag('can_realise', 'can_compare_sales', json_response=True)
@require_http_methods(['POST'])
def api_sales_data(request):
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date   = body.get('end_date', '')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)

    type_filter = _get_type_filter(request)
    force = bool(body.get('refresh') or body.get('force'))   # "Refresh from SAP" → fresh pull

    try:
        result, raw_rows = services.get_sales_data_cached(start_date, end_date, force=force)
    except Exception as e:
        logger.error('[REALISE] get_sales_data error: %s', e)
        return JsonResponse({'status': 'ok', 'data': [], 'count': 0})

    # Cache raw rows for drill-down and CSV export
    _raw_cache['key']     = f'{start_date}_{end_date}'
    _raw_cache['rows']    = raw_rows
    _raw_cache['columns'] = list(raw_rows[0].keys()) if raw_rows else []
    _raw_cache['start']   = start_date
    _raw_cache['end']     = end_date

    # Attach targets to each product row
    rows = result['products']
    month_year_pairs = set()
    for r in rows:
        if r.get('month') and r.get('year'):
            try:
                m = datetime.strptime(r['month'], '%b').month
                y = int(r['year'])
                month_year_pairs.add((m, y))
            except (ValueError, KeyError):
                pass

    # Build targets lookup for all month/year pairs found
    targets_cache = {}
    for (m, y) in month_year_pairs:
        targets_cache[(m, y)] = services.get_targets_for_month(m, y)

    output = []
    for r in rows:
        if type_filter and r['u_type'] != type_filter:
            continue

        tgt_ltrs = 0
        tgt_rate  = 0
        if r.get('month') and r.get('year'):
            try:
                m = datetime.strptime(r['month'], '%b').month
                y = int(r['year'])
                key = f"{r['u_type']}|{r['u_sub_group']}"
                td = targets_cache.get((m, y), {}).get(key, {})
                tgt_ltrs = td.get('tgt_ltrs', 0)
                tgt_rate  = td.get('tgt_rate', 0)
            except (ValueError, KeyError):
                pass

        output.append({
            'u_type':         r['u_type'],
            'u_sub_group':    r['u_sub_group'],
            'month':          r['month'],
            'year':           r['year'],
            'litres':         r['litres'],
            'linetotal':      r['linetotal'],
            'realise':        r['realise'],
            'target_sale':    tgt_ltrs,
            'target_realise': tgt_rate,
        })

    output.sort(key=lambda x: (
        0 if x['u_type'] == 'PREMIUM' else 1,
        -x.get('target_sale', 0),
        x['u_sub_group'],
        x['month'],
    ))

    channel_rows, channel_month_rows = _channel_aggregates(start_date, end_date, raw_rows)
    if body.get('include_hidden'):
        # Fold hidden invoices (U_ARNO='H', normally excluded) into the month pivot when the
        # Compare Sales "Hidden" toggle is ON. New list — the cached channel_month_rows is untouched.
        hidden_month_rows = _aggregate_channel_month_rows(services._fetch_hidden_raw(start_date, end_date))
        channel_month_rows = channel_month_rows + hidden_month_rows
    return JsonResponse({'status': 'ok', 'data': output, 'count': len(output),
                         'channel_rows': channel_rows, 'channel_month_rows': channel_month_rows})


@any_permission_flag('can_sales_cn', json_response=True)
@require_http_methods(['POST'])
def api_sales_cn_data(request):
    """Sales vs Credit Notes rows for a date range + company. Body: {start_date, end_date,
    company: 'oil'|'beverages'}. Returns the get_sales_cn_report payload (rows carry each
    dimension plus the sales / CN-goods / CN-service measures; the client pivots + filters)."""
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    company = body.get('company', 'oil')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)
    return JsonResponse(services.get_sales_cn_report(start_date, end_date, company))


@permission_flag_required('can_hidden_sales')
def hidden_sales(request):
    """Standalone tab: sales invoices flagged HIDDEN (OINV.U_ARNO='H') — the ones excluded from
    the dashboard's Done — surfaced per invoice line and drillable by customer / item / cost
    center / date / status, with quantity, litres and value. Data via /realise/api/hidden-sales/."""
    return render(request, 'realise/hidden_sales.html', {'sidebar_active': 'hidden_sales'})


@any_permission_flag('can_hidden_sales', json_response=True)
@require_http_methods(['POST'])
def api_hidden_sales_data(request):
    """Hidden invoice lines for a date range. Body: {start_date, end_date}."""
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)
    return JsonResponse(services.get_hidden_customer_sales(start_date, end_date))


@permission_flag_required('can_open_payments')
def open_payments(request):
    """Standalone tab: Open Payments — one row per incoming customer payment (SAP ORCT), showing the
    receipt amount ("payment on account") and its current open/unreconciled balance. The territory
    payload lets the client resolve the Contact Person dimension (same as Compare Sales). Data via
    /realise/api/open-payments/."""
    return render(request, 'realise/open_payments.html', {
        'sidebar_active': 'open_payments',
        'territory_payload': json.dumps(services.get_territory_dashboard_payload()),
    })


@any_permission_flag('can_open_payments', json_response=True)
@require_http_methods(['POST'])
def api_open_payments_data(request):
    """Incoming customer payments for a date range. Body: {start_date, end_date}."""
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)
    return JsonResponse(services.get_open_payments(start_date, end_date))


@permission_flag_required('can_dispatch_details')
def dispatch_details(request):
    """Standalone tab: Dispatch Details — one row per A/R invoice (SAP OINV) for the Oil company,
    showing its dispatch/logistics fields: Inv Date, Customer code/name, Inv No, Dispatch Date,
    Bilty Date, Bilty No, Transporter, Vehicle No and Driver Mobile No. Searchable / sortable
    register with an Excel export. Data via /realise/api/dispatch-details/."""
    return render(request, 'realise/dispatch_details.html', {'sidebar_active': 'dispatch_details'})


@any_permission_flag('can_dispatch_details', json_response=True)
@require_http_methods(['POST'])
def api_dispatch_details_data(request):
    """Dispatch details (invoice + logistics UDFs) for a date range. Body: {start_date, end_date}."""
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)
    return JsonResponse(services.get_dispatch_details(start_date, end_date))


@permission_flag_required('can_realise_calculator')
def realise_calculator(request):
    """Standalone tab: Realise Calculator — per-item Oil realisation (Revenue ÷ Volume) at two
    grains, ₹/Litre and ₹/Box, for a date range. A SAP-driven report (filter by P/C, sub-group,
    search; sortable; Excel export) plus an interactive what-if calculator that recomputes
    realisation from typed Revenue + Volume/Boxes. Data via /realise/api/realise-calculator/."""
    return render(request, 'realise/realise_calculator.html', {'sidebar_active': 'realise_calculator'})


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def api_realise_calculator_data(request):
    """Per-item Oil realisation for a date range. Body: {start_date, end_date}."""
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)
    return JsonResponse(services.get_realise_calculator(start_date, end_date))


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['GET'])
def api_realise_calculator_items(request):
    """Oil item master (Variety → SKU → Item) for the calculator's cascading item picker."""
    return JsonResponse(services.get_realise_calc_items())


# Column layout of the export (also the order used in the live formulas below).
_RC_XLSX_HEAD = [
    'Item', 'Item Code', 'Retailer ₹', 'SS %', 'Dist %', 'GST %', 'Pcs/Box', 'Flat Disc ₹',
    'Box Ltrs', 'Scheme L', 'To be sale (L)', 'SS Rate ₹', 'Dist Rate ₹', 'Ex-GST ₹',
    'Box Value ₹', 'Net Box ₹', 'Total Ltr/Box', 'RELISE ₹/L', 'Revenue ₹',
]


def _realise_calc_workbook(payload):
    """Build an .xlsx (bytes) from the calculator's plans, writing computed columns as LIVE Excel
    formulas (SS Rate … RELISE … Revenue) so the sheet recalculates if inputs are edited. Each
    plan gets a TOTAL row (Σ litres, Σ revenue, blended RELISE) and an optional KPI summary block.
    layout='single' packs every plan into ONE 'Compare' sheet with a coloured plan-header bar and
    tinted rows per plan; layout='separate' writes one sheet per plan."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    plans = [p for p in (payload.get('plans') or []) if isinstance(p, dict)]
    layout = payload.get('layout') or 'separate'
    summary = payload.get('summary') or []
    NCOL = len(_RC_XLSX_HEAD)
    widths = [34, 14, 11, 7, 7, 7, 9, 11, 9, 9, 13, 11, 11, 11, 12, 12, 13, 12, 14]

    wb = openpyxl.Workbook(); wb.remove(wb.active)
    hfill = PatternFill('solid', fgColor='0F172A')
    hfont = Font(bold=True, color='FFFFFF')
    tfont = Font(bold=True)

    def fnum(v):
        try:
            return float(v) if str(v).strip() != '' else 0.0
        except Exception:
            return 0.0

    def header(ws):
        ws.append(_RC_XLSX_HEAD)
        for c in ws[1]:
            c.fill = hfill; c.font = hfont; c.alignment = Alignment(horizontal='center', wrap_text=True)

    def item_row(ws, r, it, tint):
        ws.cell(r, 1, str(it.get('item') or '')); ws.cell(r, 2, str(it.get('code') or ''))
        for col, key in ((3,'retailer'),(4,'ss'),(5,'dm'),(6,'gst'),(7,'pcsbox'),(8,'disc'),(9,'boxltr'),(10,'scheme'),(11,'sell')):
            ws.cell(r, col, fnum(it.get(key)))
        ws.cell(r,12,f'=C{r}/(1+D{r}/100)'); ws.cell(r,13,f'=L{r}/(1+E{r}/100)'); ws.cell(r,14,f'=M{r}/(1+F{r}/100)')
        ws.cell(r,15,f'=N{r}*G{r}'); ws.cell(r,16,f'=O{r}-H{r}'); ws.cell(r,17,f'=I{r}+J{r}')
        ws.cell(r,18,f'=IF(Q{r}=0,0,P{r}/Q{r})'); ws.cell(r,19,f'=R{r}*K{r}')
        for cc in range(3, NCOL+1):
            ws.cell(r, cc).number_format = '#,##0.00'
        if tint:
            fl = PatternFill('solid', fgColor=tint)
            for cc in range(1, NCOL+1):
                ws.cell(r, cc).fill = fl

    def total_row(ws, tr, r0, last):
        ws.cell(tr,1,'TOTAL').font=tfont
        ws.cell(tr,11,f'=SUM(K{r0}:K{last})').font=tfont
        ws.cell(tr,18,f'=IF(SUM(K{r0}:K{last})=0,0,SUM(S{r0}:S{last})/SUM(K{r0}:K{last}))').font=tfont
        ws.cell(tr,19,f'=SUM(S{r0}:S{last})').font=tfont
        for cc in (11, 18, 19):
            ws.cell(tr, cc).number_format = '#,##0.00'

    def plan_bar(ws, r, name, hcolor):
        fl = PatternFill('solid', fgColor=hcolor or '4F46E5')
        for cc in range(1, NCOL+1):
            ws.cell(r, cc).fill = fl
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOL)
        tl = ws.cell(r, 1, name); tl.font = Font(bold=True, color='FFFFFF', size=12)
        tl.alignment = Alignment(horizontal='left', vertical='center')

    def summary_block(ws, r, rows):
        if not rows:
            return
        c = ws.cell(r, 1, 'SUMMARY / KPIs'); c.font = Font(bold=True, size=12); r += 1
        for kv in rows:
            ws.cell(r, 1, str(kv.get('label') or '')).font = tfont
            v = kv.get('value')
            if isinstance(v, (int, float)):
                ws.cell(r, 2, v); ws.cell(r, 2).number_format = '#,##0.00'
            else:
                ws.cell(r, 2, str(v))
            r += 1

    def widths_for(ws):
        for idx, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = w

    if layout == 'single':
        ws = wb.create_sheet('Compare')
        header(ws)
        cur = 2
        for plan in plans:
            items = plan.get('items') or []
            if not items:
                continue
            plan_bar(ws, cur, str(plan.get('name') or 'Plan'), plan.get('hcolor')); cur += 1
            r0 = cur
            for it in items:
                item_row(ws, cur, it, plan.get('color')); cur += 1
            last = cur - 1
            total_row(ws, cur, r0, last); cur += 2      # + blank spacer
        summary_block(ws, cur, summary)
        widths_for(ws)
    else:
        for plan in plans:
            items = plan.get('items') or []
            name = (str(plan.get('name') or 'Plan'))[:31] or 'Plan'
            ws = wb.create_sheet(name)
            header(ws)
            r0 = 2
            for i, it in enumerate(items):
                item_row(ws, r0+i, it, plan.get('color'))
            sr = r0
            if items:
                last = r0 + len(items) - 1
                total_row(ws, last+1, r0, last)
                sr = last + 3
            summary_block(ws, sr, summary)
            widths_for(ws)

    if not wb.sheetnames:
        wb.create_sheet('Empty')
    bio = BytesIO(); wb.save(bio)
    return bio.getvalue()


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def api_realise_calculator_export(request):
    """Export the calculator's items to .xlsx with LIVE formulas. Body: {filename, layout:'single'
    |'separate', plans:[{name, color, hcolor, items:[{item, code, retailer, ss, dm, gst, pcsbox,
    disc, boxltr, scheme, sell}, ...]}, ...], summary:[{label, value}, ...]}."""
    body = _parse_body(request)
    plans = body.get('plans') or []
    if not isinstance(plans, list) or not any(
            (isinstance(p, dict) and p.get('items')) for p in plans):
        return JsonResponse({'error': 'no items to export'}, status=400)
    content = _realise_calc_workbook(body)
    fn = re.sub(r'[^A-Za-z0-9._ -]', '_', str(body.get('filename') or 'Realise_Calculator'))[:120]
    if not fn.lower().endswith('.xlsx'):
        fn += '.xlsx'
    resp = HttpResponse(
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="{fn}"'
    return resp


def _rc_upload_field(t):
    """Map an uploaded-sheet header cell to a calculator input field (loose match). Deliberately
    strict on SS/Dist/GST so the export's computed columns (SS Rate, Ex-GST, …) are NOT picked up."""
    t = (str(t) if t is not None else '').strip().lower()
    if not t:
        return None
    if 'item code' in t or 'itemcode' in t or 'item_code' in t or t == 'code' or t.endswith(' code'):
        return 'code'
    if t == 'item' or 'item name' in t:
        return 'item'
    if 'retailer' in t:
        return 'retailer'
    if t in ('ss', 'ss %') or 'super stockist margin' in t:
        return 'ss'
    if t in ('dist', 'dist %') or 'distributor margin' in t:
        return 'dm'
    if t in ('gst', 'gst %'):
        return 'gst'
    if 'pcs/box' in t or 'pcs per box' in t or t == 'pcsbox':
        return 'pcsbox'
    if 'flat disc' in t:
        return 'disc'
    if 'box ltr' in t or 'box litre' in t:
        return 'boxltr'
    if 'scheme' in t:
        return 'scheme'
    if 'to be sale' in t or t == 'sell' or 'volume' in t:
        return 'sell'
    return None


_RC_CODE_RE = re.compile(r'^[A-Za-z]{1,4}\d{3,}$')     # e.g. FG0000030, RM0000015


def _parse_realise_upload(f):
    """Parse an uploaded .xlsx into calculator rows. First tries a header row containing an item-code
    column (then reads the other input columns too); if none is found, it falls back to detecting the
    column whose values match the SAP item master (a bare list of codes still works). Each code is
    matched against the master to fill name + Pcs/Box + Box Litres; missing inputs use defaults.
    Rows without a code (plan-header bars, TOTAL, blanks) are skipped."""
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(f.read()), data_only=True, read_only=True)
    master = {i['code']: i for i in (services.get_realise_calc_items().get('items') or [])}

    def s(v):
        if v is None:
            return ''
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()

    for ws in wb.worksheets:
        data = [list(r) for r in ws.iter_rows(values_only=True)]
        if not data:
            continue
        ncol = max((len(r) for r in data), default=0)

        # 1) header-based detection (scan the first 15 rows)
        colmap, header_ri = {}, None
        for ri in range(min(15, len(data))):
            cm = {}
            for ci, val in enumerate(data[ri]):
                fld = _rc_upload_field(val)
                if fld and fld not in cm:
                    cm[fld] = ci
            if 'code' in cm:
                colmap, header_ri = cm, ri
                break

        code_ci = colmap.get('code')
        start_ri = (header_ri + 1) if header_ri is not None else 0

        # 2) fallback: the column with the most master-matching codes
        if code_ci is None:
            best_ci, best_hits = None, 0
            for ci in range(ncol):
                hits = sum(1 for r in data if ci < len(r) and s(r[ci]) in master)
                if hits > best_hits:
                    best_ci, best_hits = ci, hits
            if best_hits >= 1:
                code_ci, colmap, start_ri = best_ci, {'code': best_ci}, 0
        if code_ci is None:
            continue

        out = []
        for r in data[start_ri:]:
            def g(fld):
                ci = colmap.get(fld)
                return r[ci] if (ci is not None and ci < len(r)) else None
            code = s(g('code'))
            if not code or code.upper() == 'TOTAL':
                continue
            m = master.get(code)
            if not m and not _RC_CODE_RE.match(code):    # skip stray text (e.g. a header caught in fallback)
                continue

            def val(fld, dflt=''):
                vs = s(g(fld))
                return vs if vs != '' else dflt

            out.append({
                'code': code,
                'item': (m['name'] if m else (s(g('item')) or code)),
                'retailer': val('retailer', ''),
                'ss': val('ss', '0'),
                'dm': val('dm', '0'),
                'gst': val('gst', '5'),
                'pcsbox': val('pcsbox', (s(m['pcs_per_box']) if m else '')),
                'disc': val('disc', '0'),
                'boxltr': val('boxltr', (s(m['box_litres']) if m else '')),
                'scheme': val('scheme', '0'),
                'sell': val('sell', ''),
                '_matched': bool(m),
            })
        if out:
            return out
    return []


def _rc_order_field(t):
    """Map an order-file header cell to an order field."""
    t = (str(t) if t is not None else '').strip().lower()
    if not t:
        return None
    if 'item code' in t or t == 'code':
        return 'code'
    if 'price list' in t:                 # "Price List (Basic)" — price WITHOUT GST
        return 'basic'
    if 'basic price' in t:                # fallback price column (some exports fill this instead)
        return 'basic2'
    if 'total ltr' in t or 'total litre' in t:
        return 'ltrs'
    if t in ('liters', 'litres', 'liter'):
        return 'liters'
    if t in ('qty', 'quantity'):
        return 'qty'
    return None


def _parse_realise_order_upload(f):
    """Parse an order export (Item Code + Price List (Basic) + Total Ltrs) into calculator rows.
    Price List (Basic) is ex-GST, so we gross it up by GST (5%) into the Retailer field and set
    GST%=5 — the box chain then removes it back to Basic, reproducing the order's realise/revenue.
    Item code is matched to the SAP master for name + Pcs/Box + Box Litres. Margins/disc/scheme=0;
    To-be-sale = the line's Total Ltrs."""
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(f.read()), data_only=True, read_only=True)
    master = {i['code']: i for i in (services.get_realise_calc_items().get('items') or [])}
    GST = 5.0

    def s(v):
        if v is None:
            return ''
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()

    def num(v):
        try:
            return float(str(v).replace(',', '')) if str(v).strip() != '' else 0.0
        except Exception:
            return 0.0

    for ws in wb.worksheets:
        data = [list(r) for r in ws.iter_rows(values_only=True)]
        if not data:
            continue
        colmap, header_ri = {}, None
        for ri in range(min(10, len(data))):
            cm = {}
            for ci, val in enumerate(data[ri]):
                fld = _rc_order_field(val)
                if fld and fld not in cm:
                    cm[fld] = ci
            if 'code' in cm and ('basic' in cm or 'basic2' in cm):
                colmap, header_ri = cm, ri
                break
        if header_ri is None:
            continue
        out = []
        for r in data[header_ri + 1:]:
            def g(fld):
                ci = colmap.get(fld)
                return r[ci] if (ci is not None and ci < len(r)) else None
            code = s(g('code'))
            if not code or code.upper() == 'TOTAL':
                continue
            m = master.get(code)
            basic = num(g('basic')) or num(g('basic2'))       # Price List (Basic), else Basic Price
            retailer = round(basic * (1 + GST / 100.0), 2) if basic else ''
            ltrs = num(g('ltrs')) or num(g('liters'))
            out.append({
                'code': code,
                'item': (m['name'] if m else code),
                'retailer': (str(retailer) if retailer != '' else ''),
                'ss': '0', 'dm': '0', 'gst': '5',
                'pcsbox': (s(m['pcs_per_box']) if m else ''),
                'disc': '0',
                'boxltr': (s(m['box_litres']) if m else ''),
                'scheme': '0',
                'sell': (str(ltrs) if ltrs else ''),
                '_matched': bool(m),
            })
        if out:
            return out
    return []


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def api_realise_calculator_order_upload(request):
    """Read an uploaded order export and return calculator rows (Old Order side of New vs Old)."""
    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'status': 'error', 'error': 'No file uploaded.'}, status=400)
    try:
        rows = _parse_realise_order_upload(f)
    except Exception as exc:
        logger.error('[REALISE-CALC ORDER] parse failed: %s', exc)
        return JsonResponse({'status': 'error', 'error': 'Could not read the order file.'}, status=400)
    if not rows:
        return JsonResponse({'status': 'error',
                             'error': 'No order lines found (need Item Code + Price List columns).'}, status=400)
    matched = sum(1 for r in rows if r.get('_matched'))
    for r in rows:
        r.pop('_matched', None)
    return JsonResponse({'status': 'ok', 'rows': rows, 'count': len(rows), 'matched': matched})


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def api_realise_calculator_upload(request):
    """Read an uploaded .xlsx of item codes (+ optional input columns) and return calculator rows,
    matched against the SAP item master. Multipart: field 'file'."""
    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'status': 'error', 'error': 'No file uploaded.'}, status=400)
    try:
        rows = _parse_realise_upload(f)
    except Exception as exc:
        logger.error('[REALISE-CALC UPLOAD] parse failed: %s', exc)
        return JsonResponse({'status': 'error', 'error': 'Could not read the Excel file.'}, status=400)
    if not rows:
        return JsonResponse({'status': 'error', 'error': 'No item-code rows found in the file.'}, status=400)
    matched = sum(1 for r in rows if r.get('_matched'))
    for r in rows:
        r.pop('_matched', None)
    return JsonResponse({'status': 'ok', 'rows': rows, 'count': len(rows), 'matched': matched})


# ══════════════════════ Rate List (saved calculator results) ══════════════════════

@permission_flag_required('can_realise_calculator')
def rate_list(request):
    """Rate List tab — browse saved Realise-Calculator results, grouped by state."""
    return render(request, 'realise/rate_list.html', {'sidebar_active': 'rate_list'})


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def api_rate_list_save(request):
    """Save a calculator result. Body: {name, state, scope('BOTH'/'A'/'B'), payload:{plans, compare}}."""
    from .models import RateList
    body = _parse_body(request)
    name = (body.get('name') or '').strip()
    if not name:
        return JsonResponse({'status': 'error', 'error': 'A result name is required.'}, status=400)
    payload = body.get('payload') or {}
    if not (isinstance(payload, dict) and payload.get('plans')):
        return JsonResponse({'status': 'error', 'error': 'Nothing to save.'}, status=400)
    obj = RateList.objects.create(
        name=name[:200],
        state=(body.get('state') or '').strip()[:100],
        channel=(body.get('channel') or '').strip().upper()[:20],
        scope=(str(body.get('scope') or 'BOTH').upper())[:10],
        payload=payload,
        created_by=(request.user.username if request.user.is_authenticated else ''),
    )
    return JsonResponse({'status': 'ok', 'id': obj.id})


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['GET'])
def api_rate_list(request):
    """List saved results (optionally filtered by ?state=...), newest first."""
    from .models import RateList
    qs = RateList.objects.all()
    one_id = request.GET.get('id')
    if one_id:                                     # single result (used by "Load into Calculator")
        try:
            qs = qs.filter(id=int(one_id))
        except (TypeError, ValueError):
            qs = qs.none()
    state = (request.GET.get('state') or '').strip()
    if state:
        qs = qs.filter(state=state)
    rows = [{
        'id': o.id, 'name': o.name, 'state': o.state, 'channel': o.channel, 'scope': o.scope,
        'payload': o.payload, 'created_by': o.created_by,
        'created_at': o.created_at.strftime('%Y-%m-%d %H:%M'),
    } for o in qs[:500]]
    states = list(RateList.objects.exclude(state='').order_by('state')
                  .values_list('state', flat=True).distinct())
    return JsonResponse({'status': 'ok', 'rows': rows, 'count': len(rows), 'states': states})


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def api_rate_list_delete(request):
    """Delete a saved result. Body: {id}."""
    from .models import RateList
    body = _parse_body(request)
    try:
        RateList.objects.filter(id=int(body.get('id'))).delete()
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'error': 'bad id'}, status=400)
    return JsonResponse({'status': 'ok'})


def _f(v):
    """Loose float: the saved payload stores calculator inputs as strings ('40000', '')."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _plan_assumptions(plan):
    """SS%/DM%/GST%/Disc for a plan, taken as the most common value across its already-priced
    rows. These are per-plan commercial terms, not constants — the saved 'both A & B' plans run
    3/5/5/0 while the live Delhi GT grid runs 0/0/5/0 — so an added row must inherit its own
    plan's. Rows without a retailer price are ignored: their zeros are a blank, not a decision.
    Mirrors planAssumptions() in plan_vs_done.html."""
    fields = ('ss', 'dm', 'gst', 'disc')
    tally = {k: {} for k in fields}
    priced = 0
    for r in (plan.get('rows') or []):
        if not isinstance(r, dict) or _f(r.get('retailer')) <= 0:
            continue
        priced += 1
        for k in fields:
            v = _f(r.get(k))
            tally[k][v] = tally[k].get(v, 0) + 1
    out = {'ss': 0.0, 'dm': 0.0, 'gst': 5.0, 'disc': 0.0}
    if not priced:
        return out
    for k in fields:
        if tally[k]:
            out[k] = max(tally[k].items(), key=lambda kv: kv[1])[0]
    return out


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def api_rate_list_add_items(request):
    """Append items to a saved result's plan from the Plan vs Done tab — the SKUs that sold this
    month but were never planned. Body: {id, plan, rows:[{item, code, scheme, sell, realise,
    pcsbox, boxltr}]}. `realise` is the item's ACTUAL ₹/L for the month (the tab uses the
    achieved realisation as the plan rate, since scheme + volume alone cannot derive one), so
    revenue = realise × sell. The price inputs stay blank — nothing is invented. Rows are written
    in the same shape the calculator saves, and the plan's totals are recomputed."""
    from .models import RateList
    body = _parse_body(request)
    try:
        obj = RateList.objects.filter(id=int(body.get('id'))).first()
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'error': 'bad id'}, status=400)
    if obj is None:
        return JsonResponse({'status': 'error', 'error': 'Saved result not found.'}, status=404)

    payload = obj.payload if isinstance(obj.payload, dict) else {}
    plans = payload.get('plans')
    if not isinstance(plans, list) or not plans:
        return JsonResponse({'status': 'error', 'error': 'This result has no plan to add to.'},
                            status=400)
    try:
        idx = int(body.get('plan') or 0)
    except (TypeError, ValueError):
        idx = 0
    if not (0 <= idx < len(plans)) or not isinstance(plans[idx], dict):
        return JsonResponse({'status': 'error', 'error': 'bad plan index'}, status=400)

    plan = plans[idx]
    rows = plan.get('rows')
    if not isinstance(rows, list):
        rows = plan['rows'] = []
    have = {str(r.get('code') or '').strip().upper() for r in rows if isinstance(r, dict)}

    incoming = body.get('rows')
    if not isinstance(incoming, list) or not incoming:
        return JsonResponse({'status': 'error', 'error': 'Nothing to add.'}, status=400)
    if len(incoming) > 200:
        return JsonResponse({'status': 'error', 'error': 'Too many rows in one save (max 200).'},
                            status=400)

    a = _plan_assumptions(plan)
    added = no_volume = duplicate = 0
    for src in incoming:
        if not isinstance(src, dict):
            continue
        code = str(src.get('code') or '').strip().upper()
        sell = _f(src.get('sell'))
        if not code or sell <= 0:
            no_volume += 1
            continue
        if code in have:                       # already planned — never duplicate a row
            duplicate += 1
            continue
        scheme, boxltr = _f(src.get('scheme')), _f(src.get('boxltr'))
        pcsbox, retailer = _f(src.get('pcsbox')), _f(src.get('retailer'))
        tot = boxltr + scheme
        if retailer > 0 and pcsbox > 0 and tot > 0:
            # Priced row: run the calculator's own formula on the plan's terms, so reloading the
            # result in the calculator reproduces this realise exactly instead of showing 0.
            ss_rate = retailer / (1 + a['ss'] / 100)
            dm_rate = ss_rate / (1 + a['dm'] / 100)
            exgst = dm_rate / (1 + a['gst'] / 100)
            box_val = exgst * pcsbox
            net_box = box_val - a['disc']
            realise = net_box / tot
            row = {'retailer': retailer, 'ss': a['ss'], 'dm': a['dm'], 'gst': a['gst'],
                   'disc': a['disc'], 'ssRate': ss_rate, 'dmRate': dm_rate, 'exgst': exgst,
                   'boxVal': box_val, 'netBox': net_box}
        else:
            # No price given: fall back to the month's actual realisation and leave the price
            # inputs blank — nothing is invented.
            realise = _f(src.get('realise'))
            row = {'retailer': '', 'ss': '', 'dm': '', 'gst': '', 'disc': '',
                   'ssRate': 0, 'dmRate': 0, 'exgst': 0, 'boxVal': 0, 'netBox': 0}
        row.update({
            'item': str(src.get('item') or code)[:200], 'code': code,
            'pcsbox': pcsbox or '', 'boxltr': boxltr or '',
            'scheme': scheme, 'sell': sell,
            'realise': realise, 'revenue': realise * sell, 'totLtr': tot,
        })
        rows.append(row)
        have.add(code)
        added += 1

    if not added:
        why = ('Those items are already in this plan.' if duplicate and not no_volume else
               'Enter a “To be sale” volume first.' if no_volume and not duplicate else
               'Nothing to add — items are already planned or have no volume.')
        return JsonResponse({'status': 'error', 'error': why}, status=400)

    tot_sell = sum(_f(r.get('sell')) for r in rows)
    rev_sum = sum(_f(r.get('revenue')) for r in rows)
    plan['totals'] = {'totSell': tot_sell, 'revSum': rev_sum,
                      'blend': (rev_sum / tot_sell) if tot_sell else 0}
    obj.payload = payload
    obj.save(update_fields=['payload'])
    return JsonResponse({'status': 'ok', 'added': added, 'duplicate': duplicate,
                         'no_volume': no_volume, 'rows': len(rows)})


# ══════════════════════ Plan vs Done ══════════════════════

def _month_bounds(month):
    """'YYYY-MM' -> (first_day, last_day) as 'YYYY-MM-DD'. Falls back to the current month."""
    import calendar
    today = datetime.now().date()
    try:
        y, m = str(month or '').split('-')
        y, m = int(y), int(m)
        if not (1 <= m <= 12 and 2000 <= y <= 2999):
            raise ValueError
    except (ValueError, AttributeError):
        y, m = today.year, today.month
    last = calendar.monthrange(y, m)[1]
    return f'{y:04d}-{m:02d}-01', f'{y:04d}-{m:02d}-{last:02d}', f'{y:04d}-{m:02d}'


@never_cache
@permission_flag_required('can_realise_calculator')
def plan_vs_done(request):
    """Plan vs Done tab — every saved Rate List result with the month's actual Done litres and
    realisation alongside each planned item. never_cache for the same reason as the dashboard:
    territory_payload (the person map behind the drill-down's Contact Person dimension) is baked
    into the HTML, so a mapping change must not be served from a stale copy."""
    return render(request, 'realise/plan_vs_done.html', {
        'sidebar_active': 'plan_vs_done',
        'territory_payload': json.dumps(services.get_territory_dashboard_payload()),
    })


@permission_flag_required('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def plan_vs_done_export(request):
    """Build an .xlsx from the Plan vs Done tables currently on screen (client scrapes each saved
    result's rendered table, honouring all filters/scope). Body: {filename, sheets:[{name,
    rows:[[cell, ...], ...]}]} — cell is a scalar or {value, colspan, bold, fill, color, align}."""
    from core.simple_xlsx import build_workbook
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        body = {}
    sheets = [(str(s.get('name') or 'Sheet'), s['rows'])
              for s in (body.get('sheets') or [])
              if isinstance(s, dict) and isinstance(s.get('rows'), list) and s['rows']]
    if not sheets:
        return JsonResponse({'error': 'no rows to export'}, status=400)
    fname = re.sub(r'[^A-Za-z0-9._ -]', '_', str(body.get('filename') or 'Plan-vs-Done'))[:120]
    if not fname.lower().endswith('.xlsx'):
        fname += '.xlsx'
    resp = HttpResponse(build_workbook(sheets),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="%s"' % fname
    return resp


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['GET'])
def api_done_by_item(request):
    """All-India Done litres/revenue per item code for ?month=YYYY-MM (default: this month)."""
    start, end, month = _month_bounds(request.GET.get('month'))
    payload = services.get_done_by_item(start, end)
    payload['month'] = month
    y, m = month.split('-')
    payload['targets'] = services.get_territory_targets(int(m), int(y))
    return JsonResponse(payload)


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['GET'])
def api_done_item_documents(request):
    """Party + invoice drill-down for one item: ?code=FG0000030&month=YYYY-MM."""
    code = (request.GET.get('code') or '').strip()
    if not code:
        return JsonResponse({'status': 'error', 'error': 'code is required'}, status=400)
    start, end, month = _month_bounds(request.GET.get('month'))
    state = (request.GET.get('state') or '').strip()      # blank = all-India
    channel = (request.GET.get('channel') or '').strip()  # blank = every channel
    basis = (request.GET.get('basis') or 'shipto').strip()
    payload = services.get_done_item_documents(code, start, end, state=state, channel=channel,
                                               basis=basis)
    payload['month'] = month
    payload['state'] = state
    payload['channel'] = channel
    return JsonResponse(payload)


@any_permission_flag('can_realise_calculator', json_response=True)
@require_http_methods(['POST'])
def api_rate_list_set_channel(request):
    """Tag a saved result with the channel it was planned for. Body: {id, channel}; '' clears it.

    Lives on the Rate List tab, where saved results are managed. Results created before the
    calculator offered a channel carry none, and Plan vs Done then measures them across every
    channel in their state — so "Delhi GT" counts Corporate sales, because GT is only in the
    name. The channel is never inferred from that name: a result called "Delhi — July plan"
    would be scoped wrongly and silently."""
    from .models import RateList
    body = _parse_body(request)
    try:
        obj = RateList.objects.filter(id=int(body.get('id'))).first()
    except (TypeError, ValueError):
        return JsonResponse({'status': 'error', 'error': 'bad id'}, status=400)
    if obj is None:
        return JsonResponse({'status': 'error', 'error': 'Saved result not found.'}, status=404)
    channel = (body.get('channel') or '').strip().upper()[:20]
    if channel and channel not in services.CHANNEL_MEMBERS:
        return JsonResponse({'status': 'error', 'error': 'Unknown channel.'}, status=400)
    obj.channel = channel
    obj.save(update_fields=['channel'])
    return JsonResponse({'status': 'ok', 'channel': obj.channel})


@permission_flag_required('can_customer_master')
def customer_master(request):
    """Standalone tab: the customer master — every customer (OCRD) with contact details, GSTIN /
    PAN, address & location, sales person, payment terms, credit limit, balance and status.
    Searchable / filterable table with an Excel export. Data via /realise/api/customer-master/."""
    return render(request, 'realise/customer_master.html', {'sidebar_active': 'customer_master'})


@any_permission_flag('can_customer_master', json_response=True)
@require_http_methods(['GET'])
def api_customer_master_data(request):
    """Full customer master (all OCRD customers) as JSON. Cached in the service layer."""
    return JsonResponse(services.get_customer_master())


# (key, column header) for the Customer Master Excel export — order = on-screen order.
_CUST_MASTER_COLS = [
    ('code', 'Code'), ('name', 'Customer Name'), ('main_group', 'Main Group'),
    ('status', 'Status'), ('gstin', 'GSTIN'), ('pan', 'PAN'),
    ('contact_person', 'Contact Person'), ('mobile', 'Mobile'),
    ('email', 'Email'), ('address', 'Address'), ('city', 'City'), ('state', 'State'),
    ('pincode', 'Pincode'), ('sales_person', 'Sales Person'), ('payment_terms', 'Payment Terms'),
    ('credit_limit', 'Credit Limit'), ('balance', 'Balance'),
]


@permission_flag_required('can_customer_master')
@require_http_methods(['GET'])
def export_customer_master(request):
    """Download the customer master as an .xlsx (all columns, all customers)."""
    payload = services.get_customer_master()

    def _cell(key, r):
        v = r.get(key, '')
        return round(float(v or 0), 2) if key in ('credit_limit', 'balance') else v

    header = [label for _, label in _CUST_MASTER_COLS]
    body = [[_cell(key, r) for key, _ in _CUST_MASTER_COLS] for r in payload.get('rows', [])]
    content = build_workbook([('Customer Master', [header] + body)])
    response = HttpResponse(
        content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="Customer Master.xlsx"'
    return response


@permission_flag_required('can_claims')
def claims(request):
    """Standalone tab: the Claims register — a manually-maintained table of claims (Claim Date,
    Party, Pass Date, Month & Year, Type, Hold, Amount, and the manual Passed / Hold / Reason
    columns). Rows are added/edited in-app (nothing is read from SAP as report data); SAP only
    powers the party and product/item pickers. Filter by Month/Year, date range and Type; Drill By
    Customer / Product / Item / Main Group. Data via /realise/api/claims/."""
    return render(request, 'realise/claims.html', {'sidebar_active': 'claims'})


@any_permission_flag('can_claims', json_response=True)
@require_http_methods(['GET'])
def api_claims_data(request):
    """All claim rows plus the entry-picker masters (customers / products / items)."""
    payload = services.get_claims()
    payload['masters'] = services.get_claim_masters()
    return JsonResponse(payload)


@any_permission_flag('can_claims', json_response=True)
@require_http_methods(['POST'])
def api_claim_save(request):
    """Create or update one claim from the add/edit form. Body: the claim fields (+ optional id)."""
    body = _parse_body(request)
    try:
        row = services.upsert_claim(body, user=request.user)
    except ValueError as exc:
        return JsonResponse({'status': 'error', 'error': str(exc)}, status=400)
    except Exception as exc:
        logger.error('[CLAIMS] save failed: %s', exc)
        return JsonResponse({'status': 'error', 'error': 'Could not save the claim.'}, status=500)
    return JsonResponse({'status': 'ok', 'row': row})


@any_permission_flag('can_claims', json_response=True)
@require_http_methods(['POST'])
def api_claim_delete(request):
    """Delete one claim by id. Body: {id}."""
    body = _parse_body(request)
    cid = body.get('id')
    if not cid:
        return JsonResponse({'status': 'error', 'error': 'id required'}, status=400)
    return JsonResponse({'status': 'ok', 'deleted': services.delete_claim(cid)})


@permission_flag_required('can_sales_flow')
def sales_document_flow(request):
    """Standalone tab: the sales document chain for a day's sales — Party, Sales Quotation No,
    Sales Order No, Invoice No and invoiced Litres, one row per document chain. Defaults to
    yesterday. Data via /realise/api/sales-flow/."""
    return render(request, 'realise/sales_document_flow.html', {'sidebar_active': 'sales_flow'})


@any_permission_flag('can_sales_flow', json_response=True)
@require_http_methods(['POST'])
def api_sales_flow_data(request):
    """Sales document-flow rows for a date range (defaults to yesterday when omitted).
    Body: {start_date, end_date}."""
    from datetime import date, timedelta
    body = _parse_body(request)
    yday = (date.today() - timedelta(days=1)).isoformat()
    start_date = body.get('start_date') or yday
    end_date = body.get('end_date') or yday
    company = body.get('company', 'oil')
    return JsonResponse(services.get_sales_document_flow(start_date, end_date, company))


@any_permission_flag('can_sales_flow', json_response=True)
@require_http_methods(['POST'])
def api_sales_flow_open_items(request):
    """Open line items of one Sales Order / Quotation, for the Sales Document Flow drill-down that
    opens on clicking an open document. Body: {doc_type:'order'|'quotation', doc_no, company}."""
    body = _parse_body(request)
    return JsonResponse(services.get_sales_flow_open_items(
        body.get('doc_type', ''), body.get('doc_no', ''), body.get('company', 'oil')))


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['POST'])
def api_beverages_data(request):
    """Beverages dataset (JIVO_BEVERAGES_HANADB / REPORT_SALES_COGS) — granular rows
    by Variety / Sub-Group / SKU with Quantity & Boxes for the dynamic driller."""
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date   = body.get('end_date', '')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)
    try:
        data = services.get_beverages_rows_cached(start_date, end_date)
    except Exception as e:
        logger.error('[BEVERAGES] fetch error: %s', e)
        return JsonResponse({'status': 'ok', 'data': [], 'count': 0, 'today_boxes': 0, 'yesterday_boxes': 0,
                             'today_items': [], 'yesterday_items': [], 'today_date': '', 'yesterday_date': '',
                             'customer_rows': [], 'month_rows': [], 'oih_rows': []})
    is_dict = isinstance(data, dict)
    rows = data.get('rows', []) if is_dict else (data or [])
    return JsonResponse({'status': 'ok', 'data': rows, 'count': len(rows),
                         'today_boxes': data.get('today_boxes', 0) if is_dict else 0,
                         'yesterday_boxes': data.get('yesterday_boxes', 0) if is_dict else 0,
                         'today_items': data.get('today_items', []) if is_dict else [],
                         'yesterday_items': data.get('yesterday_items', []) if is_dict else [],
                         'today_date': data.get('today_date', '') if is_dict else '',
                         'yesterday_date': data.get('yesterday_date', '') if is_dict else '',
                         'customer_rows': data.get('customer_rows', []) if is_dict else [],
                         'month_rows': data.get('month_rows', []) if is_dict else [],
                         'oih_rows': data.get('oih_rows', []) if is_dict else []})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_beverages_docs(request):
    """Invoice / open-SO documents behind a beverages driller cell, filtered to the clicked
    node (customer + ancestor dims + brand/month). metric=sales -> invoices, oih -> SOs."""
    start = request.GET.get('start', '') or ''
    end = request.GET.get('end', '') or ''
    metric = str(request.GET.get('metric', 'sales') or 'sales').strip().lower()
    if not start or not end:
        return JsonResponse({'status': 'error', 'error': 'start and end required'}, status=400)
    filters = {}
    for key in ('variety', 'sub_group', 'sku', 'item', 'main_group', 'state',
                'brand', 'chain', 'sales_person', 'customer', 'ym'):
        val = request.GET.get('f_' + key)
        if val not in (None, ''):
            filters[key] = str(val).strip().upper()
    try:
        data = services.get_beverages_documents(start, end, filters, metric)
    except Exception as exc:
        logger.error('[BEVERAGES] docs fetch failed: %s', exc)
        return JsonResponse({'status': 'ok', 'metric': metric, 'count': 0, 'data': []})
    return JsonResponse({'status': 'ok', 'metric': metric, 'count': len(data), 'data': data})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['POST'])
def api_drill_down(request):
    body = _parse_body(request)
    start_date   = body.get('start_date', '')
    end_date     = body.get('end_date', '')
    u_type       = body.get('u_type') or body.get('product_type', '')
    u_sub_group  = body.get('u_sub_group') or body.get('sub_group', '')
    drill_by     = body.get('drill_by', 'State')
    month        = body.get('month')
    year         = body.get('year')
    filters      = body.get('filters') or {}

    type_filter = _get_type_filter(request)
    if type_filter and u_type and u_type.upper() != type_filter:
        return JsonResponse({'data': []})

    cache_key = f'{start_date}_{end_date}'
    if _raw_cache['key'] != cache_key:
        try:
            _, raw_rows = services.get_sales_data(start_date, end_date)
            _raw_cache['key']  = cache_key
            _raw_cache['rows'] = raw_rows
        except Exception as e:
            logger.error('[DRILL] fetch failed: %s', e)
            return JsonResponse({'data': []})
    else:
        raw_rows = _raw_cache['rows']

    data = services.get_drill_down(
        start_date, end_date, raw_rows,
        u_type=u_type, u_sub_group=u_sub_group,
        drill_by=drill_by, month=month, year=year, filters=filters,
    )
    return JsonResponse({'data': data})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['POST'])
def api_historical_realise(request):
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date   = body.get('end_date', '')
    period     = body.get('period', '12m')

    type_filter = _get_type_filter(request)

    try:
        result, drill_result = services.get_historical_realise(start_date, end_date, period)
    except Exception as e:
        logger.error('[HIST] error: %s', e)
        return JsonResponse({'status': 'ok', 'data': {}, 'drill_data': {}})

    if type_filter:
        result = {k: v for k, v in result.items() if k.startswith(type_filter + '|')}
        drill_result = {k: v for k, v in drill_result.items() if k.startswith(type_filter + '|')}

    return JsonResponse({'status': 'ok', 'data': result, 'drill_data': drill_result, 'period': period})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_targets(request):
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year  = int(request.GET.get('year',  datetime.now().year))
    except (ValueError, TypeError):
        month = datetime.now().month
        year  = datetime.now().year

    data = services.get_targets_for_month(month, year)
    return JsonResponse({'status': 'ok', 'month': month, 'year': year, 'data': data})


@group_required('realise_admin', json_response=True)
@require_http_methods(['POST'])
def api_save_targets(request):
    body = _parse_body(request)
    month   = body.get('month')
    year    = body.get('year')
    targets = body.get('targets', [])

    if not month or not year or not targets:
        return JsonResponse({'status': 'error', 'error': 'month, year, and targets required'}, status=400)

    try:
        month = int(month)
        year  = int(year)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'month and year must be integers'}, status=400)

    saved = services.save_monthly_targets(targets, month, year, request.user)
    return JsonResponse({'status': 'ok', 'saved': saved})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['POST'])
def api_verify_pin(request):
    body = _parse_body(request)
    pin = body.get('pin', '')
    if pin == EDIT_PIN:
        return JsonResponse({'status': 'ok', 'verified': True})
    return JsonResponse({'status': 'error', 'verified': False, 'message': 'Incorrect password'})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_channel_targets(request):
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year = int(request.GET.get('year', datetime.now().year))
    except (ValueError, TypeError):
        month = datetime.now().month
        year = datetime.now().year
    data = services.get_channel_target_map(month, year, request.GET.get('seg', ''))
    return JsonResponse({'status': 'ok', 'month': month, 'year': year, 'data': data})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_order_in_hand(request):
    data = services.get_order_in_hand_by_person()
    return JsonResponse({'status': 'ok', 'data': data})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_order_in_hand_rows(request):
    return JsonResponse({'status': 'ok', 'data': services.get_order_in_hand_rows()})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_sales_pulse(request):
    """Tiny data fingerprint for the dashboard 'live' heartbeat (see services.get_sales_pulse).
    The client polls this cheaply every ~30s and only forces a fresh pull when it changes."""
    dataset = (request.GET.get('dataset') or 'oils').strip().lower()
    start = (request.GET.get('start') or '').strip()
    end = (request.GET.get('end') or '').strip()
    if not start or not end:
        return JsonResponse({'status': 'ok', 'pulse': ''})
    return JsonResponse({'status': 'ok', 'pulse': services.get_sales_pulse_cached(dataset, start, end)})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_channel_detail_docs(request):
    """Documents (invoices / open SOs) behind a Done-L or Order-in-Hand cell in the
    channel-detail modal, filtered to the clicked drill node. Same SAP sources as the
    dashboard's Done / Order-in-Hand numbers."""
    channel = str(request.GET.get('channel', '') or '').strip().upper()
    metric = str(request.GET.get('metric', 'done') or 'done').strip().lower()
    seg = request.GET.get('seg', '')
    filters = {}
    for key in ('group', 'state', 'person', 'customer', 'product', 'item'):
        val = request.GET.get(key)
        if val not in (None, ''):
            filters[key] = str(val).strip().upper()
    if metric == 'oih':
        data = services.get_channel_oih_documents(channel, filters, seg)
    else:
        start = request.GET.get('start') or _raw_cache.get('start') or ''
        end = request.GET.get('end') or _raw_cache.get('end') or ''
        # Diagnostic: ?reconcile=1 returns a party-by-party comparison of the channel Done
        # (proc) vs the popup Done (direct query) so any mismatch can be pinpointed.
        if request.GET.get('reconcile'):
            return JsonResponse({'status': 'ok', 'reconcile':
                services.reconcile_channel_done(start, end, channel, seg, filters.get('state', ''))})
        data = services.get_channel_done_documents(start, end, channel, seg, filters)
    return JsonResponse({'status': 'ok', 'metric': metric, 'count': len(data),
                         'warehouses': services.OIH_STOCK_WAREHOUSES, 'data': data})


@any_permission_flag('can_realise', 'can_compare_sales', json_response=True)
@require_http_methods(['POST'])
def api_compare_docs(request):
    """Invoices (Doc No / date / party / litres) with per-item breakdown behind a Compare-Sales
    value cell. Reuses the channel-detail Done-documents source (OINV/INV1 + ORIN/RIN1), but with
    no channel restriction (channel='') so the clicked pivot node's own dimension filters
    (group / state / person / customer / product / item) + the cell's month decide the rows."""
    body = _parse_body(request)
    start = body.get('start_date', '') or ''
    end = body.get('end_date', '') or ''
    seg = str(body.get('seg', '') or '').strip().upper()
    filters = {}
    for key in ('group', 'state', 'person', 'customer', 'product', 'item'):
        val = body.get(key)
        if val not in (None, ''):
            filters[key] = str(val).strip().upper()
    try:
        data = services.get_compare_sales_documents(start, end, seg, filters)
    except Exception as e:
        logger.error('[COMPARE-DOCS] failed: %s', e)
        return JsonResponse({'status': 'error', 'error': str(e), 'data': []})
    return JsonResponse({'status': 'ok', 'count': len(data), 'data': data})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_commodity_oih_rows(request):
    """Open-order litres for COMMODITY items, shaped for the commodity table's OIH."""
    return JsonResponse({'status': 'ok', 'data': services.get_commodity_oih_rows()})


@any_permission_flag('can_realise', 'can_oih_vs_stock', json_response=True)
@require_http_methods(['GET'])
def api_oih_breakdown(request):
    """Granular open-order litres by item dimensions (split Premium/Commodity) for the
    OIH KPI window's dynamic drill; the client nests them into any chosen order.
    Cached (90s) so repeat opens of the OIH-vs-Stock tab / dashboard reuse one pull."""
    return JsonResponse({'status': 'ok', **services.get_oih_dimension_rows_cached()})


@any_permission_flag('can_realise', 'can_oih_vs_stock', json_response=True)
@require_http_methods(['GET'])
def api_oih_breakdown_beverages(request):
    """Jivo Beverages variant of the OIH-vs-Stock breakdown: open-order BOXES vs on-hand
    stock (boxes), grouped by the beverages family (DRINKS/WATER/…) and variety. Same payload
    shape as api_oih_breakdown so the OIH-vs-Stock page renders it with the Beverages toggle."""
    return JsonResponse({'status': 'ok', **services.get_oih_dimension_rows_beverages_cached()})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_target_nodes(request):
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year = int(request.GET.get('year', datetime.now().year))
    except (ValueError, TypeError):
        month, year = datetime.now().month, datetime.now().year
    return JsonResponse({'status': 'ok', 'month': month, 'year': year,
                         'data': services.get_target_nodes(month, year, request.GET.get('seg', ''))})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET', 'POST'])
def api_flex_targets(request):
    """Persisted 'Flex TGT' overrides for the Sales Channel drill table.
    GET  ?seg=&month=&year=          -> {data: {row_key: value}}
    POST {seg, month, year, row_key, value}  upserts one override (value null/'' clears it)."""
    if request.method == 'POST':
        body = _parse_body(request)
        ok = services.save_flex_target(body.get('seg', ''), body.get('month'), body.get('year'),
                                       body.get('row_key', ''), body.get('value'))
        return JsonResponse({'status': 'ok' if ok else 'error'})
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year = int(request.GET.get('year', datetime.now().year))
    except (ValueError, TypeError):
        month, year = datetime.now().month, datetime.now().year
    return JsonResponse({'status': 'ok', 'month': month, 'year': year,
                         'data': services.get_flex_targets(request.GET.get('seg', ''), month, year)})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_segment_targets(request):
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year = int(request.GET.get('year', datetime.now().year))
    except (ValueError, TypeError):
        month = datetime.now().month
        year = datetime.now().year

    segment = str(request.GET.get('segment', 'state') or 'state').strip().lower()
    data = services.get_segment_target_map(segment, month, year, request.GET.get('seg', ''))
    return JsonResponse({'status': 'ok', 'segment': segment, 'month': month, 'year': year, 'data': data})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_territory_map(request):
    """The fixed channel×state grid with each cell's current sales person, plus the
    channel order and the distinct people list (for the reassign dropdown)."""
    return JsonResponse({'status': 'ok', **services.get_territory_map_payload()})


@group_required('realise_admin', json_response=True)
@require_http_methods(['POST'])
def api_save_territory_map(request):
    """Update ONLY the sales_person on existing grid cells (admin only)."""
    body = _parse_body(request)
    assignments = body.get('assignments', [])
    if not isinstance(assignments, list):
        return JsonResponse({'status': 'error', 'error': 'assignments must be a list'}, status=400)
    saved = services.save_territory_persons(assignments, request.user)
    return JsonResponse({'status': 'ok', 'saved': saved})


@group_required('realise_admin', json_response=True)
@require_http_methods(['POST'])
def api_refresh_territory_map(request):
    """Re-sync the fixed grid from live SAP (adds new channel×state cells; never
    wipes existing person assignments). Admin only."""
    from django.core.management import call_command
    try:
        call_command('seed_territory_map', refresh=True)
    except Exception as e:
        logger.error('[TERRITORY] refresh failed: %s', e)
        return JsonResponse({'status': 'error', 'error': str(e)}, status=500)
    return JsonResponse({'status': 'ok', **services.get_territory_map_payload()})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_territory_targets(request):
    """Single target (litres) per (channel, state) territory for a month/year."""
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year = int(request.GET.get('year', datetime.now().year))
    except (ValueError, TypeError):
        month, year = datetime.now().month, datetime.now().year
    return JsonResponse({'status': 'ok', 'month': month, 'year': year,
                         'data': services.get_territory_targets(month, year)})


@group_required('realise_admin', json_response=True)
@require_http_methods(['POST'])
def api_save_territory_targets(request):
    """Save one target (litres) per (channel, state) territory (admin only)."""
    body = _parse_body(request)
    month, year = body.get('month'), body.get('year')
    targets = body.get('targets', [])
    if not month or not year or not isinstance(targets, list):
        return JsonResponse({'status': 'error', 'error': 'month, year, targets[] required'}, status=400)
    try:
        month, year = int(month), int(year)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'month and year must be integers'}, status=400)
    saved = services.save_territory_targets(month, year, targets)
    return JsonResponse({'status': 'ok', 'saved': saved})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_territory_product_targets(request):
    """Per-product targets (litres+realise) per (channel,state) for a period, plus the
    product master — the shape the Person Mapping 'Set product targets' UI consumes."""
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year = int(request.GET.get('year', datetime.now().year))
    except (ValueError, TypeError):
        month, year = datetime.now().month, datetime.now().year
    return JsonResponse({'status': 'ok', 'month': month, 'year': year,
                         'products': services.get_product_master(),
                         'data': services.get_territory_product_targets(month, year)})


@group_required('realise_admin', json_response=True)
@require_http_methods(['POST'])
def api_save_territory_product_targets(request):
    """Save the full per-product target set for a period; rolls up into the dashboard's
    TargetNode (channel/state) and MonthlyTarget (per-product). Admin only."""
    body = _parse_body(request)
    month, year = body.get('month'), body.get('year')
    targets = body.get('targets', {})
    if not month or not year or not isinstance(targets, dict):
        return JsonResponse({'status': 'error', 'error': 'month, year, targets{} required'}, status=400)
    try:
        month, year = int(month), int(year)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'month and year must be integers'}, status=400)
    saved = services.save_territory_product_targets(month, year, targets, request.user)
    return JsonResponse({'status': 'ok', 'saved': saved})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_channel_quick_targets(request):
    """Channel Targets editor data: each channel's previous-month actual sale +
    current channel-level target for the selected (month, year)."""
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year = int(request.GET.get('year', datetime.now().year))
    except (ValueError, TypeError):
        month, year = datetime.now().month, datetime.now().year
    return JsonResponse({'status': 'ok', **services.get_channel_quick_payload(month, year)})


@group_required('realise_admin', json_response=True)
@require_http_methods(['POST'])
def api_save_channel_quick_targets(request):
    """Save channel-level targets (one total per channel) for a period. Stored as
    state-blank TargetNodes that survive per-product saves and drive the dashboard's
    channel TGT-L. Admin only."""
    body = _parse_body(request)
    month, year = body.get('month'), body.get('year')
    items = body.get('targets', [])
    if not month or not year or not isinstance(items, list):
        return JsonResponse({'status': 'error', 'error': 'month, year, targets[] required'}, status=400)
    try:
        month, year = int(month), int(year)
    except (ValueError, TypeError):
        return JsonResponse({'status': 'error', 'error': 'month and year must be integers'}, status=400)
    saved = services.save_channel_node_targets(month, year, items, request.user)
    return JsonResponse({'status': 'ok', 'saved': saved})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_product_actuals(request):
    """Previous-month actual sale per product for one (channel, state) — feeds the
    'last month sold' reference on each product card in the target editor."""
    channel = request.GET.get('channel', '')
    state = request.GET.get('state', '')
    try:
        month = int(request.GET.get('month', datetime.now().month))
        year = int(request.GET.get('year', datetime.now().year))
    except (ValueError, TypeError):
        month, year = datetime.now().month, datetime.now().year
    if not channel:
        return JsonResponse({'status': 'error', 'error': 'channel required'}, status=400)
    return JsonResponse({'status': 'ok', **services.get_product_actuals_payload(channel, state, month, year)})


@never_cache
@group_required(*REALISE_GROUPS, json_response=False)
@require_http_methods(['GET'])
def person_targets_page(request):
    """Person Mapping + Targets page (the React 'Persons Frontend'). Replaces the old
    hierarchical Update Targets editor — reached from the dashboard's Update Targets
    button. Hosts the React app in a same-origin iframe so its global CSS can't touch
    the dashboard chrome. Person reassignment → TerritoryMapping; single target per
    territory → TargetNode."""
    return render(request, 'realise/person_targets.html', {'sidebar_active': 'realise'})


@xframe_options_sameorigin
@never_cache
@group_required(*REALISE_GROUPS, json_response=False)
@require_http_methods(['GET'])
def person_targets_embed(request):
    """Standalone React app (Person Mapping & Targets) shown inside the iframe.
    Marked same-origin-frameable because Django's default X-Frame-Options is DENY,
    which would otherwise block our own iframe."""
    import os
    from django.conf import settings
    from django.middleware.csrf import get_token
    from django.urls import reverse
    from core.context_processors import derive_realise_profile
    role, _, _ = derive_realise_profile(request.user)
    now = datetime.now()

    # Cache-bust the iframe's JS/CSS by their file mtime, so a recompiled bundle is
    # picked up immediately (browsers cache iframe sub-resources aggressively).
    _sdir = os.path.join(str(settings.BASE_DIR), 'realise', 'static', 'realise')
    try:
        asset_ver = int(max(os.path.getmtime(os.path.join(_sdir, 'person_targets.js')),
                            os.path.getmtime(os.path.join(_sdir, 'person_targets.css'))))
    except OSError:
        asset_ver = 1
    boot = {
        'csrf': get_token(request),
        'isAdmin': role == 'admin',
        'month': now.month,
        'year': now.year,
        'monthOptions': list(enumerate(services.MONTHS_ORDER, start=1)),
        'yearOptions': list(range(now.year + 1, now.year - 5, -1)),
        'dashboardUrl': reverse('realise:dashboard') + '#slide2',
        'urls': {
            'map':                reverse('realise:api_territory_map'),
            'mapSave':            reverse('realise:api_save_territory_map'),
            'refresh':            reverse('realise:api_refresh_territory_map'),
            'productTargets':     reverse('realise:api_territory_product_targets'),
            'productTargetsSave': reverse('realise:api_save_territory_product_targets'),
            'channelTargets':     reverse('realise:api_channel_quick_targets'),
            'channelTargetsSave': reverse('realise:api_save_channel_quick_targets'),
            'productActuals':     reverse('realise:api_product_actuals'),
        },
    }
    return render(request, 'realise/person_targets_embed.html', {'boot': boot, 'asset_ver': asset_ver})


@group_required('realise_admin', json_response=False)
@require_http_methods(['GET', 'POST'])
def channel_targets_page(request):
    success = request.GET.get('saved') == '1'
    now = datetime.now()
    error_message = ''
    source = request.POST if request.method == 'POST' else request.GET

    try:
        month = int(source.get('month', now.month))
        year = int(source.get('year', now.year))
    except (TypeError, ValueError):
        month, year = now.month, now.year

    valid_hier = {key for key, _ in services.HIER_ORDERS}
    hier_order = source.get('hier_order', 'mg_state_sp')
    if hier_order not in valid_hier:
        hier_order = 'mg_state_sp'

    hier_filters = {
        'main_group': str(source.get('main_group', '') or '').strip().upper(),
        'state': str(source.get('state', '') or '').strip().upper(),
        'sales_person': str(source.get('sales_person', '') or '').strip().upper(),
    }

    product_segment = services._norm_segment(source.get('segment', ''))

    if request.method == 'POST':
        form_mode = request.POST.get('form_mode', 'segment')
        if form_mode == 'hier':
            keys = request.POST.getlist('node_key')
            vals = request.POST.getlist('node_val')
            reals = request.POST.getlist('node_realise')
            triples = [(keys[i], vals[i] if i < len(vals) else '0',
                        reals[i] if i < len(reals) else '0') for i in range(len(keys))]
            try:
                services.save_hier_targets(month, year, triples, product_segment)
                query = f'hier_order={hier_order}&month={month}&year={year}&saved=1'
                if product_segment:
                    query += f'&segment={product_segment}'
                for key, value in hier_filters.items():
                    if value:
                        query += f'&{key}={value}'
                return redirect(f'/realise/targets/?{query}#hier')
            except ValueError as exc:
                error_message = str(exc)

    master_rows = services.get_territory_master_rows()
    hier_filter_options = services.get_hier_filter_options(master_rows)
    saved_nodes = services.TargetNode.objects.filter(month=month, year=year)
    if product_segment:
        saved_nodes = saved_nodes.filter(segment=product_segment)
    saved_target_map = {
        f'{node.main_group}|{node.state}|{node.sales_person}': {
            'ltrs': float(node.target_ltrs or 0),
            'realise': float(node.target_realise or 0),
        }
        for node in saved_nodes
    }

    context = {
        'sidebar_active': 'realise',
        'target_month': month,
        'target_year': year,
        'product_segment': product_segment,
        'hier_order': hier_order,
        'hier_order_options': services.HIER_ORDERS,
        'hier_filters': hier_filters,
        'hier_filter_options': hier_filter_options,
        'hier_rows': services.get_hier_rows(hier_order, month, year, master_rows, hier_filters, product_segment),
        'hier_master_rows': master_rows,
        'saved_target_map': saved_target_map,
        'month_options': list(enumerate(services.MONTHS_ORDER, start=1)),
        'year_options': list(range(year + 1, year - 5, -1)),
        'save_success': success,
        'error_message': error_message,
    }
    return render(request, 'realise/channel_targets.html', context)


@group_required(*REALISE_GROUPS, json_response=False)
@require_http_methods(['GET'])
def channel_detail_placeholder(request, group):
    return render(request, 'realise/channel_detail_placeholder.html', {
        'sidebar_active': 'realise',
        'channel_group': str(group or '').upper(),
    })


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_export_raw_csv(request):
    rows    = _raw_cache.get('rows') or []
    columns = _raw_cache.get('columns') or []
    if not rows or not columns:
        return JsonResponse({'error': 'No data — click Fetch Data first'}, status=400)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        values = []
        for col in columns:
            val = row.get(col, '')
            if hasattr(val, 'isoformat'):
                val = val.isoformat()
            values.append(val)
        writer.writerow(values)

    start = _raw_cache.get('start', 'from')
    end   = _raw_cache.get('end', 'to')
    filename = f'Sales_RAW_{start}_{end}.csv'
    response = HttpResponse(buf.getvalue(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _raw_sheet_rows(columns, rows):
    sheet = [[{'value': col, 'style': 1} for col in columns]]
    for row in rows:
        values = []
        for col in columns:
            val = row.get(col, '')
            if hasattr(val, 'isoformat'):
                val = val.isoformat()
            values.append(val)
        sheet.append(values)
    return sheet


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['POST'])
def api_export_excel(request):
    rows = _raw_cache.get('rows') or []
    columns = _raw_cache.get('columns') or []
    if not rows or not columns:
        return JsonResponse({'error': 'No data - click Fetch Data first'}, status=400)

    body = _parse_body(request)
    layout_rows = body.get('layout_rows') or []
    start = _raw_cache.get('start', 'from')
    end = _raw_cache.get('end', 'to')
    content = build_workbook([
        ('Layout', layout_rows or [['Realise Dashboard']]),
        ('Raw Data', _raw_sheet_rows(columns, rows)),
    ])
    filename = f'Realise_Export_{start}_{end}.xlsx'
    response = HttpResponse(
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
