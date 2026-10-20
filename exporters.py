"""Exportacao de resultados: CSV, Excel (.xlsx), JSON e SQL (INSERTs)."""
import csv
import json
import re
import zipfile
from xml.sax.saxutils import escape as xml_escape


def _to_number(value):
    try:
        if re.fullmatch(r"-?\d{1,15}", value):
            return int(value)
        if re.fullmatch(r"-?\d+\.\d+", value) and len(value) <= 17:
            return float(value)
    except TypeError:
        pass
    return None


def export_csv(path, columns, rows, delimiter=";"):
    # utf-8-sig: o Excel reconhece a acentuacao ao abrir com duplo clique
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        w.writerow([c.name for c in columns])
        for row in rows:
            w.writerow(["" if v is None else v for v in row])


def export_json(path, columns, rows):
    names = [c.name for c in columns]
    data = []
    for row in rows:
        item = {}
        for col, name, v in zip(columns, names, row):
            if v is not None and col.is_numeric:
                n = _to_number(v)
                item[name] = v if n is None else n
            else:
                item[name] = v
        data.append(item)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sql_literal(value):
    if value is None:
        return "NULL"
    s = (value.replace("\\", "\\\\").replace("\0", "\\0").replace("\n", "\\n")
         .replace("\r", "\\r").replace("'", "\\'").replace("\x1a", "\\Z"))
    return f"'{s}'"


def export_sql(path, columns, rows, table_name="tabela"):
    from mysql4proto import escape_identifier
    cols = ", ".join(escape_identifier(c.name) for c in columns)
    table = escape_identifier(table_name)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            values = ", ".join(sql_literal(v) for v in row)
            f.write(f"INSERT INTO {table} ({cols}) VALUES ({values});\n")


_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _col_letter(index):
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def export_xlsx(path, columns, rows, sheet_name="Resultado"):
    """Gera um .xlsx minimo (sem dependencias) com strings inline."""

    def cell(ref, value, numeric):
        if value is None:
            return ""
        if numeric:
            n = _to_number(value)
            if n is not None:
                return f'<c r="{ref}"><v>{n}</v></c>'
        text = xml_escape(_ILLEGAL_XML.sub("", value))
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
             'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetData>']
    letters = [_col_letter(i) for i in range(len(columns))]
    header = "".join(cell(f"{letters[i]}1", c.name, False) for i, c in enumerate(columns))
    parts.append(f'<row r="1">{header}</row>')
    for r, row in enumerate(rows, start=2):
        cells = "".join(cell(f"{letters[i]}{r}", v, columns[i].is_numeric)
                        for i, v in enumerate(row))
        parts.append(f'<row r="{r}">{cells}</row>')
    parts.append("</sheetData></worksheet>")

    sheet_name = xml_escape(re.sub(r"[\[\]:*?/\\]", "_", sheet_name)[:31] or "Resultado")
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>',
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
        z.writestr("xl/worksheets/sheet1.xml", "".join(parts))


EXPORT_FORMATS = [
    ("Excel (*.xlsx)", ".xlsx"),
    ("CSV - separado por ; (*.csv)", ".csv"),
    ("JSON (*.json)", ".json"),
    ("SQL - INSERTs (*.sql)", ".sql"),
]


def export(path, columns, rows, table_name="tabela"):
    lower = path.lower()
    if lower.endswith(".xlsx"):
        export_xlsx(path, columns, rows, table_name)
    elif lower.endswith(".json"):
        export_json(path, columns, rows)
    elif lower.endswith(".sql"):
        export_sql(path, columns, rows, table_name)
    else:
        export_csv(path, columns, rows)
