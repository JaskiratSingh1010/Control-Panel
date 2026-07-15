# Architecture

This document explains how the Control Panel is put together: the request lifecycle,
the shared `core/` layer, SAP integration and caching, the access‑control model, the
persisted data model, and the frontend conventions.

---

## 1. The shell + apps model

Everything renders inside one shared shell — the top bar (brand, live ticker, clock) and the
left sidebar — defined in [`core/templates/core/base.html`](../core/templates/core/base.html).
Every page `{% extends "core/base.html" %}` and fills `content` / `extra_styles` /
`extra_scripts` blocks. The sidebar shows a tab only when the viewer holds the matching
`can_*` flag (see [Access control](#5-access-control)).

URL routing is centralized in [`config/urls.py`](../config/urls.py), which mounts each app:

| Mount | App | Purpose |
|-------|-----|---------|
| `/` | `home` | landing dashboard + User Management |
| `/` (also) | `dashboard` | Expenses / Salaries / COGS / ticker APIs |
| `/realise/` | `realise` | sales analytics + report suite |
| `/inventory/` | `inventory` | stock, reconciliation, production |
| `/sales/` | `sales` | standalone Sales dashboard |
| `/accounts/login` · `/admin/` | Django auth + admin | admin login is redirected to the panel's own login |

## 2. Request lifecycle of a report

A typical report page does this:

1. **View** (e.g. `realise/views.py`) is protected by `@permission_flag_required('can_x')`
   or `@group_required(...)`. It renders an HTML template that contains the page chrome and
   an empty table/pivot plus embedded JS.
2. The page's **JS** calls a JSON **API view** (e.g. `/realise/api/customer-aging-mart/`)
   with query params (date range, company toggle, filters).
3. The API view calls a **service function** (`realise/services.py`, `inventory/services/*`,
   `home/services.py`, `dashboard/*_service.py`).
4. The service builds **HANA SQL** (or calls a stored procedure), runs it through
   `core.sap_connector.execute_query`, aggregates the rows in Python into a payload, and
   returns it. Results are **cached** per date range/company for a short TTL.
5. The JS renders the payload into an interactive table / pivot / KPI grid, and offers
   **Excel/CSV export** (client‑side CSV, or POST to `api_export_xlsx` for a styled workbook).

Some pages (e.g. the main Realise dashboard, Customer Aging for oil) render the first payload
server‑side into the template and only fetch on subsequent interactions.

## 3. The `core/` shared layer

| File | Responsibility |
|------|----------------|
| [`sap_connector.py`](../core/sap_connector.py) | Opens `hdbcli` connections to SAP HANA using `settings.SAP_HANA`; `execute_query(sql)` returns a list of dict rows. All report SQL goes through here. |
| [`context_processors.py`](../core/context_processors.py) | `build_user_permissions(user)` → the `can_*` flag dict; `user_profile(request)` injects flags, groups, the period selector and the live ticker into every template. This is the single source of truth for "who can see what." |
| [`decorators.py`](../core/decorators.py) | `group_required(*groups)`, `permission_flag_required(flag)`, `any_permission_flag(*flags)`, `login_required_json` — view guards that return 403 JSON or HTML depending on the endpoint. |
| [`views.py`](../core/views.py) | `PermissionLoginView` (custom login that bakes the permission payload into the session) and `coming_soon` (stub tab). |
| [`simple_xlsx.py`](../core/simple_xlsx.py) | A dependency‑light `.xlsx` writer used by the shared `api_export_xlsx` endpoint to produce colour‑matched workbooks (header fills, number formats, tinted cells). |
| [`base.html`](../core/templates/core/base.html) | The shell + the shared **`--rz-*` light theme tokens** (card, border, ink, muted, head‑bg, accents, …). |

## 4. SAP integration & caching

- **Driver:** `hdbcli` (SAP's official Python client). Connection params come from
  `settings.SAP_HANA` (`HOST/PORT/USER/PASSWORD`), overridable via env vars.
- **Company databases (schemas):**
  - `JIVO_OIL_HANADB` — Jivo Oil (the primary/default company)
  - `JIVO_BEVERAGES_HANADB` — Jivo Beverages
  - `JIVO_MART_HANADB` — Jivo Mart
  - Jivo Wellness data is reached through the oil connection (used by the reconciliation report)
- **Toggles:** many reports expose an **Oil / Beverages / Mart** switch that simply swaps the
  schema string in the query and re‑fetches.
- **Caching strategy** (SAP is the slow part, so nearly everything is memoized):
  - Module‑level TTL dicts keyed by `(date range[, company])`, typically **~90 seconds**
    (e.g. `_AGING_TTL`, the sales‑proc cache, the channel‑aggregation cache in
    `realise/services.py`).
  - Django's cache framework for the home KPIs (`home_kpis_<year>_<month>`, ~3 min) and the
    ticker.
  - Aggregations that are pure functions of already‑cached raw rows (e.g. channel buckets,
    month buckets) are memoized separately so repeat loads skip the rework.
- **Data hygiene rules encoded in queries** (important, easy to get wrong):
  - Volume uses SAP's `Liter` column, **not** `Quantity`.
  - Hidden documents: `OINV/ORIN.U_ARNO IN ('T','H')` are excluded from "Done"/invoice
    queries (the Hidden Customer Sales report is the deliberate exception that surfaces `'H'`).
  - Ship‑to state/city come from the order's `CRD1` address, not the BP master HQ.

## 5. Access control

Access is **group‑driven** and resolved into boolean flags. There are three tiers:

1. **Realise role groups** (`realise_admin`, `realise_premium`, `realise_commodity`) — grant the
   Realise sales dashboard and set the segment filter (`derive_realise_profile`). `realise_admin`
   additionally may edit targets.
2. **Per‑page viewer groups** — one dedicated group per standalone report, e.g.
   `customer_aging_viewer`, `oih_vs_stock_viewer`, `compare_sales_viewer`, `sales_cn_viewer`,
   `hidden_sales_viewer`, `customer_master_viewer`, `sales_flow_viewer`, `claims_viewer`,
   `required_credit_viewer`, `reconciliation_viewer`, `stock_viewer`, `non_inventory_viewer`,
   `production_viewer`, `daily_production_viewer`. **These report pages are opt‑in per page and
   are NOT auto‑granted by a Realise role** — the per‑page toggle in User Management is
   authoritative.
3. **Module groups** — whole‑module read access (`inventory_viewer/admin`, `sales_viewer`,
   `expenses_viewer`, `salaries_viewer`, `cogs_viewer`).

`build_user_permissions(user)` in `core/context_processors.py` turns group membership into the
`can_*` dict (`can_realise`, `can_customer_aging`, `can_reconciliation`, `can_stock_available`,
`can_expenses`, `can_cogs`, …). Superusers get everything. Views enforce a flag with
`@permission_flag_required('can_x')`; the sidebar shows a tab with `{% if can_x %}`.

**User Management** (`home/views.py`, `/users/`, staff/superuser only) is the admin UI for all
of this: it lists users and exposes each viewer/module group as a checkbox plus the Realise
role as a dropdown. The catalog of toggles is the `PAGE_PERMS` / `MODULE_PERMS` lists in
`home/views.py`; adding a new report's permission is a one‑line entry there. Groups are created
on demand (`get_or_create`) when first assigned.

> To add a brand‑new gated report: (1) add a `can_x` flag + its viewer group in
> `build_user_permissions`, (2) guard the view with `@permission_flag_required('can_x')`,
> (3) add the sidebar `{% if can_x %}` entry in `base.html`, (4) add the toggle to `PAGE_PERMS`
> in `home/views.py`.

## 6. Persisted data model (SQLite)

Only the data the business *edits* is stored locally. Report figures are never persisted.

**`realise/models.py`** (the bulk of app state):

| Model | What it holds |
|-------|---------------|
| `MonthlyTarget` | per‑product monthly volume targets (COMMODITY segment) |
| `TargetNode` | per‑segment target tree nodes for the Realise targets page |
| `SegmentTarget` / `FlexTarget` / `TargetMaster` / `TerritoryProductTarget` | various target definitions used by the targets/territory features |
| `TerritoryMapping` / `CityOwner` | sales‑person ↔ territory / city ownership map (drives channel whitelisting) |
| `MainGroupMaster` / `StateMaster` | reference lookups |
| `AgingRemark` / `AgingRemarkLine` | per‑document remarks & split lines for Customer Aging (namespaced by prefix: oil journal `TransId:Line_ID`, and `BEVDOC:`/`OILDOC:`/`MART:` for the raw‑invoice views) |
| `AgingDueConfig` | per‑customer NOT‑DUE grace‑days override |
| `Claim` | the fully manual Claims register (not from SAP) |
| `ClosingRemark` | remarks on the Required Credit Limit report |
| `CreditLock` / `CreditLockSnapshot` | credit‑limit locks + their snapshots |

**`dashboard/models.py`** — `ExpenseBudget` (per‑category expense budgets).
**`inventory/models.py`** — `InventoryPermission` (defines the `view_inventory`/`manage_inventory` permissions).

## 7. Frontend conventions

- **Server‑rendered templates + vanilla JS.** No React/Vue. Each report's interactivity is a
  self‑contained IIFE in the template's `extra_scripts` block that fetches JSON and builds the
  DOM. Data handed to JS is passed via `{{ payload|json_script:"id" }}`.
- **Shared light theme.** Use the `--rz-*` tokens (white cards, thin accent bars, light table
  headers, soft‑indigo active states). The Home KPI cards, Customer Aging, and the Wellness–Mart
  Reconciliation tab all follow this system; new pages should too.
- **Tables are sortable & exportable.** Standing rule: data/pivot tables get click‑to‑sort
  headers (▲/▼) and an Excel/CSV export. Styled workbooks go through `api_export_xlsx` +
  `core/simple_xlsx.py`; quick dumps build CSV client‑side (with a UTF‑8 BOM).
- **Items are shown as `CODE — NAME`** where item pickers/labels appear, combined at the data
  layer so drill‑to‑document filters still match.

## 8. Deployment notes

- WSGI entrypoint: `config/wsgi.py`. Static served by WhiteNoise (`collectstatic` →
  `staticfiles/`).
- The app is **read‑mostly and cache‑heavy**; a single process is usually fine, but the
  module‑level caches are per‑process, so multiple workers each warm their own cache.
- Timezone is `Asia/Kolkata`, `USE_TZ = True`.
- Set real `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`, `DJANGO_ALLOWED_HOSTS`, and the
  `SAP_HANA_*` secrets via environment for production.
