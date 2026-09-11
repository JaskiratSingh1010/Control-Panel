import calendar
import logging
import threading
import time
from decimal import Decimal
from datetime import date, datetime, timedelta

from django.db import transaction
from django.utils import timezone

from core import sap_connector
from .models import (MainGroupMaster, MonthlyTarget, SegmentTarget, StateMaster,
                     TargetMaster, TargetNode, TerritoryMapping, TerritoryProductTarget,
                     TerritoryItemTarget,
                     CityOwner, ClosingRemark, CreditLock, CreditLockSnapshot, FlexTarget,
                     AgingRemark, AgingRemarkLine, AgingDueConfig, Claim)

logger = logging.getLogger(__name__)

SAP_SCHEMA = 'JIVO_OIL_HANADB'
SAP_PROC   = 'REPORT_SALES_ANALYSIS'

ALLOWED_SUB_GROUPS = {
    'BLENDED', 'COTTON SEED', 'MUSTARD', 'RICE BRAN', 'SLICED OLIVE',
    'SOYABEAN', 'SUNFLOWER', 'CANOLA', 'COCONUT', 'EXTRA VIRGIN OLIVE',
    'GHEE', 'GROUNDNUT', 'OLIVE', 'SESAME', 'YELLOW MUSTARD',
}

# Reserved sub_group for a state-level aggregate Premium/Commodity target entered
# directly on the state card (not split into products). It rolls up into the
# (channel, state, segment) TargetNode like any product, but is kept OUT of the
# per-product MonthlyTarget so it never appears as a phantom product.
AGG_SUBGROUP = '__ALL__'

RECLASSIFY_RULES = [
    ('YELLOW MUSTARD',       'PREMIUM',   'YELLOW MUSTARD'),
    ('EXTRA VIRGIN COCONUT', 'PREMIUM',   'COCONUT'),
    ('EXTRA VIRGIN',         'PREMIUM',   'EXTRA VIRGIN OLIVE'),
    ('SLICED OLIVE',         'PREMIUM',   'SLICED OLIVE'),
]

DEFAULT_TARGETS = {
    'COMMODITY|BLENDED':          {'tgt_ltrs': 30000,   'tgt_rate': 130},
    'COMMODITY|COTTON SEED':      {'tgt_ltrs': 20000,   'tgt_rate': 130},
    'COMMODITY|MUSTARD':          {'tgt_ltrs': 625000,  'tgt_rate': 145},
    'COMMODITY|RICE BRAN':        {'tgt_ltrs': 25000,   'tgt_rate': 131},
    'COMMODITY|SOYABEAN':         {'tgt_ltrs': 400000,  'tgt_rate': 123},
    'COMMODITY|SUNFLOWER':        {'tgt_ltrs': 135000,  'tgt_rate': 145},
    'PREMIUM|BLENDED':            {'tgt_ltrs': 10000,   'tgt_rate': 190},
    'PREMIUM|CANOLA':             {'tgt_ltrs': 350000,  'tgt_rate': 205},
    'PREMIUM|COCONUT':            {'tgt_ltrs': 5000,    'tgt_rate': 449},
    'PREMIUM|EXTRA VIRGIN OLIVE': {'tgt_ltrs': 15000,   'tgt_rate': 500},
    'PREMIUM|GHEE':               {'tgt_ltrs': 15000,   'tgt_rate': 536},
    'PREMIUM|GROUNDNUT':          {'tgt_ltrs': 50000,   'tgt_rate': 175},
    'PREMIUM|OLIVE':              {'tgt_ltrs': 310000,  'tgt_rate': 253},
    'PREMIUM|SESAME':             {'tgt_ltrs': 5000,    'tgt_rate': 290},
    'PREMIUM|SLICED OLIVE':       {'tgt_ltrs': 0,       'tgt_rate': 0},
    'PREMIUM|YELLOW MUSTARD':     {'tgt_ltrs': 10000,   'tgt_rate': 180},
}

MONTHS_ORDER = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
CHANNEL_GROUPS = ['GT', 'ROI', 'MT', 'HORECA', 'CSD', 'REST']
REST_SOURCE_GROUPS = [
    'E-COMMERCE', 'CASH SALE', 'CORPORATE', 'SANGAT',
    'BRANCH', 'STAFF', 'REFERENCE', 'PURCHASE OIL',
]


def _reclassify(u_type, u_sub, item_name):
    combined = (item_name + ' ' + u_sub).upper()
    for keyword, new_type, new_sub in RECLASSIFY_RULES:
        if keyword in combined:
            return new_type, new_sub
    return u_type, u_sub


def _parse_doc_date(doc_date):
    if isinstance(doc_date, (datetime, date)):
        return doc_date.strftime('%b').upper(), str(doc_date.year)
    if isinstance(doc_date, str) and doc_date.strip():
        s = doc_date.strip()
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%Y/%m/%d', '%d/%m/%Y'):
            try:
                dt = datetime.strptime(s[:10], fmt)
                return dt.strftime('%b').upper(), str(dt.year)
            except ValueError:
                pass
        try:
            dt = datetime.fromisoformat(s[:19])
            return dt.strftime('%b').upper(), str(dt.year)
        except ValueError:
            pass
    return '', ''


def _fetch_raw(start_date, end_date):
    sql = f'CALL "{SAP_SCHEMA}"."{SAP_PROC}"(?, ?)'
    try:
        with sap_connector.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (start_date, end_date))
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            cursor.close()
        return _apply_delhi_gt_remap([dict(zip(columns, row)) for row in rows])
    except Exception as e:
        logger.error('[SAP] Procedure call failed: %s', e)
        return []


# ── Beverages dataset (separate HANA schema + proc) ───────────────────────
# Same fetch mechanism as oils, but a different schema/proc and a product-only
# shape: Variety / Sub-Group / SKU dimensions with Quantity & Boxes metrics.
BEVERAGES_SCHEMA = 'JIVO_BEVERAGES_HANADB'
MART_SCHEMA = 'JIVO_MART_HANADB'          # Jivo Mart company — used by the Customer Aging Mart toggle
_BEV_CACHE = {}
_BEV_CACHE_TTL = 90   # seconds, same as oils


def _fetch_raw_beverages(start_date, end_date):
    # Boxes_Sold = SUM(Quantity / SalFactor2). Note the Variety/Sub_Group aliases are
    # intentionally cross-mapped to OITM's U_Sub_Group/U_Variety, per the source query.
    # OITB join restricts to FINISHED goods so packaging materials (Pouch, Caps, …) and
    # assets (Office Equipment, Plant & Machinery) sold on invoices are excluded.
    sql = f'''SELECT
        T0."DocNum", T0."DocDate", T5."SlpName" AS "SalesPerson", T4."U_Main_Group", T4."U_Chain",
        (SELECT K."Name" FROM {BEVERAGES_SCHEMA}.OCST K
          WHERE K."Code" = T7."State" AND K."Country" = T7."Country") AS "State",
        T0."CardCode", T4."CardName",
        T2."U_SKU" AS "SKU", T2."ItemName",
        T2."U_Sub_Group" AS "Variety",
        T2."U_Variety" AS "Sub_Group",
        T2."U_Brand" AS "Brand",
        YEAR(T0."DocDate") AS "Year",
        TO_CHAR(T0."DocDate",'Mon-YYYY') AS "MonthName",
        SUM(T1."Quantity") AS "PCS_Sold",
        MAX(T2."SalFactor2") AS "PCS_Per_Box",
        ROUND(SUM(T1."Quantity" / NULLIF(T2."SalFactor2",0)), 2) AS "Boxes_Sold",
        SUM(T1."LineTotal") AS "Sales_Value"
    FROM {BEVERAGES_SCHEMA}.OINV T0
    INNER JOIN {BEVERAGES_SCHEMA}.INV1 T1 ON T0."DocEntry" = T1."DocEntry"
    INNER JOIN {BEVERAGES_SCHEMA}.OITM T2 ON T1."ItemCode" = T2."ItemCode"
    INNER JOIN {BEVERAGES_SCHEMA}.OITB G ON T2."ItmsGrpCod" = G."ItmsGrpCod"
    INNER JOIN {BEVERAGES_SCHEMA}.OCRD T4 ON T0."CardCode" = T4."CardCode"
    LEFT JOIN {BEVERAGES_SCHEMA}.OSLP T5 ON T0."SlpCode" = T5."SlpCode"
    LEFT JOIN {BEVERAGES_SCHEMA}.CRD1 T7
        ON T7."CardCode" = T0."CardCode" AND T7."AdresType" = 'S' AND T7."Address" = T0."ShipToCode"
    WHERE T0."CANCELED" = 'N' AND T4."GroupCode" <> 100 AND T1."TreeType" <> 'I'
        AND G."ItmsGrpNam" = 'FINISHED'
        AND T0."DocDate" BETWEEN ? AND ?
    GROUP BY T0."DocNum", T0."DocDate", T5."SlpName", T4."U_Main_Group", T4."U_Chain",
        T7."State", T7."Country", T0."CardCode", T4."CardName",
        T2."U_SKU", T2."ItemName", T2."U_Sub_Group", T2."U_Variety", T2."U_Brand"
    ORDER BY T0."DocDate", T0."DocNum", T2."ItemName"'''
    try:
        with sap_connector.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (start_date, end_date))
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            cursor.close()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.error('[SAP-BEV] Sales query failed: %s', e)
        return []


def _fetch_raw_beverages_oih(start_date, end_date):
    # Order in Hand = open sales-order lines (ORDR/RDR1, LineStatus='O'). Same shape and
    # FINISHED-goods filter as the sales query, date-filtered to the selected range so it
    # stays coherent with the period shown.
    sql = f'''SELECT
        T0."DocNum", T0."DocDate", T5."SlpName" AS "SalesPerson", T4."U_Main_Group", T4."U_Chain",
        (SELECT K."Name" FROM {BEVERAGES_SCHEMA}.OCST K
          WHERE K."Code" = T7."State" AND K."Country" = T7."Country") AS "State",
        T0."CardCode", T4."CardName",
        T2."U_SKU" AS "SKU", T2."ItemName",
        T2."U_Sub_Group" AS "Variety",
        T2."U_Variety" AS "Sub_Group",
        T2."U_Brand" AS "Brand",
        YEAR(T0."DocDate") AS "Year",
        TO_CHAR(T0."DocDate",'Mon-YYYY') AS "MonthName",
        SUM(T1."Quantity") AS "PCS_Ordered",
        MAX(T2."SalFactor2") AS "PCS_Per_Box",
        ROUND(SUM(T1."Quantity" / NULLIF(T2."SalFactor2",0)), 2) AS "Boxes_Ordered",
        SUM(T1."LineTotal") AS "Order_Value"
    FROM {BEVERAGES_SCHEMA}.ORDR T0
    INNER JOIN {BEVERAGES_SCHEMA}.RDR1 T1 ON T0."DocEntry" = T1."DocEntry"
    INNER JOIN {BEVERAGES_SCHEMA}.OITM T2 ON T1."ItemCode" = T2."ItemCode"
    INNER JOIN {BEVERAGES_SCHEMA}.OITB G ON T2."ItmsGrpCod" = G."ItmsGrpCod"
    INNER JOIN {BEVERAGES_SCHEMA}.OCRD T4 ON T0."CardCode" = T4."CardCode"
    LEFT JOIN {BEVERAGES_SCHEMA}.OSLP T5 ON T0."SlpCode" = T5."SlpCode"
    LEFT JOIN {BEVERAGES_SCHEMA}.CRD1 T7
        ON T7."CardCode" = T0."CardCode" AND T7."AdresType" = 'S' AND T7."Address" = T0."ShipToCode"
    WHERE T0."CANCELED" = 'N' AND T4."GroupCode" <> 100 AND T1."TreeType" <> 'I'
        AND T1."LineStatus" = 'O' AND G."ItmsGrpNam" = 'FINISHED'
        AND T0."DocDate" BETWEEN ? AND ?
    GROUP BY T0."DocNum", T0."DocDate", T5."SlpName", T4."U_Main_Group", T4."U_Chain",
        T7."State", T7."Country", T0."CardCode", T4."CardName",
        T2."U_SKU", T2."ItemName", T2."U_Sub_Group", T2."U_Variety", T2."U_Brand"
    ORDER BY T0."DocDate", T0."DocNum"'''
    try:
        with sap_connector.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (start_date, end_date))
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            cursor.close()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.error('[SAP-BEV] OIH query failed: %s', e)
        return []


def _bev_pick(row, *keys):
    """First non-empty value among candidate column names (proc casing varies)."""
    for k in keys:
        if k in row and row[k] not in (None, ''):
            return row[k]
    return None


def _bev_num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _bev_date(value):
    """Parse a DocDate cell to a date (hdbcli usually returns date/datetime objects)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        s = value.strip()[:10]
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%Y/%m/%d', '%d/%m/%Y'):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    return None


def _shift_back_one_month(d):
    """One calendar month earlier, keeping the day number. A day the earlier month does
    not have is clamped to its last day, so 31 Mar becomes 28 Feb (29 in a leap year)."""
    y, m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _bev_prev_month_range(start_date, end_date):
    """The SAME day-of-month span, one calendar month back: 01-11 Sep -> 01-11 Aug.
    Both ends shift, so a range that straddles a month boundary keeps its shape.
    Returns ('', '') when either date cannot be read."""
    a, b = _bev_date(start_date), _bev_date(end_date)
    if not a or not b:
        return '', ''
    return _shift_back_one_month(a).isoformat(), _shift_back_one_month(b).isoformat()


_BEV_MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _bev_month_key(r, dd):
    """(sort key 'YYYY-MM', display label 'Mon YYYY') for a row — preferring the proc's
    Year/Month columns, falling back to the parsed DocDate. The label is built from the
    abbreviation + year (the proc's MonthName already embeds the year, so reusing it
    would double it, e.g. 'May-2026 2026')."""
    year = _bev_pick(r, 'Year', 'YEAR', 'year')
    month = _bev_pick(r, 'Month', 'MONTH', 'month')
    try:
        yv = int(float(year)); mv = int(float(month))
        if 1 <= mv <= 12:
            return '%04d-%02d' % (yv, mv), '%s %d' % (_BEV_MONTH_ABBR[mv - 1], yv)
    except (TypeError, ValueError):
        pass
    if dd:
        return '%04d-%02d' % (dd.year, dd.month), '%s %d' % (_BEV_MONTH_ABBR[dd.month - 1], dd.year)
    return None, None


def _bev_accum_item(store, item, sku, brand, qty, box, val=0.0):
    """Accumulate a single day's sale into a per-item bucket (keyed by item/SKU/brand)
    for the day-specific 'what was sold' drill-downs. `val` is the invoiced value, which
    the KPI cards divide by boxes to show the realise."""
    cell = store.setdefault((item, sku, brand), {'quantity': 0.0, 'boxes': 0.0, 'value': 0.0})
    cell['quantity'] += qty
    cell['boxes'] += box
    cell['value'] += val


def _bev_items_list(store):
    out = [{'item': k[0], 'sku': k[1], 'brand': k[2],
            'quantity': round(v['quantity'], 2), 'boxes': round(v['boxes'], 2),
            'value': round(v.get('value', 0.0), 2)}
           for k, v in store.items()]
    out.sort(key=lambda x: x['boxes'], reverse=True)
    return out


# Same salesperson under different SAP names — keyed by normalized (UPPER/stripped)
# variant → canonical name. Add more pairs here as duplicates surface.
_BEV_SALESPERSON_ALIAS = {
    'GOLDY VG': 'GOLDY',
}


def _bev_salesperson(r):
    sp = _normalize_name(_bev_pick(r, 'SalesPerson', 'SALESPERSON', 'SlpName', 'sales_person')) or '—'
    return _BEV_SALESPERSON_ALIAS.get(sp, sp)


def get_beverages_rows(start_date, end_date, want_prev=False):
    """Beverage sales rows aggregated by (Variety, Sub_Group, SKU, Item, Main Group, State,
    Brand, Chain, Month) with Quantity (PCS) and Boxes, plus today's & yesterday's box
    totals, a per-item breakdown for each of those days, and customer/month aggregates.
    The client nests the rows into any drill order, filters by Brand/Month, and opens the
    day & top breakdowns from the KPIs.

    want_prev also totals the SAME day span one calendar month back (01-11 Sep ->
    01-11 Aug) for the 'Last Month' KPI card. It is a second SAP round-trip, so the
    caller asks for it only in date-range mode - a 120-month range has no useful
    'same dates last month'."""
    raw = _fetch_raw_beverages(start_date, end_date)
    agg = {}
    # Use the project timezone (Asia/Kolkata, USE_TZ=True) for the "today"/"yesterday"
    # cut-off — date.today() reads the server's OS date, which on a UTC host points at
    # the wrong day until ~05:30 IST and would make today's sales read 0.
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    today_boxes = 0.0
    yest_boxes = 0.0
    today_items = {}
    yest_items = {}
    cust_agg = {}    # (customer, brand) -> {quantity, boxes}  → Top Customers (month-wise view)
    month_agg = {}   # (ym, brand)       -> {quantity, boxes, label}  → Top Months
    for r in raw or []:
        variety = _normalize_name(_bev_pick(r, 'Variety', 'VARIETY', 'variety')) or '—'
        sub = _normalize_name(_bev_pick(r, 'Sub_Group', 'SUB_GROUP', 'U_Sub_Group', 'sub_group')) or '—'
        sku = _normalize_name(_bev_pick(r, 'SKU', 'U_SKU', 'Sku', 'sku')) or '—'
        item = _normalize_name(_bev_pick(r, 'ItemName', 'ITEMNAME', 'Item_Name', 'item_name')) or '—'
        main_group = _normalize_name(_bev_pick(r, 'U_Main_Group', 'U_MAIN_GROUP', 'Main_Group', 'main_group')) or '—'
        state = _normalize_name(_bev_pick(r, 'State', 'STATE', 'state')) or '—'
        brand = _normalize_name(_bev_pick(r, 'Brand', 'BRAND', 'U_Brand', 'U_BRAND', 'brand')) or '—'
        chain = _normalize_name(_bev_pick(r, 'U_Chain', 'U_CHAIN', 'Chain', 'chain')) or '—'
        customer = _normalize_name(_bev_pick(r, 'CardName', 'CARDNAME', 'Customer', 'card_name')) or '—'
        sales_person = _bev_salesperson(r)
        qty = _bev_num(_bev_pick(r, 'PCS_Sold', 'PCS_SOLD', 'Quantity', 'QUANTITY', 'Qty', 'quantity'))
        box = _bev_num(_bev_pick(r, 'Boxes_Sold', 'BOXES_SOLD', 'Box', 'BOX', 'Boxes', 'box'))
        # Invoiced value of the line. Needed for realise (value / boxes = the rate a box
        # actually sold for). Same number as unit price x case pack, because
        # boxes = quantity / case pack - the division just cancels out.
        val = _bev_num(_bev_pick(r, 'Sales_Value', 'SALES_VALUE', 'LineTotal', 'LINETOTAL'))
        dd = _bev_date(_bev_pick(r, 'DocDate', 'DOCDATE', 'Doc_Date', 'doc_date'))
        ym, mlabel = _bev_month_key(r, dd)
        ymk = ym or ''   # carried on each row so the client can filter to a single month
        key = (variety, sub, sku, item, main_group, state, brand, chain, sales_person, customer, ymk)
        cell = agg.setdefault(key, {'quantity': 0.0, 'boxes': 0.0, 'value': 0.0})
        cell['quantity'] += qty
        cell['boxes'] += box
        cell['value'] += val
        cc = cust_agg.setdefault((customer, brand, ymk), {'quantity': 0.0, 'boxes': 0.0})
        cc['quantity'] += qty; cc['boxes'] += box
        if ym:
            mc = month_agg.setdefault((ym, brand), {'quantity': 0.0, 'boxes': 0.0, 'label': mlabel})
            mc['quantity'] += qty; mc['boxes'] += box
        if dd == today:
            today_boxes += box
            _bev_accum_item(today_items, item, sku, brand, qty, box, val)
        elif dd == yesterday:
            yest_boxes += box
            _bev_accum_item(yest_items, item, sku, brand, qty, box, val)

    # ── Same dates, one month back ─────────────────────────────────
    # Its own range, so its rows are NOT in `raw` above - a separate pull is needed.
    # Goes through the 90s raw cache: last month's figures barely move, and the live
    # heartbeat re-runs this function often, so re-querying every time is waste.
    prev_start, prev_end = ('', '')
    prev_items, prev_boxes = {}, 0.0
    if want_prev:
        prev_start, prev_end = _bev_prev_month_range(start_date, end_date)
        if prev_start and prev_end:
            for r in _bev_raw_cached(prev_start, prev_end, 'sales') or []:
                pbox = _bev_num(_bev_pick(r, 'Boxes_Sold', 'BOXES_SOLD', 'Box', 'BOX', 'Boxes', 'box'))
                prev_boxes += pbox
                _bev_accum_item(
                    prev_items,
                    _normalize_name(_bev_pick(r, 'ItemName', 'ITEMNAME', 'Item_Name', 'item_name')) or '—',
                    _normalize_name(_bev_pick(r, 'SKU', 'U_SKU', 'Sku', 'sku')) or '—',
                    _normalize_name(_bev_pick(r, 'Brand', 'BRAND', 'U_Brand', 'U_BRAND', 'brand')) or '—',
                    _bev_num(_bev_pick(r, 'PCS_Sold', 'PCS_SOLD', 'Quantity', 'QUANTITY', 'Qty', 'quantity')),
                    pbox,
                    _bev_num(_bev_pick(r, 'Sales_Value', 'SALES_VALUE', 'LineTotal', 'LINETOTAL')))

    # ── Order in Hand (open sales orders) ────────────────────────────────────
    # oih_main: boxes keyed by the same dims as sales rows (merged in as the 'oih' column).
    # oih_pop:  by (variety, sub, item, customer, brand, month) for the OIH drill popup.
    oih_main = {}
    oih_pop = {}
    for r in _fetch_raw_beverages_oih(start_date, end_date) or []:
        variety = _normalize_name(_bev_pick(r, 'Variety', 'VARIETY', 'variety')) or '—'
        sub = _normalize_name(_bev_pick(r, 'Sub_Group', 'SUB_GROUP', 'U_Sub_Group', 'sub_group')) or '—'
        sku = _normalize_name(_bev_pick(r, 'SKU', 'U_SKU', 'Sku', 'sku')) or '—'
        item = _normalize_name(_bev_pick(r, 'ItemName', 'ITEMNAME', 'Item_Name', 'item_name')) or '—'
        main_group = _normalize_name(_bev_pick(r, 'U_Main_Group', 'U_MAIN_GROUP', 'Main_Group', 'main_group')) or '—'
        state = _normalize_name(_bev_pick(r, 'State', 'STATE', 'state')) or '—'
        brand = _normalize_name(_bev_pick(r, 'Brand', 'BRAND', 'U_Brand', 'U_BRAND', 'brand')) or '—'
        chain = _normalize_name(_bev_pick(r, 'U_Chain', 'U_CHAIN', 'Chain', 'chain')) or '—'
        customer = _normalize_name(_bev_pick(r, 'CardName', 'CARDNAME', 'Customer', 'card_name')) or '—'
        sales_person = _bev_salesperson(r)
        opcs = _bev_num(_bev_pick(r, 'PCS_Ordered', 'PCS_ORDERED', 'PCS_Sold', 'Quantity', 'Qty'))
        obox = _bev_num(_bev_pick(r, 'Boxes_Ordered', 'BOXES_ORDERED', 'Boxes_Sold', 'Boxes', 'Box'))
        ym, _ml = _bev_month_key(r, _bev_date(_bev_pick(r, 'DocDate', 'DOCDATE', 'Doc_Date', 'doc_date')))
        ymk = ym or ''
        mk = (variety, sub, sku, item, main_group, state, brand, chain, sales_person, customer, ymk)
        oih_main[mk] = oih_main.get(mk, 0.0) + obox
        pk = (variety, sub, item, customer, brand, ymk)
        pc = oih_pop.setdefault(pk, {'pcs': 0.0, 'boxes': 0.0})
        pc['pcs'] += opcs; pc['boxes'] += obox

    # Union of sales + OIH keys so products with open orders but no in-range sales still show.
    rows = []
    for k in set(agg) | set(oih_main):
        v = agg.get(k)
        rows.append({'variety': k[0], 'sub_group': k[1], 'sku': k[2], 'item': k[3],
                     'main_group': k[4], 'state': k[5], 'brand': k[6], 'chain': k[7],
                     'sales_person': k[8], 'customer': k[9], 'ym': k[10],
                     'quantity': round(v['quantity'], 2) if v else 0.0,
                     'boxes': round(v['boxes'], 2) if v else 0.0,
                     'value': round(v.get('value', 0.0), 2) if v else 0.0,
                     'oih': round(oih_main.get(k, 0.0), 2)})
    oih_rows = [{'variety': k[0], 'sub_group': k[1], 'item': k[2], 'customer': k[3],
                 'brand': k[4], 'ym': k[5],
                 'quantity': round(v['pcs'], 2), 'boxes': round(v['boxes'], 2)}
                for k, v in oih_pop.items()]
    customer_rows = [{'customer': k[0], 'brand': k[1], 'ym': k[2],
                      'quantity': round(v['quantity'], 2), 'boxes': round(v['boxes'], 2)}
                     for k, v in cust_agg.items()]
    month_rows = [{'ym': k[0], 'brand': k[1], 'label': v['label'],
                   'quantity': round(v['quantity'], 2), 'boxes': round(v['boxes'], 2)}
                  for k, v in month_agg.items()]
    return {'rows': rows,
            'today_boxes': round(today_boxes, 2), 'yesterday_boxes': round(yest_boxes, 2),
            'today_items': _bev_items_list(today_items),
            'yesterday_items': _bev_items_list(yest_items),
            'today_date': today.isoformat(), 'yesterday_date': yesterday.isoformat(),
            'prev_boxes': round(prev_boxes, 2), 'prev_items': _bev_items_list(prev_items),
            'prev_start': prev_start, 'prev_end': prev_end,
            'customer_rows': customer_rows, 'month_rows': month_rows, 'oih_rows': oih_rows}


def get_beverages_rows_cached(start_date, end_date, want_prev=False):
    # want_prev is in the key: a payload fetched without the previous-month totals
    # must not be handed to a caller that asked for them.
    key = f'{start_date}|{end_date}|{1 if want_prev else 0}'
    now = time.time()
    hit = _BEV_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    data = get_beverages_rows(start_date, end_date, want_prev=want_prev)
    if data and data.get('rows'):
        _BEV_CACHE[key] = (now + _BEV_CACHE_TTL, data)
        for k in [k for k, v in _BEV_CACHE.items() if v[0] <= now]:
            _BEV_CACHE.pop(k, None)
    return data


# Per-document raw beverage rows (un-aggregated) cached so repeated document-drill
# expansions within the TTL reuse one SAP round-trip. Keyed by range + metric.
_BEV_RAW_CACHE = {}        # 'start|end|metric' -> (expires_at, raw_rows)
_BEV_RAW_CACHE_TTL = 90    # seconds


def _bev_raw_cached(start_date, end_date, metric):
    key = f'{start_date}|{end_date}|{metric}'
    now = time.time()
    hit = _BEV_RAW_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    raw = (_fetch_raw_beverages_oih(start_date, end_date) if metric == 'oih'
           else _fetch_raw_beverages(start_date, end_date))
    if raw:
        _BEV_RAW_CACHE[key] = (now + _BEV_RAW_CACHE_TTL, raw)
        for k in [k for k, v in _BEV_RAW_CACHE.items() if v[0] <= now]:
            _BEV_RAW_CACHE.pop(k, None)
    return raw


# Normalized extractor per driller dimension — mirrors the grouping in get_beverages_rows
# so a document query filters the raw rows by exactly the values the client drilled into.
_BEV_DIM_EXTRACT = {
    'variety':      lambda r: _normalize_name(_bev_pick(r, 'Variety', 'VARIETY', 'variety')) or '—',
    'sub_group':    lambda r: _normalize_name(_bev_pick(r, 'Sub_Group', 'SUB_GROUP', 'U_Sub_Group', 'sub_group')) or '—',
    'sku':          lambda r: _normalize_name(_bev_pick(r, 'SKU', 'U_SKU', 'Sku', 'sku')) or '—',
    'item':         lambda r: _normalize_name(_bev_pick(r, 'ItemName', 'ITEMNAME', 'Item_Name', 'item_name')) or '—',
    'main_group':   lambda r: _normalize_name(_bev_pick(r, 'U_Main_Group', 'U_MAIN_GROUP', 'Main_Group', 'main_group')) or '—',
    'state':        lambda r: _normalize_name(_bev_pick(r, 'State', 'STATE', 'state')) or '—',
    'brand':        lambda r: _normalize_name(_bev_pick(r, 'Brand', 'BRAND', 'U_Brand', 'U_BRAND', 'brand')) or '—',
    'chain':        lambda r: _normalize_name(_bev_pick(r, 'U_Chain', 'U_CHAIN', 'Chain', 'chain')) or '—',
    'sales_person': _bev_salesperson,
    'customer':     lambda r: _normalize_name(_bev_pick(r, 'CardName', 'CARDNAME', 'Customer', 'card_name')) or '—',
}


def get_beverages_documents(start_date, end_date, filters, metric='sales'):
    """Invoice (sales) or open sales-order (oih) documents behind a beverages driller cell,
    filtered to the clicked node's dimension path (customer + any ancestors) plus brand/month.
    Re-derives the same normalized dimensions the driller buckets by and rolls the raw rows
    up to document grain. metric='oih' -> open SOs (ORDR), else sales invoices (OINV)."""
    metric = 'oih' if str(metric or '').strip().lower() == 'oih' else 'sales'
    raw = _bev_raw_cached(start_date, end_date, metric)
    filters = filters or {}
    want_ym = str(filters.get('ym') or '').strip()
    dim_filters = [(k, v) for k, v in filters.items() if k in _BEV_DIM_EXTRACT]
    docs = {}
    for r in raw or []:
        ok = True
        for k, v in dim_filters:
            if _BEV_DIM_EXTRACT[k](r) != v:
                ok = False
                break
        if not ok:
            continue
        dd = _bev_date(_bev_pick(r, 'DocDate', 'DOCDATE', 'Doc_Date', 'doc_date'))
        ym, _label = _bev_month_key(r, dd)
        if want_ym and (ym or '') != want_ym:
            continue
        num = str(_bev_pick(r, 'DocNum', 'DOCNUM', 'Doc_Num', 'doc_num') or '').strip()
        qty = _bev_num(_bev_pick(r, 'PCS_Sold', 'PCS_SOLD', 'PCS_Ordered', 'PCS_ORDERED', 'Quantity', 'Qty'))
        box = _bev_num(_bev_pick(r, 'Boxes_Sold', 'BOXES_SOLD', 'Boxes_Ordered', 'BOXES_ORDERED', 'Boxes', 'Box'))
        dkey = num or ((dd.isoformat() if dd else '') + '|' + _BEV_DIM_EXTRACT['customer'](r))
        rec = docs.get(dkey)
        if rec is None:
            rec = docs[dkey] = {'doc_num': num, 'doc_date': dd.isoformat() if dd else '',
                                'customer': _BEV_DIM_EXTRACT['customer'](r), 'quantity': 0.0, 'boxes': 0.0}
        rec['quantity'] += qty
        rec['boxes'] += box
    out = list(docs.values())
    for d in out:
        d['quantity'] = round(d['quantity'], 2)
        d['boxes'] = round(d['boxes'], 2)
    out.sort(key=lambda x: (x['doc_date'] or '', x['doc_num']))
    return out


def _empty_result():
    return {
        'total_litres': 0, 'total_tonnes': 0,
        'total_revenue': 0, 'net_realise': 0,
        'products': [],
    }


def get_sales_data(start_date, end_date):
    raw = _fetch_raw(start_date, end_date)
    if not raw:
        return _empty_result(), []

    grouped = {}
    for d in raw:
        u_type    = str(d.get('U_TYPE', '') or '').strip().upper()
        u_sub     = str(d.get('U_Sub_Group', '') or '').strip().upper()
        item_name = str(d.get('ItemName', '') or '').strip().upper()
        u_type, u_sub = _reclassify(u_type, u_sub, item_name)

        if u_type not in ('PREMIUM', 'COMMODITY'):
            continue
        if u_sub not in ALLOWED_SUB_GROUPS:
            continue

        litres    = float(d.get('Liter', 0) or 0)
        linetotal = float(d.get('LineTotal', 0) or 0)
        month, year = _parse_doc_date(d.get('DocDate', ''))
        if not month or not year:
            continue

        key = f'{u_type}|{u_sub}|{month}|{year}'
        if key not in grouped:
            grouped[key] = {
                'u_type': u_type, 'u_sub_group': u_sub,
                'month': month, 'year': year,
                'litres': 0.0, 'linetotal': 0.0,
            }
        grouped[key]['litres']    += litres
        grouped[key]['linetotal'] += linetotal

    total_litres = 0.0
    total_revenue = 0.0
    products = []
    for g in grouped.values():
        g['litres']    = round(g['litres'], 2)
        g['linetotal'] = round(g['linetotal'], 2)
        g['realise']   = round(g['linetotal'] / g['litres'], 2) if g['litres'] > 0 else 0
        total_litres  += g['litres']
        total_revenue += g['linetotal']
        products.append(g)

    net_realise = round(total_revenue / total_litres, 2) if total_litres > 0 else 0
    return {
        'total_litres': round(total_litres, 2),
        'total_tonnes': round(total_litres / 1000, 3),
        'total_revenue': round(total_revenue, 2),
        'net_realise': net_realise,
        'products': products,
    }, raw


# In-process cache for the expensive REPORT_SALES_ANALYSIS call (a full-FY pull is a
# ~25s HANA round-trip). Strategy = stale-while-revalidate: once a range is cached, a
# request that finds it expired gets the STALE rows back INSTANTLY and a background
# thread refreshes them — so no user ever waits on a cold proc again (except the very
# first ever pull of a range, which startup pre-warming handles). The manual "Refresh
# from SAP" path passes force=True for a synchronous fresh pull.
# The in-process dict below is only the FAST tier. It is emptied by every restart, and
# each worker has its own, so on its own it means a user can still land on a cold copy
# and sit through the blocking 'Fetching from SAP HANA...' overlay. So every pull is
# ALSO written to the shared cache (file-backed, or Redis when DJANGO_CACHE_URL is set),
# which survives restarts and is visible to every worker. Reading 9,000 raw rows back
# from it measures ~0.08s against ~2.5s for the proc itself.
_SALES_CACHE = {}                 # 'start|end' -> {'exp', 'val', 'refreshing'}
_SALES_CACHE_TTL = 1800           # 30 min — historical SAP data drifts slowly; bg-refreshed
# The shared copy is kept four times longer on purpose: handing back a slightly stale
# answer instantly and refreshing it behind the user always beats blocking on the proc.
_SALES_SHARED_TTL = _SALES_CACHE_TTL * 4
_SALES_CACHE_LOCK = threading.Lock()


def _sales_shared_store(key, value):
    """Mirror one pull into the shared cache. Never let a cache problem break a request -
    the in-process copy above is already good, this is only the backup."""
    try:
        _shared_set('salesdata', key, {'v': value, 't': time.time()}, _SALES_SHARED_TTL)
    except Exception:
        logger.exception('sales-data shared-cache write failed for %s', key)


def _sales_shared_load(key, start_date, end_date):
    """Adopt a copy another process (or this one, before a restart) already fetched.
    Returns the value, or None when there is nothing usable to adopt. A copy that is
    already past the normal TTL is still served - instantly - and refreshed behind the
    user, which is the same stale-while-revalidate promise the in-process tier makes."""
    try:
        hit = _shared_get('salesdata', key)
    except Exception:
        logger.exception('sales-data shared-cache read failed for %s', key)
        return None
    if not hit or not hit.get('v') or not hit['v'][1]:
        return None
    value = hit['v']
    age = time.time() - (hit.get('t') or 0)
    stale = age >= _SALES_CACHE_TTL
    with _SALES_CACHE_LOCK:
        _SALES_CACHE[key] = {'exp': time.time() + max(0.0, _SALES_CACHE_TTL - age),
                             'val': value,
                             'refreshing': stale}   # True stops a second thread starting
    if stale:
        threading.Thread(target=_sales_fetch_and_store,
                         args=(key, start_date, end_date), daemon=True).start()
    return value


def _sales_fetch_and_store(key, start_date, end_date):
    """Pull fresh rows from SAP and store them. Keeps any existing stale value on a
    failed/empty pull so the dashboard never blanks out. Used both synchronously (cold
    / forced) and from the stale-while-revalidate background thread."""
    try:
        value = get_sales_data(start_date, end_date)
    except Exception:
        logger.exception('sales-data refresh failed for %s', key)
        value = None
    with _SALES_CACHE_LOCK:
        if value and value[1]:        # only cache successful, non-empty pulls
            _SALES_CACHE[key] = {'exp': time.time() + _SALES_CACHE_TTL,
                                 'val': value, 'refreshing': False}
            for k in [k for k, v in _SALES_CACHE.items() if v['exp'] <= time.time() - _SALES_CACHE_TTL]:
                _SALES_CACHE.pop(k, None)   # evict long-dead entries
        elif key in _SALES_CACHE:
            _SALES_CACHE[key]['refreshing'] = False   # keep stale value on failure
    # Outside the lock: writing 5 MB to the shared cache must not hold up other threads.
    if value and value[1]:
        _sales_shared_store(key, value)
    return value


def get_sales_data_cached(start_date, end_date, force=False):
    key = f'{start_date}|{end_date}'
    now = time.time()
    with _SALES_CACHE_LOCK:
        entry = _SALES_CACHE.get(key)
        if entry and not force:
            if entry['exp'] > now:
                return entry['val']                    # fresh hit
            # Expired but present: serve stale NOW, refresh in the background once.
            if not entry.get('refreshing'):
                entry['refreshing'] = True
                threading.Thread(target=_sales_fetch_and_store,
                                 args=(key, start_date, end_date), daemon=True).start()
            return entry['val']
    # Nothing in this process. Before making the user watch the 'Fetching from SAP HANA...'
    # overlay, see whether a restart-surviving copy is sitting in the shared cache.
    if not force:
        shared = _sales_shared_load(key, start_date, end_date)
        if shared is not None:
            return shared
    # Genuinely cold, or a forced refresh → fetch synchronously.
    return _sales_fetch_and_store(key, start_date, end_date)


# ── Live "heartbeat" pulse ──────────────────────────────────────────────────
# A tiny fingerprint of the data the dashboard shows, so the client can poll it cheaply every
# few seconds and only trigger a (heavy) fresh pull when something ACTUALLY changed. It moves
# when an invoice in the window is added / edited / cancelled (OINV, + ORIN credit notes for
# oils) or an open order is added or (partly) delivered (ORDR / RDR1). Header/line aggregates
# only - orders of magnitude cheaper than REPORT_SALES_ANALYSIS.
#
# The TTL is what decides how fresh "real time" can be: a change cannot show up faster than
# this, because every tab reads the cached answer. 2s keeps the dashboard genuinely live while
# still collapsing many tabs polling at once into one SAP round-trip.
_PULSE_CACHE = {}          # (dataset, start, end) -> (expires_at, pulse_string)
_PULSE_TTL = 2


def get_sales_pulse(dataset, start_date, end_date):
    """Short fingerprint string for one (dataset, date-range); '' on any SAP error."""
    S = BEVERAGES_SCHEMA if dataset == 'beverages' else SAP_SCHEMA
    open_cnt = (f'(SELECT COUNT(*) FROM "{S}"."RDR1" L JOIN "{S}"."ORDR" H ON H."DocEntry"=L."DocEntry" '
                f'''WHERE H."DocStatus"='O' AND L."LineStatus"='O')''')
    open_qty = (f'(SELECT COALESCE(ROUND(SUM(L."OpenQty"),2),0) FROM "{S}"."RDR1" L JOIN "{S}"."ORDR" H '
                f'''ON H."DocEntry"=L."DocEntry" WHERE H."DocStatus"='O' AND L."LineStatus"='O')''')
    inv_cnt = f'(SELECT COUNT(*) FROM "{S}"."OINV" WHERE "DocDate" BETWEEN ? AND ?)'
    inv_sum = f'(SELECT COALESCE(SUM("DocTotal"),0) FROM "{S}"."OINV" WHERE "DocDate" BETWEEN ? AND ?)'
    if dataset == 'beverages':
        sql = f'SELECT {inv_cnt} AS "A", {inv_sum} AS "B", {open_cnt} AS "C", {open_qty} AS "D" FROM DUMMY'
        params = (start_date, end_date, start_date, end_date)
    else:
        crn_sum = f'(SELECT COALESCE(SUM("DocTotal"),0) FROM "{S}"."ORIN" WHERE "DocDate" BETWEEN ? AND ?)'
        sql = f'SELECT {inv_cnt} AS "A", {inv_sum} AS "B", {crn_sum} AS "E", {open_cnt} AS "C", {open_qty} AS "D" FROM DUMMY'
        params = (start_date, end_date, start_date, end_date, start_date, end_date)
    try:
        rows = sap_connector.execute_query(sql, params)
    except Exception as exc:
        logger.error('[PULSE] fetch failed: %s', exc)
        return ''
    if not rows:
        return ''
    r = rows[0]
    return '|'.join(str(r.get(k)) for k in ('A', 'B', 'E', 'C', 'D') if k in r)


def get_sales_pulse_cached(dataset, start_date, end_date):
    key = (dataset or 'oils', str(start_date), str(end_date))
    now = time.time()
    hit = _PULSE_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = get_sales_pulse(*key)
    _PULSE_CACHE[key] = (now + _PULSE_TTL, val)
    return val


def prewarm_sales_cache():
    """Pre-fetch the ranges the dashboard opens with (current month + current FY) so the
    first load after a server start is warm. Safe to call from a daemon thread."""
    try:
        today = date.today()
        fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        ranges = [
            (today.replace(day=1).isoformat(), today.isoformat()),   # current month (main view)
            (fy_start.isoformat(), today.isoformat()),               # full FY (slide 2 / historical)
        ]
        for sd, ed in ranges:
            get_sales_data_cached(sd, ed)
    except Exception:
        logger.exception('sales cache pre-warm failed')


def get_drill_down(start_date, end_date, raw_rows, u_type=None, u_sub_group=None,
                   drill_by='State', month=None, year=None, filters=None):
    results = {}
    for d in raw_rows:
        rt = str(d.get('U_TYPE', '') or '').strip().upper()
        rs = str(d.get('U_Sub_Group', '') or '').strip().upper()
        item_name = str(d.get('ItemName', '') or '').strip().upper()
        rt, rs = _reclassify(rt, rs, item_name)

        if u_type and rt != u_type.upper():
            continue
        if u_sub_group and rs != u_sub_group.upper():
            continue

        if month or year:
            m, y = _parse_doc_date(d.get('DocDate', ''))
            if month and m != month:
                continue
            if year and y != year:
                continue

        if filters:
            skip = False
            for fk, fv in filters.items():
                if fk == 'ItemName':          # item nodes are labelled 'CODE — NAME'
                    nm = str(d.get('ItemName', '') or '').strip()
                    val = _item_label(str(d.get('ItemCode', '') or '').strip(), nm) if nm else ''
                else:
                    val = str(d.get(fk, '') or '').strip()
                if val.upper() != str(fv).upper():
                    skip = True
                    break
            if skip:
                continue

        dim_val = str(d.get(drill_by, '') or '').strip()
        if drill_by == 'ItemName' and dim_val:   # show the item code with the name
            dim_val = _item_label(str(d.get('ItemCode', '') or '').strip(), dim_val)
        if not dim_val:
            dim_val = 'UNKNOWN'

        litres    = float(d.get('Liter', 0) or 0)
        linetotal = float(d.get('LineTotal', 0) or 0)

        if dim_val not in results:
            results[dim_val] = {'dimension': dim_val, 'litres': 0.0, 'linetotal': 0.0}
        results[dim_val]['litres']    += litres
        results[dim_val]['linetotal'] += linetotal

    data = sorted(results.values(), key=lambda x: x['litres'], reverse=True)
    return data


def get_historical_realise(start_date, end_date, period='12m'):
    raw = _fetch_raw(start_date, end_date)
    if not raw:
        return {}, {}

    end_dt = datetime.strptime(end_date, '%Y-%m-%d').date() if isinstance(end_date, str) else end_date

    if period == '12m':
        months_back = 12
    elif period == '6m':
        months_back = 6
    elif period == '3m':
        months_back = 3
    elif period == 'last_month':
        months_back = 1
    else:
        months_back = 12

    agg = {}
    drill_agg = {}
    DRILL_COLS = ['State', 'U_Main_Group', 'U_Chain', 'ItemName', 'CardName']

    for d in raw:
        m, y = _parse_doc_date(d.get('DocDate', ''))
        if not m or not y:
            continue
        try:
            month_idx = MONTHS_ORDER.index(m)
            row_date  = date(int(y), month_idx + 1, 1)
        except (ValueError, IndexError):
            continue

        # Filter to the period window
        from_date = date(end_dt.year, end_dt.month, 1)
        # Step back months_back months
        m2 = end_dt.month - months_back
        y2 = end_dt.year
        while m2 <= 0:
            m2 += 12
            y2 -= 1
        window_start = date(y2, m2, 1)
        if row_date < window_start or row_date > end_dt:
            continue

        u_type    = str(d.get('U_TYPE', '') or '').strip().upper()
        u_sub     = str(d.get('U_Sub_Group', '') or '').strip().upper()
        item_name = str(d.get('ItemName', '') or '').strip().upper()
        u_type, u_sub = _reclassify(u_type, u_sub, item_name)
        if u_sub not in ALLOWED_SUB_GROUPS:
            continue

        litres    = float(d.get('Liter', 0) or 0)
        linetotal = float(d.get('LineTotal', 0) or 0)

        pk = f'{u_type}|{u_sub}'
        if pk not in agg:
            agg[pk] = {'litres': 0.0, 'linetotal': 0.0}
        agg[pk]['litres']    += litres
        agg[pk]['linetotal'] += linetotal

        for dc in DRILL_COLS:
            dim_val = str(d.get(dc, '') or '').strip().upper()
            if not dim_val:
                continue
            dk = f'{pk}|{dc}|{dim_val}'
            if dk not in drill_agg:
                drill_agg[dk] = {'litres': 0.0, 'linetotal': 0.0}
            drill_agg[dk]['litres']    += litres
            drill_agg[dk]['linetotal'] += linetotal

    result = {
        pk: round(v['linetotal'] / v['litres'], 2) if v['litres'] > 0 else 0
        for pk, v in agg.items()
    }
    drill_result = {
        dk: round(v['linetotal'] / v['litres'], 2) if v['litres'] > 0 else 0
        for dk, v in drill_agg.items()
    }
    return result, drill_result


def get_targets_for_month(month, year):
    db_rows = MonthlyTarget.objects.filter(month=month, year=year)
    db_map = {r.key: {'tgt_ltrs': r.tgt_ltrs, 'tgt_rate': r.tgt_rate, 'source': 'saved'}
              for r in db_rows}

    merged = {}
    for key, defaults in DEFAULT_TARGETS.items():
        if key in db_map:
            merged[key] = db_map[key]
        else:
            merged[key] = {
                'tgt_ltrs': defaults['tgt_ltrs'],
                'tgt_rate':  defaults['tgt_rate'],
                'source':    'default',
            }
    for key, val in db_map.items():
        if key not in merged:
            merged[key] = val
    return merged


def save_monthly_targets(updates, month, year, user):
    saved = 0
    for upd in updates:
        key = upd.get('key', '')
        parts = key.split('|', 1)
        if len(parts) != 2:
            continue
        product_type, sub_group = parts[0].strip().upper(), parts[1].strip().upper()
        tgt_ltrs = float(upd.get('tgt_ltrs', 0))
        tgt_rate  = float(upd.get('tgt_rate', 0))

        obj, _ = MonthlyTarget.objects.update_or_create(
            product_type=product_type,
            sub_group=sub_group,
            month=month,
            year=year,
            defaults={'tgt_ltrs': tgt_ltrs, 'tgt_rate': tgt_rate, 'updated_by': user},
        )
        saved += 1
    return saved


def ensure_channel_groups():
    masters = {}
    for name in CHANNEL_GROUPS:
        masters[name], _ = MainGroupMaster.objects.get_or_create(name=name)
    return masters


def get_channel_target_map(month, year, segment=None):
    ensure_channel_groups()
    grouped = {name: Decimal('0') for name in CHANNEL_GROUPS}

    # Prefer the hierarchical Update Targets editor (TargetNode) — the source of
    # truth users edit. Roll each node's target up to its main group.
    node_rows = TargetNode.objects.filter(month=month, year=year)
    seg = _norm_segment(segment)
    if seg:
        node_rows = node_rows.filter(segment=seg)
    if node_rows.exists():
        # A channel-level target (state='') is the channel's headline total and takes
        # precedence; otherwise sum the per-state rollup nodes.
        per_state = {}
        channel_level = {}
        for node in node_rows:
            name = _normalize_name(node.main_group)
            if not name:
                continue
            if not _normalize_name(node.state):
                channel_level[name] = channel_level.get(name, Decimal('0')) + (node.target_ltrs or Decimal('0'))
            else:
                per_state[name] = per_state.get(name, Decimal('0')) + (node.target_ltrs or Decimal('0'))
        for name in set(per_state) | set(channel_level):
            grouped[name] = channel_level[name] if channel_level.get(name, 0) > 0 else per_state.get(name, Decimal('0'))
        return {key: float(val) for key, val in grouped.items()}

    # Prefer the flat per-main-group editor (SegmentTarget).
    segment_rows = SegmentTarget.objects.filter(segment_type='main_group', month=month, year=year)
    if segment_rows.exists():
        for row in segment_rows:
            name = _normalize_name(row.segment_value)
            grouped[name] = grouped.get(name, Decimal('0')) + (row.target_ltrs or Decimal('0'))
        return {key: float(val) for key, val in grouped.items()}

    # Fallback: legacy TargetMaster rows.
    rows = TargetMaster.objects.filter(month=month, year=year).select_related('main_group')
    for row in rows:
        name = (row.main_group.name or '').strip().upper()
        if name not in grouped:
            grouped[name] = Decimal('0')
        grouped[name] += row.target_ltrs or Decimal('0')
    return {key: float(val) for key, val in grouped.items()}


def get_channel_target_rows(month, year):
    data = get_channel_target_map(month, year)
    return [{'name': name, 'target_ltrs': data.get(name, 0.0)} for name in CHANNEL_GROUPS]


def save_channel_targets(month, year, targets):
    masters = ensure_channel_groups()
    saved = 0
    for name in CHANNEL_GROUPS:
        raw_value = targets.get(name, 0)
        try:
            value = Decimal(str(raw_value or 0))
        except Exception:
            value = Decimal('0')
        TargetMaster.objects.update_or_create(
            main_group=masters[name],
            state=None,
            sales_person='',
            month=month,
            year=year,
            defaults={'target_ltrs': value},
        )
        saved += 1
    return saved


# ── Channel Targets editor (set a whole channel's target directly, vs last-month sale) ──
# Display order matches the dashboard's 7 channel cards.
CHANNEL_DISPLAY_ORDER = ['GT', 'MT', 'ROI', 'ECOM', 'HORECA', 'CSD', 'REST']


def _prev_month(month, year):
    """Previous calendar month for (month, year)."""
    return (12, year - 1) if month == 1 else (month - 1, year)


def month_date_range(month, year):
    """First and last day (inclusive) of a calendar month as 'YYYY-MM-DD' strings."""
    from calendar import monthrange
    last = monthrange(year, month)[1]
    return f'{year:04d}-{month:02d}-01', f'{year:04d}-{month:02d}-{last:02d}'


def _seg_stats(pair):
    """[litres, linetotal] -> {'litres', 'realise'}."""
    return {'litres': round(pair[0], 2),
            'realise': round(pair[1] / pair[0], 2) if pair[0] > 0 else 0}


def _get_channel_actuals_uncached(start_date, end_date):
    """Per display-channel actual oil litres + realise (₹/L) for a date range, split by
    Premium / Commodity — the same rows the dashboard counts, bucketed by display channel."""
    _, raw = get_sales_data_cached(start_date, end_date)
    agg = {}   # channel -> {'PREMIUM':[ltrs,linetotal], 'COMMODITY':[...]}
    for d in (raw or []):
        u_type = str(d.get('U_TYPE', '') or '').strip().upper()
        u_sub  = str(d.get('U_Sub_Group', '') or '').strip().upper()
        item   = str(d.get('ItemName', '') or '').strip().upper()
        u_type, u_sub = _reclassify(u_type, u_sub, item)
        if u_type not in ('PREMIUM', 'COMMODITY'):
            continue
        if u_sub not in ALLOWED_SUB_GROUPS:
            continue
        ch = _raw_to_channel(d.get('U_Main_Group'))
        seg = agg.setdefault(ch, {'PREMIUM': [0.0, 0.0], 'COMMODITY': [0.0, 0.0]})
        s = seg[u_type]
        s[0] += float(d.get('Liter', 0) or 0)
        s[1] += float(d.get('LineTotal', 0) or 0)
    out = {}
    for ch in CHANNEL_DISPLAY_ORDER:
        seg = agg.get(ch, {'PREMIUM': [0.0, 0.0], 'COMMODITY': [0.0, 0.0]})
        tl = seg['PREMIUM'][0] + seg['COMMODITY'][0]
        tr = seg['PREMIUM'][1] + seg['COMMODITY'][1]
        out[ch] = {'premium': _seg_stats(seg['PREMIUM']),
                   'commodity': _seg_stats(seg['COMMODITY']),
                   'litres': round(tl, 2),
                   'realise': round(tr / tl, 2) if tl > 0 else 0}
    return out


def get_channel_node_targets(month, year):
    """Current channel-level targets per channel, split by Premium / Commodity. Stored as
    state-blank TargetNodes with segment PREMIUM / COMMODITY."""
    out = {ch: {'premium_ltrs': 0.0, 'premium_realise': 0.0,
                'commodity_ltrs': 0.0, 'commodity_realise': 0.0} for ch in CHANNEL_DISPLAY_ORDER}
    for n in TargetNode.objects.filter(month=month, year=year, state='', sales_person='',
                                       segment__in=['PREMIUM', 'COMMODITY']):
        ch = _normalize_name(n.main_group)
        if ch not in out:
            continue
        pref = 'premium' if n.segment == 'PREMIUM' else 'commodity'
        out[ch][pref + '_ltrs'] = float(n.target_ltrs or 0)
        out[ch][pref + '_realise'] = float(n.target_realise or 0)
    return out


def save_channel_node_targets(month, year, items, user=None):
    """Upsert channel-level Premium + Commodity TargetNodes (state='', person='',
    segment=PREMIUM/COMMODITY) per channel. A segment with both litres and realise zero is
    deleted (clears that channel-segment target)."""
    saved = 0
    for it in (items or []):
        ch = _normalize_name(it.get('channel'))
        if ch not in CHANNEL_DISPLAY_ORDER:
            continue
        for seg, lk, rk in (('PREMIUM', 'premium_ltrs', 'premium_realise'),
                            ('COMMODITY', 'commodity_ltrs', 'commodity_realise')):
            try:
                ltrs = Decimal(str(it.get(lk) or 0))
                rlz  = Decimal(str(it.get(rk) or 0))
            except Exception:
                continue
            if ltrs <= 0 and rlz <= 0:
                TargetNode.objects.filter(main_group=ch, state='', sales_person='', segment=seg,
                                          month=month, year=year).delete()
                continue
            TargetNode.objects.update_or_create(
                main_group=ch, state='', sales_person='', segment=seg, month=month, year=year,
                defaults={'target_ltrs': ltrs, 'target_realise': rlz})
            saved += 1
    return saved


def get_channel_quick_payload(month, year):
    """Everything the Channel Targets editor needs: last calendar month's actual sale per
    channel (split Premium/Commodity) + each channel's current channel-level targets."""
    pm, py = _prev_month(month, year)
    start, end = month_date_range(pm, py)
    actuals = get_channel_actuals(start, end)
    targets = get_channel_node_targets(month, year)
    rows = []
    for ch in CHANNEL_DISPLAY_ORDER:
        a = actuals.get(ch, {})
        t = targets.get(ch, {})
        p = a.get('premium', {'litres': 0, 'realise': 0})
        c = a.get('commodity', {'litres': 0, 'realise': 0})
        rows.append({'channel': ch,
                     'last_litres': a.get('litres', 0), 'last_realise': a.get('realise', 0),
                     'last_premium_ltrs': p['litres'], 'last_premium_realise': p['realise'],
                     'last_commodity_ltrs': c['litres'], 'last_commodity_realise': c['realise'],
                     'premium_ltrs': t.get('premium_ltrs', 0), 'premium_realise': t.get('premium_realise', 0),
                     'commodity_ltrs': t.get('commodity_ltrs', 0), 'commodity_realise': t.get('commodity_realise', 0)})
    return {'rows': rows, 'last_month': pm, 'last_year': py, 'month': month, 'year': year}


def get_product_actuals(channel, state, start_date, end_date):
    """Last-month actual litres + realise per product (keyed 'P#SUB' / 'C#SUB', matching the
    target editor's product ids) for ONE (channel, state) — the per-product, per-state
    equivalent of get_channel_actuals. Merged sub-groups fold into their parent product."""
    _, raw = get_sales_data_cached(start_date, end_date)
    ch = _normalize_name(channel)
    st = _normalize_name(state)
    agg = {}
    for d in (raw or []):
        u_type = str(d.get('U_TYPE', '') or '').strip().upper()
        u_sub  = str(d.get('U_Sub_Group', '') or '').strip().upper()
        item   = str(d.get('ItemName', '') or '').strip().upper()
        u_type, u_sub = _reclassify(u_type, u_sub, item)
        if u_type not in ('PREMIUM', 'COMMODITY'):
            continue
        if u_sub not in ALLOWED_SUB_GROUPS:
            continue
        if u_sub == 'EXTRA VIRGIN OLIVE':        # folded into the OLIVE card (PRODUCT_MERGED)
            u_sub = 'OLIVE'
        if _raw_to_channel(d.get('U_Main_Group')) != ch:
            continue
        if _normalize_name(d.get('State')) != st:
            continue
        code = 'P' if u_type == 'PREMIUM' else 'C'
        a = agg.setdefault(code + '#' + u_sub, [0.0, 0.0])
        a[0] += float(d.get('Liter', 0) or 0)
        a[1] += float(d.get('LineTotal', 0) or 0)
    return {k: {'litres': round(l, 2), 'realise': round(lt / l, 2) if l > 0 else 0}
            for k, (l, lt) in agg.items()}


def get_product_actuals_payload(channel, state, month, year):
    """Previous calendar month's per-product actual sale for a (channel, state)."""
    pm, py = _prev_month(month, year)
    start, end = month_date_range(pm, py)
    return {'products': get_product_actuals(channel, state, start, end),
            'last_month': pm, 'last_year': py}


def _normalize_name(value):
    return str(value or '').strip().upper()


def _item_label(code, name):
    """Combine an item code + name into the 'CODE — NAME' string shown everywhere an item is
    displayed. Falls back to just the name (or code, or '—') when one side is missing. Kept
    consistent across the aggregation rows AND the drill-to-document queries so the modal's
    item filter still matches after the label is combined."""
    code = str(code or '').strip()
    name = str(name or '').strip()
    if code and name:
        return f'{code} — {name}'
    return name or code or '—'


def get_target_editor_options(raw_rows=None):
    ensure_channel_groups()
    state_names = {_normalize_name(row.name) for row in StateMaster.objects.all() if row.name}
    sales_people = {_normalize_name(row.sales_person) for row in TargetMaster.objects.exclude(sales_person__isnull=True).exclude(sales_person__exact='') if row.sales_person}

    raw_rows = raw_rows or []
    sales_keys = ['U_SALES_PERSON', 'U_Sales_Person', 'SALES_PERSON', 'SalesPerson', 'SlpName']
    for row in raw_rows:
        state_name = _normalize_name(row.get('State'))
        if state_name:
            state_names.add(state_name)
        for key in sales_keys:
            sales_name = _normalize_name(row.get(key))
            if sales_name:
                sales_people.add(sales_name)
                break

    return {
        'main_groups': CHANNEL_GROUPS,
        'states': sorted(state_names),
        'sales_people': sorted(sales_people),
    }


def get_target_entries(month, year):
    ensure_channel_groups()
    rows = TargetMaster.objects.filter(month=month, year=year).select_related('main_group', 'state').order_by('main_group__name', 'state__name', 'sales_person')
    data = []
    for row in rows:
        state_name = row.state.name if row.state_id else ''
        sales_person = row.sales_person or ''
        if state_name and sales_person:
            level = 'state_sales'
        elif state_name:
            level = 'state'
        elif sales_person:
            level = 'sales_person'
        else:
            level = 'main_group'
        data.append({
            'main_group': row.main_group.name,
            'state': state_name,
            'sales_person': sales_person,
            'target_ltrs': float(row.target_ltrs or 0),
            'level': level,
        })
    return data


def save_target_entry(month, year, entry):
    masters = ensure_channel_groups()
    main_group_name = _normalize_name(entry.get('main_group'))
    if main_group_name not in masters:
        raise ValueError('Invalid main group')

    state_name = _normalize_name(entry.get('state'))
    sales_person = _normalize_name(entry.get('sales_person'))
    level = _normalize_name(entry.get('level')) or 'MAIN_GROUP'
    try:
        target_ltrs = Decimal(str(entry.get('target_ltrs') or 0))
    except Exception as exc:
        raise ValueError('Invalid target litres') from exc

    if level == 'MAIN_GROUP':
        state_name = ''
        sales_person = ''
    elif level == 'STATE':
        if not state_name:
            raise ValueError('State is required')
        sales_person = ''
    elif level == 'SALES_PERSON':
        if not sales_person:
            raise ValueError('Sales person is required')
        state_name = ''
    elif level == 'STATE_SALES':
        if not state_name or not sales_person:
            raise ValueError('State and sales person are required')
    else:
        raise ValueError('Invalid target level')

    state_obj = None
    if state_name:
        state_obj, _ = StateMaster.objects.get_or_create(name=state_name)

    TargetMaster.objects.update_or_create(
        main_group=masters[main_group_name],
        state=state_obj,
        sales_person=sales_person,
        month=month,
        year=year,
        defaults={'target_ltrs': target_ltrs},
    )


# ---------------------------------------------------------------------------
# Segment targets (flat per-dimension editor: Main Group / State / Person)
# ---------------------------------------------------------------------------

SEGMENT_TYPES = list(SegmentTarget.SEGMENT_TYPES)
_SEGMENT_KEYS = {key for key, _ in SEGMENT_TYPES}


def get_segment_value_list(segment_type, raw_rows=None):
    """Return the ordered list of values to show for the chosen dimension."""
    if segment_type not in _SEGMENT_KEYS:
        segment_type = 'main_group'

    if segment_type == 'main_group':
        return list(CHANNEL_GROUPS)

    options = get_target_editor_options(raw_rows)
    if segment_type == 'state':
        values = set(options['states'])
    elif segment_type == 'person':
        values = set(options['sales_people'])
    else:
        target_type = 'PREMIUM' if segment_type == 'premium_item' else 'COMMODITY'
        values = set()
        for row in (raw_rows or []):
            row_type = _normalize_name(row.get('U_TYPE'))
            if row_type != target_type:
                continue
            item_name = _normalize_name(row.get('ItemName'))
            if item_name:
                values.add(item_name)

    # Include anything already saved so previously-entered rows never disappear.
    for value in SegmentTarget.objects.filter(segment_type=segment_type).values_list('segment_value', flat=True):
        cleaned = _normalize_name(value)
        if cleaned:
            values.add(cleaned)

    return sorted(values)


def get_segment_target_rows(segment_type, month, year, raw_rows=None):
    """Every value for the dimension with its saved ltrs / realise value (0 if unset)."""
    if segment_type not in _SEGMENT_KEYS:
        segment_type = 'main_group'

    saved = {}
    for row in SegmentTarget.objects.filter(segment_type=segment_type, month=month, year=year):
        saved[_normalize_name(row.segment_value)] = row

    rows = []
    for value in get_segment_value_list(segment_type, raw_rows):
        existing = saved.get(_normalize_name(value))
        rows.append({
            'value': value,
            'target_ltrs': float(existing.target_ltrs) if existing else 0.0,
            'target_realise_value': float(existing.target_realise_value) if existing else 0.0,
        })
    return rows


_SEGMENT_TO_NODE_FIELD = {'main_group': 'main_group', 'state': 'state', 'person': 'sales_person'}


def get_segment_target_map(segment_type, month, year, segment=None):
    if segment_type not in _SEGMENT_KEYS:
        return {}

    # Prefer the hierarchical Update Targets editor (TargetNode) — aggregate its
    # node targets onto the requested dimension so the dashboard mirrors it.
    node_field = _SEGMENT_TO_NODE_FIELD.get(segment_type)
    if node_field:
        node_rows = TargetNode.objects.filter(month=month, year=year)
        seg = _norm_segment(segment)
        if seg:
            node_rows = node_rows.filter(segment=seg)
        if node_rows.exists():
            data = {}
            for node in node_rows:
                key = _normalize_name(getattr(node, node_field))
                if not key:
                    continue
                data[key] = data.get(key, 0.0) + float(node.target_ltrs or 0)
            return data

    data = {}
    for row in SegmentTarget.objects.filter(segment_type=segment_type, month=month, year=year):
        key = _normalize_name(row.segment_value)
        if not key:
            continue
        data[key] = float(row.target_ltrs or 0)
    return data


def save_segment_targets(segment_type, month, year, entries):
    """Upsert a list of {value, target_ltrs, target_realise_value} for one dimension."""
    if segment_type not in _SEGMENT_KEYS:
        raise ValueError('Invalid segment type')

    def _decimal(raw):
        try:
            return Decimal(str(raw or 0))
        except Exception:
            return Decimal('0')

    saved = 0
    for entry in entries:
        value = _normalize_name(entry.get('value'))
        if not value:
            continue
        SegmentTarget.objects.update_or_create(
            segment_type=segment_type,
            segment_value=value,
            month=month,
            year=year,
            defaults={
                'target_ltrs': _decimal(entry.get('target_ltrs')),
                'target_realise_value': _decimal(entry.get('target_realise_value')),
            },
        )
        saved += 1
    return saved


# ---------------------------------------------------------------------------
# Hierarchical free-form target editor (TEST) — no auto-splitting.
# Three dimensions (main group / state / sales person) that can be nested in
# any order. A target may be entered at any level.
# ---------------------------------------------------------------------------

TEST_SALES_PERSONS = ['PRINCE', 'HAPPY', 'TARUN']
TEST_STATES = ['PUNJAB', 'HARYANA', 'DELHI']

HIER_ORDERS = [
    ('mg_state_sp', 'Main Group › State › Sales Person'),
    ('sp_mg_state', 'Sales Person › Main Group › State'),
    ('state_mg_sp', 'State › Main Group › Sales Person'),
]
_HIER_ORDER_DIMS = {
    'mg_state_sp': ['main_group', 'state', 'sales_person'],
    'sp_mg_state': ['sales_person', 'main_group', 'state'],
    'state_mg_sp': ['state', 'main_group', 'sales_person'],
}
_HIER_DIM_LABELS = {'main_group': 'Main Group', 'state': 'State', 'sales_person': 'Sales Person'}


def _hier_dim_values(dim):
    if dim == 'main_group':
        return list(CHANNEL_GROUPS)
    if dim == 'state':
        return list(TEST_STATES)
    return list(TEST_SALES_PERSONS)


def _hier_key(combo):
    return '|'.join([combo.get('main_group', ''), combo.get('state', ''), combo.get('sales_person', '')])


def _fmt_ltrs(value):
    f = float(value or 0)
    if f == 0:
        return ''
    return str(int(f)) if f == int(f) else str(f)


def get_hier_rows(order_key, month, year):
    """Return a fully-expanded, pre-order flat list of tree nodes for the chosen ordering."""
    dims = _HIER_ORDER_DIMS.get(order_key) or _HIER_ORDER_DIMS['mg_state_sp']

    saved = {}
    for node in TargetNode.objects.filter(month=month, year=year):
        saved[(node.main_group, node.state, node.sales_person)] = node.target_ltrs

    rows = []

    def recurse(level, combo):
        dim = dims[level]
        for val in _hier_dim_values(dim):
            child = dict(combo)
            child[dim] = val
            triple = (child.get('main_group', ''), child.get('state', ''), child.get('sales_person', ''))
            rows.append({
                'depth': level,
                'indent': 16 + level * 26,
                'label': val,
                'dim': dim,
                'dim_label': _HIER_DIM_LABELS[dim],
                'key': _hier_key(child),
                'value': _fmt_ltrs(saved.get(triple)),
            })
            if level + 1 < len(dims):
                recurse(level + 1, child)

    recurse(0, {'main_group': '', 'state': '', 'sales_person': ''})
    return rows


def _to_decimal(raw):
    try:
        return Decimal(str(raw)) if str(raw).strip() else Decimal('0')
    except Exception:
        return Decimal('0')


def _to_float(raw):
    """_to_decimal's twin for arithmetic. Blank, None and junk all read as 0.0 — every
    caller here is doing money maths where a missing input means 'not entered', not an
    error to raise."""
    try:
        s = str(raw).strip()
        return float(s) if s else 0.0
    except Exception:
        return 0.0


SEGMENT_CHOICES = ('PREMIUM', 'COMMODITY')


def _norm_segment(value):
    v = _normalize_name(value)
    return v if v in SEGMENT_CHOICES else ''


def save_hier_targets(month, year, triples, segment=''):
    """Persist (key, ltrs, realise) triples where key = 'mainGroup|state|salesPerson',
    scoped to a product segment ('' = all). Blank/zero on both metrics clears the node.

    The "All" view (segment == '') spans every segment: it shows Premium/Commodity
    rows merged, so its saves must reach those same rows. Clearing a node removes it
    from all segments; a value updates whichever segment rows the node already lives
    in (so a Premium target stays Premium) or, when none exist, creates a
    segment-agnostic row. A specific segment scopes both reads and writes to itself."""
    segment = _norm_segment(segment)
    all_view = segment == ''
    saved = 0
    for key, raw_ltrs, raw_realise in triples:
        parts = (key or '').split('|')
        if len(parts) != 3:
            continue
        mg = _normalize_name(parts[0])
        state = _normalize_name(parts[1])
        sp = _normalize_name(parts[2])
        if not (mg or state or sp):
            continue
        # Stamp the full (group, state, person) identity from the territory sheet
        # so the target reflects on the Main Group cards and in every drill order.
        mg, state, sp = complete_target_triple(mg, state, sp)
        ltrs = _to_decimal(raw_ltrs)
        realise = _to_decimal(raw_realise)
        cleared = ltrs <= 0 and realise <= 0
        node_qs = TargetNode.objects.filter(main_group=mg, state=state,
                                            sales_person=sp, month=month, year=year)
        if all_view:
            if cleared:
                node_qs.delete()
                continue
            updated = node_qs.update(target_ltrs=max(ltrs, Decimal('0')),
                                     target_realise=max(realise, Decimal('0')))
            if not updated:
                TargetNode.objects.create(
                    main_group=mg, state=state, sales_person=sp, segment='',
                    month=month, year=year,
                    target_ltrs=max(ltrs, Decimal('0')),
                    target_realise=max(realise, Decimal('0')))
            saved += 1
            continue
        # Segment-specific view: only touch that segment's row.
        if cleared:
            node_qs.filter(segment=segment).delete()
            continue
        TargetNode.objects.update_or_create(
            main_group=mg, state=state, sales_person=sp, segment=segment, month=month, year=year,
            defaults={'target_ltrs': max(ltrs, Decimal('0')), 'target_realise': max(realise, Decimal('0'))},
        )
        saved += 1
    return saved


# ---------------------------------------------------------------------------
# OCRD-backed hierarchy overrides
# ---------------------------------------------------------------------------

HIER_ORDERS = [
    ('mg_state_sp', 'Main Group > State > Person'),
    ('state_mg_sp', 'State > Main Group > Person'),
    ('sp_mg_state', 'Person > Main Group > State'),
]
_HIER_ORDER_DIMS = {
    'mg_state_sp': ['main_group', 'state', 'sales_person'],
    'state_mg_sp': ['state', 'main_group', 'sales_person'],
    'sp_mg_state': ['sales_person', 'main_group', 'state'],
}
_HIER_DIM_LABELS = {'main_group': 'Main Group', 'state': 'State', 'sales_person': 'Person'}


def get_ocrd_master_rows():
    sql = '''
        SELECT DISTINCT
            COALESCE(TRIM("U_Main_Group"), '') AS "U_Main_Group",
            COALESCE(TRIM("State1"), '') AS "State1",
            COALESCE(TRIM("CntctPrsn"), '') AS "CntctPrsn"
        FROM "JIVO_OIL_HANADB"."OCRD"
        WHERE COALESCE(TRIM("U_Main_Group"), '') <> ''
    '''
    try:
        rows = sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[OCRD] master fetch failed: %s', exc)
        rows = []

    cleaned = []
    seen = set()
    for row in rows:
        item = {
            'main_group': _normalize_name(row.get('U_Main_Group')),
            'state': _normalize_name(row.get('State1')),
            'sales_person': _normalize_name(row.get('CntctPrsn')),
        }
        if not item['main_group']:
            continue
        key = (item['main_group'], item['state'], item['sales_person'])
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    return cleaned


# OCRD "State1" holds two-letter codes; the territory sheet uses full names.
# This map lets the tree show readable state names instead of codes.
STATE_CODE_NAMES = {
    'DL': 'DELHI', 'HR': 'HARYANA', 'KT': 'KARNATAKA', 'MH': 'MAHARASHTRA',
    'RJ': 'RAJASTHAN', 'HP': 'HIMACHAL PRADESH', 'GJ': 'GUJARAT',
    'UP': 'UTTAR PRADESH', 'PB': 'PUNJAB', 'JH': 'JHARKHAND',
    'MP': 'MADHYA PRADESH', 'WB': 'WEST BENGAL', 'CA': 'CHHATTISGARH',
    'TE': 'TELANGANA', 'AP': 'ANDHRA PRADESH', 'UK': 'UTTARAKHAND',
    'JK': 'JAMMU AND KASHMIR', 'KR': 'KERALA', 'AS': 'ASSAM', 'TN': 'TAMIL NADU',
    'BH': 'BIHAR', 'NSW': 'NEW SOUTH WALES', 'GO': 'GOA', 'CH': 'CHANDIGARH',
    'AZ': 'MIZORAM', 'CT': 'CHHATTISGARH', 'NG': 'NAGALAND',
    'DN': 'DADRA & NAGAR HAVELI', 'AN': 'ANDAMAN & NICOBAR',
    'WA': 'WESTERN AUSTRALIA', 'GL': 'MEGHALAYA', 'AD': 'ANDHRA PRADESH',
    'OD': 'ODISHA', 'MZ': 'MIZORAM', 'MN': 'MANIPUR', 'TO': 'TORONTO (CANADA)',
    'DB': 'DUBAI', 'AUS': 'AUSTRALIA',
}

# ── Territory ground truth ───────────────────────────────────────────────
# Single source of truth: (main_group, state_code, state_name, person).
# Drives the Update Targets editor rows, the dashboard person drill, the
# GT/MT channel state lists, and the open-order (order-in-hand) roll-up.
# Groups/states not listed here have no owner (REST = channel total, OTHERS).
OTHERS_LABEL = 'OTHERS'
TERRITORY_SHEET = [
    ('GT',     'DL', 'DELHI',         'SUNNY JI'),
    ('GT',     'PB', 'PUNJAB',        'RAMINDER JI'),
    ('GT',     'RJ', 'RAJASTHAN',     'RAMINDER JI'),
    ('GT',     'HR', 'HARYANA',       'TANJEET JI'),
    ('GT',     'UP', 'UTTAR PRADESH', 'RAVINDER CHADHA JI'),
    ('GT',     'UK', 'UTTARAKHAND',   'TANJEET JI'),
    ('MT',     'DL', 'DELHI',         'PRINCE'),
    ('MT',     'PB', 'PUNJAB',        'PRINCE'),
    ('MT',     'HR', 'HARYANA',       'PRINCE'),
    ('ROI',    'KT', 'KARNATAKA',     'PRINCE'),
    ('ROI',    'TE', 'TELANGANA',     'PRINCE'),
    ('ROI',    'RJ', 'RAJASTHAN',     'PRINCE'),
    ('ROI',    'MH', 'MAHARASHTRA',   'HAPPY'),
    ('ROI',    'GJ', 'GUJARAT',       'HAPPY'),
    ('ROI',    'GO', 'GOA',           'HAPPY'),
    ('ROI',    'WB', 'WEST BENGAL',   'TARUN'),
    ('ROI',    'AS', 'ASSAM',         'TARUN'),
    ('HORECA', 'DL', 'DELHI',         'RAVINDER CHADHA JI'),
]

# Derived lookups.
_PERSON_ASSIGNMENTS = {(g, code): person for (g, code, name, person) in TERRITORY_SHEET}
# By state NAME, for completing partially-keyed targets (each pair is unique).
_GROUP_BY_STATE_PERSON = {(name, person): g for (g, code, name, person) in TERRITORY_SHEET}
_PERSON_BY_GROUP_STATE = {(g, name): person for (g, code, name, person) in TERRITORY_SHEET}

# National channels owned by a single person regardless of state. These groups have
# no per-state territory row, so they resolve to their owner by main group alone —
# this is what makes their sales attribute to the owner in the Sales-Person drill.
CHANNEL_OWNERS = {
    'E-COMMERCE': 'PRABHU SIR',
    'CSD': 'SACHIN STEPHEN',
}


# ── DB-backed territory mapping (TerritoryMapping) ─────────────────────────
# TERRITORY_SHEET / CHANNEL_OWNERS above are now only the SEED + fallback. The
# live source of truth is the TerritoryMapping table (editable in the Person
# Mapping tab). Reads are cached for a few seconds because the derived lookups
# are hit on hot dashboard paths (channel-detail, order-in-hand).
_TERRITORY_CACHE = {'exp': 0.0, 'derived': None}
_TERRITORY_TTL = 60


def invalidate_territory_cache():
    """Drop the cached territory lookups (call after any mapping write)."""
    _TERRITORY_CACHE['derived'] = None
    _TERRITORY_CACHE['exp'] = 0.0


def _raw_to_channel(raw_group):
    """Raw SAP U_Main_Group -> 7-channel display name. Unknown groups pass through."""
    g = _normalize_name(raw_group)
    for channel, members in CHANNEL_MEMBERS.items():
        if g in members:
            return channel
    return g


def _territory_effective_rows():
    """List of {channel, state_code, state_name, sales_person}. Falls back to the
    hardcoded TERRITORY_SHEET + CHANNEL_OWNERS when the DB table is empty (fresh
    install / not yet seeded), so the dashboard behaves identically pre-seed."""
    rows = list(TerritoryMapping.objects.all()
                .values('channel', 'state_code', 'state_name', 'sales_person'))
    if rows:
        return rows
    fallback = []
    for (group, code, name, person) in TERRITORY_SHEET:
        fallback.append({'channel': group, 'state_code': code,
                         'state_name': name, 'sales_person': person})
    for raw_group, person in CHANNEL_OWNERS.items():
        fallback.append({'channel': _raw_to_channel(raw_group), 'state_code': '',
                         'state_name': '', 'sales_person': person})
    return fallback


def _territory_derived():
    """Cached derived lookups built from the effective territory rows."""
    now = time.time()
    cache = _TERRITORY_CACHE
    if cache['derived'] is not None and cache['exp'] > now:
        return cache['derived']

    rows = _territory_effective_rows()
    d = {
        'rows': rows,
        'person_by_ch_state_name': {},   # (channel, state_name) -> person
        'person_by_ch_state_code': {},   # (channel, state_code) -> person
        'person_by_channel': {},         # channel -> national/blank-state owner
        'group_by_state_person': {},     # (state_name, person) -> channel
        'persons_order': [],
    }
    seen = set()
    for r in rows:
        ch = _normalize_name(r['channel'])
        name = _normalize_name(r['state_name'])
        code = _normalize_name(r['state_code'])
        person = _normalize_name(r['sales_person'])
        if name and person:
            d['person_by_ch_state_name'][(ch, name)] = person
            d['group_by_state_person'].setdefault((name, person), ch)
        if code and person:
            d['person_by_ch_state_code'][(ch, code)] = person
        if not name and person:
            d['person_by_channel'][ch] = person
        if person and person not in seen:
            seen.add(person)
            d['persons_order'].append(person)

    # Single-owner channels are NATIONAL: a channel whose per-state cells all name the same one
    # person (e.g. HORECA handled only by RAVINDER) is owned by them in EVERY state — the exact
    # rule get_territory_dashboard_payload() uses for the dashboard. Mirror it here so the Python
    # attribution (Required Credit, Order-in-Hand rows, …) resolves a state with no explicit owner
    # to that person too, instead of leaving it blank. setdefault never overrides an explicit
    # blank-state (channel-level) owner.
    _ch_owners = {}
    for r in rows:
        ch = _normalize_name(r['channel']); nm = _normalize_name(r['state_name']); pr = _normalize_name(r['sales_person'])
        if nm and pr:
            _ch_owners.setdefault(ch, set()).add(pr)
    for ch, ps in _ch_owners.items():
        if len(ps) == 1:
            d['person_by_channel'].setdefault(ch, next(iter(ps)))

    # City-level overrides: a (channel, state) territory split among multiple ASMs by
    # ship-to city. A matching city wins over the territory's default owner.
    d['person_by_ch_state_city'] = {}   # (channel, state_name, city) -> person
    for co in CityOwner.objects.all().values('channel', 'state_name', 'city', 'sales_person'):
        person = _normalize_name(co['sales_person'])
        if not person:
            continue
        key = (_normalize_name(co['channel']), _normalize_name(co['state_name']), _normalize_name(co['city']))
        d['person_by_ch_state_city'][key] = person
        if person not in seen:
            seen.add(person)
            d['persons_order'].append(person)

    cache['derived'] = d
    cache['exp'] = now + _TERRITORY_TTL
    return d


def person_for_group_state(group, state_name, city=''):
    """Territory owner for a (raw group, state[, city]); resolves the raw SAP main group
    to its dashboard channel. A city-level owner (CityOwner) wins when the ship-to city
    matches, so a territory can be split among multiple ASMs; otherwise it falls back to
    the territory's default owner, then the channel-level (national) owner."""
    channel = _raw_to_channel(group)
    state_name = _normalize_name(state_name)
    d = _territory_derived()
    city = _normalize_name(city)
    if city:
        owner = d['person_by_ch_state_city'].get((channel, state_name, city))
        if owner:
            return owner
    return (d['person_by_ch_state_name'].get((channel, state_name))
            or d['person_by_channel'].get(channel)
            or '')


def complete_target_triple(group, state, person):
    """Fill a blank group/person from the territory sheet when the other two
    fields identify exactly one cell. Lets a target keep its full (group, state,
    person) identity no matter which drill order was used to enter it."""
    group = _normalize_name(group)
    state = _normalize_name(state)
    person = _normalize_name(person)
    d = _territory_derived()
    if state and person and not group:
        group = d['group_by_state_person'].get((state, person), group)
    if group and state and not person:
        person = d['person_by_ch_state_name'].get((group, state), person)
    return group, state, person


def normalize_target_nodes(month=None, year=None):
    """Backfill group/person on existing TargetNode rows via the territory sheet.
    Idempotent; returns the number of rows rewritten."""
    qs = TargetNode.objects.all()
    if month:
        qs = qs.filter(month=month)
    if year:
        qs = qs.filter(year=year)
    changed = 0
    for node in list(qs):
        g, s, p = complete_target_triple(node.main_group, node.state, node.sales_person)
        if (g, s, p) == (node.main_group, node.state, node.sales_person):
            continue
        TargetNode.objects.update_or_create(
            main_group=g, state=s, sales_person=p, month=node.month, year=node.year,
            defaults={'target_ltrs': node.target_ltrs})
        node.delete()
        changed += 1
    return changed


def _assigned_persons_in_order():
    return list(_territory_derived()['persons_order'])


# REST-segment groups (no person/state owner) — targetable at group level in the
# editor. HORECA is omitted here because it already appears via the territory sheet.
REST_GROUPS = ['CSD', 'E-COMMERCE', 'CASH SALE', 'CORPORATE', 'SANGAT',
               'BRANCH', 'STAFF', 'REFERENCE', 'PURCHASE OIL']


def get_territory_master_rows():
    """Editor rows built from the live TerritoryMapping table (one row per
    channel+state+person). Drives the Update Targets editor hierarchy. DB-backed,
    so reassigning a person in the Person Mapping tab reflows here too."""
    rows, seen = [], set()
    for r in _territory_derived()['rows']:
        ch = _normalize_name(r['channel'])
        name = _normalize_name(r['state_name'])
        person = _normalize_name(r['sales_person'])
        key = (ch, name, person)
        if key in seen:
            continue
        seen.add(key)
        rows.append({'main_group': ch, 'state': name, 'sales_person': person})
    return rows


def get_territory_dashboard_payload():
    """Mapping the dashboard JS uses to remap live sales/orders onto persons and to
    fix the GT/MT channel state lists. DB-backed (TerritoryMapping). person_map is
    keyed by both the channel and each underlying raw group ('ECOM|DELHI' AND
    'E-COMMERCE|DELHI') so callers that pass the raw SAP main group still resolve."""
    d = _territory_derived()
    person_map = {}   # "GROUP|STATENAME" -> person
    whitelist = {}    # channel -> [{label, match[]}]
    group_owners = {}
    # A channel with exactly ONE distinct per-state owner is treated as a single-owner NATIONAL
    # channel (like REST / E-Commerce / CSD): that person owns the whole channel across EVERY state
    # and no per-state whitelist is built (which would otherwise hide other states). GT / MT / ROI
    # etc. with several owners stay per-state.
    _ch_owners = {}
    for r in d['rows']:
        ch = _normalize_name(r['channel']); nm = _normalize_name(r['state_name']); pr = _normalize_name(r['sales_person'])
        if nm and pr:
            _ch_owners.setdefault(ch, set()).add(pr)
    single_owner = {ch for ch, ps in _ch_owners.items() if len(ps) == 1}
    for r in d['rows']:
        channel = _normalize_name(r['channel'])
        name = _normalize_name(r['state_name'])
        code = _normalize_name(r['state_code'])
        person = _normalize_name(r['sales_person'])
        if name and person:
            person_map[channel + '|' + name] = person
            for raw in CHANNEL_MEMBERS.get(channel, [channel]):
                person_map[_normalize_name(raw) + '|' + name] = person
            if channel == 'REST' or channel in single_owner:
                # REST (a group-drilled umbrella) OR any single-owner channel (e.g. HORECA handled
                # only by RAVINDER): the person owns the WHOLE channel nationally (like E-Commerce/
                # CSD). Resolve every member group to them via group_owners, and do NOT build a
                # per-state whitelist — that would otherwise restrict the card/modal to just the
                # tagged state and drop the channel's sales in every other state.
                group_owners.setdefault(channel, person)
                for raw in CHANNEL_MEMBERS.get(channel, [channel]):
                    group_owners.setdefault(_normalize_name(raw), person)
            else:
                # Build the per-channel whitelist for EVERY channel that has a per-state
                # owner (not just GT/MT) so an assigned state always shows as a card row in
                # that channel — even with zero live Done. The dashboard treats this list
                # additively (union with live sales states), so nothing is dropped.
                bucket = whitelist.setdefault(channel, [])
                if not any(e['label'] == name for e in bucket):
                    bucket.append({'label': name, 'match': [name] + ([code] if code else [])})
        if not name and person:
            # Key by the CHANNEL name too ('ECOM'), not just its raw member groups
            # ('E-COMMERCE'): a channel-level TargetNode stores main_group='ECOM', so the
            # person drill's assignedPerson('ECOM','') must resolve the owner. Without this,
            # a whole-channel ECOM target falls into "Other" instead of its owner.
            group_owners[channel] = person
            for raw in CHANNEL_MEMBERS.get(channel, [channel]):
                group_owners[_normalize_name(raw)] = person
    # City overrides: "GROUP|STATE|CITY" -> person (keyed by channel + each raw group),
    # so the dashboard can attribute a sale to its city's ASM before the territory owner.
    city_map = {}
    for (channel, state, city), person in d['person_by_ch_state_city'].items():
        city_map[channel + '|' + state + '|' + city] = person
        for raw in CHANNEL_MEMBERS.get(channel, [channel]):
            city_map[_normalize_name(raw) + '|' + state + '|' + city] = person
    return {
        'persons': d['persons_order'],
        'map': person_map,
        'city_map': city_map,
        'whitelist': whitelist,
        'group_owners': group_owners,
    }


def _open_order_qty_by_group_code():
    """{(main_group, state_code): open_litres} from live open sales orders (SO).

    Open qty is in pieces; we convert to LITRES the same way REPORT_SALES_ANALYSIS
    derives its Liter column — Liter = Quantity * OITM.SalPackUn (litres per piece,
    e.g. 5 for a "5 LTR" pack, 14.2857 for a 13 KGS tin) — so Order-in-Hand is
    directly comparable to Done litres in the dashboard."""
    sql = f'''
        SELECT COALESCE(TRIM(C."U_Main_Group"), '') AS "GRP",
               COALESCE(TRIM(C."State1"), '')       AS "ST",
               SUM(L."OpenQty" * COALESCE(I."SalPackUn", 0)) AS "OPEN_QTY"
        FROM "{SAP_SCHEMA}"."ORDR" H
        JOIN "{SAP_SCHEMA}"."RDR1" L ON L."DocEntry" = H."DocEntry"
        JOIN "{SAP_SCHEMA}"."OCRD" C ON C."CardCode" = H."CardCode"
        LEFT JOIN "{SAP_SCHEMA}"."OITM" I ON I."ItemCode" = L."ItemCode"
        WHERE H."DocStatus" = 'O' AND L."LineStatus" = 'O'
        GROUP BY COALESCE(TRIM(C."U_Main_Group"), ''), COALESCE(TRIM(C."State1"), '')
    '''
    try:
        rows = sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[OIH] open-order fetch failed: %s', exc)
        return {}
    out = {}
    for row in rows:
        out[(_normalize_name(row.get('GRP')), _normalize_name(row.get('ST')))] = float(row.get('OPEN_QTY') or 0)
    return out


DASHBOARD_CHANNELS = ['GT', 'MT', 'ROI', 'ECOM', 'HORECA', 'CSD', 'REST']


def get_territory_map_payload():
    """Shape the TerritoryMapping grid for the Person Mapping UI: the fixed cells
    (channel + state, read-only) with their current editable person, the channel
    order, and the distinct people list (for the reassign dropdown)."""
    d = _territory_derived()
    cells = []
    people = set()
    for r in d['rows']:
        channel = _normalize_name(r['channel'])
        cells.append({
            'channel': channel,
            'state_code': _normalize_name(r['state_code']),
            'state_name': _normalize_name(r['state_name']),
            'sales_person': _normalize_name(r['sales_person']),
        })
        if r['sales_person']:
            people.add(_normalize_name(r['sales_person']))
    cells.sort(key=lambda c: (DASHBOARD_CHANNELS.index(c['channel'])
                              if c['channel'] in DASHBOARD_CHANNELS else 99,
                              c['state_name']))
    present = {c['channel'] for c in cells}
    channels = [c for c in DASHBOARD_CHANNELS if c in present] + \
               sorted(present - set(DASHBOARD_CHANNELS))
    return {'channels': channels, 'people': sorted(people), 'cells': cells}


def save_territory_persons(assignments, user=None):
    """Upsert the sales_person of each (channel, state_name) cell. Existing cells are
    updated; NEW (channel, state) territories the user adds are created (the grid is
    extensible). The channel is normalised from a raw SAP group to its dashboard
    channel (e.g. 'E-COMMERCE' -> 'ECOM') so added cells line up with the dashboard
    cards. Returns the number of rows created or changed."""
    name_to_code = {v: k for k, v in STATE_CODE_NAMES.items()}
    saved = 0
    for entry in assignments or []:
        channel = _raw_to_channel(_normalize_name(entry.get('channel')))
        state_name = _normalize_name(entry.get('state_name'))
        person = _normalize_name(entry.get('sales_person'))
        if not channel:
            continue
        obj, created = TerritoryMapping.objects.get_or_create(
            channel=channel, state_name=state_name,
            defaults={'state_code': name_to_code.get(state_name, ''),
                      'sales_person': person, 'updated_by': user})
        if created:
            saved += 1
            continue
        if obj.sales_person != person:
            obj.sales_person = person
            obj.updated_by = user
            obj.save(update_fields=['sales_person', 'updated_by', 'updated_at'])
            # Re-own any existing targets for this cell so the person-level target
            # views / drills follow the reassignment too (across all periods).
            TargetNode.objects.filter(main_group=channel, state=state_name).update(sales_person=person)
            saved += 1
    if saved:
        invalidate_territory_cache()
    return saved


def get_territory_targets(month, year):
    """{'CHANNEL|STATE': target_ltrs} — single target per (channel, state) territory
    for a period, read from the saved TargetNode rows (segment-agnostic roll-up).
    Keyed to match the Person Mapping UI's keyOf(channel, state)."""
    out = {}
    for node in TargetNode.objects.filter(month=month, year=year):
        ch = _normalize_name(node.main_group)
        state = _normalize_name(node.state)
        if not ch:
            continue
        key = f'{ch}|{state}'
        out[key] = out.get(key, 0.0) + float(node.target_ltrs or 0)
    return out


def save_territory_targets(month, year, items):
    """Upsert one target (litres) per (channel, state) territory into TargetNode,
    stamping the owning person from the territory map and PRESERVING any existing
    target_realise. Blank/zero clears the node. Returns rows written."""
    saved = 0
    for item in items or []:
        channel = _normalize_name(item.get('channel'))
        state = _normalize_name(item.get('state_name') or item.get('state'))
        if not channel:
            continue
        ltrs = _to_decimal(item.get('target_ltrs'))
        # Stamp the owner so the target reflects in the person drill / channel cards.
        mg, st, sp = complete_target_triple(channel, state, '')
        node_qs = TargetNode.objects.filter(main_group=mg, state=st, sales_person=sp,
                                            month=month, year=year)
        if ltrs <= 0:
            if node_qs.exists():
                node_qs.update(target_ltrs=Decimal('0'))
                saved += 1
            continue
        existing = node_qs.first()
        realise = existing.target_realise if existing else Decimal('0')
        TargetNode.objects.update_or_create(
            main_group=mg, state=st, sales_person=sp, segment='',
            month=month, year=year,
            defaults={'target_ltrs': ltrs, 'target_realise': realise or Decimal('0')},
        )
        saved += 1
    return saved


# Sub-groups that are folded into a parent product in the target editor (shown under
# the parent, not as their own card). 'EXTRA VIRGIN OLIVE' is part of the OLIVE family.
# Kept as a map rather than a bare set so the child -> parent name lives in ONE place:
# get_product_actuals and get_variety_items both have to apply the same fold, and a
# hard-coded string in each would drift.
PRODUCT_MERGE_PARENT = {'EXTRA VIRGIN OLIVE': 'OLIVE'}
PRODUCT_MERGED = set(PRODUCT_MERGE_PARENT)


def get_product_master():
    """Canonical product list for the 'Set product targets' UI: each sub_group with
    its type code ('P' = Premium, 'C' = Commodity). Sourced from DEFAULT_TARGETS so it
    matches the dashboard's known products / sub-groups. Merged sub-groups
    (PRODUCT_MERGED) are excluded so they don't appear as separate cards."""
    out = []
    for key in DEFAULT_TARGETS:
        ptype, sub = key.split('|', 1)
        if sub in PRODUCT_MERGED:
            continue
        out.append({'name': sub, 'type': 'P' if ptype == 'PREMIUM' else 'C'})
    out.sort(key=lambda p: (p['type'] != 'P', p['name']))
    return out


def get_territory_product_targets(month, year):
    """{'CHANNEL||STATE': {'P#SUBGROUP': {'l': ltrs, 'r': realise}, ...}} — the exact
    shape the Person Mapping UI consumes (keyOf = channel||state, pid = type#name)."""
    out = {}
    for r in TerritoryProductTarget.objects.filter(month=month, year=year):
        k = f'{_normalize_name(r.channel)}||{_normalize_name(r.state_name)}'
        code = 'P' if r.product_type == 'PREMIUM' else 'C'
        pid = f'{code}#{_normalize_name(r.sub_group)}'
        out.setdefault(k, {})[pid] = {
            'l': float(r.target_ltrs or 0),
            'r': float(r.target_realise or 0),
        }
    return out


def item_target_realise(retailer, pcs_per_box, box_litres, scheme, ss=0, dm=0, gst=0, disc=0):
    """The Realise Calculator's rate, for one item target. Same formula as addedRealise()
    in plan_vs_done.html, so an item target and the same item drafted as a plan on the
    Plan vs Done card price identically:

        ((retailer / (1+ss%) / (1+dm%) / (1+gst%)) * pcs_per_box - disc) / (box_litres + scheme)

    Returns 0.0 when the row cannot be priced (no retailer, no pack config, or a zero
    denominator) rather than raising - a half-filled row is a normal editing state."""
    ret = _to_float(retailer)
    pb = _to_float(pcs_per_box)
    total_ltr = _to_float(box_litres) + _to_float(scheme)
    if ret <= 0 or pb <= 0 or total_ltr <= 0:
        return 0.0
    ex = ret
    for pct in (ss, dm, gst):
        ex = ex / (1.0 + _to_float(pct) / 100.0)
    return round((ex * pb - _to_float(disc)) / total_ltr, 2)


def get_variety_items():
    """The FG item master regrouped under the target editor's VARIETY CARDS:
    {'P#CANOLA': [{code, name, sku, box_litres, pcs_per_box}, ...]}.

    Built on get_realise_calc_items(), which already carries pcs_per_box and box_litres -
    the two numbers the pricing row needs - but returns RAW SAP values. So the same three
    folds the cards themselves use are applied here, or items land under a card that does
    not exist and their targets roll into nothing, invisibly:
      • _reclassify        - YELLOW MUSTARD is Premium, not the commodity MUSTARD card
      • PRODUCT_MERGE_PARENT - EXTRA VIRGIN OLIVE items belong on the OLIVE card
      • ALLOWED_SUB_GROUPS - anything else has no card to sit on
    Plus FG-only: get_done_by_item is FG-only, so a target on a non-FG item could never
    show a Done figure and would read as a permanent 0% miss."""
    payload = get_realise_calc_items()
    out = {}
    for it in payload.get('items') or []:
        code = (it.get('code') or '').strip().upper()
        if not code.startswith('FG'):
            continue
        seg = 'PREMIUM' if it.get('type') == 'P' else 'COMMODITY'
        seg, sub = _reclassify(seg, (it.get('variety') or '').upper(), it.get('name') or '')
        sub = PRODUCT_MERGE_PARENT.get(sub, sub)
        if sub not in ALLOWED_SUB_GROUPS:
            continue
        pid = ('P' if seg == 'PREMIUM' else 'C') + '#' + sub
        out.setdefault(pid, []).append({
            'code': code,
            'name': it.get('name') or code,
            'sku': it.get('sku') or '',
            'box_litres': it.get('box_litres') or 0,
            'pcs_per_box': it.get('pcs_per_box') or 0,
        })
    for rows in out.values():
        rows.sort(key=lambda r: r['name'])
    return out


def _fold_item_targets(month, year):
    """{'CHANNEL||STATE': {'P#SUBGROUP': {'l': litres, 'r': volume-weighted rate}}} — the
    period's item rows summed to the variety card they sit under.

    This is what REPLACES a hand-typed variety target: clicking into the item screen is an
    explicit statement of finer intent, so the children win. Read from the DB rather than
    from the request so an old client and a new one fold identically."""
    acc = {}
    for r in TerritoryItemTarget.objects.filter(month=month, year=year):
        ltrs = float(r.target_ltrs or 0)
        if ltrs <= 0:
            continue
        key = f'{_normalize_name(r.channel)}||{_normalize_name(r.state_name)}'
        code = 'P' if r.product_type == 'PREMIUM' else 'C'
        pid = f'{code}#{_normalize_name(r.sub_group)}'
        cell = acc.setdefault(key, {}).setdefault(pid, [0.0, 0.0])
        cell[0] += ltrs
        cell[1] += ltrs * float(r.target_realise or 0)
    return {k: {p: {'l': round(v[0], 2), 'r': round(v[1] / v[0], 2) if v[0] else 0.0}
                for p, v in pm.items()}
            for k, pm in acc.items()}


def get_territory_item_targets_editor(month, year):
    """The item rows in the exact shape the Update Targets screen POSTs them back in, so
    load and save are symmetric and there is no transform to get wrong in one direction:
    {'CHANNEL||STATE': {ITEMCODE: {l, r, ret, sch, bl, pb, ss, dm, gst, dsc, sub, t, nm}}}."""
    out = {}
    for r in TerritoryItemTarget.objects.filter(month=month, year=year):
        key = f'{_normalize_name(r.channel)}||{_normalize_name(r.state_name)}'
        out.setdefault(key, {})[r.item_code] = {
            'l': float(r.target_ltrs or 0), 'r': float(r.target_realise or 0),
            'ret': float(r.retailer or 0), 'sch': float(r.scheme or 0),
            'bl': float(r.box_litres or 0), 'pb': float(r.pcs_per_box or 0),
            'ss': float(r.ss_pct or 0), 'dm': float(r.dm_pct or 0),
            'gst': float(r.gst_pct or 0), 'dsc': float(r.disc or 0),
            'sub': _normalize_name(r.sub_group),
            't': 'P' if r.product_type == 'PREMIUM' else 'C',
            'nm': r.item_name or r.item_code,
        }
    return out


def get_territory_item_targets(month, year):
    """Item targets for Plan vs Done: {'CHANNEL|STATE': {ITEMCODE: {...}}}.

    The item twin of get_territory_targets, keyed the same way (single '|', normalised
    channel and state) so the page's existing key split works unchanged. 'value'
    (= ltrs x rate) is precomputed so All India can pool rates BY VOLUME instead of
    averaging them, the same mistake get_territory_targets guards against."""
    out = {}
    for r in TerritoryItemTarget.objects.filter(month=month, year=year):
        ch = _normalize_name(r.channel)
        st = norm_state(r.state_name)
        if not ch or not st:
            continue
        ltrs = float(r.target_ltrs or 0)
        rate = float(r.target_realise or 0)
        if ltrs <= 0 and rate <= 0:
            continue
        out.setdefault(ch + '|' + st, {})[r.item_code] = {
            'ltrs': round(ltrs, 2), 'rate': round(rate, 2),
            'value': round(ltrs * rate, 2),
            'retailer': float(r.retailer or 0), 'scheme': float(r.scheme or 0),
            'boxltr': float(r.box_litres or 0), 'pcsbox': float(r.pcs_per_box or 0),
            'name': r.item_name or r.item_code,
            'seg': r.product_type, 'variety': _normalize_name(r.sub_group),
        }
    return out


def _save_item_targets(month, year, items_obj, user=None):
    """Replace the period's item rows from the editor payload. Returns the row count.

    The posted rate is recomputed from the posted inputs rather than trusted - the same
    rule api_rate_list_add_items follows - so a stale bundle cannot file a rate that does
    not follow from its own numbers. Variety and segment are re-derived from the item
    master where possible, falling back to what the client sent."""
    TerritoryItemTarget.objects.filter(month=month, year=year).delete()
    # code -> (segment, sub_group) straight off the same fold the cards use
    lookup = {}
    for pid, rows in (get_variety_items() or {}).items():
        code, sub = pid.split('#', 1)
        seg = 'PREMIUM' if code == 'P' else 'COMMODITY'
        for it in rows:
            lookup[it['code']] = (seg, sub)

    saved = 0
    for key, cell in (items_obj or {}).items():
        if '||' not in str(key):
            continue
        channel, state = str(key).split('||', 1)
        channel, state = _normalize_name(channel), _normalize_name(state)
        if not channel:
            continue
        _, _, person = complete_target_triple(channel, state, '')
        for raw_code, val in (cell or {}).items():
            code = str(raw_code or '').strip().upper()
            if not code or not isinstance(val, dict):
                continue
            ltrs = _to_float(val.get('l'))
            if ltrs <= 0:
                continue                       # a row with no volume is not a target
            seg, sub = lookup.get(code, (
                'PREMIUM' if str(val.get('t') or 'P').upper() == 'P' else 'COMMODITY',
                _normalize_name(val.get('sub') or ''),
            ))
            if not sub:
                continue
            rate = item_target_realise(val.get('ret'), val.get('pb'), val.get('bl'),
                                       val.get('sch'), val.get('ss'), val.get('dm'),
                                       val.get('gst'), val.get('dsc'))
            TerritoryItemTarget.objects.create(
                channel=channel, state_name=state, sales_person=person,
                item_code=code, item_name=str(val.get('nm') or code)[:200],
                product_type=seg, sub_group=sub, month=month, year=year,
                retailer=_to_decimal(val.get('ret')), scheme=_to_decimal(val.get('sch')),
                box_litres=_to_decimal(val.get('bl')), pcs_per_box=_to_decimal(val.get('pb')),
                ss_pct=_to_decimal(val.get('ss')), dm_pct=_to_decimal(val.get('dm')),
                gst_pct=_to_decimal(val.get('gst')), disc=_to_decimal(val.get('dsc')),
                target_ltrs=_to_decimal(ltrs), target_realise=_to_decimal(rate),
                updated_by=user)
            saved += 1
    return saved


def _rebuild_target_rollups(month, year):
    """Recompute the dashboard's target rows for a period from TerritoryProductTarget:
      • TargetNode  — one row per (channel, state, segment) = sum litres + litres-
        weighted realise. The dashboard's channel TGT-L and Premium/Commodity toggle
        read these (authoritative: the period's TargetNode rows are replaced).
      • MonthlyTarget — per (product_type, sub_group) totals across all territories,
        feeding the slide-1 product TARGET SALE / TARGET REALISE (upsert-only)."""
    rows = list(TerritoryProductTarget.objects.filter(month=month, year=year))

    cell_seg = {}   # (channel, state, segment) -> [sum_l, sum_l*r]
    prod = {}       # (product_type, sub_group) -> [sum_l, sum_l*r]
    for r in rows:
        l = float(r.target_ltrs or 0)
        rate = float(r.target_realise or 0)
        seg = r.product_type
        a = cell_seg.setdefault((_normalize_name(r.channel), _normalize_name(r.state_name), seg), [0.0, 0.0])
        a[0] += l; a[1] += l * rate
        # Aggregate (state-card) targets count toward the channel/state segment total
        # above, but must NOT become a phantom per-product MonthlyTarget row.
        if _normalize_name(r.sub_group) != AGG_SUBGROUP:
            b = prod.setdefault((r.product_type, _normalize_name(r.sub_group)), [0.0, 0.0])
            b[0] += l; b[1] += l * rate

    # TargetNode: rebuild this period's per-(channel,state,segment) rollup rows from
    # scratch (the per-product UI is their source of truth). PRESERVE channel-level
    # targets (state='', person='' — any segment, incl. Premium/Commodity) which are set
    # independently in the channel cards and must survive a product-target save.
    TargetNode.objects.filter(month=month, year=year)\
        .exclude(state='', sales_person='').delete()
    for (channel, state, seg), (suml, sumlr) in cell_seg.items():
        if suml <= 0:
            continue
        mg, st, sp = complete_target_triple(channel, state, '')
        realise = sumlr / suml if suml else 0
        TargetNode.objects.update_or_create(
            main_group=mg, state=st, sales_person=sp, segment=seg, month=month, year=year,
            defaults={'target_ltrs': Decimal(str(round(suml, 2))),
                      'target_realise': Decimal(str(round(realise, 2)))})

    # MonthlyTarget: per-product totals (upsert; leaves products not edited here intact).
    for (ptype, sub), (suml, sumlr) in prod.items():
        rate = sumlr / suml if suml else 0
        MonthlyTarget.objects.update_or_create(
            product_type=ptype, sub_group=sub, month=month, year=year,
            defaults={'tgt_ltrs': round(suml, 2), 'tgt_rate': round(rate, 2)})


def save_territory_product_targets(month, year, targets_obj, user=None, items_obj=None):
    """Replace a period's per-product territory targets with the submitted set (the UI
    always holds the full set), then rebuild the dashboard roll-ups. targets_obj shape:
    {'CHANNEL||STATE': {'P#SUBGROUP': {'l': ltrs, 'r': realise}, ...}}.

    items_obj carries the ITEM-level targets in the same request, because both grains have
    to be in front of _rebuild_target_rollups at once: it deletes and rebuilds the period's
    TargetNode rows, so two separate saves would leave the dashboard reading a half-updated
    picture until the second one landed - permanently, if it never did.
      items_obj is None -> leave existing item rows alone (an older cached bundle posts no
                           'items' key and must not wipe them)
      items_obj == {}   -> clear the period's item rows
      items_obj set     -> full replace, like targets_obj

    Where a variety has item rows, their sum REPLACES whatever litres/realise the variety
    card carries. Returns the product-row count.

    Atomic: two blind deletes run here, and a failure between them would leave item rows
    gone, product rows half-written and the roll-ups never rebuilt."""
    with transaction.atomic():
        return _save_territory_product_targets(month, year, targets_obj, user, items_obj)


def _save_territory_product_targets(month, year, targets_obj, user, items_obj):
    if items_obj is not None:
        _save_item_targets(month, year, items_obj, user)

    # Read the fold back from the DB, not from the request, so the numbers a client sees
    # and the numbers stored can never diverge on a stale bundle.
    derived = _fold_item_targets(month, year)
    merged = {k: dict(v or {}) for k, v in (targets_obj or {}).items()}
    for key, prodmap in derived.items():
        cell = merged.setdefault(key, {})
        for pid_key, val in prodmap.items():
            cell[pid_key] = val
            # The state card's whole-segment aggregate and a derived variety would both
            # roll into the same (channel, state, segment) TargetNode. Drop the aggregate.
            cell.pop(pid_key.split('#', 1)[0] + '#' + AGG_SUBGROUP, None)
    targets_obj = merged

    TerritoryProductTarget.objects.filter(month=month, year=year).delete()
    saved = 0
    for key, prodmap in (targets_obj or {}).items():
        if '||' not in str(key):
            continue
        channel, state = key.split('||', 1)
        channel, state = _normalize_name(channel), _normalize_name(state)
        if not channel:
            continue
        for pidkey, val in (prodmap or {}).items():
            if '#' not in str(pidkey):
                continue
            code, sub = pidkey.split('#', 1)
            ptype = 'PREMIUM' if code.strip().upper() == 'P' else 'COMMODITY'
            sub = _normalize_name(sub)
            if not sub:
                continue
            ltrs = _to_decimal(val.get('l') if isinstance(val, dict) else val)
            realise = _to_decimal(val.get('r') if isinstance(val, dict) else 0)
            if ltrs <= 0 and realise <= 0:
                continue
            # Stamp the owning person from the territory map (channel+state -> person)
            # so each row carries its full main-group / state / person / product identity.
            _, _, person = complete_target_triple(channel, state, '')
            TerritoryProductTarget.objects.create(
                channel=channel, state_name=state, sales_person=person,
                product_type=ptype, sub_group=sub,
                month=month, year=year, target_ltrs=ltrs, target_realise=realise,
                updated_by=user)
            saved += 1
    _rebuild_target_rollups(month, year)
    return saved


def get_order_in_hand_by_person():
    """Open-order LITRES per territory owner (assigned only). Live snapshot. Resolves
    the raw SAP main group + state code to its owner via the TerritoryMapping table."""
    d = _territory_derived()
    data = {p: 0.0 for p in d['persons_order']}
    for (group, code), qty in _open_order_qty_by_group_code().items():
        channel = _raw_to_channel(group)
        person = (d['person_by_ch_state_code'].get((channel, _normalize_name(code)))
                  or d['person_by_channel'].get(channel))
        if person:
            data[person] = data.get(person, 0.0) + qty
    return data


def _open_order_litres_by_group_code_customer():
    """[{GRP, ST, CUST, SUBG, UTYPE, OPEN_QTY, OPEN_VAL}] open-order litres AND value,
    split by customer and by product/type (from the order line's item) so the dashboard
    can filter Order-in-Hand by Premium/Commodity the same way Done is filtered.
    State/city come from the order's ship-to address (CRD1), not the BP-master HQ.

    OPEN_VAL pro-rates the still-open share of each line's LineTotal, so a part-delivered
    order only counts what is still to go. It is EX-GST on purpose: Done's line_total
    (from REPORT_SALES_ANALYSIS) is ex-GST, and the OIH realise (₹/litre) is meant to be
    read next to the Done realise. The Required Credit Limit report deliberately uses the
    tax-INCLUSIVE version instead, because that has to tie to the customer ledger."""
    sql = f'''
        SELECT COALESCE(TRIM(C."U_Main_Group"), '') AS "GRP",
               {_SHIPTO_STATE} AS "ST",
               {_SHIPTO_CITY} AS "CITY",
               COALESCE(TRIM(H."CardCode"), '')      AS "CCODE",
               COALESCE(TRIM(C."CardName"), '')      AS "CUST",
               COALESCE(TRIM(I."U_Sub_Group"), '')   AS "SUBG",
               COALESCE(TRIM(I."U_TYPE"), '')        AS "UTYPE",
               COALESCE(TRIM(I."ItemName"), '')      AS "ITEM",
               COALESCE(TRIM(I."U_SKU"), '')         AS "SKU",
               SUM(L."OpenQty" * COALESCE(I."SalPackUn", 0)) AS "OPEN_QTY",
               SUM(CASE WHEN L."Quantity" <> 0
                        THEN L."OpenQty" / L."Quantity" * L."LineTotal"
                        ELSE 0 END)                   AS "OPEN_VAL"
        FROM "{SAP_SCHEMA}"."ORDR" H
        JOIN "{SAP_SCHEMA}"."RDR1" L ON L."DocEntry" = H."DocEntry"
        JOIN "{SAP_SCHEMA}"."OCRD" C ON C."CardCode" = H."CardCode"
        LEFT JOIN "{SAP_SCHEMA}"."OITM" I ON I."ItemCode" = L."ItemCode"
        {_SHIPTO_JOIN.format(S=SAP_SCHEMA)}
        WHERE H."DocStatus" = 'O' AND L."LineStatus" = 'O'
        GROUP BY COALESCE(TRIM(C."U_Main_Group"), ''), {_SHIPTO_STATE}, {_SHIPTO_CITY},
                 COALESCE(TRIM(H."CardCode"), ''),
                 COALESCE(TRIM(C."CardName"), ''), COALESCE(TRIM(I."U_Sub_Group"), ''),
                 COALESCE(TRIM(I."U_TYPE"), ''), COALESCE(TRIM(I."ItemName"), ''),
                 COALESCE(TRIM(I."U_SKU"), '')
    '''
    try:
        return sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[OIH] open-order (by customer) fetch failed: %s', exc)
        return []


def get_order_in_hand_rows():
    """Granular open-order rows: {main_group, state(name), sales_person, card_name,
    u_type, u_sub_group, item_name, open_qty, open_value}. open_qty is in LITRES
    (Quantity * OITM.SalPackUn), matching Done; open_value is the still-open ₹ of those
    lines (ex-GST, like Done's line_total) so the dashboard can show an OIH realise. u_type/u_sub_group/item_name let the dashboard
    split Order-in-Hand by segment, product, and item; card_name attributes to a real
    buyer. State comes from the order's ship-to address (CRD1) so it matches Done
    (OCRD.State1 is unreliable for national accounts); person follows that state."""
    rows = []
    for d in _open_order_litres_by_group_code_customer():
        group = _normalize_name(d.get('GRP'))
        state_name = _delhi_gt_state(d.get('CCODE'), _state_name(d))
        rows.append({
            'main_group': group,
            'state': state_name,
            'sales_person': person_for_group_state(group, state_name),
            'card_name': _normalize_name(d.get('CUST')),
            'u_type': _normalize_name(d.get('UTYPE')),
            'u_sub_group': _normalize_name(d.get('SUBG')),
            'item_name': _normalize_name(d.get('ITEM')),
            'sku': _normalize_name(d.get('SKU')),
            'open_qty': float(d.get('OPEN_QTY') or 0),
            'open_value': float(d.get('OPEN_VAL') or 0),
        })
    return rows


# ── Required Credit Limit report (Order-in-Hand by ASM → party) ─────────────
def _required_credit_open_rows():
    """Open sales-order lines grouped by (SO number, party, main group, ship-to state,
    segment) with the open litres AND open value (₹). Litres = OpenQty × SalPackUn
    (matches Done / OIH). Value pro-rates the still-open fraction of the row's
    tax-INCLUSIVE total (LineTotal + VatSum, i.e. with GST), so Open Value / Total
    Outstanding / Required Limit tie to what SAP bills and to the GST-inclusive ledger
    balance. A partially-delivered order contributes only its undelivered amount."""
    sql = f'''
        SELECT H."DocNum"                            AS "DOCNUM",
               COALESCE(TRIM(C."U_Main_Group"), '')  AS "GRP",
               {_SHIPTO_STATE}                       AS "ST",
               {_SHIPTO_CITY}                        AS "CITY",
               COALESCE(TRIM(H."CardCode"), '')      AS "CCODE",
               COALESCE(TRIM(C."CardName"), '')      AS "CUST",
               COALESCE(C."Balance", 0)              AS "BAL",
               COALESCE(H."DocTotal", 0)             AS "DOCTOTAL",
               COALESCE(TRIM(I."U_TYPE"), '')        AS "UTYPE",
               COALESCE(TRIM(I."U_Sub_Group"), '')   AS "SUBG",
               SUM(L."OpenQty" * COALESCE(I."SalPackUn", 0)) AS "OPEN_QTY",
               SUM(CASE WHEN L."Quantity" <> 0
                        THEN L."OpenQty" / L."Quantity" * (L."LineTotal" + COALESCE(L."VatSum", 0))
                        ELSE 0 END)                  AS "OPEN_VAL"
        FROM "{SAP_SCHEMA}"."ORDR" H
        JOIN "{SAP_SCHEMA}"."RDR1" L ON L."DocEntry" = H."DocEntry"
        JOIN "{SAP_SCHEMA}"."OCRD" C ON C."CardCode" = H."CardCode"
        LEFT JOIN "{SAP_SCHEMA}"."OITM" I ON I."ItemCode" = L."ItemCode"
        {_SHIPTO_JOIN.format(S=SAP_SCHEMA)}
        WHERE H."DocStatus" = 'O' AND L."LineStatus" = 'O'
        GROUP BY H."DocNum", COALESCE(TRIM(C."U_Main_Group"), ''),
                 {_SHIPTO_STATE}, {_SHIPTO_CITY},
                 COALESCE(TRIM(H."CardCode"), ''), COALESCE(TRIM(C."CardName"), ''),
                 COALESCE(C."Balance", 0), COALESCE(H."DocTotal", 0),
                 COALESCE(TRIM(I."U_TYPE"), ''), COALESCE(TRIM(I."U_Sub_Group"), '')
    '''
    try:
        return sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[REQCREDIT] open-order fetch failed: %s', exc)
        return []


# Premium sub-groups that roll up to the Canola / Olive category columns; everything
# else premium falls into "Other Premium" (derived as total − canola − olive − commodity).
_OLIVE_TOKENS = ('OLIVE', 'POMACE')


def _blank_bucket():
    return {'litres': {'premium': 0.0, 'commodity': 0.0, 'canola': 0.0, 'olive': 0.0, 'total': 0.0},
            'value':  {'total': 0.0, 'pi_total': 0.0, 'ledger': 0.0, 'outstanding': 0.0,
                       'required_limit': 0.0, 'payment_done': 0.0, 'remaining': 0.0}}


def _round_bucket(b):
    for metric in ('litres', 'value'):
        for key in b[metric]:
            b[metric][key] = round(b[metric][key], 2)
    return b


def _row_key(card_code, state, main_group):
    """Stable identity for a report row. A customer can span several (state, main group)
    rows, so the lock snapshot and the payment split are keyed on all three parts."""
    return '%s|%s|%s' % (card_code or '', state or '', main_group or '')


def _credit_receipts_on(as_of_date):
    """Per-CardCode incoming bank-transfer receipts (SAP ORCT, DocType 'C', amount = TrsfrSum)
    dated ON as_of_date. Returns {card_code: amount}; {} on any SAP error. Powers the Payment
    Done column. The half-open [as_of, as_of+1) range is correct whether DocDate is stored as a
    date or a timestamp."""
    next_day = as_of_date + timedelta(days=1)
    sql = f'''
        SELECT COALESCE(TRIM("CardCode"), '') AS "CCODE",
               SUM(COALESCE("TrsfrSum", 0)) AS "PAID"
        FROM "{SAP_SCHEMA}"."ORCT"
        WHERE "DocType" = 'C' AND "Canceled" = 'N'
          AND "DocDate" >= ? AND "DocDate" < ?
        GROUP BY COALESCE(TRIM("CardCode"), '')
    '''
    try:
        rows = sap_connector.execute_query(sql, (as_of_date, next_day))
        return {_normalize_name(r.get('CCODE')): float(r.get('PAID') or 0) for r in rows}
    except Exception as exc:
        logger.error('[REQCREDIT] receipts fetch failed: %s', exc)
        return {}


# Per-date {card_code: account balance as of that date}, cached like the aging report.


def _credit_ledger_asof(as_of_date):
    """{card_code: customer account balance as of the END of as_of_date}, computed via SAP B1's
    reconciliation engine (the same query that powers Customer Aging; its balance_due ties to
    OCRD.Balance to the rupee). It reverses BOTH invoices and payments dated after the date, so
    it is the true historical ledger — unlike a payments-only roll-back. Cached per date for
    _AGING_TTL seconds. {} on any SAP error (the caller then falls back to the live balance)."""
    key = as_of_date.isoformat()
    now = time.time()
    hit = _shared_get('creditledger', key)
    if hit is not None:
        return hit
    try:
        m = {}
        for r in _load_aging_rows_sap(as_of_date):
            cc = _normalize_name(r.get('code'))
            m[cc] = m.get(cc, 0.0) + float(r.get('balance_due') or 0)
        _shared_set('creditledger', key, m, _AGING_TTL)
        return m
    except Exception as exc:
        logger.error('[REQCREDIT] ledger-as-of fetch failed: %s', exc)
        return {}


# Parties that owe money but have no open sales order (delivered, not yet paid) still belong on
# the sheet so credit exposure stays visible until they pay. They are placed under their REAL ASM
# (territory) — resolved from their main group + ship-to state, exactly like an open-order party —
# rather than a separate section, so e.g. KNOWTABLE (ROI / Karnataka) sits under its owner PRINCE.


def _card_territory(card_codes):
    """{card_code: {'name', 'group', 'state'(code), 'city'}} for the given cards. `group` is the
    customer's OCRD main group; state/city come from the ship-to address of the party's MOST RECENT
    sales order (so a national account is placed by where it ships, matching how open orders group),
    falling back to the OCRD billing address when the card has no orders at all. {} on empty/SAP
    error. Feeds ASM resolution for unpaid parties that have no open order."""
    codes = sorted({_normalize_name(c) for c in (card_codes or []) if _normalize_name(c)})
    if not codes:
        return {}
    inlist = ','.join("'%s'" % c.replace("'", "''") for c in codes)
    S = SAP_SCHEMA
    sql = f'''
        WITH ranked AS (
          SELECT H."CardCode" AS "CC",
                 ROW_NUMBER() OVER (PARTITION BY H."CardCode"
                                    ORDER BY H."DocDate" DESC, H."DocNum" DESC) AS "RN",
                 {_SHIPTO_STATE} AS "ST", {_SHIPTO_CITY} AS "CITY"
          FROM "{S}"."ORDR" H
          JOIN "{S}"."OCRD" C ON C."CardCode" = H."CardCode"
          {_SHIPTO_JOIN.format(S=S)}
          WHERE H."CardCode" IN ({inlist})
        )
        SELECT C."CardCode"                          AS "CC",
               COALESCE(TRIM(C."CardName"), '')       AS "NAME",
               COALESCE(TRIM(C."U_Main_Group"), '')   AS "GRP",
               COALESCE(NULLIF(TRIM(r."ST"), ''),   TRIM(C."State1"), '') AS "ST",
               COALESCE(NULLIF(TRIM(r."CITY"), ''), TRIM(C."City"),  '') AS "CITY"
        FROM "{S}"."OCRD" C
        LEFT JOIN ranked r ON r."CC" = C."CardCode" AND r."RN" = 1
        WHERE C."CardCode" IN ({inlist})
    '''
    try:
        return {_normalize_name(r.get('CC')): {
                    'name':  _normalize_name(r.get('NAME')),
                    'group': _normalize_name(r.get('GRP')),
                    'state': _normalize_name(r.get('ST')),
                    'city':  _normalize_name(r.get('CITY')),
                } for r in sap_connector.execute_query(sql)}
    except Exception as exc:
        logger.error('[REQCREDIT] card-territory fetch failed: %s', exc)
        return {}


def get_active_credit_lock():
    """The current active CreditLock, or None. A lock past its lock_until date auto-clears
    (the freeze lapses and the columns revert to live SAP on the next read)."""
    today = timezone.localdate()
    lock = CreditLock.objects.filter(active=True).order_by('-locked_at').first()
    if lock and lock.lock_until < today:
        CreditLock.objects.filter(active=True).update(active=False)
        return None
    return lock


def _lock_info(lock):
    """Serializable lock state for the template / API, or None when nothing is frozen."""
    if not lock:
        return None
    today = timezone.localdate()
    return {
        'active': True,
        'locked_at': timezone.localtime(lock.locked_at).isoformat(),
        'lock_until': lock.lock_until.isoformat(),
        'days': lock.days,
        'days_left': max((lock.lock_until - today).days, 0),
    }


def create_credit_lock(days, user=None):
    """Snapshot every party row's current (live) Total Outstanding and Required Limit and
    freeze them for `days` days. Replaces any existing active lock. Returns _lock_info."""
    try:
        days = max(1, min(int(days), 3650))
    except (TypeError, ValueError):
        days = 30
    payload = get_required_credit_rows(_apply_lock=False)        # capture live values
    CreditLock.objects.filter(active=True).update(active=False)
    lock = CreditLock.objects.create(
        lock_until=timezone.localdate() + timedelta(days=days), days=days, active=True,
        created_by=user if (user and getattr(user, 'is_authenticated', False)) else None)
    snaps = []
    for g in payload.get('asms', []):
        for r in g.get('rows', []):
            snaps.append(CreditLockSnapshot(
                lock=lock,
                row_key=_row_key(r['card_code'], r['state'], r['main_group']),
                card_code=r['card_code'],
                outstanding=float(r['value'].get('outstanding') or 0),
                required_limit=float(r['value'].get('required_limit') or 0)))
    if snaps:
        CreditLockSnapshot.objects.bulk_create(snaps)
    return _lock_info(lock)


def clear_credit_lock():
    """Lift any active lock early — the columns revert to live SAP immediately."""
    CreditLock.objects.filter(active=True).update(active=False)


def _freeze_credit_lock(buckets, lock):
    """Freeze Total Outstanding and Required Limit to the lock's snapshot (per row, where a
    snapshot exists). Rows with no snapshot — parties that appeared after the lock — stay on
    live SAP values. Payment Done is handled separately: it is independent of the lock."""
    snaps = {s.row_key: s for s in lock.snapshots.all()}
    for b in buckets:
        s = snaps.get(_row_key(b['card_code'], b['state'], b['main_group']))
        if s:
            b['value']['outstanding'] = s.outstanding
            b['value']['required_limit'] = s.required_limit


def _apply_payment_done(buckets, receipts_on):
    """Fill Payment Done and Outstanding (remaining) on every row — always, independent of
    any credit lock. Payment Done is the customer's ORCT receipts (TrsfrSum) dated ON the
    selected date (receipts_on[card]), matched by CardCode (NOT by SO number) and spread
    across whichever rows the customer currently has, in proportion to each row's Total
    Outstanding. Matching at the customer level means the payment still lands even when the
    party's rows change (new open orders, different states/main groups, more SO numbers).

    Outstanding (remaining) and the payment split are computed on the LIVE outstanding
    (OIH Revenue + the date-based Ledger), NOT the lock-frozen Total Outstanding. The frozen
    snapshot can already reflect a receipt (it is captured from a post-payment ledger), so
    subtracting Payment Done from it would double-count the payment — which is what made the
    locked Outstanding go wildly negative."""
    def live_out(b):
        return (b['value'].get('total') or 0.0) + (b['value'].get('ledger') or 0.0)
    rows_by_card = {}
    for b in buckets:
        rows_by_card.setdefault(b['card_code'], []).append(b)
    for card, rows in rows_by_card.items():
        paid = receipts_on.get(card, 0.0)
        tot = sum(live_out(b) for b in rows)
        for i, b in enumerate(rows):
            lo = live_out(b)
            pay = (paid * lo / tot) if tot > 0 else (paid if i == 0 else 0.0)
            b['value']['payment_done'] = pay
            b['value']['remaining'] = lo - pay


def get_required_credit_rows(_apply_lock=True, as_of_date=None):
    """Required Credit Limit report data: live Order-in-Hand grouped by ASM (territory
    owner) → party. Each party row carries open litres (total + the Canola / Olive /
    Premium / Commodity splits) and open value (₹), a 'type' tag (P / C / P+C / —) for the
    Type filter, the list of open SO numbers, and its saved (editable) delivery remark.
    Returns ASM groups (each with a subtotal) plus a grand total.

    `as_of_date` (default today) drives the date-aware columns: Ledger Amt is the balance AS OF
    that date (so the day's invoices are in it) taken BEFORE that day's collections, and Payment
    Done shows the receipts dated on that date (which reduces Outstanding)."""
    if as_of_date is None:
        as_of_date = timezone.localdate()
    receipts_on = _credit_receipts_on(as_of_date)          # Payment Done = receipts that day
    ledger_asof = _credit_ledger_asof(as_of_date)          # balance AS OF the date (incl. that day's invoices)
    use_aging = bool(ledger_asof)                          # fall back to live OCRD.Balance if SAP aging failed
    # Display state as its short CODE (DL/HR/UP…), but keep the full NAME for ASM
    # resolution (the territory map is keyed by state name). Reverse the code→name map.
    name_to_code = {v: k for k, v in STATE_CODE_NAMES.items()}
    agg = {}
    card_balance = {}      # card_code -> SAP ledger balance (+receivable / -payable), once per card
    for d in _required_credit_open_rows():
        group = _normalize_name(d.get('GRP'))
        state_name = _delhi_gt_state(d.get('CCODE'), _state_name(d))
        state_code = name_to_code.get(state_name, state_name)
        card_code = _normalize_name(d.get('CCODE'))
        utype = _normalize_name(d.get('UTYPE'))
        subg = _normalize_name(d.get('SUBG'))
        qty = float(d.get('OPEN_QTY') or 0)
        val = float(d.get('OPEN_VAL') or 0)
        docnum = str(d.get('DOCNUM') or '').strip()
        key = (card_code, state_name, group)
        bucket = agg.get(key)
        if bucket is None:
            bucket = agg[key] = {
                'card_code': card_code,
                'party': _normalize_name(d.get('CUST')),
                'main_group': group,
                'state': state_code,
                'asm': person_for_group_state(group, state_name, _normalize_name(d.get('CITY'))) or '',
                '_so': {},          # SO number -> its open value (₹, incl GST) for the SO-list popup
                '_solit': {},       # SO number -> its open litres, for the popup's Litres column
                '_soprem': {},      # SO number -> open premium litres  (popup P/C split)
                '_socomm': {},      # SO number -> open commodity litres (popup P/C split)
                '_sotot': {},       # SO number -> its FULL order total (ORDR.DocTotal, matches SAP)
                **_blank_bucket(),
            }
        card_balance.setdefault(card_code, float(d.get('BAL') or 0))
        if docnum:
            bucket['_so'][docnum] = bucket['_so'].get(docnum, 0.0) + val
            bucket['_solit'][docnum] = bucket['_solit'].get(docnum, 0.0) + qty   # open litres per SO
            if utype == 'PREMIUM':
                bucket['_soprem'][docnum] = bucket['_soprem'].get(docnum, 0.0) + qty
            elif utype == 'COMMODITY':
                bucket['_socomm'][docnum] = bucket['_socomm'].get(docnum, 0.0) + qty
            bucket['_sotot'][docnum] = float(d.get('DOCTOTAL') or 0)   # full SO total, set once per SO
        # total counts every open line; premium / commodity are STRICT (a line whose type is
        # neither only lands in total). Canola / Olive are premium sub-group splits for the
        # export's category columns.
        bucket['litres']['total'] += qty
        bucket['value']['total'] += val
        if utype == 'PREMIUM':
            bucket['litres']['premium'] += qty
            if 'CANOLA' in subg:
                bucket['litres']['canola'] += qty
            elif any(tok in subg for tok in _OLIVE_TOKENS):
                bucket['litres']['olive'] += qty
        elif utype == 'COMMODITY':
            bucket['litres']['commodity'] += qty

    # Parties that owe money but have NO open order (delivered, not yet paid) stay on the sheet
    # until they clear, placed under their REAL ASM (territory) — resolved from main group + ship-to
    # state, the same way an open-order party is — so they sit with the rest of that owner's book.
    # Scope: any card with a positive ledger balance as of the date that isn't already an open-order
    # party. Only when the historical ledger is available (else we can't see beyond open-order parties).
    if use_aging:
        have = {b['card_code'] for b in agg.values()}
        owing = {cc: bal for cc, bal in ledger_asof.items()
                 if cc and cc not in have and bal > 0}      # any unpaid balance (even ₹1) stays visible
        terr = _card_territory(owing.keys()) if owing else {}
        for cc, bal in owing.items():
            t = terr.get(cc, {})
            group = _normalize_name(t.get('group'))
            st_raw = _normalize_name(t.get('state'))
            state_name = _delhi_gt_state(cc, STATE_CODE_NAMES.get(st_raw, st_raw))
            state_code = name_to_code.get(state_name, state_name)
            city = _normalize_name(t.get('city'))
            agg[(cc, state_name, group)] = {
                'card_code': cc,
                'party': _normalize_name(t.get('name')) or cc,
                'main_group': group,
                'state': state_code,
                'asm': person_for_group_state(group, state_name, city) or '',
                'no_open_order': True,          # dues but no live order — 0 OIH, excluded from OIH KPIs
                '_so': {}, '_solit': {}, '_soprem': {}, '_socomm': {}, '_sotot': {},
                **_blank_bucket(),
            }
            card_balance.setdefault(cc, bal)

    # Ledger balance is per CUSTOMER but a customer can span several (state/ASM) rows. Split
    # it across those rows in proportion to open-order value so the column still totals to the
    # real balance (single-row customers get the full amount). Outstanding = open value + ledger.
    card_buckets = {}
    for bucket in agg.values():
        card_buckets.setdefault(bucket['card_code'], []).append(bucket)
    for cc, buckets in card_buckets.items():
        # Ledger Amt = the party's account balance AS OF the selected date (SAP reconciliation
        # engine, reverses only what's dated after the date — so the day's invoices ARE included),
        # taken BEFORE that day's collections: add the day's receipts back so the separate Payment
        # Done column nets Outstanding to the true post-payment balance without double-counting.
        # Falls back to the live OCRD.Balance if the historical query was unavailable.
        base = ledger_asof.get(cc, 0.0) if use_aging else card_balance.get(cc, 0.0)
        ledger = base + receipts_on.get(cc, 0.0)
        total_pi = sum(b['value']['total'] for b in buckets)
        for i, b in enumerate(buckets):
            if total_pi > 0:
                b['value']['ledger'] = ledger * (b['value']['total'] / total_pi)
            else:
                b['value']['ledger'] = ledger if i == 0 else 0.0   # no open value → first row
            b['value']['outstanding'] = b['value']['total'] + b['value']['ledger']
            b['value']['required_limit'] = b['value']['outstanding'] * 1.02   # outstanding + 2%

    try:
        remarks = {r.card_code: r.remark for r in ClosingRemark.objects.all()}
    except Exception:
        remarks = {}

    by_asm = {}
    for bucket in agg.values():
        bucket['value']['pi_total'] = sum(bucket.pop('_sotot').values())   # Σ full SO totals (= SAP total)
        _round_bucket(bucket)
        prem, comm = bucket['litres']['premium'], bucket['litres']['commodity']
        bucket['type'] = 'P+C' if (prem > 0 and comm > 0) else ('P' if prem > 0 else ('C' if comm > 0 else '—'))
        # SO list, largest open value first: so_list carries each SO's amount for the popup;
        # so_nos stays a plain string for the export column and the search/cell display.
        so_items = sorted(bucket.pop('_so').items(), key=lambda kv: (-kv[1], kv[0]))
        solit = bucket.pop('_solit', {}); soprem = bucket.pop('_soprem', {}); socomm = bucket.pop('_socomm', {})
        bucket['so_list'] = [{'no': n, 'value': round(v, 2), 'litres': round(solit.get(n, 0.0), 2),
                              'premium': round(soprem.get(n, 0.0), 2), 'commodity': round(socomm.get(n, 0.0), 2)}
                             for n, v in so_items]
        bucket['so_nos'] = ', '.join(n for n, _ in so_items)
        bucket['remark'] = remarks.get(bucket['card_code'], '')
        by_asm.setdefault(bucket['asm'] or 'UNASSIGNED', []).append(bucket)

    # The lock (when active) freezes ONLY Total Outstanding / Required Limit to the snapshot.
    lock = get_active_credit_lock() if _apply_lock else None
    if lock:
        _freeze_credit_lock(list(agg.values()), lock)
    # Payment Done is independent of the lock: each party's receipts dated ON the selected
    # date. Outstanding = Total Outstanding − Payment Done. Applied before subtotals so both
    # columns roll into the ASM subtotal and grand total.
    _apply_payment_done(list(agg.values()), receipts_on)

    asms = []
    grand = _blank_bucket()
    for asm in sorted(by_asm, key=lambda a: (a == 'UNASSIGNED', a)):   # UNASSIGNED last
        rows = sorted(by_asm[asm], key=lambda r: -r['litres']['total'])
        sub = _blank_bucket()
        for r in rows:
            for metric in ('litres', 'value'):
                for key in sub[metric]:
                    sub[metric][key] += r[metric][key]
                    grand[metric][key] += r[metric][key]
        asms.append({'asm': asm, 'rows': rows, 'subtotal': _round_bucket(sub)})

    return {'asms': asms, 'total': _round_bucket(grand), 'lock': _lock_info(lock),
            'as_of': as_of_date.isoformat()}


def _xlsx_col_letter(idx):
    name = ''
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = chr(65 + rem) + name
    return name


# Style ids — must match the cellXfs order in _render_single_sheet_xlsx's styles.xml.
_ST_TITLE, _ST_HEAD, _ST_TEXT, _ST_NUM, _ST_BTEXT, _ST_BNUM = 0, 1, 2, 3, 4, 5
# Highlighted subtotal (per-ASM) and grand-total rows, so each person's block and its total stand out.
_ST_SUBTXT, _ST_SUBNUM, _ST_GTXT, _ST_GNUM = 6, 7, 8, 9


def _render_single_sheet_xlsx(title, cells, widths, max_row, max_col, freeze_rows=0, grid_from_row=2):
    """Minimal pure-Python .xlsx writer (no third-party deps, mirrors core.simple_xlsx so it
    works on servers without openpyxl). `cells` maps (row, col)→(style, kind, value) where kind
    is 't' (text), 'n' (integer number), or 'f' (formula: value = (formula_str, cached_int)).
    Supports per-column widths (1-based col→width), an
    integer format (#,##0), bold, a gray bold-black header, thin black borders, and freezing the
    top `freeze_rows` rows. Every cell from `grid_from_row` down is bordered (blanks included) so
    the body reads as a gridded table. Style ids match cellXfs: 0 title, 1 header, 2 text,
    3 number, 4 bold text, 5 bold number."""
    import zipfile
    from io import BytesIO
    from xml.sax.saxutils import escape

    rows_xml = []
    for r in range(1, max_row + 1):
        cell_xml = []
        for c in range(1, max_col + 1):
            spec = cells.get((r, c))
            ref = '%s%d' % (_xlsx_col_letter(c), r)
            if spec is not None:
                style, kind, value = spec
                if kind == 'n':
                    cell_xml.append('<c r="%s" s="%d"><v>%d</v></c>' % (ref, style, int(value)))
                elif kind == 'f':
                    # Live formula + a cached value (so it shows immediately and recalculates on edit).
                    formula, cached = value
                    cell_xml.append('<c r="%s" s="%d"><f>%s</f><v>%d</v></c>'
                                    % (ref, style, escape(formula), int(cached)))
                else:
                    cell_xml.append('<c r="%s" t="inlineStr" s="%d"><is><t xml:space="preserve">%s</t></is></c>'
                                    % (ref, style, escape(str(value))))
            elif r >= grid_from_row:
                cell_xml.append('<c r="%s" s="%d"/>' % (ref, _ST_TEXT))   # bordered blank → full grid
        if cell_xml:
            rows_xml.append('<row r="%d">%s</row>' % (r, ''.join(cell_xml)))

    cols_xml = ''.join('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (c, c, widths.get(c, 12))
                       for c in range(1, max_col + 1))
    pane_xml = ''
    if freeze_rows:
        pane_xml = ('<sheetViews><sheetView workbookViewId="0">'
                    '<pane ySplit="%d" topLeftCell="A%d" activePane="bottomLeft" state="frozen"/>'
                    '<selection pane="bottomLeft"/></sheetView></sheetViews>') % (freeze_rows, freeze_rows + 1)
    dim = 'A1:%s%d' % (_xlsx_col_letter(max_col), max(max_row, 1))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dim}"/>{pane_xml}<cols>{cols_xml}</cols>'
        f'<sheetData>{"".join(rows_xml)}</sheetData></worksheet>'
    )

    _thin = ('<border><left style="thin"><color rgb="FF000000"/></left>'
             '<right style="thin"><color rgb="FF000000"/></right>'
             '<top style="thin"><color rgb="FF000000"/></top>'
             '<bottom style="thin"><color rgb="FF000000"/></bottom><diagonal/></border>')
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0"/></numFmts>'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF000000"/><name val="Calibri"/></font></fonts>'
        '<fills count="5"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/></patternFill></fill>'   # 3 subtotal light blue
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/></patternFill></fill></fills>'  # 4 grand gold
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
        + _thin +
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="10">'
        # 0 title (plain, no border)
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        # 1 header — bold black on gray, bordered, centered
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
        # 2 text (bordered)
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
        # 3 number (bordered, #,##0)
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
        # 4 bold text (bordered)
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"/>'
        # 5 bold number (bordered, #,##0)
        '<xf numFmtId="164" fontId="1" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1"/>'
        # 6 subtotal text — bold on light blue, bordered
        '<xf numFmtId="0" fontId="1" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
        # 7 subtotal number — bold, light blue, #,##0
        '<xf numFmtId="164" fontId="1" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1"/>'
        # 8 grand-total text — bold on gold, bordered
        '<xf numFmtId="0" fontId="1" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'
        # 9 grand-total number — bold, gold, #,##0
        '<xf numFmtId="164" fontId="1" fillId="4" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1"/>'
        '</cellXfs></styleSheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(title[:31])}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    out = BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', root_rels)
        zf.writestr('xl/workbook.xml', workbook)
        zf.writestr('xl/_rels/workbook.xml.rels', workbook_rels)
        zf.writestr('xl/styles.xml', styles)
        zf.writestr('xl/worksheets/sheet1.xml', sheet_xml)
    return out.getvalue()


def build_closing_sheet_xlsx(payload, type_filter=''):
    """Render the Required Credit Limit data into a formatted .xlsx in the CLOSING SHEET
    layout: 'Sum of TOTAL LTR' in row 1, a gray bold-black header row, each ASM's parties,
    a bold '<ASM> Total' subtotal, and a final bold 'Grand Total'; the whole body is bordered.
    Columns: SO NAME (ASM), PARTY NAME, TYPE (P/C/P+C), MAIN GROUP, STATE, DELIVERY REMARK,
    PREMIUM, COMMODITY (the type litres split), Grand Total, SO NO, PI AMT (OIH revenue),
    LEDGER AMT (SAP balance, +receivable / -payable), TOTAL OUTSTANDING (= PI AMT + LEDGER AMT)
    and REQUIRED LIMIT (= Total Outstanding + 2%); Payment Done / Outstanding stay blank.
    type_filter restricts to parties of the given type(s): accepts a single 'P'|'C'|'P+C' string
    or a list/set of them; '' or an empty collection = all types. Pure-Python writer."""
    # Normalize to a set of valid types; empty set = no filter (every party shown).
    if isinstance(type_filter, str):
        type_filter = [type_filter] if type_filter else []
    types = {t for t in type_filter if t in ('P', 'C', 'P+C')}

    # 1-based column widths: A SO NAME, B PARTY NAME, C TYPE, D MAIN GROUP, E STATE,
    # F DELIVERY REMARK, G PREMIUM, H COMMODITY, I Grand Total, J SO NO, K PI AMT,
    # L LEDGER AMT, M TOTAL OUTSTANDING, N REQUIRED LIMIT, O–P deferred financial columns.
    widths = {1: 26.7, 2: 46.6, 3: 7.0, 4: 14.4, 5: 9.0, 6: 28.0, 7: 12.3, 8: 12.3, 9: 12.0,
              10: 22.0, 11: 14.0, 12: 15.0, 13: 14.0, 14: 18.0, 15: 14.0, 16: 14.0, 17: 14.0, 18: 18.0}
    MAX_COL = 18

    cells = {}

    def put_text(rr, cc, value, style=_ST_TEXT):
        if value in (None, ''):
            return
        cells[(rr, cc)] = (style, 't', value)

    def put_num(rr, cc, value, style=_ST_NUM, blank_zero=True):
        v = int(round(value or 0))
        if blank_zero and v == 0:
            return
        cells[(rr, cc)] = (style, 'n', v)

    def put_formula(rr, cc, formula, cached, style=_ST_BNUM, blank_zero=True):
        cv = int(round(cached or 0))
        if blank_zero and cv == 0:
            return
        cells[(rr, cc)] = (style, 'f', (formula, cv))

    # Numeric value columns — subtotals and the grand total are written as live SUM formulas
    # over these so deleting any party row (or a whole ASM) re-totals automatically in Excel.
    NUM_COLS = [7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18]
    sub_rows = []   # subtotal row numbers (one per ASM) — the grand total sums these

    put_text(1, 1, 'Sum of TOTAL LTR', style=_ST_TITLE)
    headers = ['SO NAME', 'PARTY NAME', 'TYPE', 'MAIN GROUP', 'STATE', 'DELIVERY REMARK',
               'PREMIUM', 'COMMODITY', 'Grand Total', 'SO NO', 'PI AMT', 'PI TOTAL AMT',
               'LEDGER AMT', 'OUTSTANDING', 'Required Limit', 'PAYMENT DONE', 'TOTAL OUTSTANDING',
               'LEDGER - PAYMENT']
    for i, h in enumerate(headers, start=1):
        put_text(2, i, h, style=_ST_HEAD)

    r = 3
    g_prem = g_com = g_tot = g_val = g_pitot = g_led = g_out = g_req = g_pay = g_rem = g_ledpay = 0.0
    for g in payload.get('asms', []):
        rows = g.get('rows', [])
        if types:
            rows = [x for x in rows if x.get('type') in types]
        if not rows:
            continue
        first = True
        first_party_row = r
        s_prem = s_com = s_tot = s_val = s_pitot = s_led = s_out = s_req = s_pay = s_rem = s_ledpay = 0.0
        for row in rows:
            prem = float(row['litres'].get('premium', 0) or 0)
            commodity = float(row['litres'].get('commodity', 0) or 0)
            total = float(row['litres'].get('total', 0) or 0)
            val = float(row['value'].get('total', 0) or 0)
            pi_total = float(row['value'].get('pi_total', 0) or 0)   # full SO total (matches SAP)
            ledger = float(row['value'].get('ledger', 0) or 0)
            payment = float(row['value'].get('payment_done', 0) or 0)   # receipts on the selected date
            # Closing-sheet OUTSTANDING = PI TOTAL AMT (full SO) + Ledger; TOTAL OUTSTANDING = that
            # minus the payment made (the "is this party cleared" figure). Required Limit is that
            # net TOTAL OUTSTANDING + 2% (matches the sheet: O = TOTAL OUTSTANDING * 1.02).
            outstanding = pi_total + ledger
            remaining = outstanding - payment
            required = remaining * 1.02
            led_pay = ledger - payment                    # Ledger Amt − Payment Received (this date)
            if first:
                put_text(r, 1, g['asm'], style=_ST_BTEXT)  # A SO NAME (ASM) — bold, heads the person's block
            put_text(r, 2, row['party'])                  # B PARTY NAME
            put_text(r, 3, row.get('type'))               # C TYPE
            put_text(r, 4, row.get('main_group'))         # D MAIN GROUP
            put_text(r, 5, row.get('state'))              # E STATE
            put_text(r, 6, row.get('remark'))             # F DELIVERY REMARK
            put_num(r, 7, prem)                           # G PREMIUM
            put_num(r, 8, commodity)                      # H COMMODITY
            put_formula(r, 9, 'G%d+H%d' % (r, r), total, style=_ST_BNUM)       # I Grand Total = Premium + Commodity
            put_text(r, 10, row.get('so_nos'))            # J SO NO
            put_num(r, 11, val)                           # K PI AMT (OIH revenue — open/undelivered)
            put_num(r, 12, pi_total)                      # L PI TOTAL AMT (full SO total, matches SAP)
            put_num(r, 13, ledger)                        # M LEDGER AMT (+rec / -pay)
            put_formula(r, 14, 'L%d+M%d' % (r, r), outstanding, style=_ST_NUM)  # N OUTSTANDING = PI TOTAL AMT + Ledger
            put_formula(r, 15, 'Q%d*1.02' % r, required, style=_ST_NUM)         # O Required Limit = TOTAL OUTSTANDING (net) + 2%
            put_num(r, 16, payment)                       # P PAYMENT DONE (receipts on the selected date)
            put_formula(r, 17, 'N%d-P%d' % (r, r), remaining, style=_ST_NUM)    # Q TOTAL OUTSTANDING = Outstanding − Payment (clear-check)
            put_formula(r, 18, 'M%d-P%d' % (r, r), led_pay, style=_ST_NUM)      # R LEDGER − PAYMENT
            s_prem += prem; s_com += commodity; s_tot += total
            s_val += val; s_pitot += pi_total; s_led += ledger; s_out += outstanding; s_req += required
            s_pay += payment; s_rem += remaining; s_ledpay += led_pay
            first = False
            r += 1
        last_party_row = r - 1
        put_text(r, 1, f"{g['asm']} Total", style=_ST_SUBTXT)
        for cc in range(2, MAX_COL + 1):        # fill the whole subtotal band so it reads as one row
            cells.setdefault((r, cc), (_ST_SUBTXT, 't', ''))
        subvals = {7: s_prem, 8: s_com, 9: s_tot, 11: s_val, 12: s_pitot, 13: s_led,
                   14: s_out, 15: s_req, 16: s_pay, 17: s_rem, 18: s_ledpay}
        for cc in NUM_COLS:
            col = _xlsx_col_letter(cc)
            # SUBTOTAL(9,…) not SUM: the range auto-shrinks on row deletion (no #REF!), and the
            # grand-total SUBTOTAL below ignores these nested SUBTOTAL cells (so no double count).
            put_formula(r, cc, 'SUBTOTAL(9,%s%d:%s%d)' % (col, first_party_row, col, last_party_row),
                        subvals[cc], style=_ST_SUBNUM)
        sub_rows.append(r)
        g_prem += s_prem; g_com += s_com; g_tot += s_tot
        g_val += s_val; g_pitot += s_pitot; g_led += s_led; g_out += s_out; g_req += s_req
        g_pay += s_pay; g_rem += s_rem; g_ledpay += s_ledpay
        r += 1

    put_text(r, 1, 'Grand Total', style=_ST_GTXT)
    for cc in range(2, MAX_COL + 1):
        cells.setdefault((r, cc), (_ST_GTXT, 't', ''))
    gvals = {7: g_prem, 8: g_com, 9: g_tot, 11: g_val, 12: g_pitot, 13: g_led,
             14: g_out, 15: g_req, 16: g_pay, 17: g_rem, 18: g_ledpay}
    for cc in NUM_COLS:
        col = _xlsx_col_letter(cc)
        if sub_rows:
            # One contiguous SUBTOTAL over the whole body (rows 3..r-1). SUBTOTAL(9) skips the
            # nested per-ASM SUBTOTAL cells, so it sums only party rows; deleting any party row
            # OR a whole person block just shrinks the range — it never breaks into #REF!.
            put_formula(r, cc, 'SUBTOTAL(9,%s3:%s%d)' % (col, col, r - 1), gvals[cc], style=_ST_GNUM)
        else:
            put_num(r, cc, gvals[cc], style=_ST_GNUM)

    return _render_single_sheet_xlsx('CLOSING SHEET', cells, widths, max_row=r, max_col=MAX_COL, freeze_rows=2)


def save_closing_remark(card_code, remark, user=None):
    """Upsert the editable delivery remark for one party (by SAP CardCode)."""
    card_code = _normalize_name(card_code)
    if not card_code:
        return False
    ClosingRemark.objects.update_or_create(
        card_code=card_code,
        defaults={'remark': (remark or '').strip()[:255],
                  'updated_by': user if (user and user.is_authenticated) else None},
    )
    return True


# ── Flex TGT overrides (Sales Channel dashboard) ───────────────────────────
# Persist the editable "Flex TGT" column so a typed value survives a refresh. Keyed by
# segment + period (month/year) + drill row_key (the drill node path). Auto-saved on edit.
def get_flex_targets(segment, month, year):
    """{row_key: value} of saved Flex TGT overrides for the segment + period."""
    try:
        month = int(month); year = int(year)
    except (TypeError, ValueError):
        return {}
    rows = FlexTarget.objects.filter(segment=(segment or ''), month=month, year=year)
    return {r.row_key: float(r.value) for r in rows}


def save_flex_target(segment, month, year, row_key, value):
    """Upsert (or clear) one Flex TGT override. value None/'' deletes the row."""
    row_key = (row_key or '').strip()[:255]
    if not row_key:
        return False
    try:
        month = int(month); year = int(year)
    except (TypeError, ValueError):
        return False
    if value is None or value == '':
        FlexTarget.objects.filter(segment=(segment or ''), month=month, year=year, row_key=row_key).delete()
        return True
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    FlexTarget.objects.update_or_create(
        segment=(segment or ''), month=month, year=year, row_key=row_key,
        defaults={'value': value})
    return True


# ── Channel-detail drill-to-document (invoice / sales-order lists) ──────────
# Members per slide-2 channel block (mirror of the JS CHANNEL_BLOCKS). REST rolls
# up HORECA + the REST source groups; GT/ROI/MT are each a single main group.
CHANNEL_MEMBERS = {
    'GT': ['GT'],
    'ROI': ['ROI'],
    'MT': ['MT'],
    # E-Commerce, Horeca and CSD each get their own card now, so they're their own
    # single-group channels. REST holds only the leftover source groups. Keep this in
    # sync with the JS CHANNEL_BLOCKS in realise/templates/realise/dashboard.html.
    'ECOM': ['E-COMMERCE'],
    'HORECA': ['HORECA'],
    'CSD': ['CSD'],
    'REST': [g for g in REST_SOURCE_GROUPS if g != 'E-COMMERCE'],
}


def _fmt_doc_date(value):
    """DocDate -> 'YYYY-MM-DD' display string (best effort)."""
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y-%m-%d')
    return str(value or '').strip()[:10]


def _channel_state_label(channel, state_name, whitelist):
    """Python mirror of the dashboard's channelStateLabel: map a raw state spelling
    to the channel's fixed label, or None when the state isn't in this channel."""
    s = str(state_name or '').strip().upper()
    entries = whitelist.get(channel)
    if not entries:
        return s or 'UNKNOWN'
    for entry in entries:
        for match in entry.get('match', []):
            if str(match).strip().upper() == s:
                return entry['label']
    return None


def _derived_node_match(derived, filters):
    """True when every requested drill filter equals the row's derived dimension."""
    for key, want in filters.items():
        if want in (None, ''):
            continue
        if derived.get(key) != str(want).strip().upper():
            return False
    return True


def _finalize_with_stock(docs):
    """Round litres and expose each document's per-item breakdown, with each item's
    on-hand stock (litres) across OIH_STOCK_WAREHOUSES. _items must be keyed by code
    with {name, code, litres}. Sorted by date then litres descending."""
    codes = {it['code'] for r in docs.values() for it in r['_items'].values() if it['code']}
    stock = _warehouse_stock_litres(codes)
    out = []
    for r in docs.values():
        r['litres'] = round(r['litres'], 2)
        items = []
        for it in r.pop('_items').values():
            items.append({
                'name': it['name'],
                'stock': [round(stock.get((it['code'], w), 0) or 0, 2) for w in OIH_STOCK_WAREHOUSES],
                'litres': round(it['litres'], 2),
            })
        items.sort(key=lambda x: -x['litres'])
        r['items'] = items
        out.append(r)
    out.sort(key=lambda x: (x['doc_date'] or '', -x['litres']))
    return out


def _state_name(row):
    """State name from the row's ship-to state CODE (falls back to OCRD.State1 via the
    same COALESCE in SQL). A national account's true state is its ship-to address, not
    the BP-master HQ — e.g. WAL MART is registered AP but ships to PUNJAB branches."""
    code = _normalize_name(row.get('ST'))
    return STATE_CODE_NAMES.get(code, code)


# ── Delhi-GT customer remap ────────────────────────────────────────────────
# Management wants these customers counted under DELHI (so they land in the Delhi GT
# channel) regardless of their BP/ship-to state — mostly Gurugram/Faridabad (HR)
# accounts treated as Delhi-NCR territory. Matched by CardCode across every realise
# state view: Done (sales proc), Order-in-Hand, the invoice/SO popups, and the OIH
# breakdown. Source: management mapping (OCRD export, 2026-06-20). Edit this set to
# add/remove customers.
DELHI_GT_REMAP_STATE = 'DELHI'
DELHI_GT_REMAP_CARDCODES = frozenset({
    'CUSTA000365', 'CUSTA001073', 'CUSTA000998', 'CUSTA000578', 'CUSTA000352',
    'CUSTA001093', 'CUSTA000789', 'CUSTA000990', 'CUSTA000575', 'CUSTA000971',
    'CUSTA000184', 'CUSTA000587', 'CUSTA001038', 'CUSTA000670', 'CUSTA000053',
    'CUSTA000084', 'CUSTA000280', 'CUSTA000288', 'CUSTA000530', 'CUSTA000888',
    'CUSTA000938', 'CUSTA000086', 'CUSTA000309', 'CUSTA000565', 'CUSTA000329',
    'CUSTA000956', 'CUSTA000347', 'CUSTA000373', 'CUSTA000869', 'CUSTA000589',
    'CUSTA000618', 'CUSTA000801', 'CUSTA000811', 'CUSTA000415', 'CUSTA000988',
    'CUSTA000882', 'CUSTA000825', 'CUSTA000826', 'CUSTA000827', 'CUSTA000881',
    'CUSTA000433', 'CUSTA000839', 'CUSTA000456', 'CUSTA000469', 'CUSTA000691',
    'CUSTA000010', 'CUSTA000041', 'CUSTA000043', 'CUSTA000507', 'CUSTA000694',
    'CUSTA000057', 'CUSTA000058', 'CUSTA000071', 'CUSTA000078', 'CUSTA000081',
    'CUSTA000134', 'CUSTA000138', 'CUSTA000157', 'CUSTA000175', 'CUSTA000270',
    'CUSTA000203', 'CUSTA000703', 'CUSTA000221', 'CUSTA000954', 'CUSTA000527',
    'CUSTA000714', 'CUSTA000732', 'CUSTA000504', 'CUSTA000867', 'CUSTA000760',
    'CUSTA000764', 'CUSTA000783', 'CUSTA000798', 'CUSTA000355', 'CUSTA000099',
    'CUSTA000027', 'CUSTA000429', 'CUSTA000722', 'CUSTA000927', 'CUSTA001078',
    'CUSTA000708', 'CUSTA000650', 'CUSTA000926', 'CUSTA000372', 'CUSTA001075',
})


def _delhi_gt_state(cardcode, state_name):
    """DELHI for management-mapped customers, else the row's own state."""
    if cardcode and _normalize_name(cardcode) in DELHI_GT_REMAP_CARDCODES:
        return DELHI_GT_REMAP_STATE
    return state_name


def reconcile_channel_done(start_date, end_date, channel, seg, state):
    """Diagnostic only: compare the channel Done figure (from the REPORT_SALES_ANALYSIS
    proc, what the channel table shows) with the popup Done (direct OINV/ORIN query),
    broken down per party, so the source of any gap is visible. Read-only."""
    members = CHANNEL_MEMBERS.get(channel)
    seg_u = _normalize_name(seg)
    state_u = _normalize_name(state)
    _, raw = get_sales_data_cached(start_date, end_date)
    proc = {}
    for r in raw or []:
        g = _normalize_name(r.get('U_Main_Group'))
        if members is not None and g not in members:
            continue
        if seg_u and _normalize_name(r.get('U_TYPE')) != seg_u:
            continue
        if state_u and _normalize_name(r.get('State')) != state_u:
            continue
        party = _normalize_name(r.get('CardName')) or '—'
        proc[party] = proc.get(party, 0.0) + float(r.get('Liter') or 0)
    docp = {}
    for d in get_channel_done_documents(start_date, end_date, channel, seg, {'state': state}) or []:
        party = d.get('party') or '—'
        docp[party] = docp.get(party, 0.0) + float(d.get('litres') or 0)

    def pack(m):
        parties = sorted(({'party': k, 'litres': round(v, 2)} for k, v in m.items()),
                         key=lambda x: -abs(x['litres']))
        return {'total': round(sum(m.values()), 2), 'party_count': len(parties), 'parties': parties}

    proc_only = sorted(set(proc) - set(docp))
    docs_only = sorted(set(docp) - set(proc))
    return {'channel': channel, 'seg': seg, 'state': state,
            'proc_channel_done': pack(proc), 'popup_done': pack(docp),
            'gap': round(sum(docp.values()) - sum(proc.values()), 2),
            'parties_in_proc_not_popup': proc_only, 'parties_in_popup_not_proc': docs_only}


def _apply_delhi_gt_remap(rows):
    """Force mapped customers' State to DELHI on raw REPORT_SALES_ANALYSIS rows, so Done,
    drill-down, historical and the month pivot all attribute them to Delhi. Matched by the
    proc's CardCode column; logs once (and no-ops) if that column isn't present."""
    if not rows:
        return rows
    keys = list(rows[0].keys())
    code_key = next((k for k in keys if _normalize_name(k).replace('_', '') == 'CARDCODE'), None)
    state_key = next((k for k in keys if _normalize_name(k).replace('_', '') == 'STATE'), None)
    if not code_key or not state_key:
        logger.warning('[DELHI-GT] proc rows missing %s column; Done remap skipped',
                       'CardCode' if not code_key else 'State')
        return rows
    for d in rows:
        if _normalize_name(d.get(code_key)) in DELHI_GT_REMAP_CARDCODES:
            d[state_key] = DELHI_GT_REMAP_STATE
    return rows


# State & City come from the order's ship-to address (CRD1 via ShipToCode); when an
# order has no ship-to address we fall back to the BP-master OCRD.State1/City.
_SHIPTO_STATE = "COALESCE(NULLIF(TRIM(A.\"State\"), ''), TRIM(C.\"State1\"))"
_SHIPTO_CITY = "COALESCE(NULLIF(TRIM(A.\"City\"), ''), TRIM(C.\"City\"))"
_SHIPTO_JOIN = ('LEFT JOIN "{S}"."CRD1" A ON A."CardCode" = H."CardCode" '
                'AND A."Address" = H."ShipToCode" AND A."AdresType" = \'S\'')


# The U_ARNO filter mirrors REPORT_SALES_COGS (the proc behind the dashboard's Done): the
# SAP team marks invoices to hide with OINV/ORIN."U_ARNO" = 'H' (and 'T'); the proc excludes
# them, so the Done popup must too or it over-counts hidden parties vs the channel cell.
# Litres MUST be computed exactly as the REPORT_SALES_COGS proc does (the dashboard's Done source),
# or the popup over-counts: the proc zeroes litres on no-inventory-movement lines (NoInvtryMv='Y',
# e.g. combo / drop-ship billing), counts litres only for U_IsLitre='Y' items, expands bill-of-
# material (TreeType='S') items from their litre components, and converts a bulk 'MTS' UoM line by
# ×1098.9. A naive Quantity×SalPackUn (the old formula) ignored all of that and inflated litres
# several-fold for bulk customers like JIVO MART. Sales stream is +ve, the ORIN return stream −ve.
def _done_sales_litexpr(schema, ln='L', it='I'):
    """The proc's per-line litre expression, with the INV1 line and OITM aliases parameterised so
    it can be dropped into any invoice-line query (channel Done, Hidden Sales, …)."""
    return (
        f"CASE WHEN {ln}.\"NoInvtryMv\"='Y' THEN 0 "
        f"WHEN {ln}.\"TreeType\"='S' THEN (SELECT SUM(K1.\"Quantity\"*K2.\"SalPackUn\") "
        f"FROM \"{schema}\".\"ITT1\" K1 JOIN \"{schema}\".\"OITM\" K2 ON K1.\"Code\"=K2.\"ItemCode\" "
        f"WHERE K1.\"Father\"={ln}.\"ItemCode\" AND K2.\"U_IsLitre\"='Y')*{ln}.\"Quantity\" "
        f"ELSE (CASE WHEN {it}.\"U_IsLitre\"='Y' THEN CASE WHEN {ln}.\"UomCode\"='MTS' "
        f"THEN {ln}.\"Quantity\"*1098.9 ELSE {ln}.\"Quantity\" END ELSE 0 END)*{it}.\"SalPackUn\" END")


_DONE_RETURN_LITEXPR = ("(CASE WHEN L.\"NoInvtryMv\"='N' THEN -L.\"Quantity\" ELSE 0 END)"
                        "*(CASE WHEN I.\"U_IsLitre\"='Y' THEN I.\"SalPackUn\" ELSE 0 END)")

_DONE_LINE_SQL = '''
    SELECT H."DocNum" AS "DOCNUM", H."DocDate" AS "DOCDATE",
           COALESCE(TRIM(C."U_Main_Group"), '') AS "GRP",
           ''' + _SHIPTO_STATE + ''' AS "ST",
           ''' + _SHIPTO_CITY + ''' AS "CITY",
           COALESCE(TRIM(H."CardCode"), '')     AS "CCODE",
           COALESCE(TRIM(C."CardName"), '')     AS "CUST",
           COALESCE(C."Balance", 0)             AS "BAL",
           COALESCE(TRIM(I."U_Sub_Group"), '')  AS "SUBG",
           COALESCE(TRIM(I."ItemName"), '')     AS "ITEM",
           COALESCE(TRIM(I."ItemCode"), '')     AS "ICODE",
           COALESCE(TRIM(I."U_TYPE"), '')       AS "UTYPE",
           {litexpr} AS "LIT"
    FROM "{S}"."{hdr}" H
    JOIN "{S}"."{ln}" L ON L."DocEntry" = H."DocEntry"
    JOIN "{S}"."OCRD" C ON C."CardCode" = H."CardCode"
    LEFT JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode"
    ''' + _SHIPTO_JOIN + '''
    WHERE H."DocDate" BETWEEN ? AND ? AND H."CANCELED" = 'N'
      AND (H."U_ARNO" NOT IN ('T', 'H') OR H."U_ARNO" IS NULL)
      AND L."TreeType" <> 'I' AND C."GroupCode" <> 100 {extrawhere}
'''


def get_channel_done_documents(start_date, end_date, channel, seg, filters):
    """Invoice documents (date / number / party / litres) behind a Done-L cell in the
    channel-detail modal. Reads the invoice base tables directly — OINV/INV1 (sales,
    positive litres) plus ORIN/RIN1 (returns / credit memos, negative litres) so net
    Done matches the dashboard's sign — then re-derives the same drill dimensions the
    modal buckets by and keeps the matching rows. Litres = Quantity * OITM.SalPackUn,
    the same conversion REPORT_SALES_ANALYSIS uses."""
    members = CHANNEL_MEMBERS.get(channel)  # None -> all groups (commodity / all-channel)
    seg = str(seg or '').strip().upper()
    inv = _DONE_LINE_SQL.format(S=SAP_SCHEMA, hdr='OINV', ln='INV1',
                                litexpr=_done_sales_litexpr(SAP_SCHEMA),
                                extrawhere="AND I.\"U_Sub_Group\" NOT IN ('GIFT PACK')")
    crd = _DONE_LINE_SQL.format(S=SAP_SCHEMA, hdr='ORIN', ln='RIN1',
                                litexpr=_DONE_RETURN_LITEXPR, extrawhere='')
    sql = (f'SELECT "DOCNUM","DOCDATE","GRP","ST","CCODE","CUST","CITY","SUBG","ITEM","ICODE","UTYPE", '
           f'MAX("BAL") AS "BAL", SUM("LIT") AS "LIT" FROM ( {inv} UNION ALL {crd} ) T '
           f'GROUP BY "DOCNUM","DOCDATE","GRP","ST","CCODE","CUST","CITY","SUBG","ITEM","ICODE","UTYPE"')
    try:
        rows = sap_connector.execute_query(sql, (start_date, end_date, start_date, end_date))
    except Exception as exc:
        logger.error('[CH-DETAIL] invoice fetch failed: %s', exc)
        return []
    payload = get_territory_dashboard_payload()
    person_map, whitelist = payload['map'], payload['whitelist']
    docs = {}
    for row in rows or []:
        g = _normalize_name(row.get('GRP'))
        if members is not None and g not in members:
            continue
        if seg and _normalize_name(row.get('UTYPE')) != seg:
            continue
        state_name = _delhi_gt_state(row.get('CCODE'), _state_name(row))
        st = _channel_state_label(channel, state_name, whitelist)
        if st is None:
            continue
        customer = _normalize_name(row.get('CUST')) or '—'
        derived = {
            'group': g, 'state': st,
            'person': person_map.get(g + '|' + state_name) or '—',
            'customer': customer,
            'product': _normalize_name(row.get('SUBG')) or '—',
            'item': _item_label(_normalize_name(row.get('ICODE')), _normalize_name(row.get('ITEM'))),
        }
        if not _derived_node_match(derived, filters):
            continue
        num = str(row.get('DOCNUM') or '').strip()
        dkey = num or (_fmt_doc_date(row.get('DOCDATE')) + '|' + customer)
        rec = docs.get(dkey)
        if rec is None:
            rec = docs[dkey] = {'doc_num': num, 'doc_date': _fmt_doc_date(row.get('DOCDATE')),
                                'party': customer, 'state': state_name, 'balance': float(row.get('BAL') or 0),
                                'city': _normalize_name(row.get('CITY')), 'litres': 0.0, '_items': {}}
        lit = float(row.get('LIT') or 0)
        rec['litres'] += lit
        icode = _normalize_name(row.get('ICODE'))
        ikey = icode or derived['item']
        it = rec['_items'].get(ikey)
        if it is None:
            it = rec['_items'][ikey] = {'name': derived['item'], 'code': icode, 'litres': 0.0}
        it['litres'] += lit
    return _finalize_with_stock(docs)


# Compare-Sales value-cell drill: same invoice sources as the channel Done docs, but also carries
# per-item Box Qty (Qty ÷ SalFactor2), Taxable Value (INV1.LineTotal, net of discount before tax)
# and Rate/Bottle (Taxable ÷ Qty) — so it can't reuse get_channel_done_documents (litres only).
_COMPARE_LINE_SQL = '''
    SELECT H."DocNum" AS "DOCNUM", H."DocDate" AS "DOCDATE",
           COALESCE(TRIM(C."U_Main_Group"), '') AS "GRP",
           ''' + _SHIPTO_STATE + ''' AS "ST",
           ''' + _SHIPTO_CITY + ''' AS "CITY",
           COALESCE(TRIM(H."CardCode"), '')     AS "CCODE",
           COALESCE(TRIM(C."CardName"), '')     AS "CUST",
           COALESCE(TRIM(I."U_Sub_Group"), '')  AS "SUBG",
           COALESCE(TRIM(I."ItemName"), '')     AS "ITEM",
           COALESCE(TRIM(I."ItemCode"), '')     AS "ICODE",
           COALESCE(TRIM(I."U_TYPE"), '')       AS "UTYPE",
           {sign} * L."Quantity" * COALESCE(I."SalPackUn", 0)  AS "LIT",
           {sign} * L."Quantity"                               AS "QTY",
           {sign} * L."Quantity" / NULLIF(I."SalFactor2", 0)   AS "BOX",
           {sign} * L."LineTotal"                              AS "VAL"
    FROM "{S}"."{hdr}" H
    JOIN "{S}"."{ln}" L ON L."DocEntry" = H."DocEntry"
    JOIN "{S}"."OCRD" C ON C."CardCode" = H."CardCode"
    LEFT JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode"
    ''' + _SHIPTO_JOIN + '''
    WHERE H."DocDate" BETWEEN ? AND ? AND H."CANCELED" = 'N'
      AND (H."U_ARNO" NOT IN ('T', 'H') OR H."U_ARNO" IS NULL)
      AND I."ItemCode" LIKE 'FG%'
'''


def get_compare_sales_documents(start_date, end_date, seg, filters):
    """Invoices (Doc No / date / party) with a per-item breakdown — Box Qty, Rate/Bottle,
    Taxable Value and Litres — behind a Compare-Sales value cell. Same OINV/INV1 (+ ORIN/RIN1
    return) sources as the channel Done docs, but not channel-scoped: the clicked pivot node's
    derived dimensions (group/state/person/customer/product/item) + the cell's month select the
    rows. Litres = Qty × SalPackUn; Boxes = Qty ÷ SalFactor2; Taxable = LineTotal."""
    seg = str(seg or '').strip().upper()
    inv = _COMPARE_LINE_SQL.format(S=SAP_SCHEMA, hdr='OINV', ln='INV1', sign='1')
    crd = _COMPARE_LINE_SQL.format(S=SAP_SCHEMA, hdr='ORIN', ln='RIN1', sign='-1')
    sql = ('SELECT "DOCNUM","DOCDATE","GRP","ST","CCODE","CUST","CITY","SUBG","ITEM","ICODE","UTYPE", '
           'SUM("LIT") AS "LIT", SUM("QTY") AS "QTY", SUM("BOX") AS "BOX", SUM("VAL") AS "VAL" '
           f'FROM ( {inv} UNION ALL {crd} ) T '
           'GROUP BY "DOCNUM","DOCDATE","GRP","ST","CCODE","CUST","CITY","SUBG","ITEM","ICODE","UTYPE"')
    try:
        rows = sap_connector.execute_query(sql, (start_date, end_date, start_date, end_date))
    except Exception as exc:
        logger.error('[COMPARE-DOCS] invoice fetch failed: %s', exc)
        return []
    person_map = get_territory_dashboard_payload()['map']
    docs = {}
    for row in rows or []:
        g = _normalize_name(row.get('GRP'))
        if seg and _normalize_name(row.get('UTYPE')) != seg:
            continue
        state_name = _delhi_gt_state(row.get('CCODE'), _state_name(row))
        customer = _normalize_name(row.get('CUST')) or '—'
        derived = {
            'group': g, 'state': state_name,
            'person': person_map.get(g + '|' + state_name) or '—',
            'customer': customer,
            'product': _normalize_name(row.get('SUBG')) or '—',
            'item': _item_label(_normalize_name(row.get('ICODE')), _normalize_name(row.get('ITEM'))),
        }
        if not _derived_node_match(derived, filters):
            continue
        num = str(row.get('DOCNUM') or '').strip()
        dkey = num or (_fmt_doc_date(row.get('DOCDATE')) + '|' + customer)
        rec = docs.get(dkey)
        if rec is None:
            rec = docs[dkey] = {'doc_num': num, 'doc_date': _fmt_doc_date(row.get('DOCDATE')),
                                'party': customer, 'state': state_name,
                                'litres': 0.0, 'boxes': 0.0, 'taxable': 0.0, '_items': []}
        lit = float(row.get('LIT') or 0)
        qty = float(row.get('QTY') or 0)
        box = float(row.get('BOX') or 0)
        val = float(row.get('VAL') or 0)
        rec['litres'] += lit
        rec['boxes'] += box
        rec['taxable'] += val
        rec['_items'].append({
            'name': derived['item'],
            'boxes': round(box, 2),
            'litres': round(lit, 2),
            'taxable': round(val, 2),
            'rate': round(val / qty, 2) if qty else 0.0,   # net taxable rate per bottle/piece
        })
    out = []
    for r in docs.values():
        r['litres'] = round(r['litres'], 2)
        r['boxes'] = round(r['boxes'], 2)
        r['taxable'] = round(r['taxable'], 2)
        r['items'] = sorted(r.pop('_items'), key=lambda x: -x['litres'])
        out.append(r)
    out.sort(key=lambda x: (x['doc_date'] or '', -x['litres']))
    return out


# ══════════════════════ Sales vs Credit Notes report ══════════════════════
# Gross sales (OINV) alongside credit notes (ORIN), the latter split into CN for Goods (item
# credit memos, DocType 'I' — real product returns, carry litres/type/product) and Claim for
# Services (service credit memos, DocType 'S' — discounts / FOC / samples; no item line, so no
# litres and no Premium/Commodity). Net Sales = Total Sales − Total CN (goods + services).
# Grouped by any of main group / state / sales person / product / item / customer, filterable by
# Premium/Commodity (oil only) and Revenue vs quantity (Litres for oil, Boxes for beverages).
# Two companies: oil (JIVO_OIL_HANADB, U_SALES_PERSON, litres=Qty×SalPackUn, hides U_ARNO T/H) and
# beverages (JIVO_BEVERAGES_HANADB, OSLP.SlpName, boxes=Qty/SalFactor2, FINISHED goods only).
_SALESCN_TTL = 90            # seconds — matches the other live SAP report windows

# Sentinel dimension labels for service claims (they have no product / item / segment).
_CN_SERVICE_LABEL = 'SERVICE / CLAIM'


def _fetch_sales_cn_oil(start_date, end_date):
    """Raw per-line rows for the oil company: three UNION-ed streams (SALES / CNG / CNS) each
    carrying the report's dimensions, QTY (litres; 0 for service claims) and REV (LineTotal,
    pre-VAT). Cancelled and hidden (U_ARNO 'T'/'H') documents are excluded, matching Done."""
    S = SAP_SCHEMA
    st, join = _SHIPTO_STATE, _SHIPTO_JOIN.format(S=S)
    where = ('H."DocDate" BETWEEN ? AND ? AND H."CANCELED" = \'N\' '
             'AND (H."U_ARNO" NOT IN (\'T\', \'H\') OR H."U_ARNO" IS NULL)')
    item_cols = f'''COALESCE(TRIM(I."U_Sub_Group"),'') AS "PROD",
                    COALESCE(TRIM(I."ItemName"),'')     AS "ITEM",
                    COALESCE(TRIM(I."ItemCode"),'')     AS "ICODE",
                    COALESCE(TRIM(I."U_TYPE"),'')        AS "UTYPE",
                    L."Quantity" * COALESCE(I."SalPackUn",0) AS "QTY"'''
    hdr_cols = '''COALESCE(TRIM(C."U_Main_Group"),'') AS "GRP", {st} AS "ST",
                  COALESCE(TRIM(H."U_SALES_PERSON"),'') AS "PERSON",
                  COALESCE(TRIM(H."CardCode"),'') AS "CCODE",
                  COALESCE(TRIM(C."CardName"),'') AS "CUST"'''.replace('{st}', st)
    item_from = (f'JOIN "{S}"."{{ln}}" L ON L."DocEntry"=H."DocEntry" '
                 f'JOIN "{S}"."OCRD" C ON C."CardCode"=H."CardCode" '
                 f'LEFT JOIN "{S}"."OITM" I ON I."ItemCode"=L."ItemCode" {join}')
    sales = (f'SELECT \'SALES\' AS "STREAM", {hdr_cols}, {item_cols}, COALESCE(L."LineTotal",0) AS "REV" '
             f'FROM "{S}"."OINV" H ' + item_from.format(ln='INV1') + f' WHERE {where}')
    cng = (f'SELECT \'CNG\' AS "STREAM", {hdr_cols}, {item_cols}, COALESCE(L."LineTotal",0) AS "REV" '
           f'FROM "{S}"."ORIN" H ' + item_from.format(ln='RIN1') + f' WHERE {where} AND H."DocType"=\'I\'')
    cns = (f'SELECT \'CNS\' AS "STREAM", {hdr_cols}, '
           f'\'{_CN_SERVICE_LABEL}\' AS "PROD", \'{_CN_SERVICE_LABEL}\' AS "ITEM", \'\' AS "ICODE", \'SERVICE\' AS "UTYPE", '
           f'0 AS "QTY", COALESCE(L."LineTotal",0) AS "REV" '
           f'FROM "{S}"."ORIN" H JOIN "{S}"."RIN1" L ON L."DocEntry"=H."DocEntry" '
           f'JOIN "{S}"."OCRD" C ON C."CardCode"=H."CardCode" {join} WHERE {where} AND H."DocType"=\'S\'')
    sql = f'{sales} UNION ALL {cng} UNION ALL {cns}'
    return sap_connector.execute_query(sql, (start_date, end_date) * 3)


def _fetch_sales_cn_bev(start_date, end_date):
    """Raw per-line rows for the beverages company. QTY = Boxes (Qty / SalFactor2). Beverages
    have no Premium/Commodity, so UTYPE is blank; state resolves via OCST from the ship-to
    address; sales person is OSLP.SlpName. Restricted to FINISHED goods (matching the beverages
    sales report), except service claims which have no item line."""
    B = BEVERAGES_SCHEMA
    state_expr = (f'(SELECT K."Name" FROM "{B}"."OCST" K '
                  f'WHERE K."Code"=A."State" AND K."Country"=A."Country")')
    shipto = (f'LEFT JOIN "{B}"."CRD1" A ON A."CardCode"=H."CardCode" '
              f'AND A."AdresType"=\'S\' AND A."Address"=H."ShipToCode"')
    slp = f'LEFT JOIN "{B}"."OSLP" S ON S."SlpCode"=H."SlpCode"'
    where = 'H."DocDate" BETWEEN ? AND ? AND H."CANCELED"=\'N\' AND C."GroupCode"<>100'
    fin = (f'JOIN "{B}"."OITM" I ON I."ItemCode"=L."ItemCode" '
           f'JOIN "{B}"."OITB" G ON G."ItmsGrpCod"=I."ItmsGrpCod"')
    fin_where = 'AND L."TreeType"<>\'I\' AND G."ItmsGrpNam"=\'FINISHED\''
    hdr_cols = ('COALESCE(TRIM(C."U_Main_Group"),\'\') AS "GRP", {st} AS "ST", '
                'COALESCE(TRIM(S."SlpName"),\'\') AS "PERSON", '
                'COALESCE(TRIM(H."CardCode"),\'\') AS "CCODE", '
                'COALESCE(TRIM(C."CardName"),\'\') AS "CUST"').replace('{st}', state_expr)
    item_cols = ('COALESCE(TRIM(I."U_Sub_Group"),\'\') AS "PROD", '
                 'COALESCE(TRIM(I."ItemName"),\'\') AS "ITEM", '
                 'COALESCE(TRIM(I."ItemCode"),\'\') AS "ICODE", \'\' AS "UTYPE", '
                 'L."Quantity" / NULLIF(I."SalFactor2",0) AS "QTY"')
    item_from = (f'JOIN "{B}"."{{ln}}" L ON L."DocEntry"=H."DocEntry" '
                 f'JOIN "{B}"."OCRD" C ON C."CardCode"=H."CardCode" {fin} {slp} {shipto}')
    sales = (f'SELECT \'SALES\' AS "STREAM", {hdr_cols}, {item_cols}, COALESCE(L."LineTotal",0) AS "REV" '
             f'FROM "{B}"."OINV" H ' + item_from.format(ln='INV1') + f' WHERE {where} {fin_where}')
    cng = (f'SELECT \'CNG\' AS "STREAM", {hdr_cols}, {item_cols}, COALESCE(L."LineTotal",0) AS "REV" '
           f'FROM "{B}"."ORIN" H ' + item_from.format(ln='RIN1') + f' WHERE {where} {fin_where} AND H."DocType"=\'I\'')
    cns = (f'SELECT \'CNS\' AS "STREAM", {hdr_cols}, '
           f'\'{_CN_SERVICE_LABEL}\' AS "PROD", \'{_CN_SERVICE_LABEL}\' AS "ITEM", \'\' AS "ICODE", \'\' AS "UTYPE", '
           f'0 AS "QTY", COALESCE(L."LineTotal",0) AS "REV" '
           f'FROM "{B}"."ORIN" H JOIN "{B}"."RIN1" L ON L."DocEntry"=H."DocEntry" '
           f'JOIN "{B}"."OCRD" C ON C."CardCode"=H."CardCode" {slp} {shipto} '
           f'WHERE {where} AND H."DocType"=\'S\'')
    sql = f'{sales} UNION ALL {cng} UNION ALL {cns}'
    return sap_connector.execute_query(sql, (start_date, end_date) * 3)


def _aggregate_sales_cn(raw, company):
    """Collapse raw stream rows into per-dimension buckets carrying the five measures
    (sales / CN-goods qty+rev, CN-service rev). State is normalised to a full name (oil codes →
    names, with the Delhi-GT remap); beverages sales-person aliases are folded together."""
    is_oil = company != 'beverages'
    agg = {}
    for r in raw or []:
        stream = str(r.get('STREAM') or '').strip().upper()
        grp = _normalize_name(r.get('GRP')) or '—'
        ccode = _normalize_name(r.get('CCODE'))
        if is_oil:
            stc = _normalize_name(r.get('ST'))
            state = _delhi_gt_state(ccode, STATE_CODE_NAMES.get(stc, stc)) or '—'
        else:
            state = _normalize_name(r.get('ST')) or '—'
        person = _normalize_name(r.get('PERSON')) or '—'
        if not is_oil:
            person = _BEV_SALESPERSON_ALIAS.get(person, person)
        prod = _normalize_name(r.get('PROD')) or '—'
        item = _item_label(_normalize_name(r.get('ICODE')), _normalize_name(r.get('ITEM')))
        cust = _normalize_name(r.get('CUST')) or '—'
        utype = _normalize_name(r.get('UTYPE'))
        key = (grp, state, person, prod, item, cust, utype)
        b = agg.get(key)
        if b is None:
            b = agg[key] = {'main_group': grp, 'state': state, 'person': person,
                            'product': prod, 'item_name': item, 'customer': cust, 'u_type': utype,
                            'sales_qty': 0.0, 'sales_rev': 0.0, 'cng_qty': 0.0,
                            'cng_rev': 0.0, 'cns_rev': 0.0}
        qty, rev = float(r.get('QTY') or 0), float(r.get('REV') or 0)
        if stream == 'SALES':
            b['sales_qty'] += qty
            b['sales_rev'] += rev
        elif stream == 'CNG':
            b['cng_qty'] += qty
            b['cng_rev'] += rev
        elif stream == 'CNS':
            b['cns_rev'] += rev
    out = list(agg.values())
    for b in out:
        for k in ('sales_qty', 'sales_rev', 'cng_qty', 'cng_rev', 'cns_rev'):
            b[k] = round(b[k], 2)
    return out


def get_sales_cn_report(start_date, end_date, company='oil'):
    """Sales vs Credit Notes payload for the given date range and company ('oil' | 'beverages').
    Returns {status, company, measure ('Litres'|'Boxes'), has_type, rows[...], start, end}. Each
    row carries its dimensions plus sales_qty/sales_rev, cng_qty/cng_rev (CN for Goods) and
    cns_rev (Claim for Services). Cached per (company, range) for _SALESCN_TTL seconds."""
    company = 'beverages' if str(company or '').lower().startswith('bev') else 'oil'
    measure = 'Boxes' if company == 'beverages' else 'Litres'
    key = (company, str(start_date), str(end_date))
    now = time.time()
    hit = _shared_get('salescn', key)
    if hit is not None:
        return hit
    try:
        raw = (_fetch_sales_cn_bev(start_date, end_date) if company == 'beverages'
               else _fetch_sales_cn_oil(start_date, end_date))
        payload = {'status': 'ok', 'company': company, 'measure': measure,
                   'has_type': company != 'beverages', 'rows': _aggregate_sales_cn(raw, company),
                   'start': str(start_date), 'end': str(end_date)}
    except Exception as exc:
        logger.error('[SALES-CN] %s report failed: %s', company, exc)
        return {'status': 'error', 'company': company, 'measure': measure,
                'has_type': company != 'beverages', 'rows': [], 'error': str(exc),
                'start': str(start_date), 'end': str(end_date)}
    if raw:
        _shared_set('salescn', key, payload, _SALESCN_TTL)
    return payload


# ══════════════════════ Hidden Customer Sales report ══════════════════════
# Sales invoices the SAP team has flagged HIDDEN (OINV.U_ARNO = 'H') are excluded from the
# dashboard's Done (see the U_ARNO note near _DONE_LINE_SQL). This report surfaces them on their
# own — one row per invoice line — so hidden sales stay auditable. Litres = Quantity × SalPackUn,
# Value = INV1.LineTotal, Cost Center = INV1.OcrCode (holds the oil variety). Oil only. Same
# filters as the source query: U_ARNO='H' and a DocDate range (nothing else).
_HIDDEN_SALES_TTL = 90

_HIDDEN_RAW_TTL = 90


def _fetch_hidden_raw(start_date, end_date):
    """Hidden sales-invoice lines (OINV.U_ARNO='H') shaped like the sales-proc output — the same
    keys _aggregate_channel_month_rows reads — so Compare Sales can fold them into the pivot when
    the 'Hidden' toggle is ON. Ship-to state resolved to its name (OCST); Delhi-GT remap applied
    to match the normal rows. Cached per range."""
    key = (str(start_date), str(end_date))
    now = time.time()
    hit = _shared_get('hiddenraw', key)
    if hit is not None:
        return hit
    S = SAP_SCHEMA
    sql = f'''
        SELECT H."DocDate" AS "DocDate", SP."SlpName" AS "U_SALES_PERSON",
               COALESCE(TRIM(H."CardCode"),'') AS "CardCode",
               COALESCE(TRIM(I."U_TYPE"),'')      AS "U_TYPE",
               COALESCE(TRIM(C."U_Main_Group"),'') AS "U_Main_Group",
               COALESCE(TRIM(I."U_Sub_Group"),'') AS "U_Sub_Group",
               COALESCE(TRIM(I."U_Variety"),'')   AS "U_Variety",
               COALESCE(TRIM(I."U_SKU"),'')        AS "SKU",
               COALESCE(TRIM(I."ItemName"),'')     AS "ItemName",
               COALESCE(TRIM(L."ItemCode"),'')     AS "ItemCode",
               COALESCE(TRIM(C."CardName"),'')     AS "CardName",
               (SELECT K."Name" FROM "{S}".OCST K WHERE K."Code"=A."State" AND K."Country"=A."Country") AS "State",
               {_done_sales_litexpr(S)}            AS "Liter",
               COALESCE(L."LineTotal",0)           AS "LineTotal"
        FROM "{S}"."OINV" H
        JOIN "{S}"."INV1" L ON H."DocEntry"=L."DocEntry"
        JOIN "{S}"."OITM" I ON L."ItemCode"=I."ItemCode"
        LEFT JOIN "{S}"."OCRD" C ON C."CardCode"=H."CardCode"
        LEFT JOIN "{S}"."OSLP" SP ON H."SlpCode"=SP."SlpCode"
        LEFT JOIN "{S}"."CRD1" A ON A."CardCode"=H."CardCode" AND A."Address"=H."ShipToCode" AND A."AdresType"='S'
        WHERE H."U_ARNO"='H' AND H."DocDate" BETWEEN ? AND ? AND H."CANCELED"='N'
    '''
    try:
        rows = sap_connector.execute_query(sql, (start_date, end_date)) or []
    except Exception as exc:
        logger.error('[HIDDEN-RAW] fetch failed: %s', exc)
        rows = []
    rows = _apply_delhi_gt_remap(rows)
    _shared_set('hiddenraw', key, rows, _HIDDEN_RAW_TTL)
    return rows


def get_hidden_customer_sales(start_date, end_date):
    """Hidden sales-invoice lines (OINV.U_ARNO='H') in [start_date, end_date]. Returns
    {status, rows, start, end}; each row carries doc/status/date, customer, item, cost center
    (OcrCode) and qty/litres/value. The client pivots + filters. Cached per range for
    _HIDDEN_SALES_TTL seconds."""
    key = (str(start_date), str(end_date))
    now = time.time()
    hit = _shared_get('hiddensales', key)
    if hit is not None:
        return hit
    S = SAP_SCHEMA
    sql = f'''
        SELECT T0."DocNum"   AS "DOCNUM", T0."DocStatus" AS "DSTATUS",
               TO_VARCHAR(CAST(T0."DocDate" AS DATE), 'YYYY-MM-DD') AS "DDATE",
               COALESCE(TRIM(T0."CardCode"), '') AS "CCODE",
               COALESCE(TRIM(T0."CardName"), '') AS "CNAME",
               COALESCE(TRIM(T1."ItemCode"), '') AS "ICODE",
               COALESCE(TRIM(T2."ItemName"), '') AS "INAME",
               COALESCE(T1."Quantity", 0)                        AS "QTY",
               {_done_sales_litexpr(S, ln='T1', it='T2')}        AS "LIT",
               COALESCE(T1."LineTotal", 0)                       AS "VAL",
               COALESCE(T1."Price", 0)                           AS "PRICE",
               COALESCE(T1."NoInvtryMv", 'N')                    AS "NIM",
               CASE WHEN EXISTS (SELECT 1 FROM "{S}"."INV1" X WHERE X."DocEntry"=T0."DocEntry"
                    AND X."NoInvtryMv"='N') THEN 'N' ELSE 'Y' END AS "DNOTE",   -- no goods line = debit note (rate-diff etc.)
               COALESCE(TRIM(T2."U_TYPE"), '')  AS "UTYPE",
               COALESCE(TRIM(T1."OcrCode"), '') AS "OCR",
               COALESCE(NULLIF(TRIM(T4."State"), ''), TRIM(T3."State1")) AS "ST",
               COALESCE(TRIM(T3."U_Main_Group"), '') AS "GRP"
        FROM "{S}"."OINV" T0
        INNER JOIN "{S}"."INV1" T1 ON T0."DocEntry" = T1."DocEntry"
        INNER JOIN "{S}"."OITM" T2 ON T1."ItemCode" = T2."ItemCode"
        LEFT JOIN "{S}"."OCRD" T3 ON T3."CardCode" = T0."CardCode"
        LEFT JOIN "{S}"."CRD1" T4 ON T4."CardCode" = T0."CardCode"
             AND T4."Address" = T0."ShipToCode" AND T4."AdresType" = 'S'
        WHERE T0."U_ARNO" = 'H' AND T0."DocDate" BETWEEN ? AND ?
        ORDER BY T0."DocDate" DESC, T0."DocNum" DESC
    '''
    try:
        raw = sap_connector.execute_query(sql, (start_date, end_date))
    except Exception as exc:
        logger.error('[HIDDEN-SALES] fetch failed: %s', exc)
        return {'status': 'error', 'rows': [], 'error': 'Could not read hidden invoices from SAP.',
                'start': str(start_date), 'end': str(end_date)}
    status_map = {'O': 'Open', 'C': 'Closed'}
    rows = []
    for r in raw:
        code = _normalize_name(r.get('ICODE'))
        stc = _normalize_name(r.get('ST'))
        state = _normalize_name(_delhi_gt_state(r.get('CCODE'), STATE_CODE_NAMES.get(stc, stc))) or '—'
        rows.append({
            'doc': str(r.get('DOCNUM') or '').strip(),
            'status': status_map.get(str(r.get('DSTATUS') or '').strip().upper(),
                                     str(r.get('DSTATUS') or '').strip()),
            'date': str(r.get('DDATE') or '').strip(),
            'card_code': _normalize_name(r.get('CCODE')),
            'customer': _normalize_name(r.get('CNAME')) or _normalize_name(r.get('CCODE')) or '—',
            'item_code': code,
            'item_name': _item_label(code, _normalize_name(r.get('INAME'))),
            'cost_center': _normalize_name(r.get('OCR')) or '—',
            'state': state,
            'main_group': _normalize_name(r.get('GRP')) or '—',
            'u_type': _normalize_name(r.get('UTYPE')) or '—',
            'qty': float(r.get('QTY') or 0),
            'litres': round(float(r.get('LIT') or 0), 2),
            'value': round(float(r.get('VAL') or 0), 2),
            'price': round(float(r.get('PRICE') or 0), 2),   # bill's own unit rate (INV1.Price)
            'combo': str(r.get('NIM') or 'N').strip().upper() == 'Y',   # no-inventory-movement / combo split line
            'debit_note': str(r.get('DNOTE') or 'N').strip().upper() == 'Y',   # whole invoice has no goods line
        })
    payload = {'status': 'ok', 'rows': rows, 'start': str(start_date), 'end': str(end_date)}
    if raw:
        _shared_set('hiddensales', key, payload, _HIDDEN_SALES_TTL)
    return payload


# ══════════════════════ Customer Master report ══════════════════════
# A clean master-data listing of every customer (OCRD, CardType='C') — the details that matter for
# a sales/account team: contact, GSTIN/PAN, address & location, sales person, payment terms, credit
# limit, balance and account status. GSTIN lives per-address in CRD1.GSTRegnNo (prefer the default
# bill-to address, else any address that has one); state name via OCST; terms via OCTG; sales
# person via OSLP. No internal SAP plumbing columns. Oil company (JIVO_OIL_HANADB).
_CUSTMASTER_TTL = 300   # 5 min — master data changes rarely


def get_customer_master():
    """Every customer (OCRD CardType='C') with the relevant master-data fields. Returns
    {status, rows, count}. Each row: code, name, main_group, contact_person, phone, mobile, email,
    gstin, pan, address, city, state, pincode, sales_person, payment_terms, credit_limit, balance,
    status ('Active' | 'Frozen' | 'Inactive'). Cached _CUSTMASTER_TTL seconds."""
    now = time.time()
    hit = _shared_get('custmaster', 'all')
    if hit is not None:
        return hit
    S = SAP_SCHEMA
    sql = f'''
        SELECT T."CardCode" AS "CODE", T."CardName" AS "NAME",
               COALESCE(TRIM(T."U_Main_Group"), '') AS "GRP",
               COALESCE(TRIM(T."CntctPrsn"), '')    AS "CONTACT",
               COALESCE(TRIM(T."Phone1"), '')       AS "PHONE",
               COALESCE(TRIM(T."Cellular"), '')     AS "MOBILE",
               COALESCE(TRIM(T."E_Mail"), '')       AS "EMAIL",
               COALESCE(TRIM(T."LicTradNum"), '')   AS "PAN",
               COALESCE(TRIM(T."Address"), '')      AS "ADDRESS",
               COALESCE(TRIM(T."City"), '')         AS "CITY",
               COALESCE(TRIM(T."ZipCode"), '')      AS "PIN",
               (SELECT MAX(K."Name") FROM "{S}"."OCST" K WHERE K."Code" = T."State1" AND K."Country" = T."Country") AS "STATE",
               COALESCE(TRIM(SL."SlpName"), '')     AS "SALESPERSON",
               COALESCE(TRIM(G."PymntGroup"), '')   AS "TERMS",
               COALESCE(T."CreditLine", 0)          AS "CREDIT",
               COALESCE(T."Balance", 0)             AS "BALANCE",
               T."validFor" AS "ACTIVE", T."frozenFor" AS "FROZEN",
               COALESCE(
                 (SELECT MAX(B1."GSTRegnNo") FROM "{S}"."CRD1" B1 WHERE B1."CardCode" = T."CardCode"
                    AND B1."Address" = T."BillToDef" AND B1."AdresType" = 'B'
                    AND TRIM(COALESCE(B1."GSTRegnNo", '')) <> ''),
                 (SELECT MAX(B2."GSTRegnNo") FROM "{S}"."CRD1" B2 WHERE B2."CardCode" = T."CardCode"
                    AND TRIM(COALESCE(B2."GSTRegnNo", '')) <> '')
               ) AS "GSTIN"
        FROM "{S}"."OCRD" T
        LEFT JOIN "{S}"."OSLP" SL ON SL."SlpCode" = T."SlpCode"
        LEFT JOIN "{S}"."OCTG" G  ON G."GroupNum" = T."GroupNum"
        WHERE T."CardType" = 'C'
        ORDER BY T."CardName"
    '''
    try:
        raw = sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[CUST-MASTER] fetch failed: %s', exc)
        return {'status': 'error', 'rows': [], 'count': 0,
                'error': 'Could not read the customer master from SAP.'}

    def _clean(v):
        return str(v or '').strip()

    rows = []
    for r in raw:
        sp = _clean(r.get('SALESPERSON'))
        if sp.upper().startswith('-NO SALES'):      # SAP placeholder "-No Sales Employee / Buyer-"
            sp = ''
        frozen = _clean(r.get('FROZEN')).upper() == 'Y'
        active = _clean(r.get('ACTIVE')).upper() == 'Y'
        status = 'Frozen' if frozen else ('Active' if active else 'Inactive')
        rows.append({
            'code': _clean(r.get('CODE')),
            'name': _clean(r.get('NAME')) or _clean(r.get('CODE')),
            'main_group': _clean(r.get('GRP')) or '—',
            'contact_person': _clean(r.get('CONTACT')),
            'mobile': _clean(r.get('MOBILE')) or _clean(r.get('PHONE')),   # one contact number
            'email': _clean(r.get('EMAIL')),
            'gstin': _clean(r.get('GSTIN')),
            'pan': _clean(r.get('PAN')),
            'address': _clean(r.get('ADDRESS')),
            'city': _clean(r.get('CITY')),
            'state': _clean(r.get('STATE')),
            'pincode': _clean(r.get('PIN')),
            'sales_person': sp,
            'payment_terms': _clean(r.get('TERMS')),
            'credit_limit': float(r.get('CREDIT') or 0),
            'balance': float(r.get('BALANCE') or 0),
            'status': status,
        })
    payload = {'status': 'ok', 'rows': rows, 'count': len(rows)}
    if rows:
        _shared_set('custmaster', 'all', payload, _CUSTMASTER_TTL)
    return payload


# ══════════════════════ Open Payments report ══════════════════════
# One row per incoming customer payment (SAP ORCT, DocType='C', not cancelled) whose posting date
# falls in the range. "Payment on account" is the receipt amount (ORCT.DocTotal); "Open balance" is
# the still-UNRECONCILED portion of that receipt, read live from its journal entry's BP line
# (JDT1.BalDueCred − BalDueDeb, maintained by the same B1 reconciliation engine that powers Customer
# Aging). A payment applied in full to invoices has open balance 0; a payment left on account keeps
# its full amount open until it is applied. The frontend's Open/Total toggle decides whether to show
# only the open (>0) rows or every payment. Oil company (JIVO_OIL_HANADB).
# ── Shared report cache ──────────────────────────────────────────────────────
# These reports used to keep their answers in a module-level dict. That works with a
# single process, but the server runs 2-8 gunicorn workers, and a dict lives inside
# ONE of them: the same report was fetched from SAP once per worker, and your second
# click could land on a worker that had never seen it. A restart threw the lot away.
#
# django.core.cache is shared by every worker and survives a restart (file-backed
# today, Redis by setting DJANGO_CACHE_URL). Same keys, same TTLs, same control flow -
# only where the answer is kept has changed.
#
# One thing to know: the shared cache stores a SERIALISED copy, so each caller gets its
# own object rather than the one everybody else is holding. For these reports that is
# simply safer - nobody can accidentally alter another request's cached answer.
def _shared_key(name, key):
    """A stable cache key. Tuples are flattened so ('2026-09-01','2026-09-08') and the
    string '2026-09-01|2026-09-08' can never collide with each other."""
    if isinstance(key, (tuple, list)):
        key = '|'.join(str(k) for k in key)
    return 'rz:%s:%s' % (name, key)


def _shared_get(name, key):
    """The cached answer, or None. Shaped like the old dicts: callers treat a miss the
    same way they always did."""
    from django.core.cache import cache
    return cache.get(_shared_key(name, key))


def _shared_set(name, key, value, ttl):
    from django.core.cache import cache
    cache.set(_shared_key(name, key), value, ttl)


_OPEN_PAYMENTS_TTL = 90


def _parse_ymd(s):
    """A 'YYYY-MM-DD' string → datetime.date, or None."""
    try:
        return datetime.strptime(str(s or '').strip()[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def get_open_payments(start_date, end_date):
    """Incoming customer payments (ORCT) posted in [start_date, end_date], each with its receipt
    amount ("payment on account") and current open/unreconciled balance. Returns
    {status, rows, start, end}; each row: date, doc_no, code, name, main_group, state, amount,
    open_bal. Rows are NOT filtered by open balance here — the client's Open/Total toggle does that
    (Open = open_bal > 0, Total = all) — but the caller-facing rule "hide zero open balance" is the
    default (Open). Cached _OPEN_PAYMENTS_TTL seconds. {} rows on any SAP error."""
    sd, ed = _parse_ymd(start_date), _parse_ymd(end_date)
    if not sd or not ed:
        return {'status': 'error', 'rows': [], 'error': 'start_date and end_date required',
                'start': start_date, 'end': end_date}
    if ed < sd:
        sd, ed = ed, sd
    key = (sd.isoformat(), ed.isoformat())
    now = time.time()
    hit = _shared_get('openpay', key)
    if hit is not None:
        return hit

    S = SAP_SCHEMA
    next_day = ed + timedelta(days=1)           # half-open range: correct for date or timestamp DocDate
    sql = f'''
        SELECT R."DocNum"  AS "docnum",
               R."DocDate" AS "docdate",
               R."CardCode" AS "code",
               COALESCE(TRIM(C."CardName"), '')     AS "name",
               COALESCE(TRIM(C."U_Main_Group"), '') AS "grp",
               (SELECT MAX(K."Name") FROM "{S}"."OCST" K
                  WHERE K."Code" = C."State1" AND K."Country" = C."Country") AS "state",
               COALESCE(R."DocTotal", 0) AS "amount",
               (SELECT SUM(COALESCE(J."BalDueCred", 0) - COALESCE(J."BalDueDeb", 0))
                  FROM "{S}"."JDT1" J
                  WHERE J."TransId" = R."TransId" AND J."ShortName" = R."CardCode") AS "openbal"
        FROM "{S}"."ORCT" R
        JOIN "{S}"."OCRD" C ON C."CardCode" = R."CardCode"
        WHERE R."DocType" = 'C' AND R."Canceled" = 'N'
          AND R."DocDate" >= ? AND R."DocDate" < ?
        ORDER BY R."DocDate", R."DocNum"
    '''
    try:
        raw = sap_connector.execute_query(sql, (sd, next_day))
    except Exception as exc:
        logger.error('[OPEN-PAY] fetch failed: %s', exc)
        return {'status': 'error', 'rows': [],
                'error': 'Could not read incoming payments from SAP.',
                'start': sd.isoformat(), 'end': ed.isoformat()}

    rows = []
    for r in raw:
        openbal = float(r.get('openbal') or 0)
        if abs(openbal) < 0.005:
            openbal = 0.0
        rows.append({
            'date': _aging_fmt_date(r.get('docdate')),
            'doc_no': str(r.get('docnum') or '').strip(),
            'code': str(r.get('code') or '').strip(),
            'name': (str(r.get('name') or '').strip() or str(r.get('code') or '').strip()).upper(),
            'main_group': (str(r.get('grp') or '').strip() or '—').upper(),
            'state': (str(r.get('state') or '').strip() or '—').upper(),
            'amount': round(float(r.get('amount') or 0), 2),
            'open_bal': round(openbal, 2),
        })
    payload = {'status': 'ok', 'rows': rows, 'count': len(rows),
               'start': sd.isoformat(), 'end': ed.isoformat()}
    if rows:
        _shared_set('openpay', key, payload, _OPEN_PAYMENTS_TTL)
    return payload


# ══════════════════════ Dispatch Details report ══════════════════════
# One row per A/R invoice (OINV) with its dispatch / logistics UDFs — dispatch date, bilty no &
# date, transporter, vehicle no and driver mobile — for the Oil company (Jivo Wellness). These
# are the header custom fields keyed on the SAP A/R Invoice's right-hand panel. The columns may
# be absent on a schema that never defined them, so the query is retried without them.
_DISPATCH_DETAILS_TTL = 90


def get_dispatch_details(start_date, end_date):
    """A/R invoices (OINV) dated in [start_date, end_date] for the Oil company, each with its
    dispatch/logistics custom fields. Returns {status, rows, count, start, end}; each row:
    inv_date, code, name, inv_no, dispatch (dispatch date), biltydate, bilty, transporter,
    vehicle, mobile (driver mobile). Non-cancelled item invoices only (open and closed), newest
    first. Cached _DISPATCH_DETAILS_TTL seconds. The dispatch UDFs may be absent on some schemas —
    the query is retried without them so the report still lists invoices. {} rows on any SAP error."""
    sd, ed = _parse_ymd(start_date), _parse_ymd(end_date)
    if not sd or not ed:
        return {'status': 'error', 'rows': [], 'error': 'start_date and end_date required',
                'start': start_date, 'end': end_date}
    if ed < sd:
        sd, ed = ed, sd
    key = (sd.isoformat(), ed.isoformat())
    now = time.time()
    hit = _shared_get('dispatch', key)
    if hit is not None:
        return hit

    S = SAP_SCHEMA
    next_day = ed + timedelta(days=1)           # half-open range: correct for date or timestamp DocDate
    # Dispatch/bilty custom fields as named on the Oil OINV (Jivo Wellness). NB the Oil column
    # names differ from Beverages — U_BilltyNumber (double-L) and U_VehicleNoM here vs the
    # U_BiltyNumber / U_VechileNom used by the Beverages aging query. Retried-without on absence.
    udf = ('''T0."U_Dipatch_Date" AS "dispatch", T0."U_BiltyDate" AS "biltydate",
               T0."U_BilltyNumber" AS "bilty", T0."U_TransporterName" AS "transporter",
               T0."U_VehicleNoM" AS "vehicle", T0."U_Mob_No" AS "mobile",''')

    def build(extras):
        return f'''
        SELECT T0."DocNum" AS "docnum",
               TO_VARCHAR(T0."DocDate",'YYYY-MM-DD') AS "invdate",
               T0."CardCode" AS "code",
               COALESCE(TRIM(T0."CardName"), '') AS "name",
               {extras}T0."DocStatus" AS "status"
        FROM "{S}"."OINV" T0
        WHERE T0."DocType" = 'I' AND T0."CANCELED" = 'N'
          AND T0."DocDate" >= ? AND T0."DocDate" < ?
        ORDER BY T0."DocDate" DESC, T0."DocNum" DESC
    '''
    try:
        raw = sap_connector.execute_query(build(udf), (sd, next_day))
    except Exception:
        try:                                    # schema without the dispatch/bilty UDFs
            raw = sap_connector.execute_query(build(''), (sd, next_day))
        except Exception as exc:
            logger.error('[DISPATCH] fetch failed: %s', exc)
            return {'status': 'error', 'rows': [],
                    'error': 'Could not read dispatch details from SAP.',
                    'start': sd.isoformat(), 'end': ed.isoformat()}

    rows = []
    for r in raw or []:
        code = _bev_cell(r.get('code'))
        rows.append({
            'inv_date': _bev_cell(r.get('invdate')),
            'code': code,
            'name': (_bev_cell(r.get('name')) or code).upper(),
            'inv_no': _bev_cell(r.get('docnum')),
            'dispatch': _bev_cell(r.get('dispatch')),
            'biltydate': _bev_cell(r.get('biltydate')),
            'bilty': _bev_cell(r.get('bilty')),
            'transporter': _bev_cell(r.get('transporter')),
            'vehicle': _bev_cell(r.get('vehicle')),
            'mobile': _bev_cell(r.get('mobile')),
        })
    payload = {'status': 'ok', 'rows': rows, 'count': len(rows),
               'start': sd.isoformat(), 'end': ed.isoformat()}
    if rows:
        _shared_set('dispatch', key, payload, _DISPATCH_DETAILS_TTL)
    return payload


# ══════════════════════ Realise Calculator report ══════════════════════
# Per-item Oil realisation for a date range. Realisation = Revenue ÷ Volume, at two grains:
#   ₹/Litre = LineTotal ÷ (Quantity × OITM.SalPackUn)   — litres per piece
#   ₹/Box   = LineTotal ÷ (Quantity ÷ OITM.SalFactor2)  — pieces per box
# Net of returns: OINV/INV1 add (sign +1), ORIN/RIN1 subtract (sign −1). Hidden invoices
# (U_ARNO IN 'T','H') are excluded to match the dashboard's "Done". Cancelled docs dropped.
_REALISE_CALC_TTL = 90        # seconds

_REALISE_CALC_SQL = '''
    SELECT COALESCE(TRIM(I."ItemCode"), '')     AS "ICODE",
           COALESCE(TRIM(I."ItemName"), '')     AS "ITEM",
           COALESCE(TRIM(I."U_TYPE"), '')       AS "UTYPE",
           COALESCE(TRIM(I."U_Sub_Group"), '')  AS "SUBG",
           {sign} * SUM(L."Quantity")                                        AS "PCS",
           {sign} * SUM(L."Quantity" * COALESCE(I."SalPackUn", 0))           AS "LIT",
           {sign} * SUM(L."Quantity" / NULLIF(I."SalFactor2", 0))            AS "BOX",
           {sign} * SUM(L."LineTotal")                                       AS "REV"
    FROM "{S}"."{hdr}" H
    JOIN "{S}"."{ln}" L ON L."DocEntry" = H."DocEntry"
    LEFT JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode"
    WHERE H."DocDate" >= ? AND H."DocDate" < ? AND H."CANCELED" = 'N'
      AND (H."U_ARNO" NOT IN ('T', 'H') OR H."U_ARNO" IS NULL)
    GROUP BY I."ItemCode", I."ItemName", I."U_TYPE", I."U_Sub_Group"
'''


def get_realise_calculator(start_date, end_date):
    """Per-item Oil realisation (₹/Litre and ₹/Box) for [start_date, end_date]. Returns
    {status, rows, totals, count, start, end}. Each row: item_code, item_name, u_type,
    u_sub_group, pcs, litres, boxes, revenue, realise_l, realise_box. Net of credit
    notes/returns. Cached _REALISE_CALC_TTL seconds. {} rows on any SAP error."""
    sd, ed = _parse_ymd(start_date), _parse_ymd(end_date)
    if not sd or not ed:
        return {'status': 'error', 'rows': [], 'totals': {},
                'error': 'start_date and end_date required',
                'start': start_date, 'end': end_date}
    if ed < sd:
        sd, ed = ed, sd
    key = (sd.isoformat(), ed.isoformat())
    now = time.time()
    hit = _shared_get('realisecalc', key)
    if hit is not None:
        return hit

    S = SAP_SCHEMA
    next_day = ed + timedelta(days=1)          # half-open range: correct for date or timestamp DocDate
    inv = _REALISE_CALC_SQL.format(S=S, hdr='OINV', ln='INV1', sign='1')
    ret = _REALISE_CALC_SQL.format(S=S, hdr='ORIN', ln='RIN1', sign='-1')
    try:
        raw = sap_connector.execute_query(inv, (sd, next_day))
        raw += sap_connector.execute_query(ret, (sd, next_day))
    except Exception as exc:
        logger.error('[REALISE-CALC] fetch failed: %s', exc)
        return {'status': 'error', 'rows': [], 'totals': {},
                'error': 'Could not read sales from SAP.',
                'start': sd.isoformat(), 'end': ed.isoformat()}

    agg = {}       # item_code -> accumulated metrics
    for r in raw or []:
        code = _bev_cell(r.get('ICODE'))
        name = _bev_cell(r.get('ITEM'))
        cell = agg.get(code)
        if cell is None:
            cell = agg[code] = {
                'item_code': code,
                'item_name': (name or code).upper(),
                'u_type': (_bev_cell(r.get('UTYPE')) or '').upper(),
                'u_sub_group': (_bev_cell(r.get('SUBG')) or '').upper(),
                'pcs': 0.0, 'litres': 0.0, 'boxes': 0.0, 'revenue': 0.0,
            }
        cell['pcs']     += float(r.get('PCS') or 0)
        cell['litres']  += float(r.get('LIT') or 0)
        cell['boxes']   += float(r.get('BOX') or 0)
        cell['revenue'] += float(r.get('REV') or 0)

    rows = []
    tot_pcs = tot_lit = tot_box = tot_rev = 0.0
    for c in agg.values():
        lit, box, rev = c['litres'], c['boxes'], c['revenue']
        # Drop items that net to nothing (a sale fully cancelled by a return in the period).
        if round(rev, 2) == 0 and round(lit, 2) == 0 and round(box, 2) == 0:
            continue
        c['pcs']         = round(c['pcs'], 2)
        c['litres']      = round(lit, 2)
        c['boxes']       = round(box, 2)
        c['revenue']     = round(rev, 2)
        c['realise_l']   = round(rev / lit, 2) if lit else 0.0
        c['realise_box'] = round(rev / box, 2) if box else 0.0
        rows.append(c)
        tot_pcs += c['pcs']; tot_lit += c['litres']; tot_box += c['boxes']; tot_rev += c['revenue']

    rows.sort(key=lambda x: x['revenue'], reverse=True)
    totals = {
        'pcs': round(tot_pcs, 2),
        'litres': round(tot_lit, 2),
        'boxes': round(tot_box, 2),
        'revenue': round(tot_rev, 2),
        'realise_l': round(tot_rev / tot_lit, 2) if tot_lit else 0.0,
        'realise_box': round(tot_rev / tot_box, 2) if tot_box else 0.0,
    }
    payload = {'status': 'ok', 'rows': rows, 'totals': totals, 'count': len(rows),
               'start': sd.isoformat(), 'end': ed.isoformat()}
    if rows:
        _shared_set('realisecalc', key, payload, _REALISE_CALC_TTL)
    return payload


# ══════════════════════ Plan vs Done (Rate List achievement) ══════════════════════
# Per-item Done litres/revenue for one month — the actual side of the Plan vs Done tab, joined
# to a saved Rate List by ItemCode.
# STATE IS THE CUSTOMER'S OWN (OCRD.State1), not the ship-to address: a Delhi HORECA account
# delivering to a hotel in Gurgaon is still Delhi's business, and ship-to left such territories
# reading empty. This DIFFERS from the Sales dashboard, which attributes by ship-to, so per-state
# figures between the two tabs will not agree for accounts that buy in one state and take
# delivery in another (CSD, HORECA chains, corporate).
# FINISHED GOODS ONLY (ItemCode LIKE 'FG%'), same as _COMPARE_LINE_SQL: invoices also carry
# packaging (PM — glass jars, caps, cartons) and raw material (RM) lines, which are not
# sellable SKUs and have no place in a sales plan.
# Litres use _done_sales_litexpr (the REPORT_SALES_COGS formula the dashboard's Done runs
# on), NOT Quantity x SalPackUn: the naive form ignores combo/BOM expansion, the
# NoInvtryMv='Y' zeroing and the MTS bulk conversion, and inflates litres several-fold.
_DONE_ITEM_TTL = 90               # seconds

_DONE_ITEM_SQL = '''
    SELECT COALESCE(TRIM(I."ItemCode"), '')    AS "ICODE",
           COALESCE(TRIM(I."ItemName"), '')    AS "ITEM",
           COALESCE(I."SalFactor2", 0)         AS "PCSBOX",
           COALESCE(I."SalPackUn", 0)          AS "LTRPP",
           COALESCE(TRIM(H."CardCode"), '')    AS "CCODE",
           COALESCE(TRIM(C."U_Main_Group"), '') AS "GRP",
           COALESCE(TRIM(C."State1"), '')      AS "ST",
           SUM({litexpr})                      AS "LIT",
           SUM({sign} * L."Quantity")          AS "PCS",
           SUM({sign} * L."LineTotal")         AS "REV"
    FROM "{S}"."{hdr}" H
    JOIN "{S}"."{ln}" L ON L."DocEntry" = H."DocEntry"
    JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode"
    LEFT JOIN "{S}"."OCRD" C ON C."CardCode" = H."CardCode"
    WHERE H."DocDate" >= ? AND H."DocDate" < ? AND H."CANCELED" = 'N'
      AND (H."U_ARNO" NOT IN ('T', 'H') OR H."U_ARNO" IS NULL)
      AND I."ItemCode" LIKE 'FG%'
    GROUP BY I."ItemCode", I."ItemName", I."SalFactor2", I."SalPackUn",
             H."CardCode", C."U_Main_Group", C."State1"
'''


def norm_state(name):
    """Fold a state name to one spelling so a saved Rate List's 'Jammu & Kashmir' matches
    STATE_CODE_NAMES' 'JAMMU AND KASHMIR'."""
    s = str(name or '').upper().replace('&', ' AND ')
    return ' '.join(s.split())


def _raw_group_to_channel():
    """Raw OCRD.U_Main_Group -> one of the 7 dashboard channels, so Plan vs Done buckets sales
    exactly the way the Sales dashboard does (CORPORATE/BRANCH/STAFF/... all fold into REST)."""
    out = {}
    for channel, members in CHANNEL_MEMBERS.items():
        for raw in members:
            out[_normalize_name(raw)] = channel
    return out


def _done_accum(bucket, code, r):
    cell = bucket.get(code)
    if cell is None:
        # pcsbox / ltr_per_pc come off the item master, so they are per-item constants,
        # not sums: SalFactor2 = pieces per box, SalPackUn = litres per piece.
        cell = bucket[code] = {'item_name': (r.get('ITEM') or code).strip().upper(),
                               'pcsbox': float(r.get('PCSBOX') or 0),
                               'ltr_per_pc': float(r.get('LTRPP') or 0),
                               'litres': 0.0, 'pcs': 0.0, 'revenue': 0.0}
    cell['litres']  += float(r.get('LIT') or 0)
    cell['pcs']     += float(r.get('PCS') or 0)
    cell['revenue'] += float(r.get('REV') or 0)


def _done_finalize(bucket):
    for cell in bucket.values():
        pcs, rev, lit = cell['pcs'], cell['revenue'], cell['litres']
        boxes = (pcs / cell['pcsbox']) if cell['pcsbox'] else 0.0
        cell['litres']    = round(lit, 2)
        cell['pcs']       = round(pcs, 2)               # INV1.Quantity — PIECES, not cartons
        cell['boxes']     = round(boxes, 2)
        cell['revenue']   = round(rev, 2)
        cell['realise_l'] = round(rev / lit, 2) if lit else 0.0
        cell['rate_pc']   = round(rev / pcs, 2) if pcs else 0.0
        cell['rate_box']  = round(rev / boxes, 2) if boxes else 0.0
        # Litres per box — what the calculator grid calls "Box Ltrs".
        cell['boxltr']    = round(cell['ltr_per_pc'] * cell['pcsbox'], 2)
    return bucket


_FG_TYPES_TTL = 600               # seconds — the item master barely moves


def get_fg_item_types():
    """{ITEM_CODE: U_TYPE} for every finished-goods item (PREMIUM / COMMODITY / OTHERS).
    Segment always comes from OITM.U_TYPE, never item-name matching — SAP tags items like
    COLD PRESS 1 LTR as CANOLA with no hint of it in the name. Covers the whole master, not
    just what sold, because a planned item with no sale this month still has to be filterable.
    Cached _FG_TYPES_TTL seconds; {} on any SAP error."""
    now = time.time()
    hit = _shared_get('fgtypes', 'v')
    if hit is not None:
        return hit
    sql = f'''SELECT COALESCE(TRIM(I."ItemCode"), '') AS "CODE",
                     COALESCE(TRIM(I."U_TYPE"), '')   AS "UTYPE"
              FROM "{SAP_SCHEMA}"."OITM" I WHERE I."ItemCode" LIKE 'FG%' '''
    try:
        rows = sap_connector.execute_query(sql) or []
    except Exception as exc:
        logger.error('[FG-TYPES] fetch failed: %s', exc)
        return {}
    out = {(r.get('CODE') or '').strip().upper(): (r.get('UTYPE') or '').strip().upper()
           for r in rows if (r.get('CODE') or '').strip()}
    _shared_set('fgtypes', 'v', out, _FG_TYPES_TTL)
    return out


def get_territory_targets(month, year):
    """Targets set on the Update Targets screen, as {'CHANNEL|STATE': {segment: {ltrs, rate}}}
    for one month. Segment keys are 'PREMIUM', 'COMMODITY' and 'ALL' (every segment pooled).

    Source is TargetNode, whose grain is (main_group, state, sales_person, segment). Rows are
    summed across sales_person: a state's target is the state's target however many people it
    was entered under. A pooled RATE has to be volume-weighted — averaging 220 and 180 would
    invent a number neither target asked for — so rates are re-derived from pooled value over
    pooled litres. Nodes with no state are skipped: Plan vs Done scopes by state, and a
    channel-wide target cannot be attributed to one."""
    out = {}
    rows = TargetNode.objects.filter(month=int(month), year=int(year)).exclude(state='')
    for n in rows:
        ch = _normalize_name(n.main_group)
        st = norm_state(n.state)
        if not ch or not st:
            continue
        seg = _normalize_name(n.segment) or 'ALL'
        ltrs = float(n.target_ltrs or 0)
        rate = float(n.target_realise or 0)
        if ltrs <= 0 and rate <= 0:
            continue
        bucket = out.setdefault(ch + '|' + st, {})
        for key in (seg, 'ALL'):
            cell = bucket.setdefault(key, {'ltrs': 0.0, 'value': 0.0})
            cell['ltrs'] += ltrs
            cell['value'] += ltrs * rate
    for bucket in out.values():
        for cell in bucket.values():
            cell['rate'] = round(cell['value'] / cell['ltrs'], 2) if cell['ltrs'] else 0.0
            cell['ltrs'] = round(cell['ltrs'], 2)
            cell['value'] = round(cell['value'], 2)
    return out


def get_done_by_item(start_date, end_date):
    """Done sales per item for [start_date, end_date], net of returns. Returns {status, rows,
    by_state, states, start, end}: `rows` is the all-India map {ITEM_CODE: {...}} and
    `by_state` the same shape per state, so the Plan vs Done page can scope a saved Rate List
    to the state it was planned for without a second round trip.

    State is the CUSTOMER'S BILLING state (OCRD.State1), not the ship-to address, and carries
    the Delhi-GT remap. This DIFFERS from the sales dashboard, which attributes by ship-to
    (CRD1): a Delhi account delivering to a hotel in Gurgaon counts as Delhi here and as
    Haryana there. Both totals agree nationally; only the per-state split differs.

    Hidden (U_ARNO 'T'/'H') and cancelled documents excluded. FG items only.
    Cached _DONE_ITEM_TTL seconds."""
    sd, ed = _parse_ymd(start_date), _parse_ymd(end_date)
    if not sd or not ed:
        return {'status': 'error', 'rows': {}, 'error': 'start_date and end_date required',
                'start': start_date, 'end': end_date}
    if ed < sd:
        sd, ed = ed, sd
    key = (sd.isoformat(), ed.isoformat())
    now = time.time()
    hit = _shared_get('doneitem', key)
    if hit is not None:
        return hit

    S = SAP_SCHEMA
    next_day = ed + timedelta(days=1)          # half-open: safe for date or timestamp DocDate
    inv = _DONE_ITEM_SQL.format(S=S, hdr='OINV', ln='INV1', sign='1',
                                litexpr=_done_sales_litexpr(S))
    ret = _DONE_ITEM_SQL.format(S=S, hdr='ORIN', ln='RIN1', sign='-1',
                                litexpr=_DONE_RETURN_LITEXPR)
    try:
        raw = sap_connector.execute_query(inv, (sd, next_day))
        raw += sap_connector.execute_query(ret, (sd, next_day))
    except Exception as exc:
        logger.error('[DONE-BY-ITEM] fetch failed: %s', exc)
        return {'status': 'error', 'rows': {}, 'error': 'Could not read sales from SAP.',
                'start': sd.isoformat(), 'end': ed.isoformat()}

    raw2ch = _raw_group_to_channel()
    agg = {}
    # Two attribution bases. SHIP-TO is the dashboard's rule (where the goods went) and stays
    # the default. BILL-TO is the customer's own state, which is the honest lens for accounts
    # that buy in one state and take delivery in another — a Delhi HORECA account shipping to a
    # hotel in Gurgaon is still Delhi's business.
    by_state, by_sc = {}, {}
    # Channel across every state. Not derivable from by_state_channel: rows with no state at all
    # are excluded from those buckets and would silently drop out of a channel roll-up.
    by_channel = {}
    for r in raw or []:
        code = (r.get('ICODE') or '').strip().upper()
        if not code:
            continue
        channel = raw2ch.get(_normalize_name(r.get('GRP')), 'REST')
        _done_accum(agg, code, r)
        _done_accum(by_channel.setdefault(channel, {}), code, r)
        raw_st = (r.get('ST') or '').strip()
        state = norm_state(_delhi_gt_state(r.get('CCODE'), STATE_CODE_NAMES.get(raw_st, raw_st)))
        if state:
            _done_accum(by_state.setdefault(state, {}), code, r)
            _done_accum(by_sc.setdefault(state + '|' + channel, {}), code, r)

    _done_finalize(agg)
    for bucket in by_channel.values():
        _done_finalize(bucket)
    for bucket in by_state.values():
        _done_finalize(bucket)
    for bucket in by_sc.values():
        _done_finalize(bucket)

    payload = {'status': 'ok', 'rows': agg, 'count': len(agg),
               'by_state': by_state, 'states': sorted(by_state),
               'by_state_channel': by_sc, 'channels': sorted(CHANNEL_MEMBERS),
               'by_channel': by_channel,
               'item_types': get_fg_item_types(),
               'start': sd.isoformat(), 'end': ed.isoformat()}
    if agg:
        _shared_set('doneitem', key, payload, _DONE_ITEM_TTL)
    return payload


_DONE_DOCS_TTL = 90               # seconds

_DONE_DOCS_SQL = '''
    SELECT H."DocNum"                                            AS "DOCNUM",
           TO_VARCHAR(CAST(H."DocDate" AS DATE), 'YYYY-MM-DD')   AS "DOCDATE",
           COALESCE(TRIM(H."CardCode"), '')                      AS "CCODE",
           COALESCE(TRIM(C."CardName"), '')                      AS "CUST",
           COALESCE(TRIM(C."U_Main_Group"), '')                  AS "GRP",
           COALESCE(TRIM(C."State1"), '')                        AS "ST",
           '{kind}'                                              AS "KIND",
           SUM({litexpr})                                        AS "LIT",
           SUM({sign} * L."Quantity")                            AS "QTY",
           SUM({sign} * L."LineTotal")                           AS "VAL"
    FROM "{S}"."{hdr}" H
    JOIN "{S}"."{ln}" L ON L."DocEntry" = H."DocEntry"
    JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode"
    LEFT JOIN "{S}"."OCRD" C ON C."CardCode" = H."CardCode"
    WHERE L."ItemCode" = ? AND H."DocDate" >= ? AND H."DocDate" < ? AND H."CANCELED" = 'N'
      AND (H."U_ARNO" NOT IN ('T', 'H') OR H."U_ARNO" IS NULL)
    GROUP BY H."DocNum", H."DocDate", H."CardCode", C."CardName", C."U_Main_Group",
             C."State1"
'''


def get_done_item_documents(item_code, start_date, end_date, state=None, channel=None):
    """Who bought one item in a month: the invoices (and credit notes) behind its Done figure,
    with party, state, document, litres, pieces and value. State is the customer's own, the same
    basis get_done_by_item uses, so the drill-down keeps reconciling to the row it hangs under.
    Same filters and litre
    formula as get_done_by_item, so the rows sum exactly to that item's Done row — and with
    `state` set, to that state's row. Returns {status, code, rows, totals}; rows carry kind
    'INV' or 'CN' (returns, negative). Cached _DONE_DOCS_TTL seconds per (item, range, state)."""
    code = str(item_code or '').strip().upper()
    want_state = norm_state(state) if state else ''
    want_chan = _normalize_name(channel) if channel else ''
    sd, ed = _parse_ymd(start_date), _parse_ymd(end_date)
    if not code or not sd or not ed:
        return {'status': 'error', 'code': code, 'rows': [], 'totals': {},
                'error': 'item code and dates required'}
    if ed < sd:
        sd, ed = ed, sd
    key = (code, sd.isoformat(), ed.isoformat(), want_state, want_chan)
    now = time.time()
    hit = _shared_get('donedocs', key)
    if hit is not None:
        return hit

    S = SAP_SCHEMA
    next_day = ed + timedelta(days=1)
    inv = _DONE_DOCS_SQL.format(S=S, hdr='OINV', ln='INV1', sign='1', kind='INV',
                                litexpr=_done_sales_litexpr(S))
    ret = _DONE_DOCS_SQL.format(S=S, hdr='ORIN', ln='RIN1', sign='-1', kind='CN',
                                litexpr=_DONE_RETURN_LITEXPR)
    try:
        raw = sap_connector.execute_query(inv, (code, sd, next_day))
        raw += sap_connector.execute_query(ret, (code, sd, next_day))
    except Exception as exc:
        logger.error('[DONE-DOCS] fetch failed for %s: %s', code, exc)
        return {'status': 'error', 'code': code, 'rows': [], 'totals': {},
                'error': 'Could not read invoices from SAP.'}

    raw2ch = _raw_group_to_channel()
    rows, t_lit, t_qty, t_val = [], 0.0, 0.0, 0.0
    for r in raw or []:
        lit, qty, val = (float(r.get(k) or 0) for k in ('LIT', 'QTY', 'VAL'))
        if not (round(lit, 2) or round(qty, 2) or round(val, 2)):
            continue
        st_raw = (r.get('ST') or '').strip()
        row_state = _delhi_gt_state(r.get('CCODE'), STATE_CODE_NAMES.get(st_raw, st_raw))
        row_chan = raw2ch.get(_normalize_name(r.get('GRP')), 'REST')
        if want_state and norm_state(row_state) != want_state:
            continue
        if want_chan and row_chan != want_chan:
            continue
        rows.append({
            'doc_num': r.get('DOCNUM'), 'doc_date': r.get('DOCDATE'),
            'card_code': (r.get('CCODE') or '').strip(),
            'customer': (r.get('CUST') or '').strip().upper() or (r.get('CCODE') or ''),
            'channel': row_chan, 'raw_group': (r.get('GRP') or '').strip().upper(),
            'state': row_state,
            'kind': r.get('KIND'), 'litres': round(lit, 2), 'qty': round(qty, 2),
            'revenue': round(val, 2), 'realise_l': round(val / lit, 2) if lit else 0.0,
        })
        t_lit += lit; t_qty += qty; t_val += val

    rows.sort(key=lambda x: x['revenue'], reverse=True)
    payload = {'status': 'ok', 'code': code, 'rows': rows, 'count': len(rows),
               'totals': {'litres': round(t_lit, 2), 'qty': round(t_qty, 2),
                          'revenue': round(t_val, 2),
                          'realise_l': round(t_val / t_lit, 2) if t_lit else 0.0},
               'start': sd.isoformat(), 'end': ed.isoformat()}
    _shared_set('donedocs', key, payload, _DONE_DOCS_TTL)
    return payload


_REALISE_CALC_ITEMS_TTL = 600      # seconds — the item master barely changes

def get_realise_calc_items():
    """Oil finished-goods item master for the Realise Calculator's cascading item picker.
    Returns {'status', 'items'}; each item: code, name, variety (U_Sub_Group), sku (U_SKU
    pack size), type ('P'/'C'), litres_per_pack (OITM.SalPackUn — litres per selling unit,
    used to auto-fill the grid's Litres/Pack). Sellable Premium/Commodity items only.
    Cached _REALISE_CALC_ITEMS_TTL seconds; [] on any SAP error."""
    now = time.time()
    hit = _shared_get('calcitems', 'v')
    if hit is not None:
        return {'status': 'ok', 'items': hit}

    S = SAP_SCHEMA
    sql = f'''
        SELECT COALESCE(TRIM(I."ItemCode"), '')     AS "CODE",
               COALESCE(TRIM(I."ItemName"), '')     AS "NAME",
               COALESCE(TRIM(I."U_Sub_Group"), '')  AS "VARIETY",
               COALESCE(TRIM(I."U_SKU"), '')        AS "SKU",
               COALESCE(TRIM(I."U_TYPE"), '')       AS "TYPE",
               COALESCE(I."SalPackUn", 0)           AS "LPP",
               COALESCE(I."SalFactor2", 0)          AS "PPB"
        FROM "{S}"."OITM" I
        WHERE I."SellItem" = 'Y'
          AND UPPER(COALESCE(TRIM(I."U_TYPE"), '')) IN ('PREMIUM', 'COMMODITY')
          AND COALESCE(TRIM(I."ItemName"), '') <> ''
        ORDER BY "VARIETY", "SKU", "NAME", "CODE"   -- CODE breaks ties so the order never wobbles
    '''
    try:
        raw = sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[REALISE-CALC ITEMS] fetch failed: %s', exc)
        return {'status': 'error', 'items': [], 'error': 'Could not read the item list from SAP.'}

    items = []
    for r in raw or []:
        t = (_bev_cell(r.get('TYPE')) or '').upper()
        lpp = round(float(r.get('LPP') or 0), 3)          # litres per selling unit (piece)
        ppb = round(float(r.get('PPB') or 0), 2)          # pieces per box (SalFactor2)
        items.append({
            'code': _bev_cell(r.get('CODE')),
            'name': (_bev_cell(r.get('NAME')) or '').upper(),
            'variety': (_bev_cell(r.get('VARIETY')) or '—').upper(),
            'sku': (_bev_cell(r.get('SKU')) or '—').upper(),
            'type': 'P' if t == 'PREMIUM' else ('C' if t == 'COMMODITY' else ''),
            'litres_per_pack': lpp,
            'pcs_per_box': ppb,
            'box_litres': round(lpp * ppb, 3),            # litres in one box
        })
    _shared_set('calcitems', 'v', items, _REALISE_CALC_ITEMS_TTL)
    return {'status': 'ok', 'items': items}


# ══════════════════════ Sales Document Flow report ══════════════════════
# The sales document chain per party for a day's invoices: Sales Quotation → Sales Order → A/R
# Invoice, with the invoiced litres. Anchored on invoices (OINV) in the date range (excluding
# cancelled + hidden, matching Done). Each invoice line is traced to its Sales Order — directly
# (INV1.BaseType=17) or via a Delivery (BaseType=15 → DLN1.BaseType=17) — and each order line to
# its Quotation (RDR1.BaseType=23 → OQUT). Litres = Quantity × SalPackUn.
_SALES_FLOW_TTL = 90


# The OMS software integrates into SAP as the B1i integration user; every document it raises
# is stamped with that user in UserSign. Anything else was keyed by a person inside SAP.
_OMS_USER_CODE = 'B1I'


def _doc_source(ucode, uname):
    """Classify a document's creator (from OUSR) as OMS vs a manual SAP user. Returns None when
    there is no document (blank order/quotation), so the frontend shows nothing for that cell."""
    code = str(ucode or '').strip()
    if not code:
        return None
    name = str(uname or '').strip()
    is_oms = code.upper() == _OMS_USER_CODE
    return {'oms': is_oms, 'label': 'OMS' if is_oms else (name or code), 'user': name or code}


def get_sales_document_flow(start_date, end_date, company='oil'):
    """Rows of Party / Quotation No / Order No / Invoice No / qty for invoices dated in
    [start_date, end_date] for `company` ('oil' | 'beverages'). One row per (invoice, order,
    quotation) chain — an invoice drawn from several orders shows one row each. Order/Quotation are
    blank when the invoice (or order) was raised directly. `qty` is Litres (oil, Qty×SalPackUn) or
    Boxes (beverages, Qty/SalFactor2); `measure` names it. Returns {status, company, measure, rows,
    start, end}. Cached _SALES_FLOW_TTL seconds."""
    company = 'beverages' if str(company or '').lower().startswith('bev') else 'oil'
    measure = 'Boxes' if company == 'beverages' else 'Litres'
    key = (company, str(start_date), str(end_date))
    now = time.time()
    hit = _shared_get('salesflow', key)
    if hit is not None:
        return hit
    if company == 'beverages':
        S = BEVERAGES_SCHEMA
        qty = 'L."Quantity" / NULLIF(I."SalFactor2", 0)'
        # beverages: finished goods only (join item group), skip internal group-100 parties
        item_join = (f'JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode" '
                     f'JOIN "{S}"."OITB" G ON G."ItmsGrpCod" = I."ItmsGrpCod" '
                     f'JOIN "{S}"."OCRD" CU ON CU."CardCode" = H."CardCode"')
        extra_where = 'AND CU."GroupCode" <> 100 AND L."TreeType" <> \'I\' AND G."ItmsGrpNam" = \'FINISHED\''
    else:
        S = SAP_SCHEMA
        qty = 'L."Quantity" * COALESCE(I."SalPackUn", 0)'
        item_join = f'LEFT JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode"'
        extra_where = 'AND (H."U_ARNO" NOT IN (\'T\', \'H\') OR H."U_ARNO" IS NULL)'   # exclude hidden, like Done
    sql = f'''
        SELECT H."DocNum"   AS "INV",
               TO_VARCHAR(CAST(H."DocDate" AS DATE), 'YYYY-MM-DD') AS "DDATE",
               COALESCE(TRIM(H."CardCode"), '') AS "CCODE",
               COALESCE(TRIM(H."CardName"), '') AS "PARTY",
               COALESCE(O1."DocNum", O2."DocNum") AS "ORDER_NO",
               COALESCE(Q1."DocNum", Q2."DocNum") AS "QUOT_NO",
               -- creator of each document (OUSR via UserSign): B1i = OMS, else a manual SAP user.
               -- MAX() keeps them out of GROUP BY (constant within an invoice/order/quotation).
               MAX(UQ."USER_CODE") AS "QUOT_UCODE", MAX(UQ."U_NAME") AS "QUOT_UNAME",
               MAX(UO."USER_CODE") AS "ORD_UCODE",  MAX(UO."U_NAME") AS "ORD_UNAME",
               MAX(UI."USER_CODE") AS "INV_UCODE",  MAX(UI."U_NAME") AS "INV_UNAME",
               -- open/closed (DocStatus 'O'/'C') of each document, for the Open-Doc filter
               MAX(COALESCE(Q1."DocStatus", Q2."DocStatus")) AS "QUOT_ST",
               MAX(COALESCE(O1."DocStatus", O2."DocStatus")) AS "ORD_ST",
               MAX(H."DocStatus") AS "INV_ST",
               SUM({qty}) AS "QTY"
        FROM "{S}"."OINV" H
        JOIN "{S}"."INV1" L ON L."DocEntry" = H."DocEntry"
        {item_join}
        -- invoice line based directly on a Sales Order (BaseType 17)
        LEFT JOIN "{S}"."ORDR" O1 ON L."BaseType" = 17 AND O1."DocEntry" = L."BaseEntry"
        LEFT JOIN "{S}"."RDR1" R1 ON L."BaseType" = 17 AND R1."DocEntry" = L."BaseEntry" AND R1."LineNum" = L."BaseLine"
        LEFT JOIN "{S}"."OQUT" Q1 ON R1."BaseType" = 23 AND Q1."DocEntry" = R1."BaseEntry"
        -- invoice line based on a Delivery (BaseType 15) → its Sales Order
        LEFT JOIN "{S}"."DLN1" D  ON L."BaseType" = 15 AND D."DocEntry" = L."BaseEntry" AND D."LineNum" = L."BaseLine"
        LEFT JOIN "{S}"."ORDR" O2 ON D."BaseType" = 17 AND O2."DocEntry" = D."BaseEntry"
        LEFT JOIN "{S}"."RDR1" R2 ON D."BaseType" = 17 AND R2."DocEntry" = D."BaseEntry" AND R2."LineNum" = D."BaseLine"
        LEFT JOIN "{S}"."OQUT" Q2 ON R2."BaseType" = 23 AND Q2."DocEntry" = R2."BaseEntry"
        -- who created each document (OUSR.USERID = the header's UserSign / "Created By")
        LEFT JOIN "{S}"."OUSR" UI ON UI."USERID" = H."UserSign"
        LEFT JOIN "{S}"."OUSR" UO ON UO."USERID" = COALESCE(O1."UserSign", O2."UserSign")
        LEFT JOIN "{S}"."OUSR" UQ ON UQ."USERID" = COALESCE(Q1."UserSign", Q2."UserSign")
        WHERE CAST(H."DocDate" AS DATE) BETWEEN ? AND ? AND H."CANCELED" = 'N' {extra_where}
        GROUP BY H."DocNum", CAST(H."DocDate" AS DATE), H."CardCode", H."CardName",
                 COALESCE(O1."DocNum", O2."DocNum"), COALESCE(Q1."DocNum", Q2."DocNum")
        ORDER BY H."DocNum" DESC
    '''
    try:
        raw = sap_connector.execute_query(sql, (start_date, end_date))
    except Exception as exc:
        logger.error('[SALES-FLOW] %s fetch failed: %s', company, exc)
        return {'status': 'error', 'company': company, 'measure': measure, 'rows': [],
                'error': 'Could not read the sales document flow from SAP.',
                'start': str(start_date), 'end': str(end_date)}
    rows = []
    for r in raw:
        rows.append({
            'party': _normalize_name(r.get('PARTY')) or _normalize_name(r.get('CCODE')) or '—',
            'card_code': _normalize_name(r.get('CCODE')),
            'date': str(r.get('DDATE') or '').strip(),
            'quotation_no': str(r.get('QUOT_NO') or '').strip(),
            'order_no': str(r.get('ORDER_NO') or '').strip(),
            'invoice_no': str(r.get('INV') or '').strip(),
            'qty': round(float(r.get('QTY') or 0), 2),
            'quotation_src': _doc_source(r.get('QUOT_UCODE'), r.get('QUOT_UNAME')),
            'order_src': _doc_source(r.get('ORD_UCODE'), r.get('ORD_UNAME')),
            'invoice_src': _doc_source(r.get('INV_UCODE'), r.get('INV_UNAME')),
            'quotation_open': str(r.get('QUOT_ST') or '').strip().upper() == 'O',
            'order_open': str(r.get('ORD_ST') or '').strip().upper() == 'O',
            'invoice_open': str(r.get('INV_ST') or '').strip().upper() == 'O',
        })
    payload = {'status': 'ok', 'company': company, 'measure': measure, 'rows': rows,
               'start': str(start_date), 'end': str(end_date)}
    if raw:
        _shared_set('salesflow', key, payload, _SALES_FLOW_TTL)
    return payload


# Documents whose open line items can be drilled from the Sales Document Flow report
# (header table, line table). An invoice has no "open quantity" concept, so it is not here.
_SALES_FLOW_DOC_TABLES = {
    'order':     ('ORDR', 'RDR1'),
    'quotation': ('OQUT', 'QUT1'),
}


def get_sales_flow_open_items(doc_type, doc_no, company='oil'):
    """Open line items (LineStatus='O') of one Sales Order / Quotation — the drill-down that opens
    when an OPEN document is clicked on the Sales Document Flow report. Each item carries its still-
    open quantity in the company measure — Litres (oil, OpenQty×SalPackUn) or Boxes (beverages,
    OpenQty÷SalFactor2) — plus raw open pieces. Returns {status, doc_type, doc_no, company, measure,
    party, items:[{code,name,label,open_qty,open_pcs}], total_open, total_pcs}."""
    company = 'beverages' if str(company or '').lower().startswith('bev') else 'oil'
    measure = 'Boxes' if company == 'beverages' else 'Litres'
    dt = str(doc_type or '').strip().lower()
    tables = _SALES_FLOW_DOC_TABLES.get(dt)
    if not tables:
        return {'status': 'error', 'error': 'Unknown document type.', 'items': []}
    try:
        docn = int(str(doc_no or '').strip())
    except (TypeError, ValueError):
        return {'status': 'error', 'error': 'Invalid document number.', 'items': []}
    S = BEVERAGES_SCHEMA if company == 'beverages' else SAP_SCHEMA
    HT, LT = tables
    open_qty = ('L."OpenQty" / NULLIF(I."SalFactor2", 0)' if company == 'beverages'
                else 'L."OpenQty" * COALESCE(I."SalPackUn", 0)')
    sql = f'''
        SELECT COALESCE(TRIM(H."CardName"), '') AS "PARTY",
               COALESCE(TRIM(L."ItemCode"), '') AS "CODE",
               COALESCE(TRIM(I."ItemName"), TRIM(L."Dscription")) AS "NAME",
               L."OpenQty" AS "OPENPCS",
               {open_qty} AS "OPENM"
        FROM "{S}"."{HT}" H
        JOIN "{S}"."{LT}" L ON L."DocEntry" = H."DocEntry"
        LEFT JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode"
        WHERE H."DocNum" = ? AND L."LineStatus" = 'O'
        ORDER BY L."LineNum"
    '''
    try:
        raw = sap_connector.execute_query(sql, (docn,))
    except Exception as exc:
        logger.error('[SALES-FLOW] open-items %s %s fetch failed: %s', dt, docn, exc)
        return {'status': 'error', 'error': 'Could not read the open items from SAP.', 'items': []}
    items, party, total_m, total_pcs = [], '', 0.0, 0.0
    for r in raw:
        party = party or _normalize_name(r.get('PARTY'))
        code = str(r.get('CODE') or '').strip()
        name = _normalize_name(r.get('NAME'))
        m = float(r.get('OPENM') or 0)
        pcs = float(r.get('OPENPCS') or 0)
        total_m += m
        total_pcs += pcs
        items.append({'code': code, 'name': name, 'label': _item_label(code, name),
                      'open_qty': round(m, 2), 'open_pcs': round(pcs, 2)})
    return {'status': 'ok', 'doc_type': dt, 'doc_no': str(doc_no).strip(), 'company': company,
            'measure': measure, 'party': party, 'items': items,
            'total_open': round(total_m, 2), 'total_pcs': round(total_pcs, 2)}


_OIH_LINE_SQL = f'''
    SELECT H."DocNum" AS "DOCNUM", H."DocDate" AS "DOCDATE",
           COALESCE(TRIM(C."U_Main_Group"), '') AS "GRP",
           {_SHIPTO_STATE} AS "ST",
           {_SHIPTO_CITY} AS "CITY",
           COALESCE(TRIM(H."CardCode"), '')     AS "CCODE",
           COALESCE(TRIM(C."CardName"), '')     AS "CUST",
           COALESCE(C."Balance", 0)             AS "BAL",
           COALESCE(TRIM(I."U_Sub_Group"), '')  AS "SUBG",
           COALESCE(TRIM(I."ItemName"), '')     AS "ITEM",
           COALESCE(TRIM(I."ItemCode"), '')     AS "ICODE",
           COALESCE(TRIM(I."U_TYPE"), '')       AS "UTYPE",
           SUM(L."OpenQty" * COALESCE(I."SalPackUn", 0)) AS "OPEN_QTY"
    FROM "{SAP_SCHEMA}"."ORDR" H
    JOIN "{SAP_SCHEMA}"."RDR1" L ON L."DocEntry" = H."DocEntry"
    JOIN "{SAP_SCHEMA}"."OCRD" C ON C."CardCode" = H."CardCode"
    LEFT JOIN "{SAP_SCHEMA}"."OITM" I ON I."ItemCode" = L."ItemCode"
    {_SHIPTO_JOIN.format(S=SAP_SCHEMA)}
    WHERE H."DocStatus" = 'O' AND L."LineStatus" = 'O'
    GROUP BY H."DocNum", H."DocDate",
             COALESCE(TRIM(H."CardCode"), ''), COALESCE(C."Balance", 0),
             COALESCE(TRIM(C."U_Main_Group"), ''), {_SHIPTO_STATE}, {_SHIPTO_CITY},
             COALESCE(TRIM(C."CardName"), ''), COALESCE(TRIM(I."U_Sub_Group"), ''),
             COALESCE(TRIM(I."ItemName"), ''), COALESCE(TRIM(I."ItemCode"), ''),
             COALESCE(TRIM(I."U_TYPE"), '')
'''


# Warehouses whose on-hand stock (in litres) is shown per item in the OIH popup.
OIH_STOCK_WAREHOUSES = ['GP-FG', 'BH-EC', 'BH-PF', 'BH-BT']


def _warehouse_stock_litres(item_codes):
    """{(ItemCode, WhsCode): on-hand litres} for OIH_STOCK_WAREHOUSES — used to show
    each open-order item's stock across the key warehouses. Litres = OnHand * SalPackUn."""
    codes = sorted({str(c).strip() for c in item_codes if str(c or '').strip()})
    if not codes:
        return {}
    whs_ph = ','.join(['?'] * len(OIH_STOCK_WAREHOUSES))
    code_ph = ','.join(['?'] * len(codes))
    sql = f'''
        SELECT W."ItemCode" AS "ICODE", W."WhsCode" AS "WHS",
               W."OnHand" * COALESCE(M."SalPackUn", 0) AS "LIT"
        FROM "{SAP_SCHEMA}"."OITW" W
        JOIN "{SAP_SCHEMA}"."OITM" M ON M."ItemCode" = W."ItemCode"
        WHERE W."WhsCode" IN ({whs_ph}) AND W."ItemCode" IN ({code_ph})
    '''
    try:
        rows = sap_connector.execute_query(sql, tuple(OIH_STOCK_WAREHOUSES) + tuple(codes))
    except Exception as exc:
        logger.error('[CH-DETAIL] warehouse stock fetch failed: %s', exc)
        return {}
    out = {}
    for r in rows or []:
        out[(_normalize_name(r.get('ICODE')), _normalize_name(r.get('WHS')))] = float(r.get('LIT') or 0)
    return out


def _warehouse_stock_lit_pcs(item_codes):
    """{(ItemCode, WhsCode): {'lit': litres, 'pcs': pieces}} for OIH_STOCK_WAREHOUSES.
    Litres = OnHand * SalPackUn (same conversion as OIH); pcs = raw OnHand quantity — so the
    OIH-vs-Stock dashboard can switch its Stock column between litres and pieces."""
    codes = sorted({str(c).strip() for c in item_codes if str(c or '').strip()})
    if not codes:
        return {}
    whs_ph = ','.join(['?'] * len(OIH_STOCK_WAREHOUSES))
    code_ph = ','.join(['?'] * len(codes))
    sql = f'''
        SELECT W."ItemCode" AS "ICODE", W."WhsCode" AS "WHS",
               W."OnHand" * COALESCE(M."SalPackUn", 0) AS "LIT",
               W."OnHand"                              AS "PCS"
        FROM "{SAP_SCHEMA}"."OITW" W
        JOIN "{SAP_SCHEMA}"."OITM" M ON M."ItemCode" = W."ItemCode"
        WHERE W."WhsCode" IN ({whs_ph}) AND W."ItemCode" IN ({code_ph})
    '''
    try:
        rows = sap_connector.execute_query(sql, tuple(OIH_STOCK_WAREHOUSES) + tuple(codes))
    except Exception as exc:
        logger.error('[OIH-KPI] warehouse stock (lit/pcs) fetch failed: %s', exc)
        return {}
    out = {}
    for r in rows or []:
        k = (_normalize_name(r.get('ICODE')), _normalize_name(r.get('WHS')))
        out[k] = {'lit': float(r.get('LIT') or 0), 'pcs': float(r.get('PCS') or 0)}
    return out


def get_channel_oih_documents(channel, filters, seg=''):
    """Open sales-order documents behind an Order-in-Hand cell. Same ORDR/RDR1 source
    as the Order-in-Hand roll-up, kept at document grain and joined to OITM so product
    / item / type drills work (needed for the commodity table). State & city come from
    the order's ship-to address (CRD1) so they match the Done/sales side."""
    members = CHANNEL_MEMBERS.get(channel)  # None -> all groups (commodity / all-channel)
    seg = str(seg or '').strip().upper()
    try:
        rows = sap_connector.execute_query(_OIH_LINE_SQL)
    except Exception as exc:
        logger.error('[CH-DETAIL] OIH document fetch failed: %s', exc)
        return []
    payload = get_territory_dashboard_payload()
    person_map, whitelist = payload['map'], payload['whitelist']
    docs = {}
    for row in rows or []:
        g = _normalize_name(row.get('GRP'))
        if members is not None and g not in members:
            continue
        if seg and _normalize_name(row.get('UTYPE')) != seg:
            continue
        state_name = _delhi_gt_state(row.get('CCODE'), _state_name(row))
        st = _channel_state_label(channel, state_name, whitelist)
        if st is None:
            continue
        customer = _normalize_name(row.get('CUST')) or '—'
        derived = {
            'group': g, 'state': st,
            'person': person_map.get(g + '|' + state_name) or '—',
            'customer': customer,
            'product': _normalize_name(row.get('SUBG')) or '—',
            'item': _item_label(_normalize_name(row.get('ICODE')), _normalize_name(row.get('ITEM'))),
        }
        if not _derived_node_match(derived, filters):
            continue
        num = str(row.get('DOCNUM') or '').strip()
        dkey = num or (_fmt_doc_date(row.get('DOCDATE')) + '|' + customer)
        rec = docs.get(dkey)
        if rec is None:
            rec = docs[dkey] = {'doc_num': num, 'doc_date': _fmt_doc_date(row.get('DOCDATE')),
                                'party': customer, 'state': state_name,
                                'city': _normalize_name(row.get('CITY')), 'litres': 0.0,
                                'balance': float(row.get('BAL') or 0), '_items': {}}
        lit = float(row.get('OPEN_QTY') or 0)
        rec['litres'] += lit
        # Per item (by ItemCode): accumulate open litres; stock is attached below.
        icode = _normalize_name(row.get('ICODE'))
        ikey = icode or derived['item']
        it = rec['_items'].get(ikey)
        if it is None:
            it = rec['_items'][ikey] = {'name': derived['item'], 'code': icode, 'litres': 0.0}
        it['litres'] += lit
    return _finalize_with_stock(docs)


def get_commodity_oih_rows():
    """Open-order litres for COMMODITY items, shaped like the slide-2 sales rows so the
    commodity tree can bucket Order-in-Hand by product / main group / state / customer.
    Litres = OpenQty * OITM.SalPackUn (same conversion as Done). State comes from the
    order's ship-to address (CRD1) so it matches the Done/sales side."""
    try:
        rows = sap_connector.execute_query(_OIH_LINE_SQL)
    except Exception as exc:
        logger.error('[CH-DETAIL] commodity OIH fetch failed: %s', exc)
        return []
    out = []
    for row in rows or []:
        if _normalize_name(row.get('UTYPE')) != 'COMMODITY':
            continue
        out.append({
            'u_type': 'COMMODITY',
            'u_main_group': _normalize_name(row.get('GRP')),
            'u_sub_group': _normalize_name(row.get('SUBG')),
            'state': _delhi_gt_state(row.get('CCODE'), _state_name(row)),
            'card_name': _normalize_name(row.get('CUST')),
            'item_name': _normalize_name(row.get('ITEM')),
            'open_qty': round(float(row.get('OPEN_QTY') or 0), 2),
        })
    return out


# OIH KPI window: item dimensions to group open-order litres by. col = OITM field as
# exposed by REPORT_SALES_ANALYSIS. Adjust OITM_PACKTYPE_COL if the field name differs.
OITM_PACKTYPE_COL = 'U_PACK_TYPE'
# Dimensions the OIH KPI window can drill by (dynamic, multi-level — like the cards).
OIH_BREAKDOWN_DIMS = [
    {'key': 'main_group', 'label': 'Main Group'},
    {'key': 'state', 'label': 'State'},
    {'key': 'sub_group', 'label': 'U_Sub_Group'},
    {'key': 'packtype', 'label': 'PackType'},
    {'key': 'item', 'label': 'Item Name'},
    {'key': 'customer', 'label': 'Customer Name'},
]


def get_oih_dimension_rows():
    """Granular open-order litres by (Main Group, State, U_Sub_Group, PackType, Item,
    Customer), split Premium vs Commodity, for the OIH KPI window. The client nests these
    into any drill order. Litres = OpenQty * OITM.SalPackUn. State comes from the order's
    ship-to address (CRD1), so it matches the Done/sales side."""
    col = OITM_PACKTYPE_COL
    sql = f'''
        SELECT COALESCE(TRIM(C."U_Main_Group"), '—') AS "GRP",
               {_SHIPTO_STATE}                        AS "ST",
               COALESCE(TRIM(I."U_Sub_Group"), '—')  AS "SUBG",
               COALESCE(TRIM(I."{col}"), '—')         AS "PACK",
               COALESCE(TRIM(I."U_SKU"), '—')         AS "SKU",
               COALESCE(TRIM(I."ItemName"), '—')      AS "ITEM",
               COALESCE(TRIM(I."ItemCode"), '')        AS "ICODE",
               COALESCE(TRIM(H."CardCode"), '')        AS "CCODE",
               COALESCE(TRIM(C."CardName"), '—')      AS "CUST",
               COALESCE(TRIM(I."U_TYPE"), '')          AS "UTYPE",
               H."DocNum"                              AS "DOCNUM",
               SUM(L."OpenQty" * COALESCE(I."SalPackUn", 0)) AS "QTY",
               SUM(L."OpenQty")                        AS "PCS"
        FROM "{SAP_SCHEMA}"."ORDR" H
        JOIN "{SAP_SCHEMA}"."RDR1" L ON L."DocEntry" = H."DocEntry"
        JOIN "{SAP_SCHEMA}"."OCRD" C ON C."CardCode" = H."CardCode"
        LEFT JOIN "{SAP_SCHEMA}"."OITM" I ON I."ItemCode" = L."ItemCode"
        {_SHIPTO_JOIN.format(S=SAP_SCHEMA)}
        WHERE H."DocStatus" = 'O' AND L."LineStatus" = 'O'
        GROUP BY COALESCE(TRIM(C."U_Main_Group"), '—'), {_SHIPTO_STATE},
                 COALESCE(TRIM(I."U_Sub_Group"), '—'), COALESCE(TRIM(I."{col}"), '—'),
                 COALESCE(TRIM(I."U_SKU"), '—'),
                 COALESCE(TRIM(I."ItemName"), '—'), COALESCE(TRIM(I."ItemCode"), ''),
                 COALESCE(TRIM(H."CardCode"), ''),
                 COALESCE(TRIM(C."CardName"), '—'), COALESCE(TRIM(I."U_TYPE"), ''), H."DocNum"
    '''
    try:
        rows = sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[OIH-KPI] dimension rows failed: %s', exc)
        return {'rows': [], 'dims': OIH_BREAKDOWN_DIMS, 'item_stock': {}, 'item_stock_pcs': {},
                'warehouses': OIH_STOCK_WAREHOUSES, 'error': str(exc)}
    agg = {}
    name_codes = {}   # item name -> set of ItemCodes (for per-item warehouse stock)
    for r in rows or []:
        ut = _normalize_name(r.get('UTYPE'))
        if ut not in ('PREMIUM', 'COMMODITY'):
            continue
        icode = _normalize_name(r.get('ICODE'))
        item_name = _item_label(icode, _normalize_name(r.get('ITEM')))
        if icode:
            name_codes.setdefault(item_name, set()).add(icode)
        grp = _normalize_name(r.get('GRP')) or '—'
        st = _delhi_gt_state(r.get('CCODE'), _state_name(r)) or '—'
        so_no = str(r.get('DOCNUM') or '').strip() or '—'
        person = person_for_group_state(grp, st) or '—'   # territory owner for this group+state
        key = (grp, st, _normalize_name(r.get('SUBG')) or '—', _normalize_name(r.get('PACK')) or '—',
               item_name, _normalize_name(r.get('CUST')) or '—', so_no, person,
               _normalize_name(r.get('SKU')) or '—')
        cell = agg.setdefault(key, {'premium': 0.0, 'commodity': 0.0,
                                    'premium_pcs': 0.0, 'commodity_pcs': 0.0})
        seg = 'premium' if ut == 'PREMIUM' else 'commodity'
        cell[seg] += float(r.get('QTY') or 0)          # litres = OpenQty * SalPackUn
        cell[seg + '_pcs'] += float(r.get('PCS') or 0)  # raw pieces / quantity
    out = [{'main_group': k[0], 'state': k[1], 'sub_group': k[2], 'packtype': k[3],
            'item': k[4], 'customer': k[5], 'so_no': k[6], 'sales_person': k[7], 'sku': k[8],
            'premium': round(v['premium'], 2), 'commodity': round(v['commodity'], 2),
            'premium_pcs': round(v['premium_pcs'], 2), 'commodity_pcs': round(v['commodity_pcs'], 2)}
           for k, v in agg.items()]
    # On-hand stock per warehouse, keyed by item NAME (each ItemCode counted once), in both
    # litres and pieces, so the window can show GP-FG / BH-EC / BH-PF columns on item rows and
    # switch its Stock/Required columns between the two units.
    all_codes = {c for codes in name_codes.values() for c in codes}
    stock = _warehouse_stock_lit_pcs(all_codes)
    item_stock = {
        name: [round(sum((stock.get((c, w)) or {}).get('lit', 0) for c in codes), 2)
               for w in OIH_STOCK_WAREHOUSES]
        for name, codes in name_codes.items()
    }
    item_stock_pcs = {
        name: [round(sum((stock.get((c, w)) or {}).get('pcs', 0) for c in codes), 2)
               for w in OIH_STOCK_WAREHOUSES]
        for name, codes in name_codes.items()
    }
    return {'rows': out, 'dims': OIH_BREAKDOWN_DIMS, 'item_stock': item_stock,
            'item_stock_pcs': item_stock_pcs,
            'warehouses': OIH_STOCK_WAREHOUSES, 'error': None}


# get_oih_dimension_rows runs two heavy SAP queries (the grouped open-order pull + the
# warehouse stock pull) and takes no arguments, so the result is the same for everyone
# within the window. Cache it like the sales/beverages pulls so the OIH-vs-Stock tab and
# the dashboard OIH window open instantly on repeat loads instead of re-querying HANA.
_OIH_DIM_CACHE = {}        # 'oih_dim' -> (expires_at, result)
_OIH_DIM_CACHE_TTL = 90    # seconds, same as the other realise SAP caches


def get_oih_dimension_rows_cached():
    now = time.time()
    hit = _OIH_DIM_CACHE.get('oih_dim')
    if hit and hit[0] > now:
        return hit[1]
    result = get_oih_dimension_rows()
    if result and result.get('rows') and not result.get('error'):   # cache only successful pulls
        _OIH_DIM_CACHE['oih_dim'] = (now + _OIH_DIM_CACHE_TTL, result)
    return result


# ── OIH vs Stock — Jivo Beverages company ────────────────────────────────────
# Same report as the oil path but against JIVO_BEVERAGES_HANADB. The headline metric is
# BOXES (open-order OpenQty ÷ SalFactor2; stock = net (In−Out) ÷ SalFactor2), the segment
# is the beverages family (U_Sub_Group: DRINKS/WATER/…) and the product card is U_Variety.
# Warehouses are data-driven (beverages FG spreads across many godowns), like Stock Available.
OIH_BEV_DIMS = [
    {'key': 'variety', 'label': 'Variety'},
    {'key': 'family', 'label': 'Family'},
    {'key': 'item', 'label': 'Item Name'},
    {'key': 'customer', 'label': 'Customer'},
    {'key': 'state', 'label': 'State'},
    {'key': 'sales_person', 'label': 'Sales Person'},
    {'key': 'so_no', 'label': 'SO No'},
]


def _bev_empty_oih(error=None):
    return {'rows': [], 'dims': OIH_BEV_DIMS, 'types': [{'key': 'all', 'label': 'All'}],
            'item_stock': {}, 'item_stock_pcs': {}, 'warehouses': [], 'company': 'bev',
            'unit_short': 'Box', 'unit_label': 'Boxes', 'default_dim': 'variety', 'error': error}


def get_oih_dimension_rows_beverages():
    """Granular open-order boxes/pcs by beverages dimensions (family, variety, item, customer,
    state, sales person, SO), plus per-item on-hand stock in boxes and pcs across the beverage
    warehouses. Payload shape mirrors get_oih_dimension_rows so the OIH-vs-Stock UI can render
    either company; 'company'/'unit_*'/'types' tell it to relabel to Boxes and the families."""
    # 1) Open-order lines → boxes (OpenQty ÷ SalFactor2) and raw pieces, by dimension.
    oih_sql = f'''
        SELECT COALESCE(TRIM(T2."U_Sub_Group"), '—') AS "FAMILY",
               COALESCE(TRIM(T2."U_Variety"), '—')   AS "VARIETY",
               COALESCE(TRIM(T2."ItemName"), '—')     AS "ITEM",
               COALESCE(TRIM(T2."ItemCode"), '')       AS "ICODE",
               COALESCE(TRIM(T4."CardName"), '—')     AS "CUST",
               COALESCE(TRIM(T5."SlpName"), '—')      AS "PERSON",
               (SELECT K."Name" FROM {BEVERAGES_SCHEMA}.OCST K
                 WHERE K."Code" = T7."State" AND K."Country" = T7."Country") AS "STATE",
               T0."DocNum" AS "DOCNUM",
               SUM(T1."OpenQty") AS "PCS",
               SUM(T1."OpenQty" / NULLIF(T2."SalFactor2", 0)) AS "BOX"
        FROM {BEVERAGES_SCHEMA}.ORDR T0
        JOIN {BEVERAGES_SCHEMA}.RDR1 T1 ON T0."DocEntry" = T1."DocEntry"
        JOIN {BEVERAGES_SCHEMA}.OITM T2 ON T1."ItemCode" = T2."ItemCode"
        JOIN {BEVERAGES_SCHEMA}.OITB G ON T2."ItmsGrpCod" = G."ItmsGrpCod"
        JOIN {BEVERAGES_SCHEMA}.OCRD T4 ON T0."CardCode" = T4."CardCode"
        LEFT JOIN {BEVERAGES_SCHEMA}.OSLP T5 ON T0."SlpCode" = T5."SlpCode"
        LEFT JOIN {BEVERAGES_SCHEMA}.CRD1 T7
            ON T7."CardCode" = T0."CardCode" AND T7."AdresType" = 'S' AND T7."Address" = T0."ShipToCode"
        WHERE T0."CANCELED" = 'N' AND T4."GroupCode" <> 100 AND T1."TreeType" <> 'I'
          AND T1."LineStatus" = 'O' AND G."ItmsGrpNam" = 'FINISHED' AND T2."U_Unit" = 'BEVERAGES'
        GROUP BY T2."U_Sub_Group", T2."U_Variety", T2."ItemName", T2."ItemCode",
                 T4."CardName", T5."SlpName", T7."State", T7."Country", T0."DocNum"
    '''
    try:
        rows = sap_connector.execute_query(oih_sql)
    except Exception as exc:
        logger.error('[OIH-KPI-BEV] dimension rows failed: %s', exc)
        return _bev_empty_oih(str(exc))

    agg = {}
    families = {}   # family -> total boxes, for ordering the segment control
    for r in rows or []:
        family = _normalize_name(r.get('FAMILY')) or '—'
        variety = _normalize_name(r.get('VARIETY')) or '—'
        item = _normalize_name(r.get('ITEM')) or '—'
        key = (family, variety, item, _normalize_name(r.get('CUST')) or '—',
               _normalize_name(r.get('PERSON')) or '—', _normalize_name(r.get('STATE')) or '—',
               str(r.get('DOCNUM') or '').strip() or '—')
        cell = agg.setdefault(key, {'boxes': 0.0, 'pcs': 0.0})
        box = float(r.get('BOX') or 0)
        cell['boxes'] += box
        cell['pcs'] += float(r.get('PCS') or 0)
        families[family] = families.get(family, 0.0) + box
    out = [{'family': k[0], 'variety': k[1], 'item': k[2], 'customer': k[3],
            'sales_person': k[4], 'state': k[5], 'so_no': k[6],
            'boxes': round(v['boxes'], 2), 'pcs': round(v['pcs'], 2)}
           for k, v in agg.items()]

    # 2) On-hand FG stock → boxes/pcs per (item name, warehouse). Warehouses come from the
    #    data, ordered by total boxes so the busiest godowns are the first columns.
    stock_sql = f'''
        SELECT I."ItemName" AS "ITEM", O."Warehouse" AS "WHS",
               SUM(O."InQty" - O."OutQty") AS "PCS",
               CASE WHEN I."SalFactor2" > 0
                    THEN SUM(O."InQty" - O."OutQty") / I."SalFactor2" ELSE 0 END AS "BOX"
        FROM {BEVERAGES_SCHEMA}.OINM O
        JOIN {BEVERAGES_SCHEMA}.OITM I ON I."ItemCode" = O."ItemCode"
        JOIN {BEVERAGES_SCHEMA}.OITB G ON I."ItmsGrpCod" = G."ItmsGrpCod"
        WHERE G."ItmsGrpNam" = 'FINISHED' AND I."U_Unit" = 'BEVERAGES'
        GROUP BY I."ItemName", I."SalFactor2", O."Warehouse"
        HAVING SUM(O."InQty" - O."OutQty") <> 0
    '''
    try:
        srows = sap_connector.execute_query(stock_sql)
    except Exception as exc:
        logger.error('[OIH-KPI-BEV] stock fetch failed: %s', exc)
        srows = []
    box_by, pcs_by, wh_tot = {}, {}, {}
    for r in srows or []:
        name = _normalize_name(r.get('ITEM')) or '—'
        whs = _normalize_name(r.get('WHS'))
        if not whs:
            continue
        b, p = float(r.get('BOX') or 0), float(r.get('PCS') or 0)
        d = box_by.setdefault(name, {}); d[whs] = d.get(whs, 0.0) + b
        e = pcs_by.setdefault(name, {}); e[whs] = e.get(whs, 0.0) + p
        wh_tot[whs] = wh_tot.get(whs, 0.0) + b
    warehouses = sorted(wh_tot.keys(), key=lambda w: -wh_tot[w])
    names = set(box_by) | set(pcs_by)
    item_stock = {n: [round(box_by.get(n, {}).get(w, 0.0), 2) for w in warehouses] for n in names}
    item_stock_pcs = {n: [round(pcs_by.get(n, {}).get(w, 0.0), 2) for w in warehouses] for n in names}

    # Segment control: All + each family, biggest first (data-driven, like Stock Available).
    fam_order = sorted((f for f in families if f), key=lambda f: -families[f])
    types = [{'key': 'all', 'label': 'All'}] + [{'key': f, 'label': f.title()} for f in fam_order]

    return {'rows': out, 'dims': OIH_BEV_DIMS, 'types': types,
            'item_stock': item_stock, 'item_stock_pcs': item_stock_pcs,
            'warehouses': warehouses, 'company': 'bev',
            'unit_short': 'Box', 'unit_label': 'Boxes', 'default_dim': 'variety', 'error': None}


_OIH_BEV_CACHE = {}        # 'oih_bev' -> (expires_at, result)


def get_oih_dimension_rows_beverages_cached():
    now = time.time()
    hit = _OIH_BEV_CACHE.get('oih_bev')
    if hit and hit[0] > now:
        return hit[1]
    result = get_oih_dimension_rows_beverages()
    if result and result.get('rows') and not result.get('error'):
        _OIH_BEV_CACHE['oih_bev'] = (now + _OIH_DIM_CACHE_TTL, result)
    return result


def get_target_nodes(month, year, segment=None):
    """Raw saved hierarchical targets for a period (group/state/person/ltrs/realise).
    segment PREMIUM/COMMODITY filters to that product segment; blank/None = all.
    Unsegmented targets (segment='') are treated as applying to any segment, so a
    channel target entered without a segment still shows under Premium/Commodity."""
    qs = TargetNode.objects.filter(month=month, year=year)
    seg = _norm_segment(segment)
    if seg:
        qs = qs.filter(segment__in=[seg, ''])
    return [
        {'main_group': n.main_group, 'state': n.state, 'sales_person': n.sales_person,
         'segment': n.segment or '',
         'target_ltrs': float(n.target_ltrs or 0), 'target_realise': float(n.target_realise or 0)}
        for n in qs
    ]


def get_hier_filter_options(master_rows):
    return {
        'main_groups': sorted({row['main_group'] for row in master_rows if row['main_group']}),
        'states': sorted({row['state'] for row in master_rows if row['state']}),
        'sales_people': sorted({row['sales_person'] for row in master_rows if row['sales_person']}),
    }


def get_hier_rows(order_key, month, year, master_rows, filters=None, segment=''):
    dims = _HIER_ORDER_DIMS.get(order_key) or _HIER_ORDER_DIMS['mg_state_sp']
    filters = filters or {}

    # Scope the displayed target value to the selected segment so a Premium target
    # doesn't show up under Commodity (and vice-versa). 'All' shows any saved value.
    node_qs = TargetNode.objects.filter(month=month, year=year)
    seg = _norm_segment(segment)
    if seg:
        node_qs = node_qs.filter(segment=seg)
    saved = {}
    for node in node_qs:
        saved[(node.main_group, node.state, node.sales_person)] = node.target_ltrs

    filtered_rows = []
    for row in master_rows:
        keep = True
        for dim in ('main_group', 'state', 'sales_person'):
            wanted = _normalize_name(filters.get(dim))
            if wanted and row.get(dim) != wanted:
                keep = False
                break
        if keep:
            filtered_rows.append(row)

    rows = []

    def recurse(level, combo, candidate_rows):
        dim = dims[level]
        values = sorted({r.get(dim, '') for r in candidate_rows if r.get(dim, '')})
        for val in values:
            child = dict(combo)
            child[dim] = val
            child_rows = [r for r in candidate_rows if r.get(dim) == val]
            triple = (child.get('main_group', ''), child.get('state', ''), child.get('sales_person', ''))
            rows.append({
                'depth': level,
                'indent': 16 + level * 26,
                'label': val,
                'count': len(child_rows),
                'dim': dim,
                'dim_label': _HIER_DIM_LABELS[dim],
                'key': _hier_key(child),
                'value': _fmt_ltrs(saved.get(triple)),
            })
            if level + 1 < len(dims) and child_rows:
                recurse(level + 1, child, child_rows)

    recurse(0, {'main_group': '', 'state': '', 'sales_person': ''}, filtered_rows)
    return rows


# ───────────────────────── Customer Aging (AR) ─────────────────────────
# Customer-receivables aging computed LIVE from SAP, replicating SAP B1's own
# Customer Receivables Aging report. B1 ages the BP journal lines (JDT1) by
# reversing internal reconciliations (ITR1/OITR) dated after the aging date —
# NOT open invoices — so the result ties to OCRD.Balance to the rupee. We
# translate B1's system-query logic (stored as T-SQL) to HANA SQL, bucket by
# posting date (JDT1.RefDate — matches the SAP report's actual export, which ages
# by document/posting date, not due date), group by FORMAT (OCRD.U_Main_Group) →
# customers, and cache per aging date. The Customer-Aging.xlsx reader below is legacy,
# retained for reference / manual fallback but no longer used by default.
import os
from django.conf import settings

AGING_XLSX_PATH = os.path.join(settings.BASE_DIR, 'Customer-Aging.xlsx')

# Bucket columns in the DATA sheet, in display order, with the palette used by the
# Customer Aging tab (current=green → escalating to 121+=red).
AGING_BUCKETS = [
    # The first three are all "current" (0-30) - a green ramp, light to dark - so the row still
    # reads as one healthy block at a glance while staying selectable one week at a time.
    {'key': 'b0_7',    'label': '0 - 7',    'color': '#22c55e'},
    {'key': 'b8_15',   'label': '8 - 15',   'color': '#16a34a'},
    {'key': 'b16_30',  'label': '16 - 30',  'color': '#15803d'},
    {'key': 'b31_60',  'label': '31 - 60',  'color': '#0d9488'},
    {'key': 'b61_90',  'label': '61 - 90',  'color': '#d97706'},
    {'key': 'b91_120', 'label': '91 - 120', 'color': '#ea580c'},
    {'key': 'b121',    'label': '121+',     'color': '#dc2626'},
]
_BUCKET_KEYS = [b['key'] for b in AGING_BUCKETS]

_AGING_TTL = 90          # seconds, same window as the sales proc cache


def _aging_num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _empty_buckets():
    return {k: 0.0 for k in ['original', 'balance_due'] + _BUCKET_KEYS}


_XL_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_XL_RNS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def _xl_col_index(ref):
    """'C5' / 'AB12' → 0-based column index from the cell reference letters."""
    idx = 0
    for ch in ref:
        if ch.isalpha():
            idx = idx * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return idx - 1


def _read_xlsx_sheet(path, sheet_name):
    """Read one worksheet from an .xlsx into a list of rows (each a list of cell values,
    None for gaps). Stdlib-only (zipfile + ElementTree) so we don't pull in openpyxl —
    the project already hand-rolls xlsx writing in core.simple_xlsx for the same reason."""
    import zipfile
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as z:
        # name → r:id (workbook.xml) → target path (workbook.xml.rels)
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        rid = None
        for s in wb.iter(_XL_NS + 'sheet'):
            if s.get('name') == sheet_name:
                rid = s.get(_XL_RNS + 'id')
                break
        target = None
        if rid:
            rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
            for rel in rels:
                if rel.get('Id') == rid:
                    target = rel.get('Target')
                    break
        sheet_path = 'xl/' + target.lstrip('/') if target else 'xl/worksheets/sheet1.xml'

        # shared string table (string cells store an index into this)
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            sst = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in sst.iter(_XL_NS + 'si'):
                shared.append(''.join(t.text or '' for t in si.iter(_XL_NS + 't')))

        ws = ET.fromstring(z.read(sheet_path))
        rows = []
        for row in ws.iter(_XL_NS + 'row'):
            cells = {}
            width = 0
            for c in row.findall(_XL_NS + 'c'):
                ci = _xl_col_index(c.get('r', 'A'))
                ctype = c.get('t')
                if ctype == 'inlineStr':
                    is_el = c.find(_XL_NS + 'is')
                    val = ''.join(t.text or '' for t in is_el.iter(_XL_NS + 't')) if is_el is not None else None
                else:
                    v = c.find(_XL_NS + 'v')
                    raw = v.text if v is not None else None
                    if raw is None:
                        val = None
                    elif ctype == 's':
                        try:
                            val = shared[int(raw)]
                        except (ValueError, IndexError):
                            val = raw
                    elif ctype in ('str', 'e'):
                        val = raw
                    else:
                        try:
                            val = float(raw)
                        except ValueError:
                            val = raw
                cells[ci] = val
                width = max(width, ci + 1)
            rows.append([cells.get(i) for i in range(width)])
        return rows


def _load_aging_rows():
    """UNUSED - the spreadsheet fallback. Nothing calls this; every company now reads live
    SAP through _load_aging_rows_sap(). Left in place only as a record of the old sheet
    layout. NOTE: it still emits the pre-split b0_30 key, so if you ever revive it you must
    first split that column into b0_7 / b8_15 / b16_30 to match AGING_BUCKETS.

    Parse the DATA sheet into one dict per customer. Header is row 2, totals row 1,
    data from row 3 down (cols: code, name, FORMAT, original, balance, 5 buckets)."""
    sheet = _read_xlsx_sheet(AGING_XLSX_PATH, 'DATA')
    out = []
    for i, row in enumerate(sheet):
        if i < 2:                       # skip the totals row + header row
            continue
        code = (row[0] or '') if len(row) > 0 else ''
        name = (row[1] or '') if len(row) > 1 else ''
        fmt = (str(row[2]).strip() if len(row) > 2 and row[2] else '') or 'Unclassified'
        if not str(code).strip() and not str(name).strip():
            continue
        out.append({
            'code': str(code).strip(),
            'name': str(name).strip() or str(code).strip(),
            'format': fmt,
            'original': _aging_num(row[3] if len(row) > 3 else 0),
            'balance_due': _aging_num(row[4] if len(row) > 4 else 0),
            'b0_30':   _aging_num(row[5] if len(row) > 5 else 0),
            'b31_60':  _aging_num(row[6] if len(row) > 6 else 0),
            'b61_90':  _aging_num(row[7] if len(row) > 7 else 0),
            'b91_120': _aging_num(row[8] if len(row) > 8 else 0),
            'b121':    _aging_num(row[9] if len(row) > 9 else 0),
        })
    return out


# Live SAP source ─────────────────────────────────────────────────────────
def _aging_date_literal(aging_date):
    """A validated HANA TO_DATE() literal — aging_date is an internal date object, so
    formatting it (never user text) into the SQL is injection-safe."""
    return "TO_DATE('%s')" % aging_date.strftime('%Y-%m-%d')


def _load_aging_rows_sap(aging_date, schema=None, apply_oil_filters=True, with_gstin=False,
                         group_by='format'):
    """Customer receivables aging as of aging_date via SAP B1's reconciliation logic
    (JDT1 / ITR1 / OITR), translated from B1's own system query to HANA SQL. Returns one
    dict per customer in the same shape as the workbook loader, with Balance Due (ties to
    OCRD.Balance), Original Amount = Σ original posted (Debit − Credit) of the open lines,
    and the five posting-date (RefDate) buckets — matching SAP's report, which ages by
    document/posting date. Parts 1/2 reverse reconciliations dated after the aging
    date to reconstruct the historical open balance; part 3 is the never-reconciled-yet
    open lines.

    `schema` selects the company DB (default oil = SAP_SCHEMA; pass MART_SCHEMA for Jivo Mart).
    `apply_oil_filters` drops the oil-only internal/non-receivable rows (JIVO WELLNESS branches,
    FUTURE RETAIL, PURCHASE OIL/EXPORT/TRANSPORT formats) — off for Mart, whose authoritative B1
    aging includes those parties. Groups by OCRD.U_Main_Group ('format'); on a schema that lacks
    that UDF the query is retried with a blank format so the report still works.
    `with_gstin` (Mart) also selects each party's CRD1 GSTIN and tags the row 'segment' =
    'B2B' when a GSTIN exists else 'B2C', so the client can split each format into B2B/B2C."""
    ag, S = _aging_date_literal(aging_date), (schema or SAP_SCHEMA)

    # Per-party GSTIN (Mart B2B/B2C split): any CRD1 address carrying a GST reg no marks the
    # party B2B. The subquery may correlate ONLY on C."CardCode" (which is in the GROUP BY) —
    # referencing a non-grouped OCRD column (e.g. BillToDef) makes HANA reject the whole grouped
    # SELECT ("must be in group by clause"), which previously tripped the no-GSTIN fallback.
    gst_sel = (f''',
           (SELECT MAX(B2."GSTRegnNo") FROM "{S}"."CRD1" B2 WHERE B2."CardCode"=C."CardCode"
              AND TRIM(COALESCE(B2."GSTRegnNo",''))<>'') AS "gstin"''' if with_gstin else '')

    # Grouping dimension: 'format' (OCRD.U_Main_Group, default — oil/mart) or 'salesperson'
    # (OCRD.SlpCode → OSLP.SlpName). Beverages ages by the customer's sales person so the tab
    # keeps its Sales Person → Customer pivot.
    if group_by == 'salesperson':
        sp_join = f'LEFT JOIN "{S}"."OSLP" SLP ON SLP."SlpCode"=C."SlpCode"'
        default_fmt = 'SLP."SlpName"'
    else:
        sp_join = ''
        default_fmt = 'C."U_Main_Group"'

    def _sql(fmt_sel, fmt_grp):
        return f'''WITH aged AS (
      SELECT T0."ShortName" AS card, MAX(T0."RefDate") AS bdate,
             -MAX(T0."BalDueCred")-SUM(T1."ReconSum") AS bal, -MAX(T0."Credit") AS orig
      FROM "{S}"."JDT1" T0
        JOIN "{S}"."ITR1" T1 ON T1."TransId"=T0."TransId" AND T1."TransRowId"=T0."Line_ID"
        JOIN "{S}"."OITR" T2 ON T2."ReconNum"=T1."ReconNum"
        JOIN "{S}"."OCRD" T4 ON T4."CardCode"=T0."ShortName"
      WHERE T0."RefDate"<={ag} AND T4."CardType"='C' AND T2."ReconDate">{ag} AND T1."IsCredit"='C'
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
      HAVING MAX(T0."BalFcCred")<>-SUM(T1."ReconSumFC") OR MAX(T0."BalDueCred")<>-SUM(T1."ReconSum")
      UNION ALL
      SELECT T0."ShortName", MAX(T0."RefDate"),
             MAX(T0."BalDueDeb")+SUM(T1."ReconSum"), MAX(T0."Debit")
      FROM "{S}"."JDT1" T0
        JOIN "{S}"."ITR1" T1 ON T1."TransId"=T0."TransId" AND T1."TransRowId"=T0."Line_ID"
        JOIN "{S}"."OITR" T2 ON T2."ReconNum"=T1."ReconNum"
        JOIN "{S}"."OCRD" T4 ON T4."CardCode"=T0."ShortName"
      WHERE T0."RefDate"<={ag} AND T4."CardType"='C' AND T2."ReconDate">{ag} AND T1."IsCredit"='D'
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
      HAVING MAX(T0."BalFcDeb")<>-SUM(T1."ReconSumFC") OR MAX(T0."BalDueDeb")<>-SUM(T1."ReconSum")
      UNION ALL
      SELECT T0."ShortName", MAX(T0."RefDate"),
             MAX(T0."BalDueDeb")-MAX(T0."BalDueCred"), MAX(T0."Debit")-MAX(T0."Credit")
      FROM "{S}"."JDT1" T0
        JOIN "{S}"."OCRD" T2 ON T2."CardCode"=T0."ShortName"
      WHERE T0."RefDate"<={ag} AND T2."CardType"='C'
        AND (T0."BalDueCred"<>T0."BalDueDeb" OR T0."BalFcCred"<>T0."BalFcDeb")
        AND NOT EXISTS (SELECT 1 FROM "{S}"."ITR1" U0 JOIN "{S}"."OITR" U1 ON U1."ReconNum"=U0."ReconNum"
          WHERE U0."TransId"=T0."TransId" AND U0."TransRowId"=T0."Line_ID" AND U1."ReconDate">{ag})
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
    )
    SELECT C."CardCode" AS "code", C."CardName" AS "name", {fmt_sel} AS "format",
           SUM(a.orig) AS "original", SUM(a.bal) AS "balance_due",
           SUM(CASE WHEN a.bdate IS NULL OR DAYS_BETWEEN(a.bdate,{ag})<=7 THEN a.bal ELSE 0 END) AS "b0_7",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 8 AND 15 THEN a.bal ELSE 0 END) AS "b8_15",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 16 AND 30 THEN a.bal ELSE 0 END) AS "b16_30",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 31 AND 60 THEN a.bal ELSE 0 END) AS "b31_60",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 61 AND 90 THEN a.bal ELSE 0 END) AS "b61_90",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 91 AND 120 THEN a.bal ELSE 0 END) AS "b91_120",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag})>120 THEN a.bal ELSE 0 END) AS "b121"{gst_sel}
    FROM aged a JOIN "{S}"."OCRD" C ON C."CardCode"=a.card
      {sp_join}
    GROUP BY C."CardCode", C."CardName"{fmt_grp}
    HAVING SUM(a.bal)<>0
    ORDER BY SUM(a.bal) DESC'''

    def _run():
        try:
            return sap_connector.execute_query(_sql(default_fmt, ', ' + default_fmt))
        except Exception:
            return sap_connector.execute_query(_sql("''", ''))   # schema without the U_Main_Group/OSLP dim

    try:
        raw = _run()
    except Exception:
        if not with_gstin:
            raise
        gst_sel = ''                       # GST subquery unsupported here — retry without the B2B/B2C tag
        raw = _run()
    out = []
    for r in raw:
        code = str(r.get('code') or '').strip()
        name = str(r.get('name') or '').strip() or code
        fmt = (str(r.get('format')).strip() if r.get('format') else '') or 'Unclassified'
        balance_due = _aging_num(r.get('balance_due'))
        # Always drop a customer whose Balance Due nets to exactly 0.00 (tiny but real balances
        # ≥ ₹0.01 still show). For OIL, also hide internal / non-receivable rows: JIVO WELLNESS
        # inter-company branches and FUTURE RETAIL LTD (Modern Trade) by name, and the PURCHASE
        # OIL / EXPORT / TRANSPORT formats. Mart keeps every party (its B1 aging includes them).
        if balance_due == 0:
            continue
        if apply_oil_filters:
            _name_u, _fmt_u = name.upper(), fmt.upper()
            if ('JIVO WELLNESS' in _name_u or 'FUTURE RETAIL' in _name_u
                    or 'PURCHASE OIL' in _fmt_u or 'EXPORT' in _fmt_u or 'TRANSPORT' in _fmt_u):
                continue
        row = {
            'code': code,
            'name': name,
            'format': fmt,
            'original':    _aging_num(r.get('original')),
            'balance_due': balance_due,
            'b0_7':    _aging_num(r.get('b0_7')),
            'b8_15':   _aging_num(r.get('b8_15')),
            'b16_30':  _aging_num(r.get('b16_30')),
            'b31_60':  _aging_num(r.get('b31_60')),
            'b61_90':  _aging_num(r.get('b61_90')),
            'b91_120': _aging_num(r.get('b91_120')),
            'b121':    _aging_num(r.get('b121')),
        }
        if with_gstin:
            gstin = str(r.get('gstin') or '').strip()
            row['gstin'] = gstin
            row['segment'] = 'B2B' if gstin else 'B2C'   # a GSTIN on file ⇒ registered (B2B)
        out.append(row)
    return out


def _build_aging_payload(rows):
    groups = {}
    total = _empty_buckets()
    for r in rows:
        g = groups.get(r['format'])
        if g is None:
            g = groups[r['format']] = {'format': r['format'], 'customers': [], **_empty_buckets()}
        g['customers'].append(r)
        for k in ['original', 'balance_due'] + _BUCKET_KEYS:
            g[k] = round(g[k] + r[k], 2)
            total[k] = round(total[k] + r[k], 2)

    group_list = sorted(groups.values(), key=lambda g: g['balance_due'], reverse=True)
    for g in group_list:
        g['customers'].sort(key=lambda c: c['balance_due'], reverse=True)
        g['count'] = len(g['customers'])

    # Top single customer exposure across the book (largest outstanding balance).
    top_customer = max(rows, key=lambda r: r['balance_due']) if rows else None
    overdue_90 = round(total['b91_120'] + total['b121'], 2)
    # "Current" is still 0-30 days; it is just assembled from the three sub-buckets now.
    current = round(total['b0_7'] + total['b8_15'] + total['b16_30'], 2)
    bal = total['balance_due'] or 1.0

    kpis = {
        'total_outstanding': total['balance_due'],
        'current': current,
        'current_pct': round(current / bal * 100, 1),
        'overdue_90': overdue_90,
        'overdue_90_pct': round(overdue_90 / bal * 100, 1),
        'customer_count': len(rows),
        'format_count': len(group_list),
        'top_customer_name': top_customer['name'] if top_customer else '—',
        'top_customer_value': top_customer['balance_due'] if top_customer else 0,
        'top_customer_pct': round((top_customer['balance_due'] / bal * 100), 1) if top_customer else 0,
    }

    return {
        'buckets': AGING_BUCKETS,
        'groups': group_list,
        'total': total,
        'kpis': kpis,
    }


def get_customer_aging(aging_date=None):
    """Customer-receivables aging pivot as of aging_date (a date; default today),
    computed live from SAP. Cached per aging date for _AGING_TTL seconds (same window as
    the sales proc). On SAP failure returns an error payload — never stale numbers."""
    if aging_date is None:
        aging_date = date.today()
    key = aging_date.isoformat()
    now = time.time()
    hit = _shared_get('oilaging', key)
    if hit is not None:
        return hit

    try:
        payload = _build_aging_payload(_load_aging_rows_sap(aging_date))
    except Exception as e:
        logger.exception('[aging] failed to build customer aging payload')
        return {'buckets': AGING_BUCKETS, 'groups': [], 'total': _empty_buckets(),
                'kpis': {}, 'aging_date': key, 'error': str(e)}

    payload['aging_date'] = key
    _shared_set('oilaging', key, payload, _AGING_TTL)
    return payload


# ── Customer Aging — Jivo Beverages company ──────────────────────────────────
# The Beverages toggle. Unlike oil (B1 reconciliation pivot), beverages ages the open A/R
# invoices (OINV, DocStatus 'O') directly: InDaysDifference = days from DocDate to the aging
# date, bucketed the same way. We return the RAW per-invoice rows; the client pivots them by
# Sales Person → Customer, offers a per-day multi-select, and an Excel-like raw drill.


def _bev_cell(v):
    """A raw SAP cell → clean string for the JSON payload (dates → ISO; strips whitespace)."""
    if v is None:
        return ''
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, date):
        return v.strftime('%Y-%m-%d')
    return str(v).strip()


def _ar_remarks_by_prefix(card_codes, prefix):
    """{(card_code, doc): value} of manual per-invoice entries stored in AgingRemark with
    row_key='<prefix><DocNum>'. Distinct prefixes keep each A/R company's remarks (and the oil
    detail page's 'TransId:Line_ID' remarks) from colliding. All local DB — cheap, cache-free."""
    codes = {c for c in (card_codes or []) if c}
    if not codes:
        return {}
    out = {}
    for a in AgingRemark.objects.filter(card_code__in=codes, row_key__startswith=prefix):
        out[(a.card_code, a.row_key[len(prefix):])] = a.remark
    return out


def get_bev_aging_remarks(card_codes):
    """{(card_code, doc): remark} of manual remarks on the Beverages raw-invoice drill
    (row_key='BEVDOC:<DocNum>')."""
    return _ar_remarks_by_prefix(card_codes, 'BEVDOC:')


def get_bev_actual_sp(card_codes):
    """{(card_code, doc): actual_sales_person} for the Beverages raw drill (row_key='BEVSP:<DocNum>')."""
    return _ar_remarks_by_prefix(card_codes, 'BEVSP:')


def _ar_attach_remarks(payload, doc_prefix, sp_prefix):
    """Merge current manual remarks + the user-entered Actual Sales Person onto a (possibly
    cached) A/R aging payload, so edits show up immediately regardless of the SAP cache. The
    prefixes select the company's own remark namespace (Beverages vs Mart)."""
    rows = (payload or {}).get('rows') or []
    if rows:
        codes = [r.get('code') for r in rows]
        rem = _ar_remarks_by_prefix(codes, doc_prefix)
        asp = _ar_remarks_by_prefix(codes, sp_prefix)
        for r in rows:
            r['remark'] = rem.get((r.get('code'), r.get('doc')), '')
            r['actual_sp'] = asp.get((r.get('code'), r.get('doc')), '')
    return payload


def _bev_attach_remarks(payload):
    """Beverages shim over _ar_attach_remarks (kept for any external callers)."""
    return _ar_attach_remarks(payload, 'BEVDOC:', 'BEVSP:')


# --- Customer Aging: sales-person name canonicalization ----------------------
# The aging "sales person" dropdown (Beverages main + Oil RAW DATA, both via
# _customer_aging_ar) is built from raw OSLP.SlpName strings that were rendered
# verbatim — so it showed formatting noise (stray double spaces, odd casing)
# and same-person duplicates. _clean_salesperson() runs on every name before
# the client pivots by sales person.
#
#   _SALESPERSON_MERGE  – WITHIN one company these variants are the SAME person,
#                         so their invoices are combined into one row. Only
#                         confirmed pairs belong here; suffix tags (FACTORY / VG
#                         / HO / SIR / ACC / HONEY / ARY / CHADDA) usually mark
#                         DIFFERENT people who share a first name and are kept
#                         separate on purpose.
#   _SALESPERSON_CANON  – CROSS-company relabel: one person is spelled
#                         differently per company (e.g. ZIYAUL SIR / ZIAUL
#                         HAQUE); map each variant to a single canonical name so
#                         the Oil/Beverages toggle reads consistently. This only
#                         relabels a name — each company's aging is shown on its
#                         own, so it never merges another company's receivables.
#
# Keys are matched AFTER cleaning (trim + single-spaced + UPPER). Add pairs here
# as duplicates surface. (See also _BEV_SALESPERSON_ALIAS, the equivalent map
# for the Beverages *sales* report.)
_SALES_PLACEHOLDER = '-No Sales Employee / Buyer-'

_SALESPERSON_MERGE = {
    'GOLDY VG': 'GOLDY',            # Beverages: GOLDY VG is the deactivated dup of GOLDY
}

_SALESPERSON_CANON = {
    # Cross-company canonical names — populated once the groups are confirmed.
}


def _clean_salesperson(name):
    """Trim, collapse internal whitespace and upper-case a raw SlpName, then
    fold same-person variants to one name. The '-No Sales Employee / Buyer-'
    placeholder keeps its readable casing."""
    s = ' '.join(str(name or '').split())
    if not s:
        return ''
    if s.upper() == _SALES_PLACEHOLDER.upper():
        return _SALES_PLACEHOLDER
    s = s.upper()
    s = _SALESPERSON_MERGE.get(s, s)
    return _SALESPERSON_CANON.get(s, s)


def _customer_aging_ar(aging_date, schema, company, doc_prefix, sp_prefix, ns):
    """Open A/R invoice aging for one company schema (Beverages / Mart). Returns raw invoice
    rows (Sales Person, Customer, Days, Balance Due, Outstanding, dispatch/bilty fields, …),
    each carrying its manual `remark`. The client pivots by Sales Person → Customer with the
    shared aging buckets. Cached per (company, aging date) for _AGING_TTL seconds (remarks
    merged fresh). The dispatch/bilty UDF columns exist on the Beverages OINV but may be absent
    on other schemas — the query is retried without them so the report still works."""
    if aging_date is None:
        aging_date = date.today()
    key = aging_date.isoformat()
    now = time.time()
    hit = _shared_get(ns, key)
    if hit is not None:
        return _ar_attach_remarks(hit, doc_prefix, sp_prefix)

    ag, B = _aging_date_literal(aging_date), schema
    # Optional dispatch/bilty custom fields — present on Beverages, retried-without elsewhere.
    udf = ('''T0."U_Dipatch_Date" AS "dispatch", T0."U_BiltyNumber" AS "bilty",
               T0."U_BiltyDate" AS "biltydate", T0."U_TransporterName" AS "transporter",
               T0."U_VechileNom" AS "vehicle", T0."U_DriverName" AS "driver",
               T0."U_Mob_No" AS "mobile",''')

    def build(extras):
        return f'''
        SELECT COALESCE(T6."SlpName", '-No Sales Employee / Buyer-') AS "sp",
               T0."CardCode" AS "code", T0."CardName" AS "name",
               T0."DocNum" AS "doc", TO_VARCHAR(T0."DocDate",'YYYY-MM-DD') AS "date",
               DAYS_BETWEEN(T0."DocDate", {ag}) AS "days",
               TO_VARCHAR(T5."LastTransDate",'YYYY-MM-DD') AS "ltd",
               DAYS_BETWEEN(T5."LastTransDate", {ag}) AS "tdd",
               {extras}T0."DocStatus" AS "status",
               T0."DocTotal" AS "total", (T0."DocTotal" - T0."PaidToDate") AS "bal",
               T3."Balance" AS "outstanding"
        FROM {B}.OINV T0
        INNER JOIN {B}.OCRD T3 ON T0."CardCode" = T3."CardCode"
        INNER JOIN {B}.OSLP T4 ON T3."SlpCode" = T4."SlpCode"
        LEFT  JOIN {B}.OSLP T6 ON T0."SlpCode" = T6."SlpCode"
        LEFT JOIN (SELECT T1."ShortName" AS "CardCode", MAX(T1."RefDate") AS "LastTransDate"
                   FROM {B}.JDT1 T1 GROUP BY T1."ShortName") T5 ON T0."CardCode" = T5."CardCode"
        WHERE T0."DocType" = 'I' AND T0."DocStatus" = 'O' AND T0."CANCELED" = 'N'
          AND T0."CardCode" NOT IN ('CUSTA000001', 'CUSTA000002', 'CUSTA000003')
          AND UPPER(TRIM(T0."CardName")) NOT IN ('BLESSING ADVERTISING PVT LTD', 'CASH SALE DL')
          AND T0."DocDate" <= {ag}
        ORDER BY "days" DESC
    '''
    try:
        raw = sap_connector.execute_query(build(udf))
    except Exception:
        try:                                   # schema without the dispatch/bilty UDFs
            raw = sap_connector.execute_query(build(''))
        except Exception as e:
            logger.exception('[aging-ar] %s aging query failed', company)
            return {'company': company, 'rows': [], 'aging_date': key, 'error': str(e)}

    rows = []
    for r in raw or []:
        tdd = r.get('tdd')
        rows.append({
            # The sales employee written ON THE INVOICE (OINV.SlpCode), i.e. who actually
            # raised it - not the person the customer happens to be assigned to today
            # (OCRD.SlpCode). Those differ whenever a customer changes hands: the old
            # invoices used to follow the new owner, which hid who really made the sale.
            # SAP leaves this empty on many invoices; those keep SAP's own
            # '-No Sales Employee / Buyer-' label rather than being guessed at.
            'sp': _clean_salesperson(r.get('sp')) or '—',
            'code': _bev_cell(r.get('code')),
            'name': _bev_cell(r.get('name')) or _bev_cell(r.get('code')),
            'doc': _bev_cell(r.get('doc')),
            'date': _bev_cell(r.get('date')),
            'days': int(r.get('days') or 0),
            'ltd': _bev_cell(r.get('ltd')),
            'tdd': int(tdd) if tdd is not None else None,
            'dispatch': _bev_cell(r.get('dispatch')),
            'bilty': _bev_cell(r.get('bilty')),
            'biltydate': _bev_cell(r.get('biltydate')),
            'transporter': _bev_cell(r.get('transporter')),
            'vehicle': _bev_cell(r.get('vehicle')),
            'driver': _bev_cell(r.get('driver')),
            'mobile': _bev_cell(r.get('mobile')),
            'status': _bev_cell(r.get('status')),
            'total': _aging_num(r.get('total')),
            'bal': _aging_num(r.get('bal')),
            'outstanding': _aging_num(r.get('outstanding')),
        })
    payload = {'company': company, 'rows': rows, 'aging_date': key, 'error': None}
    _shared_set(ns, key, payload, _AGING_TTL)
    return _ar_attach_remarks(payload, doc_prefix, sp_prefix)


def get_customer_aging_beverages(aging_date=None):
    """Customer-receivables aging for Jivo Beverages (JIVO_BEVERAGES_HANADB) via SAP B1's
    reconciliation engine — the SAME engine as oil/mart (nets on-account payments, ties to
    OCRD.Balance), but grouped by the customer's SALES PERSON (OSLP) instead of format, so the
    tab keeps its Sales Person → Customer pivot. Balance Due drills to the per-document open
    items via the shared aging-detail page (company='bev'). Cached per aging date."""
    if aging_date is None:
        aging_date = date.today()
    key = aging_date.isoformat()
    now = time.time()
    hit = _shared_get('bevaging', key)
    if hit is not None:
        return hit
    try:
        rows = _load_aging_rows_sap(aging_date, schema=BEVERAGES_SCHEMA,
                                    apply_oil_filters=False, group_by='salesperson')
        # Same internal / non-receivable exclusions the old open-invoice Beverages view applied
        # (inter-company CUSTA000001-3, BLESSING ADVERTISING, CASH SALE DL) so the totals match.
        _excl_codes = {'CUSTA000001', 'CUSTA000002', 'CUSTA000003'}
        _excl_names = {'BLESSING ADVERTISING PVT LTD', 'CASH SALE DL'}
        rows = [r for r in rows
                if r.get('code') not in _excl_codes
                and str(r.get('name') or '').strip().upper() not in _excl_names]
        for r in rows:                                    # canonical sales-person label as the group
            r['format'] = _clean_salesperson(r.get('format')) or '—'
        payload = _build_aging_payload(rows)
    except Exception as e:
        logger.exception('[aging-bev] failed to build beverages reconciliation aging')
        return {'company': 'bev', 'buckets': AGING_BUCKETS, 'groups': [], 'total': _empty_buckets(),
                'kpis': {}, 'aging_date': key, 'error': str(e)}
    payload['company'] = 'bev'
    payload['aging_date'] = key
    _shared_set('bevaging', key, payload, _AGING_TTL)
    return payload


# Which SAP company database each Customer Aging tab reads.
_AR_ITEM_SCHEMA = {'oil': SAP_SCHEMA, 'bev': BEVERAGES_SCHEMA, 'mart': MART_SCHEMA}

# HANA has a ceiling on how many values fit in one IN (...) list, and a huge list is slow
# anyway. Ask for the invoices in batches of this size and stitch the answers together.
_ITEM_DOC_CHUNK = 500


def get_aging_invoice_items(company, doc_nums):
    """Line items of the given OPEN invoices, for the Customer Aging drill popup.

    Give it document numbers (the ones already on screen) and it returns one row per
    invoice + item: which invoice, which item, how much and how many. The popup groups
    them by item for its first level, then filters back down to invoices for its second.

    Why it re-checks DocStatus='O' instead of trusting the numbers passed in: the caller
    is a browser, and a stale tab could ask for invoices that have since been paid. The
    WHERE clause is the same one the RAW DATA list itself uses, so the popup can never
    show a document the report would not.
    """
    schema = _AR_ITEM_SCHEMA.get(company) or SAP_SCHEMA

    # Only digits survive - these arrive from the browser and go into a SQL IN list.
    docs, seen = [], set()
    for d in (doc_nums or []):
        d = str(d or '').strip()
        if d.isdigit() and d not in seen:
            seen.add(d)
            docs.append(int(d))
    if not docs:
        return []

    out = []
    for i in range(0, len(docs), _ITEM_DOC_CHUNK):
        batch = docs[i:i + _ITEM_DOC_CHUNK]
        marks = ','.join(['?'] * len(batch))
        sql = f'''
        SELECT T0."DocNum"      AS "doc",
               T1."ItemCode"    AS "item_code",
               COALESCE(T2."ItemName", T1."Dscription") AS "item_name",
               SUM(T1."Quantity")  AS "qty",
               SUM(T1."LineTotal") AS "amount"
        FROM {schema}.OINV T0
        INNER JOIN {schema}.INV1 T1 ON T0."DocEntry" = T1."DocEntry"
        LEFT  JOIN {schema}.OITM T2 ON T1."ItemCode" = T2."ItemCode"
        WHERE T0."DocType" = 'I' AND T0."DocStatus" = 'O' AND T0."CANCELED" = 'N'
          AND T0."DocNum" IN ({marks})
        GROUP BY T0."DocNum", T1."ItemCode", T2."ItemName", T1."Dscription"
        ORDER BY T0."DocNum"
        '''
        try:
            rows = sap_connector.execute_query(sql, tuple(batch))
        except Exception as exc:
            logger.exception('[aging-items] %s line-item query failed', company)
            raise RuntimeError(str(exc))
        for r in rows or []:
            name = _bev_cell(r.get('item_name')) or _bev_cell(r.get('item_code')) or '\u2014'
            out.append({
                'doc': _bev_cell(r.get('doc')),
                'item_code': _bev_cell(r.get('item_code')),
                'item_name': name,
                'qty': _aging_num(r.get('qty')),
                'amount': _aging_num(r.get('amount')),
            })
    return out


def get_customer_aging_oil_ar(aging_date=None):
    """Open A/R invoice RAW DATA for Jivo Oil (JIVO_OIL_HANADB) — the same open-OINV list the
    Beverages RAW DATA workspace uses, but for the oil company. Powers the oil RAW DATA drill;
    it is invoice-grained (open OINV) and is a separate view from the oil aging pivot (which is
    B1 journal reconciliation), so the two need not tie exactly. Its own OILDOC:/OILSP: remark
    namespace keeps oil raw-invoice remarks apart from the per-document aging-detail remarks."""
    return _customer_aging_ar(aging_date, SAP_SCHEMA, 'oil', 'OILDOC:', 'OILSP:', 'oilar')


def get_customer_aging_beverages_ar(aging_date=None):
    """Open A/R invoice RAW DATA for Jivo Beverages (JIVO_BEVERAGES_HANADB) — the per-invoice
    open-OINV list (dispatch/bilty, Actual Sales Person + Remarks) that backs the Beverages RAW
    DATA workspace. Invoice-grained; a separate view from the Beverages aging pivot (which is B1
    reconciliation), so the two need not tie. Its BEVDOC:/BEVSP: remark namespace is unchanged, so
    remarks saved before the aging pivot moved to reconciliation are still shown here."""
    return _customer_aging_ar(aging_date, BEVERAGES_SCHEMA, 'bev', 'BEVDOC:', 'BEVSP:', 'bevar')


def get_customer_aging_mart(aging_date=None):
    """Customer-receivables aging for Jivo Mart (JIVO_MART_HANADB) — the SAME B1 reconciliation
    engine and payload shape as the oil `get_customer_aging` (so the client renders it with the
    identical Format→customers pivot). Ties to SAP's Customer Receivables Aging total; unlike oil
    it keeps ALL parties (no inter-company/format exclusions). Cached per aging date."""
    if aging_date is None:
        aging_date = date.today()
    key = aging_date.isoformat()
    now = time.time()
    hit = _shared_get('martaging', key)
    if hit is not None:
        return hit

    try:
        payload = _build_aging_payload(
            _load_aging_rows_sap(aging_date, schema=MART_SCHEMA, apply_oil_filters=False, with_gstin=True))
    except Exception as e:
        logger.exception('[aging-mart] failed to build mart customer aging payload')
        return {'company': 'mart', 'buckets': AGING_BUCKETS, 'groups': [], 'total': _empty_buckets(),
                'kpis': {}, 'aging_date': key, 'error': str(e)}

    payload['company'] = 'mart'
    payload['aging_date'] = key
    _shared_set('martaging', key, payload, _AGING_TTL)
    return payload


# ── Customer Aging — per-document DETAIL (drill from a Balance Due) ──────────
# Same reconciliation engine as the aging pivot, but at journal-line (document) grain for
# ONE customer, carrying the document fields (No / Type / dates / branch) so we can show the
# open items behind a customer's balance, with an editable Remarks column and a by-Remarks
# pivot. Best-effort SAP field mapping (Doc No = JDT1.BaseRef, Type from TransType, Branch
# from JDT1.BPLId → OBPL); the branch join is retried-without on any error.
_AGING_TYPE_MAP = {13: 'IN', 14: 'CN', 24: 'RC', 30: 'JE', 15: 'DN', 19: 'DN', 18: 'PU',
                   20: 'GR', 46: 'PS', 16: 'CN'}


def _aging_fmt_date(v):
    """A SAP date value → 'YYYY-MM-DD' (best effort; '' when missing)."""
    if v is None:
        return ''
    try:
        return v.strftime('%Y-%m-%d')
    except Exception:
        s = str(v).strip()
        return s[:10] if s else ''


def _aging_to_date(v):
    """A SAP date value (date/datetime/'YYYY-MM-DD…' string) → a datetime.date, or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()[:10]
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _aging_age_days(bdate, aging_date):
    """Whole days from a document's posting date up to the aging date (>=0), or None if the
    posting date is unknown. Drives the NOT DUE / OVERDUE grace classification on the detail
    page (a document is NOT DUE while this is below the customer's configured grace days)."""
    d = _aging_to_date(bdate)
    if d is None:
        return None
    return (aging_date - d).days


def _aging_detail_sql(where_card, ag, S, with_branch):
    """`where_card` is a SQL predicate on T0."ShortName" — e.g. `T0."ShortName"='X'` for one
    customer or `T0."ShortName" IN (...)` for the bulk export. The `card` column is emitted so
    bulk callers can group rows back per customer (single-customer callers ignore it)."""
    bsel = 'MAX(T0."BPLId") AS bplid,' if with_branch else ''
    bcol = 'COALESCE(B."BPLName", \'\') AS "branch",' if with_branch else '\'\' AS "branch",'
    bjoin = f'LEFT JOIN "{S}"."OBPL" B ON B."BPLId"=a.bplid' if with_branch else ''
    return f'''WITH aged AS (
      SELECT T0."ShortName" AS card, T0."TransId" AS trans, T0."Line_ID" AS line,
             MAX(T0."RefDate") AS bdate, MAX(T0."DueDate") AS duedate,
             MAX(T0."BaseRef") AS docno, MAX(T0."TransType") AS ttype, {bsel}
             -MAX(T0."BalDueCred")-SUM(T1."ReconSum") AS bal, -MAX(T0."Credit") AS orig
      FROM "{S}"."JDT1" T0
        JOIN "{S}"."ITR1" T1 ON T1."TransId"=T0."TransId" AND T1."TransRowId"=T0."Line_ID"
        JOIN "{S}"."OITR" T2 ON T2."ReconNum"=T1."ReconNum"
      WHERE {where_card} AND T0."RefDate"<={ag} AND T2."ReconDate">{ag} AND T1."IsCredit"='C'
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
      HAVING MAX(T0."BalFcCred")<>-SUM(T1."ReconSumFC") OR MAX(T0."BalDueCred")<>-SUM(T1."ReconSum")
      UNION ALL
      SELECT T0."ShortName", T0."TransId", T0."Line_ID",
             MAX(T0."RefDate"), MAX(T0."DueDate"), MAX(T0."BaseRef"), MAX(T0."TransType"), {bsel}
             MAX(T0."BalDueDeb")+SUM(T1."ReconSum"), MAX(T0."Debit")
      FROM "{S}"."JDT1" T0
        JOIN "{S}"."ITR1" T1 ON T1."TransId"=T0."TransId" AND T1."TransRowId"=T0."Line_ID"
        JOIN "{S}"."OITR" T2 ON T2."ReconNum"=T1."ReconNum"
      WHERE {where_card} AND T0."RefDate"<={ag} AND T2."ReconDate">{ag} AND T1."IsCredit"='D'
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
      HAVING MAX(T0."BalFcDeb")<>-SUM(T1."ReconSumFC") OR MAX(T0."BalDueDeb")<>-SUM(T1."ReconSum")
      UNION ALL
      SELECT T0."ShortName", T0."TransId", T0."Line_ID",
             MAX(T0."RefDate"), MAX(T0."DueDate"), MAX(T0."BaseRef"), MAX(T0."TransType"), {bsel}
             MAX(T0."BalDueDeb")-MAX(T0."BalDueCred"), MAX(T0."Debit")-MAX(T0."Credit")
      FROM "{S}"."JDT1" T0
      WHERE {where_card} AND T0."RefDate"<={ag}
        AND (T0."BalDueCred"<>T0."BalDueDeb" OR T0."BalFcCred"<>T0."BalFcDeb")
        AND NOT EXISTS (SELECT 1 FROM "{S}"."ITR1" U0 JOIN "{S}"."OITR" U1 ON U1."ReconNum"=U0."ReconNum"
          WHERE U0."TransId"=T0."TransId" AND U0."TransRowId"=T0."Line_ID" AND U1."ReconDate">{ag})
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
    )
    SELECT a.card AS "card", a.trans AS "trans", a.line AS "line", a.docno AS "docno", a.ttype AS "ttype",
           a.bdate AS "bdate", a.duedate AS "duedate", a.orig AS "original", a.bal AS "balance_due",
           {bcol}
           CASE WHEN a.bdate IS NULL OR DAYS_BETWEEN(a.bdate,{ag})<=7 THEN a.bal ELSE 0 END AS "b0_7",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 8 AND 15 THEN a.bal ELSE 0 END AS "b8_15",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 16 AND 30 THEN a.bal ELSE 0 END AS "b16_30",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 31 AND 60 THEN a.bal ELSE 0 END AS "b31_60",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 61 AND 90 THEN a.bal ELSE 0 END AS "b61_90",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 91 AND 120 THEN a.bal ELSE 0 END AS "b91_120",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag})>120 THEN a.bal ELSE 0 END AS "b121"
    FROM aged a {bjoin}
    WHERE ABS(a.bal) > 0.005
    ORDER BY a.duedate'''


def get_aging_credit_days(card_code):
    """{row_key: days_str} of per-invoice credit-period overrides for a customer, stored in
    AgingRemark with row_key='CREDITDAYS:<TransId:Line_ID>' (reuses the remark store, so no new
    table). '' / no row = no override → the row falls back to its P/C item-type default."""
    cc = (card_code or '').strip()
    if not cc:
        return {}
    prefix = 'CREDITDAYS:'
    return {a.row_key[len(prefix):]: a.remark
            for a in AgingRemark.objects.filter(card_code=cc, row_key__startswith=prefix)}


def get_customer_aging_item_types(card_code, doc_nums):
    """{DocNum(str): 'P'|'C'|'P+C'|''} classifying each AR invoice by whether its OITM line items
    are PREMIUM, COMMODITY, or both (after RECLASSIFY_RULES). Drives the per-invoice credit period
    on the Customer Aging detail page (P→30d, C→7d, P+C→user-entered). {} on SAP error."""
    cc = (card_code or '').strip()
    docs = sorted({str(d).strip() for d in (doc_nums or []) if str(d).strip().isdigit()})
    if not (cc and docs):
        return {}
    S = SAP_SCHEMA
    card_safe = cc.replace("'", "''")
    in_list = ','.join(docs)                      # digits-only (validated above) → safe to inline
    sql = ('SELECT H."DocNum" AS "doc", I."U_TYPE" AS "utype", I."U_Sub_Group" AS "usub", '
           'I."ItemName" AS "iname" '
           'FROM "%s"."OINV" H '
           'JOIN "%s"."INV1" L ON L."DocEntry"=H."DocEntry" '
           'LEFT JOIN "%s"."OITM" I ON I."ItemCode"=L."ItemCode" '
           'WHERE H."CardCode"=\'%s\' AND H."DocNum" IN (%s)' % (S, S, S, card_safe, in_list))
    try:
        rows = sap_connector.execute_query(sql)
    except Exception:
        logger.exception('[AGINGDETAIL] item-type classification failed')
        return {}
    agg = {}                                       # doc → {'P': bool, 'C': bool}
    for r in rows or []:
        doc = str(r.get('doc') or '').strip()
        ut = str(r.get('utype') or '').strip().upper()
        us = str(r.get('usub') or '').strip().upper()
        nm = str(r.get('iname') or '').strip().upper()
        ut, _us = _reclassify(ut, us, nm)
        a = agg.setdefault(doc, {'P': False, 'C': False})
        if ut == 'PREMIUM':
            a['P'] = True
        elif ut == 'COMMODITY':
            a['C'] = True
    out = {}
    for doc, a in agg.items():
        out[doc] = 'P+C' if (a['P'] and a['C']) else ('P' if a['P'] else ('C' if a['C'] else ''))
    return out


def get_customer_aging_detail(card_code, aging_date=None, schema=None, row_key_prefix='', classify=True):
    """Per-document open items for one customer as of aging_date, with saved remarks merged
    in. row_key ('<prefix>TransId:Line_ID') ties each row to its stored remark. [] on SAP error.

    `schema` selects the company DB (default oil = SAP_SCHEMA; pass MART_SCHEMA for Jivo Mart).
    `row_key_prefix` ('MART:' for Mart) keeps a company's per-document remarks/splits/credit-days
    from colliding with oil's when the two DBs reuse the same CardCode + TransId. `classify` runs
    the oil-only PREMIUM/COMMODITY item-type tagging — off for Mart."""
    if aging_date is None:
        aging_date = date.today()
    S = schema or SAP_SCHEMA
    ag = _aging_date_literal(aging_date)
    card_safe = (card_code or '').strip().replace("'", "''")
    if not card_safe:
        return []
    where_card = 'T0."ShortName"=\'%s\'' % card_safe
    rows = None
    for with_branch in (True, False):     # retry without the branch join if it errors
        try:
            rows = sap_connector.execute_query(_aging_detail_sql(where_card, ag, S, with_branch))
            break
        except Exception as exc:
            logger.error('[AGINGDETAIL] fetch failed (branch=%s): %s', with_branch, exc)
            rows = None
    if rows is None:
        return []
    remarks = get_aging_remarks(card_code)
    splits = get_aging_remark_lines(card_code)
    credit = get_aging_credit_days(card_code)
    out = []
    for r in rows:
        try:
            ttype = int(r.get('ttype'))
        except (TypeError, ValueError):
            ttype = None
        row_key = '%s%s:%s' % (row_key_prefix, str(r.get('trans') or '').strip(), str(r.get('line') or '').strip())
        out.append({
            'row_key': row_key,
            'doc_no': str(r.get('docno') or '').strip(),
            'type': _AGING_TYPE_MAP.get(ttype, (str(r.get('ttype')).strip() if r.get('ttype') is not None else '')),
            'posting_date': _aging_fmt_date(r.get('bdate')),
            'due_date': _aging_fmt_date(r.get('duedate')),
            'age_days': _aging_age_days(r.get('bdate'), aging_date),
            'branch': str(r.get('branch') or '').strip(),
            'original': _aging_num(r.get('original')),
            'balance_due': _aging_num(r.get('balance_due')),
            'remark': remarks.get(row_key, ''),
            'splits': splits.get(row_key, []),
            'credit_override': credit.get(row_key, ''),   # per-invoice credit-period override
            'item_type': '',                              # 'P' | 'C' | 'P+C' — filled below for invoices
            'b0_7': _aging_num(r.get('b0_7')),
            'b8_15': _aging_num(r.get('b8_15')),
            'b16_30': _aging_num(r.get('b16_30')),
            'b31_60': _aging_num(r.get('b31_60')),
            'b61_90': _aging_num(r.get('b61_90')),
            'b91_120': _aging_num(r.get('b91_120')),
            'b121': _aging_num(r.get('b121')),
        })
    # Classify AR invoices (type 'IN') by their line-item mix → P / C / P+C (oil only).
    inv_docs = [r['doc_no'] for r in out if r['type'] == 'IN' and r['doc_no']]
    if classify and inv_docs:
        types = get_customer_aging_item_types(card_code, inv_docs)
        for r in out:
            if r['type'] == 'IN':
                r['item_type'] = types.get(r['doc_no'], '')
    return out


def get_aging_remarks(card_code):
    """{row_key: remark} of saved per-document remarks for a customer."""
    cc = (card_code or '').strip()
    if not cc:
        return {}
    return {a.row_key: a.remark for a in AgingRemark.objects.filter(card_code=cc)}


def get_aging_remark_index():
    """For the Customer Aging remark filter: {card_code: [tokens]} plus the sorted master
    list of every distinct token. A token is either a split category (AgingRemarkLine) or a
    free-text per-document remark (AgingRemark) that a customer actually carries. All local
    DB — cheap and independent of the SAP aging cache, so it always reflects the latest edits."""
    from collections import defaultdict
    idx = defaultdict(set)
    for cc, cat in AgingRemarkLine.objects.exclude(category='').values_list('card_code', 'category'):
        c = (cat or '').strip()
        if c:
            idx[cc].add(c)
    for cc, rem in AgingRemark.objects.exclude(remark='').values_list('card_code', 'remark'):
        r = (rem or '').strip()
        if r:
            idx[cc].add(r)
    index = {cc: sorted(v) for cc, v in idx.items()}
    # The split categories are the fixed vocabulary, so always offer them (even before any
    # are used); add whatever free-text remarks people have actually typed on top.
    options = sorted(set(AGING_REMARK_CATEGORIES) | {t for toks in index.values() for t in toks},
                     key=lambda s: s.upper())
    return {'index': index, 'options': options}


def get_customer_aging_detail_bulk(card_codes, aging_date=None):
    """Per-document open items for MANY customers in one SAP round-trip (same engine as the
    single-customer detail), with saved remarks/splits merged in. Returns {card_code: [rows]}
    where each row matches get_customer_aging_detail. Used by the whole-book detail export."""
    codes = []
    seen = set()
    for c in (card_codes or []):
        c = (c or '').strip()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    if not codes:
        return {}
    ag_date = aging_date or date.today()
    ag = _aging_date_literal(ag_date)
    in_list = ",".join("'%s'" % c.replace("'", "''") for c in codes)
    where_card = 'T0."ShortName" IN (%s)' % in_list

    rows = None
    for with_branch in (True, False):
        try:
            rows = sap_connector.execute_query(_aging_detail_sql(where_card, ag, SAP_SCHEMA, with_branch))
            break
        except Exception as exc:
            logger.error('[AGINGDETAIL-BULK] fetch failed (branch=%s): %s', with_branch, exc)
            rows = None
    if rows is None:
        return {}

    # Bulk-load the local remarks/splits for all requested customers.
    remarks_by_card = {}
    for a in AgingRemark.objects.filter(card_code__in=codes):
        remarks_by_card.setdefault(a.card_code, {})[a.row_key] = a.remark
    from collections import defaultdict
    splits_by_card = defaultdict(lambda: defaultdict(list))
    for ln in AgingRemarkLine.objects.filter(card_code__in=codes).order_by('position', 'id'):
        splits_by_card[ln.card_code][ln.row_key].append(
            {'category': ln.category, 'amount': _aging_num(ln.amount), 'remark': ln.remark})

    out = defaultdict(list)
    for r in rows:
        card = str(r.get('card') or '').strip()
        try:
            ttype = int(r.get('ttype'))
        except (TypeError, ValueError):
            ttype = None
        row_key = '%s:%s' % (str(r.get('trans') or '').strip(), str(r.get('line') or '').strip())
        out[card].append({
            'row_key': row_key,
            'doc_no': str(r.get('docno') or '').strip(),
            'type': _AGING_TYPE_MAP.get(ttype, (str(r.get('ttype')).strip() if r.get('ttype') is not None else '')),
            'posting_date': _aging_fmt_date(r.get('bdate')),
            'due_date': _aging_fmt_date(r.get('duedate')),
            'age_days': _aging_age_days(r.get('bdate'), ag_date),
            'branch': str(r.get('branch') or '').strip(),
            'original': _aging_num(r.get('original')),
            'balance_due': _aging_num(r.get('balance_due')),
            'remark': remarks_by_card.get(card, {}).get(row_key, ''),
            'splits': splits_by_card.get(card, {}).get(row_key, []),
            'b0_7': _aging_num(r.get('b0_7')),
            'b8_15': _aging_num(r.get('b8_15')),
            'b16_30': _aging_num(r.get('b16_30')),
            'b31_60': _aging_num(r.get('b31_60')),
            'b61_90': _aging_num(r.get('b61_90')),
            'b91_120': _aging_num(r.get('b91_120')),
            'b121': _aging_num(r.get('b121')),
        })
    return dict(out)


def save_aging_remark(card_code, row_key, remark):
    """Upsert (or clear) one per-document remark."""
    cc = (card_code or '').strip()
    rk = (row_key or '').strip()[:80]
    if not cc or not rk:
        return False
    remark = (remark or '').strip()[:255]
    if remark:
        AgingRemark.objects.update_or_create(card_code=cc, row_key=rk, defaults={'remark': remark})
    else:
        AgingRemark.objects.filter(card_code=cc, row_key=rk).delete()
    return True


# ── Customer Aging NOT DUE / OVERDUE grace period (per customer) ──
# grace_days is the window, counted from a document's posting date, during which it is
# automatically 'NOT DUE' (Remarks cell locked). Once the aging date is that many days past
# posting, the document is 'OVERDUE' and the cell becomes editable. 0 = feature off.
def get_aging_grace_days(card_code):
    """The saved NOT DUE grace period (whole days) for a customer, or 0 if none/feature off."""
    cc = (card_code or '').strip()
    if not cc:
        return 0
    row = AgingDueConfig.objects.filter(card_code=cc).only('grace_days').first()
    return int(row.grace_days) if row else 0


def save_aging_grace_days(card_code, days, user=None):
    """Set (or clear) the NOT DUE grace period for a customer. days<=0 removes the setting so
    the Remarks column reverts to plain free text. Returns the stored value (0 when cleared)."""
    cc = (card_code or '').strip()
    if not cc:
        return 0
    try:
        d = int(float(days))
    except (TypeError, ValueError):
        d = 0
    d = max(0, min(d, 100000))          # sane bound; 0 = off
    if d > 0:
        defaults = {'grace_days': d}
        if user is not None and getattr(user, 'is_authenticated', False):
            defaults['updated_by'] = user
        AgingDueConfig.objects.update_or_create(card_code=cc, defaults=defaults)
    else:
        AgingDueConfig.objects.filter(card_code=cc).delete()
    return d


# Fixed category vocabulary for the aging-detail split breakdown. The detail page shows
# these as a dropdown (no free text) and the server rejects anything else, so the Category
# column can only ever hold one of these. Keep this the single source of truth — the view
# hands it to the template and save_aging_remark_lines validates against it.
AGING_REMARK_CATEGORIES = [
    'NOT DUE', 'RTV DEBIT', 'RTV PICK UP', 'SHORTAGE', 'CLAIM', 'TDS',
    'SHORT & EXCESS', 'OVERDUE', 'RC', 'JE', 'REVERSE WRONG CLAIM', 'ADVICE PENDING',
]
_AGING_REMARK_CATEGORY_SET = {c.upper() for c in AGING_REMARK_CATEGORIES}


def get_aging_remark_lines(card_code):
    """{row_key: [{'category','amount','remark'}, ...]} of saved per-document splits for a
    customer (TDS / RTV / Claim / … breakdown behind each open document's balance)."""
    cc = (card_code or '').strip()
    if not cc:
        return {}
    out = {}
    for ln in AgingRemarkLine.objects.filter(card_code=cc):
        out.setdefault(ln.row_key, []).append({
            'category': ln.category,
            'amount': float(ln.amount or 0),
            'remark': ln.remark,
        })
    return out


def save_aging_remark_lines(card_code, row_key, lines):
    """Replace the full set of splits for one open document. `lines` is a list of dicts with
    'category', 'amount', 'remark'. Blank lines (no category, no remark, zero amount) are
    dropped; an empty/all-blank list clears the row's splits."""
    cc = (card_code or '').strip()
    rk = (row_key or '').strip()[:80]
    if not cc or not rk:
        return False
    clean = []
    for i, ln in enumerate(lines or []):
        if not isinstance(ln, dict):
            continue
        category = str(ln.get('category') or '').strip().upper()[:60]
        # Categories are stored verbatim (free vocabulary): uploads and manual entry may use any
        # label. The detail-page dropdown offers the built-in AGING_REMARK_CATEGORIES plus whatever
        # categories are already in use, and re-selects the saved value.
        remark = str(ln.get('remark') or '').strip()[:255]
        raw = ln.get('amount')
        if isinstance(raw, str):
            raw = raw.replace('₹', '').replace(',', '').strip()
        try:
            amount = round(float(raw or 0), 2)
        except (TypeError, ValueError):
            amount = 0.0
        if not category and not remark and abs(amount) < 0.005:
            continue                                   # skip fully-empty rows
        clean.append(AgingRemarkLine(card_code=cc, row_key=rk, category=category,
                                     amount=amount, remark=remark, position=i))
    AgingRemarkLine.objects.filter(card_code=cc, row_key=rk).delete()
    if clean:
        AgingRemarkLine.objects.bulk_create(clean)
    return True


def clear_aging_remarks(card_code, row_keys=None):
    """Delete saved per-document Remarks for a customer (the top-level Remarks column only;
    split breakdowns are left intact). Pass row_keys to limit the clear to specific lines;
    omit it to clear every remark for the customer. Returns how many were removed."""
    cc = (card_code or '').strip()
    if not cc:
        return 0
    qs = AgingRemark.objects.filter(card_code=cc)
    if row_keys is not None:
        qs = qs.filter(row_key__in=[str(k).strip() for k in row_keys if str(k).strip()])
    n = qs.count()
    qs.delete()
    return n


def clear_ar_overrides(prefixes):
    """Delete every AgingRemark whose row_key starts with one of `prefixes`. Used by the RAW DATA
    'Clear' action to wipe stale COMPANY-WIDE per-invoice overrides (e.g. 'BEVSP:' for all
    Beverages Actual Sales Person entries, 'BEVDOC:' for the Remarks) before a fresh upload —
    unlike clear_aging_remarks, which is scoped to one customer. The aging payload re-attaches
    remarks fresh on every load (_ar_attach_remarks), so a clear is reflected on the next fetch.
    Returns how many rows were removed."""
    total = 0
    for p in prefixes:
        p = (p or '').strip()
        if not p:
            continue
        qs = AgingRemark.objects.filter(row_key__startswith=p)
        total += qs.count()
        qs.delete()
    return total


# what → the row_key prefixes each RAW DATA company wipes: 'remarks' = the note (BEV/OILDOC:),
# 'sp' = the Actual Sales Person (BEV/OILSP:), 'both' = both. Mirrors _customer_aging_ar's prefixes.
_AR_CLEAR_PREFIXES = {
    'bev': {'remarks': ['BEVDOC:'], 'sp': ['BEVSP:'], 'both': ['BEVDOC:', 'BEVSP:']},
    'oil': {'remarks': ['OILDOC:'], 'sp': ['OILSP:'], 'both': ['OILDOC:', 'OILSP:']},
}


def clear_ar_company_overrides(company, what='both'):
    """Clear a RAW DATA company's per-invoice overrides. company in {'bev','oil'}; what in
    {'remarks','sp','both'} (default/unknown → 'both'). Returns how many rows were removed."""
    by_what = _AR_CLEAR_PREFIXES.get(company)
    if not by_what:
        return 0
    prefixes = by_what.get((what or 'both').strip().lower(), by_what['both'])
    return clear_ar_overrides(prefixes)


def bulk_update_aging_remarks(card_code, aging_date, entries):
    """Apply an uploaded sheet to a customer's open documents, matching on Doc No (JDT1.BaseRef)
    as of aging_date. ``entries`` is {doc_no: {'remark': str|None, 'splits': [{category, amount,
    remark}, ...]}}, where a split ``amount`` of None means 'allocate this document line's full
    Balance Due to the category'. For each matched Doc No it sets the document Remark (note) AND
    replaces its split breakdown — so a single Remark/Category value fills both the note and the
    on-screen Category with the balance allocated. A Doc No spanning several open lines is applied
    to each (each line's own Balance Due). Categories are stored verbatim (free vocabulary). Blank
    entries are skipped. Returns {rows, matched_docs, updated, splits, unmatched}."""
    cc = (card_code or '').strip()
    entries = entries or {}
    if not cc:
        return {'rows': 0, 'matched_docs': 0, 'updated': 0, 'splits': 0, 'unmatched': []}
    by_doc = {}
    for r in get_customer_aging_detail(cc, aging_date):
        by_doc.setdefault(str(r.get('doc_no') or '').strip(), []).append(
            (r.get('row_key'), float(r.get('balance_due') or 0)))
    matched, updated, split_lines, unmatched = set(), 0, 0, []
    total_rows = 0
    for doc, entry in entries.items():
        doc = str(doc or '').strip()
        splits = entry.get('splits') or []
        remark = (entry.get('remark') or '').strip()
        total_rows += len(splits) + (1 if (remark and not splits) else 0)
        if not doc or (not remark and not splits):
            continue
        keys = by_doc.get(doc)
        if not keys:
            unmatched.append(doc)
            continue
        matched.add(doc)
        for rk, bal in keys:
            if remark and save_aging_remark(cc, rk, remark):
                updated += 1
            if splits:                       # amount None → allocate this line's full Balance Due
                resolved = [{'category': s.get('category', ''), 'remark': s.get('remark', ''),
                             'amount': (bal if s.get('amount') is None else s.get('amount'))}
                            for s in splits]
                save_aging_remark_lines(cc, rk, resolved)
                split_lines += 1
    # de-dup unmatched, keep order, cap for the response
    seen, uniq = set(), []
    for d in unmatched:
        if d not in seen:
            seen.add(d); uniq.append(d)
    return {'rows': total_rows, 'matched_docs': len(matched), 'updated': updated,
            'splits': split_lines, 'unmatched': uniq[:50]}


def _bulk_update_ar_remarks(rows_fn, aging_date, doc_prefix, sp_prefix, label, entries):
    """Bulk-set an A/R company's open-invoice Remarks from an uploaded {doc_no: {'remark',
    'splits'}} set, matching on Doc No. Remarks are AgingRemark rows keyed by (customer code,
    '<doc_prefix>'+DocNo) — see _customer_aging_ar / [[oih-reco-beverages-toggle]] — so the
    customer code for each Doc No is resolved from `rows_fn(aging_date)`'s current aging rows.
    A Doc No that is on more than one customer's open book is set on each. Blank remarks are
    skipped.

    When the sheet also carries an 'Actual Sales Person' column (entry['actual_sp']), that value is
    written to the same doc's Actual Sales Person (AgingRemark '<sp_prefix>'+DocNo) — matching a doc
    on its actual_sp alone (no remark) still counts. If the column is absent it is simply skipped.
    Returns {rows, matched_docs, updated, sp_updated, unmatched}."""
    entries = entries or {}
    try:
        rows = rows_fn(aging_date).get('rows', [])
    except Exception as exc:
        logger.error('[%s-AGING] remark upload lookup failed: %s', label.upper(), exc)
        return {'rows': 0, 'matched_docs': 0, 'updated': 0, 'unmatched': [],
                'error': 'Could not read %s invoices from SAP.' % label}
    by_doc = {}
    for r in rows:
        d = str(r.get('doc') or '').strip()
        code = str(r.get('code') or '').strip()
        if d and code:
            by_doc.setdefault(d, set()).add(code)
    matched, updated, sp_updated, unmatched, total = set(), 0, 0, [], 0
    for doc, entry in entries.items():
        doc = str(doc or '').strip()
        remark = (entry.get('remark') or '').strip()
        if not remark:                         # fall back to a Category column if that's all there was
            sp = entry.get('splits') or []
            remark = str(sp[0].get('category') or '').strip() if sp else ''
        actual_sp = (entry.get('actual_sp') or '').strip()   # optional Actual Sales Person override
        total += 1
        if not doc or (not remark and not actual_sp):
            continue
        codes = by_doc.get(doc)
        if not codes:
            unmatched.append(doc)
            continue
        matched.add(doc)
        for code in codes:
            if remark and save_aging_remark(code, doc_prefix + doc, remark):
                updated += 1
            if actual_sp and save_aging_remark(code, sp_prefix + doc, actual_sp):
                sp_updated += 1
    seen, uniq = set(), []
    for d in unmatched:
        if d not in seen:
            seen.add(d); uniq.append(d)
    return {'rows': total, 'matched_docs': len(matched), 'updated': updated,
            'sp_updated': sp_updated, 'unmatched': uniq[:50]}


def bulk_update_beverages_remarks(aging_date, entries):
    """Bulk-update Jivo Beverages open-invoice remarks from an uploaded sheet, matched by Doc No."""
    return _bulk_update_ar_remarks(get_customer_aging_beverages, aging_date,
                                   'BEVDOC:', 'BEVSP:', 'beverages', entries)


def bulk_update_oil_ar_remarks(aging_date, entries):
    """Bulk-update Jivo Oil open-invoice RAW DATA remarks from an uploaded sheet, matched by Doc No."""
    return _bulk_update_ar_remarks(get_customer_aging_oil_ar, aging_date,
                                   'OILDOC:', 'OILSP:', 'oil', entries)


# ══════════════════════ Claims register ══════════════════════
# A manually-maintained claim register (see the Claim model). Nothing here is read from SAP as
# report data — SAP only feeds the entry pickers: customers (party → main group) and product/item
# masters. Every claim row is entered and edited by a reviewer and persisted via the CRUD helpers.
_CLAIM_MASTERS_TTL = 300   # 5 min — master data changes rarely


def get_claim_masters():
    """Picker data for the Claims add/edit form: {status, customers}. customers =
    [{code, name, main_group}] from the customer master; picking a party fills its Main Group.
    Cached _CLAIM_MASTERS_TTL seconds. (Product/Item pickers were removed from the form.)"""
    now = time.time()
    hit = _shared_get('claimmasters', 'all')
    if hit is not None:
        return hit

    # Customers — reuse the (cached) customer master, trimmed to what the picker needs.
    try:
        cust_rows = get_customer_master().get('rows', [])
    except Exception as exc:
        logger.error('[CLAIMS] customer master fetch failed: %s', exc)
        cust_rows = []
    customers = [{'code': r.get('code', ''), 'name': r.get('name', ''),
                  'main_group': r.get('main_group', '') or ''} for r in cust_rows if r.get('name')]
    customers.sort(key=lambda c: c['name'])

    payload = {'status': 'ok', 'customers': customers}
    if customers:
        _shared_set('claimmasters', 'all', payload, _CLAIM_MASTERS_TTL)
    return payload


def _serialize_claim(c):
    """One Claim row → the flat dict the frontend table/drill consumes."""
    d = c.claim_date
    cm = (c.claim_month or '').strip()      # explicit 'YYYY-MM' month picked on the form
    ym, month_year = '', ''
    if cm:
        try:
            month_year = datetime.strptime(cm, '%Y-%m').strftime('%b %Y')
            ym = cm
        except ValueError:
            cm = ''
    if not ym:                               # no explicit month → derive from the receiving date
        ym = d.strftime('%Y-%m') if d else ''
        month_year = d.strftime('%b %Y') if d else ''
    return {
        'id': c.id,
        'claim_date': d.isoformat() if d else '',
        'claim_month': cm,                                   # explicit picker value ('' = follow claim_date)
        'month_year': month_year,                            # 'Claim Month & Year' (explicit month, else derived)
        'ym': ym,                                            # sortable month key + Month filter value
        'party_code': c.party_code or '',
        'party_name': c.party_name or '',
        'main_group': c.main_group or '',
        'product': c.product or '',
        'item': c.item or '',
        'claim_type': c.claim_type or '',
        'ref_inv_no': c.ref_inv_no or '',
        'coop_no': c.coop_no or '',
        'claim_amount': float(c.claim_amount or 0),
        'claim_pass_date': c.claim_pass_date.isoformat() if c.claim_pass_date else '',
        'claim_hold': c.claim_hold or '',
        'claim_passed': float(c.claim_passed or 0),
        'hold_amount': float(c.hold_amount or 0),
        'reason_of_hold': c.reason_of_hold or '',
    }


def get_claims():
    """Every claim register row, newest first, serialized for the report. Read straight from the
    DB (no SAP, no cache — the set is small and edited live)."""
    return {'status': 'ok', 'rows': [_serialize_claim(c) for c in Claim.objects.all()]}


def _parse_claim_date(v):
    """A 'YYYY-MM-DD' string → date, or None if blank/unparseable."""
    try:
        return datetime.strptime(str(v or '').strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _parse_claim_amount(v):
    try:
        return Decimal(str(v if v not in (None, '') else 0))
    except Exception:
        return Decimal('0')


def _parse_claim_month(v):
    """A 'YYYY-MM' month string (from the form's month picker) → validated 'YYYY-MM', or '' if
    blank/unparseable (in which case the report derives the month from claim_date)."""
    s = str(v or '').strip()
    if not s:
        return ''
    try:
        return datetime.strptime(s, '%Y-%m').strftime('%Y-%m')
    except (ValueError, TypeError):
        return ''


def upsert_claim(data, user=None):
    """Create or update one claim from the submitted form dict. Requires a valid claim_date and a
    non-blank party_name. Returns the serialized row, or raises ValueError on bad input."""
    claim_date = _parse_claim_date(data.get('claim_date'))
    if not claim_date:
        raise ValueError('A valid Claim Date is required.')
    party_name = str(data.get('party_name') or '').strip()
    if not party_name:
        raise ValueError('Party Name is required.')

    cid = data.get('id')
    claim = Claim.objects.filter(id=cid).first() if cid else None
    if claim is None:
        claim = Claim(created_by=user if getattr(user, 'is_authenticated', False) else None)

    claim.claim_date      = claim_date
    claim.claim_month     = _parse_claim_month(data.get('claim_month'))
    claim.party_code      = str(data.get('party_code') or '').strip()
    claim.party_name      = party_name
    claim.main_group      = str(data.get('main_group') or '').strip()
    claim.product         = str(data.get('product') or '').strip()
    claim.item            = str(data.get('item') or '').strip()
    claim.claim_type      = str(data.get('claim_type') or '').strip()
    claim.ref_inv_no      = str(data.get('ref_inv_no') or '').strip()[:100]
    claim.coop_no         = str(data.get('coop_no') or '').strip()[:100]
    claim.claim_amount    = _parse_claim_amount(data.get('claim_amount'))
    claim.claim_pass_date = _parse_claim_date(data.get('claim_pass_date'))
    hold = str(data.get('claim_hold') or '').strip().capitalize()
    claim.claim_hold      = hold if hold in ('Yes', 'No') else ''
    claim.claim_passed    = _parse_claim_amount(data.get('claim_passed'))
    claim.hold_amount     = claim.claim_amount - claim.claim_passed   # Hold = Claim Amount − Claim Passed (derived)
    claim.reason_of_hold  = str(data.get('reason_of_hold') or '').strip()[:255]
    claim.save()
    return _serialize_claim(claim)


def _claim_upload_date(v):
    """Any date-ish upload cell → 'YYYY-MM-DD' (the form format _parse_claim_date accepts), or ''.
    Handles real datetimes (openpyxl) and the export's dd.mm.yyyy text plus common variants."""
    if v in (None, ''):
        return ''
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d-%m-%Y', '%d/%m/%Y', '%d.%m.%y', '%d-%m-%y', '%m/%d/%Y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ''


def _claim_upload_month(v):
    """Any month-ish upload cell → 'YYYY-MM' (the format _parse_claim_month accepts), or ''.
    Handles the export's 'Jul 2026' label plus dates / 'YYYY-MM' / 'MM/YYYY'."""
    if v in (None, ''):
        return ''
    if isinstance(v, (datetime, date)):
        return v.strftime('%Y-%m')
    s = str(v).strip()
    for fmt in ('%b %Y', '%B %Y', '%Y-%m', '%m/%Y', '%b-%Y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m')
        except ValueError:
            pass
    d = _claim_upload_date(s)
    return d[:7] if d else ''


def _claim_upload_amount(v):
    """A ₹/comma-formatted or numeric upload cell → a plain number (0 if unparseable)."""
    if isinstance(v, (int, float)):
        return v
    s = ''.join(ch for ch in str(v if v is not None else '') if ch.isdigit() or ch in '.-')
    if s in ('', '-', '.', '-.', '--'):
        return 0
    try:
        return float(s)
    except ValueError:
        return 0


def bulk_upload_claims(rows, user=None):
    """Create Claim rows from parsed upload records (the columns of the Claims Excel export). Each
    record is normalized (dates → YYYY-MM-DD, month → YYYY-MM, amounts → numbers) then handed to
    upsert_claim, so it ADDS new claims (it does not update existing ones — the export carries no
    id). Rows without a valid Receiving Date + Party Name are skipped. Returns
    {created, skipped, errors:[...]}."""
    created, skipped, errors = 0, 0, []
    for idx, r in enumerate(rows or [], 1):
        party = str(r.get('party_name') or '').strip()
        cdate = _claim_upload_date(r.get('claim_date'))
        if not party or not cdate:
            skipped += 1
            if party and not cdate:
                errors.append(f'Row {idx} ({party}): missing / unreadable Receiving Date')
            continue
        hold = str(r.get('claim_hold') or '').strip().capitalize()
        data = {
            'claim_date':      cdate,
            'party_name':      party,
            'claim_pass_date': _claim_upload_date(r.get('claim_pass_date')),
            'claim_month':     _claim_upload_month(r.get('claim_month')),
            'claim_type':      str(r.get('claim_type') or '').strip(),
            'ref_inv_no':      str(r.get('ref_inv_no') or '').strip(),
            'coop_no':         str(r.get('coop_no') or '').strip(),
            'claim_hold':      hold if hold in ('Yes', 'No') else '',
            'claim_amount':    _claim_upload_amount(r.get('claim_amount')),
            'claim_passed':    _claim_upload_amount(r.get('claim_passed')),
            'main_group':      str(r.get('main_group') or '').strip(),
            'reason_of_hold':  str(r.get('reason_of_hold') or '').strip(),
        }
        try:
            upsert_claim(data, user=user)
            created += 1
        except Exception as exc:
            skipped += 1
            errors.append(f'Row {idx} ({party}): {exc}')
    return {'created': created, 'skipped': skipped, 'errors': errors[:25]}


def delete_claim(claim_id):
    """Delete one claim by id. Returns True if a row was removed."""
    deleted, _ = Claim.objects.filter(id=claim_id).delete()
    return bool(deleted)


# get_channel_actuals grinds through every raw sales row to bucket them by
# channel. The answer only depends on the date range, so hold it in the shared
# on-disk cache: unlike the in-process dict above, that survives a restart and
# is shared by every worker process.
def get_channel_actuals(start_date, end_date):
    from core.kpi_cache import remember, ttl_for_range

    return remember(
        f'chan_actuals:{start_date}:{end_date}',
        ttl_for_range(start_date, end_date),
        lambda: _get_channel_actuals_uncached(start_date, end_date),
    )
