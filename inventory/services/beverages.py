from .shared import cf, q, safe, wf

DB1 = "JIVO_MART_HANADB"
DB2 = "JIVO_OIL_HANADB"
DB3 = "JIVO_BEVERAGES_HANADB"
ALL_DBS = [DB1, DB2, DB3]
UNIT = "BEVERAGES"


def JSONResponse(content):
    return content.get("data", content)


def unit_f(category=None):
    return f"AND M.\"U_Unit\"='{UNIT}'"


def get_dbs(category=None):
    cat = (category or '').upper()
    if cat in ('RAW MATERIAL', 'PACKAGING MATERIAL'):
        return [DB3]
    return ALL_DBS
# ════════════ KPIs ════════════
def get_kpi(category=None, whs=None):
    f = cf(category); uf = unit_f(category); wf_ = wf(whs)
    inv_kpi = "AND M.\"InvntItem\"='Y'"
    dbs = get_dbs(category)
    tq, tv, ts, oos = 0, 0, 0, 0
    for db in dbs:
        r = q(f"""SELECT
        ROUND(COALESCE((SELECT SUM(W."OnHand") FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod" WHERE 1=1 {inv_kpi} {uf} {f} AND W."OnHand">0 {wf_}),0),0) AS "TotalQty",
        ROUND(COALESCE((SELECT SUM(W."OnHand"*M."LastPurPrc") FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod" WHERE 1=1 {inv_kpi} {uf} {f} AND W."OnHand">0 {wf_}),0),0) AS "TotalValue",
        (SELECT COUNT(DISTINCT W."ItemCode") FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod" WHERE 1=1 {inv_kpi} {uf} {f} AND W."OnHand">0 {wf_}) AS "TotalSKUs",
        (SELECT COUNT(*) FROM (SELECT M."ItemCode" FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod" WHERE 1=1 {inv_kpi} {uf} {f} {wf_} GROUP BY M."ItemCode" HAVING SUM(W."OnHand")<=0)) AS "OutOfStockSKUs"
        FROM DUMMY""")
        if r: tq += float(r[0].get("TotalQty",0) or 0); tv += float(r[0].get("TotalValue",0) or 0); ts += float(r[0].get("TotalSKUs",0) or 0); oos += float(r[0].get("OutOfStockSKUs",0) or 0)
    return JSONResponse(content={"data": [{"TotalQty": round(tq), "TotalValue": round(tv), "TotalSKUs": ts, "OutOfStockSKUs": oos}]})

def get_categories(category=None):
    f = cf(category); uf = unit_f(category)
    dbs = get_dbs(category)
    rows = []
    for db in dbs:
        rows.extend(q(f"""SELECT G."ItmsGrpNam" AS "Category",
            COUNT(DISTINCT W."ItemCode") AS "SKUs",
            ROUND(SUM(W."OnHand"),0) AS "Qty",
            ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "Value"
            FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
            JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
            WHERE M."InvntItem"='Y' {uf} {f} AND W."OnHand">0
            GROUP BY G."ItmsGrpNam" ORDER BY "Value" DESC"""))
    agg = {}
    for r in rows:
        c = r["Category"]
        if c not in agg: agg[c] = {"Category": c, "SKUs": 0, "Qty": 0, "Value": 0}
        agg[c]["SKUs"]  += float(r.get("SKUs",  0) or 0)
        agg[c]["Qty"]   += float(r.get("Qty",   0) or 0)
        agg[c]["Value"] += float(r.get("Value", 0) or 0)
    return JSONResponse(content={"data": sorted(agg.values(), key=lambda x: x["Value"], reverse=True)})

def get_out_of_stock(category=None):
    f = cf(category); uf = unit_f(category)
    combined = []
    for db in get_dbs(category):
        combined.extend(q(f"""SELECT G."ItmsGrpNam" AS "Category",M."ItemCode",M."ItemName",ROUND(SUM(W."OnHand"),0) AS "TotalOnHand",M."LastPurPrc"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE M."InvntItem"='Y' {uf} {f}
        GROUP BY G."ItmsGrpNam",M."ItemCode",M."ItemName",M."LastPurPrc" HAVING SUM(W."OnHand")<=0 ORDER BY G."ItmsGrpNam",M."ItemName" """))
    return JSONResponse(content={"data": combined})

# ════════════ WAREHOUSE ════════════
def get_warehouses():
    combined = []
    for db in ALL_DBS:
        if db == DB3:
            combined.extend(q(f"""SELECT W."WhsCode", W."WhsName", '–' AS "OwnerName", '' AS "OwnerId"
            FROM {db}.OWHS W
            WHERE W."WhsCode" NOT IN ('01','BH-FA','DL-FA','GP-FA','MY-FA','DL','HR')
            ORDER BY W."WhsName" """))
        else:
            combined.extend(q(f"""SELECT W."WhsCode", W."WhsName", COALESCE(U."U_NAME",'–') AS "OwnerName", W."U_Owner" AS "OwnerId"
            FROM {db}.OWHS W LEFT JOIN {db}.OUSR U ON W."U_Owner"=U."USERID"
            WHERE W."WhsCode" NOT IN ('01','BH-FA','DL-FA','GP-FA','MY-FA','DL','HR')
            ORDER BY W."WhsName" """))
    return JSONResponse(content={"data": combined})

def get_warehouse_owners():
    seen = set(); result = []
    for db in [DB1, DB2]:
        for r in q(f"""SELECT DISTINCT COALESCE(U."U_NAME",'–') AS "OwnerName" FROM {db}.OWHS H LEFT JOIN {db}.OUSR U ON H."U_Owner"=U."USERID" WHERE U."U_NAME" IS NOT NULL AND U."U_NAME"<>'–' ORDER BY U."U_NAME" """):
            if r["OwnerName"] not in seen: seen.add(r["OwnerName"]); result.append(r)
    return JSONResponse(content={"data": result})

def get_warehouse_summary(category=None, owner=None):
    f = cf(category); uf = unit_f(category)
    combined = []
    for db in get_dbs(category):
        owner_f = f"AND U.\"U_NAME\"='{safe(owner)}'" if owner and db != DB3 else ""
        owner_sel = "COALESCE(U.\"U_NAME\",'–') AS \"OwnerName\"" if db != DB3 else "'–' AS \"OwnerName\""
        owner_join = f"LEFT JOIN {db}.OUSR U ON H.\"U_Owner\"=U.\"USERID\"" if db != DB3 else ""
        group_col = ', U."U_NAME"' if db != DB3 else ''
        
        combined.extend(q(f"""SELECT W."WhsCode", H."WhsName", {owner_sel},
        COUNT(DISTINCT W."ItemCode") AS "SKUs", ROUND(SUM(W."OnHand"),0) AS "Qty",
        ROUND(SUM(W."OnOrder"),0) AS "OnOrder",
        ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "Value"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
        JOIN {db}.OWHS H ON W."WhsCode"=H."WhsCode"
        {owner_join}
        JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE {uf[4:]} {f} AND W."OnHand">0 {owner_f}
        GROUP BY W."WhsCode",H."WhsName"{group_col} ORDER BY "Value" DESC"""))
    combined.sort(key=lambda x: x.get("Value") or 0, reverse=True)
    return JSONResponse(content={"data": combined})

def get_warehouse_items(whs="", category=None):
    f = cf(category); s = safe(whs)
    combined = []
    for db in ALL_DBS:
        combined.extend(q(f"""SELECT G."ItmsGrpNam" AS "Category", W."ItemCode", M."ItemName",
        COALESCE(M."U_Sub_Group",'–') AS "SubGroup",
        ROUND(W."OnHand",0) AS "OnHand",
        ROUND(W."OnOrder",0) AS "OnOrder", ROUND(W."OnHand"-W."IsCommited"+W."OnOrder",0) AS "Available",
        ROUND(W."OnHand"*M."LastPurPrc",0) AS "StockValue"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
        JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' {f} AND W."WhsCode"='{s}' AND W."OnHand">0
        ORDER BY "StockValue" DESC"""))
    combined.sort(key=lambda x: x.get("StockValue") or 0, reverse=True)
    return JSONResponse(content={"data": combined})

def get_stock_position(category=None, whs=None):
    f = cf(category); uf = unit_f(category); wf_ = wf(whs)
    combined = []
    for db in get_dbs(category):
        owner_sel = "COALESCE(U.\"U_NAME\",'–') AS \"OwnerName\"" if db != DB3 else "'–' AS \"OwnerName\""
        owner_join = f"LEFT JOIN {db}.OUSR U ON H.\"U_Owner\"=U.\"USERID\"" if db != DB3 else ""
        combined.extend(q(f"""SELECT G."ItmsGrpNam" AS "Category",W."ItemCode",M."ItemName",W."WhsCode",H."WhsName",
        {owner_sel},
        ROUND(W."OnHand",0) AS "OnHand",
        ROUND(W."OnOrder",0) AS "OnOrder", ROUND(W."OnHand"-W."IsCommited"+W."OnOrder",0) AS "Available",
        ROUND(W."OnHand"*M."LastPurPrc",0) AS "StockValue"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
        JOIN {db}.OWHS H ON W."WhsCode"=H."WhsCode"
        {owner_join}
        JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE M."U_Unit"='{UNIT}' {f} AND W."OnHand">0 {wf_}
        ORDER BY "StockValue" DESC"""))
    combined.sort(key=lambda x: x.get("StockValue") or 0, reverse=True)
    return JSONResponse(content={"data": combined})

# ════════════ MOVEMENT ════════════
def get_movement(days=30, category=None,
             date_from=None, date_to=None, whs=None):
    if days not in (7, 15, 30, 60, 90): days = 30
    f = cf(category)
    if date_from and date_to:
        date_filter = f"AND N.\"DocDate\">='{date_from}' AND N.\"DocDate\"<='{date_to}'"
    else:
        date_filter = f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    whs_f = f"AND N.\"Warehouse\"='{safe(whs)}'" if whs else ""
    combined = []
    for db in get_dbs(category):
        combined.extend(q(f"""SELECT TO_DATE(N."DocDate") AS "Date",
        N."Warehouse" AS "WhsCode", COALESCE(H."WhsName", N."Warehouse") AS "WhsName",
        M."ItemCode", M."ItemName", G."ItmsGrpNam" AS "Category",
        COALESCE(M."U_Sub_Group",'–') AS "SubGroup",
        ROUND(SUM(N."InQty"),0) AS "InQty", ROUND(SUM(N."OutQty"),0) AS "OutQty",
        ROUND(SUM(N."InQty"*N."Price"),0) AS "InValue", ROUND(SUM(N."OutQty"*N."Price"),0) AS "OutValue"
        FROM {db}.OINM N
        JOIN {db}.OITM M ON N."ItemCode"=M."ItemCode"
        JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN {db}.OWHS H ON N."Warehouse"=H."WhsCode"
        WHERE M."U_Unit"='{UNIT}' {f} {date_filter} {whs_f}
        GROUP BY TO_DATE(N."DocDate"), N."Warehouse", H."WhsName", M."ItemCode", M."ItemName", G."ItmsGrpNam", M."U_Sub_Group"
        ORDER BY TO_DATE(N."DocDate") DESC, "OutValue" DESC"""))
    return JSONResponse(content={"data": combined})

# ════════════ MOVERS ════════════
def get_movers_summary(days=30, category=None,
                   date_from=None, date_to=None):
    f = cf(category)
    if date_from and date_to:
        date_filter = f"AND N.\"DocDate\">='{date_from}' AND N.\"DocDate\"<='{date_to}'"
    else:
        date_filter = f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    agg = {}
    for db in get_dbs(category):
        rows = q(f"""SELECT X."MovementStatus" AS "Status",COUNT(*) AS "Count",ROUND(SUM(X."StockValue"),0) AS "Value",ROUND(SUM(X."TotalOnHand"),0) AS "Qty"
        FROM (SELECT M."ItemCode",SUM(W."OnHand") AS "TotalOnHand",SUM(W."OnHand"*M."LastPurPrc") AS "StockValue",
            CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN 'NON-MOVING' WHEN MV."TotalOut"<50 THEN 'SLOW' WHEN MV."TotalOut"<500 THEN 'MEDIUM' ELSE 'FAST' END AS "MovementStatus"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN (SELECT N."ItemCode",SUM(N."OutQty") AS "TotalOut" FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode"
            WHERE N."OutQty">0 {date_filter} AND I."U_Unit"='{UNIT}' GROUP BY N."ItemCode") MV ON M."ItemCode"=MV."ItemCode"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' {f} AND W."OnHand">0 GROUP BY M."ItemCode",MV."TotalOut") X
        GROUP BY X."MovementStatus" ORDER BY CASE X."MovementStatus" WHEN 'NON-MOVING' THEN 1 WHEN 'SLOW' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END""")
        for r in rows:
            s2 = r["Status"]
            if s2 not in agg: agg[s2] = {"Status": s2, "Count": 0, "Value": 0, "Qty": 0}
            agg[s2]["Count"] += float(r.get("Count",0) or 0); agg[s2]["Value"] += float(r.get("Value",0) or 0); agg[s2]["Qty"] += float(r.get("Qty",0) or 0)
    order = ["NON-MOVING","SLOW","MEDIUM","FAST"]
    return JSONResponse(content={"data": [agg[s] for s in order if s in agg]})

def get_movers_by_subgroup(days=30, category="FINISHED",
                       date_from=None, date_to=None):
    cat = (category or 'FINISHED').upper()
    cat_f = f"AND G.\"ItmsGrpNam\"='{cat}'"
    if date_from and date_to:
        date_filter = f"AND N.\"DocDate\">='{date_from}' AND N.\"DocDate\"<='{date_to}'"
    else:
        date_filter = f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    agg = {}
    for db in get_dbs(category):
        rows = q(f"""SELECT COALESCE(M."U_Sub_Group",'UNCLASSIFIED') AS "SubGroup",
        COUNT(DISTINCT M."ItemCode") AS "TotalSKUs",
        SUM(CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN 1 ELSE 0 END) AS "NonMovingSKUs",
        SUM(CASE WHEN COALESCE(MV."TotalOut",0)>0 AND COALESCE(MV."TotalOut",0)<50 THEN 1 ELSE 0 END) AS "SlowSKUs",
        SUM(CASE WHEN COALESCE(MV."TotalOut",0)>=50 AND COALESCE(MV."TotalOut",0)<500 THEN 1 ELSE 0 END) AS "MediumSKUs",
        SUM(CASE WHEN COALESCE(MV."TotalOut",0)>=500 THEN 1 ELSE 0 END) AS "FastSKUs",
        ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
        ROUND(SUM(CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN W."OnHand"*M."LastPurPrc" ELSE 0 END),0) AS "StuckValue"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
        JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN (SELECT N."ItemCode",SUM(N."OutQty") AS "TotalOut" FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode"
            WHERE N."OutQty">0 {date_filter} AND I."U_Unit"='{UNIT}' GROUP BY N."ItemCode") MV ON M."ItemCode"=MV."ItemCode"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' {cat_f} AND W."OnHand">0
        GROUP BY M."U_Sub_Group" ORDER BY "StuckValue" DESC,"StockValue" DESC""")
        for r in rows:
            sg = r["SubGroup"]
            if sg not in agg:
                agg[sg] = {"SubGroup": sg, "TotalSKUs": 0, "NonMovingSKUs": 0, "SlowSKUs": 0, "MediumSKUs": 0, "FastSKUs": 0, "StockValue": 0, "StuckValue": 0}
            for k in ["TotalSKUs","NonMovingSKUs","SlowSKUs","MediumSKUs","FastSKUs","StockValue","StuckValue"]:
                agg[sg][k] += float(r.get(k, 0) or 0)
    result = sorted(agg.values(), key=lambda x: (x["StuckValue"], x["StockValue"]), reverse=True)
    return JSONResponse(content={"data": result})

def get_movers(days=30, category=None, subgroup=None,
           status=None, date_from=None, date_to=None):
    f = cf(category)
    sg = f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    if date_from and date_to:
        date_filter = f"AND N.\"DocDate\">='{date_from}' AND N.\"DocDate\"<='{date_to}'"
        day_div = f"(DAYS_BETWEEN(TO_DATE('{date_from}'),TO_DATE('{date_to}'))+1)"
    else:
        date_filter = f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
        day_div = str(days)
    combined = []
    for db in get_dbs(category):
        combined.extend(q(f"""SELECT G."ItmsGrpNam" AS "Category",M."ItemCode",M."ItemName",
        COALESCE(M."U_Sub_Group",'–') AS "SubGroup",
        ROUND(SUM(W."OnHand"),0) AS "TotalOnHand",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
        COALESCE(MV."TotalOut",0) AS "Out{days}d",
        TO_DATE(MV."LastMoveDate") AS "LastMoveDate",
        CASE WHEN MV."LastMoveDate" IS NULL THEN -1 ELSE DAYS_BETWEEN(MV."LastMoveDate",CURRENT_DATE) END AS "DaysSinceMove",
        CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN 'NON-MOVING' WHEN MV."TotalOut"<50 THEN 'SLOW' WHEN MV."TotalOut"<500 THEN 'MEDIUM' ELSE 'FAST' END AS "MovementStatus",
        CASE WHEN COALESCE(MV."TotalOut",0)=0 THEN -1 ELSE ROUND(SUM(W."OnHand")/(MV."TotalOut"/{day_div}),0) END AS "DaysOfStockLeft"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode"
        JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN (SELECT N."ItemCode",SUM(N."OutQty") AS "TotalOut",MAX(CASE WHEN N."OutQty">0 THEN N."DocDate" END) AS "LastMoveDate"
            FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode"
            WHERE N."OutQty">0 {date_filter} AND I."U_Unit"='{UNIT}' GROUP BY N."ItemCode") MV ON M."ItemCode"=MV."ItemCode"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' {f} AND W."OnHand">0 {sg}
        GROUP BY G."ItmsGrpNam",M."ItemCode",M."ItemName",M."U_Sub_Group",MV."TotalOut",MV."LastMoveDate"
        ORDER BY COALESCE(MV."TotalOut",0) ASC,"StockValue" DESC"""))
    if status and status != 'all':
        combined = [r for r in combined if r.get("MovementStatus") == status.upper()]
    return JSONResponse(content={"data": combined})

# ════════════ NOT BILLED ════════════
def get_not_billed_summary():
    result = []
    for period in [30, 60, 90]:
        ts, tv = 0, 0
        for db in ALL_DBS:
            r = q(f"""SELECT COUNT(DISTINCT CASE WHEN B."ItemCode" IS NULL THEN M."ItemCode" END) AS "NotBilledSKUs",
            ROUND(SUM(CASE WHEN B."ItemCode" IS NULL THEN W."OnHand"*M."LastPurPrc" ELSE 0 END),0) AS "NotBilledValue"
            FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
            LEFT JOIN (SELECT DISTINCT L."ItemCode" FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry"
                WHERE I."CANCELED"='N' AND I."DocDate">=ADD_DAYS(CURRENT_DATE,-{period})) B ON M."ItemCode"=B."ItemCode"
            WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' AND G."ItmsGrpNam"='FINISHED' AND W."OnHand">0 AND M."CreateDate"<ADD_DAYS(CURRENT_DATE,-30)""")
            if r: ts += float(r[0].get("NotBilledSKUs",0) or 0); tv += float(r[0].get("NotBilledValue",0) or 0)
        result.append({"Period": f"{period} Days", "NotBilledSKUs": ts, "NotBilledValue": tv})
    return JSONResponse(content={"data": result})

def get_not_billed_by_subgroup(days=30, date_from=None, date_to=None):
    bill_filter = f"AND I.\"DocDate\">='{date_from}' AND I.\"DocDate\"<='{date_to}'" if date_from and date_to else f"AND I.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    agg = {}
    for db in ALL_DBS:
        rows = q(f"""SELECT COALESCE(M."U_Sub_Group",'UNCLASSIFIED') AS "SubGroup",
        COUNT(DISTINCT M."ItemCode") AS "TotalSKUs",
        COUNT(DISTINCT CASE WHEN B."ItemCode" IS NULL THEN M."ItemCode" END) AS "NotBilledSKUs",
        ROUND(SUM(CASE WHEN B."ItemCode" IS NULL THEN W."OnHand"*M."LastPurPrc" ELSE 0 END),0) AS "NotBilledValue",
        ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "TotalValue"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN (SELECT DISTINCT L."ItemCode" FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry"
            WHERE I."CANCELED"='N' {bill_filter}) B ON M."ItemCode"=B."ItemCode"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' AND G."ItmsGrpNam"='FINISHED' AND W."OnHand">0 AND M."CreateDate"<ADD_DAYS(CURRENT_DATE,-30)
        GROUP BY M."U_Sub_Group" ORDER BY "NotBilledValue" DESC""")
        for r in rows:
            sg = r["SubGroup"]
            if sg not in agg: agg[sg] = {"SubGroup": sg, "TotalSKUs": 0, "NotBilledSKUs": 0, "NotBilledValue": 0, "TotalValue": 0}
            for k in ["TotalSKUs","NotBilledSKUs","NotBilledValue","TotalValue"]: agg[sg][k] += float(r.get(k,0) or 0)
    return JSONResponse(content={"data": sorted(agg.values(), key=lambda x: x["NotBilledValue"], reverse=True)})

def get_not_billed(days=30, subgroup=None,
               date_from=None, date_to=None):
    sg = f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    if date_from and date_to:
        bill_filter = f"AND I.\"DocDate\">='{date_from}' AND I.\"DocDate\"<='{date_to}'"
        rc_filter = f"AND I.\"DocDate\">='{date_from}' AND I.\"DocDate\"<='{date_to}'"
    else:
        bill_filter = ""; rc_filter = f"AND I.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    combined = []
    for db in ALL_DBS:
        combined.extend(q(f"""SELECT G."ItmsGrpNam" AS "Category",M."ItemCode",M."ItemName",
        COALESCE(M."U_Sub_Group",'–') AS "SubGroup",
        ROUND(SUM(W."OnHand"),0) AS "CurrentStock",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
        TO_DATE(LB."LastBillDate") AS "LastBillDate",
        CASE WHEN LB."LastBillDate" IS NULL THEN 'NEVER BILLED' ELSE CAST(DAYS_BETWEEN(LB."LastBillDate",CURRENT_DATE) AS VARCHAR)||' days ago' END AS "LastBilledAgo",
        LB."LastCustomer", TO_DATE(M."CreateDate") AS "CreatedOn"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN (SELECT L."ItemCode",MAX(I."DocDate") AS "LastBillDate",MAX(I."CardName") AS "LastCustomer"
            FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry" WHERE I."CANCELED"='N' {bill_filter} GROUP BY L."ItemCode") LB ON M."ItemCode"=LB."ItemCode"
        LEFT JOIN (SELECT DISTINCT L."ItemCode" FROM {db}.OINV I JOIN {db}.INV1 L ON I."DocEntry"=L."DocEntry"
            WHERE I."CANCELED"='N' {rc_filter}) RC ON M."ItemCode"=RC."ItemCode"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' AND G."ItmsGrpNam"='FINISHED'
          AND W."OnHand">0 AND RC."ItemCode" IS NULL AND M."CreateDate"<ADD_DAYS(CURRENT_DATE,-30) {sg}
        GROUP BY G."ItmsGrpNam",M."ItemCode",M."ItemName",M."U_Sub_Group",LB."LastBillDate",LB."LastCustomer",M."CreateDate"
        ORDER BY "StockValue" DESC"""))
    combined.sort(key=lambda x: x.get("StockValue") or 0, reverse=True)
    return JSONResponse(content={"data": combined})

# ════════════ ABC-XYZ ════════════
def abc_inner(db):
    return f"""SELECT M."ItemCode",M."ItemName",COALESCE(M."U_Sub_Group",'UNCLASSIFIED') AS "SubGroup",
    ROUND(SUM(W."OnHand"),0) AS "TotalOnHand",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
    ROW_NUMBER() OVER (ORDER BY SUM(W."OnHand"*M."LastPurPrc") DESC) AS "Rank",
    ROUND(SUM(SUM(W."OnHand"*M."LastPurPrc")) OVER (ORDER BY SUM(W."OnHand"*M."LastPurPrc") DESC)/NULLIF(SUM(SUM(W."OnHand"*M."LastPurPrc")) OVER(),0)*100,2) AS "CumulativePct"
    FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
    WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' AND G."ItmsGrpNam"='FINISHED' AND W."OnHand">0
    GROUP BY M."ItemCode",M."ItemName",M."U_Sub_Group" """

def xyz_cte(db):
    return f"""MONTHLY AS (SELECT N."ItemCode",
        CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-30) THEN 'M1' WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-60) THEN 'M2' ELSE 'M3' END AS "Month",
        SUM(N."OutQty") AS "MonthlyOut"
        FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode" JOIN {db}.OITB G ON I."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE N."OutQty">0 AND N."DocDate">=ADD_DAYS(CURRENT_DATE,-90) AND I."U_Unit"='{UNIT}' AND G."ItmsGrpNam"='FINISHED'
        GROUP BY N."ItemCode",CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-30) THEN 'M1' WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-60) THEN 'M2' ELSE 'M3' END),
    STATS AS (SELECT "ItemCode",AVG("MonthlyOut") AS "AvgOut",STDDEV("MonthlyOut") AS "StdOut" FROM MONTHLY GROUP BY "ItemCode"),
    XYZ_BASE AS (SELECT S."ItemCode",ROUND(S."AvgOut",1) AS "AvgMonthlyOut",
        CASE WHEN S."AvgOut">0 THEN ROUND(S."StdOut"/S."AvgOut",4) ELSE 9999 END AS "CoV",
        CASE WHEN S."AvgOut" IS NULL OR S."AvgOut"=0 THEN 'Z' WHEN S."StdOut"/S."AvgOut"<0.5 THEN 'X' WHEN S."StdOut"/S."AvgOut"<1.0 THEN 'Y' ELSE 'Z' END AS "XYZClass"
        FROM STATS S)"""

def get_abcxyz_summary():
    agg = {}
    for db in ALL_DBS:
        AI = abc_inner(db); XC = xyz_cte(db)
        for r in q(f"""WITH ABC_BASE AS (SELECT "ItemCode","StockValue",CASE WHEN "CumulativePct"<=80 THEN 'A' WHEN "CumulativePct"<=95 THEN 'B' ELSE 'C' END AS "ABCClass" FROM ({AI}) X),
        {XC},COMBINED AS (SELECT A."ABCClass",COALESCE(X."XYZClass",'Z') AS "XYZClass",A."ABCClass"||COALESCE(X."XYZClass",'Z') AS "Combo",A."StockValue" FROM ABC_BASE A LEFT JOIN XYZ_BASE X ON A."ItemCode"=X."ItemCode")
        SELECT "Combo" AS "ABCXYZClass","ABCClass","XYZClass",COUNT(*) AS "SKUs",ROUND(SUM("StockValue"),0) AS "Value" FROM COMBINED GROUP BY "Combo","ABCClass","XYZClass" ORDER BY "ABCClass","XYZClass" """):
            k = r["ABCXYZClass"]
            if k not in agg: agg[k] = {**r, "SKUs": 0, "Value": 0}
            agg[k]["SKUs"] += float(r.get("SKUs",0) or 0); agg[k]["Value"] += float(r.get("Value",0) or 0)
    return JSONResponse(content={"data": sorted(agg.values(), key=lambda x: (x["ABCClass"],x["XYZClass"]))})

def get_abcxyz_by_subgroup():
    agg = {}
    for db in ALL_DBS:
        AI = abc_inner(db); XC = xyz_cte(db)
        for r in q(f"""WITH ABC_BASE AS (SELECT "ItemCode","ItemName","SubGroup","TotalOnHand","StockValue","CumulativePct","Rank",CASE WHEN "CumulativePct"<=80 THEN 'A' WHEN "CumulativePct"<=95 THEN 'B' ELSE 'C' END AS "ABCClass" FROM ({AI}) X),
        {XC},COMBINED AS (SELECT A."SubGroup",COUNT(*) AS "TotalSKUs",ROUND(SUM(A."StockValue"),0) AS "StockValue",
        SUM(CASE WHEN A."ABCClass"='A' THEN 1 ELSE 0 END) AS "A_Count",SUM(CASE WHEN A."ABCClass"='B' THEN 1 ELSE 0 END) AS "B_Count",SUM(CASE WHEN A."ABCClass"='C' THEN 1 ELSE 0 END) AS "C_Count",
        SUM(CASE WHEN COALESCE(X."XYZClass",'Z')='X' THEN 1 ELSE 0 END) AS "X_Count",SUM(CASE WHEN COALESCE(X."XYZClass",'Z')='Y' THEN 1 ELSE 0 END) AS "Y_Count",SUM(CASE WHEN COALESCE(X."XYZClass",'Z')='Z' THEN 1 ELSE 0 END) AS "Z_Count"
        FROM ABC_BASE A LEFT JOIN XYZ_BASE X ON A."ItemCode"=X."ItemCode" GROUP BY A."SubGroup")
        SELECT * FROM COMBINED ORDER BY "StockValue" DESC"""):
            sg = r["SubGroup"]
            if sg not in agg: agg[sg] = {k2: 0 for k2 in ["TotalSKUs","StockValue","A_Count","B_Count","C_Count","X_Count","Y_Count","Z_Count"]}; agg[sg]["SubGroup"] = sg
            for k2 in ["TotalSKUs","StockValue","A_Count","B_Count","C_Count","X_Count","Y_Count","Z_Count"]: agg[sg][k2] += float(r.get(k2,0) or 0)
    return JSONResponse(content={"data": sorted(agg.values(), key=lambda x: x["StockValue"], reverse=True)})

def get_abcxyz(subgroup=None, combo=None):
    sg = f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    combo_f = f"AND A.\"ABCClass\"||COALESCE(X.\"XYZClass\",'Z')='{safe(combo)}'" if combo and combo != 'all' else ""
    combined = []
    for db in ALL_DBS:
        AI = abc_inner(db); XC = xyz_cte(db)
        combined.extend(q(f"""WITH ABC_BASE AS (SELECT "ItemCode","ItemName","SubGroup","TotalOnHand","StockValue","CumulativePct","Rank",CASE WHEN "CumulativePct"<=80 THEN 'A' WHEN "CumulativePct"<=95 THEN 'B' ELSE 'C' END AS "ABCClass" FROM ({AI}) X),
        {XC}
        SELECT A."ItemCode",A."ItemName",A."SubGroup",A."TotalOnHand",A."StockValue",A."CumulativePct",A."Rank",A."ABCClass",
        COALESCE(X."XYZClass",'Z') AS "XYZClass",COALESCE(X."AvgMonthlyOut",0) AS "AvgMonthlyOut",COALESCE(X."CoV",9999) AS "CoV",
        A."ABCClass"||COALESCE(X."XYZClass",'Z') AS "ABCXYZClass"
        FROM ABC_BASE A LEFT JOIN XYZ_BASE X ON A."ItemCode"=X."ItemCode"
        JOIN {db}.OITM M ON A."ItemCode"=M."ItemCode"
        WHERE 1=1 {sg} {combo_f} ORDER BY A."Rank" """))
    return JSONResponse(content={"data": combined})

# ════════════ AGING ════════════
def get_aging(category=None):
    f = cf(category)
    combined = []
    for db in get_dbs(category):
        combined.extend(q(f"""SELECT G."ItmsGrpNam" AS "Category",
        CASE WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=30 THEN '0-30'
             WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=60 THEN '31-60'
             WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=90 THEN '61-90'
             ELSE '90+' END AS "Bucket",
        COUNT(DISTINCT W."ItemCode") AS "Items",ROUND(SUM(W."OnHand"),0) AS "Qty",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "Value"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        JOIN (SELECT N."ItemCode",N."Warehouse",MIN(N."DocDate") AS "FirstDate" FROM {db}.OINM N WHERE N."InQty">0 GROUP BY N."ItemCode",N."Warehouse") FR
             ON W."ItemCode"=FR."ItemCode" AND W."WhsCode"=FR."Warehouse"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' {f} AND W."OnHand">0
        GROUP BY G."ItmsGrpNam",CASE WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=30 THEN '0-30' WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=60 THEN '31-60' WHEN DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<=90 THEN '61-90' ELSE '90+' END
        ORDER BY "Category",MIN(DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE))"""))
    return JSONResponse(content={"data": combined})

def get_aging_drill(bucket="0-30", category=None):
    f = cf(category)
    lo_hi = {"0-30": (0,30), "31-60": (31,60), "61-90": (61,90), "90+": (91,99999)}
    lo, hi = lo_hi.get(bucket, (0,30))
    combined = []
    for db in get_dbs(category):
        combined.extend(q(f"""SELECT G."ItmsGrpNam" AS "Category",W."ItemCode",M."ItemName",W."WhsCode",
        TO_DATE(FR."FirstDate") AS "FirstReceiptDate",DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE) AS "DaysSitting",
        ROUND(W."OnHand",0) AS "Qty",ROUND(W."OnHand"*M."LastPurPrc",0) AS "Value"
        FROM {db}.OITW W JOIN {db}.OITM M ON W."ItemCode"=M."ItemCode" JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        JOIN (SELECT N."ItemCode",N."Warehouse",MIN(N."DocDate") AS "FirstDate" FROM {db}.OINM N WHERE N."InQty">0 GROUP BY N."ItemCode",N."Warehouse") FR
             ON W."ItemCode"=FR."ItemCode" AND W."WhsCode"=FR."Warehouse"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' {f} AND W."OnHand">0
          AND DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)>={lo} AND DAYS_BETWEEN(FR."FirstDate",CURRENT_DATE)<={hi}
        ORDER BY "Value" DESC"""))
    combined.sort(key=lambda x: x.get("Value") or 0, reverse=True)
    return JSONResponse(content={"data": combined})

# ════════════ ITEM TRACE ════════════
def get_trace_subgroups(category="FINISHED"):
    cat = (category or 'FINISHED').upper()
    dbs = [DB3] if cat in ('RAW MATERIAL', 'PACKAGING MATERIAL') else ALL_DBS
    cat_f = f"AND G.\"ItmsGrpNam\"='{cat}'" if cat in ('RAW MATERIAL','PACKAGING MATERIAL') else "AND G.\"ItmsGrpNam\"='FINISHED'"
    seen = set(); result = []
    for db in dbs:
        for r in q(f"""SELECT M."U_Sub_Group" AS "SubGroup",COUNT(DISTINCT M."ItemCode") AS "SKUs",
        ROUND(COALESCE(SUM(W."OnHand"),0),0) AS "OnHand",ROUND(COALESCE(SUM(W."OnHand"*M."LastPurPrc"),0),0) AS "StockValue"
        FROM {db}.OITM M JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN {db}.OITW W ON M."ItemCode"=W."ItemCode"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' {cat_f}
          AND M."U_Sub_Group" IS NOT NULL AND M."U_Sub_Group"!=''
        GROUP BY M."U_Sub_Group" ORDER BY SUM(W."OnHand") DESC NULLS LAST"""):
            if r["SubGroup"] not in seen: seen.add(r["SubGroup"]); result.append(r)
    return JSONResponse(content={"data": result})

def get_trace_items(category="FINISHED", subgroup=None):
    cat = (category or 'FINISHED').upper()
    dbs = [DB3] if cat in ('RAW MATERIAL', 'PACKAGING MATERIAL') else ALL_DBS
    cat_f = f"AND G.\"ItmsGrpNam\"='{cat}'" if cat in ('RAW MATERIAL','PACKAGING MATERIAL') else "AND G.\"ItmsGrpNam\"='FINISHED'"
    sg = f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    combined = []
    for db in dbs:
        combined.extend(q(f"""SELECT M."ItemCode",M."ItemName",COALESCE(M."U_Sub_Group",'–') AS "SubGroup",COALESCE(M."U_Variety",'–') AS "Variety",
        ROUND(COALESCE(SUM(W."OnHand"),0),0) AS "OnHand",ROUND(COALESCE(SUM(W."OnHand"*M."LastPurPrc"),0),0) AS "StockValue"
        FROM {db}.OITM M JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN {db}.OITW W ON M."ItemCode"=W."ItemCode"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' {cat_f} {sg}
        GROUP BY M."ItemCode",M."ItemName",M."U_Sub_Group",M."U_Variety"
        ORDER BY SUM(W."OnHand") DESC NULLS LAST,M."ItemName" """))
    return JSONResponse(content={"data": combined})

def get_trace_header(item=""):
    s = safe(item)
    for db in ALL_DBS:
        r = q(f"""SELECT M."ItemCode",M."ItemName",TO_DATE(M."CreateDate") AS "CreateDate",TO_DATE(M."UpdateDate") AS "UpdateDate",
        M."U_Unit",M."U_Sub_Group" AS "SubGroup",M."U_Variety" AS "Variety",G."ItmsGrpNam" AS "Category",
        ROUND(M."LastPurPrc",4) AS "LastPrice",
        ROUND(COALESCE(SUM(W."OnHand"),0),0) AS "TotalOnHand",
        ROUND(COALESCE(SUM(W."OnOrder"),0),0) AS "TotalOnOrder",ROUND(COALESCE(SUM(W."OnHand"*M."LastPurPrc"),0),2) AS "StockValue"
        FROM {db}.OITM M JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN {db}.OITW W ON M."ItemCode"=W."ItemCode"
        WHERE M."ItemCode"='{s}'
        GROUP BY M."ItemCode",M."ItemName",M."CreateDate",M."UpdateDate",M."U_Unit",M."U_Sub_Group",M."U_Variety",G."ItmsGrpNam",M."LastPurPrc" """)
        if r: return JSONResponse(content={"data": r})
    return JSONResponse(content={"data": []})

def get_trace_log(item="", days=0, month=None):
    s = safe(item)
    if month: date_f = f"AND TO_CHAR(N.\"DocDate\",'YYYY-MM')='{safe(month)}'"
    elif days > 0: date_f = f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    else: date_f = ""
    for db in ALL_DBS:
        r = q(f"""SELECT N."TransNum",N."TransType",CAST(N."BASE_REF" AS VARCHAR(50)) AS "BaseRef",
        TO_DATE(N."DocDate") AS "DocDate",N."CardName",N."JrnlMemo",N."Comments",
        ROUND(N."InQty",3) AS "InQty",ROUND(N."OutQty",3) AS "OutQty",
        ROUND(N."Price",4) AS "Price",ROUND(N."TransValue",2) AS "TransValue",
        N."Warehouse",COALESCE(H."WhsName",N."Warehouse") AS "WhsName",ROUND(N."Balance",3) AS "Balance"
        FROM {db}.OINM N LEFT JOIN {db}.OWHS H ON N."Warehouse"=H."WhsCode"
        WHERE N."ItemCode"='{s}' AND N."TransType" NOT IN (14,16) {date_f}
        ORDER BY N."DocDate" DESC,N."TransNum" DESC""")
        if r: return JSONResponse(content={"data": r})
    return JSONResponse(content={"data": []})

def get_trace_returns(item="", days=0, month=None):
    s = safe(item)
    if month: date_f = f"AND TO_CHAR(N.\"DocDate\",'YYYY-MM')='{safe(month)}'"
    elif days > 0: date_f = f"AND N.\"DocDate\">=ADD_DAYS(CURRENT_DATE,-{days})"
    else: date_f = ""
    for db in ALL_DBS:
        r = q(f"""SELECT N."TransNum",N."TransType",TO_DATE(N."DocDate") AS "DocDate",N."CardName",N."JrnlMemo",N."Comments",
        ROUND(N."InQty",3) AS "ReturnQty",ROUND(N."TransValue",2) AS "TransValue",
        N."Warehouse",COALESCE(H."WhsName",N."Warehouse") AS "WhsName",
        CASE N."TransType" WHEN 14 THEN 'AR Return' WHEN 16 THEN 'AR Credit Note' END AS "ReturnType"
        FROM {db}.OINM N LEFT JOIN {db}.OWHS H ON N."Warehouse"=H."WhsCode"
        WHERE N."ItemCode"='{s}' AND N."TransType" IN (14,16) AND N."InQty">0 {date_f}
        ORDER BY N."DocDate" DESC""")
        if r: return JSONResponse(content={"data": r})
    return JSONResponse(content={"data": []})

def get_trace_disassembly(item="", days=0, month=None):
    s = safe(item); date_f = f"AND TO_CHAR(W.\"StartDate\",'YYYY-MM')='{safe(month)}'" if month else (f"AND W.\"StartDate\">=ADD_DAYS(CURRENT_DATE,-{days})" if days > 0 else "")
    for db in ALL_DBS:
        r = q(f"""SELECT W."DocNum",W."Status",TO_DATE(W."StartDate") AS "StartDate",TO_DATE(W."DueDate") AS "DueDate",
        TO_DATE(W."CloseDate") AS "CloseDate",ROUND(W."PlannedQty",2) AS "PlannedQty",ROUND(W."CmpltQty",2) AS "ActualQty",W."Comments"
        FROM {db}.OWOR W WHERE W."ItemCode"='{s}' AND W."Type"='D' {date_f} ORDER BY W."StartDate" DESC""")
        if r: return JSONResponse(content={"data": r})
    return JSONResponse(content={"data": []})

# ════════════ PLANNING ════════════
def get_planning(subgroup=None):
    sg = f"AND M.\"U_Sub_Group\"='{safe(subgroup)}'" if subgroup else ""
    combined = []
    for db in ALL_DBS:
        combined.extend(q(f"""WITH CONSUMPTION AS (
        SELECT N."ItemCode",
            SUM(CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-30) THEN N."OutQty" ELSE 0 END) AS "Out30d",
            SUM(CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-60) AND N."DocDate"<ADD_DAYS(CURRENT_DATE,-30) THEN N."OutQty" ELSE 0 END) AS "Out30_60d",
            SUM(CASE WHEN N."DocDate">=ADD_DAYS(CURRENT_DATE,-90) THEN N."OutQty" ELSE 0 END) AS "Out90d",
            MAX(CASE WHEN N."OutQty">0 THEN N."DocDate" END) AS "LastMoveDate"
        FROM {db}.OINM N JOIN {db}.OITM I ON N."ItemCode"=I."ItemCode"
        WHERE N."OutQty">0 AND N."DocDate">=ADD_DAYS(CURRENT_DATE,-90) AND I."U_Unit"='{UNIT}'
        GROUP BY N."ItemCode")
        SELECT M."ItemCode",M."ItemName",COALESCE(M."U_Sub_Group",'–') AS "SubGroup",
        ROUND(SUM(W."OnHand"),0) AS "TotalOnHand",ROUND(SUM(W."OnHand"*M."LastPurPrc"),0) AS "StockValue",
        ROUND(COALESCE(C."Out30d",0),0) AS "Out30d",ROUND(COALESCE(C."Out30_60d",0),0) AS "Out30_60d",
        ROUND(COALESCE(C."Out90d",0)/90,1) AS "AvgDailyOut",ROUND(COALESCE(C."Out90d",0)/3,0) AS "AvgMonthlyOut",
        CASE WHEN COALESCE(C."Out90d",0)=0 THEN -1 ELSE ROUND(SUM(W."OnHand")/(COALESCE(C."Out90d",0)/90),0) END AS "DaysOfStockLeft",
        ROUND(CASE WHEN COALESCE(C."Out90d",0)=0 THEN 0 ELSE GREATEST(0,(COALESCE(C."Out90d",0)/90)*30-SUM(W."OnHand")) END,0) AS "SuggestedOrder",
        CASE WHEN COALESCE(C."Out30d",0)=0 AND COALESCE(C."Out30_60d",0)=0 THEN 'FLAT'
             WHEN COALESCE(C."Out30d",0)>COALESCE(C."Out30_60d",0)*1.1 THEN 'RISING'
             WHEN COALESCE(C."Out30d",0)<COALESCE(C."Out30_60d",0)*0.9 THEN 'FALLING'
             ELSE 'STABLE' END AS "Trend",
        TO_DATE(C."LastMoveDate") AS "LastMoveDate",TO_DATE(M."CreateDate") AS "CreateDate"
        FROM {db}.OITM M JOIN {db}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        LEFT JOIN {db}.OITW W ON M."ItemCode"=W."ItemCode"
        LEFT JOIN CONSUMPTION C ON M."ItemCode"=C."ItemCode"
        WHERE G."ItmsGrpNam"='FINISHED' AND M."InvntItem"='Y' AND M."U_Unit"='{UNIT}' AND W."OnHand">0 {sg}
        GROUP BY M."ItemCode",M."ItemName",M."U_Sub_Group",C."Out30d",C."Out30_60d",C."Out90d",C."LastMoveDate",M."CreateDate"
        ORDER BY "DaysOfStockLeft" ASC"""))
    combined.sort(key=lambda x: (x.get("DaysOfStockLeft") if (x.get("DaysOfStockLeft") or -1) >= 0 else 99999))
    return JSONResponse(content={"data": combined})

# ════════════ DEBUG — check what exists in BEVERAGES DB ════════════
def get_debug_rm_pm():
    """Check actual ItmsGrpNam and U_Unit values for RM/PM items in BEVERAGES DB"""
    results = {}
    # 1. What group names exist?
    grp = q(f"""SELECT DISTINCT G."ItmsGrpNam", COUNT(*) AS "Items"
        FROM {DB3}.OITM M JOIN {DB3}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE M."InvntItem"='Y'
        GROUP BY G."ItmsGrpNam" ORDER BY "Items" DESC""")
    results['all_groups_in_BEV_DB'] = grp

    # 2. What U_Unit values exist for RM?
    units = q(f"""SELECT DISTINCT M."U_Unit", G."ItmsGrpNam", COUNT(*) AS "Items"
        FROM {DB3}.OITM M JOIN {DB3}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE M."InvntItem"='Y'
        AND G."ItmsGrpNam" IN ('RAW MATERIAL','PACKAGING MATERIAL')
        GROUP BY M."U_Unit", G."ItmsGrpNam" ORDER BY "Items" DESC""")
    results['rm_pm_unit_values'] = units

    # 3. Stock with no unit filter
    stock = q(f"""SELECT G."ItmsGrpNam", COUNT(DISTINCT W."ItemCode") AS "SKUs",
        ROUND(SUM(W."OnHand"),0) AS "Qty"
        FROM {DB3}.OITW W JOIN {DB3}.OITM M ON W."ItemCode"=M."ItemCode"
        JOIN {DB3}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE M."InvntItem"='Y'
        AND G."ItmsGrpNam" IN ('RAW MATERIAL','PACKAGING MATERIAL')
        AND W."OnHand">0
        GROUP BY G."ItmsGrpNam" """)
    results['rm_pm_stock_no_unit_filter'] = stock

    # 4. With BEVERAGES filter
    stock_bev = q(f"""SELECT G."ItmsGrpNam", COUNT(DISTINCT W."ItemCode") AS "SKUs",
        ROUND(SUM(W."OnHand"),0) AS "Qty"
        FROM {DB3}.OITW W JOIN {DB3}.OITM M ON W."ItemCode"=M."ItemCode"
        JOIN {DB3}.OITB G ON M."ItmsGrpCod"=G."ItmsGrpCod"
        WHERE M."InvntItem"='Y' AND M."U_Unit"='BEVERAGES'
        AND G."ItmsGrpNam" IN ('RAW MATERIAL','PACKAGING MATERIAL')
        AND W."OnHand">0
        GROUP BY G."ItmsGrpNam" """)
    results['rm_pm_stock_with_BEVERAGES_filter'] = stock_bev

    return JSONResponse(content={"data": results})


