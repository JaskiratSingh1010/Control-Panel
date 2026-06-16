from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _seed_default_users(sender, **kwargs):
    """Create the default login users if they don't exist. Runs after migrations
    (via post_migrate) so it never queries the DB during app initialization and only
    runs when the schema is ready. Idempotent."""
    from django.contrib.auth import get_user_model

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
                User.objects.create_user(
                    username=username, password=password,
                    is_staff=is_staff, is_superuser=is_super,
                )
        except Exception:
            # Skip any per-user creation errors to avoid blocking migrations.
            continue


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        # Defer user seeding to post_migrate — querying the DB here in ready() runs on
        # every management command and triggers Django's app-init DB-access warning.
        post_migrate.connect(_seed_default_users, sender=self)
