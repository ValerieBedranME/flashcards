"""A Google-style XLSX export must bind media to the observed card row."""
import base64
import io
import unittest
import zipfile

from google_xlsx_images import parse_export
from workspace import Problem

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/GZkAAAAASUVORK5CYII=")


def sample_export():
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    drawing = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    drawing_main = "http://schemas.openxmlformats.org/drawingml/2006/main"
    files = {
        "xl/workbook.xml": f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets><sheet name="Анатомия" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{package}"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{main}" xmlns:r="{rel}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>ID карточки</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>card_1</t></is></c></row></sheetData><drawing r:id="rId1"/></worksheet>',
        "xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships xmlns="{package}"><Relationship Id="rId1" Target="../drawings/drawing1.xml"/></Relationships>',
        "xl/drawings/drawing1.xml": f'<xdr:wsDr xmlns:xdr="{drawing}" xmlns:a="{drawing_main}" xmlns:r="{rel}"><xdr:oneCellAnchor><xdr:from><xdr:col>11</xdr:col><xdr:row>1</xdr:row></xdr:from><xdr:pic><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic></xdr:oneCellAnchor></xdr:wsDr>',
        "xl/drawings/_rels/drawing1.xml.rels": f'<Relationships xmlns="{package}"><Relationship Id="rId1" Target="../media/image1.png"/></Relationships>',
        "xl/media/image1.png": PNG,
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return stream.getvalue()


class ImageExportTests(unittest.TestCase):
    def test_reads_native_image_and_refuses_moved_card(self):
        blob = sample_export()
        self.assertEqual(parse_export(blob, "Анатомия", [["ID карточки"], ["card_1"]]),
                         {(2, 12): PNG})
        with self.assertRaises(Problem):
            parse_export(blob, "Анатомия", [["ID карточки"], ["different_card"]])


if __name__ == "__main__":
    unittest.main()
