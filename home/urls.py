from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='home'),
    path('users/', views.user_management, name='user_management'),
    path('api/users/save/', views.api_user_save, name='api_user_save'),
    path('api/users/delete/', views.api_user_delete, name='api_user_delete'),
]
