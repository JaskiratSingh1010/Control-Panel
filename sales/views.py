import csv
import io
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from core import sap_connector
from core.context_processors import derive_realise_profile
from core.decorators import permission_flag_required
from core.simple_xlsx import build_workbook
from realise import services

logger = logging.getLogger(__name__)

_raw_cache = {'key': None, 'rows': [], 'columns': []}


def _parse_body(request):
    try:
        return json.loads(request.body)
    except Exception:
        return {}


def _get_type_filter(request):
    _, type_filter, _ = derive_realise_profile(request.user)
    return type_filter


@permission_flag_required('can_sales')
def dashboard(request):
    return render(request, 'sales/dashboard.html', {'sidebar_active': 'sales'})


@permission_flag_required('can_sales', json_response=True)
@require_http_methods(['GET'])
def api_health(request):
    ok, message = sap_connector.health_check()
    return JsonResponse({
        'sap_connected': ok,
        'message': message,
        'username': request.user.username,
    })


@permission_flag_required('can_sales', json_response=True)
@require_http_methods(['POST'])
def api_sales_data(request):
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    if not start_date or not end_date:
        return JsonResponse({'status': 'error', 'error': 'start_date and end_date required'}, status=400)

    type_filter = _get_type_filter(request)

    try:
        result, raw_rows = services.get_sales_data(start_date, end_date)
    except Exception as e:
        logger.error('[SALES] get_sales_data error: %s', e)
        return JsonResponse({'status': 'ok', 'data': [], 'count': 0})

    _raw_cache['key'] = f'{start_date}_{end_date}'
    _raw_cache['rows'] = raw_rows
    _raw_cache['columns'] = list(raw_rows[0].keys()) if raw_rows else []
    _raw_cache['start'] = start_date
    _raw_cache['end'] = end_date

    output = []
    for row in result.get('products', []):
        if type_filter and row.get('u_type') != type_filter:
            continue
        output.append({
            'u_type': row.get('u_type', ''),
            'u_sub_group': row.get('u_sub_group', ''),
            'month': row.get('month', ''),
            'year': row.get('year', ''),
            'litres': row.get('litres', 0),
            'linetotal': row.get('linetotal', 0),
        })

    output.sort(key=lambda x: (
        0 if x['u_type'] == 'PREMIUM' else 1,
        x['u_sub_group'],
        x['month'],
    ))
    return JsonResponse({'status': 'ok', 'data': output, 'count': len(output)})


@permission_flag_required('can_sales', json_response=True)
@require_http_methods(['POST'])
def api_drill_down(request):
    body = _parse_body(request)
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    u_type = body.get('u_type') or body.get('product_type', '')
    u_sub_group = body.get('u_sub_group') or body.get('sub_group', '')
    drill_by = body.get('drill_by', 'State')
    month = body.get('month')
    year = body.get('year')
    filters = body.get('filters') or {}

    type_filter = _get_type_filter(request)
    if type_filter and u_type and u_type.upper() != type_filter:
        return JsonResponse({'data': []})

    cache_key = f'{start_date}_{end_date}'
    if _raw_cache['key'] != cache_key:
        try:
            _, raw_rows = services.get_sales_data(start_date, end_date)
            _raw_cache['key'] = cache_key
            _raw_cache['rows'] = raw_rows
        except Exception as e:
            logger.error('[SALES DRILL] fetch failed: %s', e)
            return JsonResponse({'data': []})
    else:
        raw_rows = _raw_cache['rows']

    data = services.get_drill_down(
        start_date, end_date, raw_rows,
        u_type=u_type, u_sub_group=u_sub_group,
        drill_by=drill_by, month=month, year=year, filters=filters,
    )
    return JsonResponse({'data': data})


@permission_flag_required('can_sales', json_response=True)
@require_http_methods(['GET'])
def api_export_raw_csv(request):
    rows = _raw_cache.get('rows') or []
    columns = _raw_cache.get('columns') or []
    if not rows or not columns:
        return JsonResponse({'error': 'No data - click Fetch Data first'}, status=400)

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
    end = _raw_cache.get('end', 'to')
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


@permission_flag_required('can_sales', json_response=True)
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
        ('Layout', layout_rows or [['Sales Dashboard']]),
        ('Raw Data', _raw_sheet_rows(columns, rows)),
    ])
    filename = f'Sales_Export_{start}_{end}.xlsx'
    response = HttpResponse(
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
