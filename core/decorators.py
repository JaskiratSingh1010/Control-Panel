from functools import wraps
from django.http import JsonResponse
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required

from core.context_processors import build_user_permissions


def group_required(*group_names, json_response=False):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                if json_response:
                    return JsonResponse({'error': 'Authentication required'}, status=401)
                return redirect(f'/accounts/login/?next={request.path}')
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            user_groups = set(request.user.groups.values_list('name', flat=True))
            if user_groups.intersection(set(group_names)):
                return view_func(request, *args, **kwargs)
            if json_response:
                return JsonResponse({'error': 'Permission denied'}, status=403)
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden('You do not have access to this resource.')
        return _wrapped
    return decorator


def login_required_json(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        return view_func(request, *args, **kwargs)
    return _wrapped


def permission_flag_required(flag_name, json_response=False):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                if json_response:
                    return JsonResponse({'error': 'Authentication required'}, status=401)
                return redirect(f'/accounts/login/?next={request.path}')
            permissions = build_user_permissions(request.user)
            if permissions.get(flag_name):
                return view_func(request, *args, **kwargs)
            if json_response:
                return JsonResponse({'error': 'Permission denied'}, status=403)
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden('You do not have access to this resource.')
        return _wrapped
    return decorator
