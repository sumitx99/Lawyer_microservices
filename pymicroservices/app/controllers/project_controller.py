"""Project APIs scoped to the caller's X-User-Id."""
from fastapi import APIRouter, Request

from .unstructured_thread_controller import UserId, user
from ..schemas import (ProjectAttachResponse, ProjectCreateRequest, ProjectDeleteResponse, ProjectDetailResponse,
                       ProjectDetachResponse, ProjectIdentityRequest, ProjectListRequest, ProjectListResponse,
                       ProjectResponse, ProjectThreadsRequest, ProjectUpdateRequest)

router = APIRouter(prefix='/projects', tags=['projects'])


@router.post('/create', response_model=ProjectResponse, status_code=201)
async def create(payload: ProjectCreateRequest, request: Request, user_id: UserId):
    return await request.app.state.projects.create(user(user_id), payload)


@router.post('/list', response_model=ProjectListResponse)
async def list_projects(payload: ProjectListRequest, request: Request, user_id: UserId):
    return await request.app.state.projects.list(user(user_id))


@router.post('/get', response_model=ProjectDetailResponse)
async def get(payload: ProjectIdentityRequest, request: Request, user_id: UserId):
    return await request.app.state.projects.get(user(user_id), payload.project_id)


@router.post('/update', response_model=ProjectResponse)
async def update(payload: ProjectUpdateRequest, request: Request, user_id: UserId):
    return await request.app.state.projects.update(user(user_id), payload)


@router.post('/attach', response_model=ProjectAttachResponse)
async def attach(payload: ProjectThreadsRequest, request: Request, user_id: UserId):
    return await request.app.state.projects.attach(user(user_id), payload)


@router.post('/detach', response_model=ProjectDetachResponse)
async def detach(payload: ProjectThreadsRequest, request: Request, user_id: UserId):
    return await request.app.state.projects.detach(user(user_id), payload)


@router.post('/delete', response_model=ProjectDeleteResponse)
async def delete(payload: ProjectIdentityRequest, request: Request, user_id: UserId):
    return await request.app.state.projects.delete(user(user_id), payload.project_id)
