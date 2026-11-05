"""Collections are folders of the RAG pipeline.

Each collection owns one folder (unstructured_thread) with the same id, so a collection's files are
unstructured_data rows: uploaded with presigned PUTs, checked at complete-upload, then processed
stage by stage by the consumers (standardization -> markdown/image -> metaclassifier).

Access is decided by the collection (security/access.py) before anything here runs; the folder
services below never see a request without it. The sync helpers serve the firm routes (lists,
counts, the admin overview); the async ones wrap ThreadService/RetriggerService."""
from collections import Counter
from contextlib import suppress
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pymicroservices.app.firm_models import Collection
from pymicroservices.app.models import UnstructuredData, UnstructuredThread
from pymicroservices.app.schemas import DeclaredFileRequest
from pymicroservices.app.services.threads.unstructured.thread_service import (
    file_state, get_thread, refresh_thread_status, stages_for)
from pymicroservices.app.utilities.errors import ApiError, bad_request
from pymicroservices.app.utilities.file_validator import KINDS_BY_TYPE, UploadError


def thread_id(collection_id: str) -> UUID:
    return UUID(collection_id)


def as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (TypeError, ValueError):
        return None


# Status shown in the UI ---------------------------------------------------------------------
def document_status(row) -> str:
    """pending_upload -> queued -> processing -> ready | failed, from the upload and stage statuses."""
    state = file_state(row)
    if state == 'uploading':
        return 'pending_upload'
    if state == 'processing':
        started = [getattr(row, f'{stage}_status') for stage in stages_for(row.file_type)]
        return 'queued' if all(status in (None, 'WAITING') for status in started) else 'processing'
    return {'done': 'ready', 'failed': 'failed'}[state]


def document_error(row) -> str | None:
    if row.upload_error:
        return row.upload_error
    for stage in stages_for(row.file_type):
        if getattr(row, f'{stage}_status') == 'FAILED':
            return getattr(row, f'{stage}_error') or f'{stage} failed.'
    return None


def document_out(row) -> dict:
    return {"id": str(row.id), "collection_id": str(row.thread_id), "filename": row.file_name,
            "content_type": KINDS_BY_TYPE[row.file_type].content_type, "file_type": row.file_type,
            "size_bytes": row.byte_count, "status": document_status(row), "error": document_error(row),
            "page_count": row.page_count, "stages": {stage: getattr(row, f'{stage}_status')
                                                      for stage in stages_for(row.file_type)},
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


# Sync reads for the firm routes -----------------------------------------------------------------
def list_documents(db: Session, collection_id: str) -> list[UnstructuredData]:
    return list(db.scalars(select(UnstructuredData).where(
        UnstructuredData.thread_id == thread_id(collection_id), UnstructuredData.upload_status != 'RUNNING')
        .order_by(UnstructuredData.created_at.desc(), UnstructuredData.id.desc())))


def collection_counts(db: Session, collection_ids: list[str]) -> dict[str, int]:
    """Files per collection (uploads still in flight are not counted)."""
    ids = [thread_id(cid) for cid in collection_ids]
    if not ids:
        return {}
    rows = db.execute(select(UnstructuredData.thread_id, func.count()).where(
        UnstructuredData.thread_id.in_(ids), UnstructuredData.upload_status != 'RUNNING')
        .group_by(UnstructuredData.thread_id))
    return {str(tid): n for tid, n in rows}


def firm_document_statuses(db: Session, firm_id: str) -> dict[str, int]:
    collection_ids = db.scalars(select(Collection.id).where(Collection.firm_id == firm_id, Collection.deleted_at.is_(None)))
    ids = [thread_id(cid) for cid in collection_ids]
    if not ids:
        return {}
    rows = db.scalars(select(UnstructuredData).where(UnstructuredData.thread_id.in_(ids)))
    return dict(Counter(document_status(row) for row in rows))


def load_document(db: Session, document_id: str) -> UnstructuredData | None:
    doc_id = as_uuid(document_id)
    return db.get(UnstructuredData, doc_id) if doc_id else None


# Folder operations (async, through the RAG services) ----------------------------------------------
def _api_error(error: UploadError) -> ApiError:
    codes = {404: 'NOT_FOUND', 409: 'CONFLICT', 413: 'FILE_TOO_LARGE', 415: 'FILE_TYPE_NOT_ALLOWED'}
    return ApiError(error.status_code, codes.get(error.status_code, 'UPLOAD_REJECTED'), str(error))


async def ensure_folder(threads, collection: Collection, created_by: str) -> UUID:
    """The collection's folder, created on first use."""
    folder = thread_id(collection.id)
    async with threads.db.transaction() as session:
        await session.execute(insert(UnstructuredThread).values(id=folder, name=collection.name[:255],
                                                                created_by=created_by).on_conflict_do_nothing())
    return folder


async def start_upload(threads, collection: Collection, created_by: str, filename: str, size: int, sha256: str):
    """(document row, upload instructions) for one file the browser is about to PUT."""
    folder = await ensure_folder(threads, collection, created_by)
    try:
        created = await threads.add_files(folder, [DeclaredFileRequest(file_name=filename, byte_count=size,
                                                                       sha256=sha256)])
    except UploadError as error:
        raise _api_error(error) from None
    if not created.files:
        raise ApiError(409, 'DUPLICATE_FILE', 'This file is already in the collection.')
    target = created.files[0]
    async with threads.db.transaction() as session:
        row = await session.get(UnstructuredData, target.doc_id)
    return row, {"url": target.upload_url, "method": target.method, "headers": target.headers,
                 "expires_at": target.expires_at.isoformat()}


async def complete_upload(threads, row: UnstructuredData) -> UnstructuredData:
    """Re-check the stored object; on success the first stage is WAITING and the consumers are woken."""
    try:
        result = await threads.complete_upload(row.thread_id, [row.id])
    except UploadError as error:
        raise _api_error(error) from None
    if result.files[0].upload_status == 'RUNNING':
        raise bad_request('UPLOAD_MISSING', "We couldn't find the uploaded file. Try uploading again.")
    async with threads.db.transaction() as session:
        return await session.get(UnstructuredData, row.id)


async def retry(retrigger, user_id: str, row: UnstructuredData) -> UnstructuredData:
    try:
        result = (await retrigger.retrigger(user_id, row.thread_id, [row.id])).results[0]
    except UploadError as error:
        raise _api_error(error) from None
    if result.outcome == 'running':
        raise ApiError(409, 'STILL_RUNNING', result.reason)
    if result.outcome != 'retriggered':
        raise bad_request('NOT_RETRIGGERABLE', result.reason)
    async with retrigger.db.transaction() as session:
        return await session.get(UnstructuredData, row.id)


async def delete_document(threads, row: UnstructuredData) -> None:
    """Removes the file and everything derived from it (chunks, images, metadata cascade)."""
    async with threads.db.transaction() as session:
        await get_thread(session, row.thread_id, lock=True)
        await session.execute(delete(UnstructuredData).where(UnstructuredData.id == row.id))
        await refresh_thread_status(session, row.thread_id)
    for key in (row.s3_path, row.s3_path_tomarkdown):
        if key:
            with suppress(Exception):
                await threads.storage.delete(key)
    await threads.notify(row.thread_id)
