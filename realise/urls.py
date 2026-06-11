from django.urls import path
from . import views

app_name = 'realise'

urlpatterns = [
    path('',                         views.dashboard,              name='dashboard'),
    path('targets/',                 views.channel_targets_page,   name='channel_targets'),
    path('channel/<str:group>/',     views.channel_detail_placeholder, name='channel_detail'),
    path('api/health/',              views.api_health,             name='api_health'),
    path('api/sales-data/',          views.api_sales_data,         name='api_sales_data'),
    path('api/channel-targets/',     views.api_channel_targets,    name='api_channel_targets'),
    path('api/segment-targets/',     views.api_segment_targets,    name='api_segment_targets'),
    path('api/order-in-hand/',       views.api_order_in_hand,      name='api_order_in_hand'),
    path('api/order-in-hand-rows/',  views.api_order_in_hand_rows, name='api_order_in_hand_rows'),
    path('api/target-nodes/',        views.api_target_nodes,       name='api_target_nodes'),
    path('api/channel-detail-docs/', views.api_channel_detail_docs, name='api_channel_detail_docs'),
    path('api/commodity-oih-rows/',  views.api_commodity_oih_rows,  name='api_commodity_oih_rows'),
    path('api/oih-breakdown/',       views.api_oih_breakdown,       name='api_oih_breakdown'),
    path('api/drill-down/',          views.api_drill_down,         name='api_drill_down'),
    path('api/historical-realise/',  views.api_historical_realise, name='api_historical_realise'),
    path('api/targets/',             views.api_targets,            name='api_targets'),
    path('api/save-targets/',        views.api_save_targets,       name='api_save_targets'),
    path('api/verify-pin/',          views.api_verify_pin,         name='api_verify_pin'),
    path('api/export-raw-csv/',      views.api_export_raw_csv,     name='api_export_raw_csv'),
    path('api/export-excel/',        views.api_export_excel,       name='api_export_excel'),
]
