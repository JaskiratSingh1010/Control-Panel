from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('expenses/', views.expenses_dashboard, name='expenses'),
    path('salaries/', views.salary_dashboard, name='salaries'),
    path('api/expenses/', views.expenses_api, name='expenses_api'),
    path('api/expenses-detail/', views.expenses_detail_api, name='expenses_detail_api'),
    path('api/expenses-budgets/', views.expenses_budgets_api, name='expenses_budgets_api'),
    path('api/expenses-budgets-update/', views.expenses_budgets_update, name='expenses_budgets_update'),
    path('api/salary-detail/', views.salary_detail_api, name='salary_detail_api'),
    path('api/cogs/', views.cogs_api, name='cogs_api'),
    path('api/cogs-opt-update/', views.cogs_opt_update, name='cogs_opt_update'),
    path('api/ticker/', views.ticker_api, name='ticker_api'),
]
