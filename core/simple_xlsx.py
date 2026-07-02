import re
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr


def _col_name(index):
    name = ''
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _safe_sheet_name(name):
    cleaned = re.sub(r'[\[\]:*?/\\]', ' ', str(name or 'Sheet')).strip() or 'Sheet'
    return cleaned[:31]


# ── Style registry ───────────────────────────────────────────────────────────────────────
# Legacy fixed cellXfs indices 0..4 are kept byte-identical so existing callers that pass an
# integer `style` (and the scalar-cell default of 0) are unchanged. A cell may instead carry
# styling fields — fill (RGB hex), color (font RGB hex), bold, align, indent, numfmt — and the
# registry mints a new font / fill / numFmt / cellXf for it on demand (indices >= 5).
_BASE_FONTS = [
    '<font><sz val="11"/><name val="Calibri"/></font>',          # 0 normal
    '<font><b/><sz val="11"/><name val="Calibri"/></font>',      # 1 bold
]
_BASE_FILLS = [
    '<fill><patternFill patternType="none"/></fill>',            # 0
    '<fill><patternFill patternType="gray125"/></fill>',         # 1
    '<fill><patternFill patternType="solid"><fgColor rgb="FFEFF6FF"/></patternFill></fill>',  # 2
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF7ED"/></patternFill></fill>',  # 3
    '<fill><patternFill patternType="solid"><fgColor rgb="FFECFDF5"/></patternFill></fill>',  # 4
]
_BASE_XFS = [
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>',                                # 0
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>',    # 1
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>',    # 2
    '<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>',    # 3
    '<xf numFmtId="0" fontId="1" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1"/>',    # 4
]
_STYLE_KEYS = ('fill', 'color', 'bold', 'align', 'indent', 'numfmt')


class _Styles:
    def __init__(self):
        self.fonts = list(_BASE_FONTS)
        self.fills = list(_BASE_FILLS)
        self.xfs = list(_BASE_XFS)
        self.numfmts = []          # [(numFmtId, formatCode)], ids start at 164
        self._fonts, self._fills, self._nums, self._xfs = {}, {}, {}, {}

    @staticmethod
    def _rgb(hexstr):
        h = str(hexstr or '').lstrip('#').upper()
        return ('FF' + h) if len(h) == 6 else ('FF' + h[-6:].rjust(6, '0'))

    def _font(self, color, bold):
        key = (color or '', bool(bold))
        if key in self._fonts:
            return self._fonts[key]
        parts = []
        if bold:
            parts.append('<b/>')
        parts.append('<sz val="11"/>')
        if color:
            parts.append(f'<color rgb="{self._rgb(color)}"/>')
        parts.append('<name val="Calibri"/>')
        xml = f'<font>{"".join(parts)}</font>'
        idx = self.fonts.index(xml) if xml in self.fonts else len(self.fonts)
        if idx == len(self.fonts):
            self.fonts.append(xml)
        self._fonts[key] = idx
        return idx

    def _fill(self, rgb):
        if not rgb:
            return 0
        key = self._rgb(rgb)
        if key in self._fills:
            return self._fills[key]
        xml = f'<fill><patternFill patternType="solid"><fgColor rgb="{key}"/></patternFill></fill>'
        idx = self.fills.index(xml) if xml in self.fills else len(self.fills)
        if idx == len(self.fills):
            self.fills.append(xml)
        self._fills[key] = idx
        return idx

    def _numfmt(self, code):
        if not code:
            return 0               # 0 = General
        if code in self._nums:
            return self._nums[code]
        nid = 164 + len(self.numfmts)
        self.numfmts.append((nid, code))
        self._nums[code] = nid
        return nid

    def resolve(self, spec):
        """A cell dict with any of _STYLE_KEYS → a cellXfs index (cached)."""
        key = tuple(spec.get(k) for k in _STYLE_KEYS)
        if key in self._xfs:
            return self._xfs[key]
        font_id = self._font(spec.get('color'), spec.get('bold'))
        fill_id = self._fill(spec.get('fill'))
        num_id = self._numfmt(spec.get('numfmt'))
        flags = ' applyFont="1"'
        if fill_id:
            flags += ' applyFill="1"'
        if num_id:
            flags += ' applyNumberFormat="1"'
        align = spec.get('align')
        indent = spec.get('indent')
        align_xml = ''
        if align or indent:
            attrs = ''
            if align:
                attrs += f' horizontal="{align}"'
            if indent:
                attrs += f' indent="{int(indent)}"'
            align_xml = f'<alignment{attrs}/>'
            flags += ' applyAlignment="1"'
        if align_xml:
            xf = f'<xf numFmtId="{num_id}" fontId="{font_id}" fillId="{fill_id}" borderId="0" xfId="0"{flags}>{align_xml}</xf>'
        else:
            xf = f'<xf numFmtId="{num_id}" fontId="{font_id}" fillId="{fill_id}" borderId="0" xfId="0"{flags}/>'
        idx = len(self.xfs)
        self.xfs.append(xf)
        self._xfs[key] = idx
        return idx

    def styles_xml(self):
        numfmts_xml = ''
        if self.numfmts:
            items = ''.join(f'<numFmt numFmtId="{nid}" formatCode={quoteattr(code)}/>' for nid, code in self.numfmts)
            numfmts_xml = f'<numFmts count="{len(self.numfmts)}">{items}</numFmts>'
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'{numfmts_xml}'
            f'<fonts count="{len(self.fonts)}">{"".join(self.fonts)}</fonts>'
            f'<fills count="{len(self.fills)}">{"".join(self.fills)}</fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(self.xfs)}">{"".join(self.xfs)}</cellXfs>'
            '</styleSheet>'
        )


def _cell(value, row_idx, col_idx, style=0):
    ref = f'{_col_name(col_idx)}{row_idx}'
    # Real numeric cells (so Excel can sum/sort) for actual numbers; everything else is text.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value or value in (float('inf'), float('-inf')):   # NaN / inf → blank
            return f'<c r="{ref}" s="{style}"/>'
        num = repr(value) if isinstance(value, float) else str(value)
        return f'<c r="{ref}" t="n" s="{style}"><v>{num}</v></c>'
    text = escape('' if value is None else str(value))
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t>{text}</t></is></c>'


def _sheet_xml(rows, styles):
    xml_rows = []
    merges = []
    max_col = 1
    widths = {}        # 1-based column index → max display length (for auto-fit)

    for r_idx, row in enumerate(rows, 1):
        cells = []
        col_idx = 1
        for cell in row:
            if isinstance(cell, dict):
                value = cell.get('value', '')
                colspan = int(cell.get('colspan') or 1)
                if any(k in cell for k in _STYLE_KEYS):
                    style = styles.resolve(cell)
                else:
                    style = int(cell.get('style') or 0)
            else:
                value, colspan, style = cell, 1, 0
            cells.append(_cell(value, r_idx, col_idx, style))
            widths[col_idx] = max(widths.get(col_idx, 0), len('' if value is None else str(value)))
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
    cols = ''.join(
        f'<col min="{i}" max="{i}" width="{min(max(widths.get(i, 0) + 2, 10), 60)}" customWidth="1"/>'
        for i in range(1, max_col + 1)
    )
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

    styles = _Styles()
    # Render sheets first so the style registry is fully populated before styles.xml is built.
    rendered = [(name, _sheet_xml(rows, styles)) for name, rows in sheets]

    workbook_sheets = []
    workbook_rels = []
    content_overrides = []

    for idx, (name, _xml) in enumerate(rendered, 1):
        safe_name = escape(_safe_sheet_name(name))
        workbook_sheets.append(f'<sheet name="{safe_name}" sheetId="{idx}" r:id="rId{idx}"/>')
        workbook_rels.append(
            f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
        )
        content_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    workbook_rels.append(
        f'<Relationship Id="rId{len(rendered) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
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

    out = BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', root_rels)
        zf.writestr('xl/workbook.xml', workbook)
        zf.writestr('xl/_rels/workbook.xml.rels', workbook_rel_xml)
        zf.writestr('xl/styles.xml', styles.styles_xml())
        for idx, (_name, xml) in enumerate(rendered, 1):
            zf.writestr(f'xl/worksheets/sheet{idx}.xml', xml)
    return out.getvalue()
