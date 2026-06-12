from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve as serve_static

from core.views import PermissionLoginView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/',  PermissionLoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),

    path('', include('home.urls')),
    path('realise/', include('realise.urls')),
    path('sales/', include('sales.urls')),
    path('', include('dashboard.urls')),

    path('inventory/', include('inventory.urls')),
]

# Serve static files in development
if settings.DEBUG:
    urlpatterns += [
        re_path(r'^static/(?P<path>.*)$', serve_static, {
            'document_root': settings.STATIC_ROOT,
        }),
    ]
