# Jivo Group — Control Panel

A single, authenticated **Django** web application that consolidates every Jivo Group
business dashboard behind one shared shell (top bar + sidebar). Each dashboard is its
own Django app; almost all report data is read **live from SAP Business One (SAP HANA)**,
while operational data the business edits (targets, remarks, claims, credit locks,
per‑page permissions) lives in a local database.

> This folder is the project's living documentation.
> - **[architecture.md](architecture.md)** — how the system is put together (apps, request flow, SAP, caching, permissions, data model, frontend conventions).
> - **[apps-and-reports.md](apps-and-reports.md)** — every app and every report page: what it does, its URL, its permission, and its data source.

---

## What it is, in one paragraph

The Control Panel is a **read‑mostly analytics portal** over SAP B1. Users log in, and the
sidebar shows only the tabs they're allowed to see. Each tab is a report that runs a set of
HANA SQL queries (or stored procedures) against one of the Jivo company databases, aggregates
the result in Python, caches it briefly, and renders it as an interactive table / pivot / KPI
view. A few things are **written back** to a local SQLite database — monthly & channel sales
targets, customer‑aging remarks, the manual claims register, credit‑limit locks, and the
per‑page access grants managed from the built‑in User Management screen.

## Tech stack

| Layer | Choice |
|-------|--------|
| Framework | Django ≥ 5.2 (`config/` project package) |
| Report data source | SAP HANA (SAP Business One), via **`hdbcli`** — see [`core/sap_connector.py`](../core/sap_connector.py) |
| App database | SQLite (`db.sqlite3`) — targets, remarks, claims, locks, permission groups |
| Auth / access | Django `auth` users + **groups → `can_*` permission flags** (`core/context_processors.py`) |
| Frontend | Server‑rendered Django templates + **vanilla JS** (no SPA framework); shared light theme via CSS `--rz-*` tokens in `core/templates/core/base.html`; Material Icons |
| Excel export | `openpyxl` and a hand‑rolled writer (`core/simple_xlsx.py`) |
| Static files | WhiteNoise |
| Optional AI | Groq API (inventory "chat" helpers) — `GROQ_API_KEY` |

## Project layout

```
New_Control_Panel/
├── config/            Django project (settings.py, urls.py, wsgi/asgi)
├── core/              Shared infra: SAP connector, decorators, context processors,
│                      base.html shell + theme, simple_xlsx, custom login
├── home/              Landing page (P&L KPI cards) + User Management
├── dashboard/         Expenses, Salaries, COGS, home ticker
├── realise/           Realise sales analytics + ~10 report pages (the largest app)
├── inventory/         Stock, Non‑Inventory FG, Wellness–Mart Reconciliation,
│                      Production Plan, Daily Production, oils/beverages services
├── sales/             Standalone Sales dashboard
├── db.sqlite3         App database (NOT the report data — that's SAP)
├── requirements.txt
└── manage.py
```

App sizes are lopsided by design: `realise/services.py` (~5,200 LOC) and
`realise/views.py` (~1,600 LOC) hold the bulk of the reporting logic; `inventory/services/`
splits its logic into `oils.py`, `beverages.py`, `reconciliation.py`, `production.py`, etc.

## Running it locally

Prerequisites: **Python 3.10+** and network access to the SAP HANA host.

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt

# Configuration comes from environment variables (see "Configuration" below).
# Create a .env file or export them, then:
python manage.py migrate
python manage.py runserver 0.0.0.0:9080
```

Open <http://127.0.0.1:9080/> (or whatever host:port you bound). Log in with a seeded
account (below) or a superuser.

## Configuration (environment variables)

All settings read from the environment with fallbacks in [`config/settings.py`](../config/settings.py).
For any real deployment, **override the secrets** — do not rely on the checked‑in defaults.

| Variable | Purpose |
|----------|---------|
| `DJANGO_SECRET_KEY` | Django secret key |
| `DJANGO_DEBUG` | `True`/`False` |
| `DJANGO_ALLOWED_HOSTS` | comma‑separated hosts |
| `SAP_HANA_HOST` / `SAP_HANA_PORT` | SAP HANA endpoint (default `103.89.45.192:30015`) |
| `SAP_HANA_USER` / `SAP_HANA_PASSWORD` | SAP HANA credentials |
| `GROQ_API_KEY` | optional — enables the inventory AI chat helpers |

## Seeded accounts

Created by the data migration `realise/migrations/0002_seed_data.py`:

| Username | Password | Group | Notes |
|----------|----------|-------|-------|
| `admin` | `jivoadmin` | `realise_admin` | full Realise access, may edit targets |
| `premium` | `premium` | `realise_premium` | PREMIUM segment only |
| `commodity` | `commodity` | `realise_commodity` | COMMODITY segment only |

- **Edit‑targets PIN:** `gill`
- Create a superuser for full admin: `python manage.py createsuperuser`
- Grant individual report tabs from **User Management** (`/users/`, staff/superuser only) —
  see [apps-and-reports.md](apps-and-reports.md#access-and-user-management) and
  [architecture.md](architecture.md#5-access-control).

## Key conventions (read before contributing)

- **Report data is SAP; app data is SQLite.** Never write report data to SQLite; never expect
  SAP to hold app state (targets/remarks/claims/locks live in Django models).
- **Every report page is gated by a `can_*` flag** resolved from group membership in
  `core/context_processors.py`; views enforce it with `@permission_flag_required(...)`.
  Report tabs are opt‑in per page and are **not** auto‑granted by a broad role.
- **New report pages reuse the shared light theme** — style with the `--rz-*` CSS tokens
  defined in `core/templates/core/base.html`, don't reintroduce dark/gradient cards.
- **SAP calls are cached** (module‑level TTL caches, ~90 s, plus Django cache for KPIs) —
  the SAP round‑trip is the slow part, so aggregation results are memoized per date range.
- Company databases: **oil** = `JIVO_OIL_HANADB`, **beverages** = `JIVO_BEVERAGES_HANADB`,
  **mart** = `JIVO_MART_HANADB`, **wellness** via the oil connection. Many reports have an
  Oil / Beverages / Mart toggle that swaps the schema.
