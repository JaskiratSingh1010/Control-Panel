# Jivo Group — Control Panel

A single, authenticated **Django** application that consolidates every Jivo Group business
dashboard behind one shared shell (top bar + sidebar). Each dashboard is its own Django app;
almost all report data is read **live from SAP Business One (SAP HANA)**, while the data the
business edits (targets, remarks, claims, credit locks, per‑page permissions) lives in a local
SQLite database.

## 📚 Documentation

Full documentation lives in **[`docs/`](docs/)**:

- **[docs/README.md](docs/README.md)** — overview, tech stack, setup, configuration, seeded accounts, conventions.
- **[docs/architecture.md](docs/architecture.md)** — apps model, request lifecycle, SAP integration & caching, access control, data model, frontend conventions, deployment.
- **[docs/apps-and-reports.md](docs/apps-and-reports.md)** — every app and every report page: route, permission, and data source.

## Quick start

```bash
python -m venv venv
venv\Scripts\activate            # Windows  (source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:9080
```

Then open <http://127.0.0.1:9080/>. Configure secrets via environment variables
(`DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `SAP_HANA_HOST/PORT/USER/PASSWORD`, …) — see
[docs/README.md#configuration](docs/README.md#configuration-environment-variables).

## Seeded accounts

| Username | Password | Access |
|----------|----------|--------|
| `admin` | `jivoadmin` | Realise admin (full, may edit targets — PIN `gill`) |
| `premium` | `premium` | Realise viewer, PREMIUM segment |
| `commodity` | `commodity` | Realise viewer, COMMODITY segment |

Grant individual report tabs from **User Management** (`/users/`, staff/superuser only).
Create a superuser with `python manage.py createsuperuser`.

## Apps at a glance

| App | Mount | What's inside |
|-----|-------|---------------|
| `core` | — | shared: SAP connector, permissions, base shell + theme, xlsx writer, login |
| `home` | `/` | P&L KPI landing page + User Management |
| `dashboard` | `/` | Expenses, Salaries, COGS, home ticker |
| `realise` | `/realise/` | Realise sales analytics + ~10 report pages (Customer Aging, OIH vs Stock, Compare Sales, Sales vs CN, Hidden Sales, Customer Master, Sales Flow, Claims, Required Credit Limit, Targets) |
| `inventory` | `/inventory/` | Stock Available, Non‑Inventory FG, Wellness–Mart Reconciliation, Production Plan, Daily Production |
| `sales` | `/sales/` | standalone Sales dashboard |

## Stack

Django ≥ 5.2 · SAP HANA via `hdbcli` · SQLite (app data) · server‑rendered templates + vanilla
JS · shared `--rz-*` light theme · `openpyxl` / `core/simple_xlsx.py` exports · WhiteNoise ·
optional Groq API for inventory chat. See [docs/](docs/) for details.
