from django.contrib.auth.views import LoginView
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import render
from django.contrib.auth import get_user_model, authenticate, login
from django.db.utils import OperationalError, ProgrammingError

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
        # If this was an AJAX/json request, return the form errors as JSON
        if self._wants_json():
            return JsonResponse({
                'status': 'error',
                'errors': form.errors,
            }, status=400)

        # For regular POST attempts: if the user does not exist, create them
        # using the supplied credentials and immediately log them in. This
        # behaviour ensures first-time users can be created from the login
        # screen (useful for quick local setups). Wrap DB access to avoid
        # errors during migrations or when database is not ready.
        try:
            if self.request.method == 'POST':
                username = self.request.POST.get('username')
                password = self.request.POST.get('password')
                if username and password:
                    User = get_user_model()
                    if not User.objects.filter(username=username).exists():
                        user = User.objects.create_user(username=username, password=password)
                        user = authenticate(self.request, username=username, password=password)
                        if user:
                            login(self.request, user)
                            self.request.session['group_permissions'] = serialize_user_permissions(user)
                            return HttpResponseRedirect(self.get_success_url())
        except (OperationalError, ProgrammingError):
            # Database not ready (migrations, etc.) — fall back to normal behaviour.
            pass

        return super().form_invalid(form)


@login_required
def coming_soon(request, tab, label):
    return render(request, 'core/coming_soon.html', {
        'tab': tab,
        'label': label,
        'sidebar_active': tab,
    })
