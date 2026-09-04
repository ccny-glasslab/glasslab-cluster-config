"""Render an accepted report into a PDF + DOCX delivery bundle.

Both formats are derived deterministically from the report body using only the
standard library, so the delivered bundle always matches the accepted report
without pulling reportlab or python-docx into the service image.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import re
import zipfile


class ArtifactDeliveryError(ValueError):
    pass


@dataclass(frozen=True)
class ReportBundle:
    run_id: str
    timestamp: str
    pdf: bytes
    docx: bytes
    pdf_filename: str
    docx_filename: str


def _bundle_stem(run_id: str, timestamp: str) -> str:
    # Deterministic, filesystem-safe base name: run id prefix plus a compact
    # timestamp. Anything outside [A-Za-z0-9._-] is collapsed to '-' so the
    # name survives archives, Discord, and every filesystem.
    safe_timestamp = re.sub(r'[^A-Za-z0-9._-]+', '-', timestamp).strip('-')
    return f'glasslab-{run_id[:12]}-{safe_timestamp}'


def _pdf_escape(text: str) -> bytes:
    # PDF literal strings: escape backslash and parens. Pure-ASCII text stays
    # a plain literal string; non-ASCII is encoded as UTF-16BE with a BOM so
    # it survives the PDF text-string encoding rules.
    escaped = text.replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')
    try:
        return escaped.encode('ascii')
    except UnicodeEncodeError:
        return b'\xfe\xff' + escaped.encode('utf-16-be')


def _build_pdf(report_body: str) -> bytes:
    # Minimal single-page PDF (Helvetica, wrapped lines) built with the
    # standard library only; reportlab is deliberately not required.
    lines: list[str] = []
    for paragraph in report_body.splitlines() or ['']:
        if not paragraph.strip():
            lines.append('')
            continue
        words = paragraph.split()
        current = ''
        for word in words:
            candidate = f'{current} {word}'.strip()
            if len(candidate) <= 95:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)

    objects: list[bytes] = []
    objects.append(b'<< /Type /Catalog /Pages 2 0 R >>')
    objects.append(b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>')
    objects.append(
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
        b'/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>'
    )
    stream_lines = ['BT /F1 11 Tf 50 750 Td 14 TL']
    for line in lines[:48]:
        stream_lines.append(f'({_pdf_escape(line).decode("latin-1")}) Tj T*')
    stream_lines.append('ET')
    stream = '\n'.join(stream_lines).encode('latin-1')
    objects.append(
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n'
        + stream
        + b'\nendstream'
    )
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')

    output = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f'{index} 0 obj\n'.encode())
        output.extend(body)
        output.extend(b'\nendobj\n')
    xref_offset = len(output)
    output.extend(f'xref\n0 {len(objects) + 1}\n'.encode())
    output.extend(b'0000000000 65535 f \n')
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(
        (
            f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n'
            f'startxref\n{xref_offset}\n%%EOF\n'
        ).encode()
    )
    return bytes(output)


def _build_docx(report_body: str) -> bytes:
    # Minimal OOXML document (word/document.xml) packaged as a zip; the
    # python-docx dependency is deliberately avoided.
    paragraphs = []
    for line in report_body.splitlines() or ['']:
        escaped = (
            line.replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
        )
        paragraphs.append(f'<w:p><w:r><w:t>{escaped}</w:t></w:r></w:p>')
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main">'
        '<w:body>'
        + ''.join(paragraphs)
        + '<w:sectPr/></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '</Relationships>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', content_types)
        archive.writestr('_rels/.rels', rels)
        archive.writestr('word/document.xml', document)
    return output.getvalue()


def build_report_bundle(
    *,
    run_id: str,
    report_body: str,
    timestamp: str,
) -> ReportBundle:
    """Render a completed report into a PDF + DOCX delivery bundle.

    The report body is the authoritative markdown text produced by Honeydew;
    both formats are derived deterministically from it so the delivered bundle
    always matches the accepted report. An empty body is a delivery error, not
    a silently empty bundle.
    """
    if not report_body.strip():
        raise ArtifactDeliveryError(
            'cannot build a report bundle from an empty report body'
        )
    stem = _bundle_stem(run_id, timestamp)
    return ReportBundle(
        run_id=run_id,
        timestamp=timestamp,
        pdf=_build_pdf(report_body),
        docx=_build_docx(report_body),
        pdf_filename=f'{stem}.pdf',
        docx_filename=f'{stem}.docx',
    )