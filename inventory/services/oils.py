from .shared import FG_VALID, GIFT_EXCL, PM_VALID, RM_VALID, cf, q, safe, tf, wf

SCHEMAS = {"jivo_oil": "JIVO_OIL_HANADB", "jivo_mart": "JIVO_MART_HANADB"}


def get_schema(s):
    return SCHEMAS.get(s, "JIVO_OIL_HANADB")


def JSONResponse(content):
    return content.get("data", content)
# Owner join helper — handles int/varchar type mismatch
OWN_JOIN = 'LEFT JOIN {db}.OUSR U ON CAST({tbl}."U_Owner" AS VARCHAR(20))=CAST(U."USERID" AS VARCHAR(20))'

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

