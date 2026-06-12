from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # Create a few default users if they do not exist yet. This helps
        # when starting the application for the first time so the login
        # screen can be used immediately. Wrap DB access to avoid errors
        # during migrations or when the database is not yet ready.
        try:
            from django.contrib.auth import get_user_model
            from django.db.utils import OperationalError, ProgrammingError

            User = get_user_model()
            defaults = [
                ('admin', 'jivoadmin', True, True),
                ('commodity', 'commodity', False, False),
                ('premium', 'premium', False, False),
                ('jivo', 'jivo1234', False, False),
            ]
            for username, password, is_staff, is_super in defaults:
                try:
                    if not User.objects.filter(username=username).exists():
                        User.objects.create_user(username=username, password=password, is_staff=is_staff, is_superuser=is_super)
                except Exception:
                    # Skip any per-user creation errors to avoid blocking startup
                    continue
        except (OperationalError, ProgrammingError):
            # Database isn't available yet (migrations, etc.) — skip user creation.
            pass
