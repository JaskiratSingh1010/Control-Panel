from .shared import FG_VALID, GIFT_EXCL, PM_VALID, RM_VALID, cf, q, safe, tf, wf

SCHEMAS = {
    "jivo_oil": "JIVO_OIL_HANADB",
    "jivo_mart": "JIVO_MART_HANADB",
    "jivo_beverages": "JIVO_BEVERAGES_HANADB",
}


def get_schema(s):
    return SCHEMAS.get(s, "JIVO_OIL_HANADB")


def JSONResponse(content):
    return content.get("data", content)
# Owner join helper — handles int/varchar type mismatch
OWN_JOIN = 'LEFT JOIN {db}.OUSR U ON CAST({tbl}."U_Owner" AS VARCHAR(20))=CAST(U."USERID" AS VARCHAR(20))'

# Stock Available — finished-goods (ItmsGrpCod 102) stock by item, pivoted across these
# warehouses (display order), classified into the canonical 16 Realise products.
STOCK_WAREHOUSES = ['GP-FG', 'BH-FG', 'BH-PF', 'BH-EC', 'BH-FU', 'BH-BT']
# Jivo Mart carries its stock in different warehouses — same six plus these two (mart only).
MART_STOCK_WAREHOUSES = STOCK_WAREHOUSES + ['GP-FGM', 'DL-MP']


def get_stock_available(schema="jivo_oil"):
    # Jivo Beverages is a separate SAP company whose finished goods use a two-level
    # DRINKS/WATER → variety taxonomy (not the 16 oil products), so it gets its own path.
    if schema == "jivo_beverages":
        return _beverages_stock(get_schema(schema))

    # Lazy import keeps the product taxonomy (the 16 cards + reclassification) in one
    # place — Realise — without a load-time dependency between the apps.
    from realise.services import _reclassify, ALLOWED_SUB_GROUPS, DEFAULT_TARGETS

    db = get_schema(schema)
    whs = MART_STOCK_WAREHOUSES if schema == "jivo_mart" else STOCK_WAREHOUSES
    whs_in = ",".join("'%s'" % w for w in whs)
    rows = q(f"""SELECT
        I."ItemCode" AS "ItemCode", I."ItemName" AS "ItemName", O."Warehouse" AS "Warehouse",
        I."U_SKU" AS "U_SKU", I."U_Sub_Group" AS "U_Sub_Group", I."U_Variety" AS "U_Variety",
        I."U_TYPE" AS "U_TYPE", MAX(I."SalFactor2") AS "SalFactor2",
        SUM(O."InQty" - O."OutQty") AS "Qty",
        CASE WHEN I."U_IsLitre" = 'Y' THEN SUM(O."InQty" - O."OutQty") * I."SalPackUn" ELSE 0 END AS "Litres"
    FROM {db}.OINM O
    INNER JOIN {db}.OITM I ON I."ItemCode" = O."ItemCode"
    WHERE I."ItmsGrpCod" = 102 AND O."Warehouse" IN ({whs_in})
    GROUP BY I."ItemCode", I."ItemName", I."SalPackUn", O."Warehouse",
             I."U_SKU", I."U_Sub_Group", I."U_Variety", I."U_IsLitre", I."U_TYPE"
    HAVING SUM(O."InQty" - O."OutQty") <> 0
    ORDER BY I."U_Sub_Group", I."U_Variety", I."ItemName" """)

    items = {}
    for r in rows:
        code = r.get("ItemCode")
        name = str(r.get("ItemName") or "").strip()
        ctype, csub = _reclassify(
            str(r.get("U_TYPE") or "").strip().upper(),
            str(r.get("U_Sub_Group") or "").strip().upper(),
            name.upper(),
        )
        if ctype not in ("PREMIUM", "COMMODITY") or csub not in ALLOWED_SUB_GROUPS:
            continue
        it = items.get(code)
        if it is None:
            it = items[code] = {
                "type": ctype, "sub_group": csub,
                "variety": str(r.get("U_Variety") or "").strip(),
                "item_code": code, "item_name": name,
                "sku": str(r.get("U_SKU") or "").strip(),
                "wh": {w: 0.0 for w in whs},
                "wh_litres": {w: 0.0 for w in whs},
                "grand_total": 0.0, "litres": 0.0,
                "pcs_per_box": float(r.get("SalFactor2") or 0),   # for Boxes = pieces / pcs_per_box
            }
        wcode = str(r.get("Warehouse") or "").strip().upper()
        qty = float(r.get("Qty") or 0)
        lit = float(r.get("Litres") or 0)
        if wcode in it["wh"]:
            it["wh"][wcode] += qty
            it["wh_litres"][wcode] += lit
        it["grand_total"] += qty
        it["litres"] += lit

    item_list = list(items.values())
    for it in item_list:
        it["wh"] = {w: round(v, 2) for w, v in it["wh"].items()}
        it["wh_litres"] = {w: round(v, 2) for w, v in it["wh_litres"].items()}
        it["grand_total"] = round(it["grand_total"], 2)
        it["litres"] = round(it["litres"], 2)

    # All 16 canonical products always get a card (even at zero stock).
    products = {}
    for key in DEFAULT_TARGETS:
        ptype, psub = key.split("|", 1)
        products[(ptype, psub)] = {"type": ptype, "sub_group": psub, "qty": 0.0, "litres": 0.0, "sku_count": 0}
    for it in item_list:
        p = products.get((it["type"], it["sub_group"]))
        if p is None:
            p = products[(it["type"], it["sub_group"])] = {
                "type": it["type"], "sub_group": it["sub_group"], "qty": 0.0, "litres": 0.0, "sku_count": 0}
        p["qty"] += it["grand_total"]
        p["litres"] += it["litres"]
        p["sku_count"] += 1
    product_list = []
    for p in products.values():
        p["qty"] = round(p["qty"], 2)
        p["litres"] = round(p["litres"], 2)
        product_list.append(p)
    # Premium block first, then Commodity; within each, most stock first.
    product_list.sort(key=lambda p: (0 if p["type"] == "PREMIUM" else 1, -p["qty"], p["sub_group"]))

    return {"warehouses": whs, "products": product_list, "items": item_list}


def _beverages_stock(db):
    """Stock Available for the Jivo Beverages company (JIVO_BEVERAGES_HANADB).

    Same In−Out net-stock method as the oil view, but beverages have their own
    two-level taxonomy: U_Sub_Group (DRINKS / WATER / …) is the badge/type and
    U_Variety (JEERA / SODA / MINERAL WATER / …) is the product card. All the FG
    stock in this company is U_Unit='BEVERAGES' (the OIL/FOODS masters carry none),
    but the filter is kept explicit so it stays "beverages only" if that changes.
    Warehouses are derived from the data (stock spreads across ~15 godowns), ordered
    by stock held so the busiest columns come first.

    The headline metric is BOXES (net pieces ÷ SalFactor2, the pcs-per-box), matching
    the Realise beverages view — not litres like oils. It is carried in the shared
    'litres'/'wh_litres' slots the template/export render, with unit flags on the
    payload telling the UI to label them "Boxes".
    """
    rows = q(f"""SELECT
        I."ItemCode" AS "ItemCode", I."ItemName" AS "ItemName", O."Warehouse" AS "Warehouse",
        I."U_SKU" AS "U_SKU", I."U_Sub_Group" AS "U_Sub_Group", I."U_Variety" AS "U_Variety",
        SUM(O."InQty" - O."OutQty") AS "Qty",
        CASE WHEN I."SalFactor2" > 0 THEN SUM(O."InQty" - O."OutQty") / I."SalFactor2" ELSE 0 END AS "Boxes"
    FROM {db}.OINM O
    INNER JOIN {db}.OITM I ON I."ItemCode" = O."ItemCode"
    INNER JOIN {db}.OITB G ON I."ItmsGrpCod" = G."ItmsGrpCod"
    WHERE G."ItmsGrpNam" = 'FINISHED' AND I."U_Unit" = 'BEVERAGES'
    GROUP BY I."ItemCode", I."ItemName", I."SalFactor2", O."Warehouse",
             I."U_SKU", I."U_Sub_Group", I."U_Variety"
    HAVING SUM(O."InQty" - O."OutQty") <> 0
    ORDER BY I."U_Sub_Group", I."U_Variety", I."ItemName" """)

    items = {}
    whs_seen = []
    for r in rows:
        code = r.get("ItemCode")
        name = str(r.get("ItemName") or "").strip()
        # DRINKS/WATER/… is the type (badge); the variety (JEERA/SODA/…) is the card.
        ctype = str(r.get("U_Sub_Group") or "").strip().upper() or "OTHER"
        variety = str(r.get("U_Variety") or "").strip()
        csub = variety.upper() or ctype
        wcode = str(r.get("Warehouse") or "").strip().upper()
        if wcode and wcode not in whs_seen:
            whs_seen.append(wcode)
        it = items.get(code)
        if it is None:
            it = items[code] = {
                "type": ctype, "sub_group": csub, "variety": variety,
                "item_code": code, "item_name": name,
                "sku": str(r.get("U_SKU") or "").strip(),
                "wh": {}, "wh_litres": {}, "grand_total": 0.0, "litres": 0.0,
            }
        qty = float(r.get("Qty") or 0)
        box = float(r.get("Boxes") or 0)   # headline metric for beverages (see docstring)
        if wcode:
            it["wh"][wcode] = it["wh"].get(wcode, 0.0) + qty
            it["wh_litres"][wcode] = it["wh_litres"].get(wcode, 0.0) + box
        it["grand_total"] += qty
        it["litres"] += box

    item_list = list(items.values())

    # Warehouse columns ordered by total stock held (busiest first).
    wh_totals = {}
    for it in item_list:
        for w, v in it["wh"].items():
            wh_totals[w] = wh_totals.get(w, 0.0) + (v or 0.0)
    warehouses = sorted(whs_seen, key=lambda w: -wh_totals.get(w, 0.0))

    for it in item_list:
        it["wh"] = {w: round(it["wh"].get(w, 0.0), 2) for w in warehouses}
        it["wh_litres"] = {w: round(it["wh_litres"].get(w, 0.0), 2) for w in warehouses}
        it["grand_total"] = round(it["grand_total"], 2)
        it["litres"] = round(it["litres"], 2)

    # One card per (type, variety); cluster same-type cards, biggest first within a type.
    type_litres = {}
    products = {}
    for it in item_list:
        key = (it["type"], it["sub_group"])
        p = products.get(key)
        if p is None:
            p = products[key] = {
                "type": it["type"], "sub_group": it["sub_group"],
                "qty": 0.0, "litres": 0.0, "sku_count": 0}
        p["qty"] += it["grand_total"]
        p["litres"] += it["litres"]
        p["sku_count"] += 1
        type_litres[it["type"]] = type_litres.get(it["type"], 0.0) + it["litres"]

    product_list = []
    for p in products.values():
        p["qty"] = round(p["qty"], 2)
        p["litres"] = round(p["litres"], 2)
        product_list.append(p)
    product_list.sort(key=lambda p: (-type_litres.get(p["type"], 0.0), p["type"], -p["qty"], p["sub_group"]))

    # unit/unit_label tell the UI to render the headline metric as Boxes, not Litres.
    return {"warehouses": warehouses, "products": product_list, "items": item_list,
            "unit": "Box", "unit_label": "Boxes"}


def get_kpi(category=None,schema="jivo_oil",whs=None):
    db=get_schema(schema);f=cf(category);wf_=wf(whs)
    return JSONResponse(content={"data":q(f"""SELECT
    ROUND((SELECT SUM(W."OnHand") FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod" WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {wf_} {GIFT_EXCL.replace('M.','M.')}),0) AS "TotalQty",
    ROUND((SELECT SUM(W."OnHand"*M."LastPurPrc") FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod" WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {wf_} {GIFT_EXCL}),0) AS "TotalValue",
    (SELECT COUNT(DISTINCT W."ItemCode") FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod" WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {wf_} {GIFT_EXCL}) AS "TotalSKUs",
    (SELECT COUNT(*) FROM (SELECT M."ItemCode" FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod" WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} {wf_} {GIFT_EXCL} GROUP BY M."ItemCode" HAVING SUM(W."OnHand")<=0)) AS "OutOfStockSKUs"
    FROM DUMMY""")})

def get_categories(schema="jivo_oil"):
    db=get_schema(schema)
    return JSONResponse(content={"data":q(f"""SELECT G."ItmsGrpNam" AS "Category",COUNT(DISTINCT W."ItemCode") AS "SKUs",ROUND(SUM(W."OnHand"),0) AS "Qty",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "Value"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' AND G."ItmsGrpNam" IN ('FINISHED','RAW MATERIAL','PACKAGING MATERIAL') AND W."OnHand">0 {GIFT_EXCL}
    GROUP BY G."ItmsGrpNam" ORDER BY "Value" DESC""")})

def get_out_of_stock(category=None,schema="jivo_oil"):
    db=get_schema(schema);f=cf(category)
    return JSONResponse(content={"data":q(f"""SELECT G."ItmsGrpNam" AS "Category",M."ItemCode",M."ItemName",ROUND(SUM(W."OnHand"),0) AS "TotalOnHand"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} {GIFT_EXCL}
    GROUP BY G."ItmsGrpNam",M."ItemCode",M."ItemName" HAVING SUM(W."OnHand")<=0 ORDER BY G."ItmsGrpNam",M."ItemName" """)})

def get_warehouses(schema="jivo_oil"):
    db=get_schema(schema)
    return JSONResponse(content={"data":q(f"""
    SELECT W."WhsCode",W."WhsName",COALESCE(U."U_NAME",'–') AS "OwnerName"
    FROM {db}.OWHS W
    LEFT JOIN {db}.OUSR U ON CAST(W."U_Owner" AS VARCHAR(20))=CAST(U."USERID" AS VARCHAR(20))
    WHERE W."WhsCode" NOT IN ('01','BH-FA','DL-FA','GP-FA','MY-FA','DL','HR')
    ORDER BY W."WhsName" """)})

def get_warehouse_summary(category=None,schema="jivo_oil",owner=None):
    db=get_schema(schema);f=cf(category)
    owner_f=f"AND COALESCE(U.\"U_NAME\",'–')='{safe(owner)}'" if owner else ""
    return JSONResponse(content={"data":q(f"""
    SELECT W."WhsCode",H."WhsName",COALESCE(U."U_NAME",'–') AS "OwnerName",
        COUNT(DISTINCT W."ItemCode") AS "SKUs",
        ROUND(SUM(W."OnHand"),0) AS "Qty",ROUND(SUM(W."OnOrder"),0) AS "OnOrder",
        ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "Value"
    FROM {db}.OITW W
    JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
    JOIN {db}.OWHS H ON W."WhsCode"=H."WhsCode"
    LEFT JOIN {db}.OUSR U ON CAST(H."U_Owner" AS VARCHAR(20))=CAST(U."USERID" AS VARCHAR(20))
    JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {GIFT_EXCL} {owner_f}
    GROUP BY W."WhsCode",H."WhsName",U."U_NAME" ORDER BY "Value" DESC""")})

def get_warehouse_items(whs="",category=None,schema="jivo_oil"):
    db=get_schema(schema);f=cf(category);s=safe(whs)
    return JSONResponse(content={"data":q(f"""
    SELECT G."ItmsGrpNam" AS "Category",W."ItemCode",M."ItemName",
        COALESCE(M."U_Sub_Group",'–') AS "SubGroup",COALESCE(M."U_TYPE",'–') AS "ItemType",
        ROUND(W."OnHand",0) AS "OnHand",ROUND(W."OnOrder",0) AS "OnOrder",
        ROUND(W."OnHand"-W."IsCommited"+W."OnOrder",0) AS "Available",
        ROUND(W."OnHand"*M."LastPurPrc",0) AS "StockValue"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."WhsCode"='{s}' AND W."OnHand">0 {GIFT_EXCL}
    ORDER BY "StockValue" DESC""")})

def get_warehouse_owners(schema="jivo_oil"):
    db=get_schema(schema)
    return JSONResponse(content={"data":q(f"""
    SELECT DISTINCT U."U_NAME" AS "OwnerName"
    FROM {db}.OWHS H
    LEFT JOIN {db}.OUSR U ON CAST(H."U_Owner" AS VARCHAR(20))=CAST(U."USERID" AS VARCHAR(20))
    WHERE U."U_NAME" IS NOT NULL ORDER BY U."U_NAME" """)})

def get_stock_position(category=None,schema="jivo_oil",whs=None):
    db=get_schema(schema);f=cf(category);wf_=wf(whs)
    return JSONResponse(content={"data":q(f"""SELECT G."ItmsGrpNam" AS "Category",W."ItemCode",M."ItemName",
    W."WhsCode",H."WhsName",COALESCE(U."U_NAME",'–') AS "OwnerName",
    ROUND(W."OnHand",0) AS "OnHand",ROUND(W."OnOrder",0) AS "OnOrder",
    ROUND(W."OnHand"-W."IsCommited"+W."OnOrder",0) AS "Available",ROUND(W."OnHand"*M."LastPurPrc",0) AS "StockValue"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
    JOIN {db}.OWHS H ON W."WhsCode"=H."WhsCode"
    LEFT JOIN {db}.OUSR U ON CAST(H."U_Owner" AS VARCHAR(20))=CAST(U."USERID" AS VARCHAR(20))
    JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {wf_} {GIFT_EXCL}
    ORDER BY "StockValue" DESC""")})

def get_movement(days=30,category=None,schema="jivo_oil",
             date_from=None,date_to=None,whs=None):
    if days not in (7,15,30,60,90): days=30
    db=get_schema(schema);f=cf(category)
    date_filter=f"AND N.\"DocDate\">='{date_from}' AND N.\"DocDate\"<='{date_to}'" if date_from and date_to else f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    whs_f=f"AND N.\"Warehouse\"='{safe(whs)}'" if whs else ""
    return JSONResponse(content={"data":q(f"""
    SELECT TO_DATE(N."DocDate") AS "Date",N."Warehouse" AS "WhsCode",
        COALESCE(H."WhsName",N."Warehouse") AS "WhsName",
        M."ItemCode",M."ItemName",G."ItmsGrpNam" AS "Category",
        COALESCE(M."U_Sub_Group",'–') AS "SubGroup",
        ROUND(SUM(N."InQty"),0) AS "InQty",ROUND(SUM(N."OutQty"),0) AS "OutQty",
        ROUND(SUM(N."InQty"*N."Price"),0) AS "InValue",ROUND(SUM(N."OutQty"*N."Price"),0) AS "OutValue"
    FROM {db}.OINM N JOIN {db}.OITM M ON N."ItemCode"=M."ItemCode"
    JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN {db}.OWHS H ON N."Warehouse"=H."WhsCode"
    WHERE M."U_Unit"='OIL' {f} {GIFT_EXCL} {date_filter} {whs_f}
    GROUP BY TO_DATE(N."DocDate"),N."Warehouse",H."WhsName",M."ItemCode",M."ItemName",G."ItmsGrpNam",M."U_Sub_Group"
    ORDER BY TO_DATE(N."DocDate") DESC,"OutValue" DESC""")})

def get_movers_summary(days=30,category=None,schema="jivo_oil",
                   date_from=None,date_to=None):
    db=get_schema(schema);f=cf(category)
    date_filter=f"AND N.\"DocDate\">='{date_from}' AND N.\"DocDate\"<='{date_to}'" if date_from and date_to else f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    return JSONResponse(content={"data":q(f"""SELECT X."MovementStatus" AS "Status",COUNT(*) AS "Count",ROUND(SUM(X."StockValue"),0) AS "Value",ROUND(SUM(X."TotalOnHand"),0) AS "Qty"
    FROM (SELECT M."ItemCode",SUM(W."OnHand") AS "TotalOnHand",SUM(W."OnHand"*M."LastPurPrc") AS "StockValue",
        CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN 'NON-MOVING' WHEN MV."TotalOut"<50 THEN 'SLOW' WHEN MV."TotalOut"<500 THEN 'MEDIUM' ELSE 'FAST' END AS "MovementStatus"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN (SELECT N."ItemCode",SUM(N."OutQty") AS "TotalOut" FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode"
        WHERE N."OutQty">0 {date_filter} AND I."U_Unit"='OIL' GROUP BY N."ItemCode") MV ON M."ItemCode"=MV."ItemCode"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {GIFT_EXCL}
    GROUP BY M."ItemCode",MV."TotalOut") X
    GROUP BY X."MovementStatus" ORDER BY CASE X."MovementStatus" WHEN 'NON-MOVING' THEN 1 WHEN 'SLOW' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END""")})

def get_movers_by_subgroup(days=30,item_type=None,category="FINISHED",schema="jivo_oil",
                       date_from=None,date_to=None):
    db=get_schema(schema);type_f=tf(item_type)
    cat_f=f"AND G.\"ItmsGrpNam\"='{category.upper()}'"
    valid=FG_VALID if category=='FINISHED' else (PM_VALID if category=='PACKAGING MATERIAL' else RM_VALID)
    date_filter=f"AND N.\"DocDate\">='{date_from}' AND N.\"DocDate\"<='{date_to}'" if date_from and date_to else f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    return JSONResponse(content={"data":q(f"""
    SELECT COALESCE(CASE WHEN M."U_Sub_Group"='MUSTARD' AND M."U_TYPE"='PREMIUM' THEN 'YELLOW MUSTARD' ELSE M."U_Sub_Group" END,'UNCLASSIFIED') AS "SubGroup",
        COUNT(DISTINCT M."ItemCode") AS "TotalSKUs",
        SUM(CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN 1 ELSE 0 END) AS "NonMovingSKUs",
        SUM(CASE WHEN COALESCE(MV."TotalOut",0)>0 AND COALESCE(MV."TotalOut",0)<50 THEN 1 ELSE 0 END) AS "SlowSKUs",
        SUM(CASE WHEN COALESCE(MV."TotalOut",0)>=50 AND COALESCE(MV."TotalOut",0)<500 THEN 1 ELSE 0 END) AS "MediumSKUs",
        SUM(CASE WHEN COALESCE(MV."TotalOut",0)>=500 THEN 1 ELSE 0 END) AS "FastSKUs",
        ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
        ROUND(SUM(CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN W."OnHand"*M."LastPurPrc" ELSE 0 END),0) AS "StuckValue"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN (SELECT N."ItemCode",SUM(N."OutQty") AS "TotalOut" FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode"
        WHERE N."OutQty">0 {date_filter} AND I."U_Unit"='OIL' GROUP BY N."ItemCode") MV ON M."ItemCode"=MV."ItemCode"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {cat_f} AND W."OnHand">0 AND M."U_Sub_Group" IN ({valid}) {type_f}
    GROUP BY CASE WHEN M."U_Sub_Group"='MUSTARD' AND M."U_TYPE"='PREMIUM' THEN 'YELLOW MUSTARD' ELSE M."U_Sub_Group" END
    ORDER BY "StuckValue" DESC,"StockValue" DESC""")})

def get_movers(days=30,category=None,subgroup=None,item_type=None,schema="jivo_oil",
           date_from=None,date_to=None):
    db=get_schema(schema);f=cf(category)
    sg="AND M.\"U_Sub_Group\"='MUSTARD' AND M.\"U_TYPE\"='PREMIUM'" if subgroup=='YELLOW MUSTARD' else (f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else "")
    type_f=tf(item_type)
    if date_from and date_to:
        date_filter=f"AND N.\"DocDate\">='{date_from}' AND N.\"DocDate\"<='{date_to}'"
        day_div=f"(DAYS_BETWEEN(TO_DATE('{date_from}'),TO_DATE('{date_to}'))+1)"
    else:
        date_filter=f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
        day_div=str(days)
    return JSONResponse(content={"data":q(f"""
    SELECT G."ItmsGrpNam" AS "Category",M."ItemCode",M."ItemName",
        CASE WHEN M."U_Sub_Group"='MUSTARD' AND M."U_TYPE"='PREMIUM' THEN 'YELLOW MUSTARD' ELSE COALESCE(M."U_Sub_Group",'–') END AS "SubGroup",
        COALESCE(M."U_TYPE",'–') AS "ItemType",
        ROUND(SUM(W."OnHand"),0) AS "TotalOnHand",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
        COALESCE(MV."TotalOut",0) AS "Out{days}d",TO_DATE(MV."LastMoveDate") AS "LastMoveDate",
        CASE WHEN MV."LastMoveDate" IS NULL THEN -1 ELSE DAYS_BETWEEN(MV."LastMoveDate",CURRENT_DATE) END AS "DaysSinceMove",
        CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN 'NON-MOVING' WHEN MV."TotalOut"<50 THEN 'SLOW' WHEN MV."TotalOut"<500 THEN 'MEDIUM' ELSE 'FAST' END AS "MovementStatus",
        CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN -1 ELSE ROUND(SUM(W."OnHand")/(MV."TotalOut"/{day_div}),0) END AS "DaysOfStockLeft"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN (SELECT N."ItemCode",SUM(N."OutQty") AS "TotalOut",MAX(CASE WHEN N."OutQty">0 THEN N."DocDate" END) AS "LastMoveDate"
        FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode"
        WHERE N."OutQty">0 {date_filter} AND I."U_Unit"='OIL' GROUP BY N."ItemCode") MV ON M."ItemCode"=MV."ItemCode"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {sg} {type_f} {GIFT_EXCL}
    GROUP BY G."ItmsGrpNam",M."ItemCode",M."ItemName",M."U_Sub_Group",M."U_TYPE",MV."TotalOut",MV."LastMoveDate"
    ORDER BY COALESCE(MV."TotalOut",0) ASC,"StockValue" DESC""")})

def get_not_billed_summary(schema="jivo_oil"):
    db=get_schema(schema)
    parts=[]
    for d in [30,60,90]:
        parts.append(f"""SELECT '{d} Days' AS "Period",
        COUNT(DISTINCT CASE WHEN B."ItemCode" IS NULL THEN M."ItemCode" END) AS "NotBilledSKUs",
        ROUND(SUM(CASE WHEN B."ItemCode" IS NULL THEN W."OnHand"*M."LastPurPrc" ELSE 0 END),0) AS "NotBilledValue"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN (SELECT DISTINCT L."ItemCode" FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry"
            WHERE I."CANCELED"='N' AND I."DocDate">=ADD_DAYS(CURRENT_DATE,-{d})) B ON M."ItemCode"=B."ItemCode"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' AND G."ItmsGrpNam"='FINISHED'
          AND W."OnHand">0 AND M."CreateDate"<ADD_DAYS(CURRENT_DATE,-30) {GIFT_EXCL}""")
    return JSONResponse(content={"data":q(" UNION ALL ".join(parts))})

def get_not_billed_by_subgroup(days=30,item_type=None,schema="jivo_oil",
                           date_from=None,date_to=None):
    db=get_schema(schema);type_f=tf(item_type)
    bill_filter=f"AND I.\"DocDate\">='{date_from}' AND I.\"DocDate\"<='{date_to}'" if date_from and date_to else f"AND I.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    return JSONResponse(content={"data":q(f"""
    SELECT COALESCE(M."U_Sub_Group",'UNCLASSIFIED') AS "SubGroup",
        COUNT(DISTINCT M."ItemCode") AS "TotalSKUs",
        COUNT(DISTINCT CASE WHEN B."ItemCode" IS NULL THEN M."ItemCode" END) AS "NotBilledSKUs",
        ROUND(SUM(CASE WHEN B."ItemCode" IS NULL THEN W."OnHand"*M."LastPurPrc" ELSE 0 END),0) AS "NotBilledValue",
        ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "TotalValue"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN (SELECT DISTINCT L."ItemCode" FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry"
        WHERE I."CANCELED"='N' {bill_filter}) B ON M."ItemCode"=B."ItemCode"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' AND G."ItmsGrpNam"='FINISHED'
      AND W."OnHand">0 AND M."CreateDate"<ADD_DAYS(CURRENT_DATE,-30) AND M."U_Sub_Group" IN ({FG_VALID}) {type_f}
    GROUP BY M."U_Sub_Group" ORDER BY "NotBilledValue" DESC""")})

def get_not_billed(days=30,subgroup=None,item_type=None,schema="jivo_oil",
               date_from=None,date_to=None):
    db=get_schema(schema)
    sg=f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    type_f=tf(item_type)
    rc_filter=f"AND I.\"DocDate\">='{date_from}' AND I.\"DocDate\"<='{date_to}'" if date_from and date_to else f"AND I.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    return JSONResponse(content={"data":q(f"""
    SELECT G."ItmsGrpNam" AS "Category",M."ItemCode",M."ItemName",
        COALESCE(M."U_Sub_Group",'–') AS "SubGroup",COALESCE(M."U_TYPE",'–') AS "ItemType",
        ROUND(SUM(W."OnHand"),0) AS "CurrentStock",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
        TO_DATE(LB."LastBillDate") AS "LastBillDate",
        CASE WHEN LB."LastBillDate" IS NULL THEN 'NEVER BILLED'
             ELSE CAST(DAYS_BETWEEN(LB."LastBillDate",CURRENT_DATE) AS VARCHAR)||' days ago' END AS "LastBilledAgo",
        LB."LastCustomer",TO_DATE(M."CreateDate") AS "CreatedOn"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN (SELECT L."ItemCode",MAX(I."DocDate") AS "LastBillDate",MAX(I."CardName") AS "LastCustomer"
        FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry" WHERE I."CANCELED"='N' GROUP BY L."ItemCode") LB ON M."ItemCode"=LB."ItemCode"
    LEFT JOIN (SELECT DISTINCT L."ItemCode" FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry"
        WHERE I."CANCELED"='N' {rc_filter}) RC ON M."ItemCode"=RC."ItemCode"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' AND G."ItmsGrpNam"='FINISHED'
      AND W."OnHand">0 AND RC."ItemCode" IS NULL AND M."CreateDate"<ADD_DAYS(CURRENT_DATE,-30) {GIFT_EXCL} {sg} {type_f}
    GROUP BY G."ItmsGrpNam",M."ItemCode",M."ItemName",M."U_Sub_Group",M."U_TYPE",LB."LastBillDate",LB."LastCustomer",M."CreateDate"
    ORDER BY "StockValue" DESC""")})

# ════════════ Finished Goods — Non-Inventory (non-moving stock) ════════════
# One row per in-stock FG item with: date of production (first goods receipt into stock,
# MIN OINM.DocDate WHERE InQty>0), days in stock, last billed (MAX OINV.DocDate) + days
# since, and "days since it moved" = days since last billed (falling back to production
# age when never billed). Qty/Litres/Boxes are shown for every item (Litres = OnHand ×
# SalPackUn when the SKU is a litre item; Boxes = OnHand ÷ SalFactor2 pcs-per-box).
# FG warehouses for the Non-Inventory report — a fixed set; everything else (e.g. BH-FU) is
# excluded so "in stock" only counts these godowns. Applied to the oil/mart view (beverages,
# which live in different warehouses, are left unrestricted).
NON_INV_WHS = ['BH-PF', 'BH-FG', 'GP-FG', 'BH-EC', 'BH-BT']
_NON_INV_WHS_IN = ",".join("'%s'" % w for w in NON_INV_WHS)


def _non_inventory_rows(db, unit, tax_select, tax_group, gift_excl='', whs_in=None):
    whs_w  = f'AND W."WhsCode" IN ({whs_in})' if whs_in else ''
    oinm_w = f'AND N."Warehouse" IN ({whs_in})' if whs_in else ''
    return q(f"""SELECT M."ItemCode" AS "ItemCode", M."ItemName" AS "ItemName",
        {tax_select}
        TO_DATE(MAX(FR."FirstDate")) AS "ProdDate",
        DAYS_BETWEEN(MAX(FR."FirstDate"), CURRENT_DATE) AS "DaysInStock",
        TO_DATE(MAX(LB."LastBillDate")) AS "LastBillDate",
        DAYS_BETWEEN(MAX(LB."LastBillDate"), CURRENT_DATE) AS "DaysSinceBilled",
        MAX(LB."LastCustomer") AS "LastCustomer",
        MAX(LB."LastCode") AS "LastCode",
        MAX(LB."LastQty") AS "LastQty",
        COALESCE(DAYS_BETWEEN(MAX(LB."LastBillDate"), CURRENT_DATE),
                 DAYS_BETWEEN(MAX(FR."FirstDate"), CURRENT_DATE)) AS "DaysSinceMoved",
        ROUND(SUM(W."OnHand"),0) AS "Qty",
        CASE WHEN MAX(M."U_IsLitre")='Y' THEN ROUND(SUM(W."OnHand")*MAX(M."SalPackUn"),2) ELSE 0 END AS "Litres",
        CASE WHEN MAX(M."SalFactor2")>0 THEN ROUND(SUM(W."OnHand")/MAX(M."SalFactor2"),2) ELSE 0 END AS "Boxes",
        ROUND(SUM(W."OnHand")*MAX(M."LastPurPrc"),0) AS "Value",
        CASE WHEN MAX(M."U_IsLitre")='Y' THEN MAX(M."SalPackUn") ELSE 0 END AS "LitrePer",
        MAX(M."SalFactor2") AS "BoxPer",
        MAX(M."LastPurPrc") AS "PricePer"
    FROM {db}.OITW W
    JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
    JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN (SELECT N."ItemCode", MIN(N."DocDate") AS "FirstDate"
               FROM {db}.OINM N WHERE N."InQty">0 {oinm_w} GROUP BY N."ItemCode") FR ON M."ItemCode"=FR."ItemCode"
    LEFT JOIN (SELECT "ItemCode", "LastBillDate", "LastCustomer", "LastCode", "LastQty" FROM (
                 SELECT L."ItemCode" AS "ItemCode", I."DocDate" AS "LastBillDate",
                        I."CardName" AS "LastCustomer", I."CardCode" AS "LastCode",
                        SUM(L."Quantity") AS "LastQty",
                        ROW_NUMBER() OVER (PARTITION BY L."ItemCode" ORDER BY I."DocDate" DESC, I."DocEntry" DESC) AS "rn"
                 FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry"
                 WHERE I."CANCELED"='N'
                 GROUP BY L."ItemCode", I."DocEntry", I."DocDate", I."CardName", I."CardCode") X WHERE X."rn"=1) LB ON M."ItemCode"=LB."ItemCode"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='{unit}' AND G."ItmsGrpNam"='FINISHED' AND W."OnHand">0 {whs_w} {gift_excl}
    GROUP BY M."ItemCode", M."ItemName"{tax_group}
    ORDER BY "ProdDate" DESC NULLS LAST, "Value" DESC""")


def _non_inventory_wh(db, unit, gift_excl='', whs_in=None):
    """Per-(item, warehouse) on-hand for the same FG population — powers the warehouse
    multi-select (the client re-aggregates Qty/Ltr/Boxes/Value from the picked warehouses)."""
    whs_w = f'AND W."WhsCode" IN ({whs_in})' if whs_in else ''
    return q(f"""SELECT W."ItemCode" AS "ItemCode", W."WhsCode" AS "WhsCode", SUM(W."OnHand") AS "OnHand"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='{unit}' AND G."ItmsGrpNam"='FINISHED' AND W."OnHand">0 {whs_w} {gift_excl}
    GROUP BY W."ItemCode", W."WhsCode" """)


def get_non_inventory(schema="jivo_oil"):
    """In-stock finished goods as a non-moving aging view, newest-produced first. Each row
    carries the full taxonomy (Sub Group, Variety, SKU, Prem/Comm) for the client-side filters,
    per-item conversion factors, and a per-warehouse on-hand breakdown ('wh') so the warehouse
    multi-select can re-aggregate client-side. Beverages (jivo_beverages) have no Prem/Comm and
    aren't warehouse-restricted; oils/mart map Prem/Comm to U_TYPE and use the fixed FG godowns.
    Returns {items, unit, schema, warehouses}."""
    db = get_schema(schema)
    # Shared taxonomy columns (all UDFs exist in the oil/mart/beverages item masters).
    tax_common = ('COALESCE(M."U_Sub_Group",\'–\') AS "SubGroup", '
                  'COALESCE(M."U_Variety",\'–\') AS "Variety", '
                  'COALESCE(M."U_SKU",\'–\') AS "SKU", ')
    if schema == "jivo_beverages":
        # U_TYPE (Premium/Commodity) is an oils concept — keep it blank for beverages so the
        # query never depends on that field being populated in the beverages company.
        tax_select = tax_common + '\'–\' AS "PremComm",'
        tax_group = ', M."U_Sub_Group", M."U_Variety", M."U_SKU"'
        sql_unit, gift, whs_in, unit = 'BEVERAGES', '', None, 'boxes'
    else:
        tax_select = tax_common + 'COALESCE(M."U_TYPE",\'–\') AS "PremComm",'
        tax_group = ', M."U_Sub_Group", M."U_Variety", M."U_SKU", M."U_TYPE"'
        sql_unit, gift, whs_in, unit = 'OIL', GIFT_EXCL, _NON_INV_WHS_IN, 'litres'
    rows = _non_inventory_rows(db, sql_unit, tax_select, tax_group, gift, whs_in)

    # Attach per-warehouse on-hand to each item, and collect the warehouses present.
    wh_map, whs_seen = {}, {}
    for r in _non_inventory_wh(db, sql_unit, gift, whs_in):
        wc = str(r.get('WhsCode') or '').strip()
        if not wc:
            continue
        code = str(r.get('ItemCode') or '')
        oh = float(r.get('OnHand') or 0)
        wh_map.setdefault(code, {})[wc] = oh
        whs_seen[wc] = whs_seen.get(wc, 0.0) + oh
    for it in rows:
        it['wh'] = wh_map.get(str(it.get('ItemCode') or ''), {})
    return {'items': rows, 'unit': unit, 'schema': schema, 'warehouses': sorted(whs_seen.keys())}


def get_non_inventory_drill(item="", schema="jivo_oil", whs=""):
    """Per-warehouse breakdown behind one item's stock: warehouse code + name, on-hand qty,
    and that warehouse's production (first-receipt) date. No movement history — just the split.
    `whs` (comma-separated codes) limits the breakdown to the currently-picked warehouses."""
    db = get_schema(schema)
    it = safe(item)
    if not it:
        return []
    codes = [c.strip() for c in str(whs or '').split(',') if c.strip()]
    if codes:
        whs_in = ",".join("'%s'" % safe(c) for c in codes)
    else:
        whs_in = None if schema == "jivo_beverages" else _NON_INV_WHS_IN
    whs_w  = f'AND W."WhsCode" IN ({whs_in})' if whs_in else ''
    oinm_w = f'AND N."Warehouse" IN ({whs_in})' if whs_in else ''
    return q(f"""SELECT W."WhsCode" AS "WhsCode", COALESCE(H."WhsName", W."WhsCode") AS "WhsName",
        ROUND(W."OnHand",0) AS "Qty",
        TO_DATE(FR."FirstDate") AS "ProdDate"
    FROM {db}.OITW W
    JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
    LEFT JOIN {db}.OWHS H ON W."WhsCode"=H."WhsCode"
    LEFT JOIN (SELECT N."ItemCode", N."Warehouse", MIN(N."DocDate") AS "FirstDate"
               FROM {db}.OINM N WHERE N."InQty">0 {oinm_w} GROUP BY N."ItemCode", N."Warehouse") FR
         ON W."ItemCode"=FR."ItemCode" AND W."WhsCode"=FR."Warehouse"
    WHERE W."ItemCode"='{it}' AND W."OnHand">0 {whs_w}
    ORDER BY W."OnHand" DESC""")


def abc_inner(db):
    return f"""SELECT M."ItemCode",M."ItemName",COALESCE(M."U_Sub_Group",'UNCLASSIFIED') AS "SubGroup",COALESCE(M."U_TYPE",'–') AS "ItemType",
    ROUND(SUM(W."OnHand"),0) AS "TotalOnHand",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
    ROW_NUMBER() OVER (ORDER BY SUM(W."OnHand"*M."LastPurPrc") DESC) AS "Rank",
    ROUND(SUM(SUM(W."OnHand"*M."LastPurPrc")) OVER (ORDER BY SUM(W."OnHand"*M."LastPurPrc") DESC)/NULLIF(SUM(SUM(W."OnHand"*M."LastPurPrc")) OVER(),0)*100,2) AS "CumulativePct"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' AND G."ItmsGrpNam"='FINISHED' AND W."OnHand">0 AND M."U_Sub_Group" NOT IN ('GIFT PACK')
    GROUP BY M."ItemCode",M."ItemName",M."U_Sub_Group",M."U_TYPE" """

def xyz_cte(db):
    return f"""MONTHLY AS (SELECT N."ItemCode",
        CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-30) THEN 'M1' WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-60) THEN 'M2' ELSE 'M3' END AS "Month",
        SUM(N."OutQty") AS "MonthlyOut"
        FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode" JOIN {db}.OITB G ON I."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE N."OutQty">0 AND N."DocDate">=ADD_DAYS(CURRENT_DATE,-90) AND I."U_Unit"='OIL' AND G."ItmsGrpNam"='FINISHED'
          AND I."U_Sub_Group" NOT IN ('GIFT PACK')
        GROUP BY N."ItemCode",CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-30) THEN 'M1' WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-60) THEN 'M2' ELSE 'M3' END),
    STATS AS (SELECT "ItemCode",AVG("MonthlyOut") AS "AvgOut",STDDEV("MonthlyOut") AS "StdOut" FROM MONTHLY GROUP BY "ItemCode"),
    XYZ_BASE AS (SELECT S."ItemCode",ROUND(S."AvgOut",1) AS "AvgMonthlyOut",
        CASE WHEN S."AvgOut">0 THEN ROUND(S."StdOut"/S."AvgOut",4) ELSE 9999 END AS "CoV",
        CASE WHEN S."AvgOut" IS NULL OR S."AvgOut"=0 THEN 'Z' WHEN S."StdOut"/S."AvgOut"<0.5 THEN 'X' WHEN S."StdOut"/S."AvgOut"<1.0 THEN 'Y' ELSE 'Z' END AS "XYZClass"
        FROM STATS S)"""

def get_abcxyz_summary(schema="jivo_oil"):
    db=get_schema(schema);AI=abc_inner(db);XC=xyz_cte(db)
    return JSONResponse(content={"data":q(f"""WITH ABC_BASE AS (SELECT "ItemCode","StockValue",CASE WHEN "CumulativePct"<=80 THEN 'A' WHEN "CumulativePct"<=95 THEN 'B' ELSE 'C' END AS "ABCClass" FROM ({AI}) X),
    {XC},COMBINED AS (SELECT A."ABCClass",COALESCE(X."XYZClass",'Z') AS "XYZClass",A."ABCClass"||COALESCE(X."XYZClass",'Z') AS "Combo",A."StockValue" FROM ABC_BASE A LEFT JOIN XYZ_BASE X ON A."ItemCode"=X."ItemCode")
    SELECT "Combo" AS "ABCXYZClass","ABCClass","XYZClass",COUNT(*) AS "SKUs",ROUND(SUM("StockValue"),0) AS "Value" FROM COMBINED GROUP BY "Combo","ABCClass","XYZClass" ORDER BY "ABCClass","XYZClass" """)})

def get_abcxyz_by_subgroup(item_type=None,schema="jivo_oil"):
    db=get_schema(schema);type_f=tf(item_type);AI=abc_inner(db);XC=xyz_cte(db)
    return JSONResponse(content={"data":q(f"""WITH ABC_BASE AS (SELECT "ItemCode","ItemName","SubGroup","ItemType","TotalOnHand","StockValue","CumulativePct","Rank",
        CASE WHEN "CumulativePct"<=80 THEN 'A' WHEN "CumulativePct"<=95 THEN 'B' ELSE 'C' END AS "ABCClass" FROM ({AI}) X),
    {XC},COMBINED AS (SELECT A."SubGroup",COUNT(*) AS "TotalSKUs",ROUND(SUM(A."StockValue"),0) AS "StockValue",
        SUM(CASE WHEN A."ABCClass"='A' THEN 1 ELSE 0 END) AS "A_Count",SUM(CASE WHEN A."ABCClass"='B' THEN 1 ELSE 0 END) AS "B_Count",SUM(CASE WHEN A."ABCClass"='C' THEN 1 ELSE 0 END) AS "C_Count",
        SUM(CASE WHEN COALESCE(X."XYZClass",'Z')='X' THEN 1 ELSE 0 END) AS "X_Count",SUM(CASE WHEN COALESCE(X."XYZClass",'Z')='Y' THEN 1 ELSE 0 END) AS "Y_Count",SUM(CASE WHEN COALESCE(X."XYZClass",'Z')='Z' THEN 1 ELSE 0 END) AS "Z_Count"
        FROM ABC_BASE A LEFT JOIN XYZ_BASE X ON A."ItemCode"=X."ItemCode"
        JOIN {db}.OITM M ON A."ItemCode"=M."ItemCode"
        WHERE A."SubGroup" IN ({FG_VALID}) {type_f} GROUP BY A."SubGroup")
    SELECT * FROM COMBINED ORDER BY "StockValue" DESC""")})

def get_abcxyz(subgroup=None,item_type=None,combo=None,schema="jivo_oil"):
    db=get_schema(schema)
    sg=f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    type_f=tf(item_type)
    combo_f=f"AND A.\"ABCClass\"||COALESCE(X.\"XYZClass\",'Z')='{safe(combo)}'" if combo and combo!='all' else ""
    AI=abc_inner(db);XC=xyz_cte(db)
    return JSONResponse(content={"data":q(f"""WITH ABC_BASE AS (SELECT "ItemCode","ItemName","SubGroup","ItemType","TotalOnHand","StockValue","CumulativePct","Rank",
        CASE WHEN "CumulativePct"<=80 THEN 'A' WHEN "CumulativePct"<=95 THEN 'B' ELSE 'C' END AS "ABCClass" FROM ({AI}) X),
    {XC}
    SELECT A."ItemCode",A."ItemName",A."SubGroup",A."ItemType",A."TotalOnHand",A."StockValue",A."CumulativePct",A."Rank",A."ABCClass",
        COALESCE(X."XYZClass",'Z') AS "XYZClass",COALESCE(X."AvgMonthlyOut",0) AS "AvgMonthlyOut",COALESCE(X."CoV",9999) AS "CoV",
        A."ABCClass"||COALESCE(X."XYZClass",'Z') AS "ABCXYZClass"
    FROM ABC_BASE A LEFT JOIN XYZ_BASE X ON A."ItemCode"=X."ItemCode"
    JOIN {db}.OITM M ON A."ItemCode"=M."ItemCode"
    WHERE 1=1 {sg} {type_f} {combo_f} ORDER BY A."Rank" """)})

def get_aging(category=None,schema="jivo_oil"):
    db=get_schema(schema);f=cf(category)
    return JSONResponse(content={"data":q(f"""
    SELECT G."ItmsGrpNam" AS "Category",
        CASE WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=30 THEN '0-30'
             WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=60 THEN '31-60'
             WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=90 THEN '61-90'
             ELSE '90+' END AS "Bucket",
        COUNT(DISTINCT W."ItemCode"||'|'||W."WhsCode") AS "Items",
        ROUND(SUM(W."OnHand"),0) AS "Qty",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "Value"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    JOIN (SELECT N."ItemCode",N."Warehouse",MIN(N."DocDate") AS "FirstDate" FROM {db}.OINM N WHERE N."InQty">0 GROUP BY N."ItemCode",N."Warehouse") FR
         ON W."ItemCode"=FR."ItemCode" AND W."WhsCode"=FR."Warehouse"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {GIFT_EXCL}
    GROUP BY G."ItmsGrpNam",CASE WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=30 THEN '0-30'
        WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=60 THEN '31-60'
        WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=90 THEN '61-90' ELSE '90+' END
    ORDER BY "Category",MIN(DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE))""")})

def get_aging_drill(bucket="0-30",category=None,schema="jivo_oil"):
    db=get_schema(schema);f=cf(category)
    lo_hi={"0-30":(0,30),"31-60":(31,60),"61-90":(61,90),"90+":(91,99999)}
    lo,hi=lo_hi.get(bucket,(0,30))
    return JSONResponse(content={"data":q(f"""
    SELECT G."ItmsGrpNam" AS "Category",W."ItemCode",M."ItemName",W."WhsCode",
        TO_DATE(FR."FirstDate") AS "FirstReceiptDate",
        DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE) AS "DaysSitting",
        ROUND(W."OnHand",0) AS "Qty",ROUND(W."OnHand"*M."LastPurPrc",0) AS "Value"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    JOIN (SELECT N."ItemCode",N."Warehouse",MIN(N."DocDate") AS "FirstDate" FROM {db}.OINM N WHERE N."InQty">0 GROUP BY N."ItemCode",N."Warehouse") FR
         ON W."ItemCode"=FR."ItemCode" AND W."WhsCode"=FR."Warehouse"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='OIL' {f} AND W."OnHand">0 {GIFT_EXCL}
      AND DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)>={lo} AND DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<={hi}
    ORDER BY "DaysSitting" DESC,"Value" DESC""")})

def get_trace_subgroups(category="FINISHED",schema="jivo_oil"):
    db=get_schema(schema)
    valid=FG_VALID if category=='FINISHED' else (PM_VALID if category=='PACKAGING MATERIAL' else RM_VALID)
    return JSONResponse(content={"data":q(f"""
    SELECT M."U_Sub_Group" AS "SubGroup",COUNT(DISTINCT M."ItemCode") AS "SKUs",
        ROUND(COALESCE(SUM(W."OnHand"),0),0) AS "OnHand"
    FROM {db}.OITM M JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN {db}.OITW W ON M."ItemCode"=W."ItemCode"
    WHERE G."ItmsGrpNam"='{category.upper()}' AND M."InvntItem"='Y' AND M."U_Unit"='OIL' AND M."U_Sub_Group" IN ({valid})
    GROUP BY M."U_Sub_Group" ORDER BY SUM(W."OnHand") DESC NULLS LAST""")})

def get_trace_items(category="FINISHED",subgroup=None,schema="jivo_oil"):
    db=get_schema(schema)
    sg=f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    valid=FG_VALID if category=='FINISHED' else (PM_VALID if category=='PACKAGING MATERIAL' else RM_VALID)
    return JSONResponse(content={"data":q(f"""
    SELECT M."ItemCode",M."ItemName",COALESCE(M."U_Sub_Group",'–') AS "SubGroup",
        COALESCE(M."U_TYPE",'–') AS "ItemType",
        ROUND(COALESCE(SUM(W."OnHand"),0),0) AS "OnHand",ROUND(COALESCE(SUM(W."OnHand"*M."LastPurPrc"),0),0) AS "StockValue"
    FROM {db}.OITM M JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN {db}.OITW W ON M."ItemCode"=W."ItemCode"
    WHERE G."ItmsGrpNam"='{category.upper()}' AND M."InvntItem"='Y' AND M."U_Unit"='OIL' AND M."U_Sub_Group" IN ({valid}) {sg}
    GROUP BY M."ItemCode",M."ItemName",M."U_Sub_Group",M."U_TYPE"
    ORDER BY SUM(W."OnHand") DESC NULLS LAST,M."ItemName" """)})

def get_trace_header(item="",schema="jivo_oil"):
    db=get_schema(schema);s=safe(item)
    return JSONResponse(content={"data":q(f"""
    SELECT M."ItemCode",M."ItemName",TO_DATE(M."CreateDate") AS "CreateDate",
        M."U_Sub_Group" AS "SubGroup",M."U_TYPE" AS "ItemType",
        G."ItmsGrpNam" AS "Category",ROUND(M."LastPurPrc",4) AS "LastPrice",
        ROUND(COALESCE(SUM(W."OnHand"),0),0) AS "TotalOnHand",
        ROUND(COALESCE(SUM(W."OnOrder"),0),0) AS "TotalOnOrder",
        ROUND(COALESCE(SUM(W."OnHand"*M."LastPurPrc"),0),2) AS "StockValue"
    FROM {db}.OITM M JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN {db}.OITW W ON M."ItemCode"=W."ItemCode"
    WHERE M."ItemCode"='{s}'
    GROUP BY M."ItemCode",M."ItemName",M."CreateDate",M."U_Sub_Group",M."U_TYPE",G."ItmsGrpNam",M."LastPurPrc" """)})

def get_trace_log(item="",days=0,schema="jivo_oil",month=None):
    db=get_schema(schema);s=safe(item)
    date_f=f"AND TO_CHAR(N.\"DocDate\",'YYYY-MM')='{safe(month)}'" if month else (f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})" if days>0 else "")
    return JSONResponse(content={"data":q(f"""
    SELECT N."TransNum",N."TransType",CAST(N."BASE_REF" AS VARCHAR(50)) AS "BaseRef",
        TO_DATE(N."DocDate") AS "DocDate",N."CardName",N."JrnlMemo",N."Comments",
        ROUND(N."InQty",3) AS "InQty",ROUND(N."OutQty",3) AS "OutQty",
        ROUND(N."Price",4) AS "Price",ROUND(N."TransValue",2) AS "TransValue",
        N."Warehouse",COALESCE(H."WhsName",N."Warehouse") AS "WhsName",ROUND(N."Balance",3) AS "Balance"
    FROM {db}.OINM N LEFT JOIN {db}.OWHS H ON N."Warehouse"=H."WhsCode"
    WHERE N."ItemCode"='{s}' AND N."TransType" NOT IN (14,16) {date_f}
    ORDER BY N."DocDate" DESC,N."TransNum" DESC""")})

def get_trace_returns(item="",days=0,schema="jivo_oil",month=None):
    db=get_schema(schema);s=safe(item)
    date_f=f"AND TO_CHAR(N.\"DocDate\",'YYYY-MM')='{safe(month)}'" if month else (f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})" if days>0 else "")
    return JSONResponse(content={"data":q(f"""
    SELECT N."TransNum",N."TransType",TO_DATE(N."DocDate") AS "DocDate",N."CardName",
        N."JrnlMemo",N."Comments",ROUND(N."InQty",3) AS "ReturnQty",
        ROUND(N."TransValue",2) AS "TransValue",N."Warehouse",COALESCE(H."WhsName",N."Warehouse") AS "WhsName",
        CASE N."TransType" WHEN 14 THEN 'AR Return' WHEN 16 THEN 'AR Credit Note' END AS "ReturnType"
    FROM {db}.OINM N LEFT JOIN {db}.OWHS H ON N."Warehouse"=H."WhsCode"
    WHERE N."ItemCode"='{s}' AND N."TransType" IN (14,16) AND N."InQty">0 {date_f}
    ORDER BY N."DocDate" DESC""")})

def get_trace_disassembly(item="",days=0,schema="jivo_oil",month=None):
    db=get_schema(schema);s=safe(item)
    date_f=f"AND TO_CHAR(W.\"StartDate\",'YYYY-MM')='{safe(month)}'" if month else (f"AND W.\"StartDate\">=ADD_DAYS(CURRENT_DATE,-{days})" if days>0 else "")
    return JSONResponse(content={"data":q(f"""
    SELECT W."DocNum",W."Status",TO_DATE(W."StartDate") AS "StartDate",TO_DATE(W."DueDate") AS "DueDate",
        TO_DATE(W."CloseDate") AS "CloseDate",ROUND(W."PlannedQty",2) AS "PlannedQty",ROUND(W."CmpltQty",2) AS "ActualQty",W."Comments"
    FROM {db}.OWOR W WHERE W."ItemCode"='{s}' AND W."Type"='D' {date_f} ORDER BY W."StartDate" DESC""")})

def get_planning(subgroup=None,item_type=None,schema="jivo_oil"):
    db=get_schema(schema)
    sg=f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    type_f=tf(item_type)
    return JSONResponse(content={"data":q(f"""
    WITH CONSUMPTION AS (
        SELECT N."ItemCode",
            SUM(CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-30) THEN N."OutQty" ELSE 0 END) AS "Out30d",
            SUM(CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-60) AND N."DocDate"<ADD_DAYS(CURRENT_DATE,-30) THEN N."OutQty" ELSE 0 END) AS "Out30_60d",
            SUM(CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-90) THEN N."OutQty" ELSE 0 END) AS "Out90d",
            MAX(CASE WHEN N."OutQty">0 THEN N."DocDate" END) AS "LastMoveDate"
        FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode"
        WHERE N."OutQty">0 AND N."DocDate">=ADD_DAYS(CURRENT_DATE,-90) AND I."U_Unit"='OIL'
        GROUP BY N."ItemCode"
    )
    SELECT M."ItemCode",M."ItemName",COALESCE(M."U_Sub_Group",'–') AS "SubGroup",COALESCE(M."U_TYPE",'–') AS "ItemType",
        ROUND(SUM(W."OnHand"),0) AS "TotalOnHand",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
        ROUND(COALESCE(C."Out30d",0),0) AS "Out30d",ROUND(COALESCE(C."Out30_60d",0),0) AS "Out30_60d",
        ROUND(COALESCE(C."Out90d",0)/90,1) AS "AvgDailyOut",ROUND(COALESCE(C."Out90d",0)/3,0) AS "AvgMonthlyOut",
        CASE WHEN COALESCE(C."Out90d",0)=0 THEN -1
             ELSE ROUND(SUM(W."OnHand")/(COALESCE(C."Out90d",0)/90),0) END AS "DaysOfStockLeft",
        -- SuggestedOrder: how many units needed to bring stock up to 30-day supply
        CASE WHEN COALESCE(C."Out90d",0)=0 THEN 0
             WHEN SUM(W."OnHand")<(COALESCE(C."Out90d",0)/90)*30
             THEN ROUND(((COALESCE(C."Out90d",0)/90)*30)-SUM(W."OnHand"),0)
             ELSE 0 END AS "SuggestedOrder",
        CASE WHEN COALESCE(C."Out30d",0)=0 AND COALESCE(C."Out30_60d",0)=0 THEN 'FLAT'
             WHEN COALESCE(C."Out30d",0)>COALESCE(C."Out30_60d",0)*1.1 THEN 'RISING'
             WHEN COALESCE(C."Out30d",0)<COALESCE(C."Out30_60d",0)*0.9 THEN 'FALLING'
             ELSE 'STABLE' END AS "Trend",
        TO_DATE(C."LastMoveDate") AS "LastMoveDate",TO_DATE(M."CreateDate") AS "CreateDate"
    FROM {db}.OITM M JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    LEFT JOIN {db}.OITW W ON M."ItemCode"=W."ItemCode"
    LEFT JOIN CONSUMPTION C ON M."ItemCode"=C."ItemCode"
    WHERE G."ItmsGrpNam"='FINISHED' AND M."InvntItem"='Y' AND M."U_Unit"='OIL' AND W."OnHand">0
      AND M."U_Sub_Group" IN ({FG_VALID}) {sg} {type_f}
    GROUP BY M."ItemCode",M."ItemName",M."U_Sub_Group",M."U_TYPE",C."Out30d",C."Out30_60d",C."Out90d",C."LastMoveDate",M."CreateDate"
    ORDER BY "DaysOfStockLeft" ASC""")})

