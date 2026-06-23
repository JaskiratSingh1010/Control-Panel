"""Production feasibility (BOM availability) — for a finished-good item + planned qty,
explode its Bill of Materials from SAP B1 and compare each component's REQUIRED quantity
against current OnHand stock.

SAP B1 tables used (schema JIVO_OIL_HANADB):
  OITT  — BOM header   : "Code" = FG item, "Quantity" = base qty the tree is defined for
  ITT1  — BOM rows     : "Father" = FG, "Code" = component, "Quantity" = qty per base, "Warehouse"
  OITM  — item master  : "ItemCode", "ItemName", "InvntryUom"   (resources are NOT here)
  OITW  — wh stock     : "ItemCode", "WhsCode", "OnHand"

Labour/machine RESOURCES (e.g. JWPL09240 FILLING COST) live in ORSC, not OITM — so the
INNER JOIN to OITM naturally drops them (per requirement: skip resources). Availability =
OnHand only (no committed/on-order netting).
"""
import logging

from core import sap_connector

logger = logging.getLogger(__name__)
SAP_SCHEMA = 'JIVO_OIL_HANADB'


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def get_plan_feasibility(items, warehouses=None):
    """Aggregated production plan: items = [{'fg_code', 'qty'}, ...]. Explodes every FG's
    BOM and SUMS each material's requirement across all FGs (shared RM/PM merged), then
    compares the total required vs the material's Available — one combined material list.
    `warehouses` is passed through to each FG (see get_bom_feasibility)."""
    plan_items = []
    agg = {}   # (component code, warehouse) -> merged material row
    for it in (items or []):
        fg = get_bom_feasibility(it.get('fg_code'), it.get('qty'), warehouses=warehouses)
        plan_items.append({'fg_code': fg['fg_code'], 'fg_name': fg['fg_name'],
                           'qty': fg['planned_qty'], 'found': fg['found']})
        if not fg['found']:
            continue
        for c in fg['components']:
            key = (c['code'], c['warehouse'])
            m = agg.get(key)
            if m is None:
                m = agg[key] = {
                    'code': c['code'], 'name': c['name'], 'kind': c['kind'], 'uom': c['uom'],
                    'warehouse': c['warehouse'], 'onhand': c['onhand'], 'available': c['available'],
                    'all_wh': c['all_wh'], 'warehouses': c['warehouses'], 'required': 0.0, 'used_in': [],
                }
            m['required'] += c['required']
            if fg['fg_code'] not in m['used_in']:
                m['used_in'].append(fg['fg_code'])

    materials = []
    feasible = True
    short_count = 0
    for m in agg.values():
        m['required'] = round(m['required'], 2)
        m['balance'] = round(m['available'] - m['required'], 2)
        m['short'] = m['balance'] < -0.0001
        m['elsewhere'] = m['short'] and m['all_wh'] >= m['required']
        if m['short']:
            feasible = False
            short_count += 1
        materials.append(m)
    materials.sort(key=lambda x: (not x['short'], x['kind'], x['code']))
    return {'items': plan_items, 'materials': materials,
            'feasible': feasible and bool(materials), 'short_count': short_count,
            'material_count': len(materials)}


def get_warehouses():
    """Active, stock-holding warehouses (code + name) for the warehouse-selection control."""
    rows = sap_connector.execute_query(
        'SELECT o."WhsCode" AS CODE, o."WhsName" AS NAME '
        'FROM "%s"."OWHS" o '
        'WHERE EXISTS (SELECT 1 FROM "%s"."OITW" w WHERE w."WhsCode" = o."WhsCode" AND w."OnHand" <> 0) '
        'ORDER BY o."WhsCode"' % (SAP_SCHEMA, SAP_SCHEMA))
    return [{'code': r['CODE'], 'name': r['NAME'] or r['CODE']} for r in rows]


def get_fg_list():
    """All producible finished goods (have a BOM) with their classification, for the
    drill-down picker: Type → Sub-group → Variety → SKU → item."""
    rows = sap_connector.execute_query(
        'SELECT m."ItemCode" AS CODE, m."ItemName" AS NAME, '
        '  COALESCE(m."U_TYPE", \'\') AS TYP, COALESCE(m."U_Sub_Group", \'\') AS SUBG, '
        '  COALESCE(m."U_Variety", \'\') AS VAR, COALESCE(m."U_SKU", \'\') AS SKU '
        'FROM "%s"."OITM" m '
        'WHERE m."ItmsGrpCod" = 102 AND m."ItemCode" IN (SELECT "Code" FROM "%s"."OITT") '
        'ORDER BY m."U_TYPE", m."U_Sub_Group", m."U_Variety", m."U_SKU", m."ItemName"'
        % (SAP_SCHEMA, SAP_SCHEMA))
    out = []
    for r in rows:
        out.append({
            'code': r['CODE'], 'name': r['NAME'] or r['CODE'],
            'type': (r['TYP'] or '').strip().upper() or 'OTHERS',
            'sub_group': (r['SUBG'] or '').strip().upper() or '—',
            'variety': (r['VAR'] or '').strip().upper() or '—',
            'sku': (r['SKU'] or '').strip().upper() or '—',
        })
    return out


def get_bom_feasibility(fg_code, planned_qty, warehouses=None):
    """Return {fg_code, fg_name, planned_qty, found, feasible, max_fg, short_count,
    components[]} where each component carries required vs available, balance and the max FG
    that component alone allows.

    `warehouses`: None  → Available = SAP production-order value (OnHand+OnOrder−Committed)
                          for the component's BOM warehouse.
                  ['ALL']→ Available = total OnHand across every warehouse.
                  [codes]→ Available = sum of OnHand across the selected warehouses only."""
    fg_code = (fg_code or '').strip().upper()
    planned_qty = _num(planned_qty)
    wh_sel = set(str(w).strip().upper() for w in warehouses if str(w).strip()) if warehouses else None
    all_mode = bool(wh_sel) and 'ALL' in wh_sel
    result = {'fg_code': fg_code, 'fg_name': '', 'planned_qty': planned_qty,
              'fg_onhand': 0, 'fg_uom': '',
              'found': False, 'feasible': False, 'max_fg': 0, 'short_count': 0, 'components': []}
    if not fg_code:
        return result

    # FG master: name, inventory UoM, and the finished product's CURRENT stock — how much
    # ready maal is already on hand (OnHand summed across all warehouses).
    fg_info = sap_connector.execute_query(
        'SELECT m."ItemName" AS NAME, m."InvntryUom" AS UOM, '
        '  COALESCE((SELECT SUM(w."OnHand") FROM "%s"."OITW" w WHERE w."ItemCode" = m."ItemCode"), 0) AS ONHAND '
        'FROM "%s"."OITM" m WHERE m."ItemCode" = ?' % (SAP_SCHEMA, SAP_SCHEMA), (fg_code,))
    if fg_info:
        result['fg_name'] = fg_info[0]['NAME'] or fg_code
        result['fg_uom'] = fg_info[0]['UOM'] or ''
        result['fg_onhand'] = _num(fg_info[0]['ONHAND'])

    # BOM header — base quantity the tree is defined for (SAP B1 typo column: "Qauntity").
    hdr = sap_connector.execute_query(
        'SELECT t."Qauntity" AS BASE FROM "%s"."OITT" t WHERE t."Code" = ?' % SAP_SCHEMA, (fg_code,))
    if not hdr:
        return result   # FG has no BOM (name + current stock already set above)
    base = _num(hdr[0]['BASE']) or 1.0
    if base <= 0:
        base = 1.0
    result['found'] = True

    # Components — INNER JOIN OITM keeps stock items only (resources excluded).
    # "Available" matches SAP's production-order Available column EXACTLY:
    #   OnHand + OnOrder (incoming) - IsCommited (reserved)  — for the component's warehouse.
    rows = sap_connector.execute_query(
        'SELECT t1."Code" AS CODE, m."ItemName" AS NAME, LEFT(t1."Code", 2) AS GRP, '
        '       t1."Quantity" AS QTY, m."InvntryUom" AS UOM, t1."Warehouse" AS WH, '
        '       COALESCE(w."OnHand", 0) AS ONHAND, '
        '       COALESCE(w."OnHand", 0) + COALESCE(w."OnOrder", 0) - COALESCE(w."IsCommited", 0) AS AVAIL, '
        '       COALESCE((SELECT SUM(w2."OnHand") FROM "%s"."OITW" w2 WHERE w2."ItemCode" = t1."Code"), 0) AS ALLWH '
        'FROM "%s"."ITT1" t1 '
        'JOIN "%s"."OITM" m ON m."ItemCode" = t1."Code" '
        'LEFT JOIN "%s"."OITW" w ON w."ItemCode" = t1."Code" AND w."WhsCode" = t1."Warehouse" '
        'WHERE t1."Father" = ? '
        'ORDER BY t1."ChildNum"' % (SAP_SCHEMA, SAP_SCHEMA, SAP_SCHEMA, SAP_SCHEMA), (fg_code,))

    # Per-warehouse OnHand breakdown for every component (for the Warehouses pills).
    wh_rows = sap_connector.execute_query(
        'SELECT w."ItemCode" AS CODE, w."WhsCode" AS WH, w."OnHand" AS OH '
        'FROM "%s"."OITW" w '
        'WHERE w."ItemCode" IN (SELECT "Code" FROM "%s"."ITT1" WHERE "Father" = ?) '
        '  AND w."OnHand" <> 0 '
        'ORDER BY w."OnHand" DESC' % (SAP_SCHEMA, SAP_SCHEMA), (fg_code,))
    wh_map = {}
    for w in wh_rows:
        wh_map.setdefault(w['CODE'], []).append({'wh': w['WH'], 'qty': round(_num(w['OH']), 2)})

    feasible = True
    max_fg = None
    comps = []
    for r in rows:
        per = _num(r['QTY']) / base              # consumption per 1 FG
        required = per * planned_qty
        onhand = _num(r['ONHAND'])
        all_wh = _num(r['ALLWH'])                # total OnHand across ALL warehouses
        whs = wh_map.get(r['CODE'], [])
        if wh_sel:
            # Available = OnHand summed across the SELECTED warehouses (or all).
            available = all_wh if all_mode else sum(w['qty'] for w in whs if w['wh'].upper() in wh_sel)
        else:
            available = _num(r['AVAIL'])         # SAP "Available" for the BOM warehouse
        balance = available - required
        grp = (r['GRP'] or '').upper()
        kind = grp if grp in ('RM', 'PM', 'FG') else 'OTHER'
        item_max = (available / per) if per > 0 else None   # max FG this item alone allows
        short = balance < -0.0001
        # short in the BOM warehouse, but the material exists elsewhere → a transfer could fix it
        elsewhere = short and all_wh >= required
        if short:
            feasible = False
        if item_max is not None:
            max_fg = item_max if max_fg is None else min(max_fg, item_max)
        comps.append({
            'code': r['CODE'], 'name': r['NAME'], 'kind': kind, 'uom': r['UOM'] or '',
            'warehouse': r['WH'] or '', 'per_fg': round(per, 5), 'required': round(required, 2),
            'onhand': round(onhand, 2), 'available': round(available, 2), 'all_wh': round(all_wh, 2),
            'balance': round(balance, 2),
            'max_fg': int(item_max) if item_max is not None else None,
            'short': short, 'elsewhere': elsewhere,
            'warehouses': whs,
        })

    result['components'] = comps
    result['feasible'] = feasible and bool(comps)
    result['max_fg'] = int(max_fg) if max_fg is not None else 0
    result['short_count'] = sum(1 for c in comps if c['short'])
    return result
