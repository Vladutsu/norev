"""Deterministic, editable Word documents using standard-library OOXML only."""
import io
import re
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def render_docx(content: str) -> bytes:
    paragraphs = []
    for line in content.splitlines():
        heading = len(line) - len(line.lstrip('#')) if line.startswith('#') else 0
        text = line[heading:].strip() if heading else line
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        style = 'Title' if heading == 1 else 'Heading1' if heading else 'Normal'
        paragraphs.append(f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
                          f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')
    ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    files = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        'word/_rels/document.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        'word/document.xml': f'<w:document xmlns:w="{ns}"><w:body>{"".join(paragraphs)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1417"/></w:sectPr></w:body></w:document>',
        'word/styles.xml': f'<w:styles xmlns:w="{ns}">'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/><w:color w:val="000000"/><w:lang w:val="ro-RO"/></w:rPr></w:rPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/><w:widowControl/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style></w:styles>',
    }
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for name, xml in files.items():
            info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + xml)
    return output.getvalue()
