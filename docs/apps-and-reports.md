# Apps & Reports

A tour of every app and every report page: what it does, its route, the permission that gates
it, and its data source. Routes are relative to each app's mount (see
[architecture.md](architecture.md#1-the-shell--apps-model)).

Legend — **Source:** `SAP:<schema>` = live SAP HANA query; `DB` = local SQLite (editable);
`derived` = computed from other services.

---

## `core/` — shared infrastructure

No user‑facing report pages. Provides the SAP connector, permission machinery, the base shell
+ theme, the xlsx writer, the custom login (`PermissionLoginView`), and the `coming_soon` stub.
See [architecture.md](architecture.md#3-the-core-shared-layer).

---

## `home/` — landing page & user administration

### P&L Metrics dashboard — `/`  ·  *login required*
The control‑panel home. Renders **KPI cards** whose data is assembled in `home/services.py`
(pulling from the realise, dashboard, and inventory services) and cached ~3 min:

| Card | Gate | Source |
|------|------|--------|
| Total Sales Volume | `can_sales` | derived (realise sales) |
| Avg. Realisation | `can_realise` | derived |
| Cost of Goods Sold (OTP‑gated) | `can_cogs` | derived (COGS service) |
| Operating Expenses | `can_expenses` | derived (expenses service) |
| Salary Expenditure | `can_salaries` | derived |
| Inventory Value (Balance Sheet) | `can_inventory` | derived (inventory) |

Each card shows the current value, a **"vs last month"** comparison, and a **trend pill coloured
by meaning** (green = a good move, red = a bad move — revenue up is good, cost up is bad; driven
by `_KPI_HIGHER_IS_BETTER` in `home/views.py`). The COGS card stays locked behind an inline OTP
until unlocked. Cards use the shared light theme.

### User Management — `/users/`  ·  *staff/superuser only*
Self‑service admin to create/edit users and assign access **without the Django admin**. Each
report's viewer group and each module is a checkbox; the Realise role is a dropdown. Backed by
`api_user_save` / `api_user_delete`. The toggle catalog is `PAGE_PERMS` / `MODULE_PERMS` in
`home/views.py`. See [architecture.md](architecture.md#5-access-control).

---

## `dashboard/` — Expenses, Salaries, COGS, ticker

Financial dashboards and the shared home‑ticker/COGS APIs (`dashboard/views.py`,
`expenses_service.py`, `cogs_service.py`).

| Page / API | Route | Gate | Source |
|------------|-------|------|--------|
| Expenses dashboard | `/expenses/` | `can_expenses` | SAP + `ExpenseBudget` (DB) |
| Salary dashboard | `/salaries/` | `can_salaries` | SAP |
| Expenses API + detail + budgets get/update | `/api/expenses*` | `can_expenses` | SAP + DB |
| Salary detail | `/api/salary-detail/` | `can_salaries` | SAP |
| COGS + OTP update | `/api/cogs/`, `/api/cogs-opt-update/` | `can_cogs` | SAP (OTP‑gated) |
| Ticker | `/api/ticker/` | — | derived |

Expense budgets are editable and stored in `ExpenseBudget`; COGS is protected by an OTP option
that must be supplied to reveal the cost figures.

---

## `realise/` — Realise sales analytics + report suite

The largest app. The main dashboard is gated by the **Realise role groups**; each standalone
report has its **own per‑page viewer group** (opt‑in, not granted by the role).

### Realise Dashboard — `/realise/`  ·  *`realise_admin` / `realise_premium` / `realise_commodity`*
The flagship sales‑analysis view: channel performance, **Order‑in‑Hand (OIH)**, sales pulse,
targets, and the territory map. Data comes from SAP's `REPORT_SALES_ANALYSIS` procedure
(`JIVO_OIL_HANADB`) plus a large family of APIs (`api/sales-data`, `api/order-in-hand`,
`api/channel-targets`, `api/segment-targets`, `api/territory-map`, `api/drill-down`,
`api/historical-realise`, beverages equivalents, …). Volume is measured in **litres**
(`Liter` column). Channel cards/modals honour a state whitelist derived from the territory map;
"Done" state names must match the whitelist exactly. Targets are read/written via `TargetNode`,
`MonthlyTarget`, `SegmentTarget`, `FlexTarget`. Editing targets requires `realise_admin` (PIN
`gill`).

> Note: two target systems coexist after a branch merge — the older inline target editor and the
> newer person‑targets page (`/realise/targets/`) — and are not wired together.

### The standalone report pages

| Report | Route | Gate (viewer group) | What it does · Source |
|--------|-------|---------------------|------------------------|
| **OIH vs Stock** | `/realise/oih-vs-stock/` | `oih_vs_stock_viewer` | Open‑order Order‑in‑Hand vs available finished stock; Oil/Beverages toggle (beverages = boxes / families / variety). `SAP:oil/bev` |
| **Compare Sales** | `/realise/compare-sales/` | `compare_sales_viewer` | Period‑over‑period sales comparison with drill‑down. `SAP:oil` |
| **Sales vs Credit Notes** | `/realise/sales-cn/` | `sales_cn_viewer` | Gross `OINV` vs `ORIN`, split into CN‑for‑Goods (DocType I) / Claim‑for‑Services (DocType S); Net = Sales − CN; oil (litres) + beverages (boxes) toggle. `SAP:oil/bev` |
| **Hidden Customer Sales** | `/realise/hidden-sales/` | `hidden_sales_viewer` | Audit view of `OINV` lines flagged `U_ARNO='H'` (hidden, excluded from "Done"); per‑line drill Customer › Item. `SAP:oil` |
| **Customer Master** | `/realise/customer-master/` | `customer_master_viewer` | Clean `OCRD` customer listing (contact, GSTIN via `CRD1` bill‑to, PAN, address, terms, credit, balance, status); searchable table + Excel export. `SAP:oil` |
| **Sales Document Flow** | `/realise/sales-flow/` | `sales_flow_viewer` | Per‑party Quotation → Order → Invoice chain for a day's sales (traced via SAP base refs `INV1/DLN1/RDR1 BaseType`) + litres. `SAP:oil` |
| **Claims** | `/realise/claims/` | `claims_viewer` | **Fully manual** claim register (CRUD via `Claim` model). SAP only feeds the party/product/item pickers. Filters Month/date/Types, drill by Customer/Product/Item/Main Group. `DB` + `SAP` pickers |
| **Customer Aging** | `/realise/customer-aging/` | `customer_aging_viewer` | See detailed section below. `SAP:oil/bev/mart` + `DB` remarks |
| **Required Credit Limit** | `/realise/required-credit-limit/` | `required_credit_viewer` | Required credit limits from open SOs + unpaid parties with no OIH ("No Open Order" section); ledger / outstanding / payment columns; credit‑lock workflow (`CreditLock`), closing remarks (`ClosingRemark`), Excel export. `SAP:oil` + `DB` |
| **Targets** | `/realise/targets/` | Realise role | Person/channel target editing (`TargetNode`), plus an embeddable variant and a legacy channel‑targets page. `DB` |

Shared export plumbing lives here too: `api/export-xlsx` (styled workbook via
`core/simple_xlsx.py`) and `api/export-aging-detail` (whole‑book aging detail).

### Customer Aging — the deepest report

`/realise/customer-aging/` (pivot) and `/customer-aging/detail/` (per‑document drill). It ties
to SAP B1's own Customer Receivables Aging using the JDT1/ITR1/OITR reconciliation logic, and
supports three companies via a toggle:

- **Oil** — B1 reconciliation pivot Format → Customers, rendered server‑side (`get_customer_aging`).
- **Beverages** — open A/R invoices (`api/customer-aging-beverages`), pivoted client‑side by
  Sales Person → Customer with a per‑day multi‑select and an Excel‑like **RAW DATA** workspace
  (sortable/filterable table + customizable pivot + per‑cell remark editing + bulk remark upload
  + CSV export).
- **Mart** — same B1 engine against `JIVO_MART_HANADB` (`api/customer-aging-mart`); additionally
  **splits each format into B2B (has a GSTIN) / B2C** sub‑sections with subtotals, plus a
  Both/B2B/B2C filter (the GSTIN comes from a `CRD1` lookup tagged server‑side).
- **Oil RAW DATA** — the Beverages‑style open‑invoice workspace is also available for Oil
  (`api/customer-aging-oil-ar`), loaded on demand without changing oil's main B1 pivot.

Other behaviour: a remarks multi‑select filter and per‑document remarks (`AgingRemark` /
`AgingRemarkLine`, namespaced by prefix per company), a NOT‑DUE grace‑days override
(`AgingDueConfig`), Balance‑Due sign / numeric condition filters, and **filter‑aware exports**
(Excel/CSV that re‑sum group/segment/grand totals from only the filtered rows). The pivot export
collapses redundant single‑child levels at Doc‑No grain (each invoice once), blanks
Balance Due/Original Outstanding/Diff on per‑invoice rows, and labels the customer subtotal row
`<name> — Total (N docs)`.

---

## `inventory/` — stock, reconciliation, production

Views in `inventory/views.py`; logic split across `inventory/services/` (`oils.py`,
`beverages.py`, `reconciliation.py`, `production.py`, `stock_audit.py`, `shared.py`, `chat.py`).

| Report | Route | Gate | What it does · Source |
|--------|-------|------|------------------------|
| **Stock Available** | `/inventory/stock-available/` | `can_stock_available` (`stock_viewer`, or full inventory) | Finished‑goods stock on hand; **Oil / Beverages / Mart** company toggle with a data‑driven type toggle & KPIs (beverages FG taxonomy DRINKS/WATER → variety). Excel export. `SAP:oil/bev/mart` |
| **Finished Goods — Non‑Inventory** | `/inventory/non-inventory/` | `can_non_inventory` (`non_inventory_viewer`) | Non‑moving in‑stock FG aging (production date = first receipt; days‑since‑moved = last billed); drill Qty/Ltr/Boxes to warehouse. `SAP` |
| **Wellness–Mart Reconciliation** | `/inventory/reconciliation/` | `can_reconciliation` (`reconciliation_viewer`) | Inter‑company billing chain **Mart PO → Wellness SO → GRPO → A/P → A/R**; flags chains whose tax‑inclusive totals don't match (partials summed, differences ≤ tolerance ignored). A **Ledgers** tab pivots the BP ledgers by ORIGIN. Oil/Beverages seller toggle (swaps the seller schema). Clickable amounts reveal the underlying SAP document numbers; Excel export always combines both seller companies. `SAP:mart + wellness` |
| **Production Plan** | `/inventory/production/` | `can_production` (`production_viewer`) | Finished‑goods production feasibility vs available RM/PM; warehouse picker. `SAP` |
| **Daily Production Transaction** | `/inventory/daily-production/` | `can_daily_production` (`daily_production_viewer`) | `OWOR` Type='S' work orders → litres/boxes produced per day; warehouse filter (default BH‑PF); drill Date › Item. `SAP` |
| Oils / Beverages APIs + **AI chat** | `/inventory/oils/api/*`, `/inventory/beverages/api/*` | inventory | Company‑specific data APIs and a Groq‑backed chat helper (`GROQ_API_KEY`). `SAP` + LLM |

The Wellness–Mart Reconciliation tab is styled in the shared light theme (white cards, light
table headers, soft‑indigo active states, light document popover).

---

## `sales/` — standalone Sales dashboard

`/sales/` (`sales/views.py`) — an independent sales dashboard with its own `api/sales-data`,
`api/drill-down`, and raw‑CSV / Excel exports, gated by the sales module groups (`can_sales`).
It shares the `/api/sales-data/` audience with the Realise dashboard (`any_permission_flag`).

---

## Access and User Management

Every row above is enforced by a `@permission_flag_required('can_…')` (or `@group_required`)
decorator and mirrored by a `{% if can_… %}` sidebar entry. Admins grant access from
**`/users/`** by ticking the report's viewer group. The mapping from group → `can_*` flag lives
in `core/context_processors.build_user_permissions`; the User‑Management toggle catalog lives in
`home/views.py` (`PAGE_PERMS` / `MODULE_PERMS`). Full details in
[architecture.md](architecture.md#5-access-control).
