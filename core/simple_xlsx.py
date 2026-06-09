import re
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape


def _col_name(index):
    name = ''
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _safe_sheet_name(name):
    cleaned = re.sub(r'[\[\]:*?/\\]', ' ', str(name or 'Sheet')).strip() or 'Sheet'
    return cleaned[:31]


def _cell(value, row_idx, col_idx, style=0):
    ref = f'{_col_name(col_idx)}{row_idx}'
    text = escape('' if value is None else str(value))
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t>{text}</t></is></c>'


def _sheet_xml(rows):
    xml_rows = []
    merges = []
    max_col = 1

    for r_idx, row in enumerate(rows, 1):
        cells = []
        col_idx = 1
        for cell in row:
            if isinstance(cell, dict):
                value = cell.get('value', '')
                colspan = int(cell.get('colspan') or 1)
                style = int(cell.get('style') or 0)
            else:
                value, colspan, style = cell, 1, 0
            cells.append(_cell(value, r_idx, col_idx, style))
            if colspan > 1:
                start = f'{_col_name(col_idx)}{r_idx}'
                end = f'{_col_name(col_idx + colspan - 1)}{r_idx}'
                merges.append(f'<mergeCell ref="{start}:{end}"/>')
            col_idx += colspan
        max_col = max(max_col, col_idx - 1)
        xml_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')

    merge_xml = ''
    if merges:
        merge_xml = f'<mergeCells count="{len(merges)}">{"".join(merges)}</mergeCells>'
    dimension = f'A1:{_col_name(max_col)}{max(len(rows), 1)}'
    cols = ''.join(f'<col min="{i}" max="{i}" width="18" customWidth="1"/>' for i in range(1, max_col + 1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/><cols>{cols}</cols><sheetData>{"".join(xml_rows)}</sheetData>{merge_xml}'
        '</worksheet>'
    )


def build_workbook(sheets):
    sheets = [(name, rows) for name, rows in sheets if rows]
    if not sheets:
        sheets = [('Sheet1', [['No data']])]

    workbook_sheets = []
    workbook_rels = []
    content_overrides = []

    for idx, (name, _rows) in enumerate(sheets, 1):
        safe_name = escape(_safe_sheet_name(name))
        workbook_sheets.append(f'<sheet name="{safe_name}" sheetId="{idx}" r:id="rId{idx}"/>')
        workbook_rels.append(
            f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
        )
        content_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    workbook_rels.append(
        f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f'{"".join(content_overrides)}</Types>'
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
        f'<sheets>{"".join(workbook_sheets)}</sheets></workbook>'
    )
    workbook_rel_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(workbook_rels)}</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEFF6FF"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF7ED"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFECFDF5"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
        '</styleSheet>'
    )

    out = BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', root_rels)
        zf.writestr('xl/workbook.xml', workbook)
        zf.writestr('xl/_rels/workbook.xml.rels', workbook_rel_xml)
        zf.writestr('xl/styles.xml', styles)
        for idx, (_name, rows) in enumerate(sheets, 1):
            zf.writestr(f'xl/worksheets/sheet{idx}.xml', _sheet_xml(rows))
    return out.getvalue()
