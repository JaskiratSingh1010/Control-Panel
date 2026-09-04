from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')


def env(key, default=''):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    val = os.environ.get(key, '').strip().lower()
    if not val:
        return default
    return val in ('1', 'true', 'yes', 'on')


def env_list(key, default=None, sep=','):
    val = os.environ.get(key, '')
    if not val:
        return default or []
    return [v.strip() for v in val.split(sep) if v.strip()]


SECRET_KEY = env('DJANGO_SECRET_KEY', 'django-insecure-fallback-key-CHANGE-ME')
DEBUG = env_bool('DJANGO_DEBUG', True)
ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', ['127.0.0.1', 'localhost', '138.252.101.118'])

# POST/AJAX from the live dashboard (non-standard port, plain http) must be a trusted CSRF origin
# on Django 4+, or form submits fail with a 403. Include scheme + host + port.
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS', ['http://138.252.101.118:9080'])

# ---------------------------------------------------------------------------
# POST body size
# ---------------------------------------------------------------------------
# Django's default cap is 2.5 MB, and reading request.body past it raises
# RequestDataTooBig. The Customer Aging "Export Excel" posts the whole on-screen
# invoice book as JSON, which passes 2.5 MB at roughly 6,000 invoices - so a big
# export was rejected after the browser had already spent seconds packing it, and
# the page silently fell back to a CSV. This is an internal, login-only panel, so
# a larger cap is fine.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(env('DJANGO_MAX_POST_MB', '64')) * 1024 * 1024

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core.apps.CoreConfig',
    'home.apps.HomeConfig',
    'realise.apps.RealiseConfig',
    'sales.apps.SalesConfig',
    'inventory.apps.InventoryConfig',
    'dashboard.apps.DashboardConfig',
]

MIDDLEWARE = [
    # Stopwatch on every request. Outermost so it measures everything below it.
    'core.middleware.RequestTimingMiddleware',
    # Compress JSON/HTML responses (~70-80% smaller) — must run first.
    'django.middleware.gzip.GZipMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.user_profile',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 4}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
# WhiteNoise was already installed but left on Django's plain storage, so every
# CSS/JS/font file was re-downloaded on every visit and sent uncompressed.
#
# CompressedManifestStaticFilesStorage does two things at collectstatic time:
#   * writes a .gz and .br copy of each text file, so the browser downloads a
#     much smaller file;
#   * renames each file with a hash of its contents (app.a1b2c3.css), which
#     lets WhiteNoise mark it cacheable for a year. The name changes whenever
#     the file changes, so a stale copy can never be served.
#
# Remember: after changing any static file you must run
#     python manage.py collectstatic
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
# Without this block Django uses per-process memory, which has three problems:
#   * every worker process keeps its own copy, so the same SAP pull happens
#     once per worker instead of once in total;
#   * a restart throws everything away, so the next visitor waits again;
#   * it only holds 300 entries before it starts evicting.
#
# Default below is a file-backed cache: no extra software to install, shared by
# every worker, and it survives a restart. On the Linux server set
# DJANGO_CACHE_URL=redis://127.0.0.1:6379/1 to switch to Redis, which is
# faster still. Nothing else in the code needs to change.
CACHE_URL = env('DJANGO_CACHE_URL', '')

if CACHE_URL.startswith('redis'):
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': CACHE_URL,
            'KEY_PREFIX': 'cp',
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
            'LOCATION': str(BASE_DIR / '.django-cache'),
            'KEY_PREFIX': 'cp',
            'TIMEOUT': 300,
            'OPTIONS': {
                # Plenty for our handful of KPI keys; stops silent eviction.
                'MAX_ENTRIES': 5000,
                'CULL_FREQUENCY': 4,
            },
        }
    }

# How long the shared KPI cache holds a SAP answer (see core/kpi_cache.py).
# The current month keeps moving so it gets a short window; a finished month
# barely changes, so it can be held much longer.
KPI_CACHE_TTL_CURRENT_MONTH = int(env('KPI_TTL_CURRENT', '300'))
KPI_CACHE_TTL_PAST_MONTH = int(env('KPI_TTL_PAST', '1800'))
# After the fresh window a stale answer is still served instantly while a new
# one is fetched in the background, for up to this long.
KPI_CACHE_STALE_FOR = int(env('KPI_STALE_FOR', '3600'))

# Django Admin Settings
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

SAP_HANA = {
    'HOST': env('SAP_HANA_HOST', '103.89.45.192'),
    'PORT': int(env('SAP_HANA_PORT', '30015')),
    'USER': env('SAP_HANA_USER', 'DATA1'),
    'PASSWORD': env('SAP_HANA_PASSWORD', 'Jivo@1989'),
}

GROQ_API_KEY = env('GROQ_API_KEY', '')

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Django's built-in logging only prints error tracebacks to the console when
# DEBUG is True. With DJANGO_DEBUG=False (how we run this) a crash showed up as
# a bare
#     "GET /accounts/login/ HTTP/1.1" 500 145
# and nothing else, so there was no way to tell what actually broke.
#
# This block sends every request error to the console with its full traceback,
# whether DEBUG is on or off. It changes nothing a visitor sees - the browser
# still gets the plain "Server Error (500)" page.
# Requests slower than this get written to the slow-request log. Lower it to
# 0 while hunting a problem (logs every request), raise it to keep noise down.
SLOW_REQUEST_SECONDS = float(env('SLOW_REQUEST_SECONDS', '1.0'))

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'plain': {
            'format': '{levelname} {asctime} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'plain',
        },
        # Slow requests go to their own file so they are easy to read and do
        # not drown in everything else. Caps at 5 x 2 MB, then overwrites the
        # oldest - it can never fill the disk.
        'slowfile': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(BASE_DIR / 'logs' / 'slow-requests.log'),
            'maxBytes': 2 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'plain',
            'encoding': 'utf-8',
        },
    },
    'loggers': {
        # The traceback of any unhandled exception in a view/template.
        'django.request': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
        },
        # The stopwatch (core/middleware.py).
        'core.timing': {
            'handlers': ['console', 'slowfile'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
