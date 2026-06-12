from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include, re_path
from django.conf import settings
from django.views.generic import RedirectView
from django.views.static import serve as serve_static

from core.views import PermissionLoginView

# Configure Django admin to use the control panel login instead of default admin login
admin.site.login_url = '/accounts/login/'
admin.site.index_title = 'Site Admin'
admin.site.site_title = 'Admin'
admin.site.site_header = 'Django Administration'

urlpatterns = [
    path('admin/login/', RedirectView.as_view(url='/accounts/login/', permanent=False), name='admin_login_redirect'),
    path('admin/logout/', auth_views.LogoutView.as_view(next_page='/accounts/login/'), name='admin_logout'),
    path('admin/', admin.site.urls),
    path('accounts/login/', PermissionLoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(next_page='/accounts/login/'), name='logout'),

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
