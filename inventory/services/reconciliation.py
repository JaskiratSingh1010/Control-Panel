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

import re
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
AR_KEY = _refkey('I."NumAtCard"')   # A/R invoice's own PO ref (used when the SO ref doesn't name the PO — blank SO ref or a directly-raised invoice)
DL_KEY = _refkey('D."NumAtCard"')   # Delivery's own PO ref (same fallback as AR_KEY)

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
                   P."DocTotal" AS "POTotal", C."CardName" AS "Vendor",
                   P."NumAtCard" AS "PORef"
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

        # 3) Wellness A/R invoices. Allocate to the PO by the SO's PO ref when that ref actually
        #    names a PO in the anchor list (INV1.BaseType=17 → ORDR.NumAtCard); OTHERWISE by the
        #    invoice's OWN NumAtCard. The billing team very often leaves the SO's NumAtCard blank and
        #    types the (dotted) PO onto the A/R invoice itself — so an invoice that copies from such
        #    an SO must STILL fall back to its own ref. The earlier `S.DocEntry IS NULL` guard only
        #    allowed that fallback for invoices raised with NO sales order at all, silently dropping
        #    these as a false "Missing A/R" (verified: e.g. INV 626050312 → SO with blank ref, PO on
        #    the invoice). Scoped to Mart customers so a stray digit-run can't pull an unrelated one.
        ar_key = f"CASE WHEN {SO_KEY} IN ({ponums}) THEN {SO_KEY} ELSE {AR_KEY} END"
        ar = _agg_docs(cur, f"""
            SELECT {ar_key} AS "K", I."DocNum" AS "Num", TO_VARCHAR(I."DocDate",'YYYY-MM-DD') AS "Dt",
                   ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {WELL}.OINV I JOIN {WELL}.INV1 L ON I."DocEntry"=L."DocEntry"
            JOIN {WELL}.OCRD C ON I."CardCode"=C."CardCode"
            LEFT JOIN {WELL}.ORDR S ON L."BaseType"=17 AND L."BaseEntry"=S."DocEntry"
            WHERE I."CANCELED"='N' AND {WELL_CUST_IS_MART}
              AND ({SO_KEY} IN ({ponums}) OR {AR_KEY} IN ({ponums}))
            GROUP BY {ar_key}, I."DocNum", I."DocDate" """)

        # 3a) Second-chance: A/R invoices that carry the PO ONLY in their Remarks (OINV.Comments),
        #     with a blank Ref No AND a blank-ref SO — so neither structured key above catches them
        #     (verified: INV 626050295, Ref='' , Comments='Based On Sales Orders 1726056658.\rPO.
        #     526224533'). Matched against the exact anchor PO set (whole token), NOT a first-digit-run
        #     (which would grab the SO#). Bounded to the window + Mart customers; skips already-matched.
        po_re = _po_token_re(pos)
        _rescue_by_remarks(cur, f"""
            SELECT I."DocNum" AS "Num", TO_VARCHAR(I."DocDate",'YYYY-MM-DD') AS "Dt",
                   I."Comments" AS "Cmt", ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {WELL}.OINV I JOIN {WELL}.INV1 L ON I."DocEntry"=L."DocEntry"
            JOIN {WELL}.OCRD C ON I."CardCode"=C."CardCode"
            WHERE I."CANCELED"='N' AND {WELL_CUST_IS_MART}
              AND COALESCE(I."Comments",'') <> ''
              AND I."DocDate" BETWEEN '{d_from}' AND '{d_to}'
            GROUP BY I."DocNum", I."DocDate", I."Comments" """, ar, po_re)

        # 3b) Delivery/Challan documents (optional node — informational only). Same SO-ref-when-it-
        #     names-a-PO-else-own-ref allocation as A/R, so deliveries copied from a blank-ref SO (or
        #     raised directly) with a dotted PO ref are still caught.
        dl_key = f"CASE WHEN {SO_KEY} IN ({ponums}) THEN {SO_KEY} ELSE {DL_KEY} END"
        dl = _agg_docs(cur, f"""
            SELECT {dl_key} AS "K", D."DocNum" AS "Num", TO_VARCHAR(D."DocDate",'YYYY-MM-DD') AS "Dt",
                   ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {WELL}.ODLN D JOIN {WELL}.DLN1 L ON D."DocEntry"=L."DocEntry"
            JOIN {WELL}.OCRD C ON D."CardCode"=C."CardCode"
            LEFT JOIN {WELL}.ORDR S ON L."BaseType"=17 AND L."BaseEntry"=S."DocEntry"
            WHERE D."CANCELED"='N' AND {WELL_CUST_IS_MART}
              AND ({SO_KEY} IN ({ponums}) OR {DL_KEY} IN ({ponums}))
            GROUP BY {dl_key}, D."DocNum", D."DocDate" """)

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

        # 5a) Last-resort A/R match (needs `ap`): invoices with NO PO anywhere (blank Ref, no PO in
        #     Remarks) that copy from an SO shared across POs — attach by a UNIQUE A/P-amount match
        #     among the POs that SO's other invoices name. See _rescue_by_so_amount for the guard.
        ponum_set = {str(p["PONum"]) for p in pos}
        _rescue_by_so_amount(cur, f"""
            SELECT I."DocNum" AS "Num", TO_VARCHAR(I."DocDate",'YYYY-MM-DD') AS "Dt",
                   L."BaseEntry" AS "SOE", {AR_KEY} AS "Own", I."Comments" AS "Cmt",
                   ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {WELL}.OINV I JOIN {WELL}.INV1 L ON I."DocEntry"=L."DocEntry"
            JOIN {WELL}.OCRD C ON I."CardCode"=C."CardCode"
            WHERE I."CANCELED"='N' AND {WELL_CUST_IS_MART}
              AND L."BaseType"=17 AND I."DocDate" BETWEEN '{d_from}' AND '{d_to}'
            GROUP BY I."DocNum", I."DocDate", L."BaseEntry", {AR_KEY}, I."Comments" """,
            pos, ponum_set, po_re, ar, ap)

        # 5b) Absolute last resort (needs `ap`): A/R invoices that reference the PO NOWHERE and don't
        #     sit on its SO — attach by a GLOBALLY-UNIQUE exact A/P amount only (see the helper; a
        #     colliding amount is never guessed). Verified: PO 426224515 → 626040245 + 626040246.
        _rescue_by_unique_amount(cur, f"""
            SELECT I."DocNum" AS "Num", TO_VARCHAR(I."DocDate",'YYYY-MM-DD') AS "Dt",
                   ROUND(SUM(L."GTotal"),2) AS "Amt"
            FROM {WELL}.OINV I JOIN {WELL}.INV1 L ON I."DocEntry"=L."DocEntry"
            JOIN {WELL}.OCRD C ON I."CardCode"=C."CardCode"
            WHERE I."CANCELED"='N' AND {WELL_CUST_IS_MART}
              AND I."DocDate" BETWEEN '{d_from}' AND '{d_to}'
            GROUP BY I."DocNum", I."DocDate" """, pos, ar, ap)

        # 5c) Reverse cross-company link — runs LAST so the buyer's own record is authoritative. Some
        #     Mart POs (and their GRPO/A-P) put the WELLNESS A/R invoice number in their OWN NumAtCard
        #     instead of Wellness's SO carrying the Mart PO. Extract every 8+ digit run from each PO
        #     ref; the SQL keeps only real Mart-customer A/R DocNums (junk can't false-match). Attaches
        #     an unmatched invoice, and OVERRIDES the invoice's own (mistyped) ref by stealing it to the
        #     naming PO when that PO is owed the amount — verified 626224566←626050745, 526224556←626058189
        #     (stolen from the ₹55-lakh PO 526224557 the invoice's ref mistyped 556→557).
        ar_ref_to_po = {}
        for p in pos:
            for tok in re.findall(r'\d{8,}', str(p.get("PORef") or "")):
                ar_ref_to_po.setdefault(tok, set()).add(str(p["PONum"]))
        if ar_ref_to_po:
            inv_in = ",".join("'%s'" % n for n in ar_ref_to_po)
            _rescue_by_po_ref(cur, f"""
                SELECT I."DocNum" AS "Num", TO_VARCHAR(I."DocDate",'YYYY-MM-DD') AS "Dt",
                       ROUND(SUM(L."GTotal"),2) AS "Amt"
                FROM {WELL}.OINV I JOIN {WELL}.INV1 L ON I."DocEntry"=L."DocEntry"
                JOIN {WELL}.OCRD C ON I."CardCode"=C."CardCode"
                WHERE I."CANCELED"='N' AND {WELL_CUST_IS_MART} AND I."DocNum" IN ({inv_in})
                GROUP BY I."DocNum", I."DocDate" """, ar_ref_to_po, ar, ap, pos)

        # 6) A/R Credit Memos (ORIN) that reverse a Wellness A/R invoice — keyed by the BASE
        #    invoice's DocNum (RIN1.BaseType=13 → OINV). An invoice fully reversed by a credit note
        #    never gets a Mart GRPO/A/P (the goods came back), so it shows as "pending"; attaching
        #    its credit note explains the gap (number + amount + date). Scoped to the exact A/R
        #    invoices already matched to a PO (built after all rescues), so a later-dated credit note
        #    can never fall outside the window.
        _ar_invs = sorted({d["num"] for docs in ar.values() for d in docs})
        cn_by_inv = {}
        if _ar_invs:
            cn_by_inv = _agg_docs(cur, f"""
                SELECT BI."DocNum" AS "K", R."DocNum" AS "Num",
                       TO_VARCHAR(R."DocDate",'YYYY-MM-DD') AS "Dt", ROUND(SUM(L."GTotal"),2) AS "Amt"
                FROM {WELL}.ORIN R JOIN {WELL}.RIN1 L ON R."DocEntry"=L."DocEntry"
                JOIN {WELL}.OINV BI ON L."BaseType"=13 AND L."BaseEntry"=BI."DocEntry"
                WHERE R."CANCELED"='N' AND BI."DocNum" IN ({_in_int_list(_ar_invs)})
                GROUP BY BI."DocNum", R."DocNum", R."DocDate" """)

        # 7) Base-document reference for each node — "kiske reference pe kata" — attached to every
        #    doc as d['ref']: GRPO←PO (PDN1.BaseType=22→OPOR), A/P←GRPO (PCH1.BaseType=20→OPDN),
        #    A/R←SO (INV1.BaseType=17→ORDR), Credit Note←Invoice (already the store key). Scoped by
        #    the exact DocNums already collected, so no SUM is touched — pure lookups.
        _gn, _pn, _an = _doc_nums(grpo), _doc_nums(ap), _ar_invs
        if _gn:
            _attach_ref(cur, grpo, f"""
                SELECT D."DocNum" AS "Num", P."DocNum" AS "Ref"
                FROM {MART}.OPDN D JOIN {MART}.PDN1 L ON D."DocEntry"=L."DocEntry"
                JOIN {MART}.OPOR P ON L."BaseType"=22 AND L."BaseEntry"=P."DocEntry"
                WHERE D."DocNum" IN ({_in_int_list(_gn)})
                GROUP BY D."DocNum", P."DocNum" """)
        if _pn:
            _attach_ref(cur, ap, f"""
                SELECT H."DocNum" AS "Num", GD."DocNum" AS "Ref"
                FROM {MART}.OPCH H JOIN {MART}.PCH1 A ON H."DocEntry"=A."DocEntry"
                JOIN {MART}.PDN1 G ON A."BaseEntry"=G."DocEntry" AND A."BaseLine"=G."LineNum"
                JOIN {MART}.OPDN GD ON G."DocEntry"=GD."DocEntry"
                WHERE H."DocNum" IN ({_in_int_list(_pn)}) AND A."BaseType"=20
                GROUP BY H."DocNum", GD."DocNum" """)
        if _an:
            _attach_ref(cur, ar, f"""
                SELECT I."DocNum" AS "Num", S."DocNum" AS "Ref"
                FROM {WELL}.OINV I JOIN {WELL}.INV1 L ON I."DocEntry"=L."DocEntry"
                JOIN {WELL}.ORDR S ON L."BaseType"=17 AND L."BaseEntry"=S."DocEntry"
                WHERE I."DocNum" IN ({_in_int_list(_an)})
                GROUP BY I."DocNum", S."DocNum" """)

        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    chains = []
    for p in pos:
        ponum = str(p["PONum"]); entry = str(p["Entry"])
        node = _build_nodes(p, ponum, entry, so, ar, grpo, ap, dl, cn_by_inv)
        status, detail = _classify(_num(p["POTotal"]), node)
        chains.append({
            "po": p["PONum"], "po_date": p["PODate"], "vendor": p["Vendor"],
            "po_total": _num(p["POTotal"]),
            "po_docs": [{"num": str(p["PONum"]), "date": p["PODate"], "amt": _num(p["POTotal"])}],
            "so": node["so"], "so_cnt": node["so_cnt"], "so_docs": node["so_docs"],
            "grpo": node["grpo"], "grpo_cnt": node["grpo_cnt"], "grpo_docs": node["grpo_docs"],
            "ap": node["ap"], "ap_cnt": node["ap_cnt"], "ap_docs": node["ap_docs"],
            "ar": node["ar"], "ar_cnt": node["ar_cnt"], "ar_docs": node["ar_docs"],
            "cn": node["cn"], "cn_cnt": node["cn_cnt"], "cn_docs": node["cn_docs"],
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


def _doc_nums(store):
    """Every distinct DocNum across a doc store (for scoping a follow-up ref query precisely)."""
    return sorted({d["num"] for docs in store.values() for d in docs})


def _attach_ref(cur, store, sql):
    """Attach ``d['ref']`` — the base document number(s) each doc was copied from — to every doc in
    ``store``. ``sql`` yields {Num, Ref}; a doc copied from more than one base gets them comma-joined.
    Ref is a property of the document itself (its base), so it's matched globally by DocNum."""
    ref = {}
    for r in _rows(cur, sql):
        base = r.get("Ref")
        if base is None:
            continue
        ref.setdefault(str(r.get("Num")), set()).add(str(base))
    for docs in store.values():
        for d in docs:
            s = ref.get(str(d["num"]))
            if s:
                d["ref"] = ", ".join(sorted(s))


def _po_token_re(pos):
    """A regex matching ANY anchor PO number as a whole token (digit boundaries), or None.
    Used to find a PO written in a document's free-text Remarks. Boundaries stop a PO from
    matching inside a longer number — e.g. the auto 'Based On Sales Orders <10-digit SO>' text
    whose first digit-run is the SO, not the PO (so a first-digit-run extraction would misfire)."""
    nums = sorted({str(p["PONum"]) for p in pos}, key=len, reverse=True)
    if not nums:
        return None
    return re.compile(r'(?<!\d)(?:%s)(?!\d)' % '|'.join(re.escape(n) for n in nums))


def _rescue_by_remarks(cur, sql, store, po_re):
    """Second-chance match for documents that carry the PO ONLY in their free-text Remarks
    (e.g. a blank Ref No with 'PO. 526224533' typed into OINV.Comments), not in the Ref No.
    ``sql`` returns candidate docs {Num, Dt, Cmt, Amt}; a candidate whose DocNum isn't already
    matched and whose Comments names an anchor PO (whole-token, via ``po_re``) is appended to
    ``store`` under that PO. Mutates ``store`` in place; no-op if po_re is None."""
    if po_re is None:
        return
    taken = {d["num"] for docs in store.values() for d in docs}
    for r in _rows(cur, sql):
        num = str(r.get("Num"))
        if num in taken:
            continue
        m = po_re.search(str(r.get("Cmt") or ""))
        if not m:
            continue
        po = m.group(0)
        store.setdefault(po, []).append({"num": num, "date": r.get("Dt"), "amt": _num(r.get("Amt"))})
        taken.add(num)


def _rescue_by_po_ref(cur, sql, ar_ref_to_po, ar, ap, pos):
    """Reverse cross-company link + authoritative override — runs LAST so the buyer's own record
    wins. Some Mart POs carry the WELLNESS A/R invoice number in their OWN NumAtCard (and the GRPO/
    A-P copy it) instead of Wellness's SO carrying the Mart PO number. ``ar_ref_to_po`` = {invoice
    DocNum: {po}} from the POs' refs; ``sql`` returns those invoices {Num, Dt, Amt}. The SQL scopes
    to real Mart-customer A/R DocNums so a junk ref can't invent a match. For each invoice named by
    exactly one PO:
      • unmatched            → attach to that PO;
      • already on ANOTHER PO → STEAL it to the naming PO, but ONLY when that PO genuinely expects
        the amount (has an unclaimed A/P line ≈ it). This overrides the invoice's OWN (mistyped)
        ref, while the amount guard stops a stray PO ref from hijacking a legitimate match.
    Mutates ``ar``; returns count. Verified: 626224566 ← 626050745 (attach); 526224556 ← 626058189
    stolen from the ₹55-lakh PO 526224557 whose ref the invoice mistyped 556→557 (₹20,286 confirms)."""
    po_by_entry = {str(p["Entry"]): str(p["PONum"]) for p in pos}
    ap_rem = {}                              # unclaimed A/P amounts per PO (nearest rupee)
    for entry, docs in ap.items():
        pn = po_by_entry.get(str(entry))
        if pn:
            ap_rem.setdefault(pn, []).extend(round(_num(d["amt"])) for d in docs)

    def _take(lst, amt):
        for i, a in enumerate(lst):
            if abs(a - amt) <= TOLERANCE:
                del lst[i]
                return True
        return False

    where = {}                               # invoice DocNum → PO it currently sits on
    for pn, docs in ar.items():
        for d in docs:
            where[d["num"]] = pn
    for pn, docs in ar.items():              # remove amounts already explained by a matched A/R
        lst = ap_rem.get(pn)
        if lst:
            for d in docs:
                _take(lst, round(_num(d["amt"])))

    n = 0
    for r in _rows(cur, sql):
        num = str(r.get("Num")); amt = round(_num(r.get("Amt")))
        pos_for = ar_ref_to_po.get(num)
        if not pos_for or len(pos_for) != 1:
            continue
        target = next(iter(pos_for))
        cur_po = where.get(num)
        if cur_po == target:
            continue
        doc = {"num": num, "date": r.get("Dt"), "amt": _num(r.get("Amt"))}
        if cur_po is None:                              # unmatched → attach to the naming PO
            ar.setdefault(target, []).append(doc)
            where[num] = target; n += 1
        elif _take(ap_rem.get(target, []), amt):        # conflict → steal only if target is owed it
            ar[cur_po] = [d for d in ar[cur_po] if d["num"] != num]
            ar.setdefault(target, []).append(doc)
            where[num] = target; n += 1
    return n


def _rescue_by_so_amount(cur, sql, pos, ponum_set, po_re, ar, ap):
    """Last-resort match for A/R invoices that carry NO PO anywhere (blank Ref, no PO in Remarks)
    and copy from a Sales Order SHARED across several POs — so only the amount identifies them.
    ``sql`` returns per-(invoice, SO) rows {Num, Dt, SOE, Own, Cmt, Amt}. Steps: (1) learn each
    SO's PO set from its siblings that DO carry a PO (own ref in the anchor set, or a PO in
    Remarks); (2) for each ref-less orphan on such an SO, attach it to the candidate PO whose
    still-unmatched A/P line amount UNIQUELY equals the invoice (within TOLERANCE). Ambiguous (two
    candidate POs) or no A/P match → left unmatched. Mutates ``ar``; returns the count attached.
    Verified: INV 626050290 (blank ref) → PO 526224532 via its ₹26,86,425 A/P line (the other PO
    on SO 1726056657, 526224523, has no A/P of that amount, so it's unambiguous)."""
    # Remaining A/P doc amounts per PO, minus those already covered by a matched A/R (within tol),
    # so an orphan can only claim an A/P line no existing A/R already explains — and two orphans
    # can't both grab the same A/P line.
    po_by_entry = {str(p["Entry"]): str(p["PONum"]) for p in pos}
    ap_rem = {}
    for entry, docs in ap.items():
        pn = po_by_entry.get(str(entry))
        if pn:
            ap_rem.setdefault(pn, []).extend(round(_num(d["amt"]), 2) for d in docs)

    def _take(lst, amt):
        for i, a in enumerate(lst):
            if abs(a - amt) <= TOLERANCE:
                del lst[i]
                return True
        return False

    for pn, docs in ar.items():
        lst = ap_rem.get(pn)
        if lst:
            for d in docs:
                _take(lst, round(_num(d["amt"]), 2))

    matched_nums = {d["num"] for docs in ar.values() for d in docs}
    so_pos, orphans = {}, []
    for r in _rows(cur, sql):
        soe = str(r.get("SOE"))
        own = str(r.get("Own") or "").strip()
        po = own if own in ponum_set else None
        if po is None and po_re is not None:
            m = po_re.search(str(r.get("Cmt") or ""))
            po = m.group(0) if m else None
        if po:
            so_pos.setdefault(soe, set()).add(po)
        elif str(r.get("Num")) not in matched_nums:
            orphans.append({"num": str(r.get("Num")), "date": r.get("Dt"),
                            "amt": _num(r.get("Amt")), "soe": soe})

    n = 0
    for orp in sorted(orphans, key=lambda o: o["num"]):
        cands = so_pos.get(orp["soe"])
        if not cands:
            continue
        amt = round(orp["amt"], 2)
        hits = [pn for pn in cands if any(abs(a - amt) <= TOLERANCE for a in ap_rem.get(pn, []))]
        if len(hits) == 1:
            pn = hits[0]
            ar.setdefault(pn, []).append({"num": orp["num"], "date": orp["date"], "amt": orp["amt"]})
            _take(ap_rem[pn], amt)
            n += 1
    return n


def _rescue_by_unique_amount(cur, sql, pos, ar, ap):
    """Very last resort for A/R invoices that reference the PO NOWHERE (blank Ref, no PO in Remarks)
    and don't even sit on the PO's SO — linkable ONLY by amount. ``sql`` returns EVERY JIVO MART A/R
    invoice in the window {Num, Dt, Amt}. We attach an unmatched invoice to a PO's still-unclaimed
    A/P line ONLY when that exact rupee amount is GLOBALLY UNIQUE: it appears on exactly one unmatched
    invoice AND one unclaimed A/P line across the whole dataset. A colliding amount (two invoices, or
    two POs owing it) is left unmatched, never guessed. Mutates ``ar``; returns count.
    Verified: PO 426224515 → 626040245 (₹13,68,500) + 626040246 (₹2,42,375), both blank-ref, on an
    unrelated SO 1726046633 — each amount uniquely matches one of the PO's A/P lines."""
    po_by_entry = {str(p["Entry"]): str(p["PONum"]) for p in pos}
    # Unclaimed A/P lines per PO (nearest rupee), minus amounts already covered by a matched A/R.
    ap_rem = {}
    for entry, docs in ap.items():
        pn = po_by_entry.get(str(entry))
        if pn:
            ap_rem.setdefault(pn, []).extend(round(_num(d["amt"])) for d in docs)

    def _take(lst, amt):
        for i, a in enumerate(lst):
            if abs(a - amt) <= TOLERANCE:
                del lst[i]
                return True
        return False

    for pn, docs in ar.items():
        lst = ap_rem.get(pn)
        if lst:
            for d in docs:
                _take(lst, round(_num(d["amt"])))

    ap_amount_po = {}                       # amount → [PO, ...] still owed exactly that amount
    for pn, lst in ap_rem.items():
        for a in lst:
            ap_amount_po.setdefault(a, []).append(pn)

    matched_nums = {d["num"] for docs in ar.values() for d in docs}
    orphans = {}                            # amount → [unmatched invoice, ...]
    for r in _rows(cur, sql):
        num = str(r.get("Num"))
        if num in matched_nums:
            continue
        orphans.setdefault(round(_num(r.get("Amt"))), []).append(
            {"num": num, "date": r.get("Dt"), "amt": _num(r.get("Amt"))})

    n = 0
    for amt, invs in orphans.items():
        pos_owed = ap_amount_po.get(amt)
        if len(invs) == 1 and pos_owed and len(pos_owed) == 1:   # unique on BOTH sides
            ar.setdefault(pos_owed[0], []).append(invs[0])
            n += 1
    return n


def _build_nodes(p, ponum, entry, so, ar, grpo, ap, dl, cn_by_inv=None):
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
    _annotate_ar_match(ar_d, grpo_d, ap_d)
    cn_d = []  # every credit note reversing one of this PO's A/R invoices (own column, left of Status)
    for d in ar_d:
        creds = (cn_by_inv or {}).get(str(d["num"]), [])
        for c in creds:  # a credit note is cut against the invoice it reverses
            c.setdefault("ref", str(d["num"]))
        d["credits"] = creds
        cn_d.extend(creds)
    cn_d = sorted(cn_d, key=lambda c: c["num"])
    cn_a = round(sum(c["amt"] for c in cn_d), 2) if cn_d else None
    return {"so": so_a, "so_cnt": so_c, "so_docs": so_d,
            "ar": ar_a, "ar_cnt": ar_c, "ar_docs": ar_d,
            "dl": dl_a, "dl_cnt": dl_c, "dl_docs": dl_d,
            "grpo": grpo_a, "grpo_cnt": grpo_c, "grpo_docs": grpo_d,
            "ap": ap_a, "ap_cnt": ap_c, "ap_docs": ap_d,
            "cn": cn_a, "cn_cnt": len(cn_d), "cn_docs": cn_d}


def _annotate_ar_match(ar_docs, grpo_docs, ap_docs):
    """Flag each A/R invoice ``matched=True`` when an inbound Mart document of the same
    tax-inclusive amount exists — GRPO first (its total equals the A/R total exactly), else
    the A/P (whose total can differ by a rupee or two of tax rounding, so a wider tolerance).
    Each inbound doc is consumed once, so two same-amount A/R invoices need two GRPOs to both
    clear. An A/R with no equal-amount GRPO/A/P is the "where's the missing doc" gap — matched
    stays False so the popup can mark it Pending."""
    pool = [d["amt"] for d in (grpo_docs or [])]
    ap_pool = [d["amt"] for d in (ap_docs or [])]
    for d in ar_docs:
        amt = d["amt"]; hit = None
        for i, g in enumerate(pool):
            if abs(g - amt) <= TOLERANCE:
                hit = i; break
        if hit is not None:
            pool.pop(hit); d["matched"] = True; continue
        hit = None
        for i, a in enumerate(ap_pool):
            if abs(a - amt) <= max(TOLERANCE, amt * 0.005):
                hit = i; break
        if hit is not None:
            ap_pool.pop(hit); d["matched"] = True
        else:
            d["matched"] = False


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


# ── Combined (Oil + Beverages) reconciliation ───────────────────────────────
# One Mart PO can be fulfilled by BOTH seller companies (Wellness-Oil AND Beverages): the GRPO /
# A-P (Mart side) carry the FULL amount, but the SO / A-R split across the two seller schemas.
# Reconciling one company at a time then shows a false spread equal to the OTHER company's A-R
# (e.g. PO 526224543: Oil A-R ₹1,08,699 + Beverages A-R ₹31,384 = the ₹1,40,083 GRPO/A-P). This
# merges both runs by PO — SUMMING the seller-side nodes (SO / A-R / Delivery), keeping the shared
# Mart nodes (PO / GRPO / A-P) once — and re-classifies, so a cross-company PO reconciles. Each
# SO/A-R/Delivery doc is tagged with its company ('Oil' / 'Bev') for a traceable export.
def _tag_docs(docs, co):
    return [dict(d, co=co) for d in (docs or [])]


def get_reconciliation_combined(date_from=None, date_to=None):
    oil = get_reconciliation(date_from, date_to, schema="oil")
    bev = get_reconciliation(date_from, date_to, schema="beverages")

    merged = {}
    for c in oil.get("chains", []):
        d = dict(c)
        for node in ("so", "ar", "delivery"):
            d[node + "_docs"] = _tag_docs(c.get(node + "_docs"), "Oil")
        merged[str(c["po"])] = d
    for c in bev.get("chains", []):
        key = str(c["po"])
        dst = merged.get(key)
        if dst is None:                         # PO only in the beverages run (same anchor, so rare)
            d = dict(c)
            for node in ("so", "ar", "delivery"):
                d[node + "_docs"] = _tag_docs(c.get(node + "_docs"), "Bev")
            merged[key] = d
            continue
        for node in ("so", "ar", "delivery"):   # seller-side: ADD the beverages contribution
            sd = _tag_docs(c.get(node + "_docs"), "Bev")
            if not sd:
                continue
            dst[node + "_docs"] = (dst.get(node + "_docs") or []) + sd
            dst[node + "_cnt"] = (dst.get(node + "_cnt") or 0) + len(sd)
            dst[node] = round((dst.get(node) or 0) + (c.get(node) or 0), 2)
        # PO / GRPO / A-P are Mart-side and identical in both runs — keep the oil copy as-is.

    chains = []
    for c in merged.values():
        node = {"so": c.get("so"), "grpo": c.get("grpo"), "ap": c.get("ap"), "ar": c.get("ar")}
        c["status"], c["detail"] = _classify(_num(c.get("po_total")), node)
        chains.append(c)
    chains.sort(key=lambda c: (str(c.get("po_date") or ""), str(c.get("po"))), reverse=True)
    return {"date_from": oil.get("date_from"), "date_to": oil.get("date_to"),
            "tolerance": TOLERANCE, "summary": _summary(chains), "chains": chains,
            "company": "both"}


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
