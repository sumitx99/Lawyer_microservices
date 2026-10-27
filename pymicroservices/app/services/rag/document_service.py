"""Object upload followed by an atomic document/outbox transaction."""
import logging
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...models import Document, OutboxEvent
from ...schemas import DocumentResponse
from ...utilities.events import RagEvent
from ...utilities.file_validator import UploadError

logger = logging.getLogger(__name__)


class DocumentService:
    def __init__(self, db, storage):
        self.db, self.storage = db, storage

    async def get(self, project_id, doc_id):
        async with self.db.transaction() as session:
            document = await session.scalar(select(Document).where(
                Document.id == doc_id, Document.project_id == project_id))
            if document is None:
                raise UploadError('Document not found.', 404)
            return DocumentResponse.model_validate(document)

    async def list(self, project_id, limit=20, offset=0):
        async with self.db.transaction() as session:
            documents = await session.scalars(select(Document).where(Document.project_id == project_id)
                                              .order_by(Document.created_at.desc(), Document.id.desc())
                                              .limit(limit).offset(offset))
            return [DocumentResponse.model_validate(item) for item in documents]

    async def existing(self, project_id, key, file):
        if key is None:
            return None
        async with self.db.transaction() as session:
            document = await session.scalar(select(Document).where(
                Document.project_id == project_id, Document.upload_idempotency_key == key))
            if document:
                if (document.sha256, document.filename, document.media_type) != (file.sha256, file.filename, file.media_type):
                    raise UploadError('Idempotency-Key was already used for another upload.', 409)
                return DocumentResponse.model_validate(document)

    async def upload(self, project_id, file, idempotency_key=None):
        existing = await self.existing(project_id, idempotency_key, file)
        if existing:
            return existing, False
        doc_id = uuid4()
        extension = '.pdf' if file.media_type == 'application/pdf' else '.docx'
        key = f'projects/{project_id}/documents/{doc_id}/raw{extension}'
        await self.storage.put_bytes(key, file.data, file.media_type)
        try:
            async with self.db.transaction() as session:
                document = Document(id=doc_id, project_id=project_id, filename=file.filename,
                                    media_type=file.media_type, byte_count=len(file.data), sha256=file.sha256,
                                    object_key=key, upload_idempotency_key=idempotency_key)
                event = RagEvent(event_type='doc.uploaded', project_id=project_id, doc_id=doc_id)
                session.add(document)
                session.add(OutboxEvent(id=event.event_id, project_id=project_id,
                                       event_type=event.event_type, aggregate_id=doc_id,
                                       payload=event.model_dump(mode='json')))
                await session.flush()
                response = DocumentResponse.model_validate(document)
            return response, True
        except Exception as error:
            # Commit failure can be ambiguous. Delete only after confirming this object
            # is not linked by a committed document; preserve it if the DB is unreachable.
            try:
                async with self.db.transaction() as session:
                    linked = await session.scalar(select(Document.id).where(Document.id == doc_id))
                if linked is None:
                    await self.storage.delete(key)
            except Exception:
                logger.warning('Upload reconciliation deferred for document %s', doc_id)
            if isinstance(error, IntegrityError) and idempotency_key:
                existing = await self.existing(project_id, idempotency_key, file)
                if existing:
                    return existing, False
            raise
