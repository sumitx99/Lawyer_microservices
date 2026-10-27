"""S3-compatible object access, targeting SeaweedFS without AWS services."""

import asyncio
import base64
from dataclasses import dataclass
import hashlib

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .secrets_control import Settings, get_settings


@dataclass(frozen=True)
class StoredObject:
    byte_count: int
    content_type: str
    sha256_hex: str | None  # from the checksum SeaweedFS verified on upload, when one was sent


def sha256_b64(sha256_hex: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256_hex)).decode()


class Storage:
    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.bucket = settings.storage_bucket
        self.client = boto3.client(
            's3', endpoint_url=settings.storage_endpoint_url,
            aws_access_key_id=settings.storage_access_key.get_secret_value(),
            aws_secret_access_key=settings.storage_secret_key.get_secret_value(),
            region_name=settings.storage_region,
            config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                          connect_timeout=5, read_timeout=30,
                          retries={'max_attempts': 3, 'mode': 'standard'}),
        )

    @staticmethod
    def _key(key):
        if not key or key.startswith('/') or '..' in key.split('/'):
            raise ValueError('An object key must be a nonempty relative path without parent traversal.')
        return key

    async def put_bytes(self, key: str, data: bytes, content_type='application/octet-stream'):
        await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=self._key(key),
                                Body=data, ContentType=content_type)

    async def get_bytes(self, key: str) -> bytes:
        key = self._key(key)

        def read():
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            with response['Body'] as body:
                return body.read()

        return await asyncio.to_thread(read)

    async def delete(self, key: str):
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=self._key(key))

    def presign_put(self, key, content_type, byte_count, sha256_hex, expires):
        """A browser PUT URL that SeaweedFS rejects unless size, Content-Type and SHA-256 all match."""
        checksum = sha256_b64(sha256_hex)
        url = self.client.generate_presigned_url('put_object', ExpiresIn=expires, Params={
            'Bucket': self.bucket, 'Key': self._key(key), 'ContentType': content_type,
            'ContentLength': byte_count, 'ChecksumSHA256': checksum})
        return url, {'Content-Type': content_type, 'x-amz-checksum-sha256': checksum}

    async def inspect(self, key) -> StoredObject | None:
        """HEAD with checksum; None when the object does not exist (yet)."""
        key = self._key(key)

        def head():
            try:
                return self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode='ENABLED')
            except ClientError as error:
                if error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                    return None
                raise

        response = await asyncio.to_thread(head)
        if response is None:
            return None
        checksum = response.get('ChecksumSHA256')
        return StoredObject(response['ContentLength'], response.get('ContentType', ''),
                            base64.b64decode(checksum).hex() if checksum else None)

    async def read_head(self, key, size=8192) -> bytes:
        key = self._key(key)

        def read():
            response = self.client.get_object(Bucket=self.bucket, Key=key, Range=f'bytes=0-{size - 1}')
            with response['Body'] as body:
                return body.read()

        return await asyncio.to_thread(read)

    async def stream_sha256(self, key) -> str:
        """Fallback when no stored checksum exists: hash the object in 1 MB pieces."""
        key = self._key(key)

        def digest():
            sha = hashlib.sha256()
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            with response['Body'] as body:
                for piece in iter(lambda: body.read(1024 * 1024), b''):
                    sha.update(piece)
            return sha.hexdigest()

        return await asyncio.to_thread(digest)

    async def ping(self):
        await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        return True

    async def close(self):
        await asyncio.to_thread(self.client.close)
