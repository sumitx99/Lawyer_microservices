"""Upload validation.

Per-document API: bounded PDF/DOCX validation before storage or database writes.
Folder API: declared-file checks at /create, then magic-byte checks on the stored object at
/complete-upload (size and SHA-256 are enforced by the presigned PUT and re-checked there).
"""
import codecs
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
import re
import warnings
from xml.etree import ElementTree
from zipfile import ZipFile

from pypdf import PdfReader

from .input_sanitizer import clean_filename


class UploadError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ValidatedFile:
    filename: str
    media_type: str
    data: bytes
    sha256: str


def validate_file(filename, declared_type, data, maximum):
    try:
        filename = clean_filename(filename)
    except ValueError as error:
        raise UploadError(str(error)) from None
    if not data:
        raise UploadError('The uploaded file is empty.')
    if len(data) > maximum:
        raise UploadError('The uploaded file exceeds the configured size limit.', 413)
    suffix = PurePosixPath(filename).suffix.lower()
    media = {'.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
    if suffix not in media:
        raise UploadError('Only PDF and DOCX files are supported.', 415)
    declared_type = (declared_type or '').split(';', 1)[0].strip().lower()
    if declared_type not in {'', 'application/octet-stream', media[suffix]}:
        raise UploadError('Content type does not match the file extension.', 415)
    try:
        if suffix == '.pdf':
            if not data.startswith(b'%PDF-'):
                raise ValueError()
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                reader = PdfReader(BytesIO(data), strict=True)
                if reader.is_encrypted or not len(reader.pages):
                    raise ValueError()
        else:
            validate_docx(data)
    except (UploadError,):
        raise
    except Exception:
        raise UploadError('File content is invalid, encrypted or unsupported.') from None
    return ValidatedFile(filename, media[suffix], data, sha256(data).hexdigest())


def validate_docx(data):
    with ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(entries) > 2048 or len(names) != len(set(names)):
            raise ValueError()
        if sum(item.file_size for item in entries) > 100 * 1024 * 1024:
            raise ValueError()
        for item in entries:
            path = PurePosixPath(item.filename.replace('\\', '/'))
            if path.is_absolute() or '..' in path.parts or item.flag_bits & 1:
                raise ValueError()
        if any(name.lower().endswith('vbaproject.bin') for name in names):
            raise ValueError()
        for name, expected in [('[Content_Types].xml', '{http://schemas.openxmlformats.org/package/2006/content-types}Types'),
                               ('word/document.xml', '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document')]:
            if archive.getinfo(name).file_size > 20 * 1024 * 1024:
                raise ValueError()
            xml = archive.read(name)
            if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                raise ValueError()
            root = ElementTree.fromstring(xml)
            if root.tag != expected:
                raise ValueError()
            if name == '[Content_Types].xml' and not any(
                child.attrib.get('PartName') == '/word/document.xml' and child.attrib.get('ContentType') ==
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml' for child in root):
                raise ValueError()


@dataclass(frozen=True)
class FileKind:
    file_type: str
    category: str
    content_type: str
    magic: tuple[bytes, ...]  # empty for text formats, which are checked by decoding instead


ZIP = (b'PK\x03\x04',)
FILE_KINDS = {
    '.pdf': FileKind('pdf', 'document', 'application/pdf', (b'%PDF-',)),
    '.docx': FileKind('docx', 'document', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', ZIP),
    '.doc': FileKind('doc', 'document', 'application/msword', (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',)),
    '.txt': FileKind('txt', 'text', 'text/plain', ()),
    '.pptx': FileKind('pptx', 'presentation', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', ZIP),
    '.xlsx': FileKind('xlsx', 'spreadsheet', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', ZIP),
    '.csv': FileKind('csv', 'csv', 'text/csv', ()),
    '.jpg': FileKind('jpg', 'image', 'image/jpeg', (b'\xff\xd8\xff',)),
    '.jpeg': FileKind('jpg', 'image', 'image/jpeg', (b'\xff\xd8\xff',)),
    '.png': FileKind('png', 'image', 'image/png', (b'\x89PNG\r\n\x1a\n',)),
}
KINDS_BY_TYPE = {kind.file_type: kind for kind in FILE_KINDS.values()}
SHA256 = re.compile(r'^[0-9a-f]{64}$')


@dataclass(frozen=True)
class DeclaredFile:
    file_name: str
    byte_count: int
    sha256: str
    kind: FileKind


def validate_declared_file(file_name, byte_count, sha256_hex, maximum):
    """Checks a file the browser is about to upload: name, allowed extension, size and hash format."""
    try:
        file_name = clean_filename(file_name)
    except ValueError as error:
        raise UploadError(str(error)) from None
    if file_name in {'.', '..'}:
        raise UploadError('Filename must contain 1–255 printable characters.')
    kind = FILE_KINDS.get(PurePosixPath(file_name).suffix.lower())
    if kind is None:
        raise UploadError('Unsupported file type. Allowed: ' + ', '.join(sorted(FILE_KINDS)) + '.', 415)
    if byte_count <= 0:
        raise UploadError(f'{file_name}: the file is empty.')
    if byte_count > maximum:
        raise UploadError(f'{file_name}: the file exceeds the {maximum // (1024 * 1024)} MB limit.', 413)
    sha256_hex = (sha256_hex or '').lower()
    if not SHA256.match(sha256_hex):
        raise UploadError(f'{file_name}: sha256 must be 64 hexadecimal characters.')
    return DeclaredFile(file_name, byte_count, sha256_hex, kind)


def content_problem(file_type, head: bytes):
    """Returns why the first bytes of a stored object do not fit its declared type, or None."""
    kind = KINDS_BY_TYPE[file_type]
    if kind.magic:
        return None if head.startswith(kind.magic) else f'Content does not look like a {file_type.upper()} file.'
    if b'\x00' in head:
        return f'{file_type.upper()} file contains binary data.'
    try:
        # The sample may end inside a multi-byte character, so decode it incrementally.
        codecs.getincrementaldecoder('utf-8-sig')().decode(head, final=False)
    except UnicodeDecodeError:
        return f'{file_type.upper()} file is not UTF-8 text.'
    return None
