"""Document APIs. Query generation endpoints are implemented in a later step."""
import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from ..schemas import DocumentIdentityRequest, DocumentList, DocumentListRequest, DocumentResponse
from ..utilities.file_validator import validate_file

router = APIRouter(prefix='/documents', tags=['documents'])


@router.post('', response_model=DocumentResponse, status_code=201)
async def upload_document(request: Request, response: Response,
                          project_id: Annotated[UUID, Form()], file: Annotated[UploadFile, File()],
                          idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
    try:
        maximum = request.app.state.settings.max_upload_bytes
        data = await file.read(maximum + 1)
        validated = await asyncio.to_thread(validate_file, file.filename, file.content_type, data, maximum)
        document, created = await request.app.state.documents.upload(project_id, validated, idempotency_key)
        response.status_code = 201 if created else 200
        await request.app.state.status.notify(document)
        return document
    finally:
        await file.close()


@router.post('/list', response_model=DocumentList)
async def list_documents(payload: DocumentListRequest, request: Request):
    items = await request.app.state.documents.list(payload.project_id, payload.limit, payload.offset)
    return DocumentList(items=items, limit=payload.limit, offset=payload.offset)


@router.post('/status', response_model=DocumentResponse)
async def document_status(payload: DocumentIdentityRequest, request: Request):
    return await request.app.state.documents.get(payload.project_id, payload.doc_id)


@router.post('/events')
async def document_events(payload: DocumentIdentityRequest, request: Request):
    await request.app.state.documents.get(payload.project_id, payload.doc_id)
    return StreamingResponse(request.app.state.status.stream(payload.project_id, payload.doc_id), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
