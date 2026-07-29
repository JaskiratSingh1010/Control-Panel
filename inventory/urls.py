from django.urls import path

from . import views

app_name = 'inventory'

_ENDPOINTS = [
    ('kpi', 'kpi'),
    ('categories', 'categories'),
    ('out-of-stock', 'out_of_stock'),
    ('warehouses', 'warehouses'),
    ('warehouse-summary', 'warehouse_summary'),
    ('warehouse-items', 'warehouse_items'),
    ('warehouse-owners', 'warehouse_owners'),
    ('stock-position', 'stock_position'),
    ('movement', 'movement'),
    ('movers-summary', 'movers_summary'),
    ('movers-by-subgroup', 'movers_by_subgroup'),
    ('movers', 'movers'),
    ('not-billed-summary', 'not_billed_summary'),
    ('not-billed-by-subgroup', 'not_billed_by_subgroup'),
    ('not-billed', 'not_billed'),
    ('abcxyz-summary', 'abcxyz_summary'),
    ('abcxyz-by-subgroup', 'abcxyz_by_subgroup'),
    ('abcxyz', 'abcxyz'),
    ('aging', 'aging'),
    ('aging-drill', 'aging_drill'),
    ('trace-subgroups', 'trace_subgroups'),
    ('trace-items', 'trace_items'),
    ('trace-header', 'trace_header'),
    ('trace-log', 'trace_log'),
    ('trace-returns', 'trace_returns'),
    ('trace-disassembly', 'trace_disassembly'),
    ('planning', 'planning'),
]

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('stock-available/', views.stock_available, name='stock_available'),
    path('stock-available/api/data/', views.stock_available_data, name='stock_available_data'),
    path('stock-available/export/', views.stock_available_export, name='stock_available_export'),
    path('stock-available/view-export/', views.stock_available_view_export, name='stock_available_view_export'),
    path('non-inventory/', views.non_inventory, name='non_inventory'),
    path('non-inventory/api/data/', views.non_inventory_data, name='non_inventory_data'),
    path('non-inventory/api/drill/', views.non_inventory_drill, name='non_inventory_drill'),
    path('reconciliation/', views.reconciliation_page, name='reconciliation'),
    path('reconciliation/api/data/', views.reconciliation_data, name='reconciliation_data'),
    path('reconciliation/api/ledgers/', views.reconciliation_ledgers, name='reconciliation_ledgers'),
    path('reconciliation/export/', views.reconciliation_export, name='reconciliation_export'),
    path('production/', views.production, name='production'),
    path('production/api/feasibility/', views.production_feasibility_data, name='production_feasibility_data'),
    path('production/api/fg-list/', views.production_fg_list, name='production_fg_list'),
    path('production/api/plan/', views.production_plan_data, name='production_plan_data'),
    path('production/api/warehouses/', views.production_warehouses, name='production_warehouses'),
    path('daily-production/', views.daily_production, name='daily_production'),
    path('daily-production/api/data/', views.daily_production_data, name='daily_production_data'),
]

for path_name, view_name in _ENDPOINTS:
    urlpatterns.append(path(f'oils/api/{path_name}/', getattr(views, f'oils_api_{view_name}'), name=f'oils_api_{view_name}'))
urlpatterns.append(path('oils/api/chat/', views.oils_api_chat, name='oils_api_chat'))

for path_name, view_name in _ENDPOINTS:
    urlpatterns.append(path(f'beverages/api/{path_name}/', getattr(views, f'beverages_api_{view_name}'), name=f'beverages_api_{view_name}'))
urlpatterns += [
    path('beverages/api/chat/', views.beverages_api_chat, name='beverages_api_chat'),
    path('beverages/api/debug/rm-pm/', views.beverages_api_debug_rm_pm, name='beverages_api_debug_rm_pm'),
]
