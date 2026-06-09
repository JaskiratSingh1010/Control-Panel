from django.contrib.auth.views import LoginView
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from .context_processors import build_login_permission_payload


def serialize_user_permissions(user):
    return build_login_permission_payload(user)


class PermissionLoginView(LoginView):
    template_name = 'registration/login.html'

    def _wants_json(self):
        return (
            self.request.headers.get('x-requested-with') == 'XMLHttpRequest'
            or 'application/json' in self.request.headers.get('accept', '')
        )

    def form_valid(self, form):
        response = super().form_valid(form)
        self.request.session['group_permissions'] = serialize_user_permissions(self.request.user)

        if self._wants_json():
            return JsonResponse({
                'status': 'ok',
                'redirect_url': self.get_success_url(),
                'user': self.request.session['group_permissions'],
            })
        return response

    def form_invalid(self, form):
        if self._wants_json():
            return JsonResponse({
                'status': 'error',
                'errors': form.errors,
            }, status=400)
        return super().form_invalid(form)


@login_required
def coming_soon(request, tab, label):
    return render(request, 'core/coming_soon.html', {
        'tab': tab,
        'label': label,
        'sidebar_active': tab,
    })
