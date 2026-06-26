import csv
import io
import json
import logging
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
    return render(request, 'realise/customer_aging.html', {
        'sidebar_active': 'customer_aging',
        # raw dict — the template's |json_script does the JSON serialization (passing a
        # pre-dumped string here would double-encode and JSON.parse would yield a string).
        'aging_payload': services.get_customer_aging(aging_date),
        'aging_date': aging_date.isoformat(),
        'aging_today': today.isoformat(),
    })


@permission_flag_required('can_required_credit_limit')
def required_credit_limit(request):
    """Standalone tab: Required Credit Limit — live Order-in-Hand grouped by ASM
    (territory owner) → party, in the closing-sheet layout. Each party row shows open
    litres + open value (₹), a Premium/Commodity filter at the top, and a frontend-
    editable delivery remark (the only writable column; persisted to ClosingRemark)."""
    return render(request, 'realise/required_credit_limit.html', {
        'sidebar_active': 'required_credit_limit',
        # raw dict — the template's |json_script does the JSON serialization.
        'credit_payload': services.get_required_credit_rows(),
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
    payload = services.get_required_credit_rows()
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
        item_name = str(row.get('ItemName', '') or '').strip().upper()
        key = (u_type, u_sub, u_main, state, sales_person, card_name, item_name)
        bucket = agg.get(key)
        if bucket is None:
            bucket = agg[key] = {
                'u_type': u_type, 'u_sub_group': u_sub, 'u_main_group': u_main,
                'state': state, 'sales_person': sales_person, 'card_name': card_name,
                'item_name': item_name,
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
        item_name = str(row.get('ItemName', '') or '').strip().upper()
        card_name = str(row.get('CardName', '') or '').strip().upper()
        ym = '%s-%02d' % (year, mnum)
        key = (u_type, u_main, state, sales_person, u_sub, item_name, card_name, ym)
        bucket = agg.get(key)
        if bucket is None:
            bucket = agg[key] = {
                'u_type': u_type, 'main_group': u_main, 'state': state,
                'sales_person': sales_person, 'u_sub_group': u_sub, 'item_name': item_name,
                'card_name': card_name, 'ym': ym, 'mlabel': '%s %s' % (mon, year),
                'liter': 0.0, 'line_total': 0.0,
            }
        bucket['liter'] += float(row.get('Liter', 0) or 0)
        bucket['line_total'] += float(row.get('LineTotal', 0) or 0)
    out = list(agg.values())
    for b in out:
        b['liter'] = round(b['liter'], 2)
        b['line_total'] = round(b['line_total'], 2)
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
    return JsonResponse({'status': 'ok', 'data': output, 'count': len(output),
                         'channel_rows': channel_rows, 'channel_month_rows': channel_month_rows})


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
