"""Read Google Sheets in-cell pictures from a Drive XLSX export.

Sheets v4 exposes text values but omits CellImage bytes. Drive's XLSX export
contains the pictures and their cell anchors; we use the export only for those
two image columns and compare its text snapshot to the Sheets values response.
"""
from collections import defaultdict
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from workspace import Problem

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_MAIN = "http://schemas.openxmlformats.org/drawingml/2006/main"
MAX_EXPORT = 10 * 1024 * 1024
MAX_UNCOMPRESSED = 45 * 1024 * 1024
MAX_MEMBERS = 3000


def _part(source, target):
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), target.lstrip("/")))
    if target.startswith("/"):
        path = target.lstrip("/")
    if path.startswith("../") or path == "..":
        raise Problem("Повреждённый экспорт Google-таблицы", 409)
    return path


def _rels(archive, source):
    folder, name = posixpath.split(source)
    relname = posixpath.join(folder, "_rels", name + ".rels")
    if relname not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(relname))
    return {item.attrib["Id"]: _part(source, item.attrib["Target"])
            for item in root.findall(f"{{{PACKAGE_REL}}}Relationship")
            if item.attrib.get("TargetMode") != "External"}


def _shared(archive):
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter(f"{{{MAIN}}}t"))
            for item in root.findall(f"{{{MAIN}}}si")]


def _cell_text(cell, strings):
    formula = cell.find(f"{{{MAIN}}}f")
    if formula is not None:
        return "=" + (formula.text or "")
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{MAIN}}}t"))
    value = cell.find(f"{{{MAIN}}}v")
    if value is None:
        return ""
    if cell.attrib.get("t") == "s":
        return strings[int(value.text)]
    return value.text or ""


def _rows(root, strings, text_columns=11):
    result = []
    for row in root.iter(f"{{{MAIN}}}row"):
        index = int(row.attrib["r"])
        if index > 10000:
            raise Problem("За одно обновление поддерживается до 9999 строк", 422)
        while len(result) < index:
            result.append([])
        values = result[index - 1]
        for cell in row.findall(f"{{{MAIN}}}c"):
            match = re.fullmatch(r"([A-Z]+)([0-9]+)", cell.attrib.get("r", ""))
            if not match:
                continue
            col = 0
            for letter in match[1]:
                col = col * 26 + ord(letter) - 64
            if col > text_columns:
                continue
            values.extend([""] * max(0, col - len(values)))
            values[col - 1] = _cell_text(cell, strings)
    return result


def _normalized(rows, text_columns=11):
    result = []
    for row in rows:
        values = [str(value) if value is not None else "" for value in row[:text_columns]]
        while values and not values[-1]:
            values.pop()
        result.append(values)
    while result and not result[-1]:
        result.pop()
    return result


def parse_export(blob, title, expected_rows, columns=(12, 13), text_columns=11):
    """Return {(row, column): image bytes} for L/M, rejecting stale row maps."""
    if not isinstance(blob, bytes) or len(blob) > MAX_EXPORT:
        raise Problem("Таблица с картинками слишком велика для экспорта Google (10 МБ)", 422)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            infos = archive.infolist()
            if (len(infos) > MAX_MEMBERS or
                    sum(info.file_size for info in infos) > MAX_UNCOMPRESSED):
                raise Problem("Экспорт Google-таблицы слишком велик", 422)
            root = ET.fromstring(archive.read("xl/workbook.xml"))
            sheet = next((item for item in root.iter(f"{{{MAIN}}}sheet")
                          if item.attrib.get("name") == title), None)
            if sheet is None:
                raise Problem(f"Вкладка «{title}» не найдена в экспорте Google", 409)
            workbook_rels = _rels(archive, "xl/workbook.xml")
            part = workbook_rels[sheet.attrib[f"{{{OFFICE_REL}}}id"]]
            sheet_root = ET.fromstring(archive.read(part))
            if _normalized(_rows(sheet_root, _shared(archive), text_columns), text_columns) != _normalized(expected_rows, text_columns):
                raise Problem("Таблица изменилась во время чтения картинок. Повторите обновление.", 409)
            sheet_rels = _rels(archive, part)
            output = {}
            for drawing in sheet_root.iter(f"{{{MAIN}}}drawing"):
                drawing_part = sheet_rels[drawing.attrib[f"{{{OFFICE_REL}}}id"]]
                drawing_rels = _rels(archive, drawing_part)
                drawing_root = ET.fromstring(archive.read(drawing_part))
                for anchor in drawing_root.findall(f"{{{DRAWING}}}oneCellAnchor"):
                    origin = anchor.find(f"{{{DRAWING}}}from")
                    if origin is None:
                        continue
                    row = int(origin.findtext(f"{{{DRAWING}}}row")) + 1
                    column = int(origin.findtext(f"{{{DRAWING}}}col")) + 1
                    if column not in columns or row < 2 or row > 10000:
                        continue
                    picture = anchor.find(f"{{{DRAWING}}}pic")
                    if picture is None:
                        continue
                    blip = picture.find(f".//{{{DRAWING_MAIN}}}blip")
                    if blip is None:
                        continue
                    media_part = drawing_rels[blip.attrib[f"{{{OFFICE_REL}}}embed"]]
                    if (row, column) in output:
                        raise Problem(f"В строке {row} несколько картинок на одной стороне", 422)
                    info = archive.getinfo(media_part)
                    if info.file_size > 2 * 1024 * 1024:
                        raise Problem(f"Картинка в строке {row} больше 2 МБ", 413)
                    output[row, column] = archive.read(media_part)
            return output
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, IndexError) as error:
        raise Problem("Не удалось прочитать картинки из Google-таблицы. Повторите обновление.", 409) from error
