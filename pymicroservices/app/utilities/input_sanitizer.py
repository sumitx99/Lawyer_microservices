"""Validate display names; storage paths are generated from UUIDs, never filenames."""
import unicodedata


def clean_filename(value: str | None) -> str:
    if not value:
        raise ValueError('A filename is required.')
    value = unicodedata.normalize('NFC', value.replace('\\', '/').rsplit('/', 1)[-1]).strip()
    if not value or len(value) > 255 or any(unicodedata.category(ch).startswith('C') for ch in value):
        raise ValueError('Filename must contain 1–255 printable characters.')
    return value


def clean_user_id(value: str | None) -> str:
    """X-User-Id until authentication exists: 1–128 printable characters, no surrounding spaces."""
    value = (value or '').strip()
    if not value or len(value) > 128 or any(unicodedata.category(ch).startswith('C') for ch in value):
        raise ValueError('X-User-Id must contain 1–128 printable characters.')
    return value
