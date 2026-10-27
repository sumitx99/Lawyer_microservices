"""Folder APIs (design doc): create, complete-upload, status, events (SSE) and retrigger.

X-User-Id identifies the caller until authentication exists; it is recorded as the folder's
created_by (used for fair scheduling) and in the retrigger audit log.
"""
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..schemas import (CompleteUploadRequest, CompleteUploadResponse, RetriggerRequest, RetriggerResponse,
                       ThreadCreateRequest, ThreadCreateResponse, ThreadIdentityRequest, ThreadStatusResponse)
from ..utilities.file_validator import UploadError
from ..utilities.input_sanitizer import clean_user_id

router = APIRouter(prefix='/threads/unstructured', tags=['folders'])
UserId = Annotated[str, Header(alias='X-User-Id', min_length=1, max_length=128)]


def user(value):
    try:
        return clean_user_id(value)
    except ValueError as error:
        raise UploadError(str(error)) from None


@router.post('/create', response_model=ThreadCreateResponse, status_code=201)
async def create(payload: ThreadCreateRequest, request: Request, user_id: UserId):
    return await request.app.state.threads.create(user(user_id), payload)


@router.post('/complete-upload', response_model=CompleteUploadResponse)
async def complete_upload(payload: CompleteUploadRequest, request: Request):
    return await request.app.state.threads.complete_upload(payload.thread_id, payload.doc_ids)


@router.post('/status', response_model=ThreadStatusResponse)
async def status(payload: ThreadIdentityRequest, request: Request):
    return await request.app.state.threads.status(payload.thread_id)


@router.post('/events')
async def events(payload: ThreadIdentityRequest, request: Request):
    await request.app.state.threads.status(payload.thread_id)  # 404 before the stream starts
    return StreamingResponse(request.app.state.threads.stream(payload.thread_id), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.post('/retrigger', response_model=RetriggerResponse,
             responses={409: {'description': 'Every selected stage is still running.'}})
async def retrigger(payload: RetriggerRequest, request: Request, user_id: UserId):
    result = await request.app.state.retrigger.retrigger(user(user_id), payload.thread_id, payload.doc_ids, payload.stage)
    outcomes = {item.outcome for item in result.results}
    if 'retriggered' not in outcomes and 'running' in outcomes:
        return JSONResponse(result.model_dump(mode='json'), status_code=409)
    return result
