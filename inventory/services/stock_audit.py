"""Inventory Audit Report export — the Stock Available data rendered into the exact pivot
layout of the "View" sheet in the SAP Inventory Audit Report workbook.

Columns: Sub Group | VAREITY | ItemCode | Item Name | SKU | <one column per warehouse
("Godown")> | Grand Total. Values are "Sum of Oil Liter" — per-warehouse litres. Rows are
grouped Sub Group -> Variety, with a Sub Group Total per group and a final Grand Total only
(the pivot's per-item Item Name / ItemCode subtotals and the Variety totals are omitted).
Everything is bold Arial 10, centred, to
match the source sheet; openpyxl is used so numbers/fonts/column widths are exact (the
project's simple_xlsx writer is text-only).
"""

from .oils import get_stock_available


def _r2(v):
    """Round to 2dp; treat sub-rupee dust as zero so empty cells stay blank like the pivot."""
    v = round(float(v or 0), 2)
    return 0.0 if abs(v) < 0.005 else v


def build_view_xlsx(schema="jivo_oil"):
    """Return the .xlsx bytes for the Inventory Audit "View" pivot of the current stock."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    data = get_stock_available(schema=schema)
    items = data.get("items", [])

    # Only warehouses that actually hold stock, alphabetical — matches the source sheet's
    # column set/order (it drops empty godowns like BH-FU).
    whs = sorted(w for w in data.get("warehouses", [])
                 if any(_r2(it["wh_litres"].get(w, 0)) for it in items))

    def wl(it, w):
        return _r2(it["wh_litres"].get(w, 0))

    def sum_wl(rows, w):
        return _r2(sum(wl(it, w) for it in rows))

    def total_of(rows):
        return {w: sum_wl(rows, w) for w in whs}

    def grand(cells):
        return _r2(sum(cells.get(w, 0) for w in whs))

    # Group: Sub Group -> Variety -> [items]; each level ordered by litres descending.
    groups = {}
    for it in items:
        groups.setdefault(it["sub_group"], {}).setdefault(it["variety"] or "—", []).append(it)

    bold = Font(name="ARIAL", size=10, bold=True, color="FF000000")
    centre = Alignment(horizontal="center", vertical="center")

    wb = Workbook()
    ws = wb.active
    ws.title = "View"

    n_lead = 5                      # Sub Group, VAREITY, ItemCode, Item Name, SKU
    gt_col = n_lead + len(whs) + 1  # Grand Total column index

    def put(row, col, value, *, number=False):
        # Numbers that round to zero render as a blank cell, exactly like the pivot.
        if number and not value:
            return
        if value is None or value == "":
            return
        c = ws.cell(row=row, column=col, value=value)
        c.font = bold
        c.alignment = centre

    def emit(row, lead, cells, gtotal):
        """Write one report row: `lead` is the 5 left-hand label cells; `cells` the per-
        warehouse litres; `gtotal` the Grand Total."""
        for i, v in enumerate(lead):
            put(row, 1 + i, v)
        for i, w in enumerate(whs):
            put(row, n_lead + 1 + i, cells.get(w, 0), number=True)
        put(row, gt_col, gtotal, number=True)

    # Header band (the pivot starts at row 3; rows 1-2 are intentionally blank).
    put(3, 1, "Sum of Boxes" if schema == "jivo_beverages" else "Sum of Oil Liter")
    if whs:
        put(3, n_lead + 1, "Godown")
    for i, h in enumerate(["Sub Group", "VAREITY", "ItemCode", "Item Name", "SKU"]):
        put(4, 1 + i, h)
    for i, w in enumerate(whs):
        put(4, n_lead + 1 + i, w)
    put(4, gt_col, "Grand Total")

    row = 5
    for sg in sorted(groups, key=lambda s: -grand(total_of([it for v in groups[s].values() for it in v]))):
        sg_items = [it for v in groups[sg].values() for it in v]
        for var in sorted(groups[sg], key=lambda v: -grand(total_of(groups[sg][v]))):
            for it in sorted(groups[sg][var], key=lambda it: -grand(total_of([it]))):
                cells = {w: wl(it, w) for w in whs}
                emit(row, [sg, var, it["item_code"], it["item_name"], it["sku"] or None], cells, grand(cells)); row += 1
        # Only the Sub Group subtotal is kept (item- and variety-level subtotals omitted).
        st = total_of(sg_items)
        emit(row, [f"{sg} Total", None, None, None, None], st, grand(st)); row += 1
    gtot = total_of(items)
    emit(row, ["Grand Total", None, None, None, None], gtot, grand(gtot))

    # Column widths to match the View sheet.
    widths = {1: 24.8, 2: 30.0, 3: 15.8, 4: 74.5, 5: 8.8}
    for col in range(1, gt_col + 1):
        ws.column_dimensions[get_column_letter(col)].width = widths.get(col, 10.0 if col < gt_col else 11.7)

    from io import BytesIO
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
