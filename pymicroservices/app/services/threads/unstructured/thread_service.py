"""Folder upload lifecycle (design doc): create -> presigned PUT -> complete-upload -> first stage WAITING.

The API never receives file bytes. /create records the folder and one row per file and returns
presigned PUT URLs that SeaweedFS only accepts with the declared size, Content-Type and SHA-256.
/complete-upload re-checks the stored object (HEAD for size and checksum, a ranged read for magic
bytes), sets the file's first stage to WAITING and queues one {"unstructured_thread_id"} message per
topic through the outbox, in the same transaction.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import logging
import time
from uuid import uuid4

from sqlalchemy import select, update

from ....models import (DataFabric, FabricUnstructuredThread, OutboxEvent, UnstructuredData,
                        UnstructuredThread)
from ....schemas import (CompleteUploadResponse, FileStatus, FileUploadResult, SkippedFile, StageState,
                         ThreadCreateResponse, ThreadStatusResponse, UploadTarget)
from ....utilities.file_validator import UploadError, content_problem, validate_declared_file

logger = logging.getLogger(__name__)

DOCUMENT_STAGES = ('standardization', 'markdown', 'image', 'metaclassifier')
# Resetting a stage also resets the stages that consume its output.
DOWNSTREAM = {'standardization': ('markdown', 'image', 'metaclassifier'), 'markdown': ('metaclassifier',),
              'image': ('metaclassifier',), 'metaclassifier': (), 'parquet': ()}
DONE = {'COMPLETED', 'INAPPLICABLE'}
TERMINAL_THREAD = {'COMPLETED', 'PARTIAL', 'FAILED'}


def stages_for(file_type):
    return ('parquet',) if file_type == 'csv' else DOCUMENT_STAGES


def file_state(row):
    """uploading | processing | done | failed, from the upload status and the file's stages."""
    if row.upload_status == 'RUNNING':
        return 'uploading'
    if row.upload_status == 'FAILED':
        return 'failed'
    statuses = [getattr(row, f'{stage}_status') for stage in stages_for(row.file_type)]
    if 'FAILED' in statuses:
        return 'failed'
    return 'done' if all(status in DONE for status in statuses) else 'processing'


def derive_thread_status(rows):
    states = {file_state(row) for row in rows}
    if not states or 'uploading' in states:
        return 'UPLOADING'
    if 'processing' in states:
        return 'PROCESSING'
    if states == {'done'}:
        return 'COMPLETED'
    return 'FAILED' if states == {'failed'} else 'PARTIAL'


def queue_message(session, thread_id, stage):
    """Outbox row for one folder message; project_id holds the folder id (no project needed)."""
    session.add(OutboxEvent(id=uuid4(), project_id=thread_id, event_type=f'thread.{stage}', aggregate_id=thread_id,
                            payload={'unstructured_thread_id': str(thread_id)}))


async def refresh_thread_status(session, thread_id):
    rows = list(await session.scalars(select(UnstructuredData).where(UnstructuredData.thread_id == thread_id)))
    status = derive_thread_status(rows)
    await session.execute(update(UnstructuredThread).where(UnstructuredThread.id == thread_id,
                                                           UnstructuredThread.status != status).values(status=status))
    return status


async def get_thread(session, thread_id, lock=False):
    statement = select(UnstructuredThread).where(UnstructuredThread.id == thread_id)
    thread = await session.scalar(statement.with_for_update() if lock else statement)
    if thread is None:
        raise UploadError('Folder not found.', 404)
    return thread


class ThreadService:
    def __init__(self, db, storage, cache, settings):
        self.db, self.storage, self.cache, self.settings = db, storage, cache, settings

    def channel(self, thread_id):
        return f'progress:thread:{thread_id}'

    async def notify(self, thread_id):
        # Progress notifications are best effort; Postgres stays the source of truth.
        with suppress(Exception):
            async with asyncio.timeout(1):
                await self.cache.client.publish(self.channel(thread_id), 'changed')

    async def create(self, user_id, request):
        if len(request.files) > self.settings.thread_max_files:
            raise UploadError(f'A folder can hold at most {self.settings.thread_max_files} files.', 413)
        declared, skipped, seen = [], [], set()
        for item in request.files:
            file = validate_declared_file(item.file_name, item.byte_count, item.sha256, self.settings.thread_max_file_bytes)
            if file.sha256 in seen:
                skipped.append(SkippedFile(file_name=file.file_name, reason='Same content as another file in this folder.'))
                continue
            seen.add(file.sha256)
            declared.append(file)
        thread_id = uuid4()
        async with self.db.transaction() as session:
            if request.data_fabric_id is not None:
                if await session.scalar(select(DataFabric).where(
                        DataFabric.id == request.data_fabric_id, DataFabric.created_by == user_id).with_for_update(read=True)) is None:
                    raise UploadError('Project (data_fabric_id) not found.', 404)
            session.add(UnstructuredThread(id=thread_id, name=request.name.strip(), created_by=user_id,
                                           tags=[tag.strip() for tag in request.tags if tag.strip()],
                                           jurisdiction=request.jurisdiction))
            await session.flush()
            if request.data_fabric_id is not None:
                session.add(FabricUnstructuredThread(fabric_id=request.data_fabric_id, thread_id=thread_id))
            rows = []
            for file in declared:
                doc_id = uuid4()
                row = UnstructuredData(
                    id=doc_id, thread_id=thread_id, file_name=file.file_name, file_type=file.kind.file_type,
                    category=file.kind.category, byte_count=file.byte_count, sha256=file.sha256,
                    s3_path=f'THREAD_{thread_id}/{file.kind.file_type}/doc_{doc_id}/{file.file_name}')
                session.add(row)
                rows.append((row, file))
        expires = self.settings.presign_expiry_seconds
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires)
        targets = []
        for row, file in rows:
            url, headers = self.storage.presign_put(row.s3_path, file.kind.content_type, file.byte_count, file.sha256, expires)
            targets.append(UploadTarget(doc_id=row.id, file_name=row.file_name, file_type=row.file_type,
                                        upload_url=url, headers=headers, expires_at=expires_at))
        return ThreadCreateResponse(thread_id=thread_id, status='UPLOADING', files=targets, skipped=skipped)

    async def check_object(self, row):
        """Returns (upload_status, error) for one file: RUNNING if not uploaded yet."""
        stored = await self.storage.inspect(row['s3_path'])
        if stored is None:
            return 'RUNNING', None
        if stored.byte_count != row['byte_count']:
            return 'FAILED', f'Stored size {stored.byte_count} does not match the declared {row["byte_count"]} bytes.'
        digest = stored.sha256_hex or await self.storage.stream_sha256(row['s3_path'])
        if digest != row['sha256']:
            return 'FAILED', 'SHA-256 of the stored file does not match the declared hash.'
        problem = content_problem(row['file_type'], await self.storage.read_head(row['s3_path']))
        return ('FAILED', problem) if problem else ('COMPLETED', None)

    async def complete_upload(self, thread_id, doc_ids=None):
        async with self.db.transaction() as session:
            await get_thread(session, thread_id)
            statement = select(UnstructuredData).where(UnstructuredData.thread_id == thread_id)
            if doc_ids:
                statement = statement.where(UnstructuredData.id.in_(doc_ids))
            rows = list(await session.scalars(statement))
            if doc_ids and len(rows) != len(set(doc_ids)):
                raise UploadError('One or more files are not in this folder.', 404)
            pending = [{'id': r.id, 's3_path': r.s3_path, 'byte_count': r.byte_count, 'sha256': r.sha256,
                        'file_type': r.file_type} for r in rows if r.upload_status == 'RUNNING']
        limit = asyncio.Semaphore(8)

        async def check(row):
            async with limit:
                return row, await self.check_object(row)

        checks = await asyncio.gather(*(check(row) for row in pending))
        failed_keys = []
        async with self.db.transaction() as session:
            await get_thread(session, thread_id, lock=True)
            stages_to_wake = set()
            for row, (outcome, error) in checks:
                if outcome == 'RUNNING':
                    continue
                locked = await session.scalar(select(UnstructuredData).where(
                    UnstructuredData.id == row['id'], UnstructuredData.upload_status == 'RUNNING').with_for_update())
                if locked is None:
                    continue  # a concurrent call already finished this file
                if outcome == 'COMPLETED':
                    first = stages_for(locked.file_type)[0]
                    locked.upload_status, locked.upload_error = 'COMPLETED', None
                    setattr(locked, f'{first}_status', 'WAITING')
                    setattr(locked, f'{first}_updated_at', datetime.now(timezone.utc))
                    stages_to_wake.add(first)
                else:
                    locked.upload_status, locked.upload_error = 'FAILED', error
                    failed_keys.append(locked.s3_path)
            await session.flush()
            for stage in sorted(stages_to_wake):
                queue_message(session, thread_id, stage)
            status = await refresh_thread_status(session, thread_id)
            statement = select(UnstructuredData).where(UnstructuredData.thread_id == thread_id)
            if doc_ids:
                statement = statement.where(UnstructuredData.id.in_(doc_ids))
            results = [FileUploadResult(doc_id=r.id, file_name=r.file_name, upload_status=r.upload_status,
                                        upload_error=r.upload_error)
                       for r in await session.scalars(statement.order_by(UnstructuredData.created_at, UnstructuredData.id))]
        for key in failed_keys:
            with suppress(Exception):
                await self.storage.delete(key)  # rejected content is not kept
        await self.notify(thread_id)
        return CompleteUploadResponse(thread_id=thread_id, thread_status=status, files=results)

    async def status(self, thread_id):
        async with self.db.transaction() as session:
            thread = await get_thread(session, thread_id)
            rows = await session.scalars(select(UnstructuredData).where(UnstructuredData.thread_id == thread_id)
                                         .order_by(UnstructuredData.created_at, UnstructuredData.id))
            files = [FileStatus(
                doc_id=r.id, file_name=r.file_name, file_type=r.file_type, category=r.category,
                byte_count=r.byte_count, sha256=r.sha256, upload_status=r.upload_status, upload_error=r.upload_error,
                stages={stage: StageState(status=getattr(r, f'{stage}_status'), attempts=getattr(r, f'{stage}_attempts'),
                                          error=getattr(r, f'{stage}_error'), updated_at=getattr(r, f'{stage}_updated_at'))
                        for stage in stages_for(r.file_type)},
                created_at=r.created_at, updated_at=r.updated_at) for r in rows]
            return ThreadStatusResponse(thread_id=thread.id, name=thread.name, created_by=thread.created_by,
                                        status=thread.status, tags=thread.tags, jurisdiction=thread.jurisdiction,
                                        created_at=thread.created_at, updated_at=thread.updated_at, files=files)

    async def stream(self, thread_id):
        """SSE: a snapshot whenever the folder changes; stops once the folder reaches a final status."""
        pubsub, last, heartbeat = None, None, time.monotonic()
        try:
            with suppress(Exception):
                async with asyncio.timeout(1):
                    pubsub = self.cache.client.pubsub()
                    await pubsub.subscribe(self.channel(thread_id))
            while True:
                snapshot = await self.status(thread_id)
                data = snapshot.model_dump_json()
                if data != last:
                    yield f'event: status\ndata: {data}\n\n'
                    last = data
                if snapshot.status in TERMINAL_THREAD:
                    break
                if time.monotonic() - heartbeat >= 15:
                    yield ': keepalive\n\n'
                    heartbeat = time.monotonic()
                try:
                    if pubsub is None:
                        raise ConnectionError()
                    await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
                except Exception:
                    if pubsub is not None:
                        with suppress(Exception):
                            await pubsub.aclose()
                    pubsub = None
                    await asyncio.sleep(2)
        finally:
            if pubsub is not None:
                with suppress(Exception):
                    await pubsub.aclose()

    async def cleanup_abandoned(self):
        """Uploads never completed within UPLOAD_ABANDON_SECONDS become FAILED and their objects are deleted."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.settings.upload_abandon_seconds)
        hours = self.settings.upload_abandon_seconds // 3600
        async with self.db.transaction() as session:
            rows = list(await session.scalars(select(UnstructuredData).where(
                UnstructuredData.upload_status == 'RUNNING', UnstructuredData.created_at < cutoff)
                .order_by(UnstructuredData.created_at).limit(200).with_for_update(skip_locked=True)))
            for row in rows:
                row.upload_status = 'FAILED'
                row.upload_error = f'Upload was not completed within {hours} hours.' if hours else 'Upload was not completed in time.'
            await session.flush()
            threads = {row.thread_id for row in rows}
            for thread_id in threads:
                await refresh_thread_status(session, thread_id)
            keys = [row.s3_path for row in rows]
        for key in keys:
            with suppress(Exception):
                await self.storage.delete(key)  # a partial or never-made object
        for thread_id in threads:
            await self.notify(thread_id)
        return len(keys)

    async def run_cleanup(self):
        while True:
            try:
                if count := await self.cleanup_abandoned():
                    logger.info('Marked %s abandoned uploads as FAILED', count)
            except Exception as error:
                logger.warning('Abandoned-upload cleanup will retry (%s)', type(error).__name__)
            await asyncio.sleep(self.settings.upload_cleanup_interval_seconds)
