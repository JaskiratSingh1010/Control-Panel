import logging
import time
from decimal import Decimal
from datetime import date, datetime, timedelta

from django.utils import timezone

from core import sap_connector
from .models import MainGroupMaster, MonthlyTarget, SegmentTarget, StateMaster, TargetMaster, TargetNode

logger = logging.getLogger(__name__)

SAP_SCHEMA = 'JIVO_OIL_HANADB'
SAP_PROC   = 'REPORT_SALES_ANALYSIS'

ALLOWED_SUB_GROUPS = {
    'BLENDED', 'COTTON SEED', 'MUSTARD', 'RICE BRAN', 'SLICED OLIVE',
    'SOYABEAN', 'SUNFLOWER', 'CANOLA', 'COCONUT', 'EXTRA VIRGIN OLIVE',
    'GHEE', 'GROUNDNUT', 'OLIVE', 'SESAME', 'YELLOW MUSTARD',
}

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
        return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.error('[SAP] Procedure call failed: %s', e)
        return []


# ── Beverages dataset (separate HANA schema) ──────────────────────────────
# Sales pulled with a direct invoice query (OINV/INV1) rather than a proc. Shape:
# Variety / Sub-Group / Brand / Main Group / Chain / State dimensions, PCS_Sold (qty)
# and Boxes_Sold (boxes) metrics, by customer & month.
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
        sales_person = _normalize_name(_bev_pick(r, 'SalesPerson', 'SALESPERSON', 'SlpName', 'sales_person')) or '—'
        qty = _bev_num(_bev_pick(r, 'PCS_Sold', 'PCS_SOLD', 'Quantity', 'QUANTITY', 'Qty', 'quantity'))
        box = _bev_num(_bev_pick(r, 'Boxes_Sold', 'BOXES_SOLD', 'Box', 'BOX', 'Boxes', 'box'))
        dd = _bev_date(_bev_pick(r, 'DocDate', 'DOCDATE', 'Doc_Date', 'doc_date'))
        ym, mlabel = _bev_month_key(r, dd)
        ymk = ym or ''   # carried on each row so the client can filter to a single month
        key = (variety, sub, sku, item, main_group, state, brand, chain, sales_person, ymk)
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
        sales_person = _normalize_name(_bev_pick(r, 'SalesPerson', 'SALESPERSON', 'SlpName', 'sales_person')) or '—'
        opcs = _bev_num(_bev_pick(r, 'PCS_Ordered', 'PCS_ORDERED', 'PCS_Sold', 'Quantity', 'Qty'))
        obox = _bev_num(_bev_pick(r, 'Boxes_Ordered', 'BOXES_ORDERED', 'Boxes_Sold', 'Boxes', 'Box'))
        ym, _ml = _bev_month_key(r, _bev_date(_bev_pick(r, 'DocDate', 'DOCDATE', 'Doc_Date', 'doc_date')))
        ymk = ym or ''
        mk = (variety, sub, sku, item, main_group, state, brand, chain, sales_person, ymk)
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
                     'sales_person': k[8], 'ym': k[9],
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


# Short-lived in-process cache for the expensive REPORT_SALES_ANALYSIS call.
# Both slides (and quick re-fetches / hard refreshes / other users) hitting the same
# date range within the TTL reuse one SAP round-trip instead of re-querying HANA.
_SALES_CACHE = {}          # 'start|end' -> (expires_at, (result, raw_rows))
_SALES_CACHE_TTL = 90      # seconds


def get_sales_data_cached(start_date, end_date):
    key = f'{start_date}|{end_date}'
    now = time.time()
    hit = _SALES_CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = get_sales_data(start_date, end_date)
    if value and value[1]:                 # only cache successful, non-empty pulls
        _SALES_CACHE[key] = (now + _SALES_CACHE_TTL, value)
        # Drop any other expired entries so the dict can't grow unbounded.
        for k in [k for k, v in _SALES_CACHE.items() if v[0] <= now]:
            _SALES_CACHE.pop(k, None)
    return value


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
        for node in node_rows:
            name = _normalize_name(node.main_group)
            if not name:
                continue
            grouped[name] = grouped.get(name, Decimal('0')) + (node.target_ltrs or Decimal('0'))
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
    'JK': 'JAMMU & KASHMIR', 'KR': 'KERALA', 'AS': 'ASSAM', 'TN': 'TAMIL NADU',
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


def person_for_group_state(group, state_name):
    """Territory owner for a (group, state); falls back to the channel-level owner
    for national channels (E-Commerce, CSD) that have no per-state assignment."""
    group = _normalize_name(group)
    person = _PERSON_BY_GROUP_STATE.get((group, _normalize_name(state_name)))
    return person or CHANNEL_OWNERS.get(group, '')


def complete_target_triple(group, state, person):
    """Fill a blank group/person from the territory sheet when the other two
    fields identify exactly one cell. Lets a target keep its full (group, state,
    person) identity no matter which drill order was used to enter it."""
    group = _normalize_name(group)
    state = _normalize_name(state)
    person = _normalize_name(person)
    if state and person and not group:
        group = _GROUP_BY_STATE_PERSON.get((state, person), group)
    if group and state and not person:
        person = _PERSON_BY_GROUP_STATE.get((group, state), person)
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
    seen, ordered = set(), []
    for (_g, _c, _n, person) in TERRITORY_SHEET:
        if person not in seen:
            seen.add(person)
            ordered.append(person)
    for person in CHANNEL_OWNERS.values():
        if person not in seen:
            seen.add(person)
            ordered.append(person)
    return ordered


# REST-segment groups (no person/state owner) — targetable at group level in the
# editor. HORECA is omitted here because it already appears via the territory sheet.
REST_GROUPS = ['CSD', 'E-COMMERCE', 'CASH SALE', 'CORPORATE', 'SANGAT',
               'BRANCH', 'STAFF', 'REFERENCE', 'PURCHASE OIL']


def get_territory_master_rows():
    """Editor rows built straight from the territory sheet (one row per
    group+state+person), plus group-level rows for the REST segment so those
    channels can be targeted too. Independent of live OCRD data."""
    rows, seen = [], set()
    for (group, _code, name, person) in TERRITORY_SHEET:
        key = (group, name, person)
        if key in seen:
            continue
        seen.add(key)
        rows.append({'main_group': group, 'state': name, 'sales_person': person})
    for group in REST_GROUPS:
        # Channels with a named owner (E-Commerce → Prabhu Sir, CSD → Sachin Stephen)
        # carry that person so they show up in the Update Targets person list and a
        # target entered for them resolves back to the channel's main group.
        rows.append({'main_group': group, 'state': '', 'sales_person': CHANNEL_OWNERS.get(group, '')})
    return rows


def get_territory_dashboard_payload():
    """Mapping the dashboard JS uses to remap live sales/orders onto persons and
    to fix the GT/MT channel state lists. Single source = TERRITORY_SHEET."""
    person_map = {}   # "GROUP|STATENAME" -> person
    whitelist = {}    # channel -> [{label, match[]}]
    for (group, code, name, person) in TERRITORY_SHEET:
        person_map[group + '|' + name] = person
        if group in ('GT', 'MT'):
            bucket = whitelist.setdefault(group, [])
            if not any(e['label'] == name for e in bucket):
                bucket.append({'label': name, 'match': [name, code]})
    return {
        'persons': _assigned_persons_in_order(),
        'map': person_map,
        'whitelist': whitelist,
        'group_owners': dict(CHANNEL_OWNERS),
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


def get_order_in_hand_by_person():
    """Open-order LITRES per territory owner (assigned only). Live snapshot."""
    data = {p: 0.0 for p in _assigned_persons_in_order()}
    for (group, code), qty in _open_order_qty_by_group_code().items():
        person = _PERSON_ASSIGNMENTS.get((group, code)) or CHANNEL_OWNERS.get(group)
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
        state_name = _state_name(d)
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


# State & City come from the order's ship-to address (CRD1 via ShipToCode); when an
# order has no ship-to address we fall back to the BP-master OCRD.State1/City.
_SHIPTO_STATE = "COALESCE(NULLIF(TRIM(A.\"State\"), ''), TRIM(C.\"State1\"))"
_SHIPTO_CITY = "COALESCE(NULLIF(TRIM(A.\"City\"), ''), TRIM(C.\"City\"))"
_SHIPTO_JOIN = ('LEFT JOIN "{S}"."CRD1" A ON A."CardCode" = H."CardCode" '
                'AND A."Address" = H."ShipToCode" AND A."AdresType" = \'S\'')


_DONE_LINE_SQL = '''
    SELECT H."DocNum" AS "DOCNUM", H."DocDate" AS "DOCDATE",
           COALESCE(TRIM(C."U_Main_Group"), '') AS "GRP",
           ''' + _SHIPTO_STATE + ''' AS "ST",
           ''' + _SHIPTO_CITY + ''' AS "CITY",
           COALESCE(TRIM(C."CardName"), '')     AS "CUST",
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
    sql = (f'SELECT "DOCNUM","DOCDATE","GRP","ST","CUST","CITY","SUBG","ITEM","ICODE","UTYPE", '
           f'SUM("LIT") AS "LIT" FROM ( {inv} UNION ALL {crd} ) T '
           f'GROUP BY "DOCNUM","DOCDATE","GRP","ST","CUST","CITY","SUBG","ITEM","ICODE","UTYPE"')
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
        state_name = _state_name(row)
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
           COALESCE(TRIM(C."CardName"), '')     AS "CUST",
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
        state_name = _state_name(row)
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
                                'city': _normalize_name(row.get('CITY')), 'litres': 0.0, '_items': {}}
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
            'state': _state_name(row),
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
               COALESCE(TRIM(C."CardName"), '—')      AS "CUST",
               COALESCE(TRIM(I."U_TYPE"), '')          AS "UTYPE",
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
                 COALESCE(TRIM(C."CardName"), '—'), COALESCE(TRIM(I."U_TYPE"), '')
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
        key = (_normalize_name(r.get('GRP')) or '—', _state_name(r) or '—',
               _normalize_name(r.get('SUBG')) or '—', _normalize_name(r.get('PACK')) or '—',
               item_name, _normalize_name(r.get('CUST')) or '—')
        cell = agg.setdefault(key, {'premium': 0.0, 'commodity': 0.0})
        cell['premium' if ut == 'PREMIUM' else 'commodity'] += float(r.get('QTY') or 0)
    out = [{'main_group': k[0], 'state': k[1], 'sub_group': k[2], 'packtype': k[3],
            'item': k[4], 'customer': k[5],
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
