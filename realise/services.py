import logging
import threading
import time
from decimal import Decimal
from datetime import date, datetime, timedelta

from django.utils import timezone

from core import sap_connector
from .models import (MainGroupMaster, MonthlyTarget, SegmentTarget, StateMaster,
                     TargetMaster, TargetNode, TerritoryMapping, TerritoryProductTarget,
                     CityOwner, ClosingRemark, CreditLock, CreditLockSnapshot, FlexTarget,
                     AgingRemark, AgingRemarkLine)

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


def _bev_accum_item(store, item, sku, brand, qty, box):
    """Accumulate a single day's sale into a per-item bucket (keyed by item/SKU/brand)
    for the day-specific 'what was sold' drill-downs."""
    cell = store.setdefault((item, sku, brand), {'quantity': 0.0, 'boxes': 0.0})
    cell['quantity'] += qty
    cell['boxes'] += box


def _bev_items_list(store):
    out = [{'item': k[0], 'sku': k[1], 'brand': k[2],
            'quantity': round(v['quantity'], 2), 'boxes': round(v['boxes'], 2)}
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


def get_beverages_rows(start_date, end_date):
    """Beverage sales rows aggregated by (Variety, Sub_Group, SKU, Item, Main Group, State,
    Brand, Chain, Month) with Quantity (PCS) and Boxes, plus today's & yesterday's box
    totals, a per-item breakdown for each of those days, and customer/month aggregates.
    The client nests the rows into any drill order, filters by Brand/Month, and opens the
    day & top breakdowns from the KPIs."""
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
        dd = _bev_date(_bev_pick(r, 'DocDate', 'DOCDATE', 'Doc_Date', 'doc_date'))
        ym, mlabel = _bev_month_key(r, dd)
        ymk = ym or ''   # carried on each row so the client can filter to a single month
        key = (variety, sub, sku, item, main_group, state, brand, chain, sales_person, customer, ymk)
        cell = agg.setdefault(key, {'quantity': 0.0, 'boxes': 0.0})
        cell['quantity'] += qty
        cell['boxes'] += box
        cc = cust_agg.setdefault((customer, brand, ymk), {'quantity': 0.0, 'boxes': 0.0})
        cc['quantity'] += qty; cc['boxes'] += box
        if ym:
            mc = month_agg.setdefault((ym, brand), {'quantity': 0.0, 'boxes': 0.0, 'label': mlabel})
            mc['quantity'] += qty; mc['boxes'] += box
        if dd == today:
            today_boxes += box
            _bev_accum_item(today_items, item, sku, brand, qty, box)
        elif dd == yesterday:
            yest_boxes += box
            _bev_accum_item(yest_items, item, sku, brand, qty, box)

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
            'customer_rows': customer_rows, 'month_rows': month_rows, 'oih_rows': oih_rows}


def get_beverages_rows_cached(start_date, end_date):
    key = f'{start_date}|{end_date}'
    now = time.time()
    hit = _BEV_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    data = get_beverages_rows(start_date, end_date)
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
_SALES_CACHE = {}                 # 'start|end' -> {'exp', 'val', 'refreshing'}
_SALES_CACHE_TTL = 1800           # 30 min — historical SAP data drifts slowly; bg-refreshed
_SALES_CACHE_LOCK = threading.Lock()


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
    # Cold (never cached) or forced refresh → fetch synchronously.
    return _sales_fetch_and_store(key, start_date, end_date)


# ── Live "heartbeat" pulse ──────────────────────────────────────────────────
# A tiny fingerprint of the data the dashboard shows, so the client can poll it cheaply every
# 30s and only trigger a (heavy) fresh pull when something ACTUALLY changed. It moves when an
# invoice in the window is added / edited / cancelled (OINV, + ORIN credit notes for oils) or an
# open order is added or (partly) delivered (ORDR / RDR1). Header/line aggregates only — orders
# of magnitude cheaper than REPORT_SALES_ANALYSIS — cached ~10s so many tabs can poll for free.
_PULSE_CACHE = {}          # (dataset, start, end) -> (expires_at, pulse_string)
_PULSE_TTL = 10


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
                val = str(d.get(fk, '') or '').strip()
                if val.upper() != str(fv).upper():
                    skip = True
                    break
            if skip:
                continue

        dim_val = str(d.get(drill_by, '') or 'UNKNOWN').strip()
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


def get_channel_actuals(start_date, end_date):
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
    for r in d['rows']:
        channel = _normalize_name(r['channel'])
        name = _normalize_name(r['state_name'])
        code = _normalize_name(r['state_code'])
        person = _normalize_name(r['sales_person'])
        if name and person:
            person_map[channel + '|' + name] = person
            for raw in CHANNEL_MEMBERS.get(channel, [channel]):
                person_map[_normalize_name(raw) + '|' + name] = person
            # Build the per-channel whitelist for EVERY channel that has a per-state
            # owner (not just GT/MT) so an assigned state always shows as a card row in
            # that channel — even with zero live Done. The dashboard treats this list
            # additively (union with live sales states), so nothing is dropped.
            bucket = whitelist.setdefault(channel, [])
            if not any(e['label'] == name for e in bucket):
                bucket.append({'label': name, 'match': [name] + ([code] if code else [])})
        if not name and person:
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
PRODUCT_MERGED = {'EXTRA VIRGIN OLIVE'}


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


def save_territory_product_targets(month, year, targets_obj, user=None):
    """Replace a period's per-product territory targets with the submitted set (the UI
    always holds the full set), then rebuild the dashboard roll-ups. targets_obj shape:
    {'CHANNEL||STATE': {'P#SUBGROUP': {'l': ltrs, 'r': realise}, ...}}."""
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
    """[{GRP, ST, CUST, SUBG, UTYPE, OPEN_QTY}] open-order litres split by customer and
    by product/type (from the order line's item) so the dashboard can filter Order-in-
    Hand by Premium/Commodity the same way Done is filtered. State/city come from the
    order's ship-to address (CRD1), not the BP-master HQ."""
    sql = f'''
        SELECT COALESCE(TRIM(C."U_Main_Group"), '') AS "GRP",
               {_SHIPTO_STATE} AS "ST",
               {_SHIPTO_CITY} AS "CITY",
               COALESCE(TRIM(H."CardCode"), '')      AS "CCODE",
               COALESCE(TRIM(C."CardName"), '')      AS "CUST",
               COALESCE(TRIM(I."U_Sub_Group"), '')   AS "SUBG",
               COALESCE(TRIM(I."U_TYPE"), '')        AS "UTYPE",
               COALESCE(TRIM(I."ItemName"), '')      AS "ITEM",
               SUM(L."OpenQty" * COALESCE(I."SalPackUn", 0)) AS "OPEN_QTY"
        FROM "{SAP_SCHEMA}"."ORDR" H
        JOIN "{SAP_SCHEMA}"."RDR1" L ON L."DocEntry" = H."DocEntry"
        JOIN "{SAP_SCHEMA}"."OCRD" C ON C."CardCode" = H."CardCode"
        LEFT JOIN "{SAP_SCHEMA}"."OITM" I ON I."ItemCode" = L."ItemCode"
        {_SHIPTO_JOIN.format(S=SAP_SCHEMA)}
        WHERE H."DocStatus" = 'O' AND L."LineStatus" = 'O'
        GROUP BY COALESCE(TRIM(C."U_Main_Group"), ''), {_SHIPTO_STATE}, {_SHIPTO_CITY},
                 COALESCE(TRIM(H."CardCode"), ''),
                 COALESCE(TRIM(C."CardName"), ''), COALESCE(TRIM(I."U_Sub_Group"), ''),
                 COALESCE(TRIM(I."U_TYPE"), ''), COALESCE(TRIM(I."ItemName"), '')
    '''
    try:
        return sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[OIH] open-order (by customer) fetch failed: %s', exc)
        return []


def get_order_in_hand_rows():
    """Granular open-order rows: {main_group, state(name), sales_person, card_name,
    u_type, u_sub_group, item_name, open_qty}. open_qty is in LITRES (Quantity *
    OITM.SalPackUn), matching Done. u_type/u_sub_group/item_name let the dashboard
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
            'open_qty': float(d.get('OPEN_QTY') or 0),
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
_credit_ledger_cache = {}


def _credit_ledger_asof(as_of_date):
    """{card_code: customer account balance as of the END of as_of_date}, computed via SAP B1's
    reconciliation engine (the same query that powers Customer Aging; its balance_due ties to
    OCRD.Balance to the rupee). It reverses BOTH invoices and payments dated after the date, so
    it is the true historical ledger — unlike a payments-only roll-back. Cached per date for
    _AGING_TTL seconds. {} on any SAP error (the caller then falls back to the live balance)."""
    key = as_of_date.isoformat()
    now = time.time()
    hit = _credit_ledger_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        m = {}
        for r in _load_aging_rows_sap(as_of_date):
            cc = _normalize_name(r.get('code'))
            m[cc] = m.get(cc, 0.0) + float(r.get('balance_due') or 0)
        _credit_ledger_cache[key] = (now + _AGING_TTL, m)
        return m
    except Exception as exc:
        logger.error('[REQCREDIT] ledger-as-of fetch failed: %s', exc)
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
                '_sotot': {},       # SO number -> its FULL order total (ORDR.DocTotal, matches SAP)
                **_blank_bucket(),
            }
        card_balance.setdefault(card_code, float(d.get('BAL') or 0))
        if docnum:
            bucket['_so'][docnum] = bucket['_so'].get(docnum, 0.0) + val
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
        bucket['so_list'] = [{'no': n, 'value': round(v, 2)} for n, v in so_items]
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
    for asm in sorted(by_asm):
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


def _render_single_sheet_xlsx(title, cells, widths, max_row, max_col, freeze_rows=0, grid_from_row=2):
    """Minimal pure-Python .xlsx writer (no third-party deps, mirrors core.simple_xlsx so it
    works on servers without openpyxl). `cells` maps (row, col)→(style, kind, value) where kind
    is 't' (text) or 'n' (integer number). Supports per-column widths (1-based col→width), an
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
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/></patternFill></fill></fills>'
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
        + _thin +
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="6">'
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
              10: 22.0, 11: 14.0, 12: 15.0, 13: 14.0, 14: 18.0, 15: 14.0, 16: 14.0, 17: 14.0}
    MAX_COL = 17

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

    put_text(1, 1, 'Sum of TOTAL LTR', style=_ST_TITLE)
    headers = ['SO NAME', 'PARTY NAME', 'TYPE', 'MAIN GROUP', 'STATE', 'DELIVERY REMARK',
               'PREMIUM', 'COMMODITY', 'Grand Total', 'SO NO', 'PI AMT', 'PI TOTAL AMT',
               'LEDGER AMT', 'TOTAL OUTSTANDING', 'Required Limit', 'PAYMENT DONE', 'OUTSTANDING']
    for i, h in enumerate(headers, start=1):
        put_text(2, i, h, style=_ST_HEAD)

    r = 3
    g_prem = g_com = g_tot = g_val = g_pitot = g_led = g_out = g_req = g_pay = g_rem = 0.0
    for g in payload.get('asms', []):
        rows = g.get('rows', [])
        if types:
            rows = [x for x in rows if x.get('type') in types]
        if not rows:
            continue
        first = True
        s_prem = s_com = s_tot = s_val = s_pitot = s_led = s_out = s_req = s_pay = s_rem = 0.0
        for row in rows:
            prem = float(row['litres'].get('premium', 0) or 0)
            commodity = float(row['litres'].get('commodity', 0) or 0)
            total = float(row['litres'].get('total', 0) or 0)
            val = float(row['value'].get('total', 0) or 0)
            pi_total = float(row['value'].get('pi_total', 0) or 0)   # full SO total (matches SAP)
            ledger = float(row['value'].get('ledger', 0) or 0)
            outstanding = float(row['value'].get('outstanding', 0) or 0)
            required = float(row['value'].get('required_limit', 0) or 0)
            payment = float(row['value'].get('payment_done', 0) or 0)   # receipts on the selected date
            remaining = float(row['value'].get('remaining', 0) or 0)
            if first:
                put_text(r, 1, g['asm'])                  # A SO NAME (ASM)
            put_text(r, 2, row['party'])                  # B PARTY NAME
            put_text(r, 3, row.get('type'))               # C TYPE
            put_text(r, 4, row.get('main_group'))         # D MAIN GROUP
            put_text(r, 5, row.get('state'))              # E STATE
            put_text(r, 6, row.get('remark'))             # F DELIVERY REMARK
            put_num(r, 7, prem)                           # G PREMIUM
            put_num(r, 8, commodity)                      # H COMMODITY
            put_num(r, 9, total, style=_ST_BNUM)          # I Grand Total
            put_text(r, 10, row.get('so_nos'))            # J SO NO
            put_num(r, 11, val)                           # K PI AMT (OIH revenue — open/undelivered)
            put_num(r, 12, pi_total)                      # L PI TOTAL AMT (full SO total, matches SAP)
            put_num(r, 13, ledger)                        # M LEDGER AMT (+rec / -pay)
            put_num(r, 14, outstanding)                   # N TOTAL OUTSTANDING
            put_num(r, 15, required)                      # O Required Limit (outstanding + 2%)
            put_num(r, 16, payment)                       # P PAYMENT DONE (receipts on the selected date)
            put_num(r, 17, remaining)                     # Q OUTSTANDING (= Total Outstanding − Payment)
            s_prem += prem; s_com += commodity; s_tot += total
            s_val += val; s_pitot += pi_total; s_led += ledger; s_out += outstanding; s_req += required
            s_pay += payment; s_rem += remaining
            first = False
            r += 1
        put_text(r, 1, f"{g['asm']} Total", style=_ST_BTEXT)
        put_num(r, 7, s_prem, style=_ST_BNUM); put_num(r, 8, s_com, style=_ST_BNUM)
        put_num(r, 9, s_tot, style=_ST_BNUM); put_num(r, 11, s_val, style=_ST_BNUM)
        put_num(r, 12, s_pitot, style=_ST_BNUM); put_num(r, 13, s_led, style=_ST_BNUM)
        put_num(r, 14, s_out, style=_ST_BNUM); put_num(r, 15, s_req, style=_ST_BNUM)
        put_num(r, 16, s_pay, style=_ST_BNUM); put_num(r, 17, s_rem, style=_ST_BNUM)
        g_prem += s_prem; g_com += s_com; g_tot += s_tot
        g_val += s_val; g_pitot += s_pitot; g_led += s_led; g_out += s_out; g_req += s_req
        g_pay += s_pay; g_rem += s_rem
        r += 1

    put_text(r, 1, 'Grand Total', style=_ST_BTEXT)
    put_num(r, 7, g_prem, style=_ST_BNUM); put_num(r, 8, g_com, style=_ST_BNUM)
    put_num(r, 9, g_tot, style=_ST_BNUM); put_num(r, 11, g_val, style=_ST_BNUM)
    put_num(r, 12, g_pitot, style=_ST_BNUM); put_num(r, 13, g_led, style=_ST_BNUM)
    put_num(r, 14, g_out, style=_ST_BNUM); put_num(r, 15, g_req, style=_ST_BNUM)
    put_num(r, 16, g_pay, style=_ST_BNUM); put_num(r, 17, g_rem, style=_ST_BNUM)

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
           {sign} * L."Quantity" * COALESCE(I."SalPackUn", 0) AS "LIT"
    FROM "{S}"."{hdr}" H
    JOIN "{S}"."{ln}" L ON L."DocEntry" = H."DocEntry"
    JOIN "{S}"."OCRD" C ON C."CardCode" = H."CardCode"
    LEFT JOIN "{S}"."OITM" I ON I."ItemCode" = L."ItemCode"
    ''' + _SHIPTO_JOIN + '''
    WHERE H."DocDate" BETWEEN ? AND ? AND H."CANCELED" = 'N'
      AND (H."U_ARNO" NOT IN ('T', 'H') OR H."U_ARNO" IS NULL)
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
    inv = _DONE_LINE_SQL.format(S=SAP_SCHEMA, hdr='OINV', ln='INV1', sign='1')
    crd = _DONE_LINE_SQL.format(S=SAP_SCHEMA, hdr='ORIN', ln='RIN1', sign='-1')
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
            'item': _normalize_name(row.get('ITEM')) or '—',
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
OIH_STOCK_WAREHOUSES = ['GP-FG', 'BH-EC', 'BH-PF']


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
            'item': _normalize_name(row.get('ITEM')) or '—',
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
               COALESCE(TRIM(I."ItemName"), '—')      AS "ITEM",
               COALESCE(TRIM(I."ItemCode"), '')        AS "ICODE",
               COALESCE(TRIM(H."CardCode"), '')        AS "CCODE",
               COALESCE(TRIM(C."CardName"), '—')      AS "CUST",
               COALESCE(TRIM(I."U_TYPE"), '')          AS "UTYPE",
               H."DocNum"                              AS "DOCNUM",
               SUM(L."OpenQty" * COALESCE(I."SalPackUn", 0)) AS "QTY"
        FROM "{SAP_SCHEMA}"."ORDR" H
        JOIN "{SAP_SCHEMA}"."RDR1" L ON L."DocEntry" = H."DocEntry"
        JOIN "{SAP_SCHEMA}"."OCRD" C ON C."CardCode" = H."CardCode"
        LEFT JOIN "{SAP_SCHEMA}"."OITM" I ON I."ItemCode" = L."ItemCode"
        {_SHIPTO_JOIN.format(S=SAP_SCHEMA)}
        WHERE H."DocStatus" = 'O' AND L."LineStatus" = 'O'
        GROUP BY COALESCE(TRIM(C."U_Main_Group"), '—'), {_SHIPTO_STATE},
                 COALESCE(TRIM(I."U_Sub_Group"), '—'), COALESCE(TRIM(I."{col}"), '—'),
                 COALESCE(TRIM(I."ItemName"), '—'), COALESCE(TRIM(I."ItemCode"), ''),
                 COALESCE(TRIM(H."CardCode"), ''),
                 COALESCE(TRIM(C."CardName"), '—'), COALESCE(TRIM(I."U_TYPE"), ''), H."DocNum"
    '''
    try:
        rows = sap_connector.execute_query(sql)
    except Exception as exc:
        logger.error('[OIH-KPI] dimension rows failed: %s', exc)
        return {'rows': [], 'dims': OIH_BREAKDOWN_DIMS, 'item_stock': {},
                'warehouses': OIH_STOCK_WAREHOUSES, 'error': str(exc)}
    agg = {}
    name_codes = {}   # item name -> set of ItemCodes (for per-item warehouse stock)
    for r in rows or []:
        ut = _normalize_name(r.get('UTYPE'))
        if ut not in ('PREMIUM', 'COMMODITY'):
            continue
        item_name = _normalize_name(r.get('ITEM')) or '—'
        icode = _normalize_name(r.get('ICODE'))
        if icode:
            name_codes.setdefault(item_name, set()).add(icode)
        grp = _normalize_name(r.get('GRP')) or '—'
        st = _delhi_gt_state(r.get('CCODE'), _state_name(r)) or '—'
        so_no = str(r.get('DOCNUM') or '').strip() or '—'
        person = person_for_group_state(grp, st) or '—'   # territory owner for this group+state
        key = (grp, st, _normalize_name(r.get('SUBG')) or '—', _normalize_name(r.get('PACK')) or '—',
               item_name, _normalize_name(r.get('CUST')) or '—', so_no, person)
        cell = agg.setdefault(key, {'premium': 0.0, 'commodity': 0.0})
        cell['premium' if ut == 'PREMIUM' else 'commodity'] += float(r.get('QTY') or 0)
    out = [{'main_group': k[0], 'state': k[1], 'sub_group': k[2], 'packtype': k[3],
            'item': k[4], 'customer': k[5], 'so_no': k[6], 'sales_person': k[7],
            'premium': round(v['premium'], 2), 'commodity': round(v['commodity'], 2)}
           for k, v in agg.items()]
    # On-hand stock per warehouse, keyed by item NAME (each ItemCode counted once),
    # so the window can show GP-FG / BH-EC / BH-PF columns on item rows.
    all_codes = {c for codes in name_codes.values() for c in codes}
    stock = _warehouse_stock_litres(all_codes)
    item_stock = {
        name: [round(sum(stock.get((c, w), 0) for c in codes), 2) for w in OIH_STOCK_WAREHOUSES]
        for name, codes in name_codes.items()
    }
    return {'rows': out, 'dims': OIH_BREAKDOWN_DIMS, 'item_stock': item_stock,
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
    {'key': 'b0_30',   'label': '0 - 30',   'color': '#16a34a'},
    {'key': 'b31_60',  'label': '31 - 60',  'color': '#0d9488'},
    {'key': 'b61_90',  'label': '61 - 90',  'color': '#d97706'},
    {'key': 'b91_120', 'label': '91 - 120', 'color': '#ea580c'},
    {'key': 'b121',    'label': '121+',     'color': '#dc2626'},
]
_BUCKET_KEYS = [b['key'] for b in AGING_BUCKETS]

_aging_cache = {}        # aging-date ISO string → (expires_at, payload)
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
    """Parse the DATA sheet into one dict per customer. Header is row 2, totals row 1,
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


def _load_aging_rows_sap(aging_date):
    """Customer receivables aging as of aging_date via SAP B1's reconciliation logic
    (JDT1 / ITR1 / OITR), translated from B1's own system query to HANA SQL. Returns one
    dict per customer in the same shape as the workbook loader, with Balance Due (ties to
    OCRD.Balance), Original Amount = Σ original posted (Debit − Credit) of the open lines,
    and the five posting-date (RefDate) buckets — matching SAP's report, which ages by
    document/posting date. Parts 1/2 reverse reconciliations dated after the aging
    date to reconstruct the historical open balance; part 3 is the never-reconciled-yet
    open lines."""
    ag, S = _aging_date_literal(aging_date), SAP_SCHEMA
    sql = f'''WITH aged AS (
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
    SELECT C."CardCode" AS "code", C."CardName" AS "name", C."U_Main_Group" AS "format",
           SUM(a.orig) AS "original", SUM(a.bal) AS "balance_due",
           SUM(CASE WHEN a.bdate IS NULL OR DAYS_BETWEEN(a.bdate,{ag})<=30 THEN a.bal ELSE 0 END) AS "b0_30",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 31 AND 60 THEN a.bal ELSE 0 END) AS "b31_60",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 61 AND 90 THEN a.bal ELSE 0 END) AS "b61_90",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 91 AND 120 THEN a.bal ELSE 0 END) AS "b91_120",
           SUM(CASE WHEN DAYS_BETWEEN(a.bdate,{ag})>120 THEN a.bal ELSE 0 END) AS "b121"
    FROM aged a JOIN "{S}"."OCRD" C ON C."CardCode"=a.card
    GROUP BY C."CardCode", C."CardName", C."U_Main_Group"
    HAVING SUM(a.bal)<>0
    ORDER BY SUM(a.bal) DESC'''
    out = []
    for r in sap_connector.execute_query(sql):
        code = str(r.get('code') or '').strip()
        name = str(r.get('name') or '').strip() or code
        fmt = (str(r.get('format')).strip() if r.get('format') else '') or 'Unclassified'
        balance_due = _aging_num(r.get('balance_due'))
        # Hide internal / non-receivable rows: JIVO WELLNESS inter-company branches and
        # FUTURE RETAIL LTD (Modern Trade) by name, and the PURCHASE OIL / EXPORT / TRANSPORT
        # formats. Also drop any customer whose Balance Due nets to exactly 0.00 — tiny but
        # real balances (≥ ₹0.01) still show; only a true zero is hidden. Excluded from rows
        # AND all totals/KPIs (and therefore from the Excel export too).
        _name_u, _fmt_u = name.upper(), fmt.upper()
        if ('JIVO WELLNESS' in _name_u or 'FUTURE RETAIL' in _name_u
                or 'PURCHASE OIL' in _fmt_u or 'EXPORT' in _fmt_u or 'TRANSPORT' in _fmt_u
                or balance_due == 0):
            continue
        out.append({
            'code': code,
            'name': name,
            'format': fmt,
            'original':    _aging_num(r.get('original')),
            'balance_due': balance_due,
            'b0_30':   _aging_num(r.get('b0_30')),
            'b31_60':  _aging_num(r.get('b31_60')),
            'b61_90':  _aging_num(r.get('b61_90')),
            'b91_120': _aging_num(r.get('b91_120')),
            'b121':    _aging_num(r.get('b121')),
        })
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
    bal = total['balance_due'] or 1.0

    kpis = {
        'total_outstanding': total['balance_due'],
        'current': total['b0_30'],
        'current_pct': round(total['b0_30'] / bal * 100, 1),
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
    hit = _aging_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]

    try:
        payload = _build_aging_payload(_load_aging_rows_sap(aging_date))
    except Exception as e:
        logger.exception('[aging] failed to build customer aging payload')
        return {'buckets': AGING_BUCKETS, 'groups': [], 'total': _empty_buckets(),
                'kpis': {}, 'aging_date': key, 'error': str(e)}

    payload['aging_date'] = key
    _aging_cache[key] = (now + _AGING_TTL, payload)
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


def _aging_detail_sql(card_safe, ag, S, with_branch):
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
      WHERE T0."ShortName"='{card_safe}' AND T0."RefDate"<={ag} AND T2."ReconDate">{ag} AND T1."IsCredit"='C'
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
      HAVING MAX(T0."BalFcCred")<>-SUM(T1."ReconSumFC") OR MAX(T0."BalDueCred")<>-SUM(T1."ReconSum")
      UNION ALL
      SELECT T0."ShortName", T0."TransId", T0."Line_ID",
             MAX(T0."RefDate"), MAX(T0."DueDate"), MAX(T0."BaseRef"), MAX(T0."TransType"), {bsel}
             MAX(T0."BalDueDeb")+SUM(T1."ReconSum"), MAX(T0."Debit")
      FROM "{S}"."JDT1" T0
        JOIN "{S}"."ITR1" T1 ON T1."TransId"=T0."TransId" AND T1."TransRowId"=T0."Line_ID"
        JOIN "{S}"."OITR" T2 ON T2."ReconNum"=T1."ReconNum"
      WHERE T0."ShortName"='{card_safe}' AND T0."RefDate"<={ag} AND T2."ReconDate">{ag} AND T1."IsCredit"='D'
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
      HAVING MAX(T0."BalFcDeb")<>-SUM(T1."ReconSumFC") OR MAX(T0."BalDueDeb")<>-SUM(T1."ReconSum")
      UNION ALL
      SELECT T0."ShortName", T0."TransId", T0."Line_ID",
             MAX(T0."RefDate"), MAX(T0."DueDate"), MAX(T0."BaseRef"), MAX(T0."TransType"), {bsel}
             MAX(T0."BalDueDeb")-MAX(T0."BalDueCred"), MAX(T0."Debit")-MAX(T0."Credit")
      FROM "{S}"."JDT1" T0
      WHERE T0."ShortName"='{card_safe}' AND T0."RefDate"<={ag}
        AND (T0."BalDueCred"<>T0."BalDueDeb" OR T0."BalFcCred"<>T0."BalFcDeb")
        AND NOT EXISTS (SELECT 1 FROM "{S}"."ITR1" U0 JOIN "{S}"."OITR" U1 ON U1."ReconNum"=U0."ReconNum"
          WHERE U0."TransId"=T0."TransId" AND U0."TransRowId"=T0."Line_ID" AND U1."ReconDate">{ag})
      GROUP BY T0."TransId", T0."Line_ID", T0."ShortName"
    )
    SELECT a.trans AS "trans", a.line AS "line", a.docno AS "docno", a.ttype AS "ttype",
           a.bdate AS "bdate", a.duedate AS "duedate", a.orig AS "original", a.bal AS "balance_due",
           {bcol}
           CASE WHEN a.bdate IS NULL OR DAYS_BETWEEN(a.bdate,{ag})<=30 THEN a.bal ELSE 0 END AS "b0_30",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 31 AND 60 THEN a.bal ELSE 0 END AS "b31_60",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 61 AND 90 THEN a.bal ELSE 0 END AS "b61_90",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag}) BETWEEN 91 AND 120 THEN a.bal ELSE 0 END AS "b91_120",
           CASE WHEN DAYS_BETWEEN(a.bdate,{ag})>120 THEN a.bal ELSE 0 END AS "b121"
    FROM aged a {bjoin}
    WHERE ABS(a.bal) > 0.005
    ORDER BY a.duedate'''


def get_customer_aging_detail(card_code, aging_date=None):
    """Per-document open items for one customer as of aging_date, with saved remarks merged
    in. row_key ('TransId:Line_ID') ties each row to its stored remark. [] on SAP error."""
    if aging_date is None:
        aging_date = date.today()
    ag = _aging_date_literal(aging_date)
    card_safe = (card_code or '').strip().replace("'", "''")
    if not card_safe:
        return []
    rows = None
    for with_branch in (True, False):     # retry without the branch join if it errors
        try:
            rows = sap_connector.execute_query(_aging_detail_sql(card_safe, ag, SAP_SCHEMA, with_branch))
            break
        except Exception as exc:
            logger.error('[AGINGDETAIL] fetch failed (branch=%s): %s', with_branch, exc)
            rows = None
    if rows is None:
        return []
    remarks = get_aging_remarks(card_code)
    splits = get_aging_remark_lines(card_code)
    out = []
    for r in rows:
        try:
            ttype = int(r.get('ttype'))
        except (TypeError, ValueError):
            ttype = None
        row_key = '%s:%s' % (str(r.get('trans') or '').strip(), str(r.get('line') or '').strip())
        out.append({
            'row_key': row_key,
            'doc_no': str(r.get('docno') or '').strip(),
            'type': _AGING_TYPE_MAP.get(ttype, (str(r.get('ttype')).strip() if r.get('ttype') is not None else '')),
            'posting_date': _aging_fmt_date(r.get('bdate')),
            'due_date': _aging_fmt_date(r.get('duedate')),
            'branch': str(r.get('branch') or '').strip(),
            'original': _aging_num(r.get('original')),
            'balance_due': _aging_num(r.get('balance_due')),
            'remark': remarks.get(row_key, ''),
            'splits': splits.get(row_key, []),
            'b0_30': _aging_num(r.get('b0_30')),
            'b31_60': _aging_num(r.get('b31_60')),
            'b61_90': _aging_num(r.get('b61_90')),
            'b91_120': _aging_num(r.get('b91_120')),
            'b121': _aging_num(r.get('b121')),
        })
    return out


def get_aging_remarks(card_code):
    """{row_key: remark} of saved per-document remarks for a customer."""
    cc = (card_code or '').strip()
    if not cc:
        return {}
    return {a.row_key: a.remark for a in AgingRemark.objects.filter(card_code=cc)}


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
        if category and category not in _AGING_REMARK_CATEGORY_SET:
            category = ''       # only the fixed dropdown vocabulary persists; drop anything else
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


def bulk_update_aging_remarks(card_code, aging_date, doc_remarks):
    """Apply an uploaded {Doc No → Remark} set to a customer's open documents, matching on
    Doc No (JDT1.BaseRef) as of aging_date. A Doc No that spans several open lines updates
    every matching line. Blank remarks are skipped (left unchanged), so a partial sheet only
    sets what it fills. Returns a summary: rows read, distinct docs matched, lines updated,
    and the Doc Nos that didn't match any open document."""
    cc = (card_code or '').strip()
    rows_in = list(doc_remarks or [])
    if not cc:
        return {'rows': len(rows_in), 'matched_docs': 0, 'updated': 0, 'unmatched': []}
    by_doc = {}
    for r in get_customer_aging_detail(cc, aging_date):
        by_doc.setdefault(str(r.get('doc_no') or '').strip(), []).append(r.get('row_key'))
    matched, updated, unmatched = set(), 0, []
    for doc, remark in rows_in:
        doc = str(doc or '').strip()
        remark = (remark or '').strip()
        if not doc or not remark:           # blank remark → leave the existing note untouched
            continue
        keys = by_doc.get(doc)
        if not keys:
            unmatched.append(doc)
            continue
        matched.add(doc)
        for rk in keys:
            if save_aging_remark(cc, rk, remark):
                updated += 1
    # de-dup unmatched, keep order, cap for the response
    seen, uniq = set(), []
    for d in unmatched:
        if d not in seen:
            seen.add(d); uniq.append(d)
    return {'rows': len(rows_in), 'matched_docs': len(matched), 'updated': updated, 'unmatched': uniq[:50]}
