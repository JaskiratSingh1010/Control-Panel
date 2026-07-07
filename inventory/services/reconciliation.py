"""Jivo Wellness ↔ Jivo Mart inter-company billing reconciliation.

Jivo Mart (JIVO_MART_HANADB) buys from Jivo Wellness (JIVO_OIL_HANADB — the SAP
company there is "JIVO WELLNESS PVT LTD"). The billing chain and its documents:

    MART (buyer)                         WELLNESS (seller)
    PO   (OPOR) ──(PO# = SO NumAtCard)──► SO   (ORDR)
    GRPO (OPDN)                           A/R Tax Invoice (OINV)
    A/P  (OPCH)                           [Delivery/Challan (ODLN) — usually skipped]

The cross-company join is the Mart PO number, which Wellness's billing team types
into the sales order's "Customer Ref. No." (ORDR.NumAtCard). Within each company the
documents chain natively by base-document references:
    Mart : PO ─(PDN1.BaseType=22)→ GRPO ─(PCH1.BaseType=20)→ A/P
    Well.: SO ─(INV1.BaseType=17)→ A/R Invoice   (Delivery, when used, sits between)

We reconcile the tax-inclusive total across PO / SO / GRPO / A/P / A/R. Amounts are
summed at LINE level via each line table's GTotal (gross incl. tax) — which equals the
document total — so partial fulfilment (one PO split across several GRPOs/invoices, or a
document that mixes several POs) is allocated to the right PO without double-counting.

A chain is:
    MATCHED     — all five amounts present and equal within the rupee tolerance
    MISMATCH    — all five present but they differ  (a real reconciliation problem)
    INCOMPLETE  — one or more downstream documents are missing (often just in-flight)
Only non-matched chains are "broken" and shown by default.
"""

from datetime import date, timedelta

from core.sap_connector import get_connection
from .shared import cv

MART = "JIVO_MART_HANADB"
WELL = "JIVO_OIL_HANADB"

# The seller side (Jivo Wellness) sells both oil and beverages to Jivo Mart through the same
# PO→SO→GRPO→A/P→A/R chain and the same partner-card scoping; only the seller schema differs.
WELL_SCHEMAS = {"oil": "JIVO_OIL_HANADB", "beverages": "JIVO_BEVERAGES_HANADB"}

# Business-partner scoping. Mart's vendor cards for Wellness are named "JIVO … WELLNESS"
# (excludes unrelated names like "KOMAL … WELLNESS"); Wellness's customer cards for Mart
# are "JIVO MART …" of card type Customer.
MART_VENDOR_IS_WELLNESS = "UPPER(C.\"CardName\") LIKE '%WELLNESS%' AND UPPER(C.\"CardName\") LIKE '%JIVO%'"
WELL_CUST_IS_MART = "UPPER(C.\"CardName\") LIKE '%JIVO MART%' AND C.\"CardType\"='C'"

# The billing team's "Customer Ref No." (NumAtCard) holds the Mart PO DocNum, but they wrap it
# in punctuation the SAP team adds to sidestep SAP's duplicate-reference block: trailing dots
# ('626224546.', '626224546..') and sometimes a leading tag ('#626224546', 'PO 626224546.').
# We take the FIRST digit-run anywhere in the trimmed value, so the PO number is recovered
# whatever precedes or follows it (the previous '^'-anchored form missed anything with a leading
# tag). Because the extracted number must still appear in the PO IN-list, a stray digit-run from a
# coded ref ('HR/PO/0122' -> '0122') can't false-match a real 9-digit PO — it just falls through
# as INCOMPLETE. Same extraction is reused for a document's OWN ref (A/R, Delivery) below.
def _refkey(col):
    return "SUBSTR_REGEXPR('[0-9]+' IN TRIM(%s))" % col

SO_KEY = _refkey('S."NumAtCard"')   # Wellness SO's PO ref
AR_KEY = _refkey('I."NumAtCard"')   # A/R invoice's own PO ref (used when it isn't copied from a SO)
DL_KEY = _refkey('D."NumAtCard"')   # Delivery's own PO ref

DEFAULT_MONTHS = 3
TOLERANCE = 1.0   # rupees; differences at or below this are treated as rounding


def _rows(cur, sql):
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [{c: cv(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


def _num(v):
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _sql_date(s, fallback):
    """Accept 'YYYY-MM-DD' (already validated by the view) or fall back to a date object."""
    if s and len(str(s)) == 10 and str(s)[4] == '-' and str(s)[7] == '-':
        return str(s)
    return fallback.strftime('%Y-%m-%d')


def _in_int_list(values):
    """Comma-separated SQL list of integers (e.g. PO DocEntry) — numeric columns."""
    ints = []
    for v in values:
        try:
            ints.append(str(int(v)))
        except (TypeError, ValueError):
            continue
    return ",".join(ints) if ints else "NULL"


def _in_str_list(values):
    """Comma-separated SQL list of quoted strings. Used for matching the PO number against
    ORDR.NumAtCard, which is VARCHAR — unquoted ints would force a numeric cast of every
    NumAtCard and fail on any non-numeric reference."""
    out = []
    for v in values:
        try:
            out.append("'%d'" % int(v))
        except (TypeError, ValueError):
            continue
    return ",".join(out) if out else "NULL"


def get_reconciliation(date_from=None, date_to=None, schema="oil"):
    # Seller schema: oil (default) or beverages. Buyer (MART) and the whole chain/scoping
    # are identical — only which Wellness company we pull SO/A/R (and its ties) from changes.
    WELL = WELL_SCHEMAS.get(schema, "JIVO_OIL_HANADB")
    today = date.today()
    d_to = _sql_date(date_to, today)
    d_from = _sql_date(date_from, today - timedelta(days=DEFAULT_MONTHS * 31))

    conn = get_connection()
    try:
        cur = conn.cursor()

        # 1) Anchor: Mart POs raised on the Wellness vendor in the window.
        pos = _rows(cur, f"""
            SELECT P."DocEntry" AS "Entry", P."DocNum" AS "PONum",
                   TO_VARCHAR(P."DocDate",'YYYY-MM-DD') AS "PODate",
                   P."DocTotal" AS "POTotal", C."CardName" AS "Vendor"
            FROM {MART}.OPOR P JOIN {MART}.OCRD C ON P."CardCode"=C."CardCode"
            WHERE {MART_VENDOR_IS_WELLNESS} AND P."CANCELED"='N'
              AND P."DocDate" BETWEEN '{d_from}' AND '{d_to}'
            ORDER BY P."DocDate" DESC, P."DocNum" DESC""")

        if not pos:
            cur.close()
            return {"date_from": d_from, "date_to": d_to, "tolerance": TOLERANCE,
                    "summary": _summary([]), "chains": []}

        entries = _in_int_list(p["Entry"] for p in pos)
        ponums = _in_str_list(p["PONum"] for p in pos)

        # Each side is grouped by (PO key, document number) so we return the individual
        # source documents (DocNum + its date + its line-level GTotal) behind every amount —
        # the UI makes the amounts clickable to reveal these reference numbers and dates.

        # 2) Wellness SO documents, keyed by the PO number in NumAtCard.
        so = _agg_docs(cur, f"""
            SELECT {SO_KEY} AS "K", S."DocNum" AS "Num", TO_VARCHAR(S."DocDate",'YYYY-MM-DD') AS "Dt",
                   ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {WELL}.ORDR S JOIN {WELL}.RDR1 L ON S."DocEntry"=L."DocEntry"
            JOIN {WELL}.OCRD C ON S."CardCode"=C."CardCode"
            WHERE S."CANCELED"='N' AND {WELL_CUST_IS_MART}
              AND {SO_KEY} IN ({ponums})
            GROUP BY {SO_KEY}, S."DocNum", S."DocDate" """)

        # 3) Wellness A/R invoices. Allocate to the PO via the SO they copy from (INV1.BaseType=17);
        #    LEFT JOIN so an invoice raised DIRECTLY (not copied from a SO — the billing team then
        #    types the dotted PO number into the invoice's own NumAtCard) still links via AR_KEY.
        #    Scoped to Mart customers so a stray digit-run can't pull in an unrelated invoice.
        ar = _agg_docs(cur, f"""
            SELECT COALESCE({SO_KEY}, {AR_KEY}) AS "K", I."DocNum" AS "Num", TO_VARCHAR(I."DocDate",'YYYY-MM-DD') AS "Dt",
                   ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {WELL}.OINV I JOIN {WELL}.INV1 L ON I."DocEntry"=L."DocEntry"
            JOIN {WELL}.OCRD C ON I."CardCode"=C."CardCode"
            LEFT JOIN {WELL}.ORDR S ON L."BaseType"=17 AND L."BaseEntry"=S."DocEntry"
            WHERE I."CANCELED"='N' AND {WELL_CUST_IS_MART}
              AND ({SO_KEY} IN ({ponums}) OR (S."DocEntry" IS NULL AND {AR_KEY} IN ({ponums})))
            GROUP BY COALESCE({SO_KEY}, {AR_KEY}), I."DocNum", I."DocDate" """)

        # 3b) Delivery/Challan documents (optional node — informational only). Same SO-link-or-own-ref
        #     allocation as A/R so directly-raised deliveries with a dotted PO ref are still caught.
        dl = _agg_docs(cur, f"""
            SELECT COALESCE({SO_KEY}, {DL_KEY}) AS "K", D."DocNum" AS "Num", TO_VARCHAR(D."DocDate",'YYYY-MM-DD') AS "Dt",
                   ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {WELL}.ODLN D JOIN {WELL}.DLN1 L ON D."DocEntry"=L."DocEntry"
            JOIN {WELL}.OCRD C ON D."CardCode"=C."CardCode"
            LEFT JOIN {WELL}.ORDR S ON L."BaseType"=17 AND L."BaseEntry"=S."DocEntry"
            WHERE D."CANCELED"='N' AND {WELL_CUST_IS_MART}
              AND ({SO_KEY} IN ({ponums}) OR (S."DocEntry" IS NULL AND {DL_KEY} IN ({ponums})))
            GROUP BY COALESCE({SO_KEY}, {DL_KEY}), D."DocNum", D."DocDate" """)

        # 4) Mart GRPO documents, allocated to the PO by base reference (PDN1.BaseType=22).
        grpo = _agg_docs(cur, f"""
            SELECT L."BaseEntry" AS "K", D."DocNum" AS "Num", TO_VARCHAR(D."DocDate",'YYYY-MM-DD') AS "Dt",
                   ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {MART}.OPDN D JOIN {MART}.PDN1 L ON D."DocEntry"=L."DocEntry"
            WHERE D."CANCELED"='N' AND L."BaseType"=22 AND L."BaseEntry" IN ({entries})
            GROUP BY L."BaseEntry", D."DocNum", D."DocDate" """)

        # 5) Mart A/P documents, allocated to the PO via the GRPO line each A/P line copies
        #    from (PCH1.BaseType=20 → PDN1 → PO). Two-hop keeps split A/Ps exact.
        ap = _agg_docs(cur, f"""
            SELECT G."BaseEntry" AS "K", H."DocNum" AS "Num", TO_VARCHAR(H."DocDate",'YYYY-MM-DD') AS "Dt",
                   ROUND(SUM(A."GTotal"),2) AS "Amt"
            FROM {MART}.OPCH H JOIN {MART}.PCH1 A ON H."DocEntry"=A."DocEntry"
            JOIN {MART}.PDN1 G ON A."BaseEntry"=G."DocEntry" AND A."BaseLine"=G."LineNum"
            WHERE H."CANCELED"='N' AND A."BaseType"=20 AND G."BaseType"=22
              AND G."BaseEntry" IN ({entries})
            GROUP BY G."BaseEntry", H."DocNum", H."DocDate" """)

        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    chains = []
    for p in pos:
        ponum = str(p["PONum"]); entry = str(p["Entry"])
        node = _build_nodes(p, ponum, entry, so, ar, grpo, ap, dl)
        status, detail = _classify(_num(p["POTotal"]), node)
        chains.append({
            "po": p["PONum"], "po_date": p["PODate"], "vendor": p["Vendor"],
            "po_total": _num(p["POTotal"]),
            "po_docs": [{"num": str(p["PONum"]), "date": p["PODate"], "amt": _num(p["POTotal"])}],
            "so": node["so"], "so_cnt": node["so_cnt"], "so_docs": node["so_docs"],
            "grpo": node["grpo"], "grpo_cnt": node["grpo_cnt"], "grpo_docs": node["grpo_docs"],
            "ap": node["ap"], "ap_cnt": node["ap_cnt"], "ap_docs": node["ap_docs"],
            "ar": node["ar"], "ar_cnt": node["ar_cnt"], "ar_docs": node["ar_docs"],
            "delivery": node["dl"], "delivery_cnt": node["dl_cnt"], "delivery_docs": node["dl_docs"],
            "status": status, "detail": detail,
        })

    return {"date_from": d_from, "date_to": d_to, "tolerance": TOLERANCE,
            "summary": _summary(chains), "chains": chains}


def _agg_docs(cur, sql):
    """Return {key(str): [{'num': str, 'amt': float}, ...]} — the individual documents
    (with their tax-inclusive totals) behind each PO's amount for that node."""
    out = {}
    for r in _rows(cur, sql):
        k = r.get("K")
        if k is None:
            continue
        out.setdefault(str(k).strip(), []).append(
            {"num": str(r.get("Num")), "date": r.get("Dt"), "amt": _num(r.get("Amt"))})
    return out


def _build_nodes(p, ponum, entry, so, ar, grpo, ap, dl):
    def side(store, key):
        docs = store.get(key)
        if not docs:
            return (None, 0, [])
        docs = sorted(docs, key=lambda d: d["num"])
        return (round(sum(d["amt"] for d in docs), 2), len(docs), docs)
    so_a, so_c, so_d = side(so, ponum)
    ar_a, ar_c, ar_d = side(ar, ponum)
    dl_a, dl_c, dl_d = side(dl, ponum)
    grpo_a, grpo_c, grpo_d = side(grpo, entry)
    ap_a, ap_c, ap_d = side(ap, entry)
    return {"so": so_a, "so_cnt": so_c, "so_docs": so_d,
            "ar": ar_a, "ar_cnt": ar_c, "ar_docs": ar_d,
            "dl": dl_a, "dl_cnt": dl_c, "dl_docs": dl_d,
            "grpo": grpo_a, "grpo_cnt": grpo_c, "grpo_docs": grpo_d,
            "ap": ap_a, "ap_cnt": ap_c, "ap_docs": ap_d}


def _classify(po_total, node):
    """Compare PO/SO/GRPO/A/P/A/R (Delivery excluded — optional). Returns (status, detail)."""
    labels = [("SO", node["so"]), ("GRPO", node["grpo"]), ("A/P", node["ap"]), ("A/R", node["ar"])]
    missing = [name for name, amt in labels if amt is None]
    if missing:
        return "INCOMPLETE", "Missing " + ", ".join(missing)
    present = [po_total] + [amt for _, amt in labels]
    spread = max(present) - min(present)
    if spread <= TOLERANCE:
        return "MATCHED", None
    return "MISMATCH", "Spread ₹%s" % format(round(spread), ",")


def _summary(chains):
    s = {"total": len(chains), "matched": 0, "mismatch": 0, "incomplete": 0,
         "mismatch_value": 0.0}
    for c in chains:
        st = c["status"]
        if st == "MATCHED":
            s["matched"] += 1
        elif st == "MISMATCH":
            s["mismatch"] += 1
            amts = [c["po_total"], c["so"], c["grpo"], c["ap"], c["ar"]]
            amts = [a for a in amts if a is not None]
            s["mismatch_value"] += round(max(amts) - min(amts), 2)
        else:
            s["incomplete"] += 1
    s["mismatch_value"] = round(s["mismatch_value"], 2)
    return s


# ── BP ledgers (Mart / Wellness) — the second reconciliation tab ─────────────
# Each side is the counterparty's business-partner ledger: JDT1 (journal) lines whose BP account
# (ShortName) is the counterparty card, pivoted by ORIGIN (the originating document type, from
# JDT1.TransType). Balance = Debit − Credit (LC), matching the manual pivot in MART LEDGER.xlsx.
# Cancellation reversals are dropped by their line memo (the "Remarks" column) per the user's rule.
# JDT1.ShortName = OCRD.CardCode restricts to the BP's lines (G/L-account lines don't join).
LEDGER_ORIGINS = {13: 'IN', 14: 'CN', 24: 'RC', 18: 'PU', 19: 'PC', 46: 'PS', 30: 'JE',
                  15: 'DN', 16: 'RN', 20: 'GR', 21: 'GT', 22: 'PD', 23: 'PO', 17: 'DO'}

# Counterparty scoping for the ledgers (BP account whose ledger we build).
MART_LEDGER_BP = MART_VENDOR_IS_WELLNESS + " AND C.\"CardType\"='S'"   # Wellness as a Mart vendor
WELL_LEDGER_BP = WELL_CUST_IS_MART                                     # Mart as a Wellness customer


def _origin_of(tt):
    try:
        n = int(tt)
    except (TypeError, ValueError):
        return str(tt)
    return LEDGER_ORIGINS.get(n, str(n))


def _ledger_rows(cur, schema, bp_predicate, d_from, d_to):
    """One BP ledger pivoted by ORIGIN: {rows:[{origin,debit,credit,balance,count}], total}.
    A cancellation is TWO ledger entries — the original document and its reversal — and BOTH are
    excluded: any journal line whose source marketing document is cancelled/a-cancellation
    (CANCELED<>'N' on OPCH/ORPC/OINV/ORIN, linked by TransId) is dropped, so the original and its
    reversal go together. The memo filter is kept as a safety net for non-invoice reversals."""
    sql = f"""
        SELECT J."TransType" AS "TT",
               ROUND(SUM(J."Debit"),2)  AS "DEB",
               ROUND(SUM(J."Credit"),2) AS "CRED",
               COUNT(*) AS "N"
        FROM {schema}.JDT1 J
        JOIN {schema}.OCRD C ON C."CardCode" = J."ShortName"
        LEFT JOIN {schema}.OJDT O ON O."TransId" = J."TransId"
        WHERE {bp_predicate}
          AND J."RefDate" BETWEEN '{d_from}' AND '{d_to}'
          AND NOT (UPPER(COALESCE(J."LineMemo",'')) LIKE '%CANCEL%'
                   OR UPPER(COALESCE(O."Memo",'')) LIKE '%CANCEL%')
          AND NOT EXISTS (
              SELECT 1 FROM (
                  SELECT "TransId" AS "TX" FROM {schema}.OPCH WHERE "CANCELED" <> 'N'
                  UNION SELECT "TransId" FROM {schema}.ORPC WHERE "CANCELED" <> 'N'
                  UNION SELECT "TransId" FROM {schema}.OINV WHERE "CANCELED" <> 'N'
                  UNION SELECT "TransId" FROM {schema}.ORIN WHERE "CANCELED" <> 'N'
              ) X WHERE X."TX" = J."TransId")
        GROUP BY J."TransType" """
    agg = {}
    for r in _rows(cur, sql):
        o = _origin_of(r.get("TT"))
        a = agg.setdefault(o, {"origin": o, "debit": 0.0, "credit": 0.0, "count": 0})
        a["debit"] += _num(r.get("DEB")); a["credit"] += _num(r.get("CRED"))
        a["count"] += int(r.get("N") or 0)
    rows, tot = [], {"debit": 0.0, "credit": 0.0}
    for o in sorted(agg):
        a = agg[o]
        a["debit"] = round(a["debit"], 2); a["credit"] = round(a["credit"], 2)
        a["balance"] = round(a["debit"] - a["credit"], 2)
        tot["debit"] += a["debit"]; tot["credit"] += a["credit"]
        rows.append(a)
    tot["debit"] = round(tot["debit"], 2); tot["credit"] = round(tot["credit"], 2)
    tot["balance"] = round(tot["debit"] - tot["credit"], 2)
    return {"rows": rows, "total": tot}


def get_ledgers(date_from=None, date_to=None, schema="oil"):
    """Mart & Wellness BP ledgers (pivoted by ORIGIN) for the reconciliation 'Ledgers' tab.
    Mart = JIVO WELLNESS vendor's ledger in JIVO_MART_HANADB; Wellness = JIVO MART customer's
    ledger in the seller schema (oil default / beverages). Same date window as the main tab."""
    WELL = WELL_SCHEMAS.get(schema, "JIVO_OIL_HANADB")
    today = date.today()
    d_to = _sql_date(date_to, today)
    d_from = _sql_date(date_from, today - timedelta(days=DEFAULT_MONTHS * 31))
    conn = get_connection()
    try:
        cur = conn.cursor()
        mart = _ledger_rows(cur, MART, MART_LEDGER_BP, d_from, d_to)
        well = _ledger_rows(cur, WELL, WELL_LEDGER_BP, d_from, d_to)
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"date_from": d_from, "date_to": d_to, "company": schema,
            "mart": mart, "wellness": well}
