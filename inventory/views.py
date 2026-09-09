import inspect
import json
import logging

from datetime import date

from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from core.decorators import permission_flag_required
from .services import beverages, oils, reconciliation

logger = logging.getLogger(__name__)

def _create_inventory_view(service_func):
    @permission_flag_required('can_inventory', json_response=True)
    @require_http_methods(['GET'])
    def _view(request):
        try:
            allowed = inspect.signature(service_func).parameters
            params = {}
            for key, value in request.GET.items():
                if key not in allowed:
                    continue
                params[key] = int(value) if key == 'days' and str(value).strip() else value
            result = service_func(**params)
            return JsonResponse({'data': result})
        except Exception:
            logger.exception('[inventory] API view failed: %s', service_func.__name__)
            return JsonResponse({'data': []})
    return _view


@permission_flag_required('can_inventory')
def dashboard(request):
    return render(request, 'inventory/dashboard.html', {'sidebar_active': 'inventory'})


@permission_flag_required('can_stock_available')
def stock_available(request):
    """Standalone, independently-shareable page (own permission, not can_inventory)."""
    return render(request, 'inventory/stock_available.html', {'sidebar_active': 'stock_available'})


@permission_flag_required('can_stock_available', json_response=True)
@require_http_methods(['GET'])
def stock_available_data(request):
    schema = request.GET.get('schema', 'jivo_oil')
    try:
        data = oils.get_stock_available(schema=schema)
    except Exception:
        logger.exception('[inventory] stock-available fetch failed')
        data = {'warehouses': [], 'products': [], 'items': []}
    return JsonResponse({'data': data})


@permission_flag_required('can_stock_available')
@require_http_methods(['GET'])
def stock_available_export(request):
    """Download the current Stock Available data as the Inventory Audit Report 'View' pivot
    (.xlsx), matching the SAP report's layout exactly."""
    from .services.stock_audit import build_view_xlsx
    schema = request.GET.get('schema', 'jivo_oil')
    try:
        content = build_view_xlsx(schema=schema)
    except Exception:
        logger.exception('[inventory] stock-available export failed')
        return HttpResponse('Could not build the export.', status=500)
    resp = HttpResponse(
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    fname = 'Inventory Audit Report %s.xlsx' % date.today().strftime('%d.%m.%Y')
    resp['Content-Disposition'] = 'attachment; filename="%s"' % fname
    return resp


@permission_flag_required('can_stock_available', json_response=True)
@require_http_methods(['POST'])
def stock_available_view_export(request):
    """Build an .xlsx from the client-supplied grouped table currently on screen (honours the
    Litres/Boxes/Pieces unit selection, SKU filter and per-warehouse column groups).
    Body: {filename, sheets:[{name, rows:[[cell, ...], ...]}]} — cell is a scalar (numbers →
    real numeric cells) or {value, colspan, bold, fill, color, align}."""
    from core.simple_xlsx import build_workbook
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        body = {}
    sheets_in = body.get('sheets') or []
    sheets = []
    for s in sheets_in:
        if isinstance(s, dict) and isinstance(s.get('rows'), list) and s['rows']:
            sheets.append((str(s.get('name') or 'Sheet'), s['rows']))
    if not sheets:
        return JsonResponse({'error': 'no rows to export'}, status=400)
    import re as _re
    content = build_workbook(sheets)
    fname = _re.sub(r'[^A-Za-z0-9._ -]', '_', str(body.get('filename') or 'Stock'))[:120]
    if not fname.lower().endswith('.xlsx'):
        fname += '.xlsx'
    resp = HttpResponse(
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename="%s"' % fname
    return resp


@permission_flag_required('can_non_inventory')
def non_inventory(request):
    """Standalone Finished-Goods 'Non-Inventory' report — in-stock FG as a non-moving aging
    view (production age, last billed, days idle) with an oils/beverages toggle. Own permission."""
    return render(request, 'inventory/non_inventory.html', {'sidebar_active': 'non_inventory'})


@permission_flag_required('can_non_inventory', json_response=True)
@require_http_methods(['GET'])
def non_inventory_data(request):
    schema = request.GET.get('schema', 'jivo_oil')
    try:
        data = oils.get_non_inventory(schema=schema)
    except Exception:
        logger.exception('[inventory] non-inventory fetch failed')
        data = {'items': [], 'unit': 'litres', 'schema': schema}
    return JsonResponse({'data': data})


@permission_flag_required('can_non_inventory', json_response=True)
@require_http_methods(['GET'])
def non_inventory_drill(request):
    """Warehouse breakdown behind one item's stock (name+code, qty, production date)."""
    schema = request.GET.get('schema', 'jivo_oil')
    item = request.GET.get('item', '')
    whs = request.GET.get('whs', '')
    try:
        rows = oils.get_non_inventory_drill(item=item, schema=schema, whs=whs)
    except Exception:
        logger.exception('[inventory] non-inventory drill failed')
        rows = []
    return JsonResponse({'data': rows})


def _valid_date(s):
    """Return 'YYYY-MM-DD' if s parses as such, else None (so the service uses its default)."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10]).strftime('%Y-%m-%d')
    except ValueError:
        return None


@permission_flag_required('can_reconciliation')
def reconciliation_page(request):
    """Standalone Jivo Wellness–Mart billing reconciliation tab (own permission)."""
    return render(request, 'inventory/reconciliation.html', {'sidebar_active': 'reconciliation'})


@permission_flag_required('can_reconciliation', json_response=True)
@require_http_methods(['GET'])
def reconciliation_data(request):
    date_from = _valid_date(request.GET.get('date_from'))
    date_to = _valid_date(request.GET.get('date_to'))
    schema = 'beverages' if request.GET.get('schema') == 'beverages' else 'oil'
    try:
        data = reconciliation.get_reconciliation(date_from=date_from, date_to=date_to, schema=schema)
    except Exception:
        logger.exception('[inventory] reconciliation fetch failed')
        data = {'summary': {}, 'chains': [],
                'date_from': date_from, 'date_to': date_to, 'tolerance': reconciliation.TOLERANCE}
    return JsonResponse({'data': data})


@permission_flag_required('can_reconciliation', json_response=True)
@require_http_methods(['GET'])
def reconciliation_ledgers(request):
    """Mart & Wellness BP ledgers (pivoted by ORIGIN) for the reconciliation 'Ledgers' tab."""
    date_from = _valid_date(request.GET.get('date_from'))
    date_to = _valid_date(request.GET.get('date_to'))
    schema = 'beverages' if request.GET.get('schema') == 'beverages' else 'oil'
    try:
        data = reconciliation.get_ledgers(date_from=date_from, date_to=date_to, schema=schema)
    except Exception:
        logger.exception('[inventory] reconciliation ledgers fetch failed')
        data = {'date_from': date_from, 'date_to': date_to, 'company': schema,
                'mart': {'rows': [], 'total': {}}, 'wellness': {'rows': [], 'total': {}},
                'error': 'Could not load ledgers.'}
    return JsonResponse({'data': data})


@permission_flag_required('can_reconciliation')
@require_http_methods(['GET'])
def reconciliation_export(request):
    """Download the reconciliation (broken chains) as .xlsx. ALWAYS combines both seller companies
    (Oil + Beverages) so a PO split across both reconciles in one sheet instead of showing a false
    spread; each SO/A-R doc number is tagged with its company. (The on-screen tab stays per-company
    via the toggle.) The schema is forced to 'both' regardless of the request, so a stale page that
    still sends ?schema=oil can't accidentally produce a single-company export."""
    from .services.reconciliation_export import build_reconciliation_xlsx
    date_from = _valid_date(request.GET.get('date_from'))
    date_to = _valid_date(request.GET.get('date_to'))
    only = request.GET.get('only', 'broken')
    try:
        content = build_reconciliation_xlsx(date_from=date_from, date_to=date_to, only=only, schema='both')
    except Exception:
        logger.exception('[inventory] reconciliation export failed')
        return HttpResponse('Could not build the export.', status=500)
    resp = HttpResponse(
        content,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    company = 'Oil+Beverages'
    fname = 'Wellness-Mart Reconciliation %s %s.xlsx' % (company, date.today().strftime('%d.%m.%Y'))
    resp['Content-Disposition'] = 'attachment; filename="%s"' % fname
    return resp


@permission_flag_required('can_production')
def production(request):
    """Production feasibility: enter an FG code + planned qty, see its BOM's RM/PM
    requirement vs OnHand stock and whether/how much can actually be made."""
    return render(request, 'inventory/production.html', {'sidebar_active': 'production'})


@permission_flag_required('can_production', json_response=True)
@require_http_methods(['GET'])
def production_feasibility_data(request):
    from .services.production import get_bom_feasibility
    fg_code = request.GET.get('fg_code', '')
    qty = request.GET.get('qty', '0')
    whs = [w.strip() for w in request.GET.get('warehouses', '').split(',') if w.strip()] or None
    try:
        data = get_bom_feasibility(fg_code, qty, warehouses=whs)
    except Exception:
        logger.exception('[inventory] production feasibility failed')
        return JsonResponse({'status': 'error', 'error': 'Could not read BOM from SAP.', 'data': None})
    return JsonResponse({'status': 'ok', 'data': data})


@permission_flag_required('can_production', json_response=True)
@require_http_methods(['GET'])
def production_fg_list(request):
    from .services.production import get_fg_list
    try:
        data = get_fg_list()
    except Exception:
        logger.exception('[inventory] FG list failed')
        return JsonResponse({'status': 'error', 'data': []})
    return JsonResponse({'status': 'ok', 'data': data})


@permission_flag_required('can_production', json_response=True)
@require_http_methods(['GET'])
def production_plan_data(request):
    from .services.production import get_plan_feasibility
    try:
        items = json.loads(request.GET.get('items', '[]'))
        if not isinstance(items, list):
            items = []
    except (ValueError, TypeError):
        items = []
    whs = [w.strip() for w in request.GET.get('warehouses', '').split(',') if w.strip()] or None
    try:
        data = get_plan_feasibility(items, warehouses=whs)
    except Exception:
        logger.exception('[inventory] plan feasibility failed')
        return JsonResponse({'status': 'error', 'error': 'Could not build the plan.', 'data': None})
    return JsonResponse({'status': 'ok', 'data': data})


@permission_flag_required('can_production', json_response=True)
@require_http_methods(['GET'])
def production_warehouses(request):
    from .services.production import get_warehouses
    try:
        data = get_warehouses()
    except Exception:
        logger.exception('[inventory] warehouses list failed')
        return JsonResponse({'status': 'error', 'data': []})
    return JsonResponse({'status': 'ok', 'data': data})


@permission_flag_required('can_daily_production')
def daily_production(request):
    """Daily Production Transaction: what's being produced each day. SAP standard work orders
    (OWOR, Type='S'), drillable by date / variety / item / warehouse / user, with planned vs
    completed quantity and completed Litres / Boxes."""
    return render(request, 'inventory/daily_production.html', {'sidebar_active': 'daily_production'})


@permission_flag_required('can_daily_production', json_response=True)
@require_http_methods(['GET'])
def daily_production_data(request):
    """Work-order rows for a ?start=YYYY-MM-DD&end=YYYY-MM-DD range (default: current month to
    date). The client pivots + filters by warehouse (default BH-PF)."""
    from datetime import datetime
    from .services.production import get_daily_production

    def _pd(value, default):
        try:
            return datetime.strptime(str(value or '').strip(), '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return default

    today = date.today()
    start = _pd(request.GET.get('start'), today.replace(day=1))
    end = _pd(request.GET.get('end'), today)
    try:
        data = get_daily_production(start, end)
    except Exception:
        logger.exception('[inventory] daily-production fetch failed')
        return JsonResponse({'status': 'error', 'error': 'Could not read production orders from SAP.',
                             'rows': [], 'warehouses': []})
    return JsonResponse({'status': 'ok', **data})


oils_api_kpi = _create_inventory_view(oils.get_kpi)
oils_api_categories = _create_inventory_view(oils.get_categories)
oils_api_out_of_stock = _create_inventory_view(oils.get_out_of_stock)
oils_api_warehouses = _create_inventory_view(oils.get_warehouses)
oils_api_warehouse_summary = _create_inventory_view(oils.get_warehouse_summary)
oils_api_warehouse_items = _create_inventory_view(oils.get_warehouse_items)
oils_api_warehouse_owners = _create_inventory_view(oils.get_warehouse_owners)
oils_api_stock_position = _create_inventory_view(oils.get_stock_position)
oils_api_movement = _create_inventory_view(oils.get_movement)
oils_api_movers_summary = _create_inventory_view(oils.get_movers_summary)
oils_api_movers_by_subgroup = _create_inventory_view(oils.get_movers_by_subgroup)
oils_api_movers = _create_inventory_view(oils.get_movers)
oils_api_not_billed_summary = _create_inventory_view(oils.get_not_billed_summary)
oils_api_not_billed_by_subgroup = _create_inventory_view(oils.get_not_billed_by_subgroup)
oils_api_not_billed = _create_inventory_view(oils.get_not_billed)
oils_api_abcxyz_summary = _create_inventory_view(oils.get_abcxyz_summary)
oils_api_abcxyz_by_subgroup = _create_inventory_view(oils.get_abcxyz_by_subgroup)
oils_api_abcxyz = _create_inventory_view(oils.get_abcxyz)
oils_api_aging = _create_inventory_view(oils.get_aging)
oils_api_aging_drill = _create_inventory_view(oils.get_aging_drill)
oils_api_trace_subgroups = _create_inventory_view(oils.get_trace_subgroups)
oils_api_trace_items = _create_inventory_view(oils.get_trace_items)
oils_api_trace_header = _create_inventory_view(oils.get_trace_header)
oils_api_trace_log = _create_inventory_view(oils.get_trace_log)
oils_api_trace_returns = _create_inventory_view(oils.get_trace_returns)
oils_api_trace_disassembly = _create_inventory_view(oils.get_trace_disassembly)
oils_api_planning = _create_inventory_view(oils.get_planning)

beverages_api_kpi = _create_inventory_view(beverages.get_kpi)
beverages_api_categories = _create_inventory_view(beverages.get_categories)
beverages_api_out_of_stock = _create_inventory_view(beverages.get_out_of_stock)
beverages_api_warehouses = _create_inventory_view(beverages.get_warehouses)
beverages_api_warehouse_summary = _create_inventory_view(beverages.get_warehouse_summary)
beverages_api_warehouse_items = _create_inventory_view(beverages.get_warehouse_items)
beverages_api_warehouse_owners = _create_inventory_view(beverages.get_warehouse_owners)
beverages_api_stock_position = _create_inventory_view(beverages.get_stock_position)
beverages_api_movement = _create_inventory_view(beverages.get_movement)
beverages_api_movers_summary = _create_inventory_view(beverages.get_movers_summary)
beverages_api_movers_by_subgroup = _create_inventory_view(beverages.get_movers_by_subgroup)
beverages_api_movers = _create_inventory_view(beverages.get_movers)
beverages_api_not_billed_summary = _create_inventory_view(beverages.get_not_billed_summary)
beverages_api_not_billed_by_subgroup = _create_inventory_view(beverages.get_not_billed_by_subgroup)
beverages_api_not_billed = _create_inventory_view(beverages.get_not_billed)
beverages_api_abcxyz_summary = _create_inventory_view(beverages.get_abcxyz_summary)
beverages_api_abcxyz_by_subgroup = _create_inventory_view(beverages.get_abcxyz_by_subgroup)
beverages_api_abcxyz = _create_inventory_view(beverages.get_abcxyz)
beverages_api_aging = _create_inventory_view(beverages.get_aging)
beverages_api_aging_drill = _create_inventory_view(beverages.get_aging_drill)
beverages_api_trace_subgroups = _create_inventory_view(beverages.get_trace_subgroups)
beverages_api_trace_items = _create_inventory_view(beverages.get_trace_items)
beverages_api_trace_header = _create_inventory_view(beverages.get_trace_header)
beverages_api_trace_log = _create_inventory_view(beverages.get_trace_log)
beverages_api_trace_returns = _create_inventory_view(beverages.get_trace_returns)
beverages_api_trace_disassembly = _create_inventory_view(beverages.get_trace_disassembly)
beverages_api_planning = _create_inventory_view(beverages.get_planning)
beverages_api_debug_rm_pm = _create_inventory_view(beverages.get_debug_rm_pm)
