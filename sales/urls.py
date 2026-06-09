from django.urls import path
from . import views

app_name = 'sales'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('api/health/', views.api_health, name='api_health'),
    path('api/sales-data/', views.api_sales_data, name='api_sales_data'),
    path('api/drill-down/', views.api_drill_down, name='api_drill_down'),
    path('api/export-raw-csv/', views.api_export_raw_csv, name='api_export_raw_csv'),
    path('api/export-excel/', views.api_export_excel, name='api_export_excel'),
]
