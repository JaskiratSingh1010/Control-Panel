# Jivo Group — New Control Panel

Multi-dashboard control panel. Currently live: **Realise Dashboard** (SAP HANA).
Stub tabs for Sales, Inventory, Expenses, Salaries — each replaced with a real app in subsequent passes.

## Prerequisites

- Python 3.10+
- Network access to SAP HANA host `103.89.45.192:30015`

## Setup

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # then fill in real SAP credentials
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/>.

## Default credentials

| Username    | Password    | Role      | Access            |
|-------------|-------------|-----------|-------------------|
| `admin`     | `jivoadmin` | Admin     | All types, can edit targets |
| `commodity` | `commodity` | Viewer    | COMMODITY locked  |
| `premium`   | `premium`   | Viewer    | PREMIUM locked    |

**Edit-targets PIN:** `gill`

## Adding a new user

1. Go to `/admin/` (log in with a superuser account).
2. Create a `User` record with the desired username/password.
3. Assign the user to one of: `realise_admin`, `realise_premium`, `realise_commodity`.
4. For superuser creation on a fresh DB: `python manage.py createsuperuser`.

## Sidebar nav (`core/templates/core/base.html`)

The sidebar is defined in `core/templates/core/base.html`. Each stub tab is a
single `path(...)` line in `config/urls.py` pointing at `core_views.coming_soon`.

### Converting a stub tab into a real app

1. Build the new Django app (e.g. `python manage.py startapp sales`).
2. Add `'sales.apps.SalesConfig'` to `INSTALLED_APPS` in `config/settings.py`.
3. In `config/urls.py`, replace:
   ```python
   path('sales/', core_views.coming_soon, {'tab': 'sales', 'label': 'Sales Dashboard'}, name='sales_stub'),
   ```
   with:
   ```python
   path('sales/', include('sales.urls')),
   ```
4. Remove the `name='sales_stub'` stub and update any `{% url 'sales_stub' %}` references
   in `base.html` to the new app's URL name.
