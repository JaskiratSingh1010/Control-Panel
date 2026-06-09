import inspect
import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from core.decorators import permission_flag_required
from .services import beverages, oils
from .services.chat import chat

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


@permission_flag_required('inventory_can_edit', json_response=True)
@require_http_methods(['POST'])
def oils_api_chat(request):
    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        body = {}
    return JsonResponse(chat('oils', body.get('message', ''), body.get('context', '')))


@permission_flag_required('inventory_can_edit', json_response=True)
@require_http_methods(['POST'])
def beverages_api_chat(request):
    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        body = {}
    return JsonResponse(chat('beverages', body.get('message', ''), body.get('context', '')))


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
