import csv
import io
import json
import logging
from datetime import datetime

from django.http import JsonResponse, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from core.decorators import group_required
from core import sap_connector
from core.simple_xlsx import build_workbook
from . import services

logger = logging.getLogger(__name__)

REALISE_GROUPS = ('realise_admin', 'realise_premium', 'realise_commodity')

# In-memory cache: stores the last fetched raw SAP rows per session is not enough;
# we use a module-level cache keyed by (start_date, end_date).
_raw_cache = {'key': None, 'rows': [], 'columns': []}

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


@group_required(*REALISE_GROUPS, json_response=False)
def dashboard(request):
    return render(request, 'realise/dashboard.html', {
        'sidebar_active': 'realise',
        'territory_payload': json.dumps(services.get_territory_dashboard_payload()),
    })


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


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['POST'])
def api_sales_data(request):
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date   = body.get('end_date', '')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)

    type_filter = _get_type_filter(request)

    try:
        result, raw_rows = services.get_sales_data_cached(start_date, end_date)
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

    channel_rows = _aggregate_channel_rows(raw_rows)
    return JsonResponse({'status': 'ok', 'data': output, 'count': len(output), 'channel_rows': channel_rows})


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
        data = services.get_channel_done_documents(start, end, channel, seg, filters)
    return JsonResponse({'status': 'ok', 'metric': metric, 'count': len(data), 'data': data})


@group_required(*REALISE_GROUPS, json_response=True)
@require_http_methods(['GET'])
def api_commodity_oih_rows(request):
    """Open-order litres for COMMODITY items, shaped for the commodity table's OIH."""
    return JsonResponse({'status': 'ok', 'data': services.get_commodity_oih_rows()})


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
        'hier_rows': services.get_hier_rows(hier_order, month, year, master_rows, hier_filters),
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
