from io import BytesIO
from zipfile import ZipFile

import pytest

from pymicroservices.app.utilities.file_validator import UploadError, validate_file


def test_valid_pdf_and_safe_display_name(pdf_bytes):
    result = validate_file('../../test.pdf', 'application/pdf', pdf_bytes, 100000)
    assert result.filename == 'test.pdf'
    assert result.data == pdf_bytes
    assert len(result.sha256) == 64


def test_valid_docx(docx_bytes):
    assert validate_file('report.docx', None, docx_bytes, 100000).media_type.endswith('document')


@pytest.mark.parametrize('name,mime,data,limit,code', [
    ('file.exe', None, b'abcd', 100, 415), ('file.pdf', 'application/pdf', b'', 100, 400),
    ('file.pdf', 'application/pdf', b'%PDF-fake', 100, 400),
    ('file.pdf', 'text/plain', b'hello', 100, 415),
    ('file.pdf', 'application/pdf', b'a'*101, 100, 413),
    ('file.docx', None, b'not-a-zip', 100, 400),
    ('bad\x00.pdf', None, b'hello', 100, 400),
])
def test_rejected_uploads(name, mime, data, limit, code):
    with pytest.raises(UploadError) as error:
        validate_file(name, mime, data, limit)
    assert error.value.status_code == code


def test_docx_rejects_macro_payload(docx_bytes):
    stream = BytesIO(docx_bytes)
    with ZipFile(stream, 'a') as archive:
        archive.writestr('word/vbaProject.bin', b'macro')
    with pytest.raises(UploadError):
        validate_file('report.docx', None, stream.getvalue(), 100000)


@pytest.mark.parametrize('name,size,sha,status', [
    ('notes.exe', 10, 'a' * 64, 415), ('empty.pdf', 0, 'a' * 64, 400), ('huge.pdf', 51 * 1024 * 1024, 'a' * 64, 413),
    ('bad-hash.pdf', 10, 'xyz', 400), ('..', 10, 'a' * 64, 400), ('', 10, 'a' * 64, 400)])
def test_declared_file_rejections(name, size, sha, status):
    from pymicroservices.app.utilities.file_validator import validate_declared_file
    with pytest.raises(UploadError) as error:
        validate_declared_file(name, size, sha, 50 * 1024 * 1024)
    assert error.value.status_code == status


def test_declared_file_accepts_every_design_doc_type():
    from pymicroservices.app.utilities.file_validator import validate_declared_file
    kinds = {name: validate_declared_file(name, 10, 'A' * 64, 1024).kind for name in
             ['a.PDF', 'b.docx', 'c.doc', 'd.txt', 'e.pptx', 'f.xlsx', 'g.csv', 'h.jpeg', 'i.png']}
    assert [kind.file_type for kind in kinds.values()] == ['pdf', 'docx', 'doc', 'txt', 'pptx', 'xlsx', 'csv', 'jpg', 'png']
    assert kinds['g.csv'].category == 'csv' and kinds['e.pptx'].category == 'presentation'


@pytest.mark.parametrize('file_type,head,ok', [
    ('pdf', b'%PDF-1.7\n', True), ('pdf', b'PK\x03\x04renamed zip', False), ('docx', b'PK\x03\x04', True),
    ('docx', b'%PDF-', False), ('png', b'\x89PNG\r\n\x1a\n', True), ('jpg', b'\x89PNG\r\n\x1a\n', False),
    ('jpg', b'\xff\xd8\xff\xe0', True), ('doc', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', True),
    ('csv', 'नाम,राशि\n'.encode()[:7], True),  # sample cut inside a multi-byte character is still text
    ('csv', b'a,b\x00c', False), ('txt', b'\xff\xfe\xfd not utf-8', False), ('txt', '﻿BOM text'.encode(), True)])
def test_content_problem_by_magic_bytes(file_type, head, ok):
    from pymicroservices.app.utilities.file_validator import content_problem
    assert (content_problem(file_type, head) is None) is ok
